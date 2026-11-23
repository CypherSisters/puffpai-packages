"""How a check runs: the check files, the units of work, the focused asks, the proof of every quote, the second pass, the
ranking, the marks that survive a re-run, the old desk carried over, and the letter (EDITORS_DESK.md).

A check is a text file in ``checks/``: a head of ``key: value`` lines, and one ``## block`` for each kind of ask. A block
named for a unit (``thread``, ``section``, ...) is asked once per unit. ``judge`` weighs what code found. ``write <name>``
makes a document. Adding a check means adding a file.

Every answer is saved under the sha of its ask, so a check that stopped goes on from where it was, and asks whose words
have not changed cost nothing. Nothing here writes section text.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import docx_out
import finders
from book import DESK_OLD, PRIVATE, STATUS, Book, all_books, locate, read_index, scrub
from brainline import (REPAIR, BrainQueue, BrainSlow, now, parse_json, plain, read_json, strip_think, tokens, write_atomic,
                       write_json)

HERE = Path(__file__).resolve().parent
CHECKS = HERE / "checks"
FINDINGS, LETTER, RUNS, ANSWERS, DOCS, VERSIONS = "findings.json", "letter.json", "runs.json", "answers", "documents.json", "versions"
EXPORTS = "Editor's Desk"          # beside Manuscripts in My files: what she asked for, where she can see it
STAGES = [
    {"id": "story", "name": "The story", "plain": "Developmental edit",
     "why": "First the big shape. There is no use polishing a scene that may go."},
    {"id": "facts", "name": "The facts", "plain": "Continuity",
     "why": "Once the story holds, settle what is true: who, where, when."},
    {"id": "prose", "name": "The prose", "plain": "Line edit",
     "why": "With the story and facts settled, make it sound right, sentence by sentence."},
    {"id": "polish", "name": "The polish", "plain": "Copyedit and proof",
     "why": "Copyediting text that is still moving is wasted work. Proofreading comes last of all."},
    {"id": "out", "name": "Getting it out", "plain": "Submission",
     "why": "For the finished book: how it reads to a stranger, and what goes out the door."},
]
STAGE_IDS = [s["id"] for s in STAGES]
SIZE_RANK = {"big": 0, "medium": 1, "small": 2}
STATES = ("open", "fixed", "not_a_problem", "later")
ROOM_SHARE = 5 / 8                 # of the context, for the ask; the rest is for the answer (5,000 of 8,192, as Manuscript)
QUOTE_MIN = 8
MOST = 12
POLITE_S = float(__import__("os").environ.get("EDITORS_DESK_POLITE_S") or 20)   # how often to look while waiting
STALE_READING_S = 45 * 60          # a "reading" status untouched this long: Manuscript stopped without saying
UNITS_MAX = 40                     # threads, characters, themes or claims in one check

FORMAT = ('Answer with one JSON object only: {"findings":[{"what":"one plain sentence naming the problem",'
          '"why":"what a reader would feel or miss","fix":"one way to fix it, naming the people, places or events",'
          '"size":"big, medium or small","quotes":[{"section":1,"quote":"words copied exactly from the passages"}]}]}. '
          "Every finding needs at least one quote copied exactly, word for word. No quote you can copy, no finding. "
          "Few and true: an empty list is a good answer. Never write new story text or rewrite her sentences.")
FORMAT_CORRECT = ('Answer with one JSON object only: {"findings":[{"what":"what is wrong, in plain words","why":"why it is '
                  'wrong","fix":"the rule or what to do","size":"small","quotes":[{"section":1,"quote":"the exact words"}],'
                  '"correct":"the same words, corrected, changing as little as you can"}]}. Quote only as many words as '
                  "the correction needs. Only real errors, not matters of taste. An empty list is a good answer.")
VERIFY = ("You check an editor's note against the book. Given the note and the passages it cites, say whether the passages "
          'really show the problem. Answer with one JSON object only: {"holds":true,"evidence":"the exact words from the '
          'passages that show the problem","reason":"one short line"}. If the passages don\'t show it, or it is a matter of '
          "taste, or a frame story, flashback, memory, letter or document explains it, holds is false. Evidence is copied "
          "word for word; if no words show it, leave evidence empty.")
VERIFY_MANY = ("You check an editor's notes against the passage. For each numbered note, keep it only if the passage really "
               'shows it. Answer with one JSON object only: {"keep":[1,3]}. An empty list is fine.')
JUDGE_TAIL = ('Answer with one JSON object only: {"verdict":"problem, question or no","why":"what a reader would feel or '
              'miss, or why it is fine","fix":"one way to fix it in this book, naming names","size":"big, medium or small",'
              '"evidence":"the exact words from the passages that show the problem"}. problem: the passages plainly show it, '
              "nothing in the book marks it, and a reader would be confused; evidence is required, copied word for word. "
              "question: it may be fine and the writer should look. no: the passages explain it (a frame story, a flashback, a "
              "memory, a dream, a letter or other document, a chapter headed by its date, someone only being mentioned) or it is "
              "no problem at all. When in doubt, question.")
RANK = ("You are a publishing house's senior editor. Order these notes on one book by how much each matters to the book as "
        'a whole, most first. Answer with one JSON object only: {"order":[3,1,2],"big":[3]}. big lists those that would '
        "change how a reader takes the book.")


VERDICT = re.compile(r"\b(?:incorrect|inaccurate|factually|wrong|false|untrue|correct|accurate|is true)\b", re.I)


# a suggested line of new story, in quote marks, with what leads into it: such as 'Mara closed her eyes...'
_LINE = re.compile(r"""[,;:]?\s*(?:(?:such as|perhaps|for example|for instance|like|e\.g\.)[^'"\u2018\u201c.]{0,30}?)?['"\u2018\u201c]([^'"\u2019\u201d]{15,})['"\u2019\u201d]""")


def no_new_prose(fix: str, book: Book) -> str:
    """A fix describes; it never writes the book. Words in quote marks stay only if they are really in the book."""
    def keep(m: re.Match) -> str:
        return m.group() if any(locate(s.text, m.group(1)) for s in book.sections) else ""
    return " ".join(_LINE.sub(keep, fix).split()).rstrip(" ,;:") if fix else fix


UNIT_PLAIN = {"code": "things code noticed, each weighed against the text", "thread": "plot threads", "character": "people", "section": "sections", "theme": "themes",
              "claim": "claims and rules", "change": "things your change touches", "book": "the whole book's shape",
              "speakers": "the way people talk", "opening": "the opening", "ending": "the ending"}


FIND_PLAIN = {"open_threads": "threads left open", "dead_speaking": "the dead coming back", "two_places": "anyone in two places",
              "dates_backward": "dates out of order", "bible_continuity": "slips the reading noted", "looks_changed": "looks that change",
              "ages_changed": "ages", "names_spelled": "names spelled two ways", "wrong_weekday": "weekdays", "travel_times": "travel times",
              "world_numbers": "the world's rules", "chapter_lengths": "chapter lengths", "sagging": "stretches where nothing happens",
              "shape": "where the turns fall", "empty_scenes": "scenes with no purpose", "lead_absent": "the lead going missing",
              "could_merge": "characters who could merge", "tense_and_person": "slips of tense and person", "voice_drift": "drift in the voice",
              "dialogue_tags": "speech tags", "dialogue_only_info": "speeches that only inform", "prose_habits": "habits",
              "proof_marks": "marks and doubled words", "scene_breaks": "scene breaks", "style_sheet_findings": "spellings and styles",
              "permissions": "brands and verse"}


class Stopped(Exception):
    """She pressed Stop."""


# -- check files -----------------------------------------------------------------------------------------------------

def parse_check(text: str, cid: str) -> dict | None:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None
    head: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            head[k.strip().lower()] = v.strip()
    if not head.get("name"):
        return None
    blocks: dict[str, str] = {}
    parts = re.split(r"^## +(.+?)\s*$", text[m.end():], flags=re.M)
    for name, body in zip(parts[1::2], parts[2::2]):
        blocks[name.strip().lower()] = " ".join(body.split())
    stage = head.get("stage", "any").lower()
    return {"id": cid, "name": head["name"], "stage": stage if stage in STAGE_IDS else "any",
            "order": int(head["order"]) if head.get("order", "").isdigit() else 50, "says": head.get("says", ""),
            "reads": head.get("reads", ""), "trust": head.get("trust", ""), "thin": head.get("thin", "").lower() == "yes",
            "find": [x.strip() for x in head.get("find", "").split(",") if x.strip()],
            "most": int(head["most"]) if head.get("most", "").isdigit() else MOST,
            "needs_bible": head.get("needs_bible", "yes").lower() != "no",
            "no_verdict": head.get("no_verdict", "").lower() == "yes", "verify": head.get("verify", "each").lower(), "rank": head.get("rank", "no").lower() == "yes",
            "letter": head.get("letter", "no").lower() == "yes", "use_this": head.get("use_this", "no").lower() == "yes",
            "box": head.get("box", ""), "box_needed": head.get("box_needed", "no").lower() == "yes",
            "facts": [x.strip() for x in head.get("facts", "").split(",") if x.strip()], "makes": head.get("makes", ""), "button": head.get("button", "Run"), "blocks": blocks}


def load_checks() -> dict[str, dict]:
    out = {}
    for p in sorted(CHECKS.glob("*.md")):
        got = parse_check(p.read_text(encoding="utf-8"), p.stem)
        if got:
            out[p.stem] = got
    stage_at = {s: i for i, s in enumerate(STAGE_IDS + ["any"])}
    return dict(sorted(out.items(), key=lambda kv: (stage_at[kv[1]["stage"]], kv[1]["order"])))


# -- passages ----------------------------------------------------------------------------------------------------------

def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def fit(lines: list[str], room: int) -> str:
    out, used = [], 0
    for ln in lines:
        used += tokens(ln)
        if used > room:
            break
        out.append(ln)
    return "\n".join(out)


def passages(book: Book, terms: list[str], sections: list[int] | None, room: int) -> str:
    """The paragraphs that bear on these words, from these sections (or the whole book), most telling first, then put in
    book order with their section numbers."""
    want = set()
    for t in terms:
        want |= finders.words_of(t)
    secs = [s for s in book.sections if not sections or s.n in sections]
    scored = []
    for s in secs:
        for i, p in enumerate(paragraphs(s.story)):
            hit = len(want & finders.words_of(p))
            if hit:
                scored.append((-hit, s.n, i, p))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    chosen, used = [], 0
    seen: dict[int, int] = {}
    for _hit, n, i, p in scored:                       # at most six paragraphs of one section until the rest had a turn
        if seen.get(n, 0) >= 6:
            continue
        cost = tokens(p) + 6
        if used + cost > room:
            continue
        chosen.append((n, i, p))
        used += cost
        seen[n] = seen.get(n, 0) + 1
    if not chosen:                                     # nothing matched: the openings of the sections named
        for s in secs:
            ps = paragraphs(s.story)
            if ps and used + tokens(ps[0]) + 6 <= room:
                chosen.append((s.n, 0, ps[0]))
                used += tokens(ps[0]) + 6
    chosen.sort(key=lambda x: (x[0], x[1]))
    return "\n".join(f"[section {n}] {p}" for n, _i, p in chosen)


def around(book: Book, where: list[dict], room: int) -> str:
    """The paragraph holding each quote, with the ones beside it when there is room."""
    out = []
    each = max(150, room // max(1, len(where)))
    for w in where:
        s = book.section(w.get("section"))
        if not s:
            continue
        ps = paragraphs(s.text)
        at = next((i for i, p in enumerate(ps) if locate(p, w.get("quote") or "")), None)
        if at is None:
            continue
        got = [ps[at]]
        used = tokens(ps[at])
        for j in (at - 1, at + 1, at - 2, at + 2):
            if 0 <= j < len(ps) and used + tokens(ps[j]) <= each:
                got.append(ps[j])
                used += tokens(ps[j])
        out.append(f"[section {s.n}] " + "\n".join(p for p in ps if p in got))
    return "\n\n".join(out)


def bible_lines(book: Book, what: tuple[str, ...]) -> list[str]:
    """The bible's facts for an ask. A field that is missing just gives no lines."""
    b = book.bible
    out = []
    summary = b.get("summary") if isinstance(b.get("summary"), dict) else {}
    if "summary" in what:
        if summary.get("paragraph"):
            out.append(f"The book: {summary['paragraph']}")
        out += [f"Section {s.get('section')}: {s.get('summary')}" for s in summary.get("by_section") or [] if isinstance(s, dict)]
    if "structure" in what and isinstance(b.get("structure"), dict):
        st = b["structure"]
        out += [f"Act: {a.get('name')}, sections {a.get('from')} to {a.get('to')}" for a in st.get("acts") or [] if isinstance(a, dict)]
        out += [f"Turning point, section {t.get('section')}: {t.get('name')}. {t.get('note')}" for t in st.get("turning_points") or [] if isinstance(t, dict)]
        for k in ("midpoint", "climax"):
            if isinstance(st.get(k), dict) and st[k].get("section"):
                out.append(f"{k.capitalize()}, section {st[k]['section']}: {st[k].get('note')}")
    if "threads" in what:
        out += [f"Thread: {t.get('name')} ({t.get('kind')}), sections {t.get('sections')}; set up: {'; '.join(map(str, t.get('setups') or []))}; "
                f"paid off: {'; '.join(map(str, t.get('payoffs') or [])) or 'not yet'}" for t in book.list_of("threads")]
    if "characters" in what:
        out += [f"{c.get('name')} ({c.get('role')}): wants {c.get('wants')}; fears {c.get('fears')}" for c in book.list_of("characters")
                if c.get("importance") in ("main", "supporting") or c.get("wants")]
    if "themes" in what:
        out += [f"Theme: {t.get('name')}" for t in book.list_of("themes")]
    if "voice" in what:
        v = b.get("voice") if isinstance(b.get("voice"), dict) else {}
        br = b.get("briefs") if isinstance(b.get("briefs"), dict) else {}
        if br.get("voice"):
            out.append(f"The book's own voice: {br['voice']}")
        out += [f"Words it leans on: {', '.join(str(t.get('phrase')) for t in v.get('tics') or [] if isinstance(t, dict))}"] if v.get("tics") else []
        out += [f"{d.get('character')} talks: {d.get('habit')}" for d in v.get("dialogue_habits") or [] if isinstance(d, dict)]
    return out


# -- the units --------------------------------------------------------------------------------------------------------

def _thread_units(book: Book) -> list[dict]:
    out = []
    for t in sorted(book.list_of("threads"), key=lambda t: "main" not in str(t.get("kind") or ""))[:UNITS_MAX]:
        secs = finders.ints(t.get("sections"))
        setups, pays = "; ".join(map(str, t.get("setups") or [])), "; ".join(map(str, t.get("payoffs") or []))
        out.append({"label": f"the thread \"{t.get('name')}\"", "facts": f"Thread: {t.get('name')} ({t.get('kind')}). In sections {secs}. "
                    f"Set up: {setups or 'not noted'}. Paid off: {pays or 'not noted'}.",
                    "terms": [str(t.get("name")), setups, pays], "sections": None, "weight": 20 if "main" in str(t.get("kind") or "") else 10})
    return out


def _character_units(book: Book) -> list[dict]:
    cs = book.list_of("characters")
    picked = [c for c in cs if c.get("importance") == "main"] or cs[:6]
    picked += [c for c in cs if c.get("importance") == "supporting" and len(finders.ints(c.get("appearances"))) >= 3][:4]
    out = []
    for c in picked[:UNITS_MAX]:
        arc = " ".join(f"[{b.get('section')}] {b.get('beat')}" for b in c.get("arc") or [] if isinstance(b, dict))
        rel = "; ".join(f"{r.get('kind')} of {r.get('to')}" for r in c.get("relationships") or [] if isinstance(r, dict))
        out.append({"label": str(c.get("name")), "facts": f"{c.get('name')}: {c.get('role')}. Wants: {c.get('wants') or 'not noted'}. "
                    f"Fears: {c.get('fears') or 'not noted'}. Traits: {', '.join(map(str, c.get('traits') or []))}. Relationships: {rel}. "
                    f"In sections {c.get('appearances')}. Beats: {arc or 'not noted'}.",
                    "terms": [n for n in [str(c.get("name"))] + [str(a) for a in c.get("aliases") or []]] + [str(c.get("wants") or "")],
                    "sections": finders.ints(c.get("appearances")) or None, "weight": 18 if c.get("importance") == "main" else 8})
    return out


def _section_units(book: Book, room: int) -> list[dict]:
    out = []
    for s in book.sections:
        if not s.story.strip():                    # all of it the e-book's wrapping
            continue
        chunk: list[str] = []
        used = 0
        part = 0

        def flush() -> None:
            nonlocal chunk, used, part
            if chunk:
                part += 1
                out.append({"label": f"section {s.n}" + (f", part {part}" if part > 1 or tokens(s.text) > room else ""), "text": "\n\n".join(chunk),
                            "section": s.n, "facts": f"Section {s.n}: {s.heading}", "weight": 0})
            chunk, used = [], 0
        for p in paragraphs(s.story):
            if used + tokens(p) > room:
                flush()
            chunk.append(p)
            used += tokens(p)
        flush()
    return out


def _theme_units(book: Book) -> list[dict]:
    out = []
    for t in book.list_of("themes")[:UNITS_MAX]:
        ev = [e for e in t.get("evidence") or [] if isinstance(e, dict)]
        out.append({"label": f"the theme \"{t.get('name')}\"", "facts": f"Theme: {t.get('name')}. Strongest quotes noted: " +
                    "; ".join(f"[{e.get('section')}] {e.get('quote')}" for e in ev), "terms": [str(t.get("name"))] + [str(e.get("quote")) for e in ev],
                    "sections": None, "weight": 8})
    return out


def _claim_units(book: Book) -> list[dict]:
    out = []
    for s in book.list_of("science"):
        out.append({"label": f"the claim \"{str(s.get('claim'))[:60]}\"", "facts": f"Claim in the book: {s.get('claim')}. Noted at the reading: {s.get('note')}. "
                    f"Seemed plausible: {s.get('plausible')}.", "terms": [str(s.get("claim"))], "sections": None, "weight": 14 if s.get("plausible") == "no" else 8})
    for r in book.list_of("world_rules"):
        out.append({"label": f"the rule \"{str(r.get('rule'))[:60]}\"", "facts": f"A rule of this world: {r.get('rule')}. Noted in sections {r.get('sections')}.",
                    "terms": [str(r.get("rule"))], "sections": None, "weight": 12})
    return out[:UNITS_MAX]


def _change_units(book: Book, plan: str) -> list[dict]:
    """What her change touches: everyone, every thread, place and event her words name, one ask each."""
    want = finders.words_of(plan)
    out = []
    for c in _character_units(book) + [u for u in _character_units_minor(book)]:
        if want & finders.words_of(c["label"]):
            out.append(c)
    for t in _thread_units(book):
        hit = set(finders.words_of(t["facts"])) & want
        if hit:
            out.append(t)
    for p in book.list_of("places"):
        if want & finders.words_of(p.get("name")):
            out.append({"label": str(p.get("name")), "facts": f"Place: {p.get('name')}. {p.get('notes') or ''}", "terms": [str(p.get("name"))], "sections": None, "weight": 8})
    named = [c for c in book.list_of("characters") if want & finders.words_of(c.get("name"))]
    if named:                                                     # the threads and events those people are in
        ids = {str(c.get("id")) for c in named}
        secs = sorted({e.get("section") for e in book.list_of("events") if ids & {str(w) for w in e.get("who") or []}
                       and isinstance(e.get("section"), int)})
        if secs:
            lines = [f"[{e.get('section')}] {e.get('what')}" + (f" ({e.get('when')})" if e.get("when") else "") for e in book.list_of("events") if e.get("section") in secs]
            out.append({"label": "the timeline", "facts": "Events these people are in:\n" + "\n".join(lines[:40]), "terms": [str(c.get("name")) for c in named], "sections": secs, "weight": 10})
        for t in _thread_units(book):
            if any(t not in out and n in t["facts"] for n in [str(c.get("name")).split()[0] for c in named]):
                out.append(t)
    if not out:                                                   # her words name nothing: the best passages for them
        out.append({"label": "your change", "facts": "\n".join(bible_lines(book, ("summary", "threads"))[:30]), "terms": [plan], "sections": None, "weight": 8})
    return out[:12]


def _character_units_minor(book: Book) -> list[dict]:
    return [{"label": str(c.get("name")), "facts": f"{c.get('name')}: {c.get('role')}.", "terms": [str(c.get("name"))],
             "sections": finders.ints(c.get("appearances")) or None, "weight": 4}
            for c in book.list_of("characters") if c.get("importance") not in ("main", "supporting")]


def units_for(name: str, book: Book, room: int, plan: str = "") -> list[dict]:
    if name == "thread":
        return _thread_units(book)
    if name == "character":
        return _character_units(book)
    if name == "section":
        return _section_units(book, room)
    if name == "theme":
        return _theme_units(book)
    if name == "claim":
        return _claim_units(book)
    if name == "change":
        return _change_units(book, plan)
    if name == "book":
        return [{"label": "the whole book", "facts": "\n".join(bible_lines(book, ("summary", "structure", "threads", "characters"))), "terms": [], "sections": None, "weight": 10}]
    if name == "speakers":
        got = finders.speakers(book)
        if len(got) < 2:
            return []
        return [{"label": "how the characters talk", "facts": "\n\n".join(f"{who} says:\n{lines}" for who, lines in got) + "\n" + "\n".join(bible_lines(book, ("voice",))), "terms": [], "sections": None, "weight": 8, "no_text": True}]
    if name in ("opening", "ending"):
        story = [s for s in book.sections if s.story.strip()]
        secs = story[:2] if name == "opening" else story[-2:]
        rows = [f"[section {s.n}] {p}" for s in secs for p in paragraphs(s.story)]
        if name == "ending":
            rows.reverse()                       # filled from the last words backward
        kept, used = [], 0
        for r in rows:
            used += tokens(r)
            if used > room * 3 // 4:
                break
            kept.append(r)
        if name == "ending":
            kept.reverse()
        facts = "\n".join(bible_lines(book, ("threads",)) + ([f"Book: {book.bible.get('summary', {}).get('one_line')}"] if isinstance(book.bible.get("summary"), dict) else []))
        return [{"label": f"the {name}", "text": "\n\n".join(kept), "facts": facts, "section": secs[0].n if secs else None, "weight": 14}] if secs else []
    return []


# -- findings on disk ----------------------------------------------------------------------------------------------------

def data_root() -> Path:
    return Path(os.environ.get("PUFFPAI_DATA") or HERE / "data")


def book_key(folder: Path) -> str:
    """A book's folder in the app's own data. The added date too, so a new book under an old title starts clean."""
    added = str(read_index(folder).get("added") or "")
    return f"{folder.name} {hashlib.sha1(added.encode('utf-8')).hexdigest()[:8]}"


def home_of(folder: Path) -> Path:
    """All this app's working files for one book: findings, marks, saved answers, versions. Out of her sight, and out of
    Manuscript's folder."""
    return data_root() / "books" / book_key(folder)


def exports_of(folder: Path) -> Path:
    return folder.parent.parent / EXPORTS / folder.name


def tidy(shelf: Path) -> None:
    """Saved answers are only for going on and for re-runs, so a book no longer on the shelf loses them. Its findings and
    versions stay: if she brings the book back, her marks are there."""
    root = data_root() / "books"
    live = {book_key(f) for f in all_books(shelf)}
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and d.name not in live:
                shutil.rmtree(d / ANSWERS, ignore_errors=True)


def load_findings(folder: Path) -> list[dict]:
    got = read_json(home_of(folder) / FINDINGS, {})
    items = got.get("findings") if isinstance(got, dict) else None
    return [f for f in items or [] if isinstance(f, dict)]


def save_findings(folder: Path, items: list[dict]) -> None:
    write_json(home_of(folder) / FINDINGS, {"schema": "editors-desk-findings/1", "findings": items})


def evidence_pieces(q: str) -> list[str]:
    """The pieces of a loosely quoted verdict: split at '...', "vs." and quote marks; failing that, runs of eight words."""
    pieces = [x.strip(" '\"‘’“”.,;:") for x in re.split(r"\.{3}|…|\bvs\.?|\n|['\"‘’“”](?=\s|$)|(?:^|\s)['\"‘“]", q)]
    out = [x for x in pieces if len(x) >= 20]
    words = q.strip(" '\"“”").split()
    out += [" ".join(words[i:i + 8]).strip(" '\"‘’“”") for i in range(0, max(0, len(words) - 7), 4)]
    return [x for x in out if len(x) >= 20]


def covered(evidence: str, where: list[dict]) -> int:
    """How many of a finding's places its evidence really shows."""
    pieces = [x for x in evidence.split(" ... ") if x]
    return sum(1 for w in where if any(locate(w.get("quote") or "", x) or locate(x, w.get("quote") or "") for x in pieces))


def json_text(f: dict) -> str:
    return " ".join(str(f.get(k) or "") for k in ("what", "why", "fix")) + " " + " ".join(str(w.get("quote") or "") for w in f.get("where") or [])


def make_id(key: str) -> str:
    return "f-" + hashlib.sha1(key.lower().encode("utf-8")).hexdigest()[:10]


def _same(a: dict, b: dict) -> bool:
    """The model words a thing differently each run: same check, a shared section and most of the same words."""
    if a.get("check") != b.get("check") or a.get("plan") != b.get("plan"):
        return False
    if not {w.get("section") for w in a.get("where") or []} & {w.get("section") for w in b.get("where") or []}:
        return False
    wa, wb = finders.words_of(a.get("what")), finders.words_of(b.get("what"))
    return len(wa & wb) >= 3 and len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.7


def merge_findings(folder: Path, check: str, new: list[dict], final: bool, plan: str = "") -> list[dict]:
    """A run's findings onto what is there, her marks kept. Not a problem never comes back. Fixed stays Fixed and says if it
    still looks broken. An open finding not found again goes, unless she wrote a note on it or the old desk brought it."""
    old = load_findings(folder)
    new = [f for f in new if f and not PRIVATE.search(json_text(f))]
    by_id = {f["id"]: f for f in old if f.get("id")}
    seen: set[str] = set()
    for f in new:
        was = by_id.get(f["id"]) or next((o for o in old if _same(o, f)), None)
        if was is None:
            f.setdefault("state", "open")
            f.setdefault("note", "")
            f["first_found"] = now()
            old.append(f)
            by_id[f["id"]] = f
            seen.add(f["id"])
            continue
        seen.add(was["id"])
        if was.get("check") != check:                  # another check found this first; it keeps it
            continue
        if was.get("state") == "not_a_problem":
            continue
        if was.get("state") == "fixed":
            was["still_broken"] = True
            continue
        keep = {k: was.get(k) for k in ("id", "state", "note", "first_found", "changed_at", "carried") if k in was}
        was.clear()
        was.update({**f, **keep})
        was.pop("gone", None)
    if final:
        out = []
        for f in old:
            mine = f.get("check") == check and (f.get("plan") or "") == (plan or "")
            if mine and f["id"] not in seen:
                if f.get("state") == "fixed":
                    f.pop("still_broken", None)
                elif f.get("state") == "open" and not (f.get("note") or "").strip() and not f.get("carried"):
                    continue
                elif f.get("state") in ("open", "later"):
                    f["gone"] = True
            out.append(f)
        old = out
    save_findings(folder, old)
    return old


TEMPLATE = re.compile(r"\b(?:blank|empty|unknown|missing|not noted|none given|n/a)\b|^clarify\b", re.I)


def worth_carrying(note: str, why: str, secs: list[int], hers: str) -> bool:
    """An old desk item comes over when it says something: a real problem line, a section, and a why; or when she wrote
    on it herself. "Wilcox's want blank" says nothing, and neither does an empty one."""
    if PRIVATE.search(note + " " + why):
        return False
    real = len(note.split()) >= 4 and not TEMPLATE.search(note)
    return bool(real and secs and (why or hers))


def carry_old_desk(folder: Path) -> int:
    """Manuscript's desk.json comes over once, so nothing she marked is lost: dismissed becomes Not a problem, anything else
    Later, labelled from the old desk, never a fresh finding. Only items that say something; her notes stay. desk.json
    itself is left as it was. A book carried by 0.1 is gone over again with these rules."""
    home = home_of(folder)
    before = read_json(home / "carried.json", None)
    have = load_findings(folder)
    if isinstance(before, dict) and before.get("v") == 2:
        return 0
    if isinstance(before, dict):                        # carried by 0.1: put those through the same rules now
        keep = []
        for f in have:
            if f.get("found_by") == "old desk":
                secs = [w.get("section") for w in f.get("where") or [] if isinstance(w.get("section"), int)]
                if not worth_carrying(str(f.get("what") or ""), str(f.get("why") or ""), secs, str(f.get("note") or "")):
                    continue
                if f.get("state") == "open":
                    f["state"] = "later"
            keep.append(f)
        save_findings(folder, keep)
        write_json(home / "carried.json", {"at": now(), "v": 2, "items": sum(1 for f in keep if f.get("found_by") == "old desk")})
        return 0
    got = read_json(folder / DESK_OLD, {})
    items = [d for d in (got.get("items") if isinstance(got, dict) else []) or [] if isinstance(d, dict) and d.get("id")]
    bible = read_json(folder / "bible.json", {}) or {}
    richer = {d.get("id"): d for d in (bible.get("desk") if isinstance(bible, dict) else []) or [] if isinstance(d, dict)}
    book = Book(folder)
    known = {f["id"] for f in have}
    done = 0
    size = {"high": "big", "medium": "medium", "low": "small"}
    for d in items:
        fid = str(d["id"])
        if fid in known:
            continue
        r = {**richer.get(d["id"], {}), **d}
        secs = [n for n in finders.ints(r.get("sections")) if book.section(n)]
        note, why, hers = plain(r.get("note"), 600), plain(r.get("why"), 600), plain(r.get("writer_note"), 5000)
        if not worth_carrying(note, why, secs, hers):
            continue
        where = []
        for w in r.get("where") or []:
            s = book.section(w.get("section")) if isinstance(w, dict) else None
            if s and locate(s.text, str(w.get("quote") or "")):
                where.append({"section": s.n, "quote": str(w.get("quote"))})
        where += [{"section": n, "quote": ""} for n in secs if n not in {w["section"] for w in where}]
        have.append({"id": fid, "check": "science" if r.get("kind") == "science" else "plot", "what": note, "why": why,
                     "fix": plain(r.get("fix"), 600), "size": size.get(r.get("severity"), "medium"), "where": where,
                     "state": "not_a_problem" if r.get("state") == "dismissed" else "later", "note": hers,
                     "carried": r.get("state") or "open", "found_by": "old desk", "changed_at": r.get("changed_at"), "sha": {},
                     "first_found": r.get("changed_at") or now()})
        done += 1
    if done:
        save_findings(folder, have)
    write_json(home / "carried.json", {"at": now(), "v": 2, "items": done})
    return done


# -- one run of one check --------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, folder: Path, check: dict, job: dict, brain: Any, queue: BrainQueue, room: Callable[[], int],
                 timing: Any, put: Callable[..., None], stopped: Callable[[], bool], shelf: Callable[[], Path]) -> None:
        self.folder, self.check, self.job, self.brain, self.queue = folder, check, job, brain, queue
        self.room, self.timing, self.put, self.stopped, self.shelf = room, timing, put, stopped, shelf
        self.typed = str(job.get("plan") or "")                 # what she typed in a box
        self.plan = self.typed if check["box_needed"] else ""   # a change she is tracing: its findings stay with it
        self.calls = 0
        self.cached = 0
        self.skips: list[str] = []          # the steps that ran out of time, in plain words
        self.timeouts: list[int] = []       # how long each ask that ran out of time was, in characters
        self.checked: dict[str, int] = {}   # what it went through, for "Here is what I checked"
        self.answers: dict | None = None
        self.used: set[str] = set()
        self.problems: list[str] = []
        self.max_prompt = 0

    # one ask, saved under the sha of its words

    def _polite(self) -> None:
        """Wait while Manuscript reads a book: it was asked first, and one brain does one thing."""
        while True:
            if self.stopped():
                raise Stopped()
            who = self._reading()
            if not who:
                return
            self.put(state="waiting", step=f"Waiting while Manuscript reads {who}")
            time.sleep(POLITE_S)

    def _reading(self) -> str:
        try:
            for folder in all_books(self.shelf()):
                st = read_json(folder / STATUS, {}) or {}
                if st.get("state") == "reading" and time.time() - (folder / STATUS).stat().st_mtime < STALE_READING_S:
                    return folder.name
        except OSError:
            pass
        return ""

    def _call(self, messages: list[dict]) -> str:
        self._polite()
        self.put(state="running")
        began = time.monotonic()
        text, model = self.queue.submit(lambda: self.brain.chat(messages), BrainQueue.CHECK).wait()
        spent = time.monotonic() - began
        self.calls += 1
        usage = getattr(self.brain, "last", {}).get("usage") or {}
        self.max_prompt = max(self.max_prompt, int(usage.get("prompt_tokens") or 0))
        self.timing.note(spent, model)
        self._last = {"model": model, "seconds": round(spent, 1), "prompt_tokens": usage.get("prompt_tokens"),
                      "answer_tokens": usage.get("completion_tokens")}
        return text

    # One file per check, holding each answer under the sha of its ask. Only what the last finished run used is kept.

    def _answers_path(self) -> Path:
        return home_of(self.folder) / ANSWERS / f"{self.check['id']}.json"

    def _saved(self, system: str, user: str) -> tuple[str, Any]:
        key = hashlib.sha256(f"{system}\n{user}".encode("utf-8")).hexdigest()
        if self.answers is None:
            got = read_json(self._answers_path(), {})
            self.answers = got if isinstance(got, dict) else {}
        rec = self.answers.get(key)
        if isinstance(rec, dict) and "answer" in rec:
            self.used.add(key)
            self.cached += 1
            return key, rec
        return key, None

    def _save(self, key: str, rec: dict) -> None:
        self.answers[key] = rec
        self.used.add(key)
        write_json(self._answers_path(), self.answers)

    def prune(self) -> None:
        if self.answers is not None:
            write_json(self._answers_path(), {k: v for k, v in self.answers.items() if k in self.used})

    def _fit_call(self, system: str, user: str) -> tuple[str, list[dict]]:
        """One call. If the brain runs out of time on it, once more with half the passages: a long ask is what times out,
        and waiting longer would not help. If that runs out too, the step is skipped and named on the check's row."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            return self._call(messages), messages
        except BrainSlow:
            self.timeouts.append(len(user))
            lines = user.split("\n")
            short = "\n".join(lines[:max(1, len(lines) // 2)])
            if len(short) >= len(user):
                raise
            messages = [{"role": "system", "content": system}, {"role": "user", "content": short}]
            return self._call(messages), messages

    def ask(self, system: str, user: str, ok: Callable[[Any], bool] = lambda g: isinstance(g, dict)) -> Any:
        """JSON from one ask with one repair call; None if there is no usable answer, and that is saved too."""
        system, user = scrub(system), scrub(user)
        key, rec = self._saved(system, user)
        if rec is not None:
            return rec["answer"]
        text, messages = self._fit_call(system, user)
        got = parse_json(text)
        if got is None or not ok(got):
            text2 = self._call(messages + [{"role": "assistant", "content": strip_think(text)[:6000]}, {"role": "user", "content": REPAIR}])
            got = parse_json(text2)
            got = got if got is not None and ok(got) else None
        self._save(key, {"answer": got, **self._last})
        return got

    def ask_text(self, system: str, user: str) -> str:
        system, user = scrub(system), scrub(user)
        key, rec = self._saved(system, user)
        if rec is not None and isinstance(rec.get("answer"), str):
            return rec["answer"]
        got = strip_think(self._fit_call(system, user)[0])
        self._save(key, {"answer": got, **self._last})
        return got

    # a finding's quotes, proved by code

    def proved(self, raw: Any, near: int | None, book: Book) -> list[dict]:
        """Real quotes only, exactly as in the text, in the section they really are in. Dots between two pieces: the longer."""
        out: list[dict] = []
        for w in raw if isinstance(raw, list) else []:
            if not isinstance(w, dict):
                continue
            q = str(w.get("quote") or "").strip()
            pieces = sorted(re.split(r"\s*(?:\.{3}|…)\s*", q), key=len, reverse=True)
            q = pieces[0].strip() if pieces else ""
            if len(q) < QUOTE_MIN:
                continue
            nums = finders.ints(w.get("section"))
            order = sorted(book.sections, key=lambda s: (s.n not in nums, s.n != near, s.n))
            for s in order:
                span = locate(s.text, q)
                if span:
                    exact = s.text[span[0]:span[1]]
                    if exact not in {x["quote"] for x in out}:
                        out.append({"section": s.n, "quote": exact})
                    break
        return out[:3]

    def finding(self, book: Book, key: str, what: Any, why: Any, fix: Any, size: Any, where: list[dict], by: str, **more: Any) -> dict | None:
        size = str(size or "").lower().strip()
        if PRIVATE.search(" ".join(str(x) for x in (what, why, fix, [w.get("quote") for w in where]))):
            return None                                # it is about a handle the vault left, not about her book
        return {"id": make_id(key), "check": self.check["id"], "what": plain(what, 600), "why": plain(why, 600), "fix": no_new_prose(plain(fix, 600), book),
                "size": size if size in SIZE_RANK else "medium", "where": where, "found_by": by, "found_at": now(),
                "sha": {str(w["section"]): book.section(w["section"]).sha for w in where if book.section(w["section"])},
                **({"plan": self.plan} if self.plan else {}), **more}

    # the steps

    def weigh(self, cand: dict, book: Book) -> dict | None:
        """A code candidate: proved by code; if it says judge, the brain weighs it with the passages it cites."""
        where = [w for w in cand.get("where") or [] if book.section(w.get("section")) and
                 (not w.get("quote") or locate(book.section(w["section"]).text, w["quote"]))]
        where = [{"section": w["section"], "quote": w.get("quote") or ""} for w in where if w.get("quote")]
        if not where:
            return None
        base = dict(what=cand["what"], why=cand.get("why"), fix=cand.get("fix"), size=cand.get("size"), where=where)
        more = {k: cand[k] for k in ("correct",) if cand.get(k) and self.check["use_this"]}
        blocks = self.check["blocks"]
        if cand.get("judge") and "judge" in blocks:
            room = self.room() - tokens(blocks["judge"]) - 300
            user = (f"Found by counting and matching in the text: {cand['what']}\n\nThe passages:\n{around(book, where, room // 2)}\n\n"
                    + passages(book, cand.get("terms") or [], None, room // 3))
            try:
                got = self.ask(f"{blocks['judge']} {JUDGE_TAIL}", user, lambda g: isinstance(g, dict) and ("verdict" in g or "holds" in g))
            except BrainSlow:
                got = None
                self.skip()
            if not isinstance(got, dict):
                return self.question(book, cand["key"], base, "Your bot could not weigh this, so it is a question for you.", more)
            verdict = str(got.get("verdict") or ("problem" if got.get("holds") else "no")).strip().lower()
            if verdict.startswith("no"):
                return None
            for k in ("why", "fix"):                   # what it is stays code's own words; the model explains
                if plain(got.get(k)):
                    base[k] = got[k]
            evidence = self.evident(got.get("evidence"))
            if verdict.startswith("problem") and evidence:
                size = str(got.get("size") or "medium").lower()
                base["size"] = size if size in SIZE_RANK else "medium"
                return self.finding(book, cand["key"], **base, by="code and brain", evidence=evidence, **more)
            return self.question(book, cand["key"], base, plain(got.get("why"), 400), more)
        return self.finding(book, cand["key"], **base, by="code", **more)

    def question(self, book: Book, key: str, base: dict, reason: str, more: dict | None = None) -> dict | None:
        """Not a finding: something the writer may want to look at. Kept out of the counts and the letter."""
        f = self.finding(book, key, **{**base, "what": "Check: " + str(base["what"])}, by="code and brain", **(more or {}))
        if f:
            f.update(kind="question", size="small", reason=plain(reason, 400))
        return f

    def from_answer(self, raw: dict, unit: dict, book: Book) -> dict | None:
        where = self.proved(raw.get("quotes"), unit.get("section"), book)
        what = plain(raw.get("what"), 600)
        if not where or not what:
            return None
        key = f"{self.check['id']}:{self.plan}:{where[0]['section']}:{' '.join(where[0]['quote'].lower().split())[:60]}"
        more: dict = {}
        fixed = plain(raw.get("correct"), 800)
        if self.check["use_this"] and fixed and fixed != where[0]["quote"]:
            q = where[0]["quote"]
            if len(fixed) <= max(len(q) * 1.5, len(q) + 40) and difflib.SequenceMatcher(None, q, fixed).ratio() >= 0.5:
                more["correct"] = fixed                # a real correction, not new writing
        return self.finding(book, key, what, raw.get("why"), raw.get("fix"), raw.get("size"), where, "brain", weight=unit.get("weight", 0), **more)

    def second_pass(self, f: dict, book: Book) -> tuple[bool, str]:
        room = self.room() - tokens(VERIFY) - 200
        user = f"The note: {f['what']}\nWhy: {f['why']}\nThe words it cites: " + " | ".join(w["quote"] for w in f["where"]) + \
            f"\n\nThe passages:\n{around(book, f['where'], room)}"
        try:
            got = self.ask(VERIFY, user, lambda g: isinstance(g, dict) and "holds" in g)
        except BrainSlow:
            self.skip()
            return False, ""
        if not (isinstance(got, dict) and got.get("holds")):
            return False, ""
        return True, self.evident(got.get("evidence"))

    def second_pass_many(self, items: list[dict], text: str) -> list[dict]:
        user = "\n".join(f"{i}. {f['what']} (the words: \"{f['where'][0]['quote']}\")" for i, f in enumerate(items, 1)) + f"\n\nThe passage:\n{text}"
        try:
            got = self.ask(VERIFY_MANY, user, lambda g: isinstance(g, dict) and isinstance(g.get("keep"), list))
        except BrainSlow:
            self.skip()
            return []
        keep = set(finders.ints(got.get("keep"))) if isinstance(got, dict) else set()
        return [f for i, f in enumerate(items, 1) if i in keep]

    def do_unit(self, name: str, unit: dict) -> list[dict]:
        book = Book(self.folder)
        c = self.check
        system = f"{c['blocks'][name]} {FORMAT_CORRECT if c['use_this'] else FORMAT}"
        room = self.room() - tokens(system) - 200
        head = (f"Her plan: {self.plan}\n\n" if self.plan else "") + (unit.get("facts") or "")
        if "voice" in c["facts"]:
            head += "\n" + "\n".join(bible_lines(book, ("voice",)))
        if "before" in c["facts"] and unit.get("section") is not None:
            summary = book.bible.get("summary") if isinstance(book.bible.get("summary"), dict) else {}
            head += "\nWhat the reader knows so far:\n" + "\n".join(f"Section {s.get('section')}: {s.get('summary')}" for s in summary.get("by_section") or []
                                                                  if isinstance(s, dict) and isinstance(s.get("section"), int) and s["section"] < unit["section"])
        if unit.get("text"):
            user = f"{fit(head.splitlines(), min(room // 4, 600))}\n\nThe passage:\n{unit['text']}"
        elif unit.get("no_text"):
            user = fit(head.splitlines(), room)
        else:
            head = fit(head.splitlines(), room // 3)
            user = f"{head}\n\nThe passages:\n{passages(book, unit.get('terms') or [self.plan], unit.get('sections'), room - tokens(head))}"
        try:
            got = self.ask(system, user, lambda g: isinstance(g, dict) and isinstance(g.get("findings"), list))
        except BrainSlow:
            self.problems.append(f"{unit['label'].capitalize()} took too long, so it was skipped.")
            self.skip()
            return []
        made = []
        dropped = 0
        for raw in (got.get("findings") if isinstance(got, dict) else []) or []:
            if isinstance(raw, dict):
                f = self.from_answer(raw, unit, book)
                if f and c["no_verdict"] and VERDICT.search(f"{f['what']} {f['why']}"):
                    continue                           # it may only flag what to look up, never say it checked
                if f:
                    made.append(f)
                elif raw.get("what"):
                    dropped += 1                       # a quote that is not in the text: the finding goes
        made = made[:6]
        if dropped:
            self.job["dropped"] = int(self.job.get("dropped") or 0) + dropped
        if not made:
            return []
        if c["verify"] == "together":
            self.bump(1)
            return self.second_pass_many(made, unit.get("text") or around(book, [w for f in made for w in f["where"]], room))
        if c["verify"] == "none":                      # flags, never checked: questions for her
            for f in made:
                f.update(kind="question", size="small")
            return made
        kept = []
        self.bump(len(made))
        for f in made:
            self.put(step=f"Checking what it found in {unit['label']} against the text")
            holds, evidence = self.second_pass(f, book)
            if evidence and covered(evidence, f["where"]) < min(2, len(f["where"])):
                # A problem between places has to be shown at both. Pointing at one of them shows the words are there,
                # not that they disagree: the first real run's "late" narrator of a frame story came through just so.
                evidence = ""
            if holds and evidence and c["rank"] and f["size"] != "big":
                # A whole-book check counts only what is big and shown. The model's own smaller notes (where a small
                # model's guesses about a frame story land) wait under Questions for her.
                f.update(kind="question", size="small", what="Check: " + f["what"], reason="Worth a look, your editor thinks, not a big problem.")
                kept.append(f)
            elif holds and evidence:
                f["evidence"] = evidence
                kept.append(f)
            elif holds:                                # true, perhaps, but nothing it can point at: a question, not a finding
                f.update(kind="question", size="small", what="Check: " + f["what"], reason="Your editor could not point at words that show it.")
                kept.append(f)
            else:
                self.job["dropped"] = int(self.job.get("dropped") or 0) + 1
            self.step_done()
        return kept

    def skip(self) -> None:
        step = plain(self.job.get("step"), 120) or "One step"
        if step not in self.skips:
            self.skips.append(step)

    def checked_line(self) -> str:
        one = {"thread": "plot thread", "character": "person", "section": "section", "theme": "theme", "claim": "claim or rule",
               "change": "thing your change touches", "code": "thing code noticed, weighed against the text"}
        parts = []
        for k, n in self.checked.items():
            if k in ("book", "opening", "ending", "speakers"):
                parts.append(UNIT_PLAIN[k])
            elif n:
                parts.append(f"{n} {one.get(k, k) if n == 1 else UNIT_PLAIN.get(k, k)}")
        if self.check["find"] and "code" not in self.checked:
            parts.append("the whole text by code for " + ", ".join(FIND_PLAIN.get(x, x.replace("_", " ")) for x in self.check["find"]) + ", with nothing to show")
        return ", ".join(parts)

    def evident(self, ev: Any) -> str:
        """The verdict's evidence, if it holds words of hers: found in her section text, never the bible or a cache. A small
        model quotes loosely ('...', two pieces with "vs." between, quote marks around), so each piece counts, and failing
        that any run of eight words of it that is really in her text. A restatement of the problem holds none, and is no
        evidence."""
        q = plain(ev, 1200)
        if PRIVATE.search(q):
            return ""
        texts = [s.text for s in Book(self.folder).sections]
        found = [x for x in evidence_pieces(q) if any(locate(t, x) for t in texts)]
        return " ... ".join(found)

    def bump(self, n: int) -> None:
        self.put(total=int(self.job.get("total") or 0) + n)

    def step_done(self) -> None:
        self.put(done=int(self.job.get("done") or 0) + 1, left_s=self.timing.left(int(self.job.get("total") or 0) - int(self.job.get("done") or 0) - 1))

    def rank(self, items: list[dict]) -> list[dict]:
        def score(f: dict) -> tuple:
            return (SIZE_RANK.get(f["size"], 1), -int(f.get("weight") or 0), f["where"][0]["section"] if f["where"] else 0)
        items = sorted(items, key=score)
        if self.check["rank"] and len(items) >= 3:
            self.put(step="Putting what it found in order of how much it matters")
            room = self.room() - tokens(RANK) - 100
            top = items[:30]
            user = fit([f"{i}. {f['what']}" for i, f in enumerate(top, 1)], room)
            try:
                got = self.ask(RANK, user, lambda g: isinstance(g, dict) and isinstance(g.get("order"), list))
            except BrainSlow:
                got = None
            if isinstance(got, dict):
                order: list[int] = []
                for x in got.get("order") or []:
                    n = finders.ints(x)
                    if n and 1 <= n[0] <= len(top) and n[0] not in order:
                        order.append(n[0])
                big = {n for n in finders.ints(got.get("big")) if 1 <= n <= len(top)}
                for n in big:
                    top[n - 1]["size"] = "big"
                rest = [i for i in range(1, len(top) + 1) if i not in order]
                items = [top[i - 1] for i in order + rest] + items[30:]
        return items

    def run(self) -> dict:
        c = self.check
        book = Book(self.folder)
        if c["needs_bible"] and not book.bible:
            raise NeedsReading()
        unread = book.unread()
        if unread:
            self.problems.append("Your editor is going by the new words, but the story bible still has the old version of "
                                 f"section{'s' if len(unread) > 1 else ''} {', '.join(map(str, unread))}. Read the book again in Manuscript to bring it up to date.")
        room = self.room()
        cands = [x for name in c["find"] for x in finders.run(name, book)]
        made: list[dict] = []
        seen_keys: set[str] = set()
        self.put(step="Going through the book by code" if cands else "Getting ready", done=0, total=0)

        names = [b for b in c["blocks"] if b in ("thread", "character", "section", "theme", "claim", "change", "book", "speakers", "opening", "ending")]
        plan_units = [(n, u) for n in names for u in units_for(n, book, room - 600, self.plan)]
        writes = [b for b in c["blocks"] if b.startswith("write ")]
        judged = [x for x in cands if x.get("judge") and "judge" in c["blocks"]]
        self.put(total=len(judged) + len(plan_units) + len(writes) + (1 if c["letter"] else 0))
        if cands:
            self.checked["code"] = len({c["key"] for c in cands})
        for cand in cands:
            if cand["key"] in seen_keys:
                continue
            seen_keys.add(cand["key"])
            if cand.get("judge") and "judge" in c["blocks"]:
                self.put(step=f"Weighing: {plain(cand['what'], 90)}")
            f = self.weigh(cand, Book(self.folder))
            if cand.get("judge") and "judge" in c["blocks"]:
                self.step_done()
            if f:
                made.append(f)
        merge_findings(self.folder, c["id"], list(made), False, self.plan)
        self.put(found=len(made))
        for name, unit in plan_units:
            self.checked[name] = self.checked.get(name, 0) + 1
            self.put(step=f"Reading {unit['label']}")
            got = self.do_unit(name, unit)
            made += got
            self.step_done()
            if got:
                merge_findings(self.folder, c["id"], got, False, self.plan)
            self.put(found=len(made))

        for b in writes:
            self.put(step=f"Drafting the {b[6:]}")
            self.write_document(b, book)
            self.step_done()
        if c["makes"]:
            self.put(step="Making the document")
            made += self.make(c["makes"], book)
        if c["use_this"]:                              # code and the model both saw it: once is enough, code's first
            once: list[dict] = []
            for f in made:
                w = f["where"][0]
                if not any(g["where"][0]["section"] == w["section"] and (w["quote"] in g["where"][0]["quote"] or g["where"][0]["quote"] in w["quote"]) for g in once):
                    once.append(f)
            made = once
        asks = [f for f in made if f.get("kind") == "question"][:c["most"]]
        ranked = self.rank([f for f in made if f.get("kind") != "question"])
        kept = ranked[:c["most"]]
        cut = len(ranked) - len(kept)
        kept_all = kept + asks
        for i, f in enumerate(kept_all):
            f["rank"] = i
        merge_findings(self.folder, c["id"], kept_all, not self.skips, self.plan)     # a short run takes nothing away
        if c["letter"]:
            self.put(step="Writing your editor's letter")
            write_letter(self, Book(self.folder))
            self.step_done()
        self.prune()
        return {"found": len(kept), "skipped": len(self.skips), "skipped_steps": self.skips[:6], "timeouts": self.timeouts[:6], "checked": self.checked_line(), "left_out": cut, "dropped": int(self.job.get("dropped") or 0), "calls": self.calls, "cached": self.cached,
                "problems": self.problems, "max_prompt": self.max_prompt}

    # documents: never into the manuscript

    def make(self, what: str, book: Book) -> list[dict]:
        if what == "style_sheet":
            save_document(self.folder, "style-sheet", "Style sheet", "md", finders.style_sheet(book), self.check["id"])
            return []
        if what == "length":
            text, finds = finders.length_note(book, self.typed)
            save_document(self.folder, "length", "Length and genre fit", "md", text, self.check["id"])
            return [f for f in (self.weigh(x, book) for x in finds) if f]
        if what == "format":
            raw, name = docx_out.build(book, self.typed)
            save_document(self.folder, "submission", name, "docx", raw, self.check["id"])
            return []
        return []

    def write_document(self, block: str, book: Book) -> None:
        name = block[6:].strip()
        system = self.check["blocks"][block]
        room = self.room() - tokens(system) - 200
        lines = bible_lines(book, ("summary", "characters", "threads", "themes", "structure"))
        try:
            text = self.ask_text(system, fit(lines, room))
        except BrainSlow:
            text = ""
        text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text).strip()
        if text:
            save_document(self.folder, re.sub(r"\W+", "-", name.lower()), name.capitalize(), "md",
                          f"{text}\n\n(A draft by your editor, about the book, for you to rewrite. It is not part of your manuscript.)\n", self.check["id"])
        else:
            self.problems.append(f"The {name} came back empty. Press again to try once more.")


class NeedsReading(Exception):
    """This check goes by the story bible, and there is none yet."""


def load_documents(folder: Path) -> list[dict]:
    got = read_json(home_of(folder) / DOCS, {}) or {}
    rows = got.get("documents") if isinstance(got, dict) else None
    return [d for d in rows or [] if isinstance(d, dict)]


def save_document(folder: Path, kind: str, name: str, ext: str, body: Any, check: str) -> dict:
    """What she asked for, in a folder she can see: My files/Editor's Desk/<title>/. A new file each time, so nothing she has
    rewritten is ever replaced; the same words again make no second copy."""
    out = exports_of(folder)
    out.mkdir(parents=True, exist_ok=True)
    rows = load_documents(folder)
    last = next((d for d in rows if d.get("kind") == kind), None)
    if last and ext != "docx":
        try:
            if (out / str(last["file"])).read_text(encoding="utf-8") == body:
                return last
        except OSError:
            pass
    day = time.strftime("%Y-%m-%d")
    stem, n = f"{name} {day}", 2
    while (out / f"{stem}.{ext}").exists():
        stem, n = f"{name} {day} ({n})", n + 1
    file = f"{stem}.{ext}"
    if isinstance(body, bytes):
        (out / file).write_bytes(body)
    else:
        write_atomic(out / file, body)
    rec = {"id": f"{kind}-{int(time.time() * 1000)}", "kind": kind, "name": name, "file": file, "made_at": now(), "check": check, "ext": ext}
    write_json(home_of(folder) / DOCS, {"documents": [rec] + rows})
    return rec


# -- the letter --------------------------------------------------------------------------------------------------------

def ordered_open(items: list[dict], checks: dict[str, dict]) -> list[dict]:
    """Open findings, in the order a publisher would work: stage first, then how big, then the check's own order."""
    at = {s: i for i, s in enumerate(STAGE_IDS + ["any"])}
    def key(f: dict) -> tuple:
        c = checks.get(f.get("check")) or {}
        return (SIZE_RANK.get(f.get("size"), 1) > 0, at.get(c.get("stage"), 5), SIZE_RANK.get(f.get("size"), 1), int(f.get("rank") or 0))
    return sorted([f for f in items if f.get("state") == "open" and not f.get("plan") and f.get("kind") != "question"
                   and not PRIVATE.search(json_text(f))], key=key)


def revision_plan(items: list[dict], checks: dict[str, dict], most: int = 8) -> list[dict]:
    """What to do first, worked out from the findings as they are now, so it moves as she marks things Fixed."""
    out = []
    for f in ordered_open(items, checks)[:most]:
        c = checks.get(f.get("check")) or {}
        stage = next((s["name"] for s in STAGES if s["id"] == c.get("stage")), "")
        out.append({"id": f.get("id"), "what": f.get("what"), "stage": stage, "size": f.get("size"), "section": (f.get("where") or [{}])[0].get("section")})
    return out


def write_letter(run_: Any, book: Book) -> dict:
    folder = book.folder
    checks = load_checks()
    items = [f for f in load_findings(folder) if f.get("state") == "open" and not f.get("plan") and f.get("kind") != "question"
             and not PRIVATE.search(json_text(f))]
    top = sorted(items, key=lambda f: (SIZE_RANK.get(f.get("size"), 1), int(f.get("rank") or 0)))[:10]
    ran = read_json(home_of(folder) / RUNS, {}) or {}
    names = [checks[c]["name"] for c in ran if c in checks] + ([run_.check["name"]] if run_.check["id"] not in ran else [])
    system = checks["letter"]["blocks"]["letter"] if "letter" in checks else ""
    summary = book.bible.get("summary") if isinstance(book.bible.get("summary"), dict) else {}
    lines = [f"Title: {book.title}", f"In short: {summary.get('one_line') or ''}", f"The book: {summary.get('paragraph') or ''}",
             "Checks run so far: " + ", ".join(names), "What the checks found, biggest first (use these numbers):"]
    lines += [f"{i}. [{f.get('size')}] {f.get('what')} Why: {f.get('why')}" for i, f in enumerate(top, 1)]
    lines += ["What the reading noted:"] + [f"Theme: {t.get('name')}" for t in book.list_of("themes")][:8] + bible_lines(book, ("characters",))[:8]
    got = None
    try:
        got = run_.ask(system, fit(lines, run_.room() - tokens(system) - 200), lambda g: isinstance(g, dict) and ("book" in g or "problems" in g))
    except BrainSlow:
        pass
    got = got if isinstance(got, dict) else {}
    problems = []
    for p in got.get("problems") or []:
        n = finders.ints(p.get("n") if isinstance(p, dict) else p)
        if n and 1 <= n[0] <= len(top) and top[n[0] - 1]["id"] not in {x["id"] for x in problems}:
            problems.append({"id": top[n[0] - 1]["id"], "line": top[n[0] - 1].get("what")})   # its order; the finding's own words
    if not problems:                            # the model gave no usable list: the biggest, in its own words
        problems = [{"id": f["id"], "line": f.get("what")} for f in top[:4]]
    first = plain(got.get("first"), 500)
    sent = "\n".join(lines).lower()
    if any(d.lower() not in sent for d in finders.month_days(first)):
        first = ""                               # a date the facts never gave: a small model's slip, so not shown
    letter = {"book": plain(got.get("book"), 800) or plain(summary.get("paragraph") or summary.get("one_line"), 800),
              "working": [plain(x, 300) for x in got.get("working") or [] if plain(x)][:4],
              "problems": problems[:4], "first": first or (f"Start with this: {problems[0]['line']}" if problems else ""),
              "written_at": now(), "from_checks": names, "open_then": len(items)}
    write_json(home_of(folder) / LETTER, letter)
    return letter
