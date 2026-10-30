"""News headlines' own server: every tool, the token, bad input, and the fetch guards. Outside
calls are replaced; these tests never touch the internet. Each server listens on a free port on
127.0.0.1 and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
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
    assert "allow paper.example.org for News headlines on the Bridge page" in out
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
