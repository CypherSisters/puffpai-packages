# How Health File works out a flag, builds the summary, and decides what to skip on import

## A result's flag

A wrong flag on a health result is worse than no flag. So Health File gives a flag only when the printed range is clear, and otherwise gives no flag and says: "Range not clear, no flag given. Read the printed range."

1. A flag is worked out only from the range printed on that same result, as typed in. Health File has no table of normal ranges, because ranges differ between labs.
2. If nothing was typed for the range (no Range low, no Range high, no range text), the flag is "none", with no note.
3. If a Range low or Range high was typed but is not a number, the range is not clear.
4. If both Range low and Range high were typed and low is bigger than high, the range is not clear.
5. If range text was typed, it must be exactly one range, one of: "a-b" or "a to b" (with a no bigger than b), "<x", "<=x", ">x", ">=x", "up to x", "x or less", "at least x", or the plain words for these ("at most", "under", "below", "less than", "over", "above", "more than", "greater than"). After it may come one unit word, like "mmol/L". When the result has no unit of its own, that word must be a unit Health File knows: a plain unit word like mg, g, U, IU or mmol, or "%", or such words joined by "/", like mmol/L, mg/dL, U/L, x10^9/L or mL/min/1.73m2. Anything else after the range, like "fasting", "approx", "T2" or "x2", makes the range not clear.
6. The range text is not clear if it has anything else: a second number pair (like "10-20 (men) 5-15 (women)"), a second comparison (like "<5 >10"), brackets, any of the words "or", "and", "between", "if", "unless", "except", or a word that starts with "men", "women", "male", "female", "adult", "child", "age", "year", "pregnan", "trimester", "week", "month" and the like, so "men50+" and "age50+" count too (the "or" in "x or less" is the only "or" allowed, so "60 years or less" is not clear).
7. If the range text has a unit and the result has a unit, they must be the same (capital letters and spaces at the ends ignored). A missing unit on either side is fine. A different unit (like a result in mg/dL against "3.9-5.5 mmol/L") is not clear.
8. If both numbers (Range low, Range high) and range text were typed, they must say the same range. If they do not, the range is not clear. Neither one wins.
9. If the range is not clear, there is no flag: the flag is "none" and the note "Range not clear, no flag given. Read the printed range." is shown with the result (in the tools, the summary, the history and the page).
10. If the value is not a plain number (like "positive" or "<0.5"), the flag is "none".
11. Otherwise: a value below the low end is "low"; above the high end is "high"; else "in range". A high alone means "up to", a low alone means "at least".
12. The ends of a range count as in range ("3.5-5.0" includes 3.5 and 5.0; "up to 12" and "12 or less" include 12). Only the signs and words that mean "strictly" leave the end out: "<", ">", "under", "below", "less than", "over", "above", "more than", "greater than". So 200 with "<200" is "high".
13. The flag is worked out again every time it is shown. It is never stored, so fixing a typed range fixes the flag.

## The one-page summary, in this order

1. Allergies, first and marked clearly; "none recorded" when there are none. (None recorded does not mean none exist.)
2. Active conditions: every condition whose status is not resolved, by name.
3. Current medicines: status "taking", and no "until" date before today, by name.
4. Latest results: for each test name (capital letters ignored), the result with the latest date, with its flag.
5. Coming up, by date: follow-up dates from visits that are today or later, and each vaccine's "due again" date from its latest dose; a due date already passed is marked overdue.
6. Open questions for the next visit: questions not marked asked.
7. The fixed line at the end: "Health File keeps your records. It is not a doctor and does not diagnose. For anything urgent, call your local emergency number."

"Get ready for a visit" (prepare_for_visit) shows allergies, the open questions, the last visit (the latest visit dated today or earlier), what changed since that visit's date (medicines started or stopped, conditions begun or ended, results and vaccinations dated after it, and anything added to the file after it), and the current medicines.

## Import: what is added and what is skipped

1. Import only adds. Nothing already in Health File is changed or removed.
2. The file must be a Health File export (`"format": "health-file/1"`) or a FHIR R4 Bundle (`"resourceType": "Bundle"`). Anything else is refused as a whole.
3. From a FHIR Bundle, only Patient, Condition, MedicationStatement, AllergyIntolerance, Observation, Encounter and Immunization are read. Any other kind is skipped and counted as "other entries".
4. Words are read from a field's `text`, or else from the first coding's `display`. Codes are not read, except the standard status code of a condition (active or resolved).
5. Each record goes to its person: by the file's patient (or person) name, matched to the household by name with capital letters ignored. A name that is not in the household yet is added to Health File's own list. If you tick "Put everything under the person chosen above", every record goes to that person instead.
6. A record is skipped when it has no person, when it misses what that kind of record needs (a condition's name, a result's test, date and value, a visit's date, a vaccination's vaccine; from a FHIR file, a result, visit or vaccination also needs its date), or when a field does not fit (a date that is not a date, a range that is not a number, words that are too long).
7. A record is skipped when the same person already has a record of the same kind that says the same thing (capital letters ignored). So importing one file twice adds nothing the second time.
8. The answer says how many of each kind were imported and how many were skipped.

## Finding a record or a test by name

1. A record's id, then its exact name (its medicine, condition, allergy, test, vaccine or question), with capital letters and spaces ignored. An exact full name always wins. When a person is named, an id must be one of that person's records, or nothing changes.
2. Otherwise the one record where every word you gave is a name of three or more letters that starts a word of its name ("art" finds "Arthritis"; an apostrophe is dropped, so "john's" finds "St John's wort"). Letters inside a word never match: "haron" does not find "Sharonex". A word shorter than three letters, like "a" or "au", finds a record only when it is the record's whole name. The answer then begins "Showing <name>."
   `list_records`, which only reads, also takes a 2-character word with a digit in it as a whole word: "t4" finds "Free T4". The tools that change or delete never do.
3. When several match, the answer lists only their names and ids, and nothing is shown or changed. When none match, nothing changes; the answer lists the record names only when a person was named (and then only that person's), and otherwise asks whose record it is.
4. A person is found only by their whole name (capital letters and spaces ignored), never by part of it.
5. `list_records`, asked for results with a test name, finds the test the same way: the exact test name first, then the one test whose words start with the words given, like "ldl" for LDL cholesterol.
6. A printed range longer than 200 characters is not read: it gives no flag.
7. `list_records` answers about results when the kind asked is results (or a word for them, like tests, labs or bloods), or when the words given match only results. With one test's name it gives that test's values over time, oldest first, each with its date, value, unit, printed range and flag. With words that fit several tests (like "cholesterol" for HDL and LDL cholesterol), or other words of a result such as its lab's name, it lists every result they match. Without a name it lists every result, each test's values together, oldest first.
