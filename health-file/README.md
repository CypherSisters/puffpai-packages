# Health File

A household health record that stays on this computer. For each person: what a doctor would ask about (conditions, medicines, allergies, test results, visits, vaccinations) and what the person wants to ask the doctor. A one-page summary to print and take along. By Cypher Sisters, Apache-2.0.

Health File keeps your records. It is not a doctor and does not diagnose. For anything urgent, call your local emergency number.

## PuffPAI

**What it does.** Health File is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/health-file/`) and a tray item. Pick a person at the top. The Summary tab shows allergies first, then active conditions, current medicines, latest results with their flags, what is coming up (follow-ups, vaccinations due) and open questions, with a Print button for a clean one-page copy. A tab for each kind of record lets you add, edit and delete (delete asks first, on the page). On Results, pick a test to see its values over time as a table and a small line chart, with the printed range shaded. People and files has the household, export and import. It is sized for a tablet and works by touch;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_record` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `person_summary` | One person's one-page summary as text | no | show Alex's health summary |
| `list_records` | One person's records of one kind, or all, optionally filtered by words. For results: with one test's name, that test's values over time, oldest first, each flagged from its own printed range; with words that fit several tests (like cholesterol for HDL and LDL) or a lab's name, every result they match; without, every result | no | list Alex's medicines; show how Alex's cholesterol changed over time |
| `add_record` | Add a condition, medicine, allergy, result, visit, vaccination or question; a name not in the household is added; type `person` adds just a person | yes | Jo's cholesterol came back 5.4 mmol/L, range 3.0 to 5.0 |
| `change_record` | Change a record named in plain words (or by its id): a medicine stopped, a question asked with its answer | yes | mark Alex's ibuprofen as stopped |
| `add_question` | Write down a question for the next visit; a name not in the household is added | yes | add a question for Alex's next doctor visit |
| `prepare_for_visit` | Open questions, what changed since the last visit, current medicines and allergies | no | get Sam ready for the doctor tomorrow |
| `remove_record` | Delete one record named in plain words (or by its id) | yes | delete the flu shot from Jo's health file |

Answers that show results or medicines end with the fixed line above. It is plain code, not words from the brain.

**The household.** Health File reads PuffPAI's shared people list (`GET {PUFFPAI_API}/v1/people` with its own `PUFFPAI_TOKEN`, D12), so the household is the same in every app. When that list does not answer or is empty, Health File uses its own list, which you keep on the People and files tab. Anyone who gets a record is also kept in its own list, so their records still show if the shared list is away. It dials nothing else, and only dials `PUFFPAI_API` when that address is a plain `http` address on this computer (127.0.0.1, localhost or ::1), checked on every ask. It never follows a redirect (any answer but 200 counts as no answer, so its token goes nowhere else), ignores proxy settings, stops after 2 seconds in all, and reads at most 256 KB.

**How flags, the summary and import work.** See `PROCESS.md`. In short: a result's flag (low, in range, high, or none) comes only from the range printed on that result, as you typed it in. There is no table of normal ranges, because ranges differ between labs.

**Export and import.** The record shapes follow HL7® FHIR® R4, whose definitions are CC0; this app is not endorsed by HL7. Export one person or everyone as one JSON file, in Health File's own format or as a FHIR R4 Bundle (type `collection`) with Patient, Condition, MedicationStatement, AllergyIntolerance, Observation, Encounter and Immunization. Words go in the `text` of each coded field; no coded terminologies (such as LOINC or SNOMED) are claimed. Fields FHIR R4 has no place for are kept as extensions (a visit's "what was said" and follow-up date, a vaccination's "due again") or as a note (a medicine's prescriber). Questions stay in Health File's own format. Import reads either format, only adds, never overwrites, skips what is already there, and says how many of each kind were imported and skipped.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None. If PuffPAI's shared people list is empty, add the household on the People and files tab first.

**Data.** One file, `health.json`, in the package's data folder (`PUFFPAI_DATA`): `{format, people: [...], records: [...], last_updated}`. Every save replaces the whole file at once (temp file, then rename) and stamps it. A file that will not read, is empty, or is not this app's (no `format: "health-file/1"` marker, or other keys, like `{}`) is never written over: every change is refused with a plain message about the Health File data file (it never shows the folder). The same goes for a file edited by hand with an entry in `people` or `records` that is not an object, like a stray word or number: it still reads, but nothing is written until that entry is fixed, so nothing in it is ever dropped. A request body over its cap (64 KB, or 4 MB for an import) is read and dropped (up to 64 MB within 20 seconds), then refused; a body that does not all arrive within 5 seconds is refused; a request without the token is answered 401 after at most 64 KB or 2 seconds. On remove, PuffPAI asks whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/meta`, `/updated`, `/people`, `/view`, `/history`, `/export`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/health-file/`, which adds the token. The page checks the stamp every few seconds and reloads when the brain changed something. It takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

## Rough edges

- No reminders: nothing tells anyone on its own that a medicine, follow-up or vaccination is due. Ask, or look at the page.
- No attached scans or photos of letters and lab reports in this version.
- It does not read patient portals or lab websites; everything is typed in or imported from a file.
- The files are not encrypted by this app. Anyone who can open files in its data folder on this computer can read them.
- A flag is only as good as the range typed in. A wrong range gives a wrong flag; a missing one gives none.
- A flag is given only when the range is one clear range (see `PROCESS.md`). Ranges with more than one case (men and women, adults and children, by age), two comparisons ("<5 or >10"), a unit different from the result's, a low bigger than the high, or typed numbers that disagree with the printed text get no flag and the note "Range not clear, no flag given. Read the printed range." Health File does not convert units.
- When you ask the bot to add a record or a question for a name that is not in the household, Health File adds that person to its own list and says so ("Added Alex to Health File."). A name that differs from someone already there only by capital letters or spaces is that person. A misheard name becomes a new person; remove them on the People and files tab.
- Names are matched with capital letters and spaces ignored. A record can also be found by a name of three or more letters that starts a word of its name: "art" finds "Arthritis" and "john's" finds "St John's wort", but "ron" never finds "Sharonex". A word shorter than three letters (like "a" or "au") finds a record only when it is the record's whole name; only when listing, a 2-character word with a digit, like "t4", also finds a whole word ("Free T4"). When several records match, nothing is changed and their names are listed. A person is found only by their whole name. A record's id with a person named must be that person's record.
- A FHIR export skips a record that was edited by hand and will not read (a missing name or date, for example), and the page says how many were skipped.
- The household's names are asked from PuffPAI's people list at most once a minute (every 30 seconds after it did not answer), so a change there can take a minute to show.
- Health File's own export is not a medical record any clinic has to accept; the FHIR file is for other apps that read FHIR R4.
- Dates follow this computer's clock.

Cypher Sisters. puff puff pass.
