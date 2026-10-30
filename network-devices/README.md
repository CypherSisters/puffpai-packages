# Network devices

A list of the devices on your home network that this computer has talked to, with names you choose. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Network devices is a PuffPAI tool package (`kind: tool`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot MCP tools at `/mcp`. It reads this computer's own neighbour table, the list every computer keeps of the nearby devices it has exchanged traffic with. The tools that write its device list are in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `scan` | Read the neighbour table, look up names, guess makers, and save the list | yes (writes the device list only, nothing on the network) | scan my home network for devices |
| `list_devices` | The saved list: address, hardware address, name, maker guess, first seen, last seen, your label; optional filter word | no | what devices are on my network |
| `new_devices` | Scan, then list the devices first seen since you last asked, and remember that you asked | yes (writes the device list) | any new devices on my network since I last asked |
| `name_device` | Give a device your own label | yes | call the device at 192.168.1.23 the kitchen tablet |
| `forget_device` | Take a device off the list | yes | forget the device called old printer |

How it reads, per system: `arp -a` on Windows, `arp -an` on a Mac, `ip neigh show` then `arp -an` on Linux. Each runs from the system's own folders, as an argument list (never through a shell), with a 10 second limit and no console window. The `-n` on a Mac and Linux stops `arp` from looking up names itself, which can take minutes; the package looks them up instead, a few at a time, for at most about 6 seconds in all, and only for home-network (private) addresses. The maker guess comes from a small table of about 70 common home prefixes built into `server.py` (Apple, Samsung, Amazon, Google, Sonos, Raspberry Pi, Espressif, TP-Link, Philips Hue, Roku, Nintendo, Sony, Microsoft, Intel and others). A phone or laptop that hides its real hardware address shows as "private address".

**What it needs.** Nothing from the internet, no keys, no brain. The `arp` program comes with Windows and macOS; on Linux, `ip` or `arp` (one of them is almost always there). If neither is found, the tools say so plainly.

**Setup steps.** None.

**Data.** One file, `devices.json`, in the package's data folder (`PUFFPAI_DATA`): each device by hardware address, with its last address, name, maker guess, your label, and first and last seen times (UTC), plus the time of the last scan and the last time you asked for new devices. Every save replaces the whole file at once. A file that will not read is never written over. At most 1,000 devices are kept. On remove, the Core asks whether to keep it.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only, and every route (`/health`, `/mcp`) refuses a request without the package's token (`Authorization: Bearer <token>` or `X-PuffPAI-Key`).

## Harvested from HomeBot

HomeBot's `bridge_list_devices`, `bridge_status` and `privacy_scan_network` combined the neighbour table with mDNS, Home Assistant and AdGuard, and `bridge_device_telemetry` showed what each device looked up. This package keeps only the part the standard library can do on this computer alone. What each device talks to is the AdGuard Home package's job (`query_log` with a client filter).

## Rough edges

- It only sees devices this computer has talked to recently. A device that has been quiet, or that is on another part of the network (a guest Wi-Fi, a mesh node this computer never reached), is missing until the two exchange traffic.
- It is not a network monitor. It does not watch the network, scan it, ping every address or check ports, and it does not run on its own: the list changes only when you ask it to scan.
- Blocking a device is not a thing it does. That is the router's job.
- The maker table is small, so many devices show "unknown". A smart plug often shows the maker of its Wi-Fi chip (Espressif), not the brand on the box.
- Names come from your home network's usual name lookup, so most devices have none until your router hands them out. The lookup goes to whatever name server this computer uses; if that is not your router, the question about a home address may leave the house (it carries only the address).
- A device that changes its hardware address (many phones do, per network or per day) can show up as a new device.
- `new_devices` counts as a change because it remembers when you last asked.

Cypher Sisters. puff puff pass.
