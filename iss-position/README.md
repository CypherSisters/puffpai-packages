# ISS position

Where the International Space Station is right now, in plain words: "The ISS is over the Pacific Ocean, west of the United States, at latitude 37.1, longitude -122.9, about 422 km up, moving 27,583 km/h." By Cypher Sisters, Apache-2.0.

This is the HomeBot skill `iss_position` (a sandboxed example skill in HomeBotFounders) rewritten as a PuffPAI package.

## PuffPAI

**What it does.** ISS position is a PuffPAI tool package (`kind: tool`): one small server on this computer (Python standard library only, nothing to install) with one MCP tool for the brain, at `/mcp`. It has no page.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `iss_position` | Latitude, longitude, height in km, speed in km/h, whether the station is in sunlight, and a plain place such as "over the Pacific Ocean, west of Peru" | no | where is the space station right now |

The plain place is worked out on this computer from the latitude and longitude, with a small built-in table: rough outlines of the continents, a list of countries and islands, and rough boxes for the oceans and the bigger seas. No second website is asked.

**What it needs.** The internet, for one host only: `api.wheretheiss.at` (public, no key, no account). No brain, no keys.

**Setup steps.** None.

**Data.** None. It stores nothing; there is no file in its data folder.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each call makes at most two requests to `https://api.wheretheiss.at/v1/satellites/25544` (one try and one retry), 20 seconds in all, reads at most 64 KB of the answer and never follows a redirect. It goes out through the Bridge proxy the Core hands it.

## Rough edges

- No "when will it pass over us" tool. api.wheretheiss.at gives the position now (and positions at times you name), but no pass predictions, and the key-free sites that did predict passes are gone or need a key. Working passes out here would need the station's orbit numbers and a lot more code, so it is left out.
- The place names are rough. The continent outlines are a few dozen points each, so near a coast the station can be called "over" a country while it is just offshore, or the other way round. Big countries have a few reference points, so "nearest" can pick a neighbour near a border.
- Far from any listed place it says "far from land" rather than naming a spot.
- If the website is down or slow it says so plainly; it does not guess a position.

Cypher Sisters. puff puff pass.
