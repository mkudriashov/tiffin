"""The weekly choice view's browser contract, before the API branch is joined."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "tiffin" / "web" / "static"


def _js() -> str:
    return (STATIC / "choose.js").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "choose.css").read_text(encoding="utf-8")


def test_the_choice_view_reads_its_own_choices_and_does_not_insert_markup() -> None:
    js = _js()
    assert "async function renderChoose()" in js
    assert 'const body = $("#choose-body")' in js
    assert 'api("api/choices")' in js
    assert "body.replaceChildren()" in js
    assert "innerHTML" not in js
    assert 'drawTicket("choose")' in js and 'outdated("choose", ticket)' in js


def test_each_write_sends_only_the_fields_in_the_choice_contract() -> None:
    js = _js()
    assert 'api("api/choices/pick", {' in js
    assert "body: { set_id: setId, day, slot: slotName, option_id: optionId }" in js
    assert 'api("api/choices/confirm", { body: { set_id: setId } })' in js
    assert 'api("api/choices/build", { body: {} })' in js
    assert 'api("api/choices/setting", { body: { per_slot: perSlot } })' in js
    assert 'chooseSettingButton(3, "3 options a meal"' in js
    assert 'chooseSettingButton(4, "4 options a meal"' in js
    assert 'chooseSettingButton(0, "Switch weekly choices off"' in js
    assert "person_id" not in js and "person:" not in js


def test_a_pick_updates_its_day_and_keeps_the_rest_of_the_week_in_place() -> None:
    js = _js()
    assert '"aria-pressed": String(picked)' in js
    assert 'button.setAttribute("aria-pressed", "true")' in js
    assert "if (chooseWorking) return" in js
    assert "currentSlot && currentSlot.chosen && currentSlot.picked === optionId" in js
    assert "chooseData.set.days[index] = answer.day" in js
    assert "section.replaceWith(replacement)" in js
    assert "window.scrollTo(0, scrollY)" in js
    assert "focused.focus({ preventScroll: true })" in js
    assert "error.textContent = err.message" in js
    assert 'old.setAttribute("aria-pressed", "true")' in js


def test_recipes_open_with_buttons_and_chosen_weeks_show_only_the_picked_dish() -> None:
    js = _js()
    assert '"aria-expanded": "false"' in js
    assert 'toggle.setAttribute("aria-expanded", String(!panel.hidden))' in js
    assert "openCook(option.name, option.method || [], ingredientLines)" in js
    assert "slot.options.filter((option) => option.id === slot.picked)" in js
    # a set week is read only; so is a dish holding a food refused since Sunday's build
    assert 'disabled: readOnly || avoided ? "" : null' in js
    assert "chooseListLink(answer.list_url)" in js
    assert 'show("shopping", { list })' in js


def test_a_running_build_polls_only_while_choose_is_open() -> None:
    js = _js()
    assert 'if (state.tab !== "choose") return' in js
    assert "}, 20000)" in js
    assert "chooseStopPoll()" in js
    assert "if (data.building)" in js
    assert "chooseSchedulePoll()" in js
    assert "if (data.can_build) body.append(chooseBuildButton(data))" in js


def test_choice_words_and_css_fit_the_phone_and_stay_in_the_view() -> None:
    js, css = _js(), _css()
    assert "Protein comes to" in js and "Fibre comes to" in js
    assert "Use these for the week" in js and "Set on" in js
    assert r"\u2713" in js
    assert "min-height: 44px" in css and "overflow-wrap: anywhere" in css
    assert "min-width: 0" in css and "position: sticky" in css
    assert "focus-visible" not in css  # the shell's one keyboard focus style applies
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    selectors = re.findall(r"(?:^|})\s*([^{}]+)\{", clean)
    assert selectors
    for group in selectors:
        for selector in group.split(","):
            assert selector.strip().startswith("#choose"), selector
    for source in (js, css):
        assert not re.search(r"[\u2605\u2606\U0001F300-\U0001FAFF]", source)
