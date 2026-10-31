# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/ha_bridge.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/capability_registry.py (rewritten-from)
"""Home Assistant: read and control the owner's own Home Assistant, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

It talks to one Home Assistant only: the address the owner sets with ``set_address``, which must be
on the home network (loopback, 10/8, 172.16/12, 192.168/16, fc00::/7, link-local, or a name ending in .local or
.lan, which must also resolve to one of those). The Home Assistant token comes from the environment
(HOME_ASSISTANT_TOKEN, injected from the vault). It is never written to disk, never logged and never
put in an answer. Requests time out after 10 s, read at most 2 MB, and never follow a redirect to
another address. Locks, alarms, garage doors, gates and doors are never actuated from here.

The only file it keeps is ``ha.json`` in PUFFPAI_DATA: ``{url, last_updated}``. Writes replace the
whole file at once (temp file, then rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

NAME = "home-assistant"
VERSION = "1.0.0"
DOC = "ha.json"
TOKEN_ENV = "HOME_ASSISTANT_TOKEN"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_REPLY = 2 * 1024 * 1024        # the most it reads from Home Assistant
TIMEOUT_S = 10
DEFAULT_HA_PORT = 8123
LIST_CAP = 100

DOMAINS = ("light", "switch", "fan", "climate", "cover", "lock", "media_player", "scene", "sensor",
           "binary_sensor", "camera")
#: What turn_on / turn_off call in each domain they work on.
ON_OFF = {"light": ("turn_on", "turn_off"), "switch": ("turn_on", "turn_off"), "fan": ("turn_on", "turn_off"),
          "media_player": ("turn_on", "turn_off"), "cover": ("open_cover", "close_cover")}
#: Covers this package opens and closes. A cover with any other device class, or none, may be a garage
#: door, gate or door (HomeBot gated the whole domain for that reason), so it is refused.
SAFE_COVERS = ("blind", "curtain", "shade", "awning")
#: A scene that touches any of these is not run from here (ha_bridge SAFETY_CRITICAL_DOMAINS).
NEVER_IN_SCENE = {"lock", "alarm_control_panel", "valve", "water_heater", "siren", "camera"}
MOTION_CLASSES = ("motion", "occupancy")
HVAC_MODES = ("off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only")
COLOR_TEMPS = {"warm": 2700, "neutral": 3500, "cool": 5000, "daylight": 6500}
#: The attributes device_state shows. A list, never "all": a camera's attributes hold an access token.
SHOW_ATTRS = ("device_class", "brightness", "color_temp_kelvin", "rgb_color", "current_temperature",
              "temperature", "target_temp_low", "target_temp_high", "hvac_action", "current_position",
              "percentage", "media_title", "media_artist", "volume_level", "battery_level")
LOCK_WORDS = ("Locks are read-only here: this package can tell you if a lock is locked, but it never locks "
              "or unlocks a door. Use Home Assistant itself for that.")

_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "169.254.0.0/16", "::1/128", "fe80::/10"))
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ENTITY = re.compile(r"^([a-z_][a-z0-9_]{0,63})\.([a-z0-9_]{1,200})$")
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,4096}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value.strip().rstrip("%").replace("°", "")) if isinstance(value, str) else float(value)
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
    return json.loads(text, parse_constant=_no_constant)


def _shown(value, most: int = 80) -> str:
    """Text from Home Assistant, made safe to show: one line, no control characters, cut short."""
    return _CONTROL.sub(" ", str(value))[:most]


# ---------------------------------------------------------------- the address

def _local_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOCAL_NETS)


def _local_name(host: str) -> bool:
    if host == "localhost":
        return True
    labels = host.split(".")
    return len(labels) >= 2 and labels[-1] in ("local", "lan") and all(_LABEL.match(x) for x in labels)


def _mapped(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.version == 6 and ip.ipv4_mapped is not None


def check_address(value) -> str:
    """The owner's Home Assistant base address, ``scheme://host:port``, or a plain ValueError.
    Only http(s) on the home network: loopback, RFC1918, fc00::/7, link-local, or a .local / .lan name."""
    text = _text(value, "address", 200)
    if not text:
        raise ValueError("give Home Assistant's address, like homeassistant.local:8123 or 192.168.1.20:8123")
    if _CONTROL.search(text) or " " in text:
        raise ValueError("an address has no spaces in it")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("the address starts with http:// or https://")
    if "@" in parts.netloc:
        raise ValueError("an address with a name or password in it is not taken; the token is the way in")
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port or DEFAULT_HA_PORT
    except ValueError:
        raise ValueError("the port is a number from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("the port is a number from 1 to 65535")
    if not host or not (_local_ip(host) or _local_name(host)):
        raise ValueError("this package only talks to a Home Assistant on your home network: an address like "
                         "192.168.x.x, 10.x.x.x, 172.16 to 172.31, fd00::1, 127.0.0.1, or a name ending in .local or .lan")
    if _mapped(host):
        raise ValueError("an address written with ::ffff: is not taken; write it plainly, like 127.0.0.1 or "
                         "192.168.1.20")
    netloc = f"[{host}]" if ":" in host else host
    return f"{scheme}://{netloc}:{port}"


def _lookup(host: str, port: int) -> bool:
    """True when *host* is a home-network address or a name that resolves only to those; False when the
    name does not resolve at all. A name that resolves to any address outside the home network is an HAError."""
    if _local_ip(host):
        return True
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos or not all(_local_ip(str(i[4][0])) for i in infos):
        raise HAError(f"{host} points outside your home network, so I will not send your token there.")
    return True


def _resolves_local(host: str, port: int) -> None:
    """A name must resolve only to home-network addresses, so the token never goes outside."""
    if not _lookup(host, port):
        raise HAError(f"I could not find {host} on this network. Check the address, or set its number "
                      "address instead (like 192.168.1.20:8123).")

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


def _this_computer(host: str) -> bool:
    """``localhost`` or a loopback address: reached directly, so no Bridge step and no ask."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _only_this_computer(host: str, port: int) -> bool:
    """Every answer for ``localhost`` is loopback, so the direct path never carries a home or public address."""
    try:
        return all(ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]).is_loopback
                   for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except (OSError, ValueError):
        return False


PORT_PAIR = "Port 443 goes with https and port 80 with http, so the Bridge can't allow this pairing. Nothing was saved."


def _pair_refused(scheme: str, port: int) -> bool:
    """The Bridge takes only https on port 443 and only http on port 80, so a Yes for the other way round never works."""
    return (scheme, port) in (("http", 443), ("https", 80))


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


class HAError(RuntimeError):
    """A plain answer for the owner: Home Assistant could not be asked, or said no."""


class NotFound(HAError):
    pass


def default_doc() -> dict:
    return {"url": "", "last_updated": ""}


class Store:
    """One JSON document: the Home Assistant address. Never the token."""

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
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise Unreadable(f"{DOC} in this package's data folder will not read. "
                                 "Home Assistant will not write over it.") from None
            if not isinstance(data, dict):
                raise Unreadable(f"{DOC} is not this package's file. Home Assistant will not write over it.")
            out = default_doc()
            out.update({k: data[k] for k in ("url", "last_updated") if isinstance(data.get(k), str)})
            return out

    def save(self, url: str) -> str:
        with self.lock:
            self.load()                      # an unreadable file is never written over
            doc = {"url": url, "last_updated": _now()}
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".ha-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return doc["last_updated"]


# ---------------------------------------------------------------- talking to Home Assistant

class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same scheme, host and port. The token never goes anywhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise HAError("Home Assistant answered with a move to another address. I did not follow it.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _ha_token() -> str:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise HAError("Home Assistant's token is not set. Make a long-lived access token in Home Assistant "
                      "(your profile, Security) and add it in this package's setup.")
    if not _TOKEN_SHAPE.match(tok):
        raise HAError("The Home Assistant token does not look right (it is one long line of letters, digits "
                      "and dots). Add it again in this package's setup.")
    return tok


def _scrub(text: str) -> str:
    """Belt and braces: the token never appears in an answer, whatever happened."""
    tok = os.environ.get(TOKEN_ENV, "").strip()
    return text.replace(tok, "[token]") if len(tok) >= 8 else text


def _attrs(s: dict) -> dict:
    return s.get("attributes") if isinstance(s.get("attributes"), dict) else {}


def _friendly(s: dict) -> str:
    return _shown(_attrs(s).get("friendly_name") or s.get("entity_id", ""))


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _when(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class HomeAssistant:
    def __init__(self, store: Store, opener=None) -> None:
        self.store = store
        self.opener = opener or urllib.request.build_opener(_SameOrigin())
        self.direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameOrigin())

    # plumbing
    def _base(self) -> str:
        url = self.store.load()["url"]
        if not url:
            raise HAError("Home Assistant's address is not set yet. Tell me it, like "
                          "homeassistant.local:8123 or 192.168.1.20:8123.")
        try:
            return check_address(url)
        except ValueError:
            raise HAError("The saved address is not one this package takes. Set it again.") from None

    def _call(self, method: str, path: str, body: dict | None = None):
        base = self._base()
        token = _ha_token()
        parts = urlsplit(base)
        host, port = parts.hostname or "", parts.port or DEFAULT_HA_PORT
        _resolves_local(host, port)
        direct = _this_computer(host)
        if host == "localhost" and not _only_this_computer(host, port):
            raise HAError("localhost does not point only at this computer here, so nothing was sent.")
        req = urllib.request.Request(
            base + path, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with (self.direct if direct else self.opener).open(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read(MAX_REPLY + 1)
        except HAError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401:
                raise HAError("Home Assistant refused the token. Make a new long-lived access token in your "
                              "Home Assistant profile and add it in this package's setup.") from None
            if code == 403:
                raise HAError("The answer was 'not allowed'. If the Bridge does not allow this address and port for "
                              "this package yet, nothing works until it does.") from None
            if code == 404:
                raise NotFound("Home Assistant has no such thing.") from None
            if code == 400:
                raise HAError("Home Assistant would not do that: it said the request did not fit that device.") from None
            raise HAError(f"Home Assistant answered with error {code}.") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise HAError(f"I could not reach Home Assistant at {base} ({_shown(reason, 120)}). If the Bridge does not "
                          "allow this address and port for this package yet, nothing works until it does.") from None
        if len(raw) > MAX_REPLY:
            raise HAError("Home Assistant's answer was bigger than 2 MB, so I stopped reading it.")
        try:
            return json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HAError(f"The answer from {base} was not Home Assistant's. Check the address.") from None

    def _states(self) -> list[dict]:
        got = self._call("GET", "/api/states")
        if not isinstance(got, list):
            raise HAError("Home Assistant's list of devices did not read.")
        return [s for s in got if isinstance(s, dict) and isinstance(s.get("entity_id"), str)
                and _ENTITY.match(s["entity_id"])]

    def _service(self, domain: str, service: str, data: dict) -> None:
        self._call("POST", f"/api/services/{domain}/{service}", data)

    def _resolve(self, target, domains, what: str = "device") -> dict:
        """One entity's state from an entity id (light.kitchen) or a name (kitchen light), in *domains*."""
        text = _text(target, what, 200)
        if not text:
            raise ValueError(f"say which {what}: its name or entity id, like light.kitchen")
        low = text.lower()
        m = _ENTITY.match(low)
        if m:
            if m.group(1) == "lock" and "lock" not in domains:
                raise HAError(LOCK_WORDS)
            if m.group(1) not in domains:
                raise ValueError(f"{low} is a {m.group(1)}; this works on {', '.join(domains)}")
            try:
                got = self._call("GET", f"/api/states/{low}")
            except NotFound:
                raise HAError(f"Home Assistant has no {low}.") from None
            if not isinstance(got, dict) or got.get("entity_id") != low:
                raise HAError(f"Home Assistant's answer for {low} did not read.")
            return got
        under = low.replace(" ", "_")
        pool = [s for s in self._states() if _domain(s["entity_id"]) in domains]
        exact = [s for s in pool if _friendly(s).lower() == low or s["entity_id"].split(".", 1)[1] == under]
        found = exact or [s for s in pool if low in _friendly(s).lower() or under in s["entity_id"]]
        if len(found) == 1:
            return found[0]
        if not found:
            raise HAError(f"I found no {what} called {_shown(text)} ({', '.join(domains)}). "
                          "Ask me to list devices to see the names.")
        names = "; ".join(f"{_friendly(s)} ({s['entity_id']})" for s in found[:10])
        raise HAError(f"More than one {what} fits {_shown(text)}: {names}. Say which one.")

    # tools
    def set_address(self, address) -> str:
        url = check_address(address)
        parts = urlsplit(url)
        found = _lookup(parts.hostname or "", parts.port or DEFAULT_HA_PORT)    # outside: refused, nothing saved
        if not _this_computer(parts.hostname or "") and _pair_refused(parts.scheme, parts.port or DEFAULT_HA_PORT):
            raise ValueError(PORT_PAIR)
        self.store.save(url)
        host = parts.hostname or ""
        said = (f"Saved. Home Assistant's address is {url}. "
                + (ON_THIS_COMPUTER if _this_computer(host) else _card_words(host, parts.port or DEFAULT_HA_PORT)))
        if not found:
            said += (f" I could not find {parts.hostname} on this network right now, so nothing will work until "
                     "it can be found. Check the name, or set its number address instead (like 192.168.1.20:8123).")
        return said

    def list_devices(self, domain="all", name="") -> str:
        domain = _text(domain, "domain", 20, "all").lower().replace(" ", "_")
        if domain.endswith("s") and domain[:-1] in DOMAINS:
            domain = domain[:-1]
        if domain != "all" and domain not in DOMAINS:
            raise ValueError(f"domain is all or one of {', '.join(DOMAINS)}")
        name = _text(name, "name", 80).lower()
        want = DOMAINS if domain == "all" else (domain,)
        rows = sorted((s for s in self._states() if _domain(s["entity_id"]) in want
                       and (not name or name in _friendly(s).lower() or name.replace(" ", "_") in s["entity_id"])),
                      key=lambda s: s["entity_id"])
        if not rows:
            kind = "devices" if domain == "all" else f"{domain} devices"
            return f"Home Assistant has no {kind}" + (f" matching {_shown(name)}." if name else ".")
        lines = []
        for s in rows[:LIST_CAP]:
            unit = _attrs(s).get("unit_of_measurement")
            lines.append(f"{_friendly(s)} ({s['entity_id']}): {_shown(s.get('state'), 40)}"
                         + (f" {_shown(unit, 12)}" if unit else ""))
        more = f" (showing the first {LIST_CAP})" if len(rows) > LIST_CAP else ""
        return f"Found {_plural(len(rows), 'device')}{more}:\n" + "\n".join(lines)

    def device_state(self, entity) -> str:
        s = self._resolve(entity, DOMAINS)
        a = _attrs(s)
        unit = a.get("unit_of_measurement")
        line = (f"{_friendly(s)} ({s['entity_id']}) is {_shown(s.get('state'), 40)}"
                + (f" {_shown(unit, 12)}" if unit else "") + ".")
        bits = []
        for key in SHOW_ATTRS:
            v = a.get(key)
            if v is None or v == "":
                continue
            if key == "brightness" and isinstance(v, (int, float)):
                v = f"{round(v * 100 / 255)}%"
            bits.append(f"{key.replace('_', ' ')}: {_shown(v, 60)}")
        if _domain(s["entity_id"]) == "lock":
            bits.append("read-only here")
        changed = _when(s.get("last_changed"))
        if changed:
            bits.append(f"last changed {changed.astimezone().strftime('%Y-%m-%d %H:%M')}")
        return line + (" " + "; ".join(bits) + "." if bits else "")

    def _on_off(self, entity, on: bool) -> str:
        if isinstance(entity, str) and entity.strip().lower().startswith("lock."):
            raise HAError(LOCK_WORDS)
        s = self._resolve(entity, tuple(ON_OFF))
        eid, dom = s["entity_id"], _domain(s["entity_id"])
        if dom == "cover" and _attrs(s).get("device_class") not in SAFE_COVERS:
            return (f"{_friendly(s)} is a cover that is not a blind, curtain, shade or awning (it may be a garage "
                    "door, gate or door), so this package does not open or close it. Use Home Assistant itself.")
        self._service(dom, ON_OFF[dom][0 if on else 1], {"entity_id": eid})
        verb = ("Opened" if on else "Closed") if dom == "cover" else ("Turned on" if on else "Turned off")
        return f"{verb} {_friendly(s)}."

    def turn_on(self, entity) -> str:
        return self._on_off(entity, True)

    def turn_off(self, entity) -> str:
        return self._on_off(entity, False)

    def set_light(self, entity, brightness=None, color=None, rgb=None, color_temp=None) -> str:
        data: dict = {}
        said = []
        if brightness is not None:
            data["brightness_pct"] = _number(brightness, "brightness", 0, 100, whole=True)
            said.append(f"{data['brightness_pct']}% brightness")
        if len([x for x in (color, rgb, color_temp) if x not in (None, "", [])]) > 1:
            raise ValueError("give one of color, rgb or color_temp, not more")
        if color not in (None, ""):
            name = _text(color, "color", 30).lower().replace(" ", "")
            if not (name.isascii() and name.isalpha()):
                raise ValueError("color is a colour name, like red or blue")
            data["color_name"] = name
            said.append(name)
        if rgb not in (None, "", []):
            parts = rgb.split(",") if isinstance(rgb, str) else rgb
            if not isinstance(parts, list) or len(parts) != 3:
                raise ValueError("rgb is three numbers from 0 to 255, like 255,120,0")
            data["rgb_color"] = [_number(p, "rgb", 0, 255, whole=True) for p in parts]
            said.append("colour " + ",".join(str(p) for p in data["rgb_color"]))
        if color_temp not in (None, ""):
            word = str(color_temp).strip().lower().replace(" white", "")
            word = word[:-1].strip() if word.endswith("k") and word[:-1].strip().isdigit() else word
            kelvin = COLOR_TEMPS.get(word)
            if kelvin is None and word.isalpha():
                raise ValueError(f"color_temp is {', '.join(COLOR_TEMPS)} or a kelvin number from 1500 to 10000")
            if kelvin is None:
                kelvin = _number(word, "color_temp", 1500, 10000, whole=True)
            data["color_temp_kelvin"] = kelvin
            said.append(f"{kelvin} K white")
        if not data:
            raise ValueError("give at least one of brightness, color, rgb or color_temp")
        s = self._resolve(entity, ("light",), "light")
        data["entity_id"] = s["entity_id"]
        self._service("light", "turn_on", data)
        return f"Set {_friendly(s)} to {', '.join(said)}."

    def set_temperature(self, entity, temperature, hvac_mode=None) -> str:
        temp = _number(temperature, "temperature", -50, 150)
        s = self._resolve(entity, ("climate",), "thermostat")
        a = _attrs(s)
        lo, hi = a.get("min_temp"), a.get("max_temp")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not lo <= temp <= hi:
            raise ValueError(f"{_friendly(s)} takes {lo:g} to {hi:g} degrees")
        data = {"entity_id": s["entity_id"], "temperature": temp}
        if hvac_mode not in (None, ""):
            mode = _text(hvac_mode, "hvac_mode", 20).lower().replace(" ", "_")
            modes = [m for m in (a.get("hvac_modes") if isinstance(a.get("hvac_modes"), list) else HVAC_MODES)
                     if m in HVAC_MODES]
            if mode not in modes:
                raise ValueError(f"hvac_mode for {_friendly(s)} is one of {', '.join(modes)}")
            data["hvac_mode"] = mode
        self._service("climate", "set_temperature", data)
        return (f"Set {_friendly(s)} to {temp:g} degrees"
                + (f" in {data['hvac_mode']} mode." if "hvac_mode" in data else "."))

    def run_scene(self, scene) -> str:
        s = self._resolve(scene, ("scene",), "scene")
        members = _attrs(s).get("entity_id")
        if not isinstance(members, list) or not members:
            return (f"I cannot see what the scene {_friendly(s)} changes, so I did not run it. "
                    "Run it from Home Assistant itself.")
        states = {x["entity_id"]: x for x in self._states()}
        for m in map(str, members):
            dom = _domain(m)
            if dom in NEVER_IN_SCENE or (dom == "cover" and _attrs(states.get(m, {})).get("device_class")
                                         not in SAFE_COVERS):
                return (f"The scene {_friendly(s)} changes {_shown(m)}, which may be a lock, alarm, door, gate, "
                        "camera or valve, so this package does not run it. Run it from Home Assistant itself.")
        self._service("scene", "turn_on", {"entity_id": s["entity_id"]})
        return f"Ran the scene {_friendly(s)}."

    def list_cameras(self) -> str:
        cams = sorted((s for s in self._states() if _domain(s["entity_id"]) == "camera"),
                      key=lambda s: s["entity_id"])
        if not cams:
            return "Home Assistant has no cameras."
        lines = [f"{_friendly(s)} ({s['entity_id']}): {_shown(s.get('state'), 40)}" for s in cams[:LIST_CAP]]
        return f"Found {_plural(len(cams), 'camera')}:\n" + "\n".join(lines)

    def recent_motion(self, minutes=30) -> str:
        minutes = _number(minutes, "minutes", 1, 1440, whole=True)
        sensors = [s for s in self._states() if _domain(s["entity_id"]) == "binary_sensor"
                   and _attrs(s).get("device_class") in MOTION_CLASSES]
        if not sensors:
            return "Home Assistant has no motion or occupancy sensors."
        now = datetime.now(timezone.utc)
        rows = []
        for s in sensors:
            t = _when(s.get("last_changed"))
            if t is None:
                continue
            ago = (now - t).total_seconds() / 60
            if -5 <= ago <= minutes:          # a few minutes of clock difference count as now
                rows.append((ago, f"{_friendly(s)}: {_shown(s.get('state'), 20)}, changed "
                                  f"{_plural(max(0, round(ago)), 'minute')} ago"))
        if not rows:
            return (f"No motion or occupancy sensor changed in the last {_plural(minutes, 'minute')} "
                    f"({_plural(len(sensors), 'sensor')} checked).")
        rows.sort()
        return (f"{_plural(len(rows), 'sensor')} changed in the last {_plural(minutes, 'minute')}:\n"
                + "\n".join(r for _, r in rows[:LIST_CAP]))


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_WHICH = _p("string", "The device: its entity id (like light.kitchen) or its name (like Kitchen light).")
TOOLS = [
    ("set_address", "Save the address of your Home Assistant on your home network, like "
     "homeassistant.local:8123 or 192.168.1.20:8123. For an address off this computer, the bot raises a Yes card for that address and port.",
     {"address": _p("string", "Home Assistant's address. Port 8123 if none is given.")}, ["address"]),
    ("list_devices", "List Home Assistant devices with their state: lights, switches, fans, thermostats, covers, "
     "locks, media players, scenes, sensors, cameras. Up to 100.",
     {"domain": _p("string", "all, or one of light, switch, fan, climate, cover, lock, media_player, scene, "
                   "sensor, binary_sensor, camera. Default all."),
      "name": _p("string", "Optional: only devices whose name has these words.")}, []),
    ("device_state", "The state of one Home Assistant device, with a few details like brightness or temperature.",
     {"entity": _WHICH}, ["entity"]),
    ("turn_on", "Turn on a light, switch, fan or media player, or open a blind, curtain or shade. "
     "Never a lock, garage door or gate.", {"entity": _WHICH}, ["entity"]),
    ("turn_off", "Turn off a light, switch, fan or media player, or close a blind, curtain or shade. "
     "Never a lock, garage door or gate.", {"entity": _WHICH}, ["entity"]),
    ("set_light", "Set a light's brightness (percent), colour (a name like red, or rgb) or white colour "
     "temperature (warm, neutral, cool, daylight, or kelvin).",
     {"entity": _WHICH, "brightness": _p("integer", "Brightness from 0 to 100 percent."),
      "color": _p("string", "A colour name, like red or blue."),
      "rgb": _p("string", "Red, green, blue from 0 to 255, like 255,120,0."),
      "color_temp": _p("string", "warm, neutral, cool, daylight, or a kelvin number like 2700.")}, ["entity"]),
    ("set_temperature", "Set a thermostat's target temperature, in the unit Home Assistant uses, and optionally "
     "its mode (heat, cool, auto, off).",
     {"entity": _p("string", "The thermostat: its entity id (like climate.hallway) or its name."),
      "temperature": _p("number", "The target temperature."),
      "hvac_mode": _p("string", "Optional: off, heat, cool, heat_cool, auto, dry or fan_only.")},
     ["entity", "temperature"]),
    ("run_scene", "Run a Home Assistant scene by name. A scene that touches a lock, alarm, door or gate is not run.",
     {"scene": _p("string", "The scene's name or entity id, like Movie night or scene.movie_night.")}, ["scene"]),
    ("list_cameras", "List the cameras Home Assistant has and their state. No pictures.", {}, []),
    ("recent_motion", "Which motion or occupancy sensors changed in the last few minutes.",
     {"minutes": _p("integer", "How far back to look, 1 to 1440 minutes. Default 30.")}, []),
]
CHANGES = ["set_address", "turn_on", "turn_off", "set_light", "set_temperature", "run_scene"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: HomeAssistant, msg: dict) -> dict | None:
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
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        if not isinstance(args, dict):
            return said(f"Those arguments did not fit {name}: send them as an object.", True)
        try:
            return said(getattr(app, name)(**args), False)
        except (Unreadable, HAError) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"Home Assistant does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "HomeAssistantPkg/1"
    app: HomeAssistant
    token: str
    timeout = 15        # seconds; a stalled sender is cut off

    def log_message(self, fmt, *args):     # quiet; nothing here is logged, the token least of all
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

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _refused(self) -> bool:
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Home Assistant needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Home Assistant stopped on this request with an error "
                                          f"({type(exc).__name__})."})
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
        self._json(404, {"error": "Home Assistant has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Home Assistant has no page here."})
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
    app = HomeAssistant(Store(data))
    handler = type("HomeAssistantHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.app = app
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Home Assistant for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Home Assistant with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
