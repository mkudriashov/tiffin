import { $, api, card, counted, dayName, decimal, el, fail, kg, num, plural, state } from "./helpers.js";
import { drawTicket, outdated } from "./more.js";
import { per100Toggle, wirePlanDetails } from "./menu.js";
import { show } from "./app.js";

/* ---------------------------------------------------------------- cook overlay */
// hands-free: full screen, one step at a time, tap either half to advance. Opened from a
// Today meal, a Menu slot or a Batch pot - client-side only, reading steps already on the
// page, so there is nothing to write and nothing to fail (no per-step ledger row, spec §4).
export let cook = null;
export let cookLock = null;

// where each dish was left, so Close to check an amount and back again is not step 1 again
export const cookPlace = new Map();
export let cookOpener = null;

export function cookRender() {
  $("#cook-progress").textContent = `Step ${cook.index + 1} of ${cook.steps.length}`;
  $("#cook-title").textContent = cook.name;
  $("#cook-step").textContent = cook.steps[cook.index];
  $("#cook-left").disabled = cook.index === 0;
  $("#cook-right").textContent = cook.index + 1 >= cook.steps.length ? "Done" : "Next ›";
  cookPlace.set(cook.name, cook.index);
}

export function wakeUp() {
  // feature-detected: plenty of browsers have no Wake Lock API at all, and even where it
  // exists the request can be refused (battery saver, a backgrounded tab) - cooking works
  // either way, just with a screen that can lock between steps if it is refused
  if (cook && "wakeLock" in navigator) {
    navigator.wakeLock.request("screen").then((lock) => { cookLock = lock; }).catch(() => {});
  }
}

export function openCook(name, steps, ingredients) {
  if (!steps || !steps.length) return;
  cookOpener = document.activeElement;
  const at = Math.min(cookPlace.get(name) || 0, steps.length - 1);
  cook = { name, steps, index: at };
  // Keep ingredient amounts visible without resetting the current cooking step.
  const list = $("#cook-ing-list");
  list.replaceChildren(...(ingredients || []).map((t) => el("li", { text: t })));
  $("#cook-ings").hidden = !(ingredients || []).length;
  // folded on a phone, where the step needs the room; open beside the step on a wide screen
  $("#cook-ings").open = window.matchMedia("(min-width: 900px)").matches;
  cookRender();
  $("#cook-overlay").hidden = false;
  $("#cook-right").focus();
  wakeUp();
}

export function closeCook() {
  $("#cook-overlay").hidden = true;
  cook = null;
  if (cookLock) {
    cookLock.release().catch(() => {});
    cookLock = null;
  }
  if (cookOpener && cookOpener.focus) cookOpener.focus();
}

export function cookNext() {
  if (!cook) return;
  if (cook.index + 1 >= cook.steps.length) {
    cookPlace.delete(cook.name); // done: the next time it is cooked it starts at the top
    closeCook();
    return;
  }
  cook.index += 1;
  cookRender();
}

export function cookPrev() {
  if (!cook || cook.index === 0) return;
  cook.index -= 1;
  cookRender();
}

// How one pot's ingredient divides between its boxes. Uniform is the ordinary case and reads
// "200 g a box"; a dish made 0.8x for Monday and 0.7x for Wednesday is two boxes of ONE dish
// that do not match, and naming both is the only honest answer (batch.DishShare.box_grams).
export function splitText(amount) {
  if (!amount.boxes || amount.boxes < 2) return "";
  if (amount.uniform || !(amount.box_texts || []).length) return `· ${amount.per_box_text} a box`;
  return `· ${amount.box_texts.join(" + ")}`;
}

// How a COOKED weight divides between boxes. Not `cooked / boxes`: a dish made 1.2x for Thursday
// and 0.8x for Friday puts 480 g and 320 g of raw chicken in two boxes, and what comes out of the
// pan divides the same way it went in. An even split there is wrong in both boxes at once, which
// is the exact failure `Amount.uniform` exists to prevent (food/batch.py).
export function cookedSplit(amount, cookedGrams) {
  const boxes = amount.boxes || 1;
  if (boxes < 2) return "";
  const shares = amount.box_grams || [];
  if (amount.uniform || shares.length !== boxes || !amount.grams) {
    return `${weighJs(cookedGrams / boxes)} a box`;
  }
  return shares.map((g) => weighJs((cookedGrams * g) / amount.grams)).join(" + ");
}

// mirrors batch.weigh() in Python: kitchen units, because nobody weighs out 1620 g of chicken.
// The server renders every figure it sends; this is for the ones worked out as they are typed.
export function weighJs(grams) {
  if (grams >= 1000) return `${(grams / 1000).toFixed(1)} kg`.replace(".0 kg", " kg");
  if (grams >= 10) return `${Math.round(grams)} g`;
  return `${grams.toFixed(1)} g`.replace(".0 g", " g");
}

// "I have 600 raw turkey that I put into the airfryer -> it then goes into 400 g cooked turkey"
// the only number he has that Tiffin cannot know, so he types that one and nothing else.
// The split is what comes back, and the weighing is kept so the next cook can offer it up front.
export function cookedCalculator(dish) {
  const row = el("div", { class: "row cooked-calc" });
  const pick = el("select", { "aria-label": "The food you weighed after cooking" });
  for (const i of dish.pot) {
    if (!i.code) continue;
    pick.append(el("option", { value: i.code }, `${i.name} - ${i.text} raw`));
  }
  if (!pick.children.length) return el("span", { hidden: "hidden" });
  const cooked = el("input", { type: "text", inputmode: "decimal", placeholder: "cooked g",
    "aria-label": "Cooked weight in grams" });
  const out = el("span", { class: "muted" });
  const save = el("button", { type: "button", text: "Split" });
  // never assumed: this fills the box so accepting it is one tap, and it writes nothing by
  // itself - a weighing that did not happen must not become the number the next cook is told.
  const again = el("button", { type: "button", class: "link", text: "Use last time" });

  const chosen = () => dish.pot.find((i) => i.code === pick.value) || dish.pot[0];
  const show = () => {
    // "3,5" is three and a half on a British phone keyboard's decimal key, not 35 - and "1,620"
    // is the pot of chicken, not a gram and a half
    const grams = Number(decimal(cooked.value));
    const i = chosen();
    again.hidden = !(i && i.yield && i.yield.pct);
    if (!grams || !i) { out.textContent = ""; return null; }
    const boxes = i.boxes || dish.boxes || 1;
    const split = cookedSplit(i, grams);
    out.textContent = split
      ? `${split}${i.uniform === false ? "" : ` across ${boxes}`}`
      : `${weighJs(grams)} in the one box`;
    return { grams, i };
  };
  again.addEventListener("click", () => {
    const i = chosen();
    if (!i || !i.yield || !i.yield.pct) return;
    cooked.value = Math.round((i.grams * i.yield.pct) / 100);
    show();
    out.textContent += ` · last time's ${i.yield.pct}%, not weighed again`;
  });
  cooked.addEventListener("input", show);
  pick.addEventListener("change", () => {
    // a remembered weighing belongs to ONE food; carrying the last one's number across to the
    // next food in the list would be a number about turkey shown as a number about rice
    cooked.value = "";
    out.textContent = "";
    show();
  });
  save.addEventListener("click", async () => {
    const got = show();
    if (!got) { fail("Type what it weighed once it was cooked."); return; }
    try {
      const said = await api("api/batch/yield", { body: {
        recipe_id: dish.recipe_id,
        ingredient_code: got.i.code,
        raw_g: got.i.grams,
        cooked_g: got.grams,
      } });
      got.i.yield = { raw_g: got.i.grams, cooked_g: got.grams, pct: said.pct };
      out.textContent += ` · kept, ${said.pct}% of raw`;
      again.hidden = false;
    } catch (e) {
      // the split is already on the screen and is not wrong because the note failed to save
      fail({ message: e.message, retry: () => save.click() });
    }
  });
  row.append(el("label", { text: "Cooked weight" }), pick, cooked, save, again, out);
  show();
  return row;
}

// Show the per-100 g figures used to build the menu, alongside the pack size.
export function per100(i) {
  if (i.kcal_100 === null || i.kcal_100 === undefined) return "";
  // the four a label puts on the front of the pack, in the order a label puts them, so a tub in
  // a shop can be read straight across against this line. Fibre only where there is some: every
  // meat and dairy row would otherwise carry "fibre 0" and the eye has four numbers to find.
  const g = (v) => num(v, 1).replace(/\.0$/, "");
  const parts = [`${Math.round(i.kcal_100)} kcal`];
  if (i.protein_100 !== null && i.protein_100 !== undefined) parts.push(`P ${g(i.protein_100)}`);
  if (i.fat_100 !== null && i.fat_100 !== undefined) parts.push(`F ${g(i.fat_100)}`);
  if (i.carb_100 !== null && i.carb_100 !== undefined) parts.push(`C ${g(i.carb_100)}`);
  if (i.fibre_100) parts.push(`fibre ${g(i.fibre_100)}`);
  return `${parts.join(" · ")} per 100 g`;
}

/* ---------------------------------------------------------------- batch */
// the day the cook is asked to start from - "select since which day we cook, I have skipped
// a chip tap) so neither one snaps the view back to the whole segment.
export let batchFrom = null;

export function batchDays(startIso, endIso) {
  const days = [];
  let d = new Date(startIso + "T12:00:00");
  const last = new Date(endIso + "T12:00:00");
  while (d <= last) {
    days.push(d.toISOString().slice(0, 10));
    d = new Date(d.getTime() + 86400000);
  }
  return days;
}

export async function renderBatch() {
  const ticket = drawTicket("batch");
  const from = batchFrom || state.today;
  const data = await api(`api/batch?from=${from}`);
  if (outdated("batch", ticket)) return;
  const body = $("#batch-body");
  body.replaceChildren();
  if (!data.cook_day) {
    body.append(el("p", { class: "muted",
      text: "No menu to cook from yet. /cook builds one in Telegram, and /menu shows what is planned." }));
    return;
  }

  body.append(el("h2", { text: data.segment_start === data.segment_end
    ? `Cooking for ${dayName(data.segment_start)}`
    : `Cooking for ${dayName(data.segment_start)} to ${dayName(data.segment_end)}` }));
  if (data.refused_still_on_it && data.refused_still_on_it.length) {
    // the payload always carried it; the tab never said it
    body.append(el("p", { text: `This cook still has ${data.refused_still_on_it.join(", ")} in it, which you now avoid. /cook replace in Telegram rebuilds it without.` }));
  }

  // the day-chip row: which day the cook starts from, today pre-selected so opening the tab
  // costs zero taps on the ordinary Sunday cook - a day already past is greyed, not
  // removed, so the whole segment stays visible even once most of it is behind him
  const chips = el("div", { id: "from-day", class: "row" });
  for (const day of batchDays(data.segment_start, data.segment_end)) {
    const past = day < state.today;
    const btn = el("button", { type: "button", text: dayName(day), class: day === from ? "on" : "" });
    btn.setAttribute("aria-pressed", String(day === from));
    btn.disabled = past;
    if (past) btn.classList.add("muted");
    btn.addEventListener("click", () => {
      batchFrom = day;
      renderBatch().catch((e) => fail(e.message));
    });
    chips.append(btn);
  }
  body.append(chips);
  body.append(el("p", {
    text: `Cooking since ${dayName(from)}: ${plural(data.boxes, "box")}. About ${data.total_minutes} min.` }));

  // the pots side by side on a wide screen, one after another on a phone
  const pots = el("div", { class: "cook-dishes" });
  body.append(pots);
  for (const dish of data.dishes) {
    const box = el("div", { class: "slot" });
    box.append(el("h3", { text: dish.name }));
    box.append(el("p", { class: "muted",
      text: `${plural(dish.boxes, "box")}, ${dish.multiples}x the recipe (${dish.days.map(dayName).join(", ")})` }));
    if (dish.per_box_g) {
      box.append(el("p", { class: "muted", text: `About ${Math.round(dish.per_box_g)} g a box` }));
    }
    // Put ingredient amounts before the method so the cook can read them at the hob.
    if ((dish.pot || []).length) {
      box.append(el("h4", { text: `Weigh out${dish.boxes ? ` for ${plural(dish.boxes, "box")}` : ""}` }));
      const amounts = el("ul", { class: "items" });
      for (const i of dish.pot) {
        const whole = counted(i.grams, i.pack_g, i.pack_label);
        const intoPan = whole ? `${whole} (${i.text})` : i.text;
        amounts.append(el("li", {}, el("div", { class: "amount" },
          el("span", { text: i.name }),
          el("span", { class: "n" },
            el("b", { text: intoPan }),
            // the pot total is what goes in the pan; this is what goes in each box, which is
            // the number he is holding the phone for once the boxes are open on the counter
            el("span", { class: "muted", text: ` ${splitText(i)}` })))));
        // 600 g of raw turkey came out of the air fryer at 400 g last time: say so, and split
        // the cooked weight, because a box gets what came OUT of the pan, not what went in
        if (i.yield && i.yield.pct) {
          const expect = (i.grams * i.yield.pct) / 100;
          const split = cookedSplit(i, expect);
          amounts.append(el("li", {}, el("div", { class: "amount cooked" },
            el("span", { class: "muted",
              text: `cooked, about ${i.yield.pct}% of raw last time` }),
            el("span", { class: "n muted",
              text: `${weighJs(expect)}${split ? ` · ${split}` : ""}` }))));
        }
      }
      box.append(amounts);
      box.append(per100Toggle(dish.pot));
      box.append(cookedCalculator(dish));
    }
    if ((dish.method || []).length) {
      const ol = el("ol");
      for (const step of dish.method) ol.append(el("li", { text: step }));
      box.append(ol);
      // propped by the hob for the pot that made all the boxes, not just tonight's dinner -
      // same hands-free overlay as Today and Menu, opened from wherever the method is read
      const cookLink = el("button", { type: "button", class: "link", text: "Cook" });
      cookLink.addEventListener("click", () => openCook(dish.name, dish.method,
        (dish.pot || []).map((i) => `${i.name} ${i.text}`)));
      box.append(cookLink);
    }
    pots.append(box);
  }

  if (data.amounts.length) {
    // a read-only total, not a heading that clashed with the Cook buttons over rows that looked
    // tappable and did nothing
    body.append(el("h2", { text: "All pots together" }));
    const list = el("ul", { class: "items" });
    for (const a of data.amounts) {
      list.append(el("li", {}, el("div", { class: "amount" },
        el("span", { text: a.name }), el("span", { class: "muted", text: a.text }))));
    }
    body.append(list);
  }

  if (data.to_freeze.length) {
    body.append(el("h2", { text: "Freeze" }));
    for (const f of data.to_freeze) {
      body.append(el("p", { text: `${f.name}: ${f.days.map(dayName).join(", ")}` }));
    }
    body.append(el("p", { class: "muted", text: data.to_freeze[0].note }));
  }
  wirePlanDetails(body);
}
