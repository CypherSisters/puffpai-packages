# Web search

Search the web through a SearXNG you run yourself: web results and news, the top 8 each. No account, no key, no search company asked directly. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Web search is a PuffPAI tool package (`kind: tool`, no page). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain these tools at `/mcp`. The one tool that changes something is listed in `abilities.changes`, so Drawbridge reviews it and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `web_search` | Searches through your SearXNG (category `general`, or `news`) and gives the top 8 results: title, a one-line snippet, the address and the engine that found it. Image results are left out. | no | search the web for how to store fresh basil |
| `news_search` | The same, in SearXNG's news category | no | what's in the news about the Mars mission |
| `search_settings` | Which SearXNG address is saved, and whether it answers right now (its `/healthz`, or its front page) | no | is web search set up |
| `set_address` | Saves your SearXNG's address. Only an address on this computer (`127.0.0.1`, `localhost`) or your home network (`10.x`, `172.16.x` to `172.31.x`, `192.168.x`, IPv6 `fc00::/7` like `fd00::1`, link-local, or a name ending in `.local` or `.lan` that points only at those) is taken. A public SearXNG is refused, because it would see every question. | yes | set my SearXNG address to http://192.168.1.20:8888 |

**What it needs.**

- **A SearXNG you run**, with its JSON format turned on. That is the one setup step. SearXNG itself needs its own internet access to ask search engines; that is outside this package and outside the Bridge's view of this package.
- **The Bridge.** `needs.hosts` is empty: this package never talks to the internet itself. A SearXNG on this computer is asked directly. A SearXNG on another computer at home is asked through the Bridge, which must allow that exact address and port for Web search; until it does, searches are refused and the tool says so.
- **`needs.addresses` is `lan`.** When you set an address off this computer, the bot raises a Yes card for that address and port, and the owner can also allow one herself on the Bridge page.
- No keys, no account, no brain.

What a search sends: the words you asked, `format=json` and the category, to the address you set, with the User-Agent `PuffPAI web-search package`. Nothing else: no cookies, no language, nothing about who asked. Questions are never logged or stored by this package.

**Setup steps.** One, a `confirm` step: "Run SearXNG and enable its JSON format" ([docs.searxng.org](https://docs.searxng.org/)). In SearXNG's settings, under `search:` `formats:`, add `json` next to `html`. Then tell the bot its address (off this computer, the bot raises a Yes card for that address and port, and the owner can also allow one herself on the Bridge page), and ask "is web search set up" to check it answers.

**Data.** One file, `search.json`, in Web search's data folder (`PUFFPAI_DATA`): the SearXNG address and when it was saved. Search words and results are not stored. Every save replaces the whole file at once. A file that will not read is never written over. The address is checked again before every call, so a hand-edited file can't send searches somewhere else. On remove, the owner is asked whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). Each call to SearXNG stops waiting after 15 seconds, reads at most 1 MB, sends no cookies, and follows a redirect only to the same scheme, host and port, three at most. The proxy comes from the environment the Core sets, never from Windows proxy settings.

## Changed from HomeBot

- HomeBot's chat tool asked DuckDuckGo directly, and its Toolsmith asked a SearXNG on the box itself, at one fixed loopback address from the owner's config. This package asks only SearXNG, and the owner sets the address, on this computer or the home network.
- HomeBot also had a page reader (`web_fetch`) for any public address a result pointed to. That is not in this package: the brain gets the result addresses as words and opens nothing.

## Rough edges

- A link-local address can be saved, but it can't be allowed on the Bridge, so searches to it are refused. Use a private address or a name like `searxng.lan`, or run SearXNG on this computer.
- Off this computer, port 443 goes with `https://` and port 80 with `http://`: the other way round is refused, because the Bridge could never let it through.
- If SearXNG's limiter is on, it may turn this computer away (error 429). Let this computer's address through it in SearXNG's own settings.
- No image search, no safe-search or language choice, no time range, and no paging past the top 8. SearXNG's own defaults apply.
- The tool says SearXNG's engines were quiet only when nothing came back. Partial answers from a few engines are shown as they are.
- A name ending in `.local` or `.lan` is looked up on this computer before each call and refused if any address it gives is outside the home network. Through the Bridge, the request carries the name as saved, and the Bridge checks the answers when it connects. Outside the Bridge, the request itself looks the name up again, so a home DNS server that changes its answer in between is not stopped by this package.

Cypher Sisters. puff puff pass.
