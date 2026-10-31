import { $, addDays, api, el, fail, fmtStamp, kg, state, weightFactor, weightUnit } from "./helpers.js";
import { batchFrom } from "./cook.js";
import { todayShowing } from "./today.js";

/* ---------------------------------------------------------------- trends */
export function plotTheme(yTitle) {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const ink = dark ? "#e7ebee" : "#14171a";
  const line = dark ? "#2a3138" : "#dfe3e7";
  // the same chart draws weight in kg, waist in cm and steps as a bare count and named none of
  // them - a screenshot sent to a coach was three unlabelled numbers (test_02)
  const yaxis = { gridcolor: line, zerolinecolor: line };
  if (yTitle) yaxis.title = { text: yTitle };
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: ink, family: "system-ui, sans-serif", size: 12 },
    margin: { l: 44, r: 12, t: 10, b: 36 },
    xaxis: { gridcolor: line, zerolinecolor: line, tickformat: "%-d %b", hoverformat: "%a %-d %b" },
    yaxis,
    showlegend: true,
    legend: { orientation: "h", y: -0.2 },
  };
}

// the note under the chart: the one sentence a year of pixel-wide bars, a month of step dots or
// a goal line drawn entirely in the past cannot say by itself - always server-computed (queries
// .trend()), never re-derived here, so the words on the page always match the numbers behind it
export function trendsNote(metric, data) {
  if (metric === "intake" && data.summary) return data.summary;
  if (metric === "steps" && data.mean !== null && data.mean !== undefined) {
    const total = (data.raw || []).length;
    return `Cleared the floor on ${data.days_over} of ${total} days - averaging ${data.mean.toLocaleString()} steps.`;
  }
  if (metric === "weight" && data.projection && data.projection.caption) {
    const caption = data.projection.caption;
    return weightUnit() === "kg" ? caption : caption.replace(/(\d+(?:\.\d+)?) kg/g,
      (_match, amount) => `${(Number(amount) * weightFactor()).toFixed(1)} lb`);
  }
  return "";
}

// Fetch the chart library on demand so other tabs do not pay its loading cost.
export let plotlyLoading = null;
export function loadPlotly() {
  if (window.Plotly) return Promise.resolve();
  if (!plotlyLoading) {
    plotlyLoading = new Promise((resolve, reject) => {
      const meta = document.querySelector('meta[name="tiffin-plotly"]');
      if (!meta) { reject(new Error("The chart library is missing from the page.")); return; }
      const tag = document.createElement("script");
      tag.src = meta.content;
      tag.onload = () => resolve();
      tag.onerror = () => {
        plotlyLoading = null;  // a dropped connection is tried again on the next open
        reject(new Error("The chart library did not load. Open Trends again to retry."));
      };
      document.head.append(tag);
    });
  }
  return plotlyLoading;
}

export async function renderTrends() {
  const metric = $("#metric").value;
  const days = $("#days").value;
  const [data] = await Promise.all([api(`api/trends?metric=${metric}&days=${days}`), loadPlotly()]);
  const note = $("#trends-note");
  const traces = [];
  const accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#2f6f4f";
  const muted = getComputedStyle(document.body).getPropertyValue("--muted").trim() || "#5b6570";
  if (metric === "intake" || metric === "sessions") {
    // a day the calibration never confirmed (closed on the menu, away, unconfirmed) drew
    // exactly like a day he logged; greying it out says which bars the average is allowed to
    // trust (test_03) - sessions has no status per week, so it keeps one flat colour
    const barColor = metric === "intake" && data.status
      ? data.status.map((s) => (s === "confirmed" ? accent : muted))
      : accent;
    traces.push({ x: data.dates, y: data.raw, type: "bar", name: metric === "intake" ? "eaten" : "done",
      marker: { color: barColor } });
    if (data.target) {
      traces.push({ x: data.dates, y: data.target, type: "scatter", mode: "lines",
        name: metric === "intake" ? "target" : "planned", line: { dash: "dot", width: 2 } });
    }
  } else {
    traces.push({ x: data.dates, y: data.raw, type: "scatter", mode: "markers", name: "measured",
      marker: { size: 6, color: accent, opacity: 0.55 } });
    if (data.ewma && data.ewma.length) {
      traces.push({ x: data.ewma_dates, y: data.ewma, type: "scatter", mode: "lines", name: "trend",
        line: { width: 3, color: accent } });
    }
    if (data.target && data.target.length) {
      traces.push({ x: data.dates, y: data.target, type: "scatter", mode: "lines", name: "floor",
        line: { dash: "dot", width: 2 } });
    }
    if (data.goal && data.dates.length) {
      traces.push({ x: [data.dates[0], data.dates[data.dates.length - 1]], y: [data.goal, data.goal],
        type: "scatter", mode: "lines", name: "goal", line: { dash: "dash", width: 2 } });
      // the flat goal line above says only the number, never whether he is ahead of it or
      // behind - this second, short line carries the trend's own slope on to where it is
      // projected to actually cross the goal (test_10), the eta queries.trend() already worked out
      if (data.eta && data.ewma && data.ewma.length) {
        const last = data.ewma.length - 1;
        traces.push({
          x: [data.ewma_dates[last], data.eta], y: [data.ewma[last], data.goal],
          type: "scatter", mode: "lines", name: "projected",
          line: { dash: "dot", width: 2, color: accent },
        });
      }
    }
    // waist and hip are measured in the same breath - one /measure message, two numbers - so
    // the natural partner rides the same chart rather than needing a second tab and a memory
    // of the first one (test_05)
    if (data.compare && data.compare.length) {
      traces.push({ x: data.dates, y: data.compare, type: "scatter", mode: "lines+markers",
        name: data.compare_metric || "compare", marker: { size: 5, opacity: 0.7 },
        line: { width: 2, dash: "dot" } });
    }
  }
  if (!traces.some((t) => (t.y || []).some((v) => v !== null && v !== undefined))) {
    // purge first: replacing the children of a div Plotly still owns left it in a state where
    // the next Plotly.react threw, so picking a metric with no data broke the tab until reload
    try { window.Plotly.purge($("#chart")); } catch (e) { /* nothing was plotted yet */ }
    if (note) { note.hidden = true; note.textContent = ""; }
    // steps named on its own: a person with no watch and no intervals.icu key still has a
    // floor set, and the generic sentence said nothing about where a reading would come from
    if (metric === "steps") {
      $("#chart").replaceChildren(el("p", { class: "muted",
        text: "Nothing recorded for steps yet - no intervals.icu key, or nothing synced." }));
    } else {
      $("#chart").replaceChildren(el("p", { class: "muted", text: "Nothing recorded for this metric yet." }));
    }
    return;
  }
  if (metric === "weight" && weightUnit() === "lb") {
    for (const trace of traces) trace.y = trace.y.map((v) => typeof v === "number" ? v * weightFactor() : v);
  }
  const layout = plotTheme(metric === "weight" ? weightUnit() : data.unit);
  if (!["intake", "sessions", "steps"].includes(metric)) {
    // weights and tape measures: points above the top tick read as off the chart ("101.9" over
    // a last label of 100). A round step, and a range that puts a tick past both ends.
    const ys = traces.flatMap((t) => (t.y || []).filter((v) => typeof v === "number"));
    if (ys.length) {
      const lo = Math.min(...ys);
      const hi = Math.max(...ys);
      const span = Math.max(hi - lo, 1);
      const step = [0.5, 1, 2, 5, 10, 20].find((d) => span / d <= 8) || 50;
      layout.yaxis.range = [Math.floor(lo / step) * step - step / 2, Math.ceil(hi / step) * step + step / 2];
      layout.yaxis.dtick = step;
    }
  }
  if (metric === "steps") {
    // a finger dragged across a 320 px chart wedged into a scrolling phone page defaults to a
    // zoom-box, not a page-scroll, and with the modebar off there is no reset-axes button to
    // undo it - steps is the metric most often read one-handed while scrolling past it, and at
    // a year wide it is drawn as bare dots five pixels apart (phone_layout/06)
    layout.dragmode = false;
  }
  window.Plotly.react($("#chart"), traces, layout, { displayModeBar: false, responsive: true });
  if (note) {
    const text = trendsNote(metric, data);
    note.textContent = text;
    note.hidden = !text;
  }
}

/* ---------------------------------------------------------------- summary */
// the Monday of the week asked for, persisted across a re-render like batchFrom/todayShowing;
// null means "today's week", the tab's own default
export let summaryWeek = null;

export async function renderSummary() {
  const data = await api(`api/summary/weekly${summaryWeek ? `?week=${summaryWeek}` : ""}`);
  const body = $("#summary-body");
  body.replaceChildren();
  // "down 0.3 kg" only means something against last week's number, and the two are one query
  // apart - the tab only ever asked for today's, so comparing meant scrolling Telegram back
  // seven days (dishes_summary/10)
  const nav = el("div", { class: "row" });
  const prevBtn = el("button", { type: "button", text: "‹ Previous week" });
  prevBtn.addEventListener("click", () => {
    summaryWeek = addDays(data.week_start, -7);
    renderSummary().catch((e) => fail(e.message));
  });
  nav.append(prevBtn);
  if (summaryWeek) {
    const nextBtn = el("button", { type: "button", text: "Next week ›" });
    nextBtn.addEventListener("click", () => {
      summaryWeek = addDays(data.week_start, 7);
      renderSummary().catch((e) => fail(e.message));
    });
    const thisBtn = el("button", { type: "button", text: "This week" });
    thisBtn.addEventListener("click", () => {
      summaryWeek = null;
      renderSummary().catch((e) => fail(e.message));
    });
    nav.append(nextBtn, thisBtn);
  }
  body.append(nav);
  // everything Tiffin holds about you, to keep: yours to read, whenever you like
  const exportLink = el("a", { href: "api/export", download: "", text: "Download everything Tiffin holds about you (JSON)" });
  const exportLine = el("p", { class: "muted export" }, exportLink);
  if (!data.text) {
    body.append(el("p", { class: "muted", text: "No Sunday summary yet. The first one is written on Sunday evening." }));
    body.append(exportLine);
    return;
  }
  if (!data.text.current) {
    /* Keep the last summary visible until the current week is written. */
    body.append(el("p", { class: "muted", text: "This week's summary is written on Sunday evening. The last one:" }));
  }
  // `.sent` keeps the newlines: a Telegram message breaks its lines with real newline
  // characters rather than <br>, and dropped into HTML they collapsed - the Sunday write-up
  // arrived on the page as one unbroken wall of prose nobody would read standing up
  const div = el("div", { class: "sent" });
  div.innerHTML = data.text.text_html; /* our own message, rendered as it was sent */
  body.append(div);
  body.append(el("p", { class: "muted", text: `Written ${fmtStamp(data.text.sent_at)}` }));
  body.append(exportLine);
}
