# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/capability_registry.py (rewritten-from)
"""Document search: keep the owner's documents and search their words, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN.

The documents live in one SQLite file in PUFFPAI_DATA (``documents.sqlite``):
a ``docs`` table for each document's name and details, and an FTS5 full-text
index of its words. HomeBot searched with an embedding model and ChromaDB; this
package uses only the sqlite3 that comes with Python, so a search finds words,
not meanings. If this Python's SQLite has no FTS5, every tool says so plainly.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import io
import json
import math
import os
import re
import sqlite3
import threading
import zipfile
from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

NAME = "document-search"
VERSION = "1.0.0"
DB = "documents.sqlite"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_FILE = 10 * 1024 * 1024                    # one document, in bytes
MAX_BODY = MAX_FILE * 4 // 3 + 64 * 1024       # the file as base64, plus the JSON around it
MAX_UNZIPPED = 100 * 1024 * 1024
MAX_READ = 20_000                              # characters read_document answers
TEXT_TYPES = (".txt", ".md", ".csv", ".json")
TAKES = (*TEXT_TYPES, ".docx", ".html", ".htm")
NO_FTS = ("This computer's Python has a SQLite without full-text search (FTS5), so Document search "
          "cannot index or search documents here.")


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _number(value, name: str, lo: float, hi: float) -> int:
    """A whole number from *lo* to *hi*, or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi or x != int(x):
        raise ValueError(f"{name} is a whole number from {lo:g} to {hi:g}")
    return int(x)


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


# ---------------------------------------------------------------- reading a file

class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head", "noscript", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(raw: str) -> str:
    p = _HTMLText()
    p.feed(raw)
    p.close()
    return "".join(p.parts)


def docx_text(raw: bytes) -> str:
    """The paragraphs of a .docx (a zip of XML), read with zipfile and xml.etree."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_UNZIPPED:
                raise ValueError("the document unpacks to more than this tool reads")
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("that file is not a real .docx (it will not open as one)") from None
    except ElementTree.ParseError:
        raise ValueError("that .docx has damaged text inside, so it will not read") from None
    lines = []
    for p in root.iter():
        if p.tag.rsplit("}", 1)[-1] != "p":
            continue
        bits = []
        for el in p.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "t" and el.text:
                bits.append(el.text)
            elif tag == "tab":
                bits.append("\t")
            elif tag in ("br", "cr"):
                bits.append("\n")
        lines.append("".join(bits))
    return "\n".join(lines)


def file_text(file_name, mime, content_b64, text) -> tuple[str, str]:
    """(safe name, its text) of a file sent the contract's way, checked."""
    name = re.split(r"[\\/]", _text(file_name, "file_name", 255))[-1].strip()[:200] or "document"
    mime = _text(mime, "mime", 120).lower()
    ext = os.path.splitext(name)[1].lower()
    if ext not in TAKES and not mime.startswith("text/"):
        raise ValueError(f"{ext or 'a file with no extension'} is not a type this tool takes. "
                         f"It takes {', '.join(TAKES)} and other text files")
    if content_b64:
        if not isinstance(content_b64, str):
            raise ValueError("content_b64 must be base64 text")
        if len(content_b64) > MAX_BODY:
            raise ValueError(f"the file is over {_size(MAX_FILE)}")
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_b64 is not base64") from None
    elif text:
        if not isinstance(text, str):
            raise ValueError("text must be words")
        raw = text.encode("utf-8")
    else:
        raise ValueError("send the file as content_b64 (or text, for a text file)")
    if len(raw) > MAX_FILE:
        raise ValueError(f"the file is over {_size(MAX_FILE)}")
    if ext == ".docx":
        body = docx_text(raw)
    else:
        body = raw.decode("utf-8-sig", "replace")
        if ext in (".html", ".htm") or mime == "text/html":
            body = html_text(body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        raise ValueError(f"{name} has no text in it to index")
    return name, body


def fts_query(words: str, joiner: str) -> str:
    """Owner words as a safe FTS5 query: each word quoted, joined by AND or OR. Never raw syntax."""
    terms = re.findall(r"\w+", words)[:20]
    return f" {joiner} ".join('"' + t + '"' for t in terms)


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved file exists but will not read. Nothing writes over it."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, mime TEXT, added TEXT NOT NULL,
    chars INTEGER NOT NULL, words INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, body, tokenize = 'porter unicode61');
"""


class Library:
    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DB
        self.lock = threading.Lock()
        self.problem = ""
        try:
            with closing(sqlite3.connect(":memory:")) as c:
                c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.OperationalError:
            self.problem = NO_FTS
            return
        try:
            with closing(self._connect()) as c, c:
                c.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            self.problem = f"{self.path} will not open ({exc}). Document search will not write over it."

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _db(self):
        """A connection, or Unreadable with the plain reason the library cannot be used."""
        if self.problem:
            raise Unreadable(self.problem)
        return closing(self._connect())

    def _run(self, work):
        with self.lock:
            try:
                with self._db() as c, c:
                    return work(c)
            except sqlite3.DatabaseError as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Document search will not write over it.") from None

    # tools
    def add_document(self, file_name: str = "", mime: str = "", content_b64: str = "", text: str = "") -> str:
        if self.problem:
            return self.problem
        name, body = file_text(file_name, mime, content_b64, text)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        words = len(body.split())

        def work(c):
            same = c.execute("SELECT id FROM docs WHERE sha256 = ?", (digest,)).fetchone()
            if same:
                return f"That text is already kept as document {same[0]}, so I did not add it again."
            cur = c.execute("INSERT INTO docs (name, mime, added, chars, words, sha256) VALUES (?, ?, ?, ?, ?, ?)",
                            (name, mime[:120], datetime.now().isoformat(timespec="seconds"), len(body), words, digest))
            c.execute("INSERT INTO fts (rowid, name, body) VALUES (?, ?, ?)", (cur.lastrowid, name, body))
            return f"Added {name} as document {cur.lastrowid} ({_plural(words, 'word')}). It can be searched now."
        return self._run(work)

    def search_documents(self, query: str = "") -> str:
        if self.problem:
            return self.problem
        query = _text(query, "query", 500)
        if not re.search(r"\w", query):
            raise ValueError("say what to search for")

        def work(c):
            for joiner in ("AND", "OR"):
                rows = c.execute(
                    "SELECT rowid, name, snippet(fts, 1, '[', ']', ' ... ', 16) FROM fts "
                    "WHERE fts MATCH ? ORDER BY rank LIMIT 10", (fts_query(query, joiner),)).fetchall()
                if rows:
                    break
            if not rows:
                return f"No kept document has the words {query!r}."
            lines = [f"{rid}. {name}: {snip.replace(chr(10), ' ')}" for rid, name, snip in rows]
            return (f"{_plural(len(rows), 'match')} (document id, name, a snippet with the found words in [brackets]). "
                    "Use read_document with an id for the whole text:\n" + "\n".join(lines))
        return self._run(work)

    def read_document(self, id=None) -> str:
        if self.problem:
            return self.problem
        doc = _number(id, "id", 1, 2**62)

        def work(c):
            row = c.execute("SELECT d.name, d.words, f.body FROM docs d JOIN fts f ON f.rowid = d.id WHERE d.id = ?",
                            (doc,)).fetchone()
            if not row:
                return f"There is no document {doc}. list_documents shows the ids."
            name, words, body = row
            cut = len(body) > MAX_READ
            return (f"=== {name} (document {doc}, {_plural(words, 'word')}"
                    f"{', the first part only' if cut else ''}) ===\n\n{body[:MAX_READ]}")
        return self._run(work)

    def list_documents(self) -> str:
        if self.problem:
            return self.problem

        def work(c):
            total = c.execute("SELECT count(*) FROM docs").fetchone()[0]
            if not total:
                return "No documents kept yet."
            rows = c.execute("SELECT id, name, words, added FROM docs ORDER BY id DESC LIMIT 100").fetchall()
            lines = [f"{i}. {name} ({_plural(w, 'word')}, added {added[:10]})" for i, name, w, added in rows]
            more = f"\n...and {total - 100} older." if total > 100 else ""
            return f"{_plural(total, 'document')} kept, newest first (id, name):\n" + "\n".join(lines) + more
        return self._run(work)

    def remove_document(self, id=None) -> str:
        if self.problem:
            return self.problem
        doc = _number(id, "id", 1, 2**62)

        def work(c):
            row = c.execute("SELECT name FROM docs WHERE id = ?", (doc,)).fetchone()
            if not row:
                return f"There is no document {doc}. list_documents shows the ids."
            c.execute("DELETE FROM fts WHERE rowid = ?", (doc,))
            c.execute("DELETE FROM docs WHERE id = ?", (doc,))
            return f"Removed {row[0]} (document {doc}) and its words from the index."
        return self._run(work)


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("add_document", "Keep a document (.txt, .md, .csv, .json, .docx, .html or another text file) on this computer "
     "and index its words so it can be searched. A text file comes as file_name, mime and text; any other file as "
     "file_name, mime and content_b64.",
     {"file_name": _p("string", "The file's name with its extension, like warranty.docx."),
      "mime": _p("string", "The file's type, like text/plain."),
      "content_b64": _p("string", "The file's bytes, base64."),
      "text": _p("string", "For a text file: its text, instead of content_b64.")}, ["file_name"]),
    ("search_documents", "Search the owner's kept documents for words, and get the ten best matches with a short "
     "snippet and each document's id. Use this first when the owner asks about something in their documents.",
     {"query": _p("string", "The words to look for, like roof warranty or lasagna.")}, ["query"]),
    ("read_document", "Read the full text of one kept document by its id (from search_documents or list_documents).",
     {"id": _p("integer", "The document's id.")}, ["id"]),
    ("list_documents", "List the kept documents, newest first, with their ids.", {}, []),
    ("remove_document", "Remove one kept document and its words from the index, by its id.",
     {"id": _p("integer", "The document's id.")}, ["id"]),
]
CHANGES = ["add_document", "remove_document"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n == "remove_document"}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Library, msg: dict) -> dict | None:
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
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Document search does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DocumentSearch/1"
    app: Library
    token: str
    timeout = 60        # seconds; a stalled sender is cut off (set on the socket)

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
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _length(self) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most MAX_BODY bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_BODY:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {_size(MAX_BODY)}.")
        return int(raw)

    def _body(self):
        n = self._length()
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            raise ValueError("The request ended before its body did.")
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        """Every route needs the token, checked before anything else is read. A body with a sound
        Content-Length is read, so the refusal arrives whole; any other one closes the connection."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Document search needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Document search stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        self._json(404, {"error": "Document search has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "Document search has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError):
            self.close_connection = True
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if not self._refused():
            self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if not self._refused():
            self._send(405, b"", "text/plain")


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Library(data)
    handler = type("SearchHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Document search for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Document search with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
