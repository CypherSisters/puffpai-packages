# How Recipe Box scales, converts, matches and reads recipes

Every sum is done in code (`server.py`), never by the bot. Every constant is in one table near the top of `server.py`.

## Scaling a recipe

1. A target number of servings gives the factor: target divided by the recipe's own servings. A recipe that does not say how many it serves can only be scaled by a factor.
2. Double is 2, half is 1/2, triple is 3. The factor is between 0.05 and 100.
3. Each ingredient whose amount is a number is multiplied by the factor as an exact fraction, never a rounded decimal. A stored 0.3333 is read back as 1/3.
4. Cups, spoons, fluid ounces, pints, quarts, pounds, ounces, and counts like eggs or cloves: the amount is shown exactly when it is a whole number plus halves, thirds, quarters, sixths or eighths.
5. A part that is not one of the unit's own measures goes down to a smaller unit when that gives an exact amount: a cup to tablespoons (16), a tablespoon to teaspoons (3), a fluid ounce to tablespoons (2), a pint, quart or gallon to cups, a pound to ounces (16). Half of 1/3 cup is 1/6 cup, said "2 tbsp plus 2 tsp". 1 1/8 teaspoons is said "1 tsp plus 1/8 tsp".
6. When no exact kitchen amount exists, the answer says "about" the nearest kitchen measure (in the same unit when that is within 2 percent, otherwise to the nearest spoon) and gives the exact figure in brackets: "about 1/4 tsp (0.3 tsp)". An amount too small for any spoon says "a very little" with the exact figure. Nothing is rounded without "about".
7. Grams and milliliters round to the nearest 1 under 100 and the nearest 5 from 100 up (kilograms and liters the same, counted in grams and milliliters). When that moves the amount by more than 2 percent, the answer says "about" and gives the exact figure. A half rounds up.
8. An amount in words ("a pinch of", "to taste"), a range ("2-3") or no amount at all is left as it is, and the answer lists it under "Not scaled".
9. One blanket rule: if any other number, fraction or amount word (one to twelve, half, quarter, couple, few, dozen) appears anywhere after the leading amount and its unit, the line is not scaled and is listed under "Not scaled" as written. So "250 g (2 cups) flour", "8 oz / 225 g cheese", "1 or 2 eggs", "1 egg, or 2 if small" and "1 dozen eggs" stay as they are. The one exception is a pack size right after the amount: "1 x 400g can tomatoes" halved is "1/2 x 400g can tomatoes" (doubled, rule 11 below lists it).
10. The unit follows the amount shown: "1 cup", "2 cups", "2-3 cloves" (a range takes its larger end). Above 1 is plural, 1 or less is singular.
11. With no unit, a scaled line changes its food word only when it is just the amount, plain words like large, small or ripe, and one of these: egg, yolk, onion, shallot, carrot, potato, tomato, lemon, lime, banana, apple. Then 1 or less is singular and anything else plural: "1 1/2 eggs", "1/2 egg", "0 eggs". Any other line with no unit keeps its words, so it is not scaled across 1 (1 or less counts as singular): "2 egg whites" halved, "1 carton eggs" doubled and "2 people" halved are listed under "Not scaled"; "2 egg whites" doubled is "4 egg whites". A line read back unscaled is never reworded.

## Reading numbers

1. A number the bot or the page sends (an amount, a factor, servings, a step) is read strictly: digits with at most one decimal point, or a simple fraction like 1/2 or 1 1/2. Servings may be followed by a word like people or servings. Anything else, like 1e400, 1,5 or 2x, is refused with a plain answer.
2. A number in an imported recipe is read the same way. Servings that do not read are kept as the words "not read", and a prep or cook time that does not read (like a broken ISO duration) as "not given", never as 0 minutes.

## Converting units

1. Volume: teaspoon 4.92892 ml, tablespoon 14.78676 ml, fluid ounce 29.57353 ml, cup 236.58824 ml, pint 473.17647 ml, quart 946.35295 ml, gallon 3785.41178 ml, liter 1000 ml. These are US measures.
2. Weight: ounce 28.34952 g, pound 453.59237 g, kilogram 1000 g.
3. Volume converts only to volume and weight only to weight. Volume to weight, or weight to volume, is refused with the reason: it depends on the ingredient.
4. The answer follows the scaling rules above: exact kitchen measures when they exist, otherwise "about" with the exact figure in brackets; metric amounts to the nearest 1 or 5, with "about" when that moves them by more than 2 percent.
5. Oven temperatures: Celsius is (Fahrenheit minus 32) times 5/9; Fahrenheit is Celsius times 9/5 plus 32. The answer gives the whole degree, then "about" the nearest 5 for an oven dial.
6. "C" alone with an amount under 30 and no unit to convert to is read as cups, not degrees.

## What can I make

1. The items on hand are split on commas, semicolons, new lines and the word "and".
2. Each item and each ingredient is cut to its food words: lower case, plurals made singular (tomatoes to tomato, berries to berry), and words like fresh, chopped, large, dried or to taste left out.
3. An ingredient is on hand when its food words are all in an item's, or an item's food words are all in the ingredient's. "Cheese" matches "cheddar cheese".
4. Water, salt and pepper count as on hand. A recipe needs at least one other match to be listed.
5. Recipes are ranked by the share of their ingredients on hand, then by how many match, then by title. The top 10 are listed, each with what is missing.

## Reading pasted recipes

1. Text that starts with `{` or `[` is read as JSON. Otherwise any `<script type="application/ld+json">` blocks in it are read as JSON. Otherwise it is read as a recipe written out.
2. This app's export: its list of recipes, each checked field by field.
3. JSON-LD: every object whose `@type` is Recipe, wherever it sits (a list, `@graph`, `mainEntity`). Title from `name`; ingredients from `recipeIngredient`; steps from `recipeInstructions` (text, a list of text, HowToStep, or HowToSection with its steps); servings from the first entry of `recipeYield` that reads as a number ("4", "4 servings", "Serves 4"), otherwise "not read"; prep and cook minutes from `prepTime` and `cookTime` (ISO durations like PT1H30M), "not given" when one is there but does not read; source from the author's name, or the page address; tags from `recipeCategory`, `recipeCuisine`, `suitableForDiet` and `keywords`; notes from `description`. Tags inside text are dropped and entities read. Anything missing or of an odd shape is left out.
4. Written out: the first line is the title. Lines before "Ingredients" may say Serves, Prep, Cook, Source or Tags. Then one ingredient per line. Then "Steps", "Method", "Directions" or "Instructions", and one step per line (numbers and bullets are dropped). An optional "Notes" block follows.
5. Each ingredient line is read as amount, unit, item and note: "1 1/2 cups flour, sifted" is 1.5, cup, flour, sifted.
6. A recipe whose title is already here (any case) is skipped and named in the answer. Nothing is ever overwritten.

## Finding a recipe by name

Titles and the words asked are compared with accents and case folded: "creme" and "crème" both find "Crème brûlée". Letters in other scripts (Greek, Cyrillic, Chinese and so on) stay letters and match only themselves. The same folding is used by `find_recipes`.

1. An exact title (any case, with or without "the" in front) or id. This always wins.
2. Otherwise the words asked, without "the", "recipe" and the like and without any word of one or two letters. Each word left (three or more letters) must be a word of the title, the same word without a plural ending, or the start of a word of the title. "art" finds "Artichoke Dip"; "Ron" does not find "Sharon's Cake", because "ron" is inside "Sharon", not at its start. When only short words were asked ("s", "e", "au"), nothing matches unless the whole title was said.
3. The words must fit exactly one recipe. None, or more than one: nothing changes and the answer lists the titles.
4. When a recipe was found by part of its title, the answer says first which one it used.
