"""Clearly labeled synthetic replay; replace adapter with Massive data for validation."""
import json
from datetime import datetime, timezone

from trader_brain import Candidate, WEIGHTS, screen_and_rank


def factors(seed):
    return {name: min(1, (seed + i) / 12) for i, name in enumerate(WEIGHTS)}


universe = [
    Candidate("ALFA", 12.4, 2_200_000, factors(5), True),
    Candidate("BRVO", 8.1, 1_100_000, factors(4), False),
    Candidate("CRWN", 21.0, 950_000, factors(3), True),
    Candidate("DASH", 6.2, 800_000, factors(2), False),
    Candidate("ECHO", 35.0, 4_500_000, factors(1), False),
    Candidate("FIVE", 5.0, 3_000_000, factors(9), True),
    Candidate("LOWV", 9.0, 749_999, factors(9), True),
]

result = screen_and_rank(universe)
manifest = {
    "run_type": "synthetic_full_input_universe_dry_run",
    "validation_status": "not_yet_validated",
    "reason": "Massive credentials/data unavailable; synthetic fixtures are not market evidence",
    "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "decision_timestamp_et": "2026-08-20T03:55:00-04:00",
    "data_timestamp_et": "2026-08-20T03:54:59-04:00",
    "universe_size": len(universe),
    "accepted_count": len(result["accepted"]),
    "rejected_count": len(result["rejected"]),
    "accepted": result["accepted"],
    "rejected": result["rejected"],
    "api_failures": ["Massive adapter not configured"],
    "retries": 0,
}
print(json.dumps(manifest, indent=2, sort_keys=True))
