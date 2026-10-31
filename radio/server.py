# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/media_hub.py (rewritten-from)
"""World Radio: find internet radio stations and play them on a page, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``              the radio page (web/index.html), shown in the Desktop at /pkg/radio/
- ``/health``        "ok"
- ``/data``          favorites and what is playing (GET); the page polls it every 2 seconds
- ``/api/<action>``  POST from the page: search, play, stop, add_favorite, remove_favorite
- ``/mcp``           the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/radio/, which adds it.

The server itself contacts only the radio-browser.info directory (station
search). The streams play in the owner's browser, straight from each
station's own server; the server never fetches a stream.

Favorites and "now playing" are one JSON document in PUFFPAI_DATA
(``radio.json``). Writes replace the whole file at once (temp file, then
rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "radio"
VERSION = "1.0.0"
DOC = "radio.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
#: The station directory. de1 first; "all" is the round-robin name for every mirror.
HOSTS = ("de1.api.radio-browser.info", "all.api.radio-browser.info")
USER_AGENT = "PuffPAI radio package"
TIMEOUT_S = 8
MAX_REPLY = 2 * 1024 * 1024
TOP = 15
MAX_FAVORITES = 200
#: The page polls /data every 2 seconds; a poll in the last few seconds means it is open.
PAGE_OPEN_S = 6


def _now() -> str:
    return datetime.now().isoformat()


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


def _web_address(value, name: str, most: int, required: bool) -> str:
    """An http or https address with no spaces or control characters, or "" when allowed."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        if required:
            raise ValueError(f"{name} is missing")
        return ""
    if len(text) > most or any(c.isspace() or ord(c) < 32 for c in text):
        raise ValueError(f"{name} is not a web address")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{name} must start with http:// or https://")
    return text


def _short(value, most: int) -> str:
    return str(value or "").strip()[:most]


def _station(raw) -> dict:
    """One station in this package's shape, from a radio-browser row or from the page. Plain errors."""
    if not isinstance(raw, dict):
        raise ValueError("a station is an object with at least a name and a stream address")
    try:
        bitrate = _number(raw.get("bitrate") or 0, "bitrate", 0, 100_000, whole=True)
    except ValueError:
        bitrate = 0
    try:
        homepage = _web_address(raw.get("homepage"), "homepage", 500, False)
    except ValueError:
        homepage = ""
    return {
        "uuid": _short(raw.get("uuid") or raw.get("stationuuid"), 64),
        "name": _short(raw.get("name"), 120) or "Unnamed station",
        "url": _web_address(raw.get("url_resolved") or raw.get("url"), "the stream address", 2000, True),
        "country": _short(raw.get("country"), 80),
        "codec": _short(raw.get("codec"), 20),
        "bitrate": bitrate,
        "tags": _short(raw.get("tags"), 200),
        "homepage": homepage,
    }


def _line(st: dict) -> str:
    detail = [st["country"]] if st.get("country") else []
    if st.get("bitrate"):
        detail.append(f"{st.get('codec') or 'stream'} {st['bitrate']}k")
    return st.get("name", "Unnamed station") + (f" ({', '.join(detail)})" if detail else "")


def _match(items: list[dict], words: str) -> list[dict]:
    """Stations named *words*: exact names first, else names that contain it."""
    w = words.strip().lower()
    exact = [s for s in items if str(s.get("name", "")).strip().lower() == w]
    return exact or [s for s in items if w and w in str(s.get("name", "")).lower()]


# ---------------------------------------------------------------- the station directory

class Offline(RuntimeError):
    """radio-browser.info did not answer."""


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on one of the declared directory hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in HOSTS:
            return None                       # urllib then raises the redirect as an HTTPError
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Uses HTTPS_PROXY from the environment: the Core's Bridge proxy, which allows only needs.hosts.
OPENER = urllib.request.build_opener(_SameHost)


def fetch_json(path: str, params: dict):
    """GET https://<directory host><path>?<params> as JSON, trying each host in turn."""
    query = urllib.parse.urlencode(params)
    last = "no answer"
    for host in HOSTS:
        req = urllib.request.Request(f"https://{host}{path}?{query}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with OPENER.open(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read(MAX_REPLY + 1)
            if len(raw) > MAX_REPLY:
                last = "the answer was too big"
                continue
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, HTTPException, OSError, ValueError) as exc:
            last = type(exc).__name__
            continue
    raise Offline(f"World Radio can't reach the station directory (radio-browser.info) right now ({last}).")


def search(name: str = "", country: str = "", tag: str = "", language: str = "", limit: int = TOP) -> list[dict]:
    """Stations from radio-browser.info, most listened first. HLS streams and rows with no stream are
    dropped, because the page's player is a plain <audio> element (media_hub._station_search)."""
    params = {"limit": limit * 2, "hidebroken": "true", "order": "votes", "reverse": "true"}
    if name:
        params["name"] = name
    if len(country) == 2:
        params["countrycode"] = country.upper()
    elif country:
        params["country"] = country
    if tag:
        params["tag"] = tag.lower()
    if language:
        params["language"] = language.lower()
    rows = fetch_json("/json/stations/search", params)
    out, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("hls") or "0").lower() in ("1", "true"):
            continue
        try:
            st = _station(row)
        except ValueError:
            continue
        if st["url"] in seen:
            continue
        seen.add(st["url"])
        out.append(st)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"favorites": [], "now_playing": None, "play_id": 0, "last_updated": ""}


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return default_doc()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). World Radio will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("favorites", []), list):
                raise Unreadable(f"{self.path} is not a World Radio file. World Radio will not write over it.")
            out = default_doc()
            out.update(data)
            out["favorites"] = [f for f in out["favorites"] if isinstance(f, dict) and f.get("url")]
            if not isinstance(out["now_playing"], dict):
                out["now_playing"] = None
            if isinstance(out["play_id"], bool) or not isinstance(out["play_id"], int):
                out["play_id"] = 0
            return out

    def save(self, data: dict) -> dict:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".radio-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data


# ------------------------------------------------------------- the logic

class Radio:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.lock = threading.RLock()
        self.last: list[dict] = []          # the last search, so "play number 2" works
        self.page_seen = 0.0                # monotonic time of the page's last /data poll

    # helpers
    def page_open(self) -> bool:
        return bool(self.page_seen) and time.monotonic() - self.page_seen < PAGE_OPEN_S

    def _hear(self) -> str:
        if self.page_open():
            return ("The World Radio page is open, so it should start in a moment. If it stays quiet, "
                    "click once on that page: browsers block sound until you do.")
        return ("It plays on the World Radio page, which is not open now: open World Radio in the Desktop "
                "to hear it (the first time, click once on the page).")

    def _pick(self, station: str) -> dict:
        """A station from a number in the last search, a favorite's name, a name in the last search,
        or else a directory search by name (a station asked for by name plays that station)."""
        words = _text(station, "station", 120)
        if not words:
            raise ValueError("say which station: a name, or a number from the last search")
        if words.isdigit():
            n = int(words)
            if not self.last:
                raise ValueError("there is no search to pick a number from; find stations first")
            if not 1 <= n <= len(self.last):
                raise ValueError(f"the last search had stations 1 to {len(self.last)}")
            return self.last[n - 1]
        found = _match(self.store.load()["favorites"], words) or _match(self.last, words)
        if found:
            return found[0]
        hits = search(name=words)
        if hits:
            self.last = hits
            return hits[0]
        raise LookupError(f"No station called {words!r} was found. Try find_stations with a genre or country.")

    def _set_playing(self, st: dict | None) -> dict:
        with self.store.lock:
            data = self.store.load()
            data["now_playing"] = st
            data["play_id"] = int(data.get("play_id") or 0) + 1
            return self.store.save(data)

    def _add_fav(self, st: dict) -> str:
        with self.store.lock:
            data = self.store.load()
            if any(f.get("url") == st["url"] for f in data["favorites"]):
                return f"{st['name']} is already a favorite."
            if len(data["favorites"]) >= MAX_FAVORITES:
                raise ValueError(f"there are already {MAX_FAVORITES} favorites; remove one first")
            data["favorites"].append(st)
            self.store.save(data)
            return f"Saved {st['name']} to the radio favorites."

    def _remove_fav(self, url: str) -> None:
        with self.store.lock:
            data = self.store.load()
            keep = [f for f in data["favorites"] if f.get("url") != url]
            if len(keep) != len(data["favorites"]):
                data["favorites"] = keep
                self.store.save(data)

    # tools
    def find_stations(self, name: str = "", country: str = "", tag: str = "", language: str = "") -> str:
        """media_radio_stations (rebot_tools.py:2192): a numbered shortlist, nothing played."""
        name = _text(name, "name", 80)
        country = _text(country, "country", 60)
        tag = _text(tag, "tag", 40)
        language = _text(language, "language", 40)
        hits = search(name=name, country=country, tag=tag, language=language)
        with self.lock:
            self.last = hits
        asked = ", ".join(x for x in (name, tag, language, country) if x) or "any station"
        if not hits:
            return f"radio-browser.info has no working stations for {asked}."
        rows = "\n".join(f"{i}. {_line(s)}. Stream: {s['url']}" for i, s in enumerate(hits, 1))
        return (f"Top {len(hits)} stations for {asked} (from radio-browser.info, most voted first):\n{rows}\n"
                "Use play_station with a number or a name to play one.")

    def play_station(self, station: str) -> str:
        """media_radio_play (rebot_tools.py:2186). HomeBot played on the box's speaker; here it sets the
        page's "now playing", and the open page starts it."""
        with self.lock:
            st = self._pick(station)
            self._set_playing(st)
        return f"Now playing: {_line(st)}. {self._hear()}"

    def stop(self) -> str:
        was = self.store.load()["now_playing"]
        self._set_playing(None)
        return f"Stopped {was.get('name', 'the radio')}." if was else "Nothing was playing."

    def now_playing(self) -> str:
        st = self.store.load()["now_playing"]
        if not st:
            return "Nothing is playing on World Radio."
        where = ("The World Radio page is open." if self.page_open()
                 else "The World Radio page is not open now, so nothing can be heard until it is.")
        return f"World Radio is set to {_line(st)}. {where}"

    def favorites(self) -> str:
        favs = self.store.load()["favorites"]
        if not favs:
            return "No favorite radio stations yet."
        return f"{len(favs)} favorite stations:\n" + "\n".join(f"{i}. {_line(s)}" for i, s in enumerate(favs, 1))

    def add_favorite(self, station: str = "") -> str:
        with self.lock:
            if not _text(station, "station", 120):
                st = self.store.load()["now_playing"]
                if not st:
                    raise ValueError("nothing is playing; say which station to save")
            else:
                st = self._pick(station)
            return self._add_fav(st)

    def remove_favorite(self, station: str) -> str:
        words = _text(station, "station", 120)
        if not words:
            raise ValueError("say which favorite to remove")
        favs = self.store.load()["favorites"]
        if words.isdigit() and 1 <= int(words) <= len(favs):
            found = [favs[int(words) - 1]]
        else:
            found = _match(favs, words)
        if not found:
            return f"There is no favorite called {words!r}."
        if len(found) > 1:
            return "More than one favorite matches: " + "; ".join(s["name"] for s in found[:8]) + ". Which one?"
        self._remove_fav(found[0]["url"])
        return f"Removed {found[0]['name']} from the radio favorites."

    # the page
    def page(self, action: str, body: dict) -> dict:
        """The page's buttons. A station from the page is the owner's own pick from its results."""
        if action == "search":
            hits = search(name=_text(body.get("name"), "name", 80), country=_text(body.get("country"), "country", 60),
                          tag=_text(body.get("tag"), "tag", 40), language=_text(body.get("language"), "language", 40))
            with self.lock:
                self.last = hits
            return {"stations": hits}
        if action == "play":
            return self._set_playing(_station(body.get("station")))
        if action == "stop":
            return self._set_playing(None)
        if action == "add_favorite":
            self._add_fav(_station(body.get("station")))
            return self.store.load()
        if action == "remove_favorite":
            self._remove_fav(_text(body.get("url"), "url", 2000))
            return self.store.load()
        raise LookupError("World Radio has no page here.")


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_PICK = "A station name, a favorite's name, or a number from the last find_stations list."
TOOLS = [
    ("find_stations", "Find internet radio stations by name, country, genre or language, without playing one. "
     "Gives the top 15, numbered, with country, bitrate and stream address.",
     {"name": _p("string", "Part of the station's name, like kexp or bbc."),
      "country": _p("string", "A country name or 2-letter code, like Ireland or IE."),
      "tag": _p("string", "A genre or tag, like jazz, news, classical or rock."),
      "language": _p("string", "A language, like spanish or french.")}, []),
    ("play_station", "Play an internet radio station on the World Radio page. The page must be open in the "
     "Desktop to hear it.", {"station": _p("string", _PICK)}, ["station"]),
    ("stop", "Stop the internet radio on the World Radio page.", {}, []),
    ("now_playing", "Say which radio station World Radio is set to, and whether its page is open.", {}, []),
    ("favorites", "List the saved favorite radio stations.", {}, []),
    ("add_favorite", "Save a radio station to the favorites. With no station, saves the one playing now.",
     {"station": _p("string", _PICK + " Empty saves the one playing now.")}, []),
    ("remove_favorite", "Remove a radio station from the favorites, by its name or number.",
     {"station": _p("string", "The favorite's name, or its number in the favorites list.")}, ["station"]),
]
CHANGES = ["play_station", "stop", "add_favorite", "remove_favorite"]
TOOL_NAMES = {t[0] for t in TOOLS}


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Radio, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def said(text: str, error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": text}], "isError": error})

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
        if name not in TOOL_NAMES:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        if not isinstance(args, dict):
            return said(f"Those arguments did not fit {name}: send an object.", True)
        try:
            return said(getattr(app, name)(**args), False)
        except (Unreadable, Offline, LookupError) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"World Radio does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "WorldRadio/1"
    app: Radio
    token: str
    timeout = 15        # seconds; a stalled sender is cut off (set on the socket)

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
        """Content-Length, strictly: missing is 0; otherwise digits only, at most MAX_BODY bytes."""
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
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "World Radio needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"World Radio stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/data":
                self.app.page_seen = time.monotonic()
                return self._json(200, self.app.store.load())
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "World Radio has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if not path.startswith("/api/"):
            return self._json(404, {"error": "World Radio has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            self._json(200, self.app.page(path[5:], body))
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except Offline as exc:
            self._json(502, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def _mcp(self):
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Radio(Store(data))
    handler = type("RadioHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="World Radio for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts World Radio with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
