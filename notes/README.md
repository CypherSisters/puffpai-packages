# Notes

Offline: it uses no internet. A plain notebook kept on this computer, one Markdown file per note, so your notes are files you can open in any text editor and take anywhere. Pin notes, sort them into notebooks and tags, search them, and tick checklist lines on the page. The bot can find, read and add to your notes. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Notes is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/notes/`) and a tray item. On the left, the list of notes (pinned first, then newest), a search box (titles, text and tags) and filters for notebook and tag. On the right, one note, shown as formatted Markdown, with Edit, Pin and Delete (it asks first). Lines that start with `- [ ]` show as tick boxes you can tick on the page. Export and Import buttons at the top. It works by touch and loads nothing from the internet;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_note` also says it deletes (`destructiveHint`). Every tool takes a note's title or words; the app finds the note itself.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `find_notes` | Notes by words, tag or notebook, with the line that matched | no | find my notes about the garden |
| `read_note` | One note in full | no | read me my shopping note |
| `add_note` | A new note with its title, text, tags and notebook | yes | make a new note called Garden ideas: plant tomatoes by the fence |
| `add_to_note` | Add a line, or a tick box, to a note | yes | add milk to the shopping note |
| `change_note` | Rename, replace the text, set tags, pin or unpin, move notebook | yes | pin the shopping note |
| `tick_item` | Tick or untick one checklist line | yes | tick off milk on the shopping note |
| `list_notebooks_and_tags` | Every notebook and tag, with counts | no | what notebooks do I have in my notes |
| `remove_note` | Delete a note and its file | yes | delete my packing list note |

**Your files.** Each note is `notes/<title>.md` in the package's data folder (`PUFFPAI_DATA`), with a short front matter block at the top (id, title, created, updated, tags, pinned, notebook) and then the text. Notebooks are just a label in that block. You can copy files into that folder or edit them by hand; the list catches up within a few seconds. A file with no front matter is fine: its first `# Heading` is its title. Keys you add to the front matter yourself are kept on every save, and a file keeps its own line endings (Windows or Unix).

**Export and import.** Export gives a zip of every note file. Import takes that zip back, a zip of Markdown files from another note tool, or one `.md`, `.markdown` or `.txt` file. Front matter is read when it is there; keys Notes does not use (like `author:` or `aliases:`) are kept as written. Import never writes over a note, and a note whose id is already here is skipped. Zips with unsafe paths are refused whole, and a link inside a zip is skipped, never read. Caps: 200 MB upload, 2000 files, 1024 MB unpacked, 1024 KB a note. The steps are in `PROCESS.md`.

**How a note is found by its title, how checklists and search work.** See `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/list`, `/note`, `/updated`, `/export`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/notes/`, which adds the token. It takes its colours and fonts from the Desktop's look when the Desktop gives them. On remove, PuffPAI asks whether to keep the data.

**Licence.** Apache-2.0. The page's Markdown renderer is adapted from snarkdown (MIT); see `NOTICE.md`.

## Rough edges

- Not encrypted by this app. Notes are plain files, only as private as this computer's folder.
- No sync. The notes live on this one computer; copy the folder or use Export to move them.
- No reminders, no due dates, no alarms.
- No pictures or attachments: a picture in a note shows as its words.
- Front matter keys that Notes does not use are kept as written, but moved above Notes' own keys the first time Notes saves that note.
- A file whose id is not valid, or is the same as another file's, gets a new id. The page says so once, and the new id is written into the file the next time that note is saved.
- A file renamed or changed by hand while a note is open on the page is read again on the next check.
- One notebook per bot; no sharing between people.

Cypher Sisters. puff puff pass.
