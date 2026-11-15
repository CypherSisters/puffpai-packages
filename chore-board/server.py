"""Chore Board: a household chore board, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/chore-board/
- ``/health``       "ok"
- ``/data``         the saved document (GET), ``/updated`` its stamp
- ``/board``        what is due today and this week, who each chore is on, and the tally (GET)
- ``/api/add``, ``/api/update``, ``/api/done``, ``/api/remove``,
  ``/api/person/add``, ``/api/person/rename``, ``/api/person/remove``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/chore-board/, which adds it.

The board is one JSON document in PUFFPAI_DATA (``chores.json``:
``{people: [...], chores: [...], done: [...], last_updated}``). Writes replace the
whole file at once (temp file, then rename). A file that exists but will not read
is never written over. How a chore comes due and how a rotation moves: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
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
NAME = "chore-board"
VERSION = "1.0.1"
DOC = "chores.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_DRAIN = 64 * 1024 * 1024            # a body over MAX_BODY is read and dropped up to this much...
DRAIN_WAIT_S = 20                       # ...within this long, then answered 400
BODY_WAIT_S = 5                         # a whole body must arrive within this long
REFUSED_DRAIN = 64 * 1024               # a request without the token: at most this much is read...
REFUSED_WAIT_S = 2                      # ...within this long, then 401
PEOPLE_DEADLINE_S = 2.0                 # the whole fetch of the Core's people list
MAX_PEOPLE = 50
MAX_CHORES = 500
MAX_DONE = 10000
KEEP_DONE_DAYS = 400                    # the tally reads 30 days; older records are dropped
WEEK_DAYS = 7                           # "this week" is today and the 6 days after it
LATE_BACK_DAYS = 365                    # how far back a chore may be marked done

REPEATS = {"once": 0, "daily": 1, "weekly": 7}
REPEAT_WORDS = {"one time": "once", "one-off": "once", "none": "once", "never": "once", "no": "once",
                "every day": "daily", "day": "daily", "each day": "daily",
                "every week": "weekly", "week": "weekly", "each week": "weekly"}
NOBODY = ("", "nobody", "no one", "none", "anyone")


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _whole(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or (isinstance(value, str) and len(value) > 40):
        raise ValueError(f"{name} must be a whole number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a whole number") from None
    if x != x or x != int(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a whole number from {lo} to {hi}")
    return int(x)


def _day(value, name: str, today: date) -> date | None:
    """A date as YYYY-MM-DD, or the words today, tomorrow, yesterday; None when not given."""
    text = _text(value, name, 40).lower()
    if not text:
        return None
    words = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if text in words:
        return today + timedelta(days=words[text])
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or today, tomorrow or yesterday") from None


def _repeat(value, every_days) -> tuple[str, int]:
    """(repeat, every_days) from once, daily, weekly, every N days, or every_days alone."""
    text = _text(value, "repeat", 40).lower()
    text = REPEAT_WORDS.get(text, text)
    if not text and every_days in (None, ""):
        return "once", 0
    m = _every(text) if text else None
    if text in REPEATS:
        if every_days not in (None, "") and text != "once":
            n = _whole(every_days, "every_days", 1, 365)
            if n != REPEATS[text]:
                raise ValueError(f"{text} repeats every {REPEATS[text]} days; for every {n} days say repeat every")
        return text, REPEATS[text]
    if m is not None or not text:
        n = m if m else every_days
        if n in (None, ""):
            raise ValueError("say how many days with every_days, like every 3 days")
        n = _whole(n, "every_days", 1, 365)
        return ("daily", 1) if n == 1 else ("weekly", 7) if n == 7 else ("every", n)
    raise ValueError("repeat is once, daily, weekly, or every N days")


def _every(text: str) -> str | None:
    """'every', 'every 3', 'every 3 days', 'every3days': the number as text ('' when none); None when it is not
    that shape. Plain string steps, no pattern (the text is at most 40 characters and single spaced)."""
    if not text.startswith("every"):
        return None
    rest = text[5:].strip()
    for end in ("days", "day"):
        if rest.endswith(end):
            rest = rest[:-len(end)].strip()
            break
    if rest and not (rest.isascii() and rest.isdigit()):
        return None
    return rest


def _words(text) -> list[str]:
    """The words of a name: letters and digits of any script, lower case with accents folded (é is e), so
    'Jose' finds 'José'; anything else separates them."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch if ch.isalnum() else " " for ch in text if not unicodedata.combining(ch)).casefold().split()


def _repeat_words(c: dict) -> str:
    r = c.get("repeat")
    if r == "once":
        return "once"
    if r in ("daily", "weekly"):
        return r
    return f"every {_safe_int(c.get('every_days'), 1)} days"


def _safe_int(value, default: int) -> int:
    try:
        return int(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError):
        return default


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _when(d: date, today: date) -> str:
    gap = (d - today).days
    if gap == 0:
        return "today"
    if gap == 1:
        return "tomorrow"
    return f"{d.strftime('%a')} {d.isoformat()}"


def _squash(text) -> str:
    """Words compared with case and spaces ignored."""
    return "".join(str(text or "").split()).lower()


def _pick(items: list[dict], key: str, kind: str, extra=None, said: list | None = None) -> dict | None:
    """The one item whose id is *key*, or whose name is *key* (case and spaces ignored), or else the one item
    where every word of *key* is a whole word of its name or the start (3 letters or more) of one; never letters
    inside a word. A match that is not exact adds "Using '<name>'." to *said*. None when nothing matches. More
    than one match changes nothing: a ValueError lists them so the owner can say which."""
    for x in items:
        if x.get("id") == key:
            return x
    want = _squash(key)
    found = [x for x in items if _squash(x.get("name")) == want]
    partial = False
    if not found and want:
        given = _words(key)
        found = [x for x in items if given and all(any(w == n or (len(w) >= 3 and n.startswith(w))
                                                       for n in _words(x.get("name"))) for w in given)]
        partial = True
    if len(found) == 1:
        if partial and said is not None:
            said.append(f"Using {str(found[0].get('name'))!r}.")
        return found[0]
    if not found:
        return None
    names = [str(x.get("name")) for x in found]
    if len(set(n.lower() for n in names)) < len(names):        # the same name twice: the ids tell them apart
        names = [f"{x.get('name')} (id {x.get('id')}{', ' + extra(x) if extra else ''})" for x in found]
    raise ValueError(f"More than one {kind} matches {key!r}: {', '.join(names)}. Say which.")


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


MARKER = "chore-board"
SHAPE = ("people", "chores", "done", "last_updated")      # the keys this version writes, besides the marker


def default_doc() -> dict:
    return {"app": MARKER, "people": [], "chores": [], "done": [], "last_updated": ""}


class Store:
    """One JSON document with a stamp and this app's marker."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        """The board. No file is a fresh start. An empty, broken or foreign file raises Unreadable and is never
        written over. A file with the marker, or (from before the marker) with exactly this version's keys, loads."""
        with self.lock:
            if not self.path.exists():
                return default_doc()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({type(exc).__name__}). Chore Board will not write "
                                 f"over it; move it away or fix it.") from None
            ours = isinstance(data, dict) and (
                data.get("app") == MARKER or ("app" not in data and set(data) == set(SHAPE)))
            if not ours or not all(isinstance(data.get(k, []), list) for k in ("people", "chores", "done")):
                raise Unreadable(f"{self.path} is not this app's file: it is not a Chore Board file. Chore Board "
                                 f"will not write over it; move it away to start a new board.")
            out = default_doc()
            out.update(data)
            out["app"] = MARKER
            for k in ("people", "chores", "done"):
                out[k] = [x for x in out[k] if isinstance(x, dict)]
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            self.load()                                    # never write over a file that is not ours
            used = {d.get("by") for d in data["done"]}
            for c in data["chores"]:
                used.update(c.get("assigned") or [])
            # people from the Core's list are kept here only once a chore or a done record points at them
            data["people"] = [{k: v for k, v in p.items() if k != "core_only"} for p in data["people"]
                              if not p.get("core_only") or p.get("id") in used]
            data["app"] = MARKER
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".chores-", suffix=".tmp")
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
    """The Core's shared people list (``GET /v1/people``), read with this package's token. Only PUFFPAI_API on
    this computer (127.0.0.1, localhost or ::1, over http) is ever asked; any other address is never dialled.
    Adapted from Emergency Plan's fetch."""

    def __init__(self, api: str | None, token: str | None, ttl_s: float = 60, fail_ttl_s: float = 30) -> None:
        self.token, self.ttl_s, self.fail_ttl_s = token or "", ttl_s, fail_ttl_s
        self.where = None
        parts = urlsplit((api or "").strip())
        try:
            if (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1") and parts.port
                    and not parts.username and not parts.password and not parts.query
                    and all(ch.isalnum() or ch in "/-_." for ch in parts.path)):
                self.where = (parts.hostname, parts.port, parts.path.rstrip("/"))
        except ValueError:
            self.where = None
        self._cache: tuple[float, float, list[dict]] = (-1e9, 0, [])     # (when, how long it holds, list)
        self._busy = threading.Lock()

    def list(self) -> list[dict]:
        """The list, from the cache while it holds (60 s, or 30 s after a failure). While one request asks the
        Core, any other gets the cached list at once. Never call this while holding the data lock."""
        at, hold, got = self._cache
        if not (self.where and self.token) or time.monotonic() - at <= hold:
            return got
        if not self._busy.acquire(blocking=False):
            return got
        try:
            try:
                got, hold = self._fetch(), self.ttl_s
            except Exception:  # noqa: BLE001 - no Core, too slow, a redirect, or nonsense: the board's own list
                got, hold = [], self.fail_ttl_s
            self._cache = (time.monotonic(), hold, got)
            return got
        finally:
            self._busy.release()

    def _fetch(self) -> list[dict]:
        """GET /v1/people, all of it within PEOPLE_DEADLINE_S seconds or an error. Any answer but 200 (a redirect
        too) is an error; nothing is followed."""
        host, port, prefix = self.where
        if any(ch in self.token for ch in "\r\n"):
            raise ValueError("the token can not go in a header")
        end = time.monotonic() + PEOPLE_DEADLINE_S
        buf = bytearray()
        with socket.create_connection((host, port), timeout=PEOPLE_DEADLINE_S) as s:
            hosthead = f"[{host}]" if ":" in host else host
            s.sendall((f"GET {prefix}/v1/people HTTP/1.0\r\nHost: {hosthead}:{port}\r\nAuthorization: Bearer "
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
                top, sep, rest = bytes(buf).partition(b"\r\n\r\n")
                size = next((h.partition(b":")[2].strip() for h in top.split(b"\r\n")[1:]
                             if h.partition(b":")[0].strip().lower() == b"content-length"), b"") if sep else b""
                if size.isdigit() and len(rest) >= int(size):
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
        out = []
        for p in data[:MAX_PEOPLE]:
            if isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"] and len(p["id"]) <= 80:
                name = p.get("name") if isinstance(p.get("name"), str) else p["id"]
                name = " ".join(name.split())[:60].replace(",", " ")
                if name:
                    out.append({"id": p["id"], "name": name})
        return out


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

class Board:
    """People, chores, and the record of what was done."""

    def __init__(self, store: Store, today=date.today, people: People | None = None) -> None:
        self.store = store
        self.today = today                  # a function, so a test can move the day
        self.core = people or People(None, None)

    # helpers
    def _doc(self, core: list[dict]) -> dict:
        """The saved board with the Core's people merged into its own list: a Core person with the same name as
        one of the board's own people is that person; the rest join the list (kept on save only once a chore
        points at them). *core* is fetched before the lock is taken."""
        doc = self.store.load()
        for c in core:
            own = next((p for p in doc["people"] if p.get("core_id") == c["id"]), None) or \
                next((p for p in doc["people"] if not p.get("core_id")
                      and str(p.get("name", "")).lower() == c["name"].lower()), None)
            if own:
                own["core_id"], own["name"] = c["id"], c["name"]
            else:
                doc["people"].append({"id": "core:" + c["id"], "name": c["name"], "core_id": c["id"],
                                      "core_only": True})
        return doc

    @staticmethod
    def _find_person(doc: dict, person, name: str = "person", said: list | None = None) -> dict:
        key = _text(person, name, 200)
        if not key:
            raise ValueError(f"say which {name}, by name")
        p = _pick(doc["people"], key, "person", said=said)
        if p is None:
            known = ", ".join(str(p.get("name")) for p in doc["people"]) or "nobody yet"
            raise ValueError(f"there is no person called {key!r} (the household: {known}); add them first")
        return p

    @staticmethod
    def _find_chore(doc: dict, chore, said: list | None = None) -> dict:
        key = _text(chore, "chore", 200)
        if not key:
            raise ValueError("say which chore, by its name")
        c = _pick(doc["chores"], key, "chore", lambda c: f"due {c.get('due') or 'never'}", said)
        if c is None:
            known = ", ".join(str(c.get("name")) for c in doc["chores"]) or "none yet"
            raise ValueError(f"there is no chore called {key!r} (the chores: {known})")
        return c

    def _assignees(self, doc: dict, assign_to, said: list | None = None) -> list[str]:
        """Person ids from one name, several names (a rotation, in that order), or nobody."""
        if assign_to is None:
            return []
        if isinstance(assign_to, list):
            names = assign_to
        elif isinstance(assign_to, str):
            if len(assign_to) > 2000:
                raise ValueError("assign_to is at most 2000 characters")
            names = []                          # split on commas, '&' and the word 'and'; plain string steps
            for part in assign_to.replace("&", ",").split(","):
                cur: list[str] = []
                for w in part.split():
                    if w.lower() == "and":
                        names.append(" ".join(cur))
                        cur = []
                    else:
                        cur.append(w)
                names.append(" ".join(cur))
        else:
            raise ValueError("assign_to is a name, or names separated by commas")
        if len(names) > MAX_PEOPLE:
            raise ValueError(f"a rotation is at most {MAX_PEOPLE} people")
        names = [n for n in names if not (isinstance(n, str) and n.strip().lower() in NOBODY)]
        ids: list[str] = []
        for n in names:
            pid = self._find_person(doc, n, "assign_to", said)["id"]
            if pid in ids:
                raise ValueError("a rotation names each person once")
            ids.append(pid)
        return ids

    @staticmethod
    def _names(doc: dict) -> dict:
        return {p.get("id"): str(p.get("name", "")) for p in doc["people"]}

    @staticmethod
    def _rotation(c: dict) -> list[str]:
        r = c.get("assigned")
        return [x for x in r if isinstance(x, str)] if isinstance(r, list) else []

    def _on(self, c: dict) -> str | None:
        """The id of the person this chore is on now, or None for nobody."""
        r = self._rotation(c)
        return r[_safe_int(c.get("turn"), 0) % len(r)] if r else None

    def _who(self, doc: dict, c: dict) -> str:
        names = self._names(doc)
        r = self._rotation(c)
        if not r:
            return "nobody yet"
        on = names.get(self._on(c), "someone")
        if len(r) == 1:
            return on
        return f"{on}'s turn; rotation {', '.join(names.get(x, '?') for x in r)}"

    def _line(self, doc: dict, c: dict, today: date) -> str:
        due = _as_date(c.get("due"))
        late = (today - due).days if due else 0
        note = f", {_plural(late, 'day')} late" if late > 0 else ""
        return f"{c.get('name')} ({self._who(doc, c)}), {_repeat_words(c)}{note}"

    def _due(self, doc: dict, today: date, days: int, person: dict | None = None) -> list[dict]:
        last = today + timedelta(days=days - 1)
        out = []
        for c in doc["chores"]:
            due = _as_date(c.get("due"))
            if due is None or c.get("finished") or due > last:
                continue
            if person is not None and self._on(c) != person.get("id"):
                continue
            out.append(c)
        return sorted(out, key=lambda c: (str(c.get("due")), str(c.get("name", "")).lower()))

    # tools
    def list_due(self, when=None, person=None) -> str:
        said: list[str] = []
        text = self._due_text(when, person, said)
        return " ".join(said + [text])

    def _due_text(self, when, person, said: list) -> str:
        when = _text(when, "when", 10, "today").lower().replace("this ", "")
        if when not in ("today", "week"):
            raise ValueError("when is today or week")
        doc = self._doc(self.core.list())
        today = self.today()
        who = self._find_person(doc, person, said=said) if person not in (None, "") else None
        whose = f" for {who['name']}" if who else ""
        if when == "today":
            items = self._due(doc, today, 1, who)
            if not items:
                return f"Nothing is due today{whose}."
            return (f"Due today ({today.isoformat()}){whose}, {_plural(len(items), 'chore')}:\n"
                    + "\n".join(f"- {self._line(doc, c, today)}" for c in items))
        items = self._due(doc, today, WEEK_DAYS, who)
        span = f"{today.isoformat()} to {(today + timedelta(days=WEEK_DAYS - 1)).isoformat()}"
        if not items:
            return f"Nothing is due this week ({span}){whose}."
        lines = []
        for c in items:
            due = max(_as_date(c.get("due")), today)
            lines.append(f"- {_when(due, today)}: {self._line(doc, c, today)}")
        return f"Due this week ({span}){whose}, {_plural(len(items), 'chore')}:\n" + "\n".join(lines)

    def add_chore(self, name, repeat=None, every_days=None, assign_to=None, due_on=None, notes=None) -> dict:
        name = _text(name, "name", 80)
        if not name:
            raise ValueError("a chore needs a name")
        rep, n = _repeat(repeat, every_days)
        today = self.today()
        due = _day(due_on, "due_on", today) or today
        notes = _text(notes, "notes", 300)
        core, said = self.core.list(), []
        with self.store.lock:
            doc = self._doc(core)
            if len(doc["chores"]) >= MAX_CHORES:
                raise ValueError(f"Chore Board holds at most {MAX_CHORES} chores")
            c = {"id": "c" + secrets.token_hex(4), "name": name, "repeat": rep, "every_days": n,
                 "due": due.isoformat(), "assigned": self._assignees(doc, assign_to, said), "turn": 0,
                 "notes": notes, "finished": "", "added": _now()}
            doc["chores"].append(c)
            self.store.save(doc)
        return {"id": c["id"], "text": " ".join(said + [f"Added {name}, {_repeat_words(c)}, first due "
                                                        f"{_when(due, today)}, on {self._who(doc, c)}."])}

    def change_chore(self, chore, name=None, repeat=None, every_days=None, assign_to=None, due_on=None,
                     notes=None) -> str:
        today = self.today()
        core, said = self.core.list(), []
        with self.store.lock:
            doc = self._doc(core)
            c = self._find_chore(doc, chore, said)
            changed = []
            if name is not None:
                c["name"] = _text(name, "name", 80) or c.get("name", "")
                changed.append("name")
            if repeat not in (None, "") or every_days not in (None, ""):
                c["repeat"], c["every_days"] = _repeat(repeat, every_days)
                if c["repeat"] != "once" and c.get("finished"):      # a finished one-time chore repeats again
                    c["finished"], c["due"] = "", today.isoformat()
                changed.append("repeat")
            if assign_to is not None:
                c["assigned"], c["turn"] = self._assignees(doc, assign_to, said), 0
                changed.append("who it is on")
            if due_on not in (None, ""):
                c["due"], c["finished"] = _day(due_on, "due_on", today).isoformat(), ""
                changed.append("due date")
            if notes is not None:
                c["notes"] = _text(notes, "notes", 300)
                changed.append("notes")
            if not changed:
                raise ValueError("say what to change: name, repeat, every_days, assign_to, due_on or notes")
            self.store.save(doc)
        due = _as_date(c.get("due"))
        when = "finished" if c.get("finished") else f"next due {_when(due, today)}" if due else "no due date"
        return " ".join(said + [f"Changed {c.get('name')} ({', '.join(changed)}): {_repeat_words(c)}, {when}, "
                                f"on {self._who(doc, c)}."])

    def mark_done(self, chore, by=None, on=None) -> str:
        today = self.today()
        day = _day(on, "on", today) or today
        if day > today:
            raise ValueError("on can not be a day still to come")
        if (today - day).days > LATE_BACK_DAYS:
            raise ValueError(f"on is at most {LATE_BACK_DAYS} days back")
        core, said = self.core.list(), []
        with self.store.lock:
            doc = self._doc(core)
            c = self._find_chore(doc, chore, said)
            if c.get("finished"):
                raise ValueError(f"{c.get('name')} was a one-time chore and is already done ({c['finished']})")
            turn_id = self._on(c)
            if by not in (None, ""):
                doer = self._find_person(doc, by, "by", said)
            elif turn_id is not None:
                doer = next((p for p in doc["people"] if p.get("id") == turn_id), None)
            else:
                doer = None
            if doer is None:
                raise ValueError(f"{c.get('name')} is on nobody yet, so say who did it with by")
            doc["done"].append({"chore": c.get("id"), "name": c.get("name"), "by": doer["id"],
                                "by_name": doer.get("name"), "on": day.isoformat(), "at": _now()})
            cut = (today - timedelta(days=KEEP_DONE_DAYS)).isoformat()
            doc["done"] = [d for d in doc["done"] if str(d.get("on", "")) >= cut][-MAX_DONE:]
            rot = self._rotation(c)
            if len(rot) > 1:
                c["turn"] = (_safe_int(c.get("turn"), 0) % len(rot) + 1) % len(rot)
            if c.get("repeat") == "once":
                c["finished"] = day.isoformat()
                after = "It was a one-time chore, so it is finished."
            else:
                gap = max(1, _safe_int(c.get("every_days"), 1))
                nxt = day + timedelta(days=gap)
                was = _as_date(c.get("due"))
                if was is not None and was > nxt and any(d.get("chore") == c.get("id") and str(d.get("on", "")) >
                                                         day.isoformat() for d in doc["done"]):
                    nxt = was                    # done again for an earlier day: never move the date back
                c["due"] = nxt.isoformat()
                after = f"Next due {_when(_as_date(c['due']), today)}"
                after += f", {self._names(doc).get(self._on(c), 'someone')}'s turn." if len(rot) > 1 else "."
            self.store.save(doc)
        return " ".join(said + [f"Marked {c.get('name')} done by {doer.get('name')} on {day.isoformat()}. {after}"])

    def list_people(self) -> str:
        doc = self._doc(self.core.list())
        if not doc["people"]:
            return "Nobody is on the chore board yet. Add people first."
        today = self.today()
        tally = self.tally_counts(doc, 7, today)
        lines = []
        for p in doc["people"]:
            on = [c for c in doc["chores"] if not c.get("finished") and self._on(c) == p.get("id")]
            on.sort(key=lambda c: str(c.get("due")))
            chores = ", ".join(f"{c.get('name')} (due {_when(_as_date(c.get('due')) or today, today)})"
                               for c in on) or "no chores now"
            lines.append(f"- {p.get('name')}: {chores}; {_plural(tally.get(p.get('id'), 0), 'chore')} "
                         f"done in the last 7 days")
        return f"{_plural(len(doc['people']), 'person', 'people')} in the household:\n" + "\n".join(lines)

    def add_person(self, name) -> dict:
        name = _text(name, "name", 60)
        if not name or "," in name:
            raise ValueError("a person needs a name, with no commas")
        if name.lower() in NOBODY or name.lower() == "and":
            raise ValueError(f"{name!r} can not be a person's name here")
        core = self.core.list()
        with self.store.lock:
            doc = self._doc(core)
            if any(str(p.get("name", "")).lower() == name.lower() for p in doc["people"]):
                raise ValueError(f"{name} is already in the household")
            if sum(1 for p in doc["people"] if not p.get("core_only")) >= MAX_PEOPLE:
                raise ValueError(f"Chore Board holds at most {MAX_PEOPLE} people")
            p = {"id": "p" + secrets.token_hex(4), "name": name, "added": _now()}
            doc["people"].append(p)
            self.store.save(doc)
        return {"id": p["id"], "text": f"Added {name} to the household."}

    def rename_person(self, person, name) -> str:
        name = _text(name, "name", 60)
        if not name or "," in name:
            raise ValueError("a person needs a name, with no commas")
        core, said = self.core.list(), []
        with self.store.lock:
            doc = self._doc(core)
            p = self._find_person(doc, person, said=said)
            if p.get("core_id") and any(c["id"] == p["core_id"] for c in core):
                raise ValueError(f"{p.get('name')} comes from the PuffPAI people list; change the name there")
            if any(q is not p and str(q.get("name", "")).lower() == name.lower() for q in doc["people"]):
                raise ValueError(f"{name} is already in the household")
            old, p["name"] = p.get("name"), name
            self.store.save(doc)
        return " ".join(said + [f"Renamed {old} to {name}."])

    @staticmethod
    def tally_counts(doc: dict, days: int, today: date) -> dict:
        start = (today - timedelta(days=days - 1)).isoformat()
        counts: dict = {}
        for d in doc["done"]:
            if start <= str(d.get("on", "")) <= today.isoformat():
                counts[d.get("by")] = counts.get(d.get("by"), 0) + 1
        return counts

    def tally(self, doc: dict | None = None) -> dict:
        """Per span, per person: how many, and which chores."""
        doc = doc or self.store.load()
        today = self.today()
        names = self._names(doc)
        out = {}
        for days in (7, 30):
            start = (today - timedelta(days=days - 1)).isoformat()
            rows: dict = {}
            for d in doc["done"]:
                if not start <= str(d.get("on", "")) <= today.isoformat():
                    continue
                who = names.get(d.get("by")) or str(d.get("by_name") or "someone")
                row = rows.setdefault(who, {"name": who, "count": 0, "chores": {}})
                row["count"] += 1
                row["chores"][str(d.get("name", ""))] = row["chores"].get(str(d.get("name", "")), 0) + 1
            out[f"last_{days}_days"] = sorted(rows.values(), key=lambda r: (-r["count"], r["name"].lower()))
        return out

    def show_tally(self) -> str:
        t = self.tally(self._doc(self.core.list()))
        parts = []
        for days in (7, 30):
            rows = t[f"last_{days}_days"]
            if not rows:
                parts.append(f"Last {days} days: no chores marked done.")
                continue
            each = "; ".join(f"{r['name']} {r['count']} ("
                             + ", ".join(f"{k} {v}" for k, v in sorted(r["chores"].items(), key=lambda kv: -kv[1]))
                             + ")" for r in rows)
            parts.append(f"Last {days} days: {each}.")
        return "\n".join(parts)

    def remove_chore_or_person(self, chore=None, person=None) -> str:
        if (chore in (None, "")) == (person in (None, "")):
            raise ValueError("say one thing to remove: a chore or a person")
        core, said = self.core.list(), []
        with self.store.lock:
            doc = self._doc(core)
            if chore not in (None, ""):
                c = self._find_chore(doc, chore, said)
                doc["chores"] = [x for x in doc["chores"] if x is not c]
                self.store.save(doc)
                return " ".join(said + [f"Removed the chore {c.get('name')}. The done tally keeps what was "
                                        f"already done."])
            p = self._find_person(doc, person, said=said)
            pid, freed = p.get("id"), []
            for c in doc["chores"]:
                rot = self._rotation(c)
                if pid not in rot:
                    continue
                at, turn = rot.index(pid), _safe_int(c.get("turn"), 0) % len(rot)
                rot.remove(pid)
                c["assigned"] = rot
                c["turn"] = 0 if not rot else (turn - 1 if at < turn else turn) % len(rot)
                if not rot:
                    freed.append(str(c.get("name")))
            doc["people"] = [x for x in doc["people"] if x is not p]
            self.store.save(doc)
        text = f"Removed {p.get('name')} from the household and from every chore they were on."
        if freed:
            text += " Now on nobody: " + ", ".join(freed) + "."
        if p.get("core_id") and any(c["id"] == p["core_id"] for c in core):
            text += " They are still in the PuffPAI people list, so they still show here."
        return " ".join(said + [text + " The done tally keeps what they did."])

    def board(self) -> dict:
        """What the page shows: every chore with who it is on and when, the people, the tally."""
        doc = self._doc(self.core.list())
        today = self.today()
        names = self._names(doc)
        last = today + timedelta(days=WEEK_DAYS - 1)
        chores = []
        for c in doc["chores"]:
            due = _as_date(c.get("due"))
            if c.get("finished"):
                when = "finished"
            elif due is None:
                when = "later"
            else:
                when = "today" if due <= today else "week" if due <= last else "later"
            on = self._on(c)
            chores.append({**c, "when": when, "late": max(0, (today - due).days) if due else 0,
                           "repeat_words": _repeat_words(c), "on": on, "on_name": names.get(on, ""),
                           "rotation_names": [names.get(x, "?") for x in self._rotation(c)]})
        chores.sort(key=lambda c: (str(c.get("due")), str(c.get("name", "")).lower()))
        week = self.tally_counts(doc, 7, today)
        month = self.tally_counts(doc, 30, today)
        people = [{"id": p.get("id"), "name": p.get("name"), "done_7": week.get(p.get("id"), 0),
                   "done_30": month.get(p.get("id"), 0),
                   "on": [c["name"] for c in chores if c["on"] == p.get("id") and c["when"] != "finished"]}
                  for p in doc["people"]]
        return {"today": today.isoformat(), "week_end": last.isoformat(), "chores": chores, "people": people,
                "tally": self.tally(doc), "last_updated": doc["last_updated"]}


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_CHORE = {"chore": _p("string", "The chore's name, like dishes. An id works too but is not needed.")}
_FIELDS = {
    "name": _p("string", "What the chore is, like take out the trash."),
    "repeat": _p("string", "once, daily, weekly, or every N days (like every 3 days). Default once."),
    "every_days": _p("integer", "Optional: for every N days, the N."),
    "assign_to": _p("string", "One person's name, or several names separated by commas for a rotation that moves "
                              "to the next person each time it is done. Say nobody to leave it open."),
    "due_on": _p("string", "Optional: the date it is (next) due, like 2027-03-31, or today or tomorrow. "
                           "Default today."),
    "notes": _p("string", "Optional: a short note, like where the supplies are."),
}
TOOLS = [
    ("list_due", "Use only to see what is due: the chores due today (with any that are late) or in the next "
     "7 days, and who each one is on.",
     {"when": _p("string", "today or week. Default today."),
      "person": _p("string", "Optional: only the chores on this person, like Sam.")}, []),
    ("add_chore", "Add a household chore: its name, how often it repeats (once, daily, weekly or every N days), "
     "when it is first due, and the person or rotation of people it is assigned to.", dict(_FIELDS), ["name"]),
    ("change_chore", "Change a chore by its name, no need to look it up first: its name, how often it repeats, "
     "its next due date, its notes, or who it is on (one person, a rotation, or nobody).",
     {**_CHORE, **_FIELDS, "name": _p("string", "Optional: a new name for the chore.")}, ["chore"]),
    ("mark_done", "Mark a chore done by its name when someone did it, no need to look it up first. It sets the "
     "next due date and moves a rotation to the next person.",
     {**_CHORE, "by": _p("string", "Optional: who did it, like Alex. Default: the person it is on."),
      "on": _p("string", "Optional: the day it was done, like yesterday or 2027-03-31. Default today.")},
     ["chore"]),
    ("list_people", "Use only to see the household: the people, the chores each one is on now, and how many each "
     "did in the last 7 days.", {}, []),
    ("add_person", "Add a person to the household so chores can be assigned to them.",
     {"name": _p("string", "The person's name.")}, ["name"]),
    ("show_tally", "Show who did which chores in the last 7 days and in the last 30 days.", {}, []),
    ("remove_chore_or_person", "Remove a chore or a person by name, no need to look it up first. A person comes off "
     "every chore and rotation; the done tally keeps what was already done.",
     {**_CHORE, "person": _p("string", "Or the person's name, like Jo.")}, []),
]
CHANGES = ["add_chore", "change_chore", "mark_done", "add_person", "remove_chore_or_person"]
DELETES = ["remove_chore_or_person"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Board, msg: dict) -> dict | None:
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
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Chore Board does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

CHORE_FIELDS = ("name", "repeat", "every_days", "assign_to", "due_on", "notes")
PAGE_ROUTES = ("/api/add", "/api/update", "/api/done", "/api/remove",
               "/api/person/add", "/api/person/rename", "/api/person/remove")


class Handler(BaseHTTPRequestHandler):
    server_version = "ChoreBoard/1"
    app: Board
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
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12 or int(raw) > MAX_BODY:
            raise ValueError(f"The request body is too big: Chore Board takes at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _drop(self, most: int, wait_s: float) -> None:
        """Read and drop the body (at most *most* bytes within *wait_s* seconds) so the answer reaches the sender,
        then close the connection."""
        self.close_connection = True
        raw = (self.headers.get("Content-Length") or "0").strip()
        left = min(int(raw), most) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else 0
        end = time.monotonic() + wait_s
        try:
            while left > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.05, end - time.monotonic()))
                chunk = self.rfile.read1(min(left, 65536))
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

    def _body(self):
        """The JSON body: a body over the cap is read and dropped, then refused; the whole body must arrive within
        BODY_WAIT_S seconds. Every refusal is a ValueError with a plain message."""
        try:
            n = self._length()
        except ValueError:
            self._drop(MAX_DRAIN, DRAIN_WAIT_S)
            raise
        raw = bytearray()
        end = time.monotonic() + BODY_WAIT_S
        try:
            while len(raw) < n:
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
            raise ValueError(f"The request body did not all arrive within {BODY_WAIT_S} seconds.") from None
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        try:
            return _loads(bytes(raw).decode("utf-8")) if raw else {}
        except (ValueError, RecursionError):
            raise ValueError("The request body is not JSON this app can read.") from None

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
        self._drop(REFUSED_DRAIN, REFUSED_WAIT_S)       # at most 64 KB within 2 seconds, then the answer
        self._json(401, {"error": "Chore Board needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Chore Board stopped on this request with an error "
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
            if path == "/board":
                return self._json(200, self.app.board())
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Chore Board has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            return self._json(404, {"error": "Chore Board has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            fields = {k: body[k] for k in CHORE_FIELDS if k in body}
            app = self.app
            if path == "/api/add":
                out = app.add_chore(**fields)
            elif path == "/api/update":
                out = {"text": app.change_chore(body.get("id"), **fields)}
            elif path == "/api/done":
                out = {"text": app.mark_done(body.get("id"), body.get("by"), body.get("on"))}
            elif path == "/api/remove":
                out = {"text": app.remove_chore_or_person(chore=body.get("id"))}
            elif path == "/api/person/add":
                out = app.add_person(body.get("name"))
            elif path == "/api/person/rename":
                out = {"text": app.rename_person(body.get("id"), body.get("name"))}
            else:
                out = {"text": app.remove_chore_or_person(person=body.get("id"))}
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
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, today=date.today, api: str | None = None,
                people_ttl_s: float = 60) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Board(Store(data), today, People(api, token, people_ttl_s, min(30, people_ttl_s)))
    handler = type("ChoreHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Chore Board for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Chore Board with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
