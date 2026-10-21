"""Owner confirmation relay, end to end through a mock owner chat and a fake Lightning wallet."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from gentifi_profile.relay import Incoming, Links, MockChannel, OwnerRelay
from gentifi_profile.relay.cli import load_env
from gentifi_profile.relay.payments import PaymentError, bolt11_msat, lnurlp_url, request_invoice
from gentifi_profile.store import Directory, RequestBook

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
OWNER, STRANGER = "1001", "2002"
PRICE = 50_000  # GBP per BTC in the fake price feed


class Clock:
    def __init__(self):
        self.now = datetime.fromisoformat("2026-10-15T20:00:00+01:00")

    def __call__(self):
        return self.now


class Wallet:
    """Fake HTTP: a price feed and one LNURL-pay wallet at shop@wallet.example."""

    def __init__(self, wrong_amount=False, verify=True):
        self.wrong_amount, self.verify, self.settled, self.calls = wrong_amount, verify, False, []

    def __call__(self, url):
        self.calls.append(url)
        if url == "https://mempool.space/api/v1/prices":
            return {"GBP": PRICE, "USD": 65_000}
        if url == "https://wallet.example/.well-known/lnurlp/shop":
            return {"tag": "payRequest", "callback": "https://wallet.example/pay/shop",
                    "minSendable": 1000, "maxSendable": 10**11, "commentAllowed": 50, "metadata": "[]"}
        if url.startswith("https://wallet.example/pay/shop?"):
            msat = int(parse_qs(urlsplit(url).query)["amount"][0])
            if self.wrong_amount:
                msat += 1000
            res = {"pr": f"lnbc{msat // 100}n1pfakeinvoice", "routes": []}
            if self.verify:
                res["verify"] = "https://wallet.example/verify/abc"
            return res
        if url == "https://wallet.example/verify/abc":
            return {"status": "OK", "settled": self.settled, "preimage": None, "pr": "lnbc..."}
        raise PaymentError(f"unexpected url {url}")


@pytest.fixture
def setup():
    def make(lightning=None, link=None, wallet=None):
        clock = Clock()
        book = RequestBook(Directory.from_folder(EXAMPLES, clock=clock))
        links, chat = Links(), MockChannel()
        wallet = wallet or Wallet()
        relay = OwnerRelay(book, chat, links, http=wallet)
        links.configure(FARM, lightning, link)
        chat.type(OWNER, f"/start {links.invite(FARM)}", name="Jo")
        relay.step()
        return clock, book, links, chat, relay, wallet
    return make


def order(rid="req_relay_0001"):
    req = json.loads((EXAMPLES / "messages" / "order-request.json").read_text())
    req["request_id"] = rid
    return req


def place(book, relay, chat, rid="req_relay_0001"):
    receipt = book.submit(order(rid))
    assert receipt["status"] == "pending_confirmation"
    relay.step()
    prompt = chat.last(OWNER)
    return receipt["reference"], prompt.message_id


def status(book, reference, rid="chk_status_1"):
    return book.submit({"request_id": rid, "business_id": FARM, "action": "check_status",
                        "reference": reference, "agent": {"name": "Claude"},
                        "created_at": "2026-10-15T20:30:00+01:00"})


def test_invite_links_the_owner_once(setup):
    _, _, links, chat, relay, _ = setup()
    assert links.merchant(FARM).chat_id == OWNER
    assert "Connected to Hollowbrook Farm Shop" in chat.last(OWNER).text
    code = links.invite(FARM)
    assert links.redeem(code, STRANGER, "X") == FARM
    assert links.redeem(code, "3003", "Y") is None  # one use only


def test_invite_codes_are_letters_and_digits_only():
    links = Links()
    codes = [links.invite(FARM) for _ in range(200)]
    assert all(len(c) == 12 and c.isascii() and c.isalnum() for c in codes), "no _ or - to get lost in a link"
    assert len(set(codes)) == len(codes)


def test_strangers_get_no_access(setup):
    _, book, _, chat, relay, _ = setup()
    chat.type(STRANGER, "/orders")
    relay.step()
    assert "invite link" in chat.last(STRANGER).text
    reference, mid = place(book, relay, chat)
    chat.inbox.append(Incoming("button", STRANGER, f"a:{reference}", mid, ack_id="q"))
    relay.step()
    assert chat.acks[-1][1] == "This isn't one of your requests."
    assert book.bookings[reference]["receipt"]["status"] == "pending_confirmation"


def test_accept_pay_on_collection_then_collected(setup):
    _, book, links, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    prompt = chat.messages[mid]
    assert "2 × Free-range eggs (Dozen)" in prompt.text and "Total £25.00" in prompt.text
    assert "Please answer by 22:00 today" in prompt.text
    assert chat.labels(mid) == ["Accept", "Decline", "Suggest another time"]

    chat.press(mid, "Accept")
    relay.step()
    receipt = status(book, reference)
    assert receipt["status"] == "confirmed"
    assert receipt["payment"] == {"status": "due_on_fulfilment"}
    assert "Pay £25.00 on collection" in receipt["message"]
    assert chat.labels(mid) == ["Collected", "Cancel order"]

    chat.press(mid, "Collected")
    relay.step()
    assert status(book, reference, "chk_status_2")["payment"]["status"] == "paid"
    assert chat.labels(mid) == [] and "Collected. Order closed." in chat.messages[mid].text
    assert links.open_prompts(FARM) == []


def test_accept_with_lightning_invoice_from_the_merchant_wallet(setup):
    _, book, links, chat, relay, wallet = setup(lightning="shop@wallet.example")
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Accept")
    relay.step()
    receipt = status(book, reference)
    # £25.00 at £50,000/BTC = 50,000 sats, invoice from the merchant's own wallet
    assert receipt["payment"]["status"] == "awaiting"
    assert receipt["payment"]["checkout_url"].startswith("lightning:lnbc")
    assert bolt11_msat(receipt["payment"]["checkout_url"]) == 50_000_000
    assert "50,000 sats" in receipt["message"] and "£50,000 per BTC" in receipt["message"]
    assert any(f"comment=Gentifi+{reference}" in c for c in wallet.calls)
    assert chat.labels(mid) == ["Mark paid", "Collected", "Cancel order"]

    relay.tick()  # not paid yet
    assert status(book, reference, "chk_status_2")["payment"]["status"] == "awaiting"
    wallet.settled = True
    relay.tick()  # LUD-21 verify says settled
    assert status(book, reference, "chk_status_3")["payment"]["status"] == "paid"
    assert "Paid by Lightning." in chat.messages[mid].text
    assert links.awaiting_verification() == []


def test_wrong_invoice_amount_falls_back_and_tells_the_owner(setup):
    _, book, _, chat, relay, _ = setup(lightning="shop@wallet.example", wallet=Wallet(wrong_amount=True))
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Accept")
    relay.step()
    assert status(book, reference)["payment"] == {"status": "due_on_fulfilment"}
    assert "couldn't get a Lightning invoice" in chat.last(OWNER).text


def test_payment_link_and_mark_paid(setup):
    _, book, _, chat, relay, _ = setup(link="https://pay.example/hollowbrook")
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Accept")
    relay.step()
    assert status(book, reference)["payment"] == {"status": "awaiting", "checkout_url": "https://pay.example/hollowbrook"}
    chat.press(mid, "Mark paid")
    relay.step()
    assert status(book, reference, "chk_status_2")["payment"]["status"] == "paid"


def test_decline_sold_out_marks_items_sold_out(setup):
    _, book, _, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Decline")
    relay.step()
    assert chat.labels(mid) == ["Sold out", "Too busy", "Other reason", "« Back"]
    chat.press(mid, "Sold out")
    relay.step()
    receipt = status(book, reference)
    assert receipt["status"] == "declined" and "sold out" in receipt["message"]
    eggs = next(r for r in book.directory.get(FARM)["availability"] if r["offering_id"] == "eggs")
    assert eggs["status"] == "out_of_stock" and eggs["freshness"]["source"] == "owner"
    assert chat.labels(mid) == []


def test_soldout_and_instock_commands(setup):
    _, book, _, chat, relay, _ = setup()
    changed = []
    book.directory.listeners.append(changed.append)

    chat.type(OWNER, "/instock")
    relay.step()
    assert chat.last(OWNER).text == "Nothing is marked sold out."

    chat.type(OWNER, "/soldout")
    relay.step()
    menu = chat.last(OWNER).message_id
    assert chat.labels(menu) == ["Free-range eggs", "Seasonal veg box", "Half lamb, butchered", "Done"]
    chat.press(menu, "Free-range eggs")
    relay.step()
    assert chat.acks[-1][1] == "Free-range eggs: sold out"
    assert chat.labels(menu) == ["Seasonal veg box", "Half lamb, butchered", "Done"]
    assert changed == [FARM]
    records = [r for r in book.directory.get(FARM)["availability"] if r["offering_id"] == "eggs"]
    assert len(records) == 2, "a whole-item record covers the half dozen, which had none"
    assert all(r["status"] == "out_of_stock" and r["freshness"]["source"] == "owner" for r in records)

    chat.type(OWNER, "/instock")
    relay.step()
    menu = chat.last(OWNER).message_id
    assert chat.labels(menu) == ["Free-range eggs", "Done"]
    chat.press(menu, "Free-range eggs")
    relay.step()
    assert all(r["status"] == "in_stock" and "quantity" not in r for r in records)
    assert chat.labels(menu) == [] and "Nothing is marked sold out." in chat.messages[menu].text
    chat.type(OWNER, "/soldout")
    relay.step()
    chat.press(chat.last(OWNER).message_id, "Done")
    relay.step()
    assert chat.labels(chat.last(OWNER).message_id) == []

    chat.inbox.append(Incoming("button", STRANGER, "so:eggs", "1", ack_id="q"))
    relay.step()
    assert chat.acks[-1][1] == "This isn't one of your businesses."
    assert all(r["status"] == "in_stock" for r in records)


def test_back_returns_to_the_original_buttons(setup):
    _, book, _, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Decline")
    relay.step()
    chat.press(mid, "« Back")
    relay.step()
    assert chat.labels(mid) == ["Accept", "Decline", "Suggest another time"]


def test_suggest_a_typed_time_goes_back_to_the_agent(setup):
    _, book, _, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Suggest another time")
    relay.step()
    chat.press(mid, "Type a time")
    relay.step()
    chat.type(OWNER, "Saturday 10am")
    relay.step()
    receipt = status(book, reference)
    assert receipt["status"] == "declined" and receipt["decline_reason"] == "other"
    assert "suggests Saturday 10am instead" in receipt["message"]


def test_unanswered_requests_expire_and_the_owner_sees_it(setup):
    clock, book, links, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    clock.now += timedelta(hours=2, minutes=1)
    relay.tick()
    relay.step()
    assert book.bookings[reference]["receipt"]["status"] == "expired"
    assert "didn't answer in time" in chat.messages[mid].text and chat.labels(mid) == []
    chat.inbox.append(Incoming("button", OWNER, f"a:{reference}", mid, ack_id="q"))
    relay.step()
    assert chat.acks[-1][1] == "This request isn't open any more."


def test_customer_cancelling_updates_the_owner(setup):
    _, book, _, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    book.submit({"request_id": "cancel_req_1", "business_id": FARM, "action": "cancel", "reference": reference,
                 "agent": {"name": "Claude"}, "created_at": "2026-10-15T20:10:00+01:00"})
    relay.step()
    assert "The customer cancelled this." in chat.messages[mid].text


def test_owner_cancels_after_accepting(setup):
    _, book, _, chat, relay, _ = setup()
    reference, mid = place(book, relay, chat)
    chat.press(mid, "Accept")
    relay.step()
    chat.press(mid, "Cancel order")
    relay.step()
    chat.press(mid, "Yes, cancel it")
    relay.step()
    assert status(book, reference)["status"] == "cancelled"


def test_pause_hides_the_business_and_declines_requests(setup):
    _, book, _, chat, relay, _ = setup()
    chat.type(OWNER, "/pause")
    relay.step()
    assert all(b["business_id"] != FARM for b in book.directory.find())
    assert book.submit(order())["decline_reason"] == "closed"
    chat.type(OWNER, "/resume")
    relay.step()
    assert book.submit(order("req_relay_0002"))["status"] == "pending_confirmation"


def test_unlinked_business_gets_no_message_and_the_request_expires():
    clock = Clock()
    book = RequestBook(Directory.from_folder(EXAMPLES, clock=clock))
    chat = MockChannel()
    relay = OwnerRelay(book, chat, Links(), http=Wallet())
    reference = book.submit(order())["reference"]
    relay.step()
    assert chat.messages == {}
    clock.now += timedelta(hours=3)
    relay.tick()
    assert book.bookings[reference]["receipt"]["status"] == "expired"


# ------------------------------------------------------------ onboarding


@pytest.fixture
def onboarding(tmp_path):
    folder = tmp_path / "profiles"
    folder.mkdir()
    for path in EXAMPLES.glob("*.json"):
        (folder / path.name).write_text(path.read_text())
    book = RequestBook(Directory.from_folder(folder, clock=Clock()))
    links, chat = Links(), MockChannel()
    code = links.invite(FARM, onboarding=True)
    relay = OwnerRelay(book, chat, links, http=Wallet())
    return folder, book, links, chat, relay, code


def press_last(chat, relay, label):
    chat.press(chat.last(OWNER).message_id, label)
    relay.step()


def test_onboarding_shop_is_hidden_until_the_owner_goes_live(onboarding):
    _, book, links, chat, relay, code = onboarding
    changed = []
    book.directory.listeners.append(changed.append)
    assert all(b["business_id"] != FARM for b in book.directory.find())
    with pytest.raises(KeyError):
        book.directory.info(FARM)
    assert book.submit(order())["status"] == "failed", "agents can't reach a shop that isn't live"

    chat.type(OWNER, f"/start {code}", name="Jo")
    relay.step()
    card = chat.last(OWNER)
    assert card.text.startswith("Hi Jo, I'll get Hollowbrook Farm Shop found by people's AI assistants.")
    assert "Hollowbrook Lane, Pucklechurch, BS16 9XX" in card.text
    assert "Open Thu and Fri 10:00–17:00, Sat 09:00–13:00." in card.text
    assert "4 items, including:\n• Free-range eggs: £3.50 per dozen\n• Seasonal veg box: £18.00\n" in card.text
    assert "Customers can order for collection or delivery." in card.text
    assert chat.labels(card.message_id) == ["Looks right", "Something's wrong"]

    chat.type(OWNER, "/soldout")
    relay.step()
    assert chat.last(OWNER).text.startswith("You're not live yet.")

    checked = chat.last(OWNER).message_id
    press_last(chat, relay, "Looks right")
    assert chat.labels(checked) == [], "the checked card loses its buttons"
    assert chat.labels(chat.last(OWNER).message_id) == ["Add Lightning address", "Add payment link", "Go live"]

    press_last(chat, relay, "Add Lightning address")
    chat.type(OWNER, "shop@wallet.example")
    relay.step()
    assert "Lightning address: shop@wallet.example" in chat.last(OWNER).text
    press_last(chat, relay, "Add payment link")
    chat.type(OWNER, "pay me on the day")
    relay.step()
    assert "doesn't look like a payment link" in chat.last(OWNER).text
    chat.type(OWNER, "https://pay.example/hollowbrook")
    relay.step()
    merchant = links.merchant(FARM)
    assert (merchant.lightning_address, merchant.payment_link) == ("shop@wallet.example", "https://pay.example/hollowbrook")
    assert chat.labels(chat.last(OWNER).message_id) == ["Change Lightning address", "Change payment link", "Go live"]

    press_last(chat, relay, "Go live")
    assert chat.last(OWNER).text.startswith("You're live.")
    assert links.merchant(FARM).status == "live" and FARM not in book.directory.drafts
    assert changed == [FARM], "going live publishes the listing"
    assert book.submit(order("req_relay_0002"))["status"] == "pending_confirmation"
    press_last(chat, relay, "See my listing")
    assert chat.last(OWNER).text.startswith("Hollowbrook Farm Shop\n")


def test_something_wrong_goes_to_gentifi_and_the_fixed_listing_comes_back(onboarding):
    folder, book, links, chat, relay, code = onboarding
    chat.type(OWNER, f"/start {code}", name="Jo")
    relay.step()
    press_last(chat, relay, "Something's wrong")
    chat.type(OWNER, "Eggs are £3.80 a dozen now")
    relay.step()
    assert chat.last(OWNER).text.startswith("Thanks. Gentifi will make the change")
    assert links.merchant(FARM).change_request == "Eggs are £3.80 a dozen now"

    profile = json.loads((folder / "farm-shop.json").read_text())
    profile["offerings"][0]["price"]["amount"]["amount_minor"] = 380
    (folder / "farm-shop.json").write_text(json.dumps(profile))
    links.request_review(FARM)
    relay.tick()
    card = chat.last(OWNER)
    assert card.text.startswith("Gentifi has updated your listing. Please check it again:")
    assert "Free-range eggs: £3.80 per dozen" in card.text
    assert links.merchant(FARM).change_request is None
    assert FARM in book.directory.drafts, "still hidden until the owner goes live"

    press_last(chat, relay, "Looks right")
    press_last(chat, relay, "Add Lightning address")
    chat.type(OWNER, "nobody@nowhere.example")
    relay.step()
    assert chat.last(OWNER).text.startswith("That address didn't work")
    assert links.merchant(FARM).lightning_address is None


def test_the_gentifi_team_hears_about_change_requests_and_go_lives(onboarding):
    _, book, links, chat, relay, code = onboarding
    TEAM = "3003"
    team_code = links.team_invite()
    assert links.redeem(team_code, STRANGER, None) is None, "a team link can't take over a business"
    chat.type(TEAM, f"/start {team_code}", name="Finn")
    relay.step()
    assert chat.last(TEAM).text.startswith("You're on the Gentifi team.")
    assert not links.redeem_team(team_code, STRANGER, None), "works once"
    assert not links.redeem_team(code, STRANGER, None), "an owner's link doesn't join the team"

    chat.type(OWNER, f"/start {code}", name="Jo")
    relay.step()
    assert chat.last(TEAM).text == "Hollowbrook Farm Shop: Jo connected and is checking their listing."
    press_last(chat, relay, "Something's wrong")
    chat.type(OWNER, "We're open Wednesdays too")
    relay.step()
    alert = chat.last(TEAM).text
    assert alert.startswith("Hollowbrook Farm Shop asked for a change:") and "We're open Wednesdays too" in alert
    assert alert.endswith("gentifi-relay review gtf_hollowbrookfarm")

    links.request_review(FARM)
    relay.tick()
    press_last(chat, relay, "Looks right")
    press_last(chat, relay, "Go live")
    assert chat.last(TEAM).text == "Hollowbrook Farm Shop is live."


def test_links_from_before_onboarding_are_upgraded_and_stay_live(tmp_path):
    import sqlite3

    db = tmp_path / "relay.db"
    old = sqlite3.connect(db)
    old.executescript("CREATE TABLE merchants (business_id TEXT PRIMARY KEY, chat_id TEXT UNIQUE, owner_name TEXT, "
                      "lightning_address TEXT, payment_link TEXT, linked_at TEXT);"
                      "CREATE TABLE invites (code TEXT PRIMARY KEY, business_id TEXT NOT NULL, "
                      "created_at TEXT NOT NULL, used_at TEXT);"
                      "INSERT INTO merchants (business_id, chat_id) VALUES ('gtf_hollowbrookfarm', '1001');"
                      "INSERT INTO invites VALUES ('AbC123dEf456', 'gtf_hollowbrookfarm', '2026-10-20', NULL);")
    old.commit()
    old.close()
    links = Links(db)
    merchant = links.merchant(FARM)
    assert merchant.chat_id == OWNER and merchant.status == "live" and not merchant.onboarding
    assert links.redeem("AbC123dEf456", "4004", None) == FARM, "an unused invite from before still works"


# ------------------------------------------------------------ payments


@pytest.mark.parametrize("invoice,msat", [
    ("lnbc500u1pfake", 50_000_000),
    ("lnbc2500n1pfake", 250_000),
    ("LIGHTNING:lnbc1m1pfake", 100_000_000),
    ("lnbc10p1pfake", 1),
    ("lntb20u1pfake", 2_000_000),
    ("lnbc1pfake", None),
])
def test_bolt11_amounts(invoice, msat):
    assert bolt11_msat(invoice) == msat


def test_lightning_address_rules():
    assert lnurlp_url("Shop@Wallet.Example") == "https://wallet.example/.well-known/lnurlp/Shop"
    with pytest.raises(PaymentError):
        lnurlp_url("https://evil.example/")


def test_invoice_outside_wallet_limits_is_refused():
    with pytest.raises(PaymentError, match="outside"):
        request_invoice("shop@wallet.example", 10**9, "GBP", "x", Wallet())


def test_env_file_is_found_in_a_parent_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("GENTIFI_TEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text("# secret\nGENTIFI_TEST_TOKEN=123:abc\n")
    inner = tmp_path / "gentifi-profile"
    inner.mkdir()
    assert load_env(inner) == tmp_path / ".env"
    import os
    assert os.environ["GENTIFI_TEST_TOKEN"] == "123:abc"


def test_the_onboarding_list_is_kept_for_the_toolkits_host(tmp_path):
    db = tmp_path / "relay.db"

    def listed():
        return json.loads((tmp_path / "onboarding.json").read_text())["onboarding"]

    links = Links(db)
    assert listed() == [], "written as soon as the database is opened"
    links.invite(FARM, onboarding=True)
    links.invite("gtf_brooksideplumbing")
    assert listed() == [FARM]
    assert listed() == Links(db).onboarding(), "another process (gentifi-bot) sees the same"
    links.set_status(FARM, "live")
    assert listed() == []
    assert not list(tmp_path.glob("*.tmp")), "replaced whole, never left half-written"


def _stock(book, offering_id):
    return {r["status"] for r in book.directory.get(FARM)["availability"]
            if r["type"] == "stock" and r["offering_id"] == offering_id}


def test_sold_out_is_kept_through_reloads_and_restarts_and_written_for_the_host(tmp_path):
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "farm-shop.json").write_text((EXAMPLES / "farm-shop.json").read_text())
    db = tmp_path / "relay.db"
    clock = Clock()

    def start():
        book = RequestBook(Directory.from_folder(folder, clock=clock))
        links, chat = Links(db), MockChannel()
        relay = OwnerRelay(book, chat, links)
        return book, links, chat, relay

    book, links, chat, relay = start()
    chat.type(OWNER, f"/start {links.invite(FARM)}", name="Jo")
    relay.step()
    chat.inbox.append(Incoming("button", OWNER, "so:eggs", "1", ack_id="q"))
    relay.step()
    assert _stock(book, "eggs") == {"out_of_stock"}
    marks = json.loads((tmp_path / "stock.json").read_text())["stock"]
    assert marks == {FARM: {"eggs": {"status": "out_of_stock", "at": "2026-10-15T20:00:00+01:00"}}}

    book.directory.reload(FARM)
    assert _stock(book, "eggs") == {"out_of_stock"}, "editing the profile doesn't undo the owner's word"

    book, links, chat, relay = start()  # gentifi-bot restarted
    assert _stock(book, "eggs") == {"out_of_stock"}, "nor does a restart"

    chat.inbox.append(Incoming("button", OWNER, "si:eggs", "2", ack_id="r"))
    relay.step()
    assert _stock(book, "eggs") == {"in_stock"}
    assert json.loads((tmp_path / "stock.json").read_text())["stock"][FARM]["eggs"]["status"] == "in_stock"


def test_a_newer_sheet_wins_over_an_older_mark(tmp_path):
    folder = tmp_path / "profiles"
    folder.mkdir()
    profile = json.loads((EXAMPLES / "farm-shop.json").read_text())
    (folder / "farm-shop.json").write_text(json.dumps(profile))
    book = RequestBook(Directory.from_folder(folder, clock=Clock()))
    links = Links(tmp_path / "relay.db")
    OwnerRelay(book, MockChannel(), links)
    book.mark_sold_out(FARM, {"eggs", "lamb_half"})

    # The owner updates their sheet afterwards: the eggs are back, with the time it was read
    for rec in profile["availability"]:
        if rec.get("offering_id") == "eggs":
            rec["freshness"]["observed_at"] = "2026-10-15T21:00:00+01:00"
    (folder / "farm-shop.json").write_text(json.dumps(profile))
    book.directory.reload(FARM)
    assert _stock(book, "eggs") == {"in_stock"}, "the sheet changed after the mark"
    assert _stock(book, "lamb_half") == {"out_of_stock"}, "the lamb's record is older than the mark"
    assert set(links.stock_marks()[FARM]) == {"lamb_half"}, "the outdated mark is dropped"

    profile["updated_at"] = "2026-10-15T21:30:00+01:00"  # the sheet read again: the toolkit's host drops every older mark
    (folder / "farm-shop.json").write_text(json.dumps(profile))
    book.directory.reload(FARM)
    assert _stock(book, "lamb_half") == {"low"}, "the bot drops it too, so both agree"
    assert links.stock_marks() == {}


def test_an_invite_before_the_folder_is_copied_tells_nobody(tmp_path):
    book = RequestBook(Directory.from_folder(tmp_path))  # the business isn't there yet
    links, chat = Links(), MockChannel()
    chat.type("3003", f"/start {links.team_invite()}", name="Finn")
    relay = OwnerRelay(book, chat, links)
    relay.step()
    before = len(chat.messages)
    links.invite("gtf_newbakery01", onboarding=True)
    relay.tick()
    assert len(chat.messages) == before, "no owner yet, nothing to report"
