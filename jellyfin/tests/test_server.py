"""Jellyfin media's own server (jellyfin/server.py): the address check, the key handling, every
tool shape against a fake Jellyfin, and every route's token.

The fake Jellyfin runs in a thread on 127.0.0.1 and records each call. Every server here listens on
a free port on 127.0.0.1 and is stopped by the test that started it; nothing touches the internet."""

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


@pytest.fixture
def names(monkeypatch):
    """The test's own home names answer with a home address. Every other name is looked up as usual."""
    real = jf.socket.getaddrinfo

    def lookup(host, port, *a, **k):
        if str(host) == "gone.lan":
            raise jf.socket.gaierror("no such name")
        if str(host).endswith((".lan", ".local")):
            return [(2, 1, 6, "", ("192.168.1.20", port))]
        return real(host, port, *a, **k)
    monkeypatch.setattr(jf.socket, "getaddrinfo", lookup)


@pytest.mark.parametrize("given, url, place", [
    ("192.168.1.20", "http://192.168.1.20:8096", "192.168.1.20:8096"),
    ("http://Media.LAN:9000/jellyfin/", "http://media.lan:9000/jellyfin", "media.lan:9000"),
    ("https://media.local", "https://media.local:8920", "media.local:8920"),
    ("http://[fe80::1]:8096", "http://[fe80::1]:8096", "[fe80::1]:8096"),
])
def test_the_ask_carries_the_token_and_the_exact_saved_place(base, data, core, proxy, names, given, url, place):
    text = tool(base, "set_address", address=given)
    assert text.startswith(f"Saved {url}.")
    one_ask(core, place)
    assert json.loads((data / "jellyfin.json").read_text(encoding="utf-8"))["address"] == url


def test_a_proxy_in_the_environment_is_not_used_for_the_ask(base, core, proxy):
    tool(base, "set_address", address="192.168.1.20:8096")
    one_ask(core, "192.168.1.20:8096")
    # the one thing the proxy saw is the probe set_address always made
    assert [s[1] for s in proxy["seen"]] == ["http://192.168.1.20:8096/System/Info/Public"]


@pytest.mark.parametrize("reply, says", [
    (ALLOWED, ["Allowed. The owner said Yes", "The owner allowed 192.168.1.20:8096", "works while the Bridge is open and this package is on", "unless a block list stops it"]),
    (WAITING, ["Waiting for the owner's Yes", "A Yes card for 192.168.1.20:8096 is waiting", "until her Yes"]),
    (STALE, ["can't be used right now", "Nothing reaches 192.168.1.20:8096 for now"]),
    (REFUSED, ["too many addresses this hour", "No card was raised"]),
])
def test_each_reply_gives_true_words_and_the_address_stays_saved(base, data, core, proxy, reply, says):
    core["reply"] = reply
    text = tool(base, "set_address", address="192.168.1.20")
    assert text.startswith("Saved http://192.168.1.20:8096.") and all(w in text for w in says), text
    assert (reply is not STALE) or "Yes card" not in text        # no card is said where the Core raised none
    assert (data / "jellyfin.json").exists()


@pytest.mark.parametrize("reply", [
    (200, b"not json"), (500, b"<html>oops</html>"), (401, {"error": "no"}), (200, {"state": "granted", "line": "yes"}),
    (500, {"state": "allowed", "line": "yes"}), (200, {"state": ["allowed"], "line": "yes"}), (202, {"state": "waiting", "line": ""}),
])
def test_a_reply_that_does_not_parse_says_saved_and_no_card(base, data, core, proxy, reply):
    core["reply"] = reply
    text = tool(base, "set_address", address="192.168.1.20")
    assert text.startswith("Saved") and "No Yes card could be raised" in text and "Bridge page" in text
    assert (data / "jellyfin.json").exists()


def test_the_cores_line_is_capped_and_never_says_the_token(base, core, proxy):
    core["reply"] = (202, {"state": "waiting", "line": f"Waiting {CORE_TOKEN}\n" + "x" * 5000})
    text = tool(base, "set_address", address="192.168.1.20")
    assert CORE_TOKEN not in text and "\n" not in text and "x" * 300 not in text


def test_no_api_no_token_or_the_core_down_says_saved_promptly(base, data, proxy, monkeypatch):
    for api, token in ((None, CORE_TOKEN), ("http://127.0.0.1:9", None), (f"http://127.0.0.1:{a_closed_port()}", CORE_TOKEN)):
        for name, value in (("PUFFPAI_API", api), ("PUFFPAI_TOKEN", token)):
            monkeypatch.delenv(name, raising=False) if value is None else monkeypatch.setenv(name, value)
        started = time.monotonic()
        text = tool(base, "set_address", address="192.168.1.20")
        assert time.monotonic() - started < 6 and text.startswith("Saved")
        assert "No Yes card could be raised" in text and CORE_TOKEN not in text
        assert (data / "jellyfin.json").exists()


def test_a_core_that_never_answers_does_not_hold_the_reply(base, proxy, monkeypatch):
    monkeypatch.setattr(jf, "ASK_TIMEOUT_S", 0.3)
    with socket.socket() as quiet:
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(1)
        monkeypatch.setenv("PUFFPAI_API", f"http://127.0.0.1:{quiet.getsockname()[1]}")
        monkeypatch.setenv("PUFFPAI_TOKEN", CORE_TOKEN)
        started = time.monotonic()
        text = tool(base, "set_address", address="192.168.1.20")
    assert time.monotonic() - started < 3 and "No Yes card could be raised" in text


def test_a_name_that_does_not_resolve_yet_still_asks(base, core, proxy, names):
    text = tool(base, "set_address", address="http://gone.lan:8096")
    assert "does not resolve" in text and "A Yes card for gone.lan:8096 is waiting" in text
    one_ask(core, "gone.lan:8096")
    assert proxy["seen"] == []


def test_an_address_on_this_computer_asks_nothing(base, core, fake):
    text = tool(base, "set_address", address=fake.url)
    assert "Test Jellyfin" in text and "needs no Bridge step" in text and core["asks"] == []


def test_the_manifest_declares_lan():
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert manifest["needs"]["addresses"] == "lan" and manifest["needs"]["hosts"] == []


@pytest.mark.parametrize("saved, sent", [
    ("http://media.lan:8096", ("GET", "http://media.lan:8096/Sessions", "media.lan:8096")),
    ("http://192.168.1.20:8096", ("GET", "http://192.168.1.20:8096/Sessions", "192.168.1.20:8096")),
    ("https://media.lan:8920", ("CONNECT", "media.lan:8920", None)),
])
def test_the_proxy_is_asked_for_the_name_as_saved(data, proxy, names, saved, sent):
    """A grant for a name matches the name only, so the proxy must see the name, unchanged, and never its answer."""
    proxy["reply"] = (200, b"[]")
    store = jf.Store(data)
    store.save(saved)
    with contextlib.suppress(jf.Plain):
        jf.Jellyfin(store)._call("/Sessions")
    assert proxy["seen"] and proxy["seen"][0] == sent


@contextlib.contextmanager
def a_dripping_core(monkeypatch, head_first):
    """A Core that sends one byte at a time, each in good time, for 4 seconds: of its head, or of a long body."""
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        with contextlib.suppress(OSError):
            c, _ = listener.accept()
            with c:
                c.recv(65536)
                head = b"HTTP/1.1 200 OK\r\nContent-Length: 9000\r\n\r\n"
                if head_first:
                    c.sendall(head)
                for byte in (b"x" * 40 if head_first else head + b"x" * 40):
                    if stop.wait(0.1):
                        return
                    c.sendall(bytes([byte]))
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv("PUFFPAI_API", f"http://127.0.0.1:{listener.getsockname()[1]}")
    monkeypatch.setenv("PUFFPAI_TOKEN", CORE_TOKEN)
    try:
        yield
    finally:
        stop.set()
        listener.close()
        thread.join(5)


@pytest.mark.parametrize("head_first", [False, True])
def test_a_slow_drip_does_not_hold_the_reply(base, data, proxy, monkeypatch, head_first):
    monkeypatch.setattr(jf, "ASK_TIMEOUT_S", 1)
    with a_dripping_core(monkeypatch, head_first):
        started = time.monotonic()
        text = tool(base, "set_address", address="192.168.1.20")
    assert time.monotonic() - started < 2.5 and "No Yes card could be raised" in text
    assert (data / "jellyfin.json").exists()


@pytest.mark.parametrize("given", ["http://192.168.1.20:443", "https://192.168.1.20:80", "http://media.lan:443", "https://media.lan:80"])
def test_a_port_that_can_not_work_with_its_scheme_is_refused_before_saving(base, data, core, proxy, names, given):
    text = tool_error(base, "set_address", address=given)
    assert "Port 443 goes with https and port 80 with http" in text and "Nothing was saved" in text
    assert not (data / "jellyfin.json").exists() and core["asks"] == [] and proxy["seen"] == []


@pytest.mark.parametrize("given", ["http://127.0.0.1:443", "https://127.0.0.1:80"])
def test_those_ports_on_this_computer_are_fine(base, data, core, given):
    text = tool(base, "set_address", address=given)
    assert "needs no Bridge step" in text and (data / "jellyfin.json").exists() and core["asks"] == []


def test_the_two_pairings_that_do_work_still_ask(base, core, proxy, names):
    tool(base, "set_address", address="https://media.lan:443")
    tool(base, "set_address", address="http://media.lan:80")
    assert [a["body"]["address"] for a in core["asks"]] == ["media.lan:443", "media.lan:80"]


class V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


@pytest.mark.parametrize("bind, where", [("127.0.0.2", "127.0.0.2"), ("::1", "[::1]"), ("127.0.0.1", "127.0.0.1")])
def test_every_loopback_address_goes_direct_past_a_proxy(base, proxy, bind, where):
    state = Fake()
    with running((V6Server if ":" in bind else ThreadingHTTPServer)((bind, 0), fake_handler(state))) as url:
        port = url.rsplit(":", 1)[1]
        text = tool(base, "set_address", address=f"http://{where}:{port}")
    assert "Test Jellyfin" in text and "needs no Bridge step" in text
    assert proxy["seen"] == [] and state.calls


@pytest.mark.parametrize("given", ["http://[::ffff:127.0.0.1]:7000", "http://[::ffff:192.168.1.20]:8096"])
def test_an_address_written_as_mapped_is_refused_plainly(base, data, core, given):
    assert "write it plainly" in tool_error(base, "set_address", address=given)
    assert not (data / "jellyfin.json").exists() and core["asks"] == []


def test_a_home_address_never_takes_the_direct_path(data, proxy, names):
    proxy["reply"] = (200, b"[]")
    store = jf.Store(data)
    for saved in ("http://192.168.1.20:8096", "http://[fd00::1]:8096", "http://media.lan:8096"):
        store.save(saved)
        proxy["seen"].clear()
        jf.Jellyfin(store)._call("/Sessions")
        assert proxy["seen"][0][1] == saved + "/Sessions"


@pytest.mark.parametrize("given, place", [("http://[fd00::1]:8096", "[fd00::1]:8096"), ("http://[fc00::20]:8096", "[fc00::20]:8096"),
                                          ("[fdff:ffff::1]", "[fdff:ffff::1]:8096")])
def test_an_ipv6_home_address_is_taken_and_asked_for(base, core, proxy, given, place):
    assert tool(base, "set_address", address=given).startswith("Saved")
    one_ask(core, place)


@pytest.mark.parametrize("given", ["http://[fbff::1]:8096", "http://[fe00::1]:8096", "http://[fec0::1]:8096",
                                   "http://[2001:db8::1]:8096", "http://100.64.0.1:8096", "http://8.8.8.8:8096"])
def test_nothing_else_widens(base, data, core, given):
    tool_error(base, "set_address", address=given)
    assert not (data / "jellyfin.json").exists() and core["asks"] == []


@pytest.mark.parametrize("answers, ok", [
    (["fd00::20", "192.168.1.20"], True), (["fd00::20"], True), (["fe80::1%12", "fd00::20", "192.168.1.20"], True),
    (["fd00::20", "93.184.216.34"], False), (["2606:4700:4700::1111", "fd00::20"], False), (["fec0::1"], False),
])
def test_a_home_name_may_answer_with_ipv6_home_addresses_but_never_a_public_one(base, data, core, proxy, monkeypatch, answers, ok):
    real = jf.socket.getaddrinfo
    monkeypatch.setattr(jf.socket, "getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", (x, port)) for x in answers]
                        if host == "media.lan" else real(host, port, *a, **k))
    err, text = run(base, "set_address", address="http://media.lan:8096")
    assert err is (not ok) and (data / "jellyfin.json").exists() is ok
    assert ("points outside your home network" in text) is (not ok)
