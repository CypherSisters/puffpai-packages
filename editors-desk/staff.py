"""The editor-in-chief and her staff (EDITOR_IN_CHIEF.md): who they are, and which checks are whose.

Data and small helpers only. A staff member owns the checks of one stage (``engine.STAGES`` ids), so a new check file joins
a desk by its ``stage:`` line and nothing here changes. They are invented characters; their names live here and nowhere
else, so the owner can change them in one place.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from typing import Any

STAFF: list[dict] = [
    {"id": "chief", "name": "Dewey", "role": "Editor-in-chief", "stage": "any", "img": "img/staff/chief.png",
     "does": "Runs the house: reads what every desk found, writes your letter, and says what to do next.",
     "lens": "You are her editor-in-chief. Think about the whole book: what it is, and what matters most to it."},
    {"id": "story", "name": "Margot", "role": "Story editor", "stage": "story", "img": "img/staff/story.png",
     "does": "Looks at the big shape: plot, structure, people, point of view, the opening and ending, and the themes.",
     "lens": "You are her story editor. Look at what happens and why it matters to the story."},
    {"id": "facts", "name": "Ines", "role": "Fact checker", "stage": "facts", "img": "img/staff/facts.png",
     "does": "Checks that the book agrees with itself: who, where, when, and the rules of its world.",
     "lens": "You are her fact checker. Hold the words to what the book has already said is true."},
    {"id": "line", "name": "Theo", "role": "Line editor", "stage": "prose", "img": "img/staff/line.png",
     "does": "Listens to how it sounds: the voice, the dialogue and the habits in the sentences.",
     "lens": "You are her line editor. Listen to how it sounds: rhythm, voice and the way people talk."},
    {"id": "copy", "name": "Pearl", "role": "Copy editor", "stage": "polish", "img": "img/staff/copy.png",
     "does": "Catches grammar, spelling and style slips, keeps the style sheet, and proofreads last of all.",
     "lens": "You are her copy editor. Look at grammar and usage, and name the rule for anything you point at."},
    {"id": "reader", "name": "Sam", "role": "First reader", "stage": "out", "img": "img/staff/reader.png",
     "does": "Reads the finished book as a stranger would, and helps get it out the door.",
     "lens": "You are her first reader. Say how a reader coming to it for the first time takes it."},
    {"id": "board", "name": "Kit", "role": "Storyboard artist", "stage": None, "img": "img/staff/board.png",
     "does": "Lays the book out as panels, the way a film or picture-book artist would.",
     "lens": ""},
    {"id": "art", "name": "Wren", "role": "Illustrator", "stage": None, "img": "img/staff/art.png",
     "does": "Paints the storyboard panels, through Maps, when you ask.",
     "lens": ""},
]
BY_ID = {s["id"]: s for s in STAFF}
BY_STAGE = {s["stage"]: s["id"] for s in STAFF if s["stage"]}
IDS = [s["id"] for s in STAFF]
PASSES = [s["id"] for s in STAFF if s["stage"] and s["stage"] != "any"]     # a desk she can send over the whole book
ASKABLE = [s["id"] for s in STAFF if s["lens"]]
CHIEF = "chief"


def of_check(check: Any) -> str:
    """The staff id whose desk a check is on, by its stage. A check of no known stage is the chief's."""
    stage = check.get("stage") if isinstance(check, dict) else None
    return BY_STAGE.get(stage or "any", CHIEF)


def checks_of(sid: str, checks: Any) -> list[dict]:
    """One staff member's checks, in each check's order. ``checks`` is ``engine.load_checks()`` or a list of checks."""
    rows = list(checks.values()) if isinstance(checks, dict) else list(checks or [])
    return sorted((c for c in rows if isinstance(c, dict) and of_check(c) == sid), key=lambda c: int(c.get("order") or 50))


def public(sid: str) -> dict:
    """What the page and the chat may show of a staff member."""
    s = BY_ID[sid]
    return {k: s[k] for k in ("id", "name", "role", "does", "img")}


def name(sid: str) -> str:
    return BY_ID[sid]["name"] if sid in BY_ID else BY_ID[CHIEF]["name"]
