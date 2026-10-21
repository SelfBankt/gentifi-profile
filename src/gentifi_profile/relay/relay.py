"""Owner confirmation relay: puts each pending request in front of the business
owner with one-tap buttons and turns their answer into the receipt the agent
is waiting for.

    pending -> Accept           -> confirmed (+ payment request) -> paid -> collected/delivered
            -> Suggest a time   -> declined with the suggestion (the agent asks the customer)
            -> Decline          -> declined (Sold out also marks those items sold out)
            -> no answer        -> expired at confirm_by

Onboarding (invite --onboard): the business stays hidden from agents while the owner
checks a summary of the listing Gentifi set up, says how customers can pay, and taps
Go live. "Something's wrong" sends their note to Gentifi's team chats (linked
with `gentifi-relay team`); `gentifi-relay review` reloads the edited profile and
shows it to them again.

Photos: an owner can send a photo of their price board or menu at any time. It goes
to the team chats with a draft catalogue read from it (drafts.py), for Gentifi to
check before changing anything.

Silence is safe: nothing is confirmed to the customer until the owner taps Accept.
Gentifi never holds money: payment requests come from the business's own wallet
or payment link (see payments.py).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..freshness import parse_time
from ..nostr.mapping import hours_text
from ..store import RequestBook
from .channel import Button, Incoming, Keyboard, OwnerChannel
from .drafts import Draft, Drafter
from .links import Links, Merchant
from .payments import Http, PaymentError, check_address, http_get, is_settled, request_invoice

TICK_SECONDS = 30
SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}
STOCK_VERBS = {"so", "si", "sd"}  # sold out, back in stock, done: buttons from /soldout and /instock
SETUP_VERB = "ob"                  # onboarding buttons: ob:ok, ob:edit, ob:ln, ob:link, ob:live, ob:listing, ob:help
LIVE_COMMANDS = {"/orders", "/soldout", "/instock", "/pause", "/resume"}  # wait until the business is live
HELP = """\
Gentifi sends you orders and bookings from customers' AI assistants. Tap a button to answer.

/orders – what's waiting for you
/listing – what customers' assistants can see
/soldout – mark items sold out
/instock – mark items back in stock
/pause – stop new orders (holiday, sold out for the day)
/resume – take orders again
/help – this message

Nothing is confirmed to a customer until you tap Accept. If you don't answer in time, the request expires and the customer is told."""


def hide_onboarding(directory: Any, links: Links) -> None:
    """Keep businesses still being onboarded out of sight until their owner goes live."""
    directory.drafts.update(links.onboarding())


def price_text(offering: dict[str, Any]) -> str:
    price = offering.get("price", {})
    if price.get("type") not in {"fixed", "from"} or not price.get("amount"):
        return "price on request"
    per = price.get("per")
    return (("from " if price["type"] == "from" else "") + money(price["amount"]) +
            (f" per {per}" if per and per not in {"item", "each"} else ""))


def money(m: dict[str, Any] | None) -> str:
    if not m:
        return "price to be agreed"
    amount = m["amount_minor"] / 100
    sym = SYMBOLS.get(m["currency"])
    return f"{sym}{amount:,.2f}" if sym else f"{amount:,.2f} {m['currency']}"


class OwnerRelay:
    def __init__(self, book: RequestBook, channel: OwnerChannel, links: Links,
                 http: Http = http_get, bot_username: str | None = None, drafter: Drafter | None = None,
                 background: Callable[[Callable[[], None]], None] | None = None):
        self.book = book
        self.channel = channel
        self.links = links
        self.http = http
        self.bot_username = bot_username
        self.drafter = drafter
        # Reading a photo takes seconds: do it off the relay's thread (tests run it at once)
        self.background = background or (lambda fn: threading.Thread(target=fn, name="gentifi-draft", daemon=True).start())
        self.events: queue.Queue[tuple[str, Any, Any]] = queue.Queue()
        self.awaiting_time: dict[str, str] = {}   # chat_id -> reference waiting for a typed time
        self.awaiting_setup: dict[str, str] = {}  # chat_id -> "change", "lightning" or "link" while onboarding
        self._acting: set[str] = set()            # references whose events the relay itself caused
        self._stop = threading.Event()
        book.listeners.append(self._on_event)
        hide_onboarding(book.directory, links)

    # ------------------------------------------------------------ plumbing

    def _on_event(self, event: str, reference: str) -> None:
        if reference not in self._acting:
            self.events.put(("event", event, reference))

    def step(self) -> None:
        """Handle everything queued, then any owner activity already waiting (tests, dry runs)."""
        for inc in self.channel.poll(0):
            self.events.put(("in", inc, None))
        while not self.events.empty():
            self._dispatch(self.events.get())

    def _dispatch(self, item: tuple[str, Any, Any]) -> None:
        kind, a, b = item
        try:
            if kind == "event":
                self.handle_event(a, b)
            elif kind == "draft":
                self._drafted(a, b)
            else:
                self.handle(a)
        except Exception as exc:  # keep relaying whatever one message did
            print(f"gentifi-relay: error handling {kind}: {exc!r}", file=sys.stderr)

    def tick(self) -> None:
        """Periodic work: expire unanswered requests, check Lightning invoices, show edited listings,
        pick up profile files the toolkit's host has rewritten."""
        self.book.expire_overdue()
        with self.book.lock:
            self.book.directory.refresh()
        for business_id in self.links.take_review_requests():
            self._reviewed(business_id)
        for p in self.links.awaiting_verification():
            try:
                if is_settled(p.verify_url, self.http):
                    self._act(p.reference, lambda: self._mark_paid(p.reference))
                    self._refresh(p.reference, note="Paid by Lightning.")
            except PaymentError as exc:
                print(f"gentifi-relay: verify {p.reference}: {exc}", file=sys.stderr)

    def run(self, poll_seconds: int = 25) -> None:
        """Run until stop(): one thread polls the owner channel, this one does the work."""
        def poller() -> None:
            while not self._stop.is_set():
                for inc in self.channel.poll(poll_seconds):
                    self.events.put(("in", inc, None))
        threading.Thread(target=poller, name="gentifi-relay-poll", daemon=True).start()
        last = 0.0
        while not self._stop.is_set():
            try:
                self._dispatch(self.events.get(timeout=5))
            except queue.Empty:
                pass
            if time.monotonic() - last >= TICK_SECONDS:
                last = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:
                    print(f"gentifi-relay: tick failed: {exc!r}", file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()

    def _act(self, reference: str, fn):
        self._acting.add(reference)
        try:
            return fn()
        finally:
            self._acting.discard(reference)

    # ------------------------------------------------------------ helpers

    def _profile(self, business_id: str) -> dict[str, Any]:
        return self.book.directory.get(business_id)

    def _when(self, iso: str, business_id: str) -> str:
        dt = parse_time(iso).astimezone(ZoneInfo(self._profile(business_id).get("time_zone", "Europe/London")))
        today = self.book.now.astimezone(dt.tzinfo).date()
        day = "today" if dt.date() == today else dt.strftime("%a %-d %b")
        return f"{dt:%H:%M} {day}"

    def _summary(self, reference: str) -> str:
        booking = self.book.bookings[reference]
        req, receipt = booking["request"], booking["receipt"]
        profile = self._profile(booking["business_id"])
        offerings = {o["id"]: o for o in profile["offerings"]}
        slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
        lines = []
        for item in req.get("items", []):
            o = offerings.get(item["offering_id"], {"name": item["offering_id"]})
            variant = next((v["name"] for v in o.get("variants", []) if v["id"] == item.get("variant_id")), None)
            name = o["name"] + (f" ({variant})" if variant else "")
            slot = slots.get(item.get("slot_id"))
            when = f" at {self._when(slot['start'], booking['business_id'])}" if slot else ""
            lines.append(f"{item.get('quantity', 1)} × {name}{when}")
        customer = req.get("customer", {})
        extra = [f"Total {money(receipt.get('total'))}"]
        if req.get("fulfilment"):
            extra.append(req["fulfilment"].replace("_", " ").capitalize())
        if customer.get("party_size"):
            extra.append(f"{customer['party_size']} people")
        lines.append(" · ".join(extra))
        if customer.get("name"):
            lines.append("Customer: " + ", ".join(x for x in [customer["name"], customer.get("phone"),
                                                              customer.get("email")] if x))
        elif booking.get("forgotten"):
            lines.append("Customer: details removed now this has ended.")
        if req.get("notes"):
            lines.append(f"Note: {req['notes']}")
        return "\n".join(lines)

    def _heading(self, reference: str) -> str:
        action = self.book.bookings[reference]["request"]["action"]
        noun = {"book": "booking", "hold": "hold", "enquire": "enquiry"}.get(action, "order")
        return f"{noun.capitalize()} {reference}"

    def _owns(self, inc: Incoming, reference: str) -> bool:
        merchant = self.links.by_chat(inc.chat_id)
        booking = self.book.bookings.get(reference)
        return bool(merchant and booking and booking["business_id"] == merchant.business_id)

    # ------------------------------------------------------------ store events

    def handle_event(self, event: str, reference: str) -> None:
        if event == "pending":
            self._send_prompt(reference)
        elif event in {"confirmed", "declined", "expired", "cancelled"}:
            # Changed outside the relay: the customer cancelled, it expired, or a demo tool answered.
            note = {"expired": "Expired: you didn't answer in time, so the customer was told it wasn't confirmed.",
                    "cancelled": "The customer cancelled this."}.get(event)
            self._refresh(reference, note=note)
        elif event == "payment":
            self._refresh(reference)

    def _send_prompt(self, reference: str) -> None:
        booking = self.book.bookings[reference]
        merchant = self.links.merchant(booking["business_id"])
        if not merchant or not merchant.chat_id:
            print(f"gentifi-relay: {booking['business_id']} has no linked owner chat; "
                  f"{reference} will expire unanswered", file=sys.stderr)
            return
        text = self._pending_text(reference)
        mid = self.channel.send(merchant.chat_id, text, self._pending_buttons(reference))
        self.links.save_prompt(reference, booking["business_id"], merchant.chat_id, mid)

    def _pending_text(self, reference: str) -> str:
        booking = self.book.bookings[reference]
        by = self._when(booking["receipt"]["confirm_by"], booking["business_id"])
        return (f"New {self._heading(reference)[0].lower() + self._heading(reference)[1:]} via an AI assistant\n\n"
                f"{self._summary(reference)}\n\nPlease answer by {by}.")

    @staticmethod
    def _pending_buttons(reference: str) -> Keyboard:
        return [[Button("Accept", f"a:{reference}"), Button("Decline", f"d:{reference}")],
                [Button("Suggest another time", f"s:{reference}")]]

    def _refresh(self, reference: str, note: str | None = None) -> None:
        """Redraw the owner's message for a request to match its current state."""
        p = self.links.prompt(reference)
        booking = self.book.bookings.get(reference)
        if p is None or booking is None:
            return
        receipt = booking["receipt"]
        status = receipt["status"]
        head = f"{self._heading(reference)}\n\n{self._summary(reference)}"
        if status == "pending_confirmation":
            self.channel.edit(p.chat_id, p.message_id, self._pending_text(reference), self._pending_buttons(reference))
            return
        if status in {"confirmed", "held"}:
            pay = receipt.get("payment", {})
            fulfilment = booking["request"].get("fulfilment")
            done_label = "Delivered" if fulfilment in {"delivery", "at_customer_location"} else (
                "Collected" if fulfilment == "collection" else "Done")
            lines = [f"Accepted. {self._payment_line(booking, pay)}"]
            if note:
                lines.append(note)
            buttons: Keyboard = []
            if pay.get("status") == "awaiting":
                buttons.append([Button("Mark paid", f"p:{reference}")])
            buttons.append([Button(done_label, f"c:{reference}:{done_label}")])
            buttons.append([Button("Cancel order", f"x:{reference}")])
            self.channel.edit(p.chat_id, p.message_id, head + "\n\n" + "\n".join(lines), buttons)
            return
        label = {"declined": "Declined", "expired": "Expired", "cancelled": "Cancelled",
                 "failed": "Failed"}.get(status, status)
        text = f"{head}\n\n{note or label + '. ' + receipt.get('message', '')}".rstrip()
        self.channel.edit(p.chat_id, p.message_id, text, None)
        self.links.close(reference)

    @staticmethod
    def _payment_line(booking: dict[str, Any], pay: dict[str, Any]) -> str:
        if pay.get("status") == "paid":
            return "Paid."
        url = pay.get("checkout_url", "")
        if url.startswith("lightning:"):
            return "The customer has a Lightning invoice from your wallet."
        if url:
            return "The customer has your payment link."
        return "The customer pays you on collection or delivery."

    # ------------------------------------------------------------ owner activity

    def handle(self, inc: Incoming) -> None:
        if inc.kind == "button":
            self._button(inc)
        elif inc.kind in {"photo", "file"}:
            self._photo(inc)
        else:
            self._text(inc)

    def _button(self, inc: Incoming) -> None:
        verb, _, rest = inc.value.partition(":")
        if verb == SETUP_VERB:
            self._setup_button(inc, rest)
            return
        if verb in STOCK_VERBS:
            self._stock_button(inc, verb, rest)
            return
        reference, _, arg = rest.partition(":")
        if not self._owns(inc, reference):
            self.channel.ack(inc, "This isn't one of your requests.")
            return
        try:
            note = self._act(reference, lambda: self._do(verb, reference, arg, inc))
        except ValueError:
            note = "This request isn't open any more."
            self._refresh(reference)
        self.channel.ack(inc, note)

    def _do(self, verb: str, reference: str, arg: str, inc: Incoming) -> str | None:
        p = self.links.prompt(reference)
        if verb == "a":
            receipt = self.book.owner_respond(reference, True)
            if receipt["status"] == "confirmed":
                self._hand_off_payment(reference)
                self._refresh(reference)
                return "Accepted. The customer has been told."
            self._refresh(reference, note=f"Couldn't accept: {receipt.get('message', '')} The customer has been told.")
            return None
        if verb == "d":
            self.channel.edit(p.chat_id, p.message_id, self._pending_text(reference) + "\n\nWhy are you declining?",
                              [[Button("Sold out", f"dr:{reference}:sold"), Button("Too busy", f"dr:{reference}:busy")],
                               [Button("Other reason", f"dr:{reference}:other")],
                               [Button("« Back", f"b:{reference}")]])
            return None
        if verb == "dr":
            booking = self.book.bookings[reference]
            name = self._profile(booking["business_id"])["business"]["name"]
            message = {"sold": f"{name} has sold out of this.",
                       "busy": f"{name} can't take this one right now."}.get(arg, f"Declined by {name}.")
            if arg == "sold":
                self.book.mark_sold_out(booking["business_id"],
                                        {i["offering_id"] for i in booking["request"].get("items", [])})
            self.book.owner_respond(reference, False, message)
            self._refresh(reference, note="Declined. The customer has been told." +
                          (" Those items are now shown as sold out." if arg == "sold" else ""))
            return "Declined."
        if verb == "s":
            self.channel.edit(p.chat_id, p.message_id, self._pending_text(reference) + "\n\nWhen could you do it?",
                              [[Button("Later today", f"st:{reference}:later"),
                                Button("Tomorrow", f"st:{reference}:tomorrow")],
                               [Button("Type a time", f"st:{reference}:type")],
                               [Button("« Back", f"b:{reference}")]])
            return None
        if verb == "st":
            if arg == "type":
                self.awaiting_time[inc.chat_id] = reference
                self.channel.send(inc.chat_id, f"Type the time you can do for {reference}, e.g. \"Saturday 10am\".")
                return None
            when = {"later": "later today", "tomorrow": "tomorrow"}.get(arg, arg)
            return self._suggest(reference, when)
        if verb == "b":
            self._refresh(reference)
            return None
        if verb == "p":
            self._mark_paid(reference)
            self._refresh(reference)
            return "Marked paid."
        if verb == "c":
            pay = self.book.bookings[reference]["receipt"].get("payment", {})
            if pay.get("status") == "due_on_fulfilment":
                self.book.set_payment(reference, {"status": "paid"})  # paid in person on handover
            self.book.close(reference)
            self.channel.edit(p.chat_id, p.message_id,
                              f"{self._heading(reference)}\n\n{self._summary(reference)}\n\n{arg or 'Done'}. Order closed.",
                              None)
            self.links.close(reference)
            return "Closed."
        if verb == "x":
            booking = self.book.bookings[reference]
            self.channel.edit(p.chat_id, p.message_id,
                              f"{self._heading(reference)}\n\n{self._summary(reference)}\n\n"
                              "Cancel this order? The customer will be told.",
                              [[Button("Yes, cancel it", f"xx:{reference}")], [Button("« Back", f"b:{reference}")]])
            return None
        if verb == "xx":
            self.book.owner_cancel(reference)
            self._refresh(reference, note="Cancelled. The customer has been told.")
            return "Cancelled."
        return None

    def _suggest(self, reference: str, when: str) -> str:
        self.book.owner_suggest(reference, when)
        self._refresh(reference, note=f"You suggested {when}. The customer's assistant will check with them "
                                      "and send a new request if it suits.")
        return "Suggestion sent."

    def _mark_paid(self, reference: str) -> None:
        pay = dict(self.book.bookings[reference]["receipt"].get("payment", {}))
        pay["status"] = "paid"
        self.book.set_payment(reference, pay)
        self.links.set_verify(reference, None)

    def _hand_off_payment(self, reference: str) -> None:
        """Give the customer's agent a way to pay the business directly."""
        booking = self.book.bookings[reference]
        receipt = booking["receipt"]
        total = receipt.get("total")
        merchant = self.links.merchant(booking["business_id"]) or Merchant(booking["business_id"], None, None, None, None)
        profile = self._profile(booking["business_id"])
        name = profile["business"]["name"]
        in_person = [m.replace("_", " ") for m in profile["policies"].get("payment_methods", [])
                     if m in {"cash", "card_in_person"}]
        also = f" You can also pay {name} in person ({' or '.join(in_person)})." if in_person else ""
        if total and merchant.lightning_address:
            try:
                inv = request_invoice(merchant.lightning_address, total["amount_minor"], total["currency"],
                                      f"Gentifi {reference}", self.http)
                self.links.set_verify(reference, inv.verify_url)
                self.book.set_payment(reference, {"status": "awaiting", "checkout_url": inv.uri},
                                      message=(f"Confirmed by {name}. Pay {inv.sats:,} sats by Lightning "
                                               f"({money(total)} at {SYMBOLS.get(total['currency'], '')}"
                                               f"{inv.btc_price:,.0f} per BTC) using checkout_url; the invoice "
                                               f"goes straight to {name}'s wallet.{also}"))
                return
            except PaymentError as exc:
                p = self.links.prompt(reference)
                if p:
                    self.channel.send(p.chat_id, f"I couldn't get a Lightning invoice from {merchant.lightning_address} "
                                                 f"({exc}), so the customer was told to pay you another way.")
        if total and merchant.payment_link:
            self.book.set_payment(reference, {"status": "awaiting", "checkout_url": merchant.payment_link},
                                  message=f"Confirmed by {name}. Pay {money(total)} using checkout_url.{also}")
            return
        self.book.set_payment(reference, {"status": "due_on_fulfilment"},
                              message=f"Confirmed by {name}. Pay {money(total)} on collection or delivery.{also}")

    # ------------------------------------------------------------ text and commands

    def _text(self, inc: Incoming) -> None:
        text = inc.value.strip()
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        if command == "/start":
            self._start(inc, arg.strip())
            return
        merchant = self.links.by_chat(inc.chat_id)
        if merchant is None:
            self.channel.send(inc.chat_id, "This bot is for businesses on Gentifi. "
                                           "Open the invite link Gentifi gave you to connect your business.")
            return
        if text.startswith("/"):
            self.awaiting_setup.pop(inc.chat_id, None)
        elif inc.chat_id in self.awaiting_setup:
            self._setup_text(inc, merchant, text)
            return
        if merchant.onboarding and command in LIVE_COMMANDS:
            self._send_review(inc.chat_id, merchant.business_id, "You're not live yet. First, check your listing:")
            return
        if inc.chat_id in self.awaiting_time and not text.startswith("/"):
            reference = self.awaiting_time.pop(inc.chat_id)
            try:
                self._act(reference, lambda: self._suggest(reference, text))
                self.channel.send(inc.chat_id, f"Sent: you suggested \"{text}\" for {reference}.")
            except ValueError:
                self.channel.send(inc.chat_id, f"{reference} isn't open any more.")
            return
        directory = self.book.directory
        name = self._profile(merchant.business_id)["business"]["name"]
        if command == "/pause":
            directory.pause(merchant.business_id)
            self.channel.send(inc.chat_id, f"Paused. {name} is hidden from AI assistants and new requests are "
                                           "declined until you send /resume.")
        elif command == "/resume":
            directory.resume(merchant.business_id)
            self.channel.send(inc.chat_id, f"{name} is taking requests again.")
        elif command == "/orders":
            self._orders(inc, merchant)
        elif command == "/listing":
            self._listing(inc, merchant)
        elif command in {"/soldout", "/instock"}:
            sold_out = command == "/soldout"
            self.channel.send(inc.chat_id, *self._stock_menu(merchant.business_id, sold_out))
        else:
            self.channel.send(inc.chat_id, HELP)

    def _start(self, inc: Incoming, code: str) -> None:
        if code and self.links.redeem_team(code, inc.chat_id, inc.name):
            self.channel.send(inc.chat_id, "You're on the Gentifi team. I'll message you here when an owner asks "
                                           "for a change to their listing, connects, or goes live.")
            return
        if code:
            business_id = self.links.redeem(code, inc.chat_id, inc.name)
            if business_id and self.links.merchant(business_id).onboarding:
                self._tell_team(f"{self._name(business_id)}: {inc.name or 'the owner'} connected and is "
                                "checking their listing.")
                self._welcome(inc, business_id)
                return
            if business_id:
                name = self._profile(business_id)["business"]["name"] if business_id in self.book.directory.profiles else business_id
                self.channel.send(inc.chat_id, f"Connected to {name}. When a customer's AI assistant sends an order "
                                               f"or booking, I'll message you here to Accept or Decline.\n\n{HELP}")
                return
            if not self.links.by_chat(inc.chat_id):
                self.channel.send(inc.chat_id, "That invite link has expired or was already used. "
                                               "Ask Gentifi for a new one.")
                return
        merchant = self.links.by_chat(inc.chat_id)
        if merchant and merchant.onboarding:
            self._send_review(inc.chat_id, merchant.business_id, "Please check your listing:")
        elif merchant:
            self.channel.send(inc.chat_id, HELP)
        else:
            self.channel.send(inc.chat_id, "Hi! This bot is for businesses on Gentifi. "
                                           "Open the invite link Gentifi gave you to connect your business.")

    def _orders(self, inc: Incoming, merchant: Merchant) -> None:
        open_ = [p for p in self.links.open_prompts(merchant.business_id) if p.reference in self.book.bookings]
        if not open_:
            self.channel.send(inc.chat_id, "Nothing waiting for you right now.")
            return
        for p in open_:
            # Re-send so the buttons are at the bottom of the chat, and retire the old message.
            self.channel.edit(p.chat_id, p.message_id, f"{self._heading(p.reference)}: moved down.", None)
            mid = self.channel.send(inc.chat_id, "…")
            self.links.save_prompt(p.reference, p.business_id, inc.chat_id, mid)
            self._refresh(p.reference)

    def _listing(self, inc: Incoming, merchant: Merchant) -> None:
        profile = self._profile(merchant.business_id)
        status = {r["offering_id"]: r["status"].replace("_", " ") for r in profile["availability"]
                  if r["type"] == "stock"}
        lines = [profile["business"]["name"] +
                 (" (paused)" if merchant.business_id in self.book.directory.paused else "")]
        for o in profile["offerings"]:
            lines.append(f"• {o['name']}: {price_text(o)}" + (f" ({status[o['id']]})" if o["id"] in status else ""))
        pay = []
        if merchant.lightning_address:
            pay.append(f"Lightning to {merchant.lightning_address}")
        if merchant.payment_link:
            pay.append("your payment link")
        pay.append("in person")
        lines.append("\nCustomers can pay by " + ", ".join(pay) + ".")
        self.channel.send(inc.chat_id, "\n".join(lines))

    # ------------------------------------------------------------ sold out and back in stock

    def _stock_items(self, business_id: str) -> list[tuple[dict[str, Any], bool]]:
        """Each product with whether it's sold out (every stock record for it says so)."""
        profile = self._profile(business_id)
        items = []
        for o in profile["offerings"]:
            if o["kind"] != "product":
                continue
            records = [r for r in profile["availability"] if r["type"] == "stock" and r["offering_id"] == o["id"]]
            items.append((o, bool(records) and all(r["status"] == "out_of_stock" for r in records)))
        return items

    def _stock_menu(self, business_id: str, sold_out: bool) -> tuple[str, Keyboard | None]:
        """/soldout lists what's still for sale; /instock lists what's sold out. One tap each."""
        items = [o for o, gone in self._stock_items(business_id) if gone != sold_out]
        if not items:
            return ("Nothing is marked sold out." if not sold_out else "Everything is already marked sold out."), None
        verb = "so" if sold_out else "si"
        text = "Tap what's sold out:" if sold_out else "Tap what's back in stock:"
        return text, [[Button(o["name"], f"{verb}:{o['id']}")] for o in items] + [[Button("Done", "sd:")]]

    def _stock_button(self, inc: Incoming, verb: str, offering_id: str) -> None:
        merchant = self.links.by_chat(inc.chat_id)
        if merchant is None:
            self.channel.ack(inc, "This isn't one of your businesses.")
            return
        names = {o["id"]: o["name"] for o, _ in self._stock_items(merchant.business_id)}
        if verb == "sd" or offering_id not in names:
            self.channel.edit(inc.chat_id, inc.message_id, "Done. Send /soldout or /instock to change stock again.")
            self.channel.ack(inc)
            return
        if verb == "so":
            self.book.mark_sold_out(merchant.business_id, {offering_id})
            note = f"{names[offering_id]}: sold out. Customers' assistants now see it as sold out."
        else:
            self.book.mark_in_stock(merchant.business_id, {offering_id})
            note = f"{names[offering_id]}: back in stock. Customers' assistants now see it as in stock."
        text, buttons = self._stock_menu(merchant.business_id, verb == "so")
        self.channel.edit(inc.chat_id, inc.message_id, f"{note}\n\n{text}", buttons)
        self.channel.ack(inc, note.split(".")[0])

    # ------------------------------------------------------------ onboarding

    def _name(self, business_id: str) -> str:
        profiles = self.book.directory.profiles
        return profiles[business_id]["business"]["name"] if business_id in profiles else business_id

    def _tell_team(self, text: str) -> None:
        """Let Gentifi know, in each team chat (gentifi-relay team)."""
        for chat_id, _ in self.links.team():
            try:
                self.channel.send(chat_id, text)
            except Exception as exc:  # the owner's flow carries on regardless
                print(f"gentifi-relay: couldn't tell team chat {chat_id}: {exc!r}", file=sys.stderr)

    def _welcome(self, inc: Incoming, business_id: str) -> None:
        if business_id not in self.book.directory.profiles:
            self.channel.send(inc.chat_id, "Connected. Gentifi is still setting up your listing; "
                                           "I'll send it here for you to check when it's ready.")
            return
        name = self._profile(business_id)["business"]["name"]
        self._send_review(inc.chat_id, business_id,
                          f"Hi{' ' + inc.name if inc.name else ''}, I'll get {name} found by people's AI assistants. "
                          "Gentifi has set up your listing. Please check it:")

    def _review_text(self, business_id: str) -> str:
        profile = self._profile(business_id)
        b = profile["business"]
        address = b["location"].get("address", {})
        lines = [b["name"]]
        place = ", ".join(x for x in [address.get("line1"), address.get("locality"), address.get("postal_code")] if x)
        if place:
            lines.append(place)
        if hours_text(b):
            lines.append(hours_text(b))
        offerings = profile["offerings"]
        lines.append(f"\n{len(offerings)} item{'s' if len(offerings) != 1 else ''}" +
                     (", including:" if len(offerings) > 3 else ":"))
        lines += [f"• {o['name']}: {price_text(o)}" for o in offerings[:3]]
        ways = {f for o in offerings for f in o.get("fulfilment", [])}
        how = [w for w, label in [("collection", "collection"), ("delivery", "delivery")] if w in ways]
        if how:
            lines.append("\nCustomers can order for " + " or ".join(how) + ".")
        return "\n".join(lines)

    def _send_review(self, chat_id: str, business_id: str, intro: str) -> None:
        if business_id not in self.book.directory.profiles:
            self.channel.send(chat_id, "Gentifi is still setting up your listing; I'll send it here when it's ready.")
            return
        self.channel.send(chat_id, f"{intro}\n\n{self._review_text(business_id)}",
                          [[Button("Looks right", f"{SETUP_VERB}:ok")],
                           [Button("Something's wrong", f"{SETUP_VERB}:edit")]])

    def _send_payment_step(self, chat_id: str, merchant: Merchant) -> None:
        lines = ["How do customers pay you?",
                 "They can always pay you in person when they collect or you deliver. You can also let them pay "
                 "ahead, straight to you (Gentifi never handles the money):",
                 f"• Lightning address: {merchant.lightning_address or 'not set'}",
                 f"• Payment link: {merchant.payment_link or 'not set'}"]
        self.channel.send(chat_id, "\n\n".join(lines[:2]) + "\n" + "\n".join(lines[2:]),
                          [[Button(("Change" if merchant.lightning_address else "Add") + " Lightning address",
                                   f"{SETUP_VERB}:ln")],
                           [Button(("Change" if merchant.payment_link else "Add") + " payment link",
                                   f"{SETUP_VERB}:link")],
                           [Button("Go live", f"{SETUP_VERB}:live")]])

    def _setup_button(self, inc: Incoming, arg: str) -> None:
        merchant = self.links.by_chat(inc.chat_id)
        if merchant is None:
            self.channel.ack(inc, "This isn't one of your businesses.")
            return
        self.channel.ack(inc)
        if arg == "listing":
            self._listing(inc, merchant)
            return
        if arg == "help":
            self.channel.send(inc.chat_id, HELP)
            return
        if not merchant.onboarding:
            self.channel.edit(inc.chat_id, inc.message_id, "You're already live. Send /listing to see what "
                                                           "customers' assistants see.")
            return
        if arg in {"ok", "edit"} and inc.message_id:
            self.channel.edit(inc.chat_id, inc.message_id, self._review_text(merchant.business_id), None)
        if arg == "ok":
            self._send_payment_step(inc.chat_id, merchant)
        elif arg == "edit":
            self.awaiting_setup[inc.chat_id] = "change"
            self.channel.send(inc.chat_id, "What needs changing? Tell me in one message, for example "
                                           "\"eggs are £3.80 now and we're closed on Thursdays\".")
        elif arg == "ln":
            self.awaiting_setup[inc.chat_id] = "lightning"
            self.channel.send(inc.chat_id, "Send me your Lightning address. It looks like an email address, "
                                           "for example shop@strike.me.")
        elif arg == "link":
            self.awaiting_setup[inc.chat_id] = "link"
            self.channel.send(inc.chat_id, "Send me your payment link from SumUp, Square, Stripe or similar. "
                                           "It starts with https://")
        elif arg == "live":
            self._go_live(inc, merchant)

    def _setup_text(self, inc: Incoming, merchant: Merchant, text: str) -> None:
        step = self.awaiting_setup[inc.chat_id]
        if step == "change":
            self.awaiting_setup.pop(inc.chat_id)
            self.links.save_change_request(merchant.business_id, text)
            print(f"gentifi-relay: {merchant.business_id} asked for a change: {text}", file=sys.stderr)
            self._tell_team(f"{self._name(merchant.business_id)} asked for a change:\n\n\"{text}\"\n\n"
                            f"Edit the profile, then run: gentifi-relay review {merchant.business_id}")
            self.channel.send(inc.chat_id, "Thanks. Gentifi will make the change and send you your listing "
                                           "to check again.")
            return
        if step == "lightning":
            address = text.strip().removeprefix("lightning:")
            try:
                check_address(address, self.http)
            except PaymentError as exc:
                self.channel.send(inc.chat_id, f"That address didn't work ({exc}). Check it and send it again.")
                return
            self.links.configure(merchant.business_id, lightning_address=address)
        else:
            link = text.strip()
            parts = urlsplit(link)
            if parts.scheme != "https" or not parts.netloc or " " in link:
                self.channel.send(inc.chat_id, "That doesn't look like a payment link. It should start with "
                                               "https:// - send it again.")
                return
            self.links.configure(merchant.business_id, payment_link=link)
        self.awaiting_setup.pop(inc.chat_id)
        self.channel.send(inc.chat_id, "Saved.")
        self._send_payment_step(inc.chat_id, self.links.merchant(merchant.business_id))

    def _go_live(self, inc: Incoming, merchant: Merchant) -> None:
        if merchant.business_id not in self.book.directory.profiles:
            self.channel.send(inc.chat_id, "Your listing isn't ready yet; Gentifi will send it to you to check.")
            return
        self.links.set_status(merchant.business_id, "live")
        with self.book.lock:
            self.book.directory.go_live(merchant.business_id)
        if inc.message_id:
            self.channel.edit(inc.chat_id, inc.message_id, "Payment set up.", None)
        name = self._profile(merchant.business_id)["business"]["name"]
        print(f"gentifi-relay: {merchant.business_id} is live", file=sys.stderr)
        self._tell_team(f"{name} is live.")
        self.channel.send(inc.chat_id, f"You're live. Customers' AI assistants can now find {name}. "
                                       "When one sends an order, I'll message you here.",
                          [[Button("See my listing", f"{SETUP_VERB}:listing"), Button("Help", f"{SETUP_VERB}:help")]])

    def _reviewed(self, business_id: str) -> None:
        """Gentifi asked to show this business's profile again (gentifi-relay review, or a new invite)."""
        merchant = self.links.merchant(business_id)
        if merchant is None:
            return
        with self.book.lock:
            if merchant.onboarding:
                self.book.directory.drafts.add(business_id)  # before the reload announces the change
            try:
                self.book.directory.reload(business_id)
            except (KeyError, ValueError) as exc:
                print(f"gentifi-relay: couldn't load the profile for {business_id}: {exc.args[0]}", file=sys.stderr)
                if merchant.chat_id:  # a fresh invite may come before its folder: nothing to tell yet
                    self._tell_team(f"Couldn't load the profile for {business_id}, so the owner wasn't sent it: "
                                    f"{exc.args[0]}")
                return
        if not merchant.chat_id:
            return
        if merchant.onboarding:
            self._send_review(merchant.chat_id, business_id, "Gentifi has updated your listing. Please check it again:")
        else:
            self.channel.send(merchant.chat_id, "Gentifi has updated your listing. Send /listing to see it.")

    # ------------------------------------------------------------ photos of prices

    def _photo(self, inc: Incoming) -> None:
        merchant = self.links.by_chat(inc.chat_id)
        if merchant is None:
            self.channel.send(inc.chat_id, "This bot is for businesses on Gentifi. "
                                           "Open the invite link Gentifi gave you to connect your business.")
            return
        if inc.kind == "file":
            self.channel.send(inc.chat_id, "I can't read that kind of file. Send a photo of your price board, "
                                           "menu or price list instead (a screenshot of a PDF works too).")
            return
        name = self._name(merchant.business_id)
        self.channel.send(inc.chat_id, "Thanks, I've got your photo. Gentifi will read it and send you your "
                                       "listing to check before anything changes.")
        for chat_id, _ in self.links.team():
            try:
                self.channel.send_photo(chat_id, inc.value, f"{name} sent a photo of their prices.")
            except Exception as exc:
                print(f"gentifi-relay: couldn't pass a photo to team chat {chat_id}: {exc!r}", file=sys.stderr)
        if self.drafter is None:
            self._tell_team("Read it with gentifi draft on your Mac (the bot can't draft here).")
            return

        def read() -> None:
            try:
                draft = self.drafter.draft(merchant.business_id, self.channel.download(inc.value), inc.mime)
            except Exception as exc:  # reported to the team, never to the owner
                draft = Draft(Path("."), error=f"couldn't fetch the photo: {exc}")
            self.events.put(("draft", merchant.business_id, draft))

        self.background(read)

    def _drafted(self, business_id: str, draft: Draft) -> None:
        name = self._name(business_id)
        print(f"gentifi-relay: draft for {business_id}: {len(draft.rows)} items in {draft.folder}"
              + (f", error: {draft.error}" if draft.error else ""), file=sys.stderr)
        if draft.error:
            self._tell_team(f"Couldn't draft {name}'s photo: {draft.error}")
            return
        lines = [f"Draft for {name}: {len(draft.rows)} items, {len(draft.to_check)} to check."]
        for row in draft.rows:
            price = (f"£{float(row['price']):.2f}" if row.get("price") else
                     f"from £{float(row['price_from']):.2f}" if row.get("price_from") else "no price")
            unit = f" per {row['unit']}" if row.get("unit") and row["unit"] not in {"each", "item"} else ""
            lines.append(f"• {row.get('name', '?')}: {price}{unit}" + (f"  ⚠ {row['check']}" if row.get("check") else ""))
        lines += ["", f"Files on the VPS: {draft.folder}",
                  "Check every name and price against the photo, then set the shop up "
                  "(gentifi init, or update its sheet) and send it to the owner with "
                  f"gentifi-bot invite {business_id} --onboard, or gentifi-bot review {business_id}."]
        text = "\n".join(lines)
        self._tell_team(text if len(text) <= 4000 else text[:3900] + "\n… (more in catalogue.csv)")
