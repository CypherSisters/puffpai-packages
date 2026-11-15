# Homework Helper

Offline. Helps a learner with schoolwork by teaching one step at a time, without handing over the answer. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Homework Helper is a PuffPAI skill package (`kind: skill`): one `SKILL.md` the brain reads when a learner asks for help with homework, a maths problem, a study question or an essay. It has no server and no tools of its own.

How it helps:

| Situation | What the brain does |
|---|---|
| A new question | asks what they have tried, or shows the first step only |
| Each step | one step, then one small question to check understanding |
| Stuck after two hints | a worked example of a similar problem with different numbers, then back to theirs |
| They give an answer | says if it is right; if not, points to the step that went wrong |
| An essay | helps plan and gives feedback ("Works well", "To work on", "Try this"); never writes it |
| Sums | uses the Calculator package's `calculate` and `convert` tools when installed |

The six rules it gives the brain, each a yes-or-no check: never the final answer to their own graded question or a written essay; one step at a time; end with one small check question; match the learner's level; no sums done in its head when the calculator is there; say plainly when it is not sure of a fact.

**What it needs.** Nothing required. With the Calculator package installed, the brain does arithmetic with its `calculate` tool and unit changes with `convert`. Without it, the brain shows its working and tells the learner to check the sum on a calculator.

**Setup steps.** None.

**Data.** None. It stores nothing. The work stays in the chat.

**How it runs.** It does not run. The brain sees its name and one-line description every turn and reads the rest with `skill_view` when a learner asks for help.

## Files

- `puffpai.json`: the package manifest.
- `SKILL.md`: the instructions the brain reads (under 4,000 characters, one `skill_view` part).
- `icons/book.svg`: the icon.
- `NOTICE.md`: what it was built from.

## Licence

Apache-2.0, Cypher Sisters. See `NOTICE.md`.

## Rough edges

- A small model can be wrong about facts, especially in history, science and spelling rules. It is told to say when it is not sure, but it will not always know. Check with the textbook or teacher.
- A determined learner can still talk it into giving an answer. It is a teaching aid, not a lock. A grown-up nearby still helps.
- It cannot see the page. Questions with diagrams, graphs or pictures need to be typed out.
- Methods differ by school and country (how long division is set out, for example). If the learner's method differs, it should follow theirs, but may not.
- It has not yet been tried end to end with a real local model on a bot.

Cypher Sisters. puff puff pass.
