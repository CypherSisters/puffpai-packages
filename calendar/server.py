# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/app_data.py (adapted)
"""Calendar: events, tasks, medicines and bills for the household, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``                the page (web/index.html), shown in the Desktop at /pkg/calendar/
- ``/health``          "ok"
- ``/widget/upcoming`` the "Coming up" widget, ``{"items": [{"text"}]}``
- ``/data``            the whole document (GET), ``/updated`` its stamp
- ``/month?y=&m=``     one month for the page: events (repeats worked out), tasks, bills, medicines by day
- ``/people``          who things can be for: the Core's shared people list (D12)
- ``/api/<tool>``      POST: the page's buttons, the same functions as the MCP tools
- ``/mcp``             the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). GET routes never
change anything (D55). Calendar never syncs with an outside calendar.

The data is one JSON document in PUFFPAI_DATA (``calendar.json``) with a ``last_updated`` stamp,
the same shape as v1's ``calendar.json`` (family, events, tasks, meds, bills), so a v1 file can be
copied in. Writes replace the whole file at once (temp file, then rename). A file that exists but
will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import calendar as _cal
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "calendar"
VERSION = "2.0.0"
DOC = "calendar.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 1024 * 1024

# Fixed lists from v1 (app_data.py:649, :653; calendar/page.tsx:106-121).
CATEGORIES = ("work", "medical", "sports", "school", "family", "bills", "appointment", "other")
PRIORITIES = ("low", "medium", "high", "urgent")
REPEATS = ("none", "daily", "weekdays", "weekly", "biweekly", "monthly", "yearly")
BILL_CATEGORIES = ("utilities", "insurance", "subscription", "housing", "other")
MED_FREQUENCIES = ("daily", "twice-daily", "weekly", "as-needed")
DONE = "✅ "                      # v1 put this in front of a done task's or paid bill's event title


def _uid() -> str:
    return secrets.token_hex(5)


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


YEARS = (1900, 2200)                 # dates outside these years are refused (and old ones ignored)
MAX_AMOUNT = 10_000_000


def _text(value, name: str, default: str = "") -> str:
    """A plain text argument, checked before anything is saved."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value.strip()[:300]


def _pick(value, allowed: tuple, default: str) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"use one of: {', '.join(allowed)}")
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def amount_of(value) -> float:
    """A bill amount: a finite number from 0 to 10,000,000, kept to two decimals. Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("the amount must be a number, like 84.50")
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip() or "0"
    try:
        n = float(value if value is not None else 0)
    except (TypeError, ValueError):
        raise ValueError("the amount must be a number, like 84.50") from None
    if not math.isfinite(n) or not 0 <= n <= MAX_AMOUNT:
        raise ValueError(f"the amount must be from 0 to {MAX_AMOUNT:,}")
    return round(n, 2)


def _in_range(when: datetime) -> datetime:
    if not YEARS[0] <= when.year <= YEARS[1]:
        raise ValueError(f"dates must be between {YEARS[0]} and {YEARS[1]}")
    return when


# ---------------------------------------------------------------- dates
# The brain often does not know today's date, so a tool takes plain words too:
# "2026-10-30", "2026-10-30T10:00", "today", "tomorrow", "friday", "next monday", "friday at 10am".

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# "october 30", "oct 30th", "30 october", "30th of oct 2027": groups month, day | day, month, then year
_MONTH_DAY = re.compile(r"\b(?:" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
                        + _MON + r")(?:\s+(\d{4}))?\b")
_TIME = re.compile(r"(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$)", re.I)


def parse_when(text, now: datetime | None = None) -> tuple[datetime, bool]:
    """``(when, has_time)`` from plain words or ISO, between the years 1900 and 2200.
    Raises ValueError with a plain reason."""
    if text is not None and not isinstance(text, str):
        raise ValueError("a date must be text, like 2026-10-30 or tomorrow")
    try:
        when, timed = _parse_when(text, now)
    except OverflowError:
        raise ValueError(f"dates must be between {YEARS[0]} and {YEARS[1]}") from None
    return _in_range(when), timed


def _parse_when(text, now: datetime | None = None) -> tuple[datetime, bool]:
    now = now or datetime.now()
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("a date is needed, like 2026-10-30 or tomorrow")
    iso = raw.replace("Z", "")
    try:
        when = datetime.fromisoformat(iso.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", iso) else iso)
        return when.replace(tzinfo=None), bool(re.search(r"[T ]\d", iso))
    except ValueError:
        pass
    words = raw.lower().replace(",", " ")
    day = None
    if re.search(r"\btoday\b|\btonight\b", words):
        day = now.date()
    elif re.search(r"\btomorrow\b", words):
        day = now.date() + timedelta(days=1)
    else:
        m = re.search(r"\b(next\s+)?(" + "|".join(_DAYS) + r")\b", words)
        if m:
            ahead = (_DAYS.index(m.group(2)) - now.weekday()) % 7
            if m.group(1) and ahead == 0:
                ahead = 7
            day = now.date() + timedelta(days=ahead)
        else:
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", words)
            n = _MONTH_DAY.search(words)
            if m:
                day = date.fromisoformat(m.group(1))
            elif n:
                month = _MONTHS.index((n.group(1) or n.group(4))[:3]) + 1
                num, year = int(n.group(2) or n.group(3)), int(n.group(5) or now.year)
                day = date(year, month, num)
                if not n.group(5) and day < now.date():
                    day = date(year + 1, month, num)              # "march 3" said in November is next March
    rest = re.sub(r"\b(today|tonight|tomorrow|next|" + "|".join(_DAYS) + r"|\d{4}-\d{2}-\d{2})\b", " ", words)
    rest = _MONTH_DAY.sub(" ", rest)
    t = _TIME.search(rest)
    if day is None and t is None:
        raise ValueError(f"'{raw}' is not a date I can read; use a date like 2026-10-30 or a word like tomorrow")
    day = day or now.date()
    if t is None:
        if "tonight" in words:
            return datetime.combine(day, datetime.min.time()).replace(hour=19), True
        return datetime.combine(day, datetime.min.time()), False
    hour, minute, ampm = int(t.group(1)), int(t.group(2) or 0), (t.group(3) or "").replace(".", "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not ampm and "tonight" in words and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        raise ValueError(f"'{raw}' has a time I can't read")
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute), True


def _stamp(when: datetime, has_time: bool) -> str:
    return when.isoformat(timespec="minutes") if has_time else when.date().isoformat()


def _read(stamp) -> datetime | None:
    """A stored date, or None when it will not read or is outside 1900 to 2200 (an old or hand-edited file)."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
    return when if YEARS[0] <= when.year <= YEARS[1] else None


def _has_time(stamp) -> bool:
    return bool(re.search(r"T\d", str(stamp or "")))


def say_when(stamp) -> str:
    """Friday Oct 30, 10:00 AM (or just Friday Oct 30)."""
    when = _read(stamp)
    if when is None:
        return str(stamp or "")
    day = f"{when:%A %b} {when.day}"
    return f"{day}, {when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}" if _has_time(stamp) else day


# ---------------------------------------------------------------- repeats
# From dashboard/app/_lib/recurrence.ts (expandEvent): daily, weekdays, weekly, biweekly, monthly,
# yearly, or the object form {freq, interval, byday, count, until}. Months keep the day, or the
# month's last day when it is shorter (v1 ran over into the next month).

_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _rule(rec):
    if not rec:
        return None
    if isinstance(rec, dict):
        return rec if rec.get("freq") in ("daily", "weekly", "monthly", "yearly") else None
    return {"daily": {"freq": "daily"}, "weekdays": {"freq": "weekly", "byday": _CODES[:5]},
            "weekly": {"freq": "weekly"}, "biweekly": {"freq": "weekly", "interval": 2},
            "monthly": {"freq": "monthly"}, "yearly": {"freq": "yearly"}}.get(str(rec).strip().lower())


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, _cal.monthrange(y, m)[1]))


def occurrences(event: dict, start: datetime, end: datetime, most: int = 400) -> list[datetime]:
    """Every start of *event* in [start, end]."""
    first = _read(event.get("start"))
    if first is None:
        return []
    rule = _rule(event.get("rec"))
    if rule is None:
        return [first] if start <= first <= end else []
    every = max(1, int(rule.get("interval") or 1))
    until = _read(rule.get("until")) if rule.get("until") else None
    count = int(rule.get("count") or 0)
    byday = [c for c in rule.get("byday") or [] if c in _CODES]
    out, n, k, cursor = [], 0, 0, first
    while cursor <= end and len(out) < most and k < 20000:
        k += 1
        if until and cursor > until:
            break
        if not byday or _CODES[cursor.weekday()] in byday:
            n += 1
            if count and n > count:
                break
            if cursor >= start:
                out.append(cursor)
        if byday:
            cursor += timedelta(days=1)
        elif rule["freq"] == "daily":
            cursor += timedelta(days=every)
        elif rule["freq"] == "weekly":
            cursor += timedelta(days=7 * every)
        elif rule["freq"] == "monthly":
            cursor = _add_months(first, every * n) if n else _add_months(cursor, every)
        else:
            cursor = _add_months(first, 12 * every * n) if n else _add_months(cursor, 12 * every)
    return out


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"family": [], "events": [], "tasks": [], "meds": [], "bills": [], "last_updated": ""}


class Store:
    """One JSON document with a stamp (the v1 pattern, app_data.py:348-370)."""

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
                raise Unreadable(f"{self.path} will not read ({exc}). Calendar will not write over it.") from None
            if not isinstance(data, dict) or not all(isinstance(data.get(k, []), list)
                                                     for k in ("events", "tasks", "meds", "bills")):
                raise Unreadable(f"{self.path} is not a Calendar document. Calendar will not write over it.")
            out = default_doc()
            out.update(data)
            for k in ("events", "tasks", "meds", "bills"):
                out[k] = [r for r in out[k] if isinstance(r, dict)]
            for b in out["bills"]:                                     # repair amounts saved before the check
                try:
                    b["amt"] = amount_of(b.get("amt"))
                except ValueError:
                    b["amt"] = 0.0
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            body = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)   # fails before the file is touched
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".calendar-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ---------------------------------------------------------------- people

class People:
    """Who things can be for: the Core's shared people list (``GET /v1/people``, D12), read with this
    package's token. Calendar keeps no list of its own; a v1 file's ``family`` is read as a fallback."""

    def __init__(self, api: str | None, token: str | None, ttl_s: float = 30) -> None:
        self.api, self.token, self.ttl_s = (api or "").rstrip("/"), token or "", ttl_s
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        self._cache: tuple[float, list[dict]] = (0.0, [])

    def list(self, fallback: list | None = None) -> list[dict]:
        at, got = self._cache
        if self.api and self.token and time.monotonic() - at > self.ttl_s:
            req = urllib.request.Request(self.api + "/v1/people", headers={"Authorization": f"Bearer {self.token}"})
            try:
                with self._open(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                got = [{"id": str(p.get("id")), "name": str(p.get("name") or p.get("id")), "emoji": str(p.get("emoji") or "")}
                       for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []
            except (urllib.error.URLError, OSError, ValueError):
                got = []
            self._cache = (time.monotonic(), got)
        if got:
            return got
        return [{"id": str(p.get("id")), "name": str(p.get("name") or p.get("id")), "emoji": str(p.get("emoji") or "")}
                for p in fallback or [] if isinstance(p, dict) and p.get("id")]


# ------------------------------------------------------------- the logic
# From app_data.py:621-1035 (cal_*).

def _find(rows: list[dict], ident: str, key: str, what: str) -> tuple[dict | None, str | None]:
    """An exact id or name, then one unambiguous part of a name (as AlarmFlow's lookup).
    v1 took the first partial match for edits and deleted EVERY partial match; this asks instead."""
    text = _text(ident, what)
    low = text.lower()
    if len(low) < 2:
        return None, f"I need the {what}'s name to find it."
    for r in rows:
        name = str(r.get(key, "")).lower().removeprefix(DONE)
        if r.get("id") == text or name == low:
            return r, None
    hits = [r for r in rows if low in str(r.get(key, "")).lower()]
    if len(hits) == 1:
        return hits[0], None
    if hits:
        return None, f"Several {what}s match '{text}': {', '.join(str(r.get(key)) for r in hits[:4])}. Which one?"
    return None, f"No {what} matches '{text}'."


class Calendar:
    def __init__(self, store: Store, people: People | None = None) -> None:
        self.store = store
        self.people = people or People(None, None)

    def _who(self, who) -> str:
        """A person's id from the people list when the name matches, else the words as given; "all" for everyone."""
        text = _text(who, "who")
        if not text or text.lower() in ("all", "everyone", "everybody", "the family", "family", "us"):
            return "all"
        for p in self.people.list(self.store.load().get("family")):
            if text.lower() in (p["id"].lower(), p["name"].lower()):
                return p["id"]
        return text

    def _name(self, who, people: list[dict] | None = None) -> str:
        if not who or who == "all":
            return "everyone"
        for p in people if people is not None else self.people.list(self.store.load().get("family")):
            if p["id"] == who:
                return p["name"]
        return str(who)

    # add
    def add_event(self, title: str, start: str, end: str = "", category: str = "other", who: str = "all",
                  priority: str = "medium", location: str = "", repeat: str = "none") -> str:
        title = _text(title, "title")
        end, location = _text(end, "end"), _text(location, "location")
        if not title:
            return "An event needs a title."
        when, timed = parse_when(start)
        if end:
            stop, stop_timed = parse_when(end, when)
            if stop < when:
                return "The end is before the start."
        else:
            stop, stop_timed = (when + timedelta(hours=1), True) if timed else (when, False)
        rec = _pick(repeat, REPEATS, "none")
        event = {"id": f"e{_uid()}", "title": title, "cat": _pick(category, CATEGORIES, "other"),
                 "start": _stamp(when, timed), "end": _stamp(stop, stop_timed), "to": self._who(who),
                 "prio": _pick(priority, PRIORITIES, "medium"), "src": "puffpai",
                 "rec": None if rec == "none" else rec, "loc": str(location or "").strip() or None}
        with self.store.lock:
            data = self.store.load()
            data["events"].append(event)
            self.store.save(data)
        again = "" if rec == "none" else f", repeating {rec}"
        whom = "" if event["to"] == "all" else f", for {self._name(event['to'])}"
        return f"Added {title} on {say_when(event['start'])}{whom}{again}."

    def add_task(self, title: str, due: str, category: str = "other", who: str = "all", priority: str = "medium") -> str:
        title = _text(title, "title")
        if not title:
            return "A task needs a title."
        when, timed = parse_when(due)
        task_id = f"t{_uid()}"
        task = {"id": task_id, "title": title, "cat": _pick(category, CATEGORIES, "other"), "due": _stamp(when, timed),
                "to": self._who(who), "prio": _pick(priority, PRIORITIES, "medium"), "status": "pending", "src": "puffpai"}
        at = when if timed else when.replace(hour=9, minute=0)      # v1: a date-only task shows at 9:00
        event = {"id": f"te{_uid()}", "title": title, "cat": task["cat"], "start": _stamp(at, True),
                 "end": _stamp(at + timedelta(hours=1), True), "to": task["to"], "prio": task["prio"],
                 "src": "task", "taskId": task_id, "rec": None, "loc": None}
        with self.store.lock:
            data = self.store.load()
            data["tasks"].append(task)
            data["events"].append(event)
            self.store.save(data)
        whom = "" if task["to"] == "all" else f", for {self._name(task['to'])}"
        return f"Task added: {title}, due {say_when(task['due'])}{whom}."

    def add_med(self, name: str, dose: str = "", who: str = "all", how_often: str = "daily", next_time: str = "8:00 AM") -> str:
        name, dose, next_time = _text(name, "name"), _text(dose, "dose"), _text(next_time, "next_time")
        if not name:
            return "A medicine needs a name."
        med = {"id": f"m{_uid()}", "name": name, "dose": dose, "to": self._who(who),
               "freq": _pick(how_often, MED_FREQUENCIES, "daily"), "next": next_time or "8:00 AM",
               "status": "due", "refill": (datetime.now() + timedelta(days=30)).date().isoformat()}
        with self.store.lock:
            data = self.store.load()
            data["meds"].append(med)
            self.store.save(data)
        dose_words = f" ({med['dose']})" if med["dose"] else ""
        return f"Medicine added: {name}{dose_words}, {med['freq']} at {med['next']}, for {self._name(med['to'])}."

    def add_bill(self, name: str, amount: float = 0, due: str = "", category: str = "utilities", autopay: bool = False) -> str:
        name = _text(name, "name")
        if not name:
            return "A bill needs a name."
        amount = amount_of(amount)
        when, _timed = parse_when(due)
        bill_id = f"b{_uid()}"
        bill = {"id": bill_id, "name": name, "amt": amount, "due": when.date().isoformat(),
                "cat": _pick(category, BILL_CATEGORIES, "other"), "status": "pending", "auto": bool(autopay)}
        at = when.replace(hour=9, minute=0)
        event = {"id": f"be{_uid()}", "title": f"{name} (${amount:.2f})", "cat": "bills", "start": _stamp(at, True),
                 "end": _stamp(at + timedelta(hours=1), True), "to": "all", "prio": "medium", "src": "bill",
                 "billId": bill_id, "rec": None, "loc": None}
        with self.store.lock:
            data = self.store.load()
            data["bills"].append(bill)
            data["events"].append(event)
            self.store.save(data)
        return f"Bill tracked: {name}, ${amount:.2f}, due {say_when(bill['due'])}."

    # list
    def _between(self, data: dict, start: datetime, end: datetime) -> list[tuple[datetime, dict]]:
        got = [(at, e) for e in data["events"] for at in occurrences(e, start, end)]
        return sorted(got, key=lambda x: x[0])

    def list_events(self, days_ahead: int = 7, day: str = "") -> str:
        data = self.store.load()
        today = datetime.combine(datetime.now().date(), datetime.min.time())
        if day:
            first, _t = parse_when(day)
            start = datetime.combine(first.date(), datetime.min.time())
            end, label = start + timedelta(days=1) - timedelta(seconds=1), f"on {say_when(start.date().isoformat())}"
        else:
            if isinstance(days_ahead, bool) or not isinstance(days_ahead, (int, float, str, type(None))):
                raise ValueError("days_ahead must be a whole number")
            try:
                days_ahead = max(0, min(int(float(days_ahead if days_ahead is not None else 7)), 366))
            except (TypeError, ValueError, OverflowError):
                raise ValueError("days_ahead must be a whole number") from None
            start, end = today, today + timedelta(days=days_ahead + 1) - timedelta(seconds=1)
            label = "today" if days_ahead == 0 else f"in the next {_plural(days_ahead, 'day')}"
        found = self._between(data, start, end)
        if not found:
            return f"Nothing is on the calendar {label}."
        people = self.people.list(data.get("family"))
        lines = []
        for at, e in found:
            stamp = _stamp(at, _has_time(e.get("start")))
            whom = "" if e.get("to") in (None, "", "all") else f", {self._name(e['to'], people)}"
            where = f" at {e['loc']}" if e.get("loc") else ""
            done = " (done)" if e.get("done") or str(e.get("title", "")).startswith(DONE) else ""
            lines.append(f"{say_when(stamp)}: {str(e['title']).removeprefix(DONE)}{where} ({e.get('cat', 'other')}{whom}){done}")
        return f"{_plural(len(found), 'event')} {label}:\n" + "\n".join(lines)

    def list_tasks(self, status: str = "pending") -> str:
        status = "completed" if str(status or "").lower() in ("completed", "done", "finished") else \
                 "all" if str(status or "").lower() == "all" else "pending"
        data = self.store.load()
        tasks = [t for t in data["tasks"] if status == "all" or t.get("status") == status]
        word = {"pending": "open", "completed": "done", "all": ""}[status]
        if not tasks:
            return f"No {word + ' ' if word else ''}tasks."
        people = self.people.list(data.get("family"))
        tasks.sort(key=lambda t: str(t.get("due", "")))
        lines = []
        for t in tasks:
            prio = f" [{t['prio']}]" if t.get("prio") in ("urgent", "high") else ""
            whom = "" if t.get("to") in (None, "", "all") else f", for {self._name(t['to'], people)}"
            done = " (done)" if status == "all" and t.get("status") == "completed" else ""
            lines.append(f"{t['title']}, due {say_when(t.get('due'))}{whom}{prio}{done}")
        return f"{_plural(len(tasks), (word + ' ' if word else '') + 'task')}:\n" + "\n".join(lines)

    @staticmethod
    def _med_taken(m: dict) -> bool:
        """Taken today. v1 kept "taken" forever after one tap; here it counts for the day it was taken."""
        return m.get("status") == "taken" and m.get("takenOn", date.today().isoformat()) == date.today().isoformat()

    def list_meds(self) -> str:
        data = self.store.load()
        meds = data["meds"]
        if not meds:
            return "No medicines are tracked."
        people = self.people.list(data.get("family"))
        lines = [f"{m['name']}{' (' + m['dose'] + ')' if m.get('dose') else ''}: {m.get('freq', 'daily')} at {m.get('next', '')}, "
                 f"for {self._name(m.get('to'), people)}, {'taken today' if self._med_taken(m) else 'due'}" for m in meds]
        return f"{_plural(len(meds), 'medicine')}:\n" + "\n".join(lines)

    def list_bills(self, status: str = "pending") -> str:
        status = "paid" if str(status or "").lower() == "paid" else "all" if str(status or "").lower() == "all" else "pending"
        bills = [b for b in self.store.load()["bills"] if status == "all" or b.get("status") == status]
        word = {"pending": "unpaid", "paid": "paid", "all": ""}[status]
        if not bills:
            return f"No {word + ' ' if word else ''}bills."
        bills.sort(key=lambda b: str(b.get("due", "")))
        total = sum(float(b.get("amt") or 0) for b in bills)
        lines = [f"{b['name']}: ${float(b.get('amt') or 0):.2f}, due {say_when(b.get('due'))}"
                 + (" (autopay)" if b.get("auto") else "") + (" (paid)" if status == "all" and b.get("status") == "paid" else "")
                 for b in bills]
        return f"{_plural(len(bills), (word + ' ' if word else '') + 'bill')}, ${total:.2f} in all:\n" + "\n".join(lines)

    # change
    def edit_event(self, event: str, title: str = "", start: str = "", end: str = "", category: str = "",
                   who: str = "", priority: str = "", location: str = "", repeat: str = "") -> str:
        title, start, end = _text(title, "title"), _text(start, "start"), _text(end, "end")
        location, category, priority, repeat = (_text(location, "location"), _text(category, "category"),
                                                _text(priority, "priority"), _text(repeat, "repeat"))
        with self.store.lock:
            data = self.store.load()
            e, err = _find(data["events"], event, "title", "event")
            if err:
                return err
            changed = []
            if title:
                e["title"], _ = str(title).strip(), changed.append("title")
            if start:
                old_start, old_end = _read(e.get("start")), _read(e.get("end"))
                when, timed = parse_when(start)
                e["start"] = _stamp(when, timed)
                if not end and old_start and old_end:          # keep how long it lasts
                    e["end"] = _stamp(when + (old_end - old_start), timed)
                changed.append("start")
            if end:
                stop, stop_timed = parse_when(end, _read(e["start"]))
                e["end"] = _stamp(stop, stop_timed)
                changed.append("end")
            if category:
                e["cat"], _ = _pick(category, CATEGORIES, e.get("cat", "other")), changed.append("category")
            if who:
                e["to"], _ = self._who(who), changed.append("who")
            if priority:
                e["prio"], _ = _pick(priority, PRIORITIES, e.get("prio", "medium")), changed.append("priority")
            if location:
                e["loc"], _ = str(location).strip(), changed.append("location")
            if repeat:
                rec = _pick(repeat, REPEATS, "none")
                e["rec"], _ = (None if rec == "none" else rec), changed.append("repeat")
            if not changed:
                return f"Nothing to change on {e['title']}: say what to change."
            if e.get("taskId") and "start" in changed:          # moving a task's event moves the task
                for t in data["tasks"]:
                    if t["id"] == e["taskId"]:
                        t["due"] = e["start"]
            self.store.save(data)
        return f"Changed the {', '.join(changed)} of {e['title']}. It is on {say_when(e['start'])}."

    def edit_task(self, task: str, title: str = "", due: str = "", category: str = "", who: str = "",
                  priority: str = "") -> str:
        title, due = _text(title, "title"), _text(due, "due")
        category, priority = _text(category, "category"), _text(priority, "priority")
        with self.store.lock:
            data = self.store.load()
            t, err = _find(data["tasks"], task, "title", "task")
            if err:
                return err
            changed = []
            if title:
                t["title"], _ = str(title).strip(), changed.append("title")
            if due:
                when, timed = parse_when(due)
                t["due"], _ = _stamp(when, timed), changed.append("due date")
            if category:
                t["cat"], _ = _pick(category, CATEGORIES, t.get("cat", "other")), changed.append("category")
            if who:
                t["to"], _ = self._who(who), changed.append("who")
            if priority:
                t["prio"], _ = _pick(priority, PRIORITIES, t.get("prio", "medium")), changed.append("priority")
            if not changed:
                return f"Nothing to change on {t['title']}: say what to change."
            for e in data["events"]:                         # v1 left the task's event behind
                if e.get("taskId") == t["id"]:
                    e["title"], e["cat"], e["to"], e["prio"] = t["title"], t["cat"], t["to"], t["prio"]
                    if due:
                        at = _read(t["due"])
                        at = at if _has_time(t["due"]) else at.replace(hour=9, minute=0)
                        e["start"], e["end"] = _stamp(at, True), _stamp(at + timedelta(hours=1), True)
            self.store.save(data)
        return f"Changed the {', '.join(changed)} of the task {t['title']}."

    def complete_task(self, task: str) -> str:
        with self.store.lock:
            data = self.store.load()
            pending = [t for t in data["tasks"] if t.get("status") != "completed"]
            t, err = _find(pending or data["tasks"], task, "title", "task")
            if err:
                return err
            t["status"] = "completed"
            for e in data["events"]:
                if e.get("taskId") == t["id"]:
                    e["done"] = True
            self.store.save(data)
        return f"Done: {t['title']} is marked complete."

    def delete_event(self, event: str) -> str:
        with self.store.lock:
            data = self.store.load()
            e, err = _find(data["events"], event, "title", "event")
            if err:
                return err
            data["events"] = [x for x in data["events"] if x["id"] != e["id"]]
            self.store.save(data)
        return f"Deleted the event {str(e['title']).removeprefix(DONE)}."

    def delete_task(self, task: str) -> str:
        with self.store.lock:
            data = self.store.load()
            t, err = _find(data["tasks"], task, "title", "task")
            if err:
                return err
            data["tasks"] = [x for x in data["tasks"] if x["id"] != t["id"]]
            data["events"] = [x for x in data["events"] if x.get("taskId") != t["id"]]
            self.store.save(data)
        return f"Deleted the task {t['title']}."

    def take_med(self, med: str) -> str:
        with self.store.lock:
            data = self.store.load()
            m, err = _find(data["meds"], med, "name", "medicine")
            if err:
                return err
            m["status"], m["takenOn"] = "taken", date.today().isoformat()
            self.store.save(data)
        return f"Noted: {m['name']} taken today."

    def pay_bill(self, bill: str) -> str:
        with self.store.lock:
            data = self.store.load()
            pending = [b for b in data["bills"] if b.get("status") != "paid"]
            b, err = _find(pending or data["bills"], bill, "name", "bill")
            if err:
                return err
            b["status"] = "paid"
            for e in data["events"]:
                if e.get("billId") == b["id"]:
                    e["done"] = True
            self.store.save(data)
        return f"Marked {b['name']} as paid."

    def delete_bill(self, bill: str) -> str:
        with self.store.lock:
            data = self.store.load()
            b, err = _find(data["bills"], bill, "name", "bill")
            if err:
                return err
            data["bills"] = [x for x in data["bills"] if x["id"] != b["id"]]
            data["events"] = [x for x in data["events"] if x.get("billId") != b["id"]]
            self.store.save(data)
        return f"Deleted the bill {b['name']}."

    # the page and the widget
    def widget_upcoming(self) -> dict:
        data = self.store.load()
        now = datetime.now()
        start = datetime.combine(now.date(), datetime.min.time())
        found = [(at, e) for at, e in self._between(data, start, start + timedelta(days=8))
                 if not e.get("done") and (_has_time(e.get("start")) is False or at >= now - timedelta(hours=1))]
        return {"items": [{"text": f"{say_when(_stamp(at, _has_time(e.get('start'))))}: {str(e['title']).removeprefix(DONE)}"}
                          for at, e in found[:8]]}

    def month(self, year: int, month: int) -> dict:
        """One month for the page: every day's events (repeats worked out), tasks, bills and medicines."""
        data = self.store.load()
        first = datetime(year, month, 1)
        last = datetime(year, month, _cal.monthrange(year, month)[1], 23, 59, 59)
        days: dict[str, list] = {}

        def put(day: str, row: dict) -> None:
            days.setdefault(day, []).append(row)

        for at, e in self._between(data, first, last):
            if e.get("taskId") or e.get("billId"):
                continue                                   # shown from the task or bill itself below
            put(at.date().isoformat(), {"kind": "event", "id": e["id"], "title": str(e["title"]).removeprefix(DONE),
                                        "time": f"{at:%H:%M}" if _has_time(e.get("start")) else "", "cat": e.get("cat"),
                                        "to": e.get("to"), "prio": e.get("prio"), "loc": e.get("loc"), "rec": e.get("rec")})
        for t in data["tasks"]:
            at = _read(t.get("due"))
            if at and first <= at <= last:
                put(at.date().isoformat(), {"kind": "task", "id": t["id"], "title": t["title"], "to": t.get("to"),
                                            "prio": t.get("prio"), "done": t.get("status") == "completed"})
        for b in data["bills"]:
            at = _read(b.get("due"))
            if at and first <= at <= last:
                put(at.date().isoformat(), {"kind": "bill", "id": b["id"], "title": b["name"], "amt": b.get("amt"),
                                            "done": b.get("status") == "paid"})
        today = datetime.now()
        if first <= today <= last:
            for m in data["meds"]:
                put(today.date().isoformat(), {"kind": "med", "id": m["id"], "title": m["name"], "to": m.get("to"),
                                               "done": self._med_taken(m)})
        for rows in days.values():
            rows.sort(key=lambda r: (r["kind"] != "event", r.get("time") or ""))
        return {"year": year, "month": month, "days": days}

    def view(self) -> dict:
        data = self.store.load()
        for m in data["meds"]:
            m["takenToday"] = self._med_taken(m)
        data["people"] = self.people.list(data.get("family"))
        return data


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


def _enum(values, text: str) -> dict:
    return {"type": "string", "enum": list(values), "description": text}


_WHEN = "A date and time like 2026-10-30T10:00, or words like tomorrow at 3pm or friday at 10am."
_WHO = "Who it is for: a person's name, or all for everyone. Default all."
TOOLS = [
    ("add_event", "Add an event or appointment to the calendar, once or repeating.",
     {"title": _p("string", "What it is, like Dentist."), "start": _p("string", _WHEN),
      "end": _p("string", "Optional end, same form. Default one hour later."),
      "category": _enum(CATEGORIES, "Default other."), "who": _p("string", _WHO),
      "priority": _enum(PRIORITIES, "Default medium."), "location": _p("string", "Optional place."),
      "repeat": _enum(REPEATS, "Default none.")}, ["title", "start"]),
    ("add_task", "Add a to-do task with a due date. It shows on the calendar on that day.",
     {"title": _p("string", "The task, like take the bins out."), "due": _p("string", _WHEN),
      "category": _enum(CATEGORIES, "Default other."), "who": _p("string", _WHO),
      "priority": _enum(PRIORITIES, "Default medium.")}, ["title", "due"]),
    ("add_med", "Track a medicine to take: its dose, how often and when.",
     {"name": _p("string", "The medicine."), "dose": _p("string", "Like 10 mg or one tablet."), "who": _p("string", _WHO),
      "how_often": _enum(MED_FREQUENCIES, "Default daily."), "next_time": _p("string", "When to take it, like 8:00 AM.")},
     ["name"]),
    ("add_bill", "Track a bill to pay: its amount and due date. It shows on the calendar on that day.",
     {"name": _p("string", "The bill, like electric."), "amount": _p("number", "How much, like 84.50 (0 to 10,000,000)."),
      "due": _p("string", "The due date, like 2026-11-05 or friday."), "category": _enum(BILL_CATEGORIES, "Default utilities."),
      "autopay": _p("boolean", "True if it pays itself.")}, ["name", "amount", "due"]),
    ("list_events", "List what is on the calendar: one day, or today and the next few days.",
     {"days_ahead": _p("integer", "How many days after today. Default 7; 0 for today only."),
      "day": _p("string", "Or one day, like tomorrow, friday or 2026-10-30.")}, []),
    ("list_tasks", "List the to-do tasks: open ones by default, or done ones.",
     {"status": _enum(("pending", "completed", "all"), "Default pending (open).")}, []),
    ("list_meds", "List the medicines being tracked and whether each was taken today.", {}, []),
    ("list_bills", "List the bills: unpaid ones by default, with the total.",
     {"status": _enum(("pending", "paid", "all"), "Default pending (unpaid).")}, []),
    ("edit_event", "Change an event: move it, rename it, or change who, where or how often. Name the event, then only what changes.",
     {"event": _p("string", "The event's title, like dentist."), "title": _p("string", "A new title."),
      "start": _p("string", "A new start. " + _WHEN), "end": _p("string", "A new end."),
      "category": _enum(CATEGORIES, "A new category."), "who": _p("string", "Who it is for now."),
      "priority": _enum(PRIORITIES, "A new priority."), "location": _p("string", "A new place."),
      "repeat": _enum(REPEATS, "A new repeat, or none to stop it repeating.")}, ["event"]),
    ("edit_task", "Change a task: rename it, move its due date, or change who or its priority.",
     {"task": _p("string", "The task's title."), "title": _p("string", "A new title."), "due": _p("string", "A new due date."),
      "category": _enum(CATEGORIES, "A new category."), "who": _p("string", "Who it is for now."),
      "priority": _enum(PRIORITIES, "A new priority.")}, ["task"]),
    ("complete_task", "Mark a to-do task as done.", {"task": _p("string", "The task's title.")}, ["task"]),
    ("delete_event", "Delete an event from the calendar.", {"event": _p("string", "The event's title.")}, ["event"]),
    ("delete_task", "Delete a to-do task.", {"task": _p("string", "The task's title.")}, ["task"]),
    ("take_med", "Note that a medicine was taken today.", {"med": _p("string", "The medicine's name.")}, ["med"]),
    ("pay_bill", "Mark a bill as paid.", {"bill": _p("string", "The bill's name.")}, ["bill"]),
    ("delete_bill", "Delete a bill so it is no longer tracked.", {"bill": _p("string", "The bill's name.")}, ["bill"]),
]
CHANGES = ["add_event", "add_task", "add_med", "add_bill", "edit_event", "edit_task", "complete_task",
           "delete_event", "delete_task", "take_med", "pay_bill", "delete_bill"]
TOOL_NAMES = {t[0] for t in TOOLS}


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n.startswith("delete_")}}
            for n, d, props, req in TOOLS]


def run_tool(app: Calendar, name: str, args: dict) -> str:
    if name not in TOOL_NAMES:
        raise KeyError(name)
    if not isinstance(args, dict):
        raise TypeError("send the arguments as one JSON object")
    return getattr(app, name)(**args)


def mcp_answer(app: Calendar, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object."}}

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
        if name not in TOOL_NAMES:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            return ok({"content": [{"type": "text", "text": run_tool(app, name, args)}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those details did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - a plain answer, never a dropped connection
            return ok({"content": [{"type": "text", "text": f"Calendar hit a problem with {name} ({type(exc).__name__}). "
                                    "Nothing was saved."}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Calendar does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Calendar/2"
    app: Calendar
    token: str

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self._sent = True
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, allow_nan=False).encode("utf-8"))

    _raw: bytes | None = None                  # this request's body, read once (``_drain``)
    _too_big = False

    def _drain(self) -> bytes:
        """This request's body, read once and capped at MAX_BODY, before any reply: a reply sent over an
        unread body resets the connection on Windows (WinError 10053), so the page's buttons flaked."""
        if self._raw is None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            self._too_big = n > MAX_BODY
            self._raw = self.rfile.read(min(n, MAX_BODY)) if n > 0 else b""
        return self._raw

    def _body(self):
        raw = self._drain()
        if self._too_big:
            raise ValueError("That is too big for Calendar.")
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        """Every route needs the token (D48d). Read the body first, so the refusal arrives whole."""
        if self._token_ok():
            return False
        self._drain()
        self._json(401, {"error": "Calendar needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def do_HEAD(self):
        self.do_GET()

    def _guard(self, fn) -> None:
        """Any error left over becomes a plain 500 answer, never a dropped connection."""
        self._sent = False
        self._raw, self._too_big = None, False     # a new request on this connection: its own body
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            if not self._sent:
                self._json(500, {"error": f"Calendar hit a problem ({type(exc).__name__}). Nothing was saved."})

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        if self._refused():
            return
        path, _, query = self.path.partition("?")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/widget/upcoming":
                return self._json(200, self.app.widget_upcoming())
            if path == "/data":
                return self._json(200, self.app.view())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
            if path == "/people":
                return self._json(200, self.app.people.list(self.app.store.load().get("family")))
            if path == "/month":
                q = urllib.parse.parse_qs(query)
                today = date.today()
                y, m = int((q.get("y") or [today.year])[0]), int((q.get("m") or [today.month])[0])
                if not (1 <= m <= 12 and 1900 <= y <= 2200):
                    return self._json(400, {"error": "Ask for a month 1 to 12 of a real year."})
                return self._json(200, self.app.month(y, m))
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except ValueError:
            return self._json(400, {"error": "Ask for a month by its numbers, like ?y=2026&m=10."})
        self._json(404, {"error": "Calendar has no page here."})

    def _post(self):
        if self._refused():
            return
        self._drain()                              # read before any reply, the 404 and the errors too
        path = self.path.split("?", 1)[0]
        if path == "/mcp":
            try:
                msg = self._body()
            except ValueError:
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
            answer = mcp_answer(self.app, msg)
            return self._send(202) if answer is None else self._json(200, answer)
        if path.startswith("/api/") and path[5:] in TOOL_NAMES:
            try:
                return self._json(200, {"message": run_tool(self.app, path[5:], self._body())})
            except Unreadable as exc:
                return self._json(409, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                return self._json(400, {"error": f"Those details did not fit: {exc}"})
        self._json(404, {"error": "Calendar has no page here."})

    def do_PUT(self):
        self._raw, self._too_big = None, False
        if self._refused():
            return
        self._drain()                              # read before the reply (WinError 10053 otherwise)
        self._send(405, b"", "text/plain")

    do_DELETE = do_PUT


def make_server(port: int, data: Path | str, token: str | None, api: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Calendar(Store(data), People(api, token))
    handler = type("CalendarHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.app = app
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Calendar for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Calendar with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), os.environ.get("PUFFPAI_API"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
