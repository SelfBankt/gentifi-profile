"""Load the bundled JSON Schema and build validators for its parts."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SPEC_VERSION = "0.1"

# Top-level documents and the $defs entry that validates each one.
DEFINITIONS = {
    "profile": None,  # the root schema
    "request": "ActionRequest",
    "receipt": "Receipt",
}


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    text = resources.files("gentifi_profile").joinpath("schema/profile.schema.json").read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=None)
def validator_for(kind: str) -> Draft202012Validator:
    """Return a validator for 'profile', 'request' or 'receipt'."""
    if kind not in DEFINITIONS:
        raise ValueError(f"unknown document kind: {kind!r}")
    schema = load_schema()
    definition = DEFINITIONS[kind]
    if definition is not None:
        # Validate against one $defs entry while keeping $defs resolvable.
        schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(schema, format_checker=FormatChecker())
