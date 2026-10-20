# Gentifi Telegram Merchant Bot — Flow Spec

20 Oct 2026 · Finn

## Principles

A merchant runs their whole Gentifi presence from one Telegram chat; Nostr and payments happen underneath without them noticing.

- **Merchant never sees Nostr.** Gentifi generates and holds each merchant's keypair (encrypted, behind a NIP-46 remote signer) and publishes for them. A "claim your key" export comes later.
- **Gentifi never touches money.** Invoices come from the merchant's own Lightning address; fiat is paid on collection or through the merchant's own payment link. Refunds and disputes sit between merchant and customer.
- **Telegram first.** Free Bot API, inline buttons, no 24-hour messaging window or template approval. WhatsApp is a second adapter behind the same messaging interface.
- **One tap per decision.** Every merchant action is a button; typing is only for edits and onboarding material.
- **Silence is safe.** No reply means the order expires or the listing is marked stale, never a false confirmation.

## Onboarding

A merchant goes from QR code to live listing in about five minutes, with three taps and one photo.

1. **Start.** Merchant scans a QR code or opens `t.me/GentifiBot?start=<invite-code>`. The invite code ties the Telegram chat ID to a merchant record. The bot can only message them after this step.
2. **Welcome.** Bot: "Hi, I'll get [Business] found by people's AI assistants. Send me a photo of your menu or price list, or a link to your website." Buttons: `Send photo` · `Send link` · `Talk to a person`
3. **Extract.** AI turns the photo or link into a draft Gentifi Business Profile (name, address, hours, items, prices, collection or delivery). The validator checks it; missing required fields become follow-up questions.
4. **Fill gaps.** One question per message, with buttons where possible. Example: "Do you offer collection, delivery or both?" `Collection` · `Delivery` · `Both`
5. **Review.** Bot sends a short summary card: business name, hours, item count, three sample items. Buttons: `Looks right — go live` · `Edit something` · `Start again`
6. **Payment setup.** "How do customers pay you?" `On collection (cash/card)` · `My payment link` · `Bitcoin (Lightning address)` (multi-select). For Lightning, the merchant pastes an address like `shop@strike.me`; Gentifi checks it resolves before saving.
7. **Go live.** Gentifi signs and publishes the profile to Nostr. Bot: "You're live. When an AI assistant sends an order, I'll message you here." Buttons: `See my listing` · `Help`

## Order flow

Every order waits on one merchant tap; nothing is confirmed to the customer until the merchant accepts.

```
AI agent places order (MCP prepare_order)
  -> order created, state = pending
  -> Telegram prompt sent to merchant
  -> merchant taps:
       Accept        -> accepted -> payment request sent -> fulfilled (paid and collected/delivered)
       Suggest time  -> time_suggested -> counter-offer to agent -> customer agrees -> accepted
                                                                -> customer declines -> cancelled
       Decline       -> declined -> agent informed
       (no reply)    -> expired at end of window -> agent informed
```

**What the merchant sees**

> New order via AI assistant
> 2 × sourdough loaf, 6 × eggs — £11.00
> Collection today 16:30 · pays on collection
> `Accept` · `Decline` · `Suggest time`

- **Accept:** bot replies "Confirmed, customer told." Gentifi returns confirmation plus payment details (invoice, link or pay on collection) to the agent through the MCP.
- **Suggest time:** bot offers buttons (`+30 min`, `+1 hr`, `Tomorrow`, `Type a time`); the new time goes back to the agent as a counter-offer.
- **Decline:** optional reason buttons (`Sold out`, `Too busy`, `Other`); a sold-out reason also marks the item sold out.
- **No reply:** at expiry the bot edits the message to "Expired" and the agent is told the order wasn't confirmed.
- **After accept:** buttons `Mark paid` · `Collected` / `Delivered` · `Cancel` stay on the message until the order closes.

**Agent side (MCP):** `prepare_order` returns an order ID with state `pending`; the agent polls `get_order_status` or receives the update, then gets payment details once accepted.

## Payment handoff

Gentifi only ever passes a payment request from merchant to customer; money never lands in a Gentifi account.

| Method | How the request is made | How "paid" is known |
| --- | --- | --- |
| Lightning address | On accept, Gentifi calls the merchant's LNURL-pay endpoint for the exact amount and hands the invoice to the customer's agent | LUD-21 verify URL if the wallet supports it; otherwise merchant taps `Mark paid` |
| Merchant payment link | The merchant's own SumUp/Square/Stripe link is included in the confirmation | Merchant taps `Mark paid` |
| On collection | Confirmation says "Pay on collection" with the total | Not tracked; order closes on `Collected` tap |

- Invoice memo carries the Gentifi order ID so the merchant can match payments.
- Lightning invoices expire (default 15 minutes); the agent can request a fresh one through the MCP.
- Terms of service state that payment, refunds and disputes are between merchant and customer.

## Freshness nudges and quick edits

The bot asks short yes/no questions on a schedule so listings stay true without the merchant remembering to update them.

- **Daily open check** (morning, opening days only): "Open as usual today?" `Yes` · `Closed today` · `Different hours`
- **Stock check** for items marked perishable or limited: "Still got eggs today?" `Yes` · `Sold out` · `Low`
- **Weekly profile check:** "Anything changed this week — prices, hours, items?" `All good` · `Update something`
- **No reply:** after the freshness window the affected availability record is marked stale (freshness metadata in the profile), so agents see "unconfirmed" rather than a false "in stock".
- **Quick edits anytime:** the merchant can type plain text ("sourdough is £4.50 now", "closed Monday"). AI drafts the change, bot shows it, merchant taps `Confirm`. Every confirmed change republishes the Nostr events.
- **Pause:** `/pause` hides the listing from agents instantly (holiday, illness, sold out for the day).

## Commands, states and data model

Six commands, one order state machine and four tables cover the whole MVP.

**Commands**

| Command | Does |
| --- | --- |
| `/start` | Links chat to merchant (via invite code) and begins onboarding |
| `/listing` | Shows the live profile summary and a link to view it |
| `/edit` | Starts a guided edit (hours, items, prices, payment methods) |
| `/orders` | Lists today's open orders with their buttons |
| `/pause` / `/resume` | Hides or restores the listing for agents |
| `/help` | Short help plus `Talk to a person` |

**Order states:** `pending` → `accepted` | `declined` | `time_suggested` | `expired`; then `accepted` → `paid` (optional) → `collected` / `delivered` | `cancelled`. Only `accepted` triggers a payment request.

**Data model**

| Table | Key fields |
| --- | --- |
| merchants | id, business_name, telegram_chat_id, whatsapp_number (later), nostr_pubkey, signer_ref, lightning_address, payment_link, payment_methods, status (onboarding, live, paused) |
| profiles | merchant_id, profile_json (Gentifi Business Profile), version, published_event_ids, published_at |
| orders | id, merchant_id, agent_ref, items, total, fulfilment, requested_time, state, expires_at, invoice, payment_status, telegram_message_id |
| nudges | merchant_id, kind (open, stock, weekly), item_ref, sent_at, answered_at, answer |

The messaging layer is an interface (`send_prompt`, `on_button`, `on_text`) with a Telegram adapter now and a WhatsApp adapter later; order and profile logic never call Telegram directly.

## Build order and open questions

Build the order loop first with one real merchant; onboarding automation and Nostr publishing can follow once orders work end to end.

1. Telegram bot skeleton: `/start` with invite code, button handling, messaging interface with Telegram adapter.
2. Order loop: `prepare_order` in the MCP creates a pending order, bot sends the prompt, buttons drive the state machine, expiry job runs.
3. Payment handoff: LNURL-pay invoice fetch, payment link, pay on collection, `Mark paid`.
4. Manual onboarding: you fill the profile by hand for the first merchant; bot only does the review and go-live steps.
5. Nostr publishing: profile to NIP-15/NIP-99 events, remote signer, Gentifi relay plus public relays.
6. AI onboarding: photo/link extraction, gap questions, validator loop.
7. Freshness nudges and quick edits.
8. WhatsApp adapter.

**Open questions**

- [ ] Order expiry window: 10, 15 or 30 minutes, and does it vary by category?
- [ ] Hosting for the bot, relay and signer: always-on VPS or the M4 Mac Mini to start?
- [ ] Key custody: self-hosted NIP-46 signer, or encrypted keys in the database for the MVP?
- [ ] Which merchant category and area for the first ten?
- [ ] Telegram bot name and handle (check `@GentifiBot` availability).
