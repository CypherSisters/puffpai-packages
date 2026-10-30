# PuffPAI packages

The Cypher Sisters' own packages for the [PuffPAI Store](https://github.com/CypherSisters/puffpai-store). One folder per package, each with its own `puffpai.json` and README. The store's index points at a folder here and pins one commit.

| Folder | Kind | What it is |
|---|---|---|
| `freshtable` | app | Pantry, shopping list, meal plan and the recipes behind them. |
| `calendar` | app | Events, tasks, medicines and bills for your household on one month view. Kept on this computer, never synced to an outside calendar. |
| `fitfam` | app | Workouts for each person in the household, with streaks, goals and a history that stays on this computer. |
| `alarmflow` | app | Timers and alarms. They ring on this computer while BuildBot runs, even with no page open. |
| `trivia` | app | Quiz rounds with scores: play AeroTrivia on its page, or ask the bot to quiz you in chat. |
| `look-clean` | look | Clean: square corners, flat colours, thin 1px rules and dense spacing, with JetBrains Mono headings. Near-black at night, off-white by day, one blue accent. No glow, no clouds. |
| `look-paper` | look | Paper: warm cream paper, dark ink and one coral accent, with serif headings and small corners. A dark sepia version at night. No glow, no clouds. |
| `look-contrast` | look | High contrast: black ground, white text, white lines and a yellow accent at night; white ground, black text and a dark blue accent by day. Square corners and larger text. |
| `look-slate` | look | Slate: calm blue-grey surfaces, a teal accent and soft small corners, in Nunito. Darker slate at night, light grey by day. No glow, no clouds, a little less rounded than Puff. |
| `avatar-block` | avatar | Block, a square terminal face with two square eyes and a cursor, as the bot's face. Flat colours that go with the Clean look. |
| `avatar-pebble` | avatar | Pebble, a round coral stone with a small smile, as the bot's face. Flat colours that go with the Paper look. |

Every app ships off. Turn one on from Apps, PuffPAI store.

**Check a package:** `puffpai check <folder>` from a PuffPAI install. Level 3 is the store's bar.

**Where these came from:** copied from BuildBotv2 `packages/` at `3a7ffa95`. The looks and avatars got a "PuffPAI" README section here so they pass Level 3. From now on this repo is their home; BuildBotv2 keeps its built-in copies until the Core installs from the store (store plan, phase 2).

Apache-2.0.

Cypher Sisters. puff puff pass.
