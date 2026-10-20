# CLAUDE.md

Context for Claude when working in this repository.

## What this is

The open specification and reference tooling for the **Gentifi Business Profile**
(draft v0.1): a protocol-neutral format describing what a local business offers,
what is available right now, and what an AI agent may do on its behalf.

Gentifi's aim ("Gentifi your business": discoverable and responsive to customers'
AI) is to be the **source of truth for what a small business can actually do right
now, plus a reliable way to act on it**. MCP is how this is delivered to agents,
not the moat. The value is in keeping small businesses' data true.

The defining idea of the spec is **freshness**: every availability record says when
it was observed, how confident that is (`confirmed` / `likely` / `typical` /
`unknown`) and where it came from. Agents use this to decide whether to book
instantly, ask the owner to confirm, or warn the user. Protect this in any change.

**The customer always confirms.** `hold`, `book` and `order` requests must carry
`customer_confirmation` (`in_app`, or `confirmation_page` with its reference, and
when). An agent never commits a customer on its own. Keep this in any change.

The live Gentifi service runs on the separate
[Gentifi toolkit](https://github.com/SelfBankt/gentifi-toolkit) (hosted connector at
`https://mcp.gentifi.com/mcp`). This profile is the richer format that service is
heading towards; the plan is for the toolkit to publish each business as a profile.

## Layout

- `schema/v0.1/profile.schema.json` — the published spec (JSON Schema 2020-12).
  `src/gentifi_profile/schema/profile.schema.json` must stay identical (a test checks).
- `examples/` — valid profiles (fictional farm shop and plumber, with Ofcom drama-range
  phone numbers) and `messages/` (request, receipts).
- `src/gentifi_profile/`
  - `freshness.py` — turns freshness metadata into agent advice
  - `checks.py` — schema validation plus cross-reference, request and staleness rules
  - `cli.py` — `gentifi-validate`
  - `store.py` — in-memory directory and request handling (receipts, per-business
    idempotency, random references, holds, cancellation, owner responses that
    never overbook)
  - `server.py` — reference MCP server `gentifi-mcp` (MCP Python SDK 2.x, `MCPServer`);
    `--telegram` runs the owner relay in a background thread on the same RequestBook
  - `relay/` — owner confirmation relay. `channel.py` (OwnerChannel interface +
    MockChannel), `telegram.py` (Bot API, long polling, stdlib only), `links.py`
    (SQLite: owner chat links, invites, prompt messages), `payments.py` (LNURL-pay
    invoice from the merchant's own Lightning address, LUD-21 verify, BOLT 11 amount
    check), `relay.py` (prompts, buttons, commands, expiry), `cli.py` (`gentifi-relay`)
- `tests/` — `test_profile.py` (spec and validator), `test_requests.py` (request
  edge cases), `test_server.py` (end to end over MCP), `test_relay.py` (relay through
  a mock chat and fake wallet), `test_telegram.py` (adapter against a fake Bot API).
- `docs/telegram-merchant-bot-spec.md` — design of the Telegram merchant bot.
- `docs/developers-page.md` — copy for the Gentifi Business Profile section on
  gentifi.com/standards.

## Commands

```bash
pip install -e ".[dev]"
python -m pytest -q
gentifi-relay check                     # Telegram token from .env works?
gentifi-relay invite gtf_hollowbrookfarm --lightning-address shop@wallet.com
gentifi-mcp --profiles examples --telegram
gentifi-validate examples/*.json examples/messages/*.json --profile examples/farm-shop.json --strict --now 2026-10-15T20:00:00+01:00
gentifi-mcp --profiles examples --demo --now 2026-10-15T20:00:00+01:00
```

Example data is dated 15 Oct 2026; pass `--now` (or `GENTIFI_NOW`) so it isn't stale.

## Conventions

- Money in minor units (`amount_minor`); ISO 8601 times with offsets; IANA time zones.
- schema.org names where a close equivalent exists.
- Keep the core small; vertical-specific fields go in `extensions` under
  reverse-domain keys (Gentifi's own: `com.gentifi.*`).
- Every schema change needs an updated example and a test.
- The reference server is not production: in-memory requests, no stock decrement on
  orders. Keep it labelled that way.
- **Gentifi never touches money.** Payment is only ever a hand-off of the business's
  own payment request (their Lightning wallet's invoice, their payment link, or pay
  on fulfilment) via `Receipt.payment`. Never route funds through Gentifi.
- The relay's store listeners must stay quick (they run under the RequestBook lock);
  the relay queues work. Owner-facing text is plain, short and button-first.
- Secrets (bot token) come from the environment or a `.env` file, never the repo.

## Publishing

- Schema `$id`: `https://gentifi.com/standards/profile-0.1.schema.json`.
- Repository: github.com/SelfBankt/gentifi-profile (private for now).
- Licence: Apache 2.0 for spec, examples and code (still to confirm before going public).

## Roadmap

Priority is keeping data true; the server and spec are the smaller share of effort.

1. **Owner confirmation relay** (Telegram adapter built: `relay/`; WhatsApp to follow
   as a second adapter). Message to the owner with
   one-tap Accept / Decline / Suggest another time, producing the receipt the agent
   waits for. Requests expire when the owner misses the deadline. Freshness nudges
   ("Still got eggs today? Yes / Sold out / Only a few"). SMS fallback. Build with a
   mock WhatsApp layer first so the flow is testable end to end; switch to the
   WhatsApp Business Platform (Meta or Twilio) by configuration. Needs: Meta business
   verification, dedicated number, pre-approved templates for messages outside the
   24-hour window, always-on public hosting.
2. **Automated onboarding.** Owner sends whatever they have (price-board photo, menu,
   website, forwarded WhatsApp message); AI extracts a draft profile; the validator
   checks it; the owner approves or corrects by reply. Imports from Google Business
   Profile and Calendly later (each needs its own API approval).
3. **Till and card-reader integrations** (`source: integration`). Live stock and
   sales feed freshness with no owner effort. In order:
   - Square: list on its self-service App Marketplace (full catalogue and stock
     data); its referral programme is a side income.
   - Zettle (PayPal): product library, inventory and purchase APIs with real-time
     push events; apply early for partner approval, needed for multi-merchant apps.
   - SumUp: public API mainly covers transactions; use them as an "open and selling"
     signal. Integration and referral partner routes exist.
   Confirm item-level stock support in each developer portal before building.
   - Musqet (musqet.com): UK payments and ePOS for independents and market stalls,
     with Bitcoin payments and an agentic-commerce product. Strategic partner for
     both stock data and agent checkout (payment hand-off, Bitcoin payments). Start
     with a direct approach; confirm its API exposes stock/product data and whether
     it supports Lightning. Smaller merchant base, so a partner, not the main source.
4. **One aggregate Gentifi connector** listed in the Claude, ChatGPT and Gemini
   directories, covering every business. Per-business connectors and on-site
   structured data (schema.org JSON-LD) are extras, not the main route.
5. **Verification and discovery registry**, once there are enough businesses.
6. **Transaction fees**, once real orders flow (receipts already carry totals).

Spec work expected for v0.2: `agent_report` outcomes feeding freshness, payment
hand-off, schema.org and agent-commerce protocol mappings.

Go-to-market: one category in one local area. Success test: about 10 businesses stay
current for a month without being chased.
