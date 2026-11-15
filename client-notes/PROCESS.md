# How Client Notes gives codes, works out who is due, removes and imports

Client Notes stores what you type. Every rule here is plain code in `server.py`; none is the bot's choice.

## Codes

1. A new client gets the next code after the highest C-number in use: C-0001, C-0002, and so on. A code already taken is skipped.
2. You may type your own code: letters, numbers and dashes, up to 20 characters, starting with a letter or number. It is stored in capitals and must not be taken.

## Finding a client

These are private records, so this is stricter than a search. Every tool that takes a client uses it.

1. The exact code (any case, "code" before it allowed).
2. Otherwise the exact full name, ignoring case and extra spaces.
3. Otherwise every word asked must be a whole word of the name, or the start of one with at least 3 letters, and exactly one client must fit. A match is never inside a word: "Ron" is never Sharon, "jor" is never Major, and "Al" (2 letters) matches no one.
4. When step 3 found the client, the answer begins with the line "Showing <full name> (<code>)." so you see who was matched.
5. More than one client: the answer lists each one's name and code and shows nothing else. None: the answer says so and shows nothing. Nothing changes in either case.

## Next contact and who is due

1. A client's next contact is the date on their newest note that has one.
2. Due: a client who is not closed, whose next contact is today, in the past, or in the next 7 days (or the number of days asked).
3. Not seen: an active client whose newest note is 30 days old or more (or the number of days asked). A client with no notes counts from their start date.
4. "This week" on the page runs Monday to Sunday.
5. Days follow this computer's clock and date.

## Notes and actions

1. A note's date can not be later than today; a planned date goes in next contact.
2. The kind is session, call, email, visit or other. Everyday words are read ("phone call" is call, "appointment" is session, "home visit" is visit). Words that name no kind are stored as other, and the answer says so.
3. An action written as "Alex: send the worksheet by 2027-03-31" is who (Alex), what (send the worksheet) and by (2027-03-31).
4. Open actions are listed soonest due first; those with no date come last.
5. A private note's words, and its actions' words, are never put into the bot's answers.

## Removing

1. One note: by its date, its date and time, "latest", or words from it. None or several: nothing is removed and the answer lists the notes.
2. A client with no notes: removed when asked.
3. A client with notes: removed, with every note, only when `confirm` is that client's exact code. Spaces before or after it are ignored; otherwise it must match letter for letter, in capitals. Anything else removes nothing.
4. On the page: offer to export the record first; ask to confirm; then ask the owner to type the code. The server checks the code again.
5. The file is read before anything is removed.

## Export and import

1. Export gives one client or all, as JSON (this app's format) or as a readable text file. Both start with the line that the notes are not encrypted.
2. Import reads this app's JSON. Every client and note is checked first; if anything does not fit, nothing changes.
3. A client already here (the same id) is skipped with its notes. A client whose code is taken by another client gets the next free code, and the answer says which.
4. A note already here (the same id) is skipped. Nothing is ever overwritten.
