# Home Assistant

Read and control your own Home Assistant from the bot, by chat or by voice: which lights are on, turn the fan off, dim the lamp, set the thermostat, run a scene, list the cameras, see which motion sensors changed lately. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Home Assistant is a PuffPAI tool package (`kind: tool`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain MCP tools at `/mcp`. Every tool that changes something is listed in `abilities.changes`, so Drawbridge reviews it and the owner can ask for a Yes. Every change answers what it did in one plain sentence.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `set_address` | Save your Home Assistant address (home network only, port 8123 if none) | yes | my Home Assistant is at homeassistant.local:8123 |
| `list_devices` | Devices and their state, by kind (light, switch, fan, climate, cover, lock, media_player, scene, sensor, binary_sensor, camera) or by name, up to 100 | no | what lights do I have |
| `device_state` | One device's state and a few details (brightness, temperature, position, battery) | no | is the porch light on |
| `turn_on` | Turn on a light, switch, fan or media player; open a blind, curtain, shade or awning | yes | turn on the kitchen light |
| `turn_off` | Turn off a light, switch, fan or media player; close a blind, curtain, shade or awning | yes | turn off the TV |
| `set_light` | Brightness in percent, a colour name or rgb, or white colour temperature (warm, neutral, cool, daylight or kelvin) | yes | dim the living room lamp to 30 percent |
| `set_temperature` | A thermostat's target temperature, and its mode if asked | yes | set the thermostat to 21 |
| `run_scene` | Run a scene by name | yes | run the movie night scene |
| `list_cameras` | The cameras and their state, no pictures | no | list my cameras |
| `recent_motion` | Motion and occupancy sensors that changed in the last few minutes (default 30) | no | any motion in the last half hour |

What it never does: lock or unlock a door (locks are read-only here), open or close a garage door, gate or door, arm or disarm an alarm, run a scene that touches any of those, run scripts or automations, or show a camera picture.

**What it needs.**

- Your own Home Assistant on your home network, and its address. The address must be loopback, a private address (10.x, 172.16 to 172.31, 192.168.x), a link-local address, or a name ending in `.local` or `.lan` (like `homeassistant.local`). Anything else is refused.
- **The Bridge must allow that address for this package. Nothing works until it does.** The package declares no internet hosts (`needs.hosts` is empty), because your Home Assistant is on your own network and its address is yours to set.
- A long-lived access token, `HOME_ASSISTANT_TOKEN` (`needs.keys`). Make it in Home Assistant: open your profile, Security, Long-lived access tokens. It goes into the vault and reaches this package only as an environment variable. It is never written to disk or logs and never said in an answer.

**Setup steps.** One step: find your Home Assistant address (usually `http://homeassistant.local:8123`), then tell the bot, for example "my Home Assistant is at homeassistant.local:8123". Add the token when the Yes card asks for it, and let the Bridge allow the address.

**Data.** One file, `ha.json`, in this package's data folder (`PUFFPAI_DATA`): the address you set and when. Never the token. Every save replaces the whole file at once. A file that will not read is never written over.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_TOKEN` and `HOME_ASSISTANT_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). It calls Home Assistant's REST API (`/api/states`, `/api/states/<entity>`, `/api/services/<domain>/<service>`) through the Bridge, with a 10 second timeout, reads at most 2 MB, and never follows a redirect to another address. A name like `homeassistant.local` must also resolve to a home-network address: `set_address` refuses a name that points outside and saves nothing, says so when the name cannot be found yet, and every call checks again before anything is sent.

## Changed from HomeBot

- One address the owner sets, checked to be on the home network, instead of a configured bridge.
- Covers are opened and closed only when Home Assistant says they are a blind, curtain, shade or awning. HomeBot refused all covers; here any other cover, or one with no device class, is still refused.
- Scenes run only when every device they touch can be seen and none is a lock, alarm, valve, water heater, siren, camera, or a cover that is not a blind, curtain, shade or awning.
- Scripts are not run, and there is no Zigbee pairing, no dashboard tiles and no Home Assistant configuration changes.

## Rough edges

- HTTPS with a self-signed certificate does not work: the certificate is checked. Use `http://` on your home network, or a certificate your computer trusts.
- A very large Home Assistant (thousands of entities) can send a device list over 2 MB; then listing stops with a plain message.
- Roller shutters, windows and covers without a device class are refused, even when they are harmless. Set a device class in Home Assistant, or use Home Assistant itself.
- A `switch` wired to a gate or garage motor looks like any switch, and this package cannot tell. If you have one, keep the Yes on for this package's changes.
- Thermostats in heat_cool mode that need a low and a high target are not set yet.
- Motion means `binary_sensor` devices whose class is motion or occupancy. Camera person or visitor sensors with another class are not counted.
- A name like `homeassistant.local` is checked when it is used, and the Bridge looks it up again when it connects. A device on your network that answers for that name in between is not caught.
- IPv6 home addresses other than loopback and link-local (like fd00:: addresses) are not taken yet. Use the IPv4 address or the name.

Cypher Sisters. puff puff pass.
