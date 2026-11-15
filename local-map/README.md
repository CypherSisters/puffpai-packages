# Local Map

An offline street map of one region you choose, with its emergency places: fire stations, police, hospitals, clinics, pharmacies, shelters, food banks, drinking water, fuel, defibrillators and more, from OpenStreetMap. You pick the region and press Download once. After that the map, the list of places, straight-line distances from home and your own points work with no internet. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Local Map is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/local-map/`) and a tray item. With no region yet, the page shows the region picker and says what will be downloaded, from which hosts and what is sent. With a region, it shows the map (Leaflet with protomaps-leaflet, drawn on a canvas, light or dark to match the Desktop), coloured square markers by kind, and a list beside it, filterable by kind and sorted by distance from home. Tap a row to centre the map there. Set home by tapping the map or typing coordinates. Add, edit and remove your own points (meeting places, water, a neighbor who can help). Every fetched point shows its source and fetch date. It is sized for a tablet and works by touch.
- MCP tools for the brain, at `/mcp`. The tools that change something are in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_my_point` also says it deletes (`destructiveHint`). No tool contacts the internet; downloading a region is done on the page only.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `nearest` | The nearest places of one kind (or any kind) from home or from a named point: name, kind, straight-line distance and direction, phone, address | no | where is the nearest hospital |
| `list_points` | Every place of one kind on the map, nearest home first | no | list the shelters on the map |
| `where_is` | Where one place is, by name: a point on the map or a town, with its distance and direction from home | no | where is City Hall on the map |
| `save_my_point` | Put one of your own points on the map, move it or rename it, by name: a point with that name is changed, otherwise it is added; the answer says which. Place is home, or latitude and longitude | yes | put our meeting place on the map at home: the library steps |
| `remove_my_point` | Take one of your points off the map, by name | yes | take the library steps off the map |
| `set_home` | Set home by coordinates | yes | set home on the map to 37.46, -122.43 |
| `map_status` | The map as a whole: region, detail, size on disk, fetch date, points by kind, and whether home is set | no | what region does the offline map have |

**Finding a place by name.** `where_is`, `save_my_point`, `remove_my_point` and `nearest` (its `from_point`) take a place by its plain name; the brain does not need to look it up first. Case, accents and a leading "the", "our" or "my" do not matter. A whole name that matches one point wins; then part of a name by whole words: each word said must be a word of the name or the start of one, three letters at least ("Ron" never finds "Sharon"). When only part of a name was said, the answer says first which point it used. When none or several match, nothing is changed and the answer names the candidates (with their ids, for your own points). `save_my_point` is the exception for none: it adds a new point with that name. So saving a short name that is part of two of your points' names ("Water" beside "Water point" and "Water barrel") changes nothing; add it on the page instead. `where_is` looks at the map's points first, then the town list; several towns with the same name are all listed with their state or region and country.

**How it picks and fetches.** See `PROCESS.md`: how a region becomes a box, how the tile list and the size are worked out, what is sent to each host, how points are sorted, and how a refresh keeps your points.

**What it needs.** The internet once, for three hosts, all in `needs.hosts`, and only when you press Check size or Download on the page:

- `build-metadata.protomaps.dev`: asked only for the list of map builds, to pick the newest.
- `build.protomaps.com`: byte ranges of that one public planet file, so it can tell which map area was fetched.
- `overpass-api.de`: one query with the four edges of the box, rounded outward to a 0.05 degree grid, so the middle of the box sent is not the point you chose.

All three see this computer's internet address. The box is rounded the same way for the street map, and the page shows the rounded box before anything is fetched. Requests name a generic client (`local-map/1.0`), not this app or you. No keys, no account, no brain. Requests go through the Bridge (the Core's proxy in `HTTPS_PROXY`), follow a redirect only to the same https host, and go only to these three hosts. Nothing is fetched at start-up or by any tool.

**Limits on what a host can send.** Every host is treated as if it could be hostile. Every byte read in one Check size or Download counts against your size limit plus 32 MB for the map's index and the places, and the job stops when that is passed. One map index (directory) is at most 16 MB unpacked and 500,000 entries, and is unpacked a step at a time so an oversized one is refused early; at most 2,000 leaf directories are read in one job, 64 are kept in memory, and all directories together are at most 128 MB. A tile is at most 4 MB and must sit inside the map file's tile section; a tile that does not read back at its exact length, or does not start like gzip when the file says gzip, is not stored and the page says how many. The build name must look like `20260102.pmtiles`. The places answer is at most 16 MB; at most 20,000 places are kept (the nearest the middle, and the page says how many were left out), with names to 200 characters, addresses to 300, phones to 60 and opening hours to 300. A request that gets no further in 30 seconds (or, for tiles, at about 20 KB a second) is stopped, a whole job stops after 30 minutes, and Cancel takes effect within a second or so even when a host is sending one byte at a time.

**Setup steps.** None. Open the page, pick a town (the list has about 172,000 towns of 1,000 people or more, each shown with its state or region and country, so towns with the same name can be told apart) or type coordinates, choose a radius and a detail (Small, Standard or Detailed), press Show the area, then Check size, then Download.

**Data.** In Local Map's data folder (`PUFFPAI_DATA`): `map.json` (the region, the fetched points, your own points, home and the size limit), `tiles.sqlite` (the street map), and `tiles-partial.sqlite` while a download is unfinished. The page shows the real size before downloading and refuses anything over your limit (250 MB unless you change it). Every save of `map.json` replaces the whole file at once. `map.json` carries the marker `"app": "local-map"` (a file saved before the marker is known by its keys). A `map.json` that will not read, is empty, or is not this app's file (another app's file, `{}`) is never written over; the page says so by file name (it never shows the folder's path). One of your points whose position will not read is never dropped: it stays in the file as it was and the list shows it as "needs fixing" until you give it a latitude and longitude (a number saved as words, like "1.5", is read as a number). A `tiles.sqlite` that will not read is moved aside as `tiles.broken-<date>.sqlite` (only the newest such file is kept) and the page says so. When home is not set, a download sets it to the middle of the map and the page says so; it is never sent anywhere. On remove, the owner is asked whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/vendor/*`, `/tiles/*`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/local-map/`, which adds the token, so the map's tile requests carry it too.

**Credits.** Map data (c) OpenStreetMap contributors, openstreetmap.org/copyright. Street map tiles from the Protomaps project. Place names from GeoNames (CC BY 4.0). Leaflet (BSD-2-Clause) and protomaps-leaflet (BSD-3-Clause) are shipped unchanged in `web/vendor`. Details and checksums: `NOTICE.md`.

## Rough edges

- It needs the internet once, to download. Until then there is no map.
- The public map host has no service promise, and its addresses may change. If they do, the download fails with a plain message and the map you have stays.
- OpenStreetMap's coverage of shelters, food banks and public health offices is uneven. Check what is listed and add your own points.
- No routing: every distance is straight-line, not by road.
- Places may have closed or moved since the fetch date shown on each point. Download again to refresh; your own points are kept.
- In the United States, 911 knows more than this map. Call it in an emergency.
- The place list holds towns of 1,000 people or more (GeoNames). For a smaller place, tap the map or type its coordinates. The list is loaded on the first search, which takes a few seconds and about 70 MB of memory.
- One region at a time, and none that crosses the 180 degree line.
- Leaflet's own stylesheet names two small images for controls this page does not use; they are not shipped.

Cypher Sisters. puff puff pass.
