"""Prep Calculator's server (prep-calculator/server.py): the eight tools with hand-worked cases, the token on
every route, the refusals and the fixed safety lines. Nothing here touches the internet."""

import contextlib
import http.client
import importlib.util
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "t" * 20

_spec = importlib.util.spec_from_file_location("prep_calculator_server", PKG / "server.py")
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
GEN = "Run a generator outdoors only, far from doors and windows. Carbon monoxide kills."
BLEACH = ("Stir and wait 30 minutes. The water should smell slightly of chlorine. Bleach does not remove chemicals "
          "or fuel. Boiling is the surer method when you can do it.")


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
def base(tmp_path):
    with running(prep.make_server(0, tmp_path, TOKEN)) as url:
        yield url
    assert list(tmp_path.iterdir()) == []          # stores nothing


def post(base, body: bytes, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + "/mcp", data=body, method="POST", headers=h)
    try:
        with _open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def mcp(base, method, params=None, token=TOKEN):
    return post(base, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
                token)


def tool(base, name, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200
    return out["result"]["isError"], out["result"]["content"][0]["text"]


def ok(base, name, **args):
    is_error, text = tool(base, name, **args)
    assert not is_error, text
    assert chr(0x2014) not in text
    return text


def refused(base, name, **args):
    is_error, text = tool(base, name, **args)
    assert is_error and text.startswith(f"{name} could not do that:"), text
    return text


# ---------------------------------------------------------------- the manifest and the token

def test_tool_list_equals_the_manifest(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    code, out = mcp(base, "tools/list")
    assert code == 200
    listed = {t["name"]: t["description"] for t in out["result"]["tools"]}
    assert listed == manifest["abilities"]["tools"]                      # word for word
    assert len(listed) == 8
    assert set(manifest["abilities"]["examples"]) <= set(listed)
    assert manifest["abilities"]["changes"] == [] and all(t["annotations"]["readOnlyHint"]
                                                          for t in out["result"]["tools"])


def test_every_route_needs_the_token(base):
    assert mcp(base, "tools/list", token=None)[0] == 401
    assert mcp(base, "tools/list", token="wrong")[0] == 401
    for path in ("/health", "/mcp", "/nothing"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _open(urllib.request.Request(base + path), timeout=10)
        assert e.value.code == 401
    with _open(urllib.request.Request(base + "/health", headers={"X-PuffPAI-Key": TOKEN}), timeout=10) as r:
        assert r.status == 200 and r.read() == b"ok"


# ---------------------------------------------------------------- 1 water_needed

def test_water_needed(base):
    # 4 people x 14 days x 1 gal = 56 gal; 56 x 3.785 = 211.98 L, about 212 L.
    # Jugs 56; 5-gal cans 56 / 5 = 11.2, so 12; 20-L cans 211.98 / 20 = 10.6, so 11.
    t = ok(base, "water_needed", people=4, days=14)
    assert "4 people, for 14 days" in t and "store 56 US gallons (212 liters)" in t
    assert "56 one-gallon jugs, 12 five-gallon cans or 11 twenty-liter cans" in t
    assert "1 US gallon (3.8 liters) per person per day" in t and "hot climate, pregnancy" in t
    # 1 person + 1 dog for 1 day: 1 + 1 quart (0.25 gal) = 1.25 gal, shown 1.3; 1.25 x 3.785 = 4.73 L, shown 4.7.
    t = ok(base, "water_needed", people=1, days=1, pets=1)
    assert "1 pet at 1 US quart" in t and "store 1.3 US gallons (4.7 liters)" in t
    assert "2 one-gallon jugs, 1 five-gallon can or 1 twenty-liter can" in t
    # Hot: 2 people x 3 days x 1.5 gal = 9 gal; 9 x 3.785 = 34.07 L, shown 34.1.
    t = ok(base, "water_needed", people=2, days=3, hot_climate=True)
    assert "1.5 US gallons (5.7 liters) per person per day" in t and "store 9 US gallons (34.1 liters)" in t
    # Pet override: 1 person, 2 pets at 2 quarts, 10 days: (1 + 2 x 2 / 4) x 10 = 20 gal; 75.7 L.
    assert "store 20 US gallons (75.7 liters)" in ok(base, "water_needed", people=1, days=10, pets=2,
                                                     pet_quarts_per_day=2)


# ---------------------------------------------------------------- 2 water_on_hand

def test_water_on_hand(base):
    # 20 gal / (2 people x 1 gal) = 10 days.
    t = ok(base, "water_on_hand", gallons=20, people=2)
    assert "20 US gallons (75.7 liters) for 2 people lasts about 10 days (10 full days)" in t
    # 50 L / 3.785 = 13.21 gal; 13.21 / 1 = 13.2 days, 13 full days.
    t = ok(base, "water_on_hand", liters=50, people=1)
    assert "lasts about 13.2 days (13 full days)" in t and "1 US gallon (3.8 liters) a day" in t
    # 30 gal / (2 x 1 + 2 pets x 0.25) = 30 / 2.5 = 12 days.
    assert "lasts about 12 days" in ok(base, "water_on_hand", gallons=30, people=2, pets=2)
    # Hot: 30 gal / (2 x 1.5) = 10 days.
    assert "lasts about 10 days" in ok(base, "water_on_hand", gallons=30, people=2, hot_climate=True)


# ---------------------------------------------------------------- 3 food_needed

def test_food_needed(base):
    # 2 adults x 2,000 x 30 days = 120,000 kcal; rice 120,000 / 365 x 100 = 32,877 g = 32.9 kg;
    # 32.877 / 0.4536 = 72.5 lb.
    t = ok(base, "food_needed", adults=2, days=30)
    assert "4,000 kcal a day, 120,000 kcal in all" in t and "roughly 32.9 kg (72.5 pounds)" in t
    assert "white rice (dry)" in t and "365 kcal per 100 g" in t and "real needs vary" in t
    # 2 adults x 2,000 + 2 children x 1,500 = 7,000 a day; x 14 = 98,000; beans 98,000 / 340 x 100 = 28,824 g
    # = 28.8 kg; / 0.4536 = 63.5 lb.
    t = ok(base, "food_needed", adults=2, children=2, days=14, food="dried beans")
    assert "7,000 kcal a day, 98,000 kcal in all" in t and "roughly 28.8 kg (63.5 pounds)" in t
    # 1 heavy worker x 3,000 x 10 = 30,000; peanut butter 30,000 / 590 x 100 = 5,085 g = 5.1 kg; 11.2 lb.
    t = ok(base, "food_needed", heavy_workers=1, days=10, food="peanut butter")
    assert "30,000 kcal in all" in t and "roughly 5.1 kg (11.2 pounds)" in t
    # Override: 1 adult at 2,500 for 4 days = 10,000; label 400 kcal per 100 g: 2,500 g = 2.5 kg; 5.5 lb.
    t = ok(base, "food_needed", adults=1, days=4, kcal_per_adult=2500, food="trail mix", kcal_per_100g=400)
    assert "10,000 kcal in all" in t and "trail mix" in t and "roughly 2.5 kg (5.5 pounds)" in t


# ---------------------------------------------------------------- 4 battery_runtime

def test_battery_runtime(base):
    # 1,280 x 0.8 x 0.85 / 100 = 8.704 h, shown 8.7.
    t = ok(base, "battery_runtime", watt_hours=1280, load_watts=100)
    assert "lasts about 8.7 hours (1,280 x 0.8 x 0.85 / 100)" in t
    assert "80 percent" in t and "85 percent inverter efficiency" in t
    # 100 Ah x 12 V = 1,200 Wh; lead acid: 1,200 x 0.5 x 0.85 / 60 = 8.5 h.
    t = ok(base, "battery_runtime", amp_hours=100, volts=12, battery_type="lead acid", load_watts=60)
    assert "(1,200 watt-hour) lead acid" in t and "lasts about 8.5 hours (1,200 x 0.5 x 0.85 / 60)" in t
    # DC, no inverter: 1,280 x 0.8 / 50 = 20.48 h, shown 20.5.
    t = ok(base, "battery_runtime", watt_hours=1280, load_watts=50, inverter=False)
    assert "lasts about 20.5 hours (1,280 x 0.8 / 50)" in t and "no inverter loss" in t
    # 5,120 Wh x 0.8 x 0.85 / 20 = 174.08 h = 7.25 days, shown 174 hours (about 7.3 days).
    assert "about 174 hours (about 7.3 days)" in ok(base, "battery_runtime", watt_hours=5120, load_watts=20)


# ---------------------------------------------------------------- 5 power_budget

def test_power_budget(base):
    # 60 x 24 + 10 x 5 + 10 x 2 = 1,440 + 50 + 20 = 1,510 Wh a day.
    # Battery 1,510 / (0.8 x 0.85) = 2,220.6 Wh, shown 2,221; / 12 V = 185.05 Ah, shown 185.
    # Solar 1,510 / (4 x 0.75) = 503.3 W, shown 503.
    t = ok(base, "power_budget", devices=[{"name": "fridge", "watts": 60, "hours_per_day": 24},
                                          {"name": "lights", "watts": 10, "hours_per_day": 5},
                                          {"name": "phone", "watts": 10, "hours_per_day": 2}])
    assert "1,510 watt-hours (1.51 kWh) a day" in t
    assert "about 2,221 watt-hours (185 amp-hours at 12 volts)" in t and "about 503 watts of panels" in t
    assert "4 peak sun hours x 0.75 system efficiency" in t
    # 100 W x 3 h = 300 Wh; lead acid, no inverter, 2 days: 300 x 2 / 0.5 = 1,200 Wh = 100 Ah at 12 V;
    # solar at 5 sun hours, 80 percent: 300 / (5 x 0.8) = 75 W.
    t = ok(base, "power_budget", devices=[{"name": "radio", "watts": 100, "hours_per_day": 3}],
           battery_type="lead acid", inverter=False, days_of_storage=2, peak_sun_hours=5, system_efficiency_percent=80)
    assert "about 1,200 watt-hours (100 amp-hours at 12 volts) of lead acid" in t and "about 75 watts" in t
    # 500 W x 2 h = 1,000 Wh; lifepo4 at 24 V: 1,000 / 0.68 = 1,470.6 Wh, shown 1,471; / 24 = 61.3 Ah;
    # solar 1,000 / 3 = 333.3 W, shown 333.
    t = ok(base, "power_budget", devices=[{"name": "pump", "watts": 500, "hours_per_day": 2}], battery_volts=24)
    assert "about 1,471 watt-hours (61.3 amp-hours at 24 volts)" in t and "about 333 watts" in t


# ---------------------------------------------------------------- 6 generator_fuel

def test_generator_fuel(base):
    # 2,000 W is at or under 2,500 W: 0.5 x 10 = 5 to 0.75 x 10 = 7.5 gal; 18.9 to 28.4 L.
    t = ok(base, "generator_fuel", load_watts=2000, hours=10)
    assert "roughly 5 to 7.5 US gallons of gasoline (18.9 to 28.4 liters)" in t and t.endswith(GEN)
    assert "read the generator's own manual" in t
    # 4,000 W: scale 4,000 / 2,500 = 1.6; 0.8 x 5 = 4 to 1.2 x 5 = 6 gal; 15.1 to 22.7 L.
    t = ok(base, "generator_fuel", load_watts=4000, hours=5)
    assert "roughly 4 to 6 US gallons of gasoline (15.1 to 22.7 liters)" in t and t.endswith(GEN)
    # Propane, 1,000 W, 10 h: 5 x 1.31 = 6.55, shown 6.6 (half up), to 7.5 x 1.31 = 9.825, shown 9.8 gal;
    # 6.55 x 4.2 = 27.5 lb.
    t = ok(base, "generator_fuel", load_watts=1000, hours=10, fuel="propane")
    assert "roughly 6.6 to 9.8 US gallons of propane" in t and "about 27.5 to" in t and t.endswith(GEN)
    # 1 hour at 500 W: 0.5 to 0.75 gal; 1.9 to 2.8 L.
    assert "roughly 0.5 to 0.75 US gallons of gasoline (1.9 to 2.8 liters)" in ok(
        base, "generator_fuel", load_watts=500, hours=1)


# ---------------------------------------------------------------- 7 bleach_for_water

def test_bleach_for_water(base):
    # 5 gal clear x 8 drops = 40 drops; 5 x 1/8 tsp = 0.625 tsp, shown 0.63 (up); x 5 ml = 3.125 ml, shown 3.2 (up).
    t = ok(base, "bleach_for_water", volume=5, bleach_strength_percent=6)
    assert ": 40 drops (8 drops per gallon x 5" in t and "about 0.63 teaspoons (about 3.2 ml), rounded up" in t
    assert t.endswith(BLEACH) and "approximate" in t
    # Cloudy: 5 x 8 x 2 = 80 drops; 1.25 tsp, shown 1.3.
    t = ok(base, "bleach_for_water", volume=5, bleach_strength_percent=6, cloudy=True)
    assert ": 80 drops" in t and "x 2 for cloudy water" in t and t.endswith(BLEACH)
    # 10 L x 2 drops = 20 drops, at 8.25 percent.
    t = ok(base, "bleach_for_water", volume=10, unit="liters", bleach_strength_percent=8.25)
    assert "8.25 percent" in t and ": 20 drops (2 drops per liter x 10" in t and t.endswith(BLEACH)
    # 1 gal cloudy: 8 x 2 = 16 drops; under 2 gallons, no teaspoon line.
    t = ok(base, "bleach_for_water", volume=1, bleach_strength_percent=5, cloudy=True)
    assert ": 16 drops" in t and "teaspoon" not in t and t.endswith(BLEACH)


# ---------------------------------------------------------------- 8 unit_convert

def test_unit_convert(base):
    # 5 x 3.785411784 = 18.927 L, shown 18.93.
    assert ok(base, "unit_convert", value=5, from_unit="gallons", to_unit="liters") == \
        "5 US gallons is 18.93 liters (US measures)."
    # (100 - 32) x 5 / 9 = 37.78 C.
    assert ok(base, "unit_convert", value=100, from_unit="F", to_unit="C") == \
        "100 degrees Fahrenheit is 37.78 degrees Celsius."
    # 1,280 Wh / 12.8 V = 100 Ah.
    assert ok(base, "unit_convert", value=1280, from_unit="Wh", to_unit="Ah", volts=12.8).startswith(
        "1,280 watt-hours is 100 amp-hours at 12.8 volts")
    # 1 / 0.45359237 = 2.2046 lb, shown 2.205; 10 mi x 1.609344 = 16.09 km; 2 qt = 1.893 L; 16 oz = 453.6 g.
    assert ok(base, "unit_convert", value=1, from_unit="kg", to_unit="lb") == "1 kilogram is 2.205 pounds."
    assert ok(base, "unit_convert", value=10, from_unit="miles", to_unit="km") == "10 miles is 16.09 kilometers."
    assert ok(base, "unit_convert", value=2, from_unit="quarts", to_unit="liters").startswith("2 US quarts is 1.893")
    assert ok(base, "unit_convert", value=16, from_unit="oz", to_unit="g") == "16 ounces is 453.6 grams."


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("name,args", [
    ("water_needed", {"people": -4, "days": 14}),
    ("water_needed", {"people": 4, "days": 1_000_000}),
    ("water_needed", {"people": 2.5, "days": 3}),
    ("water_needed", {"people": "lots", "days": 3}),
    ("water_needed", {"people": 10 ** 400, "days": 3}),
    ("water_needed", {"people": 2, "days": 3, "hot_climate": "maybe"}),
    ("water_on_hand", {"people": 2}),
    ("water_on_hand", {"people": 2, "gallons": 5, "liters": 5}),
    ("water_on_hand", {"people": 2, "gallons": -5}),
    ("food_needed", {"days": 10}),
    ("food_needed", {"days": 10, "adults": 1, "food": "cake"}),
    ("food_needed", {"days": 10, "adults": 1, "kcal_per_adult": 50000}),
    ("battery_runtime", {"load_watts": 100}),
    ("battery_runtime", {"load_watts": 100, "watt_hours": 1000, "battery_type": "potato"}),
    ("battery_runtime", {"load_watts": 0, "watt_hours": 1000}),
    ("battery_runtime", {"load_watts": 100, "amp_hours": 100}),
    ("power_budget", {"devices": []}),
    ("power_budget", {"devices": "fridge"}),
    ("power_budget", {"devices": [{"watts": 60, "hours_per_day": 25}]}),
    ("power_budget", {"devices": [[[[[[1]]]]]]}),
    ("generator_fuel", {"load_watts": 2000, "hours": 10, "fuel": "diesel"}),
    ("generator_fuel", {"load_watts": 20000, "hours": 10}),
    ("generator_fuel", {"load_watts": 2000, "hours": -1}),
    ("bleach_for_water", {"volume": 5, "bleach_strength_percent": 12.5}),
    ("bleach_for_water", {"volume": 5, "bleach_strength_percent": 3}),
    ("bleach_for_water", {"volume": 5, "bleach_strength_percent": 6, "unit": "barrels"}),
    ("bleach_for_water", {"volume": 0, "bleach_strength_percent": 6}),
    ("unit_convert", {"value": 3, "from_unit": "kg", "to_unit": "km"}),
    ("unit_convert", {"value": 1000, "from_unit": "Wh", "to_unit": "Ah"}),
    ("unit_convert", {"value": -500, "from_unit": "C", "to_unit": "F"}),
    ("unit_convert", {"value": -5, "from_unit": "gallons", "to_unit": "liters"}),
    ("unit_convert", {"value": 5, "from_unit": "gallons", "to_unit": "liters", "nonsense": 1}),
])
def test_nonsense_is_refused_plainly(base, name, args):
    refused(base, name, **args)


def test_bleach_outside_5_to_9_percent_says_follow_the_label(base):
    assert "Follow the product label and official guidance" in refused(
        base, "bleach_for_water", volume=5, bleach_strength_percent=10)


def test_bad_bodies_get_a_plain_refusal_never_a_server_error(base):
    assert post(base, b"[" * 20000 + b"]" * 20000)[0] == 400                 # deeply nested
    assert post(base, b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "x": ' + b"9" * 5000 + b"}")[0] == 400
    assert post(base, b"NaN")[0] == 400
    huge = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "water_needed", '         b'"arguments": {"people": 1e400, "days": 3}}}'                    # a float too big: infinity
    code, out = post(base, huge)
    assert code == 200 and out["result"]["isError"] and "people is a number from 1 to 1,000" in         out["result"]["content"][0]["text"]
    assert post(base, b"not json")[0] == 400
    for params in (["a"], "x", 5, None, {"name": "water_needed", "arguments": [1]},
                   {"name": ["water_needed"]}, {"name": "nope"}):
        code, out = mcp(base, "tools/call", params) if params is not None else mcp(base, "tools/call")
        assert code == 200 and ("error" in out or out["result"]["isError"])
    code, out = mcp(base, "initialize", ["x"])
    assert code == 200 and out["result"]["serverInfo"]["name"] == "prep-calculator"
    # a body over 64 KB is refused before it is read
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
    conn.request("POST", "/mcp", body=b"{}", headers={"Authorization": f"Bearer {TOKEN}",
                                                       "Content-Length": str(10 ** 9)})
    assert conn.getresponse().status == 400
    conn.close()


def test_words_rules():
    text = "".join(p.read_text(encoding="utf-8") for p in PKG.rglob("*") if p.is_file() and p.suffix in
                   (".py", ".md", ".json", ".svg") and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts)
    assert chr(0x2014) not in text
    assert "semp" + "e" not in text.lower()


# ---------------------------------------------------------------- a bleach dose is never shown smaller (fix 8)

SPOONS = re.compile(r"That is about ([0-9.,]+) teaspoons? \(about ([0-9.,]+) ml\), rounded up")


def test_bleach_spoon_and_ml_figures_are_never_below_the_dose():
    app, seen = prep.Prep(), 0
    for i in range(1, 601):                                    # 0.1 to 60 gallons in steps of 0.1
        vol = Decimal(i) / 10
        for cloudy in (False, True):
            text = app.bleach_for_water(str(vol), 6, cloudy=cloudy)
            drops = int(re.search(r": ([0-9,]+) drops?", text).group(1).replace(",", ""))
            assert drops >= vol * 8 * (2 if cloudy else 1)
            got = SPOONS.search(text)
            if vol < 2:
                assert got is None and "teaspoon" not in text
                continue
            assert text.index(" drop") < text.index("teaspoon")    # the drops are the main figure
            tsp = vol * Decimal("0.125") * (2 if cloudy else 1)
            shown_tsp, shown_ml = (Decimal(g.replace(",", "")) for g in got.groups())
            assert shown_tsp >= tsp and shown_ml >= tsp * 5, (vol, cloudy, text)
            assert shown_tsp - tsp < Decimal("0.1") and shown_ml - tsp * 5 < Decimal("0.1")
            seen += 1
    assert seen == 2 * 581


@pytest.mark.parametrize("volume,cloudy,want", [
    (13, False, "about 1.7 teaspoons (about 8.2 ml)"),          # 1.625 tsp and 8.125 ml, once shown 1.6 and 8.1
    (2.1, False, "about 0.27 teaspoons (about 1.4 ml)"),        # 0.2625 tsp and 1.3125 ml, once shown 0.26
    (8, False, "about 1 teaspoon (about 5 ml)"),
    (4, True, "about 1 teaspoon (about 5 ml)")])
def test_bleach_hand_worked_doses(base, volume, cloudy, want):
    assert want in ok(base, "bleach_for_water", volume=volume, bleach_strength_percent=6, cloudy=cloudy)


# ---------------------------------------------------------------- one, and tiny results (fix 9)

def test_one_is_singular_and_tiny_times_are_minutes(base):
    t = ok(base, "bleach_for_water", volume=0.1, bleach_strength_percent=6)
    assert ": 1 drop (8 drops per gallon" in t and "1 drops" not in t
    t = ok(base, "bleach_for_water", volume=1, unit="liters", bleach_strength_percent=6)
    assert t.startswith("For 1 liter (0.26 US gallons)") and ": 2 drops" in t
    t = ok(base, "water_on_hand", liters=1, people=1)
    assert t.startswith("0.26 US gallons (1 liter) for 1 person") and "1 liters" not in t
    assert "store 1 US gallon (3.8 liters)" in ok(base, "water_needed", people=1, days=1)
    t = ok(base, "water_on_hand", gallons=10, people=100)                  # 10 / 100 = 0.1 day = 2.4 hours
    assert "lasts about 2.4 hours (0 full days)" in t
    t = ok(base, "water_on_hand", gallons=0.5, people=1000)                # 0.0005 day = 0.72 minutes
    assert "lasts less than a minute" in t and "0 days" not in t
    t = ok(base, "battery_runtime", watt_hours=100, load_watts=200)       # 100 x 0.8 x 0.85 / 200 = 0.34 h
    assert "lasts about 20.4 minutes (" in t and "0 hours" not in t
    t = ok(base, "battery_runtime", watt_hours=1, load_watts=100000)
    assert "lasts less than a minute (" in t and "0 hours" not in t
    t = ok(base, "battery_runtime", watt_hours=100, load_watts=100, inverter=False, usable_percent=100)
    assert "lasts about 1 hour (" in t
    t = ok(base, "power_budget", devices=[{"name": "led", "watts": 1, "hours_per_day": 1}])
    assert ": 1 watt-hour (0.001 kWh) a day" in t
    t = ok(base, "food_needed", adults=1, days=1, food="trail mix", kcal_per_100g=441)   # 0.4535 kg = 1 pound
    assert "(1 pound)" in t


# ---------------------------------------------------------------- numbers and yes or no (fix 10)

@pytest.mark.parametrize("people", ["1_0", "1 0", "1,0", "+4", "0x10", "inf", "nan", "4 people", "٤", "1e", "--4",
                                    "4.0.0", ""])
def test_numbers_must_be_written_in_digits(base, people):
    assert "people must be a number" in refused(base, "water_needed", people=people, days=3)


@pytest.mark.parametrize("value,want", [("10", "10 US gallons"), (" 10 ", "10 US gallons"), ("1e1", "10 US gallons"),
                                        ("10.", "10 US gallons"), ("2.5", "2.5 US gallons"), (".5", "0.5 US gallons")])
def test_numbers_in_digits_are_taken(base, value, want):
    assert ok(base, "unit_convert", value=value, from_unit="gal", to_unit="l").startswith(want)


def test_a_minus_sign_is_taken(base):
    assert ok(base, "unit_convert", value="-40", from_unit="C", to_unit="F") == \
        "-40 degrees Celsius is -40 degrees Fahrenheit."


@pytest.mark.parametrize("cloudy,drops", [(1, 80), ("yes", 80), ("1", 80), (True, 80), ("true", 80),
                                          (0, 40), ("no", 40), ("0", 40), (False, 40), ("false", 40)])
def test_cloudy_takes_1_0_yes_no(base, cloudy, drops):
    assert f": {drops} drops" in ok(base, "bleach_for_water", volume=5, bleach_strength_percent=6, cloudy=cloudy)


@pytest.mark.parametrize("cloudy", [2, "maybe", "cloudy", 0.5, [1], {"a": 1}])
def test_cloudy_refuses_anything_else(base, cloudy):
    assert "cloudy is true or false" in refused(base, "bleach_for_water", volume=5, bleach_strength_percent=6,
                                                cloudy=cloudy)


# ---------------------------------------------------------------- a short body (fix 11)

def test_a_body_shorter_than_its_length_gets_a_plain_400_in_about_5_seconds(base):
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=20) as s:
        s.sendall(f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{{\"a\": 1".encode())
        started = time.monotonic()
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
        took = time.monotonic() - started
    assert got.startswith((b"HTTP/1.0 400", b"HTTP/1.1 400")), got[:40]
    assert 4 <= took < 8, took


# ---------------------------------------------------------------- the sweep (rules 14, 14a, 14b)

HOSTILE = ["1" + " " * 100000 + "x" + " " * 99998, "a" * 199999 + "!", "1/" + "1" * 199998]
GOOD = {"water_needed": {"people": 2, "days": 3}, "water_on_hand": {"people": 2, "gallons": 10},
        "food_needed": {"days": 3, "adults": 2}, "battery_runtime": {"load_watts": 60, "watt_hours": 1000},
        "power_budget": {"devices": [{"name": "fridge", "watts": 100, "hours_per_day": 8}]},
        "generator_fuel": {"load_watts": 2000, "hours": 5}, "bleach_for_water": {"volume": 2, "bleach_strength_percent": 6},
        "unit_convert": {"value": 3, "from_unit": "gallons", "to_unit": "liters"}}


def _every_argument(bad):
    """Each tool with one argument (or one device field) set to *bad*, the rest good."""
    for name, _, props, _ in prep.TOOLS:
        for arg in props:
            if arg == "devices":
                for f in ("name", "watts", "hours_per_day"):
                    yield name, {"devices": [{**GOOD[name]["devices"][0], f: bad}]}
            else:
                yield name, {**GOOD[name], arg: bad}


def _quick_with_health(base, fn):
    """Run fn in a thread; /health must answer while it runs, and fn must finish within 1 second."""
    done, took = threading.Event(), []

    def run():
        t = time.monotonic()
        fn()
        took.append(time.monotonic() - t)
        done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    req = urllib.request.Request(base + "/health", headers={"Authorization": f"Bearer {TOKEN}"})
    with _open(req, timeout=5) as r:
        assert r.status == 200
    th.join(5)
    assert done.is_set() and took[0] < 1.0, took


def test_a_200000_character_value_in_every_argument_answers_within_a_second(base):
    app = prep.Prep()
    for bad in HOSTILE:
        for name, args in _every_argument(bad):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            t = time.monotonic()
            out = prep.mcp_answer(app, msg)                          # the tool itself, with the whole value
            assert "result" in out and time.monotonic() - t < 1.0, (name, list(args))
            # over HTTP the whole value is over the 64 KB body cap: a plain 400, quickly
            _quick_with_health(base, lambda: post(base, json.dumps(msg).encode())[0] == 400 or pytest.fail(name))
        for name, args in _every_argument(bad[:60000]):                # and a value that fits under the cap
            _quick_with_health(base, lambda: tool(base, name, **args))


def _send_quietly(s, body):
    with contextlib.suppress(OSError):
        s.sendall(body)


def _raw(base, head: bytes, body: bytes = b"", wait: float = 30):
    """Send head, then body from a second thread; the status line and headers that come back, and how long."""
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=wait) as s:
        s.sendall(head)
        started = time.monotonic()
        threading.Thread(target=_send_quietly, args=(s, body), daemon=True).start()
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
        return got, time.monotonic() - started


@pytest.mark.parametrize("size", [60000, 10000000])
def test_a_wrong_token_gets_401_after_at_most_64_kb(base, size):
    head = (f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer wrong\r\nContent-Type: application/json\r\n"
            f"Content-Length: {size}\r\n\r\n").encode()
    got, took = _raw(base, head, b"x" * 1000)             # sends a little of what it promised, then waits
    assert got.startswith(b"HTTP/1.0 401") and took < 3, (got[:40], took)


def test_a_body_over_the_cap_is_read_and_dropped_then_a_plain_400_every_time(base):
    size = prep.MAX_BODY * 30
    head = (f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {size}\r\n\r\n").encode()
    for _ in range(5):
        got, _ = _raw(base, head, b" " * size)
        assert got.startswith(b"HTTP/1.0 400"), got[:40]
    lie = (f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {10 ** 9}\r\n\r\n"
           ).encode()
    for _ in range(20):                                   # says 1 GB, sends 2 bytes: still a plain 400, never a reset
        got, took = _raw(base, lie, b"{}")
        assert got.startswith(b"HTTP/1.0 400") and took < 5, (got[:40], took)


def test_a_unit_converts_to_itself(base):
    out = tool(base, "unit_convert", value=1, from_unit="Ah", to_unit="Ah")
    assert "ZeroDivisionError" not in str(out) and "1 amp-hour is 1 amp-hour" in str(out)
