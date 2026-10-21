"""In-memory directory of profiles and a reference request handler.

This is a reference implementation for trying the spec end to end. It keeps
everything in memory and does not contact businesses: a request that needs the
owner's confirmation stays pending until something calls `owner_respond`
(the reference server exposes that only in --demo mode). A production service
would put the owner-confirmation relay (WhatsApp, SMS, email) there instead.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .checks import SLOT_KINDS, action_capability, check_document, request_issues, schema_issues
from .freshness import BOOK_INSTANTLY, assess, parse_time, utc_now

DEFAULT_SLA_MINUTES = 60


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _km(a: dict[str, float], b: dict[str, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Directory:
    """Profiles loaded from JSON files, keyed by business id."""

    def __init__(self, profiles: list[dict[str, Any]] | None = None, clock: Callable[[], datetime] = utc_now):
        self.clock = clock
        self.profiles: dict[str, dict[str, Any]] = {}
        # Businesses the owner has paused (holiday, sold out for the day): hidden from
        # search and declining new requests until resumed.
        self.paused: set[str] = set()
        for p in profiles or []:
            self.add(p)

    @classmethod
    def from_folder(cls, folder: Path, clock: Callable[[], datetime] = utc_now) -> "Directory":
        directory = cls(clock=clock)
        for path in sorted(Path(folder).glob("*.json")):
            try:
                doc = json.loads(path.read_text("utf-8"))
                directory.add(doc)
            except (ValueError, json.JSONDecodeError) as exc:
                print(f"gentifi: skipping {path.name}: {exc}", file=sys.stderr)
        return directory

    def add(self, profile: dict[str, Any]) -> None:
        kind, issues = check_document(profile, "profile", now=self.clock())
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("invalid profile: " + "; ".join(f"{i.path}: {i.message}" for i in errors[:3]))
        # Work on a copy: bookings change availability in memory.
        self.profiles[profile["business"]["id"]] = copy.deepcopy(profile)

    def get(self, business_id: str) -> dict[str, Any]:
        try:
            return self.profiles[business_id]
        except KeyError:
            raise KeyError(f"no business with id '{business_id}'") from None

    # ------------------------------------------------------------ read tools

    def find(self, query: str = "", category: str | None = None, near: dict[str, float] | None = None,
             max_km: float | None = None, limit: int = 10) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if w]
        results = []
        for p in self.profiles.values():
            b = p["business"]
            if b["id"] in self.paused:
                continue
            if category and category.lower() not in {c.lower() for c in b["categories"] + b.get("tags", [])}:
                continue
            text = " ".join([b["name"], b.get("description", ""), *b["categories"], *b.get("tags", []),
                             *(o["name"] + " " + o.get("description", "") for o in p["offerings"])]).lower()
            if words and not all(w in text for w in words):
                continue
            distance = None
            point = b["location"].get("geo") or b["location"].get("service_area", {}).get("centre")
            if near and point:
                distance = round(_km(near, point), 1)
                area = b["location"].get("service_area")
                reach = area["radius_km"] if area and area["type"] == "radius" else 0
                if max_km is not None and distance - reach > max_km:
                    continue
            results.append({
                "business_id": b["id"],
                "name": b["name"],
                "categories": b["categories"],
                "locality": b["location"].get("address", {}).get("locality"),
                "verification": b["verification"]["status"],
                "distance_km": distance,
                "offerings": [o["name"] for o in p["offerings"]],
            })
        results.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0, r["name"]))
        return results[:limit]

    def info(self, business_id: str) -> dict[str, Any]:
        p = self.get(business_id)
        return {"business": p["business"], "policies": p["policies"], "actions": p["actions"],
                "updated_at": p["updated_at"]}

    def catalogue(self, business_id: str, query: str = "") -> list[dict[str, Any]]:
        p = self.get(business_id)
        words = [w for w in query.lower().split() if w]
        out = []
        for o in p["offerings"]:
            text = " ".join([o["name"], o.get("description", ""), *o.get("tags", []),
                             *map(str, o.get("attributes", {}).keys())]).lower()
            if not words or all(w in text for w in words):
                out.append({**o, "actions": sorted({c["action"] for c in p["actions"]
                                                    if "applies_to" not in c or o["id"] in c["applies_to"]})})
        return out

    def availability(self, business_id: str, offering_id: str | None = None) -> list[dict[str, Any]]:
        """Availability records annotated with freshness advice for agents."""
        p = self.get(business_id)
        offerings = {o["id"]: o for o in p["offerings"]}
        now = self.clock()
        out = []
        for rec in p["availability"]:
            if offering_id and rec["offering_id"] != offering_id:
                continue
            kind = offerings[rec["offering_id"]]["kind"]
            primary = "book" if kind in SLOT_KINDS else "order"
            cap = (action_capability(p, primary, rec["offering_id"])
                   or action_capability(p, "enquire", rec["offering_id"]))
            out.append({**rec, "assessment": assess(rec, now, cap["mode"] if cap else None),
                        "action": ({"action": cap["action"], "mode": cap["mode"]} if cap else None)})
        return out


class RequestBook:
    """Handles ActionRequests against a Directory and remembers the outcome."""

    def __init__(self, directory: Directory):
        self.directory = directory
        # (business_id, request_id) -> receipt. Keys are per business, so one agent's
        # request_id can never return another business's receipt.
        self.by_request: dict[tuple[str, str], dict[str, Any]] = {}
        self.fingerprints: dict[tuple[str, str], str] = {}
        self.bookings: dict[str, dict[str, Any]] = {}     # reference -> {request, receipt}
        # The owner relay and the MCP server may call in from different threads.
        self.lock = threading.RLock()
        # Called as listener(event, reference) after a request changes state. Events:
        # pending, confirmed, declined, expired, cancelled, payment. Keep listeners quick
        # (queue the work): they run while the book is locked.
        self.listeners: list[Callable[[str, str], None]] = []

    @property
    def now(self) -> datetime:
        return self.directory.clock()

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._submit(request)

    def _emit(self, event: str, reference: str) -> None:
        for listener in self.listeners:
            try:
                listener(event, reference)
            except Exception as exc:  # a broken listener must never lose a request
                print(f"gentifi: listener failed on {event} {reference}: {exc}", file=sys.stderr)

    def _submit(self, request: dict[str, Any]) -> dict[str, Any]:
        issues = schema_issues(request, "request")
        if issues:
            return self._receipt(request.get("request_id", "unknown"), "failed",
                                 message="Invalid request: " + "; ".join(f"{i.path}: {i.message}" for i in issues[:3]))
        rid = request["request_id"]
        key = (request["business_id"], rid)
        fingerprint = _fingerprint(request)
        if key in self.by_request:  # idempotency: a retry gets the same answer
            if self.fingerprints[key] != fingerprint:
                return self._receipt(rid, "failed", message=f"request_id '{rid}' was already used for a different request; use a new request_id")
            return self.by_request[key]
        try:
            profile = self.directory.get(request["business_id"])
        except KeyError as exc:
            return self._remember(key, fingerprint, self._receipt(rid, "failed", message=str(exc.args[0])))

        if request["action"] in {"cancel", "check_status"}:
            self.expire_overdue()  # so a late status check never reports a dead request as pending
            return self._remember(key, fingerprint, self._existing(request, profile))
        return self._remember(key, fingerprint, self._new(request, profile))

    # ------------------------------------------------------------ actions

    def _new(self, request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        rid = request["request_id"]
        if profile["business"]["id"] in self.directory.paused:
            return self._receipt(rid, "declined", decline_reason="closed",
                                 message=f"{profile['business']['name']} isn't taking requests right now.")
        problems = request_issues(request, profile)
        full = [i for i in problems if "place(s) left" in i.message]
        if full:
            return self._receipt(rid, "declined", decline_reason="unavailable", message=full[0].message,
                                 alternatives=self._alternatives(request, profile))
        if problems:
            return self._receipt(rid, "failed", message="; ".join(f"{i.path}: {i.message}" for i in problems[:3]))

        slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
        lead = timedelta(minutes=profile["policies"].get("min_lead_time_minutes", 0))
        instant, sla = True, 0
        for item in request["items"]:
            cap = action_capability(profile, request["action"], item["offering_id"])
            slot = slots.get(item.get("slot_id"))
            if slot is not None:
                if parse_time(slot["start"]) - self.now < lead:
                    return self._receipt(rid, "declined", decline_reason="insufficient_notice",
                                         message=f"This business needs at least {int(lead.total_seconds() // 60)} minutes' notice.",
                                         alternatives=self._alternatives(request, profile))
                advice = assess(slot, self.now, cap["mode"])["advice"]
                if advice != BOOK_INSTANTLY:
                    instant = False
            elif cap["mode"] == "instant":
                # No live record to book against: the business has to confirm.
                instant = False
            if cap["mode"] != "instant":
                instant = False
            sla = max(sla, cap.get("confirmation_sla_minutes", DEFAULT_SLA_MINUTES))

        # Random, not derived from request_id: whoever holds a reference can cancel the booking
        reference = "GTF-" + secrets.token_hex(4).upper()
        while reference in self.bookings:
            reference = "GTF-" + secrets.token_hex(4).upper()
        total = self._total(request, profile)
        name = profile["business"]["name"]
        if instant:
            self._take_places(request, slots)
            if request["action"] == "hold":
                cap = action_capability(profile, "hold", request["items"][0]["offering_id"])
                receipt = self._receipt(rid, "held", reference=reference, total=total,
                                        hold_expires_at=_iso(self.now + timedelta(minutes=cap["hold_duration_minutes"])),
                                        message=f"Held with {name}.")
            else:
                receipt = self._receipt(rid, "confirmed", reference=reference, total=total,
                                        message=f"Confirmed with {name}.")
        else:
            receipt = self._receipt(rid, "pending_confirmation", reference=reference, total=total,
                                    confirm_by=_iso(self.now + timedelta(minutes=sla or DEFAULT_SLA_MINUTES)),
                                    message=f"Sent to {name}. They will confirm within {sla or DEFAULT_SLA_MINUTES} minutes.")
        self.bookings[reference] = {"request": request, "receipt": receipt, "business_id": profile["business"]["id"]}
        if receipt["status"] == "pending_confirmation":
            self._emit("pending", reference)
        return receipt

    def _existing(self, request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        rid = request["request_id"]
        booking = self.bookings.get(request["reference"])
        if booking is None or booking["business_id"] != profile["business"]["id"]:
            return self._receipt(rid, "failed", message=f"no booking with reference '{request['reference']}'")
        current = booking["receipt"]
        if request["action"] == "check_status":
            return {**current, "request_id": rid, "issued_at": _iso(self.now)}
        if current["status"] in {"cancelled", "declined", "expired", "failed"}:
            return self._receipt(rid, "failed", reference=request["reference"],
                                 message=f"booking is already {current['status']}")
        if current["status"] in {"confirmed", "held"}:
            slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
            self._take_places(booking["request"], slots, release=True)
        receipt = self._receipt(rid, "cancelled", reference=request["reference"], message="Cancelled.")
        booking["receipt"] = {**receipt, "request_id": booking["receipt"]["request_id"]}
        self._emit("cancelled", request["reference"])
        return receipt

    def owner_respond(self, reference: str, accept: bool, message: str | None = None) -> dict[str, Any]:
        """Resolve a pending request as the business owner (owner relay, or demo/testing)."""
        with self.lock:
            receipt = self._owner_respond(reference, accept, message)
            self._emit(receipt["status"], reference)
            return receipt

    def owner_suggest(self, reference: str, when: str) -> dict[str, Any]:
        """The owner can't do the time asked but suggests another. The request is declined
        with the suggestion: the agent checks with the customer and sends a new request,
        so the customer still confirms exactly what is booked."""
        with self.lock:
            name = self.directory.get(self._pending(reference)["business_id"])["business"]["name"]
            msg = (f"{name} can't do the time asked but suggests {when} instead. "
                   "If that suits the customer, send a new request for that time.")
            receipt = self._owner_respond(reference, False, msg, decline_reason="other")
            self._emit("declined", reference)
            return receipt

    def expire_overdue(self) -> list[str]:
        """Expire pending requests the owner didn't answer by confirm_by. Returns references."""
        expired = []
        with self.lock:
            for reference, booking in self.bookings.items():
                current = booking["receipt"]
                if current["status"] == "pending_confirmation" and parse_time(current["confirm_by"]) <= self.now:
                    receipt = self._receipt(current["request_id"], "expired", reference=reference,
                                            message="The business didn't confirm in time, so nothing was ordered or booked.")
                    self._settle(booking, receipt)
                    expired.append(reference)
                    self._emit("expired", reference)
        return expired

    def set_payment(self, reference: str, payment: dict[str, Any], message: str | None = None) -> dict[str, Any]:
        """Attach payment details (or a paid status) to a confirmed request's receipt.
        Gentifi only relays the business's own payment request; it never holds funds."""
        with self.lock:
            booking = self.bookings.get(reference)
            if booking is None or booking["receipt"]["status"] not in {"confirmed", "held"}:
                raise ValueError(f"no confirmed request with reference '{reference}'")
            receipt = {**booking["receipt"], "payment": payment, "issued_at": _iso(self.now)}
            if message:
                receipt["message"] = message
            self._settle(booking, receipt)
            self._emit("payment", reference)
            return receipt

    def owner_cancel(self, reference: str, message: str | None = None) -> dict[str, Any]:
        """The owner cancels a request they had confirmed (e.g. ran out after all)."""
        with self.lock:
            booking = self.bookings.get(reference)
            if booking is None or booking["receipt"]["status"] not in {"confirmed", "held"}:
                raise ValueError(f"no confirmed request with reference '{reference}'")
            profile = self.directory.get(booking["business_id"])
            slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
            self._take_places(booking["request"], slots, release=True)
            receipt = self._receipt(booking["receipt"]["request_id"], "cancelled", reference=reference,
                                    message=message or f"Cancelled by {profile['business']['name']}.")
            self._settle(booking, receipt)
            self._emit("cancelled", reference)
            return receipt

    def mark_sold_out(self, business_id: str, offering_ids: set[str]) -> None:
        """Record the owner saying these items are sold out (fresh, owner-confirmed)."""
        with self.lock:
            stamp = {"observed_at": _iso(self.now), "confidence": "confirmed", "source": "owner"}
            for rec in self.directory.get(business_id)["availability"]:
                if rec["type"] == "stock" and rec["offering_id"] in offering_ids:
                    rec["status"] = "out_of_stock"
                    rec.pop("quantity", None)
                    rec["freshness"] = dict(stamp)

    def _pending(self, reference: str) -> dict[str, Any]:
        booking = self.bookings.get(reference)
        if booking is None or booking["receipt"]["status"] != "pending_confirmation":
            raise ValueError(f"no pending request with reference '{reference}'")
        return booking

    def _settle(self, booking: dict[str, Any], receipt: dict[str, Any]) -> None:
        booking["receipt"] = receipt
        self.by_request[(booking["business_id"], receipt["request_id"])] = receipt

    def _owner_respond(self, reference: str, accept: bool, message: str | None = None,
                       decline_reason: str = "unavailable") -> dict[str, Any]:
        booking = self._pending(reference)
        profile = self.directory.get(booking["business_id"])
        original = booking["receipt"]
        slots = {r["slot_id"]: r for r in profile["availability"] if r["type"] == "slot"}
        short = self._shortfall(booking["request"], slots) if accept else None
        if short:
            # Places went to someone else while this request was pending: never overbook
            receipt = self._receipt(original["request_id"], "declined", reference=reference,
                                    decline_reason="unavailable", message=short,
                                    alternatives=self._alternatives(booking["request"], profile))
        elif accept:
            self._take_places(booking["request"], slots)
            receipt = self._receipt(original["request_id"], "confirmed", reference=reference,
                                    total=original.get("total"), message=message or "Confirmed by the business.")
        else:
            receipt = self._receipt(original["request_id"], "declined", reference=reference,
                                    decline_reason=decline_reason, message=message or "Declined by the business.")
        self._settle(booking, receipt)
        return receipt

    # ------------------------------------------------------------ helpers

    def _shortfall(self, request: dict[str, Any], slots: dict[str, Any]) -> str | None:
        """Why the request no longer fits its slots, or None when it does."""
        party = int(request.get("customer", {}).get("party_size") or 0)
        for item in request["items"]:
            slot = slots.get(item.get("slot_id"))
            n = party or int(item.get("quantity") or 1)
            if slot is not None and slot["remaining"] < n:
                return f"Only {slot['remaining']} place(s) left at {slot['start']}; {n} asked for."
        return None

    def _take_places(self, request: dict[str, Any], slots: dict[str, Any], release: bool = False) -> None:
        party = int(request.get("customer", {}).get("party_size") or 0)
        for item in request["items"]:
            slot = slots.get(item.get("slot_id"))
            if slot is not None:
                n = party or int(item.get("quantity") or 1)
                slot["remaining"] = slot["remaining"] + n if release else max(0, slot["remaining"] - n)

    def _alternatives(self, request: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
        wanted = {i["offering_id"] for i in request.get("items", [])}
        party = int(request.get("customer", {}).get("party_size") or 1)
        lead = timedelta(minutes=profile["policies"].get("min_lead_time_minutes", 0))
        asked = {i.get("slot_id") for i in request.get("items", [])}
        return [r for r in profile["availability"]
                if r["type"] == "slot" and r["offering_id"] in wanted and r["slot_id"] not in asked
                and r["remaining"] >= party and parse_time(r["start"]) - self.now >= lead][:3]

    def _total(self, request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
        offerings = {o["id"]: o for o in profile["offerings"]}
        party = request.get("customer", {}).get("party_size")
        amount, currency = 0, None
        for item in request["items"]:
            o = offerings[item["offering_id"]]
            variant = next((v for v in o.get("variants", []) if v["id"] == item.get("variant_id")), None)
            price = (variant or {}).get("price") or o["price"]
            if price["type"] != "fixed":
                return None
            n = item.get("quantity") or (party if price.get("per") == "person" and party else 1)
            amount += round(price["amount"]["amount_minor"] * n)
            currency = currency or price["amount"]["currency"]
        return {"amount_minor": amount, "currency": currency} if currency else None

    def _receipt(self, request_id: str, status: str, **fields: Any) -> dict[str, Any]:
        receipt = {"request_id": request_id, "status": status}
        receipt.update({k: v for k, v in fields.items() if v is not None and v != []})
        receipt["issued_at"] = _iso(self.now)
        return receipt

    def _remember(self, key: tuple[str, str], fingerprint: str, receipt: dict[str, Any]) -> dict[str, Any]:
        if receipt["status"] == "failed":
            return receipt  # nothing happened, so a corrected retry may reuse the request_id
        self.by_request[key] = receipt
        self.fingerprints[key] = fingerprint
        return receipt


def _fingerprint(request: dict[str, Any]) -> str:
    """What a request asks for, ignoring when it was sent, so a retry matches the original."""
    body = {k: v for k, v in request.items() if k != "created_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
