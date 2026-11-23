"""PDF to structured text, for a writer's novel: real paragraphs with heading marks, and a report of what was done.

The same file sits in Manuscript and in Document convert, because apps never import each other. Change one, copy it to
the other. Uses pypdf's visitor, which gives each piece of text with its position, font and size.

``read_pdf(raw)`` answers a :class:`Book`, or raises :class:`Refused` with one plain line for the writer.

The one rule: **no word is invented or reworded.** The only changes, each counted in the report:

- hard line wraps joined into paragraphs, using gaps, indents and how full a line is;
- an end-of-line hyphen taken out when the word really was split, kept when it is a real hyphen (well-known);
- running heads, running feet and page numbers taken out, found by what repeats across pages at the same height;
- ligatures written as plain letters, and odd spaces as plain spaces. Curly quotes, dashes and accents stay;
- a space that pypdf guessed from how far the text moved, taken out when it falls inside a word (Word's justified
  lines fool it: "inve stigated"). The book's own spelling of the word elsewhere decides;
- a scene ornament (a line of only stars or flowers) written ``* * *``, and a large gap between lines kept as three
  blank paragraphs, the two ways Manuscript sees a scene break. A blank paragraph adds no words.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import io
import logging
import math
import re
import statistics
import unicodedata
from collections import Counter
from typing import Any, NamedTuple

LOCKED = "This PDF is locked with a password, so I cannot read it. Save a copy with no password and try again."
PICTURES = "This PDF is pictures of pages, so there is no text to read. Reading scans is not built."
NOT_A_PDF = "That file is not a real PDF, so it will not open as one."
EMPTY = "This PDF has no text and no pictures. Its pages are empty."

BODY, PART, CHAPTER = 0, 1, 2                    # a paragraph's heading mark: Part is Word's Heading 1, Chapter Heading 2

LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st",
             "\ufb06": "st", "\u0132": "IJ", "\u0133": "ij"}
ODD_SPACES = "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\t"
GONE = "\u200b\u200c\u200d\u2060\ufeff"           # no width: nothing to see, so nothing is lost
SPLIT_HYPHENS = "-\u2010"
SOFT = "\u00ad"
GUESS = "\ue000"                                  # stands for a space pypdf added itself, until _settle_guesses decides
# Words that keep their hyphen at a line end when the book never shows the word either way.
KEEP_HYPHEN_AFTER = {"self", "well", "ex", "half", "all", "cross", "great", "long", "short", "twenty", "thirty", "forty",
                     "fifty", "sixty", "seventy", "eighty", "ninety", "one", "two", "three", "four", "five", "six",
                     "seven", "eight", "nine", "ten", "mother", "father", "sister", "brother", "daughter", "son"}
_NUMBER_WORD = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
                r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
                r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|final")
_NUMBER = rf"(?:\d{{1,4}}|[ivxlcdm]+|(?:{_NUMBER_WORD})(?:[ -](?:{_NUMBER_WORD}))?)"
_REST = r"(?:\s*[.:\u2014\u2013-]?\s*(?P<rest>.*))?"
CHAPTER_LINE = re.compile(rf"^(?:chapter|ch\.|letter|section|canto)\s+{_NUMBER}\b{_REST}$", re.IGNORECASE)
PART_LINE = re.compile(rf"^(?:part|book|volume)\s+{_NUMBER}\b{_REST}$", re.IGNORECASE)
ROMAN_LINE = re.compile(r"^(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})\.?$")
NUMBER_LINE = re.compile(r"^\d{1,3}\.?$")
NAMED_LINE = re.compile(r"^(?:prologue|epilogue|interlude|introduction|preface|foreword|afterword|acknowledge?ments)$",
                        re.IGNORECASE)
PAGE_NUMBER = re.compile(r"^[\s\-\u2013\u2014(\[]*(?:page\s*)?(\d{1,4}|[ivxlc]{1,7})(?:\s*(?:of|/)\s*\d{1,4})?[\s\-\u2013\u2014)\]]*$",
                         re.IGNORECASE)
KEPT_BREAK = re.compile(r"^(?:\*\s*\*\s*\*|-{3,}|#|~)$")      # Manuscript's own marks, kept as they are
ORNAMENT = re.compile(r"^(?:[*\u2217\u2022\u00b7\u2219~#_=.\-\u2013\u2014\u25c6\u2666\u25cf\u25aa\s]{3,15}|[\u2042\u2766\u2767\u2619\u25c6\u2666\u2756])$")
LOOKS_FINISHED = re.compile(r"[.?!:;\u2026][\"'\u201d\u2019)\]]*$")
BOLD_FONT = re.compile(r"bold|black|heavy|demi", re.IGNORECASE)


class Refused(Exception):
    """One plain line for the writer: nothing could be read from this file."""


class Para(NamedTuple):
    text: str                        # "" is a blank paragraph; three in a row are a scene break
    heading: int = BODY


class Book(NamedTuple):
    paragraphs: list[Para]
    report: dict[str, Any]

    def tuples(self) -> list[tuple[str, bool]]:
        """(text, is_heading), the shape Manuscript's splitter takes."""
        return [(p.text, p.heading != BODY) for p in self.paragraphs]


class Line:
    __slots__ = ("x", "y", "size", "text", "bold", "page", "after_cap", "sizes", "fonts", "last_x", "end", "spaced")

    def __init__(self, x: float, y: float, size: float, text: str, font: str, page: int) -> None:
        self.x, self.y, self.size, self.text, self.page = x, y, size, text, page
        self.bold = False
        self.after_cap = False                # beside a drop cap: its indent is the cap's, not a new paragraph's
        self.sizes: Counter = Counter()
        self.fonts: Counter = Counter()
        self.last_x = x
        self.end = x
        self._count(text, size, font, y)

    def _count(self, text: str, size: float, font: str, y: float) -> None:
        n = len(text.strip())
        self.sizes[round(size * 4) / 4] += n
        self.fonts[font] += n
        if self.sizes[round(size * 4) / 4] >= max(self.sizes.values()):
            self.y = y                         # the baseline of most of the line, not of a drop cap or a superscript

    def add(self, x: float, y: float, size: float, text: str, font: str) -> None:
        self.text += text
        self.last_x = max(self.last_x, x)
        self.end = max(self.end, x + len(text) * size * 0.5)
        self._count(text, size, font, y)

    def finish(self) -> None:
        self.size = self.sizes.most_common(1)[0][0] if self.sizes else self.size
        self.bold = bool(self.fonts) and bool(BOLD_FONT.search(self.fonts.most_common(1)[0][0]))
        self.spaced = self.text[-1:].isspace()
        self.text = " ".join(self.text.split())

    @property
    def width(self) -> float:
        return len(self.text) * self.size * 0.46


class Sheet:
    """One page of text. A two-up or two-column page is two of these."""

    __slots__ = ("page", "index", "lines", "left", "full", "center", "parity")

    def __init__(self, page: int, index: int, lines: list[Line], center: float) -> None:
        self.page, self.index, self.lines, self.center = page, index, lines, center
        self.left = 0.0
        self.full = 0.0
        self.parity = index % 2


class _Doc:
    """What holds for the whole book: the body size, the line pitch, the usual indent, the words it uses."""

    def __init__(self, sheets: list[Sheet], scanned: bool = False) -> None:
        self.scanned = scanned
        sizes: Counter = Counter()
        for s in sheets:
            for ln in s.lines:
                sizes.update(ln.sizes)
        self.size = sizes.most_common(1)[0][0] if sizes else 11.0
        if scanned:                                  # every line its own size: read them all as the text's
            for s in sheets:
                for ln in s.lines:
                    ln.size = self.size
        pitches = []
        for s in sheets:
            for a, b in zip(s.lines, s.lines[1:]):
                dy = a.y - b.y
                if self._body(a) and self._body(b) and 0.6 * self.size < dy < 2.4 * self.size:
                    pitches.append(dy)
        self.pitch = statistics.median(pitches) if pitches else self.size * 1.25
        gaps = []
        for s in sheets:
            for a, b in zip(s.lines, s.lines[1:]):
                if self._body(a) and self._body(b) and a.y - b.y > 1.45 * self.pitch:
                    gaps.append(a.y - b.y)
        # In a book with a gap between paragraphs a scene break is a gap well past that one.
        usual = statistics.median(gaps) if len(gaps) > 20 else 0.0
        self.scene_gap = max(2.2 * self.pitch, usual * 1.5)
        self._margins(sheets)
        self.words, self.hyphened = set(), set()
        for s in sheets:
            for ln in s.lines:
                for tok in re.findall(r"[^\W\d_]+(?:[-\u2010][^\W\d_]+)*", ln.text):
                    t = tok.lower().replace("\u2010", "-")
                    (self.hyphened if "-" in t else self.words).add(t)

    def _body(self, ln: Line) -> bool:
        return abs(ln.size - self.size) <= 0.1 * self.size

    def _margins(self, sheets: list[Sheet]) -> None:
        by_parity: dict[int, Counter] = {0: Counter(), 1: Counter()}
        all_full: list[int] = []
        for s in sheets:
            body = [ln for ln in s.lines if self._body(ln)]
            all_full += [len(ln.text) for ln in body]
            xs = Counter(round(ln.x) for ln in body)
            s.left = _margin(xs, len(body)) if body else 0.0
            if len(body) >= 4:
                by_parity[s.parity][round(s.left)] += 1
        all_full.sort()
        whole = all_full[int(len(all_full) * 0.9)] if all_full else 60
        offsets: Counter = Counter()
        for s in sheets:
            body = [ln for ln in s.lines if self._body(ln)]
            if len(body) < 4 and by_parity[s.parity]:
                s.left = float(by_parity[s.parity].most_common(1)[0][0])
            chars = sorted(len(ln.text) for ln in body)
            s.full = float(chars[int(len(chars) * 0.9)]) if len(chars) >= 8 else float(whole)
            for ln in body:
                off = ln.x - s.left
                if 0.6 * self.size <= off <= 6 * self.size:
                    offsets[round(off)] += 1
        self.indent = float(offsets.most_common(1)[0][0]) if offsets else 0.0


def _margin(xs: Counter, n: int) -> float:
    """The leftmost x that many lines start at, so one stray line further out does not move the margin."""
    for x in sorted(xs):
        near = sum(c for v, c in xs.items() if abs(v - x) <= 1)
        if near >= max(2, 0.15 * n):
            return float(x)
    return float(min(xs))


# -- pages to lines ------------------------------------------------------------------------------------------

def _matrix(cm: Any, tm: Any) -> tuple[float, float, float, float]:
    """(x, y, how much a font size is scaled, the text's angle in whole degrees) on the page."""
    a = tm[0] * cm[0] + tm[1] * cm[2]
    b = tm[0] * cm[1] + tm[1] * cm[3]
    x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
    y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
    return x, y, math.hypot(a, b) or 1.0, round(math.degrees(math.atan2(b, a)))


def _mark_guessed_spaces() -> None:
    """pypdf adds a space when text moved further than it measured, and it does not count the stretch in a justified
    line, so it splits words. Its check is wrapped here to mark the spaces it added, so they can be weighed. The pypdf
    version is pinned; with another one that has moved this function, the guessed spaces just stay."""
    try:
        from pypdf._text_extraction import _text_extractor as te
        real = te.crlf_space_check
    except (ImportError, AttributeError):
        return
    if getattr(real, "_marks_guesses", False):
        return

    def check(text: str, *args: Any, **kwargs: Any) -> Any:
        got = real(text, *args, **kwargs)
        return (text + GUESS, *got[1:]) if got[0] == text + " " else got

    check._marks_guesses = True                      # type: ignore[attr-defined]
    te.crlf_space_check = check


_LETTERS = r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*['\u2019]?"


def _settle_guesses(sheets: list[Sheet], counts: Counter) -> None:
    """Each space pypdf guessed: inside a word it goes, between words it stays. A guess is wrong when the joined word
    is in the book, in text it did not guess about, and one half is not. It is right when both halves are words and the
    joined one is not. When the book cannot say, what most of its other guesses turned out to be decides."""
    lines = [ln for s in sheets for ln in s.lines if GUESS in ln.text]
    if not lines:
        return
    known: set[str] = set()
    for s in sheets:                                 # the halves of a word split at a line end are not words
        split = False
        for ln in s.lines:
            toks = ln.text.split()
            for i, tok in enumerate(toks):
                if GUESS not in tok and not (i == 0 and split) and tok[-1] not in SPLIT_HYPHENS + SOFT:
                    known.update(w.lower() for w in re.findall(_LETTERS, tok))
            split = bool(toks) and toks[-1][-1] in SPLIT_HYPHENS + SOFT
    known = {w for w in known if len(w) > 1 or w in "aio"}     # a stray letter is no word
    around = re.compile(rf"({_LETTERS})?{GUESS}({_LETTERS})?")
    wrong = right = 0
    for ln in lines:
        for m in around.finditer(ln.text):
            a, b = (m.group(1) or "").lower(), (m.group(2) or "").lower()
            if a and b:
                joined = (a + b) in known
                if joined and not (a in known and b in known):
                    wrong += 1
                elif a in known and b in known and not joined:
                    right += 1
    mostly_wrong = wrong > right

    def settle(m: re.Match) -> str:
        left, after = m.group(1) or "", m.group(2) or ""
        a, b = left.lower(), after.lower()
        if not a or not b:
            return left + " " + after
        joined, ka, kb = (a + b) in known, a in known, b in known
        # With the book unable to say: two unknown halves, or a short known one ("in vestigated"), when guesses here run wrong.
        unsure = not joined and ((not ka and not kb) or (ka != kb and len(a if ka else b) <= 3))
        drop = (joined and not (ka and kb)) or (mostly_wrong and unsure)
        if drop:
            counts["stray_spaces"] += 1
            return left + after
        return left + " " + after

    for ln in lines:
        text = re.sub(rf"\s*{GUESS}[\s{GUESS}]*(?=\s)", " ", ln.text)       # a real space beside it: no question
        text = re.sub(rf"(?<=\s){GUESS}+", "", text)
        text = re.sub(rf"{GUESS}+", GUESS, text)
        spare = len(re.findall(rf"(?<=[^\W_]){GUESS}(?=[-\u2010])|(?<=\d){GUESS}(?=\d)", text))
        counts["stray_spaces"] += spare                 # before a hyphen, or inside a number: never a real space
        text = re.sub(rf"(?<=[^\W_]){GUESS}(?=[-\u2010])|(?<=\d){GUESS}(?=\d)", "", text)
        text = around.sub(settle, text)
        text = re.sub(r"(?<=[^\W_]) +(?=[.,;:!?\u201d)])", "", text)
        ln.text = " ".join(text.split())


def _clean(text: str, counts: Counter) -> str:
    """Ligatures to letters, odd spaces to spaces, nothing else. Curly quotes, dashes and accents stay."""
    out = []
    for ch in text:
        if ch in LIGATURES:
            out.append(LIGATURES[ch])
            counts["ligatures"] += 1
        elif ch in ODD_SPACES:
            out.append(" ")
            counts["odd_spaces"] += 1 if ch != "\t" else 0
        elif ch in GONE:
            counts["odd_spaces"] += 1
        elif ch == "\n" or ch == "\r":
            out.append(" ")
        elif ord(ch) < 32 or ch in "\ufffe\uffff" or 0xd800 <= ord(ch) <= 0xdfff:
            counts["odd_spaces"] += 1                   # control characters: a Word or text file cannot hold them
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _page_lines(page: Any, number: int, counts: Counter) -> list[Line]:
    """The page's text as lines, in the order the file gives them, with sideways text turned upright. Text at some
    other angle than most of the page (a margin stamp) is left out and counted."""
    pieces: list[tuple[float, float, float, str, str, int]] = []

    def visit(text: str, cm: Any, tm: Any, font: Any, size: float) -> None:
        if not text:
            return
        x, y, scale, angle = _matrix(cm, tm)
        name = str((font or {}).get("/BaseFont") or "") if hasattr(font, "get") else ""
        pieces.append((x, y, abs(float(size or 0)) * scale, text, name, angle))

    _mark_guessed_spaces()
    page.extract_text(visitor_text=visit)
    angles = Counter()
    for p in pieces:
        if p[3].strip():
            angles[p[5]] += len(p[3].strip())
    if not angles:
        return []
    angle = angles.most_common(1)[0][0]
    turn = math.radians(angle)
    cos, sin = math.cos(turn), math.sin(turn)
    lines: list[Line] = []
    cur: Line | None = None
    for x0, y0, size, raw, font, a in pieces:
        text = _clean(raw.rstrip("\r\n"), counts)          # the line break pypdf adds is no space on the page
        if not text.strip(" " + GUESS):
            if cur is not None and raw not in ("\n", "\r"):
                cur.text += text
            continue
        if a != angle:
            counts["sideways"] += len(text.split())
            continue
        x, y = x0 * cos + y0 * sin, -x0 * sin + y0 * cos
        if cur is not None:
            near = abs(y - cur.y) <= 0.4 * max(size, max(cur.sizes) if cur.sizes else size)
            jump = x > cur.end + 6 * size or x < cur.last_x - size     # a justified line can stretch four or five
            if near and not jump:
                cur.add(x, y, size, text, font)
                continue
        cur = Line(x, y, size, text, font, number)
        cur.end = x + len(text) * size * 0.5
        lines.append(cur)
    for ln in lines:
        ln.finish()
    return _join_drop_caps(lines)


def _join_drop_caps(lines: list[Line]) -> list[Line]:
    """A big first letter set apart from its word: it goes back on the word, and the lines beside it are marked so
    their indent is not read as a new paragraph. A cap that shared a line with its word was joined when the line was
    built, and is found here by the second size on that line."""
    out: list[Line] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        word = lines[i + 1] if i + 1 < len(lines) else None
        lone = re.fullmatch(r"[\u2018\u201c\"'(]?[^\W\d_]", ln.text.strip(GUESS)) is not None
        if lone and word is not None and ln.size >= 1.5 * word.size and abs(ln.y - word.y) <= 2.2 * ln.size:
            # A cap that is a whole word (A, I, O): whether a space follows is left to _settle_guesses.
            cap = ln.text.strip(GUESS)
            glue = " " if ln.spaced else GUESS if cap[-1] in "AIO" else ""
            word.text = cap + glue + word.text.lstrip(GUESS)
            word.x = ln.x
            _mark_beside_cap(lines, i + 2, ln.y)
            i += 1
            continue
        sizes = sorted(s for s, n in ln.sizes.items() if n)
        if len(sizes) > 1 and ln.sizes[sizes[-1]] == 1 and sizes[-1] >= 1.5 * ln.size:
            _mark_beside_cap(lines, i + 1, ln.y - 1.2 * sizes[-1])
        out.append(ln)
        i += 1
    return out


def _mark_beside_cap(lines: list[Line], start: int, floor: float) -> None:
    for ln in lines[start:start + 6]:
        if ln.y < floor - 2:
            break
        ln.after_cap = True


def _sheets_of(page_lines: list[Line], width: float, page: int, index: int, counts: Counter) -> list[Sheet]:
    """One sheet, or two when the page is two-up or in two columns: the left column's lines, then the right's."""
    left, right, wide = [], [], []
    for ln in page_lines:
        if ln.x >= 0.47 * width:
            right.append(ln)
        elif ln.x + ln.width <= 0.56 * width:
            left.append(ln)
        else:
            wide.append(ln)
    rows = Counter(round(ln.y / 2) for ln in page_lines)
    if sum(1 for n in rows.values() if n >= 3) >= 4:
        counts["table_pages"] += 1
        counts.setdefault("_tables", []).append(page)       # type: ignore[arg-type]
    ordered = lambda ls: sorted(ls, key=lambda ln: (-round(ln.y / 2), ln.x))     # noqa: E731
    long_left = sum(1 for ln in left if len(ln.text) > 20)
    long_right = sum(1 for ln in right if len(ln.text) > 20)
    if long_left >= 5 and long_right >= 5 and len(wide) <= 0.2 * len(page_lines):
        counts.setdefault("_columns", []).append(page)      # type: ignore[arg-type]
        top = max(ln.y for ln in left + right)
        above = [ln for ln in wide if ln.y >= top]
        below = [ln for ln in wide if ln.y < top]
        return [Sheet(page, index, ordered(above + left + below), 0.25 * width),
                Sheet(page, index + 1, ordered(right), 0.75 * width)]
    return [Sheet(page, index, ordered(page_lines), width / 2)]


# -- running heads, feet and page numbers ----------------------------------------------------------------------

def _roman_value(text: str) -> int | None:
    t = text.lower()
    if not re.fullmatch(r"[ivxlc]+", t):
        return None
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
    total = 0
    for a, b in zip(t, t[1:] + " "):
        total += -vals[a] if vals.get(b, 0) > vals[a] else vals[a]
    return total if total > 0 else None


def _page_number(text: str) -> int | None:
    m = PAGE_NUMBER.match(text)
    if not m:
        return None
    word = m.group(1)
    if word.isdigit():
        return int(word)
    return _roman_value(word) if word.islower() else None


def _clusters(items: list[tuple[float, Line, Sheet]], most: float) -> list[list[tuple[float, Line, Sheet]]]:
    """Items grouped by height: two within *most* points of each other are one group."""
    groups: list[list[tuple[float, Line, Sheet]]] = []
    for item in sorted(items, key=lambda t: t[0]):
        if groups and item[0] - groups[-1][-1][0] <= most:
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def _edges(s: Sheet) -> list[Line]:
    """The lines a head or foot could be: the first and last few on the page."""
    return list(dict.fromkeys(s.lines[:3] + s.lines[-3:]))


def _strip_page_furniture(sheets: list[Sheet], doc: _Doc, counts: Counter) -> None:
    """Takes out page numbers, then running heads and feet. Each kind must repeat at the same height, and with the same
    size, so a chapter heading or a line of dialogue that happens to match stays."""
    n = len([s for s in sheets if s.lines])
    if n < 2:
        return
    gone: set[int] = set()

    # Page numbers: a line that is only a number, whose number keeps step with the page it is on.
    offsets: dict[int, list[tuple[float, Line, Sheet]]] = {}
    for s in sheets:
        for ln in _edges(s):
            value = _page_number(ln.text)
            if value is not None:
                offsets.setdefault(value - s.index, []).append((ln.y, ln, s))
    enough = max(2, math.ceil(0.05 * n))
    for items in offsets.values():
        for group in _clusters(items, 5.0):
            if len(group) >= enough:
                for _y, ln, _s in group:
                    gone.add(id(ln))
                    counts["page_numbers"] += 1
    # The same, when the numbering does not keep step (restarts, skipped blanks): a number at one height on many pages.
    numbers = [(ln.y, ln, s) for s in sheets for ln in s.lines[-2:] + s.lines[:2]
               if id(ln) not in gone and _page_number(ln.text) is not None]
    for group in _clusters(numbers, 3.0):
        if len(group) >= max(3, 0.4 * n):
            for _y, ln, _s in group:
                gone.add(id(ln))
                counts["page_numbers"] += 1

    # Running heads and feet: the same words at one height, with any number in them keeping step with the page.
    same: dict[str, list[tuple[float, Line, Sheet]]] = {}
    for s in sheets:
        for ln in _edges(s):
            if id(ln) not in gone and len(ln.text) <= 120 and not ORNAMENT.match(ln.text):
                same.setdefault(" ".join(ln.text.casefold().split()), []).append((ln.y, ln, s))
                shape = re.sub(r"\d+", "#", " ".join(ln.text.casefold().split()))
                if "#" in shape and not _says_heading(ln.text):    # one-page chapters step with the page too
                    same.setdefault("\0" + shape, []).append((ln.y, ln, s))
    enough = max(2 if n <= 4 else 3, math.ceil(0.05 * n))
    for key, items in same.items():
        for group in _clusters(items, 3.0):
            if len(group) < enough:
                continue
            if key.startswith("\0") and not _numbers_keep_step(group):
                continue
            sizes = Counter(round(ln.size) for _y, ln, _s in group)
            usual = sizes.most_common(1)[0][0]
            for _y, ln, _s in group:
                if id(ln) not in gone and abs(ln.size - usual) <= 0.6:
                    gone.add(id(ln))
                    counts["running_heads"] += 1

    # Heads that change with the chapter: most pages have a short line at one height, set apart from the text.
    for pick in (lambda s, kept: kept[0], lambda s, kept: kept[-1]):
        items = []
        for s in sheets:
            kept = [ln for ln in s.lines if id(ln) not in gone]
            if len(kept) >= 3:
                ln = pick(s, kept)
                other = kept[1] if ln is kept[0] else kept[-2]
                if abs(ln.y - other.y) >= 1.3 * doc.pitch and len(ln.text.split()) <= 12 and not _says_heading(ln.text):
                    items.append((ln.y, ln, s))
        for group in _clusters(items, 2.0):
            if len(group) >= max(3, 0.5 * n):
                for _y, ln, _s in group:
                    if id(ln) not in gone and not _numbered(ln.text):
                        gone.add(id(ln))
                        counts["running_heads"] += 1

    # A head whose words came out a little different (a long line cut short): the first or last line of a page, at the
    # height and size most of the heads were taken from, starting the way they start.
    taken = [(ln.y, ln, s) for s in sheets for ln in s.lines if id(ln) in gone]
    for group in _clusters(taken, 1.5):
        if len(group) >= max(3, 0.4 * n):
            usual = Counter(round(ln.size) for _y, ln, _s in group).most_common(1)[0][0]
            low, high = group[0][0] - 1.5, group[-1][0] + 1.5
            starts = {ln.text.casefold()[:8] for _y, ln, _s in group}
            for s in sheets:
                kept = [ln for ln in s.lines if id(ln) not in gone]
                for ln in kept[:1] + kept[-1:]:
                    if low <= ln.y <= high and abs(ln.size - usual) <= 0.6 and (
                            ln.text.casefold()[:8] in starts or ln.text.isdigit()):
                        gone.add(id(ln))
                        counts["running_heads"] += 1

    for s in sheets:
        s.lines = [ln for ln in s.lines if id(ln) not in gone]


def _numbers_keep_step(group: list[tuple[float, Line, Sheet]]) -> bool:
    """Is the number in these lines the page number? Its distance from the page's place in the file stays the same."""
    seen: Counter = Counter()
    for _y, ln, s in group:
        for place, m in enumerate(re.finditer(r"\d+", ln.text)):
            seen[(place, int(m.group()) - s.index)] += 1
    return bool(seen) and seen.most_common(1)[0][1] >= 0.6 * len(group)


# -- lines to paragraphs ------------------------------------------------------------------------------------

class _Open:
    """A paragraph being built."""

    __slots__ = ("lines", "text", "top", "gap_before", "gap_after", "sheet", "bottom")

    def __init__(self, ln: Line, sheet: Sheet, top: bool, gap: float) -> None:
        self.lines = [ln]
        self.text = ln.text
        self.sheet = sheet
        self.top = top
        self.gap_before = gap
        self.gap_after = 0.0
        self.bottom = False

    @property
    def size(self) -> float:
        return self.lines[0].size


def _numbered(text: str) -> bool:
    t = text.strip()
    return bool(CHAPTER_LINE.match(t) or PART_LINE.match(t) or ROMAN_LINE.match(t) or NUMBER_LINE.match(t))


def _says_heading(text: str) -> bool:
    """A whole line that names a chapter or part, or is a roman numeral. "Chapter 3 was the worst" is a sentence."""
    t = text.strip()
    for pat in (CHAPTER_LINE, PART_LINE):
        m = pat.match(t)
        if m and _rest_ok(m):
            return True
    return bool(ROMAN_LINE.match(t)) and t != "I"


def _word_would_fit(a: Line, b: Line, s: Sheet) -> bool:
    """Would the next line's first word have fit on this line? Then the line was ended on purpose. A line that stops
    mid-sentence has to be much shorter before that is believed."""
    first = b.text.split(" ", 1)[0]
    used = len(a.text) + 1 + len(first)
    return used <= s.full * (0.88 if LOOKS_FINISHED.search(a.text) else 0.62)


def _ends_split(text: str) -> bool:
    return text[-1:] in SPLIT_HYPHENS or text[-1:] == SOFT


def _starts_lower(text: str) -> bool:
    return text[:1].islower()


def _new_paragraph(a: Line, b: Line, s: Sheet, doc: _Doc) -> bool:
    """Two lines on one sheet: does the second start a new paragraph?"""
    if not doc._body(b) or not doc._body(a):           # a heading's size against the text's
        if abs(a.size - b.size) > 0.1 * max(a.size, b.size):
            return True
        return a.y - b.y > 1.7 * a.size or (_numbered(a.text) and _numbered(b.text))
    if _says_heading(a.text) or _says_heading(b.text):  # "Chapter 3" set like the text is still a line of its own
        return True
    dy = a.y - b.y
    continues = (_ends_split(a.text) or not LOOKS_FINISHED.search(a.text)) and _starts_lower(b.text)
    if dy > 1.45 * doc.pitch:
        return not (continues and dy < 2.2 * doc.pitch)
    if b.after_cap:
        return False
    if _ends_split(a.text) and _starts_lower(b.text):
        return False
    tol = 0.6 * doc.size
    xa, xb = a.x - s.left, b.x - s.left
    if xb >= tol:
        usual = doc.indent and abs(xb - doc.indent) <= 2.0
        if xa < tol:
            return True                                 # flush, then indented
        if abs(xa - xb) <= 1.5:
            return bool(usual) or _word_would_fit(a, b, s)   # two indented lines: dialogue, or a block quote's wrap
        return True                                     # a centered line, or verse
    if xa >= tol and a.x + a.width < s.center + (s.center - s.left) * 0.5 and abs(a.x + a.width / 2 - s.center) < 0.12 * s.center:
        return True                                     # a centered line above
    return _word_would_fit(a, b, s)


def _new_paragraph_over_page(a: Line, b: Line, sa: Sheet, sb: Sheet, doc: _Doc) -> bool:
    """The last line of one sheet and the first of the next. With no gap to go on, the indent and the line's fullness."""
    if not doc._body(b) or not doc._body(a) or _says_heading(a.text) or _says_heading(b.text):
        return True
    if (_ends_split(a.text) or not LOOKS_FINISHED.search(a.text)) and _starts_lower(b.text):
        return False
    if b.x - sb.left >= 0.6 * doc.size:
        return True
    return _word_would_fit(a, b, sa)


def _join(a: str, b: str, doc: _Doc, counts: Counter) -> str:
    """The next line onto the paragraph. A hyphen goes only when the word really was split."""
    if a.endswith(SOFT):
        counts["hyphens_joined"] += 1
        return a[:-1] + b
    if a[-1:] in SPLIT_HYPHENS and b[:1].isalpha():
        m = re.search(r"([^\W\d_]+)[-\u2010]$", a)
        r = re.match(r"[^\W\d_]+", b)
        if m and r and b[:1].islower():
            left, right = m.group(1).lower(), r.group().lower()
            joined, hyphened = left + right, f"{left}-{right}"
            keep = (hyphened in doc.hyphened and joined not in doc.words) or (
                joined not in doc.words and left in KEEP_HYPHEN_AFTER)
            if not keep:
                counts["hyphens_joined"] += 1
                return a[:-1] + b
        return a + b                                    # a real hyphen, as in Anglo-Saxon: kept, with no space
    if a[-1:] in "\u2014\u2013" or a.endswith("--") or b[:1] in "\u2014\u2013":
        return a + b                                    # a dash runs on with no space, as it does on the page
    return a + " " + b


def _paragraphs(sheets: list[Sheet], doc: _Doc, counts: Counter) -> list[_Open | None]:
    """Paragraphs in order. None is a large gap between lines: a scene break."""
    out: list[_Open | None] = []
    cur: _Open | None = None
    prev: Line | None = None
    prev_sheet: Sheet | None = None
    for s in sheets:
        for i, ln in enumerate(s.lines):
            if prev is None:
                cur = _Open(ln, s, True, 1e9)
                out.append(cur)
            else:
                same = prev_sheet is s
                new = _new_paragraph(prev, ln, s, doc) if same else _new_paragraph_over_page(prev, ln, prev_sheet, s, doc)
                dy = prev.y - ln.y if same else 1e9
                if new:
                    cur.gap_after = dy
                    if same and dy >= doc.scene_gap and doc._body(prev) and doc._body(ln):
                        out.append(None)
                    cur = _Open(ln, s, i == 0, dy)
                    out.append(cur)
                else:
                    cur.text = _join(cur.text, ln.text, doc, counts)
                    cur.lines.append(ln)
                    counts["lines_joined"] += 1
            prev, prev_sheet = ln, s
            if i == len(s.lines) - 1 and cur is not None:
                cur.bottom = True
    if cur is not None:
        cur.gap_after = 1e9
    return out


# -- headings -------------------------------------------------------------------------------------------------

def _rest_ok(m: re.Match) -> bool:
    rest = (m.groupdict().get("rest") or "").strip()
    if not rest:
        return True
    return len(rest.split()) <= 12 and not rest[0].islower() and not rest.endswith((",", ";"))


def _centered(p: _Open) -> bool:
    ln = p.lines[0]
    s = p.sheet
    span = max(s.center - s.left, 1.0)
    return ln.x - s.left >= 1.5 * ln.size and abs(ln.x + ln.width / 2 - s.center) <= 0.2 * span


def _mark(p: _Open, doc: _Doc) -> int:
    """This paragraph's heading mark, by its words first, then its look. A paragraph of many lines is no heading."""
    t = p.text.strip()
    if not t or len(t) > 120 or len(t.split()) > 15 or len(p.lines) > 3:
        return BODY
    m = PART_LINE.match(t)
    if m and _rest_ok(m):
        return PART
    m = CHAPTER_LINE.match(t)
    if m and _rest_ok(m):
        return CHAPTER
    if doc.scanned:                                  # a scanner's hidden text: its sizes and places are too rough to trust
        return BODY
    alone = p.top or p.gap_before >= 1.3 * doc.pitch
    bigger = p.size >= 1.15 * doc.size
    if t[0].islower() or _ends_split(t):
        return BODY
    if ROMAN_LINE.match(t) or NUMBER_LINE.match(t):
        return CHAPTER if (alone or bigger or _centered(p)) and t != "I" or (t == "I" and (bigger or (alone and _centered(p)))) else BODY
    if NAMED_LINE.match(t) and alone:
        return CHAPTER
    if t.endswith((",", ";")) or (ORNAMENT.match(t) or KEPT_BREAK.match(t)):
        return BODY
    if bigger:
        return CHAPTER
    if len(t.split()) > 8 or LOOKS_FINISHED.search(t):
        return BODY
    if all(ln.bold for ln in p.lines) and alone and p.gap_after >= 1.1 * doc.pitch:
        return CHAPTER
    letters = [c for c in t if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and _centered(p) and (p.top or p.gap_before >= doc.scene_gap):
        return CHAPTER
    return BODY


def _wordy(text: str) -> bool:
    """A heading that says what it is in words, so the front matter's bigger type can be told from it."""
    return bool(CHAPTER_LINE.match(text) or PART_LINE.match(text) or ROMAN_LINE.match(text) or NUMBER_LINE.match(text)
                or NAMED_LINE.match(text))


def _headed(items: list[_Open | None], doc: _Doc, counts: Counter) -> list[Para]:
    marks = [(_mark(p, doc) if p is not None else BODY) for p in items]
    # Headings found only by their look, far more of them than a book has chapters: the look means nothing here.
    pages = len({p.sheet.page for p in items if p is not None})
    looks = [i for i, p in enumerate(items) if p is not None and marks[i] and not _wordy(p.text.strip())]
    if len(looks) > max(40, 0.5 * pages):
        for i in looks:
            marks[i] = BODY
    # Bigger type before the first heading that says "Chapter" is the title page, not a chapter.
    first = next((i for i, p in enumerate(items) if p is not None and marks[i] and _wordy(p.text.strip())), None)
    if first is not None:
        for i in range(first):
            p = items[i]
            if p is not None and marks[i] and not NAMED_LINE.match(p.text.strip()) and not _wordy(p.text.strip()):
                marks[i] = BODY
    out: list[Para] = []
    i = 0
    while i < len(items):
        p = items[i]
        if p is None:
            out += [Para(""), Para(""), Para("")]
            counts["scene_breaks"] += 1
            i += 1
            continue
        mark = marks[i]
        t = " ".join(p.text.split())
        if mark == BODY:
            bare = t.replace(" ", "")
            if KEPT_BREAK.match(t) or KEPT_BREAK.match(bare):
                counts["scene_breaks"] += 1
            elif ORNAMENT.match(t) and len(p.lines) == 1 and not re.fullmatch(r"[.\-\u2013\u2014_=\s]{1,2}", t):
                t = "* * *"
                counts["scene_breaks"] += 1
                counts["scene_marks_rewritten"] += 1
            out.append(Para(t))
            i += 1
            continue
        # A run of headings with no text between them is a contents list: one paragraph, so it makes no empty sections.
        # The last one, with text after it, is the real first chapter.
        j = i
        while j + 1 < len(items) and items[j + 1] is not None and marks[j + 1] and _wordy(items[j + 1].text.strip()):
            j += 1
        if j + 1 < len(items) and items[j + 1] is not None and not marks[j + 1]:
            j -= 1
        if j - i >= 2 and _wordy(t) and all(items[k].sheet.page - p.sheet.page <= 3 for k in range(i, j + 1)):
            out.append(Para(" ".join(" ".join(items[k].text.split()) for k in range(i, j + 1))))
            counts["contents_lines"] += j - i + 1
            i = j + 1
            continue
        # "Chapter 3", then its title on the next line: one heading.
        nxt = items[i + 1] if i + 1 < len(items) else None
        if (mark == CHAPTER and nxt is not None and _numbered_only(t) and nxt.sheet is p.sheet
                and p.gap_after <= 4 * doc.pitch and _looks_a_title(nxt, doc)):
            t = f"{t} {' '.join(nxt.text.split())}"
            i += 1
        out.append(Para(t, mark))
        i += 1
    return out


def _numbered_only(t: str) -> bool:
    for pat in (CHAPTER_LINE, PART_LINE):
        m = pat.match(t)
        if m:
            return not (m.groupdict().get("rest") or "").strip()
    return bool(ROMAN_LINE.match(t) or NUMBER_LINE.match(t))


def _looks_a_title(p: _Open, doc: _Doc) -> bool:
    t = " ".join(p.text.split())
    if _wordy(t) or not t or len(t.split()) > 12 or LOOKS_FINISHED.search(t) or len(p.lines) > 2:
        return False
    return p.size >= 1.1 * doc.size or p.lines[0].bold or _centered(p)


# -- the whole file -----------------------------------------------------------------------------------------

def _open(raw: bytes) -> Any:
    from pypdf import PdfReader                      # a missing pypdf is the caller's ImportError
    from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    if b"%PDF-" not in raw[:1024]:
        raise Refused(NOT_A_PDF)
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Refused(LOCKED)
        len(reader.pages)
    except Refused:
        raise
    except (FileNotDecryptedError, DependencyError):
        raise Refused(LOCKED) from None
    except PdfReadError as exc:
        raise Refused(LOCKED if "password" in str(exc).lower() or "decrypt" in str(exc).lower() else NOT_A_PDF) from None
    except Exception:  # noqa: BLE001 - a broken file of any kind: a plain line, never a stack
        raise Refused(NOT_A_PDF) from None
    return reader


def _has_picture(page: Any, depth: int = 0) -> bool:
    """Does the page draw a picture? Read from its resources, one form down, without decoding any picture."""
    try:
        things = (page.get("/Resources") or {}).get_object().get("/XObject") or {}
        for thing in things.get_object().values():
            thing = thing.get_object()
            kind = thing.get("/Subtype")
            if kind == "/Image" or (kind == "/Form" and depth < 2 and _has_picture(thing, depth + 1)):
                return True
    except Exception:  # noqa: BLE001 - damaged resources: not counted as a picture
        return False
    return False


def _named(pages: list[int], most: int = 12) -> str:
    """"3, 7 and 9", the first few of a long list."""
    if len(pages) > most:
        return ", ".join(map(str, pages[:most])) + f" and {len(pages) - most} more"
    if len(pages) > 1:
        return ", ".join(map(str, pages[:-1])) + f" and {pages[-1]}"
    return str(pages[0]) if pages else ""


def _garbled(paras: list[Para]) -> bool:
    """A font with no letter map gives wrong letters or empty boxes. No word is changed for it; the writer is told."""
    text = " ".join(p.text for p in paras)
    if len(text) < 200:
        return False
    odd = sum(1 for c in text if c == "\ufffd" or unicodedata.category(c) in ("Co", "Cn"))
    words = re.findall(r"\S+", text)
    plain = sum(1 for w in words if re.fullmatch(r"\W*[^\W\d_]+(?:['\u2019\-\u2010][^\W\d_]+)*\W*", w))
    return odd > 0.01 * len(text) or plain < 0.5 * len(words)


def read_pdf(raw: bytes) -> Book:
    """PDF bytes to paragraphs and a report. Raises :class:`Refused` for a file with nothing to read."""
    reader = _open(raw)
    counts: Counter = Counter()
    pages = list(reader.pages)
    sheets: list[Sheet] = []
    index = 0
    unread: list[int] = []
    no_text_pictures: list[int] = []
    for number, page in enumerate(pages, 1):
        try:
            lines = _page_lines(page, number, counts)
        except Exception:  # noqa: BLE001 - one bad page must not lose the book; the report names it
            lines = []
            unread.append(number)
        lines = [ln for ln in lines if any(c.isalnum() or not c.isspace() for c in ln.text)]
        try:
            width = float(page.mediabox.width)
        except Exception:  # noqa: BLE001
            width = 612.0
        got = _sheets_of(lines, width, number, index, counts) if lines else [Sheet(number, index, [], width / 2)]
        sheets += got
        index += len(got)
    _settle_guesses(sheets, counts)
    for s in sheets:
        s.lines = [ln for ln in s.lines if ln.text]
    doc = _Doc([s for s in sheets if s.lines])
    _strip_page_furniture(sheets, doc, counts)
    has_text = {s.page for s in sheets if any(any(c.isalnum() for c in ln.text) for ln in s.lines)}
    empty_pages = [n for n in range(1, len(pages) + 1) if n not in has_text]
    for n in empty_pages:
        if _has_picture(pages[n - 1]):
            no_text_pictures.append(n)
    if not has_text:
        raise Refused(PICTURES if no_text_pictures else EMPTY)
    live = [s for s in sheets if s.lines]
    # Text over a picture on most pages is a scanner's hidden text, not a typeset book.
    scanned = len(has_text) >= 3 and sum(1 for n in has_text if _has_picture(pages[n - 1])) >= 0.6 * len(has_text)
    doc = _Doc(live, scanned)                         # again, with the heads gone: the margins and pitch are the text's
    paras = _headed(_paragraphs(live, doc, counts), doc, counts)
    while paras and not paras[0].text:
        paras.pop(0)
    while paras and not paras[-1].text:
        paras.pop()
    chapters = sum(1 for p in paras if p.heading == CHAPTER)
    parts = sum(1 for p in paras if p.heading == PART)
    warnings = []
    if scanned:
        warnings.append("This PDF is pictures of pages with a scanner's hidden text behind them, so expect wrong letters "
                        "and paragraphs broken in the wrong places.")
    if no_text_pictures:
        warnings.append(f"Pages {_named(no_text_pictures)} are pictures with no text, so their words are not here."
                        if len(no_text_pictures) > 1 else
                        f"Page {no_text_pictures[0]} is a picture with no text, so its words are not here.")
    if unread:
        warnings.append(f"I could not read page{'s' if len(unread) > 1 else ''} {_named(unread)}, so {'they are' if len(unread) > 1 else 'it is'} left out.")
    if chapters + parts == 0:
        warnings.append("I found no chapter headings, so the book will be split by its scene breaks or by length.")
    if counts.get("_columns"):
        warnings.append(f"Page{'s' if len(counts['_columns']) > 1 else ''} {_named(counts['_columns'])} "
                        f"{'are' if len(counts['_columns']) > 1 else 'is'} two pages or two columns side by side, "
                        "so check the order of the text there.")
    if counts.get("_tables"):
        warnings.append(f"Page{'s' if len(counts['_tables']) > 1 else ''} {_named(counts['_tables'])} look{'' if len(counts['_tables']) > 1 else 's'} "
                        "like a table, so the text there may read out of order.")
    if counts["sideways"]:
        warnings.append(f"I left out {counts['sideways']} words set at an angle to the page, such as a stamp in a margin.")
    if _garbled(paras):
        warnings.append("Much of this PDF's text came out as the wrong letters, because of how its fonts were made. "
                        "If the sections look wrong, save the book from the program that made it as a Word file.")
    words = sum(len(p.text.split()) for p in paras if not p.heading)      # the text's words, as Manuscript counts them
    report = {"pages": len(pages), "words": words, "pages_no_text": empty_pages,
              "pages_pictures": no_text_pictures, "pages_unread": unread,
              "running_heads_removed": counts["running_heads"], "page_numbers_removed": counts["page_numbers"],
              "hyphens_joined": counts["hyphens_joined"], "chapters_found": chapters, "parts_found": parts,
              "scene_breaks": counts["scene_breaks"], "scene_marks_rewritten": counts["scene_marks_rewritten"],
              "lines_joined": counts["lines_joined"], "ligatures": counts["ligatures"],
              "odd_spaces": counts["odd_spaces"], "stray_spaces_removed": counts["stray_spaces"], "contents_lines": counts["contents_lines"],
              "pages_two_up": list(counts.get("_columns") or []), "pages_tables": list(counts.get("_tables") or []),
              "words_left_out_sideways": counts["sideways"], "scanned_text": scanned, "warnings": warnings}
    return Book(paras, report)


def report_line(report: dict[str, Any]) -> str:
    """The report in plain words, for the writer's status line and the chat."""
    n = report["chapters_found"]
    found = f"{n} chapter{'' if n == 1 else 's'} found"
    if report["parts_found"]:
        found += f" in {report['parts_found']} part{'' if report['parts_found'] == 1 else 's'}"
    line = (f"From the PDF: {report['pages']:,} pages, {report['words']:,} words, {found}, "
            f"{report['running_heads_removed'] + report['page_numbers_removed']:,} running heads and page numbers "
            f"removed, {report['hyphens_joined']:,} hyphens joined.")
    if report["pages_no_text"] and not report["pages_pictures"]:
        line += f" No text on pages {_named(report['pages_no_text'])}." if len(report["pages_no_text"]) > 1 else \
            f" No text on page {report['pages_no_text'][0]}."
    return " ".join([line] + list(report["warnings"]))


def plain_text(book: Book) -> str:
    """The book as text: a blank line between paragraphs, a heading on its own line."""
    out = []
    blanks = 0
    for p in book.paragraphs:
        if not p.text:
            blanks += 1
            continue
        if blanks and out:
            out.append("")
        blanks = 0
        out.append(p.text)
    return "\n\n".join(out)
