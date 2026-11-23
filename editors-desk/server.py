"""Editor's Desk: a publishing house's stages of editing for a writer's book, run by "your editor" in the background.

One small server, bound to 127.0.0.1. The design is in WritersBot's EDITORS_DESK.md:
- ``/``                 the page (web/index.html), shown in the Desktop; ``/health`` "ok"; ``/mcp`` the tools
- ``/api/list``         GET: the books on the shelf
- ``/api/open``         POST ``{title}``: brings the old desk's marks over, once
- ``/api/desk``         GET ``?title=``: everything the page shows for one book
- ``/api/book``         GET ``?title=``: every section in order, its text as the file has it (italic and bold marks and all),
                        its sha, whether it changed since the book was read, and how many earlier versions it has
- ``/api/section``      GET ``?title=&n=``: one section's text, its sha and its earlier versions
- ``/api/version``      GET ``?title=&n=&id=``: one earlier version
- ``/api/save``         POST ``{title, n, text, sha, auto?}``: the writer's own words. The old version is kept first; an
                        autosave keeps one at most every ten minutes. A stale sha is refused with the text there now
- ``/api/ask``          POST ``{title, n, passage, question, who?}``: her question about her own words, to her editor (or one of
                        the staff, by ``who``), ahead of a check's next call. ``/api/asks`` GET ``?title=``: the last 100; POST ``{title, id}`` removes one
- ``/api/restore``      POST ``{title, n, id, sha}``: an earlier version back, the one before it kept too
- ``/api/use``          POST ``{title, id}``: PROPOSED, Use this: a grammar or proofreading correction, on her click only
- ``/api/run``          POST ``{title, check, plan?}``: her press; ``/api/stop`` POST ``{title}``
- ``/api/mark``         POST ``{title, id, state?, note?}``
- ``/api/document``     GET ``?title=&id=``; POST ``{title, id, text}``: a document she keeps, never the manuscript
- ``/api/settings``     GET, POST ``{vault_free}``
- ``/api/study/books``  GET ``?title=``: the other books on the shelf she could compare hers with, books to study first
- ``/api/study/numbers`` GET ``?title=&theirs=``: the style numbers of both books side by side, by code, at once
- ``/api/study``        POST ``{title, theirs, focus?}``: her press: your editor compares matched passages, in the same line
                        as the checks. GET ``?title=&theirs=``: the latest report for that pair; ``?title=``: the list
- ``/api/wordcopy``     POST ``{title}``: a Word copy of her book as it stands, to read or share, in My files
- ``/api/board``        GET ``?title=``: the storyboard, what changed since, and each panel's picture; POST ``{title,
                        sections}``: her press, the storyboard artist draws those chapters; ``/api/board/plan``,
                        ``/api/board/picture``, and POST ``/api/board/paint {title, panels}``: her press, the file Maps paints from
- ``/api/office``       GET ``?title=``: the editor-in-chief's office: the meeting, the staff and what each is doing, by code
- ``/api/visit``        POST ``{title}``: stamps her visit to the office, so the next meeting says what is new since
- ``/api/pass``         GET ``?title=&who=``: the plan for one desk going over the book (starts nothing); POST ``{title, who}``:
                        her press, the desk's checks in line

Every route refuses a request without this package's PUFFPAI_TOKEN. GET never changes anything. No tool writes section
text, and no brain answer reaches a section: only ``/api/save``, ``/api/restore`` and ``/api/use`` do, from her hand.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))      # its own modules, also when started with python -I
import book as books  # noqa: E402
import docx_out  # noqa: E402
import engine  # noqa: E402
import marks  # noqa: E402
import staff  # noqa: E402
import storyboard  # noqa: E402
import study  # noqa: E402
from book import SHARED, Book, Refused, find_book, locate, read_index, save_section, section_path, version_text, versions  # noqa: E402
from brainline import (BrainBusy, BrainDown, BrainQueue, BrainSlow, NotYet, Brain, now, plain, read_json, sha_of, strip_think,  # noqa: E402
                       tokens, write_atomic, write_json)
from engine import NeedsReading, Stopped  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
PAGE = WEB / "index.html"
NAME = "editors-desk"
VERSION = "0.4.3"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 4 * 1024 * 1024
WEB_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
             ".html": "text/html; charset=utf-8"}
SHELF = "Manuscripts"
USE_THIS = True            # PROPOSED: the owner may remove this. False takes the route away and hides the button
ASK_WAIT = float(os.environ.get("EDITORS_DESK_ASK_WAIT_S") or 50)   # a tool call stops at 60 seconds
DEFAULT_CONTEXT = 8192
# What model_profiles.json sets, for the room in an ask, until the Core's answer says it itself (PROPOSED: one line in
# package_chat). settings.json's context_tokens wins over this table.
MODEL_CONTEXT = {"qwen3:30b-a3b": 8192, "qwen3.5:9b": 16384, "qwen3-coder:30b": 16384, "qwen2.5:7b-instruct": 8192}
DEFAULTS = {"vault_free": True, "context_tokens": 0}
JOBS = "jobs.json"
TIMING = "timing.json"
SETTINGS = "settings.json"
ASKS = "asks.json"
ASKS_KEPT = 100
ASK_PAGE_WAIT = float(os.environ.get("EDITORS_DESK_PAGE_ASK_WAIT_S") or 25)   # then the page asks again for the answer
STUDY = "study"                 # in a book's working folder: its measure, and one report per book it was compared with
STUDY_NAME = "Study a book"
VISIT = "visit.json"            # in a book's working folder: her last visit to the office, {at, shas, words}
PASS = "pass.json"              # in a book's working folder: the last pass she started, {id, who, total, started_at}
MEETING_MOST = 8
BOARD = "storyboard"            # a job kind, and in a book's working folder: board.json and the saved answers
BOARD_NAME = "Storyboard"
BOARD_ASK = "storyboard.json"   # beside the bible: the panels she asked to have painted, for Maps (CONTRACT.md)
PAINT_MOST = 40
PAINT_WAIT_S = 120              # after her press, how long before the page says Maps has not started
PICTURE_ID = re.compile(r"[A-Za-z0-9-]{1,80}")
KIND_STAFF = {STUDY: "line", BOARD: "board"}     # a job that is not a check, by whose desk it is on


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
            raise Refused("I could not ask your bot where your files are, so nothing was read or changed.")
        return Path(made).parent / SHELF


class Timing:
    """Real measured call times, kept over restarts: this is where "about 20 minutes left" comes from."""

    def __init__(self, path: Path) -> None:
        self.path = path
        got = read_json(path, {}) or {}
        self.n, self.total = int(got.get("n") or 0), float(got.get("seconds") or 0)
        self.model = str(got.get("model") or "")

    def note(self, seconds: float, model: str = "") -> None:
        self.n, self.total = self.n + 1, self.total + seconds
        self.model = model or self.model
        if self.n > 200:                 # the recent past counts most
            self.n, self.total = 100, self.total / 2
        try:
            write_json(self.path, {"n": self.n, "seconds": round(self.total, 1), "model": self.model})
        except OSError:
            pass

    def left(self, calls: int) -> int | None:
        return int(self.total / self.n * max(0, calls)) if self.n >= 2 else None


def _when_left(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    if seconds < 90:
        return "about a minute left"
    if seconds < 3600:
        return f"about {round(seconds / 60)} minutes left"
    return f"about {seconds / 3600:.1f} hours left"


def _and(names: list[str]) -> str:
    """A list in plain words: "a", "a and b", "a, b and c"."""
    names = [str(x) for x in names if x]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _sections(ns: list[int]) -> str:
    if len(ns) == 1:
        return f"section {ns[0]}"
    return f"sections {_and([str(n) for n in ns])}" if len(ns) <= 6 else f"{len(ns)} sections"


def _day(iso: str) -> str:
    """The day of a stamp, on this computer's clock: "Friday 14 November"."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return ""
    return f"{d:%A} {d.day} {d:%B}"


class Desk:
    def __init__(self, files: Any, brain: Brain | None = None) -> None:
        self.files = files
        self.brain = brain or Brain("", "")
        self.data = engine.data_root()
        self.data.mkdir(parents=True, exist_ok=True)
        self.brain.as_written = self.vault_free
        self.queue = BrainQueue()
        self.timing = Timing(self.data / TIMING)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_flag: set[str] = set()
        self.asks_lock = threading.Lock()
        self.asking: set[str] = set()
        self.study_lock = threading.Lock()
        self.costs: dict[tuple, int] = {}       # brain calls a check would make, by its book's text and the room: the office is instant
        threading.Thread(target=self._worker, daemon=True, name="your editor").start()

    # settings

    def settings(self) -> dict:
        got = read_json(self.data / SETTINGS, {})
        got = got if isinstance(got, dict) else {}
        return {k: (got[k] if isinstance(got.get(k), type(v)) else v) for k, v in DEFAULTS.items()}

    def vault_free(self) -> bool:
        return bool(self.settings()["vault_free"])

    def settings_save(self, msg: Any) -> dict:
        if not isinstance(msg, dict) or not isinstance(msg.get("vault_free"), bool):
            raise Refused("Say vault_free: true or false.")
        got = {**self.settings(), "vault_free": msg["vault_free"]}
        write_json(self.data / SETTINGS, got)
        return {"settings": got, "message": "Manuscripts stay out of the vault." if got["vault_free"] else "Manuscripts go through the vault."}

    def context(self) -> int:
        """How many tokens the brain has: what its answer says (PROPOSED), the setting, the table by model, else 8,192."""
        told = getattr(self.brain, "last", {}).get("context")
        if isinstance(told, int) and told > 0:
            return told
        mine = int(self.settings().get("context_tokens") or 0)
        if mine > 0:
            return mine
        model = str(getattr(self.brain, "last", {}).get("model") or self.timing.model or "")
        return MODEL_CONTEXT.get(model, DEFAULT_CONTEXT)

    def room(self) -> int:
        return int(self.context() * engine.ROOM_SHARE)

    # books

    def shelf(self) -> Path:
        return self.files.shelf()

    def _folder(self, title: Any) -> Path:
        shelf = self.shelf()
        if not " ".join(str(title or "").split()):         # left empty: her one manuscript, not a book she studies
            mine = [f for f in books.all_books(shelf) if read_index(f).get("study") is not True]
            if len(mine) == 1:
                return mine[0]
        return find_book(shelf, title)

    def list_view(self) -> dict:
        out = []
        for f in books.all_books(self.shelf()):
            idx = read_index(f)
            out.append({"title": idx.get("title") or f.name, "sections": len(idx.get("sections") or []),
                        "words": sum(int(c.get("words") or 0) for c in idx.get("sections") or []),
                        "read": (f / books.BIBLE).exists(), "study": idx.get("study") is True})
        return {"books": out}

    def open_book(self, title: Any) -> dict:
        folder = self._folder(title)
        return {"carried": engine.carry_old_desk(folder)}

    # the line of pressed checks

    def _jobs(self) -> list[dict]:
        got = read_json(self.data / JOBS, [])
        return [j for j in got if isinstance(j, dict)] if isinstance(got, list) else []

    def _save_jobs(self, jobs: list[dict]) -> None:
        write_json(self.data / JOBS, jobs)

    def _update(self, job: dict) -> None:
        with self.lock:
            jobs = self._jobs()
            for i, j in enumerate(jobs):
                if j.get("id") == job.get("id"):
                    jobs[i] = job
            self._save_jobs(jobs)

    def start(self, title: Any, check: Any, plan: Any = "", who: str = "", pass_id: str = "") -> tuple[dict, dict]:
        """Her press, or her Yes through a tool. The only way a check ever starts. A pass gives ``who`` and its id."""
        checks = engine.load_checks()
        c = checks.get(str(check or "").strip().lower())
        if c is None:
            raise Refused("There is no check by that name. The checks are: " + ", ".join(checks) + ".")
        folder = self._folder(title)
        engine.carry_old_desk(folder)
        words = chr(10).join(plain(x) for x in str(plan or "").splitlines()).strip()[:1000]
        if c["box_needed"] and not words:
            raise Refused(f"Say what goes in the box first: {c['box']}")
        b = Book(folder)
        if c["needs_bible"] and not b.bible:
            raise Refused(f"{c['name']} goes by the story bible, and {b.title} has not been read yet. Read it in Manuscript first.")
        with self.lock:
            jobs = self._jobs()
            if any(j.get("folder") == folder.name and j.get("check") == c["id"] for j in jobs):
                raise Refused(f"{c['name']} is already in line for {b.title}.")
            job = {"id": f"{int(time.time() * 1000)}-{c['id']}", "folder": folder.name, "title": b.title, "check": c["id"], "check_name": c["name"],
                   "plan": words, "state": "queued", "step": "In line", "done": 0, "total": 0, "found": 0, "problems": [], "started_at": None, "pressed_at": now()}
            if pass_id:
                job.update(who=who, **{"pass": pass_id})
            jobs.append(job)
            self._save_jobs(jobs)
        self.wake.set()
        return job, c

    def stop(self, title: Any) -> str:
        folder = self._folder(title)
        with self.lock:
            jobs = self._jobs()
            mine = [j for j in jobs if j.get("folder") == folder.name]
            if not mine:
                return "Nothing is running for this book."
            waiting = [j for j in mine if j.get("state") == "queued"]
            self._save_jobs([j for j in jobs if j not in waiting])
            for j in mine:
                if j not in waiting:
                    self.stop_flag.add(j["id"])
        return "Stopping after the call in progress. Press the check again and it goes on from where it was."

    def _book_status(self, job: dict) -> None:
        """Kept with the app's files, and as one small file beside the bible, so a sibling app can see what the brain is
        doing, in Manuscript's shape."""
        try:
            folder = self.shelf() / job["folder"]
            if folder.is_dir():
                st = {k: job.get(k) for k in ("state", "step", "done", "total", "started_at", "finished_at", "check", "check_name", "found", "left_s", "problems")}
                write_json(engine.home_of(folder) / "status.json", st)
                write_json(folder / SHARED, {k: st[k] for k in ("state", "step", "done", "total", "check_name")})
        except (OSError, Refused):
            pass

    def _worker(self) -> None:
        """One check at a time, in the order she pressed them. A check that was running when the app stopped goes on."""
        tidied = False
        while True:
            jobs = self._jobs()
            if not tidied:
                try:
                    engine.tidy(self.shelf())
                    tidied = True
                except (Refused, OSError):
                    pass
            if not jobs:
                self.wake.wait(5)
                self.wake.clear()
                continue
            job = jobs[0]
            try:
                shelf = self.shelf()
            except Refused:
                time.sleep(5)                    # the Core is not up yet
                continue
            self._run(job, shelf)

    def _run(self, job: dict, shelf: Path) -> None:
        if job.get("kind") == STUDY:
            self._run_study(job, shelf)
            return
        if job.get("kind") == BOARD:
            self._run_board(job, shelf)
            return
        folder = shelf / job["folder"]
        checks = engine.load_checks()
        c = checks.get(job.get("check"))
        if c is None or not folder.is_dir():
            self._finish(job, "failed", "That check or book is no longer there.")
            return
        resumed = job.get("state") in ("running", "waiting")
        job.update(state="running", started_at=job.get("started_at") or now(), resumed=resumed, done=0, total=0)

        def put(**kw: Any) -> None:
            job.update(kw)
            self._update(job)
            self._book_status(job)

        run = _Run(folder, c, job, self.brain, self.queue, self.room, self.timing, put, lambda: job["id"] in self.stop_flag, self.shelf)
        put()
        try:
            got = run.run()
        except Stopped:
            self.stop_flag.discard(job["id"])
            self._finish(job, "stopped", "Stopped. Press it again and it goes on from where it was.")
            return
        except NeedsReading:
            self._finish(job, "failed", f"{c['name']} goes by the story bible, and this book has not been read yet.")
            return
        except (BrainDown, Refused) as exc:
            self._finish(job, "failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never out of the thread; a plain line in the status
            traceback.print_exc()
            self._finish(job, "failed", f"This check stopped on an error ({type(exc).__name__}). Press it again and it goes on from where it was.")
            return
        ran = read_json(engine.home_of(folder) / engine.RUNS, {}) or {}
        ran[c["id"]] = {"finished_at": now(), "plan": job.get("plan") or "", **got}
        write_json(engine.home_of(folder) / engine.RUNS, ran)
        line = "Your editor's letter is written." if c["id"] == "letter" else f"{c['name']} is done. " + (f"Found {got['found']} worth your eye." if got["found"] else "Nothing to report.") if not c["makes"] and not any(b.startswith("write ") for b in c["blocks"]) else f"{c['name']} is done. See Documents."
        if not got.get("found") and not c["makes"] and c["id"] != "letter" and not any(b.startswith("write ") for b in c["blocks"]):
            line = f"Nothing big found on {c['name'].lower()}." + (f" Here is what I checked: {got['checked']}." if got.get("checked") else "")
        if got.get("skipped"):
            steps = "; ".join(got.get("skipped_steps") or [])
            line += f" Not finished: your bot ran out of time on {steps}, even with less text. Press it again to try that part; nothing you have is lost."
        self._finish(job, "done", line, problems=got.get("problems") or [], found=got.get("found", 0))

    def _finish(self, job: dict, state: str, line: str, **more: Any) -> None:
        job.update(state=state, step=line, finished_at=now(), left_s=None, **more)
        self._book_status(job)
        with self.lock:
            self._save_jobs([j for j in self._jobs() if j.get("id") != job.get("id")])

    def status_of(self, folder: Path) -> dict | None:
        """What is happening for this book now, or its last word."""
        jobs = [j for j in self._jobs() if j.get("folder") == folder.name]
        now_ = next((j for j in jobs if j.get("state") in ("running", "waiting")), None) or (jobs[0] if jobs else None)
        if now_:
            return {**now_, "left": _when_left(now_.get("left_s")), "in_line": [j["check_name"] for j in jobs if j is not now_]}
        last = read_json(engine.home_of(folder) / "status.json")
        return last if isinstance(last, dict) else None

    # what the page shows

    def desk_view(self, title: str) -> dict:
        folder = self._folder(title)
        status = self.status_of(folder)              # first: "done" must never come with findings read before it was
        b = Book(folder)
        checks = engine.load_checks()
        items = engine.load_findings(folder)
        ran = read_json(engine.home_of(folder) / engine.RUNS, {}) or {}
        shas = {s.n: s.sha for s in b.sections}
        out = []
        items = [f for f in items if not engine.PRIVATE.search(engine.json_text(f))]   # never a finding about a vault handle
        for f in items:
            c = checks.get(f.get("check")) or {}
            changed = any(shas.get(int(n)) != v for n, v in (f.get("sha") or {}).items() if str(n).isdigit())
            out.append({**f, "check_name": c.get("name") or "", "stage": c.get("stage") or "any", "changed": changed,
                        "can_use": bool(USE_THIS and f.get("correct") and c.get("use_this")), "who": staff.of_check(c)})
        out.sort(key=lambda f: (engine.SIZE_RANK.get(f.get("size"), 1), int(f.get("rank") or 0)))
        job_rows = {j.get("check"): j for j in self._jobs() if j.get("folder") == folder.name}
        rows = {}
        for cid, c in checks.items():
            r = ran.get(cid) or {}
            mine = [f for f in items if f.get("check") == cid]
            rows[cid] = {k: c[k] for k in ("id", "name", "says", "trust", "thin", "box", "box_needed", "button", "stage", "needs_bible")}
            rows[cid].update(last_run=r.get("finished_at"), found=r.get("found"), skipped=r.get("skipped_steps") or [],
                             open=sum(1 for f in mine if f.get("state") == "open" and f.get("kind") != "question"),
                             fixed=sum(1 for f in mine if f.get("state") == "fixed"), running=(job_rows.get(cid) or {}).get("state"),
                             can_run=not c["needs_bible"] or bool(b.bible))
        stages = []
        for s in engine.STAGES:
            mine = [rows[c] for c in rows if rows[c]["stage"] == s["id"]]
            big = sum(1 for f in items if f.get("state") == "open" and f.get("size") == "big" and f.get("kind") != "question"
                      and (checks.get(f.get("check")) or {}).get("stage") == s["id"])
            stages.append({**s, "checks": mine, "big_open": big, "ran": sum(1 for x in mine if x["last_run"]), "total": len(mine)})
        docs = engine.load_documents(folder)
        reading = read_json(folder / books.STATUS, {}) or {}
        return {"title": b.title, "version": VERSION, "has_bible": bool(b.bible), "built_with": b.bible.get("built_with"),
                "sections": [{"n": s.n, "heading": s.heading, "words": s.words} for s in b.sections], "unread": b.unread(),
                "words": sum(s.words for s in b.sections), "reading": reading.get("state") == "reading", "status": status,
                "letter": read_json(engine.home_of(folder) / engine.LETTER), "plan": engine.revision_plan(items, checks), "open_now": sum(1 for f in items if f.get("state") == "open" and f.get("kind") != "question"), "private": b.private,
                "stages": stages, "anytime": [rows[c] for c in rows if rows[c]["stage"] == "any" and c != "letter"],
                "old_notes": self._old_notes(folder), "next": self._next(stages), "findings": out, "documents": docs, "use_this": USE_THIS, "context": self.context(),
                "staff": [staff.public(sid) for sid in staff.IDS]}

    @staticmethod
    def _old_notes(folder: Path) -> str:
        """Her notes from the old Editor's desk tab, shown as they are so nothing is lost. Manuscript keeps the file."""
        for name in ("editing.md", "desk.md"):
            try:
                text = (folder / name).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            lines = text.splitlines()
            return "\n".join(lines[1:]).strip() if lines and lines[0].startswith("# ") else text
        return ""

    @staticmethod
    def _next(stages: list[dict]) -> dict | None:
        for s in stages:
            todo = next((c for c in s["checks"] if not c["last_run"] and not c["running"]), None)
            if todo:
                return {"check": todo["id"], "name": todo["name"], "stage": s["name"], "why": s["why"], "box": todo["box"], "box_needed": todo["box_needed"], "can_run": todo["can_run"],
                        "button": todo["button"], "says": todo["says"]}
            if s["big_open"]:
                n = s["big_open"]
                return {"check": None, "stage": s["name"], "why": f"{n} big thing{'s are' if n > 1 else ' is'} still open in {s['name']}. Settle {'them' if n > 1 else 'it'} before going on. Smaller work now may have to be done again."}
        return None

    def book_view(self, title: str) -> dict:
        """The whole book in one answer, for the Write view. Reading it changes nothing."""
        folder = self._folder(title)
        b = Book(folder)
        unread = set(b.unread())
        root = self._vroot(folder)
        return {"title": b.title, "version": VERSION, "sections": [
            {"n": s.n, "heading": s.heading, "text": _page_text(s.raw), "sha": s.sha, "words": s.words,
             "unread": s.n in unread, "versions": len(versions(root, s.n))} for s in b.sections]}

    def section_view(self, title: str, n: str) -> dict:
        folder = self._folder(title)
        b = Book(folder)
        s = b.section(n)
        if s is None:
            raise Refused("I could not find that section.")
        return {"n": s.n, "heading": s.heading, "text": _page_text(s.raw), "sha": s.sha, "words": s.words, "unread": s.n in b.unread(),
                "versions": versions(self._vroot(folder), s.n), "numbers": [x.n for x in b.sections]}

    def version_view(self, title: str, n: str, vid: str) -> dict:
        folder = self._folder(title)
        if not str(n).isdigit():
            raise Refused("I could not find that section.")
        return {"id": vid, "text": version_text(self._vroot(folder), int(n), vid)}

    @staticmethod
    def _vroot(folder: Path) -> Path:
        return engine.home_of(folder) / engine.VERSIONS

    # the writer's hand: the only ways text changes

    def save(self, msg: dict) -> dict:
        folder = self._folder(msg.get("title"))
        with self.lock:
            got = save_section(folder, self._vroot(folder), msg.get("n"), msg.get("text"), msg.get("sha"), auto=msg.get("auto") is True)
        return {**got, "message": "Saved." if got["changed"] else "No change to save.", "versions": versions(self._vroot(folder), int(msg.get("n")))}

    def restore(self, msg: dict) -> dict:
        folder = self._folder(msg.get("title"))
        if not str(msg.get("n")).isdigit():
            raise Refused("I could not find that section.")
        text = version_text(self._vroot(folder), int(msg.get("n")), msg.get("id"))
        with self.lock:
            got = save_section(folder, self._vroot(folder), msg.get("n"), text, msg.get("sha"), by="restored")
        return {**got, "text": text, "message": "That version is back. The one you had is kept too.", "versions": versions(self._vroot(folder), int(msg.get("n")))}

    def use_this(self, msg: dict) -> dict:
        """PROPOSED, and easy to remove. Her click, after the page showed before and after; undone like any save."""
        if not USE_THIS:
            raise Refused("Use this is turned off.")
        folder = self._folder(msg.get("title"))
        checks = engine.load_checks()
        with self.lock:
            items = engine.load_findings(folder)
            f = next((x for x in items if x.get("id") == msg.get("id")), None)
            if f is None or not f.get("correct") or not (checks.get(f.get("check")) or {}).get("use_this"):
                raise Refused("That finding has no correction to use.")
            b = Book(folder)
            w = f["where"][0]
            s = b.section(w.get("section"))
            span = locate(s.raw, w.get("quote") or "") if s else None
            if not span:
                raise Refused("Those words are not in the section any more, so nothing was changed.")
            a, z = span
            if marks.strip(s.raw[a:z]) != s.raw[a:z] or s.raw[a - 1:a] == "*" or s.raw[z:z + 1] == "*":
                raise Refused("Those words have italics or bold in them, so nothing was changed. Make the change yourself in Write.")
            text = s.raw[:a] + f["correct"] + s.raw[z:]
            got = save_section(folder, self._vroot(folder), s.n, text, s.sha, by="Use this")
            f.update(state="fixed", changed_at=now(), note=(f.get("note") or "") or "Used the correction.")
            engine.save_findings(folder, items)
        return {**got, "message": "Done, and saved. You can bring back the old version under Earlier versions.", "n": s.n}

    def mark(self, title: Any, ref: Any, state: Any = None, note: Any = None) -> tuple[str, dict]:
        folder = self._folder(title)
        engine.carry_old_desk(folder)
        with self.lock:
            items = engine.load_findings(folder)
            f = self._pick(items, ref)
            words = {"fixed": "Fixed", "not_a_problem": "Not a problem", "later": "Later", "open": "Open"}
            st = None if state in (None, "") else str(state).strip().lower().replace(" ", "_")
            if st is not None and st not in engine.STATES:
                raise Refused("A finding can be Open, Fixed, Not a problem or Later.")
            if st:
                f["state"] = st
                f.pop("still_broken", None)
            if note is not None:
                f["note"] = " ".join(str(note).split())[:5000]
            f["changed_at"] = now()
            engine.save_findings(folder, items)
        return f"Marked {words.get(f['state'])}: {f.get('what')}", f

    @staticmethod
    def _pick(items: list[dict], ref: Any) -> dict:
        r = str(ref if ref is not None else "").strip()
        if r.isdigit():
            opens = sorted([f for f in items if f.get("state") == "open"], key=lambda f: (engine.SIZE_RANK.get(f.get("size"), 1), int(f.get("rank") or 0)))
            if not 1 <= int(r) <= len(opens):
                raise Refused(f"There are {len(opens)} open findings.")
            return opens[int(r) - 1]
        got = next((f for f in items if f.get("id") == r), None)
        if got is None:
            raise Refused("I could not find that finding. Ask for the findings again.")
        return got

    def document_view(self, title: str, did: str) -> dict:
        folder = self._folder(title)
        rec, path = self._doc(folder, did)
        out = {**rec, "where": f"My files/{engine.EXPORTS}/{folder.name}/{rec['file']}"}
        if rec.get("ext") != "docx":
            out["text"] = path.read_text(encoding="utf-8")
        return out

    @staticmethod
    def _doc(folder: Path, did: Any) -> tuple[dict, Path]:
        rec = next((d for d in engine.load_documents(folder) if d.get("id") == did), None)
        if rec is None:
            raise Refused("I could not find that document.")
        home = engine.exports_of(folder)
        path = (home / str(rec["file"])).resolve()
        if path.parent != home.resolve() or not path.is_file():
            raise Refused("I could not find that document. It may have been moved in your files.")
        return rec, path

    def document_save(self, msg: dict) -> dict:
        folder = self._folder(msg.get("title"))
        rec, path = self._doc(folder, msg.get("id"))
        if rec.get("ext") == "docx" or not isinstance(msg.get("text"), str):
            raise Refused("That document can't be changed here.")
        write_atomic(path, msg["text"])
        return {"message": "Saved."}

    # tools. None of them writes section text.

    def desk_checks(self, title: str = "") -> str:
        v = self.desk_view(title)
        lines = [f"The stages of editing for {v['title']}:"]
        for s in v["stages"]:
            lines.append(f"{s['name']} ({s['plain']}): {s['why']}")
            for c in s["checks"]:
                lines.append(self._check_line(c))
        lines.append("Any time:")
        lines += [self._check_line(c) for c in v["anytime"]]
        if v["next"] and v["next"].get("check"):
            lines.append(f"Next: {v['next']['name']} ({v['next']['check']}).")
        return "\n".join(lines)

    @staticmethod
    def _check_line(c: dict) -> str:
        last = f"last run {c['last_run'][:10]}, {c['open']} open" if c["last_run"] else "not run yet"
        return f"- {c['name']} [{c['id']}]: {c['says']} ({last})"

    def _calls(self, c: dict, b: Book) -> int:
        """About how many brain calls a check makes on this book. Kept by the book's text and the room, so the office and
        a pass plan, which ask for many at once, stay quick."""
        room = self.room()
        key = (c["id"], room, tuple(s.sha for s in b.sections), sha_of(json.dumps(b.bible, sort_keys=True).encode("utf-8")))
        if key not in self.costs:
            units = sum(len(engine.units_for(n, b, room - 600, "a change")) for n in c["blocks"] if n in ("thread", "character", "section", "theme", "claim", "change", "book", "speakers", "opening", "ending"))
            cands = sum(1 for name in c["find"] for x in engine.finders.run(name, b) if x.get("judge"))
            if len(self.costs) > 500:
                self.costs.clear()
            self.costs[key] = int((units + cands) * 1.4) + (1 if c["letter"] else 0)
        return self.costs[key]

    def _estimate(self, c: dict, b: Book) -> str:
        calls = self._calls(c, b)
        if not calls:
            return "It uses no brain calls, so it takes moments."
        left = self.timing.left(calls)
        return f"That is about {calls} calls to your bot's brain, " + (f"{_when_left(left).replace(' left', '')} at the speed I have measured." if left is not None else "and I have not timed your bot yet.")

    def desk_run(self, title: str = "", check: str = "", confirm: Any = False, plan: str = "") -> str:
        cs = engine.load_checks()
        c = cs.get(str(check or "").strip().lower())
        if c is None:
            raise Refused("There is no check by that name. The checks are: " + ", ".join(cs) + ".")
        folder = self._folder(title)
        b = Book(folder)
        if confirm is not True:
            return f"This has not started. Ask the writer: \"Shall I run {c['name']} on {b.title}?\" {self._estimate(c, b)} Run it only after she says yes, with confirm true."
        job, _c = self.start(b.title, c["id"], plan)
        return f"{c['name']} is in line for {job['title']}. It runs in the background; {self._estimate(c, b)}"

    def desk_status(self, title: str = "") -> str:
        folder = self._folder(title)
        st = self.status_of(folder)
        if not st:
            return "Your editor has not run anything on this book yet."
        bits = [st.get("step") or ""]
        if st.get("total"):
            bits.append(f"step {st.get('done')} of {st.get('total')}")
        if st.get("left"):
            bits.append(st["left"])
        if st.get("in_line"):
            bits.append("then " + ", ".join(st["in_line"]))
        return f"{st.get('check_name') or ''}: " + ", ".join(b for b in bits if b)

    def desk_findings(self, title: str = "", state: str = "open", check: str = "") -> str:
        folder = self._folder(title)
        engine.carry_old_desk(folder)
        want = str(state or "open").strip().lower().replace(" ", "_")
        if want not in engine.STATES:
            raise Refused("Say open, fixed, not a problem or later.")
        cs = engine.load_checks()
        every = [f for f in self.desk_view(folder.name)["findings"] if not check or f.get("check") == check]
        items = [f for f in every if f.get("state") == want and f.get("kind") != "question"]
        asks = [f for f in every if f.get("state") == "open" and f.get("kind") == "question"]
        tail = f"\nAlso {len(asks)} question{'s' if len(asks) != 1 else ''} for the writer to look at, not counted as problems." if asks and want == "open" else ""
        if not items:
            return f"No {want.replace('_', ' ')} findings." + tail
        lines = [f"{len(items)} {want.replace('_', ' ')}, biggest first."]
        for i, f in enumerate(items, 1):
            spot = "; ".join(f"section {w['section']}: \"{w['quote']}\"" for w in f.get("where") or [] if w.get("quote"))
            lines.append(f"{i}. ({f.get('size')}, {(cs.get(f.get('check')) or {}).get('name', '')}) {f.get('what')} Where: {spot or 'see its sections'}. Why it matters: {f.get('why')} One way to fix it: {f.get('fix')} [{f.get('id')}]")
        return "\n".join(lines) + tail

    def desk_mark(self, title: str = "", id: Any = "", state: str = "", note: Any = None) -> str:  # noqa: A002
        return self.mark(title, id, state, note)[0]

    def desk_letter(self, title: str = "") -> str:
        v = self.desk_view(title)
        lt = v["letter"]
        plan = "\n".join(f"{i}. {p['what']} ({p['stage']})" for i, p in enumerate(v["plan"], 1))
        if not lt:
            return f"Your editor has not written a letter yet: it comes with the first check. Next: {(v['next'] or {}).get('name', '')}."
        working = "\n".join(f"- {x}" for x in lt.get("working") or [])
        byid = {f["id"]: f for f in v["findings"]}
        shown = [p for p in lt.get("problems") or [] if p.get("id") in byid and byid[p["id"]].get("kind") != "question"]   # never one since dropped
        probs = "\n".join(f"{i}. {p['line']}" + (" (You marked this Fixed.)" if byid[p["id"]].get("state") == "fixed" else "") for i, p in enumerate(shown, 1))
        return f"{lt.get('book')}\n\nWhat is working:\n{working}\n\nThe biggest problems:\n{probs}\n\nWhat to do first: {lt.get('first')}\n\nThe revision plan as it stands:\n{plan}"

    def desk_documents(self, title: str = "", name: str = "") -> str:
        folder = self._folder(title)
        docs = engine.load_documents(folder)
        if not docs:
            return "No documents yet. They come from Style sheet, Length and genre fit, Synopsis and query letter, and Submission format."
        if name:
            low = str(name).lower()
            d = next((x for x in docs if low in str(x.get("name")).lower() or low in str(x.get("kind"))), None)
            if d is None:
                raise Refused("I could not find that document.")
            got = self.document_view(folder.name, d["id"])
            return got.get("text") or f"{d['name']} is a Word file: {got['where']}."
        return "\n".join(f"{d.get('name')} ({str(d.get('made_at'))[:10]})" for d in docs)

    ASK_SYSTEM = ("You help a novelist think about a change to her own book, from her editor's list of what the change touches and "
                  "the story bible. Answer in plain words, naming sections like (section 3). When the facts don't say, say so. "
                  "Never write new story text.")

    @staticmethod
    def _asked(who: Any) -> str:
        """The staff id an ask goes to, by id or name; her editor-in-chief when none is given."""
        w = " ".join(str(who or "").split()).lower() or staff.CHIEF
        sid = next((s["id"] for s in staff.STAFF if w in (s["id"], s["name"].lower())), w)
        if sid not in staff.ASKABLE:
            raise Refused("You can ask " + ", ".join(staff.name(s) for s in staff.ASKABLE) + ".")
        return sid

    def desk_ask(self, title: str = "", question: str = "", who: str = "") -> str:
        q = " ".join(str(question or "").split())[:2000]
        if not q:
            raise Refused("Say what you want to know about the plan.")
        system = self.ASK_SYSTEM + " " + staff.BY_ID[self._asked(who)]["lens"]
        folder = self._folder(title)
        b = Book(folder)
        items = [f for f in engine.load_findings(folder) if f.get("check") == "future"]
        room = self.room() - tokens(system) - tokens(q) - 100
        facts = [f"Change she asked about: {f.get('plan')}. Revisit (section {(f.get('where') or [{}])[0].get('section')}): {f.get('what')}" for f in items]
        user = f"Question: {q}\n\nWhat the plan touches:\n" + (engine.fit(facts, room // 2) or "(no plan traced yet)")
        user += "\n\nStory bible:\n" + engine.fit(engine.bible_lines(b, ("summary", "threads", "characters")), room // 2)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        job = self.queue.submit(lambda: self.brain.chat(messages, patient=False))    # ahead of a check's next call
        try:
            text, _m = job.wait(ASK_WAIT)
        except (NotYet, BrainBusy):
            self.queue.cancel(job)
            return "Your editor is in the middle of a long ask. Try again in a few minutes."
        except BrainDown as exc:
            raise Refused(str(exc)) from None
        except BrainSlow:
            raise Refused("Your bot's brain took too long on that. Try a smaller question.") from None
        return strip_think(text) or "Your bot had no answer to that."

    def desk_stop(self, title: str = "") -> str:
        return self.stop(title)

    def desk_study(self, title: str = "", theirs: str = "", confirm: Any = False, focus: str = "") -> str:
        got = self.study_books(title)
        if not str(theirs or "").strip():
            if not got["books"]:
                return (f"There is no other book on the shelf to compare {got['title']} with. She can add a book she admires on "
                        "the Manuscript page with Add a book to study; it is not read into a story bible.")
            names = "; ".join(f"{b['title']} ({b['words']:,} words{', a book to study' if b['study'] else ''})" for b in got["books"])
            return f"Books she could compare {got['title']} with: {names}. Ask her which one."
        n = self.study_numbers(title, theirs)
        if not n["pairs_count"]:
            return (f"There is not enough story in one of the books to set passages side by side. The numbers are ready at once: "
                    f"call desk_study_report with theirs \"{n['theirs']}\".")
        cost = f"{n['pairs_count']} pairs of passages" + (f", {n['time_left']}" if n["time_left"] else ", and I have not timed your bot yet")
        if confirm is not True:
            return (f"This has not started. Ask the writer: \"Shall your editor compare passages of {n['mine']} with {n['theirs']}?\" "
                    f"That is {cost}. The numbers are ready now without it (desk_study_report). Start it only after she says yes, "
                    "with confirm true.")
        job = self.study_start(title, theirs, focus)
        return (f"Your editor is comparing passages of {job['title']} with {n['theirs']} in the background: {cost}. "
                "When it is done, desk_study_report has the pointers, and the Study a book page shows them beside the passages.")

    def desk_study_report(self, title: str = "", theirs: str = "") -> str:
        if not str(theirs or "").strip():
            have = self.study_report(title)["reports"]
            if len(have) != 1:
                return self.desk_study(title)
            theirs = have[0]["theirs"]
        n = self.study_numbers(title, theirs)
        lines = [f"{n['mine']} beside {n['theirs']}, measured by code. Different is not wrong.", "Where the two books differ most:"]
        lines += [f"- {x['line']}" for x in n["apart"]] or ["- Nothing stands far apart."]
        lines.append("Where she already matches:")
        lines += [f"- {x['line']}" for x in n["alike"]] or ["- Nothing measured is close."]
        rep_ = self.study_report(title, theirs)
        r = rep_["report"]
        if rep_["running"]:
            lines.append("The passage comparison is running now (desk_status says how far).")
        if not r and rep_["running"]:
            return "\n".join(lines)
        if not r:
            lines.append("Her editor has not compared passages of these two books yet. Offer it; start it with desk_study only "
                         "after her yes.")
            return "\n".join(lines)
        lines.append(f"Her editor compared passages on {str(r.get('made_at') or '')[:10]}. Her focus: {r.get('focus')}.")
        if r.get("pointers"):
            lines.append("What to try first:")
            lines += [f"{i}. {p.get('try')} Why: {p.get('why')}" for i, p in enumerate(r["pointers"], 1)]
        for p in r.get("pairs") or []:
            if p.get("state") != "done":
                continue
            lines.append(f"{p.get('label')} (her section {p['mine'].get('section')}):")
            lines.append(f"  Theirs: {p.get('theirs_does')}")
            lines.append(f"  Hers: {p.get('mine_does')}")
            for x in p.get("pointers") or []:
                quotes = "; ".join(q for q in (f"theirs: \"{x.get('their_quote')}\"" if x.get("their_quote") else "",
                                               f"hers: \"{x.get('my_quote')}\"" if x.get("my_quote") else "") if q)
                lines.append(f"  - {x.get('try')} Why: {x.get('why')}" + (f" ({quotes})" if quotes else ""))
        for s_ in r.get("skipped") or []:
            lines.append(f"Left out: {s_.get('line')}")
        lines.append("Talk through one pointer at a time. Never rewrite her sentences.")
        return "\n".join(lines)

    # Ask your editor: her question about words she selected in Write. Answers describe; they never write her book.

    PAGE_ASK_SYSTEM = ("You are her editor, reading a passage of her novel with her. Point at her own words and quote them. "
                       "Say what works and what does not, and why. Suggest what she might try. Never write replacement sentences "
                       "or passages, and never write her story: every word of the book is hers. Answer in plain words, in a few "
                       "short paragraphs.")

    def _asks_file(self, folder: Path) -> Path:
        return engine.home_of(folder) / ASKS

    def _asks(self, folder: Path) -> list[dict]:
        got = read_json(self._asks_file(folder), [])
        return [a for a in got if isinstance(a, dict) and a.get("id")] if isinstance(got, list) else []

    def _ask_put(self, folder: Path, rec: dict | None, drop: str = "") -> None:
        with self.asks_lock:
            items = [a for a in self._asks(folder) if a.get("id") not in (drop, (rec or {}).get("id"))]
            if rec is not None:
                items.append(dict(rec))
            write_json(self._asks_file(folder), items[-ASKS_KEPT:])

    def _ahead(self) -> str:
        """The check whose call runs before her ask, if one is running now."""
        run_ = next((j for j in self._jobs() if j.get("state") == "running"), None)
        return str(run_.get("check_name") or "a check") if run_ else ""

    def asks_view(self, title: str) -> dict:
        folder = self._folder(title)
        out = []
        for a in self._asks(folder):
            if a.get("state") == "waiting" and a.get("id") not in self.asking:      # the app stopped before it was answered
                a = {**a, "state": "failed", "answer": "This was cut off when your editor stopped. Ask again."}
            out.append(a)
        return {"asks": out, "ahead": self._ahead()}

    def ask_remove(self, msg: dict) -> dict:
        folder = self._folder(msg.get("title"))
        self._ask_put(folder, None, drop=str(msg.get("id") or "") or "-")
        return {"message": "Removed."}

    @staticmethod
    def _around(s: Any, passage: str, room: int) -> str:
        """The paragraphs around the passage in its section, nearest first, as far as the room allows."""
        spans = [(m.start(), m.end()) for m in books.PARAGRAPH.finditer(s.text) if m.group().strip()]
        if not spans:
            return ""
        at = locate(s.text, passage) or locate(s.text, " ".join(passage.split()[:12]))
        lo = hi = 0
        if at:
            lo = next((i for i, (a, z) in enumerate(spans) if z > at[0]), 0)
            hi = next((i for i, (a, z) in enumerate(spans) if z >= at[1]), lo)
        take = list(range(lo, hi + 1))
        used = sum(tokens(s.text[spans[i][0]:spans[i][1]]) for i in take)
        sides = {-1: lo - 1, 1: hi + 1}                 # each side grows outward until a paragraph does not fit
        while sides:
            for d in list(sides):
                i = sides[d]
                cost = tokens(s.text[spans[i][0]:spans[i][1]]) if 0 <= i < len(spans) else None
                if cost is None or used + cost > room:
                    del sides[d]
                    continue
                take.append(i)
                used += cost
                sides[d] = i + d
        return "\n\n".join(s.text[spans[i][0]:spans[i][1]].strip() for i in sorted(take))

    @staticmethod
    def _ask_facts(b: Book, passage: str) -> list[str]:
        """A few story bible facts about who and where the passage names. Cheap: names only."""
        out = []
        for c in b.list_of("characters"):
            names = [str(x) for x in [c.get("name")] + list(c.get("aliases") or []) if x]
            if any(re.search(rf"\b{re.escape(n)}\b", passage) for n in names):
                bits = [f"{c.get('name')} ({c.get('role') or 'a character'})"]
                if c.get("wants"):
                    bits.append(f"wants {c.get('wants')}")
                if c.get("fears"):
                    bits.append(f"fears {c.get('fears')}")
                if c.get("traits"):
                    bits.append("is " + ", ".join(map(str, c.get("traits") or [])))
                out.append("; ".join(bits))
        for p in b.list_of("places"):
            if p.get("name") and re.search(rf"\b{re.escape(str(p['name']))}\b", passage):
                out.append(f"{p['name']}: {p.get('kind') or 'a place'}" + (f", {p['notes']}" if p.get("notes") else ""))
        return [books.scrub(x) for x in out[:8]]

    def ask(self, msg: dict) -> dict:
        folder = self._folder(msg.get("title"))
        b = Book(folder)
        s = b.section(msg.get("n"))
        if s is None:
            raise Refused("I could not find that part of your book.")
        passage = "\n".join(" ".join(x.split()) for x in marks.strip(str(msg.get("passage") or "")).split("\n")).strip()[:3000]
        if not passage:
            raise Refused("Select some of your words first.")
        q = " ".join(str(msg.get("question") or "").split())[:1000] or "How does this read?"
        who = self._asked(msg.get("who"))
        system = self.PAGE_ASK_SYSTEM + " " + staff.BY_ID[who]["lens"]
        room = self.room() - tokens(system) - tokens(q) - tokens(passage) - 200
        facts = engine.fit(self._ask_facts(b, passage), max(0, min(room // 5, 400)))
        near = self._around(s, passage, max(0, room - tokens(facts)))
        user = f"Her question: {q}\n\nThe words she chose, from section {s.n}:\n{passage}\n\nAround them in her book:\n{near or passage}"
        if facts:
            user += f"\n\nFrom the story bible:\n{facts}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": books.scrub(user)}]
        rec = {"id": f"a{int(time.time() * 1000)}", "n": s.n, "quote": passage, "question": q, "answer": "", "state": "waiting", "asked_at": now(),
               "who": who}
        self.asking.add(rec["id"])
        self._ask_put(folder, rec)

        def go() -> dict:
            try:
                text, _m = self.brain.chat(messages, patient=False)
                words = "\n".join(re.sub("\\s*[\u2014\u2013]\\s*", ", ", x).rstrip() for x in strip_think(text).split("\n")).strip()
                rec.update(state="done", answer=words or "Your editor had no answer to that.")
            except BrainSlow:
                rec.update(state="failed", answer="Your editor took too long on that. Try fewer words.")
            except BrainDown as exc:
                rec.update(state="failed", answer=str(exc))
            finally:
                self.asking.discard(rec["id"])
                try:
                    self._ask_put(folder, rec)
                except OSError:
                    pass
            return rec
        ahead = self._ahead()
        job = self.queue.submit(go)                   # her ask goes ahead of a check's next call
        try:
            job.wait(ASK_PAGE_WAIT)
        except NotYet:
            pass
        return {"ask": dict(rec), "ahead": ahead}

    # Study a book: her manuscript beside a book she admires (study.py). The numbers are code and come at once; the passage
    # comparison is her press, a job in the same line as the checks. Kept in this book's working folder, in study/: its
    # measure, and for each book it was compared with, one report and the saved answers, each replaced by the next run.

    def _study_home(self, folder: Path) -> Path:
        return engine.home_of(folder) / STUDY

    @staticmethod
    def _pair_name(theirs: Path) -> str:
        return sha_of(engine.book_key(theirs).encode("utf-8"))[:16]

    def _measure(self, folder: Path, b: Book) -> dict:
        """One book's style numbers, kept by the shas of its sections so a second look costs nothing."""
        key = sha_of("|".join(f"{s.n}:{s.sha}" for s in b.sections).encode("utf-8"))
        path = self._study_home(folder) / "measure.json"
        got = read_json(path, {}) or {}
        if isinstance(got, dict) and got.get("key") == key and isinstance(got.get("numbers"), dict):
            return got["numbers"]
        numbers = study.measure(b)
        try:
            write_json(path, {"key": key, "numbers": numbers})
        except OSError:
            pass
        return numbers

    def _other(self, folder: Path, theirs: Any) -> Path:
        if not " ".join(str(theirs or "").split()):
            raise Refused("Say which book to compare yours with.")
        other = find_book(self.shelf(), theirs)
        if other.name == folder.name:
            raise Refused("Pick a different book to compare yours with.")
        return other

    def study_books(self, title: Any) -> dict:
        folder = self._folder(title)
        out = []
        for f in books.all_books(self.shelf()):
            if f.name == folder.name:
                continue
            idx = read_index(f)
            out.append({"title": idx.get("title") or f.name, "study": idx.get("study") is True,
                        "words": sum(int(c.get("words") or 0) for c in idx.get("sections") or [])})
        out.sort(key=lambda x: (not x["study"], str(x["title"]).lower()))
        return {"title": Book(folder).title, "books": out}

    def study_numbers(self, title: Any, theirs: Any) -> dict:
        """Both books' numbers, the lines where they differ most and where they match, and what the passages would cost.
        No brain."""
        folder = self._folder(title)
        other = self._other(folder, theirs)
        mine_b, their_b = Book(folder), Book(other)
        mine_n, their_n = self._measure(folder, mine_b), self._measure(other, their_b)
        diff = study.differences(mine_n, their_n)
        room = self.room()
        key = sha_of(json.dumps([[s.sha for s in mine_b.sections], [s.sha for s in their_b.sections], room]).encode("utf-8"))
        path = self._study_home(folder) / f"{self._pair_name(other)}.steps.json"
        got = read_json(path, {}) or {}
        if isinstance(got, dict) and got.get("key") == key and isinstance(got.get("pairs"), int):
            n = got["pairs"]
        else:
            n = len(study.pairs(mine_b, their_b, room))
            try:
                write_json(path, {"key": key, "pairs": n})
            except OSError:
                pass
        steps = n + 1 if n else 0
        left = self.timing.left(steps)
        return {"mine": mine_b.title, "theirs": their_b.title, "numbers": {"mine": mine_n, "theirs": their_n},
                "apart": diff["apart"], "alike": diff["alike"], "pairs_count": n, "steps": steps,
                "time_left": _when_left(left).replace(" left", "") if left is not None else ""}

    def study_start(self, title: Any, theirs: Any, focus: Any = "") -> dict:
        """Her press, or her yes through desk_study. The only way a comparison starts."""
        folder = self._folder(title)
        other = self._other(folder, theirs)
        mine_t, their_t = Book(folder).title, Book(other).title
        words = plain(str(focus or ""), study.FOCUS_MAX)
        with self.lock:
            jobs = self._jobs()
            if any(j.get("folder") == folder.name and j.get("kind") == STUDY for j in jobs):
                raise Refused(f"Your editor is already comparing {mine_t} with a book. Wait for it, or press Stop.")
            job = {"id": f"{int(time.time() * 1000)}-study", "kind": STUDY, "folder": folder.name, "title": mine_t,
                   "check": STUDY, "check_name": f"{STUDY_NAME}: {their_t}", "theirs": other.name, "theirs_title": their_t,
                   "plan": words, "state": "queued", "step": "In line", "done": 0, "total": 0, "found": 0, "problems": [],
                   "started_at": None, "pressed_at": now()}
            jobs.append(job)
            self._save_jobs(jobs)
        self.wake.set()
        return job

    def _run_study(self, job: dict, shelf: Path) -> None:
        folder, other = shelf / job["folder"], shelf / str(job.get("theirs") or "")
        if not folder.is_dir() or not job.get("theirs") or not other.is_dir():
            self._finish(job, "failed", "One of the two books is no longer on the shelf.")
            return
        resumed = job.get("state") in ("running", "waiting")
        job.update(state="running", started_at=job.get("started_at") or now(), resumed=resumed, done=0, total=0)

        def put(**kw: Any) -> None:
            job.update(kw)
            self._update(job)
            self._book_status(job)

        def stopped() -> bool:
            return job["id"] in self.stop_flag

        # the same way a check asks: it waits while Manuscript reads, goes at a check's rank, and is timed
        caller = _Run(folder, {"id": STUDY, "name": STUDY_NAME, "box_needed": False}, job, self.brain, self.queue, self.room,
                      self.timing, put, stopped, self.shelf)
        home = self._study_home(folder)
        pair = self._pair_name(other)
        saved = _Saved(home / f"{pair}.saved.json")

        def progress(done: int, total: int, line: str) -> None:
            put(step=line, done=done, total=total, left_s=self.timing.left(total - done))

        put()
        try:
            report = study.run(Book(folder), Book(other), caller._call, self.room(), job.get("plan") or "", saved, progress, stopped)
        except Stopped:
            report = {"state": "stopped"}
        except (BrainDown, Refused) as exc:
            self._finish(job, "failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never out of the thread; a plain line in the status
            traceback.print_exc()
            self._finish(job, "failed", f"The comparison stopped on an error ({type(exc).__name__}). Press it again and it goes on from where it was.")
            return
        if report.get("state") == "stopped":
            self.stop_flag.discard(job["id"])
            self._finish(job, "stopped", "Stopped. Press Compare again and it goes on from where it was.")
            return
        with self.study_lock:
            write_json(home / f"{pair}.json", report)          # one report per book compared with, replaced
            saved.keep_used()                                   # only what this run used is kept
        n = sum(1 for p in report.get("pairs") or [] if p.get("state") == "done")
        line = f"Your editor compared {n} pair{'s' if n != 1 else ''} of passages with {report.get('theirs')}. See Study a book."
        self._finish(job, "done", line, problems=[x["line"] for x in report.get("skipped") or [] if x.get("line")])

    def study_report(self, title: Any, theirs: Any = "") -> dict:
        folder = self._folder(title)
        home = self._study_home(folder)
        if not " ".join(str(theirs or "").split()):
            out = []
            for p in sorted(home.glob("*.json")) if home.is_dir() else []:
                if p.name.count(".") != 1 or p.name == "measure.json":
                    continue
                r = read_json(p, {}) or {}
                if isinstance(r, dict) and r.get("theirs"):
                    out.append({"theirs": r.get("theirs"), "made_at": r.get("made_at"), "state": r.get("state")})
            out.sort(key=lambda r: str(r.get("made_at") or ""), reverse=True)
            return {"reports": out}
        other = self._other(folder, theirs)
        got = read_json(home / f"{self._pair_name(other)}.json")
        running = any(j.get("folder") == folder.name and j.get("kind") == STUDY and j.get("theirs") == other.name for j in self._jobs())
        return {"report": got if isinstance(got, dict) else None, "running": running}

    # The storyboard (storyboard.py): her book as panels, notes for pictures and never words for her book. Her press, a job
    # in the same line as the checks. Kept in this book's working folder, in storyboard/: one board, and the saved answers.
    # Painting is Maps' work: her press writes storyboard.json beside the bible, and Maps paints from it (CONTRACT.md).

    def _board_home(self, folder: Path) -> Path:
        return engine.home_of(folder) / BOARD

    @staticmethod
    def _loose(path: Path) -> Any:
        """A JSON file another program may have written, or a broken one: whatever is wrong with it, None."""
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                return None
            return read_json(path)
        except (OSError, ValueError, TypeError, RecursionError, MemoryError):
            return None

    def _board_read(self, folder: Path) -> dict:
        got = self._loose(self._board_home(folder) / "board.json")
        if not isinstance(got, dict) or not isinstance(got.get("panels"), list):
            return {}
        got["panels"] = [p for p in got["panels"] if isinstance(p, dict) and isinstance(p.get("id"), str)
                         and isinstance(p.get("section"), int)]
        return got

    @staticmethod
    def _board_sections(b: Book, sections: Any) -> list[int]:
        """The sections she chose, in book order. Empty or "all" is the whole book."""
        have = [s.n for s in b.sections]
        if sections is None or sections == "all" or sections == "" or sections == []:
            return have
        if isinstance(sections, str):
            sections = [x for x in re.split(r"[\s,]+", sections) if x]
        if not isinstance(sections, list):
            raise Refused("Say which chapters, by their numbers, or all.")
        want = {int(x) for x in sections if str(x).strip().isdigit()}
        out = [n for n in have if n in want]
        if not out:
            raise Refused("I could not find those chapters in this book.")
        return out

    def board_plan(self, title: Any, sections: Any = None) -> dict:
        b = Book(self._folder(title))
        want = self._board_sections(b, sections)
        n = storyboard.steps(b, want)
        return {"sections": want, "steps": n, "time": _when_left(self.timing.left(n)).replace(" left", "")}

    def board_start(self, title: Any, sections: Any = None) -> dict:
        """Her press, or her yes through desk_storyboard. The only way a storyboard is drawn."""
        folder = self._folder(title)
        b = Book(folder)
        want = self._board_sections(b, sections)
        if not storyboard.steps(b, want):
            raise Refused("There is not enough story in those chapters to draw.")
        with self.lock:
            jobs = self._jobs()
            if any(j.get("folder") == folder.name and j.get("kind") == BOARD for j in jobs):
                raise Refused(f"The storyboard for {b.title} is already being drawn. Wait for it, or press Stop.")
            job = {"id": f"{int(time.time() * 1000)}-board", "kind": BOARD, "folder": folder.name, "title": b.title,
                   "check": BOARD, "check_name": BOARD_NAME, "sections": want, "plan": "", "state": "queued", "step": "In line",
                   "done": 0, "total": 0, "found": 0, "problems": [], "started_at": None, "pressed_at": now()}
            jobs.append(job)
            self._save_jobs(jobs)
        self.wake.set()
        return job

    def _run_board(self, job: dict, shelf: Path) -> None:
        folder = shelf / job["folder"]
        if not folder.is_dir():
            self._finish(job, "failed", "That book is no longer on the shelf.")
            return
        resumed = job.get("state") in ("running", "waiting")
        job.update(state="running", started_at=job.get("started_at") or now(), resumed=resumed, done=0, total=0)

        def put(**kw: Any) -> None:
            job.update(kw)
            self._update(job)
            self._book_status(job)

        def stopped() -> bool:
            return job["id"] in self.stop_flag

        caller = _Run(folder, {"id": BOARD, "name": BOARD_NAME, "box_needed": False}, job, self.brain, self.queue, self.room,
                      self.timing, put, stopped, self.shelf)
        home = self._board_home(folder)
        saved = _Saved(home / "answers.json")

        def progress(done: int, total: int, line: str) -> None:
            put(step=line, done=done, total=total, left_s=self.timing.left(total - done))

        put()
        try:
            board = storyboard.run(Book(folder), job.get("sections") or [], caller._call, self.room(), saved, progress, stopped,
                                   self._board_read(folder))
        except Stopped:
            self.stop_flag.discard(job["id"])
            self._finish(job, "stopped", "Stopped. Press Draw again and it goes on from where it was.")
            return
        except (BrainDown, Refused) as exc:
            self._finish(job, "failed", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never out of the thread; a plain line in the status
            traceback.print_exc()
            self._finish(job, "failed", f"The storyboard stopped on an error ({type(exc).__name__}). Press it again and it goes on from where it was.")
            return
        with self.study_lock:
            write_json(home / "board.json", board)
            saved.keep_used()
        mine = {int(n) for n in job.get("sections") or []}
        n = sum(1 for p in board.get("panels") or [] if p.get("section") in mine)
        line = f"The storyboard has {n} new panel{'s' if n != 1 else ''}. See Storyboard." if n else "No panel could be drawn from those chapters."
        self._finish(job, "done", line, found=n, problems=[str(x.get("why") or "") for x in board.get("skipped") or [] if x.get("why")])

    @staticmethod
    def _panel_pictures(folder: Path) -> dict[str, dict]:
        """The newest picture Maps made for each panel, from pictures.json. A missing or bad file counts as none."""
        out: dict[str, dict] = {}
        try:
            got = Desk._loose(folder / "pictures.json")
            rows = got.get("pictures") if isinstance(got, dict) else None
            for r in rows if isinstance(rows, list) else []:
                if not isinstance(r, dict) or not isinstance(r.get("panel"), str) or not isinstance(r.get("id"), str) \
                        or not PICTURE_ID.fullmatch(r["id"]):
                    continue
                pid = str(r["panel"])
                if pid not in out or str(r.get("made_at") or "") >= str(out[pid].get("made_at") or ""):
                    out[pid] = r
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass
        return out

    def board_view(self, title: Any) -> dict:
        folder = self._folder(title)
        b = Book(folder)
        board = self._board_read(folder)
        shas = {str(s.n): s.sha for s in b.sections}
        marks_ = board.get("sections") if isinstance(board.get("sections"), dict) else {}
        changed = sorted(int(n) for n, m in marks_.items() if str(n).isdigit() and n in shas and isinstance(m, dict)
                         and m.get("panels") and m.get("sha") != shas[n])
        rows = self._panel_pictures(folder)
        t = urllib.parse.quote(b.title)
        ids = {str(p.get("id")) for p in board.get("panels") or []}
        pictures = {}
        failed_why = ""
        for pid, r in rows.items():
            if pid in ids:
                state = r.get("state") if r.get("state") in ("making", "ready", "failed") else "making"
                pictures[pid] = {"state": state, "url": f"api/board/picture?title={t}&id={urllib.parse.quote(pid)}"}
                if state == "failed" and isinstance(r.get("why"), str) and r["why"].strip():
                    failed_why = plain(r["why"], 300)
        asked = self._loose(folder / BOARD_ASK)
        asked = asked if isinstance(asked, dict) else {}
        note = ""
        listed = asked.get("panels") if isinstance(asked.get("panels"), list) else []
        waiting = [str(x.get("id")) for x in listed if isinstance(x, dict)]
        fresh = [pid for pid in waiting if pid in ids and not (rows.get(pid) and rows[pid].get("asked_at") == asked.get("asked_at"))]
        if fresh:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(asked.get("asked_at")).replace("Z", "+00:00"))).total_seconds()
            except (ValueError, TypeError):
                age = PAINT_WAIT_S + 1
            if age <= PAINT_WAIT_S:
                for pid in fresh:
                    pictures[pid] = {"state": "making", "url": ""}
            else:
                note = ("The painting has not started. Open Maps and set up the picture maker there, keep Maps open, "
                        f"then press Have {staff.name('art')} paint these again.")
        if failed_why and not note:                  # Maps' own words for why a painting did not come
            note = f"A painting did not come. Maps says: {failed_why} Open Maps to try it again."
        running = any(j.get("folder") == folder.name and j.get("kind") == BOARD for j in self._jobs())
        return {"board": board or None, "changed": changed, "plan": self.board_plan(title), "running": running,
                "pictures": pictures, "can_paint": bool(ids) and not running, "paint_note": note,
                "sections": [{"n": s.n, "heading": s.heading, "words": s.words} for s in b.sections]}

    def board_picture(self, title: Any, pid: Any) -> bytes:
        folder = self._folder(title)
        r = self._panel_pictures(folder).get(str(pid or ""))
        if not r or r.get("state") != "ready":
            raise Refused("That panel has no picture yet.")
        file = folder / "pictures" / f"{r['id']}.png"
        inside = (folder / "pictures").resolve() == folder.resolve() / "pictures" and file.resolve().parent == folder.resolve() / "pictures"
        if not inside or file.is_symlink() or not file.is_file() or file.stat().st_size > 30 * 1024 * 1024:
            raise Refused("That picture is not there any more.")
        return file.read_bytes()

    def board_paint(self, title: Any, panels: Any = "all") -> dict:
        """Her press. Writes the one shared file Maps paints from: panel ids and prompts made from the notes only."""
        folder = self._folder(title)
        board = self._board_read(folder)
        have = [p for p in board.get("panels") or [] if isinstance(p, dict) and p.get("id")]
        if not have:
            raise Refused("Draw the storyboard first. Then its panels can be painted.")
        if panels not in (None, "", "all"):
            want = {str(x) for x in panels} if isinstance(panels, list) else set()
            have = [p for p in have if str(p["id"]) in want]
            if not have:
                raise Refused("I could not find those panels.")
        left = max(0, len(have) - PAINT_MOST)
        have = have[:PAINT_MOST]
        write_json(folder / BOARD_ASK, {"asked_at": now(), "panels": [{"id": str(p["id"]), "prompt": storyboard.prompt_for(p),
                                                                    "shot": p.get("shot") or "medium"} for p in have]})
        line = f"{staff.name('art')} has {len(have)} panel{'s' if len(have) != 1 else ''} to paint. Maps does the painting, so keep Maps open."
        if left:
            line += f" That is the most at one time; {left} more can go next."
        return {"message": line, "asked": len(have), "left": left}

    def desk_storyboard(self, title: str = "", sections: Any = "", confirm: Any = False) -> str:
        plan = self.board_plan(title, sections)
        b = Book(self._folder(title))
        if not plan["steps"]:
            return f"There is not enough story in those chapters of {b.title} to draw."
        n = len(plan["sections"])
        cost = f"{n} chapter{'s' if n != 1 else ''} or parts" + (f", {plan['time']}" if plan["time"] else ", and I have not timed your bot yet")
        kit = staff.name("board")
        if confirm is not True:
            return (f"This has not started. Ask the writer: \"Shall {kit} draw a storyboard of {b.title}?\" That is {cost}. "
                    "Panels are notes for pictures, never words for her book. Start it only after she says yes, with confirm true.")
        self.board_start(title, sections)
        return (f"{kit} is drawing the storyboard of {b.title} in the background: {cost}. When it is done, desk_storyboard_report "
                "has the panels, and the Storyboard page shows them.")

    def desk_storyboard_report(self, title: str = "") -> str:
        folder = self._folder(title)
        v = self.board_view(title)
        b = Book(folder)
        if v["running"] and not v["board"]:
            return f"The storyboard of {b.title} is being drawn now (desk_status says how far)."
        if not v["board"] or not v["board"].get("panels"):
            return (f"There is no storyboard of {b.title} yet. Offer one; start it with desk_storyboard only after her yes. "
                    "Panels are notes for pictures, never words for her book.")
        out = [f"The storyboard of {b.title}: {len(v['board']['panels'])} panels. These are notes for pictures, not words for her book."]
        if v["running"]:
            out.append("More is being drawn now (desk_status says how far).")
        if v["changed"]:
            out.append("Chapters changed since they were drawn: " + ", ".join(str(n) for n in v["changed"]) + ".")
        out += storyboard.lines(v["board"])
        return "\n".join(out)

    # a Word copy of her book, to read or share. It reads her sections and writes a new file in My files; nothing else.

    def word_copy(self, title: Any) -> dict:
        folder = self._folder(title)
        b = Book(folder)
        if not b.sections:
            raise Refused("There is no text in this book yet.")
        fmt = next((cid for cid, c in engine.load_checks().items() if c.get("makes") == "format"), "")
        ran = read_json(engine.home_of(folder) / engine.RUNS, {}) or {}
        author = str(((ran.get(fmt) or {}) if isinstance(ran, dict) else {}).get("plan") or "")    # her name, as she typed it for Submission format
        raw, name = docx_out.build(b, author, plain=True)
        rec = engine.save_document(folder, "reading-copy", name, "docx", raw, "wordcopy")
        where = f"My files/{engine.EXPORTS}/{folder.name}/{rec['file']}"
        return {"message": f"Saved in your files at: {where}", "where": where, "document": rec}

    # The editor-in-chief and her staff (EDITOR_IN_CHIEF.md). A pass is her one Yes for a whole desk: ordinary check jobs
    # in the same line, each with ``who`` and ``pass``. The office is built by code from what is kept, with no brain call.

    @staticmethod
    def _pass_who(who: Any) -> str:
        w = " ".join(str(who or "").split()).lower()
        w = next((s["id"] for s in staff.STAFF if w == s["name"].lower()), w)
        if w not in staff.PASSES + ["all"]:
            raise Refused("Say whose desk goes over the book: " + ", ".join(f"{staff.name(s)} ({s})" for s in staff.PASSES)
                          + ", or all for the full edit.")
        return w

    @staticmethod
    def _pass_checks(who: str, checks: dict) -> list[dict]:
        """One desk's checks in order. ``all`` is the full edit, big to small, then the letter; not the finished book's."""
        if who != "all":
            return staff.checks_of(who, checks)
        out = [c for st in ("story", "facts", "prose", "polish") for c in staff.checks_of(staff.BY_STAGE[st], checks)]
        return out + [c for c in staff.checks_of(staff.CHIEF, checks) if c["id"] == "letter"]

    @staticmethod
    def _pass_name(who: str) -> str:
        return "The whole staff" if who == "all" else staff.name(who)

    def _plan(self, b: Book, checks: dict, jobs: list[dict], who: str) -> dict:
        lined = {j.get("check") for j in jobs if j.get("folder") == b.folder.name}
        go, out, calls = [], [], 0
        for c in self._pass_checks(who, checks):
            if c["box_needed"]:
                why = f"It needs your words in its box first: {c['box']}"
            elif c["needs_bible"] and not b.bible:
                why = f"It goes by the story bible, and {b.title} has not been read yet."
            elif c["id"] in lined:
                why = "It is already in line."
            else:
                calls += self._calls(c, b)
                go.append({"id": c["id"], "name": c["name"], "estimate": self._estimate(c, b)})
                continue
            out.append({"id": c["id"], "name": c["name"], "why": why})
        left = self.timing.left(calls)
        why_not = ""
        if not go:
            if not out:
                why_not = "There is nothing on this desk to do."
            elif all(x["why"] == "It is already in line." for x in out):
                why_not = f"All of it is already in line for {b.title}."
            elif not b.bible and any("story bible" in x["why"] for x in out):
                why_not = f"{b.title} has not been read yet. Read it in Manuscript first."
            else:
                why_not = out[0]["why"]
        return {"who": who, "name": self._pass_name(who), "checks": go, "left_out": out,
                "time": _when_left(left).replace(" left", "") if go and left is not None else "", "can_start": bool(go), "why_not": why_not}

    def pass_plan(self, title: Any, who: Any) -> dict:
        """What a pass would do and about how long it takes. Starts nothing."""
        w = self._pass_who(who)
        return self._plan(Book(self._folder(title)), engine.load_checks(), self._jobs(), w)

    def pass_start(self, title: Any, who: Any) -> dict:
        """Her press, or her yes through desk_assign: the desk's checks go in line, in order, as ordinary checks."""
        folder = self._folder(title)
        plan = self.pass_plan(folder.name, who)
        if not plan["can_start"]:
            raise Refused(plan["why_not"])
        pid = f"p{int(time.time() * 1000)}"
        queued, left = [], list(plan["left_out"])
        for c in plan["checks"]:
            try:
                self.start(folder.name, c["id"], "", who=plan["who"], pass_id=pid)
                queued.append(c["name"])
            except Refused as exc:                   # put in line a moment ago, from another press
                left.append({"id": c["id"], "name": c["name"], "why": str(exc)})
        if not queued:
            raise Refused(f"All of it is already in line for {Book(folder).title}.")
        write_json(engine.home_of(folder) / PASS, {"id": pid, "who": plan["who"], "total": len(queued), "started_at": now()})
        return {"pass": pid, "queued": queued, "left_out": left, "time": plan["time"]}

    def visit(self, title: Any) -> dict:
        """She has seen the meeting: the next one says what is new since now."""
        folder = self._folder(title)
        b = Book(folder)
        rec = {"at": now(), "shas": {str(s.n): s.sha for s in b.sections}, "words": sum(s.words for s in b.sections)}
        write_json(engine.home_of(folder) / VISIT, rec)
        return {"at": rec["at"], "message": "Noted."}

    @staticmethod
    def _board(folder: Path) -> dict:
        """The storyboard and its painted panels, from their files. A missing or bad file counts as none."""
        out = {"panels": 0, "made_at": None, "painted": 0}
        try:
            got = Desk._loose(engine.home_of(folder) / "storyboard" / "board.json")
            if isinstance(got, dict):
                out["panels"] = len(got["panels"]) if isinstance(got.get("panels"), list) else 0
                out["made_at"] = got["made_at"] if isinstance(got.get("made_at"), str) else None
            pics = Desk._loose(folder / "pictures.json")
            rows = pics.get("pictures") if isinstance(pics, dict) else pics
            if isinstance(rows, list):
                out["painted"] = len({str(p["panel"]) for p in rows if isinstance(p, dict) and p.get("panel") and p.get("state") == "ready"})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return out

    def _pass_now(self, folder: Path, jobs: list[dict]) -> dict | None:
        rec = read_json(engine.home_of(folder) / PASS)
        if not isinstance(rec, dict) or not rec.get("id"):
            return None
        left = [str(j.get("check_name") or "") for j in jobs if j.get("pass") == rec["id"]]
        if not left:
            return None
        total = max(int(rec.get("total") or 0), len(left))
        return {"id": rec["id"], "who": rec.get("who"), "total": total, "done": total - len(left), "left": left}

    @staticmethod
    def _doing(job: dict) -> str:
        bits = [str(job.get("check_name") or "")]
        if job.get("total"):
            bits.append(f"step {job.get('done') or 0} of {job['total']}")
        bits.append(_when_left(job.get("left_s")))
        return ", ".join(b for b in bits if b)

    def office_view(self, title: Any) -> dict:
        """Her editor-in-chief's office: the meeting, and the staff with what each is doing and has found."""
        folder = self._folder(title)
        v = self.desk_view(folder.name)
        b = Book(folder)
        checks = engine.load_checks()
        jobs = [j for j in self._jobs() if j.get("folder") == folder.name]
        ran = read_json(engine.home_of(folder) / engine.RUNS, {}) or {}
        ran = ran if isinstance(ran, dict) else {}
        seen = read_json(engine.home_of(folder) / VISIT)
        seen = seen if isinstance(seen, dict) and isinstance(seen.get("at"), str) else None
        board = self._board(folder)
        found = v["findings"]
        lined = {j.get("check"): j for j in jobs}

        def job_staff(j: dict) -> str:
            return KIND_STAFF.get(str(j.get("kind") or "")) or staff.of_check(checks.get(j.get("check")) or {})

        def last_run(cid: str) -> str:
            got = ran.get(cid)
            return str(got.get("finished_at") or "") if isinstance(got, dict) else ""

        plans = {sid: self._plan(b, checks, jobs, sid) for sid in staff.PASSES}
        rows = []
        for sid in staff.IDS:
            mine = staff.checks_of(sid, checks)
            fs = [f for f in found if f.get("who") == sid]
            opened = [f for f in fs if f.get("state") == "open" and f.get("kind") != "question"]
            js = [j for j in jobs if job_staff(j) == sid]
            last = max((last_run(c["id"]) for c in mine), default="") or (board["made_at"] if sid == "board" else None) or None
            run_ = next((j for j in js if j.get("state") == "running"), None)
            wait_ = next((j for j in js if j.get("state") == "waiting"), None)
            if run_:
                state, doing = "working", self._doing(run_)
            elif wait_:
                state, doing = "waiting", str(wait_.get("step") or "")
            elif js:
                state, doing = "in_line", "In line: " + ", ".join(str(j.get("check_name") or "") for j in js)
            else:
                worked = last or any(f.get("found_by") != "old desk" for f in fs) or (sid == "art" and board["painted"])
                state = "idle" if worked else "new"
                doing = ""
            p = plans.get(sid)
            rows.append({**staff.public(sid), "state": state, "doing": doing,
                         "checks": [{"id": c["id"], "name": c["name"], "last_run": last_run(c["id"]) or None,
                                     "open": sum(1 for f in opened if f.get("check") == c["id"]),
                                     "running": (lined.get(c["id"]) or {}).get("state")} for c in mine],
                         "open": len(opened), "big": sum(1 for f in opened if f.get("size") == "big"),
                         "questions": sum(1 for f in fs if f.get("state") == "open" and f.get("kind") == "question"),
                         "fixed": sum(1 for f in fs if f.get("state") == "fixed"), "last_worked": last,
                         "top": [{"id": f.get("id"), "what": f.get("what"), "size": f.get("size"),
                                  "section": (f.get("where") or [{}])[0].get("section"), "check_name": f.get("check_name")} for f in opened[:3]],
                         "pass": {"can_start": p["can_start"], "time": p["time"], "label": f"Ask {p['name']} to go over the book",
                                  "why_not": p["why_not"]} if p else None})
        by = {r["id"]: r for r in rows}
        lines = self._meeting(b, v, checks, ran, seen, jobs, by, plans, job_staff)
        chief = staff.BY_ID[staff.CHIEF]
        return {"title": b.title, "version": VERSION, "chief": {k: chief[k] for k in ("id", "name", "role", "img")},
                "meeting": {"first": seen is None, "since": seen["at"] if seen else None, "lines": lines,
                            "say": "\n\n".join(x["text"] for x in lines)},
                "staff": rows, "status": v["status"], "pass": self._pass_now(folder, jobs), "board": board,
                "has_bible": v["has_bible"], "unread": v["unread"]}

    def _meeting(self, b: Book, v: dict, checks: dict, ran: dict, seen: dict | None, jobs: list[dict], by: dict,
                 plans: dict, job_staff: Any) -> list[dict]:
        """The meeting's lines, in the design's order, each only when true, never more than eight. Code only."""
        chief = staff.BY_ID[staff.CHIEF]

        def line(who: str, kind: str, text: str, go: dict | None = None, do: dict | None = None) -> dict:
            return {"who": who, "kind": kind, "text": text, **({"go": go} if go else {}), **({"do": do} if do else {})}

        if seen is None:
            hello = [line(staff.CHIEF, "hello", f"Hello, I am {chief['name']}, your {chief['role'].lower()}. My staff is here "
                          f"to work on {b.title} with you, from the big shape of the story down to the last comma.")]
        else:
            day = _day(seen["at"])
            hello = [line(staff.CHIEF, "hello", "Welcome back." + (f" You were last here on {day}." if day else ""))]
        news = []
        if seen is not None:
            old = seen.get("shas") if isinstance(seen.get("shas"), dict) else {}
            changed = [s.n for s in b.sections if old.get(str(s.n)) != s.sha]
            if changed:
                net = sum(s.words for s in b.sections) - int(seen.get("words") or 0)
                size = (f"{net:,} more word{'s' if net != 1 else ''}" if net > 0 else
                        f"{-net:,} fewer word{'s' if net != -1 else ''}" if net < 0 else "the same number of words")
                news.append(line(staff.CHIEF, "news", f"Since then you changed {_sections(changed)}: {size}.",
                                 go={"view": "write", "section": changed[0]}))
            done: dict[str, list[dict]] = {}
            for cid, c in checks.items():
                got = ran.get(cid)
                if isinstance(got, dict) and str(got.get("finished_at") or "") > seen["at"]:
                    done.setdefault(staff.of_check(c), []).append(c)
            for sid in staff.IDS:
                if sid not in done:
                    continue
                ids = {c["id"] for c in done[sid]}
                opened = [f for f in v["findings"] if f.get("check") in ids and f.get("state") == "open" and f.get("kind") != "question"]
                big = sum(1 for f in opened if f.get("size") == "big")
                small = len(opened) - big
                said = ", ".join(x for x in (f"{big} big thing{'s' if big != 1 else ''}" if big else "",
                                             f"{small} smaller" + (" thing" + ("s" if small != 1 else "") if not big else "") if small else "") if x)
                news.append(line(sid, "news", f"{staff.name(sid)} finished {_and([c['name'] for c in done[sid]])}: {said or 'nothing to report'}.",
                                 go={"view": "desk", "who": sid}))
        working = []
        if jobs:
            run_ = next((j for j in jobs if j.get("state") in ("running", "waiting")), None)
            waiting = [j for j in jobs if j is not run_]
            bits = []
            if run_:
                name_ = staff.name(job_staff(run_))
                bits.append(f"{name_} is waiting to start {run_.get('check_name')} while Manuscript reads your book." if run_.get("state") == "waiting"
                            else f"{name_} is working on {self._doing(run_)}.")
            groups: list[list] = []
            for j in waiting:
                sid = job_staff(j)
                if groups and groups[-1][0] == sid:
                    groups[-1][1].append(j)
                else:
                    groups.append([sid, [j]])
            if groups:
                bits.append("In line: " + "; ".join(f"{staff.name(sid)} with " + (str(js[0].get("check_name")) if len(js) == 1 else f"{len(js)} checks")
                                                     for sid, js in groups) + ".")
            first = job_staff(run_ or waiting[0])
            working.append(line(first, "working", " ".join(bits), go={"view": "desk", "who": first}))
        big_lines = []
        for sid in staff.IDS:
            top = next((f for f in v["findings"] if f.get("who") == sid and f.get("state") == "open" and f.get("kind") != "question"
                        and f.get("size") == "big"), None)
            if top and len(big_lines) < 3:
                what = str(top.get("what") or "").strip()
                big_lines.append(line(sid, "found", f"{staff.name(sid)} found something big: {what}" + ("" if what.endswith((".", "?", "!")) else "."),
                                      go={"view": "desk", "finding": top.get("id"), "who": sid}))
        unread = list(v["unread"])
        wait = [line(staff.CHIEF, "waiting", f"{_sections(unread)[0].upper()}{_sections(unread)[1:]} changed since we last read "
                     f"{'it' if len(unread) == 1 else 'them'}. Read again in Manuscript and our notes on {'it' if len(unread) == 1 else 'them'} will be current.",
                     go={"view": "write", "section": unread[0]})] if unread else []
        nxt = []
        story = plans[staff.BY_STAGE["story"]]
        lead = staff.BY_ID[story["who"]]
        if not any(isinstance(r, dict) and r.get("finished_at") for r in ran.values()):
            if story["can_start"]:
                nxt.append(line(lead["id"], "next", f"Next: {lead['name']}, your {lead['role'].lower()}, goes over the whole story first. "
                                "There is no use polishing a scene that may go." + (f" That takes {story['time']}." if story["time"] else ""),
                                do={"pass": lead["id"]}))
            elif not b.bible:
                nxt.append(line(staff.CHIEF, "next", f"Next: read {b.title} in Manuscript first. Then {lead['name']} can start on the story."))
        elif v["next"]:
            nx = v["next"]
            if nx.get("check"):
                sid = staff.of_check(checks.get(nx["check"]) or {})
                if nx.get("can_run"):
                    nxt.append(line(sid, "next", f"Next: {staff.name(sid)} on {nx['name']}. {nx['why']}", do={"check": nx["check"]}))
                else:
                    nxt.append(line(sid, "next", f"Next: {staff.name(sid)} on {nx['name']}, once {b.title} is read in Manuscript. {nx['why']}"))
            else:
                st = next((s["id"] for s in engine.STAGES if s["name"] == nx.get("stage")), "any")
                sid = staff.BY_STAGE.get(st, staff.CHIEF)
                nxt.append(line(sid, "next", f"Next: settle what {staff.name(sid)} found. {nx['why']}", go={"view": "desk", "who": sid}))
        fixed = len(hello) + len(working) + len(big_lines) + len(wait) + len(nxt)
        news = news[:max(0, MEETING_MOST - fixed)]
        return hello + news + working + big_lines + wait + nxt

    # tools for the office

    def desk_meeting(self, title: str = "") -> str:
        o = self.office_view(title)
        out = [f"The meeting in the office for {o['title']}, by code. You are {o['chief']['name']}: tell it in your own voice, short."]
        named = {staff.CHIEF}
        for x in o["meeting"]["lines"]:
            text = x["text"]
            if x["who"] not in named:
                named.add(x["who"])
                s = staff.BY_ID[x["who"]]
                if s["role"].lower() not in text.lower():
                    text += f" ({s['name']} is the {s['role'].lower()}.)"
            do = x.get("do") or {}
            if do.get("pass"):
                text += f" Offer it; start it only on her yes, with desk_assign who {do['pass']} and confirm true."
            elif do.get("check"):
                text += f" Offer it; start it only on her yes, with desk_run check {do['check']} and confirm true."
            out.append("- " + text)
        return "\n".join(out)

    def desk_staff(self, title: str = "") -> str:
        o = self.office_view(title)
        lines = [f"The staff working on {o['title']}:"]
        for s in o["staff"]:
            now_ = {"working": f"working on {s['doing']}", "waiting": s["doing"] or "waiting while Manuscript reads",
                    "in_line": s["doing"].replace("In line", "in line", 1), "idle": "free", "new": "has not worked on this book yet"}[s["state"]]
            if s["id"] == "board":
                has = f"The storyboard has {o['board']['panels']} panels." if o["board"]["panels"] else "No storyboard yet."
            elif s["id"] == "art":
                has = f"Painted {o['board']['painted']} panels." if o["board"]["painted"] else "Nothing painted yet."
            else:
                has = (f"Open: {s['open']}, {s['big']} of them big." if s["open"] else "Nothing open.") + (f" Questions for her: {s['questions']}." if s["questions"] else "")
            lines.append(f"- {s['name']}, {s['role'].lower()} [{s['id']}]: {s['does']} Now: {now_}. {has}")
        return "\n".join(lines)

    def desk_assign(self, title: str = "", who: str = "", confirm: Any = False) -> str:
        p = self.pass_plan(title, who)
        book_ = Book(self._folder(title)).title
        whom = "the whole staff" if p["who"] == "all" else p["name"]
        if not p["can_start"]:
            return f"This cannot start now. {p['why_not']}"
        cost = f"That takes {p['time']} at the speed I have measured." if p["time"] else "I have not timed your bot yet, so I cannot say how long."
        left = (" Left out: " + "; ".join(f"{x['name']} ({x['why']})" for x in p["left_out"]) + ".") if p["left_out"] else ""
        names = _and([c["name"] for c in p["checks"]])
        if confirm is not True:
            return (f"This has not started. Ask the writer: \"Shall {whom} go over {book_}? That is {names}.\" {cost}{left} "
                    "Start it only after she says yes, with confirm true.")
        got = self.pass_start(title, p["who"])
        return (f"{whom[0].upper()}{whom[1:]} {'are' if p['who'] == 'all' else 'is'} going over {book_} in the background: "
                f"{_and(got['queued'])}. {cost} desk_status says how far; desk_meeting tells her what was found.")


def _page_text(raw: str) -> str:
    """A section's text for the page: as the file has it, marks and all, one line a paragraph, no trailing newline."""
    return raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


_Run = engine.Run


class _Saved(dict):
    """Saved answers for one comparison, written to disk as each one comes, so a stop or a restart pays only for what is
    missing. It notes which it was asked for, so a finished run keeps only those."""

    def __init__(self, path: Path) -> None:
        got = read_json(path, {}) or {}
        super().__init__({k: v for k, v in got.items() if isinstance(v, str)} if isinstance(got, dict) else {})
        self.path, self.used = path, set()

    def __getitem__(self, key: str) -> str:
        self.used.add(key)
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        write_json(self.path, dict(self))

    def keep_used(self) -> None:
        write_json(self.path, {k: v for k, v in self.items() if k in self.used})


# -- MCP ------------------------------------------------------------------------------------------

CHANGES = ("desk_run", "desk_mark", "desk_stop", "desk_study", "desk_assign", "desk_storyboard")
_TITLE = {"type": "string", "description": "The manuscript's title. May be left empty when there is only one."}
STUDY_TOOL = ("Compares the writer's manuscript with a book she admires, on the shelf: your editor reads matched passages of each, "
              "one pair at a time, in the background, and gives pointers on style and flow. Without theirs it lists the books she "
              "could compare with. It starts only after the writer said yes; asked without her yes it only tells your bot what to "
              "ask her. It never writes her text.")
BOARD_TOOL = ("Draws the writer's book as storyboard panels, the way a film or picture-book artist lays a story out: what a "
              "picture would show, where, who, the mood and the shot, each with her own words under it. Panels are notes for "
              "pictures, never words for her book. It works in the background and starts only after the writer said yes; asked "
              "without her yes it only tells your bot what to ask her.")
BOARD_REPORT_TOOL = ("The storyboard of the writer's book as plain lines, panel by panel: what each picture would show, where, "
                     "who, the mood, and her words it rests on.")
STUDY_REPORT_TOOL = ("How the writer's manuscript compares with a book she admires: the style numbers at once, where the two "
                     "differ most and where she already matches, and, once her editor compared passages, what to try first and the "
                     "notes on each pair of passages.")
_ASKED = ", ".join(f"{sid} ({staff.name(sid)})" for sid in staff.ASKABLE)
_DESKS = ", ".join(f"{sid} ({staff.name(sid)})" for sid in staff.PASSES)
MEETING_TOOL = ("The editor-in-chief's meeting for one manuscript, built by code: what changed since the writer was last here, "
                "what each staff member finished and found, what is running, what is waiting, and what comes next. Call it when "
                "she says hello, sits down, or asks what is new.")
STAFF_TOOL = ("The editor-in-chief's staff for one manuscript: each one's name, role and what they do for the book, what they are "
              "doing now, and what they have open.")
ASSIGN_TOOL = ("Sends one desk of the staff over the whole book: every check on that desk, in order, in the background. who all "
               "is the full edit, big to small, then the letter. It starts only after the writer said yes; asked without her yes "
               "it only tells your bot what to ask her, with the plan and the time. It never writes her text.")
TOOLS = {
    "desk_run": ("Runs one editing check in the background. It starts only after the writer said yes; asked without her yes it only tells your bot what to ask her. A check can take a long time.",
                 {"title": _TITLE, "check": {"type": "string", "description": "The check's id from desk_checks, such as plot, continuity or future."},
                  "confirm": {"type": "boolean", "description": "True only when the writer said yes to this check, in her latest message."},
                  "plan": {"type": "string", "description": "What she typed for a check with a box: the change she is thinking of, her genre, or her name for the title page."}}, ["check"]),
    "desk_mark": ("Marks one finding Open, Fixed, Not a problem or Later, and keeps a note of the writer's on it, by its number among the open ones or its id.",
                  {"title": _TITLE, "id": {"type": "string", "description": "The finding's number or id."}, "state": {"type": "string", "enum": ["open", "fixed", "not a problem", "later"]}, "note": {"type": "string"}}, ["id"]),
    "desk_stop": ("Stops the check running for one manuscript, after the call in progress. Pressed again, it goes on from where it was.", {"title": _TITLE}, []),
    "desk_assign": (ASSIGN_TOOL, {"title": _TITLE, "who": {"type": "string", "enum": list(staff.PASSES) + ["all"], "description": f"Whose desk: {_DESKS}, or all for the full edit."},
                                  "confirm": {"type": "boolean", "description": "True only when the writer said yes to this, in her latest message."}}, ["who"]),
    "desk_study": (STUDY_TOOL, {"title": _TITLE, "theirs": {"type": "string", "description": "The title of the book she admires, on the shelf. Leave empty to list the books she could pick."},
                                "confirm": {"type": "boolean", "description": "True only when the writer said yes to the comparison, in her latest message."},
                                "focus": {"type": "string", "description": "What she most wants to learn from that book, in her words. May be left empty."}}, []),
    "desk_storyboard": (BOARD_TOOL, {"title": _TITLE, "sections": {"type": "string", "description": "The chapter numbers as the manuscript list gives them, like 3,4. Leave empty for the whole book."},
                                     "confirm": {"type": "boolean", "description": "True only when the writer said yes to this, in her latest message."}}, []),
    "desk_checks": ("The stages of editing for one manuscript and every check in them, when each last ran, what it found, and which comes next.", {"title": _TITLE}, []),
    "desk_status": ("What the writer's editor is doing now for one manuscript, how far along, and about how long is left.", {"title": _TITLE}, []),
    "desk_findings": ("What the checks found in one manuscript: each with what is wrong, the section and its exact quote, why it matters and one way to fix it, biggest first.",
                      {"title": _TITLE, "state": {"type": "string", "enum": ["open", "fixed", "not a problem", "later"]}, "check": {"type": "string", "description": "Only this check's findings."}}, []),
    "desk_letter": ("The Editor's letter for one manuscript: what the book is, what is working, its biggest problems, what to do first, and the revision plan as it stands now.", {"title": _TITLE}, []),
    "desk_ask": ("Answers a question about a change the writer is planning, from what Future plans and changes found and the story bible.",
                 {"title": _TITLE, "question": {"type": "string"},
                  "who": {"type": "string", "enum": list(staff.ASKABLE), "description": f"Which of her staff answers: {_ASKED}. Her editor-in-chief when left empty."}}, ["question"]),
    "desk_documents": ("The documents made for one manuscript: the style sheet, length note, synopsis, query letter, submission copy. Gives one by name, or the list.", {"title": _TITLE, "name": {"type": "string"}}, []),
    "desk_study_report": (STUDY_REPORT_TOOL, {"title": _TITLE, "theirs": {"type": "string", "description": "The title of the book she compared hers with."}}, []),
    "desk_storyboard_report": (BOARD_REPORT_TOOL, {"title": _TITLE}, []),
    "desk_staff": (STAFF_TOOL, {"title": _TITLE}, []),
    "desk_meeting": (MEETING_TOOL, {"title": _TITLE}, []),
}


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc, "annotations": {"readOnlyHint": name not in CHANGES, "destructiveHint": False},
             "inputSchema": {"type": "object", "properties": props, "required": need, "additionalProperties": False}}
            for name, (desc, props, need) in TOOLS.items()]


def mcp_answer(app: Desk, msg: dict) -> dict | None:
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
            return ok({"content": [{"type": "text", "text": f"I could not reach that file ({type(exc).__name__}). Nothing was changed."}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "No such method."}}


# -- HTTP -----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "editors-desk/" + VERSION
    protocol_version = "HTTP/1.1"
    app: Desk
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

    def _drain(self) -> bytes:
        """This request's body, read once before any reply (a reply over an unread body resets the connection on Windows)."""
        if self._raw is None:
            raw_len = self.headers.get("Content-Length") or "0"
            n = int(raw_len) if raw_len.isascii() and raw_len.isdigit() else 0
            self._too_big = n > MAX_BODY
            if self._too_big:
                self.close_connection = True
                self._raw = b""
            else:
                self._raw = self.rfile.read(n) if n > 0 else b""
        return self._raw

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        if self._token_ok():
            return False
        self._drain()
        self._json(401, {"error": "Editor's Desk needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guard(self, fn) -> None:
        self._sent = False
        self._raw, self._too_big = None, False     # a new request on this connection: its own body
        try:
            fn()
        except Refused as exc:
            if not self._sent:
                self._json(400, {"error": str(exc), **getattr(exc, "extra", {})})
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            if not self._sent:
                try:
                    self._json(500, {"error": f"Editor's Desk stopped on this request ({type(exc).__name__}). Nothing was changed."})
                except Exception:  # noqa: BLE001 - the connection itself is gone
                    pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guard(self._get)

    def _get(self):
        if self._refused():
            return
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        a = self.app
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        if path == "/api/list":
            return self._json(200, a.list_view())
        if path == "/api/desk":
            return self._json(200, a.desk_view(q.get("title", "")))
        if path == "/api/book":
            return self._json(200, a.book_view(q.get("title", "")))
        if path == "/api/asks":
            return self._json(200, a.asks_view(q.get("title", "")))
        if path == "/api/section":
            return self._json(200, a.section_view(q.get("title", ""), q.get("n", "")))
        if path == "/api/version":
            return self._json(200, a.version_view(q.get("title", ""), q.get("n", ""), q.get("id", "")))
        if path == "/api/document":
            return self._json(200, a.document_view(q.get("title", ""), q.get("id", "")))
        if path == "/api/settings":
            return self._json(200, {"settings": a.settings()})
        if path == "/api/study/books":
            return self._json(200, a.study_books(q.get("title", "")))
        if path == "/api/study/numbers":
            return self._json(200, a.study_numbers(q.get("title", ""), q.get("theirs", "")))
        if path == "/api/study":
            return self._json(200, a.study_report(q.get("title", ""), q.get("theirs", "")))
        if path == "/api/office":
            return self._json(200, a.office_view(q.get("title", "")))
        if path == "/api/board":
            return self._json(200, a.board_view(q.get("title", "")))
        if path == "/api/board/plan":
            return self._json(200, a.board_plan(q.get("title", ""), q.get("sections", "")))
        if path == "/api/board/picture":
            return self._send(200, a.board_picture(q.get("title", ""), q.get("id", "")), "image/png")
        if path == "/api/pass":
            return self._json(200, a.pass_plan(q.get("title", ""), q.get("who", "")))
        name = urllib.parse.unquote(path.lstrip("/"))
        file = (WEB / name).resolve()
        if name and file.suffix.lower() in WEB_TYPES and WEB.resolve() in file.parents and file.is_file():
            return self._send(200, file.read_bytes(), WEB_TYPES[file.suffix.lower()])   # the page's own files
        self._json(404, {"error": "Editor's Desk has no page here."})

    def do_POST(self):
        self._guard(self._post)

    def _post(self):
        path = self.path.split("?", 1)[0]
        if self._refused():
            return
        raw = self._drain()
        if self._too_big:
            return self._json(413, {"error": "That is too big for Editor's Desk."})
        try:
            msg = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(msg, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            if path == "/mcp":
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
            return self._json(400, {"error": "Not JSON."})
        a = self.app
        if path == "/mcp":
            answer = mcp_answer(a, msg)
            return self._send(202) if answer is None else self._json(200, answer)
        if path == "/api/open":
            return self._json(200, a.open_book(msg.get("title")))
        if path == "/api/run":
            job, c = a.start(msg.get("title"), msg.get("check"), msg.get("plan"))
            return self._json(200, {"message": f"{c['name']} is in line.", "job": job})
        if path == "/api/stop":
            return self._json(200, {"message": a.stop(msg.get("title"))})
        if path == "/api/mark":
            line, f = a.mark(msg.get("title"), msg.get("id"), msg.get("state"), msg.get("note"))
            return self._json(200, {"message": line, "finding": f})
        if path == "/api/save":
            return self._json(200, a.save(msg))
        if path == "/api/restore":
            return self._json(200, a.restore(msg))
        if path == "/api/ask":
            return self._json(200, a.ask(msg))
        if path == "/api/asks":
            return self._json(200, a.ask_remove(msg))
        if path == "/api/use" and USE_THIS:
            return self._json(200, a.use_this(msg))
        if path == "/api/document":
            return self._json(200, a.document_save(msg))
        if path == "/api/settings":
            return self._json(200, a.settings_save(msg))
        if path == "/api/study":
            job = a.study_start(msg.get("title"), msg.get("theirs"), msg.get("focus"))
            return self._json(200, {"message": f"Your editor is comparing passages with {job['theirs_title']}.", "job": job})
        if path == "/api/wordcopy":
            return self._json(200, a.word_copy(msg.get("title")))
        if path == "/api/visit":
            return self._json(200, a.visit(msg.get("title")))
        if path == "/api/board":
            job = a.board_start(msg.get("title"), msg.get("sections"))
            return self._json(200, {"message": f"{staff.name('board')} is drawing the storyboard.", "job": job})
        if path == "/api/board/paint":
            return self._json(200, a.board_paint(msg.get("title"), msg.get("panels")))
        if path == "/api/pass":
            got = a.pass_start(msg.get("title"), msg.get("who"))
            n = len(got["queued"])
            who = a._pass_who(msg.get("who"))
            return self._json(200, {**got, "message": f"{a._pass_name(who)} {'are' if who == 'all' else 'is'} going over the book: "
                                                      f"{n} check{'s' if n != 1 else ''} in line."})
        self._json(404, {"error": "Editor's Desk has no page here."})

    def do_DELETE(self):
        self._guard(self._no)

    def do_PUT(self):
        self._guard(self._no)

    def _no(self):
        if not self._refused():
            self._drain()
            self._send(405, b"", "text/plain")


def make_server(port: int, files: Any, token: str | None, brain: Brain | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    handler = type("EditorsDeskHandler", (Handler,), {"app": Desk(files, brain), "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Editor's Desk for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    token = os.environ.get("PUFFPAI_TOKEN")
    api = os.environ.get("PUFFPAI_API")
    if not args.port or not api:
        p.error("PuffPAI starts Editor's Desk with PUFFPAI_PORT (or --port) and PUFFPAI_API set.")
    server = make_server(args.port, CoreFiles(api, token), token, Brain(api, token))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
