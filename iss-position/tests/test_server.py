"""ISS position's server (iss-position/server.py): the one tool, the plain place, the token on every
route and a bad answer from the website. The website is never reached: urllib is replaced."""

import contextlib
import importlib.util
import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("iss_server", PKG / "server.py")
iss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iss)

# The reading from HomeBot's tests_fixtures.json.
FIXTURE = {"name": "iss", "id": 25544, "latitude": 37.148923, "longitude": -122.874382, "altitude": 421.63,
           "velocity": 27583.4, "visibility": "daylight", "timestamp": 1757980000}

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class FakeOpener:
    """Stands in for the package's urllib opener. Records each URL; answers with *body*."""

    def __init__(self, body):
        self.body = body
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        assert timeout and timeout <= iss.CALL_TIMEOUT_S
        if isinstance(self.body, Exception):
            raise self.body
        return contextlib.closing(io.BytesIO(self.body))


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.fixture
def web(monkeypatch):
    fake = FakeOpener(json.dumps(FIXTURE).encode())
    monkeypatch.setattr(iss, "_opener", fake)
    return fake


@pytest.fixture
def base(tmp_path, web):
    with running(iss.make_server(0, tmp_path, TOKEN)) as url:
        yield url


def mcp(base, method, params=None, token=TOKEN):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + "/mcp", data=body, method="POST", headers=h)
    try:
        with _open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_iss_position_reads_the_fixture_and_names_the_place(base, web, tmp_path):
    code, out = mcp(base, "tools/call", {"name": "iss_position", "arguments": {}})
    assert code == 200 and out["result"]["isError"] is False
    text = out["result"]["content"][0]["text"]
    assert "over the Pacific Ocean, west of the United States" in text
    assert "latitude 37.1, longitude -122.9" in text
    assert "about 422 km up" in text and "27,583 km/h" in text
    assert web.urls == [iss.URL]
    assert list(tmp_path.iterdir()) == []          # stores nothing


def test_tool_list_and_plain_places():
    assert [t["name"] for t in iss.tool_list()] == ["iss_position"]
    assert iss.describe(-10, -60) == "over Brazil"
    assert iss.describe(-12, -77.5).startswith("over the Pacific Ocean") and "of Peru" in iss.describe(-12, -77.5)


def test_every_route_needs_the_token(base):
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert mcp(base, "tools/list", token="wrong")[0] == 401
    req = urllib.request.Request(base + "/health")
    with pytest.raises(urllib.error.HTTPError) as e:
        _open(req, timeout=10)
    assert e.value.code == 401
    req = urllib.request.Request(base + "/health", headers={"X-PuffPAI-Key": TOKEN})
    with _open(req, timeout=10) as r:
        assert r.status == 200


def test_a_bad_answer_or_no_answer_is_a_plain_error(base, web):
    web.body = b'{"latitude": "nope"}'
    out = mcp(base, "tools/call", {"name": "iss_position", "arguments": {}})[1]["result"]
    assert out["isError"] and "latitude" in out["content"][0]["text"]
    web.body = urllib.error.URLError("down")
    web.urls.clear()
    out = mcp(base, "tools/call", {"name": "iss_position", "arguments": {}})[1]["result"]
    assert out["isError"] and "api.wheretheiss.at" in out["content"][0]["text"]
    assert len(web.urls) == iss.MAX_REQUESTS == 2
    web.body = b"x" * (iss.MAX_ANSWER + 10)
    out = mcp(base, "tools/call", {"name": "iss_position", "arguments": {}})[1]["result"]
    assert out["isError"] and "64 KB" in out["content"][0]["text"]
    out = mcp(base, "tools/call", {"name": "iss_position", "arguments": {"where": "x"}})[1]["result"]
    assert out["isError"]
