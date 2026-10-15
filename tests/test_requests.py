"""Request handling edge cases: overbooking, retried ids and references."""

from datetime import datetime
from pathlib import Path

from gentifi_profile.store import Directory, RequestBook

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
# A day later the tour slot's availability is stale, so bookings wait for the owner
LATER = datetime.fromisoformat("2026-10-16T21:00:00+01:00")


def book_tour(book, rid, party):
    return book.submit({
        "request_id": rid, "business_id": FARM, "action": "book",
        "items": [{"offering_id": "farm_tour", "slot_id": "tour_20261017_1000"}],
        "customer": {"name": "Sam", "contact_relay_token": "rly_1", "party_size": party},
        "customer_confirmation": {"method": "in_app", "confirmed_at": "2026-10-15T19:59:00+01:00"},
        "agent": {"name": "Claude"}, "created_at": "2026-10-16T21:00:00+01:00",
    })


def new_book():
    return RequestBook(Directory.from_folder(EXAMPLES, clock=lambda: LATER))


def test_owner_cannot_confirm_more_places_than_are_left():
    book = new_book()
    first, second = book_tour(book, "req_first_one", 4), book_tour(book, "req_second_one", 4)
    assert first["status"] == second["status"] == "pending_confirmation"
    assert book.owner_respond(first["reference"], True)["status"] == "confirmed"
    late = book.owner_respond(second["reference"], True)
    assert late["status"] == "declined"
    assert late["decline_reason"] == "unavailable"
    assert "0 place(s) left" in late["message"]
    slot = next(r for r in book.directory.get(FARM)["availability"] if r.get("slot_id") == "tour_20261017_1000")
    assert slot["remaining"] == 0


def test_a_reused_request_id_with_a_different_request_is_refused_not_answered():
    book = new_book()
    original = book_tour(book, "req_same_id_1", 2)
    assert book_tour(book, "req_same_id_1", 2) == original, "a true retry gets the original receipt"
    changed = book_tour(book, "req_same_id_1", 3)
    assert changed["status"] == "failed"
    assert "already used for a different request" in changed["message"]


def test_references_are_random_not_derived_from_the_request_id():
    a, b = new_book(), new_book()
    assert book_tour(a, "req_same_id_2", 1)["reference"] != book_tour(b, "req_same_id_2", 1)["reference"]


def test_book_hold_and_order_need_the_customers_confirmation():
    book = new_book()
    request = {
        "request_id": "req_no_consent", "business_id": FARM, "action": "book",
        "items": [{"offering_id": "farm_tour", "slot_id": "tour_20261017_1000"}],
        "customer": {"name": "Sam", "contact_relay_token": "rly_1", "party_size": 1},
        "agent": {"name": "Claude"}, "created_at": "2026-10-16T21:00:00+01:00",
    }
    missing = book.submit(request)
    assert missing["status"] == "failed"
    assert "customer_confirmation" in missing["message"]
    after = book.submit({**request, "request_id": "req_late_consent",
                         "customer_confirmation": {"method": "in_app", "confirmed_at": "2026-10-16T21:05:00+01:00"}})
    assert after["status"] == "failed"
    assert "must confirm before the request is sent" in after["message"]
    page = book.submit({**request, "request_id": "req_page_noref",
                        "customer_confirmation": {"method": "confirmation_page", "confirmed_at": "2026-10-16T20:59:00+01:00"}})
    assert page["status"] == "failed", "a confirmation page must give its reference"
    enquiry = book.submit({"request_id": "req_enquiry_01", "business_id": FARM, "action": "enquire",
                           "items": [{"offering_id": "lamb_half"}], "customer": {"name": "Sam", "contact_relay_token": "rly_1"},
                           "notes": "Half a lamb for December?", "agent": {"name": "Claude"}, "created_at": "2026-10-16T21:00:00+01:00"})
    assert enquiry["status"] != "failed" or "customer_confirmation" not in enquiry.get("message", ""), "enquiries commit no one"
