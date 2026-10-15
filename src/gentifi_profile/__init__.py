"""Reference tools for the Gentifi Business Profile specification (draft v0.1)."""

from .schema_loader import SPEC_VERSION, load_schema, validator_for
from .freshness import assess
from .checks import Issue, check_document, detect_kind

__all__ = [
    "SPEC_VERSION",
    "load_schema",
    "validator_for",
    "assess",
    "Issue",
    "check_document",
    "detect_kind",
]
__version__ = "0.1.0"
