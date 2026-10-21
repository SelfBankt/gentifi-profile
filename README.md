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

Use `--transport streamable-http` to serve it over HTTP instead. With
`--telegram`, run it on its own instead: see
[Run the bot on its own](#run-the-bot-on-its-own).

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
- Owners can send `/orders`, `/listing`, `/soldout`, `/instock`, `/pause`, `/resume` and `/help`.
  `/soldout` and `/instock` list the shop's products as buttons: one tap marks an item sold
  out or back in stock (every size of it), owner-confirmed and fresh.

Set up:

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`).
2. Put `TELEGRAM_BOT_TOKEN=<token>` in a `.env` file in the folder you run from, or
   any folder above it. Never commit it (`.env` is in `.gitignore`).
3. `gentifi-relay check` confirms the token works.
4. `gentifi-relay invite <business_id> [--lightning-address shop@wallet.com]
   [--payment-link https://...]` prints a one-time link; the owner opens it to connect.
5. Run the server with the relay on its own, as below, and leave it running.

### Onboard a new business

For a new business, Gentifi writes the profile by hand and the owner checks it in
Telegram before anything goes live. Once, run `gentifi-relay team` and open the link
yourself: that chat is then told when an owner connects, asks for a change or goes live.

1. Put the profile in the server's profiles folder.
2. `gentifi-relay invite <business_id> --onboard` prints the owner's link. The business
   is hidden from agents (as if it didn't exist) and not published to Nostr (if it was
   published before, its listings are taken down until it goes live again); a running
   bot picks this up within 30 seconds, no restart needed.
3. The owner opens the link and gets a summary: name, address, opening hours, the first
   three items with prices, collection or delivery. `Looks right` or `Something's wrong`.
4. `Something's wrong`: the owner types what to change. It's sent to your team chat,
   saved (`gentifi-relay merchants` shows it) and printed in the bot's window. Edit the
   profile, then `gentifi-relay review <business_id>`: the bot reloads it and sends the
   owner the new summary.
5. `Looks right` asks how customers pay: always in person, and optionally ahead to the
   owner's own Lightning address (checked before saving) or payment link.
6. `Go live`: the business appears to agents and is published to Nostr.

`gentifi-relay review` also works for a live business after you edit its profile: the
bot reloads it (bookings made since start-up are reset) and tells the owner.

### Orders from the toolkit's confirmation page

On the Gentifi host, customers confirm orders on the toolkit's page at
`mcp.gentifi.com/o/<link>`. When the business's owner is linked here (and live, not
paused), the host hands the confirmed order to this server instead of the business's
email: the owner answers in Telegram and the page shows the customer the answer. The
host calls three routes on `127.0.0.1` with a shared secret
(`src/gentifi_profile/relay/handoff.py`); they exist only with `--telegram` and the HTTP
transport. Owners get `--answer-minutes` (default 120) to answer, whatever a profile
asks for. With `--profiles <toolkit businesses folder>` the server also reads the
`gentifi-profile.json` the host writes for each business, and picks up changes within
30 seconds.

Businesses being onboarded are listed in `onboarding.json` next to the relay database,
rewritten whenever one is invited with `--onboard` or goes live; the toolkit's host reads it
(`GENTIFI_ONBOARDING_FILE`) to keep them hidden from customers' agents until then.

### Photos of prices

An owner linked to the bot can send it a photo of their price board or menu (or a
screenshot). The bot thanks them, passes the photo to Gentifi's team chats, and, with the
toolkit installed (`GENTIFI_TOOLKIT`) and a model set (`GENTIFI_AI_MODEL`, plus the toolkit's
AI key settings), drafts a catalogue from it with `gentifi draft` and sends the team the
items, prices and anything to check (`src/gentifi_profile/relay/drafts.py`). Nothing changes
for the business until someone has checked the draft.

### Run the bot on its own

On the Gentifi VPS it runs as the `gentifi-bot` service: see [docs/vps.md](docs/vps.md).

Only one copy of the bot can poll Telegram at a time, and each copy of the server
keeps its own requests in memory. So run exactly one server with `--telegram`, over
HTTP, from the folder with your `.env` (or one below it):

```bash
gentifi-mcp --profiles <folder> --telegram --transport streamable-http
```

It listens on `http://127.0.0.1:8000/mcp`, on this machine only (`--port` to use
another port, and change the address below to match). Then point agents
at that one server. Don't put `--telegram` in `claude_desktop_config.json`:
Claude Desktop starts each local server more than once (once for chats, once for
its local tool bridge), which gives two bots that clash over Telegram ("Conflict:
terminated by other getUpdates request") and an owner's Accept can reach the copy
that never saw the order. Connect Claude Desktop through
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) instead, which needs
Node.js:

```json
{
  "mcpServers": {
    "gentifi-local-test": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "http://127.0.0.1:8000/mcp"]
    }
  }
}
```

Start the server before Claude Desktop, and restart Claude Desktop after
restarting the server. If the hosted Gentifi connector is also switched on, turn it
off in the chat or name `gentifi-local-test` in your request, so the assistant uses
this server. Requests are lost when the server stops; owner links are kept.

Owner links live in SQLite at `~/.gentifi/relay.db` (`GENTIFI_RELAY_DB` to change).
The bot uses long polling, so it runs on any machine with internet access. The
messaging layer is an interface, so WhatsApp can be added as another adapter.
Design: [`docs/telegram-merchant-bot-spec.md`](docs/telegram-merchant-bot-spec.md).

## Publish businesses to Nostr

Gentifi can publish each business to Nostr, so any Nostr marketplace app or agent
can read it. The owner never sees Nostr: Gentifi makes a key for each business and
signs for it.

| Event | What it holds |
| --- | --- |
| kind 0 (NIP-01) | the business's name, description, place and opening hours |
| kind 30017 (NIP-15 stall) | the business, with collection and delivery as shipping zones |
| kind 30018 (NIP-15 product) | each fixed-price product and variant; stock as `quantity` (0 when sold out, null when not tracked) |
| kind 30402 (NIP-99 listing) | every offering, including services and items priced on request; `status` is `sold` when sold out |
| kind 5 (NIP-09 deletion) | sent instead of the listings while the business is paused |

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
gentifi-mcp --profiles examples --telegram --transport streamable-http --nostr-relay ws://127.0.0.1:7777
```

With `--nostr-relay`, the server publishes every business when it starts and again
when its listing changes: an owner marking items sold out, `/pause` (which deletes
the listings) and `/resume`.

- **Keys** are kept in `~/.gentifi/relay.db`, encrypted (AES-256-GCM) with the master
  key from `.env`. Keep a copy of the master key: without it Gentifi can't sign for
  those businesses again. Signing sits behind a `Signer` interface so a NIP-46 remote
  signer can replace this later.
- **Only relays on this machine are allowed** unless you pass `--allow-public`
  (`--nostr-allow-public` for the server). Events sent to a public relay are copied
  by others and can't be fully taken back.
- Pausing and resuming are kept in memory, like requests: a restarted server
  publishes every business as open.

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
