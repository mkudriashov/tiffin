import { $, api, dayName, el, fail, fmtStamp, slotLabel, state } from "./helpers.js";
import { chooseButton, show } from "./app.js";

/* ---------------------------------------------------------------- settings */
// How often the bot messages this person (migration 0035): their own choice, on their own page.
// Light is the default - one check-in a day, and a morning message only when the food is new.
export const RHYTHM_WORDS = [["light", "Light"], ["daily", "Every day"], ["weekly", "Once a week"]];

// A word about a dish where the dish is, in one tap: "not again" keeps it off every menu,
// "more like this" asks for it more often - a rating underneath (1 or 5), said back, and undone
export function dishWord(meal) {
  const box = el("div", { class: "row dish-word", role: "group", "aria-label": `Your word on ${meal.name}` });
  const said = el("p", { class: "muted", role: "status" });
  let score = meal.score;
  const draw = () => {
    box.replaceChildren();
    for (const [n, label] of [[1, "Not again"], [5, "More like this"]]) {
      const on = n === 1 ? score !== null && score !== undefined && score <= 2 : score >= 4;
      const b = el("button", { type: "button", text: label, class: on ? "on" : "" });
      b.setAttribute("aria-pressed", String(on));
      b.addEventListener("click", (e) => { e.stopPropagation(); if (!on) rate(n, b); });
      box.append(b);
    }
  };
  const rate = async (n, button, undoing = false) => {
    button.disabled = true;
    let got;
    try {
      got = await api("api/dishes/rate", { body: { recipe_id: meal.recipe_id, score: n } });
    } catch (e) {
      button.disabled = false;
      if (e.status >= 400 && e.status < 500) said.textContent = e.message;
      else fail({ message: e.message, retry: () => rate(n, button, undoing) });
      return;
    }
    const before = got.previous;
    score = got.score;
    draw();
    said.replaceChildren(undoing ? "Taken back." : got.said);
    if (!undoing) {
      const undo = el("button", { type: "button", class: "link", text: "Undo" });
      // back to their word before the tap, or to no lean either way if they had none
      undo.addEventListener("click", (e) => { e.stopPropagation(); rate(before ?? 3, undo, true); });
      said.append(" ", undo);
    }
  };
  draw();
  return el("div", {}, box, said);
}

export function rhythmPanel(current) {
  const box = el("section", { class: "weekend rhythm", "aria-label": "Messages from the bot" });
  box.append(el("h3", { text: "Messages from the bot" }));
  const said = el("p", { class: "muted", role: "status" });
  const row = el("div", { class: "row", role: "group", "aria-label": "How often" });
  const buttons = [];
  for (const [value, text] of RHYTHM_WORDS) {
    const b = el("button", { type: "button", text, class: value === current ? "on" : "" });
    b.setAttribute("aria-pressed", String(value === current));
    b.addEventListener("click", async () => {
      if (b.classList.contains("on")) return;
      buttons.forEach((x) => { x.disabled = true; });
      try {
        const got = await api("api/rhythm", { body: { rhythm: value } });
        said.textContent = got.said;
        buttons.forEach((x) => {
          x.classList.toggle("on", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
      } catch (e) {
        if (e.status >= 400 && e.status < 500) said.textContent = e.message;
        else fail({ message: e.message, retry: () => b.click() });
      } finally {
        buttons.forEach((x) => { x.disabled = false; });
      }
    });
    buttons.push(b);
    row.append(b);
  }
  box.append(row, said);
  return box;
}

// A tab redrawn twice in quick succession (two taps on the week arrows, a window chip tapped
// while the last one loads) drew whichever answer came back LAST, which was not always the one
// asked for last. Each draw takes a number; an answer to an older ask is dropped.
export const drawing = {};
export function drawTicket(name) {
  drawing[name] = (drawing[name] || 0) + 1;
  return drawing[name];
}
export function outdated(name, ticket) {
  return drawing[name] !== ticket;
}

export const PLAN_MENUS = [["auto", "Planned for me"], ["choose", "I choose"], ["off", "Track only"]];
export const PLAN_COOKING = [["daily", "Each day"], ["batch", "One batch, Mon-Fri"], ["batch2", "Two batches"]];
export const PLAN_MEALS = ["breakfast", "lunch", "dinner", "snack", "pre_bed"];
export const tracking = () => Boolean(state.plan && state.plan.now && state.plan.now.planning === "off");

export function planChoiceRow(label, options, current, onPick) {
  const row = el("div", { class: "row", role: "group", "aria-label": label });
  for (const [value, text] of options) {
    const on = value === current;
    const b = el("button", { type: "button", text, class: on ? "on" : "" });
    b.setAttribute("aria-pressed", String(on));
    b.addEventListener("click", () => onPick(value));
    row.append(b);
  }
  return row;
}

// What a draft of How you plan changes from what they last decided: only these are sent
export function planChanges(draft, latest) {
  const changes = {};
  if (draft.planning !== latest.planning) changes.planning = draft.planning;
  if (draft.planning !== "off") {
    if (draft.cadence !== latest.cadence && draft.cadence !== "custom") changes.cadence = draft.cadence;
    const was = [...(latest.batch_setting || [])].sort().join(",");
    if (draft.cadence !== "daily" && [...draft.batch].sort().join(",") !== was) changes.batch = draft.batch;
    if (draft.planning === "choose" && draft.options !== latest.options) changes.options = draft.options;
  }
  return changes;
}

export function planPanel(said = "") {
  const box = el("section", { class: "more-group plan-group", "aria-label": "How you plan" });
  const plan = state.plan;
  if (!plan || !plan.now) return null;
  // what they last decided, whether it counts yet or is booked for a later day
  const latest = plan.booked || plan.now;
  const draft = {
    planning: latest.planning,
    cadence: latest.cadence,
    batch: [...(latest.batch_setting || latest.batch || [])],
    options: latest.options,
    starts: "monday",
  };
  const status = el("p", { class: "muted", role: "status", text: said });
  const draw = () => {
    box.replaceChildren(el("h2", { text: "How you plan" }), el("p", { text: plan.now.said }));
    if (plan.booked) {
      box.append(el("p", { class: "muted", text: `From ${plan.booked.from_said}: ${plan.booked.said}` }));
    }
    box.append(el("h3", { text: "Menus" }),
      planChoiceRow("Menus", PLAN_MENUS, draft.planning, (v) => { draft.planning = v; draw(); }));
    if (draft.planning !== "off") {
      const cooking = draft.cadence === "custom" ? [...PLAN_COOKING, ["custom", "Your own days"]] : PLAN_COOKING;
      box.append(el("h3", { text: "Cooking, Monday to Friday" }),
        planChoiceRow("Cooking", cooking, draft.cadence, (v) => {
          if (v === "custom") return;
          draft.cadence = v;
          if (v !== "daily" && !draft.batch.length) draft.batch = ["lunch", "dinner"];
          draw();
        }));
      if (draft.cadence !== "daily") {
        const row = el("div", { class: "row", role: "group", "aria-label": "Made ahead" });
        for (const meal of plan.meals || PLAN_MEALS) {
          const on = draft.batch.includes(meal);
          const b = el("button", { type: "button", text: slotLabel(meal), class: on ? "on" : "" });
          b.setAttribute("aria-pressed", String(on));
          b.addEventListener("click", () => {
            draft.batch = on ? draft.batch.filter((m) => m !== meal) : [...draft.batch, meal];
            draw();
          });
          row.append(b);
        }
        box.append(el("h3", { text: "Made ahead in each batch" }), row);
      }
      if (draft.planning === "choose") {
        box.append(el("h3", { text: "Dishes to choose from, each meal" }),
          planChoiceRow("Dishes a meal", [[3, "3"], [4, "4"]], draft.options, (v) => { draft.options = v; draw(); }));
      }
    }
    box.append(el("h3", { text: "Starts" }),
      planChoiceRow("Starts", [["monday", `From ${dayName(plan.next_monday)}`], ["tomorrow", "From tomorrow"]],
        draft.starts, (v) => { draft.starts = v; draw(); }));
    // greyed until something differs from what they last decided: a filled Save beside the
    // filled answers read as one more answer
    const pending = planChanges(draft, latest);
    const save = el("button", { type: "button", class: "primary", text: "Save" });
    save.disabled = !Object.keys(pending).length;
    save.addEventListener("click", async () => {
      const changes = planChanges(draft, latest);
      if (!Object.keys(changes).length) return;
      save.disabled = true;
      try {
        const got = await api("api/plan", { body: { ...changes, starts: draft.starts } });
        // the Choose view and the empty states follow the week a build would be for
        const me = await api("api/me").catch(() => null);
        state.plan = (me && me.plan) || got.plan;
        if (me) state.choices = me.choices || 0;
        chooseButton();
        // the booked line above already says what changes and from when
        const fresh = planPanel(`Saved.${got.note ? ` ${got.note}` : ""}`);
        if (fresh) box.replaceWith(fresh);
      } catch (e) {
        if (e.status >= 400 && e.status < 500) status.textContent = e.message;
        else fail({ message: e.message, retry: () => save.click() });
      } finally {
        save.disabled = false;
      }
    });
    box.append(el("div", { class: "row" }, save), status);
  };
  draw();
  return box;
}

// More: what is set once and changed rarely, out of the way of what is used every day
export async function renderMore() {
  const ticket = drawTicket("more");
  const [today, status, devices] = await Promise.all([
    api(`api/today?date=${state.today}`).catch(() => null),
    api("api/status").catch(() => null),
    api("api/devices").catch(() => null),
  ]);
  if (outdated("more", ticket)) return;
  const body = $("#more-body");
  body.replaceChildren();
  // how often the bot writes: a choice made once, moved off Today, where it sat under every day
  if (today && today.profile && today.profile.rhythm) body.append(rhythmPanel(today.profile.rhythm));
  const plan = planPanel();
  if (plan) body.append(plan);
  const food = el("section", { class: "more-group", "aria-label": "Food" });
  food.append(el("h2", { text: "Food" }));
  const avoid = el("button", { type: "button", class: "link", text: "Foods you avoid" });
  avoid.addEventListener("click", () => show("menu").then(() => setTimeout(() => {
    const panel = document.getElementById("foods");
    if (panel) {
      panel.open = true;
      panel.scrollIntoView({ block: "start" });
    }
  }, 0)).catch((e) => fail(e.message)));
  const add = el("button", { type: "button", class: "link", text: "Add a food from its label" });
  add.addEventListener("click", () => show("dishes").catch((e) => fail(e.message)));
  food.append(el("p", {}, avoid), el("p", {}, add));
  food.append(el("p", { class: "muted",
    text: "Everything else - what Skip does, variety, your kitchen - changes when you tell the bot, or with /set." }));
  body.append(food);
  const yours = el("section", { class: "more-group", "aria-label": "Your data" });
  yours.append(el("h2", { text: "Your data" }));
  // everything Tiffin holds about you, to keep: yours to read, whenever you like
  yours.append(el("p", {}, el("a", { href: "api/export", download: "",
    text: "Download everything Tiffin holds about you (JSON)" })));
  body.append(yours);
  if (devices && devices.devices && devices.devices.length) {
    const panel = el("section", { class: "more-group", "aria-label": "Signed-in devices" });
    panel.append(el("h2", { text: "Signed-in devices" }));
    for (const device of devices.devices) {
      const button = el("button", { type: "button", text: "Sign out" });
      button.addEventListener("click", async () => {
        try {
          await api("api/devices/sign-out", { body: { device_id: device.id } });
          if (device.current) location.reload();
          else await renderMore();
        } catch (e) { fail(e.message); }
      });
      panel.append(el("p", {}, `${device.created_at}${device.current ? " (this device)" : ""} `, button));
    }
    body.append(panel);
  }
  // the box, for the admin only (the API returns nothing to anyone else): the whole list, seen
  // or not, so a problem acknowledged on Today is still one tap away
  const items = (status && status.items) || [];
  if (items.length) {
    const box = el("section", { class: "more-group", "aria-label": "The box" });
    box.append(el("h2", { text: "The box, in the last day" }));
    for (const i of items) {
      box.append(el("p", {}, `${fmtStamp(i.at)} · ${i.what}`,
        i.why ? el("span", { class: "muted", text: ` - ${i.why}` }) : null));
    }
    const total = Math.max(status.total || 0, items.length);
    if (total > items.length) box.append(el("p", { class: "muted", text: `and ${total - items.length} more` }));
    body.append(box);
  }
}

// What the admin has already seen on the status strip, by kind: the same failed job returning
// every day is one fact, not a new alarm each morning. A kind seen stays quiet for a
// week and then shows again; anything of a new kind shows at once. Kept on this phone only.
