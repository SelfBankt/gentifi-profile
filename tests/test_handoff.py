"""Orders from the toolkit's confirmation page reach the owner in Telegram, and the page can see the answer."""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp")

from gentifi_profile.relay import Links, MockChannel, OwnerRelay
from gentifi_profile.relay.handoff import Handoff, HandoffError, add_routes, load_secret
from gentifi_profile.server import build_server
from gentifi_profile.store import Directory, RequestBook

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
OWNER = "1001"
SECRET = "s3cret-for-tests"


class Clock:
    def __init__(self):
        self.now = datetime.fromisoformat("2026-10-15T20:00:00+01:00")

    def __call__(self):
        return self.now


@pytest.fixture
def shop():
    clock = Clock()
    book = RequestBook(Directory.from_folder(EXAMPLES, clock=clock))
    book.max_sla_minutes = 120
    links, chat = Links(), MockChannel()
    relay = OwnerRelay(book, chat, links)
    handoff = Handoff(book, links, SECRET)
    return clock, book, links, chat, relay, handoff


def link_owner(links, chat, relay):
    chat.type(OWNER, f"/start {links.invite(FARM)}", name="Jo")
    relay.step()


def page_order(ref="AB12CD34", **extra):
    return {"business_id": FARM, "ref": ref, "items": [{"offering_id": "eggs", "quantity": 2}],
            "customer": {"name": "Sam Carter", "contact": "07700 900123"}, "collection_date": "2026-10-17",
            "note": "Brown eggs if you have them", "confirmed_at": "2026-10-15T18:58:00Z", **extra}


def test_only_owners_on_telegram_get_orders(shop):
    _, book, links, chat, relay, handoff = shop
    assert not handoff.on_telegram(FARM)
    assert handoff.submit(page_order()) is None, "not on Telegram: the page hands over by email as before"
    link_owner(links, chat, relay)
    assert handoff.on_telegram(FARM)
    book.directory.pause(FARM)
    assert not handoff.on_telegram(FARM)
    book.directory.resume(FARM)
    links.set_status(FARM, "onboarding")
    assert not handoff.on_telegram(FARM)
    assert not handoff.on_telegram("gtf_unknownshop")


def test_a_confirmed_order_goes_to_the_owner_and_the_page_sees_the_answer(shop):
    _, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    state = handoff.submit(page_order())
    assert state["status"] == "pending_confirmation"
    relay.step()
    prompt = chat.last(OWNER)
    assert "2 × Free-range eggs" in prompt.text
    assert "Customer: Sam Carter, 07700 900123" in prompt.text
    assert "Note: Collection on 2026-10-17. Brown eggs if you have them" in prompt.text
    request = book.bookings[state["reference"]]["request"]
    assert request["customer_confirmation"] == {"method": "confirmation_page", "reference": "AB12CD34",
                                                "confirmed_at": "2026-10-15T18:58:00+00:00"}

    assert handoff.submit(page_order())["reference"] == state["reference"], "confirming twice is one order"
    assert handoff.order(FARM, "AB12CD34")["status"] == "pending_confirmation"
    chat.press(prompt.message_id, "Accept")
    relay.step()
    answer = handoff.order(FARM, "AB12CD34")
    assert answer["status"] == "confirmed"
    assert answer["payment"] == {"status": "due_on_fulfilment"}
    assert "Pay £7.00 on collection or delivery" in answer["message"]
    assert handoff.order(FARM, "ZZ99ZZ99") is None


def test_a_page_clock_slightly_ahead_still_counts_as_confirmed_first(shop):
    clock, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    ahead = (clock.now + timedelta(milliseconds=779)).isoformat()
    state = handoff.submit(page_order(confirmed_at=ahead))
    assert state["status"] == "pending_confirmation", state
    confirmed = book.bookings[state["reference"]]["request"]["customer_confirmation"]["confirmed_at"]
    assert datetime.fromisoformat(confirmed) == clock.now


def test_email_contacts_and_unanswered_orders(shop):
    clock, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    state = handoff.submit(page_order(customer={"name": "Sam", "contact": "sam@example.com"}))
    assert book.bookings[state["reference"]]["request"]["customer"] == {"name": "Sam", "email": "sam@example.com"}
    clock.now += timedelta(minutes=121)
    assert handoff.order(FARM, "AB12CD34")["status"] == "expired"


def test_answer_time_is_capped(shop):
    _, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    for action in book.directory.get(FARM)["actions"]:
        if action["action"] == "order":
            action["confirmation_sla_minutes"] = 24 * 60  # what the toolkit's profiles ask for
    state = handoff.submit(page_order())
    confirm_by = datetime.fromisoformat(state["confirm_by"])
    assert confirm_by - book.now == timedelta(minutes=120)


@pytest.mark.parametrize("bad", [
    {"ref": "short"},
    {"items": []},
    {"customer": {"name": "Sam"}},
    {"customer": {"contact": "07700 900123"}},
])
def test_incomplete_orders_are_refused(shop, bad):
    _, _, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    with pytest.raises(HandoffError):
        handoff.submit(page_order(**bad))


# ------------------------------------------------------------ over HTTP


def call(app, method, path, body=None, token=SECRET):
    """One request to the ASGI app; returns (status, JSON body)."""
    sent = []

    async def run():
        data = json.dumps(body).encode() if body is not None else b""
        headers = [(b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": headers, "scheme": "http", "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 1),
                 "http_version": "1.1", "root_path": ""}
        messages = [{"type": "http.request", "body": data, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

    anyio.run(run)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload)


def test_routes_need_the_secret_and_answer_in_json(shop):
    _, book, links, chat, relay, handoff = shop
    server = build_server(book.directory, book=book)
    add_routes(server, handoff)
    app = server.streamable_http_app()

    assert call(app, "GET", f"/handoff/businesses/{FARM}", token=None)[0] == 401
    assert call(app, "GET", f"/handoff/businesses/{FARM}", token="wrong")[0] == 401
    assert call(app, "GET", f"/handoff/businesses/{FARM}") == (200, {"telegram": False})
    assert call(app, "POST", "/handoff/orders", page_order())[0] == 409
    link_owner(links, chat, relay)
    assert call(app, "GET", f"/handoff/businesses/{FARM}") == (200, {"telegram": True})
    status, state = call(app, "POST", "/handoff/orders", page_order())
    assert status == 200 and state["status"] == "pending_confirmation"
    assert call(app, "POST", "/handoff/orders", page_order(ref="bad"))[0] == 400
    refused = page_order(ref="EE55FF66", items=[{"offering_id": "lamb_half", "quantity": 1}])  # not orderable
    assert call(app, "POST", "/handoff/orders", refused)[0] == 422, "refusals never look like success"
    assert call(app, "GET", f"/handoff/orders/{FARM}/AB12CD34") == (200, state)
    assert call(app, "GET", f"/handoff/orders/{FARM}/ZZ99ZZ99")[0] == 404


def test_the_secret_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "state" / "handoff-secret"
    first = load_secret(path)
    assert len(first) == 64 and load_secret(path) == first
    assert path.stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------------ profiles the toolkit writes


def test_profiles_the_toolkit_writes_are_loaded_and_reloaded(tmp_path):
    businesses = tmp_path / "businesses"
    (businesses / "hollowbrook").mkdir(parents=True)
    profile = json.loads((EXAMPLES / "farm-shop.json").read_text())
    target = businesses / "hollowbrook" / "gentifi-profile.json"
    target.write_text(json.dumps(profile))
    directory = Directory.from_folder([tmp_path / "profiles", businesses])
    assert list(directory.profiles) == [FARM]

    changed = []
    directory.listeners.append(changed.append)
    assert directory.refresh() == [], "unchanged files aren't read again"
    profile["business"]["name"] = "Hollowbrook Farm Shop and Café"
    target.write_text(json.dumps(profile))
    os.utime(target, (time.time() + 5, time.time() + 5))
    assert directory.refresh() == [FARM] and changed == [FARM]
    assert directory.get(FARM)["business"]["name"] == "Hollowbrook Farm Shop and Café"

    target.write_text("{not json")
    os.utime(target, (time.time() + 10, time.time() + 10))
    assert directory.refresh() == [], "a broken file is skipped"
    assert directory.get(FARM)["business"]["name"] == "Hollowbrook Farm Shop and Café", "the last good one stays"


# ------------------------------------------------------------ forgetting the customer


def customer_of(book, reference):
    request = book.bookings[reference]["request"]
    return request.get("customer"), request.get("notes")


def test_details_are_kept_while_the_order_is_open_and_forgotten_when_it_ends(shop):
    _, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    reference = handoff.submit(page_order())["reference"]
    relay.step()
    prompt = chat.last(OWNER).message_id
    chat.press(prompt, "Accept")
    relay.step()
    assert customer_of(book, reference)[0] == {"name": "Sam Carter", "phone": "07700 900123"}, "the owner may need them"
    assert "Sam Carter" in chat.messages[prompt].text

    chat.press(prompt, "Done")
    relay.step()
    assert customer_of(book, reference) == (None, None)
    assert "07700" not in chat.messages[prompt].text
    assert "Customer: details removed now this has ended." in chat.messages[prompt].text
    assert handoff.order(FARM, "AB12CD34")["status"] == "confirmed", "the page can still show the answer"


@pytest.mark.parametrize("ending", ["declined", "expired", "cancelled by the customer", "cancelled by the owner"])
def test_details_are_forgotten_however_the_order_ends(shop, ending):
    clock, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    reference = handoff.submit(page_order())["reference"]
    relay.step()
    prompt = chat.last(OWNER).message_id
    if ending == "declined":
        chat.press(prompt, "Decline")
        relay.step()
        chat.press(prompt, "Too busy")
    elif ending == "expired":
        clock.now += timedelta(minutes=121)
        relay.tick()
    elif ending == "cancelled by the customer":
        book.submit({"request_id": "cancel_page_1", "business_id": FARM, "action": "cancel", "reference": reference,
                     "agent": {"name": "Claude"}, "created_at": "2026-10-15T20:10:00+01:00"})
    else:
        chat.press(prompt, "Accept")
        relay.step()
        chat.press(prompt, "Cancel order")
        relay.step()
        chat.press(prompt, "Yes, cancel it")
    relay.step()
    assert customer_of(book, reference) == (None, None), ending
    assert "07700" not in chat.messages[prompt].text
    expected = "expired" if ending == "expired" else "declined" if ending == "declined" else "cancelled"
    assert handoff.order(FARM, "AB12CD34")["status"] == expected, "the page sees how it ended"


def test_details_are_forgotten_after_a_week_at_most(shop):
    clock, book, links, chat, relay, handoff = shop
    link_owner(links, chat, relay)
    reference = handoff.submit(page_order())["reference"]
    relay.step()
    chat.press(chat.last(OWNER).message_id, "Accept")
    relay.step()
    clock.now += timedelta(days=6, hours=23)
    book.expire_overdue()
    assert customer_of(book, reference)[0] is not None, "an accepted order not yet collected keeps them"
    clock.now += timedelta(hours=1)
    book.expire_overdue()
    assert customer_of(book, reference) == (None, None)


def restartable(tmp_path):
    """A bot that can be restarted: the same relay database and the same owner chat each time."""
    clock, db, chat = Clock(), tmp_path / "relay.db", MockChannel()

    def start():
        book = RequestBook(Directory.from_folder(EXAMPLES, clock=clock))
        book.max_sla_minutes = 120
        links = Links(db)
        relay = OwnerRelay(book, chat, links)
        return book, links, relay, Handoff(book, links, SECRET)
    return clock, db, chat, start


def test_open_orders_survive_a_restart_without_saving_the_customer(tmp_path):
    clock, db, chat, start = restartable(tmp_path)
    book, links, relay, handoff = start()
    link_owner(links, chat, relay)
    reference = handoff.submit(page_order())["reference"]
    relay.step()
    mid = chat.last(OWNER).message_id
    assert "Customer: Sam Carter, 07700 900123" in chat.messages[mid].text

    saved = db.read_bytes() + b"".join(p.read_bytes() for p in tmp_path.glob("relay.db-*"))
    for secret in (b"Sam Carter", b"07700 900123", b"Brown eggs"):
        assert secret not in saved, "the customer's details are never saved"

    book, links, relay, handoff = start()  # gentifi-bot restarted
    assert handoff.order(FARM, "AB12CD34")["status"] == "pending_confirmation", "the page still sees it"
    relay.tick()
    relay.step()
    assert "Sam Carter" in chat.messages[mid].text, "nothing rewrote the owner's message without the customer"

    chat.press(mid, "Accept")
    relay.step()
    assert handoff.order(FARM, "AB12CD34")["status"] == "confirmed"
    text = chat.messages[mid].text
    assert "Customer: Sam Carter, 07700 900123" in text and "Note: Collection on 2026-10-17." in text
    assert chat.labels(mid) == ["Done", "Cancel order"]

    chat.press(mid, "Done")
    relay.step()
    assert "Sam Carter" not in chat.messages[mid].text and "details removed" in chat.messages[mid].text
    assert "shown" not in book.bookings[reference], "forgotten again once the order ends"


def test_after_a_restart_orders_leaves_the_customers_message_alone_and_expiry_still_works(tmp_path):
    clock, db, chat, start = restartable(tmp_path)
    book, links, relay, handoff = start()
    link_owner(links, chat, relay)
    handoff.submit(page_order())
    relay.step()
    mid = chat.last(OWNER).message_id

    book, links, relay, handoff = start()
    chat.type(OWNER, "/orders")
    relay.step()
    assert "Sam Carter" in chat.messages[mid].text and chat.labels(mid) == ["Accept", "Decline", "Suggest another time"]
    assert "still open" in chat.last(OWNER).text

    clock.now += timedelta(hours=2, minutes=1)
    relay.tick()
    relay.step()
    assert handoff.order(FARM, "AB12CD34")["status"] == "expired"
    assert "didn't answer in time" in chat.messages[mid].text and "Sam Carter" not in chat.messages[mid].text

    clock.now += timedelta(days=8)
    start()[2].tick()
    assert start()[3].order(FARM, "AB12CD34") is None, "saved orders are deleted after a week"
