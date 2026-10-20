"""Send events to Nostr relays and read them back (NIP-01 over websockets).

Anything sent to a public relay is copied by others and can't be fully taken back,
so only relays on this machine are allowed unless allow_public is set.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class RelayError(Exception):
    pass


def check_relays(urls: list[str], allow_public: bool = False) -> list[str]:
    if not urls:
        raise RelayError("no relay given: pass --relay ws://127.0.0.1:7777 (see `gentifi-nostr relay`)")
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname:
            raise RelayError(f"{url} isn't a relay address (ws:// or wss://)")
        if parts.hostname not in LOCAL_HOSTS and not allow_public:
            raise RelayError(f"{url} is a public relay: events sent there are copied by others and can't be "
                             "fully taken back. Pass --allow-public if you mean it.")
    return urls


def publish(events: list[dict[str, Any]], relays: list[str], timeout: float = 10) -> dict[str, dict[str, tuple[bool, str]]]:
    """Send events to each relay. Returns {relay: {event id: (accepted, message)}}."""
    async def all_relays():
        results = await asyncio.gather(*(_publish(url, events, timeout) for url in relays))
        return dict(zip(relays, results))
    return asyncio.run(all_relays())


def fetch(relay: str, filters: list[dict[str, Any]], timeout: float = 10) -> list[dict[str, Any]]:
    """Events matching the filters, as the relay holds them now."""
    return asyncio.run(_fetch(relay, filters, timeout))


async def _publish(url: str, events: list[dict[str, Any]], timeout: float) -> dict[str, tuple[bool, str]]:
    from websockets.asyncio.client import connect

    answers = {e["id"]: (False, "no answer from the relay") for e in events}
    waiting = set(answers)
    try:
        async with asyncio.timeout(timeout):
            async with connect(url, open_timeout=timeout) as ws:
                for event in events:
                    await ws.send(json.dumps(["EVENT", event], ensure_ascii=False))
                while waiting:
                    message = json.loads(await ws.recv())
                    if message[:1] == ["OK"] and message[1] in waiting:
                        answers[message[1]] = (bool(message[2]), message[3] if len(message) > 3 else "")
                        waiting.discard(message[1])
    except TimeoutError:
        pass  # events the relay didn't answer keep "no answer from the relay"
    except Exception as exc:  # OSError, or websockets' own connection errors
        answers = {i: (False, f"couldn't reach the relay: {exc}") for i in answers}
    return answers


async def _fetch(url: str, filters: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    from websockets.asyncio.client import connect

    events = []
    async with asyncio.timeout(timeout):
        async with connect(url, open_timeout=timeout) as ws:
            await ws.send(json.dumps(["REQ", "gentifi", *filters]))
            while True:
                message = json.loads(await ws.recv())
                if message[:2] == ["EVENT", "gentifi"]:
                    events.append(message[2])
                elif message[:2] in (["EOSE", "gentifi"], ["CLOSED", "gentifi"]):
                    await ws.send(json.dumps(["CLOSE", "gentifi"]))
                    return events
