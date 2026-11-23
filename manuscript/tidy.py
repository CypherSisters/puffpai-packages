"""Tidying one book's folder, by code, with no brain call (CONTRACT.md, 0.4.1).

What it removes, and only this: a cache entry for a section text that is no longer in the book, a picture file no record in
Maps' ``pictures.json`` names, a temp or part file left by a stop, a notes file holding nothing but its heading, and a
views, corrections or desk file with nothing in it. Each is one delete, so a crash leaves nothing half-done, and the next
tidy goes on. A name it doesn't know stays, and so does anything of the kind above when it can't be sure: an unreadable
section keeps the whole cache, an unreadable ``pictures.json`` keeps every picture.

What stays: her original, the sections, her notes, the bible and its base, corrections, ``desk.json`` (Editor's Desk
carries it over once), ``editors-desk.json`` (what that app is doing now), views, boards, the Removed folder, and every
picture a record names, older paintings too. Maps' Paint again keeps the old record and file beside the new one, so an old
painting is still named and is hers to delete.

Editor's Desk keeps a section's older versions in its own data folder, not here, so the tidy can't see them. The entry for
the text a section has now always stays, as Editor's Desk's "changed since" notice needs it. An older version's entry goes;
a version she restores later is simply read again, that one section, at the next reading.

The caller holds the app's lock and has checked that no reading is running on this book. Maps and Editor's Desk are
other processes: a file younger than :data:`SETTLED_S` may be one of theirs on its way in, so it waits for a later tidy.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from analysis import CACHE, read_json

PICTURES = "pictures.json"                     # Maps' records (maps/server.py)
PICTURE_DIR = "pictures"
SETTLED_S = 3600
CACHE_ENTRY = re.compile(r"^[0-9a-f]{64}\.json$")
PICTURE_FILE = re.compile(r"^p-[0-9]{8}-[0-9]{6}-[a-z]+(-[0-9]+)?\.png$")   # Maps' PICTURE_ID
EMPTY_RECORDS = {"views.json": "views", "corrections.json": "corrections", "desk.json": "items"}
NOTE_MOST = 200                                # a heading alone is shorter than this; a longer file is not read
BIG_PICTURES = 200 * 1024 * 1024               # past this the line on the Settings fold says so


def _settled(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime >= SETTLED_S
    except OSError:
        return False


def _gone(path: Path) -> int:
    """One delete. A file in use on Windows just waits for the next tidy."""
    try:
        path.unlink()
        return 1
    except OSError:
        return 0


def _cache(folder: Path, sections: list[Path], shas: list[Any], now: float) -> int:
    """Entries for a text still in the book stay: the sections as they are on disk and as the index says. A re-read's
    work is in these. Anything else, an older version in Editor's Desk's folder too, goes once it has settled."""
    cache = folder / CACHE
    if not cache.is_dir() or not sections:
        return 0
    keep = {str(s) for s in shas}
    try:
        for path in sections:
            keep.add(hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        return 0
    return sum(_gone(p) for p in cache.iterdir()
               if p.is_file() and CACHE_ENTRY.match(p.name) and p.stem not in keep and _settled(p, now))


def _pictures(folder: Path, now: float) -> int:
    """The folder is listed before the records are read: Maps writes the record first and the file after it."""
    base = folder / PICTURE_DIR
    if not base.is_dir():
        return 0
    files = [p for p in base.iterdir() if p.is_file() and PICTURE_FILE.match(p.name)]
    got = read_json(folder / PICTURES)
    pics = got.get("pictures") if isinstance(got, dict) else None
    if not isinstance(pics, list):
        return 0
    named = {str(p.get("id")) for p in pics if isinstance(p, dict)}
    return sum(_gone(p) for p in files if p.stem not in named and _settled(p, now))


def _temps(folder: Path, sections_dir: str, now: float) -> int:
    out = 0
    for base in (folder, folder / CACHE, folder / PICTURE_DIR, folder / sections_dir):
        if base.is_dir():
            out += sum(_gone(p) for p in base.iterdir()
                       if p.is_file() and (p.name.startswith(".tmp-") or p.suffix in (".tmp", ".part")) and _settled(p, now))
    return out


def _notes(folder: Path, headings: dict[str, tuple[str, ...]]) -> int:
    """A notes file holding nothing she wrote: empty, or the one heading the app itself put there."""
    out = 0
    for name, said in headings.items():
        path = folder / name
        try:
            if path.stat().st_size > NOTE_MOST:
                continue
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not text or text in {f"# {h}" for h in said}:
            out += _gone(path)
    return out


def _empty_records(folder: Path) -> int:
    out = 0
    for name, key in EMPTY_RECORDS.items():
        if read_json(folder / name) == {key: []}:
            out += _gone(folder / name)
    return out


def tidy(folder: Path, sections_dir: str, sections: list[Path], shas: list[Any], headings: dict[str, tuple[str, ...]],
         now: float | None = None) -> dict[str, int]:
    """One book. Answers how many of each it removed."""
    at = time.time() if now is None else now
    return {"cache": _cache(folder, sections, shas, at), "pictures": _pictures(folder, at),
            "temps": _temps(folder, sections_dir, at), "notes": _notes(folder, headings), "records": _empty_records(folder)}


def _older_paintings(folder: Path) -> int:
    """The bytes of pictures Paint again replaced: each subject's newest ready picture is the one Maps shows, and the
    ready ones after it in ``pictures.json`` (newest first) stay on disk, unseen."""
    got = read_json(folder / PICTURES)
    pics = got.get("pictures") if isinstance(got, dict) else None
    seen, out = set(), 0
    for p in pics if isinstance(pics, list) else []:
        if not isinstance(p, dict) or p.get("state") != "ready":
            continue
        kind = p.get("kind")
        slot = kind if kind in ("worldmap", "cover") else (kind, p.get("who"))
        if slot in seen:
            try:
                out += (folder / PICTURE_DIR / f"{p.get('id')}.png").stat().st_size
            except OSError:
                pass
        seen.add(slot)
    return out


def usage(folder: Path) -> dict[str, int]:
    """Everything in the book's folder, Maps' share and Editor's Desk's one small file too. Only reads."""
    total = pictures = files = 0
    for here, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
        for name in names:
            try:
                size = os.path.getsize(os.path.join(here, name))
            except OSError:
                continue
            total += size
            files += 1
            if Path(here) == folder / PICTURE_DIR:
                pictures += size
    return {"bytes": total, "pictures": pictures, "older": _older_paintings(folder), "files": files}


def size_words(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


def usage_line(used: dict[str, int]) -> str:
    """"This book uses 4.3 MB: pictures 3.9 MB." and, when it is so, what is worth her knowing about the pictures."""
    if not used["pictures"]:
        return f"This book uses {size_words(used['bytes'])}."
    line = f"This book uses {size_words(used['bytes'])}: pictures {size_words(used['pictures'])}."
    if used["older"]:
        line += f" Of that, {size_words(used['older'])} is older paintings that Paint again replaced. Maps still keeps them."
    if used["pictures"] >= BIG_PICTURES:
        line += " Pictures are taking a lot of room. They are never deleted for you; that stays your choice."
    return line
