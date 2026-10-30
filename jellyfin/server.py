# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/media_bridge.py (rewritten-from)
"""Jellyfin media: search the owner's Jellyfin library and play on its players, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

HomeBot played on the box's own speakers. This package asks the owner's Jellyfin server to
play on one of the players connected to it (a TV app, a phone, a browser tab), through
Jellyfin's ``/Sessions`` API, with the owner's API key.

The address is the one thing kept: ``jellyfin.json`` in PUFFPAI_DATA, written whole (temp
file, then rename). Only http(s) addresses on this computer or the home network are taken
(loopback, 10/8, 172.16/12, 192.168/16, 169.254/16, ::1, fe80::/10, or a name ending in
.local or .lan); a name is looked up before each call and refused if it points anywhere
else. The key (JELLYFIN_API_KEY) comes from the environment only, goes only to the saved
address as ``X-Emby-Token``, and is never written, logged or put in an answer. Calls time
out after 10 seconds, never follow a redirect, and read at most 2 MB.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

NAME = "jellyfin"
VERSION = "1.0.0"
DOC = "jellyfin.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_ANSWER = 2 * 1024 * 1024          # bytes read from Jellyfin, at most
TIMEOUT = 10                          # seconds per Jellyfin call
KEY_ENV = "JELLYFIN_API_KEY"
HTTP_PORT, HTTPS_PORT = 8096, 8920    # Jellyfin's own defaults
EXAMPLE = "http://192.168.1.20:8096"

ALLOWED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fe80::/10"))
NAME_RE = re.compile(r"\A(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:local|lan)\Z")
NETLOC_RE = re.compile(r"\A[A-Za-z0-9.\-:\[\]%]+\Z")
PATH_RE = re.compile(r"\A(?:/[A-Za-z0-9_-]+)+\Z")
#: Jellyfin ids end up as path segments: allow-list them (HomeBot media_bridge.MEDIA_ID_RE).
ID_RE = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")
#: A whole Jellyfin id (32 hex, or the dashed GUID form): taken as an id rather than words.
FULL_ID_RE = re.compile(r"\A(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\Z")
KEY_RE = re.compile(r"\A[A-Za-z0-9._~+/=-]{8,200}\Z")

KINDS = {"any": "Audio,MusicAlbum,MusicArtist,Movie,Series", "music": "Audio", "songs": "Audio", "song": "Audio",
         "albums": "MusicAlbum", "album": "MusicAlbum", "artists": "MusicArtist", "artist": "MusicArtist",
         "movies": "Movie", "movie": "Movie", "shows": "Series", "show": "Series", "tv": "Series",
         "playlists": "Playlist", "playlist": "Playlist"}
PLAY_TYPES = "Audio,MusicAlbum,Playlist,Movie,Episode,Series"
TYPE_WORDS = {"Audio": "song", "MusicAlbum": "album", "MusicArtist": "artist", "Movie": "movie",
              "Series": "show", "Episode": "episode", "Playlist": "playlist"}
COMMANDS = {"pause": "Pause", "unpause": "Unpause", "stop": "Stop", "next": "NextTrack", "previous": "PreviousTrack"}
ALIASES = {"resume": "unpause", "play": "unpause", "continue": "unpause", "skip": "next", "next track": "next",
           "back": "previous", "prev": "previous", "previous track": "previous", "set volume": "volume"}


def _now() -> str:
    return datetime.now().isoformat()


class Plain(Exception):
    """A plain answer for the owner that the tool could not do what was asked."""


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a number from {lo:g} to {hi:g}")
    if whole:
        if x != int(x):
            raise ValueError(f"{name} is a whole number")
        return int(x)
    return x


def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _list(value, name: str, most: int) -> list[str]:
    """A list of 1 to *most* short phrases or ids."""
    if isinstance(value, str):
        value = [v for v in value.split(";")]
    if not isinstance(value, list):
        raise ValueError(f"{name} is a list of song names or item ids")
    out = [_text(v, f"each of {name}", 200) for v in value]
    out = [v for v in out if v]
    if not out or len(out) > most:
        raise ValueError(f"{name} holds 1 to {most} song names or item ids")
    return out


def _clean(value, most: int = 120) -> str:
    """Text from Jellyfin for an answer: printable, one line, short."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return text[:most].strip()


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this package takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


# ---------------------------------------------------------------- the address

def _ip_allowed(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in ALLOWED_NETS)


def host_allowed(host: str) -> bool:
    """Loopback, RFC1918 or link-local, or a name ending in .local or .lan."""
    try:
        return _ip_allowed(ipaddress.ip_address(host))
    except ValueError:
        return len(host) <= 253 and NAME_RE.match(host) is not None


def check_address(value) -> str:
    """The Jellyfin base URL, normalised, or a plain ValueError. Only this computer or the home network."""
    raw = _text(value, "address", 200)
    if not raw:
        raise ValueError(f"address is your Jellyfin address, like {EXAMPLE}")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        u = urlsplit(raw)
        host, port = u.hostname or "", u.port
    except ValueError:
        raise ValueError(f"that is not an address. It looks like {EXAMPLE}") from None
    if u.scheme not in ("http", "https"):
        raise ValueError("the address starts with http:// or https://")
    if "@" in u.netloc or not NETLOC_RE.match(u.netloc):
        raise ValueError(f"the address has no name, password or odd characters in it. It looks like {EXAMPLE}")
    if u.query or u.fragment:
        raise ValueError("the address has no ? or # part")
    path = u.path.rstrip("/")
    if path and not PATH_RE.match(path):
        raise ValueError("the part after the port may only hold letters, digits, - and _")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("the port is a number from 1 to 65535")
    if not host or not host_allowed(host):
        raise ValueError(f"Jellyfin must be on this computer or your home network: 127.x, 10.x, 172.16.x to "
                         f"172.31.x, 192.168.x, 169.254.x, or a name ending in .local or .lan. "
                         f"{_clean(host, 60) or 'That'} is not one of those.")
    port = port or (HTTP_PORT if u.scheme == "http" else HTTPS_PORT)
    netloc = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{netloc}:{port}{path}"


def _name_answers(base: str) -> set[str] | None:
    """The addresses a name in *base* points at now: an empty set for an IP address, None when the
    name does not resolve on this computer."""
    u = urlsplit(base)
    host = u.hostname or ""
    try:
        ipaddress.ip_address(host)
        return set()
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, u.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    return {i[4][0].split("%")[0] for i in infos}


def _points_home(addrs: set[str]) -> bool:
    try:
        return bool(addrs) and all(_ip_allowed(ipaddress.ip_address(a)) for a in addrs)
    except ValueError:
        return False


def _host_resolves_home(base: str) -> None:
    """Before each call: a name must still point only at this computer or the home network."""
    host = urlsplit(base).hostname or ""
    addrs = _name_answers(base)
    if addrs is None:
        raise Plain(f"{host} does not resolve on this computer. Check the name, or set the address by number.")
    if addrs and not _points_home(addrs):
        raise Plain(f"{host} now points outside your home network, so nothing was sent. Check the name.")


def _key() -> str:
    """The owner's key, from the environment only. Never echoed."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise Plain(f"There is no Jellyfin key yet. Add {KEY_ENV} in PuffPAI: in Jellyfin open Dashboard, "
                    "API Keys, and add one.")
    if not KEY_RE.match(key):
        raise Plain(f"{KEY_ENV} does not look like a Jellyfin key. Paste it again in PuffPAI.")
    return key


def _scrub(text: str) -> str:
    """Belt and braces: the key never leaves in an answer, even if Jellyfin echoes it."""
    key = os.environ.get(KEY_ENV, "").strip()
    return text.replace(key, "[hidden]") if len(key) >= 4 else text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the key goes to the saved address and nowhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


def _opener():
    # Proxies from the environment only (the Core gives HTTP_PROXY / NO_PROXY for the Bridge),
    # never the Windows registry.
    return urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()),
                                       NoRedirect())


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


class Store:
    """One JSON document, ``{address, last_updated}``. Nothing else is kept."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {"address": "", "last_updated": ""}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{DOC} will not read ({type(exc).__name__}). Jellyfin media will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("address", ""), str):
                raise Unreadable(f"{DOC} is not a Jellyfin media settings file. Jellyfin media will not write over it.")
            return {"address": data.get("address", ""), "last_updated": str(data.get("last_updated", ""))}

    def save(self, address: str) -> None:
        with self.lock:
            self.load()                          # an unreadable file is never written over
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".jellyfin-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"address": address, "last_updated": _now()}, f, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


# ------------------------------------------------------------- the logic

def _items(data) -> list[dict]:
    items = data.get("Items") if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _id(raw, kind: str = "item id") -> str:
    """An id from Jellyfin or the brain, safe as a path segment, or Plain."""
    if not isinstance(raw, str) or not ID_RE.match(raw):
        raise Plain(f"That {kind} is not a Jellyfin id.")
    return raw


def _item_line(it: dict, with_id: bool = True) -> str:
    kind = TYPE_WORDS.get(str(it.get("Type")), _clean(it.get("Type"), 20) or "item")
    name = _clean(it.get("Name")) or "(no name)"
    artists = it.get("Artists") if isinstance(it.get("Artists"), list) else []
    artist = _clean(it.get("AlbumArtist") or ", ".join(str(a) for a in artists[:3]), 80)
    year = it.get("ProductionYear")
    detail = (f" by {artist}" if artist and kind in ("song", "album") else "")
    if isinstance(year, int) and not isinstance(year, bool) and 1000 < year < 3000:
        detail += f" ({year})"
    iid = it.get("Id")
    tail = f" [id {iid}]" if with_id and isinstance(iid, str) and ID_RE.match(iid) else ""
    return f"{kind}: {name}{detail}{tail}"


def _player_fields(s: dict) -> tuple[str, str, str]:
    return str(s.get("Id", "")).lower(), _clean(s.get("DeviceName")).lower(), _clean(s.get("Client")).lower()


def _player_name(s: dict) -> str:
    return _clean(s.get("DeviceName"), 60) or _clean(s.get("Client"), 60) or "a player"


class Jellyfin:
    def __init__(self, store: Store) -> None:
        self.store = store

    # the one way out
    def _base(self) -> str:
        saved = self.store.load()["address"]
        if not saved:
            raise Plain(f"Jellyfin's address is not set yet. Tell me it, like: set my Jellyfin address to {EXAMPLE}")
        try:
            return check_address(saved)          # a hand-edited file gets the same check
        except ValueError:
            raise Plain("The saved Jellyfin address is not on your home network. Set it again.") from None

    def _call(self, path: str, method: str = "GET", params: dict | None = None, body=None,
              base: str | None = None, key: bool = True):
        base = base or self._base()
        _host_resolves_home(base)
        headers = {"Accept": "application/json"}
        if key:
            headers["X-Emby-Token"] = _key()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        url = base + path + (("?" + urlencode(params)) if params else "")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _opener().open(req, timeout=TIMEOUT) as r:
                raw = r.read(MAX_ANSWER + 1)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - nothing more to read
                pass
            if 300 <= exc.code < 400:
                raise Plain(f"Jellyfin at {base} answered with a redirect, which is not followed. "
                            "Set the address Jellyfin itself answers on.") from None
            if exc.code in (401, 403):
                raise Plain(f"Jellyfin at {base} refused the key (HTTP {exc.code}). Check {KEY_ENV}, "
                            "and that the Bridge allows that address.") from None
            if exc.code == 404:
                raise Plain(f"Jellyfin at {base} has nothing there (HTTP 404). Is that the right id?") from None
            raise Plain(f"Jellyfin at {base} answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Plain(f"Jellyfin at {base} did not answer ({type(exc).__name__}). Check it is running "
                        "and that the Bridge allows that address.") from None
        if len(raw) > MAX_ANSWER:
            raise Plain("Jellyfin's answer was bigger than 2 MB, so it was not read.")
        if not raw.strip():
            return {}
        try:
            return _loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Plain(f"Jellyfin at {base} answered with something that is not JSON.") from None

    def _search(self, words: str, types: str, limit: int = 20) -> list[dict]:
        return _items(self._call("/Items", params={"SearchTerm": words, "IncludeItemTypes": types,
                                                    "Recursive": "true", "Limit": limit,
                                                    "Fields": "ProductionYear"}))

    def _sessions(self) -> list[dict]:
        data = self._call("/Sessions")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def _player(self, player: str, prefer_playing: bool = False) -> dict:
        ctl = [s for s in self._sessions() if s.get("SupportsRemoteControl") and ID_RE.match(str(s.get("Id", "")))]
        if not ctl:
            raise Plain("No Jellyfin player can be controlled right now. Open a Jellyfin app on a TV, phone "
                        "or browser first, then ask again.")
        names = "; ".join(f"{_player_name(s)} ({_clean(s.get('Client'), 40)})" for s in ctl)
        if player:
            p = player.lower()
            hits = ([s for s in ctl if p in _player_fields(s)]
                    or [s for s in ctl if any(p in f for f in _player_fields(s)[1:])])
            if len(hits) == 1:
                return hits[0]
            if not hits:
                raise Plain(f"No player called {_clean(player, 60)!r}. Players: {names}.")
            raise Plain(f"More than one player matches {_clean(player, 60)!r}: {names}. Say which one.")
        if prefer_playing:
            busy = [s for s in ctl if s.get("NowPlayingItem")]
            if len(busy) == 1:
                return busy[0]
        if len(ctl) == 1:
            return ctl[0]
        raise Plain(f"Which player? {names}.")

    def _user(self) -> str:
        """Playlists belong to a Jellyfin user; an API key has none, so the first administrator (as HomeBot)."""
        users = self._call("/Users")
        users = [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []
        admins = [u for u in users if isinstance(u.get("Policy"), dict) and u["Policy"].get("IsAdministrator")]
        for u in admins + users:
            if isinstance(u.get("Id"), str) and ID_RE.match(u["Id"]):
                return u["Id"]
        raise Plain("Jellyfin has no user to keep playlists for.")

    def _track(self, phrase: str) -> tuple[str, str] | None:
        """(id, line) for an item id or the first song matching the words (HomeBot _media_find_track)."""
        if FULL_ID_RE.match(phrase):
            return phrase, f"item {phrase}"
        hits = [h for h in self._search(phrase, "Audio", 1) if ID_RE.match(str(h.get("Id", "")))]
        return (hits[0]["Id"], _item_line(hits[0], with_id=False)) if hits else None

    def _playlist(self, user: str, which: str) -> tuple[str, str]:
        if FULL_ID_RE.match(which):
            return which, which
        lists = self._lists(user)
        w = which.lower()
        hits = ([p for p in lists if _clean(p.get("Name")).lower() == w]
                or [p for p in lists if w in _clean(p.get("Name")).lower()])
        if len(hits) != 1:
            names = ", ".join(_clean(p.get("Name"), 60) for p in lists[:20]) or "none yet"
            raise Plain(f"{'No playlist' if not hits else 'More than one playlist'} matches {_clean(which, 60)!r}. "
                        f"Playlists: {names}.")
        return _id(hits[0].get("Id"), "playlist id"), _clean(hits[0].get("Name"))

    def _lists(self, user: str) -> list[dict]:
        return _items(self._call("/Items", params={"IncludeItemTypes": "Playlist", "Recursive": "true",
                                                    "UserId": user, "Fields": "ChildCount"}))

    # tools
    def set_address(self, address: str) -> str:
        base = check_address(address)
        host = urlsplit(base).hostname or ""
        addrs = _name_answers(base)
        if addrs and not _points_home(addrs):
            raise Plain(f"{host} points outside your home network, so it was not saved. "
                        "Use a name or address on your home network.")
        self.store.save(base)
        if addrs is None:
            return (f"Saved {base}, but {host} does not resolve on this computer yet, so nothing was sent. "
                    "Check the name, or set the address by number.")
        try:
            info = self._call("/System/Info/Public", base=base, key=False)
        except Plain as exc:
            return f"Saved {base}. It did not answer yet: {exc}"
        name = _clean(info.get("ServerName"), 60) if isinstance(info, dict) else ""
        ver = _clean(info.get("Version"), 20) if isinstance(info, dict) else ""
        return f"Saved {base}. Jellyfin answered there{f': {name}' if name else ''}{f', version {ver}' if ver else ''}."

    def search_media(self, words: str, kind: str = "any") -> str:
        words = _text(words, "words", 100)
        if not words:
            raise ValueError("words is what to look for, like a song, album, artist, movie or show")
        k = _text(kind, "kind", 20, "any").lower()
        if k not in KINDS:
            raise ValueError("kind is any, music, albums, artists, movies, shows or playlists")
        found = self._search(words, KINDS[k], 20)
        if not found:
            return f"Nothing in your Jellyfin library matches {_clean(words, 100)!r}."
        return f"{len(found)} found in Jellyfin:\n" + "\n".join(_item_line(i) for i in found[:20])

    def list_players(self) -> str:
        ctl = [s for s in self._sessions() if s.get("SupportsRemoteControl")]
        if not ctl:
            return "No Jellyfin player can be controlled right now. Open a Jellyfin app on a TV, phone or browser."
        lines = []
        for s in ctl:
            np = s.get("NowPlayingItem")
            now = _item_line(np, with_id=False) if isinstance(np, dict) else "nothing playing"
            user = _clean(s.get("UserName"), 40)
            sid = s.get("Id") if ID_RE.match(str(s.get("Id", ""))) else "?"
            lines.append(f"{_player_name(s)} ({_clean(s.get('Client'), 40)}{f', {user}' if user else ''}): "
                         f"{now} [player id {sid}]")
        return f"{len(lines)} Jellyfin player{'s' if len(lines) != 1 else ''}:\n" + "\n".join(lines)

    def play(self, what: str, player: str = "", kind: str = "any") -> str:
        what = _text(what, "what", 200)
        if not what:
            raise ValueError("what is a song, album, playlist, movie or show to play, or its item id")
        player = _text(player, "player", 100)
        k = _text(kind, "kind", 20, "any").lower()
        if k not in KINDS:
            raise ValueError("kind is any, music, albums, artists, movies, shows or playlists")
        if FULL_ID_RE.match(what):
            item_id, label = what, f"item {what}"
        else:
            hits = [h for h in self._search(what, PLAY_TYPES if k == "any" else KINDS[k], 1)
                    if ID_RE.match(str(h.get("Id", "")))]
            if not hits:
                return f"Nothing in your Jellyfin library matches {_clean(what, 100)!r}, so nothing was played."
            item_id, label = hits[0]["Id"], _item_line(hits[0], with_id=False)
        s = self._player(player)
        self._call(f"/Sessions/{quote(_id(s['Id'], 'player id'), safe='')}/Playing", "POST",
                   params={"ItemIds": item_id, "PlayCommand": "PlayNow"})
        return f"Playing {label} on {_player_name(s)}."

    def control(self, action: str = "", player: str = "", volume=None) -> str:
        act = _text(action, "action", 30).lower()
        act = ALIASES.get(act, act) or ("volume" if volume is not None else "")
        if act not in COMMANDS and act != "volume":
            raise ValueError("action is pause, unpause, stop, next, previous or volume")
        player = _text(player, "player", 100)
        if act == "volume":
            if volume is None:
                raise ValueError("volume is a whole number from 0 to 100")
            level = _number(volume, "volume", 0, 100, whole=True)
        s = self._player(player, prefer_playing=True)
        sid = quote(_id(s["Id"], "player id"), safe="")
        if act == "volume":
            self._call(f"/Sessions/{sid}/Command", "POST",
                       body={"Name": "SetVolume", "Arguments": {"Volume": str(level)}})
            return f"Volume set to {level} on {_player_name(s)}."
        self._call(f"/Sessions/{sid}/Playing/{COMMANDS[act]}", "POST")
        return f"Done: {act} on {_player_name(s)}."

    def now_playing(self) -> str:
        lines = []
        for s in self._sessions():
            np = s.get("NowPlayingItem")
            if not isinstance(np, dict):
                continue
            state = s.get("PlayState") if isinstance(s.get("PlayState"), dict) else {}
            ticks = state.get("PositionTicks")
            secs = int(ticks // 10_000_000) if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks > 0 else 0
            status = "paused" if state.get("IsPaused") else "playing"
            user = _clean(s.get("UserName"), 40)
            lines.append(f"{_player_name(s)}{f' ({user})' if user else ''}: {_item_line(np, with_id=False)} "
                         f"({status}, {secs // 60}:{secs % 60:02d} in)")
        return "\n".join(lines) if lines else "Nothing is playing on any Jellyfin player."

    def playlists(self) -> str:
        lists = self._lists(self._user())
        if not lists:
            return "There are no Jellyfin playlists yet."
        rows = []
        for p in lists[:100]:
            n = p.get("ChildCount")
            count = f" ({n} item{'s' if n != 1 else ''})" if isinstance(n, int) and not isinstance(n, bool) else ""
            pid = p.get("Id") if ID_RE.match(str(p.get("Id", ""))) else "?"
            rows.append(f"{_clean(p.get('Name')) or '(no name)'}{count} [id {pid}]")
        return f"{len(lists)} Jellyfin playlist{'s' if len(lists) != 1 else ''}:\n" + "\n".join(rows)

    def _resolve(self, items: list[str]) -> tuple[list[str], list[str], list[str]]:
        ids, lines, missing = [], [], []
        for phrase in items:
            got = self._track(phrase)
            if got:
                ids.append(got[0])
                lines.append(got[1])
            else:
                missing.append(_clean(phrase, 80))
        return ids, lines, missing

    def create_playlist(self, name: str, items=None) -> str:
        name = _text(name, "name", 100)
        if not name:
            raise ValueError("name is the new playlist's name")
        wanted = _list(items, "items", 50) if items not in (None, "", []) else []
        user = self._user()
        ids, lines, missing = self._resolve(wanted)
        self._call("/Playlists", "POST", body={"Name": name, "UserId": user, "Ids": ids, "MediaType": "Audio"})
        out = f"Created the Jellyfin playlist {_clean(name)!r} with {len(ids)} song{'s' if len(ids) != 1 else ''}."
        if lines:
            out += "\n" + "\n".join(lines)
        if missing:
            out += "\nNot found: " + "; ".join(missing)
        return out

    def add_to_playlist(self, playlist: str, items) -> str:
        playlist = _text(playlist, "playlist", 100)
        if not playlist:
            raise ValueError("playlist is the playlist's name or id")
        wanted = _list(items, "items", 50)
        user = self._user()
        pid, pname = self._playlist(user, playlist)
        ids, lines, missing = self._resolve(wanted)
        if ids:
            self._call(f"/Playlists/{quote(_id(pid, 'playlist id'), safe='')}/Items", "POST",
                       params={"Ids": ",".join(ids), "UserId": user})
        out = (f"Added {len(ids)} song{'s' if len(ids) != 1 else ''} to {pname!r}." if ids
               else f"Nothing was added to {pname!r}.")
        if lines:
            out += "\n" + "\n".join(lines)
        if missing:
            out += "\nNot found: " + "; ".join(missing)
        return out


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_PLAYER = _p("string", "The player's name or player id, as list_players shows. Optional when there is only one.")
_ITEMS = {"type": "array", "items": {"type": "string"},
          "description": "Song names (with the artist if known) or Jellyfin item ids, 1 to 50."}
TOOLS = [
    ("set_address", "Save the address of the owner's Jellyfin server on the home network, like "
     f"{EXAMPLE}. Only addresses on this computer or the home network are taken.",
     {"address": _p("string", f"The Jellyfin address, like {EXAMPLE}. Port 8096 when none is given.")}, ["address"]),
    ("search_media", "Search the owner's Jellyfin library for music, albums, artists, movies or shows by words. "
     "Gives up to 20 matches with their type, name, year or artist, and item id.",
     {"words": _p("string", "What to look for: a title, an artist, an album."),
      "kind": _p("string", "any, music, albums, artists, movies, shows or playlists. Default any.")}, ["words"]),
    ("list_players", "List the Jellyfin players that can be controlled (TV apps, phones, browsers): "
     "client, device, user and what each is playing.", {}, []),
    ("play", "Play something from the Jellyfin library on one of its players: a song, album, playlist, movie "
     "or show, by words or item id. Starts playback now.",
     {"what": _p("string", "What to play: words like 'Abbey Road' or an item id."),
      "player": _PLAYER,
      "kind": _p("string", "any, music, albums, movies, shows or playlists, to narrow the search. Default any.")},
     ["what"]),
    ("control", "Control a Jellyfin player: pause, unpause, stop, next, previous, or set the volume from 0 to 100.",
     {"action": _p("string", "pause, unpause, stop, next, previous or volume."),
      "player": _PLAYER,
      "volume": _p("integer", "For action volume: 0 to 100.")}, ["action"]),
    ("now_playing", "What each Jellyfin player is playing now, paused or playing, and how far in.", {}, []),
    ("playlists", "List the Jellyfin playlists with how many items each has.", {}, []),
    ("create_playlist", "Make a new Jellyfin music playlist, optionally filled with songs found by name or id.",
     {"name": _p("string", "The playlist's name."), "items": _ITEMS}, ["name"]),
    ("add_to_playlist", "Add songs, found by name or id, to an existing Jellyfin playlist.",
     {"playlist": _p("string", "The playlist's name or id."), "items": _ITEMS}, ["playlist", "items"]),
]
CHANGES = ["set_address", "play", "control", "create_playlist", "add_to_playlist"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Jellyfin, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def said(text: str, error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": _scrub(text)}], "isError": error})

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "No tool by that name."}}
        if not isinstance(args, dict):
            return said(f"Those arguments did not fit {name}: send them as an object.", True)
        try:
            return said(getattr(app, name)(**args), False)
        except (Plain, Unreadable) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Jellyfin media does not know that method."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "JellyfinMedia/1"
    app: Jellyfin
    token: str
    timeout = 15

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_BODY:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _body(self):
        n = self._length()
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            raise ValueError("The request ended before its body did.")
        return _loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Jellyfin media needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Jellyfin media stopped on this request ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")
        self._json(404, {"error": "Jellyfin media has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "Jellyfin media has no page here."})
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def _other(self):
        if not self._refused():
            self._send(405, b"", "text/plain")

    do_PUT = do_DELETE = do_PATCH = _other


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Jellyfin(Store(data))
    handler = type("JellyfinHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Jellyfin media for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Jellyfin media with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
