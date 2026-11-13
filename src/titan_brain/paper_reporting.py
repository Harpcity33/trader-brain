"""Read-only diagnostics for the unreconciled $1,000 paper experiment.

An internally balanced local book is not proof of experiment authority. Until
the host's stock and options histories are reconciled, ALL reports produced by
this module are quarantined. There is deliberately no config switch that can
turn a local seed, a checksum, or a passing arithmetic check into official NAV.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

EXPERIMENT_ID = "TRADER_BRAIN_PAPER_1000"
REPORT_VERSION = "paper-reconciliation-v1"
NY = ZoneInfo("America/New_York")
CENT = Decimal("0.01")


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid number")
    result = Decimal(str(value))
    if not result.is_finite() or result.copy_abs() > Decimal("1e15"):
        raise ValueError("non-finite number")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requires zone")
    return result.astimezone(NY)


def source_identity(path: str | Path, ledger_id: Any = None) -> str:
    # Include location even for copied ledgers with an identical ledger_id.
    value = f"{Path(path).expanduser().resolve()}\0{ledger_id or 'legacy-unidentified'}"
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def report_key(path: str | Path, state: Mapping[str, Any], day: date) -> str:
    return f"eod-quarantine:{EXPERIMENT_ID}:{source_identity(path, state.get('ledger_id'))}:{day}:{REPORT_VERSION}"


def audit_snapshot(state: Mapping[str, Any], *, source_path: str | Path,
                   trading_date: date) -> dict[str, Any]:
    """Inspect a single options-state snapshot without changing it or combining books.

    Unknown stock schemas are evidence only, never interpreted as 100x options.
    Decimal arithmetic allows one cent per comparison for legacy float rounding.
    Daily realized P&L is NOT an equity delta or a percentage return.
    """
    faults: list[str] = []
    observed: dict[str, Any] = {}
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                           allow_nan=True).encode()).hexdigest()
    if state.get("strategy_id") != EXPERIMENT_ID:
        faults.append("EXPERIMENT_ID_MISSING_OR_MISMATCHED")
    if not isinstance(state.get("ledger_id"), str) or not state["ledger_id"].strip():
        faults.append("LEDGER_ID_MISSING")
    if state.get("version") != 2:
        faults.append("UNSUPPORTED_STATE_VERSION")
    try:
        seed, cash = _number(state["starting_equity"]), _number(state["cash"])
        observed.update(starting_equity=float(seed), cash=float(cash))
        if seed != 1000:
            faults.append("STARTING_EQUITY_NOT_1000")
        if cash < 0:
            faults.append("NEGATIVE_CASH")
        expected_cash = seed
    except (KeyError, ValueError, TypeError, InvalidOperation):
        faults.append("INVALID_ACCOUNTING_BASIS")
        expected_cash = cash = None
    positions, closed = state.get("positions"), state.get("closed_trades")
    valid_rows = isinstance(positions, list) and isinstance(closed, list)
    daily_pnl = cumulative_pnl = marked_value = open_cost = Decimal(0)
    ids: set[str] = set()
    if not valid_rows:
        faults.append("INVALID_TRADE_COLLECTIONS")
    else:
        observed.update(open_positions=len(positions), closed_trades=len(closed))
        for is_closed, rows in ((False, positions), (True, closed)):
            for row in rows:
                try:
                    if not isinstance(row, dict) or not isinstance(row.get("contract"), str) or not row["contract"].startswith("O:"):
                        raise ValueError("unsupported trade schema")
                    identity = row["trade_id"]
                    if not isinstance(identity, str) or not identity.strip():
                        raise ValueError("missing trade identity")
                    if identity in ids:
                        faults.append("DUPLICATE_TRADE_ID")
                    ids.add(identity)
                    quantity = _number(row["quantity"])
                    if quantity <= 0 or quantity != quantity.to_integral_value():
                        raise ValueError("invalid quantity")
                    entry, entry_fee, cost = (_number(row[k]) for k in ("entry_fill", "entry_fee", "premium_cost"))
                    if entry <= 0 or min(entry_fee, cost) < 0:
                        raise ValueError("invalid entry")
                    opened = _timestamp(row["opened_at"])
                    if abs(cost - _money(entry * 100 * quantity)) > CENT:
                        faults.append("ENTRY_COST_MISMATCH")
                    if expected_cash is not None:
                        expected_cash -= _money(cost + entry_fee)
                    if is_closed:
                        exit_fill, exit_fee, pnl = (_number(row[k]) for k in ("exit_fill", "exit_fee", "pnl"))
                        ended = _timestamp(row["closed_at"])
                        if min(exit_fill, exit_fee) < 0 or ended < opened:
                            raise ValueError("invalid exit")
                        gross = _money(exit_fill * 100 * quantity - cost)
                        if abs(pnl - _money(gross - entry_fee - exit_fee)) > CENT:
                            faults.append("CLOSED_PNL_MISMATCH")
                        if "gross_pnl" in row and abs(_number(row["gross_pnl"]) - gross) > CENT:
                            faults.append("GROSS_PNL_MISMATCH")
                        if expected_cash is not None:
                            expected_cash += _money(exit_fill * 100 * quantity - exit_fee)
                        cumulative_pnl += pnl
                        if ended.date() == trading_date:
                            daily_pnl += pnl
                        if ended.date() > trading_date:
                            faults.append("SNAPSHOT_AFTER_REPORT_DATE")
                    else:
                        open_cost += _money(cost + entry_fee)
                        mark = _number(row["last_mark"])
                        if mark < 0:
                            raise ValueError("invalid mark")
                        marked_value += mark * 100 * quantity
                    if opened.date() > trading_date:
                        faults.append("SNAPSHOT_AFTER_REPORT_DATE")
                except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
                    faults.append("INVALID_OR_UNSUPPORTED_TRADE_RECORD")
                    valid_rows = False
        if positions:
            # This schema does not store the timestamp of last_mark.
            faults.append("OPEN_POSITION_MARK_FRESHNESS_UNVERIFIED")
        if valid_rows:
            observed.update(recorded_daily_realized_pnl=float(_money(daily_pnl)),
                            recorded_cumulative_realized_pnl=float(_money(cumulative_pnl)))
            if cash is not None:
                observed.update(indicative_equity=float(_money(cash + marked_value)),
                                fill_derived_cash=float(_money(expected_cash)),
                                cash_discrepancy=float(_money(cash - expected_cash)))
                if abs(cash - expected_cash) > CENT:
                    faults.append("CASH_HISTORY_MISMATCH")
                net_based_cash = seed + cumulative_pnl - open_cost
                observed["recorded_net_cash_discrepancy"] = float(_money(cash - net_based_cash))
                if abs(cash - net_based_cash) > CENT:
                    faults.append("RECORDED_NET_HISTORY_MISMATCH")
    daily = state.get("daily", {})
    if isinstance(daily, dict) and isinstance(daily.get(trading_date.isoformat()), dict):
        if daily[trading_date.isoformat()].get("errors"):
            faults.append("RECORDED_RUNTIME_DATA_ERRORS")
    return {
        "schema_version": 1, "report_version": REPORT_VERSION, "experiment_id": EXPERIMENT_ID,
        "trading_date": trading_date.isoformat(), "status": "QUARANTINED",
        "authority_reason": "AUTHORITATIVE_LEDGER_AND_HISTORY_UNRESOLVED",
        "official_equity": None, "official_daily_pnl": None, "official_daily_return_pct": None,
        "learning_eligible": False, "source_id": source_identity(source_path, state.get("ledger_id")),
        "snapshot_sha256": fingerprint, "ledger_id": state.get("ledger_id"),
        "local_accounting_status": "REVIEW" if faults else "BALANCED_LOCAL_ONLY",
        "faults": sorted(set(faults)), "observed": observed,
        "daily_equity_change": None, "daily_baseline_status": "UNVERIFIED",
    }


def quarantine_legacy_eod(outbox: list[dict[str, Any]]) -> bool:
    """Preserve old EOD payloads but never send pending pre-fix totals."""
    changed = False
    for item in outbox:
        legacy = (str(item.get("id", "")).startswith("eod:")
                  or "End of Market Paper Report" in str(item.get("subject", "")))
        if legacy and item.get("status") == "pending":
            item["status"] = "quarantined_unsent"
            item["quarantine_reason"] = "LEGACY_EOD_WITHOUT_RECONCILIATION_PROVENANCE"
            changed = True
    return changed
