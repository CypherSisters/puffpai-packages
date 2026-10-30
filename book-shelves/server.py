# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/books/library.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/books/librarian.py (rewritten-from)
"""Book shelves: find books in the owner's Calibre library and keep shelves, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

The Calibre library is read from its ``metadata.db``, always opened read-only
(``mode=ro``). Nothing here ever writes to the Calibre folder. The owner's
shelves and the library folder are one JSON document in PUFFPAI_DATA
(``shelves.json``). Writes replace the whole file at once (temp file, then
rename). A file that exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "book-shelves"
VERSION = "1.0.0"
DOC = "shelves.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
TOP = 20
MAX_SHELVES = 100
MAX_ON_SHELF = 5000
#: A shelf with one of these names holds books the owner has read; recommend skips them.
READ_SHELVES = ("read", "finished", "done", "have read", "already read")
NO_LIBRARY = ("No Calibre library is set yet. Tell me the folder that holds it (the one with metadata.db), "
              "like: my Calibre library is in D:\\Books\\Calibre Library.")


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be words")
    value = str(value).strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this tool takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


def _like(text: str) -> str:
    """*text* as a LIKE pattern that matches it anywhere, with its own % and _ taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"library": "", "shelves": {}, "last_updated": ""}


class Store:
    """One JSON document with a stamp: the library folder and the owner's shelves."""

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
                raise Unreadable(f"{self.path} will not read ({exc}). Book shelves will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("shelves", {}), dict):
                raise Unreadable(f"{self.path} is not a Book shelves file. Book shelves will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".shelves-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the Calibre library

class NoLibrary(RuntimeError):
    """No library set, or the one set cannot be read. The message says which, plainly."""


_SELECT = """
SELECT b.id, b.title, b.pubdate, b.timestamp, b.series_index,
       (SELECT GROUP_CONCAT(a.name, '|') FROM books_authors_link bal
          JOIN authors a ON a.id = bal.author WHERE bal.book = b.id) AS authors,
       (SELECT GROUP_CONCAT(t.name, '|') FROM books_tags_link btl
          JOIN tags t ON t.id = btl.tag WHERE btl.book = b.id) AS tags,
       (SELECT s.name FROM books_series_link bsl
          JOIN series s ON s.id = bsl.series WHERE bsl.book = b.id LIMIT 1) AS series,
       (SELECT GROUP_CONCAT(d.format, '|') FROM data d WHERE d.book = b.id) AS formats
FROM books b
"""
_AUTHOR = ("EXISTS (SELECT 1 FROM books_authors_link bal JOIN authors a ON a.id = bal.author "
           "WHERE bal.book = b.id AND a.name LIKE ? ESCAPE '\\')")
_SERIES = ("EXISTS (SELECT 1 FROM books_series_link bsl JOIN series s ON s.id = bsl.series "
           "WHERE bsl.book = b.id AND s.name LIKE ? ESCAPE '\\')")
_TAG = ("EXISTS (SELECT 1 FROM books_tags_link btl JOIN tags t ON t.id = btl.tag "
        "WHERE btl.book = b.id AND t.name LIKE ? ESCAPE '\\')")
_SERIES_ORDER = ("(SELECT s.name FROM books_series_link bsl JOIN series s ON s.id = bsl.series "
                 "WHERE bsl.book = b.id LIMIT 1), b.series_index, b.sort")


def _db_file(folder: str) -> Path:
    path = Path(folder).expanduser()
    return path if path.name.lower() == "metadata.db" else path / "metadata.db"


def _connect(folder: str) -> sqlite3.Connection:
    """The library's metadata.db, read-only (a ``mode=ro`` URI). Never written."""
    if not folder:
        raise NoLibrary(NO_LIBRARY)
    db = _db_file(folder)
    if not db.is_file():
        raise NoLibrary(f"There is no metadata.db in {db.parent}. Point me at the Calibre library folder again.")
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM books LIMIT 1")
    except sqlite3.Error as exc:
        raise NoLibrary(f"The Calibre library in {db.parent} will not read ({exc}).") from None
    return conn


def _year(pubdate) -> str:
    """The year from Calibre's pubdate. Calibre marks 'unknown' with year 101."""
    text = str(pubdate or "")[:4]
    return text if text.isdigit() and int(text) >= 1000 else ""


def _line(row) -> str:
    """One book as one line: Title by Author (Series #2), 1999, EPUB/PDF [#12]."""
    out = row["title"] or "Untitled"
    if row["authors"]:
        out += " by " + ", ".join(row["authors"].split("|"))
    if row["series"]:
        idx = row["series_index"]
        num = f" #{idx:g}" if isinstance(idx, (int, float)) else ""
        out += f" ({row['series']}{num})"
    if _year(row["pubdate"]):
        out += f", {_year(row['pubdate'])}"
    if row["formats"]:
        out += ", " + "/".join(sorted(set(row["formats"].split("|"))))
    return out + f" [#{row['id']}]"


# ------------------------------------------------------------- the logic

class Shelves:
    def __init__(self, store: Store) -> None:
        self.store = store

    # helpers
    def _rows(self, where: list[str], args: list, order: str = "b.sort, b.title", limit: int = TOP + 1):
        conn = _connect(self.store.load()["library"])
        try:
            sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ?"
            return conn.execute(sql, [*args, limit]).fetchall()
        finally:
            conn.close()

    def _book(self, book: str):
        """One book from a title (or a part of one) or its number like #12. Raises ValueError to ask which."""
        book = _text(book, "book", 300)
        if not book:
            raise ValueError("say which book, by its title or its number like #12")
        num = book.lstrip("#")
        if num.isdigit():
            rows = self._rows(["b.id = ?"], [int(num)])
        else:
            rows = self._rows(["b.title = ? COLLATE NOCASE"], [book]) or self._rows(
                ["b.title LIKE ? ESCAPE '\\'"], [_like(book)], limit=6)
        if not rows:
            raise ValueError(f"there is no book like {book!r} in the library")
        if len(rows) > 1:
            raise ValueError("more than one book fits. Say which one by its number:\n"
                             + "\n".join(_line(r) for r in rows[:5]))
        return rows[0]

    @staticmethod
    def _shelf_name(shelves: dict, shelf: str) -> str | None:
        for name in shelves:
            if name.lower() == shelf.lower():
                return name
        return None

    # tools
    def set_library(self, folder: str) -> str:
        folder = _text(folder, "folder", 500)
        if not folder:
            raise ValueError("folder is the Calibre library folder, the one that holds metadata.db")
        db = _db_file(folder)
        conn = _connect(str(db))
        try:
            count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        finally:
            conn.close()
        home = str(db.parent.resolve())
        with self.store.lock:
            data = self.store.load()
            data["library"] = home
            self.store.save(data)
        return (f"Library set to {home}: {_plural(count, 'book')}. "
                "I only read it, and never change anything in that folder.")

    def find_books(self, query: str = "", author: str = "", series: str = "", tag: str = "") -> str:
        query, author = _text(query, "query", 200), _text(author, "author", 200)
        series, tag = _text(series, "series", 200), _text(tag, "tag", 200)
        if not (query or author or series or tag):
            raise ValueError("say what to look for: words from a title, an author, a series or a tag")
        where, args = [], []
        if query:
            where.append(f"(b.title LIKE ? ESCAPE '\\' OR {_AUTHOR} OR {_SERIES} OR {_TAG})")
            args += [_like(query)] * 4
        for clause, value in ((_AUTHOR, author), (_SERIES, series), (_TAG, tag)):
            if value:
                where.append(clause)
                args.append(_like(value))
        rows = self._rows(where, args, order=_SERIES_ORDER if series else "b.sort, b.title")
        if not rows:
            return "No books in the library match that."
        head = f"The first {TOP} books that match:" if len(rows) > TOP else f"{_plural(len(rows), 'book')} match:"
        return head + "\n" + "\n".join(_line(r) for r in rows[:TOP])

    def book_details(self, book: str) -> str:
        row = self._book(book)
        lines = [_line(row)]
        if row["tags"]:
            lines.append("Tags: " + ", ".join(sorted(row["tags"].split("|"))))
        if row["timestamp"]:
            lines.append("Added to the library: " + str(row["timestamp"])[:10])
        conn = _connect(self.store.load()["library"])
        try:
            got = conn.execute("SELECT text FROM comments WHERE book = ?", [row["id"]]).fetchone()
        except sqlite3.Error:
            got = None                       # an older library without a comments table
        finally:
            conn.close()
        if got and got[0]:
            about = " ".join(re.sub(r"<[^>]+>", " ", str(got[0])).split())
            lines.append("About: " + (about[:600] + "..." if len(about) > 600 else about))
        on = [n for n, items in self.store.load()["shelves"].items() if any(i.get("id") == row["id"] for i in items)]
        lines.append("On your shelves: " + (", ".join(on) if on else "none"))
        return "\n".join(lines)

    def shelves(self, shelf: str = "") -> str:
        shelf = _text(shelf, "shelf", 60)
        data = self.store.load()["shelves"]
        if not shelf:
            if not data:
                return "You have no shelves yet. Say: put Dracula on my to-read shelf."
            parts = [f"{n} ({_plural(len(items), 'book')})" for n, items in data.items()]
            return f"You have {_plural(len(data), 'shelf', 'shelves')}: " + ", ".join(parts) + "."
        real = self._shelf_name(data, shelf)
        if real is None:
            return f"You have no shelf called {shelf}."
        items = data[real]
        if not items:
            return f"Your {real} shelf is empty."
        return f"On your {real} shelf:\n" + "\n".join(f"{i.get('title', '?')} [#{i.get('id')}]" for i in items)

    def shelf_add(self, book: str, shelf: str) -> str:
        shelf = _text(shelf, "shelf", 60)
        if not shelf:
            raise ValueError("say which shelf, like to-read")
        row = self._book(book)
        with self.store.lock:
            data = self.store.load()
            real = self._shelf_name(data["shelves"], shelf)
            if real is None:
                if len(data["shelves"]) >= MAX_SHELVES:
                    raise ValueError(f"you have {MAX_SHELVES} shelves already, the most this keeps")
                real = shelf
                data["shelves"][real] = []
            items = data["shelves"][real]
            if any(i.get("id") == row["id"] for i in items):
                return f"{row['title']} is already on your {real} shelf."
            if len(items) >= MAX_ON_SHELF:
                raise ValueError(f"the {real} shelf holds {MAX_ON_SHELF} books already, the most this keeps")
            items.append({"id": row["id"], "title": row["title"], "added": _now()})
            self.store.save(data)
        return f"Done, {row['title']} is on your {real} shelf."

    def shelf_remove(self, book: str, shelf: str) -> str:
        book, shelf = _text(book, "book", 300), _text(shelf, "shelf", 60)
        if not book or not shelf:
            raise ValueError("say which book and which shelf")
        with self.store.lock:
            data = self.store.load()
            real = self._shelf_name(data["shelves"], shelf)
            if real is None:
                return f"You have no shelf called {shelf}, so nothing changed."
            items = data["shelves"][real]
            num = book.lstrip("#")
            hits = [i for i in items if (num.isdigit() and i.get("id") == int(num))
                    or str(i.get("title", "")).lower() == book.lower()]
            hits = hits or [i for i in items if book.lower() in str(i.get("title", "")).lower()]
            if not hits:
                return f"{book} is not on your {real} shelf, so nothing changed."
            if len(hits) > 1:
                raise ValueError("more than one book on that shelf fits. Say which by its number: "
                                 + ", ".join(f"{i.get('title')} [#{i.get('id')}]" for i in hits[:5]))
            items.remove(hits[0])
            gone = not items
            if gone:
                del data["shelves"][real]
            self.store.save(data)
        return (f"Done, I took {hits[0].get('title')} off your {real} shelf."
                + (" That shelf was then empty, so it is gone." if gone else ""))

    def recommend(self, tag: str = "", author: str = "") -> str:
        tag, author = _text(tag, "tag", 200), _text(author, "author", 200)
        where, args = [], []
        if tag:
            where.append(_TAG)
            args.append(_like(tag))
        if author:
            where.append(_AUTHOR)
            args.append(_like(author))
        shelves = self.store.load()["shelves"]
        done = {i.get("id") for n, items in shelves.items() if n.lower() in READ_SHELVES for i in items}
        rows = [r for r in self._rows(where, args, order="b.timestamp DESC", limit=200) if r["id"] not in done][:3]
        if not rows:
            return "I found nothing in the library to suggest for that."
        about = " ".join(x for x in (f"tagged {tag}" if tag else "", f"by {author}" if author else "") if x)
        return (f"Picked by plain rules, not by the brain: the newest books added{' ' + about if about else ''} "
                "that are not on a Read or Finished shelf.\n" + "\n".join(_line(r) for r in rows))

    def library_stats(self) -> str:
        conn = _connect(self.store.load()["library"])
        try:
            def one(sql: str) -> int:
                return conn.execute(sql).fetchone()[0]
            books, authors = one("SELECT COUNT(*) FROM books"), one("SELECT COUNT(*) FROM authors")
            series, tags = one("SELECT COUNT(*) FROM series"), one("SELECT COUNT(*) FROM tags")
            formats = conn.execute("SELECT format, COUNT(DISTINCT book) FROM data GROUP BY format "
                                   "ORDER BY 2 DESC").fetchall()
            newest = conn.execute("SELECT title, timestamp FROM books ORDER BY timestamp DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        line = (f"Your Calibre library has {_plural(books, 'book')}, {_plural(authors, 'author')}, "
                f"{_plural(series, 'series', 'series')} and {_plural(tags, 'tag')}.")
        if formats:
            line += " Formats: " + ", ".join(f"{f} {c}" for f, c in formats) + "."
        if newest:
            line += f" Newest added: {newest[0]} ({str(newest[1] or '')[:10]})."
        return line + f" You keep {_plural(len(self.store.load()['shelves']), 'shelf', 'shelves')} here."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("set_library", "Set the Calibre library folder to read books from (the folder that holds metadata.db). "
     "It is only read, never changed.",
     {"folder": _p("string", "The Calibre library folder, like D:\\Books\\Calibre Library.")}, ["folder"]),
    ("find_books", "Search the Calibre book library by title words, author, series or tag. Shows up to 20 books "
     "with authors, series, year and formats.",
     {"query": _p("string", "Words to look for in titles, authors, series and tags."),
      "author": _p("string", "Optional: an author name, or part of one."),
      "series": _p("string", "Optional: a series name, or part of one."),
      "tag": _p("string", "Optional: a tag, like mystery or cooking.")}, []),
    ("book_details", "Everything the library knows about one book: authors, series, year, formats, tags, "
     "the description, and which of your shelves it is on.",
     {"book": _p("string", "The book's title, or its number like #12.")}, ["book"]),
    ("shelves", "List your book shelves, or the books on one shelf.",
     {"shelf": _p("string", "Optional: a shelf name. Leave empty for all shelves.")}, []),
    ("shelf_add", "Put a book from the library on one of your shelves. Makes the shelf if it is new.",
     {"book": _p("string", "The book's title, or its number like #12."),
      "shelf": _p("string", "The shelf name, like to-read.")}, ["book", "shelf"]),
    ("shelf_remove", "Take a book off one of your shelves.",
     {"book": _p("string", "The book's title, or its number like #12."),
      "shelf": _p("string", "The shelf name.")}, ["book", "shelf"]),
    ("recommend", "Suggest a few books to read from the library: the newest added, by a tag or an author, "
     "that are not on a Read shelf. Plain rules, not the brain.",
     {"tag": _p("string", "Optional: a tag, like mystery."),
      "author": _p("string", "Optional: an author.")}, []),
    ("library_stats", "How big the Calibre library is: books, authors, series, tags, formats and the newest book.",
     {}, []),
]
CHANGES = ["set_library", "shelf_add", "shelf_remove"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Shelves, msg: dict) -> dict | None:
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
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (Unreadable, NoLibrary) as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Book shelves does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "BookShelves/1"
    app: Shelves
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

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Book shelves needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Book shelves stopped on this request with an error ({type(exc).__name__})."})
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
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "Book shelves has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Book shelves has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Shelves(Store(data))
    handler = type("BookShelvesHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Book shelves for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Book shelves with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
