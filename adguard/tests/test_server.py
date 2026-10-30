"""AdGuard Home's own server: every tool shape against a fake AdGuard Home in a thread, a refused
public address, the token, a bad input, and the password never in a reply. Nothing here touches the
internet or a real AdGuard Home; both servers listen on free ports on 127.0.0.1 and are stopped by
the test that started them."""

import base64
import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "a" * 20
USER, PASSWORD = "owner", "not-a-real-password-9137"

_spec = importlib.util.spec_from_file_location("adguard_server", PKG / "server.py")
ag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ag)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
OWNER_RULES = ["||owner-blocked.example^", "! a note the owner wrote"]


class FakeAdGuard(BaseHTTPRequestHandler):
    """Just enough of AdGuard Home's /control API, with HTTP Basic like the real one."""
    state: dict

    def log_message(self, *a):
        pass

    def _answer(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b"OK"
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if obj is not None else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self._answer(401, {"message": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self._authed():
            return
        s, path = self.state, self.path
        s["paths"].append(path)
        if path == "/control/status":
            return self._answer(200, {"version": "v0.107.99", "protection_enabled": s["protection"]})
        if path == "/control/stats":
            return self._answer(200, {"num_dns_queries": 1000, "num_blocked_filtering": 125,
                                      "top_blocked_domains": [{"ads.example.com": 90}, {"track.example.net": 35}],
                                      "top_clients": [{"192.168.1.40": 600}]})
        if path == "/control/filtering/status":
            return self._answer(200, {"filters": [{"name": "AdGuard DNS filter", "enabled": True, "rules_count": 5000}],
                                      "user_rules": list(s["rules"])})
        if path.startswith("/control/querylog?"):
            s["querylog"] = path
            return self._answer(200, {"data": [
                {"time": "2026-10-30T20:01:02.5-04:00", "client": "192.168.1.40",
                 "question": {"name": "ads.example.com", "type": "A"}, "reason": "FilteredBlackList"},
                {"time": "2026-10-30T20:01:01.5-04:00", "client": "192.168.1.40",
                 "question": {"name": "example.org", "type": "A"}, "reason": "NotFilteredNotFound"}]})
        if path == "/control/clients":
            return self._answer(200, {"clients": [{"name": "Living room TV", "ids": ["192.168.1.40"]}],
                                      "auto_clients": [{"ip": "192.168.1.41", "name": "phone.lan", "source": "rDNS"}]})
        self._answer(404, {"message": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.state["posts"].append((self.path, body))
        if self.path == "/control/filtering/set_rules":
            self.state["rules"] = body["rules"]
        elif self.path == "/control/protection":
            self.state["protection"] = body["enabled"]
        else:
            return self._answer(404, {"message": "not found"})
        self._answer(200)


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
def fake():
    state = {"rules": list(OWNER_RULES), "protection": True, "paths": [], "posts": []}
    handler = type("Fake", (FakeAdGuard,), {"state": state})
    with running(ThreadingHTTPServer(("127.0.0.1", 0), handler)) as url:
        state["url"] = url
        yield state


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("ADGUARD_USER", USER)
    monkeypatch.setenv("ADGUARD_PASSWORD", PASSWORD)
    with running(ag.make_server(0, tmp_path, TOKEN)) as url:
        yield url


REPLIES = []


def call(url, body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET", headers=h)
    try:
        with _open(req, timeout=15) as r:
            text = r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    REPLIES.append(text)
    return 200, (json.loads(text) if text.startswith("{") else text)


def tool(base, name, **args):
    code, out = call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
    assert code == 200, out
    return out["result"]["content"][0]["text"], out["result"]["isError"]


def test_read_tools(base, fake, tmp_path):
    text, err = tool(base, "set_address", address=fake["url"])
    assert not err and "v0.107.99 answered" in text
    assert json.loads((tmp_path / "adguard.json").read_text(encoding="utf-8"))["address"] == fake["url"]
    text, _ = tool(base, "status")
    assert "protection is ON" in text and "1000 DNS lookups, 125 blocked (12%)" in text and "Your own rules: 1" in text
    text, _ = tool(base, "top_blocked", count=1)
    assert "ads.example.com: 90" in text and "track.example.net" not in text and "192.168.1.40: 600" in text
    text, _ = tool(base, "query_log", count=5, client="192.168.1.40")
    assert "ads.example.com blocked" in text and "example.org allowed" in text
    assert "search=192.168.1.40" in fake["querylog"] and "limit=5" in fake["querylog"]
    text, _ = tool(base, "list_clients")
    assert "Living room TV: 192.168.1.40" in text and "192.168.1.41 (phone.lan)" in text


def test_change_tools_keep_the_owners_rules(base, fake):
    tool(base, "set_address", address=fake["url"])
    text, err = tool(base, "block_domain", domain="https://Ads.Tracker.example/path")
    assert not err and "||ads.tracker.example^" in text
    assert fake["rules"] == OWNER_RULES + ["# puffpai", "||ads.tracker.example^"]
    text, _ = tool(base, "unblock_domain", domain="ads.tracker.example")
    assert "Took off the rule PuffPAI added" in text
    assert fake["rules"] == OWNER_RULES
    text, _ = tool(base, "unblock_domain", domain="owner-blocked.example")
    assert "your own rule" in text
    assert fake["rules"] == OWNER_RULES + ["# puffpai", "@@||owner-blocked.example^"]
    text, _ = tool(base, "block_domain", domain="owner-blocked.example")
    assert "Took off the allow rule PuffPAI added" in text and fake["rules"] == OWNER_RULES
    assert "OFF" in tool(base, "protection", on="off")[0] and fake["protection"] is False
    assert "ON" in tool(base, "protection", on="on")[0] and fake["protection"] is True
    text, _ = tool(base, "pause_protection", minutes=15)
    assert "15 minutes" in text and fake["posts"][-1] == ("/control/protection", {"enabled": False, "duration": 900000})


@pytest.fixture
def names(monkeypatch):
    """evil.lan points outside the house; nothing.lan is not found. Every other name is looked up as usual."""
    real = ag.socket.getaddrinfo

    def fake(host, *a, **kw):
        if host == "evil.lan":
            return [(ag.socket.AF_INET, ag.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host == "nothing.lan":
            raise ag.socket.gaierror(11001, "not found")
        return real(host, *a, **kw)

    monkeypatch.setattr(ag.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("address", ["http://8.8.8.8:3000", "https://example.com", "http://100.64.0.1",
                                     "http://user:pw@192.168.1.2:3000", "ftp://192.168.1.2", "http://192.168.1.2/x",
                                     "http://evil.lan:3000"])
def test_refuses_an_address_off_the_home_network(base, tmp_path, names, address):
    text, err = tool(base, "set_address", address=address)
    assert err and not (tmp_path / "adguard.json").exists(), text
    if "evil.lan" in address:
        assert "outside this computer and the home network, so it was not saved" in text


def test_a_name_not_found_yet_is_saved_and_says_so(base, tmp_path, names):
    text, err = tool(base, "set_address", address="http://nothing.lan:3000")
    assert not err and "Not checked yet" in text and "could not be found" in text
    assert json.loads((tmp_path / "adguard.json").read_text(encoding="utf-8"))["address"] == "http://nothing.lan:3000"


def test_bad_input_and_missing_address(base, fake):
    text, err = tool(base, "status")
    assert err and "No AdGuard Home address yet" in text
    tool(base, "set_address", address=fake["url"])
    text, err = tool(base, "block_domain", domain="not a domain!")
    assert err and "not a domain name" in text
    text, err = tool(base, "pause_protection", minutes=5000)
    assert err and "1 to 1440" in text


def test_wrong_login_and_the_password_never_in_a_reply(base, fake, monkeypatch):
    tool(base, "set_address", address=fake["url"])
    for name, args in [("status", {}), ("top_blocked", {}), ("query_log", {}), ("list_clients", {}),
                       ("block_domain", {"domain": "x.example"}), ("unblock_domain", {"domain": "x.example"}),
                       ("protection", {"on": "on"}), ("pause_protection", {"minutes": 1})]:
        assert not tool(base, name, **args)[1], name
    monkeypatch.setenv("ADGUARD_PASSWORD", "wrong-" + PASSWORD)
    text, err = tool(base, "status")
    assert err and "did not accept the login" in text
    login = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert REPLIES and not any(PASSWORD in r or login in r for r in REPLIES)


def test_every_route_needs_the_token(base, fake):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")[0] == 401
    code, out = call(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert code == 200 and len(out["result"]["tools"]) == 9
    assert fake["paths"] == [] and fake["posts"] == []
