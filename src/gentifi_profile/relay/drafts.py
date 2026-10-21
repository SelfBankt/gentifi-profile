"""Read an owner's photo of their prices with the toolkit's `gentifi draft`.

When an owner sends the bot a photo of a price board or menu, it's saved under
<drafts folder>/<business>/<time>/ and the toolkit (github.com/SelfBankt/gentifi-toolkit,
`gentifi draft`) turns it into catalogue.csv and notes.md there, for Gentifi to check
before anything changes: the AI can misread, and nothing it drafts goes live by itself.

Settings (the gentifi-bot service sets them on the VPS):
    GENTIFI_TOOLKIT       the toolkit's folder (default /srv/gentifi/toolkit)
    GENTIFI_AI_MODEL      a model that reads images, such as kimi-k3 on OpenCode Go
    GENTIFI_AI_KEY_FILE   the AI service's key (on the VPS, /etc/gentifi-bot/ai-key)
    GENTIFI_AI_URL        the AI service (default: the toolkit's, OpenCode Go)
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
PASSED_ON = ("GENTIFI_AI_MODEL", "GENTIFI_AI_KEY_FILE", "GENTIFI_AI_URL")  # never the bot's own secrets


@dataclass
class Draft:
    folder: Path
    rows: list[dict[str, str]] = field(default_factory=list)  # catalogue.csv: name, price, price_from, unit, check...
    error: str = ""

    @property
    def to_check(self) -> list[dict[str, str]]:
        return [r for r in self.rows if r.get("check")]


class Drafter:
    def __init__(self, toolkit: Path, folder: Path, env: dict[str, str],
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: int = 300):
        self.toolkit, self.folder, self.env, self.run, self.timeout = toolkit, folder, env, run, timeout

    @classmethod
    def from_env(cls, folder: Path, environ: dict[str, str] | None = None) -> Drafter | None:
        """A drafter if the toolkit and a model are set up here; None means photos are only passed on."""
        environ = dict(os.environ if environ is None else environ)
        toolkit = Path(environ.get("GENTIFI_TOOLKIT", "/srv/gentifi/toolkit"))
        node = shutil.which("node", path=environ.get("PATH"))
        if not node or not (toolkit / "bin" / "gentifi.js").is_file() or not environ.get("GENTIFI_AI_MODEL"):
            return None
        env = {"PATH": environ.get("PATH", "/usr/bin:/bin"), "HOME": str(folder),
               **{k: environ[k] for k in PASSED_ON if environ.get(k)}}
        return cls(toolkit, folder, env)

    def draft(self, business_id: str, photo: bytes, mime: str = "image/jpeg") -> Draft:
        out = self.folder / business_id / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"photo{SUFFIX.get(mime, '.jpg')}"
        path.write_bytes(photo)
        try:
            done = self.run(["node", str(self.toolkit / "bin" / "gentifi.js"), "draft", "--photo", str(path), "--out", str(out)],
                            env=self.env, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Draft(out, error=f"couldn't run gentifi draft: {exc}")
        if done.returncode != 0:
            return Draft(out, error=(done.stderr or done.stdout).strip()[-500:] or f"gentifi draft exited {done.returncode}")
        try:
            with (out / "catalogue.csv").open(newline="", encoding="utf-8") as fh:
                return Draft(out, rows=list(csv.DictReader(fh)))
        except OSError as exc:
            return Draft(out, error=f"no catalogue.csv: {exc}")
