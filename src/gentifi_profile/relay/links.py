"""Which owner chat belongs to which business, and how that business takes payment.

Kept in SQLite so links survive restarts. Requests themselves stay in the
reference server's memory (see store.py).
"""

from __future__ import annotations

import secrets
import sqlite3
import string
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

INVITE_ALPHABET = string.ascii_letters + string.digits

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    business_id       TEXT PRIMARY KEY,
    chat_id           TEXT UNIQUE,
    owner_name        TEXT,
    lightning_address TEXT,
    payment_link      TEXT,
    linked_at         TEXT,
    status            TEXT NOT NULL DEFAULT 'live',  -- 'onboarding' until the owner checks the listing
    review_requested  INTEGER NOT NULL DEFAULT 0,    -- Gentifi edited the profile: reload and show it again
    change_request    TEXT,                          -- the owner's last "something's wrong" message
    change_requested_at TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,                 -- '' for a team invite
    created_at  TEXT NOT NULL,
    used_at     TEXT,
    role        TEXT NOT NULL DEFAULT 'owner'  -- 'owner', or 'team' for Gentifi's own chats
);
CREATE TABLE IF NOT EXISTS team (
    chat_id     TEXT PRIMARY KEY,              -- told about change requests and go-lives
    name        TEXT,
    linked_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompts (
    reference   TEXT PRIMARY KEY,
    business_id TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    verify_url  TEXT,
    closed      INTEGER NOT NULL DEFAULT 0
);
"""


# Columns added after the first release, for databases made before them
MIGRATIONS = {
    ("merchants", "status"): "ALTER TABLE merchants ADD COLUMN status TEXT NOT NULL DEFAULT 'live'",
    ("merchants", "review_requested"): "ALTER TABLE merchants ADD COLUMN review_requested INTEGER NOT NULL DEFAULT 0",
    ("merchants", "change_request"): "ALTER TABLE merchants ADD COLUMN change_request TEXT",
    ("merchants", "change_requested_at"): "ALTER TABLE merchants ADD COLUMN change_requested_at TEXT",
    ("invites", "role"): "ALTER TABLE invites ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'",
}
MERCHANT_COLUMNS = "business_id, chat_id, owner_name, lightning_address, payment_link, status, change_request"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Merchant:
    business_id: str
    chat_id: str | None
    owner_name: str | None
    lightning_address: str | None
    payment_link: str | None
    status: str = "live"
    change_request: str | None = None

    @property
    def onboarding(self) -> bool:
        return self.status == "onboarding"


@dataclass
class Prompt:
    reference: str
    business_id: str
    chat_id: str
    message_id: str
    verify_url: str | None
    closed: bool


class Links:
    def __init__(self, path: Path | str = ":memory:"):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for (table, column), sql in MIGRATIONS.items():
                if column not in {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}:
                    self._db.execute(sql)
            self._db.commit()

    def _q(self, sql: str, *args: object) -> list[tuple]:
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ merchants

    def configure(self, business_id: str, lightning_address: str | None = None,
                  payment_link: str | None = None) -> None:
        self._q("INSERT OR IGNORE INTO merchants (business_id) VALUES (?)", business_id)
        if lightning_address is not None:
            self._q("UPDATE merchants SET lightning_address = ? WHERE business_id = ?",
                    lightning_address or None, business_id)
        if payment_link is not None:
            self._q("UPDATE merchants SET payment_link = ? WHERE business_id = ?",
                    payment_link or None, business_id)

    def merchant(self, business_id: str) -> Merchant | None:
        rows = self._q(f"SELECT {MERCHANT_COLUMNS} FROM merchants WHERE business_id = ?", business_id)
        return Merchant(*rows[0]) if rows else None

    def by_chat(self, chat_id: str) -> Merchant | None:
        rows = self._q(f"SELECT {MERCHANT_COLUMNS} FROM merchants WHERE chat_id = ?", chat_id)
        return Merchant(*rows[0]) if rows else None

    def all(self) -> list[Merchant]:
        return [Merchant(*r) for r in self._q(f"SELECT {MERCHANT_COLUMNS} FROM merchants ORDER BY business_id")]

    # ------------------------------------------------------------ onboarding

    def onboarding(self) -> list[str]:
        """Businesses whose owner hasn't checked the listing and gone live yet."""
        return [r[0] for r in self._q("SELECT business_id FROM merchants WHERE status = 'onboarding'")]

    def set_status(self, business_id: str, status: str) -> None:
        self.configure(business_id)
        self._q("UPDATE merchants SET status = ? WHERE business_id = ?", status, business_id)

    def request_review(self, business_id: str) -> None:
        """Ask the running bot to reload the profile and show it to the owner again. Clears the
        owner's change request, which the edit is meant to have dealt with."""
        self.configure(business_id)
        self._q("UPDATE merchants SET review_requested = 1, change_request = NULL WHERE business_id = ?", business_id)

    def take_review_requests(self) -> list[str]:
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT business_id FROM merchants WHERE review_requested = 1")]
            self._db.execute("UPDATE merchants SET review_requested = 0 WHERE review_requested = 1")
        return ids

    def save_change_request(self, business_id: str, text: str) -> None:
        self._q("UPDATE merchants SET change_request = ?, change_requested_at = ? WHERE business_id = ?",
                text, _now(), business_id)

    # ------------------------------------------------------------ invites

    @staticmethod
    def _code() -> str:
        # 12 letters and digits: fits Telegram's start parameter, and no _ or - for chat
        # apps to treat as formatting and drop when the link is shown or copied
        return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(12))

    def invite(self, business_id: str, onboarding: bool = False) -> str:
        """A one-time code the owner uses with /start to link their chat to the business.
        With onboarding, the business stays hidden until the owner checks it and goes live."""
        code = self._code()
        self.configure(business_id)
        if onboarding:
            self.set_status(business_id, "onboarding")
            self.request_review(business_id)  # a running bot loads the profile and hides it now
        self._q("INSERT INTO invites (code, business_id, created_at) VALUES (?, ?, ?)",
                code, business_id, _now())
        return code

    def redeem(self, code: str, chat_id: str, owner_name: str | None) -> str | None:
        """Link a chat using an unused invite code. Returns the business id, or None."""
        with self._lock, self._db:
            row = self._db.execute("SELECT business_id FROM invites WHERE code = ? AND used_at IS NULL "
                                   "AND role = 'owner'", (code,)).fetchone()
            if row is None:
                return None
            business_id = row[0]
            # One chat runs one business, and a business has one owner chat.
            self._db.execute("UPDATE merchants SET chat_id = NULL WHERE chat_id = ?", (chat_id,))
            self._db.execute("UPDATE merchants SET chat_id = ?, owner_name = ?, linked_at = ? "
                             "WHERE business_id = ?", (chat_id, owner_name, _now(), business_id))
            self._db.execute("UPDATE invites SET used_at = ? WHERE code = ?", (_now(), code))
            return business_id

    # ------------------------------------------------------------ Gentifi team

    def team_invite(self) -> str:
        """A one-time code that adds a chat to the Gentifi team (told about change requests)."""
        code = self._code()
        self._q("INSERT INTO invites (code, business_id, created_at, role) VALUES (?, '', ?, 'team')", code, _now())
        return code

    def redeem_team(self, code: str, chat_id: str, name: str | None) -> bool:
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM invites WHERE code = ? AND used_at IS NULL AND role = 'team'",
                                (code,)).fetchone() is None:
                return False
            self._db.execute("INSERT OR REPLACE INTO team (chat_id, name, linked_at) VALUES (?, ?, ?)",
                             (chat_id, name, _now()))
            self._db.execute("UPDATE invites SET used_at = ? WHERE code = ?", (_now(), code))
            return True

    def team(self) -> list[tuple[str, str | None]]:
        """(chat id, name) for each team chat."""
        return self._q("SELECT chat_id, name FROM team ORDER BY linked_at")

    # ------------------------------------------------------------ prompts

    def save_prompt(self, reference: str, business_id: str, chat_id: str, message_id: str) -> None:
        self._q("INSERT OR REPLACE INTO prompts (reference, business_id, chat_id, message_id) "
                "VALUES (?, ?, ?, ?)", reference, business_id, chat_id, message_id)

    def prompt(self, reference: str) -> Prompt | None:
        rows = self._q("SELECT reference, business_id, chat_id, message_id, verify_url, closed "
                       "FROM prompts WHERE reference = ?", reference)
        return Prompt(*rows[0][:5], bool(rows[0][5])) if rows else None

    def set_verify(self, reference: str, verify_url: str | None) -> None:
        self._q("UPDATE prompts SET verify_url = ? WHERE reference = ?", verify_url, reference)

    def close(self, reference: str) -> None:
        self._q("UPDATE prompts SET closed = 1, verify_url = NULL WHERE reference = ?", reference)

    def awaiting_verification(self) -> list[Prompt]:
        return [Prompt(*r[:5], bool(r[5])) for r in self._q(
            "SELECT reference, business_id, chat_id, message_id, verify_url, closed FROM prompts "
            "WHERE verify_url IS NOT NULL AND closed = 0")]

    def open_prompts(self, business_id: str) -> list[Prompt]:
        return [Prompt(*r[:5], bool(r[5])) for r in self._q(
            "SELECT reference, business_id, chat_id, message_id, verify_url, closed FROM prompts "
            "WHERE business_id = ? AND closed = 0 ORDER BY rowid", business_id)]
