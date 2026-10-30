# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
"""Code writer: the brain writes a script, saved as a file, never run, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

A request goes to the bot's brain only through the Core API ``brain`` route
(PUFFPAI_API + ``/v1/brain/chat``, 02 needs.brain). The code that comes back
is saved as one file in ``made/`` inside PUFFPAI_DATA (temp file, then
rename). Nothing here ever runs it. File names are cleaned, every path is
checked to stay inside ``made/``, and a file is at most 200 KB.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "code-writer"
VERSION = "1.0.0"
MADE = "made"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_FILE = 200 * 1024
MAX_BRAIN_ANSWER = 1024 * 1024
FIRST_LINES = 12
LANGUAGES = {"python": ".py", "powershell": ".ps1", "bash": ".sh", "javascript": ".js", "html": ".html"}
SAID = {"py": "python", "python3": "python", "ps": "powershell", "ps1": "powershell", "pwsh": "powershell",
        "sh": "bash", "shell": "bash", "js": "javascript", "node": "javascript", "nodejs": "javascript",
        "htm": "html", "web page": "html", "webpage": "html"}
BRAIN_AWAY = "The brain is not answering right now, so no script was written."
NOT_RUN = "I did not run it, and I never do: read it before you use it."


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- checked input

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


def _language(value) -> str:
    said = _text(value, "language", 30, "python").lower()
    said = SAID.get(said, said)
    if said not in LANGUAGES:
        raise ValueError(f"language is one of {', '.join(LANGUAGES)}")
    return said


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this tool takes")


def _loads(text: str):
    return json.loads(text, parse_constant=_no_constant)


def clean_name(text: str) -> str:
    """A safe file stem: lower-case letters, digits and dashes, at most 60 characters, never empty."""
    stem = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:60].strip("-")
    return stem or "script"


# ---------------------------------------------------------------- the folder

class Folder:
    """The ``made/`` folder: the only place this package writes."""

    def __init__(self, data: Path | str) -> None:
        self.path = (Path(data) / MADE).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def find(self, name: str) -> Path:
        """An existing file in made/ by its name, with or without its ending. Never a path outside it."""
        name = _text(name, "name", 100)
        if not name:
            raise ValueError("say which script, by its name")
        if any(c in name for c in "/\\:") or name.startswith(".") or ".." in name:
            raise ValueError("a script name is a plain file name, with no folders in it")
        for candidate in [self.path / name] + [self.path / (name + ext) for ext in LANGUAGES.values()]:
            real = candidate.resolve()
            if real.parent == self.path and real.is_file():
                return real
        raise ValueError(f"there is no script called {name!r}. Ask me to list the scripts")

    def scripts(self) -> list[Path]:
        return sorted((p for p in self.path.iterdir() if p.is_file() and p.suffix in LANGUAGES.values()
                       and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime, reverse=True)

    def save(self, stem: str, ext: str, code: str) -> Path:
        raw = code.encode("utf-8")
        if len(raw) > MAX_FILE:
            raise ValueError(f"the brain wrote {len(raw) // 1024} KB, more than the {MAX_FILE // 1024} KB "
                             "a script may be, so nothing was saved")
        with self.lock:
            target, n = self.path / f"{stem}{ext}", 2
            while target.exists():
                target, n = self.path / f"{stem}-{n}{ext}", n + 1
            if target.resolve().parent != self.path:
                raise ValueError("that name would leave the scripts folder")
            fd, tmp = tempfile.mkstemp(dir=self.path, prefix=".code-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return target


# ------------------------------------------------------------- the brain

class BrainUnavailable(RuntimeError):
    pass


class Brain:
    """The bot's local model, only through the Core API ``brain`` route (02 needs.brain)."""

    def __init__(self, api: str | None, token: str | None) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        # The Core is on this computer: never send this call through the Bridge proxy.
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def chat(self, prompt: str, system: str = "") -> str:
        if not self.api or not self.token:
            raise BrainUnavailable("not running under PuffPAI")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = json.dumps({"messages": messages, "images": []}).encode()
        req = urllib.request.Request(self.api + "/v1/brain/chat", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with self._open(req, timeout=300) as resp:
                data = json.loads(resp.read(MAX_BRAIN_ANSWER).decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise BrainUnavailable(str(exc)) from None
        text = data.get("text") if isinstance(data, dict) else None    # {"text", "usage"}
        if not isinstance(text, str) or not text.strip():
            raise BrainUnavailable("no text")
        return text


SYSTEM = ("You write one complete {language} file for the request. Answer with the code only, in one fenced "
          "code block. Put short comments in the code that say what it does. Do not explain outside the block.")


def code_from(answer: str) -> str:
    """The first fenced code block in the brain's answer, or the whole answer when it has none."""
    match = re.search(r"```[^\n`]*\n(.*?)```", answer, re.S)
    code = match.group(1) if match else answer
    return code.strip("\n") + "\n"


# ------------------------------------------------------------- the logic

class CodeWriter:
    def __init__(self, folder: Folder, brain: Brain) -> None:
        self.folder = folder
        self.brain = brain

    def write_script(self, request: str, language: str = "python", name: str = "") -> str:
        request = _text(request, "request", 4000)
        if not request:
            raise ValueError("say what the script should do")
        language = _language(language)
        stem = clean_name(_text(name, "name", 100) or " ".join(request.split()[:6]))
        try:
            answer = self.brain.chat(request, SYSTEM.format(language=language))
        except BrainUnavailable:
            return BRAIN_AWAY
        path = self.folder.save(stem, LANGUAGES[language], code_from(answer))
        lines = path.read_text(encoding="utf-8").splitlines()
        head = "\n".join(lines[:FIRST_LINES]) + ("\n..." if len(lines) > FIRST_LINES else "")
        return (f"Saved {path.name} ({len(lines)} lines) at {path}. {NOT_RUN}\n"
                f"The first lines:\n{head}")

    def list_scripts(self) -> str:
        found = self.folder.scripts()
        if not found:
            return "No scripts written yet."
        rows = [f"{p.name} ({max(1, p.stat().st_size // 1024)} KB, "
                f"{datetime.fromtimestamp(p.stat().st_mtime).isoformat(sep=' ', timespec='minutes')})"
                for p in found[:100]]
        return f"{len(found)} script{'' if len(found) == 1 else 's'}, newest first:\n" + "\n".join(rows)

    def read_script(self, name: str) -> str:
        path = self.folder.find(name)
        text = path.read_bytes()[:MAX_FILE].decode("utf-8", "replace")
        return f"{path.name} (not run):\n{text}"

    def delete_script(self, name: str) -> str:
        with self.folder.lock:
            path = self.folder.find(name)
            path.unlink()
        return f"Deleted {path.name}."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("write_script", "Have the brain write a script or small program from a plain request and save it as a file. "
     "It is never run. Answers the file path and the first lines.",
     {"request": _p("string", "What the script should do, in the owner's own words."),
      "language": _p("string", "python, powershell, bash, javascript or html. Default python."),
      "name": _p("string", "Optional: a short file name for it.")}, ["request"]),
    ("list_scripts", "List the scripts written so far, newest first.", {}, []),
    ("read_script", "Show the whole text of one saved script.",
     {"name": _p("string", "The script's file name, like backup-photos.py.")}, ["name"]),
    ("delete_script", "Delete one saved script.",
     {"name": _p("string", "The script's file name, like backup-photos.py.")}, ["name"]),
]
CHANGES = ["write_script", "delete_script"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: CodeWriter, msg: dict) -> dict | None:
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
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Code writer does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "CodeWriter/1"
    app: CodeWriter
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
        self._json(401, {"error": "Code writer needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Code writer stopped on this request with an error ({type(exc).__name__})."})
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
        self._json(404, {"error": "Code writer has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Code writer has no page here."})
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


def make_server(port: int, data: Path | str, token: str | None, api: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = CodeWriter(Folder(data), Brain(api, token))
    handler = type("CodeWriterHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Code writer for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Code writer with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"), os.environ.get("PUFFPAI_API"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
