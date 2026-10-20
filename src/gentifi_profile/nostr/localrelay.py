"""A small in-memory Nostr relay for testing on this machine (`gentifi-nostr relay`).

Not for production: it keeps events in memory and listens on 127.0.0.1 only. It
checks signatures, keeps the newest replaceable (kind 0) and addressable
(30000-39999) events, and honours NIP-09 deletion requests from the same author.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from .events import address, verify


class LocalRelay:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.deleted: dict[str, int] = {}  # address or event id -> deletion created_at
        self.lock = threading.Lock()

    # ------------------------------------------------------------ storage

    def accept(self, event: dict[str, Any]) -> tuple[bool, str]:
        if not verify(event):
            return False, "invalid: bad id or signature"
        with self.lock:
            if event["id"] in self.events:
                return True, "duplicate: already have this event"
            key = self._key(event)
            if self.deleted.get(event["id"]) is not None or (key and self.deleted.get(key, -1) >= event["created_at"]):
                return False, "blocked: this event was deleted"
            if event["kind"] == 5:
                self._delete(event)
            elif key:
                current = next((e for e in self.events.values() if self._key(e) == key), None)
                if current and current["created_at"] >= event["created_at"]:
                    return True, "duplicate: have a newer version"
                if current:
                    del self.events[current["id"]]
            self.events[event["id"]] = event
            return True, ""

    @staticmethod
    def _key(event: dict[str, Any]) -> str | None:
        kind = event["kind"]
        if kind == 0 or 10000 <= kind < 20000:
            return f"{kind}:{event['pubkey']}:"
        if 30000 <= kind < 40000:
            return address(event)
        return None

    def _delete(self, deletion: dict[str, Any]) -> None:
        for tag in deletion["tags"]:
            if len(tag) < 2 or tag[0] not in {"a", "e"}:
                continue
            if tag[0] == "a" and tag[1].split(":")[1:2] != [deletion["pubkey"]]:
                continue  # only the author can delete
            self.deleted[tag[1]] = deletion["created_at"]
            for event in list(self.events.values()):
                same = event["id"] == tag[1] if tag[0] == "e" else self._key(event) == tag[1]
                if same and event["pubkey"] == deletion["pubkey"] and event["created_at"] <= deletion["created_at"]:
                    del self.events[event["id"]]

    def query(self, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self.lock:
            found = [e for e in self.events.values() if any(_matches(e, f) for f in filters)]
        found.sort(key=lambda e: e["created_at"], reverse=True)
        limits = [f["limit"] for f in filters if "limit" in f]
        return found[:max(limits)] if limits else found

    # ------------------------------------------------------------ websocket

    async def _serve_client(self, ws) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
                verb = message[0]
            except (ValueError, IndexError, TypeError):
                await ws.send(json.dumps(["NOTICE", "invalid message"]))
                continue
            if verb == "EVENT" and len(message) > 1:
                ok, note = self.accept(message[1]) if isinstance(message[1], dict) else (False, "invalid")
                await ws.send(json.dumps(["OK", message[1].get("id", "") if isinstance(message[1], dict) else "", ok, note]))
            elif verb == "REQ" and len(message) > 2:
                for event in self.query(message[2:]):
                    await ws.send(json.dumps(["EVENT", message[1], event], ensure_ascii=False))
                await ws.send(json.dumps(["EOSE", message[1]]))
            elif verb != "CLOSE":
                await ws.send(json.dumps(["NOTICE", f"unsupported: {verb}"]))

    async def serve(self, port: int = 7777, ready: threading.Event | None = None, bound: list | None = None) -> None:
        from websockets.asyncio.server import serve

        async with serve(self._serve_client, "127.0.0.1", port) as server:
            if bound is not None:
                bound.append(server.sockets[0].getsockname()[1])
            if ready:
                ready.set()
            await asyncio.get_running_loop().create_future()

    def start(self, port: int = 0) -> str:
        """Run in a background thread (for tests); returns the relay's ws:// address."""
        ready, bound = threading.Event(), []
        threading.Thread(target=lambda: asyncio.run(self.serve(port, ready, bound)), daemon=True).start()
        if not ready.wait(10):
            raise RuntimeError("local relay didn't start")
        return f"ws://127.0.0.1:{bound[0]}"


def _matches(event: dict[str, Any], f: dict[str, Any]) -> bool:
    if "ids" in f and event["id"] not in f["ids"]:
        return False
    if "authors" in f and event["pubkey"] not in f["authors"]:
        return False
    if "kinds" in f and event["kind"] not in f["kinds"]:
        return False
    if "since" in f and event["created_at"] < f["since"]:
        return False
    if "until" in f and event["created_at"] > f["until"]:
        return False
    for key, wanted in f.items():
        if key.startswith("#") and len(key) == 2:
            values = {t[1] for t in event["tags"] if len(t) > 1 and t[0] == key[1]}
            if not values & set(wanted):
                return False
    return True
