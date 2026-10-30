"""World Radio's own server (radio/server.py): the tools, the page's routes and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it.
Station search is monkeypatched; nothing here touches the internet."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "r" * 20

_spec = importlib.util.spec_from_file_location("radio_server", PKG / "server.py")
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

ROWS = [
    {"stationuuid": "u1", "name": "KEXP 90.3", "url_resolved": "https://kexp.example/stream", "country": "United States",
     "codec": "MP3", "bitrate": 128, "tags": "indie", "hls": 0},
    {"stationuuid": "u2", "name": "Jazz FM", "url_resolved": "http://jazz.example/live", "country": "United Kingdom",
     "codec": "AAC", "bitrate": "64", "tags": "jazz", "hls": 0},
    {"stationuuid": "u3", "name": "HLS only", "url_resolved": "https://x.example/a.m3u8", "hls": 1},
    {"stationuuid": "u4", "name": "No stream", "url_resolved": ""},
]


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def fake(path, params):
        calls.append((path, dict(params)))
        name = params.get("name", "").lower()
        return [r for r in ROWS if name in r["name"].lower()] if name else list(ROWS)

    monkeypatch.setattr(rd, "fetch_json", fake)
    return calls


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
def base(tmp_path, asked):
    with running(rd.make_server(0, tmp_path / "data" / "radio", TOKEN)) as url:
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
            return r.status, (json.loads(text) if "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def tool(base, name, /, **args):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": args}})
    assert code == 200
    return out["result"]["content"][0]["text"], out["result"]["isError"]


def test_tools_listed_and_changes_match_the_manifest(base):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert names == set(manifest["abilities"]["tools"])
    assert set(manifest["abilities"]["changes"]) == set(rd.CHANGES) <= names


def test_find_stations_drops_hls_and_empty_and_numbers(base, asked):
    text, err = tool(base, "find_stations", tag="Jazz", country="gb")
    assert not err
    assert "1. KEXP 90.3 (United States, MP3 128k)" in text and "2. Jazz FM" in text
    assert "HLS only" not in text and "No stream" not in text
    params = asked[-1][1]
    assert asked[-1][0] == "/json/stations/search" and params["tag"] == "jazz" and params["countrycode"] == "GB"


def test_play_by_number_then_stop_and_now_playing(base):
    tool(base, "find_stations")
    text, err = tool(base, "play_station", station="2")
    assert not err and "Now playing: Jazz FM" in text and "not open now" in text
    code, doc = call(base + "/data")
    assert doc["now_playing"]["url"] == "http://jazz.example/live" and doc["play_id"] == 1
    text, _ = tool(base, "now_playing")
    assert "Jazz FM" in text and "page is open" in text          # the /data poll above counts as the page
    assert tool(base, "stop")[0] == "Stopped Jazz FM."
    assert call(base + "/data")[1]["now_playing"] is None


def test_play_by_name_searches(base, asked):
    text, err = tool(base, "play_station", station="kexp")
    assert not err and "KEXP 90.3" in text
    assert asked[-1][1]["name"] == "kexp"


def test_favorites_add_list_remove(base):
    tool(base, "play_station", station="kexp")
    assert tool(base, "add_favorite")[0] == "Saved KEXP 90.3 to the radio favorites."
    assert "already" in tool(base, "add_favorite", station="kexp")[0]
    assert "1. KEXP 90.3" in tool(base, "favorites")[0]
    assert tool(base, "remove_favorite", station="kexp")[0] == "Removed KEXP 90.3 from the radio favorites."
    assert tool(base, "favorites")[0] == "No favorite radio stations yet."


def test_page_routes(base):
    code, out = call(base + "/api/search", "POST", {"name": "jazz"})
    assert code == 200 and [s["name"] for s in out["stations"]] == ["Jazz FM"]
    code, doc = call(base + "/api/play", "POST", {"station": out["stations"][0]})
    assert code == 200 and doc["now_playing"]["name"] == "Jazz FM"
    code, doc = call(base + "/api/add_favorite", "POST", {"station": out["stations"][0]})
    assert len(doc["favorites"]) == 1
    code, doc = call(base + "/api/remove_favorite", "POST", {"url": "http://jazz.example/live"})
    assert doc["favorites"] == []
    code, page = call(base + "/")
    assert code == 200 and "World Radio" in page


def test_bad_input_is_a_plain_error(base):
    text, err = tool(base, "play_station", station="7")
    assert err and "no search" in text
    code, out = call(base + "/api/play", "POST", {"station": {"name": "x", "url": "javascript:alert(1)"}})
    assert code == 400 and "http" in out["error"]
    text, err = tool(base, "find_stations", name=5)
    assert err and "must be words" in text


def test_offline_is_plain(base, monkeypatch):
    def down(path, params):
        raise rd.Offline("World Radio can't reach the station directory (radio-browser.info) right now (URLError).")
    monkeypatch.setattr(rd, "fetch_json", down)
    text, err = tool(base, "find_stations", tag="rock")
    assert err and "can't reach" in text


def test_every_route_needs_the_token(base):
    for path, method in (("/", "GET"), ("/health", "GET"), ("/data", "GET"), ("/mcp", "POST"), ("/api/stop", "POST")):
        assert call(base + path, method, {} if method == "POST" else None, token=None)[0] == 401
        assert call(base + path, method, {} if method == "POST" else None, token="wrong")[0] == 401


def test_redirect_to_another_host_is_refused():
    handler = rd._SameHost()
    req = urllib.request.Request("https://de1.api.radio-browser.info/json/stations/search")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/") is None
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://all.api.radio-browser.info/x") is not None
