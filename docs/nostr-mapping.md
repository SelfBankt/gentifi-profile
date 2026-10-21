# Gentifi Business Profile on Nostr

How a profile becomes Nostr events (`src/gentifi_profile/nostr/mapping.py`), so any
Nostr marketplace app or agent can read a Gentifi business without guessing.

Gentifi publishes for three kinds of reader at once:

- **NIP-15 apps** (stall and products, kinds 30017 and 30018). NIP-15 is deprecated,
  but some apps still read it.
- **NIP-99 apps** (classified listings, kind 30402).
- **Market-spec apps**: the [GammaMarkets market spec](https://github.com/GammaMarkets/market-spec)
  builds a full shop on NIP-99 with extra tags on kind 30402, collections (30405) and
  shipping options (30406). The spec is still a draft; this follows it as of October 2026.

Every event carries the NIP-32 label `["L", "com.gentifi"]` and `["l", <business id>, "com.gentifi"]`.
Each business has its own key, held by Gentifi; the owner never sees Nostr.

## The business

| Profile | Kind 0 (NIP-01) | Kind 30405 collection | Kind 30017 stall |
| --- | --- | --- | --- |
| `business.id` | (label) | `d` | `d`, `id` |
| `business.name` | `name` | `title` | `name` |
| `business.description`, place, opening hours | `about` | `summary` (description), content (all three) | `description` |
| `contact.website` | `website` | | |
| locality and region | | `location` | |
| `location.geo` | | `g` (geohash, 7 characters) | |
| every listing except sizes | | `a` = `30402:<pubkey>:<d>` | |
| collection and delivery | | `shipping_option` = `30406:<pubkey>:<id>` | `shipping` zones |

## Offerings (kind 30402)

One listing per offering. An offering with variants is a `variable` product, and each
variant a `variation` with `d` = `<offering id>:<variant id>` and an `a` tag pointing at
its product.

| Profile | Tag |
| --- | --- |
| offering (and variant) id | `d` |
| name, with the variant's name in brackets | `title` |
| price text and business name | `summary` |
| `price` when `type` is `fixed` | `price` = `[amount, currency]`, e.g. `["3.50", "GBP"]` |
| `price.per` (other than "each") | `["spec", "Price per", <per>]` |
| variants | `["type", "simple" or "variable" or "variation", "physical"]` (`digital` only when the only fulfilment is `remote`) |
| stock `status` `preorder` | `["visibility", "pre-order"]`, otherwise `on-sale` |
| stock `quantity` | `stock` (0 when `out_of_stock` or `out_of_season`; no tag when not tracked) |
| stock `status` | `status` = `sold` or `active` (NIP-99); a `variable` product is `sold` when every size is |
| `attributes` | `["spec", <name>, <value>]` |
| stock status and when it was checked | `["spec", "Stock", …]`, `["spec", "Stock checked", …]` |
| `images` | `["image", <url>, "", <order>]` |
| business tags, offering kind, true attributes | `t` |
| locality and region, geo | `location`, `g` |
| stock `freshness` | `["freshness", observed_at, confidence, source]` (Gentifi's own) |
| `fulfilment` `collection` or `delivery` | `shipping_option` = `30406:<pubkey>:collection` or `…:delivery` |
| the business | `a` = `30405:<pubkey>:<business id>` (not on a `variation`) |

Items priced on request, "from" a price or as a range have no `price` tag. NIP-99
allows this; the market spec requires a price, so its apps may leave them out.

## Shipping options (kind 30406)

| Profile | `collection` | `delivery` |
| --- | --- | --- |
| `d` | `collection` | `delivery` |
| `service` | `pickup` | `standard` |
| `price` | `0` | `policies.delivery.fee` |
| `country` | address country | address country |
| `location`, `g` | street, locality and postcode for premises open to the public (otherwise locality and region); geohash | |
| content | where to collect, opening hours | postcodes or radius, days, fee, free-over and minimum order |

With no country in the address there are no shipping options.

## Paused or back in onboarding

A NIP-09 deletion (kind 5) names every stall, product, listing, collection and shipping
option. Going live or resuming publishes them again with a later `created_at`.

## Not yet

- **Orders from Nostr.** Market-spec and NIP-15 apps can send an order as an encrypted
  message (market spec kind 16 over NIP-17). Gentifi doesn't read these yet; each
  listing says to order through an AI assistant. `payment_preference` is left unset.
- **Reading businesses from Nostr** into a profile, for shops already selling there.
- NIP-89 app recommendations, reviews (kind 31555), NIP-46 remote signing.
