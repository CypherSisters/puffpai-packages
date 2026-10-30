# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/emergency_prep.py (rewritten-from)
"""Prep Supplies: an emergency supplies tracker, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/prep-supplies/
- ``/health``       "ok"
- ``/data``         the supplies document (GET), ``/updated`` its stamp
- ``/summary``      how many days the water and food last (GET)
- ``/api/add``, ``/api/update``, ``/api/remove``, ``/api/people``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/prep-supplies/, which adds it.

The supplies are one JSON document in PUFFPAI_DATA (``prep.json``:
``{supplies: [...], people, last_updated}``). Writes replace the whole file at
once (temp file, then rename). A file that exists but will not read is never
written over. Weather alerts are not here; the ``weather`` package does that.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import secrets
import tempfile
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "prep-supplies"
VERSION = "1.0.0"
DOC = "prep.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_SUPPLIES = 2000
SOON_DAYS = 30
DEFAULT_PEOPLE = 4                      # HomeBot's default family size

CATEGORIES = ("food", "water", "medicine", "first_aid", "batteries", "fuel", "light", "sanitation", "tools", "other")
CATEGORY_WORDS = {"first aid": "first_aid", "first-aid": "first_aid", "firstaid": "first_aid", "medical": "first_aid",
                  "meds": "medicine", "medication": "medicine", "medications": "medicine", "medicines": "medicine",
                  "battery": "batteries", "lighting": "light", "lights": "light", "tool": "tools",
                  "drinking water": "water"}
DEFAULT_UNIT = {"water": "gallons", "food": "servings"}

# How much one person uses a day, by unit, when an item gives no figure of its own.
WATER_PER_DAY = {"gallon": 1.0, "gallons": 1.0, "gal": 1.0, "liter": 3.8, "liters": 3.8, "litre": 3.8,
                 "litres": 3.8, "l": 3.8, "quart": 4.0, "quarts": 4.0, "qt": 4.0}
FOOD_PER_DAY = {"serving": 3.0, "servings": 3.0, "meal": 3.0, "meals": 3.0, "day": 1.0, "days": 1.0,
                "ration day": 1.0, "ration days": 1.0, "calorie": 2000.0, "calories": 2000.0, "kcal": 2000.0}

# A basic 72-hour kit (the usual public checklist), as (label, categories, words in the name).
KIT_72H = [
    ("a first aid kit", ("first_aid",), ("first aid", "bandage")),
    ("the medicines people take every day", ("medicine",), ()),
    ("a flashlight or lantern", ("light",), ("flashlight", "torch", "lantern", "headlamp")),
    ("a battery or hand-crank radio", (), ("radio",)),
    ("spare batteries", ("batteries",), ("battery", "batteries")),
    ("a whistle to signal for help", (), ("whistle",)),
    ("dust masks", (), ("mask",)),
    ("plastic sheeting and duct tape", (), ("duct tape", "sheeting", "tarp")),
    ("wet wipes, garbage bags and ties", ("sanitation",), ("wipe", "towelette", "garbage bag", "trash bag", "toilet")),
    ("a wrench or pliers to turn off utilities", ("tools",), ("wrench", "pliers", "multitool", "multi-tool")),
    ("a manual can opener", (), ("can opener",)),
    ("a phone charger or power bank", (), ("charger", "power bank")),
]


def _now() -> str:
    return datetime.now().isoformat()


def _g(x: float) -> str:
    """A number for reading: 12, 3.5, 1,200."""
    return f"{int(x):,}" if x == int(x) else f"{x:,.1f}"


def _per(n: int) -> str:
    return "person" if n == 1 else "people"


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


def _category(value) -> str:
    text = _text(value, "category", 30).lower().replace("_", " ")
    cid = CATEGORY_WORDS.get(text, text.replace(" ", "_"))
    if cid not in CATEGORIES:
        raise ValueError(f"category is one of {', '.join(c.replace('_', ' ') for c in CATEGORIES)}")
    return cid


def _date(value) -> str:
    """A date as YYYY-MM-DD, or "" for none."""
    text = _text(value, "expires_on", 40)
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValueError("expires_on is a date like 2027-03-31") from None


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


def _num(value) -> float:
    """A stored number for counting: 0 for anything that is not a finite, non-negative number."""
    if isinstance(value, bool):
        return 0.0
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return x if math.isfinite(x) and x >= 0 else 0.0


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"supplies": [], "people": DEFAULT_PEOPLE, "last_updated": ""}


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
                raise Unreadable(f"{self.path} will not read ({exc}). Prep Supplies will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("supplies", []), list):
                raise Unreadable(f"{self.path} is not a Prep Supplies file. Prep Supplies will not write over it.")
            out = default_doc()
            out.update(data)
            out["supplies"] = [s for s in out["supplies"] if isinstance(s, dict)]
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".prep-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

def _label(s: dict) -> str:
    unit = str(s.get("unit") or "").strip()[:20]
    return f"{_g(_num(s.get('quantity')))} {unit + ' of ' if unit else ''}{str(s.get('name') or '')[:80]}"


def _days_left(s: dict, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(s.get("expires_on") or "")) - today).days
    except ValueError:
        return None


def _cat(cid) -> str:
    return str(cid or "other").replace("_", " ")


class Prep:
    """From EmergencyPrep (emergency_prep.py) and the prep_* tools (rebot_tools.py:2811-3069)."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # helpers
    def _find(self, doc: dict, supply) -> dict:
        key = _text(supply, "supply", 120)
        if not key:
            raise ValueError("say which supply, by its name or id")
        for s in doc["supplies"]:
            if s.get("id") == key:
                return s
        named = [s for s in doc["supplies"] if str(s.get("name", "")).lower() == key.lower()]
        if not named:
            raise ValueError(f"there is no supply called {key!r}")
        if len(named) > 1:
            ids = "; ".join(f"{s.get('id')} ({_label(s)}, expires {s.get('expires_on') or 'never'})" for s in named)
            raise ValueError(f"more than one supply is called {key!r}; say which by its id: {ids}")
        return named[0]

    def _people(self, doc: dict, people) -> int:
        if people is None or people == "":
            return max(1, min(100, int(_num(doc.get("people")) or DEFAULT_PEOPLE)))
        return _number(people, "people", 1, 100, whole=True)

    # tools
    def add_supply(self, name, category, quantity, unit=None, expires_on=None, per_person_per_day=None) -> dict:
        name = _text(name, "name", 80)
        if not name:
            raise ValueError("a supply needs a name")
        cid = _category(category)
        qty = _number(quantity, "quantity", 0, 1_000_000)
        unit = _text(unit, "unit", 20, DEFAULT_UNIT.get(cid, "items"))
        exp = _date(expires_on)
        ppd = None if per_person_per_day in (None, "", 0) else \
            _number(per_person_per_day, "per_person_per_day", 0.001, 100_000)
        with self.store.lock:
            doc = self.store.load()
            same = (name.lower(), cid, unit.lower(), exp)
            for s in doc["supplies"]:
                if (str(s.get("name", "")).lower(), s.get("category"), str(s.get("unit", "")).lower(),
                        s.get("expires_on") or "") == same:
                    s["quantity"] = _num(s.get("quantity")) + qty
                    if ppd is not None:
                        s["per_person_per_day"] = ppd
                    self.store.save(doc)
                    return {"id": s.get("id"), "text": f"Added {_g(qty)} {unit} to {name}; there are now {_label(s)}."}
            if len(doc["supplies"]) >= MAX_SUPPLIES:
                raise ValueError(f"Prep Supplies holds at most {MAX_SUPPLIES} supplies")
            s = {"id": "s" + secrets.token_hex(4), "name": name, "category": cid, "quantity": qty, "unit": unit,
                 "expires_on": exp, "added": _now()}
            if ppd is not None:
                s["per_person_per_day"] = ppd
            doc["supplies"].append(s)
            self.store.save(doc)
        return {"id": s["id"], "text": f"Added {_label(s)} to {_cat(cid)}{f', expires {exp}' if exp else ''}."}

    def update_supply(self, supply, name=None, category=None, quantity=None, unit=None, expires_on=None,
                      per_person_per_day=None) -> str:
        with self.store.lock:
            doc = self.store.load()
            s = self._find(doc, supply)
            changed = []
            if name is not None:
                s["name"] = _text(name, "name", 80) or s.get("name", "")
                changed.append("name")
            if category is not None:
                s["category"] = _category(category)
                changed.append("category")
            if quantity is not None:
                s["quantity"] = _number(quantity, "quantity", 0, 1_000_000)
                changed.append("quantity")
            if unit is not None:
                s["unit"] = _text(unit, "unit", 20) or s.get("unit", "items")
                changed.append("unit")
            if expires_on is not None:
                s["expires_on"] = _date(expires_on)
                changed.append("expiry date")
            if per_person_per_day is not None:
                if per_person_per_day in ("", 0):
                    s.pop("per_person_per_day", None)
                else:
                    s["per_person_per_day"] = _number(per_person_per_day, "per_person_per_day", 0.001, 100_000)
                changed.append("daily use")
            if not changed:
                raise ValueError("say what to change: name, category, quantity, unit, expires_on or per_person_per_day")
            self.store.save(doc)
        exp = f", expires {s['expires_on']}" if s.get("expires_on") else ""
        return f"Updated the {', '.join(changed)}: now {_label(s)} in {_cat(s.get('category'))}{exp}."

    def remove_supply(self, supply) -> str:
        with self.store.lock:
            doc = self.store.load()
            s = self._find(doc, supply)
            doc["supplies"] = [x for x in doc["supplies"] if x is not s]
            self.store.save(doc)
        return f"Removed {_label(s)}."

    def list_supplies(self, category=None, expiring=False) -> str:
        items = self.store.load()["supplies"]
        today = date.today()
        what = "supplies"
        if category not in (None, "", "all"):
            cid = _category(category)
            items = [s for s in items if s.get("category") == cid]
            what = f"{_cat(cid)} supplies"
        if expiring is True or str(expiring).lower() in ("true", "yes", "1"):
            items = [s for s in items if (d := _days_left(s, today)) is not None and d <= SOON_DAYS]
            what += f" expired or expiring in the next {SOON_DAYS} days"
        if not items:
            return f"No {what}."
        order = {c: i for i, c in enumerate(CATEGORIES)}
        items = sorted(items, key=lambda s: (order.get(s.get("category"), 99), str(s.get("name", "")).lower()))
        lines = []
        for s in items:
            d = _days_left(s, today)
            note = "" if d is None else (f", EXPIRED {s['expires_on']}" if d < 0 else
                                         f", expires {s['expires_on']} (in {d} days)" if d <= SOON_DAYS else
                                         f", expires {s['expires_on']}")
            lines.append(f"- {_cat(s.get('category'))}: {_label(s)}{note}")
        return f"{len(items)} {what}:\n" + "\n".join(lines)

    def summary(self, people=None) -> dict:
        """Water and food days for *people*, from the per-person-per-day figures. Expired items do not count.
        Each item's days add up: 7 gallons and 7 liters of water for one person are 7 + 1.8 days."""
        doc = self.store.load()
        n = self._people(doc, people)
        today = date.today()
        days = {"water": 0.0, "food": 0.0}
        skipped, expired = [], []
        for s in doc["supplies"]:
            cid = s.get("category")
            if cid not in days:
                continue
            d = _days_left(s, today)
            if d is not None and d < 0:
                expired.append(_label(s))
                continue
            table = WATER_PER_DAY if cid == "water" else FOOD_PER_DAY
            ppd = _num(s.get("per_person_per_day")) or table.get(str(s.get("unit") or "").strip().lower(), 0.0)
            if not ppd:
                skipped.append(_label(s))
                continue
            days[cid] += _num(s.get("quantity")) / (ppd * n)
        return {"people": n, "water_days": round(days["water"], 1), "food_days": round(days["food"], 1),
                "not_counted": skipped, "expired": expired}

    def days_remaining(self, people=None) -> str:
        r = self.summary(people)
        w, f = r["water_days"], r["food_days"]
        short = "water" if w < f else "food" if f < w else ""
        text = (f"For {r['people']} {_per(r['people'])}: water lasts {_g(w)} days, food {_g(f)} days."
                + (f" The {short} runs out first." if short else ""))
        text += ("\nAssumptions: each person uses 1 gallon (about 3.8 liters) of water a day for drinking and "
                 "washing, and eats 3 servings (about 2,000 calories) a day, unless an item gives its own "
                 "per-person-per-day figure. Only the water and food categories count, and expired items do not.")
        if r["not_counted"]:
            text += ("\nNot counted, because their unit has no daily figure (give one with per_person_per_day): "
                     + "; ".join(r["not_counted"][:20]) + ".")
        if r["expired"]:
            text += "\nExpired, so not counted: " + "; ".join(r["expired"][:20]) + "."
        return text

    def checklist(self, people=None) -> str:
        r = self.summary(people)
        today = date.today()
        live = [s for s in self.store.load()["supplies"]
                if (_days_left(s, today) or 0) >= 0 and _num(s.get("quantity")) > 0]
        have, lacks = [], []
        if r["water_days"] >= 3:
            have.append("3 days of water")
        else:
            lacks.append(f"water for {_g(round(3 - r['water_days'], 1))} more days")
        if r["food_days"] >= 3:
            have.append("3 days of food")
        else:
            lacks.append(f"food for {_g(round(3 - r['food_days'], 1))} more days")
        for what, cats, words in KIT_72H:
            ok = any(s.get("category") in cats or any(w in str(s.get("name", "")).lower() for w in words)
                     for s in live)
            (have if ok else lacks).append(what)
        head = f"72-hour kit for {r['people']} {_per(r['people'])}"
        if not lacks:
            return f"{head}: everything on the basic list is stocked ({'; '.join(have)})."
        out = f"{head}. Still lacks: " + "; ".join(lacks) + "."
        if have:
            out += " Already stocked: " + "; ".join(have) + "."
        return out + " This is a basic list; add what your household also needs, like pet food, baby supplies or cash."

    def set_people(self, people) -> str:
        n = _number(people, "people", 1, 100, whole=True)
        with self.store.lock:
            doc = self.store.load()
            doc["people"] = n
            return self.store.save(doc)


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_FIELDS = {
    "name": _p("string", "What it is, like bottled water or canned beans."),
    "category": _p("string", "food, water, medicine, first aid, batteries, fuel, light, sanitation, tools or other."),
    "quantity": _p("number", "How many, in the unit."),
    "unit": _p("string", "Like gallons, liters, servings, cans, packs. Default gallons for water, servings for food."),
    "expires_on": _p("string", "Optional: the expiry date, like 2027-03-31."),
    "per_person_per_day": _p("number", "Optional: how much of this unit one person uses a day, for the days count."),
}
_WHO = {"people": _p("integer", "Optional: how many people. Default: the household size saved on the page.")}
TOOLS = [
    ("add_supply", "Add an emergency supply (food, water, medicine, batteries, fuel, first aid and so on) with "
     "its quantity, unit and expiry date.", dict(_FIELDS), ["name", "category", "quantity"]),
    ("update_supply", "Change an emergency supply: its quantity, unit, expiry date, name, category or daily use. "
     "Name the supply by its name or id; an empty expires_on clears the date.",
     {"supply": _p("string", "The supply's name or id."), **_FIELDS}, ["supply"]),
    ("remove_supply", "Take an emergency supply off the list, by its name or id.",
     {"supply": _p("string", "The supply's name or id.")}, ["supply"]),
    ("list_supplies", "List the emergency supplies: all, one category, or with expiring true only those expired "
     "or expiring in the next 30 days.",
     {"category": _p("string", "Optional: one category, like water or first aid."),
      "expiring": _p("boolean", "Optional: true for only expired or soon-expiring items.")}, []),
    ("days_remaining", "How many days the stored water and food would last the household, with the assumptions used.",
     dict(_WHO), []),
    ("checklist", "What a basic 72-hour emergency kit still lacks, compared with the supplies stocked.",
     dict(_WHO), []),
]
CHANGES = ["add_supply", "update_supply", "remove_supply"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Prep, msg: dict) -> dict | None:
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
        try:
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
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Prep Supplies does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

PAGE_FIELDS = ("name", "category", "quantity", "unit", "expires_on", "per_person_per_day")
PAGE_ROUTES = ("/api/add", "/api/update", "/api/remove", "/api/people")


class Handler(BaseHTTPRequestHandler):
    server_version = "PrepSupplies/1"
    app: Prep
    token: str
    timeout = 15        # seconds; a stalled sender is cut off

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
        self._json(401, {"error": "Prep Supplies needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Prep Supplies stopped on this request with an error "
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
            if path == "/summary":
                return self._json(200, self.app.summary())
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Prep Supplies has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            return self._json(404, {"error": "Prep Supplies has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            fields = {k: body[k] for k in PAGE_FIELDS if k in body}
            if path == "/api/add":
                out = self.app.add_supply(**fields)
            elif path == "/api/update":
                out = {"text": self.app.update_supply(body.get("id"), **fields)}
            elif path == "/api/remove":
                out = {"text": self.app.remove_supply(body.get("id"))}
            else:
                self.app.set_people(body.get("people"))
                out = {"text": "Saved."}
            out["last_updated"] = self.app.store.load()["last_updated"]
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


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Prep(Store(data))
    handler = type("PrepHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prep Supplies for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Prep Supplies with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
