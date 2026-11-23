"""Maps atlas: the manuscript's front page as data, and the list of pictures Illustrate paints.

The atlas is, top to bottom: Cast (a portrait per character), World (the painted map with its places pinned on it),
Family (the tree, a portrait in each circle), Story (up to twelve key events, a painted scene each) and Cover. The
eight charts stay as they were (draw.py). Every field of the bible is optional: a ``manuscript-bible/1`` bible has
no ``importance``, ``personality`` or ``write_up``, and the atlas still has a cast, a world, a family and a story.

Pictures are found by their slot: ``portrait:<character id>``, ``place:<place id>``, ``scene:<event id>``,
``worldmap`` and ``cover``. A slot shows its newest ready picture, and under it the proof: the evidence from the bible
(``[{owner, detail, section, quote}]``) and the prompt the picture program was told. A slot with no picture yet shows
what its picture would be told now; a picture painted before 2.1 shows the entries its prompt holds.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
from typing import Any

import draw
import pictures

IMPORTANCE = ("main", "supporting", "minor")
LEAD_ROLES = {"protagonist", "antagonist", "narrator"}
KEY_EVENTS = 12


def _s(v: Any, n: int = 400) -> str:
    return " ".join(str(v or "").split())[:n]


def _ints(v: Any) -> list[int]:
    return sorted({n for n in v if isinstance(n, int) and not isinstance(n, bool)}) if isinstance(v, list) else []


def importance(c: dict) -> str:
    """The bible's own word when it has one; else main for a lead by role or a character in four or more sections,
    supporting in two or more, minor otherwise."""
    got = _s(c.get("importance"), 20).lower()
    if got in IMPORTANCE:
        return got
    n = len(_ints(c.get("appearances")))
    if n >= 4 or _s(c.get("role"), 30).lower() in LEAD_ROLES:
        return "main"
    return "supporting" if n >= 2 else "minor"


def first_seen(c: dict) -> int | None:
    if isinstance(c.get("first_seen"), int):
        return c["first_seen"]
    seen = _ints(c.get("appearances"))
    return seen[0] if seen else None


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def slot_of(kind: Any, who: Any = "") -> str:
    k = _s(kind, 20)
    return k if k in ("worldmap", "cover") else f"{k}:{_s(who, 120)}"


def _section(e: dict) -> int:
    v = e.get("section")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def key_events(bible: dict, most: int = KEY_EVENTS) -> list[dict]:
    """Up to *most* events, spread across the sections: each round takes the next best event of every section (more
    characters first, then the earlier one), until there are enough. In the order they are told."""
    told = sorted(pictures.event_list(bible), key=lambda e: (_section(e), e.get("order") if isinstance(e.get("order"),
                                                                                                         int) else 0))
    if len(told) <= most:
        return told
    by_sec: dict[int, list[dict]] = {}
    for e in told:
        by_sec.setdefault(_section(e), []).append(e)
    for evs in by_sec.values():
        evs.sort(key=lambda e: -len([w for w in e.get("who") or [] if w]))       # stable: earlier first on a tie
    chosen: list[dict] = []
    depth = 0
    while len(chosen) < most:
        row = [evs[depth] for _sec, evs in sorted(by_sec.items()) if len(evs) > depth]
        if not row:
            break
        room = most - len(chosen)
        if len(row) > room:                      # more sections than room: keep the fullest, spread evenly on a tie
            step = len(row) / room
            even = [row[int(i * step)] for i in range(room)]
            fullest = sorted(row, key=lambda e: -len(e.get("who") or []))[:room]
            row = fullest if len({len(e.get("who") or []) for e in row}) > 1 else even
        chosen += row
        depth += 1
    keep = {id(e) for e in chosen}
    return [e for e in told if id(e) in keep]


def cast(bible: dict) -> list[dict]:
    """Every character, the main ones first, then supporting, then minor; within each the protagonist first, then
    the ones in more sections, then by first seen."""
    b = draw.Bible(bible)
    order = {k: i for i, k in enumerate(IMPORTANCE)}
    return sorted(b.chars, key=lambda c: (order[importance(c)], _s(c.get("role"), 30).lower() != "protagonist",
                                          -len(c["appearances"]), first_seen(c) or 999, c["name"]))


def illustrate_items(bible: dict, minor: bool = False) -> list[tuple[str, str, str]]:
    """What Illustrate paints, in order: ``(kind, who, label)``. Every main and supporting character (minor ones too
    when asked), every place, the world map, up to twelve key events, the cover."""
    out: list[tuple[str, str, str]] = []
    for c in cast(bible):
        if minor or importance(c) != "minor":
            out.append(("portrait", c["id"], c["name"]))
    places = draw.Bible(bible).places
    for p in places:
        out.append(("place", p["id"], p["name"]))
    if places:
        out.append(("worldmap", "", "the world map"))
    for e in key_events(bible):
        out.append(("scene", e["id"], _s(e.get("what"), 60)))
    out.append(("cover", "", "the cover"))
    return out


def slots(pics: list[dict]) -> dict[str, dict]:
    """Each slot's picture (newest first in *pics*): the newest ready one, whether one is being made, why the newest
    failed, and the note and id Paint again starts from."""
    out: dict[str, dict] = {}
    for p in pics:
        key = slot_of(p.get("kind"), p.get("who"))
        s = out.setdefault(key, {"picture": None, "making": False, "why": "", "note": "", "latest": p["id"],
                                 "record": None})
        if p.get("state") == "ready" and s["picture"] is None:
            s["picture"] = p["id"]
            s["note"] = _s(p.get("note"), pictures.NOTE_MAX)
            s["record"] = p
        if p.get("state") == "making":
            s["making"] = True
        if p["id"] == s["latest"] and p.get("state") == "failed":
            s["why"] = _s(p.get("why"), 300)
    return out


def told(bible: dict, key: str, rec: dict | None, style: str) -> dict:
    """``{proof, prompt, told_style}`` for one slot: from its picture's record, or what it would be told now."""
    kind, _, who = key.partition(":")
    try:
        prompt, _about, proof = pictures.prompt_and_proof(bible, kind, who, style)
    except pictures.PictureError:
        prompt, proof = "", []
    if rec is not None:
        prompt = _s(rec.get("prompt"), pictures.PROMPT_MAX + 100)
        if isinstance(rec.get("proof"), list):
            proof = [e for e in rec["proof"] if isinstance(e, dict)]
        else:                                               # painted before 2.1: what its prompt holds
            proof = pictures.proof_in(proof, prompt, pictures._people_tokens(bible))
        style = _s(rec.get("style"), pictures.STYLE_MAX) or style
    return {"proof": [{"owner": _s(e.get("owner"), 120), "detail": _s(e.get("detail"), 160),
                       "section": e.get("section") if isinstance(e.get("section"), int) else None,
                       "quote": _s(e.get("quote"), 200)} for e in proof],
            "prompt": prompt, "told_style": _s(style, pictures.STYLE_MAX) or pictures.DEFAULT_STYLE}


def _pic(sl: dict[str, dict], key: str, bible: dict | None = None, style: str = "") -> dict:
    s = sl.get(key) or {}
    return {"slot": key, "picture": s.get("picture"), "making": bool(s.get("making")), "why": s.get("why") or "",
            "note": s.get("note") or "", **told(bible or {}, key, s.get("record"), style)}


def routes(bible: dict, pins: list[dict]) -> list[dict]:
    """The text's distances and directions between two pinned places, for the dashed lines on the world map."""
    have = {p["id"] for p in pins}
    return [{"from": g["from"], "to": g["to"], "note": g["note"], "relation": g["relation"],
             "section": g["section"], "quote": g["quote"]}
            for g in pictures.geography(bible) if g["from"] in have and g["to"] in have and g["from"] != g["to"]]


def build(bible: dict, pics: list[dict], world: dict, family: dict, style: str = "", progress: dict | None = None,
          maker_set: bool = False) -> dict:
    """The atlas for one manuscript. *pics* are its picture records, newest first, as the page sees them; *world* is
    the world chart as Maps serves it (``{spec, saved_layout}``, the saved layout put in); *family* is
    draw.portrait_family."""
    b = draw.Bible(bible)
    sl = slots(pics)
    summary = bible.get("summary") if isinstance(bible.get("summary"), dict) else {}
    by_sec = {r.get("section"): _s(r.get("summary"), 300) for r in summary.get("by_section") or []
              if isinstance(r, dict)}
    events = pictures.event_list(bible)
    place_of = {p["id"]: p for p in b.places}

    people = []
    for c in cast(bible):
        ties: list[dict] = []

        def tie(ref: Any, kind: str) -> None:
            o = b.char(ref)
            if o and o["id"] != c["id"] and not any(t["id"] == o["id"] for t in ties):
                ties.append({"id": o["id"], "name": o["name"], "kind": _s(kind, 60)})
        fam = c.get("family") if isinstance(c.get("family"), dict) else {}
        for ref in fam.get("parents") or []:
            tie(ref, "parent")
        for ref in fam.get("children") or []:
            tie(ref, "child")
        for ref in fam.get("partners") or []:
            tie(ref, "partner")
        for r in c.get("relationships") or []:
            if isinstance(r, dict):
                tie(r.get("to"), r.get("kind"))
        seen_at = sorted({_s(e.get("where"), 80) for e in events if c["id"] in (e.get("who") or [])} - {""})
        first = first_seen(c)
        people.append({
            "id": c["id"], "name": c["name"], "role": _s(c.get("role"), 40), "importance": importance(c),
            "personality": _s(c.get("personality"), 600), "write_up": _s(c.get("write_up"), 1500),
            "traits": [_s(t, 40) for t in c.get("traits") or [] if _s(t, 40)][:8],
            "aliases": [_s(a, 60) for a in c.get("aliases") or [] if _s(a, 60)][:6],
            "first_seen": first, "appearances": c["appearances"],
            "sections": [{"section": n, "summary": by_sec.get(n, "")} for n in c["appearances"]],
            "places": [{"id": p, "name": place_of[p]["name"]} for p in seen_at if p in place_of],
            "ties": ties,
            "point": {"section": first, "quote": c["name"], "note": f"{c['name']} first comes in here."}
            if first is not None else None,
            **_pic(sl, slot_of("portrait", c["id"]), bible, style),
        })

    pins = []
    wspec = world.get("spec") if isinstance(world.get("spec"), dict) else {}
    nodes = {n["id"]: n for n in wspec.get("nodes") or []}
    for p in b.places:
        n = nodes.get(p["id"]) or {}
        here = [e for e in events if _s(e.get("where"), 80) == p["id"]]
        who: list[str] = []
        for e in here:
            for ref in e.get("who") or []:
                o = b.char(ref)
                if o and o["id"] not in who:
                    who.append(o["id"])
        pins.append({
            "id": p["id"], "name": p["name"], "lines": n.get("lines") or [p["name"]],
            "x": n.get("x", 0), "y": n.get("y", 0), "kind": _s(p.get("kind"), 40), "notes": _s(p.get("notes"), 600),
            "where": _s(p.get("where"), 120), "appearances": p["appearances"], "people": who,
            "events": [e["id"] for e in here], "point": n.get("point"),
            **_pic(sl, slot_of("place", p["id"]), bible, style),
        })

    cards = []
    for e in key_events(bible):
        sec = e.get("section") if isinstance(e.get("section"), int) else None
        where = place_of.get(_s(e.get("where"), 80))
        cards.append({
            "id": e["id"], "text": _s(e.get("what"), 300), "section": sec, "when": _s(e.get("when"), 120),
            "where": where["id"] if where else "", "where_name": where["name"] if where else "",
            "who": [o["id"] for o in (b.char(r) for r in e.get("who") or []) if o],
            "point": {"section": sec, "quote": _s(e.get("what"), 200), "note": "From the story strip in Maps."}
            if sec is not None else None,
            **_pic(sl, slot_of("scene", e["id"]), bible, style),
        })

    themes = [_s(t.get("name"), 60) for t in bible.get("themes") or [] if isinstance(t, dict) and _s(t.get("name"), 60)]
    setting = bible.get("setting") if isinstance(bible.get("setting"), dict) else {}
    ready = sum(1 for p in pics if p.get("state") == "ready")
    return {
        "title": b.title, "version": "atlas/1", "style": style, "default_style": pictures.DEFAULT_STYLE,
        "maker": bool(maker_set), "progress": progress,
        "pictures": {"count": len(pics), "ready": ready},
        "one_line": _s(summary.get("one_line"), 400),
        "setting": _s(setting.get("one_paragraph"), 1200),
        "cast": people,
        "world": {"width": wspec.get("width", 0), "height": wspec.get("height", 0),
                  "saved_layout": bool(world.get("saved_layout")), "spec": wspec, "pins": pins,
                  "routes": routes(bible, pins), **_pic(sl, "worldmap", bible, style)},
        "family": {"spec": family},
        "story": {"sections_total": bible.get("sections_total") if isinstance(bible.get("sections_total"), int)
                  else b.n, "cards": cards},
        "cover": {"title": b.title, "one_line": _s(summary.get("one_line"), 400), "themes": themes[:6],
                  **_pic(sl, "cover", bible, style)},
    }
