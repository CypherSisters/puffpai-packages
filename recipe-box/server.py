"""Recipe Box: the household's own recipes, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/recipe-box/
- ``/health``       "ok"
- ``/data``         every saved recipe (GET), ``/updated`` its stamp
- ``/view``         the recipes, with the tag words and today's date (GET)
- ``/export``       every recipe as one JSON file to download (GET)
- ``/export_text``  one recipe as plain text (GET, ``?id=``)
- ``/api/save``, ``/api/remove``, ``/api/made``, ``/api/set``, ``/api/scale``, ``/api/match``,
  ``/api/import``   the page's changes and sums (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN. The Desktop reaches the
page through the Core's /pkg/recipe-box/, which adds it.

The recipes are one JSON document in PUFFPAI_DATA (``recipes.json``). Writes replace the whole
file at once (temp file, then rename). A file that exists but will not read, an empty one
included, is never written over. Scaling, unit sums, "what can I make" and how text and
JSON-LD are read: PROCESS.md. Nothing here ever fetches a web page.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "recipe-box"
VERSION = "1.0.0"
DOC = "recipes.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 1024 * 1024
BODY_WAIT_S = 5.0                       # the whole wait for a request body
REFUSED_MOST, REFUSED_S = 64 * 1024, 2.0            # what a request without the token may send before its 401
DROP_MOST, DROP_S = 64 * 1024 * 1024, 20.0          # a body over the cap is read and dropped, then a plain 400
IDLE_S = 1.0                                        # dropping stops when nothing more arrives for this long
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
LIMITS = {"recipes": 2000, "ingredients": 100, "steps": 100, "made": 500, "tags": 20}

# ---------------------------------------------------------------- every constant the sums use
# Volume in milliliters, weight in grams: US customary measures (1 US cup = 236.5882365 ml).
VOLUME_ML = {"tsp": 4.92892159375, "tbsp": 14.78676478125, "fl oz": 29.5735295625, "cup": 236.5882365,
             "pint": 473.176473, "quart": 946.352946, "gallon": 3785.411784, "ml": 1.0, "l": 1000.0}
WEIGHT_G = {"oz": 28.349523125, "lb": 453.59237, "g": 1.0, "kg": 1000.0}
METRIC = ("ml", "l", "g", "kg")
# Metric amounts after scaling or converting: (below this amount, round to this step). 1 g under 100 g, then 5 g.
METRIC_STEPS = {"g": ((100, 1), (math.inf, 5)), "ml": ((100, 1), (math.inf, 5)),
                "kg": ((Fraction(1, 10), Fraction(1, 1000)), (math.inf, Fraction(5, 1000))),
                "l": ((Fraction(1, 10), Fraction(1, 1000)), (math.inf, Fraction(5, 1000)))}
# US measures and counts: amounts are exact fractions. A shown kitchen fraction is one of these denominators.
KITCHEN = (1, 2, 3, 4, 6, 8)            # wholes, halves, thirds, quarters, sixths, eighths
NEAR = Fraction(2, 100)                 # "about" a kitchen fraction only when it is within 2 percent
# The fractions each unit's own measures come in; a part that is not one goes down to the next unit.
MEASURES = {"cup": (1, 2, 3, 4), "tbsp": (1, 2), "tsp": (1, 2, 4, 8), "fl oz": (1, 2), "pint": (1, 2, 4),
            "quart": (1, 2, 4), "gallon": (1, 2, 4), "lb": (1, 2, 4), "oz": (1, 2, 4, 8)}
DOWN = {"cup": ("tbsp", 16), "tbsp": ("tsp", 3), "fl oz": ("tbsp", 2), "pint": ("cup", 2), "quart": ("cup", 4),
        "gallon": ("cup", 16), "lb": ("oz", 16)}
SPOONS = ("tsp", "tbsp")                # 1 1/8 tsp is said "1 tsp plus 1/8 tsp": two spoons
OVEN_STEP = 5                           # oven temperatures: "about" the nearest 5 degrees
FACTOR_RANGE = (Fraction(1, 20), Fraction(100))
NOT_READ, NOT_GIVEN = "not read", "not given"   # an imported servings or time that could not be read
UNIT_WORDS = {
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "tsps": "tsp", "tsp.": "tsp", "t": "tsp",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbsps": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "tbsp.": "tbsp", "T": "tbsp",
    "cup": "cup", "cups": "cup", "c": "cup", "c.": "cup",
    "fl oz": "fl oz", "fl. oz.": "fl oz", "fl.oz.": "fl oz", "floz": "fl oz", "fluid ounce": "fl oz",
    "fluid ounces": "fl oz", "fl oz.": "fl oz", "fl. oz": "fl oz",
    "pint": "pint", "pints": "pint", "pt": "pint", "quart": "quart", "quarts": "quart", "qt": "quart",
    "gallon": "gallon", "gallons": "gallon", "gal": "gallon",
    "ml": "ml", "mls": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l", "ltr": "l",
    "oz": "oz", "oz.": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "lb.": "lb", "lbs.": "lb", "pound": "lb", "pounds": "lb",
    "g": "g", "gr": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg",
}
OTHER_UNITS = ("clove", "can", "jar", "slice", "stick", "pinch", "dash", "handful", "bunch", "sprig", "piece",
               "package", "packet", "head", "leaf", "sheet", "bag", "bottle", "box", "drop", "knob", "stalk",
               "rasher", "fillet", "tin", "cube", "scoop")
OTHER_PLURALS = {"leaf": "leaves"}       # counted units whose plural is not just -s or -es
PLURAL_UNITS = {"cup": "cups", "pint": "pints", "quart": "quarts", "gallon": "gallons"}
# Foods counted with no unit ("3 eggs"): the item's last word follows the amount only for these. Any other item
# stays as the owner wrote it, so nothing like "flours" is ever made up.
COUNT_WORDS = {"egg": "eggs", "yolk": "yolks", "onion": "onions", "shallot": "shallots", "carrot": "carrots",
               "potato": "potatoes", "tomato": "tomatoes", "lemon": "lemons", "lime": "limes", "banana": "bananas",
               "apple": "apples"}
COUNT_ADJECTIVES = {"large", "small", "medium", "big", "ripe", "fresh", "whole", "free-range", "organic", "red",
                    "yellow", "white", "brown", "green", "sweet"}
# Any second amount anywhere after the leading amount and unit ("250 g (2 cups) flour", "1 or 2 eggs",
# "1 egg, or a couple if small") leaves the whole line unscaled: a digit, a fraction character, or one of these words.
AMOUNT_WORD_RE = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|half|halves|"
                            r"quarter|couple|few|dozen)\b", re.I)
# The one kept exception: a can or pack size right after the amount, "1 x 400g can tomatoes" (the 1 scales).
PACK_SIZE_RE = re.compile(r"^x\s*\d+(?:[.,]\d+)?\s*(?:g|kg|ml|l|oz|lb)\s+(?=\D*$)", re.I)
TEMP_WORDS = {"f": "F", "fahrenheit": "F", "c": "C", "celsius": "C", "centigrade": "C"}
TEXT_AMOUNTS = ("a pinch of", "pinch of", "a dash of", "dash of", "a handful of", "handful of", "a few",
                "a little", "some", "a splash of", "splash of", "a knob of")
FACTOR_WORDS = {"double": Fraction(2), "twice": Fraction(2), "triple": Fraction(3), "half": Fraction(1, 2),
                "halve": Fraction(1, 2), "quarter": Fraction(1, 4)}
SERVING_WORDS = {"people", "person", "persons", "servings", "serving", "portions", "portion", "times", "x"}

TAG_KINDS = ("meal", "cuisine", "diet", "season", "other")
TAG_WORDS = {
    "meal": {"breakfast", "brunch", "lunch", "dinner", "supper", "dessert", "snack", "side", "starter",
             "appetizer", "drink", "baking", "soup", "salad", "main", "bread", "sauce"},
    "cuisine": {"italian", "mexican", "chinese", "indian", "thai", "japanese", "french", "greek", "american",
                "mediterranean", "korean", "vietnamese", "spanish", "middle eastern", "cajun", "caribbean",
                "british", "german", "turkish", "moroccan", "lebanese", "ethiopian", "persian", "southern"},
    "diet": {"vegetarian", "vegan", "gluten-free", "dairy-free", "nut-free", "low-carb", "keto", "halal",
             "kosher", "pescatarian", "egg-free", "low-sugar", "low-salt"},
    "season": {"spring", "summer", "autumn", "fall", "winter", "holiday", "christmas", "thanksgiving",
               "easter", "passover", "ramadan", "diwali", "eid", "new year"},
}
DESCRIPTORS = {"fresh", "freshly", "chopped", "diced", "minced", "sliced", "grated", "shredded", "large", "small",
               "medium", "ground", "dried", "whole", "unsalted", "salted", "softened", "melted", "cold", "warm",
               "hot", "room", "temperature", "finely", "roughly", "thinly", "boneless", "skinless", "ripe", "extra",
               "virgin", "plain", "all", "purpose", "packed", "light", "dark", "raw", "cooked", "frozen",
               "canned", "organic", "of", "and", "or", "a", "an", "the", "for", "to", "taste", "optional", "about",
               "beaten", "peeled", "halved", "cubed", "crushed", "some", "few", "little", "pinch", "dash",
               "handful", "splash", "lightly", "good", "quality", "we", "have", "i", "got", "on", "hand", "my",
               "our", "in", "fridge", "pantry"}
STAPLES = [frozenset(s.split()) for s in ("water", "salt", "pepper", "black pepper", "salt pepper", "ice")]
TRUE_WORDS = {"true", "yes", "y", "1", "on", "favorite", "favourite", "star"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "not", "unstar", "none"}
FILLER = {"the", "a", "an", "recipe", "recipes", "our", "my", "for", "of", "please"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, lines: bool = False) -> str:
    """Words of at most *most* characters, trimmed, control characters dropped; '' when not given."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be words")
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(c for c in value if c == "\n" or (c >= " " and c != "\x7f"))
    if lines:
        value = "\n".join(" ".join(line.split()) for line in value.split("\n")).strip("\n")
        while "\n\n\n" in value:
            value = value.replace("\n\n\n", "\n\n")
    else:
        value = " ".join(value.split())
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value


def _flag(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in TRUE_WORDS | FALSE_WORDS:
        return value.strip().lower() in TRUE_WORDS
    raise ValueError(f"{name} is yes or no")


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _int(text: str) -> int:
    if len(text.lstrip("-")) > 15:
        raise ValueError("that number is too big for this app")
    return int(text)


def _float(text: str) -> float:
    x = float(text) if len(text) <= 40 else math.inf
    if not math.isfinite(x) or abs(x) > 1e15:
        raise ValueError("that number is too big for this app")
    return x


def _loads(text: str):
    """JSON, refusing huge numbers, NaN and Infinity, and nesting too deep to read, as plain ValueErrors."""
    try:
        return json.loads(text, parse_constant=_no_constant, parse_int=_int, parse_float=_float)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None


def _number(text) -> float | None:
    """'2', '1.5', '1,5', '3/4', '1 1/2', '1½' or '½' as a number; None when it is not one."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(text) else None
    s = str(text or "").strip().replace("⁄", "/")
    if not s or len(s) > 20:
        return None
    total, rest = 0.0, ""
    for c in s:
        if unicodedata.category(c) == "No":
            try:
                total += unicodedata.numeric(c)
            except (TypeError, ValueError):
                return None
        else:
            rest += c
    rest = rest.strip()
    m = re.fullmatch(r"(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)", rest)
    if m:
        whole, top, bottom = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return None if bottom == 0 else total + whole + top / bottom
    if re.fullmatch(r"\d+,\d{1,2}", rest):
        rest = rest.replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", rest):
        return total + float(rest)
    return total if not rest and total else None


_STRICT = r"(?:[0-9]{1,9}\s+)?[0-9]{1,9}\s*/\s*[0-9]{1,9}|[0-9]{1,9}(?:\.[0-9]{1,9})?|\.[0-9]{1,9}"


def _strict(value) -> Fraction | None:
    """An exact number from digits with at most one decimal point, or a simple fraction ('1/2', '1 1/2'), or a
    finite JSON number. Anything else ('1e400', '1,5', '2x') is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Fraction)):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(repr(value)) if math.isfinite(value) else None
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(_STRICT, value.strip()):
        return None
    s = value.strip()
    m = re.fullmatch(r"(?:([0-9]+)\s+)?([0-9]+)\s*/\s*([0-9]+)", s)
    if not m:
        return Fraction(s)
    whole, top, bottom = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return None if bottom == 0 else whole + Fraction(top, bottom)


def _num_arg(value, name: str, lo, hi) -> Fraction | None:
    """A number from a tool or the page ('6', 6, '1 1/2', '8 people'), read strictly; None when not given."""
    if value is None or value == "":
        return None
    x = _strict(value)
    if x is None and isinstance(value, str) and len(value) <= 40:   # '8 people', '6 servings'
        m = re.fullmatch(r"\s*(\S+)\s+([a-z]+)\s*", value.lower())
        x = _strict(m.group(1)) if m and m.group(2) in SERVING_WORDS else None
    if x is None:
        raise ValueError(f"{name} is a number like 6, 1.5 or 1 1/2")
    if not lo <= x <= hi:
        raise ValueError(f"{name} is between {float(lo):g} and {float(hi):g}")
    return x


def _minutes(value, name: str) -> int:
    """Minutes from 45, '45', '45 min', '1 hour 10 minutes', '1h30' or an ISO duration like PT1H30M; 0 when not given."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{name} is a number of minutes")
    if isinstance(value, (int, float)):
        n = value
    else:
        s = str(value).strip().lower()
        if len(s) > 40:                                    # a time field: capped before any pattern
            raise ValueError(f"{name} is a number of minutes, like 45 or 1 hour 10 minutes")
        iso = _iso_minutes(s.upper())
        if iso is not None:
            n = iso
        elif _number(s) is not None:
            n = _number(s)
        else:
            m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*(?:and\s*)?"
                             r"(?:(\d+)\s*(?:m|min|mins|minute|minutes)?)?", s)
            if not m or not (m.group(1) or m.group(2)):
                raise ValueError(f"{name} is a number of minutes, like 45 or 1 hour 10 minutes")
            n = float(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if not (isinstance(n, (int, float)) and math.isfinite(n) and 0 <= n <= 10000):
        raise ValueError(f"{name} is between 0 and 10000 minutes")
    return int(round(n))


def _iso_minutes(s: str) -> int | None:
    if len(s) > 40:
        return None
    m = re.fullmatch(r"P(?:(\d{1,4})D)?(?:T(?:(\d{1,4})H)?(?:(\d{1,5})M)?(?:(\d{1,6}(?:\.\d+)?)S)?)?", s.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec = (float(g or 0) for g in m.groups())
    total = d * 1440 + h * 60 + mi + sec / 60
    return int(round(total)) if total <= 10000 else None


def _day(value, name: str, today: date) -> date | None:
    text = _text(value, name, 40).lower()
    if not text:
        return None
    if text in ("today", "tonight", "yesterday", "last night"):
        return today - timedelta(days=0 if text in ("today", "tonight") else 1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or today or yesterday") from None


def _rating(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("rating is 1 to 5 stars, or 0 for none")
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(value)[:40])
    if not m or not 0 <= float(m.group(1)) <= 5:
        raise ValueError("rating is 1 to 5 stars, or 0 for none")
    return int(round(float(m.group(1))))


# ---------------------------------------------------------------- units and amounts

def unit_of(word) -> str:
    """The canonical unit for *word* ('Tbsp', 'cups', 'fl. oz.'), or '' when it is not a measure we convert."""
    w = " ".join(str(word or "").split())
    if w in ("T", "t"):
        return UNIT_WORDS[w]
    w = w.lower()
    return UNIT_WORDS.get(w, UNIT_WORDS.get(w.rstrip("."), ""))


def unit_kind(unit: str) -> str:
    u = unit_of(unit)
    return "volume" if u in VOLUME_ML else "weight" if u in WEIGHT_G else ""


def _strip0(x, places: int = 2) -> str:
    s = f"{float(x):.{places}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _exact(x) -> Fraction:
    """An amount as an exact fraction. A stored amount keeps 4 decimals, so 0.3333 reads back as 1/3."""
    if isinstance(x, Fraction):
        return x
    d = Fraction(repr(float(x))) if isinstance(x, float) else Fraction(x)
    near = d.limit_denominator(24)
    return near if abs(near - d) <= Fraction(1, 10000) else d


def _dec(x: Fraction) -> str:
    """The exact amount as a decimal to 3 significant figures (at most half a percent off)."""
    v = float(x)
    return "0" if v <= 0 else _strip0(v, max(0, 2 - math.floor(math.log10(v))))


def _frac_text(x: Fraction) -> str:
    whole, part = divmod(x.numerator, x.denominator)
    if not part:
        return str(whole)
    return f"{whole} {part}/{x.denominator}" if whole else f"{part}/{x.denominator}"


def _part(x: Fraction, u: str) -> str:
    """One measure: '1 1/2 cups', '2 tbsp', '3 eggs'; '1 tsp plus 1/8 tsp' for two spoons."""
    whole, part = divmod(x.numerator, x.denominator)
    if u in SPOONS and whole and part and x.denominator in (6, 8):
        rest = Fraction(part, x.denominator)
        return f"{whole} {unit_word(u, whole)} plus {_frac_text(rest)} {unit_word(u, rest)}"
    return " ".join(t for t in (_frac_text(x), unit_word(u, x)) if t)


def _split(x: Fraction, u: str, depth: int = 0) -> list | None:
    """*x* of *u* as exact kitchen measures [(amount, unit), ...], going down to smaller units for a part that
    is not one of this unit's measures (1/6 cup is 2 tbsp plus 2 tsp). None when nothing exact and clean exists."""
    whole = x.numerator // x.denominator
    part = x - whole
    if part.denominator in MEASURES.get(u, KITCHEN):
        return [(x, u)]
    if u in DOWN and depth < 3:
        small, k = DOWN[u]
        rest = _split(part * k, small, depth + 1)
        if rest is not None:
            return ([(Fraction(whole), u)] if whole else []) + rest
    return [(x, u)] if x.denominator in KITCHEN else None


def _nearest(x: Fraction, dens=KITCHEN) -> Fraction:
    """The nearest whole, half, third, quarter, sixth or eighth (the plainer one on a tie)."""
    return min((Fraction(round(x * d), d) for d in dens), key=lambda c: (abs(c - x), c.denominator))


def _say_metric(x: Fraction, u: str) -> str:
    """Grams and milliliters to the nearest 1 under 100 and 5 above (kilograms and liters the same in grams and
    milliliters); "about" with the exact figure when that moves it by more than 2 percent."""
    step = next(s for below, s in METRIC_STEPS[u] if x < below)
    shown = math.floor(x / step + Fraction(1, 2)) * step
    if shown == 0:
        shown = Fraction(_dec(x))
    if x and abs(shown - x) > NEAR * x:
        return f"about {_strip0(shown, 3)} {u} ({_dec(x)} {u})"
    return f"{_strip0(shown, 3)} {u}"


def say_amount(x, unit: str) -> str:
    """An exact amount with its unit, never silently rounded. Exact kitchen measures when they exist (in this
    unit, or going down to smaller ones); otherwise "about" the nearest kitchen measure with the exact decimal."""
    u = unit_of(unit) or unit
    x = _exact(x)
    if u in METRIC:
        return _say_metric(x, u)
    if x <= 0:
        return _part(Fraction(0), u)
    parts = _split(x, u)
    if parts is None:
        near = _nearest(x)
        if not (near and abs(near - x) <= NEAR * x):             # go to the smallest unit for the nearest measure
            k, small = 1, u
            while small in DOWN:
                small, step = DOWN[small]
                k *= step
            near = _nearest(x * k, MEASURES.get(small, KITCHEN)) / k
        if not near:
            return f"a very little (about {_dec(x)} {unit_word(u, x)})".replace(" )", ")")
        parts = _split(near, u) or [(near, u)]
        return f"about {' plus '.join(_part(a, b) for a, b in parts)} ({_dec(x)} {unit_word(u, x)})".replace(" )", ")")
    return " plus ".join(_part(a, b) for a, b in parts)


def fmt_plain(x, unit: str) -> str:
    """A stored amount as it was given: a fraction when it is one, otherwise its number."""
    if unit_of(unit) in METRIC:
        return _strip0(x, 4)
    e = _exact(x)
    return _frac_text(e) if e.denominator in KITCHEN else _strip0(x, 4)


def unit_word(unit: str, amount) -> str:
    u = unit_of(unit)
    if u in PLURAL_UNITS and amount is not None and amount > 1:
        return PLURAL_UNITS[u]
    if not u and unit in OTHER_UNITS and amount is not None and amount > 1:
        return OTHER_PLURALS.get(unit) or unit + ("es" if unit.endswith(("ch", "sh", "x")) else "s")
    return u or unit


_COUNT_ANY = {**{w: w for w in COUNT_WORDS}, **{p: w for w, p in COUNT_WORDS.items()}}


def count_item(item: str, note: str, amount) -> str:
    """A scaled item agrees with its amount only in one safe shape: plain adjectives from COUNT_ADJECTIVES, then one
    counted food word, and nothing else ('large eggs'; a note after a comma only without a digit or "or").
    Plural above 1 and at 0 ('1 1/2 eggs', '0 eggs'), singular otherwise ('1 egg', '1/2 egg').
    Any other shape ('dozen eggs', 'large or 2 small onions', 'bowl of eggs') stays as written."""
    one = _count_safe(item, note)
    if amount is None or not one:
        return item
    words = item.split(" ")
    word = one if _singular(amount) else COUNT_WORDS[one]
    return " ".join(words[:-1] + [word.capitalize() if words[-1][0].isupper() else word])


def _singular(amount) -> bool:
    return 0 < amount <= 1


def _count_safe(item: str, note: str) -> str:
    """The counted food (singular) when the item is in the one safe shape for count_item; '' otherwise."""
    if any(c.isnumeric() for c in item + note) or re.search(r"\bor\b", note, re.I):
        return ""
    words = item.split(" ")
    last = words[-1]
    one = _COUNT_ANY.get(last.lower())
    if (not one or last not in (last.lower(), last.lower().capitalize())
            or any(w.lower() not in COUNT_ADJECTIVES for w in words[:-1])):
        return ""
    return one


def scalable(ing: dict, factor) -> bool:
    """Whether a line's amount is scaled. Not when it has no number; not when any other amount appears anywhere
    after the leading amount and unit (a digit, a fraction, or a word like two, half or dozen), except a pack size
    like "x 400g can"; and, with no unit and not in the one safe shape count_item changes, not when scaling moves
    the amount across 1 (1 or less is singular): '2 egg whites' halved would read '1 egg whites'."""
    if not isinstance(ing["qty"], (int, float)) or factor == 1:
        return False
    rest = PACK_SIZE_RE.sub("", ing["item"], count=1) if ing["item"][:1] in "xX" else ing["item"]
    rest += " " + ing["note"]
    if any(c.isnumeric() for c in rest) or AMOUNT_WORD_RE.search(rest):
        return False
    if not ing["unit"] and not _count_safe(ing["item"], ing["note"]):
        old = _exact(ing["qty"])            # the words were written for the old amount: never cross 1 with them
        if _singular(old) != _singular(old * factor):
            return False
    return True


def _most(q) -> float | None:
    """The amount that decides singular or plural: the number, or a range's larger end ('2-3' is 3)."""
    if isinstance(q, (int, float)):
        return float(q)
    nums = [_number(p) for p in str(q).split("-")] if str(q)[:1].isdigit() else [None]
    return None if None in nums else max(nums)


def convert(amount: float, frm: str, to: str) -> float:
    """*amount* of *frm* in *to*: volume to volume, weight to weight. Never volume to weight."""
    a, b = unit_of(frm), unit_of(to)
    if not a:
        raise ValueError(f"{frm!r} is not a unit this app converts; it knows cups, tablespoons, teaspoons, fluid "
                         "ounces, milliliters, liters, ounces, pounds, grams and kilograms")
    if not b:
        raise ValueError(f"{to!r} is not a unit this app converts; it knows cups, tablespoons, teaspoons, fluid "
                         "ounces, milliliters, liters, ounces, pounds, grams and kilograms")
    if unit_kind(a) != unit_kind(b):
        raise ValueError(f"{a} is a {unit_kind(a)} and {b} is a {unit_kind(b)}. Volume can not be turned into "
                         "weight without knowing the ingredient: a cup of flour and a cup of sugar weigh different "
                         "amounts. Weigh it, or use the weight the recipe gives")
    table = VOLUME_ML if a in VOLUME_ML else WEIGHT_G
    return _exact(amount) * Fraction(repr(table[a])) / Fraction(repr(table[b]))


def oven(amount: float, frm: str) -> tuple[float, str]:
    """An oven temperature in the other scale: (degrees, 'C' or 'F')."""
    if frm == "F":
        return (amount - 32) * 5 / 9, "C"
    return amount * 9 / 5 + 32, "F"


def _temp_unit(word) -> str:
    """'F' or 'C' for 'F', 'degrees Fahrenheit', '°C', 'celsius' and the like; '' otherwise."""
    w = str(word or "").lower().replace("°", " ")
    w = " ".join(x for x in re.split(r"[^a-z]+", w) if x and x not in ("degrees", "degree", "deg"))
    return TEMP_WORDS.get(w, "")


# ---------------------------------------------------------------- ingredient lines

_QTY = r"(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:[.,]\d+)?(?:\s*[¼-¾⅐-⅞])?|[¼-¾⅐-⅞])"
QTY_RE = re.compile(rf"\s*({_QTY})(\s*(?:-|–|to)\s*{_QTY})?\s*")


def _other_unit(word: str) -> str:
    """'cloves' -> 'clove', 'pinches' -> 'pinch' for units that are counted, not converted; '' otherwise."""
    w = word.lower().rstrip(".")
    for cand in (w, w[:-1], w[:-2]):
        if cand in OTHER_UNITS and (cand == w or w in (cand + "s", cand + "es")):
            return cand
    return ""


def parse_line(line: str) -> dict:
    """One ingredient line ('1 1/2 cups flour, sifted') as {qty, unit, item, note}."""
    s = _text(line, "ingredient", 300)
    s = re.sub(r"^[-*•●▪]+\s*", "", s)
    qty, unit, rest = "", "", s
    m = QTY_RE.match(s)
    if m and m.group(1):
        rest = s[m.end():]
        if m.group(2):
            qty = (m.group(1).strip() + "-" + re.sub(r"^\s*(?:-|–|to)\s*", "", m.group(2))).strip()
        else:
            qty = _number(m.group(1))
            if qty is None:
                qty, rest = "", s
    else:
        low = s.lower()
        for phrase in TEXT_AMOUNTS:
            if low.startswith(phrase + " "):
                qty, rest = s[:len(phrase)], s[len(phrase) + 1:]
                break
    if (isinstance(qty, float) or (qty and qty[0].isdigit())) and rest:
        words = rest.split()
        for n in (3, 2, 1):
            head = " ".join(words[:n])
            if len(words) > n and unit_of(head):
                unit, rest = unit_of(head), " ".join(words[n:])
                break
            if n == 1 and len(words) > 1 and _other_unit(words[0]):
                unit, rest = _other_unit(words[0]), " ".join(words[1:])
        if rest.lower().startswith("of "):
            rest = rest[3:]
    item, _, note = rest.partition(",")
    return {"qty": qty if isinstance(qty, str) else round(qty, 4), "unit": unit, "item": item.strip() or s,
            "note": note.strip()}


def fmt_ingredient(ing: dict, amount: float | None = None) -> str:
    """One ingredient as a line; with *amount*, that scaled amount instead of the stored one."""
    q = ing["qty"]
    item = ing["item"]
    if amount is not None:                   # scaled: a counted food may follow the new amount
        head = say_amount(amount, ing["unit"])
        if not ing["unit"]:
            item = count_item(item, ing["note"], amount)
    else:                                    # as stored: the words exactly as written
        said = fmt_plain(float(q), ing["unit"]) if isinstance(q, (int, float)) else q
        head = " ".join(x for x in (said, unit_word(ing["unit"], _most(q))) if x)
    return " ".join(x for x in (head, item) if x) + (f", {ing['note']}" if ing["note"] else "")


def _split_lines(value, name: str, ingredients: bool) -> list:
    """A list from a list, or from text: one per line; one line splits on ';', and ingredients on a comma
    that is followed by an amount ('2 cups flour, 2 eggs')."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    text = _text(value, name, 60000, True)
    parts = [p for p in text.split("\n") if p.strip()]
    if len(parts) == 1:
        one = parts[0]
        if ";" in one:
            parts = one.split(";")
        elif ingredients:
            parts = re.split(rf",\s*(?=(?:{_QTY}|a |an )\s*)", one)
        else:
            numbered = re.split(r"(?:^|\s)\d{1,2}[.)]\s+", one)
            parts = [p for p in numbered if p.strip()] if len(numbered) > 2 else [one]
    return [re.sub(r"^\s*(?:step\s*\d+(?:\s*[:.)-])?|\d{1,2}[.)]|[-*•])\s*", "", p, flags=re.I).strip()
            if not ingredients else p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------- the recipe's shape

def _tags(value) -> dict:
    """Tags as {meal, cuisine, diet, season, other}: from that object, a list, or 'dinner, italian, diet: vegan'.
    A word that names no group is kept under other."""
    out = {k: [] for k in TAG_KINDS}
    if value is None or value == "":
        return out
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            words = v if isinstance(v, list) else [v] if v not in (None, "") else []
            kind = _text(k, "tag kind", 20).lower()
            pairs += [(kind, w) for w in words]
    elif isinstance(value, list):
        pairs = [("", w) for w in value]
    else:
        pairs = [("", w) for w in re.split(r"[,;\n#]", _text(value, "tags", 2000, True))]
    for kind, word in pairs:
        w = _text(word, "tag", 60).lower().strip(" .")
        if not w:
            continue
        if ":" in w and not kind:
            kind, w = (x.strip() for x in w.split(":", 1))
        kind = {"course": "meal", "category": "meal", "dish": "meal", "type": "meal", "seasons": "season",
                "diets": "diet", "cuisines": "cuisine", "meals": "meal"}.get(kind, kind)
        if kind not in TAG_KINDS:
            kind = next((k for k, words in TAG_WORDS.items() if w in words or w.replace(" ", "-") in words),
                        "other")
        if w not in out[kind] and sum(len(v) for v in out.values()) < LIMITS["tags"]:
            out[kind].append(w)
    return out


def clean_ingredient(raw) -> dict:
    if isinstance(raw, str):
        return parse_line(raw)
    if not isinstance(raw, dict):
        raise ValueError("each ingredient is a line of words or an object with qty, unit, item and note")
    q = raw.get("qty", raw.get("quantity", raw.get("amount")))
    if isinstance(q, bool):
        raise ValueError("an ingredient's quantity is a number or words")
    if isinstance(q, (int, float)):
        if not (math.isfinite(q) and 0 <= q <= 100000):
            raise ValueError("an ingredient's quantity is between 0 and 100000")
        qty = round(float(q), 4)
    else:
        text = _text(q, "quantity", 40)
        n = _number(text)
        qty = round(n, 4) if n is not None and n <= 100000 else text
    item = _text(raw.get("item", raw.get("name")), "ingredient", 200)
    if not item:
        raise ValueError("each ingredient needs an item, like flour")
    unit_raw = _text(raw.get("unit"), "unit", 30)
    return {"qty": qty, "unit": unit_of(unit_raw) or unit_raw.lower(), "item": item,
            "note": _text(raw.get("note"), "ingredient note", 200)}


def clean_recipe(raw, at: int = 0) -> dict:
    """One recipe, every field checked and capped. A recipe with no id gets one made from its title."""
    if not isinstance(raw, dict):
        raise ValueError("a recipe is an object")
    title = _text(raw.get("title"), "title", 120)
    if not title:
        raise ValueError("each recipe needs a title")
    rid = raw.get("id")
    if not (isinstance(rid, str) and ID_RE.fullmatch(rid)):
        rid = "r" + hashlib.sha256(f"{at}\n{title}".encode("utf-8")).hexdigest()[:8]
    ings = raw.get("ingredients") or []
    steps = raw.get("steps") or []
    if not isinstance(ings, list) or not isinstance(steps, list):
        raise ValueError("ingredients and steps are lists")
    if len(ings) > LIMITS["ingredients"] or len(steps) > LIMITS["steps"]:
        raise ValueError(f"a recipe holds at most {LIMITS['ingredients']} ingredients and {LIMITS['steps']} steps")
    made = raw.get("made") or []
    if not isinstance(made, list) or len(made) > LIMITS["made"]:
        raise ValueError(f"made is a list of at most {LIMITS['made']} dates")
    days = sorted({d.isoformat() for d in (_as_date(x) for x in made) if d})
    fav = raw.get("favorite", False)
    return {"id": rid, "title": title, "source": _text(raw.get("source"), "source", 300),
            "servings": NOT_READ if raw.get("servings") == NOT_READ else _servings(raw.get("servings")),
            "prep_min": NOT_GIVEN if raw.get("prep_min") == NOT_GIVEN else _minutes(raw.get("prep_min"), "prep minutes"),
            "cook_min": NOT_GIVEN if raw.get("cook_min") == NOT_GIVEN else _minutes(raw.get("cook_min"), "cook minutes"),
            "ingredients": [clean_ingredient(x) for x in ings],
            "steps": [s for s in (_text(x, "step", 2000, True) for x in steps) if s],
            "tags": _tags(raw.get("tags")), "notes": _text(raw.get("notes"), "notes", 4000, True),
            "rating": _rating(raw.get("rating")), "favorite": _flag(fav, "favorite") if fav not in (None, "") else False,
            "made": days, "added": _text(raw.get("added"), "added", 40)}


def _servings(value) -> float:
    """Servings as stored: a number up to 1000, 0 when not given."""
    x = _num_arg(value, "servings", 0, 1000)
    return round(float(x), 4) if x else 0


def _serves(r: dict) -> Fraction:
    """A recipe's servings as an exact number; 0 when not given or not read."""
    return _exact(r["servings"]) if isinstance(r["servings"], (int, float)) else Fraction(0)


def _yield(value) -> float | str:
    """Servings from an imported field ('4', '4 servings', 'Serves 4'), read strictly; 'not read' otherwise."""
    if isinstance(value, str):
        value = re.sub(r"^\s*(?:serves|servings|makes|yields?)(?:\s*:)?\s*", "", value[:200], flags=re.I)
    try:
        x = _num_arg(value, "servings", 0, 1000)
    except ValueError:
        return NOT_READ
    return round(float(x), 4) if x else 0


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def default_doc() -> dict:
    return {"app": NAME, "format": 1, "recipes": [], "last_updated": ""}


FILE_KEYS = {"app", "format", "recipes", "last_updated"}


def is_ours(raw) -> bool:
    """True for a file this app wrote: its marker, format 1, a list of recipes and no other top-level keys."""
    return (isinstance(raw, dict) and raw.get("app") == NAME and raw.get("format") == 1
            and isinstance(raw.get("recipes"), list) and set(raw) <= FILE_KEYS)


def normalize(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("the recipe file is a JSON object")
    recipes = raw.get("recipes", [])
    if not isinstance(recipes, list) or len(recipes) > LIMITS["recipes"]:
        raise ValueError(f"recipes is a list of at most {LIMITS['recipes']}")
    doc = default_doc()
    doc["recipes"] = [clean_recipe(r, i) for i, r in enumerate(recipes)]
    doc["last_updated"] = _text(raw.get("last_updated"), "last_updated", 40)
    return doc


# ---------------------------------------------------------------- reading pasted recipes

HEADINGS = {"ingredients": "ingredients", "ingredient": "ingredients", "you will need": "ingredients",
            "steps": "steps", "method": "steps", "directions": "steps", "instructions": "steps",
            "preparation": "steps", "notes": "notes", "note": "notes", "tips": "notes"}


def _trim_marks(s: str) -> str:
    """A line without the heading marks around it: '# Ingredients:' -> 'Ingredients'."""
    i, j = 0, len(s)
    while i < j and (s[i] in "#*=" or s[i].isspace()):
        i += 1
    while j > i and (s[j - 1] in ":#*=" or s[j - 1].isspace()):
        j -= 1
    return s[i:j]


def parse_text(text: str) -> dict:
    """A recipe pasted as plain text: a title line, an Ingredients block and a Steps, Method, Directions or
    Instructions block. Lines before Ingredients may say serves, prep, cook, source or tags."""
    lines = [ln.strip() for ln in _text(text, "text", 200000, True).split("\n")]
    title, block = "", "head"
    out: dict = {"ingredients": [], "steps": [], "notes": []}
    for ln in lines:
        if not ln:
            continue
        key = _trim_marks(ln[:2000]).lower()
        if key in HEADINGS:
            block = HEADINGS[key]
            continue
        if block == "head":
            low = ln[:2000].lower()
            m = re.match(r"(serves|servings|makes|yield|yields)", low)
            if m:
                rest = low[m.end():].strip()
                out["servings"] = _yield(rest[1:].strip() if rest.startswith(":") else rest)
            elif re.match(r"(prep|preparation)( time)?\s*:", low):
                out["prep_min"] = _imported_minutes(ln.split(":", 1)[1])
            elif re.match(r"(cook|cooking|bake|baking)( time)?\s*:", low):
                out["cook_min"] = _imported_minutes(ln.split(":", 1)[1])
            elif re.match(r"(source|from|by)\s*:", low):
                out["source"] = ln.split(":", 1)[1].strip()
            elif re.match(r"tags?\s*:", low):
                out["tags"] = ln.split(":", 1)[1]
            elif not title:
                title = re.sub(r"^(title|recipe)\s*:\s*", "", re.sub(r"^#+\s*", "", ln), flags=re.I)
            else:
                out["notes"].append(ln)
        else:
            out[block].append(ln)
    if not title:
        raise ValueError("pasted text needs a title on its first line")
    if not out["ingredients"] or not out["steps"]:
        raise ValueError("pasted text needs a line that says Ingredients, the ingredients one per line, then a "
                         "line that says Steps (or Method, Directions or Instructions) and the steps")
    out["steps"] = _split_lines("\n".join(out["steps"]), "steps", False)
    out["title"] = title
    out["notes"] = "\n".join(out["notes"])
    return out


def _imported_minutes(value) -> int | str:
    """Minutes from an imported field; 'not given' when it does not read, never 0 minutes."""
    try:
        return _minutes(value, "minutes")
    except ValueError:
        return NOT_GIVEN


def _ld_minutes(value) -> int | str:
    """An ISO duration (PT1H30M) from JSON-LD: 0 when absent, 'not given' when it does not read."""
    if value is None or value == "":
        return 0
    n = _iso_minutes(value.upper()) if isinstance(value, str) else None
    return NOT_GIVEN if n is None else n


def _plain(value, most: int) -> str:
    """Words from a web page's JSON-LD: tags dropped, entities read, trimmed."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    s = re.sub(r"<[^>]{0,500}>", " ", str(value))
    return re.sub(r"\s+([.,;:!?])", r"\1", " ".join(html.unescape(s).split()))[:most]


def _ld_steps(value, depth: int = 0) -> list[str]:
    if depth > 6 or value is None:
        return []
    if isinstance(value, str):
        return [p for p in (_plain(x, 2000) for x in re.split(r"\n+|<br\s*/?>|</p>", value)) if p]
    if isinstance(value, list):
        return [s for v in value[:200] for s in _ld_steps(v, depth + 1)]
    if isinstance(value, dict):
        if value.get("itemListElement") is not None:
            return _ld_steps(value.get("itemListElement"), depth + 1)
        return [p for p in [_plain(value.get("text") or value.get("name"), 2000)] if p]
    return []


def _ld_recipes(data, depth: int = 0) -> list[dict]:
    """Every schema.org Recipe object in pasted JSON-LD, wherever it sits (a list, @graph, mainEntity)."""
    if depth > 6:
        return []
    if isinstance(data, list):
        return [r for x in data[:200] for r in _ld_recipes(x, depth + 1)]
    if not isinstance(data, dict):
        return []
    kind = data.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(isinstance(k, str) and k.rsplit("/", 1)[-1] == "Recipe" for k in kinds):
        return [data]
    found = []
    for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
        if key in data:
            found += _ld_recipes(data[key], depth + 1)
    return found


def from_ld(r: dict) -> dict:
    """A schema.org Recipe as this app's recipe, read defensively: whatever is missing or odd is left out."""
    def first(v):
        return v[0] if isinstance(v, list) and v else v

    def words(v):
        v = v if isinstance(v, list) else re.split(r",", v) if isinstance(v, str) else []
        return [_plain(x, 60) for x in v[:20] if _plain(x, 60)]
    servings: float | str = 0
    for y in (r.get("recipeYield") if isinstance(r.get("recipeYield"), list) else [r.get("recipeYield")])[:5]:
        if y is None or y == "":
            continue
        got = _yield(y) if isinstance(y, (str, int, float)) and not isinstance(y, bool) else NOT_READ
        servings = got if got != 0 else servings
        if got not in (0, NOT_READ):
            break
    author = first(r.get("author"))
    author = author.get("name") if isinstance(author, dict) else author
    ings = r.get("recipeIngredient") or r.get("ingredients") or []
    ings = [ings] if isinstance(ings, str) else ings if isinstance(ings, list) else []
    keywords = words(r.get("keywords"))
    tags = {"meal": words(r.get("recipeCategory")), "cuisine": words(r.get("recipeCuisine")),
            "diet": [_plain(str(d).rsplit("/", 1)[-1].replace("Diet", ""), 60).lower()
                     for d in (r.get("suitableForDiet") if isinstance(r.get("suitableForDiet"), list)
                               else [r.get("suitableForDiet")])[:5] if isinstance(d, str)],
            "other": keywords[:8]}
    return {"title": _plain(r.get("name") or r.get("headline"), 120) or "Untitled recipe",
            "source": _plain(author if isinstance(author, str) else "", 150) or _plain(r.get("url"), 300),
            "servings": servings,
            "prep_min": _ld_minutes(r.get("prepTime")),
            "cook_min": _ld_minutes(r.get("cookTime")),
            "ingredients": [p for p in (_plain(x, 300) for x in ings[:LIMITS["ingredients"]]) if p],
            "steps": _ld_steps(r.get("recipeInstructions"))[:LIMITS["steps"]],
            "tags": tags, "notes": _plain(r.get("description"), 4000)}


def _ld_blobs(s: str) -> list[str]:
    """What sits inside each <script ... application/ld+json ...> ... </script> in *s*, at most 20."""
    low, out, at = s.lower(), [], 0
    while len(out) < 20:
        start = low.find("<script", at)
        if start < 0:
            break
        close = low.find(">", start, start + 410)
        if close < 0:
            at = start + 7
            continue
        end = low.find("</script>", close)
        if end < 0:
            break                              # no closing tag anywhere after: nothing more to find
        if "application/ld+json" in low[start:close]:
            out.append(s[close + 1:end])
        at = end + 9
    return out


def read_pasted(text: str) -> list[dict]:
    """Pasted words as raw recipes: this app's JSON, JSON-LD (bare, or inside a script tag), or plain text."""
    s = _text(text, "text", MAX_BODY, True)
    if not s:
        raise ValueError("nothing was pasted")
    blobs = []
    if s.lstrip().startswith(("{", "[")):
        blobs = [s]
    else:
        blobs = _ld_blobs(s)
    if blobs:
        found = []
        for b in blobs[:20]:
            data = _loads(b)
            if isinstance(data, dict) and isinstance(data.get("recipes"), list) and "@type" not in data:
                found += [x for x in data["recipes"] if isinstance(x, dict)]
            else:
                found += [from_ld(r) for r in _ld_recipes(data)]
        if not found:
            raise ValueError("that JSON holds no recipe: it is neither this app's export nor a schema.org Recipe")
        return found
    return [parse_text(s)]


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved recipes exist but will not read. Nothing writes over them."""


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return default_doc()
            try:
                raw = self.path.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("the file is empty")
                data = _loads(raw)
                if not is_ours(data):
                    raise Unreadable(f"The Recipe Box data file ({DOC}) is not a Recipe Box file: it does not carry "
                                     f"the recipe-box marker and format 1 with its list of recipes. Recipe Box will not "
                                     f"write over it. Move that file somewhere else to start again, or put a good copy "
                                     f"back.")
                return normalize(data)
            except (OSError, ValueError) as exc:     # an OSError's own text holds the full path: never shown
                why = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)
                raise Unreadable(f"The Recipe Box data file ({DOC}) will not read ({why}). Recipe Box will not write "
                                 f"over it. Move that file somewhere else to start again, or put a good copy "
                                 f"back.") from None

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".recipes-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

_USED = threading.local()               # a recipe found by part of its title, said first in the answer


def _fold(text) -> str:
    """Accents folded, case folded: 'Crème Brûlée' -> 'creme brulee'. Letters in any script stay letters."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


_WORD_RE = re.compile(r"[^\W_]+")       # runs of characters for which str.isalnum() is true


def _words(text) -> list[str]:
    return _WORD_RE.findall(_fold(text))


def _no_the(title: str) -> str:
    t = " ".join(_fold(title).split())
    return t[4:] if t.startswith("the ") else t


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("oes"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _core(text) -> frozenset:
    """The words that name a food ('2 large ripe tomatoes' -> {tomato})."""
    return frozenset(_stem(w) for w in _words(text) if w not in DESCRIPTORS and not w.isdigit())


def _matches(have: frozenset, ing: frozenset) -> bool:
    return bool(have) and bool(ing) and (have <= ing or ing <= have)


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _mins(m) -> str:
    return f"{m} min" if isinstance(m, int) else "not given"


def _total(r: dict) -> str:
    if not all(isinstance(r[k], int) for k in ("prep_min", "cook_min")):
        return "time not given"
    t = r["prep_min"] + r["cook_min"]
    return f"{t // 60} h {t % 60} min" if t >= 60 else f"{t} min" if t else ""


class Box:
    """The household's recipes."""

    def __init__(self, store: Store, today=date.today) -> None:
        self.store, self.today = store, today

    def find(self, doc: dict, ident, partial: bool = True) -> dict:
        """A recipe by id or title: exact first (any case, a leading "the" aside), then one unambiguous partial
        match. None or several: a plain ValueError that lists the candidates and changes nothing."""
        text = _text(ident, "title", 200)
        if not text:
            raise ValueError("say which recipe, by its title")
        rows = doc["recipes"]
        low = text.lower()
        for r in rows:
            if r["id"] == text or r["title"].lower() == low:
                return r
        bare = [r for r in rows if _no_the(r["title"]) == _no_the(text)]
        if len(bare) == 1:
            return bare[0]
        # Partial: each word of three or more letters said must be a word of the title, or start one. Shorter
        # words never match part of a title ("s" is not "Mom's"); a request of only short words matches nothing.
        words = [w for w in _words(text) if w not in FILLER and len(w) >= 3]
        if partial and words:
            hits = [r for r in rows if all(any(x == w or _stem(x) == _stem(w) or x.startswith(w)
                                               for x in _words(r["title"])) for w in words)]
            if len(hits) == 1:
                _USED.said = f"Using '{hits[0]['title']}'."
                return hits[0]
            if len(hits) > 1:
                raise ValueError(f"more than one recipe matches {text!r}: " + "; ".join(r["title"] for r in hits[:15])
                                 + ". Say which one; nothing was changed")
        known = "; ".join(r["title"] for r in rows[:30]) or "none yet"
        raise ValueError(f"no recipe is called {text!r}. The recipes are: {known}")

    def view(self) -> dict:
        with self.store.lock:
            doc = self.store.load()
            for r in doc["recipes"]:
                r["lines"] = [fmt_ingredient(i) for i in r["ingredients"]]
        return {**doc, "today": self.today().isoformat(), "tag_kinds": list(TAG_KINDS)}

    # ---- sums
    def factor_for(self, r: dict, servings=None, factor=None) -> Fraction:
        if isinstance(servings, str) and servings.strip().lower() in FACTOR_WORDS:
            factor, servings = FACTOR_WORDS[servings.strip().lower()], None
        if isinstance(factor, str) and factor.strip().lower() in FACTOR_WORDS:
            factor = FACTOR_WORDS[factor.strip().lower()]
        f = _num_arg(factor, "factor", *FACTOR_RANGE)
        n = _num_arg(servings, "servings", Fraction(1, 4), 1000)
        if n is not None:
            if not _serves(r):
                raise ValueError(f"{r['title']} does not say how many it serves, so it can not be scaled to a number "
                                 "of servings. Scale it by a factor instead, or set its servings")
            f = n / _serves(r)
            if not FACTOR_RANGE[0] <= f <= FACTOR_RANGE[1]:
                raise ValueError("that is too far from the recipe's own servings to scale sensibly")
        return f if f is not None else Fraction(1)

    def scaled(self, r: dict, factor) -> dict:
        lines, not_scaled = [], []
        for ing in r["ingredients"]:
            if scalable(ing, factor):
                lines.append(fmt_ingredient(ing, _exact(ing["qty"]) * factor))
            else:
                lines.append(fmt_ingredient(ing))
                if factor != 1:
                    not_scaled.append(fmt_ingredient(ing))
        return {"factor": float(factor), "servings": round(float(_serves(r) * factor), 2),
                "lines": lines, "not_scaled": not_scaled}

    def recipe_text(self, r: dict, factor=1) -> str:
        s = self.scaled(r, factor)
        head = [r["title"]]
        if r["source"]:
            head.append(f"Source: {r['source']}")
        if _serves(r):
            head.append(f"Serves {_strip0(s['servings'])}" + (f" (scaled by {_strip0(factor, 3)} from "
                                                               f"{_strip0(r['servings'])})" if factor != 1 else ""))
        else:
            if r["servings"] == NOT_READ:
                head.append("Serves: not read when it was imported")
            if factor != 1:
                head.append(f"Scaled by {_strip0(factor, 3)}")
        if r["prep_min"] or r["cook_min"]:
            head.append(f"Prep {_mins(r['prep_min'])}, cook {_mins(r['cook_min'])}")
        tags = [w for k in TAG_KINDS for w in r["tags"][k]]
        if tags:
            head.append("Tags: " + ", ".join(tags))
        out = "\n".join(head) + "\n\nIngredients:\n" + "\n".join(f"- {x}" for x in s["lines"] or ["none listed"])
        if s["not_scaled"]:
            out += "\nNot scaled: " + "; ".join(s["not_scaled"])
        out += "\n\nSteps:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(r["steps"], 1))
        if r["notes"]:
            out += "\n\nNotes:\n" + r["notes"]
        return out

    # ---- tools
    def find_recipes(self, words=None, tag=None, ingredient=None, favorites=None) -> str:
        doc = self.store.load()
        want = [w for w in _words(_text(words, "words", 200)) if w not in FILLER]
        tag_w = _fold(_text(tag, "tag", 60)).strip()
        ing = _core(_text(ingredient, "ingredient", 100))
        fav = _flag(favorites, "favorites") if favorites not in (None, "") else False
        hits = []
        for r in doc["recipes"]:
            tags = {_fold(w) for k in TAG_KINDS for w in r["tags"][k]}
            if tag_w and tag_w not in tags and _stem(tag_w) not in {_stem(t) for t in tags}:
                continue
            if ing and not any(_matches(ing, _core(i["item"])) for i in r["ingredients"]):
                continue
            if fav and not r["favorite"]:
                continue
            if want:
                hay = {_stem(w) for w in _words(" ".join([r["title"], r["notes"], r["source"], " ".join(tags),
                                                         " ".join(i["item"] for i in r["ingredients"])]))}
                hay |= {_stem(w.replace("-", "")) for w in tags}
                if not all(_stem(w) in hay for w in want):
                    continue
            hits.append(r)
        if not hits:
            asked = ", ".join(x for x in (" ".join(want), tag_w and "tag " + tag_w,
                                          ingredient and f"ingredient {ingredient}", fav and "favorites") if x)
            all_titles = "; ".join(r["title"] for r in doc["recipes"][:30]) or "none yet"
            return f"No recipe matches {asked or 'that'}. The recipes are: {all_titles}."
        hits.sort(key=lambda r: (not r["favorite"], r["title"].lower()))
        lines = [f"- {r['title']}{' (favorite)' if r['favorite'] else ''}"
                 + "".join(f"; {x}" for x in (f"serves {_strip0(r['servings'])}" if _serves(r) else "", _total(r),
                                              ", ".join(w for k in TAG_KINDS for w in r["tags"][k]),
                                              f"{r['rating']} stars" if r["rating"] else "") if x)
                 for r in hits[:25]]
        more = f"\n...and {len(hits) - 25} more." if len(hits) > 25 else ""
        return f"{len(hits)} recipe{'s' if len(hits) != 1 else ''}:\n" + "\n".join(lines) + more

    def read_recipe(self, title, servings=None, step=None, factor=None) -> str:
        doc = self.store.load()
        r = self.find(doc, title)
        f = self.factor_for(r, servings, factor)
        if step not in (None, ""):
            n = len(r["steps"])
            if not n:
                return f"{r['title']} has no steps written down."
            word = str(step).strip().lower()
            k = 1 if word in ("first", "start") else n if word in ("last", "final") else _num_arg(step, "step", 1, 1000)
            k = int(k)
            if k > n:
                return f"{r['title']} has {n} steps; there is no step {k}."
            more = f" Next is step {k + 1}." if k < n else " That is the last step."
            said = r["steps"][k - 1]
            said += "" if said.endswith((".", "!", "?")) else "."
            return f"{r['title']}, step {k} of {n}: {said}{more}"
        return self.recipe_text(r, f)

    def what_can_i_make(self, items=None) -> str:
        if isinstance(items, list):
            have_raw = [_text(x, "item", 100) for x in items[:200]]
        else:
            have_raw = re.split(r",|;|\n|\band\b", _text(items, "items", 4000, True))
        have = [c for c in (_core(x) for x in have_raw) if c]
        if not have:
            raise ValueError("say what is on hand, like eggs, spinach, cheese")
        doc = self.store.load()
        ranked = []
        for r in doc["recipes"]:
            if not r["ingredients"]:
                continue
            got, missing, real = 0, [], 0
            for i in r["ingredients"]:
                core = _core(i["item"])
                if any(_matches(h, core) for h in have):
                    got, real = got + 1, real + 1
                elif core in STAPLES:
                    got += 1
                else:
                    missing.append(i["item"])
            if real:
                ranked.append((got / len(r["ingredients"]), got, r, missing))
        if not ranked:
            return ("No recipe uses any of those. Water, salt and pepper are taken as on hand. "
                    f"There are {len(doc['recipes'])} recipes.")
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]["title"].lower()))
        lines = [f"- {r['title']}: {got} of {len(r['ingredients'])} on hand; "
                 + ("nothing missing" if not miss else "missing " + ", ".join(miss)) for _, got, r, miss in ranked[:10]]
        return ("Best matches first (water, salt and pepper are taken as on hand):\n" + "\n".join(lines))

    def _apply(self, r: dict, args: dict) -> list[str]:
        """Set the fields given on *r* (checked); the names of what changed."""
        said = []
        if args.get("new_title") not in (None, ""):
            r["title"] = _text(args["new_title"], "title", 120)
            said.append("title")
        for key, name in (("source", "source"), ("notes", "notes")):
            if args.get(key) is not None:
                r[key] = _text(args[key], name, 4000 if key == "notes" else 300, key == "notes")
                said.append(name)
        if args.get("servings") not in (None, ""):
            r["servings"] = _servings(args["servings"])
            said.append("servings")
        for key, name in (("prep_minutes", "prep_min"), ("cook_minutes", "cook_min")):
            if args.get(key) not in (None, ""):
                r[name] = _minutes(args[key], key.replace("_", " "))
                said.append(key.replace("_", " "))
        if args.get("ingredients") not in (None, "", []):
            ings = _split_lines(args["ingredients"], "ingredients", True)
            if len(ings) > LIMITS["ingredients"]:
                raise ValueError(f"a recipe holds at most {LIMITS['ingredients']} ingredients")
            r["ingredients"] = [clean_ingredient(x) for x in ings]
            said.append("ingredients")
        if args.get("steps") not in (None, "", []):
            steps = _split_lines(args["steps"], "steps", False)
            if len(steps) > LIMITS["steps"]:
                raise ValueError(f"a recipe holds at most {LIMITS['steps']} steps")
            r["steps"] = [s for s in (_text(x, "step", 2000, True) for x in steps) if s]
            said.append("steps")
        if args.get("tags") is not None:
            r["tags"] = _tags(args["tags"])
            said.append("tags")
        if args.get("add_tags") not in (None, ""):
            more = _tags(args["add_tags"])
            for k in TAG_KINDS:
                r["tags"][k] += [w for w in more[k] if w not in r["tags"][k]]
            said.append("tags")
        if args.get("favorite") not in (None, ""):
            r["favorite"] = _flag(args["favorite"], "favorite")
            said.append("favorite" if r["favorite"] else "not a favorite")
        if args.get("rating") not in (None, ""):
            r["rating"] = _rating(args["rating"])
            said.append("rating")
        return said

    def add_recipe(self, title=None, text=None, ingredients=None, steps=None, servings=None, prep_minutes=None,
                   cook_minutes=None, source=None, tags=None, notes=None) -> str:
        raws = read_pasted(text) if text not in (None, "") else [{}]
        given = {"servings": servings, "prep_minutes": prep_minutes, "cook_minutes": cook_minutes, "source": source,
                 "tags": tags, "notes": notes, "ingredients": ingredients, "steps": steps}
        made = []
        for i, raw in enumerate(raws):
            raw = dict(raw)
            if i == 0 and title not in (None, ""):
                raw["title"] = title
            if not _text(raw.get("title"), "title", 120):
                raise ValueError("a recipe needs a title")
            r = clean_recipe({**raw, "id": secrets.token_hex(4)})
            if i == 0:
                self._apply(r, {k: v for k, v in given.items() if v is not None})
            if not r["ingredients"] and not r["steps"]:
                raise ValueError("a recipe needs its ingredients or its steps")
            made.append(r)
        today = self.today().isoformat()
        with self.store.lock:
            doc = self.store.load()
            if len(doc["recipes"]) + len(made) > LIMITS["recipes"]:
                raise ValueError(f"Recipe Box holds at most {LIMITS['recipes']} recipes")
            have = {r["title"].lower() for r in doc["recipes"]}
            added, skipped = [], []
            for r in made:
                if r["title"].lower() in have:
                    skipped.append(r["title"])
                    continue
                r["added"] = today
                doc["recipes"].append(r)
                have.add(r["title"].lower())
                added.append(r)
            if added:
                self.store.save(doc)
        if not added:
            raise ValueError(f"a recipe called {skipped[0]!r} is already here; nothing was added. "
                             "Use change_recipe to change it, or give the new one another title")
        out = "; ".join(f"Added {r['title']}: {_count(len(r['ingredients']), 'ingredient')}, "
                        f"{_count(len(r['steps']), 'step')}"
                        + (f", serves {_strip0(r['servings'])}" if _serves(r) else
                           ", servings not read" if r["servings"] == NOT_READ else ", servings not given")
                        + (", a time not read" if NOT_GIVEN in (r["prep_min"], r["cook_min"]) else "")
                        for r in added) + "."
        return out + (f" Skipped, already here: {', '.join(skipped)}." if skipped else "")

    def change_recipe(self, title, new_title=None, servings=None, prep_minutes=None, cook_minutes=None, source=None,
                      ingredients=None, steps=None, tags=None, add_tags=None, notes=None, favorite=None,
                      rating=None) -> str:
        args = {k: v for k, v in locals().items() if k not in ("self", "title")}
        with self.store.lock:
            doc = self.store.load()
            r = self.find(doc, title)
            trial = json.loads(json.dumps(r))
            said = self._apply(trial, args)
            if not said:
                raise ValueError("say what to change: new_title, servings, prep_minutes, cook_minutes, source, "
                                 "ingredients, steps, tags, add_tags, notes, favorite or rating")
            if trial["title"].lower() != r["title"].lower() and any(
                    x["title"].lower() == trial["title"].lower() for x in doc["recipes"]):
                raise ValueError(f"a recipe called {trial['title']!r} is already here; nothing was changed")
            r.update(trial)
            self.store.save(doc)
        return f"Changed {r['title']}: {', '.join(dict.fromkeys(said))}."

    def scale_or_convert(self, amount=None, unit=None, to_unit=None, factor=None) -> str:
        text = amount if isinstance(amount, (int, float)) and not isinstance(amount, bool) \
            else _text(amount, "amount", 60)
        n = _strict(text)
        unit_t = _text(unit, "unit", 30)
        if n is None and isinstance(text, str):                  # '8 ounces', '350F': a strict number, then a unit
            m = re.fullmatch(rf"({_STRICT})\s*(.+)", text.strip())
            word = m.group(2) if m else ""
            if m and (unit_of(word) or _temp_unit(word) or _other_unit(word)
                      or word.lower().rstrip(".") in ("degrees", "degree", "deg")):
                n, unit_t = _strict(m.group(1)), unit_t or word
        if n is None or not 0 <= n <= 1000000:
            raise ValueError("amount is a number like 8, 1.5 or 1 1/2, nothing else")
        to = _text(to_unit, "to_unit", 30)
        temp, temp_to = _temp_unit(unit_t), _temp_unit(to)
        if temp_to and not temp and re.sub(r"[^a-z]", "", unit_t.lower()) in ("", "degree", "degrees", "deg"):
            temp = "F" if temp_to == "C" else "C"            # '350 degrees in Celsius'
        if temp == "C" and unit_t.strip().lower() in ("c", "c.") and n < 30 and not temp_to:
            temp = ""                                        # '2 c' is two cups, not a cold oven
        if temp and (not to or temp_to):
            if not -50 <= n <= 1000:
                raise ValueError("an oven temperature is between -50 and 1000 degrees")
            if temp_to == temp:
                return f"{_strip0(n)} {temp} is {_strip0(n)} {temp}."
            deg, other = oven(n, temp)
            near = OVEN_STEP * math.floor(deg / OVEN_STEP + 0.5)
            out = f"{_strip0(n)} {temp} is {round(deg)} {other}"
            return out + (f" (about {near} {other} on an oven dial)." if near != round(deg) else ".")
        f = Fraction(1)
        if factor not in (None, ""):
            f = FACTOR_WORDS.get(factor.strip().lower()) if isinstance(factor, str) else None
            f = f or _num_arg(factor, "factor", *FACTOR_RANGE)
        a = unit_of(unit_t) or unit_t.lower()
        start = " ".join(x for x in (fmt_plain(n, a), unit_word(a, n)) if x) + (f" times {_strip0(f, 3)}" if f != 1 else "")
        if not to:
            if factor in (None, ""):
                raise ValueError("say the unit to convert to (to_unit), or a factor to scale by")
            return f"{start} is {say_amount(n * f, a)}."
        if not unit_t:
            raise ValueError("say the unit, like cups or grams")
        if temp_to:
            raise ValueError(f"{unit_t} is not a temperature; an oven temperature is in F or C")
        return f"{start} is {say_amount(convert(n * f, unit_t, to), unit_of(to))}."

    def mark_made(self, title, date=None, rating=None) -> str:
        today = self.today()
        day = _day(date, "date", today) or today
        if day > today:
            raise ValueError("date can not be a day still to come")
        with self.store.lock:
            doc = self.store.load()
            r = self.find(doc, title)
            if len(r["made"]) >= LIMITS["made"] and day.isoformat() not in r["made"]:
                r["made"] = r["made"][1:]
            r["made"] = sorted(set(r["made"]) | {day.isoformat()})
            if rating not in (None, ""):
                r["rating"] = _rating(rating)
            self.store.save(doc)
        return (f"Marked {r['title']} as made on {day.isoformat()} ({len(r['made'])} time"
                f"{'s' if len(r['made']) != 1 else ''} in all)."
                + (f" Rating: {r['rating']} of 5." if rating not in (None, "") else ""))

    def remove_recipe(self, title) -> str:
        with self.store.lock:
            doc = self.store.load()                      # read before anything is removed
            r = self.find(doc, title)
            doc["recipes"] = [x for x in doc["recipes"] if x is not r]
            self.store.save(doc)
        return f"Removed the recipe {r['title']}."

    # ---- the page
    def save_from_page(self, recipe) -> str:
        if not isinstance(recipe, dict):
            raise ValueError("send the recipe as an object")
        raw = dict(recipe)
        raw["ingredients"] = _split_lines(raw.get("ingredients"), "ingredients", True)
        raw["steps"] = _split_lines(raw.get("steps"), "steps", False)
        rid = raw.get("id")
        with self.store.lock:
            doc = self.store.load()
            old = next((x for x in doc["recipes"] if x["id"] == rid), None) if rid else None
            r = clean_recipe({**raw, "id": old["id"] if old else secrets.token_hex(4),
                              "made": old["made"] if old else [], "added": old["added"] if old else
                              self.today().isoformat(), "rating": raw.get("rating", old["rating"] if old else 0),
                              "favorite": raw.get("favorite", old["favorite"] if old else False)})
            if any(x["title"].lower() == r["title"].lower() and x is not old for x in doc["recipes"]):
                raise ValueError(f"a recipe called {r['title']!r} is already here")
            if old:
                old.clear()
                old.update(r)
            else:
                if len(doc["recipes"]) >= LIMITS["recipes"]:
                    raise ValueError(f"Recipe Box holds at most {LIMITS['recipes']} recipes")
                doc["recipes"].append(r)
            self.store.save(doc)
        return f"Saved {r['title']}."

    def import_pasted(self, text=None, data=None) -> str:
        """This app's export (as data or text), JSON-LD, or plain text. Never overwrites: a title already here
        is skipped."""
        if data is not None:
            raws = data.get("recipes") if isinstance(data, dict) else data
            if not isinstance(raws, list):
                raise ValueError("that file holds no list of recipes")
        else:
            raws = read_pasted(text)
        if len(raws) > LIMITS["recipes"]:
            raise ValueError(f"Recipe Box holds at most {LIMITS['recipes']} recipes")
        clean = [clean_recipe({**x, "id": secrets.token_hex(4)} if isinstance(x, dict) else x, i)
                 for i, x in enumerate(raws)]
        with self.store.lock:
            doc = self.store.load()                      # a file that will not read is never replaced
            have = {r["title"].lower() for r in doc["recipes"]}
            added, skipped = [], []
            for r in clean:
                if r["title"].lower() in have:
                    skipped.append(r["title"])
                    continue
                if len(doc["recipes"]) >= LIMITS["recipes"]:
                    raise ValueError(f"Recipe Box holds at most {LIMITS['recipes']} recipes; nothing was imported")
                r["added"] = r["added"] or self.today().isoformat()
                doc["recipes"].append(r)
                have.add(r["title"].lower())
                added.append(r["title"])
            if added:
                self.store.save(doc)
        return (f"Imported {len(added)} recipe{'s' if len(added) != 1 else ''}"
                + (f": {', '.join(added[:20])}" if added else "") + "."
                + (f" Skipped {len(skipped)} already here (nothing is overwritten): {', '.join(skipped[:20])}."
                   if skipped else ""))


# --------------------------------------------------------------- MCP tools

def _p(kind, text: str) -> dict:
    return {"type": kind, "description": text}


_TITLE = _p("string", "The recipe's title, or words from it, like banana bread.")


def _LIST(text: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": text}
TOOLS = [
    ("find_recipes", "Use only when the owner asks which recipes there are: search the recipe box by words, a "
     "tag (like dessert, vegetarian or summer), an ingredient, or favorites.",
     {"words": _p("string", "Optional: words to look for in titles, notes and ingredients."),
      "tag": _p("string", "Optional: one tag, like dinner, italian, vegan or winter."),
      "ingredient": _p("string", "Optional: an ingredient the recipe uses, like chicken."),
      "favorites": _p("boolean", "Optional: true for favorites only.")}, []),
    ("read_recipe", "Use to read out one recipe, by name; no need to look it up first. It can scale the "
     "recipe to a number of servings, and give one step at a time by its number for cooking along.",
     {"title": _TITLE,
      "servings": _p("number", "Optional: scale to this many servings, or double, half, triple."),
      "step": _p("integer", "Optional: read only this step number (or first, last)."),
      "factor": _p("number", "Optional: scale by this factor, like 1.5.")}, ["title"]),
    ("what_can_i_make", "Use when the owner says what food is on hand and asks what to cook: it ranks the "
     "recipes by how many of their ingredients are on hand and lists what is missing.",
     {"items": _p("string", "The food on hand, like eggs, spinach, cheese.")},
     ["items"]),
    ("add_recipe", "Use to save a new recipe the owner gives: a title with ingredients and steps, or one "
     "block of pasted text (a recipe written out, or a recipe web page's JSON-LD the owner pasted).",
     {"title": _p("string", "The recipe's title."),
      "text": _p("string", "Optional: the whole recipe pasted as one block of text, instead of the fields."),
      "ingredients": _LIST("The ingredients, one per item, like 2 cups flour."),
      "steps": _LIST("The steps in order, one per line or a list."),
      "servings": _p("number", "Optional: how many it serves."),
      "prep_minutes": _p("integer", "Optional: preparation minutes."),
      "cook_minutes": _p("integer", "Optional: cooking minutes."),
      "source": _p("string", "Optional: where it came from, like Grandma Jo or a book."),
      "tags": _p("string", "Optional: tags like dinner, italian, vegetarian, winter."),
      "notes": _p("string", "Optional: notes.")}, []),
    ("change_recipe", "Use to change a saved recipe, by name; no need to look it up first: rename it, set "
     "its servings, times, source, ingredients, steps, tags, notes, or make it a favorite.",
     {"title": _TITLE, "new_title": _p("string", "Optional: the new title."),
      "servings": _p("number", "Optional: how many it serves."),
      "prep_minutes": _p("integer", "Optional: preparation minutes."),
      "cook_minutes": _p("integer", "Optional: cooking minutes."),
      "source": _p("string", "Optional: where it came from."),
      "ingredients": _LIST("Optional: the whole new ingredient list."),
      "steps": _LIST("Optional: the whole new list of steps."),
      "tags": _p("string", "Optional: the new tags (replaces the old ones)."),
      "add_tags": _p("string", "Optional: tags to add."),
      "notes": _p("string", "Optional: the new notes."),
      "favorite": _p("boolean", "Optional: true to make it a favorite, false to stop."),
      "rating": _p("integer", "Optional: 1 to 5 stars.")}, ["title"]),
    ("scale_or_convert", "Use for a kitchen sum with no recipe named: turn an amount into another unit (cups, "
     "tablespoons, teaspoons, fluid ounces, milliliters, liters; ounces, pounds, grams, kilograms), an oven "
     "temperature between Fahrenheit and Celsius, or multiply an amount by a factor. It never turns volume into "
     "weight.",
     {"amount": _p("string", "The amount, like 8 or 1 1/2."),
      "unit": _p("string", "Its unit, like ounces, cups or F."),
      "to_unit": _p("string", "Optional: the unit wanted, like grams or C."),
      "factor": _p("number", "Optional: multiply the amount by this, like 1.5 or double.")},
     ["amount"]),
    ("mark_made", "Use when the owner says they made or cooked a recipe: by name; no need to look it up first. "
     "It notes the day it was made (default today) and can rate it 1 to 5 stars.",
     {"title": _TITLE, "date": _p("string", "Optional: the day, like 2027-03-31 or yesterday. Default today."),
      "rating": _p("integer", "Optional: 1 to 5 stars.")}, ["title"]),
    ("remove_recipe", "Use only when the owner asks to delete a recipe, by name; no need to look it up "
     "first. It removes the recipe from this computer for good.",
     {"title": _TITLE}, ["title"]),
]
CHANGES = ["add_recipe", "change_recipe", "mark_made", "remove_recipe"]
DELETES = ["remove_recipe"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Box, msg) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params")
    if isinstance(mid, bool) or not isinstance(mid, (str, int, float, type(None))):
        mid = None                               # an id that is an object or array is never echoed back
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object."}}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if isinstance(asked, str) and asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "No tool by that name."}}
        try:
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            allowed = next(t[2] for t in TOOLS if t[0] == name)
            extra = [k for k in args if k not in allowed]
            if extra:
                raise TypeError(f"{name} does not take {', '.join(map(str, extra))}")
            _USED.said = ""
            text = getattr(app, name)(**args)
            if _USED.said:                       # found by part of its title: say first which recipe was used
                text = f"{_USED.said} {text}"
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Recipe Box does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/save", "/api/remove", "/api/made", "/api/set", "/api/scale", "/api/match", "/api/import")


class Handler(BaseHTTPRequestHandler):
    server_version = "RecipeBox/1"
    app: Box
    token: str
    timeout = 15        # seconds; a stalled sender is cut off

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"), extra=extra)

    def _length(self) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most MAX_BODY bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()):
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        if len(raw) > 12 or int(raw) > MAX_BODY:
            raise ValueError(f"The request is too large. Recipe Box takes at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _drain(self, most: int = DROP_MOST, seconds: float = DROP_S) -> None:
        """Read and drop a body that will be refused (at most *most* bytes within *seconds*, stopping early when
        nothing more arrives for IDLE_S), so the answer reaches the sender instead of a reset connection."""
        raw = (self.headers.get("Content-Length") or "").strip()
        left = min(int(raw), most) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else most
        end = time.monotonic() + seconds
        try:
            while left > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.05, min(IDLE_S, end - time.monotonic())))
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(self.timeout)
        self.close_connection = True

    def _body(self):
        try:
            n = self._length()
        except ValueError:
            self._drain()
            raise
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
        try:
            return _loads(bytes(raw).decode("utf-8")) if raw else {}
        except UnicodeDecodeError:
            raise ValueError("The request body is not UTF-8.") from None

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _refused(self) -> bool:
        """Every route needs the token, checked before anything else is read."""
        if self._token_ok():
            return False
        if (self.headers.get("Content-Length") or "0").strip() != "0":
            self._drain(REFUSED_MOST, REFUSED_S)               # at most 64 KB, for at most 2 seconds
        self.close_connection = True
        self._json(401, {"error": "Recipe Box needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Recipe Box stopped on this request with an error ({type(exc).__name__})."})
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
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/data":
                return self._json(200, self.app.store.load())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
            if path == "/view":
                return self._json(200, self.app.view())
            if path == "/export":
                name = f"recipe-box-{self.app.today().isoformat()}.json"
                return self._json(200, {**self.app.store.load(), "exported": _now()},
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/export_text":
                rid = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
                if not ID_RE.fullmatch(rid):
                    return self._json(400, {"error": "Say which recipe (id)."})
                doc = self.app.store.load()
                r = next((x for x in doc["recipes"] if x["id"] == rid), None)
                if r is None:
                    return self._json(404, {"error": "No recipe with that id."})
                return self._send(200, (self.app.recipe_text(r) + "\n").encode("utf-8"), "text/plain; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="recipe.txt"'})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Recipe Box has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            self._drain()
            return self._json(404, {"error": "Recipe Box has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path == "/api/save":
                out = {"text": app.save_from_page(body.get("recipe"))}
            elif path == "/api/remove":
                out = {"text": app.remove_recipe(self._id(body))}
            elif path == "/api/made":
                out = {"text": app.mark_made(self._id(body), body.get("date"), body.get("rating"))}
            elif path == "/api/set":
                if body.get("favorite") is None and body.get("rating") is None:
                    raise ValueError("send favorite or rating")
                out = {"text": app.change_recipe(self._id(body), favorite=body.get("favorite"),
                                                 rating=body.get("rating"))}
            elif path == "/api/scale":
                with app.store.lock:
                    doc = app.store.load()
                    r = app.find(doc, self._id(body), partial=False)
                    out = app.scaled(r, app.factor_for(r, body.get("servings"), body.get("factor")))
                return self._json(200, out)
            elif path == "/api/match":
                return self._json(200, {"text": app.what_can_i_make(body.get("items"))})
            else:
                out = {"text": app.import_pasted(body.get("text"), body.get("data"))}
            out["last_updated"] = app.store.load()["last_updated"]
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    @staticmethod
    def _id(body: dict) -> str:
        rid = body.get("id")
        if not (isinstance(rid, str) and ID_RE.fullmatch(rid)):
            raise ValueError("say which recipe (id)")
        return rid

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
        except json.JSONDecodeError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        except ValueError as exc:                    # too large, cut short, not UTF-8, nested too deeply
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, today=date.today) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Box(Store(data), today)
    handler = type("BoxHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recipe Box for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Recipe Box with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
