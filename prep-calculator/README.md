# Prep Calculator

Emergency preparedness sums for the bot, worked out in plain code so they are never guessed: water, food, batteries, solar, generator fuel, bleach for drinking water and unit conversions. It works with no internet. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Prep Calculator is a PuffPAI tool package (`kind: tool`): one small server on this computer (Python standard library only, nothing to install) with eight MCP tools for the brain, at `/mcp`. It has no page. Every answer is a short plain sentence that repeats the inputs, gives US and metric units, and names the planning figures it used.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `water_needed` | Water to store for people (and pets) for some days, in US gallons and liters, and how many 1 gallon jugs, 5 gallon cans or 20 liter cans | no | how much water do 4 people need for 2 weeks |
| `water_on_hand` | How many days the water you have lasts | no | how long will 20 gallons of water last 2 people |
| `food_needed` | Kilocalories for adults, children and heavy workers for some days, and roughly how much rice, dried beans, oats, peanut butter or canned beans that is | no | how much rice feeds 2 adults for a month |
| `battery_runtime` | Hours a battery runs a load, from watt-hours or amp-hours and volts, its type, and whether an inverter is used | no | how long will a 1280 watt-hour lithium battery run a 100 watt load |
| `power_budget` | Watt-hours a day for a list of devices, and the battery and solar panel watts to cover it | no | what battery and solar do I need for a 60 watt fridge all day |
| `generator_fuel` | A rough range of gasoline or propane for a load and hours, with the carbon monoxide safety line | no | how much gas does a generator use running 2000 watts for 10 hours |
| `bleach_for_water` | Drops of plain unscented household bleach (5 to 9 percent only) for clear or cloudy water, with the steps that follow | no | how much bleach for 5 gallons of water |
| `unit_convert` | Gallons, liters, quarts; pounds, kilograms, ounces, grams; Fahrenheit, Celsius; miles, kilometers; watt-hours and amp-hours at a voltage | no | convert 5 gallons to liters |

Every input is range-checked. Nonsense (negative people, a million days, 12 percent bleach, a load bigger than a small generator) gets a plain refusal, not a guess. Every formula and every figure is in `PROCESS.md`; the figures sit in one table at the top of `server.py`.

**What it needs.** Nothing: no internet, no brain, no keys.

**Setup steps.** None.

**Data.** None. It stores nothing; there is no file in its data folder.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Request bodies are capped at 64 KB; a body nested too deeply, a number too big, or MCP params of the wrong shape get a plain refusal.

## Rough edges

- Planning figures only. This is not medical, electrical or engineering advice. Local official guidance wins.
- Water: 1 US gallon per person per day covers drinking and basic hygiene only. Heat, pregnancy, nursing, illness and cooking need more. The pet figure is a medium dog.
- Food: 2,000, 1,500 and 3,000 kcal a day are rough defaults; real needs vary with age, size, work and health. Food energy values are approximate and differ by brand; read the label. One food alone is not a balanced diet.
- Batteries and solar: usable depth, inverter efficiency, peak sun hours and system efficiency are planning figures. Cold, age, high draw and cloudy weeks all cut what you get.
- Generator fuel is a rule of thumb for a small 3 to 5 kW unit, given as a range. It has no figure for diesel. Read the generator's own manual.
- Bleach: only plain unscented household bleach of 5 to 9 percent. For any other strength, or any other product, follow the product label and official guidance. Bleach does not remove chemicals or fuel.

Cypher Sisters. puff puff pass.
