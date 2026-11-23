"""Italics and bold in a section's text. A section file keeps them the plain way: ``*italic*``, ``**bold**`` and
``***both***``, inside one paragraph (one line). The page draws them, a Word copy keeps them, and nothing that reads the
book for sense (a brain ask, a finder, a quote check) ever sees a star: those take ``strip(text)``.

A line that is only stars and spaces is a scene break and is never touched. A star with a space beside it is a star.

The same file sits in Manuscript and in Editor's Desk, since a package cannot import a sibling. Keep the two equal.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re

# three stars before two before one, so ***both*** is one piece; the words inside never start or end with a space
_MARK = re.compile(r"(\*{1,3})(?=[^\s*])([^*\n]*?[^\s*]|[^\s*])\1(?!\*)")
_BREAK = re.compile(r"^[\s*]*$")


def runs(line: str) -> list[tuple[str, bool, bool]]:
    """One paragraph as (words, italic, bold) pieces, in order. Joined, the words are ``strip(line)``."""
    if _BREAK.match(line):
        return [(line, False, False)] if line else []
    out: list[tuple[str, bool, bool]] = []
    at = 0
    for m in _MARK.finditer(line):
        if m.start() > at:
            out.append((line[at:m.start()], False, False))
        n = len(m.group(1))
        out.append((m.group(2), n in (1, 3), n in (2, 3)))
        at = m.end()
    if at < len(line):
        out.append((line[at:], False, False))
    return out


def strip(text: str) -> str:
    """The words alone: every italic and bold mark gone, scene breaks and every line break as they were."""
    return "\n".join("".join(r[0] for r in runs(line)) for line in text.split("\n"))


def wrap(pieces: list[tuple[str, bool, bool]]) -> str:
    """(words, italic, bold) pieces back into one marked paragraph. Spaces at the edge of a marked piece move outside
    its stars, so the marks always read back; neighbours with the same marks become one piece."""
    merged: list[list] = []
    for words, italic, bold in pieces:
        if not words:
            continue
        if merged and merged[-1][1] == italic and merged[-1][2] == bold:
            merged[-1][0] += words
        else:
            merged.append([words, italic, bold])
    out = []
    for words, italic, bold in merged:
        stars = "*" * ((1 if italic else 0) + (2 if bold else 0))
        core = words.strip()
        if not stars or not core or "*" in core:
            out.append(words)
            continue
        lead = words[:len(words) - len(words.lstrip())]
        tail = words[len(words.rstrip()):]
        out.append(f"{lead}{stars}{core}{stars}{tail}")
    return "".join(out)
