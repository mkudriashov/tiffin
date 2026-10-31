TASK: write [[count]] different dishes for ONE meal, [[meal]], for the person to choose from: each weekday they pick one of a few, so every dish must be one they would be glad to pick.

Ingredients you may use, by number (nothing outside this list):
[[ingredients]]

Target for this meal, for scale only (Tiffin sizes each portion; you do not compute anything): [[target]]
Kitchen: [[equipment]]
Every method step must be possible with that kitchen. An air fryer is not an oven: say "air fry 200C 12 minutes" when that is what they have. Do not assume anything that is not listed.
Already offered, from the dishes they know (do not write these again or anything like them): [[known]]
Dishes they have loved, as a guide to their taste - the foods and the kind of cooking, not dishes to copy: [[favourites]]
Blocked (never use): [[blocked]]
Recipe names from the last 14 days (do not repeat): [[recent]]
What they want from every menu (their own words; honour it, and where it narrows a choice the rules leave open - which dishes, which foods, how much of one - it decides it): [[notes]]

Rules: [[rules]]
Make every dish genuinely different from the others ([[spread]]) across the [[count]], never two that are one dish with one food changed, so choosing means something. Plain words, no techniques to look up. One portion size per recipe, quantities per serving in grams.

Return the JSON: options, each a recipe with name, ingredients as [{"i": <number from the list>, "g": <grams per serving>}], method steps and prep_minutes. Never write kcal, macros or totals anywhere.
