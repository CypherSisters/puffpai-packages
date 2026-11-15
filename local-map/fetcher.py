"""Local Map's one-time download: a PMTiles v3 reader and extractor, and the emergency points query.

Python standard library only. Nothing here runs on its own: the server calls it when the owner
presses Download (or Check size), and it can be run by hand as a script for one box:

    python fetcher.py --west -122.52 --south 37.42 --east -122.38 --north 37.52 --max-zoom 12 \
        --cap-mb 20 --out <folder>

The street map comes from the Protomaps project's public daily planet build: one large PMTiles v3
file, read with HTTP Range requests. Only the directories and the tiles that cover the box are
read; they are written to a SQLite file (tiles by z, x, y, each tile's bytes kept once by content
hash). The format is in the PMTiles v3 specification (public domain); this reader is our own code.

Everything a host sends is treated as hostile: every read is capped, every job has a deadline and
a byte budget, Cancel cuts a request that is in the middle of a slow answer, and an index (a
directory) that is too large to be real is refused before it is unpacked in full.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import array
import bisect
import hashlib
import http.client
import json
import math
import re
import socket
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
TILES_BASE = "https://build.protomaps.com/"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
HOSTS = ("build-metadata.protomaps.dev", "build.protomaps.com", "overpass-api.de")
USER_AGENT = "local-map/1.0"                      # generic: names neither the product nor the owner
MB = 1024 * 1024
HEADER_LEN = 127
FIRST_READ = 16384                                # the header and the root directory live in here
MAX_GAP = 4096                                    # ranges this close together are fetched as one
MAX_RANGE = 8 * MB                                # and no single request asks for more than this
MAX_DIR = 16 * MB                                 # one directory, packed or unpacked, at most
MAX_DIR_ENTRIES = 500_000                         # entries in one directory, at most
MAX_LEAVES = 2000                                 # leaf directories read in one job, at most
LEAF_CACHE = 64                                   # leaf directories kept in memory (least recently used go first)
LEAF_CACHE_ENTRIES = 1_000_000                    # and at most this many entries among them
MAX_DIR_BYTES = 128 * MB                          # directory bytes in one job, packed or unpacked, at most
MAX_DEPTH = 4                                     # leaf directories inside leaf directories, at most
MAX_TILES = 400000
MAX_TILE = 4 * MB                                 # one tile, at most
MAX_BUILDS = 4 * MB
MAX_POINTS_ANSWER = 16 * MB                       # the Overpass answer, at most
MAX_POINTS = 20000                                # places kept, at most (the nearest the middle)
INDEX_ALLOWANCE = 32 * MB                         # bytes on top of the owner's cap, for indexes and places
META_DEADLINE_S = 30                              # builds.json and each directory request
MIN_DEADLINE_S = 30                               # each tile range: at least this long ...
MIN_SPEED = 20 * 1024                             # ... or as long as this many bytes a second takes
OVERPASS_DEADLINE_S = 150                         # the query may run 120 s on the Overpass side
JOB_DEADLINE_S = 30 * 60                          # one whole check or download
WATCH_EVERY_S = 0.2                               # how often Cancel and the deadlines are looked at
READ_CHUNK = 64 * 1024
INFLATE_STEP = 1 * MB
CHECK_EVERY = 4096                                # entries parsed between Cancel and deadline checks
GRID = 0.05                                       # degrees: the box sent is rounded outward to this
BOX_MARGIN = 0.02                                 # degrees: places this far outside the box are kept
MAX_NAME, MAX_ADDRESS, MAX_PHONE, MAX_HOURS = 200, 300, 60, 300
COMPRESSION = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
BUILD_KEY = re.compile(r"[0-9]{8}\.pmtiles")      # the documented daily build form, like 20260102.pmtiles

INDEX_TOO_BIG = "The map host sent an index that is too large to be real. Nothing was downloaded."
INDEX_BAD = "The map host sent an index that does not fit its own map file. Nothing was downloaded."
BAD_BUILDS = "The map host's list of builds was not in the form this app knows. Nothing was downloaded."


class FetchError(RuntimeError):
    """A plain sentence about what went wrong, for the page."""


class Cancelled(RuntimeError):
    """The owner pressed Cancel."""


# ---------------------------------------------------------------- one job's limits

def job_limit(cap_mb: float) -> int:
    """Every byte a job reads from the network counts against this: the owner's cap plus the index allowance."""
    return int(cap_mb * MB) + INDEX_ALLOWANCE


class Run:
    """One check or download: its Cancel flag, its deadline, and its byte budget."""

    def __init__(self, cancel: threading.Event | None = None, limit_bytes: int | None = None,
                 cap_mb: float | None = None, deadline_s: float | None = None) -> None:
        self.cancel = cancel if cancel is not None else threading.Event()
        self.deadline_s = JOB_DEADLINE_S if deadline_s is None else deadline_s
        self.ends = time.monotonic() + self.deadline_s
        self.limit, self.cap_mb = limit_bytes, cap_mb
        self.used = self.dir_packed = self.dir_unpacked = self.leaves = 0

    def late(self) -> bool:
        return time.monotonic() > self.ends

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if self.late():
            raise FetchError(f"This took longer than {self.deadline_s / 60:g} minutes, so Local Map stopped. Tiles "
                             f"already fetched are kept; press Download to go on.")

    def count(self, n: int) -> None:
        self.used += n
        if self.limit is not None and self.used > self.limit:
            cap = f"your {self.cap_mb:g} MB limit" if self.cap_mb is not None else "the limit"
            raise FetchError(f"This read more than {cap} plus {INDEX_ALLOWANCE // MB} MB for the map's index and "
                             f"places, so Local Map stopped. Nothing was changed. Choose a smaller area or less "
                             f"detail, or raise the limit.")

    def directory(self, packed: int, unpacked: int) -> None:
        self.dir_packed += packed
        self.dir_unpacked += unpacked
        if self.dir_packed > MAX_DIR_BYTES or self.dir_unpacked > MAX_DIR_BYTES:
            raise FetchError(INDEX_TOO_BIG)


def _as_run(run) -> Run:
    if run is None:
        return Run()
    if isinstance(run, threading.Event):
        return Run(cancel=run)
    return run


# ---------------------------------------------------------------- the watchdog: Cancel and deadlines cut a request

_tls = threading.local()


class _Guard:
    """One request in flight. When its job is cancelled or its time is up, its socket is shut down."""

    def __init__(self, run: Run, seconds: float) -> None:
        self.run, self.seconds = run, seconds
        self.ends = time.monotonic() + seconds
        self.sock = None
        self.why = ""

    def poll(self) -> None:
        if not self.why:
            if self.run.cancel.is_set():
                self.why = "cancel"
            elif time.monotonic() > min(self.ends, self.run.ends):
                self.why = "time"
            else:
                return
        s, self.sock = self.sock, None                # cut once
        if s is not None:
            try:
                socket.socket.shutdown(s, socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass

    def fail(self, host: str):
        if self.why == "cancel" or self.run.cancel.is_set():
            raise Cancelled()
        self.run.check()                              # the whole job's time is up: its own sentence
        raise FetchError(f"{host} was too slow (more than {self.seconds:.0f} seconds for one answer), so Local Map "
                         f"stopped. Nothing was changed. Try again later.")

    def check(self, host: str) -> None:
        self.poll()
        if self.why:
            self.fail(host)


class _Watch:
    """One shared thread that looks at every request in flight every WATCH_EVERY_S seconds."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.guards: set[_Guard] = set()
        self.thread: threading.Thread | None = None

    def add(self, g: _Guard) -> None:
        with self.lock:
            self.guards.add(g)
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name="local-map-watch", daemon=True)
                self.thread.start()

    def remove(self, g: _Guard) -> None:
        with self.lock:                               # after this, the watchdog never touches g's socket again
            self.guards.discard(g)

    def _loop(self) -> None:
        while True:
            time.sleep(WATCH_EVERY_S)
            with self.lock:
                if not self.guards:
                    self.thread = None
                    return
                for g in self.guards:
                    g.poll()


_WATCH = _Watch()


def _note_socket(sock):
    g = getattr(_tls, "guard", None)
    if g is not None and sock is not None:
        g.sock = sock
        if g.why:
            g.poll()
    return sock


def _guarded(base):
    class Guarded(base):
        """An HTTP(S) connection whose socket the watchdog can shut down."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make = self._create_connection
            self._create_connection = lambda *a, **k: _note_socket(make(*a, **k))

        def connect(self):
            super().connect()
            _note_socket(self.sock)

    return Guarded


_GuardedHTTP = _guarded(http.client.HTTPConnection)
_GuardedHTTPS = _guarded(http.client.HTTPSConnection)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTP, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        kw = {"context": self._context}
        if getattr(self, "_check_hostname", None) is not None:
            kw["check_hostname"] = self._check_hostname
        return self.do_open(_GuardedHTTPS, req, **kw)


# ---------------------------------------------------------------- where requests go

def make_opener(proxies: dict | None = None):
    """The opener: same-host https redirects only, and connections the watchdog can cut.
    *proxies* None takes the proxy from the environment (the Bridge); tests pass {}."""
    handlers = [_SameHost(), _GuardedHTTPHandler(), _GuardedHTTPSHandler()]
    if proxies is not None:
        handlers.insert(0, urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


class Sources:
    """The three addresses. Only code (a test) can give others; never a request or the environment."""

    def __init__(self, builds_url: str = BUILDS_URL, tiles_base: str = TILES_BASE,
                 overpass_url: str = OVERPASS_URL, opener=None) -> None:
        self.builds_url, self.tiles_base, self.overpass_url = builds_url, tiles_base, overpass_url
        self.hosts = {urllib.parse.urlsplit(u).hostname for u in (builds_url, tiles_base, overpass_url)}
        self.opener = opener or make_opener()        # proxies from the environment

    def open(self, req: urllib.request.Request, timeout: float):
        host = urllib.parse.urlsplit(req.full_url).hostname
        if host not in self.hosts:
            raise FetchError(f"Local Map only talks to {', '.join(sorted(self.hosts))}.")
        req.add_header("User-Agent", USER_AGENT)
        return self.opener.open(req, timeout=timeout)


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same https host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise FetchError(f"{old.hostname} sent the request on to another place, so Local Map stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "?"


def _http_trouble(url: str, exc: Exception) -> FetchError:
    host = _host(url)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return FetchError(f"{host} says it is busy (too many requests). Nothing was changed. Try again later.")
        if exc.code in (406, 502, 503, 504):
            return FetchError(f"{host} turned the request down for now (error {exc.code}). Nothing was changed. "
                              f"Try again later.")
        return FetchError(f"{host} answered with error {exc.code}. If the Bridge blocks it, allow {host} for "
                          f"Local Map on the Bridge page.")
    if isinstance(exc, TimeoutError) or "timed out" in str(getattr(exc, "reason", exc)):
        return FetchError(f"{host} took too long to answer. Nothing was changed. Try again later.")
    if isinstance(exc, http.client.HTTPException):
        return FetchError(f"{host} sent an answer that is not proper HTTP. Nothing was changed. Try again later.")
    return FetchError(f"Could not reach {host} ({type(getattr(exc, 'reason', exc)).__name__}). Is this computer "
                      f"online, and is {host} allowed for Local Map on the Bridge?")


def _request(sources: Sources, req: urllib.request.Request, run: Run, seconds: float, most: int,
             status: int | None = None, moved=None) -> bytes:
    """One request's body, at most *most* + 1 bytes (more than *most* means too big; the caller says so).
    Cancel and the deadlines are looked at between reads and by the watchdog; every byte counts against
    the job's budget."""
    run.check()
    url = req.full_url
    host = _host(url)
    g = _Guard(run, seconds)
    _tls.guard = g
    _WATCH.add(g)
    r = None
    parts, got = [], 0
    try:
        try:
            r = sources.open(req, seconds + 2)          # the watchdog, not the socket, ends a slow request
            if status is not None and getattr(r, "status", None) != status:
                raise FetchError(f"{host} did not send just the part asked for, so Local Map stopped.")
            read = getattr(r, "read1", r.read)          # read1: one socket read at a time, so a drip can not hold it
            while got <= most:
                g.check(host)
                piece = read(min(READ_CHUNK, most + 1 - got))
                if not piece:
                    break
                got += len(piece)
                parts.append(piece)
                run.count(len(piece))
                if moved:
                    moved(len(piece))
            g.check(host)                               # an end the watchdog caused is not a real end
        finally:
            _WATCH.remove(g)
            _tls.guard = None
            if r is not None:
                r.close()
    except (FetchError, Cancelled):
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        g.poll()
        if g.why or run.cancel.is_set() or run.late():
            g.why = g.why or ("cancel" if run.cancel.is_set() else "time")
            g.fail(host)
        raise _http_trouble(url, exc) from None
    return b"".join(parts)


# ---------------------------------------------------------------- PMTiles v3: numbers and tile ids

def read_varint(buf, pos: int) -> tuple[int, int]:
    """One little-endian base-128 varint from *buf* at *pos*: (value, next position)."""
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise FetchError("A map directory ended in the middle of a number.")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise FetchError("A map directory holds a number that is too long.")


def write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _rotate(n: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def zxy_to_tileid(z: int, x: int, y: int) -> int:
    """The tile id: tiles in all lower zooms, plus the position on zoom z's Hilbert curve."""
    if not 0 <= z <= 26 or not (0 <= x < 1 << z and 0 <= y < 1 << z):
        raise ValueError("tile out of range")
    base = ((1 << (2 * z)) - 1) // 3
    n = 1 << z
    d, s = 0, n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        x, y = _rotate(n, x, y, rx, ry)
        s >>= 1
    return base + d


def tileid_to_zxy(tile_id: int) -> tuple[int, int, int]:
    if tile_id < 0:
        raise ValueError("tile id out of range")
    z, base = 0, 0
    while True:
        count = 1 << (2 * z)
        if tile_id < base + count:
            break
        base += count
        z += 1
        if z > 26:
            raise ValueError("tile id out of range")
    n, t = 1 << z, tile_id - base
    x = y = 0
    s = 1
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        x, y = _rotate(s, x, y, rx, ry)
        x += s * rx
        y += s * ry
        t //= 4
        s <<= 1
    return z, x, y


# ---------------------------------------------------------------- header and directories

def parse_header(buf: bytes) -> dict:
    if len(buf) < HEADER_LEN or buf[:7] != b"PMTiles":
        raise FetchError("The map file does not start like a PMTiles file.")
    if buf[7] != 3:
        raise FetchError(f"The map file is PMTiles version {buf[7]}; Local Map reads version 3.")
    (root_off, root_len, meta_off, meta_len, leaf_off, leaf_len, data_off, data_len,
     n_addressed, n_entries, n_contents) = struct.unpack_from("<11Q", buf, 8)
    clustered, internal, tile_comp, tile_type, minz, maxz = struct.unpack_from("<6B", buf, 96)
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<4i", buf, 102)
    center_z = buf[118]
    center_lon, center_lat = struct.unpack_from("<2i", buf, 119)
    return {"root_offset": root_off, "root_length": root_len, "metadata_offset": meta_off,
            "metadata_length": meta_len, "leaf_offset": leaf_off, "leaf_length": leaf_len,
            "data_offset": data_off, "data_length": data_len, "addressed_tiles": n_addressed,
            "tile_entries": n_entries, "tile_contents": n_contents, "clustered": bool(clustered),
            "internal_compression": internal, "tile_compression": tile_comp, "tile_type": tile_type,
            "min_zoom": minz, "max_zoom": maxz, "bounds": [min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7],
            "center": [center_lon / 1e7, center_lat / 1e7, center_z]}


def _inflate(raw: bytes, compression: int, run: Run | None = None) -> bytes | bytearray:
    """Unpack a directory a step at a time, so one that grows past MAX_DIR is refused early."""
    run = _as_run(run)
    if compression == 1:
        if len(raw) > MAX_DIR:
            raise FetchError(INDEX_TOO_BIG)
        return raw
    if compression != 2:
        raise FetchError(f"The map file packs its directories with {COMPRESSION.get(compression, compression)}, "
                         f"which Local Map does not read.")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)          # gzip only
    out, data = bytearray(), raw
    try:
        while True:
            run.check()
            piece = d.decompress(data, INFLATE_STEP)
            out += piece
            if len(out) > MAX_DIR:
                raise FetchError(INDEX_TOO_BIG)
            data = d.unconsumed_tail
            if d.eof or (not data and not piece):
                break
    except zlib.error as exc:
        raise FetchError(f"A map directory would not unpack ({exc}).") from None
    if not d.eof:
        raise FetchError("A map directory would not unpack (it ends too soon).")
    return out


class Directory:
    """One directory's entries in four compact arrays, in tile id order: (tile_id, offset, length, run_length)."""

    __slots__ = ("ids", "offs", "lens", "runs")

    def __init__(self, ids=None, offs=None, lens=None, runs=None) -> None:
        self.ids, self.offs = ids or array.array("Q"), offs or array.array("Q")
        self.lens, self.runs = lens or array.array("Q"), runs or array.array("Q")

    def __len__(self) -> int:
        return len(self.ids)

    def __getitem__(self, i: int) -> tuple[int, int, int, int]:
        return self.ids[i], self.offs[i], self.lens[i], self.runs[i]

    def __iter__(self):
        return zip(self.ids, self.offs, self.lens, self.runs)


def parse_directory(raw: bytes, compression: int, run: Run | None = None) -> Directory:
    """A directory, checked: at most MAX_DIR unpacked and MAX_DIR_ENTRIES entries, Cancel looked at as it goes."""
    run = _as_run(run)
    buf = _inflate(raw, compression, run)
    run.directory(len(raw), len(buf))
    n, pos = read_varint(buf, 0)
    if n > MAX_DIR_ENTRIES:
        raise FetchError(INDEX_TOO_BIG)
    if n * 4 > len(buf):                                 # every entry needs at least 4 bytes
        raise FetchError("A map directory says it holds more entries than it has room for.")
    ids, runs, lens, offs = (array.array("Q") for _ in range(4))
    last = 0
    try:
        for i in range(n):
            if not i % CHECK_EVERY:
                run.check()
            d, pos = read_varint(buf, pos)
            last += d
            ids.append(last)
        for i in range(n):
            if not i % CHECK_EVERY:
                run.check()
            v, pos = read_varint(buf, pos)
            runs.append(v)
        for i in range(n):
            if not i % CHECK_EVERY:
                run.check()
            v, pos = read_varint(buf, pos)
            if v == 0:
                raise FetchError("A map directory has an entry of length 0.")
            lens.append(v)
        for i in range(n):
            if not i % CHECK_EVERY:
                run.check()
            v, pos = read_varint(buf, pos)
            if v == 0 and i > 0:
                offs.append(offs[i - 1] + lens[i - 1])
            elif v == 0:
                raise FetchError("A map directory's first entry has no offset.")
            else:
                offs.append(v - 1)
    except OverflowError:
        raise FetchError("A map directory holds a number that is too long.") from None
    return Directory(ids, offs, lens, runs)


def encode_directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """The reverse of parse_directory, not compressed (used by the tests to build a small file)."""
    out = bytearray(write_varint(len(entries)))
    last = 0
    for tid, _, _, _ in entries:
        out += write_varint(tid - last)
        last = tid
    for _, _, _, run in entries:
        out += write_varint(run)
    for _, _, length, _ in entries:
        out += write_varint(length)
    for i, (_, off, _, _) in enumerate(entries):
        prev = entries[i - 1] if i else None
        out += write_varint(0 if prev and off == prev[1] + prev[2] else off + 1)
    return bytes(out)


def find_entry(entries, tile_id: int):
    """The entry whose tile id is the greatest one at or below *tile_id*, or None."""
    ids = entries.ids if isinstance(entries, Directory) else [e[0] for e in entries]
    i = bisect.bisect_right(ids, tile_id) - 1
    return entries[i] if i >= 0 else None


# ---------------------------------------------------------------- which tiles cover a box

def snap_box(box: dict, grid: float = GRID) -> dict:
    """The box rounded outward to a *grid*-degree grid (west and south down, east and north up), so the middle
    of what is sent to a host is not the point the owner chose."""
    k = 1.0 / grid

    def down(v: float) -> float:
        return math.floor(round(v * k, 6)) / k

    def up(v: float) -> float:
        return math.ceil(round(v * k, 6)) / k

    return {"west": round(max(-180.0, down(box["west"])), 2), "south": round(max(-85.0, down(box["south"])), 2),
            "east": round(min(180.0, up(box["east"])), 2), "north": round(min(85.0, up(box["north"])), 2)}


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = max(-85.0511, min(85.0511, lat))
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_for_box(west: float, south: float, east: float, north: float, max_zoom: int) -> list[tuple[int, int, int]]:
    """Every (z, x, y) for zooms 0 to *max_zoom* that touches the box."""
    out = []
    for z in range(max_zoom + 1):
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                out.append((z, x, y))
    return out


def count_tiles(west, south, east, north, max_zoom) -> int:
    total = 0
    for z in range(max_zoom + 1):
        x0, y0 = lonlat_to_tile(west, north, z)
        x1, y1 = lonlat_to_tile(east, south, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def merge_ranges(spans: list[tuple[int, int]], max_gap: int = MAX_GAP, max_len: int = MAX_RANGE) -> list[tuple[int, int]]:
    """(offset, length) spans, merged where they touch or sit within *max_gap* bytes: (offset, length)."""
    out: list[list[int]] = []
    for off, length in sorted(set(spans)):
        end = off + length
        if out and off <= out[-1][1] + max_gap and end - out[-1][0] <= max_len:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([off, end])
    return [(a, b - a) for a, b in out]


# ---------------------------------------------------------------- reading the planet file

class Archive:
    """One remote PMTiles v3 file, read with Range requests."""

    def __init__(self, sources: Sources, url: str, run=None) -> None:
        self.sources, self.url = sources, url
        self.run = _as_run(run)
        self.bytes_read = 0
        self.requests = 0
        self.header: dict = {}
        self.root = Directory()
        self._leaves: OrderedDict[tuple[int, int], Directory] = OrderedDict()
        self._cached = 0

    def read(self, offset: int, length: int, chunk=None, seconds: float | None = None) -> bytes:
        if length <= 0 or offset < 0:
            raise FetchError(INDEX_BAD)
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
        self.requests += 1
        if seconds is None:
            seconds = max(MIN_DEADLINE_S, length / MIN_SPEED)

        def moved(n: int) -> None:
            self.bytes_read += n
            if chunk:
                chunk(n)

        data = _request(self.sources, req, self.run, seconds, length, status=206, moved=moved)
        if len(data) != length:
            raise FetchError(f"{_host(self.url)} sent {len(data)} bytes where {length} were asked for.")
        return data

    def open(self) -> dict:
        first = self.read(0, FIRST_READ, seconds=META_DEADLINE_S)
        h = parse_header(first)
        if h["tile_type"] != 1:
            raise FetchError("The map file does not hold vector tiles, so Local Map can not show it.")
        if h["root_length"] > MAX_DIR or h["leaf_length"] > 1 << 50 or h["data_length"] > 1 << 50:
            raise FetchError(INDEX_TOO_BIG)
        if h["root_offset"] < HEADER_LEN or h["root_length"] == 0:
            raise FetchError(INDEX_BAD)
        end = h["root_offset"] + h["root_length"]
        raw = first[h["root_offset"]:end] if end <= len(first) else \
            self.read(h["root_offset"], h["root_length"], seconds=META_DEADLINE_S)
        self.header, self.root = h, parse_directory(raw, h["internal_compression"], self.run)
        return h

    def _leaf(self, offset: int, length: int) -> Directory:
        key = (offset, length)
        hit = self._leaves.get(key)
        if hit is not None:
            self._leaves.move_to_end(key)
            return hit
        if length > MAX_DIR:
            raise FetchError(INDEX_TOO_BIG)
        if offset + length > self.header["leaf_length"]:
            raise FetchError(INDEX_BAD)
        self.run.leaves += 1
        if self.run.leaves > MAX_LEAVES:
            raise FetchError(INDEX_TOO_BIG)
        raw = self.read(self.header["leaf_offset"] + offset, length, seconds=META_DEADLINE_S)
        d = parse_directory(raw, self.header["internal_compression"], self.run)
        self._leaves[key] = d
        self._cached += len(d)
        while len(self._leaves) > LEAF_CACHE or (self._cached > LEAF_CACHE_ENTRIES and len(self._leaves) > 1):
            _, old = self._leaves.popitem(last=False)
            self._cached -= len(old)
        return d

    def _tile_ok(self, offset: int, length: int) -> None:
        if length > MAX_TILE:
            raise FetchError(f"The map host lists a tile bigger than {MAX_TILE // MB} MB, more than any real map "
                             f"tile. Nothing was downloaded.")
        if offset + length > self.header["data_length"]:
            raise FetchError(INDEX_BAD)

    def locate(self, tile_ids: list[int], progress=None) -> dict[int, tuple[int, int]]:
        """tile id -> (absolute offset, length), for the ids the file holds. Reads only directories, one at a
        time (a leaf directory is read when its turn comes, not all at once)."""
        out: dict[int, tuple[int, int]] = {}
        pending: list = [(None, sorted(set(tile_ids)), 0)]
        while pending:
            where, ids, depth = pending.pop()
            entries = self.root if where is None else self._leaf(*where)
            leaves: dict[tuple[int, int], list[int]] = {}
            for n, tid in enumerate(ids):
                if not n % CHECK_EVERY:
                    self.run.check()
                e = find_entry(entries, tid)
                if e is None:
                    continue
                etid, off, length, run = e
                if run == 0:
                    leaves.setdefault((off, length), []).append(tid)
                elif tid < etid + run:
                    self._tile_ok(off, length)
                    out[tid] = (self.header["data_offset"] + off, length)
            if leaves and depth >= MAX_DEPTH:
                raise FetchError("The map file's directories go deeper than Local Map reads.")
            for key, sub in leaves.items():
                pending.append((key, sub, depth + 1))
                if progress:
                    progress()
        return out


def newest_build(sources: Sources, run=None) -> dict:
    """The newest entry of builds.json whose key is in the daily form: {key, version, uploaded, size}."""
    run = _as_run(run)
    req = urllib.request.Request(sources.builds_url, headers={"Accept": "application/json"})
    raw = _request(sources, req, run, META_DEADLINE_S, MAX_BUILDS)
    if len(raw) > MAX_BUILDS:
        raise FetchError(f"{_host(sources.builds_url)} sent a list too big to read.")
    try:
        builds = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FetchError(BAD_BUILDS) from None
    good = [b for b in builds if isinstance(b, dict) and isinstance(b.get("key"), str)
            and BUILD_KEY.fullmatch(b["key"])] if isinstance(builds, list) else []
    if not good:
        raise FetchError(BAD_BUILDS)
    best = max(good, key=lambda b: (str(b.get("uploaded") or "")[:40], b["key"]))
    return {"key": best["key"], "version": _clip(best.get("version"), 40), "uploaded": _clip(best.get("uploaded"), 40),
            "size": best.get("size") if isinstance(best.get("size"), int) else None}


# ---------------------------------------------------------------- the plan (a dry run) and the download

def plan(sources: Sources, box: dict, max_zoom: int, run=None, build: dict | None = None) -> dict:
    """Directories only: which tiles, which byte ranges, and how many bytes the tiles will take."""
    run = _as_run(run)
    build = build or newest_build(sources, run)
    ar = Archive(sources, sources.tiles_base + build["key"], run)
    h = ar.open()
    want = tiles_for_box(box["west"], box["south"], box["east"], box["north"], max_zoom)
    if len(want) > MAX_TILES:
        raise FetchError(f"That is {len(want)} tiles, more than Local Map fetches at once. Choose a smaller area "
                         f"or less detail.")
    ids = {zxy_to_tileid(*t): t for t in want}
    found = ar.locate(list(ids))
    tiles = [(ids[tid][0], ids[tid][1], ids[tid][2], off, length) for tid, (off, length) in sorted(found.items())]
    ranges = merge_ranges([(off, length) for *_, off, length in tiles], MAX_GAP, MAX_RANGE)
    return {"build": build, "url": ar.url, "tile_compression": h["tile_compression"], "max_zoom": max_zoom,
            "box": dict(box), "tiles": tiles, "tile_count": len(tiles), "asked_tiles": len(want),
            "bytes": sum(length for _, length in ranges), "directory_bytes": ar.bytes_read,
            "directory_requests": ar.requests, "map_max_zoom": h["max_zoom"]}


class TileStore:
    """tiles(z, x, y, hash) and blobs(hash, data) in one SQLite file: each tile's bytes are kept once."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        try:
            self._create()
        except sqlite3.DatabaseError:
            self.db.close()
            raise

    def _create(self) -> None:
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, data BLOB NOT NULL);"
            "CREATE TABLE IF NOT EXISTS tiles(z INTEGER, x INTEGER, y INTEGER, hash TEXT NOT NULL,"
            " PRIMARY KEY(z, x, y));")
        self.db.commit()

    def meta(self) -> dict:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM meta")}

    def set_meta(self, **values) -> None:
        self.db.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                            [(k, json.dumps(v)) for k, v in values.items()])
        self.db.commit()

    def have(self) -> set[tuple[int, int, int]]:
        return set(self.db.execute("SELECT z, x, y FROM tiles"))

    def put_many(self, rows: list[tuple[int, int, int, bytes]]) -> None:
        with self.db:
            for z, x, y, data in rows:
                h = hashlib.sha256(data).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO blobs(hash, data) VALUES (?, ?)", (h, data))
                self.db.execute("INSERT OR REPLACE INTO tiles(z, x, y, hash) VALUES (?, ?, ?, ?)", (z, x, y, h))

    def get(self, z: int, x: int, y: int) -> bytes | None:
        row = self.db.execute("SELECT b.data FROM tiles t JOIN blobs b ON b.hash = t.hash "
                              "WHERE t.z = ? AND t.x = ? AND t.y = ?", (z, x, y)).fetchone()
        return row[0] if row else None

    def counts(self) -> tuple[int, int]:
        return (self.db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0],
                self.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0])

    def close(self) -> None:
        self.db.close()


def download_tiles(sources: Sources, the_plan: dict, store: TileStore, run=None, progress=None) -> dict:
    """Fetch the plan's tiles into *store*, skipping tiles it already has (a resumed download).
    A tile is stored only when exactly its length was read and, for gzip tiles, it starts like gzip;
    the others are counted in "bad" and not stored."""
    run = _as_run(run)
    have = store.have()
    todo = [t for t in the_plan["tiles"] if (t[0], t[1], t[2]) not in have]
    by_span: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for z, x, y, off, length in todo:
        by_span.setdefault((off, length), []).append((z, x, y))
    ranges = merge_ranges(list(by_span), MAX_GAP, MAX_RANGE)
    starts = [a for a, _ in ranges]
    groups: list[list[tuple[int, int]]] = [[] for _ in ranges]
    bad = 0
    for off, ln in sorted(by_span):
        k = bisect.bisect_right(starts, off) - 1          # the range that holds the whole span
        if ln <= 0 or ln > MAX_TILE or k < 0 or off + ln > ranges[k][0] + ranges[k][1]:
            bad += len(by_span[(off, ln)])
            continue
        groups[k].append((off, ln))
    total = sum(length for (_, length), g in zip(ranges, groups) if g)
    gz = the_plan.get("tile_compression") == 2
    ar = Archive(sources, the_plan["url"], run)
    done = {"bytes": 0}

    def moved(n: int) -> None:
        done["bytes"] += n
        if progress:
            progress(done["bytes"], total)

    for (start, length), spans in zip(ranges, groups):
        if not spans:
            continue
        data = ar.read(start, length, moved)
        rows = []
        for off, ln in spans:
            piece = data[off - start: off - start + ln]
            if len(piece) != ln or (gz and piece[:2] != b"\x1f\x8b"):
                bad += len(by_span[(off, ln)])
                continue
            rows.extend((z, x, y, piece) for z, x, y in by_span[(off, ln)])
        store.put_many(rows)
        run.check()
    return {"skipped": len(the_plan["tiles"]) - len(todo), "fetched_tiles": len(todo) - bad, "bad": bad,
            "bytes": ar.bytes_read, "requests": ar.requests}


# ---------------------------------------------------------------- emergency points (Overpass)

QUERY = """[out:json][timeout:120][bbox:{s},{w},{n},{e}];
(
 nwr[amenity~"^(fire_station|police|hospital|clinic|doctors|pharmacy|drinking_water|water_point|fuel|community_centre|townhall|ranger_station)$"];
 nwr[amenity=shelter][shelter_type~"^(basic_hut|weather_shelter|rock_shelter|lean_to)$"];
 nwr[amenity=social_facility][social_facility~"^(food_bank|soup_kitchen|shelter)$"];
 nwr[healthcare~"^(hospital|clinic|urgent_care|centre|pharmacy)$"];
 nwr[office=government][government=healthcare];
 nwr[emergency~"^(assembly_point|defibrillator|siren|ambulance_station)$"];
 nwr[man_made=water_tap];
);
out center tags;"""

KINDS = ("Fire station", "Police", "Hospital", "Clinic or urgent care", "Doctor", "Pharmacy", "Public health office",
         "Shelter", "Food bank or meals", "Drinking water", "Fuel", "Community center", "Town hall", "Assembly point",
         "Defibrillator", "Siren", "Ambulance station")


def kind_of(tags: dict) -> str | None:
    """One plain kind for an OpenStreetMap feature's tags, or None when it is not one we keep."""
    a, hc, em = tags.get("amenity"), tags.get("healthcare"), tags.get("emergency")
    if a == "fire_station":
        return "Fire station"
    if a == "police":
        return "Police"
    if a == "hospital" or hc == "hospital":
        return "Hospital"
    if a == "clinic" or hc in ("clinic", "urgent_care", "centre"):
        return "Clinic or urgent care"
    if a == "doctors":
        return "Doctor"
    if a == "pharmacy" or hc == "pharmacy":
        return "Pharmacy"
    if tags.get("office") == "government" and tags.get("government") == "healthcare":
        return "Public health office"
    if a == "social_facility":
        sf = tags.get("social_facility")
        if sf in ("food_bank", "soup_kitchen"):
            return "Food bank or meals"
        if sf == "shelter":
            return "Shelter"
    if a == "shelter":
        return "Shelter"
    if a in ("drinking_water", "water_point") or tags.get("man_made") == "water_tap":
        return "Drinking water"
    if a == "fuel":
        return "Fuel"
    if a == "community_centre":
        return "Community center"
    if a in ("townhall", "ranger_station"):
        return "Town hall"                # a ranger station is kept as a public office too
    if em == "assembly_point":
        return "Assembly point"
    if em == "defibrillator":
        return "Defibrillator"
    if em == "siren":
        return "Siren"
    if em == "ambulance_station":
        return "Ambulance station"
    return None


def _clip(value, most: int) -> str:
    """Words only (a string or a number), spaces squeezed, at most *most* characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value)[:most * 4 + 16].split())[:most]


def _coord(v, most: float):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -most <= v <= most:
        return None
    return float(v)


def points_from_overpass(answer: dict, fetched_on: str, box: dict | None = None,
                         centre: tuple[float, float] | None = None, most: int | None = None) -> tuple[list[dict], int]:
    """(the places kept, how many were left out). Only places with a real position inside *box* (plus a small
    margin) are kept; when there are more than *most*, the nearest *centre* are kept."""
    most = MAX_POINTS if most is None else most
    elements = answer.get("elements") if isinstance(answer, dict) else None
    out, seen = [], set()
    for el in elements if isinstance(elements, list) else []:
        if not isinstance(el, dict) or not isinstance(el.get("tags"), dict):
            continue
        tags = el["tags"]
        kind = kind_of(tags)
        if kind is None:
            continue
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None and isinstance(el.get("center"), dict):
            lat, lon = el["center"].get("lat"), el["center"].get("lon")
        lat, lon = _coord(lat, 90), _coord(lon, 180)
        if lat is None or lon is None:
            continue
        if box and not (box["south"] - BOX_MARGIN <= lat <= box["north"] + BOX_MARGIN
                        and box["west"] - BOX_MARGIN <= lon <= box["east"] + BOX_MARGIN):
            continue
        pid = f"osm-{_clip(el.get('type'), 10)}-{_clip(el.get('id'), 20)}"
        if pid in seen:
            continue
        seen.add(pid)
        addr, room = {}, MAX_ADDRESS
        for k in ("addr:housenumber", "addr:street", "addr:city", "addr:postcode", "addr:state"):
            v = _clip(tags.get(k), room) if room > 0 else ""
            if v:
                addr[k[5:]] = v
                room -= len(v)
        out.append({"id": pid, "name": _clip(tags.get("name") or tags.get("operator"), MAX_NAME), "kind": kind,
                    "lat": round(lat, 6), "lon": round(lon, 6), "address": addr,
                    "phone": _clip(tags.get("phone") or tags.get("contact:phone"), MAX_PHONE),
                    "opening_hours": _clip(tags.get("opening_hours"), MAX_HOURS),
                    "source": "OpenStreetMap", "fetched": fetched_on})
    left = 0
    if len(out) > most:
        if centre is None:
            centre = ((box["south"] + box["north"]) / 2, (box["west"] + box["east"]) / 2) if box else (0.0, 0.0)
        cy, cx = centre
        shrink = math.cos(math.radians(cy))
        out.sort(key=lambda p: (p["lat"] - cy) ** 2 + ((p["lon"] - cx) * shrink) ** 2)
        left, out = len(out) - most, out[:most]
    return out, left


def fetch_points(sources: Sources, box: dict, run=None, centre: tuple[float, float] | None = None
                 ) -> tuple[list[dict], int]:
    """One Overpass query for the box. Sends only the box's corners. Never retries on its own.
    (the places, how many were left out)."""
    run = _as_run(run)
    host = _host(sources.overpass_url)
    q = QUERY.format(s=f"{box['south']:.5f}", w=f"{box['west']:.5f}", n=f"{box['north']:.5f}", e=f"{box['east']:.5f}")
    body = urllib.parse.urlencode({"data": q}).encode("ascii")
    req = urllib.request.Request(sources.overpass_url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    raw = _request(sources, req, run, OVERPASS_DEADLINE_S, MAX_POINTS_ANSWER)
    if len(raw) > MAX_POINTS_ANSWER:
        raise FetchError(f"{host} sent more than {MAX_POINTS_ANSWER // MB} MB of places, more than Local Map reads. "
                         f"Nothing was changed. Choose a smaller region.")
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FetchError(f"{host} sent something that is not a list of places "
                         f"(it may be busy). Try again later.") from None
    del raw
    if not isinstance(answer, dict):
        raise FetchError(f"{host} sent something that is not a list of places.")
    remark = _clip(answer.get("remark"), 500)
    if "runtime error" in remark or "timed out" in remark:
        raise FetchError(f"{host} ran out of time on this area. Nothing was changed. Try again later, "
                         f"or choose a smaller area.")
    run.check()
    return points_from_overpass(answer, datetime.now(timezone.utc).date().isoformat(), box, centre)


# ---------------------------------------------------------------- by hand

def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Local Map: fetch one box by hand (tiles and emergency points).")
    for side in ("west", "south", "east", "north"):
        p.add_argument("--" + side, type=float, required=True)
    p.add_argument("--max-zoom", type=int, default=12, choices=range(0, 16))
    p.add_argument("--cap-mb", type=float, default=20)
    p.add_argument("--out", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    box = snap_box({"west": a.west, "south": a.south, "east": a.east, "north": a.north})
    src = Sources()
    run = Run(limit_bytes=job_limit(a.cap_mb), cap_mb=a.cap_mb)
    t0 = time.time()
    pl = plan(src, box, a.max_zoom, run)
    print(f"box sent (rounded outward): {box}")
    print(f"build {pl['build']['key']} (version {pl['build']['version']}), map max zoom {pl['map_max_zoom']}")
    print(f"dry run: {pl['tile_count']} tiles of {pl['asked_tiles']} asked, {pl['bytes']} bytes to fetch, "
          f"{pl['directory_bytes']} directory bytes in {pl['directory_requests']} requests, "
          f"{time.time() - t0:.1f} s")
    if pl["bytes"] > a.cap_mb * MB:
        print(f"refused: more than the {a.cap_mb} MB cap")
        return 2
    if a.dry_run:
        return 0
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    store = TileStore(out / "tiles.sqlite")
    store.set_meta(build=pl["build"], box=box, max_zoom=a.max_zoom, tile_compression=pl["tile_compression"])
    t1 = time.time()
    got = download_tiles(src, pl, store, run)
    print(f"tiles: {got['fetched_tiles']} fetched, {got['skipped']} already there, {got['bad']} could not be read, "
          f"{got['bytes']} bytes in {got['requests']} requests, {time.time() - t1:.1f} s; stored {store.counts()} "
          f"(tiles, unique)")
    store.close()
    t2 = time.time()
    try:
        pts, left = fetch_points(src, box, run)
    except FetchError as exc:
        print(f"points: {exc}")
        return 1
    (out / "points.json").write_text(json.dumps(pts, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds: dict = {}
    for pt in pts:
        kinds[pt["kind"]] = kinds.get(pt["kind"], 0) + 1
    print(f"points: {len(pts)} ({left} left out) in {time.time() - t2:.1f} s: {json.dumps(kinds, sort_keys=True)}")
    print(f"total {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
