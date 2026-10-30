# Trivia

Quiz rounds with scores. The game page is AeroTrivia: pick a mode (casual, timed brain training, or a group game for 2 to 4 players), a category and a difficulty, and play. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Trivia is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (the AeroTrivia game, at `/pkg/trivia/`) and a tray item;
- MCP tools for the brain, at `/mcp`, so the bot can quiz you in chat or by voice. The one tool that changes something is listed in `abilities.changes`, so Drawbridge reviews it and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `stats` | Games played, average score, best score and the last game | no | what are the trivia scores |
| `list_categories` | The question categories and how many questions each has | no | which trivia categories are there |
| `ask_question` | One multiple-choice question from the bank, with its answer last, for the bot to say after you guess | no | ask me a science trivia question |
| `record_result` | Save a round played in chat: how many right out of how many | yes | save our trivia round: we got 7 out of 10 right in history |

**What it needs.** Nothing from the internet and no brain: the questions come with the package (`questions.json`, 165 questions in 11 categories at three levels). The page and the brain use the same bank.

**Setup steps.** None.

**Data.** One file, `trivia.json`, in Trivia's data folder (`PUFFPAI_DATA`), the same shape as today's BuildBot (`{results: [...], last_updated}`), so an old file can be copied in. Every save replaces the whole file at once and stamps it. A file that will not read is never written over. Every field of a result is checked before it is saved: scores are whole numbers from 0 to 100,000 (the game's own scoring goes past 1,000), never NaN or infinity, a group game has 1 to 4 players with names of up to 40 characters, and the category, level and mode must be known ones. A number that is not finite in an older file reads as 0. The page saves each finished game with one call (`POST api/result`) and also keeps a copy in the browser, so a game still counts when the server is down; on open it adds the saved scores to the browser's list.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/questions`, `/data`, `/api/result`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/trivia/`, which adds the token.

## Changed from today's BuildBot

- The question bank moved out of the page into `questions.json`, so the brain can quiz from it too.
- Group games saved a list of scores, which broke the score summary. Here a group game counts with its best player's score.
- The page named a model and pinged it, but never asked it for a question. That indicator is gone; the bank is the whole question source.
- The page loads no font files: it uses the computer's own fonts.

## Rough edges

- New questions are not written by the brain yet. In chat the bot can still make up its own, but only the bank's questions come with a checked answer.
- A round played in chat is saved only when someone asks the bot to save it.
