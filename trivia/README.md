# Trivia

Quiz rounds with scores. The game page is AeroTrivia: pick a mode (casual, timed brain training, or a group game for 2 to 4 players), a category and a level (beginner, mid, jeopardy, or kids), and play. Or ask the bot to quiz you in chat. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** Trivia is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (the AeroTrivia game, at `/pkg/trivia/`) and a tray item;
- MCP tools for the brain, at `/mcp`, so the bot can quiz you in chat or by voice. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `stats` | Games played, average score, best score, the last game, and the score of a round going in chat | no | what are the trivia scores |
| `list_categories` | The categories, how many questions each has, how many are yours and how many suit kids mode | no | which trivia categories are there |
| `ask_question` | The next question of a chat round, with options A to D. The answer is kept back. A round does not repeat a question until that category and level run out | no | quiz me on science |
| `check_answer` | Checks a guess (a letter, or the words of an option), says the answer, and keeps each player's score for the round | no | is it B |
| `end_round` | Ends the chat round and saves its scores (one result; a round with several players is saved like a group game) | yes | that's enough trivia, save our scores |
| `record_result` | Saves a round played some other way: how many right out of how many | yes | save our trivia round: we got 7 out of 10 right in history |
| `add_question` | Adds a question of your own (the right answer and 3 wrong ones) to a category; a new category name makes a new category | yes | add a trivia question: what is our dog's name, the answer is Biscuit |
| `remove_question` | Removes a question you added | yes | take my question about the dog out of trivia |

**Kids mode.** A level, not an age: nothing asks for or keeps anyone's age. It asks the beginner questions of the categories that suit children (listed in `questions.json` as `kids_categories`; the movie categories are left out), plus any question you add at the kids level. On the page, picking Kids shows only those categories. A right answer counts 100 points, like beginner.

**What it needs.** Nothing from the internet and no brain: the questions come with the package (`questions.json`, 165 questions in 11 categories at three levels), plus the ones you add. The page and the brain use the same bank. In chat the brain can still write a question of its own on any topic; `add_question` keeps a good one for later.

**Setup steps.** None.

**Data.** One file, `trivia.json`, in Trivia's data folder (`PUFFPAI_DATA`), the same shape as today's BuildBot (`{results: [...], last_updated}`), so an old file can be copied in. Your own questions sit beside the scores in the same file (`my_questions`, and `my_categories` for the categories you made; at most 2,000 questions). Every save replaces the whole file at once and stamps it. A file that will not read is never written over (the questions that came with Trivia still play). Every field of a result is checked before it is saved: scores are whole numbers from 0 to 100,000 (the game's own scoring goes past 1,000), never NaN or infinity, a group game has 1 to 4 players with names of up to 40 characters, and the category, level and mode must be known ones. A number that is not finite in an older file reads as 0. The page saves each finished game with one call (`POST api/result`) and also keeps a copy in the browser, so a game still counts when the server is down; on open it adds the saved scores to the browser's list.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/questions`, `/data`, `/api/result`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/trivia/`, which adds the token.

## Changes in 2.1.0

- The game page plays again: it read the questions under old field names and stopped at the first question.
- Chat quizzes are real rounds: the bot asks, checks your answer, keeps score for each player, and does not repeat a question in a round. The answer is no longer handed over with the question, so it can't slip out early.
- Kids mode, on the page and in chat.
- Add your own questions and categories by asking the bot, and remove them again. They show up on the page too.
- Fixed questions: the most Oscar nominations without a win had two right options, the driest desert now says non-polar, the Danakil Depression question no longer lists a country it also lies in, and the cycling question asks for the oldest Grand Tour (the longest changes from year to year).
- Option text and category names on the page are shown as text, never as page markup.
- Going Home now clears the chosen level too.

## Changed from today's BuildBot

- The question bank moved out of the page into `questions.json`, so the brain can quiz from it too.
- Group games saved a list of scores, which broke the score summary. Here a group game counts with its best player's score.
- The page named a model and pinged it, but never asked it for a question. That indicator is gone; the bank is the whole question source.
- The page loads no font files: it uses the computer's own fonts.

## Rough edges

- New questions are not written by Trivia itself with the brain. In chat the bot can write its own, but only bank questions (and the ones you add) come with a checked answer.
- A chat round lives in Trivia's memory until `end_round`: if Trivia restarts mid-round, that round's score is gone. One round runs at a time for the whole household.
- Your own questions are added and removed in chat only; the page plays them but has no form for them.
- Each category has 5 questions per level, so a 10, 15 or 20 question game in one category fills up from the other categories at the same level.
