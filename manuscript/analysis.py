"""The reading: your bot reads the whole manuscript, section by section, and builds the story bible (CONTRACT.md).

Every model call goes through the Core's ``POST /v1/brain/chat`` with the package token, so the words stay on this
computer. The brain on the owner's computer is small (about 8,192 tokens of context), so each ask is kept under about
5,000 tokens of input, counted roughly as 1.4 tokens a word, and asks for compact JSON.

1. Extract: two calls per section, JSON only. The first, with the names already known, for the facts (people,
   places, events and where each sits in the story's time, threads, themes, science, continuity). The second for how
   it is told (scenes and their purpose, point of view, tense, slips, tics, dialogue habits); word counts, dialogue
   share and how often a tic comes are counted here, not by the model. The facts ask also asks for the evidence for
   pictures (how people and places look, distances and directions, the era's visible marks); a section too long to
   carry that in the same ask gets a third, small ask for it instead, and so does a section cached before 0.3.3. ``<think>`` blocks are dropped before parsing;
   bad JSON gets one repair call, then the section is skipped with a note. Both are cached by the section's sha.
2. Merge: deterministic. The same name, a real alias (not "she" or "the doctor"), or a first name that matches exactly
   one known character and no other surname merges. "he" or "she" as a name is nobody.
   Voice (point of view and tense by section, tics, dialogue habits) is put together here, by counting.
3. Synthesize: an arc call per character with four or more appearances, one call for threads and continuity, one for
   themes, one for the story order of the events, one for the structure. Each gets the merged facts, never the whole
   text. A re-read that finds every section's text and cache as the last whole reading left them, and no new word from
   the writer, asks nothing and keeps that reading (``read_of``).
4. ``bible.base.json`` (the bible as read) and ``bible.json`` (that with the writer's own corrections on top) are written after every step, through the ``publish`` hook, and ``status.json`` is kept current. Synthesis
   asks are made from the corrected facts and told her corrections are settled. Nothing here writes a section.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import itertools
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import marks
from blocks import slug

BIBLE = "bible.json"
BASE = "bible.base.json"         # 0.4: the bible as read, before her corrections, so an undo needs no re-read
STATUS = "status.json"
CACHE = "cache"
BUDGET = 5000                    # tokens of input per ask
PART_WORDS = 2200
SHORT_PART_WORDS = 1000          # a story with no headings is read finer, so the model notices more
SCHEMA = "manuscript-bible/2"
BRIEF_TABS = ("characters", "plot", "timeline", "structure", "world", "science", "themes", "voice")
MAIN_ROLES = ("protagonist", "antagonist", "narrator", "main character", "deuteragonist")
QUOTE_MAX = 200
NOTE_MAX = 400
CALL_TIMEOUT = 600
BUSY_WAIT = 30
BUSY_TRIES = 10
REPAIR = "Return only the JSON."
LOOKS_MAX = 12                   # looks kept per character
DETAILS_MAX = 10                 # details kept per place
LOOK_QUOTE = 120                 # a look's or detail's quote, in characters
TITLES = {"mr", "mrs", "ms", "miss", "dr", "doctor", "sir", "lady", "lord", "aunt", "uncle", "captain", "the",
          "old", "young", "father", "mother", "professor", "king", "queen"}
PRONOUNS = {"he", "she", "him", "her", "his", "hers", "they", "them", "their", "i", "me", "my", "we", "us", "our", "you",
            "your", "it", "someone", "somebody", "nobody", "unknown", "unnamed"}
SOMEONE = {"the", "a", "an", "her", "his", "their", "my", "our", "that", "this"}   # "the doctor", "her brother"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tokens(text: str) -> int:
    """A rough count, high rather than low: 1.4 a word, or a quarter of the characters for JSON with few spaces."""
    return int(max(len(text.split()) * 1.4, len(text) / 4)) + 1


def write_atomic(path: Path, text: str) -> None:
    """The whole file at once: a temp file beside it, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(40):            # on Windows a page reading the old file holds it for a moment
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, obj: Any) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(20):                # on Windows a file being replaced can't be opened for a moment
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                return default
            time.sleep(0.05)
        except (OSError, ValueError):
            return default
    return default


# -- the brain ---------------------------------------------------------------------------------------

class BrainDown(Exception):
    """The brain can't answer now, or refused: a plain line for the owner."""


class BrainBusy(BrainDown):
    """The Core answered 503: the brain is busy with something else."""


class BrainSlow(Exception):
    """One call took longer than the time allowed."""


class Brain:
    def __init__(self, api: str | None, token: str | None, wait_s: float = BUSY_WAIT, tries: int = BUSY_TRIES,
                 timeout: float = CALL_TIMEOUT) -> None:
        self.api = (api or "").rstrip("/")
        self.token = token or ""
        self.wait_s, self.tries, self.timeout = wait_s, tries, timeout
        # 0.3.3: True sends the words to the brain as written, out of the vault (the "Keep manuscripts out of the
        # vault" setting; the manifest declares needs.brain.as_written). The app sets this to its setting.
        self.as_written: Callable[[], bool] = lambda: True

    def chat(self, messages: list[dict], patient: bool = True) -> tuple[str, str]:
        """(text, model). ``patient``: on a 503 wait 30 seconds and try again, up to 10 times. A tool the chat is
        waiting on is not patient: it says so at once."""
        body = json.dumps({"messages": messages, "as_written": bool(self.as_written())}).encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # the Core is on this computer
        tries = self.tries if patient else 0
        busy = False
        for attempt in range(tries + 1):
            req = urllib.request.Request(self.api + "/v1/brain/chat", data=body, method="POST",
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    got = json.loads(r.read().decode("utf-8"))
                return str(got.get("text") or ""), str(got.get("model") or "")
            except urllib.error.HTTPError as exc:
                if exc.code != 503:
                    raise BrainDown(f"Your bot's brain refused the ask (code {exc.code}).") from None
                busy = True
            except (socket.timeout, TimeoutError):
                raise BrainSlow() from None
            except (urllib.error.URLError, ConnectionError, OSError, ValueError, AttributeError):
                busy = False             # the Core is restarting, or the answer was cut: like a 503
            if attempt < tries:
                time.sleep(self.wait_s)
        raise (BrainBusy if busy else BrainDown)("Your bot's brain could not answer for five minutes, so the reading stopped. "
                        "Start reading again when your bot is ready." if patient else
                        "Your bot's brain can't answer right now. Try again in a minute.")


class NotYet(Exception):
    """A job in the queue did not finish in the time the caller would wait."""


class Job:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.started = False
        self.cancelled = False

    def run(self) -> None:
        try:
            self.result = self.fn()
        except Exception as exc:  # noqa: BLE001 - handed to whoever waits on the job
            self.error = exc
        finally:
            self.done.set()

    def wait(self, timeout: float | None = None) -> Any:
        if not self.done.wait(timeout):
            raise NotYet()
        if self.error is not None:
            raise self.error
        return self.result


class BrainQueue:
    """The one line of brain calls for this server process: one job at a time, in order. Jobs the owner is waiting
    on (a view, a question) go ahead of the reading's next call; a call already running always finishes first."""

    FRONT, READING = 0, 1

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.jobs: list[tuple[int, int, Job]] = []
        self.seq = itertools.count()
        threading.Thread(target=self._work, daemon=True, name="brain queue").start()

    def submit(self, fn: Callable[[], Any], rank: int = FRONT) -> Job:
        job = Job(fn)
        with self.cv:
            heapq.heappush(self.jobs, (rank, next(self.seq), job))
            self.cv.notify()
        return job

    def cancel(self, job: Job) -> bool:
        """True when the job had not started, so it never will."""
        with self.cv:
            if job.started:
                return False
            job.cancelled = True
            return True

    def _work(self) -> None:
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                _rank, _seq, job = heapq.heappop(self.jobs)
                if job.cancelled:
                    continue
                job.started = True
            job.run()


class Settled:
    """A brain whose every ask is told what the writer has settled, so what it writes agrees with her."""

    def __init__(self, brain: Any, lines: Callable[[], list[str]]) -> None:
        self.brain, self.lines = brain, lines
        self.missed = 0                 # asks that came back with nothing usable, even after the repair call

    def chat(self, messages: list[dict], patient: bool = True) -> tuple[str, str]:
        said = self.lines()
        if said:
            head = ("The writer has corrected these facts. They are settled: treat them as true whatever any other "
                    "note here says, and never bring back what she removed or changed:\n- " + "\n- ".join(said))
            messages = [{**messages[0], "content": f"{messages[0]['content']}\n\n{head}"}] + messages[1:]
        return self.brain.chat(messages, patient)


class QueuedBrain:
    """The reading's brain: each of its calls waits its turn in the queue, behind the owner's own asks."""

    def __init__(self, brain: Brain, queue: BrainQueue) -> None:
        self.brain, self.queue = brain, queue

    def chat(self, messages: list[dict], patient: bool = True) -> tuple[str, str]:
        return self.queue.submit(lambda: self.brain.chat(messages, patient), BrainQueue.READING).wait()


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    if "</think>" in text.lower():                     # an opening tag the template already sent
        text = text[text.lower().rindex("</think>") + len("</think>"):]
    return text.strip()


def parse_json(text: str) -> Any:
    """The first JSON object or list in the answer, or None."""
    s = strip_think(text)
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(s)
    except ValueError:
        pass
    for a, b in (("{", "}"), ("[", "]")):
        i, j = s.find(a), s.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except ValueError:
                continue
    return None


def ask_json(brain: Brain, system: str, user: str, ok: Callable[[Any], bool], patient: bool = True
             ) -> tuple[Any, str]:
    """(answer, model): one ask, and one repair call when the answer is not usable JSON. (None, model) after that."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    text, model = brain.chat(messages, patient)
    got = parse_json(text)
    if got is not None and ok(got):
        return got, model
    messages += [{"role": "assistant", "content": strip_think(text)[:6000]}, {"role": "user", "content": REPAIR}]
    text, model2 = brain.chat(messages, patient)
    got = parse_json(text)
    if (got is None or not ok(got)) and isinstance(brain, Settled):
        brain.missed += 1
    return (got if got is not None and ok(got) else None), (model2 or model)


# -- small cleaners ----------------------------------------------------------------------------------

def _t(v: Any, n: int = NOTE_MAX) -> str:
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v if x)
    elif isinstance(v, dict) or v is None:
        v = ""
    return " ".join(str(v).split())[:n]


def _names(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v] if v.strip() else []
    return [_t(x, 120) for x in v if _t(x, 120)] if isinstance(v, list) else []


def _dicts(v: Any) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _ints(v: Any) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, bool):
            continue
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str) and re.search(r"\d+", x):
            out.append(int(re.search(r"\d+", x).group()))
    return sorted(set(out))


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s'-]", " ", name.lower()).split())


def _first_name(key: str) -> str:
    parts = key.split()
    return "" if not parts or parts[0].rstrip(".") in TITLES else parts[0]


def _nobody(name: str) -> bool:
    """A pronoun is not a person: facts under "he" or "she" have no named owner."""
    return _norm(name) in PRONOUNS


def _generic(alias: str) -> bool:
    """A description, not a name: "she", "the doctor", "her brother", "doctor". Many people can be called that."""
    key = _norm(alias)
    return (not key or key in PRONOUNS or key in TITLES or key.split()[0] in SOMEONE
            or not str(alias).strip()[:1].isupper())


def _other_surname(a: str, b: str) -> bool:
    """Two full names that end differently: "Tom Hale" and "Tom Ross" are two people."""
    x, y = a.split(), b.split()
    return len(x) > 1 and len(y) > 1 and x[-1] != y[-1]


# -- extraction ---------------------------------------------------------------------------------------

EXTRACT_SYSTEM = (
    "You read one section of a writer's manuscript and note the facts in it, for her story bible. Answer with one "
    "JSON object only, no other words, in this shape:\n"
    '{"characters":[{"name":"","aliases":[],"role":"","traits":[],"wants":"","fears":"","beat":"","parents":[],'
    '"children":[],"partners":[]}],"relationships":[{"a":"","b":"","kind":"","note":""}],'
    '"places":[{"name":"","kind":"","notes":"","where":""}],"events":[{"what":"","who":[],"where":"","when":"",'
    '"placement":"now, earlier or later"}],'
    '"threads":[{"name":"","kind":"main plot or subplot","setup":"","payoff":""}],'
    '"themes":[{"name":"","quote":""}],"world_rules":[""],'
    '"science":[{"claim":"","note":"","plausible":"yes, no or unclear"}],'
    '"continuity":[{"kind":"mismatch, gap or question","note":""}],'
    '"editing":[{"kind":"pacing, point of view, clarity, dialogue or repetition","note":""}],'
    '"setting":{"world":"","place":"","era":"","technology":""},"history":[{"when":"","what":""}],"summary":""}\n'
    "Only what this section says. When a person or place is one of the known names, use that name; a new person gets "
    "their own entry, even when the name is close to a known one. name is a real name or how the section calls them; "
    "never he, she or they. beat is what the "
    "character does or learns here, one short sentence. parents, children and partners are names. placement says "
    "whether an event happens in the story's present, earlier (a flashback) or later. where is the country, region "
    "or planet a place is in, when the section says. setting is any clue to the world, the place in it, the era "
    "and the technology. history is the world's past as this section tells it. A quote is copied exactly and is under 200 characters. summary is two sentences. Leave a list empty when there is nothing. Keep "
    "every note short."
)
LOOKS_RULES = (
    "looks are how a character looks and the telling physical details the section gives (hair, height, a scar, "
    "what they always wear), one detail to an entry. age_words are the section's own words on the character's age. "
    "details are what a place looks like, is made of, sounds and smells like, its weather and light, one to an "
    "entry. geography is distances, travel times and directions between named places. setting_look is what the era "
    "and its technology look like (lamps, carts, domes, uniforms). Put a detail under a person only when the section "
    "makes plain it is that person; when it says only he or she and you can't tell who, leave it out. Only what the "
    "text says, nothing guessed. Each of "
    "these quotes is copied exactly from the section and is under 120 characters."
)
EXTRACT_LOOKS_SYSTEM = (                 # the facts ask with the evidence for pictures, for a section it fits
    EXTRACT_SYSTEM.replace('"partners":[]}]', '"partners":[],"looks":[{"detail":"","quote":""}],"age_words":""}]')
    .replace('"notes":"","where":""}]', '"notes":"","where":"","details":[{"detail":"","quote":""}]}]')
    .replace('"summary":""}\n', '"geography":[{"from":"","to":"","note":"","quote":""}],'
                                 '"setting_look":[{"detail":""}],"summary":""}\n')
    + " " + LOOKS_RULES
)
LOOKS_SYSTEM = (
    "You read one section of a writer's manuscript and note only how its people and places look, for the pictures "
    "her maps draw. Answer with one JSON object only, no other words, in this shape:\n"
    '{"characters":[{"name":"","looks":[{"detail":"","quote":""}],"age_words":""}],'
    '"places":[{"name":"","details":[{"detail":"","quote":""}]}],'
    '"geography":[{"from":"","to":"","note":"","quote":""}],"setting_look":[{"detail":""}]}\n'
    "When a person or place is one of the known names, use that name. " + LOOKS_RULES +
    " Leave a list empty when there is nothing."
)


def split_words(text: str, most: int = PART_WORDS) -> list[str]:
    """Pieces of at most *most* words, at paragraph boundaries; a paragraph longer than that is cut by words. Words are
    counted without their italic and bold marks (marks.py); the pieces keep the marks."""
    paras: list[str] = []
    for p in re.split(r"\n\s*\n", text):
        w = p.split()
        while len(w) > most:
            paras.append(" ".join(w[:most]))
            w = w[most:]
        if w:
            paras.append(p.strip() if len(marks.strip(p).split()) <= most else " ".join(w))
    pieces, cur, n = [], [], 0
    for p in paras:
        k = len(marks.strip(p).split())
        if cur and n + k > most:
            pieces.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(p)
        n += k
    if cur:
        pieces.append("\n\n".join(cur))
    return pieces or [""]


def _ok_extraction(got: Any) -> bool:
    return isinstance(got, dict) and any(k in got for k in ("characters", "events", "summary", "places"))


def known_line(bible: dict, room: int = 250) -> str:
    names = []
    for c in bible.get("characters") or []:
        names.append(c["name"] + (f" ({', '.join(c['aliases'])})" if c.get("aliases") else ""))
    names += [p["name"] for p in bible.get("places") or []]
    out, used = [], 0
    for nm in names:
        used += tokens(nm)
        if used > room:
            break
        out.append(nm)
    return "; ".join(out) or "none yet"


def looks_fit(text: str, bible: dict) -> bool:
    """True when the facts ask can carry the evidence for pictures too and stay under the budget in one piece."""
    return tokens(text) <= BUDGET - tokens(EXTRACT_LOOKS_SYSTEM) - tokens(known_line(bible)) - 100


def extract(brain: Brain, n: int, heading: str, text: str, bible: dict, with_looks: bool = False
            ) -> tuple[dict | None, str]:
    """One section's facts, or None when the brain gave nothing usable. A section over the budget (one made by
    0.1.0, before parts) is read in pieces and the pieces' facts put together. ``with_looks``: the ask also asks for
    the evidence for pictures (only when ``looks_fit``)."""
    known = known_line(bible)
    system = EXTRACT_LOOKS_SYSTEM if with_looks else EXTRACT_SYSTEM
    room = BUDGET - tokens(system) - tokens(known) - 100
    pieces = [text] if tokens(text) <= room else split_words(text, max(200, int(room / 1.4 * 0.9)))
    joined: dict = {}
    model = ""
    for i, piece in enumerate(pieces):
        part = f" (part {i + 1} of {len(pieces)})" if len(pieces) > 1 else ""
        user = f"Known names: {known}\n\nSection {n}{part}: {heading}\n\n{piece}"
        got, model = ask_json(brain, system, user, _ok_extraction)
        if got is None:
            return None, model
        for k, v in got.items():
            if k == "summary":
                joined["summary"] = (joined.get("summary", "") + " " + _t(v, 600)).strip()
            elif isinstance(v, list):
                joined.setdefault(k, []).extend(v)
    return joined, model


def _ok_looks(got: Any) -> bool:
    return isinstance(got, dict) and any(k in got for k in ("characters", "places", "geography", "setting_look"))


def read_looks(brain: Brain, n: int, heading: str, text: str, bible: dict) -> tuple[dict | None, str]:
    """The small ask, looks and details only, for a section whose facts ask could not carry them (or was cached
    before 0.3.3). None when the brain gave nothing usable."""
    known = known_line(bible)
    room = BUDGET - tokens(LOOKS_SYSTEM) - tokens(known) - 100
    pieces = [text] if tokens(text) <= room else split_words(text, max(200, int(room / 1.4 * 0.9)))
    joined: dict = {}
    model = ""
    for i, piece in enumerate(pieces):
        part = f" (part {i + 1} of {len(pieces)})" if len(pieces) > 1 else ""
        user = f"Known names: {known}\n\nSection {n}{part}: {heading}\n\n{piece}"
        got, model = ask_json(brain, LOOKS_SYSTEM, user, _ok_looks)
        if got is None:
            return None, model
        for k, v in got.items():
            if isinstance(v, list):
                joined.setdefault(k, []).extend(v)
    return joined, model


def _evidence(v: Any) -> list[dict]:
    """[{detail, quote}] from what the model gave: dicts or plain strings, empty details dropped."""
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict):
            detail, quote = _t(x.get("detail") or x.get("text"), 200), _t(x.get("quote"), LOOK_QUOTE)
        else:
            detail, quote = _t(x, 200), ""
        if detail:
            out.append({"detail": detail, "quote": quote})
    return out


def _keep(into: list[dict], new: list[dict], n: int, most: int) -> None:
    """Adds each new detail with section n, unless the same detail is already there, up to *most*."""
    seen = {_norm(x["detail"]) for x in into}
    for e in new:
        if len(into) >= most:
            return
        key = _norm(e["detail"])
        if key and key not in seen:
            seen.add(key)
            into.append({"detail": e["detail"], "section": n, "quote": e["quote"]})


CRAFT_SYSTEM = (
    "You read one section of a writer's manuscript and note how it is told, not what happens. Answer with one JSON "
    "object only, no other words, in this shape:\n"
    '{"pov":"","tense":"past or present","scenes":[{"purpose":"","pov":"","tense":"","starts":""}],'
    '"slips":[""],"tics":[{"phrase":""}],"dialogue_habits":[{"character":"","habit":""}]}\n'
    "pov is the point of view and whose it is, like \"third person, Jennifer\" or \"first person, Tom\". A scene is "
    "a stretch in one place and time; purpose is what the scene does for the story, one short sentence; starts is "
    "the scene's first five words, copied exactly. slips are places where the point of view or the tense slips, "
    "each a short note. tics are words or short phrases the writing repeats more than it needs to. dialogue_habits "
    "are how a character talks. Leave a list empty when there is nothing."
)
QUOTED = re.compile(r'"[^"\n]{1,2000}"|“[^”\n]{1,2000}”')


def _ok_craft(got: Any) -> bool:
    return isinstance(got, dict) and any(k in got for k in ("scenes", "pov", "tense"))


def craft(brain: Brain, n: int, heading: str, text: str) -> tuple[dict | None, str]:
    """How one section is told, or None when the brain gave nothing usable."""
    room = BUDGET - tokens(CRAFT_SYSTEM) - 100
    pieces = [text] if tokens(text) <= room else split_words(text, max(200, int(room / 1.4 * 0.9)))
    joined: dict = {}
    model = ""
    for i, piece in enumerate(pieces):
        part = f" (part {i + 1} of {len(pieces)})" if len(pieces) > 1 else ""
        got, model = ask_json(brain, CRAFT_SYSTEM, f"Section {n}{part}: {heading}\n\n{piece}", _ok_craft)
        if got is None:
            return None, model
        for k, v in got.items():
            if isinstance(v, list):
                joined.setdefault(k, []).extend(v)
            elif k in ("pov", "tense") and not joined.get(k):
                joined[k] = v
    return joined, model


def _tense(v: Any) -> str:
    t = _t(v, 40).lower()
    for word in ("present", "past", "future"):
        if word in t:
            return word
    return t


def dialogue_share(text: str) -> float:
    total = len(text.split())
    said = sum(len(m.group().split()) for m in QUOTED.finditer(text))
    return round(min(1.0, said / total), 2) if total else 0.0


def craft_facts(raw: Any, n: int, text: str) -> dict:
    """The craft answer for section n, with the counting done here: words and dialogue share per scene (scenes cut at
    the words the model gave as each one's start, else in equal parts), and how often each tic really comes."""
    raw = raw if isinstance(raw, dict) else {}
    pov, tense = _t(raw.get("pov"), 80), _tense(raw.get("tense"))
    scenes = _dicts(raw.get("scenes"))[:20] or [{}]
    low = text.lower()
    cuts: list[int] | None = [0]
    for sc in scenes[1:]:
        start = _t(sc.get("starts"), 120).lower()
        i = low.find(start, cuts[-1] + 1) if start else -1
        if i <= cuts[-1]:
            cuts = None
            break
        cuts.append(i)
    if cuts is None:
        w, k = text.split(), len(scenes)
        spans = [" ".join(w[len(w) * j // k:len(w) * (j + 1) // k]) for j in range(k)]
    else:
        spans = [text[a:b] for a, b in zip(cuts, cuts[1:] + [len(text)])]
    out_scenes = []
    for j, (sc, span) in enumerate(zip(scenes, spans), 1):
        out_scenes.append({"section": n, "n": j, "purpose": _t(sc.get("purpose")),
                           "pov": _t(sc.get("pov"), 80) or pov, "tense": _tense(sc.get("tense")) or tense,
                           "words": len(span.split()), "dialogue_share": dialogue_share(span)})
    tics: list[dict] = []
    for t in raw.get("tics") if isinstance(raw.get("tics"), list) else []:
        phrase = _t(t.get("phrase") if isinstance(t, dict) else t, 60)
        if phrase and phrase.lower() not in {x["phrase"].lower() for x in tics}:
            count = len(re.findall(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", low))
            if count:
                tics.append({"phrase": phrase, "count": count})
    slips = []
    for x in raw.get("slips") if isinstance(raw.get("slips"), list) else []:
        note = _t(x.get("note") if isinstance(x, dict) else x)
        if note:
            slips.append(note)
    habits = [{"character": _t(h.get("character"), 120), "habit": _t(h.get("habit"))}
              for h in _dicts(raw.get("dialogue_habits")) if _t(h.get("character"), 120) and _t(h.get("habit"))]
    return {"pov": pov, "tense": tense, "slips": slips[:10], "scenes": out_scenes, "tics": tics[:20],
            "habits": habits[:20]}


# -- merge --------------------------------------------------------------------------------------------

def empty_bible(title: str, total: int) -> dict:
    return {"schema": SCHEMA, "title": title, "built_at": now(), "built_with": "",
            "sections_read": 0, "sections_total": total, "sections": [], "characters": [], "places": [],
            "events": [], "threads": [], "themes": [], "world_rules": [], "science": [], "continuity": [],
            "editing": [], "scenes": [], "geography": [],
            "structure": {"acts": [], "turning_points": [], "midpoint": {"section": None, "note": ""},
                          "climax": {"section": None, "note": ""}},
            "voice": {"by_section": [], "tics": [], "dialogue_habits": []},
            "setting": {"world": "", "place_in_world": "", "era": "", "technology": "", "one_paragraph": "",
                        "look": []},
            "history": [], "cast_summary": "", "briefs": {k: "" for k in BRIEF_TABS},
            "summary": {"one_line": "", "paragraph": "", "by_section": []}}


class _People:
    def __init__(self) -> None:
        self.people: list[dict] = []

    def keys(self, p: dict) -> set:
        return {_norm(p["name"])} | {_norm(a) for a in p["aliases"] if not _generic(a)}

    def find(self, name: str, aliases: list[str] = ()) -> dict | None:
        """The name itself first, in anyone; then a real alias, unless the two full names say two people; then a first
        name exactly one person has, and no other surname."""
        key = _norm(name)
        for p in self.people:
            if key and key in self.keys(p):
                return p
        want = {_norm(a) for a in aliases if not _generic(a)}
        for p in self.people:
            if want & self.keys(p) and not _other_surname(key, _norm(p["name"])):
                return p
        first = _first_name(key)
        if first:
            hits = [p for p in self.people if not _other_surname(key, _norm(p["name"]))
                    and any(_first_name(k) == first and not _other_surname(key, k) for k in self.keys(p))]
            if len(hits) == 1:
                return hits[0]
        return None

    def add(self, name: str, aliases: list[str]) -> dict:
        p = self.find(name, aliases)
        if p is None:
            p = {"name": name, "aliases": [], "role": "", "traits": [], "wants": "", "fears": "",
                 "appearances": [], "arc": [], "rel": [], "parents": [], "children": [], "partners": [],
                 "looks": [], "age_words": None}
            self.people.append(p)
        elif len(name.split()) > len(p["name"].split()) and _first_name(_norm(name)) == _first_name(_norm(p["name"])):
            p["aliases"].append(p["name"])               # "Jennifer" then "Jennifer Hale": the fuller name leads
            p["name"] = name
        for a in aliases + ([name] if name != p["name"] else []):
            if not _nobody(a) and _norm(a) != _norm(p["name"]) and _norm(a) not in {_norm(x) for x in p["aliases"]}:
                p["aliases"].append(a)
        return p


def merge(title: str, order: list[tuple[int, str]], found: dict[int, dict], crafts: dict[int, dict] | None = None,
          words: dict[int, int] | None = None, looks: dict[int, dict] | None = None) -> dict:
    """The bible from each section's facts, the same every time for the same facts. ``order``: (n, heading);
    ``crafts``: each section's ``craft_facts``; ``words``: each section's word count; ``looks``: each section's
    answer to the small looks ask, when it had one."""
    crafts = crafts or {}
    bible = empty_bible(title, len(order))
    bible["sections"] = [{"n": n, "heading": h, "words": int((words or {}).get(n) or 0)} for n, h in order]
    people = _People()
    places: dict[str, dict] = {}
    events, threads, themes, rules = [], {}, {}, {}
    history: dict[str, dict] = {}
    clues: dict[str, Counter] = {}
    geography: list[dict] = []
    set_look: list[dict] = []
    seen_geo: set = set()

    def new_place(name: str) -> dict:
        return {"name": name, "kind": "", "notes": "", "where": "", "appearances": [], "details": []}

    def take_looks(src: Any, n: int) -> None:
        """The evidence for pictures in one answer, every entry given section n whatever the model wrote."""
        if not isinstance(src, dict):
            return
        for c in _dicts(src.get("characters")):
            name = _t(c.get("name"), 120)
            age = c.get("age_words")
            age = _t((age.get("words") or age.get("text")) if isinstance(age, dict) else age, 120)
            got = _evidence(c.get("looks"))
            if not name or _nobody(name) or not (got or age):
                continue
            p = people.add(name, _names(c.get("aliases")))
            if n not in p["appearances"]:
                p["appearances"].append(n)
            _keep(p["looks"], got, n, LOOKS_MAX)
            if age and not p["age_words"]:
                p["age_words"] = {"words": age, "section": n}
        for pl in _dicts(src.get("places")):
            name, got = _t(pl.get("name"), 120), _evidence(pl.get("details"))
            if not name or not got:
                continue
            place = places.setdefault(_norm(name), new_place(name))
            if n not in place["appearances"]:
                place["appearances"].append(n)
            _keep(place["details"], got, n, DETAILS_MAX)
        for g in _dicts(src.get("geography")):
            note = _t(g.get("note"))
            ends = [_t(g.get(k), 120) for k in ("from", "to")]
            ends = [places[_norm(x)]["name"] if _norm(x) in places else x for x in ends]
            key = (_norm(ends[0]), _norm(ends[1]), _norm(note))
            if note and key not in seen_geo:
                seen_geo.add(key)
                geography.append({"from": ends[0], "to": ends[1], "note": note, "section": n,
                                  "quote": _t(g.get("quote"), LOOK_QUOTE)})
        for s in src.get("setting_look") if isinstance(src.get("setting_look"), list) else []:
            detail = _t(s.get("detail") if isinstance(s, dict) else s, 200)
            if detail and _norm(detail) not in {_norm(x["detail"]) for x in set_look}:
                set_look.append({"detail": detail, "section": n})

    for n, _head in order:
        ext = found.get(n)
        if not isinstance(ext, dict):
            continue
        for c in _dicts(ext.get("characters")):
            name = _t(c.get("name"), 120)
            if not name or _nobody(name):
                continue
            p = people.add(name, _names(c.get("aliases")))
            p["role"] = p["role"] or _t(c.get("role"), 60)
            p["wants"] = p["wants"] or _t(c.get("wants"))
            p["fears"] = p["fears"] or _t(c.get("fears"))
            for t in _names(c.get("traits")):
                if t.lower() not in {x.lower() for x in p["traits"]} and len(p["traits"]) < 10:
                    p["traits"].append(t)
            if n not in p["appearances"]:
                p["appearances"].append(n)
            beat = _t(c.get("beat"))
            if beat:
                p["arc"].append({"section": n, "beat": beat})
            for k in ("parents", "children", "partners"):
                p[k] += [x for x in _names(c.get(k)) if x not in p[k]]
        for r in _dicts(ext.get("relationships")):
            a, b = _t(r.get("a"), 120), _t(r.get("b"), 120)
            pa = people.find(a) if a else None
            if pa is not None and b:
                pa["rel"].append((b, _t(r.get("kind"), 60), _t(r.get("note")), n))
        for pl in _dicts(ext.get("places")):
            name = _t(pl.get("name"), 120)
            if not name:
                continue
            key = _norm(name)
            got = places.setdefault(key, new_place(name))
            got["where"] = got["where"] or _t(pl.get("where"), 120)
            got["kind"] = got["kind"] or _t(pl.get("kind"), 60)
            got["notes"] = got["notes"] or _t(pl.get("notes"))
            if n not in got["appearances"]:
                got["appearances"].append(n)
        for i, e in enumerate(_dicts(ext.get("events")), 1):
            if _t(e.get("what")):
                said = _t(e.get("placement"), 40).lower()
                pl = next((w for w in ("earlier", "later", "now") if w in said), "")
                pl = pl or ("earlier" if "flashback" in said else "")
                events.append({"section": n, "order": i, "what": _t(e.get("what")), "who": _names(e.get("who")),
                               "where": _t(e.get("where"), 120), "when": _t(e.get("when"), 120), "placement": pl})
        for t in _dicts(ext.get("threads")):
            name = _t(t.get("name"), 120)
            if not name:
                continue
            th = threads.setdefault(slug(name), {"name": name, "kind": "", "sections": [], "setups": [], "payoffs": []})
            kind = _t(t.get("kind"), 40).lower()
            th["kind"] = th["kind"] or ("main plot" if "main" in kind else "subplot")
            if n not in th["sections"]:
                th["sections"].append(n)
            if _t(t.get("setup")):
                th["setups"].append(f"section {n}: {_t(t.get('setup'))}")
            if _t(t.get("payoff")):
                th["payoffs"].append(f"section {n}: {_t(t.get('payoff'))}")
        for t in _dicts(ext.get("themes")):
            name = _t(t.get("name"), 120)
            if name:
                th = themes.setdefault(_norm(name), {"name": name, "evidence": []})
                q = _t(t.get("quote"), QUOTE_MAX)
                if q:
                    th["evidence"].append({"section": n, "quote": q})
        for r in ext.get("world_rules") or []:
            rule = _t(r.get("rule") if isinstance(r, dict) else r)
            if rule:
                got = rules.setdefault(_norm(rule), {"rule": rule, "sections": []})
                if n not in got["sections"]:
                    got["sections"].append(n)
        for s in _dicts(ext.get("science")):
            if _t(s.get("claim")):
                pl = _t(s.get("plausible"), 20).lower()
                bible["science"].append({"claim": _t(s.get("claim")), "section": n, "note": _t(s.get("note")),
                                         "plausible": pl if pl in ("yes", "no", "unclear") else "unclear"})
        for c in _dicts(ext.get("continuity")):
            if _t(c.get("note")):
                kind = _t(c.get("kind"), 20).lower()
                bible["continuity"].append({"kind": kind if kind in ("mismatch", "gap", "question") else "question",
                                            "sections": [n], "note": _t(c.get("note"))})
        for e in _dicts(ext.get("editing")):
            if _t(e.get("note")):
                bible["editing"].append({"section": n, "kind": _t(e.get("kind"), 40).lower() or "clarity",
                                         "note": _t(e.get("note"))})
        if _t(ext.get("summary")):
            bible["summary"]["by_section"].append({"section": n, "summary": _t(ext.get("summary"), 600)})
        for h in _dicts(ext.get("history")):
            what = _t(h.get("what"))
            if what:
                got = history.setdefault(_norm(what), {"when": _t(h.get("when"), 120), "what": what, "sections": []})
                if n not in got["sections"]:
                    got["sections"].append(n)
        clue = ext.get("setting") if isinstance(ext.get("setting"), dict) else {}
        for k in ("world", "place", "era", "technology"):
            if _t(clue.get(k), 200):
                clues.setdefault(k, Counter())[_t(clue.get(k), 200)] += 1
        take_looks(ext, n)                               # the facts ask carried them, when the section fitted
        take_looks((looks or {}).get(n), n)              # else the small looks ask

    # ids, once every name is known
    used: set = set()

    def unique(base: str) -> str:
        sid, k = base, 2
        while sid in used:
            sid, k = f"{base}-{k}", k + 1
        used.add(sid)
        return sid

    for p in people.people:                      # "Jennifer Hale" is "jennifer" while she is the only Jennifer
        first = _first_name(_norm(p["name"]))
        shared = sum(1 for q in people.people if _first_name(_norm(q["name"])) == first)
        p["id"] = unique(slug(first if first and shared == 1 else p["name"]))

    def pid(name: str) -> str:
        p = people.find(name)
        return p["id"] if p else slug(name)

    for p in people.people:
        rel, seen = [], set()
        for b, kind, note, _n in p["rel"]:
            to = pid(b)
            if to != p["id"] and (to, kind) not in seen:
                seen.add((to, kind))
                rel.append({"to": to, "kind": kind, "note": note})
        bible["characters"].append({
            "id": p["id"], "name": p["name"], "aliases": p["aliases"], "role": p["role"], "traits": p["traits"],
            "wants": p["wants"], "fears": p["fears"], "appearances": sorted(p["appearances"]),
            "first_seen": min(p["appearances"]) if p["appearances"] else None, "arc": p["arc"],
            "relationships": rel,
            "family": {k: sorted({pid(x) for x in p[k]} - {p["id"]}) for k in ("parents", "children", "partners")},
            "importance": importance_of(p["role"], len(p["appearances"]), len(order)), "personality": "",
            "write_up": "", "looks": p["looks"], **({"age_words": p["age_words"]} if p["age_words"] else {})})
    for c in bible["characters"]:
        if c["importance"] == "minor":
            c["write_up"] = minor_write_up(c)
    bible["setting"] = {"world": "", "place_in_world": "", "era": "", "technology": "", "one_paragraph": ""}
    for k, key in (("world", "world"), ("place", "place_in_world"), ("era", "era"), ("technology", "technology")):
        if clues.get(k):
            bible["setting"][key] = clues[k].most_common(1)[0][0]
    bible["setting"]["look"] = set_look
    bible["geography"] = geography
    bible["history"] = list(history.values())
    place_ids = {}
    for key, pl in places.items():
        place_ids[key] = unique(slug(pl["name"]))
        bible["places"].append({"id": place_ids[key], **pl, "appearances": sorted(pl["appearances"])})
    events.sort(key=lambda e: (e["section"], e["order"]))
    for i, e in enumerate(events, 1):
        where = place_ids.get(_norm(e["where"]), slug(e["where"]) if e["where"] else "")
        bible["events"].append({"id": f"e{i}", "section": e["section"], "order": e["order"], "story_order": i,
                                "what": e["what"], "who": [pid(w) for w in e["who"]], "where": where,
                                "when": e["when"], "placement": e["placement"]})
    for tid, th in threads.items():
        bible["threads"].append({"id": tid, **th, "open": not th["payoffs"]})
    bible["themes"] = list(themes.values())
    bible["world_rules"] = list(rules.values())

    # how it is told: the scenes, and the voice put together by counting
    tenses = Counter(crafts[n]["tense"] for n, _h in order if n in crafts and crafts[n]["tense"])
    main = tenses.most_common(1)[0][0] if len(tenses) > 1 else ""
    tics: dict[str, dict] = {}
    habits, seen_habits = [], set()
    for n, _h in order:
        c = crafts.get(n)
        if not c:
            continue
        bible["scenes"] += c["scenes"]
        slips = list(c["slips"])
        if main and c["tense"] and c["tense"] != main:
            slips.append(f"Told in the {c['tense']} tense; most of the book is in the {main} tense.")
        bible["voice"]["by_section"].append({"section": n, "pov": c["pov"], "tense": c["tense"], "slips": slips})
        for t in c["tics"]:
            got = tics.setdefault(t["phrase"].lower(), {"phrase": t["phrase"], "count": 0, "sections": []})
            got["count"] += t["count"]
            if n not in got["sections"]:
                got["sections"].append(n)
        for h in c["habits"]:
            p = people.find(h["character"])
            who = p["name"] if p else h["character"]
            if (who.lower(), h["habit"].lower()) not in seen_habits:
                seen_habits.add((who.lower(), h["habit"].lower()))
                habits.append({"character": who, "habit": h["habit"]})
    bible["voice"]["tics"] = sorted((t for t in tics.values() if t["count"] >= 2), key=lambda t: -t["count"])[:30]
    bible["voice"]["dialogue_habits"] = habits
    return bible


def importance_of(role: str, appearances: int, total: int) -> str:
    """main: a lead by role, or in at least three sections and two in five; supporting: in two or more; else minor."""
    if any(r in role.lower() for r in MAIN_ROLES) or appearances >= max(3, -(-2 * total // 5)):
        return "main"
    return "supporting" if appearances >= 2 else "minor"


def minor_write_up(c: dict) -> str:
    """One line for a minor character, from the facts alone (no call to the brain)."""
    secs = c.get("appearances") or []
    where = f"section {secs[0]}" if len(secs) == 1 else f"sections {', '.join(map(str, secs))}" if secs else ""
    beat = next((b.get("beat") for b in c.get("arc") or [] if b.get("beat")), "")
    line = str(c.get("name") or "") + (f", {c['role']}," if c.get("role") else "") + (f" is in {where}" if where else "")
    return (line + (f": {beat}" if beat else ".")).strip()


def timeline_conflicts(bible: dict) -> list[dict]:
    """Story order against telling order: an event told right after one that, in the story's own time, it comes
    before."""
    ev = [e for e in bible.get("events") or [] if isinstance(e, dict) and isinstance(e.get("story_order"), int)]
    out = []
    for p, e in zip(ev, ev[1:]):
        if e["story_order"] < p["story_order"]:
            out.append({"event": e["id"], "after": p["id"], "sections": sorted({p["section"], e["section"]}),
                        "flashback": e.get("placement") == "earlier",
                        "note": f"\"{e['what']}\" (section {e['section']}) happens before \"{p['what']}\" "
                                f"(section {p['section']}), but it is told after it."})
    return out


# -- synthesis ----------------------------------------------------------------------------------------

ARC_SYSTEM = (
    "You help a writer see one character's arc across her manuscript, from the facts her bot noted. Answer with one "
    'JSON object only: {"arc":[{"section":1,"beat":""}],"wants":"","fears":""}. One beat for each turning point, '
    "in section order, each one short sentence. Use only the facts given."
)
THREADS_SYSTEM = (
    "You help a writer check the plot of her manuscript, from the facts her bot noted, section by section. Answer "
    'with one JSON object only: {"threads":[{"name":"","kind":"main plot or subplot","sections":[1],'
    '"setups":["section 1: ..."],"payoffs":["section 9: ..."],"open":true}],'
    '"continuity":[{"kind":"mismatch, gap or question","sections":[2,8],"note":""}]}. A thread is open when it has '
    "no payoff yet. Continuity notes are facts that disagree between sections (looks, ages, dates, names, distances), "
    "gaps in time, and questions a reader would ask. Use only the facts given. Keep notes short."
)
THEMES_SYSTEM = (
    "You help a writer see the themes of her manuscript, from the facts her bot noted. Answer with one JSON object "
    'only: {"themes":[{"name":"","evidence":[{"section":1,"quote":""}]}],"one_line":"","paragraph":""}. Join themes '
    "that are the same idea. A quote must be one of the quotes given, copied exactly. one_line is the story in one "
    "sentence, paragraph in four or five. Use only the facts given."
)


def fit(lines: list[str], room: int) -> str:
    out, used = [], 0
    for ln in lines:
        used += tokens(ln)
        if used > room:
            break
        out.append(ln)
    return "\n".join(out)


def _summaries(bible: dict, only: set | None = None) -> list[str]:
    return [f"Section {s['section']}: {s['summary']}" for s in bible["summary"]["by_section"]
            if only is None or s["section"] in only]


def synth_arc(brain: Brain, bible: dict, c: dict) -> str:
    room = BUDGET - tokens(ARC_SYSTEM) - 200
    head = [f"Character: {c['name']}" + (f" (also {', '.join(c['aliases'])})" if c["aliases"] else ""),
            f"Role: {c['role']}. Traits: {', '.join(c['traits'])}. Wants: {c['wants']}. Fears: {c['fears']}.",
            "Relationships: " + "; ".join(f"{r['kind']} of {r['to']}" for r in c["relationships"])]
    beats = [f"Section {b['section']}: {b['beat']}" for b in c["arc"]]
    user = fit(head + ["Beats:"] + beats + ["Sections she or he is in:"] + _summaries(bible, set(c["appearances"])),
               room)
    got, model = ask_json(brain, ARC_SYSTEM, user, lambda g: isinstance(g, dict) and isinstance(g.get("arc"), list))
    if got is None:
        return model
    arc = [{"section": _ints(b.get("section"))[0], "beat": _t(b.get("beat"))}
           for b in _dicts(got.get("arc")) if _ints(b.get("section")) and _t(b.get("beat"))]
    if arc:
        c["arc"] = sorted(arc, key=lambda b: b["section"])
    c["wants"] = _t(got.get("wants")) or c["wants"]
    c["fears"] = _t(got.get("fears")) or c["fears"]
    return model


def synth_threads(brain: Brain, bible: dict) -> str:
    room = BUDGET - tokens(THREADS_SYSTEM) - 200
    names = {c["id"]: c["name"] for c in bible["characters"]}
    lines = ["Threads so far:"] + [f"{t['name']} ({t['kind']}); sections {t['sections']}; setups: "
                                   f"{'; '.join(t['setups'])}; payoffs: {'; '.join(t['payoffs'])}"
                                   for t in bible["threads"]]
    lines += ["Notes so far:"] + [f"{c['kind']} in section {c['sections']}: {c['note']}" for c in bible["continuity"]]
    lines += ["People:"] + [f"{c['name']}: {', '.join(c['traits'])}" for c in bible["characters"]]
    lines += ["Events in order:"] + [f"Section {e['section']}: {e['what']} (who: "
                                     f"{', '.join(names.get(w, w) for w in e['who'])}; when: {e['when']})"
                                     for e in bible["events"]]
    got, model = ask_json(brain, THREADS_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and ("threads" in g or "continuity" in g))
    if got is None:
        return model
    threads = []
    for t in _dicts(got.get("threads")):
        name = _t(t.get("name"), 120)
        if name:
            pays = [_t(x) for x in t.get("payoffs") or [] if _t(x)] if isinstance(t.get("payoffs"), list) else []
            kind = _t(t.get("kind"), 40).lower()
            threads.append({"id": slug(name), "name": name, "kind": "main plot" if "main" in kind else "subplot",
                            "sections": _ints(t.get("sections")),
                            "setups": [_t(x) for x in t.get("setups") or [] if _t(x)]
                            if isinstance(t.get("setups"), list) else [],
                            "payoffs": pays, "open": bool(t.get("open")) if "open" in t else not pays})
    if threads:
        bible["threads"] = threads
    flags = []
    for c in _dicts(got.get("continuity")):
        if _t(c.get("note")):
            kind = _t(c.get("kind"), 20).lower()
            flags.append({"kind": kind if kind in ("mismatch", "gap", "question") else "question",
                          "sections": _ints(c.get("sections")), "note": _t(c.get("note"))})
    have = {f["note"].lower() for f in flags}
    bible["continuity"] = flags + [c for c in bible["continuity"] if c["note"].lower() not in have]
    return model


def synth_themes(brain: Brain, bible: dict) -> str:
    room = BUDGET - tokens(THEMES_SYSTEM) - 200
    quotes = {}
    lines = ["Themes and quotes:"]
    for t in bible["themes"]:
        lines.append(f"{t['name']}: " + " | ".join(f"(section {e['section']}) \"{e['quote']}\"" for e in t["evidence"]))
        for e in t["evidence"]:
            quotes[e["quote"].lower()] = e
    lines += ["Sections:"] + _summaries(bible)
    got, model = ask_json(brain, THEMES_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and isinstance(g.get("themes"), list))
    if got is None:
        return model
    themes = []
    for t in _dicts(got.get("themes")):
        name = _t(t.get("name"), 120)
        if name:
            ev = [quotes[_t(e.get("quote"), QUOTE_MAX).lower()] for e in _dicts(t.get("evidence"))
                  if _t(e.get("quote"), QUOTE_MAX).lower() in quotes]      # only quotes that are in the book
            themes.append({"name": name, "evidence": ev})
    if themes:
        bible["themes"] = themes
    bible["summary"]["one_line"] = _t(got.get("one_line"), 300) or bible["summary"]["one_line"]
    bible["summary"]["paragraph"] = _t(got.get("paragraph"), 1500) or bible["summary"]["paragraph"]
    return model


TIMELINE_SYSTEM = (
    "You help a writer see the order in which the events of her manuscript happen in the story's own time, which "
    "may differ from the order they are told in. Answer with one JSON object only: "
    '{"story_order":["e1","e2"]}, the event ids given, earliest first. Use the hints: when it happens, and whether '
    "the telling says it happened earlier (a flashback) or later. When nothing says otherwise, keep the telling order."
)
STRUCTURE_SYSTEM = (
    "You help a writer see the shape of her manuscript, from the facts her bot noted, section by section. Answer "
    'with one JSON object only: {"acts":[{"name":"","from":1,"to":3}],"turning_points":[{"name":"","section":1,'
    '"note":""}],"midpoint":{"section":1,"note":""},"climax":{"section":1,"note":""}}. Acts cover the sections in '
    "order. A turning point is where the story changes direction. Use only the facts given. Keep notes short."
)


def apply_story_order(bible: dict, ids: list[Any]) -> None:
    """The model's story order, as event ids. An event it left out goes right after the event told before it."""
    by = {e["id"]: e for e in bible["events"]}
    seq: list[str] = []
    for x in ids:
        eid = _t(x.get("id") if isinstance(x, dict) else x, 20).lower()
        if eid in by and eid not in seq:
            seq.append(eid)
    told = [e["id"] for e in bible["events"]]
    for k, eid in enumerate(told):
        if eid not in seq:
            prev = next((told[j] for j in range(k - 1, -1, -1) if told[j] in seq), None)
            seq.insert(seq.index(prev) + 1 if prev else 0, eid)
    for pos, eid in enumerate(seq, 1):
        by[eid]["story_order"] = pos


def synth_timeline(brain: Brain, bible: dict) -> str:
    room = BUDGET - tokens(TIMELINE_SYSTEM) - 200
    lines = ["Events in the order they are told:"]
    for e in bible["events"]:
        hint = (f" When: {e['when']}." if e.get("when") else "") + \
               (f" The telling places it {e['placement']}." if e.get("placement") in ("earlier", "later") else "")
        lines.append(f"{e['id']} (section {e['section']}): {e['what']}{hint}")
    got, model = ask_json(brain, TIMELINE_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and isinstance(g.get("story_order"), list))
    if got is not None:
        apply_story_order(bible, got["story_order"])
    return model


def _at(v: Any) -> dict:
    v = v if isinstance(v, dict) else {}
    n = _ints(v.get("section"))
    return {"section": n[0] if n else None, "note": _t(v.get("note"))}


def synth_structure(brain: Brain, bible: dict) -> str:
    room = BUDGET - tokens(STRUCTURE_SYSTEM) - 200
    summaries = {s["section"]: s["summary"] for s in bible["summary"]["by_section"]}
    lines = ["Sections:"] + [f"Section {s['n']}: {s['heading']}, {s['words']} words. {summaries.get(s['n'], '')}"
                             for s in bible["sections"]]
    lines += ["Threads:"] + [f"{t['name']} ({t['kind']}), sections {t['sections']}" for t in bible["threads"]]
    lines += ["Scenes:"] + [f"Section {s['section']}, scene {s['n']}: {s['purpose']}" for s in bible["scenes"]
                            if s.get("purpose")]
    got, model = ask_json(brain, STRUCTURE_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and any(k in g for k in ("acts", "turning_points", "climax")))
    if got is None:
        return model
    acts = []
    for a in _dicts(got.get("acts")):
        lo, hi = _ints(a.get("from")), _ints(a.get("to"))
        if lo:
            acts.append({"name": _t(a.get("name"), 80) or f"Act {len(acts) + 1}", "from": lo[0],
                         "to": hi[0] if hi else lo[0]})
    tps = [{"name": _t(t.get("name"), 120), "section": _ints(t.get("section"))[0], "note": _t(t.get("note"))}
           for t in _dicts(got.get("turning_points")) if _ints(t.get("section")) and _t(t.get("name"), 120)]
    bible["structure"] = {"acts": acts, "turning_points": tps, "midpoint": _at(got.get("midpoint")),
                          "climax": _at(got.get("climax"))}
    return model


SETTING_SYSTEM = (
    "You help a writer see the world of her manuscript, from the clues her bot noted, section by section. Answer "
    'with one JSON object only: {"world":"","place_in_world":"","era":"","technology":"","one_paragraph":"",'
    '"history":[{"when":"","what":"","sections":[1]}]}. world is the kind of world (ours, another planet, an invented '
    "land). place_in_world is the country, region, city or planet. era is when it is set. technology is what people "
    "have and use. one_paragraph says it all in four or five plain sentences. history is the world's past as the "
    "story tells it, earliest first. Use only the clues given."
)
PEOPLE_SYSTEM = (
    "You help a writer see her characters, from the facts her bot noted. Answer with one JSON object only: "
    '{"people":[{"name":"","personality":"","write_up":""}]}, one entry for each character given, by the name '
    "given. personality is two or three sentences on what they are like. write_up is one paragraph: who they are, "
    "what drives them, how they matter to the story and how they change, with section numbers in brackets like "
    "[3] where a claim comes from one place. Use only the facts given. Plain words."
)
BRIEF_SYSTEM = (
    "You write the brief at the top of one tab of a writer's manuscript page: two to four short paragraphs of plain "
    "prose that tell her what the facts on that tab add up to, not a list of them. Put section numbers in brackets "
    "like [3] where a claim comes from one place. Answer with one JSON object only: "
    '{"brief":"","cast_summary":""}. Paragraphs are separated by a blank line. cast_summary is only for the '
    "characters tab: one paragraph on the whole cast and its balance; leave it empty otherwise. Use only the facts "
    "given. Never rewrite her text."
)
BRIEF_ASKS = {
    "characters": "Who the story is about, who drives it, and how the cast is balanced.",
    "plot": "What the main plot is, how the subplots serve it or pull against it, and what is still open.",
    "timeline": "How the story moves through time, where the telling jumps, and whether the order holds together.",
    "structure": "The shape of the story: its acts, its turning points, its midpoint and climax, and its pacing.",
    "world": "The world: where and when it is, how it works, and how its past bears on the story.",
    "science": "Which claims hold, which do not, and what would settle the doubtful ones.",
    "themes": "What the story is about underneath, and what the ending says about it.",
    "voice": "How the story is told: point of view, tense, rhythm, the habits of the prose and of the dialogue.",
}


def plain(text: Any, n: int = 3000) -> str:
    """The model's prose, kept as paragraphs, capped, with em dashes made commas (the owner's rule)."""
    s = str(text or "") if not isinstance(text, (dict, list)) else ""
    s = re.sub(r"\s*[\u2014\u2013]\s*", ", ", s.replace("\r", ""))
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", s)]
    return "\n\n".join(p for p in paras if p)[:n].strip()


def synth_setting(brain: Brain, bible: dict, found: dict[int, dict]) -> str:
    room = BUDGET - tokens(SETTING_SYSTEM) - 200
    lines = ["Clues, by section:"]
    for n in sorted(found):
        clue = found[n].get("setting") if isinstance(found[n].get("setting"), dict) else {}
        said = "; ".join(f"{k}: {_t(clue.get(k), 200)}" for k in ("world", "place", "era", "technology")
                         if _t(clue.get(k), 200))
        if said:
            lines.append(f"Section {n}: {said}")
    lines += ["The past, as told:"] + [f"Section {h['sections']}: {h['when']}: {h['what']}" for h in bible["history"]]
    lines += ["Places:"] + [f"{p['name']} ({p['kind']}{', ' + p['where'] if p.get('where') else ''})"
                            for p in bible["places"]]
    lines += ["World rules:"] + [r["rule"] for r in bible["world_rules"]]
    got, model = ask_json(brain, SETTING_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and any(k in g for k in ("world", "one_paragraph", "era")))
    if got is None:
        return model
    for k in ("world", "place_in_world", "era", "technology"):
        bible["setting"][k] = _t(got.get(k), 200) or bible["setting"][k]
    bible["setting"]["one_paragraph"] = plain(got.get("one_paragraph"), 1500)
    hist = [{"when": _t(h.get("when"), 120), "what": _t(h.get("what")), "sections": _ints(h.get("sections"))}
            for h in _dicts(got.get("history")) if _t(h.get("what"))]
    if hist:
        bible["history"] = hist[:40]
    return model


def _person_lines(bible: dict, c: dict, full: bool) -> list[str]:
    names = {x["id"]: x["name"] for x in bible["characters"]}
    out = [f"Character: {c['name']}" + (f" (also {', '.join(c['aliases'])})" if c["aliases"] else ""),
           f"Role: {c['role']}. Traits: {', '.join(c['traits'])}. Wants: {c['wants']}. Fears: {c['fears']}. "
           f"In sections {c['appearances']}.",
           "Relationships: " + "; ".join(f"{r['kind']} of {names.get(r['to'], r['to'])}" for r in c["relationships"]),
           "Beats: " + " ".join(f"[{b['section']}] {b['beat']}" for b in c["arc"])]
    if full:
        out += _summaries(bible, set(c["appearances"]))
    return out


def synth_people(brain: Brain, bible: dict, ids: list[str]) -> str:
    """Personality and write-up for one main character, or for up to four supporting ones in one call."""
    chars = [c for c in bible["characters"] if c["id"] in ids]
    if not chars:
        return ""
    room = BUDGET - tokens(PEOPLE_SYSTEM) - 300
    lines: list[str] = []
    for c in chars:
        lines += _person_lines(bible, c, full=len(chars) == 1)
    got, model = ask_json(brain, PEOPLE_SYSTEM, fit(lines, room),
                          lambda g: isinstance(g, dict) and isinstance(g.get("people"), list))
    if got is None:
        return model
    by = {_norm(c["name"]): c for c in chars}
    for c in chars:
        for a in c["aliases"]:
            by.setdefault(_norm(a), c)
    for x in _dicts(got.get("people")):
        c = by.get(_norm(_t(x.get("name"), 120))) or (chars[0] if len(chars) == 1 else None)
        if c is not None:
            c["personality"] = plain(x.get("personality"), 800) or c["personality"]
            c["write_up"] = plain(x.get("write_up"), 1500) or c["write_up"]
    return model


def tab_facts(bible: dict, tab: str) -> list[str]:
    """The merged facts one tab's brief is written from (never the text)."""
    all_lines = fact_lines(bible)
    pick = {"characters": ("Character",), "plot": ("Thread", "Event", "Continuity"),
            "timeline": ("Event",), "structure": ("Structure", "Scene"), "world": ("Place", "World rule"),
            "science": ("Science",), "themes": ("Theme",), "voice": ("Voice", "Dialogue")}[tab]
    lines = [ln for ln in all_lines if ln.startswith(pick)]
    if tab == "characters":
        lines += [f"{c['name']} is a {c['importance']} character. {c.get('write_up') or ''}"
                  for c in bible["characters"]]
    elif tab == "timeline":
        order = sorted(bible["events"], key=lambda e: e.get("story_order") or 0)
        lines += ["In story order: " + "; ".join(f"[{e['section']}] {e['what']}" for e in order)]
        lines += [f"Told out of order: {t['note']}" for t in timeline_conflicts(bible)]
    elif tab == "structure":
        lines += [f"Section {s['n']}: {s['words']} words" for s in bible["sections"]]
    elif tab == "world":
        st = bible["setting"]
        lines = [f"Setting: {st['world']}; {st['place_in_world']}; {st['era']}; {st['technology']}. "
                 f"{st['one_paragraph']}"] + [f"History: {h['when']}: {h['what']} {h['sections']}"
                                              for h in bible["history"]] + lines
    return lines + ["Sections:"] + _summaries(bible)


def synth_brief(brain: Brain, bible: dict, tab: str) -> str:
    room = BUDGET - tokens(BRIEF_SYSTEM) - 300
    user = f"Tab: {tab}\nWhat the brief should say: {BRIEF_ASKS[tab]}\n\nFacts:\n" + fit(tab_facts(bible, tab), room)
    got, model = ask_json(brain, BRIEF_SYSTEM, user, lambda g: isinstance(g, dict) and "brief" in g)
    if got is None:
        return model
    bible["briefs"][tab] = plain(got.get("brief"))
    if tab == "characters":
        bible["cast_summary"] = plain(got.get("cast_summary"), 1500) or bible["cast_summary"]
    return model


def carry(before: Any, after: Any, into: dict) -> None:
    """What one synthesis step changed, from the corrected copy it worked on to the bible as read: a field at a time,
    a character or an event by its id, any other list whole."""
    for k, v in after.items():
        if k in before and before[k] == v:
            continue
        if isinstance(v, dict) and isinstance(before.get(k), dict) and isinstance(into.get(k), dict):
            carry(before[k], v, into[k])
        elif k in ("characters", "events") and isinstance(into.get(k), list):
            was = {x.get("id"): x for x in before.get(k) or []}
            mine = {x.get("id"): x for x in into[k]}
            for x in v:
                if x.get("id") in mine and x.get("id") in was:
                    carry(was[x["id"]], x, mine[x["id"]])
        else:
            into[k] = v


# -- the reading thread -------------------------------------------------------------------------------

Loader = Callable[[Path], tuple[str, list[tuple[int, str, str, str]]]]


def read_of(title: str, secs: list[tuple[int, str, str, str]], as_written: Any, settled: list[str]) -> str:
    """What a whole reading was made from: the sections' text, the vault setting and her corrections."""
    what = [SCHEMA, title, bool(as_written), settled, [(n, h, sha) for n, h, _text, sha in secs]]
    return hashlib.sha256(json.dumps(what, ensure_ascii=False).encode("utf-8")).hexdigest()


def cached_whole(folder: Path, sha: str) -> bool:
    """A section's cache entry holds all three asks, so reading it again would ask nothing."""
    got = read_json(folder / CACHE / f"{sha}.json")
    return isinstance(got, dict) and all(isinstance(got.get(k), dict) for k in ("extraction", "craft", "looks"))


class Reader:
    """One reading at a time per manuscript, each a thread. ``load(folder)`` -> (title, [(n, heading, text, sha)])."""

    def __init__(self, brain: Brain, load: Loader, publish: Callable[[Path, dict], Any] | None = None,
                 as_written: Callable[[], bool] | None = None, live: Callable[[Path, dict], dict] | None = None,
                 settled: Callable[[Path], list[str]] | None = None,
                 starting: Callable[[Path], None] | None = None, finished: Callable[[Path], None] | None = None,
                 over: Callable[[Path], None] | None = None) -> None:
        self.brain = brain
        self.as_written = as_written or (lambda: True)      # kept in status.json, so the page can say it
        self.load = load
        self.publish = publish           # writes the bible as read, and it with the writer's word on top
        self.live = live or (lambda _f, b: copy.deepcopy(b))  # a copy with the writer's word on top
        self.settled = settled or (lambda _f: [])            # her corrections, as lines for a synthesis ask
        self.starting, self.finished = starting, finished   # told when a reading starts, and when it ends well
        self.over = over                 # told last of all, however the reading ended
        self.lock = threading.Lock()
        self.running: dict[str, threading.Thread] = {}

    def busy(self, folder: Path) -> bool:
        t = self.running.get(str(folder))
        return t is not None and t.is_alive()

    def start(self, folder: Path) -> bool:
        """False when that manuscript is already being read."""
        with self.lock:
            if self.busy(folder):
                return False
            t = threading.Thread(target=self._run, args=(folder,), daemon=True, name=f"reading {folder.name}")
            self.running[str(folder)] = t
            t.start()
            return True

    def _run(self, folder: Path) -> None:
        status = {"state": "reading", "step": "Getting ready", "done": 0, "total": 0, "started_at": now(),
                  "finished_at": None, "problems": [], "as_written": bool(self.as_written())}

        def put(**kw: Any) -> None:
            status.update(kw)
            try:
                write_json(folder / STATUS, status)
            except OSError:
                pass

        try:
            put()
            self._read(folder, status, put)
        except BrainDown as exc:
            status["problems"].append(str(exc))
            put(state="failed", step=str(exc), finished_at=now())
        except Exception as exc:  # noqa: BLE001 - never out of the thread; a plain line in status instead
            line = f"The reading stopped on an error ({type(exc).__name__}). Start reading again."
            status["problems"].append(line)
            put(state="failed", step=line, finished_at=now())
        finally:
            if self.over is not None:
                self.over(folder)

    def _read(self, folder: Path, status: dict, put: Callable[..., None]) -> None:
        title, secs = self.load(folder)
        total = len(secs)
        old = read_json(folder / BIBLE, {}) or {}
        made_of = read_of(title, secs, status.get("as_written"), self.settled(folder))
        if isinstance(old, dict) and old.get("read_of") == made_of and all(cached_whole(folder, s) for *_x, s in secs):
            put(state="done", step=f"Read {total} of {total} sections. Nothing changed since the last reading.",
                total=total, done=total, unchanged=True, finished_at=now())
            return                          # the last whole reading stands, with no ask and nothing written over
        if self.starting is not None:
            self.starting(folder)
        order = [(n, h) for n, h, _t, _s in secs]
        built_with = str(old.get("built_with") or "") if isinstance(old, dict) else ""
        found: dict[int, dict] = {}
        crafts: dict[int, dict] = {}
        looks: dict[int, dict] = {}
        words = {n: len(text.split()) for n, _h, text, _s in secs}
        bible = empty_bible(title, total)
        put(total=total)

        def save() -> None:
            bible["built_at"] = now()
            bible["built_with"] = built_with
            if self.publish is not None:
                self.publish(folder, bible)
            else:
                write_json(folder / BIBLE, bible)

        save()
        for i, (n, heading, text, sha) in enumerate(secs, 1):
            put(step=f"Reading section {i} of {total}", done=i - 1)
            cached = read_json(folder / CACHE / f"{sha}.json")
            told = seen = None
            entry: dict = {}
            if isinstance(cached, dict) and isinstance(cached.get("extraction"), dict):
                entry = dict(cached)
                ext, model = cached["extraction"], str(cached.get("model") or "")
                told = cached.get("craft") if isinstance(cached.get("craft"), dict) else None
                seen = cached.get("looks") if isinstance(cached.get("looks"), dict) else None
            else:
                fat = looks_fit(text, bible)                 # the evidence for pictures in the same ask when it fits
                try:
                    ext, model = extract(self.brain, n, heading, text, bible, fat)
                except BrainSlow:
                    ext, model = None, ""
                    status["problems"].append(f"Section {n} took longer than ten minutes, so it was skipped.")
                else:
                    if ext is None:
                        status["problems"].append(f"Section {n} gave no usable answer.")
                    else:
                        seen = {} if fat else None           # {}: the facts ask carried them
                        entry = {"model": model, "extraction": ext, **({"looks": seen} if fat else {})}
                        write_json(folder / CACHE / f"{sha}.json", entry)
            if ext is not None and told is None:            # a 0.2 cache has the facts but not how it is told
                try:
                    told, model2 = craft(self.brain, n, heading, text)
                except BrainSlow:
                    told, model2 = None, ""
                if told is None:
                    status["problems"].append(f"Section {n} gave no usable answer about how it is told.")
                else:
                    model = model2 or model
                    entry.update(model=model, extraction=ext, craft=told)
                    write_json(folder / CACHE / f"{sha}.json", entry)
            if ext is not None and seen is None:            # too long for one ask, or cached before 0.3.3
                try:
                    seen, model3 = read_looks(self.brain, n, heading, text, bible)
                except BrainSlow:
                    seen, model3 = None, ""
                if seen is None:
                    status["problems"].append(f"Section {n} gave no usable answer about how things look.")
                else:
                    model = model3 or model
                    entry.update(model=model, extraction=ext, looks=seen)
                    write_json(folder / CACHE / f"{sha}.json", entry)
            if seen:
                looks[n] = seen
            built_with = model or built_with
            if ext is not None:
                found[n] = ext
            if told is not None:
                crafts[n] = craft_facts(told, n, text)
            bible = merge(title, order, found, crafts, words, looks)
            bible["sections_read"] = i
            save()
            put(done=i)

        # Each step asks from the corrected facts and is told her corrections are settled. What it wrote comes back to
        # the bible as read (carry), so that one stays free of her corrections and an undo needs no re-read.
        asker = Settled(self.brain, lambda: self.settled(folder))
        now_ = self.live(folder, bible)
        steps: list[tuple[str, Callable[[dict], str]]] = []

        def person(fn: Callable[[Any, dict, dict], str], cid: str) -> Callable[[dict], str]:
            def run(b: dict) -> str:                         # merged away by a correction: nothing to write up
                c = next((x for x in b["characters"] if x["id"] == cid), None)
                return fn(asker, b, c) if c is not None else ""
            return run

        for c in now_["characters"]:
            if len(c["appearances"]) >= 4:
                steps.append((f"Putting together {c['name']}'s arc", person(synth_arc, c["id"])))
        if now_["events"] or now_["threads"]:
            steps.append(("Following the plot threads", lambda b: synth_threads(asker, b)))
        if now_["themes"] or now_["summary"]["by_section"]:
            steps.append(("Gathering the themes", lambda b: synth_themes(asker, b)))
        if len(now_["events"]) >= 2:
            steps.append(("Putting the events in story order", lambda b: synth_timeline(asker, b)))
        if now_["summary"]["by_section"] or now_["scenes"]:
            steps.append(("Finding the shape of the story", lambda b: synth_structure(asker, b)))
        if any(isinstance(x, dict) and x.get("setting") for x in found.values()) or now_["history"] or \
                now_["places"] or now_["world_rules"]:
            steps.append(("Describing the world", lambda b: synth_setting(asker, b, found)))
        for c in now_["characters"]:
            if c["importance"] == "main":
                steps.append((f"Writing up {c['name']}", lambda b, cid=c["id"]: synth_people(asker, b, [cid])))
        support = [c["id"] for c in now_["characters"] if c["importance"] == "supporting"]
        for i in range(0, len(support), 4):                  # four supporting characters to a call
            ids = support[i:i + 4]
            steps.append(("Writing up the supporting cast", lambda b, ids=ids: synth_people(asker, b, ids)))
        if found:
            for tab in BRIEF_TABS:                            # the briefs last: they sum up everything above
                steps.append((f"Writing the {tab} brief", lambda b, tab=tab: synth_brief(asker, b, tab)))
        for line, fn in steps:
            put(step=line)
            live = self.live(folder, bible)
            before = copy.deepcopy(live)
            try:
                built_with = fn(live) or built_with
            except BrainSlow:
                status["problems"].append(f"{line} took longer than ten minutes, so it was skipped.")
            carry(before, live, bible)
            save()
        if not status["problems"] and not asker.missed:     # whole: the next re-read may keep it (see read_of)
            bible["read_of"] = made_of
            save()
        read = sum(1 for n, _h in order if n in found)
        put(state="done", step=f"Read {read} of {total} sections.", done=total, finished_at=now())
        if self.finished is not None:
            self.finished(folder)


# -- picking facts for an ask -------------------------------------------------------------------------

STOP = set("the a an and or of to in on at for with from by is are was were be been it its this that these those "
           "me my i you your she he her him his they them their we our show tell what where when who how why does do "
           "did about story book manuscript there here which into than then just like".split())


def words_of(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']+", str(text).lower()) if len(w) > 2 and w not in STOP}


def fact_lines(bible: dict) -> list[str]:
    names = {c.get("id"): c.get("name") for c in bible.get("characters") or []}
    out = []
    for c in bible.get("characters") or []:
        arc = "; ".join(f"section {b.get('section')}: {b.get('beat')}" for b in c.get("arc") or [])
        rel = "; ".join(f"{r.get('kind')} of {names.get(r.get('to'), r.get('to'))}" for r in c.get("relationships") or [])
        out.append(f"Character {c.get('name')}" + (f" (also {', '.join(c.get('aliases') or [])})" if c.get("aliases")
                                                   else "") +
                   f", {c.get('role')}; first seen in section {c.get('first_seen')}; in sections "
                   f"{c.get('appearances')}; traits {', '.join(c.get('traits') or [])}; wants {c.get('wants')}; "
                   f"fears {c.get('fears')}; relationships: {rel}; arc: {arc}")
        if c.get("importance") or c.get("personality"):
            out[-1] += f"; a {c.get('importance')} character; {c.get('personality') or ''}"
        looks = [str(x.get("detail")) for x in c.get("looks") or [] if isinstance(x, dict) and x.get("detail")]
        if looks:
            out[-1] += "; looks: " + ", ".join(looks)
        if isinstance(c.get("age_words"), dict) and c["age_words"].get("words"):
            out[-1] += f"; age: {c['age_words']['words']}"
    for p in bible.get("places") or []:
        out.append(f"Place {p.get('name')} ({p.get('kind')}" + (f", in {p.get('where')}" if p.get("where") else "")
                   + f"), sections {p.get('appearances')}: {p.get('notes')}")
    st = bible.get("setting") if isinstance(bible.get("setting"), dict) else {}
    if any(st.get(k) for k in ("world", "place_in_world", "era", "technology")):
        out.append(f"Setting: the world {st.get('world')}; where {st.get('place_in_world')}; when {st.get('era')}; "
                   f"technology {st.get('technology')}")
    for h in bible.get("history") or []:
        if isinstance(h, dict):
            out.append(f"History of the world ({h.get('when')}), sections {h.get('sections')}: {h.get('what')}")
    for e in bible.get("events") or []:
        out.append(f"Event in section {e.get('section')}: {e.get('what')} Who: "
                   f"{', '.join(names.get(w, w) for w in e.get('who') or [])}. Where: {e.get('where')}. "
                   f"When: {e.get('when')}.")
    for t in bible.get("threads") or []:
        out.append(f"Thread {t.get('name')} ({t.get('kind')}, {'open' if t.get('open') else 'paid off'}), sections "
                   f"{t.get('sections')}; setups: {'; '.join(t.get('setups') or [])}; payoffs: "
                   f"{'; '.join(t.get('payoffs') or [])}")
    for t in bible.get("themes") or []:
        out.append(f"Theme {t.get('name')}: " + " | ".join(f"section {e.get('section')}: \"{e.get('quote')}\""
                                                           for e in t.get("evidence") or []))
    for r in bible.get("world_rules") or []:
        out.append(f"World rule, sections {r.get('sections')}: {r.get('rule')}")
    for s in bible.get("science") or []:
        out.append(f"Science claim in section {s.get('section')}: {s.get('claim')} Plausible: {s.get('plausible')}. "
                   f"{s.get('note')}")
    for c in bible.get("continuity") or []:
        out.append(f"Continuity {c.get('kind')} in sections {c.get('sections')}: {c.get('note')}")
    for e in bible.get("editing") or []:
        out.append(f"Editing note, section {e.get('section')} ({e.get('kind')}): {e.get('note')}")
    for s in (bible.get("summary") or {}).get("by_section") or []:
        out.append(f"Summary of section {s.get('section')}: {s.get('summary')}")
    for s in bible.get("scenes") or []:
        if isinstance(s, dict) and s.get("purpose"):
            out.append(f"Scene {s.get('n')} of section {s.get('section')} ({s.get('pov')}, {s.get('tense')} tense, "
                       f"{s.get('words')} words): {s.get('purpose')}")
    st = bible.get("structure") if isinstance(bible.get("structure"), dict) else {}
    for a in st.get("acts") or []:
        out.append(f"Structure: {a.get('name')} runs from section {a.get('from')} to section {a.get('to')}")
    for t in st.get("turning_points") or []:
        out.append(f"Structure: turning point {t.get('name')} in section {t.get('section')}: {t.get('note')}")
    for k in ("midpoint", "climax"):
        if isinstance(st.get(k), dict) and st[k].get("section") is not None:
            out.append(f"Structure: the {k} is in section {st[k]['section']}: {st[k].get('note')}")
    v = bible.get("voice") if isinstance(bible.get("voice"), dict) else {}
    for s in v.get("by_section") or []:
        out.append(f"Voice in section {s.get('section')}: point of view {s.get('pov')}, {s.get('tense')} tense"
                   + (f"; slips: {'; '.join(s.get('slips') or [])}" if s.get("slips") else ""))
    for t in v.get("tics") or []:
        out.append(f"Voice tic: \"{t.get('phrase')}\" {t.get('count')} times, sections {t.get('sections')}")
    for h in v.get("dialogue_habits") or []:
        out.append(f"Dialogue habit of {h.get('character')}: {h.get('habit')}")
    return out


def pick_facts(bible: dict, query: str, room: int) -> str:
    """The bible facts that share the most words with the ask, best first, as many as fit in *room* tokens. With
    no word in common, the section summaries and the people."""
    lines = fact_lines(bible)
    q = words_of(query)
    scored = sorted(((len(q & words_of(ln)), i) for i, ln in enumerate(lines)), key=lambda x: (-x[0], x[1]))
    chosen = [lines[i] for s, i in scored if s > 0]
    if not chosen:
        chosen = [ln for ln in lines if ln.startswith(("Summary", "Character"))]
    return fit(chosen, room)
