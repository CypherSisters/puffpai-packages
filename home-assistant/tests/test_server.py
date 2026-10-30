"""Home Assistant's own server (home-assistant/server.py): the address check, every tool shape against a
fake Home Assistant, the token guard, and that the Home Assistant token never reaches an answer or a file.

The fake Home Assistant is an http.server thread on 127.0.0.1 that records every call. Every server here
listens on a free port on 127.0.0.1 and is stopped by the test that started it; nothing touches the
internet."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "pkg-token-" + "p" * 20
HA_TOKEN = "ha.test.token." + "h" * 40

_spec = importlib.util.spec_from_file_location("home_assistant_server", PKG / "server.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _st(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs, "last_changed": _ago(300)}


def fake_states():
    motion = _st("binary_sensor.hall_motion", "on", friendly_name="Hall motion", device_class="motion")
    motion["last_changed"] = _ago(5)
    return [
        _st("light.kitchen", "on", friendly_name="Kitchen light", brightness=128),
        _st("light.porch", "off", friendly_name="Porch light"),
        _st("switch.fan_plug", "off", friendly_name="Fan plug"),
        _st("media_player.tv", "off", friendly_name="TV"),
        _st("cover.bedroom_blind", "closed", friendly_name="Bedroom blind", device_class="blind"),
        _st("cover.garage_door", "closed", friendly_name="Garage door", device_class="garage"),
        _st("lock.front_door", "locked", friendly_name="Front door"),
        _st("climate.hallway", "heat", friendly_name="Hallway", min_temp=7, max_temp=35,
            hvac_modes=["off", "heat"], temperature=20, current_temperature=19.5),
        _st("scene.movie_night", "unknown", friendly_name="Movie night", entity_id=["light.kitchen", "light.porch"]),
        _st("scene.leaving", "unknown", friendly_name="Leaving", entity_id=["light.kitchen", "lock.front_door"]),
        _st("camera.yard", "idle", friendly_name="Yard camera", access_token="camsecret123",
            entity_picture="/api/camera_proxy/camera.yard?token=camsecret123"),
        motion,
        _st("binary_sensor.garden_motion", "off", friendly_name="Garden motion", device_class="motion"),
        _st("sensor.outdoor_temp", "12.5", friendly_name="Outdoor temperature", unit_of_measurement="C"),
    ]


class FakeHA(BaseHTTPRequestHandler):
    calls: list
    mode: dict

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None, headers=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        self.calls.append((method, self.path, body, self.headers.get("Authorization")))
        if self.mode.get("redirect"):
            return self._reply(302, headers={"Location": "http://192.168.250.250:9/api/states"})
        if self.mode.get("status"):
            return self._reply(self.mode["status"], {"message": "echo " + HA_TOKEN})
        states = {s["entity_id"]: s for s in fake_states()}
        if method == "GET" and self.path == "/api/states":
            return self._reply(200, list(states.values()))
        if method == "GET" and self.path.startswith("/api/states/"):
            got = states.get(self.path.rsplit("/", 1)[1])
            return self._reply(200, got) if got else self._reply(404, {"message": "Entity not found."})
        if method == "POST" and self.path.startswith("/api/services/"):
            return self._reply(200, [])
        self._reply(404, {"message": "no"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


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


@pytest.fixture
def ha():
    """The fake Home Assistant: (port, calls, mode)."""
    calls, mode = [], {}
    handler = type("Fake", (FakeHA,), {"calls": calls, "mode": mode})
    with running(ThreadingHTTPServer(("127.0.0.1", 0), handler)) as port:
        yield port, calls, mode


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    """The package server, with no proxy between it and the fake (the test machine's own proxy is not used)."""
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", HA_TOKEN)
    server = hs.make_server(0, tmp_path / "data", TOKEN)
    server.app.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), hs._SameOrigin())
    with running(server) as port:
        yield f"http://127.0.0.1:{port}", tmp_path / "data"


def post(url, body, token=TOKEN):
    req = urllib.request.Request(url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with _open(req, timeout=10) as r:
        return json.loads(r.read())


def tool(url, tool_name, /, **args):
    got = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": args}})
    res = got["result"]
    return res["content"][0]["text"], res["isError"]


@pytest.fixture
def ready(pkg, ha):
    url, data = pkg
    port, calls, mode = ha
    text, err = tool(url, "set_address", address=f"127.0.0.1:{port}")
    assert not err and "Bridge must allow" in text
    return url, data, calls, mode


def services(calls):
    return [(c[1], c[2]) for c in calls if c[0] == "POST"]


# ---------------------------------------------------------------- the guard and the list

def test_every_route_needs_the_token(pkg):
    url, _ = pkg
    for path, data in (("/health", None), ("/mcp", b"{}")):
        req = urllib.request.Request(url + path, data=data, method="POST" if data else "GET")
        with pytest.raises(urllib.error.HTTPError) as e:
            _open(req, timeout=5)
        assert e.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong-" + "w" * 20)
    assert e.value.code == 401
    req = urllib.request.Request(url + "/health", headers={"X-PuffPAI-Key": TOKEN})
    with _open(req, timeout=5) as r:
        assert r.read() == b"ok"


def test_tools_match_the_manifest(pkg):
    url, _ = pkg
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(manifest["abilities"]["tools"]) == set(manifest["abilities"]["examples"])
    changing = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
    assert changing == set(manifest["abilities"]["changes"]) == set(hs.CHANGES)
    assert manifest["needs"]["hosts"] == []
    assert [k["name"] for k in manifest["needs"]["keys"]] == ["HOME_ASSISTANT_TOKEN"]


# ---------------------------------------------------------------- the address

@pytest.mark.parametrize("bad", ["8.8.8.8", "example.com", "http://homeassistant.com:8123", "ftp://192.168.1.2",
                                 "http://user:pw@192.168.1.5", "172.32.0.1", "100.64.0.1", "10.0.0.1:99999",
                                 "homeassistant.local.example.com", "", "192.168.1.2 evil"])
def test_a_public_or_odd_address_is_refused(pkg, bad):
    url, data = pkg
    text, err = tool(url, "set_address", address=bad)
    assert err
    assert not (data / "ha.json").exists()


def test_a_name_that_resolves_outside_is_refused_at_set_address(pkg, monkeypatch):
    url, data = pkg
    real = hs.socket.getaddrinfo

    def lookup(host, *a, **k):                       # only the test's own names are faked
        if host == "evil.lan":
            return [(2, 1, 6, "", ("93.184.216.34", 8123))]
        if host == "missing.lan":
            raise OSError("no such name")
        return real(host, *a, **k)
    monkeypatch.setattr(hs.socket, "getaddrinfo", lookup)
    text, err = tool(url, "set_address", address="http://evil.lan:8123")
    assert err and "outside your home network" in text and "Saved" not in text
    assert not (data / "ha.json").exists()
    text, err = tool(url, "set_address", address="missing.lan")
    assert not err and "Saved" in text and "could not find missing.lan" in text
    assert json.loads((data / "ha.json").read_text(encoding="utf-8"))["url"] == "http://missing.lan:8123"


def test_a_home_address_is_saved_without_the_token(pkg, monkeypatch):
    url, data = pkg
    real = hs.socket.getaddrinfo

    def lookup(host, *a, **k):                       # only the test's own names are faked
        if host in ("homeassistant.local", "ha.lan"):
            return [(2, 1, 6, "", ("192.168.1.20", 8123))]
        return real(host, *a, **k)
    monkeypatch.setattr(hs.socket, "getaddrinfo", lookup)
    for given, saved in (("homeassistant.local", "http://homeassistant.local:8123"),
                         ("https://ha.lan:443/lovelace/0", "https://ha.lan:443"),
                         ("192.168.1.20", "http://192.168.1.20:8123"),
                         ("http://[fe80::1]:8123", "http://[fe80::1]:8123")):
        text, err = tool(url, "set_address", address=given)
        assert not err and saved in text
        doc = json.loads((data / "ha.json").read_text(encoding="utf-8"))
        assert doc["url"] == saved and HA_TOKEN not in json.dumps(doc)


def test_a_name_that_points_outside_is_refused(pkg, ha, monkeypatch):
    url, data = pkg
    _, calls, _ = ha
    real = hs.socket.getaddrinfo

    def lookup(host, *a, **k):                       # only the test's own name is faked
        return [(2, 1, 6, "", ("8.8.8.8", 8123))] if host == "sneaky.lan" else real(host, *a, **k)
    monkeypatch.setattr(hs.socket, "getaddrinfo", lookup)
    hs.Store(data).save("http://sneaky.lan:8123")    # saved earlier, and the name has moved outside since
    text, err = tool(url, "list_devices")
    assert err and "outside your home network" in text
    assert calls == []


def test_no_address_yet_says_so(pkg):
    url, _ = pkg
    text, err = tool(url, "list_devices")
    assert err and "not set yet" in text


# ---------------------------------------------------------------- reading

def test_list_devices_and_device_state(ready):
    url, _, calls, _ = ready
    text, err = tool(url, "list_devices", domain="lights")
    assert not err and "Kitchen light (light.kitchen): on" in text and "Porch light" in text and "switch" not in text
    text, _ = tool(url, "list_devices", name="outdoor")
    assert "Outdoor temperature (sensor.outdoor_temp): 12.5 C" in text and "Found 1 device" in text
    text, err = tool(url, "device_state", entity="light.kitchen")
    assert not err and "is on" in text and "brightness: 50%" in text
    text, _ = tool(url, "device_state", entity="Front door")
    assert "locked" in text and "read-only here" in text
    text, _ = tool(url, "device_state", entity="camera.yard")
    assert "camsecret" not in text
    assert calls[0][3] == f"Bearer {HA_TOKEN}"
    text, err = tool(url, "list_devices", domain="toaster")
    assert err and "domain is all or one of" in text


def test_list_cameras_and_recent_motion(ready):
    url, _, _, _ = ready
    text, err = tool(url, "list_cameras")
    assert not err and "Yard camera (camera.yard): idle" in text and "camsecret" not in text
    text, err = tool(url, "recent_motion", minutes=30)
    assert not err and "Hall motion: on, changed 5 minutes ago" in text and "Garden" not in text
    text, err = tool(url, "recent_motion", minutes=0)
    assert err


# ---------------------------------------------------------------- changing

def test_turn_on_and_off(ready):
    url, _, calls, _ = ready
    text, err = tool(url, "turn_on", entity="kitchen light")
    assert not err and text == "Turned on Kitchen light."
    text, err = tool(url, "turn_off", entity="media_player.tv")
    assert not err and text == "Turned off TV."
    text, err = tool(url, "turn_on", entity="Bedroom blind")
    assert not err and text == "Opened Bedroom blind."
    assert services(calls) == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"}),
                               ("/api/services/media_player/turn_off", {"entity_id": "media_player.tv"}),
                               ("/api/services/cover/open_cover", {"entity_id": "cover.bedroom_blind"})]
    text, _ = tool(url, "turn_on", entity="cover.garage_door")
    assert "does not open or close it" in text
    text, err = tool(url, "turn_off", entity="lock.front_door")
    assert err and "never locks or unlocks" in text
    text, err = tool(url, "turn_on", entity="light")       # more than one fits: nothing happens
    assert err and "Say which one" in text
    assert len(services(calls)) == 3


def test_set_light(ready):
    url, _, calls, _ = ready
    text, err = tool(url, "set_light", entity="Kitchen light", brightness=30, color_temp="warm")
    assert not err and text == "Set Kitchen light to 30% brightness, 2700 K white."
    text, err = tool(url, "set_light", entity="light.porch", rgb="255,120,0")
    assert not err
    assert services(calls) == [
        ("/api/services/light/turn_on", {"brightness_pct": 30, "color_temp_kelvin": 2700, "entity_id": "light.kitchen"}),
        ("/api/services/light/turn_on", {"rgb_color": [255, 120, 0], "entity_id": "light.porch"})]
    for bad in ({"brightness": 150}, {"color": "red", "rgb": "1,2,3"}, {"color_temp": "purple"}, {}):
        text, err = tool(url, "set_light", entity="light.porch", **bad)
        assert err
    text, err = tool(url, "set_light", entity="switch.fan_plug", brightness=10)
    assert err
    assert len(services(calls)) == 2


def test_set_temperature(ready):
    url, _, calls, _ = ready
    text, err = tool(url, "set_temperature", entity="Hallway", temperature=21, hvac_mode="heat")
    assert not err and text == "Set Hallway to 21 degrees in heat mode."
    assert services(calls) == [("/api/services/climate/set_temperature",
                                {"entity_id": "climate.hallway", "temperature": 21, "hvac_mode": "heat"})]
    for bad in ({"temperature": 50}, {"temperature": 21, "hvac_mode": "cool"}, {"temperature": "warm"}):
        text, err = tool(url, "set_temperature", entity="climate.hallway", **bad)
        assert err
    assert len(services(calls)) == 1


def test_run_scene(ready):
    url, _, calls, _ = ready
    text, err = tool(url, "run_scene", scene="movie night")
    assert not err and text == "Ran the scene Movie night."
    text, _ = tool(url, "run_scene", scene="scene.leaving")
    assert "does not run it" in text
    assert services(calls) == [("/api/services/scene/turn_on", {"entity_id": "scene.movie_night"})]


# ---------------------------------------------------------------- the token and the edges

def test_the_token_is_never_in_a_reply(ready):
    url, data, calls, mode = ready
    texts = [tool(url, "list_devices")[0], tool(url, "device_state", entity="camera.yard")[0],
             tool(url, "turn_on", entity="light.kitchen")[0]]
    for status in (400, 401, 403, 500):
        mode["status"] = status                      # the fake echoes the token in its error body
        texts.append(tool(url, "turn_on", entity="light.kitchen")[0])
    assert all(HA_TOKEN not in t for t in texts)
    assert "refused the token" in texts[4] and "Bridge" in texts[5]
    assert HA_TOKEN not in (data / "ha.json").read_text(encoding="utf-8")
    assert all(c[3] == f"Bearer {HA_TOKEN}" for c in calls)


def test_a_redirect_to_another_host_is_not_followed(ready):
    url, _, calls, mode = ready
    mode["redirect"] = True
    text, err = tool(url, "list_devices")
    assert err and "did not follow it" in text
    assert len(calls) == 1


def test_a_bad_token_shape_is_refused_without_echo(ready, monkeypatch):
    url, _, calls, _ = ready
    monkeypatch.setenv("HOME_ASSISTANT_TOKEN", "bad token\r\nX-Evil: 1" + "z" * 20)
    text, err = tool(url, "list_devices")
    assert err and "does not look right" in text and "Evil" not in text
    assert calls == []
