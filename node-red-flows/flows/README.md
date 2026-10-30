# The HomeBot flows for Node-RED

`homebot-flows.json` is a Node-RED flows file with 14 tabs (flows). They are the home automations the PuffPAI HomeBot ran, cleaned of everything that belonged to one house: no addresses, no names, no location, no keys. You import them into your own Node-RED by hand; this package never imports or edits them for you.

## Import them

1. In Node-RED, open the menu (top right), choose **Import**.
2. Choose **select a file to import**, pick `flows/homebot-flows.json` from this package (or paste its contents).
3. Choose **new flows**, then **Import**, then **Deploy**.
4. Set the environment values below for the tabs you want, and restart Node-RED.

Tabs marked **needs editing** come in turned **off**. Edit them first, then turn them on in Node-RED (double-click the tab, Enabled) or ask PuffPAI to turn the flow on.

## The tabs

| Tab | Web path (POST) | What it does | What it needs |
|---|---|---|---|
| `daily_news` | `/webhook/daily-news` | Top 10 headlines from an RSS feed | Nothing; `NEWS_RSS_URL` to change the feed (default: the New York Times home page feed) |
| `daily_weather` | `/webhook/daily-weather` | Today's forecast, tonight, the next periods and active alerts | A weather source: api.weather.gov (United States only), and `WEATHER_DEFAULT_LAT` / `WEATHER_DEFAULT_LON`, or `lat` and `lon` in the request |
| `weather_alerts` | `/webhook/weather-alerts` | Active weather alerts for one point. Also runs every 2 hours | Same as `daily_weather`. **Needs editing** for one part: when there is an alert, the scheduled run posts it to a HomeBot-only endpoint (`/agents/event`) |
| `daily_briefing` | `/webhook/daily-briefing` | A morning briefing: weather, today's events, tasks, headlines | Same weather values. **Needs editing** for the calendar part: it reads `/apps/calendar/data` from the HomeBot API. Without it the briefing says no events |
| `health_check` | `/webhook/health-check` | Says whether the PuffPAI Core, Ollama and Home Assistant answer. Also runs every 5 minutes | `PUFFPAI_API_URL`, `OLLAMA_URL`, `HA_URL`, `HA_AUTHORIZATION`. **Needs editing** for one part: when something is down, the scheduled run posts to a HomeBot-only endpoint (`/agents/event`) |
| `media_now_playing` | `/webhook/media-now-playing` | What is playing on Jellyfin, and library counts | `JELLYFIN_URL`, `JELLYFIN_API_KEY` |
| `good_morning` | `/webhook/good-morning` | Turns on one light (brightness 1 to 100, default 70) and reads one temperature sensor | Home Assistant: `HA_URL`, `HA_AUTHORIZATION`. Edit the two entity ids in the flow (`light.living_room`, `sensor.openweathermap_temperature`) to yours |
| `leaving_home` | `/webhook/leaving-home` | Turns off every light | Home Assistant: `HA_URL`, `HA_AUTHORIZATION` |
| `security_check` | `/webhook/security-check` | Reads every Home Assistant state and counts unlocked doors, inactive cameras, motion sensors and the alarm state. Changes nothing | Home Assistant: `HA_URL`, `HA_AUTHORIZATION` |
| `calendar_sync` | `/webhook/calendar-sync` | Upcoming events, overdue tasks, medicines and bills due | **Needs editing, ships off.** Reads `/apps/calendar/data` from the HomeBot API |
| `grocery_list` | `/webhook/grocery-list` | A shopping list by store department, from the pantry and shopping list | **Needs editing, ships off.** Reads `/apps/freshtable/data` from the HomeBot API |
| `meal_planner` | `/webhook/meal-planner` | A week of meals from what is in the pantry | **Needs editing, ships off.** Reads `/apps/freshtable/data` from the HomeBot API |
| `network_scan` | `/webhook/network-scan` | Sorts a posted device list into trusted and needs-review. Also runs every 15 minutes | **Needs editing, ships off.** The schedule reads devices from `/firewall/devices` on the HomeBot API |
| `block_device` | `/webhook/block-device` | Blocks or unblocks one IPv4 address, or turns lockdown on or off, after checking the address | **Needs editing, ships off.** Calls the HomeBot firewall API (`/firewall/block`, `/firewall/unblock`, `/firewall/lockdown`). It changes your network |

The PuffPAI Core (v2) does not have the HomeBot's `/apps/...`, `/firewall/...` or `/agents/event` endpoints. Point those calls at your own service, or leave those tabs off.

## Environment values

These are read with `env.get()`, so they must be real environment variables of the Node-RED process (`docker run -e`, the service's `Environment=` line, or your shell). A value set in `settings.js` under `functionGlobalContext` is not enough.

| Name | Used by | Default if unset |
|---|---|---|
| `HA_URL` | good_morning, leaving_home, security_check, health_check | `http://homeassistant.local:8123` |
| `HA_AUTHORIZATION` | the same four | none: no login is sent, and Home Assistant answers 401. Set it to the whole header value: the word `Bearer`, a space, then a long-lived access token from your Home Assistant profile page |
| `PUFFPAI_API_URL` | health_check and the needs-editing tabs | `http://127.0.0.1:8420` |
| `PUFFPAI_API_KEY` | the needs-editing tabs and the scheduled posts | none: the `X-API-Key` header is left out |
| `OLLAMA_URL` | health_check | `http://127.0.0.1:11434` |
| `JELLYFIN_URL` | media_now_playing | `http://127.0.0.1:8096` |
| `JELLYFIN_API_KEY` | media_now_playing | none |
| `WEATHER_DEFAULT_LAT`, `WEATHER_DEFAULT_LON` | daily_weather, weather_alerts, daily_briefing | none: there is no built-in location. Without them (and without `lat` and `lon` in the request) the weather tabs answer 400 "no location" |
| `NEWS_RSS_URL` | daily_news, daily_briefing | the New York Times home page RSS feed |
| `PUFFPAI_USER_AGENT` | the weather and news tabs | `PuffPAI-node-red-flows/1.0`. api.weather.gov asks for a User-Agent that says who you are; put your own app name and a way to reach you here |

The file holds no secret values, only `env.get(...)` lookups.

## Rules these flows keep (keep them if you edit)

- **Anything that can reach port 1880 can call these web paths**, with any body. Node-RED's web paths have no login by default. Keep Node-RED on this computer (`uiHost: "127.0.0.1"` in `settings.js`) or turn on `httpNodeAuth`, especially for `good_morning`, `leaving_home` and `block_device`, which change things in your home.
- **A credential goes only to the host it belongs to.** Every host and every credential comes from the environment. No tab takes a host or a credential from the request body. (The HomeBot `health_check` took a Home Assistant address from the body; that is gone.)
- **Every answer path ends with `delete msg.headers;`**, so the outbound login headers are never sent back to the caller. Any new answer path needs the same line.
- **State-changing tabs take almost nothing from the body.** Entity ids are fixed in the flow; the only caller value is `good_morning`'s brightness, clamped to 1 to 100. `block_device` checks the address before any call and refuses loopback, multicast, link-local, broadcast and reserved addresses.
- **Weather coordinates are checked numbers** (latitude within 90, longitude within 180) before they go into a URL. Only the coordinates and the User-Agent go to api.weather.gov.

## Changed from the HomeBot copy

- The voice tab (`puffpai_voice`) is left out: it only worked with the HomeBot's own conversation endpoint and one Home Assistant text entity.
- Every house address is replaced: Home Assistant is `http://homeassistant.local:8123`, the PuffPAI API is `http://127.0.0.1:8420`, Ollama and Jellyfin default to this computer on their usual ports. Comments that named old addresses are gone.
- The built-in home coordinates are gone; set your own.
- The Home Assistant login is one value, `HA_AUTHORIZATION`, holding the whole header, instead of a bare token the flow put a prefix on.
- The PuffPAI API credential comes from the environment only; a request body can no longer supply one.
- `network_scan` works out home addresses from the three private IPv4 ranges instead of one house's subnet, and no longer trusts every `172.` address.
- Five tabs that depend on the HomeBot API ship turned off.
- The three `join` nodes leave out their `key` setting, which was Node-RED's own default (`topic`).

Cypher Sisters. puff puff pass.
