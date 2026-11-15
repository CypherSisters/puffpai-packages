# Plain Reader

Explains a contract, lease, bill or official letter in plain words, quoting the document as it goes. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Plain Reader is a PuffPAI skill package (`kind: skill`): one `SKILL.md` the brain reads when the owner gives it a contract, lease, bill, medical bill, insurance explanation of benefits, official letter or terms of service and asks what it means. It has no server and no tools of its own.

The answer always has the same sections, in this order:

| Section | What goes in it |
|---|---|
| What this is | one or two sentences |
| What it asks of you | each thing you must do, quoted |
| Dates and deadlines | each date, quoted |
| Money | each amount, quoted |
| Unusual or one-sided | ranked: serious, then worth knowing |
| Questions to ask before you sign or pay | short questions |
| Last line | "This explains the words only. It is not legal, medical or financial advice." |

An empty section says "Nothing found". The six rules it gives the brain, each a yes-or-no check: quote the document's own words for every point; write "The document does not say" when it is silent; never guess an amount, date or name; plain words and short sentences; end with the fixed line; send, save or look up nothing.

It also gives the brain a short "what to look for" table for five kinds of document: contract or lease, bill, medical bill or explanation of benefits, official letter, and terms of service.

**What it needs.** Nothing. No other package, no key, no internet. If the document is a PDF or a photo, the owner pastes its text, or a document package that reads files gives the brain the text.

**Setup steps.** None.

**Data.** None. It stores nothing. The document stays in the chat.

**How it runs.** It does not run. The brain sees its name and one-line description every turn and reads the rest with `skill_view` when a document is to be explained.

## Files

- `puffpai.json`: the package manifest.
- `SKILL.md`: the instructions the brain reads (under 700 words, one `skill_view` part).
- `icons/document.svg`: the icon.
- `NOTICE.md`: what it was built from.

## Licence

Apache-2.0, Cypher Sisters. Parts are adapted from MIT-licensed work and public guidance; see `NOTICE.md`.

## Rough edges

- It is not legal, medical or financial advice, and the brain is not a lawyer. It can miss a clause that matters, or rank one wrongly. For a decision that matters, ask a qualified person.
- A small model may still get a number wrong when it copies it. Check every amount and date against the document.
- A long contract may be more than a small model can hold with the skill. Paste it a few sections at a time.
- The "what to look for" table is short on purpose, so a small model can follow it. It is a starting point, not a full review.
- It has not yet been tried end to end with a real local model on a bot.

Cypher Sisters. puff puff pass.
