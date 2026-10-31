"""Ingredient catalogue: config/ingredients.yaml x the CoFID CSV -> versioned `ingredients` rows.

`build` inserts a new version row for a code whenever anything about it changed (the ledger is
append-only; readers take the latest version per code). `approved(db, person_id)` numbers what a
person may be offered 1..N, stable by code — the model only ever returns those indexes
(index-not-quote), so the numbers Tiffin states always come from these rows.

Approval needs a fibre value: a CoFID 'N' (present, amount unknown) leaves fibre NULL and the
row is stored with approved = 0 until a `macros_100` override in the YAML supplies label values.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import random
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..clock import Clock
from ..db import Db
from ..errors import ConfigError
from .text import normalise

DEFAULT_AISLES: tuple[str, ...] = (
    "produce",
    "meat",
    "fish",
    "dairy",
    "bakery",
    "grains",
    "tins",
    "frozen",
    "condiments",
)
SCOPES = ("both", "owner", "partner")
MIN_PLURAL_LEN = 3  # shorter than this and a trailing 's' is part of the word, not a plural
MIN_OES_LEN = 4  # 'tomatoes' -> 'tomato', but 'toes' -> 'toe' via the plain '-s' rule
_CODE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_MACRO_KEYS = ("kcal", "protein_g", "fat_g", "carb_g", "fibre_g")

LATEST_SQL = (
    "SELECT i.* FROM ingredients i WHERE i.id = (SELECT q.id FROM ingredients q "
    "WHERE q.code = i.code ORDER BY q.version DESC, q.id DESC LIMIT 1) ORDER BY i.code"
)


# ---------------------------------------------------------------- data classes
@dataclass(frozen=True)
class Ingredient:
    """The latest version row of one catalogue code, per 100 g, plus its pack and aliases."""

    id: int
    code: str
    version: int
    name: str
    cofid_code: str | None
    kcal_100: float
    protein_100: float
    fat_100: float
    carb_100: float
    fibre_100: float | None
    pack_g: float
    pack_label: str
    aisle: str
    pantry: bool
    use_whole_pack: bool
    approved: bool
    person_scope: str
    aliases: tuple[str, ...] = ()
    pack_sizes_g: tuple[float, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row, aliases: Iterable[str] = ()) -> Ingredient:
        return cls(
            id=int(row["id"]),
            code=str(row["code"]),
            version=int(row["version"]),
            name=str(row["name"]),
            cofid_code=None if row["cofid_code"] is None else str(row["cofid_code"]),
            kcal_100=float(row["kcal_100"]),
            protein_100=float(row["protein_100"]),
            fat_100=float(row["fat_100"]),
            carb_100=float(row["carb_100"]),
            fibre_100=None if row["fibre_100"] is None else float(row["fibre_100"]),
            pack_g=float(row["pack_g"]),
            pack_label=str(row["pack_label"]),
            pack_sizes_g=tuple(json.loads(str(row["pack_sizes_g"]))),
            aisle=str(row["aisle"]),
            pantry=bool(row["pantry"]),
            use_whole_pack=bool(row["use_whole_pack"]),
            approved=bool(row["approved"]),
            person_scope=str(row["person_scope"]),
            aliases=tuple(aliases),
        )

    def prompt_line(self, index: int) -> str:
        """'12. Chicken breast, raw — 165 kcal, 31 P, 3.6 F, 0 C, 0 fibre /100 g (pack 500 g)'."""
        fibre = "n/a" if self.fibre_100 is None else num(self.fibre_100)
        pack = f"pack {num(self.pack_g)} g"
        label = self.pack_label.strip()
        if label and not label.startswith(f"{num(self.pack_g)} g"):
            pack += f", {label}"
        return (
            f"{index}. {self.name} — {num(self.kcal_100)} kcal, {num(self.protein_100)} P, "
            f"{num(self.fat_100)} F, {num(self.carb_100)} C, {fibre} fibre /100 g ({pack})"
        )


@dataclass(frozen=True)
class Unit:
    """A food served whole on a plate: what one is called ("slice") and what one weighs."""

    name: str
    g: float


@dataclass(frozen=True)
class YamlIngredient:
    code: str
    name: str
    cofid_code: str | None
    pack_g: float
    pack_label: str
    pack_sizes_g: tuple[float, ...]
    aisle: str
    pantry: bool
    use_whole_pack: bool
    aliases: tuple[str, ...]
    person_scope: str
    macros_100: Mapping[str, float] | None  # label values when the food is not in CoFID
    fibre_100: float | None  # fills a CoFID 'N'/blank fibre only; a documented decision
    unit: Unit | None = None  # served whole: bread by the slice, eggs, buns


@dataclass(frozen=True)
class CatalogueYaml:
    aisles: tuple[str, ...]
    ingredients: tuple[YamlIngredient, ...]


@dataclass(frozen=True)
class CofidRow:
    code: str
    name: str
    kcal: float | None
    protein_g: float | None
    fat_g: float | None
    carb_g: float | None
    fibre_g: float | None


@dataclass
class BuildResult:
    inserted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing_cofid: list[str] = field(default_factory=list)
    fibre_null: list[str] = field(default_factory=list)
    aliases_added: int = 0
    units_set: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- loading
def num(x: float) -> str:
    """165.0 -> '165', 3.6 -> '3.6', 0.0 -> '0' (one decimal at most)."""
    s = f"{x:.1f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _bool(v: Any, what: str) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    raise ConfigError(f"{what}: expected true/false, got {v!r}")


def _macros_override(v: Any, code: str) -> Mapping[str, float] | None:
    if v is None:
        return None
    if not isinstance(v, Mapping):
        raise ConfigError(f"ingredient {code}: macros_100 must be a mapping")
    out: dict[str, float] = {}
    for key in _MACRO_KEYS:
        if key not in v:
            raise ConfigError(f"ingredient {code}: macros_100 missing {key}")
        try:
            out[key] = float(v[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"ingredient {code}: macros_100.{key} not a number") from exc
    return out


def load_yaml(path: Path) -> CatalogueYaml:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path}: top level must be a mapping")
    aisles = tuple(str(a) for a in (raw.get("aisles") or DEFAULT_AISLES))
    items = raw.get("ingredients")
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path}: 'ingredients' must be a non-empty list")
    seen: set[str] = set()
    out: list[YamlIngredient] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{path}: every ingredient must be a mapping")
        code = str(entry.get("code", ""))
        if not _CODE.match(code):
            raise ConfigError(f"{path}: bad ingredient code {code!r}")
        if code in seen:
            raise ConfigError(f"{path}: duplicate ingredient code {code}")
        seen.add(code)
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"ingredient {code}: name missing")
        aisle = str(entry.get("aisle", ""))
        if aisle not in aisles:
            raise ConfigError(f"ingredient {code}: aisle {aisle!r} not in {list(aisles)}")
        scope = str(entry.get("person_scope", "both"))
        if scope not in SCOPES:
            raise ConfigError(f"ingredient {code}: person_scope {scope!r} not in {SCOPES}")
        try:
            pack_g = float(entry["pack_g"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"ingredient {code}: pack_g missing or not a number") from exc
        if not math.isfinite(pack_g) or pack_g <= 0:
            raise ConfigError(f"ingredient {code}: pack_g must be > 0")
        raw_sizes = entry.get("pack_sizes_g") or []
        if not isinstance(raw_sizes, list) or any(
            not isinstance(size, int | float)
            or isinstance(size, bool)
            or not math.isfinite(size)
            or size <= 0
            for size in raw_sizes
        ):
            raise ConfigError(f"ingredient {code}: pack_sizes_g must be positive numbers")
        pack_sizes_g = tuple(sorted({pack_g, *(float(size) for size in raw_sizes)}))
        aliases_raw = entry.get("aliases") or []
        if not isinstance(aliases_raw, list):
            raise ConfigError(f"ingredient {code}: aliases must be a list")
        aliases = tuple(dict.fromkeys(str(a).strip() for a in aliases_raw if str(a).strip()))
        cofid = entry.get("cofid_code")
        fibre_raw = entry.get("fibre_100")
        try:
            fibre_100 = None if fibre_raw is None else float(fibre_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"ingredient {code}: fibre_100 not a number") from exc
        if fibre_100 is not None and fibre_100 < 0:
            raise ConfigError(f"ingredient {code}: fibre_100 must be >= 0")
        unit_raw = entry.get("unit")
        unit: Unit | None = None
        if unit_raw is not None:
            if not isinstance(unit_raw, Mapping) or not str(unit_raw.get("name") or "").strip():
                raise ConfigError(f"ingredient {code}: unit needs a name and g")
            try:
                unit_g = float(unit_raw["g"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigError(f"ingredient {code}: unit g missing or not a number") from exc
            if unit_g <= 0:
                raise ConfigError(f"ingredient {code}: unit g must be > 0")
            unit = Unit(str(unit_raw["name"]).strip(), unit_g)
        out.append(
            YamlIngredient(
                code=code,
                name=name,
                cofid_code=None if cofid is None else str(cofid).strip(),
                pack_g=pack_g,
                pack_label=str(entry.get("pack_label") or f"{num(pack_g)} g pack"),
                pack_sizes_g=pack_sizes_g,
                aisle=aisle,
                pantry=_bool(entry.get("pantry"), f"ingredient {code}: pantry"),
                use_whole_pack=_bool(
                    entry.get("use_whole_pack"), f"ingredient {code}: use_whole_pack"
                ),
                aliases=aliases,
                person_scope=scope,
                macros_100=_macros_override(entry.get("macros_100"), code),
                fibre_100=fibre_100,
                unit=unit,
            )
        )
    return CatalogueYaml(aisles=aisles, ingredients=tuple(out))


def _opt_float(s: str) -> float | None:
    s = s.strip()
    return None if s == "" else float(s)


def load_cofid(path: Path) -> dict[str, CofidRow]:
    """food_code -> row from tools/cofid_to_csv.py output ('' means N/unknown -> None)."""
    out: dict[str, CofidRow] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            row = CofidRow(
                code=r["food_code"],
                name=r["food_name"],
                kcal=_opt_float(r["kcal"]),
                protein_g=_opt_float(r["protein_g"]),
                fat_g=_opt_float(r["fat_g"]),
                carb_g=_opt_float(r["carb_g"]),
                fibre_g=_opt_float(r["fibre_aoac_g"]),
            )
            out.setdefault(row.code, row)
    return out


def _nutrients(y: YamlIngredient, cofid: Mapping[str, CofidRow]) -> dict[str, float | None] | None:
    """Per-100 g values for a YAML entry: CoFID first (its N/blank fibre may be filled by the
    entry's `fibre_100`), the `macros_100` label override otherwise."""
    c = cofid.get(y.cofid_code) if y.cofid_code is not None else None
    if c is not None and None not in (c.kcal, c.protein_g, c.fat_g, c.carb_g):
        return {
            "kcal_100": c.kcal,
            "protein_100": c.protein_g,
            "fat_100": c.fat_g,
            "carb_100": c.carb_g,
            "fibre_100": c.fibre_g if c.fibre_g is not None else y.fibre_100,
        }
    if y.macros_100 is not None:
        m = y.macros_100
        return {
            "kcal_100": m["kcal"],
            "protein_100": m["protein_g"],
            "fat_100": m["fat_g"],
            "carb_100": m["carb_g"],
            "fibre_100": m["fibre_g"],
        }
    return None


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return str(a) == str(b)


# ---------------------------------------------------------------- build
def build(db: Db, yaml_path: Path, csv_path: Path, clock: Clock | None = None) -> BuildResult:
    """Insert a new `ingredients` version per changed code; aliases once.

    An ingredient whose fibre is unknown is stored unapproved (fibre NULL -> approved 0).
    """
    clk = clock or db.clock
    cat = load_yaml(yaml_path)
    cofid = load_cofid(csv_path)
    result = BuildResult()
    with db.tx():
        for y in cat.ingredients:
            nutrients = _nutrients(y, cofid)
            if nutrients is None:
                result.missing_cofid.append(y.code)
                continue
            approved = 1 if nutrients["fibre_100"] is not None else 0
            if not approved:
                result.fibre_null.append(y.code)
            new: dict[str, Any] = {
                "name": y.name,
                "cofid_code": y.cofid_code if y.cofid_code in cofid else None,
                **nutrients,
                "pack_g": y.pack_g,
                "pack_label": y.pack_label,
                "pack_sizes_g": json.dumps(y.pack_sizes_g),
                "aisle": y.aisle,
                "pantry": int(y.pantry),
                "use_whole_pack": int(y.use_whole_pack),
                "approved": approved,
                "person_scope": y.person_scope,
            }
            latest = db.latest("ingredients", code=y.code)
            if latest is not None and all(_same(latest[k], v) for k, v in new.items()):
                result.unchanged.append(y.code)
            else:
                version = 1 if latest is None else int(latest["version"]) + 1
                db.insert(
                    "ingredients",
                    code=y.code,
                    version=version,
                    created_at=clk.utc_stamp(),
                    **new,
                )
                result.inserted.append(y.code)
            for alias in y.aliases:
                exists = db.one(
                    "SELECT 1 FROM ingredient_aliases WHERE ingredient_code = ? AND alias = ?",
                    (y.code, alias),
                )
                if exists is None:
                    db.insert(
                        "ingredient_aliases",
                        ingredient_code=y.code,
                        alias=alias,
                        source="yaml",
                        created_at=clk.utc_stamp(),
                    )
                    result.aliases_added += 1
            if y.unit is not None:
                have = db.one(
                    "SELECT unit_name, unit_g FROM ingredient_units WHERE code = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (y.code,),
                )
                if (
                    have is None
                    or str(have["unit_name"]) != y.unit.name
                    or float(have["unit_g"]) != y.unit.g
                ):
                    db.insert(
                        "ingredient_units",
                        code=y.code,
                        unit_name=y.unit.name,
                        unit_g=y.unit.g,
                        created_at=clk.utc_stamp(),
                    )
                    result.units_set.append(y.code)
    return result


def units(db: Db) -> dict[str, Unit]:
    """code -> how the food is served whole, for every food that is (`ingredient_units`)."""
    return {
        str(r["code"]): Unit(str(r["unit_name"]), float(r["unit_g"]))
        for r in db.all(
            "SELECT u.code, u.unit_name, u.unit_g FROM ingredient_units u "
            "WHERE u.id = (SELECT MAX(q.id) FROM ingredient_units q WHERE q.code = u.code)"
        )
    }


# ---------------------------------------------------------------- a food added by hand
#: energy from the macros: 4 kcal a gram of protein and of carbohydrate, 9 of fat. A label that
#: disagrees with its own numbers by more than this is usually a per-portion figure typed into a
#: per-100 g box, which is the typo that quietly rewrites a week of menus.
ENERGY_TOLERANCE = 0.25
#: a pack lighter than this is far more often "1,000" read as 1 than a pinch of saffron: stored,
#: and said, before a list built on it asks for 300 packs of a 1 kg bag
SMALL_PACK_G = 5


class FoodRejected(ValueError):
    """A food the catalogue will not take, with the reason a person can act on."""


def _slug(name: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in name.strip().lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:60]


def check_food(
    *,
    name: str,
    kcal_100: float,
    protein_100: float,
    fat_100: float,
    carb_100: float,
    fibre_100: float | None,
    pack_g: float,
    aisle: str,
) -> str:
    """Raise `FoodRejected` for what cannot be true; return a warning for what is merely odd.

    The line between them matters. A food whose macros weigh more than 100 g per 100 g, or that
    carries more energy than pure fat, is not a food with unusual numbers - it is a typo, and
    accepting it puts a wrong number into every menu built from then on. A food whose stated
    kcal disagrees with its own macros is usually the same typo and occasionally just a label
    (alcohol carries energy no macro row accounts for), so that one is said out loud and stored.
    """
    if not name.strip():
        raise FoodRejected("A food needs a name.")
    if not _slug(name):
        raise FoodRejected("That name has no letters or numbers in it.")
    numbers = {
        "kcal": kcal_100,
        "protein": protein_100,
        "fat": fat_100,
        "carbohydrate": carb_100,
        "fibre": fibre_100 if fibre_100 is not None else 0.0,
        "pack size": pack_g,
    }
    for label, value in numbers.items():
        if not math.isfinite(value):
            raise FoodRejected(f"{label.capitalize()} must be a finite number.")
        if value < 0:
            raise FoodRejected(f"{label.capitalize()} cannot be negative.")
    if kcal_100 > 900:
        raise FoodRejected("Nothing carries more than 900 kcal per 100 g; pure fat is 900.")
    if pack_g <= 0:
        raise FoodRejected("A pack has to weigh something.")
    if pack_g > 20000:
        raise FoodRejected("That pack is over 20 kg - check the units.")
    solids = protein_100 + fat_100 + carb_100
    if solids > 102:
        raise FoodRejected(
            f"Protein, fat and carbohydrate come to {solids:.0f} g in 100 g of food."
        )
    if aisle not in DEFAULT_AISLES:
        raise FoodRejected(f"Aisle has to be one of: {', '.join(DEFAULT_AISLES)}.")
    small = (
        f"A {pack_g:g} g pack is very small - check it is in grams. Stored as typed."
        if pack_g < SMALL_PACK_G
        else ""
    )
    from_macros = 4 * protein_100 + 9 * fat_100 + 4 * carb_100
    biggest = max(kcal_100, from_macros)
    if biggest and abs(from_macros - kcal_100) > ENERGY_TOLERANCE * biggest:
        return (
            f"The macros work out at about {from_macros:.0f} kcal per 100 g, not {kcal_100:.0f}. "
            "Stored as typed - worth a second look at the label." + (f" {small}" if small else "")
        )
    return small


def add_food(
    db: Db,
    *,
    name: str,
    kcal_100: float,
    protein_100: float,
    fat_100: float,
    carb_100: float,
    fibre_100: float | None,
    pack_g: float,
    pack_label: str,
    aisle: str,
    person_scope: str = "both",
    pantry: bool = False,
    use_whole_pack: bool = False,
    code: str | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Add an approved food, or append a corrected version with the same code.

    Codes derived from names stay separate from codes in the bundled catalogue. Recipes keep
    their original ingredient versions, so correcting a food does not change past calculations.
    """
    clk = clock or db.clock
    warning = check_food(
        name=name,
        kcal_100=kcal_100,
        protein_100=protein_100,
        fat_100=fat_100,
        carb_100=carb_100,
        fibre_100=fibre_100,
        pack_g=pack_g,
        aisle=aisle,
    )
    if person_scope not in ("both", "owner", "partner"):
        raise FoodRejected("A food is for both of you or for one of you.")
    # the catalogue's own codes came from the YAML and are not the display name slugified
    # ("whey", not "whey_protein_powder"), so re-versioning one of THOSE has to name it. A food
    # added from the page passes none and gets the slug, which is what makes adding the same
    # name twice a correction rather than a second food.
    code = _slug(code) if code else _slug(name)
    latest = db.latest("ingredients", code=code)
    version = 1 if latest is None else int(latest["version"]) + 1
    db.insert(
        "ingredients",
        code=code,
        version=version,
        name=name.strip(),
        cofid_code=None,
        kcal_100=float(kcal_100),
        protein_100=float(protein_100),
        fat_100=float(fat_100),
        carb_100=float(carb_100),
        # fibre is what `build` withholds approval over, because the fibre floor is a real
        # target and a NULL silently reads as zero against it. Typed by hand it is a number
        # somebody chose, including zero, so it approves.
        fibre_100=None if fibre_100 is None else float(fibre_100),
        pack_g=float(pack_g),
        pack_label=pack_label.strip() or f"{pack_g:.0f} g",
        aisle=aisle,
        pantry=int(bool(pantry)),
        use_whole_pack=int(bool(use_whole_pack)),
        approved=1,
        person_scope=person_scope,
        created_at=clk.utc_stamp(),
    )
    return {"code": code, "version": version, "name": name.strip(), "warning": warning}


# ---------------------------------------------------------------- reading
def _aliases(db: Db) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in db.all("SELECT ingredient_code, alias FROM ingredient_aliases ORDER BY id"):
        out.setdefault(str(r["ingredient_code"]), []).append(str(r["alias"]))
    return out


def latest_versions(db: Db) -> list[Ingredient]:
    """Every code's latest version, ordered by code, approved or not."""
    aliases = _aliases(db)
    return [Ingredient.from_row(r, aliases.get(str(r["code"]), ())) for r in db.all(LATEST_SQL)]


def by_id(db: Db, ingredient_id: int) -> Ingredient:
    row = db.one("SELECT * FROM ingredients WHERE id = ?", (ingredient_id,))
    if row is None:
        raise LookupError(f"no ingredient with id {ingredient_id}")
    return Ingredient.from_row(row, _aliases(db).get(str(row["code"]), ()))


def refused_terms(db: Db, person_id: int) -> list[str]:
    """The latest profile's refused_foods list, normalised; malformed JSON counts as empty."""
    return _profile_list(db, person_id, "refused_foods")


def refused_codes(db: Db, person_id: int) -> set[str]:
    """Codes whose latest ingredient_preferences row for the person is 'refused'."""
    status: dict[str, str] = {}
    for r in db.all(
        "SELECT ingredient_code, status FROM ingredient_preferences WHERE person_id = ? "
        "ORDER BY id",
        (person_id,),
    ):
        status[str(r["ingredient_code"])] = str(r["status"])
    return {code for code, s in status.items() if s == "refused"}


def _singular(word: str) -> str:
    """Crude English de-pluralisation, per word: enough to make an avoid-list entry typed in
    the singular meet a catalogue name written in the plural. 'ss' is never a plural ending
    ('cress'), and a word of three letters or fewer is left alone ('peas' -> 'pea' is wanted,
    'gas' -> 'ga' is not).

    '-oes' earns its own line because the foods that take it are the ones people actually
    refuse: without it 'tomatoes' de-pluralised to 'tomatoe', which matched neither 'Tomato
    ketchup' nor 'Tomato puree', so both stayed on the approved list of someone who had just
    said they do not eat tomatoes. Same for potatoes, mangoes and avocadoes. The length floor
    keeps 'toes' -> 'toe' on the plain '-s' path.
    """
    if len(word) <= MIN_PLURAL_LEN or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes") and len(word) > MIN_OES_LEN:
        return word[:-2]
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def _depluralise(text: str) -> str:
    return " ".join(_singular(w) for w in text.split())


@dataclass(frozen=True)
class _Kind:
    """A word for a whole kind of food, not one food: matched by aisle and by whole words in a
    food's name, so a food added by hand later is caught the same way."""

    aisles: frozenset[str] = frozenset()
    words: tuple[str, ...] = ()
    #: whole words that keep a food out of the kind however it is shelved ("Egg whites" sit in
    #: the dairy aisle and are not dairy; "Turkey mince" is not red meat)
    never: tuple[str, ...] = ()


_FISH = (
    "tuna",
    "salmon",
    "cod",
    "mackerel",
    "sardine",
    "haddock",
    "trout",
    "anchovy",
    "hake",
    "pollock",
    "sea bass",
    "basa",
    "kipper",
    "fish",
)
_SHELLFISH = (
    "prawn",
    "shrimp",
    "crab",
    "lobster",
    "mussel",
    "scallop",
    "squid",
    "clam",
    "oyster",
    "langoustine",
)
_RED = (
    "beef",
    "pork",
    "lamb",
    "bacon",
    "ham",
    "sausage",
    "steak",
    "mince",
    "chorizo",
    "venison",
    "salami",
    "pepperoni",
    "prosciutto",
    "pancetta",
    "veal",
    "gammon",
    "goat",
)
_POULTRY = ("chicken", "turkey", "duck", "goose")
_NOT_MEAT = ("quorn", "vegetarian", "vegan", "plant", "soya", "meat free")
_CHEESE = (
    "cheese",
    "cheddar",
    "mozzarella",
    "feta",
    "halloumi",
    "parmesan",
    "ricotta",
    "paneer",
    "gouda",
    "brie",
    "stilton",
    "mascarpone",
    "edam",
    "emmental",
)
_DAIRY = (
    *_CHEESE,
    "milk",
    "yoghurt",
    "yogurt",
    "skyr",
    "butter",
    "cream",
    "creme fraiche",
    "kefir",
    "quark",
    "whey",
)
_PULSES = (
    "lentil",
    "chickpea",
    "kidney bean",
    "baked bean",
    "butter bean",
    "black bean",
    "cannellini",
    "borlotti",
    "pinto",
    "edamame",
    "houmous",
    "hummus",
    "falafel",
    "dal",
)
#: no beans" in the same breath as "focus on cooked warm vegetables" - and the plain word, as a
#: whole word in a name, kept green beans off too, one of the vegetables she was asking for
_BEANS = (
    "kidney bean",
    "baked bean",
    "butter bean",
    "black bean",
    "cannellini",
    "borlotti",
    "pinto",
    "haricot",
    "edamame",
    "soya bean",
    "soybean",
)
_NUTS = (
    "almond",
    "peanut",
    "walnut",
    "cashew",
    "hazelnut",
    "pecan",
    "pistachio",
    "macadamia",
    "brazil nut",
    "pine nut",
    "nut",
)

#: and then matched nothing: no food in the catalogue is called "fish", so salmon, cod and tuna
#: stayed on the list the model picks from and the refusal had no effect at all.
KINDS: dict[str, _Kind] = {
    "fish": _Kind(frozenset({"fish"}), _FISH, _SHELLFISH),
    "seafood": _Kind(frozenset({"fish"}), (*_FISH, *_SHELLFISH)),
    "shellfish": _Kind(words=_SHELLFISH),
    "meat": _Kind(frozenset({"meat"}), (*_RED, *_POULTRY), _NOT_MEAT),
    "red meat": _Kind(words=_RED, never=(*_POULTRY, *_NOT_MEAT, *_FISH)),
    "poultry": _Kind(words=_POULTRY, never=_NOT_MEAT),
    "pork": _Kind(
        words=(
            "pork",
            "bacon",
            "ham",
            "chorizo",
            "salami",
            "pepperoni",
            "prosciutto",
            "pancetta",
            "gammon",
            "pork sausage",
        ),
        never=_NOT_MEAT,
    ),
    "dairy": _Kind(
        words=_DAIRY,
        never=("egg", "coconut", "almond", "oat", "soya", "soy", "peanut", "cocoa", "vegan"),
    ),
    "cheese": _Kind(words=_CHEESE, never=("vegan",)),
    "pulse": _Kind(words=_PULSES, never=("green bean", "coffee")),
    "legume": _Kind(words=_PULSES, never=("green bean", "coffee")),
    "bean": _Kind(words=_BEANS, never=("green bean", "runner bean", "coffee", "vanilla", "jelly")),
    "nut": _Kind(words=_NUTS, never=("coconut", "nutmeg")),
}
#: foods made from another food, by the words in their names: somebody refusing chickpeas may
#: still eat houmous, so it is asked once, not assumed.
#: A kind reaches these through the foods it is made of (`made_of`): red pesto is cashews, pine
#: nuts and a hard cheese, and its name says none of them, so "allergic to nuts" and "no dairy"
MADE_OF: dict[str, tuple[str, ...]] = {
    "chickpea": ("houmous", "hummus", "falafel"),
    "egg": ("mayonnaise",),
    "soya": ("tofu", "soy sauce", "edamame"),
    "soy": ("tofu", "soy sauce", "edamame"),
    "cashew": ("pesto",),
    "pine nut": ("pesto",),
    "cheese": ("pesto",),
}
#: one word naming two foods, and the one a person refusing the word does not mean: "no peppers"
#: is the vegetable, not the black pepper in every recipe; "no milk" is not the coconut milk
_NOT_MEANT: dict[str, tuple[str, ...]] = {
    "pepper": ("black pepper", "white pepper", "cayenne pepper", "peppercorn"),
    "milk": ("coconut milk", "oat milk", "almond milk", "soya milk"),
    "butter": ("peanut butter", "almond butter", "cocoa butter"),
}
#: the same, for a food kept at home, where "pepper" is the other one: "salt, pepper" is the jar
#: the word as said: "peppers", the vegetable's own alias, is still the vegetable at home
_NOT_MEANT_AT_HOME: dict[str, tuple[str, ...]] = {
    **_NOT_MEANT,
    "pepper": ("red pepper", "green pepper", "yellow pepper", "bell pepper", "sweet pepper"),
}


def _has_word(name: str, word: str) -> bool:
    return f" {_depluralise(word)} " in f" {name} "


def made_of(term: str, ing: Ingredient) -> bool:
    """Whether `ing` is made from the food a refused term names (houmous from chickpeas) without
    the term itself matching it - the foods worth one question rather than a guess. A kind
    ("nuts", "dairy") is made of its own foods, so pesto is asked about through the cashews and
    the cheese in it."""
    key = _depluralise(normalise(term))
    kind = KINDS.get(key)
    sources = (key, *kind.words) if kind is not None else (key,)
    words = [w for s in sources for w in MADE_OF.get(s, ())]
    if not words or refused_matches(normalise(term), ing):
        return False
    name = _depluralise(normalise(ing.name))
    if kind is not None and any(_has_word(name, w) for w in kind.never):
        return False  # "vegan pesto" is not made from cheese
    return any(_has_word(name, w) for w in words)


def kind_of(term: str) -> _Kind | None:
    """The kind of food a refused term names, or None when it names a food or nothing."""
    return KINDS.get(_depluralise(normalise(term)))


def refused_matches(term: str, ing: Ingredient, *, at_home: bool = False) -> bool:
    """A normalised free-text term hits an ingredient by name, by alias, or as whole words in the
    name ('mushrooms' hits 'Mushrooms, raw'; 'mush' hits nothing) - or, for a word naming a kind
    of food ('fish', 'dairy', 'red meat'), by the aisle it is shelved in and the words in its name.

    Singular and plural are the same term. 'Chickpea' on the avoid list did not match
    'Chickpeas, tinned, drained' - not by name, not by alias, not as a whole word - so the
    avoid list was silently ignored and the refused food came back in a menu. Whole-word
    matching still holds after de-pluralising, so 'mush' and 'pea' still hit nothing.

    `at_home` reads the term as an entry on the list of foods kept at home, where a word naming
    two foods means the other one: "pepper" there is the black pepper, not the red peppers.
    """
    if not term:
        return False
    said, term = term, _depluralise(term)
    name = _depluralise(normalise(ing.name))
    kind = KINDS.get(term)
    if kind is not None:
        if any(_has_word(name, w) for w in kind.never):
            return False
        if ing.aisle in kind.aisles or any(_has_word(name, w) for w in kind.words):
            return True
    not_meant = _NOT_MEANT_AT_HOME if at_home and said in _NOT_MEANT_AT_HOME else _NOT_MEANT
    if any(_has_word(name, other) for other in not_meant.get(term, ())):
        return False
    if term == name or any(term == _depluralise(normalise(a)) for a in ing.aliases):
        return True
    return f" {term} " in f" {name} "


def _refused_among(db: Db, person_id: int, codes: set[str]) -> list[str]:
    """Of these ingredient codes, the ones the person has since refused, by plain name, sorted.

    The shared tail of `refused_names_on` and `refused_names_in_recipe`: same terms, same
    matching, so a menu's warning and a single dish's warning can never name the food two
    different ways.
    """
    if not codes:
        return []
    terms = refused_terms(db, person_id)
    # a food refused by itself (the page's "Never again" chip) counts the same as a word
    by_code = refused_codes(db, person_id)
    if not terms and not by_code:
        return []
    served = [
        i
        for i in latest_versions(db)
        if i.code in codes and (i.code in by_code or any(refused_matches(t, i) for t in terms))
    ]
    return sorted({i.name.split(",")[0] for i in served})


def refused_names_on(db: Db, person_id: int, menu_ids: list[int]) -> list[str]:
    """The foods on these menus the person has since refused, by plain name, sorted.

    A menu built before a refusal still holds the food - correctly, the refusal governs the next
    build - and saying so is the difference between a stale menu and a broken promise. One
    function, so the bot's warning and the page's cannot name different foods.
    """
    if not menu_ids:
        return []
    marks = ",".join("?" for _ in menu_ids)
    codes = {
        str(r["code"])
        for r in db.all(
            "SELECT DISTINCT i.code FROM menu_days md "
            "JOIN meal_slots ms ON ms.menu_day_id = md.id "
            "JOIN recipe_ingredients ri ON ri.recipe_id = ms.recipe_id "
            f"JOIN ingredients i ON i.id = ri.ingredient_id WHERE md.menu_id IN ({marks})",
            tuple(menu_ids),
        )
    }
    return _refused_among(db, person_id, codes)


def refused_names_in_recipe(db: Db, person_id: int, recipe_id: int) -> list[str]:
    """The foods in this ONE recipe the person has since refused, by plain name, sorted.

    message a tap earlier - so its warning has to be scoped to what is actually IN this dish, not
    to "something on the menu somewhere": checking the whole menu here would flag an innocent
    Tuesday card because Monday's dish elsewhere on the same menu holds the refused food.
    """
    codes = {
        str(r["code"])
        for r in db.all(
            "SELECT DISTINCT i.code FROM recipe_ingredients ri "
            "JOIN ingredients i ON i.id = ri.ingredient_id WHERE ri.recipe_id = ?",
            (recipe_id,),
        )
    }
    return _refused_among(db, person_id, codes)


def fold_term(text: str) -> str:
    """A normalised, singularised key for one food word - "chickpea" and "Chickpeas" are the
    same entry to a person's OWN avoid/likes/pantry list, the same way they are the same entry
    against the catalogue (`refused_matches`). Without this, asking to avoid chickpeas a fourth
    time (already on the list, just spelled "chickpea") added "chickpeas" as a second, separate
    entry rather than recognising the one already there.
    """
    return _depluralise(normalise(text))


#: how a person says "not this" to a menu. Deliberately only the forms that mean a FOOD is out,
#: not "less salt" or "lighter dinners" - those are shape, and the model handles shape. Read
#: after `_contracted` has spelled "don't" as "do not", so "not" covers every negation.
_AVOID = re.compile(
    r"\b(?:no more|no|not|never|avoid|without|skip|drop|hold|exclude|leave out|minus|hate|"
    r"dislike|detest|can not stand|cannot stand|allergic to|intolerant to|sick of|fed up with|"
    r"had enough of|stop|remove|get rid of|take out|cut out)\b[:\s]+(.+)",
    re.I,
)
#: a word that ends the list of foods being refused. Looked for only once the words before the
#: food have gone (`_lead_off`): "I don't want ANY MORE lentils" is lentils, not "want". The
#: place a food is put is not the food: "stop putting mushrooms IN MY FOOD" or "ON EVERYTHING"
#: is mushrooms - but only a place food goes, never a food's own name ("tuna in brine") and
#: never a time ("rice in the evenings" is `for_now`, and was kept off for good at lunch too).
#: And only where the sentence ends with it, at a comma or before another stop word: "no
#: chicken in my meals DURING LENT" threw the time away and kept the chicken off for good, so
#: what follows stays on the food and the sentence is not read whole (`prefs.half_read`)
_ENDS = (
    r"please|pls|thanks|thank you|this week|again|instead|but|and more|more|anymore|"
    r"any more|for now|for a while|at all|ever|for me|for us"
)
_STOP = re.compile(
    rf"\b(?:{_ENDS}|"
    r"(?:in|on|into|onto) (?:(?:my|our|the|every|any|all|each) ){0,2}(?:everything|anything|"
    r"things|stuff|food|foods|meals?|dish|dishes|salads?|cooking|recipes?|menus?)"
    rf"(?=\s*(?:$|[,.;:!?-]|(?:{_ENDS})\b)))\b",
    re.I,
)
#: when a refusal for good begins, not a time it is limited to: "FROM TOMORROW, no more
#: read, so "no more lentils FROM TOMORROW" is the lentils; only a time, so "no chicken from
#: this shop" stays whole
_FROM_NOW = re.compile(
    r"\b(?:from|starting|as of) (?:now on|now|today|tomorrow|(?:this|next) (?:week|weekend|"
    r"month|year|time|morning|afternoon|evening|(?:mon|tues|wednes|thurs|fri|satur|sun)day)|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day)\b",
    re.I,
)
#: how much of a food is at home: "PLENTY", "TONS", "A BIG BAG", "TWO TINS"
_STOCK = (
    r"(?:plenty|loads|lots|tons|tonnes|heaps|masses|stacks|piles|enough|some|a lot|"
    r"(?:a|one|two|three|a few|a couple of|several) (?:\w+ )?(?:bags?|sacks?|packs?|packets?|"
    r"box(?:es)?|tins?|cans?|jars?|bottles?|kilos?|loads?|stash|supply))"
)
#: a refusal for a while, for one time of day, meal or day, or about the shopping, is not one
#: for good: "no salmon this week", "the chicken and rice on Thursday", "no rice in the
#: evenings", "not at lunch", "for the time being", "we don't need rice", "no rice, we have
#: are the shopping only in a shopping sentence: "I need to avoid it" is a refusal. Having it
#: at home is said in more words than "plenty": "we have TONS", "A BIG BAG", "we're well
#: STOCKED", "plenty AT HOME" - but never "I have IBS", which is a reason to keep it off.
_FOR_NOW = re.compile(
    r"^\s*(?:please\s+)?(?:(?:we|i)\s+)?(?:do not|don't|dont)\s+(?:need|get|buy|order)\b|"
    r"\b(?:(?:this|next) (?:week|weekend|month|time|morning|evening|afternoon)|"
    r"(?:at|on|over) the weekend|at the moment|right now|today|tonight|tomorrow|until|till|"
    r"for (?:now|a while|a bit|the time being|the moment|a few \w+|a couple of \w+|"
    r"the next \w+)|(?:mon|tues|wednes|thurs|fri|satur|sun)days?|week ?days?|weekends|"
    r"(?:in|during) the (?:mornings?|afternoons?|evenings?|week|day)|at night|"
    r"(?:at|in|with|for) (?:my |our |the )?(?:breakfasts?|lunch|lunches|lunchtime|dinners?|"
    r"suppers?)|no need|need to (?:buy|get|order)|(?:still|already) (?:got|have)|"
    rf"(?:we|i)(?:'?ve| have)?(?: got)? {_STOCK}|there(?:'s| is| are) {_STOCK}|got {_STOCK}|"
    rf"{_STOCK} (?:at home|left|spare)|stocked|in stock|"
    r"in the (?:cupboard|fridge|freezer|larder)s?)\b",
    re.I,
)
#: an allergy or an intolerance is never for now, whatever else the sentence says
_ALLERGY = re.compile(r"\b(?:allergic|allergy|intolerant|intolerance)\b", re.I)
#: what can stand between the avoid word and the food: an article, the avoid word repeated per
#: item ("no beef, no pork"), or the verb of the sentence ("I do not WANT lentils", "I don't LIKE
#: chickpeas", "stop GIVING ME tuna"). Stripped repeatedly, so "also no more the" all go. Never
#: "so much" or "too much": "not so much rice" is less rice, which is shape, not a refusal.
#: Longest first where one starts another: "a fan of" before "a", or "not a fan of salmon" was
#: left as "fan of salmon" and refused nothing.
_LEADING = re.compile(
    r"^(?:no more|no|not|never|avoid|without|skip|drop|hold|exclude|minus|the|a fan of|a|an|"
    r"any|all|also|really|ever|again|want|wanna|like|eat|eating|have|having|get|getting|"
    r"give me|giving me|giving|put|putting|serve|serving|use|using|include|including|need|"
    r"keen on|fond of|into|to|me|more|some|cook|cooking|with)(?:\s+|$)",
    re.I,
)
#: the refusal said after the foods: "lentils, never again", "mushrooms - no more", and the
#: please or thanks after it ("chickpeas, no more thanks")
_AFTER = re.compile(
    r"^(.+?)[\s,.;:!-]+(?:never again|no more|not any more|not anymore)"
    r"(?:[\s,.;:!-]+(?:please|pls|thanks|thank you))?[\s.!]*$",
    re.I,
)
#: what makes several foods one list: "lentils, chickpeas OR beans"
#: ...and a repeated avoid word with no comma before it is a list too: "No chickpeas no lentils",
#: typed on a phone, was one food named "chickpeas no lentils", which matches nothing (6 Oct)
_JOINED = r"(?i) or | and | nor | no more | no | never |/|&|\+"
#: a comma piece that starts a sentence of its own ends the list: "no more lentils, THEY were
#: awful" is the lentils, and the reason is not a food
_CLAUSE = re.compile(
    r"^(?:they|it|its|i|im|we|you|that|this|those|these|he|she|because|cos|as|so)\b", re.I
)


def _contracted(note: str) -> str:
    """Expand common contractions before matching refusal phrases."""
    text = (note or "").replace(chr(0x2019), "'").replace("`", "'")  # a phone's curly apostrophe
    text = re.sub(
        r"\b(can|won|don|doesn|didn|isn|aren|wouldn|shouldn)'?t\b", r"\1 not", text, flags=re.I
    )
    return re.sub(r"\bcannot\b", "can not", text, flags=re.I)


def _lead_off(text: str) -> str:
    """The text without what stands before the food: "want any more lentils" -> "lentils"."""
    while True:
        # "no beef, no pork" repeats the avoid word, "skip the liver" leads with an article and
        # "I do not want lentils" with its verb: all of it goes, however many there are
        shorter = _LEADING.sub("", text).strip()
        if shorter == text:
            return text
        text = shorter


def _refused_pieces(tail: str) -> tuple[list[str], list[str]]:
    """What follows the avoid word, cut into the comma-separated pieces it refuses - and the
    piece said beside them that may be the food asked for, not refused.

    "No chicken, salmon instead please" refused the salmon too: every comma piece was read as
    a food to keep out, and the one asked FOR went on the list for good. The piece
    directly before "instead" is the food wanted, so it is left out. A single
    piece after the first, bare of any avoid word, alone or joined by "or", then "please" or
    "thanks" - "no chicken, salmon thanks", "no chicken, salmon or tuna please" - may be the
    same thing said without "instead", so every food in it is set aside: not written, and
    named for the person to keep off with a tap. Anything else is a list, commas alone
    included ("no rice, chickpeas, lentils"), and "and" or "nor" make one too: "no mushrooms,
    olives and anchovies please" is three foods, and setting the last two aside kept only the
    mushrooms off. Every food in a list is refused.
    """
    tail = _lead_off(tail.strip())
    stop = _STOP.search(tail)
    said = tail[: stop.start()] if stop is not None else tail
    pieces = [p for p in re.split(r"[,;]", said) if p.strip()]
    clause = next((n for n, p in enumerate(pieces[1:], 1) if _CLAUSE.match(p.strip())), None)
    if clause is not None:
        return pieces[:clause], []
    word = stop.group(0).casefold() if stop is not None else ""
    if word == "instead" and len(pieces) > 1:
        return pieces[:-1], []
    if word in ("please", "pls", "thanks", "thank you") and len(pieces) == 2:
        last = pieces[1].strip()
        if not _AVOID.match(last) and re.search(r"\b(?:and|nor)\b|&|\+", last, re.I) is None:
            return pieces[:1], [last]
    return pieces, []


def _terms_in(pieces: list[str]) -> list[str]:
    """Each food the pieces name, cleaned and once: "rice or chickpeas" -> [rice, chickpeas]."""
    out: list[str] = []
    for piece in pieces:
        for part in re.split(_JOINED, piece):
            cleaned = _lead_off(normalise(part))
            if len(cleaned) >= 3 and cleaned not in out:
                out.append(cleaned)
    return out


def for_now(note: str) -> bool:
    """Whether a refusal is for a while, a time of day, a meal or a day, or is a remark about
    the shopping (`_FOR_NOW`): not one to keep for good. "From tomorrow" is when one for good
    begins, and an allergy is never for now."""
    text = (note or "").replace(chr(0x2019), "'").replace("`", "'")
    if _ALLERGY.search(text):
        return False
    return _FOR_NOW.search(_FROM_NOW.sub(" ", text)) is not None


def _read_refusal(note: str) -> tuple[list[str], list[str]]:
    """(the foods a sentence refuses, the foods it sets aside): `avoid_terms`, `set_aside`.
    Nothing, when the refusal is `for_now`: the bot and a cook note both write what this reads
    #27). "From tomorrow" says when, and is no part of a food: "no more lentils from tomorrow"
    was read as "lentils from tomorrow", which names nothing."""
    if for_now(note):
        return [], []
    text = _FROM_NOW.sub(" ", _contracted(note))
    m = _AVOID.search(text)
    pieces, aside = _refused_pieces(m.group(1)) if m is not None else ([], [])
    if not pieces:
        # the foods first and the refusal after them: "lentils, never again"
        after = _AFTER.match(text)
        pieces, aside = _refused_pieces(after.group(1)) if after is not None else ([], [])
    terms = _terms_in(pieces)
    return terms, [t for t in _terms_in(aside) if t not in terms]


def set_aside(note: str) -> list[str]:
    """The foods said beside a refusal and not taken as refused: "salmon" in "No chicken,
    salmon please", which may be the food asked for in the chicken's place. Never written by
    the parser; a surface names them and asks (`telegram/foodflow.py`)."""
    return _read_refusal(note)[1]


def avoid_terms(note: str) -> list[str]:
    """Extract foods a cook note asks to exclude."""
    return _read_refusal(note)[0]


def _profile_list(db: Db, person_id: int, column: str) -> list[str]:
    """One of the profile's free-text food lists, normalised; malformed JSON counts as empty."""
    row = db.one(
        # `column` is a literal in every caller below, never anything a person typed. The newest
        # row: one booked for a later day carries every list change too (targets.change_profile)
        f"SELECT {column} FROM profiles WHERE person_id = ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (person_id,),
    )
    if row is None or row[column] is None:
        return []
    try:
        raw = json.loads(str(row[column]))
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [normalise(str(t)) for t in raw if str(t).strip()]


def liked_terms(db: Db, person_id: int) -> list[str]:
    """The latest profile's liked_foods list, normalised.

    Written by the interview, by `/set likes` ("The next menu will use it.") and by the model's
    food-preferences card ("Put {foods} on the menu more often?") - and read by nothing, until
    this. Three surfaces promised a use the menu path never made: `menu_propose` filled its
    favourites slot from ratings alone, so on day one, before anybody has rated a dish, the one
    preference channel a new person has was write-only.
    """
    return _profile_list(db, person_id, "liked_foods")


def pantry_codes(db: Db, person_id: int) -> set[str]:
    """Return foods this person has marked as always at home.

    The list is opt-in and empty by default. Shopping still accounts for quantities
    and credits from packs already bought."""
    terms = _profile_list(db, person_id, "pantry_json")
    if not terms:
        return set()
    return {
        i.code
        for i in latest_versions(db)
        if any(refused_matches(t, i, at_home=True) for t in terms)
    }


def approved(db: Db, person_id: int) -> list[Ingredient]:
    """The person's approved ingredients, ordered by code; position + 1 is the prompt index."""
    slug = db.scalar("SELECT slug FROM persons WHERE id = ?", (person_id,))
    if slug is None:
        raise LookupError(f"no person with id {person_id}")
    codes_out = refused_codes(db, person_id)
    terms = refused_terms(db, person_id)
    out: list[Ingredient] = []
    for ing in latest_versions(db):
        if not ing.approved or ing.person_scope not in ("both", str(slug)):
            continue
        if ing.code in codes_out or any(refused_matches(t, ing) for t in terms):
            continue
        out.append(ing)
    return out


def shuffled(items: Iterable[Ingredient], person_id: int, on: dt.date) -> list[Ingredient]:
    """The person's list in an order of its own for this ISO week: the same order every time it
    is asked for in the week, so the prompt's numbers and the check of the answer agree, and a
    different one next week. A model leans to what is listed first (Hou 2023); code order put
    the same foods first every week."""
    year, week, _ = on.isocalendar()
    seed = hashlib.sha256(f"{person_id}:{year}-W{week:02d}".encode()).digest()[:8]
    out = list(items)
    random.Random(int.from_bytes(seed, "big")).shuffle(out)
    return out


def prompt_lines(items: Iterable[Ingredient]) -> list[str]:
    return [ing.prompt_line(i) for i, ing in enumerate(items, start=1)]
