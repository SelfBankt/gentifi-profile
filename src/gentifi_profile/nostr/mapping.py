"""Turn a Gentifi Business Profile into unsigned Nostr events.

- kind 0 (NIP-01): the business's Nostr profile
- kind 30017 (NIP-15): the business as a marketplace stall, with collection and
  delivery as its shipping zones
- kind 30018 (NIP-15): one product per fixed-price product (and per variant), with
  stock as `quantity` (0 when sold out, null when not tracked)
- kind 30402 (NIP-99): one classified listing per offering, including services and
  items priced on enquiry; `status` is `sold` when sold out
- kind 5 (NIP-09): when the business is paused (or back in onboarding after being
  published), a deletion request for the stall, products and listings instead;
  resuming or going live publishes them again

Freshness is the heart of the profile, so products and listings carry a
["freshness", observed_at, confidence, source] tag and say when stock was checked.
Every event carries a NIP-32 label (["L", "com.gentifi"], ["l", <business id>,
"com.gentifi"]) so agents can find Gentifi businesses on a relay.
"""

from __future__ import annotations

import json
from typing import Any

from ..freshness import parse_time

LABEL = "com.gentifi"
KIND_METADATA, KIND_DELETION, KIND_STALL, KIND_PRODUCT, KIND_LISTING = 0, 5, 30017, 30018, 30402
LISTING_KINDS = (KIND_STALL, KIND_PRODUCT, KIND_LISTING)
ZERO_DECIMAL = {"JPY", "KRW", "ISK", "HUF"}
SOLD_OUT = {"out_of_stock", "out_of_season"}
WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}


def profile_events(profile: dict[str, Any], created_at: int, paused: bool = False,
                   pubkey: str | None = None, reason: str | None = None) -> list[dict[str, Any]]:
    """Unsigned events for a profile. With paused, the listings are replaced by a deletion
    request (which needs the business's pubkey to name what it deletes) giving the reason."""
    business = profile["business"]
    labels = [["L", LABEL], ["l", business["id"], LABEL]]
    events = [_event(KIND_METADATA, created_at, labels, json.dumps(_metadata(business), ensure_ascii=False))]
    listings = [_stall(profile, created_at, labels), *_products(profile, created_at, labels),
                *_listings(profile, created_at, labels)]
    if not paused:
        return events + listings
    if pubkey is None:
        raise ValueError("a paused business needs its pubkey to delete its listings")
    refs = [["a", f"{e['kind']}:{pubkey}:{_d(e)}"] for e in listings]
    kinds = [["k", str(k)] for k in LISTING_KINDS]
    events.append(_event(KIND_DELETION, created_at, refs + kinds + labels,
                         reason or f"{business['name']} is paused and not taking orders right now."))
    return events


# ------------------------------------------------------------ events


def _event(kind: int, created_at: int, tags: list[list[str]], content: str) -> dict[str, Any]:
    return {"kind": kind, "created_at": created_at, "tags": [list(t) for t in tags], "content": content}


def _d(event: dict[str, Any]) -> str:
    return next(t[1] for t in event["tags"] if t[0] == "d")


def _metadata(business: dict[str, Any]) -> dict[str, Any]:
    meta = {"name": business["name"], "about": _about(business)}
    if business.get("contact", {}).get("website"):
        meta["website"] = business["contact"]["website"]
    return meta


def _about(business: dict[str, Any]) -> str:
    parts = [business.get("description", "").strip(), _place(business), hours_text(business)]
    return "\n\n".join(p for p in parts if p)


def _stall(profile: dict[str, Any], created_at: int, labels: list[list[str]]) -> dict[str, Any]:
    business = profile["business"]
    content = {"id": business["id"], "name": business["name"], "description": _about(business),
               "currency": _currency(profile), "shipping": _zones(profile)}
    return _event(KIND_STALL, created_at, [["d", business["id"]], *labels], json.dumps(content, ensure_ascii=False))


def _products(profile: dict[str, Any], created_at: int, labels: list[list[str]]) -> list[dict[str, Any]]:
    business = profile["business"]
    zones = {z["id"] for z in _zones(profile)}
    out = []
    for offering, variant, price in _priced_items(profile):
        if offering["kind"] != "product" or price.get("type") != "fixed":
            continue
        d = _item_id(offering, variant)
        stock = _stock(profile, offering, variant)
        content = {"id": d, "stall_id": business["id"], "name": _item_name(offering, variant),
                   "description": offering.get("description", ""), "images": [],
                   "currency": price["amount"]["currency"], "price": _amount(price["amount"]),
                   "quantity": _quantity(stock),
                   "specs": _specs(offering, price, stock),
                   "shipping": [{"id": f, "cost": 0} for f in offering["fulfilment"] if f in zones]}
        tags = [["d", d], *[["t", t] for t in _topics(profile, offering)], *_freshness(stock), *labels]
        out.append(_event(KIND_PRODUCT, created_at, tags, json.dumps(content, ensure_ascii=False)))
    return out


def _listings(profile: dict[str, Any], created_at: int, labels: list[list[str]]) -> list[dict[str, Any]]:
    business = profile["business"]
    out = []
    for offering, variant, price in _priced_items(profile):
        stock = _stock(profile, offering, variant)
        name = _item_name(offering, variant)
        summary = f"{_price_text(price)} at {business['name']}"
        tags = [["d", _item_id(offering, variant)], ["title", name], ["summary", summary],
                ["published_at", str(created_at)], ["location", _place(business)]]
        if price.get("type") == "fixed":
            tags.append(["price", _amount_text(price["amount"]), price["amount"]["currency"]])
        geo = business["location"].get("geo")
        if geo:
            tags.append(["g", geohash(geo["lat"], geo["lng"], 7)])
        tags += [["t", t] for t in _topics(profile, offering)]
        tags.append(["status", "sold" if stock and stock["status"] in SOLD_OUT else "active"])
        tags += _freshness(stock) + labels
        lines = [f"**{name}** — {_price_text(price)}"]
        if offering.get("description"):
            lines.append(offering["description"])
        specs = _specs(offering, price, stock)
        if specs:
            lines.append("\n".join(f"- {k}: {v}" for k, v in specs))
        lines.append(f"Ask your AI assistant to order or book from {business['name']}.")
        out.append(_event(KIND_LISTING, created_at, tags, "\n\n".join(lines)))
    return out


# ------------------------------------------------------------ profile helpers


def _priced_items(profile: dict[str, Any]):
    """(offering, variant or None, price) for each offering and each of its variants."""
    for offering in profile["offerings"]:
        variants = offering.get("variants") or []
        if not variants:
            yield offering, None, offering["price"]
        for variant in variants:
            yield offering, variant, variant.get("price") or offering["price"]


def _item_id(offering: dict[str, Any], variant: dict[str, Any] | None) -> str:
    return f"{offering['id']}:{variant['id']}" if variant else offering["id"]


def _item_name(offering: dict[str, Any], variant: dict[str, Any] | None) -> str:
    return f"{offering['name']} ({variant['name']})" if variant else offering["name"]


def _stock(profile: dict[str, Any], offering: dict[str, Any], variant: dict[str, Any] | None):
    """The stock record for this variant, or for the whole offering."""
    records = [r for r in profile["availability"] if r["type"] == "stock" and r["offering_id"] == offering["id"]]
    exact = [r for r in records if variant and r.get("variant_id") == variant["id"]]
    whole = [r for r in records if not r.get("variant_id")]
    return (exact or whole or [None])[0]


def _quantity(stock: dict[str, Any] | None) -> int | None:
    if stock is None or stock["status"] == "preorder":
        return None
    if stock["status"] in SOLD_OUT:
        return 0
    return int(stock["quantity"]) if "quantity" in stock else None


def _specs(offering: dict[str, Any], price: dict[str, Any], stock: dict[str, Any] | None) -> list[list[str]]:
    specs = []
    if price.get("per") and price["per"] != "each":
        specs.append(["Price per", price["per"]])
    for key, value in (offering.get("attributes") or {}).items():
        specs.append([key.replace("_", " ").capitalize(), "yes" if value is True else "no" if value is False else str(value)])
    if stock:
        status = {"in_stock": "In stock", "low": "Running low", "out_of_stock": "Sold out", "preorder": "Pre-order",
                  "out_of_season": "Out of season"}[stock["status"]]
        if "quantity" in stock and stock["status"] not in SOLD_OUT:
            status += f" ({stock['quantity']:g}{' ' + stock['quantity_unit'] if stock.get('quantity_unit') else ''})"
        fresh = stock["freshness"]
        when = parse_time(fresh["observed_at"]).strftime("%-d %b %Y %H:%M")
        specs += [["Stock", status], ["Stock checked", f"{when} ({fresh['confidence']})"]]
    return specs


def _freshness(stock: dict[str, Any] | None) -> list[list[str]]:
    if not stock:
        return []
    fresh = stock["freshness"]
    return [["freshness", fresh["observed_at"], fresh["confidence"], fresh["source"]]]


def _topics(profile: dict[str, Any], offering: dict[str, Any]) -> list[str]:
    business = profile["business"]
    topics = ["gentifi", *business.get("tags", []), offering["kind"]]
    topics += [k for k, v in (offering.get("attributes") or {}).items() if v is True]
    return list(dict.fromkeys(t.lower() for t in topics))


def _zones(profile: dict[str, Any]) -> list[dict[str, Any]]:
    business = profile["business"]
    fulfilment = {f for o in profile["offerings"] for f in o["fulfilment"]}
    zones = []
    address = business["location"].get("address", {})
    if "collection" in fulfilment:
        zones.append({"id": "collection", "name": f"Collection from {business['name']}", "cost": 0,
                      "regions": [r for r in [address.get("locality"), address.get("postal_code")] if r]})
    delivery = profile["policies"].get("delivery")
    if "delivery" in fulfilment and delivery:
        area = delivery.get("area", {})
        regions = area.get("postcode_prefixes") or ([f"Within {area['radius_km']} km"] if "radius_km" in area else [])
        zones.append({"id": "delivery", "name": "Local delivery", "regions": regions,
                      "cost": _amount(delivery["fee"]) if delivery.get("fee") else 0})
    return zones


def _currency(profile: dict[str, Any]) -> str:
    for _, _, price in _priced_items(profile):
        if price.get("amount"):
            return price["amount"]["currency"]
    return "GBP"


def _amount(money: dict[str, Any]) -> float | int:
    if money["currency"] in ZERO_DECIMAL:
        return money["amount_minor"]
    value = money["amount_minor"] / 100
    return int(value) if value == int(value) else value


def _amount_text(money: dict[str, Any]) -> str:
    if money["currency"] in ZERO_DECIMAL:
        return str(money["amount_minor"])
    return f"{money['amount_minor'] / 100:.2f}"


def _money_text(money: dict[str, Any]) -> str:
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(money["currency"])
    return f"{symbol}{_amount_text(money)}" if symbol else f"{_amount_text(money)} {money['currency']}"


def _price_text(price: dict[str, Any]) -> str:
    per = f" per {price['per']}" if price.get("per") and price["per"] != "each" else ""
    if price["type"] == "quote":
        return "Price on request"
    if price["type"] == "from":
        return f"From {_money_text(price['amount'])}{per}"
    if price["type"] == "range":
        return f"{_money_text(price['amount'])}–{_money_text(price['max_amount'])}{per}"
    return f"{_money_text(price['amount'])}{per}"


def _place(business: dict[str, Any]) -> str:
    address = business["location"].get("address", {})
    return ", ".join(p for p in [address.get("locality"), address.get("region")] if p)


def hours_text(business: dict[str, Any]) -> str:
    rows = business.get("opening_hours") or []
    parts = []
    for row in rows:
        if row.get("days") and row.get("opens") and row.get("closes"):
            parts.append(f"{_days(row['days'])} {row['opens']}–{row['closes']}")
    return ("Open " + ", ".join(parts) + ".") if parts else ""


def _days(days: list[str]) -> str:
    """'Mon–Fri' for a run of three or more days, otherwise 'Mon, Wed and Fri'."""
    names = [DAY_NAMES.get(d, d) for d in days]
    order = [WEEK.index(d) for d in days if d in WEEK]
    run = len(order) == len(days) and order == list(range(order[0], order[0] + len(order)))
    if len(names) > 2 and run:
        return f"{names[0]}–{names[-1]}"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# ------------------------------------------------------------ geohash

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lng: float, precision: int = 7) -> str:
    lat_range, lng_range = [-90.0, 90.0], [-180.0, 180.0]
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        rng, value = (lng_range, lng) if even else (lat_range, lat)
        mid = (rng[0] + rng[1]) / 2
        if value >= mid:
            ch = ch << 1 | 1
            rng[0] = mid
        else:
            ch <<= 1
            rng[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_BASE32[ch])
            bits, ch = 0, 0
    return "".join(out)
