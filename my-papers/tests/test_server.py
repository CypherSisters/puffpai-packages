"""My Papers' own server (my-papers/server.py): the tools, the page routes, files, search, dates and every route's token.

Every server here listens on a free port on 127.0.0.1 and is stopped by the test that started it."""

import base64
import contextlib
import http.client
import importlib.util
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "p" * 20
DAY = date(2026, 3, 2)                  # the server's "today" in these tests

_spec = importlib.util.spec_from_file_location("papers_server", PKG / "server.py")
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

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
    return tmp_path / "data" / "my-papers"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(params=[True, False], ids=["fts5", "plain"])
def fts(request):
    if request.param and not ps.fts_available():
        pytest.skip("this Python has no FTS5")
    return request.param


@pytest.fixture
def base(data, clock):
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: ["Alex", "Sam", "Jo"])) as url:
        yield url


def call(url, method="GET", body=None, token=TOKEN, raw=None, ctype="application/json"):
    h = {"Content-Type": ctype}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _open(req, timeout=20) as r:
            content = r.read()
            kind = r.headers.get("Content-Type", "")
            return r.status, (json.loads(content) if "json" in kind else content.decode() if "text" in kind else content)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        return e.code, (json.loads(text) if text.startswith("{") else text)


def head(url, token=TOKEN):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with _open(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


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


def add_file(base, name, content: bytes, **fields):
    return call(base + "/api/add", "POST", {**fields, "file": {"name": name, "data": base64.b64encode(content).decode()}})


def small_pdf(lines) -> bytes:
    """A one-page PDF whose text sits in a FlateDecode content stream."""
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({t}) Tj 0 -16 Td" for t in lines) + " ET"
    stream = zlib.compress(ops.encode("latin-1"))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    for i, o in enumerate(objs, 1):
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    return out + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def a_zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


# ------------------------------------------------------------------ the tools

def test_tool_list_matches_the_manifest_word_for_word(base):
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert len(tools) <= 8
    assert {n: t["description"] for n, t in tools.items()} == manifest["abilities"]["tools"]
    assert set(tools) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])
    assert [n for n, t in tools.items() if t["annotations"]["destructiveHint"]] == ["remove_paper"]
    assert manifest["license"] == "Apache-2.0" and manifest["author"] == "Cypher Sisters"
    assert manifest["needs"]["hosts"] == [] and "brain" not in manifest["needs"]
    assert "owner_files" not in manifest["needs"]


def test_words_rules_in_every_file():
    for path in PKG.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".md", ".json", ".html", ".svg"):
            text = path.read_text(encoding="utf-8")
            assert "\u2014" not in text, path
            assert "sem" + "pe" not in text.lower(), path


# ------------------------------------------------------------------ adding

def test_add_a_record_and_a_file(base, data):
    out = tool(base, "add_paper_record", title="Car title", type="vehicle", binder="blue binder", people="alex")
    assert out == "Added Car title (vehicle). The paper copy is in blue binder."
    content = b"City Water\nAccount 1234\nAmount due 82.50 by April 15\n"
    code, out = add_file(base, "water-march.txt", content,
                         type="bill", **{"from": "City Water"}, due="2026-04-15")
    assert code == 200 and "found" in out["text"]
    p = call(base + "/paper/" + out["id"])[1]
    assert p["title"] == "water-march" and p["file"]["kind"] == "text" and p["file"]["size"] == len(content)
    assert "Account 1234" in p["text"] and p["text_note"] == ""
    assert (data / "files" / p["file"]["sha256"]).is_file()
    car = tool(base, "find_papers", words="car")
    assert "Car title (vehicle" in car
    assert "People: Alex" in tool(base, "read_paper", paper="car title")      # named as the shared list names them


def test_the_same_file_twice_is_stored_once(base, data):
    content = b"Home insurance policy 77\n"
    a = add_file(base, "policy.txt", content)[1]
    b = add_file(base, "copy of policy.txt", content)[1]
    assert a["id"] != b["id"]
    assert len(list((data / "files").iterdir())) == 1
    assert "stays: another paper uses it" in tool(base, "remove_paper", paper=a["id"])
    assert "Its stored file was deleted too." in tool(base, "remove_paper", paper=b["id"])
    assert list((data / "files").iterdir()) == []


def test_text_found_in_a_pdf_and_html_and_none_in_a_picture(base):
    pdf = small_pdf(["Lease agreement for 12 Oak Lane", "Pets allowed: one cat (with deposit)", "Rent due on the 1st"])
    out = add_file(base, "lease.pdf", pdf, type="lease")[1]
    p = call(base + "/paper/" + out["id"])[1]
    assert p["type"] == "contract or lease" and p["file"]["kind"] == "pdf"
    assert p["text"].splitlines() == ["Lease agreement for 12 Oak Lane", "Pets allowed: one cat (with deposit)",
                                      "Rent due on the 1st"]
    read = tool(base, "read_paper", paper="lease")
    assert "Pets allowed: one cat (with deposit)" in read
    page = add_file(base, "note.html", b"<html><script>alert(1)</script><p>School trip &amp; form</p></html>")[1]
    assert call(base + "/paper/" + page["id"])[1]["text"] == "School trip & form"
    pic = add_file(base, "scan.jpg", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4)[1]
    assert ps.NO_TEXT in pic["text"]
    assert call(base + "/paper/" + pic["id"])[1]["text_note"] == ps.NO_TEXT


def test_unsafe_file_names_are_handled_safely(base, data):
    out = add_file(base, "..\\..\\x", b"plain words here")[1]
    p = call(base + "/paper/" + out["id"])[1]
    assert p["file"]["name"] == "x" and not (data.parent.parent / "x").exists()
    out = add_file(base, "<script>.html", b"<script>alert(1)</script><b>hello there</b>")[1]
    p = call(base + "/paper/" + out["id"])[1]
    assert p["file"]["name"] == "<script>.html" and "alert" not in p["text"]
    assert sorted(x.name for x in (data / "files").iterdir()) == sorted(
        [call(base + "/paper/" + i["id"])[1]["file"]["sha256"] for i in call(base + "/papers")[1]["papers"]])
    status, headers, body = head(base + "/file/" + out["id"])
    assert status == 200 and headers["Content-Type"] == "application/octet-stream"
    assert headers["Content-Disposition"].startswith('attachment; filename="_script_.html"')
    assert headers["X-Content-Type-Options"] == "nosniff" and body == b"<script>alert(1)</script><b>hello there</b>"


def test_injection_in_a_title_is_kept_as_text_and_the_page_escapes(base):
    title = '<img src=x onerror="alert(1)">'
    out = tool(base, "add_paper_record", title=title)
    assert title in out
    found = call(base + "/papers")[1]["papers"]
    assert found[0]["title"] == title
    page = call(base + "/")[1]
    assert "function esc(" in page and "http://" not in page and "https://" not in page
    assert "${p.title}" not in page and "${esc(p.title)}" in page


# ------------------------------------------------------------------ search

def test_search_and_filters(data, clock, fts):
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: None, fts=fts)) as base:
        add_file(base, "w1.txt", b"Water usage for January\nTotal 40.00", title="Water Jan", type="bill",
                 **{"from": "City Water"}, date="2025-01-20", people=["Sam"], tags="house")
        add_file(base, "w2.txt", b"Water usage for February\nTotal 41.00", title="Water Feb", type="bill",
                 **{"from": "City Water"}, date="2026-02-20", tags="house, water")
        tool(base, "add_paper_record", title="Passport", type="id", people="Jo", notes="In the safe, top shelf")
        assert call(base + "/papers")[1]["fts"] is fts
        hits = call(base + "/papers?q=water")[1]["papers"]
        assert {h["title"] for h in hits} == {"Water Jan", "Water Feb"}
        assert call(base + "/papers?q=febr")[1]["papers"][0]["line"] == "Water usage for February"   # word start
        assert [h["title"] for h in call(base + "/papers?q=water&year=2025")[1]["papers"]] == ["Water Jan"]
        assert [h["title"] for h in call(base + "/papers?person=sam")[1]["papers"]] == ["Water Jan"]
        assert [h["title"] for h in call(base + "/papers?tag=water")[1]["papers"]] == ["Water Feb"]
        assert [h["title"] for h in call(base + "/papers?type=identity")[1]["papers"]] == ["Passport"]
        assert call(base + "/papers?q=nothing+here")[1]["papers"] == []
        out = tool(base, "find_papers", words="safe")
        assert out.startswith("1 paper found:") and '"In the safe, top shelf"' in out
        assert call(base + "/info")[1]["people_source"] == "own"
        assert call(base + "/info")[1]["people"] == ["Jo", "Sam"]


def test_the_index_is_built_again_from_the_papers(data, clock):
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        tool(base, "add_paper_record", title="Boiler warranty", type="warranty")
    (data / "index.sqlite").write_bytes(b"this is not a database")
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        assert "Boiler warranty" in tool(base, "find_papers", words="boiler")


# ------------------------------------------------------------------ dates

def test_coming_up_with_due_expiry_renewal_and_overdue(base, clock):
    tool(base, "add_paper_record", title="Phone bill", type="bill", due="2026-03-10")
    tool(base, "add_paper_record", title="Old bill", type="bill", due="2026-02-20")
    tool(base, "add_paper_record", title="Passport", type="identity", expires="2026-04-20")
    tool(base, "add_paper_record", title="Car insurance", type="insurance", date="2025-09-15", renews_every_months=6)
    tool(base, "add_paper_record", title="School form", type="school", follow_up="2026-03-02")
    tool(base, "add_paper_record", title="Far away", type="bill", due="2026-08-01")
    items = call(base + "/coming")[1]["items"]
    assert [(i["title"], i["what"], i["date"]) for i in items] == [
        ("Old bill", "due", "2026-02-20"), ("School form", "follow up", "2026-03-02"),
        ("Phone bill", "due", "2026-03-10"), ("Car insurance", "renews", "2026-03-15"),
        ("Passport", "expires", "2026-04-20")]
    assert items[0]["overdue"] and not items[1]["overdue"]
    out = tool(base, "coming_up")
    assert "- 2026-02-20 (10 days overdue): Old bill due" in out and "- 2026-03-02 (today): School form follow up" in out
    assert "Far away" in tool(base, "coming_up", days=200)
    clock.day = date(2026, 3, 16)                          # the renewal moves on six months
    assert "- 2026-09-15 (in 183 days): Car insurance renews" in tool(base, "coming_up", days=200)
    assert tool(base, "change_paper", paper="Old bill", due="").startswith("Changed Old bill (due).")
    assert "Old bill" not in tool(base, "coming_up")


def test_keep_until_hint_for_tax_and_keep_or_shred(base, clock):
    out = tool(base, "add_paper_record", title="Tax return 2021", type="tax", date="2022-04-10")
    assert "Keep until 2025-04-10 (suggested). Most tax records: keep 3 years. Some need longer." in out
    tool(base, "add_paper_record", title="Tax return 2024", type="tax", date="2025-04-10")
    tool(base, "add_paper_record", title="Old receipt", type="receipt", keep_until="2026-01-01", binder="box 3")
    p = call(base + "/papers?q=2021")[1]["papers"][0]
    assert call(base + "/paper/" + p["id"])[1]["keep_hint"] == ps.TAX_HINT
    out = tool(base, "keep_or_shred")
    assert out.startswith("2 papers past the keep-until date. This is a list only; nothing was deleted.")
    assert "- Old receipt (warranty or receipt), keep until 2026-01-01, paper copy in box 3" in out
    assert "Tax return 2024" not in out
    assert len(call(base + "/papers")[1]["papers"]) == 3                    # nothing deleted


def test_change_and_counts(base):
    tool(base, "add_paper_record", title="Vet record", type="pet")
    tool(base, "add_paper_record", title="Lease", type="contract")
    out = tool(base, "change_paper", paper="vet record", due="2026-05-01", amount="120.5", currency="usd",
               sender="Happy Paws Vet", tags="cat, shots")
    assert out.startswith("Changed Vet record (from, due, tags, amount).") and "due 2026-05-01" in out
    read = tool(base, "read_paper", paper="Vet record")
    assert "Amount: 120.50 USD" in read and "From: Happy Paws Vet" in read and "Tags: cat, shots" in read
    out = tool(base, "list_types_and_counts")
    assert out.startswith("2 papers in all:") and "- pet: 1" in out and "- contract or lease: 1" in out


def test_papers_are_found_by_title(base, data):
    tool(base, "add_paper_record", title="City Water bill March", type="bill", date="2026-03-01")
    tool(base, "add_paper_record", title="City Water bill April", type="bill", date="2026-04-01")
    tool(base, "add_paper_record", title="Apartment lease", type="contract", notes="Pets allowed with a deposit.")
    tool(base, "add_paper_record", title="Car title", binder="blue binder")
    assert "Title: Apartment lease" in tool(base, "read_paper", paper="APARTMENT  lease")          # exact
    assert "Pets allowed" in tool(base, "read_paper", paper="lease")                               # unique partial
    assert tool(base, "change_paper", paper="car", binder="green binder").startswith(
        "Using 'Car title'. Changed Car title")
    before = call(base + "/papers")[1]["papers"]
    out = tool_error(base, "change_paper", paper="city water bill", due="2026-05-01")
    assert ("More than one paper matches 'city water bill': City Water bill April (2026-04-01), "
            "City Water bill March (2026-03-01). Say which.") in out
    assert "More than one paper matches" in tool_error(base, "remove_paper", paper="water")
    assert call(base + "/papers")[1]["papers"] == before                                          # nothing changed
    assert "no paper called 'passport'" in tool_error(base, "read_paper", paper="passport")
    assert tool(base, "remove_paper", paper="city water bill april").startswith("Removed the paper City Water bill April")
    assert "Title: City Water bill March" in tool(base, "read_paper", paper="city water")      # now only one
    pid = next(p["id"] for p in call(base + "/papers")[1]["papers"] if p["title"] == "Car title")
    assert f"Id: {pid}" in tool(base, "read_paper", paper=pid)                                  # an id still works


# ------------------------------------------------------------------ export and import

def test_export_then_import_round_trip(data, clock, tmp_path):
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        add_file(base, "w.txt", b"Water bill words", type="bill", due="2026-03-20", tags="house")
        tool(base, "add_paper_record", title="Car title", binder="blue binder")
        code, raw = call(base + "/export")
        assert code == 200 and isinstance(raw, bytes)
        names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
        assert "my-papers-export.json" in names and sum(n.startswith("papers/") for n in names) == 2
        assert sum(n.startswith("files/") for n in names) == 1
        out = call(base + "/api/import", "POST", raw=raw, ctype="application/zip")[1]
        assert (out["imported"], out["skipped"]) == (0, 2)                 # never written over
    other = tmp_path / "other"
    with running(ps.make_server(0, other, TOKEN, clock)) as base:
        out = call(base + "/api/import", "POST", raw=raw, ctype="application/zip")[1]
        assert (out["imported"], out["skipped"]) == (2, 0)
        assert "Water bill words" in tool(base, "read_paper", paper="w")
        assert "2026-03-20" in tool(base, "coming_up")
        assert len(list((other / "files").iterdir())) == 1


def test_import_a_folder_zip_with_sidecars(base):
    raw = a_zip({"bills/water.txt": "City Water statement", "bills/water.txt.json":
                 json.dumps({"title": "Water", "type": "bill", "due": "2026-03-05", "extra": "ignored"}),
                 "scan.png": b"\x89PNG....", "notes.json": '{"a": 1}'})
    out = call(base + "/api/import", "POST", raw=raw, ctype="application/zip")[1]
    assert (out["imported"], out["skipped"]) == (3, 0)
    assert "Water due" in tool(base, "coming_up")
    out = call(base + "/api/import", "POST", raw=raw, ctype="application/zip")[1]
    assert (out["imported"], out["skipped"]) == (0, 3)                     # the same files are skipped


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "C:/win.txt", "a/../../b.txt", "a\\..\\b.txt"])
def test_zip_path_tricks_are_refused(base, data, name):
    raw = a_zip({"fine.txt": "fine", name: "bad"})
    code, out = call(base + "/api/import", "POST", raw=raw, ctype="application/zip")
    assert code == 400 and "unsafe name" in out["error"]
    assert call(base + "/papers")[1]["papers"] == []


def test_huge_archives_and_files_are_refused(base, monkeypatch):
    monkeypatch.setattr(ps, "MAX_ZIP_ENTRIES", 3)
    code, out = call(base + "/api/import", "POST", raw=a_zip({f"{i}.txt": "x" for i in range(4)}), ctype="application/zip")
    assert code == 400 and "at most 3 files" in out["error"]
    monkeypatch.setattr(ps, "MAX_ZIP_TOTAL", 100)
    code, out = call(base + "/api/import", "POST", raw=a_zip({"big.txt": "x" * 200}), ctype="application/zip")
    assert code == 400 and "unpack to at most" in out["error"]
    code, out = call(base + "/api/import", "POST", raw=b"not a zip", ctype="application/zip")
    assert code == 400 and "not a zip" in out["error"]
    code, out = add_file(base, "big.bin", b"\0" * (ps.MAX_FILE + 10))
    assert code == 400 and "at most 20 MB" in out["error"]
    assert call(base + "/papers")[1]["papers"] == []


# ------------------------------------------------------------------ refusals and files on disk

def test_bad_input_is_refused_plainly(base):
    assert "type is one of" in tool_error(base, "add_paper_record", title="x", type="recipe")
    assert "due is a date" in tool_error(base, "add_paper_record", title="x", due="soonish")
    assert "amount must be a number below a trillion" in tool_error(base, "add_paper_record", title="x", amount=1e300)
    assert "renews_every_months is a whole number" in tool_error(base, "add_paper_record", title="x",
                                                                 renews_every_months=500)
    assert "no paper called" in tool_error(base, "read_paper", paper="nothing")
    assert "unknown field" in tool_error(base, "add_paper_record", title="x", colour="red")
    assert "days is a whole number" in tool_error(base, "coming_up", days=10 ** 15)
    assert call(base + "/api/add", "POST", {"title": ""})[0] == 400
    assert call(base + "/api/add", "POST", {"title": "x", "file": {"name": "a", "data": "!!"}})[0] == 400
    assert call(base + "/coming?days=abc")[0] == 400


def test_huge_numbers_deep_json_and_bad_params_get_plain_refusals(base):
    for raw in (b'{"title": "x", "amount": ' + b"9" * 5000 + b"}", b'{"title": "x", "amount": 1e999}',
                b"[" * 30000 + b"]" * 30000):
        code, out = call(base + "/api/add", "POST", raw=raw)
        assert code == 400 and "error" in out
        code, out = call(base + "/mcp", "POST", raw=raw)
        assert code == 400 and out["error"]["code"] == -32700
    for params in ([1, 2], "words", 7):
        code, out = call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
        assert code == 200 and out["error"]["code"] == -32602
    for args in ([1], "x"):
        code, out = mcp(base, "tools/call", {"name": "find_papers", "arguments": args})
        assert code == 200 and out["error"]["code"] == -32602
    code, out = mcp(base, "tools/call", {"name": ["find_papers"]})
    assert code == 200 and out["error"]["code"] == -32602
    assert call(base + "/health") == (200, "ok")                           # still running


@pytest.mark.parametrize("content", [b"{not json", b""], ids=["corrupt", "empty"])
def test_corrupt_and_empty_paper_files_are_never_written_over(data, clock, content):
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        pid = tool_id = call(base + "/api/add", "POST", {"title": "Lease"})[1]["id"]
    path = data / "papers" / f"{pid}.json"
    path.write_bytes(content)
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        info = call(base + "/info")[1]
        assert info["unreadable"] == [path.name] and info["total"] == 0
        assert "no paper called" in tool_error(base, "change_paper", paper=tool_id, notes="x")
        assert call(base + "/api/remove", "POST", {"id": pid})[0] == 404
        tool(base, "add_paper_record", title="Another")
        assert "would not read" in tool(base, "list_types_and_counts")
    assert path.read_bytes() == content


def test_a_paper_that_breaks_while_running_is_left_alone(base, data):
    pid = call(base + "/api/add", "POST", {"title": "Deed"})[1]["id"]
    path = data / "papers" / f"{pid}.json"
    path.write_bytes(b"")
    code, out = call(base + "/api/update", "POST", {"id": pid, "notes": "x"})
    assert code == 409 and "will not write over it" in out["error"]
    assert call(base + "/api/remove", "POST", {"id": pid})[0] == 409
    assert path.read_bytes() == b""


# ------------------------------------------------------------------ people

class _People(BaseHTTPRequestHandler):
    answer = (200, b'[{"id": "u1", "name": "Alex", "emoji": "", "role": "adult"}]')
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        code, body = self.answer
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_people_come_from_the_core_and_fall_back_to_the_own_list(data, clock):
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _People)
    with running(stub) as api:
        assert ps.fetch_people(api, "pkg-token") == ["Alex"]
        assert _People.seen[-1] == ("/v1/people", "Bearer pkg-token")
        _People.answer = (500, b"{}")
        assert ps.fetch_people(api, "pkg-token") is None
        assert ps.fetch_people(api, None) is None
    assert ps.fetch_people("http://127.0.0.1:1", "t") is None              # nothing listening
    assert ps.fetch_people("http://example.invalid", "t") is None          # only this computer is asked
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: None)) as base:
        tool(base, "add_paper_record", title="Report card", people="Jo, Sam")
        info = call(base + "/info")[1]
        assert info["people_source"] == "own" and info["people"] == ["Jo", "Sam"]


# ------------------------------------------------------------------ the page and the token

def test_page_routes(base):
    assert call(base + "/health") == (200, "ok")
    code, page = call(base + "/")
    assert code == 200 and "BASE+'updated'" not in page and "getj('updated')" in page
    info = call(base + "/info")[1]
    assert info["types"] == list(ps.TYPES) and info["people"] == ["Alex", "Sam", "Jo"] and info["fts"] in (True, False)
    pid = call(base + "/api/add", "POST", {"title": "Deed", "type": "home", "binder": "safe"})[1]["id"]
    assert call(base + "/api/update", "POST", {"id": pid, "due": "2026-03-04"})[0] == 200
    assert call(base + "/coming")[1]["items"][0]["id"] == pid
    assert call(base + "/updated")[1]["last_updated"] == call(base + "/info")[1]["last_updated"]
    assert call(base + "/file/" + pid)[0] == 404                           # a record has no file
    assert call(base + "/paper/../../etc")[0] == 404
    assert call(base + "/api/remove", "POST", {"id": pid})[0] == 200
    assert call(base + "/papers")[1]["papers"] == []


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/health"), ("GET", "/info"), ("GET", "/papers"),
                                         ("GET", "/coming"), ("GET", "/export"), ("GET", "/updated"),
                                         ("POST", "/mcp"), ("POST", "/api/add"), ("POST", "/api/import"),
                                         ("PUT", "/info"), ("GET", "/paper/FILE"), ("GET", "/file/FILE")])
def test_every_route_needs_the_token(base, method, path):
    pid = add_file(base, "a.txt", b"some words")[1]["id"]
    path = path.replace("FILE", pid)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method != "GET" else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401
    if path.startswith("/file/"):
        conn = http.client.HTTPConnection(base[7:], timeout=10)
        conn.request("GET", path, headers={"X-PuffPAI-Key": TOKEN})
        assert conn.getresponse().status == 200
        conn.close()


# ------------------------------------------------------------------ fixes: race, unreadable, big body, wording,
# ------------------------------------------------------------------ redirects, PDF budget, duplicates

def test_removing_a_paper_while_another_adds_the_same_file_keeps_the_file(base, data, monkeypatch):
    content = b"Lease for 12 Oak Lane, signed copy\n"
    c = add_file(base, "c.txt", content, title="Lease C")[1]
    entered, release = threading.Event(), threading.Event()
    real = ps.find_text

    def slow(name, raw):                                    # holds the add of D open while C is removed
        entered.set()
        assert release.wait(20)
        return real(name, raw)

    monkeypatch.setattr(ps, "find_text", slow)
    got = {}
    t = threading.Thread(target=lambda: got.update(d=add_file(base, "d.txt", content, title="Lease D")))
    t.start()
    try:
        assert entered.wait(20)
        code, out = call(base + "/api/remove", "POST", {"id": c["id"]})
        assert code == 200 and "stays" in out["text"]
        assert (data / "files" / ps.hashlib.sha256(content).hexdigest()).is_file()   # kept while D is added
    finally:
        release.set()
        t.join(60)
    code, d = got["d"]
    assert code == 200
    sha = call(base + "/paper/" + d["id"])[1]["file"]["sha256"]
    assert (data / "files" / sha).is_file()
    assert call(base + "/file/" + d["id"]) == (200, content)
    assert "Its stored file was deleted too." in tool(base, "remove_paper", paper=d["id"])
    assert list((data / "files").iterdir()) == []


def test_a_stored_file_is_kept_while_any_paper_file_will_not_read(data, clock):
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        a = add_file(base, "a.txt", b"words in a shared file", title="A")[1]
        b = add_file(base, "b.txt", b"words in a shared file", title="B")[1]
    (data / "papers" / f"{b['id']}.json").write_bytes(b"{broken")
    with running(ps.make_server(0, data, TOKEN, clock)) as base:
        code, out = call(base + "/api/remove", "POST", {"id": a["id"]})
        assert code == 200 and out["text"] == "Removed the paper A."
    assert len(list((data / "files").iterdir())) == 1


def test_an_upload_over_the_body_cap_gets_a_plain_answer(base, monkeypatch):
    monkeypatch.setattr(ps, "MAX_ADD_BODY", 1000)
    raw = b'{"title": "x", "file": {"name": "a.pdf", "data": "' + b"A" * 50_000 + b'"}}'
    code, out = call(base + "/api/add", "POST", raw=raw)
    assert code == 400 and out["error"] == "That file is larger than 20 MB."
    assert call(base + "/papers")[1]["papers"] == []
    assert "That file is larger than" in call(base + "/")[1]


def test_no_text_message_has_one_period(base):
    out = add_file(base, "scan.jpg", b"\xff\xd8\xff\xe0" + bytes(range(256)))[1]
    assert out["text"] == "Added scan (other) with the file scan.jpg. " + ps.NO_TEXT
    assert ".." not in out["text"]


class _Redirect(BaseHTTPRequestHandler):
    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_people_fetch_never_follows_a_redirect_or_leaves_this_computer():
    _People.answer = (200, b'[{"name": "Alex"}]')
    with running(ThreadingHTTPServer(("127.0.0.1", 0), _People)) as api:
        _Redirect.target = api + "/v1/people"
        with running(ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)) as hop:
            before = len(_People.seen)
            assert ps.fetch_people(hop, "pkg-token") is None
            assert len(_People.seen) == before                  # the token went nowhere else
        assert ps.fetch_people(api, "pkg-token") == ["Alex"]
        port = api.rsplit(":", 1)[1]
        assert ps.fetch_people(f"https://127.0.0.1:{port}", "t") is None
        assert ps.fetch_people(f"ftp://localhost:{port}", "t") is None
        assert ps.fetch_people(f"http://papers.invalid:{port}", "t") is None
        assert ps.fetch_people("http://[::1", "t") is None


def test_pdf_reading_stops_at_its_text_and_time_budgets(base, monkeypatch):
    pdf = small_pdf(["First line of the lease", "Second line of the lease", "Third line of the lease"])
    monkeypatch.setattr(ps, "PDF_TEXT", 10)
    out = add_file(base, "lease.pdf", pdf)[1]
    p = call(base + "/paper/" + out["id"])[1]
    assert p["text"] == "First line of the lease" and p["text_note"] == ps.PARTIAL
    assert out["text"].endswith(" " + ps.PARTIAL)
    assert ps.PARTIAL in tool(base, "read_paper", paper=out["id"])
    monkeypatch.setattr(ps, "PDF_TEXT", 2 * 1024 * 1024)
    monkeypatch.setattr(ps, "PDF_SECONDS", -1.0)
    assert ps.find_text("lease.pdf", pdf) == ("", ps.NO_TEXT + " " + ps.PARTIAL)
    monkeypatch.setattr(ps, "PDF_SECONDS", 3.0)
    assert ps.find_text("lease.pdf", pdf)[1] == ""             # a whole small PDF is read


def test_a_pdf_with_a_huge_amount_of_text_is_cut_short_in_time():
    ops = b"BT /F1 12 Tf 72 720 Td " + (b"(" + b"word " * 20 + b") Tj T* ") * 40_000 + b"ET"
    stream = zlib.compress(ops)
    pdf = (b"%%PDF-1.4\n4 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream
           + b"\nendstream\nendobj\n%%EOF\n")
    started = ps.time.monotonic()
    text, note = ps.find_text("big.pdf", pdf)
    assert ps.time.monotonic() - started < ps.PDF_SECONDS + 2
    assert note == ps.PARTIAL and text.startswith("word word")


def test_the_same_record_is_not_added_twice(base, data):
    first = tool(base, "add_paper_record", title="Car title", type="vehicle", binder="blue binder")
    assert first.startswith("Added Car title")
    again = tool(base, "add_paper_record", title="Car title", type="vehicle", binder="blue binder")
    assert again == "That paper is already there: Car title. Nothing was added."
    assert call(base + "/api/add", "POST", {"title": "car title", "type": "vehicle"})[1]["text"] == again
    assert len(call(base + "/papers")[1]["papers"]) == 1
    assert tool(base, "add_paper_record", title="Car title", type="vehicle", notes="the old car").startswith("Added")
    assert add_file(base, "car.txt", b"Car title for the blue car", title="Car title", type="vehicle")[0] == 200
    assert len(call(base + "/papers")[1]["papers"]) == 3


def test_a_record_added_over_10_minutes_ago_can_be_added_again(data, clock):
    app = ps.Papers(data, clock, fts=False)
    a = app.add_paper_record(title="Lease", type="lease")
    assert "already there" in app.add_paper_record(title="Lease", type="lease")["text"]
    app.papers[a["id"]]["added"] = (ps.datetime.now() - timedelta(minutes=11)).isoformat(timespec="seconds")
    assert app.add_paper_record(title="Lease", type="lease")["text"].startswith("Added Lease")
    assert len(app.papers) == 2


def test_tool_words_steer_a_small_model(base):
    tools = {t["name"]: t["description"] for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert tools["find_papers"].startswith("Use only to search, when the owner asks if we have a paper, where one "
                                           "is, or what papers there are: ")
    assert tools["add_paper_record"].startswith("File a new paper record whenever the owner says to file")
    for name in ("read_paper", "change_paper", "remove_paper"):
        assert "by its title; it finds the paper itself, so do not look it up first." in tools[name]
    asks = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))["abilities"]["examples"]
    assert {"do we have anything from City Water", "where is the car title"} <= set(asks["find_papers"])
    assert asks["read_paper"][0] == "read me the City Water bill"
    assert asks["add_paper_record"][0].startswith("file this: ")


# ---------------------------------------------------------------- the sweep: names, slow patterns, files, bodies

import base64 as _b64          # noqa: E402
import io as _io               # noqa: E402
import socket as _socket       # noqa: E402
import time as _time           # noqa: E402
import zipfile as _zipfile     # noqa: E402
from http.server import BaseHTTPRequestHandler as _BaseHandler  # noqa: E402

HOSTILE = ["1" + " " * 199_998 + "x", "# " + " " * 199_997 + "y", "a" * 199_999 + "!"]
FITS = ["1" + " " * 60_000 + "x", "a" * 60_000 + "!"]                  # under the 64 KB body cap
HOSTILE_FILES = {"a.html": "<script" * 30_000, "b.html": "<!--" * 50_000, "c.html": "<br" * 70_000,
                 "d.html": "<" * 200_000, "e.html": "<style>" + "</style " * 25_000, "f.txt": "\n" * 200_000,
                 "g.pdf": "%PDF-1.4\n1 0 obj<<>>stream\n" + "(" * 200_000 + "\nendstream"}
NEEDED = {"title": "Water", "paper": "Water"}


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
            conn = _socket.create_connection(("127.0.0.1", port), timeout=3)
            break
        except OSError:
            if attempt == 3:
                raise
    conn.settimeout(wait)
    start = _time.monotonic()                  # the time the server takes, from a made connection
    got = b""
    with conn as s:
        top = (f"POST {path} HTTP/1.1\r\nHost: papers.invalid\r\nContent-Type: application/json\r\n"
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
    return status, got.partition(b"\r\n\r\n")[2].decode("utf-8", "replace"), _time.monotonic() - start


def test_partial_match_is_whole_words_only(base, data):
    tool(base, "add_paper_record", title="Letter from Sharon Lee", type="other")
    assert "no paper called 'Ron'" in tool_error(base, "read_paper", paper="Ron")             # never inside a word
    tool(base, "add_paper_record", title="Ron school report", type="other")
    tool(base, "add_paper_record", title="Letter from the bank", type="other")
    assert tool(base, "read_paper", paper="Ron").startswith("Using 'Ron school report'.")
    assert "no paper called 'chool'" in tool_error(base, "read_paper", paper="chool")         # inside a word
    assert "no paper called 'sc'" in tool_error(base, "read_paper", paper="sc")               # 2 letters
    assert tool(base, "change_paper", paper="sch", binder="red").startswith("Using 'Ron school report'. Changed")
    before = {p: p.read_bytes() for p in (data / "papers").glob("*.json")}
    out = tool_error(base, "remove_paper", paper="letter")
    assert "More than one paper matches 'letter'" in out and "Sharon" in out and "bank" in out
    assert {p: p.read_bytes() for p in (data / "papers").glob("*.json")} == before


def test_hostile_text_answers_within_a_second_on_every_argument(base, data, clock):
    tool(base, "add_paper_record", title="Water", type="bill")
    papers = ps.Papers(data.parent / "side", clock, lambda: None, fts=False)     # the patterns, in this process
    papers.add({"title": "Water"})
    for name, _, props, req in ps.TOOLS:
        for arg in props:
            for bad in HOSTILE:
                for value in (bad, [bad]):
                    args = {**{r: NEEDED[r] for r in req}, arg: value}
                    start = _time.monotonic()
                    try:
                        getattr(papers, name)(**args)
                    except (TypeError, ValueError, ps.Unreadable):
                        pass
                    assert _time.monotonic() - start < 1, (name, arg, bad[:3])
    for fname, text in HOSTILE_FILES.items():                                    # text read out of files
        start = _time.monotonic()
        ps.find_text(fname, text.encode())
        assert _time.monotonic() - start < 1, fname
    jobs = []
    for name, _, props, req in ps.TOOLS:
        for arg in props:
            for bad in HOSTILE + FITS:
                jobs.append(("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": {**{r: NEEDED[r] for r in req}, arg: bad}}}).encode()))
    for fname, text in HOSTILE_FILES.items():                                    # uploads
        jobs.append(("/api/add", json.dumps({"title": "x", "file": {
            "name": fname, "data": _b64.b64encode(text.encode()).decode()}}).encode()))
    jobs.append(("/api/add", json.dumps({"title": HOSTILE[0], "file": {"name": HOSTILE[2], "data": ""}}).encode()))
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w") as z:                                        # and an import
        for fname, text in HOSTILE_FILES.items():
            z.writestr(fname, text)
            z.writestr(fname + ".json", json.dumps({"title": HOSTILE[1][:60_000], "notes": HOSTILE[2][:60_000]}))
    jobs.append(("/api/import", buf.getvalue()))
    for path, body in jobs:
        out = {}
        t = threading.Thread(target=lambda: out.update(r=raw_post(base, len(body), body, path=path)))
        t.start()
        assert health_ok(base)
        t.join(60)
        status, text, took = out["r"]
        limit = 3 * len(HOSTILE_FILES) if path == "/api/import" else 1    # an import reads every file in it
        assert status in (200, 400) and took < limit, (path, body[:80], status, took)


@pytest.mark.parametrize("content", ['{}', '{"app": "other", "id": "p0123456789ab", "title": "x"}',
                                     '{"id": "p0123456789ab", "title": "x", "lists": []}'])
def test_a_foreign_paper_file_is_never_written_over(data, clock, content):
    (data / "papers").mkdir(parents=True)
    path = data / "papers" / "p0123456789ab.json"
    path.write_text(content, encoding="utf-8")
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: None)) as base:
        assert "not this app's file" in tool(base, "list_types_and_counts")
        assert "no paper called 'x'" in tool_error(base, "change_paper", paper="x", binder="red")
        assert "no paper called 'x'" in tool_error(base, "remove_paper", paper="x")
    assert path.read_text(encoding="utf-8") == content


def test_a_paper_from_this_version_still_loads_and_gets_the_marker(data, clock):
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: None)) as base:
        tool(base, "add_paper_record", title="Water bill", type="bill")
    path = next((data / "papers").glob("*.json"))
    old = json.loads(path.read_text(encoding="utf-8"))
    old.pop("app", None)                                              # as the version before the marker wrote it
    path.write_text(json.dumps(old), encoding="utf-8")
    with running(ps.make_server(0, data, TOKEN, clock, people=lambda: None)) as base:
        tool(base, "change_paper", paper="Water bill", binder="red")
    assert json.loads(path.read_text(encoding="utf-8"))["app"] == "my-papers"


def test_bodies_wrong_token_too_big_and_short(base):
    status, _, took = raw_post(base, 1000, b"{" * 10, token="wrong")                    # wrong token, never ends
    assert status == 401 and took < 3
    status, _, took = raw_post(base, 300 * 1024 * 1024, b"x" * 70_000, token="wrong")    # wrong token, big body
    assert status == 401 and took < 3
    status, text, took = raw_post(base, 2 * 1024 * 1024, b" " * (2 * 1024 * 1024))       # over the cap: drained
    assert status == 400 and "KB" in text and took < 20
    status, text, took = raw_post(base, 1000, b"{" * 10)                                  # shorter than it says
    assert status == 400 and took < 6.5
    assert call(base + "/health")[0] == 200


class _SlowPeople(_BaseHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _time.sleep(2.6)
        body = b'[{"id": "u1", "name": "Alex"}]'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_the_core_people_list_has_one_deadline_and_a_short_failure_hold(data, clock):
    with running(ThreadingHTTPServer(("127.0.0.1", 0), _SlowPeople)) as api:
        start = _time.monotonic()
        assert ps.fetch_people(api, "pkg-token") is None                  # one 2 second deadline for all of it
        assert _time.monotonic() - start < 2.5
    calls = []
    papers = ps.Papers(data, clock, lambda: calls.append(1) or None, fts=False)
    papers.people()
    papers.people()
    assert len(calls) == 1 and papers._people_cache[1] is None
    papers._people_cache = (papers._people_cache[0] - 31, None)            # a failure is kept 30 seconds only
    papers.people()
    assert len(calls) == 2
