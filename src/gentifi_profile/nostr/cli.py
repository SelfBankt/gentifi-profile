"""gentifi-nostr: publish businesses to Nostr.

    gentifi-nostr init                                   # add a master key to .env (once)
    gentifi-nostr relay                                  # a test relay on ws://127.0.0.1:7777
    gentifi-nostr events gtf_hollowbrookfarm --profiles examples          # dry run: print signed events
    gentifi-nostr publish gtf_hollowbrookfarm --profiles examples --relay ws://127.0.0.1:7777
    gentifi-nostr show gtf_hollowbrookfarm --relay ws://127.0.0.1:7777  # what the relay holds
    gentifi-nostr key gtf_hollowbrookfarm               # the business's public key (npub)

Only relays on this machine are allowed unless you pass --allow-public: events sent
to a public relay are copied by others and can't be fully taken back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ..relay.cli import db_path, load_env
from ..store import Directory
from .client import RelayError, check_relays, fetch
from .events import npub
from .keys import KeyStore, NostrKeyError, add_master_key, master_key_from_env
from .localrelay import LocalRelay
from .mapping import KIND_DELETION, KIND_LISTING, KIND_METADATA, KIND_PRODUCT, KIND_STALL, LABEL
from .publisher import Publisher

KIND_NAMES = {KIND_METADATA: "profile", KIND_STALL: "stall", KIND_PRODUCT: "product", KIND_LISTING: "listing",
              KIND_DELETION: "deletion"}


def keystore() -> KeyStore:
    load_env()
    return KeyStore(db_path(), master_key_from_env())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gentifi-nostr", description="Publish Gentifi businesses to Nostr.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="add a Nostr master key to .env (keys are encrypted with it)")
    relay = sub.add_parser("relay", help="run a test relay on this machine (in memory)")
    relay.add_argument("--port", type=int, default=7777)
    key = sub.add_parser("key", help="show a business's Nostr public key, making it if needed")
    key.add_argument("business_id")
    for name, text in [("events", "print the signed events without sending them"),
                       ("publish", "sign and send a business's events to relays")]:
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("business_id", nargs="?", help="omit with --all")
        cmd.add_argument("--all", action="store_true", help="every business in --profiles")
        cmd.add_argument("--profiles", type=Path, default=Path(os.environ.get("GENTIFI_PROFILES", "profiles")))
        cmd.add_argument("--paused", action="store_true", help="as if paused: delete the listings instead")
        if name == "publish":
            cmd.add_argument("--relay", action="append", default=[], help="relay address; repeat for more")
            cmd.add_argument("--allow-public", action="store_true", help="allow relays not on this machine")
    show = sub.add_parser("show", help="list what a relay holds for a business")
    show.add_argument("business_id")
    show.add_argument("--relay", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            env = load_env() or Path.cwd() / ".env"
            if add_master_key(env):
                print(f"Added a Nostr master key to {env}. Keep a copy somewhere safe: "
                      "without it, Gentifi can't sign for businesses again.")
            else:
                print(f"{env} already has a Nostr master key; nothing changed.")
        elif args.command == "relay":
            print(f"Test relay on ws://127.0.0.1:{args.port} (in memory; Ctrl+C to stop)", file=sys.stderr)
            try:
                asyncio.run(LocalRelay().serve(args.port))
            except KeyboardInterrupt:
                pass
        elif args.command == "key":
            pubkey = keystore().signer(args.business_id).pubkey
            print(f"{args.business_id}\n  npub   {npub(pubkey)}\n  hex    {pubkey}")
        elif args.command in {"events", "publish"}:
            relays = check_relays(args.relay, args.allow_public) if args.command == "publish" else []
            directory = Directory.from_folder(args.profiles)
            ids = list(directory.profiles) if args.all else [args.business_id]
            if not args.all and not args.business_id:
                parser.error("give a business id or --all")
            publisher = Publisher(directory, keystore(), relays)
            for business_id in ids:
                if args.paused:
                    directory.paused.add(business_id)
                if args.command == "events":
                    for event in publisher.events(business_id):
                        print(json.dumps(event, ensure_ascii=False))
                    continue
                for relay_url, answers in publisher.publish(business_id).items():
                    refused = [m for ok, m in answers.values() if not ok]
                    print(f"{business_id} -> {relay_url}: {len(answers) - len(refused)} of {len(answers)} events accepted"
                          + (f"; refused: {refused[0]}" if refused else ""))
                    if refused:
                        return 1
        elif args.command == "show":
            pubkey = keystore().pubkey(args.business_id)
            if pubkey is None:
                print(f"{args.business_id} has no Nostr key yet, so nothing is published.")
                return 1
            events = fetch(args.relay, [{"authors": [pubkey], "#L": [LABEL]}])
            print(f"{args.business_id} ({npub(pubkey)}) on {args.relay}: {len(events)} events")
            for event in sorted(events, key=lambda e: (e["kind"], e["tags"][0][1] if e["tags"] else "")):
                print("  " + _describe(event))
        return 0
    except (NostrKeyError, RelayError, KeyError, ValueError, OSError, TimeoutError) as exc:
        print(f"gentifi-nostr: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1


def _describe(event: dict) -> str:
    tags = {t[0]: t[1:] for t in event["tags"] if t}
    kind = KIND_NAMES.get(event["kind"], f"kind {event['kind']}")
    if event["kind"] == KIND_METADATA:
        return f"{kind:9} {json.loads(event['content']).get('name')}"
    if event["kind"] == KIND_DELETION:
        count = sum(1 for t in event["tags"] if t[0] == "a")
        return f"{kind:9} {count} listings: {event['content']}"
    d = tags.get("d", [""])[0]
    if event["kind"] == KIND_PRODUCT:
        c = json.loads(event["content"])
        qty = "not tracked" if c.get("quantity") is None else c["quantity"]
        return f"{kind:9} {d:16} {c['price']} {c['currency']}, quantity {qty}"
    if event["kind"] == KIND_LISTING:
        summary = tags.get("summary", [""])[0]
        return f"{kind:9} {d:16} {summary}, {tags.get('status', ['?'])[0]}"
    return f"{kind:9} {d}"


if __name__ == "__main__":
    sys.exit(main())
