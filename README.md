# Gentifi Business Profile

**Draft v0.1 — open for feedback.**

An open, protocol-neutral format that describes what a local business offers,
what is available right now, and what an AI agent may do on its behalf.

The goal is simple: when someone asks their assistant to *"get two dozen eggs from
a farm near me for Saturday"* or *"book a boiler service next week"*, the agent can
find a real local business, see what's actually available, and make the booking
or order reliably — without that business needing its own app, platform account or
developer.

## What makes it different: freshness

Most catalogue formats say what is available. This one also says **how sure you can
be**. Every availability record carries:

| Field | Meaning |
| --- | --- |
| `observed_at` | when this was last known to be true |
| `valid_until` | after this, treat it as stale (optional) |
| `confidence` | `confirmed`, `likely`, `typical` (a recurring pattern, not a promise) or `unknown` |
| `source` | `owner`, `integration` (synced booking/till system), `message_parse` (read from a WhatsApp/SMS/photo), `pattern` or `agent_report` |

From that, an agent can decide whether the booking can be **confirmed straight away**
once the customer agrees, whether to **ask the business to confirm first**, or whether
to **warn the user** that the information may be out of date.
Small businesses rarely have live inventory systems; this lets them take part
honestly anyway.

## The shape of a profile

```
profile
├── business       identity, location or service area, hours, verification
├── offerings      products, bookable services, rentals, events, quoted work
├── availability   stock | slot | pattern — each with freshness
├── policies       notice, cancellation, deposit, delivery, payment
└── actions        hold, book, order, cancel, check_status, enquire
                   each "instant" or "request_to_confirm" (with a response time)
```

At run time an agent sends an **ActionRequest** and gets a **Receipt** back
(`confirmed`, `held`, `pending_confirmation`, `declined` with a reason and
alternatives, `cancelled`, `expired` or `failed`). Requests carry an idempotency
key, so a retried request can never create a second booking, and can use a contact
relay token instead of the customer's raw phone number or email.

**The customer always confirms.** A `hold`, `book` or `order` must carry
`customer_confirmation`: how and when the customer agreed to exactly that request,
either in their AI app (`in_app`) or by tapping Confirm on a page that showed the
order (`confirmation_page`, with its reference). An agent never commits a customer
on its own; requests without it are refused.

A trimmed example:

```json
{
  "type": "slot",
  "slot_id": "tour_20261017_1000",
  "offering_id": "farm_tour",
  "start": "2026-10-17T10:00:00+01:00",
  "end": "2026-10-17T11:00:00+01:00",
  "remaining": 4,
  "freshness": {
    "observed_at": "2026-10-15T19:30:00+01:00",
    "confidence": "confirmed",
    "source": "integration"
  }
}
```

Full examples: [`examples/farm-shop.json`](examples/farm-shop.json) (stock,
slots, patterns, quoted items) and [`examples/plumber.json`](examples/plumber.json)
(a mobile tradesperson with a service area). Request and receipt examples are in
[`examples/messages/`](examples/messages/).

Design choices worth knowing:

- Money is in minor units (`amount_minor: 350` = £3.50) to avoid rounding errors.
- Field names follow [schema.org](https://schema.org) where a close equivalent exists.
- Vertical-specific fields go in `extensions` under reverse-domain keys
  (e.g. `com.gentifi.farm`), so the core stays small.
- The format is protocol-neutral. The same profile can be served over MCP,
  published as structured data on a website, or carried by agent-to-agent and
  agent-commerce protocols.

## What's in this repository

| Path | What it is |
| --- | --- |
| [`schema/v0.1/profile.schema.json`](schema/v0.1/profile.schema.json) | the specification, as JSON Schema 2020-12 |
| `examples/` | valid example profiles and messages |
| `src/gentifi_profile/` | Python validator and reference MCP server |
| `src/gentifi_profile/nostr/` | publishing businesses to Nostr (NIP-15 and NIP-99) |
| `tests/` | tests for both |

## Validate a profile

```bash
pip install -e .                      # from a clone of this repo
gentifi-validate examples/farm-shop.json
gentifi-validate my-request.json --profile examples/farm-shop.json
```

Beyond the schema, the validator checks things a schema can't: that every id it
references exists, that slots end after they start and don't exceed capacity,
that quoted items can be enquired about, and that availability isn't stale.
Use `--strict` to fail on warnings, `--json` for machine-readable output and
`--now` to evaluate freshness at a fixed time.

## Run the reference MCP server

```bash
pip install -e ".[server]"
gentifi-mcp --profiles examples --demo --now 2026-10-15T20:00:00+01:00
```

It serves every profile in a folder to any MCP-capable agent, with five tools:
`find_businesses`, `get_business_info`, `search_catalogue`, `check_availability`
(each record annotated with `book_instantly`, `request_confirmation`, `warn_user`
or `not_available`) and `submit_request`. `--demo` adds `respond_as_owner` so you
can play the business and confirm or decline pending requests. `--now` freezes the
clock so the example data stays fresh.

To use it with Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gentifi": {
      "command": "gentifi-mcp",
      "args": ["--profiles", "/path/to/profiles", "--demo", "--now", "2026-10-15T20:00:00+01:00"]
    }
  }
}
```

Use `--transport streamable-http` to serve it over HTTP instead.

**This is a reference implementation, not a production service.** It keeps
bookings in memory, doesn't take payment (at most it passes on the business's own
payment request) and doesn't adjust stock levels on orders. Its job is to show the intended behaviour and let you test
the format with real agents.

## Owner confirmation channels

Requests that need the business to confirm wait as `pending_confirmation` until the
owner answers. In the reference server `--demo`'s `respond_as_owner` plays the owner.
A real channel (owners answering from a chat app) is a separate package that plugs into
`gentifi-mcp` through the `gentifi_profile.server_plugins` entry point: it can add
command-line options, start alongside the server on the same `RequestBook`
(`book.owner_respond` answers a request) and add routes to the HTTP server. See the
docstring at the top of `src/gentifi_profile/server.py`. Gentifi's hosted service adds
its Telegram owner bot this way.

## Publish businesses to Nostr

Gentifi can publish each business to Nostr, so any Nostr marketplace app or agent
can read it. The owner never sees Nostr: Gentifi makes a key for each business and
signs for it.

| Event | What it holds |
| --- | --- |
| kind 0 (NIP-01) | the business's name, description, place and opening hours |
| kind 30017 (NIP-15 stall) | the business, with collection and delivery as shipping zones |
| kind 30018 (NIP-15 product) | each fixed-price product and variant; stock as `quantity` (0 when sold out, null when not tracked) |
| kind 30402 (NIP-99 listing, with [market spec](https://github.com/GammaMarkets/market-spec) tags) | every offering, including services and items priced on request; `status` is `sold` when sold out; `type`, `visibility`, `stock`, `spec` and `image` tags, and an offering with sizes as a `variable` product with a `variation` per size |
| kind 30405 (market spec collection) | the business's catalogue |
| kind 30406 (market spec shipping option) | collection from the shop (`pickup`) and local delivery (`standard`) |
| kind 5 (NIP-09 deletion) | sent instead of the listings while the business is paused |

NIP-15 is deprecated in favour of NIP-99, and newer marketplace apps follow the
GammaMarkets market spec, which builds a full shop on NIP-99. Gentifi publishes
both, so older and newer apps can read the same business. Field by field:
[`docs/nostr-mapping.md`](docs/nostr-mapping.md).

Products and listings carry a `["freshness", observed_at, confidence, source]` tag,
and every event a NIP-32 label (`["L", "com.gentifi"]`, `["l", <business id>,
"com.gentifi"]`) so agents can ask a relay for Gentifi businesses.

```bash
pip install -e ".[nostr]"
gentifi-nostr init                    # adds GENTIFI_NOSTR_MASTER_KEY to .env, once
gentifi-nostr relay                   # a test relay on ws://127.0.0.1:7777 (in memory)
gentifi-nostr events gtf_hollowbrookfarm --profiles examples       # dry run
gentifi-nostr publish gtf_hollowbrookfarm --profiles examples --relay ws://127.0.0.1:7777
gentifi-nostr show gtf_hollowbrookfarm --relay ws://127.0.0.1:7777
gentifi-mcp --profiles examples --transport streamable-http --nostr-relay ws://127.0.0.1:7777
```

With `--nostr-relay`, the server publishes every business when it starts and again
when its listing changes: items marked sold out, the business paused (which deletes
the listings) and resumed.

- **Keys** are kept in `~/.gentifi/relay.db`, encrypted (AES-256-GCM) with the master
  key from `.env`. Keep a copy of the master key: without it Gentifi can't sign for
  those businesses again. Signing sits behind a `Signer` interface so a NIP-46 remote
  signer can replace this later.
- **Only relays on this machine are allowed** unless you pass `--allow-public`
  (`--nostr-allow-public` for the server). Events sent to a public relay are copied
  by others and can't be fully taken back.
- Pausing and resuming are kept in memory: a restarted server
  publishes every business as open.
- **Orders don't come back from Nostr yet.** Marketplace apps may offer to send the
  business an order as an encrypted message, which nothing reads yet; each listing says
  to order through an AI assistant. Read this before publishing to public relays.

## How this relates to the Gentifi toolkit

The live Gentifi service runs on the [Gentifi toolkit](https://github.com/SelfBankt/gentifi-toolkit),
which turns a business's spreadsheet or till export into a hosted connector at
`https://mcp.gentifi.com/mcp`. This profile is a draft of the richer format that
service is heading towards: freshness on every piece of availability, bookable
slots, and actions with receipts. The plan is for the toolkit to publish each
business as a profile; until then, the two are separate.

## Status and roadmap

v0.1 is a draft. It will change based on what breaks when real agents and real
businesses use it. Things we expect to look at next:

- how agents report back outcomes (`agent_report`) to keep data fresh
- payment hand-off (checkout links, deposits)
- mappings to schema.org JSON-LD and to emerging agent-commerce protocols
- a shared registry for discovery and verification

## Contributing

Feedback is the most useful contribution at this stage. Please open an issue,
especially if you've tried describing a real business and something didn't fit.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[Apache 2.0](LICENSE) — the specification, examples and code.
