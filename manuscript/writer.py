"""The first, instant rules for what the writer said about the bible (CONTRACT.md).

``rule_plan(bible, what, correction)`` turns a correction of a plain shape into stored operations (fixing.py), with no
call to the brain: a relationship's kind ("Tom is Jennifer's cousin, not her brother"), an alias ("Jen is also called
Jenny"), a character's role or importance ("Ilsa is the antagonist", "Tom is a minor character"), a place's kind
("Storm Harbor is a village"). Words that are not one of these plain shapes go to the brain instead.

``apply_one(bible, what, correction)`` is the older way, kept for records made before 0.4, which have no operations: it
matches the names in her words each time, and every bible entry it touches gets ``"corrected": true``.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from typing import Any

GROUPS = [("brother", "sister", "sibling"), ("half-brother", "half-sister", "half-sibling"),
          ("mother", "father", "parent"), ("son", "daughter", "child"),
          ("stepmother", "stepfather", "stepparent"), ("stepson", "stepdaughter", "stepchild"),
          ("grandmother", "grandfather", "grandparent"), ("grandson", "granddaughter", "grandchild"),
          ("husband", "wife", "spouse"), ("aunt", "uncle"), ("niece", "nephew"), ("cousin",), ("partner",),
          ("fiance", "fiancee", "fiancé", "fiancée"), ("ex-husband", "ex-wife", "ex"), ("best friend", "friend"),
          ("enemy",), ("rival",), ("lover",), ("boss", "employer"), ("employee", "assistant"), ("mentor",),
          ("student", "pupil", "apprentice"), ("neighbour", "neighbor"), ("colleague", "coworker")]
GROUP_OF = {w: g for g in GROUPS for w in g}
INVERSE = {"sibling": "sibling", "half-sibling": "half-sibling", "parent": "child", "child": "parent",
           "stepparent": "stepchild", "stepchild": "stepparent", "grandparent": "grandchild",
           "grandchild": "grandparent", "spouse": "spouse", "aunt": "niece or nephew", "niece": "aunt or uncle",
           "cousin": "cousin", "partner": "partner", "fiance": "fiance", "ex": "ex", "friend": "friend",
           "enemy": "enemy", "rival": "rival", "lover": "lover", "boss": "employee", "employee": "boss",
           "mentor": "student", "student": "mentor", "neighbour": "neighbour", "colleague": "colleague"}
FAMILY = {"parent": "parents", "child": "children", "spouse": "partners", "partner": "partners"}
IMPORTANCE = ["main", "supporting", "minor"]
ROLES = ["love interest", "protagonist", "antagonist",
         "deuteragonist", "narrator", "villain", "heroine", "hero", "sidekick", "foil"]
PLACE_KINDS = ["town", "city", "village", "island", "house", "farm", "ship", "harbor", "harbour", "port", "country",
               "kingdom", "forest", "river", "lake", "mountain", "castle", "palace", "school", "inn", "tavern",
               "street", "planet", "moon", "station", "valley", "desert", "church", "hospital", "shop", "cave",
               "cottage", "mansion", "city state", "region", "world", "continent", "sea", "bay", "beach", "camp"]
ALIAS = re.compile(r"(?:also called|also known as|known as|nicknamed|goes by|called|named)\s+[\"'“]?"
                   r"([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*)")
NEG = re.compile(r"(?:\bnot|n't|\bnever)\s+(?:(?:a|an|the|her|his|their|my|really|actually)\s+)*$")
TIME_WORDS = re.compile(r"\b(when|before|after|year|years|day|days|month|week|age|old|timeline|night|morning)\b")
SCIENCE_WORDS = re.compile(r"\b(science|tide|tides|moon|orbit|gravity|physics|planet|speed|light|distance)\b")


def _k(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s'-]", " ", text.lower()).split())


def _words_in(text: str, vocab: list[str]) -> tuple[list[str], list[str]]:
    """(said, negated): the vocabulary words in the text, in order; a word after "not" or "never" is negated."""
    low = text.lower()
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in sorted(vocab, key=len, reverse=True)) +
                     r")(?![\w-])")
    said, neg = [], []
    for m in pat.finditer(low):
        (neg if NEG.search(low[max(0, m.start() - 40):m.start()]) else said).append(m.group(1))
    return said, neg


def _named(text: str, things: list[dict], extra: bool = True) -> list[dict]:
    """The characters or places the text names, in the order it names them: by name, alias, id, and (``extra``)
    a first name only one of them has."""
    low = " " + _k(re.sub(r"['’]s\b", "", text)) + " "
    firsts: dict[str, int] = {}
    for c in things:
        parts = str(c.get("name") or "").split()
        if parts:
            firsts[parts[0].lower()] = firsts.get(parts[0].lower(), 0) + 1
    found = []
    for c in things:
        names = [str(c.get("name") or ""), str(c.get("id") or "").replace("-", " ")]
        names += [a for a in c.get("aliases") or [] if isinstance(a, str)]
        parts = str(c.get("name") or "").split()
        if extra and parts and firsts.get(parts[0].lower()) == 1:
            names.append(parts[0])
        at = [low.find(" " + _k(n) + " ") for n in names if len(_k(n)) > 1]
        at = [i for i in at if i >= 0]
        if at:
            found.append((min(at), c))
    return [c for _i, c in sorted(found, key=lambda x: x[0])]


def _base(kind: str) -> str:
    """A relationship word's group head: "sister" -> "sibling", "wife" -> "spouse"."""
    g = GROUP_OF.get(kind.lower())
    if not g:
        return kind.lower()
    return g[-1] if g[-1] in INVERSE else g[0]


def _same(a: str, b: str) -> bool:
    return _base(a) == _base(b)


def _relation_pick(bible: dict, text: str, chars: list[dict], correction: str) -> tuple[dict, dict, str] | None:
    """Who is whose what, by her words: (a, b, kind), or None."""
    said, neg = _words_in(text, list(GROUP_OF))
    fix_said, _n = _words_in(correction, list(GROUP_OF)) if correction else ([], [])
    new = (fix_said or said or [None])[0]
    if not new:
        return None
    old = neg[0] if neg else None
    a = chars[0]
    others = [c for c in chars[1:] if c is not a]
    by_id = {c.get("id"): c for c in bible.get("characters") or []}
    if others:
        b = others[0]
    else:                                         # "Tom is her cousin, not her brother": the one she meant
        cands = set()
        for r in a.get("relationships") or []:
            if old is None or _same(r.get("kind", ""), old) or _same(r.get("kind", ""), INVERSE.get(_base(old), "")):
                cands.add(r.get("to"))
        for c in bible.get("characters") or []:
            for r in c.get("relationships") or []:
                if r.get("to") == a.get("id") and (old is None or _same(r.get("kind", ""), old)
                                                   or _same(r.get("kind", ""), INVERSE.get(_base(old), ""))):
                    cands.add(c.get("id"))
        cands.discard(a.get("id"))
        if len(cands) != 1 or next(iter(cands)) not in by_id:
            return None
        b = by_id[next(iter(cands))]
    return a, b, new


def set_relation(bible: dict, a: dict, b: dict, new: str) -> None:
    """a is b's <new> now: the kind on both sides, and the family links that follow from it."""
    inv = INVERSE.get(_base(new))
    touched = []
    for r in a.get("relationships") or []:
        if r.get("to") == b.get("id"):
            r["kind"], r["corrected"] = new, True
            touched.append(r)
    for r in b.get("relationships") or []:
        if r.get("to") == a.get("id"):
            if inv and not _same(r.get("kind", ""), inv):
                r["kind"] = inv
            r["corrected"] = True
            touched.append(r)
    if not touched:
        rel = {"to": b.get("id"), "kind": new, "note": "You corrected this.", "corrected": True}
        a.setdefault("relationships", []).append(rel)
    # family links follow the kind: a parent, a child or a partner; a sibling or a cousin is none of those
    for x, y in ((a, b), (b, a)):
        fam = x.setdefault("family", {"parents": [], "children": [], "partners": []})
        for key in ("parents", "children", "partners"):
            fam.setdefault(key, [])
            if y.get("id") in fam[key]:
                fam[key].remove(y.get("id"))
    rel_a = _base(new)                            # a is <new> of b
    if FAMILY.get(rel_a) == "parents":            # a is b's parent
        b["family"]["parents"].append(a.get("id"))
        a["family"]["children"].append(b.get("id"))
    elif FAMILY.get(rel_a) == "children":
        a["family"]["parents"].append(b.get("id"))
        b["family"]["children"].append(a.get("id"))
    elif FAMILY.get(rel_a) == "partners":
        a["family"]["partners"].append(b.get("id"))
        b["family"]["partners"].append(a.get("id"))
    a["corrected"] = b["corrected"] = True


def _relation(bible: dict, text: str, chars: list[dict], correction: str) -> tuple[bool, str, list]:
    got = _relation_pick(bible, text, chars, correction)
    if got is None:
        return False, "", []
    a, b, new = got
    set_relation(bible, a, b, new)
    return True, f"{a.get('name')} is {b.get('name')}'s {new} now", [a.get("id"), b.get("id")]


def guess_tab(bible: dict, text: str) -> str:
    """The tab a correction's words belong on: a person, a place, words of time, words of science, else Characters."""
    if _named(text, [c for c in bible.get("characters") or [] if isinstance(c, dict)]):
        return "characters"
    if _named(text, [p for p in bible.get("places") or [] if isinstance(p, dict)], extra=False):
        return "world"
    low = text.lower()
    return "timeline" if TIME_WORDS.search(low) else "science" if SCIENCE_WORDS.search(low) else "characters"


def _joined(what: str, correction: str) -> str:
    return (" ".join(f"{what}. {correction}".split()) if correction and correction.lower() not in what.lower()
            else " ".join(what.split()))


def _names_marked(text: str, things: list[dict]) -> str:
    """Her words, lowered, with each name of these things as @ ("Tom's" is "@'s"), and no end stops or commas."""
    low = _k(text.replace("\u2019", "'"))
    names = set()
    for c in things:
        parts = str(c.get("name") or "").split()
        names |= {_k(str(c.get("name") or "")), parts[0].lower() if parts else ""}
        names |= {_k(str(a)) for a in c.get("aliases") or [] if isinstance(a, str)}
    for n in sorted((n for n in names if len(n) > 1), key=len, reverse=True):
        low = re.sub(r"(?<![\w@])" + re.escape(n) + r"(?![\w-])", "@", low)
    return low


def _alt(vocab: Any) -> str:
    return "|".join(re.escape(w) for w in sorted(vocab, key=len, reverse=True))


_NOT_THAT = r"(?: (?:not|and not|but not|rather than|instead of) .*)?"
_OWNS = r"(?:@'s|her|his|their|my|the|a|an)"
_REL = re.compile(rf"@ (?:is|was) (?:{_OWNS} )?(?:{_alt(GROUP_OF)})(?: of @)?{_NOT_THAT}")
_WHO = re.compile(rf"@ (?:is|was) (?:(?:a|an|the) )?(?:{_alt(IMPORTANCE + ROLES)})(?: character)?{_NOT_THAT}")
_AKA = re.compile(r"@ (?:(?:is|was) )?(?:also )?(?:called|known as|nicknamed|goes by|named) [\w' -]+")
_KIND = re.compile(rf"@ (?:is|was) (?:(?:a|an|the) )?(?:{_alt(PLACE_KINDS)}){_NOT_THAT}")


def rule_plan(bible: dict, what: str, correction: str = "") -> dict | None:
    """Stored operations for a correction of a plain shape, with the same answer the older rules give, or None for
    words that say anything else: the older rules look for their words anywhere, and now the brain can read the rest."""
    from fixing import target_of                    # fixing.py builds on this file
    text = _joined(what, correction)
    chars = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    places = [p for p in bible.get("places") or [] if isinstance(p, dict)]
    who = _named(text, chars)
    shape = _names_marked(what, chars)
    if who and _REL.fullmatch(shape):
        got = _relation_pick(bible, text, who, correction)
        if got:
            a, b, new = got
            return {"ops": [{"target": target_of("character", a), "action": "set", "field": "relationships",
                             "to": target_of("character", b), "value": new}],
                    "line": f"{a.get('name')} is {b.get('name')}'s {new} now", "tab": "characters"}
    if len(who) == 1 and _WHO.fullmatch(shape):
        c, parts, ops = who[0], [], []
        said, _n = _words_in(text, ROLES)
        weight, _n = _words_in(text, IMPORTANCE)
        if weight:
            ops.append({"target": target_of("character", c), "action": "set", "field": "importance", "value": weight[0]})
            parts.append(f"a {weight[0]} character")
        if said:
            ops.append({"target": target_of("character", c), "action": "set", "field": "role", "value": said[0]})
            parts.append(f"the {said[0]}")
        if ops:
            return {"ops": ops, "line": f"{c.get('name')} is {' and '.join(parts)} now", "tab": "characters"}
    if who and _AKA.fullmatch(shape):
        m = ALIAS.search(text)
        if m and _k(m.group(1)) != _k(str(who[0].get("name") or "")):
            return {"ops": [{"target": target_of("character", who[0]), "action": "add", "field": "aliases",
                             "value": m.group(1).strip()}],
                    "line": f"{who[0].get('name')} is also called {m.group(1).strip()} now", "tab": "characters"}
    spots = _named(text, places, extra=False)
    place_shape = _names_marked(what, places)
    if spots and not who and _KIND.fullmatch(place_shape):
        said, _n = _words_in(place_shape, PLACE_KINDS)   # not the "port" in the name Port Ash
        if said:
            return {"ops": [{"target": target_of("place", spots[0]), "action": "set", "field": "kind",
                             "value": said[0]}],
                    "line": f"{spots[0].get('name')} is a {said[0]} now", "tab": "world"}
    return None


def apply_one(bible: dict, what: str, correction: str = "") -> dict:
    """One correction on the bible. ``{"applied", "tab", "line"}``; the bible is changed in place."""
    text = " ".join(f"{what}. {correction}".split()) if correction and correction.lower() not in what.lower() \
        else " ".join(what.split())
    chars = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    places = [p for p in bible.get("places") or [] if isinstance(p, dict)]
    named = _named(text, chars)
    spots = _named(text, places, extra=False)
    tab = "characters" if named else "world" if spots else "timeline" if TIME_WORDS.search(text.lower()) \
        else "science" if SCIENCE_WORDS.search(text.lower()) else "characters"
    if named:
        ok, line, _ids = _relation(bible, text, named, correction)
        if ok:
            return {"applied": True, "tab": "characters", "line": line}
        said, _neg = _words_in(text, ROLES)
        weight, _neg = _words_in(text, IMPORTANCE)
        if (said or weight) and len(named) == 1:
            c, parts = named[0], []
            if weight:
                c["importance"] = weight[0]
                if weight[0] == "minor" and not c.get("write_up"):
                    from analysis import minor_write_up       # one line from the facts, as a reading makes it
                    c["write_up"] = minor_write_up(c)
                parts.append(f"a {weight[0]} character")
            if said:
                c["role"] = said[0]
                parts.append(f"the {said[0]}")
            c["corrected"] = True
            return {"applied": True, "tab": "characters", "line": f"{c.get('name')} is {' and '.join(parts)} now"}
        m = ALIAS.search(text)
        if m:
            c, alias = named[0], m.group(1).strip()
            if _k(alias) != _k(str(c.get("name") or "")):
                if _k(alias) not in {_k(a) for a in c.get("aliases") or []}:
                    c.setdefault("aliases", []).append(alias)
                c["corrected"] = True
                return {"applied": True, "tab": "characters", "line": f"{c.get('name')} is also called {alias} now"}
    if spots:
        said, _neg = _words_in(text, PLACE_KINDS)
        if said:
            spots[0]["kind"], spots[0]["corrected"] = said[0], True
            return {"applied": True, "tab": "world", "line": f"{spots[0].get('name')} is a {said[0]} now"}
    return {"applied": False, "tab": tab, "line": ""}
