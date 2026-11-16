"""Local Map: an offline street map of one region and its emergency places, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``                        the page (web/index.html), shown in the Desktop at /pkg/local-map/
- ``/vendor/<file>``           Leaflet and protomaps-leaflet, shipped in web/vendor
- ``/tiles/{z}/{x}/{y}.mvt``   the stored street map tiles
- ``/health``                  "ok"
- ``/api/state``, ``/api/updated``, ``/api/job``, ``/api/box``, ``/api/places``   what the page reads (GET)
- ``/api/check``, ``/api/download``, ``/api/places/fetch``, ``/api/cancel``, ``/api/home``, ``/api/settings``,
  ``/api/mine/add``, ``/api/mine/update``, ``/api/mine/remove``                    the page's changes (POST)
- ``/mcp``                     the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The Desktop reaches
the page through the Core's /pkg/local-map/, which adds it.

Nothing is fetched from the internet at start-up or by any tool. Only the owner's Check size,
Download and Fetch places buttons on the page (POST /api/check, /api/download, /api/places/fetch)
contact the hosts in ``needs.hosts``, through the Bridge proxy the Core sets in HTTPS_PROXY. How a
region becomes a box, which tiles are fetched and how points are sorted: PROCESS.md.

Saved in PUFFPAI_DATA: ``map.json`` (the region, the fetched points, the owner's own points, home,
the size cap), ``tiles.sqlite`` (the street map) and, while a download is unfinished,
``tiles-partial.sqlite``. Writes of map.json replace the whole file at once (temp file, then
rename). A map.json that exists but will not read (an empty one included) is never written over.
An owner's point whose position will not read is kept in the file as it is and listed as needing a
fix. A tiles.sqlite that will not read is moved aside as ``tiles.broken-<date>.sqlite`` (one kept)
and the page says so. Messages name files, never the data folder's path.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import gzip
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fetcher  # noqa: E402  (our own file, next to this one)

PAGE = HERE / "web" / "index.html"
VENDOR_DIR = HERE / "web" / "vendor"
VENDOR = {"leaflet.js": "text/javascript; charset=utf-8", "leaflet.css": "text/css; charset=utf-8",
          "protomaps-leaflet.js": "text/javascript; charset=utf-8"}
PLACES = HERE / "data" / "places.tsv.gz"
REGIONS = HERE / "data" / "regions.tsv.gz"
MAX_PLACE_HITS = 20
NAME = "local-map"
VERSION = "1.0.1"
DOC = "map.json"
TILES = "tiles.sqlite"
PARTIAL = "tiles-partial.sqlite"
BROKEN = "tiles.broken-{}.sqlite"
MB = 1048576
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
BODY_WAIT_S = 5.0                       # the whole wait for a request body
REFUSED_MOST, REFUSED_S = 64 * 1024, 2.0            # what a request without the token may send before its 401
DROP_MOST, DROP_S = 64 * 1024 * 1024, 20.0          # a body over the cap is read and dropped, then a plain 400
IDLE_S = 1.0                                        # dropping stops when nothing more arrives for this long
MAX_MINE = 500
DETAILS = {"small": 12, "standard": 14, "detailed": 15}
DETAIL_WORDS = {12: "Small", 14: "Standard", 15: "Detailed"}
DEFAULT_CAP_MB = 250
ROUGH_TILE_KB = 30                      # a rough guess per tile, before the real dry run
PLACES_TRIES = 3                        # the places query, in all, when the host is busy or slow
PLACES_WAITS_S = (15, 45)               # seconds before the second and the third try (Cancel ends a wait at once)
RUNNING = ("checking", "downloading", "fetching_places")
KM_PER_MILE = 1.609344
EARTH_KM = 6371.0088
MY_KINDS = ("Meeting place", "Water", "Neighbor who can help", "Supplies", "Shelter", "Medical", "Other")
ALL_KINDS = fetcher.KINDS + tuple(k for k in MY_KINDS if k not in fetcher.KINDS)
KIND_WORDS = {
    "fire": "Fire station", "fire department": "Fire station", "fire station": "Fire station",
    "police": "Police", "police station": "Police", "sheriff": "Police",
    "hospital": "Hospital", "er": "Hospital", "emergency room": "Hospital",
    "clinic": "Clinic or urgent care", "urgent care": "Clinic or urgent care", "doctor": "Doctor", "doctors": "Doctor",
    "pharmacy": "Pharmacy", "drugstore": "Pharmacy", "chemist": "Pharmacy", "health office": "Public health office",
    "public health": "Public health office", "shelter": "Shelter", "food": "Food bank or meals",
    "food bank": "Food bank or meals", "meals": "Food bank or meals", "soup kitchen": "Food bank or meals",
    "water": "Drinking water", "drinking water": "Drinking water", "water tap": "Drinking water",
    "fuel": "Fuel", "gas": "Fuel", "gas station": "Fuel", "petrol": "Fuel", "community center": "Community center",
    "community centre": "Community center", "town hall": "Town hall", "city hall": "Town hall",
    "assembly point": "Assembly point", "meeting point": "Assembly point", "aed": "Defibrillator",
    "defibrillator": "Defibrillator", "siren": "Siren", "ambulance": "Ambulance station",
    "ambulance station": "Ambulance station", "meeting place": "Meeting place", "neighbor": "Neighbor who can help",
    "neighbour": "Neighbor who can help", "supplies": "Supplies", "medical": "Medical", "other": "Other",
}
HOME_NOTE = ("Home was set to the middle of the map. Change it to your real spot if you like: it is never sent "
             "anywhere.")
COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _number(value, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, str) and len(value) > 40:
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a number from {lo:g} to {hi:g}")
    return x


def _whole(value, name: str, lo: int, hi: int) -> int:
    x = _number(value, name, lo, hi)
    if x != int(x):
        raise ValueError(f"{name} is a whole number from {lo} to {hi}")
    return int(x)


def _yes(value) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, str) and value.strip().lower() in ("yes", "true", "1", "at home", "home"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("no", "false", "0"):
        return False
    raise ValueError("at_home is yes or no")


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _loads(text: str):
    """JSON from a request: NaN and Infinity are refused, and so is very deep nesting."""
    try:
        return json.loads(text, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None


SAME_KIND = ({"Drinking water", "Water"},)


def _kind_is(have, want: str) -> bool:
    """A point's kind matches the asked-for kind; water asked for finds both kinds of water."""
    return not want or have == want or any(have in g and want in g for g in SAME_KIND)


def _kind(value, mine: bool) -> str:
    text = _text(value, "kind", 40)
    if not text:
        return "Other" if mine else ""
    low = text.lower()
    hit = next((k for k in ALL_KINDS if k.lower() == low), None) or KIND_WORDS.get(low) \
        or KIND_WORDS.get(low.rstrip("s"))
    if not hit:
        raise ValueError("kind is one of: " + ", ".join(ALL_KINDS))
    if mine and hit not in MY_KINDS and hit not in fetcher.KINDS:
        raise ValueError("kind is one of: " + ", ".join(ALL_KINDS))
    return hit


def _my_kind(value) -> tuple:
    """A kind for one of the owner's points, from everyday words. An unknown word is stored as Other, never
    refused, and the answer says so. Returns (kind or None when none was said, words to add to the answer)."""
    if value in (None, ""):
        return None, ""
    try:
        return _kind(value, mine=True), ""
    except ValueError:
        if not isinstance(value, str):
            raise
        return "Other", f" The kind {value[:40]!r} is not one this map knows, so it is kept as Other."


def _where(value):
    """Where a point goes: None (not said), "home", or (latitude, longitude) from two numbers or one text such as
    "37.47, -122.43"."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("home", "at home", "my home", "our home", "the home", "here"):
            return "home"
        parts = text.replace(",", " ").split() if len(text) <= 80 else []
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    elif isinstance(value, dict):
        parts = [value.get("latitude", value.get("lat")), value.get("longitude", value.get("lon"))]
    else:
        parts = []
    if len(parts) != 2:
        raise ValueError('where is "home", or latitude and longitude like "37.47, -122.43"')
    return _number(parts[0], "latitude", -90, 90), _number(parts[1], "longitude", -180, 180)


# ---------------------------------------------------------------- distance and direction

def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line (great-circle) distance, by the haversine formula."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The compass bearing from the first point to the second, 0 to 360 degrees, 0 is north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_word(deg: float) -> str:
    return COMPASS[int((deg % 360 + 22.5) // 45) % 8]


def how_far(lat1, lon1, lat2, lon2) -> dict:
    km = distance_km(lat1, lon1, lat2, lon2)
    deg = bearing_deg(lat1, lon1, lat2, lon2)
    word = "here" if km < 0.01 else compass_word(deg)
    return {"km": round(km, 2), "miles": round(km / KM_PER_MILE, 2), "deg": round(deg), "dir": word,
            "words": (f"{km:.1f} km ({km / KM_PER_MILE:.1f} miles) {word} ({round(deg)} degrees), straight-line"
                      if km >= 0.01 else "right here")}


def box_around(lat: float, lon: float, radius_km: float) -> dict:
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(0.01, math.cos(math.radians(lat))))
    box = {"west": round(lon - dlon, 5), "south": round(max(-85.0, lat - dlat), 5),
           "east": round(lon + dlon, 5), "north": round(min(85.0, lat + dlat), 5)}
    if box["west"] < -180 or box["east"] > 180:
        raise ValueError("that area crosses the line where longitude wraps from 180 to -180; choose a point "
                         "further from it or a smaller radius")
    return box


# ---------------------------------------------------------------- the place list (offline)

def _fold(text: str) -> str:
    """Words for matching: accents dropped, case folded, spaces squeezed ("Sao Paulo" finds "São Paulo")."""
    text = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def _name_words(text: str) -> list[str]:
    """The folded words of a name: letters and digits, everything else a gap."""
    return "".join(c if c.isalnum() else " " for c in _fold(text)).split()


def _words_fit(asked: str, name: str) -> bool:
    """Each word asked equals a word of the name or starts one (three letters at least); never letters inside
    a word, so "Ron" does not find "Sharon"."""
    want, have = _name_words(asked), _name_words(name)
    return bool(want) and all(any(h == w or (len(w) >= 3 and h.startswith(w)) for h in have) for w in want)


def _using(p: dict, key: str) -> str:
    """'Using 'Name'. ' when *p* was found by part of its name, '' when its whole name or id was said."""
    if p.get("id") == key:
        return ""
    name, low = _fold(str(p.get("name", ""))), _fold(key)
    if name == low or any(low.startswith(w) and low[len(w):].strip() == name for w in LEAD_WORDS):
        return ""
    return f"Using '{p.get('name')}'. "


class Places:
    """GeoNames towns of 1,000 people or more, searched on this computer only."""

    def __init__(self, path: Path = PLACES, regions: Path = REGIONS) -> None:
        self.path, self.regions_path, self.rows, self.regions = path, regions, None, {}
        self.lock = threading.Lock()

    def _load(self) -> list:
        with self.lock:
            if self.rows is None:
                regions = {}
                with gzip.open(self.regions_path, "rt", encoding="utf-8") as f:
                    next(f, None)
                    for line in f:
                        p = line.rstrip("\n").split("\t")
                        if len(p) == 2:
                            regions[p[0]] = p[1]
                rows = []
                with gzip.open(self.path, "rt", encoding="utf-8") as f:
                    next(f, None)
                    for line in f:
                        p = line.rstrip("\n").split("\t")
                        if len(p) != 7:
                            continue
                        try:
                            lat, lon, pop = float(p[4]), float(p[5]), int(p[6] or 0)
                        except ValueError:
                            continue
                        k1 = _fold(p[0])
                        k2 = _fold(p[1]) if p[1] else k1
                        rows.append((p[0], p[2], p[3], lat, lon, pop, k1, None if k2 == k1 else k2))
                self.rows, self.regions = rows, regions
            return self.rows

    def region_name(self, cc: str, adm: str) -> str:
        return self.regions.get(f"{cc}.{adm}", "")

    def search(self, words, limit: int = MAX_PLACE_HITS) -> list[dict]:
        """Best matches first: the whole name, then the start of a name, then anywhere in it; bigger towns
        first within each. "Name, region or country code" narrows it (each part after a comma must match the
        start of the region's name, its code, or the country code)."""
        q = _fold(_text(words, "place", 80))
        limit = max(1, min(int(limit), MAX_PLACE_HITS))
        parts = [x.strip() for x in q.split(",")]
        town, rest = parts[0], [x for x in parts[1:] if x]
        if len(town) < 2:
            return []
        rows = self._load()
        hits = []
        for name, adm, cc, lat, lon, pop, k1, k2 in rows:
            if k1 == town or k2 == town:
                rank = 0
            elif k1.startswith(town) or (k2 and k2.startswith(town)):
                rank = 1
            elif town in k1 or (k2 and town in k2):
                rank = 2
            else:
                continue
            if rest:
                region = _fold(self.region_name(cc, adm))
                if not all(r in (adm.casefold(), cc.casefold()) or (region and region.startswith(r)) for r in rest):
                    continue
            hits.append((rank, -pop, name, adm, cc, lat, lon, pop))
        hits.sort()
        out = []
        for rank, _, name, adm, cc, lat, lon, pop in hits[:limit]:
            region = self.region_name(cc, adm)
            out.append({"name": name, "region": region, "country": cc, "exact": rank == 0,
                        "label": ", ".join(x for x in (name, region or adm, cc) if x), "lat": lat, "lon": lon,
                        "population": pop})
        return out


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


UNMARKED_KEYS = ("region", "points", "mine", "home", "cap_mb", "last_updated")


def default_doc() -> dict:
    return {"app": NAME, "region": None, "points": [], "mine": [], "home": None, "cap_mb": DEFAULT_CAP_MB,
            "last_updated": "", "_held": []}


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        """The document. The owner's points whose position will not read are kept, untouched, in "_held"
        (save writes them back); fetched points that will not read are left out (a refresh brings them back)."""
        with self.lock:
            if not self.path.exists():
                return default_doc()
            try:
                text = self.path.read_text(encoding="utf-8")
                if not text.strip():
                    raise Unreadable(f"{DOC} in Local Map's data folder is empty, so it may have been cut off. Local "
                                     f"Map will not write over it. Move it aside or delete it to start fresh.")
                data = json.loads(text)
            except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
                why = type(exc).__name__ if isinstance(exc, OSError) else str(exc)[:120]
                raise Unreadable(f"{DOC} in Local Map's data folder will not read ({why}). Local Map will not write "
                                 f"over it.") from None
            if not isinstance(data, dict) or not all(isinstance(data.get(k, []), list) for k in ("points", "mine")) \
                    or not all(data.get(k) is None or isinstance(data.get(k), dict) for k in ("region", "home")):
                raise Unreadable(f"{DOC} in Local Map's data folder is not a Local Map file. Local Map will not write "
                                 f"over it.")
            # this app's marker; a file saved before the marker had every one of the keys this app writes
            if data.get("app", NAME) != NAME or ("app" not in data and not set(UNMARKED_KEYS) <= set(data)):
                raise Unreadable(f"{DOC} in Local Map's data folder is not this app's file (it has no Local Map "
                                 f"marker). Local Map will not write over it. Move it aside to start fresh.")
            out = default_doc()
            out.update(data)
            out.pop("_held", None)
            out["points"] = [q for q in (_readable_point(p) for p in out["points"]) if q is not None]
            mine, held = [], []
            for p in out["mine"]:
                q = _readable_point(p)
                if q is None:
                    held.append(p)
                else:
                    mine.append(q)
            out["mine"], out["_held"] = mine, held
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now() + "." + secrets.token_hex(2)
            out = {k: v for k, v in data.items() if k != "_held"}
            out["mine"] = list(data.get("mine") or []) + list(data.get("_held") or [])
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".map-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=1)     # a held point is written back as it was
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


def _coords_ok(p: dict) -> bool:
    lat, lon = p.get("lat"), p.get("lon")
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)) \
        and -90 <= lat <= 90 and -180 <= lon <= 180


def _coord(v, most: float):
    """A latitude or longitude from the file: a number, or a number written as words like "1.5"; else None."""
    if isinstance(v, str) and len(v) <= 40:
        try:
            v = float(v)
        except ValueError:
            return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not -most <= v <= most:
        return None
    return v


def _readable_point(p):
    """A copy of a saved point with a readable position (numbers written as words become numbers), or None."""
    if not isinstance(p, dict):
        return None
    lat, lon = _coord(p.get("lat"), 90), _coord(p.get("lon"), 180)
    if lat is None or lon is None:
        return None
    return {**p, "lat": lat, "lon": lon}


def _held_view(p) -> dict:
    """How a point that needs fixing is shown: its id, name, kind and note as plain short words."""
    if not isinstance(p, dict):
        return {"id": "", "name": "An entry with no name", "kind": "", "note": ""}

    def words(v, most):
        return " ".join(v.split())[:most] if isinstance(v, str) else ""
    return {"id": words(p.get("id"), 40), "name": words(p.get("name"), 80) or "Unnamed",
            "kind": words(p.get("kind"), 40), "note": words(p.get("note"), 300)}


# ---------------------------------------------------------------- the download job

class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = {"state": "idle", "message": "", "done_bytes": 0, "total_bytes": 0, "plan": None}

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def view(self) -> dict:
        with self.lock:
            return dict(self.state)


def _places_why(exc: Exception) -> str:
    """Why the places could not be fetched, in the host's own plain sentence, without "Nothing was changed" or
    "Try again later" (said where those are not true, or are said another way)."""
    parts = re.split(r"(?<=\.)\s+", str(exc).strip())
    keep = [p for p in parts if not p.startswith(("Nothing was changed", "Try again later", "Tiles already fetched"))]
    return " ".join(keep) or "the places host did not answer."


def _places_missing(doc: dict) -> bool:
    """The region is there but its emergency places could not be fetched: the mark is there and is not null and
    not false (any other value, {} and [] included, means missing). A file without the mark (one saved before
    version 1.0.1 included) means the places are there. The tools and the page both use this."""
    r = doc.get("region")
    if not isinstance(r, dict) or "places_missing" not in r:
        return False
    mark = r["places_missing"]
    return mark is not None and mark is not False


def _places_reason(doc: dict) -> str:
    """The saved reason the places are missing, as short plain words; "" when none can be read. Never raises."""
    r = doc.get("region")
    mark = r.get("places_missing") if isinstance(r, dict) else None
    why = mark.get("reason") if isinstance(mark, dict) else mark
    if not isinstance(why, str):
        return ""
    why = " ".join(why.split())
    return why if len(why) <= 300 else why[:297].rstrip() + "..."


def _emergency_kind(kind: str) -> bool:
    """No kind, an emergency kind, or a kind grouped with one (the owner's Water goes with Drinking water)."""
    return not kind or kind in fetcher.KINDS or any(kind in g and g & set(fetcher.KINDS) for g in SAME_KIND)


def _box_ok(box) -> bool:
    return isinstance(box, dict) and all(
        _coord(box.get(k), most) is not None and not isinstance(box.get(k), str)
        for k, most in (("west", 180), ("south", 90), ("east", 180), ("north", 90)))


def _places_not_fetched(doc: dict) -> str:
    """The honest sentence when the region's places are missing (end of an answer)."""
    if doc["points"]:
        what = ("the emergency places for this map could not be fetched with the latest download; the ones kept "
                "are from before and may be out of date.")
    else:
        what = "the emergency places for this map have not been fetched yet."
    return (what + " The owner can press Fetch places on the Local Map page to get them; your own points and home "
                   "still work.")


def _region_request(body: dict, cap_mb: float) -> dict:
    """lat, lon, radius, unit, detail and a label, checked; plus the box and the tile counts."""
    lat = _number(body.get("lat"), "latitude", -84, 84)
    lon = _number(body.get("lon"), "longitude", -180, 180)
    unit = _text(body.get("unit"), "unit", 10, "km").lower()
    if unit in ("mi", "mile", "miles"):
        unit = "miles"
    elif unit in ("km", "kilometre", "kilometres", "kilometer", "kilometers"):
        unit = "km"
    else:
        raise ValueError("unit is km or miles")
    most = 50 if unit == "km" else 31
    radius = _number(body.get("radius"), "radius", 0.5, most)
    radius_km = radius * KM_PER_MILE if unit == "miles" else radius
    detail = _text(body.get("detail"), "detail", 12, "standard").lower()
    if detail not in DETAILS:
        raise ValueError("detail is small, standard or detailed")
    label = _text(body.get("label"), "label", 120) or f"{lat:.4f}, {lon:.4f}"
    box = fetcher.snap_box(box_around(lat, lon, radius_km))    # what is fetched and sent: rounded outward
    counts = {d: fetcher.count_tiles(box["west"], box["south"], box["east"], box["north"], z)
              for d, z in DETAILS.items()}
    return {"lat": lat, "lon": lon, "radius": radius, "unit": unit, "radius_km": round(radius_km, 3),
            "detail": detail, "max_zoom": DETAILS[detail], "label": label, "box": box, "tile_counts": counts,
            "rough_mb": {d: round(n * ROUGH_TILE_KB / 1024, 1) for d, n in counts.items()}, "cap_mb": cap_mb}


# ---------------------------------------------------------------- the logic

class NoHome(Exception):
    """Home is not set: a plain answer, not an error."""


class Choose(ValueError):
    """A name matched no point or several: nothing changed, and the answer names the candidates. The page
    sees it as a 400; the brain gets it as a plain answer, not an error."""

    def __init__(self, text: str, matches: list | None = None) -> None:
        super().__init__(text)
        self.matches = matches or []


LEAD_WORDS = ("the ", "our ", "my ", "a ", "an ")


class LocalMap:
    def __init__(self, store: Store, sources: fetcher.Sources | None = None, places: Places | None = None) -> None:
        self.store = store
        self.sources = sources or fetcher.Sources()
        self.places = places or Places()
        self.job = Job()
        self.tiles_lock = threading.RLock()
        self._tiles_db: sqlite3.Connection | None = None
        self._compression: int | None = None
        self.notice = ""                         # a plain sentence for the page, like a moved-aside tile file

    # ---- tiles
    def _open_tiles(self) -> bool:
        """Open tiles.sqlite to read (call with tiles_lock held). One that will not read is moved aside."""
        if self._tiles_db is not None:
            return True
        path = self.store.folder / TILES
        if not path.exists() or path.stat().st_size == 0:
            return False
        db = None
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
            row = db.execute("SELECT value FROM meta WHERE key = 'tile_compression'").fetchone()
            db.execute("SELECT z FROM tiles LIMIT 1").fetchone()
            compression = json.loads(row[0]) if row else 1
        except (sqlite3.DatabaseError, ValueError) as exc:
            if db is not None:
                db.close()
            if "locked" not in str(exc) and "busy" not in str(exc):
                self._move_broken(path)
            return False
        self._tiles_db, self._compression = db, compression
        return True

    def _move_broken(self, path: Path) -> None:
        dest = path.with_name(BROKEN.format(datetime.now().strftime("%Y-%m-%d")))
        try:
            for old in path.parent.glob(BROKEN.format("*")):
                old.unlink()
            os.replace(path, dest)
        except OSError:
            self.notice = (f"The street map file ({TILES}) will not read, and Local Map could not move it aside. "
                           f"Download the region again to get the street map back.")
            return
        self.notice = (f"The street map file ({TILES}) would not read, so Local Map moved it aside as {dest.name} "
                       f"(only the newest such file is kept). Download the region again to get the street map back.")

    def tile(self, z: int, x: int, y: int) -> tuple[bytes | None, bool]:
        """(the tile's bytes or None, whether they are gzip-compressed)."""
        with self.tiles_lock:
            if not self._open_tiles():
                return None, False
            try:
                row = self._tiles_db.execute(
                    "SELECT b.data FROM tiles t JOIN blobs b ON b.hash = t.hash WHERE t.z = ? AND t.x = ? AND t.y = ?",
                    (z, x, y)).fetchone()
            except sqlite3.DatabaseError:
                return None, False
            return (row[0] if row else None), self._compression == 2

    def _close_tiles(self) -> None:
        with self.tiles_lock:
            if self._tiles_db is not None:
                self._tiles_db.close()
                self._tiles_db = None

    def tiles_size(self) -> int:
        p = self.store.folder / TILES
        return p.stat().st_size if p.exists() else 0

    # ---- helpers
    def _all_points(self, doc: dict) -> list[dict]:
        mine = [{**p, "mine": True, "source": "you"} for p in doc["mine"]]
        return [{**p, "mine": False} for p in doc["points"]] + mine

    @staticmethod
    def _home(doc: dict) -> dict | None:
        h = doc.get("home")
        return h if isinstance(h, dict) and _coords_ok(h) else None

    def _find(self, doc: dict, key, name: str = "point", mine_only: bool = False) -> dict:
        key = _text(key, name, 120)
        if not key:
            raise ValueError(f"say which {name}, by its name or id")
        pool = doc["mine"] + [h for h in doc["_held"] if isinstance(h, dict)] if mine_only else self._all_points(doc)
        for p in pool:
            if p.get("id") == key:
                return p
        low = _fold(key)
        tries = [low]
        bare = next((low[len(w):] for w in LEAD_WORDS if low.startswith(w) and low[len(w):].strip()), "")
        if bare:
            tries.append(bare)                       # "the library steps" finds "Library steps"
        whose = "of your points" if mine_only else "of the map's points"
        passes = [[p for p in pool if _fold(str(p.get("name", ""))) == word] for word in tries]   # a whole name wins,
        passes += [[p for p in pool if _words_fit(word, str(p.get("name", "")))] for word in tries]  # with or without "the"
        for named in passes:
            if True:
                if len(named) == 1:
                    return named[0]
                if named:
                    raise Choose(f"{len(named)} {whose} match {key!r}, so nothing was done. "
                                 f"Say which one by its full name or id: "
                                 + "; ".join(f"{p.get('name')} ({p.get('kind')}, id {p.get('id')})"
                                             for p in named[:8]) + ("; and more." if len(named) > 8 else "."),
                                 named)
        if mine_only:
            names = [str(p.get("name") or "Unnamed") for p in pool]
            raise Choose(f"None of your points is called {key!r}, so nothing was done. "
                         + (f"Your points are: {', '.join(names[:20])}{', and more' if len(names) > 20 else ''}."
                            if names else "You have no points of your own yet."))
        raise Choose(f"There is no point on the map called {key!r}.")

    @staticmethod
    def _describe(p: dict, origin: dict | None) -> str:
        bits = [f"{p.get('name') or 'Unnamed'} ({p.get('kind')})"]
        if origin:
            bits.append(how_far(origin["lat"], origin["lon"], p["lat"], p["lon"])["words"])
        if p.get("phone"):
            bits.append(f"phone {p['phone']}")
        addr = p.get("address") if isinstance(p.get("address"), dict) else {}
        street = " ".join(x for x in (addr.get("housenumber"), addr.get("street")) if x)
        where = ", ".join(x for x in (street, addr.get("city")) if x)
        if where:
            bits.append(where)
        if p.get("opening_hours"):
            bits.append(f"hours {p['opening_hours']}")
        if p.get("note"):
            bits.append(f"note: {p['note']}")
        bits.append("your own point" if p.get("mine") else f"source {p.get('source', 'OpenStreetMap')}, fetched "
                                                              f"{p.get('fetched', '?')}")
        return "; ".join(bits)

    # ---- tools
    def nearest(self, kind=None, count=3, from_point=None) -> str:
        kind = _kind(kind, mine=False) if kind not in (None, "") else ""
        count = _whole(count if count not in (None, "") else 3, "count", 1, 20)
        doc = self.store.load()
        if from_point not in (None, ""):
            p = self._find(doc, from_point, "from_point")
            origin, where = {"lat": p["lat"], "lon": p["lon"]}, f"from {p.get('name')}"
            using = _using(p, _text(from_point, "from_point", 120))
        else:
            origin, where, using = self._home(doc), "from home", ""
            if origin is None:
                raise NoHome("Home is not set yet. Tap the map on the Local Map page to set it, or tell me its "
                             "coordinates, or ask from a named point.")
        pool = [p for p in self._all_points(doc) if _kind_is(p.get("kind"), kind)]
        pool = [p for p in pool if not (from_point and p.get("lat") == origin["lat"] and p.get("lon") == origin["lon"])]
        if not pool:
            what = kind.lower() if kind else "point"
            if _places_missing(doc) and _emergency_kind(kind):
                return using + f"I can not say where the nearest {what} is: " + _places_not_fetched(doc)
            return using + (f"There is no {what} on the map yet." if not doc.get("region") else
                            f"There is no {what} in the downloaded region. OpenStreetMap may not list one there; "
                            f"you can add your own.")
        pool.sort(key=lambda p: distance_km(origin["lat"], origin["lon"], p["lat"], p["lon"]))
        lines = [f"{i}. {self._describe(p, origin)}" for i, p in enumerate(pool[:count], 1)]
        if _places_missing(doc) and _emergency_kind(kind):
            lines.append("Note: " + _places_not_fetched(doc))
        return f"{using}Nearest {kind.lower() if kind else 'points'} {where} (straight-line distance, not by road):\n" \
            + "\n".join(lines)

    def list_points(self, kind=None, words=None, limit=20) -> str:
        kind = _kind(kind, mine=False) if kind not in (None, "") else ""
        words = _text(words, "words", 80).lower()
        limit = _whole(limit if limit not in (None, "") else 20, "limit", 1, 100)
        doc = self.store.load()
        pool = [p for p in self._all_points(doc) if _kind_is(p.get("kind"), kind)
                and (not words or words in (str(p.get("name", "")) + " " + str(p.get("note", ""))).lower())]
        missing = _places_missing(doc) and _emergency_kind(kind)
        if not pool:
            if missing:
                return "No points match: " + _places_not_fetched(doc)
            return "No points match." if doc.get("region") or doc["mine"] else "There is no region downloaded yet."
        home = self._home(doc)
        if home:
            pool.sort(key=lambda p: distance_km(home["lat"], home["lon"], p["lat"], p["lon"]))
        else:
            pool.sort(key=lambda p: (str(p.get("kind")), str(p.get("name", "")).lower()))
        head = f"{_plural(len(pool), 'point')}" + (f", nearest home first" if home else ", by kind (home is not set)")
        more = f"\n...and {len(pool) - limit} more." if len(pool) > limit else ""
        if missing:
            more += "\nNote: " + _places_not_fetched(doc)
        return head + ":\n" + "\n".join(f"- {self._describe(p, home)}" for p in pool[:limit]) + more

    def where_is(self, name) -> str:
        key = _text(name, "name", 120)
        if not key:
            raise ValueError("say the name of a place")
        doc = self.store.load()
        home = self._home(doc)

        def away(lat, lon) -> str:
            return how_far(home["lat"], home["lon"], lat, lon)["words"] + " from home" if home else "no distance"

        try:
            p = self._find(doc, key, "name")
        except Choose as exc:
            if exc.matches:                          # several points match: name them all, with where they are
                return (f"{_plural(len(exc.matches), 'point')} on the map match {key!r}. Say which one:\n" + "\n".join(
                    f"- {q.get('name')} ({q.get('kind')}) at {q['lat']:.5f}, {q['lon']:.5f}, {away(q['lat'], q['lon'])}"
                    for q in exc.matches[:8]))
            p = None
        if p is not None:
            text = f"{_using(p, key)}{self._describe(p, None)}. It is at {p['lat']:.5f}, {p['lon']:.5f}"
            lat, lon = p["lat"], p["lon"]
        else:
            town = key.split(",")[0]
            hits = [h for h in self.places.search(key) if h["exact"] or _words_fit(town, h["name"])]
            if not hits:
                if _places_missing(doc):
                    return f"I could not find {key!r} among the map's points or the towns list: " \
                        + _places_not_fetched(doc)
                return f"I could not find {key!r} among the map's points or the towns list."
            same = [h for h in hits if h["exact"]] or hits
            if len(same) > 1:
                return (f"{_plural(len(same), 'town')} in the place list match {key!r}. Say which one, like "
                        f"\"{same[0]['name']}, {same[0]['region'] or same[0]['country']}\":\n" + "\n".join(
                            f"- {h['label']} (about {h['population']:,} people) at {h['lat']:.4f}, {h['lon']:.4f}, "
                            f"{away(h['lat'], h['lon'])}" for h in same[:8]))
            h = same[0]
            text = (f"{'' if h['exact'] else _using(h, town)}{h['label']} (a town in the place list) is at "
                    f"{h['lat']:.4f}, {h['lon']:.4f}")
            lat, lon = h["lat"], h["lon"]
        if home:
            text += f", {away(lat, lon)}."
        else:
            text += ". Home is not set, so there is no distance."
        return text

    def _add(self, doc: dict, name: str, kind: str, lat: float, lon: float, note: str) -> dict:
        """Adds one of the owner's points; the caller holds the lock and has loaded doc."""
        if len(doc["mine"]) + len(doc["_held"]) >= MAX_MINE:
            raise ValueError(f"Local Map keeps at most {MAX_MINE} of your own points")
        p = {"id": "m" + secrets.token_hex(4), "name": name, "kind": kind, "lat": round(lat, 6),
             "lon": round(lon, 6), "note": note, "added": _now()}
        doc["mine"].append(p)
        self.store.save(doc)
        return {"id": p["id"], "text": f"Added your point {name} ({kind}) at {lat:.5f}, {lon:.5f}. It is kept when "
                                       f"the region is downloaded again."}

    def _change(self, doc: dict, p: dict, name=None, kind=None, lat=None, lon=None, note=None) -> str:
        """Changes one of the owner's points; the caller holds the lock. Only the values given are changed."""
        changed = []
        if name:
            p["name"] = name
            changed.append("name")
        if kind:
            p["kind"] = kind
            changed.append("kind")
        if lat is not None:
            p["lat"], p["lon"] = round(lat, 6), round(lon, 6)
            changed.append("place")
        if note is not None:
            p["note"] = note
            changed.append("note")
        if not changed:
            raise ValueError("say what to change: name, kind, place or note")
        fixed = ""
        if any(h is p for h in doc["_held"]):
            q = _readable_point(p)
            if q is not None:              # it had a position that would not read; now it does
                doc["_held"] = [h for h in doc["_held"] if h is not p]
                doc["mine"].append(q)
                fixed = " It is fixed and on the map again."
            else:
                fixed = " Its position still needs fixing: give a latitude and longitude."
        self.store.save(doc)
        return f"Changed your point {p.get('name')} ({', '.join(changed)}).{fixed}"

    def add_point(self, name, kind=None, latitude=None, longitude=None, at_home=None, note=None) -> dict:
        """The page's Add button."""
        name = _text(name, "name", 80)
        if not name:
            raise ValueError("a point needs a name")
        kind = _kind(kind, mine=True)
        note = _text(note, "note", 300)
        with self.store.lock:
            doc = self.store.load()
            if _yes(at_home):
                home = self._home(doc)
                if not home:
                    raise ValueError("home is not set yet, so say the coordinates")
                lat, lon = home["lat"], home["lon"]
            else:
                if latitude in (None, "") or longitude in (None, ""):
                    raise ValueError("say latitude and longitude, or at_home")
                lat, lon = _number(latitude, "latitude", -90, 90), _number(longitude, "longitude", -180, 180)
            return self._add(doc, name, kind, lat, lon, note)

    def change_point(self, point, name=None, kind=None, latitude=None, longitude=None, note=None) -> str:
        """The page's Save button for a point being edited (found by its id)."""
        name = _text(name, "name", 80) if name not in (None, "") else None
        kind = _kind(kind, mine=True) if kind not in (None, "") else None
        lat = lon = None
        if latitude not in (None, "") or longitude not in (None, ""):
            lat, lon = _number(latitude, "latitude", -90, 90), _number(longitude, "longitude", -180, 180)
        note = _text(note, "note", 300) if note is not None else None
        with self.store.lock:
            doc = self.store.load()
            return self._change(doc, self._find(doc, point, "point", mine_only=True), name, kind, lat, lon, note)

    def save_my_point(self, name, new_name=None, kind=None, where=None, note=None) -> str:
        """The brain's one tool for the owner's points: the point with that name is changed, or else added."""
        key = _text(name, "name", 80)
        if not key:
            raise ValueError("say the point's name, like meeting place")
        new_name = _text(new_name, "new_name", 80) or None
        kind, kind_said = _my_kind(kind)
        note = _text(note, "note", 300) if note not in (None, "") else None
        spot = _where(where)
        with self.store.lock:
            doc = self.store.load()
            if spot == "home":
                home = self._home(doc)
                if not home:
                    raise ValueError("home is not set yet, so say the latitude and longitude")
                spot = (home["lat"], home["lon"])
            try:
                p = self._find(doc, key, "point", mine_only=True)
            except Choose as exc:
                if exc.matches or new_name:      # several match, or a rename of a point that is not there
                    raise
                p = None
            if p is None:
                if spot is None:
                    raise ValueError(f"{key} is not on the map yet, so say where: home, or latitude and longitude")
                bare = next((key[len(w):] for w in ("our ", "my ") if key.lower().startswith(w)), key)
                text = self._add(doc, bare, kind or "Other", spot[0], spot[1], note or "")["text"]
            elif not (new_name or kind or spot or note is not None):
                return (f"{_using(p, key)}Your point {p.get('name')} is already on the map, so nothing was changed. "
                        f"Say a new name, kind, place or note.")
            else:
                using = _using(p, key)
                text = using + self._change(doc, p, new_name, kind, *(spot or (None, None)), note)
        return text + kind_said

    def remove_my_point(self, point) -> str:
        with self.store.lock:
            doc = self.store.load()
            p = self._find(doc, point, "point", mine_only=True)
            doc["mine"] = [x for x in doc["mine"] if x is not p]
            doc["_held"] = [x for x in doc["_held"] if x is not p]
            self.store.save(doc)
        return f"{_using(p, _text(point, 'point', 120))}Removed your point {p.get('name')}."

    def set_home(self, latitude=None, longitude=None, label=None) -> str:
        lat, lon = _number(latitude, "latitude", -90, 90), _number(longitude, "longitude", -180, 180)
        label = _text(label, "label", 80, "Home")
        with self.store.lock:
            doc = self.store.load()
            doc["home"] = {"lat": round(lat, 6), "lon": round(lon, 6), "label": label}
            self.store.save(doc)
        out = f"Home is set at {lat:.5f}, {lon:.5f}. Distances are measured from here, in a straight line."
        box = (doc.get("region") or {}).get("box")
        if isinstance(box, dict) and not (box.get("south", 0) <= lat <= box.get("north", 0)
                                          and box.get("west", 0) <= lon <= box.get("east", 0)):
            out += " Note: it is outside the downloaded region."
        return out

    def map_status(self) -> str:
        doc = self.store.load()
        r = doc.get("region")
        lines = []
        if not r:
            lines.append("No region is downloaded yet. Pick one on the Local Map page and press Download.")
        else:
            lines.append(f"Region: {r.get('label')}, {r.get('radius')} {r.get('unit')} around "
                         f"{r.get('lat'):.4f}, {r.get('lon'):.4f}; detail {DETAIL_WORDS.get(r.get('max_zoom'), '?')} "
                         f"(zoom 0 to {r.get('max_zoom')}).")
            lines.append(f"Fetched {r.get('fetched_on', '?')[:10]}; street map {self.tiles_size() / 1048576:.1f} MB "
                         f"on disk, {_plural(int(r.get('tiles') or 0), 'tile')}; map build {r.get('build', '?')}.")
            kinds: dict = {}
            for p in doc["points"]:
                kinds[p.get("kind")] = kinds.get(p.get("kind"), 0) + 1
            lines.append(f"{_plural(len(doc['points']), 'emergency point')} from OpenStreetMap"
                         + (": " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) if kinds else "") + ".")
            if _places_missing(doc):
                why = _places_reason(doc)
                why = why if why.endswith("...") else why.rstrip(".")
                lines.append("The emergency places could not be fetched" + (f" ({why})" if why else "")
                             + ": " + _places_not_fetched(doc))
        lines.append(f"{_plural(len(doc['mine']), 'point')} of your own.")
        if doc["_held"]:
            lines.append(f"{_plural(len(doc['_held']), 'more point')} of yours need fixing: their position in the "
                         f"saved file will not read. They are kept as they are; give each a latitude and longitude.")
        home = self._home(doc)
        lines.append((f"Home is set at {home['lat']:.5f}, {home['lon']:.5f}." + (" " + HOME_NOTE if home.get("auto")
                                                                                  else "")) if home else
                     "Home is not set.")
        job = self.job.view()
        if job["state"] in RUNNING:
            lines.append(f"A download is running: {job['message']}")
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)

    # ---- page
    def page_state(self) -> dict:
        doc = self.store.load()
        home = self._home(doc)
        pts = []
        for p in self._all_points(doc):
            q = dict(p)
            if home:
                q["far"] = how_far(home["lat"], home["lon"], p["lat"], p["lon"])
            pts.append(q)
        with self.tiles_lock:
            self._open_tiles()                   # a tile file that will not read is moved aside, and said so
        return {"region": doc.get("region"), "points": pts, "home": home, "cap_mb": doc.get("cap_mb", DEFAULT_CAP_MB),
                "kinds": list(fetcher.KINDS), "my_kinds": list(MY_KINDS), "tiles_mb": round(self.tiles_size() / MB, 2),
                "job": self.job.view(), "last_updated": doc["last_updated"], "details": DETAILS,
                "needs_fixing": [_held_view(h) for h in doc["_held"]], "notice": self.notice,
                "places_missing": _places_missing(doc), "places_reason": _places_reason(doc),
                "home_note": HOME_NOTE if home and home.get("auto") else ""}

    def set_cap(self, cap_mb) -> str:
        cap = _whole(cap_mb, "cap_mb", 5, 5000)
        with self.store.lock:
            doc = self.store.load()
            doc["cap_mb"] = cap
            self.store.save(doc)
        return f"The size limit is now {cap} MB."

    def region_preview(self, query: dict) -> dict:
        doc = self.store.load()
        return _region_request(query, doc.get("cap_mb", DEFAULT_CAP_MB))

    # ---- the one-time download (page only)
    def start(self, body: dict, download: bool) -> str:
        doc = self.store.load()
        req = _region_request(body, doc.get("cap_mb", DEFAULT_CAP_MB))
        if self.job.busy():
            raise ValueError(self._running_words())
        self.job.cancel = threading.Event()
        run = fetcher.Run(self.job.cancel, fetcher.job_limit(req["cap_mb"]), req["cap_mb"])
        self.job.set(state="checking", message="Asking which map build is newest...", done_bytes=0, total_bytes=0)
        t = threading.Thread(target=self._run, args=(req, download, run), daemon=True)
        self.job.thread = t
        t.start()
        return "Download started." if download else "Size check started."

    def start_places(self) -> str:
        """The page's Fetch places button: the emergency places for the saved region's box, never the tiles."""
        doc = self.store.load()
        r = doc.get("region")
        if not isinstance(r, dict):
            raise ValueError("There is no region downloaded yet. Choose one and press Download first.")
        if not _box_ok(r.get("box")):
            raise ValueError("The saved region can not be read (its area is not in the form Local Map knows), so "
                             "its places can not be fetched. Press Download again to get the region.")
        if self.job.busy():
            raise ValueError(self._running_words())
        box = {k: float(r["box"][k]) for k in ("west", "south", "east", "north")}
        lat, lon = _coord(r.get("lat"), 90), _coord(r.get("lon"), 180)
        centre = (lat, lon) if lat is not None and lon is not None else None
        cap = doc.get("cap_mb", DEFAULT_CAP_MB)
        cap = cap if isinstance(cap, (int, float)) and not isinstance(cap, bool) and 5 <= cap <= 5000 \
            else DEFAULT_CAP_MB
        self.job.cancel = threading.Event()
        run = fetcher.Run(self.job.cancel, fetcher.job_limit(cap), cap)
        host = fetcher._host(self.sources.overpass_url)
        self.job.set(state="fetching_places", message=f"Fetching emergency points from {host} (this can take a "
                                                      f"minute)...", done_bytes=0, total_bytes=0, plan=None)
        t = threading.Thread(target=self._run_places, args=(r["box"], box, centre, run), daemon=True)
        self.job.thread = t
        t.start()
        return "Fetching the emergency places. The street map is not downloaded again."

    def _running_words(self) -> str:
        state = self.job.view()["state"]
        if state == "fetching_places":
            return "Fetch places is already running (the emergency places are being fetched). Wait or press Cancel."
        if state == "downloading":
            return "a download is already running. Wait or press Cancel."
        return "a download or size check is already running. Wait or press Cancel."

    def cancel(self) -> str:
        if not self.job.busy():
            return "Nothing is running."
        self.job.cancel.set()
        if self.job.view()["state"] == "fetching_places":
            return "Stopping. The map and the places you had are unchanged."
        return "Stopping. Tiles already stored are kept for next time."

    def _fetch_places(self, box: dict, run: fetcher.Run, centre) -> tuple[list, int]:
        """fetch_points, tried again up to PLACES_TRIES in all when the trouble may pass (busy, slow). Each wait
        ends at once on Cancel and never runs past the job's deadline; the byte budget is shared by every try."""
        host = fetcher._host(self.sources.overpass_url)
        for n in range(1, PLACES_TRIES + 1):
            try:
                return fetcher.fetch_points(self.sources, box, run, centre)
            except fetcher.FetchError as exc:
                if not getattr(exc, "passing", False) or n >= PLACES_TRIES:
                    raise
                wait_s = PLACES_WAITS_S[min(n, len(PLACES_WAITS_S)) - 1]
                self.job.set(message=f"{host} is busy. Trying again in {_plural(wait_s, 'second')} "
                                     f"(try {n + 1} of {PLACES_TRIES})...")
                if wait_s >= run.ends - time.monotonic():
                    raise                            # no time for another try: the last real reason stands
                if run.cancel.wait(wait_s):
                    raise fetcher.Cancelled() from None
                if run.late():
                    raise
                self.job.set(message=f"Fetching emergency points from {host}, try {n + 1} of {PLACES_TRIES} "
                                     f"(this can take a minute)...")
        raise AssertionError("not reached")

    def _run_places(self, saved_box: dict, box: dict, centre, run: fetcher.Run) -> None:
        kept = "The street map and the places you had are unchanged."
        try:
            pts, left_out = self._fetch_places(box, run, centre)
            if run.cancel.is_set():
                raise fetcher.Cancelled()
            with self.store.lock:
                doc = self.store.load()
                r = doc.get("region")
                if not isinstance(r, dict) or r.get("box") != saved_box:
                    self.job.set(state="failed", message=f"The region changed while the places were fetched, so "
                                                         f"they were not saved. {kept}")
                    return
                doc["points"] = pts
                r.pop("places_missing", None)
                self.store.save(doc)
            more = ""
            if left_out:
                more = (f" {_plural(left_out, 'more place')} {'was' if left_out == 1 else 'were'} left out: Local "
                        f"Map keeps the {fetcher.MAX_POINTS} nearest the middle of the region.")
            self.job.set(state="done", message=f"Done: {_plural(len(pts), 'emergency point')} for the map. The "
                                               f"street map was not downloaded again.{more}")
        except fetcher.Cancelled:
            self.job.set(state="cancelled", message=f"Cancelled. {kept}")
        except fetcher.FetchError as exc:
            self.job.set(state="failed", message=f"The emergency places could not be fetched: {_places_why(exc)} "
                                                 f"{kept} Press Fetch places to try again later.")
        except Unreadable as exc:
            self.job.set(state="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - said plainly on the page, never a crash
            self.job.set(state="failed", message=f"Fetching the places stopped with an error "
                                                 f"({type(exc).__name__}). {kept}")

    def _plan_summary(self, req: dict, pl: dict) -> dict:
        return {"key": [req["box"], req["max_zoom"], pl["build"]["key"]], "label": req["label"],
                "detail": req["detail"], "build": pl["build"]["key"], "version": pl["build"]["version"],
                "tiles": pl["tile_count"], "bytes": pl["bytes"], "mb": round(pl["bytes"] / 1048576, 1),
                "directory_bytes": pl["directory_bytes"], "box": req["box"], "cap_mb": req["cap_mb"]}

    def _run(self, req: dict, download: bool, run: fetcher.Run) -> None:
        try:
            cached = getattr(self, "_last_plan", None)
            if cached and cached[0] == (json.dumps(req["box"], sort_keys=True), req["max_zoom"]):
                pl = cached[1]
                build = fetcher.newest_build(self.sources, run)
                if build["key"] != pl["build"]["key"]:
                    pl = fetcher.plan(self.sources, req["box"], req["max_zoom"], run, build)
            else:
                self.job.set(message="Reading the map file's directories (no tiles yet)...")
                pl = fetcher.plan(self.sources, req["box"], req["max_zoom"], run)
            self._last_plan = ((json.dumps(req["box"], sort_keys=True), req["max_zoom"]), pl)
            summary = self._plan_summary(req, pl)
            if not pl["build"]["version"].startswith("4."):
                summary["warning"] = (f"This map build says it is version {pl['build']['version']}; the page draws "
                                      f"version 4 maps, so streets may not show.")
            cap = req["cap_mb"] * 1048576
            if pl["bytes"] > cap:
                smaller = {"detailed": "Standard or Small", "standard": "Small"}.get(req["detail"], "a smaller radius")
                self.job.set(state="too_big", plan=summary,
                             message=f"This region at {req['detail'].title()} detail is {summary['mb']} MB "
                                     f"({_plural(pl['tile_count'], 'tile')}), more than your {req['cap_mb']} MB "
                                     f"limit. Choose {smaller}, a smaller radius, or raise the limit.")
                return
            if not download:
                self.job.set(state="checked", plan=summary,
                             message=f"{summary['mb']} MB in {_plural(pl['tile_count'], 'tile')}, within your "
                                     f"{req['cap_mb']} MB limit. Press Download to fetch it.")
                return
            self._download(req, pl, summary, run)
        except fetcher.Cancelled:
            self.job.set(state="cancelled", message="Cancelled. The map you had is unchanged; tiles already "
                                                    "fetched are kept, so Download picks up where it stopped.")
        except fetcher.FetchError as exc:
            self.job.set(state="failed", message=f"{exc} The map you had is unchanged.")
        except Unreadable as exc:
            self.job.set(state="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - said plainly on the page, never a crash
            self.job.set(state="failed", message=f"The download stopped with an error ({type(exc).__name__}). "
                                                 f"The map you had is unchanged.")

    def _download(self, req: dict, pl: dict, summary: dict, run: fetcher.Run) -> None:
        partial = self.store.folder / PARTIAL
        want = {"build": pl["build"]["key"], "box": req["box"], "max_zoom": req["max_zoom"]}
        if partial.exists():
            st = None
            try:
                st = fetcher.TileStore(partial)
                meta = st.meta()
                same = all(meta.get(k) == v for k, v in want.items())
            except (sqlite3.DatabaseError, ValueError):
                same = False
            finally:
                if st is not None:
                    st.close()
            if not same:
                partial.unlink()
        seeded = None
        if not partial.exists():
            seeded = self._seed_partial(partial, want, pl["bytes"])
        try:
            self._download_into(req, pl, summary, run, partial, want)
        except BaseException:
            if seeded is not None:
                self._drop_seed(partial, seeded)
            raise

    def _drop_seed(self, partial: Path, seeded: int) -> None:
        """After a job that did not finish: a seeded copy that holds no tile beyond the map in place is removed.
        One with newly fetched tiles is kept, so Download picks up where it stopped."""
        st = None
        try:
            st = fetcher.TileStore(partial)
            more = st.counts()[0] > seeded
        except sqlite3.DatabaseError:
            more = False
        finally:
            if st is not None:
                st.close()
        if not more:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    def _download_into(self, req: dict, pl: dict, summary: dict, run: fetcher.Run, partial: Path,
                       want: dict) -> None:
        cancel = run.cancel
        folder = self.store.folder
        store = fetcher.TileStore(partial)
        try:
            store.set_meta(**want, tile_compression=pl["tile_compression"])
            self.job.set(state="downloading", plan=summary, total_bytes=pl["bytes"],
                         message="Fetching street map tiles from build.protomaps.com...")
            got = fetcher.download_tiles(self.sources, pl, store, run,
                                         lambda d, t: self.job.set(done_bytes=d, total_bytes=t))
            tiles = store.counts()[0]
        finally:
            store.close()
        self.job.set(message=f"Fetching emergency points from {fetcher._host(self.sources.overpass_url)} (this can "
                             f"take a minute)...")
        failed = None
        try:
            pts, left_out = self._fetch_places(req["box"], run, (req["lat"], req["lon"]))
        except fetcher.FetchError as exc:           # the street map is put in place anyway, below; Cancel is not this
            pts, left_out, failed = [], 0, exc
        if cancel.is_set():
            raise fetcher.Cancelled()
        with self.store.lock:
            doc = self.store.load()                 # an unreadable file stops here, before anything is swapped
            with self.tiles_lock:
                self._open_tiles()                  # a tile file that will not read is moved aside, not written over
                self._close_tiles()
                os.replace(partial, folder / TILES)
            old = doc.get("region")
            doc["region"] = {"label": req["label"], "lat": req["lat"], "lon": req["lon"], "radius": req["radius"],
                             "unit": req["unit"], "radius_km": req["radius_km"], "detail": req["detail"],
                             "max_zoom": req["max_zoom"], "box": req["box"], "build": pl["build"]["key"],
                             "build_version": pl["build"]["version"], "tiles": tiles, "bytes": pl["bytes"],
                             "tile_compression": pl["tile_compression"], "fetched_on": _now()}
            if failed is None:
                doc["points"] = pts
            else:                                   # the places saved before stay only when they cover this same box
                if not (isinstance(old, dict) and old.get("box") == req["box"]):
                    doc["points"] = []
                doc["region"]["places_missing"] = {"reason": _places_why(failed)[:300], "tried_on": _now()}
            home_set = not self._home(doc)
            if home_set:                            # a convenience, said plainly; stays on this computer
                doc["home"] = {"lat": req["lat"], "lon": req["lon"], "label": "Middle of the map", "auto": True}
            self.store.save(doc)
            self.notice = ""
        more = ""
        if got["bad"]:
            more += (f" {_plural(got['bad'], 'tile')} could not be read and {'was' if got['bad'] == 1 else 'were'} "
                     f"not stored; press Download again to try them again.")
        if left_out:
            more += (f" {_plural(left_out, 'more place')} {'was' if left_out == 1 else 'were'} left out: Local Map "
                     f"keeps the {fetcher.MAX_POINTS} nearest the middle of the region.")
        if home_set:
            more += " " + HOME_NOTE
        if failed is not None:
            n = len(doc["points"])
            before = (f" The {_plural(n, 'emergency point')} saved before for this same area "
                      f"{'is' if n == 1 else 'are'} kept." if n else "")
            self.job.set(state="places_missing", done_bytes=got["bytes"],
                         message=f"The street map is in ({_plural(tiles, 'tile')}) and works with no internet. The "
                                 f"emergency places could not be fetched: {_places_why(failed)}{before} Press Fetch "
                                 f"places to try again; the street map will not be downloaded again.{more}")
            return
        self.job.set(state="done", done_bytes=got["bytes"],
                     message=f"Done: {_plural(tiles, 'tile')} ({got['skipped']} were already here) and "
                             f"{_plural(len(pts), 'emergency point')}. The map now works with no internet.{more}")

    def _seed_partial(self, partial: Path, want: dict, planned: int) -> int | None:
        """When the street map in place is this same build, box and detail, start the new download from a copy
        of it, so Download again fetches only the tiles it lacks. Anything else: start empty, as before.
        Returns how many tiles the copy holds, or None when there is no copy."""
        with self.tiles_lock:
            if not self._open_tiles():
                return None
            try:
                meta = {k: json.loads(v) for k, v in self._tiles_db.execute("SELECT key, value FROM meta")}
                have = self._tiles_db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
            except (sqlite3.DatabaseError, ValueError, TypeError):
                return None
            if not all(meta.get(k) == v for k, v in want.items()):
                return None
            size = self.tiles_size()
            try:
                free = shutil.disk_usage(self.store.folder).free
            except OSError:
                free = None
            if free is not None and free < size + planned:
                raise fetcher.FetchError(
                    f"There is not enough free disk space to download the map again: it needs about "
                    f"{(size + planned) / MB:.1f} MB (a copy of the map in place, {size / MB:.1f} MB, plus the new "
                    f"tiles) and {free / MB:.1f} MB is free. Nothing was downloaded. Free some space and press "
                    f"Download again.")
            try:
                shutil.copyfile(self.store.folder / TILES, partial)
            except OSError:
                partial.unlink(missing_ok=True)
                return None
            return have


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_KIND = _p("string", "Optional: one kind, like Hospital, Pharmacy, Shelter, Drinking water, Fire station, Fuel, "
                     "or one of your own kinds like Meeting place.")
_COORDS = {"latitude": _p("number", "Latitude, like 37.4636."), "longitude": _p("number", "Longitude, like -122.4286.")}
TOOLS = [
    ("nearest", "Use when asked for the nearest or closest place of a kind, like the nearest hospital or water: "
     "finds the nearest emergency places of one kind (or of any kind) from home or from a named point on the "
     "offline map, with name, kind, straight-line distance and direction, phone and address.",
     {"kind": _KIND, "count": _p("integer", "How many, 1 to 20. Default 3."),
      "from_point": _p("string", "Optional: measure from this point (by name) instead of from home.")}, []),
    ("list_points", "Use only when asked to list every place of one kind on the offline map, like all the shelters "
     "or all the pharmacies: lists them, nearest home first. Never to put, move or rename a point, and never to look "
     "up one place before another tool: where_is, save_my_point and remove_my_point find a place by name "
     "themselves.", {"kind": _KIND, "words": _p("string", "Optional: words in the name or note."),
                     "limit": _p("integer", "How many to list, 1 to 100. Default 20.")}, []),
    ("where_is", "Use when asked where one place is, or how far one place is from home, by name; no need to look "
     "it up first. Finds a point on the offline map (like City Hall or the library) or a town in the place list "
     "and says its coordinates and its straight-line distance and direction from home. Not to put, move or rename "
     "a point.",
     {"name": _p("string", "The place's name as the person said it, like City Hall or the library.")}, ["name"]),
    ("save_my_point", "Use when the owner wants a place of their own put on the map, moved or renamed, by its name: "
     "if a point with that name is already there it is changed, otherwise it is added. Works by name; no need to "
     "look it up first. Use it straight away with the name as said, like meeting place, water point or Sam next "
     "door.",
     {"name": _p("string", "The point's name as the person said it, like meeting place."),
      "new_name": _p("string", "Optional: a new name, only when renaming, like North Gate."),
      "kind": _p("string", "Optional: Meeting place, Water, Neighbor who can help, Supplies, Shelter, Medical or "
                           "Other; everyday words are fine."),
      "where": _p("string", "Optional: home, or latitude and longitude like \"37.47, -122.43\"."),
      "note": _p("string", "Optional: a short note, like the library steps.")}, ["name"]),
    ("remove_my_point", "Use when asked to take one of your own points off the map, remove it or delete it, by "
     "name; no need to look it up first. Removes that point.",
     {"point": _p("string", "The point's name as the person said it, like the library steps.")}, ["point"]),
    ("set_home", "Use when asked to set or move home on the offline map to a latitude and longitude, like set home "
     "to 37.46, -122.43: it saves home there straight away, no need to look anything up first; distances are "
     "measured from there.",
     {**_COORDS, "label": _p("string", "Optional: a name for it. Default Home.")}, ["latitude", "longitude"]),
    ("map_status", "Use only when asked about the downloaded map as a whole: which region it holds, its detail, "
     "its size on disk, when it was fetched, how many emergency points of each kind, and whether home is set. Not "
     "for one place, and not to put, move, rename or remove a point.", {}, []),
]
CHANGES = ["save_my_point", "remove_my_point", "set_home"]
DELETES = ["remove_my_point"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: LocalMap, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object."}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def said(text: str, error: bool) -> dict:
        return ok({"content": [{"type": "text", "text": text}], "isError": error})

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            out = getattr(app, name)(**args)
            return said(out["text"] if isinstance(out, dict) else out, False)
        except (NoHome, Choose) as exc:
            return said(str(exc), False)
        except Unreadable as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Local Map does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

TILE_PATH = re.compile(r"^/tiles/(\d{1,2})/(\d{1,8})/(\d{1,8})\.mvt$")
PAGE_ROUTES = ("/api/check", "/api/download", "/api/places/fetch", "/api/cancel", "/api/home", "/api/settings",
               "/api/mine/add", "/api/mine/update", "/api/mine/remove")
MINE_FIELDS = ("name", "kind", "latitude", "longitude", "at_home", "note")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalMap/1"
    app: LocalMap
    token: str
    timeout = 15

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        if body or code not in (202, 204):
            self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _declared(self) -> int | None:
        """Content-Length, strictly: missing is 0; digits only; None when it is not a number."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return None
        return int(raw)

    def _drop(self, n: int, most: int, seconds: float) -> None:
        """Read and throw away up to *n* bytes of body (never more than *most*) within *seconds*, stopping early
        when nothing more arrives for IDLE_S. Nothing is left unread that the sender is still waiting to send,
        so the answer that follows is never lost to a reset."""
        left_bytes, end = min(n, most), time.monotonic() + seconds
        try:
            while left_bytes > 0:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.connection.settimeout(min(left, IDLE_S))
                chunk = self.rfile.read1(min(left_bytes, 65536))
                if not chunk:
                    break
                left_bytes -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _length(self) -> int:
        """Content-Length, at most MAX_BODY bytes. A bigger body is read and dropped first (up to DROP_MOST
        within DROP_S seconds), so the plain 400 that follows reaches the sender."""
        n = self._declared()
        if n is None or n > MAX_BODY:
            self.close_connection = True
            self._drop(DROP_MOST if n is None else n, DROP_MOST, DROP_S)
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return n

    def _body(self):
        n = self._length()
        raw = bytearray()
        end = time.monotonic() + BODY_WAIT_S
        try:
            while len(raw) < n:                      # the whole body within BODY_WAIT_S, however it trickles in
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(n - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError:
            self.close_connection = True
            raise ValueError(f"The request body did not all arrive within {BODY_WAIT_S:g} seconds.") from None
        finally:
            self.connection.settimeout(self.timeout)
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        raw = bytes(raw)
        try:
            return _loads(raw.decode("utf-8")) if raw else {}
        except UnicodeDecodeError:
            raise ValueError("Send UTF-8 JSON.") from None

    def _token_ok(self) -> bool:
        if not self.token:
            return False
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _query(self) -> dict:
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        if len(q) > 2000:
            raise ValueError("The question is too long.")
        return {k: v[0] for k, v in urllib.parse.parse_qs(q, max_num_fields=20).items()}

    def _refused(self) -> bool:
        if self._token_ok():
            return False
        n = self._declared()
        if n is None or n:                     # at most 64 KB, for at most 2 seconds, then the answer
            self._drop(REFUSED_MOST if n is None else n, REFUSED_MOST, REFUSED_S)
        self.close_connection = True
        self._json(401, {"error": "Local Map needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001
            self.close_connection = True
            try:
                self._json(500, {"error": f"Local Map stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._send(405, b"", "text/plain")
        m = TILE_PATH.match(path)
        if m:
            z, x, y = (int(v) for v in m.groups())
            if z > 22 or x >= 1 << z or y >= 1 << z:
                return self._send(404, b"", "application/vnd.mapbox-vector-tile")
            data, gz = self.app.tile(z, x, y)
            if data is None:            # an empty body reads as an empty tile, so the map just shows nothing there
                return self._send(404, b"", "application/vnd.mapbox-vector-tile")
            return self._send(200, data, "application/vnd.mapbox-vector-tile",
                              {"Content-Encoding": "gzip"} if gz else None)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/vendor/") and path[8:] in VENDOR:
                return self._send(200, (VENDOR_DIR / path[8:]).read_bytes(), VENDOR[path[8:]])
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/api/state":
                return self._json(200, self.app.page_state())
            if path == "/api/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"],
                                        "job": self.app.job.view()})
            if path == "/api/job":
                return self._json(200, self.app.job.view())
            if path == "/api/box":
                return self._json(200, self.app.region_preview(self._query()))
            if path == "/api/places":
                return self._json(200, {"places": self.app.places.search(self._query().get("q", ""))})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "Local Map has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            return self._json(404, {"error": "Local Map has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path in ("/api/check", "/api/download"):
                out = {"text": app.start(body, download=path == "/api/download")}
            elif path == "/api/places/fetch":
                out = {"text": app.start_places()}
            elif path == "/api/cancel":
                out = {"text": app.cancel()}
            elif path == "/api/home":
                out = {"text": app.set_home(body.get("latitude"), body.get("longitude"), body.get("label"))}
            elif path == "/api/settings":
                out = {"text": app.set_cap(body.get("cap_mb"))}
            elif path == "/api/mine/add":
                out = app.add_point(**{k: body[k] for k in MINE_FIELDS if k in body})
            elif path == "/api/mine/update":
                fields = {k: body[k] for k in MINE_FIELDS if k in body and k != "at_home"}
                out = {"text": app.change_point(body.get("id"), **fields)}
            else:
                out = {"text": app.remove_my_point(body.get("id"))}
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def _mcp(self):
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None,
                sources: fetcher.Sources | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. *sources* is for tests; the real hosts otherwise."""
    app = LocalMap(Store(data), sources)
    handler = type("LocalMapHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.app = app
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Local Map for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Local Map with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
