# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/security/data_classifier.py (as-is: every pattern and the scan; left out: routing to the vault, big-file chunks, scrub_health_statements)
# Harvested from PuffPAIBuildBot@774e27a4:src/puffpai/artifact_review.py (as-is: _INSTRUCTION_RE, _MODEL_ORDER_RE, fold_for_orders, fold_label, _MODEL_ORDER_MORE_RE, model_orders, reads_like_orders)
"""Two checks Web search runs before a word leaves this computer and before outside text reaches the brain.

They are today's BuildBot's own, copied word for word, so they catch and miss the same things:

* :meth:`DataClassifier.redact_pii`: finds private details in the search words. What it does NOT catch is
  listed below (first names alone, a town alone, a plate or id with no word naming it, most non-English text).
* :func:`reads_like_orders`: a Wikipedia title or snippet that reads like an order to a model.

Python standard library only. Nothing here is kept or logged.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, fields

# ── email / phone ───────────────────────────────────────────────────────────

_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
# A phone number WITH separators or brackets ("555-867-5309", "(415) 555-0132",
# "+1 415 555 0132", "555-0132" is too short and is not matched).
_PHONE_SEPARATED = re.compile(
    r"(?<![\d\-/.])"
    r"(?:\+?\d{1,3}[\s.\-])?"
    r"(?:\(\d{3}\)\s*|\d{3}[\s.\-])"
    r"\d{3}[\s.\-]\d{4}"
    r"(?![\d\-/])(?!\.\d)"
)
# A bare run of 10 or 11 digits ("8005551234"). On its own this is exactly as
# likely to be a serial number, an order id or a model number as a phone, so
# it only counts when a phone word sits just in front of it (see _PHONE_CUE).
_PHONE_BARE = re.compile(r"(?<![\d\-/.])\+?1?\d{10}(?![\d\-/])(?!\.\d)")
# An international number: "+" and a country code, then groups of digits
# separated by spaces, dots, dashes or brackets ("+44 20 7946 0958",
# "+33 1 42 68 53 00", "+81 3-1234-5678", "+49 (0)30 1234567"). The leading
# "+" is the cue: it is how a phone number is written, not a serial. The total
# must be 8 to 15 digits (the international numbering limit).
_PHONE_INTL = re.compile(
    r"(?<![\w+])\+\d{1,3}(?:[\s.\-]?\(?\d{1,5}\)?){2,6}(?![\d\-/])"
)
# A national number with a trunk 0 and separators ("020 7946 0958",
# "07700 900123", "030 12345678"): too close to other digit runs to take on
# its shape alone, so a phone word must be near (see _PHONE_CUE).
_PHONE_TRUNK = re.compile(
    r"(?<![\d\-/.+])0\d{2,4}[\s.\-]\d{3,4}(?:[\s.\-]?\d{3,4})?(?![\d\-/])"
)
_PHONE_CUE = re.compile(
    r"\b(?:phone|cell|mobile|call|text|txt|tel|telephone|number|num|reach|"
    r"contact|dial)\b",
    re.IGNORECASE,
)
# A device / order word in front of ANY digit run vetoes the phone reading,
# whether or not it has separators: "serial 800-555-1234" is a serial.
_NOT_A_PHONE_CUE = re.compile(
    r"\b(?:serial|s/n|sn|model|part|order|invoice|tracking|sku|isbn|"
    r"firmware|version|build|ticket|case|account|confirmation|catalog|"
    r"catalogue|item|product|device\s+id|id)\b",
    re.IGNORECASE,
)
_CUE_WINDOW = 30
#: How far after a national number a phone word may sit ("(mobile)").
_AFTER_CUE_WINDOW = 16

# ── SSN (ported from HBB, with its validation rules) ─────────────────────────

# XXX-XX-XXXX or XXX XX XXXX with a consistent separator; bare 9-digit runs
# only when an SSN word is right in front (the spoken form). Candidates inside a
# longer digit / dash / slash / decimal run are rejected (ZIP+4, decimals).
_SSN_CANDIDATE = re.compile(
    r"(?<![\d\-/])(?<!\d\.)"
    r"(\d{3})([-\s])(\d{2})\2(\d{4})"
    r"(?![\d\-/])(?!\.\d)"
)
_SSN_BARE_CANDIDATE = re.compile(
    r"(?<![\d\-/.])"
    r"(\d{3})()(\d{2})(\d{4})"
    r"(?![\d\-/])(?!\.\d)"
)
_SSN_CUE_RE = re.compile(
    r"\b(?:ssn|social security(?: number)?|social)\b", re.IGNORECASE,
)
_SSN_CUE_WINDOW = 60


def _valid_ssn(area: str, group: str, serial: str) -> bool:
    """SSA structural rules: no area 000/666/9xx, no group 00, no serial 0000."""
    a, g, s = int(area), int(group), int(serial)
    if a == 0 or a == 666 or a >= 900:
        return False
    return g != 0 and s != 0


def _cued_bare_ssns(text: str) -> list[str]:
    found = []
    for match in _SSN_BARE_CANDIDATE.finditer(text):
        window = text[max(0, match.start() - _SSN_CUE_WINDOW):match.start()]
        if not _SSN_CUE_RE.search(window):
            continue
        if _valid_ssn(match.group(1), match.group(3), match.group(4)):
            found.append(match.group(0))
    return found

# ── street address ──────────────────────────────────────────────────────────

_STREET_SUFFIX = (
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ct|Court|Rd|"
    r"Road|Way|Pl|Place|Ter|Terrace|Cir|Circle|Pkwy|Parkway|Hwy|Highway)"
)
# Number, one to three name words, a street word, then optionally a unit and a
# "City, ST 12345" tail. Case-insensitive so a child's "42 maple street" is
# caught; the name words are then checked against _NOT_STREET_WORDS so ordinary
# talk ("5 miles down the road", "2 dogs on my drive") is not an address.
_ADDRESS_PATTERN = re.compile(
    r"(?<![\d.])\b(\d{1,6})\s+"
    r"((?:[NSEW]\.?\s+)?(?:[A-Za-z][A-Za-z'\-]*\s+){1,3}?)"
    r"(" + _STREET_SUFFIX + r")\b\.?"
    r"(?:,?\s+(?:apt|apartment|unit|suite|#)\.?\s*[A-Za-z0-9\-]+)?"
    r"(?-i:(?:,?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2},?\s+[A-Z]{2}"
    r"(?:\s+\d{5}(?:-\d{4})?)?))?",
    re.IGNORECASE,
)
# A direction after the street word ("... Avenue NW") and then the city.
_ADDRESS_WITH_DIRECTION = re.compile(
    r"(?<![\d.])\b(\d{1,6})\s+"
    r"((?:[NSEW]\.?\s+)?(?:[A-Za-z][A-Za-z'\-]*\s+){1,3}?)"
    r"(" + _STREET_SUFFIX + r")\b\.?"
    r"(?-i:\s+(?:N|S|E|W|NE|NW|SE|SW)\b\.?)"
    r"(?-i:(?:,?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2},?\s+[A-Z]{2}"
    r"(?:\s+\d{5}(?:-\d{4})?)?))?",
    re.IGNORECASE,
)
_US_STATES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR
""".split())
# "Columbus, OH 43215", "Columbus OH 43215-1234", "Washington DC 20500".
_CITY_STATE_ZIP = re.compile(
    r"(?-i:\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),?\s+([A-Z]{2})\s+"
    r"(\d{5}(?:-\d{4})?))\b"
)
# UK ("SW1A 1AA", "M1 1AE") and Canadian ("K1A 0B1") postcodes. The shape is
# distinctive, but part numbers can share it, so a town or a postcode word
# must sit just in front.
_POSTCODE = (r"(?:[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}"
             r"|[A-Z]\d[A-Z]\s?\d[A-Z]\d)")
_TOWN_POSTCODE = re.compile(
    r"(?-i:\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),?\s+(" + _POSTCODE + r"))\b"
)
_POSTCODE_CUED = re.compile(
    r"\b(?:zip(?:\s*code)?|post\s*code|postal\s+code)\s*(?:is|was|=|:)?\s*"
    r"(\d{5}(?:-\d{4})?|(?-i:" + _POSTCODE + r"))(?![\w-])",
    re.IGNORECASE,
)
_NOT_TOWN_WORDS = frozenset({"The", "A", "An", "My", "Our", "Your", "Model",
                             "Part", "Order", "Serial", "Item", "Room",
                             "Flight", "Route", "Apollo", "Version", "Build"})

_NOT_STREET_WORDS = frozenset({
    "the", "a", "an", "my", "our", "your", "his", "her", "their", "its", "this",
    "that", "on", "in", "at", "of", "to", "up", "down", "by", "for", "from",
    "and", "or", "with", "into", "onto", "across", "along", "over", "near",
    "miles", "mile", "km", "kilometers", "kilometres", "feet", "foot", "ft",
    "meters", "metres", "yards", "blocks", "block", "minutes", "minute", "mins",
    "hours", "hour", "seconds", "degrees", "times", "days", "weeks", "years",
    "is", "was", "are", "were", "be", "no", "any", "every", "some", "each",
    "other", "same", "long", "whole", "wrong", "right", "first", "last",
    "cars", "dogs", "cats", "kids", "people", "steps", "lights", "trees",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
})

# ── card numbers (Luhn-validated) ────────────────────────────────────────────

_CREDIT_CARD_PATTERN = re.compile(
    r"\b(?:4\d{3}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}"
    r"|5[1-5]\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}"
    r"|3[47]\d{2}[\s\-]?\d{6}[\s\-]?\d{5})\b"
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0

# ── names ───────────────────────────────────────────────────────────────────

# "name: Jane Doe" / "patient = Jane Doe" (HBB), and the spoken forms
# "my name is Leo Marlow", "my mom's name is Jane Doe", "call me Leo Marlow".
# Two or more Title-case words are required: a first name alone is how the bot
# greets someone, and redacting it would break ordinary conversation.
_TITLE_NAME = r"(?-i:([A-Z][a-z]+(?:[ \-][A-Z][a-z]+){1,2}))"
_LABELED_NAME_PATTERN = re.compile(
    r"\b(?:name|patient|client|user|owner|contact)\s*[:=]\s*" + _TITLE_NAME,
    re.IGNORECASE,
)
_SPOKEN_NAME_PATTERN = re.compile(
    r"\b(?:name\s+is|name's|named|call\s+me|i\s+am\s+called|"
    # The same in a few other languages: Spanish, French, German, Italian,
    # Portuguese. Still two Title-case words after it.
    r"mi\s+nombre\s+es|me\s+llamo|je\s+m['’]appelle|mon\s+nom\s+est|"
    r"ich\s+hei(?:ß|ss)e|mein\s+name\s+ist|mi\s+chiamo|"
    r"meu\s+nome\s+(?:é|e))\s+" + _TITLE_NAME,
    re.IGNORECASE,
)
# "I'm Jordan Blake", "I am Jordan Blake". Two Title-case words are still
# required, and the first must not be an everyday word that happens to be
# capitalised ("I'm Very Happy", "I am New Here").
_INTRO_NAME_PATTERN = re.compile(
    r"(?:^|(?<=[\s,.;:!?(\"']))(?:i['’]m|im|i\s+am)\s+" + _TITLE_NAME,
    re.IGNORECASE,
)
_NOT_NAME_WORDS = frozenset({
    "Very", "So", "Not", "Just", "Really", "Going", "Here", "Back", "From",
    "In", "At", "The", "A", "An", "Happy", "Sorry", "Good", "Fine", "Okay",
    "Ok", "Done", "Ready", "Sure", "New", "Still", "Also", "Looking",
    "Trying", "Feeling", "Thinking", "Working", "Getting", "Using",
    "Planning", "Always", "Never", "Only", "Glad", "Tired", "Excited",
    "Interested", "Curious", "Home", "Out", "Off", "On", "Too", "All",
    "Pretty", "Kind", "Big", "Huge", "Totally", "Almost", "About", "Over",
    "Your", "Their", "His", "Her", "My", "Our", "Its", "This", "That",
})

# ── dates of birth ──────────────────────────────────────────────────────────

_MONTH_NAME = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
_DATE_FORMS = (
    r"(?:"
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|" + _MONTH_NAME + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_NAME + r"(?:,?\s+\d{4})?"
    r")"
)
# A date only counts when a birth word sits just in front of it, so launch
# dates and deadlines are never birthdays. A birthday with no year
# ("my birthday is March 3") still counts: for a child it is enough to find them.
_DOB_PATTERN = re.compile(
    r"(?:\b(?:DOB|date\s+of\s+birth|birth\s*date|birthday|born)[^\n\d]{0,20}?"
    r"|\bb\.\s*)(" + _DATE_FORMS + r")",
    re.IGNORECASE,
)

# ── school ──────────────────────────────────────────────────────────────────

# A proper-noun school name: "Lincoln Elementary", "Oak Ridge Middle School",
# "St. Mary's Academy". The name words must be Title-case, so "high school
# science" or "middle school" as a phrase does not fire.
_SCHOOL_PATTERN = re.compile(
    r"(?-i:\b((?:St\.\s+)?[A-Z][A-Za-z'\.]+(?:\s+[A-Z][A-Za-z'\.]+){0,3})\s+"
    r"(?:Elementary|Middle|High|Junior\s+High|Primary|Intermediate|Montessori|"
    r"Academy|Prep|Preparatory)"
    r"(?:\s+School)?)\b"
    r"|(?-i:\b((?:St\.\s+)?[A-Z][A-Za-z'\.]+(?:\s+[A-Z][A-Za-z'\.]+){0,3})\s+School)\b"
)
# Title-case words that start a sentence or are generic, never a school's name.
_NOT_SCHOOL_NAMES = frozenset({
    "The", "A", "An", "My", "Our", "Your", "His", "Her", "Their", "This", "That",
    "Public", "Private", "Boarding", "Summer", "Sunday", "Night", "Grad",
    "Graduate", "Law", "Medical", "Art", "Music", "Dance", "Driving", "Old",
    "New", "Home", "Online", "Charter", "Local", "Every", "Which", "What", "In",
    "At", "To", "From", "After", "Before", "I", "We", "High", "Middle",
    "Elementary", "Junior", "Primary", "Grade", "Space", "Flight", "Film",
})

# ── exact location (GPS) ─────────────────────────────────────────────────────

# A latitude/longitude pair with at least three decimals each (about 100 m).
# Sky coordinates (right ascension / declination, altitude / azimuth) share the
# shape, so a sky word just in front vetoes the match.
_GPS_PATTERN = re.compile(
    r"(?<![\d.])(-?\d{1,2}\.\d{3,})\s*°?\s*[NS]?\s*[,;\s]\s*"
    r"(-?\d{1,3}\.\d{3,})\s*°?\s*[EW]?(?![\d.])"
)
_SKY_COORD_CUE = re.compile(
    r"\b(?:ra|dec|right\s+ascension|declination|alt|az|azimuth|altitude|"
    r"galactic|ecliptic|epoch|j2000|magnitude|mag|redshift)\b",
    re.IGNORECASE,
)

# ── passwords typed into chat ────────────────────────────────────────────────

_PASSWORD_PATTERN = re.compile(
    r"\b(?:password|passcode|passphrase|pass\s*word|wi-?fi\s+(?:key|code)|"
    r"network\s+key|pin(?:\s+code)?)\s*(?:is|was|=|:)\s*"
    r"[\"'“]?([^\s\"'”]{4,64})",
    re.IGNORECASE,
)
_NOT_A_PASSWORD = frozenset({
    "correct", "wrong", "incorrect", "required", "needed", "strong", "weak",
    "too", "not", "the", "set", "missing", "invalid", "expired", "reset",
    "changed", "fine", "secure", "safe", "same", "different", "long", "short",
    "empty", "blank", "case", "what", "wrong.", "correct.", "something",
    "saved", "hidden", "encrypted", "stored", "private", "secret", "unknown",
    "forgotten", "lost", "easy", "hard", "good", "bad", "okay",
})


# ── health disclosures (ported from HBB) ─────────────────────────────────────

_HEALTH_TERMS = (
    r"medications?|medicines?|meds|prescriptions?|prescribed|dosage|doses?|"
    r"insulin|inhaler|epipen|epi-pen|antibiotics?|amoxicillin|albuterol|"
    r"antidepressants?|chemo(?:therapy)?|dialysis|radiation therapy|"
    r"diagnosis|diagnosed|asthma|diabetes|diabetic|epilepsy|seizures?|"
    r"migraines?|cancer|tumou?r|hiv|lupus|crohn'?s|celiac|coeliac|"
    r"depression|anxiety disorder|bipolar|adhd|autism|autistic|dementia|"
    r"alzheimer'?s|allerg(?:y|ies|ic)|eczema|"
    r"blood pressure|blood sugar|a1c|cholesterol|surgery|operation|"
    r"therapist|psychiatrist|oncologist|cardiologist|"
    r"pregnan(?:t|cy)|miscarriage|disability|wheelchair|"
    # Named conditions (without these, add-more search words keep
    # "leukemia" and "hiv").
    r"leuka?emia|lymphoma|melanoma|sclerosis|parkinson'?s|hepatitis|"
    r"tuberculosis|schizophreni[ac]|ptsd|ocd|anorexi[ac]|bulimi[ac]|"
    r"addiction|overdose|rehab|suicid(?:e|al)|self[- ]harm|infertility|ivf|"
    r"heart attack|heart disease|kidney (?:disease|failure)|chronic pain"
)
_HEALTH_TERM_RE = re.compile(r"\b(?:" + _HEALTH_TERMS + r")\b", re.IGNORECASE)


# "allergic to peanuts": the allergen is the private part, so the phrase goes.
_ALLERGY_PHRASE_RE = re.compile(
    r"\ballergic\s+to\s+[A-Za-z]+(?:\s*(?:,|and|or)\s*[A-Za-z]+){0,3}",
    re.IGNORECASE,
)
_HEALTH_CUE_RE = re.compile(
    r"\b(?:my|our|his|her|their)\b"
    r"|(?-i:\b[A-Z][a-z]+)['’]s"
    r"|\b(?:i|we|he|she|they)\s+"
    r"(?:am|is|are|was|were|have|has|had|take|takes|took|started|need|needs|"
    r"got|gets|get|use|uses)\b"
    r"|\b(?:i'm|im|i’m|he's|she's|they're|we're)\b",
    re.IGNORECASE,
)
_HEALTH_MEDLIST_RE = re.compile(
    r"\b(?:meds?|medications?|medicine)\s+list\b"
    r"|\bto\s+(?:the\s+|my\s+|his\s+|her\s+|their\s+|our\s+)?"
    r"(?:meds?|medications?)\b",
    re.IGNORECASE,
)
_HEALTH_WRITE_VERB_RE = re.compile(
    r"\b(?:add|adds|added|adding|log|logs|logged|logging|record|records|"
    r"recorded|put|note|noted|save|saves|remind)\b",
    re.IGNORECASE,
)
_HEALTH_CUE_WINDOW = 40
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]")


def _health_disclosures(text: str) -> list[str]:
    """Health terms said ABOUT a person (not questions about a subject)."""
    found: list[str] = []
    seen: set[str] = set()
    medlist_possible = bool(_HEALTH_MEDLIST_RE.search(text)
                            and _HEALTH_WRITE_VERB_RE.search(text))

    def _disclosed(start: int, end: int) -> bool:
        window = text[max(0, start - _HEALTH_CUE_WINDOW):start]
        parts = _SENTENCE_SPLIT_RE.split(window)
        window = parts[-1] if parts else window
        if _HEALTH_CUE_RE.search(window):
            return True
        if not medlist_possible:
            return False
        sentence_start = 0
        for sep in _SENTENCE_SPLIT_RE.finditer(text[:start]):
            sentence_start = sep.end()
        tail = _SENTENCE_SPLIT_RE.search(text, end)
        sentence = text[sentence_start:tail.start() if tail else len(text)]
        return bool(_HEALTH_MEDLIST_RE.search(sentence)
                    and _HEALTH_WRITE_VERB_RE.search(sentence))

    covered: list[tuple[int, int]] = []
    for match in _ALLERGY_PHRASE_RE.finditer(text):
        if _disclosed(match.start(), match.end()):
            phrase = match.group(0)
            covered.append((match.start(), match.end()))
            if phrase.lower() not in seen:
                seen.add(phrase.lower())
                found.append(phrase)
    for match in _HEALTH_TERM_RE.finditer(text):
        if any(s <= match.start() < e for s, e in covered):
            continue
        if not _disclosed(match.start(), match.end()):
            continue
        term = match.group(0)
        if term.lower() not in seen:
            seen.add(term.lower())
            found.append(term)
    return found


# ── vehicle identification number (VIN) ────────────────────────────────────

# 17 characters, no I, O or Q (ISO 3779). Counted when a VIN word is just in
# front, OR when the North American check digit (position 9) is right - a
# random 17-character code passes that by chance about one time in eleven, and
# it must also mix letters and digits.
_VIN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])([A-HJ-NPR-Z0-9]{17})(?![A-Za-z0-9])")
_VIN_CUE = re.compile(r"\b(?:vin|vehicle\s+identification(?:\s+number)?)\b",
                      re.IGNORECASE)
_VIN_VALUES = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def _vin_check_ok(vin: str) -> bool:
    total = sum(_VIN_VALUES[c] * w for c, w in zip(vin, _VIN_WEIGHTS))
    rem = total % 11
    return vin[8] == ("X" if rem == 10 else str(rem))


def _vins(text: str) -> list[str]:
    found = []
    for m in _VIN_CANDIDATE.finditer(text):
        vin = m.group(1)
        if not (re.search(r"[A-Z]", vin) and re.search(r"\d", vin)):
            continue
        window = text[max(0, m.start() - _CUE_WINDOW):m.start()]
        if _VIN_CUE.search(window) or _vin_check_ok(vin):
            found.append(vin)
    return found

# ── licence plates ───────────────────────────────────────────────────────────

# Only with a plate word in front ("my license plate is 7ABC123", "plate number
# ABC-1234"). The value must hold a digit: "plate tectonics" and "my plate is
# empty" are not plates. A plate typed with no plate word is not caught.
_PLATE_PATTERN = re.compile(
    r"\b(?:licen[cs]e\s+plate|number\s+plate|plate\s+(?:number|no\.?)|"
    r"(?:my|our|his|her|their)\s+plate|tag\s+number|registration\s+number|"
    r"reg\s+number)\s*(?:is|was|=|:|#)?\s*"
    r"([A-Za-z0-9]{1,4}[\s\-]?[A-Za-z0-9]{1,5})\b",
    re.IGNORECASE,
)

# ── government id numbers (passport, driver's licence) ──────────────────────

_GOV_ID_PATTERN = re.compile(
    r"\b(?:passport|driver'?s?\s+licen[cs]e|driving\s+licen[cs]e|"
    r"licen[cs]e\s+number|state\s+id|id\s+card)"
    r"\s*(?:number|no\.?|#)?\s*(?:is|was|=|:)?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]{4,15})\b",
    re.IGNORECASE,
)

# ── bank account and routing numbers ────────────────────────────────────────

_BANK_ACCOUNT_PATTERN = re.compile(
    r"\b(?:(?:bank|checking|savings|chequing)\s+account|account\s+number|"
    r"acct\.?\s*(?:number|no\.?|#)|routing\s+number|aba\s+number|iban)"
    r"\s*(?:number|no\.?|#)?\s*(?:is|was|=|:)?\s*"
    r"(\d[\d\s\-]{4,20}\d)\b",
    re.IGNORECASE,
)

# ── the last four digits of a card or account ───────────────────────────────

# "card ending in 4321", "account ending 0088", "last four digits of my Visa
# are 1234". Four digits alone are everywhere (years, times, model numbers), so
# a money word must sit in the same stretch of text.
_LAST4_PATTERN = re.compile(
    r"\b(?:ending(?:\s+in|\s+with)?|ends\s+(?:in|with)|"
    r"last\s+(?:four|4)(?:\s+digits)?(?:\s+(?:of|on)\s+(?:my|our|the)\s+"
    r"[A-Za-z]+)?(?:\s+(?:are|is|:))?)\s*[:#]?\s*(\d{4})\b",
    re.IGNORECASE,
)
_MONEY_CUE = re.compile(
    r"\b(?:card|account|acct|bank|visa|mastercard|amex|discover|debit|credit|"
    r"checking|savings|chequing)\b",
    re.IGNORECASE,
)
_MONEY_WINDOW = 40


def _last_fours(text: str) -> list[str]:
    found = []
    for m in _LAST4_PATTERN.finditer(text):
        window = text[max(0, m.start() - _MONEY_WINDOW):m.end()]
        if _MONEY_CUE.search(window):
            found.append(m.group(1))
    return found


def _cued_values(pattern: "re.Pattern[str]", text: str) -> list[str]:
    """Group 1 of every match that holds at least one digit."""
    out = []
    for m in pattern.finditer(text):
        value = m.group(1).strip()
        if re.search(r"\d", value):
            out.append(value)
    return out


#: Internal type id -> plain words a person reads ("your ...").
PLAIN_WORDS: dict[str, str] = {
    "email": "email address",
    "phone": "phone number",
    "ssn": "Social Security number",
    "address": "home address",
    "credit_card": "card number",
    "name": "full name",
    "date_of_birth": "birthday",
    "school": "school name",
    "location": "exact location",
    "password": "password",
    "vin": "vehicle identification number",
    "license_plate": "licence plate",
    "government_id": "ID number",
    "bank_account": "bank account number",
    "account_last4": "last digits of a card or account",
    "health.condition": "health details",
}

#: Internal type id -> the marker left in saved text, in plain words, because
#: the person reads it back in their chat history ("[phone number kept private]").
REDACTION_MARKERS: dict[str, str] = {
    kind: f"[{words} kept private]" for kind, words in PLAIN_WORDS.items()
}
# Stands in for a marker while scanning; never a character people type.
_MARKER_FILL = "\x00"
#: The key detector's mark (``secret_detector.HELD``) is a marker too: text
#: cleaned for a cloud model runs the key detector first, then this scan, and
#: "PASSWORD: [held in vault]" must not become "[password kept private] in vault]".
_KEY_HELD_MARK = "[held in vault]"
_MARKER_RE = re.compile(
    "|".join(re.escape(m) for m in sorted(list(REDACTION_MARKERS.values())
                                          + [_KEY_HELD_MARK], key=len, reverse=True)))

# Replacement order: the most specific shapes first, so a phone number inside
# an address line or a date inside a DOB sentence is replaced whole.
_REDACT_ORDER = (
    "ssn", "credit_card", "bank_account", "government_id", "vin",
    "date_of_birth", "address", "location", "email", "phone", "password",
    "license_plate", "account_last4", "name", "school",
)


@dataclass
class PIIDetection:
    """Result of scanning text for PII. Values are kept only in memory."""

    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    ssns: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    credit_cards: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    dates_of_birth: list[str] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    passwords: list[str] = field(default_factory=list)
    vins: list[str] = field(default_factory=list)
    license_plates: list[str] = field(default_factory=list)
    government_ids: list[str] = field(default_factory=list)
    bank_accounts: list[str] = field(default_factory=list)
    account_last4s: list[str] = field(default_factory=list)
    health_conditions: list[str] = field(default_factory=list)

    def items(self) -> dict[str, list[str]]:
        """``{type id: [values]}`` for every type found, in a fixed order."""
        table = {
            "email": self.emails,
            "phone": self.phones,
            "ssn": self.ssns,
            "address": self.addresses,
            "credit_card": self.credit_cards,
            "name": self.names,
            "date_of_birth": self.dates_of_birth,
            "school": self.schools,
            "location": self.locations,
            "password": self.passwords,
            "vin": self.vins,
            "license_plate": self.license_plates,
            "government_id": self.government_ids,
            "bank_account": self.bank_accounts,
            "account_last4": self.account_last4s,
            "health.condition": self.health_conditions,
        }
        return {k: list(v) for k, v in table.items() if v}

    @property
    def has_pii(self) -> bool:
        """True if any PII was detected."""
        return bool(self.items())

    @property
    def pii_types(self) -> list[str]:
        """The kinds of PII found, e.g. ``["email", "phone"]``."""
        return list(self.items().keys())

    @property
    def severity(self) -> str:
        """``"none"``, ``"medium"``, ``"high"`` or ``"critical"``."""
        if (self.ssns or self.credit_cards or self.passwords
                or self.bank_accounts or self.government_ids):
            return "critical"
        if (self.addresses or self.dates_of_birth or self.health_conditions
                or self.locations or self.schools):
            return "high"
        if (self.emails or self.phones or self.names or self.vins
                or self.license_plates or self.account_last4s):
            return "medium"
        return "none"

    def summary(self) -> str:
        """A short, value-free description of what was found."""
        if not self.has_pii:
            return "no PII detected"
        parts = [f"{len(v)} {k}" for k, v in self.items().items()]
        return "detected " + ", ".join(parts)


class DataClassifier:
    """Find personal details, redact them, and route content to a zone."""

    def scan_for_pii(self, text: str, *,
                     include_health: bool = False) -> PIIDetection:
        """Scan ``text`` and return a :class:`PIIDetection`. Search words are short, so there are no chunks here."""
        if not text or not isinstance(text, str):
            return PIIDetection()
        return self._scan_chunk(text, include_health)

    def _scan_chunk(self, text: str, include_health: bool) -> PIIDetection:
        """Scan one piece of text (at most :data:`_SCAN_CHUNK` chars)."""
        detection = PIIDetection()
        # A marker this module wrote earlier ("[password kept private]") is
        # never a new detail: fill it (same length, so nothing shifts).
        # Without this, re-scanning a saved message found "[password" after
        # "password is" and filed the marker itself as a password. The fill
        # is not a space: with spaces, "password is [password kept private]
        # Please" made "Please" the password. A value holding the fill is
        # dropped at the end.
        scan = _MARKER_RE.sub(lambda m: _MARKER_FILL * len(m.group(0)),
                              text)

        detection.emails = _unique(_EMAIL_PATTERN.findall(scan))

        detection.ssns = _unique([
            m.group(0) for m in _SSN_CANDIDATE.finditer(scan)
            if _valid_ssn(m.group(1), m.group(3), m.group(4))
        ] + _cued_bare_ssns(scan))

        cards = []
        for m in _CREDIT_CARD_PATTERN.finditer(scan):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                cards.append(m.group(0))
        detection.credit_cards = _unique(cards)

        taken = detection.ssns + detection.credit_cards
        phones = []
        for m in _PHONE_SEPARATED.finditer(scan):
            if not _vetoed(scan, m.start()):
                phones.append(m.group(0))
        for m in _PHONE_BARE.finditer(scan):
            window = scan[max(0, m.start() - _CUE_WINDOW):m.start()]
            if _PHONE_CUE.search(window) and not _vetoed(scan, m.start()):
                phones.append(m.group(0))
        for m in _PHONE_INTL.finditer(scan):
            digits = re.sub(r"\D", "", m.group(0))
            if 8 <= len(digits) <= 15 and not _vetoed(scan, m.start()):
                phones.append(m.group(0).rstrip())
        for m in _PHONE_TRUNK.finditer(scan):
            # The phone word may come before ("my mobile is 07700 900123")
            # or just after ("07700 900123 (mobile)").
            window = (scan[max(0, m.start() - _CUE_WINDOW):m.start()] + " "
                      + scan[m.end():m.end() + _AFTER_CUE_WINDOW])
            digits = re.sub(r"\D", "", m.group(0))
            if 9 <= len(digits) <= 12 and _PHONE_CUE.search(window) \
                    and not _vetoed(scan, m.start()):
                phones.append(m.group(0))
        # A shorter match inside a longer one is the same number.
        phones = [p for p in phones
                  if not any(p != q and p in q for q in phones)]
        detection.phones = _unique([
            p for p in phones
            if not any(p in e for e in detection.emails)
            and not any(p in t or t in p for t in taken)
        ])

        addresses = []
        for pattern in (_ADDRESS_WITH_DIRECTION, _ADDRESS_PATTERN):
            for m in pattern.finditer(scan):
                words = [w.lower().rstrip(".") for w in m.group(2).split()]
                if any(w in _NOT_STREET_WORDS for w in words):
                    continue
                addresses.append(m.group(0).rstrip(",. "))
        for m in _CITY_STATE_ZIP.finditer(scan):
            if m.group(2) in _US_STATES \
                    and m.group(1).split()[0] not in _NOT_TOWN_WORDS:
                addresses.append(m.group(0))
        for m in _TOWN_POSTCODE.finditer(scan):
            if m.group(1).split()[0] not in _NOT_TOWN_WORDS:
                addresses.append(m.group(0))
        addresses += [m.group(1) for m in _POSTCODE_CUED.finditer(scan)]
        # A part already inside a longer address is that address.
        addresses = [a for a in addresses
                     if not any(a != b and a in b for b in addresses)]
        detection.addresses = _unique(addresses)

        # Only the date itself: "My birthday is [birthday kept private]".
        detection.dates_of_birth = _unique(
            [m.group(1) for m in _DOB_PATTERN.finditer(scan)])

        names = [n.strip() for n in _LABELED_NAME_PATTERN.findall(scan)]
        names += [n.strip() for n in _SPOKEN_NAME_PATTERN.findall(scan)]
        names += [n.strip() for n in _INTRO_NAME_PATTERN.findall(scan)
                  if n.split()[0] not in _NOT_NAME_WORDS
                  and n.split()[-1] not in _NOT_NAME_WORDS]
        detection.names = _unique([n for n in names if len(n) > 2])

        schools = []
        for m in _SCHOOL_PATTERN.finditer(scan):
            lead = (m.group(1) or m.group(2) or "").split()
            if not lead or lead[0] in _NOT_SCHOOL_NAMES:
                continue
            schools.append(m.group(0))
        detection.schools = _unique(schools)

        locations = []
        for m in _GPS_PATTERN.finditer(scan):
            try:
                lat, lon = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            before = scan[max(0, m.start() - _CUE_WINDOW):m.start()]
            after = scan[m.end():m.end() + 12]
            if _SKY_COORD_CUE.search(before) or _SKY_COORD_CUE.search(after):
                continue
            locations.append(m.group(0).strip())
        detection.locations = _unique(locations)

        # The whole token is taken, trailing "!" included: removing one
        # punctuation mark with a password beats leaving part of it behind.
        detection.passwords = _unique([
            m.group(1) for m in _PASSWORD_PATTERN.finditer(scan)
            if m.group(1).lower().rstrip(".,;!?") not in _NOT_A_PASSWORD
        ])

        detection.vins = _unique(_vins(scan))
        detection.license_plates = _unique(_cued_values(_PLATE_PATTERN, scan))
        detection.government_ids = _unique(_cued_values(_GOV_ID_PATTERN, scan))
        detection.bank_accounts = _unique(
            _cued_values(_BANK_ACCOUNT_PATTERN, scan))
        # A number already caught whole is not also a phone number.
        whole = detection.bank_accounts + detection.government_ids
        detection.phones = [p for p in detection.phones
                            if not any(p in w or w in p for w in whole)]
        detection.account_last4s = _unique([
            d for d in _last_fours(scan)
            if not any(d in c for c in detection.credit_cards + whole)
        ])

        if include_health:
            detection.health_conditions = _health_disclosures(scan)
        for f in fields(detection):
            values = getattr(detection, f.name)
            if isinstance(values, list):
                setattr(detection, f.name,
                        [v for v in values if _MARKER_FILL not in v])
        return detection

    def redact_pii(self, text: str, *,
                   include_health: bool = False) -> tuple[str, PIIDetection]:
        """Return ``(redacted_text, detection)``.

        Every detected value is replaced by a plain marker the person can read
        back in their history (``[phone number kept private]``). Longer values
        are replaced first so a value that contains another is removed whole.
        """
        detection = self.scan_for_pii(text, include_health=include_health)
        if not detection.has_pii:
            return text, detection
        redacted = text
        found = detection.items()
        for kind in _REDACT_ORDER:
            for value in sorted(found.get(kind, []), key=len, reverse=True):
                redacted = redacted.replace(value, REDACTION_MARKERS[kind])
        for term in sorted(found.get("health.condition", []), key=len,
                           reverse=True):
            redacted = re.sub(r"\b" + re.escape(term) + r"\b",
                              REDACTION_MARKERS["health.condition"], redacted,
                              flags=re.IGNORECASE)
        return redacted, detection


def _vetoed(text: str, start: int) -> bool:
    """True when a device / order word sits just before a digit run."""
    window = text[max(0, start - _CUE_WINDOW):start]
    return bool(_NOT_A_PHONE_CUE.search(window))


def _unique(items: list[str]) -> list[str]:
    """De-duplicate while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        token = item.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


# ── words aimed at a model ──────────────────────────────────────────────────

#: Phrases that read like orders to a model or a browser rather than a label.
_INSTRUCTION_RE = re.compile(
    r"ignore (?:all |the |any )?(?:previous|prior|above)|disregard|"
    r"system prompt|you are (?:now )?(?:a|an|the)\b|as an ai|assistant:|"
    r"<\s*script|javascript:|data:text|on(?:load|error)\s*=|\bsudo\b|"
    r"\brm -rf\b|curl |wget |https?://|www\.", re.IGNORECASE)


#: Orders aimed at a MODEL only, for long prose a project writes about itself
#: (a README's opening, a description). Web addresses, ``sudo`` and code
#: samples are normal in a README and are judged elsewhere (the hidden
#: connection check); words that try to steer a model never are. Kept narrow
#: on purpose ("You are now ready", "Ignore the build step", "Override the
#: system theme", "You can ignore the warnings" are ordinary README words);
#: measured on a recorded README and description corpus with no
#: new hits. Text is folded first (:func:`fold_for_orders`), so zero-width
#: characters, full-width letters and look-alike letters do not hide words.
_FILLER = (r"(?:all|any|the|your|my|of|these|those|each|every|previous|prior|"
           r"above|earlier|system|safety|original|initial|given|current|other)")
_MODEL_ORDER_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+"
    r"(?:(?:all|any|the|your|my|of|these|those|each|every)\s+){0,3}"
    r"(?:previous|prior|above|earlier|preceding|foregoing)\b|"
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:everything|anything|all)\s+"
    r"(?:above|before|else|previous|prior|you)\b|"
    # "ignore the instructions above", "ignore all the rules", "ignore your
    # system instructions": up to four small words, then the thing obeyed.
    r"\b(?:ignore|disregard|forget)\s+(?:" + _FILLER + r"\s+){0,4}"
    r"(?:instructions?|rules|guidelines|guardrails|directions|directives|"
    r"prompts?|programming)\b|"
    # "disregard what you were told", "forget all that came before".
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|everything\s+|anything\s+)?"
    r"(?:what|that)\s+(?:you\s+(?:were|have\s+been|'ve\s+been|are\s+being)\s+"
    r"(?:told|given|asked|instructed)|(?:came|comes|was\s+(?:said|written))\s+"
    r"(?:before|above|earlier))\b|"
    r"\bsystem\s+prompt\b|"
    r"\bsystem\s+(?:note|notice|message|instructions?|override|command|alert|update)"
    r"\s*[:!\-‐-―]|"
    r"\b(?:important|urgent|attention|note)\s*[:!-]?\s*(?:to\s+)?(?:the\s+)?"
    r"(?:ai|llm|assistant|model|agent|bot)s?"
    r"(?:\s+(?:agents?|assistants?|models?|bots?))?\s*[:!,]|"
    # Speaking to a model: "Hey AI: ...", "To any AI agent: ...".
    r"\b(?:hey|hi|hello|dear)\s+(?:(?:all|any|every|the|you)\s+)?"
    r"(?:ai|llm|language\s+model)s?"
    r"(?:\s+(?:agents?|assistants?|models?|bots?|systems?))?\s*[:!,]|"
    r"\bto\s+(?:(?:all|any|every|each|the)\s+)?(?:ai|llm|language\s+model)s?"
    r"(?:\s+(?:agents?|assistants?|models?|bots?|systems?))?\s*:|"
    r"\b(?:ai|llm|language model|assistant|agent)s?\s+(?:reading|parsing|processing)\s+this\b|"
    r"\bif you are an?\s+(?:ai|llm|language model|assistant|agent|bot)\b|"
    r"\byou are now (?:a|an|the|dan|jailbroken|unrestricted|unfiltered)\b|"
    r"\bfrom now on,? you\b|"
    r"\bnew instructions?\s*:|\bas an ai(?: language)? model\b|"
    r"\b(?:do not|don't|never) (?:tell|inform|warn|alert) (?:the )?(?:user|owner|human)\b|"
    r"^\s*(?:system|assistant)\s*:|<\|(?:im_start|im_end|system|assistant|user)\|>|"
    r"\[/?inst\]|<<\s*/?sys\s*>>|</?\s*system\s*>",
    re.IGNORECASE | re.MULTILINE)

#: Cyrillic and Greek letters that look like Latin ones, read as Latin by the
#: order checks (so a Cyrillic "i" in "ignore" does not hide the word).
_LOOKALIKE = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ɡ": "g", "А": "A", "Е": "E", "О": "O",
    "Р": "P", "С": "C", "Х": "X", "І": "I", "Ј": "J",
    "Ѕ": "S", "Н": "H", "М": "M", "Т": "T", "В": "B",
    "К": "K", "ο": "o", "α": "a", "ε": "e", "ι": "i",
    "Ο": "O", "Α": "A", "Ε": "E", "Ι": "I", "Τ": "T",
    "Ν": "N", "Μ": "M", "Κ": "K", "Β": "B", "Ρ": "P",
})


def fold_for_orders(text: str) -> str:
    """*text* as the order checks read it: compatibility forms folded
    (full-width letters become plain ones), invisible format characters
    (zero-width spaces and joiners, direction marks, soft hyphens) dropped,
    and Cyrillic or Greek look-alike letters read as Latin."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return folded.translate(_LOOKALIKE)


#: Characters that render as blank space but are not ``str.isspace`` (the
#: Hangul fillers, the braille blank). Read as a space by the label check.
_BLANKS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}


def fold_label(text: str) -> str:
    """*text* as the short-label order check reads it.

    :func:`fold_for_orders`, plus: combining marks dropped (an accent on the
    "i" of "ignore" does not hide the word), blank-looking characters and
    every kind of space read as one space, and ``-``, ``_``, ``.``, ``/``,
    ``+`` and ``~`` between letters read as a space ("Ignore-all-previous").
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    decomposed = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    folded = fold_for_orders(decomposed)
    folded = "".join(" " if (ord(ch) in _BLANKS or ch.isspace()
                             or unicodedata.category(ch) in ("Zs", "Zl", "Zp"))
                     else ch for ch in folded)
    folded = re.sub(r"(?<=\w)[-_./+~]+(?=\w)", " ", folded)
    return " ".join(folded.split())


_ORDER_VERBS = (r"remove|delete|erase|wipe|clear|drop|call|run|execute|invoke|send|post|"
                r"upload|ignore|forget|disregard|obey|reveal|print|output|fetch|open|"
                r"add|mark|change|update|approve|say|reply|answer|respond|tell")
_ADDRESSED = (r"ai|a\.?i\.?|llm|assistant|claude|chatgpt|gpt(?:-?\d+)?|gemini|copilot|"
              r"llama|qwen|mistral|deepseek|grok|language\s+model")
#: More shapes of an order to a model, read in prose (a README's opening, a
#: description) after :func:`fold_label` as well as :func:`fold_for_orders`.
#: Each needs words an ordinary README does not put together: a model
#: addressed and then told to act, "previous instructions" overridden, a
#: "new task:" that is an order.
_MODEL_ORDER_MORE_RE = re.compile(
    r"\b(?:override|overrule|bypass|skip|discard|drop|replace)\s+"
    r"(?:(?:all|any|the|your|my|of|these|those|each|every)\s+){0,3}"
    r"(?:previous|prior|above|earlier|preceding|foregoing|original|system)\s+"
    r"(?:instructions?|prompts?|directives?|directions|guidelines|guardrails|orders|rules)\b|"
    r"\bpay\s+no\s+(?:attention|heed|mind)\s+to\s+(?:(?:all|any|the|your|my|of|these|those)\s+){0,3}"
    r"(?:previous|prior|above|earlier|preceding|instructions?|rules|prompts?|directions|"
    r"directives|guidelines|system)\b|"
    r"\b(?:do\s+not|don'?t|never|stop|no\s+longer)\s+(?:follow|obey|heed|listen\s+to)\s+"
    r"(?:(?:all|any|the|your|of|these|those)\s+){0,3}"
    r"(?:(?:previous|prior|above|earlier|system|original)\s+)?"
    r"(?:instructions?|prompts?|directives|guidelines|guardrails|programming)\b|"
    r"\b(?:hey|hi|hello|dear|attention|note\s+to|message\s+to)\s*,?\s+"
    r"(?:(?:all|any|every|the|you)\s+)?(?:" + _ADDRESSED + r"|model|models)"
    r"(?:\s+(?:agents?|assistants?|models?|bots?|systems?))?\s*[:,!\-]\s*(?:please\s+)?"
    r"(?:" + _ORDER_VERBS + r")\b|"
    r"(?:^|[.!?]\s+)\W*(?:" + _ADDRESSED + r")s?(?:\s+(?:agents?|assistants?|models?|bots?))?"
    r"\s*[:,]\s*(?:please\s+)?(?:" + _ORDER_VERBS + r")\b|"
    r"\b(?:you|u)\s+(?:must|will|should|shall|need\s+to|have\s+to|are\s+to)\s+now\s+"
    r"(?:remove|delete|erase|wipe|clear|drop|ignore|forget|disregard|obey|send|upload|"
    r"reveal)\s+(?:all|every|everything|each|any|the\s+(?:user|owner)|your)\b|"
    r"\b(?:new|updated|real|actual|true|secret|hidden)\s+(?:task|tasks|instructions?|"
    r"directives?|objective|goal|orders|mission)\s*:\s*(?:please\s+)?(?:you\b|(?:"
    + _ORDER_VERBS + r")\b)|"
    r"\bwhatever\s+the\s+(?:user|owner|human)\s+(?:asks|says|wants|requests|types)\b|"
    r"\bsystem\s+override\b",
    re.IGNORECASE | re.MULTILINE)


def model_orders(text: object) -> bool:
    """True when *text* holds words aimed at steering a model (the prose
    check, after folding)."""
    if not isinstance(text, str):
        return False
    folded = fold_for_orders(text)
    if _MODEL_ORDER_RE.search(folded) or _MODEL_ORDER_MORE_RE.search(folded):
        return True
    label = fold_label(text)
    return bool(_MODEL_ORDER_RE.search(label) or _MODEL_ORDER_MORE_RE.search(label))


def reads_like_orders(text: object, *, prose: bool = False) -> bool:
    """True when *text* reads like an order to a model or browser, or holds a
    web address. Catalog and API text is data; text like this never becomes a
    name or label.

    With *prose* (a README's opening, a project description) only orders
    aimed at a model count: addresses and install commands are ordinary there.
    Without it (a name, a label) both kinds count. Text is folded first.
    """
    if not isinstance(text, str):
        return False
    folded = fold_for_orders(text)
    if model_orders(text):
        return True
    return (not prose) and bool(_INSTRUCTION_RE.search(folded))
