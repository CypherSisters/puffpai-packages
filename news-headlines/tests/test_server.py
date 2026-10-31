"""News headlines' own server: every tool, the token, bad input, and the fetch guards. Outside
calls are replaced; these tests never touch the internet. Each server listens on a free port on
127.0.0.1 and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "n" * 20

_spec = importlib.util.spec_from_file_location("news_server", PKG / "server.py")
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Paper</title>
<item><title>Older &amp; wiser</title><link>https://paper.example.org/a</link>
<pubDate>Thu, 29 Oct 2026 08:00:00 GMT</pubDate><description>body text</description></item>
<item><title>Newest <b>story</b></title><link>https://paper.example.org/b</link>
<pubDate>Fri, 30 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title type="html">Middle &lt;i&gt;post&lt;/i&gt;</title>
<link rel="self" href="https://blog.example.net/self"/><link href="https://blog.example.net/p1"/>
<updated>2026-10-29T20:00:00Z</updated></entry></feed>"""


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
def fetched(monkeypatch):
    """Feeds are answered here; every address asked for is remembered."""
    calls = []

    def fake(url):
        calls.append(url)
        if "paper" in url:
            return RSS
        if "blog" in url:
            return ATOM
        raise ns.Offline("Could not reach down.example.com (blocked). Is it allowed?")

    monkeypatch.setattr(ns, "fetch", fake)
    return calls


@pytest.fixture
def base(tmp_path, fetched):
    with running(ns.make_server(0, tmp_path / "data", TOKEN)) as url:
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


def tool(base, tool_name, /, error=False, **args):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": tool_name, "arguments": args}})
    assert code == 200 and out["result"]["isError"] is error, out
    return out["result"]["content"][0]["text"]


def test_add_list_remove(base, tmp_path, fetched):
    assert "No news feeds yet" in tool(base, "list_feeds")
    out = tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert "No Yes card could be raised" in out and "allow this address and port on the Bridge page" in out
    assert "already" in tool(base, "add_feed", url="https://paper.example.org/rss", name="Other")
    assert tool(base, "list_feeds") == "1 news feed:\n- Paper: https://paper.example.org/rss"
    saved = json.loads((tmp_path / "data" / "feeds.json").read_text(encoding="utf-8"))
    assert [f["name"] for f in saved["feeds"]] == ["Paper"]
    assert fetched == []                                     # adding never fetches
    assert tool(base, "remove_feed", feed="paper").startswith("Removed the feed Paper.")
    assert "No news feeds yet" in tool(base, "list_feeds")


def test_headlines_across_feeds_newest_first_then_cached(base, fetched):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    tool(base, "add_feed", url="https://blog.example.net/atom", name="Blog")
    tool(base, "add_feed", url="https://down.example.com/rss", name="Down")
    out = tool(base, "headlines", count=3)
    lines = out.splitlines()
    assert lines[1].startswith("1. Newest story (Paper, ") and lines[1].endswith(" https://paper.example.org/b")
    assert lines[2].startswith("2. Middle post (Blog, ") and lines[2].endswith(" https://blog.example.net/p1")
    assert lines[3].startswith("3. Older & wiser (Paper, ")
    assert "Could not load: Down: Could not reach" in out and "body text" not in out
    n = len(fetched)
    one = tool(base, "headlines", feed="Paper")
    assert len(fetched) == n                                 # fresh in the cache: not fetched again
    assert "Blog" not in one
    assert "3. Bad link (Paper, time not given)" in one and "javascript" not in one


@pytest.mark.parametrize("url", ["ftp://example.org/feed", "file:///C:/x.xml", "http://192.168.1.5/rss",
                                 "http://127.0.0.1:8123/rss", "http://[::1]/rss", "http://localhost/rss",
                                 "http://router.lan/rss", "https://user:pw@example.org/rss", "http://169.254.169.254/"])
def test_bad_or_private_addresses_are_refused(base, url):
    assert "did not fit add_feed" in tool(base, "add_feed", error=True, url=url, name="x")


def test_bad_count_is_a_plain_error(base):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert "count is a number from 1 to 30" in tool(base, "headlines", error=True, count=99)


def test_fetch_refuses_a_name_that_points_at_a_private_address(monkeypatch):
    monkeypatch.setattr(ns, "_resolve", lambda host, port: ["10.0.0.7"])
    with pytest.raises(ns.Offline, match="private address"):
        ns.fetch("https://sneaky.example.org/rss")


def test_parser_refuses_entities_and_non_feeds():
    with pytest.raises(ns.Offline, match="entities"):
        ns.parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss>&a;</rss>')
    with pytest.raises(ns.Offline, match="did not send an RSS"):
        ns.parse_feed(b"<html><body>not xml")


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


@pytest.mark.parametrize("url, place", [
    ("https://Paper.Example.org/rss", "paper.example.org:443"),
    ("http://paper.example.org/rss", "paper.example.org:80"),
    ("https://paper.example.org:8443/rss", "paper.example.org:8443"),
    ("http://paper.example.org:8080/rss", "paper.example.org:8080"),
])
def test_the_ask_carries_the_token_and_the_site_and_port(base, tmp_path, core, fetched, url, place):
    assert tool(base, "add_feed", url=url, name="Paper").startswith(f"Saved the feed Paper ({url}).")
    one_ask(core, place)
    assert [f["url"] for f in json.loads((tmp_path / "data" / "feeds.json").read_text(encoding="utf-8"))["feeds"]] == [url]
    assert fetched == []                                     # adding never fetches, and the ask is not a fetch


def test_a_proxy_in_the_environment_is_not_used_for_the_ask(base, core, proxy):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    one_ask(core, "paper.example.org:443")
    assert proxy["seen"] == []


def test_adding_a_feed_again_asks_again_and_says_so(base, tmp_path, core, fetched):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    tool(base, "add_feed", url="https://blog.example.net/atom", name="Blog")
    assert [a["body"]["address"] for a in core["asks"]] == ["paper.example.org:443", "blog.example.net:443"]
    core["asks"].clear()
    out = tool(base, "add_feed", url="https://PAPER.example.org/rss", name="Other")
    assert "already a feed with that address" in out and "Asked again" in out and "A Yes card for paper.example.org:443" in out
    one_ask(core, "paper.example.org:443")
    feeds = json.loads((tmp_path / "data" / "feeds.json").read_text(encoding="utf-8"))["feeds"]
    assert [f["name"] for f in feeds] == ["Paper", "Blog"] and fetched == []


def test_a_name_already_used_for_another_address_asks_nothing(base, core):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    core["asks"].clear()
    assert "already a feed called Paper" in tool(base, "add_feed", url="https://other.example.org/rss", name="Paper")
    assert core["asks"] == []


def test_a_feed_can_ask_again_when_the_list_is_full(base, tmp_path, core):
    store = ns.Store(tmp_path / "data")
    store.save({"feeds": [{"name": f"n{i}", "url": f"https://s{i}.example.org/rss"} for i in range(ns.MAX_FEEDS)]})
    assert "Asked again" in tool(base, "add_feed", url="https://s3.example.org/rss")
    one_ask(core, "s3.example.org:443")
    core["asks"].clear()
    assert "Remove one first" in tool(base, "add_feed", url="https://new.example.org/rss")
    assert core["asks"] == []


@pytest.mark.parametrize("reply, says", [
    (ALLOWED, ["Allowed. The owner said Yes", "can reach paper.example.org:443 now, while the Bridge is open"]),
    (WAITING, ["Waiting for the owner's Yes", "A Yes card for paper.example.org:443 is waiting", "until her Yes",
               "Only one card waits at a time", "adding this feed again raises it again"]),
    (STALE, ["can't be used right now", "Nothing reaches paper.example.org:443 for now"]),
    (REFUSED, ["too many addresses this hour", "No card was raised"]),
])
def test_each_reply_gives_true_words_and_the_feed_stays_saved(base, tmp_path, core, reply, says):
    core["reply"] = reply
    text = tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert text.startswith("Saved the feed Paper") and all(w in text for w in says), text
    assert (reply is not STALE) or "Yes card" not in text        # no card is said where the Core raised none
    assert (tmp_path / "data" / "feeds.json").exists()


@pytest.mark.parametrize("reply", [
    (200, b"not json"), (500, b"<html>oops</html>"), (401, {"error": "no"}), (200, {"state": "granted", "line": "yes"}),
    (500, {"state": "allowed", "line": "yes"}), (200, {"state": ["allowed"], "line": "yes"}), (202, {"state": "waiting", "line": ""}),
])
def test_a_reply_that_does_not_parse_says_saved_and_no_card(base, tmp_path, core, reply):
    core["reply"] = reply
    text = tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert text.startswith("Saved the feed") and "No Yes card could be raised" in text and "Bridge page" in text
    assert (tmp_path / "data" / "feeds.json").exists()


def test_the_cores_line_is_capped_and_never_says_the_token(base, core):
    core["reply"] = (202, {"state": "waiting", "line": f"Waiting {CORE_TOKEN}\n" + "x" * 5000})
    text = tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert CORE_TOKEN not in text and "\n" not in text and "x" * 300 not in text


def test_no_api_no_token_or_the_core_down_says_saved_promptly(base, tmp_path, monkeypatch):
    for i, (api, token) in enumerate(((None, CORE_TOKEN), ("http://127.0.0.1:9", None),
                                      (f"http://127.0.0.1:{a_closed_port()}", CORE_TOKEN))):
        for name, value in (("PUFFPAI_API", api), ("PUFFPAI_TOKEN", token)):
            monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
        started = time.monotonic()
        text = tool(base, "add_feed", url=f"https://s{i}.example.org/rss", name=f"Feed {i}")
        assert time.monotonic() - started < 6 and text.startswith(f"Saved the feed Feed {i}")
        assert "No Yes card could be raised" in text and CORE_TOKEN not in text
    assert len(json.loads((tmp_path / "data" / "feeds.json").read_text(encoding="utf-8"))["feeds"]) == 3


def test_a_core_that_never_answers_does_not_hold_the_reply(base, monkeypatch):
    monkeypatch.setattr(ns, "ASK_TIMEOUT_S", 0.3)
    with socket.socket() as quiet:
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(1)
        monkeypatch.setenv("PUFFPAI_API", f"http://127.0.0.1:{quiet.getsockname()[1]}")
        monkeypatch.setenv("PUFFPAI_TOKEN", CORE_TOKEN)
        started = time.monotonic()
        text = tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    assert time.monotonic() - started < 3 and "No Yes card could be raised" in text


def test_a_refused_address_asks_nothing(base, core):
    assert "did not fit add_feed" in tool(base, "add_feed", error=True, url="http://router.lan/rss", name="x")
    assert core["asks"] == []


def test_removing_a_feed_says_the_site_stays_allowed(base, core):
    tool(base, "add_feed", url="https://paper.example.org/rss", name="Paper")
    core["asks"].clear()
    out = tool(base, "remove_feed", feed="Paper")
    assert "paper.example.org was allowed, it stays allowed" in out and "removes it on the Bridge page" in out
    assert core["asks"] == []


def test_the_manifest_declares_internet():
    m = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert m["needs"]["addresses"] == "internet" and m["needs"]["hosts"] == []
    assert "its own Yes" in m["description"] and "on the internet" in m["description"]
    assert "its own Yes" in m["consent"] and "on the internet" in m["consent"]


@pytest.mark.parametrize("url, sent", [
    ("http://paper.example.org:8080/rss", ("GET", "http://paper.example.org:8080/rss", "paper.example.org:8080")),
    ("https://paper.example.org/rss", ("CONNECT", "paper.example.org:443", None)),
])
def test_the_proxy_is_asked_for_the_name_and_the_answers_are_still_checked(proxy, monkeypatch, url, sent):
    monkeypatch.setattr(ns, "_resolve", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(ns, "_OPENER", urllib.request.build_opener(ns._SameHost()))       # a real start: reads the environment
    proxy["reply"] = (200, b"<rss/>")
    with contextlib.suppress(ns.Offline):
        ns.fetch(url)
    assert proxy["seen"] and proxy["seen"][0] == sent
    monkeypatch.setattr(ns, "_resolve", lambda host, port: ["10.0.0.7"])
    proxy["seen"].clear()
    with pytest.raises(ns.Offline, match="private address"):
        ns.fetch(url)
    assert proxy["seen"] == []
