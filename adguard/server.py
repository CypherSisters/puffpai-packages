# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/zone3/adguard_controller.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:scripts/zone3_bootstrap_adguard.py (rewritten-from)
"""AdGuard Home: read and change the owner's own AdGuard Home, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN.

It talks to one place only: the AdGuard Home at the address the owner saves with ``set_address``.
That address must be on this computer or the home network: an http(s) URL whose host is a loopback,
RFC 1918 or link-local address, ``localhost``, or a name ending in ``.local`` or ``.lan``. The name
is looked up at every call and every answer must be such an address. An address on this computer
is reached directly, at the address that was checked. Any other one goes through the Bridge proxy
when the Core has set one, with the name as saved, so the Bridge decides and checks the answers when
it connects. Redirects are refused.

The login (ADGUARD_USER, ADGUARD_PASSWORD) comes from the environment only (the Core puts it there
from the vault), is sent as HTTP Basic only to the saved address, and is never written to disk or
put in an answer. HomeBot's zone 3 proposed changes and the owner applied them; here each change is
a tool in ``abilities.changes``, so the Core can ask the owner for a Yes first.

Rules this package adds are marked with a comment line ``# puffpai`` just before each one (a
trailing comment would make the rule inert, as HomeBot found). It never removes a rule it did not
add. The saved address is one JSON document in PUFFPAI_DATA (``adguard.json``), written whole.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "adguard"
VERSION = "1.0.0"
DOC = "adguard.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_ANSWER = 2 * 1024 * 1024
TIMEOUT_S = 8
PROBE_TIMEOUT_S = 3
MARK = "# puffpai"
KEY_USER, KEY_PASSWORD = "ADGUARD_USER", "ADGUARD_PASSWORD"
USER_AGENT = "PuffPAI-AdGuard/1.0"
LOCAL_SUFFIXES = (".local", ".lan")
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
LINK_LOCAL = [ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fe80::/10")]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- checked input

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def _number(value, name: str, lo: float, hi: float):
    """A whole number from *lo* to *hi*, or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi or x != int(x):
        raise ValueError(f"{name} is a whole number from {lo:g} to {hi:g}")
    return int(x)


def _text(value, name: str, most: int, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    if _CONTROL.search(value):
        raise ValueError(f"{name} has hidden or control characters")
    return value or default


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this package takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


def domain_of(value) -> str:
    """A plain domain name (``ads.example.com``) from words, a URL or ``*.name``; ValueError otherwise."""
    text = _text(value, "domain", 300).lower()
    if "://" in text:
        text = urllib.parse.urlsplit(text).hostname or ""
    text = text.removeprefix("*.").removeprefix("||").removesuffix("^").rstrip(".")
    try:
        text = text.encode("idna").decode("ascii") if not text.isascii() else text
    except UnicodeError:
        raise ValueError("that is not a domain name") from None
    labels = text.split(".")
    if len(text) > 253 or len(labels) < 2 or not all(LABEL.match(x) for x in labels):
        raise ValueError(f"{value!r} is not a domain name like ads.example.com")
    return text


def _ip_ok(ip: ipaddress._BaseAddress) -> bool:
    """Loopback, RFC 1918 or link-local. Nothing else, not even other private ranges."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in n for n in RFC1918 + LINK_LOCAL if n.version == ip.version)


def _name_ok(host: str) -> bool:
    if host == "localhost":
        return True
    labels = host.split(".")
    return host.endswith(LOCAL_SUFFIXES) and len(host) <= 253 and all(LABEL.match(x) for x in labels)


def check_address(value) -> tuple[str, str, int | None]:
    """``(scheme, host, port or None)`` for an AdGuard Home address the owner gave, or a plain ValueError."""
    text = _text(value, "address", 200)
    if not text:
        raise ValueError("give the AdGuard Home address, like http://192.168.1.2:3000")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("that address does not read as a web address") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        raise ValueError("the address starts with http:// or https://")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("leave the login out of the address; it comes from the vault")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("give only the address and port, with no path after it")
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        ip = None
    if not (_ip_ok(ip) if ip is not None else _name_ok(host)):
        raise ValueError(f"{host or 'that'} is not on this computer or the home network. AdGuard Home must be at "
                         "a loopback, 192.168.x.x, 10.x.x.x, 172.16-31.x.x or 169.254.x.x address, localhost, "
                         "or a name ending in .local or .lan")
    if port == 0:
        raise ValueError("port 0 is not a port")
    return parts.scheme, host, port


def base_url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"


def resolve_checked(host: str, port: int) -> str:
    """The one address to connect to: every answer for *host* must pass the same rule, or none is used."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise Problem(f"{host} could not be found on the home network.") from None
    addrs = [info[4][0] for info in infos]
    if not addrs or not all(_ip_ok(ipaddress.ip_address(a.split("%", 1)[0])) for a in addrs):
        raise Problem(f"{host} points somewhere outside this computer and the home network, so nothing was sent.")
    return addrs[0]


def refuse_named_outside(host: str) -> None:
    """For a name (``adguard.lan``): if it is found and any answer is outside this computer and the home
    network, a plain ValueError and nothing is saved. A name that is not found yet is let through; the
    call-time check (:func:`resolve_checked`) still holds for every request."""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return                                  # a number was already checked by check_address
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return
    addrs = [info[4][0] for info in infos]
    if not all(_ip_ok(ipaddress.ip_address(a.split("%", 1)[0])) for a in addrs):
        raise ValueError(f"{host} points somewhere outside this computer and the home network, so it was not "
                         "saved. Give AdGuard Home's address on the home network instead")


# ---------------------------------------------------------------- talking to AdGuard Home

class Problem(RuntimeError):
    """AdGuard Home could not be asked, or did not answer usefully. Plain words, never the login."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Problem("AdGuard Home sent the request on to another page, so nothing more was sent. If it is not "
                      "set up yet, finish its first-run setup in a browser.")


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, *args, pin: str, **kw):
        super().__init__(*args, **kw)
        self._pin = pin

    def connect(self):
        self.sock = socket.create_connection((self._pin, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, *args, pin: str, **kw):
        super().__init__(*args, **kw)
        self._pin = pin

    def connect(self):
        sock = socket.create_connection((self._pin, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinHTTP(urllib.request.HTTPHandler):
    def __init__(self, pin: str):
        super().__init__()
        self.pin = pin

    def http_open(self, req):
        return self.do_open(functools.partial(_PinnedHTTP, pin=self.pin), req)


class _PinHTTPS(urllib.request.HTTPSHandler):
    def __init__(self, pin: str):
        super().__init__(context=ssl.create_default_context())
        self.pin = pin

    def https_open(self, req):
        return self.do_open(functools.partial(_PinnedHTTPS, pin=self.pin), req, context=self._context)


def _opener(scheme: str, host: str, port: int):
    """``(opener, through_bridge)``. On this computer: direct, to the checked address. Elsewhere: through the
    proxy the Core set (the Bridge), or direct to the checked address when there is none."""
    pin = resolve_checked(host, port)
    proxies = urllib.request.getproxies_environment()
    loopback = ipaddress.ip_address(pin.split("%", 1)[0]).is_loopback
    if not loopback and proxies.get(scheme) and not urllib.request.proxy_bypass_environment(host, proxies):
        return urllib.request.build_opener(urllib.request.ProxyHandler({scheme: proxies[scheme]}), _NoRedirect()), True
    pinned = _PinHTTPS(pin) if scheme == "https" else _PinHTTP(pin)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), pinned), False


def adguard_call(base: str, user: str, password: str, method: str, path: str, body=None, timeout=TIMEOUT_S):
    """One call to ``<base>/control<path>`` with the owner's login. JSON back (or ``{}``), or Problem."""
    scheme, host, port = check_address(base)
    opener, bridged = _opener(scheme, host, port or (443 if scheme == "https" else 80))
    login = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {login}", "Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + "/control" + path, data=data, method=method, headers=headers)
    where = f"AdGuard Home at {base}"
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(MAX_ANSWER + 1)
    except Problem:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or (exc.code == 403 and not bridged):
            raise Problem(f"{where} did not accept the login. Check {KEY_USER} and {KEY_PASSWORD} in the vault.") from None
        if exc.code == 404:
            raise Problem(f"{where} does not know that request. It may be an older AdGuard Home; update it.") from None
        hint = (" If the Bridge holds it, allow that address and port for this package on the Bridge page."
                if bridged else "")
        raise Problem(f"{where} answered with error {exc.code}.{hint}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        hint = " The Bridge may not allow that address yet." if bridged else ""
        raise Problem(f"Could not reach {where} ({reason}). Is it running?{hint}") from None
    if len(raw) > MAX_ANSWER:
        raise Problem(f"{where} sent more than 2 MB, so it was not read.")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text or text == "OK":
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Problem(f"{where} sent something that is not AdGuard Home data.") from None

# ---------------------------------------------------------------- asking the Core for a Yes card (D110)

ASK_PATH = "/v1/bridge/addresses/ask"
ASK_TIMEOUT_S = 5
ASK_MOST = 16 * 1024               # the most it reads of the Core's reply
LINE_MOST = 300                    # the most of the Core's line it says back
ASK_STATUS = {"allowed": 200, "not_needed": 200, "waiting": 202}
NO_CARD = "No Yes card could be raised right now. The owner can allow this address and port on the Bridge page."
ON_THIS_COMPUTER = "It is on this computer, so it needs no Bridge step."


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


def _this_computer(host: str, port: int) -> bool:
    """The address reached directly, as :func:`_opener` picks it: no Bridge step, and no ask."""
    try:
        return ipaddress.ip_address(resolve_checked(host, port).split("%", 1)[0]).is_loopback
    except Problem:
        return False


def _after_saving(host: str, port: int) -> str:
    return ON_THIS_COMPUTER if _this_computer(host, port) else _card_words(host, port)


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"address": "", "last_updated": ""}


class Store:
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
                raise Unreadable(f"{self.path} will not read ({exc}). AdGuard Home will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("address", ""), str):
                raise Unreadable(f"{self.path} is not this package's settings. It will not write over it.")
            out = default_doc()
            out.update({k: data[k] for k in ("address", "last_updated") if k in data})
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data = {"address": data.get("address", ""), "last_updated": _now()}
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".adguard-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ---------------------------------------------------------------- the logic

def _ours(rules: list[str], rule: str) -> list[int]:
    """Indexes of *rule* lines this package added (the line just before is exactly ``# puffpai``)."""
    return [i for i, r in enumerate(rules) if r.strip() == rule and i > 0 and rules[i - 1].strip() == MARK]


def _without(rules: list[str], indexes: list[int]) -> list[str]:
    drop = set(indexes) | {i - 1 for i in indexes}
    return [r for i, r in enumerate(rules) if i not in drop]


def _pairs(items, most: int) -> list[tuple[str, int]]:
    """AdGuard's top lists (``[{name: count}, ...]``) as ``(name, count)``."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            for k, v in item.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.append((str(k)[:120], int(v)))
    return out[:most]


class AdGuard:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.rules_lock = threading.Lock()

    # plumbing
    def _login(self) -> tuple[str, str]:
        user, password = os.environ.get(KEY_USER, ""), os.environ.get(KEY_PASSWORD, "")
        if not user or not password:
            raise Problem(f"The AdGuard Home login is not set up. Put {KEY_USER} and {KEY_PASSWORD} in the vault "
                          "(Setup steps for this package), then start it again.")
        return user, password

    def _base(self) -> str:
        base = self.store.load()["address"]
        if not base:
            raise Problem("No AdGuard Home address yet. Tell me its address, like: set my AdGuard address to "
                          "http://192.168.1.2:3000")
        return base

    def _get(self, path: str):
        got = adguard_call(self._base(), *self._login(), "GET", path)
        if not isinstance(got, dict):
            raise Problem("AdGuard Home sent something that is not AdGuard Home data.")
        return got

    def _post(self, path: str, body: dict) -> None:
        adguard_call(self._base(), *self._login(), "POST", path, body)

    def scrub(self, text: str) -> str:
        """Belt and braces: an answer never carries the password or the Basic login, whatever AdGuard echoed."""
        password = os.environ.get(KEY_PASSWORD, "")
        if len(password) >= 4:
            text = text.replace(password, "[hidden]")
            login = base64.b64encode(f"{os.environ.get(KEY_USER, '')}:{password}".encode()).decode()
            text = text.replace(login, "[hidden]")
        return text

    # tools
    def set_address(self, address: str) -> str:
        scheme, host, port = check_address(address)
        refuse_named_outside(host)
        tries = [port] if port else ([443] if scheme == "https" else [3000, 80])
        heard = ""
        for p in tries:
            base = base_url(scheme, host, p)
            try:
                version = adguard_call(base, *self._login(), "GET", "/status", timeout=PROBE_TIMEOUT_S).get("version")
            except Problem as exc:
                heard = heard or str(exc)
                continue
            with self.store.lock:
                self.store.save({"address": base})
            said = f"Saved. AdGuard Home {version or ''} answered at {base}.".replace("  ", " ")
            return f"{said} {_after_saving(host, p)}"
        base = base_url(scheme, host, tries[0])
        with self.store.lock:
            self.store.save({"address": base})
        return f"Saved {base}. Not checked yet: {heard} {_after_saving(host, tries[0])}"

    def status(self) -> str:
        """From zone3_status (rebot_tools.py:2286)."""
        st = self._get("/status")
        stats = self._get("/stats")
        filt = self._get("/filtering/status")
        if st.get("protection_enabled"):
            prot = "protection is ON"
        elif st.get("protection_disabled_until"):
            prot = f"protection is PAUSED until {str(st['protection_disabled_until'])[:19].replace('T', ' ')}"
        else:
            prot = "protection is OFF"
        queries = int(stats.get("num_dns_queries") or 0)
        blocked = int(stats.get("num_blocked_filtering") or 0)
        share = f" ({blocked * 100 // queries}%)" if queries else ""
        lists = [f for f in filt.get("filters") or [] if isinstance(f, dict)]
        on = [f for f in lists if f.get("enabled")]
        rules = [r for r in filt.get("user_rules") or [] if isinstance(r, str)]
        mine = sum(1 for i, r in enumerate(rules) if i > 0 and rules[i - 1].strip() == MARK and r.strip())
        own = sum(1 for r in rules if r.strip() and not r.lstrip().startswith(("#", "!"))) - mine
        return (f"AdGuard Home {st.get('version') or ''} at {self._base()}: {prot}.\n"
                f"In its stats period (24 hours unless you changed it): {queries} DNS lookups, {blocked} blocked{share}.\n"
                f"Filter lists: {len(on)} on of {len(lists)} "
                f"({sum(int(f.get('rules_count') or 0) for f in on)} rules). Your own rules: {own}; "
                f"added by PuffPAI: {mine}.")

    def top_blocked(self, count: int = 10) -> str:
        n = _number(count, "count", 1, 50)
        stats = self._get("/stats")
        doms = _pairs(stats.get("top_blocked_domains"), n)
        clients = _pairs(stats.get("top_clients"), n)
        lines = ["Most blocked domains:" if doms else "Nothing blocked in the stats period."]
        lines += [f"- {d}: {c}" for d, c in doms]
        if clients:
            lines += ["Busiest devices (all lookups):"] + [f"- {d}: {c}" for d, c in clients]
        return "\n".join(lines)

    def query_log(self, count: int = 20, client: str = "") -> str:
        n = _number(count, "count", 1, 100)
        who = _text(client, "client", 100)
        q = {"limit": n, "response_status": "all"}
        if who:
            q["search"] = who
        rows = self._get("/querylog?" + urllib.parse.urlencode(q)).get("data") or []
        out = []
        for row in rows[:n]:
            if not isinstance(row, dict):
                continue
            name = str((row.get("question") or {}).get("name") or "?")[:120]
            reason = str(row.get("reason") or "")
            verdict = "blocked" if reason.startswith("Filtered") and "Allow" not in reason else "allowed"
            when = str(row.get("time") or "")[11:19]
            out.append(f"- {when} {str(row.get('client') or '?')[:60]} {name} {verdict}")
        if not out:
            return "No lookups in the log" + (f" for {who}." if who else ".")
        return (f"Last {len(out)} lookups" + (f" for {who}" if who else "") + " (newest first):\n" + "\n".join(out))

    def list_clients(self) -> str:
        got = self._get("/clients")
        named = [c for c in got.get("clients") or [] if isinstance(c, dict)]
        auto = [c for c in got.get("auto_clients") or [] if isinstance(c, dict)]
        lines = [f"{_plural(len(named), 'named device')} and {len(auto)} found on their own."]
        lines += [f"- {str(c.get('name') or '?')[:60]}: {', '.join(str(i)[:60] for i in (c.get('ids') or [])[:5])}"
                  for c in named[:50]]
        lines += [f"- {str(c.get('ip') or '?')[:60]}" + (f" ({str(c['name'])[:60]})" if c.get("name") else "")
                  + (f", from {str(c['source'])[:20]}" if c.get("source") else "") for c in auto[:50]]
        return "\n".join(lines)

    def _rules(self) -> list[str]:
        return [r for r in self._get("/filtering/status").get("user_rules") or [] if isinstance(r, str)]

    def block_domain(self, domain: str) -> str:
        """From zone3_secure_device (rebot_tools.py:2378), as one rule behind the owner's Yes."""
        d = domain_of(domain)
        block, allow = f"||{d}^", f"@@||{d}^"
        with self.rules_lock:
            rules = self._rules()
            mine_allow = _ours(rules, allow)
            new = _without(rules, mine_allow)
            if any(r.strip() == block for r in new):
                if mine_allow:
                    self._post("/filtering/set_rules", {"rules": new})
                    return f"Took off the allow rule PuffPAI added for {d}; a rule already in the list blocks it."
                return f"{d} is already blocked by a rule in the list. Nothing changed."
            self._post("/filtering/set_rules", {"rules": new + [MARK, block]})
        return f"Blocked {d} (and its sub-domains) with the rule {block}, marked {MARK}."

    def unblock_domain(self, domain: str) -> str:
        d = domain_of(domain)
        block, allow = f"||{d}^", f"@@||{d}^"
        with self.rules_lock:
            rules = self._rules()
            mine = _ours(rules, block)
            new = _without(rules, mine)
            owners = any(r.strip() == block for r in new)
            if mine and not owners:
                self._post("/filtering/set_rules", {"rules": new})
                return f"Took off the rule PuffPAI added, so {d} is no longer blocked by it."
            if _ours(new, allow):
                if mine:
                    self._post("/filtering/set_rules", {"rules": new})
                return f"{d} already has an allow rule from PuffPAI."
            self._post("/filtering/set_rules", {"rules": new + [MARK, allow]})
        why = "your own rule that blocks it stays in the list" if owners else "a filter list may block it"
        return f"Added the allow rule {allow} for {d}, marked {MARK}; {why}, and the allow rule wins over it."

    def protection(self, on) -> str:
        """From zone3_enforce (rebot_tools.py:2489), applied after the owner's Yes instead of proposed."""
        if isinstance(on, str) and on.strip().lower() in ("on", "true", "yes", "enable", "1"):
            on = True
        elif isinstance(on, str) and on.strip().lower() in ("off", "false", "no", "disable", "0"):
            on = False
        if not isinstance(on, bool):
            raise ValueError("on is on or off")
        self._post("/protection", {"enabled": on})
        return ("AdGuard Home protection is ON: its filters block again." if on else
                "AdGuard Home protection is OFF: nothing is blocked until it is turned on again.")

    def pause_protection(self, minutes: int = 10) -> str:
        n = _number(minutes, "minutes", 1, 1440)
        self._post("/protection", {"enabled": False, "duration": n * 60_000})
        return f"AdGuard Home protection is paused for {_plural(n, 'minute')}; it turns itself back on after that."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("set_address", "Save where the owner's AdGuard Home is, like http://192.168.1.2:3000. Only an address on this "
     "computer or the home network is taken. Without a port it tries 3000, then 80. For an address off this computer, the bot raises a Yes card for that address and port.",
     {"address": _p("string", "The AdGuard Home web address.")}, ["address"]),
    ("status", "AdGuard Home's state: protection on or off, DNS lookups and blocked lookups in its stats period, "
     "and how many filter lists and rules it has.", {}, []),
    ("top_blocked", "The most blocked domains and the busiest devices, from AdGuard Home's stats.",
     {"count": _p("integer", "How many of each, 1 to 50. Default 10.")}, []),
    ("query_log", "The latest DNS lookups from AdGuard Home's log: which device asked for which domain, and whether "
     "it was blocked. Optional filter by device address or name.",
     {"count": _p("integer", "How many lookups, 1 to 100. Default 20."),
      "client": _p("string", "Optional: a device address or name to show only its lookups.")}, []),
    ("list_clients", "The devices AdGuard Home knows: the ones named in it and the ones it found on its own.", {}, []),
    ("block_domain", "Block a domain and its sub-domains in AdGuard Home by adding the rule ||domain^.",
     {"domain": _p("string", "The domain, like ads.example.com.")}, ["domain"]),
    ("unblock_domain", "Unblock a domain: take off the rule PuffPAI added, or add an allow rule @@||domain^.",
     {"domain": _p("string", "The domain, like example.com.")}, ["domain"]),
    ("protection", "Turn AdGuard Home's protection on or off for the whole home network.",
     {"on": _p("string", "on or off.")}, ["on"]),
    ("pause_protection", "Turn AdGuard Home's protection off for some minutes; it comes back on by itself.",
     {"minutes": _p("integer", "Minutes, 1 to 1440. Default 10.")}, []),
]
CHANGES = ["set_address", "block_domain", "unblock_domain", "protection", "pause_protection"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "openWorldHint": False}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: AdGuard, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(text: str, error: bool) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": app.scrub(text)}], "isError": error}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": NAME, "version": VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        if not isinstance(args, dict):
            return ok(f"{name} takes named arguments.", True)
        try:
            return ok(getattr(app, name)(**args), False)
        except (Problem, Unreadable) as exc:
            return ok(str(exc), True)
        except (TypeError, ValueError) as exc:
            return ok(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"AdGuard Home does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AdGuardPkg/1"
    app: AdGuard
    token: str
    timeout = 15

    def log_message(self, fmt, *args):
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
            return False
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
        self._json(401, {"error": "AdGuard Home needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            try:
                self._json(500, {"error": f"AdGuard Home stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self.path.split("?", 1)[0]
        if path == "/mcp":
            return self._send(405, b"", "text/plain")
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "AdGuard Home has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "AdGuard Home has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if not self._refused():
            self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if not self._refused():
            self._send(405, b"", "text/plain")


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = AdGuard(Store(data))
    handler = type("AdGuardHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AdGuard Home for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts AdGuard Home with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
