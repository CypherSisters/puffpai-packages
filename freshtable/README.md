# FreshTable

Pantry, shopping list, meal plan and the recipes behind them. By Cypher Sisters, Apache-2.0.

## PuffPAI

**What it does.** FreshTable is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (the FreshTable kitchen app, at `/pkg/freshtable/`) and a tray item;
- a "Shopping list" widget (`list`, at `/widget/list`);
- MCP tools for the brain, at `/mcp`. Tools that change something are listed in `abilities.changes`, so Drawbridge reviews them and the owner can ask for a Yes:

| Tool | What it does | Changes |
|---|---|---|
| `add_pantry` | Add a food to the pantry, or add to its count, with its unit | yes |
| `list_pantry` | The pantry, one food or category, or what is running low | no |
| `use_pantry` | Use some of a pantry item, or all of it; at none left it goes on the shopping list | yes |
| `remove_pantry` | Take an item out of the pantry | yes |
| `add_shopping` | Put an item on the shopping list, with amount and unit; the same item again adds to it | yes |
| `list_shopping` | What is still on the shopping list | no |
| `check_off_shopping` | Tick an item off (bought) | yes |
| `remove_shopping` | Take an item off the list | yes |
| `clear_bought_shopping` | Clear the ticked-off items | yes |
| `add_ingredients_to_shopping` | Put a recipe's or the whole meal plan's ingredients on the list, skipping what is already there or well stocked | yes |
| `list_meal_plan` | The meals planned for the week, Monday first | no |
| `add_meal` | Plan a meal for a day; a saved recipe of the same name brings its ingredients and steps | yes |
| `remove_meal` | Take a meal off the plan, by day or by name | yes |
| `plan_week` | Plan dinners for the week from the saved recipes, with the brain; without it the saved recipes take turns. Days already planned are kept unless the owner says replace | yes |
| `add_recipe` | Save a recipe | yes |
| `list_recipes` | The saved recipes, or one recipe with its instructions | no |
| `remove_recipe` | Delete a saved recipe | yes |
| `import_recipes` | Read recipes from a text file or a photo and save them. The Desktop's "send this file to FreshTable" calls it (`accepts`). | yes |

**What it needs.** Nothing from the internet. Week plans, meal ideas, the chat tab, receipt reading and recipe photos ask the bot's own brain through the Core API (`needs.brain`: text and images). Without a model that can read images, photo reading does not work; text import still works and falls back to a plain reader (first line is the name, then an "Ingredients" and an "Instructions" part).

**Setup steps.** None.

**Data.** One file, `freshtable.json`, in FreshTable's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once and stamps it (`last_updated`). A file that will not read is never written over. The page sends the whole document with the stamp it started from, one save at a time. If the brain changed anything meanwhile, the server refuses the page's copy, keeps the saved one, and the page reloads it and says so; the change just made on the page is not saved and has to be done again. The brain's tools always read the saved file fresh, so they never undo a page save.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA`, `PUFFPAI_API` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, the widget, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/freshtable/`, which adds the token, so the page's own relative calls work.

## Changes in 2.1.0

- The bot can now use up or take out pantry items, clear the bought items, take a meal off the plan, delete a recipe, put a recipe's (or the week's) ingredients on the shopping list, and plan the week's dinners from your recipes. A week plan keeps days you already planned unless you say to replace them.
- Amounts and units are kept on the shopping list ("2 lbs of chicken"), and asking for the same item twice adds to it instead of making a second line.
- "Do we have rice?" and "how do I make the pancakes?" get a straight answer.
- The page has a button to take a meal off the plan and to delete a saved recipe.
- The page no longer makes up prices for items it adds; an unknown price is $0.
- Two quick saves can no longer share a stamp, and the page saves one change at a time, so an edit on the page and an add from the bot do not quietly undo each other.
- A PDF sent to FreshTable gets a plain "cannot read PDF files yet" instead of garbled text.

## Fixed from today's BuildBot

- Recipe import was refused (403): the import called an app action that was not on the allowlist. Here the import is FreshTable's own tool, `import_recipes`.
- The kitchen page deleted saved recipes on every edit, because its push sent only three lists. Here the page sends the whole document, and the server keeps any part a push leaves out.

## Rough edges

- The "Agents" tab is a set of steps around one brain call, not separate agents.
- When the brain does not answer, the receipt scanner fills in demo items for review, and the page's theme meal planner uses a preset plan. Both say so in a message. (The bot's `plan_week` falls back to your saved recipes instead, and says so.)
- PDF recipes are not read yet (no PDF reader in the standard library).
- If the bot changes something while you are editing on the page, the page reloads the saved copy and your last change there has to be done again.
- Recipes keep their ingredients as one comma list, so an ingredient with a comma in it ("1 cup flour, sifted") splits in two.
- Categories are guessed from words in the name, so some land oddly (candy under Pantry, eggplant under Protein).
- Shopping list prices are only what you type in; nothing looks up real prices.
