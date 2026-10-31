import { $, addDays, api, card, counted, dayName, el, fail, fmtInt, fmtTime, kg, num, plateGrams, plural, slotLabel, state, unitWords, weekday } from "./helpers.js";
import { batchFrom, openCook } from "./cook.js";
import { dishWord, tracking } from "./more.js";
import { renderMenu, scrollToAsked } from "./menu.js";
import { show, showTab } from "./app.js";

/* ---------------------------------------------------------------- today */
// the day the Today tab is showing - kept across a re-render (a tap on Menu and back) the same
// way batchFrom is for Batch, so picking a date does not snap straight back to the real today
export let todayShowing = null;
export function setTodayShowing(day) { todayShowing = day; }

export async function renderToday() {
  const asked = new URLSearchParams(location.search).get("date");
  // a date named by a link is a choice, like the picker; state.today is only ever a default.
  // Writing `data.day` back into todayShowing pinned the tab to the first day it drew, so a
  // phone left open overnight kept showing yesterday as "Today"
  if (asked && !todayShowing && asked !== state.today) todayShowing = asked;
  const day = todayShowing || state.today;
  const data = await api(`api/today?date=${day}`);
  $("#day").value = data.day;
  // Today, not "0040": at 00:40 the day he still has to answer for is Wednesday, and the one
  // tap back to it is this button, not retyping a date he may not remember
  $("#day-today").hidden = data.day === state.today;
  const rings = $("#today-rings");
  const detail = $("#today-detail");
  rings.replaceChildren();
  detail.replaceChildren();
  // three parts, in the order a phone reads them: what the day's numbers say, then the food beside
  const top = el("div", { class: "today-top" });
  const main = el("div", { class: "today-main" });
  const side = el("div", { class: "today-side" });
  detail.append(top, el("div", { class: "today-cols" }, main, side));

  /* the tab that opens by default had no empty state: someone who has not onboarded got one
     card reading "no targets" and nothing else, while every other tab says what is missing */
  const anything = data.targets || data.record || data.trend || data.meals.length ||
    data.estimates.length || data.sessions.length;
  if (!anything) {
    top.append(el("p", { class: "muted",
      text: "Nothing set up yet. Finish the questions in Telegram and this page fills in." }));
    return;
  }

  const t = data.targets;
  const rec = data.record;
  // Show intake only when recorded; otherwise label the available numbers as the plan.
  const recorded = rec && rec.status === "confirmed" && rec.kcal !== null ? rec.kcal : null;
  const estimatesTotal = data.estimates.reduce((sum, e) => sum + e.kcal, 0);
  const eaten = recorded !== null ? recorded : (data.estimates.length ? estimatesTotal : null);
  const hasMenu = data.meals.length > 0;
  const plannedKcal = data.meals.reduce((sum, m) => sum + (m.kcal || 0), 0);
  const plannedProtein = data.meals.reduce((sum, m) => sum + (m.protein_g || 0), 0);
  if (eaten !== null) {
    const left = t ? Math.round(t.kcal - eaten) : null;
    rings.append(card("Calories", t ? `${fmtInt(eaten)} / ${fmtInt(t.kcal)}` : fmtInt(eaten),
      recorded !== null ? "recorded" : `off plan so far${left === null ? "" : ` · ${fmtInt(left)} left`}`,
      t ? eaten / t.kcal : undefined));
  } else if (hasMenu) {
    const unanswered = rec && rec.status === "unconfirmed" ? " · not answered" : "";
    rings.append(card("Planned", `${fmtInt(plannedKcal)} kcal`,
      t ? `target ${fmtInt(t.kcal)}${unanswered}` : unanswered.slice(3) || null,
      t ? plannedKcal / t.kcal : undefined));
  } else {
    rings.append(card("Calories", t ? `target ${fmtInt(t.kcal)}` : "no targets",
      t ? "no menu for this day" : null));
  }
  if (t) {
    const proteinKnown = recorded !== null && rec.protein_g !== null;
    if (proteinKnown) {
      rings.append(card("Protein", `${Math.round(rec.protein_g)} / ${t.protein_g} g`, "at least",
        rec.protein_g / t.protein_g));
    } else if (hasMenu) {
      rings.append(card("Protein", `${Math.round(plannedProtein)} g`, `planned · at least ${t.protein_g}`,
        Math.min(plannedProtein / t.protein_g, 1)));
    } else {
      rings.append(card("Protein", `- / ${t.protein_g} g`, "at least"));
    }
  } else {
    // "no targets" on the Calories card names the gap, not what to do about it - and a single
    // weigh-in already makes `anything` true above, so the tab's one helpful sentence (behind
    // the all-empty early return) never reaches a person who has this much set up
    top.append(el("p", { class: "muted",
      text: "No weekly targets yet. They are set by the Sunday calibration in Telegram." }));
  }
  if (data.trend) {
    // Show how long the latest weight trend has been waiting for a new reading.
    const since = Math.round(
      (new Date(data.day + "T12:00:00") - new Date(data.trend.day + "T12:00:00")) / 86400000);
    rings.append(card("Trend", kg(data.trend.trend_kg),
      since >= 3 ? `no weigh-in for ${plural(since, "day")}` : `${data.trend.n} weigh-ins`,
      undefined, () => show("progress").catch((e) => fail(e.message))));
  }
  if (data.wellness && data.wellness.steps !== null) {
    const floor = data.profile ? data.profile.steps_floor : null;
    const steps = data.wellness.steps;
    // clamped: a floor is a minimum, so clearing it reads as the win it is, not as the amber
    // "over" colour Calories gets for eating past its own target - card()'s ratio > 1.05 rule
    // is unmodified, it is just never handed more than 1 to compare against. Short of the
    // floor, the subtitle does the same subtraction the 16:00 nudge already says out loud
    // ("N short of floor") rather than leaving a bare count for him to do it off.
    // Tappable straight into the chart it hints at; when the number arrived, because a morning
    const synced = data.wellness.synced_at ? ` · synced ${fmtTime(data.wellness.synced_at)}` : "";
    rings.append(card("Steps", fmtInt(steps),
      floor
        ? (steps < floor
            ? `${fmtInt(floor - steps)} short of ${fmtInt(floor)}${synced}`
            : `over ${fmtInt(floor)}${synced}`)
        : synced.slice(3) || null,
      floor ? Math.min(steps / floor, 1) : undefined, () => showTab("trends", "steps")));
  } else if (data.profile && data.profile.steps_floor) {
    // wellness is null or steps is null: nothing has counted a step today, but the floor still
    // exists, and the two reasons for that read very differently in Telegram already
    // (morning_targets.py) - the page should not be quieter about it than the bot is
    const floor = data.profile.steps_floor;
    // "yet" is true of a watch that counted yesterday. One left off for weeks is a choice, not a
    const worn = data.steps_last && data.steps_last >= addDays(data.day, -7);
    if (data.has_icu_key && worn) {
      rings.append(card("Steps", "-", `floor ${fmtInt(floor)} (no steps from the watch yet today)`));
    } else {
      const why = !data.has_icu_key
        ? "Steps are not counted: no intervals.icu."
        : data.steps_last
          ? `Steps are not counted: no reading from the watch since ${dayName(data.steps_last)}.`
          : "Steps are not counted without a watch.";
      top.append(el("p", { class: "muted", text: why }));
    }
  }
  // the fibre floor and how far the plan sits from the target, said once under the cards
  if (t) {
    const gap = hasMenu && Math.abs(plannedKcal - t.kcal) >= 1
      ? ` The menu plans ${fmtInt(plannedKcal)} kcal (${plannedKcal > t.kcal ? "+" : "-"}${fmtInt(Math.abs(plannedKcal - t.kcal))} on the target).`
      : "";
    top.append(el("p", { class: "muted",
      text: `Target ${fmtInt(t.kcal)} kcal · protein at least ${t.protein_g} g · fibre at least ${t.fibre_g} g.${gap}` }));
  }

  if (!data.meals.length && t && data.day === state.today && tracking()) {
    top.append(el("p", { class: "muted", text: "Tracking only: no menu, the targets are yours to eat to." }));
  } else if (!data.meals.length && t && data.day === state.today) {
    // a day with no menu said nothing at all about food; say so and point at what is planned
    const ahead = el("button", { type: "button", class: "link", text: "See what is planned" });
    ahead.addEventListener("click", () => show("menu").catch((e) => fail(e.message)));
    top.append(el("p", { class: "muted" }, "No menu for today. ", ahead));
  }
  // the meal it is time for, first and large, with the one tap that cooks it: the reason most
  // people open the page at 12:40 is lunch, not the day's arithmetic
  const nextMeal = data.meals.find((m) => m.slot === data.next_slot);
  if (nextMeal) {
    const box = el("section", { class: "next-meal", "aria-label": "Next meal" });
    box.append(el("p", { class: "eyebrow", text: `Next · ${slotLabel(nextMeal.slot)}` }),
      el("h2", { text: nextMeal.name }),
      el("p", { class: "muted",
        text: `${fmtInt(nextMeal.kcal)} kcal · P ${num(nextMeal.protein_g)} g${nextMeal.prep_minutes ? ` · ${nextMeal.prep_minutes} min` : ""}` }));
    const acts = el("div", { class: "row" });
    if ((nextMeal.method || []).length) {
      const cookNow = el("button", { type: "button", class: "primary fill", text: "Cook it" });
      cookNow.addEventListener("click", () => openCook(nextMeal.name, nextMeal.method,
        (nextMeal.ingredients || []).map((ing) =>
          `${ing.name} ${plateGrams(ing, nextMeal.portion_multiplier, nextMeal.servings)} g`)));
      acts.append(cookNow);
    }
    const recipe = el("button", { type: "button", text: "Recipe" });
    recipe.addEventListener("click", () => {
      const row = document.getElementById(`today-${nextMeal.slot}`);
      if (row && row.getAttribute("aria-expanded") !== "true") row.click();
      if (row) row.scrollIntoView({ block: "center" });
    });
    acts.append(recipe);
    box.append(acts);
    main.append(box);
  }
  // what goes in the freezer tonight, for a later day's dinner
  for (const said of data.freeze || []) main.append(el("p", { class: "note-box freeze", text: said }));
  // today or up to a week back: what the page may still record for the day on screen
  const recordAge = Math.round(
    (new Date(state.today + "T12:00:00") - new Date(data.day + "T12:00:00")) / 86400000);
  const recordable = recordAge >= 0 && recordAge <= 7;
  if (data.meals.length) {
    main.append(el("h2", { text: "Meals" }));
    const list = el("ul", { class: "items" });
    for (const m of data.meals) {
      const isNext = data.next_slot === m.slot;
      // the dish name wraps rather than clipping: "Lunch: Chicken, chickpea and feta s..." hid
      // the one word that said what it was
      const row = el("div", { class: "tickable wrap", id: `today-${m.slot}` },
        el("span", { text: `${slotLabel(m.slot)}: ${m.name}` }),
        el("span", { class: "muted", text: isNext ? `next · ${fmtInt(m.kcal)} kcal` : `${fmtInt(m.kcal)} kcal` }));
      // Open the meal details from the Today card using the data already loaded.
      const openDetail = el("div", { class: "meal-detail", hidden: true });
      for (const ing of m.ingredients || []) {
        // one box's worth, not the whole pot's - the same servings/portion_multiplier division
        // Menu applies to the same slot, or a batch lunch here would print four boxes of
        // chicken under one box's kcal heading again (numbers_units/04). The per-100 g figures
        // stay on Menu, a tap away: read standing at a hob they buried the grams
        const scaled = plateGrams(ing, m.portion_multiplier, m.servings);
        const units = unitWords(ing, scaled);
        openDetail.append(el("p", { class: "muted",
          text: `${ing.name} - ${scaled} g${units ? `, ${units}` : ""}` }));
      }
      for (const [i, step] of (m.method || []).entries()) {
        openDetail.append(el("p", { text: `${i + 1}. ${step}` }));
      }
      // Menu already gives every slot an anchor (`d-${day}-${slot}`) and reads one back off its
      // own address; nothing on Today pointed at it, so the recipe named here was two taps and
      // a scroll away on another tab
      const openLink = el("button", { type: "button", class: "link", text: "Open in Menu" });
      openLink.addEventListener("click", (e) => {
        e.stopPropagation();
        // one timer later than renderMenu's own `setTimeout(scrollToAsked, 0)`: a promise
        // callback drains before any timer, so scrolling here first meant renderMenu's fallback
        // ran second, found no ?day= in the address and threw him back to today's heading -
        // the one thing this link exists to save him doing by hand
        show("menu", { day: data.day, slot: m.slot }).then(() => setTimeout(() => {
          const target = document.getElementById(`d-${data.day}-${m.slot}`);
          if (target) target.scrollIntoView({ block: "start" });
          else fail(`${dayName(data.day)} is not on the Menu tab.`);
        }, 0)).catch((err) => fail(err.message));
      });
      openDetail.append(openLink);
      openDetail.append(dishWord(m));
      if ((m.method || []).length) {
        // propped against the kettle with wet hands, one method paragraph among several looks
        // just like the ingredient list above it - the overlay is the actual hands-free reader
        const cookLink = el("button", { type: "button", class: "link", text: "Cook" });
        cookLink.addEventListener("click", (e) => {
          e.stopPropagation();
          openCook(m.name, m.method, (m.ingredients || []).map((ing) =>
            `${ing.name} ${plateGrams(ing, m.portion_multiplier, m.servings)} g`));
        });
        openDetail.append(" ", cookLink);
      }
      if (recordable) openDetail.append(differentForm(data.day, m));
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      // open or shut, said to a screen reader as well as shown
      row.setAttribute("aria-expanded", String(!openDetail.hidden));
      const toggle = () => {
        openDetail.hidden = !openDetail.hidden;
        row.setAttribute("aria-expanded", String(!openDetail.hidden));
      };
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggle();
        }
      });
      list.append(el("li", { class: isNext ? "next" : "" }, row, openDetail));
    }
    main.append(list);
  }
  /* Show recorded training even when no session was planned for that day. */
  const planned = data.planned_sessions || (data.planned_session ? [data.planned_session] : []);
  const trained = data.sessions.filter((s) => ["confirmed", "manual", "matched", "extra"].includes(s.status));
  if (planned.length || trained.length) {
    side.append(el("h2", { text: "Training" }));
    const real = planned.filter((p) => p.kind !== "rest");
    // "not yet" only where something could count it: the watch sent training in the last four
    const counted = data.activity_last && data.activity_last >= addDays(data.day, -28);
    if (real.length) {
      const done = trained.length ? " - done" : counted ? " - not yet" : "";
      side.append(el("p", { text: real.map((p) => `${p.kind} ${p.target_minutes} min`).join(" then ") + done }));
    } else if (planned.length) {
      side.append(el("p", { text: trained.length ? "Rest day, and you trained anyway" : "Rest day" }));
    }
    for (const s of trained) {
      side.append(el("p", { class: "muted",
        text: `${s.name || s.kind || "activity"}${s.minutes ? ` ${s.minutes} min` : ""}` }));
    }
  }
  // the page, and the evening check-in that asked is gone for her)
  if (recordable) {
    if (!planned.length && !trained.length) side.append(el("h2", { text: "Training" }));
    side.append(sessionForm(data.day, planned.some((p) => p.kind !== "rest") && !trained.length));
  }
  if (data.cycle) side.append(cycleCard(data.cycle));
  /* the evening answer, on the screen that shows the day it is about. "Not quite" stays in the
     chat on purpose: it needs what-changed, an estimate and a tap to accept the number. */
  // any day still open inside the week the server accepts - including one day_close already
  // wrote as "unconfirmed" at midnight, which is exactly the day a late answer is for
  const ageDays = Math.round(
    (new Date(state.today + "T12:00:00") - new Date(data.day + "T12:00:00")) / 86400000);
  const open = !rec || rec.status === "unconfirmed";
  if (open && data.meals.length && ageDays >= 0 && ageDays <= 7) {
    const confirm = el("button", { type: "button", class: "primary",
      text: data.day === state.today ? "Record today as planned" : `Record ${dayName(data.day)} as planned` });
    confirm.addEventListener("click", async () => {
      confirm.disabled = true;
      try {
        // the day ON SCREEN; the server checks it is today or up to a week back
        await api("api/today/confirm", { body: { as_planned: true, day: data.day } });
        await renderToday(); // the cards and the status move with the record
      } catch (e) {
        confirm.disabled = false;
        fail({ message: e.message, retry: () => confirm.click() });
      }
    });
    const box = el("section", { class: "the-day", "aria-label": "The day" });
    box.append(el("h2", { text: "The day" }));
    // the days before it nobody answered: one tap records the week. Each was asked
    // about by name here; a silent day is never assumed to have gone to plan
    const earlier = data.open_days || [];
    if (earlier.length) {
      const names = earlier.map((d) => new Date(d + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short" }));
      const all = el("button", { type: "button", class: "primary fill",
        text: `All ${earlier.length + 1} days as planned` });
      all.addEventListener("click", async () => {
        all.disabled = true;
        try {
          await api("api/today/confirm", { body: { as_planned: true, day: data.day, with_open: true } });
          await renderToday();
        } catch (e) {
          all.disabled = false;
          fail({ message: e.message, retry: () => all.click() });
        }
      });
      box.append(el("p", { class: "muted", text: `Also still open: ${names.join(", ")}.` }));
      box.append(el("div", { class: "row" }, all, confirm));
    } else {
      box.append(el("div", { class: "row" }, confirm));
    }
    box.append(el("p", { class: "muted",
      text: "Something changed? Open the meal and use \"I ate something different\"." }));
    side.append(box);
  }
  if (data.estimates.length) {
    side.append(el("h2", { text: "Off plan" }));
    for (const e of data.estimates) {
      side.append(el("p", { text: `${e.description} - ${Math.round(e.kcal)} kcal` }));
    }
  }
}

// "I ate something different" on one meal: estimated and counted for the day, the rest of the
export function differentForm(day, meal) {
  const box = el("details", { class: "foods different" });
  box.append(el("summary", { text: "I ate something different" }));
  const input = el("input", { type: "text", maxlength: "300", autocomplete: "off",
    placeholder: "e.g. a chicken wrap from Pret", "aria-label": `What you ate instead of ${meal.name}` });
  const go = el("button", { type: "submit", class: "primary", text: "Record" });
  const said = el("p", { class: "plate-said", role: "status" });
  said.hidden = true;
  const form = el("form", { class: "row add-avoid" }, input, go);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!input.value.trim()) return;
    go.disabled = true;
    said.hidden = false;
    said.textContent = "Working out what that was...";
    try {
      const got = await api("api/today/different", { body: { day, slot: meal.slot, text: input.value.trim() } });
      said.textContent = got.said;
      if (got.ok) setTimeout(() => renderToday().catch(() => {}), 1500);
      else go.disabled = false;
    } catch (e) {
      go.disabled = false;
      said.textContent = e.message;
    }
  });
  box.addEventListener("click", (e) => e.stopPropagation());
  box.append(form, said);
  return box;
}

// a session trained, in minutes, or a planned one skipped
export function sessionForm(day, canSkip) {
  const box = el("div", { class: "session-form" });
  const minutes = el("input", { type: "number", min: "1", max: "600", inputmode: "numeric",
    placeholder: "minutes", "aria-label": "Minutes trained" });
  const did = el("button", { type: "button", class: "primary", text: "I trained" });
  const said = el("p", { class: "muted", role: "status" });
  const send = async (done) => {
    const n = parseInt(minutes.value, 10);
    try {
      const got = await api("api/today/session", { body: { day, done, minutes: done && n > 0 ? n : null } });
      said.textContent = got.said;
      setTimeout(() => renderToday().catch(() => {}), 1200);
    } catch (e) {
      said.textContent = e.message;
    }
  };
  did.addEventListener("click", () => send(true));
  const row = el("div", { class: "row" }, minutes, did);
  if (canSkip) {
    const skip = el("button", { type: "button", text: "Skipped it" });
    skip.addEventListener("click", () => send(false));
    row.append(skip);
  }
  box.append(row, said);
  return box;
}

// where the day falls in her cycle, and the first day of a period entered here
export function cycleCard(c) {
  const box = el("section", { class: "cycle", "aria-label": "Cycle" });
  box.append(el("h2", { text: "Cycle" }));
  box.append(el("p", { text: c.day ? `Day ${c.day} of about ${c.length}: ${c.phase}` : "No period start entered yet." }));
  const date = el("input", { type: "date", value: state.today, max: state.today, "aria-label": "First day of your period" });
  const go = el("button", { type: "button", text: "Period started" });
  const said = el("p", { class: "muted", role: "status" });
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await api("api/cycle/start", { body: { day: date.value } });
      await renderToday();
    } catch (e) {
      go.disabled = false;
      said.textContent = e.message;
    }
  });
  box.append(el("div", { class: "row" }, date, go), said);
  return box;
}
