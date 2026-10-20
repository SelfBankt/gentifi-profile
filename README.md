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
| `src/gentifi_profile/relay/` | owner confirmation relay: owners answer requests on Telegram |
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

## Let owners answer on Telegram

With `--telegram`, the server runs the **owner confirmation relay** alongside it.
Each request that needs the owner goes to their Telegram chat:

```
New order GTF-5E54326D via an AI assistant

2 × Free-range eggs (Dozen)
1 × Seasonal veg box
Total £25.00 · Collection
Customer: Sam

Please answer by 22:00 today.
[Accept] [Decline] [Suggest another time]
```

- **Accept** confirms the request, and the agent gets the business's own way to
  pay: an invoice from the owner's Lightning wallet (LNURL-pay, checked for the exact
  amount; marked paid automatically if the wallet supports LUD-21), their own
  payment link, or pay on collection. **Gentifi never holds money.**
- **Decline** asks why (Sold out also marks those items sold out).
- **Suggest another time** declines with the suggestion, so the agent checks with
  the customer and sends a new request. The customer still confirms what is booked.
- **No answer** by `confirm_by` expires the request; nothing is confirmed.
- Owners can send `/orders`, `/listing`, `/pause`, `/resume` and `/help`.

Set up:

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`).
2. Put `TELEGRAM_BOT_TOKEN=<token>` in a `.env` file in the folder you run from, or
   any folder above it. Never commit it (`.env` is in `.gitignore`).
3. `gentifi-relay check` confirms the token works.
4. `gentifi-relay invite <business_id> [--lightning-address shop@wallet.com]
   [--payment-link https://...]` prints a one-time link; the owner opens it to connect.
5. `gentifi-mcp --profiles <folder> --telegram`

Owner links live in SQLite at `~/.gentifi/relay.db` (`GENTIFI_RELAY_DB` to change).
The bot uses long polling, so it runs on any machine with internet access. The
messaging layer is an interface, so WhatsApp can be added as another adapter.
Design: [`docs/telegram-merchant-bot-spec.md`](docs/telegram-merchant-bot-spec.md).

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
