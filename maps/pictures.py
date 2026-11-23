"""Maps pictures: prompts from the story bible, and one picture program (ComfyUI) on the home network that paints them.

Nothing here reads the manuscript text. Every prompt is built from bible facts only. First the text's own evidence
when the bible has it (``looks`` and ``age_words`` of a character, ``details`` of a place, ``geography`` and
``setting.look``, each with its section), then traits, roles, kinds of places, place notes, an event's own short line,
theme names and the one-line summary, with the characters' names taken out
(in a scene, each name becomes a plain description like "the curious person"), the writer's note for that picture
when she gave one, and a style tail she can change. Each prompt keeps a list of the evidence it used, its proof,
shown under the picture. The bot's chat model never makes a picture; the picture program does, on another computer.

Calls to the picture program go through ``urllib`` with the proxy settings the Core gives this process, so the
Bridge sees and allows each one. Nothing here bypasses it.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

KINDS = {"portrait": "Portrait", "place": "Place", "worldmap": "World map", "scene": "Scene", "cover": "Cover"}
DEFAULT_STYLE = "painterly, soft light, book illustration"
DEFAULT_PORT = 8188
PROMPT_MAX = 900
STYLE_MAX = 200
NOTE_MAX = 200
NEGATIVE = "text, letters, watermark, signature, blurry, deformed, extra limbs"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MOST_JSON = 64 * 1024 * 1024                  # object_info is big on a ComfyUI with many custom nodes
MOST_PNG = 64 * 1024 * 1024

# words about the setting, looked for in world rules, place kinds and notes, and the one-line summary
SETTING_WORDS = (
    "moon", "planet", "space", "spaceship", "starship", "station", "colony", "dome", "mining", "salt", "brine",
    "desert", "sea", "ocean", "island", "coast", "harbor", "harbour", "river", "lake", "forest", "jungle", "swamp",
    "mountain", "mountains", "valley", "ice", "snow", "tundra", "city", "town", "village", "farm", "castle", "kingdom",
    "palace", "temple", "tower", "cathedral", "medieval", "victorian", "regency", "steam", "steampunk", "industrial",
    "factory", "railway", "modern", "suburban", "future", "futuristic", "cyberpunk", "ancient", "roman", "viking",
    "magic", "wizard", "dragon", "robot", "robots", "android", "frontier", "western", "war", "underwater", "cave",
    "caves", "tunnels", "wasteland", "ship", "airship")
AGE_WORDS = (
    "child", "baby", "boy", "girl", "teen", "teenage", "teenager", "young", "youth", "old", "elderly", "aged",
    "ancient", "grandmother", "grandfather", "grandma", "grandpa", "mother", "father", "aunt", "uncle", "widow",
    "middle-aged", "adult")
AGE_NOUNS = (
    "child", "baby", "boy", "girl", "teen", "teenager", "youth", "grandmother", "grandfather", "grandma", "grandpa",
    "mother", "father", "aunt", "uncle", "widow", "adult")
TIME_WORDS = (
    "dawn", "sunrise", "morning", "noon", "midday", "afternoon", "evening", "dusk", "sunset", "twilight", "night",
    "midnight", "supper", "dinner", "breakfast", "lunch", "tide", "winter", "spring", "summer", "autumn")
NEW_FACE = re.compile(r"\b(new|different|another)\s+(face|seed|person|look)\b", re.IGNORECASE)
ROLE_WORDS = {"protagonist": "the main character", "antagonist": "the antagonist", "ally": "an ally",
              "supporting": "a supporting character"}


class PictureError(Exception):
    """A plain line for the owner about why a picture could not be made."""


class NoModel(PictureError):
    pass


def _s(v: Any, n: int = 400) -> str:
    return " ".join(str(v or "").split())[:n]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z'-]*", text.lower())


# ---------------------------------------------------------------- the address

_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10"))
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _local_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        return host == "localhost" or (len(labels) >= 2 and labels[-1] in ("local", "lan")
                                       and all(_LABEL.match(x) for x in labels))
    if ip.version == 6 and ip.ipv4_mapped:
        return False
    return any(ip in net for net in _LOCAL_NETS)


def check_address(value: Any) -> str:
    """The picture program's base address, ``http://host:port``, or a plain ValueError. Home network only."""
    text = _s(value, 200)
    if not text:
        raise ValueError("Type the picture program's address, like http://192.168.1.20:8188.")
    if " " in text:
        raise ValueError("An address has no spaces in it.")
    if "://" not in text:
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("The address starts with http:// or https://.")
    if "@" in parts.netloc or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("Type only the address and port, like http://192.168.1.20:8188.")
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError:
        raise ValueError("The port is a number from 1 to 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("The port is a number from 1 to 65535.")
    if not host or not _local_host(host):
        raise ValueError("Maps only reaches a picture program on your home network: an address like 192.168.x.x, "
                         "10.x.x.x, 172.16 to 172.31, or a name ending in .local or .lan.")
    netloc = f"[{host}]" if ":" in host else host
    return f"{parts.scheme.lower()}://{netloc}:{port}"


# ---------------------------------------------------------------- prompts from the bible

def _people_tokens(bible: dict) -> set[str]:
    """Every word of every character's name and alias that starts with a capital: these never go in a prompt."""
    out: set[str] = set()
    for c in bible.get("characters") or []:
        if not isinstance(c, dict):
            continue
        for name in [c.get("name")] + list(c.get("aliases") or []):
            for w in re.findall(r"[A-Za-z][A-Za-z'-]*", str(name or "")):
                if w[0].isupper():
                    out.add(w.lower())
    return out


def _scrub(text: str, people: set[str]) -> str:
    """The words of *text* without any character's name (also as a plural or with 's)."""
    keep = []
    for w in str(text or "").split():
        bare = re.sub(r"[^A-Za-z'-]", "", w).lower()
        stem = re.sub(r"('s|s')$", "", bare)
        if bare and (bare in people or stem in people or (stem.endswith("s") and stem[:-1] in people)):
            continue
        keep.append(w)
    return " ".join(keep)


def setting_words(bible: dict) -> list[str]:
    """The setting words found in the bible's world rules, places and one-line summary, in the order found."""
    texts = [str(r.get("rule") or "") for r in bible.get("world_rules") or [] if isinstance(r, dict)]
    for p in bible.get("places") or []:
        if isinstance(p, dict):
            texts += [str(p.get("kind") or ""), str(p.get("notes") or "")]
    summary = bible.get("summary") if isinstance(bible.get("summary"), dict) else {}
    texts.append(str(summary.get("one_line") or ""))
    for key in ("period", "setting", "technology", "era"):      # a later bible may carry these plainly
        if isinstance(bible.get(key), str):
            texts.insert(0, bible[key])
    found: list[str] = []
    for t in texts:
        for w in _words(t):
            if w in SETTING_WORDS and w not in found:
                found.append(w)
    return found[:8]


def _tail(style: Any) -> str:
    return _s(style, STYLE_MAX) or DEFAULT_STYLE


def _finish(parts: list[str], style: Any, people: set[str], note: Any = "") -> str:
    """The prompt: the parts, then the writer's note (when there is one), then the style. The parts are cut first
    when it is too long, so the note and the style always travel."""
    body = "; ".join(p for p in (_scrub(x, people).strip(" ;,") for x in parts) if p)
    said = _scrub(_s(note, NOTE_MAX), people).strip(" .;,")
    tail = (said + ". " if said else "") + _tail(style)
    return body[:max(PROMPT_MAX - len(tail) - 2, 0)].rstrip() + ". " + tail


def _setting(bible: dict) -> str:
    words = setting_words(bible)
    return ("setting: " + ", ".join(words)) if words else ""


def find_character(bible: dict, who: Any) -> dict | None:
    want = _s(who, 120).lower()
    if not want:
        return None
    chars = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    for c in chars:
        names = [c.get("id"), c.get("name")] + list(c.get("aliases") or [])
        if want in (_s(n, 120).lower() for n in names):
            return c
    near = [c for c in chars if want in _s(c.get("name"), 120).lower()]
    return near[0] if len(near) == 1 else None


def find_place(bible: dict, where: Any) -> dict | None:
    want = _s(where, 120).lower()
    if not want:
        return None
    places = [p for p in bible.get("places") or [] if isinstance(p, dict)]
    for p in places:
        if want in (_s(p.get("id"), 120).lower(), _s(p.get("name"), 120).lower()):
            return p
    near = [p for p in places if want in _s(p.get("name"), 120).lower()]
    return near[0] if len(near) == 1 else None


# ---------------------------------------------------------------- the evidence: what the text says, with sections

def _sec(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def evidence(v: Any, most: int) -> list[dict]:
    """``[{detail, section, quote}]`` from a bible list (``looks``, ``details``, ``setting.look``), in order."""
    out: list[dict] = []
    for e in v if isinstance(v, list) else []:
        if isinstance(e, str):
            e = {"detail": e}
        if not isinstance(e, dict):
            continue
        detail = _s(e.get("detail") or e.get("words") or e.get("text"), 120).strip(" .;,")
        if detail and not any(x["detail"].lower() == detail.lower() for x in out):
            out.append({"detail": detail, "section": _sec(e.get("section")), "quote": _s(e.get("quote"), 120)})
    return out[:most]


def age_entry(v: Any) -> dict | None:
    """The text's own words on a character's age: a string, ``{words or detail, section, quote}``, or a list."""
    got = evidence(v if isinstance(v, list) else [v], 1)
    return got[0] if got else None


def _proof(owner: str, entries: list[dict]) -> list[dict]:
    return [{"owner": owner, "detail": e["detail"], "section": e.get("section"), "quote": e.get("quote") or ""}
            for e in entries]


def proof_in(proof: list[dict], prompt: str, people: set[str] | None = None) -> list[dict]:
    """Only the entries whose words reached *prompt* (a long prompt is cut at the end), in order."""
    low = str(prompt or "").lower()
    keep = []
    for e in proof:
        said = _scrub(str(e.get("detail") or ""), people or set()).strip(" ;,").lower()
        if said and said in low:
            keep.append(e)
    return keep


def has_sections(proof: list[dict]) -> bool:
    return any(isinstance(e.get("section"), int) for e in proof or [])


def proof_line(proof: list[dict]) -> str:
    """One line for the chat: "From the text: grey hair pulled back (section 1), ...", or the bible facts used."""
    if not proof:
        return ""
    if not has_sections(proof):
        return "From the story bible, without sections yet: " + ", ".join(_s(e.get("detail"), 120) for e in proof) + "."

    def bit(e: dict) -> str:
        return _s(e.get("detail"), 120) + (f" (section {e['section']})" if isinstance(e.get("section"), int) else "")
    owners: list[Any] = []
    for e in proof:
        if e.get("owner") not in owners:
            owners.append(e.get("owner"))
    if len(owners) == 1:
        return "From the text: " + ", ".join(bit(e) for e in proof) + "."
    return "From the text: " + "; ".join(f"{o}: " + ", ".join(bit(e) for e in proof if e.get("owner") == o)
                                         for o in owners) + "."


LINK_WORDS = ("from", "of", "to", "above", "below", "beyond", "near", "past", "across", "under", "behind", "beside",
              "over", "toward", "towards", "around", "inside", "outside", "beneath")


def geography(bible: dict) -> list[dict]:
    """The bible's distances and directions as relations: ``[{from, to, from_name, to_name, relation, note, section,
    quote}]``. "Brine Station is two hours on foot from the salt flats"; a note that names both places stays as the
    text says it."""
    out = []
    for g in bible.get("geography") or []:
        if not isinstance(g, dict):
            continue
        note = _s(g.get("note"), 160).strip(" .;,")
        if not note:
            continue
        a, b = find_place(bible, g.get("from")), find_place(bible, g.get("to"))
        an = _s(a.get("name"), 80) if a else _s(g.get("from"), 80)
        bn = _s(b.get("name"), 80) if b else _s(g.get("to"), 80)
        low = note.lower()
        if (not an or an.lower() in low) and (not bn or bn.lower() in low):
            rel = note
        elif an and bn and bn.lower() in low:
            rel = f"{an} is {note}"
        elif an and bn:
            rel = f"{an} is {note} {bn}" if low.split()[-1] in LINK_WORDS else f"{an} is {note} from {bn}"
        else:
            rel = f"{an or bn}: {note}"
        out.append({"from": _s(a.get("id"), 120) if a else "", "to": _s(b.get("id"), 120) if b else "",
                    "from_name": an, "to_name": bn, "relation": rel, "note": note,
                    "section": _sec(g.get("section")), "quote": _s(g.get("quote"), 120)})
    return out


def setting_look(bible: dict) -> list[dict]:
    setting = bible.get("setting") if isinstance(bible.get("setting"), dict) else {}
    return evidence(setting.get("look"), 12)


def _told(parts: list[str], style: Any, people: set[str], note: Any, proof: list[dict]) -> tuple[str, list[dict]]:
    prompt = _finish(parts, style, people, note)
    return prompt, proof_in(proof, prompt, people)


def _portrait(bible: dict, c: dict, style: Any = "", note: Any = "") -> tuple[str, list[dict]]:
    """The text's looks first, every one in order, then its words on age, then the role and traits as before."""
    people = _people_tokens(bible)
    owner = _s(c.get("name"), 120)
    looks = evidence(c.get("looks"), 12)
    age = age_entry(c.get("age_words"))
    traits = [_s(t, 40) for t in c.get("traits") or [] if _s(t, 40)][:6]
    age_from = " ".join([str(c.get("age") or "")] + [str(a) for a in c.get("aliases") or []] + traits)
    ages = []
    for w in _words(age_from):
        if w in AGE_WORDS and w not in ages:
            ages.append(w)
    if isinstance(c.get("age"), (int, float)) or re.fullmatch(r"\d{1,3}", str(c.get("age") or "")):
        ages.insert(0, f"{int(c['age'])} years old")
    who = "a character portrait of " + (" ".join(ages[:3]) + " " if ages else "") + "a person"
    role = ROLE_WORDS.get(_s(c.get("role"), 30).lower(), "")
    lead = ([", ".join(x["detail"] for x in looks)] if looks else []) + ([age["detail"]] if age else [])
    parts = lead + [who, role, ", ".join(traits), _setting(bible), "head and shoulders, looking at the viewer"]
    proof = _proof(owner, looks + ([age] if age else []))
    if not proof:                                   # an older reading: the role and traits are all there is
        proof = _proof(owner, [{"detail": d} for d in ([role] if role else []) + traits])
    return _told(parts, style, people, note, proof)


def portrait_prompt(bible: dict, c: dict, style: Any = "", note: Any = "") -> str:
    return _portrait(bible, c, style, note)[0]


def _place(bible: dict, p: dict, style: Any = "", note: Any = "") -> tuple[str, list[dict]]:
    """What the text says the place looks, sounds and smells like; its kind and notes when the text gave no details."""
    people = _people_tokens(bible)
    owner = _s(p.get("name"), 120)
    kind = _s(p.get("kind"), 60)
    details = evidence(p.get("details"), 10)
    lead = "a painting of a place" + (f", a {kind}" if kind else "")
    if details:
        parts = [lead, ", ".join(x["detail"] for x in details), _setting(bible),
                 "wide view, no people in the foreground"]
        proof = _proof(owner, details)
    else:
        notes = _s(p.get("notes"), 300)
        parts = [lead, notes, _setting(bible), "wide view, no people in the foreground"]
        proof = _proof(owner, [{"detail": d} for d in (kind, notes.strip(" .;,")) if d])
    return _told(parts, style, people, note, proof)


def place_prompt(bible: dict, p: dict, style: Any = "", note: Any = "") -> str:
    return _place(bible, p, style, note)[0]


def _worldmap(bible: dict, style: Any = "", note: Any = "") -> tuple[str, list[dict]]:
    """The places and their kinds, then the text's distances and directions as relations, then the setting's look."""
    people = _people_tokens(bible)
    shown = []
    for p in bible.get("places") or []:
        if isinstance(p, dict) and (_s(p.get("name"), 60) or _s(p.get("kind"), 40)):
            kind = _s(p.get("kind"), 40)
            shown.append(_s(p.get("name"), 60) + (f" ({kind})" if kind else ""))
    what = ", ".join(shown[:12]) or "the lands of the story"
    geo = geography(bible)
    look = setting_look(bible)
    parts = ["a hand-drawn fantasy map showing " + what, "; ".join(g["relation"] for g in geo),
             ("the look of the world: " + ", ".join(x["detail"] for x in look)) if look else "", _setting(bible),
             "seen from above, parchment, ink lines, compass rose"]
    proof = [{"owner": "world map", "detail": g["relation"], "section": g["section"], "quote": g["quote"]}
             for g in geo] + _proof("world map", look)
    if not proof:
        proof = _proof("world map", [{"detail": d} for d in shown[:12]])
    return _told(parts, style, people, note, proof)


def worldmap_prompt(bible: dict, style: Any = "", note: Any = "") -> str:
    return _worldmap(bible, style, note)[0]


def cover_prompt(bible: dict, style: Any = "", note: Any = "") -> str:
    people = _people_tokens(bible)
    summary = bible.get("summary") if isinstance(bible.get("summary"), dict) else {}
    themes = [_s(t.get("name"), 60) for t in bible.get("themes") or [] if isinstance(t, dict) and _s(t.get("name"), 60)]
    parts = ["a book cover illustration", _s(summary.get("one_line"), 300),
             ("themes: " + ", ".join(themes[:4])) if themes else "", _setting(bible),
             "tall format, room at the top for a title, no lettering"]
    return _finish(parts, style, people, note)


def event_list(bible: dict) -> list[dict]:
    """The bible's events with an id each (``e1``, ``e2``, ... where the bible gives none), in the order given."""
    out = []
    for i, e in enumerate(bible.get("events") or []):
        if isinstance(e, dict) and _s(e.get("what"), 300):
            out.append({**e, "id": _s(e.get("id"), 80) or f"e{i + 1}"})
    return out


def find_event(bible: dict, which: Any) -> dict | None:
    """An event by its id, by its words, or by a few of its words that only one event has."""
    want = _s(which, 300).lower().strip(" .")
    if not want:
        return None
    events = event_list(bible)
    for e in events:
        if want in (e["id"].lower(), _s(e.get("what"), 300).lower().strip(" .")):
            return e
    near = [e for e in events if want in _s(e.get("what"), 300).lower()]
    return near[0] if len(near) == 1 else None


def describe(c: dict) -> tuple[str, str]:
    """A character without their name: ``("curious", "mother")``, the first trait and an age word or "person"."""
    trait = next((_s(t, 30) for t in c.get("traits") or [] if _s(t, 30)), "")
    words = _words(" ".join([str(c.get("age") or "")] + [str(a) for a in c.get("aliases") or []]
                            + [str(t) for t in c.get("traits") or []]))
    noun = next((w for w in words if w in AGE_NOUNS), "person")
    return trait, noun


def _person(c: dict, article: str) -> str:
    trait, noun = describe(c)
    if article == "a":
        article = "an" if (trait or noun)[:1].lower() in "aeiou" else "a"
    return f"{article} {trait + ' ' if trait else ''}{noun}"


def _unnamed(text: str, bible: dict) -> str:
    """The event's words with each character's name put as a plain description ("the curious person")."""
    owner: dict[str, dict | None] = {}
    for c in bible.get("characters") or []:
        if not isinstance(c, dict):
            continue
        for name in [c.get("name")] + list(c.get("aliases") or []):
            for w in re.findall(r"[A-Za-z][A-Za-z'-]*", str(name or "")):
                if w[0].isupper():
                    key = w.lower()
                    owner[key] = c if owner.get(key, c) is c else None        # a shared surname belongs to no one
    out: list[str] = []
    last: dict | None = None
    for w in str(text or "").split():
        bare = re.sub(r"[^A-Za-z'-]", "", w)
        stem = re.sub(r"('s|s'|')$", "", bare.lower())
        key = stem if stem in owner else stem[:-1] if stem.endswith("s") and stem[:-1] in owner else None
        if key is None or not bare[:1].isupper():
            out.append(w)
            last = None
            continue
        c = owner[key]
        tail = ("'s" if re.search(r"('s|s')", bare.lower()[len(stem):] or "") else "") + re.sub(r"[A-Za-z'-]", "", w)
        if c is not None and c is last and out:              # the second word of the same name
            out[-1] = out[-1].rstrip(".,;:!?") + tail
            continue
        out.append((_person(c, "the") if c else "the family") + tail)
        last = c
    return " ".join(out)


SCENE_LOOKS = 4
SCENE_DETAILS = 4


def _scene(bible: dict, e: dict, style: Any = "", note: Any = "") -> tuple[str, list[dict]]:
    """One event as a picture: what happens in plain words, who is there by description and by the text's looks
    (never by name), where and what the text says it looks like, and the time of day when the bible's words say."""
    people = _people_tokens(bible)
    chars = [c for c in bible.get("characters") or [] if isinstance(c, dict)]
    present = []
    for ref in e.get("who") or []:
        c = find_character({"characters": chars}, ref)
        if c is not None and c not in present:
            present.append(c)
    present = present[:5]
    proof: list[dict] = []
    who = []
    for c in present:
        looks = evidence(c.get("looks"), SCENE_LOOKS)
        who.append(_person(c, "a") + (" (" + ", ".join(x["detail"] for x in looks) + ")" if looks else ""))
        proof += _proof(_s(c.get("name"), 120), looks)
    place = find_place(bible, e.get("where"))
    where = ""
    if place:
        kind = _s(place.get("kind"), 40)
        details = evidence(place.get("details"), SCENE_DETAILS)
        if details:
            said = ", ".join(x["detail"] for x in details)
            proof += _proof(_s(place.get("name"), 120), details)
        else:
            said = _s(place.get("notes"), 300).split(". ")[0].strip(" .")
        where = "at " + (f"a {kind}" if kind else "a place") + (f", {said}" if said else "")
    times = []
    for w in _words(_s(e.get("when"), 120) + " " + _s(e.get("what"), 300)):
        if w in TIME_WORDS and w not in times:
            times.append(w)
    parts = ["a story illustration of one moment: " + _unnamed(_s(e.get("what"), 300), bible).strip(" ."),
             ("people there: " + ", ".join(who)) if who else "",
             where, ("time: " + ", ".join(times[:3])) if times else "", _setting(bible),
             "wide scene, no text"]
    if not proof:                                   # an older reading: the traits that describe who is there
        for c in present:
            trait = describe(c)[0]
            if trait:
                proof.append({"owner": _s(c.get("name"), 120), "detail": trait, "section": None, "quote": ""})
        if place and _s(place.get("kind"), 40):
            proof.append({"owner": _s(place.get("name"), 120), "detail": _s(place.get("kind"), 40), "section": None,
                          "quote": ""})
    return _told(parts, style, people, note, proof)


def scene_prompt(bible: dict, e: dict, style: Any = "", note: Any = "") -> str:
    return _scene(bible, e, style, note)[0]


def make_prompt(bible: dict, kind: str, who: Any, style: Any = "", note: Any = "") -> tuple[str, dict]:
    """``(prompt, {who, who_name, section})`` for one picture, or a PictureError in plain words. *note* is the
    writer's own words for this picture ("older, grey hair"); it goes in before the style."""
    prompt, about, _used = prompt_and_proof(bible, kind, who, style, note)
    return prompt, about


def prompt_and_proof(bible: dict, kind: str, who: Any, style: Any = "", note: Any = "") -> tuple[str, dict, list]:
    """``(prompt, about, proof)``: make_prompt's two, and the evidence the prompt was built from, in the order used:
    ``[{owner, detail, section, quote}]``. The entries have no section when the bible has no evidence fields."""
    if kind == "scene":
        e = find_event(bible, who)
        if e is None:
            some = "; ".join(_s(x.get("what"), 60) for x in event_list(bible)[:5])
            raise PictureError("Say which event to paint, by its words or its id." +
                               (f" Some in the story bible: {some}." if some else " The story bible has no events yet."))
        sec = e.get("section") if isinstance(e.get("section"), int) else None
        prompt, proof = _scene(bible, e, style, note)
        return prompt, {"who": e["id"], "who_name": _s(e.get("what"), 80), "section": sec}, proof
    if kind == "portrait":
        c = find_character(bible, who)
        if c is None:
            names = ", ".join(_s(x.get("name"), 60) for x in bible.get("characters") or [] if isinstance(x, dict))
            raise PictureError("Say which character to paint." + (f" These are in the story bible: {names}." if names
                                                                  else " The story bible has no characters yet."))
        first = c.get("first_seen") if isinstance(c.get("first_seen"), int) else None
        if first is None:
            seen = [n for n in c.get("appearances") or [] if isinstance(n, int)]
            first = min(seen) if seen else None
        prompt, proof = _portrait(bible, c, style, note)
        return prompt, {"who": _s(c.get("id"), 120), "who_name": _s(c.get("name"), 120), "section": first}, proof
    if kind == "place":
        p = find_place(bible, who)
        if p is None:
            names = ", ".join(_s(x.get("name"), 60) for x in bible.get("places") or [] if isinstance(x, dict))
            raise PictureError("Say which place to paint." + (f" These are in the story bible: {names}." if names
                                                              else " The story bible has no places yet."))
        seen = [n for n in p.get("appearances") or [] if isinstance(n, int)]
        prompt, proof = _place(bible, p, style, note)
        return prompt, {"who": _s(p.get("id"), 120), "who_name": _s(p.get("name"), 120),
                        "section": min(seen) if seen else None}, proof
    if kind == "worldmap":
        prompt, proof = _worldmap(bible, style, note)
        return prompt, {"who": "", "who_name": "", "section": None}, proof
    if kind == "cover":
        return cover_prompt(bible, style, note), {"who": "", "who_name": "", "section": None}, []
    raise PictureError("Maps makes five kinds of picture: a portrait, a place, a world map, a scene and a cover.")


# ---------------------------------------------------------------- what the picture program has

def _names_in(info: dict, node: str, field: str) -> list[str]:
    """A loader's list of file names in object_info, in the old form ``[[...]]`` or the new ``["COMBO", {options}]``."""
    try:
        spec = info[node]["input"]["required"][field]
    except (KeyError, TypeError):
        return []
    if isinstance(spec, list) and spec:
        if isinstance(spec[0], list):
            return [str(x) for x in spec[0]]
        if len(spec) > 1 and isinstance(spec[1], dict) and isinstance(spec[1].get("options"), list):
            return [str(x) for x in spec[1]["options"]]
    return []


def choose(info: dict, models: dict[str, list[str]]) -> dict:
    """Which workflow this picture program can run, from its object_info and model lists, or NoModel."""
    gguf_loader = isinstance(info, dict) and "UnetLoaderGGUF" in info
    unets = list(models.get("unet") or []) + _names_in(info, "UnetLoaderGGUF", "unet_name")
    flux = [u for u in unets if u.lower().endswith(".gguf") and "flux" in u.lower()]
    flux.sort(key=lambda u: "schnell" not in u.lower())
    clips = list(models.get("clip") or []) + _names_in(info, "DualCLIPLoader", "clip_name1")
    t5 = next((c for c in clips if "t5" in c.lower()), None)
    clip_l = next((c for c in clips if "clip_l" in c.lower()), None)
    vaes = list(models.get("vae") or []) + _names_in(info, "VAELoader", "vae_name")
    vae = next((v for v in vaes if v.lower() in ("ae.safetensors", "ae.sft")), None) or \
        next((v for v in vaes if "flux" in v.lower() or v.lower().startswith("ae")), None)
    if gguf_loader and flux and t5 and clip_l and vae:
        return {"workflow": "flux-gguf", "unet": flux[0], "t5": t5, "clip_l": clip_l, "vae": vae}
    ckpts = list(models.get("checkpoints") or []) or _names_in(info, "CheckpointLoaderSimple", "ckpt_name")
    if ckpts:
        return {"workflow": "checkpoint", "ckpt": ckpts[0]}
    if flux and not gguf_loader:
        why = "it has a FLUX GGUF model but not the GGUF loader, and no checkpoint"
    elif gguf_loader and flux:
        lack = [n for n, have in (("the t5xxl text encoder", t5), ("clip_l", clip_l), ("the ae VAE", vae)) if not have]
        why = "its FLUX GGUF model is missing " + " and ".join(lack) + ", and it has no checkpoint"
    else:
        why = "it has no FLUX schnell GGUF model and no checkpoint in its checkpoints folder"
    raise NoModel(f"The picture program has no model Maps can use: {why}. Add one there, then Test again.")


def workflow_line(choice: dict) -> str:
    if choice["workflow"] == "flux-gguf":
        return f"FLUX schnell ({choice['unet']}), four steps."
    return f"The checkpoint {choice['ckpt']}."


def size_for(kind: str, choice: dict) -> tuple[int, int]:
    tall = kind in ("portrait", "cover")
    if choice["workflow"] == "flux-gguf":
        if tall:
            return 640, 832
        return (1024, 640) if kind in ("worldmap", "scene") else (768, 768)
    return (512, 768) if tall else (768, 512)


def build_workflow(choice: dict, prompt: str, kind: str, seed: int) -> dict:
    """The API-format workflow (node id -> {class_type, inputs}) for one picture."""
    w, h = size_for(kind, choice)
    save = {"class_type": "SaveImage", "inputs": {"filename_prefix": "puffpai-maps", "images": ["8", 0]}}
    if choice["workflow"] == "flux-gguf":
        return {
            "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": choice["unet"]}},
            "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": choice["t5"], "clip_name2": choice["clip_l"],
                                                             "type": "flux"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": choice["vae"]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
            "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
            "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                                                       "scheduler": "simple", "denoise": 1.0, "model": ["1", 0],
                                                       "positive": ["4", 0], "negative": ["5", 0],
                                                       "latent_image": ["6", 0]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
            "9": save,
        }
    turbo = "turbo" in choice["ckpt"].lower()
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": choice["ckpt"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE, "clip": ["1", 1]}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 4 if turbo else 20, "cfg": 1.0 if turbo else 6.0,
                                                   "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                                                   "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                                                   "latent_image": ["6", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": save,
    }


# ---------------------------------------------------------------- talking to the picture program

class Comfy:
    """One picture program at one address. Uses the proxy settings this process was given (the Bridge)."""

    def __init__(self, base: str, opener: Any = None, timeout: float = 30) -> None:
        self.base = base.rstrip("/")
        self.opener = opener or urllib.request.build_opener()      # honours HTTP_PROXY, HTTPS_PROXY and NO_PROXY
        self.timeout = timeout

    def _open(self, path: str, data: bytes | None = None, most: int = MOST_JSON) -> bytes:
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        what = path.split("?", 1)[0]
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read(most + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 407):
                raise PictureError(f"Not allowed yet: the Bridge did not let Maps reach {self.base}. Say Yes on the "
                                   "Bridge page, and check the Bridge is open.") from None
            raise PictureError(f"The picture program answered {exc.code} to {what}.") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PictureError(f"The picture program did not answer at {self.base}. Check it is running on that "
                               "computer and the address is right.") from None
        if len(raw) > most:
            raise PictureError(f"The picture program sent more than Maps takes for {what}.")
        return raw

    def get_json(self, path: str) -> Any:
        try:
            return json.loads(self._open(path).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PictureError(f"The picture program's answer to {path.split('?', 1)[0]} did not make sense.") from None

    def post_json(self, path: str, obj: Any) -> Any:
        try:
            return json.loads(self._open(path, json.dumps(obj).encode("utf-8")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PictureError(f"The picture program's answer to {path} did not make sense.") from None

    def _list(self, folder: str) -> list[str]:
        try:
            got = self.get_json("/models/" + folder)
        except PictureError:
            return []                               # an older picture program: object_info has the lists
        return [str(x) for x in got] if isinstance(got, list) else []

    def survey(self) -> dict:
        """What the picture program says about itself: its stats, its nodes and its model lists."""
        stats = self.get_json("/system_stats")
        info = self.get_json("/object_info")
        if not isinstance(info, dict):
            raise PictureError("The picture program's list of nodes did not make sense.")
        models = {"unet": self._list("unet"), "clip": self._list("clip") or self._list("text_encoders"),
                  "vae": self._list("vae"), "checkpoints": self._list("checkpoints")}
        return {"stats": stats if isinstance(stats, dict) else {}, "info": info, "models": models}

    def submit(self, workflow: dict, client_id: str) -> str:
        got = self.post_json("/prompt", {"prompt": workflow, "client_id": client_id})
        pid = got.get("prompt_id") if isinstance(got, dict) else None
        if not isinstance(pid, str) or not pid:
            err = got.get("error") if isinstance(got, dict) else None
            msg = err.get("message") if isinstance(err, dict) else err
            raise PictureError("The picture program did not take the picture" + (f": {_s(msg, 200)}." if msg else "."))
        return pid

    def wait(self, pid: str, timeout_s: float, poll_s: float, sleep: Callable[[float], None] = time.sleep) -> dict:
        """The first image of a finished prompt: ``{filename, subfolder, type}``."""
        deadline = time.monotonic() + timeout_s
        while True:
            got = self.get_json("/history/" + urllib.parse.quote(pid, safe=""))
            entry = got.get(pid) if isinstance(got, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if status.get("status_str") == "error":
                    raise PictureError("The picture program stopped with an error while painting it.")
                for out in (entry.get("outputs") or {}).values():
                    for img in (out or {}).get("images") or [] if isinstance(out, dict) else []:
                        if isinstance(img, dict) and img.get("filename"):
                            return img
                if status.get("completed") is True:
                    raise PictureError("The picture program finished but made no picture.")
            if time.monotonic() >= deadline:
                raise PictureError(f"The picture program took longer than {int(timeout_s // 60)} minutes, so Maps "
                                   "stopped waiting.")
            sleep(poll_s)

    def fetch(self, img: dict) -> bytes:
        q = urllib.parse.urlencode({"filename": str(img.get("filename") or ""),
                                    "subfolder": str(img.get("subfolder") or ""),
                                    "type": str(img.get("type") or "output")})
        raw = self._open("/view?" + q, most=MOST_PNG)
        if not raw.startswith(PNG_MAGIC):
            raise PictureError("The picture program sent something that is not a PNG picture.")
        return raw
