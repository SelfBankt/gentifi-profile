"""Reference MCP server for Gentifi Business Profiles.

Serves every profile in a folder to any MCP-capable agent:

    gentifi-mcp --profiles examples                       # stdio (Claude Desktop, etc.)
    gentifi-mcp --profiles examples --transport streamable-http
    gentifi-mcp --profiles examples --demo --now 2026-10-15T20:00:00+01:00
    gentifi-mcp --profiles profiles --telegram --transport streamable-http
                                                          # owners answer on Telegram

--telegram runs the owner confirmation relay alongside the server: pending
requests go to the business owner's Telegram chat with one-tap buttons (see
gentifi-relay for linking owners). Run only one copy with --telegram, over HTTP, and
connect agents to it: Claude Desktop starts stdio servers more than once.
--nostr-relay publishes every business to Nostr (gentifi-nostr has the details). --demo adds a tool that lets you play the business owner, so pending requests
can be confirmed or declined. --now freezes the clock, so the example data stays
fresh while you try things.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .freshness import parse_time, utc_now
from .store import Directory, RequestBook

INSTRUCTIONS = """\
This server gives access to independent local businesses that publish a Gentifi
Business Profile. Typical flow: find_businesses -> search_catalogue ->
check_availability -> submit_request -> (later) submit_request with action
check_status.

Every availability record has an 'assessment.advice':
  book_instantly       - confirmed and current; a book/order request will be confirmed at once
  request_confirmation - send the request, but tell the user the business will confirm by 'confirm_by'
  warn_user            - the information may be out of date; say so before relying on it
  not_available        - full, sold out, out of season or already started
Always tell the user when availability was last confirmed (age_minutes), and
never tell them something is booked unless the receipt status is 'confirmed' or 'held'.
Never send hold, book or order on your own: show the customer exactly what will be
requested (items, time, price) and send it only after they agree, recording that in
customer_confirmation.
Give each request a unique request_id; retrying with the same id is safe and
returns the original receipt.
"""


def build_server(directory: Directory, demo: bool = False, book: RequestBook | None = None) -> MCPServer:
    book = book or RequestBook(directory)
    server = MCPServer(name="gentifi-reference", title="Gentifi reference server",
                       version="0.1.0", instructions=INSTRUCTIONS)

    @server.tool()
    def find_businesses(query: str = "", category: str | None = None, near_lat: float | None = None,
                        near_lng: float | None = None, max_km: float | None = None,
                        limit: int = 10) -> list[dict[str, Any]]:
        """Find local businesses by keywords (matched against names, descriptions and offerings),
        optionally by category, and optionally near a point. Returns business ids for the other tools."""
        near = {"lat": near_lat, "lng": near_lng} if near_lat is not None and near_lng is not None else None
        return directory.find(query, category, near, max_km, limit)

    @server.tool()
    def get_business_info(business_id: str) -> dict[str, Any]:
        """Full details of one business: location, hours, verification, policies
        (lead time, cancellation, delivery, payment) and the actions agents may take."""
        return directory.info(business_id)

    @server.tool()
    def search_catalogue(business_id: str, query: str = "") -> list[dict[str, Any]]:
        """What a business offers, with prices (in minor units, e.g. pence) and the actions
        available for each offering. Leave query empty to list everything."""
        return directory.catalogue(business_id, query)

    @server.tool()
    def check_availability(business_id: str, offering_id: str | None = None) -> list[dict[str, Any]]:
        """Current stock, bookable slots and recurring patterns, each with an assessment telling
        you whether to book straight away, ask the business to confirm, or warn the user."""
        return directory.availability(business_id, offering_id)

    @server.tool()
    def submit_request(request: dict[str, Any]) -> dict[str, Any]:
        """Send an ActionRequest (hold, book, order, cancel, check_status or enquire) and get a
        Receipt back. Only send hold, book or order after the customer has agreed to exactly this
        request, and say how in customer_confirmation {method: in_app or confirmation_page,
        confirmed_at, reference for a confirmation page}. Required: request_id (unique, used for
        safe retries), business_id, action, agent {name}, created_at (ISO 8601); items for
        hold/book/order/enquire; reference for cancel/check_status. Booking a slot needs items[].slot_id (from check_availability) and
        customer.party_size where relevant. Check get_business_info for required customer fields."""
        return book.submit(request)

    if demo:
        @server.tool()
        def respond_as_owner(reference: str, accept: bool, message: str | None = None) -> dict[str, Any]:
            """DEMO ONLY: play the business owner and confirm or decline a pending request.
            In production this is done by the owner, e.g. with one tap on WhatsApp."""
            return book.owner_respond(reference, accept, message)

    server.book = book  # exposed for tests
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gentifi-mcp", description="Reference MCP server for Gentifi Business Profiles.")
    parser.add_argument("--profiles", type=Path, default=Path(os.environ.get("GENTIFI_PROFILES", "profiles")),
                        help="folder of profile JSON files (default: $GENTIFI_PROFILES or ./profiles)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GENTIFI_PORT", 8000)),
                        help="port for --transport streamable-http, on 127.0.0.1 (default: $GENTIFI_PORT or 8000)")
    parser.add_argument("--demo", action="store_true", help="add the respond_as_owner tool")
    parser.add_argument("--now", default=os.environ.get("GENTIFI_NOW"),
                        help="freeze the clock at this ISO 8601 time (for demos and tests)")
    parser.add_argument("--telegram", action="store_true",
                        help="send pending requests to owners on Telegram (needs TELEGRAM_BOT_TOKEN, e.g. in .env)")
    parser.add_argument("--nostr-relay", action="append", default=[], metavar="URL",
                        help="publish every business to this Nostr relay, and again when its listing changes "
                             "(repeat for more; needs GENTIFI_NOSTR_MASTER_KEY, see gentifi-nostr init)")
    parser.add_argument("--nostr-allow-public", action="store_true",
                        help="allow --nostr-relay addresses that aren't on this machine")
    args = parser.parse_args(argv)

    fixed: datetime | None = parse_time(args.now) if args.now else None
    clock = (lambda: fixed) if fixed else utc_now
    directory = Directory.from_folder(args.profiles, clock=clock)
    book = RequestBook(directory)
    hide_onboarding(directory)
    if args.telegram:
        start_telegram_relay(book)
    if args.nostr_relay:
        start_nostr_publisher(book, args.nostr_relay, args.nostr_allow_public)
    server = build_server(directory, demo=args.demo, book=book)
    if args.transport == "streamable-http":
        server.run(args.transport, port=args.port)
    else:
        server.run(args.transport)


def hide_onboarding(directory: Directory) -> None:
    """Businesses whose owner is still checking their listing stay hidden, Telegram or not."""
    from .relay.cli import db_path
    from .relay.links import Links
    from .relay.relay import hide_onboarding as hide

    if db_path().is_file():
        hide(directory, Links(db_path()))


def start_telegram_relay(book: RequestBook) -> None:
    from .relay import Links, OwnerRelay
    from .relay.cli import db_path, telegram_channel
    from .relay.telegram import TelegramError

    try:
        channel = telegram_channel()
        username = channel.check()
    except TelegramError as exc:
        sys.exit(f"gentifi-mcp: can't start the Telegram relay: {exc}")
    relay = OwnerRelay(book, channel, Links(db_path()), bot_username=username)
    threading.Thread(target=relay.run, name="gentifi-relay", daemon=True).start()
    # stderr only: with the stdio transport, stdout carries MCP messages
    print(f"gentifi-mcp: owner relay running as @{username}", file=sys.stderr)


def start_nostr_publisher(book: RequestBook, relays: list[str], allow_public: bool) -> None:
    try:
        from .nostr import NostrKeyError, Publisher, RelayError, check_relays
        from .nostr.cli import keystore
    except ImportError as exc:
        sys.exit(f"gentifi-mcp: Nostr publishing needs the nostr extra (pip install -e \".[nostr]\"): {exc}")
    try:
        publisher = Publisher(book.directory, keystore(), check_relays(relays, allow_public), lock=book.lock)
    except (NostrKeyError, RelayError) as exc:
        sys.exit(f"gentifi-mcp: can't publish to Nostr: {exc}")
    publisher.start()
    print(f"gentifi-mcp: publishing to Nostr at {', '.join(relays)}", file=sys.stderr)


if __name__ == "__main__":
    main()
