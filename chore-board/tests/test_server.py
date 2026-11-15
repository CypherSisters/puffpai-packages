"""Chore Board's own server (chore-board/server.py): the tools, the page routes and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import contextlib
import importlib.util
import json
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
TOKEN = "test-token-" + "c" * 20
DAY = date(2026, 3, 2)                  # a Monday; the server's "today" in these tests

_spec = importlib.util.spec_from_file_location("chore_server", PKG / "server.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


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
    return tmp_path / "data" / "chore-board"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def base(data, clock):
    with running(cs.make_server(0, data, TOKEN, clock)) as url:
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


def household(base):
    for name in ("Alex", "Sam", "Jo"):
        tool(base, "add_person", name=name)


def test_tool_list_matches_the_manifest_word_for_word(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert len(tools) <= 8
    assert {n: t["description"] for n, t in tools.items()} == manifest["abilities"]["tools"]
    assert set(tools) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])
    assert [n for n, t in tools.items() if t["annotations"]["destructiveHint"]] == ["remove_chore_or_person"]


def test_people_and_due_today_and_week(base):
    household(base)
    assert "already in the household" in tool_error(base, "add_person", name="alex")
    assert tool(base, "list_due") == "Nothing is due today."
    tool(base, "add_chore", name="Dishes", repeat="daily", assign_to="Alex")
    tool(base, "add_chore", name="Trash", repeat="weekly", assign_to="Sam, Jo", due_on="2026-03-05")
    tool(base, "add_chore", name="Windows", repeat="every 14 days", assign_to="Jo", due_on="2026-03-20")
    tool(base, "add_chore", name="Fix the gate", due_on="2026-02-27")                 # once, nobody, late
    today = tool(base, "list_due", when="today")
    assert today.startswith("Due today (2026-03-02), 2 chores:")
    assert "- Fix the gate (nobody yet), once, 3 days late" in today and "- Dishes (Alex), daily" in today
    week = tool(base, "list_due", when="week")
    assert "Due this week (2026-03-02 to 2026-03-08), 3 chores:" in week
    assert "- Thu 2026-03-05: Trash (Sam's turn; rotation Sam, Jo), weekly" in week and "Windows" not in week
    assert tool(base, "list_due", when="week", person="jo") == "Nothing is due this week (2026-03-02 to 2026-03-08) for Jo."
    people = tool(base, "list_people")
    assert people.startswith("3 people in the household:")
    assert "- Jo: Windows (due Fri 2026-03-20); 0 chores done in the last 7 days" in people


def test_mark_done_moves_the_due_date_and_the_rotation(base, clock):
    household(base)
    tool(base, "add_chore", name="Trash", repeat="weekly", assign_to=["Alex", "Sam", "Jo"])
    assert tool(base, "mark_done", chore="trash") == \
        "Marked Trash done by Alex on 2026-03-02. Next due Mon 2026-03-09, Sam's turn."
    # Jo covers Sam's turn a day late; the rotation still moves on from Sam
    clock.day = DAY + timedelta(days=8)
    assert tool(base, "mark_done", chore="Trash", by="Jo", on="yesterday") == \
        "Marked Trash done by Jo on 2026-03-09. Next due Mon 2026-03-16, Jo's turn."
    assert tool(base, "mark_done", chore="Trash").endswith("Next due Tue 2026-03-17, Alex's turn.")   # back to the first
    tool(base, "add_chore", name="Water plants", repeat="every", every_days=3, assign_to="Sam")
    assert "Next due Fri 2026-03-13." in tool(base, "mark_done", chore="Water plants")
    tool(base, "add_chore", name="Fix the gate", assign_to="nobody")
    assert "on nobody yet, so say who did it" in tool_error(base, "mark_done", chore="Fix the gate")
    assert "it is finished" in tool(base, "mark_done", chore="Fix the gate", by="Sam")
    assert "already done" in tool_error(base, "mark_done", chore="Fix the gate", by="Sam")
    assert "day still to come" in tool_error(base, "mark_done", chore="Trash", on="tomorrow")


def test_tally_counts_7_and_30_days(base, clock):
    household(base)
    tool(base, "add_chore", name="Dishes", repeat="daily", assign_to="Alex")
    tool(base, "add_chore", name="Laundry", repeat="weekly", assign_to="Sam")
    tool(base, "mark_done", chore="Dishes", on="2026-02-10")                      # 21 days back: 30 only
    tool(base, "mark_done", chore="Dishes")
    tool(base, "mark_done", chore="Dishes", by="Jo")
    tool(base, "mark_done", chore="Laundry")
    out = tool(base, "show_tally")
    assert out == ("Last 7 days: Alex 1 (Dishes 1); Jo 1 (Dishes 1); Sam 1 (Laundry 1).\n"
                   "Last 30 days: Alex 2 (Dishes 2); Jo 1 (Dishes 1); Sam 1 (Laundry 1).")
    clock.day = DAY + timedelta(days=40)
    assert tool(base, "show_tally") == ("Last 7 days: no chores marked done.\n"
                                        "Last 30 days: no chores marked done.")


def test_change_and_remove(base, data):
    household(base)
    tool(base, "add_chore", name="Trash", repeat="weekly", assign_to="Alex, Sam, Jo")
    tool(base, "mark_done", chore="Trash")                                         # Sam's turn now
    assert tool(base, "change_chore", chore="Trash", assign_to="Jo", repeat="every 2 days") == \
        "Changed Trash (repeat, who it is on): every 2 days, next due Mon 2026-03-09, on Jo."
    tool(base, "change_chore", chore="Trash", assign_to="Alex, Sam, Jo")
    tool(base, "mark_done", chore="Trash")                                         # Sam's turn
    out = tool(base, "remove_chore_or_person", person="Sam")
    assert out.startswith("Removed Sam from the household and from every chore they were on.")
    assert "Jo's turn; rotation Alex, Jo" in tool(base, "list_due", when="week")   # the turn passed on
    assert "Sam 1" not in tool(base, "show_tally") and "Alex 2 (Trash 2)" in tool(base, "show_tally")
    assert "say one thing to remove" in tool_error(base, "remove_chore_or_person")
    assert tool(base, "remove_chore_or_person", chore="trash").startswith("Removed the chore Trash.")
    saved = json.loads((data / "chores.json").read_text(encoding="utf-8"))
    assert saved["chores"] == [] and [p["name"] for p in saved["people"]] == ["Alex", "Jo"]
    assert len(saved["done"]) == 2 and saved["last_updated"]


def test_chores_and_people_are_found_by_name(base, data):
    household(base)
    tool(base, "add_chore", name="Wash dishes", repeat="daily", assign_to="Alex")
    tool(base, "add_chore", name="Dry dishes", repeat="daily", assign_to="Sam")
    tool(base, "add_chore", name="Take out the trash", repeat="weekly", assign_to="Jo")
    assert tool(base, "mark_done", chore="WASH  dishes").startswith("Marked Wash dishes done by Alex")   # exact
    assert tool(base, "mark_done", chore="trash").startswith("Using 'Take out the trash'. Marked Take out the trash "
                                                            "done by Jo")                         # partial
    before = (data / "chores.json").read_text(encoding="utf-8")
    out = tool_error(base, "change_chore", chore="dishes", notes="x")
    assert "More than one chore matches 'dishes': Wash dishes, Dry dishes. Say which." in out
    assert (data / "chores.json").read_text(encoding="utf-8") == before                  # nothing changed
    out = tool_error(base, "mark_done", chore="vacuum")
    assert "no chore called 'vacuum' (the chores: Wash dishes, Dry dishes, Take out the trash)" in out
    assert tool(base, "change_chore", chore="dry", assign_to="jo").startswith("Using 'Dry dishes'. Changed")
    assert "no person called 'sa'" in tool_error(base, "remove_chore_or_person", person="sa")     # 2 letters: no
    assert tool(base, "remove_chore_or_person", person="sam").startswith("Removed Sam from")
    tool(base, "add_chore", name="Dry dishes", repeat="weekly")
    assert "Say which." in tool_error(base, "mark_done", chore="dry dishes", by="Jo")       # same name twice: ids
    chore_id = json.loads((data / "chores.json").read_text(encoding="utf-8"))["chores"][-1]["id"]
    assert "(id " + chore_id in tool_error(base, "mark_done", chore="dry dishes", by="Jo")
    assert tool(base, "mark_done", chore=chore_id, by="Jo").startswith("Marked Dry dishes done by Jo")


def test_page_routes(base):
    assert call(base + "/health") == (200, "ok")
    code, page = call(base + "/")
    assert code == 200 and "BASE+'updated'" in page and "http://" not in page and "https://" not in page
    code, out = call(base + "/api/person/add", "POST", {"name": "Alex"})
    assert code == 200 and out["id"] and out["last_updated"]
    alex = out["id"]
    assert call(base + "/api/person/add", "POST", {"name": "Sam"})[0] == 200
    code, out = call(base + "/api/add", "POST", {"name": "Dishes", "repeat": "daily", "assign_to": "Alex, Sam"})
    assert code == 200 and out["id"]
    dishes = out["id"]
    board = call(base + "/board")[1]
    assert board["today"] == "2026-03-02" and board["chores"][0]["when"] == "today"
    assert board["chores"][0]["on_name"] == "Alex" and board["chores"][0]["rotation_names"] == ["Alex", "Sam"]
    assert call(base + "/api/done", "POST", {"id": dishes, "by": alex})[0] == 200
    board = call(base + "/board")[1]
    assert board["chores"][0]["when"] == "week" and board["chores"][0]["on_name"] == "Sam"
    assert [(p["name"], p["done_7"], p["done_30"]) for p in board["people"]] == [("Alex", 1, 1), ("Sam", 0, 0)]
    assert call(base + "/api/update", "POST", {"id": dishes, "notes": "hot water"})[0] == 200
    assert call(base + "/api/person/rename", "POST", {"id": alex, "name": "Alexa"})[0] == 200
    assert call(base + "/updated")[1]["last_updated"] == call(base + "/data")[1]["last_updated"]
    assert call(base + "/api/remove", "POST", {"id": dishes})[0] == 200
    assert call(base + "/api/person/remove", "POST", {"id": alex})[0] == 200
    doc = call(base + "/data")[1]
    assert doc["chores"] == [] and [p["name"] for p in doc["people"]] == ["Sam"]


def test_bad_input_is_refused_plainly(base):
    household(base)
    assert "repeat is once, daily, weekly" in tool_error(base, "add_chore", name="x", repeat="sometimes")
    assert "every_days is a whole number" in tool_error(base, "add_chore", name="x", repeat="every", every_days=0)
    assert "due_on is a date" in tool_error(base, "add_chore", name="x", due_on="soonish")
    assert "no person called 'Pat'" in tool_error(base, "add_chore", name="x", assign_to="Alex, Pat")
    assert "names each person once" in tool_error(base, "add_chore", name="x", assign_to="Alex, alex")
    assert "no chore called" in tool_error(base, "mark_done", chore="nothing")
    assert "when is today or week" in tool_error(base, "list_due", when="month")
    assert call(base + "/api/add", "POST", {"name": ""})[0] == 400


def test_unreadable_file_is_never_written_over(data, clock):
    data.mkdir(parents=True)
    (data / "chores.json").write_text("{not json", encoding="utf-8")
    with running(cs.make_server(0, data, TOKEN, clock)) as base:
        assert call(base + "/data")[0] == 500
        assert "will not write over it" in tool_error(base, "add_person", name="Alex")
    assert (data / "chores.json").read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/board"), ("POST", "/mcp"),
                                         ("POST", "/api/add"), ("PUT", "/data")])
def test_every_route_needs_the_token(base, method, path):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method != "GET" else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401


# ---------------------------------------------------------------- the sweep: names, slow patterns, files, bodies

HOSTILE = ["1" + " " * 199_998 + "x", "# " + " " * 199_997 + "y", "a" * 199_999 + "!"]
FITS = ["1" + " " * 60_000 + "x", "# " + " " * 60_000 + "y", "a" * 60_000 + "!"]     # under the 64 KB body cap


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
        head = (f"POST {path} HTTP/1.1\r\nHost: chores.invalid\r\nContent-Type: application/json\r\n"
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
    tool(base, "add_person", name="Sharon")
    assert "no person called 'Ron'" in tool_error(base, "list_due", person="Ron")          # never inside a word
    tool(base, "add_person", name="Ron")
    assert tool(base, "list_due", person="Ron") == "Nothing is due today for Ron."           # exact
    assert tool(base, "list_due", person="sha").startswith("Using 'Sharon'.")               # 3-letter prefix
    assert "no person called 'Sh'" in tool_error(base, "list_due", person="Sh")              # 2 letters: no
    tool(base, "add_chore", name="Wash dishes", repeat="daily", assign_to="Ron")
    tool(base, "add_chore", name="Dry dishes", repeat="daily", assign_to="Ron")
    tool(base, "add_chore", name="Take out the trash", repeat="weekly", assign_to="Ron")
    assert "no chore called 'ash'" in tool_error(base, "mark_done", chore="ash")             # inside Wash and trash
    assert "no chore called 'tr'" in tool_error(base, "mark_done", chore="tr")
    assert tool(base, "mark_done", chore="tra").startswith("Using 'Take out the trash'. Marked Take out the trash")
    assert tool(base, "mark_done", chore="wash dish").startswith("Using 'Wash dishes'. Marked")
    before = (data / "chores.json").read_text(encoding="utf-8")
    assert "More than one chore matches 'dishes': Wash dishes, Dry dishes" in tool_error(base, "mark_done", chore="dishes")
    assert (data / "chores.json").read_text(encoding="utf-8") == before


def test_hostile_text_answers_within_a_second_on_every_argument(base, data, clock):
    household(base)
    tool(base, "add_chore", name="Dishes", repeat="daily", assign_to="Alex")
    board = cs.Board(cs.Store(data), clock)                    # the same data, called in this process
    for name, _, props, req in cs.TOOLS:
        for arg in props:
            for bad in HOSTILE:
                args = {**{r: "Dishes" for r in req}, arg: bad}
                start = time.monotonic()
                try:
                    getattr(board, name)(**args)
                except (TypeError, ValueError):
                    pass
                assert time.monotonic() - start < 1, (name, arg, bad[:3])
    for name, _, props, req in cs.TOOLS:                      # and over HTTP, with /health answering meanwhile
        for arg in props:
            for bad in HOSTILE + FITS:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": name, "arguments": {**{r: "Dishes" for r in req}, arg: bad}}})
                out = {}
                t = threading.Thread(target=lambda: out.update(r=raw_post(base, len(body), body.encode())))
                t.start()
                assert health_ok(base)
                t.join(60)
                status, text, took = out["r"]
                assert status in (200, 400) and took < 1, (name, arg, status, took)


@pytest.mark.parametrize("content", ['{}', '{"app": "other"}', '{"people": [], "chores": [], "lists": []}',
                                     '{"app": "other", "people": [], "chores": [], "done": [], "last_updated": ""}'])
def test_a_foreign_data_file_is_never_written_over(data, clock, content):
    data.mkdir(parents=True)
    (data / "chores.json").write_text(content, encoding="utf-8")
    with running(cs.make_server(0, data, TOKEN, clock)) as base:
        out = tool_error(base, "add_person", name="Alex")
        assert "chores.json" in out and "not this app's file" in out
        assert call(base + "/api/person/add", "POST", {"name": "Alex"})[0] == 409
    assert (data / "chores.json").read_text(encoding="utf-8") == content


def test_a_file_from_this_version_still_loads_and_gets_the_marker(data, clock):
    data.mkdir(parents=True)
    old = {"people": [{"id": "p1", "name": "Alex", "added": ""}], "chores": [], "done": [], "last_updated": "x"}
    (data / "chores.json").write_text(json.dumps(old), encoding="utf-8")
    with running(cs.make_server(0, data, TOKEN, clock)) as base:
        tool(base, "add_person", name="Sam")
    saved = json.loads((data / "chores.json").read_text(encoding="utf-8"))
    assert saved["app"] == "chore-board" and [p["name"] for p in saved["people"]] == ["Alex", "Sam"]


def test_bodies_wrong_token_too_big_and_short(base):
    status, _, took = raw_post(base, 1000, b"{" * 10, token="wrong")               # wrong token, body never ends
    assert status == 401 and took < 3
    status, _, took = raw_post(base, 10 * 1024 * 1024, b"x" * 70_000, token="wrong")  # wrong token, big body
    assert status == 401 and took < 3
    status, text, took = raw_post(base, 2 * 1024 * 1024, b" " * (2 * 1024 * 1024))    # over the cap: drained, 400
    assert status == 400 and "KB" in text and took < 20
    status, text, took = raw_post(base, 1000, b"{" * 10)                             # shorter than it says
    assert status == 400 and took < 6.5
    assert call(base + "/health") == (200, "ok")


class _People(BaseHTTPRequestHandler):
    answer = b'[{"id": "u1", "name": "Alex"}, {"id": "u2", "name": "Pat"}]'
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.endswith("/slow/v1/people"):
            time.sleep(3)
        if self.path.endswith("/hop/v1/people"):
            self.send_response(302)
            self.send_header("Location", "/v1/people")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.answer)))
        self.end_headers()
        self.wfile.write(self.answer)


def test_the_core_people_list_is_read_and_merged(data, clock):
    _People.seen = []
    with running(ThreadingHTTPServer(("127.0.0.1", 0), _People)) as api:
        with running(cs.make_server(0, data, TOKEN, clock, api=api)) as base:
            tool(base, "add_person", name="Sam")
            people = tool(base, "list_people")
            assert people.startswith("3 people") and "- Pat:" in people and "- Sam:" in people
            assert "already in the household" in tool_error(base, "add_person", name="pat")
            tool(base, "add_chore", name="Trash", repeat="weekly", assign_to="Pat, Sam")
            assert tool(base, "mark_done", chore="Trash").startswith("Marked Trash done by Pat")
            assert {p["name"] for p in call(base + "/board")[1]["people"]} == {"Sam", "Alex", "Pat"}
        assert _People.seen[0] == ("/v1/people", f"Bearer {TOKEN}") and len(_People.seen) == 1   # kept 60 s
        for where in (api + "/hop", api + "/slow"):
            people = cs.People(where, TOKEN)
            start = time.monotonic()
            assert people.list() == [] and time.monotonic() - start < 2.6
    for where in ("http://chores.invalid:9", "https://127.0.0.1:9", "ftp://localhost:9", "http://user:pw@127.0.0.1:9",
                  "", None):
        assert cs.People(where, TOKEN).where is None, where
    assert cs.People("http://[::1]:9", TOKEN).where == ("::1", 9, "")


def test_marking_done_twice_with_an_earlier_day_never_moves_the_due_date_back(base, clock):
    household(base)
    tool(base, "add_chore", name="Dishes", repeat="weekly", assign_to="Alex")
    assert "Next due Mon 2026-03-09" in tool(base, "mark_done", chore="Dishes")
    assert "Next due Mon 2026-03-09" in tool(base, "mark_done", chore="Dishes", on="2026-02-27")


def test_page_tick_boxes_and_the_every_box():
    page = (PKG / "web" / "index.html").read_text(encoding="utf-8")
    assert "[hidden]{display:none!important}" in page                       # the Every box really hides
    assert "form.add .picks label{" in page and "flex-direction:row" in page   # tick box beside the name
    assert 'aria-label="${esc(p.name)}"' in page
