# Drill Coach

Offline. An agent that walks the household through an emergency practice, one what-now prompt at a time, and writes down what to fix. The drill runs in code, so it works with no brain at all; the bot's own brain only suggests ratings and writes a short summary. It checks whether your household is ready. It does not teach first aid: use a first aid course and the First Aid Basics pack for that. This is practice. In a real emergency, call your local emergency number. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Drill Coach is a PuffPAI agent package (`kind: agent`). Its scope is `drill.run` and `drill.review`. It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/drill-coach/`) and a tray item. It shows the drill that is going, the fix list, past drills and the scenarios, under the practice line. It only shows; you run the drill by talking to the bot. It loads nothing from the internet;
- eight MCP tools at `/mcp`. The tools that change something are in `abilities.changes`; `remove_drill` also says it deletes (`destructiveHint`). Every tool answer, and every error answer, ends with "This is practice. In a real emergency, call your local emergency number."

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `list_scenarios` | The six practice scenarios | no | what emergency drills can we practise |
| `start_drill` | Starts a drill by its scenario and gives the first prompt | yes | let's practise the earthquake drill |
| `drill_answer` | Records the household's answer, suggests a rating to confirm, gives the next prompt; or changes or confirms the last rating | yes | our answer for the drill: we grab the flashlights from the kitchen drawer |
| `finish_drill` | Finishes the drill, gives a summary (a plain one made in code when the brain does not answer), adds what to fix to the fix list | yes | we are done, finish the drill |
| `fix_list` | The open items on the fix list | no | what do we still need to fix from our drills |
| `mark_fixed` | Marks a fix list item done, by its words | yes | mark the battery radio as fixed on our drill fix list |
| `drill_history` | Past drills with their ratings, or one drill in full | no | how did our past drills go |
| `remove_drill` | Deletes one past drill; its fix list items stay | yes | delete the earthquake drill from our drill history |

**The scenarios.** A winter power cut that lasts three days; a storm with flooding and an order to leave; a wildfire smoke week; an earthquake; a week with no running water; a medical emergency at home with the phones down. Each has a setting and 6 to 8 timed prompts, and for each prompt the things a ready household would have or know. The ready lists are checks of readiness ("Someone here has learned how to stop heavy bleeding"), not instructions. They are plain data in `scenarios.json`, written for this package.

**What code does and what the brain does.** Code runs the drill: which prompt comes next, what is saved, and what goes on the fix list (each prompt rated partly or not ready adds that prompt's fix, unless the same item is already open). The brain is asked for two things only: a rating suggestion with one line why, and a short summary at the end written from the ratings code gives it. A suggestion is used only when the first line of the brain's answer is the one word READY, PARTLY or NOT_READY; anything else is unclear, nothing is saved, and the household is asked to rate it. A suggestion is not counted until the household confirms it ("confirm the drill rating") or changes it; the summary counts confirmed ratings only and says how many were left unconfirmed, and each unconfirmed prompt's fix goes on the fix list. The household's typed answers go to the brain marked as material, not instructions; its answer is shown as text and never run. When the brain does not answer a rating within 120 seconds, Drill Coach says "The brain did not answer. Nothing was changed." and changes nothing. Finishing always works: when the brain does not answer, the summary is the plain one made in code (the counts, the prompts rated not ready and the new fix list items), with the line "The brain did not answer, so this summary is the plain one." With no brain, give your own rating with each answer. Step by step: `PROCESS.md`.

**With Emergency Plan.** If the Emergency Plan package is installed, you can ask the bot to check your drill answers against your household plan. Drill Coach does not call other packages itself.

**What it needs.** The bot's own brain (text only) for ratings and summaries, reached through the Core on this computer (`POST /v1/brain/chat` with this package's token). No hosts, no keys, no internet.

**Setup steps.** None.

**Data.** One file, `drills.json`, in the package's data folder (`PUFFPAI_DATA`): the drills with every answer and rating, and the fix list. Every save replaces the whole file at once (temp file, then rename). A file that will not read, or an empty one, is never written over. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} agent.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/view`, `/updated`, `/health` and `/mcp`) refuses a request without the package's token. The only place it dials is `PUFFPAI_API`, only when that is an http address on this computer (127.0.0.1, localhost or ::1), directly and never through a proxy, and it never follows a redirect.

**Licence.** Apache-2.0. See `NOTICE.md`.

## Rough edges

- It is practice, not training. It checks whether your household is ready. It does not teach first aid: use a first aid course and the First Aid Basics pack for that. The ready lists are a starting point, not official advice. Check your plans with your local emergency services.
- On this platform today an agent's tools are picked like any other tool's: the bot chooses a tool from your words. Answers work best said as "our answer for the drill: ...".
- The brain's rating suggestions can be wrong. Confirm the ones you agree with and change the others by saying the rating. Only the last answer's rating can be confirmed or changed; a suggestion left unconfirmed is not counted.
- A small model takes time to answer: a rating or a summary can take a minute. The deadline is 120 seconds, and the Core itself waits about a minute for a tool, so with a slow brain rate the answers yourselves.
- One drill at a time. A drill that is going must be finished before another starts.
- The scenarios are fixed in this version; you can not add your own yet.
- Not encrypted by this app.

Cypher Sisters. puff puff pass.
