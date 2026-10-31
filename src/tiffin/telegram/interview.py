"""Ask one household member how Tiffin should work for them.

Each answer is an append-only `onboarding_answers` row. Buttons, Skip and Pause
keep the interview resumable without repeating answered questions. The opening
message explains how answers may be used and what the owner will see."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..clock import DEFAULT_CLOCK, Clock, parse_stamp
from ..db import Db
from ..persons import Person
from .api import inline_keyboard
from .handlers import Handlers, Inbound
from .outbox import Outbox, SendResult
from .render import esc

FLOW = "interview"
#: the step prefix in onboarding_answers of the first round; each round keeps its own
ROUND = "iv-2026-10"
#: a typed question claims her next message for this long, then lets go
TYPED_TTL_S = 24 * 3600
MAX_TYPED = 1000


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    #: the buttons; () is a typed answer
    options: tuple[str, ...] = ()
    #: what the question is about, in plain words with an example, said above it
    explain: str = ""


QUESTIONS: tuple[Question, ...] = (
    # what she wants from it
    Question(
        "aim",
        "What do you most want Tiffin to help you with?",
        ("Eat to my targets", "Lose weight", "Not think about meals", "Shop and waste less"),
    ),
    Question("works", "What would make you say it works for you, a month from now?"),
    Question("annoys", "What annoys you most about it today?"),
    Question(
        "clash",
        "When they pull apart, which matters more: hitting the numbers, or food you enjoy?",
        ("The numbers", "Food I enjoy", "It depends"),
    ),
    Question("unused", "Is there anything it does now that you never use?"),
    # planning the week
    Question(
        "plan_day",
        "Which day would you like the whole week's menu?",
        ("Thursday", "Friday", "Saturday", "Sunday"),
    ),
    Question("plan_time", "What time of that day?", ("Morning", "Midday", "Afternoon", "Evening")),
    Question(
        "approve",
        "Would you like to look the week over and change it before it is final, or just get it?",
        ("Look it over first", "Just get it"),
    ),
    Question(
        "change_often",
        "Once a week is planned, how often do you change a day?",
        ("Never", "About once a week", "Often"),
    ),
    Question(
        "change_rest",
        "If you change one day, should the days after it be planned again around it?",
        ("Yes, re-plan them", "No, only that day"),
    ),
    Question("eat_out", "Are there weekdays you usually eat out or do not cook? Which?"),
    Question(
        "no_cook_day",
        "Would you like the odd no-cook day in the week (assembled, nothing on the hob)?",
        ("Yes", "No"),
    ),
    Question(
        "week_span",
        "Is Monday to Friday the right stretch to plan and shop for?",
        ("Yes", "No, it runs differently"),
    ),
    # shopping
    Question(
        "shop_day",
        "When do you do the main shop?",
        ("Saturday", "Sunday", "Monday", "A few times a week"),
    ),
    Question(
        "top_up",
        "One shop for the week, or one shop plus a top-up midweek?",
        ("One shop", "Plus a midweek top-up"),
    ),
    Question(
        "shop_where",
        "Where do you actually shop?",
        ("Sainsbury's", "M&S", "Tesco", "Somewhere else"),
    ),
    Question("shop_how", "In the shop or delivered?", ("In the shop", "Delivered", "Both")),
    Question(
        "tick_list",
        "Do you tick the list off on your phone as you shop?",
        ("Yes", "Sometimes", "No"),
    ),
    Question(
        "small_packs",
        "Would you pay a little more for a smaller pack so less is thrown away?",
        ("Yes", "No", "Depends on the food"),
    ),
    Question("budget", "Is there a weekly food budget you want kept to? Roughly how much?"),
    Question(
        "list_order",
        "How should the list be ordered?",
        ("By aisle", "By day", "By meal"),
    ),
    # packs and what is left over
    Question(
        "same_meat_week",
        "Is it fine to have the same meat or fish on two or three days of a week, in "
        "different dishes?",
        ("Yes", "Two days at most", "I would rather not"),
    ),
    Question(
        "same_meat_row",
        "And on two days in a row?",
        ("Fine", "Not in a row"),
    ),
    Question(
        "mains_week",
        "How many different meats or fish in a week suits you?",
        ("One", "Two", "Three", "No limit"),
    ),
    Question(
        "pack_on_meal",
        "On each meal, would you like to see where the meat comes from, like \"Monday's 500 g "
        'pack: 200 g today, 300 g left for Wednesday"?',
        ("Yes, on each meal", "Only on the list", "No"),
    ),
    Question(
        "freeze_ok",
        "Are you happy to freeze what is left of a pack and use it later in the week?",
        ("Yes", "Only some things", "No"),
    ),
    Question(
        "freeze_remind",
        "A reminder on the evening something needs to go in the freezer?",
        ("Yes", "No"),
    ),
    Question(
        "defrost_remind",
        "A reminder the evening before, to take something out of the freezer?",
        ("Yes", "No"),
    ),
    Question("veg_left", "What usually happens to veg or herbs left over at the end of the week?"),
    Question(
        "carry_over",
        "Should food left at the end of a week be used in the next week's menu?",
        ("Yes", "No"),
    ),
    Question("always_home", "Which foods do you always have at home, so never need buying?"),
    Question(
        "cook_extra",
        "Do you cook extra on purpose to eat the next day?",
        ("Often", "Sometimes", "Never"),
    ),
    # cooking
    Question(
        "dinner_time",
        "On a weekday, how long are you happy to spend cooking dinner?",
        ("15 minutes", "30 minutes", "45 minutes", "Longer"),
    ),
    Question(
        "lunch_how",
        "And lunch on a weekday?",
        ("Cook it fresh", "Make it the night before", "Nothing to cook"),
    ),
    Question(
        "lunch_where",
        "Where do you eat lunch on weekdays?",
        ("At home", "At work", "It varies"),
    ),
    Question(
        "prep_once",
        "Would you cook one thing once for the week (rice, grains, a sauce) on the weekend?",
        ("Yes", "No", "Maybe"),
    ),
    Question(
        "recipe_detail",
        "How much detail in a recipe?",
        ("Short steps", "Full steps"),
    ),
    Question("kitchen", "Anything in your kitchen you love using, or do not have?"),
    Question(
        "confidence",
        "How do you feel about cooking?",
        ("Simple dishes only", "Comfortable", "Love trying things"),
    ),
    # food and taste
    Question(
        "variety",
        "How much variety in a week?",
        ("Different every day", "Some repeats are fine", "The same few dishes"),
    ),
    Question(
        "breakfast",
        "Breakfast: the same every day, or different?",
        ("The same", "A few in turn", "Different each day"),
    ),
    Question("cuisines", "Which kinds of food or cuisines would you like more of?"),
    Question("less_of", "Anything you would like less of on your menus?"),
    Question("try", "Any foods or dishes you would like to try?"),
    Question(
        "snacks",
        "What kind of snacks?",
        ("Sweet", "Savoury", "Both", "No snacks"),
    ),
    Question(
        "treats",
        "Treats in the week too, or only at the weekend?",
        ("In the week too", "Weekend only"),
    ),
    Question("every_week", "A dish you would happily have every week?"),
    # targets and body
    Question(
        "focus",
        "What matters most to you right now?",
        ("Weight", "Strength", "Energy", "Health generally"),
    ),
    Question(
        "kcal_strict",
        "How closely should calories be kept to?",
        ("Close every day", "On average over the week", "Loosely"),
    ),
    Question(
        "training_food",
        "Should training days have more food than rest days?",
        ("Yes", "No", "Not sure"),
    ),
    Question(
        "protein_feel",
        "Your protein target: does it feel right?",
        ("About right", "Too much food", "Too little"),
    ),
    Question(
        "trends",
        "Would you like to see your trends (weight, waist) on the page?",
        ("Yes", "No"),
    ),
    # the bot and the page
    Question(
        "messages",
        "How many messages from the bot a day is right?",
        ("One", "Morning and evening", "Only when needed"),
    ),
    Question(
        "morning",
        "What should the morning message tell you?",
        ("Today's meals", "What to prepare", "Just a link"),
    ),
    Question(
        "evening",
        "Is the evening check-in useful?",
        ("Yes", "Not every day", "No"),
    ),
    Question(
        "page_or_bot",
        "Which do you use more, the page or the bot?",
        ("The page", "The bot", "Both"),
    ),
    Question(
        "tone",
        "How should the messages sound?",
        ("Blunt", "Neutral", "Warm"),
    ),
    # Shared meals use the other household member's menu.
    Question(
        "weekend_together",
        "Weekends cooked together, one menu for both: keep it?",
        ("Yes", "Sometimes", "I would rather have my own"),
    ),
    Question(
        "weekday_shared",
        "Would you like some weekday dinners shared with the other person too?",
        ("Yes", "Sometimes", "No"),
    ),
    Question("anything_else", "Anything else you want Tiffin to do that was not asked?"),
)

INTRO = (
    "The household owner asked how Tiffin should work for you: "
    "planning the week, shopping, packs, cooking, your targets and the messages.\n"
    f"{len(QUESTIONS)} short questions, most answered with a tap. Skip any, pause any time, and "
    "carry on later with /questions.\n"
    "Your answers are kept here. The configured model reads them to suggest changes; the "
    "household owner is told what changes, not your answers."
)

#: her ... in more simple words / give examples ... without being condescending"): what the first
#: round's answers would change, a piece at a time, each with an example and one question
PLAN: tuple[Question, ...] = (
    Question(
        "p_sunday",
        "Is Sunday at 12:00 early enough for your shop?",
        ("Yes", "Earlier: Saturday evening", "Later on Sunday"),
        explain="Your whole Monday to Friday would arrive on Sunday at 12:00, with one shopping "
        "list for all of it. You look it over, change any day you like, and the list changes "
        "with it. Then you shop once.",
    ),
    Question(
        "p_change_after",
        "If you change a day after you have shopped, which do you want?",
        ("Use what I bought", "A few new things is fine"),
        explain="If you change a day after shopping, only that day is rebuilt. For example, you "
        "swap Wednesday's dinner on Tuesday: the new dinner can be made only from what you "
        "already bought, or it can need a couple of new things.",
    ),
    Question(
        "p_two_days",
        "Does that work for you?",
        ("Yes", "I would rather spread it out more"),
        explain="The main idea: every pack of meat or fish is planned to be finished, not just "
        "opened. Raw meat keeps about two days once opened, and you would rather not freeze it, "
        "so each pack is cooked over two days close together.\n"
        "For example, chicken thighs 500 g: Monday dinner 250 g, Tuesday lunch 250 g. Pack "
        "finished, nothing left.",
    ),
    Question(
        "p_thu_fri",
        "Which would you pick?",
        ("Food that keeps", "Freeze it whole", "Wednesday top-up"),
        explain="That leaves Thursday and Friday. Meat bought on Sunday would wait four days "
        "before it is opened, which is near or past many use-by dates. Three ways round it:\n"
        "1. Food that keeps: tinned tuna or sardines, eggs, cheese, smoked salmon.\n"
        "2. Freeze the second pack whole and unopened on Sunday, take it out on Wednesday "
        "night. You would get a reminder.\n"
        "3. A small top-up shop on Wednesday.",
    ),
    Question(
        "p_tins",
        "How many meals a week from tinned fish is fine?",
        ("One or two", "Three or four", "As many as fit"),
        explain="Tins and eggs fit this well: they keep for months, and they are things you "
        "like. For example, Thursday lunch a tuna melt on toast, Friday dinner sardines on toast "
        "with roasted tomatoes.",
    ),
    Question(
        "p_pack_size",
        "Which do you prefer?",
        ("Finish the bigger pack", "Smaller pack", "Whichever wastes least"),
        explain="Packs come in fixed sizes. For example, the week needs 300 g of beef mince and "
        "the shop sells 250 g and 500 g. Either the week buys 500 g and mince goes into one more "
        "meal so it is all used, or it buys 250 g and the mince meals are a little smaller.",
    ),
    Question(
        "p_veg",
        "Is the same vegetable two or three times in a week fine?",
        ("Yes", "I prefer different veg each day"),
        explain="The same goes for vegetables. For example, a bag of spinach is 240 g: instead of "
        "40 g in one dish and the rest in the bin, it goes into three meals that week.",
    ),
    Question(
        "p_herbs",
        "Which would you rather?",
        ("Fresh, used up", "Dried or frozen", "I do not mind"),
        explain="Fresh herbs are what gets thrown away most often. Dried herbs, frozen herb "
        "portions or a herb paste in a tube keep for weeks and taste close to fresh. Or fresh "
        "herbs stay, and each bunch is used in several meals.",
    ),
    Question(
        "p_lunch",
        "Do you reheat your lunch, or eat it cold?",
        ("Reheat it", "Eat it cold", "Either"),
        explain="Lunch made the evening before: you make it after dinner, it goes in the fridge, "
        "and the next day you reheat it or eat it cold. So lunches would be dishes that keep "
        "well overnight, like a pasta bake or chicken with potatoes, not a fried egg.",
    ),
    Question(
        "p_double",
        "How often is that fine?",
        ("Most days", "Two or three times a week", "Rather not"),
        explain="Often the easiest is one cook, two meals: dinner is made a bit bigger and the "
        "rest is the next day's lunch. For example, a chilli for Monday dinner, the same chilli "
        "for Tuesday lunch. It also finishes packs neatly.",
    ),
    Question(
        "p_nocook",
        "How often would you like a no-cook day?",
        ("Once a week", "Now and then", "Only when I ask"),
        explain="A no-cook day means nothing on the hob or in the oven, everything put together. "
        "For example, yoghurt with berries for breakfast, a tuna and egg wrap for lunch, smoked "
        "salmon with bread and cheese for dinner.",
    ),
    Question(
        "p_45min",
        "Do the 45 minutes include chopping and getting things ready?",
        ("Yes, everything", "No, only the cooking"),
        explain="Weekday dinners could take up to 45 minutes, and still be simple: one or two "
        "pans, and never three things at once.",
    ),
    Question(
        "p_steps",
        "Is the full version what you want?",
        ("Yes, like that", "Somewhere in between"),
        explain='Short steps: "Fry the mince, add the tomatoes, simmer."\n'
        'Full steps: "Heat 1 tsp oil in a large pan on a medium-high heat. Add the mince, break '
        "it up and fry for 6 to 8 minutes until browned. Stir in the tomatoes, bring to a "
        'bubble, then turn the heat down and simmer for 15 minutes."',
    ),
    Question(
        "p_rice",
        "What about other things made from rice, like rice noodles or rice cakes?",
        ("Take them off too", "Those are fine"),
        explain="Rice would come off your menus completely, with the broccoli, beans, lentils "
        "and almonds. Rice paper, for your burger, is a different thing and stays.",
    ),
    Question(
        "p_carbs",
        "Which of those do you like most?",
        explain="Without rice, beans and lentils, the carbohydrate in your meals would come from "
        "things like potatoes, sweet potato, pasta, bread, wraps, oats, couscous and fruit.",
    ),
    Question(
        "p_balance",
        "Does that balance sound right?",
        ("Yes", "A bit more room on some days"),
        explain="Calories: each day stays within about 5% of your number, so the weight keeps "
        "going down. Inside that, when two dishes both fit, the one you are likely to enjoy "
        "more is the one you get.",
    ),
    Question(
        "p_different",
        "After you enter it, should the rest of that day's meals change to fit?",
        ("Yes, resize the rest", "No, just record it"),
        explain="Eating something different: on the page, on any day, you tap a meal and write "
        'what you had instead, for example "a chicken wrap from Pret". It is counted for that '
        "day.",
    ),
    Question(
        "p_weekly",
        "When would you like it?",
        ("Sunday morning", "Monday morning", "Sunday, with the menu"),
        explain="Once a week, one message with three quick things: a reminder to step on the "
        "scale, your waist and hip, and where you are in your cycle. Buttons where possible; "
        "about a minute.",
    ),
    Question(
        "p_training",
        "Which would you like?",
        ("A bit more on training days", "The same every day", "Not sure yet"),
        explain="Training: on the page you would add a session you did, what and for how long. "
        "On a training day you could get a little more food, usually 100 to 200 kcal, or every "
        "day can stay the same.",
    ),
    Question(
        "p_budget",
        "Is that useful even when only part of the list is priced?",
        ("Yes", "Only once it is complete"),
        explain="Budget: the list would show what it costs. Only about a third of items have a "
        'price so far, so it would say something like "about £32 for the 14 items '
        'priced".',
    ),
    Question(
        "p_messages",
        "Is anything missing from that?",
        explain="Messages only when needed would mean: the week and its list on Sunday, the "
        "weekly check, and a note if a menu of yours could not be made. No evening check-in, "
        "and no message every morning.",
    ),
    Question(
        "p_unclear",
        "Is anything here unclear, or anything you would do differently?",
        explain="That is the whole plan.",
    ),
)

INTRO_PLAN = (
    "Thank you for your answers. Before anything is built, here is how it would work, one piece "
    "at a time, each with an example and a question.\n"
    f"{len(PLAN)} of them, same buttons as before. Skip any, pause any time; /questions carries "
    "on.\n"
    "As before, the configured model reads your answers to suggest changes; the household "
    "owner is told what changes, not your answers."
)


#: something the build needs to decide
DETAILS: tuple[Question, ...] = (
    Question(
        "d_topup_when",
        "When should the Wednesday list arrive?",
        ("Tuesday evening", "Wednesday morning", "Wednesday afternoon"),
        explain="You picked a small top-up shop on Wednesday for Thursday and Friday. So there "
        "would be two lists: Sunday's for Monday to Wednesday, and a short one for the top-up.",
    ),
    Question(
        "d_monday_lunch",
        "What should Monday's lunch be?",
        ("Cook it Sunday evening", "A no-cook lunch", "Cook it Monday morning"),
        explain="Most lunches would be the previous dinner, reheated. Monday is the exception: "
        "Sunday is the weekend menu you share, so there is no separate dinner of yours to make "
        "Monday's lunch from.",
    ),
    Question(
        "d_nocook_day",
        "Which day should be the no-cook day?",
        ("Monday", "Wednesday", "Thursday", "Friday"),
        explain="One day a week with nothing on the hob or in the oven. Wednesday fits neatly: "
        "it is the top-up day, when the first shop's food is running out.",
    ),
    Question(
        "d_breakfasts",
        "How many different breakfasts would you like to rotate between?",
        ("Two", "Three", "Four"),
        explain="Breakfasts would take turns. For example, with two: Greek yoghurt with berries "
        "and chia on Monday, eggs on toast on Tuesday, then yoghurt again on Wednesday.",
    ),
    Question(
        "d_prebed",
        "What should happen to it?",
        ("Keep it", "Make it a daytime snack", "Drop it"),
        explain="At the moment your day ends with a small protein snack before bed, like a "
        "yoghurt or a chia pudding. It counts towards your protein for the day.",
    ),
    Question(
        "d_training_day",
        "Which days should count as training days?",
        ("My planned days", "Only days I record", "Planned, fixed by what I record"),
        explain="On a training day you would get a little more food. Tiffin knows the "
        "sessions you planned for each weekday, and on the page you could record what you "
        "actually did.",
    ),
    Question(
        "d_cycle_how",
        "How would you like to track your cycle?",
        ("Tap the phase each Monday", "Enter my period's first day"),
        explain="Two ways. You tap which phase you are in, in the Monday message. Or you enter "
        "the first day of your period when it starts, and Tiffin works out the phase "
        "from there.",
    ),
    Question(
        "d_cycle_food",
        "Should your food change with your cycle?",
        ("Just record it", "Adjust food a little", "Not sure yet"),
        explain="Many people are hungrier, and a little heavier on the scale, in the week before "
        "their period. Tiffin could simply record your cycle, so a jump on the scale is "
        "explained, or give a little more food that week.",
    ),
    Question(
        "d_leftover_lunch",
        "Which would you prefer?",
        ("The same dish is fine", "Make it different"),
        explain="When dinner becomes the next day's lunch, it can be the same dish reheated, or "
        "the same base made into something else. For example, Monday's chilli becomes "
        "Tuesday's chilli-stuffed jacket potato.",
    ),
    Question(
        "d_change_items",
        "Where should those extra items go?",
        ("On the page only", "A message too"),
        explain="If you change a day after you have shopped, the new dish may need a few things "
        "you did not buy, and they would go on a small extra list.",
    ),
)

INTRO_DETAILS = (
    "Thank you, that settles most of it. A few last details, so it is built the way you want: "
    f"{len(DETAILS)} questions, each with a short explanation.\n"
    "Same buttons as before; /questions carries on if you pause."
)


@dataclass(frozen=True)
class Round:
    #: the step prefix in onboarding_answers
    key: str
    intro: str
    questions: tuple[Question, ...]

    def by_key(self, key: str) -> Question | None:
        return next((q for q in self.questions if q.key == key), None)


ROUNDS: tuple[Round, ...] = (
    Round(ROUND, INTRO, QUESTIONS),
    Round("iv-2026-10b", INTRO_PLAN, PLAN),
    Round("iv-2026-10c", INTRO_DETAILS, DETAILS),
)


def step(key: str, round_: Round = ROUNDS[0]) -> str:
    return f"{round_.key}:{key}"


def answered(db: Db, person_id: int, round_: Round = ROUNDS[0]) -> dict[str, str | None]:
    """Each question's latest answer in `round_` (None when skipped), by key."""
    rows = db.all(
        "SELECT step, answer_json FROM onboarding_answers WHERE person_id = ? AND step LIKE ? "
        "ORDER BY id",
        (person_id, f"{round_.key}:%"),
    )
    out: dict[str, str | None] = {}
    for r in rows:
        key = str(r["step"]).split(":", 1)[1]
        try:
            out[key] = json.loads(str(r["answer_json"])).get("answer")
        except ValueError, AttributeError:
            out[key] = None
    return out


def opened(db: Db, person_id: int, round_: Round) -> bool:
    """Has this round been put to them: its opening sent, or an answer given?"""
    return (
        db.one(
            "SELECT 1 FROM messages_out WHERE person_id = ? AND kind = 'interview' "
            "AND due_key = ? AND status = 'sent' "
            "UNION SELECT 1 FROM onboarding_answers WHERE person_id = ? AND step LIKE ? LIMIT 1",
            (person_id, f"{round_.key}:intro", person_id, f"{round_.key}:%"),
        )
        is not None
    )


def next_question(db: Db, person_id: int) -> tuple[int, int, Question] | None:
    """(round number, question number, both from 1, the question) for the first question not yet
    answered or skipped, in the rounds put to them."""
    for r, round_ in enumerate(ROUNDS, start=1):
        if r > 1 and not opened(db, person_id, round_):
            continue
        done = answered(db, person_id, round_)
        for n, q in enumerate(round_.questions, start=1):
            if q.key not in done:
                return r, n, q
    return None


def record(
    db: Db,
    person_id: int,
    q: Question,
    answer: str | None,
    message_in_id: int | None,
    round_: Round = ROUNDS[0],
) -> None:
    db.insert(
        "onboarding_answers",
        person_id=person_id,
        step=step(q.key, round_),
        answer_json=json.dumps({"question": q.text, "answer": answer}),
        message_in_id=message_in_id,
    )


def keyboard(n: int, q: Question, r: int = 1) -> dict:
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    for i, label in enumerate(q.options):
        row.append((label, f"k:iv:a:{r}:{n}:{i}"))
        if len(row) == 2 or len(label) > 18:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("Skip", f"k:iv:s:{r}:{n}"), ("Pause", "k:iv:p")])
    return inline_keyboard(rows)


def question_text(n: int, q: Question, r: int = 1) -> str:
    total = len(ROUNDS[r - 1].questions)
    tail = "" if q.options else "\nType your answer, or Skip."
    if q.explain:
        return f"<b>{n} of {total}</b>\n{esc(q.explain)}\n\n<b>{esc(q.text)}</b>{tail}"
    return f"<b>Question {n} of {total}</b>\n{esc(q.text)}{tail}"


class InterviewFlow:
    def __init__(self, *, clock: Clock = DEFAULT_CLOCK):
        self.clock = clock

    # -- the typed answer's claim on the next message ------------------------------------
    def _armed(self, db: Db, person_id: int) -> tuple[Round, Question] | None:
        """The typed question waiting for her next message, or None."""
        row = db.one(
            "SELECT state_json, updated_at FROM conversation_state WHERE person_id = ?",
            (person_id,),
        )
        if row is None:
            return None
        try:
            st = json.loads(str(row["state_json"]))
        except ValueError:
            return None
        if st.get("flow") != FLOW:
            return None
        age = (self.clock.now_utc() - parse_stamp(str(row["updated_at"]))).total_seconds()
        if age > TYPED_TTL_S:
            self._disarm(db, person_id)
            return None
        round_ = next((x for x in ROUNDS if x.key == st.get("round", ROUND)), None)
        q = round_.by_key(str(st.get("q") or "")) if round_ is not None else None
        return (round_, q) if round_ is not None and q is not None else None

    def _arm(self, db: Db, person_id: int, round_: Round, key: str) -> None:
        db.execute(
            "INSERT INTO conversation_state(person_id, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (
                person_id,
                json.dumps({"flow": FLOW, "round": round_.key, "q": key}),
                self.clock.utc_stamp(),
            ),
        )

    def _disarm(self, db: Db, person_id: int) -> None:
        db.execute(
            "DELETE FROM conversation_state WHERE person_id = ? "
            "AND json_extract(state_json, '$.flow') = ?",
            (person_id, FLOW),
        )

    # -- asking ---------------------------------------------------------------------------
    def ask_next(self, h: Handlers, person: Person) -> None:
        """The next unanswered question, or the thank-you when there is none."""
        got = next_question(h.db, person.id)
        if got is None:
            self._disarm(h.db, person.id)
            h.outbox.reply(
                person,
                "That is every question. Thank you: what changes because of your answers comes "
                "in the menus and on the page, and /questions shows this again.",
            )
            return
        r, n, q = got
        if q.options:
            self._disarm(h.db, person.id)
        else:
            self._arm(h.db, person.id, ROUNDS[r - 1], q.key)
        h.outbox.reply(person, question_text(n, q, r), keyboard(n, q, r))

    def cmd_questions(self, h: Handlers, inbound: Inbound, _arg: str) -> str:
        if next_question(h.db, inbound.person.id) is None:
            h.outbox.reply(inbound.person, "You have answered every question. Thank you.")
            return "handled"
        self.ask_next(h, inbound.person)
        return "handled"

    # -- answers ---------------------------------------------------------------------------
    def tap(self, h: Handlers, inbound: Inbound, parts: list[str]) -> str:
        """k:iv:go | k:iv:later | k:iv:p | k:iv:s:<round>:<n> | k:iv:a:<round>:<n>:<option>;
        the first round's buttons carried no round (k:iv:s:<n>, k:iv:a:<n>:<option>)."""
        person = inbound.person
        what = parts[0] if parts else ""
        if what == "go":
            h.clear_buttons(inbound)
            self.ask_next(h, person)
            return "handled"
        if what in ("later", "p"):
            h.clear_buttons(inbound)
            self._disarm(h.db, person.id)
            h.outbox.reply(person, "Paused. Send /questions whenever you want to carry on.")
            return "handled"
        rest = parts[1:]
        if (what == "s" and len(rest) == 1) or (what == "a" and len(rest) == 2):
            rest = ["1", *rest]  # an old first-round button
        if what not in ("s", "a") or len(rest) < 2:
            return "ignored"
        found = self._question(rest[0], rest[1])
        if found is None:
            return "ignored"
        round_, q = found
        if what == "s":
            answer: str | None = None
        else:
            try:
                answer = q.options[int(rest[2])]
            except IndexError, ValueError:
                return "ignored"
        with h.db.tx():
            record(h.db, person.id, q, answer, inbound.message_in_id, round_)
        h.clear_buttons(inbound)
        h.answer(inbound, "Skipped" if answer is None else f"Noted: {answer}")
        self.ask_next(h, person)
        return "handled"

    def typed(self, h: Handlers, inbound: Inbound, text: str) -> str | None:
        armed = self._armed(h.db, inbound.person.id)
        if armed is None or not text.strip():
            return None
        round_, q = armed
        with h.db.tx():
            record(
                h.db, inbound.person.id, q, text.strip()[:MAX_TYPED], inbound.message_in_id, round_
            )
        self.ask_next(h, inbound.person)
        return "handled"

    @staticmethod
    def _question(round_number: str, number: str) -> tuple[Round, Question] | None:
        try:
            r, n = int(round_number), int(number)
        except ValueError:
            return None
        if not 1 <= r <= len(ROUNDS):
            return None
        qs = ROUNDS[r - 1].questions
        return (ROUNDS[r - 1], qs[n - 1]) if 1 <= n <= len(qs) else None


def start_keyboard() -> dict:
    return inline_keyboard([[("Start", "k:iv:go"), ("Not now", "k:iv:later")]])


def send_intro(outbox: Outbox, person: Person, round_: Round = ROUNDS[0]) -> SendResult:
    """A round's opening message, once (the outbox is idempotent on its key)."""
    return outbox.send(
        person, "interview", f"{round_.key}:intro", esc(round_.intro), start_keyboard()
    )


def install(handlers: Handlers, *, clock: Clock = DEFAULT_CLOCK) -> InterviewFlow:
    flow = InterviewFlow(clock=clock)
    handlers.register_command("questions", flow.cmd_questions)
    handlers.register_callback("iv", flow.tap)
    # first: a typed answer is only claimed while a typed question is armed
    handlers.text_handlers.insert(0, flow.typed)
    return flow


__all__ = [
    "DETAILS",
    "INTRO",
    "PLAN",
    "QUESTIONS",
    "ROUND",
    "ROUNDS",
    "InterviewFlow",
    "Round",
    "answered",
    "install",
    "next_question",
    "send_intro",
]
