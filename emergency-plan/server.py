"""Emergency Plan: the household's emergency plan, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/emergency-plan/
- ``/health``       "ok"
- ``/data``         the saved plan (GET), ``/updated`` its stamp
- ``/view``         the plan with the household merged in, the readiness score and the review note (GET)
- ``/export``       the whole plan as one JSON file to download (GET)
- ``/api/update``, ``/api/remove``, ``/api/grab``, ``/api/practice``, ``/api/import``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/emergency-plan/, which adds it.

The plan is one JSON document in PUFFPAI_DATA (``plan.json``). Writes replace the
whole file at once (temp file, then rename). A file that exists but will not read,
an empty one included, is never written over. The household comes from the Core's
shared people list (``GET /v1/people`` on PUFFPAI_API, this computer only) when it
answers, and from the plan's own list otherwise. How the readiness score is worked
out, when the review note shows and how import works: PROCESS.md.

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
import unicodedata
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "emergency-plan"
VERSION = "1.0.1"
DOC = "plan.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
REVIEW_DAYS = 183                       # about 6 months
DRILL_DAYS = 365
LIMITS = {"people": 30, "contacts": 100, "pets": 30, "grab_list": 100, "practice": 500}

# ---------------------------------------------------------------- the plan's shape
# Each field: (label, most characters, more than one line allowed)
PERSON = {"mobile": ("mobile number", 40, False), "work_school": ("work or school", 120, False),
          "work_address": ("work or school address", 200, False), "work_phone": ("work or school number", 40, False),
          "medical": ("medical needs", 1000, True), "medicines": ("medicines that must go with them", 1000, True),
          "allergies": ("allergies", 300, False), "blood_type": ("blood type", 10, False),
          "notes": ("a helper should know", 1000, True)}
CONTACT = {"kind": ("kind", 20, False), "phone": ("number", 40, False), "address": ("address", 200, False),
           "policy_number": ("policy number", 60, False), "claims_number": ("claims number", 40, False),
           "notes": ("notes", 500, True)}
PLACE = {"place": ("place", 120, False), "address": ("address", 200, False), "note": ("note", 500, True)}
HOME = {"water_shutoff": ("water shutoff", 300, False), "water_how": ("how to turn the water off", 1000, True),
        "gas_shutoff": ("gas shutoff", 300, False), "gas_how": ("how to turn the gas off", 1000, True),
        "breaker": ("main breaker", 300, False), "breaker_how": ("how to turn the power off", 1000, True),
        "extinguishers": ("fire extinguishers", 300, False), "first_aid": ("first aid kit", 300, False),
        "go_bags": ("go-bags", 300, False), "spare_keys": ("spare keys", 300, False),
        "papers": ("important papers", 300, False), "safe_storm": ("safe spot in a storm", 300, False),
        "safe_quake": ("safe spot in an earthquake", 300, False),
        "ways_out": ("two ways out of each sleeping room", 1000, True)}
LEAVING = {"route_1": ("first way out of the area", 1000, True), "route_2": ("second way out of the area", 1000, True),
           "stay": ("where we would stay", 500, True), "transport": ("transport", 500, True)}
PET = {"kind": ("kind", 60, False), "chip": ("chip number", 60, False), "vet": ("vet", 200, False),
       "carrier": ("carrier is at", 200, False), "food_meds": ("food and medicine", 1000, True)}
HAZARD = {"applies": ("applies here", 0, False), "note": ("what we do", 1000, True)}
PRACTICE = {"date": ("date", 10, False), "kind": ("kind", 10, False), "what": ("what was practiced", 300, False),
            "fix": ("what to fix", 1000, True)}
GRAB = {"done": ("packed", 0, False)}
FIELDS = {"people": PERSON, "contacts": CONTACT, "meeting_places": PLACE, "home": HOME, "leaving": LEAVING,
          "pets": PET, "hazards": HAZARD, "practice": PRACTICE, "grab_list": GRAB}

CONTACT_KINDS = {"out_of_area": "out-of-area contact", "helper": "nearby helper", "doctor": "doctor", "vet": "vet",
                 "school": "school", "work": "work", "insurance": "insurance", "utility": "utility", "other": "other"}
KIND_HELP = {"out_of_area": "the person outside the area everyone checks in with",
             "helper": "a neighbor or nearby helper", "doctor": "a doctor, pediatrician, dentist or clinic",
             "vet": "the pets' vet", "school": "a school or child care", "work": "a workplace",
             "insurance": "an insurance company or agent", "utility": "the power, gas or water company",
             "other": "anyone else"}
# Words that point to a contact's kind, in order: the first kind with a word in the text wins.
KIND_WORDS = (("out_of_area", ("out of area", "out of town", "out of state", "check in", "checkin")),
              ("vet", ("vet", "vets", "veterinarian", "veterinary")),
              ("doctor", ("doctor", "doctors", "dr", "pediatrician", "dentist")),
              ("insurance", ("insurance", "insurer")),
              ("school", ("school",)),
              ("utility", ("utility", "utilities", "power company", "electric company", "gas company",
                           "water company")),
              ("helper", ("neighbor", "neighbour", "neighbors", "neighbours", "helper", "nearby helper")),
              ("work", ("work",)))
KIND_FILLER = {"our", "the", "my", "a", "an", "contact", "contacts", "person", "emergency", "number", "phone", "s"}
PEOPLE_DEADLINE_S = 2.0                 # the whole fetch of the Core's people list
BODY_WAIT_S = 5.0                       # the whole wait for a request body
REFUSED_MOST, REFUSED_S = 64 * 1024, 2.0            # what a request without the token may send before its 401
DROP_MOST, DROP_S = 64 * 1024 * 1024, 20.0          # a body over the cap is read and dropped, then a plain 400
IDLE_S = 1.0                                        # dropping stops when nothing more arrives for this long
MEETING = {"outside_home": "right outside the home", "neighborhood": "in the neighborhood",
           "out_of_neighborhood": "out of the neighborhood", "out_of_town": "out of town"}
HAZARDS = {"power_outage": "power outage", "severe_storm": "severe storm", "flood": "flood",
           "winter_storm": "winter storm", "wildfire": "wildfire", "earthquake": "earthquake",
           "hurricane": "hurricane", "tornado": "tornado", "extreme_heat": "extreme heat", "other": "other"}
STARTER_GRAB = ["Important papers (IDs, insurance, copies)", "Medicines", "Phone chargers", "Cash",
                "Keys", "Pet supplies", "Glasses"]
SECTIONS = ("people", "contacts", "meeting_places", "home", "leaving", "grab_list", "pets", "hazards", "practice")
SECTION_TITLES = {"people": "People", "contacts": "Contacts", "meeting_places": "Meeting places", "home": "Home",
                  "leaving": "Leaving", "grab_list": "Grab list", "pets": "Pets", "hazards": "Hazards",
                  "practice": "Practice"}
SECTION_WORDS = {"person": "people", "household": "people", "family": "people", "contact": "contacts",
                 "meeting": "meeting_places", "meeting_place": "meeting_places", "meet": "meeting_places",
                 "places": "meeting_places", "house": "home", "shutoffs": "home", "leave": "leaving",
                 "evacuation": "leaving", "evacuate": "leaving", "grab": "grab_list", "checklist": "grab_list",
                 "grab_list_item": "grab_list", "pet": "pets", "hazard": "hazards", "drill": "practice",
                 "drills": "practice", "review": "practice", "reviews": "practice"}
LIST_SECTIONS = ("people", "contacts", "pets", "grab_list", "practice")
TRUE_WORDS = {"true", "yes", "y", "1", "on", "tick", "ticked", "done", "packed", "applies", "check", "checked"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "untick", "unticked", "not packed", "clear", "unchecked"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _key(word) -> str:
    return "_".join(str(word).lower().replace("-", " ").split())


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
    if isinstance(value, str) and value.strip().lower() in TRUE_WORDS | FALSE_WORDS:
        return value.strip().lower() in TRUE_WORDS
    raise ValueError(f"{name} is yes or no")


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


def _loads(text: str):
    """JSON, refusing huge numbers, NaN and Infinity, and nesting too deep to read, as plain ValueErrors."""
    try:
        return json.loads(text, parse_constant=_no_constant, parse_int=_int, parse_float=_float)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None


def _section(value) -> str:
    key = _key(_text(value, "section", 40))
    key = SECTION_WORDS.get(key, key)
    if key not in SECTIONS:
        raise ValueError("section is one of " + ", ".join(SECTIONS))
    return key


def _field(section: str, word) -> str:
    """A field of *section* by its name or label words (like 'phone' or 'water shutoff')."""
    fields = FIELDS[section]
    key = _key(word)
    if key in fields:
        return key
    aliases = {"phone": "mobile" if section == "people" else "phone", "number": "mobile" if section == "people"
               else "phone", "cell": "mobile", "medicine": "medicines", "allergy": "allergies", "blood": "blood_type",
               "school": "work_school", "work": "work_school", "name_of_place": "place", "notes": "note"
               if section in ("meeting_places", "hazards") else "notes", "food": "food_meds",
               "chip_number": "chip", "water": "water_shutoff", "gas": "gas_shutoff", "power": "breaker",
               "main_breaker": "breaker", "electric": "breaker", "first_aid_kit": "first_aid",
               "extinguisher": "extinguishers", "fire_extinguishers": "extinguishers", "go_bag": "go_bags",
               "keys": "spare_keys", "important_papers": "papers", "storm": "safe_storm", "earthquake": "safe_quake",
               "exits": "ways_out", "route": "route_1", "routes": "route_1", "stay_at": "stay", "where_we_stay": "stay",
               "policy": "policy_number", "claims": "claims_number", "packed": "done", "what_to_fix": "fix",
               "type": "kind"}
    key = aliases.get(key, key)
    if key in fields:
        return key
    for k, (label, _, _) in fields.items():
        if _key(label) == _key(word):
            return k
    raise ValueError(f"{section} has no field {word!r}; its fields are " + ", ".join(fields))


def _slot(table: dict, value, what: str) -> str:
    key = _key(_text(value, what, 60))
    words = {"outside": "outside_home", "outside_the_home": "outside_home", "home": "outside_home",
             "near": "neighborhood", "neighbourhood": "neighborhood", "out_of_neighbourhood": "out_of_neighborhood",
             "outside_the_neighborhood": "out_of_neighborhood", "town": "out_of_town", "out_of_area": "out_of_town",
             "power": "power_outage", "outage": "power_outage", "blackout": "power_outage", "storm": "severe_storm",
             "fire": "wildfire", "heat": "extreme_heat", "heat_wave": "extreme_heat", "snow": "winter_storm",
             "quake": "earthquake", "flooding": "flood"} if table is not CONTACT_KINDS else \
        {"out_of_area_contact": "out_of_area", "out_of_state": "out_of_area", "neighbor": "helper",
         "neighbour": "helper", "helpers": "helper", "nearby_helper": "helper", "doctors": "doctor",
         "veterinarian": "vet", "utilities": "utility", "insurer": "insurance"}
    key = words.get(key, key)
    for k, label in table.items():
        if key in (k, _key(label)):
            return k
    raise ValueError(f"{what} is one of " + ", ".join(table.values()))


def _fold(text) -> str:
    """Lower case with accents folded away (é is e, ü is u); letters of every script stay letters."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _words(text) -> str:
    """Lower-case words with single spaces around them, so ' dr ' or ' out of area ' can be looked for."""
    return " " + " ".join(_name_words(text)) + " "


def _infer_kind(*texts) -> str | None:
    """The contact kind the words point to (like 'our check-in person' -> out_of_area), or None."""
    for text in texts:
        words = _words(text)
        for kind, hints in KIND_WORDS:
            if any(f" {h} " in words for h in hints):
                return kind
    return None


def _kind_phrase(text) -> str | None:
    """The kind when the words name only a kind (like 'the out-of-area contact'), else None."""
    left = " ".join(w for w in _words(text).split() if w not in KIND_FILLER)
    for kind, hints in KIND_WORDS:
        if left in hints or left == kind.replace("_", " ") or left == _words(CONTACT_KINDS[kind]).strip():
            return kind
    return None


def _contact_kind(value) -> tuple[str, bool]:
    """(kind, whether the words named a kind outright). Words that name no kind give the best guess or other."""
    try:
        return _slot(CONTACT_KINDS, value, "kind"), True
    except ValueError:
        return _infer_kind(_text(value, "kind", 60)) or "other", False


def _clean_item(section: str, raw: dict, today: date, name_most: int = 80, at: int = 0) -> dict:
    """One list item, every field checked; used for import and for the file on load. An item with no id
    gets one made from its section, place and name, the same on every load, and saved on the next write."""
    if not isinstance(raw, dict):
        raise ValueError(f"each {section} item must be an object")
    made = hashlib.sha256(f"{section}\n{at}\n{raw.get('name', raw.get('what', ''))}".encode("utf-8")).hexdigest()
    out = {"id": _text(raw.get("id"), "id", 40) or "i" + made[:8]}
    if section != "practice":
        out["name"] = _text(raw.get("name"), "name", name_most)
        if not out["name"]:
            raise ValueError(f"each {section} item needs a name")
    for k, (label, most, lines) in FIELDS[section].items():
        v = raw.get(k)
        if k == "done":
            out[k] = bool(v) if isinstance(v, bool) else False if v is None else _flag(v, label)
        elif section == "contacts" and k == "kind":
            out[k] = _contact_kind(v)[0] if v not in (None, "") else "other"
        elif section == "practice" and k == "date":
            d = _as_date(v)
            if d is None:
                raise ValueError("each practice entry needs a date like 2027-03-31")
            out[k] = d.isoformat()
        elif section == "practice" and k == "kind":
            out[k] = "review" if str(v or "").lower() == "review" else "drill"
        else:
            out[k] = _text(v, label, most, lines)
    if section == "people":
        out["core_id"] = _text(raw.get("core_id"), "core_id", 80)
    return out


def default_doc() -> dict:
    return {"app": NAME, "format": 1, "people": [], "contacts": [],
            "meeting_places": {k: {f: "" for f in PLACE} for k in MEETING},
            "home": {k: "" for k in HOME}, "leaving": {k: "" for k in LEAVING},
            "grab_list": [{"id": f"g{i}", "name": n, "done": False} for i, n in enumerate(STARTER_GRAB, 1)],
            "pets": [], "hazards": {k: {"applies": False, "note": ""} for k in HAZARDS},
            "practice": [], "last_reviewed": "", "last_updated": ""}


def normalize(raw, today: date) -> dict:
    """A whole plan, every part checked and capped. ValueError says what does not fit."""
    if not isinstance(raw, dict):
        raise ValueError("a plan is a JSON object")
    doc = default_doc()
    doc["grab_list"] = []
    for sec in LIST_SECTIONS:
        items = raw.get(sec, [] if sec != "grab_list" else None)
        if items is None:
            doc[sec] = default_doc()["grab_list"]
            continue
        if not isinstance(items, list):
            raise ValueError(f"{sec} must be a list")
        if len(items) > LIMITS[sec]:
            raise ValueError(f"{sec} holds at most {LIMITS[sec]} items")
        doc[sec] = [_clean_item(sec, x, today, 60 if sec in ("people", "pets") else 120, i)
                    for i, x in enumerate(items)]
    for sec, table, shape in (("meeting_places", MEETING, PLACE), ("hazards", HAZARDS, HAZARD)):
        got = raw.get(sec, {})
        if not isinstance(got, dict):
            raise ValueError(f"{sec} must be an object")
        for k in table:
            one = got.get(k, {})
            if not isinstance(one, dict):
                raise ValueError(f"{sec} {k} must be an object")
            for f, (label, most, lines) in shape.items():
                doc[sec][k][f] = (bool(one.get(f)) if isinstance(one.get(f), bool) else False) if f == "applies" \
                    else _text(one.get(f), label, most, lines)
    for sec, shape in (("home", HOME), ("leaving", LEAVING)):
        got = raw.get(sec, {})
        if not isinstance(got, dict):
            raise ValueError(f"{sec} must be an object")
        for f, (label, most, lines) in shape.items():
            doc[sec][f] = _text(got.get(f), label, most, lines)
    reviewed = raw.get("last_reviewed")
    doc["last_reviewed"] = _as_date(reviewed).isoformat() if _as_date(reviewed) else ""
    doc["last_updated"] = _text(raw.get("last_updated"), "last_updated", 40)
    return doc


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved plan exists but will not read. Nothing writes over it."""


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str, today) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()
        self.today = today

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return default_doc()
            try:
                raw = self.path.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("the file is empty")
                got = _loads(raw)
            except (OSError, ValueError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Emergency Plan will not write over it. "
                                 f"Move that file somewhere else to start a new plan, or put a good copy back.") from None
            if not isinstance(got, dict) or got.get("app") != NAME:
                raise Unreadable(f"{self.path} is not this app's file: it has no Emergency Plan marker. Emergency Plan "
                                 f"will not write over it. Move that file somewhere else to start a new plan.")
            try:
                return normalize(got, self.today())
            except (OSError, ValueError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Emergency Plan will not write over it. "
                                 f"Move that file somewhere else to start a new plan, or put a good copy back.") from None

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".plan-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]

    def backup(self) -> str:
        """Copy the plan as it is now to a dated file in the data folder; '' when there is no plan file yet."""
        with self.lock:
            if not self.path.exists():
                return ""
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            for n in range(100):
                name = f"plan-backup-{stamp}{'' if not n else '-' + str(n)}.json"
                try:
                    with open(self.folder / name, "xb") as f:
                        f.write(self.path.read_bytes())
                    return name
                except FileExistsError:
                    continue
            raise OSError("no free backup file name")


# ---------------------------------------------------------------- people

class People:
    """The Core's shared people list (``GET /v1/people``, D12), read with this package's token.
    Only PUFFPAI_API on this computer is ever asked; any other address is never dialled."""

    def __init__(self, api: str | None, token: str | None, ttl_s: float = 60, fail_ttl_s: float = 30) -> None:
        self.token, self.ttl_s, self.fail_ttl_s = token or "", ttl_s, fail_ttl_s
        self.where = None
        parts = urlsplit((api or "").strip())
        try:
            if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and parts.port:
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
        """GET /v1/people, all of it within PEOPLE_DEADLINE_S seconds or an error."""
        host, port, prefix = self.where
        end = time.monotonic() + PEOPLE_DEADLINE_S
        buf = bytearray()
        with socket.create_connection((host, port), timeout=PEOPLE_DEADLINE_S) as s:
            s.sendall((f"GET {prefix}/v1/people HTTP/1.0\r\nHost: {host}:{port}\r\nAuthorization: Bearer {self.token}"
                       f"\r\nAccept: application/json\r\nConnection: close\r\n\r\n").encode("latin-1"))
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
                size = next((v.strip() for k, _, v in (h.partition(b":") for h in head.split(b"\r\n")[1:])
                             if k.strip().lower() == b"content-length"), b"") if sep else b""
                if size.isdigit() and len(size) <= 12 and len(body) >= int(size):
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
        data = _loads(body.decode("utf-8"))
        if not isinstance(data, list):
            raise ValueError("the Core's people list is not a list")
        return [{"id": _text(p.get("id"), "id", 80), "name": _text(p.get("name") or p.get("id"), "name", 60)}
                for p in data[:LIMITS["people"]] if isinstance(p, dict) and p.get("id")]


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

def _name_words(text) -> list[str]:
    """The lower-case words of a name, accents folded: letters and digits of any script, everything else a gap."""
    return "".join(c if c.isalnum() else " " for c in _fold(text)).split()


def _words_fit(asked: list[str], name) -> bool:
    """Each word asked equals a word of the name or starts one (three letters at least); never letters inside a word."""
    have = _name_words(name)
    return bool(asked) and all(any(h == w or (len(w) >= 3 and h.startswith(w)) for h in have) for w in asked)


def _find(rows: list[dict], ident, what: str, key: str = "name", partial: bool = True) -> dict | None:
    """An exact id or name, then (with *partial*) the one name whose words the words given equal or start;
    None when nothing matches; several is a ValueError naming them, and nothing changes."""
    text = _text(ident, what, 120)
    if not text:
        raise ValueError(f"say which {what}")
    low = text.lower()
    for r in rows:
        if r.get("id") == text or str(r.get(key, "")).lower() == low:
            return r
    same = " ".join(_fold(text).split())                 # the whole name typed with or without its accents
    hits = [r for r in rows if " ".join(_fold(r.get(key, "")).split()) == same]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(f"more than one {what} matches {text!r}: " + ", ".join(str(r.get(key)) for r in hits)
                         + ". Say the whole name; nothing was changed")
    if not partial:
        return None
    asked = _name_words(text)
    hits = [r for r in rows if _words_fit(asked, r.get(key, ""))]
    if len(hits) > 1:
        raise ValueError(f"more than one {what} matches {text!r}: " + ", ".join(str(r.get(key)) for r in hits)
                         + ". Say the whole name; nothing was changed")
    return hits[0] if hits else None


def _using(row: dict, ident, key: str = "name") -> str:
    """'Using 'X'. ' when *row* was found by part of its name, '' when the name or id was given whole."""
    text = str(ident or "").strip()
    if row.get("id") == text or " ".join(str(row.get(key, "")).lower().split()) == " ".join(text.lower().split()):
        return ""
    return f"Using '{row.get(key)}'. "


def _lines(fields: dict, row: dict) -> str:
    return "; ".join(f"{label}: {row[k]}" for k, (label, _, _) in fields.items()
                     if k not in ("kind", "done", "applies") and row.get(k))


class Plan:
    """The household emergency plan."""

    def __init__(self, store: Store, people: People, today=date.today) -> None:
        self.store, self.core, self.today = store, people, today

    # the household
    def household(self, doc: dict, core: list[dict] | None = None) -> tuple[list[dict], bool]:
        """(people with their details, whether the list came from the Core). Inside the plan's lock, pass
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
                             **{k: "" for k in PERSON}})
        rows += [{**p, "from_core": False} for p in doc["people"] if p["id"] not in used
                 and not any(p.get("core_id") == c["id"] for c in core)]
        return rows, True

    # words
    def _section_text(self, doc: dict, sec: str) -> str:
        title = SECTION_TITLES[sec]
        if sec == "people":
            rows, _ = self.household(doc)
            body = [f"- {p['name']}: {_lines(PERSON, p) or 'no details yet'}" for p in rows]
        elif sec == "contacts":
            body = [f"- {c['name']} ({CONTACT_KINDS[c['kind']]}): {_lines(CONTACT, c) or 'no number yet'}"
                    for k in CONTACT_KINDS for c in doc["contacts"] if c["kind"] == k]
        elif sec == "meeting_places":
            body = [f"- {MEETING[k]}: {_lines(PLACE, p)}" for k, p in doc["meeting_places"].items()
                    if p["place"] or p["address"]]
        elif sec in ("home", "leaving"):
            fields = HOME if sec == "home" else LEAVING
            body = [f"- {label}: {doc[sec][k]}" for k, (label, _, _) in fields.items() if doc[sec][k]]
        elif sec == "grab_list":
            body = [f"- [{'x' if g['done'] else ' '}] {g['name']}" for g in doc["grab_list"]]
        elif sec == "pets":
            body = [f"- {p['name']}{' (' + p['kind'] + ')' if p['kind'] else ''}: {_lines(PET, p) or 'no details yet'}"
                    for p in doc["pets"]]
        elif sec == "hazards":
            body = [f"- {HAZARDS[k]}: {h['note'] or 'no note yet'}" for k, h in doc["hazards"].items() if h["applies"]]
        else:
            body = [f"- Last reviewed: {doc['last_reviewed'] or 'never'}"]
            body += [f"- {p['date']} {p['kind']}: {p['what']}{'; to fix: ' + p['fix'] if p['fix'] else ''}"
                     for p in sorted(doc["practice"], key=lambda p: p["date"], reverse=True)[:10]]
        return f"{title}:\n" + ("\n".join(body) if body else "- nothing written down yet")

    def readiness(self, doc: dict) -> dict:
        today = self.today()
        rows, _ = self.household(doc)
        places = [p for p in doc["meeting_places"].values() if p["place"] or p["address"]]
        reviewed = _as_date(doc["last_reviewed"])                 # a date later than today does not count
        drills = [d for d in (_as_date(p["date"]) for p in doc["practice"] if p["kind"] == "drill")
                  if d and 0 <= (today - d).days <= DRILL_DAYS]
        no_number = [p["name"] for p in rows if not p.get("mobile")]
        checks = [
            ("an out-of-area contact with a number",
             any(c["kind"] == "out_of_area" and c["phone"] for c in doc["contacts"]), ""),
            ("at least two meeting places", len(places) >= 2, f"{len(places)} written down"),
            ("every person has a number", bool(rows) and not no_number,
             "add the people in the household" if not rows else "no number for " + ", ".join(no_number)),
            ("the water, gas and power shutoffs written down",
             all(doc["home"][k] for k in ("water_shutoff", "gas_shutoff", "breaker")),
             "write 'none' for a shutoff the home does not have"),
            ("the grab list has items", bool(doc["grab_list"]), ""),
            ("at least one hazard has a note", any(h["applies"] and h["note"] for h in doc["hazards"].values()), ""),
            ("reviewed in the last 6 months", bool(reviewed and 0 <= (today - reviewed).days <= REVIEW_DAYS),
             "never reviewed" if not reviewed else f"the review date {reviewed.isoformat()} is later than today"
             if reviewed > today else f"last reviewed {reviewed.isoformat()}"),
            ("a drill in the last year", bool(drills), ""),
        ]
        return {"score": sum(1 for _, ok, _ in checks if ok), "out_of": len(checks),
                "checks": [{"check": c, "ok": ok, "why": "" if ok else why} for c, ok, why in checks]}

    def review_note(self, doc: dict) -> str:
        reviewed = _as_date(doc["last_reviewed"])
        if reviewed is None:
            return "This plan has not been reviewed yet. When the household has gone through it together, log a review."
        if reviewed > self.today():
            return (f"This plan's review date, {reviewed.isoformat()}, is later than today, so it does not count. "
                    f"When the household has gone through it together, log a review.")
        if (self.today() - reviewed).days > REVIEW_DAYS:
            return (f"This plan was last reviewed on {reviewed.isoformat()}, more than 6 months ago. "
                    f"A short look together, then log a review, keeps it current.")
        return ""

    def view(self) -> dict:
        doc = self.store.load()
        rows, from_core = self.household(doc)
        return {**doc, "household": rows, "people_from_core": from_core, "readiness": self.readiness(doc),
                "review_note": self.review_note(doc), "today": self.today().isoformat(),
                "labels": {"contact_kinds": CONTACT_KINDS, "meeting": MEETING, "hazards": HAZARDS}}

    # tools
    def show_plan(self, section=None) -> str:
        doc = self.store.load()
        if section not in (None, "", "all", "whole", "everything"):
            return self._section_text(doc, _section(section))
        r = self.readiness(doc)
        parts = [self._section_text(doc, s) for s in SECTIONS]
        parts.append(f"Readiness: {r['score']} of {r['out_of']}.")
        note = self.review_note(doc)
        return "\n\n".join(parts + ([note] if note else []))

    def find_contact(self, need=None) -> str:
        doc = self.store.load()
        need = _text(need, "need", 120).lower()
        contacts = doc["contacts"]
        if not need:
            hits = contacts
        else:
            kind = _infer_kind(need)
            for k, words in () if kind else (("out_of_area", ("out of area", "out-of-area", "check in", "check-in", "out of state")),
                             ("vet", ("vet", "animal")), ("doctor", ("doctor", "clinic", "pediatric", "dentist")),
                             ("insurance", ("insurance", "claim", "policy")), ("school", ("school",)),
                             ("work", ("work", "office", "job")), ("helper", ("helper", "neighbo", "nearby")),
                             ("utility", ("utilit", "power", "electric", "gas company", "water company"))):
                if any(w in need for w in words):
                    kind = k
                    break
            hits = [c for c in contacts if c["kind"] == kind] if kind else []
            words = [w for w in _name_words(need.replace("'s", " ")) if len(w) > 2]
            hits += [c for c in contacts if c not in hits and any(_words_fit([w], c["name"] + " " + c["notes"])
                                                                    for w in words)]
        lines = [f"- {c['name']} ({CONTACT_KINDS[c['kind']]}): {_lines(CONTACT, c) or 'no number yet'}" for c in hits]
        if need:
            rows, _ = self.household(doc)
            lines += [f"- {p['name']} (household): mobile {p['mobile'] or 'not written down'}"
                      + (f"; {p['work_school']} {p['work_phone']}".rstrip() if p.get("work_school") else "")
                      for p in rows if _name_words(p["name"])
                      and set(_name_words(p["name"])) <= set(_name_words(need) + _name_words(need.replace("'s", " ")))]
        if not lines:
            known = ", ".join(f"{c['name']} ({CONTACT_KINDS[c['kind']]})" for c in contacts) or "none yet"
            return (f"No contact in the plan matches {need!r}. The contacts are: {known}. "
                    f"In danger right now, call your local emergency number.")
        return "From the emergency plan:\n" + "\n".join(lines)

    def where_is(self, thing=None) -> str:
        doc = self.store.load()
        thing = _text(thing, "thing", 120).lower()
        spots = [(label, doc["home"][k], k.split("_")[0].replace("breaker", "breaker power electric")
                  + " " + label + {"extinguishers": " fire", "first_aid": " kit medical", "go_bags": " bag bags kit",
                                   "papers": " documents passport insurance", "safe_storm": " tornado wind shelter",
                                   "safe_quake": " quake", "ways_out": " exit exits escape window"}.get(k, ""))
                 for k, (label, _, _) in HOME.items()]
        spots += [(f"meeting place {MEETING[k]}", _lines(PLACE, p), "meet meeting place " + MEETING[k])
                  for k, p in doc["meeting_places"].items()]
        spots += [(f"{p['name']}'s carrier", p["carrier"], "carrier pet " + p["name"].lower()) for p in doc["pets"]]
        spots += [(label, doc["leaving"][k], "route routes leave stay transport " + label)
                  for k, (label, _, _) in LEAVING.items()]
        if thing:
            words = [w for w in thing.replace("-", " ").split() if len(w) > 2 and w not in ("the", "where", "our")]
            scored = [(sum(1 for w in words if w.rstrip("s") in s[2].lower()), s) for s in spots]
            best = max((n for n, _ in scored), default=0)
            spots = [s for n, s in scored if best and n == best]
            if not spots:
                return (f"The plan says nothing about {thing!r}. It knows the shutoffs, fire extinguishers, first aid "
                        f"kit, go-bags, spare keys, important papers, safe spots, ways out and meeting places.")
        else:
            spots = [s for s in spots if s[1]]
            if not spots:
                return "Nothing is written down yet about where things are."
        return "From the emergency plan:\n" + "\n".join(f"- {l}: {v or 'not written down yet'}" for l, v, _ in spots)

    def update_plan(self, section, item=None, fields=None) -> str:
        sec = _section(section)
        if sec == "practice":
            raise ValueError("use log_practice to add a drill or a review")
        if fields is None:
            fields = {}
        if not isinstance(fields, dict):
            raise ValueError("fields is an object of field names and values")
        today = self.today()
        core = self.core.list() if sec == "people" else []       # asked before the lock, never while holding it
        with self.store.lock:
            doc = self.store.load()
            if sec in ("home", "leaving") and item not in (None, "") and len(fields) == 1:
                k = _field(sec, item)
                fields = {k: next(iter(fields.values()))}
            got = {_field(sec, k): v for k, v in fields.items() if _key(k) != "name"}
            new_name = fields.get("name") if "name" in fields else None
            if not got and new_name is None and sec not in LIST_SECTIONS:
                raise ValueError("say what to set in fields, like {\"note\": \"...\"}")
            kind = got.pop("kind") if sec == "contacts" and "kind" in got else None
            clean = {}
            for k, v in got.items():
                label, most, lines = FIELDS[sec][k]
                if k in ("done", "applies"):
                    clean[k] = _flag(v, label)
                else:
                    clean[k] = _text(v, label, most, lines)
            if sec == "contacts":
                said = self._update_contact(doc, item, clean, new_name, kind)
                self.store.save(doc)
                return f"Saved in Contacts: {said}"
            if sec in ("home", "leaving"):
                doc[sec].update(clean)
                said = ", ".join(FIELDS[sec][k][0] for k in clean)
            elif sec in ("meeting_places", "hazards"):
                table = MEETING if sec == "meeting_places" else HAZARDS
                slot = _slot(table, item, "meeting place" if sec == "meeting_places" else "hazard")
                if sec == "hazards" and clean.get("note") and "applies" not in clean:
                    clean["applies"] = True
                doc[sec][slot].update(clean)
                said = f"{table[slot]} ({', '.join(FIELDS[sec][k][0] for k in clean)})"
            else:
                said = self._update_list(doc, sec, item, clean, new_name, today, core)
            self.store.save(doc)
        return f"Saved in {SECTION_TITLES[sec]}: {said}."

    def _update_contact(self, doc: dict, item, clean: dict, new_name, kind_raw) -> str:
        """One contact. Its kind comes from fields kind; when that is missing, other or not a kind, from the item
        and the name and notes. Words that name no kind are never refused: the answer says which kind was used."""
        contacts = doc["contacts"]
        name = _text(item, "item", 120)
        named = _text(new_name, "name", 120) if new_name is not None else ""
        if not name and not named:
            raise ValueError("say which contact, by its name")
        target = _find(contacts, name, "contact", partial=False) if name else None
        phrase = _kind_phrase(name) if name and target is None else None   # item like 'the out-of-area contact'
        said_item = name
        filler = not [w for w in _words(name).split() if w not in KIND_FILLER]    # like 'contacts' or 'our contact'
        if target is None and named and (phrase or filler):
            name, new_name = named, None
            target = _find(contacts, name, "contact", partial=False)
        elif phrase and not named:
            same = [c for c in contacts if c["kind"] == phrase]
            target = same[0] if len(same) == 1 else None
        added = target is None
        if added:
            if len(contacts) >= LIMITS["contacts"]:
                raise ValueError(f"contacts holds at most {LIMITS['contacts']} items")
            target = {"id": secrets.token_hex(4), "name": name, **{k: "" for k in CONTACT}, "kind": "other"}
            contacts.append(target)
        if new_name is not None:
            if not named:
                raise ValueError("a name can not be empty")
            target["name"] = named
        target.update(clean)
        given = kind_raw not in (None, "")
        kind, outright = _contact_kind(kind_raw) if given else ("other", False)
        guess = _infer_kind(said_item, target["name"], target["notes"])
        if outright and kind != "other":
            how = ""
        elif given and kind != "other":
            how = f"from the kind words {_text(kind_raw, 'kind', 60)!r}"
        elif phrase:
            kind, how = phrase, f"from {said_item!r}"
        elif (given or target["kind"] == "other") and guess:
            kind, how = guess, "from the name or notes"
        elif given or added:
            kind = "other"
            how = "" if outright else (f"{_text(kind_raw, 'kind', 60)!r} is not one of the kinds, so it is other"
                                       if given else "nothing in the name or notes says which kind")
        else:
            kind, how = target["kind"], None
        changed = kind != target["kind"]
        target["kind"] = kind
        what = ", ".join([CONTACT[k][0] for k in clean] + (["kind"] if changed else [])) or \
            ("added" if added else "name")
        said = f"{'added ' if added else ''}{target['name']} ({what}). Kind: {CONTACT_KINDS[kind]} ({kind})"
        if how:
            said += (f", {how}. To change it, set kind to one of "
                     + ", ".join(CONTACT_KINDS) + "; for the out-of-area contact, set kind to out_of_area")
        return said + "."

    def _update_list(self, doc: dict, sec: str, item, clean: dict, new_name, today: date, core=None) -> str:
        most = 60 if sec in ("people", "pets") else 120
        name = _text(item, "item", most)
        if not name:
            raise ValueError(f"say which item in {sec}, by its name")
        if sec == "people":
            rows, _ = self.household(doc, core)
            row = _find(rows, name, "person", partial=False)
            own = next((p for p in doc["people"] if row and p["id"] == row["id"]), None)
            if row and not own:                       # a Core person with no details here yet
                own = {"id": secrets.token_hex(4), "name": row["name"], "core_id": row.get("core_id", ""),
                       **{k: "" for k in PERSON}}
                doc["people"].append(own)
            target = own
        else:
            target = _find(doc[sec], name, sec.rstrip("s").replace("_", " "), partial=False)
        added = target is None
        if added:
            if len(doc[sec]) >= LIMITS[sec]:
                raise ValueError(f"{sec} holds at most {LIMITS[sec]} items")
            target = {"id": secrets.token_hex(4), "name": name,
                      **{k: ("other" if k == "kind" and sec == "contacts" else False if k == "done" else "")
                         for k in FIELDS[sec]}}
            if sec == "people":
                target["core_id"] = ""
            doc[sec].append(target)
        if new_name is not None:
            renamed = _text(new_name, "name", most)
            if not renamed:
                raise ValueError("a name can not be empty")
            if sec == "people" and target.get("core_id"):
                raise ValueError("this person's name comes from the Core's people list; change it there")
            target["name"] = renamed
        target.update(clean)
        what = ", ".join(FIELDS[sec][k][0] for k in clean) or ("added" if added else "name")
        return f"{'added ' if added else ''}{target['name']} ({what})"

    def readiness_check(self) -> str:
        doc = self.store.load()
        r = self.readiness(doc)
        missing = [f"- {c['check']}{' (' + c['why'] + ')' if c['why'] else ''}" for c in r["checks"] if not c["ok"]]
        text = f"Readiness: {r['score']} of {r['out_of']}."
        text += "\nMissing:\n" + "\n".join(missing) if missing else " The plan has everything on the checklist."
        note = self.review_note(doc)
        return text + ("\n" + note if note else "")

    def grab_list(self, item=None, done=None) -> str:
        if item in (None, ""):
            doc = self.store.load()
            g = doc["grab_list"]
            if not g:
                return "The grab list is empty. Add items with update_plan."
            return (f"Grab list, {sum(1 for x in g if x['done'])} of {len(g)} packed:\n"
                    + "\n".join(f"- [{'x' if x['done'] else ' '}] {x['name']}" for x in g))
        flag = True if done is None else _flag(done, "done")
        with self.store.lock:
            doc = self.store.load()
            if _text(item, "item", 120).lower() in ("all", "everything", "the whole list"):
                for x in doc["grab_list"]:
                    x["done"] = flag
                self.store.save(doc)
                return f"{'Ticked' if flag else 'Unticked'} every item on the grab list."
            x = _find(doc["grab_list"], item, "grab list item")
            if x is None:
                raise ValueError(f"the grab list has no {item!r}; add it with update_plan first")
            x["done"] = flag
            self.store.save(doc)
        return f"{_using(x, item)}{'Ticked' if flag else 'Unticked'} {x['name']} on the grab list."

    def log_practice(self, what, date=None, fix=None, kind=None) -> str:
        today = self.today()
        what = _text(what, "what", 300)
        if not what:
            raise ValueError("say what was practiced or reviewed")
        day = _day(date, "date", today) or today
        if day > today:
            raise ValueError("date can not be a day still to come")
        if (today - day).days > 3650:
            raise ValueError("date is at most 10 years back")
        k = _key(_text(kind, "kind", 10)) or ("review" if "review" in what.lower() else "drill")
        if k not in ("drill", "review"):
            raise ValueError("kind is drill or review")
        with self.store.lock:
            doc = self.store.load()
            if len(doc["practice"]) >= LIMITS["practice"]:
                raise ValueError(f"the practice log holds at most {LIMITS['practice']} entries")
            doc["practice"].append({"id": secrets.token_hex(4), "date": day.isoformat(), "kind": k, "what": what,
                                    "fix": _text(fix, "what to fix", 1000, True)})
            # a review date later than today (from an import) does not count, so a real review replaces it
            if k == "review" and (day.isoformat() > doc["last_reviewed"] or doc["last_reviewed"] > today.isoformat()):
                doc["last_reviewed"] = day.isoformat()
            self.store.save(doc)
        return (f"Logged a {k} on {day.isoformat()}: {what}."
                + (f" Last reviewed is now {doc['last_reviewed']}." if k == "review" else ""))

    def remove_item(self, section, item) -> str:
        sec = _section(section)
        core = self.core.list() if sec == "people" else []       # asked before the lock, never while holding it
        with self.store.lock:
            doc = self.store.load()
            if sec in ("home", "leaving"):
                k = _field(sec, item)
                doc[sec][k] = ""
                said = f"cleared {FIELDS[sec][k][0]}"
            elif sec in ("meeting_places", "hazards"):
                table = MEETING if sec == "meeting_places" else HAZARDS
                slot = _slot(table, item, "meeting place" if sec == "meeting_places" else "hazard")
                doc[sec][slot] = {f: (False if f == "applies" else "") for f in FIELDS[sec]}
                said = f"cleared {table[slot]}"
            elif sec == "people":
                rows, _ = self.household(doc, core)
                row = _find(rows, item, "person")
                own = next((p for p in doc["people"] if row and p["id"] == row["id"]), None)
                if own is None:
                    raise ValueError(f"no person called {item!r} has details in the plan")
                doc["people"] = [p for p in doc["people"] if p is not own]
                using = _using(row, item)
                said = f"removed {own['name']}" + (" (their details only; they stay in the Core's people list)"
                                                   if row.get("from_core") else "")
            else:
                key = "what" if sec == "practice" else "name"
                x = _find(doc[sec], item, "entry" if sec == "practice" else sec.rstrip("s").replace("_", " "), key)
                if x is None:
                    raise ValueError(f"{SECTION_TITLES[sec]} has nothing called {item!r}")
                doc[sec] = [y for y in doc[sec] if y is not x]
                using = _using(x, item, key)
                said = f"removed {x.get(key)}"
            self.store.save(doc)
        return f"{using if sec in LIST_SECTIONS else ''}{SECTION_TITLES[sec]}: {said}."

    def import_plan(self, plan, confirm) -> dict:
        if confirm is not True:
            raise ValueError("importing replaces the whole plan; send confirm: true to go ahead")
        doc = normalize(plan, self.today())
        with self.store.lock:
            self.store.load()                         # a plan file that will not read is never replaced
            backup = self.store.backup()
            self.store.save(doc)
        return {"backup": backup, "text": "Imported the plan." + (f" The old plan is kept as {backup}." if backup
                                                                  else "")}


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_SECTION = _p("string", "One of people, contacts, meeting_places, home, leaving, grab_list, pets, hazards, practice.")
TOOLS = [
    ("show_plan", "Read out the household emergency plan: the whole plan, or one section (people, contacts, "
     "meeting places, home, leaving, grab list, pets, hazards, practice).",
     {"section": _p("string", "Optional: one section. Default the whole plan.")}, []),
    ("find_contact", "Find who to call from the emergency plan, by need: the out-of-area contact everyone checks in "
     "with, a nearby helper, the doctor, the vet, school, work, insurance, a utility, or a person in the household.",
     {"need": _p("string", "Who or what for, like out-of-area contact, vet, insurance claims, or Sam.")}, []),
    ("where_is", "Say where something is, from the emergency plan: the water, gas and power shutoffs and how to "
     "turn them off, fire extinguishers, first aid kit, go-bags, spare keys, important papers, safe spots, ways out, "
     "meeting places, pet carriers and routes out of the area.",
     {"thing": _p("string", "What to find, like gas shutoff or meeting place. Empty: everything written down.")}, []),
    ("update_plan", "Set or change one item in a section of the emergency plan: a person's details, a contact, a "
     "meeting place, a home detail, a leaving detail, a grab list item, a pet, or a hazard and what we do. An item "
     "that is not there yet is added. For the out-of-area contact, set kind to out_of_area.",
     {"section": _SECTION,
      "item": _p("string", "Which item: a person's, contact's or pet's name, a grab list item, a meeting place "
                           "(outside home, neighborhood, out of neighborhood, out of town), a hazard (like flood), "
                           "or for home and leaving the field to set."),
      "fields": {"type": "object", "description": "Field names and values to set, like {\"mobile\": \"555-0100\"} "
                 "for a person, {\"kind\": \"vet\", \"phone\": \"555-0123\"} for a contact, {\"address\": \"...\"} for "
                 "a meeting place, {\"water_shutoff\": \"...\", \"water_how\": \"...\"} for home, {\"note\": \"...\"} "
                 "for a hazard, {\"name\": \"...\"} to rename.",
                 "properties": {"kind": {
                     "type": "string", "enum": list(CONTACT_KINDS),
                     "description": "For a contact only, its kind, one of:\n"
                                    + "\n".join(f"{k}: {v}" for k, v in KIND_HELP.items())}}}},
     ["section"]),
    ("readiness_check", "Score how complete the emergency plan is, out of 8 plain checks, and list what is missing.",
     {}, []),
    ("grab_list", "Show the grab list of things to take when leaving home in a hurry, or tick or untick one item on "
     "it as packed.",
     {"item": _p("string", "Optional: the item to tick or untick, or all."),
      "done": _p("boolean", "Optional: true to tick (packed), false to untick. Default true.")}, []),
    ("log_practice", "Log a drill or a review of the emergency plan: the date, what was practiced and what to fix. "
     "A review also sets the plan's last reviewed date.",
     {"what": _p("string", "What was practiced or reviewed, like fire drill, out the back door."),
      "date": _p("string", "Optional: the day, like 2027-03-31 or yesterday. Default today."),
      "fix": _p("string", "Optional: what to fix next time."),
      "kind": _p("string", "Optional: drill or review. Default drill, or review when what says review.")},
     ["what"]),
    ("remove_item", "When the owner wants something taken out of the emergency plan, remove it by its name, no need "
     "to look it up first: a person's details, a contact, a meeting place, a home or leaving detail, a grab list "
     "item, a pet, a hazard note, or a practice log entry.",
     {"section": _SECTION, "item": _p("string", "Which item, by its name (for home and leaving, the field).")},
     ["section", "item"]),
]
CHANGES = ["update_plan", "grab_list", "log_practice", "remove_item"]
DELETES = ["remove_item"]


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
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Emergency Plan does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/update", "/api/remove", "/api/grab", "/api/practice", "/api/import")


class Handler(BaseHTTPRequestHandler):
    server_version = "EmergencyPlan/1"
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
        """Content-Length, strictly: missing is 0; digits only; None when it is not a number."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return None
        return int(raw)

    def _drop(self, n: int, most: int, seconds: float) -> None:
        """Read and throw away up to *n* bytes of body (never more than *most*) within *seconds*, stopping early
        when nothing more arrives for IDLE_S. Nothing is left unread that the sender is still waiting to send,
        so the answer that follows is never lost to a reset."""
        left_bytes, end = min(n, most), time.monotonic() + seconds
        try:
            while left_bytes > 0:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(min(left, IDLE_S))
                chunk = self.rfile.read1(min(left_bytes, 65536))
                if not chunk:
                    break
                left_bytes -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _length(self) -> int:
        """Content-Length, at most MAX_BODY bytes. A bigger body is read and dropped first (up to DROP_MOST
        within DROP_S seconds), so the plain 400 that follows reaches the sender."""
        n = self._declared()
        if n is None or n > MAX_BODY:
            self.close_connection = True
            self._drop(DROP_MOST if n is None else n, DROP_MOST, DROP_S)
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return n

    def _body(self):
        n = self._length()
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
        return _loads(bytes(raw).decode("utf-8")) if raw else {}

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
        n = self._declared()
        if n is None or n:                     # at most 64 KB, for at most 2 seconds, then the answer
            self._drop(REFUSED_MOST if n is None else n, REFUSED_MOST, REFUSED_S)
        self.close_connection = True
        self._json(401, {"error": "Emergency Plan needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Emergency Plan stopped on this request with an error "
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
            if path == "/export":
                name = f"emergency-plan-{self.app.today().isoformat()}.json"
                return self._json(200, {**self.app.store.load(), "exported": _now()},
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Emergency Plan has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            return self._json(404, {"error": "Emergency Plan has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path == "/api/update":
                out = {"text": app.update_plan(body.get("section"), body.get("item"), body.get("fields"))}
            elif path == "/api/remove":
                out = {"text": app.remove_item(body.get("section"), body.get("item"))}
            elif path == "/api/grab":
                if body.get("item") in (None, ""):
                    raise ValueError("say which grab list item")
                out = {"text": app.grab_list(body.get("item"), body.get("done"))}
            elif path == "/api/practice":
                out = {"text": app.log_practice(body.get("what"), body.get("date"), body.get("fix"), body.get("kind"))}
            else:
                out = app.import_plan(body.get("plan"), body.get("confirm"))
            out["last_updated"] = app.store.load()["last_updated"]
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
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


def make_server(port: int, data: Path | str, token: str | None, today=date.today, api: str | None = None,
                people_ttl_s: float = 60) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Plan(Store(data, today), People(api, token, people_ttl_s, min(30, people_ttl_s)), today)
    handler = type("PlanHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Emergency Plan for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Emergency Plan with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
