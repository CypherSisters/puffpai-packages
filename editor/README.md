# Editor

Fixes writing you paste in, without turning it into someone else's writing. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Editor is a PuffPAI skill package (`kind: skill`): one `SKILL.md` the brain reads when the owner asks it to proofread, fix, check, tidy, tighten, shorten or edit their own text. It has no server and no tools of its own. It gives the brain three levels:

| Level | When | What may change |
|---|---|---|
| Proofread | "proofread", "fix typos", or when the ask is unclear | spelling, grammar and punctuation only |
| Tidy | "tidy", "clean up", "make it clearer", "tighten" | also extra words, unclear parts, sentences over about 30 words |
| Reshape | only when asked: shorter, a different tone, a different reader | length, tone and wording for that ask |

The six rules it gives the brain, each a yes-or-no check: add no fact, claim or idea; keep the writer's words and rhythm where they are correct; change only what the level allows; add no headings, bold or bullets; add none of a short list of AI-sounding words and patterns; no chatty line before or after.

The answer is always the same shape: the edited text, then "What I changed" as up to 8 lines of `old -> new (why)`, then "Left alone on purpose:" when something odd was kept as the writer's voice, and "Cut:" when the length changed.

**What it needs.** Nothing. No other package, no key, no internet.

**Setup steps.** None.

**Data.** None. It stores nothing. The text you paste stays in the chat.

**How it runs.** It does not run. The brain sees its name and one-line description every turn and reads the rest with `skill_view` when an edit is asked for.

## Files

- `puffpai.json`: the package manifest.
- `SKILL.md`: the instructions the brain reads (under 700 words, one `skill_view` part).
- `icons/pencil.svg`: the icon.
- `NOTICE.md`: what it was built from.

## Licence

Apache-2.0, Cypher Sisters. Parts are adapted from MIT-licensed work; see `NOTICE.md`.

## Rough edges

- How well it keeps to the level and the rules depends on the brain the owner picked. A small model may still smooth a sentence it should have left alone, or miss a typo. Read the "What I changed" list.
- Very long text (many pages) may be more than a small model can hold with the skill. Paste a few paragraphs at a time.
- The list of AI-sounding words is short on purpose. It stops the brain adding them; it does not hunt for them in your text.
- It has not yet been tried end to end with a real local model on a bot.

Cypher Sisters. puff puff pass.
