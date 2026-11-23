"""Study a book: put a book she admires beside her own manuscript and learn from how it is written, mostly narrative style
and flow. Code measures both books. Your editor only ever sees two short matched passages at a time, one of each, and then
one last ask over the notes, never the books.

The caller gives ``ask(messages) -> str`` (it goes through the brain queue) and a plain dict of saved answers keyed by the
sha of each ask, so a stopped run goes on without paying twice. Nothing here writes a file.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from typing import Any, Callable

from book import locate
from brainline import BrainSlow, now, parse_json, plain, sha_of, tokens
from marks import strip

DEFAULT_FOCUS = "narrative style and flow"
FOCUS_MAX = 240                 # characters of her focus that go into an ask
TITLE_MAX = 80
PASSAGE_MOST = 300              # words in one passage, at most
PASSAGE_LEAST = 180             # and at least, when the section and the room allow it
QUOTE_MOST = 25                 # words in a checked quote kept in the report
EDGE_WORDS = 40                 # words of a chapter's first or last sentence kept
WINDOW = 5000                   # words in one stretch for vocabulary variety
FRONT = "before the first chapter"    # Manuscript's name for what comes before the first heading
FRONT_LEAST = 100               # below this, that section is the e-book's leftovers, not a prologue
TIMEOUTS = (BrainSlow, TimeoutError)

SYSTEM = "You are a senior fiction editor helping a writer learn from a book she admires."

PAIR_RULES = """Rules:
- Say what each passage does on the page, as technique. Be concrete.
- Quote at most 12 words at a time, exactly as written in the passage.
- Pointers are things for her to try in her own way.
- Never rewrite her sentences. Never write new prose or example sentences for her book.
- Use plain words and short sentences.
Answer with JSON only, in this shape, with 1 to 3 pointers:
{"theirs_does": "what passage A does", "mine_does": "what passage B does", "pointers": [{"try": "something to try", "why": "why it may help", "their_quote": "a few words from passage A", "my_quote": "a few words from passage B"}]}"""

LAST_RULES = """Pick at most 5 pointers for her to try, in the order she should try them, the most useful first.
Rules:
- Each pointer is something to try in her own way.
- Never rewrite her sentences. Never write new prose or example sentences for her book.
- Use plain words and short sentences.
Answer with JSON only, in this shape:
[{"try": "something to try", "why": "why it may help", "from": "opening, dialogue, description, fast, ending, transition or numbers"}]"""

KINDS = ["opening", "dialogue", "description", "fast", "ending", "transition"]
LABELS = {"opening": "How the book opens", "dialogue": "People talking", "description": "A slower, descriptive stretch",
          "fast": "A fast stretch", "ending": "How a chapter closes", "transition": "Right after a scene break"}
# the measured differences that bear on each kind of passage, most telling first
BEARS_ON = {
    "opening": ["opens_with_dialogue", "sentences.mean", "narration.person", "narration.tense", "openers.pronoun_or_name"],
    "dialogue": ["dialogue.share", "dialogue.said_share", "dialogue.tag_adverbs", "per_1000.contractions",
                 "paragraphs.one_sentence"],
    "description": ["sentences.mean", "sentences.over_30", "per_1000.ly_adverbs", "per_1000.filter_words",
                    "paragraphs.mean", "per_1000.semicolons"],
    "fast": ["sentences.under_8", "sentences.mean", "paragraphs.one_sentence", "per_1000.dashes", "sentences.spread"],
    "ending": ["ends_with_dialogue", "chapter_words.mean", "scenes_per_chapter"],
    "transition": ["scenes_per_chapter", "openers.the", "openers.and_but_so", "openers.ing"],
}

# -- the words of a book --------------------------------------------------------------------------------------------

WORD = re.compile(r"[A-Za-z\u00c0-\u024f0-9]+(?:['\u2019][A-Za-z\u00c0-\u024f]+)*")
BREAK = re.compile(r"^\s*(?:\*(?:\s*\*)*|-{3,}|#{1,3}|~{1,3}|\u2022(?:\s*\u2022)*)\s*$")
PART = re.compile(r"^(.+?),\s*part\s+\w+\s*$", re.I)
DOUBLE = re.compile(r"\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d")
SINGLE = re.compile(r"(?<!\w)\u2018(?:[^\u2019\n]|\u2019(?=\w))*\u2019(?!\w)")
SENTENCE_END = re.compile(r"[.!?\u2026]+[\"\u201d\u2019')\]]*(?=\s+[\"\u201c\u2018'(\[]*[A-Z0-9])")
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "mt", "jr", "sr", "prof", "capt", "col", "gen", "lt", "sgt", "rev", "vs",
                 "etc", "no"}
NOT_LY = {"only", "early", "family", "lovely", "friendly", "holy", "ugly", "silly", "july", "reply", "apply", "supply",
          "ally", "rally", "belly", "jelly", "bully", "butterfly", "daily", "weekly", "monthly", "yearly", "likely",
          "unlikely", "lonely", "lively", "elderly", "costly", "deadly", "oily", "woolly", "hilly", "italy", "emily",
          "lily", "kelly", "sally", "billy", "molly", "polly", "holly", "dolly", "jolly", "folly", "curly", "chilly",
          "surly", "burly", "ghastly", "ghostly", "beastly", "heavenly", "worldly", "wily", "homely", "comely",
          "manly", "godly", "assembly", "anomaly", "melancholy", "monopoly", "rely", "tally", "gully", "sully",
          "fly", "lowly", "orderly", "scaly", "smelly", "wobbly", "bubbly", "prickly", "sickly", "measly", "unruly"}
FILTER_WORDS = {"saw", "felt", "heard", "noticed", "realized", "realised", "seemed", "wondered", "watched", "knew",
                "thought"}
FIRST = {"i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves"}
THIRD = {"he", "she", "him", "her", "his", "hers", "himself", "herself", "they", "them", "their", "theirs",
         "themselves"}
TENSE = [("was", "is"), ("were", "are"), ("had", "has"), ("said", "says"), ("went", "goes"), ("came", "comes"),
         ("looked", "looks"), ("took", "takes"), ("saw", "sees"), ("knew", "knows"), ("thought", "thinks"),
         ("felt", "feels"), ("turned", "turns"), ("walked", "walks"), ("stood", "stands"), ("got", "gets"),
         ("made", "makes"), ("did", "does"), ("asked", "asks"), ("told", "tells"), ("seemed", "seems"),
         ("began", "begins"), ("ran", "runs"), ("sat", "sits"), ("found", "finds"), ("left", "leaves"),
         ("tried", "tries"), ("smiled", "smiles"), ("watched", "watches"), ("shook", "shakes")]
PAST = {a for a, _b in TENSE}
PRESENT = {b for _a, b in TENSE}
OPEN_PRONOUNS = {"i", "he", "she", "they", "we", "you", "it", "his", "her", "their", "my", "our", "its"}
NOT_ING = {"nothing", "something", "everything", "anything", "morning", "evening", "during", "king", "thing", "spring",
           "string", "ring", "bring", "sing", "wing", "ceiling", "building", "wedding", "darling", "pudding",
           "being", "thing", "things"}
CONTRACTED = ("n't", "'re", "'ve", "'ll", "'d", "'m")
CONTRACTED_S = {"it's", "that's", "he's", "she's", "what's", "there's", "here's", "who's", "where's", "let's", "how's"}
SPEECH = ("said|says|asked|asks|replied|replies|answered|answers|whispered|whispers|shouted|shouts|called|calls|"
          "muttered|mutters|murmured|murmurs|exclaimed|exclaims|snapped|snaps|hissed|hisses|growled|growls|breathed|"
          "cried|cries|yelled|yells|screamed|screams|added|adds|continued|continues|insisted|insists|demanded|"
          "offered|admitted|agreed|laughed|laughs|sighed|sighs|gasped|snarled|barked|stammered|mumbled|warned|"
          "explained|repeated|retorted|pleaded|begged|teased|announced|declared|protested|groaned|whimpered|"
          "chuckled|grunted|sobbed|spat|drawled|bellowed|roared|sneered")
PLAIN_TAGS = {"said", "says", "asked", "asks"}
WHO = r"(?:[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*)?|he|she|they|I|we|you|it)"
# A dialogue tag: a closing quote mark, then a speaker and a speech verb in either order ("he said", "said Elias"). The
# -ly word it catches is one right before the verb ("she quietly said") or within the two words after the tag ("he said
# softly", "said Elias quietly"), not on the NOT_LY list. It misses tags far from the quote, such as "the man at the gate
# says", and tags in single straight quotes.
TAG = re.compile(rf"[\"\u201d\u2019]\s*,?\s*(?:{WHO}\s+(?P<pre>[a-z]+ly\s+)?(?P<v1>{SPEECH})|(?P<v2>{SPEECH})\s+{WHO})"
                 rf"\b(?P<rest>(?:\s+[\w'\u2019]+){{0,2}})")


def words_of(text: str) -> list[str]:
    return WORD.findall(text)


def is_ly(w: str) -> bool:
    w = w.lower()
    return len(w) >= 5 and w.endswith("ly") and w not in NOT_LY


def sentences(par: str) -> list[str]:
    """A paragraph's sentences. A full stop after Mr, Dr and the like, or after one capital letter, ends nothing."""
    out, at = [], 0
    for m in SENTENCE_END.finditer(par):
        if m.group().startswith(".") and len(m.group().rstrip("\"\u201d\u2019')]")) == 1:
            before = re.findall(r"[A-Za-z]+", par[max(at, m.start() - 8):m.start()])
            if before and (before[-1].lower() in ABBREVIATIONS or (len(before[-1]) == 1 and before[-1].isupper())):
                continue
        out.append(par[at:m.end()].strip())
        at = m.end()
    tail = par[at:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if WORD.search(s)]


def paragraphs_of(text: str) -> list[str]:
    """Paragraphs: one per line, as Manuscript writes them; a block of hard-wrapped lines is joined back into one."""
    out = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) > 1 and all(45 <= len(ln) <= 80 for ln in lines[:-1]) and not any(BREAK.match(ln) for ln in lines):
            out.append(" ".join(lines))
        else:
            out.extend(lines)
    return out


def spoken(par: str, single: bool) -> list[tuple[int, int]]:
    """Where the talk is in a paragraph. Speech that runs on into the next paragraph opens here and does not close."""
    spans = [(m.start(), m.end()) for m in (SINGLE if single else DOUBLE).finditer(par)]
    rest_at = spans[-1][1] if spans else 0
    rest = par[rest_at:]
    m = re.search(r"(?<!\w)\u2018" if single else r"[\"\u201c]", rest)
    if m:
        spans.append((rest_at + m.start(), len(par)))
    return spans


def outside(par: str, single: bool) -> str:
    chars = list(par)
    for a, b in spoken(par, single):
        chars[a:b] = " " * (b - a)
    return "".join(chars)


class Para:
    __slots__ = ("n", "text", "brk", "words", "said", "sents", "lens", "narration")

    def __init__(self, n: int, text: str, single: bool) -> None:
        self.n, self.text = n, text
        self.brk = bool(BREAK.match(text))
        if self.brk:
            self.words, self.said, self.sents, self.lens, self.narration = 0, 0, [], [], ""
            return
        self.words = len(words_of(text))
        self.said = sum(len(words_of(text[a:b])) for a, b in spoken(text, single))
        self.sents = sentences(text)
        self.lens = [len(words_of(s)) for s in self.sents]
        self.narration = outside(text, single)


def _single_quoted(text: str) -> bool:
    """A book that puts its talk in single curly quotes, the British way, and hardly ever in double ones."""
    doubles = text.count("\u201c") + text.count('"') // 2
    singles = len(re.findall(r"(?<!\w)\u2018", text))
    return doubles < 2 and singles >= 2


def chapters(book) -> list[dict]:
    """The story's chapters, in order: each section's text without its italic marks and without the e-book's wrapping,
    and sections named "X, part N" joined into chapter X. Each is {"title", "paras": [Para]}."""
    texts = [(s, strip(s.story)) for s in book.sections]
    texts = [(s, t) for s, t in texts if words_of(t)]
    if len(texts) > 1:
        texts = [(s, t) for s, t in texts
                 if not (s.heading.strip().lower() == FRONT and len(words_of(t)) < FRONT_LEAST)]
    single = _single_quoted("\n".join(t for _s, t in texts))
    out: list[dict] = []
    for s, t in texts:
        m = PART.match(s.heading.strip())
        title = m.group(1).strip() if m else (s.heading.strip() or f"Section {s.n}")
        paras = [Para(s.n, p, single) for p in paragraphs_of(t)]
        if m and out and out[-1]["title"] == title:
            out[-1]["paras"].extend(paras)
        else:
            out.append({"title": title, "paras": paras})
    return out


# -- measure: the style numbers, by code ------------------------------------------------------------------------------

def _r(x: float, d: int = 1) -> float:
    return round(float(x), d)


def _share(a: float, b: float) -> float:
    return round(a / b, 2) if b else 0.0


def _per_k(a: float, b: float) -> float:
    return round(a * 1000 / b, 1) if b else 0.0


def _cap(text: str) -> str:
    w = text.split()
    return " ".join(w[:EDGE_WORDS]) + (" ..." if len(w) > EDGE_WORDS else "")


def _is_talk(sentence: str) -> bool:
    return bool(re.search(r"[\"\u201c\u201d]|(?<!\w)\u2018", sentence))


def measure(book) -> dict:
    """The style numbers of one book, by code alone."""
    chs = chapters(book)
    paras = [p for c in chs for p in c["paras"] if not p.brk]
    lens = [n for p in paras for n in p.lens]
    sents = [s for p in paras for s in p.sents]
    words = sum(p.words for p in paras)
    said = sum(p.said for p in paras)
    text = "\n".join(p.text for p in paras)
    all_words = [w.lower().replace("\u2019", "'") for w in words_of(text)]
    narration = [w.lower().replace("\u2019", "'") for p in paras for w in words_of(p.narration)]

    ch_words = [sum(p.words for p in c["paras"]) for c in chs]
    scenes = []
    for c in chs:
        body = c["paras"]
        inner = [i for i, p in enumerate(body) if p.brk and any(not q.brk for q in body[:i])
                 and any(not q.brk for q in body[i + 1:])]
        scenes.append(len(inner) + 1)

    tags = plain_tags = tag_ly = 0
    for p in paras:
        for m in TAG.finditer(p.text):
            tags += 1
            verb = m.group("v1") or m.group("v2")
            plain_tags += verb in PLAIN_TAGS
            if m.group("pre") and is_ly(m.group("pre").strip()) or any(is_ly(w) for w in words_of(m.group("rest"))):
                tag_ly += 1

    first = sum(w in FIRST for w in narration)
    third = sum(w in THIRD for w in narration)
    person = "first" if first + third and first / (first + third) >= 0.3 else "third"
    past = sum(w in PAST for w in narration)
    present = sum(w in PRESENT for w in narration)
    tense = "present" if present > past else "past"

    names = {w for w, k in _counts(re.findall(r"(?<=[a-z,;] )([A-Z][a-z\u00e0-\u024f]+)", text)).items() if k >= 2}
    starts = []
    for s in sents:
        m = WORD.search(s)
        starts.append(m.group() if m else "")
    n_s = len(starts)

    windows = []
    size = min(WINDOW, len(all_words))
    if size:
        for i in range(0, max(1, len(all_words) - size + 1), size):
            windows.append(len(set(all_words[i:i + size])))

    edges = []
    for c in chs:
        body = [p for p in c["paras"] if p.sents]
        if body:
            first_s, last_s = body[0].sents[0], body[-1].sents[-1]
            edges.append({"chapter": c["title"], "first": _cap(first_s), "first_dialogue": _is_talk(first_s),
                          "last": _cap(last_s), "last_dialogue": _is_talk(last_s)})

    return {
        "words": words,
        "chapters": len(chs),
        "chapter_words": {"mean": _r(statistics.mean(ch_words), 0) if ch_words else 0,
                          "shortest": min(ch_words) if ch_words else 0, "longest": max(ch_words) if ch_words else 0},
        "scenes_per_chapter": _r(statistics.mean(scenes)) if scenes else 0,
        "sentences": {
            "count": len(lens),
            "mean": _r(statistics.mean(lens)) if lens else 0,
            "median": _r(statistics.median(lens)) if lens else 0,
            "under_8": _share(sum(n < 8 for n in lens), len(lens)),
            "over_30": _share(sum(n > 30 for n in lens), len(lens)),
            "spread": _r(statistics.pstdev(lens)) if len(lens) > 1 else 0,
            "by_chapter": [_r(statistics.mean(ls)) if ls else 0
                           for ls in ([n for p in c["paras"] for n in p.lens] for c in chs)],
        },
        "paragraphs": {"count": len(paras), "mean": _r(words / len(paras)) if paras else 0,
                       "one_sentence": _share(sum(len(p.sents) == 1 for p in paras), len(paras))},
        "dialogue": {
            "share": _share(said, words),
            "by_chapter": [_share(sum(p.said for p in c["paras"]), sum(p.words for p in c["paras"])) for c in chs],
            "tags": tags,
            "said_share": _share(plain_tags, tags),
            "tag_adverbs": _per_k(tag_ly, words),
        },
        "narration": {
            "person": person,
            # the share of pronouns outside the talk that are in that person, and how many pronouns that was
            "person_share": _share(first if person == "first" else third, first + third),
            "pronouns": first + third,
            "tense": tense,
            "tense_share": _share(present if tense == "present" else past, past + present),
            "verbs": past + present,
        },
        "per_1000": {
            "ly_adverbs": _per_k(sum(is_ly(w) for w in all_words), words),
            # in the narration only, per 1,000 words of narration
            "filter_words": _per_k(sum(w in FILTER_WORDS for w in narration), len(narration)),
            "dashes": _per_k(text.count("\u2014") + text.count(" - ") + text.count(" \u2013 ") + text.count("--"), words),
            "semicolons": _per_k(text.count(";"), words),
            "ellipses": _per_k(len(re.findall(r"\u2026|\.\s?\.\s?\.", text)), words),
            "exclamations": _per_k(text.count("!"), words),
            "questions": _per_k(text.count("?"), words),
            "contractions": _per_k(sum(w.endswith(CONTRACTED) or w in CONTRACTED_S for w in all_words), words),
        },
        "openers": {
            "pronoun_or_name": _share(sum(w.lower() in OPEN_PRONOUNS or w in names for w in starts), n_s),
            "the": _share(sum(w == "The" for w in starts), n_s),
            "and_but_so": _share(sum(w in ("And", "But", "So") for w in starts), n_s),
            "ing": _share(sum(len(w) >= 5 and w.lower().endswith("ing") and w.lower() not in NOT_ING
                              for w in starts), n_s),
        },
        "variety": {"distinct_per_window": _r(statistics.mean(windows), 0) if windows else 0, "window": size},
        "opens_with_dialogue": _share(sum(e["first_dialogue"] for e in edges), len(edges)),
        "ends_with_dialogue": _share(sum(e["last_dialogue"] for e in edges), len(edges)),
        "chapter_edges": edges,
    }


def _counts(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


# -- differences: plain lines a novelist understands -----------------------------------------------------------------

FRACTIONS = [(1 / 20, "one part in twenty"), (1 / 10, "a tenth"), (1 / 8, "an eighth"), (1 / 6, "a sixth"),
             (1 / 5, "a fifth"), (1 / 4, "a quarter"), (1 / 3, "a third"), (2 / 5, "two fifths"), (1 / 2, "half"),
             (3 / 5, "three fifths"), (2 / 3, "two thirds"), (3 / 4, "three quarters"), (4 / 5, "four fifths"),
             (9 / 10, "nine tenths")]


def frac(x: float) -> str:
    if x < 0.015:
        return "almost none"
    if x > 0.96:
        return "nearly all"
    return "about " + min(FRACTIONS, key=lambda f: abs(f[0] - x))[1]


def num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() or abs(x) >= 100 else f"{x:.1f}"


def _per_line(what: str) -> Callable[[float, float], str]:
    return lambda m, t: f"{what}: {num(t)} per 1,000 words in theirs, {num(m)} in yours."


def _share_line(what: str) -> Callable[[float, float], str]:
    return lambda m, t: f"{what}: {frac(t)} of theirs, {frac(m)} of yours."


# key: (unit, smallest gap worth a line, what each book needs to count, line)
NUMBERS: dict[str, tuple[str, float, str, Callable[[Any, Any], str]]] = {
    "sentences.mean": ("words", 2, "sentences",
                       lambda m, t: f"Your sentences run {num(m)} words on average. Theirs run {num(t)}."),
    "sentences.under_8": ("share", 0.05, "sentences", _share_line("Short sentences, under 8 words")),
    "sentences.over_30": ("share", 0.04, "sentences", _share_line("Long sentences, over 30 words")),
    "sentences.spread": ("words", 2, "sentences",
                         lambda m, t: f"Their sentence lengths swing about {num(t)} words either side of the average. "
                                      f"Yours swing about {num(m)}."),
    "paragraphs.mean": ("words", 8, "words",
                        lambda m, t: f"Their paragraphs run {num(t)} words on average. Yours run {num(m)}."),
    "paragraphs.one_sentence": ("share", 0.08, "words", _share_line("Paragraphs of one sentence")),
    "dialogue.share": ("share", 0.05, "words",
                       lambda m, t: f"{frac(t)[0].upper() + frac(t)[1:]} of their book is people talking. "
                                    f"In yours it is {frac(m)}."),
    "dialogue.said_share": ("share", 0.1, "tags",
                            lambda m, t: f"Of their dialogue tags, {frac(t)} are said or asked. Of yours, {frac(m)}."),
    "dialogue.tag_adverbs": ("per 1,000 words", 1, "tags", _per_line("Tags with an -ly word, like said softly")),
    "narration.person": ("person", 0, "pronouns",
                         lambda m, t: f"Theirs is told in the {t} person. Yours is told in the {m} person."),
    "narration.tense": ("tense", 0, "verbs",
                        lambda m, t: f"Theirs is told in the {t} tense. Yours is told in the {m} tense."),
    "per_1000.ly_adverbs": ("per 1,000 words", 3, "words", _per_line("Words ending in -ly")),
    "per_1000.filter_words": ("per 1,000 words", 3, "words",
                              _per_line("Words like saw, felt, heard and wondered in the narration")),
    "per_1000.dashes": ("per 1,000 words", 2, "words", _per_line("Dashes")),
    "per_1000.semicolons": ("per 1,000 words", 1, "words", _per_line("Semicolons")),
    "per_1000.ellipses": ("per 1,000 words", 1, "words", _per_line("Trailing dots (...)")),
    "per_1000.exclamations": ("per 1,000 words", 1.5, "words", _per_line("Exclamation marks")),
    "per_1000.questions": ("per 1,000 words", 2, "words", _per_line("Question marks")),
    "per_1000.contractions": ("per 1,000 words", 5, "words", _per_line("Contractions like don't and she'd")),
    "openers.pronoun_or_name": ("share", 0.08, "sentences", _share_line("Sentences that start with a name or a pronoun")),
    "openers.the": ("share", 0.05, "sentences", _share_line("Sentences that start with The")),
    "openers.and_but_so": ("share", 0.03, "sentences", _share_line("Sentences that start with And, But or So")),
    "openers.ing": ("share", 0.02, "sentences", _share_line("Sentences that start with an -ing word")),
    "variety.distinct_per_window": ("distinct words", 60, "window",
                                    lambda m, t: f"In each stretch of 5,000 words they use {num(t)} different words. "
                                                 f"You use {num(m)}."),
    "chapter_words.mean": ("words", 300, "chapters",
                           lambda m, t: f"Their chapters run {num(t)} words on average. Yours run {num(m)}."),
    "scenes_per_chapter": ("scenes", 0.5, "chapters",
                           lambda m, t: f"Their chapters hold {num(t)} scenes on average. Yours hold {num(m)}."),
    "opens_with_dialogue": ("share", 0.15, "chapters3", _share_line("Chapters that open with someone talking")),
    "ends_with_dialogue": ("share", 0.15, "chapters3", _share_line("Chapters that close on someone talking")),
}
APART_MOST = 8
ALIKE_MOST = 4
APART_GAP = 0.25                # how far apart, in relative terms, before it is worth a line
ALIKE_GAP = 0.1


def _get(numbers: dict, key: str) -> Any:
    v: Any = numbers
    for k in key.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def _enough(numbers: dict, need: str) -> bool:
    if numbers.get("words", 0) < 200:
        return False
    return {"words": True,
            "sentences": _get(numbers, "sentences.count") >= 20,
            "tags": _get(numbers, "dialogue.tags") >= 5,
            "pronouns": _get(numbers, "narration.pronouns") >= 20,
            "verbs": _get(numbers, "narration.verbs") >= 20,
            "window": _get(numbers, "variety.window") >= WINDOW,
            "chapters": numbers.get("chapters", 0) >= 2,
            "chapters3": numbers.get("chapters", 0) >= 3}[need]


def _counted(mine: dict, theirs: dict, key: str, unit: str, m: float, t: float) -> bool:
    """A rate or share stands on at least 3 real cases in one of the books, not on one stray dash."""
    if unit == "per 1,000 words":
        return max(m * mine["words"], t * theirs["words"]) / 1000 >= 3
    if unit == "share" and key.startswith(("sentences.", "openers.")):
        return max(m * mine["sentences"]["count"], t * theirs["sentences"]["count"]) >= 3
    return True


def differences(mine: dict, theirs: dict) -> dict:
    """Where the two books are far apart and where they are close, as plain lines with both values. Observations only."""
    apart, alike = [], []
    for key, (unit, least, need, line) in NUMBERS.items():
        m, t = _get(mine, key), _get(theirs, key)
        if m is None or t is None or not (_enough(mine, need) and _enough(theirs, need)):
            continue
        item = {"key": key, "line": line(m, t), "mine": m, "theirs": t, "unit": unit}
        if isinstance(m, str):
            (apart if m != t else alike).append((1.0 if m != t else 0.0, item))
            continue
        top = max(abs(m), abs(t))
        gap = abs(m - t) / top if top else 0.0
        if gap >= APART_GAP and abs(m - t) >= least and _counted(mine, theirs, key, unit, m, t):
            # ranked by the gap softened by the smallest gap worth a line, so 0 against 1.6 does not beat 12 against 3.5;
            # punctuation and sentence openers weigh a little less than the shape of sentences, paragraphs and talk
            weight = 0.7 if key.startswith(("per_1000.", "openers.")) else 1.0
            apart.append((weight * abs(m - t) / (top + least) if unit not in ("person", "tense") else 1.0, item))
        elif top and gap <= ALIKE_GAP:
            alike.append((gap, item))
    apart.sort(key=lambda g: -g[0])
    alike.sort(key=lambda g: g[0])
    return {"apart": [i for _g, i in apart[:APART_MOST]], "alike": [i for _g, i in alike[:ALIKE_MOST]]}


# -- pairs: matched passages ----------------------------------------------------------------------------------------

def _pair_user(their_title: str, label: str, focus: str, facts: list[str], theirs: str, mine: str) -> str:
    lines = [f"Passage A comes from a book she admires, {their_title[:TITLE_MAX]}. Passage B comes from her own manuscript.",
             f"What the two passages show: {label}.",
             f"Her focus: {focus}."]
    if facts:
        lines.append("What was measured across both books:")
        lines += [f"- {f}" for f in facts]
    lines += ["", "Passage A, from the book she admires:", "<<<", theirs, ">>>", "",
              "Passage B, from her manuscript:", "<<<", mine, ">>>", "", PAIR_RULES]
    return "\n".join(lines)


def ask_tokens(messages: list[dict]) -> int:
    return sum(tokens(m["content"]) for m in messages)


def _most_words(room: int) -> int:
    """Words one passage may have so that two of them and the ask around them fit the room."""
    filler = " ".join(["word"] * 30)
    frame = [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": _pair_user("x" * TITLE_MAX, max(LABELS.values(), key=len), "x" * FOCUS_MAX,
                                                     [filler] * 3, "", "")}]
    per = (room - ask_tokens(frame) - 8) // 2
    return min(PASSAGE_MOST, int(per / 1.45))


def _window(paras: list[Para], i: int, least: int, most: int, backward: bool = False) -> tuple[int, int, str] | None:
    """Paragraphs from i on (or back from i), inside one section and one scene, up to ``most`` words, stopping once
    ``least`` is reached. A paragraph too long on its own is cut at a sentence."""
    if i < 0 or i >= len(paras) or paras[i].brk or not paras[i].words:
        return None
    n = paras[i].n
    got: list[int] = []
    count = 0
    j = i
    while 0 <= j < len(paras) and paras[j].n == n and not paras[j].brk:
        if count + paras[j].words > most:
            break
        got.append(j)
        count += paras[j].words
        if count >= least:
            break
        j += -1 if backward else 1
    if not got:
        sents = paras[i].sents[::-1] if backward else paras[i].sents
        keep, c = [], 0
        for s in sents:
            w = len(words_of(s))
            if keep and c + w > most:
                break
            keep.append(s)
            c += w
        text = " ".join(keep[::-1] if backward else keep)
        return i, i, text
    got.sort()
    return got[0], got[-1], "\n\n".join(paras[k].text for k in got)


def _stats(paras: list[Para], a: int, b: int) -> tuple[float, float]:
    """(share of talk, mean sentence length) of paragraphs a to b."""
    w = sum(p.words for p in paras[a:b + 1])
    said = sum(p.said for p in paras[a:b + 1])
    lens = [n for p in paras[a:b + 1] for n in p.lens]
    return (said / w if w else 0.0), (statistics.mean(lens) if lens else 0.0)


def _candidates(paras: list[Para], least: int, most: int) -> list[tuple[int, int, str, float, float]]:
    """Every window, without the stubs at the end of a section: a window must be near the longest the book allows."""
    out, seen = [], set()
    for i in range(len(paras)):
        got = _window(paras, i, least, most)
        if got and got[:2] not in seen:
            seen.add(got[:2])
            out.append((*got, len(words_of(got[2]))))
    floor = min(least, 0.7 * max((c[3] for c in out), default=0))
    return [(a, b, t, *_stats(paras, a, b)) for a, b, t, w in out if w >= floor]


def _choose(paras: list[Para], chs: list[dict], least: int, most: int) -> dict[str, tuple[int, int, str]]:
    """For one book, the passage of each kind it has: (first paragraph, last paragraph, text)."""
    out: dict[str, tuple[int, int, str]] = {}
    if not paras:
        return out
    starts, at = [], 0
    for c in chs:
        starts.append(at)
        at += len(c["paras"])
    first = next((k for k in range(len(paras)) if not paras[k].brk and paras[k].words), None)
    if first is not None:
        got = _window(paras, first, least, most)
        if got:
            out["opening"] = got
    cands = _candidates(paras, least, most)
    used = [out["opening"][:2]] if "opening" in out else []

    def free(c: tuple) -> bool:
        return all(c[1] < a or c[0] > b for a, b in used)

    scored = {
        "dialogue": [(-c[3], k, c) for k, c in enumerate(cands) if c[3] >= 0.15],
        "description": [(-(c[4] * (1 - c[3])), k, c) for k, c in enumerate(cands) if c[3] <= 0.5],
        "fast": [(c[4], k, c) for k, c in enumerate(cands) if c[3] < 0.5],
    }
    for kind in ("dialogue", "description", "fast"):
        for _score, _k, c in sorted(scored[kind], key=lambda s: (s[0], s[1])):
            if free(c):
                out[kind] = c[:3]
                used.append(c[:2])
                break
    middle = chs[len(chs) // 2]
    begin = starts[len(chs) // 2]
    last = max((begin + k for k, p in enumerate(middle["paras"]) if not p.brk and p.words), default=None)
    if last is not None:
        got = _window(paras, last, least, most, backward=True)
        if got:
            out["ending"] = got
    brk = next((k for k in range(1, len(paras) - 1) if paras[k].brk and paras[k + 1].n == paras[k].n
                and not paras[k + 1].brk and any(not p.brk for p in paras[:k])), None)
    if brk is not None:
        got = _window(paras, brk + 1, least, most)
        if got:
            out["transition"] = got
    return out


def pairs(mine_book, their_book, room: int) -> list[dict]:
    """Passages of the same kind from each book, cut at paragraphs, sized so two of them and the ask fit ``room``."""
    most = _most_words(room)
    if most < 40:
        return []
    least = min(PASSAGE_LEAST, int(most * 0.6))
    flat_mine, chs_mine = _flat(mine_book)
    flat_theirs, chs_theirs = _flat(their_book)
    mine = _choose(flat_mine, chs_mine, least, most)
    theirs = _choose(flat_theirs, chs_theirs, least, most)
    out = []
    for kind in KINDS:
        if kind in mine and kind in theirs:
            out.append({"kind": kind, "label": LABELS[kind],
                        "mine": {"section": flat_mine[mine[kind][0]].n, "text": mine[kind][2]},
                        "theirs": {"section": flat_theirs[theirs[kind][0]].n, "text": theirs[kind][2]}})
    return out


def _flat(book) -> tuple[list[Para], list[dict]]:
    chs = chapters(book)
    return [p for c in chs for p in c["paras"]], chs


def steps(mine_book, their_book, room: int) -> int:
    """How many asks :func:`run` makes when nothing is saved yet and nothing runs out of time."""
    n = len(pairs(mine_book, their_book, room))
    return n + 1 if n else 0


# -- run: the asks ----------------------------------------------------------------------------------------------------

def _cut(text: str, keep_end: bool, half: bool) -> str:
    """Shorter: half the paragraphs (or sentences), or one paragraph (or sentence) fewer. The end kept for an ending."""
    parts = text.split("\n\n")
    joiner = "\n\n"
    if len(parts) == 1:
        parts, joiner = sentences(text), " "
    if len(parts) <= 1:
        w = text.split()
        if len(w) <= 1:
            return text
        k = len(w) // 2 if half else len(w) - max(1, len(w) // 10)
        return " ".join(w[-k:] if keep_end else w[:k])
    k = math.ceil(len(parts) / 2) if half else len(parts) - 1
    return joiner.join(parts[-k:] if keep_end else parts[:k])


def _facts(kind: str, diff: dict) -> list[str]:
    near = {i["key"]: i for i in diff["apart"] + diff["alike"]}
    picked = [near[k]["line"] for k in BEARS_ON[kind] if k in near][:3]
    return picked or [i["line"] for i in diff["apart"][:2]]


def _pair_messages(p: dict, theirs: str, mine: str, their_title: str, focus: str, facts: list[str],
                   room: int) -> tuple[list[dict], str, str]:
    keep_end = p["kind"] == "ending"
    while True:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _pair_user(their_title, p["label"], focus, facts, theirs, mine)}]
        if ask_tokens(msgs) <= room:
            return msgs, theirs, mine
        if len(facts) > 1 and len(theirs.split()) + len(mine.split()) < 120:
            facts = facts[:-1]
            continue
        a, b = (theirs, mine) if len(theirs) >= len(mine) else (mine, theirs)
        shorter = _cut(a, keep_end, half=False)
        if shorter == a:
            return msgs, theirs, mine         # nothing left to cut; the caller's room is too small for any ask
        theirs, mine = (shorter, b) if a is theirs else (b, shorter)


QUOTED = re.compile(r"\"([^\"]+)\"|\u201c([^\u201d]+)\u201d|\u2018([^\u2019]+)\u2019|(?<!\w)'([^']+)'(?!\w)")
REWRITE = re.compile(r"(?:\b(?:rewrite|rephrase|reword)\b[^:]{0,60}|\b(?:replace|change|turn)\b[^:]{0,40}"
                     r"\b(?:with|to|into|as))\s*:\s*(.{20,})", re.I)


def _writes_prose(text: str, passages: list[str]) -> bool:
    """New prose for her book: a long quoted sentence, or the words after "rewrite it as:", in none of the passages."""
    for m in QUOTED.finditer(text):
        q = next(g for g in m.groups() if g is not None)
        if len(q.split()) >= 6 and not any(locate(p, q) for p in passages):
            return True
    m = REWRITE.search(text)
    if m and len(m.group(1).split()) >= 5 and not any(locate(p, m.group(1).strip(" .")) for p in passages):
        return True
    return False


def _real(quote: Any, passage: str) -> str:
    """The quote exactly as it is in the passage, at most 25 words, or nothing if it is not really there."""
    q = str(quote or "").strip() if not isinstance(quote, (dict, list)) else ""
    pieces = sorted(re.split(r"\s*(?:\.{3}|\u2026)\s*", q), key=len, reverse=True)
    q = pieces[0].strip() if pieces else ""
    if len(words_of(q)) < 2:
        return ""
    at = locate(passage, q)
    if not at:
        return ""
    return " ".join(passage[at[0]:at[1]].split()[:QUOTE_MOST])


def _notes(got: Any, theirs: str, mine: str) -> dict | None:
    if not isinstance(got, dict):
        return None
    out = {"theirs_does": plain(got.get("theirs_does"), 800), "mine_does": plain(got.get("mine_does"), 800),
           "pointers": []}
    for x in got.get("pointers") if isinstance(got.get("pointers"), list) else []:
        if not isinstance(x, dict):
            continue
        try_, why = plain(x.get("try"), 400), plain(x.get("why"), 400)
        if not try_ or _writes_prose(try_, [theirs, mine]) or _writes_prose(why, [theirs, mine]):
            continue
        tq, mq = _real(x.get("their_quote"), theirs), _real(x.get("my_quote"), mine)
        if not tq and not mq:
            continue
        out["pointers"].append({"try": try_, "why": why, "their_quote": tq, "my_quote": mq})
    out["pointers"] = out["pointers"][:3]
    return out


def _last_user(focus: str, diff: dict, done: list[dict], room: int, half: bool = False) -> list[dict]:
    head = [f"She is comparing her manuscript with a book she admires. Her focus: {focus}.", "",
            "What was measured across both books:"]
    head += [f"- {i['line']}" for i in diff["apart"]] or ["- Nothing stood far apart."]
    head += ["", "What you saw in matched passages, A from the book she admires, B from her manuscript:"]
    notes = []
    for p in done:
        notes.append(f"[{p['kind']}] {p['label']}")
        notes.append(f"A: {p['theirs_does']}")
        notes.append(f"B: {p['mine_does']}")
        notes += [f"Pointer: {x['try']} Why: {x['why']}" for x in p["pointers"]]
    if half:
        notes = notes[:max(1, len(notes) // 2)]
    while True:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "\n".join(head + notes + ["", LAST_RULES])}]
        if ask_tokens(msgs) <= room or not notes:
            return msgs
        notes = notes[:-1]


def _last_pointers(got: Any, passages: list[str]) -> list[dict] | None:
    if isinstance(got, dict) and isinstance(got.get("pointers"), list):
        got = got["pointers"]
    if not isinstance(got, list):
        return None
    out = []
    for x in got:
        if not isinstance(x, dict):
            continue
        try_, why = plain(x.get("try"), 400), plain(x.get("why"), 400)
        if not try_ or _writes_prose(try_, passages) or _writes_prose(why, passages):
            continue
        frm = str(x.get("from") or "").strip().lower()
        out.append({"try": try_, "why": why, "from": frm if frm in KINDS or frm == "numbers" else ""})
    return out[:5]


def run(mine_book, their_book, ask: Callable[[list[dict]], str], room: int, focus: str = "",
        saved: dict | None = None, progress: Callable[[int, int, str], Any] | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict:
    """Study her manuscript beside a book she admires. Returns one report, JSON-ready."""
    saved = {} if saved is None else saved
    focus = plain(focus, FOCUS_MAX) or DEFAULT_FOCUS
    mine_n, theirs_n = measure(mine_book), measure(their_book)
    diff = differences(mine_n, theirs_n)
    ps = pairs(mine_book, their_book, room)
    total = len(ps) + 1 if ps else 0
    report: dict = {"state": "running", "mine": mine_book.title, "theirs": their_book.title, "focus": focus,
                    "numbers": {"mine": mine_n, "theirs": theirs_n}, "apart": diff["apart"], "alike": diff["alike"],
                    "pairs": [], "pointers": [], "skipped": [], "made_at": now()}
    done = 0

    def tell(line: str) -> None:
        if progress:
            progress(done, total, line)

    def stop() -> bool:
        if should_stop and should_stop():
            report["state"] = "stopped"
            tell("Stopped")
            return True
        return False

    def call(msgs: list[dict]) -> str:
        key = sha_of(json.dumps(msgs, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if key not in saved:
            saved[key] = ask(msgs)
        return saved[key]

    if not ps:
        report["skipped"].append({"step": "all", "label": "The passages",
                                  "line": "There was not enough story in one of the books to set passages side by "
                                          "side, so only the numbers are here."})
    for p in ps:
        if stop():
            return report
        tell(f"Reading two passages: {p['label'].lower()}")
        facts = _facts(p["kind"], diff)
        msgs, theirs, mine = _pair_messages(p, p["theirs"]["text"], p["mine"]["text"], their_book.title, focus, facts,
                                            room)
        entry = {**p, "state": "skipped", "theirs_does": "", "mine_does": "", "pointers": []}
        try:
            text = call(msgs)
        except TIMEOUTS:
            keep_end = p["kind"] == "ending"
            msgs, theirs, mine = _pair_messages(p, _cut(theirs, keep_end, True), _cut(mine, keep_end, True),
                                                their_book.title, focus, facts, room)
            try:
                text = call(msgs)
            except TIMEOUTS:
                text = None
        notes = _notes(parse_json(text), theirs, mine) if text is not None else None
        if notes is None:
            report["skipped"].append({"step": p["kind"], "label": p["label"],
                                      "line": f"{p['label']}: your editor ran out of time on these passages twice, "
                                              "so they were left out." if text is None else
                                              f"{p['label']}: your editor's answer could not be read, so these "
                                              "passages were left out."})
        else:
            entry.update(notes, state="done")
        report["pairs"].append(entry)
        done += 1

    finished = [e for e in report["pairs"] if e["state"] == "done"]
    if ps and not finished:
        report["skipped"].append({"step": "all", "label": "Every pair",
                                  "line": "Your editor could not read any of the passages this time. The numbers are "
                                          "all here. Study again to try the passages."})
    elif finished:
        if stop():
            return report
        tell("Putting the pointers in order")
        passages = [t for e in report["pairs"] for t in (e["theirs"]["text"], e["mine"]["text"])]
        got = None
        try:
            got = _last_pointers(parse_json(call(_last_user(focus, diff, finished, room))), passages)
        except TIMEOUTS:
            try:
                got = _last_pointers(parse_json(call(_last_user(focus, diff, finished, room, half=True))), passages)
            except TIMEOUTS:
                got = None
        if got is None:
            report["skipped"].append({"step": "pointers", "label": "Putting the pointers in order",
                                      "line": "Your editor could not put the pointers in order, so here is the first "
                                              "pointer from each pair of passages."})
            got = [{"try": e["pointers"][0]["try"], "why": e["pointers"][0]["why"], "from": e["kind"]}
                   for e in finished if e["pointers"]][:5]
        report["pointers"] = got
        done += 1
    report["state"] = "done"
    tell("Done")
    return report
