"""Maps' boards: each one built from the story bible by fixed rules, and laid out here, so the page only draws.

A board's ``spec`` (the page's contract, "maps/1"):

    {"layout": "maps/1", "kind", "width", "height", "note", "legend": [{"text", "cls"}], "draggable": bool,
     "nodes":  [{"id", "label", "lines", "x", "y", "r", "cls", "title", "point"}], circles, edges join them by id
     "edges":  [{"from", "to", "label", "cls", "title"}],
     "lines":  [{"x1", "y1", "x2", "y2", "cls"}],
     "paths":  [{"points": [[x, y], ...], "cls", "smooth", "title"}],
     "rects":  [{"id", "label", "x", "y", "w", "h", "cls", "title", "point"}],
     "texts":  [{"x", "y", "lines": [..], "cls", "anchor", "rotate"}],         rotate: degrees, when given
     "pins":   [{"id", "label", "x", "y", "r", "shape": "dot | diamond | flag", "cls", "title", "point"}]}

A node's ``lines`` is its name as drawn, on one or two lines; nodes are placed so no two name boxes overlap
(``label_box``). ``point`` is ``{"section", "quote", "note"}``: what a click writes to pointer.json. Only the
bible is read here.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import math
import re
from typing import Any

LAYOUT = "maps/1"
KINDS = {"family": "Family tree", "relationships": "Relationship map", "timeline": "Timeline",
         "touchpoints": "Character touchpoints", "arcs": "Arcs", "structure": "Story structure",
         "world": "World map", "threads": "Thread board"}
ALIASES = {"family tree": "family", "tree": "family", "relationship": "relationships", "relationship map": "relationships",
           "interactions": "relationships", "touchpoint": "touchpoints", "character touchpoints": "touchpoints",
           "arc": "arcs", "story structure": "structure", "world map": "world", "map": "world", "places": "world",
           "thread": "threads", "thread board": "threads", "events": "timeline"}
# the words in an ask, in the order they are tried ("relationship map" is a relationship map, not the world map)
WORDS = [(r"\btouch ?points?\b", "touchpoints"), (r"\btime ?lines?\b", "timeline"), (r"\bfamil(y|ies)\b", "family"),
         (r"\btrees?\b", "family"), (r"\brelationships?\b", "relationships"), (r"\binteract\w*", "relationships"),
         (r"\bsupporting\b", "relationships"), (r"\barcs?\b", "arcs"), (r"\bstructure\b", "structure"),
         (r"\bthreads?\b", "threads"), (r"\bworld\b", "world"), (r"\bmaps?\b", "world")]
MAIN_ROLES = {"protagonist", "antagonist"}
SECTION_REF = re.compile(r"^\s*(?:section|scene|chapter|part)?\s*(\d+)\s*[:.,)\-]\s*(.*)$", re.IGNORECASE | re.DOTALL)


class NoKind(ValueError):
    """The ask names no board this version can draw."""


def pick_kind(kind: Any, asked: Any) -> str:
    k = " ".join(str(kind or "").lower().replace("_", " ").split())
    if k:
        if k in KINDS:
            return k
        if k in ALIASES:
            return ALIASES[k]
        for key, label in KINDS.items():
            if k == label.lower():
                return key
    text = str(asked or "").lower()
    for pat, key in WORDS:
        if re.search(pat, text):
            return key
    raise NoKind("Maps draws a family tree, a relationship map, a timeline, character touchpoints, arcs, the story "
                 "structure, a world map or a thread board. Say which one.")


# -- reading the bible ----------------------------------------------------------------------------------

def _list(v: Any) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _ints(v: Any) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n >= 0 and n not in out:
            out.append(n)
    return sorted(out)


def _s(v: Any, n: int = 400) -> str:
    return " ".join(str(v or "").split())[:n]


def _sec(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


class Bible:
    def __init__(self, bible: dict) -> None:
        self.raw = bible if isinstance(bible, dict) else {}
        self.title = _s(self.raw.get("title"), 200) or "this manuscript"
        self.chars = []
        for i, c in enumerate(_list(self.raw.get("characters"))):
            cid = _s(c.get("id"), 80) or f"c{i + 1}"
            self.chars.append({**c, "id": cid, "name": _s(c.get("name"), 80) or cid,
                               "appearances": _ints(c.get("appearances"))})
        self.by_id = {c["id"]: c for c in self.chars}
        self.places = []
        for i, p in enumerate(_list(self.raw.get("places"))):
            pid = _s(p.get("id"), 80) or f"p{i + 1}"
            self.places.append({**p, "id": pid, "name": _s(p.get("name"), 80) or pid,
                                "appearances": _ints(p.get("appearances"))})
        self.events = [e for e in _list(self.raw.get("events")) if _sec(e.get("section")) is not None]
        self.threads = _list(self.raw.get("threads"))
        seen: list[int] = []
        for c in self.chars:
            seen += c["appearances"]
        for p in self.places:
            seen += p["appearances"]
        seen += [_sec(e.get("section")) or 0 for e in self.events]
        for t in self.threads:
            seen += _ints(t.get("sections"))
        total = _sec(self.raw.get("sections_total")) or 0
        self.n = max([total] + seen) or 1
        self.first = 0 if 0 in seen else 1           # Manuscript's section 0 is the text before the first chapter

    def char(self, ref: Any) -> dict | None:
        r = _s(ref, 120).lower()
        if not r:
            return None
        if r in self.by_id:
            return self.by_id[r]
        for c in self.chars:
            if r == c["name"].lower() or r in [_s(a).lower() for a in c.get("aliases") or [] if isinstance(a, str)]:
                return c
        return None

    def named_in(self, asked: Any) -> list[dict]:
        """The characters an ask names: by full name, alias, id or a first name only one of them has."""
        text = " " + re.sub(r"[^\w']+", " ", re.sub(r"['’]s\b", "", str(asked or "").lower())) + " "
        firsts: dict[str, list[dict]] = {}
        for c in self.chars:
            firsts.setdefault(c["name"].split()[0].lower(), []).append(c)
        out = []
        for c in self.chars:
            names = [c["name"], c["id"]] + [a for a in c.get("aliases") or [] if isinstance(a, str)]
            first = c["name"].split()[0].lower()
            if len(firsts.get(first, [])) == 1:
                names.append(first)
            if any(f" {_s(n).lower()} " in text for n in names if len(_s(n)) > 1):
                out.append(c)
        return out

    def words(self) -> dict[int, int]:
        """Word counts per section, when the bible carries them (``sections[]`` or ``summary.by_section[]``)."""
        out: dict[int, int] = {}
        summary = self.raw.get("summary") if isinstance(self.raw.get("summary"), dict) else {}
        for row in _list(self.raw.get("sections")) + _list(summary.get("by_section")):
            n, w = _sec(row.get("n", row.get("section"))), row.get("words")
            if n is not None and isinstance(w, (int, float)) and w > 0:
                out[n] = int(w)
        return out

    def summary(self, n: int) -> str:
        summary = self.raw.get("summary") if isinstance(self.raw.get("summary"), dict) else {}
        for row in _list(summary.get("by_section")):
            if _sec(row.get("section")) == n:
                return _s(row.get("summary"), 300)
        return ""


def _role(c: dict) -> str:
    r = _s(c.get("role"), 40).lower()
    return r if r in ("protagonist", "antagonist", "ally") else "supporting"


def _wrap(text: str, width: int, lines: int = 3) -> list[str]:
    words, out, cur = _s(text, 600).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][: max(1, width - 3)].rstrip() + "..."
    return out


def _point(section: Any, quote: Any = "", note: Any = "") -> dict | None:
    n = _sec(section)
    if n is None:
        return None
    return {"section": n, "quote": _s(quote, 200), "note": _s(note, 300)}


def _blank(kind: str, width: int = 480, height: int = 240) -> dict:
    return {"layout": LAYOUT, "kind": kind, "width": width, "height": height, "note": "", "legend": [],
            "draggable": False, "nodes": [], "edges": [], "lines": [], "paths": [], "rects": [], "texts": [],
            "pins": []}


def _empty(spec: dict, line: str) -> dict:
    spec["note"] = line
    spec["texts"].append({"x": spec["width"] / 2, "y": spec["height"] / 2, "lines": _wrap(line, 64, 4), "cls": "muted",
                          "anchor": "middle"})
    return spec


def _round(spec: dict) -> dict:
    for key in ("nodes", "pins", "rects", "texts"):
        for it in spec[key]:
            for f in ("x", "y", "w", "h", "r"):
                if isinstance(it.get(f), float):
                    it[f] = round(it[f], 1)
    for ln in spec["lines"]:
        for f in ("x1", "y1", "x2", "y2"):
            ln[f] = round(float(ln[f]), 1)
    for p in spec["paths"]:
        p["points"] = [[round(float(x), 1), round(float(y), 1)] for x, y in p["points"]]
    spec["width"], spec["height"] = int(math.ceil(spec["width"])), int(math.ceil(spec["height"]))
    return spec


# -- sizes of drawn text, as the page draws it (13 pixel names, 11 pixel small text) ---------------------

NAME_PX = 7.4          # about one character of a node name, a little wide so the boxes are never short
LINE_PX = 16.5         # one line of a node name
SMALL_PX = 6.4         # one character of small text
PAD = 10               # the least space between two name boxes


def name_lines(name: Any) -> list[str]:
    """A node name on one or two lines of about 14 characters, centred under the node by the page."""
    return _wrap(name, 14, 2) or [""]


def extents(r: float, lines: list[str]) -> tuple[float, float, float]:
    """(half width, above the centre, below the centre) of a node and the name under it."""
    hw = max(r, max(len(s) for s in lines) * NAME_PX / 2 + 4)
    return hw, r + 2, r + 6 + len(lines) * LINE_PX


def label_box(nd: dict) -> tuple[float, float, float, float]:
    """The box (x0, y0, x1, y1) a drawn node and its name take up."""
    r = float(nd.get("r") or 12)
    hw, up, down = extents(r, nd.get("lines") or [str(nd.get("label") or nd.get("id") or "")])
    return nd["x"] - hw, nd["y"] - up, nd["x"] + hw, nd["y"] + down


# -- force layout (the same answer every time for the same input) -------------------------------------

def _clash(a: tuple, b: tuple) -> tuple[float, float, float]:
    """How far two placed boxes (x, y, hw, up, down) overlap across and down, with PAD between them, and how far
    their centres are short of the least distance (the taller box's height plus 10). Positive means too close."""
    (xa, ya, wa, ua, da), (xb, yb, wb, ub, db) = a, b
    ox = wa + wb + PAD - abs(xb - xa)
    oy = (ya + da + PAD) - (yb - ub) if yb >= ya else (yb + db + PAD) - (ya - ua)
    dmin = max(ua + da, ub + db) + 10
    return ox, oy, dmin - math.hypot(xb - xa, yb - ya)


def force(ids: list[str], links: list[tuple[str, str, float]], sizes: dict[str, tuple[float, float, float]],
          width: float, height: float, margin: float = 40) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Places nodes so no two name boxes overlap and every box sits inside the drawing, ``margin`` in from its
    edges. ``sizes`` is each node's (half width, above, below). Returns (positions, width, height): the drawing
    grows when the cast does not fit in it."""
    n = len(ids)
    if n == 0:
        return {}, width, height
    if n == 1:
        return {ids[0]: (width / 2, height / 2)}, width, height
    cx, cy = width / 2, height / 2
    pos = {}
    for i, k in enumerate(ids):
        a = 2 * math.pi * i / n + 0.3
        pos[k] = [cx + math.cos(a) * width / 3, cy + math.sin(a) * height / 3]
    k = math.sqrt((width - 2 * margin) * (height - 2 * margin) / n) * 0.8
    t = width / 8
    iters = min(800, 200 + 8 * n)                    # more rounds for a bigger cast
    cool = 0.05 ** (1 / iters)
    for _ in range(iters):
        disp = {key: [0.0, 0.0] for key in ids}
        for i in range(n):
            a = ids[i]
            for j in range(i + 1, n):
                b = ids[j]
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) or 0.01
                f = k * k / d
                disp[a][0] += dx / d * f
                disp[a][1] += dy / d * f
                disp[b][0] -= dx / d * f
                disp[b][1] -= dy / d * f
        for a, b, w in links:
            if a not in pos or b not in pos or a == b:
                continue
            dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 0.01
            f = d * d / k * min(max(w, 1.0), 3.0) ** 0.5
            disp[a][0] -= dx / d * f
            disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f
            disp[b][1] += dy / d * f
        for key in ids:
            disp[key][0] -= (pos[key][0] - cx) * 0.6
            disp[key][1] -= (pos[key][1] - cy) * 0.6
            d = math.hypot(*disp[key]) or 0.01
            step = min(d, t)
            pos[key][0] += disp[key][0] / d * step
            pos[key][1] += disp[key][1] / d * step
        t = max(t * cool, 1.0)
    hw = max(s[0] for s in sizes.values())
    up = max(s[1] for s in sizes.values())
    down = max(s[2] for s in sizes.values())

    def fit(w: float, h: float) -> None:
        """Scale and centre the cloud into the drawing, the largest box's room kept at every edge."""
        xs, ys = [p[0] for p in pos.values()], [p[1] for p in pos.values()]
        w0, h0 = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
        iw, ih = max(w - 2 * margin - 2 * hw, 1), max(h - 2 * margin - up - down, 1)
        s = min(iw / w0, ih / h0)
        ox, oy = margin + hw + (iw - w0 * s) / 2, margin + up + (ih - h0 * s) / 2
        x0, y0 = min(xs), min(ys)
        for p in pos.values():
            p[0], p[1] = ox + (p[0] - x0) * s, oy + (p[1] - y0) * s

    def settle(w: float, h: float) -> bool:
        """Push overlapping boxes apart, kept inside the drawing. True when none overlap."""
        for _ in range(400):
            moved = False
            for i in range(n):
                a = ids[i]
                for j in range(i + 1, n):
                    b = ids[j]
                    pa, pb = pos[a], pos[b]
                    ox, oy, od = _clash((pa[0], pa[1]) + sizes[a], (pb[0], pb[1]) + sizes[b])
                    if not ((ox > 0 and oy > 0) or od > 0):
                        continue
                    moved = True
                    dx, dy = pb[0] - pa[0], pb[1] - pa[1]
                    if dx == 0 and dy == 0:
                        dx, dy = (1.0, 0.3) if (i + j) % 2 else (-0.3, 1.0)
                    if ox > 0 and oy > 0:                  # the boxes overlap: part them the shorter way
                        if ox <= oy:
                            m = (ox / 2 + 0.5) * (1 if dx >= 0 else -1)
                            pa[0] -= m
                            pb[0] += m
                        else:
                            m = (oy / 2 + 0.5) * (1 if dy >= 0 else -1)
                            pa[1] -= m
                            pb[1] += m
                    else:                                  # only the centres are too close: part them along the line
                        d = math.hypot(dx, dy)
                        m = od / 2 + 0.5
                        pa[0] -= dx / d * m
                        pa[1] -= dy / d * m
                        pb[0] += dx / d * m
                        pb[1] += dy / d * m
            for key in ids:
                sw, su, sd = sizes[key]
                p = pos[key]
                p[0] = min(max(p[0], margin + sw), w - margin - sw)
                p[1] = min(max(p[1], margin + su), h - margin - sd)
            if not moved:
                return True
        return False

    fit(width, height)
    for _ in range(8):
        if settle(width, height):
            break
        for p in pos.values():                          # no room: a bigger drawing, the same picture
            p[0], p[1] = p[0] * 1.15, p[1] * 1.15
        width, height = width * 1.15, height * 1.15
    # fit to the box: the whole cast centred in the drawing
    x0 = min(pos[i][0] - sizes[i][0] for i in ids)
    x1 = max(pos[i][0] + sizes[i][0] for i in ids)
    y0 = min(pos[i][1] - sizes[i][1] for i in ids)
    y1 = max(pos[i][1] + sizes[i][2] for i in ids)
    sx, sy = (width - x1 - x0) / 2, (height - y1 - y0) / 2
    return {key: (p[0] + sx, p[1] + sy) for key, p in pos.items()}, width, height


def _side(n: int, sizes: dict[str, tuple[float, float, float]] | None = None) -> int:
    """The side of a square drawing that fits n nodes with room to spare for their names."""
    area = sum((2 * w + PAD) * (u + d + PAD) for w, u, d in (sizes or {}).values())
    return int(max(460, 130 * math.sqrt(max(n, 1)), math.sqrt(area * 3.2)))


# -- the eight boards ---------------------------------------------------------------------------------

def family(b: Bible, asked: str = "", r: float = 16) -> dict:
    """The family trees. *r* is the circle's radius: 16 on the chart, bigger in the atlas, where each circle holds a
    portrait."""
    spec = _blank("family")
    parent_of: set[tuple[str, str]] = set()
    partners: set[tuple[str, str]] = set()
    for c in b.chars:
        fam = c.get("family") if isinstance(c.get("family"), dict) else {}
        for ref in fam.get("parents") or []:
            p = b.char(ref)
            if p and p["id"] != c["id"]:
                parent_of.add((p["id"], c["id"]))
        for ref in fam.get("children") or []:
            ch = b.char(ref)
            if ch and ch["id"] != c["id"]:
                parent_of.add((c["id"], ch["id"]))
        for ref in fam.get("partners") or []:
            p = b.char(ref)
            if p and p["id"] != c["id"]:
                partners.add(tuple(sorted((c["id"], p["id"]))))
    group = {c["id"]: c["id"] for c in b.chars}

    def root(x: str) -> str:
        while group[x] != x:
            group[x] = group[group[x]]
            x = group[x]
        return x
    for a, c in parent_of | partners:
        group[root(a)] = root(c)
    comps: dict[str, list[str]] = {}
    for c in b.chars:
        comps.setdefault(root(c["id"]), []).append(c["id"])
    trees = [m for m in comps.values() if len(m) >= 2]
    if not trees:
        return _empty(spec, "Nothing to draw yet: the bible has no family of two or more people.")
    gap_y, top = 130 + 2 * (r - 16), 80 + (r - 16)
    sib_gap, partner_gap = 32, 16                   # space between two name boxes in a row
    lines = {c["id"]: name_lines(c["name"]) for c in b.chars}
    half = {i: extents(r, ln)[0] for i, ln in lines.items()}
    left = 40.0
    height = 0.0
    for members in trees:
        ms = set(members)
        gen = {m: 0 for m in members}
        for _ in range(len(members) + 1):
            moved = False
            for p, ch in parent_of:
                if p in ms and gen[ch] < gen[p] + 1:
                    gen[ch] = gen[p] + 1
                    moved = True
            for a, c in partners:
                if a in ms and gen[a] != gen[c]:
                    gen[a] = gen[c] = max(gen[a], gen[c])
                    moved = True
            if not moved:
                break
        layers: dict[int, list[str]] = {}
        for m in members:
            layers.setdefault(gen[m], []).append(m)
        order: dict[str, float] = {}
        rows = []
        for g in sorted(layers):
            def key(m: str) -> tuple:
                ps = [order[p] for p, ch in parent_of if ch == m and p in order]
                c = b.by_id[m]
                return (sum(ps) / len(ps) if ps else 0, c.get("first_seen") or 999, c["name"])
            row: list[str] = []
            for m in sorted(layers[g], key=key):
                if m in row:
                    continue
                row.append(m)
                for a, c in sorted(partners):
                    other = c if a == m else a if c == m else None
                    if other and other in layers[g] and other not in row:
                        row.append(other)
            for i, m in enumerate(row):
                order[m] = i
            rows.append((g, row))
        def gaps(row: list[str]) -> list[float]:
            return [partner_gap if tuple(sorted((row[i], row[i + 1]))) in partners else sib_gap
                    for i in range(len(row) - 1)]
        tree_w = max(sum(2 * half[m] for m in row) + sum(gaps(row)) for _g, row in rows)
        names = [b.by_id[m]["name"].split() for m in members]
        lasts = [n[-1] for n in names if len(n) > 1]
        fam_name = max(set(lasts), key=lasts.count) if lasts else b.by_id[members[0]]["name"]
        spec["texts"].append({"x": left + tree_w / 2, "y": 36, "lines": [f"The {fam_name} family" if lasts
                                                                        else f"{fam_name}'s family"],
                              "cls": "head", "anchor": "middle"})
        xy = {}
        for g, row in rows:                         # siblings spaced by their name boxes, partners close
            between = gaps(row)
            x = left + (tree_w - sum(2 * half[m] for m in row) - sum(between)) / 2
            for i, m in enumerate(row):
                xy[m] = (x + half[m], top + g * gap_y)
                x += 2 * half[m] + (between[i] if i < len(between) else 0)
        for m, (x, y) in xy.items():
            c = b.by_id[m]
            spec["nodes"].append({"id": m, "label": c["name"], "lines": lines[m], "x": x, "y": y, "r": r,
                                  "cls": _role(c),
                                  "title": f"{c['name']}" + (f", {_s(c.get('role'))}" if c.get("role") else ""),
                                  "point": _point(c.get("first_seen") or (c["appearances"] or [None])[0],
                                                  c["name"], f"{c['name']} first comes in here.")})
            height = max(height, y + extents(r, lines[m])[2] + 20)
        for a, c in sorted(partners):                # a short bar between the two circles
            if a in xy and c in xy:
                p, q = (a, c) if xy[a][0] < xy[c][0] else (c, a)
                spec["paths"].append({"points": [[xy[p][0] + r, xy[p][1]], [xy[q][0] - r, xy[q][1]]],
                                      "cls": "partner", "smooth": False, "title": "partners"})
        for ch in members:
            ps = sorted(p for p, kid in parent_of if kid == ch and p in xy)
            if not ps:
                continue
            pair = len(ps) == 2 and tuple(ps) in partners and xy[ps[0]][1] == xy[ps[1]][1]
            # from the middle of the partners' bar, or from under a lone parent's name
            anchors = [((xy[ps[0]][0] + xy[ps[1]][0]) / 2, xy[ps[0]][1], 0)] if pair else \
                [(xy[p][0], xy[p][1], extents(r, lines[p])[2]) for p in ps]
            cx, cy = xy[ch]
            for ax, ay, drop in anchors:
                mid = cy - gap_y / 2 + 6
                spec["paths"].append({"points": [[ax, ay + drop], [ax, mid], [cx, mid], [cx, cy - r]],
                                      "cls": "parent", "smooth": False, "title": ""})
        left += tree_w + 60
    spec["width"], spec["height"] = left - 20, max(height, 240)
    spec["legend"] = [{"text": "lines down: parent to child", "cls": "parent"},
                      {"text": "line across: partners", "cls": "partner"}]
    return _round(spec)


def relationships(b: Bible, asked: str = "") -> dict:
    spec = _blank("relationships")
    text = str(asked or "").lower()
    supporting = bool(re.search(r"\bsupporting\b", text))
    shared = supporting or bool(re.search(r"\binteract\w*|\bmeets?\b|\bwho\b.*\bwhom\b", text))
    chars = [c for c in b.chars if not supporting or _s(c.get("role")).lower() not in MAIN_ROLES]
    ids = {c["id"] for c in chars}
    pairs: dict[tuple[str, str], dict] = {}
    for c in chars:
        for r in _list(c.get("relationships")):
            o = b.char(r.get("to"))
            if not o or o["id"] not in ids or o["id"] == c["id"]:
                continue
            key = tuple(sorted((c["id"], o["id"])))
            e = pairs.setdefault(key, {"kinds": [], "notes": [], "shared": []})
            kind = _s(r.get("kind"), 40)
            if kind and kind not in e["kinds"]:
                e["kinds"].append(kind)
            if _s(r.get("note")):
                e["notes"].append(_s(r.get("note"), 200))
    if shared:
        for i, a in enumerate(chars):
            for c in chars[i + 1:]:
                both = sorted(set(a["appearances"]) & set(c["appearances"]))
                if both:
                    key = tuple(sorted((a["id"], c["id"])))
                    pairs.setdefault(key, {"kinds": [], "notes": [], "shared": []})["shared"] = both
    if not chars:
        return _empty(spec, "Nothing to draw yet: the bible has no supporting characters." if supporting
                      else "Nothing to draw yet: the bible has no characters.")
    radius = {c["id"]: 8 + 3 * math.sqrt(len(c["appearances"])) for c in chars}
    lines = {c["id"]: name_lines(c["name"]) for c in chars}
    sizes = {i: extents(radius[i], lines[i]) for i in radius}
    side = _side(len(chars), sizes)
    links = [(a, c, len(e["shared"]) or 1.0) for (a, c), e in pairs.items()]
    pos, spec["width"], spec["height"] = force([c["id"] for c in chars], links, sizes, side + 80, side)
    for c in chars:
        x, y = pos[c["id"]]
        n = len(c["appearances"])
        role = _s(c.get("role"), 40)
        spec["nodes"].append({"id": c["id"], "label": c["name"], "lines": lines[c["id"]], "x": x, "y": y,
                              "r": radius[c["id"]], "cls": _role(c),
                              "title": c["name"] + (f", {role}" if role else "") + f". In {n} section" +
                              ("" if n == 1 else "s") + ".",
                              "point": _point(c.get("first_seen") or (c["appearances"] or [None])[0], c["name"],
                                              f"{c['name']} first comes in here.")})
    for (a, c), e in sorted(pairs.items()):
        both = e["shared"]
        label = " / ".join(e["kinds"]) or ("both in " + ", ".join(map(str, both[:4])) + ("..." if len(both) > 4 else ""))
        title = "; ".join(e["notes"]) or label
        if both:
            title += f". Both in section{'s' if len(both) > 1 else ''} {', '.join(map(str, both))}."
        spec["edges"].append({"from": a, "to": c, "label": label, "cls": "rel" if e["kinds"] else "share",
                              "title": title})
    spec["draggable"] = True
    if supporting:
        linked = {x for k in pairs for x in k}
        alone = [c["name"] for c in chars if c["id"] not in linked]
        spec["note"] = (f"{len(chars)} supporting characters. {len(pairs)} pair" + ("s" if len(pairs) != 1 else "") +
                        " share a section or a tie." + (f" Never with another: {', '.join(alone)}." if alone else ""))
    spec["legend"] = [{"text": "circle size: how many sections they are in", "cls": "muted"},
                      {"text": "solid line: a tie the story names", "cls": "rel"}]
    if shared:
        spec["legend"].append({"text": "dashed line: in the same section", "cls": "share"})
    return _round(spec)


def _events(b: Bible) -> tuple[list[dict], list[dict]]:
    """(story order, telling order). Telling order is section, then order. Story order is ``story_order`` when the
    bible has it, else ``order`` when it numbers every event once (not again in each section)."""
    told = sorted(b.events, key=lambda e: (_sec(e.get("section")) or 0, _sec(e.get("order")) or 0))
    has = [e for e in b.events if isinstance(e.get("story_order"), (int, float))]
    if has and len(has) == len(b.events):
        return sorted(told, key=lambda e: e["story_order"]), told
    orders = [e.get("order") for e in b.events]
    if all(isinstance(o, (int, float)) for o in orders) and len(set(orders)) == len(orders):
        return sorted(told, key=lambda e: e["order"]), told
    return told, told


def timeline(b: Bible, asked: str = "") -> dict:
    spec = _blank("timeline")
    story, told = _events(b)
    if not told:
        return _empty(spec, "Nothing to draw yet: the bible has no events.")
    gap, left = 100, 90                    # events never closer than 90 pixels: the rail grows with the count
    tier = 3 * 16.25 + 18                  # a second row of labels sits this much further from the rail
    two = [id(e) for e in story] != [id(e) for e in told]
    # (events, rail y, name, side): side -1 labels above, 1 below, 0 alternating above and below
    rails = [(story, 190, "In story order", -1), (told, 310, "As told, by section", 1)] if two else \
        [(told, 120, "", 0)]
    spots: dict[int, list[tuple[float, float]]] = {}
    joins, right = [], left + gap * (len(told) - 1)
    for events, y, name, side in rails:
        ends: dict[tuple[bool, int], float] = {}   # the right edge of the last label in each row
        texts, leads = [], []
        for i, e in enumerate(events):
            x = left + i * gap
            up = side < 0 or (side == 0 and i % 2 == 0)
            row = i % 2 if side else 0             # one rail per side: alternate a near and a far row
            what = _s(e.get("what"), 300)
            sec = _sec(e.get("section"))
            lines = _wrap(what, 18)
            small = f"section {sec}"
            half = max(max(len(s) for s in lines) * NAME_PX, len(small) * SMALL_PX) / 2 + 4
            lx = max(x, ends.get((up, row), -1e9) + 8 + half)    # still in the way: shift along the rail
            ends[(up, row)] = lx + half
            right = max(right, lx + half)
            off = row * tier
            if up:
                texts.append({"x": lx, "y": y - 14 - off, "lines": [small], "cls": "small", "anchor": "middle"})
                texts.append({"x": lx, "y": y - 30 - off - (len(lines) - 1) * 16.25, "lines": lines, "cls": "label",
                              "anchor": "middle"})
            else:
                texts.append({"x": lx, "y": y + 24 + off, "lines": [small], "cls": "small", "anchor": "middle"})
                texts.append({"x": lx, "y": y + 42 + off, "lines": lines, "cls": "label", "anchor": "middle"})
            if row or abs(lx - x) > 1:             # a thin line from the dot to a label that is not right by it
                edge = 10 + off if up else 12 + off
                leads.append({"x1": x, "y1": y - 8 if up else y + 8, "x2": lx, "y2": y - edge if up else y + edge,
                              "cls": "lead"})
            when = _s(e.get("when"), 80)
            eid = _s(e.get("id"), 40) or f"e{i + 1}"
            spec["pins"].append({"id": eid if side <= 0 else f"{eid}-told", "label": what, "x": x, "y": y, "r": 7,
                                 "shape": "dot", "cls": "event", "title": f"Section {sec}: {what}" +
                                 (f" ({when})" if when else ""), "point": _point(sec, what, what)})
            spots.setdefault(id(e), []).append((x, y))
        spec["lines"] += leads
        spec["texts"] += texts
        if name:
            spec["texts"].append({"x": 16, "y": y - 160 if side < 0 else y + 172, "lines": [name], "cls": "head",
                                  "anchor": "start"})
    width = right + 40
    for events, y, name, side in rails:
        spec["lines"].append({"x1": left - 30, "y1": y, "x2": width - 30, "y2": y, "cls": "rail"})
    for (x1, y1), (x2, y2) in (v for v in spots.values() if len(v) == 2):
        joins.append({"x1": x1, "y1": y1 + 8, "x2": x2, "y2": y2 - 8, "cls": "join"})
    spec["lines"] = joins + spec["lines"]          # the joining lines first, so they are drawn behind
    spec["width"], spec["height"] = width, 500 if two else 240
    spec["note"] = ("The story order and the telling order differ: the thin lines join each event's two places."
                    if two else "Events in the order the story tells them.")
    return _round(spec)


def touchpoints(b: Bible, asked: str = "") -> dict:
    spec = _blank("touchpoints")
    named = b.named_in(asked)
    chars = sorted(named or [c for c in b.chars if c["appearances"]],
                   key=lambda c: (c.get("first_seen") or (c["appearances"] or [999])[0], -len(c["appearances"]),
                                  c["name"]))
    if not chars:
        return _empty(spec, "Nothing to draw yet: the bible has no characters with sections.")
    secs = list(range(b.first, b.n + 1))
    many = len(secs) > 12                     # past 12 sections: narrower cells, headers turned 45 degrees
    cell, top, gutter = (26 if many else 38), (78 if many else 60), 12
    shown = {c["id"]: c["name"] if len(c["name"]) <= 22 else c["name"][:21].rstrip() + "..." for c in chars}
    left = 16 + max(len(n) for n in shown.values()) * NAME_PX + gutter
    xs = {s: left + (i + 0.5) * cell for i, s in enumerate(secs)}
    ys = {c["id"]: top + (i + 0.5) * cell for i, c in enumerate(chars)}
    for s, x in xs.items():
        spec["texts"].append({"x": x - 3, "y": top - 8, "lines": [str(s)], "cls": "small", "anchor": "start",
                              "rotate": -45} if many else
                             {"x": x, "y": top - 14, "lines": [str(s)], "cls": "small", "anchor": "middle"})
        spec["lines"].append({"x1": x, "y1": top, "x2": x, "y2": top + len(chars) * cell, "cls": "grid"})
    spec["texts"].append({"x": left, "y": top - (56 if many else 34), "lines": ["Sections"], "cls": "small",
                          "anchor": "start"})
    for c in chars:                           # names right-aligned against a fixed gutter
        y = ys[c["id"]]
        spec["texts"].append({"x": left - gutter, "y": y + 4, "lines": [shown[c["id"]]], "cls": "label",
                              "anchor": "end"})
        spec["lines"].append({"x1": left, "y1": y, "x2": left + len(secs) * cell, "y2": y, "cls": "grid"})
    for s in secs:
        here = [c for c in chars if s in c["appearances"]]
        if len(here) >= 2:
            spec["lines"].append({"x1": xs[s], "y1": min(ys[c["id"]] for c in here), "x2": xs[s],
                                  "y2": max(ys[c["id"]] for c in here), "cls": "join"})
        for c in here:
            others = [o["name"].split()[0] for o in here if o is not c]
            spec["pins"].append({"id": f"{c['id']}@{s}", "label": c["name"], "x": xs[s], "y": ys[c["id"]], "r": 7,
                                 "shape": "dot", "cls": _role(c),
                                 "title": f"{c['name']} in section {s}" + (f", with {', '.join(others)}" if others
                                                                           else ", alone"),
                                 "point": _point(s, c["name"], f"{c['name']} in section {s}.")})
    spec["width"], spec["height"] = left + len(secs) * cell + 40, top + len(chars) * cell + 30
    spec["note"] = "A dot where a character is in a section; a line down a column joins everyone in that section."
    return _round(spec)


def arcs(b: Bible, asked: str = "") -> dict:
    spec = _blank("arcs")
    named = b.named_in(asked)
    chars = named or [c for c in b.chars if len(c["appearances"]) >= 4]
    chars = [c for c in chars if any(_sec(a.get("section")) is not None for a in _list(c.get("arc")))]
    if not chars:
        return _empty(spec, "Nothing to draw yet: no character in four or more sections has an arc in the bible.")
    sw, left, band = 72, 60, 150
    secs = list(range(b.first, b.n + 1))
    width = left + len(secs) * sw + 40

    def sx(s: int) -> float:
        return left + (s - b.first + 0.5) * sw
    chars = sorted(chars, key=lambda c: (c.get("first_seen") or 999, c["name"]))
    for i, c in enumerate(chars):
        top = 30 + i * band
        cls = f"line-{i % 6}"
        spec["texts"].append({"x": 12, "y": top + 4, "lines": [c["name"]], "cls": "head", "anchor": "start"})
        want, fear = _s(c.get("wants"), 200), _s(c.get("fears"), 200)
        chars_wide = max(20, int((width - 24) / 6.6))
        if want:
            spec["texts"].append({"x": 12, "y": top + 22, "lines": _wrap("Wants " + want, chars_wide, 1),
                                  "cls": "small", "anchor": "start"})
        if fear:
            spec["texts"].append({"x": 12, "y": top + band - 18, "lines": _wrap("Fears " + fear, chars_wide, 1),
                                  "cls": "small", "anchor": "start"})
        beats = sorted([(_sec(a.get("section")), _s(a.get("beat"), 300)) for a in _list(c.get("arc"))
                        if _sec(a.get("section")) is not None], key=lambda x: x[0])
        lo, hi = top + band - 36, top + 40
        pts = []
        same: dict[int, int] = {}
        for k, (s, beat) in enumerate(beats):
            x = sx(s) + 10 * same.get(s, 0)
            same[s] = same.get(s, 0) + 1
            y = lo + (hi - lo) * (k / (len(beats) - 1) if len(beats) > 1 else 0.5)     # climbs, beat by beat
            pts.append([x, y])
            spec["pins"].append({"id": f"{c['id']}-beat-{k + 1}", "label": beat or c["name"], "x": x, "y": y, "r": 6,
                                 "shape": "dot", "cls": cls, "title": f"Section {s}: {beat}",
                                 "point": _point(s, c["name"], beat)})
        if len(pts) > 1:
            spec["paths"].append({"points": pts, "cls": cls, "smooth": True, "title": c["name"]})
        spec["lines"].append({"x1": left, "y1": top + band - 30, "x2": width - 30, "y2": top + band - 30,
                              "cls": "grid"})
        spec["legend"].append({"text": f"{c['name']}: wants {want or 'not given'}; fears {fear or 'not given'}",
                               "cls": cls})
    bottom = 30 + len(chars) * band
    for s in secs:
        spec["texts"].append({"x": sx(s), "y": bottom + 6, "lines": [str(s)], "cls": "small", "anchor": "middle"})
    spec["texts"].append({"x": left, "y": bottom + 26, "lines": ["Sections"], "cls": "small", "anchor": "start"})
    spec["width"], spec["height"] = width, bottom + 40
    spec["note"] = "Each curve climbs through a character's arc, one point per beat. Hover a point for the beat."
    return _round(spec)


def _refs(items: Any, fallback: int | None) -> list[tuple[int | None, str]]:
    """Setups and payoffs, as (section, text): "section 4: the badge" or {section, text}."""
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            out.append((_sec(it.get("section")) if it.get("section") is not None else fallback,
                        _s(it.get("text") or it.get("note") or it.get("what"), 300)))
            continue
        m = SECTION_REF.match(str(it or ""))
        out.append((int(m.group(1)), _s(m.group(2), 300)) if m else (fallback, _s(it, 300)))
    return [(s, t) for s, t in out if s is not None]


def structure(b: Bible, asked: str = "") -> dict:
    spec = _blank("structure")
    sw, left = 64, 50
    secs = list(range(b.first, b.n + 1))
    words = b.words()
    per = {s: words.get(s, 0) for s in secs} if words else \
        {s: sum(1 for e in b.events if _sec(e.get("section")) == s) for s in secs}
    unit = "words" if words else "events"
    threads = b.threads
    lane_h, pin_top = 22, 50
    base = pin_top + len(threads) * lane_h + 30 + 150
    top = max(per.values()) or 1

    def sx(s: int) -> float:
        return left + (s - b.first + 0.5) * sw
    for s in secs:
        h = 150 * per[s] / top
        summary = b.summary(s)
        spec["rects"].append({"id": f"section-{s}", "label": f"Section {s}", "x": sx(s) - sw * 0.32, "y": base - h,
                              "w": sw * 0.64, "h": max(h, 1), "cls": "bar",
                              "title": f"Section {s}: {per[s]} {unit}" + (f". {summary}" if summary else ""),
                              "point": _point(s, "", f"Section {s}" + (f": {summary}" if summary else ""))})
        spec["texts"].append({"x": sx(s), "y": base - h - 6, "lines": [str(per[s])], "cls": "small",
                              "anchor": "middle"})
        spec["texts"].append({"x": sx(s), "y": base + 16, "lines": [str(s)], "cls": "small", "anchor": "middle"})
    spec["lines"].append({"x1": left, "y1": base, "x2": left + len(secs) * sw, "y2": base, "cls": "axis"})
    for i, t in enumerate(threads):
        y = pin_top + i * lane_h
        cls = f"line-{i % 6}"
        name = _s(t.get("name"), 80) or f"Thread {i + 1}"
        tsecs = _ints(t.get("sections"))
        for kind, items, shape in (("setup", t.get("setups"), "diamond"), ("payoff", t.get("payoffs"), "dot")):
            fall = (tsecs[0] if kind == "setup" else tsecs[-1]) if tsecs else None
            for k, (s, what) in enumerate(_refs(items, fall)):
                if s < b.first or s > b.n:
                    continue
                spec["pins"].append({"id": f"{_s(t.get('id'), 60) or i}-{kind}-{k + 1}", "label": what or name,
                                     "x": sx(s) + (6 if kind == "payoff" else -6), "y": y, "r": 6, "shape": shape,
                                     "cls": cls, "title": f"{name}, {kind} in section {s}: {what}",
                                     "point": _point(s, what, f"{name}: {kind}")})
        spec["legend"].append({"text": name + (" (open)" if t.get("open") else ""), "cls": cls})
    spec["legend"] += [{"text": "diamond: a setup; dot: a payoff", "cls": "muted"}]
    spec["width"], spec["height"] = left + len(secs) * sw + 30, base + 40
    spec["note"] = ("Bars: words per section." if words else
                    "Bars: events per section. The bible has no word counts yet, so bars count events instead.")
    if threads:
        spec["texts"].append({"x": left, "y": pin_top - 26, "lines": ["Setups and payoffs"], "cls": "small",
                              "anchor": "start"})
    return _round(spec)


def world(b: Bible, asked: str = "") -> dict:
    spec = _blank("world")
    places = b.places
    if not places:
        return _empty(spec, "Nothing to draw yet: the bible has no places.")
    radius = {p["id"]: 9 + 3 * math.sqrt(len(p["appearances"])) for p in places}
    lines = {p["id"]: name_lines(p["name"]) for p in places}
    sizes = {i: extents(radius[i], lines[i]) for i in radius}
    links = []
    for i, a in enumerate(places):
        for c in places[i + 1:]:
            both = sorted(set(a["appearances"]) & set(c["appearances"]))
            if both:
                links.append((a["id"], c["id"], float(len(both))))
                spec["edges"].append({"from": a["id"], "to": c["id"], "label": "",
                                      "cls": f"travel w{min(len(both), 3)}",
                                      "title": f"{a['name']} and {c['name']}: both in section"
                                               f"{'s' if len(both) > 1 else ''} {', '.join(map(str, both))}"})
    side = _side(len(places), sizes)
    pos, spec["width"], spec["height"] = force([p["id"] for p in places], links, sizes, side + 80, side)
    for p in places:
        x, y = pos[p["id"]]
        kind = _s(p.get("kind"), 40)
        spec["nodes"].append({"id": p["id"], "label": p["name"], "lines": lines[p["id"]], "x": x, "y": y,
                              "r": radius[p["id"]],
                              "cls": "place", "title": p["name"] + (f", {kind}" if kind else "") +
                              (f". {_s(p.get('notes'), 200)}" if p.get("notes") else ""),
                              "point": _point((p["appearances"] or [None])[0], p["name"], _s(p.get("notes"), 300) or
                                              f"{p['name']} first comes in here.")})
    spec["draggable"] = True
    spec["note"] = ("A schematic, not geography: places that share a section sit close. Drag them where you see them "
                    "and Save layout.")
    spec["legend"] = [{"text": "thicker line: in more sections together", "cls": "travel"}]
    return _round(spec)


def threads(b: Bible, asked: str = "") -> dict:
    spec = _blank("threads")
    ts = b.threads
    if not ts:
        return _empty(spec, "Nothing to draw yet: the bible has no threads.")
    sw, left, lane, top = 60, 150, 56, 50
    secs = list(range(b.first, b.n + 1))
    right = left + len(secs) * sw
    width = right + 70

    def sx(s: int) -> float:
        return left + (s - b.first + 0.5) * sw
    for s in secs:
        spec["texts"].append({"x": sx(s), "y": top - 16, "lines": [str(s)], "cls": "small", "anchor": "middle"})
        spec["lines"].append({"x1": sx(s), "y1": top, "x2": sx(s), "y2": top + len(ts) * lane, "cls": "grid"})
    spec["texts"].append({"x": left, "y": top - 34, "lines": ["Sections"], "cls": "small", "anchor": "start"})
    for i, t in enumerate(ts):
        y = top + (i + 0.5) * lane
        cls = f"line-{i % 6}"
        name = _s(t.get("name"), 80) or f"Thread {i + 1}"
        tid = _s(t.get("id"), 60) or f"t{i + 1}"
        tsecs = [s for s in _ints(t.get("sections")) if b.first <= s <= b.n]
        refs = {k: [r for r in _refs(t.get(k), (tsecs[0] if k == "setups" else tsecs[-1]) if tsecs else None)
                    if b.first <= r[0] <= b.n] for k in ("setups", "payoffs")}
        span = sorted(set(tsecs) | {s for k in refs for s, _w in refs[k]})
        kind = _s(t.get("kind"), 40)
        spec["texts"].append({"x": 12, "y": y - 2, "lines": _wrap(name, 18, 2), "cls": "label", "anchor": "start"})
        if kind:
            spec["texts"].append({"x": 12, "y": y + 16 + (16 if len(_wrap(name, 18, 2)) > 1 else 0),
                                  "lines": [kind], "cls": "small", "anchor": "start"})
        if span:
            spec["lines"].append({"x1": sx(span[0]), "y1": y, "x2": sx(span[-1]), "y2": y, "cls": f"lane {cls}"})
        for s in tsecs:
            spec["pins"].append({"id": f"{tid}@{s}", "label": name, "x": sx(s), "y": y, "r": 3.5, "shape": "dot",
                                 "cls": f"tick {cls}", "title": f"{name} in section {s}",
                                 "point": _point(s, "", f"{name}, section {s}")})
        for k, shape, label in (("setups", "diamond", "setup"), ("payoffs", "dot", "payoff")):
            for n, (s, what) in enumerate(refs[k]):
                spec["pins"].append({"id": f"{tid}-{label}-{n + 1}", "label": what or name,
                                     "x": sx(s) + (-7 if label == "setup" else 7), "y": y - 12, "r": 6,
                                     "shape": shape, "cls": cls, "title": f"{name}, {label} in section {s}: {what}",
                                     "point": _point(s, what, f"{name}: {label}")})
        if t.get("open"):
            last = span[-1] if span else b.first
            spec["lines"].append({"x1": sx(last), "y1": y, "x2": right + 14, "y2": y, "cls": "open"})
            spec["pins"].append({"id": f"{tid}-open", "label": name, "x": right + 24, "y": y, "r": 7, "shape": "flag",
                                 "cls": "open", "title": f"{name} is still open at the end",
                                 "point": _point(last, "", f"{name} is still open after this section.")})
    spec["width"], spec["height"] = width, top + len(ts) * lane + 20
    spec["legend"] = [{"text": "diamond: a setup; dot above the lane: a payoff", "cls": "muted"},
                      {"text": "flag at the right edge: still open at the end", "cls": "open"}]
    return _round(spec)


BUILDERS = {"family": family, "relationships": relationships, "timeline": timeline, "touchpoints": touchpoints,
            "arcs": arcs, "structure": structure, "world": world, "threads": threads}


def build(bible: dict, kind: str, asked: str = "") -> dict:
    return BUILDERS[kind](Bible(bible), asked)


def portrait_family(bible: dict) -> dict:
    """The family tree with room for a portrait in each circle, for the atlas."""
    return family(Bible(bible), "", r=30)


def ask_title(bible: dict, kind: str, asked: str) -> str:
    b = Bible(bible)
    label = KINDS[kind]
    text = str(asked or "").lower()
    if kind == "relationships" and re.search(r"\bsupporting\b", text):
        return "Supporting characters: who meets whom"
    if kind == "relationships" and re.search(r"\binteract\w*", text):
        return "Who meets whom"
    named = b.named_in(asked) if kind in ("arcs", "touchpoints") else []
    if named:
        return f"{label}: " + ", ".join(c["name"] for c in named[:3]) + ("..." if len(named) > 3 else "")
    return label


def with_positions(spec: dict, positions: Any) -> dict:
    """The spec with dragged node positions put back, for the nodes that are still there."""
    if not isinstance(positions, dict):
        return spec
    for nd in spec.get("nodes", []):
        got = positions.get(nd.get("id"))
        if isinstance(got, (list, tuple)) and len(got) == 2 and all(isinstance(v, (int, float)) for v in got):
            x, y = (min(max(float(v), 0.0), 20000.0) for v in got)
            nd["x"], nd["y"] = round(x, 1), round(y, 1)
            spec["width"] = max(spec.get("width", 0), int(x + nd.get("r", 10) + 60))
            spec["height"] = max(spec.get("height", 0), int(y + nd.get("r", 10) + 40))
    return spec


def find_item(spec: dict, item: Any) -> dict | None:
    """A clickable item on a board, by its id or its label (case does not matter)."""
    want = _s(item, 300).lower()
    if not want:
        return None
    every = [x for key in ("nodes", "pins", "rects") for x in spec.get(key, []) if x.get("point")]
    for test in (lambda x: str(x.get("id", "")).lower() == want, lambda x: str(x.get("label", "")).lower() == want,
                 lambda x: want in str(x.get("label", "")).lower()):
        hit = next((x for x in every if test(x)), None)
        if hit:
            return hit
    return None
