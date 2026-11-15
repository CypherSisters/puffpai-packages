# Plant Keeper

Offline. An agent that knows the household's plants and tells you what needs doing. It keeps each plant and a care log on this computer, works out what is due from your own intervals, and asks the bot's own brain for a suggested starting care plan and for advice about one plant. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Plant Keeper is a PuffPAI agent package (`kind: agent`). Its scope is `plants.care_plan`, `plants.log` and `plants.what_is_due`. It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/plant-keeper/`) and a tray item. It shows what is due this week, every plant, any suggested plan that is waiting, and the care log. The only thing you change there is the hemisphere; everything else you ask the bot. It loads nothing from the internet;
- eight MCP tools at `/mcp`. The tools that change something are in `abilities.changes`; `remove_plant` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `plants_due` | What is due for water or feed today, or this week | no | what do my plants need today |
| `add_plant` | Adds a plant and asks the brain for a suggested plan (kept aside until you accept it) | yes | add my new peace lily in the living room to my plants |
| `log_care` | Logs watered, fed, repotted, pruned, moved or a note, for one plant or all | yes | I watered the fern |
| `plant_history` | One plant's details and care log | no | when did I last repot the snake plant |
| `change_plant` | Changes a plant's details or intervals, or accepts its suggested plan | yes | change the fern's watering to every 5 days |
| `ask_about_plant` | The brain's advice about one plant, from your words and its log | no | the basil's leaves are going yellow, what is wrong with it |
| `list_plants` | Every plant with where it lives and its intervals | no | which plants do I have |
| `remove_plant` | Deletes a plant and its care log | yes | remove the basil from my plants, it died |

Every tool that names a plant takes its name as you say it ("the fern", "basil"); part of a name is fine. When no plant or more than one matches, nothing changes and the answer lists the plants.

**What code does and what the brain does.** Code keeps the plants and the log, reads dates, and works out what is due, the season (from the date and your hemisphere) and every number that is saved. The brain is asked for two things only: a suggested starting plan for a new plant (shown with the line "A suggestion from the bot's brain. Check it against your own plant."). From it code keeps only two numbers, the watering days (1 to 90) and the feeding days (7 to 365), aside until you say to accept them; then only those two intervals go into the plant, and the light and everything else stay what you typed. Its words are kept only as a note on the plant, "Suggested by the bot's brain on" the date, at most 600 characters, shown as text, and advice about one plant (words only, never saved). What you typed goes to the brain marked as material, not instructions. Its answer is shown as text and never run. When the brain does not answer within 120 seconds, Plant Keeper says "The brain did not answer. Nothing was changed." and changes nothing. You can add a plant with no suggestion at all. Step by step: `PROCESS.md`.

**What it needs.** The bot's own brain (text only), reached through the Core on this computer (`POST /v1/brain/chat` with this package's token). No hosts, no keys, no internet.

**Setup steps.** None. Pick your hemisphere on the page if you are south of the equator.

**Data.** One file, `plants.json`, in the package's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once (temp file, then rename). A file that will not read, or an empty one, is never written over; the bot and the page say so and which file. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} agent.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/view`, `/updated`, `/api/settings`, `/health` and `/mcp`) refuses a request without the package's token. The only place it dials is `PUFFPAI_API`, only when that is an http address on this computer (127.0.0.1, localhost or ::1), directly and never through a proxy, and it never follows a redirect.

**Licence.** Apache-2.0. See `NOTICE.md`.

## Rough edges

- On this platform today an agent's tools are picked like any other tool's: the bot chooses a tool from your words, and a small model can pick the wrong one. Naming the plant helps.
- The brain's suggestions and advice can be wrong. They are not checked against any plant guide. Check them against your own plant.
- A small model takes time to answer: a suggested plan or an answer can take a minute or more. The deadline is 120 seconds, but the Core waits only about a minute for a tool's answer. When the brain is slower than that, the bot may say the tool did not answer while Plant Keeper still adds the plant when the brain does answer; look at the list before adding it again. With a slow brain, add a plant with no suggestion.
- The season is worked out from the month only; it does not know your local climate, frost dates or rain.
- What is due is only as good as the log: a watering that is not logged makes a plant look due.
- No reminders: nothing tells you on its own that a plant is due. Ask, or look at the page.
- It does not identify plants from photos.
- Not encrypted by this app.

Cypher Sisters. puff puff pass.
