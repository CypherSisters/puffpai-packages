# Recipe Box

Offline: it uses no internet. The household's own recipes, kept on this computer, that the bot can find, scale and read out step by step. By Cypher Sisters, Apache-2.0.

Recipe Box keeps recipes. It does not plan meals, keep a pantry or make shopping lists; FreshTable does those.

## PuffPAI

**What it does.** Recipe Box is a PuffPAI app package (`kind: app`). It runs one small server on this computer (Python standard library only, nothing to install) and gives the bot:

- a page in the Desktop (at `/pkg/recipe-box/`) and a tray item. The page has a list with search, a tag filter and favorites; a recipe view with a servings box that rescales the ingredients as you type, Half and Double buttons, a star for favorites, a rating and a "Made it today" button; a cook mode (large text, one step at a time, tap the step or Next to go on, Back to go back, and it asks the browser to keep the screen on where the browser allows); add and edit forms; "What can I make"; import and export; and a print view of one recipe in plain black on white. It works by touch and loads nothing from the internet;
- MCP tools for the brain, at `/mcp`, so you can ask by chat or voice. The tools that change something are listed in `abilities.changes`. `remove_recipe` also says it deletes (`destructiveHint`):

| Tool | What it does | Changes | Sample ask |
|---|---|---|---|
| `find_recipes` | Recipes by words, a tag, an ingredient, or favorites | no | show me our vegetarian recipes |
| `read_recipe` | One recipe, by name, scaled to a number of servings if asked, or one step by its number | no | read me step 3 of the lasagna recipe |
| `what_can_i_make` | Recipes ranked by how many of their ingredients are on hand, with what is missing | no | what can I make with eggs, spinach and cheese |
| `add_recipe` | Save a recipe from fields, or from one block of pasted text or JSON-LD | yes | add a recipe called tomato soup to the recipe box |
| `change_recipe` | Rename, set servings, times, source, ingredients, steps, tags, notes, favorite or rating | yes | rename the banana bread recipe to Grandma's banana bread |
| `scale_or_convert` | Turn an amount into another unit, an oven temperature between F and C, or multiply an amount | no | how many grams is 8 ounces |
| `mark_made` | Note the day a recipe was made, and rate it | yes | mark the chili as made tonight and give it 4 stars |
| `remove_recipe` | Delete one recipe | yes | delete the tuna casserole recipe |

The tools take a recipe by its title or words from it; the bot does not need to look it up first. When no recipe or more than one matches, nothing changes and the answer lists the titles. An exact title (any case, with or without "the" in front) always wins. Otherwise each word said must be a word of three or more letters that is a word of the title or starts a word of the title, and the words together must fit one recipe only: "art" finds "Artichoke Dip", but "Ron" never finds "Sharon's Soup". Words of one or two letters ("s", "e", "au") never match part of a title; they only count in a full exact title like "Pasta e Fagioli". Accents and case do not matter: "creme" and "crème" both find "Crème Brûlée". When only part of a title was said, the answer says first which recipe it used ("Using 'Banana Bread'.").

**With FreshTable.** FreshTable's `list_pantry` tool lists what is in the pantry. When both are installed, the bot can read the pantry with `list_pantry` and pass those items to `what_can_i_make`. Recipe Box never calls FreshTable itself. FreshTable also has tools called `add_recipe` and `remove_recipe` for its own saved recipes; if both apps are installed, say "recipe box" in the ask so the bot picks this one.

**Scaling and units.** All sums are done in code, never by the bot, with every constant in one table at the top of `server.py`. Cups, spoons and counts snap to 1/4, 1/3, 1/2, 2/3 or 3/4; grams and milliliters round to a step that suits the size. Words like "a pinch of" or "to taste" are never scaled, and the answer lists them as "Not scaled". Volume converts to volume and weight to weight; volume is never turned into weight, because a cup of flour and a cup of sugar weigh different amounts. How each rule works: `PROCESS.md`.

**Importing.** Paste a recipe into the Import box (or give it to the bot as one block of text): a recipe written out, with a title line, a line that says Ingredients and a line that says Steps, Method, Directions or Instructions; or the JSON-LD a recipe web page carries (a schema.org Recipe, bare or inside its `<script type="application/ld+json">` tag); or this app's own export file. You paste it; Recipe Box never fetches a web page. A recipe whose title is already here is skipped, never overwritten.

**Exporting.** Export all gives one JSON file with every recipe. Each recipe also exports as a plain text file, and prints.

**What it needs.** Nothing from the internet, no keys and no brain.

**Setup steps.** None. Add a recipe, or paste one in.

**Data.** One file, `recipes.json`, in the package's data folder (`PUFFPAI_DATA`). Every save replaces the whole file at once (temp file, then rename) under one lock, and stamps it. The file carries the marker `"app": "recipe-box"` and `"format": 1`. A file that will not read, an empty one, or one without that marker and a list of recipes (another app's file, `{}`) is never written over: every save is refused, and the page and the bot say so and name the file. A missing file is a fresh start. On remove, PuffPAI asks whether to keep the data.

**How it runs.** The Core starts `{python} server.py --port {port}` with `PUFFPAI_PORT`, `PUFFPAI_DATA` and `PUFFPAI_TOKEN` set. It listens on 127.0.0.1 only. Every route (the page, `/data`, `/updated`, `/view`, `/export`, `/export_text`, `/api/*`, `/health` and `/mcp`) refuses a request without the package's `PUFFPAI_TOKEN` (as `Authorization: Bearer <token>` or `X-PuffPAI-Key`). The Desktop opens the page through the Core at `/pkg/recipe-box/`, which adds the token. The page checks the stamp every few seconds and reloads when the bot changed something. It takes its colours and fonts from the Desktop's look when the Desktop gives them.

**Files.** `puffpai.json` (the manifest), `server.py` (the server and all the sums), `web/index.html` (the page), `icons/recipe-box.svg`, `PROCESS.md`, `NOTICE.md`, `tests/test_server.py`.

**Licence.** Apache-2.0. See `NOTICE.md`.

## Rough edges

- Ranges like "2-3 tbsp", and any line with a second number or amount word anywhere ("1 or 2 eggs", "250 g (2 cups) flour", "1 (14 oz) can tomatoes", "1 dozen eggs"), are kept as written and not scaled; they show under "Not scaled". A pack size like "x 400g" right after the amount does not count.
- When scaling, only a plain counted food changes its word ("1 egg" doubled is "2 eggs"). Other lines with no unit are not scaled across 1: "2 egg whites" halved and "1 carton eggs" doubled show under "Not scaled".
- Scaled amounts are not moved to a bigger unit: 4 times 3 tbsp says 12 tbsp, not 3/4 cup. They do go down to smaller ones for an exact amount: half of 1/3 cup says 2 tbsp plus 2 tsp.
- An amount with no exact kitchen measure says "about" with the exact figure in brackets, like "about 1/4 tsp (0.3 tsp)".
- An imported recipe whose servings or times do not read keeps "not read" or "not given"; set them by hand.
- "What can I make" is plain word matching. "Milk" also matches "coconut milk", and "cheese" matches any cheese. Water, salt and pepper are taken as on hand.
- US measures only: a cup is the US cup (236.6 ml) and an ounce is the US ounce. A metric cup (250 ml) or an imperial pint is not known.
- JSON-LD from web pages varies a lot. What it can not read (odd shapes, nutrition, images, reviews) is left out; check a pasted recipe after import.
- Not encrypted by this app; your recipes are as private as this computer's folder.
- An empty, damaged or foreign `recipes.json` stops the app from saving until you move that file away or put a good copy back. Nothing is overwritten by accident.

Cypher Sisters. puff puff pass.
