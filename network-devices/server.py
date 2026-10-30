# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/security/device_classifier.py (adapted: the maker table)
"""Network devices: the devices this computer has talked to on the home network, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN.

What it reads: this computer's own neighbour table (``arp -a`` on Windows, ``arp -an`` on a Mac,
``ip neigh`` then ``arp -an`` on Linux), run as an argument list (never a shell string) with a
10 second limit. For each private address it asks the computer's usual name lookup for a name
(``socket.gethostbyaddr``, best effort, a few seconds in all), and it guesses the maker from the
first half of the hardware address with a small table built in. It sends nothing anywhere: no
pings, no port scans, no internet. HomeBot's version (``bridge_list_devices``, ``bridge_status``,
``privacy_scan_network``) also asked Home Assistant, mDNS and AdGuard; this one does not.

The device list is one JSON document in PUFFPAI_DATA (``devices.json``). Writes replace the whole
file at once (temp file, then rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "network-devices"
VERSION = "1.0.0"
DOC = "devices.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
ARP_TIMEOUT_S = 10
MAX_TABLE = 1024 * 1024         # bytes of command output read at most
NAME_BUDGET_S = 6.0             # all the name lookups of one scan together
NAME_WORKERS = 8
MAX_DEVICES = 1000
MAX_LOOKUPS = 256

#: A small table of common home makers, by the first three bytes of the hardware address.
#: A guess only: many devices use a chip maker's prefix (Espressif, Intel), not the brand on the box.
MAKERS = {
    "00:03:93": "Apple", "00:1b:63": "Apple", "00:1e:c2": "Apple", "00:25:00": "Apple", "a4:83:e7": "Apple",
    "ac:bc:32": "Apple", "b8:17:c2": "Apple", "d8:1d:72": "Apple", "f0:d1:a9": "Apple",
    "00:07:ab": "Samsung", "00:16:32": "Samsung", "00:21:19": "Samsung", "00:26:37": "Samsung",
    "50:01:bb": "Samsung",
    "00:fc:8b": "Amazon", "10:ce:a9": "Amazon", "34:d2:70": "Amazon", "44:65:0d": "Amazon", "68:54:fd": "Amazon",
    "74:c2:46": "Amazon", "f0:81:73": "Amazon",
    "00:1a:11": "Google", "48:d6:d5": "Google", "54:60:09": "Google", "f4:f5:d8": "Google",
    "18:b4:30": "Google Nest", "64:16:66": "Google Nest",
    "00:0e:58": "Sonos", "48:a6:b8": "Sonos", "94:9f:3e": "Sonos",
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
    "24:0a:c4": "Espressif (smart plug or sensor chip)", "5c:cf:7f": "Espressif (smart plug or sensor chip)",
    "a4:cf:12": "Espressif (smart plug or sensor chip)", "84:cc:a8": "Espressif (smart plug or sensor chip)",
    "50:c7:bf": "TP-Link", "60:32:b1": "TP-Link", "d8:07:b6": "TP-Link",
    "00:17:88": "Philips Hue", "ec:b5:fa": "Philips Hue",
    "dc:3a:5e": "Roku", "b0:a7:37": "Roku",
    "00:17:ab": "Nintendo", "34:af:2c": "Nintendo",
    "00:04:1f": "Sony", "28:0d:fc": "Sony",
    "00:50:f2": "Microsoft", "7c:1e:52": "Microsoft",
    "00:1b:21": "Intel", "00:1e:65": "Intel", "00:22:fb": "Intel",
    "b0:09:da": "Ring", "5c:47:5e": "Ring", "2c:aa:8e": "Wyze", "d0:3f:27": "Wyze", "44:61:32": "ecobee",
    "00:11:85": "HP", "00:17:a4": "HP", "3c:d9:2b": "HP", "00:00:85": "Canon", "00:1e:8f": "Canon",
    "00:00:48": "Epson", "00:1b:a9": "Epson",
    "00:14:6c": "NETGEAR", "00:22:3f": "NETGEAR", "04:18:d6": "Ubiquiti", "80:2a:a8": "Ubiquiti",
    "00:14:bf": "Linksys", "00:1d:7e": "Linksys",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(stamp: str) -> datetime | None:
    try:
        got = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def _show(stamp: str) -> str:
    got = _when(stamp)
    return got.astimezone().strftime("%b %d %H:%M").replace(" 0", " ") if got else "unknown"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- checked input

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed, with no control characters; *default* when not given."""
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


def _flag(value, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "true", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false", "off", "0"):
        return False
    raise ValueError(f"{name} is true or false")


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this package takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


# ---------------------------------------------------------------- reading the neighbour table

class NoTable(RuntimeError):
    """The neighbour table could not be read. The words say why, plainly."""


_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_MAC = r"[0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5}"
WINDOWS_ROW = re.compile(rf"^\s*({_IPV4})\s+({_MAC})\s+\S+\s*$")
ARP_ROW = re.compile(rf"^(\S+)\s+\(({_IPV4})\)\s+at\s+({_MAC})(?:\s|$)")          # Mac and Linux arp -a
NEIGH_ROW = re.compile(rf"^(\S+)\s+dev\s+\S+.*?\blladdr\s+({_MAC})(?:\s|$)")       # Linux ip neigh


def normal_mac(text: str) -> str | None:
    """``aa:bb:cc:dd:ee:ff`` from any of the table spellings (``aa-bb-..``, a Mac's ``a:b:c:..``), or None."""
    parts = re.split(r"[:-]", str(text or "").strip())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9A-Fa-f]{1,2}", p) for p in parts):
        return None
    return ":".join(p.zfill(2).lower() for p in parts)


def _usable(address: str, mac: str | None) -> bool:
    """A real neighbour: a unicast hardware address and a unicast, non-loopback network address."""
    if not mac or mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00") or int(mac[:2], 16) & 1:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not (ip.is_multicast or ip.is_loopback or ip.is_unspecified or str(ip) == "255.255.255.255")


def parse_table(text: str) -> list[dict]:
    """``[{address, mac, name}]`` from ``arp -a`` (Windows, Mac, Linux) or ``ip neigh`` output. One row per
    hardware address (an IPv4 address wins over IPv6, then the last one); broadcast, multicast and
    incomplete rows are left out."""
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        name = ""
        m = WINDOWS_ROW.match(line)
        if m:
            address, raw = m.group(1), m.group(2)
        elif (m := ARP_ROW.match(line)):
            name, address, raw = ("" if m.group(1) == "?" else m.group(1)), m.group(2), m.group(3)
        elif (m := NEIGH_ROW.match(line.strip())):
            address, raw = m.group(1), m.group(2)
        else:
            continue
        mac = normal_mac(raw)
        if _usable(address, mac):
            if ":" in address and mac in rows and ":" not in rows[mac]["address"]:
                continue
            rows[mac] = {"address": address.split("%", 1)[0], "mac": mac, "name": _clean_name(name)}
    return list(rows.values())


def _find(program: str) -> str | None:
    """A full path to a system program from the system's own folders only, never the current folder."""
    if sys.platform == "win32":
        path = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / f"{program}.exe"
        return str(path) if path.is_file() else None
    return shutil.which(program, path="/usr/sbin:/sbin:/usr/bin:/bin")


def _commands(platform: str) -> list[list[str]]:
    """The commands to try, in order, as argument lists. Missing programs are left out."""
    if platform == "win32":
        wanted = [("arp", ["-a"])]
    elif platform.startswith("linux"):
        wanted = [("ip", ["neigh", "show"]), ("arp", ["-an"])]
    else:
        wanted = [("arp", ["-an"])]
    out = []
    for program, args in wanted:
        path = _find(program)
        if path:
            out.append([path, *args])
    return out


def run_table(argv: list[str]) -> str:
    """One command's output (argument list, no shell, 10 second limit, no console window)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    env = dict(os.environ, LC_ALL="C", LANG="C")
    done = subprocess.run(argv, capture_output=True, timeout=ARP_TIMEOUT_S, shell=False, check=False,
                          stdin=subprocess.DEVNULL, env=env, creationflags=flags)
    return bytes(done.stdout or b"")[:MAX_TABLE].decode("utf-8", errors="replace")


def read_neighbours(platform: str | None = None) -> list[dict]:
    """This computer's neighbour table, from the first command that gives any rows."""
    commands = _commands(platform or sys.platform)
    if not commands:
        raise NoTable("This computer has no arp or ip program in its system folders, so the device list "
                      "cannot be read.")
    problems = []
    for argv in commands:
        program = Path(argv[0]).name
        try:
            rows = parse_table(run_table(argv))
        except subprocess.TimeoutExpired:
            problems.append(f"{program} took longer than {ARP_TIMEOUT_S} seconds")
            continue
        except OSError as exc:
            problems.append(f"{program} would not start ({type(exc).__name__})")
            continue
        if rows:
            return rows
        problems.append(f"{program} listed no devices")
    if all(p.endswith("listed no devices") for p in problems):
        return []
    raise NoTable("The device list could not be read: " + "; ".join(problems) + ".")


def _clean_name(name) -> str:
    name = str(name or "").strip().rstrip(".")
    if not name or len(name) > 253 or _CONTROL.search(name):
        return ""
    try:
        ipaddress.ip_address(name)
        return ""                         # an address is not a name
    except ValueError:
        return name


def _lookup_ok(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def look_up_names(addresses: list[str], budget_s: float = NAME_BUDGET_S) -> dict[str, str]:
    """Reverse names for private addresses, best effort: a few lookups at once, and whatever is back
    when *budget_s* is up. A lookup still running then is left to finish on its own and is not used."""
    todo: queue.Queue = queue.Queue()
    for a in [a for a in addresses if _lookup_ok(a)][:MAX_LOOKUPS]:
        todo.put(a)
    found: dict[str, str] = {}
    lock = threading.Lock()

    def work():
        while True:
            try:
                a = todo.get_nowait()
            except queue.Empty:
                return
            try:
                name = _clean_name(socket.gethostbyaddr(a)[0])
            except (OSError, UnicodeError, ValueError):
                name = ""
            if name:
                with lock:
                    found[a] = name

    workers = [threading.Thread(target=work, daemon=True) for _ in range(min(NAME_WORKERS, todo.qsize()))]
    for w in workers:
        w.start()
    end = time.monotonic() + budget_s
    for w in workers:
        w.join(max(0.0, end - time.monotonic()))
    with lock:
        return dict(found)


def maker(mac: str) -> str:
    """A maker guess from the first three bytes, or a plain note for a private (random) address."""
    if int(mac[:2], 16) & 2:
        return "private address (a phone or computer hiding its real one)"
    return MAKERS.get(mac[:8], "")


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"devices": {}, "last_scan": "", "last_asked_new": "", "last_updated": ""}


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
                raise Unreadable(f"{self.path} will not read ({exc}). Network devices will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("devices", {}), dict):
                raise Unreadable(f"{self.path} is not a device list. Network devices will not write over it.")
            out = default_doc()
            out.update(data)
            out["devices"] = {k: v for k, v in out["devices"].items() if isinstance(v, dict) and normal_mac(k)}
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".devices-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ---------------------------------------------------------------- the logic

def _title(d: dict) -> str:
    return d.get("label") or d.get("name") or "unnamed"


def _line(d: dict) -> str:
    bits = [str(d.get("address") or "no address"), str(d.get("mac") or "")]
    if d.get("label") and d.get("name"):
        bits.append(f"name {d['name']}")
    bits.append(f"maker guess: {d.get('vendor') or 'unknown'}")
    bits.append(f"first seen {_show(d.get('first_seen', ''))}, last seen {_show(d.get('last_seen', ''))}")
    return f"- {_title(d)}: " + "; ".join(bits)


class Devices:
    def __init__(self, store: Store, platform: str | None = None) -> None:
        self.store = store
        self.platform = platform

    def _find_one(self, data: dict, device: str) -> dict:
        want = _text(device, "device", 120)
        if not want:
            raise ValueError("say which device: its address, its hardware address, its name or its label")
        devices = list(data["devices"].values())
        mac = normal_mac(want)
        low = want.lower()
        for test in (lambda d: mac is not None and d.get("mac") == mac,
                     lambda d: d.get("address") == want,
                     lambda d: low in (str(d.get("label") or "").lower(), str(d.get("name") or "").lower())):
            hits = [d for d in devices if test(d)]
            if len(hits) == 1:
                return hits[0]
        hits = [d for d in devices if low in str(d.get("label") or "").lower() or low in str(d.get("name") or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ValueError(f"{want!r} fits {len(hits)} devices ({', '.join(_title(d) for d in hits[:6])}). "
                             "Give its address instead.")
        raise ValueError(f"there is no device {want!r} on the list. Ask for the device list to see them.")

    def _merge(self, data: dict, rows: list[dict], names: dict[str, str]) -> list[dict]:
        now = _now()
        added = []
        for row in rows:
            known = data["devices"].get(row["mac"])
            if known is None:
                if len(data["devices"]) >= MAX_DEVICES:
                    continue
                known = data["devices"][row["mac"]] = {"mac": row["mac"], "label": "", "first_seen": now}
                added.append(known)
            known["address"] = row["address"]
            known["name"] = row["name"] or names.get(row["address"]) or known.get("name", "")
            known["vendor"] = maker(row["mac"])
            known["last_seen"] = now
        data["last_scan"] = now
        return added

    def _scan(self, look_up: bool) -> tuple[dict, list[dict], int]:
        rows = read_neighbours(self.platform)
        names = look_up_names([r["address"] for r in rows if not r["name"]]) if look_up else {}
        with self.store.lock:
            data = self.store.load()
            added = self._merge(data, rows, names)
            self.store.save(data)
        return data, added, len(rows)

    # tools
    def scan(self, look_up_names: bool = True) -> str:
        """From privacy_scan_network (rebot_tools.py:1505): read the table, keep it, say what is new."""
        try:
            data, added, seen = self._scan(_flag(look_up_names, "look_up_names", True))
        except NoTable as exc:
            return str(exc) + " Nothing was saved."
        line = (f"Scanned this computer's device list: {_plural(seen, 'device')} seen just now, "
                f"{_plural(len(data['devices']), 'device')} on the list in all.")
        if added:
            line += f"\nNew on the list ({len(added)}):\n" + "\n".join(_line(d) for d in added)
        elif seen:
            line += " No new ones."
        if len(data["devices"]) >= MAX_DEVICES:
            line += f"\nThe list is full ({MAX_DEVICES} devices). Forget some to make room."
        return line

    def list_devices(self, filter: str = "") -> str:  # noqa: A002 - the tool's own word
        """From bridge_list_devices (rebot_tools.py:2580): the list as saved, with an optional filter."""
        word = _text(filter, "filter", 60).lower()
        data = self.store.load()
        devices = sorted(data["devices"].values(), key=lambda d: (_title(d) == "unnamed", _title(d).lower(),
                                                                  str(d.get("address") or "")))
        if word:
            devices = [d for d in devices if any(word in str(d.get(f) or "").lower()
                                                 for f in ("address", "mac", "name", "label", "vendor"))]
        if not data["last_scan"]:
            return "No scan yet. Ask me to scan the network first."
        head = f"{_plural(len(devices), 'device')}"
        head += f" matching {word!r}" if word else " on the list"
        head += f" (last scan {_show(data['last_scan'])}). Only devices this computer has talked to show up."
        return head + ("\n" + "\n".join(_line(d) for d in devices) if devices else "")

    def new_devices(self) -> str:
        """Scan, then list the devices first seen since the owner last asked, and move that mark to now."""
        try:
            self._scan(True)
        except NoTable as exc:
            return str(exc)
        with self.store.lock:
            data = self.store.load()
            since = _when(data.get("last_asked_new", ""))
            fresh = [d for d in data["devices"].values()
                     if since is None or ((_when(d.get("first_seen", "")) or since) > since)]
            data["last_asked_new"] = _now()
            self.store.save(data)
        if since is None:
            return (f"This is the first time you asked, so everything is new: {_plural(len(fresh), 'device')}.\n"
                    + "\n".join(_line(d) for d in fresh) + "\nNext time I list only the ones that appear after now.")
        if not fresh:
            return f"No new devices since you last asked ({_show(since.isoformat())})."
        return (f"{_plural(len(fresh), 'new device')} since you last asked ({_show(since.isoformat())}):\n"
                + "\n".join(_line(d) for d in fresh))

    def name_device(self, device: str, label: str) -> str:
        label = _text(label, "label", 60)
        if not label:
            raise ValueError("a label is a few words, like the kitchen tablet")
        with self.store.lock:
            data = self.store.load()
            d = self._find_one(data, device)
            d["label"] = label
            self.store.save(data)
        return f"Saved: {d.get('address') or d['mac']} ({d['mac']}) is now called {label!r}."

    def forget_device(self, device: str) -> str:
        with self.store.lock:
            data = self.store.load()
            d = self._find_one(data, device)
            del data["devices"][d["mac"]]
            self.store.save(data)
        return (f"Forgot {_title(d)} ({d['mac']}). If it is still on the network it comes back as new "
                "on the next scan.")


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("scan", "Read this computer's own list of devices it has talked to on the home network and save it: "
     "addresses, hardware addresses, names where known and a maker guess. Sends nothing to the devices.",
     {"look_up_names": _p("boolean", "Ask the home network's usual name lookup for each device's name. Default true.")},
     []),
    ("list_devices", "List the saved devices: address, hardware address, name if known, maker guess, first seen, "
     "last seen and the owner's label. Optional filter word.",
     {"filter": _p("string", "Optional: a word to match, like apple, a label, or part of an address.")}, []),
    ("new_devices", "Scan now, then list the devices that first showed up since the owner last asked.", {}, []),
    ("name_device", "Give a device a label the owner chooses, like the kitchen tablet.",
     {"device": _p("string", "The device: its address, hardware address, name or current label."),
      "label": _p("string", "The new label, up to 60 characters.")}, ["device", "label"]),
    ("forget_device", "Take a device off the saved list. It comes back as new if this computer sees it again.",
     {"device": _p("string", "The device: its address, hardware address, name or label.")}, ["device"]),
]
CHANGES = ["scan", "new_devices", "name_device", "forget_device"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "openWorldHint": False}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Devices, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

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
            return ok({"content": [{"type": "text", "text": f"{name} takes named arguments."}], "isError": True})
        try:
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Network devices does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "NetworkDevices/1"
    app: Devices
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
        self._json(401, {"error": "Network devices needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            try:
                self._json(500, {"error": f"Network devices stopped on this request with an error ({type(exc).__name__})."})
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
        self._json(404, {"error": "Network devices has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "Network devices has no page here."})
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


def make_server(port: int, data: Path | str, token: str | None, platform: str | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Devices(Store(data), platform)
    handler = type("NetworkDevicesHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Network devices for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Network devices with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
