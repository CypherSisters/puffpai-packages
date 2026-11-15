"""Journal: a private journal that stays on this computer, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/journal/
- ``/health``       "ok"
- ``/updated``      the stamp of the last change
- ``/index``        every entry in short (no body), for the calendar and the filters
- ``/list``         whole entries, newest first, filtered (tag, mood, starred, month, day, words)
- ``/stats``        count, writing streak, entries from this day in earlier years, tags
- ``/export.json``  every entry in one JSON file; ``/export.zip`` one Markdown file per entry
- ``/api/add``, ``/api/change``, ``/api/remove``, ``/api/import``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/journal/, which adds it.

Each entry is one JSON file in PUFFPAI_DATA, ``entries/YYYY/MM/<id>.json``. The
entry files are the journal; ``index.json`` is a short list built from them, so it
can always be built again. Writes go to a temp file, then a rename. A file that
exists but will not read (bad JSON, or 0 bytes) is never written over. How the
streak, "on this day" and import work: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "journal"
VERSION = "1.0.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024                   # one request from the page or the bot
MAX_IMPORT = 16 * 1024 * 1024           # one import file
MAX_DRAIN = 64 * 1024 * 1024            # a body over its cap is read and dropped up to this much...
DRAIN_WAIT_S = 20                       # ...within this long, then answered 400
BODY_WAIT_S = 5                         # a whole body must arrive within this long
REFUSED_DRAIN = 64 * 1024               # a request without the token: at most this much is read...
REFUSED_WAIT_S = 2                      # ...within this long, then 401
MAX_TEXT = 100_000                      # characters in one entry's body
MAX_ENTRIES = 50_000
MAX_TAGS = 30
MAX_INT_DIGITS = 15                     # a number in a request has at most this many digits
ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4,8}")   # always used with fullmatch, so no trailing newline gets in
TAG_RE = re.compile(r"(?<![\w&#/])#([^\W\d_][\w-]{0,39})")
MOODS = {1: "awful", 2: "low", 3: "okay", 4: "good", 5: "great"}
MOOD_WORDS = {"awful": 1, "terrible": 1, "very bad": 1, "low": 2, "bad": 2, "sad": 2, "down": 2,
              "okay": 3, "ok": 3, "fine": 3, "meh": 3, "so so": 3, "good": 4, "happy": 4,
              "great": 5, "amazing": 5, "excellent": 5, "wonderful": 5}
PERIODS = {"week": 7, "month": 30, "year": 365}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _refuse_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _parse_int(s: str) -> int:
    if len(s.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError("a number in the request is too long")
    return int(s)


def _parse_float(s: str) -> float:
    if len(s) > 40:
        raise ValueError("a number in the request is too long")
    x = float(s)
    if not math.isfinite(x):
        raise ValueError("a number in the request is too big")
    return x


def _loads(text: str):
    """JSON in, with plain refusals for huge numbers, NaN and deep nesting."""
    try:
        return json.loads(text, parse_constant=_refuse_constant, parse_int=_parse_int, parse_float=_parse_float)
    except RecursionError:
        raise ValueError("the JSON is nested too deeply") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"that is not JSON ({exc.msg})") from None


def _text(value, name: str, most: int, default: str = "", lines: bool = False) -> str:
    """Words of at most *most* characters, trimmed; *lines* keeps line breaks (for a body)."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip() if lines else " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _whole(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a whole number")
    try:
        x = float(value)
        if not math.isfinite(x) or x != int(x) or not lo <= x <= hi:
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"{name} is a whole number from {lo} to {hi}") from None
    return int(x)


def _mood(value) -> int | None:
    """1 to 5, or a mood word; 0, none or an empty value clears it."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
        word = " ".join(value.lower().replace("-", " ").split())
        if word in ("none", "no mood", "clear"):
            return 0
        if word in MOOD_WORDS:
            return MOOD_WORDS[word]
        raise ValueError("mood is 1 to 5 (1 awful, 2 low, 3 okay, 4 good, 5 great)")
    return _whole(value, "mood", 0, 5)


def _flag(value, name: str) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "true", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false", "off", "0"):
        return False
    raise ValueError(f"{name} is yes or no")


def _clean_tag(t) -> str:
    if not isinstance(t, str) or len(t) > 200:          # capped before any pattern touches it
        return ""
    t = t.strip().lstrip("#@").strip().lower()
    t = re.sub(r"\s+", "-", t)
    return t[:40] if re.fullmatch(r"[^\W_][\w-]*", t) else ""


def _tags(value, name: str = "tags") -> list[str] | None:
    """A list of tags, or words separated by commas or spaces; None when not given."""
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > 2000:
            raise ValueError(f"{name} is at most 2000 characters")
        value = value.replace(",", " ").split()
    if not isinstance(value, list) or len(value) > MAX_TAGS * 2:
        raise ValueError(f"{name} is a list of short words")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str):
            raise ValueError(f"{name} is a list of short words")
        t = _clean_tag(v)
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def inline_tags(body: str) -> list[str]:
    out: list[str] = []
    for m in TAG_RE.finditer(body or ""):
        t = _clean_tag(m.group(1))
        if t and t not in out:
            out.append(t)
    return out


def _words(text) -> list[str]:
    """The words of a title: letters and digits of any script, lower case with accents folded (é is e), so
    'Jose' finds 'José'; anything else separates them."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch if ch.isalnum() else " " for ch in text if not unicodedata.combining(ch)).casefold().split()


def _merge(*lists) -> list[str]:
    out: list[str] = []
    for lst in lists:
        for t in lst or []:
            if t not in out:
                out.append(t)
    return out[:MAX_TAGS]


def _day(value, name: str, today: date) -> date | None:
    """A date as YYYY-MM-DD, or today or yesterday; None when not given."""
    text = _text(value, name, 40).lower()
    if not text:
        return None
    if text in ("today", "yesterday"):
        return today - timedelta(days=0 if text == "today" else 1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or today or yesterday") from None


def _stamp(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _when(created: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(created))
    except ValueError:
        return None


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """An entry file exists but will not read. Nothing writes over it."""


def _write_json(path: Path, obj) -> None:
    """Temp file in the same folder, then one rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".journal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


ENTRY_KEYS = ("id", "created", "updated", "title", "body", "mood", "tags", "starred", "prompt")
INDEX_KEYS = {"app", "built", "count", "unreadable", "entries"}


def _read_entry(path: Path) -> dict:
    """An entry file, checked; Unreadable for a 0 byte file, bad JSON or the wrong shape, and for a file that is
    not this app's: one with another app's marker or other keys. A file from before the marker (exactly the keys
    this version writes) still reads; it gets the marker on its next save."""
    try:
        raw = path.read_bytes()
        if not raw.strip():
            raise Unreadable(f"{path.name} is empty")
        data = _loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Unreadable(f"{path.name} will not read ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("body"), str) or _when(data.get("created")) is None:
        raise Unreadable(f"{path.name} is not a Journal entry")
    if data.get("app", NAME) != NAME or not set(data) - {"app"} <= set(ENTRY_KEYS):
        raise Unreadable(f"{path.name} is not this app's file (it is not a Journal entry)")
    mood = data.get("mood")
    return {"id": path.stem, "created": str(data["created"]), "updated": str(data.get("updated") or data["created"]),
            "title": str(data.get("title") or ""), "body": data["body"],
            "mood": mood if isinstance(mood, int) and not isinstance(mood, bool) and 1 <= mood <= 5 else None,
            "tags": [t for t in (data.get("tags") or []) if isinstance(t, str)][:MAX_TAGS]
            if isinstance(data.get("tags"), list) else [],
            "starred": data.get("starred") is True, "prompt": str(data.get("prompt") or "")}


def summary(e: dict) -> dict:
    snippet = " ".join(e["body"].split())
    return {"id": e["id"], "created": e["created"], "updated": e["updated"], "title": e["title"],
            "mood": e["mood"], "tags": e["tags"], "starred": e["starred"],
            "words": len(e["body"].split()), "snippet": snippet[:160] + ("..." if len(snippet) > 160 else "")}


class Store:
    """The entry files, held in memory once read, and the index built from them."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.root = self.folder / "entries"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.folder / "index.json"
        self.lock = threading.RLock()
        self.entries: dict[str, dict] = {}
        self.paths: dict[str, Path] = {}
        self.unreadable: list[str] = []
        self.stamp = ""
        self.foreign = ""                    # set when index.json is another app's file: then nothing is written
        self._index_seen: tuple = ()
        self.rebuild()

    def rebuild(self) -> None:
        """Read every entry file again and write the index from them."""
        with self.lock:
            self.entries, self.paths, self.unreadable = {}, {}, []
            for path in sorted(self.root.glob("*/*/*.json")):
                rel = path.relative_to(self.folder).as_posix()
                if not ID_RE.fullmatch(path.stem) or path.stem in self.entries:
                    self.unreadable.append(f"{rel} (not a Journal entry file name)")
                    continue
                try:
                    self.entries[path.stem] = _read_entry(path)
                    self.paths[path.stem] = path
                except Unreadable as exc:
                    self.unreadable.append(f"{rel} ({exc})")
            if self.index_path.exists():
                try:
                    if not isinstance(_loads(self.index_path.read_text(encoding="utf-8")), dict):
                        raise ValueError
                except (OSError, UnicodeDecodeError, ValueError):
                    # The index only repeats the entry files, but a file that will not read is still kept.
                    aside = self.folder / f"index.unreadable-{datetime.now():%Y%m%d-%H%M%S}.json"
                    os.replace(self.index_path, aside)
            try:
                self.guard()
            except Unreadable:
                return                           # another app's index: read the entries, write nothing
            self._changed()

    def guard(self) -> None:
        """Unreadable (and nothing is written) while index.json is not this app's file: valid JSON without the
        Journal marker, or with other keys. Read again only when the file changed on disk."""
        try:
            st = self.index_path.stat()
            seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            seen = None
        if seen is None or seen == self._index_seen:
            if seen is None:
                self.foreign = ""
            if self.foreign:
                raise Unreadable(self.foreign)
            return
        try:
            data = _loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            data = None                          # a broken index is moved aside at the next start, never written
        if not (isinstance(data, dict) and data.get("app") == NAME and set(data) <= INDEX_KEYS
                and isinstance(data.get("entries"), list)):
            self.foreign = (f"{self.index_path} is not this app's file (it is not a Journal index). Journal "
                            f"changes nothing while it is there; move it away")
        else:
            self.foreign = ""
        self._index_seen = seen
        if self.foreign:
            raise Unreadable(self.foreign)

    def _changed(self) -> None:
        self.guard()
        self.stamp = datetime.now().isoformat()
        items = sorted((summary(e) for e in self.entries.values()), key=lambda s: s["created"], reverse=True)
        _write_json(self.index_path, {"app": NAME, "built": self.stamp, "count": len(items),
                                      "unreadable": self.unreadable, "entries": items})
        try:
            st = self.index_path.stat()
            self._index_seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass

    def path_for(self, entry_id: str, created: str) -> Path:
        dt = _when(created) or datetime.now()
        return self.root / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{entry_id}.json"

    def new_id(self, created: str) -> str:
        dt = _when(created) or datetime.now()
        while True:
            eid = f"{dt:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
            if eid not in self.entries and not self.path_for(eid, created).exists():
                return eid

    def put(self, e: dict, new: bool) -> None:
        """Save one entry. A new one never lands on a file that exists; a changed one is read first."""
        with self.lock:
            self.guard()
            path = self.paths.get(e["id"]) if not new else self.path_for(e["id"], e["created"])
            if new and (path.exists() or e["id"] in self.entries):
                raise ValueError(f"an entry {e['id']} is already there; Journal never writes over one")
            if not new:
                _read_entry(path)                      # still readable; otherwise Unreadable, nothing written
            if len(self.entries) >= MAX_ENTRIES and new:
                raise ValueError(f"Journal holds at most {MAX_ENTRIES} entries")
            _write_json(path, {"app": NAME, **{k: e[k] for k in ENTRY_KEYS}})
            self.entries[e["id"]], self.paths[e["id"]] = e, path

    def delete(self, entry_id: str) -> None:
        """Remove one entry. Its file is read first; one that will not read or is empty is left as it is."""
        with self.lock:
            self.guard()
            path = self.paths[entry_id]
            if path.exists():
                _read_entry(path)                      # still readable; otherwise Unreadable, nothing deleted
            path.unlink(missing_ok=True)
            del self.paths[entry_id]
            self.entries.pop(entry_id, None)


# ------------------------------------------------------------- the logic

def _brief(e: dict) -> str:
    when = e["created"][:16].replace("T", " ")
    bits = [when]
    if e["title"]:
        bits.append(e["title"])
    if e["mood"]:
        bits.append(f"mood {e['mood']} ({MOODS[e['mood']]})")
    if e["starred"]:
        bits.append("starred")
    if e["tags"]:
        bits.append("tags " + ", ".join(e["tags"]))
    return f"[{e['id']}] " + "; ".join(bits)


def _label(e: dict) -> str:
    """How an entry is named back to its owner: day, title, id."""
    return f"{e['created'][:10]} {e['title'] or '(no title)'} (id {e['id']})"


def _full(e: dict) -> str:
    out = _brief(e)
    if e["prompt"]:
        out += f"\nPrompt: {e['prompt']}"
    return out + "\n" + e["body"]


class Journal:
    """Entries, search, streak and the rest."""

    def __init__(self, store: Store, now=datetime.now) -> None:
        self.store = store
        self.now = now                       # a function, so a test can move the clock

    def today(self) -> date:
        return self.now().date()

    def _sorted(self, items=None) -> list[dict]:
        items = self.store.entries.values() if items is None else items
        return sorted(items, key=lambda e: (e["created"], e["id"]), reverse=True)

    def _as_day(self, key: str) -> date | None:
        """The day *key* names (today, yesterday or a date like 2027-03-31), or None."""
        if not re.fullmatch(r"today|yesterday|\d{4}-\d{2}-\d{2}", key.strip().lower()):
            return None
        try:
            return _day(key, "entry", self.today())
        except ValueError:
            return None

    def _find(self, entry, said: list | None = None) -> dict:
        """One entry from latest, its id, its day, or its title: exact (case and spaces ignored), then the one title
        where every word given is a whole word or the start (3 letters or more) of one; never letters inside a word.
        A title match that is not exact adds "Using '<title>' (<day>)." to *said*. More than one match, or none,
        changes nothing: the error lists them."""
        key = _text(entry, "entry", 200)
        if not key:
            raise ValueError("say which entry: latest, its date, or words in its title")
        if key.lower() in ("latest", "last", "newest", "my latest", "my last", "the latest", "the last"):
            items = self._sorted()
            titled = [e for e in items if "".join(e["title"].split()).lower() == "".join(key.split()).lower()]
            if len(titled) == 1:
                return titled[0]                     # an entry with exactly that title wins over the word
            if not items:
                raise ValueError("the journal has no entries yet")
            return items[0]
        if key in self.store.entries:
            return self.store.entries[key]
        if ID_RE.fullmatch(key):
            raise ValueError(f"there is no entry {key!r}; say its date or words in its title instead")
        items = self._sorted()
        d = self._as_day(key)
        if d is not None:
            found = [e for e in items if e["created"][:10] == d.isoformat()]
            what = f"on {d.isoformat()}"
        else:
            want = "".join(key.split()).lower()
            found = [e for e in items if "".join(e["title"].split()).lower() == want]
            if not found:
                given = _words(key)
                found = [e for e in items if given and all(any(w == n or (len(w) >= 3 and n.startswith(w))
                                                               for n in _words(e["title"])) for w in given)]
                if len(found) == 1 and said is not None:
                    said.append(f"Using {found[0]['title']!r} ({found[0]['created'][:10]}).")
            what = f"with {key!r} in its title"
        if len(found) == 1:
            return found[0]
        if not found:
            recent = "; ".join(_label(e) for e in items[:5]) or "none yet"
            raise ValueError(f"there is no journal entry {what} (the newest: {recent})")
        raise ValueError(f"More than one entry matches {key!r}: {'; '.join(_label(e) for e in found[:10])}. "
                         f"Say which.")

    # ---- writing
    def add_entry(self, body=None, title=None, mood=None, tags=None, starred=None, prompt=None, day=None) -> dict:
        body = _text(body, "body", MAX_TEXT, lines=True)
        title = _text(title, "title", 200)
        if not body and not title:
            raise ValueError("an entry needs some words")
        m = _mood(mood) or None
        tag_list = _tags(tags) or []
        star = bool(_flag(starred, "starred"))
        prompt = _text(prompt, "prompt", 300)
        now = self.now()
        on = _day(day, "day", now.date())
        if on is not None:
            if on > now.date():
                raise ValueError("day can not be a day still to come")
            now = datetime.combine(on, now.time())
        created = _stamp(now)
        with self.store.lock:
            e = {"id": self.store.new_id(created), "created": created, "updated": created, "title": title,
                 "body": body, "mood": m, "tags": _merge(tag_list, inline_tags(body)), "starred": star,
                 "prompt": prompt}
            self.store.put(e, new=True)
            self.store._changed()
        extra = f", tags {', '.join(e['tags'])}" if e["tags"] else ""
        return {"id": e["id"], "text": f"Saved a journal entry for {created[:16].replace('T', ' ')} "
                                       f"({_plural(len(body.split()), 'word')}{extra}). Its id is {e['id']}."}

    def change_entry(self, entry=None, title=None, body=None, add_text=None, tags=None, add_tags=None, mood=None,
                     starred=None) -> str:
        said: list[str] = []
        with self.store.lock:
            self.store.guard()
            old = self._find(entry, said)
            e = dict(old)
            changed = []
            if title is not None:
                e["title"] = _text(title, "title", 200)
                changed.append("title")
            if body is not None:
                e["body"] = _text(body, "body", MAX_TEXT, lines=True)
                changed.append("text")
            if add_text not in (None, ""):
                more = _text(add_text, "add_text", MAX_TEXT, lines=True)
                e["body"] = (e["body"] + "\n\n" + more).strip()
                if len(e["body"]) > MAX_TEXT:
                    raise ValueError(f"body is at most {MAX_TEXT} characters")
                changed.append("added text")
            new_tags = _tags(tags)
            plus = _tags(add_tags, "add_tags")
            if new_tags is not None:
                e["tags"] = new_tags
                changed.append("tags")
            if plus:
                e["tags"] = _merge(e["tags"], plus)
                if "tags" not in changed:
                    changed.append("tags")
            m = _mood(mood)
            if m is not None:
                e["mood"] = m or None
                changed.append("mood")
            s = _flag(starred, "starred")
            if s is not None:
                e["starred"] = s
                changed.append("star")
            if not changed:
                raise ValueError("say what to change: title, body, add_text, tags, add_tags, mood or starred")
            if not e["body"] and not e["title"]:
                raise ValueError("an entry needs some words; to remove it, use remove_entry")
            e["tags"] = _merge(e["tags"], inline_tags(e["body"]))
            e["updated"] = _stamp(self.now())
            self.store.put(e, new=False)
            self.store._changed()
        return " ".join(said + [f"Changed {', '.join(changed)}: {_brief(e)}"])

    def remove_entry(self, entry=None) -> str:
        said: list[str] = []
        with self.store.lock:
            self.store.guard()
            e = self._find(entry, said)
            self.store.delete(e["id"])
            self.store._changed()
        return " ".join(said + [f"Deleted the entry from {e['created'][:16].replace('T', ' ')}"
                                + (f" ({e['title']})." if e["title"] else ".")])

    # ---- reading
    def filtered(self, words=None, tag=None, start=None, end=None, mood=None, starred=None, month=None,
                 day=None) -> list[dict]:
        today = self.today()
        words = _text(words, "words", 200).lower().split()
        tag = _clean_tag(_text(tag, "tag", 41)) if tag not in (None, "") else ""
        a = _day(start, "start", today)
        b = _day(end, "end", today)
        d = _day(day, "day", today)
        m = _mood(mood)
        s = _flag(starred, "starred")
        month = _text(month, "month", 7)
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month is like 2027-03")
        out = []
        for e in self.store.entries.values():
            on = e["created"][:10]
            if (a and on < a.isoformat()) or (b and on > b.isoformat()) or (d and on != d.isoformat()):
                continue
            if month and not on.startswith(month):
                continue
            if tag and tag not in e["tags"]:
                continue
            if m and e["mood"] != m:
                continue
            if s is not None and e["starred"] != s:
                continue
            if words:
                hay = " ".join([e["title"], e["body"], " ".join(e["tags"])]).lower()
                if not all(w.lstrip("#") in hay for w in words):
                    continue
            out.append(e)
        return self._sorted(out)

    def search_entries(self, words=None, tag=None, start=None, end=None, mood=None, starred=None, limit=None) -> str:
        n = _whole(limit, "limit", 1, 50) if limit not in (None, "") else 10
        found = self.filtered(words, tag, start, end, mood, starred)
        if not found:
            return "No journal entries match."
        lines = [f"- {_brief(e)}: {summary(e)['snippet']}" for e in found[:n]]
        more = f" (showing the newest {n})" if len(found) > n else ""
        return f"{_plural(len(found), 'entry', 'entries')} match{more}:\n" + "\n".join(lines)

    def read_entry(self, entry=None, day=None) -> str:
        if entry not in (None, "") and day in (None, "") and isinstance(entry, str) and self._as_day(entry.strip()):
            entry, day = None, entry                   # a date reads every entry on that day
        if day not in (None, "") and entry in (None, ""):
            d = _day(day, "day", self.today())
            found = self.filtered(day=d.isoformat())
            if not found:
                return f"No journal entries on {d.isoformat()}."
            return f"{_plural(len(found), 'entry', 'entries')} on {d.isoformat()}:\n\n" + \
                "\n\n".join(_full(e) for e in reversed(found))
        said: list[str] = []
        e = self._find(entry, said)
        return " ".join(said + [_full(e)])

    def list_recent(self, count=None) -> str:
        n = _whole(count, "count", 1, 50) if count not in (None, "") else 10
        items = self._sorted()[:n]
        if not items:
            return "The journal has no entries yet."
        return f"The newest {_plural(len(items), 'entry', 'entries')}:\n" + \
            "\n".join(f"- {_brief(e)}: {summary(e)['snippet']}" for e in items)

    def on_this_day_entries(self, d: date) -> list[dict]:
        md = d.strftime("%m-%d")
        also = "02-29" if md == "02-28" else None
        out = []
        for e in self.store.entries.values():
            on = e["created"][:10]
            if on[:4] >= f"{d.year:04d}":
                continue
            if on[5:] == md:
                out.append(e)
            elif also and on[5:] == also:
                try:
                    date(d.year, 2, 29)            # a leap year has its own 29th
                except ValueError:
                    out.append(e)
        return self._sorted(out)

    def on_this_day(self, day=None) -> str:
        d = _day(day, "day", self.today()) or self.today()
        found = self.on_this_day_entries(d)
        if not found:
            return f"No entries from {d.strftime('%B')} {d.day} in earlier years."
        return f"{_plural(len(found), 'entry', 'entries')} from {d.strftime('%B')} {d.day} in earlier years:\n\n" + \
            "\n\n".join(_full(e) for e in found)

    def streaks(self) -> tuple[int, int]:
        """(current streak, longest streak) in days; see PROCESS.md."""
        days = sorted({e["created"][:10] for e in self.store.entries.values()})
        dates = [d for d in (_as_date(x) for x in days) if d]
        longest = run = 0
        prev = None
        for d in dates:
            run = run + 1 if prev and (d - prev).days == 1 else 1
            longest, prev = max(longest, run), d
        have = set(dates)
        today = self.today()
        cur = today if today in have else today - timedelta(days=1)
        current = 0
        while cur in have:
            current += 1
            cur -= timedelta(days=1)
        return current, longest

    def journal_stats(self, period=None, start=None, end=None) -> str:
        today = self.today()
        a, b = _day(start, "start", today), _day(end, "end", today)
        period = _text(period, "period", 10).lower().replace("this ", "").replace("last ", "")
        if a or b:
            a = a or date(1900, 1, 1)
            b = b or today
            span = f"from {a.isoformat()} to {b.isoformat()}"
        elif period in ("", "all", "ever"):
            a, b, span = date(1900, 1, 1), today, "in all"
        elif period in PERIODS:
            a, b = today - timedelta(days=PERIODS[period] - 1), today
            span = f"in the last {PERIODS[period]} days"
        else:
            raise ValueError("period is week, month, year or all; or give start and end")
        items = self.filtered(start=a.isoformat(), end=b.isoformat())
        cur, longest = self.streaks()
        moods = [e["mood"] for e in items if e["mood"]]
        tags: dict = {}
        for e in items:
            for t in e["tags"]:
                tags[t] = tags.get(t, 0) + 1
        lines = [f"{_plural(len(items), 'entry', 'entries')} {span}, on "
                 f"{_plural(len({e['created'][:10] for e in items}), 'day')}, "
                 f"{_plural(sum(len(e['body'].split()) for e in items), 'word')}.",
                 f"Writing streak: {_plural(cur, 'day')} in a row now; longest {_plural(longest, 'day')}. "
                 f"{_plural(len(self.store.entries), 'entry', 'entries')} in the journal."]
        if moods:
            counts = ", ".join(f"{MOODS[k]} {moods.count(k)}" for k in range(5, 0, -1) if moods.count(k))
            lines.append(f"Mood: average {sum(moods) / len(moods):.1f} of 5 over {_plural(len(moods), 'entry', 'entries')}"
                         f" ({counts}).")
        else:
            lines.append("No moods noted.")
        if tags:
            top = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            lines.append("Tags: " + ", ".join(f"{t} {n}" for t, n in top) + ".")
        if self.store.unreadable:
            lines.append(f"{_plural(len(self.store.unreadable), 'file')} in the journal folder will not read and "
                         f"are left as they are.")
        return "\n".join(lines)

    def stats(self) -> dict:
        cur, longest = self.streaks()
        tags: dict = {}
        for e in self.store.entries.values():
            for t in e["tags"]:
                tags[t] = tags.get(t, 0) + 1
        return {"today": self.today().isoformat(), "count": len(self.store.entries), "streak": cur,
                "longest": longest, "on_this_day": self.on_this_day_entries(self.today()),
                "tags": sorted(tags.items(), key=lambda kv: (-kv[1], kv[0])),
                "unreadable": self.store.unreadable, "last_updated": self.store.stamp}

    # ---- bring and take data
    def export_json(self) -> bytes:
        items = sorted(self.store.entries.values(), key=lambda e: (e["created"], e["id"]))
        return json.dumps({"app": NAME, "version": 1, "exported": _stamp(self.now()), "entries": items},
                          ensure_ascii=False, indent=1).encode("utf-8")

    def export_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for e in sorted(self.store.entries.values(), key=lambda e: (e["created"], e["id"])):
                head = ["---", f"id: {e['id']}", f"created: {e['created']}", f"updated: {e['updated']}",
                        f"title: {json.dumps(e['title'], ensure_ascii=False)}",
                        f"mood: {e['mood'] if e['mood'] else 'null'}",
                        f"tags: {json.dumps(e['tags'], ensure_ascii=False)}",
                        f"starred: {'true' if e['starred'] else 'false'}",
                        f"prompt: {json.dumps(e['prompt'], ensure_ascii=False)}", "---", ""]
                z.writestr(f"journal/{e['created'][:4]}/{e['created'][5:7]}/{e['id']}.md",
                           "\n".join(head) + "\n" + e["body"] + "\n")
        return buf.getvalue()

    @staticmethod
    def _from_item(item) -> dict | None:
        """One imported item as an entry (no id yet), or None when it can not be read."""
        if not isinstance(item, dict):
            return None
        if "creationDate" in item or "text" in item:                       # Day One
            body, title = item.get("text"), ""
            when = _parse_time(item.get("creationDate"))
            mood, prompt, given_id = None, "", None
        elif "date" in item and ("body" in item or "title" in item):        # jrnl
            body, title = item.get("body", ""), item.get("title", "")
            when = _parse_time(f"{item.get('date')}T{item.get('time') or '00:00'}")
            mood, prompt, given_id = None, "", None
        elif "created" in item and "body" in item:                          # Journal's own export
            body, title = item.get("body"), item.get("title") or ""
            when = _parse_time(item.get("created"))
            mood, prompt, given_id = item.get("mood"), item.get("prompt") or "", item.get("id")
        else:
            return None
        if not isinstance(body, str) or not isinstance(title, str) or not isinstance(prompt, str) or when is None:
            return None
        body = body.replace("\r\n", "\n").strip()
        title = " ".join(title.split())[:200]
        if len(body) > MAX_TEXT or not (body or title):
            return None
        tags = [_clean_tag(t) for t in item.get("tags") or [] if isinstance(t, str)] \
            if isinstance(item.get("tags"), list) else []
        created = _stamp(when)
        upd = _parse_time(item.get("updated") or item.get("modifiedDate"))
        return {"given_id": given_id if isinstance(given_id, str) and ID_RE.fullmatch(given_id) else None,
                "created": created, "updated": _stamp(upd) if upd else created, "title": title, "body": body,
                "mood": mood if isinstance(mood, int) and not isinstance(mood, bool) and 1 <= mood <= 5 else None,
                "tags": _merge([t for t in tags if t], inline_tags(body)), "starred": item.get("starred") is True,
                "prompt": " ".join(prompt.split())[:300]}

    def import_data(self, data) -> str:
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            items = data["entries"]
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("this file is not a journal export Journal can read (Journal, jrnl or Day One JSON)")
        if len(items) > MAX_ENTRIES:
            raise ValueError(f"an import holds at most {MAX_ENTRIES} entries")
        added = already = unread = 0
        failed: list[str] = []
        with self.store.lock:
            self.store.guard()
            try:
                seen = {(e["created"][:16], e["body"]) for e in self.store.entries.values()}
                for item in items:
                    try:
                        e = self._from_item(item)
                        if e is None:
                            unread += 1
                            continue
                        gid = e.pop("given_id")
                        if (gid and gid in self.store.entries) or (e["created"][:16], e["body"]) in seen:
                            already += 1
                            continue
                        if gid and not self.store.path_for(gid, e["created"]).exists():
                            e["id"] = gid
                        else:
                            e["id"] = self.store.new_id(e["created"])
                        try:
                            self.store.put(e, new=True)
                        except ValueError:
                            already += 1
                            continue
                        seen.add((e["created"][:16], e["body"]))
                        added += 1
                    except Exception as exc:  # noqa: BLE001 - one bad item is skipped, the rest go on
                        failed.append(type(exc).__name__)
            finally:
                self.store._changed()              # always, so the index on disk matches what is in memory
        text = (f"Imported {_plural(added, 'entry', 'entries')}. Skipped {already} already in the journal "
                f"and {unread} that could not be read.")
        if failed:
            reasons = ", ".join(sorted(set(failed)))
            text += f" Skipped {_plural(len(failed), 'more entry', 'more entries')} that could not be saved ({reasons})."
        return text


def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_time(value) -> datetime | None:
    """A date and time from an import; a time with a zone (Day One's UTC) becomes this computer's local time."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    if not 1900 <= dt.year <= 2200:
        return None
    return dt


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_ENTRY = {"entry": _p("string", "Which entry, in plain words: latest, its date like yesterday or 2027-03-31, or "
                           "words in its title like river walk.")}
TOOLS = [
    ("add_entry", "Write a new journal entry: the text, and if wanted a title, a mood from 1 (awful) to 5 (great), "
     "tags and a star. Words written as #word in the text become tags too.",
     {"body": _p("string", "What to write in the entry. Markdown is fine."),
      "title": _p("string", "Optional: a short title."),
      "mood": _p("integer", "Optional: 1 awful, 2 low, 3 okay, 4 good, 5 great."),
      "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: tags, like work or family."},
      "starred": _p("boolean", "Optional: true to star it."),
      "prompt": _p("string", "Optional: the writing prompt it answers."),
      "day": _p("string", "Optional: yesterday or a date like 2027-03-31 for an entry about an earlier day. "
                          "Default today.")}, ["body"]),
    ("change_entry", "Star, unstar or change a journal entry in one step. Name the entry by latest, its date, or "
     "words in its title; this tool finds it itself, so call it straight away. It changes the title, the text "
     "(replace it or add to the end), the tags, the mood or the star.",
     {**_ENTRY, "title": _p("string", "Optional: a new title."),
      "body": _p("string", "Optional: new text that replaces the whole text."),
      "add_text": _p("string", "Optional: text to add at the end."),
      "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: the new list of tags."},
      "add_tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: tags to add."},
      "mood": _p("integer", "Optional: 1 to 5, or 0 to clear it."),
      "starred": _p("boolean", "Optional: true to star it, false to take the star off.")}, ["entry"]),
    ("remove_entry", "Delete a journal entry for good in one step. Name the entry by latest, its date, or words "
     "in its title; this tool finds it itself, so call it straight away. Its file is removed from this computer.",
     dict(_ENTRY),
     ["entry"]),
    ("search_entries", "Use only to find entries when the owner asks what they wrote: search the journal by words in "
     "the title, text or tags, by tag, by a date range, by mood or by star, newest first.",
     {"words": _p("string", "Optional: words that must all appear."),
      "tag": _p("string", "Optional: one tag."),
      "start": _p("string", "Optional: the first day, like 2027-03-01."),
      "end": _p("string", "Optional: the last day, like 2027-03-31."),
      "mood": _p("integer", "Optional: only entries with this mood, 1 to 5."),
      "starred": _p("boolean", "Optional: true for starred entries only."),
      "limit": _p("integer", "Optional: how many to show, 1 to 50. Default 10.")}, []),
    ("read_entry", "Use only when the owner asks to hear or see an entry's words, never as a first step before "
     "starring, changing or deleting one: read a journal entry in full by its date, words in its title, or latest. "
     "A date reads every entry written that day.",
     {**_ENTRY, "day": _p("string", "Optional instead of entry: a date like 2027-03-31, or today or yesterday.")},
     []),
    ("list_recent", "Use only when the owner asks to see a list, never as a first step before starring, changing "
     "or deleting an entry: list the newest journal entries with their date, title, mood, tags and first words.",
     {"count": _p("integer", "Optional: how many, 1 to 50. Default 10.")}, []),
    ("on_this_day", "Show the journal entries written on this same date in earlier years.",
     {"day": _p("string", "Optional: another date, like 2027-03-31. Default today.")}, []),
    ("journal_stats", "Show the writing streak (days in a row with an entry), how many entries there are, and the "
     "moods and tags over a period.",
     {"period": _p("string", "Optional: week, month, year or all. Default all."),
      "start": _p("string", "Optional instead of period: the first day, like 2027-03-01."),
      "end": _p("string", "Optional: the last day. Default today.")}, []),
]
CHANGES = ["add_entry", "change_entry", "remove_entry"]
DELETES = ["remove_entry"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Journal, msg) -> dict | None:
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
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            out = getattr(app, name)(**args)
            text = out["text"] if isinstance(out, dict) else out
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": f"{exc}. Journal will not write over it."}],
                       "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Journal does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

ENTRY_FIELDS = ("title", "body", "mood", "tags", "starred", "prompt")
LIST_FIELDS = ("words", "tag", "mood", "starred", "month", "day", "start", "end")


class Handler(BaseHTTPRequestHandler):
    server_version = "Journal/1"
    app: Journal
    token: str
    timeout = 30        # seconds; a stalled sender is cut off

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

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _length(self, most: int = MAX_BODY) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most *most* bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12 or int(raw) > most:
            size = f"{most // (1024 * 1024)} MB" if most >= 1024 * 1024 else f"{most // 1024} KB"
            raise ValueError(f"The request body is too big: Journal takes at most {size} here.")
        return int(raw)

    def _drain(self, most: int = MAX_DRAIN, wait_s: float = DRAIN_WAIT_S) -> None:
        """Read and drop a body that will be refused (at most *most* bytes within *wait_s* seconds), so the sender
        gets the answer, not a reset connection; then close the connection."""
        self.close_connection = True
        raw = (self.headers.get("Content-Length") or "0").strip()
        left = min(int(raw), most) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else 0
        end = time.monotonic() + wait_s
        try:
            while left > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.05, end - time.monotonic()))
                got = self.rfile.read1(min(left, 64 * 1024))
                if not got:
                    break
                left -= len(got)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _body(self, most: int = MAX_BODY):
        try:
            n = self._length(most)
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
            raise ValueError(f"The request body did not all arrive within {BODY_WAIT_S} seconds.") from None
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        raw = bytes(raw)
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("Send the JSON as UTF-8 text.") from None
        return _loads(text) if text.strip() else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _query(self) -> dict:
        q = parse_qs(urlsplit(self.path).query, max_num_fields=20)
        return {k: v[0] for k, v in q.items() if k in LIST_FIELDS + ("offset", "limit")}

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        self._drain(REFUSED_DRAIN, REFUSED_WAIT_S)     # at most 64 KB within 2 seconds, then the answer
        self._json(401, {"error": "Journal needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Journal stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path, app = self._path(), self.app
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/updated":
                return self._json(200, {"last_updated": app.store.stamp})
            if path == "/index":
                with app.store.lock:
                    items = [summary(e) for e in app._sorted()]
                return self._json(200, {"entries": items, "last_updated": app.store.stamp})
            if path == "/list":
                q = self._query()
                off = _whole(q.pop("offset", 0), "offset", 0, MAX_ENTRIES)
                lim = _whole(q.pop("limit", 30), "limit", 1, 200)
                with app.store.lock:
                    found = app.filtered(**q)
                return self._json(200, {"total": len(found), "entries": found[off:off + lim]})
            if path == "/stats":
                with app.store.lock:
                    return self._json(200, app.stats())
            if path == "/export.json":
                with app.store.lock:
                    body = app.export_json()
                return self._send(200, body, "application/json; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="journal-export.json"'})
            if path == "/export.zip":
                with app.store.lock:
                    body = app.export_zip()
                return self._send(200, body, "application/zip",
                                  {"Content-Disposition": 'attachment; filename="journal-markdown.zip"'})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "Journal has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path, app = self._path(), self.app
        if path == "/mcp":
            return self._mcp()
        if path not in ("/api/add", "/api/change", "/api/remove", "/api/import"):
            return self._json(404, {"error": "Journal has no page here."})
        try:
            body = self._body(MAX_IMPORT if path == "/api/import" else MAX_BODY)
            if path == "/api/import":
                out = {"text": app.import_data(body)}
            elif not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            elif path == "/api/add":
                out = app.add_entry(**{k: body[k] for k in ENTRY_FIELDS if k in body})
            elif path == "/api/change":
                fields = {k: body[k] for k in ("title", "body", "tags", "mood", "starred") if k in body}
                out = {"text": app.change_entry(body.get("id"), **fields)}
            else:
                out = {"text": app.remove_entry(body.get("id"))}
            out["last_updated"] = app.store.stamp
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": f"{exc}. Journal will not write over it."})
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


def make_server(port: int, data: Path | str, token: str | None, now=datetime.now) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Journal(Store(data), now)
    handler = type("JournalHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Journal for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Journal with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
