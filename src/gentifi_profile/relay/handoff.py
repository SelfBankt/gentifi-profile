"""Orders confirmed on the toolkit's confirmation page, handed to the owner relay.

The toolkit's host (mcp.gentifi.com) shows the customer their order at /o/<link>;
when they tap Confirm and the business's owner is on Telegram, the host sends the
order here instead of handing the customer to the business's email. The owner
answers in Telegram and the customer's page shows the answer.

Only for the host on the same machine: the server listens on 127.0.0.1, and every
call needs the shared secret from a file both services can read
(GENTIFI_HANDOFF_SECRET_FILE, default next to the relay database).

    GET  /handoff/businesses/<business_id>      -> {"telegram": true|false}
    POST /handoff/orders                        -> the order's state (below)
    GET  /handoff/orders/<business_id>/<ref>    -> the order's state, or 404

State: {"status": pending_confirmation | confirmed | declined | expired | cancelled | failed,
"reference", "message", "confirm_by", "payment": {"status", "checkout_url"}}.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Any

from ..store import RequestBook, utc_now
from .links import Links

AGENT = {"name": "Gentifi confirmation page"}
REQUEST_PREFIX = "page-"  # request_id = page-<ref>: a page confirmed twice is one order
REF = re.compile(r"^[A-Z0-9]{8}$")


class HandoffError(ValueError):
    pass


def secret_file(db: Path) -> Path:
    return Path(os.environ.get("GENTIFI_HANDOFF_SECRET_FILE", db.parent / "handoff-secret"))


def load_secret(path: Path) -> str:
    """The shared secret, made on first use (readable by the services' own user only)."""
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32) + "\n")
    return path.read_text().strip()


class Handoff:
    def __init__(self, book: RequestBook, links: Links, secret: str):
        self.book, self.links, self._secret = book, links, secret

    def authorised(self, header: str | None) -> bool:
        scheme, _, token = (header or "").partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(token.strip().encode(), self._secret.encode())

    def on_telegram(self, business_id: str) -> bool:
        """Whether this business's owner answers orders in Telegram right now."""
        merchant = self.links.merchant(business_id)
        directory = self.book.directory
        return bool(merchant and merchant.chat_id and not merchant.onboarding
                    and business_id in directory.profiles
                    and business_id not in directory.paused and business_id not in directory.drafts)

    def submit(self, order: dict[str, Any]) -> dict[str, Any] | None:
        """Send a confirmed order to the owner. None if the owner isn't on Telegram (the
        page then hands the customer to the business as before)."""
        business_id, ref = order.get("business_id"), order.get("ref")
        if not isinstance(business_id, str) or not isinstance(ref, str) or not REF.match(ref):
            raise HandoffError("business_id and an 8-character ref are needed")
        items = order.get("items")
        if not isinstance(items, list) or not items:
            raise HandoffError("items are needed")
        customer = order.get("customer") or {}
        name, contact = str(customer.get("name", "")).strip(), str(customer.get("contact", "")).strip()
        if not name or not contact:
            raise HandoffError("the customer's name and a phone number or email are needed")
        if not self.on_telegram(business_id):
            return None
        notes = " ".join(x for x in [f"Collection on {order['collection_date']}." if order.get("collection_date") else "",
                                     str(order.get("note") or "").strip()] if x)
        request = {
            "request_id": REQUEST_PREFIX + ref,
            "business_id": business_id,
            "action": "order",
            "agent": AGENT,
            "created_at": utc_now().isoformat(timespec="seconds"),
            "customer_confirmation": {"method": "confirmation_page", "reference": ref,
                                      "confirmed_at": order.get("confirmed_at") or utc_now().isoformat(timespec="seconds")},
            "items": [{"offering_id": str(i.get("offering_id")), "quantity": i.get("quantity", 1)} for i in items],
            "customer": {"name": name[:100], ("email" if "@" in contact else "phone"): contact[:100]},
        }
        if notes:
            request["notes"] = notes[:500]
        return self.view(self.book.submit(request))

    def order(self, business_id: str, ref: str) -> dict[str, Any] | None:
        with self.book.lock:
            self.book.expire_overdue()
            receipt = self.book.by_request.get((business_id, REQUEST_PREFIX + ref))
        return self.view(receipt) if receipt else None

    @staticmethod
    def view(receipt: dict[str, Any]) -> dict[str, Any]:
        out = {k: receipt[k] for k in ("status", "reference", "message", "confirm_by") if k in receipt}
        if receipt.get("payment"):
            out["payment"] = {k: v for k, v in receipt["payment"].items() if k in {"status", "checkout_url"}}
        return out


def add_routes(server: Any, handoff: Handoff) -> None:
    """The three routes above, on the MCP server's HTTP app."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    def guard(request: Request) -> JSONResponse | None:
        if not handoff.authorised(request.headers.get("authorization")):
            return JSONResponse({"error": "not authorised"}, status_code=401)
        return None

    @server.custom_route("/handoff/businesses/{business_id}", methods=["GET"], include_in_schema=False)
    async def business(request: Request) -> JSONResponse:
        return guard(request) or JSONResponse({"telegram": handoff.on_telegram(request.path_params["business_id"])})

    @server.custom_route("/handoff/orders", methods=["POST"], include_in_schema=False)
    async def submit(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        try:
            body = await request.json()
            state = handoff.submit(body if isinstance(body, dict) else {})
        except ValueError as exc:  # bad JSON, or HandoffError
            return JSONResponse({"error": str(exc)}, status_code=400)
        if state is None:
            return JSONResponse({"error": "not_on_telegram"}, status_code=409)
        return JSONResponse(state)

    @server.custom_route("/handoff/orders/{business_id}/{ref}", methods=["GET"], include_in_schema=False)
    async def status(request: Request) -> JSONResponse:
        if denied := guard(request):
            return denied
        state = handoff.order(request.path_params["business_id"], request.path_params["ref"])
        return JSONResponse(state) if state else JSONResponse({"error": "not found"}, status_code=404)
