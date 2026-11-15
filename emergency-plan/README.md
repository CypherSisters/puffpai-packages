# Emergency Plan

The household's emergency plan, kept on this computer: the people in the household with their numbers and medical notes, an out-of-area contact and the other people to call, meeting places, where the shutoffs, kit and papers are, how to leave and what to grab, pets, the hazards that apply here, and a log of drills and reviews. It scores how complete the plan is, prints the plan and wallet cards, and the bot can answer from it when the internet is down. By Cypher Sisters, Apache-2.0.

Emergency Plan holds the plan. Supplies and their expiry dates belong in Prep Supplies.

## PuffPAI

**What it does.** Emergency Plan is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/emergency-plan/`) and a tray item. The page shows the plan by section, and every box saves as soon as you leave it. At the top: a Readiness panel (a score out of 8 and what is missing) and a gentle note when the plan has not been reviewed in 6 months. Buttons print the full plan or one wallet card per person (credit card size, to cut out), in plain black on white, and export or import the whole plan as one JSON file. It is sized for a tablet and works by touch. It loads nothing from the internet: no web fonts, no outside scripts;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_item` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `show_plan` | The whole plan, or one section, as text the bot can read aloud | no | read me our emergency plan |
| `find_contact` | Contacts by need: the out-of-area contact, a helper, doctor, vet, school, work, insurance, a utility, or a person in the household | no | who is our out-of-area contact |
| `where_is` | Where the shutoffs are and how to turn them off, the extinguishers, first aid kit, go-bags, spare keys, papers, safe spots, ways out, meeting places, pet carriers, routes | no | where is the gas shutoff |
| `update_plan` | Set or change one item in a section; an item that is not there yet is added | yes | set Sam's mobile in the emergency plan to 555-0100 |
| `readiness_check` | The score out of 8 and what is missing | no | what is our emergency plan missing |
| `grab_list` | The leaving checklist, or tick or untick one item | yes | tick the phone chargers on the grab list |
| `log_practice` | Log a drill or a review (a review sets the last reviewed date) | yes | we did a fire drill today; next time Jo needs a flashlight by the bed |
| `remove_item` | Remove one item from a section | yes | take the old insurance contact out of the emergency plan |

**The sections.** People; Contacts (out-of-area contact, nearby helpers, doctors, vet, school, work, insurance with policy and claims numbers, utilities, other); Meeting places (right outside the home, in the neighborhood, out of the neighborhood, out of town); Home (shutoffs and how to turn each off, extinguishers, first aid kit, go-bags, spare keys, important papers, safe spots, two ways out of each sleeping room); Leaving (two routes, where you would stay, transport) and the Grab list (starts with papers, medicines, chargers, cash, keys, pet supplies and glasses; change it as you like); Pets; Hazards (tick each that applies here, with a note on what you do); Practice (drills and reviews).

**The household.** When the Core's shared people list answers (`GET /v1/people` on `PUFFPAI_API`, with this package's own token), the People section shows those people, and the details you type are kept with them in the plan. When it does not answer, the plan's own list is used. Emergency Plan only ever asks `PUFFPAI_API` when it is on this computer (127.0.0.1 or localhost); it asks nothing else.

**How the score works, when the review note shows, how import works.** See `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None. Start with the out-of-area contact and two meeting places, then work down the page.

**Data.** One file, `plan.json`, in the package's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once (temp file, then rename) and stamps it. The file carries the marker `"app": "emergency-plan"`. A file that will not read, an empty one, or one without that marker (another app's file, `{}`) is never written over: every save is refused, and the page and the bot say so, name the file and say what to do. A missing file is a fresh start. A tool that takes a name finds it whole first, then by whole words: each word given must be a word of the name or the start of one, three letters at least ("Ron" never finds "Sharon"). When it used part of a name, the answer says first which one ("Using 'Ronald Park'."); when several match, nothing changes and the answer lists them. Import keeps the old plan as `plan-backup-<date and time>.json` in the same folder. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/view`, `/export`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/emergency-plan/`, which adds the token. The page checks the stamp every few seconds and reloads when the bot changed something. It takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

## Rough edges

- No reminders and no alerts: nothing tells you on its own that a review or drill is due. The page shows a note, and the bot says so when asked.
- No maps. Routes and places are words you type.
- Not encrypted by this app. The plan holds phone numbers and medical notes; it is only as private as this computer's folder.
- One household per bot.
- The People section reads the shared people list; names there are changed in the Core, not here.
- A person's details are matched to the shared list by its id, or by the same name the first time.
- An empty or damaged `plan.json` stops the app from saving until you move that file away or put a good copy back. Nothing is overwritten by accident.

Cypher Sisters. puff puff pass.
