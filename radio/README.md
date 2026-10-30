# World Radio

Internet radio on its own page: find stations from the radio-browser.info directory, play them in the browser, keep favorites. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** World Radio is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (search, results, favorites and a now-playing bar with the player, at `/pkg/radio/`) and a tray item;
- MCP tools for the brain, at `/mcp`. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes |
|---|---|---|
| `find_stations` | Search by name, country (name or 2-letter code), genre or language. The top 15, most voted first, numbered, with country, bitrate and stream address. Plays nothing. | no |
| `play_station` | Set the page's "now playing" to a station: a number from the last search, a favorite's name, or a name (searched if needed). The open page starts it. | yes |
| `stop` | Stop the radio on the page | yes |
| `now_playing` | Which station is set, and whether the page is open | no |
| `favorites` | List the saved stations | no |
| `add_favorite` | Save a station; with no station, the one playing now | yes |
| `remove_favorite` | Remove a favorite by name or number | yes |

**Where the sound plays.** On the World Radio page, in the browser, and only while that page is open. The page asks the server what to play every 2 seconds, so "play KEXP" in chat starts it within 2 seconds. Browsers block sound until you click on a page once, so the first play may need one click; the page says so and shows a Play button. `play_station` and `now_playing` say honestly whether the page is open (it counts as open when it asked in the last 6 seconds).

**What it needs.** The internet, for station search only: `de1.api.radio-browser.info`, and `all.api.radio-browser.info` when de1 does not answer. No keys, no brain, nothing to set up. The server never fetches a stream: a station you pick plays straight from that station's own server into the page, so the page loads whatever stream address the station lists.

**Setup steps.** None.

**Data.** One file, `radio.json`, in World Radio's data folder (`PUFFPAI_DATA`): `{favorites: [...], now_playing, play_id, last_updated}`. Every save replaces the whole file at once. A file that will not read is never written over. On remove, the owner is asked whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). GET routes never change anything; the page's buttons POST to `/api/search`, `/api/play`, `/api/stop`, `/api/add_favorite` and `/api/remove_favorite`. Station search goes out through the Bridge proxy (`HTTPS_PROXY`), with an 8 second timeout, a 2 MB answer cap, the User-Agent `PuffPAI radio package`, and redirects followed only to the two declared hosts.

## Changed from HomeBot

- HomeBot played radio through a speaker on the box. This plays on the World Radio page in the browser.
- HomeBot also searched TuneIn for local call letters. This uses radio-browser.info only.

## Rough edges

- No sound unless the World Radio page is open somewhere. Closing the page stops the sound; "now playing" stays set, and opening the page again starts it.
- Some stations use formats a browser can't play, or are off the air. The page says so when a stream fails; the directory's "broken" filter catches most, not all.
- HLS stations (`.m3u8`) are left out of search results, because the page's player is a plain audio element.
- An `https` Desktop may refuse to play `http` streams (mixed content). On the usual `http://127.0.0.1` Desktop they play.
- HomeBot's genre and country word lists ("french jazz", "news from japan") and its "a different station each time" shuffle are not carried over. Search with the separate name, genre, country and language fields.
- The numbered list for "play the second one" is the last search, kept in memory; it is gone after a restart.

Cypher Sisters. puff puff pass.
