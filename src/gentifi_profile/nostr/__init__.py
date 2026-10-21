"""Publish businesses to Nostr: NIP-15 stall and products, NIP-99 listings, NIP-09 deletions.

Gentifi holds a key per business and signs for it, so the owner never sees Nostr
(docs/telegram-merchant-bot-spec.md). Needs the `nostr` extra:
`pip install -e ".[nostr]"`.
"""

from .client import RelayError, check_relays, fetch, publish
from .events import LocalSigner, Signer, event_id, finish, npub, verify
from .keys import KeyStore, NostrKeyError, add_master_key, master_key_from_env
from .localrelay import LocalRelay
from .mapping import profile_events
from .publisher import Publisher

__all__ = ["KeyStore", "LocalRelay", "LocalSigner", "NostrKeyError", "Publisher", "RelayError", "Signer",
           "add_master_key", "check_relays", "event_id", "fetch", "finish", "master_key_from_env", "npub",
           "profile_events", "publish", "verify"]
