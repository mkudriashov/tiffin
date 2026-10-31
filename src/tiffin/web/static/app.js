import { $, addDays, api, card, counted, dayName, el, fail, fmtStamp, plural, state, weekday } from "./helpers.js";
import { closeCook, cook, cookNext, cookPrev, renderBatch, wakeUp } from "./cook.js";
import { renderChoose } from "./choose.js";
import { renderDishes } from "./recipes.js";
import { renderMenu } from "./menu.js";
import { renderMore } from "./more.js";
import { renderProgress } from "./progress.js";
import { renderShopping } from "./shopping.js";
import { renderSummary, renderTrends } from "./progress_page.js";
import { renderToday, todayShowing, setTodayShowing } from "./today.js";

/* ---------------------------------------------------------------- shell */
// Five destinations: Today, Menu, Shopping, Progress and More. Eight
// tabs in one sideways row hid half of them on a phone. Menu is three views - the days, the cook
// and the recipe book - each its own section with its own address, so ?tab=batch from the bot
// still opens the cook.
export const RENDER = {
  today: renderToday,
  menu: renderMenu,
  batch: renderBatch,
  dishes: renderDishes,
  shopping: renderShopping,
  progress: renderProgressTab,
  more: renderMore,
  // drawn by choose.js, loaded after this file: looked up when the view opens
  choose: () => (typeof renderChoose === "function" ? renderChoose() : Promise.resolve()),
};

// tabs this page no longer has, named by links already sent: each opens the part of Progress
// that took its place
export const ALIASES = { week: "progress", trends: "progress", summary: "progress" };

// which of the five the bar lights for each section
export const NAV_OF = {
  today: "today", menu: "menu", batch: "menu", dishes: "menu", choose: "menu",
  shopping: "shopping", progress: "progress", more: "more",
};

export const TAB_TITLES = {
  today: "Today", menu: "Menu", batch: "Cook", dishes: "Recipes", choose: "Choose",
  shopping: "Shopping", progress: "Progress", more: "More",
};

export async function show(name, params) {
  // an old address (Week, Trends, Summary) opens the part of Progress it named
  const named = name;
  if (ALIASES[name]) name = ALIASES[name];
  const moved = state.tab !== undefined && state.tab !== name;
  state.tab = name;
  for (const section of document.querySelectorAll(".tab")) section.classList.toggle("on", section.id === name);
  const lit = NAV_OF[name] || name;
  for (const button of document.querySelectorAll("#tabs button")) {
    const on = button.dataset.tab === lit;
    button.classList.toggle("on", on);
    // plain buttons that say which page is open: the tablist roles promised arrow keys and a
    // roving focus that were never wired
    if (on) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  // Days, Cook and Recipes: one row of choices over whichever of the three is open
  const views = $("#menu-views");
  views.hidden = lit !== "menu";
  for (const b of views.querySelectorAll("button")) {
    const on = b.dataset.view === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  // Choose is there for a person who picks their weekday meals, or on the way to it from a link
  chooseButton(name === "choose");
  // the box's status is a Today thing; on every tab it pushed the content a screen down
  const strip = $("#status-strip");
  if (strip) strip.hidden = name !== "today" || !strip.childElementCount;
  try {
    localStorage.setItem("tiffin.tab", name);
  } catch (e) { /* private mode: the tab just does not persist */ }
  // every tab read "tiffin": eleven tabs open on the laptop all looked the same, and a
  // bookmark of Menu was indistinguishable from a bookmark of Today
  document.title = TAB_TITLES[name] ? `tiffin - ${TAB_TITLES[name]}` : "tiffin";
  if (name === "progress") {
    // the Sunday summary and the longer charts are folded; a link to either opens it
    if (named === "summary") $("#progress-sunday").open = true;
    if (named === "trends" || (params && params.metric)) $("#progress-explore").open = true;
  }
  // a new destination starts at its top: the bar is at the bottom now, and a long list left
  // half-scrolled made the next tab open somewhere in its middle
  if (moved) window.scrollTo(0, 0);
  // a popstate-driven call already matches the address bar - pushing again here would mean
  // the back button never actually goes back, it just pushes a forward entry over itself
  if (!state.restoring) {
    const url = new URL(location.href);
    url.search = "";
    url.searchParams.set("tab", name);
    // a day and a meal to open on arrival - "Open in Menu" from Today on any day but today used
    for (const [k, v] of Object.entries(params || {})) if (v) url.searchParams.set(k, v);
    const exploring = name === "progress" && $("#progress-explore").open;
    if (exploring) {
      url.searchParams.set("metric", $("#metric").value);
      url.searchParams.set("days", $("#days").value);
    }
    // the metric and the days are in the address, so they have to be in the state too, or Back
    // rewrites the URL and redraws the same chart from the untouched selects
    history.pushState(
      exploring ? { tab: name, metric: $("#metric").value, days: $("#days").value } : { tab: name },
      "",
      url,
    );
  }
  // Mark the tab busy while its content loads, both visibly and for assistive technology.
  const section = document.getElementById(name);
  if (section) section.setAttribute("aria-busy", "true");
  try {
    await RENDER[name]();
    lastDrawn = Date.now();
  } catch (e) {
    fail(e.message);
  } finally {
    if (section) section.removeAttribute("aria-busy");
  }
}

// a card on Today naming where it leads - same function as a plain tab click, with an optional
// metric for the one card (Steps) that points at a specific chart, opened on Progress
export function showTab(name, metric) {
  if (metric) {
    $("#metric").value = metric;
    // the same guard the metric select applies: 90 days of steps is about 5 px a point on a
    // phone, and this card is the route that exists to make the chart reachable from one
    if (metric === "steps" && !state.daysChosen) $("#days").value = "30";
    $("#progress-explore").open = true;
  }
  show(name).catch((e) => fail(e.message));
}

window.addEventListener("popstate", (event) => {
  // Restore the selected tab when browser history moves.
  const name = (event.state && event.state.tab) || "today";
  if (event.state && event.state.metric) {
    $("#metric").value = event.state.metric;
    $("#progress-explore").open = true;
  }
  if (event.state && event.state.days) $("#days").value = event.state.days;
  state.restoring = true;
  show(RENDER[name] || ALIASES[name] ? name : "today").finally(() => { state.restoring = false; });
});

// Progress: the overview drawn by progress.js, then the Sunday summary and one measure over a
// longer time, each drawn only while it is open - a folded chart costs nothing
export async function renderProgressTab() {
  const jobs = [];
  if (typeof renderProgress === "function") jobs.push(renderProgress());
  if ($("#progress-sunday").open) jobs.push(renderSummary());
  if ($("#progress-explore").open) jobs.push(renderTrends());
  await Promise.all(jobs);
}

// the chart's own address, kept up to date without a history entry per select change
export function exploreAddress() {
  if (state.restoring || state.tab !== "progress") return;
  const url = new URL(location.href);
  url.search = "";
  url.searchParams.set("tab", "progress");
  if ($("#progress-explore").open) {
    url.searchParams.set("metric", $("#metric").value);
    url.searchParams.set("days", $("#days").value);
  }
  history.replaceState({ tab: "progress", metric: $("#metric").value, days: $("#days").value }, "", url);
}

// Menu's Choose view, for a person with weekly choices on (and whoever a link brings to it)
export function chooseButton(force = false) {
  const b = document.querySelector('#menu-views button[data-view="choose"]');
  if (b) b.hidden = !(force || state.choices > 0);
  const views = $("#menu-views");
  if (views) views.style.gridTemplateColumns = `repeat(${b && !b.hidden ? 4 : 3}, minmax(0, 1fr))`;
}

// Keep status notices visible long enough to be read after a refresh.
export const SEEN_FOR_MS = 7 * 86400 * 1000;
export function seenKinds() {
  try {
    return JSON.parse(localStorage.getItem("tiffin.status-seen") || "{}") || {};
  } catch (e) {
    return {};
  }
}
export function unseen(items) {
  const seen = seenKinds();
  return items.filter((i) => !(seen[i.what] && Date.now() - seen[i.what] < SEEN_FOR_MS));
}
export function markSeen(items) {
  const seen = seenKinds();
  for (const i of items) seen[i.what] = Date.now();
  // a kind not seen for a month is forgotten, so the store never grows
  for (const [k, t] of Object.entries(seen)) if (Date.now() - t > 30 * 86400 * 1000) delete seen[k];
  try {
    localStorage.setItem("tiffin.status-seen", JSON.stringify(seen));
  } catch (e) { /* not kept: it shows again next time */ }
}

// What went wrong on the box lately - for the admin only (the API returns nothing to anyone
// else). The replacement for the ops DMs that reached his food chat at 23:30 and 05:30.
export async function renderStatus() {
  const strip = $("#status-strip");
  let data;
  try {
    data = await api("api/status");
  } catch (e) {
    return; // the strip is a courtesy; a page that failed to fetch it says nothing new
  }
  const fresh = unseen(data.items || []);
  const items = fresh.slice(0, 5);
  // the count is of everything not yet seen, not of the five shown - and the lines past the
  // eight the API sends are counted too, unseen or not: "5 problems" read as the whole story
  const total = fresh.length + Math.max((data.total || 0) - (data.items || []).length, 0);
  strip.replaceChildren();
  if (!items.length) {
    strip.hidden = true;
    return;
  }
  // one line until it is opened: on a phone the open list took a quarter of the screen
  const all = el("details", {},
    el("summary", { text: `On the box: ${plural(total, "problem")} in the last day` }));
  for (const i of items) {
    all.append(el("p", {}, `${fmtStamp(i.at)} · ${i.what}`,
      i.why ? el("span", { class: "muted", text: ` - ${i.why}` }) : null));
  }
  if (total > items.length) {
    all.append(el("p", { class: "muted", text: `and ${total - items.length} more` }));
  }
  const ok = el("button", { type: "button", class: "link", text: "Seen - quiet for a week" });
  ok.addEventListener("click", () => {
    markSeen(fresh);
    strip.replaceChildren();
    strip.hidden = true;
  });
  all.append(el("p", { class: "muted" }, ok, " The whole list stays under More."));
  strip.append(all);
  strip.hidden = state.tab !== "today";
}

export function bootFailed(message) {
  // the error bar hid itself after six seconds and boot() returned before a single tab was
  // wired, so a Tailscale hiccup at load left a blank page whose tabs did nothing. A panel that stays, says why, and tries again.
  const panel = $("#boot-failed");
  panel.replaceChildren(
    el("p", { text: "Could not open Tiffin." }),
    el("p", { class: "muted", text: message }),
  );
  const retry = el("button", { type: "button", class: "primary", text: "Try again" });
  retry.addEventListener("click", () => {
    panel.hidden = true;
    boot();
  });
  panel.append(retry);
  panel.hidden = false;
}

// a phone keeps the page open for days; "today" has to move when the day does
// a page left open on the phone: what changed in Telegram meanwhile (a /swap, a /cook, a list
// ticked by the other person) reached it only when the date changed. Coming back after a while,
// the open tab is drawn again where it was. Focus and visibility both fire on one return, so one
// refresh at a time.
export const STALE_AFTER_MS = 90 * 1000;
export let lastDrawn = Date.now();
export function setLastDrawn(value) { lastDrawn = value; }
export let refreshing = null;

// The boxes a person has typed in, moved or ticked since they were drawn. A tab drawn again throws
// that away, so the redraw on coming back waits while one of them still holds something not yet
// sent: the cooked weight typed at the hob, a slider not yet applied, a word half typed.
export const touched = new WeakSet();
document.addEventListener("input", (e) => touched.add(e.target), true);
// the shell's own controls (the date, the chart's pickers) have ids and are never drawn again;
// nothing a tab draws has one
export function unsent(name) {
  const section = document.getElementById(name);
  return !!section && [...section.querySelectorAll("input, select, textarea")].some((x) =>
    touched.has(x) && !x.id && (x.type === "text" ? x.value.trim() !== "" : true));
}

export async function redrawInPlace(name) {
  const body = document.getElementById(name);
  const open = body
    ? [...body.querySelectorAll("details[open]")].map((d) => d.id).filter(Boolean)
    : [];
  // a word typed into a named box and not yet sent goes back into the box drawn in its place,
  // with the focus if it had it: the end of a build draws the tab again under whoever is typing
  const typed = body
    ? [...body.querySelectorAll("input[name]")].filter((x) => x.value).map((x) => [x.name, x.value])
    : [];
  const focused = body && body.contains(document.activeElement) ? document.activeElement.name : "";
  const y = window.scrollY;
  if (name === "menu") await renderMenu({ stay: true });
  else await RENDER[name]();
  for (const id of open) {
    const d = document.getElementById(id);
    if (d) d.open = true;
  }
  for (const [n, v] of typed) {
    const x = body.querySelector(`input[name="${n}"]`);
    if (x && !x.value) x.value = v;
  }
  if (focused) body.querySelector(`input[name="${focused}"]`)?.focus({ preventScroll: true });
  window.scrollTo(0, y);
}

export function refreshToday() {
  if (!refreshing) refreshing = refreshNow().finally(() => { refreshing = null; });
  return refreshing;
}

export async function refreshNow() {
  let me;
  try {
    me = await api("api/me");
  } catch (e) {
    return; // the next visibility change tries again; a read failing here is not news
  }
  state.csrf = me.csrf;
  state.tz = me.tz;
  if (me.today === state.today) {
    renderStatus();
    const name = state.tab || "today";
    if (Date.now() - lastDrawn > STALE_AFTER_MS && ["menu", "shopping", "batch", "today"].includes(name)) {
      // something typed, moved or ticked on the tab and not yet sent is worth more than a fresher
      // draw: the redraw waits for a later return, or the next time the tab is opened
      if (unsent(name)) return;
      lastDrawn = Date.now();
      redrawInPlace(name).catch(() => { /* the tab as it was is still on screen */ });
    }
    return;
  }
  renderStatus();
  state.today = me.today;
  $("#stamp").textContent = dayName(me.today);
  const name = state.tab || "today";
  if (RENDER[name]) RENDER[name]().catch((e) => fail(e.message));
}

export let wired = false;

export async function boot() {
  try {
    const me = await api("api/me");
    state.csrf = me.csrf;
    state.tz = me.tz;
    state.person = me.person;
    state.units = me.units || state.units;
    state.today = me.today;
    state.choices = me.choices || 0;
    state.plan = me.plan || null;
    $("#who").textContent = me.person.display_name || me.person.slug;
    $("#stamp").textContent = dayName(me.today);
  } catch (e) {
    bootFailed(e.message);
    return;
  }
  if (wired) {
    // a retry after a failed start: everything below is already attached
    await show(state.tab || "today");
    return;
  }
  wired = true;
  renderStatus();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshToday();
  });
  window.addEventListener("focus", () => refreshToday());
  for (const button of document.querySelectorAll("#tabs button")) {
    button.addEventListener("click", () => show(button.dataset.tab));
  }
  for (const button of document.querySelectorAll("#menu-views button")) {
    button.addEventListener("click", () => {
      if (button.dataset.view !== state.tab) show(button.dataset.view);
    });
  }
  // The overlay uses separate tap zones for the previous and next step.
  $("#cook-left").addEventListener("click", cookPrev);
  $("#cook-right").addEventListener("click", cookNext);
  $("#cook-close").addEventListener("click", closeCook);
  document.addEventListener("keydown", (e) => {
    if (!cook) return;
    if (e.key === "Escape") closeCook();
    else if (e.key === "ArrowRight") cookNext();
    else if (e.key === "ArrowLeft") cookPrev();
    else if (e.key === "Tab") {
      // a modal keeps the focus inside it: Tab from Next walked out to the page behind the
      // overlay, where nothing could be seen
      const inside = [...$("#cook-overlay").querySelectorAll("button, summary, [tabindex]")]
        .filter((x) => !x.disabled && x.offsetParent !== null);
      if (!inside.length) return;
      const first = inside[0];
      const last = inside[inside.length - 1];
      if (e.shiftKey && (document.activeElement === first || !$("#cook-overlay").contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !$("#cook-overlay").contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    }
  });
  // a wake lock is dropped whenever the tab is hidden (a timer app, a text) and was never asked
  // for again, so the screen slept mid-recipe after the first glance away
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") wakeUp();
  });
  // Menu's row of views sticks to the top; headings a link scrolls to clear it, sized from the
  // row as it actually is rather than a guess (0 on the tabs that do not show it)
  const views = $("#menu-views");
  const navHeight = () => document.documentElement.style.setProperty("--nav-h", `${views.offsetHeight}px`);
  navHeight();
  if ("ResizeObserver" in window) new ResizeObserver(navHeight).observe(views);
  // the Sunday summary and the longer chart are drawn when opened, not before
  $("#progress-sunday").addEventListener("toggle", () => {
    if ($("#progress-sunday").open) renderSummary().catch((e) => fail(e.message));
  });
  $("#progress-explore").addEventListener("toggle", () => {
    if ($("#progress-explore").open) renderTrends().catch((e) => fail(e.message));
    exploreAddress();
  });
  $("#metric").addEventListener("change", () => {
    // 90 days of steps draws at about 5 px a point on a 372 px-wide chart (phone_layout/06);
    // default the window down to a month the first time Steps is opened, but never override a
    // window she has actually picked (for Steps or anything else)
    if ($("#metric").value === "steps" && !state.daysChosen) $("#days").value = "30";
    exploreAddress();
    renderTrends().catch((e) => fail(e.message));
  });
  $("#days").addEventListener("change", () => {
    state.daysChosen = true;
    exploreAddress();
    renderTrends().catch((e) => fail(e.message));
  });
  // reads and writes the address the same way a tab switch does, so a link to yesterday (sent
  // before midnight, read after) opens straight on the day it names
  const showDay = (iso) => {
    setTodayShowing(iso && iso !== state.today ? iso : null);
    const url = new URL(location.href);
    if (todayShowing) url.searchParams.set("date", todayShowing);
    else url.searchParams.delete("date");
    history.replaceState(null, "", url);
    renderToday().catch((e) => fail(e.message));
  };
  $("#day").addEventListener("change", () => showDay($("#day").value || null));
  // a day either side in one tap: the date box alone meant a calendar for "yesterday"
  $("#day-prev").addEventListener("click", () => showDay(addDays(todayShowing || state.today, -1)));
  $("#day-next").addEventListener("click", () => showDay(addDays(todayShowing || state.today, 1)));
  $("#day-today").addEventListener("click", () => showDay(null));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    // the charts on screen repaint in the new scheme: the overview, and the one measure if open
    if (state.tab !== "progress") return;
    if (typeof renderProgress === "function") renderProgress().catch((e) => fail(e.message));
    if ($("#progress-explore").open) renderTrends().catch((e) => fail(e.message));
  });
  let start = "today";
  try {
    const saved = localStorage.getItem("tiffin.tab");
    if (saved && RENDER[saved]) start = saved;
  } catch (e) { /* ignore */ }
  // a link from the bot names its tab; that beats whichever tab was open last time. Week,
  // Trends and Summary are parts of Progress now, and their old links still land there
  const asked = new URLSearchParams(location.search).get("tab");
  const params = new URLSearchParams(location.search);
  if (asked && (RENDER[asked] || ALIASES[asked])) start = asked;
  else if (asked) {
    // a link built by hand, or a future one, naming a tab that does not exist: landing on
    // whichever tab was open last with no word about it is the page degrading to silence
    fail(`"${asked}" is not a tab on this page. Showing ${TAB_TITLES[start]} instead.`);
  }
  if ((start === "trends" || start === "progress") && params.get("metric")) {
    $("#metric").value = params.get("metric");
    if (params.get("days")) {
      $("#days").value = params.get("days");
      state.daysChosen = true; // a link named its own window; the Steps default must not fight it
    }
    $("#progress-explore").open = true;
  }
  // the first render matches whatever the browser already shows - it does not need its own
  // history entry, only the cleanup below once the query has done its one job
  state.restoring = true;
  await show(start);
  state.restoring = false;
  // a link followed hours ago must not keep winning on every later refresh of the same tab:
  // ?day=/?slot=/?date= are one-shot, honoured once then stripped. The tab itself stays in the
  // address, so a refresh lands where the person is rather than on whatever was stored last
  if (location.search) history.replaceState({ tab: state.tab }, "", `${location.pathname}?tab=${state.tab}`);
}

boot();
