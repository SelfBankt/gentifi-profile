"""Where secrets and local state come from: a .env file and the state database.

The database (GENTIFI_RELAY_DB, default ~/.gentifi/relay.db) holds each business's
encrypted Nostr key, and whatever an owner-confirmation plugin keeps beside them.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB = Path.home() / ".gentifi" / "relay.db"


def load_env(start: Path | None = None) -> Path | None:
    """Read KEY=value lines from the nearest .env (this folder, then parents). Existing
    environment variables win. Returns the file used, if any."""
    folder = (start or Path.cwd()).resolve()
    for d in [folder, *folder.parents]:
        path = d / ".env"
        if path.is_file():
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
            return path
    return None


def db_path() -> Path:
    path = Path(os.environ.get("GENTIFI_RELAY_DB", DEFAULT_DB)).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
