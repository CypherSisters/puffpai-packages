"""Journal's own server (journal/server.py): the tools, the page routes, import and export, every route's token,
plain refusals of bad input, files that will not read, and the page's Markdown renderer.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import contextlib
import http.client
import importlib.util
import io
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "j" * 20
NOW = datetime(2026, 3, 12, 20, 15, 0)           # the server's "now" in these tests

_spec = importlib.util.spec_from_file_location("journal_server", PKG / "server.py")
js = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(js)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class Clock:
    def __init__(self):
        self.at = NOW

    def __call__(self):
        return self.at


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
    return tmp_path / "data" / "journal"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def base(data, clock):
    with running(js.make_server(0, data, TOKEN, clock)) as url:
        yield url


def call(url, method="GET", body=None, token=TOKEN, raw=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=payload, method=method, headers=h)
    try:
        with _open(req, timeout=10) as r:
            out = r.read()
            ctype = r.headers.get("Content-Type", "")
            if "zip" in ctype:
                return r.status, out
            text = out.decode()
            return r.status, (json.loads(text) if text and "json" in ctype else text)
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


def new_id(text):
    return re.search(r"Its id is (\S+)\.$", text).group(1)


def test_tool_list_matches_the_manifest_word_for_word(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert len(tools) <= 8
    assert {n: t["description"] for n, t in tools.items()} == manifest["abilities"]["tools"]
    assert set(tools) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])
    assert [n for n, t in tools.items() if t["annotations"]["destructiveHint"]] == ["remove_entry"]
    assert manifest["needs"]["hosts"] == [] and manifest["needs"]["keys"] == [] and "brain" not in manifest["needs"]


def test_words_are_plain():
    for path in [p for p in PKG.rglob("*") if p.suffix in (".py", ".md", ".json", ".html", ".svg")
                 and not any(part.startswith((".", "__")) for part in p.relative_to(PKG).parts)]:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path
        assert "sem" + "pe" not in text.lower(), path
        assert set(re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)) <= {"127.0.0.1"}, path


def test_add_search_read_and_list_recent(base, data):
    out = tool(base, "add_entry", body="Walked by the river with Sam. #outdoors felt calm", title="River walk",
               mood="good", tags=["Family"], starred=True)
    first = new_id(out)
    assert out.startswith("Saved a journal entry for 2026-03-12 20:15 (9 words, tags family, outdoors).")
    assert (data / "entries" / "2026" / "03" / f"{first}.json").exists()
    tool(base, "add_entry", body="Long day at work. Jo called.", tags="work", mood=2, day="yesterday")
    tool(base, "add_entry", body="Nothing much.", day="2026-02-01")
    assert "day still to come" in tool_error(base, "add_entry", body="x", day="2026-03-13")
    found = tool(base, "search_entries", words="river calm")
    assert found.startswith("1 entry match:") and "River walk" in found and "mood 4 (good)" in found
    assert "2 entries match" in tool(base, "search_entries", start="2026-03-01")
    assert "Jo called" in tool(base, "search_entries", tag="#work") and "Jo called" in tool(base, "search_entries", mood=2)
    assert tool(base, "search_entries", words="nothing-like-this") == "No journal entries match."
    assert "River walk" in tool(base, "search_entries", starred=True)
    full = tool(base, "read_entry", entry=first)
    assert full.startswith(f"[{first}] 2026-03-12 20:15; River walk; mood 4 (good); starred; tags family, outdoors")
    assert "1 entry on 2026-03-11" in tool(base, "read_entry", day="yesterday")
    recent = tool(base, "list_recent", count=2)
    assert recent.startswith("The newest 2 entries:") and "River walk" in recent and "Nothing much" not in recent
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 3 and index["entries"][0]["id"] == first


def test_streak_on_this_day_and_stats(base, clock):
    for day in ("2026-03-09", "2026-03-10", "2026-03-11", "2026-03-01", "2026-02-28"):
        tool(base, "add_entry", body=f"Entry for {day}", day=day, mood=3)
    assert "Writing streak: 3 days in a row now; longest 3 days." in tool(base, "journal_stats")   # alive until tonight
    tool(base, "add_entry", body="Today counts too", mood=5, tags=["garden"])
    stats = tool(base, "journal_stats", period="week")
    assert stats.startswith("4 entries in the last 7 days, on 4 days, 12 words.")
    assert "Writing streak: 4 days in a row now; longest 4 days. 6 entries in the journal." in stats
    assert "Mood: average 3.5 of 5 over 4 entries (great 1, okay 3)." in stats and "Tags: garden 1." in stats
    clock.at = NOW + timedelta(days=2)
    assert "Writing streak: 0 days in a row now; longest 4 days." in tool(base, "journal_stats")
    # on this day: same month and day, earlier years only; Feb 29 shows on Feb 28 in other years
    tool(base, "add_entry", body="A year ago", day="2025-03-14")
    tool(base, "add_entry", body="Leap day", day="2024-02-29")
    tool(base, "add_entry", body="Two years ago", day="2024-03-14")
    otd = tool(base, "on_this_day")
    assert otd.startswith("2 entries from March 14 in earlier years:") and otd.index("A year ago") < otd.index("Two years")
    assert "Leap day" in tool(base, "on_this_day", day="2026-02-28") and "Entry for 2026-02-28" not in \
        tool(base, "on_this_day", day="2026-02-28")
    assert tool(base, "on_this_day", day="2026-07-04") == "No entries from July 4 in earlier years."
    assert "period is week" in tool_error(base, "journal_stats", period="fortnight")


def test_change_and_remove(base, data):
    eid = new_id(tool(base, "add_entry", body="First draft", tags=["a"]))
    assert tool(base, "change_entry", entry="latest", add_text="More later. #later", starred=True, mood=0) \
        .startswith("Changed added text, mood, star:")
    saved = json.loads(next((data / "entries").rglob(f"{eid}.json")).read_text(encoding="utf-8"))
    assert saved["body"] == "First draft\n\nMore later. #later" and saved["tags"] == ["a", "later"] and saved["starred"]
    tool(base, "change_entry", entry=eid, body="Rewritten", tags="b, c", mood="great", title="New")
    saved = json.loads(next((data / "entries").rglob(f"{eid}.json")).read_text(encoding="utf-8"))
    assert (saved["body"], saved["tags"], saved["mood"], saved["title"]) == ("Rewritten", ["b", "c"], 5, "New")
    assert "say what to change" in tool_error(base, "change_entry", entry=eid)
    assert "no entry" in tool_error(base, "change_entry", entry="20260101-000000-abcd", mood=1)
    assert tool(base, "remove_entry", entry=eid) == "Deleted the entry from 2026-03-12 20:15 (New)."
    assert not list((data / "entries").rglob("*.json"))
    assert "no entries yet" in tool_error(base, "remove_entry", entry="latest")



def test_entries_are_found_by_date_or_title(base, data):
    walk = new_id(tool(base, "add_entry", title="River walk", body="Calm by the water.", day="yesterday"))
    tool(base, "add_entry", title="Walk to the shop", body="Bread.", day="2026-03-01")
    interview = new_id(tool(base, "add_entry", title="Job interview", body="It went well."))
    tool(base, "add_entry", title="After the interview", body="Relieved.")
    assert tool(base, "change_entry", entry="river  WALK", starred=True).startswith(f"Changed star: [{walk}]")  # exact
    assert tool(base, "change_entry", entry="shop", mood=4).startswith("Using 'Walk to the shop' (2026-03-01). "
                                                                       "Changed mood: [")         # partial
    assert "Calm by the water." in tool(base, "read_entry", entry="yesterday")                             # a date
    assert tool(base, "read_entry", entry="2026-03-12").startswith("2 entries on 2026-03-12:")
    files = {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")}
    out = tool_error(base, "change_entry", entry="walk", mood=1)
    assert "More than one entry matches 'walk': 2026-03-11 River walk (id " in out and out.endswith("Say which.")
    assert "Walk to the shop" in out
    out = tool_error(base, "remove_entry", entry="today")                        # two entries today: nothing deleted
    assert "More than one entry matches 'today'" in out and "Job interview" in out
    assert {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")} == files
    out = tool_error(base, "remove_entry", entry="holiday")
    assert "there is no journal entry with 'holiday' in its title (the newest: " in out
    assert tool(base, "remove_entry", entry="job interview").endswith("(Job interview).")                 # exact wins
    assert interview not in tool(base, "list_recent")
    assert "no journal entry on 2026-02-01" in tool_error(base, "change_entry", entry="2026-02-01", mood=2)


JRNL = {"tags": {"@work": 1}, "entries": [
    {"title": "Standup", "body": "Talked with Jo about the plan.", "date": "2022-05-01", "time": "09:15",
     "tags": ["@work"], "starred": False},
    {"title": "Short one", "body": "", "date": "2022-05-02", "time": "10:00", "tags": [], "starred": True},
    {"title": "Bad date", "body": "x", "date": "the fifth", "time": "10:00"}]}
DAYONE = {"metadata": {"version": "1.0"}, "entries": [
    {"uuid": "A1B2", "creationDate": "2023-03-04T12:00:00Z", "text": "Hiked the ridge with Alex. #hike",
     "tags": ["Outdoors"], "starred": True, "timeZone": "Europe/London"},
    {"creationDate": "not a date", "text": "lost"},
    {"creationDate": "2023-03-05T12:00:00Z", "text": 42},
    {"creationDate": "2023-03-06T12:00:00Z", "richText": "{}", "photos": [{}]}]}


def test_import_each_format_and_never_overwrite(base, data):
    code, out = call(base + "/api/import", "POST", JRNL)
    assert code == 200 and out["text"] == "Imported 2 entries. Skipped 0 already in the journal and 1 that could not be read."
    code, out = call(base + "/api/import", "POST", DAYONE)
    assert out["text"] == "Imported 1 entry. Skipped 0 already in the journal and 3 that could not be read."
    hike = call(base + "/list?words=ridge")[1]["entries"][0]
    assert hike["created"].startswith("2023-03-0") and hike["tags"] == ["outdoors", "hike"] and hike["starred"]
    standup = call(base + "/list?tag=work")[1]["entries"][0]
    assert standup["created"] == "2022-05-01T09:15:00" and standup["title"] == "Standup"
    assert call(base + "/api/import", "POST", JRNL)[1]["text"].startswith("Imported 0 entries. Skipped 2 already")
    # Journal's own export comes back in with its ids; an id that is already here is skipped, not overwritten
    tool(base, "add_entry", body="Mine", mood=4, prompt="What made you smile today?")
    export = call(base + "/export.json")[1]
    assert export["app"] == "journal" and len(export["entries"]) == 4
    mine = next(e for e in export["entries"] if e["body"] == "Mine")
    path = next((data / "entries").rglob(f"{mine['id']}.json"))
    before = path.read_bytes()
    changed = {"app": "journal", "entries": [{**mine, "body": "Overwritten?"}]}
    assert call(base + "/api/import", "POST", changed)[1]["text"].startswith("Imported 0 entries. Skipped 1 already")
    assert path.read_bytes() == before
    assert call(base + "/api/import", "POST", {"something": "else"})[0] == 400
    assert call(base + "/api/import", "POST", [1, "two", None])[1]["text"].endswith("3 that could not be read.")


def test_export_round_trip_and_markdown_zip(base, tmp_path, clock):
    tool(base, "add_entry", body="Line one\n\n**bold** and #tagged", title='Say "hi"', mood=3, starred=True,
         prompt="What surprised you today?")
    tool(base, "add_entry", body="Second", day="2025-12-31")
    export = call(base + "/export.json")[1]
    code, zipped = call(base + "/export.zip")
    names = sorted(zipfile.ZipFile(io.BytesIO(zipped)).namelist())
    assert code == 200 and len(names) == 2 and names[0].startswith("journal/2025/12/")
    md = zipfile.ZipFile(io.BytesIO(zipped)).read(names[1]).decode()
    assert md.startswith("---\nid: ") and 'title: "Say \\"hi\\""' in md and "mood: 3\n" in md
    assert 'tags: ["tagged"]' in md and "starred: true" in md and md.endswith("---\n\nLine one\n\n**bold** and #tagged\n")
    with running(js.make_server(0, tmp_path / "other", TOKEN, clock)) as other:
        assert call(other + "/api/import", "POST", export)[1]["text"].startswith("Imported 2 entries.")
        again = call(other + "/export.json")[1]
    assert again["entries"] == export["entries"]


def test_page_routes(base):
    assert call(base + "/health") == (200, "ok")
    code, page = call(base + "/")
    assert code == 200 and "getJ('updated')" in page and "http://" not in page and "https://" not in page
    assert len(re.findall(r"'[^']{10,}'", page.split("const PROMPTS=[")[1].split("];")[0])) >= 40
    code, out = call(base + "/api/add", "POST", {"body": "From the page #tablet", "mood": 4, "tags": ["x"]})
    assert code == 200 and out["id"] and out["last_updated"]
    eid = out["id"]
    assert call(base + "/updated")[1]["last_updated"] == out["last_updated"]
    lst = call(base + "/list?day=today")[1]
    assert lst["total"] == 1 and lst["entries"][0]["tags"] == ["x", "tablet"]
    assert call(base + "/list?mood=4&starred=false&month=2026-03")[1]["total"] == 1
    assert call(base + "/list?month=2026-04")[1]["total"] == 0
    assert call(base + "/api/change", "POST", {"id": eid, "starred": True, "mood": 0})[0] == 200
    stats = call(base + "/stats")[1]
    assert stats["count"] == 1 and stats["streak"] == 1 and stats["tags"] == [["tablet", 1], ["x", 1]]
    assert call(base + "/index")[1]["entries"][0]["starred"] is True
    assert call(base + "/api/remove", "POST", {"id": eid})[0] == 200
    assert call(base + "/stats")[1]["count"] == 0
    assert call(base + "/nothing")[0] == 404


def test_bad_input_is_refused_plainly(base):
    assert "mood is" in tool_error(base, "add_entry", body="x", mood=9)
    assert "mood is" in tool_error(base, "add_entry", body="x", mood="sideways")
    assert "needs some words" in tool_error(base, "add_entry", body="   ")
    assert "day is a date" in tool_error(base, "add_entry", body="x", day="someday")
    assert "did not fit" in tool_error(base, "add_entry", body="x", colour="red")
    assert call(base + "/api/add", "POST", {"body": ""})[0] == 400
    assert call(base + "/api/add", "POST", ["not", "an", "object"])[0] == 400
    assert call(base + "/list?month=March")[0] == 400 and call(base + "/list?limit=0")[0] == 400
    # a huge number, as a float and as an integer, is a plain refusal
    for raw in (b'{"body":"x","mood":1e999}', b'{"body":"x","mood":' + b"9" * 5000 + b"}",
                b'{"body":"x","mood":NaN}', b'{"body":"x","mood":1e308}'):
        code, out = call(base + "/api/add", "POST", raw=raw)
        assert code == 400 and "error" in out, raw[:40]
    code, out = call(base + "/mcp", "POST", raw=b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                     b'{"name":"list_recent","arguments":{"count":1e999}}}')
    assert code == 400 and out["error"]["code"] == -32700
    code, out = mcp(base, "tools/call", {"name": "list_recent", "arguments": {"count": 1e300}})
    assert code == 200 and out["result"]["isError"] and "count is a whole number" in out["result"]["content"][0]["text"]
    # deeply nested JSON
    deep = b"[" * 100000 + b"]" * 100000
    assert call(base + "/api/add", "POST", raw=deep)[0] == 400
    assert call(base + "/mcp", "POST", raw=b'{"a":' * 40000 + b"1" + b"}" * 40000)[0] == 400
    # wrong-shaped MCP values
    for params in ([], "text", 5, True):
        code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params})
        assert code == 200 and out["error"]["code"] == -32602, params
    code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": ["x"]})
    assert out["error"]["code"] == -32602
    code, out = mcp(base, "tools/call", {"name": ["add_entry"], "arguments": {}})
    assert out["error"]["code"] == -32602
    code, out = mcp(base, "tools/call", {"name": "add_entry", "arguments": ["body"]})
    assert out["result"]["isError"] and "arguments must be an object" in out["result"]["content"][0]["text"]
    code, out = call(base + "/mcp", "POST", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert out["error"]["code"] == -32600
    assert call(base + "/mcp", "POST", raw=b"\xff\xfe not utf8")[0] == 400
    # a body over the cap, and a bad Content-Length
    assert call(base + "/api/add", "POST", raw=b'{"body":"' + b"a" * (300 * 1024) + b'"}')[0] == 400
    conn = http.client.HTTPConnection(base[7:].split(":")[0], int(base.rsplit(":", 1)[1]), timeout=10)
    conn.putrequest("POST", "/api/add")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", "-5")
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()
    assert call(base + "/health")[1] == "ok"                     # still serving after all of it


def test_unreadable_and_empty_files_are_never_written_over(data, clock):
    month = data / "entries" / "2026" / "03"
    month.mkdir(parents=True)
    bad, empty = month / "20260301-090000-aaaa.json", month / "20260302-090000-bbbb.json"
    bad.write_text("{not json", encoding="utf-8")
    empty.write_bytes(b"")
    (data / "index.json").write_bytes(b"")                       # an empty index is moved aside, not written over
    with running(js.make_server(0, data, TOKEN, clock)) as base:
        stats = call(base + "/stats")[1]
        assert stats["count"] == 0 and len(stats["unreadable"]) == 2 and "is empty" in " ".join(stats["unreadable"])
        assert "2 files in the journal folder will not read" in tool(base, "journal_stats")
        assert "no entry" in tool_error(base, "change_entry", entry="20260302-090000-bbbb", body="new")
        eid = new_id(tool(base, "add_entry", body="A good one"))
        good = next(month.glob(f"{eid}.json"))
        good.write_bytes(b"")                                    # emptied while running: changes are refused
        assert "is empty. Journal will not write over it." in tool_error(base, "change_entry", entry=eid, mood=3)
        assert call(base + "/api/change", "POST", {"id": eid, "body": "x"})[0] == 409
        assert good.read_bytes() == b""
    assert bad.read_text(encoding="utf-8") == "{not json" and empty.read_bytes() == b""
    assert len(list(data.glob("index.unreadable-*.json"))) == 1


def test_an_id_with_a_trailing_newline_is_not_an_id(base, data):
    assert js.ID_RE.fullmatch("20260202-120000-abcd") and not js.ID_RE.fullmatch("20260202-120000-abcd\n")
    item = {"id": "20260202-120000-abcd\n", "created": "2026-02-02T12:00:00", "body": "Newline id"}
    code, out = call(base + "/api/import", "POST", {"app": "journal", "entries": [item]})
    assert code == 200 and out["text"].startswith("Imported 1 entry."), out
    saved = [p.stem for p in (data / "entries").rglob("*.json")]
    assert len(saved) == 1 and saved[0] != "20260202-120000-abcd" and js.ID_RE.fullmatch(saved[0])
    assert json.loads((data / "index.json").read_text(encoding="utf-8"))["count"] == 1


def test_import_skips_an_item_that_fails_to_save_and_keeps_the_index_true(data, clock, monkeypatch):
    store = js.Store(data)
    app = js.Journal(store, clock)
    real_put = js.Store.put

    def put(self, e, new):
        if e["body"] == "breaks":
            raise OSError("disk said no")
        return real_put(self, e, new)

    monkeypatch.setattr(js.Store, "put", put)
    items = [{"created": f"2026-01-0{i}T10:00:00", "body": b} for i, b in enumerate(("one", "breaks", "two"), 1)]
    text = app.import_data(items)
    assert text == ("Imported 2 entries. Skipped 0 already in the journal and 0 that could not be read. "
                    "Skipped 1 more entry that could not be saved (OSError).")
    index = json.loads((data / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == len(store.entries) == len(list((data / "entries").rglob("*.json"))) == 2


def test_remove_refuses_a_file_that_became_unreadable(base, data):
    eid = new_id(tool(base, "add_entry", body="Keep me"))
    path = next((data / "entries").rglob(f"{eid}.json"))
    path.write_bytes(b"")
    assert "is empty. Journal will not write over it." in tool_error(base, "remove_entry", entry=eid)
    assert call(base + "/api/remove", "POST", {"id": eid})[0] == 409
    path.write_text("{not json", encoding="utf-8")
    assert "will not read" in tool_error(base, "remove_entry", entry=eid)
    assert path.read_text(encoding="utf-8") == "{not json" and call(base + "/stats")[1]["count"] == 1


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/stats"), ("GET", "/list"),
                                         ("GET", "/export.json"), ("GET", "/export.zip"), ("POST", "/mcp"),
                                         ("POST", "/api/add"), ("POST", "/api/import"), ("PUT", "/index"),
                                         ("DELETE", "/index"), ("HEAD", "/updated")])
def test_every_route_needs_the_token(base, method, path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method in ("POST", "PUT") else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401
    req = urllib.request.Request(base + path, method=method, headers={"X-PuffPAI-Key": TOKEN})
    if method in ("GET", "HEAD"):
        with _open(req, timeout=10) as r:
            assert r.status == 200


# ---------------------------------------------------------------- the page's Markdown renderer

HARMFUL = [
    "<img src=x onerror=alert(1)>",
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x]( javascript:alert(1))",
    "<script>alert(1)</script>",
    '"><svg onload=alert(1)>',
    '[x](https://example.org" onmouseover="alert(1))',
    "![a](https://example.org/p.png)",
    "![a](x\" onerror=\"alert(1))",
    "[x][evil]\n\n[evil]: javascript:alert(1)",
    "`<b onclick=x>`",
    "```\n<script>alert(1)</script>\n```",
    "- <iframe src=x>\n- two",
    "# <a href=javascript:alert(1)>hi</a>",
    "[x](data:text/html,<script>alert(1)</script>)",
    "&lt;img src=x onerror=alert(1)&gt;",
]
OK_TAGS = {"em", "strong", "s", "br", "hr", "pre", "code", "blockquote", "ul", "ol", "li", "a",
           "h1", "h2", "h3", "h4", "h5", "h6"}


def _render(texts):
    page = (PKG / "web" / "index.html").read_text(encoding="utf-8")
    code = page[page.index("/* md-start"):page.index("/* md-end */")]
    script = code + "\nprocess.stdout.write(JSON.stringify(JSON.parse(require('fs').readFileSync(0,'utf8')).map(renderMd)));"
    run = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(texts), capture_output=True,
                         text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not on this computer")
def test_markdown_renderer_keeps_entry_text_harmless():
    for text, html in zip(HARMFUL, _render(HARMFUL)):
        for m in re.finditer(r"<(/?)([a-zA-Z0-9]+)([^>]*)>", html):
            assert m.group(2).lower() in OK_TAGS, (text, html)
            attrs = m.group(3).strip().rstrip("/").strip()
            pairs = re.findall(r'([a-z]+)="([^"]*)"', attrs)
            assert re.sub(r'[a-z]+="[^"]*"', "", attrs).strip() == "", (text, html)
            for name, value in pairs:
                assert name in ("href", "target", "rel", "class"), (text, html)
                if name == "href":
                    assert re.match(r"https?://[^\s\"<>]+$", value), (text, html)
        assert not re.search(r"<(img|script|svg|iframe)", html, re.I), (text, html)
    good = _render(["**bold** and _it_", "# Head", "- a\n- b", "[site](https://example.org/a?b=1&c=2)"])
    assert good[0] == "<strong>bold</strong> and <em>it</em>"
    assert good[1] == "<h1>Head</h1>" and good[2] == "<ul><li>a</li><li>b</li></ul>"
    assert good[3] == '<a href="https://example.org/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">site</a>'


# ---------------------------------------------------------------- the sweep: names, slow patterns, files, bodies

HOSTILE = ["1" + " " * 199_998 + "x", "# " + " " * 199_997 + "y", "a" * 199_999 + "!"]
NEEDED = {"body": "x", "entry": "latest"}


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
        head = (f"POST {path} HTTP/1.1\r\nHost: journal.invalid\r\nContent-Type: application/json\r\n"
                f"Content-Length: {length}\r\n" + (f"Authorization: Bearer {token}\r\n" if token else "") + "\r\n")
        s.sendall(head.encode())
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
                top, sep, rest = got.partition(b"\r\n\r\n")
                if sep and b"content-length:" in top.lower():
                    n = int(top.lower().split(b"content-length:")[1].split(b"\r\n")[0])
                    if len(rest) >= n:
                        break
        except OSError:
            pass
    status = int(got.split(b" ")[1]) if got.startswith(b"HTTP/") else 0
    return status, got.partition(b"\r\n\r\n")[2].decode("utf-8", "replace"), time.monotonic() - start


def test_partial_match_is_whole_words_only(base, data):
    tool(base, "add_entry", title="Lunch with Sharon", body="Soup.", day="2026-03-01")
    assert "no journal entry with 'Ron' in its title" in tool_error(base, "read_entry", entry="Ron")
    tool(base, "add_entry", title="Call Ron", body="He is well.", day="2026-03-02")
    tool(base, "add_entry", title="Walk with Jo", body="Cold.", day="2026-03-03")
    assert tool(base, "read_entry", entry="Ron").startswith("Using 'Call Ron' (2026-03-02).")
    assert "no journal entry with 'haro'" in tool_error(base, "read_entry", entry="haro")          # inside a word
    assert "no journal entry with 'lu'" in tool_error(base, "read_entry", entry="lu")              # 2 letters
    assert tool(base, "read_entry", entry="lun").startswith("Using 'Lunch with Sharon' (2026-03-01).")
    before = {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")}
    out = tool_error(base, "change_entry", entry="with", mood=2)
    assert "More than one entry matches 'with'" in out and "Lunch with Sharon" in out and "Walk with Jo" in out
    assert {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")} == before


def test_an_accented_word_is_one_word_and_matches_with_or_without_its_accents(base, data):
    for title in ("Pesto Park", "Sliders Lane", "Berry Clinic", "Joseph Day", "José Alvarez", "Café Luna"):
        tool(base, "add_entry", title=title, body="Notes.", day="2026-03-01")
    before = {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")}
    # an accented letter is never a gap, so the pieces of a word find nothing else
    for typed in ("crêpes", "müsli", "über", "Renée"):
        assert f"no journal entry with {typed!r} in its title" in tool_error(base, "remove_entry", entry=typed)
    out = tool_error(base, "remove_entry", entry="Jose")                         # never just Joseph Day
    assert "More than one entry matches 'Jose'" in out and "Joseph Day" in out and "José Alvarez" in out
    assert {p: p.read_text(encoding="utf-8") for p in (data / "entries").rglob("*.json")} == before
    # a title kept with accents is found typed with them or without them
    assert not tool(base, "read_entry", entry="José Alvarez").startswith("Using")         # exact
    assert tool(base, "read_entry", entry="jose alvarez").startswith("Using 'José Alvarez' (2026-03-01).")
    assert "(Café Luna)" in tool(base, "remove_entry", entry="CAFE")
    assert len(list((data / "entries").rglob("*.json"))) == 5


def test_hostile_text_answers_within_a_second_on_every_argument(base, data, clock):
    tool(base, "add_entry", title="Dishes", body="Done.")
    journal = js.Journal(js.Store(data.parent / "side"), clock)      # the patterns, called in this process
    journal.add_entry(body="x", title="Dishes")
    for name, _, props, req in js.TOOLS:
        for arg in props:
            for bad in HOSTILE:
                for value in (bad, [bad]):                            # the list arguments too
                    args = {**{r: NEEDED[r] for r in req}, arg: value}
                    start = time.monotonic()
                    try:
                        getattr(journal, name)(**args)
                    except (TypeError, ValueError, js.Unreadable):
                        pass
                    assert time.monotonic() - start < 1, (name, arg, bad[:3])
    jobs = []
    for name, _, props, req in js.TOOLS:
        for arg in props:
            for bad in HOSTILE:
                jobs.append(("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": {**{r: NEEDED[r] for r in req}, arg: bad}}})))
    for bad in HOSTILE:                                                  # and the import route
        jobs.append(("/api/import", json.dumps({"entries": [
            {"created": bad, "body": bad, "title": bad, "tags": [bad], "prompt": bad},
            {"created": "2026-03-01T10:00:00", "body": bad[:90_000], "title": bad, "tags": [bad], "prompt": bad},
            {"date": bad, "time": bad, "title": bad, "body": bad, "tags": [bad]},
            {"creationDate": "2023-03-04T12:00:00Z", "text": bad[:90_000] + " #" + bad[:9_000], "tags": [bad]}]})))
    for path, body in jobs:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=raw_post(base, len(body.encode()), body.encode(), path=path)))
        t.start()
        assert health_ok(base)
        t.join(60)
        status, text, took = out["r"]
        assert status in (200, 400) and took < 1, (path, body[:80], status, took)


@pytest.mark.parametrize("content", ['{}', '{"app": "other"}', '{"app": "other", "entries": []}',
                                     '{"entries": [], "count": 0, "lists": []}'])
def test_a_foreign_index_is_never_written_over(data, clock, content):
    data.mkdir(parents=True)
    (data / "index.json").write_text(content, encoding="utf-8")
    with running(js.make_server(0, data, TOKEN, clock)) as base:
        out = tool_error(base, "add_entry", body="Hello")
        assert "index.json" in out and "not this app's file" in out
        assert call(base + "/api/add", "POST", {"body": "Hello"})[0] == 409
        assert call(base + "/api/import", "POST", {"entries": [{"created": "2026-03-01T10:00:00", "body": "x"}]})[0] == 409
    assert (data / "index.json").read_text(encoding="utf-8") == content
    assert not list((data / "entries").rglob("*.json"))


def test_a_foreign_entry_file_is_never_written_over_and_an_old_one_gets_the_marker(data, clock):
    month = data / "entries" / "2026" / "03"
    month.mkdir(parents=True)
    old = {"id": "20260301-090000-aaaa", "created": "2026-03-01T09:00:00", "updated": "2026-03-01T09:00:00",
           "title": "Old walk", "body": "Fine.", "mood": None, "tags": [], "starred": False, "prompt": ""}
    (month / "20260301-090000-aaaa.json").write_text(json.dumps(old), encoding="utf-8")
    other = {**old, "id": "20260302-090000-bbbb", "title": "Other app", "app": "other"}
    extra = {**old, "id": "20260303-090000-cccc", "title": "Extra keys", "lists": []}
    foreign = {month / "20260302-090000-bbbb.json": json.dumps(other), month / "20260303-090000-cccc.json": json.dumps(extra)}
    for p, text in foreign.items():
        p.write_text(text, encoding="utf-8")
    with running(js.make_server(0, data, TOKEN, clock)) as base:
        stats = call(base + "/stats")[1]
        assert stats["count"] == 1 and len(stats["unreadable"]) == 2
        assert "not this app's file" in " ".join(stats["unreadable"])
        assert "no journal entry with 'other app'" in tool_error(base, "change_entry", entry="other app", mood=2)
        tool(base, "change_entry", entry="old walk", mood=4)
    assert json.loads((month / "20260301-090000-aaaa.json").read_text(encoding="utf-8"))["app"] == "journal"
    assert all(p.read_text(encoding="utf-8") == text for p, text in foreign.items())


def test_bodies_wrong_token_too_big_and_short(base):
    status, _, took = raw_post(base, 1000, b"{" * 10, token="wrong")                 # wrong token, body never ends
    assert status == 401 and took < 3
    status, _, took = raw_post(base, 100 * 1024 * 1024, b"x" * 70_000, token="wrong")  # wrong token, big body
    assert status == 401 and took < 3
    status, text, took = raw_post(base, 20 * 1024 * 1024, b" " * (20 * 1024 * 1024), path="/api/import")
    assert status == 400 and "MB" in text and took < 20                               # over the cap: drained, 400
    status, text, took = raw_post(base, 1000, b"{" * 10)                               # shorter than it says
    assert status == 400 and took < 6.5
    assert call(base + "/health") == (200, "ok")


def test_an_entry_titled_last_wins_over_the_word_last(base, data):
    tool(base, "add_entry", body="an old day", title="Last", day="2026-02-01")
    tool(base, "add_entry", body="a newer day", title="newest one")
    out = tool(base, "remove_entry", entry="Last")
    assert "newest one" not in str(out)
    assert "a newer day" in str(tool(base, "read_entry", entry="latest"))
