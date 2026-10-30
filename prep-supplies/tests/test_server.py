"""Prep Supplies' own server (prep-supplies/server.py): the tools, the page routes and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "p" * 20

_spec = importlib.util.spec_from_file_location("prep_server", PKG / "server.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

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
    return tmp_path / "data" / "prep-supplies"


@pytest.fixture
def base(data):
    with running(ps.make_server(0, data, TOKEN)) as url:
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


def soon(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_tool_list_matches_the_manifest(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert set(tools) == set(manifest["abilities"]["tools"]) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])


def test_add_update_remove_and_list(base, data):
    assert tool(base, "add_supply", name="Bottled water", category="water", quantity=8, expires_on=soon(10)) \
        .startswith("Added 8 gallons of Bottled water to water, expires ")
    assert "now 12 gallons" in tool(base, "add_supply", name="bottled water", category="water", quantity=4,
                                    expires_on=soon(10))
    tool(base, "add_supply", name="Bandages", category="first aid", quantity=1, unit="box")
    assert "now 2 box of Bandages" in tool(base, "update_supply", supply="bandages", quantity=2)
    out = tool(base, "list_supplies")
    assert out.startswith("2 supplies:") and "first aid: 2 box of Bandages" in out
    exp = tool(base, "list_supplies", expiring=True)
    assert "Bottled water" in exp and "Bandages" not in exp and "(in 10 days)" in exp
    assert tool(base, "list_supplies", category="medicine") == "No medicine supplies."
    assert tool(base, "remove_supply", supply="Bandages") == "Removed 2 box of Bandages."
    saved = json.loads((data / "prep.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in saved["supplies"]] == ["Bottled water"] and saved["last_updated"]


def test_days_remaining_states_assumptions_and_skips_expired(base):
    tool(base, "add_supply", name="Water jugs", category="water", quantity=12)                  # gallons
    tool(base, "add_supply", name="Water bottles", category="water", quantity=7.6, unit="liters")
    tool(base, "add_supply", name="Rice", category="food", quantity=24)                         # servings
    tool(base, "add_supply", name="Beans", category="food", quantity=10, unit="cans")           # no daily figure
    tool(base, "add_supply", name="Old soup", category="food", quantity=30, expires_on=soon(-3))
    out = tool(base, "days_remaining", people=2)
    assert out.startswith("For 2 people: water lasts 7 days, food 4 days. The food runs out first.")
    assert "1 gallon" in out and "3 servings" in out and "Assumptions" in out
    assert "10 cans of Beans" in out and "Expired, so not counted: 30 servings of Old soup" in out
    tool(base, "update_supply", supply="Beans", per_person_per_day=1)
    assert "food 9 days" in tool(base, "days_remaining", people=2)


def test_checklist_compares_with_stock(base):
    out = tool(base, "checklist", people=1)
    assert "Still lacks: water for 3 more days; food for 3 more days; a first aid kit" in out
    tool(base, "add_supply", name="Water", category="water", quantity=3)
    tool(base, "add_supply", name="LED lantern", category="other", quantity=1)
    tool(base, "add_supply", name="Hand-crank radio", category="other", quantity=1)
    out = tool(base, "checklist", people=1)
    lacks, stocked = out.split("Already stocked:")
    assert "water for" not in lacks and "a flashlight or lantern" in stocked and "radio" in stocked
    assert "a manual can opener" in lacks


def test_page_routes_and_people(base):
    assert call(base + "/health") == (200, "ok")
    code, page = call(base + "/")
    assert code == 200 and "BASE+'updated'" in page and "http://" not in page and "https://" not in page
    code, out = call(base + "/api/add", "POST", {"name": "AA batteries", "category": "batteries", "quantity": 24,
                                                 "unit": "batteries"})
    assert code == 200 and out["id"] and out["last_updated"]
    assert call(base + "/api/update", "POST", {"id": out["id"], "quantity": 20})[0] == 200
    assert call(base + "/api/people", "POST", {"people": 3})[0] == 200
    assert call(base + "/summary")[1]["people"] == 3
    doc = call(base + "/data")[1]
    assert doc["supplies"][0]["quantity"] == 20 and call(base + "/updated")[1]["last_updated"] == doc["last_updated"]
    assert call(base + "/api/remove", "POST", {"id": out["id"]})[0] == 200
    assert call(base + "/data")[1]["supplies"] == []


def test_bad_input_is_refused_plainly(base):
    assert "category is one of" in tool_error(base, "add_supply", name="x", category="snacks", quantity=1)
    assert "expires_on is a date" in tool_error(base, "add_supply", name="x", category="food", quantity=1,
                                               expires_on="soonish")
    assert "quantity is a number" in tool_error(base, "add_supply", name="x", category="food", quantity=-2)
    assert "no supply called" in tool_error(base, "remove_supply", supply="nothing")
    assert call(base + "/api/people", "POST", {"people": 0})[0] == 400


def test_unreadable_file_is_never_written_over(data):
    data.mkdir(parents=True)
    (data / "prep.json").write_text("{not json", encoding="utf-8")
    with running(ps.make_server(0, data, TOKEN)) as base:
        assert call(base + "/data")[0] == 500
        assert "will not write over it" in tool_error(base, "add_supply", name="x", category="food", quantity=1)
    assert (data / "prep.json").read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/data"), ("POST", "/mcp"),
                                         ("POST", "/api/add"), ("PUT", "/data")])
def test_every_route_needs_the_token(base, method, path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method != "GET" else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401
