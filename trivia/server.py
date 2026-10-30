# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/app_data.py (adapted)
"""Trivia: quiz rounds and scores, as a PuffPAI package.

One small server, Python standard library only, bound to 127.0.0.1:
- ``/``            the game page (web/index.html), shown in the Desktop at /pkg/trivia/
- ``/health``      "ok"
- ``/questions``   the question bank (questions.json in this package, plus the owner's own questions)
- ``/data``        the scores document (GET), ``/updated`` its stamp
- ``/api/result``  the page saves one finished game (POST)
- ``/mcp``         the MCP server (JSON-RPC over HTTP)

Every route refuses a request without this package's PUFFPAI_TOKEN (D48d). The
Desktop reaches the page through the Core's /pkg/trivia/, which adds it.

The scores are one JSON document in PUFFPAI_DATA (``trivia.json``, the v1 shape
``{results: [...], last_updated}``, so a v1 file can be copied in), with the
owner's own questions added beside them (``my_questions``, ``my_categories``).
A round played in chat is kept in memory until ``end_round`` saves it. Writes
replace the whole file at once (temp file, then rename). A file that exists
but will not read is never written over.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import random
import tempfile
import threading
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "web" / "index.html"
BANK = HERE / "questions.json"
NAME = "trivia"
VERSION = "2.1.0"
DOC = "trivia.json"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 256 * 1024
POINTS = {"kids": 100, "beginner": 100, "mid": 200, "jeopardy": 300}     # per right answer, as the page counts
LEVELS = tuple(POINTS)
LETTERS = "ABCD"
MODES = ("casual", "brain", "group", "chat")
#: The page's own scoring goes past 1,000 (up to 300 a question plus time and streak bonuses, 20 questions),
#: so a score is a whole number from 0 to 100,000.
MAX_SCORE = 100_000
MAX_OWN = 2000              # questions of the owner's own, at most
OWN_ICON = "\u2b50"         # the icon of a category the owner adds


def _now() -> str:
    return datetime.now().isoformat()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- checked input

def _number(value, name: str, lo: float, hi: float, whole: bool = False):
    """A finite number from *lo* to *hi* (a whole one when *whole*), or a plain ValueError.
    Never NaN or infinity, and never a huge number that overflows."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be a number")
    try:
        x = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(f"{name} is a number from {lo:g} to {hi:g}")
    if whole:
        if x != int(x):
            raise ValueError(f"{name} is a whole number")
        return int(x)
    return x


def _text(value, name: str, most: int, default: str = "") -> str:
    """Words of at most *most* characters, trimmed; *default* when not given."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be words")
    value = value.strip()
    if len(value) > most:
        raise ValueError(f"{name} is at most {most} characters")
    return value or default


def _num(value) -> float:
    """A stored number for showing: 0 for anything that is not a finite number."""
    if isinstance(value, bool):
        return float(value)
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _finite(obj):
    """*obj* with every NaN or infinity replaced by 0: repairs a file saved before input was checked."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return 0
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_finite(v) for v in obj]
    return obj


def _no_constant(word: str):
    raise ValueError(f"{word} is not a number this app takes")


def _loads(text: str):
    """JSON from a request: NaN and Infinity are refused, and a huge number is checked where it is used."""
    return json.loads(text, parse_constant=_no_constant)


# ---------------------------------------------------------------- the store

class Unreadable(RuntimeError):
    """The saved document exists but will not read. Nothing writes over it."""


def default_doc() -> dict:
    return {"results": [], "last_updated": ""}


class Store:
    """One JSON document with a stamp (the v1 pattern, app_data.py)."""

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DOC
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return default_doc()
            try:
                data = _finite(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Unreadable(f"{self.path} will not read ({exc}). Trivia will not write over it.") from None
            if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
                raise Unreadable(f"{self.path} is not a Trivia scores file. Trivia will not write over it.")
            out = default_doc()
            out.update(data)
            return out

    def save(self, data: dict) -> str:
        with self.lock:
            data["last_updated"] = _now()
            fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".trivia-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1, allow_nan=False)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return data["last_updated"]

    def add_result(self, result: dict) -> str:
        with self.lock:
            data = self.load()
            data["results"].append(result)
            return self.save(data)


# ------------------------------------------------------------- the bank

def load_bank() -> dict:
    return json.loads(BANK.read_text(encoding="utf-8"))


def _top_score(result: dict) -> tuple[float, str]:
    """A result's score and who got it. A group game keeps a list of {name, score}; its best counts."""
    score = result.get("score", 0)
    if isinstance(score, list):
        best = max((p for p in score if isinstance(p, dict)), key=lambda p: _num(p.get("score")), default={})
        return _num(best.get("score")), str(best.get("name") or "")[:40]
    return _num(score), str(result.get("player") or "")[:40]


def _slug(name: str) -> str:
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in name.lower()).strip("-")[:30] or "mine"


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _own_question(item) -> bool:
    """An owner's question as kept in trivia.json: sound enough to ask."""
    return (isinstance(item, dict) and isinstance(item.get("q"), str) and isinstance(item.get("category"), str)
            and item.get("difficulty") in LEVELS and isinstance(item.get("options"), list)
            and len(item["options"]) == 4 and all(isinstance(o, str) for o in item["options"])
            and item.get("answer") in (0, 1, 2, 3))


# ------------------------------------------------------------- the logic

class Trivia:
    def __init__(self, store: Store, bank: dict) -> None:
        self.store = store
        self.bank = bank
        self.recent: deque[str] = deque(maxlen=40)     # questions asked lately, so a new round does not repeat
        self.rng = random.Random()
        self.lock = threading.Lock()
        self.round: dict | None = None                 # the chat round going now (in memory; end_round saves it)

    # the bank with the owner's own questions added
    def view(self) -> dict:
        """The bank as the page and the brain see it: the questions that came with Trivia plus the owner's own
        (``my_categories`` and ``my_questions`` in trivia.json). A scores file that will not read leaves the
        bank as it came."""
        try:
            doc = self.store.load()
        except Unreadable:
            doc = {}
        cats = [dict(c) for c in self.bank["categories"]]
        ids = {c["id"] for c in cats}
        for c in doc.get("my_categories") or []:
            if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("name"), str) \
                    and c["id"] not in ids:
                cats.append({"id": c["id"], "icon": OWN_ICON, "name": c["name"][:40], "own": True})
                ids.add(c["id"])
        questions = {cid: {d: list(items) for d, items in by.items()} for cid, by in self.bank["questions"].items()}
        for item in doc.get("my_questions") or []:
            if _own_question(item) and item["category"] in ids and item["category"] != "random":
                questions.setdefault(item["category"], {}).setdefault(item["difficulty"], []).append(
                    {"q": item["q"], "options": item["options"], "answer": item["answer"], "own": True})
        return {**{k: v for k, v in self.bank.items() if k not in ("categories", "questions")},
                "categories": cats, "questions": questions}

    # helpers
    def _names(self, view: dict | None = None) -> dict:
        return {c["id"]: c["name"] for c in (view or self.view())["categories"]}

    def _category(self, category: str | None, view: dict | None = None) -> str:
        """A category id from an id or a name ("Food & Drink", "food", "the 2000s"); random if empty."""
        names = self._names(view)
        text = str(category or "").strip().lower()
        if not text or text in ("any", "random", "mixed", "anything"):
            return "random"
        for cid, name in names.items():
            if text in (cid, name.lower()) or text.removeprefix("the ") in (cid, name.lower().removeprefix("the ")):
                return cid
        for cid, name in names.items():
            if cid != "random" and (cid in text or text in name.lower()):
                return cid
        raise ValueError(f"there is no {category!r} category. Categories: {', '.join(names.values())}")

    def _difficulty(self, difficulty: str | None) -> str:
        text = str(difficulty or "").strip().lower()
        if not text or text in ("easy", "beginner", "simple"):
            return "beginner"
        if text in ("mid", "medium", "middle", "normal", "moderate"):
            return "mid"
        if text in ("jeopardy", "hard", "difficult", "expert"):
            return "jeopardy"
        if text in ("kids", "kid", "children", "child", "kids mode"):
            return "kids"
        raise ValueError("difficulty is beginner, mid, jeopardy or kids")

    def _label(self, cid, view: dict | None = None) -> str:
        cid = cid if isinstance(cid, str) else ""
        return self._names(view).get(cid, cid[:40] or "Random")

    def _pool(self, view: dict, cid: str, diff: str) -> list[tuple[str, dict]]:
        """(category, question) pairs to ask from. Kids mode: the beginner questions of the kids categories,
        plus any question the owner added at the kids level."""
        q = view["questions"]
        kids_ok = set(view.get("kids_categories") or [])
        out = []
        for c in (q if cid == "random" else [cid]):
            by = q.get(c, {})
            items = ((by.get("beginner", []) if c in kids_ok else []) + by.get("kids", [])) if diff == "kids" \
                else by.get(diff, [])
            out += [(c, x) for x in items]
        return out

    def _scores(self) -> str:
        """The round's score line: "Sam: 3 of 4 right (300 points), Ada: 2 of 4 right (200 points)"."""
        players = self.round["players"] if self.round else {}
        return ", ".join(f"{name or 'you'}: {p['right']} of {p['asked']} right ({p['points']} points)"
                         for name, p in players.items())

    # tools
    def stats(self) -> str:
        """From trivia_get_stats (app_data.py:1301), plus the last game, group games and the round going now."""
        results = [r for r in self.store.load()["results"] if isinstance(r, dict)]
        with self.lock:
            scores_now = self._scores()
        going = f" A round is going in chat: {scores_now}." if scores_now else ""
        if not results:
            return "No trivia games played yet." + going
        view = self.view()
        total = len(results)
        scores = [_top_score(r)[0] for r in results]
        best_i = max(range(total), key=lambda i: scores[i])
        best, (_, who) = results[best_i], _top_score(results[best_i])
        last = results[-1]
        line = (f"Trivia stats: {_plural(total, 'game')} played. Average score: {sum(scores) / total:.1f}. "
                f"Best: {scores[best_i]:g}{f' by {who}' if who else ''} in {self._label(best.get('category'), view)} "
                f"({str(best.get('difficulty') or 'unknown')[:20]}).")
        if total > 1:
            line += (f" Last game: {_top_score(last)[0]:g} in {self._label(last.get('category'), view)} "
                     f"({str(last.get('difficulty') or 'unknown')[:20]}).")
        return line + going

    def list_categories(self) -> str:
        view = self.view()
        q = view["questions"]
        rows = []
        for c in view["categories"]:
            if c["id"] == "random":
                continue
            by = q.get(c["id"], {})
            own = sum(1 for v in by.values() for x in v if x.get("own"))
            kids = len(self._pool(view, c["id"], "kids"))
            rows.append(f"{c['name']}: {_plural(sum(len(v) for v in by.values()), 'question')}"
                        + (f", {own} of them yours" if own else "") + (f", {kids} for kids mode" if kids else ""))
        return ("Trivia categories, at beginner, mid and jeopardy level (Random mixes them all; "
                "kids mode asks the easy ones that suit children):\n" + "\n".join(rows))

    def ask_question(self, category: str = "random", difficulty: str = "beginner", player: str = "") -> str:
        view = self.view()
        cid = self._category(_text(category, "category", 40), view)
        diff = self._difficulty(_text(difficulty, "difficulty", 20))
        player = _text(player, "player", 40)
        pool = self._pool(view, cid, diff)
        if not pool:
            kids = [self._label(c, view) for c in view.get("kids_categories") or []]
            more = f" Kids mode has: {', '.join(kids)}." if diff == "kids" else ""
            return f"There are no {diff} questions in {self._label(cid, view)}.{more}"
        with self.lock:
            if self.round is None:
                self.round = {"asked": [], "pending": None, "players": {}, "kinds": []}
            rnd = self.round
            fresh = [p for p in pool if p[1]["q"] not in rnd["asked"]]
            note = "" if fresh else "Every question here was asked this round, so this one comes round again. "
            fresh = [p for p in fresh if p[1]["q"] not in self.recent] or fresh or pool
            got_cat, item = self.rng.choice(fresh)
            self.recent.append(item["q"])
            rnd["asked"].append(item["q"])
            options = list(item["options"])
            right = options[item["answer"]]
            self.rng.shuffle(options)
            rnd["pending"] = {"q": item["q"], "options": options, "right": options.index(right),
                              "category": got_cat, "difficulty": diff, "player": player}
            rnd["kinds"].append((got_cat, diff))
            number = len(rnd["asked"])
        lines = "\n".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(options))
        who = f", for {player}" if player else ""
        return (f"{note}Question {number} of this round{who}. {self._label(got_cat, view)} ({diff}): "
                f"{item['q']}\n{lines}")

    @staticmethod
    def _guess(answer: str, options: list[str]) -> int:
        """The option a guess names: a letter (A, "b.", "c)") or the option's words."""
        low = answer.strip().rstrip(".!").strip().lower()
        for prefix in ("the answer is ", "answer ", "option ", "letter "):
            low = low.removeprefix(prefix)
        if len(low) == 1 and low.upper() in LETTERS:
            return LETTERS.index(low.upper())
        if len(low) >= 2 and low[0].upper() in LETTERS and low[1] in ".):":
            rest = low[2:].strip()
            if not rest or any(_same(rest, o) for o in options):
                return LETTERS.index(low[0].upper())
        exact = [i for i, o in enumerate(options) if _same(low, o)]
        if exact:
            return exact[0]
        near = [i for i, o in enumerate(options) if len(low) >= 3 and (low in o.lower() or o.lower() in low)]
        if len(near) == 1:
            return near[0]
        raise ValueError("answer is a letter A to D, or the words of one of the options")

    def check_answer(self, answer: str, player: str = "") -> str:
        answer = _text(answer, "answer", 200)
        player = _text(player, "player", 40)
        with self.lock:
            pending = self.round["pending"] if self.round else None
            if not pending:
                return "There is no question waiting for an answer. Ask one first."
            pick = self._guess(answer, pending["options"])
            right = pick == pending["right"]
            p = self.round["players"].setdefault(player or pending["player"], {"right": 0, "asked": 0, "points": 0})
            p["asked"] += 1
            if right:
                p["right"] += 1
                p["points"] += POINTS[pending["difficulty"]]
            self.round["pending"] = None
            scores = self._scores()
        said = "Right!" if right else f"Not quite, it is not {LETTERS[pick]}."
        return (f"{said} The answer is {LETTERS[pending['right']]}. {pending['options'][pending['right']]}. "
                f"Score: {scores}.")

    def end_round(self, save: bool = True) -> str:
        if not isinstance(save, bool):
            raise ValueError("save is true or false")
        with self.lock:
            rnd, self.round = self.round, None
        players = {n: p for n, p in (rnd or {}).get("players", {}).items() if p["asked"]}
        if not players:
            return "No round to end: nobody has answered a question yet."
        cats = {c for c, _ in rnd["kinds"]}
        diffs = {d for _, d in rnd["kinds"]}
        board = ", ".join(f"{n or 'you'}: {p['right']} of {p['asked']} right ({p['points']} points)"
                          for n, p in players.items())
        if not save:
            return f"Round over, not saved. {board}."
        result = {"date": _now(), "mode": "chat", "category": cats.pop() if len(cats) == 1 else "random",
                  "difficulty": diffs.pop() if len(diffs) == 1 else "mixed", "source": "brain",
                  "totalQuestions": sum(p["asked"] for p in players.values())}
        if len(players) == 1:
            ((name, p),) = players.items()
            result.update(score=p["points"], correct=p["right"], player=name)
        else:
            result["score"] = [{"name": n or "Player", "score": p["points"]} for n, p in players.items()]
        self.store.add_result(result)
        win = ""
        if len(players) > 1:
            top = max(p["points"] for p in players.values())
            best = [n or "you" for n, p in players.items() if p["points"] == top]
            win = f" Winner: {best[0]}." if len(best) == 1 else f" A tie: {' and '.join(best)}."
        return f"Round over and saved. {board}.{win}"

    def record_result(self, correct: int, total_questions: int, category: str = "random",
                      difficulty: str = "beginner", player: str = "") -> str:
        correct = _number(correct, "correct", 0, 1000, whole=True)
        total_questions = _number(total_questions, "total_questions", 1, 1000, whole=True)
        if correct > total_questions:
            raise ValueError("correct is a count from 0 to total_questions, and total_questions is at least 1")
        cid = self._category(_text(category, "category", 40))
        diff = self._difficulty(_text(difficulty, "difficulty", 20))
        player = _text(player, "player", 40)
        score = correct * POINTS[diff]
        self.store.add_result({
            "date": _now(), "mode": "chat", "category": cid, "difficulty": diff, "score": score,
            "correct": correct, "totalQuestions": total_questions, "player": player, "source": "brain",
        })
        who = f" for {player}" if player else ""
        return (f"Saved{who}: {correct} of {total_questions} right in {self._label(cid)} ({diff}), "
                f"{score} points.")

    def add_question(self, question: str, right_answer: str, wrong_answers: list, category: str,
                     difficulty: str = "beginner") -> str:
        question = _text(question, "question", 300)
        right = _text(right_answer, "right_answer", 120)
        if len(question) < 5 or not right:
            raise ValueError("a question needs its words and the right answer")
        if not isinstance(wrong_answers, list) or len(wrong_answers) != 3:
            raise ValueError("wrong_answers is a list of 3 wrong answers")
        options = [right, *(_text(w, "a wrong answer", 120) for w in wrong_answers)]
        if not all(options) or len({" ".join(o.lower().split()) for o in options}) != 4:
            raise ValueError("the right answer and the 3 wrong ones must be 4 different answers")
        diff = self._difficulty(_text(difficulty, "difficulty", 20))
        name = _text(category, "category", 40)
        if not name:
            raise ValueError("a question needs a category: one on the list, or a new one")
        with self.store.lock:
            data = self.store.load()
            view = self.view()
            mine = [x for x in data.get("my_questions") or [] if isinstance(x, dict)]
            if len(mine) >= MAX_OWN:
                raise ValueError(f"Trivia keeps at most {MAX_OWN} questions of your own; remove one first")
            if any(_same(question, x["q"]) for by in view["questions"].values() for v in by.values() for x in v):
                raise ValueError("that question is already in the bank")
            new = ""
            try:
                cid = self._category(name, view)
            except ValueError:
                cid, ids = _slug(name), {c["id"] for c in view["categories"]}
                while cid in ids:
                    cid += "-2"
                data["my_categories"] = [*(data.get("my_categories") or []),
                                         {"id": cid, "icon": OWN_ICON, "name": name}]
                new = f" It starts a new category, {name}."
            if cid == "random":
                raise ValueError("pick a category for it; Random mixes them all")
            data["my_questions"] = [*mine, {"category": cid, "difficulty": diff, "q": question,
                                            "options": options, "answer": 0, "added": _now()}]
            self.store.save(data)
        return f"Added to {self._label(cid)} ({diff}): {question} The answer: {right}.{new}"

    def remove_question(self, question: str) -> str:
        question = _text(question, "question", 300)
        with self.store.lock:
            data = self.store.load()
            mine = [x for x in data.get("my_questions") or [] if isinstance(x, dict)]
            keep = [x for x in mine if not _same(question, str(x.get("q") or ""))]
            if len(keep) == len(mine):
                if any(_same(question, x["q"]) for by in self.bank["questions"].values()
                       for v in by.values() for x in v):
                    return "That question came with Trivia; only questions you added can be removed."
                return f"None of your own questions is {question!r}."
            data["my_questions"] = keep
            used = {x.get("category") for x in keep}
            data["my_categories"] = [c for c in data.get("my_categories") or []
                                     if isinstance(c, dict) and c.get("id") in used]
            self.store.save(data)
        return f"Removed: {question}"

    # the page
    def page_result(self, body: dict) -> str:
        """One finished game from the page (the v1 page result, ``saveGameResult``)."""
        mode = _text(body.get("mode"), "mode", 20, "casual")
        if mode not in MODES:
            raise ValueError(f"mode is one of {', '.join(MODES)}")
        if "score" not in body:
            raise ValueError("A result needs a score.")
        score = body["score"]
        if isinstance(score, list):
            if not 1 <= len(score) <= 4 or not all(isinstance(p, dict) for p in score):
                raise ValueError("A group score is a list of 1 to 4 players, each {name, score}.")
            score = [{"name": _text(p.get("name"), "a player's name", 40, f"Player {i + 1}"),
                      "score": _number(p.get("score"), "a player's score", 0, MAX_SCORE, whole=True)}
                     for i, p in enumerate(score)]
        else:
            score = _number(score, "score", 0, MAX_SCORE, whole=True)
        when = _text(body.get("date"), "date", 40) or _now()
        try:
            datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("date is an ISO date and time, like 2026-10-25T20:00:00Z") from None
        return self.store.add_result({
            "date": when, "mode": mode, "category": self._category(_text(body.get("category"), "category", 40)),
            "difficulty": self._difficulty(_text(body.get("difficulty"), "difficulty", 20)), "score": score,
            "bestStreak": _number(body.get("bestStreak", 0), "bestStreak", 0, 1000, whole=True),
            "totalQuestions": _number(body.get("totalQuestions", 0), "totalQuestions", 0, 1000, whole=True),
            "source": "page",
        })


# --------------------------------------------------------------- MCP tools

def _p(kind: str, text: str) -> dict:
    return {"type": kind, "description": text}


_LEVEL = "beginner, mid, jeopardy, or kids (easy questions that suit children). Default beginner."
TOOLS = [
    ("stats", "Trivia scores so far: games played, the average score, the best score, the last game, and the "
     "score of a round going in chat.", {}, []),
    ("list_categories", "List the trivia question categories and how many questions each has.", {}, []),
    ("ask_question", "Ask the next multiple-choice trivia question of a round in chat: the question and options A "
     "to D. The answer is kept back until check_answer. A round does not repeat a question until the category "
     "runs out.",
     {"category": _p("string", "Like history, science, movies, music, sports, geography, or random. Default random."),
      "difficulty": _p("string", _LEVEL),
      "player": _p("string", "Optional: whose turn it is, for a game with several players.")}, []),
    ("check_answer", "Check a guess at the question just asked: says if it is right, gives the answer, and keeps "
     "each player's score for the round.",
     {"answer": _p("string", "The guess: a letter A to D, or the words of an option."),
      "player": _p("string", "Optional: who answered. Default the player the question was for.")}, ["answer"]),
    ("end_round", "End the trivia round played in chat and save each player's score.",
     {"save": _p("boolean", "False to end it without saving. Default true.")}, []),
    ("record_result", "Save the score of a trivia round played some other way: how many answers were right out of "
     "how many.",
     {"correct": _p("integer", "How many answers were right."),
      "total_questions": _p("integer", "How many questions were asked."),
      "category": _p("string", "The category played. Default random."),
      "difficulty": _p("string", _LEVEL),
      "player": _p("string", "Optional: who played.")}, ["correct", "total_questions"]),
    ("add_question", "Add a question of your own to the trivia bank, for the game page and for chat. A category "
     "that is not on the list becomes a new category.",
     {"question": _p("string", "The question."), "right_answer": _p("string", "The right answer."),
      "wrong_answers": {"type": "array", "items": {"type": "string"}, "description": "Exactly 3 wrong answers."},
      "category": _p("string", "Like history or science, or a new one like Our Family."),
      "difficulty": _p("string", _LEVEL)}, ["question", "right_answer", "wrong_answers", "category"]),
    ("remove_question", "Remove a question you added to the trivia bank.",
     {"question": _p("string", "The question's words, as added.")}, ["question"]),
]
CHANGES = ["end_round", "record_result", "add_question", "remove_question"]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "required": req},
             "annotations": {"readOnlyHint": n not in CHANGES}}
            for n, d, props, req in TOOLS]


def mcp_answer(app: Trivia, msg: dict) -> dict | None:
    """One JSON-RPC message in, one answer out (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 request."}}
    if "id" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tool_list()})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t[0] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"No tool named {name!r}."}}
        try:
            text = getattr(app, name)(**args)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Unreadable as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except (TypeError, ValueError) as exc:
            return ok({"content": [{"type": "text", "text": f"Those arguments did not fit {name}: {exc}"}], "isError": True})
        except Exception as exc:  # noqa: BLE001 - any other failure is a plain tool error, never a crash
            return ok({"content": [{"type": "text", "text": f"{name} stopped with an error ({type(exc).__name__})."}],
                       "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Trivia does not know {method!r}."}}


# --------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Trivia/2"
    app: Trivia
    token: str
    timeout = 15        # seconds; a stalled sender is cut off (set on the socket)

    def log_message(self, fmt, *args):     # quiet; the Core keeps the logs it needs
        pass

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
        self.send_response(code)
        if body or code != 202:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _length(self) -> int:
        """Content-Length, strictly: missing is 0; otherwise digits only, at most MAX_BODY bytes."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        raw = raw.strip()
        if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_BODY:
            raise ValueError(f"Content-Length must be a whole number of bytes, at most {MAX_BODY // 1024} KB.")
        return int(raw)

    def _body(self):
        n = self._length()
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            raise ValueError("The request ended before its body did.")
        return _loads(raw.decode("utf-8")) if raw else {}

    def _token_ok(self) -> bool:
        if not self.token:
            return False                     # no token given at start: refuse every call
        got = self.headers.get("Authorization", "")
        got = got[7:] if got.lower().startswith("bearer ") else self.headers.get("X-PuffPAI-Key", "")
        return hmac.compare_digest(got.encode(), self.token.encode())

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _refused(self) -> bool:
        """Every route needs the token (D48d), checked before anything else is read. A body with a sound
        Content-Length is read, so the refusal arrives whole; any other one closes the connection."""
        if self._token_ok():
            return False
        try:
            n = self._length()
            if n:
                self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
        self._json(401, {"error": "Trivia needs its PuffPAI token. Open it from the PuffPAI Desktop."})
        return True

    def _guarded(self, route) -> None:
        """Any error a route did not expect becomes a plain JSON answer, never a dropped connection."""
        try:
            route()
        except Exception as exc:  # noqa: BLE001 - the answer says what kind of error it was
            self.close_connection = True
            try:
                self._json(500, {"error": f"Trivia stopped on this request with an error ({type(exc).__name__})."})
            except Exception:  # noqa: BLE001 - the connection itself is gone
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._send(405, b"", "text/plain")      # no server-sent stream; POST only
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/questions":
                return self._json(200, self.app.view())
            if path == "/data":
                return self._json(200, self.app.store.load())
            if path == "/updated":
                return self._json(200, {"last_updated": self.app.store.load()["last_updated"]})
        except Unreadable as exc:
            return self._json(500, {"error": str(exc)})
        self._json(404, {"error": "Trivia has no page here."})

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        if self._refused():
            return
        path = self._path()
        if path == "/mcp":
            return self._mcp()
        if path != "/api/result":
            return self._json(404, {"error": "Trivia has no page here."})
        try:
            body = self._body()
            if not isinstance(body, dict):
                return self._json(400, {"error": "Send one game result as a JSON object."})
            self._json(200, {"last_updated": self.app.page_result(body)})
        except Unreadable as exc:
            self._json(409, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_PUT(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def do_DELETE(self):
        if self._refused():
            return
        self._send(405, b"", "text/plain")

    def _mcp(self):
        try:
            msg = self._body()
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}})
        answer = mcp_answer(self.app, msg)
        if answer is None:
            return self._send(202)
        self._json(200, answer)


def make_server(port: int, data: Path | str, token: str | None) -> ThreadingHTTPServer:
    """The server, bound to 127.0.0.1 only. Call serve_forever(); stop with shutdown()."""
    app = Trivia(Store(data), load_bank())
    handler = type("TriviaHandler", (Handler,), {"app": app, "token": token or ""})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Trivia for PuffPAI")
    p.add_argument("--port", type=int, default=int(os.environ.get("PUFFPAI_PORT") or 0))
    args = p.parse_args(argv)
    data = os.environ.get("PUFFPAI_DATA")
    if not args.port or not data:
        p.error("PuffPAI starts Trivia with PUFFPAI_PORT (or --port) and PUFFPAI_DATA set.")
    server = make_server(args.port, data, os.environ.get("PUFFPAI_TOKEN"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
