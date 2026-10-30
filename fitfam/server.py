# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/app_data.py (adapted)
"""FitFam: workouts for each person in the household, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``               the page (web/index.html), shown in the Desktop at /pkg/fitfam/
- ``/health``         "ok"
- ``/widget/streaks`` the streaks widget, ``{"items": [{"text"}]}``
- ``/people``         who can log workouts (the Core's people list, see below)
- ``/exercises``      the exercise library (exercises.json in this package)
- ``/suggest``        a workout made from the library (GET, changes nothing)
- ``/data``           the whole document (GET), ``/updated`` its stamp
- ``/api/<action>``   the page's changes (POST), the same functions as the tools
- ``/mcp``            the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/fitfam/, which adds it.

People (D12): FitFam keeps no people list of its own. It reads the Core's
shared list (``GET /v1/people`` with its token). Anyone already in FitFam's
data (a file copied in from today's BuildBot) still counts, so no history is
lost. With nobody at all, it logs for one person, "You".

The data is one JSON document in PUFFPAI_DATA (``fitfam.json``, the v1 shape
``{users: {<id>: {name, emoji, prefs, goals, history, sets, streak,
totalWorkouts}}, last_updated}``, so a v1 file can be copied in). Writes replace
the whole file at once (temp file, then rename). A file that exists but will
not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import random
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
LIBRARY = HERE / "exercises.json"
NAME = "fitfam"
VERSION = "2.0.0"
DOC = "fitfam.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
ME = {"id": "me", "name": "You", "emoji": ""}
SELF_WORDS = ("", "me", "i", "myself", "you", "mine")
LEVEL_WORDS = {"easy": "easy", "beginner": "easy", "light": "easy", "mid": "mid", "moderate": "mid",
               "medium": "mid", "normal": "mid", "challenging": "challenging", "hard": "challenging",
               "advanced": "challenging", "tough": "challenging"}
KIND_WORDS = {"yoga": "rom", "stretch": "rom", "stretching": "rom", "flexibility": "rom", "mobility": "rom",
              "weights": "gym", "strength": "gym", "lifting": "gym", "bodyweight": "calisthenics", "kb": "kettlebell",
              "full body": "full"}


def _uid() -> str:
    return secrets.token_hex(6)


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int | float, word: str) -> str:
    return f"{n:g} {word}{'' if n == 1 else 's'}"


def _words(text: str) -> list[str]:
    """Lower-case words with a plural s dropped: "Pull-Ups" and "pull up" read the same."""
    return [w[:-1] if len(w) > 2 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", text.lower())]


def _day(iso: str) -> date | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError.
    Never NaN or infinity, and never a huge number that overflows."""
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


def _num(value) -> float:
    """A stored number for showing: 0 for anything that is not a finite number."""
    if isinstance(value, bool):
        return float(value)
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _finite(obj):
    """*obj* with every NaN or infinity replaced by 0: repairs a file saved before input was checked."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return 0
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _loads(text: str):
    """JSON from a request: NaN and Infinity are refused, and a huge number is checked where it is used."""
    return json.loads(text, parse_constant=_no_constant)


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"users": {}, "last_updated": ""}


class Store:
    """One JSON document with a stamp (the v1 pattern, app_data.py)."""

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
                data = _finite(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). FitFam will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("users", {}), dict):
                raise Unreadable(f"{self.path} is not a FitFam file. FitFam will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".fitfam-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- people

class CorePeople:
    """The Core's shared people list (D12), read with this package's token. None when it can't be read."""

    def __init__(self, api: str | None, token: str | None, ttl_s: float = 30) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        self.ttl_s = ttl_s
        self._cache: tuple[float, list[dict] | None] = (0.0, None)
        # The Core is on this computer: never send this call through the Bridge proxy.
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def get(self) -> list[dict] | None:
        if not self.api or not self.token:
            return None
        at, got = self._cache
        if time.monotonic() - at < self.ttl_s:
            return got
        req = urllib.request.Request(self.api + "/v1/people", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self._open(req, timeout=3) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            got = [p for p in body if isinstance(p, dict) and isinstance(p.get("id"), str)] \
                if isinstance(body, list) else None
        except (urllib.error.URLError, OSError, ValueError):
            got = None
        self._cache = (time.monotonic(), got)
        return got


# ------------------------------------------------------------- the logic
# From app_data.py:1175-1295 (fit_log_workout, fit_get_status, fit_log_set), plus what v1 did only in its page
# (fitfam-app.jsx: the workout maker, goals, history).

def _new_user(name: str, emoji: str) -> dict:
    return {"name": name, "emoji": emoji, "prefs": {"level": "mid", "type": "full", "duration": 30},
            "goals": [], "history": [], "sets": [], "streak": 0, "totalWorkouts": 0}


def streak_of(user: dict, today: date | None = None) -> int:
    """Days in a row with a workout or a set, counting back from today (or from yesterday when today
    has nothing yet, so a streak is not lost before the day's workout)."""
    days = {d for d in (_day(e.get("date", "")) for e in (user.get("history") or []) + (user.get("sets") or []))
            if d is not None}
    day = today or date.today()
    if day not in days:
        day -= timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


class FitFam:
    def __init__(self, store: Store, people: CorePeople, library: dict) -> None:
        self.store = store
        self.core_people = people
        self.library = library
        self.by_id = {e["id"]: e for pool in library["exercises"].values() for e in pool}
        self.rng = random.Random()

    # people
    def people(self, data: dict | None = None) -> list[dict]:
        data = data if data is not None else self.store.load()
        out = [{"id": p["id"], "name": str(p.get("name") or p["id"]), "emoji": str(p.get("emoji") or ""),
                "role": p.get("role", "adult")} for p in (self.core_people.get() or [])]
        known = {p["id"] for p in out}
        for uid, u in data["users"].items():
            if uid not in known and isinstance(u, dict):
                out.append({"id": uid, "name": str(u.get("name") or uid), "emoji": str(u.get("emoji") or ""),
                            "role": "adult"})
        return out or [dict(ME, role="adult")]

    def _who(self, data: dict, person: str | None) -> tuple[str, dict]:
        """The person *person* names (an id, a name or a first name; "me" when there is one person), and their
        record in *data* (made when they have none yet)."""
        people = self.people(data)
        text = _text(person, "person", 60).lower()
        found = None
        if text in SELF_WORDS:
            if len(people) == 1:
                found = people[0]
            else:
                found = next((p for p in people if p["id"] == "me"), None)
                if found is None:
                    raise ValueError(f"say who it is for. People in FitFam: {', '.join(p['name'] for p in people)}")
        else:
            found = next((p for p in people if text in (p["id"].lower(), p["name"].lower())), None) or \
                next((p for p in people if p["name"].lower().split()[:1] == [text]), None)
            if found is None:
                raise ValueError(f"FitFam does not know {person!r}. People in FitFam: "
                                 f"{', '.join(p['name'] for p in people)}. People are added to the household "
                                 "list in PuffPAI")
        user = data["users"].setdefault(found["id"], _new_user(found["name"], found["emoji"]))
        user["name"] = found["name"]
        if found["emoji"]:
            user["emoji"] = found["emoji"]
        for key, empty in (("goals", []), ("history", []), ("sets", []), ("prefs", {})):
            user.setdefault(key, empty)
        return found["id"], user

    @staticmethod
    def _whose(user: dict, cap: bool = False) -> str:
        if user["name"] == ME["name"]:
            return "Your" if cap else "your"
        return f"{user['name']}'s"

    @staticmethod
    def _them(user: dict) -> str:
        return "you" if user["name"] == ME["name"] else user["name"]

    def _bump(self, user: dict, first_today: bool) -> None:
        if first_today:
            user["totalWorkouts"] = int(_num(user.get("totalWorkouts"))) + 1
        user["streak"] = streak_of(user)

    def _exercise(self, text: str) -> tuple[str, str]:
        """(id, name) of the library exercise *text* names, or the words as given."""
        low = text.strip().lower()
        for e in self.by_id.values():
            if low in (e["id"], e["name"].lower()):
                return e["id"], e["name"]
        words = _words(low)
        hits = [e for e in self.by_id.values() if words and set(words) <= set(_words(e["name"]))]
        if hits:
            e = min(hits, key=lambda e: len(e["name"]))
            return e["id"], e["name"]
        return text.strip(), text.strip()

    # tools
    def log_workout(self, type: str = "workout", minutes: int = 30, person: str = "", notes: str = "") -> str:
        """From fit_log_workout (app_data.py:1175)."""
        minutes = _number(minutes, "minutes", 1, 1440, whole=True)
        kind = _text(type, "type", 60, "workout")
        notes = _text(notes, "notes", 500)
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            user["history"].append({"date": _now(), "type": kind, "typeName": kind, "duration": minutes,
                                    "notes": notes, "source": "brain"})
            self._bump(user, True)
            self.store.save(data)
        streak = user["streak"]
        more = f" That's {_plural(streak, 'day')} in a row!" if streak > 1 else ""
        return f"Logged {self._whose(user)} {minutes}-minute {kind} workout.{more}"

    def log_set(self, exercise: str, reps: int, weight: float = 0, person: str = "", rpe: float | None = None,
                notes: str = "") -> str:
        """From fit_log_set (app_data.py:1234): one set, with its weight, reps and an optional RPE."""
        reps = _number(reps, "reps", 1, 1000, whole=True)
        weight = _number(0 if weight in (None, "") else weight, "weight", 0, 2000)
        rpe_val = None if rpe in (None, "") else _number(rpe, "RPE", 0, 10)
        notes = _text(notes, "notes", 500)
        ex_id, ex_name = self._exercise(_text(exercise, "exercise", 80))
        if not ex_id:
            raise ValueError("say which exercise")
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            today = date.today()
            first_today = not any(_day(e.get("date", "")) == today for e in user["history"] + user["sets"])
            user["sets"].append({"id": f"s{_uid()}", "date": _now(), "exerciseId": ex_id, "exercise": ex_name,
                                 "weight": weight, "reps": reps, "rpe": rpe_val, "notes": notes})
            self._bump(user, first_today)
            self.store.save(data)
        load = f"{weight:g} x {reps}" if weight else f"{reps} reps"
        return (f"Logged a set of {ex_name} for {self._them(user)}: {load}"
                + (f" at RPE {rpe_val:g}" if rpe_val is not None else "") + ".")

    def status(self, person: str = "") -> str:
        """From fit_get_status (app_data.py:1213): streaks and totals, for one person or everyone."""
        data = self.store.load()
        if _text(person, "person", 60).lower() not in SELF_WORDS or len(self.people(data)) == 1:
            _pid, user = self._who(data, person)
            return self._status_line(user)
        users = [self._who(data, p["id"])[1] for p in self.people(data)]
        return "\n".join(self._status_line(u) for u in users)

    def _status_line(self, u: dict) -> str:
        week = date.today() - timedelta(days=6)
        this_week = sum(1 for h in u["history"] if (_day(h.get("date", "")) or date.min) >= week)
        line = (f"{u['name']}: {streak_of(u)}-day streak, {_plural(int(_num(u.get('totalWorkouts'))), 'workout')} "
                f"in all, {this_week} this week.")
        if u["history"]:
            h = u["history"][-1]
            line += (f" Last workout: {h.get('typeName') or h.get('type') or 'workout'} "
                     f"({h.get('duration', 0)} min) on {(_day(h.get('date', '')) or date.today()):%b %d}.")
        return line

    def history(self, person: str = "", limit: int = 5) -> str:
        data = self.store.load()
        _pid, u = self._who(data, person)
        limit = min(_number(5 if limit in (None, "", 0) else limit, "limit", 1, 1000, whole=True), 50)
        rows = []
        for h in u["history"][-limit:][::-1]:
            d = _day(h.get("date", ""))
            bits = [f"{h.get('duration', 0)} min"] + ([f"{h['exerciseCount']} exercises"] if h.get("exerciseCount") else [])
            rows.append((f"{d:%b %d} " if d else "") + f"{h.get('typeName') or h.get('type') or 'workout'} ({', '.join(bits)})")
        sets = [f"{(_day(s.get('date', '')) or date.today()):%b %d} {s.get('exercise') or s.get('exerciseId')}: "
                + (f"{_num(s.get('weight')):g} x {_num(s.get('reps')):g}" if s.get("weight") else f"{_num(s.get('reps')):g} reps")
                for s in u["sets"][-limit:][::-1]]
        if not rows and not sets:
            return f"No workouts are logged yet for {self._them(u)}."
        out = f"{self._whose(u, True)} recent workouts:\n" + ("\n".join(rows) if rows else "none")
        if sets:
            out += "\nRecent sets:\n" + "\n".join(sets)
        return out

    def suggest_workout(self, type: str = "", minutes: int = 0, level: str = "", person: str = "") -> str:
        """The page's workout maker (fitfam-app.jsx generateWorkout), for chat. Nothing is saved."""
        got = self.make_workout(type, minutes, level, person)
        lines = [f"{i + 1}. {e['name']}: {e['prescription']} ({e['desc']})" for i, e in enumerate(got["exercises"])]
        return (f"A {got['minutes']}-minute {got['label']} workout ({self.library['levels'][got['level']].lower()}):\n"
                + "\n".join(lines) + "\nSay when it is done and I can log it.")

    def make_workout(self, type: str = "", minutes: int = 0, level: str = "", person: str = "") -> dict:
        data = self.store.load()
        try:
            prefs = self._who(data, person)[1].get("prefs") or {}
        except ValueError:
            if _text(person, "person", 60).lower() not in SELF_WORDS:
                raise
            prefs = {}
        type, level = _text(type, "type", 40), _text(level, "level", 20)
        minutes = _number(minutes or 0, "minutes", 0, 1440, whole=True)
        types = {t["id"]: t for t in self.library["types"]}
        want = str(type or prefs.get("type") or "full").strip().lower()
        want = KIND_WORDS.get(want, want)
        kind = types.get(want) or next((t for t in self.library["types"]
                                        if want in (t["label"].lower(), t["id"]) or want in t["label"].lower()
                                        or (want.rstrip("s") in t["id"])), None)
        if kind is None:
            raise ValueError(f"there is no {type!r} workout. Kinds: {', '.join(t['label'] for t in self.library['types'])}")
        lvl = LEVEL_WORDS.get(str(level or prefs.get("level") or "mid").strip().lower())
        if lvl is None:
            raise ValueError("level is easy, mid or challenging")
        mins = int(minutes or _num(prefs.get("duration")) or 30)
        sizes = {int(k): v for k, v in self.library["durations"].items()}
        mins = min(sizes, key=lambda m: abs(m - mins))
        count = sizes[mins]
        pool = [e for cat in kind["pool"] for e in self.library["exercises"].get(cat, [])]
        shuffled = self.rng.sample(pool, len(pool))
        picked, muscles = [], set()
        for e in shuffled:                          # one per muscle first, as the v1 page did
            if len(picked) >= count:
                break
            if e["muscle"] not in muscles:
                picked.append(e)
                muscles.add(e["muscle"])
        for e in shuffled:
            if len(picked) >= count:
                break
            if e not in picked:
                picked.append(e)
        return {"type": kind["id"], "label": kind["label"], "level": lvl, "minutes": mins,
                "exercises": [dict(e, prescription=e["levels"][lvl]) for e in picked]}

    def list_goals(self, person: str = "") -> str:
        data = self.store.load()
        _pid, u = self._who(data, person)
        if not u["goals"]:
            return f"No fitness goals yet for {self._them(u)}."
        rows = []
        for g in u["goals"]:
            target = _num(g.get("target"))
            pct = min(100, round(100 * _num(g.get("current")) / target)) if target > 0 else 0
            rows.append(f"{g.get('text')}: {_num(g.get('current')):g} of {target:g} {g.get('unit', '')} ({pct}%)"
                        + (" done" if pct >= 100 else ""))
        return f"{self._whose(u, True)} goals:\n" + "\n".join(rows)

    def add_goal(self, goal: str, target: float, unit: str = "reps", person: str = "") -> str:
        text = _text(goal, "goal", 200)
        target = _number(target, "target", 0, 1_000_000)
        unit = _text(unit, "unit", 20, "reps")
        if not text or target <= 0:
            raise ValueError("a goal needs words and a target above 0")
        with self.store.lock:
            data = self.store.load()
            _pid, u = self._who(data, person)
            u["goals"].append({"id": f"g{_uid()}", "text": text, "target": target, "unit": unit,
                               "current": 0})
            self.store.save(data)
        return f"Added a goal for {self._them(u)}: {text} ({target:g} {unit or 'reps'})."

    def _goal(self, u: dict, goal: str) -> dict:
        low = _text(goal, "goal", 200).lower()
        hit = next((g for g in u["goals"] if low in (str(g.get("id", "")).lower(), str(g.get("text", "")).lower())), None) \
            or next((g for g in u["goals"] if low and low in str(g.get("text", "")).lower()), None) \
            or next((g for g in u["goals"] if low and any(w in str(g.get("text", "")).lower()
                                                           for w in low.split() if len(w) > 3)), None)
        if hit is None:
            have = "; ".join(str(g.get("text")) for g in u["goals"]) or "none yet"
            raise ValueError(f"there is no goal like {goal!r} for {self._them(u)}. Goals: {have}")
        return hit

    def update_goal(self, goal: str, current: float, person: str = "") -> str:
        current = _number(current, "current", 0, 1_000_000)
        with self.store.lock:
            data = self.store.load()
            _pid, u = self._who(data, person)
            g = self._goal(u, goal)
            target = _num(g.get("target")) or current
            g["current"] = min(current, target)
            self.store.save(data)
        done = " Goal reached!" if g["current"] >= target else ""
        return f"Updated {self._whose(u)} goal {g['text']}: {g['current']:g} of {target:g} {g.get('unit', '')}.{done}"

    # the page only
    def complete_workout(self, person: str, typeName: str, level: str, duration: int, exerciseCount: int,
                         elapsed: int = 0) -> str:
        """The page's "Complete Workout" (fitfam-app.jsx handleCompleteWorkout)."""
        typeName = _text(typeName, "typeName", 80, "Workout")
        if level not in self.library["levels"]:
            raise ValueError("level is easy, mid or challenging")
        duration = _number(duration, "duration", 0, 1440, whole=True)
        exerciseCount = _number(exerciseCount, "exerciseCount", 0, 200, whole=True)
        elapsed = _number(elapsed or 0, "elapsed", 0, 86_400, whole=True)
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            user["history"].append({"date": _now(), "type": typeName, "typeName": typeName, "level": level,
                                    "duration": duration, "exerciseCount": exerciseCount, "elapsed": elapsed})
            self._bump(user, True)
            self.store.save(data)
        return f"Logged {self._whose(user)} {typeName} workout."

    def remove_workout(self, person: str, index: int) -> str:
        index = _number(index, "index", 0, 1_000_000, whole=True)
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            if not 0 <= index < len(user["history"]):
                raise ValueError("there is no workout at that place in the list")
            gone = user["history"].pop(index)
            user["totalWorkouts"] = max(0, int(_num(user.get("totalWorkouts"))) - 1)
            user["streak"] = streak_of(user)
            self.store.save(data)
        return f"Removed the {gone.get('typeName') or gone.get('type') or 'workout'} workout."

    def remove_goal(self, person: str, goal: str) -> str:
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            g = self._goal(user, goal)
            user["goals"].remove(g)
            self.store.save(data)
        return f"Removed the goal {g['text']}."

    def save_prefs(self, person: str, type: str, level: str, duration: int) -> str:
        if type not in {t["id"] for t in self.library["types"]} or level not in self.library["levels"]:
            raise ValueError("pick a kind of workout and a level from the list")
        duration = _number(duration, "duration", 0, 1440, whole=True)
        if str(duration) not in self.library["durations"]:
            raise ValueError(f"duration is one of {', '.join(self.library['durations'])} minutes")
        with self.store.lock:
            data = self.store.load()
            _pid, user = self._who(data, person)
            user["prefs"] = {"type": type, "level": level, "duration": duration}
            self.store.save(data)
        return "Saved as the Quick Start."

    def widget_streaks(self) -> dict:
        data = self.store.load()
        items = []
        for p in self.people(data):
            u = data["users"].get(p["id"]) or _new_user(p["name"], p["emoji"])
            items.append({"text": f"{p['name']}: {streak_of(u)}-day streak, "
                                  f"{_plural(int(_num(u.get('totalWorkouts'))), 'workout')}"})
        return {"items": items}


PAGE_ACTIONS = ("complete_workout", "remove_workout", "remove_goal", "save_prefs", "add_goal", "update_goal")


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


PERSON = _p("string", "Who it is for, by name. Leave empty for the person speaking when only one person uses FitFam.")
TOOLS = [
    ("log_workout", "Log a workout someone did: what kind and how many minutes. Updates their streak.",
     {"type": _p("string", "What they did, like run, yoga, kettlebell or swim."),
      "minutes": _p("integer", "How long, in minutes."), "person": PERSON,
      "notes": _p("string", "Optional notes.")}, ["type", "minutes"]),
    ("log_set", "Log one strength set: the exercise, the reps, and the weight lifted.",
     {"exercise": _p("string", "Like bench press or goblet squat."), "reps": _p("integer", "How many reps."),
      "weight": _p("number", "The weight lifted, in the owner's usual unit. 0 for bodyweight."), "person": PERSON,
      "rpe": _p("number", "Optional effort from 1 to 10."), "notes": _p("string", "Optional notes.")},
     ["exercise", "reps"]),
    ("status", "Workout streaks and totals: how many days in a row, workouts this week and in all, and the last one.",
     {"person": _p("string", "Optional: one person by name. Empty for everyone.")}, []),
    ("history", "List someone's recent workouts and strength sets, newest first.",
     {"person": PERSON, "limit": _p("integer", "How many of each to list. Default 5.")}, []),
    ("suggest_workout", "Make a workout plan from the exercise library: a list of exercises with sets and reps. "
     "Nothing is logged.",
     {"type": _p("string", "Calisthenics, Kettlebell, Range of Motion, Gym Strength, Calisthenics + KB, "
                 "Gym + Flexibility or Full Session. Default: their Quick Start."),
      "minutes": _p("integer", "15, 30, 45 or 60."), "level": _p("string", "easy, mid or challenging."),
      "person": PERSON}, []),
    ("list_goals", "List someone's fitness goals and how close they are.", {"person": PERSON}, []),
    ("add_goal", "Add a fitness goal with a target, like 20 push-ups in a row or a 2-minute plank.",
     {"goal": _p("string", "The goal in words."), "target": _p("number", "The number to reach."),
      "unit": _p("string", "Like reps, seconds, minutes, days, sessions or lbs. Default reps."),
      "person": PERSON}, ["goal", "target"]),
    ("update_goal", "Set the progress on a fitness goal: the number reached so far.",
     {"goal": _p("string", "Which goal, by a word in it, like plank."),
      "current": _p("number", "The number reached so far."), "person": PERSON}, ["goal", "current"]),
]
CHANGES = ["log_workout", "log_set", "add_goal", "update_goal"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: FitFam, msg: dict) -> dict | None:
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
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"FitFam does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FitFam/2"
    app: FitFam
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

    def _query(self) -> dict:
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        return {k: v[0] for k, v in q.items()}

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read. A body with a sound
        Content-Length is read, so the refusal arrives whole; any other one closes the connection."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "FitFam needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"FitFam stopped on this request with an error ({type(exc).__name__})."})
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
            if path == "/widget/streaks":
                return self._json(200, self.app.widget_streaks())
            if path == "/people":
                return self._json(200, self.app.people())
            if path == "/exercises":
                return self._json(200, self.app.library)
            if path == "/suggest":
                q = self._query()
                return self._json(200, self.app.make_workout(q.get("type", ""), q.get("minutes") or 0,
                                                             q.get("level", ""), q.get("person", "")))
            if path == "/data":
                return self._json(200, self.app.store.load())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "FitFam has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        action = path[len("/api/"):] if path.startswith("/api/") else ""
        if action not in PAGE_ACTIONS:
            return self._json(404, {"error": "FitFam has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send the details as one JSON object."})
            message = getattr(self.app, action)(**body)
            self._json(200, {"message": message, "data": self.app.store.load()})
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


def make_server(port: int, data: Path | str, token: str | None, api: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    library = json.loads(LIBRARY.read_text(encoding="utf-8"))
    app = FitFam(Store(data), CorePeople(api, token), library)
    handler = type("FitFamHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="FitFam for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts FitFam with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
