"""Web search's own server: every tool, the token, the address rules and a bad input. Outside calls
are replaced; these tests never touch the network. Each server listens on a free port on 127.0.0.1
and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    assert "through the Bridge" in tool(base, "set_address", address="http://192.168.1.20:8888/")
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


# ---------------------------------------------------------------- asking the Core for a Yes card (D110)

CORE_TOKEN = "core-token-" + "c" * 20
ASK = "/v1/bridge/addresses/ask"
WAITING = (202, {"state": "waiting", "line": "Waiting for the owner's Yes. Nothing opens before that."})
ALLOWED = (200, {"state": "allowed", "line": "Allowed. The owner said Yes to this address and port."})
STALE = (202, {"state": "waiting", "line": "The owner's Yes for this address can't be used right now."})
REFUSED = (429, {"state": "refused", "error": "Too many.", "line": "This package has asked for too many addresses this hour."})


class FakeCore(BaseHTTPRequestHandler):
    """The one Core route a package asks. It records each ask and answers what the test set."""
    state: dict

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.state["asks"].append({"path": self.path, "auth": self.headers.get("Authorization"),
                                   "type": self.headers.get("Content-Type"), "body": json.loads(raw)})
        code, reply = self.state["reply"]
        out = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FakeProxy(BaseHTTPRequestHandler):
    """Stands in for the Bridge proxy: it records what it is asked for, reaches nothing, and answers what the test set."""
    state: dict

    def log_message(self, *a):
        pass

    def _seen(self):
        self.state["seen"].append((self.command, self.path, self.headers.get("Host") if self.command != "CONNECT" else None))
        code, out = self.state["reply"]
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_CONNECT = _seen


@pytest.fixture(autouse=True)
def no_core(monkeypatch):
    """No test reaches a running Core: a test that wants one starts its own fake."""
    monkeypatch.delenv("PUFFPAI_API", raising=False)
    monkeypatch.delenv("PUFFPAI_TOKEN", raising=False)


@pytest.fixture
def core(monkeypatch):
    """A fake Core on a free loopback port, in this process's environment as the Core sets it: no /v1 in PUFFPAI_API."""
    state = {"asks": [], "reply": WAITING}
    server = ThreadingHTTPServer(("127.0.0.1", 0), type("Core", (FakeCore,), {"state": state}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PUFFPAI_API", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("PUFFPAI_TOKEN", CORE_TOKEN)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(5)


@pytest.fixture
def proxy(monkeypatch):
    """A fake Bridge proxy in the environment. Nothing passes it, so no test dials a real address."""
    state = {"seen": [], "reply": (403, b"")}
    server = ThreadingHTTPServer(("127.0.0.1", 0), type("Proxy", (FakeProxy,), {"state": state}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://pkg:secret@127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("HTTP_PROXY", url)
    monkeypatch.setenv("HTTPS_PROXY", url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    yield state
    server.shutdown()
    server.server_close()
    thread.join(5)


def a_closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def one_ask(core, place):
    """Exactly one ask, with the package's token, the right path with no doubled /v1, and this one host:port."""
    assert core["asks"] == [{"path": ASK, "auth": f"Bearer {CORE_TOKEN}", "type": "application/json",
                             "body": {"address": place}}]


@pytest.mark.parametrize("given, url, place", [
    ("http://192.168.1.20:8888/", "http://192.168.1.20:8888", "192.168.1.20:8888"),
    ("SearXNG.lan:8080", "http://searxng.lan:8080", "searxng.lan:8080"),
    ("http://searxng.lan", "http://searxng.lan", "searxng.lan:80"),
    ("https://searxng.lan/search-box/", "https://searxng.lan/search-box", "searxng.lan:443"),
    ("http://[fe80::1]:8888", "http://[fe80::1]:8888", "[fe80::1]:8888"),
])
def test_the_ask_carries_the_token_and_the_exact_saved_place(base, tmp_path, core, given, url, place):
    assert tool(base, "set_address", address=given).startswith(f"Saved. Web search will ask the SearXNG at {url},")
    one_ask(core, place)
    assert json.loads((tmp_path / "data" / "search.json").read_text(encoding="utf-8"))["address"] == url


def test_a_proxy_in_the_environment_is_not_used_for_the_ask(base, core, proxy):
    tool(base, "set_address", address="http://192.168.1.20:8888")
    one_ask(core, "192.168.1.20:8888")
    assert proxy["seen"] == []                       # set_address dials nothing, and the ask does not pass the proxy


@pytest.mark.parametrize("reply, says", [
    (ALLOWED, ["Allowed. The owner said Yes", "can reach 192.168.1.20:8888 now, while the Bridge is open"]),
    (WAITING, ["Waiting for the owner's Yes", "A Yes card for 192.168.1.20:8888 is waiting", "until her Yes"]),
    (STALE, ["can't be used right now", "Nothing reaches 192.168.1.20:8888 for now"]),
    (REFUSED, ["too many addresses this hour", "No card was raised"]),
])
def test_each_reply_gives_true_words_and_the_address_stays_saved(base, tmp_path, core, reply, says):
    core["reply"] = reply
    text = tool(base, "set_address", address="http://192.168.1.20:8888")
    assert text.startswith("Saved.") and all(w in text for w in says), text
    assert (reply is not STALE) or "Yes card" not in text        # no card is said where the Core raised none
    assert (tmp_path / "data" / "search.json").exists()


@pytest.mark.parametrize("reply", [
    (200, b"not json"), (500, b"<html>oops</html>"), (401, {"error": "no"}), (200, {"state": "granted", "line": "yes"}),
    (500, {"state": "allowed", "line": "yes"}), (200, {"state": ["allowed"], "line": "yes"}), (202, {"state": "waiting", "line": ""}),
])
def test_a_reply_that_does_not_parse_says_saved_and_no_card(base, tmp_path, core, reply):
    core["reply"] = reply
    text = tool(base, "set_address", address="http://192.168.1.20:8888")
    assert text.startswith("Saved.") and "No Yes card could be raised" in text and "Bridge page" in text
    assert (tmp_path / "data" / "search.json").exists()


def test_the_cores_line_is_capped_and_never_says_the_token(base, core):
    core["reply"] = (202, {"state": "waiting", "line": f"Waiting {CORE_TOKEN}\n" + "x" * 5000})
    text = tool(base, "set_address", address="http://192.168.1.20:8888")
    assert CORE_TOKEN not in text and "\n" not in text and "x" * 300 not in text


def test_no_api_no_token_or_the_core_down_says_saved_promptly(base, tmp_path, monkeypatch):
    for api, token in ((None, CORE_TOKEN), ("http://127.0.0.1:9", None), (f"http://127.0.0.1:{a_closed_port()}", CORE_TOKEN)):
        for name, value in (("PUFFPAI_API", api), ("PUFFPAI_TOKEN", token)):
            monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
        started = time.monotonic()
        text = tool(base, "set_address", address="http://192.168.1.20:8888")
        assert time.monotonic() - started < 6 and text.startswith("Saved.")
        assert "No Yes card could be raised" in text and CORE_TOKEN not in text
        assert (tmp_path / "data" / "search.json").exists()


def test_a_core_that_never_answers_does_not_hold_the_reply(base, monkeypatch):
    monkeypatch.setattr(ws, "ASK_TIMEOUT_S", 0.3)
    with socket.socket() as quiet:
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(1)
        monkeypatch.setenv("PUFFPAI_API", f"http://127.0.0.1:{quiet.getsockname()[1]}")
        monkeypatch.setenv("PUFFPAI_TOKEN", CORE_TOKEN)
        started = time.monotonic()
        text = tool(base, "set_address", address="http://192.168.1.20:8888")
    assert time.monotonic() - started < 3 and "No Yes card could be raised" in text


@pytest.mark.parametrize("given", ["http://127.0.0.1:8888", "http://localhost:8888", "http://[::1]:8888"])
def test_an_address_on_this_computer_asks_nothing_and_keeps_its_words(base, core, given):
    assert tool(base, "set_address", address=given).endswith(", on this computer.")
    assert core["asks"] == []


def test_the_manifest_declares_lan():
    m = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert m["needs"]["addresses"] == "lan" and m["needs"]["hosts"] == []


@pytest.mark.parametrize("url, sent", [
    ("http://searxng.lan:8080/search?q=x", ("GET", "http://searxng.lan:8080/search?q=x", "searxng.lan:8080")),
    ("http://192.168.1.20:8888/search?q=x", ("GET", "http://192.168.1.20:8888/search?q=x", "192.168.1.20:8888")),
    ("https://searxng.lan:8443/search?q=x", ("CONNECT", "searxng.lan:8443", None)),
])
def test_the_proxy_is_asked_for_the_name_as_saved(proxy, url, sent):
    """A grant for a name matches the name only, so the proxy must see the name, unchanged, and never its answer."""
    proxy["reply"] = (200, b"{}")
    with contextlib.suppress(ws.Offline):
        ws.http_get(url, False)
    assert proxy["seen"] and proxy["seen"][0] == sent
