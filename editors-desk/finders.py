"""What a program can find without asking the brain: every finder takes a Book and gives candidates with real quotes.

A candidate is ``{key, what, why, fix, where: [{section, quote}], size, judge?, terms?, correct?}``. ``key`` keeps the
writer's mark through a re-run. ``judge`` sends it to the brain (a flashback can explain a date, a code match can be
wrong). ``correct`` is an exact correction, for a Use this button. A check file names its finders in ``find:``.

Every field of the story bible may be missing: a book read before Manuscript 0.3.3 has no looks or distances, and an
unread one has no bible. A finder that lacks its field looks in the text, or finds nothing; it never fails.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable, Iterable

from book import PRIVATE, Book, Section, locate

FINDERS: dict[str, Callable[[Book], list[dict]]] = {}
QUOTE_MAX = 220
SIZES = ("big", "medium", "small")


def finder(fn: Callable[[Book], list[dict]]) -> Callable[[Book], list[dict]]:
    FINDERS[fn.__name__] = fn
    return fn


def run(name: str, book: Book) -> list[dict]:
    return FINDERS[name](book) if name in FINDERS else []


# -- sentences and quotes -------------------------------------------------------------------------------------------

_SENT = re.compile(r"[^.!?\n]+(?:[.!?]+[\"'”’]*|$)", re.MULTILINE)
_WORD = re.compile(r"[A-Za-zÀ-ɏ]+(?:['’-][A-Za-zÀ-ɏ]+)*")
STOP = set("the a an and or of to in on at for with from by is are was were be been it its this that these those he she "
           "they his her their him them i you we not but as so if then than there here had has have do did what who how "
           "section sections into out up down over about when where which while would could should will can may might "
           "very just only also".split())


def sentences(text: str) -> Iterable[tuple[int, int]]:
    for m in _SENT.finditer(text):
        a, b = m.start(), m.end()
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            yield a, b


def quote_at(text: str, i: int, j: int | None = None) -> str:
    """The sentence around a place in the text, exactly as written, cut to a sensible length around that place."""
    j = i if j is None else j
    for a, b in sentences(text):
        if a <= i < b or (i == b):
            if b - a <= QUOTE_MAX:
                return text[a:b]
            start = max(a, i - QUOTE_MAX // 2)
            return text[start:min(b, start + QUOTE_MAX)].strip()
    return text[max(0, i - 80):j + 80].strip()[:QUOTE_MAX].strip()


def words_of(text: Any) -> set:
    return {w.lower() for w in _WORD.findall(str(text or "")) if len(w) > 2 and w.lower() not in STOP}


def best_sentence(sec: Section, about: Any) -> str:
    """The sentence of a section that shares the most words with ``about``, as written."""
    want = words_of(about)
    best, score = "", 0
    for a, b in sentences(sec.story):
        got = len(want & words_of(sec.story[a:b]))
        if got > score:
            best, score = sec.story[a:b], got
    if best:
        return best[:QUOTE_MAX].strip()
    first = next(iter(sentences(sec.story)), None)
    return sec.story[first[0]:first[1]][:QUOTE_MAX].strip() if first else ""


def where(book: Book, n: Any, about: Any) -> list[dict]:
    s = book.section(n)
    q = best_sentence(s, about) if s else ""
    return [{"section": s.n, "quote": q}] if q else []


def ints(v: Any) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, bool):
            continue
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str):
            m = re.search(r"\d+", x)
            if m:
                out.append(int(m.group()))
    return sorted(set(out))


def people(book: Book) -> list[dict]:
    """Each character with every name to look for: the full name, its first and last word, real aliases."""
    out = []
    for c in book.list_of("characters"):
        name = str(c.get("name") or "").strip()
        if not name or PRIVATE.search(name):          # a name the vault holds is no name to search for
            continue
        names = {name}
        names.update(str(a).strip() for a in c.get("aliases") or [] if isinstance(a, str) and a.strip() and not PRIVATE.search(a))
        parts = [p for p in name.split() if p.lower() not in ("mr", "mrs", "ms", "miss", "dr", "the", "captain", "old", "professor", "inspector", "sir", "lady", "lord")]
        if parts:
            names.update({parts[0], parts[-1]})
        names = {n for n in names if len(n) > 2 and n.lower() not in STOP}
        out.append({"c": c, "name": name, "id": str(c.get("id") or name.lower()), "names": sorted(names, key=len, reverse=True)})
    return out


def _names_re(person: dict) -> str:
    return r"(?:" + "|".join(re.escape(n) for n in person["names"]) + r")"


def _main(book: Book) -> list[dict]:
    return [c for c in book.list_of("characters") if c.get("importance") == "main"
            or str(c.get("role") or "").lower() in ("protagonist", "antagonist", "narrator")]


def _base_heading(h: str) -> str:
    return re.sub(r",\s*part\s+\d+\s*$", "", h, flags=re.IGNORECASE)


# -- plot ---------------------------------------------------------------------------------------------------------

@finder
def open_threads(book: Book) -> list[dict]:
    """A thread with a setup and no payoff, or one that stops well before the end of the book."""
    out = []
    last = max((s.n for s in book.sections), default=0)
    for t in book.list_of("threads"):
        pays = [p for p in t.get("payoffs") or [] if str(p).strip()]
        if pays and not t.get("open"):
            continue
        secs = ints(t.get("sections"))
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        setups = " ".join(str(x) for x in t.get("setups") or [])
        spot = where(book, secs[0], f"{name} {setups}") if secs else []
        dropped = secs and last >= 4 and secs[-1] <= last * 0.6
        if len(secs) > 1 and dropped:
            spot += where(book, secs[-1], f"{name} {setups}")
        main = "main" in str(t.get("kind") or "")
        ending = f"It is last touched in section {secs[-1]} of {last}" if secs else "It never comes back"
        out.append({"key": f"thread:{t.get('id') or name.lower()}", "judge": True, "size": "big" if main else "medium",
                    "what": f"The thread \"{name}\" is set up and never paid off. {ending}.",
                    "why": "A reader remembers what the book told them to watch, and will wait for it.",
                    "fix": f"Bring \"{name}\" back and settle it before the end, or cut the setup.",
                    "where": spot, "terms": [name, setups], "thread": name})
    return out


_DEATH_AFTER = r"(?:\s+\w+){0,4}?\s+(?:died|was dead|is dead|lay dead|was killed|was buried|had died|drowned|was drowned|was shot dead|breathed his last|breathed her last)\b"
_DEATH_BEFORE = r"\b(?:funeral|grave|body|corpse|death|coffin)\s+of\s+(?:\w+\s+)?"
_DEATH_POSS = r"['’]s\s+(?:funeral|grave|corpse|coffin|death)\b"
_NOT_REAL = re.compile(r"\b(?:if|as though|thought|believed|almost|nearly|not|never|rumou?r\w*|feared|might|would have|dreamed|dreamt)\b", re.I)
_SPEAK = r"(?:said|says|asked|replied|answered|whispered|shouted|called|muttered|laughed|smiled|nodded|turned|walked|took|shook|told)"


def _deaths(book: Book) -> dict[str, tuple[int, str]]:
    """Who the text says is dead, and the first place it says so."""
    dead: dict[str, tuple[int, str]] = {}
    for p in people(book):
        names = _names_re(p)
        pats = [rf"\b{names}\b{_DEATH_AFTER}", rf"{_DEATH_BEFORE}{names}\b", rf"\b{names}{_DEATH_POSS}"]
        for s in book.sections:
            for pat in pats:
                for m in re.finditer(pat, s.story):
                    q = quote_at(s.story, m.start(), m.end())
                    if not _NOT_REAL.search(q) and (p["name"] not in dead or s.n < dead[p["name"]][0]):
                        dead[p["name"]] = (s.n, q)
    return dead


_SAYS = "said|says|asked|replied|answered|whispered|shouted|called|cried|muttered|snapped|told"
_ACTS = ("walked|stood|turned|took|came|went|looked|smiled|nodded|laughed|ran|sat|opened|shook|raised|reached|stepped|"
         "entered|left|pointed|grabbed|lifted|climbed|knelt|leaned|waved|stopped|moved")
# not him, now: a document, a memory, what was said once, what someone else says of him
_PAPERS = re.compile(r"\b(?:letters?|diary|diaries|journals?|notes?|papers?|manuscripts?|memoirs?|wrote|written|writes|"
                     r"records?|recorded|accounts?|testimony|statements?|footnote|clipping|cutting|transcript)\b", re.I)
_MEMORY = re.compile(r"\b(?:remember\w*|recall\w*|recollect\w*|used to|once|years ago|long ago|in those days|had|would|"
                     r"dream\w*|ghost|spirit|as if|as though|imagined|pictured|thought of)\b", re.I)
_QUOTED_SPAN = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")


def unquoted(text: str) -> str:
    """The text with what anyone says blanked, same length: the narration alone."""
    return _QUOTED_SPAN.sub(lambda m: " " * len(m.group()), text)


def _paragraph_of(text: str, i: int) -> str:
    a = text.rfind("\n\n", 0, i) + 1
    b = text.find("\n\n", i)
    return text[a:b if b >= 0 else len(text)]


def _sentence_of(text: str, i: int) -> str:
    return next((text[a:b] for a, b in sentences(text) if a <= i < b), "")


def returns(p: dict, s: Section) -> tuple[int, int] | None:
    """Where a person really speaks or acts in the present of the story: their own line of dialogue with its tag, or a
    sentence where they are the one doing something. Never a mention, a possessive ("Hale's notes", "what Hale called"),
    the past perfect ("Hale had said"), reported speech ("said that"), a letter, diary or other document, or a memory."""
    names = _names_re(p)
    plain_ = unquoted(s.story)
    speech = re.compile(rf"(?:\"[^\"\n]{{2,}}\"|“[^”\n]{{2,}}”)\s*,?\s*(?:{names}\s+(?:{_SAYS})\b(?!\s+that)|(?:{_SAYS})\s+{names}\b)"
                        rf"|\b{names}\s+(?:{_SAYS})\s*[,:]\s*[\"“]")
    acting = re.compile(rf"(?:^|(?<=[.!?]\s)|(?<=[.!?][\"”]\s)|(?<=\n))\s*{names}\s+(?:{_ACTS})\b")
    for pat, where_ in ((speech, s.story), (acting, plain_)):
        for m in pat.finditer(where_):
            if pat is speech and plain_[m.start():m.end()].strip(" ,") == "":
                continue                                   # inside someone else's line
            at = m.start() + len(m.group()) - len(m.group().lstrip())    # where the words begin, past any line break
            para = unquoted(_paragraph_of(s.story, at))
            sent = unquoted(_sentence_of(s.story, at))
            if _PAPERS.search(para) or _MEMORY.search(sent):
                continue
            return at, m.end()
    return None


@finder
def dead_speaking(book: Book) -> list[dict]:
    """Someone the text says is dead, then really speaking or acting in a later section, in the story's present."""
    out = []
    dead = _deaths(book)
    earlier = {e.get("section") for e in book.list_of("events") if e.get("placement") == "earlier"}
    for p in people(book):
        if p["name"] not in dead:
            continue
        n, q = dead[p["name"]]
        for s in book.sections:
            if s.n <= n or s.n in earlier:
                continue
            got = returns(p, s)
            if got:
                acted = quote_at(s.story, *got)
                out.append({"key": f"dead:{p['id']}", "judge": True, "size": "medium",
                            "what": f"{p['name']} dies in section {n}, then speaks or acts in section {s.n}.",
                            "why": "A reader stops believing the book the moment a dead character walks back in.",
                            "fix": f"Move that moment before section {n}, mark it as a memory, or give it to someone else.",
                            "where": [{"section": n, "quote": q}, {"section": s.n, "quote": acted}],
                            "terms": [p["name"], "dead", "died"]})
                break
    return out


@finder
def two_places(book: Book) -> list[dict]:
    """The bible's events: one person at two places at one stated time."""
    seen: dict[tuple[str, str], dict] = {}
    out = []
    for e in book.list_of("events"):
        when = " ".join(str(e.get("when") or "").lower().split())
        place = str(e.get("where") or "").strip()
        if not when or not place or not re.search(r"\d|morning|night|evening|noon|dawn|dusk|day", when):
            continue
        for who in e.get("who") or []:
            old = seen.setdefault((str(who), when), e)
            other = str(old.get("where") or "").strip()
            if other and other.lower() != place.lower() and old.get("section") != e.get("section"):
                name = next((p["name"] for p in people(book) if p["id"] == who), str(who))
                out.append({"key": f"two-places:{who}:{when}", "judge": True, "size": "medium",
                            "what": f"{name} is in {other} (section {old.get('section')}) and in {place} (section "
                                    f"{e.get('section')}), both at \"{when}\".",
                            "why": "A careful reader keeps track of who is where.",
                            "fix": "Change one of the times or places, or show how they got there.",
                            "where": where(book, old.get("section"), old.get("what")) +
                            where(book, e.get("section"), e.get("what")), "terms": [name, other, place]})
    return out


MONTHS = {m: i for i, m in enumerate(("january february march april may june july august september october november "
                                      "december").split(), 1)}
_DATE = re.compile(r"\b(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
                   r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                   r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b")


def month_days(text: str) -> list[str]:
    """Every "March 20" in some words."""
    return [f"{m.group(2)} {m.group(3)}" for m in _DATE.finditer(text)]


DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()


PARAGRAPH_FIRST = re.compile(r"\s*([^\n]+)")       # a chapter that opens with its date, as a diary or a log does


def _dates(book: Book) -> list[dict]:
    out = []
    year = None
    for s in book.sections:
        for m in _DATE.finditer(s.story):
            y = int(m.group(4)) if m.group(4) else year
            if m.group(4):
                year = y
            try:
                d = date(y or 2000, MONTHS[m.group(2).lower()], int(m.group(3)))
            except ValueError:
                continue
            first = PARAGRAPH_FIRST.match(s.story)
            dated = bool(_DATE.search(s.heading)) or bool(first and first.start(1) <= m.start() < first.end(1) and len(first.group(1).split()) <= 10)
            out.append({"section": s.n, "date": d, "has_year": y is not None, "weekday": m.group(1), "stated_year": m.group(4), "dated": dated,
                        "quote": quote_at(s.story, m.start(), m.end()), "shown": m.group().strip()})
    return out


@finder
def dates_backward(book: Book) -> list[dict]:
    """A stated date earlier than the one before it, in a section the bible does not mark as told out of order."""
    earlier = {e.get("section") for e in book.list_of("events") if e.get("placement") == "earlier"}
    out = []
    prev = None
    for d in _dates(book):
        if not d["has_year"]:
            continue
        if d["dated"] or (prev and prev["dated"]):    # chapters headed by their dates are in the order the book chose
            prev = d
            continue
        if prev and d["date"] < prev["date"] and d["section"] > prev["section"] and d["section"] not in earlier:
            out.append({"key": f"date:{d['section']}:{d['shown']}", "judge": True, "size": "medium",
                        "what": f"The dates run backward: {prev['shown']} in section {prev['section']}, then {d['shown']} "
                                f"in section {d['section']}, with nothing saying we have gone back.",
                        "why": "A reader who notices the calendar loses track of what happened first.",
                        "fix": "Correct one of the dates, or make the jump back clear to the reader.",
                        "where": [{"section": prev["section"], "quote": prev["quote"]},
                                  {"section": d["section"], "quote": d["quote"]}], "terms": [d["shown"], prev["shown"]]})
        prev = d
    return out


@finder
def wrong_weekday(book: Book) -> list[dict]:
    """A weekday with a full date that does not fall on it (the calendar is plain arithmetic)."""
    out = []
    for d in _dates(book):
        if d["weekday"] and d["stated_year"] and int(d["stated_year"]) >= 1583:
            real = DAYS[d["date"].weekday()]
            if real != d["weekday"]:
                out.append({"key": f"weekday:{d['section']}:{d['shown']}", "size": "small",
                            "what": f"{d['shown']} was a {real}, not a {d['weekday']}, by the ordinary calendar.",
                            "why": "Some readers do look it up. In an invented world this may be fine.",
                            "fix": f"Change the day to {real}, or the date.",
                            "where": [{"section": d["section"], "quote": d["quote"]}]})
    return out


# -- continuity -----------------------------------------------------------------------------------------------------

_COLOURS = ("grey gray blue green brown hazel amber black dark pale red blond blonde auburn golden white silver violet "
            "chestnut").split()
_CLR = r"(" + "|".join(_COLOURS) + r")"
_FEATURES = {"eyes": "eyes", "eye": "eyes", "hair": "hair"}
_SAMECOL = {"gray": "grey", "blonde": "blond"}


def _looks(book: Book) -> dict[tuple[str, str], dict[str, tuple[int, str]]]:
    """(person, feature) -> colour -> first (section, quote), from the text with the bible's looks on top."""
    table: dict[tuple[str, str], dict[str, tuple[int, str]]] = defaultdict(dict)
    folks = people(book)

    def note(person: str, feature: str, colour: str, n: int, q: str) -> None:
        table[(person, feature)].setdefault(_SAMECOL.get(colour, colour), (n, q))

    for s in book.sections:
        for p in folks:
            names = _names_re(p)
            for m in re.finditer(rf"\b{names}['’]s\s+(?:\w+\s+)?{_CLR}\s+(eyes|eye|hair)\b", s.story, re.I):
                note(p["name"], _FEATURES[m.group(2).lower()], m.group(1).lower(), s.n, quote_at(s.story, m.start(), m.end()))
            for m in re.finditer(rf"\b{names}['’]s\s+(eyes|hair)\s+(?:were|was)\s+(?:\w+\s+)?{_CLR}\b", s.story, re.I):
                note(p["name"], _FEATURES[m.group(1).lower()], m.group(2).lower(), s.n, quote_at(s.story, m.start(), m.end()))
        # "her grey eyes": hers is the one person named in this sentence, or else in the one before
        spans = list(sentences(s.story))
        for m in re.finditer(rf"\b(?:his|her|with|had)\s+(?:\w+\s+)?{_CLR}\s+(eyes|eye|hair)\b", s.story, re.I):
            at = next((k for k, (a, b) in enumerate(spans) if a <= m.start() < b), None)
            if at is None:
                continue
            for near in (s.story[spans[at][0]:m.start()], s.story[spans[at - 1][0]:spans[at - 1][1]] if at else ""):
                named = [p for p in folks if re.search(rf"\b{_names_re(p)}\b", near)]
                if len(named) == 1:
                    note(named[0]["name"], _FEATURES[m.group(2).lower()], m.group(1).lower(), s.n, quote_at(s.story, m.start(), m.end()))
                if named:
                    break
    for p in folks:
        for x in p["c"].get("looks") or []:
            if not isinstance(x, dict):
                continue
            m = re.search(rf"\b{_CLR}\s+(eyes|eye|hair)\b", str(x.get("detail") or ""), re.I)
            s = book.section(x.get("section"))
            if m and s:
                q = str(x.get("quote") or "")
                if q and locate(s.story, q):
                    note(p["name"], _FEATURES[m.group(2).lower()], m.group(1).lower(), s.n, q)
    return table


@finder
def looks_changed(book: Book) -> list[dict]:
    """A stated eye or hair colour that changes."""
    out = []
    for (person, feature), colours in _looks(book).items():
        if len(colours) < 2:
            continue
        spots = sorted(colours.items(), key=lambda kv: kv[1][0])
        said = " and ".join(f"{c} in section {v[0]}" for c, v in spots)
        out.append({"key": f"looks:{person.lower()}:{feature}", "size": "medium",
                    "what": f"{person}'s {feature} change: {said}.",
                    "why": "It is small, but readers picture a character and notice when the picture changes.",
                    "fix": f"Choose one colour for {person}'s {feature} and make the others match, unless the change is the point.",
                    "where": [{"section": v[0], "quote": v[1]} for _c, v in spots[:3]]})
    return out


_NUMS = {w: i for i, w in enumerate(("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                     "fifteen sixteen seventeen eighteen nineteen twenty").split())}
_NUMS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90})
_AGE = r"(\d{1,3}|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:-(?:one|two|three|four|five|six|seven|eight|nine))?|" \
       r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"


def _age(s: str) -> int | None:
    s = s.lower()
    if s.isdigit():
        return int(s)
    if "-" in s:
        a, b = s.split("-", 1)
        return _NUMS.get(a, 0) + _NUMS.get(b, 0)
    return _NUMS.get(s)


@finder
def ages_changed(book: Book) -> list[dict]:
    """A stated age that does not fit: younger later on, or a gap the book's calendar might not explain."""
    out = []
    for p in people(book):
        names = _names_re(p)
        seen: list[tuple[int, int, str]] = []
        pats = [rf"\b{names},\s+{_AGE}(?:\s+years\s+old)?,", rf"\b{names}\s+was\s+{_AGE}(?:\s+years\s+old|\s+that\b)",
                rf"\b{_AGE}-year-old\s+{names}\b"]
        for s in book.sections:
            for pat in pats:
                for m in re.finditer(pat, s.story):
                    a = _age(m.group(1))
                    if a is not None and (not seen or seen[-1][1] != a):
                        seen.append((s.n, a, quote_at(s.story, m.start(), m.end())))
        for a, b in zip(seen, seen[1:]):
            if b[1] != a[1]:
                out.append({"key": f"age:{p['id']}:{a[1]}:{b[1]}", "judge": True, "size": "medium",
                            "what": f"{p['name']} is {a[1]} in section {a[0]} and {b[1]} in section {b[0]}.",
                            "why": "A change of age has to fit the time that passes between.",
                            "fix": "Check how much time passes here and make the ages agree.",
                            "where": [{"section": a[0], "quote": a[2]}, {"section": b[0], "quote": b[2]}], "terms": [p["name"]]})
    return out


def _edit1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


def _caps(book: Book) -> Counter:
    """Capitalised words inside a sentence: names, mostly."""
    got: Counter = Counter()
    for s in book.sections:
        for m in re.finditer(r"(?<=[a-z,;] )([A-Z][a-zà-ɏ]{3,})\b", s.story):
            got[m.group(1)] += 1
    return got


@finder
def names_spelled(book: Book) -> list[dict]:
    """A name written two ways, one of them rare: a letter out of a character's name."""
    out = []
    caps = Counter(m.group() for s in book.sections for m in re.finditer(r"\b[A-Z][a-z\u00e0-\u024f]{3,}\b", s.story))
    known = {n for p in people(book) for n in p["names"] if " " not in n}
    for name in known:
        for other, count in caps.items():
            if other != name and other not in known and _edit1(name, other) and count <= max(2, caps[name] // 4):
                sec = next((s for s in book.sections if re.search(rf"\b{other}\b", s.story)), None)
                if sec:
                    m = re.search(rf"\b{other}\b", sec.story)
                    out.append({"key": f"name:{other}", "size": "medium",
                                "what": f"\"{other}\" appears {count} time{'s' if count > 1 else ''}; everywhere else it is \"{name}\".",
                                "why": "A misspelled name looks like a new character.",
                                "fix": f"Make it \"{name}\", unless this is someone else.",
                                "where": [{"section": sec.n, "quote": quote_at(sec.story, m.start(), m.end())}]})
    return out


@finder
def bible_continuity(book: Book) -> list[dict]:
    """The notes Manuscript's reading made: mismatches and gaps, to be weighed against the text."""
    out = []
    for c in book.list_of("continuity"):
        note = str(c.get("note") or "").strip()
        secs = ints(c.get("sections"))
        if note and c.get("kind") in ("mismatch", "gap") and secs:
            spot = [w for n in secs[:3] for w in where(book, n, note)]
            out.append({"key": "note:" + " ".join(sorted(words_of(note)))[:80], "judge": True, "size": "medium", "what": note,
                        "why": "It came up while the book was being read, so a reader may catch it too.",
                        "fix": "Check both places and make them agree.", "where": spot, "terms": [note]})
    return out


@finder
def travel_times(book: Book) -> list[dict]:
    """The bible's distances (0.3.3 on): the same two places said to be different distances apart."""
    seen: dict[tuple[str, str], dict] = {}
    out = []
    for g in book.list_of("geography"):
        a, b = str(g.get("from") or "").lower().strip(), str(g.get("to") or "").lower().strip()
        note = str(g.get("note") or "")
        if not a or not b or not re.search(r"\d|day|hour|week|mile", note):
            continue
        key = tuple(sorted((a, b)))
        old = seen.setdefault(key, g)
        if old is not g and str(old.get("note")).lower() != note.lower():
            out.append({"key": f"travel:{key[0]}:{key[1]}", "judge": True, "size": "medium",
                        "what": f"From {g.get('from')} to {g.get('to')}: \"{old.get('note')}\" in section {old.get('section')}, "
                                f"\"{note}\" in section {g.get('section')}.",
                        "why": "Journeys that change length pull a careful reader out of the world.",
                        "fix": "Settle one distance; note what changes it if it is meant to change (a storm, a road).",
                        "where": where(book, old.get("section"), old.get("quote") or old.get("note")) +
                        where(book, g.get("section"), g.get("quote") or note), "terms": [a, b]})
    return out


@finder
def world_numbers(book: Book) -> list[dict]:
    """A world rule with a number, and the same words with another number in the text."""
    out = []
    for r in book.list_of("world_rules"):
        rule = str(r.get("rule") or "")
        for m in re.finditer(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|twelve)[- ](hour|day|week|month|year|mile)s?\b", rule, re.I):
            unit = m.group(2).lower()
            nouns = words_of(rule) - {unit, m.group(1).lower(), unit + "s"}
            for s in book.sections:
                for x in re.finditer(rf"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|twelve)[- ]{unit}s?\b", s.story, re.I):
                    if x.group(1).lower() != m.group(1).lower():
                        q = quote_at(s.story, x.start(), x.end())
                        if nouns & words_of(q):
                            out.append({"key": f"rule:{rule[:40].lower()}:{s.n}", "judge": True, "size": "medium",
                                        "what": f"The book's rule is \"{rule}\", but section {s.n} says {x.group().strip()}.",
                                        "why": "A rule the book sets and then breaks reads as a mistake.",
                                        "fix": "Make them agree, or show why this case differs.",
                                        "where": [{"section": s.n, "quote": q}], "terms": [rule]})
                            break
    return out


# -- structure ------------------------------------------------------------------------------------------------------

def _chapters(book: Book) -> list[dict]:
    """Parts of one chapter put back together: lengths are for what the writer calls a chapter."""
    out: list[dict] = []
    for s in book.sections:
        base = _base_heading(s.heading)
        if out and out[-1]["heading"] == base:
            out[-1]["words"] += s.words
            out[-1]["last"] = s.n
        else:
            out.append({"heading": base, "first": s.n, "last": s.n, "words": s.words})
    return out


@finder
def chapter_lengths(book: Book) -> list[dict]:
    """Chapters far longer or shorter than the book's usual. Counted, never guessed."""
    ch = [c for c in _chapters(book) if c["words"] > 0]
    if len(ch) < 4:
        return []
    mid = statistics.median(c["words"] for c in ch)
    out = []
    for c in ch:
        if c["words"] >= mid * 2.2 or c["words"] <= mid * 0.33:
            long = c["words"] > mid
            out.append({"key": f"length:{c['heading'].lower()}", "size": "small",
                        "what": f"{c['heading']} is {c['words']:,} words; the book's usual chapter is about {int(mid):,}.",
                        "why": "A long chapter can feel like it stalls." if long else "A very short chapter lands hard, so it should earn that.",
                        "fix": "See whether it wants splitting, if the length is not on purpose." if long else "Check that its weight fits what happens in it.",
                        "where": where(book, c["first"], c["heading"]) or []})
    return out


@finder
def sagging(book: Book) -> list[dict]:
    """A stretch of three or more sections where the bible records no event and no thread moves."""
    events = {e.get("section") for e in book.list_of("events")}
    threads = {n for t in book.list_of("threads") for n in ints(t.get("sections"))}
    if not events and not threads:
        return []
    out: list[dict] = []
    run_: list = []
    for s in book.sections + [None]:
        if s is not None and s.n not in events and s.n not in threads:
            run_.append(s)
            continue
        if len(run_) >= 3:
            out.append({"key": f"sag:{run_[0].n}", "judge": True, "size": "big",
                        "what": f"Sections {run_[0].n} to {run_[-1].n}: nothing the reading noted happens, and no thread moves.",
                        "why": "This is where a reader may put the book down.",
                        "fix": "Give this stretch something that changes, shorten it, or join it to what follows.",
                        "where": where(book, run_[0].n, run_[0].heading) + where(book, run_[-1].n, run_[-1].heading),
                        "terms": [x.heading for x in run_]})
        run_ = []
    return out


@finder
def shape(book: Book) -> list[dict]:
    """Where the book's turns fall, from the bible's structure, by share of the words."""
    st = book.bible.get("structure")
    total = sum(s.words for s in book.sections)
    if not isinstance(st, dict) or not total or len(book.sections) < 4:
        return []
    out = []
    before = {}
    used = 0
    for s in book.sections:
        before[s.n] = used / total
        used += s.words
    for key, lo, hi, line in (("midpoint", 0.3, 0.7, "The midpoint"), ("climax", 0.7, 1.01, "The climax")):
        v = st.get(key)
        n = ints(v.get("section"))[0] if isinstance(v, dict) and ints(v.get("section")) else None
        if n in before and not lo <= before[n] <= hi:
            out.append({"key": f"shape:{key}", "judge": True, "size": "medium",
                        "what": f"{line} comes {int(before[n] * 100)}% of the way through the book (section {n}).",
                        "why": "Readers feel where a book's weight falls.",
                        "fix": "See whether it should move, or whether the book's shape is really something else.",
                        "where": where(book, n, v.get("note")), "terms": [str(v.get("note") or "")]})
    return out


@finder
def empty_scenes(book: Book) -> list[dict]:
    """Scenes the reading could find no purpose for."""
    out = []
    for s in book.list_of("scenes"):
        if not str(s.get("purpose") or "").strip() and int(s.get("words") or 0) > 300:
            n = s.get("section")
            spot = where(book, n, "")
            if spot:
                out.append({"key": f"scene:{n}:{s.get('n')}", "judge": True, "size": "medium",
                            "what": f"Scene {s.get('n')} of section {n} has no clear purpose the reading could name.",
                            "why": "A scene that changes nothing slows the book.",
                            "fix": "Name what it changes, or fold what it needs into another scene.", "where": spot, "terms": []})
    return out


# -- characters -----------------------------------------------------------------------------------------------------

@finder
def lead_absent(book: Book) -> list[dict]:
    """A main character missing for a long stretch, or missing from the climax."""
    out = []
    total = len(book.sections)
    if total < 5:
        return out
    st = book.bible.get("structure") if isinstance(book.bible.get("structure"), dict) else {}
    climax = ints((st.get("climax") or {}).get("section")) if isinstance(st.get("climax"), dict) else []
    for c in _main(book):
        seen = ints(c.get("appearances"))
        name = str(c.get("name") or "")
        if not seen or not name:
            continue
        nums = [s.n for s in book.sections]
        gaps = [(a, b) for a, b in zip(seen, seen[1:]) if len([n for n in nums if a < n < b]) >= max(3, total // 4)]
        for a, b in gaps[:1]:
            out.append({"key": f"absent:{c.get('id')}", "judge": True, "size": "medium",
                        "what": f"{name} is gone from section {a} to section {b}.",
                        "why": "Readers follow a lead; a long absence loosens the hold.",
                        "fix": "Keep them in view: a letter, someone thinking of them, a short scene.",
                        "where": where(book, a, name) + where(book, b, name), "terms": [name]})
        if climax and climax[0] not in seen and str(c.get("role") or "").lower() == "protagonist":
            out.append({"key": f"climax:{c.get('id')}", "judge": True, "size": "big",
                        "what": f"{name}, the lead, is not in the climax (section {climax[0]}).",
                        "why": "A lead who isn't there when it matters doesn't drive the story.",
                        "fix": f"Find a way for {name}'s choice to decide the climax.",
                        "where": where(book, climax[0], (st.get("climax") or {}).get("note")), "terms": [name]})
    return out


@finder
def could_merge(book: Book) -> list[dict]:
    """Minor characters in one section each, with the same role: a publisher's editor would ask if both are needed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in book.list_of("characters"):
        if c.get("importance") == "minor" and len(ints(c.get("appearances"))) <= 1 and str(c.get("role") or "").strip():
            groups[str(c["role"]).lower()].append(c)
    out = []
    for role, cs in groups.items():
        if len(cs) >= 2:
            names = ", ".join(str(c.get("name")) for c in cs[:4])
            spot = [w for c in cs[:2] for n in ints(c.get("appearances"))[:1] for w in where(book, n, c.get("name"))]
            out.append({"key": f"merge:{role}", "size": "small", "what": f"{names} all play \"{role}\" in one scene each.",
                        "why": "Fewer names are easier to hold on to.",
                        "fix": "See whether one person could do all of this.", "where": spot})
    return out


# -- point of view --------------------------------------------------------------------------------------------------

@finder
def tense_and_person(book: Book) -> list[dict]:
    """Slips the reading noted, and sections told in another person from the rest of the book (outside dialogue)."""
    out = []
    voice = book.bible.get("voice") if isinstance(book.bible.get("voice"), dict) else {}
    for v in voice.get("by_section") or []:
        if isinstance(v, dict):
            for slip in [x for x in v.get("slips") or [] if str(x).strip()][:2]:
                spot = where(book, v.get("section"), slip)
                if spot:
                    out.append({"key": f"slip:{v.get('section')}:{str(slip)[:40].lower()}", "judge": True, "size": "medium",
                                "what": f"Section {v.get('section')}: {slip}", "why": "A slip in who is telling, or when, trips the reader.",
                                "fix": "Keep it to the section's point of view and tense, unless the change is the point.",
                                "where": spot, "terms": [str(slip)]})
    firsts = {}
    for s in book.sections:
        body = _QUOTED.sub(" ", s.story)
        words = max(1, len(body.split()))
        firsts[s.n] = len(re.findall(r"\b(?:I|me|my)\b", body)) / words
    if len(firsts) >= 3:
        mid = statistics.median(firsts.values())
        for n, share in firsts.items():
            if (mid < 0.003 and share > 0.02) or (mid > 0.02 and share < 0.002):
                out.append({"key": f"person:{n}", "judge": True, "size": "medium",
                            "what": f"Section {n} seems told in a different person from the rest of the book.",
                            "why": "A change of narrator without a signal confuses a reader.",
                            "fix": "Make the change clear, or bring it into line.", "where": where(book, n, "I me my"), "terms": ["I", "my"]})
    return out


# -- voice, dialogue, habits ------------------------------------------------------------------------------------------

_QUOTED = re.compile(r"\"[^\"\n]{1,2000}\"|“[^”\n]{1,2000}”")


def measures(s: Section) -> dict:
    sents = [s.story[a:b] for a, b in sentences(s.story)]
    words = max(1, s.words)
    said = sum(len(q.split()) for q in _QUOTED.findall(s.story))
    return {"sentence": sum(len(x.split()) for x in sents) / max(1, len(sents)), "dialogue": said / words,
            "adverbs": len(re.findall(r"\b\w{3,}ly\b", s.story)) / words * 1000}


@finder
def voice_drift(book: Book) -> list[dict]:
    """A section that measures far from the book's own: sentence length, share of dialogue, adverbs."""
    ms = {s.n: measures(s) for s in book.sections if s.words >= 80}
    if len(ms) < 5:
        return []
    out = []
    words = {"sentence": "sentences", "adverbs": "adverbs"}
    for key, shown in words.items():
        vals = [m[key] for m in ms.values()]
        mid, dev = statistics.median(vals), statistics.pstdev(vals)
        if dev <= 0:
            continue
        for n, m in ms.items():
            if abs(m[key] - mid) > 2.2 * dev and abs(m[key] - mid) > 0.3 * mid:
                more = "longer" if key == "sentence" and m[key] > mid else "shorter" if key == "sentence" else "more" if m[key] > mid else "fewer"
                line = f"Section {n} has much {more} {shown} than the rest of the book" + \
                    (f" (about {m[key]:.0f} words a sentence; the book's usual is {mid:.0f})." if key == "sentence" else f" ({m[key]:.0f} in each thousand words; the book's usual is {mid:.0f}).")
                out.append({"key": f"drift:{n}:{key}", "judge": True, "size": "small", "what": line,
                            "why": "A change like this reads as a change of voice, unless it is on purpose.",
                            "fix": "Read it beside the section before it, and decide if the change is meant.",
                            "where": where(book, n, book.section(n).heading), "terms": []})
    return out


_TAGS = re.compile(r"[\"”]\s*(?:\w+\s+)?(said|asked|replied|answered|whispered|shouted|called|muttered|exclaimed|snapped|hissed|growled|breathed|intoned|opined|retorted|gasped|sighed|laughed)\b(?:\s+\w+)?(\s+\w+ly\b)?", re.I)


@finder
def dialogue_tags(book: Book) -> list[dict]:
    """Fancy tags and adverbs on tags, counted across the book."""
    fancy: Counter = Counter()
    adverb, first = 0, None
    plain_ = {"said", "asked", "replied", "answered"}
    total = 0
    for s in book.sections:
        for m in _TAGS.finditer(s.story):
            total += 1
            if m.group(1).lower() not in plain_:
                fancy[m.group(1).lower()] += 1
                first = first or (s.n, quote_at(s.story, m.start(), m.end()))
            if m.group(2):
                adverb += 1
                first = first or (s.n, quote_at(s.story, m.start(), m.end()))
    if not first or total < 15 or (sum(fancy.values()) + adverb) / total < 0.3:
        return []
    top = ", ".join(f"{w} ({c})" for w, c in fancy.most_common(4))
    return [{"key": "tags", "size": "small",
             "what": f"Of {total} speech tags, {sum(fancy.values())} are not plain ones ({top}) and {adverb} have an adverb.",
             "why": "\"Said\" disappears on the page; fancier tags pull the eye to themselves.",
             "fix": "Let most lines end in said, or nothing, and show the feeling in the line.",
             "where": [{"section": first[0], "quote": first[1]}]}]


FILLER = ("just really very quite rather somewhat suddenly actually basically literally seemed began started "
          "almost nearly simply").split()
TELLING = r"\b(?:felt|knew|realized|realised|noticed|wondered|thought|saw that|could see|was angry|was sad|was afraid|was happy)\b"


@finder
def prose_habits(book: Book) -> list[dict]:
    """Counted: the filler she leans on, words used far more than a book needs, and telling words."""
    total = sum(s.words for s in book.sections)
    if total < 200:
        return []
    allw: Counter = Counter()
    for s in book.sections:
        allw.update(w.lower() for w in _WORD.findall(s.story))
    out = []
    names = {w.lower() for p in people(book) for n in p["names"] for w in n.split()}

    def first_of(word: str) -> list[dict]:
        for s in book.sections:
            m = re.search(rf"\b{re.escape(word)}\b", s.story, re.I)
            if m:
                return [{"section": s.n, "quote": quote_at(s.story, m.start(), m.end())}]
        return []

    for w in FILLER:
        rate = allw[w] / total * 1000
        if allw[w] >= 8 and rate >= 2.5:
            out.append({"key": f"filler:{w}", "size": "small",
                        "what": f"\"{w}\" comes {allw[w]} times, about {rate:.1f} in each thousand words.",
                        "why": "A habit a reader starts to hear.", "fix": f"Search for \"{w}\" and keep only those that earn it.",
                        "where": first_of(w)})
    common = [(w, c) for w, c in allw.most_common(400)
              if len(w) >= 5 and w not in STOP and w not in FILLER and w not in names and c / total * 1000 >= 2.5 and c >= 8]
    for w, c in common[:4]:
        out.append({"key": f"tic:{w}", "size": "small", "what": f"\"{w}\" comes {c} times. It may be a word you lean on.",
                    "why": "A repeated word takes the shine off itself.", "fix": "Vary it where it isn't doing a job.", "where": first_of(w)})
    tell = [m for s in book.sections for m in re.finditer(TELLING, s.story)]
    if len(tell) / total * 1000 >= 6:
        s0 = next((s for s in book.sections if re.search(TELLING, s.story)), None)
        m = re.search(TELLING, s0.story)
        out.append({"key": "telling", "size": "small",
                    "what": f"{len(tell)} telling words (felt, knew, noticed and the like), about {len(tell) / total * 1000:.0f} in each thousand.",
                    "why": "The reader gets the feeling reported, not the feeling.", "fix": "In the moments that matter, show what she does instead.",
                    "where": [{"section": s0.n, "quote": quote_at(s0.story, m.start(), m.end())}]})
    lengths = [len(s.story[a:b].split()) for s in book.sections for a, b in sentences(s.story)]
    if len(lengths) > 40 and statistics.pstdev(lengths) < 4:
        out.append({"key": "rhythm", "size": "small",
                    "what": f"Sentences hardly vary: about {statistics.mean(lengths):.0f} words, give or take {statistics.pstdev(lengths):.0f}.",
                    "why": "An even beat dulls the ear.", "fix": "Break the beat where it counts: one short line after long ones.",
                    "where": first_of(_WORD.findall(book.sections[0].story)[0]) if _WORD.findall(book.sections[0].story) else []})
    return out


@finder
def dialogue_only_info(book: Book) -> list[dict]:
    """A very long unbroken speech: often a character telling the reader what the writer wants them to know."""
    out = []
    for s in book.sections:
        for m in _QUOTED.finditer(s.story):
            if len(m.group().split()) >= 90:
                out.append({"key": f"speech:{s.n}:{m.start() // 200}", "judge": True, "size": "small",
                            "what": f"A speech of {len(m.group().split())} words in section {s.n}, with nothing breaking it up.",
                            "why": "Long talk can read as the book explaining itself.",
                            "fix": "Break it with what the others do, or cut what the listener already knows.",
                            "where": [{"section": s.n, "quote": m.group()[:QUOTE_MAX].strip()}], "terms": []})
    return out[:6]


# -- proofreading ---------------------------------------------------------------------------------------------------

@finder
def proof_marks(book: Book) -> list[dict]:
    """Doubled words, spaces before punctuation, stray quote marks in a paragraph, double spaces. Each has a correction
    where it is exact."""
    out = []

    def add(s: Section, kind: str, what: str, fix: str, i: int, j: int, quote: str | None = None, correct: str | None = None) -> None:
        q = quote if quote is not None else quote_at(s.story, i, j)
        c = {"key": f"proof:{kind}:{s.n}:{q[:50].lower()}", "size": "small", "what": what, "why": "A proofreader would mark it.", "fix": fix,
             "where": [{"section": s.n, "quote": q}]}
        if correct is not None:
            c["correct"] = correct
        out.append(c)

    for s in book.sections:
        for m in re.finditer(r"\b(\w{2,})\s+\1\b", s.story, re.I):
            if m.group(1).lower() in ("had", "that", "very", "bye", "knock", "tut", "ha", "no", "so", "yes", "now", "there", "well", "oh"):
                continue
            q = quote_at(s.story, m.start(), m.end())
            pos = m.start() - s.story.index(q) if q in s.story else -1
            fixed = (q[:pos] + m.group(1) + q[pos + len(m.group()):]) if pos >= 0 else None
            add(s, "double", f"A doubled word: \"{m.group()}\".", f"Take one \"{m.group(1)}\" out.", m.start(), m.end(), q, fixed)
        for m in re.finditer(r"\w( +)([,;:.!?])(?![.\w])", s.story):
            q = quote_at(s.story, m.start(), m.end())
            add(s, "space", "A space before punctuation.", "Close it up.", m.start(), m.end(), q, q.replace(m.group(1) + m.group(2), m.group(2), 1))
        for m in re.finditer(r"[^\n ]( {2,})[^\n ]", s.story):
            q = quote_at(s.story, m.start(), m.end())
            add(s, "spaces", "Two spaces where one belongs.", "Make it one.", m.start(), m.end(), q, re.sub(r"  +", " ", q))
        for para in re.finditer(r"[^\n]+", s.story):
            p = para.group()
            straight = p.count('"')
            opens, closes = p.count("“"), p.count("”")
            if straight % 2 or opens != closes:
                # a long speech may run across paragraphs with no closing mark; the next one then opens
                next_para = s.story[para.end():].lstrip()
                if opens > closes and next_para.startswith("“"):
                    continue
                add(s, "quote", "The quotation marks in this paragraph don't balance.", "Check where the speech opens and closes.", para.start(), para.start(), quote_at(s.story, para.start()))
    return out[:200]


@finder
def scene_breaks(book: Book) -> list[dict]:
    """More than one way of marking a scene break."""
    kinds: Counter = Counter()
    first: dict[str, tuple[int, str]] = {}
    for s in book.sections:
        for m in re.finditer(r"^\s*(\*\s*\*\s*\*|-{3,}|#|~)\s*$", s.story, re.M):
            k = re.sub(r"\s", "", m.group(1))
            kinds[k] += 1
            first.setdefault(k, (s.n, m.group(1)))
    if len(kinds) < 2:
        return []
    usual = kinds.most_common(1)[0][0]
    return [{"key": "scene-marks", "size": "small",
             "what": "Scene breaks are marked " + " and ".join(f"\"{k}\" ({c} times)" for k, c in kinds.items()) + ".",
             "why": "A publisher wants one mark throughout.", "fix": f"Use \"{usual}\" throughout.",
             "where": [{"section": v[0], "quote": v[1]} for v in first.values()][:3]}]


# -- the style sheet ----------------------------------------------------------------------------------------------------

UK_US = {"colour": "color", "honour": "honor", "harbour": "harbor", "favour": "favor", "neighbour": "neighbor", "grey": "gray",
         "realise": "realize", "recognise": "recognize", "centre": "center", "theatre": "theater", "towards": "toward",
         "travelled": "traveled", "metre": "meter", "defence": "defense", "plough": "plow", "whilst": "while"}
NUM_WORDS = ("two three four five six seven eight nine ten eleven twelve twenty thirty forty fifty hundred thousand").split()


def _sheet(book: Book) -> tuple[list[tuple[str, list[str]]], list[dict]]:
    """(headed lists for the sheet, findings for what is inconsistent)."""
    text = "\n".join(s.story for s in book.sections)
    words = Counter(w for w in _WORD.findall(text))
    low = Counter(w.lower() for w in _WORD.findall(text))
    out: list[tuple[str, list[str]]] = []
    finds: list[dict] = []

    def spot(word: str) -> list[dict]:
        for s in book.sections:
            m = re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", s.story)
            if m:
                return [{"section": s.n, "quote": quote_at(s.story, m.start(), m.end())}]
        return []

    def incon(key: str, a: str, ca: int, b: str, cb: int, what: str) -> None:
        minor = b if cb <= ca else a
        major = a if cb <= ca else b
        finds.append({"key": f"sheet:{key}", "size": "small", "what": f"{what}: \"{a}\" ({ca}) and \"{b}\" ({cb}).",
                      "why": "A copyeditor chooses one and holds to it.", "fix": (f"You mostly use \"{major}\". " if ca != cb else "") + "Decide, and it goes on the style sheet.",
                      "where": spot(minor) + spot(major)})

    # names, as used
    rows = []
    for p in people(book):
        seen = [(n, len(re.findall(rf"\b{re.escape(n)}\b", text))) for n in p["names"]]
        rows.append(f"{p['name']}: " + ", ".join(f"{n} ({c})" for n, c in seen if c))
    out.append(("Names, as you use them", rows or ["(Needs the story bible's characters. Have Manuscript read the book.)"]))
    places = [str(x.get("name")) for x in book.list_of("places") if x.get("name")]
    if places:
        out.append(("Places", [f"{n} ({len(re.findall(re.escape(n), text))})" for n in places]))

    # spellings
    rows = []
    for uk, us in UK_US.items():
        a, b = low[uk], low[us]
        if a or b:
            rows.append(f"{uk if a >= b else us}" + (f" (also {us if a >= b else uk})" if a and b else ""))
        if a and b:
            incon(f"spell:{uk}", uk, a, us, b, "Two spellings")
    out.append(("Spellings", rows or ["(Nothing to note.)"]))

    # hyphens
    rows, seen_pairs = [], set()
    hyph = Counter(m.group().lower() for m in re.finditer(r"\b[A-Za-z]{2,}-[A-Za-z]{2,}\b", text))
    for w, c in hyph.most_common(120):
        a, b = w.split("-", 1)
        open_ = len(re.findall(rf"\b{a}\s+{b}\b", text, re.I))
        closed = low[a + b]
        rows.append(w + (f" (also \"{a} {b}\")" if open_ else "") + (f" (also \"{a}{b}\")" if closed else ""))
        if (open_ or closed) and w not in seen_pairs:
            seen_pairs.add(w)
            incon(f"hyphen:{w}", w, c, f"{a} {b}" if open_ else a + b, open_ or closed, "Hyphen or not")
    out.append(("Hyphens", rows[:60] or ["(None used.)"]))

    # capitals: words she capitalises in the middle of a sentence and also does not
    rows = []
    mids = _caps(book)
    for w, c in mids.most_common(200):
        small = words[w.lower()]
        if small and c >= 2 and w.lower() not in STOP:
            rows.append(f"{w} ({c}), also {w.lower()} ({small})")
            incon(f"caps:{w.lower()}", w, c, w.lower(), small, "Capital or not")
    out.append(("Capitals", rows[:40] or ["(Nothing to note.)"]))

    # numbers
    digits = len(re.findall(r"(?<![\w-])\d+(?![\w-])", text))
    spelled = sum(low[w] for w in NUM_WORDS)
    out.append(("Numbers", [f"Spelled out: {spelled}. In figures: {digits}."]))
    if digits and spelled and min(digits, spelled) >= 3:
        finds.append({"key": "sheet:numbers", "size": "small", "what": f"Numbers are spelled out {spelled} times and in figures {digits} times.",
                      "why": "A copyeditor settles one rule; fiction usually spells them out.", "fix": "Pick a rule for the sheet.",
                      "where": spot(next((m.group() for m in re.finditer(r"(?<![\w-])\d+(?![\w-])", text)), ""))})

    # marks
    marks = {"straight double quotes": text.count('"'), "curly double quotes": text.count("“"), "em dashes": text.count("\u2014"),
             "double hyphens": text.count("--"), "three dots": text.count("..."), "ellipsis marks": text.count("…")}
    out.append(("Marks", [f"{k}: {v}" for k, v in marks.items() if v] or ["(None to note.)"]))
    if marks["straight double quotes"] and marks["curly double quotes"]:
        finds.append({"key": "sheet:quotes", "size": "small", "what": "Both straight and curly quotation marks are used.",
                      "why": "One kind throughout looks right, and a word processor can do it for you.", "fix": "Use one kind.",
                      "where": spot(next((m.group() for m in re.finditer(r'"\w+', text)), "").strip('"'))})
    return out, [f for f in finds if f["where"]]


@finder
def style_sheet_findings(book: Book) -> list[dict]:
    return _sheet(book)[1][:40]


def style_sheet(book: Book) -> str:
    """The sheet a publisher's copyeditor makes, as a document she keeps. Counted from the text she has now."""
    rows = [f"# Style sheet: {book.title}", "",
            "Made from your text as it stands, by counting. Yours to change; nothing here touches your book.", ""]
    for head, lines in _sheet(book)[0]:
        rows += [f"## {head}", ""] + [f"- {x}" for x in lines] + [""]
    return "\n".join(rows)


# -- getting it out -----------------------------------------------------------------------------------------------------

# Rough and general: what agents and publishers commonly say for a first novel, from memory, not from any one source.
# Marked rough on screen. A writer should check the houses she is sending to.
GENRES = {"literary fiction": (70_000, 100_000), "general or commercial fiction": (70_000, 100_000), "mystery": (70_000, 90_000),
          "thriller": (70_000, 100_000), "romance": (50_000, 90_000), "historical fiction": (80_000, 120_000),
          "science fiction": (90_000, 120_000), "fantasy": (90_000, 130_000), "horror": (70_000, 90_000),
          "young adult": (50_000, 80_000), "middle grade": (25_000, 50_000), "short story": (1_000, 7_500), "novella": (17_500, 40_000)}


def length_note(book: Book, genre: str = "") -> tuple[str, list[dict]]:
    total = sum(s.words for s in book.sections)
    want = " ".join(genre.lower().split())
    hit = next((g for g in GENRES if want and (want in g or g in want)), None)
    rows = [f"# Length and genre fit: {book.title}", "", f"**{total:,} words**, counted from your text as it stands.", ""]
    finds: list[dict] = []
    if hit:
        lo, hi = GENRES[hit]
        place = "within" if lo <= total <= hi else "under" if total < lo else "over"
        rows += [f"For {hit} the rough range is {lo:,} to {hi:,} words, so this is {place} it.", ""]
        if place != "within":
            first = book.sections[0] if book.sections else None
            finds.append({"key": "length:genre", "size": "medium", "what": f"At {total:,} words this is {place} the rough range for {hit} ({lo:,} to {hi:,}).",
                          "why": "Agents often go by length first, though a strong book can break the rule.", "fix": "Weigh it against the houses you mean to send to.",
                          "where": [{"section": first.n, "quote": best_sentence(first, first.heading)}] if first else []})
    rows += ["Rough ranges only, from common advice and not from any one publisher. Check the guidelines of the houses you send to.", "",
             "| Genre | Rough range, words |", "|---|---|"]
    rows += [f"| {g} | {lo:,} to {hi:,} |" for g, (lo, hi) in GENRES.items()]
    return "\n".join(rows) + "\n", finds


BRANDS = ("Coca-Cola Pepsi Google Facebook Instagram iPhone Microsoft Disney Starbucks McDonald's Nike Ford Rolex Kleenex Xerox Amazon "
          "Walmart Twitter YouTube Netflix").split()


@finder
def permissions(book: Book) -> list[dict]:
    """Flags only: brand names, and indented or verse-like lines that may be quoted from elsewhere."""
    out = []
    for b in BRANDS:
        for s in book.sections:
            m = re.search(rf"\b{re.escape(b)}\b", s.story)
            if m:
                out.append({"key": f"brand:{b.lower()}", "size": "small", "what": f"A brand name: {b}.",
                            "why": "Usually fine in passing; a publisher looks twice if the brand is shown badly.", "fix": "Your judgment. Keep it if it is neutral.",
                            "where": [{"section": s.n, "quote": quote_at(s.story, m.start(), m.end())}]})
                break
    for s in book.sections:
        lines = [m for m in re.finditer(r"^[^\n]{5,70}$", s.story, re.M)]
        run_ = []
        for m in lines + [None]:
            if m is not None and (not run_ or s.story.count("\n", run_[-1].end(), m.start()) == 1):
                run_.append(m)
                continue
            if len(run_) >= 3 and not any(_QUOTED.search(x.group()) for x in run_):
                out.append({"key": f"verse:{s.n}:{run_[0].start() // 100}", "size": "medium",
                            "what": f"These short lines in section {s.n} look like verse or lyrics.",
                            "why": "Words quoted from a song or poem can need permission, even a line or two. If they are yours, there is nothing to do.",
                            "fix": "If they are someone else's, note where they are from, for your publisher.",
                            "where": [{"section": s.n, "quote": run_[0].group().strip()}]})
            run_ = [m] if m is not None else []
    return out


def speakers(book: Book, each: int = 300) -> list[tuple[str, str]]:
    """(character, lines they speak), found by the tags beside the lines. Whatever can be attributed by code."""
    out = []
    folks = sorted(people(book), key=lambda p: (p["c"].get("importance") != "main", -len(ints(p["c"].get("appearances")))))
    for p in folks[:6]:
        names = _names_re(p)
        pat = re.compile(rf"(\"[^\"\n]+\"|“[^”\n]+”),?\s+(?:said|asked|replied|answered|whispered|shouted|muttered)?\s*{names}\b|"
                         rf"(\"[^\"\n]+\"|“[^”\n]+”),?\s+{names}\s+{_SPEAK}\b")
        got = []
        for s in book.sections:
            for m in pat.finditer(s.story):
                got.append(f"[{s.n}] {m.group(1) or m.group(2)}")
                if sum(len(x.split()) for x in got) > each:
                    break
        if got:
            out.append((p["name"], "\n".join(got[:40])))
    return out
