# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
"""News headlines: a plain RSS and Atom reader for the feeds the owner adds, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

HomeBot's ``auto_news_summary`` asked a Node-RED flow for the day's news. This
package needs no Node-RED: it reads the RSS and Atom feeds the owner adds, with
``xml.etree``, and gives back titles, sources, times and links. It ships with no
feeds and fetches no article pages.

The owner picks the feeds, so the package cannot list its hosts ahead of time;
its ``needs.hosts`` is empty and each feed's host must be allowed on the Bridge.
Every fetch: http or https only, never a private, loopback or link-local address,
at most 1 MB, at most 15 seconds, redirects only to the same host.

The feed list is ``feeds.json`` in PUFFPAI_DATA, written whole at once (temp file,
then rename); a file that will not read is never written over. ``cache.json``
keeps each feed's last headlines for 15 minutes.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "news-headlines"
VERSION = "1.0.0"
FEEDS = "feeds.json"
CACHE = "cache.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_ANSWER = 1024 * 1024        # a feed bigger than 1 MB is refused
TIMEOUT_S = 15
FRESH_S = 15 * 60               # a feed is fetched again after 15 minutes
MAX_FEEDS = 50
PER_FEED = 40                   # items kept per feed
USER_AGENT = "PuffPAI news-headlines package"
EPOCH = datetime(1970, 1, 2, tzinfo=timezone.utc)
LOCAL_NAMES = ("localhost", ".localhost", ".local", ".lan", ".home", ".internal", ".arpa")


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


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this package takes")


def _loads(text: str):
    """JSON from a request: NaN and Infinity are refused."""
    return json.loads(text, parse_constant=_no_constant)


# ---------------------------------------------------------------- outside calls

class Offline(RuntimeError):
    """A feed could not be read. The words say why, plainly."""


def check_url(url: str) -> urllib.parse.SplitResult:
    """A feed address that is http or https, names a host, and is not a private place. ValueError if not."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("a feed address starts with http:// or https://")
    try:
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ValueError("that feed address has a bad port") from None
    if not host:
        raise ValueError("that feed address has no host name")
    if parts.username or parts.password:
        raise ValueError("a feed address may not carry a user name or password")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError("News headlines never reads feeds from a private, loopback or local address")
    if ip is None and (host == "localhost" or host.endswith(LOCAL_NAMES) or "." not in host):
        raise ValueError("News headlines never reads feeds from a name on the home network")
    return parts


def _resolve(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _public_only(parts: urllib.parse.SplitResult) -> None:
    """Refuse a host whose name points at a private, loopback or link-local address."""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addrs = _resolve(parts.hostname, port)
    except OSError:
        raise Offline(f"{parts.hostname} could not be found. Is this computer online?") from None
    for a in addrs:
        if not ipaddress.ip_address(a.split("%", 1)[0]).is_global:
            raise Offline(f"{parts.hostname} points at a private address, so News headlines will not read it.")


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same host, http or https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme not in ("http", "https") or new.hostname != old.hostname:
            raise Offline(f"{old.hostname} sent the feed on to another host ({new.hostname}), so it was not read. "
                          f"Add the feed with its new address instead.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Proxies come from the environment (the Core points HTTP_PROXY and HTTPS_PROXY at the Bridge).
_OPENER = urllib.request.build_opener(_SameHost())


def fetch(url: str) -> bytes:
    """The feed's bytes, after every check above."""
    try:
        parts = check_url(url)
    except ValueError as exc:
        raise Offline(str(exc)) from None
    _public_only(parts)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/rss+xml, application/atom+xml, text/xml, */*"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        raise Offline(f"{parts.hostname} answered with error {exc.code}. If that is the Bridge, allow "
                      f"{parts.hostname} for News headlines on the Bridge page.") from None
    except (urllib.error.URLError, OSError) as exc:
        raise Offline(f"Could not reach {parts.hostname} ({getattr(exc, 'reason', exc)}). Is it allowed for "
                      f"News headlines on the Bridge page?") from None
    if len(raw) > MAX_ANSWER:
        raise Offline(f"{parts.hostname} sent more than 1 MB, so the feed was not read.")
    return raw


# ---------------------------------------------------------------- reading a feed

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _clean(text: str, most: int) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:most]


def _when(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(raw: bytes) -> list[dict]:
    """Items from an RSS 2.0, RSS 1.0 or Atom document: ``{title, link, time}``, newest first."""
    if b"<!ENTITY" in raw.upper():
        raise Offline("that feed declares its own XML entities, so it was not read.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise Offline("that address did not send an RSS or Atom feed.") from None
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title, link, guid, when = "", "", "", None
        for child in el:
            name = _local(child.tag)
            if name == "title":
                title = _clean("".join(child.itertext()), 300)
            elif name == "link":
                href = child.get("href")
                if href is not None:
                    if child.get("rel", "alternate") == "alternate" and not link:
                        link = href.strip()
                elif not link:
                    link = (child.text or "").strip()
            elif name == "guid" and child.get("isPermaLink", "true") != "false":
                guid = (child.text or "").strip()
            elif name in ("pubdate", "published", "updated", "date") and when is None:
                when = _when(child.text or "")
        link = link or guid
        if urllib.parse.urlsplit(link).scheme not in ("http", "https"):
            link = ""
        if title or link:
            items.append({"title": title or "(no title)", "link": link[:500],
                          "time": when.isoformat() if when else ""})
    items.sort(key=lambda i: (_when(i["time"]) or EPOCH).timestamp(), reverse=True)
    return items[:PER_FEED]


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


class Store:
    """The feed list (``feeds.json``) and the cache (``cache.json``)."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / FEEDS
        self.cache_path = self.folder / CACHE
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return {"feeds": [], "last_updated": ""}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). News headlines will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
                raise Unreadable(f"{self.path} is not a feed list. News headlines will not write over it.")
            data["feeds"] = [f for f in data["feeds"] if isinstance(f, dict) and isinstance(f.get("url"), str)
                             and isinstance(f.get("name"), str)]
            return data

    def _write(self, path: Path, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".news-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self, data: dict) -> None:
        with self.lock:
            data["last_updated"] = _now()
            self._write(self.path, data)

    def cache(self) -> dict:
        """The cache; a cache that will not read is simply empty (it is only a copy)."""
        with self.lock:
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return {}

    def save_cache(self, data: dict) -> None:
        with self.lock:
            self._write(self.cache_path, data)


# ------------------------------------------------------------- the logic

class News:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _find(self, feeds: list[dict], which: str) -> dict | None:
        w = which.strip().lower()
        return next((f for f in feeds if f["name"].lower() == w or f["url"].lower() == w), None)

    def _items(self, feed: dict, cache: dict) -> tuple[list[dict], str]:
        """A feed's items, from the cache when fresh; and a plain note when it could not be fetched."""
        kept = cache.get(feed["url"]) if isinstance(cache.get(feed["url"]), dict) else {}
        try:
            age = datetime.now().timestamp() - datetime.fromisoformat(kept.get("fetched", "")).timestamp()
        except (TypeError, ValueError):
            age = None
        if age is not None and 0 <= age < FRESH_S and isinstance(kept.get("items"), list):
            return kept["items"], ""
        try:
            items = parse_feed(fetch(feed["url"]))
        except Offline as exc:
            old = kept.get("items") if isinstance(kept.get("items"), list) else []
            return old, f"{feed['name']}: {exc}" + (" Showing the last headlines saved." if old else "")
        cache[feed["url"]] = {"fetched": _now(), "items": items}
        return items, ""

    # tools
    def add_feed(self, url: str, name: str = "") -> str:
        url = _text(url, "url", 500)
        parts = check_url(url)
        name = _text(name, "name", 60) or parts.hostname
        doc = self.store.load()
        if len(doc["feeds"]) >= MAX_FEEDS:
            return f"There are already {MAX_FEEDS} feeds. Remove one first."
        if self._find(doc["feeds"], url) or self._find(doc["feeds"], name):
            return f"There is already a feed called {name} or with that address. Nothing changed."
        doc["feeds"].append({"name": name, "url": url, "added": _now()})
        self.store.save(doc)
        return (f"Saved the feed {name} ({url}). News headlines can only reach the hosts the owner allows, so "
                f"before its headlines can load, allow {parts.hostname} for News headlines on the Bridge page.")

    def remove_feed(self, feed: str) -> str:
        feed = _text(feed, "feed", 500)
        doc = self.store.load()
        found = self._find(doc["feeds"], feed)
        if not found:
            return f"There is no feed called {feed}. Nothing changed."
        doc["feeds"].remove(found)
        self.store.save(doc)
        cache = self.store.cache()
        if cache.pop(found["url"], None) is not None:
            self.store.save_cache(cache)
        host = urllib.parse.urlsplit(found["url"]).hostname
        return f"Removed the feed {found['name']}. You can also close {host} for News headlines on the Bridge page."

    def list_feeds(self) -> str:
        feeds = self.store.load()["feeds"]
        if not feeds:
            return "No news feeds yet. Give me a feed address (RSS or Atom) and a name, and I will add it."
        return f"{len(feeds)} news feed{'s' if len(feeds) != 1 else ''}:\n" + "\n".join(
            f"- {f['name']}: {f['url']}" for f in feeds)

    def headlines(self, count: int = 10, feed: str = "") -> str:
        count = _number(count, "count", 1, 30, whole=True)
        feed = _text(feed, "feed", 500)
        feeds = self.store.load()["feeds"]
        if not feeds:
            return self.list_feeds()
        if feed:
            one = self._find(feeds, feed)
            if not one:
                return f"There is no feed called {feed}. " + self.list_feeds()
            feeds = [one]
        cache = self.store.cache()
        with ThreadPoolExecutor(max_workers=min(6, len(feeds))) as pool:
            got = list(pool.map(lambda f: self._items(f, cache), feeds))
        self.store.save_cache({f["url"]: cache[f["url"]] for f in self.store.load()["feeds"] if f["url"] in cache})
        rows, notes = [], []
        for f, (items, note) in zip(feeds, got):
            rows += [{**i, "source": f["name"]} for i in items if isinstance(i, dict)]
            if note:
                notes.append(note)
        rows.sort(key=lambda i: (_when(i.get("time", "")) or EPOCH).timestamp(), reverse=True)
        lines = []
        for n, i in enumerate(rows[:count], 1):
            when = _when(i.get("time", ""))
            stamp = when.astimezone().strftime("%a %d %b, %H:%M") if when else "time not given"
            lines.append(f"{n}. {i.get('title', '')} ({i['source']}, {stamp})" + (f" {i['link']}" if i.get("link") else ""))
        out = ("Latest headlines:\n" + "\n".join(lines)) if lines else "No headlines to show."
        return out + ("\n\nCould not load: " + " ".join(notes) if notes else "")


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("add_feed", "Add a news feed (an RSS or Atom address) with a short name. Its host must then be allowed on "
     "the Bridge before its headlines load.",
     {"url": _p("string", "The feed address, starting with http:// or https://."),
      "name": _p("string", "A short name for the feed, like the paper or site. Default: its host.")}, ["url"]),
    ("remove_feed", "Remove a news feed by its name or address.",
     {"feed": _p("string", "The feed's name or address.")}, ["feed"]),
    ("list_feeds", "List the news feeds that have been added, with their addresses.", {}, []),
    ("headlines", "The latest news headlines from the added feeds, or from one feed: title, source, time and "
     "link. No article text.",
     {"count": _p("integer", "How many headlines, from 1 to 30. Default 10."),
      "feed": _p("string", "Optional: one feed's name, to read only that one.")}, []),
]
CHANGES = ["add_feed", "remove_feed"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: News, msg: dict) -> dict | None:
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
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            return said(getattr(app, name)(**args), False)
        except (Unreadable, Offline) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601,
                                                   "message": f"News headlines does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "NewsHeadlines/1"
    app: News
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
        return _loads(raw.decode("utf-8")) if raw else {}

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
        self._json(401, {"error": "News headlines needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"News headlines stopped on this request ({type(exc).__name__})."})
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
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "News headlines has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "News headlines has no page here."})
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = News(Store(data))
    handler = type("NewsHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="News headlines for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts News headlines with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
