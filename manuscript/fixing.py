"""The writer's fixes to the story bible, as stored operations (CONTRACT.md, 0.4).

A record in ``corrections.json`` keeps what she said and the operations it came to. ``apply_corrections`` replays them,
in order, on the bible as read (``bible.base.json``), so a re-read never undoes them and never needs the brain, and an
undo is one record less. An operation is

    {"target": {"kind", "id"?, "match": [normalised names]}, "action": "set | add | remove | move | merge",
     "field"?, "item"?, "value"?, "to"?: a target, "old"?, "says"}

``target`` is an entry of the bible (``KINDS`` lists them all), found by a normalised match first, as an id may change
in a re-read. ``field`` and ``item`` narrow it: a field of that entry and, in a list, one item by its normalised words.
With no field the operation is on the whole entry. ``says`` is the plain line shown to the writer.

Three ways in, all ending in the same operations: ``writer.rule_plan`` for a plain sentence, ``plan_words`` for any other
sentence through the brain, and ``hand_ops`` for the page's Edit controls. The brain only ever names facts by the refs
it was shown; each thing it asks for is checked here against the bible and against her words, and dropped if it fails.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from typing import Any

import analysis
from analysis import _norm, _t, ask_json, importance_of, minor_write_up, tokens
from blocks import IDENT, entry_key, slug
from writer import _named, apply_one, guess_tab, set_relation

DONE, GONE = "done", "gone"
PROSE_WAIT = "after reading"
OPS_MAX = 8                      # what one sentence can come to; more than this from the brain is a bad answer
VIEW_ROOM = 2600                 # tokens of bible entries in one ask, on a small model's 8,192
SETTLED_ROOM = 300               # tokens of her corrections told to a synthesis ask

_SECTIONS = {"sections": "secs"}
KINDS: dict[str, dict] = {
    "character": {"at": ("characters",), "tab": "characters", "word": "character", "name": "name", "fields": {
        "name": "name", "role": "text", "importance": ("main", "supporting", "minor"), "aliases": "strs", "traits": "strs",
        "wants": "text", "fears": "text", "age_words": "age", "looks": "evidence", "personality": "prose",
        "write_up": "prose", "relationships": "rels", "arc": "beats", "appearances": "secs"}},
    "place": {"at": ("places",), "tab": "world", "word": "place", "name": "name", "fields": {
        "name": "name", "kind": "text", "where": "text", "notes": "text", "details": "evidence", "appearances": "secs"}},
    "event": {"at": ("events",), "tab": "timeline", "word": "event", "name": "what", "fields": {
        "what": "text", "when": "text", "where": "place", "section": "sec",
        "placement": ("now", "earlier", "later", ""), "who": "who"}},
    "thread": {"at": ("threads",), "tab": "plot", "word": "thread", "name": "name", "fields": {
        "name": "text", "kind": ("main plot", "subplot"), "open": "bool", "sections": "secs", "setups": "strs",
        "payoffs": "strs"}},
    "theme": {"at": ("themes",), "tab": "themes", "word": "theme", "name": "name", "fields": {
        "name": "text", "evidence": "quotes"}},
    "world_rule": {"at": ("world_rules",), "tab": "world", "word": "world rule", "name": "rule", "fields": {
        "rule": "text", **_SECTIONS}},
    "science": {"at": ("science",), "tab": "science", "word": "science claim", "name": "claim", "fields": {
        "claim": "text", "section": "sec", "plausible": ("yes", "no", "unclear"), "note": "text"}},
    "continuity": {"at": ("continuity",), "tab": "plot", "word": "continuity note", "name": "note", "fields": {
        "note": "text", "kind": ("mismatch", "gap", "question"), **_SECTIONS}},
    "history": {"at": ("history",), "tab": "world", "word": "piece of history", "name": "what", "fields": {
        "what": "text", "when": "text", **_SECTIONS}},
    "geography": {"at": ("geography",), "tab": "world", "word": "distance", "name": "note", "fields": {
        "from": "text", "to": "text", "note": "text"}},
    "scene": {"at": ("scenes",), "tab": "structure", "word": "scene", "name": "purpose", "fields": {
        "purpose": "text", "pov": "text", "tense": "text"}},
    "act": {"at": ("structure", "acts"), "tab": "structure", "word": "act", "name": "name", "fields": {
        "name": "text", "from": "sec", "to": "sec"}},
    "turning_point": {"at": ("structure", "turning_points"), "tab": "structure", "word": "turning point",
                      "name": "name", "fields": {"name": "text", "section": "sec", "note": "text"}},
    "midpoint": {"at": ("structure", "midpoint"), "one": True, "tab": "structure", "word": "midpoint", "fields": {
        "section": "sec", "note": "text"}},
    "climax": {"at": ("structure", "climax"), "one": True, "tab": "structure", "word": "climax", "fields": {
        "section": "sec", "note": "text"}},
    "setting": {"at": ("setting",), "one": True, "tab": "world", "word": "setting", "fields": {
        "world": "text", "place_in_world": "text", "era": "text", "technology": "text", "one_paragraph": "prose",
        "look": "evidence"}},
    "voice_section": {"at": ("voice", "by_section"), "tab": "voice", "word": "section's voice", "name": "pov", "fields": {
        "pov": "text", "tense": "text", "slips": "strs"}},
    "tic": {"at": ("voice", "tics"), "tab": "voice", "word": "tic", "name": "phrase", "fields": {"phrase": "text"}},
    "habit": {"at": ("voice", "dialogue_habits"), "tab": "voice", "word": "dialogue habit", "name": "habit", "fields": {
        "character": "text", "habit": "text"}},
    "summary": {"at": ("summary",), "one": True, "tab": "plot", "word": "summary", "fields": {
        "one_line": "text", "paragraph": "prose"}},
    "section_summary": {"at": ("summary", "by_section"), "tab": "plot", "word": "section summary", "name": "summary",
                        "fields": {"summary": "text"}},
    "brief": {"at": ("briefs",), "one": True, "tab": "characters", "word": "brief", "fields": {
        t: "prose" for t in analysis.BRIEF_TABS}},
    "cast": {"at": (), "one": True, "tab": "characters", "word": "cast summary", "fields": {"cast_summary": "prose"}},
}
LISTS = {"strs", "evidence", "beats", "rels", "secs", "who", "quotes"}
FIELD_WORDS = {"age_words": "age", "write_up": "write-up", "one_paragraph": "paragraph", "place_in_world": "place",
               "one_line": "one line", "arc": "beats", "pov": "point of view"}
TAB_KINDS = {"characters": ("character", "cast"), "plot": ("thread", "continuity", "event", "summary", "section_summary"),
             "timeline": ("event",), "structure": ("act", "turning_point", "midpoint", "climax", "scene"),
             "world": ("setting", "history", "world_rule", "place", "geography"), "science": ("science",),
             "themes": ("theme",), "voice": ("voice_section", "tic", "habit")}
ADDABLE = ("character", "place", "event", "thread", "theme", "world_rule", "science", "continuity", "history",
           "geography", "act", "turning_point", "habit")
OWNS_MOVABLE = ("character", "place")
# shown to the brain only on their own tab or when she names them: nothing else she says is about these
ONLY_NEAR = ("scene", "act", "turning_point", "midpoint", "climax", "voice_section", "tic", "habit",
             "summary", "section_summary")
HEAD = {"character": "C", "place": "P", "event": "E", "thread": "T", "theme": "M", "world_rule": "R", "science": "S",
        "continuity": "N", "history": "H", "geography": "G", "act": "A", "turning_point": "U", "midpoint": "MID",
        "climax": "CLI", "setting": "SET", "voice_section": "V", "tic": "I", "habit": "B", "summary": "SUM",
        "section_summary": "Y", "scene": "Z"}
# the words that show she means a field, when the old value shares no word with hers
MEANS = {"wants": {"want", "wish", "goal", "dream", "hope", "after"}, "fears": {"fear", "afraid", "scared", "dread"},
         "age_words": {"age", "old", "young", "year", "older", "younger"}, "role": {"role"},
         "section": {"section", "chapter", "part"}, "when": {"when", "time", "year", "day", "night", "morning"},
         "where": {"where", "place"}, "kind": {"kind", "type"}, "plausible": {"plausible", "possible", "true", "false"},
         "name": {"name", "called", "named"}, "open": {"open", "paid", "closed"}, "tense": {"tense"},
         "pov": {"point", "view", "person"}, "era": {"era", "century", "year", "set"}, "technology": {"technology"}}


class Bad(Exception):
    """A plain line: this operation can't be done."""


class Held(Bad):
    """The fact says more than she denied, so it stays, and she is told. The text is what to tell her."""


class Missing(Bad):
    """What she speaks of is not in the bible at all. The text is what to tell her."""


# -- finding things -----------------------------------------------------------------------------------

def fact_key(text: Any) -> str:
    """The same fact in other small words: "her green eyes" and "green eyes" and "eyes, green" are one."""
    w = re.findall(r"[\w'-]+", str(text or "").lower())
    keep = sorted(set(w) - {"a", "an", "the", "her", "his", "their", "its", "of", "with", "and", "has", "have", "had"})
    return " ".join(keep or w)


def _holder(bible: dict, spec: dict) -> Any:
    here: Any = bible
    for k in spec["at"]:
        if not isinstance(here, dict):
            return None
        if k not in here:
            here[k] = [] if k == spec["at"][-1] and not spec.get("one") else {}
        here = here[k]
    return here


def rows_of(bible: dict, kind: str) -> list[dict]:
    got = _holder(bible, KINDS[kind])
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def match_of(kind: str, e: dict) -> list[str]:
    if kind == "character":
        return [_norm(str(e.get("name") or ""))] + [_norm(str(a)) for a in e.get("aliases") or []]
    if IDENT.get(kind) == ("id",):
        name = e.get(KINDS[kind].get("name") or "")
        return [_norm(str(name))] if name else []
    return [_norm(" ".join(str(e.get(f)) for f in IDENT[kind]))]


def target_of(kind: str, e: dict) -> dict:
    t: dict = {"kind": kind}
    if KINDS[kind].get("one"):
        return t
    if IDENT.get(kind) == ("id",):
        t["id"] = e.get("id")
    t["match"] = [m for m in match_of(kind, e) if m]
    return t


def find(bible: dict, t: Any) -> dict | None:
    kind = t.get("kind") if isinstance(t, dict) else None
    spec = KINDS.get(kind)
    if spec is None:
        return None
    if spec.get("one"):
        got = _holder(bible, spec)
        return got if isinstance(got, dict) else None
    rows = rows_of(bible, kind)
    want = set(t.get("match") or [])
    hits = [e for e in rows if want & set(match_of(kind, e))]
    tid = t.get("id")
    for e in hits:
        if tid and e.get("id") == tid:
            return e
    if hits:
        return hits[0]
    return next((e for e in rows if tid and e.get("id") == tid), None)


def _item_text(ftype: str, x: Any) -> str:
    if ftype == "evidence":
        return str(x.get("detail") or "") if isinstance(x, dict) else str(x)
    if ftype == "beats":
        return str(x.get("beat") or "") if isinstance(x, dict) else str(x)
    if ftype == "quotes":
        return str(x.get("quote") or "") if isinstance(x, dict) else str(x)
    if ftype == "rels":
        return f"{x.get('kind')} of {x.get('to')}" if isinstance(x, dict) else str(x)
    return str(x)


def item_key(ftype: str, x: Any) -> str:
    if ftype == "rels" and isinstance(x, dict):
        return f"{x.get('to')}|{fact_key(x.get('kind'))}"
    if ftype in ("secs", "who"):
        return str(x)
    return fact_key(_item_text(ftype, x))


def _items(e: dict, field: str) -> list:
    if not isinstance(e.get(field), list):
        e[field] = []
    return e[field]


def _spot(e: dict, field: str, ftype: str, key: Any) -> int:
    for i, x in enumerate(_items(e, field)):
        if item_key(ftype, x) == key:
            return i
    return -1


def _char(bible: dict, cid: Any) -> dict | None:
    return next((c for c in rows_of(bible, "character") if c.get("id") == cid), None)


# -- applying one operation, the same way every time ----------------------------------------------------

def _seen(c: dict) -> None:
    secs = sorted({x for x in c.get("appearances") or [] if isinstance(x, int)})
    c["appearances"] = secs
    if "first_seen" in c:
        c["first_seen"] = secs[0] if secs else None


def _place_event(bible: dict, e: dict) -> None:
    """An event whose section changed: its place in the telling and, right after the events told before it, the story."""
    events = bible.get("events") or []
    events.sort(key=lambda x: (x.get("section") if isinstance(x.get("section"), int) else 10 ** 6, x.get("order") or 0))
    if not all(isinstance(x.get("story_order"), int) for x in events):
        return
    rest = sorted((x for x in events if x is not e), key=lambda x: x["story_order"])
    at = 0
    for i, x in enumerate(rest, 1):
        if isinstance(x.get("section"), int) and isinstance(e.get("section"), int) and x["section"] <= e["section"]:
            at = i
    rest.insert(at, e)
    for i, x in enumerate(rest, 1):
        x["story_order"] = i


def _renamed(bible: dict, kind: str, old: str, new: str) -> None:
    """A new name, wherever the bible keeps it as a word and not an id."""
    if kind == "character":
        for h in rows_of(bible, "habit"):
            if _norm(str(h.get("character") or "")) == _norm(old):
                h["character"] = new
    elif kind == "place":
        for g in rows_of(bible, "geography"):
            for k in ("from", "to"):
                if _norm(str(g.get(k) or "")) == _norm(old):
                    g[k] = new


def _set_scalar(bible: dict, kind: str, e: dict, field: str, ftype: Any, value: Any) -> None:
    if ftype == "age":
        was = e.get("age_words") if isinstance(e.get("age_words"), dict) else {}
        if value:
            e["age_words"] = {"words": value, "section": was.get("section")}
        else:
            e.pop("age_words", None)
        return
    old = e.get(field)
    e[field] = value
    if ftype == "name":
        _renamed(bible, kind, str(old or ""), str(value))
    if kind == "event" and field == "section":
        _place_event(bible, e)
    if kind == "character" and field == "importance" and value == "minor" and not e.get("write_up"):
        e["write_up"] = minor_write_up(e)


def _new_item_value(ftype: str, item: Any, value: Any) -> Any:
    """An item with a changed value. A look keeps its section; its quote was the text saying otherwise, so it goes."""
    if ftype == "evidence" and isinstance(item, dict):
        return {**item, "detail": value, "quote": "", "corrected": True}
    if ftype == "beats" and isinstance(item, dict):
        return {**item, "beat": value, "corrected": True}
    if ftype == "quotes" and isinstance(item, dict):
        return {**item, "quote": value}
    return value


def _unrelate(bible: dict, a: dict, rel: dict) -> None:
    """A wrong relationship gone from both sides, with its family links."""
    b = _char(bible, rel.get("to"))
    a["relationships"] = [r for r in a.get("relationships") or [] if r is not rel]
    if b is None:
        return
    if not any(isinstance(r, dict) and r.get("to") == b.get("id") for r in a["relationships"]):
        b["relationships"] = [r for r in b.get("relationships") or [] if r.get("to") != a.get("id")]
        for x, y in ((a, b), (b, a)):
            fam = x.get("family") if isinstance(x.get("family"), dict) else {}
            for k in ("parents", "children", "partners"):
                if y.get("id") in (fam.get(k) or []):
                    fam[k].remove(y.get("id"))


def _move_relation(bible: dict, a: dict, b: dict, rel: dict) -> None:
    """A relationship on the wrong person: it is b's now, and the other person's side points at b."""
    other = _char(bible, rel.get("to"))
    a["relationships"] = [r for r in a.get("relationships") or [] if r is not rel]
    if rel.get("to") != b.get("id"):
        have = {item_key("rels", r) for r in _items(b, "relationships")}
        if item_key("rels", rel) not in have:
            b["relationships"].append({**rel, "corrected": True})
    if other is not None:
        for r in list(_items(other, "relationships")):
            if r.get("to") == a.get("id"):
                if other is b:
                    other["relationships"].remove(r)
                else:
                    r["to"] = b.get("id")
    for k, back in (("parents", "children"), ("children", "parents"), ("partners", "partners")):
        fam_a = a.setdefault("family", {})
        if rel.get("to") in (fam_a.get(k) or []):
            fam_a[k].remove(rel.get("to"))
            if other is not None and other is not b:
                b.setdefault("family", {}).setdefault(k, []).append(rel.get("to"))
                fb = other.setdefault("family", {}).setdefault(back, [])
                other["family"][back] = [b.get("id") if x == a.get("id") else x for x in fb]


def _take(into: dict, field: str, ftype: str, x: Any) -> None:
    have = {item_key(ftype, y) for y in _items(into, field)}
    if item_key(ftype, x) not in have:
        into[field].append(x)


def _move(bible: dict, kind: str, a: dict, b: dict, field: str, ftype: Any, key: Any) -> str:
    if a is b:
        return DONE
    if ftype in LISTS:
        at = _spot(a, field, ftype, key)
        if at < 0:
            return DONE if _spot(b, field, ftype, key) >= 0 else GONE
        item = a[field][at]
        if ftype == "rels":
            _move_relation(bible, a, b, item)
        else:
            a[field].pop(at)
            _take(b, field, ftype, item)
        if field == "appearances":
            _seen(a)
            _seen(b)
    else:
        val = a.get(field)
        if not val:
            return DONE if b.get(field) else GONE
        a.pop(field) if ftype == "age" else a.__setitem__(field, "")
        if ftype == "age" or not b.get(field):
            b[field] = val
        elif str(val).lower() not in str(b[field]).lower():
            b[field] = f"{b[field]}; {val}"
    a["corrected"] = b["corrected"] = True
    return DONE


def _swap_people(bible: dict, gone: Any, kept: Any) -> None:
    for c in rows_of(bible, "character"):
        for r in list(c.get("relationships") or []):
            if r.get("to") == gone:
                if c.get("id") == kept:
                    c["relationships"].remove(r)
                else:
                    r["to"] = kept
        fam = c.get("family") if isinstance(c.get("family"), dict) else {}
        for k in ("parents", "children", "partners"):
            if gone in (fam.get(k) or []):
                fam[k] = sorted({kept if x == gone else x for x in fam[k]} - {c.get("id")})
    for e in rows_of(bible, "event"):
        if gone in (e.get("who") or []):
            e["who"] = list(dict.fromkeys(kept if w == gone else w for w in e["who"]))


def _merge(bible: dict, kind: str, a: dict, b: dict) -> None:
    """a is b: what a had, b has, and everything that pointed at a points at b."""
    spec = KINDS[kind]
    for field, ftype in spec["fields"].items():
        if ftype in LISTS and field != "aliases":
            for x in a.get(field) or []:
                if ftype != "rels" or x.get("to") != b.get("id"):
                    _take(b, field, ftype, x)
        elif ftype not in LISTS and field != spec.get("name") and not b.get(field) and a.get(field):
            b[field] = a[field]
    if kind == "character":
        names = [a.get("name")] + list(a.get("aliases") or [])
        for n in names:
            if n and _norm(str(n)) not in match_of(kind, b):
                _items(b, "aliases").append(n)
        _swap_people(bible, a.get("id"), b.get("id"))
        _renamed(bible, kind, str(a.get("name") or ""), str(b.get("name") or ""))
        _seen(b)
        fa, fb = a.get("family") or {}, b.setdefault("family", {})
        for k in ("parents", "children", "partners"):
            fb[k] = sorted(set(fb.get(k) or []) | ((set(fa.get(k) or [])) - {b.get("id")}))
        rank = ["minor", "supporting", "main"]
        got = [x for x in (a.get("importance"), b.get("importance")) if x in rank]
        total = len(bible.get("sections") or [])
        b["importance"] = max(got + [importance_of(str(b.get("role") or ""), len(b.get("appearances") or []), total)],
                              key=rank.index)
    elif kind == "place":
        for e in rows_of(bible, "event"):
            if e.get("where") == a.get("id"):
                e["where"] = b.get("id")
        _renamed(bible, kind, str(a.get("name") or ""), str(b.get("name") or ""))
        b["appearances"] = sorted(set(b.get("appearances") or []))
    _drop(bible, kind, a)
    b["corrected"] = True


def _drop(bible: dict, kind: str, e: dict) -> None:
    rows = _holder(bible, KINDS[kind])
    rows[:] = [x for x in rows if x is not e]


def _forget(bible: dict, kind: str, e: dict) -> None:
    """A whole entry removed: what pointed at it."""
    if kind == "character":
        gone = e.get("id")
        for c in rows_of(bible, "character"):
            c["relationships"] = [r for r in c.get("relationships") or [] if r.get("to") != gone]
            fam = c.get("family") if isinstance(c.get("family"), dict) else {}
            for k in ("parents", "children", "partners"):
                if gone in (fam.get(k) or []):
                    fam[k].remove(gone)
        for x in rows_of(bible, "event"):
            if gone in (x.get("who") or []):
                x["who"].remove(gone)
    elif kind == "place":
        for x in rows_of(bible, "event"):
            if x.get("where") == e.get("id"):
                x["where"] = ""


def new_entry(bible: dict, kind: str, value: dict) -> dict:
    """A whole new entry, with every field the rest of the app reads."""
    v = value if isinstance(value, dict) else {}
    text = lambda k, n=400: _t(v.get(k), n)                 # noqa: E731
    sec = v.get("section") if isinstance(v.get("section"), int) else None
    if kind == "character":
        used = {c.get("id") for c in rows_of(bible, "character")}
        sid, k = slug(v.get("name")), 2
        while sid in used:
            sid, k = f"{slug(v.get('name'))}-{k}", k + 1
        c = {"id": sid, "name": text("name", 120), "aliases": [], "role": text("role", 60), "traits": [],
             "wants": text("wants"), "fears": text("fears"), "appearances": [], "first_seen": None, "arc": [],
             "relationships": [], "family": {"parents": [], "children": [], "partners": []},
             "importance": "minor", "personality": "", "write_up": "", "looks": []}
        c["write_up"] = minor_write_up(c)
        return c
    if kind == "place":
        return {"id": slug(v.get("name")), "name": text("name", 120), "kind": text("kind", 60), "notes": text("notes"),
                "where": text("where", 120), "appearances": [], "details": []}
    if kind == "event":
        where = text("where", 120)
        spot = next((p.get("id") for p in rows_of(bible, "place") if where and _norm(where) in match_of("place", p)), where)
        who = [c.get("id") for c in rows_of(bible, "character")
               if {_norm(str(n)) for n in v.get("who") or []} & set(match_of("character", c))]
        return {"id": "x" + entry_key("event", {"what": text("what")}).split(":")[1], "section": sec, "order": 99,
                "story_order": 0, "what": text("what"), "who": who, "where": spot, "when": text("when", 120),
                "placement": ""}
    if kind == "thread":
        return {"id": slug(v.get("name")), "name": text("name", 120), "kind": "main plot" if "main" in text("kind") else
                "subplot", "sections": [sec] if sec is not None else [], "setups": [], "payoffs": [], "open": True}
    if kind == "theme":
        return {"name": text("name", 120), "evidence": []}
    if kind == "world_rule":
        return {"rule": text("rule"), "sections": [sec] if sec is not None else []}
    if kind == "science":
        pl = text("plausible").lower()
        return {"claim": text("claim"), "section": sec, "note": text("note"),
                "plausible": pl if pl in ("yes", "no", "unclear") else "unclear"}
    if kind == "continuity":
        k = text("kind").lower()
        return {"kind": k if k in ("mismatch", "gap", "question") else "question", "note": text("note"),
                "sections": [sec] if sec is not None else []}
    if kind == "history":
        return {"when": text("when", 120), "what": text("what"), "sections": [sec] if sec is not None else []}
    if kind == "geography":
        return {"from": text("from", 120), "to": text("to", 120), "note": text("note"), "section": sec, "quote": ""}
    if kind == "act":
        return {"name": text("name", 80), "from": v.get("from") if isinstance(v.get("from"), int) else sec,
                "to": v.get("to") if isinstance(v.get("to"), int) else sec}
    if kind == "turning_point":
        return {"name": text("name", 120), "section": sec, "note": text("note")}
    if kind == "habit":
        return {"character": text("character", 120), "habit": text("habit")}
    raise Bad("That cannot be added here.")


def _add_entry(bible: dict, kind: str, value: Any) -> str:
    made = new_entry(bible, kind, value)
    rows = _holder(bible, KINDS[kind])
    if any(set(match_of(kind, made)) & set(match_of(kind, e)) for e in rows if isinstance(e, dict)):
        return DONE
    made["corrected"] = True
    rows.append(made)
    if kind == "event":
        _place_event(bible, made)
    return DONE


def apply_op(bible: dict, op: dict) -> str:
    """One operation, in place. DONE when the bible now says what it asks, GONE when what it names is not there."""
    t = op.get("target") or {}
    kind, act, field = t.get("kind"), op.get("action"), op.get("field")
    spec = KINDS.get(kind)
    if spec is None:
        return GONE
    if act == "add" and not field:
        return _add_entry(bible, kind, op.get("value"))
    e = find(bible, t)
    if act == "remove" and not field:                       # the whole entry: gone is what she asked for
        if e is not None:
            _drop(bible, kind, e)
            _forget(bible, kind, e)
        return DONE
    if e is None:
        return GONE
    ftype = spec["fields"].get(field)
    if act == "merge" or act == "move":
        b = find(bible, op.get("to"))
        if b is None:
            return GONE
        if act == "merge":
            if b is not e:
                _merge(bible, kind, e, b)
            return DONE
        return _move(bible, kind, e, b, field, ftype, op.get("item"))
    if ftype is None:
        return GONE
    if act == "set":
        if ftype == "rels":
            b = find(bible, op.get("to"))
            if b is None:
                return GONE
            set_relation(bible, e, b, str(op.get("value")))
        elif ftype in LISTS:
            at = _spot(e, field, ftype, op.get("item"))
            if at >= 0:
                e[field][at] = _new_item_value(ftype, e[field][at], op.get("value"))
            elif _spot(e, field, ftype, item_key(ftype, _new_item_value(ftype, {}, op.get("value")))) < 0:
                return GONE
        else:
            _set_scalar(bible, kind, e, field, ftype, op.get("value"))
        e["corrected"] = True
        return DONE
    if act == "add":
        if ftype == "rels":
            b = _char(bible, (op.get("value") or {}).get("to"))
            if b is None:
                return GONE
            set_relation(bible, e, b, str((op.get("value") or {}).get("kind")))
        else:
            _take(e, field, ftype, op.get("value"))
            if field == "appearances":
                _seen(e)
        e["corrected"] = True
        return DONE
    if act == "remove":
        if ftype in LISTS:
            at = _spot(e, field, ftype, op.get("item"))
            if at >= 0:
                if ftype == "rels":
                    _unrelate(bible, e, e[field][at])
                else:
                    e[field].pop(at)
                if field == "appearances":
                    _seen(e)
        else:
            _set_scalar(bible, kind, e, field, ftype, "" if not isinstance(ftype, tuple) else e.get(field))
        e["corrected"] = True
        return DONE
    return GONE


# -- all her records, in order -------------------------------------------------------------------------

def _hand_prose(records: list) -> set:
    """The prose fields she wrote herself: the brain's rewrites never go over these."""
    out = set()
    for r in records:
        if isinstance(r, dict) and r.get("by") == "hand":
            for op in r.get("ops") or []:
                if op.get("action") == "set" and _is_prose(op):
                    out.add(_prose_key(op))
    return out


def _is_prose(op: dict) -> bool:
    return KINDS.get((op.get("target") or {}).get("kind"), {}).get("fields", {}).get(op.get("field")) == "prose"


def _prose_key(op: dict) -> tuple:
    t = op.get("target") or {}
    return t.get("kind"), t.get("id"), op.get("field")


def apply_corrections(bible: dict, records: Any) -> list[dict]:
    """Every correction on top of a bible, in the order made, in place. A record says for itself whether it held:
    ``[{id, what, correction, made_at, by, state, applied, tab, tabs, line, why, ...}]``."""
    out = []
    rows = records if isinstance(records, list) else []
    hand = _hand_prose(rows)
    touched: list[tuple[str, dict]] = []
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("what") or "").strip():
            continue
        base = {k: r.get(k) for k in ("id", "what", "correction", "made_at")}
        if "ops" not in r and "state" not in r:             # before 0.4: her words, matched each time
            try:
                got = apply_one(bible, str(r.get("what") or ""), str(r.get("correction") or ""))
            except (TypeError, ValueError, AttributeError, KeyError):
                got = {"applied": False, "tab": "characters", "line": ""}   # a bible it can't read: kept as a note
            out.append({**base, "by": "old", "state": "done" if got["applied"] else "note", "tabs": [got["tab"]],
                        "why": "", "looks": False, **got})
            continue
        states = []
        for op in r.get("ops") or []:
            try:
                states.append(apply_op(bible, op))
            except (TypeError, ValueError, AttributeError, KeyError, Bad):
                states.append(GONE)
            t = op.get("target") or {}
            if t.get("kind") == "character" and op.get("action") != "merge":
                touched.extend(("character", x) for x in (find(bible, t), find(bible, op.get("to"))) if x)
        for p in r.get("prose") or []:
            if _prose_key(p) not in hand:
                e = find(bible, p.get("target"))
                if e is not None:
                    e[p.get("field")] = p.get("value")
        ops = r.get("ops") or []
        applied = DONE in states
        tabs = list(dict.fromkeys([KINDS.get((o.get("target") or {}).get("kind"), {}).get("tab", "characters") for o in ops]
                                  + ([r["asked_on"]] if r.get("asked_on") else [])))
        tab = next(iter(tabs), None) or r.get("tab") or guess_tab(bible, str(r.get("what")))
        state = r.get("state") or ("done" if applied else "note")
        why = str(r.get("why") or "")
        if ops and not applied and not why:
            why = "The story bible no longer has what this changed, since the last reading. Your words stay here."
        out.append({**base, "by": r.get("by") or "words", "state": state, "applied": applied, "tab": tab,
                    "tabs": tabs or [tab], "line": str(r.get("line") or "") if applied else "", "why": why,
                    "looks": applied and touches_looks(ops), "prose_state": r.get("prose_state") or "",
                    "partial": bool(r.get("partial"))})
    for _k, c in touched:                                   # a minor character's one line is made from the facts
        if c.get("importance") == "minor" and ("character", c.get("id"), "write_up") not in hand:
            c["write_up"] = minor_write_up(c)
    return out


def touches_looks(ops: list) -> bool:
    return any(o.get("field") in ("looks", "age_words", "details", "look") or o.get("action") == "merge"
               or (o.get("field") == "name" and (o.get("target") or {}).get("kind") == "character") for o in ops)


def settled_lines(records: Any, room: int = SETTLED_ROOM) -> list[str]:
    """Her corrections as lines for an ask, newest first, as many as fit: what she said and what it changed."""
    out, used = [], 0
    for r in reversed(records if isinstance(records, list) else []):
        if not isinstance(r, dict) or r.get("state") == "working" or not str(r.get("what") or "").strip():
            continue
        said = str(r.get("line") or "") if r.get("by") == "hand" else str(r.get("what"))
        fix = str(r.get("correction") or "")
        line = said + (f" ({fix})" if fix and fix.lower() not in said.lower() else "")
        used += tokens(line)
        if used > room:
            break
        out.append(line)
    return out


# -- what the operations say -------------------------------------------------------------------------

def _clip(text: Any, n: int = 70) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def who_of(kind: str, e: dict) -> str:
    spec = KINDS[kind]
    if spec.get("one"):
        return f"the {spec['word']}"
    name = e.get(spec.get("name") or "") or e.get("name") or (f"section {e.get('section')}" if "section" in e else "")
    return str(name) if kind in OWNS_MOVABLE else f"the {spec['word']} \"{_clip(name)}\""


def _word(field: Any) -> str:
    return FIELD_WORDS.get(field, str(field).replace("_", " "))


def _names(bible: dict, text: str) -> str:
    """A relationship's "kind of id", in names."""
    head, _, rest = text.rpartition(" of ")
    c = _char(bible, rest)
    return f"{head} of {c.get('name')}" if head and c else text


# -- making operations (every way in ends here) ----------------------------------------------------------

def _coerce(bible: dict, ftype: Any, value: Any) -> Any:
    """A value of the field's own type, or Bad."""
    if isinstance(ftype, tuple):
        v = _t(value, 60).lower()
        if v not in ftype:
            raise Bad("That is not one of the choices.")
        return v
    if ftype in ("sec",):
        n = value if isinstance(value, int) and not isinstance(value, bool) else None
        if isinstance(value, str) and value.strip().isdigit():
            n = int(value.strip())
        if n is None:
            raise Bad("A section is a number.")
        known = [s.get("n") for s in bible.get("sections") or [] if isinstance(s, dict)]
        if known and n not in known:
            raise Bad(f"There is no section {n}.")
        return n
    if ftype == "secs":
        return _coerce(bible, "sec", value)
    if ftype == "bool":
        return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "open")
    if ftype == "prose":
        return analysis.plain(value)
    if ftype == "name":
        v = _t(value, 120)
        if not v:
            raise Bad("A name can't be empty.")
        return v
    if ftype == "who":
        key = _norm(str(value))
        c = next((c for c in rows_of(bible, "character")
                  if key in match_of("character", c) or c.get("id") == value), None)
        if c is None:
            raise Bad("That person is not in the story bible.")
        return c.get("id")
    if ftype == "place":
        key = _norm(str(value))
        p = next((p for p in rows_of(bible, "place") if key in match_of("place", p) or p.get("id") == value), None)
        return p.get("id") if p else _t(value, 120)
    return _t(value, 400)


def _list_value(bible: dict, ftype: str, value: Any) -> Any:
    """A new list item from a plain value. What she adds has no quote: it is her word, not the text's."""
    if ftype == "evidence":
        return {"detail": _t(value, 200), "section": None, "quote": "", "corrected": True}
    if ftype == "beats":
        return {"section": None, "beat": _t(value), "corrected": True}
    if ftype == "quotes":
        return {"section": None, "quote": _t(value, analysis.QUOTE_MAX)}
    if ftype == "strs":
        return _t(value, 200)
    return _coerce(bible, ftype, value)


def _op(bible: dict, kind: str, e: dict | None, action: str, field: Any = None, item: Any = None, value: Any = None,
        dest: dict | None = None) -> dict:
    """One operation with its plain line, checked against the bible. Bad when it can't be done."""
    spec = KINDS[kind]
    if action == "add" and not field:
        if kind not in ADDABLE:
            raise Bad("That cannot be added here.")
        made = new_entry(bible, kind, value)
        name = made.get(spec.get("name") or "")
        if not name:
            raise Bad(f"Say what the {spec['word']} is.")
        return {"target": {"kind": kind}, "action": "add", "value": value,
                "says": f"added the {spec['word']} \"{_clip(name)}\""}
    if e is None:
        raise Bad("That is not in the story bible any more.")
    op: dict = {"target": target_of(kind, e), "action": action}
    who = who_of(kind, e)
    if action == "merge":
        if dest is None or dest is e:
            raise Bad("Say which one it is the same as.")
        op["to"] = target_of(kind, dest)
        op["says"] = f"{who} and {who_of(kind, dest)} are one {spec['word']} now, shown as {who_of(kind, dest)}"
        return op
    if action == "remove" and not field:
        op["says"] = f"removed {'the ' + spec['word'] + ' ' if kind in OWNS_MOVABLE else ''}{who}"
        op["old"] = str(e.get(spec.get("name") or "") or "")
        return op
    ftype = spec["fields"].get(field)
    if ftype is None:
        raise Bad("That can't be changed here.")
    op["field"] = field
    word = _word(field)
    if ftype in LISTS and action in ("set", "remove", "move"):
        at = _spot(e, field, ftype, item)
        if at < 0:
            raise Bad("That is not in the story bible any more.")
        op["item"] = item
        old = _item_text(ftype, e[field][at])
        if ftype == "rels":
            old = _names(bible, old)
        elif ftype == "who":
            c = _char(bible, e[field][at])
            old = str(c.get("name")) if c else old
        elif ftype == "secs":
            old = f"section {old}"
        op["old"] = old
    elif ftype not in LISTS:
        old = e.get(field)
        old = (old or {}).get("words") if isinstance(old, dict) else old
        op["old"] = "" if old is None or old == "" else str(old)
    else:
        op["old"] = ""
    old = op["old"]
    if action == "move":
        if kind not in OWNS_MOVABLE:
            raise Bad("That can only move between people or places.")
        if dest is None or dest is e:
            raise Bad("Say who it belongs to.")
        if not old:
            raise Bad("There is nothing there to move.")
        op["to"] = target_of(kind, dest)
        op["says"] = f"moved {word} \"{_clip(old)}\" from {who} to {who_of(kind, dest)}"
        return op
    if action == "remove":
        if not old:
            raise Bad("There is nothing there to remove.")
        op["says"] = f"removed \"{_clip(old)}\" from {who}'s {word}"
        return op
    if action == "set":
        if ftype == "rels":
            kindword = _t(value, 60)
            other = _char(bible, e[field][at].get("to")) if item is not None else dest
            if not kindword or other is None:
                raise Bad("Say who and what they are to each other.")
            op.pop("item", None)
            op.update(to=target_of("character", other), value=kindword)
            op["says"] = f"{e.get('name')} is {other.get('name')}'s {kindword} now"
            return op
        val = _t(value, 200) if ftype in ("strs", "evidence", "beats", "quotes") else _coerce(bible, ftype, value)
        if val == "" and ftype in LISTS:
            raise Bad("Say what it should be.")
        if field == "section" and ftype == "sec":
            shown = f"section {val}"
        elif ftype == "bool":
            shown = "open" if val else "paid off"
        else:
            shown = _clip(val)
        op["value"] = val
        was = f"section {old}" if ftype == "sec" else f"\"{_clip(old)}\""
        now = shown if ftype == "sec" else f"\"{shown}\""
        if ftype == "name":
            op["says"] = f"{old} is called {val} now"
        elif field == spec.get("name"):
            op["says"] = f"the {spec['word']} {was} is now {now}"
        elif kind in OWNS_MOVABLE:
            op["says"] = f"{who}'s {word}: {was} is now {now}" if old else f"{who}'s {word} is {now} now"
        else:
            op["says"] = f"{who}: its {word} was {was}, and is {now} now" if old else f"{who}: its {word} is {now} now"
        return op
    if action == "add":
        if ftype == "rels":
            v = value if isinstance(value, dict) else {}
            other = dest or _char(bible, v.get("to"))
            kindword = _t(v.get("kind"), 60)
            if other is None or not kindword:
                raise Bad("Say who and what they are to each other.")
            op["value"] = {"to": other.get("id"), "kind": kindword}
            op["says"] = f"{e.get('name')} is {other.get('name')}'s {kindword} now"
            return op
        if ftype not in LISTS:
            raise Bad("That can't be added to.")
        v = _list_value(bible, ftype, value)
        if v in ("", None):
            raise Bad("Say what to add.")
        op["value"] = v
        op["says"] = f"added \"{_clip(_item_text(ftype, v))}\" to {who}'s {word}"
        return op
    raise Bad("That can't be done here.")


# -- by hand ---------------------------------------------------------------------------------------------

def _by_key(bible: dict, key: Any) -> tuple[str, dict | None]:
    kind, _, rest = str(key or "").partition(":")
    spec = KINDS.get(kind)
    if spec is None:
        raise Bad("That is not in the story bible any more.")
    if spec.get("one"):
        return kind, _holder(bible, spec)
    return kind, next((e for e in rows_of(bible, kind) if entry_key(kind, e) == key), None) if rest else None


def hand_ops(bible: dict, asked: Any) -> list[dict]:
    """The page's Edit controls: ``[{key, action, field?, item?, value?, to?}]`` as stored operations. Bad, with a plain
    line, for anything that can't be done."""
    ops = []
    for a in asked if isinstance(asked, list) else []:
        if not isinstance(a, dict) or a.get("action") not in ("set", "add", "remove", "move", "merge"):
            raise Bad("That can't be done here.")
        kind, e = _by_key(bible, a.get("key"))
        dest = _by_key(bible, a.get("to"))[1] if a.get("to") else None
        value = a.get("value")
        if a.get("action") == "add" and not a.get("field") and isinstance(value, dict):
            spec = KINDS[kind]
            value = {k: (_coerce(bible, spec["fields"].get(k, "text"), v) if k in ("section", "from", "to") else v)
                     for k, v in value.items() if str(v).strip()}
        ops.append(_op(bible, kind, e, a["action"], a.get("field"), a.get("item"), value, dest))
    if not ops:
        raise Bad("Nothing to change.")
    return ops


def line_of(ops: list[dict]) -> str:
    says = [str(o.get("says")) for o in ops if o.get("says")]
    return "; ".join(says)


# -- what the page's Edit controls show ------------------------------------------------------------------

def _field(bible: dict, kind: str, e: dict, field: str, ftype: Any) -> dict:
    label = _word(field).capitalize()
    moves = kind in OWNS_MOVABLE and field not in ("name", "role", "importance", "aliases", "kind", "where", "notes",
                                                   "personality", "write_up", "arc")
    if ftype in LISTS:
        items = []
        for x in _items(e, field) if field in e or ftype in LISTS else []:
            text = _item_text(ftype, x)
            if ftype == "rels":
                text = _names(bible, text)
            elif ftype == "who":
                c = _char(bible, x)
                text = str(c.get("name")) if c else text
            elif ftype == "secs":
                text = f"Section {text}"
            sec = x.get("section") if isinstance(x, dict) and isinstance(x.get("section"), int) else None
            items.append({"key": item_key(ftype, x), "text": text, "section": sec})
        return {"name": field, "label": label, "type": "list", "items": items, "add": ftype, "moves": moves}
    v = e.get(field)
    v = (v or {}).get("words") if isinstance(v, dict) else v
    out = {"name": field, "label": label, "value": "" if v is None else v, "moves": moves and ftype in ("text", "age")}
    if isinstance(ftype, tuple):
        out.update(type="select", options=list(ftype))
    elif ftype == "prose":
        out["type"] = "long"
    elif ftype == "sec":
        out["type"] = "number"
    elif ftype == "bool":
        out.update(type="select", options=["true", "false"], value="true" if v else "false", labels=["open", "paid off"])
    else:
        out["type"] = "text"
    return out


def _edit_item(bible: dict, kind: str, e: dict, only: tuple = ()) -> dict:
    spec = KINDS[kind]
    fields = [_field(bible, kind, e, f, t) for f, t in spec["fields"].items() if not only or f in only]
    return {"key": entry_key(kind, e) if not spec.get("one") else f"{kind}:" + (only[0] if kind == "brief" else ""),
            "kind": kind, "label": who_of(kind, e), "fields": fields,
            "removable": not spec.get("one"), "word": spec["word"]}


ADD_FIELDS = {"character": ("name", "role"), "place": ("name", "kind", "notes"),
              "event": ("what", "section", "when", "where"), "thread": ("name", "section"), "theme": ("name",),
              "world_rule": ("rule", "section"), "science": ("claim", "section", "note"),
              "continuity": ("note", "section"), "history": ("what", "when", "section"), "geography": ("from", "to", "note"),
              "act": ("name", "from", "to"), "turning_point": ("name", "section", "note"), "habit": ("character", "habit")}
FOLDS = {"event": "Change an event", "section_summary": "Change a section's summary", "summary": "Change the summary",
         "setting": "Change the setting", "history": "Change the history"}


def edit_view(bible: dict, tab: str) -> dict:
    """What the page's Edit controls need for one tab: ``items`` by key, ``adds`` (a kind and its fields), ``folds`` (the
    entries no block row shows: a timeline's events, the summaries), ``people`` and ``places`` for "Belongs to..."."""
    items: dict[str, dict] = {}
    folds = []
    for kind in TAB_KINDS.get(tab, ()):
        spec = KINDS[kind]
        if spec.get("one"):
            holder = _holder(bible, spec)
            if isinstance(holder, dict):
                it = _edit_item(bible, kind, holder, () if kind != "cast" else ("cast_summary",))
                items[it["key"]] = it
                if kind in FOLDS:
                    folds.append({"label": FOLDS[kind], "keys": [it["key"]]})
            continue
        keys = []
        for e in rows_of(bible, kind):
            it = _edit_item(bible, kind, e)
            items[it["key"]] = it
            keys.append(it["key"])
        if kind in FOLDS and keys:
            folds.append({"label": FOLDS[kind], "keys": keys})
    briefs = _holder(bible, KINDS["brief"])
    if isinstance(briefs, dict) and tab in briefs:
        it = _edit_item(bible, "brief", briefs, (tab,))
        it["label"] = "the brief"
        it["fields"][0]["label"] = "Brief"
        items[it["key"]] = it
    adds = [{"kind": k, "word": KINDS[k]["word"], "key": f"{k}:",
             "fields": [{"name": f, "label": _word(f).capitalize(),
                         "type": "number" if f in ("section", "from", "to") and k != "geography" else "text"}
                        for f in ADD_FIELDS[k]]}
            for k in TAB_KINDS.get(tab, ()) if k in ADDABLE]
    who = [{"key": entry_key("character", c), "id": c.get("id"), "name": c.get("name")}
           for c in rows_of(bible, "character")]
    spots = [{"key": entry_key("place", p), "id": p.get("id"), "name": p.get("name")} for p in rows_of(bible, "place")]
    return {"items": items, "adds": adds, "folds": folds, "people": who, "places": spots}


# -- through the brain -----------------------------------------------------------------------------------

CORRECT_SYSTEM = (
    "You fix facts in a writer's notes on her own book. She says in her own words what is wrong. Below are her words "
    "and the entries that may matter. Every fact has a ref in square brackets. Answer with one JSON object only: "
    '{"ops":[...]}. An op is one of:\n'
    '{"do":"change","ref":"C70.looks.1","to":"grey eyes"} a fact with a wrong value\n'
    '{"do":"remove","ref":"C70.looks.1"} a fact that is wrong, with nothing in its place\n'
    '{"do":"move","ref":"C71.looks.1","owner":"C70"} a fact on the wrong person or place; owner is the right one\n'
    '{"do":"add","on":"C70","field":"traits","to":"shy"} a missing fact; for a person field is looks, traits, aliases, '
    "wants, fears or role; for a place details\n"
    '{"do":"merge","ref":"C73","into":"C70"} two entries that are one person or place\n'
    '{"do":"change","ref":"C70.name","to":"New Name"} a new name\n'
    '{"do":"new","kind":"event","to":{"what":"","section":1}} a whole new entry; kind is character, place, event, '
    "thread, theme, world_rule, science, history or continuity\n"
    "Rules. Use only the refs shown below her words, copied exactly; the refs in these examples are not real. A ref "
    "with no dot is the whole entry. Do only what her words say and nothing more. Change the entry she speaks of, not "
    "another that happens to share a section. One fact usually means one change. \"X, not Y\" means X is right and Y is wrong. Keep her own words in "
    "\"to\". When she says someone never does something, remove the events and beats that say they did; change nothing "
    "else. When you can't tell which fact she means, or she is only thinking aloud, answer "
    '{"ops":[]}. Never guess. Examples:\n'
    'Her words: Ida has grey eyes, not green. Facts: [C70.looks.1] green eyes. Answer: {"ops":[{"do":"change",'
    '"ref":"C70.looks.1","to":"grey eyes"}]}\n'
    'Her words: That burn belongs to Ned, not Olaf. Facts: [C71] Olaf, [C71.looks.3] a burn on his arm, [C70] Ned. '
    'Answer: {"ops":[{"do":"move","ref":"C71.looks.3","owner":"C70"}]}\n'
    'Her words: The fire is in chapter 4, not chapter 2. Facts: [E70] The fire starts, [E70.section] section: 2. Answer: '
    '{"ops":[{"do":"change","ref":"E70.section","to":4}]}\n'
    'Her words: Olaf never goes to the mill. Facts: [E71] Olaf walks to the mill, [C71.arc.1] beats: Olaf reaches the '
    'mill. Answer: {"ops":[{"do":"remove","ref":"E71"},{"do":"remove","ref":"C71.arc.1"}]}\n'
    'Her words: Grief is not a theme of this book. Facts: [M70] Theme: grief. Answer: {"ops":[{"do":"remove","ref":"M70"}]}\n'
    'Her words: Ann and Annie are the same person. Facts: [C70] Ann, [C74] Annie. Answer: {"ops":[{"do":"merge",'
    '"ref":"C74","into":"C70"}]}'
)
_STOP = analysis.STOP | {"not", "never", "no", "isn", "wasn", "doesn", "didn", "don", "aren", "weren", "same", "one",
                         "wrong", "belongs", "belong", "has", "have", "had", "should", "actually", "really", "been",
                         "goes", "go"}
_NEG = re.compile(r"\b(?:not|never|no longer|no|\w+n't|wrong|remove|delete|forget)\b")
_CLAUSE = re.compile(r"\b(?:not|never|no longer|\w+n't)\b[^,.;]*")
NUMBERS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                      "fifteen sixteen seventeen eighteen nineteen twenty".split(), 1)}


def toks(text: Any) -> set:
    """The words that carry meaning, with plurals and "'s" off."""
    out = set()
    for w in re.findall(r"[a-z0-9']+", str(text or "").lower().replace("’", "'")):
        w = re.sub(r"'s$", "", w).strip("'")
        if w and w not in _STOP:
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _about(text: Any) -> set:
    """The words that say which entry: not a number, not "chapter". Every event in section 2 shares a 2."""
    return {w for w in toks(text) if not w.isdigit() and w not in ("chapter", "section", "part")}


_VERB = re.compile(r"\b(?:is|are|was|were|has|have|had|comes?|came|happens?|happened|belongs?|goes|went|turns?|never|not|"
                   r"no longer|\w+n't|in|at|on|should|must|would|could|can|does|did)\b")
_PRONOUNS = {"i", "she", "he", "it", "they", "this", "that", "there"}


def subject_of(words: str, value: Any = None) -> set:
    """Who or what her sentence is about: the words before its verb ("the storm" in "the storm is in chapter 4"). With
    none ("in chapter 2 the storm sinks"), her words less what she denies and the new value."""
    low = _low(words)
    m = _VERB.search(low)
    head = (_about(low[:m.start()]) if m else set()) - _PRONOUNS
    return head or (_about(low) - denied(words) - _about(value) - _PRONOUNS) or _about(low)


def what_it_is(kind: str, e: dict) -> str:
    """The words that say what an entry IS. Not a passing field: an event's "when" may mention a storm it is not."""
    fields = [f for f in IDENT.get(kind, ()) if f not in ("id", "section", "n")] + [KINDS[kind].get("name") or ""]
    return " ".join(str(e.get(f) or "") for f in dict.fromkeys(fields) if f)


TAB_WORDS = {"plot": "Plot and subplots"}


def no_such(words: str, kind: str, subject: set) -> str:
    """She speaks of something the bible does not have: say so, and where she can add it."""
    low = _low(words)
    m = _VERB.search(low)
    head = low[:m.start()] if m else ""
    head = " ".join(w for w in re.findall(r"[\w'-]+", head) if w not in ("the", "a", "an", "that", "this", "her", "his"))
    name = head if head and toks(head) & subject else " ".join(sorted(subject))
    spec = KINDS[kind]
    line = f"There is no {name} {spec['word']} in the story bible, so nothing changed."
    if kind in ADDABLE:
        tab = spec["tab"]
        line += f" To add one, use Add: {spec['word']} on the {TAB_WORDS.get(tab, tab.capitalize())} tab."
    return line


_SAME = (re.compile(r"^(.+?)\s+and\s+(.+?)\s+(?:are|were)\s+(?:really\s+|actually\s+)?(?:the\s+same|one)\b"),
         re.compile(r"^(.+?)\s+(?:is|was)\s+(?:really\s+|actually\s+)?the\s+same\s+(?:person|character|place)\s+as\s+(.+)$"))


def already_one(bible: dict, words: str) -> str:
    """ "Anna and Annie are the same person" when Annie is already Anna's other name: the line that says so, or ""."""
    low = _low(words).strip(" .!")
    for pat in _SAME:
        m = pat.match(low)
        if not m:
            continue
        for kind in OWNS_MOVABLE:
            a, b = _named_in(m.group(1), bible, kind), _named_in(m.group(2), bible, kind)
            if len(a) == 1 and len(b) == 1 and a[0] is b[0]:
                name, other = str(a[0].get("name")), m.group(2) if _norm(m.group(2)) != _norm(str(a[0].get("name"))) else m.group(1)
                what = "person" if kind == "character" else "place"
                return f"{name} and {other.strip().title()} are already one {what} in the story bible, shown as {name}, so nothing needed changing."
    return ""


def _low(words: str) -> str:
    return str(words or "").lower().replace("’", "'")


def denied(words: str) -> set:
    """What she says is NOT so: the words after a "not" that she doesn't also say the other way."""
    low = _low(words)
    said = " ".join(m.group() for m in _CLAUSE.finditer(low))
    rest = _CLAUSE.sub(" ", low)
    return toks(said) - toks(rest)


def _section_for(bible: dict, k: int) -> int | None:
    """Section k, or the section whose heading says "Chapter k": what she calls chapter 4 may be section 5."""
    secs = [s for s in bible.get("sections") or [] if isinstance(s, dict)]
    for s in secs:
        m = re.search(r"\b(?:chapter|part|section)\s+(\w+)", str(s.get("heading") or "").lower())
        if m and (m.group(1) == str(k) or NUMBERS.get(m.group(1)) == k):
            return s.get("n")
    if any(s.get("n") == k for s in secs) or not secs:
        return k
    return None


def _numbers_said(words: str) -> set:
    low = _low(words)
    got = {int(x) for x in re.findall(r"\b\d+\b", low)}
    got |= {NUMBERS[w] for w in re.findall(r"[a-z]+", low) if w in NUMBERS}
    return got


def _facts(bible: dict, kind: str, e: dict) -> list[tuple[str, Any, Any, str]]:
    """Every fact of an entry the brain may change: (ref tail, field, item key, what it says). No prose."""
    out = []
    for field, ftype in KINDS[kind]["fields"].items():
        if ftype == "prose":
            continue
        if ftype in LISTS:
            for j, x in enumerate(e.get(field) or [], 1):
                text = _item_text(ftype, x)
                if ftype == "rels":
                    text = _names(bible, text)
                elif ftype == "who":
                    c = _char(bible, x)
                    text = str(c.get("name")) if c else text
                elif ftype == "secs":
                    text = f"in section {text}"
                sec = x.get("section") if isinstance(x, dict) and isinstance(x.get("section"), int) else None
                out.append((f".{field}.{j}", field, item_key(ftype, x),
                            f"{_word(field)}: {text}" + (f" (section {sec})" if sec is not None else "")))
        else:
            v = e.get(field)
            v = (v or {}).get("words") if isinstance(v, dict) else v
            if v not in (None, "", []):
                out.append((f".{field}", field, None, f"{_word(field)}: {_clip(v, 200)}"))
    return out


def _named_in(words: str, bible: dict, kind: str) -> list[dict]:
    if kind == "character":
        return _named(words, rows_of(bible, kind))
    if kind == "place":
        return _named(words, rows_of(bible, kind), extra=True)
    return []


def build_view(bible: dict, words: str, tab: str = "", room: int = VIEW_ROOM) -> tuple[str, dict]:
    """The entries that may matter, each fact with a ref, within *room* tokens, and what each ref means."""
    said = _about(words)
    refs: dict[str, dict] = {}
    numbered: dict[int, str] = {}
    for kind, head in HEAD.items():
        spec = KINDS[kind]
        rows = [_holder(bible, spec)] if spec.get("one") else rows_of(bible, kind)
        for i, e in enumerate(rows, 1):
            if isinstance(e, dict):
                ref = head if spec.get("one") else f"{head}{i}"
                numbered[id(e)] = ref
    lines = []
    for kind, label in (("character", "People in the book"), ("place", "Places in the book")):
        names = []
        for e in rows_of(bible, kind):
            names.append(f"[{numbered[id(e)]}] {e.get('name')}" + (f" (also {', '.join(map(str, e['aliases']))})"
                                                                   if e.get("aliases") else ""))
            refs[numbered[id(e)].upper()] = {"kind": kind, "e": e}
        if names:
            lines.append(f"{label}: " + analysis.fit(names, 300).replace("\n", "; "))
    if re.search(r"\b(chapter|section|part)\b|\b\d+\b", _low(words)):
        heads = [f"{s.get('n')} {s.get('heading')}" for s in bible.get("sections") or [] if isinstance(s, dict)]
        if heads:
            lines.append("Sections, by number: " + analysis.fit(heads, 200).replace("\n", "; "))
    chosen = []
    near = TAB_KINDS.get(tab, ())
    for kind in HEAD:
        spec = KINDS[kind]
        if kind in ONLY_NEAR and kind not in near and not toks(spec["word"]) & said:
            continue
        rows = [_holder(bible, spec)] if spec.get("one") else rows_of(bible, kind)
        named = _named_in(words, bible, kind)
        for e in rows:
            if not isinstance(e, dict):
                continue
            facts = _facts(bible, kind, e)
            whole = " ".join(f[3] for f in facts)
            score = len(said & _about(whole))
            is_named = any(e is n for n in named) or bool(spec.get("one") and toks(spec["word"]) & said)
            if is_named or score:
                chosen.append((-(100 if is_named else 0) - score - (0.5 if kind in near else 0), len(chosen), kind, e,
                               facts, is_named))
    chosen.sort(key=lambda x: (x[0], x[1]))
    used = tokens("\n".join(lines))
    for _score, _n, kind, e, facts, is_named in chosen:
        ref = numbered[id(e)]
        keep = facts if is_named or kind not in OWNS_MOVABLE else [f for f in facts if said & _about(f[3])]
        spec = KINDS[kind]
        name = e.get(spec.get("name") or "") or spec["word"]
        block = [f"[{ref}] {spec['word'].capitalize()}: {_clip(name, 120)}"]
        shown = {ref.upper(): {"kind": kind, "e": e}}          # a ref exists only once the brain has been shown it
        for tail, field, item, text in keep[:40]:
            block.append(f"  [{ref}{tail}] {text}")
            shown[f"{ref}{tail}".upper()] = {"kind": kind, "e": e, "field": field, "item": item, "text": text}
        got = tokens("\n".join(block))
        if used + got > room:
            continue
        used += got
        lines += block
        refs.update(shown)
    return "\n".join(lines), refs


def _ops_of(got: Any) -> list[dict] | None:
    if isinstance(got, list):
        return [x for x in got if isinstance(x, dict)]
    if isinstance(got, dict):
        for k in ("ops", "operations", "changes"):
            if isinstance(got.get(k), list):
                return [x for x in got[k] if isinstance(x, dict)]
        if got.get("do"):
            return [got]
    return None


def _fits(value: Any, old: str, said: set, no: set) -> bool:
    """A new value comes from her words: nothing she denied, some of what she said, at most one word of the model's."""
    want = toks(value)
    if not want or want & no or not want & said:
        return False
    fresh = want - said - toks(old)
    return len(fresh) <= (1 if len(want) >= 4 else 0)


def _means_field(field: Any, old: str, said: set) -> bool:
    """The fact she means: its old value shares a word with hers, or she names what kind of fact it is."""
    return bool(toks(old) & said) or bool(toks(_word(field)) & said) or bool(MEANS.get(field, set()) & said)


def _one(bible: dict, m: dict, refs: dict, words: str, said: set, no: set) -> dict:
    """One thing the model asked for, as an operation, or Bad. Refs only: nothing it names can be outside the bible,
    and each kind of change must fit what her own words say."""
    do = str(m.get("do") or m.get("action") or "").lower()
    do = {"set": "change", "replace": "change", "update": "change", "rename": "change", "delete": "remove",
          "drop": "remove", "append": "add"}.get(do, do)
    negated = bool(_NEG.search(_low(words)))

    def at(k: str) -> dict | None:
        ref = str(m.get(k) or "").strip("[] ").upper()
        # "C1.FEARS.1" for the one fact C1.FEARS: a small model's slip, and it can only name what it was shown
        return refs.get(ref) or refs.get(re.sub(r"\.\d+$", "", ref))

    def is_said(kind: str, e: dict) -> bool:
        return any(e is n for n in _named_in(words, bible, kind))

    if do == "new":
        kind = str(m.get("kind") or "").strip().lower().replace(" ", "_")
        if kind not in ADDABLE:
            raise Bad("not a kind of entry")
        v = m.get("to") if m.get("to") is not None else m.get("value")
        name = KINDS[kind].get("name")
        v = {name: v} if isinstance(v, str) else v
        if not isinstance(v, dict) or not _fits(v.get(name), "", said, no):
            raise Bad("the new entry is not in her words")
        if isinstance(v.get("section"), (int, str)):
            sec = _section_for(bible, int(v["section"])) if str(v["section"]).isdigit() else None
            v = {**v, "section": sec} if sec is not None and int(v["section"]) in _numbers_said(words) else \
                {k: x for k, x in v.items() if k != "section"}
        return _op(bible, kind, None, "add", value=v)
    if do == "merge":
        a, b = at("ref"), at("into") or at("to") or at("owner")
        if not a or not b or a.get("field") or b.get("field") or a["kind"] != b["kind"] or a["kind"] not in OWNS_MOVABLE:
            raise Bad("merge needs two people or two places")
        if not is_said(a["kind"], a["e"]) or not is_said(b["kind"], b["e"]) or toks(_names_of(a)) & no \
                or toks(_names_of(b)) & no:
            raise Bad("she did not name both as one")
        return _op(bible, a["kind"], a["e"], "merge", dest=b["e"])
    if do == "add":
        on = at("on") or at("ref")
        if not on or on.get("field"):
            raise Bad("add needs an entry")
        field = str(m.get("field") or "").strip()
        v = m.get("to") if m.get("to") is not None else m.get("value")
        ftype = KINDS[on["kind"]]["fields"].get(field)
        if ftype is None or ftype == "prose" or not isinstance(v, (str, int)):
            raise Bad("no such field")
        if on["kind"] in OWNS_MOVABLE and not is_said(on["kind"], on["e"]):
            raise Bad("she did not name who it is on")
        if not _fits(v, "", said, no):
            raise Bad("the value is not in her words")
        if ftype in LISTS and ftype not in ("rels",):
            return _op(bible, on["kind"], on["e"], "add", field, value=v)
        if ftype in ("text", "age"):
            had = on["e"].get(field)
            had = (had or {}).get("words") if isinstance(had, dict) else had
            if had and toks(v) & toks(had):
                raise Bad("already there")
            return _op(bible, on["kind"], on["e"], "set", field, value=f"{had}; {v}" if had else v)
        raise Bad("that cannot be added")
    r = at("ref")
    if not r:
        raise Bad("not a ref she was shown")
    kind, e, field, item = r["kind"], r["e"], r.get("field"), r.get("item")
    owner_words = toks(_names_of(r)) if kind in OWNS_MOVABLE else set()
    about = _about(words)
    subject = subject_of(words, m.get("to") if isinstance(m.get("to"), str) else None)

    def not_someone_else() -> None:
        """She named someone, or some place, and this is on another: not what she means."""
        if kind in OWNS_MOVABLE and _named_in(words, bible, kind) and not is_said(kind, e):
            raise Bad("that is on someone she did not name")

    def right_entry() -> None:
        """An event, a thread, a theme: the subject of her sentence must be what it IS, not a word in a passing field."""
        if kind in OWNS_MOVABLE or KINDS[kind].get("one") or _about(what_it_is(kind, e)) & subject:
            return
        if any(_about(what_it_is(kind, x)) & subject for x in rows_of(bible, kind)):
            raise Bad("not the entry she means")
        raise Missing(no_such(words, kind, subject))

    def her_subject() -> None:
        """On a person or a place: her sentence is about them, or about the fact itself."""
        if kind in OWNS_MOVABLE and not subject & (owner_words | toks(r.get("text"))):
            raise Bad("her sentence is about something else")

    def holds_more(text: str, word: str) -> None:
        """A beat or an event that says more than she denied: removing it would lose the rest."""
        if len(toks(text) - said) >= 3:
            raise Held(f"the {word} \"{_clip(text, 120)}\"")

    if do == "move":
        dest = at("owner") or at("to") or at("into")
        if not field or not dest or dest.get("field") or dest["kind"] != kind or kind not in OWNS_MOVABLE:
            raise Bad("a move is a fact and its right owner")
        if not is_said(kind, dest["e"]) or toks(_names_of(dest)) & no:
            raise Bad("she did not name the right owner")
        if not toks(r.get("text")) & (said - owner_words):
            raise Bad("not the fact she means")
        her_subject()
        return _op(bible, kind, e, "move", field, item, dest=dest["e"])
    if do == "remove":
        if not negated:
            raise Bad("she did not say it is wrong")
        if not field:
            name = str(e.get(KINDS[kind].get("name") or "") or "")
            have = toks(name)
            right_entry()
            if kind == "event":
                holds_more(name, "event")
            if kind in OWNS_MOVABLE and not is_said(kind, e) or not have or len(have & said) * 2 < len(have):
                raise Bad("she did not name it")
            return _op(bible, kind, e, "remove")
        not_someone_else()
        if field == "who":
            rest = toks(" ".join(f[3] for f in _facts(bible, kind, e) if f[1] != "who"))
            if not toks(r.get("text")) & subject or not rest & said:
                raise Bad("not the event she means")
        else:
            right_entry()
            her_subject()
            if not toks(r.get("text")) & (said - owner_words):
                raise Bad("not the fact she means")
            if KINDS[kind]["fields"].get(field) == "beats":
                at_ = _spot(e, field, "beats", item)
                if at_ >= 0:
                    holds_more(_item_text("beats", e[field][at_]), "beat")
        return _op(bible, kind, e, "remove", field, item)
    if do == "change":
        if not field:
            field = KINDS[kind].get("name")
            item = None
            if not field or KINDS[kind]["fields"].get(field) == "text":
                raise Bad("a change is on a fact")
        not_someone_else()
        right_entry()
        her_subject()
        if KINDS[kind].get("one") and not (toks(KINDS[kind]["word"]) & said or _means_field(field, str(r.get("text") or ""), about)):
            raise Bad("she did not speak of that")
        v = m.get("to") if m.get("to") is not None else m.get("value")
        ftype = KINDS[kind]["fields"].get(field)
        if ftype is None or ftype == "prose" or v is None or isinstance(v, (dict, list)):
            raise Bad("no such field")
        old = str(r.get("text") or "")
        if ftype in ("sec", "secs"):
            k = v if isinstance(v, int) else int(v) if str(v).strip().isdigit() else None
            ok = {_section_for(bible, n) for n in _numbers_said(words) if str(n) not in no}
            if k is None or k not in ok:
                raise Bad("she did not say that section")
            if ftype == "secs":
                raise Bad("change one section at a time")
            v = k
        elif isinstance(ftype, tuple) or ftype == "bool":
            if not (toks(old) & said or toks(_names_of(r)) & said):
                raise Bad("not the fact she means")
        elif ftype == "name":
            if not toks(e.get(KINDS[kind].get("name") or "")) & said or not _fits(v, "", said, no):
                raise Bad("she did not give that name")
        else:
            if not _means_field(field, old, said - owner_words):
                raise Bad("not the fact she means")
            if not _fits(v, old, said, no):
                raise Bad("the value is not in her words")
        return _op(bible, kind, e, "set", field, item, v)
    raise Bad("not an operation")


def _names_of(r: dict) -> str:
    e = r["e"]
    return " ".join([str(e.get("name") or "")] + [str(a) for a in e.get("aliases") or []])


def read_answer(bible: dict, got: Any, refs: dict, words: str, told: dict | None = None) -> tuple[list[dict], int]:
    """The operations that hold, and how many it asked for that were dropped. ``told``, when given, collects what to tell
    her about the rest: ``held``, the beats and events left as they are, and ``missing``, what is not in the bible."""
    told = told if told is not None else {}
    wanted = _ops_of(got) or []
    said, no = toks(words), denied(words)
    ops, dropped, seen = [], 0, set()
    for m in wanted[:OPS_MAX]:
        try:
            op = _one(bible, m, refs, words, said, no)
        except Held as exc:                                  # not a failure: what she said, kept from going too far
            if str(exc) not in told.setdefault("held", []):
                told["held"].append(str(exc))
            continue
        except Missing as exc:
            told.setdefault("missing", []).append(str(exc))
            dropped += 1
            continue
        except (Bad, ValueError, TypeError, KeyError, AttributeError):
            dropped += 1
            continue
        mark = str(sorted(str(v) for v in op.items() if v[0] != "says"))
        if mark not in seen:
            seen.add(mark)
            ops.append(op)
    return ops, dropped + max(0, len(wanted) - OPS_MAX)


def plan_words(brain: Any, bible: dict, what: str, correction: str = "", tab: str = "") -> dict:
    """Any sentence, through the brain: ``{ops, line, partial, why}``. It sees only the entries that may matter and
    never writes the bible: what it answers becomes operations only if they check out."""
    words = what if not correction or correction.lower() in what.lower() else f"{what}. {correction}"
    same = already_one(bible, words)
    if same:                                                 # no brain needed, and nothing to change
        return {"ops": [], "line": "", "partial": False, "why": same}
    view, refs = build_view(bible, words, tab)
    user = f"Her words: {words}\n\n" + (f"She is looking at the {tab} tab.\n\n" if tab else "") + f"Entries:\n{view}\n\n" \
        f"Answer for her words, \"{words}\", with refs from these entries only."
    got, _model = ask_json(brain, CORRECT_SYSTEM, user, lambda g: _ops_of(g) is not None, patient=True)
    if got is None:
        return {"ops": [], "line": "", "partial": False,
                "why": "Your bot could not give a clear answer on that, so nothing changed."}
    told: dict = {}
    ops, dropped = read_answer(bible, got, refs, words, told)
    held = " and ".join(told.get("held") or [])
    if not ops:
        if held:
            why = f"{held[:1].upper()}{held[1:]} says more than that, so I left it as it is. Press Edit to change it yourself."
        else:
            why = next(iter(told.get("missing") or []), "") or _no_event(bible, words) or \
                "Your bot could not tell which fact in the story bible that changes, so nothing changed."
        return {"ops": [], "line": "", "partial": False, "why": why}
    line = line_of(ops)
    if held:
        line += f"; left {held} as it is because it says more than that (press Edit to change it yourself)"
    return {"ops": ops, "line": line, "partial": dropped > 0, "why": ""}


def _no_event(bible: dict, words: str) -> str:
    """The model answered nothing to "the storm is in chapter 4", and there is no storm in the book's events: say that."""
    if not re.search(r"\b(?:chapter|section|part)\s+\w+", _low(words)):
        return ""
    subject = subject_of(words)
    if _named_in(words, bible, "character") or _named_in(words, bible, "place"):
        return ""
    if any(toks(KINDS[k]["word"]) & subject for k in KINDS if KINDS[k].get("one")):
        return ""
    for kind in ("event", "thread", "theme", "world_rule", "science", "history", "act", "turning_point"):
        if any(_about(what_it_is(kind, x)) & subject for x in rows_of(bible, kind)):
            return ""
    return no_such(words, "event", subject)


# -- wrong prose follows the fix ---------------------------------------------------------------------------

def prose_steps(bible: dict, ops: list[dict], records: list) -> list[tuple[str, Any]]:
    """What to rewrite after these operations: each character they touched who has prose, and each tab's brief that
    still mentions what changed. A field she wrote herself is left to her."""
    hand = _hand_prose(records)
    steps: list[tuple[str, Any]] = []
    tabs: list[str] = []
    for op in ops:
        t = op.get("target") or {}
        kind = t.get("kind")
        if _is_prose(op):
            continue
        tab = KINDS.get(kind, {}).get("tab")
        if tab and tab not in tabs:
            tabs.append(tab)
        if kind == "character":
            for e in (find(bible, op.get("to")), find(bible, t)):
                if e is None or e.get("importance") == "minor" or not (e.get("personality") or e.get("write_up")):
                    continue
                if all(("character", e.get("id"), f) in hand for f in ("personality", "write_up")):
                    continue
                if ("character", e.get("id")) not in steps:
                    steps.append(("character", e.get("id")))
    briefs = bible.get("briefs") if isinstance(bible.get("briefs"), dict) else {}
    olds = [str(o.get("old") or "") for o in ops] + [m for o in ops for m in (o.get("target") or {}).get("match") or []]
    for tab in tabs:
        text = str(briefs.get(tab) or "")
        if text and ("brief", None, tab) not in hand and any(toks(o) & toks(text) for o in olds if o):
            steps.append(("brief", tab))
    return steps


def stale_words(ops: list[dict], who: Any = None) -> list[str]:
    """What a rewrite must no longer say: an old value she changed or removed, on this character or on anything."""
    out = []
    for o in ops:
        t = o.get("target") or {}
        if o.get("action") in ("set", "remove") or (o.get("action") == "move" and who is not None and t.get("id") == who):
            if len(str(o.get("old") or "")) >= 4 and (who is None or t.get("id") == who) and o.get("field") != "name":
                out.append(str(o["old"]).lower())
    return out
