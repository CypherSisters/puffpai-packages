"""Node-RED flows' own server (node-red-flows/server.py): the address check, the six tools against a fake
Node-RED, the token on every route, and the scrubbed flows file.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it;
nothing is left running and nothing touches the internet."""

import contextlib
import importlib.util
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20
NR_SECRET = "nr-admin-" + "s" * 24

_spec = importlib.util.spec_from_file_location("node_red_flows_server", PKG / "server.py")
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


# ------------------------------------------------------------ a fake Node-RED

def fake_flows():
    return [
        {"id": "aaa111", "type": "tab", "label": "daily_news", "disabled": False, "info": ""},
        {"id": "bbb222", "type": "tab", "label": "block_device", "disabled": True, "info": ""},
        {"id": "n1", "type": "http in", "z": "aaa111", "url": "/webhook/daily-news", "method": "post"},
        {"id": "n2", "type": "http in", "z": "bbb222", "url": "/webhook/block-device", "method": "post"},
        {"id": "n3", "type": "function", "z": "aaa111", "func": "return msg;"},
        {"id": "ccc333", "type": "tab", "label": "leaky", "disabled": False, "info": ""},
        {"id": "n4", "type": "http in", "z": "ccc333", "url": "/webhook/leaky", "method": "get"},
    ]


class FakeNodeRed(BaseHTTPRequestHandler):
    state: dict

    def log_message(self, *a):
        pass

    def _reply(self, code, obj, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _admin_ok(self):
        self.state["seen"].append((self.command, self.path, self.headers.get("Authorization")))
        if self.state.get("need_login") and self.headers.get("Authorization") != "Bearer " + NR_SECRET:
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/webhook/leaky":           # a broken flow that echoes the admin login back
            return self._reply(500, ("upstream said: Authorization: Bearer " + NR_SECRET).encode(), "text/plain")
        if self.path.startswith("/webhook/"):
            self.state["seen"].append((self.command, self.path, self.headers.get("Authorization")))
            return self._reply(200, {"ran": self.path})
        if self.path == "/big":
            return self._reply(200, b"x" * (nr.MAX_ANSWER + 10), "text/plain")
        if not self._admin_ok():
            return
        if self.path == "/flows":
            return self._reply(200, self.state["flows"])
        if self.path == "/settings":
            return self._reply(200, {"version": "4.0.2", "httpNodeRoot": "/"})
        if self.path.startswith("/flow/"):
            fid = self.path[len("/flow/"):]
            tab = next((n for n in self.state["flows"] if n["id"] == fid and n["type"] == "tab"), None)
            if tab is None:
                return self._reply(404, {"error": "not found"})
            nodes = [n for n in self.state["flows"] if n.get("z") == fid]
            return self._reply(200, {**tab, "nodes": nodes})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/webhook/"):
            self.state["seen"].append((self.command, self.path, self.headers.get("Authorization")))
            return self._reply(200, {"ran": self.path, "got": self._body()})
        self._reply(404, {"error": "not found"})

    def do_PUT(self):
        if not self._admin_ok():
            return
        fid = self.path[len("/flow/"):]
        body = self._body()
        self.state["puts"].append((fid, body))
        for n in self.state["flows"]:
            if n["id"] == fid and n["type"] == "tab":
                n["disabled"] = body["disabled"]
        self._reply(200, {"id": fid})


@pytest.fixture
def fake():
    state = {"flows": fake_flows(), "seen": [], "puts": []}
    handler = type("Fake", (FakeNodeRed,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    with running(server) as port:
        state["address"] = f"http://127.0.0.1:{port}"
        yield state


@pytest.fixture
def base(tmp_path, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", nr.KEY_ENV):
        monkeypatch.delenv(k, raising=False)
    data = tmp_path / "data" / "node-red-flows"
    with running(nr.make_server(0, data, TOKEN)) as port:
        yield f"http://127.0.0.1:{port}", data


def call(url, method="GET", body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method, headers=h)
    try:
        with _open(req, timeout=20) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def tool(url, name, ok=True, **args):
    code, out = call(url + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": args}})
    assert code == 200 and out["result"]["isError"] is (not ok), out
    return out["result"]["content"][0]["text"]


# ------------------------------------------------------------ the server

def test_every_route_needs_the_token_and_tools_are_declared(base):
    url, _ = base
    assert call(url + "/health", token=None)[0] == 401
    assert call(url + "/health", token="wrong")[0] == 401
    assert call(url + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
    assert call(url + "/health") == (200, "ok")
    code, out = call(url + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in out["result"]["tools"]}
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert names == set(manifest["abilities"]["tools"]) == {t[0] for t in nr.TOOLS}
    assert set(manifest["abilities"]["changes"]) == set(nr.CHANGES)
    assert manifest["needs"]["hosts"] == []


def test_set_address_keeps_home_addresses_and_refuses_the_rest(base, monkeypatch):
    url, data = base
    assert "http://192.168.1.20:1880" in tool(url, "set_address", address="192.168.1.20")
    assert json.loads((data / "nodered.json").read_text())["address"] == "http://192.168.1.20:1880"
    for good, want in [("https://nodered.local", "https://nodered.local:1880"), ("http://box.lan:1990/admin/", "http://box.lan:1990/admin"),
                       ("http://[::1]:1880", "http://[::1]:1880"), ("10.0.0.5:1880", "http://10.0.0.5:1880")]:
        assert nr.check_address(good) == want
    for bad in ["http://8.8.8.8:1880", "http://example.com:1880", "http://nodered.example.lan.com",
                "ftp://192.168.1.2", "http://user:pw@192.168.1.2", "http://192.168.1.2/?x=1", "http://100.64.0.1",
                "http://0.0.0.0:1880", "http://[2001:db8::1]:1880", "http://192.168.1.2:99999", ""]:
        text = tool(url, "set_address", ok=False, address=bad)
        assert "Those arguments did not fit" in text, bad
    real = nr.socket.getaddrinfo
    monkeypatch.setattr(nr.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 1880))]
                        if host == "evil.lan" else real(host, *a, **k))
    text = tool(url, "set_address", ok=False, address="http://evil.lan:1880")
    assert text == ("The name evil.lan pointed at an address outside your home network, so it was not used. "
                    "Nothing was saved.")
    assert json.loads((data / "nodered.json").read_text())["address"] == "http://192.168.1.20:1880"


def test_a_lan_name_that_looks_up_outside_the_home_is_refused(monkeypatch):
    monkeypatch.setattr(nr.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 1880))])
    with pytest.raises(nr.Refused, match="outside your home network"):
        nr.fetch("http://evil.lan:1880", "GET", "/settings")


def test_status_list_enable_disable(base, fake):
    url, _ = base
    tool(url, "set_address", address=fake["address"])
    assert tool(url, "node_red_status") == f"Node-RED 4.0.2 is running at {fake['address']}."
    listed = tool(url, "list_flows")
    assert "3 flows" in listed and "- daily_news (id aaa111, on): POST /webhook/daily-news" in listed
    assert "- block_device (id bbb222, off): POST /webhook/block-device" in listed
    assert tool(url, "enable_flow", flow="block_device") == "Turned the flow 'block_device' on."
    fid, sent = fake["puts"][-1]
    assert fid == "bbb222" and sent["disabled"] is False and [n["id"] for n in sent["nodes"]] == ["n2"]
    assert tool(url, "disable_flow", flow="aaa111") == "Turned the flow 'daily_news' off."
    assert tool(url, "disable_flow", flow="daily_news") == "The flow 'daily_news' is already off."
    assert "no flow named" in tool(url, "enable_flow", ok=False, flow="nope")


def test_run_flow_calls_only_the_flows_own_paths(base, fake):
    url, _ = base
    tool(url, "set_address", address=fake["address"])
    out = tool(url, "run_flow", path="/webhook/daily-news", body={"n": 1})
    assert out.startswith("Ran daily_news (POST /webhook/daily-news): HTTP 200.") and '"n": 1' in out
    assert ("POST", "/webhook/daily-news", None) in fake["seen"]
    before = len(fake["seen"])
    refused = tool(url, "run_flow", ok=False, path="/admin/flows")
    assert "is not one of the web paths your Node-RED flows define" in refused
    assert "turned off" in tool(url, "run_flow", ok=False, path="/webhook/block-device")
    assert all(p.startswith(("/flows", "/settings")) for _, p, _ in fake["seen"][before:])   # no call to a refused path
    assert "did not fit" in tool(url, "run_flow", ok=False, path="/webhook/daily-news", body="not json")
    assert "did not fit" in tool(url, "run_flow", ok=False, path="/webhook/daily-news", method="DELETE")


def test_the_admin_login_comes_from_the_environment_only(base, fake, monkeypatch):
    url, data = base
    fake["need_login"] = True
    tool(url, "set_address", address=fake["address"])
    assert "asked for a login" in tool(url, "node_red_status") and NR_SECRET not in tool(url, "node_red_status")
    monkeypatch.setenv(nr.KEY_ENV, NR_SECRET)
    assert "4.0.2" in tool(url, "node_red_status")
    out = tool(url, "run_flow", path="/webhook/daily-news")
    assert NR_SECRET not in out and NR_SECRET not in (data / "nodered.json").read_text()
    flow_calls = [auth for _, p, auth in fake["seen"] if p.startswith("/webhook/")]
    assert flow_calls == [None]                  # the admin login never goes to a flow


def test_a_flow_answer_never_shows_the_admin_login(base, fake, monkeypatch):
    url, _ = base
    monkeypatch.setenv(nr.KEY_ENV, NR_SECRET)
    tool(url, "set_address", address=fake["address"])
    out = tool(url, "run_flow", path="/webhook/leaky")
    assert "HTTP 500" in out and "[hidden]" in out and NR_SECRET not in out


def test_big_answers_are_not_read(fake):
    with pytest.raises(nr.Refused, match="bigger than 1 MB"):
        nr.fetch(fake["address"], "GET", "/big")


# ------------------------------------------------------------ the flows file

GREP = ["192.168", "10.", "172.16", "@", "token", "key", "password", "Bearer"]


def test_the_scrubbed_flows_parse_and_hold_nothing_from_the_house():
    raw = (PKG / "flows" / "homebot-flows.json").read_text(encoding="utf-8")
    for word in GREP:
        assert word not in raw, word
    # the only addresses left are this computer and the broadcast address; the only name is the HA default
    assert set(re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", raw)) <= {"127.0.0.1", "255.255.255.255"}
    assert set(re.findall(r"[A-Za-z0-9-]+\.(?:local|lan)", raw)) <= {"homeassistant.local"}
    assert not re.search(r"-?\d{1,3}\.\d{4,}", raw)            # no coordinates
    flows = json.loads(raw)
    tabs = {n["id"]: n for n in flows if n["type"] == "tab"}
    assert sorted(t["label"] for t in tabs.values()) == sorted([
        "calendar_sync", "daily_briefing", "daily_news", "daily_weather", "grocery_list", "health_check",
        "meal_planner", "media_now_playing", "network_scan", "block_device", "weather_alerts", "good_morning",
        "leaving_home", "security_check"])
    assert {t["label"] for t in tabs.values() if t["disabled"]} == {
        "calendar_sync", "grocery_list", "meal_planner", "network_scan", "block_device"}
    ids = {n["id"] for n in flows}
    for n in flows:
        assert "credentials" not in n
        if n["type"] != "tab":
            assert n["z"] in tabs
            assert all(w in ids for out in n.get("wires", []) for w in out)
