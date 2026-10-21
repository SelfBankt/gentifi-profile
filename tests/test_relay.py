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
