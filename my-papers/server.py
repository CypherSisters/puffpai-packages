"""My Papers: household paperwork filing that stays on this computer, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``               the page (web/index.html), shown in the Desktop at /pkg/my-papers/
- ``/health``         "ok"
- ``/updated``        the stamp of the last change
- ``/info``           the paper types, counts, people, whether full text search is on
- ``/papers``         find papers (GET, ?q=&type=&person=&tag=&year=)
- ``/paper/<id>``     one paper with its found text
- ``/coming``         what is coming up (?days=60)
- ``/file/<id>``      the stored file of a paper, always as a download
- ``/export``         every paper and file as one zip
- ``/api/add``, ``/api/update``, ``/api/remove``, ``/api/import``   the page's changes (POST)
- ``/mcp``            the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/my-papers/, which adds it.

The truth is one JSON file per paper in ``papers/`` and the stored files in
``files/`` (named by their sha256) under PUFFPAI_DATA. ``index.sqlite`` is only a
search index, built again at every start. Writes replace a file at once (temp file,
then rename). A paper file that exists but will not read (or is empty) is never
written over. How text is found and how dates come up: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "my-papers"
VERSION = "1.0.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

MAX_BODY = 64 * 1024                    # page changes and MCP calls
MAX_FILE = 20 * 1024 * 1024             # one stored file
MAX_ADD_BODY = MAX_FILE * 4 // 3 + 64 * 1024     # a file sent as base64 inside JSON
MAX_ZIP = 200 * 1024 * 1024             # an import archive
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_TOTAL = 1024 * 1024 * 1024      # everything inside an import archive, unpacked
MAX_SIDECAR = 64 * 1024
MAX_TEXT = 200_000                      # found text kept per paper
MAX_PDF_INFLATE = 40 * 1024 * 1024      # inflated PDF streams, in all, per file
PDF_SECONDS = 3.0                       # time to read the text of one PDF
PDF_TEXT = 2 * 1024 * 1024              # text read from one PDF, in characters
MAX_DRAIN = MAX_ZIP + 64 * 1024         # a refused body up to this size is read and dropped before the answer...
DRAIN_WAIT_S = 20                       # ...within this long
BODY_WAIT_S = 5                         # a whole body must arrive within this long (an upload: 1 s more per 8 MB)
REFUSED_DRAIN = 64 * 1024               # a request without the token: at most this much is read...
REFUSED_WAIT_S = 2                      # ...within this long, then 401
READ_TEXT = 4000                        # found text given to the brain by read_paper
DEFAULT_DAYS = 60
SAME_PAPER_SECONDS = 600                # the same record added again within this time is not added twice
TAX_HINT = "Most tax records: keep 3 years. Some need longer."
NO_TEXT = "No text found (a scan or a picture). Add notes so you can find it."
PARTIAL = "Only the first part of this file was read."
TOO_BIG = f"That file is larger than {MAX_FILE // (1024 * 1024)} MB."

TYPES = ("bill", "statement", "tax", "insurance", "contract or lease", "warranty or receipt", "identity",
         "medical", "school", "vehicle", "home", "pet", "letter", "other")
TYPE_WORDS = {"contract": "contract or lease", "lease": "contract or lease", "warranty": "warranty or receipt",
              "receipt": "warranty or receipt", "id": "identity", "car": "vehicle", "bank": "statement",
              "taxes": "tax", "bills": "bill", "letters": "letter"}
TEXT_KINDS = {".txt": "text", ".md": "text", ".csv": "text", ".json": "text", ".html": "html", ".htm": "html",
              ".pdf": "pdf"}
FILE_KINDS = {".pdf": "pdf", ".txt": "text", ".md": "text", ".csv": "spreadsheet", ".json": "text",
              ".html": "web page", ".htm": "web page", ".jpg": "picture", ".jpeg": "picture", ".png": "picture",
              ".gif": "picture", ".heic": "picture", ".webp": "picture", ".tif": "picture", ".tiff": "picture",
              ".doc": "document", ".docx": "document", ".odt": "document", ".rtf": "document",
              ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".ods": "spreadsheet"}
ID_RE = re.compile(r"p[0-9a-f]{12}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
DATE_FIELDS = ("date", "due", "expires", "keep_until", "follow_up")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, default: str = "", lines: bool = False) -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    if len(value) > max(2 * most, most + 2000):          # capped before any pattern touches it
        raise ValueError(f"{name} is at most {most} characters")
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value)
    value = "\n".join(" ".join(x.split()) for x in value.strip().splitlines()) if lines else " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _whole(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a whole number")
    if isinstance(value, str) and len(value.strip()) > 12:
        raise ValueError(f"{name} is a whole number from {lo} to {hi}")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a whole number") from None
    if not math.isfinite(x) or not lo <= x <= hi or x != int(x):
        raise ValueError(f"{name} is a whole number from {lo} to {hi}")
    return int(x)


def _amount(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("amount must be a number")
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if len(value) > 20:
            raise ValueError("amount must be a number below a trillion")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError("amount must be a number") from None
    if not math.isfinite(x) or abs(x) >= 1e12:
        raise ValueError("amount must be a number below a trillion")
    return round(x, 2)


def _day(value, name: str, today: date) -> date | None:
    """A date as YYYY-MM-DD, or today, tomorrow, yesterday; None when not given or cleared."""
    text = _text(value, name, 40).lower()
    if text in ("", "none", "no", "clear"):
        return None
    words = {"today": 0, "tomorrow": 1, "yesterday": -1}
    if text in words:
        return today + timedelta(days=words[text])
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or today, tomorrow or yesterday") from None
    if not 1900 <= d.year <= 2200:
        raise ValueError(f"{name} is a year from 1900 to 2200")
    return d


def _names(value, name: str, most_each: int, most: int) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ValueError(f"{name} is a list, or words separated by commas")
    out: list[str] = []
    for v in value:
        v = _text(v, name, most_each)
        if v and v.lower() not in (x.lower() for x in out):
            out.append(v)
    if len(out) > most:
        raise ValueError(f"at most {most} {name}")
    return out


def _type(value) -> str:
    text = _text(value, "type", 40).lower()
    if not text:
        return "other"
    text = TYPE_WORDS.get(text, text)
    if text not in TYPES:
        raise ValueError("type is one of: " + ", ".join(TYPES))
    return text


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _small_int(text: str) -> int:
    if len(text) > 18:
        raise ValueError("that number is too big")
    return int(text)


def _finite(text: str) -> float:
    x = float(text)
    if not math.isfinite(x):
        raise ValueError("that number is too big")
    return x


def _loads(raw: bytes | str):
    """JSON in, with plain refusals (ValueError) for huge numbers and deep nesting."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, parse_constant=_no_constant, parse_int=_small_int, parse_float=_finite)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None
    except UnicodeDecodeError:
        raise ValueError("send UTF-8 text") from None


def safe_name(name) -> str:
    """A file name for showing only: no folders, no control characters, at most 120 characters."""
    name = str(name or "")[-1000:]                      # capped before any pattern touches it
    name = re.split(r"[\\/]", name)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    return (name[:120] or "file").strip()


def _words(text) -> list[str]:
    """The words of a title: letters and digits, lower case; anything else separates them."""
    return "".join(ch if ch.isalnum() else " " for ch in str(text or "").lower()).split()


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


# ---------------------------------------------------------------- finding text

def _clean(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = (" ".join(x.split()) for x in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_TEXT]


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_HTML_BREAKS = ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6")


def _html_text(raw: str) -> str:
    """The text of an HTML page in one forward pass with plain string steps: script, style and comments dropped,
    a line break for br and the end of a block, a space for any other tag. A search that finds nothing is never
    repeated, so a hostile page (thousands of unclosed tags) costs one pass, not one pass per tag."""
    low = raw.translate(_ASCII_LOWER)                    # same length as raw, so positions agree
    out: list[str] = []
    gone: set[str] = set()                               # closings already known to be missing after here
    i, n = 0, len(raw)

    def find(what: str, at: int) -> int:
        if what in gone:
            return -1
        k = low.find(what, at)
        if k < 0:
            gone.add(what)
        return k

    while i < n:
        j = raw.find("<", i)
        if j < 0:
            out.append(raw[i:])
            break
        out.append(raw[i:j])
        if low.startswith("<!--", j):
            k = find("-->", j + 4)
            if k >= 0:
                out.append(" ")
                i = k + 3
                continue
        k = find(">", j + 1)
        if k < 0:                                        # no tag ends after here: the rest is text
            out.append(raw[j:])
            break
        tag = low[j + 1:min(k, j + 12)]
        shut = tag.startswith("/")
        word = ""
        for ch in tag[1:] if shut else tag:
            if not ch.isalnum():
                break
            word += ch
        if not shut and word in ("script", "style"):
            c = find("</" + word, k + 1)
            e = find(">", c + 2) if c >= 0 else -1
            if e >= 0:
                out.append(" ")
                i = e + 1
                continue
        out.append("\n" if (not shut and word == "br") or (shut and word in _HTML_BREAKS) else " ")
        i = k + 1
    return html.unescape("".join(out))


_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_DELIM = b"()<>[]{}/%"
_PDF_SPACE = b" \t\r\n\x00\x0c"
_PDF_ESC = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}


def _pdf_string(raw: bytes) -> str:
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace")
    if len(raw) >= 4 and len(raw) % 2 == 0 and all(b == 0 for b in raw[0::2]):
        return raw.decode("utf-16-be", "replace")
    return raw.decode("latin-1")


def _pdf_literal(b: bytes, i: int) -> tuple[str, int]:
    out, depth, n = bytearray(), 1, len(b)
    i += 1
    while i < n:
        c = b[i]
        if c == 0x5C:
            i += 1
            if i >= n:
                break
            e = b[i]
            if e in _PDF_ESC:
                out.append(_PDF_ESC[e])
                i += 1
            elif 0x30 <= e <= 0x37:
                j = i
                while j < n and j < i + 3 and 0x30 <= b[j] <= 0x37:
                    j += 1
                out.append(int(b[i:j], 8) & 0xFF)
                i = j
            elif e in (0x0A, 0x0D):
                i += 1
                if e == 0x0D and i < n and b[i] == 0x0A:
                    i += 1
            else:
                out.append(e)
                i += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                i += 1
                break
        out.append(c)
        i += 1
    return _pdf_string(bytes(out)), i


class _Budget:
    """Time and text left for reading one PDF; *out* is True once either ran out."""

    def __init__(self) -> None:
        self.until = time.monotonic() + PDF_SECONDS
        self.room = PDF_TEXT
        self.out = False

    def spent(self) -> bool:
        if self.room <= 0 or time.monotonic() > self.until:
            self.out = True
        return self.out


def _pdf_content_text(b: bytes, budget: _Budget | None = None) -> str:
    """The text a content stream shows: strings before Tj, TJ, ' and ", with line breaks at moves."""
    lines: list[str] = []
    line: list[str] = []
    strings: list[str] = []
    nums: list[float] = []
    i, n, steps = 0, len(b), 0

    def newline():
        if line:
            lines.append("".join(line))
            line.clear()

    while i < n:
        steps += 1
        if budget is not None and (budget.room <= 0 or steps % 1024 == 0) and budget.spent():
            break
        c = b[i]
        if c in _PDF_SPACE or c in (0x5B, 0x5D):
            i += 1
        elif c == 0x28:
            s, i = _pdf_literal(b, i)
            strings.append(s)
        elif c == 0x3C:
            if b[i + 1:i + 2] == b"<":
                i += 2
                continue
            j = b.find(b">", i)
            if j < 0:
                break
            hexed = re.sub(rb"[^0-9A-Fa-f]", b"", b[i + 1:j])
            if len(hexed) % 2:
                hexed += b"0"
            strings.append(_pdf_string(bytes.fromhex(hexed.decode())))
            i = j + 1
        elif c == 0x3E or c in (0x7B, 0x7D):
            i += 1
        elif c == 0x2F:
            i += 1
            while i < n and b[i] not in _PDF_SPACE and b[i] not in _PDF_DELIM:
                i += 1
        elif c == 0x25:
            j = b.find(b"\n", i)
            i = n if j < 0 else j
        elif c in b"0123456789+-.":
            j = i + 1
            while j < n and b[j] in b"0123456789.":
                j += 1
            try:
                x = float(b[i:j])
                nums.append(x)
                if x < -180:                        # a wide gap inside a TJ array reads as a space
                    strings.append(" ")
            except ValueError:
                pass
            i = j
        else:
            j = i
            while j < n and b[j] not in _PDF_SPACE and b[j] not in _PDF_DELIM:
                j += 1
            j = max(j, i + 1)
            op = b[i:j]
            i = j
            if op in (b"Tj", b"TJ", b"'", b'"'):
                if op in (b"'", b'"'):
                    newline()
                shown = "".join(strings).strip(" ") if op == b"TJ" else "".join(strings)
                line.append(shown)
                if budget is not None:
                    budget.room -= len(shown) + 1
            elif op in (b"Td", b"TD"):
                if len(nums) >= 2 and nums[-1] != 0:
                    newline()
                elif line:
                    line.append(" ")
            elif op in (b"T*", b"ET", b"Tm"):
                newline()
            elif op == b"BI":
                k = b.find(b"EI", i)
                i = n if k < 0 else k + 2
            strings.clear()
            nums.clear()
    newline()
    return "\n".join(lines)


def pdf_text(data: bytes, limits: _Budget | None = None) -> str:
    """Best effort, standard library only: inflate FlateDecode streams and read their text operators.

    Reading stops when *limits* runs out of time or text (limits.out is then True); what was read is kept."""
    if b"%PDF" not in data[:1024]:
        return ""
    limits = limits or _Budget()
    out: list[str] = []
    budget, pos, count = MAX_PDF_INFLATE, 0, 0
    while budget > 0 and count < 5000 and not limits.spent():
        m = _PDF_STREAM.search(data, pos)
        if not m:
            break
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        pos, count = end + 9, count + 1
        head = data[max(0, m.start() - 600):m.start()]
        k = head.rfind(b"obj")
        head = head[k:] if k >= 0 else head
        if any(w in head for w in (b"/Image", b"/Length1", b"/Length2", b"/XRef", b"/Metadata")):
            continue
        body = data[start:end]
        if b"/FlateDecode" in head or b"/Fl " in head or b"/Fl]" in head:
            try:
                inflater = zlib.decompressobj()
                body = inflater.decompress(body, budget)
            except zlib.error:
                continue
        elif b"/Filter" in head:
            continue                                     # other filters are not read in this version
        budget -= len(body)
        if b"BT" in body:
            out.append(_pdf_content_text(body, limits))
    return "\n".join(x for x in out if x.strip())


def find_text(name: str, data: bytes) -> tuple[str, str]:
    """The text found in a file, for search (empty when none was found), and the note to show with it."""
    kind = TEXT_KINDS.get(_ext(name))
    limits = _Budget()
    try:
        if kind == "text":
            text = data.decode("utf-8-sig", "replace")
        elif kind == "html":
            text = _html_text(data.decode("utf-8-sig", "replace"))
        elif kind == "pdf":
            text = pdf_text(data, limits)
        else:
            text = ""
    except Exception:  # noqa: BLE001 - a file that will not read gives no text, never a crash
        text = ""
    text = _clean(text)
    found = 0
    for ch in text:                                      # at least 3 letters or digits; stops at the third
        if ch.isalnum() or ch == "_":
            found += 1
            if found >= 3:
                break
    text = text if found >= 3 else ""
    note = " ".join(x for x in ("" if text else NO_TEXT, PARTIAL if limits.out else "") if x)
    return text, note


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """A saved paper exists but will not read. Nothing writes over it."""


class NotFound(ValueError):
    pass


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".paper-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


PAPER_KEYS = {"id", "title", "type", "from", "date", "added", "people", "tags", "amount", "due", "expires",
              "renews_every_months", "keep_until", "follow_up", "binder", "notes", "file", "text", "text_note"}


def _read_paper(path: Path) -> dict:
    """A paper file, checked. One that is empty, will not read, or is not this app's (no My Papers marker and
    not exactly the keys this version writes, or another app's marker) raises Unreadable and is never written
    over. A file from before the marker still reads; it gets the marker on its next save."""
    try:
        if path.stat().st_size == 0:
            raise Unreadable(f"{path.name} is empty. My Papers will not write over it.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, Unreadable):
            raise
        raise Unreadable(f"{path.name} will not read. My Papers will not write over it.") from None
    if not isinstance(data, dict) or data.get("id") != path.stem or data.get("app", NAME) != NAME \
            or set(data) - {"app"} != PAPER_KEYS:
        raise Unreadable(f"{path.name} is not this app's file (not a My Papers paper). My Papers will not write "
                         f"over it.")
    data.pop("app", None)
    return data


class Index:
    """The search index: SQLite FTS5 when this Python has it, plain word matching when not."""

    def __init__(self, folder: Path, fts: bool | None) -> None:
        self.fts = fts_available() if fts is None else fts
        self.db = None
        if self.fts:
            path = folder / "index.sqlite"
            for attempt in (1, 2):
                try:
                    self.db = sqlite3.connect(path, check_same_thread=False)
                    self.db.execute("DROP TABLE IF EXISTS papers")
                    self.db.execute("CREATE VIRTUAL TABLE papers USING fts5(id UNINDEXED, title, sender, tags, "
                                    "notes, body)")
                    self.db.commit()
                    break
                except sqlite3.Error:
                    if self.db is not None:
                        self.db.close()
                    self.db = None
                    if attempt == 1:
                        path.unlink(missing_ok=True)          # only an index: built again from the papers
            self.fts = self.db is not None

    def put(self, p: dict, commit: bool = True) -> None:
        if self.db is None:
            return
        self.db.execute("DELETE FROM papers WHERE id = ?", (p["id"],))
        self.db.execute("INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?)",
                        (p["id"], p.get("title", ""), p.get("from", ""), " ".join(p.get("tags") or []),
                         p.get("notes", ""), p.get("text", "")))
        if commit:
            self.db.commit()

    def drop(self, pid: str) -> None:
        if self.db is not None:
            self.db.execute("DELETE FROM papers WHERE id = ?", (pid,))
            self.db.commit()

    def match(self, words: list[str], papers: dict) -> list[str]:
        """Ids of papers holding every word (a word also matches the start of a longer word), best first."""
        if self.db is not None:
            q = " ".join(f'"{w}"*' for w in words)
            rows = self.db.execute("SELECT id FROM papers WHERE papers MATCH ? ORDER BY rank", (q,)).fetchall()
            return [r[0] for r in rows if r[0] in papers]
        out = []
        for pid, p in papers.items():
            hay = _haystack(p)
            if all(re.search(r"(?<!\w)" + re.escape(w), hay) for w in words):
                out.append(pid)
        return out


def _haystack(p: dict) -> str:
    return "\n".join([str(p.get("title", "")), str(p.get("from", "")), " ".join(p.get("tags") or []),
                      str(p.get("notes", "")), str(p.get("text", ""))]).lower()


def fts_available() -> bool:
    try:
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        finally:
            db.close()
        return True
    except sqlite3.Error:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is no answer: the token is never sent on to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PEOPLE_DEADLINE_S = 2.0                 # the whole fetch of the Core's people list


def _people_get(host: str, port: int, prefix: str, token: str) -> bytes:
    """GET {prefix}/v1/people, all of it within PEOPLE_DEADLINE_S seconds; only a 200 answer counts, so a
    redirect is never followed."""
    end = time.monotonic() + PEOPLE_DEADLINE_S
    buf = bytearray()
    with socket.create_connection((host, port), timeout=PEOPLE_DEADLINE_S) as s:
        hosthead = f"[{host}]" if ":" in host else host
        s.sendall((f"GET {prefix}/v1/people HTTP/1.0\r\nHost: {hosthead}:{port}\r\nAuthorization: Bearer {token}"
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
    lines = head.decode("latin-1").split("\r\n")
    if not sep or len(lines[0].split()) < 2 or lines[0].split()[1] != "200":
        raise ValueError("the Core did not answer 200")
    headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:])}
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raise ValueError("a chunked answer is not read")
    if headers.get("content-length", "").isdigit():
        body = body[:int(headers["content-length"])]
    return body[:MAX_BODY]


def fetch_people(api: str | None, token: str | None) -> list[str] | None:
    """Names from the Core's shared people list, or None when it does not answer within 2 seconds."""
    if not api or not token or any(ch in token for ch in "\r\n"):
        return None
    try:
        parts = urllib.parse.urlsplit(api.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or host not in ("127.0.0.1", "localhost", "::1") or not port \
            or parts.username or parts.password or parts.query \
            or not all(ch.isalnum() or ch in "/-_." for ch in parts.path):
        return None                                     # this app talks to nothing but this computer
    try:
        data = _loads(_people_get(host, port, parts.path.rstrip("/"), token))
    except Exception:  # noqa: BLE001 - any failure means: use the app's own list
        return None
    if isinstance(data, dict):
        data = data.get("people")
    if not isinstance(data, list):
        return None
    names = [str(x.get("name")).strip()[:60] for x in data if isinstance(x, dict) and x.get("name")]
    return [n for n in names if n]


class Papers:
    """The papers, their stored files and the search index."""

    def __init__(self, folder: Path | str, today=date.today, people=None, fts: bool | None = None) -> None:
        self.folder = Path(folder)
        self.pdir = self.folder / "papers"
        self.fdir = self.folder / "files"
        self.pdir.mkdir(parents=True, exist_ok=True)
        self.fdir.mkdir(parents=True, exist_ok=True)
        self.today = today                      # a function, so a test can move the day
        self.people_source = people or (lambda: None)
        self._people_cache: tuple[float, list | None] = (-1e9, None)
        self.lock = threading.RLock()
        self.adding: dict[str, int] = {}       # sha256 of files being added right now; never deleted
        self.papers: dict[str, dict] = {}
        self.unreadable: list[str] = []
        self.unreadable_why: list[str] = []
        for path in sorted(self.pdir.glob("*.json")):
            try:
                p = _read_paper(path)
            except Unreadable as exc:
                self.unreadable.append(path.name)
                self.unreadable_why.append(str(exc))
                continue
            self.papers[p["id"]] = p
        self.index = Index(self.folder, fts)
        for p in self.papers.values():
            self.index.put(p, commit=False)
        if self.index.db is not None:
            self.index.db.commit()
        self.stamp = _now()

    # ------------------------------------------------------------ helpers
    def _save(self, p: dict) -> None:
        _write_json(self.pdir / f"{p['id']}.json", {"app": NAME, **p})
        self.papers[p["id"]] = p
        self.index.put(p)
        self.stamp = _now()

    def _new_id(self) -> str:
        while True:
            pid = "p" + secrets.token_hex(6)
            if pid not in self.papers and not (self.pdir / f"{pid}.json").exists():
                return pid

    def _find(self, paper, said: list | None = None) -> dict:
        """A paper by id, by its exact title (case and spaces ignored), or by the one title where every word given
        is a whole word or the start (3 letters or more) of one; never letters inside a word. A match that is not
        exact adds "Using '<title>'." to *said*. None or several: an error that lists them; nothing changes."""
        key = _text(paper, "paper", 200)
        if not key:
            raise ValueError("say which paper, by its title or id")
        if ID_RE.fullmatch(key) and key in self.papers:
            return self.papers[key]
        # Its title: exact first (case and spaces ignored), then the one title that holds the words.
        want = "".join(key.split()).lower()
        flat = {pid: "".join(str(p.get("title", "")).split()).lower() for pid, p in self.papers.items()}
        named = [self.papers[pid] for pid, t in flat.items() if t == want]
        partial = False
        if not named and want:
            given = _words(key)
            named = [p for p in self.papers.values() if given and all(
                any(w == t or (len(w) >= 3 and t.startswith(w)) for t in _words(p.get("title"))) for w in given)]
            partial = True
        if not named:
            raise NotFound(f"there is no paper called {key!r}; find_papers looks for words inside papers")
        if len(named) > 1:
            named.sort(key=lambda p: (str(p.get("title", "")).lower(), str(p.get("date") or "")))
            same = len({str(p.get("title", "")).lower() for p in named}) < len(named)
            said = ", ".join(f"{p.get('title')} ({p.get('date') or p.get('added', '')[:10]}"
                             + (f", id {p['id']}" if same else "") + ")" for p in named[:15])
            raise ValueError(f"More than one paper matches {key!r}: {said}. Say which.")
        if partial and said is not None:
            said.append(f"Using {str(named[0].get('title'))!r}.")
        return named[0]

    def _fresh(self, p: dict) -> dict:
        """The paper as it is on disk now; refuses when that file will not read."""
        path = self.pdir / f"{p['id']}.json"
        if not path.exists():
            raise NotFound(f"the paper {p.get('title')!r} is gone")
        return _read_paper(path)

    def people(self, kept_only: bool = False) -> dict:
        """The Core's people (kept 60 seconds, a failure 30), else the names on the papers. *kept_only* never
        asks the Core: for use while holding the lock."""
        now = time.monotonic()
        at, names = self._people_cache
        if not kept_only and now - at > (60 if names is not None else 30):
            names = self.people_source()
            self._people_cache = (time.monotonic(), names)
        if names is not None:
            return {"source": "shared", "names": names}
        own = sorted({n for p in self.papers.values() for n in (p.get("people") or [])}, key=str.lower)
        return {"source": "own", "names": own}

    def _person_names(self, value) -> list[str]:
        names = _names(value, "people", 60, 10)
        known = {n.lower(): n for n in self.people(kept_only=True)["names"]}
        return [known.get(n.lower(), n) for n in names]

    def begin_file(self, name: str, data: bytes) -> dict:
        """The file's record; its sha256 is marked as being added until end_file, so no delete removes it."""
        if len(data) > MAX_FILE:
            raise ValueError(f"a file is at most {MAX_FILE // (1024 * 1024)} MB")
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            self.adding[sha] = self.adding.get(sha, 0) + 1
        name = safe_name(name)
        return {"name": name, "size": len(data), "sha256": sha, "kind": FILE_KINDS.get(_ext(name), "other")}

    def end_file(self, sha: str) -> None:
        with self.lock:
            left = self.adding.get(sha, 0) - 1
            if left > 0:
                self.adding[sha] = left
            else:
                self.adding.pop(sha, None)

    def _write_file(self, sha: str, data: bytes) -> None:
        """Store the file if it is not there yet. Call with the lock held, in the same step as saving its paper."""
        target = self.fdir / sha
        if target.exists() and target.stat().st_size == len(data):
            return
        fd, tmp = tempfile.mkstemp(dir=self.fdir, prefix=".file-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _drop_file_if_unused(self, sha: str | None) -> bool | None:
        """Delete a stored file no paper uses: True when deleted, False when a paper uses it, None when kept
        because a paper file will not read (it may point to it)."""
        with self.lock:
            if not sha or not SHA_RE.fullmatch(sha):
                return False
            if sha in self.adding or any((p.get("file") or {}).get("sha256") == sha for p in self.papers.values()):
                return False
            if self.unreadable or any(path.stem not in self.papers for path in self.pdir.glob("*.json")):
                return None
            (self.fdir / sha).unlink(missing_ok=True)
            return True

    def _fields(self, p: dict, f: dict, today: date) -> list[str]:
        """Apply the given fields to *p*; returns the names of the fields changed."""
        changed = []
        if "title" in f and f["title"] is not None:
            title = _text(f["title"], "title", 200)
            if not title:
                raise ValueError("a paper needs a title")
            p["title"] = title
            changed.append("title")
        if f.get("type") is not None:
            p["type"] = _type(f["type"])
            changed.append("type")
        if f.get("from") is not None:
            p["from"] = _text(f["from"], "from", 120)
            changed.append("from")
        for key in DATE_FIELDS:
            if f.get(key) is not None:
                d = _day(f[key], key, today)
                p[key] = d.isoformat() if d else ""
                changed.append(key.replace("_", " "))
        if f.get("people") is not None:
            p["people"] = self._person_names(f["people"])
            changed.append("people")
        if f.get("tags") is not None:
            p["tags"] = [t.lower() for t in _names(f["tags"], "tags", 40, 20)]
            changed.append("tags")
        if f.get("amount") is not None:
            value = _amount(f["amount"])
            cur = _text(f.get("currency"), "currency", 10, (p.get("amount") or {}).get("currency", ""))
            p["amount"] = {"value": value, "currency": cur.upper()} if value is not None else None
            changed.append("amount")
        elif f.get("currency") is not None and p.get("amount"):
            p["amount"]["currency"] = _text(f["currency"], "currency", 10).upper()
            changed.append("amount")
        if f.get("renews_every_months") is not None:
            v = f["renews_every_months"]
            p["renews_every_months"] = 0 if v in ("", 0, "0", "none", "never") else \
                _whole(v, "renews_every_months", 1, 120)
            changed.append("renewal")
        if f.get("binder") is not None:
            p["binder"] = _text(f["binder"], "binder", 60)
            changed.append("binder")
        if f.get("notes") is not None:
            p["notes"] = _text(f["notes"], "notes", 4000, lines=True)
            changed.append("notes")
        return changed

    @staticmethod
    def keep_hint(p: dict) -> str:
        return TAX_HINT if p.get("type") == "tax" else ""

    # ------------------------------------------------------------ changes
    def add(self, fields: dict, file_name=None, file_data: bytes | None = None) -> dict:
        today = self.today()
        self.people()                                   # asked before any lock; later only the kept list
        f = dict(fields)
        if file_data is not None and not _text(f.get("title"), "title", 200):
            f["title"] = os.path.splitext(safe_name(file_name))[0] or "Untitled paper"
        if not _text(f.get("title"), "title", 200):
            raise ValueError("a paper needs a title")
        p = {"id": "", "title": "", "type": "other", "from": "", "date": "", "added": _now(), "people": [],
             "tags": [], "amount": None, "due": "", "expires": "", "renews_every_months": 0, "keep_until": "",
             "follow_up": "", "binder": "", "notes": "", "file": None, "text": "", "text_note": ""}
        self._fields(p, f, today)
        suggested = False
        if p["type"] == "tax" and not p["keep_until"] and _as_date(p["date"]):
            p["keep_until"] = _add_months(_as_date(p["date"]), 36).isoformat()
            suggested = True
        meta = self.begin_file(file_name or "file", file_data) if file_data is not None else None
        try:
            if meta is not None:
                p["text"], p["text_note"] = find_text(meta["name"], file_data)    # slow, so outside the lock
            with self.lock:                     # the stored file and the paper pointing at it: one step
                if meta is None:
                    same = self._same_record(p)
                    if same is not None:
                        return {"id": same["id"], "text": f"That paper is already there: {same['title']}. "
                                                          "Nothing was added."}
                else:
                    self._write_file(meta["sha256"], file_data)
                    p["file"] = meta
                p["id"] = self._new_id()
                self._save(p)
        finally:
            if meta is not None:
                self.end_file(meta["sha256"])
        text = f"Added {p['title']} ({p['type']})"
        if p["file"]:
            text += f" with the file {p['file']['name']}"
            if p["text"]:
                text += f"; found {_plural(len(p['text']), 'character')} of text"
        text += "."
        if p["file"] and not p["text"]:
            text += " " + NO_TEXT
        if PARTIAL in p["text_note"]:
            text += " " + PARTIAL
        if p.get("binder"):
            text += f" The paper copy is in {p['binder']}."
        if suggested:
            text += f" Keep until {p['keep_until']} (suggested). {TAX_HINT}"
        return {"id": p["id"], "text": text}

    def _same_record(self, p: dict) -> dict | None:
        """A record without a file with the same title, type and notes, added in the last 10 minutes."""
        since = datetime.now() - timedelta(seconds=SAME_PAPER_SECONDS)
        for q in self.papers.values():
            if q.get("file") or str(q.get("title", "")).lower() != p["title"].lower():
                continue
            if q.get("type") != p["type"] or q.get("notes", "") != p["notes"]:
                continue
            try:
                if datetime.fromisoformat(str(q.get("added", ""))) >= since:
                    return q
            except (ValueError, TypeError):             # no date, or a date with a time zone (imported)
                continue
        return None

    def change(self, paper, fields: dict) -> str:
        today = self.today()
        said: list[str] = []
        self.people()                                   # asked before the lock; inside it only the kept list
        with self.lock:
            p = self._fresh(self._find(paper, said))
            changed = self._fields(p, fields, today)
            if not changed:
                raise ValueError("say what to change: title, type, from, date, people, tags, amount, due, expires, "
                                 "renews_every_months, keep_until, follow_up, binder or notes")
            self._save(p)
        return " ".join(said + [f"Changed {p['title']} ({', '.join(dict.fromkeys(changed))}). "
                                + self._dates_line(p)])

    def remove(self, paper) -> str:
        said: list[str] = []
        with self.lock:
            p = self._find(paper, said)
            path = self.pdir / f"{p['id']}.json"
            if path.exists():
                _read_paper(path)                       # an unreadable file is left alone
                path.unlink()
            self.papers.pop(p["id"], None)
            self.index.drop(p["id"])
            sha = (p.get("file") or {}).get("sha256")
            dropped = self._drop_file_if_unused(sha)
            self.stamp = _now()
        text = f"Removed the paper {p.get('title')}."
        if p.get("file") and dropped is not None:
            text += " Its stored file was deleted too." if dropped else " Its stored file stays: another paper uses it."
        return " ".join(said + [text])

    # ------------------------------------------------------------ reading
    def _dates_line(self, p: dict) -> str:
        bits = []
        for key, word in (("due", "due"), ("expires", "expires"), ("follow_up", "follow up"),
                          ("keep_until", "keep until")):
            if p.get(key):
                bits.append(f"{word} {p[key]}")
        n = _safe_int(p.get("renews_every_months"))
        if n:
            nxt = self.next_renewal(p, self.today())
            bits.append(f"renews every {_plural(n, 'month')}" + (f", next {nxt.isoformat()}" if nxt else ""))
        return ("Dates: " + "; ".join(bits) + ".") if bits else "No dates set."

    def summary(self, p: dict, line: str = "") -> dict:
        f = p.get("file") or {}
        return {"id": p["id"], "title": p.get("title", ""), "type": p.get("type", ""), "from": p.get("from", ""),
                "date": p.get("date", ""), "added": p.get("added", ""), "people": p.get("people") or [],
                "tags": p.get("tags") or [], "due": p.get("due", ""), "binder": p.get("binder", ""),
                "file_name": f.get("name", ""), "file_kind": f.get("kind", ""), "has_text": bool(p.get("text")),
                "line": line}

    def search(self, q="", type=None, person=None, tag=None, year=None, limit=200) -> list[dict]:  # noqa: A002
        words = [w.lower() for w in re.findall(r"\w+", _text(q, "words", 200))][:12]
        kind = _type(type) if type not in (None, "") else None
        person = _text(person, "person", 60).lower()
        tag = _text(tag, "tag", 40).lower()
        year = _whole(year, "year", 1900, 2200) if year not in (None, "") else None
        with self.lock:
            ids = self.index.match(words, self.papers) if words else \
                sorted(self.papers, key=lambda i: (self.papers[i].get("date") or self.papers[i].get("added", "")),
                       reverse=True)
            out = []
            for pid in ids:
                p = self.papers.get(pid)
                if p is None or (kind and p.get("type") != kind):
                    continue
                if person and person not in [str(x).lower() for x in p.get("people") or []]:
                    continue
                if tag and tag not in [str(x).lower() for x in p.get("tags") or []]:
                    continue
                if year and str(p.get("date") or p.get("added", ""))[:4] != str(year):
                    continue
                out.append(self.summary(p, matching_line(p, words)))
                if len(out) >= limit:
                    break
        return out

    def next_renewal(self, p: dict, today: date) -> date | None:
        n = _safe_int(p.get("renews_every_months"))
        start = _as_date(p.get("date")) or _as_date(p.get("added"))
        if not n or not start:
            return None
        k = max(1, ((today.year - start.year) * 12 + today.month - start.month) // n)
        for _ in range(4):
            d = _add_months(start, k * n)
            if d >= today:
                return d
            k += 1
        return None

    def coming(self, days: int = DEFAULT_DAYS) -> list[dict]:
        today = self.today()
        last = today + timedelta(days=days)
        out = []
        with self.lock:
            for p in self.papers.values():
                seen = set()
                for key, word in (("due", "due"), ("expires", "expires"), ("follow_up", "follow up")):
                    d = _as_date(p.get(key))
                    if d and d <= last:
                        out.append((d, word, p))
                        seen.add(d)
                r = self.next_renewal(p, today)
                if r and r <= last and r not in seen:
                    out.append((r, "renews", p))
        out.sort(key=lambda x: (x[0], str(x[2].get("title", "")).lower()))
        return [{**self.summary(p), "paper_date": p.get("date", ""), "date": d.isoformat(), "what": w,
                 "days": (d - today).days, "overdue": d < today} for d, w, p in out]

    def counts(self) -> dict:
        with self.lock:
            c = {t: 0 for t in TYPES}
            for p in self.papers.values():
                c[p.get("type") if p.get("type") in c else "other"] += 1
        return c

    def info(self) -> dict:
        ppl = self.people()
        return {"types": list(TYPES), "counts": self.counts(), "total": len(self.papers), "fts": self.index.fts,
                "unreadable": list(self.unreadable), "people": ppl["names"], "people_source": ppl["source"],
                "tax_hint": TAX_HINT, "today": self.today().isoformat(), "last_updated": self.stamp,
                "max_file_mb": MAX_FILE // (1024 * 1024)}

    def paper(self, pid: str) -> dict:
        p = self.papers.get(pid) if ID_RE.fullmatch(pid or "") else None
        if p is None:
            raise NotFound("there is no paper with that id")
        nxt = self.next_renewal(p, self.today())
        return {**p, "next_renewal": nxt.isoformat() if nxt else "", "keep_hint": self.keep_hint(p)}

    # ------------------------------------------------------------ MCP tools
    def find_papers(self, words=None, type=None, person=None, tag=None, year=None, limit=None) -> str:  # noqa: A002
        n = _whole(limit, "limit", 1, 50) if limit not in (None, "") else 20
        found = self.search(words or "", type, person, tag, year, n)
        if not found:
            return "No papers match." if self.papers else "There are no papers yet."
        lines = []
        for s in found:
            head = f"- {s['title']} ({s['type']}, {s['date'] or 'added ' + s['added'][:10]}"
            head += f", from {s['from']}" if s["from"] else ""
            head += f"; id {s['id']})"
            if s["line"]:
                head += f": \"{s['line']}\""
            lines.append(head)
        return f"{_plural(len(found), 'paper')} found:\n" + "\n".join(lines)

    def read_paper(self, paper) -> str:
        said: list[str] = []
        p = self._find(paper, said)
        f = p.get("file") or {}
        rows = [f"Title: {p.get('title')}", f"Id: {p['id']}", f"Type: {p.get('type')}"]
        for key, word in (("from", "From"), ("date", "Date on the paper"), ("due", "Due"), ("expires", "Expires"),
                          ("follow_up", "Follow up"), ("keep_until", "Keep until"), ("binder", "Paper copy in")):
            if p.get(key):
                rows.append(f"{word}: {p[key]}")
        rows.append(f"Added: {str(p.get('added', ''))[:10]}")
        if p.get("people"):
            rows.append("People: " + ", ".join(p["people"]))
        if p.get("tags"):
            rows.append("Tags: " + ", ".join(p["tags"]))
        a = p.get("amount")
        if isinstance(a, dict) and a.get("value") is not None:
            rows.append(f"Amount: {a['value']:.2f} {a.get('currency', '')}".rstrip())
        if _safe_int(p.get("renews_every_months")):
            nxt = self.next_renewal(p, self.today())
            rows.append(f"Renews every {_plural(_safe_int(p['renews_every_months']), 'month')}"
                        + (f", next {nxt.isoformat()}" if nxt else ""))
        if p.get("notes"):
            rows.append(f"Notes: {p['notes']}")
        rows.append(f"File: {f.get('name')} ({f.get('kind')}, {f.get('size')} bytes)" if f else
                    "File: none (a record of where the paper copy is)")
        text = p.get("text") or ""
        if text:
            more = f" (the first {READ_TEXT} of {len(text)} characters)" if len(text) > READ_TEXT else ""
            if p.get("text_note"):
                rows.append(p["text_note"])
            rows.append(f"Text found in the file{more}:\n{text[:READ_TEXT]}")
        elif f:
            rows.append(p.get("text_note") or NO_TEXT)
        return " ".join(said + ["\n".join(rows)])

    def coming_up(self, days=None) -> str:
        n = _whole(days, "days", 1, 366) if days not in (None, "") else DEFAULT_DAYS
        items = self.coming(n)
        if not items:
            return f"Nothing is due, expiring or renewing in the next {_plural(n, 'day')}, and nothing is overdue."
        lines = []
        for c in items:
            when = (f"{_plural(-c['days'], 'day')} overdue" if c["overdue"] else "today" if c["days"] == 0 else
                    f"in {_plural(c['days'], 'day')}")
            lines.append(f"- {c['date']} ({when}): {c['title']} {c['what']} ({c['type']}; id {c['id']})")
        return f"Coming up in the next {_plural(n, 'day')}, and anything overdue, soonest first:\n" + "\n".join(lines)

    def add_paper_record(self, title=None, **fields) -> dict:
        return self.add({"title": title, **_tool_fields(fields)})

    def change_paper(self, paper=None, **fields) -> str:
        return self.change(paper, _tool_fields(fields))

    def list_types_and_counts(self) -> str:
        c = self.counts()
        if not self.papers:
            return "There are no papers yet." + "".join(f"\nLeft as it is: {u}" for u in self.unreadable_why[:20])
        rows = [f"- {t}: {n}" for t, n in c.items() if n]
        none = sum(1 for p in self.papers.values() if p.get("file") and not p.get("text"))
        tail = f"\n{_plural(none, 'stored file')} with no text found (scans or pictures)." if none else ""
        bad = f"\n{_plural(len(self.unreadable), 'paper file')} would not read and were left as they are: " \
            + " ".join(self.unreadable_why[:20]) if self.unreadable else ""
        return f"{_plural(len(self.papers), 'paper')} in all:\n" + "\n".join(rows) + tail + bad

    def keep_or_shred(self) -> str:
        today = self.today()
        past = sorted((p for p in self.papers.values() if (_as_date(p.get("keep_until")) or date.max) < today),
                      key=lambda p: p.get("keep_until"))
        if not past:
            return "No paper is past its keep-until date."
        lines = [f"- {p.get('title')} ({p.get('type')}), keep until {p.get('keep_until')}"
                 + (f", paper copy in {p['binder']}" if p.get("binder") else "") + f"; id {p['id']}" for p in past]
        return (f"{_plural(len(past), 'paper')} past the keep-until date. This is a list only; nothing was deleted. "
                f"Check before you shred: {TAX_HINT}\n" + "\n".join(lines))

    def remove_paper(self, paper=None) -> str:
        return self.remove(paper)

    # ------------------------------------------------------------ export and import
    def export_to(self, fileobj) -> int:
        with self.lock:
            papers = [dict(p) for p in self.papers.values()]
        with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("my-papers-export.json", json.dumps({"format": "my-papers/1", "papers": len(papers),
                                                             "made": _now()}))
            done = set()
            for p in papers:
                z.writestr(f"papers/{p['id']}.json", json.dumps(p, ensure_ascii=False, indent=1))
                sha = (p.get("file") or {}).get("sha256")
                if sha and SHA_RE.fullmatch(sha) and sha not in done and (self.fdir / sha).exists():
                    z.write(self.fdir / sha, f"files/{sha}")
                    done.add(sha)
        return len(papers)

    def import_zip(self, raw: bytes) -> dict:
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
            infos = z.infolist()
        except (zipfile.BadZipFile, ValueError, OSError):
            raise ValueError("that is not a zip file") from None
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"a zip may hold at most {MAX_ZIP_ENTRIES} files")
        if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
            raise ValueError(f"a zip may unpack to at most {MAX_ZIP_TOTAL // (1024 * 1024)} MB")
        for i in infos:
            n = i.filename
            parts = re.split(r"[\\/]", n)
            if (n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or ".." in parts or "\x00" in n):
                raise ValueError(f"the zip holds an unsafe name ({safe_name(n)}); nothing was imported")
        files = {i.filename: i for i in infos if not i.is_dir()}

        def read(info, most):
            if info.file_size > most:
                raise ValueError("too big")
            with z.open(info) as f:
                data = f.read(most + 1)
            if len(data) > most:
                raise ValueError("too big")
            return data

        imported, skipped, notes = 0, 0, []
        if "my-papers-export.json" in files:
            for name, info in files.items():
                if not re.fullmatch(r"papers/p[0-9a-f]{12}\.json", name):
                    continue
                try:
                    p = _loads(read(info, MAX_TEXT * 4 + MAX_SIDECAR))
                    if not isinstance(p, dict) or p.get("id") != name[7:-5]:
                        raise ValueError("not a paper")
                    if p["id"] in self.papers or (self.pdir / name[7:]).exists():
                        skipped += 1
                        continue
                    fields = {k: p.get(k) for k in ("title", "type", "from", "people", "tags", "renews_every_months",
                                                    "binder", "notes", *DATE_FIELDS) if p.get(k) not in (None, "")}
                    if isinstance(p.get("amount"), dict):
                        fields["amount"] = p["amount"].get("value")
                        fields["currency"] = p["amount"].get("currency")
                    clean = {"id": p["id"], "title": "", "type": "other", "from": "", "date": "",
                             "added": _text(p.get("added"), "added", 40) or _now(), "people": [], "tags": [],
                             "amount": None, "due": "", "expires": "", "renews_every_months": 0, "keep_until": "",
                             "follow_up": "", "binder": "", "notes": "", "file": None, "text": "", "text_note": ""}
                    self._fields(clean, fields, self.today())
                    if not clean["title"]:
                        raise ValueError("no title")
                    f = p.get("file")
                    if isinstance(f, dict) and f.get("sha256"):
                        sha = str(f["sha256"])
                        entry = files.get(f"files/{sha}") if SHA_RE.fullmatch(sha) else None
                        if entry is None:
                            raise ValueError("its file is missing from the zip")
                        data = read(entry, MAX_FILE)
                        if hashlib.sha256(data).hexdigest() != sha:
                            raise ValueError("its file does not match")
                        meta = self.begin_file(str(f.get("name") or "file"), data)
                    else:
                        meta, data = None, None
                    try:
                        if meta is not None:
                            clean["text"], clean["text_note"] = find_text(meta["name"], data)
                        with self.lock:                 # the stored file and the paper pointing at it: one step
                            if clean["id"] in self.papers or (self.pdir / name[7:]).exists():
                                skipped += 1
                                continue
                            if meta is not None:
                                self._write_file(meta["sha256"], data)
                                clean["file"] = meta
                            self._save(clean)
                    finally:
                        if meta is not None:
                            self.end_file(meta["sha256"])
                    imported += 1
                except ZIP_ERRORS as exc:
                    skipped += 1
                    notes.append(f"{safe_name(name)}: {exc}")
        else:
            known = {(p.get("file") or {}).get("sha256") for p in self.papers.values()}
            for name, info in files.items():
                if name.lower().endswith(".json") and name[:-5] in files:
                    continue                                # a sidecar, read with its file
                if name.startswith("__MACOSX/") or safe_name(name).startswith("."):
                    continue
                try:
                    data = read(info, MAX_FILE)
                    sha = hashlib.sha256(data).hexdigest()
                    if sha in known:
                        skipped += 1
                        continue
                    fields = {}
                    side = files.get(name + ".json")
                    if side is not None:
                        s = _loads(read(side, MAX_SIDECAR))
                        if not isinstance(s, dict):
                            raise ValueError("its sidecar is not a JSON object")
                        fields = _tool_fields({k: v for k, v in s.items()
                                               if k != "title" and (k in FIELD_NAMES or k == "sender")})
                        if s.get("title") is not None:
                            fields["title"] = s["title"]
                    self.add(fields, safe_name(name), data)
                    known.add(sha)
                    imported += 1
                except ZIP_ERRORS as exc:
                    skipped += 1
                    notes.append(f"{safe_name(name)}: {exc}")
        return {"imported": imported, "skipped": skipped, "notes": notes[:20],
                "text": f"Imported {_plural(imported, 'paper')}; skipped {skipped}."}


ZIP_ERRORS = (ValueError, TypeError, RuntimeError, zipfile.BadZipFile, zlib.error, NotImplementedError, OSError,
              EOFError)
FIELD_NAMES = ("title", "type", "from", "date", "people", "tags", "amount", "currency", "due", "expires",
               "renews_every_months", "keep_until", "follow_up", "binder", "notes")


def _tool_fields(fields: dict) -> dict:
    """Tool arguments to fields; the tool says sender for from (a word some callers can not use)."""
    out = dict(fields)
    if "sender" in out:
        out["from"] = out.pop("sender")
    bad = [k for k in out if k not in FIELD_NAMES]
    if bad:
        raise TypeError(f"unknown field {bad[0]!r}")
    return out


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value) if not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def matching_line(p: dict, words: list[str]) -> str:
    """The first line that holds one of the words, from the title, sender, notes or found text."""
    if not words:
        return ""
    for source in (str(p.get("notes", "")), str(p.get("text", "")), str(p.get("from", "")),
                   " ".join(p.get("tags") or []), str(p.get("title", ""))):
        for line in source.split("\n"):
            low = line.lower()
            for w in words:
                at = low.find(w)
                if at >= 0:
                    line = line.strip()
                    if len(line) <= 160:
                        return line
                    s = max(0, at - 60)
                    return ("..." if s else "") + line[s:s + 160].strip() + "..."
    return ""


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_PAPER = {"paper": _p("string", "The paper's title, like City Water bill. An id works too but is not needed.")}
_FIELDS = {
    "title": _p("string", "What the paper is, like City Water bill March."),
    "type": _p("string", "One of: " + ", ".join(TYPES) + ". Default other."),
    "sender": _p("string", "Who sent it or made it, like City Water."),
    "date": _p("string", "The date on the paper, like 2027-03-31."),
    "people": _p("string", "Who it is about, names separated by commas."),
    "tags": _p("string", "Short words to find it by, separated by commas."),
    "amount": _p("number", "Optional: the amount on it, like 82.50."),
    "currency": _p("string", "Optional: the currency of the amount, like USD."),
    "due": _p("string", "Optional: the date it is due, like 2027-03-31. An empty value clears it."),
    "expires": _p("string", "Optional: the date it expires. An empty value clears it."),
    "renews_every_months": _p("integer", "Optional: it renews every this many months, counted from the date on "
                                         "the paper. 0 for never."),
    "keep_until": _p("string", "Optional: keep it until this date. For tax papers it is suggested as the date on "
                               "the paper plus 3 years."),
    "follow_up": _p("string", "Optional: a date to look at it again. An empty value clears it."),
    "binder": _p("string", "Optional: where the paper copy is, like blue binder 2."),
    "notes": _p("string", "Optional: notes, like what it is for."),
}
TOOLS = [
    ("find_papers", "Use only to search, when the owner asks if we have a paper, where one is, or what papers there "
     "are: finds papers by words in them and by type, person, tag or year, with each one's title, type, date and "
     "the line that matched.",
     {"words": _p("string", "Words to look for in the title, sender, tags, notes and the text of the file."),
      "type": _p("string", "Optional: only this type of paper, like bill or tax."),
      "person": _p("string", "Optional: only papers about this person."),
      "tag": _p("string", "Optional: only papers with this tag."),
      "year": _p("integer", "Optional: only papers dated in this year."),
      "limit": _p("integer", "Optional: at most this many, 1 to 50. Default 20.")}, []),
    ("read_paper", "Read a paper named by its title; it finds the paper itself, so do not look it up first. It gives "
     "all its fields and up to 4000 characters of its text, so you can answer what it says and quote it.", dict(_PAPER), ["paper"]),
    ("coming_up", "List papers with a due date, expiry, renewal or follow-up date in the next days (60 unless "
     "asked), and anything overdue, soonest first.",
     {"days": _p("integer", "Optional: how many days ahead, 1 to 366. Default 60.")}, []),
    ("add_paper_record", "File a new paper record whenever the owner says to file, note or keep track of a paper, like where "
     "a paper copy is kept. It saves the title, type, sender, dates, people, tags, amount and notes, "
     "without a file.",
     dict(_FIELDS), ["title"]),
    ("change_paper", "Change a paper named by its title; it finds the paper itself, so do not look it up first. It sets "
     "or clears its due date, its type, its tags, where the paper copy is, or its other fields.",
     {**_PAPER, **_FIELDS, "title": _p("string", "Optional: a new title for the paper.")}, ["paper"]),
    ("list_types_and_counts", "Count the papers of each type, and say how many stored files had no text found.",
     {}, []),
    ("keep_or_shred", "List the papers that are past their keep-until date. It is a list only and deletes nothing.",
     {}, []),
    ("remove_paper", "Delete a paper named by its title; it finds the paper itself, so do not look it up first. Its "
     "stored file goes too when no other paper uses that file.", dict(_PAPER),
     ["paper"]),
]
CHANGES = ["add_paper_record", "change_paper", "remove_paper"]
DELETES = ["remove_paper"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def mcp_answer(app: Papers, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _err(None, -32600, "Not a JSON-RPC 2.0 request.")
    if "id" not in msg:
        return None
    mid, method = msg["id"], msg["method"]
    if not (mid is None or isinstance(mid, (str, int, float))) or isinstance(mid, bool):
        return _err(None, -32600, "The id must be a string or a number.")
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _err(mid, -32602, "params must be an object.")

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
            return _err(mid, -32602, f"No tool named {str(name)[:60]!r}.")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _err(mid, -32602, "arguments must be an object.")
        try:
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
    return _err(mid, -32601, f"My Papers does not know {method[:60]!r}.")


# --------------------------------------------------------------- HTTP

def _attachment(name: str) -> str:
    plain = re.sub(r'[^A-Za-z0-9._ -]', "_", name).strip() or "file"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


class Handler(BaseHTTPRequestHandler):
    server_version = "MyPapers/1"
    app: Papers
    token: str
    timeout = 30        # seconds; a stalled sender is cut off

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", extra: dict | None = None):
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
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {most // 1024} KB.")
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
                got = self.rfile.read1(min(left, 256 * 1024))
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

    def _raw(self, most: int = MAX_BODY, too_big: str | None = None) -> bytes:
        try:
            n = self._length(most)
        except ValueError:
            self._drain()
            if too_big and (self.headers.get("Content-Length") or "").strip().isdigit():
                raise ValueError(too_big) from None
            raise
        raw = bytearray()
        end = time.monotonic() + BODY_WAIT_S + n / (8 * 1024 * 1024)
        try:
            while len(raw) < n:                      # the whole body within its time, however it trickles in
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
        return bytes(raw)

    def _body(self, most: int = MAX_BODY, too_big: str | None = None):
        raw = self._raw(most, too_big)
        return _loads(raw) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode())

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        self._drain(REFUSED_DRAIN, REFUSED_WAIT_S)     # at most 64 KB within 2 seconds, then the answer
        self._json(401, {"error": "My Papers needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"My Papers stopped on this request with an error "
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
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        try:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, max_num_fields=20).items()}
        except ValueError:
            return self._json(400, {"error": "Too many query fields."})
        app = self.app
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/updated":
                return self._json(200, {"last_updated": app.stamp})
            if path == "/info":
                return self._json(200, app.info())
            if path == "/papers":
                return self._json(200, {"papers": app.search(q.get("q", ""), q.get("type"), q.get("person"),
                                                             q.get("tag"), q.get("year")), "fts": app.index.fts})
            if path == "/coming":
                days = _whole(q["days"], "days", 1, 366) if q.get("days") else DEFAULT_DAYS
                return self._json(200, {"days": days, "today": app.today().isoformat(), "items": app.coming(days)})
            if path.startswith("/paper/"):
                return self._json(200, app.paper(path[7:]))
            if path.startswith("/file/"):
                return self._file(path[6:])
            if path == "/export":
                return self._export()
        except NotFound as exc:
            return self._json(404, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "My Papers has no page here."})

    def _file(self, pid: str):
        p = self.app.papers.get(pid) if ID_RE.fullmatch(pid) else None
        f = (p or {}).get("file") or {}
        sha = str(f.get("sha256") or "")
        path = self.app.fdir / sha
        if not SHA_RE.fullmatch(sha) or not path.is_file():
            return self._json(404, {"error": "That paper has no stored file."})
        self._send(200, path.read_bytes(), "application/octet-stream",
                   {"Content-Disposition": _attachment(safe_name(f.get("name"))),
                    "Content-Security-Policy": "sandbox"})

    def _export(self):
        fd, tmp = tempfile.mkstemp(dir=self.app.folder, prefix=".export-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w+b") as f:
                self.app.export_to(f)
                size = f.tell()
                f.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", _attachment(f"my-papers-{self.app.today().isoformat()}.zip"))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := f.read(1024 * 1024):
                        self.wfile.write(chunk)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/mcp":
            return self._mcp()
        if path not in ("/api/add", "/api/update", "/api/remove", "/api/import"):
            return self._json(404, {"error": "My Papers has no page here."})
        app = self.app
        try:
            if path == "/api/import":
                out = app.import_zip(self._raw(MAX_ZIP))
                out["last_updated"] = app.stamp
                return self._json(200, out)
            body = self._body(MAX_ADD_BODY, TOO_BIG) if path == "/api/add" else self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            fields = {k: body[k] for k in FIELD_NAMES if k in body}
            if path == "/api/add":
                f = body.get("file")
                if f is None:
                    out = app.add(fields)
                else:
                    if not isinstance(f, dict) or not isinstance(f.get("data"), str):
                        raise ValueError("file is {name, data} with the data as base64")
                    if len(f["data"]) > MAX_FILE * 4 // 3 + 8:
                        raise ValueError(f"a file is at most {MAX_FILE // (1024 * 1024)} MB")
                    try:
                        data = base64.b64decode(f["data"], validate=True)
                    except ValueError:
                        raise ValueError("the file data is not base64") from None
                    out = app.add(fields, _text(f.get("name"), "file name", 400, "file"), data)
            elif path == "/api/update":
                out = {"text": app.change(body.get("id"), fields)}
            else:
                out = {"text": app.remove(body.get("id"))}
            out["last_updated"] = app.stamp
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except NotFound as exc:
            self._json(404, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self.close_connection = True
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
            self.close_connection = True
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Not JSON: {exc}"}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, today=date.today, people=None,
                fts: bool | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Papers(data, today, people, fts)
    handler = type("PapersHandler", (Handler,), {"app": app, "token": token or ""})
    return _Server(("127.0.0.1", port), handler)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self) -> None:
        super().server_close()
        db = self.RequestHandlerClass.app.index.db
        if db is not None:
            db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="My Papers for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts My Papers with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    token = os.environ.get("PUFFPAI_TOKEN")
    api = os.environ.get("PUFFPAI_API")
    server = make_server(args.port, data, token, people=lambda: fetch_people(api, token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
