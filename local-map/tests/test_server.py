"""Local Map's own server and its PMTiles reader, all offline.

A tiny PMTiles v3 file is built here (header, root directory, leaf directories, a few gzip tiles)
and served by a stub on 127.0.0.1 that answers Range requests, with a stub builds.json and a stub
Overpass answer. The stub can be made hostile (a different archive, list or answer, or one byte a
second). Nothing here contacts a real internet host. Every server is stopped by the test that started it."""

import contextlib
import gzip
import importlib.util
import json
import math
import re
import socket
import struct
import threading
import time
import tracemalloc
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
TOKEN = "test-token-" + "m" * 20

_spec = importlib.util.spec_from_file_location("local_map_server", PKG / "server.py")
lm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lm)
fx = lm.fetcher

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

# The test region: 5 km around a point; the synthetic file holds zooms 0 to 4.
CENTER = (37.46, -122.43)
REGION = {"lat": CENTER[0], "lon": CENTER[1], "radius": 5, "unit": "km", "detail": "small", "label": "Test town"}
MB = 1024 * 1024
PAD = 3 * MB                             # two tiles this big make the whole download larger than a 5 MB cap


# ---------------------------------------------------------------- a tiny PMTiles v3 file

def pack(root: bytes, leaves: bytes = b"", data: bytes = b"", data_len=None, tile_comp=2) -> bytes:
    """A PMTiles v3 file around the given (already packed) directories and tile data, at least 16 KB long."""
    meta = gzip.compress(b"{}")
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    header = b"PMTiles" + bytes([3]) + struct.pack(
        "<11Q", root_off, len(root), meta_off, len(meta), leaf_off, len(leaves), data_off,
        len(data) if data_len is None else data_len, 1, 1, 1) + struct.pack("<6B", 1, 2, tile_comp, 1, 0, 12) + \
        struct.pack("<4i", -1800000000, -850000000, 1800000000, 850000000) + bytes([0]) + struct.pack("<2i", 0, 0)
    assert len(header) == 127
    out = header + root + meta + leaves + data
    return out + b"\0" * max(0, fx.FIRST_READ - len(out))


def build_archive(leaves: int = 1, not_gzip=None) -> tuple[bytes, dict]:
    """Zooms 0 to 12 around CENTER. z1 is one run of 4 equal tiles; zooms 10 to 12 sit in *leaves* leaf
    directories. The z12 and z10 tiles under CENTER are padded by 3 MB each, so one download is bigger than a
    5 MB cap. The tile *not_gzip* (z, x, y), if given, holds bytes that are not gzip."""
    tiles = {}
    for (z, x, y) in fx.tiles_for_box(CENTER[1] - 0.06, CENTER[0] - 0.06, CENTER[1] + 0.06, CENTER[0] + 0.06, 12):
        tiles[fx.zxy_to_tileid(z, x, y)] = (z, x, y)
    for x in range(2):
        for y in range(2):
            tiles[fx.zxy_to_tileid(1, x, y)] = (1, x, y)
    big = {(12, *fx.lonlat_to_tile(CENTER[1], CENTER[0], 12)), (10, *fx.lonlat_to_tile(CENTER[1], CENTER[0], 10))}
    data, entries, want = bytearray(), [], {}
    ocean = gzip.compress(b"ocean tile")
    run_start = fx.zxy_to_tileid(1, 0, 0)
    for tid in sorted(tiles):
        z, x, y = tiles[tid]
        if z == 1:
            want[(z, x, y)] = ocean
            if tid == run_start:
                entries.append((tid, len(data), len(ocean), 4))
                data += ocean
            continue
        body = gzip.compress(f"tile {z}/{x}/{y}".encode())
        if (z, x, y) in big:
            body += b"\0" * PAD
        if (z, x, y) == not_gzip:
            body = b"these bytes are not gzip"
        want[(z, x, y)] = body
        entries.append((tid, len(data), len(body), 1))
        data += body
    leaf_from = fx.zxy_to_tileid(10, 0, 0)
    root_entries = [e for e in entries if e[0] < leaf_from]
    leaf_entries = [e for e in entries if e[0] >= leaf_from]
    step = -(-len(leaf_entries) // leaves)
    blob = b""
    for i in range(0, len(leaf_entries), step):
        part = leaf_entries[i:i + step]
        leaf = gzip.compress(fx.encode_directory(part))
        root_entries.append((part[0][0], len(blob), len(leaf), 0))
        blob += leaf
    root = gzip.compress(fx.encode_directory(root_entries))
    return pack(root, blob, bytes(data)), want


ARCHIVE, TILE_BYTES = build_archive()

def in_region(radius=5):
    """The archive's tiles that cover the test region (zooms 0 to 12)."""
    b = server_box(radius)
    cover = set(fx.tiles_for_box(b["west"], b["south"], b["east"], b["north"], 12))
    return {t: v for t, v in TILE_BYTES.items() if t in cover}


def server_box(radius):
    return fx.snap_box(lm.box_around(CENTER[0], CENTER[1], radius))

OVERPASS = {"elements": [
    {"type": "node", "id": 1, "lat": 37.462, "lon": -122.431, "tags": {"amenity": "hospital", "name": "Coast Hospital",
                                                                        "phone": "+1 555 0100"}},
    {"type": "way", "id": 2, "center": {"lat": 37.45, "lon": -122.43}, "tags": {"amenity": "fire_station",
                                                                                 "name": "Station 40"}},
    {"type": "node", "id": 3, "lat": 37.47, "lon": -122.42, "tags": {"amenity": "drinking_water"}},
    {"type": "node", "id": 4, "lat": 37.461, "lon": -122.43, "tags": {"amenity": "bench"}},
    {"type": "node", "id": 5, "lat": 37.465, "lon": -122.44,
     "tags": {"amenity": "pharmacy", "name": "<img src=x onerror=alert(1)>"}},
]}


BUILDS = json.dumps([
    {"key": "20260101.pmtiles", "uploaded": "2026-01-01T00:00:00Z", "version": "4.1.0"},
    {"key": "20260102.pmtiles", "uploaded": "2026-01-02T00:00:00Z", "version": "4.1.1"},
]).encode()


class Stub:
    """builds.json, the archive (with Range) and Overpass, on 127.0.0.1. Counts every request.
    Hostile on demand: *archive*, *builds* and *overpass* can be swapped, and a path in *drip* is answered
    one byte a second."""

    def __init__(self):
        self.hits, self.overpass_code, self.delay, self.lock = [], 200, 0.0, threading.Lock()
        self.overpass_fails = []                 # error codes for the next places requests, one each, then overpass_code
        self.archive, self.builds, self.overpass = ARCHIVE, BUILDS, json.dumps(OVERPASS).encode()
        self.drip, self.agents, self.stop = set(), [], threading.Event()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, code, body, ctype="application/octet-stream", extra=None):
                with stub.lock:
                    stub.agents.append(self.headers.get("User-Agent"))
                if self.path.split("?")[0] in stub.drip:
                    return self._drip(code, len(body), ctype)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _drip(self, code, n, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                for _ in range(n):
                    if stub.stop.wait(1):
                        return
                    try:
                        self.wfile.write(b" ")
                    except OSError:
                        return

            def do_GET(self):
                with stub.lock:
                    stub.hits.append(("GET", self.path))
                if self.path == "/builds.json":
                    return self._out(200, stub.builds, "application/json")
                if self.path == "/20260102.pmtiles":
                    arc = stub.archive
                    m = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                    if not m:
                        return self._out(200, arc)
                    a, b = int(m.group(1)), min(int(m.group(2)), len(arc) - 1)
                    if stub.delay:
                        time.sleep(stub.delay)
                    return self._out(206, arc[a:b + 1], extra={"Content-Range": f"bytes {a}-{b}/{len(arc)}"})
                self._out(404, b"")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                with stub.lock:
                    stub.hits.append(("POST", self.path, body))
                    code = stub.overpass_fails.pop(0) if stub.overpass_fails else stub.overpass_code
                if code != 200:
                    return self._out(code, b"busy", "text/plain")
                self._out(200, stub.overpass, "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def sources(self):
        return fx.Sources(f"{self.url}/builds.json", f"{self.url}/", f"{self.url}/api/interpreter",
                          opener=fx.make_opener({}))

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


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
        server.app._close_tiles()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data" / "local-map"


@pytest.fixture
def srv(data, stub):
    server = lm.make_server(0, data, TOKEN, stub.sources())
    with running(server) as url:
        yield url, server.app


def call(url, method="GET", body=None, token=TOKEN, raw=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _open(req, timeout=10) as r:
            out = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, (json.loads(out) if "json" in ctype and out else out), dict(r.headers)
    except urllib.error.HTTPError as e:
        out = e.read()
        return e.code, (json.loads(out) if out.startswith(b"{") else out), dict(e.headers)


def mcp(base, method, params=None, token=TOKEN):
    return call(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, token)[:2]


def tool(base, name, /, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200 and not out["result"]["isError"], out
    return out["result"]["content"][0]["text"]


def tool_error(base, name, /, **args):
    code, out = mcp(base, "tools/call", {"name": name, "arguments": args})
    assert code == 200 and out["result"]["isError"], out
    return out["result"]["content"][0]["text"]


def wait(app, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        j = app.job.view()
        if j["state"] not in ("checking", "downloading") and not app.job.busy():
            return j
        time.sleep(0.05)
    raise AssertionError("the job did not finish in time")


def download(base, app, **extra):
    assert call(base + "/api/download", "POST", {**REGION, **extra})[0] == 200
    return wait(app)


# ---------------------------------------------------------------- the reader

def test_varints_and_hilbert_tile_ids():
    for n in (0, 1, 127, 128, 300, 2 ** 35 + 7):
        assert fx.read_varint(fx.write_varint(n), 0) == (n, len(fx.write_varint(n)))
    assert fx.read_varint(bytes([0xAC, 0x02]), 0) == (300, 2)
    for z, x, y, tid in [(0, 0, 0, 0), (1, 0, 0, 1), (1, 0, 1, 2), (1, 1, 1, 3), (1, 1, 0, 4), (2, 0, 0, 5),
                         (12, 3423, 1763, 19078479)]:
        assert fx.zxy_to_tileid(z, x, y) == tid and fx.tileid_to_zxy(tid) == (z, x, y)
    for z in range(0, 16):
        for x, y in ((0, 0), ((1 << z) - 1, 0), (0, (1 << z) - 1), ((1 << z) // 3, (1 << z) // 2)):
            assert fx.tileid_to_zxy(fx.zxy_to_tileid(z, x, y)) == (z, x, y)
    with pytest.raises(fx.FetchError):
        fx.read_varint(bytes([0x80]), 0)


def test_header_directories_runs_and_leaf(stub):
    ar = fx.Archive(stub.sources(), stub.url + "/20260102.pmtiles")
    h = ar.open()
    assert (h["tile_compression"], h["internal_compression"], h["tile_type"], h["max_zoom"]) == (2, 2, 1, 12)
    assert any(run == 0 for *_, run in ar.root) and any(run == 4 for *_, run in ar.root)
    ids = {fx.zxy_to_tileid(*t): t for t in TILE_BYTES}
    found = ar.locate(list(ids) + [fx.zxy_to_tileid(4, 0, 0)])           # one id the file does not hold
    assert set(found) == set(ids)
    for tid, (off, ln) in found.items():
        assert ARCHIVE[off:off + ln] == TILE_BYTES[ids[tid]]
    one = {found[fx.zxy_to_tileid(1, x, y)] for x in range(2) for y in range(2)}
    assert len(one) == 1                                                   # the run: 4 tiles, one blob
    assert len(ar._leaves) == 1                                            # the leaf directory was read once


def test_tiles_for_a_box_and_range_merging():
    tiles = fx.tiles_for_box(-122.52, 37.42, -122.38, 37.52, 12)
    assert (0, 0, 0) in tiles and len(tiles) == fx.count_tiles(-122.52, 37.42, -122.38, 37.52, 12)
    assert {t for t in tiles if t[0] == 12} == {(12, x, y) for x in range(653, 656) for y in range(1586, 1589)}
    assert fx.merge_ranges([(0, 10), (10, 5), (100, 5), (30, 5)], max_gap=0) == [(0, 15), (30, 5), (100, 5)]
    assert fx.merge_ranges([(0, 10), (12, 5)], max_gap=4) == [(0, 17)]
    assert fx.merge_ranges([(0, 10), (10, 10)], max_gap=0, max_len=15) == [(0, 10), (10, 10)]
    assert fx.merge_ranges([(5, 5), (5, 5)]) == [(5, 5)]


def test_the_tile_store_keeps_equal_tiles_once(tmp_path):
    st = fx.TileStore(tmp_path / "t.sqlite")
    st.put_many([(1, 0, 0, b"same"), (1, 1, 0, b"same"), (1, 0, 1, b"other")])
    assert st.counts() == (3, 2) and st.get(1, 1, 0) == b"same" and st.get(1, 1, 1) is None
    st.close()


# ---------------------------------------------------------------- the download, through the page routes

def test_dry_run_size_and_the_cap(srv, stub):
    base, app = srv
    assert call(base + "/api/check", "POST", REGION)[0] == 200
    j = wait(app)
    assert j["state"] == "checked", j
    want = in_region()
    assert j["plan"]["tiles"] == len(want) and j["plan"]["bytes"] >= sum(len(b) for b in set(want.values()))
    assert j["plan"]["bytes"] > 2 * PAD
    assert not any(h[0] == "POST" for h in stub.hits)                    # a dry run asks Overpass nothing
    assert not (app.store.folder / "tiles.sqlite").exists()
    assert call(base + "/api/settings", "POST", {"cap_mb": 5})[0] == 200
    assert call(base + "/api/check", "POST", {**REGION, "detail": "detailed"})[0] == 200
    j = wait(app)
    assert j["state"] == "too_big" and "more than your 5 MB limit. Choose Standard or Small" in j["message"]


def test_download_into_sqlite_tiles_route_and_resume(srv, stub):
    base, app = srv
    j = download(base, app)
    assert j["state"] == "done", j
    import sqlite3
    db = sqlite3.connect(app.store.folder / "tiles.sqlite")
    assert db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == len(in_region())
    assert db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == len(set(in_region().values()))
    db.close()
    assert not (app.store.folder / "tiles-partial.sqlite").exists()
    z11 = (11, *fx.lonlat_to_tile(CENTER[1], CENTER[0], 11))
    code, body, headers = call(base + "/tiles/%d/%d/%d.mvt" % z11)
    assert code == 200 and body == TILE_BYTES[z11] and headers["Content-Encoding"] == "gzip"
    assert headers["Content-Type"] == "application/vnd.mapbox-vector-tile"
    assert gzip.decompress(body) == ("tile %d/%d/%d" % z11).encode()
    assert call(base + "/tiles/3/0/0.mvt")[0] == 404
    assert call(base + "/tiles/3/9/0.mvt")[0] == 404
    assert call(base + "/tiles/a/0/0.mvt")[1]["error"]
    st = call(base + "/api/state")[1]
    assert st["region"]["max_zoom"] == 12 and st["region"]["tiles"] == len(in_region())
    assert {p["kind"] for p in st["points"]} == {"Hospital", "Fire station", "Drinking water", "Pharmacy"}
    assert st["home"]["lat"] == CENTER[0]                     # home starts at the region's centre, and says so
    assert st["home_note"] == lm.HOME_NOTE and lm.HOME_NOTE in j["message"]
    posts = [h for h in stub.hits if h[0] == "POST"]
    assert len(posts) == 1 and "nwr%5Bman_made%3Dwater_tap%5D" in posts[0][2]
    for side, value in server_box(5).items():                # the box sent is the snapped one
        assert f"{value:.5f}" in posts[0][2], side
    assert set(stub.agents) == {"local-map/1.0"}
    # Download again, same build, box and detail: it starts from a copy of the map in place, so no tile is
    # fetched again; the places are fetched again
    ranges = len([h for h in stub.hits if h[0] == "GET" and h[1] == "/20260102.pmtiles"])
    j = download(base, app)
    assert j["state"] == "done" and f"({len(in_region())} were already here)" in j["message"], j
    assert len([h for h in stub.hits if h[0] == "GET" and h[1] == "/20260102.pmtiles"]) == ranges
    assert len([h for h in stub.hits if h[0] == "POST"]) == 2


def test_cancel_keeps_the_old_map_and_resume_skips_stored_tiles(srv, stub, monkeypatch):
    base, app = srv
    monkeypatch.setattr(fx, "MAX_RANGE", 1)                    # one request per tile, so Cancel lands between them
    assert download(base, app)["state"] == "done"
    before = (app.store.folder / "tiles.sqlite").read_bytes()
    stub.delay = 0.4
    call(base + "/api/settings", "POST", {"cap_mb": 250})
    # a different box (radius 4 snaps to a smaller box; radius 6 snaps to the same box as 5): a new partial file
    assert call(base + "/api/download", "POST", {**REGION, "radius": 4})[0] == 200
    end = time.time() + 10
    while time.time() < end:
        if app.job.view()["state"] == "downloading" and app.job.view()["done_bytes"] > 0:
            break
        time.sleep(0.02)
    assert call(base + "/api/cancel", "POST", {})[0] == 200
    j = wait(app)
    assert j["state"] == "cancelled", j
    assert (app.store.folder / "tiles.sqlite").read_bytes() == before
    assert (app.store.folder / "tiles-partial.sqlite").exists()
    assert call(base + "/api/state")[1]["region"]["radius"] == 5
    stub.delay = 0
    j = download(base, app, radius=4)
    assert j["state"] == "done", j
    skipped = int(re.search(r"\((\d+) were already here\)", j["message"]).group(1))
    assert skipped > 0


def posts(stub):
    return len([h for h in stub.hits if h[0] == "POST"])


@pytest.fixture
def quick(monkeypatch):
    """No waits between places tries, so the suite stays fast."""
    assert lm.PLACES_TRIES == 3 and lm.PLACES_WAITS_S == (15, 45)
    monkeypatch.setattr(lm, "PLACES_WAITS_S", (0, 0))


def saved(app):
    return json.loads((app.store.folder / "map.json").read_text(encoding="utf-8"))


def test_a_busy_overpass_is_tried_three_times_and_the_new_street_map_is_kept(srv, stub, quick):
    base, app = srv
    assert download(base, app)["state"] == "done"
    stub.overpass_code = 429
    before = posts(stub)
    j = download(base, app, radius=4)
    assert j["state"] == "places_missing" and "says it is busy" in j["message"], j
    assert posts(stub) == before + 3
    assert call(base + "/api/state")[1]["region"]["radius"] == 4          # the new street map is in place
    stub.overpass_code = 406                                              # 406 is not a passing answer
    before = posts(stub)
    assert "turned the request down" in download(base, app, radius=4)["message"]
    assert posts(stub) == before + 1


def test_places_host_502_three_times_still_puts_the_street_map_in_place(srv, stub, quick):
    base, app = srv
    stub.overpass_code = 502
    j = download(base, app)
    assert j["state"] == "places_missing", j
    assert posts(stub) == 3
    m = j["message"]
    assert m.startswith(f"The street map is in ({len(in_region())} tiles) and works with no internet.")
    assert m.count("could not be fetched") == 1 and m.count("error 502") == 1, m
    assert "Nothing was changed" not in m and "unchanged" not in m and "Try again later" not in m
    assert "Press Fetch places to try again; the street map will not be downloaded again." in m
    assert (app.store.folder / "tiles.sqlite").exists() and not (app.store.folder / "tiles-partial.sqlite").exists()
    z11 = (11, *fx.lonlat_to_tile(CENTER[1], CENTER[0], 11))
    code, body, _ = call(base + "/tiles/%d/%d/%d.mvt" % z11)
    assert code == 200 and body == TILE_BYTES[z11]
    doc = saved(app)
    assert doc["points"] == [] and "error 502" in doc["region"]["places_missing"]["reason"]
    assert doc["region"]["places_missing"]["tried_on"]
    st = call(base + "/api/state")[1]
    assert st["region"]["places_missing"] and st["job"]["state"] == "places_missing"
    assert "could not be fetched (127.0.0.1 turned the request down for now (error 502))" in tool(base, "map_status")


def test_places_come_on_the_second_try(srv, stub, quick):
    base, app = srv
    said = []
    plain_set = app.job.set

    def spy(**kw):
        if "message" in kw:
            said.append(kw["message"])
        plain_set(**kw)
    app.job.set = spy
    stub.overpass_fails = [503]
    j = download(base, app)
    assert j["state"] == "done" and "4 emergency points" in j["message"], j
    assert posts(stub) == 2
    assert "127.0.0.1 is busy. Trying again in 0 seconds (try 2 of 3)..." in said
    assert any("try 2 of 3" in x and x.startswith("Fetching emergency points") for x in said)
    assert "places_missing" not in saved(app)["region"]


def test_a_failure_that_will_not_pass_is_not_retried(srv, stub, quick):
    base, app = srv
    stub.overpass_code = 403
    j = download(base, app)
    assert j["state"] == "places_missing" and "answered with error 403" in j["message"], j
    assert posts(stub) == 1
    stub.overpass_code = 200
    stub.overpass = b"[1, 2]"                                            # JSON, but not a list of places
    j = download(base, app)
    assert j["state"] == "places_missing" and "not a list of places" in j["message"], j
    assert posts(stub) == 2
    assert fx._http_trouble("https://h/x", urllib.error.HTTPError("https://h/x", 504, "", {}, None)).passing
    assert not fx._http_trouble("https://h/x", urllib.error.HTTPError("https://h/x", 404, "", {}, None)).passing


def test_cancel_during_the_wait_ends_at_once_and_keeps_the_old_map(srv, stub, monkeypatch):
    base, app = srv
    assert download(base, app)["state"] == "done"
    before = (app.store.folder / "tiles.sqlite").read_bytes()
    doc = saved(app)
    monkeypatch.setattr(lm, "PLACES_WAITS_S", (60, 60))
    stub.overpass_code = 502
    assert call(base + "/api/download", "POST", {**REGION, "radius": 4})[0] == 200
    assert wait_for(lambda: "Trying again in 60 seconds (try 2 of 3)" in app.job.view()["message"])
    t0 = time.monotonic()
    assert call(base + "/api/cancel", "POST", {})[0] == 200
    j = wait(app, 5)
    assert time.monotonic() - t0 < 2 and j["state"] == "cancelled", j
    assert "The map you had is unchanged" in j["message"]
    assert (app.store.folder / "tiles.sqlite").read_bytes() == before
    after = saved(app)
    assert after["region"] == doc["region"] and after["points"] == doc["points"]


def test_same_box_keeps_the_old_places_and_a_different_box_saves_none(srv, stub, quick):
    base, app = srv
    assert download(base, app)["state"] == "done"
    old = saved(app)["points"]
    stub.overpass_code = 502
    assert server_box(6) == server_box(5) and server_box(4) != server_box(5)
    j = download(base, app, radius=6)
    assert j["state"] == "places_missing", j
    assert "The 4 emergency points saved before for this same area are kept." in j["message"], j
    doc = saved(app)
    assert doc["points"] == old and doc["region"]["radius"] == 6 and doc["region"]["places_missing"]
    out = tool(base, "nearest", kind="hospital")
    assert "Coast Hospital" in out and "Note: the emergency places for this map could not be fetched" in out
    j = download(base, app, radius=4)
    assert j["state"] == "places_missing" and "saved before" not in j["message"], j
    doc = saved(app)
    assert doc["points"] == [] and doc["region"]["radius"] == 4


def test_fetch_places_after_a_failure_fetches_only_the_places(srv, stub, quick):
    base, app = srv
    stub.overpass_code = 502
    assert download(base, app)["state"] == "places_missing"
    tiles = (app.store.folder / "tiles.sqlite").read_bytes()
    gets, before = len([h for h in stub.hits if h[0] == "GET"]), posts(stub)
    stub.overpass_code = 200
    code, out, _ = call(base + "/api/places/fetch", "POST", {})
    assert code == 200 and "not downloaded again" in out["text"]
    j = wait(app)
    assert j["state"] == "done" and "4 emergency points" in j["message"], j
    assert len([h for h in stub.hits if h[0] == "GET"]) == gets            # no tile, no list of builds
    assert posts(stub) == before + 1
    for side, value in server_box(5).items():                             # the saved box, and only its corners
        assert f"{value:.5f}" in stub.hits[-1][2], side
    doc = saved(app)
    assert "places_missing" not in doc["region"] and len(doc["points"]) == 4
    assert (app.store.folder / "tiles.sqlite").read_bytes() == tiles
    assert "Coast Hospital" in tool(base, "nearest", kind="hospital") and "Note:" not in tool(base, "nearest")


def test_fetch_places_that_fails_leaves_everything_as_it_was(srv, stub, quick):
    base, app = srv
    assert call(base + "/api/places/fetch", "POST", {})[1]["error"].startswith("There is no region downloaded yet")
    assert download(base, app)["state"] == "done"
    tiles, doc = (app.store.folder / "tiles.sqlite").read_bytes(), saved(app)
    stub.overpass_code = 504
    before = posts(stub)
    assert call(base + "/api/places/fetch", "POST", {})[0] == 200
    j = wait(app)
    assert j["state"] == "failed" and posts(stub) == before + 3, j
    assert "could not be fetched: 127.0.0.1 turned the request down for now (error 504)." in j["message"]
    assert "The street map and the places you had are unchanged." in j["message"]
    after = saved(app)
    assert after["points"] == doc["points"] and after["region"] == doc["region"]
    assert (app.store.folder / "tiles.sqlite").read_bytes() == tiles
    stub.overpass_code = 200
    stub.drip = {"/api/interpreter"}                                      # one running job at a time
    assert call(base + "/api/places/fetch", "POST", {})[0] == 200
    assert call(base + "/api/places/fetch", "POST", {})[1]["error"].startswith("Fetch places is already running")
    assert call(base + "/api/cancel", "POST", {})[1]["text"].startswith("Stopping. The map and the places")
    assert wait(app, 6)["state"] == "cancelled" and saved(app)["points"] == doc["points"]


def test_the_tools_say_plainly_when_the_places_are_missing(srv, stub, quick):
    base, app = srv
    stub.overpass_code = 502
    assert download(base, app)["state"] == "places_missing"
    honest = "the emergency places for this map have not been fetched yet. The owner can press Fetch places"
    out = tool(base, "nearest", kind="hospital")
    assert out.startswith("I can not say where the nearest hospital is: ") and honest in out
    assert "no hospital" not in out.lower()
    assert honest in tool(base, "list_points", kind="pharmacy")
    assert honest in tool(base, "where_is", name="Coast Hospital")
    # the owner's own points and home keep working
    tool(base, "save_my_point", name="Meet", kind="meeting place", where="37.47, -122.43")
    out = tool(base, "nearest", kind="meeting place")
    assert "Meet (Meeting place)" in out and "Fetch places" not in out
    assert "Meet (Meeting place)" in tool(base, "where_is", name="Meet")
    out = tool(base, "nearest")
    assert "Meet (Meeting place)" in out and "Note: the emergency places" in out


def test_an_older_file_without_the_missing_mark_means_the_places_are_there(data, stub):
    data.mkdir(parents=True)
    older = {"app": "local-map", "region": {"label": "Test town", "lat": 37.46, "lon": -122.43, "radius": 5,
                                            "unit": "km", "radius_km": 5, "detail": "small", "max_zoom": 12,
                                            "box": server_box(5), "build": "20260102.pmtiles", "tiles": 20,
                                            "fetched_on": "2026-11-01T10:00:00"},
             "points": [{"id": "osm-node-1", "name": "Coast Hospital", "kind": "Hospital", "lat": 37.462,
                         "lon": -122.431, "source": "OpenStreetMap", "fetched": "2026-11-01"}],
             "mine": [], "home": {"lat": 37.46, "lon": -122.43, "label": "Home"}, "cap_mb": 250,
             "last_updated": "2026-11-01T10:00:00.ab12"}
    (data / "map.json").write_text(json.dumps(older), encoding="utf-8")
    with running(lm.make_server(0, data, TOKEN, stub.sources())) as base:
        out = tool(base, "nearest", kind="hospital")
        assert "1. Coast Hospital (Hospital)" in out and "Fetch places" not in out
        assert "There is no shelter in the downloaded region" in tool(base, "nearest", kind="shelter")
        assert "could not be fetched" not in tool(base, "map_status")
        assert call(base + "/api/state")[1]["region"].get("places_missing") is None
    assert stub.hits == []


def test_a_failing_tiles_step_behaves_as_before(srv, stub, quick, monkeypatch):
    base, app = srv
    assert download(base, app)["state"] == "done"
    tiles, doc, before = (app.store.folder / "tiles.sqlite").read_bytes(), saved(app), posts(stub)

    def trouble(*a, **k):
        raise fx.FetchError("build.protomaps.com turned the request down for now (error 502). Nothing was "
                            "changed. Try again later.", passing=True)
    monkeypatch.setattr(fx, "download_tiles", trouble)
    j = download(base, app, radius=4)
    assert j["state"] == "failed" and j["message"].endswith("Try again later. The map you had is unchanged."), j
    assert posts(stub) == before                                          # the places were never asked for
    assert (app.store.folder / "tiles.sqlite").read_bytes() == tiles and saved(app)["region"] == doc["region"]


def test_the_page_shows_a_job_message_once_and_has_fetch_places(srv):
    base, _ = srv
    page = call(base + "/")[1].decode()
    assert "say(j.message" not in page                                   # only #jobInfo shows the job's message
    assert page.count("j.message") == 2                                  # set once, and once with the MB count
    assert "api/places/fetch" in page and "'Fetch places'" in page and "'Refresh places'" in page
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    assert manifest["version"] == lm.VERSION == "1.0.1"


def test_water_and_every_emergency_kind_say_the_places_are_missing(srv, stub, quick):
    base, app = srv
    stub.overpass_code = 502
    assert download(base, app)["state"] == "places_missing"
    honest = "the emergency places for this map have not been fetched yet. The owner can press Fetch places"
    for kind in ("water", "drinking water", "hospital", "shelter", None):
        args = {"count": 5} if kind is None else {"kind": kind, "count": 5}
        out = tool(base, "nearest", **args)
        assert out.startswith("I can not say where the nearest ") and honest in out, (kind, out)
        assert "There is no" not in out and "may not list" not in out
        assert honest in tool(base, "list_points", **({} if kind is None else {"kind": kind}))
    out = tool(base, "nearest", kind="meeting place")                   # the owner's own kind: as before
    assert out.startswith("There is no meeting place in the downloaded region") and "Fetch places" not in out
    tool(base, "save_my_point", name="Meet", kind="meeting place", where="37.47, -122.43")
    out = tool(base, "nearest", kind="meeting place")
    assert "Meet (Meeting place)" in out and "Note:" not in out
    out = tool(base, "nearest", kind="water")                          # the owner's own Water goes with Drinking water
    assert out.startswith("I can not say where the nearest water is")
    # the places from before are kept for the same box: the same questions carry the "from before" note
    stub.overpass_code = 200
    assert download(base, app)["state"] == "done"
    stub.overpass_code = 502
    assert download(base, app, radius=6)["state"] == "places_missing"
    before = "could not be fetched with the latest download; the ones kept are from before"
    for kind in ("water", "drinking water", "hospital"):
        out = tool(base, "nearest", kind=kind, count=5)
        assert out.startswith("Nearest ") and "Note: the emergency places" in out and before in out, (kind, out)
    out = tool(base, "nearest", kind="shelter", count=5)
    assert out.startswith("I can not say where the nearest shelter is") and before in out
    assert before in tool(base, "nearest", count=5)
    assert "Note:" not in tool(base, "nearest", kind="meeting place")


@pytest.mark.parametrize("mark", ["502", "", 5, 0, None, True, False, [], {}, {"reason": 5}, {"reason": None},
                                  {"reason": "far too long " * 100}, ["x"]])
def test_any_stored_shape_of_the_missing_mark_is_read_one_way(srv, stub, quick, mark):
    base, app = srv
    assert download(base, app)["state"] == "done"
    doc = saved(app)
    doc["points"] = []
    doc["region"]["places_missing"] = mark
    (app.store.folder / "map.json").write_text(json.dumps(doc), encoding="utf-8")
    missing = mark is not None and mark is not False
    status = tool(base, "map_status")                                   # never an error on a stored value
    assert ("could not be fetched" in status) == missing
    if isinstance(mark, dict) and isinstance(mark.get("reason"), str):
        assert "..." in status and len(status) < 1500
    st = call(base + "/api/state")[1]
    assert st["places_missing"] is missing
    assert ("Fetch places" in tool(base, "nearest", kind="shelter")) == missing
    assert ("Fetch places" in tool(base, "list_points", kind="shelter")) == missing


def test_no_try_starts_once_the_job_deadline_is_too_near(srv, stub, monkeypatch):
    base, app = srv
    monkeypatch.setattr(fx, "JOB_DEADLINE_S", 20)
    monkeypatch.setattr(lm, "PLACES_WAITS_S", (60, 60))                 # longer than the time left
    stub.overpass_code = 502
    t0 = time.monotonic()
    j = download(base, app)
    assert time.monotonic() - t0 < 15 and posts(stub) == 1, j
    assert j["state"] == "places_missing" and "(error 502)" in j["message"], j
    assert "press Download to go on" not in j["message"]
    late = fx.FetchError("This took longer than 30 minutes, so Local Map stopped. Tiles already fetched are kept; "
                         "press Download to go on.")
    assert lm._places_why(late) == "This took longer than 30 minutes, so Local Map stopped."


def test_a_seeded_copy_is_removed_when_download_again_ends_with_nothing_new(srv, stub, quick, monkeypatch):
    base, app = srv
    assert download(base, app)["state"] == "done"
    folder = app.store.folder
    tiles = (folder / "tiles.sqlite").read_bytes()
    plain = fx.download_tiles

    def trouble(*a, **k):
        raise fx.FetchError("build.protomaps.com took too long to answer. Nothing was changed. Try again later.")
    monkeypatch.setattr(fx, "download_tiles", trouble)
    assert download(base, app)["state"] == "failed"                     # failed after the copy, nothing new in it
    assert not (folder / "tiles-partial.sqlite").exists()

    def one_new_then_trouble(sources, pl, store, run=None, progress=None):
        store.put_many([(5, 0, 0, b"a new tile")])
        trouble()
    monkeypatch.setattr(fx, "download_tiles", one_new_then_trouble)
    assert download(base, app)["state"] == "failed"
    assert (folder / "tiles-partial.sqlite").exists()                   # a new tile in it: kept, as resume does
    (folder / "tiles-partial.sqlite").unlink()
    monkeypatch.setattr(fx, "download_tiles", plain)
    stub.drip = {"/api/interpreter"}                                     # cancelled while the places are fetched
    asked = posts(stub)
    assert call(base + "/api/download", "POST", REGION)[0] == 200
    assert wait_for(lambda: posts(stub) > asked)
    assert call(base + "/api/cancel", "POST", {})[0] == 200
    assert wait(app, 6)["state"] == "cancelled"
    assert not (folder / "tiles-partial.sqlite").exists()
    stub.drip = set()
    before = posts(stub)
    monkeypatch.setattr(lm.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    j = download(base, app)
    assert j["state"] == "failed" and "not enough free disk space" in j["message"], j
    assert "0.0 MB is free" in j["message"] and "Nothing was downloaded" in j["message"]
    assert not (folder / "tiles-partial.sqlite").exists() and posts(stub) == before
    assert (folder / "tiles.sqlite").read_bytes() == tiles


def test_fetch_places_refusals_say_what_is_wrong(srv, stub, quick):
    base, app = srv
    assert download(base, app)["state"] == "done"
    doc = saved(app)
    stub.drip = {"/api/interpreter"}
    assert call(base + "/api/places/fetch", "POST", {})[0] == 200
    busy = "Fetch places is already running (the emergency places are being fetched)."
    assert call(base + "/api/places/fetch", "POST", {})[1]["error"].startswith(busy)
    assert call(base + "/api/download", "POST", REGION)[1]["error"].startswith(busy)
    assert call(base + "/api/check", "POST", REGION)[1]["error"].startswith(busy)
    assert call(base + "/api/cancel", "POST", {})[0] == 200
    assert wait(app, 6)["state"] == "cancelled"
    stub.drip = set()
    doc["region"]["box"] = {"west": "far", "south": 1}
    (app.store.folder / "map.json").write_text(json.dumps(doc), encoding="utf-8")
    err = call(base + "/api/places/fetch", "POST", {})[1]["error"]
    assert err.startswith("The saved region can not be read") and "Press Download again" in err


def test_own_points_survive_a_refresh(srv):
    base, app = srv
    assert download(base, app)["state"] == "done"
    tool(base, "save_my_point", name="School field", kind="meeting place", where="37.466, -122.43",
         note="by the flagpole")
    tool(base, "save_my_point", name="Pat next door", kind="neighbour", where="home")
    assert download(base, app, radius=4)["state"] == "done"
    doc = json.loads((app.store.folder / "map.json").read_text(encoding="utf-8"))
    assert [p["name"] for p in doc["mine"]] == ["School field", "Pat next door"]
    assert doc["region"]["radius"] == 4 and len(doc["points"]) == 4


# ---------------------------------------------------------------- points, distance and the tools

def test_points_map_to_plain_kinds():
    k = fx.kind_of
    assert k({"amenity": "fire_station"}) == "Fire station" and k({"healthcare": "urgent_care"}) == "Clinic or urgent care"
    assert k({"amenity": "social_facility", "social_facility": "food_bank"}) == "Food bank or meals"
    assert k({"amenity": "social_facility", "social_facility": "shelter"}) == "Shelter"
    assert k({"amenity": "shelter", "shelter_type": "basic_hut"}) == "Shelter"
    assert k({"office": "government", "government": "healthcare"}) == "Public health office"
    assert k({"man_made": "water_tap"}) == "Drinking water" and k({"emergency": "defibrillator"}) == "Defibrillator"
    assert k({"emergency": "ambulance_station"}) == "Ambulance station" and k({"amenity": "bench"}) is None
    pts, left = fx.points_from_overpass(OVERPASS, "2026-11-15")
    assert left == 0 and [p["kind"] for p in pts] == ["Hospital", "Fire station", "Drinking water", "Pharmacy"]
    assert pts[1]["lat"] == 37.45 and pts[0]["phone"] == "+1 555 0100"
    assert all(p["source"] == "OpenStreetMap" and p["fetched"] == "2026-11-15" for p in pts)


def test_distance_and_direction_by_hand():
    # One degree of a great circle is 6371.0088 * pi / 180 = 111.195 km.
    assert lm.distance_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.001)
    assert lm.bearing_deg(0, 0, 0, 1) == pytest.approx(90) and lm.compass_word(90) == "east"
    assert lm.bearing_deg(0, 0, 1, 0) == pytest.approx(0) and lm.compass_word(359) == "north"
    # (0,0) to (1,1): about 157.25 km, a little east of north-east
    assert lm.distance_km(0, 0, 1, 1) == pytest.approx(157.25, abs=0.05)
    assert lm.bearing_deg(0, 0, 1, 1) == pytest.approx(45.0, abs=0.1)
    assert lm.compass_word(lm.bearing_deg(0, 0, -1, -1)) == "south-west"
    f = lm.how_far(0, 0, 0, 1)
    assert f["miles"] == pytest.approx(69.09, abs=0.01) and f["words"] == \
        "111.2 km (69.1 miles) east (90 degrees), straight-line"


def test_tools_nearest_where_is_status(srv):
    base, app = srv
    assert "Home is not set yet" in tool(base, "nearest", kind="hospital")
    assert "No region is downloaded yet" in tool(base, "map_status")
    assert download(base, app)["state"] == "done"
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    out = tool(base, "nearest", kind="Hospital", count=1)
    assert out.startswith("Nearest hospital from home (straight-line distance")
    assert "1. Coast Hospital (Hospital); 0.2 km (0.1 miles) north (338 degrees), straight-line" in out and "phone +1 555 0100" in out
    assert "source OpenStreetMap, fetched" in out
    out = tool(base, "nearest", count=4)
    assert out.index("Coast Hospital") < out.index("Station 40")
    assert "Station 40" in tool(base, "nearest", kind="fire", from_point="Coast Hospital")
    assert "There is no shelter in the downloaded region" in tool(base, "nearest", kind="shelter")
    assert "Station 40 (Fire station)" in tool(base, "where_is", name="station 40")
    assert "Paris, Île-de-France, FR (a town in the place list)" in tool(base, "where_is", name="Paris, FR")
    assert "Drinking water" in tool(base, "list_points", kind="water")
    status = tool(base, "map_status")
    assert "Region: Test town, 5.0 km around 37.4600, -122.4300; detail Small (zoom 0 to 12)" in status
    assert "4 emergency points from OpenStreetMap: Drinking water 1, Fire station 1, Hospital 1, Pharmacy 1." in status
    assert "Home is set at 37.46000, -122.43000." in status


def test_my_points_add_change_remove(srv):
    base, app = srv
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    assert "Added your point Meet" in tool(base, "save_my_point", name="Meet", kind="Meeting place",
                                           where=["37.47", "-122.43"])
    assert "Changed your point Meeting spot (name, note)" in tool(base, "save_my_point", name="meet",
                                                                     new_name="Meeting spot", note="gate B")
    assert "1.1 km (0.7 miles) north (0 degrees)" in tool(base, "where_is", name="Meeting spot")
    assert tool(base, "remove_my_point", point="Meeting spot") == "Removed your point Meeting spot."
    assert "None of your points is called 'Meeting spot', so nothing was done" in tool(base, "remove_my_point",
                                                                                      point="Meeting spot")


def test_save_my_point_adds_or_changes_and_says_which(srv):
    base, app = srv
    assert "home is not set yet" in tool_error(base, "save_my_point", name="our meeting place", where="home")
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    # the check's first ask: a new point, at home, with a note; "our" is not kept in the name
    out = tool(base, "save_my_point", name="our meeting place", kind="meeting place", where="home",
               note="the library steps")
    assert out.startswith("Added your point meeting place (Meeting place) at 37.46000, -122.43000.")
    # the same name again changes it, never a second point
    out = tool(base, "save_my_point", name="the Meeting Place", new_name="North Gate")
    assert out == "Changed your point North Gate (name)."
    assert "Changed your point North Gate (place)" in tool(base, "save_my_point", name="north gate",
                                                           where={"latitude": 37.47, "longitude": -122.43})
    assert "nothing was changed" in tool(base, "save_my_point", name="North Gate")
    # an everyday kind word is read; an unknown one is kept as Other and said
    assert "Added your point water point (Water)" in tool(base, "save_my_point", name="our water point",
                                                                   kind="water", where="37.45, -122.43")
    out = tool(base, "save_my_point", name="Sam next door", kind="spaceport", where="home")
    assert "(Other)" in out and "'spaceport' is not one this map knows, so it is kept as Other" in out
    # renaming a point that is not there adds nothing
    out = tool(base, "save_my_point", name="the old shed", new_name="Barn")
    assert out.startswith("None of your points is called 'the old shed', so nothing was done.")
    # several partial matches change nothing
    tool(base, "save_my_point", name="Water barrel", where="37.44, -122.43")
    out = tool(base, "save_my_point", name="water", where="home")
    assert "2 of your points match 'water', so nothing was done" in out
    mine = app.store.load()["mine"]
    assert [(p["name"], p["lat"]) for p in mine] == [("North Gate", 37.47), ("water point", 37.45),
                                                     ("Sam next door", 37.46), ("Water barrel", 37.44)]


def test_the_page_adds_and_changes_points_by_id(srv):
    base, app = srv
    code, out, _ = call(base + "/api/mine/add", "POST", {"name": "Gate", "kind": "Meeting place",
                                                          "latitude": 1, "longitude": 2})
    assert code == 200 and out["text"].startswith("Added your point Gate")
    code, out, _ = call(base + "/api/mine/update", "POST", {"id": out["id"], "name": "Gate B", "latitude": 3,
                                                             "longitude": 4})
    assert code == 200 and out["text"] == "Changed your point Gate B (name, place)."
    assert call(base + "/api/mine/update", "POST", {"id": "nope", "name": "x"})[0] == 400


def test_place_search_is_offline_and_ranked(srv, stub):
    base, _ = srv
    hits = call(base + "/api/places?q=paris")[1]["places"]
    assert hits[0]["label"] == "Paris, Île-de-France, FR"
    hits = call(base + "/api/places?q=springfield,%20IL")[1]["places"]
    assert hits and all(h["label"].endswith(", Illinois, US") for h in hits)
    assert call(base + "/api/places?q=x")[1]["places"] == []
    box = call(base + "/api/box?lat=37.46&lon=-122.43&radius=3&unit=miles&detail=detailed")[1]
    assert box["radius_km"] == pytest.approx(4.828, abs=0.001) and box["tile_counts"]["detailed"] > box["tile_counts"]["small"]
    assert stub.hits == []


def test_tools_find_a_point_by_its_plain_name(srv):
    base, app = srv
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    for name, lat in (("Library steps", 37.47), ("Water point", 37.45), ("Water barrel", 37.44), ("North gate", 37.48)):
        tool(base, "save_my_point", name=name, kind="Other", where=f"{lat}, -122.43")
    before = app.store.load()["mine"]
    # exact, case and accent insensitive, with "the" or "our" in front
    assert "Library steps (Other)" in tool(base, "where_is", name="the LIBRARY steps")
    assert "Changed your point North Gate (name)" in tool(base, "save_my_point", name="north gate", new_name="North Gate")
    # a unique partial name
    assert "Changed your point Library steps (place)" in tool(base, "save_my_point", name="library",
                                                             where="37.471 -122.431")
    assert "It is at 37.47100, -122.43100, 1.2 km" in tool(base, "where_is", name="librar")
    # several match: nothing changes and the candidates are named
    out = tool(base, "remove_my_point", point="our water")
    assert "2 of your points match 'our water', so nothing was done" in out
    assert "Water point (Other, id m" in out and "Water barrel (Other, id m" in out
    out = tool(base, "save_my_point", name="water", new_name="Tap")
    assert "nothing was done" in out and "Water point" in out and "Water barrel" in out
    out = tool(base, "where_is", name="water")
    assert out.startswith("2 points on the map match 'water'. Say which one:") and "Water barrel (Other) at 37.44000" in out
    assert len(app.store.load()["mine"]) == len(before)
    # none match: nothing changes and the owner's points are named
    out = tool(base, "remove_my_point", point="the old shed")
    assert out.startswith("None of your points is called 'the old shed', so nothing was done. Your points are: ")
    assert "Library steps" in out and "Water barrel" in out
    # an exact name wins over a partial one, and the id still works
    app.add_point("Water", "Water", 37.43, -122.43)          # saving "Water" by name would match both
    assert tool(base, "remove_my_point", point="water") == "Removed your point Water."
    pid = next(p["id"] for p in app.store.load()["mine"] if p["name"] == "Water barrel")
    assert tool(base, "remove_my_point", point=pid) == "Removed your point Water barrel."
    assert tool(base, "remove_my_point", point="take the water point").startswith("None of your points")
    assert tool(base, "remove_my_point", point="the water point") == "Removed your point Water point."
    assert [p["name"] for p in app.store.load()["mine"]] == ["Library steps", "North Gate"]


def test_place_list_has_small_towns_and_tells_same_names_apart(srv):
    base, app = srv
    hits = app.places.search("zermatt")                  # about 6,000 people: not in the old 15,000 list
    assert hits[0]["label"] == "Zermatt, Valais, CH" and hits[0]["population"] < 15000
    assert len(app.places.rows) > 150000
    # accents and case do not matter, either way round
    assert app.places.search("SAINT-EMILION")[0]["label"] == "Saint-Émilion, New Aquitaine, FR"
    assert app.places.search("zurich")[0]["name"] == "Zürich"
    assert app.places.search("Zürich")[0]["name"] == "Zürich"
    # same-named towns: each shows its region and country, exact names first, bigger first, at most 20
    hits = app.places.search("portland")
    assert len(hits) > 5 and len({h["label"] for h in hits[:5]}) == 5 and len(app.places.search("spring")) == 20
    assert [h["label"] for h in hits[:2]] == ["Portland, Oregon, US", "Portland, Maine, US"]
    assert all(h["exact"] for h in hits[:5])
    assert app.places.search("portland, maine")[0]["label"] == "Portland, Maine, US"
    assert app.places.search("portland, GB")[0]["label"] == "Portland, England, GB"
    # a prefix ranks before a match in the middle of a name
    hits = app.places.search("montreux")
    assert hits[0]["label"] == "Montreux, Vaud, CH" and hits[1]["name"].startswith("Montreux-")
    # where_is names every same-named town instead of picking one
    out = tool(base, "where_is", name="Portland")
    assert "towns in the place list match 'Portland'" in out
    assert "- Portland, Oregon, US" in out and "- Portland, Maine, US" in out
    assert "Portland, Maine, US (a town in the place list) is at 43.6" in tool(base, "where_is", name="Portland, Maine")
    assert "Zermatt, Valais, CH (a town in the place list)" in tool(base, "where_is", name="zermatt")
    assert (PKG / "data" / "places.tsv.gz").stat().st_size < 4 * MB and not (PKG / "data" / "places.tsv").exists()


def test_tool_list_matches_the_manifest_word_for_word(srv):
    base, _ = srv
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in mcp(base, "tools/list")[1]["result"]["tools"]}
    assert len(tools) <= 8
    assert {n: t["description"] for n, t in tools.items()} == manifest["abilities"]["tools"]
    assert set(tools) == set(manifest["abilities"]["examples"])
    assert {n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]} == set(manifest["abilities"]["changes"])
    assert [n for n, t in tools.items() if t["annotations"]["destructiveHint"]] == ["remove_my_point"]
    for n, t in tools.items():                    # each sentence starts with when to use it
        assert t["description"].startswith("Use "), n
    for n in ("where_is", "save_my_point", "remove_my_point"):
        assert "by name; no need to look it up first" in tools[n]["description"]
    asks = [a for v in manifest["abilities"]["examples"].values() for a in v]
    assert len(asks) == len(set(asks)) and all(len(v) >= 1 for v in manifest["abilities"]["examples"].values())
    assert manifest["needs"]["hosts"] == list(fx.HOSTS)


# ---------------------------------------------------------------- safety

def test_nothing_is_dialed_at_start_or_by_any_tool(srv, stub):
    base, _ = srv
    for path in ("/", "/health", "/api/state", "/api/updated", "/api/job", "/api/places?q=lon",
                 "/api/box?lat=1&lon=1&radius=2", "/tiles/0/0/0.mvt", "/vendor/leaflet.js"):
        call(base + path)
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    tool(base, "save_my_point", name="A", where="37.4, -122.4")
    tool(base, "save_my_point", name="A", note="x")
    for name, args in (("nearest", {}), ("list_points", {}), ("where_is", {"name": "Paris"}), ("map_status", {})):
        tool(base, name, **args)
    tool(base, "remove_my_point", point="A")
    assert stub.hits == []


def test_hosts_come_only_from_code():
    assert fx.Sources().hosts == set(fx.HOSTS)
    with pytest.raises(fx.FetchError):
        fx.Sources().open(urllib.request.Request("https://example.com/x"), 1)
    for f in ("server.py", "fetcher.py"):
        text = (PKG / f).read_text(encoding="utf-8")
        assert set(re.findall(r"os\.environ\.get\(\"(\w+)\"", text)) <= {"PUFFPAI_PORT", "PUFFPAI_DATA", "PUFFPAI_TOKEN"}
        assert "os.environ[" not in text and "getenv" not in text
    assert "sources" not in lm.Handler._post.__code__.co_names


def test_the_page_escapes_every_value_and_names_no_outside_address(srv):
    base, app = srv
    evil = "<img src=x onerror=alert(1)>"
    tool(base, "save_my_point", name=evil, where="1, 1", note=evil)
    st = call(base + "/api/state")[1]
    assert st["points"][0]["name"] == evil                                   # data stays data
    page = call(base + "/")[1].decode()
    for line in page.splitlines():
        if "<" in line and "${" in line:                                   # a line that builds HTML
            for m in re.finditer(r"p\.(name|note|phone|kind|source|fetched|label)", line):
                assert line[:m.start()].endswith("esc("), line
    assert "${esc(p.name||'Unnamed')}" in page and "http://" not in page and "https://" not in page
    assert "vendor/leaflet.js" in page and "tiles/{z}/{x}/{y}.mvt" in page


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/health"), ("GET", "/api/state"), ("GET", "/tiles/0/0/0.mvt"),
    ("GET", "/vendor/leaflet.js"), ("GET", "/vendor/protomaps-leaflet.js"), ("GET", "/api/places?q=paris"),
    ("POST", "/mcp"), ("POST", "/api/download"), ("POST", "/api/places/fetch"), ("POST", "/api/mine/add"),
    ("PUT", "/api/state"),
    ("DELETE", "/api/state")])
def test_every_route_needs_the_token(srv, stub, method, path):
    base, _ = srv
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"} if method == "POST" else None
    assert call(base + path, method, body, token=None)[0] == 401
    assert call(base + path, method, body, token="wrong-" + TOKEN)[0] == 401
    assert stub.hits == []


def test_bad_input_is_refused_plainly(srv):
    base, app = srv
    assert call(base + "/api/download", "POST", raw=b'{"lat": 1e999, "lon": 0, "radius": 5}')[0] == 400
    code, out, _ = call(base + "/mcp", "POST", raw=b"[" * 30000 + b"]" * 30000)
    assert code == 400 and out["error"]["code"] == -32700
    assert call(base + "/api/home", "POST", raw=b"[" * 30000 + b"]" * 30000)[0] == 400
    assert call(base + "/api/home", "POST", raw=b"x" * (65 * 1024))[0] in (400, 500)
    assert call(base + "/api/home", "POST", {"latitude": 10 ** 400, "longitude": 0})[0] == 400
    assert call(base + "/api/download", "POST", {**REGION, "radius": 500})[1]["error"] == "radius is a number from 0.5 to 50"
    assert "unit is km or miles" in call(base + "/api/check", "POST", {**REGION, "unit": "leagues"})[1]["error"]
    assert "detail is small" in call(base + "/api/check", "POST", {**REGION, "detail": "huge"})[1]["error"]
    assert call(base + "/api/box?lat=abc&lon=0&radius=1")[0] == 400
    code, out = mcp(base, "tools/call", ["not", "an", "object"])
    assert out["error"]["code"] == -32602
    code, out = mcp(base, "tools/call", {"name": "nearest", "arguments": [1, 2]})
    assert out["result"]["isError"]
    assert "kind is one of" in tool_error(base, "nearest", kind="spaceport")
    assert "count is a number from 1 to 20" in tool_error(base, "nearest", count=10 ** 30)
    assert "latitude is a number" in tool_error(base, "set_home", latitude=91, longitude=0)
    assert "unexpected keyword" in tool_error(base, "map_status", url="https://example.com")
    assert "x is not on the map yet, so say where" in tool_error(base, "save_my_point", name="x")
    assert 'where is "home"' in tool_error(base, "save_my_point", name="x", where="by the school")
    assert "latitude is a number" in tool_error(base, "save_my_point", name="x", where="91, 0")
    assert "unexpected keyword" in tool_error(base, "save_my_point", name="x", latitude=1, longitude=1)
    assert app.job.view()["state"] == "idle"


def test_corrupt_and_empty_files(data, stub):
    data.mkdir(parents=True)
    (data / "map.json").write_text("", encoding="utf-8")
    (data / "tiles.sqlite").write_bytes(b"not a database at all" * 10)
    server = lm.make_server(0, data, TOKEN, stub.sources())
    with running(server) as base:
        code, out, _ = call(base + "/api/state")                          # empty: refused, never written over
        assert code == 500 and "is empty" in out["error"] and str(data) not in out["error"]
        assert "will not write over it" in tool_error(base, "set_home", latitude=1, longitude=1)
        assert call(base + "/tiles/0/0/0.mvt")[0] == 404                 # a broken tile file: no tile, no crash
    assert (data / "map.json").read_bytes() == b""
    broken = list(data.glob("tiles.broken-*.sqlite"))
    assert len(broken) == 1 and not (data / "tiles.sqlite").exists()     # moved aside, not replaced silently
    (data / "tiles.sqlite").write_bytes(b"broken again" * 10)
    (data / "map.json").unlink()                                        # a missing map.json is a fresh start
    server = lm.make_server(0, data, TOKEN, stub.sources())
    with running(server) as base:
        st = call(base + "/api/state")[1]
        assert "moved it aside as tiles.broken-" in st["notice"] and str(data) not in st["notice"]
        assert "tiles.broken-" in tool(base, "map_status")
    assert len(list(data.glob("tiles.broken-*.sqlite"))) == 1           # at most one kept
    assert not (data / "map.json").exists()
    (data / "map.json").write_text("{not json", encoding="utf-8")
    (data / "tiles-partial.sqlite").write_bytes(b"garbage" * 50)
    server = lm.make_server(0, data, TOKEN, stub.sources())
    with running(server) as base:
        code, out, _ = call(base + "/api/state")
        assert code == 500 and "map.json" in out["error"] and str(data) not in out["error"]
        assert "local-map" not in out["error"]                           # the folder's name is not shown either
        assert "will not write over it" in tool_error(base, "set_home", latitude=1, longitude=1)
        assert call(base + "/api/download", "POST", REGION)[0] == 409
    assert (data / "map.json").read_text(encoding="utf-8") == "{not json"
    assert stub.hits == []
    (data / "map.json").write_text("[]", encoding="utf-8")
    with running(lm.make_server(0, data, TOKEN, stub.sources())) as base:
        assert "not a Local Map file" in tool_error(base, "save_my_point", name="x", where="1, 1")
    (data / "map.json").unlink()
    server = lm.make_server(0, data, TOKEN, stub.sources())
    with running(server) as base:
        j = download(base, server.app)                                   # the garbage partial file is replaced
        assert j["state"] == "done", j


# ---------------------------------------------------------------- hostile hosts (each one a local stub)

def tiny_box():
    return server_box(5)


def bomb(n: int, level: int = 9) -> bytes:
    """A gzip root directory claiming *n* entries, each 4 bytes unpacked."""
    raw = fx.write_varint(n) + b"\x01" * (3 * n) + b"\x02" + b"\x00" * (n - 1)
    return gzip.compress(raw, level)


@pytest.mark.parametrize("entries", [8_400_000, 600_000])
def test_a_directory_bomb_is_refused_quickly_and_in_little_memory(stub, entries):
    stub.archive = pack(bomb(entries))
    src = stub.sources()
    tracemalloc.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(fx.FetchError) as e:
            fx.plan(src, tiny_box(), 12)
        took = time.monotonic() - t0
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert str(e.value) == "The map host sent an index that is too large to be real. Nothing was downloaded."
    assert took < 10 and peak < 64 * MB, (took, peak)


def test_leaf_directories_are_cached_least_recently_used_and_counted(stub, monkeypatch):
    stub.archive = build_archive(leaves=3)[0]
    src = stub.sources()
    monkeypatch.setattr(fx, "LEAF_CACHE", 2)
    ar = fx.Archive(src, stub.url + "/20260102.pmtiles")
    ar.open()
    ids = {fx.zxy_to_tileid(*t): t for t in TILE_BYTES}
    found = ar.locate(list(ids))
    assert set(found) == set(ids) and len(ar._leaves) == 2 and ar.run.leaves == 3
    monkeypatch.setattr(fx, "MAX_LEAVES", 2)                   # leaf directories read in one job
    ar = fx.Archive(src, stub.url + "/20260102.pmtiles")
    ar.open()
    with pytest.raises(fx.FetchError, match="too large to be real"):
        ar.locate(list(ids))
    monkeypatch.setattr(fx, "MAX_LEAVES", 2000)
    ar = fx.Archive(src, stub.url + "/20260102.pmtiles")
    ar.open()
    ar.locate(list(ids))
    most = max(ar.run.dir_packed, ar.run.dir_unpacked)
    monkeypatch.setattr(fx, "MAX_DIR_BYTES", most - 1)         # directory bytes in one job
    ar = fx.Archive(src, stub.url + "/20260102.pmtiles")
    ar.open()
    with pytest.raises(fx.FetchError, match="too large to be real"):
        ar.locate(list(ids))


def wait_for(cond, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize("route,path", [("/api/check", "/20260102.pmtiles"), ("/api/download", "/api/interpreter")])
def test_cancel_stops_a_one_byte_a_second_host_within_seconds(srv, stub, route, path):
    base, app = srv
    stub.drip = {path}
    assert call(base + route, "POST", REGION)[0] == 200
    assert wait_for(lambda: any(h[1] == path for h in stub.hits))
    time.sleep(1.0)
    assert app.job.busy()
    t0 = time.monotonic()
    assert call(base + "/api/cancel", "POST", {})[0] == 200
    assert wait_for(lambda: not app.job.busy(), 6)
    assert time.monotonic() - t0 < 6 and app.job.view()["state"] == "cancelled"
    stub.drip = set()
    assert call(base + "/api/check", "POST", REGION)[0] == 200          # a new start is accepted
    assert wait(app)["state"] == "checked"


def test_a_slow_host_hits_its_deadline(stub, monkeypatch):
    stub.drip = {"/builds.json"}
    monkeypatch.setattr(fx, "META_DEADLINE_S", 1)
    t0 = time.monotonic()
    with pytest.raises(fx.FetchError, match="was too slow"):
        fx.newest_build(stub.sources())
    assert time.monotonic() - t0 < 4
    monkeypatch.setattr(fx, "META_DEADLINE_S", 30)
    t0 = time.monotonic()
    with pytest.raises(fx.FetchError, match="took longer than"):
        fx.newest_build(stub.sources(), fx.Run(deadline_s=1))           # the whole job's deadline
    assert time.monotonic() - t0 < 4


def overpass_with(elements):
    return json.dumps({"elements": elements}).encode()


def test_overpass_answer_too_big_is_refused(stub):
    stub.overpass = b"[" + b" " * (17 * MB) + b"]"
    run = fx.Run()
    with pytest.raises(fx.FetchError, match="more than 16 MB of places.*Choose a smaller region"):
        fx.fetch_points(stub.sources(), tiny_box(), run)
    assert run.used <= 16 * MB + 1


def test_overpass_places_are_capped_clipped_and_checked(stub):
    box = tiny_box()
    els = [{"type": "node", "id": i, "lat": 37.42 + (i % 500) * 0.00016, "lon": -122.48 + (i // 500) * 0.002,
            "tags": {"amenity": "drinking_water"}} for i in range(25_000)]
    long = {"amenity": "hospital", "name": "N" * 500, "phone": "1" * 100, "opening_hours": "h" * 500,
            "addr:street": "s" * 250, "addr:city": "c" * 250, "addr:housenumber": "9"}
    els.append({"type": "node", "id": 99_001, "lat": 37.46, "lon": -122.43, "tags": long})
    raw = overpass_with(els)[:-2] + (b', {"type": "node", "id": 99002, "lat": NaN, "lon": 1, "tags": '
                                      b'{"amenity": "police"}}, {"type": "node", "id": 99003, "lat": 1e400, "lon": 1, '
                                      b'"tags": {"amenity": "police"}}, {"type": "node", "id": 99004, "lat": 95, '
                                      b'"lon": 1, "tags": {"amenity": "police"}}, {"type": "node", "id": 99005, '
                                      b'"lat": "37.46", "lon": -122.43, "tags": {"amenity": "police"}}, {"type": '
                                      b'"node", "id": 99006, "lat": 38.5, "lon": -122.43, "tags": {"amenity": '
                                      b'"police"}}]}')
    stub.overpass = raw
    pts, left = fx.fetch_points(stub.sources(), box, None, CENTER)
    assert len(pts) == 20_000 and left == 25_001 - 20_000
    assert not any(p["kind"] == "Police" for p in pts)                 # NaN, infinite, 95, words, outside the box
    big = next(p for p in pts if p["kind"] == "Hospital")              # the nearest the middle is kept
    assert len(big["name"]) == 200 and len(big["phone"]) == 60 and len(big["opening_hours"]) == 300
    assert sum(len(v) for v in big["address"].values()) <= 300

    def far(p):
        return (p["lat"] - CENTER[0]) ** 2 + ((p["lon"] - CENTER[1]) * math.cos(math.radians(CENTER[0]))) ** 2
    kept = {p["id"] for p in pts}
    dropped = [{"lat": e["lat"], "lon": e["lon"]} for e in els if f"osm-node-{e['id']}" not in kept]
    assert max(far(p) for p in pts) <= min(far(p) for p in dropped) + 1e-12


def test_a_download_says_how_many_places_were_left_out(srv, stub):
    base, app = srv
    stub.overpass = overpass_with([{"type": "node", "id": i, "lat": 37.43 + (i % 300) * 0.0002,
                                    "lon": -122.46 + (i // 300) * 0.0008, "tags": {"amenity": "fuel"}}
                                   for i in range(20_005)])
    j = download(base, app)
    assert j["state"] == "done" and "5 more places were left out" in j["message"], j
    doc = json.loads((app.store.folder / "map.json").read_text(encoding="utf-8"))
    assert len(doc["points"]) == 20_000


def one_tile_archive(off, length, data_len, leaf=False):
    """A root directory for the z0 tile only (or a leaf pointer), with the given offset, length and section size."""
    if leaf:
        root = gzip.compress(fx.encode_directory([(0, off, length, 0)]))
        return pack(root, b"\0" * 10, b"\0" * 10)
    root = gzip.compress(fx.encode_directory([(0, off, length, 1)]))
    return pack(root, b"", b"\0" * min(data_len, 64), data_len=data_len)


@pytest.mark.parametrize("off,length,data_len,leaf,words", [
    (0, 5 * MB, 6 * MB, False, "a tile bigger than 4 MB"),
    (0, 100, 50, False, "does not fit its own map file"),
    (40, 20, 50, False, "does not fit its own map file"),
    (0, 100, 0, True, "does not fit its own map file"),
])
def test_bad_tile_lengths_are_refused_before_any_tile_is_fetched(stub, off, length, data_len, leaf, words):
    stub.archive = one_tile_archive(off, length, data_len, leaf)
    with pytest.raises(fx.FetchError, match=words):
        fx.plan(stub.sources(), tiny_box(), 12)


def test_overlapping_tiles_are_stored_whole_and_bad_ones_not_at_all(stub, tmp_path, monkeypatch):
    a = gzip.compress(b"tile a")
    b = a + b"\0" * 50                                                  # starts where a starts, and is longer
    stub.archive = b
    monkeypatch.setattr(fx, "MAX_RANGE", len(a) + 5)                    # so b and the third tile get ranges of their own
    pl = {"url": stub.url + "/20260102.pmtiles", "tile_compression": 2,
          "tiles": [(1, 0, 0, 0, len(a)), (1, 1, 0, 0, len(b)), (1, 0, 1, len(a) + 10, 20)]}
    st = fx.TileStore(tmp_path / "t.sqlite")
    try:
        got = fx.download_tiles(stub.sources(), pl, st)
        assert st.get(1, 0, 0) == a and st.get(1, 1, 0) == b            # b whole, not cut off at a's end
        assert st.get(1, 0, 1) is None and got["bad"] == 1               # not gzip: not stored, counted
    finally:
        st.close()


def test_a_download_says_how_many_tiles_could_not_be_read(srv, stub):
    base, app = srv
    stub.archive = build_archive(not_gzip=(0, 0, 0))[0]
    j = download(base, app)
    assert j["state"] == "done" and "1 tile could not be read" in j["message"], j
    assert call(base + "/tiles/0/0/0.mvt")[0] == 404


@pytest.mark.parametrize("key", ["a?b#c.pmtiles", "x" * 5000 + ".pmtiles", "%2e%2e%2fx.pmtiles",
                                 "@evil.example.pmtiles", "a b.pmtiles", "2026\n0102.pmtiles", "20260102.pmtiles\n",
                                 "../20260102.pmtiles", "202601021.pmtiles", "２０２６０１"
                                 "０２.pmtiles", "20260102.PMTILES", ""])
def test_a_build_key_not_in_the_daily_form_is_refused(stub, key):
    stub.builds = json.dumps([{"key": key, "uploaded": "2026-01-03T00:00:00Z", "version": "4.1.1"}]).encode()
    with pytest.raises(fx.FetchError) as e:
        fx.newest_build(stub.sources())
    assert str(e.value) == ("The map host's list of builds was not in the form this app knows. Nothing was "
                            "downloaded.")


def test_a_bad_key_beside_good_ones_is_passed_over(stub):
    stub.builds = json.dumps([{"key": "a?b#c.pmtiles", "uploaded": "2026-01-09T00:00:00Z"},
                              {"key": "20260102.pmtiles", "uploaded": "2026-01-02T00:00:00Z"}]).encode()
    assert fx.newest_build(stub.sources())["key"] == "20260102.pmtiles"


def test_every_byte_read_counts_against_the_cap(srv, stub, monkeypatch):
    with pytest.raises(fx.FetchError, match="limit"):                   # directories in Check size count
        fx.plan(stub.sources(), tiny_box(), 12, fx.Run(limit_bytes=10_000, cap_mb=5))
    with pytest.raises(fx.FetchError, match="limit"):                   # and the Overpass answer
        fx.fetch_points(stub.sources(), tiny_box(), fx.Run(limit_bytes=100, cap_mb=5))
    run = fx.Run(limit_bytes=fx.job_limit(250), cap_mb=250)
    pl = fx.plan(stub.sources(), tiny_box(), 12, run)
    assert run.used == len(BUILDS) + pl["directory_bytes"] and fx.job_limit(5) == 5 * MB + 32 * MB
    base, app = srv
    monkeypatch.setattr(fx, "job_limit", lambda cap_mb: 10_000)
    assert call(base + "/api/check", "POST", REGION)[0] == 200
    j = wait(app)
    assert j["state"] == "failed" and "more than your 250 MB limit plus 32 MB" in j["message"], j


def test_an_owner_point_that_will_not_read_is_kept_and_shown(srv):
    base, app = srv
    folder = app.store.folder
    folder.mkdir(parents=True, exist_ok=True)
    odd = {"id": "m2", "name": "Creek crossing", "kind": "Water", "lat": "north", "lon": 2, "note": "by the bridge"}
    (folder / "map.json").write_text(json.dumps({"app": "local-map", "mine": [
        {"id": "m1", "name": "Words", "kind": "Other", "lat": "1", "lon": "2"}, odd, "junk"]}), encoding="utf-8")
    tool(base, "save_my_point", name="New", where="3, 4")              # a save
    doc = json.loads((folder / "map.json").read_text(encoding="utf-8"))
    assert odd in doc["mine"] and "junk" in doc["mine"]                 # kept, untouched
    words = next(p for p in doc["mine"] if isinstance(p, dict) and p.get("id") == "m1")
    assert (words["lat"], words["lon"]) == (1.0, 2.0)                   # a number written as words is read
    st = call(base + "/api/state")[1]
    assert [h["name"] for h in st["needs_fixing"]] == ["Creek crossing", "An entry with no name"]
    assert "2 more points of yours need fixing" in tool(base, "map_status")
    page = call(base + "/")[1].decode()
    assert "Needs fixing" in page and "data-fix" in page
    assert "It is fixed" in tool(base, "save_my_point", name="m2", where="5, 2")
    st = call(base + "/api/state")[1]
    assert [h["name"] for h in st["needs_fixing"]] == ["An entry with no name"]
    assert any(p["name"] == "Creek crossing" and p["lat"] == 5 for p in st["points"])


def test_the_box_sent_is_snapped_outward_to_a_grid(srv, stub):
    base, app = srv
    o = call(base + "/api/box?lat=37.46&lon=-122.43&radius=5&unit=km")[1]
    raw = lm.box_around(37.46, -122.43, 5)
    b = o["box"]
    for side in b:
        assert abs(b[side] * 20 - round(b[side] * 20)) < 1e-9, b       # on the 0.05 degree grid
    assert b["west"] <= raw["west"] and b["south"] <= raw["south"] and b["east"] >= raw["east"] \
        and b["north"] >= raw["north"]
    assert ((b["west"] + b["east"]) / 2, (b["south"] + b["north"]) / 2) != (-122.43, 37.46)
    assert fx.snap_box({"west": -122.45, "south": 37.4, "east": -122.4, "north": 37.45}) == \
        {"west": -122.45, "south": 37.4, "east": -122.4, "north": 37.45}  # already on the grid: unchanged
    assert call(base + "/api/check", "POST", REGION)[0] == 200
    assert wait(app)["plan"]["box"] == b                                # the tile fetch uses the same box


def test_the_consent_and_the_page_say_the_same_thing():
    manifest = json.loads((PKG / "puffpai.json").read_text(encoding="utf-8"))
    page = (PKG / "web" / "index.html").read_text(encoding="utf-8")
    same = ("build-metadata.protomaps.dev is asked only for the list of map builds; build.protomaps.com sends the "
            "street map, so it can tell which map area was fetched; overpass-api.de receives the corner coordinates "
            "of the area, rounded outward to a 0.05 degree grid, and sends the emergency places. All three see this "
            "computer's internet address.")
    assert same in manifest["consent"] and same in page
    assert fx.USER_AGENT == "local-map/1.0"
    for f in ("server.py", "fetcher.py", "web/index.html", "puffpai.json", "README.md", "PROCESS.md",
              "tests/test_server.py"):
        assert chr(0x2014) not in (PKG / f).read_text(encoding="utf-8"), f


# ---------------------------------------------------------------- the sweep (rules 10, 14, 14a, 14b, 15)

def test_a_partial_name_is_whole_words_only(srv):
    base, app = srv
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    for name, lat in (("Sharon Lee's gate", 37.47), ("Ronald Park", 37.45), ("Rona Day's well", 37.44),
                      ("City Hall steps", 37.48)):
        tool(base, "save_my_point", name=name, kind="Other", where=f"{lat}, -122.43")
    out = tool(base, "remove_my_point", point="Ron")                   # starts two, is inside "Sharon": never that
    assert "2 of your points match" in out and "Ronald Park" in out and "Sharon" not in out
    out = tool(base, "remove_my_point", point="aron")                  # letters inside a word
    assert out.startswith("None of your points is called 'aron'")
    assert tool(base, "remove_my_point", point="Ro").startswith("None of your points")    # two letters
    out = tool(base, "where_is", name="cit hal")                       # three-letter starts of each word
    assert out.startswith("Using 'City Hall steps'.") and "It is at 37.48000" in out
    out = tool(base, "save_my_point", name="Ronald", note="by the oak")
    assert out.startswith("Using 'Ronald Park'. Changed your point Ronald Park (note)")
    assert tool(base, "remove_my_point", point="city hall steps") == "Removed your point City Hall steps."
    out = tool(base, "nearest", from_point="sha")
    assert out.startswith("Using 'Sharon Lee's gate'.")
    assert len(app.store.load()["mine"]) == 3


HOSTILE = ["1" + " " * 100000 + "x" + " " * 99998, "a" * 199999 + "!", "1/" + "1" * 199998]
GOOD = {"nearest": {}, "list_points": {}, "where_is": {"name": "gate"}, "save_my_point": {"name": "gate"},
        "remove_my_point": {"point": "gate"}, "set_home": {"latitude": 1, "longitude": 1}, "map_status": {}}


def _every_argument(bad):
    for t in lm.tool_list():
        for arg in t["inputSchema"]["properties"]:
            yield t["name"], {**GOOD[t["name"]], arg: bad}


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
    assert call(base + "/health")[0] == 200
    th.join(5)
    assert done.is_set() and took[0] < 1.0, took


def test_a_200000_character_value_in_every_argument_answers_within_a_second(srv):
    base, app = srv
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    tool(base, "save_my_point", name="North gate", where="37.47, -122.43")
    tool(base, "where_is", name="Paris")                 # the town list loads once, on its first use; not timed
    for bad in HOSTILE:
        for name, args in _every_argument(bad):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            t = time.monotonic()
            assert "result" in lm.mcp_answer(app, msg) and time.monotonic() - t < 1.0, (name, list(args))
            _quick_with_health(base, lambda: call(base + "/mcp", "POST", msg)[0] == 400 or pytest.fail(name))
        for name, args in _every_argument(bad[:60000]):                # a value that fits under the 64 KB cap
            _quick_with_health(base, lambda: mcp(base, "tools/call", {"name": name, "arguments": args}))
        for path, body in (("/api/check", {**REGION, "label": bad[:60000]}), ("/api/check", {**REGION, "unit": bad[:60000]}),
                           ("/api/home", {"latitude": bad[:60000], "longitude": 1}),
                           ("/api/mine/add", {"name": bad[:60000], "latitude": 1, "longitude": 1}),
                           ("/api/settings", {"cap_mb": bad[:60000]})):
            _quick_with_health(base, lambda: call(base + path, "POST", body))
        _quick_with_health(base, lambda: call(base + "/api/places?q=" + bad[:1900].replace(" ", "+")))


def test_a_hostile_build_key_from_the_map_host_is_passed_over_quickly(stub):
    for bad in HOSTILE:
        stub.builds = json.dumps([{"key": bad + ".pmtiles", "uploaded": "2026-01-09T00:00:00Z"},
                                  {"key": "20260102.pmtiles", "uploaded": "2026-01-02T00:00:00Z"}]).encode()
        t = time.monotonic()
        assert fx.newest_build(stub.sources())["key"] == "20260102.pmtiles" and time.monotonic() - t < 1.0


@pytest.mark.parametrize("content", ["{}", '{"app": "other"}', '{"recipes": [], "accounts": []}'])
def test_a_file_that_is_not_this_apps_is_never_written_over(data, stub, content):
    data.mkdir(parents=True)
    (data / "map.json").write_text(content, encoding="utf-8")
    with running(lm.make_server(0, data, TOKEN, stub.sources())) as base:
        out = tool_error(base, "set_home", latitude=1, longitude=1)
        assert "not this app's file" in out and "map.json" in out
        assert "not this app's file" in tool_error(base, "save_my_point", name="gate", where="1, 1")
        assert call(base + "/api/home", "POST", {"latitude": 1, "longitude": 1})[0] == 409
    assert (data / "map.json").read_text(encoding="utf-8") == content
    assert sorted(f.name for f in data.iterdir()) == ["map.json"]


def test_a_file_this_version_wrote_still_loads(data, stub):
    data.mkdir(parents=True)
    older = {"region": None, "points": [], "mine": [{"id": "m1", "name": "Gate", "kind": "Other", "lat": 1.0,
                                                     "lon": 1.0, "note": "", "added": "2026-11-01T10:00:00"}],
             "home": None, "cap_mb": 250, "last_updated": "2026-11-01T10:00:00.ab12"}   # saved before the marker
    (data / "map.json").write_text(json.dumps(older), encoding="utf-8")
    with running(lm.make_server(0, data, TOKEN, stub.sources())) as base:
        assert "Gate (Other)" in tool(base, "where_is", name="Gate")
        tool(base, "set_home", latitude=1, longitude=1)
    saved = json.loads((data / "map.json").read_text(encoding="utf-8"))
    assert saved["app"] == "local-map" and saved["mine"][0]["name"] == "Gate"


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
def test_a_wrong_token_gets_401_after_at_most_64_kb(srv, size):
    head = (f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer wrong\r\nContent-Type: application/json\r\n"
            f"Content-Length: {size}\r\n\r\n").encode()
    got, took = _raw(srv[0], head, b"x" * 1000)           # sends a little of what it promised, then waits
    assert got.startswith(b"HTTP/1.0 401") and took < 3, (got[:40], took)


@pytest.mark.parametrize("path", ["/api/home", "/mcp"])
def test_a_body_shorter_than_its_length_gets_a_plain_400_in_about_5_seconds(srv, path):
    head = (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
            f"Content-Length: 100\r\n\r\n").encode()
    got, took = _raw(srv[0], head, b'{"a": 1')
    assert got.startswith(b"HTTP/1.0 400") and 4 <= took < 8, (got[:40], took)


@pytest.mark.parametrize("path", ["/api/home", "/mcp"])
def test_a_body_over_the_cap_is_read_and_dropped_then_a_plain_400(srv, path):
    size = lm.MAX_BODY * 30
    head = (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {size}\r\n\r\n").encode()
    for _ in range(5):
        got, _ = _raw(srv[0], head, b" " * size)
        assert got.startswith(b"HTTP/1.0 400"), got[:40]
    lie = f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {10 ** 9}\r\n\r\n"
    for _ in range(10):
        got, took = _raw(srv[0], lie.encode(), b"{}")
        assert got.startswith(b"HTTP/1.0 400") and took < 5, (got[:40], took)


def test_a_whole_name_wins_even_after_a_leading_the(srv):
    base, app = srv
    tool(base, "set_home", latitude=37.46, longitude=-122.43)
    tool(base, "save_my_point", name="Library", kind="Meeting place", where=["37.47", "-122.43"])
    tool(base, "save_my_point", name="The Library Annex", kind="Meeting place", where=["37.48", "-122.43"])
    out = tool(base, "remove_my_point", point="the library")
    assert "Removed your point Library." in out and "Annex" not in out
