# Contributing

Thanks for taking a look. At draft stage, the most valuable contributions are:

1. **Real-world fit.** Try describing a real business (anonymise it if you like).
   Open an issue saying what didn't fit: a kind of offering, a policy, a way of
   taking bookings.
2. **Agent behaviour.** Point an agent at the reference server and tell us where it
   got confused, guessed wrongly or misled the user.
3. **Interoperability.** Mappings to schema.org, booking and till systems, and
   agent-commerce protocols.

## Changing the schema

- Open an issue before a pull request for anything beyond a typo, so the design can
  be discussed first.
- Keep the core small. If a field only matters for one kind of business, propose it
  as an extension (`extensions` with a reverse-domain key) instead.
- Add or update an example that uses the change, and a test.
- `schema/v0.1/profile.schema.json` is the published copy; keep
  `src/gentifi_profile/schema/profile.schema.json` identical (a test checks this).

## Development

```bash
pip install -e ".[dev]"
pytest
gentifi-validate examples/*.json examples/messages/*.json --profile examples/farm-shop.json --strict --now 2026-10-15T20:00:00+01:00
```
