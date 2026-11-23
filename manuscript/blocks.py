"""Blocks: the one format everything on the tabs is shown in (CONTRACT.md, "Blocks").

``tab_blocks(bible, tab)`` builds a tab's default blocks from the story bible, exactly as the contract's
"Default tab content" lists them. ``clean_blocks(raw)`` checks the blocks the model sends back for a view: unknown
block types are dropped, strings are capped at 2,000 characters, at most 40 blocks are kept, and every ``section`` is
made a whole number or removed. The server never sends HTML; the page draws each kind itself.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

TYPES = ("markdown", "table", "timeline", "graph", "tree", "list", "quote")
MAX_BLOCKS = 40
MAX_TEXT = 2000
MAX_DEPTH = 8                    # a tree deeper than this is cut, so a looping answer can't run forever


def slug(text: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:60].strip("-") or "x"


# What names an entry of each kind in the bible, for the page's Edit controls and the writer's stored fixes (fixing.py).
IDENT = {"character": ("id",), "place": ("id",), "thread": ("id",), "event": ("what",),
         "theme": ("name",), "world_rule": ("rule",), "science": ("claim",), "continuity": ("note",),
         "history": ("what",), "geography": ("from", "to", "note"),
         "scene": ("section", "n"), "act": ("name",), "turning_point": ("name",), "voice_section": ("section",),
         "tic": ("phrase",), "habit": ("character", "habit"), "section_summary": ("section",)}


def entry_key(kind: str, e: Any) -> str:
    """One entry's address on the page: "character:tom", "event:3fa91c20de"."""
    got = [str((e if isinstance(e, dict) else {}).get(f)) for f in IDENT.get(kind, ())]
    if IDENT.get(kind) == ("id",):
        return f"{kind}:{got[0]}"
    return f"{kind}:{hashlib.sha1(' '.join(got).lower().encode('utf-8')).hexdigest()[:10]}"


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = ", ".join(_s(x) for x in v if _s(x))
    elif isinstance(v, dict):
        v = v.get("text") or v.get("label") or v.get("name") or ""
    return str(v)[:MAX_TEXT]


def _section(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and v.is_integer() and v >= 0:
        return int(v)
    m = re.search(r"\d+", str(v or "")) if isinstance(v, str) else None
    return int(m.group()) if m else None


def _with_section(out: dict, raw: dict) -> dict:
    n = _section(raw.get("section"))
    if n is not None:
        out["section"] = n
    return out


def _cell(v: Any) -> Any:
    if isinstance(v, dict):
        return _with_section({"text": _s(v.get("text"))}, v)
    return _s(v)


def _tree(node: Any, depth: int = 0) -> dict | None:
    if not isinstance(node, dict):
        return None
    out = _with_section({"label": _s(node.get("label") or node.get("name"))}, node)
    kids = node.get("children")
    if depth < MAX_DEPTH and isinstance(kids, list):
        out["children"] = [c for c in (_tree(k, depth + 1) for k in kids) if c is not None]
    else:
        out["children"] = []
    return out


def clean_block(raw: Any) -> dict | None:
    """One block, cleaned, or None when it is not a block the page can show."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or "").strip().lower()
    if kind == "markdown":
        text = _s(raw.get("text"))
        return {"type": kind, "text": text} if text.strip() else None
    if kind == "table":
        cols = raw.get("columns")
        rows = raw.get("rows")
        if not isinstance(cols, list) or not cols:
            return None
        rows = rows if isinstance(rows, list) else []
        out = {"type": kind, "columns": [_s(c) for c in cols],
               "rows": [[_cell(c) for c in r] for r in rows if isinstance(r, list)]}
        if isinstance(raw.get("keys"), list) and len(raw["keys"]) == len(out["rows"]):
            out["keys"] = [_s(k) for k in raw["keys"]]               # each row's entry, for its Edit control
        return out
    if kind == "timeline":
        items = raw.get("items")
        if not isinstance(items, list):
            return None
        out = []
        for it in items:
            if isinstance(it, dict):
                who = it.get("who")
                out.append(_with_section({"label": _s(it.get("label") or it.get("text")), "when": _s(it.get("when")),
                                          "who": [_s(w) for w in who] if isinstance(who, list) else []}, it))
        return {"type": kind, "items": out} if out else None
    if kind == "graph":
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            return None
        clean_nodes, ids = [], set()
        for nd in nodes:
            if isinstance(nd, dict) and _s(nd.get("id") or nd.get("label")):
                nid = _s(nd.get("id") or slug(nd.get("label")))
                if nid not in ids:
                    ids.add(nid)
                    clean_nodes.append({"id": nid, "label": _s(nd.get("label") or nid), "kind": _s(nd.get("kind"))})
        edges = raw.get("edges") if isinstance(raw.get("edges"), list) else []
        clean_edges = [{"from": _s(e.get("from")), "to": _s(e.get("to")), "label": _s(e.get("label"))}
                       for e in edges if isinstance(e, dict) and _s(e.get("from")) in ids and _s(e.get("to")) in ids]
        return {"type": kind, "nodes": clean_nodes, "edges": clean_edges} if clean_nodes else None
    if kind == "tree":
        root = _tree(raw.get("root"))
        return {"type": kind, "root": root} if root else None
    if kind == "list":
        items = raw.get("items")
        if not isinstance(items, list):
            return None
        out = [_with_section({"text": _s(it.get("text")), **({"key": _s(it["key"])} if it.get("key") else {})}, it)
               if isinstance(it, dict) else {"text": _s(it)} for it in items]
        out = [o for o in out if o["text"].strip()]
        return {"type": kind, "items": out} if out else None
    if kind == "quote":
        text = _s(raw.get("text"))
        if not text.strip():
            return None
        return _with_section({"type": kind, "text": text, "note": _s(raw.get("note"))}, raw)
    return None


def clean_blocks(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("blocks")
    if not isinstance(raw, list):
        return []
    out = []
    for b in raw:
        got = clean_block(b)
        if got is not None:
            out.append(got)
        if len(out) >= MAX_BLOCKS:
            break
    return out


# -- default tab content from the bible --------------------------------------------------------------

def _first(seq: Any) -> int | None:
    if isinstance(seq, list):
        for v in seq:
            n = _section(v)
            if n is not None:
                return n
    return None


def _sections_text(seq: Any) -> str:
    return ", ".join(str(n) for n in (seq or []) if _section(n) is not None)


def _families(chars: list[dict]) -> list[dict]:
    """A tree per family with more than two people: people joined by parents, children or partners."""
    by_id = {c.get("id"): c for c in chars if c.get("id")}
    links: dict[str, set] = {}
    parents_of: dict[str, set] = {}
    partners_of: dict[str, set] = {}

    def link(a: str, b: str) -> None:
        links.setdefault(a, set()).add(b)
        links.setdefault(b, set()).add(a)

    for c in chars:
        cid = c.get("id")
        fam = c.get("family") if isinstance(c.get("family"), dict) else {}
        for p in fam.get("parents") or []:
            link(cid, p)
            parents_of.setdefault(cid, set()).add(p)
        for k in fam.get("children") or []:
            link(cid, k)
            parents_of.setdefault(k, set()).add(cid)
        for p in fam.get("partners") or []:
            link(cid, p)
            partners_of.setdefault(cid, set()).add(p)
            partners_of.setdefault(p, set()).add(cid)

    def name(pid: str) -> str:
        c = by_id.get(pid)
        return str(c.get("name")) if c else pid.replace("-", " ").title()

    seen: set = set()
    trees = []
    for start in sorted(links):
        if start in seen:
            continue
        group, todo = set(), [start]
        while todo:
            x = todo.pop()
            if x not in group:
                group.add(x)
                todo.extend(links.get(x, ()))
        seen |= group
        if len(group) <= 2:
            continue
        placed: set = set()

        def node(pid: str, depth: int) -> dict:
            placed.add(pid)
            mates = sorted(m for m in partners_of.get(pid, ()) if m in group and m not in placed)
            placed.update(mates)
            label = " and ".join([name(pid)] + [name(m) for m in mates])
            first = by_id.get(pid, {}).get("first_seen")
            out = {"label": label, "children": []}
            if isinstance(first, int):
                out["section"] = first
            if depth < MAX_DEPTH:
                kids = sorted(k for k in group if k not in placed and (pid in parents_of.get(k, ())
                              or any(m in parents_of.get(k, ()) for m in mates)))
                for k in kids:
                    if k not in placed:
                        out["children"].append(node(k, depth + 1))
            return out

        roots = [p for p in sorted(group) if not (parents_of.get(p, set()) & group)]
        made = []
        for r in roots or sorted(group)[:1]:
            if r not in placed:
                made.append(node(r, 0))
        for p in sorted(group):          # anyone the walk above missed still shows
            if p not in placed:
                made.append(node(p, 0))
        root = made[0] if len(made) == 1 else {"label": "Family", "children": made}
        trees.append({"type": "tree", "root": root})
    return trees


def _one_line(text: Any, n: int = 220) -> str:
    s = " ".join(_s(text).split())
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


def _evidence(v: Any) -> list[dict]:
    """A character's looks or a place's details, [{detail, section, quote}], the ones with a detail."""
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict) and _s(x.get("detail")).strip():
            out.append({"detail": _s(x.get("detail")), "section": _section(x.get("section")),
                        "quote": _s(x.get("quote"))})
    return out


def _bracketed(items: list[dict]) -> str:
    """"light hair [1]; a burn on her left hand [3]": each detail with its section in brackets."""
    return "; ".join(x["detail"] + (f" [{x['section']}]" if x["section"] is not None else "") for x in items)


def cards(bible: dict) -> list[dict]:
    """One card per main character: the page draws these with an importance pill and links; the same card is also
    a markdown block in the Characters tab, for anything that reads blocks only."""
    out = []
    for c in (bible.get("characters") or []) if isinstance(bible, dict) else []:
        if not isinstance(c, dict) or c.get("importance") != "main":
            continue
        first = _section(c.get("first_seen"))
        head = f"**{c.get('role') or 'character'}**, a main character" + (f". First seen in section {first}."
                                                                          if first is not None else ".")
        looks = _evidence(c.get("looks"))
        text = "\n\n".join(x for x in (f"### {c.get('name')}" + (" (you corrected this)" if c.get("corrected") else ""),
                                       head, _s(c.get("personality")), _s(c.get("write_up")),
                                       f"**Looks:** {_bracketed(looks)}" if looks else "") if str(x).strip())
        out.append({"id": c.get("id"), "name": c.get("name"), "role": c.get("role") or "",
                    "importance": "main", "personality": _s(c.get("personality")), "write_up": _s(c.get("write_up")),
                    "first_seen": first, "corrected": bool(c.get("corrected")), "looks": looks, "text": text,
                    "key": entry_key("character", c)})
    return out


def tab_blocks(bible: dict, tab: str) -> list[dict]:
    b = bible if isinstance(bible, dict) else {}
    chars = [c for c in b.get("characters") or [] if isinstance(c, dict)]
    names = {c.get("id"): c.get("name") for c in chars}
    out: list[dict] = []
    briefs = b.get("briefs") if isinstance(b.get("briefs"), dict) else {}
    brief = _s(briefs.get(tab)).strip()
    if brief:
        out.append({"type": "markdown", "text": brief})              # every tab opens with what it adds up to
    if tab == "characters":
        if str(b.get("cast_summary") or "").strip():
            out.append({"type": "markdown", "text": _s(b.get("cast_summary"))})
        for c in cards(b):
            out.append({"type": "markdown", "text": c["text"]})
        rest = [c for c in chars if c.get("importance") != "main"]
        if rest:
            out.append({"type": "table", "columns": ["Name", "Importance", "Role", "First seen", "Write-up"],
                        "rows": [[{"text": f"{c.get('name')}" + (" (you corrected this)" if c.get("corrected") else ""),
                                   "section": c.get("first_seen")}, c.get("importance") or "", c.get("role"),
                                  {"text": f"Section {c.get('first_seen')}", "section": c.get("first_seen")},
                                  _one_line(c.get("write_up") or c.get("wants"))] for c in rest],
                        "keys": [entry_key("character", c) for c in rest]})
        if chars:
            edges = []
            for c in chars:
                for r in c.get("relationships") or []:
                    if isinstance(r, dict) and r.get("to") in names:
                        edges.append({"from": c.get("id"), "to": r.get("to"), "label": r.get("kind")})
            out.append({"type": "graph", "nodes": [{"id": c.get("id"), "label": c.get("name"), "kind": c.get("role")}
                                                   for c in chars], "edges": edges})
            out.extend(_families(chars))
    elif tab == "plot":
        events = [e for e in b.get("events") or [] if isinstance(e, dict)]
        if events:
            out.append({"type": "timeline", "items": [{"label": e.get("what"), "when": e.get("when"),
                                                       "section": e.get("section"),
                                                       "who": [names.get(w, w) for w in e.get("who") or []]}
                                                      for e in events]})
        threads = [t for t in b.get("threads") or [] if isinstance(t, dict)]
        if threads:
            out.append({"type": "table", "columns": ["Thread", "Kind", "Sections", "Open or paid off"],
                        "rows": [[{"text": t.get("name"), "section": _first(t.get("sections"))}, t.get("kind"),
                                  _sections_text(t.get("sections")), "open" if t.get("open") else "paid off"]
                                 for t in threads], "keys": [entry_key("thread", t) for t in threads]})
        flags = [c for c in b.get("continuity") or [] if isinstance(c, dict) and c.get("kind") in ("mismatch", "gap")]
        if flags:
            out.append({"type": "list", "items": [{"text": f"{c.get('kind').capitalize()}: {c.get('note')}",
                                                   "section": _first(c.get("sections")),
                                                   "key": entry_key("continuity", c)} for c in flags]})
    elif tab == "world":
        st = b.get("setting") if isinstance(b.get("setting"), dict) else {}
        if str(st.get("one_paragraph") or "").strip():
            out.append({"type": "markdown", "text": _s(st.get("one_paragraph"))})
        else:
            said = [f"**{label}:** {st.get(k)}" for k, label in (("world", "World"), ("place_in_world", "Where"),
                                                                  ("era", "When"), ("technology", "Technology"))
                    if str(st.get(k) or "").strip()]
            if said:
                out.append({"type": "markdown", "text": "\n".join(f"- {x}" for x in said)})
        hist = [h for h in b.get("history") or [] if isinstance(h, dict) and str(h.get("what") or "").strip()]
        if hist and any(str(h.get("when") or "").strip() for h in hist):
            out.append({"type": "timeline", "items": [{"label": h.get("what"), "when": h.get("when"),
                                                       "section": _first(h.get("sections")), "who": []}
                                                      for h in hist]})
        elif hist:
            out.append({"type": "list", "items": [{"text": h.get("what"), "section": _first(h.get("sections")),
                                                   "key": entry_key("history", h)} for h in hist]})
        rules = [r for r in b.get("world_rules") or [] if isinstance(r, dict)]
        if rules:
            out.append({"type": "list", "items": [{"text": r.get("rule"), "section": _first(r.get("sections")),
                                                   "key": entry_key("world_rule", r)} for r in rules]})
        places = [p for p in b.get("places") or [] if isinstance(p, dict)]
        if places:

            def details(p: dict) -> Any:                 # the first three, each with its section
                got = _evidence(p.get("details"))[:3]
                return {"text": _bracketed(got), "section": got[0]["section"]} if got else ""

            out.append({"type": "table", "columns": ["Place", "Kind", "Where", "Notes", "Details", "Sections"],
                        "rows": [[{"text": p.get("name") + (" (you corrected this)" if p.get("corrected") else ""),
                                   "section": _first(p.get("appearances"))}, p.get("kind"),
                                  p.get("where") or st.get("place_in_world") or "",
                                  p.get("notes"), details(p), _sections_text(p.get("appearances"))] for p in places],
                        "keys": [entry_key("place", p) for p in places]})
        geo = [g for g in b.get("geography") or [] if isinstance(g, dict) and _s(g.get("note")).strip()]
        if geo:
            out.append({"type": "markdown", "text": "### Distances and directions"})
            out.append({"type": "table", "columns": ["From", "To", "Distance or direction", "In the text"],
                        "rows": [[g.get("from"), g.get("to"), {"text": g.get("note"), "section": g.get("section")},
                                  {"text": g.get("quote") or f"Section {g.get('section')}", "section": g.get("section")}]
                                 for g in geo], "keys": [entry_key("geography", g) for g in geo]})
    elif tab == "themes":
        themes = [t for t in b.get("themes") or [] if isinstance(t, dict)]
        if themes:
            out.append({"type": "list", "items": [{"text": t.get("name"),
                                                   "section": _first([e.get("section") for e in t.get("evidence") or []
                                                                      if isinstance(e, dict)]),
                                                   "key": entry_key("theme", t)} for t in themes]})
            for t in themes:
                ev = [e for e in t.get("evidence") or [] if isinstance(e, dict) and str(e.get("quote") or "").strip()]
                if ev:
                    best = max(ev, key=lambda e: len(str(e.get("quote"))))
                    out.append({"type": "quote", "text": best.get("quote"), "section": best.get("section"),
                                "note": t.get("name")})
    elif tab == "science":
        sci = [s for s in b.get("science") or [] if isinstance(s, dict)]
        if sci:
            out.append({"type": "table", "columns": ["Claim", "Section", "Plausible", "Note"],
                        "rows": [[{"text": s.get("claim"), "section": s.get("section")},
                                  {"text": str(s.get("section")), "section": s.get("section")},
                                  s.get("plausible"), s.get("note")] for s in sci],
                        "keys": [entry_key("science", s) for s in sci]})
    elif tab == "timeline":
        events = [e for e in b.get("events") or [] if isinstance(e, dict)]
        if events:
            ranked = sorted(enumerate(events), key=lambda x: (x[1].get("story_order")
                                                               if isinstance(x[1].get("story_order"), int) else 10**6,
                                                               x[0]))
            out.append({"type": "timeline", "items": [{"label": e.get("what"), "when": e.get("when"),
                                                       "section": e.get("section"),
                                                       "who": [names.get(w, w) for w in e.get("who") or []]}
                                                      for _i, e in ranked]})
            rows = []
            for p, e in zip(events, events[1:]):
                a, z = p.get("story_order"), e.get("story_order")
                if isinstance(a, int) and isinstance(z, int) and z < a:
                    rows.append([{"text": e.get("what"), "section": e.get("section")},
                                 {"text": f"Section {e.get('section')}", "section": e.get("section")},
                                 {"text": f"before \"{p.get('what')}\" (section {p.get('section')})",
                                  "section": p.get("section")},
                                 "the telling marks it as earlier" if e.get("placement") == "earlier"
                                 else "nothing in the telling marks the jump"])
            if rows:
                out.append({"type": "table", "columns": ["Event", "Told in", "Happens", "Note"], "rows": rows})
    elif tab == "structure":
        st = b.get("structure") if isinstance(b.get("structure"), dict) else {}
        acts = [a for a in st.get("acts") or [] if isinstance(a, dict)]
        if acts:
            out.append({"type": "table", "columns": ["Act", "From", "To"],
                        "rows": [[{"text": a.get("name"), "section": a.get("from")},
                                  {"text": f"Section {a.get('from')}", "section": a.get("from")},
                                  {"text": f"Section {a.get('to')}", "section": a.get("to")}] for a in acts],
                        "keys": [entry_key("act", a) for a in acts]})
        points = [{"text": f"{t.get('name')}" + (f": {t.get('note')}" if t.get("note") else ""),
                   "section": t.get("section"), "key": entry_key("turning_point", t)}
                  for t in st.get("turning_points") or [] if isinstance(t, dict)]
        for k, label in (("midpoint", "Midpoint"), ("climax", "Climax")):
            m = st.get(k) if isinstance(st.get(k), dict) else {}
            if _section(m.get("section")) is not None:
                points.append({"text": f"{label}" + (f": {m.get('note')}" if m.get("note") else ""),
                               "section": m.get("section"), "key": f"{k}:"})
        if points:
            out.append({"type": "list", "items": points})
        scenes = [s for s in b.get("scenes") or [] if isinstance(s, dict)]
        if scenes:
            out.append({"type": "table", "columns": ["Section", "Scene", "Purpose", "Point of view", "Words"],
                        "rows": [[{"text": str(s.get("section")), "section": s.get("section")}, str(s.get("n")),
                                  {"text": s.get("purpose"), "section": s.get("section")}, s.get("pov"),
                                  str(s.get("words"))] for s in scenes], "keys": [entry_key("scene", s) for s in scenes]})
    elif tab == "voice":
        v = b.get("voice") if isinstance(b.get("voice"), dict) else {}
        rows = [s for s in v.get("by_section") or [] if isinstance(s, dict)]
        if rows:
            out.append({"type": "table", "columns": ["Section", "Point of view", "Tense", "Slips"],
                        "rows": [[{"text": str(s.get("section")), "section": s.get("section")}, s.get("pov"),
                                  s.get("tense"), "; ".join(str(x) for x in s.get("slips") or [])] for s in rows],
                        "keys": [entry_key("voice_section", s) for s in rows]})
        tics = [t for t in v.get("tics") or [] if isinstance(t, dict)]
        if tics:
            out.append({"type": "list", "items": [{"text": f"\"{t.get('phrase')}\", {t.get('count')} times, in "
                                                           f"sections {_sections_text(t.get('sections'))}",
                                                   "section": _first(t.get("sections")),
                                                   "key": entry_key("tic", t)} for t in tics]})
        habits = [h for h in v.get("dialogue_habits") or [] if isinstance(h, dict)]
        if habits:
            out.append({"type": "table", "columns": ["Character", "How they talk"],
                        "rows": [[h.get("character"), h.get("habit")] for h in habits],
                        "keys": [entry_key("habit", h) for h in habits]})
    return clean_blocks(out)
