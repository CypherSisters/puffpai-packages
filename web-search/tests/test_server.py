"""Web search's own server: every tool, the token, the address rules and a bad input. Outside calls
are replaced; these tests never touch the network. Each server listens on a free port on 127.0.0.1
and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "s" * 20

_spec = importlib.util.spec_from_file_location("web_search_server", PKG / "server.py")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

ANSWER = {
    "results": [
        {"title": "Storing basil", "content": "Keep it in water\non the counter.", "url": "https://a.example/basil",
         "engine": "duckduckgo"},
        {"title": "A picture", "url": "https://img.example/1", "category": "images", "template": "images.html"},
        {"title": "Bad link", "url": "javascript:alert(1)", "engine": "x"},
        {"title": "Same again", "url": "https://a.example/basil", "engine": "bing"},
    ] + [{"title": f"Result {i}", "content": "x" * 400, "url": f"https://r{i}.example/", "engines": ["brave"]}
         for i in range(10)],
}


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
def asked(monkeypatch):
    """Every outside call is answered here and remembered as (url, direct). Name lookups too."""
    calls = []

    def fake(url, direct):
        calls.append((url, direct))
        path = urllib.parse.urlsplit(url).path
        if path.endswith("/search"):
            return 200, json.dumps(ANSWER).encode()
        if path.endswith("/healthz"):
            return 200, b"OK"
        raise AssertionError(url)

    monkeypatch.setattr(ws, "http_get", fake)
    monkeypatch.setattr(ws, "_resolve", lambda host, port: {"searxng.lan": ["192.168.1.30"],
                                                            "evil.lan": ["192.168.1.30", "93.184.216.34"],
                                                            "localhost": ["127.0.0.1", "::1"]}[host])
    return calls


@pytest.fixture
def base(tmp_path, asked):
    with running(ws.make_server(0, tmp_path / "data", TOKEN)) as url:
        yield url


def call(url, method="GET", body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method, headers=h)
    try:
        with _open(req, timeout=10) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def tool(base, name, error=False, **args):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": args}})
    assert code == 200 and out["result"]["isError"] is error, out
    return out["result"]["content"][0]["text"]


def test_no_address_yet_says_so(base, asked):
    assert tool(base, "web_search", question="basil") == ws.NO_ADDRESS
    assert tool(base, "search_settings") == ws.NO_ADDRESS
    assert asked == []


def test_set_address_then_web_search_sends_only_the_words(base, asked, tmp_path):
    assert "home network" in tool(base, "set_address", address="http://192.168.1.20:8888/")
    saved = json.loads((tmp_path / "data" / "search.json").read_text(encoding="utf-8"))
    assert saved["address"] == "http://192.168.1.20:8888"
    out = tool(base, "web_search", question="how to store basil")
    url, direct = asked[-1]
    parts = urllib.parse.urlsplit(url)
    assert direct is False and parts.netloc == "192.168.1.20:8888" and parts.path == "/search"
    assert urllib.parse.parse_qs(parts.query) == {"q": ["how to store basil"], "format": ["json"],
                                                  "categories": ["general"]}
    lines = out.splitlines()
    assert lines[0] == "Top 8 results from your SearXNG:"
    assert lines[1:4] == ["1. Storing basil", "   Keep it in water on the counter.",
                          "   https://a.example/basil (from duckduckgo)"]
    assert "img.example" not in out and "javascript" not in out and out.count("a.example/basil") == 1
    assert "8. Result 6" in out and "Result 7" not in out and "(from brave)" in out
    assert max(len(l) for l in lines) <= 205


def test_news_search_and_loopback_is_direct(base, asked):
    assert "on this computer" in tool(base, "set_address", address="http://localhost:8888")
    assert tool(base, "news_search", question="mars mission").startswith("Top 8 news stories")
    url, direct = asked[-1]
    assert direct is True and "categories=news" in url


def test_search_settings_checks_healthz(base, asked):
    tool(base, "set_address", address="searxng.lan:8080")
    out = tool(base, "search_settings")
    assert out.startswith("Web search asks the SearXNG at http://searxng.lan:8080") and "health check" in out
    assert asked[-1] == ("http://searxng.lan:8080/healthz", False)


@pytest.mark.parametrize("address,why", [
    ("https://searx.be", "would see every question"),
    ("http://8.8.8.8:8888", "would see every question"),
    ("http://evil.lan", "outside your home network"),
    ("http://100.64.0.1", "not an address on this computer or a home network"),
    ("ftp://192.168.1.2", "http:// or https://"),
    ("http://me:pw@192.168.1.2", "name or password"),
    ("http://192.168.1.2:99999", "port"),
])
def test_set_address_refuses_what_is_not_the_owners(base, asked, tmp_path, address, why):
    assert why in tool(base, "set_address", error=True, address=address)
    assert not (tmp_path / "data" / "search.json").exists() and asked == []


def test_bad_input_is_a_plain_error(base):
    tool(base, "set_address", address="http://127.0.0.1:8888")
    assert "did not fit" in tool(base, "web_search", error=True, question="basil", category="images")
    assert "did not fit" in tool(base, "web_search", error=True, question="x" * 301)


def test_a_hand_edited_public_address_is_refused_at_call_time(base, asked, tmp_path):
    (tmp_path / "data" / "search.json").write_text(json.dumps({"address": "https://searx.be"}), encoding="utf-8")
    assert "not one Web search will ask" in tool(base, "web_search", error=True, question="basil")
    assert asked == []


def test_every_route_refuses_without_the_token(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")


def test_manifest_matches_the_live_tool_list(base):
    m = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[1]["result"]["tools"]
    assert m["abilities"]["tools"] == {t["name"]: t["description"] for t in tools}
    assert set(m["abilities"]["examples"]) == set(m["abilities"]["tools"])
    assert {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]} == set(m["abilities"]["changes"])
    assert m["needs"]["hosts"] == []


def test_redirects_stay_on_the_same_host():
    h = ws._SameOrigin()
    req = urllib.request.Request("http://192.168.1.20:8888/search?q=x")
    with pytest.raises(ws.Offline):
        h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest")
    with pytest.raises(ws.Offline):
        h.redirect_request(req, None, 302, "Found", {}, "https://192.168.1.20:8888/search")
    assert h.redirect_request(req, None, 302, "Found", {}, "http://192.168.1.20:8888/search/") is not None
