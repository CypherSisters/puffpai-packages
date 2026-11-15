# How Emergency Plan scores readiness, shows the review note, and imports a plan

## The readiness score

1. The score is a count of 8 plain checks. Each check is yes or no; there is no weighting and no model.
2. An out-of-area contact: at least one contact of the kind "out-of-area contact" with a number.
3. At least two meeting places: two or more of the four meeting places have a place name or an address.
4. Every person has a number: there is at least one person in the household, and every person has a mobile number. The household is the shared people list when it answers, and the plan's own list otherwise.
5. The shutoffs are written down: the water shutoff, the gas shutoff and the main breaker each say where they are. A home with no gas writes "none".
6. The grab list has at least one item.
7. At least one hazard is ticked as applying here and has a note on what we do.
8. Reviewed in the last 6 months: the last reviewed date is 183 days ago or less. A date later than today does not count.
9. A drill in the last year: a practice entry of the kind "drill" is dated 365 days ago or less. A date later than today does not count.
10. The score is how many of the 8 are yes. Each one that is not says what is missing.

## The review note

1. Logging a practice entry of the kind "review" sets the plan's last reviewed date to that day, unless a later review is already recorded. A recorded date later than today (from an import) does not count, and the next review logged replaces it.
2. The page shows the note, and the bot adds it to its answer, when the plan has never been reviewed, the last review was more than 183 days ago, or its date is later than today.
3. The note goes away as soon as a review is logged.
4. Days follow this computer's clock and date.

## Import

1. Export gives one JSON file with the whole plan.
2. Import reads such a file. The page asks before it replaces anything, and the server refuses an import that does not say it is confirmed.
3. Every part of the file is checked first: each section has the right shape, each piece of text fits its length, and the lists are not too long. If anything does not fit, nothing changes and the answer says what was wrong.
4. If the current `plan.json` will not read (damaged or empty), import stops and nothing is changed. Move that file away first.
5. The current plan is copied to `plan-backup-<date and time>.json` in the data folder.
6. Then the imported plan replaces the whole plan at once (temp file, then rename).
7. To go back, import the backup file.
