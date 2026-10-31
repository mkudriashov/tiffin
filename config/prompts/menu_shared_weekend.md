TASK: write the weekend menu for two people who cook and eat together on Saturday and Sunday.

Ingredients you may use, by number (nothing outside this list; it already leaves out anything either of them refuses):
[[ingredients]]

Days and slots to fill (one dish per slot for BOTH of them - Tiffin gives each person their own portion of the same dish). The figures are for the dishes both of them eat, at an ordinary serving for one person: what those dishes together come to. Protein, fat and fibre are floors, so build those dishes to reach them on their own. A slot marked 'also' is for one of them only and comes on top, with its own share. You do not compute anything:
[[days]]

Kitchen: [[equipment]]
Every method step must be possible with that kitchen. An air fryer is not an oven: say "air fry 200C 12 minutes" when that is what they have.
Favourites (may recur; the first ones listed are this weekend's pick, so lean to those): [[favourites]]
Dinners from the last 6 days (never a dinner again this time): [[dinner_gap]]
Blocked (never use): [[blocked]]
Recipe names from the last 14 days (do not repeat unless a favourite): [[recent]]
Leftovers to use up first: [[leftovers]]
Open packs from the week, by number - cook from these first: [[packs]]
Main proteins: [[mains]]
What they asked for this time (their own words; honour it unless it contradicts the rules above): [[asked_for]]
What either of them wants from every menu (their own words; honour it the same way - both eat every dish, so keep each wish that bears on it): [[notes]]
Their treats: [[taste]]

This is the weekend, and they want it to feel like a treat: healthy versions of indulgent food, built so it still fits their targets. Take the dish people crave and make it high in protein, not a salad with a treat's name. Ideas to draw on (use what the ingredient list allows, vary them, never the same dish twice in one weekend):
- Breakfast: protein French toast (bread soaked in egg white and skyr, cinnamon, berries), protein pancakes (oats or flour, egg whites, whey, a thin drizzle of syrup), breakfast muffins with egg, lean bacon and a light cheese slice, loaded Greek yoghurt pots.
- Lunch and dinner: smash burgers on 5% beef mince with a light cheese slice, gherkins and mustard in a soft bun; air-fryer "fried" chicken in a cornflake crust; lighter carbonara or creamy pasta (half-fat creme fraiche, parmesan, lean bacon); pizza on a thin base with light mozzarella and lean toppings; steak with air-fryer chips; loaded wraps; a lean chilli or curry with rice.
- Snack and pre-bed: protein desserts - chocolate protein pots (skyr or Greek yoghurt, whey, cocoa), a square of dark chocolate with berries and yoghurt, cottage cheese with fruit, peanut butter and banana on a muffin.
- Savoury treats, for breakfast, the snack and pre-bed as much as for lunch: a lighter full English (lean bacon, eggs, beans, mushrooms, wholemeal toast), shakshuka with feta, smoked salmon and eggs on a bagel, a breakfast burrito; a mini pizza on a muffin or a wrap, a tuna melt or ham and cheese toastie, air-fryer chicken bites with a yoghurt dip, an egg muffin or a mini frittata, cottage cheese on rye crackers with ham or smoked salmon, hummus with crunchy vegetables and a toasted pitta, edamame with chilli salt. Savoury ones as good as the sweet, never an afterthought.

Lean is not the aim. A weekend of the leanest version of everything cannot reach the day's fat however it is portioned: put real fat in on purpose - whole eggs, cheese, avocado, olive oil, nuts, peanut butter, dark chocolate, salmon, steak - and keep buns, bread, chips, pasta and syrup to ordinary portions, because protein has to stay high. Fibre counts too: berries, oats, beans, wholemeal bread and a vegetable side.
Balance every dish on its own, not only the day: each should carry about the day's share of protein and of fibre (the pre-bed pot too - berries, oats or seeds in it), because Tiffin scales whole plates up and down, and a dish far leaner in protein than the rest (a pizza, a burger in a big bun) gets cut to half a portion to make room. Pack the treats themselves with protein - more lean meat, egg whites, whey in the batter, skyr in the sauce.

Rules for the weekend: no limit on effort. The two of them are happy to cook properly and for longer when the food is really good - the oven, the hob and the air fryer together, a marinade, a homemade sauce, two things on the go at once. Make it genuinely delicious: proper seasoning, a sauce, some crunch.
The only ceiling is what a recipe card holds: at most 12 ingredients in total (oil and seasoning included), at most 12 method steps and at most 2 hours from start to plate - every minute the method names counts towards prep_minutes.
Protein-rich ingredients in every main meal, and at least one deliberate source of fat that belongs in the dish - never a food added only for its numbers. Every dish properly seasoned (garlic, onion, herbs, spices, citrus or a sauce from the list): a treat is something people would order. Portions are per ONE serving in grams, as for one person; Tiffin scales each person's plate to their own targets.
Plain words, no techniques to look up.

Return the JSON: days, each with its day and slots; each slot has its slot name and a recipe with name, ingredients as [{"i": <number from the list>, "g": <grams per serving>}], method steps and prep_minutes. Never write kcal, macros or totals anywhere.
