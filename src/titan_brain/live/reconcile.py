"""Authoritative account reconciliation for the live execution lifecycle.

This module is deliberately transport-free.  A broker adapter obtains one
normalized :class:`AccountSnapshot`; the reconciler proves whether that
snapshot is safe to act on and imports only orders that are tied to durable
local intents.  Anything else is surfaced as manual/external activity rather
than silently adopted.

An UNKNOWN submission is never retried here.  It can be resolved only by a
matching broker order in strictly newer evidence, or by an explicit negative
reference-id lookup in a complete, strictly newer snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import json
from typing import Iterable, Mapping

from .broker.base import (
    AccountSnapshot,
    BrokerCapabilities,
    OrderSnapshot,
)
from .models import BrokerOrder, BrokerOrderState, Fill, IntentState
from .money import whole_shares
from .state import LiveStateStore, object_hash


class ReconciliationError(RuntimeError):
    """Base error for contradictory or unusable reconciliation evidence."""


class ReconciliationConflict(ReconciliationError):
    """Broker evidence conflicts with a durable local identity or revision."""


class ReconciliationPhase(str, Enum):
    STARTUP = "STARTUP"
    CONTINUOUS = "CONTINUOUS"


class ActivityOwner(str, Enum):
    MANUAL = "MANUAL"
    OTHER_AGENT = "OTHER_AGENT"


class UnknownResolutionState(str, Enum):
    MATCHED_ORDER = "MATCHED_ORDER"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
    WAITING_FOR_NEWER_EVIDENCE = "WAITING_FOR_NEWER_EVIDENCE"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ExternalActivity:
    broker_order_id: str
    symbol: str
    owner: ActivityOwner
    state: BrokerOrderState
    cumulative_filled_quantity: str
    blocks_entries: bool


@dataclass(frozen=True)
class IngestedOrder:
    broker_order_id: str
    intent_id: str
    order_revision_recorded: bool
    new_fill_ids: tuple[str, ...]
    duplicate_fill_ids: tuple[str, ...]


@dataclass(frozen=True)
class UnknownResolution:
    intent_id: str
    state: UnknownResolutionState
    evidence_at: datetime
    broker_order_id: str | None = None
    reason: str = ""
    new_fill_ids: tuple[str, ...] = ()
    duplicate_fill_ids: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.state in {
            UnknownResolutionState.MATCHED_ORDER,
            UnknownResolutionState.CONFIRMED_ABSENT,
        }

    @property
    def retry_same_intent_allowed(self) -> bool:
        # A resolved absence permits a future newly risk-checked intent, never
        # a blind replay of the ambiguous request.
        return False


@dataclass(frozen=True)
class ReconciliationReport:
    phase: ReconciliationPhase
    account_masked: str
    snapshot_received_at: datetime
    entries_allowed: bool
    blockers: tuple[str, ...]
    external_activity: tuple[ExternalActivity, ...]
    unknown_intent_ids: tuple[str, ...]
    missing_local_order_ids: tuple[str, ...]
    ingested_orders: tuple[IngestedOrder, ...] = ()
    unknown_resolutions: tuple[UnknownResolution, ...] = ()

    @property
    def pause_new_entries(self) -> bool:
        return not self.entries_allowed


_BENIGN_EXTERNAL_TERMINAL_STATES = frozenset(
    {
        BrokerOrderState.CANCELLED,
        BrokerOrderState.REJECTED,
        BrokerOrderState.FAILED,
        BrokerOrderState.VOIDED,
        BrokerOrderState.LOCATE_FAILED,
    }
)


# These conditions make the snapshot envelope unsafe as a source of any
# durable broker transition.  In particular, a stale/future/account-mismatched
# envelope must not be allowed to ingest an order or resolve UNKNOWN merely
# because its individual rows happen to look well formed.
NON_INGESTIBLE_SNAPSHOT_BLOCKERS = frozenset(
    {
        "ACCOUNT_MISMATCH",
        "CAPABILITY_ACCOUNT_MISMATCH",
        "ACCOUNT_NOT_ACTIVE",
        "AUTH_NOT_CURRENT",
        "SNAPSHOT_FROM_FUTURE",
        "STALE_SNAPSHOT",
        "OUT_OF_ORDER_SNAPSHOT",
        "DUPLICATE_POSITION_SYMBOL",
        "DUPLICATE_BROKER_ORDER_ID",
        "DUPLICATE_FILL_ID_IN_SNAPSHOT",
        "DUPLICATE_CLIENT_REFERENCE",
        "UNSUPPORTED_POSITION_ASSET_CLASS",
        "FRACTIONAL_POSITION_PRESENT",
        "FRACTIONAL_ORDER_OR_FILL_PRESENT",
    }
)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_db_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _utc(parsed, "database timestamp")


def _revision(updated_at: datetime) -> int:
    """Derive a stable monotonic microsecond revision from broker time."""

    updated = _utc(updated_at, "broker_updated_at")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = updated - epoch
    revision = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if revision < 0:
        raise ReconciliationConflict("broker_updated_at predates the revision epoch")
    return revision


def _order_facts(order: OrderSnapshot) -> Mapping[str, object]:
    return {
        "broker_order_id": order.broker_order_id,
        "account_masked": order.account_masked,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "state": order.state.value,
        "requested_quantity": format(order.requested_quantity, "f"),
        "cumulative_filled_quantity": format(
            order.cumulative_filled_quantity, "f"
        ),
        "market_hours": order.market_hours.value,
        "time_in_force": order.time_in_force.value,
        "limit_price": (
            format(order.limit_price, "f") if order.limit_price is not None else None
        ),
        "stop_price": (
            format(order.stop_price, "f") if order.stop_price is not None else None
        ),
        "client_ref_id": order.client_ref_id,
        "broker_updated_at": order.broker_updated_at,
        "fills": tuple(
            {
                "fill_id": fill.fill_id,
                "quantity": format(fill.quantity, "f"),
                "price": format(fill.price, "f"),
                "executed_at": fill.executed_at,
                "fee": format(fill.fee, "f"),
            }
            for fill in order.fills
        ),
    }


def ingest_local_order(
    store: LiveStateStore,
    *,
    order: OrderSnapshot,
    intent_id: str,
    account_key: str,
) -> IngestedOrder:
    """Idempotently import one broker order and all of its confirmed fills.

    The durable intent must already exist.  Broker revisions may arrive out of
    order; stale order envelopes are ignored while their immutable fill IDs
    are still de-duplicated and checked for conflicts.
    """

    intent = store.row("order_intents", "intent_id", intent_id)
    if intent is None:
        raise ReconciliationConflict("broker order has no durable local intent")
    if intent["account_key"] != account_key:
        raise ReconciliationConflict("intent account differs from reconciler account")
    if order.client_ref_id != intent["client_ref"]:
        raise ReconciliationConflict("broker client reference differs from local intent")

    requested = whole_shares(order.requested_quantity, field="requested_quantity")
    cumulative = whole_shares(
        order.cumulative_filled_quantity,
        field="cumulative_filled_quantity",
        allow_zero=True,
    )
    revision = _revision(order.broker_updated_at)
    raw_hash = object_hash(_order_facts(order))

    by_intent = store.rows(
        "SELECT * FROM broker_orders WHERE intent_id = ?", (intent_id,)
    )
    if by_intent and by_intent[0]["broker_order_id"] != order.broker_order_id:
        raise ReconciliationConflict("one intent resolved to multiple broker orders")

    existing = store.row("broker_orders", "broker_order_id", order.broker_order_id)
    should_record = True
    if existing is not None:
        if existing["intent_id"] != intent_id or existing["account_key"] != account_key:
            raise ReconciliationConflict("broker order durable identity changed")
        if revision < int(existing["revision"]):
            # A stale order envelope is not accepted as a source of *any*
            # new broker transition.  In particular, do not cherry-pick fill
            # rows from an older revision merely because fill IDs are
            # immutable.  The fill will be imported with a current order
            # revision on a later authoritative snapshot.
            return IngestedOrder(
                broker_order_id=order.broker_order_id,
                intent_id=intent_id,
                order_revision_recorded=False,
                new_fill_ids=(),
                duplicate_fill_ids=(),
            )
        elif revision == int(existing["revision"]):
            expected = (
                existing["state"],
                int(existing["quantity"]),
                int(existing["cumulative_filled_quantity"]),
                existing["raw_hash"],
            )
            actual = (order.state.value, requested, cumulative, raw_hash)
            if expected != actual:
                raise ReconciliationConflict(
                    "same broker revision contains different order facts"
                )
            should_record = False

    recorded = False
    if should_record:
        recorded = store.record_broker_order(
            BrokerOrder(
                broker_order_id=order.broker_order_id,
                intent_id=intent_id,
                account_key=account_key,
                state=order.state,
                quantity=requested,
                cumulative_filled_quantity=cumulative,
                revision=revision,
                broker_updated_at=order.broker_updated_at,
                received_at=order.received_at,
                raw_hash=raw_hash,
            )
        )

    new_fills: list[str] = []
    duplicate_fills: list[str] = []
    for broker_fill in order.fills:
        existing_fill = store.row("fills", "fill_id", broker_fill.fill_id)
        # FillSnapshot intentionally carries the immutable execution time but
        # not a broker-side receipt time.  Preserve our first-seen receipt on
        # replay so a later list envelope remains idempotent.
        received_at = (
            _parse_db_time(existing_fill["received_at"])
            if existing_fill is not None
            else max(order.received_at, broker_fill.executed_at)
        )
        inserted = store.record_fill(
            Fill(
                fill_id=broker_fill.fill_id,
                broker_order_id=order.broker_order_id,
                account_key=account_key,
                quantity=whole_shares(broker_fill.quantity, field="fill quantity"),
                price=broker_fill.price,
                executed_at=broker_fill.executed_at,
                received_at=received_at,
            )
        )
        (new_fills if inserted else duplicate_fills).append(broker_fill.fill_id)

    return IngestedOrder(
        broker_order_id=order.broker_order_id,
        intent_id=intent_id,
        order_revision_recorded=recorded,
        new_fill_ids=tuple(new_fills),
        duplicate_fill_ids=tuple(duplicate_fills),
    )


def resolve_unknown_intent(
    store: LiveStateStore,
    *,
    intent_id: str,
    snapshot: AccountSnapshot,
    capabilities: BrokerCapabilities,
    confirmed_absent_client_refs: Iterable[str] = (),
) -> UnknownResolution:
    """Resolve an UNKNOWN intent without ever replaying its submission.

    ``confirmed_absent_client_refs`` must come from a supported, authoritative
    broker reference-id lookup.  Mere absence from a list response is not
    accepted as proof that an ambiguous submission did not reach the broker.
    """

    intent = store.row("order_intents", "intent_id", intent_id)
    if intent is None:
        raise ReconciliationConflict(f"unknown durable intent {intent_id!r}")
    if IntentState(intent["state"]) is not IntentState.UNKNOWN:
        raise ReconciliationConflict("intent is not in UNKNOWN state")
    evidence_at = snapshot.received_at
    evidence_floor = _parse_db_time(intent["updated_at"])
    if evidence_at <= evidence_floor:
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.WAITING_FOR_NEWER_EVIDENCE,
            evidence_at=evidence_at,
            reason="snapshot is not strictly newer than the UNKNOWN transition",
        )

    client_ref = intent["client_ref"]
    matches = tuple(
        order for order in snapshot.equity_orders if order.client_ref_id == client_ref
    )
    if len(matches) > 1:
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.AMBIGUOUS,
            evidence_at=evidence_at,
            reason="multiple broker orders share the local client reference",
        )
    if matches:
        order = matches[0]
        if order.received_at <= evidence_floor:
            return UnknownResolution(
                intent_id=intent_id,
                state=UnknownResolutionState.WAITING_FOR_NEWER_EVIDENCE,
                evidence_at=evidence_at,
                reason="matching order evidence is not strictly newer",
            )
        ingested = ingest_local_order(
            store,
            order=order,
            intent_id=intent_id,
            account_key=intent["account_key"],
        )
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.MATCHED_ORDER,
            evidence_at=evidence_at,
            broker_order_id=ingested.broker_order_id,
            reason="strictly newer broker evidence matched the durable client reference",
            new_fill_ids=ingested.new_fill_ids,
            duplicate_fill_ids=ingested.duplicate_fill_ids,
        )

    negative_refs = frozenset(str(value) for value in confirmed_absent_client_refs)
    if client_ref not in negative_refs:
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.UNRESOLVED,
            evidence_at=evidence_at,
            reason="absence from an order list is not proof of non-submission",
        )
    if not capabilities.supports_ref_id_lookup:
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.UNRESOLVED,
            evidence_at=evidence_at,
            reason="connector cannot prove a negative client-reference lookup",
        )
    if not snapshot.standard_equity_orders_complete:
        return UnknownResolution(
            intent_id=intent_id,
            state=UnknownResolutionState.UNRESOLVED,
            evidence_at=evidence_at,
            reason="equity-order snapshot is incomplete",
        )
    store.transition_intent(
        intent_id,
        IntentState.RECONCILED,
        occurred_at=evidence_at,
        detail={
            "resolution": "BROKER_CONFIRMED_CLIENT_REF_ABSENT",
            "client_ref": client_ref,
            "same_intent_retry_allowed": False,
        },
    )
    return UnknownResolution(
        intent_id=intent_id,
        state=UnknownResolutionState.CONFIRMED_ABSENT,
        evidence_at=evidence_at,
        reason="supported reference lookup confirmed no broker order",
    )


def validate_authoritative_snapshot(
    snapshot: AccountSnapshot,
    capabilities: BrokerCapabilities,
    *,
    account_masked: str,
    phase: ReconciliationPhase = ReconciliationPhase.CONTINUOUS,
    now: datetime,
    max_age: timedelta = timedelta(seconds=15),
    evidence_floor_at: datetime | None = None,
    known_broker_order_ids: Iterable[str] = (),
    known_client_refs: Iterable[str] = (),
    known_nonterminal_order_ids: Iterable[str] = (),
    managed_symbols: Iterable[str] = (),
    expected_position_quantities: Mapping[str, Decimal] | None = None,
    unknown_intent_ids: Iterable[str] = (),
) -> ReconciliationReport:
    """Fail closed unless the normalized snapshot proves the whole account."""

    phase = ReconciliationPhase(phase)
    current = _utc(now, "now")
    floor = _utc(evidence_floor_at, "evidence_floor_at") if evidence_floor_at else None
    blockers: list[str] = []

    if snapshot.account_masked != account_masked:
        blockers.append("ACCOUNT_MISMATCH")
    if capabilities.account_masked != account_masked:
        blockers.append("CAPABILITY_ACCOUNT_MISMATCH")
    if snapshot.account_state.strip().lower() not in {"active", "open"}:
        blockers.append("ACCOUNT_NOT_ACTIVE")
    if not snapshot.auth_point_in_time:
        blockers.append("AUTH_NOT_CURRENT")
    if snapshot.received_at > current + timedelta(seconds=2):
        blockers.append("SNAPSHOT_FROM_FUTURE")
    elif current - snapshot.received_at > max_age:
        blockers.append("STALE_SNAPSHOT")
    if floor is not None and snapshot.received_at <= floor:
        blockers.append("OUT_OF_ORDER_SNAPSHOT")

    if not capabilities.can_prove_whole_broker_reconciliation:
        blockers.append("CONNECTOR_CANNOT_RECONCILE_WHOLE_BROKER")
    completeness = {
        "STANDARD_POSITIONS_INCOMPLETE": snapshot.standard_equity_positions_complete,
        "STANDARD_ORDERS_INCOMPLETE": snapshot.standard_equity_orders_complete,
        "OPTION_POSITIONS_INCOMPLETE": snapshot.option_positions_complete,
        "OPTION_ORDERS_INCOMPLETE": snapshot.option_orders_complete,
        "ADVANCED_RECONCILIATION_INCOMPLETE": snapshot.advanced_orders_complete,
    }
    blockers.extend(code for code, complete in completeness.items() if not complete)
    if snapshot.option_position_count:
        blockers.append("OPTION_EXPOSURE_PRESENT")
    if snapshot.option_order_count:
        blockers.append("OPTION_ORDERS_PRESENT")
    if snapshot.advanced_order_count:
        blockers.append("ADVANCED_ORDER_ACTIVITY_PRESENT")
    if snapshot.funds.cash < 0 or snapshot.funds.unleveraged_buying_power < 0:
        blockers.append("NEGATIVE_UNLEVERAGED_FUNDS")

    # Risk evidence gates only new risk.  Protection and exit modules consume
    # position/order evidence independently and therefore remain available
    # when P&L provenance is missing.
    if (
        not snapshot.daily_realized_pnl_complete
        or snapshot.daily_realized_pnl is None
    ):
        blockers.append("DAILY_REALIZED_PNL_INCOMPLETE")
    if not snapshot.risk_evidence_authoritative:
        blockers.append("DAILY_REALIZED_PNL_NON_AUTHORITATIVE")
    if (
        snapshot.risk_evidence_source is None
        or snapshot.risk_evidence_as_of is None
    ):
        blockers.append("RISK_EVIDENCE_PROVENANCE_MISSING")
    elif current - snapshot.risk_evidence_as_of > max_age:
        blockers.append("RISK_EVIDENCE_STALE")
    if (
        not snapshot.weekly_realized_pnl_complete
        or snapshot.weekly_realized_pnl is None
    ):
        blockers.append("WEEKLY_REALIZED_PNL_INCOMPLETE")
    if not snapshot.peak_equity_complete or snapshot.peak_equity is None:
        blockers.append("PEAK_EQUITY_INCOMPLETE")
    elif snapshot.peak_equity <= 0:
        blockers.append("PEAK_EQUITY_NONPOSITIVE")
    elif snapshot.peak_equity < snapshot.funds.total_value:
        blockers.append("PEAK_EQUITY_BELOW_CURRENT_EQUITY")

    position_symbols = [position.symbol for position in snapshot.equity_positions]
    if len(position_symbols) != len(set(position_symbols)):
        blockers.append("DUPLICATE_POSITION_SYMBOL")
    if any(position.asset_class.lower() != "equity" for position in snapshot.equity_positions):
        blockers.append("UNSUPPORTED_POSITION_ASSET_CLASS")
    if any(
        any(
            value != value.to_integral_value()
            for value in (
                position.quantity,
                position.sellable_quantity,
                position.held_for_sells,
            )
        )
        for position in snapshot.equity_positions
    ):
        blockers.append("FRACTIONAL_POSITION_PRESENT")

    order_ids = [order.broker_order_id for order in snapshot.equity_orders]
    if len(order_ids) != len(set(order_ids)):
        blockers.append("DUPLICATE_BROKER_ORDER_ID")
    fill_ids = [fill.fill_id for order in snapshot.equity_orders for fill in order.fills]
    if len(fill_ids) != len(set(fill_ids)):
        blockers.append("DUPLICATE_FILL_ID_IN_SNAPSHOT")
    client_refs = [
        order.client_ref_id
        for order in snapshot.equity_orders
        if order.client_ref_id is not None
    ]
    if len(client_refs) != len(set(client_refs)):
        blockers.append("DUPLICATE_CLIENT_REFERENCE")
    if any(
        order.requested_quantity != order.requested_quantity.to_integral_value()
        or order.cumulative_filled_quantity
        != order.cumulative_filled_quantity.to_integral_value()
        or any(fill.quantity != fill.quantity.to_integral_value() for fill in order.fills)
        for order in snapshot.equity_orders
    ):
        blockers.append("FRACTIONAL_ORDER_OR_FILL_PRESENT")

    known_ids = frozenset(str(value) for value in known_broker_order_ids)
    known_refs = frozenset(str(value) for value in known_client_refs)
    nonterminal_ids = frozenset(str(value) for value in known_nonterminal_order_ids)
    actual_ids = frozenset(order_ids)
    missing = tuple(sorted(nonterminal_ids - actual_ids))
    if missing and snapshot.standard_equity_orders_complete:
        blockers.append("LOCAL_NONTERMINAL_ORDER_MISSING")

    external: list[ExternalActivity] = []
    for order in snapshot.equity_orders:
        if order.broker_order_id in known_ids or order.client_ref_id in known_refs:
            continue
        owner = (
            ActivityOwner.MANUAL
            if order.client_ref_id is None
            else ActivityOwner.OTHER_AGENT
        )
        material = (
            order.state not in _BENIGN_EXTERNAL_TERMINAL_STATES
            or order.cumulative_filled_quantity > 0
        )
        external.append(
            ExternalActivity(
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                owner=owner,
                state=order.state,
                cumulative_filled_quantity=format(
                    order.cumulative_filled_quantity, "f"
                ),
                blocks_entries=material,
            )
        )
    if any(item.blocks_entries for item in external):
        blockers.append("EXTERNAL_BROKER_ACTIVITY")

    if expected_position_quantities is not None:
        actual_positions = {
            position.symbol: position.quantity
            for position in snapshot.equity_positions
            if position.quantity > 0
        }
        expected_positions = {
            str(key).upper(): Decimal(value)
            for key, value in expected_position_quantities.items()
            if Decimal(value) > 0
        }
        if actual_positions != expected_positions:
            blockers.append("POSITION_OWNERSHIP_MISMATCH")
    else:
        owned_symbols = frozenset(str(value).upper() for value in managed_symbols)
        if any(
            position.quantity > 0 and position.symbol not in owned_symbols
            for position in snapshot.equity_positions
        ):
            blockers.append("UNOWNED_POSITION_PRESENT")

    unresolved = tuple(sorted(str(value) for value in unknown_intent_ids))
    if unresolved:
        blockers.append("UNKNOWN_LOCAL_INTENT")

    # Preserve deterministic ordering while removing repeated codes.
    unique_blockers = tuple(dict.fromkeys(blockers))
    return ReconciliationReport(
        phase=phase,
        account_masked=account_masked,
        snapshot_received_at=snapshot.received_at,
        entries_allowed=not unique_blockers,
        blockers=unique_blockers,
        external_activity=tuple(external),
        unknown_intent_ids=unresolved,
        missing_local_order_ids=missing,
    )


class AuthoritativeReconciler:
    """State-aware startup and continuous reconciliation coordinator."""

    def __init__(
        self,
        *,
        account_masked: str,
        account_key: str,
        max_snapshot_age: timedelta = timedelta(seconds=15),
    ) -> None:
        if max_snapshot_age <= timedelta(0):
            raise ValueError("max_snapshot_age must be positive")
        self.account_masked = str(account_masked)
        self.account_key = str(account_key).strip()
        if not self.account_key:
            raise ValueError("account_key is required")
        self.max_snapshot_age = max_snapshot_age
        self._last_received_at: datetime | None = None

    def reconcile_snapshot(
        self,
        store: LiveStateStore,
        *,
        snapshot: AccountSnapshot,
        capabilities: BrokerCapabilities,
        now: datetime,
        phase: ReconciliationPhase = ReconciliationPhase.CONTINUOUS,
        confirmed_absent_client_refs: Iterable[str] = (),
    ) -> ReconciliationReport:
        intents = store.rows(
            "SELECT intent_id, client_ref, state FROM order_intents "
            "WHERE account_key = ?",
            (self.account_key,),
        )
        local_orders = store.rows(
            "SELECT broker_order_id, state FROM broker_orders WHERE account_key = ?",
            (self.account_key,),
        )
        managed_symbols = {
            row["symbol"]
            for row in store.rows(
                "SELECT DISTINCT symbol FROM plans WHERE account_key = ?",
                (self.account_key,),
            )
        }
        expected_positions: dict[str, Decimal] = {}
        for fill_row in store.rows(
            "SELECT f.quantity, i.order_tuple_json "
            "FROM fills f "
            "JOIN broker_orders o ON o.broker_order_id = f.broker_order_id "
            "JOIN order_intents i ON i.intent_id = o.intent_id "
            "WHERE f.account_key = ?",
            (self.account_key,),
        ):
            order_tuple = json.loads(fill_row["order_tuple_json"])
            symbol = str(order_tuple.get("symbol", "")).upper()
            side = str(order_tuple.get("side", "")).lower()
            if not symbol or side not in {"buy", "sell"}:
                raise ReconciliationConflict(
                    "durable fill has no parseable side/symbol ownership facts"
                )
            signed = Decimal(int(fill_row["quantity"]))
            if side == "sell":
                signed = -signed
            expected_positions[symbol] = expected_positions.get(symbol, Decimal("0")) + signed
            if expected_positions[symbol] < 0:
                raise ReconciliationConflict(
                    "durable local fills imply an impossible short position"
                )
        refs_to_intents = {row["client_ref"]: row["intent_id"] for row in intents}
        order_ids = {row["broker_order_id"] for row in local_orders}
        nonterminal_ids = {
            row["broker_order_id"]
            for row in local_orders
            if not BrokerOrderState(row["state"]).terminal
        }
        unresolved_ids = {
            row["intent_id"]
            for row in intents
            if IntentState(row["state"])
            in {IntentState.SUBMITTING, IntentState.UNKNOWN}
        }
        unknown_ids = {
            row["intent_id"]
            for row in intents
            if IntentState(row["state"]) is IntentState.UNKNOWN
        }

        report = validate_authoritative_snapshot(
            snapshot,
            capabilities,
            account_masked=self.account_masked,
            phase=phase,
            now=now,
            max_age=self.max_snapshot_age,
            evidence_floor_at=self._last_received_at,
            known_broker_order_ids=order_ids,
            known_client_refs=refs_to_intents,
            known_nonterminal_order_ids=nonterminal_ids,
            managed_symbols=managed_symbols,
            expected_position_quantities=expected_positions,
            unknown_intent_ids=unresolved_ids,
        )

        # Validate the authoritative envelope before *any* broker-derived
        # mutation.  Returning the report still lets the service persist a
        # quarantined, explicitly incomplete diagnostic envelope.
        if NON_INGESTIBLE_SNAPSHOT_BLOCKERS.intersection(report.blockers):
            return report

        ingested: list[IngestedOrder] = []
        resolutions: list[UnknownResolution] = []
        seen_intents: set[str] = set()
        for broker_order in snapshot.equity_orders:
            intent_id = refs_to_intents.get(broker_order.client_ref_id or "")
            if intent_id is None:
                continue
            if intent_id in seen_intents:
                raise ReconciliationConflict(
                    "multiple snapshot orders resolve to one durable intent"
                )
            seen_intents.add(intent_id)
            if intent_id in unknown_ids:
                resolution = resolve_unknown_intent(
                    store,
                    intent_id=intent_id,
                    snapshot=snapshot,
                    capabilities=capabilities,
                    confirmed_absent_client_refs=confirmed_absent_client_refs,
                )
                resolutions.append(resolution)
                if resolution.resolved:
                    unknown_ids.discard(intent_id)
                    unresolved_ids.discard(intent_id)
                continue
            ingested_order = ingest_local_order(
                store,
                order=broker_order,
                intent_id=intent_id,
                account_key=self.account_key,
            )
            ingested.append(ingested_order)
            unresolved_ids.discard(intent_id)

        # UNKNOWN intents without a matching order may be cleared only when an
        # explicit negative ref lookup was supplied.
        for intent_id in sorted(unknown_ids):
            resolution = resolve_unknown_intent(
                store,
                intent_id=intent_id,
                snapshot=snapshot,
                capabilities=capabilities,
                confirmed_absent_client_refs=confirmed_absent_client_refs,
            )
            resolutions.append(resolution)
            if resolution.resolved:
                unknown_ids.discard(intent_id)
                unresolved_ids.discard(intent_id)

        blockers = [code for code in report.blockers if code != "UNKNOWN_LOCAL_INTENT"]
        if unresolved_ids:
            blockers.append("UNKNOWN_LOCAL_INTENT")
        blockers = list(dict.fromkeys(blockers))
        result = ReconciliationReport(
            phase=report.phase,
            account_masked=report.account_masked,
            snapshot_received_at=report.snapshot_received_at,
            entries_allowed=not blockers,
            blockers=tuple(blockers),
            external_activity=report.external_activity,
            unknown_intent_ids=tuple(sorted(unresolved_ids)),
            missing_local_order_ids=report.missing_local_order_ids,
            ingested_orders=tuple(ingested),
            unknown_resolutions=tuple(resolutions),
        )
        if "OUT_OF_ORDER_SNAPSHOT" not in result.blockers:
            if self._last_received_at is None or snapshot.received_at > self._last_received_at:
                self._last_received_at = snapshot.received_at
        return result


__all__ = [
    "ActivityOwner",
    "AuthoritativeReconciler",
    "ExternalActivity",
    "IngestedOrder",
    "NON_INGESTIBLE_SNAPSHOT_BLOCKERS",
    "ReconciliationConflict",
    "ReconciliationError",
    "ReconciliationPhase",
    "ReconciliationReport",
    "UnknownResolution",
    "UnknownResolutionState",
    "ingest_local_order",
    "resolve_unknown_intent",
    "validate_authoritative_snapshot",
]
