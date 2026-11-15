# Radio Plan

Offline: it uses no internet. The household's plan for staying in touch by radio when phones fail, kept on this computer: the radios you own, the channels and what each is for, each person's call sign, the check-in times and what to do if no one answers, the weather alert and broadcast stations to listen to, a message log and a practice log. It prints the plan and a pocket card for each person, says when the next check-in is, and the bot can answer from it when the internet is down. By Cypher Sisters, Apache-2.0.

Rules differ by country. Some channels need a license. Check yours before you transmit. Radio Plan stores what you type. It ships no frequency lists and does not judge what is legal where you live.

## PuffPAI

**What it does.** Radio Plan is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/radio-plan/`) and a tray item. The page shows the plan by section, and every box saves as soon as you leave it. At the top, a Now panel says from the clock when the next check-in is, on which channel, and what to do if no one answers. Buttons print the full plan, or one pocket card per person (credit card size, to cut out) with their call sign, primary and backup channel, the check-in times and what to do if there is no contact. It is sized for a tablet and works by touch. It loads nothing from the internet: no web fonts, no outside scripts;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_radio_item` also says it deletes (`destructiveHint`). Every tool takes a plain name; the app finds the thing itself.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `show_radio_plan` | The whole plan, or one section, as text the bot can read aloud | no | read me our radio plan |
| `next_check_in` | When the next check-in is, on which channel, how long to listen, what to do if no one answers | no | when is our next radio check-in |
| `which_channel` | The channel for a purpose (primary, backup, neighbors, emergency, listen only, weather) or for a person | no | which radio channel do we use to reach the neighbors |
| `update_radio_plan` | Set or change one item in a section; an item that is not there yet is added | yes | set Sam's call sign in the radio plan to Bluebird |
| `log_message` | Add a message to the message log | yes | log a radio message from Jo to Alex on Family 1: home safe, the bridge is flooded |
| `list_messages` | The latest messages, or those from or to one person, or on one channel | no | read me the radio messages from Jo |
| `log_radio_practice` | Log a radio drill or test and what to fix | yes | we did a radio drill this morning; Sam's handheld battery was flat |
| `remove_radio_item` | Remove one item from a section | yes | remove the scanner from the radio plan |

These answers end with the reminder above: every answer of `which_channel` and `next_check_in`, a refusal included; `show_radio_plan` for the whole plan or for the channels, people or check-in times section; and `update_radio_plan` when it changes a channel, a person or a check-in. `remove_radio_item`, `log_message`, `list_messages` and `log_radio_practice` do not carry it.

**The sections.** Radios (a name; kind: handheld two-way, mobile, base station, weather alert radio, shortwave receiver, scanner, mesh text radio or other; who carries it; where it is kept; battery type; notes). Channels (a label such as "Family 1"; the service in your words; the channel number or frequency as text; a tone or code as text; what it is for: primary, backup, neighbors, listen only, emergency or other; notes). People (call sign or radio name, license as free text, their own primary and backup channel if they differ from the household's). Check-in times (time of day, minutes to listen, channel, minutes before going to the backup channel, backup channel, what to do then). Listen-only stations (the weather alert channel for this area as you type it, and local broadcast stations). Message log (date and time, from, to, channel, the message, acted on or not). Practice.

Kinds can be everyday words: "walkie-talkie" is stored as a handheld two-way, "the car radio" as mobile. A word that names no kind is never refused: it is stored as other, and the answer says so.

**The household.** When the Core's shared people list answers (`GET /v1/people` on `PUFFPAI_API`, with this package's own token), the People section shows those people, and the call signs you type are kept with them in the plan. When it does not answer, the plan's own list is used. Radio Plan only ever asks `PUFFPAI_API` when it is on this computer (127.0.0.1, localhost or ::1) and holds no space or control character; it asks nothing else.

**How the next check-in and the pocket cards are worked out.** See `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None. Start with a primary and a backup channel and two check-in times, then add the people's call signs and print the pocket cards.

**Data.** One file, `radio.json`, in the package's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once (temp file, then rename) and stamps it. A file that will not read, or an empty one, is never written over; the page and the bot say so and which file. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/view`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/radio-plan/`, which adds the token. The page checks the stamp every few seconds, reloads when the bot changed something, and redraws the Now panel every minute. It takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

**Licence.** Apache-2.0. See `NOTICE.md` for what it was built from.

## Rough edges

- Not encrypted by this app. Call signs and messages are only as private as this computer's folder.
- No sync. The plan lives on this one computer; print it so it is there when the power is out.
- No reminders and no alarms: nothing sounds at a check-in time. The Now panel and the bot say when it is.
- It does not control or read a radio. The message log is what you type.
- It does not check that a channel or frequency is legal, or that a license is right. That is up to you.
- Times follow this computer's clock. One plan per bot.
- An empty or damaged `radio.json` stops the app from saving until you move that file away or put a good copy back.

Cypher Sisters. puff puff pass.
