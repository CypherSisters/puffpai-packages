# Daily briefing

A good-morning briefing, put together by the brain from the packages you already have. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Daily briefing is a PuffPAI skill package (`kind: skill`): one `SKILL.md` the brain reads when the owner says "good morning", "brief me" or asks for a daily briefing. It has no server and no tools of its own. It tells the brain which installed tools to call and in what order:

| Section | Comes from | Changes |
|---|---|---|
| Today's date | the brain | no |
| Weather, then any severe alert | `weather.current_weather`, `weather.severe_alerts` | no |
| Today's events, tasks due, medicines not yet taken, bills due | `calendar.list_events`, `list_tasks`, `list_meds`, `list_bills` | no |
| How many things are on the shopping list | `freshtable.list_shopping` | no |
| The top two or three headlines | `news-headlines.headlines` | no |
| Timers and alarms that are set | `alarmflow.list` | no |
| One line of encouragement | the brain | no |

The rules it gives the brain: skip a section whose package is not installed, without apologising; stay under 150 spoken words; never invent a fact; say "I can't reach the weather right now" rather than guess; read only, change nothing.

**What it needs.** Nothing on its own. It is useful once one or more of `weather`, `calendar`, `freshtable`, `news-headlines` and `alarmflow` are installed. Whatever those packages need (the internet for weather and news) is asked on their own Yes cards, not here.

**Setup steps.** None.

**Data.** None. It stores nothing.

**How it runs.** It does not run. The brain sees its name and description every turn and reads the rest when a briefing is asked for.

## Changed from HomeBot

- HomeBot's `auto_daily_briefing` asked a Node-RED flow on the box to put the briefing together from weather, calendar and news. Here the brain does it, from the store packages, and the sections grow with what is installed.

## Rough edges

- How well it follows the order and the 150-word limit depends on the brain the owner picked. A small model may skip a section or run long.
- It does not run on a schedule. There is no "brief me at 7 every morning" yet; ask for it.
- It names tools of other packages by their current names. If one of those packages renames a tool, this skill needs an update.

Cypher Sisters. puff puff pass.
