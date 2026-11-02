"""Linking through the page (More), and what a linked person can and cannot see.

The attacks tried here: a guessed code through either door, a third party's request, a write
with no token or with someone else's ids, the partner's list or menu fetched by id, and the
partner's private numbers searched for in every page after they are linked and sharing.
"""

from __future__ import annotations

from tiffin import links
from tiffin.food import shared

from .test_dash_two_people import (  # noqa: F401
    OWNER_LOGIN,
    PARTNER_LOGIN,
    SAT,
    SUN,
    THU,
    at,
    open_kitchen,
)

# -- the page --------------------------------------------------------------------------------


def post(k, login: str, path: str, body: dict | None = None, *, token: bool = True):
    headers = {"Tailscale-User-Login": login}
    if token:
        headers["X-CSRF-Token"] = k.client.get("api/me", headers=headers).json()["csrf"]
    return k.client.post(path, json=body or {}, headers=headers)


def denied(got) -> bool:
    """Refused, or answered with nothing: no list, no items."""
    if got.status_code in (403, 404):
        return True
    body = got.json() if got.status_code == 200 else {}
    return got.status_code == 200 and body.get("list") is None and not body.get("items")


def test_the_page_links_two_people_and_either_can_unlink(open_kitchen) -> None:  # noqa: F811
    k = open_kitchen(at(THU, 9))
    first = k.as_owner("api/link").json()
    assert first["state"] == "none" and first["partner_joined"] is True and first["partner"] is None
    made = post(k, OWNER_LOGIN, "api/link/mint")
    assert made.status_code == 200
    code = made.json()["code"]
    assert k.as_owner("api/link").json()["state"] == "offered"
    assert "code" not in k.as_owner("api/link").json(), "the code is shown once, on making it"
    entered = post(k, PARTNER_LOGIN, "api/link/accept", {"code": code})
    assert entered.status_code == 200 and entered.json()["state"] == "linked"
    assert entered.json()["partner"] == "Alex"
    assert k.as_owner("api/link").json()["partner"] == "Sam"
    # both are told in their chats
    tg = k.client.app.state.tiffin.transport
    assert any(m["chat_id"] == 1000001 and "linked" in m["text"] for m in tg.sent)
    gone = post(k, OWNER_LOGIN, "api/link/unlink")
    assert gone.status_code == 200 and gone.json()["state"] == "none"
    told = k.db.one(
        "SELECT text_html FROM messages_out WHERE person_id = ? AND kind = 'link_ended'",
        (k.partner_id,),
    )
    assert told is not None and "has unlinked" in told["text_html"]
    assert post(k, OWNER_LOGIN, "api/link/unlink").status_code == 409


def test_page_refusals_are_in_words_and_guessing_is_limited(open_kitchen) -> None:  # noqa: F811
    k = open_kitchen(at(THU, 9))
    code = post(k, OWNER_LOGIN, "api/link/mint").json()["code"]
    for _ in range(5):
        wrong = post(k, PARTNER_LOGIN, "api/link/accept", {"code": "QQQQ-QQQQ"})
        assert wrong.status_code == 409 and "not right" in wrong.json()["detail"]
    locked = post(k, PARTNER_LOGIN, "api/link/accept", {"code": code})
    assert locked.status_code == 409 and "Too many codes" in locked.json()["detail"]
    assert not links.is_linked(k.db)


def test_the_page_fences_every_link_write(open_kitchen) -> None:  # noqa: F811
    k = open_kitchen(at(THU, 9))
    for path in ("api/link/mint", "api/link/accept", "api/link/cancel", "api/link/unlink"):
        assert post(k, OWNER_LOGIN, path, {"code": "AAAA-AAAA"}, token=False).status_code == 403
        extra = post(k, OWNER_LOGIN, path, {"code": "AAAA-AAAA", "person_id": 2})
        assert extra.status_code in (400, 409)
        assert "person_id" in extra.text or extra.status_code == 409
    wrong_type = post(k, OWNER_LOGIN, "api/link/accept", {"code": 12345678})
    assert wrong_type.status_code == 400


def test_a_request_from_nobody_can_not_link_anyone(open_kitchen) -> None:  # noqa: F811
    k = open_kitchen(at(THU, 9))
    headers = {"Tailscale-User-Login": "stranger@example.com"}
    assert k.client.get("api/link", headers=headers).status_code in (401, 403)
    assert k.client.post("api/link/mint", json={}, headers=headers).status_code in (401, 403)
    assert k.db.scalar("SELECT COUNT(*) FROM links") == 0


# -- what a linked person can see ------------------------------------------------------------

#: the partner's private numbers: each exists nowhere else in the fixture's world
SECRET_WEIGHT = "61.37"
SECRET_KCAL = "1873"
SECRET_NOTE = "quail egg allergy note"


def linked_sharing_kitchen(open_kitchen):  # noqa: F811
    k = open_kitchen(at(THU, 9))
    db = k.db
    for pid in (k.owner_id, k.partner_id):
        db.insert(
            "consents", person_id=pid, kind="shared_meals", granted=1, wording_version="shared_v1"
        )
    db.insert(
        "weigh_ins", person_id=k.partner_id, day="2026-09-14", weight_kg=61.37, source="manual"
    )
    db.insert(
        "targets", person_id=k.partner_id, week_start="2026-09-14", kcal=1873, protein_g=143,
        fat_g=51, carb_g=177, fibre_g=26, source="formula",
    )  # fmt: skip
    db.insert("person_notes", person_id=k.partner_id, text=SECRET_NOTE, source="chat")
    link = links.record_migrated(db, k.owner_id, k.partner_id)
    pair = db.insert(
        "shared_menus", segment_start=SAT.isoformat(), segment_end=SUN.isoformat(), link_id=link
    )
    for pid, key in ((k.owner_id, "his_weekend"), (k.partner_id, "partner_weekend")):
        db.insert("shared_menu_members", shared_menu_id=pair, person_id=pid, menu_id=k.menus[key])
    return k


def test_a_linked_person_sees_the_plate_of_a_shared_dish_and_nothing_private(
    open_kitchen,  # noqa: F811
) -> None:
    k = linked_sharing_kitchen(open_kitchen)
    assert shared.linked_and_both_consented(k.db)
    plate = shared.partner_plate(k.db, k.owner_id, k.menus["his_weekend"], SAT, "dinner")
    assert plate is not None and set(plate) == {
        "name", "portion_g", "multiplier", "kcal", "protein_g",
    }  # fmt: skip

    # every page the owner can open, with every id the partner owns offered as a parameter
    ids = {
        "list": k.lists["hers"],
        "menu_id": k.menus["partner_week"],
        "menu": k.menus["partner_weekend"],
        "person_id": k.partner_id,
        "person": k.partner_id,
        "recipe_id": k.recipes["partner_lunch"],
        "date": "2026-09-14",
        "segment": "2026-09-14",
        "week": "2026-09-14",
        "start": "2026-09-14",
    }
    paths = [
        "api/me", "api/today", "api/menu", "api/week", "api/shopping", "api/summary/weekly",
        "api/progress", "api/trends", "api/measurements", "api/batch", "api/dishes", "api/foods",
        "api/prefs", "api/plan", "api/export", "api/choices", "api/manual", "api/status",
        "api/whoami", "api/link", "api/devices",
    ]  # fmt: skip
    query = "&".join(f"{key}={value}" for key, value in ids.items())
    for path in paths:
        for suffix in ("", "?" + query):
            got = k.as_owner(path + suffix)
            blob = got.text
            for secret in (SECRET_WEIGHT, SECRET_KCAL, SECRET_NOTE, "1873", "143"):
                assert secret not in blob, f"{path}{suffix[:20]} leaked {secret!r}"


def test_the_partners_lists_and_items_cannot_be_read_or_ticked_by_id(
    open_kitchen,  # noqa: F811
) -> None:
    k = linked_sharing_kitchen(open_kitchen)
    hers = k.lists["hers"]
    assert denied(k.as_owner(f"api/shopping?list={hers}"))
    item = int(k.db.scalar("SELECT id FROM shopping_items WHERE list_id = ?", (hers,)))
    before = k.db.scalar("SELECT COUNT(*) FROM shopping_ticks")
    for path, body in (
        ("api/shopping/tick", {"item_id": item, "ticked": True}),
        ("api/shopping/out", {"item_id": item}),
        ("api/shopping/send", {"list_id": hers}),
        ("api/shopping/skip", {"list_id": hers}),
    ):
        got = post(k, OWNER_LOGIN, path, body)
        assert got.status_code in (400, 403, 404, 409, 422), (path, got.status_code, got.text)
    assert k.db.scalar("SELECT COUNT(*) FROM shopping_ticks") == before


def test_after_unlinking_the_joint_list_is_not_readable_by_its_old_id(
    open_kitchen,  # noqa: F811
) -> None:
    k = linked_sharing_kitchen(open_kitchen)
    joint = k.lists["joint"]
    assert k.as_owner(f"api/shopping?list={joint}").status_code == 200
    assert k.as_partner(f"api/shopping?list={joint}").status_code == 200
    assert post(k, OWNER_LOGIN, "api/link/unlink").status_code == 200
    for who in (k.as_owner, k.as_partner):
        got = who(f"api/shopping?list={joint}")
        assert denied(got), got.text
    item = int(k.db.scalar("SELECT id FROM shopping_items WHERE list_id = ? LIMIT 1", (joint,)))
    for login in (OWNER_LOGIN, PARTNER_LOGIN):
        tick = post(k, login, "api/shopping/tick", {"item_id": item, "ticked": True})
        assert tick.status_code in (400, 403, 404, 409), tick.text
    # each now reads a list of their own
    assert k.as_owner("api/shopping").status_code == 200
    assert k.as_partner("api/shopping").status_code == 200


def test_after_unlinking_no_plate_of_the_other_is_shown(open_kitchen) -> None:  # noqa: F811
    k = linked_sharing_kitchen(open_kitchen)
    assert shared.partner_plate(k.db, k.owner_id, k.menus["his_weekend"], SAT, "dinner")
    post(k, PARTNER_LOGIN, "api/link/unlink")
    assert shared.partner_plate(k.db, k.owner_id, k.menus["his_weekend"], SAT, "dinner") is None
    assert (
        shared.partner_plate(k.db, k.partner_id, k.menus["partner_weekend"], SAT, "dinner") is None
    )
