import { $, api, card, counted, dayName, el, fail, fmtInt, kg, num, plural, weightFactor, weightUnit } from "./helpers.js";
import { drawTicket, outdated } from "./more.js";
import { loadPlotly, plotTheme } from "./progress_page.js";
import { show } from "./app.js";

/* Progress: one person's weeks at a glance - weight, movement, training, recovery and the plan -
   built from what is measured or planned, never from intake nobody recorded. Loaded after app.js, whose helpers it uses; the server writes every caption, so the
   words always match the numbers behind them. */
"use strict";

export let progressWeeks = 8;
export let progressWeek = null;
try {
  const saved = Number(localStorage.getItem("tiffin.progress-weeks"));
  if ([4, 8, 12, 26].includes(saved)) progressWeeks = saved;
} catch (e) { /* Private browsing still gets the default window. */ }

export function progressCard(label, value, detail) {
  return el("div", { class: "card" }, el("div", { class: "k", text: label }),
    el("div", { class: "v", text: value }), el("div", { class: "s", text: detail }));
}

export function progressChartBlock(parent, title, caption) {
  const plot = el("div", { class: "progress-plot", role: "img", "aria-label": `${title}. ${caption}` });
  parent.append(el("section", { class: "progress-chart" },
    el("h3", { text: title }), plot, el("p", { class: "muted", text: caption })));
  return plot;
}

export function progressPlot(plot, traces, layout) {
  const theme = plotTheme();
  return window.Plotly.newPlot(plot, traces, {
    ...theme, height: 220, dragmode: false, ...layout,
    xaxis: { ...theme.xaxis, ...(layout.xaxis || {}) },
    yaxis: { ...theme.yaxis, ...(layout.yaxis || {}) },
  }, { displayModeBar: false, responsive: true });
}

// the tape, weekly: the latest of each, how it is taken and what it decides, and what the
export async function renderMeasures() {
  const box = $("#measures-body");
  if (!box) return;
  const data = await api("api/measurements");
  box.replaceChildren();
  const unit = data.unit === "in" ? '"' : " cm";
  for (const r of data.reads || []) box.append(el("p", { text: r }));
  for (const s of data.sites) {
    const item = el("details", { class: "foods" });
    item.append(el("summary", { text: `${s.label}: ${s.value === null ? "not yet" : `${s.value}${unit}`}${s.day ? ` (${dayName(s.day)})` : ""}` }));
    if (s.how) item.append(el("p", { text: s.how }));
    if (s.decides) item.append(el("p", { class: "muted", text: s.decides }));
    box.append(item);
  }
  box.append(el("p", { class: "muted", text: "Send /measure in Telegram any day, or answer the weekly check." }));
}

export async function renderProgress() {
  renderMeasures().catch(() => {});
  const ticket = drawTicket("progress");
  const data = await api(`api/progress?weeks=${progressWeeks}${progressWeek ? `&week=${progressWeek}` : ""}`);
  if (outdated("progress", ticket)) return;
  const body = $("#progress-body");
  body.replaceChildren();
  const chips = el("div", { class: "progress-chips", "aria-label": "Chart window" });
  for (const weeks of [4, 8, 12, 26]) {
    const chip = el("button", { type: "button", class: weeks === progressWeeks ? "on" : "",
      "aria-pressed": String(weeks === progressWeeks), text: `${weeks} weeks` });
    chip.addEventListener("click", () => {
      progressWeeks = weeks;
      try { localStorage.setItem("tiffin.progress-weeks", String(weeks)); } catch (e) { /* Keep this choice here. */ }
      renderProgress().catch((err) => fail(err.message));
    });
    chips.append(chip);
  }
  body.append(chips);
  // a sync that runs every hour and brings nothing reads like a quiet week on every card: said
  // first, with the day it stopped, never left for the person to work out
  const sync = data.sync || {};
  const silent = [];
  if (sync.watch_regular && sync.watch_days > 2) {
    silent.push(`No steps, sleep or heart readings from the watch since ${dayName(sync.watch_last)}.`);
  }
  if (sync.activity_days !== null && sync.activity_days !== undefined && sync.activity_days > 14) {
    silent.push(`No training has arrived from intervals.icu since ${dayName(sync.activity_last)}.`);
  }
  if (silent.length) {
    // don't wear it") - the cards and charts below simply stop where the readings stop
    body.append(el("div", { class: "progress-sync muted", role: "status" },
      ...silent.map((line) => el("p", { text: line }))));
  }
  const title = data.week.is_current ? "This week" : `Week of ${dayName(data.week.start)}`;
  const nav = el("div", { class: "progress-week-nav" }, el("h2", { text: title }));
  const prev = el("button", { type: "button", "aria-label": "Previous week", text: "‹" });
  const next = el("button", { type: "button", "aria-label": "Next week", text: "›" });
  next.disabled = data.week.next === null;
  prev.addEventListener("click", () => {
    progressWeek = data.week.prev;
    renderProgress().catch((err) => fail(err.message));
  });
  next.addEventListener("click", () => {
    if (!data.week.next) return;
    progressWeek = data.week.next;
    renderProgress().catch((err) => fail(err.message));
  });
  nav.append(prev, next);
  body.append(nav);
  const c = data.cards;
  const cards = el("div", { class: "cards progress-cards" });
  if (c.weight) cards.append(progressCard("Weight",
    c.weight.trend_kg === null ? plural(c.weight.weigh_ins, "weigh-in") : kg(c.weight.trend_kg),
    c.weight.change_kg === null ? "3 weigh-ins needed for a trend" :
      `${c.weight.change_kg > 0 ? "+" : ""}${num(c.weight.change_kg * weightFactor(), 1)} ${weightUnit()} over ${data.weeks} weeks`));
  if (c.steps) cards.append(progressCard("Steps",
    c.steps.avg === null ? plural(c.steps.days_read, "reading") : fmtInt(c.steps.avg),
    c.steps.avg === null ? "3 readings needed for an average" :
      `${plural(c.steps.days_read, "reading")} in the last 7 days`));
  // done is counted from the watch: unworn for a fortnight, the card says what is planned
  const unworn = (data.sync || {}).activity_days === null || (data.sync || {}).activity_days > 14;
  if (c.training) cards.append(unworn && !c.training.done
    ? progressCard("Training", `${c.training.planned} planned`, "done is counted from the watch")
    : progressCard("Training", `${c.training.done} of ${c.training.planned}`,
      `${fmtInt(c.training.minutes)} moving minutes this week`));
  if (c.sleep) cards.append(progressCard("Sleep",
    c.sleep.avg_h === null ? plural(c.sleep.days_read, "reading") : `${num(c.sleep.avg_h, 1)} h`,
    c.sleep.avg_h === null ? "3 readings needed for an average" :
      `${plural(c.sleep.days_read, "reading")} in the last 7 days`));
  if (c.recovery) cards.append(progressCard("Recovery",
    c.recovery.resting_hr === null ? `${num(c.recovery.hrv)} HRV` : `${num(c.recovery.resting_hr)} bpm`,
    c.recovery.hrv === null ? "Resting heart rate" : `HRV ${num(c.recovery.hrv)}`));
  if (c.plan) cards.append(progressCard("Planned", `${fmtInt(c.plan.avg_kcal)} kcal`,
    c.plan.target_kcal ? `a day; target ${fmtInt(c.plan.target_kcal)} · P ${fmtInt(c.plan.avg_protein_g)} g`
      : `a day over ${plural(c.plan.days_planned, "day")}`));
  if (c.food) cards.append(progressCard("Food", plural(c.food.dishes, "dish"),
    c.food.top.length ? `${c.food.rated} rated; top: ${c.food.top[0].name} (${c.food.top[0].score}/5)` :
      "No dishes rated yet"));
  body.append(cards);
  const hasData = cards.childElementCount > 0 || Object.values(data.charts).some(Boolean);
  if (!hasData) {
    body.append(el("p", { class: "muted", text:
      "Nothing to show yet: weigh-ins, steps, training and menus appear here as they arrive." }));
    return;
  }
  // coverage, said plainly - never a streak or a score - and the average only when the record
  // is there to average
  const average = c.recorded.avg_kcal === null || c.recorded.avg_kcal === undefined ? ""
    : `, averaging ${fmtInt(c.recorded.avg_kcal)} kcal`
      + (c.recorded.target_kcal ? ` against a ${fmtInt(c.recorded.target_kcal)} target` : "");
  body.append(el("p", { class: "progress-recorded", text:
    `Intake recorded on ${c.recorded.days} of ${plural(c.recorded.of, "day")} `
    + `${data.week.is_current ? "so far this week" : "that week"}${average}.` }));
  const table = el("table", { class: "progress-days" });
  table.append(el("caption", { text: "Planned kcal; an asterisk marks steps at or over the floor." }));
  table.append(el("thead", {}, el("tr", {}, ["Day", "Plan", "Record", "Steps", "Train", weightUnit()].map((h) =>
    el("th", { scope: "col", text: h })))));
  const tbody = el("tbody");
  for (const d of data.days) {
    const step = d.steps === null || d.future ? "-" :
      `${fmtInt(d.steps)}${d.steps_floor !== null && d.steps >= d.steps_floor ? " *" : ""}`;
    tbody.append(el("tr", { class: d.future ? "progress-future" : "" },
      el("th", { scope: "row", text: dayName(d.day).split(" ")[0] }),
      el("td", { text: d.planned_kcal === null ? "-" : fmtInt(d.planned_kcal) }),
      el("td", { text: d.future ? "-" : d.recorded || (d.recorded_kcal !== null ? "recorded" : "-") }),
      el("td", { text: step }),
      el("td", { text: d.future ? (d.planned_training.join(", ") || "-") :
        (d.trained.join(", ") || "-") }),
      el("td", { text: d.future ? "-" : num(d.weight_kg === null ? null : d.weight_kg * weightFactor(), 1) })));
  }
  table.append(tbody);
  body.append(el("section", { class: "progress-day-section" },
    el("h2", { text: "The week, day by day" }), table));
  if (!Object.values(data.charts).some(Boolean)) return;
  await loadPlotly();
  if (outdated("progress", ticket)) return;
  const blocks = el("div", { class: "progress-charts" });
  body.append(blocks);
  await progressDrawCharts(blocks, data);
}

export async function progressDrawCharts(blocks, data) {
  const chart = data.charts;
  const jobs = [];
  const style = getComputedStyle(document.body);
  // the accent for what was measured, the ink for a line drawn through it, muted and dashed for a
  // reference (a floor, a target, a goal); amber stays the colour of a caution and is not used
  const colour = {
    accent: style.getPropertyValue("--accent").trim(),
    ink: style.getPropertyValue("--ink").trim(),
    soft: style.getPropertyValue("--muted").trim(),
  };
  if (chart.weight) {
    const w = chart.weight;
    const caption = weightUnit() === "kg" ? w.caption : w.caption.replace(/(\d+(?:\.\d+)?) kg/g,
      (_match, amount) => `${(Number(amount) * weightFactor()).toFixed(1)} lb`);
    const plot = progressChartBlock(blocks, "Weight", caption);
    const scaled = (values) => values.map((v) => v === null ? null : v * weightFactor());
    const traces = [
      { x: w.dates, y: scaled(w.raw), type: "scatter", mode: "markers", name: "Weigh-ins", marker: { color: colour.accent, size: 6, opacity: 0.55 } },
      { x: w.trend_dates, y: scaled(w.trend), type: "scatter", mode: "lines", name: "Trend", line: { color: colour.accent, width: 3 } },
    ];
    const values = [...scaled(w.raw), ...scaled(w.trend)].filter((v) => v !== null);
    const low = Math.min(...values), high = Math.max(...values), pad = Math.max(0.5, (high - low) * 0.2);
    const goal = w.goal_kg === null ? null : w.goal_kg * weightFactor();
    if (goal !== null && goal >= low - pad && goal <= high + pad) {
      traces.push({ x: [data.from, data.to], y: [goal, goal], type: "scatter",
        mode: "lines", name: "Goal", line: { dash: "dash", color: colour.soft } });
    }
    jobs.push(progressPlot(plot, traces, { xaxis: { range: [data.from, data.to] }, yaxis: { title: weightUnit() } }));
  }
  if (chart.steps) {
    const s = chart.steps;
    jobs.push(progressPlot(progressChartBlock(blocks, "Steps", s.caption), [
      { x: s.dates, y: s.raw, type: "bar", name: "Steps", marker: { color: colour.accent, opacity: 0.55 } },
      { x: s.dates, y: s.avg7, type: "scatter", mode: "lines", name: "7-day average", line: { color: colour.ink, width: 2 } },
      { x: s.dates, y: s.floor, type: "scatter", mode: "lines", name: "Floor", line: { color: colour.soft, dash: "dash" } },
    ], { yaxis: { title: "steps" } }));
  }
  if (chart.training) {
    const t = chart.training;
    const hover = t.weeks.map((_, i) => `${t.done[i]} done of ${t.planned[i]} planned`);
    // the done/planned words are for the hover only: drawn in the bar they were a vertical
    // smudge in a 20 px column
    const kinds = Object.entries(t.minutes_by_kind).map(([kind, minutes]) => ({ x: t.weeks, y: minutes,
      type: "bar", name: kind, text: hover, textposition: "none",
      hovertemplate: "%{x}<br>%{y} min<br>%{text}<extra>%{fullData.name}</extra>" }));
    const plot = progressChartBlock(blocks, "Training", t.caption);
    if (kinds.length) jobs.push(progressPlot(plot, kinds, { barmode: "stack", yaxis: { title: "minutes" } }));
    else {
      plot.classList.add("progress-no-plot");
      plot.append(el("p", { class: "muted", text: "No activity minutes recorded yet." }));
    }
  }
  if (chart.recovery) {
    const r = chart.recovery;
    jobs.push(progressPlot(progressChartBlock(blocks, "Recovery", r.caption), [
      { x: r.dates, y: r.sleep_h, type: "bar", name: "Sleep h", marker: { color: colour.soft }, opacity: 0.4 },
      { x: r.dates, y: r.resting_hr, type: "scatter", mode: "lines+markers", name: "Resting HR", yaxis: "y2", line: { color: colour.accent } },
      { x: r.dates, y: r.hrv, type: "scatter", mode: "lines+markers", name: "HRV", yaxis: "y2", line: { color: colour.ink } },
    ], { yaxis: { title: "sleep h" }, yaxis2: { overlaying: "y", side: "right", title: "bpm / ms" } }));
  }
  if (chart.plan) {
    // the plan is built to the target, so its chart was a row of equal bars under a flat line:
    // the sentence says everything it could
    blocks.append(el("section", { class: "progress-chart" },
      el("h3", { text: "Plan" }), el("p", { class: "muted", text: chart.plan.caption })));
  }
  await Promise.all(jobs);
}
