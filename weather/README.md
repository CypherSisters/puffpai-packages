# Weather

The weather where you live: right now, a forecast for up to 7 days, and severe weather alerts in the United States. Free public weather services, no account, no key. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Weather is a PuffPAI tool package (`kind: tool`, no page). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain these tools at `/mcp`. The one tool that changes something is listed in `abilities.changes`, so Drawbridge reviews it and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `set_location` | Saves the place (a town name, or coordinates like `40.71,-74.01`) and the unit, `c` or `f`. A town name is looked up once to get its coordinates. Default unit: Fahrenheit in the US, Celsius elsewhere. | yes | set my weather location to Asheville, North Carolina |
| `current_weather` | Temperature, sky, feels-like, humidity and wind right now | no | what's the weather like right now |
| `forecast` | Today and the next days (1 to 7, default 3): sky, high and low, chance of rain or snow | no | will it rain tomorrow |
| `severe_alerts` | Active alerts from the US National Weather Service (storms, floods, heat advisories). Outside the United States it says plainly there are none to check. | no | are there any severe weather alerts |

**What it needs.** The internet, for three hosts, all in `needs.hosts`:

- `geocoding-api.open-meteo.com`: only when you give a town name, once, to get its coordinates. Give coordinates instead and this host is never asked.
- `api.open-meteo.com`: the current weather and the forecast.
- `api.weather.gov`: US severe weather alerts. It asks every caller for a User-Agent; Weather sends `PuffPAI weather package`, which names no one.

No keys, no account, no brain. Each request carries only the place's coordinates (rounded to 4 decimals, about 10 metres) and the units. Like any web request, those sites also see your home's internet address.

**Setup steps.** None. The first time you ask for the weather, the bot tells you to set your place.

**Data.** One file, `weather.json`, in Weather's data folder (`PUFFPAI_DATA`): the place's name, coordinates and country code, and the unit. Weather answers are not stored. Every save replaces the whole file at once. A file that will not read is never written over. On remove, the owner is asked whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Outside calls go through the Bridge (the Core's proxy), wait at most 15 seconds, read at most 1 MB, follow a redirect only to the same https host, and go only to the three hosts above.

## Changed from HomeBot

- HomeBot read Home Assistant weather sensors for the current weather and asked a Node-RED flow, then the National Weather Service, for alerts. This package needs no Home Assistant and no Node-RED; it asks Open-Meteo and the National Weather Service directly.
- HomeBot could find your place from your internet address (through ipapi.co) and looked names up with OpenStreetMap. Here you say the place once, and it is looked up with Open-Meteo's own geocoder, so one fewer outside service is involved.

## Rough edges

- No air quality, pollen or smog. HomeBot had these from another Open-Meteo service; they are not in this first version.
- Alerts are for the United States only. Other countries' alert services are not built.
- Coordinates given without a town name are placed in the United States by rough boxes around the lower 48, Alaska, Hawaii and Puerto Rico. Near a border, set the unit yourself, and US alerts may be checked (or skipped) for a place just over the line.
- A town name with many matches (Springfield) takes the first one Open-Meteo gives. Add the state or country, or give coordinates.
- No hourly forecast, and no weather for a second place without changing the saved one.
- If the Bridge is closed for one of the three hosts, the tool says which host could not be reached; it does not guess the weather.

Cypher Sisters. puff puff pass.
