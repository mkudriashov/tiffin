import { api, el, fail, slotLabel, state } from "./helpers.js";

const slots = ["breakfast", "lunch", "dinner", "snack", "pre_bed"];

function field(label, control) {
  return el("label", {}, `${label} `, control);
}

function recipeOptions(select, recipes) {
  const selected = select.value;
  select.replaceChildren(el("option", { value: "" }, "Choose a dish"));
  for (const recipe of recipes) select.append(el("option", { value: recipe.id }, recipe.name));
  select.value = selected;
}

export function manualPanel(refreshMenu) {
  const panel = el("details", { class: "panel" });
  panel.append(el("summary", { text: "Plan meals yourself" }),
    el("p", { class: "muted", text: "Save a recipe from catalogue foods, then choose dishes for one day or repeat them for up to seven days. Portions and the shopping list are calculated here." }));
  const contents = el("div");
  panel.append(contents);
  let loaded = false;
  panel.addEventListener("toggle", async () => {
    if (!panel.open || loaded) return;
    try {
      const library = await api("api/manual");
      draw(library);
      loaded = true;
    } catch (e) { fail(e.message); }
  });

  function draw(library) {
    contents.replaceChildren();
    const recipeForm = el("form");
    recipeForm.append(el("h3", { text: "Add a recipe" }));
    const name = el("input", { type: "text", required: true, maxlength: "60" });
    const prep = el("input", { type: "number", min: "0", max: "120", value: "15", required: true });
    const servings = el("input", { type: "number", min: "1", max: "20", value: "1", required: true });
    const method = el("textarea", { rows: "3", required: true, placeholder: "One method step per line" });
    recipeForm.append(field("Dish name", name), field("Preparation minutes", prep),
      field("Servings", servings));
    const ingredientRows = el("div");
    const ingredientInputs = [];
    function addIngredient() {
      const food = el("select", { required: true });
      food.append(el("option", { value: "" }, "Choose a food"));
      for (const item of library.foods) food.append(el("option", { value: item.code }, item.name));
      const grams = el("input", { type: "number", min: "0.1", max: "5000", step: "any", required: true, placeholder: "grams" });
      const row = el("div", { class: "row" }, field("Food", food), field("Grams", grams));
      ingredientRows.append(row);
      ingredientInputs.push({ food, grams });
    }
    addIngredient();
    const add = el("button", { type: "button", text: "Add ingredient" });
    add.addEventListener("click", () => { if (ingredientInputs.length < 12) addIngredient(); });
    recipeForm.append(ingredientRows, add, field("Method, one step per line", method));
    const recipeResult = el("p", { role: "status" });
    recipeForm.append(el("button", { type: "submit", text: "Save recipe" }), recipeResult);
    recipeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const saved = await api("api/manual/recipes", { body: {
          name: name.value,
          prep_minutes: Number(prep.value),
          servings: Number(servings.value),
          method: method.value.split("\n").map((step) => step.trim()).filter(Boolean),
          ingredients: ingredientInputs.map(({ food, grams }) => ({ code: food.value, g: Number(grams.value) })),
        } });
        recipeResult.textContent = saved.said;
        library.recipes = (await api("api/manual")).recipes;
        for (const select of dishInputs.values()) recipeOptions(select, library.recipes);
      } catch (e) { fail(e.message); }
    });

    const menuForm = el("form");
    menuForm.append(el("h3", { text: "Choose meals" }));
    const start = el("input", { type: "date", min: state.today, value: state.today, required: true });
    const days = el("select");
    for (let count = 1; count <= 7; count += 1) days.append(el("option", { value: count }, `${count} day${count === 1 ? "" : "s"}`));
    menuForm.append(field("First day", start), field("Repeat for", days));
    const dishInputs = new Map();
    const portionInputs = new Map();
    for (const slot of slots) {
      const dish = el("select");
      recipeOptions(dish, library.recipes);
      const portion = el("input", { type: "number", min: "0.25", max: "4", step: "0.05", value: "1", "aria-label": `${slotLabel(slot)} servings` });
      menuForm.append(el("div", { class: "row" }, field(slotLabel(slot), dish), field("Servings", portion)));
      dishInputs.set(slot, dish);
      portionInputs.set(slot, portion);
    }
    const menuResult = el("p", { role: "status" });
    menuForm.append(el("button", { type: "submit", text: "Save menu and shopping list" }), menuResult);
    menuForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const meals = slots.filter((slot) => dishInputs.get(slot).value).map((slot) => ({
        slot, recipe_id: Number(dishInputs.get(slot).value), portion: Number(portionInputs.get(slot).value),
      }));
      try {
        const saved = await api("api/manual/menu", { body: { start: start.value, days: Number(days.value), meals } });
        menuResult.textContent = saved.said;
        await refreshMenu();
      } catch (e) { fail(e.message); }
    });
    contents.append(recipeForm, menuForm);
  }
  return panel;
}
