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

## Layout

- `schema/v0.1/profile.schema.json` — the published spec (JSON Schema 2020-12).
  `src/gentifi_profile/schema/profile.schema.json` must stay identical (a test checks).
- `examples/` — valid profiles (farm shop, plumber) and `messages/` (request, receipts).
- `src/gentifi_profile/`
  - `freshness.py` — turns freshness metadata into agent advice
  - `checks.py` — schema validation plus cross-reference, request and staleness rules
  - `cli.py` — `gentifi-validate`
  - `store.py` — in-memory directory and request handling (receipts, idempotency,
    holds, cancellation, owner responses)
  - `server.py` — reference MCP server `gentifi-mcp` (MCP Python SDK 2.x, `MCPServer`)
- `docs/developers-page.md` — copy for the Gentifi website's developers page.

## Commands

```bash
pip install -e ".[dev]"
pytest -q
gentifi-validate examples/*.json examples/messages/*.json --profile examples/farm-shop.json --strict --now 2026-10-15T20:00:00+01:00
gentifi-mcp --profiles examples --demo --now 2026-10-15T20:00:00+01:00
```

Example data is dated 15 Oct 2026; pass `--now` (or `GENTIFI_NOW`) so it isn't stale.

## Conventions

- Money in minor units (`amount_minor`); ISO 8601 times with offsets; IANA time zones.
- schema.org names where a close equivalent exists.
- Keep the core small; vertical-specific fields go in `extensions` under
  reverse-domain keys.
- Every schema change needs an updated example and a test.
- The reference server is not production: in-memory, no payments, no stock
  decrement on orders. Keep it labelled that way.

## Before publishing (open decisions)

- Domain in the schema `$id` (currently `gentifi.org`, a placeholder).
- GitHub URL (`REPO_URL` placeholders in `docs/developers-page.md`).
- Licence (currently Apache 2.0 for spec, examples and code).

## Roadmap

Priority is keeping data true; the server and spec are the smaller share of effort.

1. **Owner confirmation relay** (build first). WhatsApp message to the owner with
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
   Profile, Square and Calendly later (each needs its own API approval).
3. **One aggregate Gentifi connector** listed in the Claude, ChatGPT and Gemini
   directories, covering every business. Per-business connectors and on-site
   structured data (schema.org JSON-LD) are extras, not the main route.
4. **Verification and discovery registry**, once there are enough businesses.
5. **Transaction fees**, once real orders flow (receipts already carry totals).

Spec work expected for v0.2: `agent_report` outcomes feeding freshness, payment
hand-off, schema.org and agent-commerce protocol mappings.

Go-to-market: one category in one local area. Success test: about 10 businesses stay
current for a month without being chased.
