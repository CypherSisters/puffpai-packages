"""Manuscript: a writer's novel and stories, read by her bot into a story bible, with her notes beside them.

One small server, bound to 127.0.0.1 (CONTRACT.md is the agreement with the page):
- ``/``                       the page (web/index.html), shown in the Desktop
- ``/health``                 "ok"
- ``/api/list``               GET: every manuscript with its sections and its reading status
- ``/api/section``            GET ``?title=&n=``: one section's text (``/api/chapter?title=&chapter=``, as 0.1.0)
- ``/api/upload``             POST ``{file_name, content_b64, title?, read?}``: a new manuscript (.docx .txt .md .pdf);
                              starts the reading. ``read: false`` adds a book to study instead: no reading, marked
                              ``"study": true`` in its manuscript.json
- ``/api/status``             GET ``?title=``: the reading's progress
- ``/api/analyze``            POST ``{title}``: start the reading again
- ``/api/bible``              GET ``?title=``: the story bible
- ``/api/tab``                GET ``?title=&tab=``: a tab's blocks and its saved views
- ``/api/show``               POST ``{title, tab, asked}``: a new view, from the page's Ask box
- ``/api/views``              GET ``?title=&tab=``; DELETE ``?title=&id=``: saved views
- ``/api/notes``              GET ``?title=&tab=``; POST ``{title, tab, text, stamp}``: as 0.1.0
- ``/api/pointer``            GET and DELETE ``?title=``: where the bot is pointing
- ``/api/correct``            POST ``{title, what, correction?, tab?}``: the writer's correction to any bible fact
- ``/api/correct/undo``       POST ``{title, id}``: takes one correction back; id may be "last"
- ``/api/edit``               POST ``{title, ops}``: her own edits from the page, the same stored operations
- ``/api/size``               GET ``?title=``: what the book's folder uses, in one line for the Settings fold
- ``/mcp``                    the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN. GET and DELETE never change manuscript text.

Where the files go: ``<data folder>\\My files\\Manuscripts\\<title>\\``, found by asking the Core where My files is
(``GET /v1/owner-files``, the same way packages/office does). The one rule: this app never changes section text. The
upload writes the sections once; after that only the app's own files change: notes, bible, views, pointer, status, cache.
The writer may change a section's text herself, through Editor's Desk, by the save contract in CONTRACT.md.
``My files\\Manuscripts`` holds ``.app-files``, so the Core's lists and index leave the app's bookkeeping out, and tidy.py
clears what no longer carries anything, by code, at most once a day per book.

Every model call goes to the Core's ``POST /v1/brain/chat`` (analysis.py); nothing leaves the computer.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import copy
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))      # its own modules, also when started with python -I
import analysis  # noqa: E402
import marks  # noqa: E402
import pdf_text  # noqa: E402
import tidy  # noqa: E402
from analysis import (BASE, BIBLE, BUDGET, CACHE, STATUS, Brain, BrainBusy, BrainDown, BrainQueue, BrainSlow, NotYet,
                      QueuedBrain, Reader, Settled, empty_bible, pick_facts, read_json, split_words, strip_think, tokens,
                      words_of, write_json)  # noqa: E402
from analysis import write_atomic as _write  # noqa: E402
from blocks import cards, clean_blocks, slug, tab_blocks  # noqa: E402
from fixing import (Bad, apply_corrections, edit_view, find, hand_ops, line_of, plan_words, prose_steps,  # noqa: E402
                    settled_lines, stale_words, target_of)  # noqa: E402
from writer import guess_tab, rule_plan  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
PAGE = WEB / "index.html"
NAME = "manuscript"
VERSION = "0.6.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 2 * 1024 * 1024
MAX_UPLOAD = 40 * 1024 * 1024                  # a long novel with pictures, as base64
SHELF = "Manuscripts"
CONVERTED = "Converted"                        # where Document convert leaves a Word file it made from a PDF
REMOVED = "Removed"                     # put-away manuscripts live here, inside the shelf, never deleted
INDEX = "manuscript.json"
SECTIONS = "sections"
CHAPTERS = "chapters"                          # 0.1.0's folder; its manuscripts still read from it
VIEWS = "views.json"
POINTER = "pointer.json"
TABS = {"characters": "Characters", "plot": "Plot and subplots", "timeline": "Timeline", "structure": "Structure",
        "world": "World", "science": "Science", "themes": "Themes", "voice": "Voice"}
TAB_NAMES = {"subplots": "plot", "plot and subplots": "plot"}
# 0.4.1: a notes file is made on first write. What an older version put in one at upload, which is nothing of hers:
# editing.md and desk.md stay as they are, shown read-only by Editor's Desk.
NOTES_HEADINGS = {**{f"{k}.md": (v,) for k, v in TABS.items()}, "editing.md": ("Editor's desk", "Editing"),
                  "desk.md": ("Editor's desk",)}
APP_FILES = ".app-files"                       # the Core's lists and index leave this folder out (02_PACKAGE_CONTRACT)
APP_FILES_SAYS = "Manuscript and Maps keep their working files here. The Your files list leaves this folder out.\n"
TIDIED = "tidied.json"                         # when each book was last tidied, in the app's own data folder
TIDY_EVERY = 24 * 3600
START_TRIES = 40                               # at start the Core may not answer yet: 40 tries, 15 seconds apart
START_WAIT = float(os.environ.get("MANUSCRIPT_START_WAIT_S") or 15)
CORRECTIONS = "corrections.json"
BOARDS = "boards.json"
# an ask with a drawing twin in Maps, and which board it is (the order Maps tries them in, draw.py WORDS)
TWINS = [(r"\btouch ?points?\b", "touchpoints"), (r"\btime ?lines?\b", "timeline"), (r"\bfamil(y|ies)\b", "family"),
         (r"\btrees?\b", "family"), (r"\brelationships?\b", "relationships"), (r"\binteract\w*", "relationships"),
         (r"\bsupporting\b", "relationships"), (r"\barcs?\b", "arcs"), (r"\bstructure\b", "structure"),
         (r"\bthreads?\b", "threads"), (r"\bworld\b", "world"), (r"\bmaps?\b", "world")]
UPLOAD_TYPES = (".docx", ".txt", ".md", ".pdf")
WEB_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
             ".json": "application/json", ".html": "text/html; charset=utf-8"}
NOTE_MAX = 50_000
NOTES_FILE_MAX = 5_000_000
READ_MAX = 400_000
ASK_MAX = 2_000
WHOLE = "Whole manuscript"
FRONT = "Before the first chapter"
PART_WORDS = analysis.PART_WORDS
ASK_WAIT = float(os.environ.get("MANUSCRIPT_ASK_WAIT_S") or 90)   # how long a chat question waits for the brain
CORRECT_WAIT = float(os.environ.get("MANUSCRIPT_CORRECT_WAIT_S") or 40)  # the proxy and a tool call stop at 60 seconds
SETTINGS = "settings.json"                     # 0.3.3: the app's own settings, in its own data folder
DEFAULTS = {"vault_free": True}

_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                 "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
                 "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
                 "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
                 "last", "final", "prologue", "epilogue"}
CHAPTER_LINE = re.compile(r"^(chapter|part)\s+(\w+)", re.IGNORECASE)
NUMBER_LINE = re.compile(r"^\d+\.?\s*$")
SCENE_BREAK = re.compile(r"^\s*(\*\s*\*\s*\*|-{3,}|#|~)\s*$")
WORD = re.compile(r"\w+(?:'\w+)?")


class Refused(Exception):
    """A plain line for the owner: nothing was changed."""


class CoreFiles:
    """Asks the Core where My files is, each time: she may rename the bot or move her data folder while this runs."""

    def __init__(self, api: str | None, token: str | None) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""

    def shelf(self) -> Path:
        req = urllib.request.Request(self.api + "/v1/owner-files", headers={"Authorization": f"Bearer {self.token}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the Core is on this computer
        try:
            with opener.open(req, timeout=10) as r:
                made = str(json.loads(r.read().decode("utf-8")).get("made") or "")
        except (OSError, ValueError, AttributeError, urllib.error.URLError):
            made = ""
        if not made:
            raise Refused("I could not ask BuildBot where your files are, so nothing was read or changed.")
        return Path(made).parent / SHELF                # My files\Manuscripts, beside "Made by <bot name>"


# -- splitting ----------------------------------------------------------------------------------------

def twin_kind(asked: str) -> str:
    """The Maps board an ask has as its drawing twin, or "" when it has none."""
    for pat, kind in TWINS:
        if re.search(pat, asked, re.IGNORECASE):
            return kind
    return ""


def is_chapter_line(text: str) -> bool:
    """A paragraph that starts a chapter by its words: "Chapter 3", "Part Two: The Sea", "12", "4.".
    "Part of me wanted to run" is a sentence, not a heading, so after "chapter" or "part" comes a number,
    a roman numeral or a number word."""
    s = text.strip()
    if not s or len(s) > 100:
        return False
    if NUMBER_LINE.match(s):
        return True
    m = CHAPTER_LINE.match(s)
    if not m:
        return False
    word = m.group(2).lower()
    return word.isdigit() or word in _NUMBER_WORDS or re.fullmatch(r"[ivxlcdm]+", word) is not None


def split_paragraphs(paras: list[tuple[str, bool]], by_words: bool = True) -> list[tuple[str, list[str]]]:
    """``paras``: (text, is_heading) in order. Chapters as (heading, paragraphs). Text before the first heading is
    its own chapter; no heading at all is one chapter, "Whole manuscript". ``by_words`` False: only the marks count
    (a PDF's marks are already decided, and it may keep a contents line that reads like a heading)."""
    chapters: list[tuple[str, list[str]]] = []
    head: str | None = None
    body: list[str] = []
    for text, heading in paras:
        if heading or (by_words and is_chapter_line(marks.strip(text))):
            if head is not None or any(b.strip() for b in body):
                chapters.append((head or FRONT, body))
            head, body = " ".join(marks.strip(text).split()), []          # a heading is kept plain
        else:
            body.append(text)
    if head is not None or any(b.strip() for b in body):
        chapters.append((head or (WHOLE if not chapters else FRONT), body))
    if len(chapters) == 1 and chapters[0][0] == FRONT:
        chapters = [(WHOLE, chapters[0][1])]
    return chapters or [(WHOLE, [])]


def split_scenes(body: list[str]) -> list[list[str]]:
    """A manuscript with no headings, at its scene breaks: a line that is only ``***``, ``* * *``, ``---``, ``#``
    or ``~``, or three or more blank lines in a row."""
    scenes: list[list[str]] = []
    cur: list[str] = []
    blank = 0

    def cut() -> None:
        nonlocal cur
        if any(p.strip() for p in cur):
            scenes.append(cur)
        cur = []

    for p in body:
        if SCENE_BREAK.match(p):
            cut()
            blank = 0
        elif not p.strip():
            blank += 1
            if blank == 3:
                cut()
            cur.append(p)
        else:
            blank = 0
            cur.append(p)
    cut()
    return scenes


def split_sections(paras: list[tuple[str, bool]], by_words: bool = True) -> tuple[list[tuple[str, str]], bool]:
    """(sections as (heading, text), whether the first is the text before the first heading). Headings first; with
    none, scene breaks; then any section over 2,200 words (1,000 with no headings) in parts at paragraph
    boundaries."""
    chapters = split_paragraphs(paras, by_words)
    most = PART_WORDS
    if len(chapters) == 1 and chapters[0][0] == WHOLE:          # no headings: a short story, read finer
        most = analysis.SHORT_PART_WORDS
        scenes = split_scenes(chapters[0][1])
        if len(scenes) > 1:
            chapters = [(f"Scene {i}", s) for i, s in enumerate(scenes, 1)]
    front = chapters[0][0] == FRONT
    out = []
    for head, body in chapters:
        text = chapter_text(body)
        if words(text) <= most:
            out.append((head, text))
        else:
            out += [(f"{head}, part {i}", piece) for i, piece in enumerate(split_words(text, most), 1)]
    return out, front


def _style_says(style: Any, name: str) -> bool | None:
    """Italic or bold as a style sets it, or as the style it is based on does; None when none of them says."""
    seen = 0
    while style is not None and seen < 20:
        got = getattr(style.font, name, None)
        if got is not None:
            return bool(got)
        style, seen = style.base_style, seen + 1
    return None


def _run_says(run: Any, para_style: Any, name: str) -> bool:
    """A run's own italic or bold, else its character style's, else its paragraph style's."""
    got = getattr(run.font, name, None)
    if got is None:
        got = _style_says(run.style, name)
    if got is None:
        got = _style_says(para_style, name)
    return bool(got)


def _marked(p: Any) -> str:
    """One Word paragraph as one marked line (marks.py): its italic and bold kept, its words exactly as p.text has them.
    A soft line break inside it stays a line break, and each line is marked on its own."""
    pieces: list[tuple[str, bool, bool]] = []
    for item in p.iter_inner_content():                     # runs, and the runs inside a link
        for run in getattr(item, "runs", None) or [item]:
            pieces.append((run.text, _run_says(run, p.style, "italic"), _run_says(run, p.style, "bold")))
    if "".join(x[0] for x in pieces) != p.text:              # something it can't follow: her words plain, never fewer
        return p.text
    lines: list[list[tuple[str, bool, bool]]] = [[]]
    for words, italic, bold in pieces:
        for k, part in enumerate(words.split("\n")):
            if k:
                lines.append([])
            lines[-1].append((part, italic, bold))
    return "\n".join(marks.wrap(line) for line in lines)


def docx_paragraphs(raw: bytes) -> list[tuple[str, bool]]:
    """(text, is_heading) for each paragraph. A heading stays plain; any other paragraph keeps its italic and bold as
    marks (marks.py). Stars she typed herself stay as she typed them."""
    import docx
    doc = docx.Document(io.BytesIO(raw))
    out = []
    for p in doc.paragraphs:
        style = (p.style.name if p.style is not None else "") or ""
        heading = style.lower() in ("heading 1", "heading 2")
        out.append((p.text if heading else _marked(p), heading))
    return out


def text_paragraphs(text: str, markdown: bool) -> list[tuple[str, bool]]:
    """One line is one paragraph, as in a Word file; blank lines are kept so the breaks stay. In a .md file a
    "# " or "## " line is a heading."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = re.match(r"^(#{1,2})\s+(.*\S.*)$", line) if markdown else None
        out.append((m.group(2), True) if m else (line, False))
    return out


def pdf_book(raw: bytes) -> pdf_text.Book:
    """A PDF through pdf_text.py: paragraphs with heading marks, and its report. A plain line when it can't be read."""
    try:
        return pdf_text.read_pdf(raw)
    except pdf_text.Refused as exc:
        raise Refused(str(exc)) from None
    except ImportError:
        raise Refused("Manuscript is missing a part it needs. Remove it and add it again from Apps.") from None
    except Exception:  # noqa: BLE001 - a file it cannot read: a plain line, never the file's words
        raise Refused("I could not read that PDF. Save it again from the program that made it and try once more.") from None


def chapter_text(body: list[str]) -> str:
    """Paragraphs with a blank line between each, as Markdown keeps them. Blank paragraphs between are dropped."""
    paras = [p.strip() for p in body if p.strip()]
    return "\n\n".join(paras)


def words(text: str) -> int:
    return len(re.findall(r"\S+", marks.strip(text)))


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- finding a passage ------------------------------------------------------------------------------------

def _paragraphs(text: str) -> list[tuple[int, int]]:
    """(start, end) of each paragraph that is not the "# heading" line."""
    return [(m.start(), m.end()) for m in re.finditer(r"[^\n]+(?:\n(?![ \t]*\n)[^\n]+)*", text)
            if not m.group().startswith("# ")]


def find_exact(text: str, quote: str) -> tuple[int, int] | None:
    q = quote.strip()
    if not q:
        return None
    i = text.find(q)
    if i < 0:
        i = text.lower().find(q.lower())
    return (i, i + len(q)) if i >= 0 else None


def find_run(text: str, quote: str) -> tuple[int, int, int] | None:
    """The longest run of five or more words the passage and the quote share: (start, end, words)."""
    tw = [(m.group().lower(), m.start(), m.end()) for m in WORD.finditer(text)]
    qw = [w.lower() for w in WORD.findall(quote)][:300]
    if len(qw) < 5:
        return None
    best, at = 0, -1
    prev = [0] * (len(qw) + 1)
    for i, (w, _s, _e) in enumerate(tw):
        cur = [0] * (len(qw) + 1)
        for j, q in enumerate(qw, 1):
            if w == q:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, at = cur[j], i
        prev = cur
    if best < 5:
        return None
    return tw[at - best + 1][1], tw[at][2], best


def find_paragraph(text: str, quote: str) -> tuple[int, int, int]:
    """The first paragraph sharing the most words with the quote: (start, end, words shared)."""
    q = words_of(quote)
    paras = _paragraphs(text) or [(0, len(text))]
    best = max(paras, key=lambda p: (len(q & words_of(text[p[0]:p[1]])), -p[0]))
    return best[0], best[1], len(q & words_of(text[best[0]:best[1]]))


# -- files ----------------------------------------------------------------------------------------------

def _safe(name: Any, fallback: str, n: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(name or ""))
    text = " ".join(text.split()).strip(" .")[:n].strip(" .")
    return text or fallback


def _stamp(path: Path) -> str:
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return ""


def _when() -> str:
    return analysis.now()


class Manuscripts:
    def __init__(self, files: Any, brain: Brain | None = None) -> None:
        self.files = files
        self.brain = brain or Brain("", "")
        self.lock = threading.Lock()
        self.data = Path(os.environ.get("PUFFPAI_DATA") or HERE / "data")
        self.brain.as_written = self.vault_free          # every brain call sends the setting as it is now
        self.queue = BrainQueue()                    # every brain call this process makes, one at a time
        self.reader = Reader(QueuedBrain(self.brain, self.queue), self._load, self._publish, self.vault_free,
                             self._with_word, self._settled, self._read_starts, self._read_ended, self._tidy_quietly)
        self.making: set[str] = set()                # views whose job is in this process's queue
        self.working: set[str] = set()               # corrections the brain has not answered yet
        self.rewriting: dict[str, list[str]] = {}    # correction id -> what the brain is rewriting for it

    # settings (0.3.3)

    def settings(self) -> dict:
        got = read_json(self.data / SETTINGS, {})
        got = got if isinstance(got, dict) else {}
        return {k: (got[k] if isinstance(got.get(k), type(v)) else v) for k, v in DEFAULTS.items()}

    def vault_free(self) -> bool:
        return bool(self.settings()["vault_free"])

    def settings_save(self, msg: Any) -> dict:
        msg = msg if isinstance(msg, dict) else {}
        if not isinstance(msg.get("vault_free"), bool):
            raise Refused("Say vault_free: true or false.")
        with self.lock:
            got = {**self.settings(), "vault_free": msg["vault_free"]}
            write_json(self.data / SETTINGS, got)
        line = ("Manuscripts stay out of the vault: your bot's brain gets them as written." if got["vault_free"]
                else "Manuscripts go through the vault like anything else you tell your bot.")
        if any((folder / BIBLE).exists() for folder, _idx in self._all()):
            line += " A manuscript your bot has already read keeps what it was read with until you read it again."
        return {"settings": got, "message": line}

    @staticmethod
    def vault_words(st: dict, now_free: bool) -> str:
        """"out of the vault" or "vault on": what the reading used, else (read before 0.3.3) the setting now."""
        free = st.get("as_written") if isinstance(st.get("as_written"), bool) else now_free
        return "out of the vault" if free else "vault on"

    @staticmethod
    def counts(bible: Any) -> dict | None:
        """Real counts from bible.json, so the chat never has to guess them. None before any reading."""
        if not isinstance(bible, dict):
            return None
        lst = lambda k: [x for x in bible.get(k) or [] if isinstance(x, dict)]   # noqa: E731
        return {"characters": len(lst("characters")),
                "main": sum(1 for c in lst("characters") if c.get("importance") == "main"),
                "threads": len(lst("threads")), "places": len(lst("places")), "events": len(lst("events"))}

    @staticmethod
    def counts_line(c: dict | None) -> str:
        if not c:
            return ""
        return (f"Found: {c['characters']} characters ({c['main']} main), {c['threads']} plot threads, "
                f"{c['places']} places, {c['events']} events.")

    # where things are

    def shelf(self) -> Path:
        got = self.files.shelf()
        self._mark(got)
        return got

    @staticmethod
    def _mark(shelf: Path) -> None:
        """The shelf belongs to the app, for a bot from before 0.4.1 too. It can't be written: the lists just stay longer."""
        try:
            if shelf.is_dir() and not (shelf / APP_FILES).exists():
                (shelf / APP_FILES).write_text(APP_FILES_SAYS, encoding="utf-8")
        except OSError:
            pass

    # tidying (0.4.1): by code, at most once a day per book, never during a reading

    def _reading_now(self, folder: Path) -> bool:
        """A reading tells us when it is over from its own thread, which is still alive then."""
        t = self.reader.running.get(str(folder))
        return t is not None and t.is_alive() and t is not threading.current_thread()

    def tidy_book(self, folder: Path) -> dict | None:
        """One book, when it is due and no reading is running on it. What it removed, or None when it did nothing."""
        if self._reading_now(folder):
            return None
        with self.lock:
            stamps = read_json(self.data / TIDIED, {})
            stamps = stamps if isinstance(stamps, dict) else {}
            last = stamps.get(folder.name)
            if isinstance(last, (int, float)) and 0 <= time.time() - last < TIDY_EVERY:
                return None
            idx = self._index(folder)
            if not idx or self._reading_now(folder):
                return None
            paths = []
            for c in idx["sections"]:
                try:
                    paths.append(self._section_path(folder, idx, c))
                except Refused:
                    return None
            gone = tidy.tidy(folder, idx["_dir"], paths, [c.get("sha") for c in idx["sections"]], NOTES_HEADINGS)
            there = {p.name for p in folder.parent.iterdir()}
            write_json(self.data / TIDIED, {**{k: v for k, v in stamps.items() if k in there}, folder.name: time.time()})
        return gone

    def _tidy_quietly(self, folder: Path) -> None:
        try:
            self.tidy_book(folder)
        except (OSError, ValueError, Refused):
            pass                                     # nothing was half-done: the next one goes on

    def tidy_due(self) -> None:
        """Every book that is due. After an upload and at start. It never stops what asked for it."""
        try:
            books = self._all()
        except (OSError, Refused):
            return
        for folder, _idx in books:
            self._tidy_quietly(folder)

    def at_start(self) -> None:
        """The shelf's mark and the first tidy, as soon as the Core says where the shelf is."""
        for _try in range(START_TRIES):
            try:
                self.shelf()
                break
            except (OSError, Refused):
                time.sleep(START_WAIT)
        else:
            return
        self.tidy_due()

    def size_view(self, title: str) -> dict:
        folder, idx = self._find(title)
        used = tidy.usage(folder)
        return {"title": idx.get("title"), **used, "line": tidy.usage_line(used)}

    def _index(self, folder: Path) -> dict:
        got = read_json(folder / INDEX, {})
        if not isinstance(got, dict):
            return {}
        secs = got.get("sections") if isinstance(got.get("sections"), list) else got.get("chapters")
        if not isinstance(secs, list):
            return {}
        got["_dir"] = SECTIONS if isinstance(got.get("sections"), list) else CHAPTERS
        got["sections"] = [c for c in secs if isinstance(c, dict)]
        return got

    def _all(self) -> list[tuple[Path, dict]]:
        shelf = self.shelf()
        if not shelf.is_dir():
            return []
        out = []
        for folder in sorted(shelf.iterdir(), key=lambda p: p.name.lower()):
            if folder.is_dir():
                idx = self._index(folder)
                if idx:
                    out.append((folder, idx))
        return out

    def _find(self, title: Any) -> tuple[Path, dict]:
        want = " ".join(str(title or "").split()).lower()
        books = self._all()
        if not books:
            raise Refused("There are no manuscripts yet. Upload one on the Manuscript page.")
        if not want and len(books) == 1:
            return books[0]
        for folder, idx in books:
            if want in (str(idx.get("title", "")).lower(), folder.name.lower()):
                return folder, idx
        near = [(f, i) for f, i in books if want and want in str(i.get("title", "")).lower()]
        if len(near) == 1:
            return near[0]
        names = ", ".join(str(i.get("title")) for _f, i in books)
        raise Refused(f"I could not find that manuscript. These are here: {names}.")

    def _section(self, idx: dict, section: Any) -> dict:
        secs = idx["sections"]
        if not secs:
            raise Refused("That manuscript has no sections.")
        raw = str(section if section is not None else "").strip()
        if not raw and len(secs) == 1:
            return secs[0]
        if raw.isdigit():
            n = int(raw)
            for c in secs:
                if c.get("n") == n:
                    return c
        low = raw.lower()
        for c in secs:
            if low and low in (str(c.get("heading", "")).lower(), str(c.get("file", "")).lower()):
                return c
        near = [c for c in secs if low and low in str(c.get("heading", "")).lower()]
        if len(near) == 1:
            return near[0]
        nums = [c.get("n") for c in secs if isinstance(c.get("n"), int)]
        span = f", numbered {min(nums)} to {max(nums)}" if nums else ""
        raise Refused(f"I could not find that section. It has {len(secs)} sections{span}.")

    def _section_path(self, folder: Path, idx: dict, c: dict) -> Path:
        base = folder / idx["_dir"]
        path = base / str(c.get("file"))
        if path.parent != base:
            raise Refused("I could not find that section.")
        return path

    def _section_text(self, folder: Path, idx: dict, c: dict) -> str:
        try:
            return self._section_path(folder, idx, c).read_text(encoding="utf-8")
        except OSError:
            raise Refused("That section's file is missing from its folder.") from None

    def _tab(self, tab: Any) -> str:
        t = " ".join(str(tab or "").strip().lower().replace("’", "'").split())
        t = TAB_NAMES.get(t, t)
        for key, label in TABS.items():
            if t in (key, label.lower()):
                return key
        raise Refused("The tabs are characters, plot, timeline, structure, world, science, themes or voice. Editing is "
                      "in the Editor's Desk app.")

    def _load(self, folder: Path) -> tuple[str, list[tuple[int, str, str, str]]]:
        """For the reading: (title, [(n, heading, text, sha)]). The sha is of the file as it is on disk."""
        idx = self._index(folder)
        out = []
        for c in idx.get("sections") or []:
            try:
                raw = self._section_path(folder, idx, c).read_bytes()
            except (OSError, Refused):
                continue
            text = raw.decode("utf-8", errors="replace")
            body = re.sub(r"\A# [^\n]*\n*", "", text)              # the heading goes in the ask on its own
            body = marks.strip(body)                               # the reading and the asks see words, never stars
            out.append((int(c.get("n") or 0), str(c.get("heading") or ""), body, sha_of(raw)))
        return str(idx.get("title") or folder.name), out

    def _status(self, folder: Path, idx: dict) -> dict:
        got = read_json(folder / STATUS)
        if not isinstance(got, dict):
            return {"state": "idle", "step": "Not read yet.", "done": 0, "total": len(idx["sections"]),
                    "started_at": None, "finished_at": None, "problems": []}
        if got.get("state") == "reading" and not self.reader.busy(folder):
            got = {**got, "state": "failed", "step": "The reading stopped when Manuscript stopped. Start reading again."}
        return got

    def _bible(self, folder: Path, idx: dict) -> dict:
        return self._live(folder, idx)[0]            # the writer's own word is always on top

    def _base(self, folder: Path, make: bool = False) -> dict | None:
        """The bible as read, under her corrections. A book read before 0.4 has only bible.json: with ``make``, that
        becomes the base, and "baked" names the older corrections already in it, which an undo can't take out."""
        got = read_json(folder / BASE)
        if isinstance(got, dict):
            return got
        got = read_json(folder / BIBLE)
        if not isinstance(got, dict):
            return None
        if make:
            got["baked"] = [r.get("id") for r in self._corrections(folder) if "ops" not in r and "state" not in r]
            write_json(folder / BASE, got)
        return got

    def _live(self, folder: Path, idx: dict) -> tuple[dict, list[dict]]:
        """(the bible with her word on top, what each of her records came to). bible.json is the same, once written."""
        got = self._base(folder)
        if not isinstance(got, dict):
            got = empty_bible(str(idx.get("title")), len(idx["sections"]))
        got.pop("baked", None)
        return got, self._overlay(folder, got)

    def _publish(self, folder: Path, bible: dict) -> None:
        """The reading's every write: the bible as read, and that with her word on top."""
        with self.lock:
            write_json(folder / BASE, bible)
            write_json(folder / BIBLE, self._with_word(folder, bible))

    def _with_word(self, folder: Path, bible: dict) -> dict:
        got = copy.deepcopy(bible)
        got.pop("baked", None)
        self._overlay(folder, got)
        return got

    def _settled(self, folder: Path) -> list[str]:
        return settled_lines(self._corrections(folder))

    def _rebuild(self, folder: Path) -> None:
        """bible.json again, from the bible as read and her records as they are now. Hold the lock."""
        base = self._base(folder, make=True)
        if isinstance(base, dict):
            write_json(folder / BIBLE, self._with_word(folder, base))

    def _corrections(self, folder: Path) -> list[dict]:
        got = read_json(folder / CORRECTIONS, {})
        got = got.get("corrections") if isinstance(got, dict) else got
        return [c for c in got if isinstance(c, dict)] if isinstance(got, list) else []

    def _overlay(self, folder: Path, bible: dict) -> list[dict]:
        """Her corrections on top of a bible, in place. The reading calls this before every
        write, so a re-read never undoes them."""
        done = apply_corrections(bible, self._corrections(folder))
        for d in done:                               # asked of a brain that is gone: her words stay
            if d.get("state") == "working" and d.get("id") not in self.working:
                d.update(state="note", why="Manuscript stopped before your bot worked this out. Say it again.")
        return done

    def _notes_path(self, folder: Path, key: str) -> Path:
        return folder / f"{key}.md"

    def _views(self, folder: Path) -> list[dict]:
        got = read_json(folder / VIEWS, {})
        views = got.get("views") if isinstance(got, dict) else None
        views = [v for v in views if isinstance(v, dict)] if isinstance(views, list) else []
        for i, v in enumerate(views):                # a view left making when Manuscript stopped
            if v.get("state") == "making" and v.get("id") not in self.making:
                views[i] = {**v, "state": "failed", "why": "Manuscript stopped before this view was made. "
                                                           "Press Retry to make it again."}
        return views

    # reading

    def manuscript_list(self) -> str:
        books = self._all()
        if not books:
            return "There are no manuscripts yet. Upload one on the Manuscript page."
        lines = []
        for folder, idx in books:
            secs = idx["sections"]
            total = sum(int(c.get("words") or 0) for c in secs)
            st = self._status(folder, idx)
            if idx.get("study") is True:
                lines.append(f"{idx.get('title')}: {len(secs)} sections, {total} words. A book to study: the writer added it to "
                             "compare with her own in Editor's Desk (desk_study), not to be read into a story bible.")
            else:
                lines.append(f"{idx.get('title')}: {len(secs)} sections, {total} words. Reading: {st.get('step')}")
            found = self.counts_line(self.counts(read_json(folder / BIBLE)))
            if found:
                lines.append(f"  {found}")
            if isinstance(idx.get("pdf_report"), dict):
                lines.append(f"  {pdf_text.report_line(idx['pdf_report'])}")
            for c in secs:
                lines.append(f"  {c.get('n')}. {c.get('heading')} ({c.get('words')} words)")
        return "\n".join(lines)

    def manuscript_read(self, title: str = "", section: Any = "", chapter: Any = "") -> str:
        folder, idx = self._find(title)
        c = self._section(idx, section if str(section or "").strip() else chapter)
        text = marks.strip(self._section_text(folder, idx, c))
        more = "" if len(text) <= READ_MAX else "\n\n(The section goes on; this is its first part.)"
        return f"{idx.get('title')}, section {c.get('n')}: {c.get('heading')}\n\n{text[:READ_MAX]}{more}"

    def manuscript_status(self, title: str = "") -> str:
        folder, idx = self._find(title)
        st = self._status(folder, idx)
        line = f"{idx.get('title')}: {st.get('step')}"
        if st.get("state") == "done":
            model = self._bible(folder, idx).get("built_with")
            line += f" Read by {model} on this computer." if model else ""
        if st.get("state") in ("reading", "done", "failed"):
            line += f" ({self.vault_words(st, self.vault_free())})"
        if st.get("problems"):
            line += " " + " ".join(str(p) for p in st["problems"][:5])
        found = self.counts_line(self.counts(read_json(folder / BIBLE)))
        return line + (f"\n{found}" if found else "")

    def manuscript_analyze(self, title: str = "", confirm: Any = False) -> str:
        folder, idx = self._find(title)
        if confirm is not True:
            # 0.3.2: a reading takes the chip for a long while, so the bot never starts one on its own. Without the
            # writer's yes this only hands back the question to ask; the page's own buttons use the route, not this.
            n = len(idx.get("sections") or [])
            mins = max(5, n * 4)
            return (f"Not started. Ask the writer first: \"Shall I read {idx.get('title')} now? It is {n} sections and "
                    f"takes about {mins} minutes on this computer, and chat is slower while it reads.\" Call this again with "
                    "confirm true only after they say yes in their latest message.")
        if not self.reader.start(folder):
            return f"Your bot is already reading {idx.get('title')}: {self._status(folder, idx).get('step')}"
        return (f"Your bot started reading {idx.get('title')}, {len(idx['sections'])} sections. "
                "The tabs fill in as it goes. The manuscript text is not changed.")

    def manuscript_notes_read(self, title: str = "", tab: str = "") -> str:
        folder, idx = self._find(title)
        key = self._tab(tab)
        text = self._notes(folder, key)
        return text if text.strip() else f"The {TABS[key]} notes for {idx.get('title')} are empty."

    def _notes(self, folder: Path, key: str) -> str:
        try:
            return self._notes_path(folder, key).read_text(encoding="utf-8")
        except OSError:
            return f"# {TABS[key]}\n"

    # asking the brain

    def _busy_line(self) -> str:
        later = "Ask again in a few minutes, or ask me to show it under a tab and it will appear when ready."
        for key, t in list(self.reader.running.items()):
            if not t.is_alive():
                continue
            folder = Path(key)
            st = read_json(folder / STATUS, {}) or {}
            title = self._index(folder).get("title") or folder.name
            total = int(st.get("total") or 0)
            if total:
                n = min(int(st.get("done") or 0) + 1, total)
                return f"Your bot is busy reading {title} right now (section {n} of {total}). {later}"
            return f"Your bot is busy reading {title} right now. {later}"
        return f"Your bot is busy right now. {later}"

    ASK_SYSTEM = ("You help a writer think about her own manuscript. Answer her question from the story bible facts "
                  "and the sections given, in plain words. Name the sections you rely on, like (section 3). When the "
                  "facts don't say, say so. Never rewrite her text. What she has corrected is settled: it goes above "
                  "the facts and above the sections.")

    def manuscript_ask(self, title: str = "", question: str = "") -> str:
        q = " ".join(str(question or "").split())[:ASK_MAX]
        if not q:
            raise Refused("Say what you want to know about the manuscript.")
        folder, idx = self._find(title)
        bible = self._bible(folder, idx)
        room = BUDGET - tokens(self.ASK_SYSTEM) - tokens(q) - 100
        facts = pick_facts(bible, q, min(1800, room))
        room -= tokens(facts)
        word = self._settled(folder)
        said = "What the writer has said about the facts, settled:\n" + "\n".join(f"The writer said: {x}" for x in word) \
            if word else ""
        room -= tokens(said)
        qw = words_of(q)
        _t, secs = self._load(folder)
        ranked = sorted(((len(qw & words_of(text)), i) for i, (_n, _h, text, _s) in enumerate(secs)),
                        key=lambda x: (-x[0], x[1]))
        parts = []
        for score, i in ranked[:2]:
            n, head, text, _sha = secs[i]
            block = f"Section {n}: {head}\n{text}"
            if score > 0 and tokens(block) <= room:
                parts.append(block)
                room -= tokens(block)
        user = f"Question: {q}\n\n" + (said + "\n\n" if said else "") + \
            f"Story bible facts:\n{facts or '(the bible is empty so far)'}"
        if parts:
            user += "\n\n" + "\n\n".join(parts)
        messages = [{"role": "system", "content": self.ASK_SYSTEM}, {"role": "user", "content": user}]
        job = self.queue.submit(lambda: self.brain.chat(messages, patient=False))
        try:
            text, _model = job.wait(ASK_WAIT)
        except NotYet:
            self.queue.cancel(job)                   # not started yet: it never will; started: its answer is dropped
            return self._busy_line()
        except BrainBusy:
            return self._busy_line()
        except BrainDown as exc:
            raise Refused(str(exc)) from None
        except BrainSlow:
            raise Refused("Your bot's brain took too long on that. Try asking a smaller question.") from None
        answer = strip_think(text)
        if not bible.get("sections_read"):
            answer = (answer + "\n\n" if answer else "") + "(Your bot has not finished reading this manuscript yet.)"
        return answer or "Your bot had no answer to that."

    SHOW_SYSTEM = (
        "You make one view for a writer's manuscript page from the story bible facts given. Answer with one JSON "
        'object only: {"title":"a short title","blocks":[...]}. Each block is one of:\n'
        '{"type":"markdown","text":""}\n'
        '{"type":"table","columns":["",""],"rows":[["",{"text":"","section":1}]]}\n'
        '{"type":"timeline","items":[{"label":"","when":"","section":1,"who":[""]}]}\n'
        '{"type":"graph","nodes":[{"id":"","label":"","kind":""}],"edges":[{"from":"","to":"","label":""}]}\n'
        '{"type":"tree","root":{"label":"","children":[{"label":"","children":[]}]}}\n'
        '{"type":"list","items":[{"text":"","section":1}]}\n'
        '{"type":"quote","text":"","section":1,"note":""}\n'
        "Use only the facts given. Put the section number on an item when the facts give it. One to four blocks. "
        "Plain words."
    )

    def show(self, title: Any, tab: Any, asked: Any, made_by: str = "chat") -> tuple[str, dict]:
        """Checks the ask, saves a view in state "making" at once and puts the brain's part in the queue. The view
        turns "ready" with its blocks, or "failed" with a plain "why", when the queue gets to it."""
        what = " ".join(str(asked or "").split())[:ASK_MAX]
        if not what:
            raise Refused("Say what you want to see.")
        folder, idx = self._find(title)
        key = self._tab(tab)
        if not pick_facts(self._bible(folder, idx), what, BUDGET):
            raise Refused(f"Your bot has not read {idx.get('title')} yet, so there is nothing to show. "
                          "Start reading first.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        with self.lock:
            views = self._views(folder)
            have = {v.get("id") for v in views}
            vid, k = f"v-{stamp}-{slug(' '.join(what.split()[-3:]))[:30]}", 2
            base = vid
            while vid in have:
                vid, k = f"{base}-{k}", k + 1
            view = {"id": vid, "tab": key, "title": what[:1].upper() + what[1:120], "asked": what,
                    "made_at": _when(), "made_by": "page" if made_by == "page" else "chat", "state": "making"}
            kind = twin_kind(what)
            if kind and self._add_board(folder, {"id": vid, "kind": kind, "title": view["title"], "asked": what,
                                                 "made_at": view["made_at"]}):
                view["board"] = vid
            write_json(folder / VIEWS, {"views": [view] + views})
            self.making.add(vid)
        self.queue.submit(lambda: self._make_view(folder, vid, key, what))
        line = f"Making it. It shows under {TABS[key]} when it is ready"
        return (line + ", and it is drawn in Maps." if view.get("board") else line + "."), view

    @staticmethod
    def _add_board(folder: Path, board: dict) -> bool:
        """A board record with no spec in boards.json, for Maps to lay out when it is opened. Every other board and
        key in the file stays as it was (Maps writes there too)."""
        got = read_json(folder / BOARDS, {})
        if (folder / BOARDS).exists() and not isinstance(got, dict):
            return False                             # a file it can't read is left alone
        got = got if isinstance(got, dict) else {}
        boards = got.get("boards") if isinstance(got.get("boards"), list) else []
        try:
            write_json(folder / BOARDS, {**got, "boards": [board] + [b for b in boards
                                                                      if not (isinstance(b, dict) and
                                                                              b.get("id") == board["id"])]})
        except OSError:
            return False
        return True

    def _make_view(self, folder: Path, vid: str, key: str, what: str) -> None:
        """In the queue: the brain's blocks for one view, then the view's record replaced with them."""
        done: dict = {"state": "failed", "why": "Your bot could not put that view together. Try asking it another way."}
        try:
            idx = self._index(folder)
            room = BUDGET - tokens(self.SHOW_SYSTEM) - tokens(what) - 100
            facts = pick_facts(self._bible(folder, idx), what, room)
            user = f"Tab: {TABS[key]}\nAsked: {what}\n\nFacts:\n{facts}"
            got, _model = analysis.ask_json(Settled(self.brain, lambda: self._settled(folder)), self.SHOW_SYSTEM, user,
                                            lambda g: bool(clean_blocks(g)), patient=True)
            blocks = clean_blocks(got)
            if blocks:
                name = " ".join(str(got.get("title") or "").split())[:120] if isinstance(got, dict) else ""
                done = {"state": "ready", "blocks": blocks, **({"title": name} if name else {})}
        except BrainSlow:
            done["why"] = "Your bot's brain took too long on that. Try asking for something smaller."
        except BrainDown:
            done["why"] = "Your bot's brain could not answer, so this view was not made. Try again in a few minutes."
        except Exception as exc:  # noqa: BLE001 - a plain line on the view instead
            done["why"] = f"Manuscript stopped while making this view ({type(exc).__name__}). Try again."
        try:
            with self.lock:
                views = self._views(folder)
                for i, v in enumerate(views):
                    if v.get("id") == vid:             # removed while making: it stays removed
                        new = {k: x for k, x in v.items() if k not in ("why", "blocks")}
                        views[i] = {**new, **done}
                        write_json(folder / VIEWS, {"views": views})
                        break
        finally:
            with self.lock:
                self.making.discard(vid)

    def manuscript_show(self, title: str = "", tab: str = "", asked: str = "") -> str:
        return self.show(title, tab, asked, "chat")[0]

    def manuscript_point(self, title: str = "", section: Any = "", quote: str = "", note: str = "") -> str:
        q = " ".join(str(quote or "").split())[:ASK_MAX]
        if not q:
            raise Refused("Give a few words from the passage to point at.")
        folder, idx = self._find(title)
        cands = [self._section(idx, section)] if str(section or "").strip() else idx["sections"]
        texts = []
        for c in cands:
            try:
                texts.append((c, marks.strip(self._section_text(folder, idx, c))))   # places in the words the page shows
            except Refused:
                continue
        if not texts:
            raise Refused("That section's file is missing from its folder.")
        hit = None
        for c, text in texts:
            got = find_exact(text, q)
            if got:
                hit = (c, text, got[0], got[1], "exact", 0)
                break
        if hit is None:
            runs = [(c, text, find_run(text, q)) for c, text in texts]
            runs = [(c, text, r) for c, text, r in runs if r]
            if runs:
                c, text, r = max(runs, key=lambda x: x[2][2])         # max keeps the first of equals
                hit = (c, text, r[0], r[1], "words", r[2])
        if hit is None:
            paras = [(c, text, find_paragraph(text, q)) for c, text in texts]
            c, text, p = max(paras, key=lambda x: x[2][2])
            hit = (c, text, p[0], p[1], "paragraph" if p[2] else "nothing", p[2])
        c, text, start, end, how, k = hit
        where = f"section {c.get('n')}, {c.get('heading')}"
        pointer = {"title": idx.get("title"), "section": c.get("n"), "start": start, "end": end,
                   "quote": text[start:end][:200], "note": " ".join(str(note or "").split())[:400],
                   "match": how, "set_at": _when()}
        write_json(folder / POINTER, pointer)
        if how == "exact":
            return f"Pointing at {where}, at the words you gave."
        if how == "words":
            return f"Pointing at {where}, at the passage closest to those words ({k} words in a row match)."
        if how == "paragraph":
            return f"I did not find those exact words. Pointing at the paragraph in {where} that shares the most."
        return f"I did not find anything like those words, so I am pointing at the start of {where}."

    # the one change the bot can make to the owner's own notes

    def manuscript_notes_append(self, title: str = "", tab: str = "", text: str = "") -> str:
        body = str(text or "").strip()
        if not body:
            raise Refused("Say what goes in the notes.")
        if len(body) > NOTE_MAX:
            raise Refused("That note is too long for one go. Split it in parts.")
        folder, idx = self._find(title)
        key = self._tab(tab)
        with self.lock:
            old = self._notes(folder, key).rstrip("\n")
            if len(old) > NOTES_FILE_MAX:
                raise Refused("That notes file is full. Move some of it out on the Manuscript page first.")
            when = datetime.now().strftime("%Y-%m-%d %H:%M")
            _write(self._notes_path(folder, key), f"{old}\n\n## {when}\n\n{body}\n")
        return f"Added a note to {TABS[key]} for {idx.get('title')}. The manuscript text is unchanged."

    # the writer's corrections (the app's own files: corrections.json, bible.json)

    @staticmethod
    def _new_id(records: list[dict]) -> str:
        have = {r.get("id") for r in records}
        stamp, k = datetime.now().strftime("%Y%m%d-%H%M%S"), len(records) + 1
        while f"c-{stamp}-{k}" in have:
            k += 1
        return f"c-{stamp}-{k}"

    def correct(self, title: Any, what: Any, correction: Any = "", tab: Any = "") -> tuple[str, dict]:
        """A correction in her own words: the plain shapes at once, anything else through the brain's queue. It waits a
        short time for the brain, then says it is working on it; the record turns done or note when the answer comes."""
        said = " ".join(str(what or "").split())[:ASK_MAX]
        fix = " ".join(str(correction or "").split())[:ASK_MAX]
        if not said:
            raise Refused("Say which fact is wrong and what it should be.")
        folder, idx = self._find(title)
        on = self._tab(tab) if str(tab or "").strip() else ""
        with self.lock:
            bible, _done = self._live(folder, idx)
            records = self._corrections(folder)
            rid = self._new_id(records)
            rec = {"id": rid, "what": said, "correction": fix, "made_at": _when(), "by": "words", "ops": [],
                   **({"asked_on": on} if on else {})}
            plan = rule_plan(bible, said, fix)
            read = any(bible.get(k) for k in ("characters", "places", "events", "threads", "themes", "world_rules",
                                              "science", "history"))
            if plan:
                rec.update(ops=plan["ops"], line=plan["line"], by="rules", state="done")
            elif read:
                rec.update(state="working", tab=on or guess_tab(bible, said))
                self.working.add(rid)
            else:
                rec.update(state="note", tab=on or guess_tab(bible, said))
            write_json(folder / CORRECTIONS, {"corrections": records + [rec]})
            self._rebuild(folder)
        if plan:
            self._want_prose(folder, rid)
        elif read:
            job = self.queue.submit(lambda: self._resolve(folder, rid))
            try:
                job.wait(CORRECT_WAIT)
            except NotYet:
                pass                                 # it stays in the queue; the page and the record show it later
        return self._answer(folder, idx, rid)

    def _resolve(self, folder: Path, rid: str) -> None:
        """In the queue: her words to the brain, what comes back to checked operations, and the record updated."""
        try:
            idx = self._index(folder)
            rec = next((r for r in self._corrections(folder) if r.get("id") == rid), None)
            if rec is None:                          # undone while it waited
                return
            bible = self._live(folder, idx)[0]
            try:
                plan = plan_words(self.brain, bible, str(rec.get("what")), str(rec.get("correction") or ""),
                                  str(rec.get("asked_on") or ""))
            except BrainSlow:
                plan = {"ops": [], "why": "Your bot's brain took too long on that, so nothing changed. Try again later."}
            except BrainDown:
                plan = {"ops": [], "why": "Your bot's brain could not answer, so nothing changed. Try again in a few "
                                          "minutes."}
            except Exception as exc:  # noqa: BLE001 - a plain line on the record instead
                plan = {"ops": [], "why": f"Manuscript stopped on this ({type(exc).__name__}), so nothing changed."}
            with self.lock:
                records = self._corrections(folder)
                now = next((r for r in records if r.get("id") == rid), None)
                if now is None:
                    return
                now.update(ops=plan["ops"], line=plan.get("line") or "", why=plan.get("why") or "",
                           partial=bool(plan.get("partial")), state="done" if plan["ops"] else "note")
                write_json(folder / CORRECTIONS, {"corrections": records})
                self._rebuild(folder)
            if plan["ops"]:
                self._want_prose(folder, rid)
        finally:
            with self.lock:
                self.working.discard(rid)

    # 0.4: wrong prose follows the fix

    def _want_prose(self, folder: Path, rid: str, after_reading: bool = False) -> None:
        """Rewrites what a correction touched, one brain call at a time in the queue. During a reading it waits for
        the reading's end, as the reading's own write-ups already take the correction as settled."""
        with self.lock:
            records = self._corrections(folder)
            rec = next((r for r in records if r.get("id") == rid), None)
            if rec is None or not rec.get("ops"):
                return
            if self.reader.busy(folder) and not after_reading:
                rec["prose_state"] = "after reading"
                write_json(folder / CORRECTIONS, {"corrections": records})
                return
            steps = prose_steps(self._live(folder, self._index(folder))[0], rec["ops"], records)
            if not steps:
                return
            rec["prose_state"] = "rewriting"
            write_json(folder / CORRECTIONS, {"corrections": records})
            self.rewriting[rid] = [f"{k}:{v}" for k, v in steps]
        self.queue.submit(lambda: self._rewrite_next(folder, rid, steps, True))

    def _rewrite_next(self, folder: Path, rid: str, steps: list, ok: bool) -> None:
        if self.reader.busy(folder):                 # a reading started meanwhile: it comes after that
            with self.lock:
                self.rewriting.pop(rid, None)
                records = self._corrections(folder)
                for r in records:
                    if r.get("id") == rid:
                        r["prose_state"] = "after reading"
                        write_json(folder / CORRECTIONS, {"corrections": records})
            return
        kind, which = steps[0]
        got: list[dict] = []
        try:
            live = self._live(folder, self._index(folder))[0]
            rec = next((r for r in self._corrections(folder) if r.get("id") == rid), None)
            work = copy.deepcopy(live)
            asker = Settled(self.brain, lambda: self._settled(folder))
            if rec is None:
                steps = steps[:1]
            elif kind == "character":
                analysis.synth_people(asker, work, [which])
                c = next(x for x in work["characters"] if x["id"] == which)
                was = next(x for x in live["characters"] if x["id"] == which)
                stale = stale_words(rec["ops"], which)
                for f in ("personality", "write_up"):
                    if c.get(f) and c.get(f) != was.get(f):
                        got.append({"target": target_of("character", was), "field": f, "value": c[f]})
                    ok = ok and not any(w in str(c.get(f)).lower() for w in stale)
            else:
                analysis.synth_brief(asker, work, which)
                stale = stale_words(rec["ops"])
                text = str(work["briefs"].get(which) or "")
                if text and text != live["briefs"].get(which):
                    got.append({"target": {"kind": "brief"}, "field": which, "value": text})
                if which == "characters" and work.get("cast_summary") != live.get("cast_summary"):
                    got.append({"target": {"kind": "cast"}, "field": "cast_summary", "value": work["cast_summary"]})
                ok = ok and not any(w in text.lower() for w in stale)
            if any(any(w in str(p["value"]).lower() for w in stale_words((rec or {}).get("ops") or [],
                                                                          which if kind == "character" else None))
                   for p in got):
                got = []                             # it still says the wrong thing: the old one stays, and she is told
        except Exception:  # noqa: BLE001 - the brain could not: she is told, and her hand edit still works
            ok, got = False, []
        rest = steps[1:]
        with self.lock:
            records = self._corrections(folder)
            now = next((r for r in records if r.get("id") == rid), None)
            if now is not None:
                keep = [p for p in now.get("prose") or [] if not any(p["target"] == g["target"] and p["field"] == g["field"]
                                                                     for g in got)]
                now["prose"] = keep + got
                if not rest:
                    now["prose_state"] = "done" if ok else "failed"
                write_json(folder / CORRECTIONS, {"corrections": records})
                self._rebuild(folder)
            if rest and now is not None:
                self.rewriting[rid] = [f"{k}:{v}" for k, v in rest]
            else:
                self.rewriting.pop(rid, None)
                rest = []
        if rest:
            self.queue.submit(lambda: self._rewrite_next(folder, rid, rest, ok))

    def _read_starts(self, folder: Path) -> None:
        """The reading writes fresh prose with her corrections as settled, so the rewrites made for the last one go."""
        with self.lock:
            records = self._corrections(folder)
            for r in records:
                if r.get("prose") or r.get("prose_state") in ("rewriting", "done", "failed"):
                    r.pop("prose", None)
                    r.pop("prose_state", None)
                self.rewriting.pop(str(r.get("id")), None)
            if records:
                write_json(folder / CORRECTIONS, {"corrections": records})

    def _read_ended(self, folder: Path) -> None:
        """What was corrected during the reading, and waited for it to end, goes in the queue now."""
        for r in self._corrections(folder):
            if r.get("prose_state") == "after reading":
                self._want_prose(folder, str(r.get("id")), after_reading=True)

    def _answer(self, folder: Path, idx: dict, rid: str) -> tuple[str, dict]:
        """What a correction came to, in plain words, and its record."""
        title = idx.get("title")
        done = self._live(folder, idx)[1]
        got = next((d for d in done if d.get("id") == rid), {"id": rid, "applied": False, "tab": "characters", "what": ""})
        return self._told(got, title), {**got, "message": self._told(got, title)}

    def _told(self, got: dict, title: Any) -> str:
        said = str(got.get("what") or "")
        tab = TABS.get(str(got.get("tab")), "Characters")
        if got.get("state") == "working":
            return (f"Your bot is working that out. Nothing has changed yet. When it is done the change shows on the {tab} "
                    "tab, in plain words, with an Undo. The manuscript text is unchanged.")
        if not got.get("applied"):
            if "so nothing needed changing" in str(got.get("why") or ""):
                return f"{got['why']} The manuscript text is unchanged."
            why = f" {got['why']}" if got.get("why") else ""
            return (f"I could not change that in the story bible by myself, so I kept it as a note on the {tab} tab: "
                    f"\"You said: {said}\".{why} The manuscript text is unchanged.")
        line = (f"Changed it: {got['line']}, in the story bible for {title}. It stays that way when your bot reads the "
                "book again. The manuscript text is unchanged.")
        if got.get("partial"):
            line += " Part of what you said could not be matched to the story bible, so that part did not change."
        if got.get("looks"):
            line += " A picture Maps already painted keeps the old look until you press Paint again on it."
        if got.get("prose_state") == "rewriting":
            line += " Your bot is rewriting the paragraphs this touched, and they say so until it is done."
        elif got.get("prose_state") == "after reading":
            line += " The paragraphs this touched are rewritten when the reading ends."
        return line

    def manuscript_correct(self, title: str = "", what: str = "", correction: str = "", tab: str = "") -> str:
        line, rec = self.correct(title, what, correction, tab)
        return f"{line} [{rec.get('id')}]"

    def edit(self, title: Any, asked: Any) -> tuple[str, dict]:
        """Her own edits from the page: the same stored operations, with no call to the brain."""
        folder, idx = self._find(title)
        with self.lock:
            bible = self._live(folder, idx)[0]
            try:
                ops = hand_ops(bible, asked)
            except Bad as exc:
                raise Refused(str(exc)) from None
            records = self._corrections(folder)
            line = line_of(ops)
            rid = self._new_id(records)
            rec = {"id": rid, "what": line[:1].upper() + line[1:], "correction": "", "made_at": _when(), "by": "hand",
                   "ops": ops, "line": line, "state": "done"}
            write_json(folder / CORRECTIONS, {"corrections": records + [rec]})
            self._rebuild(folder)
        return self._answer(folder, idx, rid)

    def undo(self, title: Any, which: Any = "last") -> tuple[str, dict]:
        """Takes one correction back, by its id or "last". The bible goes back to the bible as read with the rest on
        top, so nothing needs reading again."""
        folder, idx = self._find(title)
        ref = " ".join(str(which or "last").lower().split())
        with self.lock:
            records = self._corrections(folder)
            if not records:
                raise Refused("There are no corrections to undo.")
            if ref in ("last", "the last one", "last one", "the last", "latest", "the latest"):
                rec = records[-1]
            else:
                rec = next((r for r in records if str(r.get("id")).lower() == ref), None)
                if rec is None:
                    raise Refused("I could not find that correction. manuscript_corrections lists them with their ids.")
            baked = rec.get("id") in ((self._base(folder) or {}).get("baked") or [])
            write_json(folder / CORRECTIONS, {"corrections": [r for r in records if r is not rec]})
            self._rebuild(folder)
            self.working.discard(str(rec.get("id")))
            self.rewriting.pop(str(rec.get("id")), None)
        said = str(rec.get("line") or rec.get("what") or "")
        if baked:
            line = (f"Took that back: {said}. It was made before Undo existed, so what it changed stays in the story bible "
                    "until your bot reads the book again.")
        else:
            line = f"Took that back: {said}. The story bible is as it was before. The manuscript text is unchanged."
        return line, {"id": rec.get("id"), "what": rec.get("what"), "line": said}

    def manuscript_correct_undo(self, title: str = "", id: str = "last") -> str:  # noqa: A002
        return self.undo(title, id)[0]

    def manuscript_corrections(self, title: str = "") -> str:
        folder, idx = self._find(title)
        done = self._live(folder, idx)[1]
        if not done:
            return f"No corrections on {idx.get('title')} yet."
        out = []
        for d in done:
            state = (f"Changed: {d.get('line')}" if d.get("applied") else "Still being worked out; nothing changed yet."
                     if d.get("state") == "working" else "Kept as a note; nothing changed." +
                     (f" {d['why']}" if d.get("why") else ""))
            out.append(f"[{d.get('id')}] She said: {d.get('what')}. {state}")
        return "\n".join(out)

    # the page

    def view(self) -> dict:
        out = []
        for folder, idx in self._all():
            secs = [{"n": c.get("n"), "heading": c.get("heading"), "words": c.get("words")} for c in idx["sections"]]
            out.append({"title": idx.get("title"), "folder": folder.name, "original": idx.get("original"),
                        "sections": secs, "chapters": secs, "status": self._status_vault(folder, idx),
                        "counts": self.counts(read_json(folder / BIBLE)), "study": idx.get("study") is True})
        return {"manuscripts": out, "tabs": [{"id": k, "label": v} for k, v in TABS.items()]}

    def section_view(self, title: str, section: str) -> dict:
        folder, idx = self._find(title)
        c = self._section(idx, section)
        return {"title": idx.get("title"), "n": c.get("n"), "heading": c.get("heading"), "words": c.get("words"),
                "text": self._section_text(folder, idx, c), "changed": self._changed(folder, idx, c)}

    def _changed(self, folder: Path, idx: dict, c: dict) -> bool:
        """Changed since the last reading, through Editor's Desk: no cache entry for the text as it is now, and the file
        is newer than that reading's start, so a section the reading had to skip does not count."""
        st = self._status(folder, idx)
        if st.get("state") != "done":
            return False
        try:
            path = self._section_path(folder, idx, c)
            if (folder / CACHE / f"{sha_of(path.read_bytes())}.json").exists():
                return False
            began = datetime.strptime(str(st.get("started_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return path.stat().st_mtime >= began.timestamp()
        except (OSError, ValueError, Refused):
            return False

    def status_view(self, title: str) -> dict:
        folder, idx = self._find(title)
        return self._status_vault(folder, idx)

    def _status_vault(self, folder: Path, idx: dict) -> dict:
        st = self._status(folder, idx)
        return {**st, "vault": self.vault_words(st, self.vault_free())}

    def analyze_view(self, title: Any) -> dict:
        line = self.manuscript_analyze(str(title or ""), confirm=True)   # the page's button is the writer's own hand
        folder, idx = self._find(title)
        return {"message": line, "status": self._status(folder, idx)}

    def bible_view(self, title: str) -> dict:
        folder, idx = self._find(title)
        return self._bible(folder, idx)

    def tab_view(self, title: str, tab: str) -> dict:
        folder, idx = self._find(title)
        key = self._tab(tab)
        bible, done = self._live(folder, idx)
        out = {"title": idx.get("title"), "tab": key, "blocks": tab_blocks(bible, key),
               "views": [v for v in self._views(folder) if v.get("tab") == key],
               "corrections": [{**{k: d.get(k) for k in ("id", "what", "correction", "made_at", "applied", "line", "by",
                                                         "state", "why", "prose_state", "partial", "looks")},
                                "message": self._told(d, idx.get("title"))}
                               for d in done if key in (d.get("tabs") or [d.get("tab")])],
               "edits": edit_view(bible, key),
               "rewriting": sorted({k for rid, keys in self.rewriting.items() for k in keys
                                    if any(d.get("id") == rid for d in done)})}
        said = {str((bible.get("briefs") or {}).get(key) or ""): f"brief:{key}",     # the prose blocks' own Edit
                str(bible.get("cast_summary") or ""): "cast:",
                str((bible.get("setting") or {}).get("one_paragraph") or ""): "setting:"}
        out["block_keys"] = {str(i): said[x["text"]] for i, x in enumerate(out["blocks"])
                             if x.get("type") == "markdown" and x.get("text") and x["text"] in said}
        if key == "characters":                     # the page draws these blocks as cards, with links
            got = []
            for c in cards(bible):
                at = next((i for i, x in enumerate(out["blocks"]) if x.get("type") == "markdown"
                           and x.get("text") == c["text"][:2000]), None)
                if at is not None:
                    got.append({**{k: v for k, v in c.items() if k != "text"}, "block": at})
            out["cards"] = got
        return out

    def views_view(self, title: str, tab: str = "") -> dict:
        folder, _idx = self._find(title)
        key = self._tab(tab) if str(tab or "").strip() else ""
        return {"views": [v for v in self._views(folder) if not key or v.get("tab") == key]}

    def manuscript_remove(self, title: Any = None) -> str:
        """Put one manuscript away: its whole folder moves into Removed inside Manuscripts. Nothing is deleted."""
        folder, idx = self._find(title)
        if self.reader.busy(folder):                 # 0.3.4: only a reading that is really running, never a stale status file
            raise Refused(f"Your bot is still reading {idx.get('title')}. Wait for it to finish, then put it away.")
        removed = self.shelf() / REMOVED
        removed.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = removed / f"{folder.name} {stamp}"
        with self.lock:
            last = None
            for _try in range(6):                   # Windows may hold a file for a moment after a write
                try:
                    shutil.move(str(folder), str(target))
                    last = None
                    break
                except (shutil.Error, OSError) as exc:
                    last = exc
                    time.sleep(0.3)
            if last is not None:
                raise Refused(f"I could not put {idx.get('title')} away just now: a file in it is still in use. "
                              "Try again in a moment. Nothing was changed.")
        return (f"Put {idx.get('title')} away. Its folder, with the original file, the story bible, views, notes and "
                f"pictures, is now in {REMOVED} inside Manuscripts. Nothing was deleted; move the folder back to get it again.")

    def views_delete(self, title: str, vid: str) -> dict:
        folder, _idx = self._find(title)
        with self.lock:
            views = self._views(folder)
            keep = [v for v in views if v.get("id") != vid]
            if len(keep) == len(views):
                raise Refused("That view is not there any more.")
            write_json(folder / VIEWS, {"views": keep})
        return {"message": "Removed that view."}

    def pointer_view(self, title: str) -> dict:
        folder, idx = self._find(title)
        got = read_json(folder / POINTER)
        return got if isinstance(got, dict) else {"title": idx.get("title"), "set_at": None}

    def pointer_clear(self, title: str) -> dict:
        folder, _idx = self._find(title)
        try:
            (folder / POINTER).unlink()
        except FileNotFoundError:
            pass
        return {"message": "Cleared."}

    def notes_view(self, title: str, tab: str) -> dict:
        folder, idx = self._find(title)
        key = self._tab(tab)
        return {"title": idx.get("title"), "tab": key, "text": self._notes(folder, key),
                "stamp": _stamp(self._notes_path(folder, key))}

    def notes_save(self, title: str, tab: str, text: Any, stamp: Any = None) -> dict:
        """The writer's own Save on the page: the whole notes file. Refused when the file changed since she opened
        it (her bot may have added a note), so nothing she didn't see is written over."""
        body = str(text or "")
        if len(body) > NOTES_FILE_MAX:
            raise Refused("Those notes are too long to save in one file.")
        folder, idx = self._find(title)
        key = self._tab(tab)
        path = self._notes_path(folder, key)
        with self.lock:
            there = path.exists()                    # gone since she opened it: a tidy took an empty file, nothing is lost
            if there and stamp is not None and str(stamp) != _stamp(path):
                raise Refused("These notes changed since you opened them (your bot may have added one). "
                              "Copy what you wrote, then press Reload.")
            if not there and body.strip() in ("", f"# {TABS[key]}"):
                return {"message": f"Saved the {TABS[key]} notes.", "stamp": ""}      # nothing to keep: no file yet
            _write(path, body if body.endswith("\n") or not body else body + "\n")
            return {"message": f"Saved the {TABS[key]} notes.", "stamp": _stamp(path)}

    def upload(self, file_name: Any, content_b64: Any, title: Any = None, read: Any = True) -> dict:
        """Her Upload. ``read`` False is "Add a book to study": a book she admires, put on the shelf for Editor's Desk to
        compare with hers. It is never read into a story bible, and it is marked so the pages can say so."""
        name = os.path.basename(str(file_name or "").replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_TYPES:
            raise Refused("Upload a Word file (.docx), a PDF (.pdf), a text file (.txt) or a Markdown file (.md).")
        try:
            raw = base64.b64decode(str(content_b64 or ""), validate=True)
        except (binascii.Error, ValueError):
            raise Refused("That file did not arrive whole. Try the upload again.") from None
        study = read is False
        got = self._add(name, ext, raw, title, study=study)
        if not study:
            self.reader.start(got["folder"])
        self.tidy_due()
        got["message"] = f"Added {got['title']}: {got['sections']} sections, {got['words']} words. " + (
            "It is a book to study. Open Editor's Desk to compare it with yours." if study else "Your bot is reading it now.")
        if got.get("report"):
            got["message"] += " " + pdf_text.report_line(got["report"])
        return {k: v for k, v in got.items() if k not in ("folder", "words")}

    def manuscript_add_file(self, source: Any = None, title: Any = None) -> str:
        """A file already in My files, as a new manuscript. It adds and stops: the reading waits for the writer's yes."""
        path = self._my_file(source)
        ext = path.suffix.lower()
        if ext not in UPLOAD_TYPES:
            raise Refused("I can add a Word file (.docx), a PDF (.pdf), a text file (.txt) or a Markdown file (.md).")
        got = self._add(path.name, ext, path.read_bytes(), title)
        self.tidy_due()
        line = f"Added {got['title']} from My files: {got['sections']} sections, {got['words']} words."
        if got.get("report"):
            line += " " + pdf_text.report_line(got["report"])
        return (line + f" The reading has not started; it waits for the writer's yes. Ask her, then call manuscript_analyze "
                       f"with this title and confirm true.")

    def _my_file(self, source: Any) -> Path:
        """One file under My files, by its name or its path inside My files. Never outside: no "..", no links."""
        raw = str(source or "").strip().strip('"').replace("\\", "/")
        if not raw:
            raise Refused("Say which file in My files.")
        outside = "I can only add a file from My files, and that one is outside it. Nothing was added."
        if "\x00" in raw or ".." in raw.split("/"):
            raise Refused(outside)
        root = self.shelf().parent
        real_root = root.resolve()
        if os.path.isabs(raw) or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            for base in (root, real_root):
                try:
                    rel = Path(raw).relative_to(base)
                    break
                except ValueError:
                    continue
            else:
                raise Refused(outside)
            path = root / rel
        elif "/" in raw:
            path = root / raw
        else:
            path = self._named_in_my_files(root, raw)
        here = root
        for part in path.relative_to(root).parts:     # a link anywhere on the way: refused, wherever it points
            here = here / part
            if here.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(here):
                raise Refused(outside)
        if real_root not in path.resolve().parents:
            raise Refused(outside)
        if not path.is_file():
            raise Refused(f"I could not find {raw} in My files. Nothing was added.")
        return path

    @staticmethod
    def _named_in_my_files(root: Path, name: str) -> Path:
        """A bare name: its place in Converted first, then anywhere in My files but the manuscripts' own folders."""
        want = name.lower()
        found = []
        for here, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(here) == root and d == SHELF))
            for n in sorted(names):
                low = n.lower()
                if low == want or (os.path.splitext(low)[0] == want and os.path.splitext(low)[1] in UPLOAD_TYPES):
                    found.append(Path(here) / n)
        first = [p for p in found if p.parent == root / CONVERTED]
        if len(first) == 1 or len(found) == 1:
            return first[0] if first else found[0]
        if not found:
            raise Refused(f"I could not find {name} in My files. Nothing was added.")
        names = ", ".join(str(p.relative_to(root)).replace("\\", "/") for p in found[:8])
        raise Refused(f"More than one file is called that: {names}. Say which, with its folder.")

    def _add(self, name: str, ext: str, raw: bytes, title: Any, study: bool = False) -> dict:
        """The one way in, for the page's Upload and for manuscript_add_file: the folder, the sections and the index.
        It starts no reading; the caller decides that."""
        if not raw:
            raise Refused("That file is empty.")
        report = None
        if ext == ".pdf":
            book = pdf_book(raw)
            paras, report = book.tuples(), book.report
        elif ext == ".docx":
            try:
                paras = docx_paragraphs(raw)
            except ImportError:
                raise Refused("Manuscript is missing a part it needs. Remove it and add it again from Apps.") from None
            except Exception:  # noqa: BLE001 - a file it cannot read: a plain line, never the file's words
                raise Refused("I could not read that Word file. Save it again in Word as .docx and upload it.") from None
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp1252", errors="replace")
            paras = text_paragraphs(text, ext == ".md")
        sections, front = split_sections(paras, ext != ".pdf")
        shown = " ".join(str(title or "").split())[:120] or os.path.splitext(name)[0] or "Untitled"
        with self.lock:
            shelf = self.shelf()
            shelf.mkdir(parents=True, exist_ok=True)
            self._mark(shelf)
            stem = _safe(shown, "Untitled", 60)
            folder, n = shelf / stem, 2
            while folder.exists():
                folder, n = shelf / f"{stem} ({n})", n + 1
            if n > 2:
                shown = f"{shown} ({n - 1})"
            (folder / SECTIONS).mkdir(parents=True)
            (folder / ("original" + ext)).write_bytes(raw)
            index = []
            width = max(2, len(str(len(sections))))
            first = 0 if front else 1                       # text before the first heading is section 0, so the
            for i, (head, text) in enumerate(sections, first):   # numbers match "Chapter 1", "Chapter 2"
                fname = f"{str(i).zfill(width)} - {_safe(head, 'Section', 60)}.md"
                content = f"# {head}\n\n{text}\n" if text else f"# {head}\n"
                _write(folder / SECTIONS / fname, content)
                index.append({"n": i, "heading": head, "file": fname, "words": words(text),
                              "sha": sha_of(content.encode("utf-8"))})
            record = {"title": shown, "original": name, "original_file": "original" + ext,
                      "added": datetime.now().isoformat(timespec="seconds"),
                      "version": VERSION, "sections": index, "chapters": index}
            if report:
                record["pdf_report"] = report
            if study:
                record["study"] = True                      # a book she admires: compared in Editor's Desk, never read
            _write(folder / INDEX, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        got = {"title": shown, "sections": len(index), "chapters": len(index), "folder": folder,
               "words": sum(c["words"] for c in index)}
        if report:
            got["report"] = report
        return got


# -- MCP ------------------------------------------------------------------------------------------

CHANGES = ("manuscript_notes_append", "manuscript_remove", "manuscript_add_file")
_TITLE = {"type": "string", "description": "The manuscript's title. May be left empty when there is only one."}
_TAB = {"type": "string", "enum": list(TABS), "description": "Which tab: characters, plot (plot and subplots), timeline, "
        "structure, world, science, themes or voice."}
_SECTION = {"type": "string", "description": "The section's number, like 3, or its heading."}
TOOLS = {
    "manuscript_list": ("The writer's manuscripts on this computer, each with its sections, their numbers and word "
                        "counts, how far her bot has read it, and the real counts of what it found.", {}, []),
    "manuscript_read": ("The text of one section of one of the writer's manuscripts, by its number or heading. "
                        "Reading only; manuscript text is never changed by this package.",
                        {"title": _TITLE, "section": _SECTION,
                         "chapter": {"type": "string", "description": "The same as section, the 0.1.0 name."}},
                        []),
    "manuscript_status": ("How far her bot has read one manuscript into its story bible, in one line, then the real counts of "
                          "what it found: characters, main characters, plot threads, places and events.",
                          {"title": _TITLE}, []),
    "manuscript_analyze": ("Starts her bot reading one manuscript again, section by section, to rebuild its story "
                           "bible. The manuscript text is not changed. It starts only after the writer said yes, in "
                           "their latest message; asked without that, it only answers with the question to ask.",
                           {"title": _TITLE, "confirm": {"type": "boolean", "description": "True only after the writer said yes, in their latest message, to reading this exact title."}}, []),
    "manuscript_ask": ("An answer to a question about one manuscript, from its story bible and the sections that "
                       "match the question best, citing sections by number.",
                       {"title": _TITLE, "question": {"type": "string", "description": "The question, in her words."}},
                       ["question"]),
    "manuscript_show": ("Makes a view on one tab of the Manuscript page, such as a character's arc, a timeline or a "
                        "family tree, from the story bible, and keeps it on that tab.",
                        {"title": _TITLE, "tab": _TAB,
                         "asked": {"type": "string", "description": "What she asked to see, in her words."}},
                        ["tab", "asked"]),
    "manuscript_point": ("Points the Manuscript page at a passage: it opens the section, scrolls to the words and "
                         "highlights them.",
                         {"title": _TITLE, "section": {"type": "string", "description": "The section's number or "
                                                       "heading. When empty, every section is searched."},
                          "quote": {"type": "string", "description": "Words from the passage."},
                          "note": {"type": "string", "description": "A short line shown above the passage."}},
                         ["quote"]),
    "manuscript_correct": ("Corrects any fact in a manuscript's story bible, on any tab, in the writer's words: what "
                           "a character looks like, a fact on the wrong character, two names that are one person, "
                           "when something happens, a theme, a science claim, a wrong fact to remove, a missing one to "
                           "add. The answer says exactly what changed, or that it was kept as a note, or that it is "
                           "still being worked out. The change stays when the book is read again. The manuscript text "
                           "is not changed.",
                           {"title": _TITLE,
                            "what": {"type": "string", "description": "What she said, in her words, like That scar "
                                                                      "belongs to Tom, not Elias."},
                            "correction": {"type": "string", "description": "The right value on its own, when she gave "
                                                                            "one apart from the rest."},
                            "tab": {**_TAB, "description": "The tab she is on, when known. May be left empty."}},
                           ["what"]),
    "manuscript_correct_undo": ("Takes one correction back, when the writer says it was wrong, and the story bible goes "
                                "back as it was before it. By its id, shown in square brackets in the answer that made "
                                "it, or last for the newest.",
                                {"title": _TITLE,
                                 "id": {"type": "string", "description": "The correction's id, or last."}},
                                []),
    "manuscript_corrections": ("Every correction on one manuscript, oldest first, each with its id, what she said and "
                               "what it came to: what changed, a note, or still being worked out.",
                               {"title": _TITLE}, []),
    "manuscript_notes_read": ("The writer's notes for one manuscript on one subject: characters, plot and subplots, "
                              "timeline, structure, world building, science, themes or voice.",
                              {"title": _TITLE, "tab": _TAB}, ["tab"]),
    "manuscript_notes_append": ("Adds a dated section to the end of the writer's notes for one manuscript on one "
                                "subject. Her earlier notes and her manuscript stay as they are.",
                                {"title": _TITLE, "tab": _TAB,
                                 "text": {"type": "string", "description": "What goes in the notes, as plain text "
                                                                           "or Markdown."}},
                                ["tab", "text"]),
}


TOOLS["manuscript_remove"] = (
    "Puts one of the writer's manuscripts away: its whole folder, with the original file, the story bible, views, notes "
    "and pictures, moves into a Removed folder inside Manuscripts, where it can be got back. Nothing is deleted. "
    "Refused while her bot is still reading it.",
    {"title": _TITLE}, ["title"])


# 0.3.5: the argument is "source", not "path" or "file": the Core reads those names as a file being changed, and hides
# such a tool while "Let your bot change your documents" is off. This one reads a file and changes nothing of hers.
TOOLS["manuscript_add_file"] = (
    "Adds a file from My files as a new manuscript: a Word file, PDF, text or Markdown file, by its name or its path "
    "inside My files, such as Converted/My Novel.docx. The file stays where it is. The reading does not start: it "
    "waits for the writer's yes, and then manuscript_analyze with confirm true starts it. A PDF's answer says what was "
    "found in it.",
    {"source": {"type": "string", "description": "The file's name, or its path inside My files, like Converted/My Novel.docx."},
     "title": {"type": "string", "description": "The manuscript's title. Left empty, the file's name."}},
    ["source"])


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc,
             "annotations": {"readOnlyHint": name not in CHANGES, "destructiveHint": False},
             "inputSchema": {"type": "object", "properties": props, "required": need, "additionalProperties": False}}
            for name, (desc, props, need) in TOOLS.items()]


def mcp_answer(app: Manuscripts, msg: dict) -> dict | None:
    method = msg.get("method") if isinstance(msg, dict) else None
    mid = msg.get("id") if isinstance(msg, dict) else None
    if mid is None and method:
        return None                          # a notification

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0], "capabilities": {"tools": {}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS or not isinstance(args, dict):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "No such tool."}}
        try:
            text = getattr(app, name)(**{k: v for k, v in args.items() if k in TOOLS[name][1]})
            return ok({"content": [{"type": "text", "text": text}]})
        except Refused as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except OSError as exc:
            return ok({"content": [{"type": "text", "text": f"I could not reach that file ({type(exc).__name__}). "
                                                            "Nothing was changed."}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "No such method."}}


# -- HTTP -----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "manuscript/" + VERSION
    protocol_version = "HTTP/1.1"
    app: Manuscripts
    token = ""

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self._sent = True
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    _raw: bytes | None = None
    _too_big = False

    def _drain(self, limit: int = MAX_BODY) -> bytes:
        """This request's body, read once before any reply (a reply over an unread body resets the connection on
        Windows). A body over *limit* is read and dropped, then refused."""
        if self._raw is None:
            raw_len = self.headers.get("Content-Length") or "0"
            n = int(raw_len) if raw_len.isascii() and raw_len.isdigit() else 0
            self._too_big = n > limit
            if self._too_big and n > MAX_UPLOAD:
                self.close_connection = True
                self._raw = b""
            else:
                self._raw = self.rfile.read(n) if n > 0 else b""
        return self._raw

    def _body(self, limit: int = MAX_BODY) -> dict:
        raw = self._drain(limit)
        if self._too_big:
            raise ValueError("That is too big for Manuscript.")
        got = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(got, dict):
            raise ValueError("Send a JSON object.")
        return got

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self, limit: int = MAX_BODY) -> bool:
        if self._token_ok():
            return False
        self._drain(limit)
        self._json(401, {"error": "Manuscript needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guard(self, fn) -> None:
        self._sent = False
        self._raw, self._too_big = None, False     # a new request on this connection: its own body
        try:
            fn()
        except Refused as exc:
            if not self._sent:
                self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            if not self._sent:
                try:
                    self._json(500, {"error": f"Manuscript stopped on this request ({type(exc).__name__}). "
                                              "Nothing was changed."})
                except Exception:  # noqa: BLE001 - the connection itself is gone
                    pass

    def _query(self) -> tuple[str, dict]:
        path, _, query = self.path.partition("?")
        return path, {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guard(self._get)

    def _get(self):
        if self._refused():
            return
        path, q = self._query()
        a = self.app
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        if path == "/api/list":
            return self._json(200, a.view())
        if path == "/api/section":
            return self._json(200, a.section_view(q.get("title", ""), q.get("n", q.get("section", ""))))
        if path == "/api/chapter":
            return self._json(200, a.section_view(q.get("title", ""), q.get("chapter", "")))
        if path == "/api/status":
            return self._json(200, a.status_view(q.get("title", "")))
        if path == "/api/bible":
            return self._json(200, a.bible_view(q.get("title", "")))
        if path == "/api/tab":
            return self._json(200, a.tab_view(q.get("title", ""), q.get("tab", "")))
        if path == "/api/views":
            return self._json(200, a.views_view(q.get("title", ""), q.get("tab", "")))
        if path == "/api/settings":
            return self._json(200, {"settings": a.settings()})
        if path == "/api/notes":
            return self._json(200, a.notes_view(q.get("title", ""), q.get("tab", "")))
        if path == "/api/pointer":
            return self._json(200, a.pointer_view(q.get("title", "")))
        if path == "/api/size":
            return self._json(200, a.size_view(q.get("title", "")))
        name = urllib.parse.unquote(path.lstrip("/"))
        file = (WEB / name).resolve()
        if name and file.suffix.lower() in WEB_TYPES and WEB.resolve() in file.parents and file.is_file():
            return self._send(200, file.read_bytes(), WEB_TYPES[file.suffix.lower()])   # the page's own files
        self._json(404, {"error": "Manuscript has no page here."})

    def do_POST(self):
        self._guard(self._post)

    def _post(self):
        path = self.path.split("?", 1)[0]
        limit = MAX_UPLOAD if path == "/api/upload" else MAX_BODY
        if self._refused(limit):
            return
        try:
            msg = self._body(limit)
        except (ValueError, UnicodeDecodeError) as exc:
            self.close_connection = self._too_big
            if path == "/mcp":
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
            return self._json(413 if self._too_big else 400, {"error": str(exc) if self._too_big else "Not JSON."})
        a = self.app
        if path == "/mcp":
            answer = mcp_answer(a, msg)
            return self._send(202) if answer is None else self._json(200, answer)
        if path == "/api/notes":
            return self._json(200, a.notes_save(msg.get("title"), msg.get("tab"), msg.get("text"), msg.get("stamp")))
        if path == "/api/upload":
            return self._json(200, a.upload(msg.get("file_name"), msg.get("content_b64"), msg.get("title"), msg.get("read", True)))
        if path == "/api/analyze":
            return self._json(200, a.analyze_view(msg.get("title")))
        if path == "/api/correct":
            line, rec = a.correct(msg.get("title"), msg.get("what"), msg.get("correction"), msg.get("tab"))
            return self._json(200, {"message": line, "correction": rec})
        if path == "/api/correct/undo":
            line, rec = a.undo(msg.get("title"), msg.get("id"))
            return self._json(200, {"message": line, "correction": rec})
        if path == "/api/edit":
            line, rec = a.edit(msg.get("title"), msg.get("ops"))
            return self._json(200, {"message": line, "correction": rec})
        if path == "/api/settings":
            return self._json(200, a.settings_save(msg))
        if path == "/api/show":
            line, view = a.show(msg.get("title"), msg.get("tab"), msg.get("asked"), "page")
            return self._json(200, {"message": line, "view": view})
        self._json(404, {"error": "Manuscript has no page here."})

    def do_DELETE(self):
        self._guard(self._delete)

    def _delete(self):
        if self._refused():
            return
        self._drain()
        path, q = self._query()
        if path == "/api/manuscript":
            return self._json(200, {"message": self.app.manuscript_remove(q.get("title", ""))})
        if path == "/api/views":
            return self._json(200, self.app.views_delete(q.get("title", ""), q.get("id", "")))
        if path == "/api/pointer":
            return self._json(200, self.app.pointer_clear(q.get("title", "")))
        self._send(405, b"", "text/plain")

    def do_PUT(self):
        self._raw, self._too_big = None, False
        if not self._refused():
            self._drain()
            self._send(405, b"", "text/plain")


def make_server(port: int, files: Any, token: str | None, brain: Brain | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    handler = type("ManuscriptHandler", (Handler,), {"app": Manuscripts(files, brain), "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manuscript for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    token = os.environ.get("PUFFPAI_TOKEN")
    api = os.environ.get("PUFFPAI_API")
    if not args.port or not api:
        p.error("PuffPAI starts Manuscript with PUFFPAI_PORT (or --port) and PUFFPAI_API set.")
    server = make_server(args.port, CoreFiles(api, token), token, Brain(api, token))
    threading.Thread(target=server.RequestHandlerClass.app.at_start, daemon=True, name="tidying at start").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
