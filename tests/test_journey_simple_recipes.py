"""Journey tests for clear recipes built from fixture catalogue entries."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Mapping
from typing import Any

import pytest

from tiffin import scheduler as sc
from tiffin.app import App
from tiffin.clock import TZ, FrozenClock
from tiffin.db import Db
from tiffin.food import catalogue
from tiffin.jobs import menu_propose
from tiffin.secrets import Secrets
from tiffin.telegram.poller import Poller

from .fakes import FakeTelegram
from .test_menu import FakeGateway

OWNER_CHAT = 1000001
PARTNER_CHAT = 1000002

SAT = dt.date(2026, 9, 12)
MON = dt.date(2026, 9, 14)
TUE = dt.date(2026, 9, 15)
WED = dt.date(2026, 9, 16)
THU = dt.date(2026, 9, 17)
FRI = dt.date(2026, 9, 18)

REAL_INGREDIENTS: list[tuple[Any, ...]] = [
    ("almonds", "Almonds, whole", 554, 21.2, 49.9, 5.3, 12.5, 200, "200 g bag", "condiments", 1, 0),
    ("apples", "Apples, eating, raw", 51, 0.6, 0.5, 11.6, 1.2, 900, "6 apples", "produce", 0, 0),
    ("avocado", "Avocado, Hass", 171, 1.8, 17.4, 1.8, 3.1, 300, "2 avocados", "produce", 0, 0),
    (
        "bacon_back",
        "Back bacon, fat trimmed, raw",
        136,
        18.8,
        6.7,
        0,
        0,
        240,
        "8 rashers",
        "meat",
        0,
        0,
    ),
    (
        "beef_mince_5",
        "Beef mince 5% fat, raw",
        130,
        21.9,
        4.2,
        0,
        0,
        500,
        "500 g pack",
        "meat",
        0,
        1,
    ),
    (
        "black_pepper",
        "Black pepper, ground",
        251,
        10.4,
        3.3,
        64,
        25.3,
        100,
        "100 g jar",
        "condiments",
        1,
        0,
    ),
    ("bread_wholemeal", "Wholemeal bread", 217, 9.4, 2.5, 42, 7, 800, "800 g loaf", "bakery", 0, 0),
    ("broccoli", "Broccoli, raw", 34, 4.3, 0.6, 3.2, 4, 350, "350 g head", "produce", 0, 0),
    ("carrots", "Carrots, raw", 34, 0.5, 0.4, 7.7, 3.9, 1000, "1 kg bag", "produce", 0, 0),
    ("cauliflower", "Cauliflower, raw", 30, 2.5, 0.4, 4.4, 1.8, 700, "1 head", "produce", 0, 0),
    (
        "cherry_tomatoes",
        "Cherry tomatoes, raw",
        22,
        1.1,
        0.5,
        3.6,
        1.3,
        250,
        "250 g punnet",
        "produce",
        0,
        0,
    ),
    ("chicken_breast", "Chicken breast, raw", 106, 24, 1.1, 0, 0, 500, "500 g pack", "meat", 0, 0),
    (
        "chicken_stock",
        "Chicken stock, ready made",
        12,
        2.3,
        0.2,
        0.2,
        0.3,
        500,
        "500 ml pouch",
        "condiments",
        1,
        0,
    ),
    (
        "chicken_thigh",
        "Chicken thigh fillets, skinless, raw",
        109,
        20.9,
        2.8,
        0,
        0,
        500,
        "500 g pack",
        "meat",
        0,
        0,
    ),
    (
        "chickpeas",
        "Chickpeas, tinned, drained",
        129,
        8.4,
        3,
        18.3,
        7.1,
        240,
        "400 g tin",
        "tins",
        0,
        0,
    ),
    (
        "chopped_tomatoes",
        "Chopped tomatoes, tinned",
        19,
        1.1,
        0.1,
        3.8,
        0.8,
        400,
        "400 g tin",
        "tins",
        0,
        0,
    ),
    (
        "coconut_milk",
        "Coconut milk, tinned",
        169,
        1.1,
        16.9,
        3.3,
        0.4,
        400,
        "400 ml tin",
        "tins",
        0,
        0,
    ),
    ("cottage_cheese", "Cottage cheese", 103, 9.4, 6, 3.1, 0, 300, "300 g tub", "dairy", 0, 0),
    ("cucumber", "Cucumber, raw", 14, 1, 0.6, 1.2, 0.7, 400, "1 cucumber", "produce", 0, 0),
    ("curry_paste", "Curry paste", 253, 4.7, 21.3, 11.3, 6.8, 200, "200 g jar", "condiments", 1, 0),
    ("eggs", "Eggs, whole, raw", 131, 12.6, 9, 0, 0, 348, "6 eggs", "dairy", 0, 0),
    ("feta", "Feta cheese", 250, 15.6, 20.2, 1.5, 0, 200, "200 g pack", "dairy", 0, 0),
    ("garlic", "Garlic, raw", 98, 7.9, 0.6, 16.3, 2.1, 50, "1 bulb", "produce", 1, 0),
    ("ginger", "Ginger, fresh", 44, 1.8, 0.8, 8.1, 2, 100, "100 g root", "produce", 1, 0),
    ("greek_yoghurt_0", "Greek yoghurt 0% fat", 57, 10.3, 0, 3, 0, 500, "500 g tub", "dairy", 0, 0),
    ("green_beans", "Green beans, raw", 24, 2.1, 0.4, 3.1, 3.4, 200, "200 g pack", "produce", 0, 0),
    (
        "kidney_beans",
        "Kidney beans, tinned, drained",
        100,
        8.6,
        1,
        15.1,
        6.8,
        240,
        "400 g tin",
        "tins",
        0,
        0,
    ),
    (
        "lentils_red",
        "Red lentils, dry",
        311,
        25.6,
        1.8,
        51.2,
        17.4,
        500,
        "500 g bag",
        "grains",
        1,
        0,
    ),
    ("lettuce", "Lettuce, raw", 11, 1.2, 0.1, 1.4, 1.5, 300, "1 lettuce", "produce", 0, 0),
    (
        "mayo_light",
        "Mayonnaise, reduced fat",
        288,
        1,
        28.1,
        8.2,
        0,
        400,
        "400 g jar",
        "condiments",
        1,
        0,
    ),
    ("milk_semi", "Milk, semi-skimmed", 46, 3.5, 1.7, 4.7, 0, 1136, "2 pint carton", "dairy", 0, 0),
    ("oats", "Porridge oats", 381, 10.9, 8.1, 70.7, 7.8, 1000, "1 kg bag", "grains", 1, 0),
    ("olive_oil", "Olive oil", 899, 0, 99.9, 0, 0, 460, "500 ml bottle", "condiments", 1, 0),
    ("onions", "Onions, raw", 35, 1, 0.1, 8, 2.2, 1000, "1 kg bag", "produce", 0, 0),
    (
        "peanut_butter",
        "Peanut butter, smooth",
        607,
        22.8,
        51.8,
        13.1,
        6.6,
        340,
        "340 g jar",
        "condiments",
        1,
        0,
    ),
    ("peas_frozen", "Peas, frozen", 68, 5.3, 0.7, 10.7, 5.3, 1000, "1 kg bag", "frozen", 0, 0),
    ("potatoes", "Potatoes, raw", 82, 1.9, 0.1, 19.6, 2, 2000, "2 kg bag", "produce", 0, 0),
    ("rapeseed_oil", "Rapeseed oil", 899, 0, 99.9, 0, 0, 920, "1 litre bottle", "condiments", 1, 0),
    (
        "raspberries_frozen",
        "Raspberries, frozen",
        25,
        0.8,
        0.3,
        5.1,
        3.7,
        350,
        "350 g bag",
        "frozen",
        0,
        0,
    ),
    ("red_pepper", "Red pepper, raw", 21, 0.8, 0.2, 4.3, 2.2, 480, "3 peppers", "produce", 0, 0),
    (
        "rice_basmati",
        "Basmati rice, dry",
        351,
        8.1,
        0.5,
        83.7,
        1.1,
        1000,
        "1 kg bag",
        "grains",
        1,
        0,
    ),
    ("rice_brown", "Brown rice, dry", 333, 7.7, 1.5, 77, 3, 1000, "1 kg bag", "grains", 1, 0),
    ("salmon", "Salmon fillets, raw", 217, 20.4, 15, 0, 0.2, 240, "2 fillets", "fish", 0, 1),
    ("salt", "Salt", 0, 0, 0, 0, 0, 750, "750 g tub", "condiments", 1, 0),
    ("skyr", "Skyr, natural", 63, 11, 0.2, 4, 0, 450, "450 g tub", "dairy", 0, 0),
    ("spinach", "Spinach, baby, raw", 16, 2.6, 0.6, 0.2, 1, 240, "240 g bag", "produce", 0, 0),
    (
        "strawberries",
        "Strawberries, raw",
        30,
        0.6,
        0.5,
        6.1,
        3.8,
        400,
        "400 g punnet",
        "produce",
        0,
        0,
    ),
    ("sweet_potato", "Sweet potato, raw", 87, 1.2, 0.3, 21.3, 3, 1000, "1 kg bag", "produce", 0, 0),
    (
        "sweetcorn",
        "Sweetcorn, tinned, drained",
        78,
        2.6,
        1.7,
        13.9,
        3.1,
        285,
        "325 g tin",
        "tins",
        0,
        0,
    ),
    ("tomato_puree", "Tomato puree", 67, 4.4, 0.2, 12.9, 4.7, 200, "200 g tube", "tins", 1, 0),
    ("tuna", "Tuna in brine, drained", 109, 24.9, 1, 0, 0, 102, "145 g tin", "tins", 0, 0),
    ("turkey_breast", "Turkey breast, raw", 105, 24.4, 0.8, 0, 0, 500, "500 g pack", "meat", 0, 0),
    ("whey", "Whey protein powder", 400, 80, 7, 5, 0, 1000, "1 kg bag", "dairy", 1, 0),
    ("wraps", "Tortilla wraps", 285, 7.8, 5.7, 53.9, 3.6, 512, "8 wraps", "bakery", 0, 0),
]


@dataclasses.dataclass(frozen=True)
class Dish:
    name: str
    grams: tuple[tuple[str, float], ...]
    method: tuple[str, ...]
    prep_minutes: int


def _dish(name: str, grams: list[tuple[str, float]], method: list[str], prep: int) -> Dish:
    return Dish(name, tuple(grams), tuple(method), prep)


# -- the real dishes, by their prod recipe id -------------------------------------------------
DISHES: dict[int, Dish] = {
    66: _dish(
        "Overnight oats with skyr, whey and berries",
        [
            ("oats", 40),
            ("skyr", 150),
            ("whey", 20),
            ("raspberries_frozen", 80),
            ("strawberries", 80),
            ("almonds", 10),
        ],
        [
            "Stir the whey into the skyr until smooth.",
            "Mix in the oats and frozen raspberries.",
            "Cover and chill overnight.",
            "Top with sliced strawberries and chopped almonds before eating.",
        ],
        5,
    ),
    67: _dish(
        "Warm chicken, green bean and potato salad",
        [
            ("chicken_breast", 170),
            ("potatoes", 100),
            ("green_beans", 80),
            ("broccoli", 80),
            ("lettuce", 50),
            ("avocado", 50),
            ("olive_oil", 15),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Cut the potatoes into chunks and boil on the hob for 15 minutes.",
            "Brush the chicken with a little of the olive oil, season, and air fry 200C 18 minutes "
            "until cooked through.",
            "Boil the green beans and broccoli for the last 4 minutes with the potatoes, "
            "then drain.",
            "Slice the chicken and avocado.",
            "Toss lettuce, potatoes, greens, chicken and avocado with the remaining "
            "olive oil, salt "
            "and pepper.",
        ],
        30,
    ),
    68: _dish(
        "Garlic turkey with air-fried carrots and potato wedges",
        [
            ("turkey_breast", 210),
            ("potatoes", 100),
            ("carrots", 100),
            ("broccoli", 100),
            ("rapeseed_oil", 17),
            ("garlic", 5),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Cut the potatoes into wedges and the carrots into batons; toss with half the oil and "
            "salt.",
            "Air fry the potatoes and carrots 200C 20 minutes, shaking halfway.",
            "Rub the turkey with crushed garlic, the rest of the oil and pepper.",
            "Pan fry the turkey on the hob over medium heat 5 to 6 minutes each side until cooked "
            "through.",
            "Microwave the broccoli with a splash of water, covered, 3 minutes.",
            "Serve the turkey with the wedges, carrots and broccoli.",
        ],
        35,
    ),
    69: _dish(
        "Strawberry protein yoghurt with almonds",
        [("greek_yoghurt_0", 150), ("whey", 20), ("strawberries", 100), ("almonds", 15)],
        [
            "Stir the whey into the yoghurt until smooth.",
            "Top with sliced strawberries and almonds.",
        ],
        3,
    ),
    70: _dish(
        "Skyr with peanut butter and raspberries",
        [("skyr", 125), ("whey", 10), ("peanut_butter", 15), ("raspberries_frozen", 50)],
        [
            "Microwave the raspberries 40 seconds to thaw.",
            "Stir the whey into the skyr.",
            "Top with the raspberries and peanut butter.",
        ],
        3,
    ),
    # the partner, menu 5 Tuesday (the one they called too complex)
    95: _dish(
        "Scrambled eggs, bacon and spinach on toast",
        [
            ("eggs", 116),
            ("bacon_back", 60),
            ("bread_wholemeal", 70),
            ("spinach", 50),
            ("cherry_tomatoes", 80),
            ("cottage_cheese", 100),
            ("black_pepper", 1),
        ],
        [
            "Air fry the bacon at 180C for 7 to 8 minutes until cooked.",
            "Add the bread to the air fryer for the last 3 minutes to toast.",
            "Meanwhile beat the eggs with the pepper and cook in a non-stick pan on a low hob, "
            "stirring, for 2 to 3 minutes.",
            "Add the spinach to the pan for the last minute so it wilts.",
            "Halve the cherry tomatoes.",
            "Serve the eggs and bacon on the toast, with the cottage cheese and tomatoes on the "
            "side.",
        ],
        15,
    ),
    96: _dish(
        "Tuna, avocado and sweetcorn wrap",
        [
            ("wraps", 64),
            ("tuna", 102),
            ("avocado", 50),
            ("sweetcorn", 50),
            ("lettuce", 50),
            ("red_pepper", 80),
            ("carrots", 80),
            ("mayo_light", 10),
            ("greek_yoghurt_0", 30),
            ("black_pepper", 1),
        ],
        [
            "Drain the tuna and sweetcorn.",
            "Mix the tuna, sweetcorn, mayonnaise, Greek yoghurt and pepper in a bowl.",
            "Mash the avocado with a fork.",
            "Shred the lettuce, slice the red pepper into strips and thinly slice the carrots.",
            "Spread the avocado over the wrap, add the tuna mix and the vegetables, "
            "roll up tightly "
            "and cut in half.",
        ],
        10,
    ),
    97: _dish(
        "Chicken thigh and red lentil curry with rice",
        [
            ("chicken_thigh", 180),
            ("lentils_red", 30),
            ("coconut_milk", 60),
            ("curry_paste", 20),
            ("cauliflower", 100),
            ("spinach", 50),
            ("onions", 60),
            ("chopped_tomatoes", 100),
            ("rice_basmati", 40),
        ],
        [
            "Chop the onion, cut the chicken thighs into chunks and break the "
            "cauliflower into small "
            "florets.",
            "Fry the onion and curry paste in a large pan on a medium hob for 3 minutes.",
            "Add the chicken and cook for 5 minutes, stirring.",
            "Rinse the lentils, then add them with the chopped tomatoes, coconut milk, cauliflower "
            "and 150 ml water. Simmer for 20 minutes, stirring now and then, until the lentils are "
            "soft.",
            "Meanwhile rinse the rice and simmer in a pan of water on the hob for "
            "10 to 12 minutes, "
            "then drain.",
            "Stir the spinach into the curry until wilted and serve with the rice.",
        ],
        35,
    ),
    98: _dish(
        "Skyr with raspberries and peanut butter",
        [("skyr", 170), ("raspberries_frozen", 100), ("peanut_butter", 10)],
        [
            "Microwave the frozen raspberries in a bowl for 1 minute to thaw.",
            "Spoon the skyr over the raspberries.",
            "Top with the peanut butter.",
        ],
        3,
    ),
    # the partner, menu 7 (the chickpea-free rebuild, rotating)
    103: _dish(
        "Porridge with skyr, raspberries and almonds",
        [
            ("oats", 40),
            ("milk_semi", 150),
            ("whey", 15),
            ("skyr", 150),
            ("raspberries_frozen", 80),
            ("almonds", 15),
        ],
        [
            "Put the oats and milk in a microwave-safe bowl.",
            "Microwave on full power for 2 minutes, stir, then 1 more minute.",
            "Stir in the whey protein powder until smooth.",
            "Top with the skyr, raspberries and almonds.",
        ],
        6,
    ),
    104: _dish(
        "Chicken, kidney bean and pepper wrap",
        [
            ("chicken_breast", 120),
            ("olive_oil", 5),
            ("wraps", 64),
            ("kidney_beans", 60),
            ("red_pepper", 80),
            ("lettuce", 40),
            ("cucumber", 50),
            ("mayo_light", 15),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Slice the chicken into strips and season with salt and pepper.",
            "Fry in the olive oil on the hob over a medium-high heat for 7 to 8 minutes "
            "until cooked "
            "through.",
            "Slice the red pepper and cucumber and shred the lettuce.",
            "Warm the wrap in the microwave for 15 seconds.",
            "Spread the mayonnaise over the wrap.",
            "Fill with the chicken, kidney beans, pepper, cucumber and lettuce, then roll up.",
        ],
        15,
    ),
    105: _dish(
        "Beef and kidney bean chilli with brown rice",
        [
            ("beef_mince_5", 150),
            ("olive_oil", 5),
            ("onions", 60),
            ("garlic", 5),
            ("red_pepper", 80),
            ("tomato_puree", 15),
            ("chopped_tomatoes", 150),
            ("kidney_beans", 80),
            ("rice_brown", 50),
            ("avocado", 40),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Put the rice on to boil in salted water on the hob for 25 to 30 minutes, then drain.",
            "Meanwhile, chop the onion, garlic and red pepper.",
            "Fry the onion and pepper in the olive oil for 5 minutes, add the garlic for 1 minute.",
            "Add the beef mince and brown it for 5 minutes, breaking it up.",
            "Stir in the tomato puree, chopped tomatoes and kidney beans, season, and "
            "simmer for 15 "
            "minutes.",
            "Serve the chilli over the rice with the avocado sliced on top.",
        ],
        40,
    ),
    106: _dish(
        "Greek yoghurt with raspberries and peanut butter",
        [("greek_yoghurt_0", 170), ("raspberries_frozen", 30), ("peanut_butter", 15)],
        [
            "Defrost the raspberries in the microwave for 30 seconds.",
            "Spoon the yoghurt into a bowl.",
            "Top with the raspberries and peanut butter.",
        ],
        2,
    ),
    107: _dish(
        "Scrambled eggs on wholemeal toast with avocado and spinach",
        [
            ("eggs", 174),
            ("bread_wholemeal", 70),
            ("avocado", 30),
            ("spinach", 50),
            ("cherry_tomatoes", 80),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Toast the bread in a dry frying pan on the hob for 2 minutes each side, "
            "then set aside.",
            "Beat the eggs with the salt and pepper.",
            "Wilt the spinach in a non-stick pan for 1 minute.",
            "Add the eggs and stir over a low heat for 2 to 3 minutes until just set.",
            "Slice the avocado and halve the cherry tomatoes.",
            "Serve the eggs and spinach on the toast with the avocado and tomatoes.",
        ],
        10,
    ),
    108: _dish(
        "Curried chicken, red lentil and carrot dhal",
        [
            ("chicken_breast", 80),
            ("lentils_red", 50),
            ("carrots", 120),
            ("onions", 60),
            ("garlic", 5),
            ("ginger", 5),
            ("curry_paste", 20),
            ("chicken_stock", 120),
            ("olive_oil", 5),
            ("greek_yoghurt_0", 100),
            ("salt", 1),
        ],
        [
            "Chop the onion, carrots, garlic and ginger, and dice the chicken.",
            "Fry the onion and carrots in the olive oil for 5 minutes.",
            "Add the garlic, ginger and curry paste and cook for 1 minute.",
            "Add the chicken and stir for 2 minutes.",
            "Add the rinsed lentils, stock and salt, and simmer for 20 minutes until thick and the "
            "lentils are "
            "soft.",
            "Serve topped with the Greek yoghurt.",
        ],
        35,
    ),
    109: _dish(
        "Garlic chicken thigh, sweet potato and cauliflower traybake",
        [
            ("chicken_thigh", 150),
            ("sweet_potato", 150),
            ("cauliflower", 150),
            ("green_beans", 100),
            ("olive_oil", 8),
            ("garlic", 5),
            ("salt", 1),
            ("black_pepper", 1),
        ],
        [
            "Heat the oven to 200C.",
            "Cut the sweet potato into chunks and the cauliflower into florets, and "
            "crush the garlic.",
            "Toss the chicken, sweet potato and cauliflower with the olive oil, garlic, salt and "
            "pepper on a baking tray.",
            "Roast for 35 minutes until the chicken is cooked through.",
            "In the last 5 minutes, boil the green beans on the hob, then drain.",
            "Serve the traybake with the green beans.",
        ],
        45,
    ),
    110: _dish(
        "Cottage cheese with apple and almonds",
        [("cottage_cheese", 150), ("apples", 100), ("almonds", 10)],
        [
            "Chop the apple.",
            "Spoon the cottage cheese into a bowl.",
            "Top with the apple and roughly chopped almonds.",
        ],
        3,
    ),
}

# the days as they are on the box
MENU7 = {  # the partner's chickpea-free proposal, Mon-Wed rotating
    MON: {"breakfast": 103, "lunch": 104, "dinner": 105, "pre_bed": 106},
    TUE: {"breakfast": 107, "lunch": 108, "dinner": 109, "pre_bed": 110},
    WED: {"breakfast": 103, "lunch": 104, "dinner": 105, "pre_bed": 106},
}
MENU5_TUE = {"breakfast": 95, "lunch": 96, "dinner": 97, "pre_bed": 98}
MENU6_DAY = {"breakfast": 66, "lunch": 67, "dinner": 68, "snack": 69, "pre_bed": 70}

# -- the same dishes made simple: what Tiffin now asks for and accepts -------
# Each keeps its ingredients as far as the rules allow, so the day still portions to the same
# targets. What changes is the work: one or two appliances, one pan, one thing at a time, 30
# minutes. These are the answers the model gives once it is told why its first menu was refused.
SIMPLE: dict[int, Dish] = {
    67: _dish(
        "Warm chicken, green bean and potato salad",
        list(DISHES[67].grams),
        [
            "Cut the potatoes into small chunks, toss with a little of the oil and air fry 200C "
            "15 minutes.",
            "Add the seasoned chicken breast and air fry 200C 12 minutes more until "
            "cooked through.",
            "Microwave the green beans and broccoli with a splash of water, covered, 3 minutes.",
            "Slice the chicken and avocado and toss everything with the lettuce, the rest of the "
            "oil, salt and pepper.",
        ],
        30,
    ),
    68: _dish(
        "Garlic turkey with air-fried carrots and potato wedges",
        list(DISHES[68].grams),
        [
            "Cut the potatoes into wedges and the carrots into batons; toss with half the oil and "
            "salt.",
            "Air fry the potatoes and carrots 200C 12 minutes.",
            "Rub the turkey with crushed garlic, the rest of the oil and pepper; add it and the "
            "broccoli and air fry 200C 15 minutes more until cooked through.",
            "Serve the turkey with the wedges, carrots and broccoli.",
        ],
        27,
    ),
    105: _dish(
        "Beef and kidney bean chilli with brown rice",
        [g for g in DISHES[105].grams if g[0] != "garlic"],
        [
            "Fry the onion and red pepper in the olive oil in a large pan for 5 minutes.",
            "Add the beef mince and brown it for 5 minutes, breaking it up.",
            "Stir in the tomato puree, chopped tomatoes, kidney beans, rice, salt, "
            "pepper and 300 ml "
            "water.",
            "Cover and simmer for 20 minutes, stirring now and then, until the rice is soft.",
            "Serve with the avocado sliced on top.",
        ],
        30,
    ),
    107: _dish(
        "Scrambled eggs on wholemeal toast with avocado and spinach",
        list(DISHES[107].grams),
        [
            "Toast the bread.",
            "Beat the eggs with the salt and pepper and stir them with the spinach in a non-stick "
            "pan over a low heat for 3 minutes until just set.",
            "Serve on the toast with the avocado sliced and the tomatoes halved.",
        ],
        6,
    ),
    108: _dish(
        "Curried chicken, red lentil and carrot dhal",
        [g for g in DISHES[108].grams if g[0] != "ginger"],
        [
            "Chop the onion, carrots and garlic, and dice the chicken.",
            "Fry the onion and carrots in the olive oil in a large pan for 5 minutes.",
            "Add the garlic, curry paste and chicken and stir for 3 minutes.",
            "Add the rinsed lentils, stock and salt and simmer for 20 minutes until thick.",
            "Serve topped with the Greek yoghurt.",
        ],
        28,
    ),
    109: _dish(
        "Garlic chicken thigh, sweet potato and cauliflower traybake",
        list(DISHES[109].grams),
        [
            "Cut the sweet potato into small chunks and the cauliflower into florets.",
            "Toss with the chicken, green beans, olive oil, crushed garlic, salt and pepper.",
            "Air fry at 200C for 25 minutes, shaking halfway, until the chicken is cooked through.",
        ],
        25,
    ),
}


def simpler(days: dict[dt.date, dict[str, int]]) -> dict[dt.date, dict[str, int | Dish]]:
    return {d: {s: SIMPLE.get(k, DISHES[k]) for s, k in slots.items()} for d, slots in days.items()}


SIMPLE7 = simpler(MENU7)
SIMPLE6_DAY = simpler({MON: MENU6_DAY})[MON]

#: nobody shops for these, and a card is not complicated because it says "season"
SEASONING = {"Salt", "Black pepper, ground", "Olive oil", "Rapeseed oil"}
#: the protein-rich things a person would call "the protein" of a plate
PROTEINS = {
    "Eggs, whole, raw",
    "Back bacon, fat trimmed, raw",
    "Cottage cheese",
    "Skyr, natural",
    "Whey protein powder",
    "Greek yoghurt 0% fat",
    "Chicken breast, raw",
    "Chicken thigh fillets, skinless, raw",
    "Turkey breast, raw",
    "Beef mince 5% fat, raw",
    "Salmon fillets, raw",
    "Tuna in brine, drained",
    "Feta cheese",
}
APPLIANCES = {
    "air fryer": re.compile(r"\bair[- ]?fr", re.I),
    "oven": re.compile(r"\boven\b|\broast\b|baking tray|\bbake\b", re.I),
    "hob": re.compile(r"\bhob\b|\bpan\b|\bsimmer|\bboil|(?<!air )(?<!air-)\bfr(?:y|ied)\b", re.I),
    "microwave": re.compile(r"\bmicrowave", re.I),
}
NEW_PAN = re.compile(r"\ba (?:large |small |non-stick |dry |frying )*pan\b", re.I)
JUGGLING = re.compile(r"\bmeanwhile\b|\bwhile it cooks\b|\bin the last \d+ minutes\b", re.I)
MINUTES = re.compile(r"(\d+)(?:\s*(?:to|-)\s*(\d+))?\s*minutes?\b", re.I)


# -- reading the card the way a person does ---------------------------------------------------
@dataclasses.dataclass
class Card:
    text: str
    ingredients: list[str]  # the ingredient lines as printed
    steps: list[str]
    minutes: int

    @property
    def names(self) -> list[str]:
        return [re.split(r" \d+ g", line, maxsplit=1)[0] for line in self.ingredients]

    @property
    def to_shop(self) -> list[str]:
        return [n for n in self.names if n not in SEASONING]

    @property
    def appliances(self) -> list[str]:
        return sorted(a for a, rx in APPLIANCES.items() if any(rx.search(s) for s in self.steps))

    @property
    def pans(self) -> int:
        return sum(len(NEW_PAN.findall(s)) for s in self.steps)

    @property
    def proteins(self) -> list[str]:
        return [n for n in self.names if n in PROTEINS]

    @property
    def juggling(self) -> list[str]:
        return [s for s in self.steps if JUGGLING.search(s)]

    @property
    def longest_step_minutes(self) -> int:
        best = 0
        for s in self.steps:
            for lo, hi in MINUTES.findall(s):
                best = max(best, int(hi or lo))
        return best

    def summary(self) -> str:
        return (
            f"{len(self.to_shop)} ingredients to shop {self.to_shop}, {len(self.steps)} steps, "
            f"appliances {self.appliances}, {self.pans} pans, {self.minutes} min on the card, "
            f"proteins {self.proteins}, juggling {len(self.juggling)}"
        )


def read_card(text: str) -> Card:
    lines = text.splitlines()
    assert "<b>Ingredients</b>" in lines, f"not a recipe card: {text[:200]}"
    i = lines.index("<b>Ingredients</b>")
    j = lines.index("<b>Method</b>") if "<b>Method</b>" in lines else len(lines)
    ingredients = [ln for ln in lines[i + 1 : j] if re.search(r" \d+ g\b", ln)]
    steps = [re.sub(r"^\d+\. ", "", ln) for ln in lines[j + 1 :] if re.match(r"^\d+\. ", ln)]
    found = re.search(r"· (\d+) min\b", text)
    assert found is not None, f"no minutes on the card: {text[:300]}"
    return Card(text, ingredients, steps, int(found.group(1)))


# -- the world --------------------------------------------------------------------------------
PROFILES = {
    "owner": {
        "sex": "m",
        "birth_date": "1998-04-30",
        "height_cm": 186.0,
        "activity_factor": 1.375,
        "steps_floor": 10000,
        "goal_weight_kg": 85.0,
        "goal_date": "2026-12-31",
        "rate_pct_per_week": 1.0,
        "kcal_floor": 1850,
        "protein_g_per_kg": 2.0,
        "fat_g_per_kg_min": 0.7,
        "fibre_g_min": 25,
        "max_loss_kg_week": 0.8,
        "variety_json": '{"all": "same"}',
        "segment_days": "[[0, 1, 2, 3, 4]]",
        "meals_per_day": 4,
        "pre_bed_protein": 1,
        "tone": "blunt",
        "refused_foods": '["Chickpea"]',
        "liked_foods": "[]",
        "pantry_json": "[]",
        "equipment_json": '["air_fryer", "hob", "microwave", "oven"]',
        "batch_slots": '["lunch", "dinner"]',
        "freezes": 1,
    },
    "partner": {
        "sex": "f",
        "birth_date": "1996-12-20",
        "height_cm": 172.0,
        "activity_factor": 1.2,
        "steps_floor": 10000,
        "goal_weight_kg": 55.0,
        "goal_date": "2026-12-31",
        "rate_pct_per_week": 0.7,
        "kcal_floor": 1600,
        "protein_g_per_kg": 2.0,
        "fat_g_per_kg_min": 0.7,
        "fibre_g_min": 25,
        "max_loss_kg_week": 0.8,
        "variety_json": '{"all": "rotate"}',
        "segment_days": "[[0, 1, 2], [3, 4]]",
        "cycle_tracking": 1,
        "meals_per_day": 3,
        "pre_bed_protein": 1,
        "tone": "warm",
        "refused_foods": '["Liver", "broccoli", "chickpea"]',
        "liked_foods": '["Eggs", "chicken", "avocado", "cheese"]',
        "pantry_json": "[]",
        "equipment_json": '["air_fryer", "hob", "microwave", "oven"]',
        "batch_slots": '["lunch", "dinner"]',
        "freezes": 0,
    },
}
TARGETS = {"owner": (1914, 201, 71, 118, 27), "partner": (1600, 132, 47, 162, 25)}


@dataclasses.dataclass
class Kitchen:
    db: Db
    clock: FrozenClock
    tg: FakeTelegram
    app: App
    poller: Poller
    ids: dict[str, int]
    gateways: list[FakeGateway]
    n: list[int]

    def at(self, day: dt.date, hh: int, mm: int = 0) -> None:
        self.clock.set_local(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ))

    def chat(self, who: str) -> int:
        return OWNER_CHAT if who == "owner" else PARTNER_CHAT

    def say(self, who: str, text: str) -> list[str]:
        before = len(self.tg.sent)
        self.n[0] += 1
        self.tg.queue_update(self.tg.text_update(self.n[0], self.chat(who), text))
        self.poller.run_once()
        return [m["text"] for m in self.tg.sent[before:]]

    def tap(self, who: str, data: str) -> str:
        before = len(self.tg.sent)
        self.n[0] += 1
        self.tg.queue_update(
            self.tg.callback_update(self.n[0], self.chat(who), data, message_id=self.n[0])
        )
        self.poller.run_once()
        sent = self.tg.sent[before:]
        assert sent, f"tapping {data} sent nothing"
        return sent[-1]["text"]

    def dish(self, who: str, key: int | Dish) -> dict[str, Any]:
        """A dish as the model writes it: indexes into THIS person's numbered catalogue."""
        d = DISHES[key] if isinstance(key, int) else key
        cat = catalogue.approved(self.db, self.ids[who])
        index = {ing.code: n for n, ing in enumerate(cat, start=1)}
        return {
            "name": d.name,
            "ingredients": [{"i": index[code], "g": g} for code, g in d.grams],
            "method": list(d.method),
            "prep_minutes": d.prep_minutes,
        }

    def payload(self, who: str, days: Mapping[dt.date, Mapping[str, int | Dish]]) -> dict[str, Any]:
        return {
            "days": [
                {
                    "day": day.isoformat(),
                    "slots": [{"slot": s, "recipe": self.dish(who, k)} for s, k in slots.items()],
                }
                for day, slots in days.items()
            ]
        }

    def scheduled_menu(self, who: str, run_day: dt.date, *payloads: dict[str, Any]) -> str:
        """The 18:00 menu_propose job, two days before the segment, with the model's answers."""
        self.at(run_day, 18, 0)
        self.gateways.append(FakeGateway(list(payloads)))
        pid = self.ids[who]
        s = sc.Scheduler(self.app, [menu_propose.JOB], lambda p, fn: None)
        prof = self.db.one(
            "SELECT * FROM profiles WHERE person_id = ? ORDER BY id DESC LIMIT 1", (pid,)
        )
        slots = sc.slots_for(menu_propose.JOB, pid, prof, run_day, self.db)
        assert slots, f"no menu_propose slot for {who} on {run_day}"
        return s.execute(self.db, slots[0])

    def latest_menu(self, who: str) -> int:
        mid = self.db.scalar(
            "SELECT id FROM menus WHERE person_id = ? ORDER BY id DESC LIMIT 1", (self.ids[who],)
        )
        assert mid is not None, f"no menu for {who}; ledger: {self.ledger()}"
        return int(mid)

    def ledger(self) -> list[str]:
        return [
            f"{r['status']}: {r['detail']}"
            for r in self.db.all("SELECT status, detail FROM run_ledger ORDER BY started_at")
        ]

    def in_force(self, who: str, menu_id: int) -> None:
        """Return the menu already in force after it is built and sent."""
        decision = self.db.scalar(
            "SELECT decision FROM menu_decisions WHERE menu_id = ? ORDER BY id DESC LIMIT 1",
            (menu_id,),
        )
        assert decision == "accepted", f"{who}'s menu {menu_id} is not in force: {decision}"

    def open_meal(self, who: str, menu_id: int, slot: str, day: dt.date) -> Card:
        return read_card(self.tap(who, f"k:meal:{menu_id}:{slot}:{day:%Y%m%d}"))


@pytest.fixture
def kitchen(fixture_db: Db, two_persons, frozen_clock: FrozenClock, config, tmp_path) -> Kitchen:
    # these stories are about simple dishes, and her canned week cooks beef, chicken breast and
    # chicken thigh: the shop window's protein budget is off here, so
    # each answer is judged on its simplicity alone. tests/test_packs_build.py holds the budget
    config.data["guardrails"]["mains_per_window"] = 0
    for row in REAL_INGREDIENTS:
        code, name, kcal, p, f, c, fib, pack, label, aisle, pantry, whole = row
        fixture_db.insert(
            "ingredients",
            code=code,
            version=1,
            name=name,
            kcal_100=kcal,
            protein_100=p,
            fat_100=f,
            carb_100=c,
            fibre_100=fib,
            pack_g=pack,
            pack_label=label,
            aisle=aisle,
            pantry=pantry,
            use_whole_pack=whole,
            approved=1,
            person_scope="both",
        )
        fixture_db.insert(
            "ingredient_aliases", ingredient_code=code, alias=name.lower(), source="yaml"
        )
    for who, fields in PROFILES.items():
        pid = two_persons[who]
        fixture_db.insert("profiles", person_id=pid, effective_from="2026-09-01", **fields)
        kcal, p, f, c, fib = TARGETS[who]
        fixture_db.insert(
            "targets",
            person_id=pid,
            week_start="2026-09-14",
            kcal=kcal,
            protein_g=p,
            fat_g=f,
            carb_g=c,
            fibre_g=fib,
            source="formula",
        )
    secrets_dir = tmp_path / "s"
    secrets_dir.mkdir()
    (secrets_dir / "telegram-token-dev.txt").write_text("1:t", encoding="utf-8")
    tg = FakeTelegram()
    gateways: list[FakeGateway] = []
    app = App(
        config,
        Secrets(secrets_dir),
        clock=frozen_clock,
        transport=tg,
        gateway_factory=lambda _app, _db: gateways[-1],  # type: ignore[arg-type]
    )
    handlers = app.handlers(fixture_db)
    # a shell is dev and dev only ever sends to the owner; flip the bot's outbox to prod so her
    # replies
    # land in the fake Telegram (as test_journey_batch_cook does). Nothing leaves the process.
    handlers.outbox.env = "prod"
    poller = Poller(
        app.api(),
        fixture_db,
        app.registry(fixture_db),
        handlers.handle,
        clock=frozen_clock,
        sleeper=lambda s: None,
    )
    return Kitchen(fixture_db, frozen_clock, tg, app, poller, dict(two_persons), gateways, [0])


def partner_week(
    k: Kitchen,
    days: dict[dt.date, dict[str, Any]],
    then: dict[dt.date, dict[str, Any]] | None = None,
) -> int:
    """Saturday 18:00 builds her Mon-Wed menu from these dishes; she accepts it.

    `then` is the model's second answer, given after Tiffin refuses the first and says why.
    """
    answers = [k.payload("partner", days)] + ([k.payload("partner", then)] if then else [])
    status = k.scheduled_menu("partner", SAT, *answers)
    assert status in ("ok", "degraded"), (status, k.ledger())
    mid = k.latest_menu("partner")
    k.in_force("partner", mid)
    return mid


# -- the stories ------------------------------------------------------------------------------
def test_01_a_work_morning_breakfast_is_one_pan_and_a_few_steps(kitchen: Kitchen) -> None:
    """Tuesday 07:10, before work: the partner opens today's breakfast - the eggs with bacon she
    called
    too complex - and wants something she can make with one pan and a handful of steps.

    The model offers the bacon breakfast again. Tiffin refuses it, says why, and what
    reaches her is the simple answer that comes back - the complex one never does."""
    k = kitchen
    mid = partner_week(k, {MON: MENU7[MON], TUE: MENU5_TUE, WED: MENU7[WED]}, then=SIMPLE7)
    k.at(TUE, 7, 10)
    card = k.open_meal("partner", mid, "breakfast", TUE)
    assert "bacon" not in card.text.lower(), "the breakfast they called too complex reached her"
    assert "not simple" in k.gateways[-1].calls[1], "the model was told why it was refused"
    simple = len(card.appliances) <= 1 and len(card.steps) <= 4 and len(card.proteins) <= 2
    assert simple, (
        "a work-morning breakfast should be one appliance, <= 4 steps, one or two proteins; "
        f"she got {card.summary()}"
    )


def test_02_a_weeknight_dinner_after_work_fits_in_thirty_minutes(kitchen: Kitchen) -> None:
    """Tuesday 18:40, just home from work: she opens tonight's dinner and wants it on the plate in
    half an hour from one appliance."""
    k = kitchen
    mid = partner_week(k, MENU7, then=SIMPLE7)
    k.at(TUE, 18, 40)
    card = k.open_meal("partner", mid, "dinner", TUE)
    assert card.minutes <= 30 and len(card.appliances) <= 1, (
        f"a weeknight dinner should be <= 30 min from one appliance; she got {card.summary()}"
    )


def test_03_the_brief_for_partner_week_asks_for_weeknight_food(kitchen: Kitchen) -> None:
    """Saturday 18:00: Tiffin writes the brief the model cooks her week from. She will cook
    every one of those dishes after work, so the brief has to ask for that - short and simple."""
    k = kitchen
    k.scheduled_menu("partner", SAT, k.payload("partner", MENU7))
    prompt = k.gateways[-1].calls[0]
    ing = re.search(r"at most (\d+) ingredients", prompt)
    mins = re.search(r"at most (\d+) minutes", prompt)
    caps = (int(ing.group(1)) if ing else 99, int(mins.group(1)) if mins else 999)
    asks_simple = bool(re.search(r"\bsimple\b|\bone pan\b|\bone tray\b", prompt, re.I))
    assert caps[0] <= 8 and caps[1] <= 30 and asks_simple, (
        f"the brief allows {caps[0]} ingredients and {caps[1]} minutes a dish and says "
        f"'simple'/'one pan': {asks_simple}. Rules line: "
        + next((ln for ln in prompt.splitlines() if ln.startswith("Rules:")), "(none)")
    )


def test_04_a_dish_longer_than_the_brief_allows_never_reaches_partner(kitchen: Kitchen) -> None:
    """Tuesday 18:40: the brief promised "at most 8 method steps a tired person can follow". She
    opens tonight's dinner and should never find a ten-step recipe on it."""
    k = kitchen
    long_traybake = Dish(
        "Garlic chicken thigh, sweet potato and cauliflower traybake",
        DISHES[109].grams,
        (
            "Heat the oven to 200C.",
            "Peel the sweet potato.",
            "Cut the sweet potato into chunks.",
            "Break the cauliflower into florets.",
            "Crush the garlic.",
            "Toss everything but the beans with the oil, garlic, salt and pepper.",
            "Spread on a baking tray.",
            "Roast for 35 minutes.",
            "Boil the green beans on the hob for 5 minutes.",
            "Drain the beans and serve with the traybake.",
        ),
        45,
    )
    mid = partner_week(
        k,
        {MON: MENU7[MON], TUE: {**MENU7[TUE], "dinner": long_traybake}, WED: MENU7[WED]},
        then=SIMPLE7,
    )
    k.at(TUE, 18, 40)
    card = k.open_meal("partner", mid, "dinner", TUE)
    assert len(card.steps) <= 8, (
        f"the brief said at most 8 steps; the card she cooks from has {len(card.steps)}. "
        f"Model calls: {len(k.gateways[-1].calls)} (a rejected dish would have been re-asked)"
    )


def test_05_asking_for_simple_gets_partner_simple(kitchen: Kitchen) -> None:
    """Monday 13:00, after the shop: she sends "/cook replace simple please, nothing
    complicated" and opens Wednesday's dinner on the menu that comes back."""
    k = kitchen
    k.at(MON, 13, 0)
    k.gateways.append(
        FakeGateway(
            [
                k.payload("partner", {TUE: MENU7[TUE], WED: MENU7[WED]}),
                k.payload("partner", {TUE: SIMPLE7[TUE], WED: SIMPLE7[WED]}),
            ]
        )
    )
    said = k.say("partner", "/cook replace simple please, nothing complicated")
    assert any("Menu for the next cook" in s for s in said), said
    prompt = k.gateways[-1].calls[0]
    assert "simple please" in prompt, "her words reached the model"
    mid = k.latest_menu("partner")
    card = k.open_meal("partner", mid, "dinner", WED)
    too_many = [
        n
        for n, v in (
            ("ingredients", len(card.to_shop) > 8),
            ("pans", card.pans > 1),
            ("minutes", card.minutes > 30),
        )
        if v
    ]
    assert not too_many, (
        f"she asked for simple and got {card.summary()}; nothing checked it against her words "
        f"(model calls: {len(k.gateways[-1].calls)})"
    )


def test_06_simple_is_still_what_partner_wants_on_tuesday(kitchen: Kitchen) -> None:
    """Tuesday 18:00: she asked for simple food on Monday. Tiffin now plans her Thursday and
    Friday, and the brief for them should still say simple."""
    k = kitchen
    k.at(MON, 13, 0)
    k.gateways.append(FakeGateway([k.payload("partner", {TUE: SIMPLE7[TUE], WED: SIMPLE7[WED]})]))
    k.say("partner", "/cook replace simple please, nothing complicated")
    thu_fri = {THU: SIMPLE7[MON], FRI: SIMPLE7[TUE]}
    status = k.scheduled_menu("partner", TUE, k.payload("partner", thu_fri))
    assert status in ("ok", "degraded"), (status, k.ledger())
    prompt = k.gateways[-1].calls[0]
    asked = re.search(r"What they asked for[^\n]*: ([^\n]*)", prompt)
    assert re.search(r"simple", prompt, re.I), (
        "Monday's 'simple please, nothing complicated' is gone from Thursday's brief; it says: "
        f"{asked.group(1) if asked else '(no asked-for line)'}"
    )


def test_07_thursday_lunch_at_partner_desk_needs_no_hob(kitchen: Kitchen) -> None:
    """Thursday 12:40, at her desk: she opens Thursday lunch - batch-cooked, boxed - and needs to
    know what to do with it there, where there is a microwave and nothing else."""
    k = kitchen
    thu_fri = {THU: SIMPLE7[MON], FRI: SIMPLE7[TUE]}
    status = k.scheduled_menu("partner", TUE, k.payload("partner", thu_fri))
    assert status in ("ok", "degraded"), (status, k.ledger())
    mid = k.latest_menu("partner")
    k.in_force("partner", mid)
    k.at(THU, 12, 40)
    card = k.open_meal("partner", mid, "lunch", THU)
    at_desk = [
        s
        for s in card.steps
        if {"hob", "oven", "air fryer"} & {a for a, rx in APPLIANCES.items() if rx.search(s)}
    ]
    split = re.search(
        r"cook day|the night before|ahead|at (?:your|the) desk|on the day", card.text, re.I
    )
    assert not at_desk or split, (
        f"the lunch card has {len(at_desk)} steps needing a hob/oven/air fryer and no word on "
        f"what is done ahead vs at the desk: {at_desk}"
    )


def test_08_a_nervous_cook_is_never_asked_to_run_two_pans_at_once(kitchen: Kitchen) -> None:
    """Wednesday 18:30: the partner, not a confident cook, opens tonight's chilli and wants to do
    one
    thing at a time in one pan, not start rice and then "meanwhile" build a sauce."""
    k = kitchen
    mid = partner_week(k, MENU7, then=SIMPLE7)
    k.at(WED, 18, 30)
    card = k.open_meal("partner", mid, "dinner", WED)
    assert "chilli" in card.text.lower()
    assert card.pans <= 1 and not card.juggling, (
        f"{card.pans} pans and {len(card.juggling)} parallel steps {card.juggling}; "
        f"{card.summary()}"
    )


def test_09_owner_monday_dinner_is_not_three_appliances(kitchen: Kitchen) -> None:
    """Monday 18:30: the owner opens tonight's dinner on his Mon-Fri menu and wants to cook it with
    one or two appliances, not the air fryer, a frying pan and the microwave at once."""
    k = kitchen
    week = {MON + dt.timedelta(days=i): MENU6_DAY for i in range(5)}
    simple_week = {MON + dt.timedelta(days=i): SIMPLE6_DAY for i in range(5)}
    status = k.scheduled_menu(
        "owner", SAT, k.payload("owner", week), k.payload("owner", simple_week)
    )
    assert status in ("ok", "degraded"), (status, k.ledger())
    mid = k.latest_menu("owner")
    k.in_force("owner", mid)
    k.at(MON, 18, 30)
    card = k.open_meal("owner", mid, "dinner", MON)
    assert len(card.appliances) <= 2, f"Alex's dinner needs {card.appliances}; {card.summary()}"


def test_10_the_minutes_on_the_card_are_the_minutes_it_takes(kitchen: Kitchen) -> None:
    """Tuesday 18:40: she picks tonight's dinner because the card says 10 min. The method says
    to roast for 35 minutes; the number she planned her evening around has to be the real one."""
    k = kitchen
    # simple in every other way, so the only thing wrong with it is the number on the card
    fibbed = Dish(
        "Air-fried chicken thigh with sweet potato and cauliflower",
        DISHES[109].grams,
        (
            "Toss everything with the oil, garlic, salt and pepper.",
            "Air fry at 200C for 25 minutes, shaking halfway, until the chicken is cooked through.",
        ),
        10,
    )
    mid = partner_week(
        k, {MON: SIMPLE7[MON], TUE: {**SIMPLE7[TUE], "dinner": fibbed}, WED: SIMPLE7[WED]}
    )
    k.at(TUE, 18, 40)
    card = k.open_meal("partner", mid, "dinner", TUE)
    assert card.minutes >= card.longest_step_minutes, (
        f"the card says {card.minutes} min; one step alone takes {card.longest_step_minutes}"
    )


def test_11_eggs_are_counted_in_eggs_not_grams(kitchen: Kitchen) -> None:
    """Tuesday 07:10: she is cracking eggs for breakfast and needs to know how many, not how many
    grams - nobody weighs an egg before work."""
    k = kitchen
    mid = partner_week(k, SIMPLE7)
    k.at(TUE, 7, 10)
    card = k.open_meal("partner", mid, "breakfast", TUE)
    line = next(ln for ln in card.ingredients if ln.startswith("Eggs"))
    outside_pack = re.sub(r"\(.*?\)", "", line)
    assert re.search(r"\b[1-9]\d? eggs?\b", outside_pack), (
        f"the card says {line!r}: grams of egg, the pack in brackets, no count"
    )


def test_12_plain_eggs_on_toast_survives_the_portioning(kitchen: Kitchen) -> None:
    """Monday 13:00: she asks for simple and the model gives her plain scrambled eggs on toast
    for Tuesday. Tiffin should keep that simple breakfast, not send it back for more protein
    until the bacon and cottage cheese return."""
    k = kitchen
    plain = Dish(
        "Scrambled eggs on toast",
        (("eggs", 116), ("bread_wholemeal", 70), ("spinach", 50)),
        (
            "Toast the bread.",
            "Scramble the eggs in a non-stick pan over a low heat for 3 minutes.",
            "Stir in the spinach until it wilts and pile onto the toast.",
        ),
        8,
    )
    simple = k.payload("partner", {TUE: {**SIMPLE7[TUE], "breakfast": plain}, WED: SIMPLE7[WED]})
    complex_again = k.payload("partner", {TUE: MENU5_TUE, WED: MENU7[WED]})
    k.at(MON, 13, 0)
    k.gateways.append(FakeGateway([simple, complex_again, complex_again, complex_again]))
    k.say("partner", "/cook replace simple please")
    calls = k.gateways[-1].calls
    mid = k.latest_menu("partner")
    card = k.open_meal("partner", mid, "breakfast", TUE)
    retry = calls[1].split("The previous attempt failed:", 1)[-1][:400] if len(calls) > 1 else ""
    assert card.text.splitlines()[1].startswith("<b>Scrambled eggs on toast</b>") or (
        "<b>Scrambled eggs on toast</b>" in card.text
    ), (
        f"the plain breakfast was sent back ({len(calls)} model calls); she got "
        f"{card.summary()}. The retry told the model: {retry!r}"
    )
