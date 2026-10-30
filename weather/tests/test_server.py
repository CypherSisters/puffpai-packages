"""Weather's own server: every tool, the token, and a bad input. Outside calls are replaced;
these tests never touch the internet. Each server listens on a free port on 127.0.0.1 and is
stopped by the test that started it."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "w" * 20

_spec = importlib.util.spec_from_file_location("weather_server", PKG / "server.py")
ws = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws)

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
def asked(monkeypatch):
    """Every outside call is answered here and remembered."""
    calls = []

    def fake(url, params, accept="application/json"):
        calls.append((url, dict(params)))
        if url == ws.GEOCODE:
            if params["name"] == "Nowhere":
                return {}
            return {"results": [{"name": "Asheville", "admin1": "North Carolina", "country": "United States",
                                 "country_code": "US", "latitude": 35.60095, "longitude": -82.55402}]}
        if url == ws.FORECAST and "current" in params:
            return {"current": {"temperature_2m": 61.4, "apparent_temperature": 59.0, "relative_humidity_2m": 72,
                                "weather_code": 2, "wind_speed_10m": 7.8, "precipitation": 0}}
        if url == ws.FORECAST:
            n = params["forecast_days"]
            return {"daily": {"time": ["2026-10-30", "2026-10-31", "2026-11-01"][:n],
                              "weather_code": [0, 61, 3][:n], "temperature_2m_max": [70.2, 64, 58][:n],
                              "temperature_2m_min": [48, 50.6, 41][:n],
                              "precipitation_probability_max": [0, 80, 10][:n]}}
        if url == ws.ALERTS:
            return {"features": [{"properties": {"event": "Wind Advisory", "severity": "Moderate",
                                                 "headline": "Wind Advisory until 6 PM"}}]}
        raise AssertionError(url)

    monkeypatch.setattr(ws, "fetch_json", fake)
    return calls


@pytest.fixture
def base(tmp_path, asked):
    with running(ws.make_server(0, tmp_path / "data", TOKEN)) as url:
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


def tool(base, name, error=False, **args):
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": args}})
    assert code == 200 and out["result"]["isError"] is error, out
    return out["result"]["content"][0]["text"]


def test_no_place_yet_says_so(base, asked):
    assert tool(base, "current_weather") == ws.NO_PLACE
    assert asked == []


def test_set_location_by_name_then_current_weather(base, asked, tmp_path):
    out = tool(base, "set_location", place="Asheville")
    assert out.startswith("Saved the place: Asheville, North Carolina, United States") and "Fahrenheit" in out
    saved = json.loads((tmp_path / "data" / "weather.json").read_text(encoding="utf-8"))
    assert saved["units"] == "f" and saved["location"]["country"] == "US"
    assert tool(base, "current_weather") == ("Weather now in Asheville, North Carolina, United States: 61F, "
                                             "partly cloudy, feels like 59F, humidity 72%, wind 8 mph.")
    url, params = asked[-1]
    assert url == ws.FORECAST and params["latitude"] == round(35.60095, 4) and params["temperature_unit"] == "fahrenheit"
    assert "Nowhere" in tool(base, "set_location", place="Nowhere")


def test_coordinates_skip_the_lookup_and_alerts_are_us_only(base, asked):
    assert "Celsius" in tool(base, "set_location", place="51.5074,-0.1278")
    assert asked == []
    assert "United States only" in tool(base, "severe_alerts")
    assert asked == []


def test_forecast_and_alerts(base, asked):
    tool(base, "set_location", place="35.6,-82.55", units="c")
    out = tool(base, "forecast", days=2)
    assert out.splitlines()[1:] == ["Today: clear, high 70C, low 48C, 0% chance of rain or snow",
                                    "Tomorrow: light rain, high 64C, low 51C, 80% chance of rain or snow"]
    assert asked[-1][1]["forecast_days"] == 2
    assert tool(base, "severe_alerts").endswith("- Wind Advisory (moderate): Wind Advisory until 6 PM")
    assert asked[-1] == (ws.ALERTS, {"point": "35.6000,-82.5500"})


@pytest.mark.parametrize("name,args", [("forecast", {"days": 9}), ("set_location", {"place": ""}),
                                       ("set_location", {"place": "Oslo", "units": "kelvin"}),
                                       ("set_location", {"place": "95,10"})])
def test_bad_input_is_a_plain_error(base, name, args):
    if name == "forecast":
        tool(base, "set_location", place="40.7,-74.0")
    assert "did not fit" in tool(base, name, error=True, **args)


def test_every_route_refuses_without_the_token(base):
    assert call(base + "/health", token=None)[0] == 401
    assert call(base + "/health", token="wrong")[0] == 401
    assert call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
    assert call(base + "/health") == (200, "ok")


def test_manifest_matches_the_live_tool_list(base):
    m = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[1]["result"]["tools"]
    assert m["abilities"]["tools"] == {t["name"]: t["description"] for t in tools}
    assert set(m["abilities"]["examples"]) == set(m["abilities"]["tools"])
    assert {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]} == set(m["abilities"]["changes"])
    assert sorted(m["needs"]["hosts"]) == sorted(ws.HOSTS)


def test_fetch_refuses_an_undeclared_host():
    with pytest.raises(ws.Offline):
        ws.fetch_json("https://example.org/x", {})
