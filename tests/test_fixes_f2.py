"""Regression tests for Menu and Shopping page interactions."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tiffin import building
from tiffin.clock import isoday
from tiffin.food import shopping
from tiffin.web import api

from .test_swap import (  # noqa: F401
    MON,
    OWNER_LOGIN,
    PARTNER_LOGIN,
    _headers,
    _menu,
    _profile,
    page,
)
from .web_source import app_source

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _js() -> str:
    return app_source()


def _css() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def _block(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


def _regex(source: str, near: str) -> re.Pattern[str]:
    """The first /.../ literal after `near` in the script, compiled as Python reads it: the
    patterns tested here use nothing the two dialects spell differently."""
    at = source.index(near)
    found = re.search(r"/(\^[^/]+)/(i?)", source[at:])
    assert found, f"no pattern after {near!r}"
    return re.compile(found.group(1), re.I if found.group(2) else 0)


# ---------------------------------------------------------------- #21: "1,000" is a thousand
def test_21_a_thousands_comma_is_a_thousand_and_a_decimal_comma_a_point() -> None:
    """A pack typed "1,000" is 1000 g, while "8,1" is still eight point one."""
    js = _js()
    parse = _block(js, "decimal")
    thousands = _regex(parse, "function decimal(")
    assert thousands.match("1,000") and thousands.match("12,500") and thousands.match("1,000.5")
    assert (
        not thousands.match("8,1") and not thousands.match("3,5") and not thousands.match("0,500")
    )
    assert 'replace(",", ".")' in parse, "a lone comma is still a decimal point"
    form = _block(js, "foodForm")
    assert "decimal(pack.value)" in form and "decimal(inputs[f.key].value)" in form
    assert "decimal(cooked.value)" in _block(js, "cookedCalculator"), "the same parse at the hob"
    assert js.count('replace(",", ".")') == 1, "one parse for every number typed on the page"


def test_21_a_pack_of_a_gram_or_two_is_stored_with_a_word_about_it(page) -> None:  # noqa: F811
    """A pack under a few grams is said back as worth checking, not taken in
    silence.
    """
    client, _db, _partners, _his, _r = page
    food = {
        "name": "Jumbo oats", "kcal_100": 375, "protein_100": 11, "fat_100": 8.1,
        "carb_100": 60, "pack_label": "1 kg bag", "aisle": "grains",
    }  # fmt: skip
    tiny = client.post("/api/foods", json={**food, "pack_g": 1}, headers=_headers(client))
    assert tiny.status_code == 200, tiny.text
    assert "very small" in tiny.json()["warning"], tiny.json()
    real = client.post("/api/foods", json={**food, "pack_g": 1000}, headers=_headers(client))
    assert real.status_code == 200 and not real.json()["warning"], real.json()


# ---------------------------------------------------------------- #68: a dropped answer
def test_68_a_dropped_answer_while_waiting_is_looked_for_again() -> None:
    """A failed look at a build in flight arms the next look, later, instead of
    stopping.
    """
    js = _js()
    watch = _block(js, "watchBuild")
    assert "catch" in watch and "watchBuild(" in watch[watch.index("catch") :], "looks again"
    assert "Math.min(wait * 2" in watch, "and backs off while the tailnet is away"
    assert "watchBuild(" in _block(js, "rebuildPanel")
    swap = _block(js, "fillSwap")
    assert "the next open tries again" not in swap
    look = swap[swap.index("const look") :]
    assert "catch (e) { look(Math.min(wait * 2" in look, "the swap area looks again too"
    # ...and so does an ask whose own redraw was dropped (in a browser: test_68_an_ask_whose_...)
    asked = swap[swap.index('"api/menu/new-dish"') :]
    assert "look(15000, true);" in asked[asked.index("catch (e)", asked.index("newAsks.set(")) :]


# ---------------------------------------------------------------- #69: Just Mon stays Just Mon
def test_69_the_days_chosen_in_swap_survive_a_refill() -> None:
    """ "Just Mon" stays chosen when the swap area is drawn again by the ask, its
    look or a redraw: kept per meal, not on the area a redraw replaces (in a browser:
    test_104_69_the_result_and_the_day_chosen_survive_...).
    """
    js = _js()
    swap = _block(js, "fillSwap")
    assert "let everyDay = swapDays.has(meal) ? swapDays.get(meal)" in swap
    assert "swapDays.set(meal, value);" in swap
    assert 'class: everyDay ? "on" : ""' in swap and 'class: everyDay ? "" : "on"' in swap
    assert "let everyDay = true;" not in swap and "dataset.everyDay" not in js
    assert "swapDays.delete(" in _block(js, "swapMade"), "let go once a swap is made"
    # a look while the dishes are written leaves the area alone: an open food list stays open
    look = swap[swap.index("const look") :]
    assert "if (again.building && !draw) look(" in look


# ---------------------------------------------------------------- #71: the place is kept
def test_71_a_change_in_the_foods_panel_or_weekend_stays_there() -> None:
    """After a foods panel or weekend change the page stays by the panel that
    said it.
    """
    js = _js()
    menu = _block(js, "renderMenu")
    assert 'foodsSaid ? "foods" : weekendSaid ? "weekend"' in menu
    assert "opts.keepPlace ? () => {} : scrollToAsked" in menu, "kept place, never today's heading"
    assert 'id: "weekend"' in _block(js, "weekendPanel")
    assert "#weekend { scroll-margin-top: calc(var(--nav-h" in _css(), "clear of the tab row"
    swap = _block(js, "fillSwap")
    never = swap[swap.index('"Never again:"') :]
    assert "scrollIntoView" not in never, "the redraw's own anchor lands on the panel"


# ---------------------------------------------------------------- #72: no redraw while waiting
def test_72_waiting_for_a_build_redraws_only_its_banner() -> None:
    """A build in flight replaces only its banner; the tab is drawn again once,
    at the end.
    """
    js = _js()
    panel = _block(js, "rebuildPanel")
    assert "renderMenu(" not in panel[: panel.index("const panel")], "no whole-tab redraw"
    watch = _block(js, "watchBuild")
    assert "box.replaceWith(rebuildPanel(data))" in watch
    assert 'redrawInPlace("menu")' in watch
    again = _block(js, "redrawInPlace")
    assert "typed" in again and ".focus(" in again, "a word half typed is put back"


# ---------------------------------------------------------------- #74: a tick is the trolley
def test_74_the_check_at_home_section_never_asks_for_a_tick_for_what_is_at_home() -> None:
    """The long-life section asks for a cupboard check, not a tick that reads as
    packs bought.
    """
    js = _js()
    shop = _block(js, "renderShopping")
    assert "Tick what you already have" not in js
    section = shop[shop.index('"Check at home first"') :][:400]
    assert "in the trolley" in section, section
    assert "tick (bought, or already at home)" not in js


# ---------------------------------------------------------------- #104: the ask's result
def test_104_what_came_of_an_ask_is_kept_for_that_meal(page) -> None:  # noqa: F811
    """An ask's result is answered for the meal it was asked for, and only that
    meal.
    """
    client, _db, _partners, _his, _r = page
    h = _headers(client)
    asked = client.post("/api/menu/new-dish", json={"day": isoday(MON), "slot": "lunch"}, headers=h)
    assert asked.status_code == 200, asked.text
    lunch = client.get(
        "/api/menu/swap", params={"day": isoday(MON), "slot": "lunch"}, headers=h
    ).json()
    assert lunch["new_said"] and "No model" in lunch["new_said"], lunch["new_said"]
    tuesday = client.get(
        "/api/menu/swap", params={"day": "2026-09-15", "slot": "lunch"}, headers=h
    ).json()
    assert tuesday["new_said"] == lunch["new_said"], "the same pot, opened from another day"
    dinner = client.get(
        "/api/menu/swap", params={"day": isoday(MON), "slot": "dinner"}, headers=h
    ).json()
    assert dinner["new_said"] is None, "not said under a meal nobody asked about"


def test_104_what_came_of_an_ask_is_kept_before_the_build_is_over(page, monkeypatch) -> None:  # noqa: F811
    client, db, _partners, _his, _r = page
    tiffin = client.app.state.tiffin
    later: list = []
    tiffin.background = lambda _pid, fn: later.append(fn)
    asked = client.post(
        "/api/menu/new-dish", json={"day": isoday(MON), "slot": "lunch"}, headers=_headers(client)
    )
    assert asked.status_code == 200 and later, asked.text
    seen: list[str | None] = []
    real = building.done

    def done(bdb, person_id: int, *token: str) -> None:
        real(bdb, person_id, *token)
        # what a look for this meal reads the moment the build stops being in flight
        seen.append(api._new_said(bdb, person_id, "lunch", [isoday(MON)]))

    monkeypatch.setattr(building, "done", done)
    later[0](db)
    assert seen and seen[0] and "No model" in seen[0], seen


def test_104_the_result_is_said_wherever_that_meal_is_opened_next() -> None:
    """The ask is remembered past a redraw, and its meal opens again to say
    what came of it - kept once said, until it is closed or a swap is made (in a browser:
    test_104_69_the_result_and_the_day_chosen_survive_...).
    """
    js = _js()
    swap = _block(js, "fillSwap")
    assert "area.dataset.asked" not in js, "not kept on the one element a redraw replaces"
    assert "newAsks.set(" in swap and "swapMade(got.day, got.slot, got.days)" in swap
    assert "newAsks.delete(" in _block(js, "swapMade")
    assert "newAsks.get(asked)" in swap, "the dishes arrive under the days the ask was sent for"
    assert "did not finish" in swap, "a build that died is said too, never silence"
    assert "askOut.said = askOut.said ||" in swap, "kept with the ask, said again after a redraw"
    control = _block(js, "swapControl")
    assert "askedFor(dayIso, slot.slot" in control, "opened again after a redraw"
    assert "newAsks.delete(area.dataset.said)" in control, "let go once read and closed"
    assert "askedFor(day.day, slot.slot" in _block(js, "renderMenu"), "its card open too"


# ---------------------------------------------------------------- #105: never sent twice
def test_105_a_change_that_went_in_is_never_sent_again_by_retry() -> None:
    """When only the redraw fails, Retry draws again and never repeats the
    write.
    """
    js = _js()
    shown = _block(js, "showChange")
    assert "renderMenu({ keepPlace: true })" in shown and "retry: () => showChange(" in shown
    for name, write in (
        ("fillSwap", '"api/menu/swap"'),
        ("fillSwap", '"api/prefs/food"'),
        ("fillSubst", '"api/menu/swap-food"'),
        ("sizeRow", '"api/menu/plates"'),
        ("foodsPanel", '"api/prefs"'),
        ("foodsPanel", '"api/prefs/food"'),
        ("foodsPanel", '"api/menu/swap-avoided"'),
        ("weekendPanel", '"api/weekend/mode"'),
    ):
        body = _block(js, name)
        after = body[body.index(write) :]
        tail = after[: after.index("showChange(")]
        assert "renderMenu(" not in tail, f"{name}: {write} still redraws inside its own try"
        assert "showChange(" in after[:900], f"{name}: {write} is not redrawn on its own"


# ---------------------------------------------------------------- #119: a return keeps state
def test_119_shelf_empty_is_read_back_after_a_redraw(page) -> None:  # noqa: F811
    """The list says which lines were marked shelf-empty, so Undo is still
    there after a redraw.
    """
    client, db, partners, _his, _r = page
    base = int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (partners,)))
    shopping.build(db, base, retailers=("sainsburys",))
    h = _headers(client)
    q = {"Tailscale-User-Login": PARTNER_LOGIN}
    items = client.get("/api/shopping", headers=q).json()["items"]
    assert items and not any(i["out"] for i in items)
    first = items[0]["id"]
    assert client.post("/api/shopping/out", json={"item_id": first}, headers=h).status_code == 200
    marked = {i["id"]: i["out"] for i in client.get("/api/shopping", headers=q).json()["items"]}
    assert marked[first] is True and sum(marked.values()) == 1
    client.post("/api/shopping/out", json={"item_id": first, "undo": True}, headers=h)
    back = {i["id"]: i["out"] for i in client.get("/api/shopping", headers=q).json()["items"]}
    assert back[first] is False


def test_119_a_shelf_marked_by_the_other_person_is_not_drawn_as_yours(page) -> None:  # noqa: F811
    client, db, partners, his, r = page
    shopping.build(
        db,
        int(db.scalar("SELECT MAX(id) FROM menus WHERE person_id = ?", (partners,))),
        retailers=("sainsburys",),
    )
    _profile(db, his, sex="m")
    shopping.build(db, _menu(db, his, MON, 5, {"lunch": r["dal"]}), retailers=("sainsburys",))
    lentils = "Red lentils, dry"
    his_items = client.get("/api/shopping", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    mine = next(i["id"] for i in his_items["items"] if i["name"] == lentils)
    h = _headers(client, OWNER_LOGIN)
    out = client.post("/api/shopping/out", json={"item_id": mine}, headers=h)
    assert out.status_code == 200, out.text
    his_now = client.get("/api/shopping", headers={"Tailscale-User-Login": OWNER_LOGIN}).json()
    assert next(i["out"] for i in his_now["items"] if i["name"] == lentils) is True
    partners_now = client.get(
        "/api/shopping", headers={"Tailscale-User-Login": PARTNER_LOGIN}
    ).json()
    assert next(i["out"] for i in partners_now["items"] if i["name"] == lentils) is False


def test_119_a_return_after_a_while_leaves_unsent_input_alone() -> None:
    """The redraw on returning waits while a box on the tab holds something not
    yet sent.
    """
    js = _js()
    assert "if (item.out) drawMarked();" in _block(js, "shoppingRow")
    refresh = _block(js, "refreshNow")
    assert "unsent(name)" in refresh
    stale = refresh[refresh.index("STALE_AFTER_MS") :]
    assert stale.index("unsent(name)") < stale.index("lastDrawn = Date.now()")
    assert 'document.addEventListener("input"' in js, "what the person touched is known"


# ---------------------------------------------------------------- #120: the aisles again
def test_120_the_aisles_are_asked_for_again_when_the_form_opens_without_them() -> None:
    """An add-a-food form whose aisles failed to load asks again when opened or
    saved.
    """
    form = _block(_js(), "foodForm")
    assert "const loadAisles" in form
    opened = form[form.index('open.addEventListener("click"') :][:400]
    assert "!aisle.options.length" in opened and "loadAisles()" in opened
    saved = form[form.index('save.addEventListener("click"') :]
    assert "if (!body.aisle)" in saved and "loadAisles()" in saved[: saved.index("api(")]


# ---------------------------------------------------------------- #121: days with a gap
def test_121_days_with_a_gap_are_listed_not_spanned() -> None:
    """Mon, Tue and Thu are said as a list; only a run is said "Mon to Thu"."""
    js = _js()
    words = _block(js, "daysWords")
    assert "addDays(" in words and " and " in words and " to " in words
    assert "daysWords(d.days)" in _block(js, "foodsPanel")
    swap = _block(js, "fillSwap")
    assert "daysWords(got.days" in swap
    assert "${wd(got.days[0])} to" not in swap


# ---------------------------------------------------------------- #123: watercress is a food
def test_123_watercress_and_salted_foods_have_their_own_buttons() -> None:
    """Only the seasonings themselves are left off the card, not foods that
    start with their names.
    """
    staples = _regex(_block(_js(), "fillSwap"), "const foods = ")
    for seasoning in ("Salt", "Water", "Water, distilled", "Black pepper, ground", "Olive oil"):
        assert staples.match(seasoning), seasoning
    for food in ("Watercress, raw", "Watermelon", "Salted peanuts", "Salt beef", "Water chestnuts"):
        assert not staples.match(food), food


# ---------------------------------------------------------------- #124: whose shelf
def test_124_the_check_at_home_rows_ask_about_the_shop_not_the_cupboard() -> None:
    """Under "Check at home first" the shelf-empty link names the shop."""
    js = _js()
    shop = _block(js, "renderShopping")
    section = shop[shop.index('"Check at home first"') : shop.index("let aisle")]
    assert "shoppingRow(item, retailers, true)" in section
    row = _block(js, "shoppingRow")
    assert '"Not in the shop?"' in row and "atHome" in row


# ---------------------------------------------------------------- #134: clear of the tab row
def test_134_a_changed_card_is_brought_clear_of_the_sticky_tab_row() -> None:
    """A card whose top is under the tab row is scrolled to its start, not left
    "nearest".
    """
    js = _js()
    bring = _block(js, "bringIntoView")
    assert "--nav-h" in bring and "getBoundingClientRect().top" in bring
    assert '"start" : "nearest"' in bring
    menu = _block(js, "renderMenu")
    assert 'block: "nearest"' not in menu and "bringIntoView(" in menu


# ---------------------------------------------------------------- #141: one draw at a time
def test_141_an_older_menu_answer_never_draws_over_a_newer_one() -> None:
    """A menu render that was overtaken returns before it draws or takes a said
    line.
    """
    menu = _block(_js(), "renderMenu")
    assert 'const ticket = drawTicket("menu");' in menu
    check = menu.index('if (outdated("menu", ticket)) return;')
    assert check < menu.index("const toSay = menuSaid;") < menu.index("body.replaceChildren()")


# ---------------------------------------------------------------- #142: words, not codes
def test_142_the_building_banner_says_pre_bed_in_words(page) -> None:  # noqa: F811
    """A page ask for new pre-bed dishes is shown as "new pre-bed dishes",
    never the slot code.
    """
    client, _db, _partners, _his, _r = page
    tiffin = client.app.state.tiffin
    later: list[object] = []
    tiffin.background = lambda _pid, fn: later.append(fn)  # still running while we look
    asked = client.post(
        "/api/menu/new-dish", json={"day": isoday(MON), "slot": "pre_bed"}, headers=_headers(client)
    )
    assert asked.status_code == 200, asked.text
    assert later, "the build was queued"
    what = client.get("/api/menu", headers={"Tailscale-User-Login": PARTNER_LOGIN}).json()
    assert what["building"]["what"] == "new pre-bed dishes", what["building"]


# ---------------------------------------------------------------- the swap area, in a browser
# The swap area's waits and redraws are timers racing each other on the page, which a grep of the
# script cannot see. They are driven here in Chromium against the page behind uvicorn, the page's
# clock in the test's hands and the build run when the test says so - where this machine has node
# with Playwright and its Chromium (a dev container does; the box need not, and these skip).
DRIVE = r"""
const { chromium } = require("playwright");
const [url, scenario] = process.argv.slice(2);
const say = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const heard = [];
let wake = null;
require("readline").createInterface({ input: process.stdin }).on("line", (l) => {
  heard.push(l);
  if (wake) wake();
});
const told = () => new Promise((ok) => {
  if (heard.length) ok(heard.shift());
  else wake = () => { wake = null; ok(heard.shift()); };
});
const MEAL = "#d-2026-09-14-lunch";
// Monday's lunch and its swap area, as the person sees them
const seen = (page) => page.evaluate((meal) => {
  const box = document.querySelector(meal);
  const area = box && box.querySelector(".swap-area");
  const just = area
    && [...area.querySelectorAll("button")].find((b) => b.textContent.startsWith("Just "));
  return { open: !!box && box.open, shown: !!area && !area.hidden,
    text: area ? area.textContent : "", just: just ? just.classList.contains("on") : null };
}, MEAL);
const quiet = (p) => p.catch(() => {});

(async () => {
  let browser;
  try {
    browser = await chromium.launch();
  } catch (e) {
    say({ skip: String(e.message).split("\n")[0] });
    return;
  }
  try {
    const context = await browser.newContext(
      { extraHTTPHeaders: { "Tailscale-User-Login": "sam@example.com" } });
    const page = await context.newPage();
    const t0 = Date.parse("2026-09-07T19:30:00Z");
    await page.clock.install({ time: t0 });
    await page.clock.pauseAt(t0 + 1000);
    await page.goto(url);
    await page.click("#tab-menu");
    const meal = page.locator(MEAL);
    await meal.locator("summary").click();
    await meal.getByRole("button", { name: "Swap this dish" }).click();
    await meal.getByRole("button", { name: "Just Mon" }).click();
    if (scenario === "dropped") {
      // the area's own look straight after the ask is lost on the way
      let first = true;
      await page.route(/api\/menu\/swap\?/, (route) => {
        if (first) { first = false; route.abort(); } else route.continue();
      });
    }
    await meal.getByRole("button", { name: "Ask for new dishes" }).click();
    if (scenario === "dropped") {
      await meal.getByText("could not be drawn again").waitFor();
    } else {
      await meal.getByText("Writing new dishes").waitFor();
      // back after a while with the build still running: the tab drawn again, with its banner
      await page.clock.fastForward(100000);
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await page.locator("#menu-body").getByText("A new menu can be built once").waitFor();
      await meal.getByText("Writing new dishes").waitFor();
    }
    say({ step: "build" });
    await told();
    // the swap area's look comes round first, and says what came of the ask
    await page.clock.runFor(15000);
    await quiet(meal.getByText("No model to ask").waitFor({ timeout: 15000 }));
    const said = await seen(page);
    let after = null;
    let reopened = null;
    if (scenario !== "dropped") {
      // then the banner's: the build is over, and the tab is drawn again
      await page.clock.runFor(5000);
      const banner = page.locator("#menu-body").getByText("A new menu can be built once");
      await quiet(banner.waitFor({ state: "detached", timeout: 15000 }));
      await quiet(page.waitForFunction((m) => {
        const a = document.querySelector(`${m} .swap-area`);
        return a && (a.hidden || /No model|did not finish/.test(a.textContent));
      }, MEAL, { timeout: 15000 }));
      after = await seen(page);
      // closed and opened again, to pick one of the dishes: still on the day she chose
      const swap = meal.getByRole("button", { name: "Swap this dish" });
      if (after.shown) await swap.click();
      await swap.click();
      await quiet(meal.getByRole("button", { name: /^Just / }).waitFor({ timeout: 15000 }));
      reopened = await seen(page);
    }
    say({ step: "end", said, after, reopened });
  } catch (e) {
    say({ step: "error", error: String(e.stack || e) });
  } finally {
    await browser.close();
  }
})();
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node here to drive a browser with")
    assert node is not None
    found = subprocess.run(
        [node, "-e", "require.resolve('playwright')"], capture_output=True, timeout=60, check=False
    )
    if found.returncode:
        pytest.skip("no Playwright here to drive a browser with")
    return node


@pytest.fixture
def served(page, tmp_path):  # noqa: F811
    """The swap fixture's page behind uvicorn on a loopback port, its builds held until run."""
    import uvicorn

    node = _node()
    client, db, _partners, _his, _r = page
    held: list = []
    client.app.state.tiffin.background = lambda _pid, fn: held.append(fn)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            client.app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the page never came up"
        time.sleep(0.05)
    script = tmp_path / "drive.js"
    script.write_text(DRIVE, encoding="utf-8")
    try:
        yield node, script, f"http://127.0.0.1:{port}/", db, held
    finally:
        server.should_exit = True
        thread.join(10)


def _drive(served, scenario: str) -> dict:
    node, script, url, db, held = served
    proc = subprocess.Popen(
        [node, str(script), url, scenario],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(180, proc.kill)
    timer.start()
    assert proc.stdin is not None and proc.stdout is not None
    try:
        for line in proc.stdout:
            said = json.loads(line)
            if "skip" in said:
                pytest.skip(f"no browser to drive: {said['skip']}")
            if said["step"] == "build":
                assert len(held) == 1, "one build asked for"
                held.pop()(db)
                proc.stdin.write("built\n")
                proc.stdin.flush()
            elif said["step"] == "error":
                pytest.fail(said["error"])
            else:
                return said
        raise AssertionError("the browser stopped without a word")
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()


def test_104_69_the_result_and_the_day_chosen_survive_the_redraw_at_the_end_of_the_build(
    served,
) -> None:
    got = _drive(served, "redraw")
    assert "No model to ask" in got["said"]["text"], got["said"]
    assert got["said"]["just"] is True, got["said"]
    after = got["after"]
    assert after["open"] and after["shown"], after
    assert "No model to ask" in after["text"], after
    assert after["just"] is True, after
    assert got["reopened"]["just"] is True, got["reopened"]


def test_68_an_ask_whose_redraw_was_dropped_is_looked_at_again(served) -> None:
    got = _drive(served, "dropped")
    assert "No model to ask" in got["said"]["text"], got["said"]
