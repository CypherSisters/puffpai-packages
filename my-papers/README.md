# My Papers

Household paperwork filing that stays on this computer: add a paper, find it again, and see what is coming due. Each paper keeps its type, who sent it, the date on it, the people it is about, tags, an amount, a due date, an expiry date, how often it renews, how long to keep it, and where the paper copy is. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** My Papers is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/my-papers/`) and a tray item. The page has Coming up (due, expiring, renewing or to follow up in the next 60 days, and anything overdue, soonest first), Find (words plus type, person, tag and year, with the matching line shown), By type, Add a paper (a file is optional: a paper can be just a record of where the paper copy is), each paper's own view (its fields, the text found in its file, a download of the file, Edit and Delete), and Export and import. It is sized for a tablet and works by touch;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes. `remove_paper` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `find_papers` | Find papers by words and by type, person, tag or year; gives title, type, date and the matching line | no | where is the car title |
| `read_paper` | One paper's fields and up to 4000 characters of its found text, to answer and quote | no | what does the lease say about pets |
| `coming_up` | Due dates, expiry dates, renewals and follow-ups in the next days (60 unless asked), and anything overdue | no | what bills are due soon |
| `add_paper_record` | Add a paper without a file, like where a paper copy is kept | yes | the car title is in the blue binder |
| `change_paper` | Change a paper's fields, like setting or clearing a due date | yes | set the City Water bill due date to 2027-04-15 |
| `list_types_and_counts` | How many papers of each type, and how many files had no text found | no | how many papers do I have of each kind |
| `keep_or_shred` | Papers past their keep-until date. A list only; it deletes nothing | no | which papers can I shred now |
| `remove_paper` | Delete a paper, and its stored file when no other paper uses it | yes | delete the paper called gym contract |

**How it works things out.** See `PROCESS.md`: how text is found in a file, how a search matches, how "coming up" is worked out (renewals too), how keep-until is suggested for tax papers, and how import decides to skip. Where the ideas came from: `NOTICE.md`.

**People.** The people a paper is about come from the Core's shared people list (`GET /v1/people` with the package's token, on this computer only, all within 2 seconds, never following a redirect; the answer is kept 60 seconds, a failure 30) when the Core answers. When it does not, the page offers the names already used on papers, and you can type new ones.

**What it needs.** Nothing from the internet, no keys and no brain. It works with no internet.

**Setup steps.** None.

**Data.** In the package's data folder (`PUFFPAI_DATA`): `papers/<id>.json`, one file per paper (its fields and the text found in its file); `files/<sha256>`, the stored files, each kept once however many papers use it; and `index.sqlite`, a search index that is built again at every start and may be deleted. Every save replaces a file at once (temp file, then rename). Each paper file carries the marker `app: "my-papers"`. A paper file that will not read, is empty, or is not this app's (another marker, or other keys) is never written over; the page and `list_types_and_counts` say which. A paper file from before the marker, with exactly this version's keys, still reads and gets the marker on its next save. A request body over its cap is read and dropped (up to about 200 MB within 20 seconds), then refused; a body that does not all arrive in time (5 seconds, and 1 more per 8 MB for an upload) is refused. `index.sqlite` is not checked this way: it holds nothing of its own and is built again at every start. Deleting a paper deletes its stored file when no other paper uses it. Export gives everything as one zip; import takes that zip back, or a zip of files with optional `name.ext.json` files beside them, and never writes over a paper. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/info`, `/papers`, `/paper/<id>`, `/coming`, `/file/<id>`, `/export`, `/updated`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/my-papers/`, which adds the token. A stored file is always sent as a download (`application/octet-stream`, `Content-Disposition: attachment`), never shown as a page. Each file is at most 20 MB. The page takes its colours and fonts from the Desktop's look when the Desktop gives them, and has plain ones of its own otherwise.

## Rough edges

- Scans and photos are stored but not read: there is no OCR in this version. Add notes to find them.
- PDF text is best effort, with the standard library only. Many PDFs give little or no text.
- Word, Excel and other office files are stored but not read.
- No watch folder: files come in through the page (or an import zip) only.
- No email import.
- The files are not encrypted by this app. They sit in its data folder as they came.
- The keeping hint for tax papers is for the United States, and is a hint only.
- A bill stays overdue until you clear or change its due date; there is no "paid" mark yet.
- Days follow this computer's clock and date.

Cypher Sisters. puff puff pass.
