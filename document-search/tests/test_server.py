"""Document search's own server (document-search/server.py): the five tools, the missing
FTS5 answer, the token on every route and a refused input. The server listens on a free
port on 127.0.0.1 and is stopped by the test that started it; nothing is left running."""

import base64
import contextlib
import importlib.util
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("document_search_server", PKG / "server.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)

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


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data" / "document-search"


@pytest.fixture
def base(data):
    with running(ds.make_server(0, data, TOKEN)) as url:
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


def tool(base, name, /, error=False, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200 and out["result"]["isError"] is error, out
    return out["result"]["content"][0]["text"]


def docx(*paragraphs: str) -> str:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    return base64.b64encode(buf.getvalue()).decode()


def test_every_route_needs_the_token(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")
    code, out = mcp(base, "tools/list")
    assert [t["name"] for t in out["result"]["tools"]] == [
        "add_document", "search_documents", "read_document", "list_documents", "remove_document"]


def test_add_search_read_list_remove(base, data):
    assert tool(base, "list_documents") == "No documents kept yet."
    said = tool(base, "add_document", file_name="Roof warranty.docx", mime="", content_b64=docx(
        "The roof warranty covers leaks for twenty years.", "Call the roofer first."))
    assert said.startswith("Added Roof warranty.docx as document 1")
    tool(base, "add_document", file_name="lasagna.html", mime="text/html",
         text="<html><head><style>p{}</style></head><body><p>Vegetable lasagna &amp; ricotta</p><script>x()</script></body></html>")
    tool(base, "add_document", file_name="notes", mime="text/plain", text="A plain note about the garden.")
    assert "already kept as document 2" in tool(base, "add_document", file_name="again.html", mime="text/html",
                                                   text="<p>Vegetable lasagna &amp; ricotta</p>")
    found = tool(base, "search_documents", query="warranties for the roof?")
    assert found.startswith("1 match") and "1. Roof warranty.docx:" in found and "[warranty]" in found
    assert "2. lasagna.html" in tool(base, "search_documents", query='lasagna" OR NEAR(')
    assert "No kept document" in tool(base, "search_documents", query="submarine")
    page = tool(base, "read_document", id=2)
    assert "Vegetable lasagna & ricotta" in page and "x()" not in page and "p{}" not in page
    assert tool(base, "list_documents").startswith("3 documents kept")
    assert "Removed Roof warranty.docx" in tool(base, "remove_document", id=1)
    assert "No kept document" in tool(base, "search_documents", query="roof")
    assert "There is no document 1" in tool(base, "read_document", id="1")
    assert (data / "documents.sqlite").is_file()


def test_without_fts5_every_tool_says_so(data, monkeypatch):
    real = sqlite3.connect

    class NoFts:
        def __init__(self, *a, **k):
            self.c = real(*a, **k)

        def execute(self, sql, *a):
            if "fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self.c.execute(sql, *a)

        def close(self):
            self.c.close()

    monkeypatch.setattr(ds.sqlite3, "connect", NoFts)
    with running(ds.make_server(0, data, TOKEN)) as base:
        assert call(base + "/health") == (200, "ok")
        assert tool(base, "list_documents") == ds.NO_FTS
        assert tool(base, "add_document", file_name="a.txt", text="hi") == ds.NO_FTS


def test_a_bad_input_is_refused_plainly(base):
    assert "not a type this tool takes" in tool(base, "add_document", error=True, file_name="a.pdf",
                                                mime="application/pdf", content_b64="JVBERg==")
    assert "must be a number" in tool(base, "read_document", error=True, id="two")
    assert "whole number" in tool(base, "remove_document", error=True, id=1.5)
    assert "not a real .docx" in tool(base, "add_document", error=True, file_name="a.docx",
                                      content_b64=base64.b64encode(b"plain").decode())
