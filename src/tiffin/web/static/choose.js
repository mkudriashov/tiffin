import { $, api, card, dayName, el, fail, fmtInt, slotLabel, state, weekday } from "./helpers.js";
import { cook, openCook } from "./cook.js";
import { drawTicket, outdated } from "./more.js";
import { show } from "./app.js";

/* Weekly meal choices. The shell loads this after app.js and opens this view on demand. */
"use strict";

export let chooseData = null;
export let chooseNotice = "";
export let choosePoll = null;
export let chooseWorking = false;

export function chooseStopPoll() {
  if (choosePoll !== null) clearTimeout(choosePoll);
  choosePoll = null;
}

export function chooseSchedulePoll() {
  chooseStopPoll();
  // The build takes minutes; a timer belongs only to the visible view.
  choosePoll = setTimeout(() => {
    choosePoll = null;
    if (state.tab !== "choose") return;
    renderChoose().catch((err) => {
      if (state.tab === "choose") {
        fail(err.message);
        chooseSchedulePoll();
      }
    });
  }, 20000);
}

export function chooseRange(start, end) {
  const first = dayName(start);
  const last = dayName(end);
  const sameMonth = start.slice(0, 7) === end.slice(0, 7);
  return `${sameMonth ? first.replace(/ [A-Za-z]+$/, "") : first} to ${last}`;
}

export function chooseListLink(url) {
  if (!url) return null;
  const list = new URL(url, location.href).searchParams.get("list");
  if (!list) return null;
  const link = el("a", { href: url, text: "Open the shopping list" });
  link.addEventListener("click", (event) => {
    event.preventDefault();
    show("shopping", { list }).catch((err) => fail(err.message));
  });
  return link;
}

export function chooseNoticeRow(body) {
  if (chooseNotice) {
    body.append(el("p", { class: "choose-notice", role: "status", text: chooseNotice }));
    chooseNotice = "";
  }
}

export function chooseBuildButton(data) {
  const button = el("button", { type: "button", class: "choose-primary",
    text: `Build choices for ${data.build_for} now` });
  button.addEventListener("click", async () => {
    if (chooseWorking) return;
    chooseWorking = true;
    button.disabled = true;
    const ticket = drawTicket("choose");
    try {
      const answer = await api("api/choices/build", { body: {} });
      if (outdated("choose", ticket) || state.tab !== "choose") return;
      chooseNotice = answer.said;
      await renderChoose();
    } catch (err) {
      if (!outdated("choose", ticket) && state.tab === "choose") fail(err.message);
    } finally {
      chooseWorking = false;
      button.disabled = false;
    }
  });
  return button;
}

export function chooseSettingButton(perSlot, label, className) {
  const button = el("button", { type: "button", class: className, text: label });
  button.addEventListener("click", async () => {
    if (chooseWorking) return;
    chooseWorking = true;
    button.disabled = true;
    const ticket = drawTicket("choose");
    try {
      const answer = await api("api/choices/setting", { body: { per_slot: perSlot } });
      if (outdated("choose", ticket) || state.tab !== "choose") return;
      chooseNotice = answer.said;
      await renderChoose();
    } catch (err) {
      if (!outdated("choose", ticket) && state.tab === "choose") fail(err.message);
    } finally {
      chooseWorking = false;
      button.disabled = false;
    }
  });
  return button;
}

export function chooseIngredientLine(ingredient) {
  return `${ingredient.name}: ${fmtInt(ingredient.grams)} g${ingredient.units ? ` (${ingredient.units})` : ""}`;
}

export function chooseRecipe(option) {
  const panel = el("div", { class: "choose-recipe", hidden: "" });
  const toggle = el("button", { type: "button", class: "choose-recipe-toggle",
    "aria-expanded": "false", text: "Recipe" });
  toggle.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
  });
  const ingredientLines = (option.ingredients || []).map(chooseIngredientLine);
  panel.append(el("h4", { text: "Ingredients" }),
    el("ul", {}, ingredientLines.map((line) => el("li", { text: line }))));
  panel.append(el("h4", { text: "Method" }),
    el("ol", {}, (option.method || []).map((step) => el("li", { text: step }))));
  const cook = el("button", { type: "button", class: "choose-cook", text: "Cook" });
  cook.addEventListener("click", () => openCook(option.name, option.method || [], ingredientLines));
  panel.append(cook);
  return [toggle, panel];
}

export function chooseCard(option, slot, day, setId, readOnly) {
  const picked = slot.picked === option.id;
  // a food refused since the week was built: still shown, so the choice is plain, never picked
  const avoided = (option.avoided || []).length > 0;
  const button = el("button", { type: "button", class: avoided ? "choose-card choose-avoided" : "choose-card",
    "aria-pressed": String(picked), "data-option-id": String(option.id), disabled: readOnly || avoided ? "" : null });
  // A visible tick accompanies the accent so a chosen dish is clear without colour.
  button.append(el("span", { class: "choose-tick", "aria-hidden": "true", text: "\u2713" }),
    el("span", { class: "choose-card-copy" },
      el("span", { class: "choose-card-name", text: option.name }),
      el("span", { class: "choose-card-meta",
        text: `${fmtInt(option.kcal)} kcal · P ${fmtInt(option.protein_g)} g · ${fmtInt(option.prep_minutes)} min` }),
      el("span", { class: "choose-tag", text: avoided
        ? `holds ${option.avoided.join(", ").toLowerCase()}, which you avoid now`
        : option.new ? "new" : option.why || "" })));
  if (!readOnly) {
    button.addEventListener("click", () => choosePick(setId, day, slot.slot, option.id, button));
  }
  return el("div", { class: "choose-option" }, button, ...chooseRecipe(option));
}

export function chooseMeal(slot, day, setId, readOnly) {
  // A meal made ahead for a cook of several days is one pot: it is picked once, on the cook's
  // first day, and the other days say which dish it is.
  const title = slot.batch
    ? `${slotLabel(slot.slot)} · ${chooseRange(slot.batch.from, slot.batch.to)}`
    : slotLabel(slot.slot);
  const meal = el("section", { class: "choose-meal" }, el("h3", { text: title }));
  if (slot.from_batch) {
    const dish = slot.options[0];
    meal.append(el("p", { class: "choose-batch-note",
      text: `${dish ? dish.name : "The batch"}: the batch, picked under ${dayName(slot.from_batch)}.` }));
    return meal;
  }
  if (slot.batch && !readOnly) {
    meal.append(el("p", { class: "choose-batch-note",
      text: `One pot for ${slot.batch.days} days: pick it once.` }));
  }
  const options = readOnly ? slot.options.filter((option) => option.id === slot.picked) : slot.options;
  for (const option of options) meal.append(chooseCard(option, slot, day, setId, readOnly));
  const error = el("p", { class: "choose-meal-error", role: "alert", hidden: "" });
  meal.append(error);
  return meal;
}

export function chooseDay(day, target, setId, readOnly) {
  const section = el("section", { class: "choose-day" });
  section.dataset.day = day.day;
  section.append(el("h2", {}, el("span", { text: dayName(day.day) }),
    el("span", { class: "choose-day-total",
      text: `${fmtInt(day.total.kcal)} kcal · P ${fmtInt(day.total.protein_g)} g` })));
  // The API supplies the floors missed by the picked dishes; the page names their amounts.
  for (const floor of day.short || []) {
    if (floor === "protein") section.append(el("p", { class: "choose-short",
      text: `Protein comes to ${fmtInt(day.total.protein_g)} g, under ${fmtInt(target.protein_g)} g.` }));
    if (floor === "fibre") section.append(el("p", { class: "choose-short",
      text: `Fibre comes to ${fmtInt(day.total.fibre_g)} g, under ${fmtInt(target.fibre_g)} g.` }));
  }
  for (const slot of day.slots) section.append(chooseMeal(slot, day.day, setId, readOnly));
  return section;
}

export async function choosePick(setId, day, slotName, optionId, button) {
  if (chooseWorking) return;
  const currentDay = chooseData.set.days.find((entry) => entry.day === day);
  const currentSlot = currentDay && currentDay.slots.find((entry) => entry.slot === slotName);
  // A tap on the default first card counts as the person's own choice; a repeat does not write.
  if (currentSlot && currentSlot.chosen && currentSlot.picked === optionId) return;
  const section = button.closest(".choose-day");
  const meal = button.closest(".choose-meal");
  const old = meal.querySelector('.choose-card[aria-pressed="true"]');
  const error = meal.querySelector(".choose-meal-error");
  const hadFocus = button === document.activeElement;
  chooseWorking = true;
  error.hidden = true;
  // The pressed state changes at once. One in-flight write per screen prevents two taps racing.
  if (old) old.setAttribute("aria-pressed", "false");
  button.setAttribute("aria-pressed", "true");
  for (const choice of section.querySelectorAll(".choose-card")) choice.disabled = true;
  const ticket = drawTicket("choose");
  try {
    const answer = await api("api/choices/pick", {
      body: { set_id: setId, day, slot: slotName, option_id: optionId },
    });
    if (outdated("choose", ticket) || state.tab !== "choose") return;
    const index = chooseData.set.days.findIndex((entry) => entry.day === day);
    if (index < 0) return;
    chooseData.set.days[index] = answer.day;
    // a pick for a batch changes every day of that cook: redraw the others too
    for (const other of (answer.days || []).slice(1)) {
      const at = chooseData.set.days.findIndex((entry) => entry.day === other.day);
      if (at < 0) continue;
      chooseData.set.days[at] = other;
      const node = document.querySelector(`#choose .choose-day[data-day="${other.day}"]`);
      if (node) node.replaceWith(chooseDay(other, chooseData.set.target, setId, false));
    }
    chooseData.set.picked = answer.picked;
    chooseData.set.of = answer.of;
    $("#choose-count").textContent = `${answer.picked} of ${answer.of} meals chosen`;
    // Replace only this day. Restore its position and the activated control's keyboard focus.
    const scrollY = window.scrollY;
    const replacement = chooseDay(answer.day, chooseData.set.target, setId, false);
    section.replaceWith(replacement);
    window.scrollTo(0, scrollY);
    if (hadFocus) {
      const focused = [...replacement.querySelectorAll(".choose-card")].find((node) =>
        node.dataset.optionId === String(optionId));
      if (focused) focused.focus({ preventScroll: true });
    }
  } catch (err) {
    if (outdated("choose", ticket) || state.tab !== "choose") return;
    button.setAttribute("aria-pressed", "false");
    if (old) old.setAttribute("aria-pressed", "true");
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    chooseWorking = false;
    if (!outdated("choose", ticket)) {
      for (const choice of section.querySelectorAll(".choose-card")) choice.disabled = false;
      if (hadFocus && button.isConnected && state.tab === "choose") button.focus({ preventScroll: true });
    }
  }
}

export function chooseConfirmButton(setId) {
  const button = el("button", { type: "button", class: "choose-primary", text: "Use these for the week" });
  button.addEventListener("click", async () => {
    if (chooseWorking) return;
    chooseWorking = true;
    button.disabled = true;
    const ticket = drawTicket("choose");
    try {
      const answer = await api("api/choices/confirm", { body: { set_id: setId } });
      if (outdated("choose", ticket) || state.tab !== "choose") return;
      chooseNotice = answer.said;
      await renderChoose();
      if (state.tab === "choose") {
        const link = chooseListLink(answer.list_url);
        const body = $("#choose-body");
        if (link && !body.querySelector(".choose-list")) {
          body.insertBefore(el("p", { class: "choose-list" }, link), body.firstChild.nextSibling);
        }
      }
    } catch (err) {
      if (!outdated("choose", ticket) && state.tab === "choose") fail(err.message);
    } finally {
      chooseWorking = false;
      button.disabled = false;
    }
  });
  return button;
}

export async function renderChoose() {
  chooseStopPoll();
  const ticket = drawTicket("choose");
  const data = await api("api/choices");
  if (outdated("choose", ticket) || state.tab !== "choose") return;
  chooseData = data;
  const body = $("#choose-body");
  body.replaceChildren();
  chooseNoticeRow(body);
  if (!data.enabled) {
    body.append(el("p", { text: "Choose your weekday meals from 3 options a meal, each Sunday for the week ahead. Whatever you pick, each day still adds up to your targets." }),
      el("div", { class: "choose-settings" },
        chooseSettingButton(3, "3 options a meal", "choose-primary"),
        chooseSettingButton(4, "4 options a meal", "choose-secondary")));
    return;
  }
  if (data.building) {
    body.append(el("p", { text: "Building your choices now. It takes about 10 minutes. This page will update when they are ready." }));
    chooseSchedulePoll();
  } else if (!data.set) {
    body.append(el("p", { text: "Your choices for next week arrive each Sunday morning. The first option for each meal is already picked, so you can leave any you like." }));
    if (data.can_build) body.append(chooseBuildButton(data));
  } else if (data.set.status === "failed") {
    body.append(el("p", { class: "choose-short", text: data.set.said }));
    if (data.can_build) body.append(chooseBuildButton(data));
  } else {
    const set = data.set;
    const readOnly = set.status === "chosen";
    if (readOnly) {
      body.append(el("p", { class: "choose-set-on", text: `Set on ${set.chosen_on}` }));
      const link = chooseListLink(set.list_url);
      if (link) body.append(el("p", { class: "choose-list" }, link));
    } else {
      const summary = el("div", { class: "choose-summary" },
        el("p", {}, el("strong", { text: chooseRange(set.start, set.end) }), " · ",
          el("span", { id: "choose-count", text: `${set.picked} of ${set.of} meals chosen` }),
          ` · set by ${set.deadline}`),
        chooseConfirmButton(set.id));
      body.append(summary);
    }
    for (const day of set.days) body.append(chooseDay(day, set.target, set.id, readOnly));
  }
  body.append(el("div", { class: "choose-footer" },
    chooseSettingButton(0, "Switch weekly choices off", "choose-link")));
}
