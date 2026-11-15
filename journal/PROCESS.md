# How Journal counts a streak, picks "on this day" entries, and decides what to skip on import

## The writing streak

1. Each entry has a created date and time, in this computer's local time. Only the date part counts for the streak.
2. Journal makes a list of every date that has at least one entry. Two entries on one day still count as one day.
3. The streak starts at today if today has an entry. If today has no entry yet, it starts at yesterday, so the streak is not lost before the day is over.
4. From that day, Journal steps back one day at a time and counts each day that has an entry. It stops at the first day with no entry.
5. The count is the current streak. If neither today nor yesterday has an entry, the streak is 0.
6. The longest streak is the longest run of dates one after another anywhere in the journal.
7. An entry written about an earlier day (with a day given) counts for that day, so filling in a missed day joins the streak back up.

## On this day

1. Journal takes today's month and day (or the day you ask about).
2. It picks every entry whose created date has the same month and day and a year before this one. Entries from this year are never shown.
3. An entry from February 29 shows on February 28 in a year that has no February 29.
4. The entries are shown newest first: last year, then the year before, and so on.

## Import

1. Journal reads one JSON file: either an object with a list called `entries`, or a plain list.
2. For each item it decides the shape by its fields:
   - an item with `creationDate` or `text` is read as Day One (text, creationDate, tags, starred);
   - an item with `date` and a `body` or `title` is read as jrnl (title, body, date, time, tags, starred);
   - an item with `created` and `body` is read as this Journal's own export (all of its fields).
3. An item is skipped as "could not be read" when it is not one of those shapes, when its text is not words, when its date does not read as a date (or is before 1900 or after 2200), when it has no text and no title, or when its text is over 100,000 characters.
4. Day One dates with a time zone (like the Z for UTC) are changed to this computer's local time. jrnl dates and times are taken as local time. A jrnl entry without a time gets midnight.
5. An item is skipped as "already in the journal" when an entry with the same id is already here (own export), or when an entry with the same date, hour and minute and the very same text is already here. This is what makes importing the same file twice safe.
6. Every other item becomes a new entry with its own new file. An import never changes or writes over an entry that is already here.
7. Tags from the file are kept (a leading @ or # is dropped and they are made lower case), and any #words in the text are added as tags.
8. At the end Journal says how many it imported, how many were already there, and how many could not be read.

## Finding an entry by name

1. `latest` (or last, newest) is the newest entry. An id is that entry.
2. A date (today, yesterday, or like 2027-03-31) is every entry written that day; a change or delete needs exactly one.
3. Otherwise the exact title, with case and spaces ignored.
4. Otherwise the one title where every word you gave is a whole word of the title or the start of one, three letters at least. Letters inside a word never match: "haro" does not find "Lunch with Sharon". The answer then starts with "Using '<title>' (<date>)."
5. When several match, nothing changes and the answer lists them. When none match, nothing changes and the answer names the newest entries.
