# Budget Ledger

Offline: it needs no host. Budget Ledger shows where the household's money went, from the statement files you download from your own bank's website and give it. It reads CSV, OFX, QFX and QIF files, sorts each transaction into a category, finds transfers between your own accounts and repeating payments, and adds up each month by category against the budgets you set. By Cypher Sisters, Apache-2.0.

It moves no money, signs in nowhere and never contacts a bank. It gives no financial advice. Every answer with totals ends with: "Budget Ledger shows what your statements say. It gives no financial advice and moves no money."

## PuffPAI

**What it does.** Budget Ledger is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/budget-ledger/`) and a tray item, with these tabs:
  - **Import**: pick the account (or name a new one), drop a statement file, look at the preview (how many rows are new, how many are already here, the first rows as they will be saved), then confirm. For a CSV file the preview shows which column it took for what; change any of them, tick "flip signs" for a card file that shows spending as positive, pick the date order when the dates read two ways, and read it again. The choice is remembered for that account. The list of imported files can remove everything one file brought in, after you confirm.
  - **Transactions**: search, filter by account, month and category, uncategorized first. Set a category, a note or the transfer tick on any row. After you set a category the page offers to make it a rule. Suggested transfers between your own accounts show at the top to confirm or dismiss.
  - **Month**: income, spending and what was left over, spending by category with simple bars and a line for each budget, the largest payees, the month before, and the last 12 months.
  - **Recurring**: repeating payments with how often they come, the usual amount and the cost a year.
  - **Rules** and **Categories**, **Budgets**, **Accounts** (name, kind, last four, currency) and the ledger's own currency.
  - Export everything as one JSON file, or the transactions as CSV.
  It loads nothing from the internet, takes the Desktop's colours, has square corners and works by touch.
- MCP tools for the brain, at `/mcp`. The tools that change something are in `abilities.changes`, so the owner can be asked for a Yes. `remove_transactions` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `month_summary` | Income, spending, by category against each budget, and the month before | no | how did our money go last month |
| `find_transactions` | A list by payee words, month, category, account and amount range, with the count | no | list every transaction over 200 dollars this month |
| `spending_on` | The total on one category or payee over a period | no | how much did we spend on groceries last month |
| `recurring_payments` | Repeating payments with their cost a year | no | what subscriptions and regular bills are we paying |
| `uncategorized` | Transactions with no category yet, each with a best guess | no | which transactions still need a category |
| `set_category` | One transaction by payee words and date, or every one from a payee, with an optional rule | yes | put the Corner Cafe payment on November 3 under eating out |
| `set_budget` | A monthly budget for a category, every month or one month | yes | set our groceries budget in Budget Ledger to 600 a month |
| `remove_transactions` | One transaction, or everything from one imported file | yes | delete everything imported from the file october-checking.csv |

Every tool takes plain names: an account, a category or a payee by its words, a month like "last month", "October" or "2026-10". When none or several things match, nothing changes and the answer lists the candidates. Part of a name counts only by whole words: each word said must be a word of the name or the start of one, three letters at least ("Ron" never finds "Sharon"). Searching for transactions works the same way, in the payee, the description and the note. Upper and lower case are the same and accents are folded, never dropped: "cafe" finds Café Luna, "über" never finds Berry Farm. To change or remove something, words that are all shorter than three letters pick only a name that is exactly those words. When only part of a name was said, the answer says first which one it used ("Using 'Joint checking'.").

**How a file is read, how duplicates are found, how a category is chosen, how transfers and repeating payments are found, and how totals are worked out.** See `PROCESS.md`.

**What it needs.** Nothing from the internet, no keys and no brain. Statement files you download yourself from your bank's website (look for "download", "export" or "statements"; CSV, OFX, QFX, Quicken or QIF).

**Setup steps.** None. Open the page, pick "A new account", give it a name, and drop your first statement file.

**Data.** One file, `ledger.json`, in the package's data folder (`PUFFPAI_DATA`). Amounts are kept as whole numbers of the smallest unit (cents), so there are no rounding errors. Every save replaces the whole file at once (temp file, then rename) under one lock, and an import's record and its transactions are saved in the same write. A statement file itself is never kept: only its name and its sha256. It keeps only the last four digits of any account or card number it finds in your files: in the file's name, in each description, payee and memo, and the bank's own id for a transaction is kept only as a keyed fingerprint. What you type (an account's name, a category, a note, a rule) is masked the same way, and every answer and export is masked once more on the way out. Only a file's own name is kept, never its folder, and no answer shows a folder. A ledger saved by an older version is cleaned the same way the first time it is opened. The JSON export leaves out the fingerprint key. A file that will not read, or an empty one, is never written over; the page and the bot say so and which file. Import only ever adds. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/view`, `/updated`, `/transactions`, `/month`, `/recurring`, `/export`, `/export.csv`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). A statement file is at most 10 MB.

**Files.** `puffpai.json`, `server.py`, `web/index.html`, `icons/ledger.svg`, `PROCESS.md`, `NOTICE.md`, `tests/test_server.py`.

**Licence.** Apache-2.0. See `NOTICE.md` for what it was built from.

## Rough edges

- Bank files differ. Some CSV files will need the columns picked by hand the first time; the choice is then remembered for that account.
- No bank sign-in and no automatic download, on purpose. You download each statement yourself.
- Not encrypted by this app. The ledger is only as private as this computer's folder.
- One household per bot.
- No investment, stock or pension tracking: bank and card accounts and cash only.
- No tax advice and no financial advice.
- A statement in a file with more than one account is refused; download one account at a time.
- An uncategorized amount that comes in (a refund, say) counts as income until you give it a category.
- Card and account numbers keep only their last four digits. Digits written unbroken, or as groups joined by one or several of these, in any mix: any space (tabs, no-break and thin spaces too), line or paragraph separator or control character; any invisible format character (zero-width characters, direction marks, soft hyphen, byte order mark, tags); any combining or spacing mark; any unassigned or private-use code point; any dash or minus sign; middle dots and bullets; the blank-looking letters U+3164, U+115F, U+1160, U+FFA0 and U+2800; and `- . / _ * + # : ~ | ; ' = \ &` in their plain and full-width forms, and a comma directly between two digits (`1,234.56`, `4111,1111`), are one run; a decimal point between digits joins too. If a run has 12 or more digits in all, amounts and cents included, all of it is hidden under one label with its last four digits. Any unbroken run of 8 or more digits keeps its last four too. Only a zero-padded date with a four-digit year (`2026-11-15`, `15.11.2026`, `11/15/2026`) ends a run. A `+` before the digits changes nothing. Masked on purpose, though ordinary: any free text where numbers side by side add up to 12 or more digits, such as `Acct 123456789012 1,234.56`, `Due 1,234.56 2,345.67`, `ref 1234567 329.74`, two phone numbers side by side, a time beside a reference, or an international phone number of 12 or more digits (`+44 20 7946 0958`). Only free text changes: a transaction's amount, date and account are never changed by this, and totals stay the same. A run of more than 4 digits in a file's name keeps only its last four too, so a date in a name is cut.
- It cannot recognise every number. Check a file's text before you import it if that matters to you. Not caught: digits split by letters (`4111x1111x1111x1111`) or by any character not in that list: brackets `( ) [ ] { } < >` (`4111(1111)1111(1111)`), `@ ! ? $ % ^ "` and the backquote, and any other punctuation or symbol (a comma joins only when it sits directly between two digits, so `4111, 1111` is two numbers); letters or symbols that only look like digits (O for 0, l for 1, Roman numerals, circled numbers from 10 up); a number written in words; a number of fewer than 12 digits that has a join inside it (an unbroken run of 8 or more is cut); a number with a zero-padded date with a four-digit year from 1970 to 2069 inside it or at its edge (`Acct 4124 2026-11-15 5678 9012`); a number split across columns other than the description (or payee or name) and its memo; and a category an older file saved without its accented letter, which is found only when typed with the accent, until renamed.
- There is no restore from the JSON export yet; it is a copy for you to keep.

Cypher Sisters. puff puff pass.
