"""Code writer's own server (code-writer/server.py): scripts from the brain (stubbed here),
saved in made/ only, never run, and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it.
The brain is replaced by a stub; nothing here reaches a network."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("code_writer_server", PKG / "server.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

ANSWER = "Here you go:\n```python\n# says hello\nprint('hello')\n```\nEnjoy."


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
    return tmp_path / "data" / "code-writer"


@pytest.fixture
def asked():
    return []


@pytest.fixture
def base(data, asked, monkeypatch):
    def fake_chat(self, prompt, system=""):
        asked.append((prompt, system))
        if prompt == "away":
            raise cw.BrainUnavailable("down")
        if prompt == "huge":
            return "x" * (cw.MAX_FILE + 1)
        return ANSWER
    monkeypatch.setattr(cw.Brain, "chat", fake_chat)
    with running(cw.make_server(0, data, TOKEN, "http://127.0.0.1:9")) as url:
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
    names = {t["name"] for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert names == set(manifest["abilities"]["tools"]) == set(manifest["abilities"]["examples"])
    assert set(manifest["abilities"]["changes"]) == set(cw.CHANGES) <= names


def test_write_list_read_delete(base, data, asked):
    said = tool(base, "write_script", request="Say hello, please!", language="py")
    assert "Saved say-hello-please.py" in said and "never do" in said and "print('hello')" in said
    assert "python" in asked[0][1] and asked[0][0] == "Say hello, please!"
    made = data / "made"
    assert (made / "say-hello-please.py").read_text(encoding="utf-8") == "# says hello\nprint('hello')\n"
    assert "say-hello-please-2.py" in tool(base, "write_script", request="Say hello, please!", language="python")
    assert "hello.ps1" in tool(base, "write_script", request="x", language="PowerShell", name="../Hello")

    listed = tool(base, "list_scripts")
    assert listed.startswith("3 scripts") and "hello.ps1" in listed
    assert "print('hello')" in tool(base, "read_script", name="say-hello-please")
    assert "Deleted say-hello-please.py" in tool(base, "delete_script", name="say-hello-please.py")
    assert sorted(p.name for p in made.iterdir()) == ["hello.ps1", "say-hello-please-2.py"]
    assert sorted(p.name for p in data.iterdir()) == ["made"]


def test_brain_away_is_one_sentence(base, data):
    assert tool(base, "write_script", request="away") == cw.BRAIN_AWAY
    assert list((data / "made").iterdir()) == []


def test_bad_input_is_refused(base, data, tmp_path):
    (tmp_path / "outside.py").write_text("keep", encoding="utf-8")
    assert "no folders" in tool_error(base, "read_script", name="../../outside.py")
    assert "no folders" in tool_error(base, "delete_script", name="..\\..\\outside.py")
    assert (tmp_path / "outside.py").read_text(encoding="utf-8") == "keep"
    assert "language is one of" in tool_error(base, "write_script", request="x", language="cobol")
    assert "nothing was saved" in tool_error(base, "write_script", request="huge")
    assert "no script called" in tool_error(base, "read_script", name="missing")
    assert list((data / "made").iterdir()) == []
