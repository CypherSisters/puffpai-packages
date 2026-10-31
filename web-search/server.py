# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/agents/toolsmith.py (rewritten-from)
"""Web search: ask a SearXNG the owner runs, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

The one setting is the SearXNG address, one JSON document in PUFFPAI_DATA
(``search.json``). Only an address on this computer (loopback) or on the home
network (10/8, 172.16/12, 192.168/16, link-local, or a name ending in .local
or .lan that points only at such addresses) is taken. A public SearXNG is
refused: it would see every question. The address is checked again before
every call, so a hand-edited file can't point it somewhere else.

A search sends ``q``, ``format=json`` and ``categories`` and nothing else: no
cookies, no language, no other headers about the owner. Queries are never
logged or stored. A SearXNG on this computer is asked directly; one on the
home network is asked through the Bridge (the Core's proxy, from the
environment only), which must allow that address. Each call waits at most
15 seconds, reads at most 1 MB, and follows a redirect only to the same
scheme, host and port (3 at most).

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "web-search"
VERSION = "1.0.0"
DOC = "search.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_ANSWER = 1024 * 1024           # an answer bigger than 1 MB is refused
TIMEOUT_S = 15.0
MAX_REDIRECTS = 3
TOP = 8
MAX_QUESTION = 300
USER_AGENT = "PuffPAI web-search package"
CATEGORIES = ("general", "news")
HOME_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
LOCAL_SUFFIXES = (".local", ".lan")
_NAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_PATH_RE = re.compile(r"^[A-Za-z0-9/._~-]*$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")

NO_ADDRESS = ("Web search has no SearXNG address yet. Tell me the address of your own SearXNG, "
              "like http://192.168.1.20:8888 or http://127.0.0.1:8888, and I will save it.")
PUBLIC = ("That looks like a SearXNG on the internet. This package is only for your own SearXNG, on this "
          "computer or your home network, because a public one would see every question you ask. "
          "Nothing was saved.")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- checked input

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


def _clean(value, most: int) -> str:
    """Outside text for showing: no control or direction characters, one line, at most *most* characters."""
    text = _CTRL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= most else text[: most - 3].rstrip() + "..."


def address_kind(addr: str) -> str:
    """``loopback``, ``home`` (RFC1918) or ``link-local`` for an address this package may ask; else ''."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified:
        return ""
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.version == 4 and any(ip in n for n in HOME_NETS):
        return "home"
    return ""


def _resolve(host: str, port: int) -> list[str]:
    """Every address *host* names (v4 and v6). Patched in tests."""
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


class Refused(ValueError):
    """An address this package will not ask. The words say why."""


def check_base(value) -> tuple[str, bool]:
    """``(base URL, direct)`` for a SearXNG address the owner may use, or :class:`Refused`.
    *direct* is True only when every address it names is on this computer (no proxy needed)."""
    raw = _text(value, "address", 200)
    if not raw:
        raise Refused("Give the address of your own SearXNG, like http://192.168.1.20:8888.")
    if any(c.isspace() for c in raw) or _CTRL_RE.search(raw):
        raise Refused("An address has no spaces in it.")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise Refused("The address starts with http:// or https://.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("Leave any name or password out of the address. This package uses no keys.")
    if parts.query or parts.fragment:
        raise Refused("Give only the address, without anything after a ? or #.")
    try:
        port = parts.port
    except ValueError:
        raise Refused("The port in that address is not a number from 1 to 65535.") from None
    host = (parts.hostname or "").lower()
    if not host or "%" in host:
        raise Refused("That address has no plain host in it.")
    path = parts.path.rstrip("/")
    if not _PATH_RE.match(path) or ".." in path:
        raise Refused("The part after the host may hold only letters, digits and / . _ ~ -.")
    scheme = parts.scheme.lower()
    if port is not None and not 1 <= port <= 65535:
        raise Refused("The port in that address is not a number from 1 to 65535.")
    port = port or (443 if scheme == "https" else 80)
    if address_kind(host):
        kinds = {address_kind(host)}
    else:
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        if is_ip:
            ip = ipaddress.ip_address(host)
            if ip.is_global:
                raise Refused(PUBLIC)
            raise Refused(f"{host} is not an address on this computer or a home network (10.x, 172.16-31.x, "
                          "192.168.x or link-local). Nothing was saved.")
        if not _NAME_RE.match(host):
            raise Refused("That host is not a plain name or address.")
        if host != "localhost" and not host.endswith(LOCAL_SUFFIXES):
            raise Refused(PUBLIC)
        try:
            found = _resolve(host, port)
        except (OSError, UnicodeError):
            raise Refused(f"This computer can't find {host} right now. Try its numbers instead, "
                          "like http://192.168.1.20:8888.") from None
        kinds = {address_kind(a.split("%", 1)[0]) for a in found}
        if not found or "" in kinds:
            raise Refused(f"{host} points at an address outside your home network, so this package will "
                          "not ask it. Nothing was sent.")
    shown = f"[{host}]" if ":" in host else host
    netloc = shown if port == (443 if scheme == "https" else 80) else f"{shown}:{port}"
    return f"{scheme}://{netloc}{path}", kinds == {"loopback"}


# ---------------------------------------------------------------- outside calls

class Offline(RuntimeError):
    """The SearXNG did not answer usefully. The words say what happened, never the question."""


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same scheme, host and port, and at most MAX_REDIRECTS times."""
    max_redirections = MAX_REDIRECTS
    max_repeats = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc.lower()) != (old.scheme, old.netloc.lower()):
            raise Offline("Your SearXNG sent the request on to another place, so Web search stopped there.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(direct: bool):
    """No cookies. Direct for this computer; otherwise proxies from the environment only (the Bridge),
    never the Windows proxy settings."""
    proxies = {} if direct else urllib.request.getproxies_environment()
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _SameOrigin())


def http_get(url: str, direct: bool) -> tuple[int, bytes]:
    """GET *url*: ``(status, body)``. Errors are :class:`Offline` with plain words and never the URL's
    query. Patched in tests, so they never touch the network."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with _opener(direct).open(req, timeout=TIMEOUT_S) as r:
            status, chunks, size = r.status, [], 0
            while True:
                if time.monotonic() > deadline:
                    raise Offline(f"Your SearXNG took more than {TIMEOUT_S:g} seconds, so Web search stopped.")
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ANSWER:
                    raise Offline("Your SearXNG sent more than 1 MB, so Web search did not read it.")
                chunks.append(chunk)
            return status, b"".join(chunks)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b""
    except Offline:
        raise
    except (TimeoutError, socket.timeout):
        raise Offline(f"Your SearXNG did not answer within {TIMEOUT_S:g} seconds.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        why = getattr(exc, "reason", None) or type(exc).__name__
        if isinstance(why, BaseException):
            why = type(why).__name__
        raise Offline(f"Could not reach your SearXNG ({_clean(why, 80)}). Is it running, and if it is on "
                      "another computer, does the Bridge allow this address and port for Web search?") from None


def _status_words(code: int) -> str:
    if code == 403:
        return ("Your SearXNG (or the Bridge) answered 403, refused. Check that SearXNG has the json format "
                "turned on, and, for a SearXNG on another computer, that the Bridge allows this address and port.")
    if code == 429:
        return "Your SearXNG answered 429, too many searches. Its limiter is on; let this computer through it."
    if code in (407, 502):
        return f"The Bridge did not pass the search on (error {code}). Allow this address and port for Web search."
    return f"Your SearXNG answered with error {code}."

# ---------------------------------------------------------------- asking the Core for a Yes card (D110)

ASK_PATH = "/v1/bridge/addresses/ask"
ASK_TIMEOUT_S = 5
ASK_MOST = 16 * 1024               # the most it reads of the Core's reply
LINE_MOST = 300                    # the most of the Core's line it says back
ASK_STATUS = {"allowed": 200, "not_needed": 200, "waiting": 202}
NO_CARD = "No Yes card could be raised right now. The owner can allow this address and port on the Bridge page."


class _NoAskRedirect(urllib.request.HTTPRedirectHandler):
    """This package's token goes to the Core and nowhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Direct, never through the Bridge proxy: the Core is on this computer.
_ASK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoAskRedirect())


def _ask_core(place: str) -> tuple[str, str] | None:
    """Ask the Core for a Yes card for one ``host:port``: ``(state, its line)``, or None when no answer could be had.
    It grants nothing and never raises, so a failed ask never touches what was saved."""
    api = os.environ.get("PUFFPAI_API", "").strip().rstrip("/")
    token = os.environ.get("PUFFPAI_TOKEN", "").strip()
    if not api or not token:
        return None
    try:
        req = urllib.request.Request(api + ASK_PATH, data=json.dumps({"address": place}).encode("utf-8"), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with _ASK_OPENER.open(req, timeout=ASK_TIMEOUT_S) as r:
                code, raw = r.status, r.read(ASK_MOST + 1)
        except urllib.error.HTTPError as exc:
            with exc:
                code, raw = exc.code, exc.read(ASK_MOST + 1)
        said = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - no Core, no answer or no sense: the same plain words, and the save stands
        return None
    state, line = (said.get("state"), said.get("line")) if isinstance(said, dict) else (None, None)
    if not isinstance(state, str) or not isinstance(line, str):
        return None
    if code != ASK_STATUS.get(state) and not (state == "refused" and code >= 400):
        return None
    line = " ".join("".join(c if c.isprintable() else " " for c in line.replace(token, "[hidden]")).split())[:LINE_MOST]
    return (state, line) if line else None


def _card_words(host: str, port: int) -> str:
    """What is true of this address now, after the save: the Core's line, and what it means."""
    place = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    got = _ask_core(place)
    if got is None:
        return NO_CARD
    state, line = got
    if state == "allowed":
        return f"{line} This package can reach {place} now, while the Bridge is open."
    if state == "waiting":
        if line.lower().startswith("waiting"):
            return f"{line} A Yes card for {place} is waiting for the owner. Nothing reaches it until her Yes."
        return f"{line} Nothing reaches {place} for now."       # the Core's "waiting" with no card: its line says why
    return f"{line} No card was raised." if state == "refused" else line


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"address": "", "last_updated": ""}


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
                raise Unreadable(f"{self.path} will not read ({type(exc).__name__}). "
                                 "Web search will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("address", ""), str):
                raise Unreadable(f"{self.path} is not a Web search settings file. Web search will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".search-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

def _engine(r: dict) -> str:
    e = r.get("engine")
    if not isinstance(e, str) or not e:
        es = r.get("engines")
        e = es[0] if isinstance(es, list) and es and isinstance(es[0], str) else ""
    return _clean(e, 40)


def format_results(data, label: str) -> str:
    """The top results from a SearXNG JSON answer, as plain lines. Image results are left out."""
    if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
        raise Offline("Your SearXNG sent something that is not search results.")
    rows, seen = [], set()
    for r in data.get("results") or []:
        if not isinstance(r, dict) or r.get("category") == "images" or "images" in str(r.get("template") or ""):
            continue
        url = r.get("url")
        if not isinstance(url, str) or len(url) > 2000 or not url.lower().startswith(("http://", "https://")):
            continue
        url = _clean(url, 2000)
        if url in seen:
            continue
        seen.add(url)
        title = _clean(r.get("title"), 150) or "(no title)"
        snippet = _clean(r.get("content"), 200)
        engine = _engine(r)
        line = f"{len(rows) + 1}. {title}"
        if snippet:
            line += f"\n   {snippet}"
        line += f"\n   {url}" + (f" (from {engine})" if engine else "")
        rows.append(line)
        if len(rows) == TOP:
            break
    if not rows:
        quiet = [_clean(u[0], 40) for u in data.get("unresponsive_engines") or []
                 if isinstance(u, (list, tuple)) and u and isinstance(u[0], str)][:5]
        if quiet:
            return f"Your SearXNG found no {label} for that. These engines did not answer: {', '.join(quiet)}."
        return f"Your SearXNG found no {label} for that."
    return f"Top {len(rows)} {label} from your SearXNG:\n" + "\n".join(rows)


class WebSearch:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _base(self) -> tuple[str, bool] | None:
        saved = self.store.load().get("address") or ""
        if not saved:
            return None
        try:
            return check_base(saved)
        except Refused as exc:
            raise Offline(f"The saved address is not one Web search will ask: {exc}") from None

    def _search(self, question, category: str, label: str) -> str:
        question = _text(question, "question", MAX_QUESTION)
        if not question:
            raise ValueError("question is the words to search for")
        base = self._base()
        if base is None:
            return NO_ADDRESS
        url, direct = base
        query = urllib.parse.urlencode({"q": question, "format": "json", "categories": category})
        status, raw = http_get(f"{url}/search?{query}", direct)
        if status != 200:
            raise Offline(_status_words(status))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Offline("Your SearXNG did not send JSON. Turn on its json format.") from None
        return format_results(data, label)

    # tools
    def web_search(self, question: str, category: str = "general") -> str:
        cat = _text(category, "category", 20, "general").lower()
        if cat not in CATEGORIES:
            raise ValueError("category is general or news (image search is off in this package)")
        return self._search(question, cat, "news stories" if cat == "news" else "results")

    def news_search(self, question: str) -> str:
        return self._search(question, "news", "news stories")

    def search_settings(self) -> str:
        doc = self.store.load()
        if not doc.get("address"):
            return NO_ADDRESS
        url, direct = self._base()
        where = "on this computer" if direct else "on your home network, through the Bridge"
        head = f"Web search asks the SearXNG at {url} ({where}), saved {doc.get('last_updated') or 'earlier'}."
        try:
            status, _ = http_get(f"{url}/healthz", direct)
            if status == 200:
                return f"{head} It answered its health check just now."
            status, _ = http_get(f"{url}/", direct)
            if status == 200:
                return f"{head} It answered at its front page just now (no health check there)."
            return f"{head} It did not answer as SearXNG: {_status_words(status)}"
        except Offline as exc:
            return f"{head} {exc}"

    def set_address(self, address: str) -> str:
        url, direct = check_base(address)
        with self.store.lock:
            doc = self.store.load()
            doc["address"] = url
            self.store.save(doc)
        if direct:
            return f"Saved. Web search will ask the SearXNG at {url}, on this computer."
        parts = urllib.parse.urlsplit(url)
        words = _card_words(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
        return f"Saved. Web search will ask the SearXNG at {url}, through the Bridge. {words}"


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str, **more) -> dict:
    return {"type": kind, "description": text, **more}


TOOLS = [
    ("web_search", "Search the web through the owner's own SearXNG and get the top 8 results: the title, a one-line "
     "snippet, the address and the engine that found it. Good for news, prices, opening hours and anything that "
     "changes.",
     {"question": _p("string", "The words to search for, at most 300 characters."),
      "category": _p("string", "general or news. Default general.", enum=list(CATEGORIES))}, ["question"]),
    ("news_search", "Search recent news through the owner's own SearXNG and get the top 8 stories: the title, a "
     "one-line snippet, the address and the engine that found it.",
     {"question": _p("string", "The news topic to search for, at most 300 characters.")}, ["question"]),
    ("search_settings", "Which SearXNG address web search uses, and whether it answers right now.", {}, []),
    ("set_address", "Save the address of the owner's own SearXNG, like http://192.168.1.20:8888 or "
     "http://127.0.0.1:8888. Only an address on this computer or the home network is taken; a public SearXNG is "
     "refused. For an address off this computer, the bot raises a Yes card for that address and port.",
     {"address": _p("string", "The SearXNG address, http or https, on this computer or the home network.")},
     ["address"]),
]
CHANGES = ["set_address"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: WebSearch, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params is an object."}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def text(words: str, error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": words}], "isError": error})

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
        if not isinstance(args, dict):
            return text(f"Those arguments did not fit {name}: arguments is an object", True)
        try:
            return text(getattr(app, name)(**args), False)
        except (Unreadable, Offline, Refused) as exc:
            return text(str(exc), True)
        except (TypeError, ValueError) as exc:
            return text(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return text(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Web search does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "WebSearch/1"
    app: WebSearch
    token: str
    timeout = 15        # seconds; a stalled sender is cut off (set on the socket)

    def log_message(self, fmt, *args):     # quiet: nothing is logged, questions least of all
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
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

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
        self._json(401, {"error": "Web search needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Web search stopped on this request with an error ({type(exc).__name__})."})
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
        self._json(404, {"error": "Web search has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Web search has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError):
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
    app = WebSearch(Store(data))
    handler = type("WebSearchHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Web search for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Web search with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
