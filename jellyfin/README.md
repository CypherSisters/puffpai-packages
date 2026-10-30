# Jellyfin media

Search your own Jellyfin library and play on its players: a Jellyfin app on the TV, a phone, a browser tab. Pause, skip, stop, set the volume, ask what is playing, and make music playlists. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Jellyfin media is a PuffPAI tool package (`kind: tool`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain MCP tools at `/mcp`. It asks your Jellyfin server to play on one of the players connected to it, through Jellyfin's `/Sessions` API. It does not play on this computer's own speakers. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `set_address` | Saves your Jellyfin address (only one on this computer or your home network) | yes | set my Jellyfin address to http://192.168.1.20:8096 |
| `search_media` | Up to 20 songs, albums, artists, movies or shows matching your words, with type, name, year or artist, and item id | no | do I have any Fleetwood Mac in Jellyfin |
| `list_players` | The players Jellyfin can control: client, device, user, what is playing | no | which Jellyfin players are on |
| `play` | Plays one item, or the best match for your words, on a named player | yes | play Abbey Road on the living room TV |
| `control` | Pause, unpause, stop, next, previous, or volume 0 to 100 on a player | yes | pause the music |
| `now_playing` | What each player is playing, paused or playing, how far in | no | what is playing on Jellyfin |
| `playlists` | Your Jellyfin playlists with how many items each has | no | list my Jellyfin playlists |
| `create_playlist` | A new music playlist, optionally filled with songs found by name or id | yes | make a playlist called Road Trip with Dreams by Fleetwood Mac |
| `add_to_playlist` | Adds songs, found by name or id, to a playlist | yes | add Here Comes the Sun to my Road Trip playlist |

When only one player is on, you do not need to name it. When several are on, the tool lists them and asks which one; for `control` it picks the one that is playing if only one is.

**What it needs.**

- A Jellyfin server on this computer or your home network.
- A Jellyfin API key, `JELLYFIN_API_KEY`: in Jellyfin open Dashboard, API Keys, and add one. PuffPAI keeps it in the vault and hands it to the package as an environment variable when it starts. The package never writes it to disk, never logs it and never puts it in an answer.
- The Bridge must allow your Jellyfin address for this package. `needs.hosts` is empty because the address is yours, not one the package knows in advance.

**Setup steps.**

1. Add the key when PuffPAI asks for `JELLYFIN_API_KEY`.
2. One `confirm` step: tell the bot your Jellyfin address in chat, for example "set my Jellyfin address to http://192.168.1.20:8096" with your own address. Jellyfin's usual port is 8096; when you leave the port off, 8096 is used for `http://` (8920, Jellyfin's own HTTPS port, for `https://`).
3. Allow that address on the Bridge page.

Addresses it takes: `http://` or `https://` only, with a host that is loopback (127.x, ::1), private (10.x, 172.16.x to 172.31.x, 192.168.x), link-local (169.254.x, fe80::), or a name ending in `.local` or `.lan`. It refuses public addresses, `localhost` (use 127.0.0.1), a name or password inside the address, and anything after `?` or `#`. A `.local` or `.lan` name is looked up when you set it (refused and not saved if it points outside those ranges; saved with a note if it does not resolve yet) and again before every call.

**Data.** One file, `jellyfin.json`, in the package's data folder (`PUFFPAI_DATA`): `{address, last_updated}`. Nothing else is kept: no library, no history, no key. Every save replaces the whole file at once (temp file, then rename). A file that will not read is never written over.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_TOKEN` and `JELLYFIN_API_KEY` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Calls to Jellyfin go through the Bridge proxy the Core gives it, time out after 10 seconds, never follow a redirect, read at most 2 MB, and carry the key as `X-Emby-Token` only to the saved address.

## Changed from HomeBot

- HomeBot played on the box's own speakers and knew its own Jellyfin. This package plays on a Jellyfin player you name, at the address you set.
- HomeBot picked the most recent player on its own. Here, with several players on, it asks which one.
- Song, album, artist, movie and show search are one tool, `search_media`, with a `kind` to narrow it.

## Rough edges

- Not built here from HomeBot: books and comics (Calibre), favorites, removing songs from a playlist, playlists made from a list of artists or from a document, library stats and scans, the iPod import, radio and Spotify.
- Playlists are music playlists. They belong to the first Jellyfin administrator, because an API key has no user of its own.
- Artist search uses Jellyfin's item search; some Jellyfin versions list artists only through their own artists page, so an artist can be missing from `search_media` while its songs show.
- `play` plays the first match for your words. If that is the wrong one, search first and play by item id.
- Names from your library (song titles, player names) go to the brain as they are, shortened and with odd characters removed.
- A player only shows up while a Jellyfin app is open on it and allows remote control.
- An `https://` Jellyfin with a self-signed certificate is refused, because the certificate is checked. Use `http://` on your home network, or give Jellyfin a certificate this computer trusts. With `http://`, the key crosses your home network unencrypted, as it does for any Jellyfin app.

Cypher Sisters. puff puff pass.
