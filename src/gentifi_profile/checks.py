"""Validation: JSON Schema first, then the cross-reference and freshness rules
that a schema cannot express (ids that must exist, slots that must make sense,
data that has gone stale)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from .freshness import is_stale, parse_time, utc_now
from .schema_loader import validator_for

SLOT_KINDS = {"bookable_service", "rental", "event"}
PROFILE_STALE_AFTER = timedelta(days=7)


@dataclass
class Issue:
    level: str  # "error" or "warning"
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _path(parts: Iterable[Any]) -> str:
    out = "$"
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def detect_kind(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return None
    if "spec_version" in doc and "business" in doc:
        return "profile"
    if "request_id" in doc and "action" in doc:
        return "request"
    if "request_id" in doc and "status" in doc:
        return "receipt"
    return None


def schema_issues(doc: Any, kind: str) -> list[Issue]:
    validator = validator_for(kind)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [Issue("error", _path(e.absolute_path), _short(e)) for e in errors]


def _short(error) -> str:
    # oneOf failures are unreadable by default; point at the best-matching branch.
    if error.validator == "oneOf" and error.context:
        best = max(error.context, key=lambda e: len(list(e.absolute_path)))
        return f"does not match any allowed shape ({best.message})"
    return error.message


# ---------------------------------------------------------------- profile rules

def profile_issues(profile: dict[str, Any], now: datetime | None = None) -> list[Issue]:
    now = now or utc_now()
    issues: list[Issue] = []
    err = lambda p, m: issues.append(Issue("error", p, m))
    warn = lambda p, m: issues.append(Issue("warning", p, m))

    offerings = {o["id"]: o for o in profile["offerings"]}

    for oid, n in Counter(o["id"] for o in profile["offerings"]).items():
        if n > 1:
            err("$.offerings", f"offering id '{oid}' is used {n} times")
    for i, o in enumerate(profile["offerings"]):
        for vid, n in Counter(v["id"] for v in o.get("variants", [])).items():
            if n > 1:
                err(f"$.offerings[{i}].variants", f"variant id '{vid}' is used {n} times")

    currencies = {m["currency"] for m in _money_values(profile)}
    if len(currencies) > 1:
        warn("$", f"profile mixes currencies: {', '.join(sorted(currencies))}")

    slot_ids = Counter(r["slot_id"] for r in profile["availability"] if r["type"] == "slot")
    for sid, n in slot_ids.items():
        if n > 1:
            err("$.availability", f"slot id '{sid}' is used {n} times")

    for i, rec in enumerate(profile["availability"]):
        p = f"$.availability[{i}]"
        offering = offerings.get(rec["offering_id"])
        if offering is None:
            err(f"{p}.offering_id", f"unknown offering '{rec['offering_id']}'")
            continue
        vid = rec.get("variant_id")
        if vid and vid not in {v["id"] for v in offering.get("variants", [])}:
            err(f"{p}.variant_id", f"offering '{offering['id']}' has no variant '{vid}'")
        if rec["type"] == "slot":
            if offering["kind"] not in SLOT_KINDS:
                warn(p, f"slot given for '{offering['id']}', which is a {offering['kind']}, not a bookable kind")
            start, end = parse_time(rec["start"]), parse_time(rec["end"])
            if end <= start:
                err(f"{p}.end", "slot ends before it starts")
            if "capacity" in rec and rec["remaining"] > rec["capacity"]:
                err(f"{p}.remaining", "remaining is greater than capacity")
        if rec["type"] == "stock" and offering["kind"] in SLOT_KINDS:
            warn(p, f"stock given for '{offering['id']}', which is slot-based ({offering['kind']})")
        f = rec["freshness"]
        if "valid_until" in f and parse_time(f["valid_until"]) < parse_time(f["observed_at"]):
            err(f"{p}.freshness.valid_until", "valid_until is earlier than observed_at")
        elif is_stale(rec, now):
            warn(f"{p}.freshness", "record is stale; agents will treat it as unreliable")

    covered: dict[str, set[str]] = {}
    for i, act in enumerate(profile["actions"]):
        targets = act.get("applies_to") or list(offerings)
        for oid in act.get("applies_to", []):
            if oid not in offerings:
                err(f"$.actions[{i}].applies_to", f"unknown offering '{oid}'")
        for oid in targets:
            if act["action"] in covered.setdefault(oid, set()) and "applies_to" in act:
                warn(f"$.actions[{i}]", f"'{act['action']}' is declared more than once for '{oid}'")
            covered[oid].add(act["action"])

    for oid, o in offerings.items():
        acts = covered.get(oid, set())
        if o["price"]["type"] == "quote" and "enquire" not in acts:
            warn("$.actions", f"'{oid}' is priced on quote but has no 'enquire' action")
        if o["kind"] in SLOT_KINDS and not acts & {"book", "hold"}:
            warn("$.actions", f"'{oid}' is bookable but has no 'book' or 'hold' action")
        if not acts & {"book", "hold", "order", "enquire"}:
            warn("$.actions", f"agents cannot act on '{oid}' (no book, order or enquire action)")

    if now - parse_time(profile["updated_at"]) > PROFILE_STALE_AFTER:
        warn("$.updated_at", "profile has not been updated for over 7 days")
    return issues


def _money_values(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if set(node) == {"amount_minor", "currency"}:
            yield node
        else:
            for v in node.values():
                yield from _money_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _money_values(v)


# ---------------------------------------------------------------- request rules

def action_capability(profile: dict[str, Any], action: str, offering_id: str | None) -> dict[str, Any] | None:
    """The capability that governs this action for this offering, if any.
    A capability naming the offering wins over a catch-all one."""
    specific = general = None
    for cap in profile["actions"]:
        if cap["action"] != action:
            continue
        if "applies_to" not in cap:
            general = general or cap
        elif offering_id in cap["applies_to"]:
            specific = specific or cap
    return specific or general


def request_issues(request: dict[str, Any], profile: dict[str, Any]) -> list[Issue]:
    """Check an ActionRequest against the profile of the business it targets."""
    issues: list[Issue] = []
    err = lambda p, m: issues.append(Issue("error", p, m))

    if request["business_id"] != profile["business"]["id"]:
        err("$.business_id", f"request is for '{request['business_id']}', not '{profile['business']['id']}'")
        return issues

    offerings = {o["id"]: o for o in profile["offerings"]}
    slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
    customer = request.get("customer", {})

    agreed = request.get("customer_confirmation")
    if agreed and parse_time(agreed["confirmed_at"]) > parse_time(request["created_at"]):
        err("$.customer_confirmation.confirmed_at", "the customer must confirm before the request is sent")

    for i, item in enumerate(request.get("items", [])):
        p = f"$.items[{i}]"
        offering = offerings.get(item["offering_id"])
        if offering is None:
            err(f"{p}.offering_id", f"unknown offering '{item['offering_id']}'")
            continue
        cap = action_capability(profile, request["action"], offering["id"])
        if cap is None:
            err(p, f"'{request['action']}' is not offered for '{offering['id']}'")
            continue
        for field in cap.get("required_customer_fields", []):
            present = (
                bool(customer.get("phone") or customer.get("email") or customer.get("contact_relay_token"))
                if field == "contact"
                else bool(request.get("notes")) if field == "notes"
                else field in customer
            )
            if not present:
                err("$.customer", f"'{field}' is required for '{request['action']}' on '{offering['id']}'")
        vid = item.get("variant_id")
        if vid and vid not in {v["id"] for v in offering.get("variants", [])}:
            err(f"{p}.variant_id", f"offering '{offering['id']}' has no variant '{vid}'")
        if offering["kind"] in SLOT_KINDS and request["action"] in {"book", "hold"}:
            sid = item.get("slot_id")
            if sid is None and "requested_time" not in item:
                err(p, "a slot_id or requested_time is needed to book this offering")
            elif sid is not None:
                slot = slots.get(sid)
                if slot is None or slot["offering_id"] != offering["id"]:
                    err(f"{p}.slot_id", f"unknown slot '{sid}' for '{offering['id']}'")
                elif slot["remaining"] < _party(item, customer):
                    err(f"{p}.slot_id", f"slot '{sid}' has only {slot['remaining']} place(s) left")
        fulfilment = request.get("fulfilment")
        if fulfilment and fulfilment not in offering["fulfilment"]:
            err("$.fulfilment", f"'{offering['id']}' is not available for {fulfilment}")
    return issues


def _party(item: dict[str, Any], customer: dict[str, Any]) -> int:
    return int(customer.get("party_size") or item.get("quantity") or 1)


# ---------------------------------------------------------------- entry point

def check_document(doc: Any, kind: str | None = None, *, profile: dict[str, Any] | None = None,
                   now: datetime | None = None) -> tuple[str | None, list[Issue]]:
    """Validate one document. Returns (kind, issues)."""
    kind = kind or detect_kind(doc)
    if kind is None:
        return None, [Issue("error", "$", "not a recognisable profile, request or receipt")]
    issues = schema_issues(doc, kind)
    if issues:  # semantic checks assume a schema-valid document
        return kind, issues
    if kind == "profile":
        issues += profile_issues(doc, now)
    elif kind == "request" and profile is not None:
        issues += request_issues(doc, profile)
    return kind, issues
