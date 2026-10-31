import { $, addDays, api, card, counted, dayName, daysWords, el, fail, fmtInt, num, plateGrams, plural, slotLabel, state, unitWords, weekday } from "./helpers.js";
import { cook, openCook, per100 } from "./cook.js";
import { drawTicket, outdated, tracking } from "./more.js";
import { redrawInPlace, setLastDrawn, show } from "./app.js";
import { packsPanel } from "./shopping.js";
import { manualPanel } from "./manual.js";

/* ---------------------------------------------------------------- menu */
// The day and meal a link from the bot asked for, or today. The page never read its own
// address, so no link could open a day or a meal: every one landed on the top of the tab.
export function scrollToAsked() {
  const q = new URLSearchParams(location.search);
  const day = q.get("day");
  const slot = q.get("slot");
  // a meal skipped since the link was sent has no card: its day is still the place to land
  const target = (day && ((slot && document.getElementById(`d-${day}-${slot}`))
    || document.getElementById(`d-${day}`)))
    || document.getElementById(`d-${state.today}`);
  if (target && target.tagName === "DETAILS") target.open = true;
  if (target) target.scrollIntoView({ block: "start" });
}

// A card or panel brought into view after a redraw. "nearest" leaves one whose top is already on
// screen where it is - under the sticky tab row, when that top is in the strip the row covers -
// and ignores scroll-margin-top; "start" honours the margin and clears the row.
export function bringIntoView(node) {
  if (!node) return;
  const nav = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--nav-h")) || 56;
  node.scrollIntoView({ block: node.getBoundingClientRect().top < nav + 12 ? "start" : "nearest" });
}

export function wirePlanDetails(root) {
  for (const details of root.querySelectorAll("details")) {
    const summary = details.querySelector(":scope > summary");
    if (!summary || summary.dataset.planWired) continue;
    const reflect = () => summary.setAttribute("aria-expanded", String(details.open));
    summary.dataset.planWired = "true";
    reflect();
    details.addEventListener("toggle", reflect);
  }
}

export async function renderMenu(opts = {}) {
  // without a word: the fetch never carried the day the link named, so it always rendered
  // whichever segment covers today and silently substituted it under an unchanged heading.
  // ?segment= now names any day IN a segment, not only its first one - the server resolves the
  // segment that covers it (queries.menu) - so the day out of the address is enough on its own.
  const day = new URLSearchParams(location.search).get("day");
  const ticket = drawTicket("menu");
  // the person's own food lists ride along: what they avoid is what this tab is changed by.
  // A page that cannot read them still shows the menu - the lists are a panel, not the tab
  const prefsAsked = api("api/prefs").catch(() => null);  // asked alongside, not after
  const data = await api(`api/menu?segment=${day || ""}`);
  const prefs = await prefsAsked;
  // the return-after-a-while redraw and a card's own change can be in flight at once: an answer
  // overtaken by a newer ask is dropped before it draws, or takes the newer one's said line
  if (outdated("menu", ticket)) return;
  // what the last change on a card said, looked for in this render and then let go, drawn or
  // not: a plate skipped from its card has no card to carry it, and a result left waiting came
  // back later under a card it no longer described
  const toSay = menuSaid;
  menuSaid = null;
  // after this render has put the days on the page. Redrawn after a change, the page stays with
  // what said how it went: jumping to today's heading left the result line off the screen. The
  // foods panel and the weekend switch sit above every day, and say theirs in the panel itself
  const landing = opts.keepPlace && plateSaid ? plateSaid.day : null;
  const swapped = opts.keepPlace && toSay ? toSay.anchor : null;
  const panelSaid = opts.keepPlace ? (foodsSaid ? "foods" : weekendSaid ? "weekend" : null) : null;
  if (!opts.stay) setLastDrawn(Date.now());
  setTimeout(opts.stay ? () => {} : swapped
    ? () => bringIntoView(document.getElementById(swapped))
    : landing
      ? () => {
        plateSaid = null;  // said in its panel, or that day has no panel any more: either way, once
        bringIntoView(document.getElementById(`plates-${landing}`));
      }
      : panelSaid
        ? () => bringIntoView(document.getElementById(panelSaid))
        : opts.keepPlace ? () => {} : scrollToAsked, 0);
  const body = $("#menu-body");
  body.replaceChildren();
  // Questions and a running build take priority; ordinary settings follow the days.
  const foodSettings = prefs ? foodsPanel(prefs) : null;
  const buildSettings = rebuildPanel(data);
  if (data.building) body.append(buildSettings);
  if (foodSettings && foodSettings.open) body.append(foodSettings);
  if (data.weekend) body.append(weekendPanel(data.weekend));
  if (!data.menu && tracking()) {
    const more = el("button", { type: "button", class: "link", text: "How you plan, in More" });
    more.addEventListener("click", () => show("more").catch((e) => fail(e.message)));
    body.append(el("p", { class: "muted" }, "You track only, so no menus are planned. ", more, " changes that."));
    if (foodSettings && !foodSettings.open) body.append(foodSettings);
    wirePlanDetails(body);
    return;
  }
  if (!data.menu) {
    if (!data.building) body.append(el("p", { class: "muted", text: "No menu for today or the days ahead yet: Build a menu below, or send /cook to the bot (usually 2-4 minutes)." }));
    if (!data.building) body.append(buildSettings);
    if (foodSettings && !foodSettings.open) body.append(foodSettings);
    body.append(manualPanel(() => renderMenu({ keepPlace: true })));
    wirePlanDetails(body);
    return;
  }
  if (data.covers_today === false) {
    // nothing today, something ahead: say so and show it, rather than stopping at the gap
    body.append(el("p", { class: "muted", text: `No menu for today. Coming up from ${dayName(data.menu.segment_start)}:` }));
  }
  const menus = data.menus && data.menus.length ? data.menus : [data.menu];
  for (const [n, window] of (data.packs || []).entries()) {
    body.append(packsPanel(window, n === 0 && (data.mains === null || data.mains === undefined) ? data.mains_question : null));
  }
  if (!prefs && data.refused_still_on_it && data.refused_still_on_it.length) {
    // the bot says it; the page showing the same menu has to as well. With the lists in hand,
    // the foods panel above says it instead - by dish, with a button that swaps them out
    body.append(el("p", { class: "warn-box", text: `This menu still has ${data.refused_still_on_it.join(", ")} in it, which you now avoid. Send /cook replace to the bot to rebuild it without.` }));
  }
  // one heading per cook, not one span over every segment shown: Mon-Wed and Thu-Fri are two
  // separate shops and two separate cooks, and a single "14th to 20th" span read as one - with
  // the two unplanned days between them not mentioned at all
  // each day once, under the first heading that covers it: a replacement ahead of the week it
  // replaces covers some of the same days, and those were drawn under both headings
  const drawn = new Set();
  for (const [idx, m] of menus.entries()) {
    const segDays = data.days.filter(
      (d) => d.day >= m.segment_start && d.day <= m.segment_end && !drawn.has(d.day));
    if (!segDays.length) continue;
    segDays.forEach((d) => drawn.add(d.day));
    const firstDay = segDays[0].day;
    const lastDay = segDays[segDays.length - 1].day;
    body.append(el("h2", { class: "segment",
      text: firstDay === lastDay ? dayName(firstDay) : `${dayName(firstDay)} to ${dayName(lastDay)}` }));
    // a cook's days side by side on a wide screen, one after another on a phone
    const daysBox = el("div", { class: "menu-days" });
    body.append(daysBox);
    for (const day of segDays) {
      const dayBox = el("section", { class: "menu-day", "aria-label": dayName(day.day) });
      daysBox.append(dayBox);
      const head = el("h3", { class: "day" },
        el("span", { text: dayName(day.day) }),
        el("span", { class: "muted day-total", text: `${fmtInt(day.kcal)} kcal · P ${num(day.protein_g)} g` }));
      const askedSlot = new URLSearchParams(location.search).get("slot");
      head.id = `d-${day.day}`;  // a link from the bot names a day, and lands here
      dayBox.append(head);
      if (toSay && toSay.anchor === head.id) {
        dayBox.append(el("p", { class: "plate-said", role: "status", text: toSay.text }));
      }
      if (day.plates && day.plates.adjustable) dayBox.append(platesPanel(day));
      for (const slot of day.slots) {
        // A link or a change opens only the meal it names.
        const box = el("details", { class: "slot" });
        box.open = askedSlot === slot.slot && new URLSearchParams(location.search).get("day") === day.day;
        // ...or the meal new dishes were asked for, until what came of it has been said
        if (askedFor(day.day, slot.slot)) box.open = true;
        box.id = `d-${day.day}-${slot.slot}`;  // ...or one meal on it
        box.append(el("summary", {},
          el("span", { class: "muted slot-kind", text: slotLabel(slot.slot) }),
          el("h3", { text: slot.name }),
          el("p", { class: "muted",
            text: `${fmtInt(slot.kcal)} kcal · P ${num(slot.protein_g)} g · ${slot.prep_minutes} min${slot.batch_cooked ? " · from the box" : ""}${slot.together ? " · cooked together" : ""}` }),
          // Chicken (Bought 500g pack, used 200g)")
          ...(slot.from_packs || []).map((line) => el("p", { class: "muted from-pack", text: line }))));
        if (toSay && toSay.anchor === box.id) {
          box.open = true;
          box.append(el("p", { class: "plate-said", role: "status", text: toSay.text }));
        }
        box.append(el("p", { class: "muted meal-macros", text:
          `${fmtInt(slot.kcal)} kcal · P ${num(slot.protein_g)} g · F ${num(slot.fat_g)} g · C ${num(slot.carb_g)} g · fibre ${num(slot.fibre_g)} g` }));
        // the pot cooked once for the week never said how many boxes it made - a quarter of it
        // read as the whole lunch - and a box out of the freezer was not told how long
        // it keeps or whether it was meant to be frozen at all
        if (slot.together) {
          // the shared weekend, cooked one plate at a time: yours, then theirs, each weighed out
          box.append(el("p", {}, el("b", { text: "Cooked together, one plate at a time. " }),
            `Yours first (${fmtInt(Math.round(slot.portion_g))} g, ${fmtInt(slot.kcal)} kcal), then `
            + `${slot.together.name}'s (${fmtInt(Math.round(slot.together.portion_g))} g, `
            + `${fmtInt(slot.together.kcal)} kcal). Same method for each.`));
        }
        const facts = [slot.servings > 1 ? `Serves ${slot.servings}` : "",
          slot.keep_days ? `Keeps ${plural(slot.keep_days, "day")} in the fridge` : "",
          slot.freezable ? "Freezes" : ""].filter(Boolean);
        if (facts.length) box.append(el("p", { class: "muted", text: facts.join(" · ") }));
        const mult = slot.portion_multiplier;
        /* ing.grams is the WHOLE recipe's grams; a slot is one serving of it, so a pot that
           serves four printed four boxes' worth of chicken under one box's kcal heading
           (numbers_units/04) until this divided by the recipe's own servings too. `m` is the
           plate's own multiplier: this person's, or on a shared dish the other plate's. */
        const gramsAt = (ing, m) => plateGrams(ing, m, slot.servings);
        const ingredientLines = (m) => slot.ingredients.map((ing) => {
          const p = el("p", { class: "muted",
            text: `${ing.name} - ${gramsAt(ing, m)} g` });
          // "2 eggs" beats "116 g" at a hob with no scales: a food served whole says how many,
          // and for any other the pack the grams come out of is in the same payload
          const count = unitWords(ing, gramsAt(ing, m))
            || (ing.pack_g ? counted(gramsAt(ing, m), ing.pack_g, ing.pack_label) : "");
          if (count) p.append(`, ${count}`);
          const what = per100(ing);
          return el("div", { class: "ingredient-line" }, p,
            what ? el("p", { class: "muted per100-figure", hidden: true, text: what }) : null);
        });
        if (slot.ingredients.length) {
          // a heading between the two lists, not just a colour change - at arm's length across
          // a hob a grey ingredient line and a black method line read as one undifferentiated
          // column of sentences otherwise (phone_layout/09)
          if (slot.together) {
            box.append(el("h4", { text: `Ingredients for your plate (${fmtInt(slot.kcal)} kcal)` }),
              ...ingredientLines(mult),
              el("h4", { text: `Ingredients for ${slot.together.name}'s plate (${fmtInt(slot.together.kcal)} kcal)` }),
              ...ingredientLines(slot.together.multiplier));
          } else {
            box.append(el("h4", { text: "Ingredients" }), ...ingredientLines(mult));
          }
          box.append(per100Toggle(slot.ingredients, box.querySelectorAll(".per100-figure")));
        }
        if ((slot.method || []).length) {
          box.append(el("h4", { text: "Method" }));
        }
        for (const [i, step] of (slot.method || []).entries()) {
          box.append(el("p", { text: `${i + 1}. ${step}` }));
        }
        if ((slot.method || []).length) {
          // hands-free from the hob: one step at a time, full screen, tap either half to
          // advance - the plain numbered paragraphs above stay for a quick glance or a copy-paste.
          // A shared dish is cooked once per plate, so each plate has its own run with its own
          // amounts, and each remembers its own place
          const plates = slot.together
            ? [["Cook your plate", `${slot.name} (your plate)`, mult],
              [`Cook ${slot.together.name}'s plate`, `${slot.name} (${slot.together.name}'s plate)`, slot.together.multiplier]]
            : [["Cook", slot.name, mult]];
          const actions = el("div", { class: "row meal-actions" });
          for (const [label, run, m] of plates) {
            const cookLink = el("button", { type: "button", class: "link", text: label });
            cookLink.addEventListener("click", () => openCook(run, slot.method,
              slot.ingredients.map((ing) => `${ing.name} ${gramsAt(ing, m)} g`)));
            actions.append(cookLink);
          }
          box.append(actions);
        }
        if (slot.batch_cooked) {
          // Thursday's lunch is a method for Monday, eaten from a box, not a hob to start now
          // - the bot says which part of the card happens when; the page names the day
          box.append(el("p", { text: `Batch-cooked: this method was for ${dayName(slot.cook_day)}. Eat it from the box, reheating until piping hot if it is a hot dish.` }));
        }
        const actions = box.querySelector(".meal-actions") || el("div", { class: "row meal-actions" });
        if (!actions.parentNode) box.append(actions);
        if (day.plates && day.plates.adjustable) {
          actions.append(swapControl(day.day, slot));
          const row = sizeRow(day, slot);
          if (row) actions.append(row);
        }
        // rated from the exact card it was cooked from, not a name picked off a list on another
        // tab with no day on it
        // labelled, showing the score already given, and saying what a score does - bare
        // "1".."5" under the Cook link dropped a dish on a wet-handed tap with no word
        box.append(el("p", { class: "muted rate-label", text: "Rate it (1 = never again, 5 = more often):" }));
        const stars = el("div", { class: "stars", role: "group", "aria-label": `Rate ${slot.name}` });
        const said = el("p", { class: "muted", "aria-live": "polite" });
        for (let n = 1; n <= 5; n += 1) {
          const b = el("button", { type: "button", text: String(n), "aria-label": `${n} out of 5`,
            class: slot.score === n ? "on" : "" });
          b.setAttribute("aria-pressed", slot.score === n ? "true" : "false");
          b.addEventListener("click", async () => {
            if (b.disabled) return;
            for (const other of stars.children) other.disabled = true;
            try {
              await api("api/dishes/rate", { body: { recipe_id: slot.recipe_id, score: n } });
              for (const other of stars.children) {
                other.className = "";
                other.setAttribute("aria-pressed", "false");
              }
              b.className = "on";
              b.setAttribute("aria-pressed", "true");
              slot.score = n;
              said.textContent = n <= 2 ? "Noted - I will stop proposing it."
                : n >= 4 ? "Noted - it will come back often." : "Noted.";
            } catch (e) {
              fail({ message: e.message, retry: () => b.click() });
            } finally {
              for (const other of stars.children) other.disabled = false;
            }
          });
          stars.append(b);
        }
        box.append(stars, said);
        dayBox.append(box);
      }
    }
    const next = menus[idx + 1];
    if (next) {
      const gapStart = addDays(m.segment_end, 1);
      const gapEnd = addDays(next.segment_start, -1);
      if (gapStart <= gapEnd) {
        // Show gaps between planned cooking days so the missing plan is explicit.
        body.append(el("p", { class: "muted",
          text: `${dayName(gapStart)} to ${dayName(gapEnd)}: not planned yet. Send /cook to the bot to build ${gapStart === gapEnd ? "it" : "them"}.` }));
      }
    }
  }
  if (!data.building) body.append(buildSettings);
  if (foodSettings && !foodSettings.open) body.append(foodSettings);
  body.append(manualPanel(() => renderMenu({ keepPlace: true })));
  wirePlanDetails(body);
}

export function per100Toggle(ingredients, rows = []) {
  const wrap = el("div", { class: "per100" });
  const button = el("button", { type: "button", text: "Per 100 g", "aria-expanded": "false" });
  const figures = el("div", { hidden: true });
  if (!rows.length) {
    for (const ing of ingredients) {
      const what = per100(ing);
      if (what) figures.append(el("p", { class: "muted", text: `${ing.name}: ${what}` }));
    }
  }
  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") !== "true";
    for (const item of rows.length ? rows : [figures]) item.hidden = !expanded;
    button.setAttribute("aria-expanded", String(expanded));
  });
  wrap.append(button);
  if (!rows.length) wrap.append(figures);
  return wrap;
}

// What a person avoids: show affected foods and planned dishes beside the menu controls.
// what the last change to the lists or the menu said, carried across the redraw it caused
export let menuSaid = null;
export let foodsSaid = null;

// The redraw after a change that went in, kept apart from the change: a redraw that fails (one
// dropped answer on the tailnet) is not the change failing, so its Retry draws again and never
// sends the change twice - a swap sent again was refused as not on offer, and one tapped afresh
// swapped out the dish just chosen. What the change said waits for the draw that works.
export async function showChange(said) {
  try {
    await renderMenu({ keepPlace: true });
  } catch (e) {
    fail({ message: `${said ? `${said} ` : ""}The menu could not be drawn again: ${e.message}`,
      retry: () => showChange() });
  }
}

// The meals new dishes were asked for from this page, and what came of each ask once it is known:
// kept here, not on the swap area that asked, which any redraw replaces (the return after a
// while, a change on another card, the end of the build itself - whose redraw came seconds after
// the area had said the result, and drew the meal again with nothing said). Kept, and the meal
// opened again by each redraw to say it, until the area that said it is closed or a swap is made
// for the meal. Each with the days it was asked for - all of them or just the one - so the
// dishes arrive under the same choice.
export const newAsks = new Map();  // "day|slot" -> { everyDay, said }
// "All N days" or "Just Mon" as last picked for a meal, kept here too and not on its area: a
// redraw put the picker back on "All 5 days", and one tap then swapped every day of the pot.
// Let go once a swap is made for the meal.
export const swapDays = new Map();  // "day|slot" -> everyDay
// the ask covering this meal: the same slot, asked from this day or another day of the same pot
export function askedFor(day, slot, days = []) {
  for (const key of newAsks.keys()) {
    const [d, s] = key.split("|");
    if (s === slot && (d === day || days.includes(d))) return key;
  }
  return null;
}
// a swap made for a meal (a dish, or one food in it): its ask and its days are done with
export function swapMade(day, slot, days = []) {
  swapDays.delete(`${day}|${slot}`);
  newAsks.delete(askedFor(day, slot, days));
}

export function chipList(items, label, onRemove) {
  const list = el("ul", { class: "chips", "aria-label": label });
  for (const item of items) {
    const term = typeof item === "string" ? item : item.term;
    const foods = typeof item === "string" ? [] : (item.foods || []);
    const shown = foods.filter((f) => f.toLowerCase() !== term.toLowerCase());
    const x = el("button", { type: "button", class: "chip-x", "aria-label": `Take ${term} off` , text: "×" });
    x.addEventListener("click", () => onRemove(term, x));
    list.append(el("li", { class: "chip" },
      el("span", {}, term, shown.length ? el("span", { class: "muted", text: ` (${shown.join(", ")})` }) : null),
      x));
  }
  return list;
}

export function foodsPanel(prefs) {
  const avoid = prefs.avoid || [];
  const single = prefs.avoid_foods || [];
  const asks = prefs.asks || [];
  const still = prefs.still_on_menu || [];
  const count = avoid.length + single.length;
  const notes = prefs.notes || [];
  const clash = prefs.conflicts || [];
  const panel = el("details", { class: "foods", id: "foods" });
  panel.open = still.length > 0 || asks.length > 0 || clash.length > 0 || foodsSaid !== null;
  panel.append(el("summary", { text: (count
    ? `Foods you avoid (${count})${still.length ? " - still on your menu" : asks.length ? " - one question" : ""}`
    : "Foods you avoid") + (notes.length ? ` · ${notes.length} ${notes.length === 1 ? "note" : "notes"}` : "") }));
  // what one wish keeps another from doing: said, never quietly resolved
  if (clash.length) {
    const box = el("div", { class: "warn-box" });
    for (const line of clash) box.append(el("p", { text: line }));
    panel.append(box);
  }
  const said = el("p", { class: "plate-said", role: "status" });
  said.hidden = true;
  const say = (text) => { said.textContent = text; said.hidden = !text; };
  const change = async (body, button) => {
    if (button) button.disabled = true;
    let got;
    try {
      got = await api("api/prefs", { body });
    } catch (e) {
      if (button) button.disabled = false;
      if (e.status >= 400 && e.status < 500) say(e.message);
      else fail({ message: e.message, retry: () => change(body, button) });
      return;
    }
    foodsSaid = got.said;
    await showChange(got.said);
  };
  // one food by itself (the "Never again" chip, or a made-from answer): by its code, so it
  // reaches no further than the food tapped
  const byItself = async (food, refused, button) => {
    if (button) button.disabled = true;
    let got;
    try {
      got = await api("api/prefs/food", { body: { food, refused } });
    } catch (e) {
      if (button) button.disabled = false;
      if (e.status >= 400 && e.status < 500) say(e.message);
      else fail({ message: e.message, retry: () => byItself(food, refused, button) });
      return;
    }
    foodsSaid = got.said;
    await showChange(got.said);
  };
  if (avoid.length) {
    panel.append(chipList(avoid, "Foods you avoid", (term, b) => change({ list: "avoid", remove: [term] }, b)));
  }
  if (single.length) {
    const codes = Object.fromEntries(single.map((f) => [f.name, f.code]));
    panel.append(chipList(single.map((f) => f.name), "Single foods you avoid", (name, b) => byItself(codes[name], false, b)));
  }
  if (!count) {
    panel.append(el("p", { class: "muted", text: "Nothing yet. A food here never reaches a menu again." }));
  }
  // houmous after chickpeas: asked once, kept either way
  for (const a of asks) {
    const box = el("div", { class: "ask-box" });
    box.append(el("p", { text: `${a.name} is made from ${a.of}: keep it off too, or is it fine?` }));
    const off = el("button", { type: "button", text: `Keep ${a.name.toLowerCase()} off` });
    const fine = el("button", { type: "button", class: "primary", text: `${a.name} is fine` });
    off.addEventListener("click", () => byItself(a.code, true, off));
    fine.addEventListener("click", () => byItself(a.code, false, fine));
    box.append(el("div", { class: "row" }, off, fine));
    panel.append(box);
  }
  // one box for one food or several: "lentils, chickpeas" - and kinds work too: fish, dairy
  const form = el("form", { class: "row add-avoid" });
  const input = el("input", { type: "text", name: "avoid", list: "food-names", autocomplete: "off",
    placeholder: "lentils, chickpeas, fish...", "aria-label": "A food to avoid" });
  const names = el("datalist", { id: "food-names" });
  for (const n of prefs.suggest || []) names.append(el("option", { value: n }));
  const add = el("button", { type: "submit", class: "primary", text: "Avoid" });
  form.append(input, add, names);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const words = input.value.split(",").map((w) => w.trim()).filter(Boolean);
    if (!words.length) { say("Type a food first."); input.focus(); return; }
    change({ list: "avoid", add: words }, add);
  });
  // what the last change did, right under the box it was typed in - not below the fold
  panel.append(form, said);
  if (still.length) {
    const box = el("div", { class: "warn-box" });
    box.append(el("p", {}, el("b", { text: "Still on your menu: " }),
      still.map((d) => `${d.name} (${slotLabel(d.slot).toLowerCase()} ${daysWords(d.days)}) has ${d.holds.join(", ")}`).join("; ") + "."));
    const go = el("button", { type: "button", class: "primary", text: still.length > 1 ? "Swap them for dishes you have had" : "Swap it for a dish you have had" });
    go.addEventListener("click", async () => {
      go.disabled = true;
      go.textContent = "Swapping...";
      let got;
      try {
        got = await api("api/menu/swap-avoided", { body: {} });
      } catch (e) {
        go.disabled = false;
        go.textContent = "Try again";
        if (e.status >= 400 && e.status < 500) say(e.message);
        else fail({ message: e.message, retry: () => go.click() });
        return;
      }
      foodsSaid = got.said;
      await showChange(got.said);
    });
    box.append(el("div", { class: "row" }, go));
    box.append(el("p", { class: "muted", text: "Instant, from your own recipe book. For brand-new dishes, Ask for new dishes on the meal's card (usually 1-2 minutes)." }));
    panel.append(box);
  }
  const likes = prefs.likes || [];
  if (likes.length) {
    panel.append(el("h4", { text: "Foods you like to see often" }));
    panel.append(chipList(likes, "Foods you like", (term, b) => change({ list: "likes", remove: [term] }, b)));
  }
  // Each standing menu note can be removed, and a new one can be added.
  const noteChange = async (body, button) => {
    if (button) button.disabled = true;
    let got;
    try {
      got = await api("api/prefs/notes", { body });
    } catch (e) {
      if (button) button.disabled = false;
      if (e.status >= 400 && e.status < 500) say(e.message);
      else fail({ message: e.message, retry: () => noteChange(body, button) });
      return;
    }
    foodsSaid = got.said;
    await showChange(got.said);
  };
  panel.append(el("h4", { text: "What every menu is asked for" }));
  if (notes.length) {
    panel.append(chipList(notes.map((n) => ({ term: n.text, foods: n.half === "all" ? [] : [n.half] })),
      "Your notes", (term, b) => noteChange({ remove: term }, b)));
  } else {
    panel.append(el("p", { class: "muted", text: "Nothing yet. In your words: less rice, warm vegetables, simple on weekdays." }));
  }
  const noteForm = el("form", { class: "row add-avoid" });
  const noteInput = el("input", { type: "text", name: "note", autocomplete: "off", maxlength: "200",
    placeholder: "less rice, no salads on weekdays...", "aria-label": "A note for every menu" });
  const noteAdd = el("button", { type: "submit", class: "primary", text: "Add" });
  noteForm.append(noteInput, noteAdd);
  noteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = noteInput.value.trim();
    if (!text) { say("Type a note first."); noteInput.focus(); return; }
    noteChange({ add: text }, noteAdd);
  });
  panel.append(noteForm);
  // the rest the planner reads about them: their ratings both ways, what is resting, the kitchen
  const steer = [];
  if ((prefs.favourites || []).length) {
    steer.push(el("p", {}, el("b", { text: "Asked for more often: " }), `${prefs.favourites.join(", ")}.`));
  }
  if ((prefs.never || []).length) {
    steer.push(el("p", {}, el("b", { text: "Never offered (rated 1 or 2): " }), `${prefs.never.join(", ")}.`));
  }
  if ((prefs.resting || []).length) {
    // a few by name: a dozen in a row was a wall nobody would read
    const rest = prefs.resting;
    const more = rest.length > 4 ? `, and ${rest.length - 4} more` : "";
    steer.push(el("p", { class: "muted", text: `Resting for now, eaten lately: ${rest.slice(0, 4).join(", ")}${more}.` }));
  }
  if ((prefs.kit || []).length) {
    steer.push(el("p", { class: "muted", text: `Your kitchen: ${prefs.kit.join(", ")}. /kit in the bot changes it.` }));
  }
  if (steer.length) {
    panel.append(el("h4", { text: "What else steers your menus" }), ...steer,
      el("p", { class: "muted", text: "A rating changes it: rate a dish on its card, or in Recipes." }));
  }
  // what you always have at home stays off your shopping list until it runs out
  const pantry = prefs.pantry || [];
  if (pantry.length) {
    panel.append(el("h4", { text: "Always at home (off your shopping list)" }));
    panel.append(chipList(pantry, "Foods always at home", (term, b) => change({ list: "pantry", remove: [term] }, b)));
  }
  if (foodsSaid) {
    say(foodsSaid);
    foodsSaid = null;
  }
  return panel;
}

// A whole new menu from the model, the page's own /cook replace. It runs in the background (on
// the person's own build lane, usually 2-4 minutes); the current menu stays in force until the new
// one is, and the tab looks again every 20 s until then. The model is the only thing that can
// write a new dish, and it is slow: the page says so rather than spinning.
export let buildPoll = null;
// the build this page started or saw running: how it ended (data.built) is said here once it has,
// rather than only in Telegram while the same menu is redrawn without a word
export let buildWatched = null;
export function rebuildPanel(data) {
  clearTimeout(buildPoll);
  if (data.building && data.building.kind === "dishes") {
    // new dishes for one meal, not a menu: they appear in that meal's Swap, never in Telegram.
    // Watched like a menu build, so the line goes and Build comes back once they are in
    const line = el("p", { class: "muted", role: "status", text: `Writing ${data.building.what}: usually 1-2 minutes. A new menu can be built once they are in.` });
    watchBuild(line);
    return line;
  }
  if (data.building) {
    buildWatched = data.building.id;
    const box = el("div", { class: "ask-box", role: "status" });
    const mins = data.building.minutes;
    box.append(el("p", {}, el("b", { text: "Building: " }),
      `${data.building.what}. Usually 2-4 minutes${mins ? ` (${mins} so far)` : ""}. The current menu stays until the new one is ready, and it comes to Telegram too.`));
    watchBuild(box);
    return box;
  }
  const panel = el("details", { class: "foods rebuild" });
  panel.append(el("summary", { text: data.menu ? "Build a new menu" : "Build a menu" }));
  const form = el("form", { class: "row add-avoid" });
  const input = el("input", { type: "text", name: "note", maxlength: "200", autocomplete: "off",
    placeholder: "Optional: more fish, no rice...", "aria-label": "What should change (optional)" });
  const go = el("button", { type: "submit", class: "primary", text: "Build" });
  const days = data.rebuild_days || [];
  const which = days.length ? el("select", { name: "only_day", "aria-label": "Which day" },
    el("option", { value: "", text: "Every day left" }),
    ...days.map((d) => el("option", { value: d.day, text: `Only ${d.label}` }))) : null;
  if (which) form.append(which);
  form.append(input, go);
  const said = el("p", { class: "plate-said", role: "status" });
  said.hidden = true;
  if (data.built && data.built.said && data.built.id === buildWatched) {
    said.textContent = data.built.said;
    said.hidden = false;
    panel.open = true;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    go.disabled = true;
    try {
      const body = { note: input.value.trim(), day: data.menu ? data.menu.segment_start : "" };
      if (which && which.value) body.only_day = which.value;
      const started = await api("api/menu/rebuild", { body });
      buildWatched = started.id;
      await renderMenu({ keepPlace: true, stay: true });
    } catch (e) {
      go.disabled = false;
      said.textContent = e.message;
      said.hidden = false;
    }
  });
  panel.append(el("p", { class: "muted", text: "Every dish written afresh by the model: usually 2-4 minutes. To change one dish, Swap it on its card instead - that is instant." }), form, said);
  return panel;
}

// The look while a build runs. Only its banner is drawn again: the whole tab every 20 s shut every
// open card, closed the swap areas and emptied a half-typed box, for minutes on end. Once the build
// is over the tab is drawn once, in place. A dropped answer arms the next look, a little later each
// time, so a dropped response cannot leave the page waiting indefinitely.
export function watchBuild(box, wait = 20000) {
  buildPoll = setTimeout(async () => {
    if (!box.isConnected || !$("#menu")?.classList.contains("on")) return;
    let data;
    try {
      data = await api("api/menu");
    } catch (e) {
      if (box.isConnected) watchBuild(box, Math.min(wait * 2, 120000));
      return;
    }
    if (!box.isConnected) return;  // the tab was drawn meanwhile, with a banner and look of its own
    if (data.building) box.replaceWith(rebuildPanel(data));
    else redrawInPlace("menu").catch(() => watchBuild(box, Math.min(wait * 2, 120000)));
  }, wait);
}

// One food inside a dish, changed for another playing the same part (a pulse for a pulse, a
// protein for a protein): the grams worked out in code, a new dish written, the days solved.
export function fillSubst(area, got, everyDay, said) {
  area.replaceChildren();
  if (!got.options.length) {
    area.append(el("p", { class: "muted", text: `Nothing you can have plays the same part as ${got.food.name} (${got.role}).` }));
    return;
  }
  area.append(el("p", { class: "muted", text: `Instead of ${got.food.name} (${got.role}), best first. The day resizes to stay on target.` }));
  const list = el("ul", { class: "items swap-list" });
  for (const o of got.options) {
    const delta = o.kcal_delta === 0 ? "same kcal" : `${o.kcal_delta > 0 ? "+" : ""}${o.kcal_delta} kcal`;
    const b = el("button", { type: "button" },
      el("span", {}, o.name, el("span", { class: "muted", text: `${Math.round(o.grams)} g${o.why ? " · " + o.why : ""}` })),
      el("span", { text: delta }));
    b.setAttribute("aria-label", `Use ${o.name} instead of ${got.food.name}`);
    b.addEventListener("click", async () => {
      for (const x of list.querySelectorAll("button")) x.disabled = true;
      let done;
      try {
        done = await api("api/menu/swap-food", { body: { day: got.day, slot: got.slot, food: got.food.code, instead: o.code, every_day: everyDay() } });
      } catch (e) {
        for (const x of list.querySelectorAll("button")) x.disabled = false;
        if (e.status >= 400 && e.status < 500) {
          said.textContent = e.message;
          said.hidden = false;
        } else {
          fail({ message: e.message, retry: () => b.click() });
        }
        return;
      }
      swapMade(got.day, got.slot);
      menuSaid = { anchor: `d-${got.day}-${got.slot}`, text: done.said };
      await showChange(done.said);
    });
    list.append(el("li", {}, b));
  }
  area.append(list);
  if (got.together) area.append(el("p", { class: "muted", text: `${got.together}'s plate changes too.` }));
}

// Less, More or Skip on one meal's own card: the same solve as the day's panel, with this plate
// moved and the rest of the day making room. None on a box cooked for the week (it is eaten as it
// was cooked), on a meal of today the clock has passed, or on the only plate left that could move
// (it carries the day whatever it is asked): the server says which (plates.one_tap). That last
// one still has As planned when it is set to something else (plates.resettable), the way back a
// refusal names. Today's meals behind the clock are sent as eaten, so a tap on tonight's dinner
// holds the breakfast already eaten - the day's panel asks with its ticks instead.
export function sizeRow(day, slot) {
  const meal = (day.plates.meals || []).find((m) => m.slot === slot.slot);
  if (!meal || (!meal.sizable && !meal.resettable)) return null;
  const now = (day.plates.levels || {})[slot.slot] || "planned";
  const row = el("div", { class: "row size-row", role: "group", "aria-label": `${slotLabel(slot.slot)} size` });
  row.append(el("span", { class: "muted", text: "This plate:" }));
  const choices = meal.sizable ? [["less", "Less"], ["more", "More"], ["skip", "Skip"]] : [];
  if (meal.resettable) choices.push(["planned", "As planned"]);
  const eaten = (day.plates.meals || []).filter((m) => m.past).map((m) => m.slot);
  for (const [level, word] of choices) {
    const b = el("button", { type: "button", text: word, "aria-pressed": String(now === level) });
    if (now === level) b.classList.add("on");
    b.addEventListener("click", async () => {
      for (const x of row.querySelectorAll("button")) x.disabled = true;
      let got;
      try {
        got = await api("api/menu/plates", { body: { day: day.day, levels: { ...day.plates.levels, [slot.slot]: level }, eaten } });
      } catch (e) {
        for (const x of row.querySelectorAll("button")) x.disabled = false;
        if (e.status >= 400 && e.status < 500) {
          row.after(el("p", { class: "plate-said", role: "status", text: e.message }));
        } else {
          fail({ message: e.message, retry: () => b.click() });
        }
        return;
      }
      // a plate left out has no card to say it under: the day's heading says it instead
      menuSaid = { anchor: level === "skip" ? `d-${day.day}` : `d-${day.day}-${slot.slot}`, text: got.said };
      await showChange(got.said);
    });
    row.append(b);
  }
  // on a day cooked together one tap moves both plates: said before it, as the day's panel does
  const other = day.plates.together;
  if (other) row.append(el("span", { class: "muted", text: `Cooked with ${other}: ${other}'s plates follow.` }));
  return row;
}

// Swap one dish for one already eaten: the dishes on offer are this person's own, with nothing
// they avoid and none rated "never again", best first; the day is solved again in code. A dish
// on several days (one pot for the week) is swapped on all of them unless one day is chosen.
export function swapControl(dayIso, slot) {
  const wrap = el("div", { class: "swap" });
  const open = el("button", { type: "button", class: "link", text: "Swap this dish", "aria-expanded": "false" });
  const area = el("div", { class: "swap-area" });
  area.hidden = true;
  const load = async () => {
    open.disabled = true;
    area.replaceChildren(el("p", { class: "muted", text: "Finding dishes..." }));
    area.hidden = false;
    open.setAttribute("aria-expanded", "true");
    try {
      const got = await api(`api/menu/swap?day=${dayIso}&slot=${slot.slot}`);
      fillSwap(area, got, slot);
    } catch (e) {
      area.replaceChildren(el("p", { class: "muted", text: e.message }));
      // a meal that can no longer be swapped says why here, and is not opened on every redraw
      if (e.status >= 400 && e.status < 500) newAsks.delete(askedFor(dayIso, slot.slot));
    } finally {
      open.disabled = false;
    }
  };
  open.addEventListener("click", () => {
    if (!area.hidden) {
      area.hidden = true;
      open.setAttribute("aria-expanded", "false");
      // closed after it said what came of an ask: read, so no longer opened by every redraw. An
      // ask still being written stays, to be said when it is
      const read = newAsks.get(area.dataset.said);
      if (read && read.said) newAsks.delete(area.dataset.said);
      return;
    }
    load();
  });
  // new dishes asked for this meal, and what came of it not yet read: open again after any
  // redraw, so it is seen where it was asked for - and waited for there while it is written
  if (askedFor(dayIso, slot.slot)) load();
  wrap.append(open, area);
  return wrap;
}

export function fillSwap(area, got, slot) {
  area.replaceChildren();
  area.dataset.look = String(Number(area.dataset.look || 0) + 1);
  // all of its days or just this one, as last picked for the meal (swapDays): a refill (the ask,
  // the look while new dishes are written) or a redraw drew "All 3 days" again, and a tap then
  // swapped days she meant to keep. A meal not picked for, with an ask out, takes the choice the
  // ask was sent with
  const meal = `${got.day}|${got.slot}`;
  const asked = askedFor(got.day, got.slot, got.days);
  const askOut = asked ? newAsks.get(asked) : null;
  let everyDay = swapDays.has(meal) ? swapDays.get(meal) : !askOut || askOut.everyDay;
  if (got.days.length > 1) {
    const run = daysWords(got.days, false);
    const row = el("div", { class: "row", role: "group", "aria-label": "Which days" });
    const all = el("button", { type: "button", class: everyDay ? "on" : "", text: `All ${got.days.length} days (${run})` });
    const one = el("button", { type: "button", class: everyDay ? "" : "on", text: `Just ${weekday(got.day)}` });
    const pick = (b, value) => {
      everyDay = value;
      swapDays.set(meal, value);
      for (const x of [all, one]) {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-pressed", String(x === b));
      }
    };
    all.setAttribute("aria-pressed", String(everyDay));
    one.setAttribute("aria-pressed", String(!everyDay));
    all.addEventListener("click", () => pick(all, true));
    one.addEventListener("click", () => pick(one, false));
    row.append(all, one);
    area.append(row);
  }
  if (got.together) {
    area.append(el("p", { class: "muted", text: `You cook this with ${got.together}: ${got.together}'s plate changes too, sized to their day.` }));
  }
  const said = el("p", { class: "plate-said", role: "status" });
  said.hidden = true;
  if (!got.candidates.length) {
    area.append(el("p", { class: "muted", text: "Nothing you have eaten before fits this meal yet: change one food in it, or ask for new dishes." }));
  }
  // what came of an ask for new dishes for this meal, in whichever area for the meal is open when
  // the dishes are written - and said when nothing came back too, never left to silence. Kept with
  // the ask and said again in any area a redraw draws in this one's place, until it is closed
  if (askOut && !got.building) {
    askOut.said = askOut.said || got.new_said || "The ask for new dishes did not finish: ask again, or pick one you have had.";
    area.append(el("p", { class: "plate-said", role: "status", text: askOut.said }));
  }
  area.dataset.said = askOut && askOut.said ? asked : "";
  const list = el("ul", { class: "items swap-list" });
  for (const c of got.candidates) {
    // why each is offered, in a few words: "rated 5; last had 3 weeks ago"
    const rated = c.why ? ` · ${c.why}` : (c.new ? " · new" : "") + (c.score ? ` · rated ${c.score}` : "") + (c.resting ? " · had lately" : "");
    // and what in the kitchen it cooks from: a pack the week already has open
    const kitchen = c.kitchen ? ` · ${c.kitchen}` : "";
    const b = el("button", { type: "button" },
      el("span", {}, c.name, el("span", { class: "muted", text: `P ${num(c.protein_g)} g · ${c.prep_minutes} min${rated}${kitchen}` })),
      el("span", { text: `${fmtInt(c.kcal)} kcal` }));
    b.setAttribute("aria-label", `Swap in ${c.name}`);
    b.addEventListener("click", async () => {
      for (const x of list.querySelectorAll("button")) x.disabled = true;
      let done;
      try {
        done = await api("api/menu/swap", { body: { day: got.day, slot: got.slot, recipe_id: c.recipe_id, every_day: everyDay } });
      } catch (e) {
        for (const x of list.querySelectorAll("button")) x.disabled = false;
        if (e.status >= 400 && e.status < 500) {
          said.textContent = e.message;
          said.hidden = false;
        } else {
          fail({ message: e.message, retry: () => b.click() });
        }
        return;
      }
      swapMade(got.day, got.slot, got.days);
      menuSaid = { anchor: `d-${got.day}-${got.slot}`, text: done.said };
      await showChange(done.said);
    });
    list.append(el("li", {}, b));
  }
  if (got.candidates.length) {
    area.append(el("p", { class: "muted", text: "Dishes you have had, best first. The rest of the day resizes to stay on target." }));
    area.append(list);
  }
  // two or three dishes written for this meal by the model, when none of these will do
  const fresh = el("div", { class: "row" });
  // looked at again until the dishes are written, and the area drawn again only then - a refill
  // every 15 s shut a food list opened meanwhile. A dropped answer is looked for again, later:
  // a dropped response cannot leave the area waiting indefinitely. One look per area: a
  // later fill of it (the look's own, or the area closed and opened again) ends this one.
  // `draw`: drawn again on the first answer, written or not - after an ask whose own redraw was
  // dropped, the area still showed the Ask button, and nothing else on the page looked again
  const mine = area.dataset.look;
  const gone = () => !area.isConnected || area.hidden || area.dataset.look !== mine;
  const look = (wait, draw = false) => setTimeout(async () => {
    if (gone()) return;
    let again;
    try {
      again = await api(`api/menu/swap?day=${got.day}&slot=${got.slot}`);
    } catch (e) { look(Math.min(wait * 2, 120000), draw); return; }
    if (gone()) return;
    if (again.building && !draw) look(15000);
    else fillSwap(area, again, slot);
  }, wait);
  const building = got.building;
  if (building && building.kind === "dishes" && building.day === got.day && building.slot === got.slot) {
    fresh.append(el("p", { class: "muted", role: "status", text: `Writing new dishes for this meal: usually 1-2 minutes. They appear here.` }));
    look(15000);
  } else if (building) {
    // something else is being built: one build at a time, and it is not this meal's dishes
    const what = building.kind === "dishes" ? `${building.what} for ${dayName(building.day)}` : "a new menu";
    fresh.append(el("p", { class: "muted", role: "status", text: `Writing ${what} first: ask for new dishes here once that is in.` }));
    look(15000);
  } else {
    const ask = el("button", { type: "button", text: "Ask for new dishes" });
    ask.addEventListener("click", async () => {
      ask.disabled = true;
      try {
        await api("api/menu/new-dish", { body: { day: got.day, slot: got.slot, every_day: everyDay } });
      } catch (e) {
        ask.disabled = false;
        said.textContent = e.message;
        said.hidden = false;
        return;
      }
      // asked: remembered past any redraw until what came of it has been said and read
      newAsks.set(`${got.day}|${got.slot}`, { everyDay, said: null });
      try {
        fillSwap(area, await api(`api/menu/swap?day=${got.day}&slot=${got.slot}`), slot);
      } catch (e) {
        said.textContent = `Asked (usually 1-2 minutes), but this could not be drawn again: ${e.message} Looking again shortly.`;
        said.hidden = false;
        look(15000, true);
      }
    });
    fresh.append(ask, el("span", { class: "muted", text: "written for this meal, usually 1-2 min" }));
  }
  area.append(fresh);
  // the seasonings by their whole name ("Salt", "Water, distilled"), not by how a name begins:
  // watercress, watermelon and salted peanuts are foods a person can refuse or change
  const foods = (slot.ingredients || []).filter((i) => !/^(salt|black pepper|olive oil|rapeseed oil|water)\s*(,|$)/i.test(i.name));
  // one food inside the dish changed for another playing the same part: a new dish in code,
  // the days solved again (no model)
  if (foods.length) {
    const row = el("div", { class: "row avoid-row", role: "group", "aria-label": "Change one food in it" });
    row.append(el("span", { class: "muted", text: "Change one food:" }));
    const pickArea = el("div", { class: "subst-area" });
    pickArea.hidden = true;
    for (const ing of foods) {
      const short = ing.name.split(",")[0];
      const b = el("button", { type: "button", text: short, "aria-expanded": "false" });
      b.addEventListener("click", async () => {
        const opening = b.getAttribute("aria-expanded") !== "true";
        for (const x of row.querySelectorAll("button")) x.setAttribute("aria-expanded", "false");
        if (!opening) { pickArea.hidden = true; return; }
        b.setAttribute("aria-expanded", "true");
        pickArea.hidden = false;
        pickArea.replaceChildren(el("p", { class: "muted", text: `Finding what could stand in for ${short}...` }));
        try {
          const got3 = await api(`api/menu/swap-food?day=${got.day}&slot=${got.slot}&food=${encodeURIComponent(ing.code)}`);
          fillSubst(pickArea, got3, () => everyDay, said);
        } catch (e) {
          pickArea.replaceChildren(el("p", { class: "muted", text: e.message }));
        }
      });
      row.append(b);
    }
    area.append(row, pickArea);
  }
  // never this food again, from the dish that made you think of it: two taps, not a command -
  // and by the food itself, so "Eggs" never also keeps egg whites off
  if (foods.length) {
    const row = el("div", { class: "row avoid-row", role: "group", "aria-label": "Avoid a food in it" });
    row.append(el("span", { class: "muted", text: "Never again:" }));
    for (const ing of foods) {
      const short = ing.name.split(",")[0];
      const b = el("button", { type: "button", text: short });
      b.addEventListener("click", async () => {
        b.disabled = true;
        let got2;
        try {
          got2 = await api("api/prefs/food", { body: { food: ing.code, refused: true } });
        } catch (e) {
          b.disabled = false;
          if (e.status >= 400 && e.status < 500) {
            said.textContent = e.message;
            said.hidden = false;
          } else {
            fail({ message: e.message, retry: () => b.click() });
          }
          return;
        }
        // said in the foods panel, and the redraw lands there (renderMenu's own anchor)
        foodsSaid = got2.said;
        await showChange(got2.said);
      });
      row.append(b);
    }
    area.append(row);
  }
  area.append(said);
}

// treats ... flick a switch, whether it's shared menu or not shared, and we keep both menus"):
// the switch - only while the two of them share weekend menus at all - and this person's own
// treats. Both versions are built ahead, so a flip changes which is in force and the tab redraws.
export const WEEKEND_MODES = [["together", "Cooked together"], ["apart", "Each our own"]];
export const TASTE_WORDS = [["sweet", "Sweet"], ["savoury", "Savoury"], ["both", "Both"]];
// what the last flip said, carried across the redraw that replaces the panel it came from
export let weekendSaid = null;

export function weekendPanel(w) {
  const box = el("details", { class: "weekend", id: "weekend", "aria-label": "The weekend" });
  box.open = weekendSaid !== null;
  const modeWord = (mode) => mode === "together" ? "cooked together" : mode === "apart" ? "each our own" : "cooking not set";
  const tasteWord = (taste) => taste === "sweet" ? "sweet treats" : taste === "savoury" ? "savoury treats" : taste === "both" ? "sweet and savoury treats" : "treats not set";
  const summary = el("summary", { text: `Weekend ${dayName(w.start)} and ${dayName(w.end)}: ${modeWord(w.switch?.mode)}, ${tasteWord(w.taste)}` });
  box.append(summary);
  const said = el("p", { class: "muted", role: "status" });
  const choose = (label, options, current, send) => {
    const row = el("div", { class: "row", role: "group", "aria-label": label });
    const buttons = [];
    for (const [value, text] of options) {
      const b = el("button", { type: "button", text, class: value === current ? "on" : "" });
      b.setAttribute("aria-pressed", String(value === current));
      b.addEventListener("click", async () => {
        if (b.classList.contains("on")) return;
        buttons.forEach((x) => { x.disabled = true; });
        try {
          said.textContent = await send(value);
          buttons.forEach((x) => {
            x.classList.toggle("on", x === b);
            x.setAttribute("aria-pressed", String(x === b));
          });
          if (label === "How the weekend is cooked") w.switch.mode = value;
          else w.taste = value;
          summary.textContent = `Weekend ${dayName(w.start)} and ${dayName(w.end)}: ${modeWord(w.switch?.mode)}, ${tasteWord(w.taste)}`;
        } catch (e) {
          // a refusal is an answer, said here; only a fault is worth trying again
          if (e.status >= 400 && e.status < 500) said.textContent = e.message;
          else fail({ message: e.message, retry: () => b.click() });
        } finally {
          buttons.forEach((x) => { x.disabled = false; });
        }
      });
      buttons.push(b);
      row.append(b);
    }
    return row;
  };
  if (w.switch) {
    box.append(choose("How the weekend is cooked", WEEKEND_MODES, w.switch.mode, async (mode) => {
      const got = await api("api/weekend/mode", { body: { mode } });
      // the menus in force changed: the tab is drawn again from them, and says what happened -
      // a redraw that fails is said on its own, and never flips the switch a second time
      weekendSaid = got.said;
      await showChange(got.said);
      return got.said;
    }));
    said.textContent = [w.switch.note, w.switch.by].filter(Boolean).join(" ");
  }
  box.append(el("p", { class: "muted", text: "Your treats at the weekend:" }));
  box.append(choose("Your weekend treats", TASTE_WORDS, w.taste, async (taste) => {
    const got = await api("api/weekend/taste", { body: { taste } });
    return got.said;
  }));
  if (weekendSaid) {
    said.textContent = weekendSaid;
    weekendSaid = null;
  }
  box.append(said);
  return box;
}

// and want more smash burger and French toast, I can achieve that"). One slider per meal as
// planned, skipped ones included so they can come back; a box batch-cooked for the week has no
// slider (it is eaten as it was cooked), and today's meals can be ticked as already eaten, which
// holds them as they are. Apply solves the day again and the tab redraws from what was stored.
export const PLATE_LEVELS = ["skip", "less", "planned", "more", "lots"];
export const PLATE_WORDS = { skip: "Skip", less: "Less", planned: "As planned", more: "More", lots: "Lots more" };
// the result of the last Apply, carried across the redraw that replaces the panel it came from
export let plateSaid = null;

export function platesPanel(day) {
  const panel = el("details", { class: "plates", id: `plates-${day.day}` });
  const changed = Object.values(day.plates.levels).some((v) => v !== "planned");
  panel.append(el("summary", { text: changed ? "Your plates for this day (changed)" : "Change this day's plates" }));
  const chosen = { ...day.plates.levels };
  const ranges = {};
  for (const meal of day.plates.meals) {
    const name = el("span", { class: "plate-name", text: `${slotLabel(meal.slot)}: ${meal.name}` });
    if (meal.boxed) {
      panel.append(el("div", { class: "plate-row" }, name,
        el("span", { class: "muted boxed", text: "From the box, as cooked" })));
      continue;
    }
    const level = chosen[meal.slot] || "planned";
    const word = el("span", { class: "muted", text: PLATE_WORDS[level] });
    const range = el("input", {
      type: "range", min: "0", max: "4", step: "1", value: String(PLATE_LEVELS.indexOf(level)),
      "aria-label": `${slotLabel(meal.slot)}: ${meal.name}`,
    });
    range.setAttribute("aria-valuetext", PLATE_WORDS[level]);
    range.addEventListener("input", () => {
      const next = PLATE_LEVELS[Number(range.value)];
      chosen[meal.slot] = next;
      word.textContent = PLATE_WORDS[next];
      range.setAttribute("aria-valuetext", PLATE_WORDS[next]);
      previewSoon();
    });
    ranges[meal.slot] = range;
    panel.append(el("div", { class: "plate-row" }, name, range, word));
  }
  // what was eaten today is a fact, not a plan: ticked, it is held at the size it was eaten and
  // the rest of the day makes room around it (a resize at 11:42 re-sized a breakfast already eaten)
  const eaten = new Set();
  if (day.plates.today) {
    const row = el("div", { class: "eaten" }, el("span", { class: "muted", text: "Already eaten:" }));
    for (const meal of day.plates.meals) {
      if (meal.boxed || (chosen[meal.slot] || "planned") === "skip") continue;
      const tick = el("input", { type: "checkbox" });
      tick.addEventListener("change", () => {
        if (tick.checked) eaten.add(meal.slot); else eaten.delete(meal.slot);
        ranges[meal.slot].disabled = tick.checked;
        previewSoon();
      });
      row.append(el("label", {}, tick, slotLabel(meal.slot)));
    }
    if (row.children.length > 1) panel.append(row);
  }
  // on a day cooked together a change moves both plates: said before Apply, not discovered after
  const other = day.plates.together;
  panel.append(el("p", { class: "muted",
    text: "The other meals make room, so the day stays on your calories, protein, fat and fibre. "
      + (other
        ? `You cook this day with ${other}, so ${other}'s plates follow: the same dishes, sized to ${other}'s own day.`
        : "Only your plates change.") }));
  if (day.plates.shortfall) {
    panel.append(el("p", { class: "shop-note", text: `This day is short: ${day.plates.shortfall}.` }));
  }
  const said = el("p", { class: "plate-said", role: "status" });
  said.hidden = true;
  const say = (text) => { said.textContent = text; said.hidden = false; };
  // what Apply would do, worked out as the sliders move and stored nowhere: "Dinner 620 to 0,
  // lunch 540 to 690 kcal" before the tap, not after
  const preview = el("p", { class: "muted plate-preview", role: "status", "aria-live": "polite" });
  preview.hidden = true;
  let previewTimer = null;
  let previewAsked = 0;
  function previewSoon() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const asked = ++previewAsked;
      try {
        const got = await api("api/menu/plates", { body: { day: day.day, levels: chosen, eaten: [...eaten], preview: true } });
        if (asked !== previewAsked) return;  // a later move has asked again
        preview.textContent = `If you apply: ${got.said}`;
      } catch (e) {
        if (asked !== previewAsked) return;
        preview.textContent = e.status >= 400 && e.status < 500 ? e.message : "";
      }
      preview.hidden = !preview.textContent;
    }, 350);
  }
  const send = async (levels, button) => {
    button.disabled = true;
    let got;
    try {
      got = await api("api/menu/plates", { body: { day: day.day, levels, eaten: [...eaten] } });
    } catch (e) {
      button.disabled = false;
      if (e.status >= 400 && e.status < 500) {
        // a refusal is an answer about these plates, not a fault: said here, with nothing to
        // retry - sending the same plates again gets the same answer
        say(e.message);
        return;
      }
      fail({ message: e.message, retry: () => button.click() });
      return;
    }
    button.disabled = false;
    plateSaid = { day: day.day, text: got.said };
    try {
      await renderMenu({ keepPlace: true });
    } catch (e) {
      // stored already: only the redraw failed, so the retry redraws and never sends it again
      plateSaid = null;
      say(got.said);
      fail({ message: e.message, retry: () => renderMenu() });
    }
  };
  const apply = el("button", { type: "button", class: "primary", text: "Apply" });
  apply.addEventListener("click", () => send(chosen, apply));
  const back = el("button", { type: "button", class: "link", text: "Back to the plan" });
  back.addEventListener("click", () => send({}, back));
  panel.append(preview, el("div", { class: "row" }, apply, changed ? back : null));
  if (plateSaid && plateSaid.day === day.day) {
    say(plateSaid.text);
    panel.open = true;
    plateSaid = null;
  }
  panel.append(said);
  return panel;
}
