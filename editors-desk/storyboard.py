"""The storyboard: the book as panels, the way a film or picture-book artist lays it out. For each moment worth drawing,
what we see, where, who, the mood, the shot, and her own words under it (EDITOR_IN_CHIEF.md, "The storyboard").

One ask per section, filled to the room. Code checks every answer: a panel's quote must really be in that section
(``book.locate``) or the panel goes; names stay only when the section or the story bible has them; the shot is one of
three words. A panel is a note about a picture, never a sentence for her book.

The caller gives ``ask(messages) -> str`` (it goes through the brain queue) and a plain dict of saved answers keyed by
the sha of each ask, so a re-run pays only for what changed. Nothing here writes a file.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from book import PRIVATE, _folded, locate, scrub
from brainline import BrainSlow, now, parse_json, plain, sha_of, tokens
from engine import Stopped, fit, paragraphs
from study import sentences

LEAST_WORDS = 40                # a section shorter than this is not drawn
PANELS_MOST = 5
WHAT_MOST = 24                  # words in what and in frame
WHERE_MOST = 16
MOOD_MOST = 8
WHO_MOST = 6
QUOTE_LEAST = 3                 # words, so a quote says where it sits; 5 to 15 are asked for
QUOTE_KEEP = 25                 # words of a proved quote kept
COPIED = 8                      # this many of her words in a row in what or frame is her prose, not a note
PROMPT_MOST = 60                # words in a picture prompt
SHOTS = ("wide", "medium", "close")
TIMEOUTS = (BrainSlow, TimeoutError)

SYSTEM = ("You are a storyboard artist making notes for pictures of a novel. You describe what a picture would show. "
          "You never write or rewrite the story.")

RULES = """Pick the 2 to 5 moments in this passage most worth drawing, in the order they happen.
Rules:
- what: what we see in the picture, in the present tense, in at most 24 words.
- where: the place. who: the people in the picture, by the names the passage uses. mood: a few words.
- shot: wide, medium or close. frame: how the picture is framed, in the present tense, in at most 24 words.
- quote: 5 to 15 words copied exactly from the passage, where the moment happens.
- Describe the picture. Never write new sentences for the story and never rewrite hers.
Answer with JSON only, a list in this shape:
[{"what": "...", "where": "...", "who": ["..."], "mood": "...", "shot": "wide", "frame": "...", "quote": "..."}]"""


def _user(heading: str, names: list[str], passage: str) -> str:
    lines = [f"The passage is {heading or 'one section of her book'}."]
    if names:
        lines.append("People in the book: " + ", ".join(names) + ".")
    lines += ["", "The passage:", "<<<", passage, ">>>", "", RULES]
    return "\n".join(lines)


def _messages(heading: str, names: list[str], passage: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": scrub(_user(heading, names, passage))}]


def _ask_tokens(messages: list[dict]) -> int:
    return sum(tokens(m["content"]) for m in messages)


def _words(text: str) -> int:
    return len(text.split())


def _cut(text: str, most: int) -> str:
    w = text.split()
    return " ".join(w[:most])


def _bible_names(book) -> list[str]:
    """The story bible's characters' names and aliases, never a handle the vault left."""
    out: list[str] = []
    for c in book.list_of("characters"):
        for n in [c.get("name")] + list(c.get("aliases") or []):
            n = " ".join(str(n or "").split()) if isinstance(n, str) else ""
            if n and not PRIVATE.search(n) and n not in out:
                out.append(n)
    return out[:40]


def _story_words(s) -> int:
    return _words(s.story)


def _drawable(s) -> str:
    """Why a section is not drawn, or nothing when it is."""
    if _story_words(s) >= LEAST_WORDS:
        return ""
    if _words(s.text) >= LEAST_WORDS:
        return "This is the e-book's wrapping, not the story, so it was not drawn."
    return f"This section has fewer than {LEAST_WORDS} words, so it was not drawn."


def _wanted(book, sections: Any) -> list[int]:
    out: list[int] = []
    for n in sections or []:
        try:
            k = int(n)
        except (TypeError, ValueError):
            continue
        if k not in out:
            out.append(k)
    return out


def steps(book, sections: list[int]) -> int:
    """How many asks :func:`run` makes when nothing is saved yet."""
    return sum(1 for n in _wanted(book, sections) for s in [book.section(n)] if s is not None and not _drawable(s))


def _passage(heading: str, names: list[str], story: str, room: int) -> str:
    """The section's story text cut at paragraph edges to fit the room; a paragraph too long on its own is cut at a
    sentence. Never mid-sentence."""
    paras = [" ".join(p.split()) for p in paragraphs(story)]
    left = room - _ask_tokens(_messages(heading, names, "")) - 8
    if left <= 0:
        return ""
    text = fit(paras, left)                                 # one paragraph a line, as many as the room holds
    kept = text.split("\n") if text else []
    if not kept and paras:                                  # the first paragraph alone is too long: its sentences
        kept_s: list[str] = []
        for sent in sentences(paras[0]):
            if _ask_tokens(_messages(heading, names, " ".join(kept_s + [sent]))) > room:
                break
            kept_s.append(sent)
        return " ".join(kept_s)
    while kept and _ask_tokens(_messages(heading, names, "\n\n".join(kept))) > room:
        kept = kept[:-1]
    return "\n\n".join(kept)


def _shot(v: Any) -> str:
    w = re.findall(r"[a-z]+", str(v or "").lower() if not isinstance(v, (dict, list)) else "")
    return w[0] if w and w[0] in SHOTS else "medium"


def _copies(text: str, hay: str) -> bool:
    """Has this note copied a run of her words?"""
    w = text.split()
    for i in range(0, max(0, len(w) - COPIED) + 1):
        q = _folded(" ".join(w[i:i + COPIED]))[0].strip()
        if len(w[i:i + COPIED]) == COPIED and q and q in hay:
            return True
    return False


def _who(v: Any, story: str, known: list[str]) -> list[str]:
    items = v if isinstance(v, list) else [v] if isinstance(v, str) else []
    low_known = {k.lower() for k in known}
    out: list[str] = []
    for x in items:
        name = plain(x, 80).strip(" .,;:") if isinstance(x, str) else ""
        if not name or PRIVATE.search(name) or name in out:
            continue
        if name.lower() in low_known or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", story, re.I):
            out.append(name)
    return out[:WHO_MOST]


def _panels(got: Any, s, known: list[str]) -> list[dict] | None:
    """The answer's panels, each checked against the section, in the order their quotes sit. None when the answer could
    not be read at all."""
    if isinstance(got, dict):
        got = next((v for v in got.values() if isinstance(v, list)), None)
    if not isinstance(got, list):
        return None
    hay = _folded(s.story)[0]
    out: list[dict] = []
    for x in got:
        if not isinstance(x, dict):
            continue
        quote = plain(x.get("quote"), 600).strip()
        quote = re.split(r"\s*(?:\.{3}|\u2026)\s*", quote)[0].strip()       # the first piece of a quote with a gap
        if _words(quote) < QUOTE_LEAST:
            continue
        at = locate(s.story, quote)
        if not at:
            continue
        what = _cut(plain(x.get("what"), 600), WHAT_MOST)
        frame = _cut(plain(x.get("frame"), 600), WHAT_MOST)
        if not what or _copies(what, hay):
            continue
        if frame and _copies(frame, hay):
            frame = ""
        where = _cut(plain(x.get("where"), 300), WHERE_MOST)
        mood = _cut(plain(x.get("mood"), 200), MOOD_MOST)
        if _copies(where, hay):
            where = ""
        if _copies(mood, hay):
            mood = ""
        panel = {"what": what, "where": where,
                 "who": _who(x.get("who"), s.story, known), "mood": mood,
                 "shot": _shot(x.get("shot")), "frame": frame,
                 "quote": _cut(" ".join(s.text[at[0]:at[1]].split()), QUOTE_KEEP), "at": at[0]}
        if PRIVATE.search(json.dumps(panel, ensure_ascii=False)):
            continue
        if any(p["at"] == panel["at"] for p in out):
            continue
        out.append(panel)
    out.sort(key=lambda p: p["at"])
    return out[:PANELS_MOST]


def run(book, sections: list[int], ask: Callable[[list[dict]], str], room: int, saved: dict | None = None,
        progress: Callable[[int, int, str], Any] | None = None, should_stop: Callable[[], bool] | None = None,
        old: dict | None = None) -> dict:
    """Draw these sections as panels. Returns the whole board, JSON-ready: the panels of these sections new, the rest
    kept from ``old`` while their sections are still in the book."""
    saved = {} if saved is None else saved
    want = _wanted(book, sections)
    total = steps(book, want)
    known = _bible_names(book)
    done = 0
    panels: dict[int, list[dict]] = {}
    marks: dict[str, dict] = {}
    skipped: list[dict] = []
    failed: set[int] = set()

    def tell(line: str) -> None:
        if progress:
            progress(done, total, line)

    for n in want:
        s = book.section(n)
        if s is None:
            skipped.append({"section": n, "why": f"There is no section {n} in the book now."})
            continue
        heading = s.heading.strip() or f"Section {s.n}"
        why = _drawable(s)
        if why:
            skipped.append({"section": s.n, "why": why})
            marks[str(s.n)] = {"sha": s.sha, "panels": 0}
            panels[s.n] = []
            continue
        if should_stop and should_stop():
            tell("Stopped")
            raise Stopped()
        tell(f"Drawing {heading}")
        passage = _passage(heading, known, s.story, room)
        if not passage:
            skipped.append({"section": s.n, "why": "There was no room to show this section, so it was not drawn."})
            failed.add(s.n)
            done += 1
            continue
        msgs = _messages(heading, known, passage)
        key = sha_of(json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        try:
            if key not in saved:
                saved[key] = ask(msgs)
            text = saved[key]
        except Stopped:
            raise
        except TIMEOUTS:
            text = None
            skipped.append({"section": s.n, "why": "Your bot's brain ran out of time on this section, so it was not "
                                                  "drawn this time. Draw it again later."})
        except Exception:  # noqa: BLE001 - one section left out, the run goes on
            text = None
            skipped.append({"section": s.n, "why": "Your bot's brain could not answer for this section, so it was not "
                                                  "drawn this time. Draw it again later."})
        done += 1
        if text is None:
            failed.add(s.n)
            continue
        got = _panels(parse_json(str(text)), s, known)
        if got is None:
            saved.pop(key, None)                    # an answer that could not be read is not worth keeping
            skipped.append({"section": s.n, "why": "The answer for this section could not be read, so it was not "
                                                  "drawn this time. Draw it again later."})
            failed.add(s.n)
            continue
        if not got:
            skipped.append({"section": s.n, "why": "No moment drawn for this section could be found in your words, "
                                                  "so no panel was kept."})
        for i, p in enumerate(got, 1):
            p.update(id=f"s{s.n}-{i}", section=s.n, heading=heading, n=i)
        panels[s.n] = [{k: p[k] for k in ("id", "section", "heading", "n", "what", "where", "who", "mood", "shot",
                                          "frame", "quote", "at")} for p in got]
        marks[str(s.n)] = {"sha": s.sha, "panels": len(got)}

    # what the old board had: kept for sections not drawn now (or whose drawing failed), while they are in the book
    old = old if isinstance(old, dict) else {}
    here = {s.n for s in book.sections}
    old_marks = old.get("sections") if isinstance(old.get("sections"), dict) else {}
    every = [p for ps in panels.values() for p in ps]
    for p in old.get("panels") if isinstance(old.get("panels"), list) else []:
        n = p.get("section") if isinstance(p, dict) else None
        if isinstance(n, int) and n in here and (n not in want or n in failed) and n not in panels:
            every.append(p)
    for key, m in old_marks.items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        if n in here and (n not in want or n in failed) and isinstance(m, dict) and str(n) not in marks:
            marks[str(n)] = m
    for e in old.get("skipped") if isinstance(old.get("skipped"), list) else []:
        n = e.get("section") if isinstance(e, dict) else None
        if isinstance(n, int) and n in here and n not in want:
            skipped.append(e)

    order = {s.n: i for i, s in enumerate(book.sections)}
    every.sort(key=lambda p: (order.get(p.get("section"), len(order)), p.get("at") if isinstance(p.get("at"), int)
                              else 0))
    skipped.sort(key=lambda e: order.get(e.get("section"), len(order)))
    tell("Done")
    return {"made_at": now(), "panels": every,
            "sections": {k: marks[k] for k in sorted(marks, key=lambda k: order.get(int(k), len(order)))},
            "skipped": skipped}


def prompt_for(panel: dict) -> str:
    """The picture prompt for one panel, from its notes only: "<what>. <frame>. <where>. Mood: <mood>." Nothing is
    added, her quote never goes, and it is at most 60 words."""
    def part(v: Any) -> str:
        t = plain(v, 600)
        t = re.sub(r"\"[^\"]*\"|\u201c[^\u201d]*\u201d", " ", t)          # nothing she may have written
        t = re.sub(r"[\"\u201c\u201d].*$", " ", t)                           # an unclosed quote, and what follows it
        return " ".join(t.split()).strip(" .,;:")
    bits = [part(panel.get("what")), part(panel.get("frame")), part(panel.get("where"))]
    mood = part(panel.get("mood"))
    text = " ".join(b + "." for b in bits if b) + (f" Mood: {mood}." if mood else "")
    w = text.split()
    if len(w) > PROMPT_MOST:
        text = " ".join(w[:PROMPT_MOST]).rstrip(" .,;:") + "."
    return text


def lines(board: dict) -> list[str]:
    """The panels as plain lines for the chat, one a panel, then what was not drawn."""
    panels = [p for p in (board or {}).get("panels") or [] if isinstance(p, dict)]
    skipped = [e for e in (board or {}).get("skipped") or [] if isinstance(e, dict)]
    if not panels and not skipped:
        return ["There is no storyboard for this book yet."]
    def bit(v: Any) -> str:
        return plain(", ".join(map(str, v)) if isinstance(v, list) else v, 600).strip(" .,;:")
    out = []
    for p in panels:
        line = f"{bit(p.get('heading')) or 'Section ' + str(p.get('section'))}, panel {p.get('n')}: {bit(p.get('what'))}."
        for label, key in (("Where", "where"), ("Who", "who"), ("Mood", "mood")):
            if bit(p.get(key)):
                line += f" {label}: {bit(p.get(key))}."
        if p.get("quote"):
            line += f" Her words: \"{plain(p['quote'], 600)}\""
        out.append(line)
    for e in skipped:
        out.append(f"Section {e.get('section')}: {e.get('why')}")
    return out
