# How Chore Board decides what is due, and how a rotation moves

1. Every chore has one next due date. When you add a chore it is the date you give, or today.
2. Today is this computer's date. A chore is due today when its next due date is today or earlier. A date before today means it is late, and the board says by how many days.
3. A chore is due this week when its next due date falls between today and 6 days from now. Late chores show under Today, not under This week.
4. A chore is on one person, on a rotation (several people in a set order), or on nobody yet.
5. For a rotation, the board keeps whose turn it is. The first person in the order has the first turn.
6. Marking a chore done records who did it and on which day. By default that is the person whose turn it is. You can name someone else, and a chore on nobody needs a name.
7. After it is marked done, a rotation moves to the next person in the order, and after the last person it goes back to the first. It moves from the person whose turn it was, even when someone else did it.
8. A repeating chore's next due date becomes the day it was done plus its gap: 1 day for daily, 7 for weekly, N for every N days. Done early or late, the count starts from the day it was done. Marked done again for a day earlier than one already recorded, the next due date never moves back.
9. A one-time chore is finished once it is done. It stays in All chores as done until you remove it, or give it a new due date or a repeat.
10. Changing who a chore is on starts its rotation again at the first person.
11. Removing a person takes them off every chore. In a rotation, if it was their turn the turn passes to the next person; otherwise it stays with whoever had it. A chore left with nobody says so.
12. The tally counts the done records whose day is in the last 7 days (today and the 6 before) and the last 30 days, per person. Removing a chore or a person does not change what is already counted.
13. The people are the board's own list merged with the Core's shared people list when the Core gives one: a Core person with the same name as one of the board's people is that person. The Core's list is asked at most once a minute (once every 30 seconds after a failure), within 2 seconds, and never while a change is being saved.
14. A chore or a person is found by name: first the exact name (case and spaces ignored), then the one whose name has every word you gave as a whole word or the start of a word (three letters at least). Letters inside a word never match: "ash" does not find "Wash dishes". When the match was not exact, the answer starts by saying which one it used. When several match, nothing changes and the answer lists them.
