"""Payment hand-off: ask the business's own Lightning wallet for an invoice.

Gentifi never receives or holds money. For a Lightning address (user@domain) the
relay follows LNURL-pay (LUD-06/LUD-16): fetch the address's pay endpoint, ask its
callback for an invoice for the exact amount, check the invoice is for that amount,
and hand it to the customer's agent. Payment goes straight to the business's wallet.
If the wallet supports LUD-21, the callback also returns a `verify` URL that says
when the invoice is settled; otherwise the owner taps "Mark paid".
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

Http = Callable[[str], dict[str, Any]]
PRICES_URL = "https://mempool.space/api/v1/prices"  # BTC price in major fiat currencies
ADDRESS = re.compile(r"^([a-z0-9._+-]+)@([a-z0-9.-]+\.[a-z]{2,})$", re.IGNORECASE)


class PaymentError(RuntimeError):
    pass


def http_get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "gentifi-relay/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except (OSError, ValueError) as exc:
        raise PaymentError(f"couldn't reach {urllib.parse.urlsplit(url).netloc}: {exc}") from None


def lnurlp_url(address: str) -> str:
    m = ADDRESS.match(address.strip())
    if not m:
        raise PaymentError(f"'{address}' isn't a Lightning address (it should look like name@wallet.com)")
    user, domain = m.groups()
    return f"https://{domain.lower()}/.well-known/lnurlp/{user}"


def _pay_params(address: str, http: Http) -> dict[str, Any]:
    meta = http(lnurlp_url(address))
    if meta.get("status") == "ERROR" or meta.get("tag") != "payRequest" or "callback" not in meta:
        raise PaymentError(f"{address} didn't return a Lightning payment endpoint: {meta.get('reason', 'unexpected reply')}")
    return meta


def check_address(address: str, http: Http = http_get) -> None:
    """Raise PaymentError unless the address resolves to a working pay endpoint."""
    _pay_params(address, http)


def btc_price(currency: str, http: Http = http_get) -> float:
    prices = http(PRICES_URL)
    try:
        price = float(prices[currency.upper()])
    except (KeyError, TypeError, ValueError):
        raise PaymentError(f"no bitcoin price available in {currency}") from None
    if price <= 0:
        raise PaymentError(f"bad bitcoin price for {currency}: {price}")
    return price


_MSAT_PER_UNIT = {"": 100_000_000_000, "m": 100_000_000, "u": 100_000, "n": 100}


def bolt11_msat(invoice: str) -> int | None:
    """The amount a BOLT 11 invoice asks for, in millisatoshis (None if it has no amount)."""
    text = invoice.strip().lower()
    if text.startswith("lightning:"):
        text = text[len("lightning:"):]
    sep = text.rfind("1")
    m = re.fullmatch(r"ln(bcrt|bc|tbs|tb)(\d*)([munp]?)", text[:sep] if sep > 0 else "")
    if not m:
        raise PaymentError("not a Lightning invoice")
    digits, unit = m.group(2), m.group(3)
    if not digits:
        return None
    if unit == "p":
        if int(digits) % 10:
            raise PaymentError("invoice amount isn't a whole millisatoshi")
        return int(digits) // 10
    return int(digits) * _MSAT_PER_UNIT[unit]


@dataclass
class Invoice:
    bolt11: str
    sats: int
    btc_price: float        # price of 1 BTC in the order's currency, used for the conversion
    verify_url: str | None  # LUD-21: GET it to learn whether the invoice is settled

    @property
    def uri(self) -> str:
        return "lightning:" + self.bolt11


def request_invoice(address: str, amount_minor: int, currency: str, memo: str,
                    http: Http = http_get, price: float | None = None) -> Invoice:
    """Ask the business's wallet for an invoice worth `amount_minor` of `currency`."""
    if amount_minor <= 0:
        raise PaymentError("nothing to pay")
    meta = _pay_params(address, http)
    price = price if price is not None else btc_price(currency, http)
    sats = max(1, round(amount_minor / 100 / price * 100_000_000))
    msat = sats * 1000
    low, high = int(meta.get("minSendable", 1000)), int(meta.get("maxSendable", 0)) or None
    if msat < low or (high is not None and msat > high):
        raise PaymentError(f"{sats} sats is outside what {address} accepts "
                           f"({low // 1000}–{(high or 0) // 1000} sats)")
    query = {"amount": str(msat)}
    allowed = int(meta.get("commentAllowed", 0) or 0)
    if allowed > 0 and memo:
        query["comment"] = memo[:allowed]
    callback = meta["callback"]
    joiner = "&" if urllib.parse.urlsplit(callback).query else "?"
    res = http(callback + joiner + urllib.parse.urlencode(query))
    if res.get("status") == "ERROR" or "pr" not in res:
        raise PaymentError(f"{address} wouldn't create an invoice: {res.get('reason', 'unexpected reply')}")
    asked = bolt11_msat(res["pr"])
    if asked != msat:
        # Never pass on an invoice for a different amount from the one the customer agreed.
        raise PaymentError(f"{address} returned an invoice for the wrong amount")
    verify = res.get("verify")
    return Invoice(res["pr"], sats, price, verify if isinstance(verify, str) and verify.startswith("https://") else None)


def is_settled(verify_url: str, http: Http = http_get) -> bool:
    res = http(verify_url)
    return res.get("status") != "ERROR" and res.get("settled") is True
