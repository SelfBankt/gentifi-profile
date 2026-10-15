import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from gentifi_profile.server import build_server  # noqa: E402
from gentifi_profile.store import Directory  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FARM = "gtf_hollowbrookfarm"
PLUMBER = "gtf_brooksideplumbing"
PUCKLECHURCH = {"near_lat": 51.486, "near_lng": -2.44}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self, iso):
        self.now = datetime.fromisoformat(iso)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock("2026-10-15T20:00:00+01:00")


@pytest.fixture
def server(clock):
    return build_server(Directory.from_folder(EXAMPLES, clock=clock), demo=True)


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    data = result.structured_content
    return data["result"] if set(data) == {"result"} else data


def tour_request(rid, party, created="2026-10-15T20:00:00+01:00"):
    return {
        "request_id": rid, "business_id": FARM, "action": "book",
        "items": [{"offering_id": "farm_tour", "slot_id": "tour_20261017_1000"}],
        "customer": {"name": "Sam", "contact_relay_token": "rly_1", "party_size": party},
        "agent": {"name": "Claude"}, "created_at": created,
    }


@pytest.mark.anyio
async def test_find_nearby_includes_mobile_trades(server):
    async with Client(server) as c:
        found = await call(c, "find_businesses", max_km=5, **PUCKLECHURCH)
        ids = [b["business_id"] for b in found]
        assert ids[0] == FARM  # closest first
        assert PLUMBER in ids  # its 12 km service area reaches here
        assert await call(c, "find_businesses", query="boiler") != []
        assert await call(c, "find_businesses", query="boiler", category="Store") == []


@pytest.mark.anyio
async def test_instant_booking_retry_full_and_cancel(server):
    async with Client(server) as c:
        [slot] = await call(c, "check_availability", business_id=FARM, offering_id="farm_tour")
        assert slot["assessment"]["advice"] == "book_instantly"

        receipt = await call(c, "submit_request", request=tour_request("req_tour_0001", 2))
        assert receipt["status"] == "confirmed"
        assert receipt["total"] == {"amount_minor": 1600, "currency": "GBP"}

        again = await call(c, "submit_request", request=tour_request("req_tour_0001", 2))
        assert again == receipt  # idempotent: no double booking
        [slot] = await call(c, "check_availability", business_id=FARM, offering_id="farm_tour")
        assert slot["remaining"] == 2

        full = await call(c, "submit_request", request=tour_request("req_tour_0002", 3))
        assert full["status"] == "declined" and full["decline_reason"] == "unavailable"

        cancel = {"request_id": "req_tour_0003", "business_id": FARM, "action": "cancel",
                  "reference": receipt["reference"], "agent": {"name": "Claude"},
                  "created_at": "2026-10-15T20:05:00+01:00"}
        assert (await call(c, "submit_request", request=cancel))["status"] == "cancelled"
        [slot] = await call(c, "check_availability", business_id=FARM, offering_id="farm_tour")
        assert slot["remaining"] == 4


@pytest.mark.anyio
async def test_order_waits_for_owner(server):
    order = json.loads((EXAMPLES / "messages" / "order-request.json").read_text())
    async with Client(server) as c:
        receipt = await call(c, "submit_request", request=order)
        assert receipt["status"] == "pending_confirmation"
        assert receipt["confirm_by"] == "2026-10-15T22:00:00+01:00"  # 120-minute SLA
        assert receipt["total"] == {"amount_minor": 2500, "currency": "GBP"}

        confirmed = await call(c, "respond_as_owner", reference=receipt["reference"], accept=True)
        assert confirmed["status"] == "confirmed"

        status = await call(c, "submit_request", request={
            "request_id": "req_status_01", "business_id": FARM, "action": "check_status",
            "reference": receipt["reference"], "agent": {"name": "Claude"},
            "created_at": "2026-10-15T20:30:00+01:00"})
        assert status["status"] == "confirmed"


@pytest.mark.anyio
async def test_too_little_notice_is_declined(server, clock):
    clock.now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)  # 09:00 London, tour at 10:00, 2h notice needed
    async with Client(server) as c:
        [slot] = await call(c, "check_availability", business_id=FARM, offering_id="farm_tour")
        assert slot["assessment"]["advice"] == "warn_user"  # last confirmed 2 days ago
        receipt = await call(c, "submit_request", request=tour_request("req_tour_late", 1))
        assert receipt["status"] == "declined" and receipt["decline_reason"] == "insufficient_notice"


@pytest.mark.anyio
async def test_stale_slot_with_enough_notice_goes_to_owner(server, clock):
    clock.now = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)  # >24h after last confirmation
    async with Client(server) as c:
        receipt = await call(c, "submit_request", request=tour_request("req_tour_stale", 1))
        # The business allows instant booking, but the data is old: ask the owner instead.
        assert receipt["status"] == "pending_confirmation"


@pytest.mark.anyio
async def test_bad_requests_fail_clearly(server):
    async with Client(server) as c:
        bad = tour_request("req_bad_00001", 2)
        del bad["agent"]
        assert (await call(c, "submit_request", request=bad))["status"] == "failed"

        no_address = {"request_id": "req_plumb_001", "business_id": PLUMBER, "action": "book",
                      "items": [{"offering_id": "boiler_service", "slot_id": "svc_20261020_0900"}],
                      "customer": {"name": "Sam", "phone": "+44 7700 900000"},
                      "agent": {"name": "Claude"}, "created_at": "2026-10-15T20:00:00+01:00"}
        receipt = await call(c, "submit_request", request=no_address)
        assert receipt["status"] == "failed" and "'address' is required" in receipt["message"]
