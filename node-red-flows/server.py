# Harvested from HomeBotFounders@b77fc40:src/puffpai/automation_client.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:nodered_flows/flows.json (adapted: flows/homebot-flows.json)
"""Node-RED flows: list, run, and turn on or off the flows in the owner's Node-RED, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``   "ok"
- ``/mcp``      the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

It talks to one Node-RED only: the address the owner sets with ``set_address``
(default ``http://127.0.0.1:1880``, this computer). That address must be on
this computer or the home network: a loopback, private (RFC 1918, fc00::/7)
or link-local address, or a name ending in ``.local`` or ``.lan``. A name is
looked up when it is saved. Off this computer, the request goes through the
Bridge proxy when PuffPAI gives one, never around it, with the name as saved,
and the Bridge checks the name's answers when it connects. With no proxy, a
name is looked up at each request, every answer must be such an address, and
the connection goes to the address that was checked.
Every request has a 10 second limit, follows no redirect, and reads at most
1 MB of answer.

The optional NODE_RED_TOKEN (Node-RED's adminAuth) comes from the environment
only. It is sent to the Node-RED admin API and nowhere else, and is never
written to disk, logged or put in an answer.

The address is one JSON document in PUFFPAI_DATA (``nodered.json``). Writes
replace the whole file at once (temp file, then rename). A file that exists
but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

NAME = "node-red-flows"
VERSION = "1.0.0"
DOC = "nodered.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024            # what this server reads from the Core
DEFAULT_ADDRESS = "http://127.0.0.1:1880"
DEFAULT_PORT = 1880
TIMEOUT_S = 10.0
MAX_ANSWER = 1024 * 1024         # what it reads from Node-RED
MAX_SEND = 16 * 1024             # the JSON body run_flow may send
SHOW = 2000                      # characters of a flow's answer put in a reply
AGENT = "PuffPAI-node-red-flows/1.0"
KEY_ENV = "NODE_RED_TOKEN"

_LOOP4 = ipaddress.ip_network("127.0.0.0/8")
_LAN4 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
_LINK6 = ipaddress.ip_network("fe80::/10")
_HOME6 = ipaddress.ip_network("fc00::/7")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_NAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*$")
_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._~-]+)*/?$")
_FLOW_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _now() -> str:
    return datetime.now().isoformat()


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


def _secrets() -> list[str]:
    """The secret values this process holds: the Node-RED login and the Bridge proxy password."""
    found = [os.environ.get(KEY_ENV, "").strip()]
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        raw = os.environ.get(k) or ""
        try:
            pw = urlsplit(raw if "://" in raw else "http://" + raw).password if raw else None
        except ValueError:
            pw = None
        if pw:
            found += [pw, unquote(pw)]
    return sorted({v for v in found if len(v) >= 8}, key=len, reverse=True)


def _scrub(text: str) -> str:
    """Belt and braces: a secret never appears in an answer, whatever an outside service sent back."""
    text = str(text)
    for secret in _secrets():
        text = text.replace(secret, "[hidden]")
    return text


def _shown(value, most: int = 80) -> str:
    """Text from Node-RED, made safe to show: one line, no control characters, capped."""
    return _CONTROL.sub(" ", str(value if value is not None else "")).replace("\n", " ").strip()[:most]


# ---------------------------------------------------------------- addresses

def _ip_kind(ip) -> str | None:
    """'loopback', 'lan', or None for any address this package may not reach."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 4:
        if ip in _LOOP4:
            return "loopback"
        return "lan" if any(ip in n for n in _LAN4) else None
    if ip == ipaddress.IPv6Address("::1"):
        return "loopback"
    return "lan" if ip in _LINK6 or ip in _HOME6 else None


def host_kind(host: str) -> str | None:
    """What kind of host an address names: 'loopback', 'lan', 'name' (a .local or .lan name), or None."""
    host = (host or "").strip().lower().rstrip(".")
    try:
        return _ip_kind(ipaddress.ip_address(host))
    except ValueError:
        pass
    if host == "localhost":
        return "loopback"
    if _NAME_RE.match(host) and (host.endswith(".local") or host.endswith(".lan")) and host.count(".") >= 1 \
            and host.split(".")[0] not in ("local", "lan"):
        return "name"
    return None

def _mapped(host: str) -> bool:
    """An IPv4 address written the IPv6 way (``::ffff:127.0.0.1``), which a direct connect on Windows can't use."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.version == 6 and ip.ipv4_mapped is not None

def check_address(text) -> str:
    """The owner's Node-RED address, checked and written one way: ``scheme://host:port[/admin-path]``."""
    text = _text(text, "address", 200)
    if not text:
        raise ValueError("Give the Node-RED address, like http://127.0.0.1:1880 or http://192.168.1.20:1880.")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = DEFAULT_PORT if parts.port is None else parts.port
    except ValueError:
        raise ValueError("That is not a web address with a valid port.") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("The address starts with http:// or https://.")
    if "@" in parts.netloc:
        raise ValueError("Leave the login out of the address. A login goes in NODE_RED_TOKEN.")
    if parts.query or parts.fragment or not _PATH_RE.match(parts.path or "/") or ".." in parts.path:
        raise ValueError("The address is the Node-RED server, with no ?query or #part, like http://127.0.0.1:1880.")
    host = (parts.hostname or "").rstrip(".")
    if not 1 <= port <= 65535:
        raise ValueError("The port is a number from 1 to 65535.")
    if _mapped(host):
        raise ValueError("An address written with ::ffff: is not taken. Write it plainly, like 127.0.0.1 or 192.168.1.20.")
    if host_kind(host) is None:
        raise ValueError("That address is not on this computer or your home network. Use 127.0.0.1, localhost, "
                         "a home address (10.x, 172.16-31.x, 192.168.x, fd00::1, 169.254.x), or a name ending in .local "
                         "or .lan.")
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown}:{port}{(parts.path or '').rstrip('/')}"


class Refused(RuntimeError):
    """A request this package will not make, with a plain reason."""


def _vetted_ip(host: str, port: int) -> str:
    """The address to connect to: the host itself, or every answer for a name checked first."""
    kind = host_kind(host)
    if kind is None:
        raise Refused("The saved address is not on this computer or your home network. Set it again.")
    if kind in ("loopback", "lan"):
        return "127.0.0.1" if host == "localhost" else host
    try:
        answers = {a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError:
        raise Refused(f"The name {host} did not look up on this network.") from None
    if not answers or any(_ip_kind(ipaddress.ip_address(a.split("%", 1)[0])) is None for a in answers):
        raise Refused(f"The name {host} pointed at an address outside your home network, so it was not used.")
    return sorted(answers)[0]


def _proxy() -> tuple[str, int, dict] | None:
    """The Bridge proxy PuffPAI gives this package, as (host, port, headers), or None."""
    raw = next((os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
                if os.environ.get(k)), "")
    if not raw:
        return None
    p = urlsplit(raw if "://" in raw else "http://" + raw)
    if p.scheme != "http" or not p.hostname:
        raise Refused("The proxy PuffPAI gave is not one this package can use.")
    headers = {}
    if p.username is not None:
        pair = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(pair).decode()
    return p.hostname, p.port or 80, headers


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip, self._ctx = ip, context

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


def fetch(base: str, method: str, path: str, body: bytes | None = None,
          headers: dict | None = None) -> tuple[int, str, bytes, bool]:
    """One request to the owner's Node-RED: (status, content type, answer, went through the Bridge proxy).
    No redirects are followed."""
    parts = urlsplit(base)
    host, port, scheme = (parts.hostname or ""), (parts.port or DEFAULT_PORT), parts.scheme
    kind = host_kind(host)
    if kind is None or scheme not in ("http", "https"):
        raise Refused("The saved address is not on this computer or your home network. Set it again.")
    send = {"User-Agent": AGENT, "Accept": "application/json", **(headers or {})}
    proxy = _proxy() if kind != "loopback" else None
    ctx = ssl.create_default_context()
    if proxy:
        p_host, p_port, p_headers = proxy
        if scheme == "https":
            conn = http.client.HTTPSConnection(p_host, p_port, timeout=TIMEOUT_S, context=ctx)
            conn.set_tunnel(host, port, headers=p_headers)
            target = path
        else:
            conn = http.client.HTTPConnection(p_host, p_port, timeout=TIMEOUT_S)
            shown = f"[{host}]" if ":" in host else host
            target = f"http://{shown}:{port}{path}"
            send.update(p_headers)
    else:
        ip = _vetted_ip(host, port)
        conn = (_PinnedHTTPS(host, port, ip, TIMEOUT_S, ctx) if scheme == "https"
                else _PinnedHTTP(host, port, ip, TIMEOUT_S))
    deadline = time.monotonic() + TIMEOUT_S
    try:
        conn.request(method, target if proxy else path, body=body, headers=send)
        resp = conn.getresponse()
        got = bytearray()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout()
            if conn.sock is not None:
                conn.sock.settimeout(left)
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            got += chunk
            if len(got) > MAX_ANSWER:
                raise Refused("Node-RED's answer was bigger than 1 MB, so it was not read.")
        return resp.status, resp.getheader("Content-Type") or "", bytes(got), bool(proxy)
    except Refused:
        raise
    except (socket.timeout, TimeoutError):
        raise Refused(f"Node-RED at {base} did not answer within {TIMEOUT_S:g} seconds.") from None
    except ssl.SSLError:
        raise Refused(f"The HTTPS certificate at {base} did not check out.") from None
    except ConnectionRefusedError:
        raise Refused(f"Nothing answered at {base}. Is Node-RED running?") from None
    except OSError as exc:
        if proxy and "Tunnel connection failed" in str(exc):
            raise Refused("The Bridge did not let this through. A Node-RED on another computer needs the owner's Yes: "
                          "allow this address and port for this package.") from None
        raise Refused(f"Node-RED at {base} could not be reached ({type(exc).__name__}).") from None
    except http.client.HTTPException as exc:
        raise Refused(f"Node-RED at {base} gave an answer this package could not read ({type(exc).__name__}).") \
            from None
    finally:
        conn.close()

# ---------------------------------------------------------------- asking the Core for a Yes card (D110)

ASK_PATH = "/v1/bridge/addresses/ask"
ASK_TIMEOUT_S = 5                  # the whole ask: the connect and every read together
ASK_MOST = 16 * 1024               # the most it reads of the Core's reply
LINE_MOST = 300                    # the most of the Core's line it says back
ASK_STATUS = {"allowed": 200, "not_needed": 200, "waiting": 202}
NO_CARD = "No Yes card could be raised right now. The owner can allow this address and port on the Bridge page."
ON_THIS_COMPUTER = "It is on this computer, so it needs no Bridge step."


class _DeadlineSocket(socket.socket):
    """Each read gets only what is left of one deadline, so a slow drip can't hold the reply."""
    deadline = 0.0

    def recv_into(self, buffer, *args):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the Core took too long")
        self.settimeout(left)
        return super().recv_into(buffer, *args)


class _AskConnection(http.client.HTTPConnection):
    """One plain connection to the Core on this computer: never the Bridge proxy, and no redirect is followed."""

    def connect(self):
        deadline = time.monotonic() + ASK_TIMEOUT_S
        plain = socket.create_connection((self.host, self.port), ASK_TIMEOUT_S)
        self.sock = _DeadlineSocket(fileno=plain.detach())
        self.sock.deadline = deadline
        self.sock.settimeout(max(deadline - time.monotonic(), 0.001))


def _ask_core(place: str) -> tuple[str, str] | None:
    """Ask the Core for a Yes card for one ``host:port``: ``(state, its line)``, or None when no answer could be had.
    It grants nothing and never raises, so a failed ask never touches what was saved."""
    token = os.environ.get("PUFFPAI_TOKEN", "").strip()
    conn = None
    try:
        api = urlsplit(os.environ.get("PUFFPAI_API", "").strip())
        if api.scheme != "http" or not api.hostname or not token:
            return None
        conn = _AskConnection(api.hostname, api.port or 80)
        conn.request("POST", ASK_PATH, body=json.dumps({"address": place}).encode("utf-8"),
                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                              "Accept": "application/json"})
        resp = conn.getresponse()
        code, raw = resp.status, resp.read(ASK_MOST + 1)
        said = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - no Core, no answer or no sense: the same plain words, and the save stands
        return None
    finally:
        if conn is not None:
            conn.close()
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
        return f"{line} The owner allowed {place}, and it works while the Bridge is open and this package is on, unless a block list stops it."
    if state == "waiting":
        if line.lower().startswith("waiting"):
            return f"{line} A Yes card for {place} is waiting for the owner. Nothing reaches it until her Yes."
        return f"{line} Nothing reaches {place} for now."       # the Core's "waiting" with no card: its line says why
    return f"{line} No card was raised." if state == "refused" else line


PORT_PAIR = "Port 443 goes with https and port 80 with http, so the Bridge can't allow this pairing. Nothing was saved."


def _pair_refused(scheme: str, port: int) -> bool:
    """The Bridge takes only https on port 443 and only http on port 80, so a Yes for the other way round never works."""
    return (scheme, port) in (("http", 443), ("https", 80))


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


class Store:
    """One JSON document: the Node-RED address and its stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return {"address": "", "last_updated": ""}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({type(exc).__name__}). "
                                 "This package will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("address", ""), str):
                raise Unreadable(f"{self.path} is not a Node-RED address file. This package will not write over it.")
            return {"address": data.get("address", ""), "last_updated": str(data.get("last_updated", ""))}

    def save(self, address: str) -> str:
        with self.lock:
            self.load()                      # refuses to write over a file that will not read
            data = {"address": address, "last_updated": _now()}
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".nodered-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class NodeRed:
    def __init__(self, store: Store) -> None:
        self.store = store

    # helpers
    def address(self) -> str:
        saved = self.store.load()["address"]
        return saved or DEFAULT_ADDRESS

    def _admin(self, method: str, path: str, body=None) -> tuple[int, object]:
        """One admin API call (the address's own path is the admin root). Raises Refused on a login refusal."""
        base = self.address()
        headers = {"Node-RED-API-Version": "v1"}
        secret = os.environ.get(KEY_ENV, "")
        if secret and not _CONTROL.search(secret) and "\n" not in secret:
            headers["Authorization"] = "Bearer " + secret
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, _, answer, proxied = fetch(base, method, urlsplit(base).path + path, raw, headers)
        if proxied and status in (403, 407, 502):
            raise Refused(f"Refused on the way to {base} (HTTP {status}). Either the Bridge has not allowed this "
                          "address and port for this package (allow them on the Bridge page), "
                          f"or Node-RED did not take {KEY_ENV}.")
        if status in (401, 403):
            raise Refused(f"Node-RED at {base} asked for a login. " + (
                f"It did not take {KEY_ENV}." if secret else
                f"Add {KEY_ENV} for this package if your Node-RED uses adminAuth."))
        if 300 <= status < 400:
            raise Refused(f"Node-RED at {base} answered with a redirect (HTTP {status}). "
                          "This package does not follow redirects; check the address.")
        return status, _json_or_none(answer)

    def _nodes(self) -> list[dict]:
        status, data = self._admin("GET", "/flows")
        if isinstance(data, dict) and isinstance(data.get("flows"), list):
            data = data["flows"]
        if status != 200 or not isinstance(data, list):
            raise Refused(f"Node-RED did not give its flow list (HTTP {status}).")
        return [n for n in data if isinstance(n, dict)]

    def _tabs(self) -> list[dict]:
        nodes = self._nodes()
        tabs = [{"id": str(n.get("id", "")), "label": _shown(n.get("label") or n.get("id"), 80),
                 "disabled": bool(n.get("disabled")), "paths": []}
                for n in nodes if n.get("type") == "tab"]
        by_id = {t["id"]: t for t in tabs}
        for n in nodes:
            tab = by_id.get(str(n.get("z", "")))
            if n.get("type") == "http in" and tab is not None and isinstance(n.get("url"), str):
                url = n["url"] if n["url"].startswith("/") else "/" + n["url"]
                tab["paths"].append({"method": str(n.get("method", "get")).lower(), "path": url,
                                     "disabled": bool(n.get("d"))})
        return tabs

    def _one_tab(self, flow) -> dict:
        flow = _text(flow, "flow", 80)
        if not flow:
            raise ValueError("Say which flow: its name or id, as list_flows shows.")
        tabs = self._tabs()
        hits = [t for t in tabs if t["id"] == flow] or [t for t in tabs if t["label"].lower() == flow.lower()]
        if not hits:
            raise ValueError(f"There is no flow named {_shown(flow, 80)!r}. Ask for list_flows to see them.")
        if len(hits) > 1:
            raise ValueError("More than one flow has that name. Use its id: " + ", ".join(t["id"] for t in hits))
        if not _FLOW_ID_RE.match(hits[0]["id"]):
            raise Refused("That flow's id has characters this package will not put in a web address.")
        return hits[0]

    # tools
    def set_address(self, address: str) -> str:
        address = check_address(address)
        parts = urlsplit(address)
        if host_kind(parts.hostname or "") == "name":
            try:
                _vetted_ip(parts.hostname, parts.port or DEFAULT_PORT)   # the same check every call makes
            except Refused as exc:
                raise Refused(f"{exc} Nothing was saved.") from None
        if host_kind(parts.hostname or "") != "loopback" and _pair_refused(parts.scheme, parts.port or DEFAULT_PORT):
            raise ValueError(PORT_PAIR)
        self.store.save(address)
        host, port = parts.hostname or "", parts.port or DEFAULT_PORT
        words = ON_THIS_COMPUTER if host_kind(host) == "loopback" else _card_words(host, port)
        return f"Saved. The Node-RED address is now {address}. {words} Ask for node_red_status to check it answers."

    def node_red_status(self) -> str:
        base = self.address()
        try:
            status, data = self._admin("GET", "/settings")
        except Refused as exc:
            return f"Node-RED is not reachable: {exc}"
        if status == 200 and isinstance(data, dict):
            version = _shown(data.get("version") or "", 40)
            return f"Node-RED {version + ' ' if version else ''}is running at {base}."
        return f"Node-RED is not reachable at {base}: it answered HTTP {status}."

    def list_flows(self) -> str:
        tabs = self._tabs()
        if not tabs:
            return f"Node-RED at {self.address()} has no flows yet. Import flows/homebot-flows.json to add some."
        lines = []
        for t in tabs[:100]:
            paths = ", ".join(f"{p['method'].upper()} {_shown(p['path'], 120)}{' (node off)' if p['disabled'] else ''}"
                              for p in t["paths"][:10]) or "no web paths"
            lines.append(f"- {t['label']} (id {_shown(t['id'], 64)}, {'off' if t['disabled'] else 'on'}): {paths}")
        more = f"\n...and {len(tabs) - 100} more." if len(tabs) > 100 else ""
        return f"Node-RED at {self.address()} has {len(tabs)} flow{'s' if len(tabs) != 1 else ''}:\n" \
               + "\n".join(lines) + more

    def run_flow(self, path: str, method: str = "", body=None) -> str:
        path = _text(path, "path", 200)
        if not path.startswith("/") or _CONTROL.search(path) or " " in path or "?" in path or "#" in path:
            raise ValueError("path is one of the flows' own web paths, like /webhook/daily-news, as list_flows shows.")
        method = _text(method, "method", 8).lower()
        if method not in ("", "get", "post"):
            raise ValueError("method is GET or POST.")
        if isinstance(body, str) and body.strip():
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                raise ValueError("body is a small JSON object, like {\"lat\": 40.7, \"lon\": -74}.") from None
        if body in ("", None):
            body = None
        elif not isinstance(body, dict):
            raise ValueError("body is a small JSON object, like {\"lat\": 40.7, \"lon\": -74}.")
        raw = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        if raw is not None and len(raw) > MAX_SEND:
            raise ValueError(f"body is at most {MAX_SEND // 1024} KB.")
        found = [(t, p) for t in self._tabs() for p in t["paths"] if p["path"] == path]
        if not found:
            raise Refused(f"{_shown(path, 120)} is not one of the web paths your Node-RED flows define, so it was "
                          "not called. Ask for list_flows to see them.")
        usable = [(t, p) for t, p in found if p["method"] in ("get", "post") and (not method or p["method"] == method)]
        if not usable:
            raise Refused(f"{_shown(path, 120)} takes {', '.join(sorted({p['method'].upper() for _, p in found}))}; "
                          "this package calls only GET and POST paths.")
        on = [(t, p) for t, p in usable if not t["disabled"] and not p["disabled"]]
        if not on:
            raise Refused(f"The flow {usable[0][0]['label']!r} is turned off. Turn it on first (enable_flow).")
        tab, p = on[0]
        verb = p["method"].upper()
        if verb == "GET" and raw is not None:
            raise ValueError("A GET path takes no body.")
        if verb == "POST" and raw is None:
            raw = b"{}"
        base = self.address()
        root = "/"
        try:
            status, settings = self._admin("GET", "/settings")
            if status == 200 and isinstance(settings, dict) and isinstance(settings.get("httpNodeRoot"), str) \
                    and _PATH_RE.match(settings["httpNodeRoot"]) and ".." not in settings["httpNodeRoot"]:
                root = settings["httpNodeRoot"]
        except Refused:
            pass                              # a locked admin API still serves its flows at the default root
        parts = urlsplit(base)
        origin = f"{parts.scheme}://{parts.netloc}"
        target = root.rstrip("/") + quote(path, safe="/-_.~:")
        headers = {"Content-Type": "application/json"} if verb == "POST" else {}
        status, ctype, answer, _ = fetch(origin, verb, target, raw, headers)   # no admin login sent to a flow
        if 300 <= status < 400:
            return f"{tab['label']}: Node-RED answered with a redirect (HTTP {status}), which this package does not follow."
        data = _json_or_none(answer) if "json" in ctype.lower() or answer[:1] in (b"{", b"[") else None
        if data is not None:
            text = json.dumps(data, ensure_ascii=False, indent=1)
        else:
            text = answer.decode("utf-8", errors="replace")
        text = _scrub(_CONTROL.sub(" ", text))
        if len(text) > SHOW:
            text = text[:SHOW] + f"\n...(cut at {SHOW} characters)"
        return f"Ran {tab['label']} ({verb} {path}): HTTP {status}.\n{text}".rstrip()

    def _set_disabled(self, flow, disabled: bool) -> str:
        tab = self._one_tab(flow)
        word = "off" if disabled else "on"
        if tab["disabled"] == disabled:
            return f"The flow {tab['label']!r} is already {word}."
        status, whole = self._admin("GET", f"/flow/{tab['id']}")
        if status != 200 or not isinstance(whole, dict) or str(whole.get("id")) != tab["id"]:
            raise Refused(f"Node-RED did not give the flow {tab['label']!r} (HTTP {status}).")
        whole["disabled"] = disabled
        status, _ = self._admin("PUT", f"/flow/{tab['id']}", whole)
        if status not in (200, 204):
            raise Refused(f"Node-RED did not save the change (HTTP {status}).")
        return f"Turned the flow {tab['label']!r} {word}."

    def enable_flow(self, flow: str) -> str:
        return self._set_disabled(flow, False)

    def disable_flow(self, flow: str) -> str:
        return self._set_disabled(flow, True)


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("list_flows", "List the flows in your Node-RED: each flow's name, id, whether it is on or off, and the web "
     "paths it answers.", {}, []),
    ("run_flow", "Run one Node-RED flow by calling one of its own web paths (GET or POST), with an optional small "
     "JSON body. Only paths your flows define are called.",
     {"path": _p("string", "The flow's web path, like /webhook/daily-news, as list_flows shows."),
      "method": _p("string", "GET or POST. Default: the method the path takes."),
      "body": _p("object", "Optional small JSON object to send, like {\"lat\": 40.7, \"lon\": -74}.")}, ["path"]),
    ("enable_flow", "Turn one Node-RED flow on, by its name or id.",
     {"flow": _p("string", "The flow's name or id, as list_flows shows.")}, ["flow"]),
    ("disable_flow", "Turn one Node-RED flow off, by its name or id.",
     {"flow": _p("string", "The flow's name or id, as list_flows shows.")}, ["flow"]),
    ("node_red_status", "Say whether Node-RED is running at the saved address, and its version.", {}, []),
    ("set_address", "Save the address of your Node-RED (on this computer or your home network), like "
     "http://192.168.1.20:1880. Port 1880 if none is given. For an address off this computer, the bot raises a Yes card for that address and port.",
     {"address": _p("string", "The Node-RED address, like http://127.0.0.1:1880 or nodered.local.")}, ["address"]),
]
CHANGES = ["run_flow", "enable_flow", "disable_flow", "set_address"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: NodeRed, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

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
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        if not isinstance(args, dict):
            return said(f"The arguments for {name} are a JSON object.", True)
        try:
            return said(getattr(app, name)(**args), False)
        except (Unreadable, Refused) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"This package does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "NodeRedFlows/1"
    app: NodeRed
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
        self._json(401, {"error": "This package needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Stopped on this request with an error ({type(exc).__name__})."})
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
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")
        self._json(404, {"error": "Nothing here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Nothing here."})
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
    app = NodeRed(Store(data))
    handler = type("NodeRedHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Node-RED flows for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts this package with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
