---
name: shop-product-reader
description: Look up real products on a UK supermarket site and report pack size, price and per-100g nutrition as JSON. Use for Tiffin's weekly product refresh.
---

# Reading a supermarket product

Work through the foods one at a time. Finish one completely before starting the next.

## Once, at the start

1. `tabs_context_mcp` to see what is open. Do not reuse a tab the user is working in.
2. `tabs_create_mcp`. Keep one tab for the whole job, and close it before you report
   (see Reporting).
3. If a cookie banner appears, leave it. You cannot click it (see the last section), and
   `get_page_text` and `read_page` read the page underneath it.

## For each food

1. Search by `navigate` to `search_url`, with two or three plain words from the food's name in
   place of `{q}`, url-encoded, not the whole name. Never type into the site's search box. Do
   not invent a different site - the shop you were given is the shop the answer is filed under.
2. `get_page_text` on the results. The list gives you names with pack sizes, the price, and the
   price per kilo. Choose the ordinary own-brand pack a person would actually buy.
3. `find` that product's name on the results page and `read_page` that element: the link's
   `href` is the product's own address, and the product id in it is `retailer_sku`. With the
   name, the pack and the price from the list, **that is already enough to report the product.**
4. Then, turns allowing, open the product page by `navigate` to that address (never click the
   link) for the nutrition table. **The results list does not carry nutrition.** A product page
   may show a bot check for a few seconds: wait, then read it again. If it does not clear on the
   second try, stop the whole job and report what you have. **A product page that errors, will
   not load or shows half a table never makes a listed food `not_found`:** report it from the
   results list as in step 7. On 26 Sep 2026 rump steak, crème fraîche, egg whites and beef mince
   were all in Sainsbury's results and all came back "not found" because their pages errored.
5. `find` the nutrition table, then `read_page` with its `ref_id`. The headline strip on the
   page shows only energy, fat, saturates, sugars and salt: **protein and fibre are in the
   full table**.
6. Record the pack size from the product name or the "Pack size" line, the normal price, the
   price per kilo, and every per-100 g figure the table gives.
7. Short of turns? Skip the product page for the foods that are left: report each one from the
   results list with every nutrition field null and `"nutrition_basis": "not read"`. A product
   with no nutrition is worth far more than an `unread`.

## Judging a match

The question is **whether this shop sells the food**, in the form the catalogue holds. The
planner uses the answer to decide whether a menu can be bought here at all (Alex, 26 Sep 2026:
"check if the menu can be supported by Sainsbury's food"), so a food the shop plainly sells must
come back as a product even when its page is awkward. Pack size, brand and the nutrition table
are secondary: the macros come from the food table and always did.

The food table's kcal and protein come with each food so you can tell the form. Raw chicken
breast is about 106 kcal and 24 g protein per 100 g; a breaded fillet is nearer 220 kcal. A
different FORM of the food - breaded, flavoured, in a sauce, a ready meal - is not the food.

Report the product, its pack and its price in every one of these cases:

- **Any pack size of the same food.** Six eggs, twelve or fifteen: report the pack you found.
  The planner buys whole packs of whatever size the shop sells.
- **Branded when there is no own-brand** (Arla, Graham's, Kallo). It is still the food.
- **The shop's own name for the same thing.** Garlic granules are garlic powder. Pasteurised
  liquid egg white is egg whites. Half-fat cheddar in 6 x 20 g mini blocks is half-fat cheddar.
  Soy sauce is soy sauce whatever else is in the bottle. A stock pot, a cube or a carton of
  stock is stock. Frozen garden peas are frozen peas. Pizza dough and a pizza base are the same
  food for this planner.
- **Loose and by-the-unit produce.** A bulb of garlic, a head of cauliflower, loose tomatoes:
  report the product with its price, and `pack_g` from the stated weight or null.
- **No usable per-100 g table.** Cooked values (Sainsbury's fresh meat is "cooked as per
  instructions", potatoes "boiled in unsalted water", dry rice and pasta often cooked too), per
  serving only, energy only, or no table at all: report the product with every nutrition field
  null and `nutrition_basis` saying which ("cooked", "per serving", "no table", "not read").
  Do not also list it under `not_found`.

`not_found` is only for these two, and every entry carries its `kind`:

- `"kind": "unstocked"` - you searched and read, and this shop does not sell the food, or sells
  it only in a different form. "The page shows cooked nutrition" is NOT unstocked: it is a
  product with a `nutrition_basis`. Neither is a pack size, a missing weight or a missing table.
- `"kind": "unread"` - you never got to decide: out of turns, out of budget, nothing in the
  search to open, a page that would not load without signing in. Use this for every food still
  on your list when you stop early.

Getting `unstocked` wrong is expensive twice over: the food is not asked about again for a
month, and a menu checked against the shop reads it as unbuyable. On 26 Sep 2026 eggs ("multiple
pack sizes"), garlic ("sold by the bulb"), frozen peas and stock ("nutrition format") and egg
whites ("pasteurised, not raw") were all filed unstocked at a shop that sells every one of them.

## Things that have gone wrong before

- **Clubcard and Nectar prices sit above the normal price** and often carry an end date. The
  normal price is the one to report.
- **"270G-470G" is a variable-weight pack.** `pack_g` is null; the price is still real.
- **Multipacks state the total**, e.g. "6 x 58g". That is 348 g.
- **A blank nutrition row is blank, not zero.** Report null.
- **Do not trigger a browser dialog.** Avoid anything that adds to a basket or confirms.
- **Some shops want a postcode or a sign-in before they show groceries.** Whole Foods Market UK
  sells through Amazon and Ocado asks where you are. If a shop will not show products without
  signing in or setting a delivery address, do not attempt either: stop and put every remaining
  food in `not_found` with the reason. A session that cannot read is a normal outcome here.
- **Marks & Spencer food is read through Ocado**, which lists it as a brand. The product name
  carries the brand, so report it exactly as shown.

## Reporting

Before your final message, close every tab you opened with `tabs_close_mcp` - whether the
job finished, stopped early or hit a problem. A tab left open holds memory on a box that runs
all day, and every run opens a new one (Alex, 27 Sep 2026). Running short of turns? Close the
tab first, then report.

Your final message ends with the JSON object described in `CLAUDE.md`, and nothing after it.
Copy numbers exactly as displayed. Do not round.

## One action per tool call

Every browser action in this job is approved by Tiffin's gate, which allows reading and
navigation to the job's own sites only. Call tools one at a time. `browser_batch` bundles
actions past that check, so the gate refuses it, and trying it first only wastes a turn.
Clicking, typing, form input and JavaScript are refused too: read the page, never act on it.
Everything this job needs is reachable that way - a search is a URL, a product is a URL, and a
table is read with `find` and `read_page`. On 2026-09-14 a run that kept trying to click read
one product out of twelve.
