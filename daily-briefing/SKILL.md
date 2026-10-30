---
name: daily-briefing
description: How to give a short "good morning" daily briefing (date, weather, today's events, tasks, medicines and bills, shopping list, headlines, timers, one line of encouragement) from whichever PuffPAI packages are installed. Use when the owner says good morning, asks for a daily briefing, a morning update, "brief me" or "catch me up".
---
<!-- Harvested from HomeBotFounders@b77fc40:src/puffpai/rebot_tools.py (rewritten-from): auto_daily_briefing and capability briefing.daily. Copyright (c) 2026 Cypher Sisters. Apache-2.0 License. -->

# Daily briefing

When the owner says "good morning", or asks for a daily briefing, a morning update, "brief me" or "catch me up", give one short spoken briefing in this order. Gather first, then speak once.

Tools are named here as `<package>.<tool>`; your tool list may show them as `<package>__<tool>`. A package is installed when its tools are in your tool list.

1. **Today.** The day and date in plain words, like "It's Friday, the 30th of October."
2. **Weather.** If the `weather` package is installed, call `weather.current_weather`, then `weather.severe_alerts`. Say the temperature and the sky in one sentence. Say an alert only if there is one, and say it first in this section. If either call fails, say "I can't reach the weather right now" and move on.
3. **Today's plans.** If the `calendar` package is installed, call `calendar.list_events` for today, `calendar.list_tasks` for open tasks, `calendar.list_meds` and `calendar.list_bills`. Say today's events in time order, the tasks due today or overdue, the medicines not yet taken today, and any bill due today or overdue. Leave out what is not due today.
4. **Shopping.** If the `freshtable` package is installed, call `freshtable.list_shopping` and say only how many items are on the list, like "Seven things are on the shopping list."
5. **Headlines.** If the `news-headlines` package is installed, call `news-headlines.headlines` and say the top two or three in a few words each.
6. **Timers.** If the `alarmflow` package is installed, call `alarmflow.list` and say any timers or alarms that are set, with the time.
7. **One line of encouragement.** Warm and short, and true to the day you just described. No quotes from famous people.

## Rules

- Skip any section whose package is not installed. Do not apologise for it or mention it; just go to the next section.
- Skip a section that has nothing in it ("no events today" is fine to say once, in a few words, for the calendar; skip empty shopping lists and empty timer lists).
- Keep the whole briefing under 150 words when spoken. Short sentences, no lists read out as bullets, no headings.
- Never invent a fact. Every number, time, name and headline comes from a tool answer you got just now. If a tool fails, say plainly that you could not reach it, for example "I can't reach the weather right now", rather than guess.
- Do not change anything while briefing: no ticking off tasks, no taking medicines, no new timers. Only read.
- If the owner asks for more on one part ("tell me more about the news"), answer that part on its own afterwards.
