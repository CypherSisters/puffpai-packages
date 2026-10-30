"""Book shelves' own server (book-shelves/server.py): a tiny Calibre metadata.db built here,
read read-only, the owner's shelves, and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import contextlib
import hashlib
import importlib.util
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("book_shelves_server", PKG / "server.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def make_library(folder: Path) -> Path:
    """The handful of Calibre tables this package reads, with four books."""
    folder.mkdir(parents=True)
    conn = sqlite3.connect(folder / "metadata.db")
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, sort TEXT, timestamp TEXT, pubdate TEXT,
                            series_index REAL DEFAULT 1.0, path TEXT DEFAULT '');
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
        CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
        CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT, uncompressed_size INTEGER);
        INSERT INTO books VALUES (1, 'Dracula', 'Dracula', '2026-01-01 10:00:00+00:00', '1897-05-26', 1, '');
        INSERT INTO books VALUES (2, 'Murder on the Orient Express', 'Murder on the Orient Express',
                                  '2026-03-01 10:00:00+00:00', '1934-01-01', 1, '');
        INSERT INTO books VALUES (3, 'The Mysterious Affair at Styles', 'Mysterious Affair at Styles, The',
                                  '2026-02-01 10:00:00+00:00', '0101-01-01', 1, '');
        INSERT INTO books VALUES (4, 'The ABC Murders', 'ABC Murders, The', '2026-04-01 10:00:00+00:00', '1936-01-06', 2, '');
        INSERT INTO authors VALUES (1, 'Bram Stoker'), (2, 'Agatha Christie');
        INSERT INTO books_authors_link (book, author) VALUES (1, 1), (2, 2), (3, 2), (4, 2);
        INSERT INTO series VALUES (1, 'Hercule Poirot');
        INSERT INTO books_series_link (book, series) VALUES (2, 1), (3, 1), (4, 1);
        INSERT INTO tags VALUES (1, 'Horror'), (2, 'Mystery');
        INSERT INTO books_tags_link (book, tag) VALUES (1, 1), (2, 2), (3, 2), (4, 2);
        INSERT INTO data (book, format, name, uncompressed_size) VALUES
            (1, 'EPUB', 'Dracula', 1), (1, 'PDF', 'Dracula', 1), (2, 'EPUB', 'Murder', 1),
            (3, 'EPUB', 'Styles', 1), (4, 'MOBI', 'ABC', 1);
    """)
    conn.commit()
    conn.close()
    return folder


@pytest.fixture
def library(tmp_path):
    return make_library(tmp_path / "Calibre Library")


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data" / "book-shelves"


@pytest.fixture
def base(data):
    with running(bs.make_server(0, data, TOKEN)) as url:
        yield url


def call(url, method="GET", body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method, headers=h)
    try:
        with _open(req, timeout=10) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def mcp(base, method, params=None, token=TOKEN):
    return call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token)


def tool(base, name, /, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200 and not out["result"]["isError"], out
    return out["result"]["content"][0]["text"]


def tool_error(base, name, /, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200 and out["result"]["isError"], out
    return out["result"]["content"][0]["text"]


def test_every_route_needs_the_token(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")


def test_tools_match_the_manifest(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    code, out = mcp(base, "tools/list")
    names = {t["name"] for t in out["result"]["tools"]}
    assert names == set(manifest["abilities"]["tools"]) == set(manifest["abilities"]["examples"])
    assert set(manifest["abilities"]["changes"]) == set(bs.CHANGES) <= names


def test_no_library_is_a_plain_answer(base):
    assert "No Calibre library is set yet" in tool_error(base, "find_books", query="Dracula")


def test_library_is_read_and_never_written(base, library, data):
    db = library / "metadata.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert "4 books" in tool(base, "set_library", folder=str(library))

    found = tool(base, "find_books", author="christie")
    assert "3 books match" in found and "Murder on the Orient Express by Agatha Christie (Hercule Poirot #1), 1934, EPUB [#2]" in found
    assert "Dracula" in tool(base, "find_books", query="horror")
    assert "No books" in tool(base, "find_books", tag="cooking")
    series = tool(base, "find_books", series="poirot").splitlines()
    assert series[1].startswith("Murder on the Orient Express") and series[3].startswith("The ABC Murders")

    details = tool(base, "book_details", book="dracula")
    assert "1897" in details and "EPUB/PDF" in details and "Tags: Horror" in details and "On your shelves: none" in details
    assert "Styles" in tool(base, "book_details", book="#3") and ", 0101" not in tool(base, "book_details", book="#3")
    assert "more than one book" in tool_error(base, "book_details", book="murder")

    stats = tool(base, "library_stats")
    assert "4 books, 2 authors, 1 series and 2 tags" in stats and "EPUB 3" in stats and "The ABC Murders" in stats

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in library.iterdir()) == ["metadata.db"]
    assert json.loads((data / "shelves.json").read_text(encoding="utf-8"))["library"] == str(library.resolve())


def test_shelves_and_recommend(base, library, data):
    tool(base, "set_library", folder=str(library / "metadata.db"))
    assert "no shelves yet" in tool(base, "shelves")
    assert "Dracula is on your to-read shelf" in tool(base, "shelf_add", book="Dracula", shelf="to-read")
    assert "already" in tool(base, "shelf_add", book="#1", shelf="To-Read")
    tool(base, "shelf_add", book="ABC", shelf="Read")
    assert tool(base, "shelves") == "You have 2 shelves: to-read (1 book), Read (1 book)."
    assert "Dracula [#1]" in tool(base, "shelves", shelf="to-read")
    assert "On your shelves: to-read" in tool(base, "book_details", book="Dracula")

    rec = tool(base, "recommend", tag="mystery")
    assert "not by the brain" in rec and "ABC" not in rec
    assert rec.splitlines()[1].startswith("Murder on the Orient Express")

    assert "nothing changed" in tool(base, "shelf_remove", book="Styles", shelf="to-read")
    assert "it is gone" in tool(base, "shelf_remove", book="dracula", shelf="to-read")
    saved = json.loads((data / "shelves.json").read_text(encoding="utf-8"))
    assert list(saved["shelves"]) == ["Read"]


def test_bad_input_is_refused(base, library, data):
    assert "no metadata.db" in tool_error(base, "set_library", folder=str(library.parent / "nowhere"))
    assert "did not fit" in tool_error(base, "find_books")
    assert "did not fit" in tool_error(base, "shelf_add", book="Dracula", shelf="x" * 61)
    assert "did not fit" in tool_error(base, "find_books", query=["not", "words"])
    (data / "shelves.json").write_text("{broken", encoding="utf-8")
    assert "will not write over it" in tool_error(base, "shelf_add", book="Dracula", shelf="to-read")
    assert (data / "shelves.json").read_text(encoding="utf-8") == "{broken"
