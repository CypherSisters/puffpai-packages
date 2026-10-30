"""Document convert's own server (document-convert/server.py): the three tools, the missing
LibreOffice answer, the token on every route and a refused input. LibreOffice itself is
never run: the tests stand in for it. The server listens on a free port on 127.0.0.1 and
is stopped by the test that started it."""

import base64
import contextlib
import importlib.util
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("document_convert_server", PKG / "server.py")
dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dc)

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
    return tmp_path / "data" / "document-convert"


@pytest.fixture
def base(data):
    with running(dc.make_server(0, data, TOKEN)) as url:
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


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def docx(*paragraphs: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_every_route_needs_the_token(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")
    code, out = mcp(base, "tools/list")
    assert code == 200 and [t["name"] for t in out["result"]["tools"]] == ["convert_to_pdf", "read_document_text", "list_converted"]


def test_convert_runs_libreoffice_with_an_argument_list_and_keeps_the_pdf(base, data, monkeypatch):
    seen = []

    def fake_run(argv, timeout):
        seen.append((argv, timeout))
        out = Path(argv[argv.index("--outdir") + 1])
        (out / (Path(argv[-1]).stem + ".pdf")).write_bytes(b"%PDF-1.7 fake")
        return 0, ""

    monkeypatch.setattr(dc, "find_soffice", lambda: "soffice")
    monkeypatch.setattr(dc, "run_soffice", fake_run)
    said = tool(base, "convert_to_pdf", file_name="../Our Plan; rm -rf.docx", mime="", content_b64=b64(docx("hi")))
    argv, timeout = seen[0]
    assert timeout == 120 and argv[:2] == ["soffice", "--headless"] and "--convert-to" in argv
    assert Path(argv[-1]).name == "Our Plan_ rm -rf.docx" and ".." not in Path(argv[-1]).name
    made = data / "made" / "Our Plan_ rm -rf.pdf"
    assert made.read_bytes() == b"%PDF-1.7 fake" and str(made) in said
    assert list((data / "work").iterdir()) == []
    tool(base, "convert_to_pdf", file_name="Our Plan; rm -rf.docx", content_b64=b64(docx("hi")))
    listed = tool(base, "list_converted")
    assert listed.startswith("2 PDFs made") and "Our Plan_ rm -rf (2).pdf" in listed


def test_convert_without_libreoffice_says_so_plainly(base, monkeypatch):
    monkeypatch.setattr(dc, "find_soffice", lambda: None)
    assert tool(base, "convert_to_pdf", file_name="a.txt", text="hello") == dc.MISSING
    assert tool(base, "list_converted") == "No PDFs made yet."


def test_read_document_text_reads_office_files_without_libreoffice(base):
    said = tool(base, "read_document_text", file_name="notes.docx", content_b64=b64(docx("First line", "Second line")))
    assert "First line\nSecond line" in said
    assert "hello there" in tool(base, "read_document_text", file_name="a.md", text="# hello there")
    assert "without LibreOffice" in tool(base, "read_document_text", file_name="slides.odp", content_b64=b64(b"x"))


def test_a_bad_input_is_refused_plainly(base):
    assert "not a type this tool takes" in tool(base, "convert_to_pdf", error=True, file_name="run.exe", content_b64=b64(b"MZ"))
    assert "not base64" in tool(base, "convert_to_pdf", error=True, file_name="a.docx", content_b64="@@@")
    assert "not a real .docx" in tool(base, "read_document_text", error=True, file_name="a.docx", content_b64=b64(b"plain"))
