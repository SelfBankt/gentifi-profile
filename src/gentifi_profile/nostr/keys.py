"""Each business's Nostr key, made by Gentifi and kept encrypted in SQLite.

The owner never sees Nostr (docs/telegram-merchant-bot-spec.md): Gentifi holds a
key per business and signs for it. Secrets are encrypted with AES-256-GCM under a
master key from GENTIFI_NOSTR_MASTER_KEY (64 hex characters, in the environment or
.env; `gentifi-nostr init` makes one). The database alone can't sign anything.
Signing sits behind the Signer interface, so a NIP-46 remote signer can replace
this later; a "claim your key" export for owners comes later too.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .events import LocalSigner

MASTER_KEY_ENV = "GENTIFI_NOSTR_MASTER_KEY"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nostr_keys (
    business_id  TEXT PRIMARY KEY,
    pubkey       TEXT NOT NULL UNIQUE,
    nonce        BLOB NOT NULL,
    secret       BLOB NOT NULL,
    created_at   TEXT NOT NULL
);
"""


class NostrKeyError(Exception):
    """A missing or wrong master key, or a key that can't be decrypted."""


def master_key_from_env() -> bytes:
    value = os.environ.get(MASTER_KEY_ENV, "").strip()
    if not value:
        raise NostrKeyError(f"no {MASTER_KEY_ENV}: run `gentifi-nostr init` to add one to your .env")
    try:
        key = bytes.fromhex(value)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise NostrKeyError(f"{MASTER_KEY_ENV} must be 64 hex characters")
    return key


def add_master_key(env_file: Path) -> bool:
    """Append a new master key to env_file unless it already has one. Never prints it."""
    if env_file.is_file() and any(line.strip().removeprefix("export ").strip().startswith(MASTER_KEY_ENV + "=")
                                  for line in env_file.read_text("utf-8").splitlines()):
        return False
    existing = env_file.read_text("utf-8") if env_file.is_file() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{sep}{MASTER_KEY_ENV}={secrets.token_hex(32)}\n")
    env_file.chmod(0o600)
    return True


class KeyStore:
    def __init__(self, path: Path | str, master_key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._aead = AESGCM(master_key)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def signer(self, business_id: str) -> LocalSigner:
        """The business's signer, making its key on first use."""
        from cryptography.exceptions import InvalidTag

        with self._lock, self._db:
            row = self._db.execute("SELECT nonce, secret FROM nostr_keys WHERE business_id = ?",
                                   (business_id,)).fetchone()
            if row:
                try:
                    return LocalSigner(self._aead.decrypt(row[0], row[1], business_id.encode()))
                except InvalidTag:
                    raise NostrKeyError(f"can't unlock the Nostr key for {business_id}: "
                                        f"wrong {MASTER_KEY_ENV}?") from None
            secret = secrets.token_bytes(32)
            signer = LocalSigner(secret)
            nonce = secrets.token_bytes(12)
            self._db.execute("INSERT INTO nostr_keys (business_id, pubkey, nonce, secret, created_at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (business_id, signer.pubkey, nonce, self._aead.encrypt(nonce, secret, business_id.encode()),
                              datetime.now(timezone.utc).isoformat(timespec="seconds")))
            return signer

    def pubkey(self, business_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT pubkey FROM nostr_keys WHERE business_id = ?", (business_id,)).fetchone()
        return row[0] if row else None
