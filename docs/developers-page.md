<!--
Copy for the Gentifi Business Profile section on gentifi.com/standards.
The repo README holds the detail.
-->

## Gentifi Business Profile (draft 0.1)

An open format that describes what a local business offers, what's available right
now, and what an AI agent may do on its behalf: hold, book, order, cancel, check
status or enquire. It's designed for businesses without live inventory systems: the
farm shop, the plumber, the independent café.

**Every piece of availability says how sure you can be.** Each stock level,
bookable slot or recurring pattern carries when it was last confirmed, how confident
that is, and where it came from: the owner, a synced booking or till system, or a
message the owner sent. An agent can then tell whether a booking can be confirmed
straight away, whether the business needs to confirm it first, or whether to warn
the customer that the information may be out of date.

**The customer always confirms.** Every hold, booking or order carries how and when
the customer agreed to it, in their AI app or on a confirmation page. Requests come
back with a clear receipt: confirmed, pending (with a time the business will reply
by), or declined with a reason and alternatives. Retries are safe.

**Use it with any agent.** The same profile can be served over MCP, published as
structured data on a website, or carried by other agent protocols. The repository
has the JSON Schema with worked examples (a farm shop and a mobile plumber, both
fictional), a validator that checks ids, slots and stale data as well as structure,
and a reference MCP server for trying it out. The reference server isn't the live
Gentifi service: that runs on the Gentifi toolkit, which will publish profiles in
this format.

Draft 0.1, Apache 2.0. Schema: `https://gentifi.com/standards/profile-0.1.schema.json`.
Repository: [github.com/SelfBankt/gentifi-profile](https://github.com/SelfBankt/gentifi-profile).
If you've tried describing a real business and something didn't fit, please
[open an issue](https://github.com/SelfBankt/gentifi-profile/issues).
