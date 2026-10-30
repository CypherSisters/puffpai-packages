# Calculator

Exact math for the bot, so it never guesses a sum: arithmetic, percentages, tips and unit conversions. By Cypher Sisters, Apache-2.0.

This is HomeBot's `calculate` tool (its safe evaluator and its spoken-math reader) rewritten as a PuffPAI package, with a tip tool and a unit converter added.

## PuffPAI

**What it does.** Calculator is a PuffPAI tool package (`kind: tool`): one small server on this computer (Python standard library only, nothing to install) with three MCP tools for the brain, at `/mcp`. It has no page.

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `calculate` | An expression with + - * / // % ** and brackets, sqrt, abs, round, min, max, pi and e; spoken math ("847 times 62", "2 squared"); percentages ("15% of 80", "80 + 15%"). Answers in a sentence: "15% of 80 is 12." | no | what's 847 times 62 |
| `tip` | A tip on a bill (15% unless you say), the total, and each person's share when split | no | what's an 18 percent tip on 64 dollars split four ways |
| `convert` | Length, weight, volume, temperature and speed, from a built-in table | no | how many kilometres is 5 miles |

`calculate` never runs the text it is given. It reads it into Python's parse tree and walks that tree itself, allowing only numbers, the operators above, brackets, `pi`, `e` and the five functions. Names, attributes, strings, lists and every other call are refused with a plain reason. An expression is at most 200 characters, an exponent at most 1,000, and a whole-number answer at most about 1,200 digits, so `9**9**9` is refused, not computed.

**What it needs.** Nothing: no internet, no brain, no keys.

**Setup steps.** None.

**Data.** None. It stores nothing; there is no file in its data folder.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (`/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`).

## Rough edges

- It works out numbers only. No equations to solve ("x + 3 = 7"), no algebra, no dates or times, no currency conversion (that needs today's rates from the internet).
- Volumes are US measures (cup, pint, quart, gallon, fluid ounce). Imperial pints and gallons are not in the table.
- Tips are plain numbers with two decimals; it does not know your currency.
- Spoken math covers the common words (times, divided by, plus, minus, squared, cubed, to the power of, square root of, percent). Longer sentences may need the brain to turn them into an expression first.
- Answers are rounded to about ten significant figures (conversions to six), so very long decimals are cut short.

Cypher Sisters. puff puff pass.
