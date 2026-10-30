# Harvested from HomeBotFounders@b77fc40:src/puffpai/skills_examples/iss_position/skill.py (rewritten-from)
"""ISS position: where the International Space Station is right now, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP), one tool: ``iss_position``

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

The position comes from one public website, api.wheretheiss.at (no key). The
plain place ("over the Pacific Ocean, west of the United States") is worked
out here from the latitude and longitude with a small built-in table of rough
continent outlines, islands and countries; no second website is asked. At most
two requests per call (one try and one retry), each with a timeout, a 64 KB
answer cap and no redirects. Nothing is stored.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = "iss-position"
VERSION = "1.0.0"
HOST = "api.wheretheiss.at"
URL = f"https://{HOST}/v1/satellites/25544"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_ANSWER = 64 * 1024          # bytes read from the website, at most
MAX_REQUESTS = 2                # per call: one try, one retry
CALL_TIMEOUT_S = 20.0           # per call, both tries together
EARTH_KM = 6371.0


# ---------------------------------------------------------------- the world, roughly

# Rough outlines (longitude, latitude) of the land the station flies over. The station never goes
# past 51.6 degrees north or south, so the outlines stop around 55 degrees.
CONTINENTS = {
    "North America": [(-130, 55), (-57, 52), (-66, 44), (-75, 38), (-81, 31), (-80, 25.5), (-84, 30), (-90, 29),
                      (-97, 26), (-97, 21), (-91, 18.5), (-87, 21), (-88, 16), (-83, 15), (-83, 10), (-77, 8.5),
                      (-80, 7), (-85, 10), (-92, 14), (-105, 20), (-110, 23), (-118, 33), (-124, 40), (-124, 48)],
    "South America": [(-77, 8.5), (-60, 10.5), (-50, 0), (-35, -5), (-39, -14), (-48, -26), (-58, -38), (-65, -42),
                      (-68, -52), (-75, -52), (-73, -40), (-71, -30), (-70, -18), (-76, -14), (-81, -5), (-80, 1)],
    "Africa": [(-17, 21), (-5, 36), (10, 37), (20, 32), (32, 31), (34, 28), (43, 12), (51, 12), (40, -3),
               (40, -16), (35, -24), (32, -29), (20, -35), (17, -29), (12, -17), (13, -6), (9, 4), (-8, 4.5),
               (-17, 14)],
    "Europe and Asia": [(-10, 36), (-9, 43), (-2, 44), (-5, 48), (2, 51), (8, 54), (20, 55), (60, 55), (140, 55),
                        (141, 52), (135, 43), (129, 35), (122, 31), (121, 25), (110, 20), (109, 12), (105, 9),
                        (100, 13), (100, 3), (104, 1.5), (98, 8), (98, 16), (94, 16), (92, 22), (87, 21), (80, 15),
                        (77, 8), (73, 17), (67, 25), (57, 25), (56, 27), (50, 30), (48, 29), (51, 24), (56, 26),
                        (59, 22), (52, 16), (43, 12), (39, 21), (35, 28), (34, 31), (36, 36), (28, 37), (26, 40),
                        (23, 37), (20, 40), (19, 42), (13, 45), (16, 41), (18, 40), (12, 38), (9, 44), (3, 43),
                        (0, 38), (-6, 36)],
    "Australia": [(114, -22), (114, -34), (117, -35), (124, -34), (131, -31.5), (138, -35), (141, -38), (147, -39),
                  (150, -37), (153, -28), (153, -25), (146, -19), (142, -11), (141, -17), (136, -12), (130, -12),
                  (126, -14), (122, -18)],
}

# Places: name, then points "lat,lon" or "lat,lon,r" where r (km) means "within r km is over this
# place" (islands the outlines leave out). Big countries get several points so the nearest one is fair.
PLACES = """
the United States:39,-98;45,-120;37,-120;33,-112;33,-85;42,-75;31,-99;45,-93
Canada:50,-115;49,-80;47,-66;51,-100;49.5,-125,150
Mexico:23,-102;18,-97;28,-111
Cuba:22,-79.5,150
Hispaniola:19,-71,150
Central America:14,-87;9,-80
Colombia:4,-73
Venezuela:7,-66
Peru:-10,-76
Ecuador:-1.5,-78
Brazil:-10,-53;-4,-63;-20,-45;-8,-38
Bolivia:-17,-64
Paraguay:-23,-58
Argentina:-34,-64;-25,-63;-45,-68
Chile:-30,-71;-40,-73;-50,-74
Uruguay:-33,-56
the Falkland Islands:-51.7,-59.5,100
Ireland:53,-8,180
the United Kingdom:52.5,-1.5,320
Iceland:64.9,-18.6
Portugal:39.5,-8
Spain:40,-4
France:46.5,2.5
Germany:51,10
Italy:42.5,12.5;38,14,120;40,9,120
Poland:52,19
Ukraine:49,32
Romania:46,25
Greece:39,22
Turkey:39,35
Russia:53,45;53,85;52,115;50,135
Kazakhstan:48,67
Georgia:42,43.5
Iran:32,54
Iraq:33,44
Saudi Arabia:24,45
Egypt:27,30
Libya:27,17
Algeria:28,3
Morocco:32,-6
Mauritania:20,-10
Mali:17,-4
Niger:17,9
Chad:15,19
Sudan:15,30
Ethiopia:9,39
Somalia:5,46
Kenya:0,38
Nigeria:9,8
Ghana:7.5,-1
Senegal:14,-14.5
DR Congo:-3,23
Angola:-12,18
Tanzania:-6,35
Mozambique:-17,35
Namibia:-22,17
Botswana:-22,24
South Africa:-30,25
Madagascar:-19,46.5,350
Mauritius:-20.2,57.5,60
Pakistan:30,70
Afghanistan:34,66
India:22,79;13,77;28,77
Sri Lanka:7.8,80.7,130
Nepal:28,84
Bangladesh:24,90
Myanmar:21,96
Thailand:15,101
Vietnam:15,108
Malaysia:4,102;2,113
China:35,103;30,115;40,116;25,105;44,88
Mongolia:46,104
North and South Korea:38,127
Japan:36,138.5,350;43.5,142.5,200;33,131,200
Taiwan:23.7,121,150
the Philippines:12,122.5,450
Indonesia:-1,102,350;-7.3,110,280;0.5,114,500;-2,121,300;-8.6,117.5,200
Papua New Guinea:-6,145,350
Australia:-25,134;-32,117;-33,146;-20,145;-42,146.5,170
New Zealand:-38.5,176,250;-44,170.5,280
Fiji:-17.8,178,150
New Caledonia:-21.3,165.5,150
French Polynesia:-17.6,-149.5,120
Hawaii:20.5,-157,250
the Galapagos Islands:-0.6,-90.5,80
Bermuda:32.3,-64.8,40
the Azores:38.5,-28,120
the Canary Islands:28.3,-15.8,150
Cape Verde:16,-24,120
Easter Island:-27.1,-109.4,30
"""


def _places() -> list[tuple[str, float, float, float]]:
    out = []
    for line in PLACES.strip().splitlines():
        name, points = line.split(":", 1)
        for point in points.split(";"):
            parts = [float(x) for x in point.split(",")]
            out.append((name, parts[0], parts[1], parts[2] if len(parts) > 2 else 0.0))
    return out


PLACE_POINTS = _places()


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def _direction(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """The compass word for the way from point 1 to point 2 ("west" when the station is west of a place)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    words = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")
    return words[int((bearing + 22.5) // 45) % 8]


def _inside(lon: float, lat: float, outline: list[tuple[float, float]]) -> bool:
    hit = False
    j = len(outline) - 1
    for i, (xi, yi) in enumerate(outline):
        xj, yj = outline[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def _sea(lat: float, lon: float) -> str:
    """Which sea or ocean a point off the land is in, by rough boxes."""
    if 30 <= lat <= 46 and -6 <= lon <= 36:
        return "the Mediterranean Sea"
    if 41 <= lat <= 47 and 27 <= lon <= 42:
        return "the Black Sea"
    if 36.5 <= lat <= 47 and 46.5 <= lon <= 54.5:
        return "the Caspian Sea"
    if 18 <= lat <= 31 and -98 <= lon <= -81:
        return "the Gulf of Mexico"
    if 9 <= lat <= 22 and -84 <= lon <= -60:
        return "the Caribbean Sea"
    if 12 <= lat <= 30 and 32 <= lon <= 43:
        return "the Red Sea"
    if 0 <= lat <= 23 and 105 <= lon <= 121:
        return "the South China Sea"
    if 34 <= lat <= 52 and 127 <= lon <= 142:
        return "the Sea of Japan"
    if -48 <= lat <= -28 and 147 <= lon <= 175:
        return "the Tasman Sea"
    if -25 <= lat <= -10 and 145 <= lon <= 160:
        return "the Coral Sea"
    if lat >= 0:
        if lon < -83 or (lat < 9 and lon < -77):
            return "the Pacific Ocean"
        if lon < 20:
            return "the Atlantic Ocean"
        if lon < 100:
            return "the Indian Ocean"
        return "the Pacific Ocean"
    if lon < -70 or lon >= 147:
        return "the Pacific Ocean"
    if lon < 20:
        return "the Atlantic Ocean"
    return "the Indian Ocean"


def describe(lat: float, lon: float) -> str:
    """A plain place for a latitude and longitude: "over Brazil", or "over the Pacific Ocean, west of Peru"."""
    near = min(PLACE_POINTS, key=lambda p: _km(lat, lon, p[1], p[2]))
    dist = _km(lat, lon, near[1], near[2])
    island = next((p for p in PLACE_POINTS if p[3] and _km(lat, lon, p[1], p[2]) <= p[3]), None)
    if island:
        return f"over {island[0]}"
    land = next((name for name, outline in CONTINENTS.items() if _inside(lon, lat, outline)), None)
    if land:
        return f"over {near[0]}" if dist < 1500 else f"over {land}, nearest {near[0]}"
    sea = _sea(lat, lon)
    if dist > 2500:
        return f"over {sea}, far from land"
    return f"over {sea}, {_direction(near[1], near[2], lat, lon)} of {near[0]}"


# ---------------------------------------------------------------- the website

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the answer must come from api.wheretheiss.at itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


# The Core gives the package HTTPS_PROXY (the Bridge proxy); the default ProxyHandler uses it.
_opener = urllib.request.build_opener(NoRedirect())


def fetch_reading() -> dict:
    """The station's reading from api.wheretheiss.at: one try and, if that fails, one retry."""
    deadline = time.monotonic() + CALL_TIMEOUT_S
    last = "no answer"
    for _ in range(MAX_REQUESTS):
        left = deadline - time.monotonic()
        if left <= 1:
            break
        req = urllib.request.Request(URL, headers={"Accept": "application/json", "User-Agent": f"PuffPAI-{NAME}/{VERSION}"})
        try:
            with _opener.open(req, timeout=min(left, CALL_TIMEOUT_S / MAX_REQUESTS)) as r:
                raw = r.read(MAX_ANSWER + 1)
            if len(raw) > MAX_ANSWER:
                raise ValueError("the answer was bigger than 64 KB")
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the answer was not a reading")
            return data
        except urllib.error.HTTPError as exc:
            last = f"it answered {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = f"it could not be reached ({type(exc).__name__})"
        except (ValueError, UnicodeDecodeError) as exc:
            last = f"its answer would not read ({exc})"
            break
    raise ConnectionError(f"api.wheretheiss.at did not give the station's position: {last}.")


def _reading_number(reading: dict, key: str, lo: float, hi: float) -> float:
    try:
        x = float(reading[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError(f"the website's answer has no usable {key}") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"the website's answer has an impossible {key}")
    return x


# ---------------------------------------------------------------- the logic

class ISS:
    def __init__(self, fetch=fetch_reading) -> None:
        self.fetch = fetch

    def iss_position(self) -> str:
        """From skill.py run(): the reading, rounded, plus a plain place."""
        reading = self.fetch()
        lat = _reading_number(reading, "latitude", -90, 90)
        lon = _reading_number(reading, "longitude", -180, 180)
        alt = _reading_number(reading, "altitude", 0, 100_000)
        vel = _reading_number(reading, "velocity", 0, 1_000_000)
        lat_r, lon_r = round(lat, 1), round(lon, 1)
        ns = f"{abs(lat_r):g} degrees {'north' if lat_r >= 0 else 'south'}"
        ew = f"{abs(lon_r):g} degrees {'east' if lon_r >= 0 else 'west'}"
        line = (f"The ISS is {describe(lat, lon)}, at latitude {lat_r}, longitude {lon_r} ({ns}, {ew}), "
                f"about {int(round(alt))} km up, moving {int(round(vel)):,} km/h.")
        light = reading.get("visibility")
        if light in ("daylight", "eclipsed"):
            line += " It is in sunlight right now." if light == "daylight" else " It is in Earth's shadow right now."
        return line


# --------------------------------------------------------------- MCP tools

TOOLS = [
    ("iss_position", "Where the International Space Station is right now: latitude, longitude, height in km, "
     "speed in km/h, and a plain place such as over the Pacific Ocean, west of Peru.", {}, []),
]
CHANGES: list[str] = []


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "openWorldHint": True}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: ISS, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object."}}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        if not isinstance(args, dict) or args:
            return ok({"content": [{"type": "text", "text": f"{name} takes no arguments."}], "isError": True})
        try:
            text = getattr(app, name)()
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (ConnectionError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"ISS position does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ISSPosition/1"
    app: ISS
    token: str
    timeout = 15        # seconds; a stalled sender is cut off (set on the socket)

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _length(self) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most MAX_BODY bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_BODY:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _body(self):
        n = self._length()
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            raise ValueError("The request ended before its body did.")
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "ISS position needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"ISS position stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

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
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "ISS position has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "ISS position has no page here."})
        try:
            msg = self._body()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        self.do_PUT()


def make_server(port: int, data=None, token: str | None = None, fetch=None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. *data* is accepted for the common shape; nothing is stored.
    Call serve_forever(); stop with shutdown()."""
    app = ISS(fetch or fetch_reading)
    handler = type("ISSHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ISS position for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    if not args.port:
        p.error("PuffPAI starts ISS position with PUFFPAI_PORT (or --port) set.")
    server = make_server(args.port, os.environ.get("PUFFPAI_DATA"), os.environ.get("PUFFPAI_TOKEN"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
