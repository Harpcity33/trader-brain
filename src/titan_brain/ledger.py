"""Append-only, strategy-isolated Titan ledger records.

The three ledgers use the same auditable record envelope but cannot be mixed.
The aggressive lab is structurally denied live-order authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .models import Instrument, SetupID


class LedgerKind(str, Enum):
    LIVE_EQUITY = "live-equity"
    LIVE_OPTIONS = "live-options"
    PAPER_AGGRESSIVE = "paper-aggressive"


@dataclass(frozen=True)
class TradeLedgerRecord:
    trade_id: str
    strategy_id: str
    ledger_kind: LedgerKind
    setup_id: SetupID
    instrument: Instrument
    recorded_at: datetime
    planned_risk_dollars: float
    planned_risk_pct: float
    stress_risk_dollars: float
    stress_risk_pct: float
    setup_score: float
    execution_score: float
    market_regime: str
    actual_route: Mapping[str, Any]
    shadow_routes: Mapping[str, Any]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    broker_evidence_revision: str | None = None
    live_order_authority: bool = False

    def __post_init__(self) -> None:
        if not self.trade_id.strip() or not self.strategy_id.strip():
            raise ValueError("trade_id and strategy_id are required")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.ledger_kind is LedgerKind.PAPER_AGGRESSIVE and self.live_order_authority:
            raise ValueError("paper-aggressive records can never have live authority")
        if self.ledger_kind is LedgerKind.LIVE_EQUITY and self.instrument is not Instrument.STOCK:
            raise ValueError("live-equity ledger accepts stock instruments only")
        if self.ledger_kind is LedgerKind.LIVE_OPTIONS and self.instrument not in {
            Instrument.LONG_CALL,
            Instrument.LONG_PUT,
            Instrument.DEBIT_SPREAD,
        }:
            raise ValueError("live-options ledger accepts option instruments only")
        for name in ("planned_risk_dollars", "planned_risk_pct", "stress_risk_dollars", "stress_risk_pct"):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} cannot be negative")
        for name in ("setup_score", "execution_score"):
            if not 0 <= float(getattr(self, name)) <= 100:
                raise ValueError(f"{name} must be in [0, 100]")

    def to_json_object(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ledger_kind"] = self.ledger_kind.value
        payload["setup_id"] = self.setup_id.value
        payload["instrument"] = self.instrument.value
        payload["recorded_at"] = self.recorded_at.isoformat()
        return payload


def append_jsonl_create_or_append(
    path: str | Path,
    record: TradeLedgerRecord,
    *,
    expected_kind: LedgerKind,
) -> Path:
    """Append one fsynced JSON line without reading or rewriting old records."""

    if record.ledger_kind is not expected_kind:
        raise ValueError("record ledger kind does not match destination")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_json_object(), sort_keys=True, separators=(",", ":")) + "\n"
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    descriptor = os.open(destination, flags, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination


__all__ = ["LedgerKind", "TradeLedgerRecord", "append_jsonl_create_or_append"]

