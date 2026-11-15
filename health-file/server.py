"""Health File: a household health record that stays on this computer, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/health-file/
- ``/health``       "ok"
- ``/updated``      the saved file's stamp; ``/meta`` the record types and their fields
- ``/people``       the household (the Core's shared people list when it answers, else this app's own)
- ``/view``         one person's summary and records; ``/history`` one test's values over time (the page's chart)
- ``/export``       one person or everyone, as this app's JSON or as a FHIR R4 Bundle (GET, changes nothing)
- ``/api/record/add``, ``/api/record/change``, ``/api/record/remove``,
  ``/api/person/add``, ``/api/person/remove``, ``/api/import``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/health-file/, which adds it.

The household's people come from the Core's shared people list (GET {PUFFPAI_API}/v1/people
with PUFFPAI_TOKEN, D12) when it answers with names; otherwise from this app's own list.
Nothing else is ever dialed, and PUFFPAI_API is dialed only when it is on this computer: no proxy, no
redirect followed, 2 seconds in all, at most 256 KB, and the answer kept for a minute.

The records are one JSON document in PUFFPAI_DATA (``health.json``). Writes replace the
whole file at once (temp file, then rename). A file that exists but will not read, or is
empty, is never written over. How a result's flag is worked out, what the summary shows
and how import decides to skip: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import io
import json
import math
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "health-file"
VERSION = "1.0.0"
DOC = "health.json"
FORMAT = "health-file/1"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_IMPORT = 4 * 1024 * 1024
MAX_DEPTH = 40
MAX_PEOPLE = 50
MAX_RECORDS = 20000
NAME_MOST = 80                           # a person's name is at most this many characters
BIGGEST = 1e12                           # no health number is bigger; larger is refused
SHARED_FOR_S = 60                        # how long one answer from the Core's people list is used
SHARED_FAILED_FOR_S = 30                 # how long "the Core's people list did not answer" is used
PEOPLE_DEADLINE_S = 2.0                  # the whole ask (connect and every read) ends by then
PEOPLE_MOST = 256 * 1024                 # the people list's answer is at most this many bytes
BODY_DEADLINE_S = 5.0                    # a request body must arrive whole by then
MAX_DRAIN = 64 * 1024 * 1024             # a body over its cap is read and dropped up to this much...
DRAIN_WAIT_S = 20                        # ...within this long, then answered 400
REFUSED_DRAIN = 64 * 1024                # a request without the token: at most this much is read...
REFUSED_WAIT_S = 2                       # ...within this long, then 401
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
UNCLEAR = "Range not clear, no flag given. Read the printed range."

DISCLAIMER = ("Health File keeps your records. It is not a doctor and does not diagnose. "
              "For anything urgent, call your local emergency number.")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _json_float(text: str) -> float:
    x = float(text)
    if not math.isfinite(x) or abs(x) > BIGGEST:
        raise ValueError("a number in that request is too large")
    return x


def _json_int(text: str) -> int:
    if len(text.lstrip("-")) > 13:
        raise ValueError("a number in that request is too large")
    return int(text)


def _depth_ok(obj) -> bool:
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if isinstance(o, (dict, list)):
            if d > MAX_DEPTH:
                return False
            stack.extend((v, d + 1) for v in (o.values() if isinstance(o, dict) else o))
    return True


def _loads(text: str):
    """JSON in, refusing NaN, Infinity, huge numbers and deep nesting with a plain ValueError."""
    try:
        out = json.loads(text, parse_constant=_no_constant, parse_float=_json_float, parse_int=_json_int)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None
    if not _depth_ok(out):
        raise ValueError("that JSON is nested too deeply")
    return out


def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters on one line, trimmed; *default* when not given."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be words")
    value = " ".join(str(value).split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _long(value, name: str, most: int) -> str:
    """Text that may have several lines."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    lines = [" ".join(line.split()) for line in value.replace("\r", "").split("\n")]
    value = "\n".join(lines).strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value


_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?")


def number(value) -> float | None:
    """A number from a number or from words like 5.4; None when it is not one. Never raises."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            x = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str) and len(value.strip()) <= 40 and _NUM.fullmatch(value.strip()):
        x = float(value.strip())
    else:
        return None
    return x if math.isfinite(x) and abs(x) <= BIGGEST else None


def _numtext(x: float) -> str:
    return str(int(x)) if float(x).is_integer() and abs(x) < 1e15 else repr(float(x))


def _number_field(value, name: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    x = number(value)
    if x is None:
        raise ValueError(f"{name} is a number, like 4.5")
    return value.strip() if isinstance(value, str) else _numtext(x)


def _day(value, name: str, today: date, partial: bool = False) -> str:
    """A date as YYYY-MM-DD (or YYYY or YYYY-MM when *partial*), or today, tomorrow, yesterday; "" when not given."""
    text = _text(value, name, 40).lower()
    if not text:
        return ""
    words = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if text in words:
        return (today + timedelta(days=words[text])).isoformat()
    if partial and re.fullmatch(r"\d{4}", text) and 1900 <= int(text) <= 2200:
        return text
    if partial and re.fullmatch(r"\d{4}-\d{2}", text) and 1 <= int(text[5:]) <= 12:
        return text
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    raise ValueError(f"{name} is a date like 2027-03-31" + (", or a year or a month like 2019 or 2019-05" if partial
                                                             else "") + ", or today, tomorrow or yesterday")


def _yes(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "y", "true", "asked", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "n", "false", "not yet", "0", ""):
        return False
    raise ValueError(f"{name} is yes or no")


def _after(d: str, ref: str) -> bool:
    """*d* (maybe only a year or month) is after the day *ref*."""
    return bool(d) and d > ref[:len(d)]


def _not_before(d: str, ref: str) -> bool:
    """*d* (maybe only a year or month) is on or after the day *ref*."""
    return bool(d) and d >= ref[:len(d)]


# ---------------------------------------------------------------- record types

def F(key, label, kind, most=0, choices=(), required=False, default=None):
    return {"key": key, "label": label, "kind": kind, "most": most, "choices": list(choices),
            "required": required, "default": default}


NOTE = F("note", "Note", "long", 1000)
TYPES = {
    "condition": {"label": "Condition", "plural": "Conditions", "fhir": "Condition", "title": "name", "fields": [
        F("name", "Condition", "text", 120, required=True),
        F("status", "Status", "choice", choices=("active", "resolved"), default="active"),
        F("since", "Since", "partial"), F("until", "Until", "partial"), NOTE]},
    "medicine": {"label": "Medicine", "plural": "Medicines", "fhir": "MedicationStatement", "title": "name", "fields": [
        F("name", "Medicine", "text", 120, required=True), F("dose", "Dose", "text", 80),
        F("how_often", "How often", "text", 80), F("since", "Since", "partial"), F("until", "Until", "partial"),
        F("status", "Status", "choice", choices=("taking", "stopped"), default="taking"),
        F("reason", "What it is for", "text", 120), F("prescriber", "Prescriber", "text", 80), NOTE]},
    "allergy": {"label": "Allergy", "plural": "Allergies", "fhir": "AllergyIntolerance", "title": "what", "fields": [
        F("what", "Allergic to", "text", 120, required=True), F("reaction", "Reaction", "text", 120),
        F("severity", "How serious", "choice", choices=("mild", "moderate", "severe")), NOTE]},
    "result": {"label": "Result", "plural": "Results", "fhir": "Observation", "title": "test", "fields": [
        F("test", "Test", "text", 120, required=True), F("date", "Date", "date", required=True, default="today"),
        F("value", "Value", "text", 40, required=True), F("unit", "Unit", "text", 30),
        F("range_low", "Range low (as printed)", "number"), F("range_high", "Range high (as printed)", "number"),
        F("range_text", "Range as printed (text)", "text", 60), F("lab", "Lab or source", "text", 80), NOTE]},
    "visit": {"label": "Visit", "plural": "Visits", "fhir": "Encounter", "title": "who", "fields": [
        F("date", "Date", "date", required=True, default="today"), F("who", "Who or where", "text", 120),
        F("why", "Why", "text", 200), F("said", "What was said", "long", 2000),
        F("follow_up", "Follow-up date", "date")]},
    "vaccination": {"label": "Vaccination", "plural": "Vaccinations", "fhir": "Immunization", "title": "vaccine",
                    "fields": [
        F("vaccine", "Vaccine", "text", 120, required=True), F("date", "Date", "date", default="today"),
        F("lot", "Lot (optional)", "text", 40), F("due_again", "Due again (optional)", "date")]},
    "question": {"label": "Question", "plural": "Questions", "fhir": "", "title": "text", "fields": [
        F("text", "Question", "text", 300, required=True), F("added", "Added", "date", default="today"),
        F("asked", "Asked", "bool", default=False), F("answer", "Answer", "long", 1000)]},
}
TYPE_WORDS = {
    "conditions": "condition", "problem": "condition", "problems": "condition", "diagnosis": "condition",
    "medicines": "medicine", "medication": "medicine", "medications": "medicine", "meds": "medicine",
    "med": "medicine", "drug": "medicine", "drugs": "medicine", "prescription": "medicine", "prescriptions": "medicine",
    "allergies": "allergy", "results": "result", "test": "result", "tests": "result", "lab": "result",
    "labs": "result", "observation": "result", "bloods": "result", "blood": "result", "bloodwork": "result",
    "bloodtest": "result", "bloodtests": "result", "testresult": "result", "testresults": "result", "visits": "visit", "appointment": "visit", "appointments": "visit",
    "encounter": "visit", "vaccinations": "vaccination", "vaccine": "vaccination", "vaccines": "vaccination",
    "shot": "vaccination", "shots": "vaccination", "immunization": "vaccination", "immunizations": "vaccination",
    "questions": "question",
}
FIELD_WORDS = {
    "allergy": {"name": "what", "allergen": "what", "how_serious": "severity"},
    "result": {"name": "test", "test_name": "test", "low": "range_low", "high": "range_high", "range": "range_text",
               "source": "lab", "reference_range": "range_text"},
    "vaccination": {"name": "vaccine", "due": "due_again"},
    "medicine": {"for": "reason", "what_it_is_for": "reason", "frequency": "how_often", "dose_text": "dose"},
    "visit": {"where": "who", "follow_up_date": "follow_up", "what_was_said": "said", "reason": "why"},
    "question": {"question": "text"},
    "condition": {},
}
CHOICE_WORDS = {"current": "taking", "on it": "taking", "active": "taking", "yes": "taking",
                "no longer": "stopped", "stopped taking": "stopped", "ended": "stopped", "off": "stopped",
                "inactive": "resolved", "gone": "resolved", "over": "resolved"}


PERSON_WORDS = ("person", "people", "household member", "family member", "member")


def _is_record_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"r[0-9a-f]{8}", value.strip()) is not None


def name_key(name: str) -> str:
    """A name with capital letters and spaces ignored: "Dee Dee" and "deedee" are the same person."""
    return "".join(str(name).split()).lower()


def kind_of(value) -> str:
    word = _text(value, "type", 30).lower().replace(" ", "")
    word = TYPE_WORDS.get(word, word)
    if word not in TYPES:
        raise ValueError("type is one of: " + ", ".join(TYPES))
    return word


def clean_fields(kind: str, fields, today: date, old: dict | None = None) -> dict:
    """The record's fields, checked. For a change, *old* is the record and only the given fields change."""
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise ValueError("fields is an object, like {\"name\": \"Asthma\"}")
    spec = {f["key"]: f for f in TYPES[kind]["fields"]}
    words = FIELD_WORDS.get(kind, {})
    given = {}
    for k, v in fields.items():
        key = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        key = words.get(key, key)
        if key not in spec:
            raise ValueError(f"a {TYPES[kind]['label'].lower()} has no field {str(k)[:40]!r}; "
                             f"its fields are {', '.join(spec)}")
        given[key] = v
    out = {}
    for key, f in spec.items():
        if old is not None and key not in given:
            out[key] = old.get(key, "" if f["kind"] != "bool" else False)
            continue
        v = given.get(key, f["default"])
        name, kd = f["label"].lower(), f["kind"]
        if kd == "text":
            v = _text(v, key, f["most"])
        elif kd == "long":
            v = _long(v, key, f["most"])
        elif kd in ("date", "partial"):
            v = _day(v, key, today, kd == "partial")
        elif kd == "number":
            v = _number_field(v, key)
        elif kd == "bool":
            v = _yes(v if v is not None else False, key)
        elif kd == "choice":
            v = _text(v, key, 30).lower()
            v = CHOICE_WORDS.get(v, v) if v not in f["choices"] else v
            if v and v not in f["choices"]:
                raise ValueError(f"{key} is " + " or ".join(f["choices"]))
            if not v and f["default"]:
                v = f["default"]
        if f["required"] and (v == "" or v is None):
            raise ValueError(f"a {TYPES[kind]['label'].lower()} needs its {name} ({key})")
        out[key] = v
    return out


# ---------------------------------------------------------------- the flag

# A flag is given ONLY when the printed range is clear (PROCESS.md, "A result's flag"). Anything else gives no flag
# and the UNCLEAR note: a wrong flag on a health result is worse than none.
_RNUM = r"(\d+(?:\.\d+)?|\.\d+)"
# A unit: one word with no spaces, not starting with a digit, with no brackets, commas or comparison signs.
_UNIT = r"(?:\s*([^\d\s.,;:()\[\]<>=+\-][^\s,;:()\[\]<>=]*))?"
_BETWEEN = re.compile(_RNUM + r"\s*(?:-|to)\s*" + _RNUM + _UNIT)
_SIGN = re.compile(r"(<=|<|>=|>)\s*" + _RNUM + _UNIT)
_WORDS = re.compile(r"(up to|at most|under|below|less than|at least|over|above|more than|greater than)\s+"
                    + _RNUM + _UNIT)
_NUM_UNIT = re.compile(_RNUM + _UNIT)                    # "x unit", used before " or less" (cut off by hand)
RANGE_MOST = 200                                          # a printed range longer than this is not read
# A word after a range is taken as a unit, when the result has none, only when it is one: a plain unit word below,
# "%", or such words joined by "/" (mmol/L, mg/dL, x10^9/L, mL/min/1.73m2). Anything else, like "fasting",
# "approx", "T2" or "x2", makes the range not clear.
UNIT_WORDS = frozenset((
    "g", "mg", "ug", "mcg", "µg", "ng", "pg", "kg", "mol", "mmol", "umol", "µmol", "nmol", "pmol", "l", "dl", "ml",
    "ul", "µl", "fl", "iu", "miu", "uiu", "µiu", "u", "mu", "units", "meq", "mosm", "mm", "cm", "mmhg", "kpa",
    "bpm", "sec", "secs", "seconds", "min", "mins", "minutes", "ratio", "percent", "pct", "titer", "titre",
    "cells", "hpf"))
_TEN_POWER = re.compile(r"x?10(?:\^|\*\*?)\d{1,2}")                 # x10^9, 10*9: only before a "/"
_AREA = re.compile(r"(?:\d{1,2}(?:\.\d{1,3})?)?(?:m|cm|mm|um|µm)[23]")  # 1.73m2, mm3: only after a "/"
_PER_TIME = re.compile(r"\d{0,2}(?:h|hr|hrs|hour|d|day|wk|week)")        # 24h, day: only after a "/"


def _looks_like_unit(word: str) -> bool:
    """*word* (lower case) is a unit this app knows the shape of. Strict: when unsure, it is not one."""
    if word in UNIT_WORDS or word == "%":
        return True
    parts = word.split("/")
    if len(parts) < 2 or len(parts) > 3:
        return False
    first, rest = parts[0], parts[1:]
    if not (first in UNIT_WORDS or _TEN_POWER.fullmatch(first)):
        return False
    return all(p in UNIT_WORDS or _AREA.fullmatch(p) or _PER_TIME.fullmatch(p) for p in rest)


# Words that mean the printed range has more than one case, or is not one plain range. The first group is whole
# words; the second matches the start of a word too, so "men50+" and "age50+" are caught.
_UNCLEAR_WORDS = re.compile(r"\b(?:(?:or|and|if|unless|except|between)\b|men|women|man|woman|male|female|adult"
                            r"|child|kid|boy|girl|infant|newborn|neonat|teen|elder|senior|age|year|yr|pregnan"
                            r"|trimester|week|wk|month|menopaus|postmeno|premeno|luteal|follicular|ovulat)")


def parse_range_text(text) -> tuple | None:
    """(low, high, low_strict, high_strict, unit) from ONE clear printed range, else None.

    Clear: exactly one of "a-b" (or "a to b") with a <= b, "<x", "<=x", ">x", ">=x", "up to x", "x or less",
    "at least x" (and the plain words for them: at most, under, below, less than, over, above, more than,
    greater than), with at most one unit word after it. More than one range, a second comparison, or any word
    like "or", "and", "men", "women", "adult", "child" or "age": None."""
    t = str(text or "")
    if len(t) > RANGE_MOST * 4:                          # capped before any pattern touches it
        return None
    t = " ".join(t.split()).replace("–", "-").replace("≤", "<=").replace("≥", ">=")
    low = t.lower()
    if not t or len(t) > RANGE_MOST:
        return None
    or_less = low.endswith(" or less")
    if _UNCLEAR_WORDS.search(low[:-len(" or less")] if or_less else low):
        return None                                      # checked first, so "60 years or less" is not clear
    m = _NUM_UNIT.fullmatch(low[:-len(" or less")]) if or_less else None
    if m:
        return None, float(m.group(1)), False, False, (m.group(2) or "")
    m = _BETWEEN.fullmatch(low)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo, hi, False, False, m.group(3) or "") if lo <= hi else None
    m = _SIGN.fullmatch(low)
    if m:
        op, x, unit = m.group(1), float(m.group(2)), m.group(3) or ""
        return (None, x, False, op == "<", unit) if op.startswith("<") else (x, None, op == ">", False, unit)
    m = _WORDS.fullmatch(low)
    if m:
        w, x, unit = m.group(1), float(m.group(2)), m.group(3) or ""
        if w in ("up to", "at most"):
            return None, x, False, False, unit
        if w in ("under", "below", "less than"):
            return None, x, False, True, unit
        return (x, None, False, False, unit) if w == "at least" else (x, None, True, False, unit)
    return None


def _given(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def range_given(r: dict) -> bool:
    """Some range was typed on this result (a low, a high or the printed text)."""
    return any(_given(r.get(k)) for k in ("range_low", "range_high", "range_text"))


def printed_range(r: dict):
    """The range printed on this result as (low, high, low_strict, high_strict) when it is clear, else None."""
    lo_raw, hi_raw = r.get("range_low"), r.get("range_high")
    lo, hi = number(lo_raw), number(hi_raw)
    if (_given(lo_raw) and lo is None) or (_given(hi_raw) and hi is None):
        return None                                   # a low or high that is not a number
    if lo is not None and hi is not None and lo > hi:
        return None                                   # reversed
    text = r.get("range_text")
    if _given(text):
        parsed = parse_range_text(text)
        if parsed is None:
            return None
        plo, phi, lo_strict, hi_strict, unit = parsed
        mine = " ".join(str(r.get("unit") or "").split()).lower()
        if unit and mine and unit.strip().lower() != mine:
            return None                               # the range is in another unit than the result
        if unit and not mine and not _looks_like_unit(unit.strip().lower()):
            return None                               # a word after the range that is not a unit, like "fasting"
        if (lo is not None or hi is not None) and (lo, hi) != (plo, phi):
            return None                               # the typed numbers and the printed text disagree
        return plo, phi, lo_strict, hi_strict
    if lo is None and hi is None:
        return None
    return lo, hi, False, False


def flag(r: dict) -> str:
    """low, in range, high, or none: only from a clear range printed on this result."""
    v = number(r.get("value"))
    rng = printed_range(r)
    if v is None or rng is None:
        return "none"
    lo, hi, lo_strict, hi_strict = rng
    if lo is not None and (v < lo or (lo_strict and v == lo)):
        return "low"
    if hi is not None and (v > hi or (hi_strict and v == hi)):
        return "high"
    return "in range"


def flag_note(r: dict) -> str:
    """UNCLEAR when a range was typed but is not clear enough to flag against; "" otherwise."""
    return UNCLEAR if range_given(r) and printed_range(r) is None else ""


def range_words(r: dict) -> str:
    lo, hi = str(r.get("range_low") or ""), str(r.get("range_high") or "")
    nums = ""
    if lo or hi:
        nums = f"{lo} to {hi}" if lo and hi else (f"at least {lo}" if lo else f"up to {hi}")
    text = str(r.get("range_text") or "")
    if nums and text:
        return f"{text} (typed as {nums})"
    return nums or text


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


ODD_ENTRIES = ("The Health File data file has entries in its people or records that Health File does not understand "
               "(each should be an object in curly brackets). Health File will not write over it, so nothing was "
               "changed; fix or remove those entries by hand, or move the file away to start a new one.")


def default_doc() -> dict:
    return {"format": FORMAT, "people": [], "records": [], "last_updated": ""}


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        """The document. Entries in people or records that are not objects are left out of what is read, and
        the file is then never written over (save refuses)."""
        return self._read()[0]

    def _read(self) -> tuple[dict, int]:
        """(the document, how many entries in people or records are not objects). Never names the file's folder:
        an error goes back to the page and the bot, and the folder holds the user's own name."""
        with self.lock:
            if not self.path.exists():
                return default_doc(), 0
            try:
                raw = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise Unreadable(f"The Health File data file will not read ({type(exc).__name__}). Health File will "
                                 "not write over it.") from None
            if not raw.strip():
                raise Unreadable("The Health File data file is there but empty. Health File will not write over it; "
                                 "move it aside or put back a saved copy.")
            try:
                data = _loads(raw)
            except ValueError as exc:
                raise Unreadable(f"The Health File data file will not read ({exc}). Health File will not write over "
                                 "it.") from None
            if not isinstance(data, dict) or data.get("format") != FORMAT or not set(data) <= set(default_doc()) \
                    or not all(isinstance(data.get(k, []), list) for k in ("people", "records")):
                raise Unreadable("The Health File data file is not this app's file (it has no Health File marker, or "
                                 "other keys). Health File will not write over it; move it away to start a new file.")
            out = default_doc()
            out.update(data)
            odd = 0
            for k in ("people", "records"):
                keep = [x for x in out[k] if isinstance(x, dict)]
                odd += len(out[k]) - len(keep)
                out[k] = keep
            return out, odd

    def load_for_change(self) -> dict:
        """The document, for a change that will be saved: refused first, before anything else is looked up, when
        the file has entries this app does not understand."""
        doc, odd = self._read()
        if odd:
            raise Unreadable(ODD_ENTRIES)
        return doc

    def save(self, data: dict) -> str:
        with self.lock:
            if self._read()[1]:                            # never write over a file that is not ours, or not all ours
                raise Unreadable(ODD_ENTRIES)
            data["last_updated"] = datetime.now().isoformat()
            data["format"] = FORMAT
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".health-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ---------------------------------------------------------------- the shared people list

def local_target(api: str) -> tuple[str, int, str, str] | None:
    """(host, port, Host header, path) when *api* is a plain http address on this computer, else None.

    Checked on every ask, not only at start. No user name, query or fragment; a path of plain characters only."""
    if not isinstance(api, str) or not api:
        return None
    try:
        u = urllib.parse.urlsplit(api)
        port = u.port or 80
    except ValueError:
        return None
    host = u.hostname or ""
    if u.scheme != "http" or host not in LOCAL_HOSTS or "@" in u.netloc or u.query or u.fragment:
        return None
    path = u.path.rstrip("/") + "/v1/people"
    if not re.fullmatch(r"[A-Za-z0-9/._~-]+", path):
        return None
    return host, port, u.netloc, path


class _Canned:
    """Bytes already read, handed to http.client only to be parsed: nothing more is read from the network."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def makefile(self, *_a, **_k):
        return io.BytesIO(self.raw)


def _whole_reply(buf: bytearray) -> bool:
    """The reply in *buf* is whole (or is not a 200 and needs no more reading)."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        return False
    head = bytes(buf[:end]).decode("latin-1")
    if not re.match(r"HTTP/1\.[01] 200[ \r]", head + "\r"):
        return True                                   # any other answer, a redirect too, is "no answer"
    if re.search(r"(?im)^transfer-encoding:", head):
        return buf.endswith(b"\r\n0\r\n\r\n")
    m = re.search(r"(?im)^content-length:[ \t]*(\d{1,9})[ \t]*$", head)
    return bool(m) and len(buf) - end - 4 >= int(m.group(1))


def _ask(addr: str, port: int, request: bytes, end: float) -> bytes | None:
    """Send *request* and read the reply, all by the monotonic time *end*; None when late, too big or failed."""
    left = end - time.monotonic()
    if left <= 0:
        return None
    try:
        with socket.create_connection((addr, port), timeout=left) as sock:
            sock.settimeout(max(0.01, end - time.monotonic()))
            sock.sendall(request)
            buf = bytearray()
            while not _whole_reply(buf):
                left = end - time.monotonic()
                if left <= 0:
                    return None                       # the deadline is for the whole ask, not each read
                sock.settimeout(left)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > PEOPLE_MOST + 16 * 1024:
                    return None
            return bytes(buf)
    except OSError:
        return None


def read_shared_people(api: str, token: str, timeout: float = PEOPLE_DEADLINE_S) -> list[dict] | None:
    """The Core's shared people list (GET {api}/v1/people), or None when it does not answer.

    Only an http address on this computer is dialed, with http.client parsing a reply read by this code: no
    proxy, no redirect ever followed (any answer but 200 is "no answer"), one deadline for the whole ask, and
    at most PEOPLE_MOST bytes of answer."""
    if not api or not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,512}", token):
        return None
    target = local_target(api)
    if target is None:
        return None
    host, port, netloc, path = target
    end = time.monotonic() + max(0.0, min(timeout, PEOPLE_DEADLINE_S))
    request = (f"GET {path} HTTP/1.1\r\nHost: {netloc}\r\nAuthorization: Bearer {token}\r\n"
               "Accept: application/json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii")
    raw = None
    for addr in (("127.0.0.1", "::1") if host == "localhost" else (host,)):    # never a name lookup
        raw = _ask(addr, port, request, end)
        if raw is not None:
            break
    if not raw:
        return None
    try:
        resp = http.client.HTTPResponse(_Canned(raw))  # type: ignore[arg-type]
        resp.begin()
        if resp.status != 200:
            return None
        body = resp.read(PEOPLE_MOST + 1)
        if len(body) > PEOPLE_MOST:
            return None
        data = _loads(body.decode("utf-8"))
    except (http.client.HTTPException, ValueError, OSError):
        return None
    if not isinstance(data, list):
        return None
    out = []
    for p in data[:MAX_PEOPLE]:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("name"), str):
            pid, name = p["id"].strip()[:40], " ".join(p["name"].split())[:NAME_MOST]
            if pid and name:
                out.append({"id": pid, "name": name})
    return out


# ---------------------------------------------------------------- the logic

WORD_LEAST = 3                           # a typed word shorter than this matches only as an exact full name


def _words(text) -> list[str]:
    """The words of a name: letters and digits, lower case; an apostrophe is dropped ("John's" is "johns");
    anything else separates them."""
    return "".join("" if ch in "'’" else ch if ch.isalnum() else " " for ch in str(text or "").lower()).split()


def _word_hit(given: list[str], name: str, reading: bool = False) -> bool:
    """Every word given is 3 letters or more and starts a word of *name* (a whole word starts itself).

    A shorter word never matches here: "s" or "au" find a record only when they are its exact full name. Only
    when *reading* (list_records, which changes nothing), a 2-character word with a digit in it, like t4 or b6,
    may match a whole word of *name*: "t4" finds "Free T4"."""
    have = _words(name)
    return bool(given) and all(
        (len(w) >= WORD_LEAST and any(n.startswith(w) for n in have))
        or (reading and len(w) == 2 and any(ch.isdigit() for ch in w) and w in have) for w in given)


def _title(r: dict) -> str:
    """A record's name: its medicine, condition, test, vaccine, question..."""
    return str(r.get(TYPES.get(r.get("type"), {}).get("title", ""), "") or "")


def _name_hit(name: str, r: dict) -> bool:
    """For list_records: *name* is the record's exact name, or its words (3 letters or more each) start words of
    the record's line (its name, a result's lab...)."""
    return name_key(name) == name_key(_title(r)) or _word_hit(_words(name), describe(r), reading=True)


class HealthFile:
    def __init__(self, store: Store, today=date.today, api: str = "", token: str = "", shared=None) -> None:
        self.store = store
        self.today = today                    # a function, so a test can move the day
        self.api, self.token = api, token
        self._shared_fetch = shared or (lambda: read_shared_people(self.api, self.token))
        self._shared_cache: tuple[float, list | None] | None = None     # (good until, the list or None)
        self._shared_lock = threading.Lock()

    # ---- people
    def shared(self) -> list[dict] | None:
        """The Core's people list, asked at most once a minute (once every 30 s after it did not answer).

        Never called while the data lock is held: every caller asks first, then takes the lock. One ask at a
        time; it ends within PEOPLE_DEADLINE_S, so a slow Core holds other requests up for at most that long."""
        with self._shared_lock:
            c = self._shared_cache
            if c is not None and time.monotonic() < c[0]:
                return c[1]
            got = self._shared_fetch()
            self._shared_cache = (time.monotonic() + (SHARED_FOR_S if got is not None else SHARED_FAILED_FOR_S), got)
            return got

    def people(self, doc: dict, shared: list[dict] | None) -> tuple[list[dict], str]:
        """The household as [{id, name, ids, source}] and where it came from (shared or own).

        *shared* is what shared() gave, asked before the data lock was taken."""
        own = [p for p in doc["people"] if isinstance(p.get("id"), str) and isinstance(p.get("name"), str)]
        if not shared:
            return [{"id": o["id"], "name": o["name"], "source": "own",
                     "ids": {o["id"]} | ({o["shared_id"]} if isinstance(o.get("shared_id"), str) else set())}
                    for o in own], "own"
        out, used = [], set()
        for s in shared:
            ids = {s["id"]}
            for o in own:
                if o["id"] == s["id"] or o.get("shared_id") == s["id"] or name_key(o["name"]) == name_key(s["name"]):
                    ids.add(o["id"])
                    used.add(o["id"])
            out.append({"id": s["id"], "name": s["name"], "source": "shared", "ids": ids})
        out += [{"id": o["id"], "name": o["name"], "source": "own", "ids": {o["id"]}} for o in own
                if o["id"] not in used]
        return out, "shared"

    def find_person(self, doc: dict, person, shared: list[dict] | None) -> dict:
        p = self._match_person(doc, person, shared)
        if p is not None:
            return p
        everyone, _ = self.people(doc, shared)
        known = ", ".join(x["name"] for x in everyone) or "nobody yet"
        raise ValueError(f"there is no person called {_text(person, 'person', NAME_MOST)!r} in Health File "
                         f"(the household: {known}); add them first")

    def _match_person(self, doc: dict, person, shared: list[dict] | None) -> dict | None:
        """The person by id or by name (capital letters and spaces ignored); None when nobody matches."""
        everyone, _ = self.people(doc, shared)
        key = _text(person, "person", NAME_MOST)
        if not key:
            if len(everyone) == 1:
                return everyone[0]
            raise ValueError("say which person, by name")
        for p in everyone:
            if p["id"] == key:
                return p
        for p in everyone:
            if name_key(p["name"]) == name_key(key):
                return p
        return None

    def _person_or_new(self, doc: dict, person, shared: list[dict] | None) -> tuple[dict, str]:
        """For a tool: the person, or, for a name not in the household, a new person in the app's own list.

        Returns the person and the sentence to add to the answer ("" or "Added Alex to Health File.").
        The caller holds the data lock and saves the document."""
        p = self._match_person(doc, person, shared)
        if p is not None:
            return p, ""
        name = _text(person, "person", NAME_MOST)
        if re.fullmatch(r"own-[0-9a-f]{8}", name):
            raise ValueError(f"there is no person with the id {name!r} in Health File")
        if len(doc["people"]) >= MAX_PEOPLE:
            raise ValueError(f"Health File holds at most {MAX_PEOPLE} people")
        o = {"id": "own-" + secrets.token_hex(4), "name": name, "added": self.today().isoformat()}
        doc["people"].append(o)
        return {"id": o["id"], "name": name, "source": "own", "ids": {o["id"]}}, f"Added {name} to Health File."

    @staticmethod
    def _mirror(doc: dict, p: dict) -> None:
        """Keep a person from the shared list in the app's own list too, so their records show when it is away."""
        for o in doc["people"]:
            if o.get("id") == p["id"] or o.get("shared_id") == p["id"]:
                return
        for o in doc["people"]:
            if name_key(str(o.get("name", ""))) == name_key(p["name"]) and o.get("id") in p["ids"]:
                o["shared_id"] = p["id"]
                return
        doc["people"].append({"id": p["id"], "name": p["name"], "added": date.today().isoformat(), "shared": True})

    def add_person(self, name) -> dict:
        name = _text(name, "name", NAME_MOST)
        if not name:
            raise ValueError("a person needs a name")
        shared = self.shared()
        with self.store.lock:
            doc = self.store.load_for_change()
            everyone, _ = self.people(doc, shared)
            if any(name_key(p["name"]) == name_key(name) for p in everyone):
                raise ValueError(f"{name} is already in the household")
            if len(doc["people"]) >= MAX_PEOPLE:
                raise ValueError(f"Health File holds at most {MAX_PEOPLE} people")
            p = {"id": "own-" + secrets.token_hex(4), "name": name, "added": self.today().isoformat()}
            doc["people"].append(p)
            self.store.save(doc)
        return {"id": p["id"], "text": f"Added {name} to Health File."}

    def remove_person(self, person) -> str:
        shared = self.shared()
        with self.store.lock:
            doc = self.store.load_for_change()
            p = self.find_person(doc, person, shared)
            if p["source"] == "shared":
                raise ValueError(f"{p['name']} comes from PuffPAI's shared people list; change it there")
            if any(r.get("person") in p["ids"] for r in doc["records"]):
                raise ValueError(f"{p['name']} still has records; remove their records first")
            doc["people"] = [o for o in doc["people"] if o.get("id") not in p["ids"]]
            self.store.save(doc)
        return f"Removed {p['name']} from Health File."

    # ---- records
    @staticmethod
    def _of(doc: dict, p: dict, kind: str | None = None) -> list[dict]:
        return [r for r in doc["records"] if isinstance(r.get("person"), str) and r["person"] in p["ids"]
                and isinstance(r.get("type"), str) and r["type"] in TYPES
                and (kind is None or r.get("type") == kind)]

    def _find_record(self, doc: dict, record, person=None, shared: list[dict] | None = None,
                     said: list | None = None) -> dict:
        """One record by its id, or by its words (its medicine, condition, test, vaccine...: exact first with case
        and spaces ignored, then the one record where every word given, each 3 letters or more, starts a word of
        its name; never letters inside a word), among *person*'s records when a person is named. An id with a
        person named must be that person's record. A match that is not exact adds "Showing <name>." to *said*.
        More than one match, or none, changes nothing: the error lists only the records' names, and lists records
        only when a person is named (then only theirs)."""
        key = _text(record, "record", 300)
        if not key:
            raise ValueError("say which record, in plain words like ibuprofen or flu shot")
        who = self.find_person(doc, person, shared) if person not in (None, "") else None
        for r in doc["records"]:
            if r.get("id") == key:
                if who is not None and r.get("person") not in who["ids"]:
                    raise ValueError(f"the record {key!r} is not {who['name']}'s; nothing was changed")
                return r
        if re.fullmatch(r"r[0-9a-f]{8}", key):
            raise ValueError(f"there is no record with the id {key!r}; say it in plain words instead")
        pool = self._of(doc, who) if who else [r for r in doc["records"] if r.get("type") in TYPES]
        want = name_key(key)
        found = [r for r in pool if name_key(_title(r)) == want]
        if not found:
            given = _words(key)
            found = [r for r in pool if _word_hit(given, _title(r))]
            if len(found) == 1 and said is not None:
                said.append(f"Showing {_title(found[0])}.")
        if len(found) == 1:
            return found[0]
        names = {} if who else {i: p["name"] for p in self.people(doc, shared)[0] for i in p["ids"]}

        def named(r: dict) -> str:
            owner = "" if who else f"{names.get(r.get('person'), 'someone')}'s "
            return f"{owner}{TYPES[r['type']]['label'].lower()} {_title(r)} [{r['id']}]"
        if not found and not who:          # never a list of everyone's records
            raise ValueError(f"there is no record matching {key!r}; nothing was changed. Say whose record it is, "
                             f"like Alex's, and its name")
        if not found:
            some = "; ".join(named(r) for r in sorted(pool, key=sort_key)[:10]) or "none yet"
            raise ValueError(f"there is no record matching {key!r} for {who['name']} (the records: {some})")
        raise ValueError(f"More than one record matches {key!r}: {'; '.join(named(r) for r in found[:10])}. "
                         f"Say which; nothing was shown or changed.")

    def add_record(self, person=None, type=None, fields=None, **more) -> dict:  # noqa: A002 - the tool's word
        """The add_record tool: a name not in the household is added to the app's own list (and said so).

        type "person" with just a name adds that person."""
        if isinstance(type, str) and type.strip().lower() in PERSON_WORDS:
            return self._add_person_by_tool(person, fields, more)
        return self._add_record(person, type, fields, more, may_add_person=True)

    def page_add_record(self, person=None, type=None, fields=None) -> dict:  # noqa: A002
        """The page's add: the page picks a person from its list, so an unknown one is refused, never added."""
        return self._add_record(person, type, fields, {}, may_add_person=False)

    def _add_person_by_tool(self, person, fields, more) -> dict:
        if fields is not None and not isinstance(fields, dict):
            raise ValueError("fields is an object, like {\"name\": \"Alex\"}")
        extra = {**(fields or {}), **more}
        name = _text(person, "person", NAME_MOST) or _text(extra.pop("name", None), "name", NAME_MOST)
        if extra:
            raise ValueError("to add a person, give just their name")
        if not name:
            raise ValueError("a person needs a name")
        shared = self.shared()
        with self.store.lock:
            doc = self.store.load_for_change()
            p = self._match_person(doc, name, shared)
            if p is not None:
                return {"id": p["id"], "text": f"{p['name']} is already in Health File."}
            p, said = self._person_or_new(doc, name, shared)
            self.store.save(doc)
        return {"id": p["id"], "text": said}

    def _add_record(self, person, type, fields, more: dict, may_add_person: bool) -> dict:  # noqa: A002
        kind = kind_of(type)
        if fields is not None and not isinstance(fields, dict):
            raise ValueError("fields is an object, like {\"name\": \"Asthma\"}")
        fields = {**(fields or {}), **more}
        today = self.today()
        clean = clean_fields(kind, fields, today)
        shared = self.shared()                         # asked before the data lock, never while holding it
        with self.store.lock:
            doc = self.store.load_for_change()
            if may_add_person:
                p, said = self._person_or_new(doc, person, shared)
            else:
                p, said = self.find_person(doc, person, shared), ""
            if len(doc["records"]) >= MAX_RECORDS:
                raise ValueError(f"Health File holds at most {MAX_RECORDS} records")
            if p["source"] == "shared":
                self._mirror(doc, p)
            r = {"id": "r" + secrets.token_hex(4), "person": p["id"], "type": kind, **clean,
                 "added_on": today.isoformat(), "changed_on": today.isoformat()}
            doc["records"].append(r)
            self.store.save(doc)
        text = (said + " " if said else "") + \
            f"Added to {p['name']}'s file: {TYPES[kind]['label'].lower()} {describe(r)} [{r['id']}]."
        if kind in ("result", "medicine"):
            text += "\n" + DISCLAIMER
        return {"id": r["id"], "text": text}

    def change_record(self, record=None, person=None, fields=None, **more) -> str:
        if fields is not None and not isinstance(fields, dict):
            raise ValueError("fields is an object, like {\"status\": \"stopped\"}")
        fields = {**(fields or {}), **more}
        if not fields:
            raise ValueError("say what to change in fields")
        today = self.today()
        shared = self.shared() if person not in (None, "") or not _is_record_id(record) else None
        said: list[str] = []
        with self.store.lock:
            doc = self.store.load_for_change()
            r = self._find_record(doc, record, person, shared, said)
            kind = r.get("type")
            if kind not in TYPES:
                raise ValueError("that record is of a kind this version does not know")
            r.update(clean_fields(kind, fields, today, r))
            r["changed_on"] = today.isoformat()
            self.store.save(doc)
        text = " ".join(said + [f"Changed {TYPES[kind]['label'].lower()} {describe(r)} [{r['id']}]."])
        if kind in ("result", "medicine"):
            text += "\n" + DISCLAIMER
        return text

    def remove_record(self, record=None, person=None) -> str:
        shared = self.shared() if person not in (None, "") or not _is_record_id(record) else None
        said: list[str] = []
        with self.store.lock:
            doc = self.store.load_for_change()
            r = self._find_record(doc, record, person, shared, said)
            doc["records"] = [x for x in doc["records"] if x is not r]
            self.store.save(doc)
        return " ".join(said) + (" " if said else "") + f"Removed {TYPES.get(r.get('type'), {}).get('label', 'record').lower()} {describe(r)}."

    def add_question(self, person=None, text=None) -> str:
        out = self.add_record(person=person, type="question", fields={"text": text})
        return out["text"] + " It shows under open questions until it is marked asked."

    def list_records(self, person=None, type=None, name=None, test=None) -> str:  # noqa: A002
        """One kind of record, or all. For results: with one test's name, that test over time; with other words
        (several tests, a lab), every result they match; without, every result. *test* is taken as the name too,
        since a small model may send it."""
        kind = kind_of(type) if type not in (None, "", "all") else None
        name = _text(name, "name", 120) or _text(test, "test", 120)
        if kind is None and _text(test, "test", 120):
            kind = "result"
        shared = self.shared()
        doc = self.store.load()
        p = self.find_person(doc, person, shared)
        if kind is None and name:           # words that match only results, like cholesterol: about results
            hits = [r for r in self._of(doc, p) if _name_hit(name, r)]
            if hits and all(r["type"] == "result" for r in hits):
                kind = "result"
        if kind == "result":
            return self._results_text(doc, p, name)
        recs = [r for r in self._of(doc, p, kind) if not name or _name_hit(name, r)]
        recs.sort(key=sort_key)
        what = TYPES[kind]["plural"].lower() if kind else "records"
        if not recs:
            text = f"{p['name']} has no {what}" + (f" matching {name.lower()!r}" if name else "") + " in Health File."
        else:
            lines = []
            for k in TYPES:
                group = [r for r in recs if r["type"] == k]
                if group:
                    lines.append(f"{TYPES[k]['plural']}:")
                    lines += [f"- {describe(r)} [{r['id']}]" for r in group]
            text = f"{p['name']}'s {what} ({len(recs)}):\n" + "\n".join(lines)
        if kind in (None, "result", "medicine"):
            text += "\n" + DISCLAIMER
        return text

    def history(self, doc: dict, p: dict, test: str) -> list[dict]:
        key = test.lower()
        pts = [r for r in self._of(doc, p, "result") if str(r.get("test", "")).lower() == key]
        pts.sort(key=lambda r: (str(r.get("date", "")), str(r.get("added_on", ""))))
        out = []
        for r in pts:
            rng = printed_range(r)
            out.append({"id": r["id"], "date": r.get("date", ""), "value": r.get("value", ""),
                        "number": number(r.get("value")), "unit": r.get("unit", ""), "range": range_words(r),
                        "low": rng[0] if rng else None, "high": rng[1] if rng else None, "flag": flag(r),
                        "flag_note": flag_note(r), "lab": r.get("lab", "")})
        return out

    def _results_text(self, doc: dict, p: dict, test: str) -> str:
        """The exact name of one test, or words that start the words of only one test (ldl: LDL cholesterol): that
        test's values over time, oldest first. Words that fit several tests (cholesterol: HDL and LDL), or other
        words of a result (its lab, like Quest): every result they match. No words: every result."""
        everything = self._of(doc, p, "result")
        if not test:
            if not everything:
                return f"{p['name']} has no results in Health File.\n" + DISCLAIMER
            return self._results_list(everything, f"{p['name']}'s results")
        pts = self.history(doc, p, test)
        shown = ""
        if not pts:
            given = _words(test)
            near = {str(r.get("test")).lower() for r in everything
                    if _word_hit(given, str(r.get("test", "")), reading=True)}
            if len(near) > 1:
                return self._results_list([r for r in everything if str(r.get("test")).lower() in near],
                                          f"{p['name']}'s results matching {test!r}")
            if near:
                name = next(str(r.get("test")) for r in everything if str(r.get("test")).lower() in near)
                pts = self.history(doc, p, name)
                shown = f"Showing {name}.\n" if pts else ""
            else:
                hits = [r for r in everything if _word_hit(given, describe(r), reading=True)]
                if hits:
                    return self._results_list(hits, f"{p['name']}'s results matching {test!r}")
        if not pts:
            names = sorted({str(r.get("test")) for r in self._of(doc, p, "result")}, key=str.lower)
            text = (f"{p['name']} has no results for {test!r}. "
                    + (f"Tests in the file: {', '.join(names)}." if names else "No results are in the file yet."))
        else:
            lines = [f"- {x['date']}: {x['value']}{' ' + x['unit'] if x['unit'] else ''}"
                     f" ({'range ' + x['range'] if x['range'] else 'no range given'}; flag {x['flag']}"
                     + (f". {x['flag_note']}" if x["flag_note"] else "") + ")"
                     + (f", {x['lab']}" if x["lab"] else "") for x in pts]
            test = next(str(r.get("test")) for r in self._of(doc, p, "result") if r["id"] == pts[-1]["id"])
            text = (f"{shown}{p['name']}'s {test} over time ({_plural(len(pts), 'result')}; flags come only from the "
                    "range printed on each result):\n" + "\n".join(lines))
        return text + "\n" + DISCLAIMER

    @staticmethod
    def _results_list(recs: list[dict], heading: str) -> str:
        """Results as a list, each test's values together, oldest first."""
        recs = sorted(recs, key=lambda r: (str(r.get("test", "")).lower(), str(r.get("date", "")),
                                           str(r.get("added_on", ""))))
        lines = [f"- {describe(r)} [{r['id']}]" for r in recs]
        return (f"{heading} ({_plural(len(recs), 'result')}; flags come only from the range printed on each result; "
                "give one test's name to see it over time):\n" + "\n".join(lines) + "\n" + DISCLAIMER)

    # ---- the summary
    def summary(self, doc: dict, p: dict) -> dict:
        today = self.today().isoformat()
        recs = self._of(doc, p)
        by = {k: sorted([r for r in recs if r["type"] == k], key=sort_key) for k in TYPES}
        latest: dict = {}
        for r in sorted(by["result"], key=lambda r: (str(r.get("date", "")), str(r.get("added_on", "")))):
            latest[str(r.get("test", "")).lower()] = r
        coming = []
        for r in by["visit"]:
            if r.get("follow_up") and r["follow_up"] >= today:
                coming.append({"date": r["follow_up"], "what": "Follow-up" + (f" with {r['who']}" if r.get("who") else ""),
                               "late": False})
        for r in by["vaccination"]:
            due = r.get("due_again", "")
            later = any(str(o.get("vaccine", "")).lower() == str(r.get("vaccine", "")).lower()
                        and str(o.get("date", "")) > str(r.get("date", "")) for o in by["vaccination"])
            if due and not later:
                coming.append({"date": due, "what": f"{r.get('vaccine')} vaccination due", "late": due < today})
        coming.sort(key=lambda c: c["date"])
        return {
            "person": {"id": p["id"], "name": p["name"]}, "today": today,
            "allergies": [dict(r) for r in by["allergy"]],
            "conditions": [dict(r) for r in by["condition"] if r.get("status") != "resolved"],
            "medicines": [dict(r) for r in by["medicine"] if is_current(r, today)],
            "results": [{**r, "flag": flag(r), "flag_note": flag_note(r), "range": range_words(r)}
                        for r in sorted(latest.values(), key=lambda r: str(r.get("test", "")).lower())],
            "coming": coming,
            "questions": [dict(r) for r in by["question"] if not r.get("asked")],
            "disclaimer": DISCLAIMER,
        }

    def person_summary(self, person=None) -> str:
        shared = self.shared()
        doc = self.store.load()
        p = self.find_person(doc, person, shared)
        return summary_text(self.summary(doc, p))

    def prepare_for_visit(self, person=None) -> str:
        shared = self.shared()
        doc = self.store.load()
        p = self.find_person(doc, person, shared)
        s = self.summary(doc, p)
        today = s["today"]
        recs = self._of(doc, p)
        visits = sorted([r for r in recs if r["type"] == "visit" and r.get("date", "") <= today],
                        key=lambda r: r.get("date", ""))
        out = [f"Getting ready for {p['name']}'s next visit (today is {today})."]
        if s["allergies"]:
            out.append("ALLERGIES: " + "; ".join(describe(r) for r in s["allergies"]) + ".")
        out.append(f"Questions to ask ({len(s['questions'])}):")
        out += [f"- {r.get('text')}" for r in s["questions"]] or ["- none written down yet"]
        if visits:
            lv = visits[-1]
            out.append(f"Last visit: {describe(lv)}.")
            out.append("Since the last visit:")
            since = changes_since(recs, lv["date"])
            out += [f"- {line}" for line in since] or ["- nothing new in the file"]
        else:
            out.append("No earlier visit is in the file, so there is nothing to compare with.")
        out.append(f"Current medicines ({len(s['medicines'])}):")
        out += [f"- {describe(r)}" for r in s["medicines"]] or ["- none in the file"]
        return "\n".join(out) + "\n" + DISCLAIMER

    # ---- the page
    def view(self, person=None) -> dict:
        shared = self.shared()
        doc = self.store.load()
        everyone, source = self.people(doc, shared)
        out = {"people": [{"id": p["id"], "name": p["name"], "source": p["source"]} for p in everyone],
               "source": source, "last_updated": doc["last_updated"], "disclaimer": DISCLAIMER}
        if not everyone:
            return out
        p = self.find_person(doc, person, shared) if person not in (None, "") else everyone[0]
        recs = sorted(self._of(doc, p), key=sort_key)
        out["person"] = {"id": p["id"], "name": p["name"]}
        out["summary"] = self.summary(doc, p)
        out["records"] = {k: [{**r, **({"flag": flag(r), "flag_note": flag_note(r), "range": range_words(r)} if k == "result" else {})}
                              for r in recs if r["type"] == k] for k in TYPES}
        out["tests"] = sorted({str(r.get("test")) for r in recs if r["type"] == "result"}, key=str.lower)
        return out

    # ---- export and import
    def export(self, person=None, fmt: str = "app") -> dict:
        return self.export_counted(person, fmt)[0]

    def export_counted(self, person=None, fmt: str = "app") -> tuple[dict, int]:
        """(the export, how many records were skipped because they would not read)."""
        if fmt not in ("app", "fhir"):
            raise ValueError("format is app or fhir")
        shared = self.shared()
        doc = self.store.load()
        everyone, _ = self.people(doc, shared)
        chosen = [self.find_person(doc, person, shared)] if person not in (None, "") else everyone
        if fmt == "fhir":
            return to_fhir(chosen, doc["records"], self.today())
        recs = []
        for p in chosen:
            recs += [{**{k: v for k, v in r.items() if k != "person"}, "person": p["id"]} for r in self._of(doc, p)]
        return {"format": FORMAT, "exported": datetime.now().isoformat(timespec="seconds"),
                "people": [{"id": p["id"], "name": p["name"]} for p in chosen], "records": recs}, 0

    def import_file(self, data, person=None) -> dict:
        """Add what a file holds; nothing already in Health File changes. Returns counts and a sentence."""
        if not isinstance(data, dict):
            raise ValueError("that file is not a Health File export or a FHIR Bundle")
        if data.get("resourceType") == "Bundle":
            people, recs, skipped = from_fhir(data)
        elif data.get("format") == FORMAT:
            people, recs, skipped = from_app(data)
        else:
            raise ValueError("that file is not a Health File export or a FHIR Bundle")
        today = self.today()
        added: dict = {}
        shared = self.shared()                         # asked before the data lock, never while holding it
        with self.store.lock:
            doc = self.store.load_for_change()
            target = self.find_person(doc, person, shared) if person not in (None, "") else None
            seen = {(r.get("person"), fingerprint(r)) for r in doc["records"]}
            made: dict = {}
            for key, kind, fields in recs:
                label = TYPES[kind]["plural"].lower()
                try:
                    clean = clean_fields(kind, fields, today)
                except ValueError:
                    skipped[label] = skipped.get(label, 0) + 1
                    continue
                p = target or made.get(key)
                if p is None:
                    name = people.get(key, "")
                    if not name:
                        skipped[label] = skipped.get(label, 0) + 1
                        continue
                    p = self._person_for_import(doc, name, shared)
                    made[key] = p
                if p is None or len(doc["records"]) >= MAX_RECORDS:
                    skipped[label] = skipped.get(label, 0) + 1
                    continue
                r = {"id": "r" + secrets.token_hex(4), "person": p["id"], "type": kind, **clean,
                     "added_on": today.isoformat(), "changed_on": today.isoformat()}
                fp = fingerprint(r)
                if any((i, fp) in seen for i in p["ids"]):
                    skipped[label] = skipped.get(label, 0) + 1
                    continue
                seen.add((p["id"], fp))
                doc["records"].append(r)
                added[label] = added.get(label, 0) + 1
            if added:
                self.store.save(doc)
        words = ", ".join(f"{n} {k}" for k, n in added.items()) or "nothing"
        text = f"Imported {words}."
        if skipped:
            text += " Skipped " + ", ".join(f"{n} {k}" for k, n in skipped.items()) + \
                    " (already in the file, missing what they need, or of a kind Health File does not keep)."
        return {"imported": added, "skipped": skipped, "text": text}

    def _person_for_import(self, doc: dict, name: str, shared: list[dict] | None) -> dict | None:
        everyone, _ = self.people(doc, shared)
        for p in everyone:
            if name_key(p["name"]) == name_key(name):
                if p["source"] == "shared":
                    self._mirror(doc, p)
                return p
        name = _text(name, "name", NAME_MOST)
        if not name or len(doc["people"]) >= MAX_PEOPLE:
            return None
        o = {"id": "own-" + secrets.token_hex(4), "name": name, "added": self.today().isoformat()}
        doc["people"].append(o)
        return {"id": o["id"], "name": name, "source": "own", "ids": {o["id"]}}


def is_current(r: dict, today: str) -> bool:
    return r.get("status") != "stopped" and (not r.get("until") or _not_before(r["until"], today))


def sort_key(r: dict) -> tuple:
    """Records by type; conditions, medicines and allergies by name; the rest by date."""
    k = r.get("type")
    if k not in TYPES:
        return 99, "", ""
    i = list(TYPES).index(k)
    if k in ("result", "visit", "vaccination"):
        return i, str(r.get("date", "")), str(r.get("test", "")).lower()
    if k == "question":
        return i, str(r.get("added", "")), ""
    return i, str(r.get(TYPES[k]["title"], "")).lower(), str(r.get("since", ""))


def fingerprint(r: dict) -> str:
    """What a record says, without its id, owner or stamps, in lower case: two records alike are duplicates."""
    keep = {k: (v.lower() if isinstance(v, str) else v) for k, v in r.items()
            if k not in ("id", "person", "added_on", "changed_on")}
    return json.dumps(keep, sort_keys=True, ensure_ascii=False)


def describe(r: dict) -> str:
    """One plain line for a record."""
    k = r.get("type")
    bits: list[str] = []
    if k == "condition":
        bits = [str(r.get("name")), r.get("status", "")]
        if r.get("since"):
            bits.append(f"since {r['since']}")
        if r.get("until"):
            bits.append(f"until {r['until']}")
    elif k == "medicine":
        bits = [" ".join(x for x in (str(r.get("name")), r.get("dose", "")) if x), r.get("how_often", ""),
                r.get("status", "")]
        if r.get("reason"):
            bits.append(f"for {r['reason']}")
        if r.get("since"):
            bits.append(f"since {r['since']}")
        if r.get("until"):
            bits.append(f"until {r['until']}")
        if r.get("prescriber"):
            bits.append(f"prescribed by {r['prescriber']}")
    elif k == "allergy":
        bits = [str(r.get("what")), r.get("reaction", ""), r.get("severity", "")]
    elif k == "result":
        rng = range_words(r)
        bits = [f"{r.get('test')} {r.get('value')}{' ' + r['unit'] if r.get('unit') else ''} on {r.get('date')}",
                f"range {rng}" if rng else "no range given",
                f"flag {flag(r)} ({flag_note(r)})" if flag_note(r) else f"flag {flag(r)}", r.get("lab", "")]
    elif k == "visit":
        bits = [str(r.get("date")), r.get("who", ""), r.get("why", "")]
        if r.get("follow_up"):
            bits.append(f"follow-up {r['follow_up']}")
    elif k == "vaccination":
        bits = [str(r.get("vaccine")), r.get("date", "")]
        if r.get("lot"):
            bits.append(f"lot {r['lot']}")
        if r.get("due_again"):
            bits.append(f"due again {r['due_again']}")
    elif k == "question":
        bits = [str(r.get("text")), "asked" if r.get("asked") else "not asked yet"]
        if r.get("answer"):
            bits.append(f"answer: {' '.join(str(r['answer']).split())}")
    return ", ".join(b for b in bits if b)


def summary_text(s: dict) -> str:
    out = [f"Health summary for {s['person']['name']} (as of {s['today']})."]
    out.append("ALLERGIES: " + ("; ".join(describe(r) for r in s["allergies"]) if s["allergies"]
                                else "none recorded in Health File") + ".")
    sections = [("Active conditions", s["conditions"], describe),
                ("Current medicines", s["medicines"], describe),
                ("Latest results", s["results"], describe),
                ("Coming up", s["coming"], lambda c: f"{c['date']}: {c['what']}" + (" (overdue)" if c["late"] else "")),
                ("Open questions for the next visit", s["questions"], lambda r: str(r.get("text")))]
    for title, items, line in sections:
        out.append(f"{title} ({len(items)}):")
        out += [f"- {line(x)}" for x in items] or ["- none"]
    return "\n".join(out) + "\n" + s["disclaimer"]


def changes_since(recs: list[dict], last: str) -> list[str]:
    out = []
    for r in sorted(recs, key=sort_key):
        k = r.get("type")
        if k in ("visit", "question"):
            continue
        added = _after(str(r.get("added_on", "")), last)
        if k == "medicine":
            if r.get("until") and _after(r["until"], last):
                out.append(f"Medicine stopped: {describe(r)}")
            elif r.get("since") and _after(r["since"], last):
                out.append(f"Medicine started: {describe(r)}")
            elif added:
                out.append(f"Medicine added to the file: {describe(r)}")
        elif k == "condition":
            if r.get("until") and _after(r["until"], last):
                out.append(f"Condition ended: {describe(r)}")
            elif (r.get("since") and _after(r["since"], last)) or added:
                out.append(f"Condition: {describe(r)}")
        elif k in ("result", "vaccination"):
            if _after(str(r.get("date", "")), last) or added:
                out.append(f"{TYPES[k]['label']}: {describe(r)}")
        elif k == "allergy" and added:
            out.append(f"Allergy: {describe(r)}")
    return out


# ---------------------------------------------------------------- FHIR R4

EXT = "https://github.com/CypherSisters/puffpai-packages/health-file/fhir/"
NS = uuid.UUID("6f1d1c3e-2b4a-4c7e-9a51-0c2f6b7d8e90")


def _urn(key: str) -> str:
    return "urn:uuid:" + str(uuid.uuid5(NS, key))


def _cc(text: str) -> dict:
    return {"text": text}


def to_fhir(people: list[dict], records: list[dict], today: date) -> tuple[dict, int]:
    """(the FHIR R4 Bundle, how many records were skipped because they would not read).

    Records are read defensively: one edited by hand with a missing or odd field is skipped and counted,
    never a failed export."""
    entries, skipped = [], 0
    usable = []
    for r in records:
        if isinstance(r, dict) and isinstance(r.get("person"), str) and isinstance(r.get("type"), str):
            usable.append(r)
        else:
            skipped += 1                                  # no owner or kind that can be read
    for p in people:
        pref = _urn("patient:" + p["id"])
        entries.append({"fullUrl": pref, "resource": {"resourceType": "Patient", "name": [{"text": p["name"]}]}})
        for r in usable:
            try:
                if r["person"] not in p["ids"] or r["type"] not in TYPES:
                    continue
                res = _fhir_one(r, {"reference": pref, "display": p["name"]}, today)
                if res is not None:
                    json.dumps(res, allow_nan=False)      # anything that would not write out is skipped here
                    entries.append({"fullUrl": _urn("record:" + str(r.get("id"))), "resource": res})
            except (KeyError, TypeError, ValueError, AttributeError):
                skipped += 1
    return {"resourceType": "Bundle", "type": "collection",
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), "entry": entries}, skipped


def _fhir_one(r: dict, who: dict, today: date) -> dict | None:
    k, note = r["type"], [{"text": r["note"]}] if r.get("note") else []
    if k == "condition":
        res = {"resourceType": "Condition", "clinicalStatus": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": r.get("status") or "active"}],
            "text": r.get("status") or "active"}, "code": _cc(r["name"]), "subject": who}
        if r.get("since"):
            res["onsetDateTime"] = r["since"]
        if r.get("until"):
            res["abatementDateTime"] = r["until"]
    elif k == "medicine":
        res = {"resourceType": "MedicationStatement", "status": "stopped" if r.get("status") == "stopped" else "active",
               "medicationCodeableConcept": _cc(r["name"]), "subject": who}
        period = {k2: r[k1] for k1, k2 in (("since", "start"), ("until", "end")) if r.get(k1)}
        if period:
            res["effectivePeriod"] = period
        dose = {}
        if r.get("dose"):
            dose["text"] = r["dose"]
        if r.get("how_often"):
            dose["timing"] = {"code": _cc(r["how_often"])}
        if dose:
            res["dosage"] = [dose]
        if r.get("reason"):
            res["reasonCode"] = [_cc(r["reason"])]
        if r.get("prescriber"):
            note = [{"text": "Prescriber: " + r["prescriber"]}] + note
    elif k == "allergy":
        res = {"resourceType": "AllergyIntolerance", "code": _cc(r["what"]), "patient": who}
        if r.get("reaction") or r.get("severity"):
            rx: dict = {"manifestation": [_cc(r.get("reaction") or "not recorded")]}
            if r.get("severity"):
                rx["severity"] = r["severity"]
            res["reaction"] = [rx]
    elif k == "result":
        res = {"resourceType": "Observation", "status": "final", "code": _cc(r["test"]), "subject": who,
               "effectiveDateTime": r["date"]}
        v = number(r.get("value"))
        if v is not None:
            q: dict = {"value": v}
            if r.get("unit"):
                q["unit"] = r["unit"]
            res["valueQuantity"] = q
        else:
            res["valueString"] = r["value"]
        rr: dict = {}
        for side in ("low", "high"):
            x = number(r.get("range_" + side))
            if x is not None:
                rr[side] = {"value": x, **({"unit": r["unit"]} if r.get("unit") else {})}
        if r.get("range_text"):
            rr["text"] = r["range_text"]
        if rr:
            res["referenceRange"] = [rr]
        if flag(r) != "none":
            res["interpretation"] = [_cc(flag(r))]
        if r.get("lab"):
            res["performer"] = [{"display": r["lab"]}]
    elif k == "visit":
        res = {"resourceType": "Encounter", "status": "finished" if r["date"] <= today.isoformat() else "planned",
               "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB",
                         "display": "ambulatory"}, "subject": who, "period": {"start": r["date"]}}
        if r.get("who"):
            res["participant"] = [{"individual": {"display": r["who"]}}]
        if r.get("why"):
            res["reasonCode"] = [_cc(r["why"])]
        ext = [{"url": EXT + key, "valueString" if key == "said" else "valueDate": r[key]}
               for key in ("said", "follow_up") if r.get(key)]
        if ext:
            res["extension"] = ext
        note = []
    elif k == "vaccination":
        res = {"resourceType": "Immunization", "status": "completed", "vaccineCode": _cc(r["vaccine"]),
               "patient": who, "occurrenceDateTime": r.get("date") or today.isoformat()}
        if r.get("lot"):
            res["lotNumber"] = r["lot"]
        if r.get("due_again"):
            res["extension"] = [{"url": EXT + "due_again", "valueDate": r["due_again"]}]
        note = []
    else:
        return None                     # questions stay in this app's own format
    if note:
        res["note"] = note
    return res


def _s(value, most: int = 2000) -> str:
    return value.strip()[:most] if isinstance(value, str) else ""


def _cc_text(value) -> str:
    """Words from a CodeableConcept: its text, else the first coding's display."""
    if not isinstance(value, dict):
        return ""
    if _s(value.get("text")):
        return _s(value.get("text"))
    for c in value.get("coding") if isinstance(value.get("coding"), list) else []:
        if isinstance(c, dict) and _s(c.get("display")):
            return _s(c.get("display"))
    return ""


def _first(value) -> dict:
    return value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else {}


def _d(value) -> str:
    return _s(value)[:10]


def _numfield(value) -> str:
    x = number(value) if not isinstance(value, str) else None
    return _numtext(x) if x is not None else ""


def _ext(res: dict, key: str) -> str:
    for e in res.get("extension") if isinstance(res.get("extension"), list) else []:
        if isinstance(e, dict) and e.get("url") == EXT + key:
            return _s(e.get("valueString") or e.get("valueDate"))
    return ""


def from_fhir(bundle: dict):
    """(people {key: name}, [(person key, type, fields)], skipped {kind: n}) from a FHIR R4 Bundle, read defensively."""
    people: dict = {}
    recs: list = []
    skipped: dict = {}
    entries = bundle.get("entry") if isinstance(bundle.get("entry"), list) else []
    for e in entries:
        res = e.get("resource") if isinstance(e, dict) else None
        if isinstance(res, dict) and res.get("resourceType") == "Patient":
            n = _first(res.get("name"))
            name = _s(n.get("text"), NAME_MOST) or " ".join(
                [x for x in (n.get("given") if isinstance(n.get("given"), list) else []) if isinstance(x, str)]
                + ([n["family"]] if isinstance(n.get("family"), str) else [])).strip()[:NAME_MOST]
            for key in (_s(e.get("fullUrl")), f"Patient/{_s(res.get('id'))}" if _s(res.get("id")) else ""):
                if key and name:
                    people[key] = name
    for e in entries:
        res = e.get("resource") if isinstance(e, dict) else None
        if not isinstance(res, dict) or res.get("resourceType") == "Patient":
            continue
        rt = res.get("resourceType") if isinstance(res.get("resourceType"), str) else ""
        who =res.get("patient") if rt in ("AllergyIntolerance", "Immunization") else res.get("subject")
        key = _s(who.get("reference")) if isinstance(who, dict) else ""
        try:
            kind, fields = _from_resource(rt, res)
        except (TypeError, ValueError, AttributeError, KeyError):
            kind, fields = None, None
        if kind is None:
            label = TYPES[{v["fhir"]: k for k, v in TYPES.items() if v["fhir"]}[rt]]["plural"].lower() \
                if rt in {v["fhir"] for v in TYPES.values() if v["fhir"]} else "other entries"
            skipped[label] = skipped.get(label, 0) + 1
            continue
        recs.append((key, kind, fields))
    return people, recs, skipped


def _from_resource(rt, res: dict):
    note = [_s(n.get("text")) for n in res.get("note", []) if isinstance(n, dict)] \
        if isinstance(res.get("note"), list) else []
    if rt == "Condition":
        status = _cc_text(res.get("clinicalStatus")).lower() or _s(_first(
            (res.get("clinicalStatus") or {}).get("coding") if isinstance(res.get("clinicalStatus"), dict) else []
        ).get("code")).lower()
        return "condition", {"name": _cc_text(res.get("code")),
                             "status": "resolved" if status in ("resolved", "inactive", "remission") else "active",
                             "since": _d(res.get("onsetDateTime")), "until": _d(res.get("abatementDateTime")),
                             "note": "\n".join(n for n in note if n)}
    if rt == "MedicationStatement":
        period = res.get("effectivePeriod") if isinstance(res.get("effectivePeriod"), dict) else {}
        dose = _first(res.get("dosage"))
        timing = dose.get("timing") if isinstance(dose.get("timing"), dict) else {}
        prescriber = next((n[len("Prescriber: "):] for n in note if n.startswith("Prescriber: ")), "")
        return "medicine", {
            "name": _cc_text(res.get("medicationCodeableConcept")), "dose": _s(dose.get("text"), 80),
            "how_often": _cc_text(timing.get("code"))[:80], "since": _d(period.get("start")),
            "until": _d(period.get("end")),
            "status": "stopped" if res.get("status") in ("stopped", "completed") else "taking",
            "reason": _cc_text(_first(res.get("reasonCode")))[:120], "prescriber": prescriber[:80],
            "note": "\n".join(n for n in note if n and not n.startswith("Prescriber: "))}
    if rt == "AllergyIntolerance":
        rx = _first(res.get("reaction"))
        reaction = _cc_text(_first(rx.get("manifestation")))
        sev = _s(rx.get("severity")) or {"high": "severe", "low": "mild"}.get(_s(res.get("criticality")), "")
        return "allergy", {"what": _cc_text(res.get("code")),
                           "reaction": "" if reaction == "not recorded" else reaction[:120],
                           "severity": sev if sev in ("mild", "moderate", "severe") else "",
                           "note": "\n".join(n for n in note if n)}
    if rt == "Observation":
        q = res.get("valueQuantity") if isinstance(res.get("valueQuantity"), dict) else {}
        value = _numfield(q.get("value")) or _s(res.get("valueString"), 40)
        rr = _first(res.get("referenceRange"))
        lo = rr.get("low") if isinstance(rr.get("low"), dict) else {}
        hi = rr.get("high") if isinstance(rr.get("high"), dict) else {}
        when = _d(res.get("effectiveDateTime")) or _d((res.get("effectivePeriod") or {}).get("start")
                                                     if isinstance(res.get("effectivePeriod"), dict) else "")
        if not when:
            return None, None
        return "result", {"test": _cc_text(res.get("code")), "date": when, "value": value,
                          "unit": _s(q.get("unit") or q.get("code"), 30), "range_low": _numfield(lo.get("value")),
                          "range_high": _numfield(hi.get("value")), "range_text": _s(rr.get("text"), 60),
                          "lab": _s(_first(res.get("performer")).get("display"), 80),
                          "note": "\n".join(n for n in note if n)}
    if rt == "Encounter":
        period = res.get("period") if isinstance(res.get("period"), dict) else {}
        part = _first(res.get("participant"))
        ind = part.get("individual") if isinstance(part.get("individual"), dict) else {}
        when = _d(period.get("start"))
        if not when:
            return None, None
        return "visit", {"date": when, "who": _s(ind.get("display"), 120),
                         "why": _cc_text(_first(res.get("reasonCode")))[:200], "said": _ext(res, "said"),
                         "follow_up": _ext(res, "follow_up")[:10]}
    if rt == "Immunization":
        if res.get("status") not in (None, "completed"):
            return None, None
        when = _d(res.get("occurrenceDateTime"))
        if not when:
            return None, None
        return "vaccination", {"vaccine": _cc_text(res.get("vaccineCode")), "date": when,
                               "lot": _s(res.get("lotNumber"), 40), "due_again": _ext(res, "due_again")[:10]}
    return None, None


def from_app(data: dict):
    people = {}
    for p in data.get("people") if isinstance(data.get("people"), list) else []:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("name"), str):
            people[p["id"]] = p["name"]
    recs, skipped = [], {}
    for r in data.get("records") if isinstance(data.get("records"), list) else []:
        kind = r.get("type") if isinstance(r, dict) else None
        if kind not in TYPES:
            skipped["other entries"] = skipped.get("other entries", 0) + 1
            continue
        keys = {f["key"] for f in TYPES[kind]["fields"]}
        recs.append((r.get("person") if isinstance(r.get("person"), str) else "", kind,
                     {k: v for k, v in r.items() if k in keys}))
    return people, recs, skipped


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_PERSON = {"person": _p("string", "The person's name, like Alex.")}
_TYPE = _p("string", "condition, medicine, allergy, result, visit, vaccination or question.")
_ADD_TYPE = _p("string", "condition, medicine, allergy, result, visit, vaccination or question; or person, with "
                          "just a name in person and no fields, to add a person to Health File.")
_FIELDS = {"type": "object", "description": (
    "The record's fields. condition: name, status (active or resolved), since, until, note. "
    "medicine: name, dose, how_often, since, until, status (taking or stopped), reason (what it is for), "
    "prescriber, note. allergy: what, reaction, severity (mild, moderate or severe), note. "
    "result: test, date, value, unit, range_low, range_high, range_text (the range exactly as printed on the "
    "result), lab, note. visit: date, who, why, said, follow_up. vaccination: vaccine, date, lot, due_again. "
    "question: text, asked (yes or no), answer. Dates look like 2027-03-31.")}
_RECORD = {"record": _p("string", "Which record, in plain words: its medicine, condition, allergy, test, vaccine or "
                                     "question, like ibuprofen or flu shot. An id works too but is not needed."),
           "person": _p("string", "Optional: whose record, like Alex.")}
TOOLS = [
    ("person_summary", "Use only when the owner wants the whole picture of one person on one page, not one kind of "
     "record: allergies first, then active conditions, current medicines, latest results, coming follow-ups and "
     "vaccinations, and open questions.",
     dict(_PERSON), ["person"]),
    ("list_records", "Use to see one kind of record for a person, such as their medicines, allergies or visits; for "
     "test results, give a test name to see how it changed over time. With one test's name it gives every result of "
     "that test, oldest first, each flagged only from its printed range; with words that fit several tests or a lab, "
     "every result they match; without, every result. It finds the person and the test by their plain names, so do "
     "not look anything up first.",
     {**_PERSON, "type": _TYPE, "name": _p("string", "Optional: only records with these words. For results, the "
                                                     "test's plain name, like cholesterol, or the lab's.")}, ["person"]),
    ("add_record", "Add a health record for one person: a condition, medicine, allergy, test result, visit, "
     "vaccination or question, with its fields. A person not in Health File yet is added by that name, and the "
     "answer says so. To add only a person, use type person with just their name.",
     {**_PERSON, "type": _ADD_TYPE, "fields": _FIELDS}, ["person", "type"]),
    ("change_record", "Change a health record named in plain words, like Alex's ibuprofen; it finds the record itself, "
     "so do not look it up first. For example mark a medicine stopped, a condition resolved or a question asked with "
     "its answer.",
     {**_RECORD, "fields": _FIELDS}, ["record", "fields"]),
    ("add_question", "Write down a question one person wants to ask at their next doctor's visit. A person not "
     "in Health File yet is added by that name, and the answer says so.",
     {**_PERSON, "text": _p("string", "The question, in the person's own words.")}, ["person", "text"]),
    ("prepare_for_visit", "Get one person ready for their next doctor's visit: open questions, what changed in the "
     "file since the last visit, current medicines and allergies.", dict(_PERSON), ["person"]),
    ("remove_record", "Delete a health record named in plain words, like Jo's flu shot; it finds the record itself, so "
     "do not look it up first. This can not be undone.",
     dict(_RECORD), ["record"]),
]
CHANGES = ["add_record", "change_record", "add_question", "remove_record"]
DELETES = ["remove_record"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: HealthFile, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params")
    if not isinstance(mid, (str, int)) or isinstance(mid, bool):
        mid = None
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
            out = getattr(app, name)(**args)
            text = out["text"] if isinstance(out, dict) else out
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Health File does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/record/add", "/api/record/change", "/api/record/remove", "/api/person/add",
               "/api/person/remove", "/api/import")


def meta() -> dict:
    return {"types": [{"key": k, "label": v["label"], "plural": v["plural"],
                       "fields": [{x: f[x] for x in ("key", "label", "kind", "choices", "required")}
                                  for f in v["fields"]]} for k, v in TYPES.items()],
            "disclaimer": DISCLAIMER}


class Handler(BaseHTTPRequestHandler):
    server_version = "HealthFile/1"
    app: HealthFile
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
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"), extra=extra)

    def _cap(self) -> int:
        return MAX_IMPORT if self._path() == "/api/import" else MAX_BODY

    def _length(self) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most the route's cap."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12 or int(raw) > self._cap():
            self.close_connection = True
            raise ValueError(f"The request body is too big: Health File takes at most {self._cap() // 1024} KB here.")
        return int(raw)

    def _drain(self, most: int, wait_s: float) -> None:
        """Read and drop a body that will be refused (at most *most* bytes within *wait_s* seconds), so the sender
        gets the answer, not a reset connection; then close the connection."""
        self.close_connection = True
        raw = (self.headers.get("Content-Length") or "0").strip()
        left = min(int(raw), most) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else 0
        end = time.monotonic() + wait_s
        try:
            while left > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.05, end - time.monotonic()))
                chunk = self.rfile.read1(min(left, 256 * 1024))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _read_body(self, n: int) -> bytes:
        """Exactly *n* bytes of body, all within BODY_DEADLINE_S; a short or slow body is a plain ValueError."""
        end = time.monotonic() + BODY_DEADLINE_S
        buf = bytearray()
        try:
            while len(buf) < n:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(left)
                chunk = self.rfile.read1(n - len(buf))
                if not chunk:
                    break
                buf += chunk
        except OSError:                              # a timeout too: the sender stopped
            pass
        try:
            self.connection.settimeout(self.timeout)
        except OSError:
            pass
        if len(buf) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        return bytes(buf)

    def _body(self):
        try:
            n = self._length()
        except ValueError:
            self._drain(MAX_DRAIN, DRAIN_WAIT_S)          # read and drop it first, so the refusal arrives
            raise
        raw = self._read_body(n) if n else b""
        try:
            return _loads(raw.decode("utf-8")) if raw else {}
        except UnicodeDecodeError:
            raise ValueError("Send UTF-8 JSON.") from None

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _query(self) -> dict:
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "", max_num_fields=10)
        return {k: v[0] for k, v in q.items()}

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        self._drain(REFUSED_DRAIN, REFUSED_WAIT_S)     # at most 64 KB within 2 seconds, then the answer
        self._json(401, {"error": "Health File needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Health File stopped on this request with an error "
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
            q = self._query()
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/meta":
                return self._json(200, meta())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
            if path == "/people":
                v = self.app.view("")
                return self._json(200, {"people": v["people"], "source": v["source"]})
            if path == "/view":
                return self._json(200, self.app.view(q.get("person")))
            if path == "/history":
                shared = self.app.shared()
                doc = self.app.store.load()
                p = self.app.find_person(doc, q.get("person"), shared)
                return self._json(200, {"test": q.get("test", ""),
                                        "points": self.app.history(doc, p, _text(q.get("test"), "test", 120))})
            if path == "/export":
                fmt = q.get("format", "app")
                out, skipped = self.app.export_counted(q.get("person"), fmt)
                fname = f"health-file-{'fhir-' if fmt == 'fhir' else ''}{date.today().isoformat()}.json"
                return self._json(200, out, {"Content-Disposition": f'attachment; filename="{fname}"',
                                             "X-Health-File-Skipped": str(skipped)})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "Health File has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            return self._json(404, {"error": "Health File has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path == "/api/record/add":
                out = app.page_add_record(body.get("person"), body.get("type"), body.get("fields"))
            elif path == "/api/record/change":
                out = {"text": app.change_record(body.get("id"), fields=body.get("fields"))}
            elif path == "/api/record/remove":
                out = {"text": app.remove_record(body.get("id"))}
            elif path == "/api/person/add":
                out = app.add_person(body.get("name"))
            elif path == "/api/person/remove":
                out = {"text": app.remove_person(body.get("id"))}
            else:
                out = app.import_file(body.get("data"), body.get("person"))
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
        except ValueError as exc:
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": f"Not JSON this server takes: {exc}"}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, today=date.today, api: str = "",
                shared=None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = HealthFile(Store(data), today, api, token or "", shared)
    handler = type("HealthHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Health File for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Health File with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), api=os.environ.get("PUFFPAI_API", ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
