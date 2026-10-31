# Node-RED flows

Ask the bot about the flows in your Node-RED: which ones you have, run one, turn one on or off. Comes with the 14 home flows the PuffPAI HomeBot ran (news, weather, a daily briefing, Home Assistant routines and more), cleaned for any house, for you to import. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Node-RED flows is a PuffPAI tool package (`kind: tool`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot MCP tools at `/mcp`. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `list_flows` | Each flow's name, id, on or off, and the web paths it answers | no | which Node-RED flows do I have |
| `run_flow` | Calls one of a flow's own web paths (GET or POST) with an optional small JSON body, and says what came back | yes | run the daily news flow in Node-RED |
| `enable_flow` | Turns one flow on, by name or id | yes | turn on the weather alerts flow in Node-RED |
| `disable_flow` | Turns one flow off, by name or id | yes | turn off the health check flow in Node-RED |
| `node_red_status` | Whether Node-RED answers at the saved address, and its version | no | is Node-RED running |
| `set_address` | Saves your Node-RED address | yes | my Node-RED is at 192.168.1.20 port 1880 |

`run_flow` calls only a path one of your flows defines (an `http in` node), on a flow that is turned on; any other path is refused before anything is sent. It sends no Node-RED login to a flow.

**What it needs.** A Node-RED you run yourself, on this computer or your home network. Nothing from the internet, and no brain.

- The address: `http://127.0.0.1:1880` (this computer) until you set another with `set_address`. It must be a loopback address, a home address (10.x, 172.16 to 172.31.x, 192.168.x, or IPv6 fc00::/7 like fd00::1), a link-local address (169.254.x, fe80::), `localhost`, or a name ending in `.local` or `.lan`. Port 1880 if none is given. Anything else is refused. Off this computer, port 443 goes with `https://` and port 80 with `http://`: the other way round is refused, because the Bridge could never let it through.
- `needs.addresses` is `lan`. When you set an address off this computer, the bot raises a Yes card for that address and port, and the owner can also allow one herself on the Bridge page. An address on this computer needs no Bridge step. A link-local address can be saved, but it can't be allowed.
- `NODE_RED_TOKEN`, only if your Node-RED admin API asks for a login (`adminAuth` in Node-RED's `settings.js`). PuffPAI keeps it in the vault and hands it over as an environment value at start. It is sent to the Node-RED admin API only, and never saved, logged or shown.

**Setup steps.** One `confirm` step, "Import the flows into your Node-RED": open `flows/README.md`, import `flows/homebot-flows.json` in Node-RED (menu, Import, new flows), and set the environment values it lists. Skip it if you only want to manage flows you already have. The flows file is the whole of what this package ships for Node-RED; see `flows/README.md` for what each tab does, what it needs, and which ones need editing.

**Data.** One file, `nodered.json`, in this package's data folder (`PUFFPAI_DATA`): the Node-RED address and when it was saved. Nothing else is kept: no flow lists, no answers, no login. Every save replaces the whole file at once. A file that will not read is never written over. The address is checked again before every request, so a file edited by hand to point outside your home network is refused.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only, and every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each call to Node-RED has a 10 second limit, follows no redirect, and reads at most 1 MB. A Node-RED off this computer is reached through the Bridge proxy PuffPAI gives the package, never around it. A name (`.local`, `.lan`) is looked up when you set it, the request carries the name as saved, and the Bridge checks the name's answers when it connects. With no proxy, a name is looked up at each call, every answer must be a home or loopback address, and the connection goes to the address that was checked.

## Rough edges

- **A Node-RED on another computer needs the owner's Yes.** Off this computer, calls go through the Bridge, which lets a package reach a home address only when the owner has allowed that exact address and port for it. A number or a `.local` or `.lan` name can be allowed. A Node-RED on this computer needs nothing.
- **Some shipped flows need editing.** Five tabs call HomeBot-only endpoints and ship turned off (`calendar_sync`, `grocery_list`, `meal_planner`, `network_scan`, `block_device`); three more have one HomeBot-only part (`daily_briefing`, `health_check`, `weather_alerts`). `flows/README.md` says which.
- **The weather flows are United States only** (api.weather.gov), and have no location until you set one.
- **Node-RED's web paths have no login by default.** Anything on your network that reaches port 1880 can run the flows too. Keep Node-RED on this computer, or turn on `httpNodeAuth`. This package does not send `httpNodeAuth` logins, so a flow behind one answers 401 to `run_flow`.
- **HTTPS needs a certificate that checks out.** A self-signed certificate on your Node-RED is refused; use http on your home network, or a certificate your computer trusts.
- **Flows inside subflows are not listed**, and only `http in` paths that take GET or POST can be run. Flows started by a timer or a button only run on their own schedule.
- **Left out from the HomeBot:** the voice flow (`puffpai_voice`) that fed the HomeBot's own conversation endpoint; importing flows for you; and writing new flows. Writing or changing a flow's insides is done in Node-RED.

Cypher Sisters. puff puff pass.
