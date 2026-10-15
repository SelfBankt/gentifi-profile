<!--
Copy for the Developers page on the Gentifi website.
Replace REPO_URL with the public GitHub link before publishing.
About 350 words; the repo README holds the detail.
-->

# Gentifi for developers

## An open format for businesses that AI agents can rely on

**Gentifi Business Profile** is an open, protocol-neutral format that describes what
a local business offers, what's available right now, and what an AI agent can do on
its behalf: hold, book, order, cancel, check status or enquire.

It's built for the businesses platforms tend to miss: the farm shop, the plumber,
the independent café. They rarely have live inventory systems, so the format is
designed around that.

**Draft v0.1 — open for feedback.** Apache 2.0 licensed.

## Every piece of availability says how sure you can be

Each stock level, bookable slot or recurring pattern carries when it was last
confirmed, how confident that is, and where it came from: the owner, a synced
booking system, or a WhatsApp message read by AI. Agents can then decide whether to:

- **book straight away** — confirmed and current
- **ask the business to confirm first** — with a stated response time
- **warn the user** — the information may be out of date

```json
{
  "type": "slot",
  "offering_id": "farm_tour",
  "start": "2026-10-17T10:00:00+01:00",
  "remaining": 4,
  "freshness": {
    "observed_at": "2026-10-15T19:30:00+01:00",
    "confidence": "confirmed",
    "source": "integration"
  }
}
```

Bookings and orders return a clear receipt — confirmed, pending (with a time the
business will reply by), or declined with a reason and alternatives. Retries are
safe: every request carries an idempotency key.

## Use it with any agent

The same profile can be served over MCP, published as structured data on a
website, or carried by other agent protocols. The repository includes:

- **The schema** (JSON Schema 2020-12) with worked examples
- **A validator** that checks ids, slots, policies and stale data, not just structure
- **A reference MCP server** you can point Claude, ChatGPT or any MCP client at

```bash
git clone REPO_URL && cd gentifi-profile
pip install -e ".[server]"
gentifi-mcp --profiles examples --demo --now 2026-10-15T20:00:00+01:00
# --now freezes the clock so the example data stays fresh
```

[View on GitHub →](REPO_URL)

## Help shape it

This is a draft, and it will change based on what breaks in real use. If you've
tried to describe a real business and something didn't fit, or an agent got
confused, please [open an issue](REPO_URL/issues).

Running a business rather than building agents? Gentifi can set up and keep your
profile current for you — [Gentifi your business →](/)
