# How My Papers finds text, works out what is coming up, suggests keeping dates and skips on import

## Finding text in a file

1. The file is stored as it is, under `files/` in the data folder, named by its sha256. The same file added twice is stored once. The file's own name is kept only for showing and for the download; it is never used as a path.
2. The kind of file is read from the end of its name.
3. For `.txt`, `.md`, `.csv` and `.json`, the text is the file itself, read as UTF-8 (letters that will not read become a mark).
4. For `.html` and `.htm`, scripts, styles and comments are dropped, the tags are taken out, and entities like `&amp;` are turned back into letters. This is one pass over the page, so a page full of unclosed tags is read as quickly as any other.
5. For `.pdf`, the app looks for each stream in the file. A stream marked FlateDecode is inflated with zlib; a stream with no filter is read as it is; streams with other filters, pictures, fonts and metadata are passed over. In each stream that holds text, the strings shown by the Tj, TJ, ' and " operators are read, with a new line where the text moves down. At most 40 MB is inflated per file. Many PDFs give little or nothing: scans, and PDFs whose fonts map letters in their own way.
6. Any other kind of file (pictures, Word and Excel files and the rest) is stored but not read.
7. Spaces and blank lines are tidied and at most 200,000 characters are kept. When fewer than 3 letters or digits are found, the paper says "No text found (a scan or a picture). Add notes so you can find it."

## Finding papers

1. The words you search for are split into words. A paper matches when it holds every word, in its title, sender, tags, notes or found text. A word also matches the start of a longer word (feb finds February).
2. When this Python has SQLite FTS5, the search uses it and puts the best matches first. When it does not, every paper is checked word by word, newest first. The page says which one is in use.
3. Type, person, tag and year then narrow the list. The year is the year of the date on the paper, or the year it was added when it has no date.
4. The matching line shown is the first line of the notes, the found text, the sender, the tags or the title that holds one of the words.

## What is coming up

1. Today is this computer's date.
2. Each paper can have a due date, an expiry date and a follow-up date. Each one that falls between any day before today and the given number of days ahead (60 unless asked) is listed.
3. A date before today is overdue and stays on the list until you change or clear it (for example after paying a bill, clear its due date or mark it with a new one).
4. A paper that renews every N months renews on the date on the paper plus N months, plus 2N months, and so on. (With no date on the paper, the date it was added is used.) Only the next renewal on or after today is listed; a renewal is never overdue.
5. When the next renewal falls on the same day as that paper's due, expiry or follow-up date, it is listed once.
6. The list is sorted by date, soonest first, then by title.

## Keep until

1. Any paper can have a keep-until date that you choose.
2. When a paper's type is tax, it has a date on the paper, and you give no keep-until date, the app suggests the date on the paper plus 3 years (a 29 February becomes 28 February) and says "Most tax records: keep 3 years. Some need longer." This is a hint only, from the United States IRS guidance; change the date when your paper needs longer.
3. Keep or shred lists the papers whose keep-until date is before today. It deletes nothing.

## Import

1. A zip is refused as a whole when it holds a name that starts with a slash or a drive letter, or has a `..` part; when it holds more than 2000 entries; or when its files would unpack to more than 1 GB. The zip itself is at most 200 MB, and each file in it at most 20 MB.
2. A zip that holds `my-papers-export.json` is a My Papers export. Each `papers/<id>.json` in it is one paper. A paper is skipped when a paper with that id is already here, or when its file is named but missing from the zip or does not match its sha256. Its fields are checked as if it were typed in, and its text is found again from its file.
3. Any other zip is a zip of files. A file `name.ext.json` next to `name.ext` is read as that file's fields (title, type, from, date, people, tags, amount, currency, due, expires, renews_every_months, keep_until, follow_up, binder, notes; other keys are passed over). A file is skipped when a paper here already holds a file with the same sha256. Folders, and files whose names start with a dot, are passed over.
4. Import never writes over a paper. It reports how many papers it imported and how many it skipped, with a short reason for each skip that was not simply already here.

## Finding a paper by name

1. Its id, then its exact title (case and spaces ignored).
2. Otherwise the one title where every word you gave is a whole word of the title or the start of one, three letters at least. Letters inside a word never match: "Ron" does not find "Letter from Sharon Lee". The answer then starts with "Using '<title>'."
3. When several match, nothing changes and the answer lists them. When none match, nothing changes.
