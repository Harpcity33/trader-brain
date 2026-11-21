#!/usr/bin/env python3
"""Read-only local evidence check. Never sends email, migrates, or chooses a NAV."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from titan_brain.paper_reporting import EXPERIMENT_ID, audit_snapshot, source_identity


def inspect_source(path: Path, trading_date: date) -> dict:
    result = {"source_id": source_identity(path), "status": "QUARANTINED",
              "learning_eligible": False, "official_equity": None}
    try:
        raw = path.read_bytes()  # Do not load_state: no seeding/rollover/rewrite.
        result["file_sha256"] = hashlib.sha256(raw).hexdigest()
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("invalid state root")
        result.update(audit_snapshot(state, source_path=path, trading_date=trading_date))
    except (OSError, ValueError, TypeError, UnicodeError):
        result["faults"] = ["SOURCE_MISSING_UNREADABLE_OR_INVALID"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True,
                        help="Path to a preserved state snapshot; repeat for each source")
    args = parser.parse_args()
    reports = [inspect_source(p.expanduser(), args.date) for p in args.source]
    print(json.dumps({"experiment_id": EXPERIMENT_ID, "status": "QUARANTINED",
                      "official_equity": None, "official_daily_pnl": None,
                      "learning_eligible": False, "sources": reports,
                      "note": "Sources are preserved separately. No combined balance or authority is inferred."},
                     indent=2, sort_keys=True, allow_nan=False))
    return 2  # A diagnostic report is not successful historical reconciliation.


if __name__ == "__main__":
    raise SystemExit(main())
