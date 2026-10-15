"""gentifi-validate: check profiles, action requests and receipts.

Examples:
    gentifi-validate examples/farm-shop.json
    gentifi-validate request.json --profile examples/farm-shop.json
    gentifi-validate examples/*.json --now 2026-10-15T20:00:00+01:00 --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checks import check_document
from .freshness import parse_time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gentifi-validate", description="Validate Gentifi Business Profile documents.")
    parser.add_argument("files", nargs="+", type=Path, help="JSON files: profiles, action requests or receipts")
    parser.add_argument("--profile", type=Path, help="profile to check action requests against")
    parser.add_argument("--kind", choices=["profile", "request", "receipt"], help="skip auto-detection")
    parser.add_argument("--now", help="evaluate freshness as of this ISO 8601 time (default: now)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    now = parse_time(args.now) if args.now else None
    profile = _load(args.profile) if args.profile else None

    results, failed = [], False
    for path in args.files:
        try:
            doc = _load(path)
        except (OSError, json.JSONDecodeError) as exc:
            results.append({"file": str(path), "kind": None, "issues": [{"level": "error", "path": "$", "message": str(exc)}]})
            failed = True
            continue
        kind, issues = check_document(doc, args.kind, profile=profile, now=now)
        if any(i.level == "error" or args.strict for i in issues):
            failed = True
        results.append({"file": str(path), "kind": kind, "issues": [i.as_dict() for i in issues]})

    if args.json:
        print(json.dumps({"ok": not failed, "results": results}, indent=2))
    else:
        for r in results:
            errors = sum(i["level"] == "error" for i in r["issues"])
            warnings = len(r["issues"]) - errors
            mark = "FAIL" if errors or (args.strict and warnings) else "ok"
            print(f"{mark:4}  {r['file']}  ({r['kind'] or 'unknown'}: {errors} error(s), {warnings} warning(s))")
            for i in r["issues"]:
                print(f"      {i['level']:7} {i['path']}: {i['message']}")
    return 1 if failed else 0


def _load(path: Path):
    return json.loads(path.read_text("utf-8"))


if __name__ == "__main__":
    sys.exit(main())
