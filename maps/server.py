"""Maps: draws what Manuscript has already read about a novel, big, in the window's side pane.

One small server, bound to 127.0.0.1:
- ``/``                        the page (web/index.html) and its files
- ``/health``                  "ok"
- ``/api/list``                GET: manuscripts that have a story bible, with their boards
- ``/api/atlas``               GET ``?title=``: the manuscript's front page as data: cast, world pins, family, story
                               cards and cover, each with its picture id or null and its proof: the bible's
                               evidence, with sections, that told the picture program what to paint (atlas.py)
- ``/api/illustrate``          GET ``?title=``: the progress of Illustrate; POST ``{title, minor}``: paints what the
                               manuscript needs, one at a time, skipping what is already painted (``maps_illustrate``)
- ``/api/style``               POST ``{title, style}``: the manuscript's style line (empty: the picture maker's style)
- ``/api/bible``               GET ``?title=``: the story bible
- ``/api/boards``              GET ``?title=`` (saved boards) or ``?title=&id=`` (one board; the standard eight are
                               built from the bible each time); DELETE ``?title=&id=``
- ``/api/draw``                POST ``{title, kind, asked}``: a new board, saved (the same as ``maps_draw``)
- ``/api/boards/<id>/layout``  POST ``{title, positions}``: dragged positions, saved
- ``/api/point``               POST ``{title, section, quote, note}``: Manuscript's page scrolls there
- ``/api/maker``               GET: the picture program's address, the style tail and the last ask's state;
                               POST ``{address, style}``: saved in this package's data folder, then the address asked for
- ``/api/maker/test``          POST: asks for the address again, then reaches the picture program and says what it has
- ``/api/picture``             POST ``{title, kind, who, event, note}``: a new picture, made in the background (the
                               same as ``maps_picture``); ``{title, retry: id}`` makes a failed one again;
                               ``{title, again: id, note}`` paints that one again with the writer's note
- ``/api/pictures``            GET ``?title=``: the pictures; DELETE ``?title=&id=``
- ``/api/picture/<id>.png``    GET ``?title=``: the picture file
- ``/mcp``                     the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN. GET never changes anything.

Where the files are: ``<data folder>\\My files\\Manuscripts\\<title>\\``, found by asking the Core where My files is
(``GET /v1/owner-files``). Maps reads only ``bible.json``, ``views.json``, ``boards.json``, ``corrections.json``,
``pointer.json`` and ``storyboard.json`` there, and writes only ``boards.json``, ``pointer.json``, ``pictures.json``
(the pictures and each character's and place's fixed seed), ``style.json``, ``illustrate.json`` and
``pictures\\<id>.png``. It
never reads or writes section text. No brain calls: every board is built from the bible by fixed rules (draw.py), and
every picture is painted by a picture program (ComfyUI) on another computer on the home network, from a prompt built
from bible facts only (pictures.py). Its own data folder (PUFFPAI_DATA) holds ``maker.json``: that address, the style
and the last ten picture times, for Illustrate's time line.

The storyboard's panels (2.2): Editor's Desk writes ``storyboard.json`` in a book's folder when she presses "Have Wren
paint these". While Maps runs, a small watcher looks at each book's file every few seconds and paints each panel once
for each ``asked_at``, with kind ``panel``, from the prompt Editor's Desk built from the panel's notes. Each row says
``panel`` and ``asked_at``, so a panel is never painted twice for the same press, also after a restart. Without a
picture program set up, the watcher does nothing.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))      # its own module, also when started with python -I
import atlas  # noqa: E402
import draw  # noqa: E402
import pictures  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
PAGE = WEB / "index.html"
NAME = "maps"
VERSION = "2.2.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 1024 * 1024
SHELF = "Manuscripts"
BIBLE = "bible.json"
VIEWS = "views.json"
BOARDS = "boards.json"
CORRECTIONS = "corrections.json"
POINTER = "pointer.json"
PICTURES = "pictures.json"
PICTURE_DIR = "pictures"
MAKER = "maker.json"
STYLE = "style.json"
ILLUSTRATE = "illustrate.json"
STORYBOARD = "storyboard.json"                 # written only by Editor's Desk: panels to paint
PANEL = "panel"                                # the kind of a storyboard panel's picture
PANEL_ID = re.compile(r"s[0-9]+-[0-9]+")       # whole, by fullmatch: "s3-1"
PANELS_MOST = 40                               # panels taken from one storyboard.json
PANEL_PROMPT_MAX = 600
STORYBOARD_MAX = 1024 * 1024                   # a bigger file is not read
SIZE_AS = {PANEL: "scene"}                     # a panel is painted at a scene's landscape size
LOOK_S = 5.0                                   # how often the watcher looks at the storyboards
TIMES_KEPT = 10
ASK_PATH = "/v1/bridge/addresses/ask"
ASK_STATUS = {"allowed": 200, "not_needed": 200, "waiting": 202}
PICTURE_TIMEOUT_S = 600
POLL_S = 1.0
PICTURE_ID = re.compile(r"^p-[0-9]{8}-[0-9]{6}-[a-z]+(-[0-9]+)?$")
STD = "std-"                                   # the standard boards' ids: std-family, std-timeline, ...
ASK_MAX = 400
WEB_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
             ".html": "text/html; charset=utf-8"}


class Refused(Exception):
    """A plain line for the owner: nothing was changed."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path: Path, text: str) -> None:
    """The whole file at once: a temp file beside it, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-maps-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(40):            # on Windows a page reading the old file holds it for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, obj: Any) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(20):                # on Windows a file being replaced can't be opened for a moment
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                return default
            time.sleep(0.05)
        except (OSError, ValueError):
            return default
    return default


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

    def ask(self, address: str) -> tuple[str, str] | None:
        """Ask the Core for a Yes card for one address (D110): ``(state, line)``, or None when no answer could be had.
        It grants nothing; the owner says Yes on the card."""
        if not self.api or not self.token:
            return None
        req = urllib.request.Request(self.api + ASK_PATH, data=json.dumps({"address": address}).encode("utf-8"),
                                     method="POST", headers={"Authorization": f"Bearer {self.token}",
                                                             "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the Core is on this computer
        try:
            with opener.open(req, timeout=10) as r:
                code, raw = r.status, r.read(16 * 1024)
        except urllib.error.HTTPError as exc:
            code, raw = exc.code, exc.read(16 * 1024)
        except (OSError, urllib.error.URLError):
            return None
        try:
            said = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        state, line = (said.get("state"), said.get("line")) if isinstance(said, dict) else (None, None)
        if not isinstance(state, str) or not isinstance(line, str):
            return None
        if code != ASK_STATUS.get(state) and not (state == "refused" and code >= 400):
            return None
        line = " ".join("".join(c if c.isprintable() else " " for c in line.replace(self.token, "[hidden]")).split())
        return (state, line[:300]) if line else None


def ask_words(got: tuple[str, str] | None) -> dict:
    """The ask's state and the line the Picture maker box shows."""
    if got is None:
        return {"state": "unknown", "line": "Your bot could not ask for this address just now. You can allow it on the "
                                            "Bridge page.", "at": now()}
    state, line = got
    words = {"allowed": "Allowed.", "not_needed": "Allowed. " + line, "waiting": "Waiting for your Yes on the Bridge page."}
    return {"state": state, "line": words.get(state, line), "at": now()}


def _s(v: Any, n: int = 400) -> str:
    return " ".join(str(v or "").split())[:n]


def how_long(seconds: float) -> str:
    """"less than a minute", "about 12 minutes", "about 2.5 hours"."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 90:
        return "about a minute" if minutes == 1 else f"about {minutes} minutes"
    return f"about {round(minutes / 60, 1):g} hours"


def time_left(seconds: float) -> str:
    said = how_long(seconds)
    return said[0].upper() + said[1:] + " left."


def panel_id(v: Any) -> str:
    """A storyboard panel's id, like "s3-1", or nothing: the file is another app's, so its words are not trusted."""
    return v if isinstance(v, str) and len(v) <= 20 and PANEL_ID.fullmatch(v) else ""


def panel_prompt(v: Any) -> str:
    """A panel's prompt from storyboard.json: no control characters, one line, at most 600 characters."""
    if not isinstance(v, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in v).split())[:PANEL_PROMPT_MAX].strip()


def when(v: Any) -> datetime | None:
    """An ISO time from another app's file, in UTC, or None. A time without a zone is taken as UTC."""
    if not isinstance(v, str) or not 10 <= len(v) <= 40:
        return None
    try:
        t = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


class Maps:
    def __init__(self, files: Any, data: Path | str | None = None) -> None:
        self.files = files
        self.lock = threading.Lock()
        self.data = Path(data) if data else HERE / "data"
        self.jobs: queue.Queue = queue.Queue()
        self.queued: set[str] = set()                      # picture ids queued or being made now
        self.worker: threading.Thread | None = None
        self.watcher: threading.Thread | None = None

    # where things are

    def _all(self) -> list[tuple[Path, dict]]:
        shelf = self.files.shelf()
        if not shelf.is_dir():
            return []
        out = []
        for folder in sorted(shelf.iterdir(), key=lambda p: p.name.lower()):
            if folder.is_dir():
                bible = read_json(folder / BIBLE)
                if isinstance(bible, dict):
                    out.append((folder, bible))
        return out

    def _find(self, title: Any) -> tuple[Path, dict]:
        want = " ".join(str(title or "").split()).lower()
        books = self._all()
        if not books:
            raise Refused("No manuscript has been read yet. Add one in Manuscript and let your bot read it; then "
                          "Maps can draw it.")
        if not want and len(books) == 1:
            return books[0]
        for folder, bible in books:
            if want in (_s(bible.get("title")).lower(), folder.name.lower()):
                return folder, bible
        near = [(f, b) for f, b in books if want and want in (_s(b.get("title")) or f.name).lower()]
        if len(near) == 1:
            return near[0]
        names = ", ".join(self._title(f, b) for f, b in books)
        raise Refused(f"I could not find that manuscript. These have been read: {names}.")

    @staticmethod
    def _title(folder: Path, bible: dict) -> str:
        return _s(bible.get("title"), 200) or folder.name

    @staticmethod
    def _boards(folder: Path) -> list[dict]:
        got = read_json(folder / BOARDS, {})
        boards = got.get("boards") if isinstance(got, dict) else None
        return [b for b in boards if isinstance(b, dict) and b.get("id")] if isinstance(boards, list) else []

    def _save_boards(self, folder: Path, boards: list[dict]) -> None:
        got = read_json(folder / BOARDS, {})
        keep = got if isinstance(got, dict) else {}            # anything else Manuscript keeps there stays
        write_json(folder / BOARDS, {**keep, "boards": boards})

    @staticmethod
    def _views(folder: Path) -> dict[str, str]:
        got = read_json(folder / VIEWS, {})
        views = got.get("views") if isinstance(got, dict) else None
        return {str(v.get("id")): _s(v.get("tab"), 40) for v in views or [] if isinstance(v, dict) and v.get("id")}

    @staticmethod
    def _corrections(folder: Path) -> int:
        got = read_json(folder / CORRECTIONS)
        if isinstance(got, dict):
            got = got.get("corrections")
        return len(got) if isinstance(got, list) else 0

    @staticmethod
    def _head(b: dict, tabs: dict[str, str]) -> dict:
        out = {k: b.get(k) for k in ("id", "kind", "title", "asked", "made_at")}
        if tabs.get(str(b.get("id"))):
            out["tab"] = tabs[str(b["id"])]                    # a view of the same id in Manuscript
        return out

    def _standard(self, folder: Path, bible: dict, kind: str) -> dict:
        spec = draw.build(bible, kind, "")
        saved = next((b for b in self._boards(folder) if b.get("id") == STD + kind), None)
        if saved and isinstance(saved.get("spec"), dict):
            got = {n.get("id"): [n.get("x"), n.get("y")] for n in saved["spec"].get("nodes", []) if isinstance(n, dict)}
            spec = draw.with_positions(spec, got)
        return {"id": STD + kind, "kind": kind, "title": draw.KINDS[kind], "asked": "", "standard": True,
                "made_at": (saved or {}).get("made_at") or bible.get("built_at"), "spec": spec,
                "saved_layout": bool(saved)}

    def _board(self, folder: Path, bible: dict, bid: Any) -> dict:
        bid = _s(bid, 120)
        if bid.startswith(STD) and bid[len(STD):] in draw.KINDS:
            return self._standard(folder, bible, bid[len(STD):])
        got = next((b for b in self._boards(folder) if b.get("id") == bid), None)
        if got is None:
            for b in self._boards(folder):                     # by its title, for the chat
                if _s(b.get("title")).lower() == bid.lower() and not str(b.get("id")).startswith(STD):
                    got = b
                    break
        if got is None:
            try:
                return self._standard(folder, bible, draw.pick_kind(bid, ""))
            except draw.NoKind:
                raise Refused("That board is not there any more.") from None
        spec = got.get("spec")
        if not (isinstance(spec, dict) and spec.get("layout") == draw.LAYOUT):
            # a board written by someone else (Manuscript's own twin of a view): laid out here, from its ask
            try:
                kind = draw.pick_kind(got.get("kind"), got.get("asked"))
            except draw.NoKind:
                raise Refused("Maps does not know how to draw that board yet.") from None
            got = {**got, "kind": kind, "spec": draw.build(bible, kind, _s(got.get("asked"), ASK_MAX))}
        return got

    # what the page and the chat see

    def list_view(self) -> dict:
        out = []
        for folder, bible in self._all():
            tabs = self._views(folder)
            saved = [self._head(b, tabs) for b in self._boards(folder) if not str(b.get("id")).startswith(STD)]
            out.append({"title": self._title(folder, bible), "folder": folder.name,
                        "sections_read": bible.get("sections_read"), "sections_total": bible.get("sections_total"),
                        "built_at": bible.get("built_at"), "built_with": bible.get("built_with"),
                        "corrections": self._corrections(folder), "boards": saved})
        return {"manuscripts": out, "standard": [{"id": STD + k, "kind": k, "title": v} for k, v in draw.KINDS.items()]}

    def bible_view(self, title: str) -> dict:
        return self._find(title)[1]

    def boards_view(self, title: str, bid: str = "") -> dict:
        folder, bible = self._find(title)
        if bid:
            return {"board": self._board(folder, bible, bid)}
        tabs = self._views(folder)
        return {"title": self._title(folder, bible),
                "boards": [{**self._head(b, tabs), "spec": b.get("spec")} for b in self._boards(folder)
                           if not str(b.get("id")).startswith(STD)]}

    def draw_board(self, title: Any, kind: Any, asked: Any) -> dict:
        ask = _s(asked, ASK_MAX)
        try:
            k = draw.pick_kind(kind, ask)
        except draw.NoKind as exc:
            raise Refused(str(exc)) from None
        folder, bible = self._find(title)
        spec = draw.build(bible, k, ask)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        with self.lock:
            boards = self._boards(folder)
            bid, n = f"b-{stamp}-{k}", 2
            while any(b.get("id") == bid for b in boards):
                bid, n = f"b-{stamp}-{k}-{n}", n + 1
            board = {"id": bid, "kind": k, "title": draw.ask_title(bible, k, ask), "asked": ask, "made_at": now(),
                     "spec": spec}
            self._save_boards(folder, [board] + boards)
        return board

    def save_layout(self, title: Any, bid: str, positions: Any) -> dict:
        if not isinstance(positions, dict) or not positions:
            raise Refused("There were no positions to save.")
        folder, bible = self._find(title)
        with self.lock:
            boards = self._boards(folder)
            if bid.startswith(STD) and bid[len(STD):] in draw.KINDS:
                kind = bid[len(STD):]
                spec = draw.with_positions(draw.build(bible, kind, ""), positions)
                rec = {"id": bid, "kind": kind, "title": draw.KINDS[kind], "asked": "", "made_at": now(), "spec": spec}
                boards = [b for b in boards if b.get("id") != bid] + [rec]
            else:
                i = next((i for i, b in enumerate(boards) if b.get("id") == bid), None)
                if i is None:
                    raise Refused("That board is not there any more.")
                rec = self._board(folder, bible, bid)
                rec = {**rec, "spec": draw.with_positions(rec["spec"], positions)}
                boards[i] = rec
            self._save_boards(folder, boards)
        return {"message": "Saved where you put them.", "board": rec}

    def delete_board(self, title: str, bid: str) -> dict:
        folder, _bible = self._find(title)
        with self.lock:
            boards = self._boards(folder)
            keep = [b for b in boards if b.get("id") != bid]
            if len(keep) == len(boards):
                raise Refused("That board is not there any more.")
            self._save_boards(folder, keep)
        return {"message": "Back to the drawn layout." if bid.startswith(STD) else "Removed that board."}

    def point(self, title: Any, section: Any, quote: Any, note: Any) -> dict:
        folder, bible = self._find(title)
        try:
            n = int(section)
        except (TypeError, ValueError):
            raise Refused("Say which section to point at, by its number.") from None
        total = bible.get("sections_total")
        if n < 0 or (isinstance(total, int) and total > 0 and n > total):
            raise Refused(f"{self._title(folder, bible)} has no section {n}.")
        pointer = {"title": self._title(folder, bible), "section": n, "quote": _s(quote, 200),
                   "note": _s(note, 400), "set_at": now()}
        write_json(folder / POINTER, pointer)
        return pointer

    # the picture maker: one address, kept in this package's own data folder

    def maker(self) -> dict:
        got = read_json(self.data / MAKER, {})
        got = got if isinstance(got, dict) else {}
        ask = got.get("ask") if isinstance(got.get("ask"), dict) else None
        avg = got.get("avg_s")
        return {"address": _s(got.get("address"), 200), "style": _s(got.get("style"), pictures.STYLE_MAX)
                or pictures.DEFAULT_STYLE, "ask": ask, "default_style": pictures.DEFAULT_STYLE,
                "avg_s": round(float(avg), 1) if isinstance(avg, (int, float)) and avg > 0 else None}

    def _timed(self, seconds: float) -> None:
        """One more picture time in maker.json: the last ten, and their average."""
        with self.lock:
            cur = read_json(self.data / MAKER, {})
            if not isinstance(cur, dict) or not cur.get("address"):
                return
            times = [t for t in cur.get("times") or [] if isinstance(t, (int, float))][-(TIMES_KEPT - 1):]
            times.append(round(seconds, 1))
            write_json(self.data / MAKER, {**cur, "times": times, "avg_s": round(sum(times) / len(times), 1)})

    # the style line of one manuscript: style.json beside the bible, else the picture maker's style

    def _style(self, folder: Path) -> str:
        got = read_json(folder / STYLE, {})
        own = _s(got.get("style"), pictures.STYLE_MAX) if isinstance(got, dict) else ""
        return own or self.maker()["style"]

    def set_style(self, title: Any, style: Any) -> dict:
        folder, bible = self._find(title)
        line = _s(style, pictures.STYLE_MAX)
        with self.lock:
            if line:
                write_json(folder / STYLE, {"style": line, "set_at": now()})
            else:
                (folder / STYLE).unlink(missing_ok=True)
        got = self._style(folder)
        return {"style": got, "own": bool(line), "message": f"Every new picture for {self._title(folder, bible)} ends "
                                                             f"with: {got}." + ("" if line else " That is the picture "
                                                                                "maker's style.")}

    def _ask(self, address: str) -> dict:
        asker = getattr(self.files, "ask", None)
        words = ask_words(asker(address) if asker else None)
        with self.lock:
            cur = read_json(self.data / MAKER, {})
            if isinstance(cur, dict) and cur.get("address") == address:      # still the address she typed
                write_json(self.data / MAKER, {**cur, "ask": words})
        return words

    def set_maker(self, address: Any, style: Any) -> dict:
        try:
            base = pictures.check_address(address)
        except ValueError as exc:
            raise Refused(f"{exc} Nothing was saved.") from None
        with self.lock:
            cur = self.maker()
            raw = read_json(self.data / MAKER, {})
            same = cur["address"] == base and isinstance(raw, dict)
            doc = {"address": base, "style": _s(style, pictures.STYLE_MAX) or pictures.DEFAULT_STYLE,
                   "ask": cur["ask"] if cur["address"] == base else None, "last_updated": now(),
                   **({k: raw[k] for k in ("times", "avg_s") if k in raw} if same else {})}
            self.data.mkdir(parents=True, exist_ok=True)
            write_json(self.data / MAKER, doc)
        ask = self._ask(base)
        return {**self.maker(), "message": f"Saved {base}. {ask['line']}"}

    def _base(self) -> str:
        base = self.maker()["address"]
        if not base:
            raise Refused("The picture program's address is not set yet. Type it in the Picture maker box in Maps.")
        return base

    def test_maker(self) -> dict:
        base = self._base()
        ask = self._ask(base)
        out = {**self.maker(), "reached": False, "workflow": None, "models": {}, "message": ask["line"]}
        if ask["state"] in ("waiting", "refused"):
            return out
        try:
            got = pictures.Comfy(base, timeout=15).survey()
        except pictures.PictureError as exc:
            return {**out, "message": str(exc)}
        info = got["info"]
        models = {"unet": got["models"]["unet"] or pictures._names_in(info, "UnetLoaderGGUF", "unet_name"),
                  "clip": got["models"]["clip"] or pictures._names_in(info, "DualCLIPLoader", "clip_name1"),
                  "vae": got["models"]["vae"] or pictures._names_in(info, "VAELoader", "vae_name"),
                  "checkpoints": got["models"]["checkpoints"]
                  or pictures._names_in(info, "CheckpointLoaderSimple", "ckpt_name")}
        out = {**out, "reached": True, "models": {k: v[:20] for k, v in models.items()}}
        try:
            choice = pictures.choose(info, got["models"])
        except pictures.NoModel as exc:
            return {**out, "message": f"Reached the picture program. {exc}"}
        return {**out, "workflow": choice["workflow"],
                "message": f"Reached the picture program. Maps would use {pictures.workflow_line(choice)}"}

    # pictures: records in pictures.json beside the bible, files in pictures\<id>.png

    @staticmethod
    def _pictures(folder: Path) -> list[dict]:
        got = read_json(folder / PICTURES, {})
        pics = got.get("pictures") if isinstance(got, dict) else None
        if not isinstance(pics, list):
            return []
        return [p for p in pics if isinstance(p, dict) and PICTURE_ID.match(str(p.get("id") or ""))]

    @staticmethod
    def _seeds(folder: Path) -> dict[str, int]:
        got = read_json(folder / PICTURES, {})
        seeds = got.get("seeds") if isinstance(got, dict) else None
        return {str(k): v for k, v in seeds.items() if isinstance(v, int)} if isinstance(seeds, dict) else {}

    @staticmethod
    def _save_pics(folder: Path, pics: list[dict], seeds: dict[str, int] | None = None) -> None:
        """pictures.json with these records; the seeds kept, or replaced when given. Called with the lock held."""
        got = read_json(folder / PICTURES, {})
        keep = got if isinstance(got, dict) else {}
        write_json(folder / PICTURES, {**keep, "pictures": pics, **({"seeds": seeds} if seeds is not None else {})})

    def _update_picture(self, folder: Path, pid: str, **change: Any) -> dict | None:
        with self.lock:
            pics = self._pictures(folder)
            i = next((i for i, p in enumerate(pics) if p["id"] == pid), None)
            if i is None:
                return None                     # removed while it was being made
            pics[i] = {**pics[i], **change}
            self._save_pics(folder, pics)
            return pics[i]

    def _shown(self, p: dict) -> dict:
        if p.get("state") == "making" and p["id"] not in self.queued:
            return {**p, "state": "failed", "why": "Maps was restarted before this picture was done. Retry makes it again."}
        return p

    @staticmethod
    def _kind(kind: Any) -> str:
        k = _s(kind, 20).lower().replace(" ", "")
        k = {"map": "worldmap", "world": "worldmap", "character": "portrait", "book": "cover", "event": "scene"}.get(k, k)
        if k not in pictures.KINDS:
            raise Refused("Maps makes five kinds of picture: a portrait, a place, a world map, a scene and a cover.")
        return k

    @staticmethod
    def _basis(p: dict) -> str:
        """The hash of the prompt a picture had before any note: what Illustrate compares."""
        return _s(p.get("basis"), 40) or ("" if p.get("note") else atlas.prompt_hash(str(p.get("prompt") or "")))

    @staticmethod
    def _record(folder: Path, pics: list[dict], seeds: dict[str, int], k: str, about: dict, prompt: str, basis: str,
                note: str, base: str, reuse: dict | None = None, again: dict | None = None,
                proof: list | None = None, style: str = "") -> tuple[list[dict], dict]:
        """A picture record, new (first in the list) or *reuse* made again in place; its seed chosen. Each character
        and place keeps one seed, so a repaint keeps the face. A fresh seed when that prompt and seed were already
        painted (it would paint the same picture again) or the note asks for a new face; that becomes the seed."""
        slot = atlas.slot_of(k, about["who"])
        fixed = slot if k in ("portrait", "place") else ""
        if reuse is not None and isinstance(reuse.get("seed"), int):
            seed = reuse["seed"]
        else:
            keep = seeds.get(fixed) if fixed else (again or {}).get("seed")
            painted = any(atlas.slot_of(p.get("kind"), p.get("who")) == slot and p.get("state") == "ready"
                          and p.get("prompt") == prompt and p.get("seed") == keep for p in pics)
            fresh = painted or bool(pictures.NEW_FACE.search(note))
            seed = keep if isinstance(keep, int) and not fresh else random.randint(1, 2 ** 31 - 1)
        if fixed:
            seeds[fixed] = seed
        fresh_rec = {"kind": k, **about, "prompt": prompt, "proof": list(proof or []), "style": style,
                     "note": note, "basis": basis, "seed": seed,
                     "state": "making", "why": "", "address": base, "made_at": now(), "done_at": ""}
        if reuse is not None:
            rec = {**reuse, **fresh_rec}
            (folder / PICTURE_DIR / f"{reuse['id']}.png").unlink(missing_ok=True)
            return [rec if p["id"] == reuse["id"] else p for p in pics], rec
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        pid, n = f"p-{stamp}-{k}", 2
        while any(p["id"] == pid for p in pics):
            pid, n = f"p-{stamp}-{k}-{n}", n + 1
        rec = {"id": pid, **fresh_rec}
        return [rec] + pics, rec

    def _enqueue(self, folder: Path, pids: list[str]) -> None:
        """Called with the lock held, after pictures.json is written."""
        for pid in pids:
            self.queued.add(pid)
            self.jobs.put((folder, pid))
        if pids and (self.worker is None or not self.worker.is_alive()):
            self.worker = threading.Thread(target=self._work, name="maps-pictures", daemon=True)
            self.worker.start()

    def make_picture(self, title: Any, kind: Any, who: Any, retry: Any = None, again: Any = None, note: Any = None,
                     event: Any = None) -> dict:
        folder, bible = self._find(title)
        base = self._base()
        style = self._style(folder)
        rid, aid = _s(retry, 60), _s(again, 60)
        said = _s(note, pictures.NOTE_MAX)
        old = None
        if rid or aid:
            old = next((p for p in self._pictures(folder) if p["id"] == (rid or aid)), None)
            if old is None:
                raise Refused("That picture is not there any more.")
            if rid and rid in self.queued:
                return old
            kind, who = old.get("kind"), old.get("who")
            if rid:
                said = _s(old.get("note"), pictures.NOTE_MAX)          # a retry is the same picture again
            if kind == PANEL:
                return self._panel_again(folder, bible, old, base, style, said, retry=bool(rid))
        k = self._kind(kind)
        if k == "scene" and _s(event, 300) and not old:
            who = event
        try:
            plain, about, proof = pictures.prompt_and_proof(bible, k, who, style)
            if said:
                prompt, _about, proof = pictures.prompt_and_proof(bible, k, who, style, said)
            else:
                prompt = plain
        except pictures.PictureError as exc:
            raise Refused(str(exc)) from None
        with self.lock:
            pics, seeds = self._pictures(folder), self._seeds(folder)
            pics, rec = self._record(folder, pics, seeds, k, about, prompt, atlas.prompt_hash(plain), said, base,
                                     reuse=old if rid else None, again=old if aid else None, proof=proof, style=style)
            self._save_pics(folder, pics, seeds)
            self._enqueue(folder, [rec["id"]])
        return rec

    # the storyboard's panels: storyboard.json from Editor's Desk, each panel painted once for each asked_at

    @staticmethod
    def _panel_prompt(bible: dict, asked: str, style: str, note: str = "") -> str:
        """The panel's description with each character's name put as a plain description, then the note and the
        style, the way every other prompt ends."""
        body = pictures._unnamed(asked, bible).strip(" .")
        return pictures._finish([body], style, pictures._people_tokens(bible), note)

    @staticmethod
    def _panel_about(pid: str, asked_at: str, asked: str) -> dict:
        return {"who": pid, "who_name": f"storyboard {pid}", "section": int(pid[1:].split("-")[0]), "panel": pid,
                "asked_at": asked_at, "panel_prompt": asked}

    def _panel_again(self, folder: Path, bible: dict, old: dict, base: str, style: str, note: str,
                     retry: bool) -> dict:
        """Retry, or Paint again with a note, for a panel's picture: from the description Editor's Desk sent."""
        pid, asked = panel_id(old.get("panel")), panel_prompt(old.get("panel_prompt"))
        if not pid or not asked:
            raise Refused("That panel's description is not there any more. Have Wren paint it again in Editor's Desk.")
        plain = self._panel_prompt(bible, asked, style)
        prompt = self._panel_prompt(bible, asked, style, note) if note else plain
        about = self._panel_about(pid, _s(old.get("asked_at"), 40), asked)
        with self.lock:
            pics, seeds = self._pictures(folder), self._seeds(folder)
            pics, rec = self._record(folder, pics, seeds, PANEL, about, prompt, atlas.prompt_hash(plain), note, base,
                                     reuse=old if retry else None, again=None if retry else old, style=style)
            self._save_pics(folder, pics, seeds)
            self._enqueue(folder, [rec["id"]])
        return rec

    def _queue_panels(self, folder: Path, base: str) -> int:
        """Queue each panel of this book's storyboard.json not painted yet for its asked_at. Returns how many."""
        path = folder / STORYBOARD
        if path.is_symlink() or not path.is_file() or path.stat().st_size > STORYBOARD_MAX:
            return 0
        got = read_json(path)                               # half-written or broken: None, and nothing happens
        if not isinstance(got, dict) or not isinstance(got.get("panels"), list):
            return 0
        asked_raw = got.get("asked_at")
        asked = when(asked_raw)
        if asked is None or (asked - datetime.now(timezone.utc)).total_seconds() > 600:
            return 0                                        # no date, or one from the future: not her press
        bible = read_json(folder / BIBLE, {})
        bible = bible if isinstance(bible, dict) else {}
        style = self._style(folder)
        with self.lock:
            pics, seeds = self._pictures(folder), self._seeds(folder)
            rows = [(panel_id(p.get("panel")), when(p.get("asked_at"))) for p in pics if p.get("kind") == PANEL]
            rows = [(i, t) for i, t in rows if i and t is not None]
            if rows and asked < max(t for _i, t in rows):
                return 0                                    # an older request than what was already painted
            done = set(rows)
            seen = read_json(folder / PICTURES, {})
            seen = seen.get("panels_asked") if isinstance(seen, dict) else None
            seen = seen if isinstance(seen, dict) and seen.get("asked_at") == _s(asked_raw, 40) and isinstance(seen.get("ids"), list) else None
            done |= {(i, asked) for i in (seen or {}).get("ids", []) if isinstance(i, str)}    # asked for this press before, even if she deleted it since
            jobs: list[str] = []
            took: list[str] = []
            for x in got["panels"][:PANELS_MOST]:
                pid = panel_id(x.get("id")) if isinstance(x, dict) else ""
                said = panel_prompt(x.get("prompt")) if pid else ""
                if not said or (pid, asked) in done:
                    continue
                done.add((pid, asked))
                took.append(pid)
                prompt = self._panel_prompt(bible, said, style)
                pics, rec = self._record(folder, pics, seeds, PANEL, self._panel_about(pid, _s(asked_raw, 40), said),
                                         prompt, atlas.prompt_hash(prompt), "", base, style=style)
                jobs.append(rec["id"])
            if jobs:
                self._save_pics(folder, pics, seeds)
                kept = read_json(folder / PICTURES, {})
                if isinstance(kept, dict):                  # which panels this press has had, so a deleted one stays deleted
                    ids = sorted({*((seen or {}).get("ids", [])), *took})[:PANELS_MOST * 4]
                    write_json(folder / PICTURES, {**kept, "panels_asked": {"asked_at": _s(asked_raw, 40), "ids": ids}})
                self._enqueue(folder, jobs)
        return len(jobs)

    def look_for_panels(self) -> int:
        """One look at every book's storyboard.json. Nothing at all without a picture program set up. Returns how many
        pictures were queued."""
        base = self.maker()["address"]
        if not base:
            return 0
        try:
            shelf = self.files.shelf()
        except Refused:
            return 0
        if not shelf.is_dir():
            return 0
        n = 0
        for folder in sorted(shelf.iterdir(), key=lambda p: p.name.lower()):
            if folder.is_dir() and not folder.is_symlink():
                try:
                    n += self._queue_panels(folder, base)
                except Exception:  # noqa: BLE001 - one book's odd file never stops the others
                    continue
        return n

    def watch(self) -> None:
        """Start the storyboard watcher, a daemon thread that looks every few seconds while Maps runs."""
        if self.watcher is not None and self.watcher.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    self.look_for_panels()
                except Exception:  # noqa: BLE001 - the watcher never stops on a bad file or a busy Core
                    pass
                time.sleep(LOOK_S)
        self.watcher = threading.Thread(target=loop, name="maps-storyboard", daemon=True)
        self.watcher.start()

    def _work(self) -> None:
        while True:                             # one worker, one picture at a time
            folder, pid = self.jobs.get()
            try:
                self._step(folder, pid, True)
                self._paint(folder, pid)
            except Exception as exc:  # noqa: BLE001 - the card says what kind of error it was
                self._update_picture(folder, pid, state="failed", done_at=now(),
                                     why=f"Maps stopped on this picture ({type(exc).__name__}).")
            finally:
                try:                            # the progress first: a run with nothing queued reads as stopped
                    self._step(folder, pid, False)
                except Exception:  # noqa: BLE001 - the progress line is not worth stopping the worker for
                    pass
                self.queued.discard(pid)

    def _paint(self, folder: Path, pid: str) -> None:
        rec = next((p for p in self._pictures(folder) if p["id"] == pid), None)
        if rec is None:
            return
        base = _s(rec.get("address"), 200)

        def failed(why: str) -> None:
            self._update_picture(folder, pid, state="failed", why=why, done_at=now())
        ask = self._ask(base)
        if ask["state"] == "waiting":
            return failed("Not allowed yet: waiting for your Yes on the Bridge page. Say Yes there, then Retry.")
        if ask["state"] == "refused":
            return failed(f"Not allowed: {ask['line']}")
        comfy = pictures.Comfy(base)
        started = time.monotonic()
        try:
            got = comfy.survey()
            choice = pictures.choose(got["info"], got["models"])
            seed = rec["seed"] if isinstance(rec.get("seed"), int) else random.randint(1, 2 ** 31 - 1)
            kind = str(rec.get("kind"))
            flow = pictures.build_workflow(choice, str(rec.get("prompt") or ""), SIZE_AS.get(kind, kind), seed)
            self._update_picture(folder, pid, workflow=choice["workflow"], seed=seed)
            prompt_id = comfy.submit(flow, "puffpai-maps-" + pid)
            img = comfy.wait(prompt_id, PICTURE_TIMEOUT_S, POLL_S)
            png = comfy.fetch(img)
        except pictures.PictureError as exc:
            return failed(str(exc))
        with self.lock:
            if not any(p["id"] == pid for p in self._pictures(folder)):
                return                          # removed while it was being made
            (folder / PICTURE_DIR).mkdir(exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(folder / PICTURE_DIR), prefix=".tmp-maps-", suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(png)
            os.replace(tmp, folder / PICTURE_DIR / f"{pid}.png")
        self._update_picture(folder, pid, state="ready", why="", done_at=now())
        self._timed(time.monotonic() - started)

    # Illustrate: every picture a manuscript needs, one at a time, its progress in illustrate.json

    def _step(self, folder: Path, pid: str, starting: bool) -> None:
        with self.lock:
            prog = read_json(folder / ILLUSTRATE)
            if not isinstance(prog, dict) or prog.get("state") != "running":
                return
            item = next((i for i in prog.get("items") or [] if isinstance(i, dict) and i.get("id") == pid), None)
            if item is None:
                return
            if starting:
                item["state"], prog["now"] = "painting", item.get("label") or ""
            else:
                rec = next((p for p in self._pictures(folder) if p["id"] == pid), None)
                item["state"] = "ready" if rec and rec.get("state") == "ready" else "failed"
                if item["state"] == "failed":
                    why = _s((rec or {}).get("why"), 200) or "it was removed while it was being made"
                    prog["problems"] = list(prog.get("problems") or []) + [f"{item.get('label')}: {why}"]
                prog["done"] = sum(1 for i in prog["items"] if i.get("state") in ("ready", "failed"))
                prog["now"] = ""
                if prog["done"] >= prog.get("total", 0):
                    prog["state"], prog["finished_at"] = "done", now()
            write_json(folder / ILLUSTRATE, prog)

    def illustrate(self, title: Any, minor: Any = False) -> dict:
        folder, bible = self._find(title)
        base = self._base()
        style = self._style(folder)
        also_minor = minor is True or _s(minor, 10).lower() in ("true", "yes", "1", "on")
        plan = atlas.illustrate_items(bible, also_minor)
        problems: list[str] = []
        with self.lock:
            pics, seeds = self._pictures(folder), self._seeds(folder)
            items: list[dict] = []
            jobs: list[str] = []
            first_proof: list | None = None
            for k, who, label in plan:
                try:
                    plain, about, proof = pictures.prompt_and_proof(bible, k, who, style)
                except pictures.PictureError as exc:
                    problems.append(f"{label}: {exc}")
                    continue
                basis = atlas.prompt_hash(plain)
                slot = atlas.slot_of(k, about["who"])
                mine = [p for p in pics if atlas.slot_of(p.get("kind"), p.get("who")) == slot]
                ready = next((p for p in mine if p.get("state") == "ready"), None)
                if ready is not None and self._basis(ready) == basis:
                    continue                                    # painted already for this description
                busy = next((p for p in mine if p.get("state") == "making" and p["id"] in self.queued
                             and self._basis(p) == basis), None)
                if busy is not None:
                    items.append({"id": busy["id"], "slot": slot, "label": label, "state": "waiting"})
                    if first_proof is None:
                        first_proof = list(busy.get("proof") or [])
                    continue
                note = _s(mine[0].get("note"), pictures.NOTE_MAX) if mine else ""      # her last note stays
                prompt = plain
                if note:
                    prompt, _about, proof = pictures.prompt_and_proof(bible, k, who, style, note)
                stale = next((p for p in mine if p.get("state") in ("failed", "making") and p["id"] not in self.queued
                              and self._basis(p) == basis), None)
                pics, rec = self._record(folder, pics, seeds, k, about, prompt, basis, note, base, reuse=stale,
                                         proof=proof, style=style)
                items.append({"id": rec["id"], "slot": slot, "label": label, "state": "waiting"})
                if first_proof is None:
                    first_proof = proof
                jobs.append(rec["id"])
            if items:
                self._save_pics(folder, pics, seeds)
                write_json(folder / ILLUSTRATE, {"state": "running", "done": 0, "total": len(items), "now": "",
                                                 "started_at": now(), "finished_at": "", "problems": problems,
                                                 "minor": also_minor, "items": items})
                self._enqueue(folder, jobs)
        return {**self.illustrate_view(self._title(folder, bible)), "painting": len(items), "planned": len(plan),
                "first_proof": first_proof or []}

    def _progress(self, folder: Path) -> dict | None:
        prog = read_json(folder / ILLUSTRATE)
        if not isinstance(prog, dict):
            return None
        items = [i for i in prog.get("items") or [] if isinstance(i, dict)]
        total, done = int(prog.get("total") or len(items)), int(prog.get("done") or 0)
        state = _s(prog.get("state"), 20) or "done"
        if state == "running" and not any(i.get("id") in self.queued for i in items):
            state = "stopped"                                   # Maps restarted before it was done
        avg = self.maker()["avg_s"]
        left = max(total - done, 0)
        eta = round(avg * left) if avg and state == "running" else None
        if state == "running":
            line = f"Painting {min(done + 1, total)} of {total}" + (f": {prog['now']}." if prog.get("now") else ".")
            if eta is not None:
                line += " " + time_left(eta)
        elif state == "stopped":
            line = (f"Stopped at {done} of {total}: Maps was restarted before Illustrate was done. Illustrate again "
                    "paints the rest.")
        else:
            ready = sum(1 for i in items if i.get("state") == "ready")
            line = f"Painted {ready} of {total}."
        problems = [_s(p, 300) for p in prog.get("problems") or []][:40]
        if problems and state != "running":
            line += f" {len(problems)} could not be painted; the cards say why."
        return {"state": state, "done": done, "total": total, "now": _s(prog.get("now"), 120),
                "started_at": prog.get("started_at") or "", "finished_at": prog.get("finished_at") or "",
                "problems": problems, "line": line, "eta_s": eta, "avg_s": avg,
                "items": [{k: i.get(k) for k in ("id", "slot", "label", "state")} for i in items]}

    def illustrate_view(self, title: str) -> dict:
        folder, bible = self._find(title)
        return {"title": self._title(folder, bible), "progress": self._progress(folder)}

    def atlas_view(self, title: str) -> dict:
        folder, bible = self._find(title)
        pics = [self._shown(p) for p in self._pictures(folder)]
        got = atlas.build(bible, pics, self._standard(folder, bible, "world"), draw.portrait_family(bible),
                          self._style(folder), self._progress(folder), bool(self.maker()["address"]))
        return {**got, "title": self._title(folder, bible), "own_style": bool(read_json(folder / STYLE))}

    def pictures_view(self, title: str) -> dict:
        folder, bible = self._find(title)
        return {"title": self._title(folder, bible), "pictures": [self._shown(p) for p in self._pictures(folder)],
                "kinds": pictures.KINDS}

    def picture_file(self, title: str, pid: str) -> bytes:
        if not PICTURE_ID.match(pid):
            raise Refused("That picture is not there any more.")
        books = [self._find(title)] if _s(title) else self._all()
        for folder, _bible in books:
            path = folder / PICTURE_DIR / f"{pid}.png"
            if path.is_file() and any(p["id"] == pid for p in self._pictures(folder)):
                return path.read_bytes()
        raise Refused("That picture is not there any more.")

    def delete_picture(self, title: str, pid: str) -> dict:
        folder, _bible = self._find(title)
        with self.lock:
            pics = self._pictures(folder)
            keep = [p for p in pics if p["id"] != pid]
            if len(keep) == len(pics):
                raise Refused("That picture is not there any more.")
            self._save_pics(folder, keep)
            (folder / PICTURE_DIR / f"{pid}.png").unlink(missing_ok=True)
        return {"message": "Removed that picture."}

    # the chat's tools

    def maps_list(self, title: str = "") -> str:
        books = [self._find(title)] if _s(title) else self._all()
        if not books:
            return "No manuscript has been read yet, so there is nothing to draw."
        lines = []
        for folder, bible in books:
            saved = [b for b in self._boards(folder) if not str(b.get("id")).startswith(STD)]
            lines.append(f"{self._title(folder, bible)}: the eight standard boards ({', '.join(draw.KINDS.values())})"
                         + (f", and {len(saved)} drawn from asks:" if saved else ", and no boards drawn from asks yet."))
            for b in saved:
                lines.append(f"  {b.get('title')} (id {b.get('id')}): asked \"{_s(b.get('asked'), 120)}\"")
            pics = [self._shown(p) for p in self._pictures(folder)]
            ready = sum(1 for p in pics if p.get("state") == "ready")
            prog = self._progress(folder)
            line = (f"  Pictures: {len(pics)}, {ready} of them ready." if pics else "  Pictures: none painted yet.")
            if prog and prog["state"] == "running":
                line += f" An Illustrate is running: {prog['line']}"
            else:
                line += " No Illustrate is running."
            lines.append(line)
        return "\n".join(lines)

    def maps_draw(self, title: str = "", kind: str = "", asked: str = "") -> str:
        board = self.draw_board(title, kind, asked)
        spec = board["spec"]
        count = len(spec["nodes"]) + len(spec["pins"]) + len(spec["rects"])
        if not count:
            return f"{spec.get('note') or 'There was nothing to draw.'} The board is saved in Maps as \"{board['title']}\"."
        folder, bible = self._find(title)
        line = f"Drew \"{board['title']}\" for {self._title(folder, bible)}. It is saved in Maps (id {board['id']})."
        return line + (f" {spec['note']}" if spec.get("note") else "")

    def maps_point(self, title: str = "", board: str = "", item: str = "") -> str:
        folder, bible = self._find(title)
        b = self._board(folder, bible, board or "")
        hit = draw.find_item(b["spec"], item)
        if not hit:
            raise Refused(f"I could not find \"{_s(item, 80)}\" on {b.get('title')}.")
        p = hit["point"]
        self.point(self._title(folder, bible), p["section"], p.get("quote") or hit.get("label"), p.get("note"))
        return f"Manuscript is showing section {p['section']}" + (f", at \"{p['quote']}\"." if p.get("quote") else ".")

    def maps_picture(self, title: str = "", kind: str = "", who: str = "", event: str = "", note: str = "") -> str:
        rec = self.make_picture(title, kind, who, note=note, event=event)
        what = {"portrait": f"a portrait of {rec.get('who_name')}", "place": f"a picture of {rec.get('who_name')}",
                "worldmap": "a painted world map", "cover": "a cover mock",
                "scene": f"a scene: {rec.get('who_name')}"}[rec["kind"]]
        said = pictures.proof_line(rec.get("proof") or [])
        return (f"Making {what} on the picture program. It shows in Maps when it is ready, usually within a few "
                f"minutes (id {rec['id']})." + (f" {said}" if said else ""))

    def maps_illustrate(self, title: str = "", minor: str = "") -> str:
        got = self.illustrate(title, minor)
        n, name = got["painting"], got["title"]
        if not n:
            return (f"Every picture {name} needs is already painted for its current description, so there is nothing "
                    "to paint.")
        avg = self.maker()["avg_s"]
        how = (f", which may take {how_long(avg * n)} at about {round(avg)} seconds each" if avg else
               ", and the picture maker has not been timed yet, so how long it takes is not known")
        said = pictures.proof_line(got.get("first_proof") or [])
        return (f"Illustrating {name}: {n} picture{'s' if n != 1 else ''} to paint, one at a time{how}. "
                "Its progress shows on its page in Maps." + (f" {said}" if said else ""))


KIND_NAMES = list(draw.KINDS)
_TITLE = {"type": "string", "description": "The manuscript's title. May be left empty when there is only one."}
TOOLS = {
    "maps_list": ("The boards Maps can draw for each manuscript her bot has read: the eight standard ones and the "
                  "ones drawn from earlier asks.", {"title": _TITLE}, []),
    "maps_draw": ("A drawing of one manuscript in Maps, made from its story bible and saved with the ask: a family "
                  "tree, a relationship map, a timeline, character touchpoints, arcs, the story structure, a world map "
                  "or a thread board. Asks about supporting characters and whether they interact give a relationship "
                  "map of only the supporting characters.",
                  {"title": _TITLE,
                   "kind": {"type": "string", "enum": KIND_NAMES + [""], "description": "Which board. May be left "
                            "empty when the ask names it."},
                   "asked": {"type": "string", "description": "What she asked to see, in her words."}},
                  ["asked"]),
    "maps_point": ("Points the Manuscript page at the passage behind one item on a board, such as a character, an "
                   "event or a setup; the manuscript pane opens that section and highlights the words.",
                   {"title": _TITLE,
                    "board": {"type": "string", "description": "The board's id or title, or a standard board's name "
                              "such as timeline or family."},
                    "item": {"type": "string", "description": "The item's name or id: a character, a place or an "
                             "event's words."}},
                   ["board", "item"]),
    "maps_picture": ("A picture painted from the story bible by the picture program on the owner's home network, shown "
                     "in Maps: a character portrait, a place, a painted world map, a scene from one event or a cover "
                     "mock. A character or a place keeps its face from one painting to the next. It returns at once; "
                     "the picture is ready a few minutes later. Your bot never paints it.",
                     {"title": _TITLE,
                      "kind": {"type": "string", "enum": list(pictures.KINDS), "description": "Which picture."},
                      "who": {"type": "string", "description": "For a portrait, the character's name; for a place, "
                              "the place's name. Empty for a world map, a scene or a cover."},
                      "event": {"type": "string", "description": "For a scene, the event's words or its id."},
                      "note": {"type": "string", "description": "The writer's own words for this picture, such as "
                               "\"older, grey hair\". They join the description."}},
                     ["kind"]),
    "maps_illustrate": ("Every picture one manuscript needs, painted one at a time on the picture program: a portrait "
                        "of each main and supporting character, each place, the world map, up to twelve key scenes "
                        "and the cover. Pictures already painted for the same description are skipped. It returns at "
                        "once with how many it will paint and about how long that takes.",
                        {"title": _TITLE,
                         "minor": {"type": "boolean", "description": "True to paint the minor characters too."}},
                        []),
}


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc,
             "annotations": {"readOnlyHint": True, "destructiveHint": False,       # only its own files change
                             "openWorldHint": name in ("maps_picture", "maps_illustrate")},
             "inputSchema": {"type": "object", "properties": props, "required": need, "additionalProperties": False}}
            for name, (desc, props, need) in TOOLS.items()]


def mcp_answer(app: Maps, msg: dict) -> dict | None:
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
            text = getattr(app, name)(**{k: str(v) for k, v in args.items() if k in TOOLS[name][1]})
            return ok({"content": [{"type": "text", "text": text}]})
        except Refused as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except OSError as exc:
            return ok({"content": [{"type": "text", "text": f"I could not reach that file ({type(exc).__name__}). "
                                                            "Nothing was changed."}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "No such method."}}


# -- HTTP -----------------------------------------------------------------------------------------

LAYOUT_ROUTE = re.compile(r"^/api/boards/([^/]+)/layout$")
PICTURE_ROUTE = re.compile(r"^/api/picture/([^/]+)\.png$")


class Handler(BaseHTTPRequestHandler):
    server_version = "maps/" + VERSION
    protocol_version = "HTTP/1.1"
    app: Maps
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

    def _drain(self) -> bytes:
        """This request's body, read once before any reply (a reply over an unread body resets the connection on
        Windows). A body over MAX_BODY is dropped, then refused."""
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

    def _body(self) -> dict:
        raw = self._drain()
        if self._too_big:
            raise ValueError("That is too big for Maps.")
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

    def _refused(self) -> bool:
        if self._token_ok():
            return False
        self._drain()
        self._json(401, {"error": "Maps needs its PuffPAI token. Open it from the PuffPAI Desktop."})
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
                    self._json(500, {"error": f"Maps stopped on this request ({type(exc).__name__}). "
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
            return self._json(200, a.list_view())
        if path == "/api/bible":
            return self._json(200, a.bible_view(q.get("title", "")))
        if path == "/api/boards":
            return self._json(200, a.boards_view(q.get("title", ""), q.get("id", "")))
        if path == "/api/maker":
            return self._json(200, a.maker())
        if path == "/api/pictures":
            return self._json(200, a.pictures_view(q.get("title", "")))
        if path == "/api/atlas":
            return self._json(200, a.atlas_view(q.get("title", "")))
        if path == "/api/illustrate":
            return self._json(200, a.illustrate_view(q.get("title", "")))
        m = PICTURE_ROUTE.match(path)
        if m:
            try:
                return self._send(200, a.picture_file(q.get("title", ""), urllib.parse.unquote(m.group(1))), "image/png")
            except Refused as exc:
                return self._json(404, {"error": str(exc)})
        name = urllib.parse.unquote(path.lstrip("/"))
        file = (WEB / name).resolve()
        if name and file.suffix.lower() in WEB_TYPES and WEB.resolve() in file.parents and file.is_file():
            return self._send(200, file.read_bytes(), WEB_TYPES[file.suffix.lower()])   # the page's own files
        self._json(404, {"error": "Maps has no page here."})

    def do_POST(self):
        self._guard(self._post)

    def _post(self):
        path = self.path.split("?", 1)[0]
        if self._refused():
            return
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError) as exc:
            if path == "/mcp":
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
            return self._json(413 if self._too_big else 400, {"error": str(exc) if self._too_big else "Not JSON."})
        a = self.app
        if path == "/mcp":
            answer = mcp_answer(a, msg)
            return self._send(202) if answer is None else self._json(200, answer)
        if path == "/api/draw":
            board = a.draw_board(msg.get("title"), msg.get("kind"), msg.get("asked"))
            return self._json(200, {"message": f"Drawn and saved as \"{board['title']}\".", "board": board})
        if path == "/api/point":
            p = a.point(msg.get("title"), msg.get("section"), msg.get("quote"), msg.get("note"))
            return self._json(200, {"message": f"Manuscript is showing section {p['section']}.", "pointer": p})
        if path == "/api/maker":
            return self._json(200, a.set_maker(msg.get("address"), msg.get("style")))
        if path == "/api/maker/test":
            return self._json(200, a.test_maker())
        if path == "/api/picture":
            rec = a.make_picture(msg.get("title"), msg.get("kind"), msg.get("who"), msg.get("retry"), msg.get("again"),
                                 msg.get("note"), msg.get("event"))
            return self._json(200, {"message": "Making it on the picture program. It shows here when it is ready.",
                                    "picture": rec})
        if path == "/api/illustrate":
            got = a.illustrate(msg.get("title"), msg.get("minor"))
            n = got["painting"]
            return self._json(200, {**got, "message": f"Painting {n} picture{'s' if n != 1 else ''}, one at a time."
                                    if n else "Every picture is already painted for its current description."})
        if path == "/api/style":
            return self._json(200, a.set_style(msg.get("title"), msg.get("style")))
        m = LAYOUT_ROUTE.match(path)
        if m:
            return self._json(200, a.save_layout(msg.get("title"), urllib.parse.unquote(m.group(1)),
                                                 msg.get("positions")))
        self._json(404, {"error": "Maps has no page here."})

    def do_DELETE(self):
        self._guard(self._delete)

    def _delete(self):
        if self._refused():
            return
        self._drain()
        path, q = self._query()
        if path == "/api/boards":
            return self._json(200, self.app.delete_board(q.get("title", ""), q.get("id", "")))
        if path == "/api/pictures":
            return self._json(200, self.app.delete_picture(q.get("title", ""), q.get("id", "")))
        self._send(405, b"", "text/plain")

    def do_PUT(self):
        self._raw, self._too_big = None, False
        if not self._refused():
            self._drain()
            self._send(405, b"", "text/plain")


def make_server(port: int, files: Any, token: str | None, data: Path | str | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown(). *data* is the package's own
    data folder (PUFFPAI_DATA), else ``data`` beside this file."""
    app = Maps(files, data)
    app.watch()                                     # the storyboard's panels, painted while Maps runs
    handler = type("MapsHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Maps for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    token = os.environ.get("PUFFPAI_TOKEN")
    api = os.environ.get("PUFFPAI_API")
    if not args.port or not api:
        p.error("PuffPAI starts Maps with PUFFPAI_PORT (or --port) and PUFFPAI_API set.")
    server = make_server(args.port, CoreFiles(api, token), token, os.environ.get("PUFFPAI_DATA") or None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
