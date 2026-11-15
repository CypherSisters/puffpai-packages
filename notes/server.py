"""Notes: a plain notebook the bot can search and add to, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/notes/
- ``/health``       "ok"
- ``/updated``      a stamp that changes when any note changes (GET)
- ``/list``         the notes, filtered by words, tag and notebook, with the notebooks and tags (GET)
- ``/note``         one note in full (GET ?id=)
- ``/export``       every note as a zip of its Markdown file (GET)
- ``/api/add``, ``/api/change``, ``/api/tick``, ``/api/remove``, ``/api/import``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/notes/, which adds it.

Each note is one Markdown file in ``notes/`` in PUFFPAI_DATA, with a small front
matter block (id, title, created, updated, tags, pinned, notebook) and the body.
The list is an index kept in memory and built again from the files whenever a file
in that folder changes, so files copied in or edited by hand show up. Writes replace
a file at once (temp file, then rename). A file that will not read, an empty one
included, is never written over. Import never writes over a note. How a note is
found by its title, how checklists work and how import reads files: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
import zipfile
import zlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "notes"
VERSION = "1.0.1"
FOLDER = "notes"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 2 * 1024 * 1024              # page changes and MCP calls
MAX_NOTE = 1024 * 1024                  # one note file
MAX_NOTES = 5000
MAX_ZIP = 200 * 1024 * 1024             # an import archive (as in My Papers)
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_TOTAL = 1024 * 1024 * 1024      # everything inside an import archive, unpacked
MAX_DRAIN = 64 * 1024 * 1024            # a refused body: up to this much is read and dropped before the 400
DRAIN_WAIT_S = 20.0                     # ... within this many seconds
NO_TOKEN_DRAIN = 64 * 1024              # a request without the token: at most this much is read before the 401
NO_TOKEN_WAIT_S = 2.0
MAX_TAG_TEXT = 2000                     # tags given as one piece of text
MAX_LINE = 2000                         # a line looked at for a heading
BODY_WAIT_S = 5.0                      # the whole wait for a request body (a big import gets longer, below)
BIG_WAIT_S = 120.0
MAX_TAGS = 20
SHOWN = 20                              # notes in one find answer
READ_CHARS = 8000                       # characters of a note read out by read_note
FILLER = {"the", "my", "our", "a", "an", "note", "notes", "called", "named", "titled", "on", "in", "from", "about"}
TRUE_WORDS = {"true", "yes", "y", "1", "on", "done", "tick", "ticked", "checked", "pin", "pinned", "x"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "not done", "untick", "unticked", "unchecked", "unpin", "unpinned",
               "open", ""}
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
TAG_RE = re.compile(r"[\w][\w -]{0,39}")
NOTE_EXTS = (".md", ".markdown", ".txt")
FRONT_KEYS = ("id", "title", "created", "updated", "tags", "pinned", "notebook")     # Notes writes these; others kept
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}


def _now(clock) -> str:
    return clock().isoformat(timespec="seconds")


def _check(line: str) -> tuple[str, str, str] | None:
    """A tick box line, '- [ ] words' or '- [x] words' with any spaces before the dash, as (those spaces, the mark,
    the words); None for any other line. Plain string steps, no pattern."""
    rest = line.lstrip()
    if not rest.startswith(("- [ ] ", "- [x] ", "- [X] ")):
        return None
    return line[:len(line) - len(rest)], rest[3], rest[6:]


def _heading(line: str) -> str | None:
    """The words of a '# Heading' line (one #, then a space), trailing #s dropped; None otherwise or when the line
    is longer than MAX_LINE. Plain string steps, no pattern."""
    if len(line) > MAX_LINE:
        return None
    parts = line.strip().split(None, 1)
    if len(parts) != 2 or parts[0] != "#":
        return None
    return " ".join(parts[1].rstrip("#").split()) or None


def _unmarked(text: str) -> str:
    """Text without a list mark it already starts with ('- [ ] ', '- [x] ', '- ' or '* ')."""
    for mark in ("- [ ] ", "- [x] ", "- [X] ", "- ", "* "):
        if text.startswith(mark):
            return text[len(mark):]
    return text


def _is_note_name(name: str) -> bool:
    return name.lower().endswith(NOTE_EXTS)


def _declared_total(infos) -> int:
    """What a zip says it unpacks to. Only a first check: import also counts the bytes it really reads."""
    return sum(i.file_size for i in infos)


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, lines: bool = False) -> str:
    """Words of at most *most* characters, control characters dropped; '' when not given. *lines* keeps the
    text as typed (new lines, spaces at the start of a line); otherwise it is one trimmed line."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be words")
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c in "\n\t" or (c >= " " and c != "\x7f"))
    value = value.rstrip() if lines else " ".join(value.split())
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


def _tags(value) -> list[str]:
    """Tags from a list or from words like 'food, home' or '#food #home'; lower case, no #."""
    if value is None:
        return []
    if isinstance(value, str):
        if len(value) > MAX_TAG_TEXT:
            raise ValueError(f"tags are at most {MAX_TAG_TEXT} characters in all")
        parts = re.split(r"[,;]|\s(?=#)", value) if ("," in value or ";" in value or "#" in value) else [value]
    elif isinstance(value, list):
        parts = value
    else:
        raise ValueError("tags is a list of words")
    out = []
    for p in parts:
        t = " ".join(_text(p, "a tag", 60).lstrip("#").lower().split())
        if not t:
            continue
        if not TAG_RE.fullmatch(t):
            raise ValueError(f"a tag is letters, digits, spaces or -, at most 40 characters: {t[:40]!r}")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError(f"a note has at most {MAX_TAGS} tags")
    return out


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


WORD_RE = re.compile(r"[^\W_]+")                     # letters and digits of any script: what str.isalnum takes


@functools.lru_cache(maxsize=65536)
def _fold_word(word: str) -> tuple[str, ...]:
    """One word that is not plain ASCII, lower case with its accents folded away (é is e, İ is i)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))
    return tuple(WORD_RE.findall(text.casefold()))


def _words(text) -> list[str]:
    """Lower-case words with accents folded (é is e), so 'Jose' finds 'José'; anything that is not a letter or
    digit separates them. Each word that is not plain ASCII is folded once and kept, so long text stays quick."""
    text = str(text or "")
    if text.isascii():
        return WORD_RE.findall(text.lower())
    out: list[str] = []
    for w in WORD_RE.findall(unicodedata.normalize("NFC", text)):
        if w.isascii():
            out.append(w.lower())
        else:
            out.extend(_fold_word(w))
    return out


def _bare(text) -> str:
    """A whole title or item for comparing: folded words, a leading 'the' dropped."""
    words = _words(text)
    return " ".join(words[1:] if len(words) > 1 and words[0] == "the" else words)


def _starts(given: list[str], text) -> bool:
    """Each word given is a whole word of *text* or the start of one; never letters inside a word."""
    have = _words(text)
    return bool(given) and all(any(h.startswith(w) for h in have) for w in given)


def safe_name(name: str) -> str:
    """The last part of a path, printable, at most 120 characters, for answers only."""
    n = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in n if c >= " " and c != "\x7f")[:120] or "file"


# ---------------------------------------------------------------- front matter

def _scalar(v: str) -> str:
    v = v.strip()
    if v.startswith('"'):
        try:
            got = json.loads(v)
            return got if isinstance(got, str) else str(got)
        except ValueError:
            return v.strip('"')
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _flow_list(v: str) -> list[str]:
    v = v.strip()
    if v.startswith("["):
        try:
            got = json.loads(v)
            if isinstance(got, list):
                return [str(x) for x in got if isinstance(x, (str, int))]
        except ValueError:
            pass
        v = v.strip("[]")
    return [_scalar(x) for x in v.split(",") if x.strip()]


def _front_key(line: str) -> str | None:
    """The key of a front matter line 'key: value' that starts at the line's start, in lower case; else None.
    Plain string steps, no pattern."""
    key, sep, _ = line.partition(":")
    if not sep or not key or len(key) > 100 or not (key[0].isascii() and (key[0].isalpha() or key[0] == "_")):
        return None
    return key.lower() if all(c.isalnum() or c in "_-" for c in key) else None


def split_front(text: str) -> tuple[dict | None, str, list[str]]:
    """(front matter as a dict, or None when there is none; the body; the front matter lines of every key Notes
    does not use, as written and in their order, with the lines that belong to them). A block that opens with ---
    and never closes is an error, so the file is left alone."""
    t = text[1:] if text.startswith("﻿") else text
    lines = t.split("\n")
    if lines[0].rstrip() != "---":
        return None, t, []
    for i in range(1, min(len(lines), 300)):
        if lines[i].rstrip() in ("---", "..."):
            fm, key, extra = {}, None, []
            for line in lines[1:i]:
                k = _front_key(line.rstrip())
                if k:
                    key, v = k, line.rstrip().partition(":")[2].lstrip()
                    fm[key] = _flow_list(v) if v.strip().startswith("[") else (_scalar(v) if v.strip() else [])
                else:
                    item = line.lstrip()
                    if key and item.startswith("-") and item[1:2].isspace() and isinstance(fm.get(key), list):
                        fm[key].append(_scalar(item.strip()[1:]))
                if key not in FRONT_KEYS:
                    extra.append(line)
            return fm, "\n".join(lines[i + 1:]), extra
    raise ValueError("its front matter starts with --- and never ends")


def _stamp(value, fallback: str) -> str:
    try:
        return datetime.fromisoformat(str(value).strip()[:19].replace(" ", "T")).isoformat(timespec="seconds")
    except ValueError:
        return fallback


def render(note: dict) -> str:
    """A note as its file: the front matter block (the owner's own keys first, as written, then Notes' keys), then
    the body."""
    return ("---\n" + "".join(line + "\n" for line in note.get("extra") or []) +
            f"id: {note['id']}\n"
            f"title: {json.dumps(note['title'], ensure_ascii=False)}\n"
            f"created: {note['created']}\n"
            f"updated: {note['updated']}\n"
            f"tags: {json.dumps(note['tags'], ensure_ascii=False)}\n"
            f"pinned: {'true' if note['pinned'] else 'false'}\n"
            f"notebook: {json.dumps(note['notebook'], ensure_ascii=False)}\n"
            "---\n" + note["body"].rstrip("\n") + "\n")


def parse_note(text: str, stem: str, mtime: str) -> tuple[dict, bool]:
    """(a note, whether it had front matter) from a file's text. ValueError when it will not read."""
    fm, body, extra = split_front(text)
    had = fm is not None
    fm = fm or {}
    title = _scalar(str(fm.get("title") or "")) if not isinstance(fm.get("title"), list) else ""
    title = " ".join(title.split())[:200]
    if not title:
        first = next((ln for ln in body.split("\n") if ln.strip()), "")
        title = (_heading(first) or " ".join(stem.replace("_", " ").split()))[:200]
    tags = fm.get("tags")
    tags = tags if isinstance(tags, list) else _flow_list(tags) if isinstance(tags, str) and tags else []
    try:
        tags = _tags([t for t in tags if isinstance(t, str)][:MAX_TAGS])
    except ValueError:
        tags = []
    pinned = fm.get("pinned")
    try:
        pinned = _flag(pinned, "pinned") if isinstance(pinned, str) else False
    except ValueError:
        pinned = False
    notebook = fm.get("notebook")
    notebook = " ".join(str(notebook).split())[:60] if isinstance(notebook, str) else ""
    created = _stamp(fm.get("created") or fm.get("date") or "", mtime)
    return {"id": str(fm.get("id") or ""), "title": title or "Untitled", "created": created,
            "updated": _stamp(fm.get("updated") or "", created if fm.get("created") else mtime),
            "tags": tags, "pinned": pinned, "notebook": notebook, "body": body.strip("\n"), "extra": extra}, had


def read_file(path: Path) -> tuple[str, str]:
    """(a note file's text with every line ending made \\n, the file's own line ending), or ValueError: too big,
    empty, not UTF-8."""
    size = path.stat().st_size
    if size > MAX_NOTE:
        raise ValueError(f"it is larger than {MAX_NOTE // 1024} KB")
    raw = path.read_bytes()
    if len(raw) > MAX_NOTE:
        raise ValueError(f"it is larger than {MAX_NOTE // 1024} KB")
    if not raw.strip():
        raise ValueError("it is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("it is not UTF-8 text") from None
    nl = "\r\n" if "\r\n" in text else "\r" if "\r" in text else "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n"), nl


# ---------------------------------------------------------------- the notebook

class Notes:
    """Every note, one file each, with an index kept in memory."""

    def __init__(self, folder: Path | str, clock=datetime.now) -> None:
        self.dir = Path(folder) / FOLDER
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.notes: dict[str, dict] = {}
        self.unreadable: list[dict] = []
        self.renewed: list[dict] = []           # files whose own id could not be used, so they have a new one
        self.told: set[str] = set()             # ... already said on the page, so each is said once
        self.sig = None
        self.version = 0
        self._hays: dict[str, tuple] = {}       # note id -> (its title, tags and text, their folded words)

    # ------------------------------------------------------------ the index
    def _signature(self):
        out = []
        with os.scandir(self.dir) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".md") and not e.name.startswith("."):
                    st = e.stat()
                    out.append((e.name, st.st_mtime_ns, st.st_size))
        return tuple(sorted(out))

    def fresh(self) -> None:
        """Build the index again when any file in the notes folder changed."""
        with self.lock:
            sig = self._signature()
            if sig == self.sig:
                return
            notes, bad, seen, renewed = {}, [], set(), []
            for name, mtime_ns, _ in sig[:MAX_NOTES]:
                path = self.dir / name
                mtime = datetime.fromtimestamp(mtime_ns / 1e9).isoformat(timespec="seconds")
                try:
                    text, nl = read_file(path)
                    note, had = parse_note(text, name[:-3], mtime)
                except (OSError, ValueError) as exc:
                    bad.append({"file": name, "why": str(exc)})
                    continue
                own = note["id"]
                if not (ID_RE.fullmatch(own) and own not in seen):
                    # no id, an id that is not valid, or one another file has: a new id, the same on every load
                    # (made from the file name), written into the file the next time the note is saved
                    note["id"] = "f" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:9]
                    if own:
                        note["was"] = own
                        renewed.append({"file": name, "why": f"its id {own[:60]!r} is " +
                                        ("used by another note" if ID_RE.fullmatch(own) else "not a valid id")})
                seen.add(note["id"])
                note["file"], note["front"], note["nl"] = name, had, nl
                notes[note["id"]] = note
            self.notes, self.unreadable, self.renewed, self.sig = notes, bad, renewed, sig
            self.version += 1

    def stamp(self) -> str:
        self.fresh()
        return f"{self.version}-{len(self.notes)}"

    def ordered(self) -> list[dict]:
        """Pinned first, then newest first."""
        self.fresh()
        with self.lock:
            rows = list(self.notes.values())
        rows.sort(key=lambda n: n["updated"], reverse=True)
        rows.sort(key=lambda n: not n["pinned"])
        return rows

    # ------------------------------------------------------------ files
    def _free_name(self, title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-") or "note"
        if slug.split(".")[0] in RESERVED:
            slug = "note-" + slug
        for n in range(1, 10000):
            name = f"{slug}.md" if n == 1 else f"{slug}-{n}.md"
            if not (self.dir / name).exists():
                return name
        raise OSError("no free file name")

    def _write(self, name: str, note: dict, new: bool) -> None:
        """Write one note file at once. A new file never replaces one already there; an old file is written
        only when it still reads as this same note."""
        path = self.dir / name
        text = render(note)
        nl = note.get("nl") or "\n"                         # the file's own line ending is kept
        if len(text.encode("utf-8")) + (len(nl) - 1) * text.count("\n") > MAX_NOTE:
            raise ValueError(f"a note is at most {MAX_NOTE // 1024} KB")
        if new:
            if path.exists():
                raise OSError("that file name is taken")
        else:
            try:
                now, _ = parse_note(read_file(path)[0], name[:-3], "")
            except (OSError, ValueError) as exc:
                raise ValueError(f"the note file {path} will not read ({exc}). Notes will not write over it. "
                                 f"Move it away or fix it by hand.") from None
            if now["id"] and now["id"] not in (note["id"], note.get("was")):
                raise ValueError(f"the note file {path} changed on disk; nothing was saved. Try again.")
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".note-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline=nl) as f:
                f.write(text)
            if new and path.exists():
                raise OSError("that file name is taken")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        # the index, kept in step without reading every file again
        old = next((k for k, v in self.notes.items() if v["file"] == name), None)
        if old is not None and old != note["id"]:
            del self.notes[old]
        self.notes[note["id"]] = {**{k: v for k, v in note.items() if k != "was"}, "body": note["body"].strip("\n"),
                                  "file": name, "front": True, "nl": nl}
        self.sig = self._signature()
        self.version += 1

    def create(self, title, body="", tags=None, notebook=None, pinned=None, created=None, keep_id=None,
               extra=None) -> dict:
        title = _text(title, "title", 200)
        body = _text(body, "body", MAX_NOTE, True)
        if not title:
            first = next((ln for ln in body.split("\n") if ln.strip()), "")
            title = (_heading(first) or " ".join(first[:MAX_LINE].split()))[:80]
        if not title:
            raise ValueError("say the note's title or some words for it")
        with self.lock:
            self.fresh()
            if len(self.notes) >= MAX_NOTES:
                raise ValueError(f"Notes holds at most {MAX_NOTES} notes")
            nid = keep_id if keep_id and ID_RE.fullmatch(keep_id) and keep_id not in self.notes else \
                "n" + secrets.token_hex(4)
            while nid in self.notes:
                nid = "n" + secrets.token_hex(4)
            now = _now(self.clock)
            note = {"id": nid, "title": title, "created": created or now, "updated": now, "tags": _tags(tags),
                    "pinned": False if pinned in (None, "") else _flag(pinned, "pinned"),
                    "notebook": _text(notebook, "notebook", 60), "body": body, "extra": list(extra or [])}
            name = self._free_name(title)
            self._write(name, note, new=True)
            return self.notes[nid]

    def save(self, note: dict, **changes) -> dict:
        with self.lock:
            new = {k: note[k] for k in ("id", "title", "created", "updated", "tags", "pinned", "notebook", "body")}
            new["extra"], new["nl"], new["was"] = note.get("extra") or [], note.get("nl") or "\n", note.get("was")
            new.update(changes)
            new["updated"] = _now(self.clock)
            self._write(note["file"], new, new=False)
            return self.notes[note["id"]]

    # ------------------------------------------------------------ finding
    def _hay(self, n: dict) -> list[str]:
        """The folded words of a note's title, tags and text. Text that is not plain ASCII is folded once for each
        version of the note and kept as one string, so a search stays quick."""
        if n["body"].isascii() and n["title"].isascii() and all(t.isascii() for t in n["tags"]):
            return _words(n["title"] + " " + " ".join(n["tags"]) + " " + n["body"])
        parts = (n["title"], tuple(n["tags"]), n["body"])
        got = self._hays.get(n["id"])
        if got is None or got[0][0] != parts[0] or got[0][1] != parts[1] or got[0][2] is not parts[2]:
            got = (parts, " ".join(_words(n["title"] + " " + " ".join(n["tags"]) + " " + n["body"])))
            if len(self._hays) >= MAX_NOTES:
                self._hays.clear()
            self._hays[n["id"]] = got
        return got[1].split()

    def find(self, ident, what: str = "note", by_text: bool = True) -> dict:
        """A note by its id or its whole title (case, accents and words like 'the' or 'note' ignored), else the one
        title where every word of 3 letters or more is a whole word or the start of one, else the one note holding
        every such word. Never letters inside a word; shorter words only count in a whole title. Several or none:
        ValueError with the candidates, and nothing changes. With by_text False (deleting) only the title counts."""
        text = _text(ident, what, 200)
        if not text:
            raise ValueError(f"say which {what}, by its title")
        rows = self.ordered()
        low = text.lower()
        words = [w for w in _words(low) if w not in FILLER] or _words(low)
        bare = " ".join(words)
        given = [w for w in words if len(w) >= 3]

        def pick(hits, how):
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ValueError(f"more than one note {how} {text!r}: " + "; ".join(n["title"] for n in hits[:12])
                                 + ". Say which one; nothing was changed.")
            return None

        for test, how in ((lambda n: n["id"] == text or n["title"].lower() == low, "is called"),
                          (lambda n: bare and bare in (" ".join(_words(n["title"])), _bare(n["title"])), "is called"),
                          (lambda n: _starts(given, n["title"]), "has a title with"),
                          (lambda n: by_text and given and set(given) <= set(self._hay(n)), "holds")):
            got = pick([n for n in rows if test(n)], how)
            if got:
                return got
        known = "; ".join(n["title"] for n in rows[:12]) or "none yet"
        raise ValueError(f"no note matches {text!r}; nothing was changed. The newest notes are: {known}")

    # ------------------------------------------------------------ tools
    @staticmethod
    def _line(n: dict, extra: str = "") -> str:
        meta = [n["notebook"] and f"notebook {n['notebook']}", n["tags"] and "tags " + ", ".join(n["tags"]),
                "pinned" if n["pinned"] else "", f"updated {n['updated'][:10]}"]
        return f"- {n['title']} ({'; '.join(m for m in meta if m)})" + (f": {extra}" if extra else "")

    def search(self, words=None, tag=None, notebook=None) -> list[tuple[dict, str]]:
        words_t = _text(words, "words", 200)
        tag_t = " ".join(_text(tag, "tag", 60).lstrip("#").lower().split())
        book = _text(notebook, "notebook", 60).lower()
        rows = self.ordered()
        if tag_t:
            rows = [n for n in rows if tag_t in n["tags"]]
        if book:
            exact = [n for n in rows if n["notebook"].lower() == book]
            rows = exact or [n for n in rows if book in n["notebook"].lower()]
        out = []
        want = _words(words_t)
        for n in rows:
            if want:
                hay = self._hay(n)
                if not all(any(h.startswith(w) for h in hay) for w in want):
                    continue
                line = next((ln.strip() for ln in n["body"].split("\n")
                             if any(w in ln.lower() for w in want)), "")
                out.append((n, line[:160]))
            else:
                first = next((ln.strip() for ln in n["body"].split("\n") if ln.strip()), "")
                out.append((n, first[:160]))
        return out

    def find_notes(self, words=None, tag=None, notebook=None) -> str:
        hits = self.search(words, tag, notebook)
        said = " and ".join(x for x in (words and f"words {words!r}", tag and f"tag {tag!r}",
                                        notebook and f"notebook {notebook!r}") if x)
        if not hits:
            return f"No notes match {said or 'that'}." + self._bad_note()
        head = f"{len(hits)} note{'s' if len(hits) != 1 else ''}" + (f" {'match' if len(hits) != 1 else 'matches'} {said}" if said else "") + \
            (f"; the first {SHOWN}" if len(hits) > SHOWN else "") + ":"
        return head + "\n" + "\n".join(self._line(n, line) for n, line in hits[:SHOWN]) + self._bad_note()

    def _bad_note(self) -> str:
        if not self.unreadable:
            return ""
        return (f"\n{len(self.unreadable)} file(s) in the notes folder would not read and are left as they are: "
                + ", ".join(b["file"] for b in self.unreadable[:5]) + ".")

    def read_note(self, note=None) -> str:
        n = self.find(note)
        body = n["body"]
        more = ""
        if len(body) > READ_CHARS:
            body, more = body[:READ_CHARS], f"\n(The note goes on for {len(n['body']) - READ_CHARS} more characters; " \
                                            f"open it on the page to read the rest.)"
        return self._line(n)[2:] + "\n\n" + (body or "(no text yet)") + more

    def add_note(self, title=None, body=None, tags=None, notebook=None, pinned=None) -> str:
        n = self.create(title, body or "", tags, notebook, pinned)
        return f"Added the note {n['title']!r}" + (f" in notebook {n['notebook']}" if n["notebook"] else "") + \
            (f", tags {', '.join(n['tags'])}" if n["tags"] else "") + f" (file {n['file']})."

    def add_to_note(self, note=None, text=None, checklist=None) -> str:
        items = text if isinstance(text, list) else [text]
        lines = [_text(t, "text", 2000) for t in items[:50]]
        lines = [t for t in lines if t]
        if not lines:
            raise ValueError("say what to add")
        with self.lock:
            n = self.find(note)
            has_list = any(_check(ln) for ln in n["body"].split("\n"))
            as_box = has_list if checklist in (None, "") else _flag(checklist, "checklist")
            new = [("- [ ] " + _unmarked(t)) if as_box else t for t in lines]
            body = n["body"].rstrip("\n")
            body = (body + "\n" if body else "") + "\n".join(new)
            n = self.save(n, body=body)
        return f"Added to {n['title']!r}: " + "; ".join(new) + "."

    def change_note(self, note=None, title=None, body=None, tags=None, pinned=None, notebook=None) -> str:
        changes = {}
        if title not in (None, ""):
            changes["title"] = _text(title, "title", 200)
        if body is not None:
            changes["body"] = _text(body, "body", MAX_NOTE, True)
        if tags is not None:
            changes["tags"] = _tags(tags)
        if pinned not in (None, ""):
            changes["pinned"] = _flag(pinned, "pinned")
        if notebook is not None:
            changes["notebook"] = _text(notebook, "notebook", 60)
        if not changes:
            raise ValueError("say what to change: title, body, tags, pinned or notebook")
        with self.lock:
            n = self.save(self.find(note), **changes)
        said = []
        for k, v in changes.items():
            said.append(f"title {v!r}" if k == "title" else "the text" if k == "body" else
                        "tags " + (", ".join(v) or "none") if k == "tags" else
                        ("pinned" if v else "not pinned") if k == "pinned" else
                        (f"notebook {v!r}" if v else "no notebook"))
        return f"Changed the note {n['title']!r}: " + ", ".join(said) + "."

    def tick_item(self, note=None, item=None, done=None) -> str:
        want = _text(item, "item", 300).lower()
        if not want:
            raise ValueError("say which checklist item")
        mark = True if done in (None, "") else _flag(done, "done")
        with self.lock:
            n = self.find(note)
            lines = n["body"].split("\n")
            boxes = [(i, m) for i, ln in enumerate(lines) if (m := _check(ln))]
            if not boxes:
                raise ValueError(f"the note {n['title']!r} has no checklist; lines that start with - [ ] are its items")
            given = [w for w in _words(want) if len(w) >= 3]          # never letters inside a word
            hits = [b for b in boxes if b[1][2].strip().lower() == want] or \
                [b for b in boxes if _bare(want) and _bare(b[1][2]) == _bare(want)] or \
                [b for b in boxes if _starts(given, b[1][2])]
            if len(hits) != 1:
                names = "; ".join(b[1][2] for b in (hits or boxes)[:20])
                raise ValueError((f"more than one item in {n['title']!r} matches {item!r}: " if hits else
                                  f"no item in {n['title']!r} matches {item!r}. Its items are: ") + names
                                 + ". Nothing was changed.")
            i, m = hits[0]
            lines[i] = f"{m[0]}- [{'x' if mark else ' '}] {m[2]}"
            n = self.save(n, body="\n".join(lines))
        return f"{'Ticked' if mark else 'Unticked'} {m[2]!r} in {n['title']!r}."

    def tick_line(self, nid, line, text, done) -> str:
        """The page's tick box: line *line* of note *nid*, which must still say *text*."""
        if not isinstance(nid, str) or not ID_RE.fullmatch(nid):
            raise ValueError("say which note")
        if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line < 100000:
            raise ValueError("line is a line number")
        with self.lock:
            self.fresh()
            n = self.notes.get(nid)
            if n is None:
                raise ValueError("that note is gone")
            lines = n["body"].split("\n")
            m = _check(lines[line]) if line < len(lines) else None
            if not m or m[2].strip() != _text(text, "text", 2000, True).strip():
                raise ValueError("the note changed; it was read again, try once more")
            lines[line] = f"{m[0]}- [{'x' if _flag(done, 'done') else ' '}] {m[2]}"
            self.save(n, body="\n".join(lines))
        return f"{'Ticked' if _flag(done, 'done') else 'Unticked'} {m[2]!r}."

    def list_notebooks_and_tags(self) -> str:
        rows = self.ordered()
        books, tags = {}, {}
        for n in rows:
            books[n["notebook"] or "(no notebook)"] = books.get(n["notebook"] or "(no notebook)", 0) + 1
            for t in n["tags"]:
                tags[t] = tags.get(t, 0) + 1
        if not rows:
            return "There are no notes yet." + self._bad_note()
        fmt = lambda d: ", ".join(f"{k} ({v})" for k, v in sorted(d.items(), key=lambda x: (-x[1], x[0])))  # noqa: E731
        return (f"{len(rows)} notes. Notebooks: {fmt(books)}. Tags: {fmt(tags) or 'none yet'}. "
                f"Pinned: {sum(1 for n in rows if n['pinned'])}." + self._bad_note())

    def remove_note(self, note=None) -> str:
        with self.lock:
            n = self.find(note, by_text=False)       # a delete goes by the title only, never by words in the text
            path = self.dir / n["file"]
            try:                                     # read it first: remove only the note that was found
                now, _ = parse_note(read_file(path)[0], n["file"][:-3], "")
            except (OSError, ValueError) as exc:
                raise ValueError(f"the note file {path} will not read ({exc}); nothing was removed") from None
            if now["id"] and now["id"] not in (n["id"], n.get("was")):
                raise ValueError("that note's file changed on disk; nothing was removed. Try again.")
            path.unlink()
            self.notes.pop(n["id"], None)
            self.sig = self._signature()
            self.version += 1
        return f"Deleted the note {n['title']!r} (its file {n['file']} is gone from this computer)."

    # ------------------------------------------------------------ export and import
    def export_zip(self) -> bytes:
        buf = io.BytesIO()
        with self.lock:
            self.fresh()
            files = [n["file"] for n in self.notes.values()]
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name in sorted(files):
                    z.write(self.dir / name, f"notes/{name}")
        return buf.getvalue()

    def import_one(self, name: str, raw: bytes) -> str:
        """One Markdown file as a new note: 'added', or 'already here' when a note with its id is here."""
        if len(raw) > MAX_NOTE:
            raise ValueError(f"it is larger than {MAX_NOTE // 1024} KB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("it is not UTF-8 text") from None
        if not text.strip():
            raise ValueError("it is empty")
        stem = safe_name(name)
        stem = next((stem[:-len(e)] for e in NOTE_EXTS if stem.lower().endswith(e)), stem)
        note, had = parse_note(text.replace("\r\n", "\n").replace("\r", "\n"), stem, _now(self.clock))
        body = note["body"]
        lines = body.split("\n")
        first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
        if first is not None and (_heading(lines[first]) or "")[:200] == note["title"]:
            body = "\n".join(lines[first + 1:]).strip("\n")          # the heading became the title
        with self.lock:
            self.fresh()
            if had and note["id"] and note["id"] in self.notes:
                return "already here"
            self.create(note["title"], body, note["tags"], note["notebook"], note["pinned"],
                        created=note["created"], keep_id=note["id"] if had else None, extra=note["extra"])
        return "added"

    def import_bytes(self, name: str, raw: bytes) -> dict:
        if raw[:4] == b"PK\x03\x04" or name.lower().endswith(".zip"):
            return self.import_zip(raw)
        if not _is_note_name(name):
            raise ValueError("import takes a zip of notes, or one .md, .markdown or .txt file")
        try:
            got = self.import_one(name, raw)
        except ValueError as exc:
            raise ValueError(f"{safe_name(name)} was not imported: {exc}") from None
        return {"imported": int(got == "added"), "skipped": int(got != "added"), "notes": [],
                "text": "Imported 1 note." if got == "added" else "That note is already here; nothing was changed."}

    def import_zip(self, raw: bytes) -> dict:
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            infos = z.infolist()
        except (zipfile.BadZipFile, ValueError, OSError):
            raise ValueError("that is not a zip file") from None
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"a zip may hold at most {MAX_ZIP_ENTRIES} files")
        if _declared_total(infos) > MAX_ZIP_TOTAL:
            raise ValueError(f"a zip may unpack to at most {MAX_ZIP_TOTAL // (1024 * 1024)} MB")
        for i in infos:
            for n in (i.orig_filename, i.filename):          # as written in the zip, and as read on this computer
                if n.startswith("/") or "\\" in n or ":" in n or ".." in n.split("/") or "\x00" in n:
                    raise ValueError(f"the zip holds an unsafe name ({safe_name(n)}); nothing was imported")
        added, skipped, notes, unpacked = 0, 0, [], 0
        for info in infos:
            n = info.filename
            if info.is_dir() or n.startswith("__MACOSX/") or safe_name(n).startswith("."):
                continue
            if stat.S_ISLNK(info.external_attr >> 16):         # a link is never read as a note
                skipped += 1
                notes.append(f"{safe_name(n)}: it is a link, not a file")
                continue
            if not _is_note_name(n):
                continue
            try:
                if info.file_size > MAX_NOTE:
                    raise ValueError(f"it is larger than {MAX_NOTE // 1024} KB")
                with z.open(info) as f:
                    data = f.read(MAX_NOTE + 1)
                unpacked += len(data)                           # the bytes really read, whatever the zip says
                if unpacked > MAX_ZIP_TOTAL:
                    skipped += 1
                    notes.append(f"stopped: the zip unpacks to more than {MAX_ZIP_TOTAL // (1024 * 1024)} MB; "
                                 f"{safe_name(n)} and the files after it were not imported")
                    break
                got = self.import_one(n, data)
                if got == "added":
                    added += 1
                else:
                    skipped += 1
            except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error, NotImplementedError,
                    EOFError) as exc:
                skipped += 1
                notes.append(f"{safe_name(n)}: {exc}")
        return {"imported": added, "skipped": skipped, "notes": notes[:20],
                "text": f"Imported {added} note{'s' if added != 1 else ''}; skipped {skipped}."}

    # ------------------------------------------------------------ the page
    def listing(self, q="", tag="", notebook="") -> dict:
        hits = self.search(q or None, tag or None, notebook or None)
        rows = self.ordered()
        books = sorted({n["notebook"] for n in rows if n["notebook"]}, key=str.lower)
        tags = sorted({t for n in rows for t in n["tags"]})
        with self.lock:                                    # each new id is said once on the page
            renewed = [r for r in self.renewed if r["file"] not in self.told]
            self.told.update(r["file"] for r in renewed)
        return {"renewed": renewed, "notes": [{k: n[k] for k in ("id", "title", "created", "updated", "tags", "pinned", "notebook",
                                             "file")} | {"line": line} for n, line in hits],
                "notebooks": books, "tags": tags, "unreadable": self.unreadable, "count": len(rows),
                "stamp": self.stamp()}

    def one(self, nid) -> dict:
        if not isinstance(nid, str) or not ID_RE.fullmatch(nid):
            raise ValueError("say which note")
        self.fresh()
        n = self.notes.get(nid)
        if n is None:
            raise ValueError("that note is gone")
        return {k: v for k, v in n.items() if k not in ("front", "extra", "nl", "was")}


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_NOTE = _p("string", "The note's title, or words in it, like shopping.")
TOOLS = [
    ("find_notes", "Use to search the notes by words, tag or notebook: gives each matching note's title, notebook, "
     "tags and the line that matched, pinned notes first.",
     {"words": _p("string", "Optional: words to look for in titles, text and tags."),
      "tag": _p("string", "Optional: one tag."), "notebook": _p("string", "Optional: one notebook.")}, []),
    ("read_note", "Use to read out one note in full, by its title or words in it; no need to look it up first.",
     {"note": _NOTE}, ["note"]),
    ("add_note", "Use when the owner asks to write down a new note: its title, its text, and if wanted tags and a "
     "notebook. Lines that start with - [ ] become tick boxes.",
     {"title": _p("string", "The note's title."), "body": _p("string", "Optional: the note's text, in Markdown."),
      "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: tags, like [\"garden\"]."},
      "notebook": _p("string", "Optional: the notebook, like Home or Work.")}, ["title"]),
    ("add_to_note", "Use when the owner asks to add a line or an item to a note that is already there, by its title; "
     "no need to look it up first. In a note with tick boxes it goes in as a new tick box.",
     {"note": _NOTE, "text": _p("string", "What to add, like milk."),
      "checklist": _p("boolean", "Optional: true to add it as a tick box, false for a plain line.")},
     ["note", "text"]),
    ("change_note", "Use to change a note by its title; no need to look it up first: rename it, replace its text, set "
     "its tags, pin or unpin it, or move it to another notebook.",
     {"note": _NOTE, "title": _p("string", "Optional: a new title."),
      "body": _p("string", "Optional: the whole new text."),
      "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional: the tags it should have."},
      "pinned": _p("boolean", "Optional: true to pin it at the top, false to unpin."),
      "notebook": _p("string", "Optional: the notebook to move it to.")}, ["note"]),
    ("tick_item", "Use when the owner says an item on a note's checklist is done or not done, by the note's title and "
     "the item's words; no need to look either up first.",
     {"note": _NOTE, "item": _p("string", "The checklist item's words, like milk."),
      "done": _p("boolean", "Optional: true to tick, false to untick. Default true.")}, ["note", "item"]),
    ("list_notebooks_and_tags", "Use to see how the notes are sorted: every notebook and tag, with how many notes "
     "each has.", {}, []),
    ("remove_note", "Use when the owner asks to delete a note, by its title as they said it; no need to look it up or "
     "search first, the app finds it. Its file is removed from this computer.", {"note": _NOTE}, ["note"]),
]
CHANGES = ["add_note", "add_to_note", "change_note", "tick_item", "remove_note"]
DELETES = ["remove_note"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Notes, msg) -> dict | None:
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
        except (TypeError, ValueError, OSError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Notes does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/add", "/api/change", "/api/tick", "/api/remove", "/api/import")


class Handler(BaseHTTPRequestHandler):
    server_version = "Notes/1"
    app: Notes
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

    def _raw(self, most: int = MAX_BODY) -> bytes:
        try:
            n = self._length(most)
        except ValueError:
            self._drain()
            raise
        raw = bytearray()
        end = time.monotonic() + (BODY_WAIT_S if n <= MAX_BODY else BIG_WAIT_S)
        try:
            while len(raw) < n:                      # the whole body in time, however it trickles in
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(n - len(raw), 1024 * 1024))
                if not chunk:
                    break
                raw += chunk
        except OSError:
            self.close_connection = True
            raise ValueError("The request body did not all arrive in time.") from None
        finally:
            self.connection.settimeout(self.timeout)
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        return bytes(raw)

    def _body(self):
        raw = self._raw()
        return _loads(raw) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        self._drain(NO_TOKEN_DRAIN, NO_TOKEN_WAIT_S)          # a caller without the token is never drained for long
        self._json(401, {"error": "Notes needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Notes stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        parts = urlsplit(self.path)
        path = parts.path
        q = {k: v[0][:200] for k, v in parse_qs(parts.query, max_num_fields=10).items()}
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/updated":
                return self._json(200, {"stamp": self.app.stamp()})
            if path == "/list":
                return self._json(200, self.app.listing(q.get("q", ""), q.get("tag", ""), q.get("notebook", "")))
            if path == "/note":
                return self._json(200, self.app.one(q.get("id")))
            if path == "/export":
                body = self.app.export_zip()
                name = f"notes-{self.app.clock().date().isoformat()}.zip"
                return self._send(200, body, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "Notes has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = urlsplit(self.path).path
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            self._drain()
            return self._json(404, {"error": "Notes has no page here."})
        app = self.app
        try:
            if path == "/api/import":
                name = unquote(self.headers.get("X-File-Name") or "notes.md")[:300]
                raw = self._raw(MAX_ZIP)
                return self._json(200, app.import_bytes(name, raw))
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            nid = body.get("id")
            if path == "/api/add":
                n = app.create(body.get("title"), body.get("body") or "", body.get("tags"), body.get("notebook"),
                               body.get("pinned"))
                out = {"id": n["id"], "text": f"Added {n['title']!r}."}
            elif path == "/api/change":
                note = app.one(nid)
                keys = {k: body[k] for k in ("title", "body", "tags", "pinned", "notebook") if k in body}
                out = {"text": app.change_note(note["id"], **keys)}
            elif path == "/api/tick":
                out = {"text": app.tick_line(nid, body.get("line"), body.get("text"), body.get("done"))}
            else:
                out = {"text": app.remove_note(app.one(nid)["id"])}
            out["stamp"] = app.stamp()
            self._json(200, out)
        except (TypeError, ValueError, OSError) as exc:
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


def make_server(port: int, data: Path | str, token: str | None, clock=datetime.now) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Notes(data, clock)
    handler = type("NotesHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Notes for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Notes with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
