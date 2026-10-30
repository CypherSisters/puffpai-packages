# Calendar

Events, tasks, medicines and bills for your household on one month view. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Calendar is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (month view, tasks, medicines and bills, at `/pkg/calendar/`) and a tray item;
- a "Coming up" widget (`list`, at `/widget/upcoming`): the next 8 events in the coming week;
- MCP tools for the brain, at `/mcp`. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes |
|---|---|---|
| `add_event` | Add an event, once or repeating (daily, weekdays, weekly, biweekly, monthly, yearly) | yes |
| `add_task` | Add a to-do with a due date; it shows on the calendar that day | yes |
| `add_med` | Track a medicine: dose, how often, when | yes |
| `add_bill` | Track a bill: amount and due date; it shows on the calendar that day | yes |
| `list_events` | What is on one day, or today and the next few days (repeats worked out) | no |
| `list_tasks` | Open tasks, done ones, or all | no |
| `list_meds` | The medicines, and whether each was taken today | no |
| `list_bills` | Unpaid bills with the total, paid ones, or all | no |
| `edit_event` | Move, rename or change an event | yes |
| `edit_task` | Rename a task or move its due date | yes |
| `complete_task` | Mark a task done | yes |
| `delete_event` / `delete_task` | Delete one | yes |
| `take_med` | Note a medicine was taken today | yes |
| `pay_bill` | Mark a bill paid | yes |
| `delete_bill` | Stop tracking a bill (its calendar entry goes too) | yes |

Dates can be ISO (`2026-10-30`, `2026-10-30T10:00`) or plain words (`tomorrow at 3pm`, `friday at 10am`, `next monday`, `October 30`), so the brain does not need to know today's date. Items are found by name; when two names match, the tool asks which one.

**Checked before saving.** Text arguments must be text. Dates must fall between 1900 and 2200. A bill amount must be a finite number from 0 to 10,000,000 (kept to two decimals). Anything else gets a plain error and nothing is saved. An amount saved before this check that is not a number reads as 0. Every route answers in JSON (never NaN), and an unexpected error is a plain 500 answer, not a dropped connection.

**Who.** "Who it is for" comes from the Core's shared people list (`GET /v1/people`, D12), read with the package's token. Calendar keeps no people list of its own. A copied-in v1 file's `family` list is read only when the Core has no people yet.

**What it needs.** Nothing from the internet, no keys, no brain. Calendar never syncs to an outside calendar.

**Setup steps.** None.

**Data.** One file, `calendar.json`, in Calendar's data folder (`PUFFPAI_DATA`). Same shape as today's BuildBot (`family`, `events`, `tasks`, `meds`, `bills`, `last_updated`), so an old `calendar.json` can be copied in. Every save replaces the whole file at once and stamps it. A file that will not read is never written over. The page never sends the whole document: each button calls the same function as the matching tool (`POST /api/<tool>`). Medicines are labelled `health.medication` (`data.labels`), so the Core treats them as health details.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). GET routes never change anything.

## Changed from today's BuildBot

- The brain can add, list, change and delete events, tasks, medicines and bills from chat. Before, only the page and file upload could.
- There is a standalone page. Before, Calendar was only a native Desktop page built from Desktop parts.
- Delete no longer removes every item whose name contains the words, and edits no longer change the first partial match: one match is used, several get a question.
- Deleting a task deletes its calendar entry too, and moving a task's due date moves its entry.
- "Taken" on a medicine counts for the day it was taken. Before, it stayed taken forever.
- Listing events works out repeating events. Before, only the page did.
- Monthly repeats on the 31st land on the month's last day instead of running into the next month.

## Not in this port

- File import (`ImportEvents`): it was switched off in today's BuildBot (no route behind it). It can come back as an `accepts` tool fed by Archivist.
- Today's BuildBot has an optional n8n workflow (`n8n_workflows/calendar_sync.json`) that hands the calendar to n8n over a webhook. It is left out. Calendar has no outside sync of any kind.

## Rough edges

- Medicines can be added, but not removed, from chat or the page (the same as today's BuildBot).
- Bill amounts are shown in dollars.
- Medicine times are plain text ("8:00 AM"); nothing reminds at that time yet. Use AlarmFlow for a ringing reminder.
