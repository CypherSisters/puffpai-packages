# How Radio Plan works out the next check-in, the channels and the pocket cards

## The numbers

1. A check-in with no listen time written listens for 5 minutes.
2. A check-in with no wait written goes to the backup channel after 5 minutes.
3. A listen time or a wait is 1 to 240 minutes.
4. The message list shows the newest 10 unless asked for more, and at most 50.

All of these are in one table, `NUMBERS`, in `server.py`.

## A check-in's channels

1. The check-in's channel is the channel whose label matches what is written, by the name rule below.
2. When nothing is written, it is the first channel kept for primary.
3. The backup is worked out the same way, from the check-in's backup channel, or the first channel kept for backup.
4. Words that match no channel are shown as they are written.

## The next check-in (the Now panel and next_check_in)

1. The check-in times are put in order through the day.
2. If the clock is inside a check-in's listen time (from its start, for its listen minutes), the answer says that check-in is on now, on which channel, and until when. A listen time can run past midnight: a check-in at 23:58 with 5 minutes to listen is still on at 00:01.
3. The next check-in is the first one later than now today. If there is none, it is the first one tomorrow.
4. The answer says its time, how long until it, its channel, how long to listen, when to go to the backup channel and which one, and that if there is still no contact, try again at the check-in after it. Any "then" note the owner wrote is added.
5. Times follow this computer's clock.

## which_channel

1. A channel's exact label: that channel.
2. Otherwise a person's name or call sign: their call sign, primary and backup channel (their own if written, else the household's), the check-in times and what to do if there is no contact.
3. Otherwise words for a purpose (like neighbors, emergency, backup, weather): the channels kept for it. Listen only and weather also list the listen-only stations.
4. Otherwise one channel found by the name rule below.
5. Nothing matches: the answer lists the channels and the people. Several match: it lists them and asks which.

## Finding an item by name

1. An exact name always wins, with accents, case and a leading "the" ignored ("cafe luna" is "Café Luna").
2. Otherwise every word said of 3 letters or more must be a whole word of the name or the start of one, never letters inside a word; shorter words are ignored, so "hub" finds "Hubert Bay" but "ub", "i" or "über" find nothing. One match is used and the answer says which; several change nothing and are listed.
3. update_radio_plan adds or changes by name: it changes an item only on its exact name, and any other name is a new item, so "Zoë" never changes "Zoey Park".

## A pocket card

1. One card per person in the household.
2. It shows their name and call sign, their primary and backup channel (as in which_channel), every check-in time, and what to do if there is no contact, from the first check-in of the day.
3. It ends with the reminder that rules differ by country.

## Kinds from everyday words

1. A kind is read from its own name first (like "scanner" or "base station").
2. Otherwise from words that point to it, in a fixed order (like walkie-talkie or FRS for a handheld two-way, car for mobile, NOAA for a weather alert radio).
3. Words that point to no kind are stored as other, and the answer says so. Nothing is refused for its kind.
4. A new radio or listen-only station with no kind given takes its kind from its name when the name says one (like "Sam's walkie-talkie" or "Weather alert").

## Finding an item by name

1. The same id, name, time or words first.
2. Then one item whose name holds the words.
3. When several hold them, nothing changes and the answer lists them.
4. update_radio_plan adds an item that is not there yet; remove_radio_item changes nothing and lists what is there.
