# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/app_data.py (adapted)
"""AlarmFlow: timers and alarms, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``               the page (web/index.html), shown in the Desktop at /pkg/alarmflow/
- ``/health``         "ok" while the ringing clock runs (503 if it stopped)
- ``/widget/running`` the "Timers and alarms" widget, ``{"items": [{"text"}]}``
- ``/data``           the whole document (GET), ``/updated`` its stamp
- ``/api/<tool>``     POST: the page's buttons, the same functions as the MCP tools
- ``/mcp``            the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). GET routes never
change anything (D55).

The ringing clock is a thread in this process: once a second it marks every item that is due
as ``ringing`` and asks the Core to tell the owner (``POST /v1/notify``, through the Core API with
this package's token). On Windows it also beeps through this computer's speakers (``winsound``, standard
library) every few seconds for up to 10 minutes, so an alarm rings while AlarmFlow runs, with no page
open. Elsewhere there is no beep; the Desktop card and the open page carry the ring. Steward starts
AlarmFlow again when BuildBot starts, and Caretaker restarts it if it stops. While BuildBot is off
nothing rings; an item that came due meanwhile rings at the next start and says how late it is.

The data is one JSON document in PUFFPAI_DATA (``alarmflow.json``) with a ``last_updated`` stamp,
the same shape as v1's ``alarmflow.json``, so a v1 file can be copied in. Writes replace the whole
file at once (temp file, then rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "alarmflow"
VERSION = "2.1.0"
DOC = "alarmflow.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 1024 * 1024
SOUNDS = ("chime", "alert", "gentle", "bell", "none")
REPEATS = ("none", "daily", "weekdays", "weekends", "weekly")
ACTIVE = ("running", "paused", "ringing")


def _uid() -> str:
    return secrets.token_hex(5)


def _ms() -> int:
    return int(time.time() * 1000)


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _things(n: int) -> str:
    return "1 timer or alarm" if n == 1 else f"{n} timers or alarms"


MAX_TIMER_S = 7 * 24 * 3600         # a timer runs at most 7 days
MAX_SHIFT_MIN = 24 * 60              # snooze and add_time move an item at most 24 hours
RING_EVERY_MS = 5_000                # a ringing item beeps every 5 seconds ...
RING_FOR_MS = 10 * 60_000            # ... for up to 10 minutes; then the notice card and the page still show it


def clock(ms: int) -> str:
    """7:05 AM, from epoch milliseconds, in this computer's time ("an unknown time" for a broken stamp)."""
    try:
        t = datetime.fromtimestamp(int(ms) / 1000)
    except (OverflowError, OSError, ValueError, TypeError):
        return "an unknown time"
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _text(value, name: str, default: str = "") -> str:
    """A plain text argument, checked before anything is saved."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value.strip()[:200]


def _whole(value, name: str, lo: int, hi: int) -> int:
    """A whole number from lo to hi, checked before anything is saved."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a whole number")
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
            raise ValueError(f"{name} must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be from {lo} to {hi}")
    return n


def _amount(value, name: str, hi: float) -> float:
    """A length from the brain or the page: a whole or part number (1.5 hours), from 0 to hi."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if n != n or n in (float("inf"), float("-inf")) or n < 0:
        raise ValueError(f"{name} must be a number, 0 or more")
    if n > hi:
        raise ValueError(f"{name} must be from 0 to {hi:g}")
    return n


_NUMBERS = {w: n for n, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                       "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_NUMBERS.update({"a": 1, "an": 1, "couple": 2, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
                 "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90})
_UNITS = {**dict.fromkeys(("hours", "hour", "hrs", "hr", "h"), 3600),
          **dict.fromkeys(("minutes", "minute", "mins", "min", "m"), 60),
          **dict.fromkeys(("seconds", "second", "secs", "sec", "s"), 1)}


def parse_duration(text: str) -> int | None:
    """Seconds from the way people say a length: "20 minutes", "an hour and a half", "half an hour",
    "1h30m", "two and a half hours", "90 seconds". None when there is no unit in it."""
    words = re.findall(r"\d+(?:\.\d+)?|[a-z]+", re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", str(text).lower()))
    total, n, last, i = 0.0, None, 0, 0
    while i < len(words):
        w, rest = words[i], words[i + 1:i + 3]
        if w in _UNITS:
            total += (1 if n is None else n) * _UNITS[w]
            last, n = _UNITS[w], None
        elif w == "and" and rest == ["a", "half"]:              # "two and a half hours", "an hour and a half"
            if n is not None:
                n += 0.5
            elif last:
                total += last / 2
            i += 2
        elif w in ("half", "quarter") and rest and (rest[0] in ("a", "an", "of") or rest[0] in _UNITS):
            n = 0.5 if w == "half" else 0.25                     # "half an hour", "quarter of an hour"
            while i + 1 < len(words) and words[i + 1] in ("a", "an", "of"):
                i += 1
        elif re.fullmatch(r"\d+(?:\.\d+)?", w):
            n = float(w)
        elif w in _NUMBERS:
            v = _NUMBERS[w]
            n = n + v if n is not None and n >= 20 and n % 10 == 0 and v < 10 else v     # "twenty five"
        i += 1
    return round(total) if total > 0 else None


def parse_clock(text: str) -> tuple[int, int] | None:
    """(hour, minute) from a time of day as people say it: "7:30 AM", "6pm", "18:30", "noon", "midnight",
    "6:30 in the evening". None when it does not read as a time of day."""
    t = re.sub(r"\b([ap])\.?\s?m\b\.?", r"\1m", str(text).lower().strip())
    if re.search(r"\b(noon|midday)\b", t):
        return 12, 0
    if "midnight" in t:
        return 0, 0
    m = re.search(r"\b(\d{1,2})(?:[:.]?(\d{2}))?\s*(am|pm)?\b", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
    elif re.search(r"\b(evening|afternoon|tonight|night)\b", t) and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


_REPEAT_WORDS = {"": "none", "once": "none", "never": "none", "no": "none", "every day": "daily",
                 "everyday": "daily", "weekday": "weekdays", "weekend": "weekends", "every week": "weekly"}


def say_span(seconds: int) -> str:
    """1 hour and 30 minutes, the way the brain says a length back."""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return " and ".join(_plural(n, w) for n, w in ((h, "hour"), (m, "minute"), (s, "second")) if n) or "0 seconds"


def day_word(ms: int, now: datetime | None = None) -> str:
    """today, tomorrow, or the weekday's name."""
    now = now or datetime.now()
    try:
        when = datetime.fromtimestamp(int(ms) / 1000)
    except (OverflowError, OSError, ValueError, TypeError):
        return ""
    if when.date() == now.date():
        return "today"
    if when.date() == now.date() + timedelta(days=1):
        return "tomorrow"
    return f"{when:%A}"


def span(seconds: int) -> str:
    """4m 05s, or 1h 02m 05s."""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"items": [], "last_updated": ""}


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
                raise Unreadable(f"{self.path} will not read ({exc}). AlarmFlow will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                raise Unreadable(f"{self.path} is not an AlarmFlow document. AlarmFlow will not write over it.")
            out = default_doc()
            out.update(data)
            out["items"] = [_repair(i) for i in out["items"] if isinstance(i, dict)]
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            body = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)   # fails before the file is touched
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".alarmflow-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


def _num(value, lo: int, hi: int, default: int = 0) -> int:
    """A stored number, kept inside lo..hi (a hand-edited or old file may hold anything)."""
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(n, lo), hi)


_MS_HI = 253402300799000             # 9999-12-31 in epoch ms


def _repair(item: dict) -> dict:
    """One stored item made safe to read: text where text belongs, numbers in range."""
    i = dict(item)
    i["id"] = str(i.get("id") or f"a{_uid()}")
    i["desc"] = str(i.get("desc") or "Timer")[:200]
    i["mode"] = "alarm" if i.get("mode") == "alarm" else "timer"
    i["status"] = i.get("status") if i.get("status") in ACTIVE + ("stopped",) else "stopped"
    for k, hi in (("tot", MAX_TIMER_S * 2), ("rem", MAX_TIMER_S * 2)):
        i[k] = _num(i.get(k), 0, hi)
    for k in ("ts", "targetMs"):
        if i.get(k) is not None:
            i[k] = _num(i.get(k), 0, _MS_HI)
    return i


# ------------------------------------------------------------- the logic
# From app_data.py:384-618 (alarm_*). v1 kept ``rem`` and let the page count it down; here the
# server is the clock: a running timer is due at ts + tot seconds, an alarm at targetMs.

def due_ms(item: dict) -> int | None:
    """When a running item rings (epoch ms), or None if it is not running."""
    if item.get("status") != "running":
        return None
    if item.get("mode") == "alarm":
        return int(item.get("targetMs") or 0)
    return int(item.get("ts") or 0) + int(item.get("tot") or 0) * 1000


def left(item: dict, now_ms: int) -> int:
    """Seconds left on an item."""
    if item.get("status") == "paused":
        return int(item.get("rem") or 0)
    due = due_ms(item)
    return max(0, (due - now_ms + 999) // 1000) if due is not None else 0


def next_repeat(item: dict, now: datetime) -> int | None:
    """The next ring of a repeating alarm (epoch ms), from the v1 page's nextRecurrence
    (dashboard/public/alarm-app.html:427-444)."""
    repeat = item.get("repeat") or "none"
    if repeat == "none" or not item.get("targetMs"):
        return None
    first = datetime.fromtimestamp(int(item["targetMs"]) / 1000)
    # The alarm's own time of day, kept on the item since 2.1.0, so a snooze never moves the schedule.
    hour, minute = _num(item.get("hour", first.hour), 0, 23), _num(item.get("minute", first.minute), 0, 59)
    days = {"daily": range(7), "weekdays": range(5), "weekends": (5, 6),
            "weekly": (_num(item.get("weekday", first.weekday()), 0, 6),)}.get(repeat)
    if days is None:
        return None
    for d in range(0, 8):
        t = (now + timedelta(days=d)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t > now and t.weekday() in days:
            return int(t.timestamp() * 1000)
    return None


class AlarmFlow:
    def __init__(self, store: Store, notify=None, ring=None) -> None:
        self.store = store
        self.notify = notify or (lambda title, text: False)
        self.ring = ring or (lambda sound: False)      # the speaker beep (``Speaker``); silent by default
        self._beeped = 0

    def _add(self, item: dict) -> None:
        with self.store.lock:
            data = self.store.load()
            data["items"].append(item)
            self.store.save(data)

    def create_timer(self, desc: str = "Timer", hours: float = 0, minutes: float = 0, seconds: float = 0,
                     sound: str = "chime", duration: str = "") -> str:
        desc = _text(desc, "desc") or "Timer"
        sound = _text(sound, "sound", "chime")
        duration = _text(duration, "duration")
        if duration:
            total = parse_duration(duration)
            if total is None:
                return f"I could not read '{duration}' as a length. Say it like 20 minutes or 1 hour 30 minutes."
        else:
            total = round(_amount(hours, "hours", MAX_TIMER_S // 3600) * 3600
                          + _amount(minutes, "minutes", MAX_TIMER_S // 60) * 60
                          + _amount(seconds, "seconds", MAX_TIMER_S))
        if total <= 0:
            return "A timer needs a length: some hours, minutes or seconds."
        if total > MAX_TIMER_S:
            return "A timer can run for at most 7 days. Use Calendar for anything further off."
        self._add({"id": f"a{_uid()}", "mode": "timer", "desc": desc, "tot": total, "rem": total,
                   "sound": sound if sound in SOUNDS else "chime", "status": "running", "source": "puffpai",
                   "ts": _ms(), "targetMs": None})
        return f"Timer set: {desc}, {say_span(total)}. It rings at {clock(_ms() + total * 1000)}."

    def create_alarm(self, desc: str = "Alarm", hour: int | None = None, minute: int = 0, sound: str = "chime",
                     repeat: str = "none", time: str = "") -> str:
        desc = _text(desc, "desc") or "Alarm"
        sound = _text(sound, "sound", "chime")
        said = _text(time, "time")
        repeat = _text(repeat, "repeat", "none").lower()
        repeat = _REPEAT_WORDS.get(repeat, repeat)
        if repeat not in REPEATS:
            return "Repeat is once, daily, weekdays, weekends or weekly."
        if said:
            got = parse_clock(said)
            if got is None:
                return f"I could not read '{said}' as a time of day. Say it like 7:30 AM or 18:30."
            hour, minute = got
        elif hour is None:
            return "An alarm needs a time, like 7:30 AM."
        try:
            hour = _whole(hour, "hour", 0, 23)
            minute = _whole(minute or 0, "minute", 0, 59)
        except ValueError:
            return "An alarm needs an hour from 0 to 23 and a minute from 0 to 59."
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        item = {"id": f"a{_uid()}", "mode": "alarm", "desc": desc, "tot": 0, "rem": 0,
                "sound": sound if sound in SOUNDS else "chime", "status": "running", "source": "puffpai",
                "ts": _ms(), "targetMs": int(target.timestamp() * 1000), "repeat": repeat,
                "hour": hour, "minute": minute, "weekday": target.weekday()}
        if repeat != "none":
            nxt = next_repeat(item, now - timedelta(seconds=1))
            item["targetMs"] = nxt or item["targetMs"]
            item["weekday"] = datetime.fromtimestamp(item["targetMs"] / 1000).weekday()
        item["tot"] = item["rem"] = max(0, (item["targetMs"] - _ms()) // 1000)
        self._add(item)
        day = day_word(item["targetMs"], now)
        weekday = f"{datetime.fromtimestamp(item['targetMs'] / 1000):%A}"
        again = {"none": "", "weekly": f", then every {weekday}"}.get(repeat, f", then {repeat}")
        return f"Alarm set: {item['desc']}, ringing at {clock(item['targetMs'])} {day}{again}."

    def list(self) -> str:
        items = [i for i in self.store.load()["items"] if i.get("status") in ACTIVE]
        if not items:
            return "No timers or alarms are set."
        now = _ms()
        lines = []
        for i in items:
            if i["status"] == "ringing":
                lines.append(f"{i['desc']}: ringing now")
            elif i.get("mode") == "alarm":
                again = "" if (i.get("repeat") or "none") == "none" else f", {i['repeat']}"
                lines.append(f"{i['desc']}: alarm at {clock(due_ms(i) or 0)} {day_word(due_ms(i) or 0)}{again}")
            else:
                paused = " (paused)" if i["status"] == "paused" else ""
                lines.append(f"{i['desc']}: {span(left(i, now))} left{paused}")
        return f"{_things(len(items))}:\n" + "\n".join(lines)

    @staticmethod
    def _find(data: dict, item: str, active_only: bool = True) -> tuple[dict | None, str | None]:
        """Voice-friendly lookup (v1 _alarm_find, app_data.py:479): an exact id or name, then one
        unambiguous part of a name. Returns (item, None) or (None, a plain question)."""
        ident = str(item or "").lower().strip()
        active = [i for i in data["items"] if i.get("status") in ACTIVE]
        for pool in ([active] if active_only else [active, data["items"]]):    # a set item wins over an old one
            for i in pool:
                if i["id"] == item or i["desc"].lower() == ident:
                    return i, None
            if len(ident) >= 3:
                hits = [i for i in pool if ident in i["desc"].lower() or i["desc"].lower() in ident]
                if len(hits) == 1:
                    return hits[0], None
                if len(hits) > 1:
                    return None, f"Several match '{item}': {', '.join(i['desc'] for i in hits[:4])}. Which one?"
        if not ident and len(active) == 1:
            return active[0], None
        if not ident and active:
            return None, f"Which one: {', '.join(i['desc'] for i in active[:4])}?"
        return None, f"No timer or alarm matches '{item}'."

    def _change(self, item: str, fn, active_only: bool = True) -> str:
        item = _text(item, "item")
        with self.store.lock:
            data = self.store.load()
            found, err = self._find(data, item, active_only)
            if err:
                return err
            said = fn(found, data)
            if said.startswith("!"):
                return said[1:]
            self.store.save(data)
            return said

    def snooze(self, item: str = "", minutes: int = 9) -> str:
        minutes = _whole(minutes or 9, "minutes", 1, MAX_SHIFT_MIN)

        def go(i, _data):
            now = _ms()
            if i["mode"] == "alarm":
                i["targetMs"] = now + minutes * 60_000
            else:
                i["ts"], i["tot"] = now, minutes * 60
            i["rem"], i["status"] = minutes * 60, "running"
            i["snoozed"] = int(i.get("snoozed", 0)) + 1
            i.pop("rangAt", None)
            return f"Snoozed {i['desc']} for {_plural(minutes, 'minute')}. It rings again at {clock(now + minutes * 60_000)}."
        return self._change(item, go)

    def add_time(self, item: str = "", minutes: int = 1) -> str:
        """The v1 page's +1m, +5m and -1m buttons (alarm-app.html:448-457)."""
        minutes = _whole(minutes or 0, "minutes", -MAX_SHIFT_MIN, MAX_SHIFT_MIN)

        def go(i, _data):
            if i["status"] == "ringing":
                if minutes <= 0:
                    return f"!{i['desc']} is ringing. Snooze or stop it."
                return "!" + self.snooze(i["id"], minutes)
            if i["status"] == "paused":
                i["rem"] = min(MAX_TIMER_S, max(0, int(i.get("rem") or 0) + minutes * 60))
            elif i["mode"] == "alarm":
                i["targetMs"] = int(i["targetMs"]) + minutes * 60_000
            else:
                i["tot"] = min(MAX_TIMER_S * 2, max(0, int(i["tot"]) + minutes * 60))
            more = "Added" if minutes >= 0 else "Took off"
            return f"{more} {_plural(abs(minutes), 'minute')} on {i['desc']}."
        return self._change(item, go)

    def pause(self, item: str = "") -> str:
        def go(i, _data):
            if i["mode"] != "timer":
                return "!Alarms ring at a set time, so they can't be paused. Snooze or stop it instead."
            if i["status"] != "running":
                return f"!{i['desc']} isn't running."
            i["rem"], i["status"], i["ts"] = left(i, _ms()), "paused", None
            return f"Paused {i['desc']} with {span(i['rem'])} left."
        return self._change(item, go)

    def resume(self, item: str = "") -> str:
        def go(i, _data):
            if i["status"] != "paused":
                return f"!{i['desc']} isn't paused."
            i["tot"], i["ts"], i["status"] = int(i.get("rem") or 0), _ms(), "running"
            return f"Resumed {i['desc']}, {span(i['tot'])} left."
        return self._change(item, go)

    def _stop(self, i: dict) -> str:
        """Stop one item. A ringing repeating alarm moves to its next day instead (the v1 page's dismiss);
        one that is only set is cancelled."""
        nxt = None
        if i.get("mode") == "alarm" and i.get("status") == "ringing":
            rang = datetime.fromtimestamp(int(i.get("targetMs") or 0) / 1000)
            nxt = next_repeat(i, max(datetime.now(), rang))     # always after the ring it stops
        i.pop("rangAt", None)
        if nxt:
            i["targetMs"], i["status"] = nxt, "running"
            i["tot"] = i["rem"] = max(0, (nxt - _ms()) // 1000)
            return f"Stopped {i['desc']}. It rings again at {clock(nxt)} {datetime.fromtimestamp(nxt / 1000):%A}."
        i["status"] = "stopped"
        return f"Stopped {i['desc']}."

    def dismiss(self, item: str = "") -> str:
        ident = str(item or "").strip()
        if ident.lower() in ("all", "everything", "all of them", "all timers", "all alarms"):
            return self.dismiss_all()
        if not ident:
            with self.store.lock:
                data = self.store.load()
                ringing = [i for i in data["items"] if i.get("status") == "ringing"]
                if len(ringing) == 1:
                    said = self._stop(ringing[0])
                    self.store.save(data)
                    return said
                if len(ringing) > 1:
                    return f"Several are ringing: {', '.join(i['desc'] for i in ringing[:4])}. Which one?"
        return self._change(item, lambda i, _d: self._stop(i))

    def dismiss_all(self) -> str:
        with self.store.lock:
            data = self.store.load()
            hits = [i for i in data["items"] if i.get("status") in ACTIVE]
            if not hits:
                return "Nothing is set, so there is nothing to stop."
            for i in hits:
                i["status"] = "stopped"
                i.pop("rangAt", None)
            self.store.save(data)
        return f"Stopped {_things(len(hits))}."

    def delete(self, item: str = "") -> str:
        if len(str(item or "").strip()) < 2:
            return "I need the timer or alarm's name to delete it."

        def go(i, data):
            data["items"] = [x for x in data["items"] if x["id"] != i["id"]]
            return f"Deleted {i['desc']}."
        return self._change(item, go, active_only=False)

    # the ringing clock
    def tick(self, now_ms: int | None = None) -> list[str]:
        """Mark every due item as ringing and tell the owner once per ring. Returns the ids that rang."""
        now_ms = _ms() if now_ms is None else now_ms
        rang = []
        with self.store.lock:
            data = self.store.load()
            for i in data["items"]:
                due = due_ms(i)
                if due is not None and due <= now_ms:
                    i["status"], i["rem"], i["rangAt"] = "ringing", 0, now_ms
                    rang.append(i)
            if rang:
                self.store.save(data)
            loud = [i for i in data["items"] if i.get("status") == "ringing" and i.get("sound") != "none"
                    and 0 <= now_ms - int(i.get("rangAt") or 0) < RING_FOR_MS]
        if loud and now_ms - self._beeped >= RING_EVERY_MS:      # heard with no page open (``Speaker``)
            self._beeped = now_ms
            self.ring(loud[0].get("sound") or "chime")
        for i in rang:
            late = (now_ms - (i.get("targetMs") if i.get("mode") == "alarm" else
                              int(i.get("ts") or 0) + int(i.get("tot") or 0) * 1000)) // 60_000
            what = "Alarm" if i.get("mode") == "alarm" else "Timer"
            text = f"{what}: {i['desc']}." + (f" It was due {_plural(late, 'minute')} ago, while BuildBot was off."
                                              if late >= 2 else "")
            self.notify(i["desc"], text)
        return [i["id"] for i in rang]

    def widget_running(self) -> dict:
        now = _ms()
        items = sorted((i for i in self.store.load()["items"] if i.get("status") in ACTIVE),
                       key=lambda i: (i["status"] != "ringing", due_ms(i) or now + left(i, now) * 1000))
        out = []
        for i in items:
            if i["status"] == "ringing":
                out.append({"text": f"{i['desc']}: ringing"})
            elif i.get("mode") == "alarm":
                out.append({"text": f"{i['desc']}: {clock(due_ms(i) or 0)}"})
            else:
                out.append({"text": f"{i['desc']}: {span(left(i, now))} left" + (" (paused)" if i["status"] == "paused" else "")})
        return {"items": out}

    def view(self) -> dict:
        """The document for the page, with each item's seconds left worked out now."""
        data = self.store.load()
        now = _ms()
        data["now"] = now
        data["items"] = [{**i, "left": left(i, now), "due": due_ms(i), "day": day_word(due_ms(i)) if due_ms(i) else ""}
                         for i in data["items"] if i.get("status") in ACTIVE]
        return data


class Clock(threading.Thread):
    """The ringing clock: one tick a second. One bad tick never stops it, but it counts them:
    after FAILED_TICKS failures in a row, /health answers 503 (nothing would ring)."""

    FAILED_TICKS = 3

    def __init__(self, app: AlarmFlow, every_s: float = 1.0) -> None:
        super().__init__(name="alarmflow-clock", daemon=True)
        self.app, self.every_s, self.halt = app, every_s, threading.Event()
        self.problem = ""
        self.failures = 0

    def run(self) -> None:
        while not self.halt.wait(self.every_s):
            try:
                self.app.tick()
                self.problem, self.failures = "", 0
            except Unreadable as exc:
                self.problem, self.failures = str(exc), self.failures + 1
            except Exception as exc:  # noqa: BLE001 - one bad tick never stops the clock
                self.problem, self.failures = f"The ringing clock hit a problem ({type(exc).__name__}).", self.failures + 1

    def trouble(self) -> str:
        """Why nothing would ring, or "" when the clock is fine."""
        if not self.is_alive():
            return "The ringing clock stopped."
        if self.failures >= self.FAILED_TICKS:
            return self.problem or "The ringing clock keeps failing."
        return ""


class Speaker:
    """Beeps through this computer's speakers with ``winsound`` (Windows, standard library), so a ringing
    item is heard with no page open. Elsewhere it does nothing. One pattern at a time, off the clock thread."""

    PATTERNS = {"chime": ((523, 150), (659, 150), (784, 300)), "alert": ((880, 120), (440, 120)) * 2,
                "gentle": ((392, 600),), "bell": ((440, 100), (554, 100), (659, 400))}

    def __init__(self) -> None:
        try:
            import winsound
        except ImportError:
            winsound = None
        self._beep = getattr(winsound, "Beep", None)
        self._busy = threading.Lock()

    def __call__(self, sound: str) -> bool:
        tones = self.PATTERNS.get(sound, self.PATTERNS["chime"])
        if self._beep is None or sound == "none" or not self._busy.acquire(blocking=False):
            return False

        def play() -> None:
            try:
                for freq, ms in tones:
                    self._beep(freq, ms)
            except RuntimeError:         # no sound device: the card and the page still show the ring
                pass
            finally:
                self._busy.release()
        threading.Thread(target=play, name="alarmflow-beep", daemon=True).start()
        return True


class Notifier:
    """Tells the owner through the Core API ``notify`` route, with this package's token (02)."""

    def __init__(self, api: str | None, token: str | None) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        # The Core is on this computer: never send this call through the Bridge proxy.
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def __call__(self, title: str, text: str) -> bool:
        if not self.api or not self.token:
            return False
        body = json.dumps({"title": title[:120], "text": text[:600], "sound": True, "speak": True,
                           "urgency": "high"}).encode()
        req = urllib.request.Request(self.api + "/v1/notify", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with self._open(req, timeout=10) as resp:
                return resp.status in (200, 201)
        except (urllib.error.URLError, OSError, ValueError):
            return False


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_ITEM = _p("string", "The timer or alarm's name, like pasta. Leave empty when only one is set or ringing.")
_SOUND = {"type": "string", "enum": list(SOUNDS), "description": "The sound. Default chime."}
TOOLS = [
    ("create_timer", "Start a countdown timer, like 20 minutes or an hour and a half, up to 7 days. It rings on this computer.",
     {"desc": _p("string", "What it is for, like pasta."),
      "duration": _p("string", "The length as said, like 20 minutes, an hour and a half or 1h30m. Use this or the numbers."),
      "hours": _p("number", "Hours, like 1.5. Default 0."), "minutes": _p("number", "Minutes. Default 0."),
      "seconds": _p("number", "Seconds. Default 0."), "sound": _SOUND}, []),
    ("create_alarm", "Set an alarm for a time of day, like 6:30 AM, once or repeating daily, on weekdays, on weekends or weekly.",
     {"desc": _p("string", "What it is for, like wake up."),
      "time": _p("string", "The time as said, like 6:30 AM, 7pm, 18:30 or noon. Use this or hour and minute."),
      "hour": _p("integer", "Hour 0 to 23, like 6 for 6 AM or 18 for 6 PM."),
      "minute": _p("integer", "Minute 0 to 59. Default 0."), "sound": _SOUND,
      "repeat": {"type": "string", "enum": list(REPEATS),
                 "description": "none (once), daily, weekdays, weekends, or weekly (the same day each week). Default none."}},
     []),
    ("list", "List the timers and alarms that are set, with the time left on each.", {}, []),
    ("snooze", "Snooze a ringing timer or alarm for some minutes, up to 24 hours.",
     {"item": _ITEM, "minutes": _p("integer", "How long. Default 9.")}, []),
    ("add_time", "Add minutes to a running timer or alarm, or take some off with a negative number, up to 24 hours either way.",
     {"item": _ITEM, "minutes": _p("integer", "Minutes to add, like 5, or -1 to take one off.")}, ["minutes"]),
    ("pause", "Pause a running timer.", {"item": _ITEM}, []),
    ("resume", "Start a paused timer again.", {"item": _ITEM}, []),
    ("dismiss", "Stop a ringing timer or alarm, or cancel one that is set. A ringing repeating alarm moves to its next day.",
     {"item": _ITEM}, []),
    ("dismiss_all", "Stop and cancel every timer and alarm.", {}, []),
    ("delete", "Delete a timer or alarm for good, by its name.", {"item": _p("string", "The name, like pasta.")}, ["item"]),
]
CHANGES = ["create_timer", "create_alarm", "snooze", "add_time", "pause", "resume", "dismiss", "dismiss_all", "delete"]
TOOL_NAMES = {t[0] for t in TOOLS}


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in ("delete", "dismiss_all")}}
            for n, d, props, req in TOOLS]


def run_tool(app: AlarmFlow, name: str, args: dict) -> str:
    if name not in TOOL_NAMES:
        raise KeyError(name)
    if not isinstance(args, dict):
        raise TypeError("send the arguments as one JSON object")
    return getattr(app, name)(**args)


def mcp_answer(app: AlarmFlow, msg: dict) -> dict | None:
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
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - a plain answer, never a dropped connection
            return ok({"content": [{"type": "text", "text": f"AlarmFlow hit a problem with {name} ({type(exc).__name__}). "
                                    "Nothing was saved."}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"AlarmFlow does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AlarmFlow/2"
    app: AlarmFlow
    clock: Clock
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
            raise ValueError("That is too big for AlarmFlow.")
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
        self._json(401, {"error": "AlarmFlow needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

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
                self._json(500, {"error": f"AlarmFlow hit a problem ({type(exc).__name__}). Nothing was saved."})

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

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
                try:
                    self.app.store.load()
                    trouble = self.clock.trouble()
                except Unreadable as exc:
                    trouble = str(exc)
                if not trouble:
                    return self._send(200, b"ok", "text/plain")
                return self._send(503, trouble.encode("utf-8"), "text/plain")
            if path == "/widget/running":
                return self._json(200, self.app.widget_running())
            if path == "/data":
                return self._json(200, self.app.view())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"],
                                        "problem": self.clock.problem})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "AlarmFlow has no page here."})

    def _post(self):
        if self._refused():
            return
        self._drain()                              # read before any reply, the 404 and the errors too
        path = self._path()
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
        self._json(404, {"error": "AlarmFlow has no page here."})

    def do_PUT(self):
        self._raw, self._too_big = None, False
        if self._refused():
            return
        self._drain()                              # read before the reply (WinError 10053 otherwise)
        self._send(405, b"", "text/plain")

    do_DELETE = do_PUT


def make_server(port: int, data: Path | str, token: str | None, api: str | None,
                every_s: float = 1.0, beep: bool = False) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only, with its ringing clock started (``beep``: ring the speakers
    too; ``main`` turns it on). Call serve_forever(); stop with shutdown() and server.clock.halt.set()."""
    app = AlarmFlow(Store(data), Notifier(api, token), Speaker() if beep else None)
    ticker = Clock(app, every_s)
    handler = type("AlarmFlowHandler", (Handler,), {"app": app, "clock": ticker, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.app, server.clock = app, ticker
    ticker.start()
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AlarmFlow for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts AlarmFlow with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), os.environ.get("PUFFPAI_API"),
                         beep=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.clock.halt.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
