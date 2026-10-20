"""Nostr events (NIP-01): ids, Schnorr signatures (BIP-340) and npub encoding (NIP-19).

Signing uses coincurve (libsecp256k1); install with `pip install -e ".[nostr]"`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol


class Signer(Protocol):
    """Holds one business's key. A NIP-46 remote signer can implement the same two members."""

    @property
    def pubkey(self) -> str: ...

    def sign(self, event_id: str) -> str: ...


class LocalSigner:
    def __init__(self, secret: bytes):
        from coincurve import PrivateKey, PublicKeyXOnly

        self._key = PrivateKey(secret)
        self._pubkey = PublicKeyXOnly.from_secret(secret).format().hex()

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign(self, event_id: str) -> str:
        return self._key.sign_schnorr(bytes.fromhex(event_id)).hex()


def serialise(event: dict[str, Any]) -> str:
    return json.dumps([0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
                      separators=(",", ":"), ensure_ascii=False)


def event_id(event: dict[str, Any]) -> str:
    return hashlib.sha256(serialise(event).encode("utf-8")).hexdigest()


def finish(unsigned: dict[str, Any], signer: Signer) -> dict[str, Any]:
    """Add pubkey, id and sig to an event that has created_at, kind, tags and content."""
    event = {"pubkey": signer.pubkey, "created_at": unsigned["created_at"], "kind": unsigned["kind"],
             "tags": unsigned["tags"], "content": unsigned["content"]}
    event["id"] = event_id(event)
    event["sig"] = signer.sign(event["id"])
    return event


def verify(event: dict[str, Any]) -> bool:
    from coincurve import PublicKeyXOnly

    try:
        if event_id(event) != event["id"]:
            return False
        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(bytes.fromhex(event["sig"]),
                                                                      bytes.fromhex(event["id"]))
    except (KeyError, TypeError, ValueError):
        return False


def address(event: dict[str, Any]) -> str:
    """The `a` reference of an addressable event: kind:pubkey:d."""
    d = next((t[1] for t in event["tags"] if t[:1] == ["d"] and len(t) > 1), "")
    return f"{event['kind']}:{event['pubkey']}:{d}"


# ------------------------------------------------------------ NIP-19 (bech32)

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _to_5bit(data: bytes) -> list[int]:
    acc = bits = 0
    out = []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def npub(pubkey: str) -> str:
    data = _to_5bit(bytes.fromhex(pubkey))
    poly = _polymod(_expand("npub") + data + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return "npub1" + "".join(_CHARSET[d] for d in data + checksum)
