"""Client Notes: notes per client for a one-person practice, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``             the page (web/index.html), shown in the Desktop at /pkg/client-notes/
- ``/health``       "ok"
- ``/data``         the saved clients and notes (GET), ``/updated`` its stamp
- ``/view``         the same with open actions, who is due and this week worked out (GET)
- ``/export``       every client, or one (``?id=``), as JSON to download (GET)
- ``/export_text``  every client, or one (``?id=``), as a readable text file (GET)
- ``/api/client``, ``/api/note``, ``/api/action``, ``/api/remove_note``, ``/api/remove_client``,
  ``/api/import``   the page's changes (POST)
- ``/mcp``          the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN. The Desktop reaches the
page through the Core's /pkg/client-notes/, which adds it.

Everything is one JSON document in PUFFPAI_DATA (``clients.json``). Writes replace the whole
file at once (temp file, then rename). A file that exists but will not read, an empty one
included, is never written over. The notes are plain files: this app does not encrypt them.
It stores what the owner types and writes no judgement of its own. How codes, due dates,
private notes, removal and import work: PROCESS.md.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
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
NAME = "client-notes"
VERSION = "1.0.1"
DOC = "clients.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 1024 * 1024
BODY_WAIT_S = 5.0                       # the whole wait for a request body
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,19}")
LIMITS = {"clients": 2000, "notes": 20000, "actions": 50, "tags": 20}
NOT_SEEN_DAYS = 30                      # who_is_due: active clients with no note in this many days
DUE_WITHIN_DAYS = 7                     # who_is_due: next contact dates up to this many days ahead
RECENT_NOTES = 3                        # client_summary: this many latest notes

KINDS = ("session", "call", "email", "visit", "other")
KIND_WORDS = {"session": ("session", "appointment", "meeting", "lesson", "class", "consult", "consultation",
                          "therapy", "tutoring", "coaching", "in person", "face to face", "met"),
              "visit": ("visit", "home visit", "site visit", "house call", "callout", "call out", "job"),
              "call": ("call", "phone", "phone call", "ring", "video", "video call", "zoom", "telephone", "called", "phoned", "rang"),
              "email": ("email", "e-mail", "mail", "message", "text", "sms", "letter", "wrote", "emailed", "texted", "messaged")}
STATUSES = ("active", "paused", "closed")
STATUS_WORDS = {"active": ("active", "open", "current", "ongoing", "seeing"),
                "paused": ("paused", "pause", "on hold", "hold", "waiting", "break"),
                "closed": ("closed", "close", "done", "ended", "finished", "discharged", "inactive", "former")}
TRUE_WORDS = {"true", "yes", "y", "1", "on", "done", "private", "given", "signed", "on file"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "not done", "open", "not private", "none", "not given"}
NOTICE = ("These notes are kept in plain files on this computer and are not encrypted by this app. "
          "Your profession may have rules about keeping client records: check them.")


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


def _whole(value, name: str, lo: int, hi: int) -> int | None:
    """A whole number, loosely given (50, '50', '50 minutes', 'an hour'); None when not given."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} is a number")
    if isinstance(value, (int, float)):
        x = value
    else:
        s = str(value).strip().lower()
        plain = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes?|days?)?", s)
        hours = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)(?:\s*(?:and\s*)?(\d+)\s*(?:m|min|mins|minutes?)?)?", s)
        if s in ("an hour", "one hour", "hour"):
            x = 60
        elif s in ("half an hour", "half hour"):
            x = 30
        elif plain:
            x = float(plain.group(1))
        elif hours:
            x = float(hours.group(1)) * 60 + int(hours.group(2) or 0)
        else:
            raise ValueError(f"{name} is a number")
    if not (math.isfinite(x) and lo <= x <= hi):
        raise ValueError(f"{name} is between {lo} and {hi}")
    return int(round(x))


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _day(value, name: str, today: date, ahead: bool = False) -> date | None:
    """A date: 2027-03-31, today, yesterday, tomorrow, a weekday, in 3 days, in 2 weeks, next week, next month."""
    text = _text(value, name, 40).lower().strip(" .")
    if not text:
        return None
    if text in ("today", "now", "this morning", "this afternoon", "tonight"):
        return today
    if text in ("yesterday", "last night"):
        return today - timedelta(days=1)
    if text == "tomorrow":
        return today + timedelta(days=1)
    m = re.fullmatch(r"in (\d{1,3}|a|one|two|three|four|six) (day|days|week|weeks|month|months)", text)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "six": 6}.get(m.group(1)) or int(m.group(1))
        return today + timedelta(days=n * {"d": 1, "w": 7, "m": 30}[m.group(2)[0]])
    if text in ("next week", "a week"):
        return today + timedelta(days=7)
    if text in ("next month", "a month"):
        return today + timedelta(days=30)
    word = text.removeprefix("next ").removeprefix("on ").removeprefix("last ")
    if word in WEEKDAYS:
        diff = WEEKDAYS.index(word) - today.weekday()
        if ahead:
            return today + timedelta(days=diff % 7 or 7)
        return today - timedelta(days=(-diff) % 7 or 7)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"{name} is a date like 2027-03-31, or words like today, yesterday, tomorrow, "
                         f"friday or in 2 weeks") from None


def _clock(value) -> str:
    """'14:30', '2pm', '2:30 pm', '9' as HH:MM; '' when not given."""
    s = _text(value, "time", 20).lower().replace(".", "")
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        raise ValueError("time is like 14:30 or 2:30 pm")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        raise ValueError("time is like 14:30 or 2:30 pm")
    return f"{h:02d}:{mi:02d}"


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _pick(table: dict, value, default: str) -> tuple[str, bool]:
    """(the key whose words match *value*, whether it matched). Unknown words give *default*."""
    words = " " + " ".join(_words(value)) + " "
    if not words.strip():
        return default, True
    for key, hints in table.items():
        if words.strip() == key or any(f" {h} " in words for h in hints):
            return key, True
    return default, False


def _tags(value) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else re.split(r"[,;\n#]", _text(value, "tags", 1000, True))
    out = []
    for t in items:
        w = _text(t, "tag", 40).lower().strip(" .")
        if w and w not in out:
            out.append(w)
    if len(out) > LIMITS["tags"]:
        raise ValueError(f"at most {LIMITS['tags']} tags")
    return out


# ---------------------------------------------------------------- the shapes

def clean_action(raw, today: date) -> dict:
    """One action: an object (who, what, by, done) or words like 'Alex: send the worksheet by 2026-11-20'."""
    if isinstance(raw, str):
        s = _text(raw, "action", 400)
        who = ""
        m = re.match(r"([A-Za-z][\w .'-]{0,30}):\s+(.+)$", s)
        if m:
            who, s = m.group(1), m.group(2)
        by = ""
        m = re.search(r"\s+by\s+(\S+(?:\s+(?:days?|weeks?|months?|week|month))?)$", s, flags=re.I)
        if m:
            try:
                d = _day(m.group(1), "by", today, ahead=True)
                by, s = (d.isoformat() if d else ""), s[:m.start()]
            except ValueError:
                pass
        raw = {"who": who, "what": s, "by": by}
    if not isinstance(raw, dict):
        raise ValueError("each action is words or an object with who, what and by")
    what = _text(raw.get("what") or raw.get("action") or raw.get("task"), "action", 400)
    if not what:
        raise ValueError("each action needs its words (what)")
    rid = raw.get("id")
    by = raw.get("by") or raw.get("due")
    d = _day(by, "by", today, ahead=True) if by not in (None, "") else None
    done = raw.get("done", False)
    return {"id": rid if isinstance(rid, str) and ID_RE.fullmatch(rid) else secrets.token_hex(4),
            "who": _text(raw.get("who"), "who", 60), "what": what, "by": d.isoformat() if d else "",
            "done": _flag(done, "done") if done not in (None, "") else False,
            "done_on": (_as_date(raw.get("done_on")) or "") and _as_date(raw.get("done_on")).isoformat()}


def _actions(value, today: date) -> list[dict]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        value = [p for p in re.split(r"\n|;", value) if p.strip()]
    if not isinstance(value, list):
        raise ValueError("actions is a list")
    if len(value) > LIMITS["actions"]:
        raise ValueError(f"a note holds at most {LIMITS['actions']} actions")
    return [clean_action(a, today) for a in value]


def clean_client(raw, today: date) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a client is an object")
    name = _text(raw.get("name"), "name", 80)
    if not name:
        raise ValueError("each client needs a name or initials")
    code = _text(raw.get("code"), "code", 20).upper()
    if not CODE_RE.fullmatch(code):
        raise ValueError("a client code is letters, numbers and dashes, like C-0001")
    rid = raw.get("id")
    if not (isinstance(rid, str) and ID_RE.fullmatch(rid)):
        raise ValueError("each client needs an id")
    consent = raw.get("consent") if isinstance(raw.get("consent"), dict) else {}
    on_file = consent.get("on_file", "")
    started = _as_date(raw.get("started"))
    cdate = _as_date(consent.get("date"))
    return {"id": rid, "code": code, "name": name,
            "phone": _text(raw.get("phone"), "phone", 40), "email": _text(raw.get("email"), "email", 120),
            "address": _text(raw.get("address"), "address", 300),
            "started": started.isoformat() if started else "",
            "status": raw.get("status") if raw.get("status") in STATUSES else _pick(STATUS_WORDS, raw.get("status"),
                                                                                   "active")[0],
            "tags": _tags(raw.get("tags")), "about": _text(raw.get("about"), "about", 2000, True),
            "consent": {"on_file": "" if on_file in (None, "") else "yes" if _flag(on_file, "consent") else "no",
                        "date": cdate.isoformat() if cdate else "",
                        "note": _text(consent.get("note"), "consent note", 500, True)},
            "added": _text(raw.get("added"), "added", 40)}


def clean_note(raw, today: date) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a note is an object")
    rid, cid = raw.get("id"), raw.get("client_id")
    if not (isinstance(rid, str) and ID_RE.fullmatch(rid) and isinstance(cid, str) and ID_RE.fullmatch(cid)):
        raise ValueError("each note needs an id and a client_id")
    when = str(raw.get("when") or "")
    d = _as_date(when)
    if d is None:
        raise ValueError("each note needs a date (when)")
    t = when[11:16] if len(when) >= 16 and re.fullmatch(r"\d{2}:\d{2}", when[11:16]) else ""
    nxt = _as_date(raw.get("next_contact"))
    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    private = raw.get("private", False)
    return {"id": rid, "client_id": cid, "when": d.isoformat() + (f"T{t}" if t else ""), "kind": kind,
            "minutes": _whole(raw.get("minutes"), "minutes", 0, 1440) or 0,
            "discussed": _text(raw.get("discussed"), "what was discussed", 8000, True),
            "agreed": _text(raw.get("agreed"), "what was agreed", 4000, True),
            "actions": _actions(raw.get("actions"), today),
            "next_contact": nxt.isoformat() if nxt else "",
            "private": _flag(private, "private") if private not in (None, "") else False,
            "added": _text(raw.get("added"), "added", 40)}


def default_doc() -> dict:
    return {"app": NAME, "format": 1, "clients": [], "notes": [], "last_updated": ""}


FILE_KEYS = {"app", "format", "clients", "notes", "last_updated"}


def is_ours(raw) -> bool:
    """True for a file this app wrote: its marker, format 1, lists of clients and notes, no other top-level keys."""
    return (isinstance(raw, dict) and raw.get("app") == NAME and raw.get("format") == 1
            and isinstance(raw.get("clients"), list) and isinstance(raw.get("notes"), list) and set(raw) <= FILE_KEYS)


def normalize(raw, today: date) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("the client file is a JSON object")
    clients, notes = raw.get("clients", []), raw.get("notes", [])
    if not isinstance(clients, list) or not isinstance(notes, list):
        raise ValueError("clients and notes are lists")
    if len(clients) > LIMITS["clients"] or len(notes) > LIMITS["notes"]:
        raise ValueError(f"at most {LIMITS['clients']} clients and {LIMITS['notes']} notes")
    doc = default_doc()
    doc["clients"] = [clean_client(c, today) for c in clients]
    ids = {c["id"] for c in doc["clients"]}
    doc["notes"] = [clean_note(n, today) for n in notes]
    if any(n["client_id"] not in ids for n in doc["notes"]):
        raise ValueError("a note belongs to a client that is not in the file")
    doc["last_updated"] = _text(raw.get("last_updated"), "last_updated", 40)
    return doc


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved notes exist but will not read. Nothing writes over them."""


class NoClient(ValueError):
    """No client, or more than one, fits what was asked. Its words are the whole answer."""


class Store:
    """One JSON document with a stamp."""

    def __init__(self, folder: Path | str, today) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()
        self.today = today

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
                    raise Unreadable(f"{self.path} is not a Client Notes file: it does not carry the client-notes "
                                     f"marker and format 1 with its lists of clients and notes. Client Notes will not "
                                     f"write over it. Move that file somewhere else to start again, or put a good "
                                     f"copy back.")
                return normalize(data, self.today())
            except (OSError, ValueError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Client Notes will not write over it. "
                                 f"Move that file somewhere else to start again, or put a good copy back.") from None

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".clients-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]


# ------------------------------------------------------------- the logic

def _words(text) -> list[str]:
    """Lower-case words, accents folded (é is e): letters and digits of any script, everything else a gap.
    So 'über' is the one word 'uber', never 'ber'."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c)).casefold().split()


def _who(c: dict) -> str:
    return f"{c['code']} {c['name']}"


class Notes:
    """The practice's clients and notes."""

    def __init__(self, store: Store, today=date.today) -> None:
        self.store, self.today = store, today

    # ---- finding
    @staticmethod
    def client(doc: dict, ident) -> tuple[dict, str]:
        """(client, shown) by code or name, stricter than a search because these are private records:
        (a) the exact code (any case); (b) the exact full name (any case, extra spaces ignored); (c) every word
        given is a whole word of the name, or the start (3 letters or more) of one, and exactly one client fits.
        Never inside a word. *shown* is '' for (a) and (b), and 'Showing <name> (<code>).' for (c).
        None or several: NoClient, which names only the candidates and changes nothing."""
        text = _text(ident, "client", 80)
        if not text:
            raise ValueError("say which client, by name or code")
        rows = doc["clients"]
        bare = re.sub(r"^(?:the\s+|my\s+)?(?:client|code)\s+", "", text, flags=re.I)
        hits = [c for c in rows if c["code"].lower() in (text.lower(), bare.lower())]
        if not hits:
            hits = [c for c in rows if " ".join(c["name"].lower().split()) == text.lower()]
        shown = ""
        if not hits:
            given = [w for w in _words(bare) if w not in ("client", "the", "my", "code")]
            hits = [c for c in rows if given and all(any(w == n or (len(w) >= 3 and n.startswith(w))
                                                         for n in _words(c["name"])) for w in given)]
            shown = f"Showing {hits[0]['name']} ({hits[0]['code']}).\n" if len(hits) == 1 else ""
        if len(hits) == 1:
            return hits[0], shown
        if hits:
            raise NoClient(f"More than one client matches {text!r}: "
                           + "; ".join(f"{c['name']} ({c['code']})" for c in hits[:15])
                           + ". Say which one by code; nothing was shown or changed.")
        raise NoClient(f"No client matches {text!r}. Nothing was shown or changed. Say the client's code or "
                       f"full name.")

    @staticmethod
    def notes_of(doc: dict, c: dict) -> list[dict]:
        return sorted((n for n in doc["notes"] if n["client_id"] == c["id"]), key=lambda n: n["when"], reverse=True)

    def next_code(self, doc: dict) -> str:
        nums = [int(m.group(1)) for c in doc["clients"] if (m := re.fullmatch(r"C-(\d{1,8})", c["code"]))]
        n = max(nums, default=0) + 1
        taken = {c["code"] for c in doc["clients"]}
        while f"C-{n:04d}" in taken:
            n += 1
        return f"C-{n:04d}"

    # ---- worked out
    def next_contact(self, doc: dict, c: dict) -> str:
        """The next contact date written on the client's newest note that has one; '' when none."""
        return next((n["next_contact"] for n in self.notes_of(doc, c) if n["next_contact"]), "")

    def open_actions(self, doc: dict, c: dict | None = None) -> list[tuple[dict, dict, dict]]:
        """(client, note, action) for every open action, soonest due first (no date last)."""
        by_id = {x["id"]: x for x in doc["clients"]}
        out = [(by_id[n["client_id"]], n, a) for n in doc["notes"] if (c is None or n["client_id"] == c["id"])
               for a in n["actions"] if not a["done"]]
        return sorted(out, key=lambda t: (t[2]["by"] or "9999", t[1]["when"]))

    def due(self, doc: dict, days: int = NOT_SEEN_DAYS, within: int = DUE_WITHIN_DAYS) -> dict:
        today = self.today()
        soon, unseen = [], []
        for c in doc["clients"]:
            if c["status"] == "closed":
                continue
            nxt = _as_date(self.next_contact(doc, c))
            if nxt and nxt <= today + timedelta(days=within):
                soon.append((nxt, c))
            if c["status"] == "active":
                notes = self.notes_of(doc, c)
                last = _as_date(notes[0]["when"]) if notes else None
                since = last or _as_date(c["started"]) or _as_date(c["added"])
                if since is None or (today - since).days >= days:
                    unseen.append((last, c))
        soon.sort(key=lambda t: t[0])
        unseen.sort(key=lambda t: (t[0] or date.min, t[1]["code"]))
        return {"soon": soon, "unseen": unseen, "days": days, "within": within}

    def view(self) -> dict:
        today = self.today()
        monday = today - timedelta(days=today.weekday())
        with self.store.lock:
            doc = self.store.load()
            d = self.due(doc)
            for c in doc["clients"]:
                notes = self.notes_of(doc, c)
                c["last_note"] = notes[0]["when"][:10] if notes else ""
                c["notes_count"] = len(notes)
                c["next_contact"] = self.next_contact(doc, c)
        return {**doc, "today": today.isoformat(), "week": [monday.isoformat(), (monday + timedelta(days=6)).isoformat()],
                "next_code": self.next_code(doc), "notice": NOTICE, "kinds": list(KINDS), "statuses": list(STATUSES),
                "due_soon": [c["id"] for _, c in d["soon"]], "not_seen": [c["id"] for _, c in d["unseen"]],
                "not_seen_days": NOT_SEEN_DAYS}

    # ---- words for the bot (a private note's words never go into these)
    def _note_line(self, n: dict) -> str:
        head = f"{n['when'].replace('T', ' ')} {n['kind']}" + (f", {n['minutes']} min" if n["minutes"] else "")
        if n["private"]:
            return f"- {head}: a private note (its words are kept off chat; open it on the page)"
        parts = [f"discussed: {n['discussed']}" if n["discussed"] else "", f"agreed: {n['agreed']}" if n["agreed"] else "",
                 f"next contact {n['next_contact']}" if n["next_contact"] else ""]
        return f"- {head}: " + ("; ".join(p for p in parts if p) or "no words written")

    def _action_line(self, c: dict, n: dict, a: dict, with_client: bool = True) -> str:
        today = self.today().isoformat()
        late = " (overdue)" if a["by"] and a["by"] < today else ""
        who = f"{c['code']} {c['name']}: " if with_client else ""
        if n["private"]:
            return f"- {who}an action in a private note from {n['when'][:10]}" + (f", due {a['by']}{late}" if a["by"] else "")
        return f"- {who}{a['what']}" + (f" (who: {a['who']})" if a["who"] else "") + (f", due {a['by']}{late}" if a["by"] else "")

    # ---- tools
    def find_client(self, words=None, status=None, tag=None) -> str:
        with self.store.lock:
            doc = self.store.load()
        want = [w for w in _words(_text(words, "words", 200)) if w not in ("client", "clients", "the", "my", "all")]
        st = _pick(STATUS_WORDS, status, "")[0] if status not in (None, "", "all", "any") else ""
        tg = _text(tag, "tag", 40).lower()
        hits = []
        for c in doc["clients"]:
            if st and c["status"] != st:
                continue
            if tg and tg not in c["tags"]:
                continue
            if want:
                hay = set(_words(" ".join([c["name"], c["code"], " ".join(c["tags"]), c["status"], c["about"]])))
                hay |= set(_words(c["code"].replace("-", "")))
                if not all(w in hay or w.rstrip("s") in hay for w in want):
                    continue
            hits.append(c)
        if not hits:
            known = "; ".join(_who(c) for c in doc["clients"][:30]) or "none yet"
            return f"No client matches that. The clients are: {known}."
        lines = []
        for c in sorted(hits, key=lambda c: c["code"]):
            notes = self.notes_of(doc, c)
            bits = [c["status"], ", ".join(c["tags"]), f"last note {notes[0]['when'][:10]}" if notes else "no notes yet",
                    f"next contact {self.next_contact(doc, c)}" if self.next_contact(doc, c) else ""]
            lines.append(f"- {_who(c)} (" + "; ".join(b for b in bits if b) + ")")
        return f"{len(hits)} client{'s' if len(hits) != 1 else ''}:\n" + "\n".join(lines[:50])

    def client_summary(self, client) -> str:
        with self.store.lock:
            doc = self.store.load()
        c, shown = self.client(doc, client)
        return shown + self._summary(doc, c)

    def _summary(self, doc: dict, c: dict) -> str:
        notes = self.notes_of(doc, c)
        head = [f"{_who(c)}, {c['status']}" + (f", started {c['started']}" if c["started"] else "")]
        if c["tags"]:
            head.append("Tags: " + ", ".join(c["tags"]))
        if c["about"]:
            head.append("About: " + c["about"])
        con = c["consent"]
        head.append("Consent on file: " + ({"yes": "yes", "no": "no"}.get(con["on_file"], "not recorded"))
                    + (f", {con['date']}" if con["date"] else "") + (f" ({con['note']})" if con["note"] else ""))
        nxt = self.next_contact(doc, c)
        head.append(f"Next contact: {nxt}" if nxt else "Next contact: not set")
        out = "\n".join(head)
        out += f"\n\nLast notes ({len(notes)} in all):\n" + ("\n".join(self._note_line(n) for n in notes[:RECENT_NOTES])
                                                             or "- none yet")
        acts = self.open_actions(doc, c)
        out += "\n\nOpen actions:\n" + ("\n".join(self._action_line(c, n, a, False) for _, n, a in acts) or "- none")
        return out

    def add_client(self, name, code=None, phone=None, email=None, address=None, started=None, status=None, tags=None,
                   about=None, consent=None, consent_date=None, consent_note=None) -> str:
        today = self.today()
        st, known = _pick(STATUS_WORDS, status, "active")
        start = _day(started, "started", today)
        cd = _day(consent_date, "consent date", today)
        with self.store.lock:
            doc = self.store.load()
            if len(doc["clients"]) >= LIMITS["clients"]:
                raise ValueError(f"Client Notes holds at most {LIMITS['clients']} clients")
            code_t = _text(code, "code", 20).upper() or self.next_code(doc)
            if any(c["code"] == code_t for c in doc["clients"]):
                raise ValueError(f"the code {code_t} is taken; leave code out and the app picks the next free one")
            on_file = "" if consent in (None, "") else "yes" if _flag(consent, "consent") else "no"
            c = clean_client({"id": secrets.token_hex(4), "code": code_t, "name": name, "phone": phone, "email": email,
                              "address": address, "started": (start or today).isoformat(), "status": st, "tags": tags,
                              "about": about, "added": today.isoformat(),
                              "consent": {"on_file": on_file, "date": cd.isoformat() if cd else
                                          (today.isoformat() if on_file == "yes" else ""), "note": consent_note}}, today)
            same = [x for x in doc["clients"] if x["name"].lower() == c["name"].lower()]
            doc["clients"].append(c)
            self.store.save(doc)
        out = f"Added {c['name']} as {c['code']} ({c['status']}, started {c['started']}"
        out += f", consent on file: {c['consent']['on_file']})." if c["consent"]["on_file"] else ", consent not recorded)."
        if not known:
            out += f" The status {status!r} is not one of active, paused or closed, so it is active."
        if same:
            out += f" Another client is also called {c['name']} ({', '.join(x['code'] for x in same)}): use the codes."
        return out + f" Use the code {c['code']} to talk about this client by code."

    def add_session_note(self, client, discussed=None, agreed=None, kind=None, date=None, time=None, minutes=None,
                         actions=None, next_contact=None, private=None) -> str:
        today = self.today()
        when_text = _text(date, "date", 40)
        clock = ""
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})", when_text)
        if m:
            when_text, clock = m.group(1), m.group(2)
        day = _day(when_text, "date", today) or today
        if day > today:
            raise ValueError("a note's date can not be a day still to come; put a planned date in next_contact")
        clock = _clock(time) or (_clock(clock) if clock else "")
        k, known = _pick(KIND_WORDS, kind, "session")
        if kind in (None, ""):
            k, known = "session", True
        elif not known:
            k = "other"
        nxt = _day(next_contact, "next contact", today, ahead=True)
        with self.store.lock:
            doc = self.store.load()
            c, shown = self.client(doc, client)
            if len(doc["notes"]) >= LIMITS["notes"]:
                raise ValueError(f"Client Notes holds at most {LIMITS['notes']} notes")
            n = clean_note({"id": secrets.token_hex(4), "client_id": c["id"],
                            "when": day.isoformat() + (f"T{clock}" if clock else ""), "kind": k, "minutes": minutes,
                            "discussed": discussed, "agreed": agreed, "actions": _actions(actions, today),
                            "next_contact": nxt.isoformat() if nxt else "", "private": private,
                            "added": _now()}, today)
            if not (n["discussed"] or n["agreed"] or n["actions"] or n["next_contact"]):
                raise ValueError("say what was discussed, what was agreed, an action or the next contact date")
            doc["notes"].append(n)
            self.store.save(doc)
        out = (f"{shown}Saved a {n['kind']} note for {_who(c)} on {n['when'].replace('T', ' ')}"
               + (f", {n['minutes']} min" if n["minutes"] else "") + (", private" if n["private"] else "") + ".")
        if n["actions"]:
            out += f" Actions: {len(n['actions'])}" + (f" ({'; '.join(a['what'] for a in n['actions'])})"
                                                       if not n["private"] else "") + "."
        if n["next_contact"]:
            out += f" Next contact: {n['next_contact']}."
        if not known:
            out += f" The kind {kind!r} is not one of {', '.join(KINDS)}, so it is other."
        return out

    def list_open_actions(self, client=None) -> str:
        with self.store.lock:
            doc = self.store.load()
        c, shown = self.client(doc, client) if client not in (None, "", "all", "everyone") else (None, "")
        acts = self.open_actions(doc, c)
        if not acts:
            return shown + "No open actions" + (f" for {_who(c)}." if c else ".")
        return (f"{shown}Open actions{' for ' + _who(c) if c else ''}, soonest due first:\n"
                + "\n".join(self._action_line(x, n, a, c is None) for x, n, a in acts))

    def mark_action_done(self, action, client=None, done=None) -> str:
        flag = True if done in (None, "") else _flag(done, "done")
        text = _text(action, "action", 400)
        if not text:
            raise ValueError("say which action, by its words")
        with self.store.lock:
            doc = self.store.load()
            c, shown = self.client(doc, client) if client not in (None, "") else (None, "")
            rows =[(x, n, a) for n in doc["notes"] if (c is None or n["client_id"] == c["id"]) and not n["private"]
                    for x in [next(y for y in doc["clients"] if y["id"] == n["client_id"])]
                    for a in n["actions"] if a["done"] != flag]
            low = text.lower()
            hits = [r for r in rows if r[2]["id"] == text or r[2]["what"].lower() == low]
            if not hits:
                words = [w for w in _words(text) if w not in ("the", "a", "action", "done", "for", "to")]
                hits = [r for r in rows if words and all(w in _words(r[2]["what"] + " " + r[2]["who"]) or
                                                         w.rstrip("s") in _words(r[2]["what"]) for w in words)]
            if len(hits) != 1:
                if hits:
                    raise ValueError(f"more than one {'open' if flag else 'done'} action matches {text!r}: "
                                     + "; ".join(f"{x['code']}: {a['what']}" for x, _, a in hits[:15])
                                     + ". Say more of its words or the client; nothing was changed")
                known = "; ".join(f"{x['code']}: {a['what']}" for x, _, a in rows[:20]) or "none"
                raise ValueError(f"no {'open' if flag else 'done'} action matches {text!r}. They are: {known}")
            x, n, a = hits[0]
            a["done"] = flag
            a["done_on"] = self.today().isoformat() if flag else ""
            self.store.save(doc)
        return f"{shown}{'Done' if flag else 'Open again'}: {a['what']} ({_who(x)})."

    def who_is_due(self, days=None, within=None) -> str:
        n = _whole(days, "days", 1, 3650) or NOT_SEEN_DAYS
        w = _whole(within, "within", 0, 365)
        w = DUE_WITHIN_DAYS if w is None else w
        with self.store.lock:
            doc = self.store.load()
        d = self.due(doc, n, w)
        today = self.today()
        lines = [f"Next contact due in the next {w} days or overdue:"]
        lines += [f"- {_who(c)}: {nxt.isoformat()}" + (" (overdue)" if nxt < today else " (today)" if nxt == today
                                                       else "") for nxt, c in d["soon"]] or ["- none"]
        lines += [f"Active clients with no note in {n} days or more:"]
        lines += [f"- {_who(c)}: " + (f"last note {last.isoformat()}, {(today - last).days} days ago" if last
                                      else "no notes yet") for last, c in d["unseen"]] or ["- none"]
        return "\n".join(lines)

    def remove_note_or_client(self, client, note=None, confirm=None) -> str:
        with self.store.lock:
            doc = self.store.load()                      # read before anything is removed
            c, shown = self.client(doc, client)
            return shown + self._remove(doc, c, note, confirm)

    def _remove(self, doc: dict, c: dict, note=None, confirm=None) -> str:
        with self.store.lock:
            notes = self.notes_of(doc, c)
            if note not in (None, ""):
                n = self._one_note(c, notes, note)
                doc["notes"] = [x for x in doc["notes"] if x is not n]
                self.store.save(doc)
                return f"Removed the {n['kind']} note of {n['when'].replace('T', ' ')} for {_who(c)}."
            given = confirm.strip() if isinstance(confirm, str) else ""      # spaces around it only; else exact
            if notes and not hmac.compare_digest(given.encode("utf-8"), c["code"].encode("utf-8")):
                raise ValueError(f"{_who(c)} has {len(notes)} note{'s' if len(notes) != 1 else ''}. Nothing was "
                                 f"removed. To remove one note, say which (its date, or latest). To remove the client "
                                 f"and every note, export the record first, then send confirm as the exact client "
                                 f"code, {c['code']}")
            doc["clients"] = [x for x in doc["clients"] if x is not c]
            doc["notes"] = [x for x in doc["notes"] if x["client_id"] != c["id"]]
            self.store.save(doc)
        return f"Removed the client {_who(c)}" + (f" and {len(notes)} note{'s' if len(notes) != 1 else ''}." if notes
                                                  else ".")

    def _one_note(self, c: dict, notes: list[dict], which) -> dict:
        text = _text(which, "note", 200)
        low = text.lower()
        if low in ("latest", "last", "newest", "the latest", "the last one", "most recent"):
            if not notes:
                raise ValueError(f"{_who(c)} has no notes")
            return notes[0]
        hits = [n for n in notes if n["id"] == text or n["when"] == text or n["when"].replace("T", " ") == text]
        if not hits:
            try:
                d = _day(text, "note", self.today())
                hits = [n for n in notes if n["when"][:10] == d.isoformat()]
            except ValueError:
                words = _words(text)
                hits = [n for n in notes if not n["private"] and words and
                        all(w in _words(n["discussed"] + " " + n["agreed"]) for w in words)]
        if len(hits) == 1:
            return hits[0]
        listed = "; ".join(f"{n['when'].replace('T', ' ')} {n['kind']}" for n in (hits or notes)[:15]) or "none"
        raise ValueError(("more than one note matches" if hits else "no note matches") + f" {text!r} for {_who(c)}. "
                         f"Its notes: {listed}. Nothing was removed")

    # ---- the page
    def save_client(self, body) -> str:
        if not isinstance(body, dict):
            raise ValueError("send the client as an object")
        today = self.today()
        with self.store.lock:
            doc = self.store.load()
            rid = body.get("id")
            old = next((c for c in doc["clients"] if c["id"] == rid), None) if rid else None
            if rid and old is None:
                raise ValueError("that client is not here any more")
            code = _text(body.get("code"), "code", 20).upper() or (old["code"] if old else self.next_code(doc))
            c = clean_client({**body, "id": old["id"] if old else secrets.token_hex(4), "code": code,
                              "added": old["added"] if old else today.isoformat(),
                              "started": body.get("started") or (old["started"] if old else today.isoformat())}, today)
            if any(x["code"] == c["code"] and x is not old for x in doc["clients"]):
                raise ValueError(f"the code {c['code']} is taken")
            if old:
                old.clear()
                old.update(c)
            else:
                if len(doc["clients"]) >= LIMITS["clients"]:
                    raise ValueError(f"Client Notes holds at most {LIMITS['clients']} clients")
                doc["clients"].append(c)
            self.store.save(doc)
        return f"Saved {_who(c)}."

    def save_note(self, body) -> str:
        if not isinstance(body, dict):
            raise ValueError("send the note as an object")
        today = self.today()
        with self.store.lock:
            doc = self.store.load()
            c = next((x for x in doc["clients"] if x["id"] == body.get("client_id")), None)
            if c is None:
                raise ValueError("that client is not here any more")
            rid = body.get("id")
            old = next((n for n in doc["notes"] if n["id"] == rid), None) if rid else None
            if rid and old is None:
                raise ValueError("that note is not here any more")
            d = _day(body.get("date"), "date", today) or today
            if d > today:
                raise ValueError("a note's date can not be a day still to come")
            clock = _clock(body.get("time"))
            nxt = _day(body.get("next_contact"), "next contact", today, ahead=True)
            n = clean_note({**body, "id": old["id"] if old else secrets.token_hex(4), "client_id": c["id"],
                            "when": d.isoformat() + (f"T{clock}" if clock else ""),
                            "kind": _pick(KIND_WORDS, body.get("kind"), "other")[0],
                            "actions": _actions(body.get("actions"), today),
                            "next_contact": nxt.isoformat() if nxt else "",
                            "added": old["added"] if old else _now()}, today)
            if old:
                old.clear()
                old.update(n)
            else:
                if len(doc["notes"]) >= LIMITS["notes"]:
                    raise ValueError(f"Client Notes holds at most {LIMITS['notes']} notes")
                doc["notes"].append(n)
            self.store.save(doc)
        return f"Saved the note for {_who(c)}."

    def set_action(self, note_id, action_id, done) -> str:
        flag = _flag(done, "done")
        with self.store.lock:
            doc = self.store.load()
            n = next((x for x in doc["notes"] if x["id"] == note_id), None)
            a = next((x for x in n["actions"] if x["id"] == action_id), None) if n else None
            if a is None:
                raise ValueError("that action is not here any more")
            a["done"], a["done_on"] = flag, self.today().isoformat() if flag else ""
            self.store.save(doc)
        return f"{'Done' if flag else 'Open again'}: {a['what']}."

    def remove_note_by_id(self, note_id) -> str:
        with self.store.lock:
            doc = self.store.load()
            n = next((x for x in doc["notes"] if x["id"] == note_id), None)
            if n is None:
                raise ValueError("that note is not here any more")
            doc["notes"] = [x for x in doc["notes"] if x is not n]
            self.store.save(doc)
        return f"Removed the note of {n['when'].replace('T', ' ')}."

    def remove_client_by_id(self, client_id, confirm) -> str:
        with self.store.lock:
            doc = self.store.load()
            c = next((x for x in doc["clients"] if x["id"] == client_id), None)
            if c is None:
                raise ValueError("that client is not here any more")
            return self._remove(doc, c, None, confirm)

    def export(self, client_id: str | None = None) -> dict:
        with self.store.lock:
            doc = self.store.load()
        if client_id:
            c = next((x for x in doc["clients"] if x["id"] == client_id), None)
            if c is None:
                raise LookupError
            doc = {**doc, "clients": [c], "notes": [n for n in doc["notes"] if n["client_id"] == c["id"]]}
        return {**doc, "exported": _now(), "notice": NOTICE}

    def export_text(self, client_id: str | None = None) -> str:
        doc = self.export(client_id)
        out = [f"Client Notes export, {self.today().isoformat()}", NOTICE, ""]
        for c in sorted(doc["clients"], key=lambda c: c["code"]):
            out += ["=" * 60, _who(c), f"Status: {c['status']}. Started: {c['started'] or 'not given'}."]
            for k, label in (("phone", "Phone"), ("email", "Email"), ("address", "Address")):
                if c[k]:
                    out.append(f"{label}: {c[k]}")
            if c["tags"]:
                out.append("Tags: " + ", ".join(c["tags"]))
            con = c["consent"]
            out.append("Consent on file: " + (con["on_file"] or "not recorded") + (f", {con['date']}" if con["date"] else "")
                       + (f". {con['note']}" if con["note"] else ""))
            if c["about"]:
                out += ["About:", c["about"]]
            for n in self.notes_of(doc, c):
                out += ["", f"--- {n['when'].replace('T', ' ')}, {n['kind']}" + (f", {n['minutes']} min" if n["minutes"]
                                                                                else "")
                        + (", private" if n["private"] else "")]
                if n["discussed"]:
                    out += ["Discussed:", n["discussed"]]
                if n["agreed"]:
                    out += ["Agreed:", n["agreed"]]
                for a in n["actions"]:
                    out.append(f"[{'x' if a['done'] else ' '}] {a['what']}" + (f" (who: {a['who']})" if a["who"] else "")
                               + (f", by {a['by']}" if a["by"] else "") + (f", done {a['done_on']}" if a["done_on"] else ""))
                if n["next_contact"]:
                    out.append(f"Next contact: {n['next_contact']}")
            out.append("")
        return "\n".join(out) + "\n"

    def import_doc(self, data) -> str:
        """This app's export. Never overwrites: a client already here (same id) is skipped with its notes; a code
        already taken by another client gets the next free code; a note already here (same id) is skipped."""
        today = self.today()
        if not isinstance(data, dict):
            raise ValueError("that file is not a Client Notes export")
        incoming = normalize({"clients": data.get("clients", []), "notes": data.get("notes", [])}, today)
        with self.store.lock:
            doc = self.store.load()                      # a file that will not read is never replaced
            ids = {c["id"] for c in doc["clients"]}
            note_ids = {n["id"] for n in doc["notes"]}
            added, skipped, recoded, notes = [], [], [], 0
            for c in incoming["clients"]:
                if c["id"] in ids:
                    skipped.append(_who(c))
                    continue
                if any(x["code"] == c["code"] for x in doc["clients"]):
                    old = c["code"]
                    c["code"] = self.next_code(doc)
                    recoded.append(f"{old} is now {c['code']}")
                if len(doc["clients"]) >= LIMITS["clients"]:
                    raise ValueError(f"Client Notes holds at most {LIMITS['clients']} clients; nothing was imported")
                doc["clients"].append(c)
                added.append(_who(c))
                for n in incoming["notes"]:
                    if n["client_id"] == c["id"] and n["id"] not in note_ids:
                        doc["notes"].append(n)
                        note_ids.add(n["id"])
                        notes += 1
            if len(doc["notes"]) > LIMITS["notes"]:
                raise ValueError(f"Client Notes holds at most {LIMITS['notes']} notes; nothing was imported")
            if added:
                self.store.save(doc)
        return (f"Imported {len(added)} client{'s' if len(added) != 1 else ''} with {notes} note{'s' if notes != 1 else ''}."
                + (f" Codes changed because they were taken: {'; '.join(recoded)}." if recoded else "")
                + (f" Skipped {len(skipped)} already here (nothing is overwritten): {'; '.join(skipped[:20])}."
                   if skipped else ""))


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_CLIENT = _p("string", "The client's code (like C-0001) or name.")
TOOLS = [
    ("find_client", "Use to look up which clients there are: by words in a name, code, tag or the about note, "
     "or by status (active, paused, closed). It lists each with the last note and next contact date.",
     {"words": _p("string", "Optional: words to look for, like a name, initials or a tag."),
      "status": _p("string", "Optional: active, paused or closed."),
      "tag": _p("string", "Optional: one tag.")}, []),
    ("client_summary", "Use to catch up on one client, by name or code; no need to look it up first: the "
     "latest notes, open actions, next contact date and consent on file.",
     {"client": _CLIENT}, ["client"]),
    ("add_client", "Use when the owner takes on a new client: a display name or initials, and if given the "
     "contact details, start date, status, tags, a short about note and consent on file. The app gives a "
     "private code like C-0001.",
     {"name": _p("string", "A display name or initials."),
      "code": _p("string", "Optional: a code to use. Default the next free one, like C-0004."),
      "phone": _p("string", "Optional."), "email": _p("string", "Optional."), "address": _p("string", "Optional."),
      "started": _p("string", "Optional: the start date, like 2027-03-31. Default today."),
      "status": _p("string", "Optional: active, paused or closed. Default active."),
      "tags": _p("string", "Optional: tags, comma separated."),
      "about": _p("string", "Optional: a short about note."),
      "consent": _p("boolean", "Optional: true when consent is on file, false when not."),
      "consent_date": _p("string", "Optional: the date of the consent."),
      "consent_note": _p("string", "Optional: words about the consent.")}, ["name"]),
    ("add_session_note", "Use to write down a session, call, email or visit with a client, by name or code; "
     "no need to look it up first: what was discussed, what was agreed, actions, the length and the next "
     "contact date.",
     {"client": _CLIENT,
      "discussed": _p("string", "What was discussed."),
      "agreed": _p("string", "Optional: what was agreed."),
      "kind": _p("string", "Optional: session, call, email, visit or other. Default session."),
      "date": _p("string", "Optional: the day, like 2027-03-31 or yesterday. Default today."),
      "time": _p("string", "Optional: the time, like 14:30."),
      "minutes": _p("integer", "Optional: how long, in minutes."),
      "actions": {"type": "array", "items": {"type": "string"},
                  "description": "Optional: things to do, each like 'Alex: send the worksheet by 2027-03-31'."},
      "next_contact": _p("string", "Optional: the next contact date, like 2027-04-07 or in 2 weeks."),
      "private": _p("boolean", "Optional: true keeps this note's words off chat.")}, ["client"]),
    ("list_open_actions", "Use to see the actions still open: across all clients, or for one client by name "
     "or code, soonest due first.",
     {"client": _p("string", "Optional: one client's code or name. Default all clients.")}, []),
    ("mark_action_done", "Use when the owner says an action from a note is done: it finds the action by its "
     "words, so no need to look it up first.",
     {"action": _p("string", "Words from the action, like send the worksheet."),
      "client": _p("string", "Optional: the client's code or name, when several actions sound alike."),
      "done": _p("boolean", "Optional: false to open it again. Default true.")}, ["action"]),
    ("who_is_due", "Use when the owner asks which clients are due for contact or have not been seen lately: it "
     "lists clients whose next contact date is in the next 7 days or past, and active clients with no note in 30 "
     "days (or the number of days asked).",
     {"days": _p("integer", "Optional: not seen in this many days. Default 30."),
      "within": _p("integer", "Optional: next contact dates this many days ahead. Default 7.")}, []),
    ("remove_note_or_client", "Use only when the owner asks to delete a client's note or a client, by name or "
     "code; no need to look it up first. It removes one note (by its date, or latest), or a client who has no "
     "notes left; a client with notes is removed only when confirm is that client's exact code.",
     {"client": _CLIENT,
      "note": _p("string", "Optional: which note to remove: its date, like 2027-03-31, or latest."),
      "confirm": _p("string", "Only to remove a client who still has notes: the client's exact code.")},
     ["client"]),
]
CHANGES = ["add_client", "add_session_note", "mark_action_done", "remove_note_or_client"]
DELETES = ["remove_note_or_client"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES, "destructiveHint": n in DELETES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Notes, msg) -> dict | None:
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
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (Unreadable, NoClient) as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}],
                       "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Client Notes does not know that method."}}


# --------------------------------------------------------------- HTTP

PAGE_ROUTES = ("/api/client", "/api/note", "/api/action", "/api/remove_note", "/api/remove_client", "/api/import")


class Handler(BaseHTTPRequestHandler):
    server_version = "ClientNotes/1"
    app: Notes
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
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12 or int(raw) > MAX_BODY:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _drain(self) -> None:
        """Read and drop a body that is too big (up to a limit and a time), so the answer reaches the sender."""
        raw = self.headers.get("Content-Length", "").strip()
        left = min(int(raw), 8 * MAX_BODY) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else 0
        end = time.monotonic() + BODY_WAIT_S
        try:
            while left > 0 and time.monotonic() < end:
                self.connection.settimeout(max(0.1, end - time.monotonic()))
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

    def _query_id(self) -> str | None:
        """The ?id= of an export: None for all; a ValueError for a value that is not an id."""
        got = parse_qs(urlsplit(self.path).query).get("id")
        if not got:
            return None
        if not ID_RE.fullmatch(got[0]):
            raise ValueError("that is not a client id")
        return got[0]

    def _refused(self) -> bool:
        """Every route needs the token, checked before anything else is read."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Client Notes needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Client Notes stopped on this request with an error ({type(exc).__name__})."})
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
            if path in ("/export", "/export_text"):
                cid = self._query_id()
                stem = f"client-notes-{self.app.today().isoformat()}"
                if path == "/export":
                    return self._json(200, self.app.export(cid),
                                      {"Content-Disposition": f'attachment; filename="{stem}.json"'})
                return self._send(200, self.app.export_text(cid).encode("utf-8"), "text/plain; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{stem}.txt"'})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except LookupError:
            return self._json(404, {"error": "No client with that id."})
        self._json(404, {"error": "Client Notes has no page here."})

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
            return self._json(404, {"error": "Client Notes has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send a JSON object."})
            app = self.app
            if path == "/api/client":
                out = {"text": app.save_client(body.get("client"))}
            elif path == "/api/note":
                out = {"text": app.save_note(body.get("note"))}
            elif path == "/api/action":
                out = {"text": app.set_action(self._id(body, "note_id"), self._id(body, "action_id"), body.get("done"))}
            elif path == "/api/remove_note":
                out = {"text": app.remove_note_by_id(self._id(body, "id"))}
            elif path == "/api/remove_client":
                out = {"text": app.remove_client_by_id(self._id(body, "id"), body.get("confirm"))}
            else:
                out = {"text": app.import_doc(body.get("data"))}
            out["last_updated"] = app.store.load()["last_updated"]
            self._json(200, out)
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    @staticmethod
    def _id(body: dict, key: str) -> str:
        v = body.get(key)
        if not (isinstance(v, str) and ID_RE.fullmatch(v)):
            raise ValueError(f"say which one ({key})")
        return v

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


def make_server(port: int, data: Path | str, token: str | None, today=date.today) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Notes(Store(data, today), today)
    handler = type("NotesHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Client Notes for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Client Notes with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
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
