"""Network devices' own server: every tool, the token, and a bad input. The neighbour-table command and
the name lookups are replaced with canned answers (Windows, Mac and Linux shapes); these tests never run
arp, never ask a name server and never touch the network. Each server listens on a free port on 127.0.0.1
and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "n" * 20

_spec = importlib.util.spec_from_file_location("network_devices_server", PKG / "server.py")
nd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nd)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

WINDOWS = """
Interface: 192.168.1.10 --- 0x4
  Internet Address      Physical Address      Type
  192.168.1.1           50-c7-bf-11-22-33     dynamic
  192.168.1.23          b8-27-eb-aa-bb-cc     dynamic
  192.168.1.40          da-11-22-33-44-55     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
"""
MAC = """? (192.168.1.1) at 50:c7:bf:11:22:33 on en0 ifscope [ethernet]
? (192.168.1.30) at 0:17:88:1:2:3 on en0 ifscope [ethernet]
? (192.168.1.31) at (incomplete) on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""
LINUX = """192.168.1.1 dev wlan0 lladdr 50:c7:bf:11:22:33 REACHABLE
192.168.1.50 dev wlan0 lladdr 00:0e:58:de:ad:01 STALE
192.168.1.51 dev wlan0  FAILED
fe80::1 dev wlan0 lladdr 50:c7:bf:11:22:33 router STALE
"""


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
def table(monkeypatch):
    """The command and the name lookup, replaced. Set table['out'] to the canned output."""
    state = {"out": WINDOWS, "argv": []}

    def fake_run(argv, **kw):
        assert kw.get("shell") is False and isinstance(argv, list)
        state["argv"].append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=state["out"].encode(), stderr=b"")

    def fake_name(address):
        if address == "192.168.1.1":
            return ("router.lan", [], [address])
        raise socket.herror(1, "unknown host")

    monkeypatch.setattr(nd.subprocess, "run", fake_run)
    monkeypatch.setattr(nd.socket, "gethostbyaddr", fake_name)
    monkeypatch.setattr(nd, "_find", lambda program: program)
    return state


def call(url, body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET", headers=h)
    try:
        with _open(req, timeout=10) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def tool(base, name, **args):
    code, out = call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
    assert code == 200, out
    return out["result"]["content"][0]["text"], out["result"]["isError"]


@pytest.mark.parametrize("platform,out,argv0,want,missing", [
    ("win32", WINDOWS, "arp", {"50:c7:bf:11:22:33", "b8:27:eb:aa:bb:cc", "da:11:22:33:44:55"}, "ff:ff:ff:ff:ff:ff"),
    ("darwin", MAC, "arp", {"50:c7:bf:11:22:33", "00:17:88:01:02:03"}, "01:00:5e:00:00:fb"),
    ("linux", LINUX, "ip", {"50:c7:bf:11:22:33", "00:0e:58:de:ad:01"}, "192.168.1.51"),
])
def test_scan_reads_each_table_shape(tmp_path, table, platform, out, argv0, want, missing):
    table["out"] = out
    with running(nd.make_server(0, tmp_path, TOKEN, platform=platform)) as base:
        text, err = tool(base, "scan")
    assert not err and "No new ones" not in text, text
    assert table["argv"][0][0] == argv0
    saved = json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))["devices"]
    assert set(saved) == want and missing not in text
    assert saved["50:c7:bf:11:22:33"]["vendor"] == "TP-Link"
    assert saved["50:c7:bf:11:22:33"]["name"] == "router.lan"
    if platform == "win32":
        assert saved["b8:27:eb:aa:bb:cc"]["vendor"] == "Raspberry Pi"
        assert saved["da:11:22:33:44:55"]["vendor"].startswith("private address")


def test_list_name_new_and_forget(tmp_path, table):
    with running(nd.make_server(0, tmp_path, TOKEN, platform="win32")) as base:
        assert "No scan yet" in tool(base, "list_devices")[0]
        first, _ = tool(base, "new_devices")
        assert "first time you asked" in first and "3 devices" in first
        text, err = tool(base, "name_device", device="192.168.1.23", label="the kitchen tablet")
        assert not err and "the kitchen tablet" in text
        listed, _ = tool(base, "list_devices", filter="tablet")
        assert "the kitchen tablet" in listed and "b8:27:eb:aa:bb:cc" in listed and "first seen" in listed
        assert "No new devices" in tool(base, "new_devices")[0]
        table["out"] = WINDOWS + "  192.168.1.77          00-17-88-0a-0b-0c     dynamic\n"
        fresh, _ = tool(base, "new_devices")
        assert "1 new device " in fresh and "192.168.1.77" in fresh and "Philips Hue" in fresh
        text, err = tool(base, "forget_device", device="the kitchen tablet")
        assert not err and "Forgot" in text
        assert "the kitchen tablet" not in tool(base, "list_devices")[0]


def test_missing_program_is_a_plain_message(tmp_path, table, monkeypatch):
    monkeypatch.setattr(nd, "_find", lambda program: None)
    with running(nd.make_server(0, tmp_path, TOKEN, platform="win32")) as base:
        text, err = tool(base, "scan")
    assert "no arp or ip program" in text and not (tmp_path / "devices.json").exists()


def test_bad_input_is_refused(tmp_path, table):
    with running(nd.make_server(0, tmp_path, TOKEN, platform="win32")) as base:
        tool(base, "scan")
        text, err = tool(base, "name_device", device="192.168.1.23", label="x" * 61)
        assert err and "at most 60" in text
        text, err = tool(base, "forget_device", device="no such thing")
        assert err and "no device" in text


def test_every_route_needs_the_token(tmp_path, table):
    with running(nd.make_server(0, tmp_path, TOKEN, platform="win32")) as base:
        assert call(base + "/health", token=None)[0] == 401
        assert call(base + "/health", token="wrong")[0] == 401
        assert call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
        code, out = call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert code == 200 and {t["name"] for t in out["result"]["tools"]} == {
        "scan", "list_devices", "new_devices", "name_device", "forget_device"}
    assert table["argv"] == []
