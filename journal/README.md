# Journal

A private journal that stays on this computer. Write an entry with a mood, tags and a star, ask for a writing prompt when you are stuck, look back by timeline, calendar, search and "on this day", and keep an eye on your writing streak. Every entry is a plain file you can read without this app, and you can bring entries in from jrnl or Day One and take them out as JSON or Markdown. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Journal is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/journal/`) and a tray item. The page has:
  - **Today**: a write box (Markdown is fine), mood buttons from 1 Awful to 5 Great, tags, a star, and a "Give me a prompt" button with 40 prompts; then entries from this date in earlier years, and what you wrote today;
  - **Timeline**: newest first, filtered by tag, mood, starred and month;
  - **Calendar**: a month at a time, with a dot on days that have entries; tap a day to read it;
  - **Search**: words in the title, text and tags;
  - **Your data**: export and import.
  The top line shows how many entries there are and the writing streak. Every entry can be starred, edited and deleted (the page asks first). It is sized for a tablet and works by touch.
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_entry` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `add_entry` | Write a new entry: text, and if wanted a title, mood, tags, star, the prompt it answers, or an earlier day | yes | write in my journal that Sam and I walked by the river today and I felt calm |
| `search_entries` | Find entries by words, tag, date range, mood or star, newest first | no | find my journal entries about the garden |
| `read_entry` | Read an entry in full by its date, words in its title or latest; a date reads every entry from that day | no | read me what I wrote yesterday in my journal |
| `list_recent` | The newest entries with date, title, mood, tags and first words | no | show my last five journal entries |
| `on_this_day` | Entries from this same date in earlier years | no | what did I write on this day in earlier years |
| `journal_stats` | Writing streak, how many entries, moods and tags over a week, month, year, all, or a date range | no | what is my journal writing streak |
| `change_entry` | Change an entry's title, text (replace or add to the end), tags, mood or star | yes | star my latest journal entry |
| `remove_entry` | Delete an entry and its file | yes | delete my latest journal entry |

An entry is named by its date, words in its title, `latest` for the newest one, or its id. Words in a title match whole words or the start of a word (three letters at least), never letters inside a word; when the match is not the exact title, the answer starts by saying which entry it used. When more than one entry matches, nothing changes and the answer lists them.

**How the streak, "on this day" and import work.** See `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None.

**Data.** Everything is in the package's data folder (`PUFFPAI_DATA`):

- `entries/YYYY/MM/<id>.json`, one file per entry: `id`, `created` and `updated` (local date and time), `title`, `body` (Markdown), `mood` (1 to 5 or empty), `tags`, `starred`, `prompt`, and the marker `app: "journal"`. A file from before the marker, with exactly those keys, still reads and gets the marker on its next save. The id starts with the date and time it was written, like `20270331-211500-3fa2`.
- `index.json`, a short list of every entry made from the entry files. Journal builds it again from the files each time it starts, so it never holds anything the entry files do not.

Every save writes a temp file and then renames it over the old one. A file that will not read (bad JSON, or empty), or an entry file that is not this app's (another marker, or other keys), is never written over: it is left as it is, listed on the Your data tab, and counted in `journal_stats`. An `index.json` that is not this app's (valid JSON without the Journal marker, like `{}`) stops every change, with a message naming the file, until it is moved away. A request body over its cap (256 KB, or 16 MB for an import) is read and dropped, then refused; a body that does not all arrive within 5 seconds is refused. On remove, PuffPAI asks whether to keep the folder.

**Bring your entries in, take them out.** Import reads a JSON export from this Journal, from jrnl (`jrnl --export json`) or from Day One. It never writes over an entry already here, and says how many it imported and how many it skipped. Export gives every entry in one JSON file, or one Markdown file per entry (with the fields at the top as front matter) in a zip.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/index`, `/list`, `/stats`, `/updated`, the exports, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/journal/`, which adds the token. The page checks for changes every few seconds, so an entry the bot adds shows up. Entry text is shown through a small Markdown renderer that escapes the text first and only keeps web links, so nothing written in an entry can run in the page. The page takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

## Rough edges

- The entry files are not encrypted in this version. Anyone who can open this computer's files can read them.
- No pictures, sound or attachments. A picture in imported Markdown shows as its words.
- No reminders: nothing tells you to write. Ask the bot, or open the page.
- An entry file changed or added by hand while Journal runs shows up after Journal restarts.
- Day One's export is not fully described by its makers, so only its text, date, tags and star come in; its photos, places and weather are left out.
- Tags are lower case. Taking a #word out of the text does not take the tag off; change the tags to do that.
- Days follow this computer's clock and date.

Cypher Sisters. puff puff pass.
