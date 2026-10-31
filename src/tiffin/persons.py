"""Person records, Telegram identities and one-time pairing codes.

The owner's chat identity comes from configuration. A partner is paired on first use of a code;
unrecognised senders receive no reply.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as _secrets
import sqlite3
from dataclasses import dataclass

from .clock import DEFAULT_CLOCK, Clock, stamp
from .config import Config
from .db import Db

logger = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
SLUGS = ("owner", "partner")


@dataclass(frozen=True)
class Person:
    id: int
    slug: str
    display_name: str
    tone: str
    chat_id: int | None
    tailscale_login: str | None
    erased: bool

    @property
    def is_admin(self) -> bool:
        return self.slug == "owner"


def normalise_code(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def code_hash(code: str) -> str:
    return hashlib.sha256(normalise_code(code).encode("ascii")).hexdigest()


class PersonRegistry:
    def __init__(self, db: Db, config: Config, *, clock: Clock = DEFAULT_CLOCK):
        self.db = db
        self.config = config
        self.clock = clock

    # -- bootstrap ---------------------------------------------------------------
    def ensure_from_config(self) -> None:
        """Create person rows and the configured owner's Telegram identity once."""
        for slug in SLUGS:
            if self.db.one("SELECT id FROM persons WHERE slug = ?", (slug,)) is None:
                # Leave an unknown name blank; persons rows are append-only.
                name = str(self.config.get(f"persons.{slug}.display_name", "") or "")
                self.db.insert("persons", slug=slug, display_name=name, tone="neutral")
        owner_id = self._id_of("owner")
        chat_id = self.config.person_chat_id("owner")
        if (
            chat_id
            and self.db.one("SELECT id FROM telegram_identities WHERE person_id = ?", (owner_id,))
            is None
        ):
            self.db.insert(
                "telegram_identities", person_id=owner_id, chat_id=chat_id, method="config"
            )

    def _id_of(self, slug: str) -> int:
        row = self.db.one("SELECT id FROM persons WHERE slug = ?", (slug,))
        if row is None:
            raise LookupError(f"no person with slug {slug}")
        return int(row["id"])

    # -- lookups -----------------------------------------------------------------
    def _login_of(self, person_id: int) -> str | None:
        row = self.db.one(
            "SELECT t.login FROM tailscale_identities t WHERE t.person_id = ? "
            "AND t.id = (SELECT MAX(q.id) FROM tailscale_identities q "
            "WHERE q.login = t.login COLLATE NOCASE) ORDER BY t.id DESC LIMIT 1",
            (person_id,),
        )
        return None if row is None else str(row["login"])

    def _name_of(self, person_id: int, fallback: str) -> str:
        row = self.db.one(
            "SELECT name FROM person_names WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (person_id,),
        )
        return fallback if row is None else str(row["name"])

    def set_display_name(self, slug: str, name: str, *, set_by: str = "operator") -> str:
        """Append a display name; the latest name takes effect."""
        cleaned = " ".join(name.split())[:60]
        if not cleaned:
            raise ValueError("a display name cannot be blank")
        if any(ch in cleaned for ch in "<>&"):
            raise ValueError("a display name cannot contain < > or &")
        person_id = self._id_of(slug)
        self.db.insert("person_names", person_id=person_id, name=cleaned, set_by=set_by)
        return cleaned

    def _row_to_person(self, row: sqlite3.Row) -> Person:
        ident = self.db.one(
            "SELECT chat_id FROM telegram_identities WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (row["id"],),
        )
        prof = self.db.one(
            "SELECT tone FROM profiles WHERE person_id = ? "
            "ORDER BY effective_from DESC, id DESC LIMIT 1",
            (row["id"],),
        )
        legacy_login = row["tailscale_login"]
        if legacy_login:
            binding = self.db.one(
                "SELECT person_id FROM tailscale_identities WHERE login = ? COLLATE NOCASE "
                "ORDER BY id DESC LIMIT 1",
                (legacy_login,),
            )
            if binding is not None and binding["person_id"] != row["id"]:
                legacy_login = None
        return Person(
            id=int(row["id"]),
            slug=str(row["slug"]),
            display_name=self._name_of(int(row["id"]), str(row["display_name"])),
            tone=str(prof["tone"] if prof is not None else row["tone"]),
            chat_id=None if ident is None else int(ident["chat_id"]),
            tailscale_login=self._login_of(int(row["id"])) or legacy_login,
            erased=row["erased_at"] is not None,
        )

    def by_id(self, person_id: int) -> Person | None:
        row = self.db.one("SELECT * FROM persons WHERE id = ?", (person_id,))
        return None if row is None else self._row_to_person(row)

    def by_slug(self, slug: str) -> Person | None:
        row = self.db.one("SELECT * FROM persons WHERE slug = ?", (slug,))
        return None if row is None else self._row_to_person(row)

    def by_chat_id(self, chat_id: int) -> Person | None:
        row = self.db.one(
            "SELECT p.* FROM telegram_identities t JOIN persons p ON p.id = t.person_id "
            "WHERE t.chat_id = ? AND p.erased_at IS NULL",
            (chat_id,),
        )
        return None if row is None else self._row_to_person(row)

    def by_tailscale_login(self, login: str) -> Person | None:
        """The latest attachment wins, including when that person has been erased."""
        binding = self.db.one(
            "SELECT person_id FROM tailscale_identities WHERE login = ? COLLATE NOCASE "
            "ORDER BY id DESC LIMIT 1",
            (login,),
        )
        if binding is not None:
            person = self.by_id(int(binding["person_id"]))
            return person if person is not None and not person.erased else None
        row = self.db.one(
            "SELECT * FROM persons WHERE tailscale_login = ? COLLATE NOCASE AND erased_at IS NULL",
            (login,),
        )
        return None if row is None else self._row_to_person(row)

    def attach_tailscale_login(self, slug: str, login: str) -> int:
        """Bind a Tailscale login to a person. Append-only: a rebind is a new row."""
        cleaned = login.strip()
        if not cleaned or "@" not in cleaned:
            raise ValueError(f"not a Tailscale login: {login!r}")
        return self.db.insert("tailscale_identities", person_id=self._id_of(slug), login=cleaned)

    def active(self) -> list[Person]:
        rows = self.db.all("SELECT * FROM persons WHERE erased_at IS NULL ORDER BY id")
        return [self._row_to_person(r) for r in rows]

    def require(self, slug: str) -> Person:
        p = self.by_slug(slug)
        if p is None:
            raise LookupError(f"no person with slug {slug}")
        return p

    def admin(self) -> Person:
        p = self.by_slug("owner")
        if p is None:
            raise LookupError("admin person 'owner' missing")
        return p

    # -- pairing -----------------------------------------------------------------
    def create_pairing_code(self, slug: str, *, ttl_hours: int = 48) -> str:
        """Returns the plain code once; only its SHA-256 is stored."""
        code = "".join(_secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires = self.clock.now_utc().replace(microsecond=0)
        expires = expires.__class__.fromtimestamp(
            expires.timestamp() + ttl_hours * 3600, tz=expires.tzinfo
        )
        self.db.insert(
            "pairing_codes",
            person_id=self._id_of(slug),
            code_sha256=code_hash(code),
            expires_at=stamp(expires),
        )
        return f"{code[:4]}-{code[4:]}"

    def try_pair(self, chat_id: int, text: str) -> Person | None:
        """Bind chat_id to the person whose valid, unused code matches `text`. None otherwise."""
        cleaned = normalise_code(text)
        if len(cleaned) != 8:
            return None
        row = self.db.one(
            "SELECT c.id, c.person_id FROM pairing_codes c "
            "LEFT JOIN pairing_uses u ON u.pairing_code_id = c.id "
            "WHERE c.code_sha256 = ? AND u.id IS NULL AND c.expires_at > ?",
            (code_hash(cleaned), self.clock.utc_stamp()),
        )
        if row is None:
            return None
        try:
            with self.db.tx():
                self.db.insert(
                    "telegram_identities",
                    person_id=row["person_id"],
                    chat_id=chat_id,
                    method="pairing_code",
                )
                self.db.insert(
                    "pairing_uses",
                    pairing_code_id=row["id"],
                    chat_id=chat_id,
                    used_at=self.clock.utc_stamp(),
                )
        except sqlite3.IntegrityError:
            logger.warning("pairing refused", extra={"event": "pair_refused"})
            return None
        return self.by_id(int(row["person_id"]))
