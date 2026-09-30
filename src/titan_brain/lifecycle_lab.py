"""Durable order-evidence/recovery lab. No broker client, dispatcher or scheduler.

This module observes supplied records and produces human-review diagnostics.
It cannot submit/cancel orders, change permissions, or protect a real position.
Local SQLite files are its only side effect. Synthetic fixtures drive the tests.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

VERSION = "lifecycle-lab-v1"
TERMINAL = frozenset({"filled", "cancelled", "rejected", "expired"})
STATES = TERMINAL | {"accepted", "pending", "partially_filled", "pending_cancel"}


class ObservationError(ValueError):
    """Fixed diagnostic; do not include raw broker responses or credentials."""


def decimal(value: Any, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ObservationError("INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ObservationError("INVALID_NUMBER") from None
    if not result.is_finite() or (nonnegative and result < 0):
        raise ObservationError("INVALID_NUMBER")
    return result


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ObservationError("INVALID_IDENTIFIER")
    return value


def quantity(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ObservationError("INVALID_QUANTITY")
    return value


def timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ObservationError("AWARE_TIMESTAMP_REQUIRED")
    return value


def is_fresh(value: datetime, now: datetime, seconds: int) -> bool:
    timestamp(value); timestamp(now)
    return 0 <= (now - value).total_seconds() <= seconds


def canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class OrderEvidence:
    """Normalized cumulative broker evidence, NOT an instruction to trade.

    A future provider normalizer must verify field semantics and source identity.
    notional is cumulative executed premium IN DOLLARS including the multiplier,
    excluding fees. Fees are cumulative dollars; quantity is whole contracts.
    """
    account: str
    ref: str
    broker_id: str
    contract: str
    side: str
    requested: int
    filled: int
    notional: str
    fees: str
    state: str
    observed_at: datetime


class Journal:
    """Append evidence and retain cumulative outcomes across local restarts."""
    def __init__(self, path: Path, *, account: str):
        identifier(account)
        self.account = account
        self.path = Path(path)
        if self.path.is_symlink():
            raise ObservationError("SYMLINK_DATABASE_REFUSED")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        elif self.path.stat().st_mode & 0o077:
            raise ObservationError("DATABASE_PERMISSIONS_UNSAFE")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS expected (
                    ref TEXT PRIMARY KEY, contract TEXT NOT NULL, side TEXT NOT NULL,
                    requested INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS latest (
                    ref TEXT PRIMARY KEY, broker_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    digest TEXT PRIMARY KEY, ref TEXT NOT NULL, body TEXT NOT NULL);
            """)
            binding = hashlib.sha256(account.encode()).hexdigest()
            old = db.execute("SELECT value FROM metadata WHERE name='account'").fetchone()
            if old and old[0] != binding:
                raise ObservationError("DATABASE_ACCOUNT_MISMATCH")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('account', ?)", (binding,))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def expect(self, ref: str, contract: str, side: str, requested: int) -> None:
        """Register an observation expectation; this does NOT transmit an order."""
        identifier(ref); identifier(contract); quantity(requested, positive=True)
        if side not in {"buy", "sell"}:
            raise ObservationError("UNSUPPORTED_SIDE")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM expected WHERE ref=?", (ref,)).fetchone()
            if old and (old["contract"], old["side"], old["requested"]) != (contract, side, requested):
                raise ObservationError("REFERENCE_REUSE_CONFLICT")
            db.execute("INSERT OR IGNORE INTO expected VALUES (?,?,?,?)", (ref, contract, side, requested))

    def observe(self, event: OrderEvidence, *, now: datetime) -> dict[str, Any]:
        """Apply exactly one cumulative observation transactionally, never send.

        Reordered records, changed terminal evidence and unexplained economic
        corrections require review; the prior record is preserved on failure.
        """
        if event.account != self.account:
            raise ObservationError("ACCOUNT_MISMATCH")
        for value in (event.ref, event.broker_id, event.contract): identifier(value)
        quantity(event.requested, positive=True); quantity(event.filled)
        if event.side not in {"buy", "sell"} or event.state not in STATES:
            raise ObservationError("UNSUPPORTED_ORDER_EVIDENCE")
        if not is_fresh(event.observed_at, now, 15):
            raise ObservationError("ORDER_OBSERVATION_STALE")
        notional, fees = decimal(event.notional, nonnegative=True), decimal(event.fees, nonnegative=True)
        if event.filled > event.requested or (event.filled == 0 and notional != 0) or (event.filled > 0 and notional <= 0):
            raise ObservationError("INVALID_CUMULATIVE_FILL")
        if (event.state == "filled" and event.filled != event.requested
                or event.state == "partially_filled" and not 0 < event.filled < event.requested
                or event.state in {"rejected", "expired"} and event.filled > 0):
            raise ObservationError("STATE_FILL_CONFLICT_REVIEW_REQUIRED")
        record = asdict(event)
        del record["account"]  # exact account binding is stored only as a hash
        record.update(notional=str(notional), fees=str(fees), observed_at=event.observed_at.isoformat())
        body = canonical(record)
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            expected = db.execute("SELECT * FROM expected WHERE ref=?", (event.ref,)).fetchone()
            if not expected or (expected["contract"], expected["side"], expected["requested"]) != (event.contract, event.side, event.requested):
                raise ObservationError("UNEXPECTED_ORDER_IDENTITY")
            previous_row = db.execute("SELECT body FROM latest WHERE ref=?", (event.ref,)).fetchone()
            previous = json.loads(previous_row[0]) if previous_row else None
            duplicate = db.execute("SELECT 1 FROM events WHERE digest=?", (digest,)).fetchone()
            if duplicate:
                return {"duplicate": True, "incremental_filled": 0, "broker_actions": 0}
            old_filled = 0
            if previous:
                if previous["broker_id"] != event.broker_id:
                    raise ObservationError("BROKER_ID_CHANGED")
                old_filled = previous["filled"]
                if event.observed_at <= datetime.fromisoformat(previous["observed_at"]):
                    raise ObservationError("ORDERING_REVIEW_REQUIRED")
                if event.filled < old_filled or notional < decimal(previous["notional"]) or fees < decimal(previous["fees"]):
                    raise ObservationError("CUMULATIVE_REGRESSION")
                if event.filled == old_filled and notional != decimal(previous["notional"]):
                    raise ObservationError("ECONOMIC_CORRECTION_REVIEW_REQUIRED")
                if event.filled > old_filled and notional <= decimal(previous["notional"]):
                    raise ObservationError("FILL_WITHOUT_INCREMENTAL_NOTIONAL")
                if previous["state"] in TERMINAL and any(record[k] != previous[k] for k in ("state", "filled", "notional", "fees")):
                    raise ObservationError("TERMINAL_CORRECTION_REVIEW_REQUIRED")
            collision = db.execute("SELECT ref FROM latest WHERE broker_id=?", (event.broker_id,)).fetchone()
            if collision and collision[0] != event.ref:
                raise ObservationError("BROKER_ID_REUSED")
            db.execute("INSERT INTO events VALUES (?,?,?)", (digest, event.ref, body))
            db.execute("INSERT INTO latest VALUES (?,?,?) ON CONFLICT(ref) DO UPDATE SET body=excluded.body",
                       (event.ref, event.broker_id, body))
        return {"duplicate": False, "incremental_filled": event.filled - old_filled,
                "pending_quantity": event.requested - event.filled if event.state not in TERMINAL else 0,
                "broker_actions": 0}

    def snapshot(self) -> dict[str, Any]:
        with self.db() as db:
            db.execute("BEGIN")
            expected = [dict(x) for x in db.execute("SELECT * FROM expected ORDER BY ref")]
            latest = [json.loads(x[0]) for x in db.execute("SELECT body FROM latest ORDER BY ref")]
            count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"expected": expected, "latest": latest, "evidence_events": count, "broker_actions": 0}

    def recover(self, *, account: str, positions: Mapping[str, int], observed_at: datetime,
                now: datetime, complete: bool) -> dict[str, Any]:
        """Compare persisted fills with a complete fresh supplied position snapshot.

        This is a restart diagnostic, not proof of broker authentication, order
        protection, cash reconciliation, or permission to start a live service.
        """
        if account != self.account:
            raise ObservationError("ACCOUNT_MISMATCH")
        if complete is not True or not is_fresh(observed_at, now, 15):
            return {"status": "REVIEW_REQUIRED", "issues": ["POSITION_SNAPSHOT_INCOMPLETE_OR_STALE"],
                    "broker_actions": 0, "automatic_resubmission": False}
        for symbol, value in positions.items(): identifier(symbol); quantity(value)
        snapshot = self.snapshot()
        observed = {x["ref"]: x for x in snapshot["latest"]}
        holdings: dict[str, int] = {}
        pending, unknown, issues = [], [], []
        for order in snapshot["expected"]:
            item = observed.get(order["ref"])
            if item is None:
                unknown.append(order["ref"])
                continue
            sign = 1 if item["side"] == "buy" else -1
            holdings[item["contract"]] = holdings.get(item["contract"], 0) + sign * item["filled"]
            if item["state"] not in TERMINAL:
                pending.append({"ref": item["ref"], "remaining": item["requested"] - item["filled"]})
                if not is_fresh(datetime.fromisoformat(item["observed_at"]), now, 15):
                    issues.append("PENDING_ORDER_OBSERVATION_STALE")
        mismatches = [symbol for symbol in sorted(set(holdings) | set(positions))
                      if holdings.get(symbol, 0) != positions.get(symbol, 0)]
        if unknown: issues.append("UNKNOWN_ORDER_DELIVERY")
        if pending: issues.append("WORKING_ORDERS_REQUIRE_RECONCILIATION")
        if mismatches: issues.append("POSITION_MISMATCH")
        if any(v < 0 for v in holdings.values()): issues.append("UNEXPLAINED_SHORT_EXPOSURE")
        return {"status": "REVIEW_REQUIRED" if issues else "RECONCILED_NOT_EXECUTION_AUTHORITY",
                "issues": sorted(set(issues)), "observed_fill_holdings": holdings,
                "unknown_references": unknown, "working_orders": pending,
                "mismatched_contracts": mismatches, "broker_actions": 0,
                "automatic_resubmission": False}


def daily_budget(*, starting_equity: str, current_equity: str, net_external_flows: str) -> dict[str, Any]:
    """Report the user's 5% daily loss preference and 10% aspirational goal.

    The $500 deposit is not assumed completed. No old weekly/per-trade ceilings
    are inherited. This report never places trades or changes a running policy.
    """
    starting = decimal(starting_equity, nonnegative=True)
    current = decimal(current_equity, nonnegative=True)
    flows = decimal(net_external_flows)
    if starting <= 0: raise ObservationError("STARTING_EQUITY_REQUIRED")
    pnl = current - starting - flows
    loss_limit, goal = starting * Decimal(".05"), starting * Decimal(".10")
    return {"daily_loss_threshold": str(loss_limit), "daily_gain_goal": str(goal),
            "flow_adjusted_pnl": str(pnl), "loss_threshold_reached": pnl <= -loss_limit,
            "gain_goal_reached": pnl >= goal, "goal_is_guaranteed": False,
            "loss_is_guaranteed_capped": False, "broker_actions": 0}


def protection_review(*, contract: str, position_qty: int, position_at: datetime,
                      quote_contract: str, bid: str, ask: str, bid_size: int, quote_at: datetime,
                      stop: str, target: str, close_cutoff: datetime, now: datetime,
                      closing_order_pending: bool, working_protection_confirmed: bool) -> dict[str, Any]:
    """Detect exit/protection conditions for HUMAN REVIEW, never send an exit.

    A stop threshold is not a placed stop order. The pending-close flag avoids
    suggesting a competing close, but complete broker reconciliation is external.
    """
    identifier(contract); quantity(position_qty); timestamp(now); timestamp(close_cutoff)
    if type(closing_order_pending) is not bool or type(working_protection_confirmed) is not bool:
        raise ObservationError("BOOLEAN_EVIDENCE_REQUIRED")
    if not is_fresh(position_at, now, 15):
        return {"status": "REVIEW_REQUIRED", "reasons": ["POSITION_OBSERVATION_STALE"], "broker_actions": 0}
    if position_qty == 0:
        return {"status": "NO_POSITION_OBSERVED", "reasons": [], "broker_actions": 0}
    reasons = []
    if not working_protection_confirmed: reasons.append("WORKING_PROTECTION_NOT_VERIFIED")
    if now >= close_cutoff: reasons.append("CLOSEOUT_TIME_REACHED")
    try:
        b, a, s, t = (decimal(v, nonnegative=True) for v in (bid, ask, stop, target))
        quantity(bid_size)
        if not 0 <= b <= a or a <= 0 or not 0 < s < t:
            raise ObservationError("INVALID_PRICE_RELATION")
        if quote_contract != contract or not is_fresh(quote_at, now, 30) or bid_size < position_qty:
            raise ObservationError("EXIT_QUOTE_UNUSABLE")
        if b <= s: reasons.append("STOP_THRESHOLD_OBSERVED")
        elif b >= t: reasons.append("TARGET_THRESHOLD_OBSERVED")
    except ObservationError:
        reasons.append("EXIT_QUOTE_UNUSABLE")
    if closing_order_pending: reasons.append("RECONCILE_EXISTING_CLOSE_BEFORE_ANOTHER")
    return {"status": "REVIEW_REQUIRED" if reasons else "NO_TRIGGER_OBSERVED",
            "reasons": sorted(set(reasons)), "broker_actions": 0,
            "protection_order_placed": False, "position_closed": False}
