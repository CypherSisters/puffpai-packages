# Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from)
"""Calculator: arithmetic, tips and unit conversions, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/health``      "ok"
- ``/mcp``         the MCP server (JSON-RPC over HTTP): ``calculate``, ``tip``, ``convert``

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d).

``calculate`` reads spoken math ("what's 847 times 62", "15% of 80", "80 + 15%")
and works it out with a small evaluator that walks Python's parse tree and allows
only numbers, + - * / // % **, brackets, pi, e and sqrt, abs, round, min, max.
Names, attributes, strings and any other call are refused. An expression is at
most 200 characters, an exponent at most 1,000, and a whole-number answer at most
about 1,200 digits, so ``9**9**9`` is refused, not computed. It runs on this
computer only, uses no internet and stores nothing.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import ast
import hmac
import json
import math
import operator
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME = "calculator"
VERSION = "1.0.0"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024
MAX_EXPRESSION = 200
MAX_EXPONENT = 1000
MAX_BITS = 4000                 # a whole number of about 1,200 digits
MAX_DEPTH = 40                  # brackets inside brackets


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(str(value).replace(",", "").strip()) if isinstance(value, str) else float(value)
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
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def show(x) -> str:
    """A number for a person: 1,234 or 3.14159265 or 1.5e+30, never a long tail of digits."""
    if isinstance(x, int):
        return f"{x:,}" if x.bit_length() <= 64 else str(x)
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e15:
        return f"{int(x):,}"
    if 1e-6 <= abs(x) < 1e15:
        whole = int(abs(x))
        places = max(0, 10 - len(str(whole))) if whole else 10
        text = f"{x:,.{places}f}".rstrip("0").rstrip(".")
        return text
    return f"{x:.10g}"


# ---------------------------------------------------------------- spoken math (rebot_tools.py:220-262)

_WORD_OPS = [
    (re.compile(r"\bmultiplied\s+by\b"), "*"),
    (re.compile(r"\btimes\b"), "*"),
    (re.compile(r"\bdivided\s+by\b"), "/"),
    (re.compile(r"\bover\b"), "/"),
    (re.compile(r"\bplus\b"), "+"),
    (re.compile(r"\bminus\b"), "-"),
    (re.compile(r"\bmodulo\b"), "%"),
    (re.compile(r"\bmod\b"), "%"),
    (re.compile(r"\bto\s+the\s+power\s+of\b"), "**"),
    (re.compile(r"\bsquared\b"), "**2"),
    (re.compile(r"\bcubed\b"), "**3"),
    (re.compile(r"\bsquare\s+root\s+of\s+(\d+(?:\.\d+)?)"), r"sqrt(\1)"),
    (re.compile(r"\^"), "**"),
    (re.compile(r"(?<=[\s)])x(?=[\s(])"), "*"),          # 847 x 62 or (2+3)x(4+5)
    (re.compile(r"\b(?!0x)(\d+(?:\.\d+)?)x(?=\d)"), r"\1*"),     # 3x4; a hex number like 0x10 is left alone
    (re.compile(r"[×⋅]"), "*"),
    (re.compile(r"÷"), "/"),
]
_LEADING_WORDS = re.compile(r"^(?:what\s+is|what's|whats|calculate|compute|evaluate|solve|how\s+much\s+is)\b[:\s]*", re.I)
_THOUSANDS_COMMA = re.compile(r"(\d),(?=\d{3}(?:\D|$))")
_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+")
_PLUS_PERCENT = re.compile(r"^(.+?)\s*([+-])\s*(\d+(?:\.\d+)?)\s*(?:%|percent)$")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)(?!\s*[\d(.])")


def normalize(expression: str) -> str:
    """Spoken math to an expression: "what's 847 times 62?" is "847 * 62"; "15% of 80" is "(15/100)* 80";
    "80 + 15%" is "(80)*(1+15/100)"; a lone "15%" is "(15/100)"."""
    s = expression.strip().strip("'\"").lower()
    s = _LEADING_WORDS.sub("", s)
    s = s.rstrip(" ?.!=")
    s = _THOUSANDS_COMMA.sub(r"\1", s)
    s = _PERCENT_OF.sub(r"(\1/100)*", s)
    s = _PLUS_PERCENT.sub(r"(\1)*(1\2\3/100)", s)
    s = _PERCENT.sub(r"(\1/100)", s)
    for pattern, replacement in _WORD_OPS:
        s = pattern.sub(replacement, s)
    return s.strip()


# ---------------------------------------------------------------- the safe evaluator (rebot_tools.py:576)

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_CONSTANTS = {"pi": math.pi, "e": math.e}


def _round(x, digits=0):
    return round(x, int(_number(digits, "round's places", -20, 20, whole=True)))


_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": _round, "min": min, "max": max}


def _check(x):
    if isinstance(x, int) and x.bit_length() > MAX_BITS:
        raise ValueError("the answer is too big to show (more than about 1,200 digits)")
    if isinstance(x, complex):
        raise ValueError("the answer is not a real number")
    if isinstance(x, float) and not math.isfinite(x):
        raise ValueError("the answer is too big to show")
    return x


def _eval(node, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ValueError("there are too many brackets inside brackets")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return _check(node.value)
        raise ValueError("only numbers are allowed")
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"{node.id!r} is not a number (the names it knows are pi and e)")
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow):
            if abs(right) > MAX_EXPONENT:
                raise ValueError(f"the exponent {show(right)} is bigger than {MAX_EXPONENT:,}")
            if isinstance(left, int) and isinstance(right, int) and right > 0 \
                    and left not in (0, 1, -1) and abs(left).bit_length() * right > MAX_BITS + 64:
                raise ValueError("the answer is too big to show (more than about 1,200 digits)")
        try:
            return _check(_OPS[type(node.op)](left, right))
        except ZeroDivisionError:
            raise ValueError("that divides by zero") from None
        except OverflowError:
            raise ValueError("the answer is too big to show") from None
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand, depth + 1))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
            raise ValueError("the only functions are sqrt, abs, round, min and max")
        args = [_eval(a, depth + 1) for a in node.args]
        name = node.func.id
        if name in ("sqrt", "abs") and len(args) != 1 or name == "round" and not 1 <= len(args) <= 2 \
                or name in ("min", "max") and not args:
            raise ValueError(f"{name} got the wrong number of numbers")
        if name == "sqrt" and args[0] < 0:
            raise ValueError("a negative number has no real square root")
        try:
            return _check(_FUNCS[name](*args))
        except OverflowError:
            raise ValueError("the answer is too big to show") from None
    raise ValueError("only numbers, + - * / // % **, brackets, pi, e, sqrt, abs, round, min and max are allowed")


def evaluate(expression: str):
    """The value of an expression the evaluator allows, or a plain ValueError."""
    expr = normalize(expression)
    if not expr:
        raise ValueError("there is no expression to work out")
    if len(expr) > MAX_EXPRESSION:
        raise ValueError(f"an expression is at most {MAX_EXPRESSION} characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        raise ValueError(f"{expression.strip()!r} is not a number expression") from None
    value = _eval(tree)
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        value = int(value)
    return value


# ---------------------------------------------------------------- units (no internet)

# Each unit: its kind and how many base units one of it is (metre, kilogram, litre, metre per second).
_UNITS = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048, "yd": 0.9144,
               "mi": 1609.344, "nmi": 1852.0},
    "weight": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "oz": 0.028349523125, "lb": 0.45359237,
               "st": 6.35029318, "ton": 907.18474},
    "volume": {"ml": 0.001, "l": 1.0, "tsp": 0.00492892159375, "tbsp": 0.01478676478125, "floz": 0.0295735295625,
               "cup": 0.2365882365, "pt": 0.473176473, "qt": 0.946352946, "gal": 3.785411784},
    "speed": {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 1852 / 3600, "ft/s": 0.3048},
    "temperature": {"c": 0.0, "f": 0.0, "k": 0.0},
}
_ALIASES = {
    "millimetre": "mm", "millimeter": "mm", "centimetre": "cm", "centimeter": "cm", "metre": "m", "meter": "m",
    "kilometre": "km", "kilometer": "km", "inch": "in", "inches": "in", '"': "in", "foot": "ft", "feet": "ft",
    "'": "ft", "yard": "yd", "mile": "mi", "nautical mile": "nmi", "milligram": "mg", "gram": "g",
    "kilogram": "kg", "kilo": "kg", "kgs": "kg", "tonne": "t", "metric ton": "t", "ounce": "oz", "pound": "lb",
    "lbs": "lb", "stone": "st", "short ton": "ton", "us ton": "ton", "millilitre": "ml", "milliliter": "ml",
    "litre": "l", "liter": "l", "teaspoon": "tsp", "tablespoon": "tbsp", "fluid ounce": "floz", "fl oz": "floz",
    "fl. oz": "floz", "cups": "cup", "pint": "pt", "quart": "qt", "gallon": "gal", "meters per second": "m/s",
    "metres per second": "m/s", "mps": "m/s", "kph": "km/h", "kmh": "km/h", "km/hr": "km/h",
    "kilometers per hour": "km/h", "kilometres per hour": "km/h", "miles per hour": "mph", "knot": "kn",
    "kt": "kn", "kts": "kn", "feet per second": "ft/s", "fps": "ft/s", "celsius": "c", "centigrade": "c",
    "°c": "c", "fahrenheit": "f", "°f": "f", "kelvin": "k", "degrees c": "c", "degrees f": "f",
    "degrees celsius": "c", "degrees fahrenheit": "f",
}
_NAMES = {"mm": "millimetres", "cm": "centimetres", "m": "metres", "km": "kilometres", "in": "inches", "ft": "feet",
          "yd": "yards", "mi": "miles", "nmi": "nautical miles", "mg": "milligrams", "g": "grams", "kg": "kilograms",
          "t": "tonnes", "oz": "ounces", "lb": "pounds", "st": "stone", "ton": "US tons", "ml": "millilitres",
          "l": "litres", "tsp": "teaspoons", "tbsp": "tablespoons", "floz": "US fluid ounces", "cup": "US cups",
          "pt": "US pints", "qt": "US quarts", "gal": "US gallons", "m/s": "metres per second",
          "km/h": "km/h", "mph": "mph", "kn": "knots", "ft/s": "feet per second", "c": "degrees Celsius",
          "f": "degrees Fahrenheit", "k": "kelvin"}


_ONE = {"ft": "foot", "in": "inch", "ft/s": "foot per second", "m/s": "metre per second", "c": "degree Celsius",
        "f": "degree Fahrenheit"}


def _name(unit: str, x: float) -> str:
    """The unit's name for *x* of it: "1 foot", "2 feet", "1 US cup"."""
    full = _NAMES[unit]
    if x != 1 or unit in ("km/h", "mph", "k", "st"):
        return full
    return _ONE.get(unit) or full.removesuffix("s")


def _unit(word: str, name: str) -> tuple[str, str]:
    w = _text(word, name, 40).lower().replace("degree ", "degrees ")
    if not w:
        raise ValueError(f"{name} is a unit, like km or pounds")
    for candidate in (w, w.removesuffix("s"), w.removesuffix("es")):
        unit = candidate if any(candidate in u for u in _UNITS.values()) else _ALIASES.get(candidate)
        if unit:
            kind = next(k for k, u in _UNITS.items() if unit in u)
            return unit, kind
    raise ValueError(f"{word!r} is not a unit this calculator knows")


def _to_kelvin(x: float, unit: str) -> float:
    return {"c": x + 273.15, "f": (x - 32) * 5 / 9 + 273.15, "k": x}[unit]


def _from_kelvin(x: float, unit: str) -> float:
    return {"c": x - 273.15, "f": (x - 273.15) * 9 / 5 + 32, "k": x}[unit]


# ---------------------------------------------------------------- the tools

class Calculator:
    def calculate(self, expression: str) -> str:
        """From calculate (rebot_tools.py:554), with a plain answer sentence."""
        text = _text(expression, "expression", MAX_EXPRESSION)
        if not text:
            raise ValueError("expression is the math to work out, like 15% of 80")
        value = evaluate(text)
        return f"{_LEADING_WORDS.sub('', text).rstrip(' ?.!=')} is {show(value)}."

    def tip(self, bill, percent=15, people=1) -> str:
        bill = _number(bill, "bill", 0, 1e9)
        percent = _number(percent, "percent", 0, 100)
        people = _number(people, "people", 1, 100, whole=True)
        tip = bill * percent / 100
        total = bill + tip
        line = f"A {percent:g}% tip on {bill:,.2f} is {tip:,.2f}, so the total is {total:,.2f}."
        if people > 1:
            line += f" Split between {people} people, each pays {total / people:,.2f}."
        return line

    def convert(self, value, from_unit: str, to_unit: str) -> str:
        x = _number(value, "value", -1e15, 1e15)
        a, kind_a = _unit(from_unit, "from_unit")
        b, kind_b = _unit(to_unit, "to_unit")
        if kind_a != kind_b:
            raise ValueError(f"{_NAMES[a]} are {kind_a} and {_NAMES[b]} are {kind_b}; they do not convert")
        if kind_a == "temperature":
            k = _to_kelvin(x, a)
            if k < 0:
                raise ValueError("that is colder than absolute zero")
            y = _from_kelvin(k, b)
        else:
            y = x * _UNITS[kind_a][a] / _UNITS[kind_a][b]
        y = float(f"{y:.6g}")          # six significant figures: 176.667, not 176.6666667
        return f"{show(x)} {_name(a, x)} is {show(y)} {_name(b, y)}."


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


TOOLS = [
    ("calculate", "Work out arithmetic exactly: + - * / // % ** and brackets, sqrt, abs, round, min, max, pi and e, "
     "and percentages like 15% of 80 or 80 + 15%. Answers in a plain sentence. Use it for any math.",
     {"expression": _p("string", "The math, in symbols or words, like 847 times 62 or 15% of 80. At most 200 characters.")},
     ["expression"]),
    ("tip", "Work out a tip on a bill and the total, and what each person pays when the bill is split.",
     {"bill": _p("number", "The bill before the tip."),
      "percent": _p("number", "The tip in percent. Default 15."),
      "people": _p("integer", "How many people split the bill. Default 1.")}, ["bill"]),
    ("convert", "Convert a number between units of length, weight, volume (US cups, pints, gallons), "
     "temperature or speed, like 5 miles to km or 350 F to C.",
     {"value": _p("number", "The number to convert."),
      "from_unit": _p("string", "The unit it is in, like miles, lb, cups, F, mph."),
      "to_unit": _p("string", "The unit wanted, like km, kg, ml, C, km/h.")}, ["value", "from_unit", "to_unit"]),
]
CHANGES: list[str] = []


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Calculator, msg: dict) -> dict | None:
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
        if not isinstance(args, dict):
            return ok({"content": [{"type": "text", "text": "arguments must be an object."}], "isError": True})
        try:
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"{name} could not do that: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Calculator does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Calculator/1"
    app: Calculator
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
        self._json(401, {"error": "Calculator needs its PuffPAI token."})
        return True

    def _guarded(self, route) -> None:
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Calculator stopped on this request with an error ({type(exc).__name__})."})
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
        self._json(404, {"error": "Calculator has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        if self._path() != "/mcp":
            return self._json(404, {"error": "Calculator has no page here."})
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


def make_server(port: int, data=None, token: str | None = None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. *data* is accepted for the common shape; nothing is stored.
    Call serve_forever(); stop with shutdown()."""
    handler = type("CalculatorHandler", (Handler,), {"app": Calculator(), "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Calculator for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    if not args.port:
        p.error("PuffPAI starts Calculator with PUFFPAI_PORT (or --port) set.")
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
