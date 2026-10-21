"""Sign each business's events and send them to relays, again whenever its listing changes."""

from __future__ import annotations

import copy
import queue
import sys
import threading
import time
from contextlib import nullcontext
from typing import Any, Callable

from ..store import Directory
from .client import publish
from .events import finish
from .keys import KeyStore
from .mapping import profile_events


class Publisher:
    def __init__(self, directory: Directory, keys: KeyStore, relays: list[str], lock: Any = None,
                 clock: Callable[[], float] = time.time):
        self.directory, self.keys, self.relays = directory, keys, relays
        self.lock = lock or nullcontext()  # the RequestBook lock, so we read a whole profile at once
        self.clock = clock
        self._last: dict[str, int] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()

    def events(self, business_id: str) -> list[dict[str, Any]]:
        """Signed events for the business as it is now. Each call gets a later created_at
        than the last, so relays replace the previous version (and a resume outdates a pause).
        A business being onboarded has nothing published, or, if it was published before,
        its listings taken down until it goes live again."""
        with self.lock:
            profile = copy.deepcopy(self.directory.get(business_id))
            paused = business_id in self.directory.paused
            draft = business_id in self.directory.drafts
        if draft and self.keys.pubkey(business_id) is None:
            return []  # never published, and no key made until it goes live
        signer = self.keys.signer(business_id)
        created_at = max(int(self.clock()), self._last.get(business_id, 0) + 1)
        self._last[business_id] = created_at
        reason = f"{profile['business']['name']} is updating its listing and isn't taking orders right now." \
            if draft else None
        return [finish(e, signer) for e in profile_events(profile, created_at, paused or draft, signer.pubkey, reason)]

    def publish(self, business_id: str) -> dict[str, dict[str, tuple[bool, str]]]:
        events = self.events(business_id)
        return publish(events, self.relays) if events else {}

    # ------------------------------------------------------------ background

    def start(self) -> None:
        """Publish every business now and again after each change, from a background thread."""
        self.directory.listeners.append(self._queue.put)
        for business_id in self.directory.profiles:
            self._queue.put(business_id)
        threading.Thread(target=self._run, name="gentifi-nostr", daemon=True).start()

    def stop(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        while (business_id := self._queue.get()) is not None:
            try:
                results = self.publish(business_id)
            except Exception as exc:  # keep publishing the others
                print(f"gentifi-nostr: couldn't publish {business_id}: {exc}", file=sys.stderr)
                continue
            for relay, answers in results.items():
                refused = [m for ok, m in answers.values() if not ok]
                note = f"{len(refused)} refused ({refused[0]})" if refused else "all accepted"
                print(f"gentifi-nostr: {business_id} -> {relay}: {len(answers)} events, {note}", file=sys.stderr)
