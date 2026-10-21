"""gentifi-relay: set up owner chats for the Telegram relay.

    gentifi-relay check                                   # is the bot token working?
    gentifi-relay invite gtf_hollowbrookfarm --lightning-address shop@strike.me
    gentifi-relay invite gtf_newshop --onboard            # hidden until the owner checks it and goes live
    gentifi-relay review gtf_newshop                      # after editing its profile: show the owner again
    gentifi-relay merchants                               # who is linked, live or not, and how they take payment
    gentifi-relay team                                    # link your own chat to hear about change requests

The relay itself runs inside the MCP server: gentifi-mcp --telegram ...
The bot token is read from TELEGRAM_BOT_TOKEN, or from a .env file in this
folder or any folder above it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .links import Links
from .payments import PaymentError, check_address
from .telegram import TelegramChannel, TelegramError

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


def telegram_channel() -> TelegramChannel:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise TelegramError("no TELEGRAM_BOT_TOKEN: put TELEGRAM_BOT_TOKEN=<token> in a .env file "
                            "in this folder (or a folder above it)")
    return TelegramChannel(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gentifi-relay", description="Set up owner chats for the Gentifi relay.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="check the Telegram bot token works")
    inv = sub.add_parser("invite", help="make a one-time link that connects an owner's Telegram to a business")
    inv.add_argument("business_id")
    inv.add_argument("--lightning-address", help="the business's own Lightning address, e.g. shop@strike.me")
    inv.add_argument("--payment-link", help="the business's own card payment link (SumUp, Square, Stripe...)")
    inv.add_argument("--onboard", action="store_true",
                     help="keep the business hidden until the owner checks the listing and taps Go live")
    rev = sub.add_parser("review", help="after editing a profile: the running bot reloads it and, while "
                                        "onboarding, shows it to the owner to check again")
    rev.add_argument("business_id")
    pay = sub.add_parser("payment", help="change how a business takes payment")
    pay.add_argument("business_id")
    pay.add_argument("--lightning-address", help="Lightning address ('' to remove)")
    pay.add_argument("--payment-link", help="payment link ('' to remove)")
    sub.add_parser("merchants", help="list businesses and their linked chats")
    sub.add_parser("team", help="make a one-time link that adds your Telegram to the Gentifi team: the bot "
                                "tells you when an owner asks for a change, connects or goes live")
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            print(f"Token works: the bot is @{telegram_channel().check()}")
            return 0
        links = Links(db_path())
        if args.command in {"invite", "payment"}:
            if args.lightning_address:
                check_address(args.lightning_address)
            links.configure(args.business_id, args.lightning_address, args.payment_link)
        if args.command in {"invite", "team"}:
            code = links.invite(args.business_id, onboarding=args.onboard) if args.command == "invite" \
                else links.team_invite()
            who = f"the owner of {args.business_id}" if args.command == "invite" else "yourself (or a teammate)"
            try:
                username = telegram_channel().check()
                print(f"Open this link as {who} (works once):\nhttps://t.me/{username}?start={code}")
            except TelegramError as exc:
                print(f"Invite code: {code}\n(Couldn't reach the bot to build the link: {exc}. "
                      f"Send '/start {code}' to the bot instead.)")
            if args.command == "team" and links.team():
                print("Already on the team: " + ", ".join(name or chat for chat, name in links.team()))
        elif args.command == "review":
            m = links.merchant(args.business_id)
            if m is None:
                print(f"gentifi-relay: {args.business_id} has no invite yet; make one with "
                      "`gentifi-relay invite`", file=sys.stderr)
                return 1
            links.request_review(args.business_id)
            print(f"The running bot will reload {args.business_id} within 30 seconds" +
                  (" and send the owner their listing to check." if m.onboarding and m.chat_id else "."))
        elif args.command == "payment":
            m = links.merchant(args.business_id)
            print(f"{args.business_id}: Lightning {m.lightning_address or '-'}, link {m.payment_link or '-'}")
        elif args.command == "merchants":
            for m in links.all():
                print(f"{m.business_id} ({'onboarding' if m.onboarding else 'live'}): "
                      f"{'linked to ' + (m.owner_name or m.chat_id) if m.chat_id else 'not linked'}; "
                      f"Lightning {m.lightning_address or '-'}; link {m.payment_link or '-'}")
                if m.change_request:
                    print(f"    asked for a change: {m.change_request}")
        return 0
    except (TelegramError, PaymentError) as exc:
        print(f"gentifi-relay: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
