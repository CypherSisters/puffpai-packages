# Chore Board

A household chore board: the people in the household, chores that repeat (once, daily, weekly or every N days), each one assigned to a person or to a rotation that moves to the next person when it is done, what is due today and this week, and a tally of who did what in the last 7 and 30 days. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Chore Board is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/chore-board/`) and a tray item. The page shows Today (with anything late), This week, and People (what each person is on now and how many chores they did in the last 7 and 30 days). You can add, edit, mark done and remove chores, and add, rename and remove people. It is sized for a tablet and works by touch;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_chore_or_person` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `list_due` | Chores due today (and any late), or in the next 7 days (`when: week`), for everyone or one person | no | what chores are due today |
| `add_chore` | Add a chore: name, how often it repeats, first due date, and one person, a rotation (several names in order) or nobody | yes | add a weekly chore to take out the trash, rotating between Alex, Sam and Jo |
| `change_chore` | Change a chore's name, repeat, next due date, notes or who it is on, by its name (whole words or the start of a word, three letters at least, are enough; the answer then says which chore it used) | yes | change the trash chore to every 3 days |
| `mark_done` | Mark a chore done, by whoever's turn it is or by someone named, today or on an earlier day. Sets the next due date and moves a rotation | yes | Alex did the dishes |
| `list_people` | The household, what each person is on now, and how many they did in the last 7 days | no | who is on which chores right now |
| `add_person` | Add a person to the household | yes | add Jo to the chore board |
| `show_tally` | Who did which chores in the last 7 and 30 days | no | who did the most chores this month |
| `remove_chore_or_person` | Remove a chore, or a person (taken off every chore and rotation too). The tally keeps what was already done | yes | take the window washing chore off the chore board |

**How chores come due.** See `PROCESS.md`: a short list of how the board decides what is due and how a rotation moves.

**What it needs.** Nothing from the internet, no keys and no brain. When the Core gives it `PUFFPAI_API`, it reads the household from the Core's shared people list (`GET /v1/people`, on this computer only) and merges it with its own list; without it, or when the Core does not answer within 2 seconds, it uses its own list.

**Setup steps.** None. Add the people in the household first, then the chores.

**Data.** One file, `chores.json`, in the package's data folder (`PUFFPAI_DATA`): `{app: "chore-board", people: [...], chores: [...], done: [...], last_updated}`. A person from the Core's list is saved here only once a chore points at them. `done` holds one record per chore marked done (which chore, who, which day); records older than 400 days are dropped. Every save replaces the whole file at once (temp file, then rename) and stamps it. A file that will not read, an empty file, or a file that is not this app's (no `app: "chore-board"` marker, or other keys) is never written over: every change is refused with a message naming the file. A file from before the marker, with exactly these keys, still loads and gets the marker on the next save. A request body over 64 KB is read and dropped, then refused; a body that does not all arrive within 5 seconds is refused. On remove, PuffPAI asks whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/board`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/chore-board/`, which adds the token. The page checks the stamp every few seconds and reloads when the brain changed something. The page takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

## Rough edges

- No reminders: nothing tells anyone on its own that a chore is due. Ask, or look at the page.
- A person from the Core's people list can not be renamed here; rename them in PuffPAI. Removing one takes them off every chore, but they still show while the Core lists them.
- "This week" means today and the next 6 days, not a calendar week.
- A rotation moves one step each time the chore is marked done, even when someone else covered that turn.
- Marking a repeating chore done twice in one day moves it on twice (and a rotation two steps).
- Days follow this computer's clock and date.

Cypher Sisters. puff puff pass.
