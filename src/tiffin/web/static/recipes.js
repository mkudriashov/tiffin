import { $, addDays, api, card, counted, dayName, decimal, el, fail, fmtInt, plateGrams, state, unitWords } from "./helpers.js";
import { batchFrom, cook, openCook } from "./cook.js";
import { drawTicket, outdated } from "./more.js";
import { per100Toggle, wirePlanDetails } from "./menu.js";
import { todayShowing } from "./today.js";

/* ---------------------------------------------------------------- dishes */
// how far back the recipe book looks - persisted across a re-render the same way batchFrom and
// todayShowing are, so widening it does not snap straight back to the 30-day default. "All" is
// ten years, the API's own clamp: a recipe kept for another time stays in the book.
export let dishesRecent = 30;

// a dish from the book, opened to cook again: the ingredients for one portion or a few, the
// recipes - e.g. to cook smash burgers per portion"). Portions are the recipe's own servings -
// what the menu scaled each plate from - with whole buns, eggs and slices kept whole.
export const DISH_PORTIONS = [1, 2, 3, 4];

export function dishRecipe(dish) {
  const ingredients = dish.ingredients || [];
  const method = dish.method || [];
  if (!ingredients.length && !method.length) return null;
  const open = el("details", { class: "recipe" });
  open.open = true;
  open.append(el("summary", { text: "Recipe" }));
  const inner = el("div", {});
  let portions = 1;
  const grams = (ing) => plateGrams(ing, portions, dish.servings);
  const draw = () => {
    inner.replaceChildren();
    const picks = el("div", { class: "row" });
    for (const n of DISH_PORTIONS) {
      const b = el("button", { type: "button", text: n === 1 ? "1 portion" : `${n} portions`,
        class: n === portions ? "on" : "" });
      b.setAttribute("aria-pressed", n === portions ? "true" : "false");
      b.addEventListener("click", () => { portions = n; draw(); });
      picks.append(b);
    }
    inner.append(picks);
    const each = dish.per_portion;
    if (each) {
      inner.append(el("p", { class: "muted", text: `A portion: ${fmtInt(each.kcal)} kcal, `
        + `protein ${Math.round(each.protein_g)} g, fat ${Math.round(each.fat_g)} g, `
        + `carbs ${Math.round(each.carb_g)} g, fibre ${Math.round(each.fibre_g)} g` }));
    }
    if (ingredients.length) {
      inner.append(el("h4", { text: portions === 1 ? "Ingredients" : `Ingredients for ${portions}` }));
      for (const ing of ingredients) {
        const g = grams(ing);
        const p = el("p", { class: "muted", text: `${ing.name} - ${g} g` });
        const count = unitWords(ing, g) || (ing.pack_g ? counted(g, ing.pack_g, ing.pack_label) : "");
        if (count) p.append(`, ${count}`);
        inner.append(p);
      }
      inner.append(per100Toggle(ingredients));
    }
    if (method.length) {
      inner.append(el("h4", { text: "Method" }));
      for (const [i, step] of method.entries()) inner.append(el("p", { text: `${i + 1}. ${step}` }));
      const cookLink = el("button", { type: "button", class: "link", text: "Cook" });
      cookLink.addEventListener("click", () => openCook(
        portions === 1 ? dish.name : `${dish.name} (${portions} portions)`, method,
        ingredients.map((ing) => `${ing.name} ${grams(ing)} g`)));
      inner.append(cookLink);
    }
  };
  draw();
  open.append(inner);
  return open;
}

// Keep the add-a-food form mounted while a window chip redraws the recipe book.
export let foodFormEl = null;
export let dishesSearch = "";
export let dishesSort = "Recent";
export const dishRatingBusy = new Set();
try {
  const saved = localStorage.getItem("tiffin.recipes-sort");
  if (["Recent", "Best rated", "A to Z"].includes(saved)) dishesSort = saved;
} catch (e) { /* private browsing can disable storage */ }

export function dishRating(dish, noteBox) {
  const wrap = el("div", { class: "dish-rating" });
  const stars = el("div", { class: "stars", role: "group", "aria-label": `Rate ${dish.name}` });
  const confirmed = el("p", { class: "muted", "aria-live": "polite" });
  for (let n = 1; n <= 5; n += 1) {
    const b = el("button", { type: "button", text: String(n), class: dish.score === n ? "on" : "",
      "aria-label": `${n} out of 5`, "aria-pressed": String(dish.score === n) });
    b.addEventListener("click", async () => {
      if (b.disabled || dishRatingBusy.has(dish.id)) return;
      dishRatingBusy.add(dish.id);
      const body = { recipe_id: dish.id, score: n };
      if (noteBox.value.trim()) body.note = noteBox.value.trim();
      else if (dish.note) body.note = "";
      const buttons = [...document.querySelectorAll(`#dishes .dish-rating[data-recipe-id="${dish.id}"] button`)];
      for (const other of buttons) other.disabled = true;
      try {
        await api("api/dishes/rate", { body });
        dish.note = body.note === undefined ? dish.note : body.note;
        dish.score = n;
        for (const group of document.querySelectorAll("#dishes .dish-rating")) {
          if (group.dataset.recipeId !== String(dish.id)) continue;
          for (const other of group.querySelectorAll("button")) {
            const selected = Number(other.textContent) === n;
            other.classList.toggle("on", selected);
            other.setAttribute("aria-pressed", String(selected));
          }
        }
        confirmed.textContent = n <= 2 ? "Noted - I will stop proposing it."
          : n >= 4 ? "Noted - it will come back often." : "Noted.";
      } catch (e) {
        fail({ message: e.message, retry: () => b.click() });
      } finally {
        dishRatingBusy.delete(dish.id);
        for (const other of buttons) other.disabled = false;
      }
    });
    stars.append(b);
  }
  wrap.dataset.recipeId = String(dish.id);
  wrap.append(stars, confirmed);
  return wrap;
}

export async function renderDishes() {
  const ticket = drawTicket("dishes");
  const data = await api(`api/dishes?recent=${dishesRecent}`);
  if (outdated("dishes", ticket)) return;
  const body = $("#dishes-body");
  body.replaceChildren();
  const search = el("input", { type: "search", id: "find-dish", value: dishesSearch,
    placeholder: "Dish or ingredient" });
  body.append(el("label", { for: "find-dish", text: "Find a dish" }), search);
  const sort = el("div", { class: "row sort-chips", role: "group", "aria-label": "Sort recipes" });
  for (const choice of ["Recent", "Best rated", "A to Z"]) {
    const btn = el("button", { type: "button", text: choice, class: dishesSort === choice ? "on" : "",
      "aria-pressed": String(dishesSort === choice) });
    btn.addEventListener("click", () => {
      dishesSort = choice;
      try { localStorage.setItem("tiffin.recipes-sort", choice); } catch (e) { /* storage unavailable */ }
      for (const other of sort.children) {
        other.classList.toggle("on", other === btn);
        other.setAttribute("aria-pressed", String(other === btn));
      }
      drawRows();
    });
    sort.append(btn);
  }
  body.append(sort);
  // a dish cooked once in July is still in the book at recent=90 (dishes_summary/07); with no
  // control at all it drops out of his own recipe book a month after he last cooked it
  const chips = el("div", { class: "row" });
  for (const days of [30, 90, 180, 3650]) {
    const btn = el("button", { type: "button", text: days === 3650 ? "All" : `${days} days`,
      class: days === dishesRecent ? "on" : "", "aria-pressed": String(days === dishesRecent) });
    btn.addEventListener("click", () => {
      dishesRecent = days;
      renderDishes().catch((e) => fail(e.message));
    });
    chips.append(btn);
  }
  body.append(chips);
  const list = el("div", { class: "recipe-list" });
  body.append(list);
  const noteBoxes = new Map();
  const noteFor = (dish) => {
    if (noteBoxes.has(dish.id)) return noteBoxes.get(dish.id);
    const noteBox = el("input", { type: "text", placeholder: "Why? (optional) - tap a score to save it",
      "aria-label": `Note on ${dish.name}` });
    noteBox.value = dish.note || "";
    noteBoxes.set(dish.id, noteBox);
    return noteBox;
  };
  const drawRows = () => {
    list.replaceChildren();
    const match = data.dishes.filter((dish) => `${dish.name} ${(dish.ingredients || []).map((i) => i.name).join(" ")}`
      .toLocaleLowerCase("en-GB").includes(dishesSearch.trim().toLocaleLowerCase("en-GB")));
    const today = state.today;
    const recent = match.filter((dish) => dish.score === null && dish.last_day
      && dish.last_day <= today && dish.last_day >= addDays(today, -6));
    if (recent.length) {
      // folded once there are more than a few: the book under it is what the view is for
      const group = el("details", { class: "fold rate-recent" });
      group.open = recent.length <= 4;
      group.append(el("summary", { text: `Rate what you had (${recent.length})` }));
      for (const dish of recent) {
        group.append(el("div", { class: "rate-recent-row" },
          el("span", { text: dish.name }), dishRating(dish, noteFor(dish))));
      }
      list.append(group);
    }
    const ordered = [...match].sort((a, b) => dishesSort === "Best rated"
      ? (b.score ?? -1) - (a.score ?? -1) || a.name.localeCompare(b.name)
      : dishesSort === "A to Z" ? a.name.localeCompare(b.name)
        : (b.last_day || "").localeCompare(a.last_day || "") || a.name.localeCompare(b.name));
    if (!ordered.length) {
      list.append(el("p", { class: "muted", text: data.dishes.length
        ? "No dishes match that search." : "No dishes in this window yet." }));
    }
    for (const dish of ordered) {
      const box = el("details", { class: "slot recipe-card" });
      box.append(el("summary", {}, el("h3", { text: dish.name }),
        el("p", { class: "muted", text: `${dish.score === null ? "Not rated" : `${dish.score} of 5`} · ${dish.prep_minutes} min${dish.last_day ? ` · last on ${dayName(dish.last_day)}` : ""}` })));
      const ingredientBits = (dish.ingredients || []).map((i) => i.name);
      if (ingredientBits.length) box.append(el("p", { class: "muted", text: ingredientBits.join(", ") }));
      const recipe = dishRecipe(dish);
      if (recipe) box.append(recipe);
      box.append(el("p", { class: "muted rate-label", text: "Rate it" }), dishRating(dish, noteFor(dish)));
      const note = el("details", { class: "recipe-note" });
      note.append(el("summary", { text: "Add a note" }), noteFor(dish));
      box.append(note);
      list.append(box);
    }
    wirePlanDetails(list);
  };
  search.addEventListener("input", () => { dishesSearch = search.value; drawRows(); });
  drawRows();
  if (!foodFormEl) foodFormEl = foodForm();
  body.append(foodFormEl);
}

/* ---------------------------------------------------------------- add a food */
// the recipes? example being I add skyr that I bought, provide it's kcal/100g, protein/fat/carbs
// per 100g". There is no second step: an approved row of the right scope IS the roster the menu
// prompt is numbered from, so a food added here is in the next menu the model builds.
//
// It lives on Dishes rather than behind a ninth tab - eight pills already wrap to two rows on a
// phone - and it stays folded shut, because this is a once-in-a-while job and the tab is opened
// to rate last night's dinner.
export const FOOD_FIELDS = [
  { key: "kcal_100", label: "kcal", hint: "per 100 g" },
  { key: "protein_100", label: "Protein", hint: "g per 100 g" },
  { key: "fat_100", label: "Fat", hint: "g per 100 g" },
  { key: "carb_100", label: "Carbs", hint: "g per 100 g" },
  { key: "fibre_100", label: "Fibre", hint: "g per 100 g, optional" },
];

export function foodForm() {
  const wrap = el("div", { class: "food-form" });
  const open = el("button", { type: "button", class: "link", text: "Add a food", "aria-expanded": "false" });
  const panel = el("div", { hidden: true });
  open.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    open.setAttribute("aria-expanded", String(!panel.hidden));
    open.textContent = panel.hidden ? "Add a food" : "Close";
    if (!panel.hidden) name.focus();
    if (!panel.hidden && !aisle.options.length) loadAisles();
  });

  const name = el("input", { type: "text", placeholder: "Skyr, natural", "aria-label": "Food name" });
  const pack = el("input", { type: "text", inputmode: "decimal", placeholder: "450",
    "aria-label": "Pack size in grams" });
  const packLabel = el("input", { type: "text", placeholder: "450 g tub", "aria-label": "Pack label" });
  const aisle = el("select", { "aria-label": "Aisle" });
  const scope = el("select", { "aria-label": "Who it is for" });
  scope.append(el("option", { value: "both" }, "both of us"), el("option", { value: "me" }, "just me"));
  const inputs = {};
  const note = el("p", { class: "muted" });
  const save = el("button", { type: "button", text: "Add to the catalogue" });

  panel.append(el("p", { class: "muted",
    text: "Off the label, per 100 g. It joins the foods menus are built from straight away." }));
  const row1 = el("div", { class: "row" });
  row1.append(el("label", { text: "Food" }), name);
  panel.append(row1);
  const grid = el("div", { class: "food-grid" });
  for (const f of FOOD_FIELDS) {
    const input = el("input", { type: "text", inputmode: "decimal", placeholder: f.hint });
    inputs[f.key] = input;
    grid.append(el("label", {}, f.label, input));
  }
  panel.append(grid);
  const row2 = el("div", { class: "row" });
  row2.append(el("label", { text: "Pack" }), pack, packLabel);
  panel.append(row2);
  const row3 = el("div", { class: "row" });
  row3.append(el("label", { text: "Aisle" }), aisle, el("label", { text: "For" }), scope);
  panel.append(row3);
  panel.append(el("div", { class: "row" }, save), note);

  // the aisles come from the server so the shopping walk can order a hand-added food; until
  // they arrive the form cannot be submitted into a made-up aisle. The form is kept for the life
  // of the page, so an ask that failed is made again when it is opened or saved: once dropped,
  // every save was refused for a blank aisle until the page was reloaded
  let asking = null;
  const loadAisles = () => {
    if (!asking) {
      asking = api("api/foods").then((data) => {
        if (!aisle.options.length) for (const a of data.aisles) aisle.append(el("option", { value: a }, a));
        note.textContent = `${data.foods.length} foods in your menus now.`;
      }).catch((e) => fail(e.message)).finally(() => { asking = null; });
    }
    return asking;
  };
  loadAisles();

  save.addEventListener("click", async () => {
    const body = {
      name: name.value.trim(),
      pack_g: Number(decimal(pack.value)),
      pack_label: packLabel.value.trim(),
      aisle: aisle.value,
      person_scope: scope.value,
    };
    const blank = [];
    for (const f of FOOD_FIELDS) {
      // "3,5" is three and a half on a phone keyboard set to a comma, not thirty-five
      const raw = decimal(inputs[f.key].value);
      // a box left empty is a question not answered, never a zero: sending 0 kcal put a food
      // Tiffin believed was weightless into the roster every menu is built from
      if (raw !== "") body[f.key] = Number(raw);
      else if (f.key !== "fibre_100") blank.push(f.label);
    }
    if (!body.name) { fail("A food needs a name."); return; }
    if (blank.length) { fail(`Off the label, please: ${blank.join(", ")}.`); return; }
    if (!body.pack_g) { fail("How big is the pack?"); return; }
    if (!body.aisle) {
      // the aisles never arrived: asked for again, and the food waits for one to be picked
      // rather than being sent with a blank one and refused for it
      await loadAisles();
      if (aisle.options.length) fail("Pick the aisle it lives in, then add it again.");
      return;
    }
    save.disabled = true;
    try {
      const got = await api("api/foods", { body });
      const again = got.version > 1 ? ` (updated, version ${got.version})` : "";
      note.textContent = `${got.name} is in your menus${again}.` + (got.warning ? ` ${got.warning}` : "");
      name.value = "";
      pack.value = "";
      packLabel.value = "";
      for (const f of FOOD_FIELDS) inputs[f.key].value = "";
    } catch (e) {
      // the numbers are still in the boxes, so the fix is a keystroke rather than a retype
      fail({ message: e.message, retry: () => save.click() });
    } finally {
      save.disabled = false;
    }
  });

  wrap.append(open, panel);
  return wrap;
}
