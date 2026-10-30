# Prep Supplies

An emergency supplies tracker: food, water, medicine, first aid, batteries, fuel and the rest, with quantities and expiry dates, how many days the water and food would last your household, and what a basic 72-hour kit still lacks. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Prep Supplies is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/prep-supplies/`) and a tray item. The page shows the days of water and food at the top with the number of people in the household, the supplies grouped by category, and lets you add, edit and delete them. Items that have expired, or expire in the next 30 days, are flagged;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `add_supply` | Add a supply: name, category, quantity, unit, expiry date, and how much one person uses a day (optional). The same item with the same expiry date adds to its count | yes | add 10 gallons of bottled water to the emergency supplies, expiring 2027-06-01 |
| `update_supply` | Change a supply's quantity, unit, expiry date, name, category or daily use, by its name or id | yes | we have 6 gallons of bottled water left in the emergency supplies now |
| `remove_supply` | Take a supply off the list, by its name or id | yes | take the old flashlight off the emergency supplies |
| `list_supplies` | All supplies, one category, or (`expiring: true`) those expired or expiring in the next 30 days | no | which emergency supplies are expiring soon |
| `days_remaining` | Days of water and food for N people (default: the household size saved on the page), with the assumptions spelled out | no | how many days would our emergency water and food last |
| `checklist` | What a basic 72-hour kit still lacks, from a small built-in list, compared with what is stocked | no | what is our 72-hour emergency kit still missing |

Categories: food, water, medicine, first aid, batteries, fuel, light, sanitation, tools, other.

**How the days are counted.** Each person uses 1 gallon (about 3.8 liters) of water a day and eats 3 servings (about 2,000 calories) a day. Units the count knows: gallons, liters, quarts for water; servings, meals, days, calories for food. An item in any other unit (cans, packs) counts once you give it a daily use per person. Expired items do not count. The answer says all this every time.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None.

**Data.** One file, `prep.json`, in the package's data folder (`PUFFPAI_DATA`): `{supplies: [...], people, last_updated}`. Every save replaces the whole file at once (temp file, then rename) and stamps it. A file that will not read is never written over. On remove, PuffPAI asks whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/summary`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/prep-supplies/`, which adds the token. The page checks the stamp every few seconds and reloads when the brain changed something.

## Changed from HomeBot

- Rewritten from HomeBot's `prep_*` tools and its `EmergencyPrep` class. Tools take named fields instead of one `category|item|quantity|unit|expiration` string.
- HomeBot only counted water in gallons and food in servings. Here liters, quarts, meals, days and calories count too, and any item can carry its own daily use.
- HomeBot's recommendation list (14 days, scaled for a family of four) is replaced by the basic 72-hour kit list.
- Expired items no longer count toward the days.

## Rough edges

- Weather alerts and the location setting are not here; the `weather` package does weather.
- No reminders: nothing tells you on its own when something is about to expire. Ask, or look at the page.
- The 72-hour checklist matches by category and by words in the item name (a "LED lantern" counts as a light). An item named oddly may not be recognised; put it in the matching category.
- Food in cans or packs needs a daily use per person before it counts toward the days. The page and the answer both list what was not counted.
- One household size for the whole list. There is no split by person, pet or age.

Cypher Sisters. puff puff pass.
