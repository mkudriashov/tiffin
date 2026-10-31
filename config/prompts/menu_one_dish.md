TASK: write two or three dishes for ONE meal, for the person to choose from. Nothing else on their menu changes.

Ingredients you may use, by number (nothing outside this list):
[[ingredients]]

The meal: [[meal]]
Target for this meal, for scale only (Tiffin sizes the portion; you do not compute anything): [[target]]
The dish going out (do not offer it, or anything like it): [[going]]
The rest of the day, staying as it is (do not build on the same main ingredient): [[rest]]
Open packs in the kitchen, by number - build at least one option on one of these: [[packs]]
Kitchen: [[equipment]]
Every method step must be possible with that kitchen. An air fryer is not an oven: say "air fry 200C 12 minutes" when that is what they have. Do not assume anything that is not listed.
Favourites to draw on: [[favourites]]
Blocked (never use): [[blocked]]
Recipe names from the last 14 days (do not repeat): [[recent]]
What they asked for this time (their own words; honour it unless it contradicts the rules above): [[asked_for]]
What they want from every menu (their own words; honour it the same way): [[notes]]

Rules: [[rules]]
Make the options genuinely different from one another: a different main ingredient or a different kind of dish each, so choosing means something. Plain words, no techniques to look up. One portion size per recipe, quantities per serving in grams.

Return the JSON: options, each a recipe with name, ingredients as [{"i": <number from the list>, "g": <grams per serving>}], method steps and prep_minutes. Never write kcal, macros or totals anywhere.
