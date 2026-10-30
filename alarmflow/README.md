# AlarmFlow

Timers and alarms that ring on this computer. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** AlarmFlow is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (the AlarmFlow timer and alarm page, at `/pkg/alarmflow/`) and a tray item;
- a "Timers and alarms" widget (`list`, at `/widget/running`);
- MCP tools for the brain, at `/mcp`. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes |
|---|---|---|
| `create_timer` | Start a countdown for some hours, minutes and seconds, up to 7 days | yes |
| `create_alarm` | Ring at a clock time, today or tomorrow, once, daily, on weekdays or on weekends | yes |
| `list` | The timers and alarms that are set, with the time left | no |
| `snooze` | Push a ringing item back some minutes (default 9, up to 24 hours) | yes |
| `add_time` | Add minutes to a running item, or take some off (up to 24 hours either way) | yes |
| `pause` / `resume` | Freeze a timer, or start it again | yes |
| `dismiss` | Stop a ringing item, or cancel one that is set. A ringing repeating alarm moves to its next day. | yes |
| `dismiss_all` | Stop and cancel everything | yes |
| `delete` | Remove an item for good | yes |

Every argument is checked before anything is saved (text must be text, numbers must be whole and in range); anything else gets a plain error. An unexpected error is a plain 500 answer, not a dropped connection.

Items are found by name, the way people say them: "the pasta timer" finds "Pasta". When two names match, the tool asks which one.

**How alarms ring.** The ringing clock is part of AlarmFlow's own process, not the page. Once a second it marks every item that is due as ringing and asks the Core to tell the owner (`POST /v1/notify`, urgency high, with sound and speech asked for). The Desktop shows that as a notice card. The AlarmFlow page, when it is open, also plays the item's sound every few seconds until it is stopped or snoozed.

- AlarmFlow needs to keep running in the background for this. Steward starts it again each time BuildBot starts (it was running at the last stop), and Caretaker restarts it if it stops (up to 3 tries, then it is marked broken with a plain reason). `/health` answers 503 if the ringing clock stopped, if `alarmflow.json` will not read, or if the last 3 clock ticks failed, so Caretaker sees that nothing would ring. The page shows the same problem in a banner.
- While BuildBot is off, nothing rings. An item that came due meanwhile rings at the next start, and its notice says how late it is.

**What it needs.** Nothing from the internet, no keys, no brain.

**Setup steps.** One: "Let AlarmFlow play sound". Browsers stay silent until you tap a page once, so open AlarmFlow and tap Test sound.

**Data.** One file, `alarmflow.json`, in AlarmFlow's data folder (`PUFFPAI_DATA`). Same shape as today's BuildBot (`{items: [...], last_updated}`), so an old `alarmflow.json` can be copied in. Every save replaces the whole file at once and stamps it. A file that will not read is never written over. The page never sends the whole document: each button calls the same function as the matching tool (`POST /api/<tool>`).

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). GET routes never change anything.

## Changed from today's BuildBot

- Alarms ring with no page open. Before, the countdown lived in a browser tab, so nothing rang unless the AlarmFlow page, the home page or the floating Puff was open.
- The brain can set, list, snooze, stop and delete timers and alarms from chat.
- Repeating alarms (daily, weekdays, weekends) were page-only; they are tools now.
- The page's Voice button is gone. It used the browser's speech service, which can send audio off this computer. Ask the bot instead.
- The n8n message bridge on the page is gone; the MCP tools replace it.

## Rough edges

- The Desktop shows a notice card for a ringing item but does not play a sound itself; the sound plays on the AlarmFlow page when it is open.
- Alarms take a clock time in the next 24 hours (or a repeat). There is no "on the 3rd of next month" alarm; use Calendar for that.
