import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gentifi_profile import assess, check_document, load_schema

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 19, 0, tzinfo=timezone.utc)  # 20:00 London


def load(rel):
    return json.loads((ROOT / rel).read_text())


def errors(issues):
    return [i.message for i in issues if i.level == "error"]


def warnings(issues):
    return [i.message for i in issues if i.level == "warning"]


def test_bundled_schema_matches_published_schema():
    assert load("schema/v0.1/profile.schema.json") == load_schema()


@pytest.mark.parametrize("rel", sorted(str(p.relative_to(ROOT)) for p in (ROOT / "examples").rglob("*.json")))
def test_examples_are_clean(rel):
    profile = load("examples/farm-shop.json")
    kind, issues = check_document(load(rel), profile=profile, now=NOW)
    assert kind is not None
    assert issues == []


@pytest.fixture
def farm():
    return copy.deepcopy(load("examples/farm-shop.json"))


def test_unknown_offering_reference(farm):
    farm["availability"][0]["offering_id"] = "duck_eggs"
    _, issues = check_document(farm, now=NOW)
    assert "unknown offering 'duck_eggs'" in errors(issues)


def test_slot_must_end_after_start_and_fit_capacity(farm):
    slot = farm["availability"][3]
    slot["end"], slot["remaining"] = slot["start"], 9
    _, issues = check_document(farm, now=NOW)
    assert "slot ends before it starts" in errors(issues)
    assert "remaining is greater than capacity" in errors(issues)


def test_bookable_offering_needs_duration(farm):
    del farm["offerings"][3]["duration_minutes"]
    _, issues = check_document(farm, now=NOW)
    assert any("duration_minutes" in m for m in errors(issues))


def test_request_to_confirm_needs_sla(farm):
    del farm["actions"][1]["confirmation_sla_minutes"]
    _, issues = check_document(farm, now=NOW)
    assert any("confirmation_sla_minutes" in m for m in errors(issues))


def test_quote_without_enquire_is_warned(farm):
    farm["actions"] = [a for a in farm["actions"] if a["action"] != "enquire"]
    _, issues = check_document(farm, now=NOW)
    assert "'lamb_half' is priced on quote but has no 'enquire' action" in warnings(issues)


def test_stale_data_is_warned(farm):
    later = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
    _, issues = check_document(farm, now=later)
    assert "record is stale; agents will treat it as unreliable" in warnings(issues)


def test_receipt_rules():
    _, issues = check_document({"request_id": "req_12345678", "status": "declined", "issued_at": "2026-10-15T19:00:00Z"})
    assert any("decline_reason" in m for m in errors(issues))


def test_request_checked_against_profile(farm):
    req = load("examples/messages/order-request.json")
    req["items"][0]["variant_id"] = "eggs_30"
    req["fulfilment"] = "on_premises"
    _, issues = check_document(req, profile=farm, now=NOW)
    assert "offering 'eggs' has no variant 'eggs_30'" in errors(issues)
    assert "'eggs' is not available for on_premises" in errors(issues)


# --------------------------------------------------------------- freshness advice

def record(confidence, observed, valid_until=None, **extra):
    f = {"observed_at": observed, "confidence": confidence, "source": "owner"}
    if valid_until:
        f["valid_until"] = valid_until
    return {"type": "stock", "offering_id": "x", "status": "in_stock", "freshness": f, **extra}


def test_confirmed_and_current_books_instantly():
    a = assess(record("confirmed", "2026-10-15T18:00:00Z"), NOW, "instant")
    assert a["advice"] == "book_instantly" and a["age_minutes"] == 60


def test_confirmed_but_business_wants_to_confirm():
    assert assess(record("confirmed", "2026-10-15T18:00:00Z"), NOW, "request_to_confirm")["advice"] == "request_confirmation"


def test_expired_record_warns():
    a = assess(record("confirmed", "2026-10-15T08:00:00Z", "2026-10-15T16:00:00Z"), NOW, "instant")
    assert a["stale"] and a["advice"] == "warn_user"


def test_likely_goes_stale_after_12_hours():
    assert assess(record("likely", "2026-10-15T08:00:00Z"), NOW)["advice"] == "request_confirmation"
    assert assess(record("likely", "2026-10-14T18:00:00Z"), NOW)["advice"] == "warn_user"


def test_patterns_never_promise():
    assert assess(record("typical", "2026-01-01T00:00:00Z"), NOW, "instant")["advice"] == "request_confirmation"


def test_sold_out():
    assert assess(record("confirmed", "2026-10-15T18:00:00Z", status="out_of_stock"), NOW)["advice"] == "not_available"
