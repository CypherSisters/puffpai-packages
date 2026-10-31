# News headlines

A plain news feed reader for the bot: add the RSS or Atom feeds you like, then ask for the latest headlines. Titles, sources, times and links; no article text, no summaries, no feeds chosen for you. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** News headlines is a PuffPAI tool package (`kind: tool`, no page). It runs one small server on this computer (Python standard library only, nothing to install) and gives the brain these tools at `/mcp`. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `add_feed` | Saves a feed address (http or https) and a short name. It raises a Yes card for the feed's site and port, and says what the Core answered. Adding a feed already on the list asks again. | yes | add the news feed at https://www.example.org/news/rss.xml and call it Local Paper |
| `remove_feed` | Removes a feed by name or address, and its saved headlines. A site that was allowed stays allowed until the owner removes it on the Bridge page. | yes | remove the Local Paper news feed |
| `list_feeds` | The feeds added so far | no | which news feeds do I have |
| `headlines` | The latest headlines across all feeds, or from one feed (1 to 30, default 10): title, source, time, link | no | what are the latest headlines |

**What it needs.** The internet, for the feeds you add, and nothing else: no keys, no account, no brain.

`needs.hosts` is empty and `needs.addresses` is `internet`: the owner picks the feeds, and each feed's site on the internet needs its own Yes, for one exact name and port, never its sub-hosts. When you add a feed, the bot raises a Yes card for that address and port, and the owner can also allow one herself on the Bridge page. Until then, `headlines` says that feed could not be reached; it does not make headlines up.

**Setup steps.** None at install. After you add a feed, say Yes on its card.

**Data.** In News headlines' data folder (`PUFFPAI_DATA`):

- `feeds.json`: the feed list (name, address, when it was added). Every save replaces the whole file at once. A file that will not read is never written over.
- `cache.json`: each feed's last 40 headlines and when they were fetched. A feed is fetched again after 15 minutes. If a fetch fails, the last saved headlines are shown with a note.

On remove, the owner is asked whether to keep them.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`).

Every fetch goes through the Bridge (the Core's proxy) and:

- takes only `http://` and `https://` addresses, with no user name or password in them;
- refuses a private, loopback, link-local or other non-public address, written as a number or reached through a name (the name is looked up first), and names like `localhost` or `something.local`;
- waits at most 15 seconds and reads at most 1 MB;
- follows a redirect only to the same host;
- reads the feed with Python's `xml.etree`, and refuses a feed that declares its own XML entities.

## Changed from HomeBot

HomeBot's `auto_news_summary` asked a Node-RED flow for the day's news, and that flow chose the sources. Here there is no Node-RED and no built-in source: the owner adds the feeds, and the package only reads them.

## Rough edges

- Only one Yes card waits at a time for this package. Adding three feeds from three sites in a row leaves only the last card, and the others wait with no card: add each of those feeds again, one at a time, to raise its card again. Five new cards an hour at most, and a site the owner said No to is not raised again for an hour.
- The Yes is for one name and one port. A feed at `http://` (port 80) that sends you on to `https://` (port 443) is refused at the Bridge until the `https://` address has its own Yes: add the feed with its `https://` address. A feed whose site moves to another host (a redirect) is not followed; add it again with its new address.
- A site stays allowed after its feed is removed, until the owner removes it on the Bridge page.
- No article text. A `read_item` tool (fetch an article and return its readable words) was left out: the article's site is usually not the feed's host, so it would need yet another Yes per site, and turning any web page into safe readable text is not something this package can promise yet.
- No summary by the brain, no topics and no search. Ask the bot to sum up the headlines it read if you want that.
- The private-address check looks the name up once before the fetch; the Bridge looks it up again to connect. A site that changes its address in between could slip through. The Bridge's own rules still apply.
- Feed times are shown in this computer's time zone. A feed item with no date shows "time not given" and sorts last.
- At most 50 feeds.

Cypher Sisters. puff puff pass.
