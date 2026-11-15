# Letter Writer

Offline. Writes a letter or email for you from scratch, using only the facts you give and leaving every gap in [square brackets]. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Letter Writer is a PuffPAI skill package (`kind: skill`): one `SKILL.md` the brain reads when the owner asks for a letter or email to be written: a complaint, a request, a thank you, a cancellation, or a note to a landlord, a school or an office. It has no server and no tools of its own.

The answer always has the same parts, in this order:

| Part | What goes in it |
|---|---|
| Subject | a short line that says the ask |
| Greeting | "Dear [name]," |
| The ask | what you want, in the first two sentences |
| The facts | what, when, where, in a short paragraph |
| What you want and by when | the outcome and the date |
| Close | "Kind regards," or "Thank you," and your name |
| To fill in | every [bracket] in the letter, one per line |

The six rules it gives the brain, each a yes-or-no check: ask one question first only if the purpose or the reader is unknown; use only facts the owner gave and bracket every gap; say the ask in the first two sentences; plain words, short paragraphs, polite and firm; no threats and no legal claims the owner did not make; no filler phrases. It also gives four short patterns: complain, ask, cancel and thank you.

**What it needs.** Nothing. No other package, no key, no internet.

**Setup steps.** None.

**Data.** None. It stores nothing. The letter stays in the chat. It never sends anything; you copy the letter and send it yourself.

**How it runs.** It does not run. The brain sees its name and one-line description every turn and reads the rest with `skill_view` when a letter is to be written.

## Files

- `puffpai.json`: the package manifest.
- `SKILL.md`: the instructions the brain reads (under 4,000 characters, one `skill_view` part).
- `icons/envelope.svg`: the icon.
- `NOTICE.md`: what it was built from.

## Licence

Apache-2.0, Cypher Sisters. The rules follow public plain-language guidance; see `NOTICE.md`.

## Rough edges

- It is not legal advice. It will not add legal claims, so a letter that needs one (a formal notice, a claim with a legal deadline) needs a qualified person or an advice service.
- A small model may still slip in a detail you did not give, or a stock phrase. Read the letter before you send it, and fill every bracket.
- It writes in the language you asked in. Letter customs differ by country (how to open and close, how formal to be); it uses a plain, widely used style.
- It has not yet been tried end to end with a real local model on a bot.

Cypher Sisters. puff puff pass.
