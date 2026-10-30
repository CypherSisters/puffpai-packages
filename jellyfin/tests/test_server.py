"""Jellyfin media's own server (jellyfin/server.py): the address check, the key handling, every
tool shape against a fake Jellyfin, and every route's token.

The fake Jellyfin runs in a thread on 127.0.0.1 and records each call. Every server here listens on
a free port on 127.0.0.1 and is stopped by the test that started it; nothing touches the internet."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20
KEY = "0123456789abcdef0123456789abcdef"       # a made-up key, never a real one
USER = "a" * 32
SONG, ALBUM, MOVIE, PLAYLIST = "1" * 32, "2" * 32, "3" * 32, "4" * 32
TV, PHONE = "sess-tv-1", "sess-phone-2"

_spec = importlib.util.spec_from_file_location("jellyfin_server", PKG / "server.py")
jf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jf)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


# ------------------------------------------------------------ the fake Jellyfin

CATALOG = [
    {"Id": SONG, "Name": "Dreams", "Type": "Audio", "AlbumArtist": "Fleetwood Mac", "ProductionYear": 1977},
    {"Id": ALBUM, "Name": "Rumours", "Type": "MusicAlbum", "AlbumArtist": "Fleetwood Mac", "ProductionYear": 1977},
    {"Id": MOVIE, "Name": "Arrival", "Type": "Movie", "ProductionYear": 2016},
]


class Fake:
    def __init__(self):
        self.calls = []
        self.redirect = False
        self.catalog = list(CATALOG)


def fake_handler(state: Fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self, code, obj=None, headers=None):
            raw = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _route(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            state.calls.append({"method": self.command, "path": u.path, "query": q,
                                "token": self.headers.get("X-Emby-Token"), "body": body})
            if state.redirect:
                return self._answer(302, headers={"Location": "http://127.0.0.2:9/elsewhere"})
            if u.path == "/System/Info/Public":
                return self._answer(200, {"ServerName": "Test Jellyfin", "Version": "10.9.0"})
            if self.headers.get("X-Emby-Token") != KEY:
                return self._answer(401)
            if u.path == "/Items" and q.get("IncludeItemTypes") == "Playlist":
                return self._answer(200, {"Items": [{"Id": PLAYLIST, "Name": "Road Trip", "Type": "Playlist",
                                                     "ChildCount": 2}]})
            if u.path == "/Items":
                types = q.get("IncludeItemTypes", "").split(",")
                term = q.get("SearchTerm", "").lower()
                hits = [i for i in state.catalog if i["Type"] in types
                        and (term in i["Name"].lower() or term in i.get("AlbumArtist", "").lower())]
                return self._answer(200, {"Items": hits[: int(q.get("Limit", 20))]})
            if u.path == "/Sessions":
                return self._answer(200, [
                    {"Id": TV, "Client": "Jellyfin Android TV", "DeviceName": "Living Room TV", "UserName": "sam",
                     "SupportsRemoteControl": True, "NowPlayingItem": CATALOG[0],
                     "PlayState": {"IsPaused": False, "PositionTicks": 950_000_000}},
                    {"Id": PHONE, "Client": "Jellyfin Mobile", "DeviceName": "Kitchen Phone", "UserName": "ali",
                     "SupportsRemoteControl": True},
                    {"Id": "api-1", "Client": "Some API", "DeviceName": "Server", "SupportsRemoteControl": False},
                ])
            if u.path == "/Users":
                return self._answer(200, [{"Id": "b" * 32, "Name": "kid", "Policy": {"IsAdministrator": False}},
                                          {"Id": USER, "Name": "owner", "Policy": {"IsAdministrator": True}}])
            if u.path == "/Playlists" and self.command == "POST":
                return self._answer(200, {"Id": "5" * 32})
            if self.command == "POST" and (u.path.startswith("/Sessions/") or u.path.startswith("/Playlists/")):
                return self._answer(204)
            return self._answer(404)

        do_GET = do_POST = _route

    return H


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


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("JELLYFIN_API_KEY", KEY)


@pytest.fixture
def fake():
    state = Fake()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fake_handler(state))
    srv.daemon_threads = True
    with running(srv) as url:
        state.url = url
        yield state


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data" / "jellyfin"


@pytest.fixture
def base(data):
    with running(jf.make_server(0, data, TOKEN)) as url:
        yield url


REPLIES = []


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


def run(base, name, /, **args):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": args}})
    assert code == 200, out
    text = out["result"]["content"][0]["text"]
    REPLIES.append(text)
    return out["result"]["isError"], text


def tool(base, name, /, **args):
    err, text = run(base, name, **args)
    assert not err, text
    return text


def tool_error(base, name, /, **args):
    err, text = run(base, name, **args)
    assert err, text
    return text


def connected(base, fake):
    tool(base, "set_address", address=fake.url)
    fake.calls.clear()


# ------------------------------------------------------------ routes and the manifest

def test_every_route_needs_the_token_and_the_tools_match_the_manifest(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert names == set(manifest["abilities"]["tools"]) == set(manifest["abilities"]["examples"])
    assert set(manifest["abilities"]["changes"]) == set(jf.CHANGES) <= names
    assert manifest["needs"]["hosts"] == [] and manifest["needs"]["keys"][0]["name"] == "JELLYFIN_API_KEY"


# ------------------------------------------------------------ the address

@pytest.mark.parametrize("address", [
    "http://8.8.8.8:8096", "https://jellyfin.example.com", "http://user:pw@192.168.1.5:8096",
    "ftp://192.168.1.5", "http://localhost:8096", "http://100.64.0.1:8096", "http://172.32.0.1",
    "http://[::ffff:8.8.8.8]:8096", "http://192.168.1.5:8096/?x=1", "http://0.0.0.0:8096", "",
    "http://evil.lan:8096",
])
def test_a_public_or_odd_address_is_refused_and_nothing_is_saved(base, data, address, fake_dns):
    tool_error(base, "set_address", address=address)
    assert not (data / "jellyfin.json").exists()


@pytest.fixture
def fake_dns(monkeypatch):
    """evil.lan points at a public address; any other .lan or .local name does not resolve. Everything
    else (the test's own 127.0.0.1 connections) goes to the real lookup."""
    real = jf.socket.getaddrinfo

    def getaddrinfo(host, port, *a, **k):
        if host == "evil.lan":
            return [(2, 1, 6, "", ("93.184.216.34", port))]
        if str(host).endswith((".lan", ".local")):
            raise jf.socket.gaierror("no such name")
        return real(host, port, *a, **k)
    monkeypatch.setattr(jf.socket, "getaddrinfo", getaddrinfo)


def test_a_name_that_does_not_resolve_is_saved_with_a_note(base, data, fake_dns):
    text = tool(base, "set_address", address="http://nowhere.lan:8096")
    assert "does not resolve" in text and "nothing was sent" in text
    assert json.loads((data / "jellyfin.json").read_text(encoding="utf-8"))["address"] == "http://nowhere.lan:8096"


def test_set_address_saves_only_the_address_and_probes_without_the_key(base, data, fake):
    assert jf.check_address("192.168.1.20") == "http://192.168.1.20:8096"
    assert jf.check_address("https://media.lan/jellyfin/") == "https://media.lan:8920/jellyfin"
    text = tool(base, "set_address", address=fake.url)
    assert "Test Jellyfin" in text and fake.url in text
    saved = json.loads((data / "jellyfin.json").read_text(encoding="utf-8"))
    assert set(saved) == {"address", "last_updated"} and saved["address"] == fake.url
    assert KEY not in (data / "jellyfin.json").read_text(encoding="utf-8")
    assert fake.calls == [{"method": "GET", "path": "/System/Info/Public", "query": {}, "token": None, "body": None}]


# ------------------------------------------------------------ the tools

def test_search_media_sends_the_key_only_to_the_saved_address(base, fake):
    connected(base, fake)
    text = tool(base, "search_media", words="fleetwood")
    assert "song: Dreams by Fleetwood Mac (1977) [id " + SONG in text and "album: Rumours" in text
    assert "movie: Arrival (2016) [id " + MOVIE in tool(base, "search_media", words="arrival", kind="movies")
    assert all(c["token"] == KEY and c["path"] == "/Items" for c in fake.calls)
    assert fake.calls[1]["query"]["IncludeItemTypes"] == "Movie" and fake.calls[0]["query"]["Limit"] == "20"


def test_list_players_and_now_playing(base, fake):
    connected(base, fake)
    players = tool(base, "list_players")
    assert "Living Room TV (Jellyfin Android TV, sam): song: Dreams" in players and f"[player id {TV}]" in players
    assert "Kitchen Phone" in players and "Server" not in players
    assert tool(base, "now_playing") == "Living Room TV (sam): song: Dreams by Fleetwood Mac (1977) (playing, 1:35 in)"


def test_play_by_words_on_a_named_player_and_asks_which_when_unnamed(base, fake):
    connected(base, fake)
    assert tool(base, "play", what="rumours", player="kitchen") == \
        "Playing album: Rumours by Fleetwood Mac (1977) on Kitchen Phone."
    sent = fake.calls[-1]
    assert sent["method"] == "POST" and sent["path"] == f"/Sessions/{PHONE}/Playing"
    assert sent["query"] == {"ItemIds": ALBUM, "PlayCommand": "PlayNow"} and sent["token"] == KEY
    fake.calls.clear()
    assert "Which player?" in tool_error(base, "play", what=MOVIE)
    assert not any(c["method"] == "POST" for c in fake.calls)


def test_control_pause_and_volume(base, fake):
    connected(base, fake)
    assert tool(base, "control", action="resume", player="living room") == "Done: unpause on Living Room TV."
    assert fake.calls[-1]["path"] == f"/Sessions/{TV}/Playing/Unpause"
    assert tool(base, "control", action="pause") == "Done: pause on Living Room TV."     # the one that is playing
    assert fake.calls[-1]["path"] == f"/Sessions/{TV}/Playing/Pause"
    assert tool(base, "control", action="volume", volume=30, player="Kitchen Phone") == "Volume set to 30 on Kitchen Phone."
    assert fake.calls[-1]["path"] == f"/Sessions/{PHONE}/Command"
    assert fake.calls[-1]["body"] == {"Name": "SetVolume", "Arguments": {"Volume": "30"}}


def test_playlists_create_and_add(base, fake):
    connected(base, fake)
    assert f"Road Trip (2 items) [id {PLAYLIST}]" in tool(base, "playlists")
    text = tool(base, "create_playlist", name="Summer", items=["dreams", "no such song"])
    assert "with 1 song" in text and "Not found: no such song" in text
    made = [c for c in fake.calls if c["path"] == "/Playlists"][-1]
    assert made["body"] == {"Name": "Summer", "UserId": USER, "Ids": [SONG], "MediaType": "Audio"}
    assert tool(base, "add_to_playlist", playlist="road trip", items=["dreams"]).startswith("Added 1 song to 'Road Trip'.")
    added = fake.calls[-1]
    assert added["path"] == f"/Playlists/{PLAYLIST}/Items" and added["query"] == {"Ids": SONG, "UserId": USER}


# ------------------------------------------------------------ bad input, the key, redirects

def test_bad_input_is_refused_before_anything_is_sent(base, fake):
    connected(base, fake)
    assert "volume" in tool_error(base, "control", action="volume", volume=150)
    assert "action is" in tool_error(base, "control", action="explode")
    assert "kind is" in tool_error(base, "search_media", words="x", kind="podcasts")
    assert "what is" in tool_error(base, "play", what="  ")
    assert "1 to 50" in tool_error(base, "create_playlist", name="Big", items=[f"song {i}" for i in range(51)])
    assert fake.calls == []


def test_the_key_is_never_in_a_reply(base, fake, monkeypatch):
    assert "address is not set" in tool_error(base, "search_media", words="dreams")
    connected(base, fake)
    fake.catalog.append({"Id": "6" * 32, "Name": f"echo {KEY} echo", "Type": "Audio"})
    assert "echo [hidden] echo" in tool(base, "search_media", words="echo")
    monkeypatch.setenv("JELLYFIN_API_KEY", "f" * 32)
    assert "refused the key" in tool_error(base, "list_players")
    monkeypatch.setenv("JELLYFIN_API_KEY", "bad key with spaces")
    assert "does not look like" in tool_error(base, "list_players")
    monkeypatch.delenv("JELLYFIN_API_KEY")
    assert "no Jellyfin key yet" in tool_error(base, "list_players")
    assert REPLIES and not any(KEY in r or "f" * 32 in r for r in REPLIES)


def test_a_redirect_is_not_followed(base, fake):
    connected(base, fake)
    fake.redirect = True
    assert "redirect" in tool_error(base, "now_playing")
    assert len(fake.calls) == 1
