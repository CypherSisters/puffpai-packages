# How Budget Ledger reads statements and works out its totals

Every number used below is in one table, `NUMBERS`, at the top of `server.py`, with `CADENCES` for repeating payments.

## How a file is read

1. A file is at most 10 MB. An empty file, or one that is not text, is refused plainly.
2. The kind comes from the file's name and its first lines: OFX or QFX (it has an `<OFX>` part), QIF (it starts with `!Type`, `!Account` or `!Option`), otherwise CSV.
3. Text is read as UTF-8, or UTF-16 when the file says so, or else as Windows Latin.
4. **OFX and QFX.** Our own small tag reader takes the SGML form (no closing tags) and the XML form alike. It reads each `STMTTRN` block: `DTPOSTED` (or `DTUSER`), `TRNAMT`, `FITID`, `NAME` and `MEMO`, and the account block: `ACCTID`, `ACCTTYPE`, `CURDEF`. It is not an XML parser, so no entity a file declares is ever expanded, and deep nesting does not matter. Of `ACCTID`, only the last four characters are kept. A file with more than one account is refused.
5. **QIF.** Each record is the lines from one `^` to the next: `D` date, `T` or `U` amount, `P` payee, `M` memo, `L` category. `!Type:` gives the account kind.
6. **CSV.** The separator (comma, semicolon, tab or bar) is the one that gives the same number of cells on the most lines. The header line is the first of the first 20 lines with a date column and an amount column (or money out and money in columns) among its words. Columns are found by their header words: date (date, transaction date, posted, posting date, ...), description (description, payee, name, details, ...), memo, amount, money out (debit, withdrawal, ...), money in (credit, deposit, ...), balance, category, and a debit or credit word column. When the header is not found, or a column is missing, or more than 10 in 100 rows do not read, the page shows the app's guess and the owner confirms or picks the columns. The confirmed choice is remembered for that account and used again when the header line is the same.
7. **Dates.** Every date in the column is tried as year-month-day, month/day/year and day/month/year. The order that reads every date is used. When two orders read every date but give different days, the owner is asked. Month names (15 Oct 2026, Oct 18 2026) read the same in any order.
8. **Amounts.** Currency signs and codes, spaces and thousands separators are dropped. A minus at the front or the end, parentheses, or `DR` at the end mean money out; `CR` at the end means money in. Whether the decimal mark is a point or a comma is decided for the whole column. An amount becomes a whole number of the currency's smallest unit (2 decimal places for most, 0 for yen, 3 for some). With money out and money in columns, the amount is money in minus money out. "Flip signs" turns every sign around, for card files that show spending as positive.
9. Each field keeps at most 1,000 characters. In every text taken from a file (description, payee, memo, category, the column names and the rows the preview shows), a card or account number keeps only its last four digits, written `[...1234]`. The rule is plain, with no special cases for amounts. A run is digits written unbroken, or as groups joined by one or several of these, in any mix: any space (tabs, no-break and thin spaces too), line or paragraph separator or control character; any invisible format character (zero-width characters, direction marks, soft hyphen, byte order mark, tags); any combining or spacing mark; any unassigned or private-use code point; any dash or minus sign; middle dots and bullets; the blank-looking letters U+3164, U+115F, U+1160, U+FFA0 and U+2800; and `- . / _ * + # : ~ | ; ' = \ &` in their plain and full-width forms, and a comma directly between two digits (`1,234.56`, `4111,1111`). A decimal point between digits joins like any dot. The joins are decided by Unicode category, not by a hand list. Invisible format characters and lone surrogates are also dropped from every text before it is kept. All the digits of a run are counted, amounts and cents included; if there are 12 or more, the whole run is hidden under one label with its last four digits (`4111 - 1111 - 1111 - 1111` and `Card 5555555 555,554,444.00` become `[...1111]` and `Card [...4400]`). A run with fewer than 12 digits is left as it is, except that any unbroken run of 8 or more digits keeps only its last four. Letters may touch a run (`card4111111111111111x`); full-width digits and other scripts' digits count as digits. A description and its memo are read as one text, so a number split between the two is found.

   Only one thing ends a run: a zero-padded date with a four-digit year from 1970 to 2069, written `2026-11-15`, `15.11.2026` or `11/15/2026`. Each part on either side of it is counted on its own. A `+` before the digits changes nothing. So one amount, one date and one amount, one phone number (`+1 555 123 4567` too), and a short reference with an amount under 12 digits in all stay as written.

   **Masked on purpose, though ordinary** (over-masking, accepted to keep the promise): any free text where numbers side by side add up to 12 or more digits. For example `Acct 123456789012 1,234.56` becomes `Acct [...3456]`, `Due 1,234.56 2,345.67` becomes `Due [...4567]`, `ref 1234567 329.74` becomes `ref [...2974]`, two phone numbers `555-123-4567 / 555-987-6543` become `[...6543]`, a time beside a reference `2026-10-01 10:30:45 #123456` becomes `2026-10-01 [...3456]`, an international phone number of 12 or more digits `+44 20 7946 0958` becomes `+[...0958]`, and a list such as `10,20,30,40,50,60` becomes `[...5060]`. A word between the numbers keeps them apart. This only ever changes free text: a transaction's amount, date and account are read from their own columns and are never changed by it, so every total stays the same.
10. **The file's name.** Only the file's own name is kept, never a folder. Any run of more than 4 digits in the name, joined by the same separators or not, keeps only its last four: `acct_4111111111111111.ofx` is kept and shown as `acct_[...1111].ofx`. A date in a name is cut the same way. Brackets do not join, so `report 2026 (1).csv` stays as it is.
11. A row whose date or amount does not read is left out, and the preview and the answer say how many.
12. **What you type** is masked the same way before it is kept or said back: an account's name, a category, a note, a rule's words and the words you search with. Every answer, the page's data and both exports are masked once more on the way out. A ledger saved by an older version (format 1, 2 or 3) is masked again the first time it is opened, and invisible characters are dropped from it; its transactions keep pointing at their account. Older versions kept categories and remembered column names only as lower-case words with every letter outside a to z dropped (`Café Treats` was kept as `caf treats`), and no other copy of the name was kept, so the lost letter can not be put back. Such a category is still found when you type it with the accent, and such remembered columns are still used (and kept the new way at the next import); typed without the accent (`cafe treats`) it is not found. Rename the category once to fix it.
13. **What is not caught.** Digits split by letters (`4111x1111x1111x1111`) or by any character not in that list: brackets `( ) [ ] { } < >` (`4111(1111)1111(1111)`), `@ ! ? $ % ^ "` and the backquote, and any other punctuation or symbol (a comma joins only when it sits directly between two digits, so `4111, 1111` is two numbers); letters or symbols that only look like digits (O for 0, l for 1, Roman numerals, circled numbers from 10 up); a number written in words; a number of fewer than 12 digits that has a join inside it (an unbroken run of 8 or more is cut); a number with a zero-padded date with a four-digit year from 1970 to 2069 inside it or at its edge (`Acct 4124 2026-11-15 5678 9012`); a number split across columns other than the description (or payee or name) and its memo; and a category an older file saved without its accented letter, which is found only when typed with the accent, until renamed.

## How words and names are matched

1. A word is letters and digits of any script (and `&` or `'`); anything else splits words. Upper and lower case are the same, and accents are folded, never dropped: "cafe" and "café" both find Café Luna, and "über" never finds Berry Farm. A word in another script matches only itself.
2. Searching (`find_transactions`, the page's search box): each word must be a whole word of the payee, the description or the note, or the start of one when it has 3 letters or more. Never letters inside a word: "ron" finds Ron's Hardware, not Sharon Florist.
3. The same for an account, a category, a rule or an imported file named in part. For anything that changes or removes (`set_category`, `remove_transactions`, a category for `set_budget`), words that are all shorter than 3 letters select only a name that is exactly those words.
4. An answer that says nothing matched shows the words as you typed them, with any card number masked.

## How duplicates are found

1. A row is already here when its bank id (`FITID`) is already in that account. The bank id often holds the account number, so it is never kept: the ledger keeps a fingerprint of it instead (HMAC-SHA256 with a random key made once and kept in `ledger.json`, cut to 16 hex characters), and compares fingerprints.
2. Otherwise it is already here when the account, the date, the amount and the description all match a saved transaction.
3. Rows that are exactly the same inside one file (two coffees the same day) each count once: a file adds as many as it has beyond those already saved.
4. So importing the same file twice adds nothing, and the answer says "Nothing new" and when that file was imported before. An import never changes or removes what is there.
5. The import record (the file's name and sha256) and its transactions are saved in one write. The file itself is not kept.

## How a category is chosen

1. The payee is cleaned from the name or description: card terminal words, card and reference numbers and dates are dropped, and capitals are evened out (`SQ *CORNER CAFE 4455` becomes `Corner Cafe`).
2. The owner's rules come first: a rule says "if the payee has these words, use this category". When several rules match, the one with the longest words wins.
3. Then the bank's own category, when the file has one and it names one of ours.
4. Then a small list of built-in words for obvious cases, like payroll or salary for income (money in only) and grocery or supermarket for groceries (money out only).
5. Otherwise the transaction stays uncategorized. The best guess shown for it is the category the same payee has most often, then the built-in words.
6. When the owner sets a category, the page offers to make it a rule; the `set_category` tool can do the same with `make_rule`.

## How transfers are found

1. A pair is suggested when two transactions are in different accounts, in the same currency, of the same size with opposite signs, and at most 3 days apart. The closest dates pair first.
2. Nothing is marked on its own. The owner confirms each pair, or says it is not a transfer, and then it is not suggested again.
3. A confirmed transfer, or anything the owner puts in the category "transfers", is left out of income and spending.

## How repeating payments are found

1. Money-out transactions are grouped by payee and currency.
2. The middle gap between their dates picks a pattern: weekly (6 to 8 days), every two weeks (13 to 15), monthly (26 to 35), every three months (85 to 97) or yearly (355 to 375).
3. It counts when it was seen at least 3 times (2 for yearly), at least 75 in 100 gaps fit the pattern, and at least 75 in 100 amounts are within 20 in 100 of the middle amount.
4. The cost a year is the middle amount times 52, 26, 12, 4 or 1.
5. One not seen for more than two of its gaps since the latest transaction is shown as "may have stopped" and left out of the yearly total.

## How totals are worked out

1. All sums are whole numbers of the smallest unit, so there is no rounding.
2. Each currency is added up on its own. Amounts in different currencies are never added together.
3. Income is everything in the category "income", plus uncategorized money in.
4. Spending is everything else that went out, less refunds in the same category. Transfers are left out of both.
5. Spending by category is the money out less the money in for that category. Uncategorized money out shows as "uncategorized".
6. A budget is set per category for every month, or for one month only; one month's budget wins over the every-month one. Left is the budget less what was spent; over is what was spent beyond the budget.
7. The month before is worked out the same way, and the change is the difference.
8. The largest payees are those with the most money out in the month.
9. Days follow this computer's clock and date. "Last month" is the calendar month before this one.
