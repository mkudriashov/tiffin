
/* tiffin page. Vanilla, relative URLs through <base>, and every number comes from the API. */


export const state = { csrf: null, person: null, tz: "UTC", units: { weight: "kg", height: "cm", currency: "GBP" }, today: null, loaded: {}, restoring: false, daysChosen: false };

export const $ = (sel) => document.querySelector(sel);
export const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

export let failTimer = null;

export function fail(message) {
  // a plain string is a read that failed - six seconds and it is gone, nobody needs it back.
  // Failed writes keep their retry notice visible. Only the latest ordinary notice has a timer.
  const opts = typeof message === "string" ? { message } : message;
  const box = $("#error");
  if (failTimer) {
    clearTimeout(failTimer);
    failTimer = null;
  }
  box.textContent = opts.message;
  if (opts.retry) {
    const btn = el("button", { type: "button", text: "Retry" });
    btn.addEventListener("click", () => {
      box.hidden = true;
      opts.retry();
    });
    box.append(" ", btn);
  }
  const close = el("button", { type: "button", text: "Close", "aria-label": "Dismiss" });
  close.addEventListener("click", () => { box.hidden = true; });
  box.append(" ", close);
  box.hidden = false;
  if (!opts.retry) {
    failTimer = setTimeout(() => { box.hidden = true; failTimer = null; }, 6000);
  }
}

export async function api(path, options = {}) {
  const opts = { credentials: "same-origin", headers: {}, ...options };
  if (opts.body) {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRF-Token"] = state.csrf || "";
    opts.body = JSON.stringify(opts.body);
    opts.method = opts.method || "POST";
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (e) {
    // "Failed to fetch" is the browser talking; the person needs to know what it means
    const err = new Error("Could not reach Tiffin. Check your connection and try again.");
    err.status = 0;
    throw err;
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* keep status */ }
    // the server's sentence, not "500: ..." - and never markup shown as text
    const plain = String(detail).replace(/<[^>]*>/g, "").replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">").replace(/&amp;/g, "&");
    // the status travels with it: a 4xx is an answer (a refusal worth reading, not retrying),
    // a 5xx or no answer at all is a fault worth trying again
    const err = new Error(response.status >= 500 ? `Something went wrong on the box: ${plain}` : plain);
    err.status = response.status;
    throw err;
  }
  return response.json();
}

export const num = (v, digits = 0) =>
  v === null || v === undefined ? "-" : Number(v).toFixed(digits);
export const weightFactor = () => state.units.weight === "lb" ? 2.20462262 : 1;
export const weightUnit = () => state.units.weight === "lb" ? "lb" : "kg";
export const kg = (v) => (v === null || v === undefined ? "-" : `${(Number(v) * weightFactor()).toFixed(1)} ${weightUnit()}`);
export const dayName = (iso) =>
  new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
// "1 box", "5 boxes" - a naive +"s" reads as a machine talking (mirrors render.py's `plural`).
export const ES_ENDINGS = ["x", "s", "z", "ch", "sh"];
export const plural = (n, one) =>
  `${n} ${one}${n === 1 ? "" : ES_ENDINGS.some((e) => one.endsWith(e)) ? "es" : "s"}`;
// A number typed on a phone, as the digits to read ("" for a box left empty). "3,5" is three and
// a half on a keyboard whose decimal key is a comma, but "1,000" is a thousand: commas before
// groups of exactly three digits are thousands, and reading that one as a decimal point stored a
// 1 kg bag as a 1 g pack - and the list then asked for 300 of them. Units and spaces go.
export function decimal(text) {
  const s = String(text).replace(/[^\d.,]/g, "");
  if (/^[1-9]\d{0,2}(,\d{3})+(\.\d*)?$/.test(s)) return s.replace(/,/g, "");
  return s.replace(",", ".").replace(/,/g, "");
}

/* ------------------------------------------------ one display layer
   The page printed what the database holds: "pre_bed: ...", "unconfirmed", "segment",
   "sent 2026-09-20 18:00:03" (a UTC stamp, an hour off in summer) and "12800" beside "1,200".
   Every word and number a person reads goes through one of these instead. */
export const SLOT_LABEL = {
  breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", snack: "Snack", pre_bed: "Pre-bed",
};
export const STATUS_LABEL = {
  confirmed: "recorded", unconfirmed: "not answered", paused: "paused", open: "open",
};
export const slotLabel = (s) => SLOT_LABEL[s] || String(s || "").replace(/_/g, " ");
export const statusLabel = (s) => STATUS_LABEL[s] || String(s || "").replace(/_/g, " ");
export const fmtInt = (v) => (v === null || v === undefined ? "-" : Math.round(Number(v)).toLocaleString("en-GB"));
export function fmtStamp(utc) {
  if (!utc) return "";
  const d = new Date(String(utc).replace(" ", "T") + "Z");
  if (isNaN(d)) return String(utc);
  return d.toLocaleString("en-GB", { timeZone: state.tz, weekday: "short", day: "numeric",
    month: "short", hour: "2-digit", minute: "2-digit" });
}
export function fmtTime(utc) {
  if (!utc) return "";
  const d = new Date(String(utc).replace(" ", "T") + "Z");
  return isNaN(d) ? "" : d.toLocaleTimeString("en-GB", { timeZone: state.tz, hour: "2-digit", minute: "2-digit" });
}

export const addDays = (iso, n) => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
// mirrors prefs.days_words in Python: "Mon to Wed 16 Sep" for a run of days, "Mon, Tue and Thu
// 17 Sep" for days with a gap - a span over the gap ("Mon to Thu") read as if the day missing
// from it still held the dish. Undated, the last day is a weekday too ("Mon and Wed").
export const weekday = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short" });
export function daysWords(days, dated = true) {
  const all = [...days].sort();
  if (!all.length) return "";
  const last = (dated ? dayName : weekday)(all[all.length - 1]);
  if (all.length === 1) return last;
  const run = all.every((d, i) => i === 0 || addDays(all[i - 1], 1) === d);
  return run ? `${weekday(all[0])} to ${last}` : `${all.slice(0, -1).map(weekday).join(", ")} and ${last}`;
}

// mirrors menuflow.counted() in Python: "2 eggs" for 116 g out of a box of six, so a person
// standing at a hob with no scales reads a whole-unit count rather than a gram figure to weigh
// out - kept in step with the server function rather than imported, the way plural() above
// already mirrors render.py's rather than the page importing telegram code
export const COUNTED_PACK = /^(\d+)\s+([a-z]+)$/;
export const NOT_COUNTS = new Set(
  ["g", "kg", "ml", "l", "litre", "litres", "pack", "tub", "bag", "jar", "tin", "bottle"]
);
export const singularUnit = (word) => {
  if (["oes", "ches", "shes", "xes"].some((e) => word.endsWith(e))) return word.slice(0, -2);
  return word.endsWith("s") ? word.slice(0, -1) : word;
};
export function counted(grams, packG, packLabel) {
  const m = COUNTED_PACK.exec(String(packLabel || "").trim().toLowerCase());
  if (!m || !packG || packG <= 0) return "";
  const n = Number(m[1]);
  const unit = m[2];
  if (n < 2 || NOT_COUNTS.has(unit)) return "";
  const many = grams / (packG / n);
  const whole = Math.round(many);
  if (whole < 1 || Math.abs(many - whole) > 0.2 * whole) return "";
  return `${whole} ${whole === 1 ? singularUnit(unit) : unit}`;
}

// one plate's grams of an ingredient: scaled, then a food served whole put down in whole ones -
// the rule the day's numbers were counted by (`solver.whole_units`): 30 g of a 40 g slice is one
export function plateGrams(ing, m, servings) {
  const g = (ing.grams * (m || 1)) / (servings || 1);
  const u = ing.unit_g;
  if (!u) return Math.round(g);
  const n = Math.floor(g / u + 0.5);
  if (n >= 1) return Math.round(n * u);
  return Math.round(g >= 0.35 * u ? u : g);
}

// "2 slices" for a food served whole at a whole number of them, "" otherwise
export function unitWords(ing, grams) {
  if (!ing.unit_g || !ing.unit_name) return "";
  const n = grams / ing.unit_g;
  const whole = Math.round(n);
  if (whole < 1 || Math.abs(n - whole) > 0.01) return "";
  return `${whole} ${whole === 1 ? ing.unit_name : `${ing.unit_name}s`}`;
}

export function card(k, v, s, ratio, onClick) {
  const kids = [el("div", { class: "k", text: k }), el("div", { class: "v", text: v })];
  if (s) kids.push(el("div", { class: "s", text: s }));
  if (typeof ratio === "number" && isFinite(ratio)) {
    const bar = el("div", { class: ratio > 1.05 ? "bar over" : "bar" });
    bar.append(el("i", { style: `width:${Math.min(Math.max(ratio, 0), 1) * 100}%` }));
    kids.push(bar);
  }
  const node = el("div", { class: onClick ? "card clickable" : "card" }, kids);
  if (onClick) {
    // Today's Steps card is a div, not a link, so the chart it hints at was three taps away
    // with nothing on the card saying it existed - a keyboard activation works the same as a tap
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.addEventListener("click", onClick);
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onClick();
      }
    });
  }
  return node;
}
