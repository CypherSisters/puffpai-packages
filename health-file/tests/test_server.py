"""Health File's own server (health-file/server.py): the tools, the page routes, the flags, export and import,
and every route's token. All data here is made up.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "h" * 20
DAY = date(2026, 3, 2)                  # the server's "today" in these tests

_spec = importlib.util.spec_from_file_location("health_server", PKG / "server.py")
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
NOBODY_SHARED = lambda: None            # noqa: E731 - the Core's people list does not answer


class Clock:
    def __init__(self):
        self.day = DAY

    def __call__(self):
        return self.day


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
    return tmp_path / "data" / "health-file"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def base(data, clock):
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as url:
        yield url


def raw_call(url, method="GET", raw=None, token=TOKEN, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=raw, method=method, headers=h)
    try:
        with _open(req, timeout=10) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text and "json" in r.headers.get("Content-Type", "") else text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def call(url, method="GET", body=None, token=TOKEN):
    return raw_call(url, method, json.dumps(body).encode() if body is not None else None, token)


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


def household(base):
    for name in ("Alex", "Sam", "Jo"):
        assert call(base + "/api/person/add", "POST", {"name": name})[0] == 200


def rid(text):
    return text.rsplit("[", 1)[1].split("]", 1)[0]


# ------------------------------------------------------------------ the manifest and the tools

def test_tool_list_matches_the_manifest_word_for_word(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert len(tools) <= 8
    assert {n: t["description"] for n, t in tools.items()} == manifest["abilities"]["tools"]
    assert set(tools) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])
    assert [n for n, t in tools.items() if t["annotations"]["destructiveHint"]] == ["remove_record"]
    assert manifest["license"] == "Apache-2.0" and manifest["author"] == "Cypher Sisters"
    assert manifest["needs"]["hosts"] == [] and manifest["needs"]["keys"] == [] and "brain" not in manifest["needs"]


def test_words_in_the_package_folder():
    for f in PKG.rglob("*"):
        if f.is_file() and f.suffix in (".py", ".md", ".json", ".html", ".svg"):
            text = f.read_text(encoding="utf-8")
            assert chr(0x2014) not in text, f
            assert "se" + "mpe" not in text.lower(), f


# ------------------------------------------------------------------ each record type

@pytest.mark.parametrize("kind,fields,change,seen", [
    ("condition", {"name": "Asthma", "since": "2019"}, {"status": "resolved"}, "Asthma, resolved, since 2019"),
    ("medicine", {"name": "Ibuprofen", "dose": "200 mg", "how_often": "twice a day", "for": "knee"},
     {"status": "stopped", "until": "2026-03-01"}, "Ibuprofen 200 mg, twice a day, stopped, for knee"),
    ("allergy", {"what": "Penicillin", "reaction": "hives", "severity": "severe"}, {"severity": "moderate"},
     "Penicillin, hives, moderate"),
    ("result", {"test": "Cholesterol", "value": "5.4", "unit": "mmol/L", "range_text": "3.0-5.0"},
     {"value": "4.8"}, "Cholesterol 4.8 mmol/L on 2026-03-02, range 3.0-5.0, flag in range"),
    ("visit", {"date": "2026-02-20", "who": "Dr Rivera", "why": "knee pain", "follow_up": "2026-04-01"},
     {"said": "rest it"}, "2026-02-20, Dr Rivera, knee pain, follow-up 2026-04-01"),
    ("vaccination", {"vaccine": "Flu", "date": "2025-10-01", "due_again": "2026-10-01"}, {"lot": "AB12"},
     "Flu, 2025-10-01, lot AB12, due again 2026-10-01"),
    ("question", {"text": "Is the knee getting better?"}, {"asked": "yes", "answer": "yes, slowly"},
     "Is the knee getting better?, asked, answer: yes, slowly"),
])
def test_each_record_type_add_change_remove(base, kind, fields, change, seen):
    household(base)
    added = tool(base, "add_record", person="Sam", type=kind, fields=fields)
    r = rid(added)
    changed = tool(base, "change_record", record=r, fields=change)
    assert seen in changed
    assert seen in tool(base, "list_records", person="sam", type=kind)
    assert tool(base, "remove_record", record=r).startswith("Removed")
    assert "has no" in tool(base, "list_records", person="Sam", type=kind)
    assert "no record with the id" in tool_error(base, "remove_record", record=r)


def test_records_and_tests_are_found_by_their_words(base, data):
    household(base)
    tool(base, "add_record", person="Alex", type="medicine", fields={"name": "Ibuprofen", "dose": "200 mg"})
    tool(base, "add_record", person="Sam", type="medicine", fields={"name": "Ibuprofen gel"})
    tool(base, "add_record", person="Jo", type="vaccination", fields={"vaccine": "Flu shot", "date": "2025-10-01"})
    tool(base, "add_record", person="Jo", type="vaccination", fields={"vaccine": "Tetanus", "date": "2020-01-01"})
    tool(base, "add_record", person="Jo", type="result", fields={"test": "LDL cholesterol", "value": "3.1"})
    tool(base, "add_record", person="Jo", type="result", fields={"test": "HDL cholesterol", "value": "1.4"})
    out = tool(base, "change_record", record="IBUPROFEN", person="alex", fields={"status": "stopped"})   # exact
    assert "Ibuprofen 200 mg, stopped" in out
    assert "Ibuprofen gel" in tool(base, "change_record", record="gel", fields={"dose": "thin layer"})  # partial
    before = (data / "health.json").read_text(encoding="utf-8")
    out = tool_error(base, "remove_record", record="ibu")                    # no person named: both people's
    assert "More than one record matches 'ibu': Alex's medicine Ibuprofen [" in out and "Sam's medicine" in out
    assert "200 mg" not in out                                               # names only: nothing else shown
    out = tool_error(base, "change_record", record="cholesterol", person="Jo", fields={"note": "x"})
    assert "More than one record matches 'cholesterol': result LDL cholesterol" in out and "HDL" in out
    assert out.endswith("Say which; nothing was shown or changed.")
    assert (data / "health.json").read_text(encoding="utf-8") == before                       # nothing changed
    out = tool_error(base, "remove_record", record="measles", person="Jo")
    assert "there is no record matching 'measles' for Jo (the records: " in out and "Flu shot" in out
    assert tool(base, "remove_record", record="flu", person="Jo").startswith("Showing Flu shot. Removed vaccination "
                                                                            "Flu shot")
    both = tool(base, "list_records", person="Jo", type="result", name="cholesterol")    # several tests: each result
    assert both.startswith("Jo's results matching 'cholesterol' (2 results;") and "HDL" in both and "LDL" in both
    assert "Jo's LDL cholesterol over time (1 result" in tool(base, "list_records", person="Jo", type="tests", name="ldl")


# ------------------------------------------------------------------ the flag

@pytest.mark.parametrize("value,low,high,text,expected", [
    ("5.4", "3.0", "5.0", "", "high"),
    ("2.9", "3.0", "5.0", "", "low"),
    ("5.0", "3.0", "5.0", "", "in range"),           # on the boundary is in range
    ("3", "3.0", "5.0", "", "in range"),
    ("150", "", "200", "", "in range"),               # only a high side
    ("40", "40", "", "", "in range"),
    ("39.9", "40", "", "", "low"),
    ("5", "", "", "", "none"),                        # no range given
    ("5", "", "", "3.5 - 5.0 mmol/L", "in range"),    # a text range with its unit
    ("5.1", "", "", "3.5 to 5.0", "high"),
    ("200", "", "", "< 200", "high"),                 # "less than 200": 200 itself is out
    ("200", "", "", "<=200", "in range"),
    ("199", "", "", "under 200", "in range"),
    ("60", "", "", ">60", "low"),                     # "more than 60": 60 itself is out
    ("61", "", "", "over 60", "in range"),
    ("12", "", "", "up to 12", "in range"),
    ("5", "", "", "see report", "none"),              # a text range that can not be read
    ("positive", "", "", "negative", "none"),         # a value that is not a number
])
def test_flag_comes_only_from_the_printed_range(value, low, high, text, expected):
    assert hs.flag({"value": value, "range_low": low, "range_high": high, "range_text": text}) == expected


UNCLEAR = "unclear"


@pytest.mark.parametrize("value,unit,low,high,text,expected", [
    # every example the verifier found, now no flag and the note
    ("7", "", "", "", "10-20 (men) 5-15 (women)", UNCLEAR),
    ("5", "", "", "", "0.4-4.0 (adults) 0.7-6.4 (children)", UNCLEAR),
    ("100", "mg/dL", "", "", "3.9-5.5 mmol/L", UNCLEAR),          # another unit
    ("7", "", "10", "5", "", UNCLEAR),                              # reversed numbers
    ("7", "", "", "", "10-5", UNCLEAR),                             # reversed text
    ("7", "", "3", "10", "5-15", UNCLEAR),                          # numbers and text disagree
    ("7", "", "", "", "<5 or >10", UNCLEAR),
    # more that must not flag
    ("7", "", "", "", "<5 >10", UNCLEAR),                           # a second comparison
    ("7", "", "", "", "5-10 and 20-30", UNCLEAR),
    ("7", "", "", "", "10 to 20 5 to 15", UNCLEAR),                 # two number pairs
    ("7", "", "", "", "5-10 male", UNCLEAR),
    ("7", "", "", "", "female 5-10", UNCLEAR),
    ("7", "", "", "", "age 5-10", UNCLEAR),
    ("7", "", "", "", "5-10 adult", UNCLEAR),
    ("7", "", "", "", "<=10 >=2", UNCLEAR),
    ("7", "mg", "", "", "5-10 mg/dL", UNCLEAR),
    ("7", "", "", "", "see report", UNCLEAR),
    ("7", "", "", "", "between 5 and 10", UNCLEAR),
    # what must still flag, and flag right
    ("7", "", "", "", "5-10", "in range"),
    ("4", "", "", "", "5-10", "low"),
    ("11", "", "", "", "5-10", "high"),
    ("5", "", "", "", "5-10", "in range"),                          # on the low boundary
    ("10", "", "", "", "5 to 10", "in range"),                      # on the high boundary
    ("4.9", "", "", "", "<5", "in range"),
    ("5", "", "", "", "<5", "high"),
    ("60", "", "", "", ">=60", "in range"),
    ("59.9", "", "", "", ">=60", "low"),
    ("200", "", "", "", "up to 200", "in range"),
    ("201", "", "", "", "up to 200", "high"),
    ("5", "", "", "", "5 or less", "in range"),
    ("6", "", "", "", "5 or less", "high"),
    ("40", "", "", "", "at least 40", "in range"),
    ("39", "", "", "", "at least 40", "low"),
    ("5.0", "mmol/L", "", "", "3.5-5.0 MMOL/L", "in range"),        # the same unit, capitals ignored
    ("5.1", "", "", "", "3.5-5.0 mmol/L", "high"),                  # no unit on the result: fine
    ("2", " mmol/L ", "", "", "3.5-5.0", "low"),                     # no unit on the range: fine
    ("7", "", "5", "15", "5-15", "in range"),                       # numbers and text agree
    ("7", "", "", "", "", "none"),                                  # nothing typed: no flag, no note
])
def test_a_flag_only_when_the_range_is_clear(value, unit, low, high, text, expected):
    r = {"value": value, "unit": unit, "range_low": low, "range_high": high, "range_text": text}
    if expected == UNCLEAR:
        assert hs.flag(r) == "none" and hs.flag_note(r) == hs.UNCLEAR, r
        assert hs.UNCLEAR in hs.describe({"type": "result", "test": "T", "date": "2026-01-01", **r})
    else:
        assert hs.flag(r) == expected and hs.flag_note(r) == "", r


def test_the_unclear_note_shows_in_the_tools_and_the_page(base):
    household(base)
    tool(base, "add_record", person="Jo", type="result",
         fields={"test": "Ferritin", "value": "7", "range_text": "10-20 (men) 5-15 (women)"})
    hist = tool(base, "list_records", person="Jo", type="labs", name="ferritin")
    assert "flag none. Range not clear, no flag given. Read the printed range.)" in hist
    v = call(base + "/view?person=Jo")[1]
    assert v["records"]["result"][0]["flag_note"] == hs.UNCLEAR and v["summary"]["results"][0]["flag"] == "none"
    pts = call(base + "/history?person=Jo&test=Ferritin")[1]["points"]
    assert pts[0]["flag_note"] == hs.UNCLEAR and pts[0]["low"] is None and pts[0]["high"] is None


# ------------------------------------------------------------------ adding a person by name

def test_a_tool_adds_a_person_it_does_not_know(base):
    household(base)
    out = tool(base, "add_record", person="Pat", type="result", fields={"test": "Iron", "value": "12"})
    assert out.startswith("Added Pat to Health File. Added to Pat's file: result Iron 12")
    out = tool(base, "add_question", person="Robin", text="Is the cough better?")
    assert out.startswith("Added Robin to Health File. Added to Robin's file: question")
    assert tool(base, "add_record", person="alex", type="person") == "Alex is already in Health File."
    # a name that differs only by capitals or spaces is the person already there
    out = tool(base, "add_record", person="  p A t ", type="allergy", fields={"what": "Latex"})
    assert out.startswith("Added to Pat's file: allergy Latex")
    assert tool(base, "add_question", person="ROBIN", text="And the rash?").startswith("Added to Robin's file")
    # type person with just a name: add the person only
    assert tool(base, "add_record", person="Casey", type="person") == "Added Casey to Health File."
    assert tool(base, "add_record", person="casey", type="person") == "Casey is already in Health File."
    assert tool(base, "add_record", person="", type="person", fields={"name": "Dee Dee"}) == \
        "Added Dee Dee to Health File."
    assert "just their name" in tool_error(base, "add_record", person="Sam", type="person", fields={"what": "x"})
    assert "say which person" in tool_error(base, "add_record", person="", type="condition", fields={"name": "x"})
    names = [p["name"] for p in call(base + "/people")[1]["people"]]
    assert names == ["Alex", "Sam", "Jo", "Pat", "Robin", "Casey", "Dee Dee"]
    assert "Iron 12" in tool(base, "list_records", person="pat", type="result")


def test_a_tool_adds_a_person_next_to_the_shared_list(data, clock):
    with core_stub([{"id": "jo", "name": "Jo"}]) as (api, seen):
        with running(hs.make_server(0, data, TOKEN, clock, api=api)) as base:
            assert tool(base, "add_record", person="JO", type="allergy", fields={"what": "Nuts"}).startswith(
                "Added to Jo's file")
            assert tool(base, "add_record", person="Alex", type="person") == "Added Alex to Health File."
            assert call(base + "/people")[1]["people"] == [{"id": "jo", "name": "Jo", "source": "shared"},
                                                          {"id": call(base + "/people")[1]["people"][1]["id"],
                                                           "name": "Alex", "source": "own"}]
    assert len(seen) == 1                                     # asked once, then kept for a minute


# ------------------------------------------------------------------ a hand-edited file and the FHIR export

def test_fhir_export_skips_a_record_that_will_not_read(data, clock):
    data.mkdir(parents=True)
    people = [{"id": "own-1", "name": "Alex"}]
    records = [
        {"id": "r1", "person": "own-1", "type": "condition", "name": "Asthma", "status": "active"},
        {"id": "r2", "person": "own-1", "type": "condition", "status": "active"},             # no name
        {"id": "r3", "person": "own-1", "type": "result", "test": "Iron", "value": "9"},       # no date
        {"id": "r4", "person": "own-1", "type": "visit", "date": 20260101},                    # date not words
        {"id": "r5", "person": ["own-1"], "type": "allergy", "what": "x"},                     # odd person
        {"id": "r6", "person": "own-1", "type": ["allergy"]},                                  # odd type
        {"id": "r7", "person": "own-1", "type": "allergy", "what": "Penicillin"},
    ]
    (data / "health.json").write_text(json.dumps({"format": "health-file/1", "people": people, "records": records}),
                                      encoding="utf-8")
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
        req = urllib.request.Request(base + "/export?format=fhir", headers={"Authorization": f"Bearer {TOKEN}"})
        with _open(req, timeout=10) as r:
            assert r.status == 200 and r.headers["X-Health-File-Skipped"] == "5"
            out = json.loads(r.read())
        kinds = sorted(e["resource"]["resourceType"] for e in out["entry"])
        assert kinds == ["AllergyIntolerance", "Condition", "Patient"]
        assert call(base + "/export?format=app")[0] == 200
        assert call(base + "/view?person=Alex")[0] == 200


def test_list_records_gives_one_test_over_time_with_its_flags(base, clock):
    household(base)
    tool(base, "add_record", person="Jo", type="result",
         fields={"test": "Cholesterol", "date": "2025-01-10", "value": 5.6, "unit": "mmol/L", "range_low": 3,
                 "range_high": 5})
    tool(base, "add_record", person="Jo", type="result",
         fields={"test": "cholesterol", "date": "2025-07-10", "value": "5.0", "unit": "mmol/L", "range_text": "3-5"})
    tool(base, "add_record", person="Jo", type="result", fields={"test": "Cholesterol", "value": "4.1"})
    out = tool(base, "list_records", person="Jo", type="result", name="CHOLESTEROL")
    lines = out.splitlines()
    assert lines[0].startswith("Jo's Cholesterol over time (3 results;")
    assert lines[1] == "- 2025-01-10: 5.6 mmol/L (range 3 to 5; flag high)"
    assert lines[2] == "- 2025-07-10: 5.0 mmol/L (range 3-5; flag in range)"
    assert lines[3] == "- 2026-03-02: 4.1 (no range given; flag none)"
    assert lines[-1] == hs.DISCLAIMER
    pts = call(base + "/history?person=Jo&test=Cholesterol")[1]["points"]
    assert [(p["number"], p["low"], p["high"], p["flag"]) for p in pts] == \
        [(5.6, 3.0, 5.0, "high"), (5.0, 3.0, 5.0, "in range"), (4.1, None, None, "none")]
    assert "has no results for 'Iron'" in tool(base, "list_records", person="Jo", type="bloods", name="Iron")
    # a test name sent as test, or with no type: the same answer
    assert tool(base, "list_records", person="Jo", test="cholesterol") == out
    assert tool(base, "list_records", person="Jo", name="cholesterol") == out
    # no test name: every result, each test's values together, oldest first
    tool(base, "add_record", person="Jo", type="result",
         fields={"test": "Iron", "date": "2025-02-01", "value": "9", "range_text": "10-30"})
    tool(base, "add_record", person="Jo", type="medicine", fields={"name": "Iron tablets"})
    every = tool(base, "list_records", person="Jo", type="result").splitlines()
    assert every[0].startswith("Jo's results (4 results;")
    assert every[1].startswith("- Cholesterol 5.6 mmol/L on 2025-01-10, range 3 to 5, flag high [")
    assert every[3].startswith("- Cholesterol 4.1 on 2026-03-02, no range given, flag none [")
    assert every[4].startswith("- Iron 9 on 2025-02-01, range 10-30, flag low [")
    assert every[-1] == hs.DISCLAIMER and len(every) == 6
    # words that match more than results (Iron tablets): the plain list, not a history
    assert "Medicines:" in tool(base, "list_records", person="Jo", name="iron")


# ------------------------------------------------------------------ the summary and the visit

def test_summary_order_and_the_fixed_line(base, clock):
    household(base)
    add = lambda kind, **f: tool(base, "add_record", person="Alex", type=kind, fields=f)  # noqa: E731
    add("condition", name="Asthma", since="2019")
    add("condition", name="Broken wrist", status="resolved", until="2024-06")
    add("medicine", name="Salbutamol inhaler", how_often="when needed")
    add("medicine", name="Amoxicillin", status="stopped")
    add("allergy", what="Penicillin", reaction="hives", severity="severe")
    add("result", test="HbA1c", date="2025-12-01", value="40", unit="mmol/mol", range_text="20-41")
    add("result", test="HbA1c", date="2026-02-01", value="44", unit="mmol/mol", range_text="20-41")
    add("visit", date="2026-02-20", who="Dr Rivera", follow_up="2026-04-01")
    add("vaccination", vaccine="Tetanus", date="2016-01-01", due_again="2026-01-01")
    add("question", text="Do I still need the inhaler?")
    add("question", text="Old question", asked=True)
    out = tool(base, "person_summary", person="Alex")
    order = ["ALLERGIES: Penicillin, hives, severe.", "Active conditions (1):", "- Asthma, active, since 2019",
             "Current medicines (1):", "- Salbutamol inhaler, when needed, taking", "Latest results (1):",
             "- HbA1c 44 mmol/mol on 2026-02-01, range 20-41, flag high", "Coming up (2):",
             "- 2026-01-01: Tetanus vaccination due (overdue)", "- 2026-04-01: Follow-up with Dr Rivera",
             "Open questions for the next visit (1):", "- Do I still need the inhaler?"]
    at = [out.index(x) for x in order]
    assert at == sorted(at), out
    assert "Broken wrist" not in out and "Amoxicillin" not in out and "Old question" not in out
    assert out.endswith("\n" + hs.DISCLAIMER)
    for name, args in (("list_records", {"type": "medicine"}), ("list_records", {}), ("prepare_for_visit", {})):
        assert tool(base, name, person="Alex", **args).endswith(hs.DISCLAIMER)
    assert not tool(base, "list_records", person="Alex", type="allergy").endswith(hs.DISCLAIMER)
    page = call(base + "/")[1]
    assert hs.DISCLAIMER in page


def test_prepare_for_visit_shows_what_changed(base, clock):
    household(base)
    add = lambda kind, **f: tool(base, "add_record", person="Sam", type=kind, fields=f)  # noqa: E731
    add("visit", date="2026-01-15", who="Dr Rivera")
    add("medicine", name="Ibuprofen", since="2025-06")
    add("medicine", name="Naproxen", since="2026-02-01")
    add("result", test="Iron", date="2026-02-10", value="9", range_low="10", range_high="30")
    out = tool(base, "add_question", person="Sam", text="Can I stop the naproxen?")
    assert "open questions" in out
    out = tool(base, "prepare_for_visit", person="Sam")
    assert "Questions to ask (1):\n- Can I stop the naproxen?" in out
    assert "Last visit: 2026-01-15, Dr Rivera." in out
    assert "- Medicine started: Naproxen" in out and "- Result: Iron 9 on 2026-02-10, range 10 to 30, flag low" in out
    assert "Current medicines (2):" in out


# ------------------------------------------------------------------ export and import

def fill(base):
    household(base)
    add = lambda person_, kind, **f: tool(base, "add_record", person=person_, type=kind, fields=f)  # noqa: E731
    add("Alex", "condition", name="Asthma", since="2019", note="worse in winter")
    add("Alex", "medicine", name="Salbutamol", dose="100 mcg", how_often="when needed", reason="asthma",
        prescriber="Dr Rivera", since="2019-05")
    add("Alex", "allergy", what="Penicillin", reaction="hives", severity="severe")
    add("Alex", "result", test="Peak flow", date="2026-01-05", value="420", unit="L/min", range_low="400",
        range_high="600", lab="Clinic")
    add("Alex", "result", test="Vitamin D", date="2026-01-05", value="60", unit="nmol/L", range_text=">50")
    add("Alex", "visit", date="2026-01-05", who="Dr Rivera", why="check-up", said="all fine",
        follow_up="2026-07-01")
    add("Alex", "vaccination", vaccine="Flu", date="2025-10-01", lot="AB12", due_again="2026-10-01")
    add("Alex", "question", text="Do I need a spacer?")
    add("Jo", "allergy", what="Peanuts", severity="severe")


@pytest.mark.parametrize("fmt", ["app", "fhir"])
def test_export_then_import_round_trip(base, data, clock, tmp_path, fmt):
    fill(base)
    code, out = call(base + f"/export?format={fmt}")
    assert code == 200
    if fmt == "fhir":
        assert out["resourceType"] == "Bundle" and out["type"] == "collection"
        kinds = [e["resource"]["resourceType"] for e in out["entry"]]
        assert kinds.count("Patient") == 3 and "Encounter" in kinds and "Immunization" in kinds
        obs = next(e["resource"] for e in out["entry"] if e["resource"]["resourceType"] == "Observation")
        assert obs["valueQuantity"] == {"value": 420, "unit": "L/min"} and obs["code"] == {"text": "Peak flow"}
        assert obs["referenceRange"] == [{"low": {"value": 400, "unit": "L/min"}, "high": {"value": 600, "unit": "L/min"}}]
        text = json.dumps(out).lower()
        assert "loinc" not in text and "snomed" not in text
    before = call(base + f"/view?person=Alex")[1]["records"]
    # into an empty Health File: everything comes back
    with running(hs.make_server(0, tmp_path / "other", TOKEN, clock, shared=NOBODY_SHARED)) as other:
        code, got = call(other + "/api/import", "POST", {"data": out})
        assert code == 200, got
        want = 9 if fmt == "app" else 8                  # questions stay in this app's own format
        assert sum(got["imported"].values()) == want and not got["skipped"]
        after = call(other + "/view?person=Alex")[1]["records"]
        strip = lambda rs: [{k: v for k, v in r.items() if k not in ("id", "person", "added_on", "changed_on")}  # noqa: E731
                            for r in rs]
        for kind in ("condition", "medicine", "allergy", "result", "visit", "vaccination"):
            assert strip(after[kind]) == strip(before[kind]), kind
        # importing the same file again adds nothing: import never overwrites and skips what is there
        code, again = call(other + "/api/import", "POST", {"data": out})
        assert code == 200 and not again["imported"] and sum(again["skipped"].values()) == want
    # one person only
    one = call(base + f"/export?format={fmt}&person=Jo")[1]
    assert "Peanuts" in json.dumps(one) and "Asthma" not in json.dumps(one)


def test_fhir_import_reads_defensively(base):
    household(base)
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": "urn:uuid:p1", "resource": {"resourceType": "Patient", "name": [{"given": ["Sam"]}]}},
        {"resource": {"resourceType": "Condition", "code": {"coding": [{"display": "Migraine"}]},
                      "subject": {"reference": "urn:uuid:p1"}}},
        {"resource": {"resourceType": "Condition", "code": {}, "subject": {"reference": "urn:uuid:p1"}}},  # no name
        {"resource": {"resourceType": "Observation", "code": {"text": "Iron"}, "subject": {"reference": "urn:uuid:p1"},
                      "valueQuantity": {"value": 12}, "effectiveDateTime": "2026-01-01T09:00:00Z",
                      "referenceRange": "not a list"}},
        {"resource": {"resourceType": "Observation", "code": {"text": "Iron"}, "subject": {"reference": "urn:uuid:p1"},
                      "valueQuantity": {"value": 12}}},                                       # no date
        {"resource": {"resourceType": "Procedure", "subject": {"reference": "urn:uuid:p1"}}},
        {"resource": {"resourceType": "AllergyIntolerance", "code": {"text": "Latex"},
                      "patient": {"reference": "urn:uuid:nobody"}}},                          # no such patient
        {"resource": {"resourceType": ["odd"]}}, "not an object", {"resource": None}, 7,
    ]}
    code, out = call(base + "/api/import", "POST", {"data": bundle})
    assert code == 200, out
    assert out["imported"] == {"conditions": 1, "results": 1}
    assert out["skipped"] == {"conditions": 1, "results": 1, "other entries": 2, "allergies": 1}
    assert "Migraine" in tool(base, "list_records", person="Sam", type="condition")
    assert "Iron 12 on 2026-01-01, no range given, flag none" in tool(base, "list_records", person="Sam", type="result")
    assert call(base + "/api/import", "POST", {"data": {"resourceType": "Patient"}})[0] == 400
    assert call(base + "/api/import", "POST", {"data": [1, 2]})[0] == 400


# ------------------------------------------------------------------ page routes, names, bad input

def test_page_routes_and_injection_shown_as_text(base):
    assert call(base + "/health") == (200, "ok")
    code, page = call(base + "/")
    assert code == 200 and "getJSON('updated')" in page and "http://" not in page and "https://" not in page
    assert "function esc(" in page and "innerHTML=view.people.map(p=>`<option value=\"${esc(p.id)}\"" in page
    evil = "<img src=x onerror=alert(1)>"
    code, out = call(base + "/api/person/add", "POST", {"name": evil})
    assert code == 200
    pid = out["id"]
    code, out = call(base + "/api/record/add", "POST", {"person": pid, "type": "condition", "fields": {"name": evil}})
    assert code == 200
    v = call(base + f"/view?person={pid}")[1]
    assert v["person"]["name"] == evil and v["records"]["condition"][0]["name"] == evil   # kept as text
    assert call(base + "/meta")[1]["disclaimer"] == hs.DISCLAIMER
    rec = v["records"]["condition"][0]["id"]
    assert call(base + "/api/record/change", "POST", {"id": rec, "fields": {"note": "x"}})[0] == 200
    assert "still has records" in call(base + "/api/person/remove", "POST", {"id": pid})[1]["error"]
    assert call(base + "/api/record/remove", "POST", {"id": rec})[0] == 200
    assert call(base + "/api/person/remove", "POST", {"id": pid})[0] == 200
    assert call(base + "/people")[1] == {"people": [], "source": "own"}


@pytest.mark.parametrize("raw", [
    b'{"person": "Alex", "type": "result", "fields": {"test": "x", "value": 1e999}}',          # a huge number
    b'{"person": "Alex", "type": "result", "fields": {"test": "x", "value": ' + b"9" * 5000 + b"}}",
    b'{"a":' + b"[" * 50000 + b"]" * 50000 + b"}",                                              # deeply nested
    b'{"a":' + b"[" * 60 + b"]" * 60 + b"}",
    b'[1, 2]', b'not json', b'{"x": NaN}', b"\xff\xfe",
], ids=["huge-float", "huge-int", "nested-50000", "nested-60", "list", "not-json", "nan", "not-utf8"])
def test_bad_bodies_get_a_plain_refusal(base, raw):
    household(base)
    code, out = raw_call(base + "/api/record/add", "POST", raw)
    assert code == 400 and out["error"], out
    code, out = raw_call(base + "/mcp", "POST", raw)
    assert code in (200, 400) and "error" in out, out


@pytest.mark.parametrize("msg", [
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "a string"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1, 2]},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["x"], "arguments": {}}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add_record", "arguments": [1]}},
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": {"a": 1}}},
    {"jsonrpc": "2.0", "id": {"a": 1}, "method": ["x"]},
    [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
])
def test_wrong_shaped_mcp_gets_a_plain_answer(base, msg):
    code, out = call(base + "/mcp", "POST", msg)
    assert code == 200 and ("error" in out or out["result"].get("isError") or "protocolVersion" in out["result"]), out


def test_bad_input_is_refused_plainly(base):
    household(base)
    assert "type is one of" in tool_error(base, "add_record", person="Alex", type="xray", fields={"name": "x"})
    code, out = call(base + "/api/record/add", "POST", {"person": "Pat", "type": "condition", "fields": {"name": "x"}})
    assert code == 400 and "no person called 'Pat'" in out["error"]        # the page never adds a person
    assert "at most 80 characters" in tool_error(base, "add_record", person="P" * 81, type="condition",
                                                 fields={"name": "x"})
    assert "no person with the id" in tool_error(base, "add_record", person="own-0123abcd", type="condition",
                                                 fields={"name": "x"})
    assert [p["name"] for p in call(base + "/people")[1]["people"]] == ["Alex", "Sam", "Jo"]
    assert "needs its condition" in tool_error(base, "add_record", person="Alex", type="condition", fields={})
    assert "status is active or resolved" in tool_error(base, "add_record", person="Alex", type="condition",
                                                        fields={"name": "x", "status": "maybe"})
    assert "range_low is a number" in tool_error(base, "add_record", person="Alex", type="result",
                                                 fields={"test": "x", "value": "1", "range_low": "lots"})
    assert "date is a date" in tool_error(base, "add_record", person="Alex", type="visit", fields={"date": "soon"})
    assert "has no field 'colour'" in tool_error(base, "add_record", person="Alex", type="allergy",
                                                 fields={"what": "x", "colour": "red"})
    assert "fields is an object" in tool_error(base, "add_record", person="Alex", type="allergy", fields="x")
    assert "say what to change" in tool_error(base, "change_record", record="r00000000")
    assert call(base + "/api/record/add", "POST", {"person": "Alex", "type": "condition", "fields": {}})[0] == 400
    assert call(base + "/view?person=Nobody")[0] == 400
    assert call(base + "/export?format=pdf")[0] == 400


def test_unreadable_and_empty_files_are_never_written_over(data, clock):
    data.mkdir(parents=True)
    for content in ("{not json", "", "[1, 2]"):
        (data / "health.json").write_text(content, encoding="utf-8")
        with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
            assert call(base + "/view")[0] == 500
            assert call(base + "/api/person/add", "POST", {"name": "Alex"})[0] == 409
            assert "will not write over it" in tool_error(base, "add_question", person="Alex", text="x")
        assert (data / "health.json").read_text(encoding="utf-8") == content


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/view"), ("GET", "/export"),
                                         ("POST", "/mcp"), ("POST", "/api/record/add"), ("POST", "/api/import"),
                                         ("PUT", "/view"), ("DELETE", "/view")])
def test_every_route_needs_the_token(base, method, path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method != "GET" else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401
    h = {"X-PuffPAI-Key": TOKEN}
    assert raw_call(base + "/health", "GET", None, token=None, headers=h) == (200, "ok")


# ------------------------------------------------------------------ the Core's shared people list

class PeopleStub(BaseHTTPRequestHandler):
    answer: bytes = b"[]"
    code = 200
    seen: list
    extra: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.answer)))
        for k, v in self.extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(self.answer)


@contextlib.contextmanager
def core_stub(answer, code=200):
    seen: list = []
    handler = type("Stub", (PeopleStub,), {"answer": json.dumps(answer).encode(), "code": code, "seen": seen})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    with running(srv) as url:
        yield url, seen


def test_shared_people_list_is_used_when_it_answers(data, clock):
    people = [{"id": "alex", "name": "Alex", "emoji": "", "role": "adult"},
              {"id": "jo", "name": "Jo", "emoji": "", "role": "child"}]
    with core_stub(people) as (api, seen):
        with running(hs.make_server(0, data, TOKEN, clock, api=api)) as base:
            assert call(base + "/people")[1] == {"people": [{"id": "alex", "name": "Alex", "source": "shared"},
                                                            {"id": "jo", "name": "Jo", "source": "shared"}],
                                                 "source": "shared"}
            tool(base, "add_record", person="jo", type="allergy", fields={"what": "Peanuts"})
    assert seen[0] == ("/v1/people", f"Bearer {TOKEN}")
    # the Core stops answering: the app's own list still has Jo, and Jo's records
    with core_stub({"error": "down"}, 500) as (api, _):
        with running(hs.make_server(0, data, TOKEN, clock, api=api)) as base:
            assert call(base + "/people")[1] == {"people": [{"id": "jo", "name": "Jo", "source": "own"}],
                                                 "source": "own"}
            assert "Peanuts" in tool(base, "list_records", person="Jo", type="allergy")


@pytest.mark.parametrize("answer,code", [([], 200), ({"not": "a list"}, 200), ("x", 500)])
def test_people_route_failing_falls_back_to_own_list(data, clock, answer, code):
    with core_stub(answer, code) as (api, seen):
        with running(hs.make_server(0, data, TOKEN, clock, api=api)) as base:
            household(base)
            assert [p["name"] for p in call(base + "/people")[1]["people"]] == ["Alex", "Sam", "Jo"]
            assert call(base + "/people")[1]["source"] == "own"
    assert seen                                       # it did ask


def test_nothing_but_a_local_core_is_ever_dialed(monkeypatch):
    dialed = []
    monkeypatch.setattr(hs.socket, "create_connection", lambda *a, **k: dialed.append(a) or 1 / 0)
    for api in ("http://example.com", "https://127.0.0.1:1", "http://nas.home.arpa", "ftp://localhost", "", "x:",
                "http://user:pw@127.0.0.1:9", "http://127.0.0.1.example.com:9", "http://127.0.0.1:9/?x=1",
                "http://127.0.0.1:9/a b", "http://other.invalid:9", "http://lan.invalid:9"):
        assert hs.read_shared_people(api, TOKEN) is None, api
    assert hs.read_shared_people("http://127.0.0.1:9", "") is None
    assert hs.read_shared_people("http://127.0.0.1:9", "bad\r\nX-Evil: 1") is None
    assert dialed == []
    with core_stub([]) as (api, seen):
        monkeypatch.undo()
        assert hs.read_shared_people(api, TOKEN) == [] and seen
    assert hs.read_shared_people("http://127.0.0.1:1", TOKEN) is None     # nothing listening


def test_proxy_settings_are_never_used(monkeypatch):
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with core_stub([{"id": "jo", "name": "Jo"}]) as (api, seen):
        assert hs.read_shared_people(api, TOKEN) == [{"id": "jo", "name": "Jo"}] and len(seen) == 1


# ------------------------------------------------------------------ the people list: redirects and slow answers

def test_a_redirect_is_never_followed_and_the_token_never_leaves(data, clock):
    with core_stub([{"id": "x", "name": "Somebody"}]) as (second, second_seen):
        handler = type("Redirect", (PeopleStub,), {"answer": b"", "code": 302, "seen": [],
                                                   "extra": {"Location": second + "/v1/people"}})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        with running(srv) as first:
            assert hs.read_shared_people(first, TOKEN) is None                # a 3xx is "no answer"
            for code in (301, 303, 307, 308):
                handler.code = code
                assert hs.read_shared_people(first, TOKEN) is None
            handler.code = 302
            with running(hs.make_server(0, data, TOKEN, clock, api=first)) as base:
                assert call(base + "/people")[1] == {"people": [], "source": "own"}
        assert len(handler.seen) == 6 and all(a == f"Bearer {TOKEN}" for _, a in handler.seen)
    assert second_seen == []                                                  # not one request, so no token


class DripStub(BaseHTTPRequestHandler):
    """Answers one byte a second, for up to 60 seconds."""
    seen: list

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append(self.path)
        try:
            for b in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + b" " * 60:
                self.wfile.write(bytes([b]))
                self.wfile.flush()
                time.sleep(1)
        except OSError:
            pass


def test_a_slow_people_list_does_not_hold_up_the_page(data, clock):
    seen: list = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("Drip", (DripStub,), {"seen": seen}))
    srv.daemon_threads = True
    with running(srv) as api:
        with running(hs.make_server(0, data, TOKEN, clock, api=api)) as base:
            took = []                                         # four pages at once while the Core drips
            def view():
                t = time.monotonic()
                assert call(base + "/view")[0] == 200
                took.append(time.monotonic() - t)
            threads = [threading.Thread(target=view) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            assert len(took) == 4 and max(took) < 3, took
            household(base)
            t = time.monotonic()
            assert call(base + "/people")[1]["source"] == "own"
            assert time.monotonic() - t < 1                  # the failed ask is kept for 30 seconds
    assert len(seen) == 1
    t = time.monotonic()
    assert hs.read_shared_people(api, TOKEN, timeout=60) is None   # the deadline never goes past 2 seconds
    assert time.monotonic() - t < 3


def test_the_people_list_is_kept_for_a_minute(data, clock, monkeypatch):
    asked = []
    answers = iter([None, [{"id": "jo", "name": "Jo"}]])
    now = [1000.0]
    monkeypatch.setattr(hs.time, "monotonic", lambda: now[0])
    app = hs.HealthFile(hs.Store(data), clock, shared=lambda: asked.append(1) or next(answers))
    assert app.shared() is None and app.shared() is None and len(asked) == 1
    now[0] += 29
    assert app.shared() is None and len(asked) == 1
    now[0] += 2                                              # 31 s after a failed ask: ask again
    assert app.shared() == [{"id": "jo", "name": "Jo"}] and len(asked) == 2
    now[0] += 59
    assert app.shared() and len(asked) == 2


def test_an_answer_bigger_than_256_kb_is_not_read(data):
    with core_stub([{"id": "p", "name": "x" * 300 * 1024}]) as (api, seen):
        assert hs.read_shared_people(api, TOKEN) is None and seen


# ------------------------------------------------------------------ a short body

def test_a_body_shorter_than_its_length_gets_a_plain_400(base):
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
        s.sendall(b"POST /api/person/add HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                  + b"\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"name\": \"A")
        t = time.monotonic()
        reply = b""
        while b"\r\n\r\n" not in reply or not reply.rstrip().endswith(b"}"):
            chunk = s.recv(65536)
            if not chunk:
                break
            reply += chunk
        took = time.monotonic() - t
    assert reply.startswith(b"HTTP/1.0 400") or reply.startswith(b"HTTP/1.1 400"), reply[:60]
    assert b"ended before its body did" in reply
    assert 4 < took < 10, took


# ------------------------------------------------------------------ the sweep: names, slow patterns, files, bodies

HOSTILE = ["1" + " " * 199_998 + "x", "# " + " " * 199_997 + "y", "a" * 199_999 + "!",
           "1 " * 99_999 + "or less"]
FITS = ["1" + " " * 60_000 + "x", "a" * 60_000 + "!"]                  # under the 64 KB body cap
NEEDED = {"person": "Alex", "record": "Cholesterol", "test": "cholesterol", "type": "result", "text": "x",
          "name": "x", "fields": {"test": "Cholesterol", "value": "5"}}


def health_ok(base):
    """/health answers (a connect the OS drops is tried again)."""
    for _ in range(3):
        try:
            return call(base + "/health")[0] == 200
        except OSError:
            continue
    return False


def raw_post(base, length, body=b"", token=TOKEN, path="/mcp", wait=25):
    """One POST by hand: (status, body text, seconds). The body may be shorter than *length*."""
    port = int(base.rsplit(":", 1)[1])
    for attempt in range(4):                # a connect the OS drops (a port still closing) is tried again
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=3)
            break
        except OSError:
            if attempt == 3:
                raise
    conn.settimeout(wait)
    start = time.monotonic()                  # the time the server takes, from a made connection
    got = b""
    with conn as s:
        top = (f"POST {path} HTTP/1.1\r\nHost: health.invalid\r\nContent-Type: application/json\r\n"
               f"Content-Length: {length}\r\n" + (f"Authorization: Bearer {token}\r\n" if token else "") + "\r\n")
        s.sendall(top.encode())
        try:
            s.sendall(body)
        except OSError:
            pass
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
                hd, sep, rest = got.partition(b"\r\n\r\n")
                if sep and b"content-length:" in hd.lower():
                    n = int(hd.lower().split(b"content-length:")[1].split(b"\r\n")[0])
                    if len(rest) >= n:
                        break
        except OSError:
            pass
    status = int(got.split(b" ")[1]) if got.startswith(b"HTTP/") else 0
    return status, got.partition(b"\r\n\r\n")[2].decode("utf-8", "replace"), time.monotonic() - start


def test_partial_match_is_whole_words_only_and_private(base, data):
    household(base)
    tool(base, "add_record", person="Alex", type="medicine", fields={"name": "Sharonex", "dose": "5 mg"})
    assert "no record matching 'ron'" in tool_error(base, "remove_record", record="ron", person="Alex")
    tool(base, "add_record", person="Alex", type="medicine", fields={"name": "Ron tablets", "dose": "10 mg"})
    out = tool(base, "change_record", record="Ron", person="Alex", fields={"status": "stopped"})
    assert out.startswith("Showing Ron tablets.")
    assert "no record matching 'haron'" in tool_error(base, "remove_record", record="haron", person="Alex")
    assert "no record matching 'sh'" in tool_error(base, "remove_record", record="sh", person="Alex")
    assert tool(base, "change_record", record="sha", person="Alex", fields={"note": "x"}).startswith(
        "Showing Sharonex.")
    tool(base, "add_record", person="Alex", type="result", fields={"test": "LDL cholesterol", "value": "3.1"})
    tool(base, "add_record", person="Alex", type="result", fields={"test": "HDL cholesterol", "value": "1.4"})
    before = (data / "health.json").read_text(encoding="utf-8")
    out = tool_error(base, "remove_record", record="chol", person="Alex")
    assert "More than one record matches 'chol'" in out and "LDL cholesterol" in out
    assert "3.1" not in out and "1.4" not in out                            # names only: nothing else shown
    assert (data / "health.json").read_text(encoding="utf-8") == before
    assert tool(base, "list_records", person="Alex", type="result", name="ldl").startswith("Showing LDL cholesterol.")
    assert "no results for 'ldl c'" not in tool(base, "list_records", person="Alex", type="result", name="ldl chol")
    assert "has no results for 'olest'" in tool(base, "list_records", person="Alex", type="result", name="olest")


def test_hostile_text_answers_within_a_second_on_every_argument(base, data, clock):
    household(base)
    tool(base, "add_record", person="Alex", type="result", fields={"test": "Cholesterol", "value": "5"})
    for bad in HOSTILE:
        start = time.monotonic()
        hs.parse_range_text(bad)
        hs.number(bad)
        assert time.monotonic() - start < 1, bad[:3]
    app = hs.HealthFile(hs.Store(data.parent / "side"), clock, shared=lambda: None)
    app.add_record(person="Alex", type="result", fields={"test": "Cholesterol", "value": "5"})
    for name, _, props, req in hs.TOOLS:
        for arg in props:
            for bad in HOSTILE:
                for value in (bad, [bad], {"test": bad, "value": bad, "range_text": bad, "name": bad}):
                    args = {**{r: NEEDED.get(r, "x") for r in req}, arg: value}
                    start = time.monotonic()
                    try:
                        getattr(app, name)(**args)
                    except (TypeError, ValueError, hs.Unreadable):
                        pass
                    assert time.monotonic() - start < 1, (name, arg, bad[:3])
    jobs = []
    for name, _, props, req in hs.TOOLS:
        for arg in props:
            for bad in HOSTILE + FITS:
                jobs.append(("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": {**{r: NEEDED.get(r, "x") for r in req}, arg: bad}}}).encode()))
    for bad in HOSTILE:                                                     # and the import route
        jobs.append(("/api/import?person=Alex", json.dumps({"format": "health-file/1", "people": [{"name": bad}],
                     "records": [{"type": "result", "test": bad, "value": bad, "range_text": bad}]}).encode()))
        jobs.append(("/api/import?person=Alex", json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {"resourceType": "Observation", "code": {"text": bad}, "valueString": bad,
                          "referenceRange": [{"text": bad}], "effectiveDateTime": bad}}]}).encode()))
    for path, body in jobs:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=raw_post(base, len(body), body, path=path)))
        t.start()
        assert health_ok(base)
        t.join(60)
        status, text, took = out["r"]
        assert status in (200, 400) and took < 1, (path, body[:80], status, took)


@pytest.mark.parametrize("content", ['{}', '{"app": "other"}', '{"format": "other/1", "people": [], "records": []}',
                                     '{"people": [], "records": [], "lists": []}'])
def test_a_foreign_data_file_is_never_written_over(data, clock, content):
    data.mkdir(parents=True)
    (data / "health.json").write_text(content, encoding="utf-8")
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
        out = tool_error(base, "add_question", person="Alex", text="x")
        assert "The Health File data file is not this app's file" in out and str(data) not in out
        assert call(base + "/api/person/add", "POST", {"name": "Alex"})[0] == 409
    assert (data / "health.json").read_text(encoding="utf-8") == content


def test_bodies_wrong_token_too_big_and_short(base):
    status, _, took = raw_post(base, 1000, b"{" * 10, token="wrong")                    # wrong token, never ends
    assert status == 401 and took < 3
    status, _, took = raw_post(base, 100 * 1024 * 1024, b"x" * 70_000, token="wrong")    # wrong token, big body
    assert status == 401 and took < 3
    status, text, took = raw_post(base, 30 * 1024 * 1024, b" " * (30 * 1024 * 1024))     # over the cap: drained
    assert status == 400 and "KB" in text and took < 20
    status, text, took = raw_post(base, 1000, b"{" * 10)                                  # shorter than it says
    assert status == 400 and took < 6.5
    assert call(base + "/health")[0] == 200


# ------------------------------------------------------------------ the second verifier's faults

def test_a_short_word_never_finds_a_record(base, data):
    household(base)
    tool(base, "add_record", person="Alex", type="medicine", fields={"name": "St John's wort"})
    tool(base, "add_record", person="Alex", type="vaccination", fields={"vaccine": "Hepatitis A", "date": "2025-01-01"})
    tool(base, "add_record", person="Alex", type="condition", fields={"name": "Autism"})
    before = (data / "health.json").read_text(encoding="utf-8")
    for short in ("s", "a", "au", "St", "j"):
        assert "no record matching" in tool_error(base, "remove_record", record=short)
        assert "no record matching" in tool_error(base, "remove_record", record=short, person="Alex")
        assert "no record matching" in tool_error(base, "change_record", record=short, person="Alex",
                                                  fields={"note": "x"})
    assert (data / "health.json").read_text(encoding="utf-8") == before                  # nothing removed
    assert "has no records matching 'a'" in tool(base, "list_records", person="Alex", name="a")
    assert "Hepatitis" not in tool(base, "list_records", person="Alex", type="vaccination", name="a")
    # three letters that start a word, a word with its apostrophe, or the exact full name: found
    assert tool(base, "change_record", record="john's", person="Alex", fields={"note": "x"}).startswith(
        "Showing St John's wort.")
    assert tool(base, "change_record", record="aut", fields={"note": "x"}).startswith("Showing Autism.")
    tool(base, "add_record", person="Jo", type="condition", fields={"name": "Au"})
    assert tool(base, "remove_record", record="au", person="Jo").startswith("Removed condition Au")   # exact name
    # people are found by their full name only
    tool(base, "add_record", person="Sharon", type="person")
    tool(base, "add_record", person="Ron Smith", type="person")
    assert "no person called 'Ron'" in tool_error(base, "list_records", person="Ron")
    assert "no person called 'Sh'" in tool_error(base, "remove_record", record="aut", person="Sh")


def test_a_record_id_must_belong_to_the_person_named(base, data):
    household(base)
    r = rid(tool(base, "add_record", person="Jo", type="condition", fields={"name": "Depression"}))
    before = (data / "health.json").read_text(encoding="utf-8")
    out = tool_error(base, "change_record", record=r, person="Alex", fields={"status": "resolved"})
    assert f"the record '{r}' is not Alex's; nothing was changed" in out and "Depression" not in out
    out = tool_error(base, "remove_record", record=r, person="Alex")
    assert f"the record '{r}' is not Alex's" in out and "Depression" not in out
    assert (data / "health.json").read_text(encoding="utf-8") == before
    assert "resolved" in tool(base, "change_record", record=r, person="jo", fields={"status": "resolved"})
    assert tool(base, "remove_record", record=r, person="Jo").startswith("Removed condition Depression")


def test_a_name_that_matches_nothing_lists_nobody_elses_records(base):
    household(base)
    tool(base, "add_record", person="Jo", type="condition", fields={"name": "Depression"})
    tool(base, "add_record", person="Alex", type="medicine", fields={"name": "Ibuprofen"})
    for name in ("remove_record", "change_record"):
        out = tool_error(base, name, record="zzz", **({"fields": {"note": "x"}} if name == "change_record" else {}))
        assert "there is no record matching 'zzz'; nothing was changed. Say whose record it is" in out
        assert "Depression" not in out and "Ibuprofen" not in out and "[r" not in out
    out = tool_error(base, "remove_record", record="zzz", person="Jo")              # a person named: only theirs
    assert "for Jo (the records: condition Depression [" in out and "Ibuprofen" not in out


def test_list_records_lists_every_result_by_test_or_lab(base):
    household(base)
    add = lambda **f: tool(base, "add_record", person="Jo", type="result", fields=f)  # noqa: E731
    add(test="LDL cholesterol", date="2025-01-10", value="4.1", unit="mmol/L", range_text="<3.0", lab="Quest")
    add(test="LDL cholesterol", date="2026-01-10", value="2.9", unit="mmol/L", range_text="<3.0", lab="City lab")
    add(test="HDL cholesterol", date="2026-01-10", value="1.4", unit="mmol/L", range_text=">1.0", lab="City lab")
    add(test="Iron", date="2026-01-10", value="12", lab="Quest")
    # a: every result, the older LDL 4.1 too
    every = tool(base, "list_records", person="Jo", type="result")
    assert every.startswith("Jo's results (4 results;") and "LDL cholesterol 4.1 mmol/L on 2025-01-10" in every
    assert "LDL cholesterol 2.9" in every and "HDL cholesterol 1.4" in every and "Iron 12" in every
    assert every.endswith(hs.DISCLAIMER)
    # b: words that fit several tests: each result of those tests, with a type or without
    for args in ({"type": "result"}, {}):
        chol = tool(base, "list_records", person="Jo", name="cholesterol", **args)
        assert chol.startswith("Jo's results matching 'cholesterol' (3 results;"), chol
        assert "LDL cholesterol 4.1" in chol and "LDL cholesterol 2.9" in chol and "HDL cholesterol 1.4" in chol
        assert "Iron" not in chol and "More than one" not in chol
    # c: a lab's name
    for args in ({"type": "result"}, {}):
        quest = tool(base, "list_records", person="Jo", name="Quest", **args)
        assert quest.startswith("Jo's results matching 'Quest' (2 results;"), quest
        assert "LDL cholesterol 4.1" in quest and "Iron 12" in quest and "2.9" not in quest
    # kept: words of one test give that test over time
    ldl = tool(base, "list_records", person="Jo", name="ldl")
    assert ldl.startswith("Showing LDL cholesterol.\nJo's LDL cholesterol over time (2 results;")


@pytest.mark.parametrize("value,unit,text,expected", [
    ("70", "", "60 years or less", UNCLEAR),
    ("6", "", "60 years or less", UNCLEAR),
    ("70", "", "60 age or less", UNCLEAR),
    ("7", "", "<5 fasting", UNCLEAR),
    ("3", "", "<5 fasting", UNCLEAR),
    ("7", "", "1-5 approx", UNCLEAR),
    ("3", "", "1-5 approx", UNCLEAR),
    ("3", "", "1-5 adults", UNCLEAR),
    # the third verifier's cases: a word after the range that only looks like a unit, or a warning word's start
    ("7", "", "3-6 T2", UNCLEAR),
    ("7", "", "3-6 Tri1", UNCLEAR),
    ("7", "", "3-6 wk12-16", UNCLEAR),
    ("7", "", "3-6 F18-49", UNCLEAR),
    ("7", "", "3-6 x2", UNCLEAR),
    ("7", "", "3-6 under-18", UNCLEAR),
    ("7", "", "3-6 men50+", UNCLEAR),
    ("7", "", "3-6 age50+", UNCLEAR),
    ("7", "", "3-6 *", UNCLEAR),
    ("7", "", "3-6 **", UNCLEAR),
    ("7", "", "3-6 %ile", UNCLEAR),
    ("7", "", "3-6 ^", UNCLEAR),
    ("7", "mmol/L", "3-6 men50+", UNCLEAR),
    ("7", "", "3-6 trimester", UNCLEAR),
    ("7", "", "3-6 mg/T2", UNCLEAR),
    ("7", "", "3-6 x10^9", UNCLEAR),
    # a real unit after the range still flags, with or without a unit on the result
    ("7", "", "<5 mmol/L", "high"),
    ("4", "", "5 mg or less", "in range"),
    ("12", "", "4-11 x10^9/L", "high"),
    ("50", "", "40-60 %", "in range"),
    ("30", "", "10-40 U", "in range"),
    ("7", "", "3-6 mg/dL", "high"),
    ("7", "", "3-6 g/L", "high"),
    ("7", "", "3-6 U/L", "high"),
    ("7", "", "3-6 IU/L", "high"),
    ("7", "", "3-6 ng/mL", "high"),
    ("7", "", "3-6 pg/mL", "high"),
    ("7", "", "3-6 mmol/mol", "high"),
    ("7", "", "3-6 mm/hr", "high"),
    ("70", "", ">60 mL/min/1.73m2", "in range"),
    ("12", "", "4-11 X10^9/L", "high"),
    ("7", "fasting", "<5 fasting", "high"),              # the result's own unit: it matches
])
def test_a_word_after_the_range_that_is_not_a_unit_gives_no_flag(value, unit, text, expected):
    r = {"value": value, "unit": unit, "range_low": "", "range_high": "", "range_text": text}
    if expected == UNCLEAR:
        assert hs.flag(r) == "none" and hs.flag_note(r) == hs.UNCLEAR, r
    else:
        assert hs.flag(r) == expected and hs.flag_note(r) == "", r


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]", '{"app": "other"}'])
def test_no_error_names_the_data_files_folder(data, clock, content):
    data.mkdir(parents=True)
    (data / "health.json").write_text(content, encoding="utf-8")
    folder = str(data.parent.parent)
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
        out = tool_error(base, "add_question", person="Alex", text="x")
        assert out.startswith("The Health File data file") and folder not in out and "\\" not in out
        for code, body in (call(base + "/view"), call(base + "/api/person/add", "POST", {"name": "Alex"})):
            assert code in (409, 500) and body["error"].startswith("The Health File data file")
            assert folder not in body["error"] and "\\" not in body["error"]


@pytest.mark.parametrize("junk", [{"people": [7]}, {"records": ["junk-entry"]}])
def test_entries_it_does_not_understand_are_never_dropped(data, clock, junk):
    data.mkdir(parents=True)
    doc = {"format": "health-file/1", "people": [{"id": "own-1", "name": "Alex"}], "records": [
        {"id": "r00000001", "person": "own-1", "type": "condition", "name": "Asthma", "status": "active"}]}
    for k, v in junk.items():
        doc[k] = doc[k] + v
    (data / "health.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    before = (data / "health.json").read_bytes()
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
        assert "Asthma" in tool(base, "list_records", person="Alex")              # reading still works
        out = tool_error(base, "add_question", person="Alex", text="Is it better?")
        assert "does not understand" in out and "will not write over it" in out
        assert "does not understand" in tool_error(base, "remove_record", record="asthma", person="Alex")
        code, body = call(base + "/api/person/add", "POST", {"name": "Sam"})
        assert code == 409 and "does not understand" in body["error"]
    assert (data / "health.json").read_bytes() == before                         # byte for byte


def test_a_change_on_a_file_with_odd_entries_is_refused_first(data, clock):
    data.mkdir(parents=True)
    doc = {"format": "health-file/1", "people": [{"id": "own-1", "name": "Alex"}], "records": ["x"]}
    (data / "health.json").write_text(json.dumps(doc), encoding="utf-8")
    before = (data / "health.json").read_bytes()
    with running(hs.make_server(0, data, TOKEN, clock, shared=NOBODY_SHARED)) as base:
        for name, args in (("change_record", {"record": "r11111111", "fields": {"note": "x"}}),
                           ("remove_record", {"record": "r11111111"}),
                           ("remove_record", {"record": "zzz", "person": "Nobody"}),
                           ("add_record", {"person": "Pat", "type": "allergy", "fields": {"what": "Latex"}}),
                           ("add_record", {"person": "Alex", "type": "person"})):
            out = tool_error(base, name, **args)
            assert out == hs.ODD_ENTRIES, (name, out)
        for path, body in (("/api/record/change", {"id": "r11111111", "fields": {"note": "x"}}),
                           ("/api/record/remove", {"id": "r11111111"}), ("/api/person/remove", {"id": "own-1"})):
            code, out = call(base + path, "POST", body)
            assert code == 409 and out["error"] == hs.ODD_ENTRIES, (path, out)
    assert (data / "health.json").read_bytes() == before


def test_list_records_finds_a_short_test_name_with_a_digit(base, data):
    household(base)
    tool(base, "add_record", person="Sharon", type="result", fields={"test": "Free T4", "value": "14"})
    tool(base, "add_record", person="Sharon", type="medicine", fields={"name": "Vitamin B6"})
    out = tool(base, "list_records", person="Sharon", name="t4")
    assert out.startswith("Showing Free T4.\nSharon's Free T4 over time (1 result;"), out
    assert "Vitamin B6" in tool(base, "list_records", person="Sharon", type="medicine", name="b6")
    assert "has no records matching 't'" in tool(base, "list_records", person="Sharon", name="t")
    before = (data / "health.json").read_bytes()
    for name in ("t4", "b6"):                                   # the change tools keep the 3-letter rule
        assert "no record matching" in tool_error(base, "remove_record", record=name, person="Sharon")
        assert "no record matching" in tool_error(base, "change_record", record=name, fields={"note": "x"})
    assert (data / "health.json").read_bytes() == before


def test_the_health_question_helper_names_only_tools_health_file_has():
    helper = PKG.parent / "health-question-helper"
    if not helper.is_dir():
        pytest.skip("the health-question-helper package is not next to this one")
    names = {t[0] for t in hs.TOOLS}
    for f in ("SKILL.md", "README.md"):
        used = set(re.findall(r"health-file\.([a-z_]+)", (helper / f).read_text(encoding="utf-8")))
        assert used and used <= names, (f, used - names)


def test_a_bare_s_after_the_range_is_not_read_as_seconds():
    # "over 50s" may be an age group, so it gives no flag; "sec" is still a unit
    assert hs.flag({"value": "40", "unit": "", "range_text": "over 50s"}) == "none"
    assert hs.flag({"value": "20", "unit": "", "range_text": "under 18s"}) == "none"
    assert hs.flag({"value": "20", "unit": "", "range_text": "under 14 sec"}) == "high"
