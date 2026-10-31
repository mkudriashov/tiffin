TASK: write the menu for one cook segment.

Ingredients you may use, by number (nothing outside this list):
[[ingredients]]

Days and slots to fill (the targets are Tiffin's, for scale only; you do not compute anything):
[[days]]

Each meal's rough size, so the day's dishes are planned in proportion (Tiffin sets the exact portions): [[meals]]
Variety per slot: [[variety]]
Kitchen: [[equipment]]
Every method step must be possible with that kitchen. An air fryer is not an oven: say "air fry 200C 12 minutes", not "roast 40 minutes", when that is what they have. Do not assume anything that is not listed.
same = the same recipe in that slot every day of the segment; rotate = two or three recipes alternating; different = a different recipe every day.
Favourites (may recur once per segment): [[favourites]]
Dishes they have loved, as a guide to their taste - the foods and the kind of cooking, not dishes to copy: [[examples]]
Blocked (never use): [[blocked]]
Recipe names from the last 14 days (do not repeat unless a favourite): [[recent]]
Dinners from the last 6 days (never a dinner again this time, favourite or not): [[dinner_gap]]
Something new: [[novelty]]
Leftovers to use up first: [[leftovers]]
Open packs in this shop window, by number - cook from these first: [[packs]]
Main proteins: [[mains]]
What they asked for this time (their own words; honour it unless it contradicts the rules above): [[asked_for]]
What they want from every menu (their own words; honour it the same way, and where it narrows a choice the rules leave open - which dishes, which foods, how much of one - it decides it): [[notes]]
How their week runs (their own answers; these are rules, checked by Tiffin): [[week]]

Rules: simple, healthy food for someone tired after work.
Breakfast, snack and pre-bed: at most 6 ingredients (salt, pepper and oil do not count), at most 4 steps and at most 10 minutes, with one appliance at most - the microwave or a single pan. Mostly assembly: stir, top, warm.
Lunch and dinner: at most 8 ingredients (salt, pepper and oil do not count) and at most [[minutes]] minutes from start to plate - every minute the method names counts towards prep_minutes. Build the dish around ONE cooked centrepiece - a traybake, a stir fry, a thick curry, a chilli, a rice or pasta dish, a salad - not a protein plus two or three separately cooked sides. No soups, broths or other liquid dishes: [[boxed]], and there are no soup containers. A curry, chilli or stew is fine when it is thick enough to sit in a box. One pan or one tray for the whole dish, and at most two appliances. At most 6 method steps, one thing at a time: never "meanwhile", never "at the same time", never "in the last N minutes", never two pans on the go.
Every slot: plain words, no techniques to look up. One portion size per recipe, quantities per serving in grams; a serving should be a plausible meal for the slot. Choose foods so that protein-rich ingredients appear in every main meal with a proper portion of a carbohydrate food beside them (rice, potatoes, pasta, bread, oats, beans, fruit) - protein has a floor and no ceiling, so a day of meat, eggs and dairy meets its calories with protein and leaves the carbs short - and include at least one deliberate source of fat that belongs in that dish (oil to cook in, a fattier cut such as chicken thigh, oily fish, nuts or seeds, the cheese a dish is known for) rather than relying on lean protein alone - never a food added only for its numbers (cheddar on tuna pasta, peanut butter in a savoury dish); Tiffin scales portions to the targets.
Taste: every dish is one people cook or order because it tastes good - a known pairing of foods, properly seasoned with garlic, onion, herbs, spices, citrus or a sauce from the list. Healthy comes from the foods and the portions, never from blandness.

Choices: for a slot whose variety is 'same', you may also offer ONE alternative dish under "alternatives" ({"slot": ..., "recipe": {...}}), genuinely different from your first choice (another main ingredient or another kind of dish) and keeping every rule above. Tiffin chooses between them; the other is kept for them to swap to. Leave alternatives out when you have none.

Return the JSON: days, each with its day and slots; each slot has its slot name and a recipe with name, ingredients as [{"i": <number from the list>, "g": <grams per serving>}], method steps and prep_minutes; and optionally alternatives. Never write kcal, macros or totals anywhere.
