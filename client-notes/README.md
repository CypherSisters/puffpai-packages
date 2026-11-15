# Client Notes

Offline: it uses no internet. Notes per client for a one-person practice (a counsellor, coach, tutor, tradesperson, carer, or a small law or accounts office), kept on this computer. By Cypher Sisters, Apache-2.0.

**These notes are kept in plain files on this computer and are not encrypted by this app. Your profession may have rules about keeping client records: check them.**

Client Notes stores what you type. It writes no clinical or professional judgement of its own, and it does not send reminders, bills or messages.

## PuffPAI

**What it does.** Client Notes is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/client-notes/`) and a tray item. The page has the line above at the top; a client list with search and a status filter (active, paused, closed), marked when a client is due or not seen in 30 days; a client view with the client's details and a timeline of notes, newest first; add and edit forms for clients and notes; an "Open actions" view across all clients, with a box to tick each one done; a "This week" view (next contacts due, actions due, notes written this week, clients not seen); a print view of one client's record or one note, in plain black on white; export of one client or all, as JSON and as a readable text file; and import. It works by touch and loads nothing from the internet;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`. `remove_note_or_client` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `find_client` | Clients by words in a name, code, tag or about note, or by status | no | which of my clients are paused |
| `client_summary` | One client: the latest notes, open actions, next contact and consent on file | no | catch me up on C-0002 before the session |
| `add_client` | A new client; the app gives the next private code, like C-0004 | yes | add a new client Sam L, started today, consent on file |
| `add_session_note` | A session, call, email or visit note: discussed, agreed, actions, length, next contact | yes | log a 15 minute call with Jo about the invoice |
| `list_open_actions` | Open actions across all clients, or one, soonest due first | no | what actions are still open across all my clients |
| `mark_action_done` | Tick an action done, found by its words | yes | the send the worksheet action is done |
| `who_is_due` | Next contact dates in the next 7 days or past, and active clients not seen in 30 days | no | which clients am I due to contact in the next few days |
| `remove_note_or_client` | Remove one note, or a client (see below) | yes | delete the latest note for C-0002 |

The tools take a client by code (C-0001) or name; the bot does not need to look it up first. Matching is strict because these are private records: the exact code, the exact full name, or whole words of one name (a start of 3 letters or more counts, a piece inside a word never does). When it was not an exact match, the answer begins "Showing <full name> (<code>)." When no client or more than one matches, nothing changes and nothing is shown; for more than one, the answer lists only their names and codes. See PROCESS.md.

**Codes.** Each client gets a private code, C-0001, C-0002 and so on, so you can talk about a client with the bot by code and keep the name out of the chat. You can type your own code instead.

**Private notes.** A note marked private keeps its words off chat: the bot sees only its date, kind and length, and that it is private. Its actions are counted but not worded. The page shows everything.

**Removing.** A note is removed by its date, or "latest". A client with no notes is removed when asked. A client who still has notes is removed only when the bot sends `confirm` as that client's exact code; any other answer removes nothing and says how. On the page, "Close and remove this client" first offers to export the record, then asks twice: once to confirm, then to type the client's code. To keep a finished client, set the status to closed instead.

**Import.** Import reads this app's JSON export and never overwrites: a client already here is skipped, a code already taken gets the next free one, and the answer says what happened. How codes, due dates and import work, step by step: `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None. Add your first client.

**Data.** One file, `clients.json`, in the package's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once (temp file, then rename) under one lock, and stamps it. The file carries the marker `"app": "client-notes"` and `"format": 1`. A file that will not read, an empty one, or one without that marker and its lists of clients and notes (another app's file, `{}`) is never written over: every save is refused, and the page and the bot say so and name the file. A missing file is a fresh start. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/view`, `/export`, `/export_text`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/client-notes/`, which adds the token. The page checks the stamp every few seconds and reloads when the bot changed something, but never redraws a form you are filling in.

**Files.** `puffpai.json` (the manifest), `server.py` (the server), `web/index.html` (the page), `icons/client-notes.svg`, `PROCESS.md`, `NOTICE.md`, `tests/test_server.py`.

**Licence.** Apache-2.0. See `NOTICE.md`.

## Rough edges

- Not encrypted. The notes are as private as this computer's folder and its backups. If your profession needs encrypted records, keep them somewhere that does that.
- No reminders: nothing tells you on its own that a contact is due. The page marks it, and the bot says so when asked.
- One practitioner. There are no accounts, no sharing and no audit log of who read what.
- When you chat about a client, what the bot says and hears is part of the chat; use codes, and mark sensitive notes private.
- "Next contact" is the date on the client's newest note that has one; an older date on an older note does not count.
- Actions are found by their words. Two actions with the same words need the client named too.
- Times are this computer's local time; there are no time zones.
- An empty or damaged `clients.json` stops the app from saving until you move that file away or put a good copy back. Nothing is overwritten by accident.

Cypher Sisters. puff puff pass.
