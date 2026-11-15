# Reviewer

Offline. An agent that reads something you made and comments on it. You paste a letter, an essay, a plan, a how-to or a block of code; the bot's own brain reads it and fills a fixed report. Reviewer never changes your text. It keeps its review reports, which quote short passages of what you pasted so the comments make sense. It does not keep the whole text. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Reviewer is a PuffPAI agent package (`kind: agent`). Its scope is `review.read` and `review.report`. It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/reviewer/`) and a tray item that show the saved reports. It loads nothing from the internet;
- six MCP tools at `/mcp`. The tools that change something are in `abilities.changes`; `remove_review` also says it deletes (`destructiveHint`). No tool edits your text:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `review_text` | Reads pasted text as its kind, for its reader, and saves a report | yes (saves the report) | review this cover letter for a hiring manager: Dear Sam, ... |
| `list_reviews` | The saved reports, newest first | no | list my saved reviews |
| `read_review` | One report again, by its title or latest | no | read me the review of my cover letter again |
| `compare_versions` | What changed between two pasted versions, and whether the must-fix problems were fixed (a verdict counts only when the brain says FIXED, PARTLY or NOT_FIXED plainly) | no | compare these two versions of my note and tell me what changed. Old: ... New: ... |
| `review_checklist` | The checklist a kind of text is judged by | no | what does the reviewer check in a plan or proposal |
| `remove_review` | Deletes one saved report | yes | remove my saved review of the cover letter |

**The report.** Its shape is fixed in code: What it is; Works well (three at most); Problems, each with the quoted words, why, and a fix in words, ranked must fix, should fix, could fix; Questions for the writer; Not checked (always "Facts were not verified."; for code also "The code was read, never run."; for a long text that it was read in parts). Code checks every quote against your text and marks one that is not there.

**The kinds.** General writing, a formal letter, a plan or proposal (is the ask clear, what is missing, risks, costs, dates), instructions or a how-to (could a first-time reader follow it), and code (bugs, unclear names, missing error handling, security smells). Each kind has its own short checklist; `review_checklist` shows it. A kind said in other words is read as the nearest kind, and the answer says how it was read.

**What code does and what the brain does.** Code splits a long text at paragraph breaks into parts of at most 5,000 characters, makes the line diff when you compare versions (Python's own `difflib`), ranks and caps the problems, and saves the report. The brain reads each part, then one more pass merges the notes from the parts, and when comparing it says whether each must-fix problem was fixed. Your text goes to the brain marked as material, not instructions. Its answer is read as text and never run. When the brain does not answer within 120 seconds, Reviewer says "The brain did not answer. Nothing was changed." and saves nothing. Step by step: `PROCESS.md`.

**What it needs.** The bot's own brain (text only), reached through the Core on this computer (`POST /v1/brain/chat` with this package's token). No hosts, no keys, no internet.

**Setup steps.** None.

**Data.** One file, `reviews.json`, in the package's data folder (`PUFFPAI_DATA`). It keeps its review reports, which quote short passages of what you pasted so the comments make sense. It does not keep the whole text. Each quote is at most 200 characters, and one report holds at most 2,000 characters of quoted text (the brain's notes kept as it wrote them count too); the rest is cut with "...". A run of 40 characters or more copied from your text into the brain's other comments is cut to "...". The file also holds the text's length and a fingerprint of it (so a later compare can find its review). A report's title is the one you give, or "Review of a" the kind and the date and time, never words from your text. Every save replaces the whole file at once (temp file, then rename). A file that will not read, or an empty one, is never written over. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} agent.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/view`, `/updated`, `/health` and `/mcp`) refuses a request without the package's token. The only place it dials is `PUFFPAI_API`, only when that is an http address on this computer (127.0.0.1, localhost or ::1), directly and never through a proxy, and it never follows a redirect.

**Licence.** Apache-2.0. See `NOTICE.md`.

## Rough edges

- On this platform today an agent's tools are picked like any other tool's: the bot chooses a tool from your words. Say "review this" with the text.
- The brain's comments can be wrong, can miss real problems, and can quote words that are not in your text (those are marked). It checks no facts.
- A small model takes time to answer. A short text takes about a minute; a long one is read in parts, one brain call each plus one to merge, and can take several minutes. The Core waits about a minute for a tool's answer, so Reviewer answers after 45 seconds with "still reading" and keeps going; ask for the review by its title a little later. A review that is still being read is lost if Reviewer stops.
- One review at a time.
- Texts up to 40,000 characters.
- Not encrypted by this app.

Cypher Sisters. puff puff pass.
