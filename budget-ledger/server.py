"""Budget Ledger: where the household's money went, read from the statement files the owner downloads.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``                the page (web/index.html), shown in the Desktop at /pkg/budget-ledger/
- ``/health``          "ok"
- ``/updated``         the ledger's stamp; ``/view`` accounts, categories, rules, budgets, imports, transfer pairs
- ``/transactions``    the transactions, filtered (GET); ``/month`` one month's sums; ``/recurring`` repeating payments
- ``/export``          everything as one JSON file; ``/export.csv`` the transactions as CSV
- ``/api/...``         the page's changes (POST), among them ``/api/preview`` and ``/api/import`` for a statement file
- ``/mcp``             the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN. The Desktop reaches the page through the
Core's /pkg/budget-ledger/, which adds it.

The ledger is one JSON document in PUFFPAI_DATA (``ledger.json``). Writes replace the whole file at once (temp
file, then rename) under one lock. A file that exists but will not read, an empty one included, is never written
over. An imported statement is kept only as its cleaned name and sha256, never the file itself. Only the last four
digits of an account or card number found in a file are kept (in its name, descriptions, payees and memos), and the
bank's id for a transaction only as a keyed fingerprint. The import record and its transactions are saved in the
same write. How a file is read,
how duplicates, categories, transfers and repeating payments are found and how totals are worked out: PROCESS.md.

It moves no money, signs in nowhere and never contacts a bank. It uses no internet.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import html
import io
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
NAME = "budget-ledger"
VERSION = "1.0.0"
DOC = "ledger.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LINE = "Budget Ledger shows what your statements say. It gives no financial advice and moves no money."

# ---------------------------------------------------------------- every number, in one table
NUMBERS = {
    "file_bytes": 10 * 1024 * 1024,      # one statement file
    "body_bytes": 256 * 1024,            # a page change or an MCP call
    "body_wait_s": 5.0,                  # the whole wait for a request body
    "field_chars": 1000,                 # characters kept from one field of a statement
    "csv_field_bytes": 100_000,          # a CSV field longer than this stops the read
    "amount_digits": 13,                 # digits before the decimal mark, at most
    "header_rows": 20,                   # rows looked at for the header line
    "bad_row_share_pct": 10,             # more rows than this that will not read: the columns are asked
    "preview_rows": 20,
    "list_rows": 50,
    "page_rows_most": 1000,              # /transactions?limit= is capped here
    "drain_bytes": 256 * 1024 * 1024,    # a refused body up to this size is read and dropped, then answered
    "drain_wait_s": 20.0,                # the whole wait for that
    "top_payees": 10,
    "transfer_days": 3,                  # a transfer's two sides are at most this many days apart
    "recurring_min": 3,                  # times seen before a payment counts as repeating
    "recurring_min_yearly": 2,
    "similar_pct": 20,                   # a repeating amount stays within this share of its middle value
    "steady_share_pct": 75,              # this share of gaps and amounts must fit
    "stopped_after_periods": 2,          # not seen for this many gaps: "may have stopped"
    "months_shown": 12,
}
LIMITS = {"transactions": 200_000, "accounts": 50, "categories": 100, "rules": 500, "budgets": 2000,
          "imports": 2000, "not_transfers": 5000, "mappings": 50}
# (name, shortest gap in days, longest gap in days, times a year)
CADENCES = (("weekly", 6, 8, 52), ("every two weeks", 13, 15, 26), ("monthly", 26, 35, 12),
            ("every three months", 85, 97, 4), ("yearly", 355, 375, 1))
# Digits after the decimal mark; every other currency has 2.
EXPONENT = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "PYG": 0, "UGX": 0, "XAF": 0, "XOF": 0,
            "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3}
CURRENCY_WORDS = {"$": "USD", "dollar": "USD", "dollars": "USD", "us dollars": "USD", "€": "EUR", "euro": "EUR",
                  "euros": "EUR", "£": "GBP", "pound": "GBP", "pounds": "GBP", "¥": "JPY", "yen": "JPY",
                  "canadian dollars": "CAD", "australian dollars": "AUD", "rupees": "INR", "₹": "INR"}

ACCOUNT_KINDS = ("checking", "savings", "card", "cash", "other")
KIND_WORDS = {"current": "checking", "current account": "checking", "debit": "checking", "bank": "checking",
              "checking account": "checking", "cheque": "checking", "chequing": "checking", "saving": "savings",
              "savings account": "savings", "money market": "savings", "moneymrkt": "savings", "credit card": "card",
              "creditcard": "card", "credit": "card", "creditline": "card", "ccard": "card", "visa": "card",
              "mastercard": "card", "cards": "card", "wallet": "cash", "petty cash": "cash"}
STARTER_CATEGORIES = ["housing", "utilities", "groceries", "eating out", "transport", "fuel", "health", "insurance",
                      "childcare", "school", "clothing", "household", "entertainment", "subscriptions", "gifts",
                      "travel", "savings", "debt payments", "taxes", "income", "transfers", "other"]
CATEGORY_WORDS = {"rent": "housing", "mortgage": "housing", "home": "housing", "bills": "utilities",
                  "utility": "utilities", "electric": "utilities", "power": "utilities", "water": "utilities",
                  "grocery": "groceries", "supermarket": "groceries", "food shopping": "groceries",
                  "restaurants": "eating out", "restaurant": "eating out", "dining": "eating out",
                  "dining out": "eating out", "takeout": "eating out", "take out": "eating out",
                  "takeaway": "eating out", "coffee": "eating out", "gas": "fuel", "petrol": "fuel",
                  "diesel": "fuel", "car": "transport", "transit": "transport", "bus": "transport",
                  "medical": "health", "doctor": "health", "pharmacy": "health", "daycare": "childcare",
                  "child care": "childcare", "tuition": "school", "clothes": "clothing", "fun": "entertainment",
                  "subscription": "subscriptions", "streaming": "subscriptions", "present": "gifts",
                  "presents": "gifts", "gift": "gifts", "holiday": "travel", "vacation": "travel",
                  "debt": "debt payments", "loan": "debt payments", "loans": "debt payments", "tax": "taxes",
                  "pay": "income", "paycheck": "income", "salary": "income", "wages": "income",
                  "transfer": "transfers", "misc": "other", "miscellaneous": "other"}
REFUND_WORDS = {"refund", "return", "reversal", "credit", "rebate"}
UNCATEGORIZED_WORDS = {"uncategorized", "uncategorised", "none", "no category", "unsorted", "blank"}
# The built-in words for obvious cases: (category, words, which sign: +1 money in, -1 money out)
BUILTIN_WORDS = (
    ("income", ("payroll", "salary", "direct deposit", "dir dep", "wages", "paycheck", "pension"), 1),
    ("housing", ("rent", "mortgage", "property management", "hoa"), -1),
    ("utilities", ("electric", "electricity", "water bill", "utility", "utilities", "gas bill", "sewer",
                   "internet", "broadband", "phone bill", "mobile bill", "energy"), -1),
    ("groceries", ("grocery", "groceries", "supermarket", "food market", "foods market"), -1),
    ("eating out", ("restaurant", "cafe", "coffee", "pizza", "diner", "bakery", "grill", "bistro", "burger",
                    "takeaway", "taqueria", "sushi"), -1),
    ("fuel", ("fuel", "petrol", "gas station", "service station"), -1),
    ("transport", ("parking", "transit", "taxi", "toll", "tolls", "railway", "rail", "metro", "bus fare",
                   "rideshare"), -1),
    ("health", ("pharmacy", "clinic", "dental", "dentist", "doctor", "hospital", "optician", "medical"), -1),
    ("insurance", ("insurance", "assurance"), -1),
    ("childcare", ("daycare", "childcare", "child care", "nursery", "babysitter"), -1),
    ("school", ("tuition", "school"), -1),
    ("clothing", ("clothing", "apparel", "shoes", "boutique"), -1),
    ("household", ("hardware", "home improvement", "home goods", "furniture"), -1),
    ("entertainment", ("cinema", "movie", "movies", "theater", "theatre", "concert", "tickets"), -1),
    ("subscriptions", ("subscription", "streaming", "membership", "monthly plan"), -1),
    ("gifts", ("gift", "florist", "flowers"), -1),
    ("travel", ("hotel", "airline", "airlines", "flight", "motel", "hostel"), -1),
    ("taxes", ("tax payment", "property tax", "income tax", "council tax"), -1),
    ("debt payments", ("loan payment", "student loan", "auto loan", "car loan"), -1),
)
# Header words for a CSV statement, by role; more exact words first.
HEADER_WORDS = (
    ("date", ("date", "transaction date", "trans date", "txn date", "posted", "posting date", "posted date",
              "post date", "date posted", "booking date", "value date", "effective date", "transaction posted date")),
    ("description", ("description", "payee", "name", "details", "transaction description", "narrative",
                     "particulars", "merchant", "merchant name", "transaction details", "description 1", "payee name")),
    ("memo", ("memo", "notes", "note", "reference", "description 2", "additional details", "extended description")),
    ("amount", ("amount", "transaction amount", "amt", "value", "sum", "net amount", "amount usd", "amount eur",
                "amount gbp")),
    ("debit", ("debit", "debits", "debit amount", "withdrawal", "withdrawals", "withdrawal amount", "money out",
               "paid out", "out", "amount debit", "charges", "spent")),
    ("credit", ("credit", "credits", "credit amount", "deposit", "deposits", "deposit amount", "money in",
                "paid in", "in", "amount credit", "received")),
    ("balance", ("balance", "running balance", "available balance", "ledger balance", "running bal")),
    ("category", ("category", "categories", "transaction category")),
    ("type", ("type", "transaction type", "dr cr", "cr dr", "debit credit", "credit debit", "debit or credit")),
)
ROLES = tuple(r for r, _ in HEADER_WORDS)
DATE_ORDERS = {"ymd": "year-month-day", "mdy": "month/day/year", "dmy": "day/month/year"}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12, "sept": 9}
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
TRUE_WORDS = {"true", "yes", "y", "1", "on", "please", "do", "ok"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "dont", "don't", "not"}
ID_RE = re.compile(r"[a-z][0-9a-f]{4,16}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
csv.field_size_limit(NUMBERS["csv_field_bytes"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(text) -> str:
    """Words in lower case, accents folded, single spaces: 'Eating_Out' -> 'eating out', 'Caf\u00e9 Luna' -> 'cafe
    luna', '\u00dcber' -> 'uber'. A word is letters and digits of any script (and & or '); nothing else joins
    one, and no letter is ever dropped, so a word in another script matches only itself."""
    text = str(text or "")
    if text.isascii():
        return " ".join(re.sub(r"[^a-z0-9&']+", " ", text.lower()).split())
    out = []
    for c in unicodedata.normalize("NFKD", text.casefold()):
        kind = unicodedata.category(c)
        if kind in ("Mn", "Me"):                         # an accent: folded away
            continue
        out.append(c if c.isalnum() or c in "&'" or kind == "Mc" else "'" if c in "\u2019\u02bc" else " ")
    return " ".join("".join(out).split())


def _ascii_norm(text) -> str:
    """A name as versions before format 4 kept it: every letter outside a to z dropped ('Caf\u00e9 Treats' was kept
    as 'caf treats'). The letter is lost, so such a key is only ever found again by this same rule."""
    def old(t):
        return " ".join(re.sub(r"[^a-z0-9&']+", " ", str(t or "").lower()).split())
    return old(_mask_numbers(old(text)))


def _masked_norm(text) -> str:
    """A category as it is kept: lower-case words, any card or account number in them cut to its last four."""
    return _norm(_mask_numbers(_norm(text)))


def _spaced(text) -> str:
    return " " + _norm(text) + " "


def _fits(words, hay) -> bool:
    """Each word given equals a word of *hay* or starts one (three letters at least); never letters inside a
    word, so "Ron" does not find "Sharon"."""
    want, have = _norm(words).split(), _norm(hay).split()
    return bool(want) and all(any(h == w or (len(w) >= 3 and h.startswith(w)) for h in have) for w in want)


def _selects(words, hay, *whole) -> bool:
    """_fits, for what changes or removes something by its name: words that are all shorter than three letters
    select only a name that is exactly those words (one of *whole*), never one word of a longer name."""
    want = _norm(words)
    if want and all(len(w) < 3 for w in want.split()):
        return any(_norm(x) == want for x in whole)
    return _fits(want, hay)


_USED = threading.local()               # what a tool found by part of its name, said first in its answer


def _using(name: str) -> None:
    said = getattr(_USED, "said", None)
    if said is not None and f"Using '{name}'." not in said:
        said.append(f"Using '{name}'.")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- checked input

def _text(value, name: str, most: int, cut: bool = False) -> str:
    """Words of at most *most* characters, trimmed, control characters made spaces and invisible format
    characters (Unicode category Cf) dropped; '' when not given. With *cut*, longer words are cut to *most* instead
    of refused."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be words")
    value = "".join(c if c >= " " and not "\x7f" <= c <= "\x9f" else " " for c in str(value))
    if not value.isascii():
        value = _strip_invisible(value)      # invisible format characters (bidi marks, zero-width) are dropped
    value = " ".join(value.split())
    if len(value) > most:
        if not cut:
            raise ValueError(f"{name} is at most {most} characters")
        value = value[:most].rstrip()
    return value


def _typed(value, name: str, most: int) -> str:
    """Words the owner typed that are kept or said back (a name, a note, rule or search words): checked as
    _text, then any card or account number in them keeps only its last four."""
    return _mask_numbers(_text(value, name, most))


def _flag(value, name: str) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
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


def _loads(raw: bytes | str):
    """JSON, refusing huge numbers, NaN and Infinity, and nesting too deep to read, as plain ValueErrors."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, parse_constant=_no_constant, parse_int=_int, parse_float=_float)
    except RecursionError:
        raise ValueError("that JSON is nested too deeply") from None
    except UnicodeDecodeError:
        raise ValueError("that is not UTF-8 text") from None


def exponent(cur: str) -> int:
    return EXPONENT.get(cur, 2)


def money(minor: int, cur: str) -> str:
    """An integer count of the smallest unit as words, like -1,234.56 USD."""
    exp = exponent(cur)
    whole, frac = divmod(abs(int(minor)), 10 ** exp)
    return f"{'-' if minor < 0 else ''}{whole:,}{'.' + str(frac).zfill(exp) if exp else ''} {cur}"


def _currency(value, default: str = "USD") -> str:
    text = _text(value, "currency", 30)
    if not text:
        return default
    if re.fullmatch(r"[A-Za-z]{3}", text):
        return text.upper()
    got = CURRENCY_WORDS.get(text.lower()) or CURRENCY_WORDS.get(_norm(text))
    if got:
        return got
    raise ValueError("currency is a three-letter code like USD, EUR or GBP")


# ---------------------------------------------------------------- amounts

def guess_decimal(values) -> str:
    """The decimal mark a whole column uses: ',' when some value clearly ends in a comma and one or two digits
    and none in a point and one or two digits; '.' otherwise."""
    comma = point = False
    for v in values:
        s = str(v or "").strip()[-40:]                      # only the end matters; plain string methods
        for end in ("cr", "dr", "-", ")"):
            if s.lower().endswith(end):
                s = s[:-len(end)].rstrip()
                break
        if re.search(r",\d{1,2}$", s):
            comma = True
        if re.search(r"\.\d{1,2}$", s):
            point = True
    return "," if comma and not point else "."


def parse_amount(raw, exp: int = 2, decimal: str | None = None) -> int | None:
    """A written amount as an integer count of the smallest unit. None for an empty cell; ValueError for one
    that is not an amount. Takes currency signs and codes, thousands separators, a leading or trailing minus,
    parentheses, and CR (money in) or DR (money out) at the end."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        raw = str(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw) or abs(raw) > 1e13:
            raise ValueError("that amount is too big")
        raw = f"{raw:.{max(exp, 0)}f}"
    s = str(raw).strip()
    if not s:
        return None
    if len(s) > 40:
        raise ValueError("that amount is too long")
    t = s.upper().replace("−", "-").replace("\xa0", "").replace(" ", "").replace("'", "")
    neg = False
    if t.endswith("CR"):
        t = t[:-2]
    elif t.endswith("DR"):
        t, neg = t[:-2], True
    t = re.sub(r"[A-Z$€£¥₹₩₽₪₫₱₦฿]", "", t)
    for _ in range(3):                     # signs and parentheses, in any order
        if t.startswith("(") and t.endswith(")"):
            t, neg = t[1:-1], not neg
        elif t.startswith("+"):
            t = t[1:]
        elif t.startswith("-"):
            t, neg = t[1:], not neg
        elif t.endswith("-"):
            t, neg = t[:-1], not neg
    if not re.fullmatch(r"[0-9.,]+", t) or not re.search(r"\d", t):
        raise ValueError(f"{s!r} is not an amount")
    if decimal is None:
        if "," in t and "." in t:
            decimal = "," if t.rfind(",") > t.rfind(".") else "."
        else:
            decimal = "," if re.fullmatch(r"\d*,\d{1,2}", t) else "."
    other = "." if decimal == "," else ","
    t = t.replace(other, "").replace(decimal, ".")
    if t.count(".") > 1:
        raise ValueError(f"{s!r} is not an amount")
    whole, _, frac = t.partition(".")
    whole = whole.lstrip("0") or "0"
    if len(whole) > NUMBERS["amount_digits"]:
        raise ValueError("that amount is too big")
    if len(frac) > exp and frac[exp:].strip("0"):
        raise ValueError(f"{s!r} has more decimal places than the currency has")
    value = int(whole) * 10 ** exp + (int(frac[:exp].ljust(exp, "0")) if exp else 0)
    return -value if neg else value


def _loose_amount(value, exp: int, name: str) -> int | None:
    """A sent amount, loosely: 600, '600', '$600', '600 a month', '1,200.50'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} is an amount like 600 or 42.50")
    if isinstance(value, (int, float)):
        return parse_amount(value, exp)
    text = _text(value, name, 60)
    if text.lower() in ("none", "nothing", "zero", "remove", "no budget"):
        return 0
    m = re.search(r"[-+(]?[$€£¥₹]?\s*\d[\d,.]*\)?(?:\s*(?:cr|dr))?", text, re.I)
    if not m:
        raise ValueError(f"{name} is an amount like 600 or 42.50")
    try:
        return parse_amount(m.group(0), exp)
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None


# ---------------------------------------------------------------- dates

def _year(text: str) -> int:
    n = int(text)
    if len(text) == 2:
        n += 2000 if n < 70 else 1900
    return n


def _month_word(text: str) -> int | None:
    """1 to 12 for a month name or its start (oct, Sept, december); None otherwise."""
    t = text.lower().rstrip(".")
    if len(t) < 3 or not t.isalpha():
        return None
    if t == "sept":
        return 9
    return next((i for i, name in enumerate(MONTH_NAMES, 1) if name.lower().startswith(t)), None)


def parse_date(raw, order: str) -> date | None:
    """One written date read with *order* (ymd, mdy or dmy); None when it does not read that way."""
    t = str(raw or "").strip().lower()[:40]
    t = re.sub(r"(?:t|\s)\d{1,2}:\d{2}.*$", "", t).strip()
    try:
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:\d{6})?(?:\.\d+)?(?:\[.*\])?", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])) if order == "ymd" else None
        m = re.fullmatch(r"(\d{1,2})[\s\-/.]*([a-z]{3,9})[\s\-/.,]*(\d{2,4})", t)
        if m and _month_word(m[2]):
            return date(_year(m[3]), _month_word(m[2]), int(m[1]))
        m = re.fullmatch(r"([a-z]{3,9})[\s\-/.]*(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/.]*(\d{2,4})", t)
        if m and _month_word(m[1]):
            return date(_year(m[3]), _month_word(m[1]), int(m[2]))
        m = re.fullmatch(r"(\d{1,4})[/\-.'](\d{1,2})[/\-.'](\d{1,4})", t)
        if not m:
            return None
        a, b, c = m.groups()
        if order == "ymd":
            return date(int(a), int(b), int(c)) if len(a) == 4 and len(c) <= 2 else None
        if len(a) > 2 or len(c) not in (2, 4):
            return None
        return date(_year(c), int(a), int(b)) if order == "mdy" else date(_year(c), int(b), int(a))
    except ValueError:
        return None


def date_orders(values) -> tuple[list[str], bool]:
    """(the orders every value reads with, whether they give different dates). Two orders that read every value
    to different dates mean the owner is asked."""
    values = [v for v in values if str(v or "").strip()]
    if not values:
        return [], False
    fits = {o: [parse_date(v, o) for v in values] for o in DATE_ORDERS}
    fits = {o: got for o, got in fits.items() if all(got)}
    distinct = {tuple(got) for got in fits.values()}
    return list(fits), len(distinct) > 1


def _day_words(value, today: date) -> date | None:
    """A day said loosely: 2026-11-03, today, yesterday, November 3, 3 Nov, Nov 3 2026."""
    text = _text(value, "date", 40).lower().replace(",", " ")
    if not text:
        return None
    if text in ("today", "yesterday"):
        return today - timedelta(days=0 if text == "today" else 1)
    got = parse_date(text, "ymd")
    if got:
        return got
    words = text.replace("the ", "").replace(" of ", " ").split()
    words = [re.sub(r"(st|nd|rd|th)$", "", w) if re.fullmatch(r"\d{1,2}(st|nd|rd|th)", w) else w for w in words]
    mon = next((_month_word(w) for w in words if _month_word(w)), None)
    nums = [w for w in words if w.isdigit()]
    day = next((int(w) for w in nums if len(w) <= 2), None)
    year = next((int(w) for w in nums if len(w) == 4), None)
    if mon and day:
        try:
            got = date(year or today.year, mon, day)
            if year is None and got > today:
                got = date(today.year - 1, mon, day)
            return got
        except ValueError:
            pass
    for order in ("mdy", "dmy"):
        got = parse_date(text, order)
        if got:
            return got
    raise ValueError("date is a day like 2026-11-03, November 3, today or yesterday")


def _month_start(d: date, back: int = 0) -> date:
    n = d.year * 12 + d.month - 1 - back
    return date(n // 12, n % 12 + 1, 1)


def _month_end(first: date) -> date:
    return _month_start(first, -1) - timedelta(days=1)


def month_words(value, today: date) -> str:
    """A month said loosely as 'YYYY-MM': this month, last month, October, Oct 2026, 2026-10, 10/2026."""
    text = _norm(_text(value, "month", 40)).replace("the ", "")
    if text in ("", "this month", "current month", "this", "now", "month"):
        return _month_start(today).isoformat()[:7]
    if text in ("last month", "previous month", "past month", "last"):
        return _month_start(today, 1).isoformat()[:7]
    if text in ("month before last", "two months ago", "2 months ago"):
        return _month_start(today, 2).isoformat()[:7]
    m = re.fullmatch(r"(\d+) months? ago", text)
    if m and int(m[1]) < 600:
        return _month_start(today, int(m[1])).isoformat()[:7]
    raw = _text(value, "month", 40)
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", raw) or None
    if m and 1 <= int(m[2]) <= 12:
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{4})", raw)
    if m and 1 <= int(m[1]) <= 12:
        return f"{int(m[2]):04d}-{int(m[1]):02d}"
    words = text.split()
    mon = next((_month_word(w) for w in words if _month_word(w)), None)
    year = next((int(w) for w in words if re.fullmatch(r"\d{4}", w)), None)
    if mon:
        if year is None:
            year = today.year if mon <= today.month else today.year - 1
            if "last" in words and mon == today.month:
                year -= 1
        return f"{year:04d}-{mon:02d}"
    raise ValueError("month is like this month, last month, October, October 2026 or 2026-10")


def period_words(value, today: date) -> tuple[date, date, str]:
    """(first day, last day, the words for it) for a period said loosely. Default: this month."""
    text = _norm(_text(value, "period", 60)).replace("the ", "")
    if text in ("all", "ever", "all time", "everything", "always", "any time", "anytime"):
        return date(1900, 1, 1), date(9999, 12, 31), "all the statements"
    if text in ("this year", "year to date", "ytd", "so far this year", "year"):
        return date(today.year, 1, 1), today, f"{today.year} so far"
    if text in ("last year", "previous year"):
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), str(today.year - 1)
    if text in ("past year", "last 12 months", "past 12 months", "last twelve months", "a year"):
        first = _month_start(today, 11)
        return first, today, f"{first.isoformat()} to {today.isoformat()}"
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1), date(int(text), 12, 31), text
    m = re.fullmatch(r"(?:last|past) (\d+|two|three|six) (months?|weeks?|days?)", text)
    if m:
        n = {"two": 2, "three": 3, "six": 6}.get(m[1]) or int(m[1])
        if not 1 <= n <= 1200:
            raise ValueError("that period is too long")
        if m[2].startswith("month"):
            first = _month_start(today, n - 1)
        else:
            first = today - timedelta(days=n * (7 if m[2].startswith("week") else 1) - 1)
        return first, today, f"{first.isoformat()} to {today.isoformat()}"
    if text in ("this week", "week"):
        first = today - timedelta(days=today.weekday())
        return first, today, f"the week from {first.isoformat()}"
    if text in ("last week", "previous week"):
        first = today - timedelta(days=today.weekday() + 7)
        return first, first + timedelta(days=6), f"the week from {first.isoformat()}"
    month = month_words(value, today)
    first = date.fromisoformat(month + "-01")
    return first, _month_end(first), _month_label(month)


def _month_label(month: str) -> str:
    return f"{MONTH_NAMES[int(month[5:7]) - 1]} {month[:4]}"


# ---------------------------------------------------------------- reading a statement file

class FileError(ValueError):
    """A statement file that can not be read, in plain words."""


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            raise FileError("that file is not text this app can read") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _cell(value) -> str:
    return _text(value, "field", NUMBERS["field_chars"], cut=True)


# -- card and account numbers
# A digit is any character Python counts as one: 0-9, full-width and other scripts' digits, superscript and
# circled digits. A join, between two digit groups, is decided by Unicode category, not by a list: every format
# character (Cf: zero-width, bidi marks, soft hyphen), combining or spacing mark (Mn, Me, Mc), space, line or
# paragraph separator (Zs, Zl, Zp), control character (Cc), dash (Pd), unassigned code point (Cn), private-use
# character (Co) and lone surrogate (Cs); the minus signs, middle dots and bullets; the blank-looking letters
# (U+3164, U+115F, U+1160, U+FFA0, U+2800); and the visible joins - . / _ * + # : ~ | ; ' = \ & in their plain and
# full-width forms. Never a comma, which amounts use, and never a bracket, which phone numbers use.
# Each character's class is worked out from the Unicode tables the first time it is seen and then remembered, so
# start-up reads no tables. A text is turned into a class string of the same length ('0' digit, ' ' join, 'x'
# anything else) and the patterns run on that: they never backtrack more than once per character, linear time.
_JOINS = "-./_*+#:~|;'=\\&"
_ALSO_JOINS = "\u3164\u115f\u1160\uffa0\u2800\u2212\u2796\u02d7\u2043\u00b7\u2022\u2027\u2219\u30fb\uff65\u2215\u2044"
_JOIN_CATEGORIES = ("Cf", "Mn", "Me", "Mc", "Zs", "Zl", "Zp", "Cc", "Pd", "Cn", "Co", "Cs")
_STRIPPED = ("Cf", "Cs")                 # dropped from every text on the way in: invisible, or not a character
CLASS_CACHE_MOST = 65536                 # characters remembered; past that each is worked out again


class _Classes(dict):
    """For str.translate: each character as '0' (a digit), ' ' (a join) or 'x' (anything else)."""

    def __missing__(self, code: int) -> str:
        c = chr(code)
        if c.isdigit():
            kind = "0"
        elif unicodedata.category(c) in _JOIN_CATEGORIES or c.isspace() or c in _JOINS or c in _ALSO_JOINS or (
                0xFE10 <= code <= 0xFF6F and len(n := unicodedata.normalize("NFKC", c)) == 1 and n in _JOINS):
            kind = " "
        else:
            kind = "x"
        if len(self) < CLASS_CACHE_MOST:
            self[code] = kind
        return kind


class _Invisible(dict):
    """For str.translate: format characters (Cf) and lone surrogates (Cs) dropped, every other character kept."""

    def __missing__(self, code: int) -> int | None:
        kept = None if unicodedata.category(chr(code)) in _STRIPPED else code
        if len(self) < CLASS_CACHE_MOST:
            self[code] = kept
        return kept


_CLASSES, _INVISIBLE = _Classes(), _Invisible()
_CHAIN = re.compile(r"0+(?: +0+)*")       # on a class string
_GROUP = re.compile(r"0+")


def _strip_invisible(text: str) -> str:
    return text if text.isascii() else text.translate(_INVISIBLE)


_DATE_SEPS = {"/": "/", "\uff0f": "/", "\u2215": "/", "\u2044": "/", ".": ".", "\uff0e": ".", "\ufe52": "."}
NUMBER_DIGITS = (12, 19)               # a card or account number: this many digits, joined by separators or not
UNBROKEN_DIGITS = 8                    # an unbroken run of this many digits or more is cut too


def _ascii_digits(group: str) -> str:
    return group if group.isascii() else "".join(str(unicodedata.digit(c, 0)) for c in group)


def _hidden(digits: str) -> str:
    return "[..." + digits[-4:] + "]"


def _sep_kind(sep: str) -> str | None:
    """'-', '/' or '.' for a separator a date or an amount uses (one character); None for any other."""
    if len(sep) != 1:
        return None
    return _DATE_SEPS.get(sep) or ("-" if unicodedata.category(sep) == "Pd" or sep in "\u2212\u2796\u02d7\u2043"
                                   else None)


def _is_date(groups: list[str], kinds: list[str | None]) -> bool:
    """Whether 3 digit groups with the same separator are a zero-padded date with a four-digit year from 1970 to
    2069: 2026-11-15, 15.11.2026 or 11/15/2026 (or 15/11/2026, 2026/11/15, 2026.11.15). The only thing that ends a
    run of digits."""
    if kinds[0] is None or kinds[0] != kinds[1] or [len(g) for g in groups] not in ([4, 2, 2], [2, 2, 4]):
        return False
    a, b, c = (int(g) for g in groups)
    if len(groups[0]) == 4:
        return 1970 <= a <= 2069 and 1 <= b <= 12 and 1 <= c <= 31
    return 1970 <= c <= 2069 and min(a, b) >= 1 and ((a <= 12 and b <= 31) or (a <= 31 and b <= 12))


def _dates(groups: list[str], seps: list[str]) -> list[bool]:
    """Which digit groups of one chain are a date (see _is_date), read left to right."""
    n = len(groups)
    keep, k = [False] * n, 0
    kinds = [_sep_kind(s) for s in seps]
    while k + 2 < n:
        if kinds[k] and len(groups[k]) in (2, 4) and _is_date(groups[k:k + 3], kinds[k:k + 2]):
            keep[k:k + 3] = [True] * 3
            k += 3
        else:
            k += 1
    return keep


def _cuts(text: str, m: re.Match) -> list[tuple[int, int, str]]:
    """What to hide in one chain (digit groups joined by joins; *m* is its match on the class string), as
    (start, end, [...1234]) in *text*. The rule is plain: the chain is cut only at dates (see _is_date); each
    stretch between them with 12 or more digits in all (amounts and cents included) is hidden whole and shown by
    its last four digits; in a shorter stretch any unbroken run of 8 or more digits keeps its last four. There is
    no other exception: a + before the digits changes nothing."""
    base = m.start()
    spans = [(g.start(), g.end()) for g in _GROUP.finditer(m.string, base, m.end())]
    groups = [_ascii_digits(text[a:b]) for a, b in spans]
    total = sum(map(len, groups))
    if total < UNBROKEN_DIGITS:
        return []
    seps = [text[spans[i][1]:spans[i + 1][0]] for i in range(len(spans) - 1)]
    keep = _dates(groups, seps)
    cuts, i, n = [], 0, len(groups)
    while i < n:
        j = i
        while j < n and not keep[j]:
            j += 1
        if j > i and sum(len(g) for g in groups[i:j]) >= NUMBER_DIGITS[0]:
            cuts.append((spans[i][0], spans[j - 1][1], _hidden("".join(groups[max(i, j - 4):j]))))
        else:                                   # a date or a short stretch: only long unbroken runs go
            cuts += [(spans[x][0], spans[x][1], _hidden(groups[x])) for x in range(i, max(j, i + 1))
                     if len(groups[x]) >= UNBROKEN_DIGITS]
        i = max(j, i + 1)
    return cuts


_COMMA = re.compile("[,\uff0c]")


def _classes(text: str) -> str:
    """The class string of *text*; a comma directly between two digits (1,234.56, 4111,1111) joins too."""
    classes = text.translate(_CLASSES)
    if "," not in text and "\uff0c" not in text:
        return classes
    marks = [m.start() for m in _COMMA.finditer(text)
             if 0 < m.start() < len(text) - 1 and classes[m.start() - 1] == "0" == classes[m.start() + 1]]
    if not marks:
        return classes
    chars = list(classes)
    for at in marks:
        chars[at] = " "
    return "".join(chars)


def _all_cuts(text: str) -> list[tuple[int, int, str]]:
    classes = _classes(text)
    if classes.count("0") < UNBROKEN_DIGITS:   # fewer than 8 digits in all: nothing to cut
        return []
    return [c for m in _CHAIN.finditer(classes) for c in _cuts(text, m)]


def _mask_numbers(text) -> str:
    """Card and account numbers keep only their last four, as [...1234]: 12 to 19 digits joined by any mix of
    spaces, dashes, dots, slashes, underscores and stars (or not joined at all), and any unbroken run of 8 or more
    digits. Dates, amounts, phone numbers and short numbers stay. Linear in the length of the text."""
    text = str(text or "")
    out, cur = [], 0
    for a, b, new in _all_cuts(text):
        out += [text[cur:a], new]
        cur = b
    return "".join(out) + text[cur:] if out else text


def _mask_cells(cells: list[str]) -> list[str]:
    """Each cell masked as if the cells were one text joined by spaces, so a number split across two cells (a
    description and a memo) is still found. A hidden number is shown in the cell it starts in; the part of it in
    the next cell is left out there."""
    joined = " ".join(cells)
    cuts = _all_cuts(joined)
    out, start, k = [], 0, 0
    for cell in cells:
        end = start + len(cell)
        while k < len(cuts) and cuts[k][1] <= start:            # ended before this cell
            k += 1
        parts, cur, x = [], start, k
        while x < len(cuts) and cuts[x][0] < end:                # starts before this cell ends: it overlaps
            a, b, new = cuts[x]
            parts.append(joined[cur:max(a, start)])
            if a >= start:
                parts.append(new)
            cur = min(b, end)
            x += 1
        out.append("".join(parts + [joined[cur:end]]).strip() if x > k else cell)
        start = end + 1
    return out


def clean_file_name(name) -> str:
    """A statement file's name as it is kept and shown: only the part after the last / or \\, and any run of more
    than 4 digits (separators inside it left out) keeps only its last four, so acct_4111111111111111.ofx becomes
    acct_[...1111].ofx."""
    def cut(m):
        digits = "".join(_ascii_digits(name[g.start():g.end()]) for g in _GROUP.finditer(m.string, m.start(),
                                                                                         m.end()))
        return _hidden(digits) if len(digits) > 4 else name[m.start():m.end()]
    name = re.split(r"[\\/]", str(name or ""))[-1]
    out, cur = [], 0
    for m in _CHAIN.finditer(_classes(name)):
        out += [name[cur:m.start()], cut(m)]
        cur = m.end()
    return "".join(out) + name[cur:]


def fingerprint(key: str, bank_id: str) -> str:
    """The bank's own id for a transaction (OFX FITID, which often holds the account number) as a keyed
    fingerprint: HMAC-SHA256 with the ledger's own random key, 16 hex characters. The id itself is never kept."""
    return hmac.new(bytes.fromhex(key), bank_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def file_kind(name: str, text: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    head = text.lstrip()[:4096].upper()
    if ext in (".ofx", ".qfx") or head.startswith("OFXHEADER") or "<OFX>" in head or "<OFX " in head:
        return "ofx"
    if ext == ".qif" or head.startswith("!TYPE") or head.startswith("!ACCOUNT") or head.startswith("!OPTION"):
        return "qif"
    return "csv"


# -- OFX and QFX: a tolerant tag reader of our own, never an XML parser (no entities are ever expanded)
OFX_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9._]{0,40})(?:\s[^<>]{0,2000})?/?>([^<]*)")


def read_ofx(text: str, exp: int = 2) -> dict:
    """Transactions from an OFX or QFX file, SGML form (no closing tags) or XML form. Only the last four
    characters of the account number are kept."""
    start = text.upper().find("<OFX")
    if start < 0:
        raise FileError("that file has no <OFX> part, so it is not an OFX or QFX statement")
    items, skipped, account = [], [], {"last4": "", "kind": "", "currency": ""}
    current: dict | None = None
    in_card = False
    accounts: set[str] = set()

    def flush():
        nonlocal current
        if current is not None:
            items.append(current)
        current = None

    for m in OFX_TOKEN.finditer(text, start):
        closing, tag, value = m.group(1) == "/", m.group(2).upper(), html.unescape(m.group(3)).strip()
        if tag == "STMTTRN":
            flush()
            if not closing:
                current = {}
            continue
        if tag in ("BANKTRANLIST", "STMTRS", "CCSTMTRS") and closing:
            flush()
            continue
        if closing:
            continue
        if tag == "CCACCTFROM":
            in_card = True
        if current is not None:
            if tag in ("TRNTYPE", "DTPOSTED", "DTUSER", "TRNAMT", "FITID", "NAME", "MEMO", "CHECKNUM", "PAYEEID"):
                current.setdefault(tag, value[:NUMBERS["field_chars"]])
        elif tag == "ACCTID" and value:
            accounts.add(value)
            if not account["last4"]:
                account["last4"] = re.sub(r"[^0-9A-Za-z]", "", value)[-4:]
        elif tag == "ACCTTYPE" and value and not account["kind"]:
            account["kind"] = _account_kind(value)[0]
        elif tag == "CURDEF" and re.fullmatch(r"[A-Za-z]{3}", value) and not account["currency"]:
            account["currency"] = value.upper()
    flush()
    if in_card and not account["kind"]:
        account["kind"] = "card"
    if len(accounts) > 1:
        raise FileError("that file holds statements for more than one account; download one account at a time")
    exp = exponent(account["currency"]) if account["currency"] else exp
    out = []
    for n, t in enumerate(items, 1):
        stamp = (t.get("DTPOSTED") or t.get("DTUSER") or "")[:40]
        when = parse_date(stamp[:len(stamp) - len(stamp.lstrip("0123456789"))][:8], "ymd")   # its leading digits
        try:
            amount = parse_amount(t.get("TRNAMT"), exp)
        except ValueError:
            amount = None
        if when is None or amount is None:
            skipped.append({"line": n, "why": "no date" if when is None else "no amount"})
            continue
        name, memo = _cell(t.get("NAME")), _cell(t.get("MEMO"))
        joined = " ".join(x for x in (name, memo if memo and memo != name else "") if x)
        out.append({"date": when.isoformat(), "amount": amount, "fitid": _cell(t.get("FITID"))[:255],
                    "description": _mask_numbers(joined or _cell(t.get("TRNTYPE"))) or "no description",
                    "name": _mask_cells([name, memo])[0], "bank_category": "", "line": n})
    return {"kind": "ofx", "items": out, "skipped": skipped, "account": account, "certain": True, "why": []}


def read_qif(text: str, date_order: str | None, exp: int = 2) -> dict:
    """Transactions from a QIF file: D date, T or U amount, P payee, M memo, L category, each ending with ^."""
    kind, records, rec = "", [], {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("!"):
            if line.lower().startswith("!type:"):
                kind = _account_kind(line[6:])[0]
            continue
        code, value = line[0].upper(), _cell(line[1:])
        if code == "^":
            if rec:
                records.append(rec)
            rec = {}
        elif code in "DTUPML" and code not in rec:
            rec[code] = value
            rec.setdefault("line", n)
    if rec:
        records.append(rec)
    orders, ambiguous = date_orders([r.get("D", "").replace("'", "/").replace(" ", "") for r in records])
    why, certain = [], True
    if date_order in DATE_ORDERS:
        order = date_order
    elif ambiguous:
        order, certain = None, False
        why.append("the dates read two ways (" + " or ".join(DATE_ORDERS[o] for o in orders if o != "ymd")
                   + "); pick the date order")
    else:
        order = orders[0] if orders else "mdy"
    out, skipped = [], []
    for r in records:
        when = parse_date(r.get("D", "").replace("'", "/").replace(" ", ""), order) if order else None
        try:
            amount = parse_amount(r.get("T") or r.get("U"), exp, guess_decimal([r.get("T") or r.get("U")]))
        except ValueError:
            amount = None
        if order and (when is None or amount is None):
            skipped.append({"line": r["line"], "why": "no date" if when is None else "no amount"})
            continue
        payee, memo = r.get("P", ""), r.get("M", "")
        out.append({"date": when.isoformat() if when else "", "amount": amount or 0, "fitid": "",
                    "description": _mask_numbers(" ".join(x for x in (payee, memo if memo != payee else "") if x))
                    or "no description",
                    "name": _mask_cells([payee, memo])[0],
                    "bank_category": _mask_numbers(r.get("L", "").split(":")[0].strip("[]")), "line": r["line"]})
    if not records:
        raise FileError("that QIF file holds no transactions")
    return {"kind": "qif", "items": out if certain else [], "skipped": skipped,
            "account": {"last4": "", "kind": kind, "currency": ""}, "certain": certain, "why": why,
            "date_order": order, "date_orders": orders}


# -- CSV
def _role_of(cell: str) -> tuple[str, int, int] | None:
    """(role, how exact: 0 exact or 1 partial, its place in the word list) for one header cell."""
    words = _norm(cell)
    if not words:
        return None
    for exact in (True, False):
        for role, hints in HEADER_WORDS:
            for i, h in enumerate(hints):
                if words == h if exact else (len(h) > 3 and (words.startswith(h + " ") or words.endswith(" " + h))):
                    return role, 0 if exact else 1, i
    return None


def _sniff(text: str) -> str:
    lines = [ln for ln in text.splitlines()[:60] if ln.strip()]
    best, score = ",", -1
    for d in (",", ";", "\t", "|"):
        try:
            rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=d))
        except csv.Error:
            continue
        counts = [len(r) for r in rows if len(r) > 1]
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if counts.count(common) > score:
            best, score = d, counts.count(common)
    return best


def _csv_rows(text: str, delimiter: str) -> list[list[str]]:
    try:
        rows = []
        for r in csv.reader(io.StringIO(text), delimiter=delimiter):
            rows.append([_cell(c) for c in r[:60]])
            if len(rows) > LIMITS["transactions"]:
                raise FileError(f"that file has more than {LIMITS['transactions']:,} rows")
        return rows
    except csv.Error as exc:
        raise FileError(f"that CSV file will not read ({exc}); a field may be too long") from None


def _header(rows: list[list[str]]) -> tuple[int, dict]:
    """(header row index or -1, {role: column}) from the first rows."""
    for i, row in enumerate(rows[:NUMBERS["header_rows"]]):
        found: dict[str, tuple[int, int, int]] = {}
        for col, cell in enumerate(row):
            got = _role_of(cell)
            if got:
                role, how, rank = got
                if role not in found or (how, rank) < found[role][1:]:
                    found[role] = (col, how, rank)
        if "date" in found and ({"amount", "debit", "credit"} & set(found)) and len(found) >= 2:
            cols, used = {}, set()
            for role in ROLES:
                if role in found and found[role][0] not in used:
                    cols[role] = found[role][0]
                    used.add(found[role][0])
            return i, cols
    return -1, {}


def _content_guess(rows: list[list[str]]) -> dict:
    """With no header line: the date column (most cells read as dates), the amount column (numbers) and the
    description (longest words). Always shown to the owner to confirm."""
    width = max((len(r) for r in rows[:50]), default=0)
    cols = {}
    sample = rows[:50]

    def share(col, test):
        cells = [r[col] for r in sample if col < len(r) and r[col].strip()]
        return sum(1 for c in cells if test(c)) / len(cells) if cells else 0

    def is_amount(c):
        try:
            return parse_amount(c) is not None
        except ValueError:
            return False
    dates = [c for c in range(width) if share(c, lambda v: any(parse_date(v, o) for o in DATE_ORDERS)) > .8]
    if dates:
        cols["date"] = dates[0]
    nums = [c for c in range(width) if c not in dates and share(c, is_amount) > .8]
    if nums:
        cols["amount"] = nums[0]
    words = [c for c in range(width) if c not in dates and c not in nums]
    if words:
        cols["description"] = max(words, key=lambda c: sum(len(r[c]) for r in sample if c < len(r)))
    return cols


def read_csv(text: str, mapping: dict | None, remembered: dict | None, exp: int = 2) -> dict:
    """Transactions from a CSV statement. The columns come from *mapping* (the owner's pick), else from a
    remembered mapping whose header line is the same, else from the header words; the answer says whether
    it is certain."""
    if "\x00" in text:
        raise FileError("that file is not a text statement")
    mapping = dict(mapping or {})
    delimiter = mapping.get("delimiter") if mapping.get("delimiter") in (",", ";", "\t", "|") else _sniff(text)
    rows = _csv_rows(text, delimiter)
    if not [r for r in rows if any(c.strip() for c in r)]:
        raise FileError("that CSV file is empty")
    why, certain = [], True
    head, cols = _header(rows)
    headers = rows[head] if head >= 0 else []
    normed = [_norm(_mask_numbers(c)) for c in headers]
    if remembered and head >= 0 and normed != remembered.get("headers") \
            and [_ascii_norm(c) for c in headers] == remembered.get("headers"):
        normed = [_ascii_norm(c) for c in headers]       # remembered by an older version, accents dropped
    if remembered and head >= 0 and normed == remembered.get("headers"):
        cols = {role: normed.index(name) for role, name in remembered.get("roles", {}).items() if name in normed}
        mapping.setdefault("date_order", remembered.get("date_order"))
        mapping.setdefault("flip", remembered.get("flip", False))
        remembered_used = True
    else:
        remembered_used = False
    if mapping.get("columns"):
        given = mapping["columns"]
        if not isinstance(given, dict):
            raise ValueError("columns is an object of roles and column numbers")
        cols = {}
        for role, col in given.items():
            if role not in ROLES:
                raise ValueError(f"{role!r} is not a column role; the roles are " + ", ".join(ROLES))
            if col in (None, "", -1):
                continue
            if isinstance(col, bool) or not isinstance(col, int) or not 0 <= col < 60:
                raise ValueError("a column is a number from 0")
            cols[role] = col
        if isinstance(mapping.get("header_row"), int) and not isinstance(mapping.get("header_row"), bool) \
                and -1 <= mapping["header_row"] < min(len(rows), NUMBERS["header_rows"]):
            head = mapping["header_row"]
            headers = rows[head] if head >= 0 else []
    elif head < 0:
        cols = _content_guess(rows)
        certain = False
        why.append("the file has no header line this app knows; check which column is which")
    elif not remembered_used:
        if "description" not in cols and "memo" in cols:
            cols["description"] = cols.pop("memo")
        missing = [r for r in ("date", "description") if r not in cols]
        if missing:
            certain = False
            why.append("no column for the " + " and ".join(missing) + " was found; pick it")
    if "amount" in cols:
        cols.pop("debit", None)
        cols.pop("credit", None)
    if "date" not in cols or not ({"amount", "debit", "credit"} & set(cols)):
        certain = False
        if not why:
            why.append("pick the date column and the amount column (or the money out and money in columns)")
    body = [r for r in rows[head + 1:] if any(c.strip() for c in r)]
    cell = lambda r, role: r[cols[role]] if role in cols and cols[role] < len(r) else ""  # noqa: E731
    order = mapping.get("date_order") if mapping.get("date_order") in DATE_ORDERS else None
    orders, ambiguous = date_orders([cell(r, "date") for r in body if cell(r, "date")
                                     and any(parse_date(cell(r, "date"), o) for o in DATE_ORDERS)])
    if order is None and "date" in cols:
        if ambiguous:
            certain = False
            why.append("every date reads both as " + " and as ".join(DATE_ORDERS[o] for o in orders if o != "ymd")
                       + "; pick the date order")
        elif orders:
            order = orders[0]
    money_cols = [r for r in ("amount", "debit", "credit") if r in cols]
    decimal = mapping.get("decimal") if mapping.get("decimal") in (".", ",") else \
        guess_decimal([cell(r, m) for r in body for m in money_cols][:2000])
    flip = _flag(mapping.get("flip"), "flip")
    items, skipped = [], []
    for n, r in enumerate(body, head + 2):
        raw_date = cell(r, "date")
        when = parse_date(raw_date, order) if order else None
        try:
            if "amount" in cols:
                amount = parse_amount(cell(r, "amount"), exp, decimal)
                kind = _norm(cell(r, "type"))
                if amount is not None and kind and not re.search(r"[-(]|cr|dr", cell(r, "amount"), re.I):
                    if kind in ("dr", "debit", "d", "withdrawal", "payment", "purchase", "out"):
                        amount = -abs(amount)
                    elif kind in ("cr", "credit", "c", "deposit", "in"):
                        amount = abs(amount)
            else:
                out_, in_ = parse_amount(cell(r, "debit"), exp, decimal), parse_amount(cell(r, "credit"), exp, decimal)
                amount = None if out_ is None and in_ is None else abs(in_ or 0) - abs(out_ or 0)
        except ValueError:
            amount = None
            if order and when is not None:
                skipped.append({"line": n, "why": "the amount does not read"})
                continue
        if when is None or amount is None:
            if order and (raw_date or amount is not None):
                skipped.append({"line": n, "why": "the date does not read" if when is None else "no amount"})
            continue
        d, mm = cell(r, "description"), cell(r, "memo")       # a number split across the two is found too
        desc = _mask_numbers(" ".join(x for x in (d, mm if mm != d else "") if x))
        items.append({"date": when.isoformat(), "amount": -amount if flip else amount, "fitid": "",
                      "description": desc or "no description", "name": _mask_cells([d, mm])[0],
                      "bank_category": _mask_numbers(cell(r, "category")), "line": n})
    if order and body and len(skipped) * 100 > len(body) * NUMBERS["bad_row_share_pct"] and certain:
        certain = False
        why.append(f"{_plural(len(skipped), 'row')} did not read; check the columns")
    if order and not items and certain:
        certain = False
        why.append("no row read as a transaction; check the columns")
    return {"kind": "csv", "items": items, "skipped": skipped, "certain": certain, "why": why,
            "account": {"last4": "", "kind": "", "currency": ""},
            "csv": {"delimiter": delimiter, "header_row": head, "headers": [_mask_numbers(c) for c in headers],
                    "columns": cols,
                    "date_order": order, "date_orders": orders, "decimal": decimal, "flip": flip,
                    "remembered": remembered_used,
                    "sample": [_mask_cells(r[:20]) for r in rows[max(head, 0):max(head, 0) + 8]]}}


def read_statement(name: str, data: bytes, mapping: dict | None = None, remembered: dict | None = None,
                   exp: int = 2) -> dict:
    if len(data) > NUMBERS["file_bytes"]:
        raise FileError(f"a statement file is at most {NUMBERS['file_bytes'] // (1024 * 1024)} MB")
    text = _decode(data)
    if not text.strip():
        raise FileError("that file is empty")
    kind = file_kind(name, text)
    if kind == "ofx":
        return read_ofx(text, exp)
    if kind == "qif":
        return read_qif(text, (mapping or {}).get("date_order"), exp)
    return read_csv(text, mapping, remembered, exp)


# ---------------------------------------------------------------- payees and categories

PAYEE_NOISE = re.compile(r"(?i)\b(purchase authori[sz]ed on|pos purchase|pos debit|pos|debit card purchase|"
                         r"card purchase|checkcard|check card|recurring payment|recurring|ach debit|ach credit|ach|"
                         r"visa purchase|visa debit|point of sale|contactless|online payment|bill payment|"
                         r"debit purchase|purchase|web pmts?|ppd|ccd)\b")


def _digit_run(word: str, most: int) -> bool:
    """Whether *word* has *most* or more digits in a row."""
    run = 0
    for c in word:
        run = run + 1 if c.isdigit() else 0
        if run >= most:
            return True
    return False


def clean_payee(description: str) -> str:
    description = description[:NUMBERS["field_chars"] * 2]
    t = PAYEE_NOISE.sub(" ", description)
    m = re.match(r"\s*[A-Za-z]{2,4}\s?\*", t)                     # like SQ *CORNER CAFE
    if m and t[m.end():].strip():
        t = t[m.end():].strip()
    t = re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", t)
    t = re.sub(r"[#*]\S*", " ", t)
    t = " ".join(w for w in t.split() if not _digit_run(w, 4))     # a word with 4 or more digits in a row goes
    t = re.sub(r"[^\w&'.,\- ]", " ", t)
    t = re.sub(r"(?<![0-9]),|,(?![0-9])", " ", t)                 # a comma stays only inside 1,234
    t = " ".join(t.split()[:6]).strip(" -.")
    if t.isupper() or t.islower():
        small = {"to", "of", "at", "on", "in", "by", "for", "and", "the", "from", "a"}
        t = " ".join(w.lower() if i and w.lower() in small else w.capitalize() for i, w in enumerate(t.split()))
    return t[:60] or description[:60]


def builtin_guess(text: str, amount: int) -> str:
    hay = _spaced(text)
    for cat, words, sign in BUILTIN_WORDS:
        if (amount > 0) == (sign > 0) and any(f" {w} " in hay for w in words):
            return cat
    return ""


def rule_for(rules: list[dict], text: str) -> dict | None:
    """The owner's rule whose words are in the text; the longest words win."""
    hay = _spaced(text)
    hits = [r for r in rules if _norm(r["words"]) and _spaced(r["words"]) in hay]
    return max(hits, key=lambda r: len(_norm(r["words"]))) if hits else None


def category_lookup(categories: list[str], value) -> tuple[str | None, list[str]]:
    """(the category, or None; the candidates when several match). Exact, then everyday words, then one part."""
    key = _masked_norm(value)
    if not key:
        return None, []
    for c in categories:
        if _norm(c) == key or _norm(c) == key.rstrip("s") or _norm(c).rstrip("s") == key:
            return c, []
    old = _ascii_norm(value)              # a category an older version kept with its accented letters dropped
    if old != key and old in categories:
        return old, []
    alias = CATEGORY_WORDS.get(key)
    if alias and alias in categories:
        _using(alias)
        return alias, []
    hits = [c for c in categories if _selects(key, c, c)]
    if len(hits) == 1:
        _using(hits[0])
        return hits[0], []
    return None, hits


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved ledger exists but will not read. Nothing writes over it."""


class Choose(ValueError):
    """None or several things match: nothing was changed, and the answer lists the candidates."""


FORMAT = 4            # 2: file names cleaned, bank ids kept only as keyed fingerprints, numbers masked again;
                      # 3: masked again with every separator, and what the owner typed (names, notes, rules) too;
                      # 4: masked again with joins by category (invisible characters, + # : and others), invisible
                      #    format characters dropped
REQUIRED_KEYS = ("app", "format", "accounts", "transactions", "categories", "rules", "budgets", "imports",
                 "mappings", "not_transfers")


def default_doc() -> dict:
    return {"app": NAME, "format": FORMAT, "currency": "USD", "accounts": [], "transactions": [],
            "categories": list(STARTER_CATEGORIES), "rules": [], "budgets": [], "imports": [], "mappings": {},
            "not_transfers": [], "fitid_key": secrets.token_hex(32), "last_updated": ""}


def _check_doc(raw) -> dict:
    """The saved ledger, its shape checked. ValueError says what does not fit. A file without this app's marker,
    or without every key this app always writes, is not this app's file and is never written over."""
    if not isinstance(raw, dict) or raw.get("app") != NAME:
        raise ValueError("it is not this app's file: it has no Budget Ledger marker")
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise ValueError("it is not this app's file: it has no " + ", ".join(missing))
    if not isinstance(raw["format"], int) or isinstance(raw["format"], bool) or not 1 <= raw["format"] <= FORMAT:
        raise ValueError(f"its format is not one this version reads (1 to {FORMAT})")
    doc = default_doc()
    doc["format"] = raw["format"]
    key = raw.get("fitid_key")
    if key is not None and not (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)):
        raise ValueError("its fingerprint key does not have the right shape")
    doc["fitid_key"] = key or ""
    for key in ("accounts", "transactions", "categories", "rules", "budgets", "imports", "not_transfers"):
        if not isinstance(raw[key], list):
            raise ValueError(f"{key} must be a list")
        doc[key] = raw[key]
    if not isinstance(raw["mappings"], dict):
        raise ValueError("mappings must be an object")
    doc["mappings"] = raw["mappings"]
    for i in doc["imports"]:
        if not isinstance(i, dict) or not isinstance(i.get("name"), str) or not isinstance(i.get("id"), str):
            raise ValueError("an imported file record does not have the right shape")
    doc["currency"] = _currency(raw.get("currency"), "USD")
    doc["last_updated"] = _text(raw.get("last_updated"), "last_updated", 40)
    for t in doc["transactions"]:
        if not isinstance(t, dict) or not isinstance(t.get("amount"), int) or isinstance(t.get("amount"), bool) \
                or not isinstance(t.get("date"), str) or not isinstance(t.get("id"), str) \
                or not isinstance(t.get("currency"), str) or not isinstance(t.get("description"), str):
            raise ValueError("a transaction does not have the right shape")
    for a in doc["accounts"]:
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not isinstance(a.get("name"), str):
            raise ValueError("an account does not have the right shape")
        if len(str(a.get("last4", ""))) > 4:
            raise ValueError("an account number is longer than its last four")
    if not all(isinstance(c, str) for c in doc["categories"]):
        raise ValueError("categories must be words")
    return doc


def migrate_privacy(doc: dict) -> bool:
    """Bring a ledger saved by an older version up to the privacy rules, once: file names cleaned, every text
    taken from a file or typed by the owner (account names, categories, rule words, notes) masked again, each
    stored bank id replaced by its keyed fingerprint. Transactions point at their account by its id, which never
    changes, so masking an account's name keeps every link. True when it changed anything (the caller saves)."""
    if doc["format"] >= FORMAT and doc["fitid_key"]:
        return False
    if not doc["fitid_key"]:
        doc["fitid_key"] = secrets.token_hex(32)
    for i in doc["imports"]:
        i["name"] = clean_file_name(i["name"])
    for a in doc["accounts"]:
        a["name"] = _mask_numbers(_strip_invisible(a["name"]))
    renamed = {c: _masked_norm(c) or _mask_numbers(c) for c in doc["categories"]}
    doc["categories"] = list(dict.fromkeys(renamed.values()))
    for coll in (doc["transactions"], doc["rules"], doc["budgets"]):
        for x in coll:
            if isinstance(x, dict) and isinstance(x.get("category"), str):
                x["category"] = renamed.get(x["category"], x["category"])
    for r in doc["rules"]:
        if isinstance(r, dict) and isinstance(r.get("words"), str):
            r["words"] = _mask_numbers(_strip_invisible(r["words"]))
    for t in doc["transactions"]:
        for f in ("description", "payee", "note"):
            if isinstance(t.get(f), str):
                t[f] = _mask_numbers(_strip_invisible(t[f]))
        bank_id = t.pop("fitid", "")
        if isinstance(bank_id, str) and bank_id and not t.get("fitid_fp"):
            t["fitid_fp"] = fingerprint(doc["fitid_key"], bank_id)
    for m in doc["mappings"].values():
        if isinstance(m, dict) and isinstance(m.get("headers"), list):
            m["headers"] = [_norm(_mask_numbers(h)) if isinstance(h, str) else h for h in m["headers"]]
            if isinstance(m.get("roles"), dict):
                m["roles"] = {r: _norm(_mask_numbers(h)) if isinstance(h, str) else h for r, h in m["roles"].items()}
    doc["format"] = FORMAT
    return True


class Store:
    """One JSON document with a stamp, kept in memory while the file on disk is unchanged. Callers hold
    ``lock`` for as long as they use what ``load`` gives."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()
        self._cache: tuple[tuple, dict] | None = None

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                self._cache = None
                return default_doc()
            try:
                st = self.path.stat()
                key = (st.st_mtime_ns, st.st_size)
                if self._cache and self._cache[0] == key:
                    return self._cache[1]
                raw = self.path.read_text(encoding="utf-8")
                if not raw.strip():
                    raise ValueError("the file is empty")
                doc = _check_doc(_loads(raw))
            except (OSError, ValueError) as exc:
                self._cache = None
                why = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else str(exc)  # no path
                raise Unreadable(f"The ledger file, {DOC} in Budget Ledger's data folder, will not read ({why}). "
                                 f"Budget Ledger will not write over it. Move that file somewhere else to start a "
                                 f"new ledger, or put a good copy back.") from None
            if migrate_privacy(doc):
                self.save(doc)                 # once: the older copy with full numbers is replaced on disk
            else:
                self._cache = (key, doc)
            return doc

    def save(self, doc: dict) -> str:
        with self.lock:
            doc["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".ledger-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                os.replace(tmp, self.path)
                st = self.path.stat()
                self._cache = ((st.st_mtime_ns, st.st_size), doc)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                self._cache = None                 # what is in memory may be half changed: read the file again
                raise
            return doc["last_updated"]

    def stamp(self) -> str:
        with self.lock:
            return self.load()["last_updated"]


# ---------------------------------------------------------------- the ledger

def _account_kind(value) -> tuple[str, bool]:
    """(kind, whether the words named a kind). Words that name no kind give 'other'."""
    key = _norm(value)
    if key in ACCOUNT_KINDS:
        return key, True
    if key in KIND_WORDS:
        return KIND_WORDS[key], True
    for word, kind in KIND_WORDS.items():
        if word in key.split() or (len(word) > 4 and word in key):
            return kind, True
    return "other", key in ("", "other")


def _excluded(t: dict) -> bool:
    """A confirmed transfer, or one the owner put in transfers: left out of income and spending."""
    return bool(t.get("transfer")) or t.get("category") == "transfers"


class Ledger:
    def __init__(self, store: Store, today=date.today) -> None:
        self.store, self.today = store, today

    # -- finding things by name
    @staticmethod
    def _pick(rows: list[dict], value, what: str, key: str = "name") -> dict:
        raw = _text(value, what, 120)
        if not raw:
            raise Choose(f"say which {what}. The {what}s are: " + (", ".join(r[key] for r in rows) or "none yet") + ".")
        text = _mask_numbers(raw)           # names are kept masked; a number typed in full still finds its name
        low = _norm(text)
        for r in rows:
            if r.get("id") == raw or _norm(r[key]) == low:
                return r
        hits = [r for r in rows if _selects(low, r[key], r[key])]
        if len(hits) == 1:
            _using(hits[0][key])
            return hits[0]
        if hits:
            raise Choose(f"more than one {what} matches {text!r}, so nothing was changed: "
                         + ", ".join(r[key] for r in hits) + ". Say which one.")
        raise Choose(f"no {what} matches {text!r}, so nothing was changed. The {what}s are: "
                     + (", ".join(r[key] for r in rows) or "none yet") + ".")

    def _category(self, doc: dict, value, add: bool = False) -> tuple[str, str]:
        """(category, a note). '' is uncategorized. With *add*, words that match no category add one."""
        text = _typed(value, "category", 40)
        if _norm(text) in UNCATEGORIZED_WORDS:
            return "", ""
        got, hits = category_lookup(doc["categories"], text)
        if got:
            return got, ""
        if hits:
            raise Choose(f"more than one category matches {text!r}, so nothing was changed: {', '.join(hits)}. "
                         f"Say which one.")
        if not add or not _norm(text):
            raise Choose(f"no category matches {text!r}. The categories are: {', '.join(doc['categories'])}.")
        if len(doc["categories"]) >= LIMITS["categories"]:
            raise ValueError(f"there can be at most {LIMITS['categories']} categories")
        name = _masked_norm(text)
        if name in doc["categories"]:
            return name, ""
        doc["categories"].append(name)
        return name, f" {name!r} was not a category yet, so it was added."

    def _account_by_id(self, doc: dict) -> dict:
        return {a["id"]: a for a in doc["accounts"]}

    def _line(self, doc: dict, t: dict, accounts: dict | None = None) -> str:
        accounts = accounts or self._account_by_id(doc)
        acc = accounts.get(t["account"], {}).get("name", "?")
        cat = t.get("category") or "uncategorized"
        flags = " (transfer)" if t.get("transfer") else ""
        desc = t["description"] if _norm(t["description"]) != _norm(t.get("payee")) else ""
        return f"- {t['date']} {money(t['amount'], t['currency'])} {t.get('payee') or t['description']}" \
               f"{' (' + desc[:80] + ')' if desc else ''} [{cat}] {acc}{flags}"

    def _find_one(self, doc: dict, words, day) -> dict:
        """One transaction by words in its payee or description and, if given, its date."""
        said, text = _typed(words, "words", 120), _masked_norm(_text(words, "words", 120))
        when = _day_words(day, self.today()) if day not in (None, "") else None
        if not text and when is None:
            raise Choose("say which transaction: words from its payee and its date")
        hits = [t for t in doc["transactions"]
                if (not text or _selects(text, t.get("payee", "") + " " + t["description"], t.get("payee", ""),
                                         t["description"]))
                and (when is None or t["date"] == when.isoformat())]
        if len(hits) == 1:
            if text and _norm(hits[0].get("payee")) != text and _norm(hits[0]["description"]) != text:
                _using(f"{hits[0]['date']} {hits[0].get('payee') or hits[0]['description']}")
            return hits[0]
        accounts = self._account_by_id(doc)
        if not hits:
            near = [t for t in doc["transactions"] if text and _fits(text, t.get("payee", "") + " " + t["description"])]
            more = (" Transactions with those words: \n" + "\n".join(self._line(doc, t, accounts) for t in
                                                                    sorted(near, key=lambda t: t["date"])[-10:])) \
                if near else ""
            raise Choose(f"no transaction matches {said!r}{' on ' + when.isoformat() if when else ''}, so nothing "
                         f"was changed.{more}")
        hits.sort(key=lambda t: t["date"], reverse=True)
        raise Choose(f"{_plural(len(hits), 'transaction')} match, so nothing was changed. Say the date too:\n"
                     + "\n".join(self._line(doc, t, accounts) for t in hits[:10]))

    # -- accounts
    def account_save(self, account=None, name=None, kind=None, last4=None, currency=None) -> str:
        with self.store.lock:
            doc = self.store.load()
            if account not in (None, ""):
                a = self._pick(doc["accounts"], account, "account")
            else:
                if len(doc["accounts"]) >= LIMITS["accounts"]:
                    raise ValueError(f"there can be at most {LIMITS['accounts']} accounts")
                a = {"id": "a" + secrets.token_hex(4), "name": "", "kind": "other", "last4": "",
                     "currency": doc["currency"]}
                if not _text(name, "name", 60):
                    raise ValueError("a new account needs a name")
            if name not in (None, ""):
                new = _typed(name, "name", 60)
                if any(_norm(x["name"]) == _norm(new) and x is not a for x in doc["accounts"]):
                    raise ValueError(f"there is already an account called {new}")
                a["name"] = new
            if kind not in (None, ""):
                a["kind"] = _account_kind(kind)[0]
            if last4 is not None:
                a["last4"] = re.sub(r"[^0-9A-Za-z]", "", _text(last4, "last four", 60))[-4:]
            if currency not in (None, ""):
                cur = _currency(currency)
                if cur != a["currency"] and any(t["account"] == a["id"] for t in doc["transactions"]):
                    raise ValueError("this account already has transactions in " + a["currency"]
                                     + "; its currency can not change")
                a["currency"] = cur
            if a not in doc["accounts"]:
                doc["accounts"].append(a)
            self.store.save(doc)
        return f"Saved the account {a['name']} ({a['kind']}{', ending ' + a['last4'] if a['last4'] else ''}, " \
               f"{a['currency']})."

    def account_remove(self, account) -> str:
        with self.store.lock:
            doc = self.store.load()
            a = self._pick(doc["accounts"], account, "account")
            n = sum(1 for t in doc["transactions"] if t["account"] == a["id"])
            if n:
                raise ValueError(f"{a['name']} still has {_plural(n, 'transaction')}; remove its imported files "
                                 f"first")
            doc["accounts"] = [x for x in doc["accounts"] if x is not a]
            doc["mappings"].pop(a["id"], None)
            self.store.save(doc)
        return f"Removed the account {a['name']}."

    # -- import
    def _resolve_account(self, doc: dict, account, guess: dict, new: dict | None) -> tuple[dict, bool]:
        """(the account to import into, whether it is new). An existing account by name; otherwise the file's
        last four; otherwise a new account by the given name."""
        raw = _text(account, "account", 60)
        name = _mask_numbers(raw)                 # a new account's name is kept masked
        if new is None:
            if raw:
                return self._pick(doc["accounts"], raw, "account"), False
            same = [a for a in doc["accounts"] if guess.get("last4") and a.get("last4") == guess["last4"]]
            if len(same) == 1:
                return same[0], False
            raise Choose("say which account this file is for: one of " + (", ".join(a["name"] for a in doc["accounts"])
                         or "none yet") + ", or a new account with its name")
        if not name:
            raise ValueError("a new account needs a name")
        if any(_norm(a["name"]) == _norm(name) for a in doc["accounts"]):
            raise ValueError(f"there is already an account called {name}; pick it instead")
        if len(doc["accounts"]) >= LIMITS["accounts"]:
            raise ValueError(f"there can be at most {LIMITS['accounts']} accounts")
        return {"id": "a" + secrets.token_hex(4), "name": name,
                "kind": _account_kind(new.get("kind") or guess.get("kind") or "other")[0],
                "last4": re.sub(r"[^0-9A-Za-z]", "", _text(new.get("last4"), "last four", 60) or guess.get("last4", ""))[-4:],
                "currency": _currency(new.get("currency") or guess.get("currency"), doc["currency"])}, True

    def _plan_import(self, doc: dict, read: dict, account: dict, sha: str) -> dict:
        """Which rows are new and which are already here, and each new one as a transaction."""
        cur = account["currency"]
        have_fitid = {t["fitid_fp"] for t in doc["transactions"] if t["account"] == account["id"] and t.get("fitid_fp")}
        counts: dict[tuple, int] = {}
        for t in doc["transactions"]:
            if t["account"] == account["id"]:
                k = (t["date"], t["amount"], _norm(t["description"]))
                counts[k] = counts.get(k, 0) + 1
        seen: dict[tuple, int] = {}
        new, dup = [], 0
        payee_cats = self._payee_categories(doc)
        for it in read["items"]:
            desc = _mask_numbers(it["description"])
            k = (it["date"], it["amount"], _norm(desc))
            seen[k] = seen.get(k, 0) + 1
            fp = fingerprint(doc["fitid_key"], it["fitid"]) if it["fitid"] else ""
            if (fp and fp in have_fitid) or seen[k] <= counts.get(k, 0):
                dup += 1
                continue
            if fp:
                have_fitid.add(fp)
            payee = clean_payee(_mask_numbers(it.get("name") or "") or desc)
            cat, how = self._choose(doc, payee, desc, it["amount"], it.get("bank_category", ""), payee_cats)
            new.append({"id": "t" + secrets.token_hex(6), "account": account["id"], "date": it["date"],
                        "amount": it["amount"], "currency": cur, "description": desc, "payee": payee,
                        "category": cat, "how": how, "note": "", "transfer": False, "pair": "",
                        "fitid_fp": fp, "source": sha, "import": ""})
        return {"new": new, "duplicates": dup}

    def _payee_categories(self, doc: dict) -> dict:
        """For each payee, the category it has most often."""
        tally: dict[str, dict[str, int]] = {}
        for t in doc["transactions"]:
            if t.get("category") and t.get("payee"):
                d = tally.setdefault(_norm(t["payee"]), {})
                d[t["category"]] = d.get(t["category"], 0) + 1
        return {p: max(d, key=d.get) for p, d in tally.items()}

    def _choose(self, doc: dict, payee: str, desc: str, amount: int, bank: str = "", payee_cats=None) -> tuple:
        """(category, how): the owner's rules, then the bank's own category when it names one of ours, then the
        built-in words; else uncategorized."""
        r = rule_for(doc["rules"], payee + " " + desc)
        if r and r["category"] in doc["categories"]:
            return r["category"], "rule"
        if bank:
            got, _ = category_lookup(doc["categories"], bank)
            if got and _norm(got) in (_norm(bank), CATEGORY_WORDS.get(_norm(bank), "")):
                return got, "bank"
        guess = builtin_guess(payee + " " + desc, amount)
        if guess in doc["categories"]:
            return guess, "words"
        return "", ""

    def _read(self, doc: dict, name: str, data: bytes, account, mapping, new: dict | None) -> tuple[dict, dict | None]:
        """The file read with the account's remembered columns and its currency's decimal places."""
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("mapping is an object")
        acc = None
        name_text = _text(account, "account", 60)
        if name_text and new is None:
            try:
                acc = self._pick(doc["accounts"], name_text, "account")
            except Choose:
                acc = None
        cur = acc["currency"] if acc else _currency((new or {}).get("currency"), doc["currency"])
        remembered = doc["mappings"].get(acc["id"]) if acc else None
        return read_statement(name, data, mapping, remembered, exponent(cur)), acc

    def preview(self, name, data: bytes, account=None, mapping=None, new_account=None) -> dict:
        name = clean_file_name(_text(name, "file name", 200)) or "statement"
        sha = hashlib.sha256(data).hexdigest()
        with self.store.lock:
            doc = self.store.load()
            read, acc = self._read(doc, name, data, account, mapping, new_account)
            guess = read["account"]
            if acc is None and guess.get("last4"):
                same = [a for a in doc["accounts"] if a.get("last4") == guess["last4"]]
                acc = same[0] if len(same) == 1 else None
            plan = self._plan_import(doc, read, acc or {"id": "", "currency": guess.get("currency") or doc["currency"]},
                                     sha) if read["certain"] else {"new": [], "duplicates": 0}
            before = [i for i in doc["imports"] if i["sha256"] == sha]
        cur = (acc or {}).get("currency") or guess.get("currency") or doc["currency"]
        rows = [{"date": t["date"], "amount": t["amount"], "money": money(t["amount"], cur), "payee": t["payee"],
                 "description": t["description"], "category": t["category"]} for t in plan["new"][:NUMBERS["preview_rows"]]]
        if not read["certain"]:
            rows = [{"date": it["date"], "amount": it["amount"], "money": money(it["amount"], cur), "payee": "",
                     "description": it["description"], "category": ""} for it in read["items"][:NUMBERS["preview_rows"]]]
        return {"name": name, "kind": read["kind"], "certain": read["certain"], "why": read["why"],
                "account": acc["name"] if acc else "", "account_guess": guess, "csv": read.get("csv"),
                "date_order": read.get("date_order") or (read.get("csv") or {}).get("date_order"),
                "date_orders": read.get("date_orders") or (read.get("csv") or {}).get("date_orders") or [],
                "count": len(read["items"]), "new": len(plan["new"]), "duplicates": plan["duplicates"],
                "skipped": read["skipped"][:20], "skipped_count": len(read["skipped"]), "rows": rows,
                "imported_before": [i["added"][:10] for i in before], "roles": list(ROLES)}

    def import_file(self, name, data: bytes, account=None, mapping=None, new_account=None) -> dict:
        """Read the file and add what is new, in one write with its import record. Never overwrites."""
        name = clean_file_name(_text(name, "file name", 200)) or "statement"
        sha = hashlib.sha256(data).hexdigest()
        if new_account is not None and not isinstance(new_account, dict):
            raise ValueError("new_account is an object with kind, currency and last4")
        with self.store.lock:
            doc = self.store.load()
            read, _ = self._read(doc, name, data, account, mapping, new_account)
            if not read["certain"]:
                raise Choose("Budget Ledger is not sure how to read this file: " + "; ".join(read["why"])
                             + ". Look at the preview and confirm the columns and the date order.")
            acc, is_new = self._resolve_account(doc, account, read["account"], new_account)
            said_cur = read["account"].get("currency")
            if said_cur and said_cur != acc["currency"]:
                raise ValueError(f"this file is in {said_cur} but {acc['name']} is in {acc['currency']}; currencies "
                                 f"are never mixed, so import it into an account in {said_cur}")
            if is_new:
                doc["accounts"].append(acc)
            plan = self._plan_import(doc, read, acc, sha)
            before = [i for i in doc["imports"] if i["sha256"] == sha]
            c = read.get("csv") or {}
            if c.get("header_row", -1) >= 0 and (acc["id"] in doc["mappings"]
                                                  or len(doc["mappings"]) < LIMITS["mappings"]):
                heads = [_norm(x) for x in c["headers"]]
                doc["mappings"][acc["id"]] = {
                    "headers": heads, "roles": {r: heads[i] for r, i in c["columns"].items() if i < len(heads)},
                    "date_order": c["date_order"], "flip": c["flip"]}
            if not plan["new"]:
                if is_new or read["kind"] == "csv":
                    self.store.save(doc)
                said = f" (this same file was imported on {before[0]['added'][:10]})" if before else ""
                return {"added": 0, "duplicates": plan["duplicates"],
                        "text": f"Nothing new in {name}: all {_plural(plan['duplicates'], 'transaction')} are already "
                                f"in {acc['name']}{said}. Nothing was added."}
            if len(doc["transactions"]) + len(plan["new"]) > LIMITS["transactions"]:
                raise ValueError(f"the ledger holds at most {LIMITS['transactions']:,} transactions")
            if len(doc["imports"]) >= LIMITS["imports"]:
                raise ValueError(f"the ledger keeps at most {LIMITS['imports']} imported files")
            imp = {"id": "f" + secrets.token_hex(4), "name": name, "sha256": sha, "account": acc["id"],
                   "added": _now(), "count": len(plan["new"]), "kind": read["kind"]}
            for t in plan["new"]:
                t["import"] = imp["id"]
                t.pop("how", None)
            doc["imports"].append(imp)
            doc["transactions"].extend(plan["new"])
            self.store.save(doc)                  # the import record and its transactions: one write
        uncat = sum(1 for t in plan["new"] if not t["category"])
        text = (f"Added {_plural(len(plan['new']), 'transaction')} to {acc['name']} from {name}"
                + (f"; {plan['duplicates']} were already here" if plan["duplicates"] else "")
                + (f"; {len(read['skipped'])} rows did not read" if read["skipped"] else "")
                + (f". {uncat} have no category yet" if uncat else "") + ".")
        return {"added": len(plan["new"]), "duplicates": plan["duplicates"], "import": imp["id"], "text": text}

    # -- reading the ledger
    def transactions(self, words=None, month=None, category=None, account=None, min_amount=None, max_amount=None,
                     uncat_first=False) -> tuple[list[dict], dict]:
        today = self.today()
        with self.store.lock:
            doc = self.store.load()
            rows = doc["transactions"]
            text = _masked_norm(_text(words, "words", 120))
            if text:             # whole words or the start of one, as everywhere: "ron" never finds "Sharon"
                rows = [t for t in rows if _fits(text, t.get("payee", "") + " " + t["description"] + " "
                                                 + t.get("note", ""))]
            if month not in (None, ""):
                first, last, _ = period_words(month, today)
                rows = [t for t in rows if first.isoformat() <= t["date"] <= last.isoformat()]
            if category not in (None, ""):
                cat, _ = self._category(doc, category)
                rows = [t for t in rows if t.get("category", "") == cat]
            if account not in (None, ""):
                acc = self._pick(doc["accounts"], account, "account")
                rows = [t for t in rows if t["account"] == acc["id"]]
            lo = _loose_amount(min_amount, 2, "min_amount")
            hi = _loose_amount(max_amount, 2, "max_amount")
            if lo is not None:          # lo and hi have 2 decimal places; compare in whole integers
                rows = [t for t in rows if abs(t["amount"]) * 100 >= abs(lo) * 10 ** exponent(t["currency"])]
            if hi is not None:
                rows = [t for t in rows if abs(t["amount"]) * 100 <= abs(hi) * 10 ** exponent(t["currency"])]
            rows = sorted(rows, key=lambda t: (t["date"], t["id"]), reverse=True)
            if uncat_first:
                rows.sort(key=lambda t: bool(t.get("category")))
            return [dict(t) for t in rows], self._account_by_id(doc)

    def month(self, month: str, doc: dict) -> dict:
        """One month's sums, every currency on its own: income, spending, by category, budgets, top payees,
        and the change from the month before. Integer arithmetic only."""
        prev = _month_start(date.fromisoformat(month + "-01"), 1).isoformat()[:7]
        out: dict[str, dict] = {}

        def fig(cur):
            return out.setdefault(cur, {"income": 0, "spending": 0, "by_category": {}, "payees": {}, "count": 0,
                                        "prev_income": 0, "prev_spending": 0, "prev_by_category": {},
                                        "uncategorized": 0})
        for t in doc["transactions"]:
            m = t["date"][:7]
            if m not in (month, prev) or _excluded(t):
                continue
            f, amt, cat = fig(t["currency"]), t["amount"], t.get("category", "")
            pre = "" if m == month else "prev_"
            if cat == "income" or (cat == "" and amt > 0):
                f[pre + "income"] += amt
            else:
                f[pre + "spending"] -= amt
                key = cat or "uncategorized"
                f[pre + "by_category"][key] = f[pre + "by_category"].get(key, 0) - amt
                if m == month and amt < 0:
                    p = t.get("payee") or t["description"]
                    f["payees"][p] = f["payees"].get(p, 0) - amt
            if m == month:
                f["count"] += 1
                if not cat:
                    f["uncategorized"] += 1
        budgets = self.budgets_for(doc, month)
        for (cat, cur), amount in budgets.items():
            fig(cur)
        for cur, f in out.items():
            lines = []
            cats = set(f["by_category"]) | {c for (c, bc) in budgets if bc == cur}
            for cat in sorted(cats, key=lambda c: -f["by_category"].get(c, 0)):
                spent = f["by_category"].get(cat, 0)
                b = budgets.get((cat, cur))
                lines.append({"category": cat, "spent": spent, "budget": b,
                              "left": None if b is None else b - spent,
                              "over": None if b is None else max(0, spent - b),
                              "before": f["prev_by_category"].get(cat, 0)})
            f["categories"] = lines
            f["net"] = f["income"] - f["spending"]
            f["top_payees"] = sorted(f.pop("payees").items(), key=lambda kv: -kv[1])[:NUMBERS["top_payees"]]
        return {"month": month, "label": _month_label(month), "previous": prev, "currencies": out}

    def budgets_for(self, doc: dict, month: str) -> dict:
        """{(category, currency): amount} for the month: a budget for that month, else the every-month one."""
        got: dict[tuple, int] = {}
        for b in doc["budgets"]:
            if b["month"] == "":
                got.setdefault((b["category"], b["currency"]), b["amount"])
        for b in doc["budgets"]:
            if b["month"] == month:
                got[(b["category"], b["currency"])] = b["amount"]
        return {k: v for k, v in got.items() if v > 0}

    def by_month(self, doc: dict, months: int) -> list[dict]:
        today = self.today()
        keys = [_month_start(today, n).isoformat()[:7] for n in range(months - 1, -1, -1)]
        out = {k: {} for k in keys}
        for t in doc["transactions"]:
            m = t["date"][:7]
            if m in out and not _excluded(t):
                f = out[m].setdefault(t["currency"], {"income": 0, "spending": 0})
                if t.get("category") == "income" or (not t.get("category") and t["amount"] > 0):
                    f["income"] += t["amount"]
                else:
                    f["spending"] -= t["amount"]
        return [{"month": k, "currencies": v} for k, v in out.items()]

    def recurring(self, doc: dict) -> list[dict]:
        """Repeating payments: the same payee and currency, a steady gap and a similar amount."""
        groups: dict[tuple, list[dict]] = {}
        latest = max((t["date"] for t in doc["transactions"]), default="")
        for t in doc["transactions"]:
            if t["amount"] < 0 and not _excluded(t) and _norm(t.get("payee")):
                groups.setdefault((_norm(t["payee"]), t["currency"]), []).append(t)
        found = []
        for (_, cur), rows in groups.items():
            rows.sort(key=lambda t: t["date"])
            days = sorted({date.fromisoformat(t["date"]) for t in rows})
            if len(days) < NUMBERS["recurring_min_yearly"]:
                continue
            gaps = [(b - a).days for a, b in zip(days, days[1:])]
            mid_gap = sorted(gaps)[len(gaps) // 2]
            cad = next((c for c in CADENCES if c[1] <= mid_gap <= c[2]), None)
            if cad is None:
                continue
            need = NUMBERS["recurring_min_yearly"] if cad[0] == "yearly" else NUMBERS["recurring_min"]
            if len(days) < need:
                continue
            if sum(1 for g in gaps if cad[1] <= g <= cad[2]) * 100 < len(gaps) * NUMBERS["steady_share_pct"]:
                continue
            amounts = sorted(t["amount"] for t in rows)
            mid = amounts[len(amounts) // 2]
            close = sum(1 for a in amounts if abs(a - mid) * 100 <= abs(mid) * NUMBERS["similar_pct"])
            if close * 100 < len(amounts) * NUMBERS["steady_share_pct"]:
                continue
            last = days[-1]
            stopped = bool(latest) and (date.fromisoformat(latest) - last).days > cad[2] * NUMBERS["stopped_after_periods"]
            found.append({"payee": rows[-1]["payee"], "currency": cur, "cadence": cad[0], "usual": mid,
                          "times": len(rows), "first": days[0].isoformat(), "last": last.isoformat(),
                          "next": (last + timedelta(days=mid_gap)).isoformat(), "yearly": mid * cad[3],
                          "category": rows[-1].get("category", ""), "maybe_stopped": stopped})
        found.sort(key=lambda r: (r["maybe_stopped"], r["yearly"]))
        return found

    def transfer_pairs(self, doc: dict) -> list[tuple[dict, dict]]:
        """Suggested pairs: same currency, same size, opposite sign, different accounts, at most 3 days apart."""
        no = {tuple(sorted(p)) for p in doc["not_transfers"] if isinstance(p, list) and len(p) == 2}
        ins: dict[tuple, list[dict]] = {}
        for t in doc["transactions"]:
            if t["amount"] > 0 and not t.get("transfer"):
                ins.setdefault((t["currency"], t["amount"]), []).append(t)
        used, pairs = set(), []
        for t in sorted(doc["transactions"], key=lambda t: t["date"]):
            if t["amount"] >= 0 or t.get("transfer"):
                continue
            d = date.fromisoformat(t["date"])
            best = None
            for o in ins.get((t["currency"], -t["amount"]), []):
                gap = abs((date.fromisoformat(o["date"]) - d).days)
                if o["account"] != t["account"] and o["id"] not in used and gap <= NUMBERS["transfer_days"] \
                        and tuple(sorted((t["id"], o["id"]))) not in no and (best is None or gap < best[0]):
                    best = (gap, o)
            if best:
                used.add(best[1]["id"])
                pairs.append((t, best[1]))
        return pairs

    def view(self) -> dict:
        with self.store.lock:
            doc = self.store.load()
            accounts = self._account_by_id(doc)
            counts: dict[str, int] = {}
            for t in doc["transactions"]:
                counts[t["account"]] = counts.get(t["account"], 0) + 1
            months = sorted({t["date"][:7] for t in doc["transactions"]}, reverse=True)
            pairs = [{"out": self._row(a, accounts), "in": self._row(b, accounts)} for a, b in self.transfer_pairs(doc)]
            return {"currency": doc["currency"], "today": self.today().isoformat(), "line": LINE,
                    "accounts": [{**a, "transactions": counts.get(a["id"], 0)} for a in doc["accounts"]],
                    "categories": doc["categories"], "rules": doc["rules"],
                    "budgets": [{**b, "money": money(b["amount"], b["currency"])} for b in doc["budgets"]],
                    "imports": [{**i, "account_name": accounts.get(i["account"], {}).get("name", "")}
                                for i in sorted(doc["imports"], key=lambda i: i["added"], reverse=True)],
                    "months": months, "transfer_pairs": pairs, "count": len(doc["transactions"]),
                    "uncategorized": sum(1 for t in doc["transactions"] if not t.get("category")),
                    "kinds": list(ACCOUNT_KINDS), "date_orders": DATE_ORDERS, "max_file_mb":
                        NUMBERS["file_bytes"] // (1024 * 1024), "last_updated": doc["last_updated"]}

    @staticmethod
    def _row(t: dict, accounts: dict) -> dict:
        return {**t, "money": money(t["amount"], t["currency"]),
                "account_name": accounts.get(t["account"], {}).get("name", "")}

    # -- page changes
    def set_transaction(self, tid, category=None, note=None, transfer=None) -> dict:
        with self.store.lock:
            doc = self.store.load()
            t = next((x for x in doc["transactions"] if x["id"] == tid), None)
            if t is None:
                raise ValueError("that transaction is not in the ledger any more")
            said, extra = [], ""
            if category is not None:
                t["category"], extra = self._category(doc, category, add=True)
                said.append(f"category {t['category'] or 'uncategorized'}")
            if note is not None:
                t["note"] = _typed(note, "note", 300)
                said.append("note")
            if transfer is not None:
                t["transfer"] = _flag(transfer, "transfer")
                if not t["transfer"] and t.get("pair"):
                    other = next((x for x in doc["transactions"] if x["id"] == t["pair"]), None)
                    if other:
                        other["transfer"], other["pair"] = False, ""
                    t["pair"] = ""
                said.append("a transfer" if t["transfer"] else "not a transfer")
            self.store.save(doc)
            payee = t.get("payee") or ""
            offer = bool(category not in (None, "") and t["category"] and payee
                         and not (rule_for(doc["rules"], payee) or {}).get("category") == t["category"])
        return {"text": f"Saved {t['date']} {payee}: {', '.join(said) or 'nothing changed'}.{extra}",
                "offer_rule": {"words": payee, "category": t["category"]} if offer else None}

    def pair(self, out_id, in_id, confirm) -> str:
        with self.store.lock:
            doc = self.store.load()
            a = next((x for x in doc["transactions"] if x["id"] == out_id), None)
            b = next((x for x in doc["transactions"] if x["id"] == in_id), None)
            if not a or not b or a["account"] == b["account"] or a["amount"] != -b["amount"] \
                    or a["currency"] != b["currency"]:
                raise ValueError("those two transactions are not a matching pair")
            if _flag(confirm, "confirm"):
                for x, y in ((a, b), (b, a)):
                    x["transfer"], x["pair"] = True, y["id"]
                said = "Marked as a transfer between your own accounts; left out of spending."
            else:
                if len(doc["not_transfers"]) < LIMITS["not_transfers"]:
                    doc["not_transfers"].append(sorted((a["id"], b["id"])))
                said = "Not a transfer; it will not be suggested again."
            self.store.save(doc)
        return said

    def rule_add(self, words, category) -> str:
        words = _typed(words, "words", 80)
        if not _norm(words):
            raise ValueError("a rule needs the words the payee contains")
        with self.store.lock:
            doc = self.store.load()
            cat, extra = self._category(doc, category, add=True)
            if not cat:
                raise ValueError("a rule needs a category")
            said = self._add_rule(doc, words, cat)
            self.store.save(doc)
        return said + extra

    def _add_rule(self, doc: dict, words: str, cat: str) -> str:
        same = next((r for r in doc["rules"] if _norm(r["words"]) == _norm(words)), None)
        if same:
            same["category"] = cat
            return f"The rule for {words!r} now gives {cat}."
        if len(doc["rules"]) >= LIMITS["rules"]:
            raise ValueError(f"there can be at most {LIMITS['rules']} rules")
        doc["rules"].append({"id": "r" + secrets.token_hex(4), "words": words, "category": cat})
        return f"New rule: a payee with {words!r} goes in {cat}."

    def rule_remove(self, rid) -> str:
        with self.store.lock:
            doc = self.store.load()
            r = self._pick(doc["rules"], rid, "rule", "words")
            doc["rules"] = [x for x in doc["rules"] if x is not r]
            self.store.save(doc)
        return f"Removed the rule for {r['words']!r}."

    def category_save(self, name, rename=None) -> str:
        with self.store.lock:
            doc = self.store.load()
            if rename in (None, ""):
                new = _masked_norm(_text(name, "name", 40))
                if not new or new in UNCATEGORIZED_WORDS:
                    raise ValueError("say a name for the category")
                if new in doc["categories"]:
                    return f"{new} is already a category."
                if len(doc["categories"]) >= LIMITS["categories"]:
                    raise ValueError(f"there can be at most {LIMITS['categories']} categories")
                doc["categories"].append(new)
                said = f"Added the category {new}."
            else:
                old = self._category(doc, name)[0]
                new = _masked_norm(_text(rename, "new name", 40))
                if not old or not new or new in doc["categories"] or new in UNCATEGORIZED_WORDS:
                    raise ValueError("say a new name that is not a category yet")
                doc["categories"] = [new if c == old else c for c in doc["categories"]]
                for coll in (doc["transactions"], doc["rules"], doc["budgets"]):
                    for x in coll:
                        if x.get("category") == old:
                            x["category"] = new
                said = f"Renamed {old} to {new}."
            self.store.save(doc)
        return said

    def category_remove(self, name) -> str:
        with self.store.lock:
            doc = self.store.load()
            cat = self._category(doc, name)[0]
            if not cat:
                raise ValueError("say which category")
            n = 0
            for t in doc["transactions"]:
                if t.get("category") == cat:
                    t["category"], n = "", n + 1
            doc["categories"] = [c for c in doc["categories"] if c != cat]
            doc["rules"] = [r for r in doc["rules"] if r["category"] != cat]
            doc["budgets"] = [b for b in doc["budgets"] if b["category"] != cat]
            self.store.save(doc)
        return f"Removed the category {cat}; {_plural(n, 'transaction')} are uncategorized now."

    def set_currency(self, currency) -> str:
        with self.store.lock:
            doc = self.store.load()
            doc["currency"] = _currency(currency)
            self.store.save(doc)
        return f"New accounts and budgets use {doc['currency']} unless you say otherwise."

    # -- tools
    def month_summary(self, month=None) -> str:
        today = self.today()
        m = month_words(month, today)
        with self.store.lock:
            doc = self.store.load()
            got = self.month(m, doc)
            have = sorted({t["date"][:7] for t in doc["transactions"]})
        cur_figs = got["currencies"]
        if not any(f["count"] for f in cur_figs.values()):
            return (f"There are no transactions for {got['label']}. "
                    + (f"The statements cover {have[0]} to {have[-1]}." if have else
                       "Nothing is imported yet: download a statement file from the bank's website and import it "
                       "on the Budget Ledger page.") + f"\n{LINE}")
        parts = [f"Budget Ledger, {got['label']} ({m}):"]
        for cur, f in sorted(cur_figs.items()):
            parts.append(f"{cur}: income {money(f['income'], cur)}; spending {money(f['spending'], cur)}; "
                         f"{'left over' if f['net'] >= 0 else 'spent more than came in by'} "
                         f"{money(abs(f['net']), cur)}.")
            for c in f["categories"]:
                line = f"- {c['category']}: {money(c['spent'], cur)}"
                if c["budget"] is not None:
                    line += f" of a {money(c['budget'], cur)} budget, " + (
                        f"over by {money(c['over'], cur)}" if c["over"] else f"{money(c['left'], cur)} left")
                parts.append(line)
            diff = f["spending"] - f["prev_spending"]
            parts.append(f"Against {_month_label(got['previous'])}: spending "
                         + ("the same" if not diff else f"{'up' if diff > 0 else 'down'} {money(abs(diff), cur)}")
                         + f" (then {money(f['prev_spending'], cur)}).")
            if f["uncategorized"]:
                parts.append(f"{_plural(f['uncategorized'], 'transaction')} in {cur} have no category yet.")
        return "\n".join(parts) + "\n" + LINE

    def find_transactions(self, words=None, month=None, category=None, account=None, min_amount=None,
                          max_amount=None) -> str:
        rows, accounts = self.transactions(words, month, category, account, min_amount, max_amount)
        if not rows:
            return "No transaction matches that." + ("" if month else " Say a month or a period to look further.")
        totals: dict[str, int] = {}
        for t in rows:
            totals[t["currency"]] = totals.get(t["currency"], 0) + t["amount"]
        with self.store.lock:
            doc = self.store.load()
            lines = [self._line(doc, t, accounts) for t in rows[:NUMBERS["list_rows"]]]
        more = f"\n(and {_plural(len(rows) - len(lines), 'more')}; say a month or more words to narrow it)" \
            if len(rows) > len(lines) else ""
        sums = "; ".join(f"{money(v, k)} net" for k, v in sorted(totals.items()))
        return f"{_plural(len(rows), 'transaction')}, {sums}:\n" + "\n".join(lines) + more + f"\n{LINE}"

    def spending_on(self, what=None, period=None) -> str:
        today = self.today()
        first, last, label = period_words(period, today)
        text = _typed(what, "what", 120)
        if not _norm(text):
            raise ValueError("say a category or a payee, like groceries or Corner Cafe")
        with self.store.lock:
            doc = self.store.load()
            got, hits = category_lookup(doc["categories"], text)
            if _norm(text) in UNCATEGORIZED_WORDS:
                got = ""
            inside = [t for t in doc["transactions"] if first.isoformat() <= t["date"] <= last.isoformat()]
            payee_rows = [t for t in inside if _fits(text, t.get("payee", "") + " " + t["description"])]
            if got is not None:
                rows, said = [t for t in inside if t.get("category", "") == got and not _excluded(t)], \
                    f"the category {got or 'uncategorized'}"
            elif payee_rows:
                rows = [t for t in payee_rows if not t.get("transfer")]
                names = sorted({t.get("payee") or t["description"] for t in rows})
                said = "payees with " + repr(text) + (f" ({', '.join(names[:5])}{', ...' if len(names) > 5 else ''})"
                                                      if names else "")
            elif hits:
                raise Choose(f"more than one category matches {text!r}: {', '.join(hits)}. Say which one.")
            else:
                return (f"Nothing in {label} matches {text!r}, as a category or a payee. The categories are: "
                        f"{', '.join(doc['categories'])}.\n{LINE}")
        if not rows:
            return f"Nothing was spent on {said} in {label}.\n{LINE}"
        totals: dict[str, list[int]] = {}
        sign = 1 if got == "income" else -1          # income is what came in; everything else what went out
        for t in rows:
            x = totals.setdefault(t["currency"], [0, 0])
            x[0] += sign * t["amount"]
            x[1] += 1
        word = "came in" if got == "income" else "spent"
        sums = "; ".join(f"{money(v[0], k)} {word} over {_plural(v[1], 'transaction')}"
                         for k, v in sorted(totals.items()))
        return f"On {said} in {label}: {sums}.\n{LINE}"

    def recurring_payments(self) -> str:
        with self.store.lock:
            found = self.recurring(self.store.load())
        if not found:
            return "No repeating payments were found yet. They show once the same payee is paid a similar amount " \
                   "at a steady gap at least 3 times (twice for yearly ones).\n" + LINE
        lines, totals = [], {}
        for r in found:
            if not r["maybe_stopped"]:
                totals[r["currency"]] = totals.get(r["currency"], 0) + r["yearly"]
            lines.append(f"- {r['payee']}: {r['cadence']}, usually {money(-r['usual'], r['currency'])}, about "
                         f"{money(-r['yearly'], r['currency'])} a year; last {r['last']}"
                         + ("; not seen lately, may have stopped" if r["maybe_stopped"] else f", next about {r['next']}"))
        sums = "; ".join(money(-v, k) for k, v in sorted(totals.items()))
        return f"Repeating payments ({len(found)}):\n" + "\n".join(lines) + \
            (f"\nTogether about {sums} a year." if sums else "") + f"\n{LINE}"

    def uncategorized(self, month=None) -> str:
        today = self.today()
        with self.store.lock:
            doc = self.store.load()
            rows = [t for t in doc["transactions"] if not t.get("category")]
            if month not in (None, ""):
                first, last, _ = period_words(month, today)
                rows = [t for t in rows if first.isoformat() <= t["date"] <= last.isoformat()]
            if not rows:
                return "Every transaction has a category."
            rows.sort(key=lambda t: t["date"], reverse=True)
            known = self._payee_categories(doc)
            accounts = self._account_by_id(doc)
            lines = []
            for t in rows[:NUMBERS["list_rows"]]:
                bare = " ".join(w for w in _norm(t.get("payee")).split() if w not in REFUND_WORDS)
                guess = known.get(_norm(t.get("payee"))) or known.get(bare) or \
                    builtin_guess(t.get("payee", "") + " " + t["description"], t["amount"]) or \
                    builtin_guess(bare, -abs(t["amount"]))
                lines.append(self._line(doc, t, accounts) + f"; best guess: {guess or 'none'}")
        more = f"\n(and {_plural(len(rows) - len(lines), 'more')})" if len(rows) > len(lines) else ""
        return f"{_plural(len(rows), 'transaction')} with no category, newest first:\n" + "\n".join(lines) + more

    def set_category(self, category=None, words=None, date=None, every=None, make_rule=None) -> str:
        every, rule = _flag(every, "every"), _flag(make_rule, "make_rule")
        with self.store.lock:
            doc = self.store.load()
            cat, extra = self._category(doc, category, add=True)
            if every:
                text = _masked_norm(_text(words, "words", 120))
                if not text:
                    raise Choose("say the payee, like Corner Cafe")
                hits = [t for t in doc["transactions"] if _selects(text, t.get("payee", "") + " " + t["description"],
                                                                   t.get("payee", ""), t["description"])]
                if not hits:
                    raise Choose(f"no transaction has {_typed(words, 'words', 120)!r} in its payee, so nothing "
                                 f"was changed.")
                payees = sorted({t.get("payee", "") for t in hits})
                for t in hits:
                    t["category"] = cat
                said = f"Put {_plural(len(hits), 'transaction')} from {', '.join(payees[:5])} in {cat or 'uncategorized'}."
                words_for_rule = _typed(words, "words", 80)
            else:
                t = self._find_one(doc, words, date)
                t["category"] = cat
                said = f"Put {t['date']} {money(t['amount'], t['currency'])} {t.get('payee')} in {cat or 'uncategorized'}."
                words_for_rule = t.get("payee") or _typed(words, "words", 80)
            if rule and cat:
                said += " " + self._add_rule(doc, words_for_rule, cat)
            elif cat and not rule:
                said += f" To do the same for future imports, make it a rule (make_rule yes)."
            self.store.save(doc)
        return said + extra

    def set_budget(self, category=None, amount=None, month=None, currency=None) -> str:
        today = self.today()
        with self.store.lock:
            doc = self.store.load()
            cat, extra = self._category(doc, category, add=True)
            if not cat or cat in ("income", "transfers"):
                raise ValueError("a budget is for a spending category")
            cur = _currency(currency, doc["currency"])
            value = _loose_amount(amount, exponent(cur), "amount")
            if value is None:
                raise ValueError("say the amount, like 600")
            value = abs(value)
            m = "" if month in (None, "", "every month", "each month", "monthly", "all") else month_words(month, today)
            same = next((b for b in doc["budgets"] if b["category"] == cat and b["month"] == m
                         and b["currency"] == cur), None)
            when = f"for {_month_label(m)}" if m else "a month"
            if value == 0:
                if same:
                    doc["budgets"].remove(same)
                said = f"Removed the {cat} budget {when}." if same else f"There was no {cat} budget {when}."
            else:
                if same:
                    same["amount"] = value
                else:
                    if len(doc["budgets"]) >= LIMITS["budgets"]:
                        raise ValueError(f"there can be at most {LIMITS['budgets']} budgets")
                    doc["budgets"].append({"id": "b" + secrets.token_hex(4), "category": cat, "month": m,
                                           "amount": value, "currency": cur})
                said = f"The {cat} budget is {money(value, cur)} {when}."
            self.store.save(doc)
        return said + extra

    def remove_transactions(self, words=None, date=None, file=None) -> str:
        with self.store.lock:
            doc = self.store.load()                    # read first, then remove
            if file not in (None, ""):
                text = _text(file, "file", 200)
                day = re.search(r"\d{4}-\d{2}-\d{2}", text)
                rows = doc["imports"]
                if day:
                    text = text.replace(day.group(0), "").strip()
                    rows = [i for i in rows if i["added"][:10] == day.group(0)] or rows
                by_id = [i for i in doc["imports"] if i["id"] == text]         # the page sends the id
                text = clean_file_name(text)          # names are kept cleaned; a name typed in full still finds it
                if not rows:
                    raise Choose("no statement file has been imported yet, so nothing was removed.")
                same = by_id or [i for i in rows if _norm(i["name"]) == _norm(text)]
                if len(same) > 1:
                    raise Choose("more than one import has that name, so nothing was removed. Say the date too: "
                                 + ", ".join(f"{i['name']} {i['added'][:10]}" for i in same))
                imp = same[0] if same else self._pick(rows, text, "imported file")
                gone = [t for t in doc["transactions"] if t.get("import") == imp["id"]]
                self._drop(doc, {t["id"] for t in gone})
                doc["imports"] = [i for i in doc["imports"] if i is not imp]
                self.store.save(doc)
                return f"Removed {_plural(len(gone), 'transaction')} imported from {imp['name']} on " \
                       f"{imp['added'][:10]}. This can not be undone; import the file again to bring them back."
            t = self._find_one(doc, words, date)
            line = self._line(doc, t)
            self._drop(doc, {t["id"]})
            self.store.save(doc)
        return f"Removed one transaction:\n{line}"

    def remove_by_id(self, tid) -> str:
        with self.store.lock:
            doc = self.store.load()
            t = next((x for x in doc["transactions"] if x["id"] == tid), None)
            if t is None:
                raise ValueError("that transaction is not in the ledger any more")
            line = self._line(doc, t)
            self._drop(doc, {tid})
            self.store.save(doc)
        return f"Removed {line[2:]}."

    @staticmethod
    def _drop(doc: dict, ids: set[str]) -> None:
        for t in doc["transactions"]:
            if t.get("pair") in ids and t["id"] not in ids:
                t["transfer"], t["pair"] = False, ""
        doc["transactions"] = [t for t in doc["transactions"] if t["id"] not in ids]
        left: dict[str, int] = {}
        for t in doc["transactions"]:
            left[t.get("import", "")] = left.get(t.get("import", ""), 0) + 1
        for i in doc["imports"]:
            i["count"] = left.get(i["id"], 0)
        doc["imports"] = [i for i in doc["imports"] if i["count"] > 0]


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_MONTH = "A month in words, like this month, last month, October or 2026-10."
TOOLS = [
    ("month_summary", "When the owner asks how a month went for money, by month in words; no need to look anything "
     "up first. Gives the income, the spending, the spending by category against each budget, and the change from "
     "the month before, each currency on its own.",
     {"month": _p("string", "Optional: " + _MONTH + " Default this month.")}, []),
    ("find_transactions", "When the owner wants to see a list of transactions: finds them by words in the payee, a "
     "month, a category name, an account name and an amount range, with the count; no need to look anything up "
     "first.",
     {"words": _p("string", "Optional: words in the payee or description, like hardware store."),
      "month": _p("string", "Optional: " + _MONTH + " Also this year or last 3 months."),
      "category": _p("string", "Optional: a category name, like groceries, or uncategorized."),
      "account": _p("string", "Optional: an account name, like Checking."),
      "min_amount": _p("string", "Optional: the smallest amount, sign ignored, like 200."),
      "max_amount": _p("string", "Optional: the largest amount, sign ignored.")}, []),
    ("spending_on", "When the owner asks how much went on one thing: the total spent on a category or a payee, by "
     "name, over a period; no need to look it up first.",
     {"what": _p("string", "A category or payee name, like groceries or the electric company."),
      "period": _p("string", "Optional: like this month, last month, October, this year, last year, last 3 months "
                             "or all. Default this month.")}, ["what"]),
    ("recurring_payments", "When the owner asks about subscriptions, regular bills or other repeating payments: lists "
     "each one found in the statements with how often it comes, its usual amount and what it costs a year.", {}, []),
    ("uncategorized", "When the owner asks which transactions still need sorting: lists the transactions with no "
     "category yet, newest first, each with the app's best guess.",
     {"month": _p("string", "Optional: " + _MONTH)}, []),
    ("set_category", "When the owner says which category a payment belongs in, by payee words and its date; no need "
     "to look it up first. Sets one transaction, or with every yes all from that payee; with make_rule yes, future "
     "imports from that payee get it too.",
     {"category": _p("string", "The category name, like eating out."),
      "words": _p("string", "Words from the payee, like Corner Cafe."),
      "date": _p("string", "Optional: the day of the one transaction, like 2026-11-03 or November 3."),
      "every": _p("boolean", "Optional: yes to set every transaction from that payee."),
      "make_rule": _p("boolean", "Optional: yes to make a rule so future imports from that payee get it too.")},
     ["category", "words"]),
    ("set_budget", "When the owner sets or changes a budget for a category, by category name and amount; no need to "
     "look anything up first, this one call saves it. Sets the amount for every month, or for one month only; an "
     "amount of 0 removes it.",
     {"category": _p("string", "The category name, like groceries."),
      "amount": _p("string", "The amount, like 600."),
      "month": _p("string", "Optional: one month only, like December or 2026-12. Default every month."),
      "currency": _p("string", "Optional: a three-letter code like USD. Default the ledger's currency.")},
     ["category", "amount"]),
    ("remove_transactions", "When the owner wants transactions deleted from the ledger: one transaction by payee "
     "words and its date, or everything imported from one statement file by the file's name; no need to look it up "
     "first. This can not be undone.",
     {"words": _p("string", "Words from the payee of one transaction."),
      "date": _p("string", "The day of that one transaction, like 2026-11-03."),
      "file": _p("string", "Or: the name of an imported statement file, like october-checking.csv.")}, []),
]
CHANGES = ["set_category", "set_budget", "remove_transactions"]
DELETES = ["remove_transactions"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Ledger, msg) -> dict | None:
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

    def said(text: str, error: bool) -> dict:        # the answer masked again on its way out: a second guard
        return ok({"content": [{"type": "text", "text": _mask_numbers(text)}], "isError": error})

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
            _USED.said = []
            text = getattr(app, name)(**args)
            return said(" ".join(_USED.said + [text]), False)
        except Unreadable as exc:
            return said(str(exc), True)
        except Choose as exc:
            msg_ = str(exc)
            return said(msg_[:1].upper() + msg_[1:], True)
        except (TypeError, ValueError) as exc:
            return said(f"Those arguments did not fit {name}: {exc}", True)
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return said(f"{name} stopped with an error ({type(exc).__name__}).", True)
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Budget Ledger does not know that method."}}


# --------------------------------------------------------------- HTTP

MAX_UPLOAD = NUMBERS["file_bytes"] * 4 // 3 + 64 * 1024     # a file sent as base64 inside JSON
MAX_DRAIN = MAX_UPLOAD + 1024 * 1024                       # a refused body up to this size is read and dropped
REFUSED_MOST, REFUSED_S = 64 * 1024, 2.0                   # what a request without the token may send before its 401
UPLOAD_ROUTES = ("/api/preview", "/api/import")
PAGE_ROUTES = UPLOAD_ROUTES + ("/api/transaction", "/api/remove", "/api/pair", "/api/rule", "/api/rule-remove",
                               "/api/category", "/api/category-remove", "/api/budget", "/api/account",
                               "/api/account-remove", "/api/settings")
CSV_FIELDS = ("date", "account", "amount", "currency", "payee", "description", "category", "note", "transfer")


_ID_KEYS = frozenset(("id", "account", "pair", "import", "source", "sha256", "fitid_fp", "not_transfers"))
_STAMP_KEYS = frozenset(("date", "last_updated", "added", "exported", "today"))
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?")
_FP = re.compile(r"[0-9a-f]*[a-f][0-9a-f]*")


def _shown(value, key: str = ""):
    """An answer masked again on its way out, a second guard behind the masking on the way in: every text in it,
    except this app's own ids, file hashes, fingerprints and time stamps (which never hold a number)."""
    if isinstance(value, str):
        if key in _ID_KEYS and (ID_RE.fullmatch(value) or SHA_RE.fullmatch(value)
                                or (len(value) == 16 and _FP.fullmatch(value))):
            return value
        if key in _STAMP_KEYS and _STAMP.fullmatch(value):
            return value
        return _mask_numbers(value)
    if isinstance(value, dict):
        return {k: _shown(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shown(v, key) for v in value]
    return value


def _csv_safe(value) -> str:
    """A cell that a spreadsheet will not run as a formula."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not re.fullmatch(r"-?\d+(\.\d+)?", s) else s


def _file_from(body: dict) -> tuple[str, bytes]:
    f = body.get("file")
    if not isinstance(f, dict) or not isinstance(f.get("data"), str):
        raise ValueError("file is {name, data} with the data as base64")
    if len(f["data"]) > NUMBERS["file_bytes"] * 4 // 3 + 8:
        raise ValueError(f"a statement file is at most {NUMBERS['file_bytes'] // (1024 * 1024)} MB")
    try:
        data = base64.b64decode(f["data"], validate=True)
    except ValueError:
        raise ValueError("the file data is not base64") from None
    if len(data) > NUMBERS["file_bytes"]:
        raise ValueError(f"a statement file is at most {NUMBERS['file_bytes'] // (1024 * 1024)} MB")
    name = f.get("name")
    if isinstance(name, str):
        name = re.split(r"[\\/]", name)[-1]          # never a folder, only the file's own name
    return _text(name, "file name", 200, cut=True) or "statement", data


class TooBig(ValueError):
    """A body over the cap. With words: it was read and dropped, answer 400 with them. Without: it was bigger
    than the drain takes, so the connection is closed with no answer."""


class Handler(BaseHTTPRequestHandler):
    server_version = "BudgetLedger/1"
    app: Ledger
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
        self._send(code, json.dumps(_shown(obj), ensure_ascii=False, allow_nan=False).encode("utf-8"), extra=extra)

    def _length(self, most: int) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most *most* bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12 or int(raw) > most:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {most // 1024} KB.")
        return int(raw)

    def _drain(self, signed_in: bool = False) -> bool:
        """Read and drop a body that will be refused, so the sender gets the answer, not a reset connection. A
        request with the token may send up to 256 MB within 20 seconds; one without it, a little over the upload
        cap within 5. True when the whole body was read; False when it was bigger than that (the caller then
        closes without answering)."""
        self.close_connection = True
        most, wait = ((NUMBERS["drain_bytes"], NUMBERS["drain_wait_s"]) if signed_in
                      else (MAX_DRAIN, NUMBERS["body_wait_s"]))
        end = time.monotonic() + wait
        raw = (self.headers.get("Content-Length") or "0").strip()
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return True                        # no length to read by: answer, then close
        try:
            n = self._length(most)
            while n > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.1, end - time.monotonic()))
                got = self.rfile.read1(min(n, 256 * 1024))
                if not got:
                    break
                n -= len(got)
            return True
        except (ValueError, OSError):
            return False
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass

    def _body(self, most: int):
        try:
            n = self._length(most)
        except ValueError:
            if not self._drain(signed_in=True):
                raise TooBig("") from None
            raw = (self.headers.get("Content-Length") or "").strip()
            if most == MAX_UPLOAD and raw.isascii() and raw.isdigit():
                raise TooBig(f"That file is larger than {NUMBERS['file_bytes'] // (1024 * 1024)} MB.") from None
            raise
        raw = bytearray()
        end = time.monotonic() + NUMBERS["body_wait_s"]
        try:
            while len(raw) < n:                      # the whole body within the wait, however it trickles in
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
            raise ValueError(f"The request body did not all arrive within {NUMBERS['body_wait_s']:g} seconds.") from None
        finally:
            self.connection.settimeout(self.timeout)
        if len(raw) != n:
            self.close_connection = True
            raise ValueError("The request ended before its body did.")
        return _loads(bytes(raw)) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode("utf-8"))

    def _refused(self) -> bool:
        """Every route needs the token, checked before anything else is read."""
        if self._token_ok():
            return False
        self.close_connection = True
        raw = (self.headers.get("Content-Length") or "0").strip()
        n = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else REFUSED_MOST
        left_bytes, end = min(n, REFUSED_MOST), time.monotonic() + REFUSED_S     # at most 64 KB, at most 2 s
        try:
            while left_bytes > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.05, min(1.0, end - time.monotonic())))
                got = self.rfile.read1(min(left_bytes, 65536))
                if not got:
                    break
                left_bytes -= len(got)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        self._json(401, {"error": "Budget Ledger needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Budget Ledger stopped on this request with an error "
                                          f"({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        parts = urlsplit(self.path)
        path = parts.path
        try:
            q = {k: v[0] for k, v in parse_qs(parts.query, max_num_fields=20).items()}
        except ValueError:
            return self._json(400, {"error": "Too many query fields."})
        app = self.app
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/updated":
                return self._json(200, {"last_updated": app.store.stamp()})
            if path == "/view":
                return self._json(200, app.view())
            if path == "/transactions":
                limit = int(q["limit"]) if q.get("limit", "").isascii() and q.get("limit", "").isdigit() \
                    and len(q["limit"]) < 7 else 300
                limit = min(limit, NUMBERS["page_rows_most"])
                rows, accounts = app.transactions(q.get("q"), q.get("month"), q.get("category"), q.get("account"),
                                                  q.get("min"), q.get("max"), uncat_first=q.get("uncat") == "1")
                return self._json(200, {"total": len(rows), "rows": [Ledger._row(t, accounts) for t in rows[:limit]]})
            if path == "/month":
                with app.store.lock:
                    doc = app.store.load()
                    m = month_words(q.get("month"), app.today())
                    got = app.month(m, doc)
                    got["by_month"] = app.by_month(doc, NUMBERS["months_shown"])
                for cur, f in got["currencies"].items():
                    f["money"] = {k: money(f[k], cur) for k in ("income", "spending", "net", "prev_spending",
                                                                  "prev_income")}
                    for c in f["categories"]:
                        c["money"] = {k: money(c[k], cur) for k in ("spent", "budget", "left", "over", "before")
                                      if c[k] is not None}
                    f["top_payees"] = [[p, money(v, cur)] for p, v in f["top_payees"]]
                for row in got["by_month"]:
                    for cur, f in row["currencies"].items():
                        f["money"] = {k: money(f[k], cur) for k in ("income", "spending")}
                got["line"] = LINE
                return self._json(200, got)
            if path == "/recurring":
                with app.store.lock:
                    found = app.recurring(app.store.load())
                for r in found:
                    r["money"] = {"usual": money(-r["usual"], r["currency"]), "yearly": money(-r["yearly"], r["currency"])}
                return self._json(200, {"recurring": found, "line": LINE})
            if path == "/export":
                with app.store.lock:
                    doc = dict(app.store.load())
                doc.pop("fitid_key", None)            # the fingerprint key stays in the data folder only
                name = f"budget-ledger-{app.today().isoformat()}.json"
                return self._json(200, {**doc, "exported": _now()},
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/export.csv":
                rows, accounts = app.transactions()
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(CSV_FIELDS)
                for t in rows:
                    exp = exponent(t["currency"])
                    w.writerow([_csv_safe(x) for x in (
                        t["date"], _mask_numbers(accounts.get(t["account"], {}).get("name", "")),
                        f"{'-' if t['amount'] < 0 else ''}{abs(t['amount']) // 10 ** exp}"
                        + (f".{str(abs(t['amount']) % 10 ** exp).zfill(exp)}" if exp else ""),
                        t["currency"], *map(_mask_numbers, (t.get("payee", ""), t["description"],
                                                            t.get("category", ""), t.get("note", ""))),
                        "yes" if t.get("transfer") else "")])
                name = f"budget-ledger-transactions-{app.today().isoformat()}.csv"
                return self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "Budget Ledger has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = urlsplit(self.path).path
        if path == "/mcp":
            return self._mcp()
        if path not in PAGE_ROUTES:
            if self._drain(signed_in=True):
                self._json(404, {"error": "Budget Ledger has no page here."})
            return None
        app = self.app
        try:
            body = self._body(MAX_UPLOAD if path in UPLOAD_ROUTES else NUMBERS["body_bytes"])
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            g = body.get
            if path == "/api/preview":
                name, data = _file_from(body)
                out = app.preview(name, data, g("account"), g("mapping"), g("new_account"))
            elif path == "/api/import":
                name, data = _file_from(body)
                out = app.import_file(name, data, g("account"), g("mapping"), g("new_account"))
            elif path == "/api/transaction":
                out = app.set_transaction(g("id"), g("category"), g("note"), g("transfer"))
            elif path == "/api/remove":
                if g("id"):
                    out = {"text": app.remove_by_id(g("id"))}
                elif g("confirm") is not True:      # the page asks first and sends confirm: true; nothing else does
                    return self._json(400, {"error": "Removing every transaction from an imported file needs "
                                                     "confirm: true after the page has asked. Nothing was removed."})
                else:
                    out = {"text": app.remove_transactions(file=g("import"))}
            elif path == "/api/pair":
                out = {"text": app.pair(g("out"), g("in"), g("confirm"))}
            elif path == "/api/rule":
                out = {"text": app.rule_add(g("words"), g("category"))}
            elif path == "/api/rule-remove":
                out = {"text": app.rule_remove(g("id"))}
            elif path == "/api/category":
                out = {"text": app.category_save(g("name"), g("rename"))}
            elif path == "/api/category-remove":
                out = {"text": app.category_remove(g("name"))}
            elif path == "/api/budget":
                out = {"text": app.set_budget(g("category"), g("amount"), g("month"), g("currency"))}
            elif path == "/api/account":
                out = {"text": app.account_save(g("id"), g("name"), g("kind"), g("last4"), g("currency"))}
            elif path == "/api/account-remove":
                out = {"text": app.account_remove(g("id"))}
            else:
                out = {"text": app.set_currency(g("currency"))}
            out["last_updated"] = app.store.stamp()
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except TooBig as exc:
            if str(exc):
                self._json(400, {"error": str(exc)})
            # else: bigger than the drain takes; close with no answer (close_connection is already set)
        except (TypeError, ValueError) as exc:
            msg = str(exc)
            self._json(400, {"error": msg[:1].upper() + msg[1:]})

    def do_PUT(self):
        if self._refused():
            return
        if self._drain(signed_in=True):
            self._send(405, b"", "text/plain")

    def do_DELETE(self):
        self.do_PUT()

    def _mcp(self):
        try:
            msg = self._body(NUMBERS["body_bytes"])
        except TooBig as exc:
            if not str(exc):
                return None                    # bigger than the drain takes: close with no answer
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None, today=date.today) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Ledger(Store(data), today)
    handler = type("LedgerHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Budget Ledger for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Budget Ledger with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
