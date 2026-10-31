# AdGuard Home

Read and change your own AdGuard Home from the bot: protection on or off, what it blocked, what each device looked up, and blocking or unblocking a domain. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** AdGuard Home is a PuffPAI tool package (`kind: tool`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot MCP tools at `/mcp`. It works with an AdGuard Home you already run. Every tool that changes something is in `abilities.changes`, so Drawbridge reviews it and the Core can ask you for a Yes first:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `set_address` | Save where your AdGuard Home is (no port given: tries 3000, then 80) | yes (its own settings file) | set my AdGuard address to http://192.168.1.2:3000 |
| `status` | Protection on, off or paused; DNS lookups and blocked lookups in the stats period; filter lists and rule counts | no | is AdGuard blocking ads right now |
| `top_blocked` | The most blocked domains and the busiest devices | no | what is AdGuard blocking the most |
| `query_log` | The latest lookups (up to 100): device, domain, blocked or allowed; optional device filter | no | what has the TV been looking up |
| `list_clients` | The devices AdGuard Home knows, named and found | no | which devices does AdGuard know about |
| `block_domain` | Add the rule `\|\|domain^` (the domain and its sub-domains) | yes | block ads.example.com in AdGuard |
| `unblock_domain` | Take off the block rule PuffPAI added, or add the allow rule `@@\|\|domain^` | yes | unblock example.com, it broke a site |
| `protection` | Turn protection on or off for the whole home network | yes | turn AdGuard protection back on |
| `pause_protection` | Turn protection off for 1 to 1,440 minutes; AdGuard Home turns it back on | yes | pause AdGuard for 15 minutes |

It uses AdGuard Home's own API: `/control/status`, `/control/stats`, `/control/querylog`, `/control/filtering/status`, `/control/filtering/set_rules`, `/control/clients` and `/control/protection`.

**Your rules stay yours.** Each rule this package adds sits right under a comment line `# puffpai`. It only ever removes a rule with that line above it. To unblock something your own rule or a filter list blocks, it adds an allow rule instead of touching yours. (A comment at the end of a rule line would stop AdGuard Home from matching the rule at all, which HomeBot found the hard way; that is why the mark is a line of its own.)

**What it needs.**
- Your AdGuard Home login, as two keys: `ADGUARD_USER` and `ADGUARD_PASSWORD` (`needs.keys`). They go into the vault and reach the package only as environment variables when it starts. The package sends them as HTTP Basic only to the saved address, and never writes them to disk or puts them in an answer.
- `needs.addresses` is `lan`: it may ask for a Yes card for an address on the home network. No internet hosts (`needs.hosts` is empty) and no brain.

**Setup steps.**
1. Enter your AdGuard Home user name (`enter_secret`, key `ADGUARD_USER`).
2. Enter your AdGuard Home password (`enter_secret`, key `ADGUARD_PASSWORD`).
3. Tell the bot where AdGuard Home is (`confirm`): say "set my AdGuard address to http://<its address>:<its port>". Only an `http` or `https` address on this computer or the home network is taken: a loopback, `10.x.x.x`, `172.16-31.x.x`, `192.168.x.x` or `169.254.x.x` address, `localhost`, or a name ending in `.local` or `.lan`. A name is looked up at every call, and every answer must be one of those addresses. If AdGuard Home is on another computer, the bot raises a Yes card for that address and port, and the owner can also allow one herself on the Bridge page.

**Data.** One file, `adguard.json`, in the package's data folder (`PUFFPAI_DATA`), holding only the address and when it was saved. Saves replace the whole file at once. A file that will not read is never written over. On remove, the Core asks whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_TOKEN` and the two keys set. It listens on 127.0.0.1 only, and every route (`/health`, `/mcp`) refuses a request without the package's token. Calls to AdGuard Home on this computer go straight to it; calls to one elsewhere on the home network go through the Bridge proxy whenever the Core has set one, with the name as saved, and the Bridge checks the name's answers when it connects. It never follows a redirect, waits at most 8 seconds, and reads at most 2 MB of any answer.

## Harvested from HomeBot

HomeBot's zone 3 (`zone3_status`, `zone3_secure_device`, `zone3_enforce` and `scripts/zone3_bootstrap_adguard.py`) ran its own AdGuard Home and proposed changes for the owner to apply on a Firewall page. Here the owner's own AdGuard Home is used as it is, and each change is a tool the Core can put behind a Yes. HomeBot's per-device modes, its own rule block and its telemetry ledger are not part of this package.

## Rough edges

- It does not install or set up AdGuard Home. Finish AdGuard Home's first-run setup in a browser first; until then this package gets a redirect and stops.
- An AdGuard Home on another computer works only after the owner's Yes for that exact address and port. A number (like 192.168.1.2) or a `.local` or `.lan` name can be allowed. A link-local address (169.254.x.x, fe80::) can be saved, but it can't be allowed.
- With no port given, the tries at 3000 and 80 can't pass the Bridge before a Yes, so it saves 3000 and asks for 3000. If yours is on another port, give the port.
- Addresses in other private ranges (100.64.x.x, IPv6 home addresses that start with fd) are refused, on purpose. So is an address with a path after it (AdGuard Home behind a web server at `/adguard`).
- An `https` address must have a certificate this computer trusts for that name. AdGuard Home's self-made certificates are refused; use its `http` address on the home network instead.
- AdGuard Home has one kind of login, with full control. There is no read-only login to give it.
- Changing rules reads the whole list and writes it back. If you edit rules in AdGuard Home's own page at the very same moment, one of the two edits can be lost.
- Rules with extras (like `$important` or `$client=`) are not read as the same rule, so an allow rule may not beat an owner's `$important` block.
- "Today" is AdGuard Home's stats period, 24 hours unless you changed it.
- Not built: editing filter lists, per-device settings (HomeBot's lockdown, balanced and always-allow modes), blocked services, and the upstream DNS list.

Cypher Sisters. puff puff pass.
