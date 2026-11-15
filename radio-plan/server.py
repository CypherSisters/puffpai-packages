"""Radio Plan: the household's plan for staying in touch by radio when phones fail, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/radio-plan/
- ``/health``       "ok"
- ``/data``         the saved plan (GET), ``/updated`` its stamp
- ``/view``         the plan with the household merged in, the Now panel and the pocket cards (GET)
- ``/api/update``, ``/api/remove``, ``/api/message``, ``/api/practice``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/radio-plan/, which adds it.

The plan is one JSON document in PUFFPAI_DATA (``radio.json``). Writes replace the
whole file at once (temp file, then rename). A file that exists but will not read,
an empty one included, is never written over. The household comes from the Core's
shared people list (``GET /v1/people`` on PUFFPAI_API, this computer only) when it
answers, and from the plan's own list otherwise. How the next check-in is worked
out and what a pocket card says: PROCESS.md.

The app stores what the owner types. It does not judge what is legal to transmit
and ships no frequency lists.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "radio-plan"
VERSION = "1.0.0"
DOC = "radio.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_DRAIN = 64 * 1024 * 1024            # a refused body: up to this much is read and dropped before the 400
DRAIN_WAIT_S = 20.0                     # ... within this many seconds
NO_TOKEN_DRAIN = 64 * 1024              # a request without the token: at most this much is read before the 401
NO_TOKEN_WAIT_S = 2.0
PEOPLE_DEADLINE_S = 2.0                 # the whole fetch of the Core's people list
BODY_WAIT_S = 5.0                       # the whole wait for a request body
RULES_LINE = "Rules differ by country. Some channels need a license. Check yours before you transmit."

# Every number the plan uses, in one table (PROCESS.md says how each is used).
NUMBERS = {
    "listen_minutes": 5,                # a check-in's listen time when none is written
    "backup_after_minutes": 5,          # go to the backup channel after this many minutes when none is written
    "most_minutes": 240,                # the most minutes a check-in's listen or backup wait may be
    "recent_messages": 10,              # list_messages with no count
    "most_messages_shown": 50,
}
LIMITS = {"radios": 50, "channels": 50, "people": 30, "check_ins": 24, "listen": 30, "messages": 2000,
          "practice": 500}

# ---------------------------------------------------------------- the plan's shape
# Each field: (label, most characters, how it is read: line, lines, flag, minutes, radio_kind, use, listen_kind, when)
RADIO = {"kind": ("kind", 0, "radio_kind"), "carrier": ("who carries it", 60, "line"),
         "kept": ("where it is kept", 200, "line"), "battery": ("battery type", 80, "line"),
         "notes": ("notes", 500, "lines")}
CHANNEL = {"service": ("service", 80, "line"), "channel": ("channel or frequency", 40, "line"),
           "tone": ("tone or code", 40, "line"), "use": ("used for", 0, "use"), "notes": ("notes", 500, "lines")}
PERSON = {"call_sign": ("call sign or radio name", 40, "line"), "license": ("license", 120, "line"),
          "primary_channel": ("their primary channel", 60, "line"),
          "backup_channel": ("their backup channel", 60, "line"), "notes": ("notes", 500, "lines")}
CHECKIN = {"listen_minutes": ("minutes to listen", 0, "minutes"), "channel": ("channel", 60, "line"),
           "backup_after_minutes": ("go to backup after minutes", 0, "minutes"),
           "backup_channel": ("backup channel", 60, "line"),
           "no_contact": ("then, if still no contact", 300, "lines"), "notes": ("notes", 500, "lines")}
LISTEN = {"kind": ("kind", 0, "listen_kind"), "frequency": ("station or frequency", 60, "line"),
          "notes": ("notes", 500, "lines")}
MESSAGE = {"when": ("date and time", 16, "when"), "sender": ("from", 60, "line"), "to": ("to", 60, "line"),
           "channel": ("channel", 60, "line"), "message": ("message", 1000, "lines"), "acted": ("acted on", 0, "flag")}
PRACTICE = {"date": ("date", 10, "date"), "what": ("what was practiced", 300, "line"),
            "fix": ("what to fix", 1000, "lines")}
FIELDS = {"radios": RADIO, "channels": CHANNEL, "people": PERSON, "check_ins": CHECKIN, "listen": LISTEN,
          "messages": MESSAGE, "practice": PRACTICE}
KEY = {"messages": "message", "practice": "what"}          # what names an item; "name" for the rest
ONE = {"radios": "radio", "channels": "channel", "people": "person", "check_ins": "check-in",
       "listen": "listen-only station", "messages": "message", "practice": "practice entry"}
NAME_MOST = {"radios": 80, "channels": 60, "people": 60, "check_ins": 5, "listen": 80}

SECTIONS = ("radios", "channels", "people", "check_ins", "listen", "messages", "practice")
SECTION_TITLES = {"radios": "Radios", "channels": "Channels", "people": "People and call signs",
                  "check_ins": "Check-in times", "listen": "Listen-only stations", "messages": "Message log",
                  "practice": "Practice"}
SECTION_WORDS = {"radio": "radios", "equipment": "radios", "gear": "radios", "channel": "channels",
                 "frequencies": "channels", "frequency": "channels", "person": "people", "household": "people",
                 "call_signs": "people", "call_sign": "people", "callsigns": "people", "family": "people",
                 "check_in": "check_ins", "checkins": "check_ins", "checkin": "check_ins", "schedule": "check_ins",
                 "check_in_times": "check_ins", "times": "check_ins", "listen_only": "listen",
                 "listen_only_stations": "listen", "stations": "listen", "station": "listen", "weather": "listen",
                 "message": "messages", "message_log": "messages", "log": "messages", "drill": "practice",
                 "drills": "practice", "practice_log": "practice", "tests": "practice"}

RADIO_KINDS = {"handheld": "handheld two-way", "mobile": "mobile (in a vehicle)", "base": "base station",
               "weather": "weather alert radio", "shortwave": "shortwave receiver", "scanner": "scanner",
               "mesh": "mesh text radio", "other": "other"}
# Words that point to a kind, in order: the first kind with a word in the text wins.
RADIO_WORDS = (("weather", ("weather", "noaa", "alert radio", "weather alert", "weather radio")),
               ("shortwave", ("shortwave", "short wave", "world band", "sw")),
               ("scanner", ("scanner", "police scanner")),
               ("mesh", ("mesh", "meshtastic", "lora", "text radio")),
               ("base", ("base", "base station", "desk", "home station")),
               ("mobile", ("mobile", "car", "truck", "vehicle", "dash", "in car")),
               ("handheld", ("handheld", "hand held", "walkie", "walkie talkie", "walkie talkies", "walkietalkie",
                             "two way", "2 way", "frs", "gmrs", "pmr", "pmr446", "ht", "handie", "portable")))
USES = {"primary": "primary", "backup": "backup", "neighbors": "neighbors", "listen_only": "listen only",
        "emergency": "emergency", "other": "other"}
USE_WORDS = (("emergency", ("emergency", "emergencies", "help", "distress", "sos", "mayday")),
             ("neighbors", ("neighbor", "neighbors", "neighbour", "neighbours", "street", "block", "community")),
             ("listen_only", ("listen", "listen only", "receive", "receive only", "monitor", "weather", "news")),
             ("backup", ("backup", "back up", "second", "alternate", "alt", "fallback", "spare", "secondary")),
             ("primary", ("primary", "main", "first", "household", "usual", "default", "everyday")))
LISTEN_KINDS = {"weather": "weather alert", "broadcast": "local broadcast station", "other": "other"}
LISTEN_WORDS = (("weather", ("weather", "noaa", "alert", "alerts")),
                ("broadcast", ("am", "fm", "broadcast", "news", "station", "talk", "radio station", "local")))
TRUE_WORDS = {"true", "yes", "y", "1", "on", "done", "acted", "acted on", "handled", "checked"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "not yet", "not acted", "open", "unchecked"}
FIELD_WORDS = {"call": "call_sign", "callsign": "call_sign", "radio_name": "call_sign", "handle": "call_sign",
               "licence": "license", "license_class": "license", "who": "carrier", "carried_by": "carrier",
               "owner": "carrier", "person": "carrier", "where": "kept", "location": "kept", "stored": "kept",
               "kept_at": "kept", "batteries": "battery", "battery_type": "battery", "freq": "channel",
               "channel_number": "channel", "number": "channel", "ctcss": "tone", "dcs": "tone", "code": "tone",
               "privacy_code": "tone", "tone_code": "tone", "purpose": "use", "for": "use", "used_for": "use",
               "type": "kind", "listen": "listen_minutes", "minutes": "listen_minutes", "listen_for": "listen_minutes",
               "backup_after": "backup_after_minutes", "wait": "backup_after_minutes", "primary": "primary_channel",
               "label": "name", "from": "sender", "acted_on": "acted", "note": "notes", "station": "frequency",
               "if_no_contact": "no_contact", "then": "no_contact", "text": "message", "body": "message",
               "time": "name", "date_time": "when", "at": "when", "to_fix": "fix", "what_to_fix": "fix"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _key(word) -> str:
    return "_".join(str(word).lower().replace("-", " ").split())


def _words(text) -> str:
    """Lower-case words with single spaces around them, so ' frs ' can be looked for."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()) + " "


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, lines: bool = False) -> str:
    """Words of at most *most* characters, trimmed, control characters dropped; '' when not given."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be words")
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c == "\n" or (c >= " " and c != "\x7f"))
    if lines:
        value = "\n".join(" ".join(line.split()) for line in value.split("\n")).strip("\n")
        while "\n\n\n" in value:
            value = value.replace("\n\n\n", "\n\n")
    else:
        value = " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value


def _flag(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in TRUE_WORDS | FALSE_WORDS:
        return value.strip().lower() in TRUE_WORDS
    raise ValueError(f"{name} is yes or no")


def _minutes(value, name: str) -> int:
    """Whole minutes, 1 to the most; words like '10 minutes' are fine. 0 when not given."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{name} is a number of minutes")
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ValueError(f"{name} is a whole number of minutes")
        value = int(value)
    if isinstance(value, str):
        text = value.strip().lower() if len(value) <= 40 else ""      # capped before anything reads it
        digits = text[:len(text) - len(text.lstrip("0123456789"))]
        unit = text[len(digits):].strip()
        if not 1 <= len(digits) <= 4 or unit not in ("", "m", "min", "mins", "minute", "minutes"):
            raise ValueError(f"{name} is a number of minutes, like 10")
        value = int(digits)
    if not isinstance(value, int) or not 1 <= value <= NUMBERS["most_minutes"]:
        raise ValueError(f"{name} is 1 to {NUMBERS['most_minutes']} minutes")
    return value


def _clock(value, name: str = "time") -> str:
    """A time of day as HH:MM, from words like 8, 8am, 8:30 pm, 20:00, noon or midnight."""
    text = _text(value, name, 20).lower().replace(".", "")
    if text in ("noon", "midday"):
        return "12:00"
    if text == "midnight":
        return "00:00"
    m = re.fullmatch(r"(\d{1,2})(?:[:h](\d{2}))?\s*(am|pm|a m|p m)?", text)
    if not m:
        raise ValueError(f"{name} is a time of day, like 08:00, 8am or 18:30")
    h, mi, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    if half:
        if not 1 <= h <= 12:
            raise ValueError(f"{name} is a time of day, like 08:00, 8am or 18:30")
        h = (h % 12) + (12 if half == "pm" else 0)
    if h > 23 or mi > 59:
        raise ValueError(f"{name} is a time of day, like 08:00, 8am or 18:30")
    return f"{h:02d}:{mi:02d}"


def _when(value, now: datetime) -> str:
    """A message's date and time as YYYY-MM-DD HH:MM: now, a time today, or a full date and time."""
    text = _text(value, "when", 40).lower()
    if text in ("", "now", "just now"):
        return now.strftime("%Y-%m-%d %H:%M")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[ t](.+))?", text)
    try:
        if m:
            day = date.fromisoformat(m.group(1))
            hm = _clock(m.group(2)[:5] if m.group(2) and re.match(r"\d\d:\d\d", m.group(2)) else m.group(2) or "0")
            return f"{day.isoformat()} {hm}"
        return f"{now.date().isoformat()} {_clock(text)}"
    except ValueError:
        raise ValueError("when is now, a time today like 18:30, or a date and time like 2027-03-31 18:30") from None


def _day(value, name: str, today: date) -> date | None:
    text = _text(value, name, 40).lower()
    if not text:
        return None
    if text in ("today", "yesterday"):
        return today - timedelta(days=0 if text == "today" else 1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or today or yesterday") from None


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _int(text: str) -> int:
    if len(text.lstrip("-")) > 15:
        raise ValueError("that number is too big for this app")
    return int(text)


def _float(text: str) -> float:
    x = float(text) if len(text) <= 40 else math.inf
    if not math.isfinite(x) or abs(x) > 1e15:
        raise ValueError("that number is too big for this app")
    return x


def _loads(text):
    """JSON, refusing huge numbers, NaN and Infinity, and nesting too deep to read, as plain ValueErrors."""
    try:
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8")
        return json.loads(text, parse_constant=_no_constant, parse_int=_int, parse_float=_float)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None
    except UnicodeDecodeError:
        raise ValueError("that is not UTF-8 text") from None


def _section(value) -> str:
    key = _key(_text(value, "section", 40))
    key = SECTION_WORDS.get(key, key)
    if key not in SECTIONS:
        raise ValueError("section is one of " + ", ".join(SECTIONS))
    return key


def _field(section: str, word) -> str:
    """A field of *section* by its name, a common word for it, or its label words."""
    fields = FIELDS[section]
    key = _key(_text(word, "field", 60))
    if key == "name" or key in fields:
        return key
    if section == "listen" and key in ("channel", "freq", "number"):
        return "frequency"
    if section == "check_ins" and key == "backup":
        return "backup_channel"
    if section == "people" and key in ("backup", "backup_channel"):
        return "backup_channel"
    if section == "people" and key == "channel":
        return "primary_channel"
    if section == "practice" and key in ("when", "day"):
        return "date"
    alias = FIELD_WORDS.get(key, key)
    if alias == "name" or alias in fields:
        return alias
    for k, (label, _, _) in fields.items():
        if _key(label) == key:
            return k
    raise ValueError(f"{section} has no field {word!r}; its fields are " + ", ".join(fields))


def _pick(table: dict, words: tuple, value, what: str) -> tuple[str, str]:
    """(kind, how it was read) from everyday words. Never refused: no match gives other, and the answer says so."""
    text = _text(value, what, 80)
    key = _key(text)
    for k, label in table.items():
        if key in (k, _key(label)):
            return k, ""
    w = _words(text)
    for k, hints in words:
        if any(f" {h} " in w for h in hints):
            return k, f"{table[k]}, from {text!r}"
    return "other", f"other, because {text!r} is not one of: " + ", ".join(table.values())


def _clean(sec: str, k: str, v, now: datetime):
    """One field's value, checked. Returns (value, a note on how a kind word was read or '')."""
    label, most, how = FIELDS[sec][k]
    if how == "flag":
        return _flag(v, label), ""
    if how == "minutes":
        return _minutes(v, label), ""
    if how == "radio_kind":
        return _pick(RADIO_KINDS, RADIO_WORDS, v, label) if v not in (None, "") else ("other", "")
    if how == "use":
        return _pick(USES, USE_WORDS, v, label) if v not in (None, "") else ("other", "")
    if how == "listen_kind":
        return _pick(LISTEN_KINDS, LISTEN_WORDS, v, label) if v not in (None, "") else ("other", "")
    if how == "when":
        return _when(v, now), ""
    if how == "date":
        d = _day(v, label, now.date())
        if d is None:
            raise ValueError("say the date")
        return d.isoformat(), ""
    return _text(v, label, most, how == "lines"), ""


def _blank(sec: str) -> dict:
    out = {}
    for k, (_, _, how) in FIELDS[sec].items():
        out[k] = False if how == "flag" else 0 if how == "minutes" else "other" if how.endswith("kind") or how == "use" \
            else ""
    return out


ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _clean_item(sec: str, raw, at: int, now: datetime) -> dict:
    """One saved list item, every field checked; used when the file is read. An item with a missing or odd id
    gets one made from its section, place and name, the same on every load, and saved on the next write."""
    if not isinstance(raw, dict):
        raise ValueError(f"each {sec} item must be an object")
    key = KEY.get(sec, "name")
    made = hashlib.sha256(f"{sec}\n{at}\n{raw.get(key, '')}".encode("utf-8")).hexdigest()
    rid = raw.get("id")
    out = {"id": rid if isinstance(rid, str) and ID_RE.fullmatch(rid) else "i" + made[:8]}
    if key == "name":
        out["name"] = _clock(raw.get("name")) if sec == "check_ins" else _text(raw.get("name"), "name", NAME_MOST[sec])
        if not out["name"]:
            raise ValueError(f"each {sec} item needs a name")
    blank = _blank(sec)
    for k, (label, most, how) in FIELDS[sec].items():
        v = raw.get(k)
        if v in (None, "") or (how == "minutes" and v == 0 and not isinstance(v, bool)):
            out[k] = blank[k]
        elif how in ("radio_kind", "use", "listen_kind"):
            table = {"radio_kind": RADIO_KINDS, "use": USES, "listen_kind": LISTEN_KINDS}[how]
            out[k] = v if v in table else "other"
        elif how == "when":
            out[k] = _text(v, label, most)
        else:
            out[k] = _clean(sec, k, v, now)[0]
    if sec == "people":
        out["core_id"] = _text(raw.get("core_id"), "core_id", 80)
    if key != "name" and not out[key]:
        raise ValueError(f"each {sec} entry needs its {FIELDS[sec][key][0]}")
    return out


def default_doc() -> dict:
    return {"app": NAME, "format": 1, **{s: [] for s in SECTIONS}, "last_updated": ""}


def normalize(raw, now: datetime) -> dict:
    """A whole plan, every part checked and capped. ValueError says what does not fit."""
    if not isinstance(raw, dict):
        raise ValueError("a plan is a JSON object")
    doc = default_doc()
    for sec in SECTIONS:
        items = raw.get(sec, [])
        if not isinstance(items, list):
            raise ValueError(f"{sec} must be a list")
        if len(items) > LIMITS[sec]:
            raise ValueError(f"{sec} holds at most {LIMITS[sec]} items")
        doc[sec] = [_clean_item(sec, x, i, now) for i, x in enumerate(items)]
    doc["last_updated"] = _text(raw.get("last_updated"), "last_updated", 40)
    return doc


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved plan exists but will not read. Nothing writes over it."""


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str, now) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()
        self.now = now

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return default_doc()
            try:
                raw = self.path.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("the file is empty")
                return normalize(_loads(raw), self.now())
            except (OSError, ValueError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Radio Plan will not write over it. "
                                 f"Move that file somewhere else to start a new plan, or put a good copy back.") from None

    def save(self, data: dict) -> str:
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
            return data["last_updated"]


# ---------------------------------------------------------------- people

class People:
    """The Core's shared people list (``GET /v1/people``, D12), read with this package's token.
    Only PUFFPAI_API on this computer is ever asked; any other address is never dialled."""

    def __init__(self, api: str | None, token: str | None, ttl_s: float = 60, fail_ttl_s: float = 30) -> None:
        self.token, self.ttl_s, self.fail_ttl_s = token or "", ttl_s, fail_ttl_s
        self.where = None
        api = api or ""
        try:
            if any(not c.isprintable() or c.isspace() for c in api):    # a line break or space: never dialled
                raise ValueError("PUFFPAI_API holds a space or a control character")
            parts = urlsplit(api)
            if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1") and parts.port:
                self.where = (parts.hostname, parts.port, parts.path.rstrip("/"))
        except ValueError:
            self.where = None
        self._cache: tuple[float, float, list[dict]] = (-1e9, 0, [])     # (when, how long it holds, list)
        self._busy = threading.Lock()

    def list(self) -> list[dict]:
        """The list, from the cache while it holds (60 s, or 30 s after a failure). While one request asks the
        Core, any other gets the cached list at once. Never call this while holding the plan's lock."""
        at, hold, got = self._cache
        if not (self.where and self.token) or time.monotonic() - at <= hold:
            return got
        if not self._busy.acquire(blocking=False):
            return got
        try:
            try:
                got, hold = self._fetch(), self.ttl_s
            except Exception:  # noqa: BLE001 - no Core, too slow, or an answer that makes no sense: the plan's own list
                got, hold = [], self.fail_ttl_s
            self._cache = (time.monotonic(), hold, got)
            return got
        finally:
            self._busy.release()

    def _fetch(self) -> list[dict]:
        """GET /v1/people, all of it within PEOPLE_DEADLINE_S seconds or an error. A redirect is not followed."""
        host, port, prefix = self.where
        end = time.monotonic() + PEOPLE_DEADLINE_S
        buf = bytearray()
        with socket.create_connection((host, port), timeout=PEOPLE_DEADLINE_S) as s:
            hosthdr = f"[{host}]" if ":" in host else host
            s.sendall((f"GET {prefix}/v1/people HTTP/1.0\r\nHost: {hosthdr}:{port}\r\nAuthorization: Bearer "
                       f"{self.token}\r\nAccept: application/json\r\nConnection: close\r\n\r\n").encode("latin-1"))
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("the Core's people list took too long")
                s.settimeout(left)
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BODY + 16384:
                    raise ValueError("the Core's people list is too big")
                head, sep, body = bytes(buf).partition(b"\r\n\r\n")
                size = _content_length(head) if sep else None
                if size is not None and len(body) >= size:
                    break
        head, sep, body = bytes(buf).partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not sep or len(lines[0].split()) < 2 or lines[0].split()[1] != "200":
            raise ValueError("the Core did not answer 200")
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
        if headers.get("content-length", "").isdigit():
            body = body[:int(headers["content-length"])]
        elif "chunked" in headers.get("transfer-encoding", "").lower():
            body = _unchunk(body)
        if len(body) > MAX_BODY:
            raise ValueError("the Core's people list is too big")
        data = _loads(body)
        if not isinstance(data, list):
            raise ValueError("the Core's people list is not a list")
        return [{"id": _text(p.get("id"), "id", 80), "name": _text(p.get("name") or p.get("id"), "name", 60)}
                for p in data[:LIMITS["people"]] if isinstance(p, dict) and p.get("id")]


def _content_length(head: bytes) -> int | None:
    """The Content-Length of an answer's head, read with plain string steps, or None."""
    for line in head.split(b"\r\n")[1:]:
        k, sep, v = line.partition(b":")
        v = v.strip()
        if sep and k.strip().lower() == b"content-length" and v.isdigit() and len(v) <= 12:
            return int(v)
    return None


def _unchunk(body: bytes) -> bytes:
    out = bytearray()
    while True:
        size_line, sep, body = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0].strip() or b"x", 16)
        if not sep or size == 0:
            return bytes(out)
        out += body[:size]
        body = body[size + 2:]


# ------------------------------------------------------------- the logic

def _find(rows: list[dict], ident, what: str, key: str = "name", also: tuple = ()) -> dict | None:
    """An exact id or name (or one of *also*), then one unambiguous part of a name. None when nothing matches;
    more than one partial match is refused with the candidates, and nothing changes."""
    text = _text(ident, what, 1000)
    if not text:
        raise ValueError(f"say which {what}")
    low = text.lower()
    for r in rows:
        if r.get("id") == text or str(r.get(key, "")).lower() == low or \
                any(str(r.get(a, "")).lower() == low and r.get(a) for a in also):
            return r
    hits = [r for r in rows if low in str(r.get(key, "")).lower()]
    if len(hits) > 1:
        raise ValueError(f"more than one {what} matches {text!r}: " + "; ".join(str(r.get(key))[:60] for r in hits)
                         + ". Say which one.")
    return hits[0] if hits else None


def _names(rows: list[dict], key: str = "name") -> str:
    return "; ".join(str(r.get(key))[:60] for r in rows) or "none yet"


def _lines(sec: str, row: dict) -> str:
    out = []
    for k, (label, _, how) in FIELDS[sec].items():
        v = row.get(k)
        if how in ("radio_kind", "use", "listen_kind", "flag") or v in (None, "", 0):
            continue
        out.append(f"{label}: {v}")
    return "; ".join(out)


def _hm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return (f"{h} h " if h else "") + f"{m} min" if m or not h else f"{h} h"


class Plan:
    """The household radio plan."""

    def __init__(self, store: Store, people: People, now=datetime.now) -> None:
        self.store, self.core, self.now = store, people, now

    # the household
    def household(self, doc: dict, core: list[dict] | None = None) -> tuple[list[dict], bool]:
        """(people with their radio details, whether the list came from the Core). Inside the plan's lock, pass
        *core* fetched before the lock was taken."""
        core = self.core.list() if core is None else core
        if not core:
            return [{**p, "from_core": False} for p in doc["people"]], False
        rows, used = [], set()
        for c in core:
            own = next((p for p in doc["people"] if p.get("core_id") == c["id"]), None) or \
                next((p for p in doc["people"] if not p.get("core_id") and p["name"].lower() == c["name"].lower()
                      and p["id"] not in used), None)
            if own:
                used.add(own["id"])
                rows.append({**own, "name": c["name"], "core_id": c["id"], "from_core": True})
            else:
                rows.append({"id": "core:" + c["id"], "name": c["name"], "core_id": c["id"], "from_core": True,
                             **_blank("people")})
        rows += [{**p, "from_core": False} for p in doc["people"] if p["id"] not in used
                 and not any(p.get("core_id") == c["id"] for c in core)]
        return rows, True

    # channels and check-ins
    @staticmethod
    def channel_text(ch: dict | None, fallback: str = "") -> str:
        if ch is None:
            return fallback or "not set"
        parts = [ch["service"], f"channel {ch['channel']}" if ch["channel"] else "", f"tone {ch['tone']}"
                 if ch["tone"] else ""]
        parts = [p for p in parts if p]
        return ch["name"] + (f" ({', '.join(parts)})" if parts else "")

    @staticmethod
    def _channel(doc: dict, words: str = "", use: str = "") -> dict | None:
        """The channel the words name (exact, then one partial), else the first channel kept for *use*."""
        if words:
            try:
                got = _find(doc["channels"], words, "channel")
            except ValueError:
                got = None
            if got:
                return got
        return next((c for c in doc["channels"] if use and c["use"] == use), None)

    def _channel_words(self, doc: dict, words: str, use: str) -> str:
        ch = self._channel(doc, words, use)
        if ch is None and words:
            return words
        return self.channel_text(ch)

    def schedule(self, doc: dict) -> list[dict]:
        """Each check-in with its minutes filled in and its channels worked out, by time of day."""
        out = []
        for c in sorted(doc["check_ins"], key=lambda c: c["name"]):
            listen = c["listen_minutes"] or NUMBERS["listen_minutes"]
            after = c["backup_after_minutes"] or NUMBERS["backup_after_minutes"]
            out.append({**c, "listen": listen, "after": after,
                        "on": self._channel_words(doc, c["channel"], "primary"),
                        "backup": self._channel_words(doc, c["backup_channel"], "backup")})
        return out

    @staticmethod
    def no_contact(c: dict, next_label: str) -> str:
        said = (f"listen for {c['listen']} minutes. If no contact, after {c['after']} minutes go to the backup channel, "
                f"{c['backup']}." if c["backup"] != "not set" else
                f"listen for {c['listen']} minutes. No backup channel is set.")
        said += f" If still no contact, try again at the next check-in, {next_label}."
        if c["no_contact"]:
            said += " Then: " + c["no_contact"]
        return said[0].upper() + said[1:]

    def now_panel(self, doc: dict) -> dict:
        """The Now panel: when the next check-in is and on which channel, from the clock."""
        now = self.now()
        sched = self.schedule(doc)
        if not sched:
            return {"text": "No check-in times are set yet. Add one, like 08:00 and 18:00.", "next": None}
        mins = now.hour * 60 + now.minute

        def at(c):
            h, m = c["name"].split(":")
            return int(h) * 60 + int(m)

        on = [c for c in sched if (mins - at(c)) % 1440 < c["listen"]]     # a listen time may run past midnight
        later = [c for c in sched if at(c) > mins]
        nxt = later[0] if later else sched[0]
        day = "today" if later else "tomorrow"
        after_i = (sched.index(nxt) + 1) % len(sched)
        after = sched[after_i]
        after_day = day if after_i > sched.index(nxt) else "tomorrow"
        if len(sched) == 1:
            after_day = "tomorrow" if day == "today" else "the day after"
        wait = (at(nxt) - mins) % 1440 or 1440
        lines = []
        if on:
            c = on[0]
            lines.append(f"A check-in is on now: it started at {c['name']}. Listen on {c['on']} until "
                         f"{(at(c) + c['listen']) // 60 % 24:02d}:{(at(c) + c['listen']) % 60:02d}.")
        lines.append(f"Next check-in: {nxt['name']} {day} (in {_hm(wait)}), on {nxt['on']}.")
        lines.append(self.no_contact(nxt, f"{after['name']} {after_day}"))
        return {"text": " ".join(lines), "next": nxt["name"], "day": day, "channel": nxt["on"],
                "on_now": on[0]["name"] if on else ""}

    def cards(self, doc: dict, rows: list[dict] | None = None) -> list[dict]:
        """One pocket card per person: call sign, primary and backup channel, check-in times, what to do."""
        rows = self.household(doc)[0] if rows is None else rows
        sched = self.schedule(doc)
        times = ", ".join(c["name"] for c in sched) or "not set"
        first = sched[0] if sched else None
        out = []
        for p in rows:
            primary = self._channel_words(doc, p.get("primary_channel", ""), "primary")
            backup = self._channel_words(doc, p.get("backup_channel", ""), "backup")
            if first:
                todo = (f"Listen {first['listen']} minutes. No contact after {first['after']} minutes: go to "
                        f"{backup if backup != 'not set' else 'the backup channel (not set)'}. Still none: try at the "
                        f"next check-in.")
            else:
                todo = "No check-in times are set yet."
            out.append({"name": p["name"], "call_sign": p.get("call_sign", ""), "primary": primary, "backup": backup,
                        "check_ins": times, "no_contact": todo})
        return out

    # words
    def _section_text(self, doc: dict, sec: str) -> str:
        title = SECTION_TITLES[sec]
        if sec == "radios":
            body = [f"- {r['name']} ({RADIO_KINDS[r['kind']]}){': ' + _lines(sec, r) if _lines(sec, r) else ''}"
                    for r in doc["radios"]]
        elif sec == "channels":
            body = [f"- {c['name']} ({USES[c['use']]}): {_lines(sec, c) or 'no details yet'}"
                    for u in USES for c in doc["channels"] if c["use"] == u]
        elif sec == "people":
            rows, _ = self.household(doc)
            body = [f"- {p['name']}: {_lines(sec, p) or 'no call sign yet'}" for p in rows]
        elif sec == "check_ins":
            sched = self.schedule(doc)
            body = [f"- {c['name']}: listen {c['listen']} min on {c['on']}; no contact after {c['after']} min: "
                    f"backup {c['backup']}" + (f"; then: {c['no_contact']}" if c["no_contact"] else "")
                    for c in sched]
        elif sec == "listen":
            body = [f"- {s['name']} ({LISTEN_KINDS[s['kind']]}): {_lines(sec, s) or 'no station written yet'}"
                    for s in doc["listen"]]
        elif sec == "messages":
            body = [self._message_line(m) for m in sorted(doc["messages"], key=lambda m: m["when"],
                                                          reverse=True)[:NUMBERS["recent_messages"]]]
        else:
            body = [f"- {p['date']}: {p['what']}{'; to fix: ' + p['fix'] if p['fix'] else ''}"
                    for p in sorted(doc["practice"], key=lambda p: p["date"], reverse=True)[:10]]
        return f"{title}:\n" + ("\n".join(body) if body else "- nothing written down yet")

    @staticmethod
    def _message_line(m: dict) -> str:
        return (f"- {m['when']} from {m['sender'] or '?'} to {m['to'] or '?'}"
                f"{' on ' + m['channel'] if m['channel'] else ''}: {m['message']} "
                f"({'acted on' if m['acted'] else 'not acted on yet'})")

    def view(self) -> dict:
        doc = self.store.load()
        rows, from_core = self.household(doc)
        return {**doc, "household": rows, "people_from_core": from_core, "now": self.now_panel(doc),
                "schedule": self.schedule(doc), "cards": self.cards(doc, rows), "rules": RULES_LINE,
                "today": self.now().date().isoformat(), "numbers": NUMBERS,
                "labels": {"radio_kinds": RADIO_KINDS, "uses": USES, "listen_kinds": LISTEN_KINDS}}

    # tools
    def show_radio_plan(self, section=None) -> str:
        doc = self.store.load()
        if _key(_text(section, "section", 40)) not in ("", "all", "whole", "everything", "plan", "whole_plan"):
            sec = _section(section)
            text = self._section_text(doc, sec)
            return text + ("\n\n" + RULES_LINE if sec in ("channels", "check_ins", "people") else "")
        parts = [self._section_text(doc, s) for s in SECTIONS]
        parts.append("Now: " + self.now_panel(doc)["text"])
        return "\n\n".join(parts + [RULES_LINE])

    def next_check_in(self) -> str:
        try:
            doc = self.store.load()
        except Unreadable as exc:
            raise Unreadable(f"{exc}\n{RULES_LINE}") from None
        return self.now_panel(doc)["text"] + "\n" + RULES_LINE

    def which_channel(self, need=None) -> str:
        """Every answer, a refusal included, ends with the rules line."""
        try:
            return self._which_channel(need)
        except Unreadable as exc:
            raise Unreadable(f"{exc}\n{RULES_LINE}") from None
        except ValueError as exc:
            raise ValueError(f"{exc}\n{RULES_LINE}") from None

    def _which_channel(self, need) -> str:
        doc = self.store.load()
        need = _text(need, "need", 80)
        rows, _ = self.household(doc)
        if not need:
            return self._section_text(doc, "channels") + "\n" + RULES_LINE
        low = need.lower()
        ch = next((c for c in doc["channels"] if c["name"].lower() == low or c["id"] == need), None)
        if ch:
            return f"{self.channel_text(ch)}: used for {USES[ch['use']]}" + \
                (f"; {ch['notes']}" if ch["notes"] else "") + ".\n" + RULES_LINE
        person = next((p for p in rows if p["name"].lower() in _words(need).strip().split()
                       or p["name"].lower() == low or (p.get("call_sign") and p["call_sign"].lower() == low)), None)
        if person:
            card = next(c for c in self.cards(doc, rows) if c["name"] == person["name"])
            return (f"{card['name']}{' (call sign ' + card['call_sign'] + ')' if card['call_sign'] else ''}: "
                    f"primary {card['primary']}; backup {card['backup']}. Check-ins: {card['check_ins']}. "
                    f"{card['no_contact']}\n{RULES_LINE}")
        use, how = _pick(USES, USE_WORDS, need, "need")
        if use != "other":
            hits = [c for c in doc["channels"] if c["use"] == use]
            lines = [f"- {self.channel_text(c)}{': ' + c['notes'] if c['notes'] else ''}" for c in hits]
            if use == "listen_only" or "weather" in low:
                lines += [f"- {s['name']} ({LISTEN_KINDS[s['kind']]}): {s['frequency'] or 'no station written yet'}"
                          for s in doc["listen"] if use == "listen_only" or s["kind"] == "weather"]
            if lines:
                return f"For {USES[use]}:\n" + "\n".join(lines) + "\n" + RULES_LINE
            return (f"No channel in the radio plan is kept for {USES[use]}. The channels are: "
                    f"{_names(doc['channels'])}.\n{RULES_LINE}")
        try:
            ch = _find(doc["channels"], need, "channel")
        except ValueError as exc:
            return f"{exc}\n{RULES_LINE}"
        if ch:
            return f"{self.channel_text(ch)}: used for {USES[ch['use']]}.\n{RULES_LINE}"
        return (f"Nothing in the radio plan matches {need!r}. The channels are: {_names(doc['channels'])}. "
                f"The people are: {_names(rows)}.\n{RULES_LINE}")

    def update_radio_plan(self, section, item=None, fields=None) -> str:
        sec = _section(section)
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            raise ValueError("fields is an object of field names and values")
        if len(fields) > 20:
            raise ValueError("fields holds at most 20 names")
        now = self.now()
        core = self.core.list() if sec == "people" else []       # asked before the lock, never while holding it
        with self.store.lock:
            doc = self.store.load()
            got = {}
            for k, v in fields.items():
                got[_field(sec, k)] = v
            new_name = got.pop("name", None)
            clean, notes = {}, []
            for k, v in got.items():
                clean[k], note = _clean(sec, k, v, now)
                if note:
                    notes.append(f"{FIELDS[sec][k][0]}: {note}")
            key = KEY.get(sec, "name")
            if sec == "check_ins" and item not in (None, ""):
                try:
                    item = _clock(item, "check-in time")
                except ValueError:
                    pass                                       # an id, or a channel's words
            if sec == "check_ins" and new_name is not None:
                new_name = _clock(new_name, "check-in time")
            name = _text(item, "item", 1000)
            if not name and sec == "check_ins" and new_name:
                name, new_name = new_name, None
            if not name:
                raise ValueError(f"say which item in {sec}, by its {'time' if sec == 'check_ins' else key}")
            if sec == "people":
                rows, _ = self.household(doc, core)
                row = _find(rows, name, "person", also=("call_sign",))
                own = next((p for p in doc["people"] if row and p["id"] == row["id"]), None)
                if row and not own:                           # a Core person with no radio details here yet
                    own = {"id": secrets.token_hex(4), "name": row["name"], "core_id": row.get("core_id", ""),
                           **_blank("people")}
                    doc["people"].append(own)
                target = own
            else:
                target = _find(doc[sec], name, ONE[sec], key)
            added = target is None
            if added:
                if sec in ("messages", "practice"):
                    raise ValueError(f"no {sec} entry matches {name!r}; use "
                                     f"{'log_message' if sec == 'messages' else 'log_radio_practice'} to add one")
                if len(doc[sec]) >= LIMITS[sec]:
                    raise ValueError(f"{sec} holds at most {LIMITS[sec]} items")
                if sec == "check_ins":
                    name = _clock(name, "check-in time")
                target = {"id": secrets.token_hex(4), "name": _text(name, "name", NAME_MOST[sec]), **_blank(sec)}
                if sec == "people":
                    target["core_id"] = ""
                if sec in ("radios", "listen") and "kind" not in clean:   # named for its kind, like "Sam's walkie-talkie"
                    kind, how = _pick(RADIO_KINDS, RADIO_WORDS, name, "kind") if sec == "radios" else                         _pick(LISTEN_KINDS, LISTEN_WORDS, name, "kind")
                    if kind != "other":
                        clean["kind"] = kind
                        notes.append(f"kind: {how}")
                doc[sec].append(target)
            if new_name is not None:
                renamed = _text(new_name, "name", NAME_MOST.get(sec, 60))
                if not renamed:
                    raise ValueError("a name can not be empty")
                if sec == "people" and target.get("core_id"):
                    raise ValueError("this person's name comes from the Core's people list; change it there")
                target["name"] = renamed
            target.update(clean)
            self.store.save(doc)
        what = ", ".join(FIELDS[sec][k][0] for k in clean) or ("added" if added else "name")
        said = f"Saved in {SECTION_TITLES[sec]}: {'added ' if added else ''}{str(target.get(key))[:80]} ({what})."
        if notes:
            note = "; ".join(notes)
            said += " " + note[0].upper() + note[1:] + "."
        if sec in ("channels", "people", "check_ins"):
            said += " " + RULES_LINE
        return said

    def log_message(self, message=None, sender=None, to=None, channel=None, acted=None, when=None) -> str:
        now = self.now()
        entry = {"id": secrets.token_hex(4), "when": _when(when, now),
                 "sender": _text(sender, "from", 60), "to": _text(to, "to", 60),
                 "channel": _text(channel, "channel", 60), "message": _text(message, "message", 1000, True),
                 "acted": False if acted in (None, "") else _flag(acted, "acted on")}
        if not entry["message"]:
            raise ValueError("say what the message was")
        with self.store.lock:
            doc = self.store.load()
            if len(doc["messages"]) >= LIMITS["messages"]:
                raise ValueError(f"the message log holds at most {LIMITS['messages']} messages; remove old ones first")
            doc["messages"].append(entry)
            self.store.save(doc)
        return "Logged: " + self._message_line(entry)[2:] + "."

    def list_messages(self, who=None, channel=None, count=None) -> str:
        doc = self.store.load()
        who, channel = _text(who, "who", 60).lower(), _text(channel, "channel", 60).lower()
        try:
            n = NUMBERS["recent_messages"] if count in (None, "") else _minutes(count, "count")
        except ValueError:
            raise ValueError(f"count is a number from 1 to {NUMBERS['most_messages_shown']}") from None
        n = min(n, NUMBERS["most_messages_shown"])
        rows = sorted(doc["messages"], key=lambda m: m["when"], reverse=True)
        if who:
            rows = [m for m in rows if who in m["sender"].lower() or who in m["to"].lower()]
        if channel:
            rows = [m for m in rows if channel in m["channel"].lower()]
        if not rows:
            return "No radio messages" + (f" from or to {who}" if who else "") + (f" on {channel}" if channel else "") \
                + " in the log."
        open_n = sum(1 for m in rows if not m["acted"])
        return (f"Radio messages, newest first ({len(rows)} in all, {open_n} not acted on yet):\n"
                + "\n".join(self._message_line(m) for m in rows[:n]))

    def log_radio_practice(self, what=None, date=None, fix=None) -> str:
        today = self.now().date()
        what = _text(what, "what", 300)
        if not what:
            raise ValueError("say what was practiced")
        day = _day(date, "date", today) or today
        if day > today:
            raise ValueError("date can not be a day still to come")
        if (today - day).days > 3650:
            raise ValueError("date is at most 10 years back")
        with self.store.lock:
            doc = self.store.load()
            if len(doc["practice"]) >= LIMITS["practice"]:
                raise ValueError(f"the practice log holds at most {LIMITS['practice']} entries")
            doc["practice"].append({"id": secrets.token_hex(4), "date": day.isoformat(), "what": what,
                                    "fix": _text(fix, "what to fix", 1000, True)})
            self.store.save(doc)
        return f"Logged radio practice on {day.isoformat()}: {what}." + (f" To fix: {_text(fix, 'fix', 1000, True)}."
                                                                         if fix else "")

    def remove_radio_item(self, section=None, item=None) -> str:
        sec = _section(section)
        core = self.core.list() if sec == "people" else []       # asked before the lock, never while holding it
        key = KEY.get(sec, "name")
        if sec == "check_ins" and item not in (None, ""):
            try:
                item = _clock(item)
            except ValueError:
                pass
        with self.store.lock:
            doc = self.store.load()                                # read first: a file that will not read stops here
            if sec == "people":
                rows, _ = self.household(doc, core)
                row = _find(rows, item, "person", also=("call_sign",))
                own = next((p for p in doc["people"] if row and p["id"] == row["id"]), None)
                if own is None:
                    raise ValueError(f"no person called {item!r} has radio details in the plan; the people with "
                                     f"details are: {_names(doc['people'])}")
                doc["people"] = [p for p in doc["people"] if p is not own]
                said = f"removed {own['name']}'s radio details" + (" (they stay in the Core's people list)"
                                                                   if row.get("from_core") else "")
            else:
                x = _find(doc[sec], item, ONE[sec], key)
                if x is None:
                    raise ValueError(f"{SECTION_TITLES[sec]} has nothing called {item!r}. It has: "
                                     f"{_names(doc[sec], key)}. Nothing was removed.")
                doc[sec] = [y for y in doc[sec] if y is not x]
                said = f"removed {str(x.get(key))[:80]}"
            self.store.save(doc)
        return f"{SECTION_TITLES[sec]}: {said}."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_SECTION = _p("string", "One of radios, channels, people, check_ins, listen, messages, practice.")
TOOLS = [
    ("show_radio_plan", "Use to read out the household radio plan for when phones fail: the whole plan, or one "
     "section (radios, channels, people and call signs, check-in times, listen-only stations, practice). "
     "For the message log, use list_messages.",
     {"section": _p("string", "Optional: one section. Default the whole plan.")}, []),
    ("next_check_in", "Use when someone asks when the next radio check-in is: says from the clock when it is, on "
     "which channel, how long to listen, and what to do if no one answers.", {}, []),
    ("which_channel", "Use when someone asks which radio channel to use, by name; no need to look it up first: for a "
     "purpose (primary, backup, neighbors, emergency, listen only, weather) or for a person (their call sign, primary "
     "and backup channel).",
     {"need": _p("string", "A purpose, a person's name or call sign, or a channel's label, like neighbors or Sam.")},
     []),
    ("update_radio_plan", "Use when the owner tells you to set or change something in the radio plan, by name; no need "
     "to look it up first: a radio, a channel, a person's call sign or license, a check-in time, or a listen-only "
     "station. Something not there yet is added. Kinds can be everyday words, like walkie-talkie.",
     {"section": _SECTION,
      "item": _p("string", "Which item: a radio's or channel's name (like Family 1), a person's name, a check-in "
                           "time (like 18:00), or a station's name."),
      "fields": {"type": "object", "description": "Field names and values to set, like {\"call_sign\": \"Bluebird\"} "
                 "for a person, {\"service\": \"FRS\", \"channel\": \"1\", \"use\": \"primary\"} for a channel, "
                 "{\"kind\": \"walkie-talkie\", \"carrier\": \"Sam\"} for a radio, {\"channel\": \"Family 1\", "
                 "\"listen_minutes\": 5, \"backup_after_minutes\": 10} for a check-in, {\"frequency\": \"...\"} for a "
                 "station, {\"name\": \"...\"} to rename."}},
     ["section"]),
    ("log_message", "Use when someone says a radio message came in or went out: adds it to the radio message log with "
     "the time, who it was from and to, the channel, the words, and whether it was acted on.",
     {"message": _p("string", "The message, in the words heard or sent."),
      "from": _p("string", "Optional: who sent it."), "to": _p("string", "Optional: who it was for."),
      "channel": _p("string", "Optional: the channel, like Family 1."),
      "acted": _p("boolean", "Optional: true when it was acted on. Default false."),
      "when": _p("string", "Optional: now, a time today like 18:30, or 2027-03-31 18:30. Default now.")},
     ["message"]),
    ("list_messages", "Use to read back the radio message log: the latest messages, or those from or to one person, "
     "or on one channel, newest first.",
     {"who": _p("string", "Optional: a person, for messages from or to them."),
      "channel": _p("string", "Optional: a channel."),
      "count": _p("integer", "Optional: how many, default 10.")}, []),
    ("log_radio_practice", "Use when the household did a radio drill or test: logs the date, what was practiced and "
     "what to fix.",
     {"what": _p("string", "What was practiced, like evening check-in on the backup channel."),
      "date": _p("string", "Optional: the day, like 2027-03-31 or yesterday. Default today."),
      "fix": _p("string", "Optional: what to fix next time.")}, ["what"]),
    ("remove_radio_item", "Use when the owner asks to take something out of the radio plan, by its name as they said "
     "it; no need to look it up or show the plan first, the app finds it: a radio, a channel, a person's radio "
     "details, a check-in, a listen-only station, a message or a practice entry. It deletes it.",
     {"section": _SECTION, "item": _p("string", "Which item, by its name, time or words.")}, ["section", "item"]),
]
CHANGES = ["update_radio_plan", "log_message", "log_radio_practice", "remove_radio_item"]
DELETES = ["remove_radio_item"]
ARG_NAMES = {"from": "sender"}          # a tool argument the code reads under another name


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Plan, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object."}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if isinstance(asked, str) and asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "No tool by that name."}}
        try:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            allowed = next(t[2] for t in TOOLS if t[0] == name)
            extra = [k for k in args if k not in allowed]
            if extra:
                raise TypeError(f"{name} does not take {', '.join(map(str, extra))}")
            text = getattr(app, name)(**{ARG_NAMES.get(k, k): v for k, v in args.items()})
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Radio Plan does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/update", "/api/remove", "/api/message", "/api/practice")


class Handler(BaseHTTPRequestHandler):
    server_version = "RadioPlan/1"
    app: Plan
    token: str
    timeout = 15        # seconds; a stalled sender is cut off

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"), extra=extra)

    def _declared(self) -> int | None:
        """Content-Length as sent: 0 when missing, None when it is not a plain number."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 15:
            return None
        return int(raw)

    def _length(self, most: int = MAX_BODY) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most *most* bytes."""
        n = self._declared()
        if n is None:
            raise ValueError("Content-Length must be a whole number of bytes.")
        if n > most:
            raise ValueError(f"The request body is larger than {most // 1024} KB.")
        return n

    def _drain(self, most: int = MAX_DRAIN, wait_s: float = DRAIN_WAIT_S) -> None:
        """Read and drop a body that will be refused (at most *most* bytes within *wait_s* seconds), so the sender
        gets the answer, not a reset connection. The connection closes after the answer."""
        self.close_connection = True
        n = min(self._declared() or 0, most)
        end = time.monotonic() + wait_s
        try:
            while n > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.1, end - time.monotonic()))
                got = self.rfile.read1(min(n, 64 * 1024))
                if not got:
                    break
                n -= len(got)
        except OSError:
            pass

    def _body(self):
        try:
            n = self._length()
        except ValueError:
            self._drain()
            raise
        raw = bytearray()
        end = time.monotonic() + BODY_WAIT_S
        try:
            while len(raw) < n:                      # the whole body within BODY_WAIT_S, however it trickles in
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(n - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError:
            self.close_connection = True
            raise ValueError(f"The request body did not all arrive within {BODY_WAIT_S:g} seconds.") from None
        finally:
            self.connection.settimeout(self.timeout)
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        return _loads(bytes(raw)) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        self._drain(NO_TOKEN_DRAIN, NO_TOKEN_WAIT_S)          # a caller without the token is never drained for long
        self._json(401, {"error": "Radio Plan needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Radio Plan stopped on this request with an error ({type(exc).__name__})."})
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
                return self._json(200, self.app.store.load())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
            if path == "/view":
                return self._json(200, self.app.view())
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Radio Plan has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            self._drain()
            return self._json(404, {"error": "Radio Plan has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path == "/api/update":
                out = {"text": app.update_radio_plan(body.get("section"), body.get("item"), body.get("fields"))}
            elif path == "/api/remove":
                out = {"text": app.remove_radio_item(body.get("section"), body.get("item"))}
            elif path == "/api/message":
                out = {"text": app.log_message(body.get("message"), body.get("from"), body.get("to"),
                                               body.get("channel"), body.get("acted"), body.get("when"))}
            else:
                out = {"text": app.log_radio_practice(body.get("what"), body.get("date"), body.get("fix"))}
            out["last_updated"] = app.store.load()["last_updated"]
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self):
        if self._refused():
            return
        self._drain()
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        self.do_PUT()

    def _mcp(self):
        try:
            msg = self._body()
        except ValueError as exc:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, now=datetime.now, api: str | None = None,
                people_ttl_s: float = 60) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Plan(Store(data, now), People(api, token, people_ttl_s, min(30, people_ttl_s)), now)
    handler = type("RadioHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Radio Plan for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Radio Plan with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), api=os.environ.get("PUFFPAI_API"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
