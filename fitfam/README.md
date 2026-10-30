# FitFam

Workouts for each person in the household, with streaks, goals and a history that stays on this computer. Pick a kind of workout, an intensity and a length, and FitFam makes a session from its exercise library (50 exercises: calisthenics, kettlebell, range of motion, gym). Tick exercises off with a rest timer between them. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** FitFam is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (the FitFam app, at `/pkg/fitfam/`) and a tray item;
- a "Workout streaks" widget (`list`, at `/widget/streaks`);
- MCP tools for the brain, at `/mcp`. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `log_workout` | Log a workout: what kind, how many minutes, and an optional distance (mi or km). Updates the streak. | yes | log a 30 minute run for me |
| `log_set` | Log one strength set: exercise, reps, weight in lb or kg, optional effort (RPE) | yes | log a set of bench press: 135 pounds for 5 reps |
| `status` | Streaks and totals, for one person or everyone | no | how long is my workout streak |
| `history` | Recent workouts and sets, newest first | no | what workouts did I do lately |
| `personal_records` | The best set for each exercise, with an estimated one-rep max, for one person or everyone | no | what are my personal records |
| `remove_workout` | Remove a workout logged by mistake: the newest, or the newest of one kind | yes | remove that run, I logged it by mistake |
| `remove_set` | Remove a set logged by mistake: the newest, or the newest of one exercise | yes | delete the last bench press set |
| `list_exercises` | The exercise library by muscle group, optionally one muscle group or one kind | no | what exercises work the legs |
| `suggest_workout` | A workout plan from the exercise library. Nothing is logged. | no | make me a 30 minute kettlebell workout |
| `list_goals` | Goals and how close each one is | no | what are my fitness goals |
| `add_goal` | Add a goal with a target | yes | add a fitness goal: 20 push-ups in a row |
| `update_goal` | Set the progress on a goal | yes | update my plank goal: I held it for 90 seconds |
| `remove_goal` | Remove a goal | yes | remove my plank goal |

Every tool except `list_exercises` takes an optional `person`. With one person in FitFam it can be left out; with more, the bot says who. `status` and `personal_records` with nobody named answer for everyone. The page and the tools keep the same split: each person has their own workouts, sets, goals and Quick Start, and the streak, "this week" and total counts are worked out the same way in both.

**People.** FitFam keeps no people list of its own (decision D12). It reads the Core's household people list (`GET /v1/people` with its token) for names and emoji. Anyone already in FitFam's data, such as a file copied in from today's BuildBot, still shows, so no history is lost. With nobody on either list, FitFam logs for one person, "You".

**What it needs.** Nothing from the internet and no brain (`needs.brain` is off). The workout maker (`suggest_workout` and the page's Start Workout) picks exercises from the library that comes with the package (`exercises.json`), one per muscle group first, by plain rules, so it works the same whether or not a model is running. The bot's own model only chooses which tool to call and how to say the answer.

**Units.** A set's weight is kept with its unit, `lb` or `kg` ("pounds" and "kilos" are understood too); with no unit it is `lb`. A workout's distance is `mi` or `km`; with no unit it is `mi`. Nothing is converted: what was said is what is shown. Personal records compare lb and kg fairly (the estimated one-rep max uses the Epley formula, weight x (1 + reps / 30)) and show each record in the unit it was lifted in. Sets saved before 2.1.0 have no unit and show the number alone (and count as lb in records).

**Setup steps.** None.

**Data.** One file, `fitfam.json`, in FitFam's data folder (`PUFFPAI_DATA`), the same shape as today's BuildBot (`{users: {<id>: {name, emoji, prefs, goals, history, sets, streak, totalWorkouts}}, last_updated}`), so an old file can be copied in. Every save replaces the whole file at once and stamps it. A file that will not read is never written over. Every number is checked before it is saved: a finite number in a sane range (weight 0 to 2000, reps 1 to 1000, minutes up to 1440, exercises up to 200, goal targets up to 1,000,000), never NaN or infinity, and every name or note has a length limit. A number that is not finite in an older file reads as 0. The page changes things through the same functions the tools use (`POST api/<action>`), so the page and the bot can never undo each other's work; it reloads when the bot logs something.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/people`, `/exercises`, `/suggest`, the widget, `/api/...`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). GET routes never change anything. The Desktop opens the page through the Core at `/pkg/fitfam/`, which adds the token.

## Changed from today's BuildBot

- The brain can log workouts and sets, read streaks and goals, and make a workout. Before, only the page could.
- The page was a 66 KB React bundle that could no longer be rebuilt from its source. It is harvested from that source (`fitfam-app.jsx`, adapted): the same screens, look and workout maker, as one plain page with no outside code, using the same exercise library as the server.
- The page knew two fixed people. It now shows whoever is on the household list.
- Three different streak counts (page, workout log, set log) are now one: days in a row with a workout or a set, counted from today or yesterday.

## Rough edges

- The household people list has no page of its own yet, so adding people needs the Core API (`PUT /v1/people`). Until then FitFam logs for "You".
- When FitFam is not answering, the page shows the last copy it saw and saves nothing.
- Goal units are words only: a goal in kg and a set in lb are not linked, and goal progress is set by hand (or by the bot), not from logged sets.
- The page shows the bot's distances and lets you remove sets and workouts, but it does not log a single set or a distance itself; the bot does.
- `remove_workout` and `remove_set` take the newest match. To remove an older workout, or one of the last 10 sets, use the page.
- The Quick Start (kind, intensity, length) is set on the page only.

## Changes in 2.1.0

- Weights are lb or kg, and a workout can have a distance in miles or km. Both show on the page and in the bot's answers.
- The bot can read personal records (best set per exercise, with an estimated one-rep max), list exercises by muscle group or kind, and remove a goal.
- The bot can undo a mistake: remove the newest workout or set (or the newest of one kind or exercise).
- The page's Recent Sets has a remove button for each set.
- The page's "this week" now counts the same 7 calendar days as the bot's status, and the recent list shows the intensity by name ("Moderate", not "mid").
