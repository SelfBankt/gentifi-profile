"""Turn an availability record's freshness metadata into advice for an agent.

This is the behaviour the spec exists to enable: an agent should be able to tell
whether to book straight away, ask the business to confirm first, or warn the user
that the information may be out of date.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# How long a record stays fresh when it has no explicit valid_until.
DEFAULT_MAX_AGE = {
    "confirmed": timedelta(hours=24),
    "likely": timedelta(hours=12),
}

BOOK_INSTANTLY = "book_instantly"
REQUEST_CONFIRMATION = "request_confirmation"
WARN_USER = "warn_user"
NOT_AVAILABLE = "not_available"

UNAVAILABLE_STATUSES = {"out_of_stock", "out_of_season"}


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_stale(record: dict[str, Any], now: datetime) -> bool:
    freshness = record["freshness"]
    if "valid_until" in freshness:
        return now > parse_time(freshness["valid_until"])
    max_age = DEFAULT_MAX_AGE.get(freshness["confidence"])
    if max_age is None:  # typical patterns don't expire; unknown is never fresh
        return freshness["confidence"] == "unknown"
    return now - parse_time(freshness["observed_at"]) > max_age


def assess(record: dict[str, Any], now: datetime | None = None, action_mode: str | None = None) -> dict[str, Any]:
    """Return {'stale', 'age_minutes', 'advice', 'reason'} for one availability record.

    action_mode is the mode of the action the agent intends to use ('instant' or
    'request_to_confirm'); instant booking is only advised when the business allows it.
    """
    now = now or utc_now()
    freshness = record["freshness"]
    age = now - parse_time(freshness["observed_at"])
    stale = is_stale(record, now)
    confidence = freshness["confidence"]

    if record.get("type") == "stock" and record.get("status") in UNAVAILABLE_STATUSES:
        advice, reason = NOT_AVAILABLE, f"marked {record['status']}"
    elif record.get("type") == "slot" and record.get("remaining", 1) == 0:
        advice, reason = NOT_AVAILABLE, "slot is full"
    elif record.get("type") == "slot" and parse_time(record["start"]) <= now:
        advice, reason = NOT_AVAILABLE, "slot has already started"
    elif stale or confidence == "unknown":
        advice, reason = WARN_USER, "information may be out of date; confirm with the business"
    elif confidence == "typical" or record.get("type") == "pattern":
        advice, reason = REQUEST_CONFIRMATION, "recurring pattern, not a promise"
    elif confidence == "likely":
        advice, reason = REQUEST_CONFIRMATION, "recent but not verified"
    elif action_mode == "request_to_confirm":
        advice, reason = REQUEST_CONFIRMATION, "business confirms requests itself"
    else:
        advice, reason = BOOK_INSTANTLY, "confirmed and current"

    return {
        "stale": stale,
        "age_minutes": max(0, int(age.total_seconds() // 60)),
        "advice": advice,
        "reason": reason,
    }
