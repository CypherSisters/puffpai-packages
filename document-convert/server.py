# Harvested from HomeBotFounders@b77fc40:src/puffpai/agents/toolsmith.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/capability_registry.py (rewritten-from)
"""Document convert: office files to PDF, and plain text out of them, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN.

``convert_to_pdf`` runs LibreOffice on this computer (``soffice --headless
--convert-to pdf``) when it is installed; the input is written to a job folder
inside PUFFPAI_DATA, LibreOffice writes the PDF beside it, and the PDF moves to
``made/``. LibreOffice gets an argument list, never a shell string, a profile
folder of its own inside PUFFPAI_DATA, and 120 seconds. Without LibreOffice
every tool that needs it answers one plain sentence.

``read_document_text`` needs no LibreOffice: a .docx, .xlsx or .pptx is a zip of
XML, read here with zipfile and xml.etree.

HomeBot ran the same LibreOffice call inside a bubblewrap jail with no network.
This package has no jail: LibreOffice runs as the owner's own user, like any
program she opens.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import glob
import hmac
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

NAME = "document-convert"
VERSION = "1.0.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_FILE = 25 * 1024 * 1024                    # one input file, in bytes
MAX_BODY = MAX_FILE * 4 // 3 + 64 * 1024       # the file as base64, plus the JSON around it
MAX_UNZIPPED = 200 * 1024 * 1024               # what a .docx/.xlsx/.pptx may unpack to
MAX_TEXT = 20_000                              # characters read_document_text answers
TIMEOUT_S = 120
CONVERTS = (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".txt", ".md")
READS = (".docx", ".xlsx", ".pptx", ".txt", ".md")
MISSING = ("LibreOffice is not installed on this computer, so I cannot make a PDF yet. "
           "Install it from https://www.libreoffice.org/download/ and ask again.")


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


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


def _safe_name(file_name: str) -> str:
    """The file's own name with only plain characters, never a folder part."""
    base = re.split(r"[\\/]", file_name)[-1]
    base = re.sub(r"[^A-Za-z0-9 ._()-]", "_", base).strip(" .")
    return base[:120] or "document"


def _file_bytes(file_name, mime, content_b64, text, allowed) -> tuple[str, str, bytes]:
    """(safe name, extension, bytes) of a file sent the contract's way, checked."""
    name = _safe_name(_text(file_name, "file_name", 255))
    _text(mime, "mime", 120)
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        raise ValueError(f"{ext or 'a file with no extension'} is not a type this tool takes. "
                         f"It takes {', '.join(allowed)}")
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
    if not raw:
        raise ValueError("the file is empty")
    return name, ext, raw


# ---------------------------------------------------------------- LibreOffice

def find_soffice() -> str | None:
    """LibreOffice's soffice on PATH or in its usual install folders, or None."""
    for word in ("soffice", "libreoffice"):
        found = shutil.which(word)
        if found:
            return found
    places = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(env):
            places.append(os.path.join(os.environ[env], "LibreOffice", "program", "soffice.exe"))
    places += ["/Applications/LibreOffice.app/Contents/MacOS/soffice",
               "/usr/bin/soffice", "/usr/local/bin/soffice", "/usr/lib/libreoffice/program/soffice",
               "/snap/bin/libreoffice", *sorted(glob.glob("/opt/libreoffice*/program/soffice"))]
    for place in places:
        if os.path.isfile(place):
            return place
    return None


def run_soffice(argv: list[str], timeout: float) -> tuple[int | None, str]:
    """Run LibreOffice with an argument list (never a shell). (exit code, its output), or
    (None, "") when it ran past *timeout*; then it and the processes it started are stopped."""
    kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(argv, **kw)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or b"").decode("utf-8", "replace")[-400:]
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":     # this one process and its children, by its PID
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.kill()
        proc.communicate()
        return None, ""


# ---------------------------------------------------------------- reading office files

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _member(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_UNZIPPED:
        raise ValueError("the document unpacks to more than this tool reads")
    return z.read(info)


def _paragraphs(xml: bytes, para: str) -> list[str]:
    """Text of each <para> element (w:p or a:p): its t runs, tabs and breaks."""
    out = []
    for p in ElementTree.fromstring(xml).iter():
        if _local(p.tag) != para:
            continue
        bits = []
        for el in p.iter():
            tag = _local(el.tag)
            if tag == "t" and el.text:
                bits.append(el.text)
            elif tag == "tab":
                bits.append("\t")
            elif tag in ("br", "cr"):
                bits.append("\n")
        out.append("".join(bits))
    return out


def _numbered(names: list[str], pattern: str) -> list[str]:
    found = [(int(m.group(1)), n) for n in names if (m := re.fullmatch(pattern, n))]
    return [n for _, n in sorted(found)]


def office_text(raw: bytes, ext: str) -> str:
    """Plain text from a .docx, .xlsx or .pptx (a zip of XML)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError(f"that file is not a real {ext} (it will not open as one)") from None
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
            raise ValueError("the document unpacks to more than this tool reads")
        names = z.namelist()
        try:
            if ext == ".docx":
                return "\n".join(_paragraphs(_member(z, "word/document.xml"), "p"))
            if ext == ".pptx":
                slides = []
                for i, n in enumerate(_numbered(names, r"ppt/slides/slide(\d+)\.xml"), 1):
                    lines = [x for x in _paragraphs(_member(z, n), "p") if x.strip()]
                    slides.append(f"Slide {i}:\n" + "\n".join(lines))
                return "\n\n".join(slides)
            if ext == ".xlsx":
                shared = []
                if "xl/sharedStrings.xml" in names:
                    for si in ElementTree.fromstring(_member(z, "xl/sharedStrings.xml")):
                        shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
                sheets = []
                for i, n in enumerate(_numbered(names, r"xl/worksheets/sheet(\d+)\.xml"), 1):
                    rows = []
                    for row in ElementTree.fromstring(_member(z, n)).iter():
                        if _local(row.tag) != "row":
                            continue
                        cells = []
                        for c in row:
                            if _local(c.tag) != "c":
                                continue
                            kind = c.get("t", "")
                            if kind == "inlineStr":
                                cells.append("".join(t.text or "" for t in c.iter() if _local(t.tag) == "t"))
                                continue
                            v = next((x.text or "" for x in c if _local(x.tag) == "v"), "")
                            if kind == "s" and v.isdigit() and int(v) < len(shared):
                                v = shared[int(v)]
                            cells.append(v)
                        rows.append("\t".join(cells))
                    sheets.append(f"Sheet {i}:\n" + "\n".join(rows))
                return "\n\n".join(sheets)
        except KeyError:
            raise ValueError(f"that file is not a real {ext} (a part of it is missing)") from None
        except ElementTree.ParseError:
            raise ValueError(f"that {ext} has damaged text inside, so it will not read") from None
    raise ValueError(f"{ext} is not a format this tool reads")


# ---------------------------------------------------------------- the tools

class Converter:
    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.made = self.folder / "made"
        self.work = self.folder / "work"
        self.profile = self.folder / "libreoffice-profile"
        self.made.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()        # one LibreOffice at a time (one profile)

    def _free_name(self, stem: str) -> Path:
        path = self.made / f"{stem}.pdf"
        n = 2
        while path.exists():
            path = self.made / f"{stem} ({n}).pdf"
            n += 1
        return path

    def convert_to_pdf(self, file_name: str = "", mime: str = "", content_b64: str = "", text: str = "") -> str:
        name, _, raw = _file_bytes(file_name, mime, content_b64, text, CONVERTS)
        soffice = find_soffice()
        if not soffice:
            return MISSING
        with self.lock:
            job = self.work / uuid.uuid4().hex
            out = job / "out"
            out.mkdir(parents=True)
            try:
                source = job / name
                source.write_bytes(raw)
                argv = [soffice, "--headless", "--norestore", "--nolockcheck",
                        f"-env:UserInstallation={self.profile.resolve().as_uri()}",
                        "--convert-to", "pdf", "--outdir", str(out), str(source)]
                code, said = run_soffice(argv, TIMEOUT_S)
                if code is None:
                    return f"LibreOffice took longer than {TIMEOUT_S} seconds on {name}, so I stopped it. No PDF was made."
                produced = out / (source.stem + ".pdf")
                if not produced.is_file():      # LibreOffice can exit 0 without writing anything
                    why = said.strip().splitlines()[-1][:200] if said.strip() else "it gave no reason"
                    return f"LibreOffice did not make a PDF from {name} ({why})."
                target = self._free_name(source.stem)
                shutil.move(str(produced), str(target))
                return f"Made {target.name} ({_size(target.stat().st_size)}). It is at {target}"
            finally:
                shutil.rmtree(job, ignore_errors=True)

    def read_document_text(self, file_name: str = "", mime: str = "", content_b64: str = "", text: str = "") -> str:
        name = _safe_name(_text(file_name, "file_name", 255))
        ext = os.path.splitext(name)[1].lower()
        if ext not in READS:
            return (f"I cannot read {ext or 'that kind of file'} without LibreOffice. I read "
                    f"{', '.join(READS)}. Converting it to a PDF may help.")
        name, ext, raw = _file_bytes(file_name, mime, content_b64, text, READS)
        if ext in (".txt", ".md"):
            body = raw.decode("utf-8-sig", "replace")
        else:
            body = office_text(raw, ext)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if not body:
            return f"{name} has no text in it."
        cut = len(body) > MAX_TEXT
        body = body[:MAX_TEXT]
        return f"Text of {name}{' (the first part only)' if cut else ''}:\n\n{body}"

    def list_converted(self) -> str:
        files = sorted((p for p in self.made.glob("*.pdf") if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return "No PDFs made yet."
        lines = [f"- {p.name} ({_size(p.stat().st_size)}, "
                 f"{datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')})" for p in files[:50]]
        more = f"\n...and {len(files) - 50} more." if len(files) > 50 else ""
        return f"{len(files)} PDF{'s' if len(files) != 1 else ''} made, newest first, in {self.made}:\n" + "\n".join(lines) + more


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


FILE_ARGS = {
    "file_name": _p("string", "The file's name with its extension, like report.docx."),
    "mime": _p("string", "The file's type, like application/vnd.openxmlformats-officedocument.wordprocessingml.document."),
    "content_b64": _p("string", "The file's bytes, base64."),
    "text": _p("string", "For a .txt or .md file: its text, instead of content_b64."),
}

TOOLS = [
    ("convert_to_pdf", "Turn a document (Word .docx, Excel .xlsx, PowerPoint .pptx, .odt, .ods, .odp, .txt or .md) "
     "into a PDF with LibreOffice on this computer, and say where the PDF is.", FILE_ARGS, ["file_name"]),
    ("read_document_text", "Read the plain text out of a Word .docx, Excel .xlsx or PowerPoint .pptx (or a .txt or .md) "
     "without changing it.", FILE_ARGS, ["file_name"]),
    ("list_converted", "List the PDFs this tool has made, newest first.", {}, []),
]
CHANGES = ["convert_to_pdf"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Converter, msg: dict) -> dict | None:
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
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Document convert does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "DocumentConvert/1"
    app: Converter
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
        self._json(401, {"error": "Document convert needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Document convert stopped on this request with an error ({type(exc).__name__})."})
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
        self._json(404, {"error": "Document convert has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self.path.split("?", 1)[0] != "/mcp":
            return self._json(404, {"error": "Document convert has no page here."})
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
    app = Converter(data)
    handler = type("ConvertHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Document convert for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Document convert with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
