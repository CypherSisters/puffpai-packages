# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
# Harvested from HomeBotFounders@b77fc40:src/puffpai/capability_registry.py (rewritten-from)
"""Weather: current weather, a forecast and US severe weather alerts, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

HomeBot's ``auto_weather_forecast`` read Home Assistant weather sensors and
``prep_weather_alerts`` asked the National Weather Service. This package needs
no Home Assistant and no key: it asks three key-free public services, and only
with the chosen place's coordinates:
- geocoding-api.open-meteo.com, once, to turn a place name into coordinates
- api.open-meteo.com, for the current weather and the forecast
- api.weather.gov, for severe weather alerts in the United States

The location and units are one JSON document in PUFFPAI_DATA (``weather.json``).
Writes replace the whole file at once (temp file, then rename). A file that
exists but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = "weather"
VERSION = "1.0.0"
DOC = "weather.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
MAX_ANSWER = 1024 * 1024           # an outside answer bigger than 1 MB is refused
TIMEOUT_S = 15
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ALERTS = "https://api.weather.gov/alerts/active"
HOSTS = ("geocoding-api.open-meteo.com", "api.open-meteo.com", "api.weather.gov")
USER_AGENT = "PuffPAI weather package"       # api.weather.gov asks for one; it names no one

#: WMO weather codes, as open-meteo sends them, in plain words.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "light snow showers", 86: "snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with heavy hail",
}

#: Rough boxes around the United States (lower 48, Alaska, Hawaii, Puerto Rico), for coordinates given
#: without a place name. Only used to pick a default unit and to know whether US alerts apply.
US_BOXES = ((24.4, 49.5, -125.0, -66.9), (51.0, 71.5, -180.0, -129.0), (18.9, 22.3, -160.3, -154.8),
            (17.6, 18.6, -67.3, -64.5))


def _now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a number from {lo:g} to {hi:g}")
    if whole:
        if x != int(x):
            raise ValueError(f"{name} is a whole number")
        return int(x)
    return x


def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this package takes")


def _loads(text: str):
    """JSON from a request: NaN and Infinity are refused."""
    return json.loads(text, parse_constant=_no_constant)


def _in_us(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in US_BOXES)


# ---------------------------------------------------------------- outside calls

class Offline(RuntimeError):
    """An outside service did not answer usefully. The words say which one, plainly."""


class _SameHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to the same https host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise Offline(f"{old.hostname} sent the request on to another place, so Weather stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Proxies come from the environment (the Core points HTTPS_PROXY at the Bridge).
_OPENER = urllib.request.build_opener(_SameHost())


def fetch_json(url: str, params: dict, accept: str = "application/json"):
    """GET *url* with *params* from one of the three declared hosts and return its JSON."""
    host = urllib.parse.urlsplit(url).hostname
    if host not in HOSTS:
        raise Offline(f"Weather only talks to {', '.join(HOSTS)}.")
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        raise Offline(f"{host} answered with error {exc.code}. If the Bridge blocks it, allow {host} "
                      f"for Weather on the Bridge page.") from None
    except (urllib.error.URLError, OSError) as exc:
        raise Offline(f"Could not reach {host} ({getattr(exc, 'reason', exc)}). Is this computer online, "
                      f"and is {host} allowed for Weather on the Bridge?") from None
    if len(raw) > MAX_ANSWER:
        raise Offline(f"{host} sent more than 1 MB, so Weather did not read it.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Offline(f"{host} sent something that is not weather data.") from None


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"location": None, "units": "", "last_updated": ""}


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return default_doc()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Weather will not write over it.") from None
            loc = data.get("location") if isinstance(data, dict) else None
            if not isinstance(data, dict) or not (loc is None or isinstance(loc, dict)):
                raise Unreadable(f"{self.path} is not a Weather file. Weather will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".weather-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
NO_PLACE = "No place is set yet. Tell me a town, or coordinates like 40.71,-74.01, and I will save it."


class NoPlace(Exception):
    """No place saved yet: a plain answer, not an error."""


class Weather:
    def __init__(self, store: Store) -> None:
        self.store = store

    # helpers
    def _place(self) -> tuple[dict, str]:
        doc = self.store.load()
        loc = doc.get("location")
        if not isinstance(loc, dict):
            raise NoPlace(NO_PLACE)
        try:
            lat = _number(loc.get("latitude"), "latitude", -90, 90)
            lon = _number(loc.get("longitude"), "longitude", -180, 180)
        except ValueError:
            raise Unreadable(f"{self.store.path} has no usable coordinates. Set the place again.") from None
        units = doc.get("units") if doc.get("units") in ("c", "f") else "c"
        name = str(loc.get("name") or f"{lat:.2f},{lon:.2f}")[:120]
        return {"name": name, "latitude": lat, "longitude": lon, "country": str(loc.get("country") or "")}, units

    def _ask(self, place: dict, units: str, **params) -> dict:
        query = {"latitude": round(place["latitude"], 4), "longitude": round(place["longitude"], 4),
                 "timezone": "auto", "temperature_unit": "fahrenheit" if units == "f" else "celsius",
                 "wind_speed_unit": "mph" if units == "f" else "kmh", **params}
        data = fetch_json(FORECAST, query)
        if not isinstance(data, dict):
            raise Offline("api.open-meteo.com sent something that is not weather data.")
        return data

    # tools
    def set_location(self, place: str, units: str = "") -> str:
        place = _text(place, "place", 120)
        if not place:
            raise ValueError("place is a town name or coordinates like 40.71,-74.01")
        units = _text(units, "units", 12).lower()
        units = {"celsius": "c", "fahrenheit": "f", "metric": "c", "imperial": "f"}.get(units, units)
        if units not in ("", "c", "f"):
            raise ValueError('units is "c" or "f"')
        m = COORDS.match(place)
        if m:
            lat = _number(m.group(1), "latitude", -90, 90)
            lon = _number(m.group(2), "longitude", -180, 180)
            loc = {"name": f"{lat:.4f},{lon:.4f}", "latitude": lat, "longitude": lon,
                   "country": "US" if _in_us(lat, lon) else ""}
        else:
            found = fetch_json(GEOCODE, {"name": place, "count": 1, "language": "en", "format": "json"})
            hits = found.get("results") if isinstance(found, dict) else None
            if not hits or not isinstance(hits[0], dict):
                return (f"I could not find a place called {place!r}. Try the town and country, "
                        f"or coordinates like 40.71,-74.01.")
            hit = hits[0]
            lat = _number(hit.get("latitude"), "latitude", -90, 90)
            lon = _number(hit.get("longitude"), "longitude", -180, 180)
            parts = [str(hit.get(k) or "").strip()[:60] for k in ("name", "admin1", "country")]
            loc = {"name": ", ".join(p for p in dict.fromkeys(parts) if p) or place, "latitude": lat,
                   "longitude": lon, "country": str(hit.get("country_code") or "").upper()[:2]}
        doc = self.store.load()
        doc["location"] = loc
        doc["units"] = units or ("f" if loc["country"] == "US" else "c")
        self.store.save(doc)
        unit_word = "Fahrenheit" if doc["units"] == "f" else "Celsius"
        return (f"Saved the place: {loc['name']} ({lat:.4f}, {lon:.4f}). Temperatures in {unit_word}. "
                f"Only these coordinates go to the weather services.")

    def current_weather(self) -> str:
        place, units = self._place()
        data = self._ask(place, units, current="temperature_2m,apparent_temperature,relative_humidity_2m,"
                                               "weather_code,wind_speed_10m,precipitation")
        now = data.get("current")
        if not isinstance(now, dict) or now.get("temperature_2m") is None:
            raise Offline("api.open-meteo.com sent no current weather for this place.")
        deg = "F" if units == "f" else "C"
        speed = "mph" if units == "f" else "km/h"
        bits = [f"{round(float(now['temperature_2m']))}{deg}"]
        if now.get("weather_code") in CODES:
            bits.append(CODES[now["weather_code"]])
        if now.get("apparent_temperature") is not None:
            bits.append(f"feels like {round(float(now['apparent_temperature']))}{deg}")
        if now.get("relative_humidity_2m") is not None:
            bits.append(f"humidity {round(float(now['relative_humidity_2m']))}%")
        if now.get("wind_speed_10m") is not None:
            bits.append(f"wind {round(float(now['wind_speed_10m']))} {speed}")
        if now.get("precipitation"):
            bits.append(f"{now['precipitation']} {'inches' if units == 'f' else 'mm'} of rain or snow in the last hour")
        return f"Weather now in {place['name']}: " + ", ".join(bits) + "."

    def forecast(self, days: int = 3) -> str:
        days = _number(days, "days", 1, 7, whole=True)
        place, units = self._place()
        data = self._ask(place, units, forecast_days=days,
                         daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                         **({"precipitation_unit": "inch"} if units == "f" else {}))
        daily = data.get("daily")
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            raise Offline("api.open-meteo.com sent no forecast for this place.")
        deg = "F" if units == "f" else "C"
        rows = []
        for i, when in enumerate(daily["time"][:days]):
            def col(key):
                vals = daily.get(key)
                return vals[i] if isinstance(vals, list) and i < len(vals) else None
            try:
                day = date.fromisoformat(str(when))
                label = "Today" if i == 0 else ("Tomorrow" if i == 1 else day.strftime("%A"))
            except ValueError:
                label = str(when)[:12]
            bits = []
            if col("weather_code") in CODES:
                bits.append(CODES[col("weather_code")])
            hi, lo = col("temperature_2m_max"), col("temperature_2m_min")
            if hi is not None and lo is not None:
                bits.append(f"high {round(float(hi))}{deg}, low {round(float(lo))}{deg}")
            if col("precipitation_probability_max") is not None:
                bits.append(f"{round(float(col('precipitation_probability_max')))}% chance of rain or snow")
            rows.append(f"{label}: " + (", ".join(bits) or "no numbers"))
        return f"Forecast for {place['name']}:\n" + "\n".join(rows)

    def severe_alerts(self) -> str:
        place, _ = self._place()
        if place["country"] != "US":
            return (f"There are no alerts to check for {place['name']}: severe weather alerts come from the US "
                    f"National Weather Service and cover the United States only.")
        data = fetch_json(ALERTS, {"point": f"{place['latitude']:.4f},{place['longitude']:.4f}"},
                          accept="application/geo+json")
        feats = data.get("features") if isinstance(data, dict) else None
        if not isinstance(feats, list):
            raise Offline("api.weather.gov sent no alert list for this place.")
        alerts = [f.get("properties") for f in feats if isinstance(f, dict) and isinstance(f.get("properties"), dict)]
        if not alerts:
            return f"No active severe weather alerts for {place['name']}."
        rows = []
        for a in alerts[:6]:
            event = str(a.get("event") or "Alert")[:80]
            sev = str(a.get("severity") or "")[:20]
            head = str(a.get("headline") or "")[:300]
            rows.append(f"- {event}" + (f" ({sev.lower()})" if sev and sev != "Unknown" else "")
                        + (f": {head}" if head else ""))
        more = f"\n...and {len(alerts) - 6} more." if len(alerts) > 6 else ""
        return f"{len(alerts)} active alert{'s' if len(alerts) != 1 else ''} for {place['name']}:\n" + "\n".join(rows) + more


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("set_location", "Save the place to give weather for: a town name, or coordinates like 40.71,-74.01, and "
     "the temperature unit. A town name is looked up once to get its coordinates.",
     {"place": _p("string", "A town (with its country if it is a common name), or latitude,longitude."),
      "units": _p("string", 'Optional: "c" for Celsius or "f" for Fahrenheit. Default: f in the US, c elsewhere.')},
     ["place"]),
    ("current_weather", "The weather right now at the saved place: temperature, sky, feels-like, humidity and wind.",
     {}, []),
    ("forecast", "The forecast at the saved place for today and the next days: sky, high and low, and the "
     "chance of rain or snow.",
     {"days": _p("integer", "How many days, from 1 (today only) to 7. Default 3.")}, []),
    ("severe_alerts", "Active severe weather alerts (storms, floods, heat advisories) at the saved place, from "
     "the US National Weather Service. Outside the United States there are none to check.", {}, []),
]
CHANGES = ["set_location"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Weather, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

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
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            return said(getattr(app, name)(**args), False)
        except NoPlace as exc:
            return said(str(exc), False)
        except (Unreadable, Offline) as exc:
            return said(str(exc), True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Weather does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Weather/1"
    app: Weather
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
        return _loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

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
        self._json(401, {"error": "Weather needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Weather stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
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
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        self._json(404, {"error": "Weather has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Weather has no page here."})
        try:
            msg = self._body()
        except ValueError:
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
        if self._refused():
            return
        self._send(405, b"", "text/plain")


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Weather(Store(data))
    handler = type("WeatherHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Weather for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Weather with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
