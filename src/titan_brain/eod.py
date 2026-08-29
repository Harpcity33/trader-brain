"""Immutable end-of-day learning packets with no automatic promotion."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .research import classify_process_outcome, write_immutable_artifact


@dataclass(frozen=True)
class EODReviewItem:
    record_id: str
    setup_id: str
    instrument: str
    process_adherent: bool | None
    realized_r: float | None
    mae_r: float | None
    mfe_r: float | None
    exit_efficiency: float | None
    spread_cost: float | None
    slippage_cost: float | None
    route_comparison: Mapping[str, Any]
    lesson: str


def build_eod_learning_packet(
    *,
    trading_date: date,
    created_at: datetime,
    reviews: Sequence[EODReviewItem],
    operational_errors: Sequence[str] = (),
) -> dict[str, Any]:
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    normalized: list[dict[str, Any]] = []
    for item in reviews:
        classification = classify_process_outcome(
            item.process_adherent, realized_r=item.realized_r
        )
        row = asdict(item)
        row.update(classification)
        # Explicitly prevent a profitable violation from becoming a rule input.
        row["promotion_eligible"] = False
        normalized.append(row)
    return {
        "schema_version": 1,
        "trading_date": trading_date.isoformat(),
        "created_at": created_at.isoformat(),
        "artifact_policy": "immutable_create_only",
        "reviews": normalized,
        "operational_errors": list(operational_errors),
        "observation_destination": f"research/observations/{trading_date.isoformat()}-eod.json",
        "automatic_promoted_edge_mutation": False,
        "production_rule_effect": "NONE",
    }


def write_eod_learning_packet(path: str | Path, packet: Mapping[str, Any]) -> Path:
    content = json.dumps(dict(packet), indent=2, sort_keys=True) + "\n"
    return write_immutable_artifact(path, content)


__all__ = ["EODReviewItem", "build_eod_learning_packet", "write_eod_learning_packet"]

