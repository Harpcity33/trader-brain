"""Concrete, fail-closed discovery-to-entry pipeline for the live equity core.

The installed Massive watcher is a read-only *shadow* source.  A
``PreparedStructure`` is therefore only a hint about where to spend validation
work; it can never authorize an order.  This module requires an independent,
provenance-bearing live validation for the exact geometry, every setup and
execution score component, and every non-numeric hard gate before it creates a
short-lived plan.

The final mutation boundary remains :class:`EntryExecutionCoordinator`.  This
pipeline reconstructs account-wide risk from a fresh normalized broker
snapshot plus durable local state, sizes in whole shares, calls
``evaluate_entry``, and then hands at most one quality-ranked plan to that
coordinator.  Unknown, manual, or unprotected exposure always wins over
discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, localcontext
from enum import Enum
import hashlib
import json
import math
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from titan_brain.models import SetupID
from titan_brain.scoring import (
    BASELINE_SETUP_WEIGHTS,
    EQUITY_EXECUTION_WEIGHTS,
    ScoreResult,
    score_execution,
    score_setup,
)

from .broker import AccountSnapshot, BrokerClient, BrokerSide
from .authority import MutationAuthority
from .execution import (
    EntryExecutionCoordinator,
    ExecutionOutcome,
    ExecutionStatus,
    PreparedOrderPlanSealer,
)
from .latency import LatencyRecorder, LatencySpan
from .market_data import (
    CompletedBar,
    EvidenceDecision,
    MarketDataCache,
    MarketSessionState,
    Quote,
)
from .massive_adapter import MassiveFeedHealth, PreparedStructure, TradabilityProvider
from .money import decimal_value
from .plans import ExpiringPlan
from .policy import PolicyBundle
from .risk_runtime import (
    AccountRiskSnapshot,
    RiskDecision,
    RiskExposure,
    SessionAccountRiskSnapshot,
    SessionLatch,
    dollar_headroom_capacity,
    daily_starting_equity_capacity,
    entry_lifecycle_fee_reserve,
    evaluate_entry,
)
from .state import LiveStateStore
from .session_trading_policy import SessionTradingState, evaluate_session_state


ZERO = Decimal("0")

# These facts cannot be inferred from a numerical score.  Requiring the exact
# key set makes an upstream schema omission a rejection rather than a default.
REQUIRED_HARD_GATE_FACTS = frozenset(
    {
        "independent_geometry_revalidation",
        "causal_completed_bar_structure",
        "fresh_executable_quote",
        "broker_tradable",
        "acceptable_spread",
        "adequate_displayed_depth",
        "acceptable_extension",
        "favorable_reward_risk",
        "remaining_capacity",
        "current_session_eligible",
    }
)

# These two facts require the actual sized order and a fresh account snapshot.
# A quality reader may explicitly defer them, but never assert them by default.
# _plan_candidate still enforces evaluate_entry and _market_decision before the
# durable submission boundary; no other hard gate may use this two-stage path.
POST_SIZING_HARD_GATE_FACTS = frozenset(
    {"adequate_displayed_depth", "remaining_capacity"}
)


class PipelineStatus(str, Enum):
    NO_TRADE = "NO_TRADE"
    WAITING_FOR_SESSION = "WAITING_FOR_SESSION"
    BLOCKED = "BLOCKED"
    NOT_SELECTED = "NOT_SELECTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ATTEMPT_BLOCKED = "ATTEMPT_BLOCKED"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


class PremarketAnalysisStatus(str, Enum):
    """Caller-visible state for one analysis-only schedule slot."""

    OUTSIDE_LANE = "OUTSIDE_LANE"
    NOT_DUE = "NOT_DUE"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class PremarketAnalysisSchedule:
    """Exact 30-minute premarket schedule decision.

    The caller owns durable de-duplication and passes the last successfully
    completed slot back on the next invocation.  This object grants no order
    authority and deliberately contains no broker request.
    """

    lane: str
    due: bool
    interval_minutes: int
    scheduled_for: datetime | None
    next_due_at: datetime | None
    reason: str
    execution_authority: bool = field(init=False, default=False)
    approved_to_buy: bool = field(init=False, default=False)


@dataclass(frozen=True)
class PremarketCandidateFact:
    """Compact deterministic facts for one analysis-only candidate."""

    rank: int
    symbol: str
    source_plan_id: str
    source_observed_at: datetime
    shadow_ranking_score: Decimal
    setup_score: float
    execution_score: float
    instrument_evidence_id: str | None
    instrument_id: str | None
    instrument_source: str | None
    instrument_observed_at: datetime | None
    regular_session_eligibility_at: datetime | None
    quote_bid: Decimal | None
    quote_ask: Decimal | None
    quote_bid_size: int | None
    quote_ask_size: int | None
    quote_observed_at: datetime | None
    spread_bps: Decimal | None
    latest_completed_bar_end: datetime | None
    session_volume: int
    structural_invalidation: Decimal
    targets: tuple[Decimal, ...]
    hard_gate_failures: tuple[str, ...]
    deferred_execution_gates: tuple[str, ...]
    execution_authority: bool = field(init=False, default=False)
    approved_to_buy: bool = field(init=False, default=False)


@dataclass(frozen=True)
class PremarketAnalysisResult:
    """Analysis facts returned to a scheduler; never an entry recommendation."""

    status: PremarketAnalysisStatus
    schedule: PremarketAnalysisSchedule
    analysis_id: str | None
    observed_at: datetime
    candidates: tuple[PremarketCandidateFact, ...]
    blockers: tuple[str, ...]
    message: str
    execution_authority: bool = field(init=False, default=False)
    approved_to_buy: bool = field(init=False, default=False)


@dataclass(frozen=True)
class PipelineThresholds:
    """Explicit operator-approved score floors; there are no hidden defaults."""

    minimum_setup_score: float | None
    minimum_execution_score: float | None
    a_plus_setup_score: float | None
    a_plus_execution_score: float | None
    score_policy: str = "threshold_gated"
    a_plus_enabled: bool = True

    def __post_init__(self) -> None:
        if self.score_policy not in {"threshold_gated", "ranking_only"}:
            raise ValueError("unsupported signed score policy")
        if type(self.a_plus_enabled) is not bool:
            raise ValueError("a_plus_enabled must be boolean")
        values = tuple(
            getattr(self, name)
            for name in (
                "minimum_setup_score",
                "minimum_execution_score",
                "a_plus_setup_score",
                "a_plus_execution_score",
            )
        )
        if self.score_policy == "ranking_only":
            if any(value is not None for value in values):
                raise ValueError("ranking-only scores cannot contain hidden floors")
            if self.a_plus_enabled is not False:
                raise ValueError("ranking-only score policy requires A+ disabled")
            return
        if any(value is None for value in values):
            raise ValueError("threshold-gated score policy requires every threshold")
        if self.a_plus_enabled is not True:
            raise ValueError("threshold-gated score policy requires A+ enabled")
        for name in (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"{name} must be finite and in [0, 100]")
            object.__setattr__(self, name, value)
        assert self.minimum_setup_score is not None
        assert self.minimum_execution_score is not None
        assert self.a_plus_setup_score is not None
        assert self.a_plus_execution_score is not None
        if self.a_plus_setup_score < self.minimum_setup_score:
            raise ValueError("A+ setup threshold cannot be below the live floor")
        if self.a_plus_execution_score < self.minimum_execution_score:
            raise ValueError("A+ execution threshold cannot be below the live floor")

    @classmethod
    def from_policy(cls, policy: PolicyBundle) -> "PipelineThresholds":
        """Load one exact signed score treatment without implicit thresholds."""

        discovery = policy.config.get("discovery")
        if not isinstance(discovery, Mapping):
            raise ValueError("signed discovery configuration is missing")
        names = (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        )
        missing_keys = tuple(name for name in names if name not in discovery)
        if missing_keys:
            raise ValueError(
                "signed live score fields are missing: " + ",".join(missing_keys)
            )
        score_policy = discovery.get("score_policy", "threshold_gated")
        a_plus_enabled = discovery.get("a_plus_enabled", True)
        if score_policy == "ranking_only":
            if any(discovery.get(name) is not None for name in names):
                raise ValueError(
                    "ranking-only signed score policy requires null thresholds"
                )
            if a_plus_enabled is not False:
                raise ValueError(
                    "ranking-only signed score policy requires a_plus_enabled=false"
                )
            return cls(
                None,
                None,
                None,
                None,
                score_policy="ranking_only",
                a_plus_enabled=False,
            )
        if score_policy != "threshold_gated":
            raise ValueError("signed score_policy is unsupported")
        missing = tuple(name for name in names if discovery.get(name) is None)
        if missing:
            raise ValueError(
                "signed live score thresholds are unresolved: " + ",".join(missing)
            )
        if a_plus_enabled is not True:
            raise ValueError(
                "threshold-gated signed score policy requires a_plus_enabled=true"
            )
        return cls(*(float(discovery[name]) for name in names))


@dataclass(frozen=True)
class InstrumentEvidence:
    """Current broker/instrument eligibility from an injected live reader.

    ``robinhood_tradable`` is retained as the serialized v1 field name. Core
    policy consumes the broker-neutral ``broker_tradable`` property so an IBKR
    record never masquerades as Robinhood evidence.
    """

    evidence_id: str
    symbol: str
    instrument_id: str
    observed_at: datetime
    source: str
    asset_type: str
    exchange_listed: bool
    robinhood_tradable: bool
    regular_hours_eligible: bool
    eligibility_at: datetime | None = None
    eligibility_scope: str = "current_regular_session"

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or not symbol.replace(".", "").replace("-", "").isalnum():
            raise ValueError("instrument evidence symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "instrument.observed_at")
        )
        eligibility_at = self.eligibility_at
        if eligibility_at is None:
            eligibility_at = self.observed_at
        object.__setattr__(
            self,
            "eligibility_at",
            _aware_utc(eligibility_at, "instrument.eligibility_at"),
        )
        for field in ("evidence_id", "instrument_id", "source", "asset_type"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"instrument {field} is required")
        for field in (
            "exchange_listed",
            "robinhood_tradable",
            "regular_hours_eligible",
        ):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"instrument {field} must be boolean")
        if self.eligibility_scope not in {
            "current_regular_session",
            "upcoming_regular_session_analysis",
        }:
            raise ValueError("instrument eligibility scope is invalid")

    @property
    def broker_tradable(self) -> bool:
        return self.robinhood_tradable


@dataclass(frozen=True)
class LiveValidationEvidence:
    """Independent validation of one shadow structure and its quality inputs."""

    evidence_id: str
    source_plan_id: str
    symbol: str
    observed_at: datetime
    completed_bar_end: datetime
    entry_limit: Decimal
    structural_stop: Decimal
    targets: tuple[Decimal, ...]
    execution_reserve_per_share: Decimal
    setup_components: Mapping[str, float]
    execution_components: Mapping[str, float]
    hard_gate_facts: Mapping[str, bool]
    shadow_proposal_grants_authority: bool = False
    deferred_hard_gate_facts: frozenset[str] = frozenset()


class InstrumentEvidenceProvider(Protocol):
    def get_instrument_evidence(
        self, symbol: str, *, now: datetime
    ) -> InstrumentEvidence | None: ...


class RobinhoodInstrumentRecordReader(Protocol):
    """Injected authenticated reader; this module never handles credentials."""

    def get_equity_instrument(
        self,
        symbol: str,
        *,
        as_of: datetime,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class RobinhoodInstrumentEvidenceProvider:
    """Strict normalized Robinhood instrument/tradability evidence adapter.

    Every eligibility boolean must be explicit in the injected broker record;
    the adapter never derives tradability from Massive, a symbol format, or a
    missing response.  Provider/auth failures become missing evidence and are
    therefore fail-closed at the pipeline gate.
    """

    REQUIRED_FIELDS = frozenset(
        {
            "evidence_id",
            "symbol",
            "instrument_id",
            "observed_at",
            "source",
            "asset_type",
            "exchange_listed",
            "robinhood_tradable",
            "regular_hours_eligible",
        }
    )

    def __init__(
        self,
        reader: RobinhoodInstrumentRecordReader,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("instrument timeout must be in (0, 30]")
        self.reader = reader
        self.timeout_seconds = float(timeout_seconds)

    def get_instrument_evidence(
        self, symbol: str, *, now: datetime
    ) -> InstrumentEvidence | None:
        current = _aware_utc(now, "instrument.now")
        normalized = str(symbol).strip().upper()
        try:
            raw = self.reader.get_equity_instrument(
                normalized,
                as_of=current,
                timeout_seconds=self.timeout_seconds,
            )
            if not isinstance(raw, Mapping) or not self.REQUIRED_FIELDS.issubset(raw):
                return None
            observed = raw["observed_at"]
            if isinstance(observed, str):
                observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            evidence = InstrumentEvidence(
                evidence_id=str(raw["evidence_id"]),
                symbol=str(raw["symbol"]),
                instrument_id=str(raw["instrument_id"]),
                observed_at=observed,
                source=str(raw["source"]),
                asset_type=str(raw["asset_type"]),
                exchange_listed=raw["exchange_listed"],
                robinhood_tradable=raw["robinhood_tradable"],
                regular_hours_eligible=raw["regular_hours_eligible"],
            )
            if evidence.symbol != normalized or not evidence.source.startswith("robinhood"):
                return None
            return evidence
        except Exception:
            return None


class QualityEvidenceProvider(Protocol):
    def revalidate_structure(
        self, structure: PreparedStructure, *, now: datetime
    ) -> LiveValidationEvidence | None: ...


class PreparedStructureSource(Protocol):
    """Read-only market source used by the lifecycle discovery adapter."""

    def health(self, *, now: datetime) -> MassiveFeedHealth: ...

    def prepared_structures(
        self, *, now: datetime, limit: int
    ) -> tuple[PreparedStructure, ...]: ...

    def hydrate_cache(
        self,
        cache: MarketDataCache,
        *,
        structures: Sequence[PreparedStructure],
        session_start: datetime,
        now: datetime,
        tradability: TradabilityProvider,
    ) -> tuple[str, ...]: ...

    def ensure_risk_symbols(
        self,
        cache: MarketDataCache,
        *,
        symbols: Sequence[str],
        session_start: datetime,
        now: datetime,
        tradability: TradabilityProvider,
    ) -> tuple[str, ...]: ...

    def evidence_snapshot(self, symbol: str, *, now: datetime) -> object: ...


@dataclass(frozen=True)
class CandidateResult:
    source_plan_id: str
    symbol: str
    status: PipelineStatus
    failures: tuple[str, ...]
    rank: int | None = None
    setup_score: float | None = None
    execution_score: float | None = None
    quality_tier: str | None = None
    quantity: int | None = None
    plan: ExpiringPlan | None = None
    risk_decision: RiskDecision | None = None
    market_decision: EvidenceDecision | None = None
    execution: ExecutionOutcome | None = None


@dataclass(frozen=True)
class PipelineRunResult:
    status: PipelineStatus
    candidates: tuple[CandidateResult, ...]
    attempted_plan_id: str | None
    message: str

    @property
    def selected(self) -> CandidateResult | None:
        for candidate in self.candidates:
            if candidate.execution is not None:
                return candidate
        return None


@dataclass(frozen=True)
class _ScoredCandidate:
    structure: PreparedStructure
    instrument: InstrumentEvidence | None
    validation: LiveValidationEvidence | None
    setup: ScoreResult | None
    execution: ScoreResult | None
    failures: tuple[str, ...]

    @property
    def rank_key(self) -> tuple[Decimal, Decimal, Decimal, str]:
        """Quality dominates; the shadow rank is only a deterministic tie-break."""

        if self.setup is None or self.execution is None or self.failures:
            return (Decimal("-1"), Decimal("-1"), Decimal("-1"), self.structure.symbol)
        setup = Decimal(str(self.setup.score))
        execution = Decimal(str(self.execution.score))
        return (
            min(setup, execution),
            setup + execution,
            self.structure.ranking_score,
            self.structure.symbol,
        )


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _decimal_from_cents(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("money cents cannot be boolean")
    return Decimal(int(value)) / Decimal("100")


def _event_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _risk_cap(
    policy: PolicyBundle,
    equity: Decimal,
    percent_name: str,
    absolute_name: str,
) -> Decimal:
    result = equity * decimal_value(policy.risk_raw[percent_name], percent_name)
    absolute = policy.risk_raw.get("absolute_dollar_ceilings", {}).get(absolute_name)
    if absolute is not None:
        result = min(result, decimal_value(absolute, absolute_name))
    return result


def _manual_price(
    *, symbol: str, average_price: Decimal | None, prices: Mapping[str, Decimal]
) -> Decimal | None:
    if average_price is not None and average_price > 0:
        return average_price
    result = prices.get(symbol)
    if result is not None and result > 0:
        return result
    return None


def build_account_risk_snapshot(
    *,
    policy: PolicyBundle,
    state: LiveStateStore,
    broker_snapshot: AccountSnapshot,
    now: datetime,
    prices: Mapping[str, Decimal] | None = None,
    exclude_plan_id: str | None = None,
) -> tuple[AccountRiskSnapshot | None, tuple[str, ...]]:
    """Merge exact broker risk facts with every durable possible exposure.

    ``exclude_plan_id`` is used only for an exact, demonstrably unfilled
    idempotent replay so that its pending reservation is not charged twice.
    Unknown or filled state is never excluded. Its broker order remains
    recognized as locally owned; any different plan continues to see it.
    """

    current = _aware_utc(now, "now")
    failures: list[str] = []
    if policy.session_trading_risk:
        return None, ("SESSION_RISK_SNAPSHOT_PATH_REQUIRED",)
    masked = f"••••{policy.account_last4}"
    if broker_snapshot.account_masked != masked:
        failures.append("ACCOUNT_POLICY_MISMATCH")
    if broker_snapshot.account_type != str(policy.config["account"]["allowed_type"]):
        failures.append("ACCOUNT_TYPE_POLICY_MISMATCH")
    if not broker_snapshot.auth_point_in_time:
        failures.append("BROKER_AUTH_POINT_IN_TIME_UNPROVEN")
    if not broker_snapshot.entry_risk_evidence_ready:
        failures.append("AUTHORITATIVE_ACCOUNT_RISK_EVIDENCE_INCOMPLETE")
    if policy.daily_starting_equity_risk and not broker_snapshot.daily_starting_equity_ready:
        failures.append("DAILY_STARTING_EQUITY_EVIDENCE_NOT_READY")
        return None, _unique(failures)
    if policy.daily_starting_equity_risk and any(
        position.quantity != 0 for position in broker_snapshot.equity_positions
    ):
        # The connected position valuation diagnostic provides a receipt time,
        # not a provider valuation time or a valuation from the same epoch as
        # account NLV. It also cannot prove an all-in remaining-fee upper bound.
        # A shaped calculation input must never lift this entry-only blocker.
        failures.extend(
            (
                "OPEN_POSITION_SOURCE_VALUATION_TIME_UNAVAILABLE",
                "OPEN_POSITION_COMMON_NLV_EPOCH_UNAVAILABLE",
                "OPEN_POSITION_REMAINING_FEE_BOUND_UNAVAILABLE",
                "DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED",
            )
        )
        return None, _unique(failures)
    if broker_snapshot.option_position_count:
        failures.append("OPTION_POSITION_PRESENT_OUTSIDE_EQUITY_SCOPE")
    if broker_snapshot.option_order_count:
        failures.append("OPTION_ORDER_PRESENT_OUTSIDE_EQUITY_SCOPE")
    if any(position.is_fractional for position in broker_snapshot.equity_positions):
        failures.append("FRACTIONAL_POSITION_PRESENT_OUTSIDE_LIVE_SCOPE")
    snapshot_age = (current - broker_snapshot.observed_at).total_seconds()
    maximum_age = int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
    if snapshot_age < -1 or snapshot_age > maximum_age:
        failures.append("BROKER_RISK_SNAPSHOT_STALE")
    if failures and not broker_snapshot.entry_risk_evidence_ready:
        return None, _unique(failures)

    assert broker_snapshot.daily_realized_pnl is not None
    assert broker_snapshot.weekly_realized_pnl is not None
    assert broker_snapshot.peak_equity is not None
    assert broker_snapshot.risk_evidence_as_of is not None
    risk_as_of = min(broker_snapshot.observed_at, broker_snapshot.risk_evidence_as_of)
    risk_age = (current - risk_as_of).total_seconds()
    if risk_age < -1 or risk_age > maximum_age:
        failures.append("AUTHORITATIVE_ACCOUNT_RISK_EVIDENCE_STALE")

    exposures, exposure_failures = _build_account_risk_exposures(
        policy=policy, state=state, broker_snapshot=broker_snapshot,
        prices=prices, exclude_plan_id=exclude_plan_id,
    )
    failures.extend(exposure_failures)
    if exposures is None:
        return None, _unique(failures)
    result = AccountRiskSnapshot.build(
        account_last4=policy.account_last4,
        observed_at=risk_as_of,
        usable_equity=broker_snapshot.funds.total_value,
        total_equity=broker_snapshot.funds.total_value,
        daily_starting_equity=broker_snapshot.daily_starting_equity,
        daily_external_cash_flow=broker_snapshot.daily_external_cash_flow,
        unleveraged_buying_power=broker_snapshot.funds.unleveraged_buying_power,
        cash=broker_snapshot.funds.cash,
        daily_realized_pnl=broker_snapshot.daily_realized_pnl,
        weekly_realized_pnl=broker_snapshot.weekly_realized_pnl,
        peak_equity=broker_snapshot.peak_equity,
        exposures=exposures,
        account_active=broker_snapshot.account_state.lower() == "active",
        restricted=broker_snapshot.account_state.lower() != "active",
        standard_orders_reconciled=broker_snapshot.standard_equity_orders_complete,
        option_orders_reconciled=(broker_snapshot.option_orders_complete and broker_snapshot.option_positions_complete),
        advanced_orders_reconciled=broker_snapshot.advanced_orders_complete,
        positions_reconciled=broker_snapshot.standard_equity_positions_complete,
    )
    return result, _unique(failures)


def _build_account_risk_exposures(
    *, policy: PolicyBundle, state: LiveStateStore,
    broker_snapshot: AccountSnapshot, prices: Mapping[str, Decimal] | None,
    exclude_plan_id: str | None,
) -> tuple[tuple[RiskExposure, ...] | None, tuple[str, ...]]:
    """Shared durable exposure accounting, never measurement authentication.

    Both measurement paths must account for identical reservations, external
    orders, positions and unresolved safety intents. Legacy P&L requirements
    remain in the legacy caller, not invented as inputs to session arithmetic.
    """
    failures: list[str] = []
    account_key = policy.account_key
    prices = dict(prices or {})
    broker_positions = {
        item.symbol: item
        for item in broker_snapshot.equity_positions
        if item.quantity > 0
    }
    active_reservations = state.rows(
        """SELECT r.*,p.symbol,p.limit_price,p.structural_stop,
                         p.quantity AS plan_quantity,
                         p.account_key AS plan_account_key,
                         p.strategy_id AS plan_strategy_id,
                         p.policy_hash AS plan_policy_hash,
                         p.config_hash AS plan_config_hash,
                         i.intent_id,i.client_ref,i.state AS intent_state,
                         i.plan_id AS intent_plan_id,
                         i.account_key AS intent_account_key
              FROM risk_reservations r
              JOIN plans p ON p.plan_id=r.plan_id
              JOIN order_intents i ON i.reservation_id=r.reservation_id
             WHERE r.account_key=? AND r.state<>'RELEASED'
             ORDER BY r.created_at,r.reservation_id""",
        (account_key,),
    )
    reservation_fees: dict[str, Decimal] = {}
    reservation_failures: list[str] = []
    for row in active_reservations:
        if any(
            (
                row["plan_account_key"] != account_key,
                row["intent_account_key"] != account_key,
                row["intent_plan_id"] != row["plan_id"],
                row["plan_strategy_id"] != policy.strategy_id,
                row["plan_policy_hash"] != policy.policy_hash,
                row["plan_config_hash"] != policy.config_hash,
            )
        ):
            reservation_failures.append(
                "ACTIVE_RESERVATION_POLICY_BINDING_MISMATCH"
            )
            continue
        try:
            quantity = int(row["plan_quantity"])
            if quantity != row["plan_quantity"] or quantity <= 0:
                raise ValueError("invalid durable plan quantity")
            fee = entry_lifecycle_fee_reserve(policy, quantity=quantity)
            planned = _decimal_from_cents(row["planned_risk_cents"])
            execution = _decimal_from_cents(row["execution_reserve_cents"])
            stress = _decimal_from_cents(row["stress_risk_cents"])
        except (KeyError, TypeError, ValueError):
            reservation_failures.append(
                "ACTIVE_RESERVATION_FEE_DERIVATION_INVALID"
            )
            continue
        if stress != planned + execution + fee:
            reservation_failures.append(
                "ACTIVE_RESERVATION_FEE_BINDING_MISMATCH"
            )
            continue
        reservation_fees[str(row["reservation_id"])] = fee
    if reservation_failures:
        return None, _unique(tuple(failures) + tuple(reservation_failures))

    all_owned = state.rows(
        """SELECT i.client_ref,i.plan_id,p.symbol,o.broker_order_id
              FROM order_intents i JOIN plans p ON p.plan_id=i.plan_id
              LEFT JOIN broker_orders o ON o.intent_id=i.intent_id
             WHERE i.account_key=?""",
        (account_key,),
    )
    local_refs = {str(row["client_ref"]) for row in all_owned}
    local_order_ids = {
        str(row["broker_order_id"])
        for row in all_owned
        if row["broker_order_id"] is not None
    }
    local_symbols = {
        str(row["symbol"])
        for row in active_reservations
    }
    exposures: list[RiskExposure] = []
    has_open_reservation = False
    for row in active_reservations:
        order_rows = state.rows(
            "SELECT * FROM broker_orders WHERE intent_id=?", (row["intent_id"],)
        )
        broker_order = order_rows[0] if order_rows else None
        intent_state = str(row["intent_state"])
        order_state = str(broker_order["state"]) if broker_order is not None else ""
        filled = (
            int(broker_order["cumulative_filled_quantity"])
            if broker_order is not None
            else 0
        )
        possible_unknown = intent_state in {"SUBMITTING", "UNKNOWN"} or order_state == "UNKNOWN"
        is_open = filled > 0 or str(row["symbol"]) in broker_positions
        has_open_reservation = has_open_reservation or is_open
        # Partial fills do not resolve an ambiguous remaining entry. Preserve
        # UNKNOWN precedence for every policy, independently of the daily-mode
        # prohibition on unvalued open risk below.
        category = "unknown" if possible_unknown else ("open" if is_open else "pending")
        if (
            exclude_plan_id is not None
            and row["plan_id"] == exclude_plan_id
            and category == "pending"
            and not is_open
        ):
            continue
        protected = True
        if category == "open":
            obligations = state.rows(
                """SELECT po.required_quantity,po.working_quantity,po.state
                      FROM fills f
                      LEFT JOIN protection_obligations po ON po.source_fill_id=f.fill_id
                     WHERE f.broker_order_id=?""",
                (broker_order["broker_order_id"],) if broker_order is not None else ("",),
            )
            protected = bool(obligations) and all(
                item["state"] == "WORKING"
                and int(item["working_quantity"]) == int(item["required_quantity"])
                for item in obligations
            )
        exposures.append(
            RiskExposure.build(
                reference=f"reservation:{row['reservation_id']}",
                category=category,
                planned_risk=_decimal_from_cents(row["planned_risk_cents"]),
                stress_risk=_decimal_from_cents(row["stress_risk_cents"]),
                execution_reserve=_decimal_from_cents(row["execution_reserve_cents"]),
                fee_reserve=reservation_fees[str(row["reservation_id"])],
                notional=_decimal_from_cents(row["notional_cents"]),
                protected=protected,
            )
        )

    # A broker position that has no live local reservation is manual/external.
    # Without a stop its entire known notional is the conservative bounded
    # amount; if price evidence is unavailable the category becomes unknown.
    for position in broker_snapshot.equity_positions:
        if position.quantity <= 0 or position.symbol in local_symbols:
            continue
        price = _manual_price(
            symbol=position.symbol,
            average_price=position.average_price,
            prices=prices,
        )
        notional = position.quantity * price if price is not None else ZERO
        category = "manual" if price is not None else "unknown"
        exposures.append(
            RiskExposure.build(
                reference=f"broker-position:{position.symbol}",
                category=category,
                planned_risk=notional,
                stress_risk=notional,
                execution_reserve=ZERO,
                fee_reserve=ZERO,
                notional=notional,
                protected=False,
            )
        )

    # Active broker orders not tied to any durable local identity are manual.
    # Both buys and sells pause new entries; only a conclusively terminal order
    # disappears from this exposure set.
    for order in broker_snapshot.equity_orders:
        locally_owned = (
            order.broker_order_id in local_order_ids
            or (order.client_ref_id is not None and order.client_ref_id in local_refs)
        )
        if locally_owned or order.state.terminal:
            continue
        remaining = order.requested_quantity - order.cumulative_filled_quantity
        price = order.limit_price or order.stop_price or prices.get(order.symbol)
        notional = remaining * price if price is not None else ZERO
        category = "unknown" if order.state.value == "UNKNOWN" or price is None else "manual"
        exposures.append(
            RiskExposure.build(
                reference=f"external-order:{order.broker_order_id}",
                category=category,
                planned_risk=notional if order.side is BrokerSide.BUY else ZERO,
                stress_risk=notional if order.side is BrokerSide.BUY else ZERO,
                execution_reserve=ZERO,
                fee_reserve=ZERO,
                notional=notional,
                protected=False,
            )
        )

    # An ambiguous protection/exit/cancel is still possible broker exposure,
    # even though safety intents correctly carry no incremental risk reserve.
    safety_unknown = state.rows(
        """SELECT intent_id FROM order_intents
             WHERE account_key=? AND kind<>'ENTRY' AND state IN ('SUBMITTING','UNKNOWN')""",
        (account_key,),
    )
    exposures.extend(
        RiskExposure.build(
            reference=f"safety-intent:{row['intent_id']}",
            category="unknown",
            planned_risk=ZERO,
            stress_risk=ZERO,
            execution_reserve=ZERO,
            fee_reserve=ZERO,
            notional=ZERO,
            protected=False,
        )
        for row in safety_unknown
    )

    # A newer flat broker snapshot cannot release a filled durable reservation
    # by itself. Final dispatch also consumes this builder without calling
    # evaluate_entry, so retain the same remaining-risk gate here.
    if policy.daily_starting_equity_risk and (
        has_open_reservation
        or any(exposure.category in {"open", "manual"} for exposure in exposures)
    ):
        failures.append("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED")
        return None, _unique(failures)

    return tuple(exposures), _unique(failures)


def build_session_account_risk_snapshot(
    *, policy: PolicyBundle, state: LiveStateStore,
    broker_snapshot: AccountSnapshot, session_state: SessionTradingState,
    account_binding_sha256: str, now: datetime,
    prices: Mapping[str, Decimal] | None = None, exclude_plan_id: str | None = None,
) -> tuple[SessionAccountRiskSnapshot | None, tuple[str, ...]]:
    """Build distinct session constraints without inventing legacy risk facts.

    This consumes already reconciled exposure and session inputs, not an
    authentication shortcut. The production source/seal/activation path must
    independently verify them; this builder cannot issue order authority.
    Until current-bid/stop remaining-risk evidence is connected, open inventory
    fails here rather than borrowing original-entry risk from the legacy path.
    """
    current = _aware_utc(now, "now")
    failures: list[str] = []
    if not policy.session_trading_risk:
        return None, ("SESSION_RISK_POLICY_REQUIRED",)
    if type(session_state) is not SessionTradingState:
        return None, ("SESSION_RISK_STATE_REQUIRED",)
    if (session_state.baseline.policy_sha256 != policy.risk_hash
            or session_state.baseline.account_binding_sha256 != account_binding_sha256
            or account_binding_sha256 != policy.config["execution"].get("production_account_binding_fingerprint")):
        return None, ("SESSION_RISK_BINDING_MISMATCH",)
    if broker_snapshot.account_masked != f"••••{policy.account_last4}":
        failures.append("ACCOUNT_POLICY_MISMATCH")
    if broker_snapshot.account_type != str(policy.config["account"]["allowed_type"]):
        failures.append("ACCOUNT_TYPE_POLICY_MISMATCH")
    if not broker_snapshot.auth_point_in_time:
        failures.append("BROKER_AUTH_POINT_IN_TIME_UNPROVEN")
    if broker_snapshot.funds.currency != "USD":
        failures.append("SESSION_USD_FUNDS_REQUIRED")
    if broker_snapshot.option_position_count or broker_snapshot.option_order_count:
        failures.append("OPTION_EXPOSURE_PRESENT_OUTSIDE_EQUITY_SCOPE")
    if broker_snapshot.advanced_order_count:
        failures.append("SESSION_ADVANCED_ORDER_RECONCILIATION_REQUIRED")
    if any(position.quantity != 0 for position in broker_snapshot.equity_positions):
        failures.append("SESSION_OPEN_RISK_REVALUATION_REQUIRED")
    if not all((broker_snapshot.standard_equity_orders_complete,
                broker_snapshot.standard_equity_positions_complete,
                broker_snapshot.option_orders_complete,
                broker_snapshot.option_positions_complete,
                broker_snapshot.advanced_orders_complete)):
        failures.append("WHOLE_BROKER_RECONCILIATION_INCOMPLETE")
    maximum_age = int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
    if any(not 0 <= (current - stamp).total_seconds() <= maximum_age
           for stamp in (broker_snapshot.observed_at, broker_snapshot.received_at)):
        failures.append("BROKER_RISK_SNAPSHOT_STALE")
    decision = evaluate_session_state(session_state, now=current)
    failures.extend(decision.entry_blockers)
    if failures:
        return None, _unique(failures)
    # Monetary conversion and reservation assembly must be exact before the
    # evaluator sees them; its own context cannot undo earlier rounding.
    with localcontext() as context:
        context.prec = 80
        exposures, exposure_failures = _build_account_risk_exposures(
            policy=policy, state=state, broker_snapshot=broker_snapshot,
            prices=prices, exclude_plan_id=exclude_plan_id,
        )
    failures.extend(exposure_failures)
    if exposures is None:
        return None, _unique(failures)
    if any(row.category in {"open", "manual"} for row in exposures):
        return None, _unique((*failures, "SESSION_OPEN_RISK_REVALUATION_REQUIRED"))
    measurement = session_state.last_measurement
    assert measurement is not None  # evaluate_session_state rejected missing
    try:
        result = SessionAccountRiskSnapshot.build(
            account_last4=policy.account_last4, account_binding_sha256=account_binding_sha256,
            observed_at=min(broker_snapshot.observed_at, measurement.as_of),
            usable_equity=broker_snapshot.funds.total_value,
            unleveraged_buying_power=broker_snapshot.funds.unleveraged_buying_power,
            cash=broker_snapshot.funds.cash, exposures=exposures,
            account_active=broker_snapshot.account_state.lower() == "active",
            restricted=broker_snapshot.account_state.lower() != "active",
            standard_orders_reconciled=True, option_orders_reconciled=True,
            advanced_orders_reconciled=True, positions_reconciled=True,
            session_state=session_state,
        )
    except (TypeError, ValueError):
        return None, _unique((*failures, "SESSION_RISK_INPUT_INVALID"))
    return result, _unique(failures)


class FullLiveEntryPipeline:
    """Validate, rank, size, and submit at most one long-equity entry."""

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        market_data: MarketDataCache,
        state: LiveStateStore,
        broker: BrokerClient,
        authority: MutationAuthority,
        instrument_evidence: InstrumentEvidenceProvider,
        quality_evidence: QualityEvidenceProvider,
        thresholds: PipelineThresholds,
        plan_sealer: PreparedOrderPlanSealer | None = None,
        clock: Callable[[], datetime] | None = None,
        latency: LatencyRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.market_data = market_data
        self.state = state
        self.broker = broker
        self.authority = authority
        self.instrument_evidence = instrument_evidence
        self.quality_evidence = quality_evidence
        self.thresholds = thresholds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.latency = latency
        self.execution = EntryExecutionCoordinator(
            policy=policy,
            market_data=market_data,
            state=state,
            broker=broker,
            authority=authority,
            plan_sealer=plan_sealer,
            clock=self._clock,
            latency=latency,
        )

    def run_once(
        self,
        *,
        structures: Sequence[PreparedStructure],
        broker_snapshot: AccountSnapshot,
        latch: SessionLatch,
        now: datetime | None = None,
    ) -> PipelineRunResult:
        current = _aware_utc(now or self._clock(), "now")
        if not structures:
            return PipelineRunResult(
                status=PipelineStatus.NO_TRADE,
                candidates=(),
                attempted_plan_id=None,
                message="no candidate structures were supplied; none were fabricated",
            )
        unique_structures: list[PreparedStructure] = []
        seen: set[tuple[str, str]] = set()
        for item in structures:
            if not isinstance(item, PreparedStructure):
                raise TypeError("structures must contain PreparedStructure records")
            key = (item.source_plan_id, item.payload_hash)
            if key not in seen:
                seen.add(key)
                unique_structures.append(item)

        run_id = _event_id(
            "pipeline-run",
            {
                "observed_at": current.isoformat(),
                "structures": [
                    [item.source_plan_id, item.payload_hash]
                    for item in unique_structures
                ],
            },
        )
        signal_span = self._latency_start(
            "signal_compute",
            observed_at=current,
            correlation_id=run_id,
            metadata={"candidate_count": len(unique_structures)},
        )
        scored = [self._score_candidate(item, current) for item in unique_structures]
        scored.sort(key=lambda item: item.rank_key, reverse=True)
        self._latency_finish(
            signal_span,
            observed_at=current,
            metadata={"scored_count": len(scored)},
        )
        results: list[CandidateResult] = []
        attempted: str | None = None
        overall_attempt_status: PipelineStatus | None = None
        execution_claimed = False
        global_failures = self._global_policy_failures(current, broker_snapshot)

        for rank, candidate in enumerate(scored, start=1):
            if execution_claimed:
                results.append(self._result(candidate, rank, ("LOWER_QUALITY_NOT_SELECTED",)))
                continue
            failures = list(candidate.failures)
            failures.extend(global_failures)
            if failures:
                results.append(self._result(candidate, rank, _unique(failures)))
                continue
            assert candidate.instrument is not None
            assert candidate.validation is not None
            assert candidate.setup is not None
            assert candidate.execution is not None
            risk_span = self._latency_start(
                "preflight_risk",
                observed_at=current,
                correlation_id=candidate.structure.source_plan_id,
                metadata={"rank": rank, "symbol": candidate.structure.symbol},
            )
            plan, risk, market, planning_failures = self._plan_candidate(
                candidate=candidate,
                broker_snapshot=broker_snapshot,
                latch=latch,
                now=current,
            )
            self._latency_finish(
                risk_span,
                observed_at=current,
                metadata={
                    "allowed": bool(risk is not None and risk.allowed),
                    "failure_count": len(planning_failures),
                },
            )
            failures.extend(planning_failures)
            if failures or plan is None or risk is None or market is None:
                results.append(
                    self._result(
                        candidate,
                        rank,
                        _unique(failures or ("PLAN_NOT_CREATED",)),
                        plan=plan,
                        risk=risk,
                        market=market,
                    )
                )
                continue

            # The coordinator repeats signed-plan, market, risk, and broker
            # capability validation immediately around review/place.
            outcome = self.execution.submit_entry(
                plan=plan,
                risk_decision=risk,
                broker_snapshot=broker_snapshot,
                now=current,
            )
            attempted = plan.plan_id
            execution_claimed = True
            attempt_status = {
                ExecutionStatus.ACKNOWLEDGED: PipelineStatus.ACKNOWLEDGED,
                ExecutionStatus.BLOCKED: PipelineStatus.ATTEMPT_BLOCKED,
                ExecutionStatus.REJECTED: PipelineStatus.ATTEMPT_FAILED,
                ExecutionStatus.FAILED: PipelineStatus.ATTEMPT_FAILED,
                ExecutionStatus.UNKNOWN: PipelineStatus.SUBMISSION_UNKNOWN,
            }[outcome.status]
            overall_attempt_status = attempt_status
            results.append(
                self._result(
                    candidate,
                    rank,
                    outcome.failure_codes,
                    status=attempt_status,
                    plan=plan,
                    risk=risk,
                    market=market,
                    execution=outcome,
                )
            )

        status = overall_attempt_status or PipelineStatus.BLOCKED
        return PipelineRunResult(
            status=status,
            candidates=tuple(results),
            attempted_plan_id=attempted,
            message=(
                "one quality-ranked candidate reached the durable execution boundary; status does not imply a fill"
                if attempted is not None
                else "no candidate passed every independent market, policy, and risk gate"
            ),
        )

    def _latency_start(
        self,
        stage: str,
        *,
        observed_at: datetime,
        correlation_id: str,
        metadata: Mapping[str, Any],
    ) -> LatencySpan | None:
        if self.latency is None:
            return None
        try:
            return self.latency.start(
                stage,
                observed_at=observed_at,
                correlation_id=correlation_id,
                metadata=metadata,
            )
        except Exception:
            return None

    def _latency_finish(
        self,
        span: LatencySpan | None,
        *,
        observed_at: datetime,
        metadata: Mapping[str, Any],
    ) -> None:
        if self.latency is None or span is None:
            return
        try:
            self.latency.finish(span, observed_at=observed_at, metadata=metadata)
        except Exception:
            return

    def _global_policy_failures(
        self, now: datetime, broker_snapshot: AccountSnapshot
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if not self.policy.live_entries_configured:
            failures.append("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
        failures.extend(self.policy.activation_blockers)
        try:
            if self.thresholds != PipelineThresholds.from_policy(self.policy):
                failures.append("PIPELINE_THRESHOLDS_DO_NOT_MATCH_SIGNED_CONFIG")
        except (TypeError, ValueError):
            failures.append("SIGNED_PIPELINE_THRESHOLDS_UNRESOLVED_OR_INVALID")
        if self.policy.calendar.lane(now) != "regular_entry":
            failures.append("REGULAR_ENTRY_LANE_CLOSED")
        try:
            self.policy.require_account(
                broker_snapshot.account_masked,
                broker_snapshot.account_type,
            )
        except ValueError as exc:
            failures.append("ACCOUNT_POLICY_INVALID")
        return _unique(failures)

    def _score_candidate(
        self, structure: PreparedStructure, now: datetime
    ) -> _ScoredCandidate:
        failures = list(self._structure_failures(structure, now))
        instrument: InstrumentEvidence | None = None
        validation: LiveValidationEvidence | None = None
        setup: ScoreResult | None = None
        execution: ScoreResult | None = None
        try:
            instrument = self.instrument_evidence.get_instrument_evidence(
                structure.symbol, now=now
            )
        except Exception as exc:
            failures.append(f"INSTRUMENT_EVIDENCE_PROVIDER_FAILED:{type(exc).__name__}")
        if instrument is None:
            failures.append("INSTRUMENT_EVIDENCE_MISSING")
        else:
            try:
                failures.extend(self._instrument_failures(instrument, structure, now))
            except (AttributeError, TypeError, ValueError) as exc:
                failures.append("INSTRUMENT_EVIDENCE_INVALID")
        try:
            validation = self.quality_evidence.revalidate_structure(
                structure, now=now
            )
        except Exception as exc:
            failures.append(f"LIVE_REVALIDATION_PROVIDER_FAILED:{type(exc).__name__}")
        if validation is None:
            failures.append("INDEPENDENT_LIVE_REVALIDATION_MISSING")
        else:
            try:
                failures.extend(self._validation_failures(validation, structure, now))
            except (AttributeError, TypeError, ValueError) as exc:
                failures.append("LIVE_REVALIDATION_INVALID")
            try:
                setup = score_setup(validation.setup_components)
            except (TypeError, ValueError) as exc:
                failures.append("SETUP_SCORE_COMPONENTS_INCOMPLETE_OR_INVALID")
            try:
                execution = score_execution(
                    validation.execution_components,
                    instrument_kind="equity",
                )
            except (TypeError, ValueError) as exc:
                failures.append("EXECUTION_SCORE_COMPONENTS_INCOMPLETE_OR_INVALID")
            if self.thresholds.score_policy == "threshold_gated":
                assert self.thresholds.minimum_setup_score is not None
                assert self.thresholds.minimum_execution_score is not None
                if (
                    setup is not None
                    and setup.score < self.thresholds.minimum_setup_score
                ):
                    failures.append("SETUP_SCORE_BELOW_MINIMUM")
                if (
                    execution is not None
                    and execution.score < self.thresholds.minimum_execution_score
                ):
                    failures.append("EXECUTION_SCORE_BELOW_MINIMUM")
        return _ScoredCandidate(
            structure=structure,
            instrument=instrument,
            validation=validation,
            setup=setup,
            execution=execution,
            failures=_unique(failures),
        )

    def _structure_failures(
        self, structure: PreparedStructure, now: datetime
    ) -> tuple[str, ...]:
        failures: list[str] = []
        try:
            SetupID(structure.setup_id)
        except ValueError:
            failures.append("UNKNOWN_SETUP_ID")
        age = (now - _aware_utc(structure.observed_at, "structure.observed_at")).total_seconds()
        if age < -1 or age > int(self.policy.config["market_data"]["candidate_max_age_seconds"]):
            failures.append("SHADOW_STRUCTURE_STALE_OR_FUTURE")
        if structure.entry_limit <= Decimal("5"):
            failures.append("PRICE_NOT_STRICTLY_ABOVE_5")
        if structure.structural_stop <= 0 or structure.structural_stop >= structure.entry_limit:
            failures.append("SHADOW_STRUCTURE_GEOMETRY_INVALID")
        if not structure.targets or any(target <= structure.entry_limit for target in structure.targets):
            failures.append("SHADOW_TARGET_GEOMETRY_INVALID")
        if (
            structure.payload.get("trade_authority") is not False
            or structure.payload.get("broker_authority") is not False
            or structure.payload.get("book_mode") != "SHADOW"
        ):
            failures.append("SHADOW_SOURCE_AUTHORITY_BOUNDARY_UNPROVEN")
        return _unique(failures)

    def _instrument_failures(
        self,
        evidence: InstrumentEvidence,
        structure: PreparedStructure,
        now: datetime,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if not evidence.evidence_id or not evidence.instrument_id or not evidence.source:
            failures.append("INSTRUMENT_EVIDENCE_PROVENANCE_MISSING")
        if evidence.symbol.strip().upper() != structure.symbol.strip().upper():
            failures.append("INSTRUMENT_EVIDENCE_SYMBOL_MISMATCH")
        age = (now - _aware_utc(evidence.observed_at, "instrument.observed_at")).total_seconds()
        if age < -1 or age > int(self.policy.config["evidence"]["quote_max_age_seconds"]):
            failures.append("INSTRUMENT_EVIDENCE_STALE_OR_FUTURE")
        if evidence.asset_type != "stock" or evidence.exchange_listed is not True:
            failures.append("INSTRUMENT_OUTSIDE_ALLOWED_SCOPE")
        eligibility_age = (
            abs(
                (
                    now
                    - _aware_utc(
                        evidence.eligibility_at, "instrument.eligibility_at"
                    )
                ).total_seconds()
            )
            if evidence.eligibility_at is not None
            else math.inf
        )
        if (
            evidence.eligibility_scope != "current_regular_session"
            or eligibility_age
            > int(self.policy.config["evidence"]["quote_max_age_seconds"])
        ):
            failures.append("INSTRUMENT_ELIGIBILITY_NOT_CURRENT")
        if (
            evidence.broker_tradable is not True
            or evidence.regular_hours_eligible is not True
        ):
            failures.append("BROKER_NOT_CURRENTLY_TRADABLE")
        return _unique(failures)

    def _validation_failures(
        self,
        evidence: LiveValidationEvidence,
        structure: PreparedStructure,
        now: datetime,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if not evidence.evidence_id:
            failures.append("LIVE_REVALIDATION_PROVENANCE_MISSING")
        if evidence.source_plan_id != structure.source_plan_id:
            failures.append("LIVE_REVALIDATION_SOURCE_PLAN_MISMATCH")
        if evidence.symbol.strip().upper() != structure.symbol.strip().upper():
            failures.append("LIVE_REVALIDATION_SYMBOL_MISMATCH")
        if evidence.shadow_proposal_grants_authority is not False:
            failures.append("SHADOW_PROPOSAL_ATTEMPTED_TO_GRANT_AUTHORITY")
        age = (now - _aware_utc(evidence.observed_at, "validation.observed_at")).total_seconds()
        if age < -1 or age > int(self.policy.config["evidence"]["quote_max_age_seconds"]):
            failures.append("LIVE_REVALIDATION_STALE_OR_FUTURE")
        if (
            decimal_value(evidence.entry_limit, "validated entry") != structure.entry_limit
            or decimal_value(evidence.structural_stop, "validated stop")
            != structure.structural_stop
            or tuple(decimal_value(item, "validated target") for item in evidence.targets)
            != structure.targets
        ):
            failures.append("INDEPENDENT_GEOMETRY_MISMATCH")
        try:
            reserve = decimal_value(
                evidence.execution_reserve_per_share,
                "execution_reserve_per_share",
            )
            if reserve <= 0:
                failures.append("POSITIVE_EXECUTION_RESERVE_REQUIRED")
        except ValueError as exc:
            failures.append("EXECUTION_RESERVE_INVALID")
        deferred = evidence.deferred_hard_gate_facts
        if (
            not isinstance(deferred, frozenset)
            or not deferred.issubset(POST_SIZING_HARD_GATE_FACTS)
        ):
            failures.append("DEFERRED_HARD_GATE_FACTS_INVALID")
            deferred = frozenset()
        supplied = set(evidence.hard_gate_facts)
        if supplied != REQUIRED_HARD_GATE_FACTS:
            missing = sorted(REQUIRED_HARD_GATE_FACTS - supplied)
            extra = sorted(supplied - REQUIRED_HARD_GATE_FACTS)
            failures.append(f"HARD_GATE_FACTS_INCOMPLETE:missing={missing}:extra={extra}")
        else:
            for name in sorted(REQUIRED_HARD_GATE_FACTS):
                if name in deferred:
                    if evidence.hard_gate_facts[name] is not False:
                        failures.append(f"DEFERRED_HARD_GATE_MUST_BE_UNASSERTED:{name}")
                elif evidence.hard_gate_facts[name] is not True:
                    failures.append(f"HARD_GATE_FAILED:{name}")
        # Scoring helpers enforce the complete exact component schemas.  These
        # checks make the failure readable without silently filling anything.
        if set(evidence.setup_components) != set(BASELINE_SETUP_WEIGHTS):
            failures.append("SETUP_SCORE_COMPONENT_SET_MISMATCH")
        if set(evidence.execution_components) != set(EQUITY_EXECUTION_WEIGHTS):
            failures.append("EXECUTION_SCORE_COMPONENT_SET_MISMATCH")
        return _unique(failures)

    def _plan_candidate(
        self,
        *,
        candidate: _ScoredCandidate,
        broker_snapshot: AccountSnapshot,
        latch: SessionLatch,
        now: datetime,
    ) -> tuple[
        ExpiringPlan | None,
        RiskDecision | None,
        EvidenceDecision | None,
        tuple[str, ...],
    ]:
        assert candidate.instrument is not None
        assert candidate.validation is not None
        assert candidate.setup is not None
        assert candidate.execution is not None
        structure = candidate.structure
        validation = candidate.validation
        failures: list[str] = []
        quote, bars, _watermark, degradation = self.market_data.symbol_state(
            structure.symbol
        )
        if quote is None:
            return None, None, None, ("QUOTE_MISSING",)
        if quote.bid > quote.ask:
            failures.append("CROSSED_QUOTE")
        if quote.tradable is not True or not candidate.instrument.broker_tradable:
            failures.append("BROKER_TRADABILITY_EVIDENCE_CONFLICT")
        if quote.halted:
            failures.append("MARKET_HALTED")
        completed_at = _aware_utc(
            validation.completed_bar_end, "completed_bar_end"
        )
        bar = next((item for item in bars if item.end_at == completed_at), None)
        if bar is None:
            failures.append("CAUSAL_COMPLETED_BAR_MISSING")
        if degradation is not None:
            failures.append(degradation)
        if failures:
            return None, None, None, _unique(failures)
        assert bar is not None

        quality_tier = (
            "a_plus"
            if self.thresholds.a_plus_enabled
            and self.thresholds.a_plus_setup_score is not None
            and self.thresholds.a_plus_execution_score is not None
            and candidate.setup.score >= self.thresholds.a_plus_setup_score
            and candidate.execution.score >= self.thresholds.a_plus_execution_score
            else "normal"
        )
        reserve_per_share = decimal_value(
            validation.execution_reserve_per_share,
            "execution_reserve_per_share",
        )
        created_at = max(
            _aware_utc(structure.observed_at, "structure.observed_at"),
            _aware_utc(validation.observed_at, "validation.observed_at"),
            _aware_utc(candidate.instrument.observed_at, "instrument.observed_at"),
            quote.observed_at.astimezone(timezone.utc),
            bar.end_at.astimezone(timezone.utc),
        )
        if created_at > now:
            return None, None, None, ("PLAN_EVIDENCE_FUTURE_DATED",)
        expires_at = created_at + timedelta(
            seconds=int(self.policy.config["evidence"]["plan_ttl_seconds"])
        )
        source_event_ids = self._source_event_ids(
            structure=structure,
            validation=validation,
            instrument=candidate.instrument,
            quote=quote,
            bar=bar,
        )

        existing_quantity = self._exact_replay_quantity(
            structure=structure,
            validation=validation,
            instrument=candidate.instrument,
            quality_tier=quality_tier,
            created_at=created_at,
            expires_at=expires_at,
            source_event_ids=source_event_ids,
            quote=quote,
        )
        price_map = self.market_data.quote_prices()
        preliminary_snapshot, snapshot_failures = build_account_risk_snapshot(
            policy=self.policy,
            state=self.state,
            broker_snapshot=broker_snapshot,
            now=now,
            prices=price_map,
            exclude_plan_id=None,
        )
        failures.extend(snapshot_failures)
        if preliminary_snapshot is None:
            return None, None, None, _unique(failures)

        if existing_quantity is not None:
            plan = self._build_plan(
                structure=structure,
                validation=validation,
                instrument=candidate.instrument,
                quantity=existing_quantity,
                quality_tier=quality_tier,
                quote=quote,
                created_at=created_at,
                expires_at=expires_at,
                source_event_ids=source_event_ids,
            )
            risk_snapshot, replay_failures = build_account_risk_snapshot(
                policy=self.policy,
                state=self.state,
                broker_snapshot=broker_snapshot,
                now=now,
                prices=price_map,
                exclude_plan_id=plan.plan_id,
            )
            failures.extend(replay_failures)
            if risk_snapshot is None:
                return plan, None, None, _unique(failures)
        else:
            plan, risk_snapshot, sizing_failures = self._size_plan(
                structure=structure,
                validation=validation,
                instrument=candidate.instrument,
                quality_tier=quality_tier,
                quote=quote,
                created_at=created_at,
                expires_at=expires_at,
                source_event_ids=source_event_ids,
                snapshot=preliminary_snapshot,
                latch=latch,
                now=now,
            )
            failures.extend(sizing_failures)
            if plan is None or risk_snapshot is None:
                return None, None, None, _unique(failures)

        # Symbol-level ADD/reentry protection is independent of account risk.
        position = next(
            (
                item
                for item in broker_snapshot.equity_positions
                if item.symbol == structure.symbol and item.quantity > 0
            ),
            None,
        )
        if position is not None and existing_quantity is None:
            failures.append("ADD_PROHIBITED_EXISTING_POSITION")
        if existing_quantity is None and self._prior_symbol_plan_today(
            structure.symbol, now, broker_snapshot=broker_snapshot
        ):
            failures.append("REENTRY_PROHIBITED_FOR_ACCOUNT_DAY")

        risk = evaluate_entry(
            policy=self.policy,
            snapshot=risk_snapshot,
            plan=plan,
            latch=latch,
            now=now,
        )
        failures.extend(risk.failures)
        market = self._market_decision(plan, now)
        failures.extend(market.failures)
        try:
            plan.validate(self.policy, now)
        except (TypeError, ValueError) as exc:
            failures.append("PLAN_INVALID")
        return plan, risk, market, _unique(failures)

    def _size_plan(
        self,
        *,
        structure: PreparedStructure,
        validation: LiveValidationEvidence,
        instrument: InstrumentEvidence,
        quality_tier: str,
        quote: Quote,
        created_at: datetime,
        expires_at: datetime,
        source_event_ids: tuple[str, ...],
        snapshot: AccountRiskSnapshot,
        latch: SessionLatch,
        now: datetime,
    ) -> tuple[ExpiringPlan | None, AccountRiskSnapshot | None, tuple[str, ...]]:
        per_share_planned = validation.entry_limit - validation.structural_stop
        reserve = validation.execution_reserve_per_share
        per_share_stress = per_share_planned + reserve
        if per_share_planned <= 0 or reserve <= 0 or per_share_stress <= 0:
            return None, snapshot, ("POSITIVE_STOP_DISTANCE_AND_RESERVE_REQUIRED",)
        equity = snapshot.usable_equity
        quality_prefix = "a_plus" if quality_tier == "a_plus" else "normal"
        quantity_caps = [
            min(snapshot.cash, snapshot.unleveraged_buying_power) / validation.entry_limit,
        ]
        if self.policy.daily_starting_equity_risk:
            try:
                quantity_caps.append(daily_starting_equity_capacity(
                    self.policy, starting_equity=snapshot.daily_starting_equity,
                    total_equity=snapshot.total_equity,
                    external_cash_flow=snapshot.daily_external_cash_flow,
                ) / per_share_stress)
            except (ValueError, TypeError):
                return None, snapshot, ("DAILY_STARTING_EQUITY_EVIDENCE_INCOMPLETE",)
        elif self.policy.dollar_headroom_risk:
            quantity_caps.append(dollar_headroom_capacity(
                self.policy, realized_pnl=snapshot.daily_realized_pnl,
                profit_goal_crossed=latch.profit_goal_crossed,
            ) / per_share_stress)
        else:
            quantity_caps.extend((
                _risk_cap(
                    self.policy, equity, f"{quality_prefix}_planned_risk_pct",
                    f"{quality_prefix}_planned_risk_dollars",
                ) / per_share_planned,
                _risk_cap(
                    self.policy, equity, "max_single_trade_stress_risk_pct",
                    "max_single_trade_stress_risk_dollars",
                ) / per_share_stress,
            ))
        depth_multiple = self.policy.config["evidence"].get("minimum_depth_multiple")
        if depth_multiple is not None:
            multiple = decimal_value(depth_multiple, "minimum_depth_multiple")
            if multiple <= 0:
                return None, snapshot, ("NUMERIC_DEPTH_GATE_INVALID",)
            quantity_caps.append(Decimal(min(quote.bid_size, quote.ask_size)) / multiple)
        upper = int(min(quantity_caps).to_integral_value(rounding=ROUND_FLOOR))
        if upper < 1:
            one = self._build_plan(
                structure=structure,
                validation=validation,
                instrument=instrument,
                quantity=1,
                quality_tier=quality_tier,
                quote=quote,
                created_at=created_at,
                expires_at=expires_at,
                source_event_ids=source_event_ids,
            )
            decision = evaluate_entry(
                policy=self.policy,
                snapshot=snapshot,
                plan=one,
                latch=latch,
                now=now,
            )
            return None, snapshot, _unique(
                ("NO_WHOLE_SHARE_RISK_CAPACITY",) + decision.failures
            )

        # All quantitative entry constraints are monotonic in quantity.  A
        # deterministic binary search finds the largest approved whole-share
        # size without a floating-point approximation or an arbitrary max.
        low, high = 1, upper
        selected: tuple[ExpiringPlan, RiskDecision] | None = None
        one_share_failures: tuple[str, ...] = ()
        while low <= high:
            quantity = (low + high) // 2
            plan = self._build_plan(
                structure=structure,
                validation=validation,
                instrument=instrument,
                quantity=quantity,
                quality_tier=quality_tier,
                quote=quote,
                created_at=created_at,
                expires_at=expires_at,
                source_event_ids=source_event_ids,
            )
            decision = evaluate_entry(
                policy=self.policy,
                snapshot=snapshot,
                plan=plan,
                latch=latch,
                now=now,
            )
            if quantity == 1:
                one_share_failures = decision.failures
            if decision.allowed:
                selected = (plan, decision)
                low = quantity + 1
            else:
                high = quantity - 1
        if selected is None:
            if not one_share_failures:
                one = self._build_plan(
                    structure=structure,
                    validation=validation,
                    instrument=instrument,
                    quantity=1,
                    quality_tier=quality_tier,
                    quote=quote,
                    created_at=created_at,
                    expires_at=expires_at,
                    source_event_ids=source_event_ids,
                )
                one_share_failures = evaluate_entry(
                    policy=self.policy,
                    snapshot=snapshot,
                    plan=one,
                    latch=latch,
                    now=now,
                ).failures
            return None, snapshot, _unique(
                ("NO_WHOLE_SHARE_RISK_CAPACITY",) + one_share_failures
            )
        return selected[0], snapshot, ()

    def _build_plan(
        self,
        *,
        structure: PreparedStructure,
        validation: LiveValidationEvidence,
        instrument: InstrumentEvidence,
        quantity: int,
        quality_tier: str,
        quote: Quote,
        created_at: datetime,
        expires_at: datetime,
        source_event_ids: tuple[str, ...],
    ) -> ExpiringPlan:
        return ExpiringPlan.build(
            strategy_id=self.policy.strategy_id,
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            account_last4=self.policy.account_last4,
            symbol=structure.symbol,
            instrument_id=instrument.instrument_id,
            setup_id=structure.setup_id,
            quantity=quantity,
            entry_limit=validation.entry_limit,
            structural_stop=validation.structural_stop,
            targets=validation.targets,
            execution_reserve_per_share=validation.execution_reserve_per_share,
            market_hours="regular_hours",
            time_in_force="gfd",
            quality_tier=quality_tier,
            completed_bar_end=validation.completed_bar_end,
            quote_observed_at=quote.observed_at,
            created_at=created_at,
            expires_at=expires_at,
            source_event_ids=source_event_ids,
            direction="long",
            allow_add=False,
            allow_reentry=False,
        )

    def _market_decision(self, plan: ExpiringPlan, now: datetime) -> EvidenceDecision:
        config = self.policy.config
        return self.market_data.validate_entry_evidence(
            symbol=plan.symbol,
            now=now,
            plan_created_at=plan.created_at,
            plan_expires_at=plan.expires_at,
            causal_bar_end=plan.completed_bar_end,
            quote_max_age_seconds=int(config["evidence"]["quote_max_age_seconds"]),
            completed_bar_max_age_seconds=int(
                config["evidence"]["completed_bar_max_age_seconds"]
            ),
            minimum_session_volume=int(
                config["scope"]["minimum_session_volume_inclusive"]
            ),
            max_spread_bps=config["evidence"].get("max_spread_bps"),
            minimum_depth_multiple=config["evidence"].get("minimum_depth_multiple"),
            quantity=plan.quantity,
        )

    def _source_event_ids(
        self,
        *,
        structure: PreparedStructure,
        validation: LiveValidationEvidence,
        instrument: InstrumentEvidence,
        quote: Quote,
        bar: CompletedBar,
    ) -> tuple[str, ...]:
        quote_event = _event_id(
            "quote",
            {
                "symbol": quote.symbol,
                "bid": format(quote.bid, "f"),
                "ask": format(quote.ask, "f"),
                "bid_size": quote.bid_size,
                "ask_size": quote.ask_size,
                "size_unit": quote.size_unit,
                "size_source_version": quote.size_source_version,
                "depth_scope": quote.depth_scope,
                "venue_bid_at": quote.venue_bid_at.isoformat(),
                "venue_ask_at": quote.venue_ask_at.isoformat(),
                "observed_at": quote.observed_at.isoformat(),
                "source": quote.source,
            },
        )
        return _unique(
            (
                bar.source_event_id,
                quote_event,
                f"shadow-plan:{structure.source_plan_id}:{structure.payload_hash}",
                f"live-validation:{validation.evidence_id}",
                f"instrument:{instrument.evidence_id}",
            )
        )

    def _exact_replay_quantity(
        self,
        *,
        structure: PreparedStructure,
        validation: LiveValidationEvidence,
        instrument: InstrumentEvidence,
        quality_tier: str,
        created_at: datetime,
        expires_at: datetime,
        source_event_ids: tuple[str, ...],
        quote: Quote,
    ) -> int | None:
        rows = self.state.rows(
            """SELECT plan_id,quantity FROM plans
                 WHERE account_key=? AND symbol=? AND setup_id=?
                   AND policy_hash=? AND config_hash=?
                 ORDER BY created_at DESC""",
            (
                self.policy.account_key,
                structure.symbol,
                structure.setup_id,
                self.policy.policy_hash,
                self.policy.config_hash,
            ),
        )
        for row in rows:
            quantity = int(row["quantity"])
            candidate = self._build_plan(
                structure=structure,
                validation=validation,
                instrument=instrument,
                quantity=quantity,
                quality_tier=quality_tier,
                quote=quote,
                created_at=created_at,
                expires_at=expires_at,
                source_event_ids=source_event_ids,
            )
            if candidate.plan_id == row["plan_id"]:
                return quantity
        return None

    def _prior_symbol_plan_today(
        self, symbol: str, now: datetime, *, broker_snapshot: AccountSnapshot
    ) -> bool:
        """Block reentry unless every same-day attempt was rejected unused.

        A released reservation alone is insufficient: it can also describe a
        completed trade.  Only the existing immediate zero-fill release proof
        for a rejected placement or failed pre-submit review permits a *new*
        plan.  Exact plan replay retains its separate idempotent path.
        """
        zone = ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
        trading_date = now.astimezone(zone).date()
        rows = self.state.rows(
            "SELECT plan_id,created_at FROM plans WHERE account_key=? AND symbol=?",
            (
                self.policy.account_key,
                symbol,
            ),
        )
        prior_plans = [
            row for row in rows
            if datetime.fromisoformat(str(row["created_at"])).astimezone(zone).date()
            == trading_date
        ]
        if not prior_plans:
            return False
        if any(
            item.symbol == symbol and item.quantity != ZERO
            for item in broker_snapshot.equity_positions
        ) or any(
            item.symbol == symbol and not item.state.terminal
            for item in broker_snapshot.equity_orders
        ):
            return True
        return any(
            not self._rejected_attempt_proven_unused(
                str(row["plan_id"]), broker_snapshot=broker_snapshot
            )
            for row in prior_plans
        )

    def _rejected_attempt_proven_unused(
        self, plan_id: str, *, broker_snapshot: AccountSnapshot
    ) -> bool:
        intents = self.state.rows(
            """SELECT i.*,r.state AS reservation_state
                 FROM order_intents i LEFT JOIN risk_reservations r
                   ON r.reservation_id=i.reservation_id
                 WHERE i.plan_id=?""",
            (plan_id,),
        )
        if len(intents) != 1:
            return False
        intent = intents[0]
        if (
            intent["kind"] != "ENTRY"
            or intent["state"] not in {"REJECTED", "FAILED"}
            or intent["reservation_state"] != "RELEASED"
        ):
            return False
        proofs = self.state.rows(
            """SELECT event_type,payload_json FROM audit_events
                 WHERE (entity_type='order_intent' AND entity_id=? AND event_type=?)
                    OR (entity_type='risk_reservation' AND entity_id=?
                        AND event_type='RISK_RESERVATION_RELEASED')
                 ORDER BY sequence""",
            (intent["intent_id"], f"INTENT_{intent['state']}", intent["reservation_id"]),
        )
        by_event = {str(row["event_type"]): row["payload_json"] for row in proofs}
        try:
            terminal = json.loads(by_event[f"INTENT_{intent['state']}"])
            release = json.loads(by_event["RISK_RESERVATION_RELEASED"])
        except (KeyError, TypeError, ValueError):
            return False
        if not isinstance(terminal, dict) or not isinstance(release, dict):
            return False
        rejected_unused = (
            intent["state"] == "REJECTED" and terminal.get("known_reject") is True
        ) or (
            intent["state"] == "FAILED"
            and terminal.get("phase") in {"review", "review_validation"}
        )
        if (
            not rejected_unused
            or release.get("proof") != "KNOWN_TERMINAL_ZERO_FILL"
            or release.get("intent_state") != intent["state"]
        ):
            return False
        orders = self.state.rows(
            """SELECT o.*, (SELECT COUNT(*) FROM fills f
                            WHERE f.broker_order_id=o.broker_order_id) AS fill_count
                 FROM broker_orders o WHERE o.intent_id=?""",
            (intent["intent_id"],),
        )
        if any(
            row["state"] != "REJECTED"
            or row["cumulative_filled_quantity"] != 0
            or int(row["fill_count"]) != 0
            for row in orders
        ):
            return False
        order_ids = {str(row["broker_order_id"]) for row in orders}
        return not any(
            (
                order.client_ref_id == intent["client_ref"]
                or order.broker_order_id in order_ids
            )
            and (
                order.state.value != "REJECTED"
                or order.cumulative_filled_quantity != ZERO
                or bool(order.fills)
            )
            for order in broker_snapshot.equity_orders
        )

    @staticmethod
    def _result(
        candidate: _ScoredCandidate,
        rank: int,
        failures: Sequence[str],
        *,
        status: PipelineStatus | None = None,
        plan: ExpiringPlan | None = None,
        risk: RiskDecision | None = None,
        market: EvidenceDecision | None = None,
        execution: ExecutionOutcome | None = None,
    ) -> CandidateResult:
        if status is None:
            status = (
                PipelineStatus.NOT_SELECTED
                if "LOWER_QUALITY_NOT_SELECTED" in failures
                else PipelineStatus.BLOCKED
            )
        setup_score = candidate.setup.score if candidate.setup is not None else None
        execution_score = (
            candidate.execution.score if candidate.execution is not None else None
        )
        quality_tier = None
        if setup_score is not None and execution_score is not None:
            quality_tier = (
                "a_plus"
                if plan is not None and plan.quality_tier == "a_plus"
                else "normal"
            )
        return CandidateResult(
            source_plan_id=candidate.structure.source_plan_id,
            symbol=candidate.structure.symbol,
            status=status,
            failures=_unique(tuple(failures)),
            rank=rank,
            setup_score=setup_score,
            execution_score=execution_score,
            quality_tier=quality_tier,
            quantity=(plan.quantity if plan is not None else None),
            plan=plan,
            risk_decision=risk,
            market_decision=market,
            execution=execution,
        )


class _InstrumentTradabilityView:
    """Adapt full instrument evidence to Massive's narrow boolean contract.

    The adapter deliberately catches provider failures and returns ``False``.
    Massive cannot prove broker eligibility on its own, and an exception or
    omitted record must therefore become a non-tradable quote rather than a
    permissive default.
    """

    def __init__(
        self,
        provider: InstrumentEvidenceProvider,
        *,
        maximum_age_seconds: int,
    ) -> None:
        self.provider = provider
        self.maximum_age_seconds = maximum_age_seconds

    def is_tradable(self, symbol: str, *, as_of: datetime) -> bool:
        current = _aware_utc(as_of, "tradability.as_of")
        try:
            evidence = self.provider.get_instrument_evidence(symbol, now=current)
            if evidence is None:
                return False
            observed = _aware_utc(evidence.observed_at, "instrument.observed_at")
            age = (current - observed).total_seconds()
            return bool(
                evidence.evidence_id
                and evidence.instrument_id
                and evidence.source
                and evidence.symbol.strip().upper() == symbol.strip().upper()
                and -1 <= age <= self.maximum_age_seconds
                and evidence.asset_type == "stock"
                and evidence.exchange_listed is True
                and evidence.broker_tradable is True
                and evidence.regular_hours_eligible is True
                and evidence.eligibility_scope == "current_regular_session"
                and evidence.eligibility_at is not None
                and abs((current - evidence.eligibility_at).total_seconds())
                <= self.maximum_age_seconds
            )
        except Exception:
            return False


class PremarketAnalysisExecutor:
    """Read-only premarket ranking over shared Massive and IBKR evidence.

    This object intentionally has no broker client, state store, mutation
    authority, order coordinator, or plan sealer.  It can subscribe/hydrate the
    existing read-only Massive cache and request IBKR contract details, but its
    public result is only a deterministic fact set with explicit deferred
    execution gates.
    """

    def __init__(
        self,
        *,
        source: PreparedStructureSource,
        policy: PolicyBundle,
        market_data: MarketDataCache,
        instrument_evidence: InstrumentEvidenceProvider,
        tradability: TradabilityProvider,
        thresholds: PipelineThresholds,
    ) -> None:
        self.source = source
        self.policy = policy
        self.market_data = market_data
        self.instrument_evidence = instrument_evidence
        self.tradability = tradability
        self.thresholds = thresholds

    def due(
        self,
        *,
        now: datetime,
        last_completed_slot: datetime | None = None,
    ) -> PremarketAnalysisSchedule:
        current = _aware_utc(now, "premarket analysis time")
        sessions = self.policy.config.get("sessions", {})
        try:
            interval = int(sessions.get("premarket_analysis_interval_minutes"))
            start_clock = time.fromisoformat(str(sessions.get("premarket_start")))
        except (TypeError, ValueError):
            interval = 0
            start_clock = time(7, 0)
        lane = self.policy.calendar.lane(current)
        if (
            sessions.get("premarket_mode") != "analysis_only"
            or sessions.get("premarket_orders_enabled") is not False
            or interval != 30
        ):
            return PremarketAnalysisSchedule(
                lane=lane,
                due=False,
                interval_minutes=interval,
                scheduled_for=None,
                next_due_at=None,
                reason="PREMARKET_ANALYSIS_POLICY_INVALID",
            )
        if lane != "premarket_attended":
            return PremarketAnalysisSchedule(
                lane=lane,
                due=False,
                interval_minutes=interval,
                scheduled_for=None,
                next_due_at=None,
                reason="OUTSIDE_PREMARKET_ANALYSIS_LANE",
            )
        zone = ZoneInfo(str(sessions.get("timezone")))
        local = current.astimezone(zone)
        session = self.policy.calendar.session_times(local.date())
        if session is None:
            return PremarketAnalysisSchedule(
                lane="closed",
                due=False,
                interval_minutes=interval,
                scheduled_for=None,
                next_due_at=None,
                reason="NO_VERIFIED_TRADING_SESSION",
            )
        start = datetime.combine(local.date(), start_clock, zone)
        elapsed_minutes = int((local - start).total_seconds() // 60)
        slot_local = start + timedelta(
            minutes=(elapsed_minutes // interval) * interval
        )
        slot = slot_local.astimezone(timezone.utc)
        next_local = slot_local + timedelta(minutes=interval)
        next_due = (
            next_local.astimezone(timezone.utc)
            if self.policy.calendar.lane(next_local) == "premarket_attended"
            else None
        )
        last: datetime | None = None
        if last_completed_slot is not None:
            try:
                last = _aware_utc(
                    last_completed_slot, "premarket last completed slot"
                )
                last_local = last.astimezone(zone)
                aligned_seconds = (last_local - start).total_seconds()
                if (
                    last_local.date() == local.date()
                    and (
                        aligned_seconds < 0
                        or aligned_seconds % (interval * 60) != 0
                        or last > slot
                    )
                ):
                    raise ValueError("unaligned or future slot")
                if last_local.date() != local.date():
                    last = None
            except (TypeError, ValueError):
                return PremarketAnalysisSchedule(
                    lane="premarket",
                    due=False,
                    interval_minutes=interval,
                    scheduled_for=slot,
                    next_due_at=next_due,
                    reason="PREMARKET_LAST_COMPLETED_SLOT_INVALID",
                )
        due = last is None or last < slot
        return PremarketAnalysisSchedule(
            lane="premarket",
            due=due,
            interval_minutes=interval,
            scheduled_for=slot,
            next_due_at=next_due,
            reason=("PREMARKET_ANALYSIS_DUE" if due else "PREMARKET_SLOT_COMPLETE"),
        )

    def analyze(
        self,
        *,
        now: datetime,
        last_completed_slot: datetime | None = None,
    ) -> PremarketAnalysisResult:
        current = _aware_utc(now, "premarket analysis time")
        schedule = self.due(now=current, last_completed_slot=last_completed_slot)
        if schedule.reason in {
            "PREMARKET_ANALYSIS_POLICY_INVALID",
            "NO_VERIFIED_TRADING_SESSION",
        }:
            return self._blocked(current, schedule, (schedule.reason,))
        if schedule.lane != "premarket":
            return PremarketAnalysisResult(
                status=PremarketAnalysisStatus.OUTSIDE_LANE,
                schedule=schedule,
                analysis_id=None,
                observed_at=current,
                candidates=(),
                blockers=(schedule.reason,),
                message="Premarket analysis is unavailable outside its analysis-only lane.",
            )
        if not schedule.due:
            return PremarketAnalysisResult(
                status=PremarketAnalysisStatus.NOT_DUE,
                schedule=schedule,
                analysis_id=None,
                observed_at=current,
                candidates=(),
                blockers=(
                    ()
                    if schedule.reason == "PREMARKET_SLOT_COMPLETE"
                    else (schedule.reason,)
                ),
                message="The current 30-minute premarket analysis slot is not due.",
            )

        try:
            health = self.source.health(now=current)
        except Exception as exc:
            return self._blocked(
                current,
                schedule,
                (f"MASSIVE_HEALTH_FAILED:{type(exc).__name__}",),
            )
        health_failures = tuple(str(value) for value in getattr(health, "blockers", ()))
        if getattr(health, "service_healthy", not health_failures) is not True:
            return self._blocked(
                current,
                schedule,
                health_failures or ("MASSIVE_SERVICE_UNHEALTHY",),
            )
        try:
            structures = self.source.prepared_structures(
                now=current,
                limit=int(self.policy.config["market_data"]["max_active_candidates"]),
            )
        except Exception as exc:
            return self._blocked(
                current,
                schedule,
                (f"MASSIVE_CANDIDATE_READ_FAILED:{type(exc).__name__}",),
            )
        unique: dict[tuple[str, str], PreparedStructure] = {}
        for structure in structures:
            unique[(structure.source_plan_id, structure.payload_hash)] = structure
        structures = tuple(unique[key] for key in sorted(unique))
        hydration_failures: tuple[str, ...] = ()
        if structures:
            try:
                hydration_failures = tuple(
                    str(value)
                    for value in self.source.hydrate_cache(
                        self.market_data,
                        structures=structures,
                        session_start=self._session_start(current),
                        now=current,
                        tradability=self.tradability,
                    )
                )
            except Exception as exc:
                return self._blocked(
                    current,
                    schedule,
                    (f"MASSIVE_CACHE_HYDRATION_FAILED:{type(exc).__name__}",),
                )

        session_open = self._regular_session_open(current)
        facts = [
            self._candidate_fact(
                structure=structure,
                now=current,
                session_open=session_open,
            )
            for structure in structures
        ]
        facts.sort(
            key=lambda value: (
                bool(value.hard_gate_failures),
                -min(value.setup_score, value.execution_score),
                -(value.setup_score + value.execution_score),
                -value.shadow_ranking_score,
                value.symbol,
                value.source_plan_id,
            )
        )
        ranked = tuple(replace(value, rank=index) for index, value in enumerate(facts, 1))
        payload = {
            "schema": "titan_premarket_analysis_2026-09-14_v1",
            "scheduled_for": schedule.scheduled_for.isoformat()
            if schedule.scheduled_for is not None
            else None,
            "observed_at": current.isoformat(),
            "blockers": list(_unique(hydration_failures)),
            "candidates": [self._candidate_payload(value) for value in ranked],
            "execution_authority": False,
            "approved_to_buy": False,
        }
        return PremarketAnalysisResult(
            status=PremarketAnalysisStatus.COMPLETED,
            schedule=schedule,
            analysis_id=_event_id("premarket-analysis", payload),
            observed_at=current,
            candidates=ranked,
            blockers=_unique(hydration_failures),
            message=(
                "Analysis-only premarket ranking completed; no candidate is "
                "approved to buy and no order action is available from this result."
            ),
        )

    def _candidate_fact(
        self,
        *,
        structure: PreparedStructure,
        now: datetime,
        session_open: datetime,
    ) -> PremarketCandidateFact:
        failures: list[str] = []
        try:
            SetupID(structure.setup_id)
        except ValueError:
            failures.append("UNKNOWN_SETUP_ID")
        structure_age = (
            now - _aware_utc(structure.observed_at, "structure observed_at")
        ).total_seconds()
        if structure_age < -1 or structure_age > int(
            self.policy.config["market_data"]["candidate_max_age_seconds"]
        ):
            failures.append("SHADOW_STRUCTURE_STALE_OR_FUTURE")
        if structure.entry_limit <= Decimal("5"):
            failures.append("PRICE_NOT_STRICTLY_ABOVE_5")
        if structure.structural_stop <= 0 or structure.structural_stop >= structure.entry_limit:
            failures.append("SHADOW_STRUCTURE_GEOMETRY_INVALID")
        if not structure.targets or any(
            target <= structure.entry_limit for target in structure.targets
        ):
            failures.append("SHADOW_TARGET_GEOMETRY_INVALID")
        if (
            structure.payload.get("trade_authority") is not False
            or structure.payload.get("broker_authority") is not False
            or structure.payload.get("book_mode") != "SHADOW"
        ):
            failures.append("SHADOW_SOURCE_AUTHORITY_BOUNDARY_UNPROVEN")

        instrument: InstrumentEvidence | None = None
        analysis_reader = getattr(
            self.instrument_evidence, "get_premarket_analysis_evidence", None
        )
        if not callable(analysis_reader):
            failures.append("IBKR_UPCOMING_SESSION_EVIDENCE_UNSUPPORTED")
        else:
            try:
                instrument = analysis_reader(
                    structure.symbol,
                    now=now,
                    regular_session_open=session_open,
                )
            except Exception as exc:
                failures.append(
                    f"IBKR_UPCOMING_SESSION_EVIDENCE_FAILED:{type(exc).__name__}"
                )
        if instrument is None:
            failures.append("IBKR_UPCOMING_SESSION_EVIDENCE_MISSING")
        else:
            instrument_age = (
                now
                - _aware_utc(instrument.observed_at, "instrument observed_at")
            ).total_seconds()
            if instrument_age < -1 or instrument_age > int(
                self.policy.config["evidence"]["quote_max_age_seconds"]
            ):
                failures.append("INSTRUMENT_EVIDENCE_STALE_OR_FUTURE")
            if (
                instrument.source != "ibkr:tws-contract-details"
                or not instrument.evidence_id
                or not instrument.instrument_id
            ):
                failures.append("IBKR_INSTRUMENT_PROVENANCE_INVALID")
            if instrument.symbol != structure.symbol:
                failures.append("INSTRUMENT_EVIDENCE_SYMBOL_MISMATCH")
            if (
                instrument.asset_type != "stock"
                or instrument.exchange_listed is not True
                or instrument.broker_tradable is not True
                or instrument.regular_hours_eligible is not True
            ):
                failures.append("IBKR_UPCOMING_REGULAR_SESSION_NOT_ELIGIBLE")
            if (
                instrument.eligibility_scope
                != "upcoming_regular_session_analysis"
                or instrument.eligibility_at != session_open
            ):
                failures.append("IBKR_UPCOMING_SESSION_SCOPE_MISMATCH")

        quote, bars, _watermark, degradation = self.market_data.symbol_state(
            structure.symbol
        )
        completed = tuple(item for item in bars if item.end_at <= now)
        latest_bar = max(completed, key=lambda item: item.end_at, default=None)
        session_start = self._session_start(now)
        session_volume = sum(
            item.volume
            for item in completed
            if session_start <= item.end_at <= now
        )
        if degradation is not None:
            failures.append(degradation)
        if latest_bar is None:
            failures.append("COMPLETED_MINUTE_BAR_MISSING")
        else:
            if (
                latest_bar.start_at.second != 0
                or latest_bar.start_at.microsecond != 0
                or latest_bar.end_at.second != 0
                or latest_bar.end_at.microsecond != 0
                or latest_bar.end_at - latest_bar.start_at != timedelta(minutes=1)
            ):
                failures.append("COMPLETED_MINUTE_BAR_INVALID")
            if (now - latest_bar.end_at).total_seconds() > int(
                self.policy.config["evidence"]["completed_bar_max_age_seconds"]
            ):
                failures.append("COMPLETED_MINUTE_BAR_STALE")
        if session_volume < int(
            self.policy.config["scope"]["minimum_session_volume_inclusive"]
        ):
            failures.append("VOLUME_BELOW_750000")

        spread: Decimal | None = None
        if quote is None:
            failures.append("QUOTE_MISSING")
        else:
            oldest_age = (now - quote.oldest_venue_at).total_seconds()
            newest_age = (now - quote.newest_venue_at).total_seconds()
            if newest_age < -1:
                failures.append("QUOTE_FUTURE_DATED")
            if oldest_age > int(
                self.policy.config["evidence"]["quote_max_age_seconds"]
            ):
                failures.append("QUOTE_STALE")
            if quote.bid > quote.ask:
                failures.append("CROSSED_QUOTE")
            if quote.ask <= Decimal("5"):
                failures.append("PRICE_NOT_STRICTLY_ABOVE_5")
            if quote.halted:
                failures.append("MARKET_HALTED")
            if quote.ask > structure.entry_limit:
                failures.append("ACCEPTABLE_EXTENSION_FAILED")
            spread = quote.spread_bps
            max_spread = self.policy.config["evidence"].get("max_spread_bps")
            if max_spread is None:
                failures.append("NUMERIC_SPREAD_GATE_UNRESOLVED")
            elif spread > decimal_value(max_spread, "max_spread_bps"):
                failures.append("SPREAD_TOO_WIDE")

        risk = structure.entry_limit - structure.structural_stop
        reward = (
            min(structure.targets) - structure.entry_limit
            if structure.targets
            else Decimal("-1")
        )
        if risk <= 0 or reward < risk * Decimal("2"):
            failures.append("FAVORABLE_REWARD_RISK_FAILED")

        setup_components = self._setup_components(
            structure=structure,
            latest_bar=latest_bar,
            session_volume=session_volume,
        )
        execution_components = self._execution_components(
            quote=quote,
            latest_bar=latest_bar,
        )
        setup = score_setup(setup_components)
        execution = score_execution(execution_components, instrument_kind="equity")
        if self.thresholds.score_policy == "threshold_gated":
            assert self.thresholds.minimum_setup_score is not None
            assert self.thresholds.minimum_execution_score is not None
            if setup.score < self.thresholds.minimum_setup_score:
                failures.append("SETUP_SCORE_BELOW_MINIMUM")
            if execution.score < self.thresholds.minimum_execution_score:
                failures.append("EXECUTION_SCORE_BELOW_MINIMUM")
        deferred = [
            "CURRENT_SESSION_ENTRY_ELIGIBILITY_NOT_EVALUATED",
            "ACCOUNT_CAPACITY_NOT_EVALUATED",
            "ORDER_SIZED_DISPLAYED_DEPTH_NOT_EVALUATED",
            "FRESH_BROKER_SNAPSHOT_NOT_EVALUATED",
        ]
        if self.policy.config["evidence"].get("minimum_depth_multiple") is None:
            failures.append("NUMERIC_DEPTH_GATE_UNRESOLVED")
        return PremarketCandidateFact(
            rank=0,
            symbol=structure.symbol,
            source_plan_id=structure.source_plan_id,
            source_observed_at=_aware_utc(
                structure.observed_at, "structure observed_at"
            ),
            shadow_ranking_score=structure.ranking_score,
            setup_score=float(setup.score),
            execution_score=float(execution.score),
            instrument_evidence_id=(instrument.evidence_id if instrument else None),
            instrument_id=(instrument.instrument_id if instrument else None),
            instrument_source=(instrument.source if instrument else None),
            instrument_observed_at=(instrument.observed_at if instrument else None),
            regular_session_eligibility_at=(
                instrument.eligibility_at if instrument else None
            ),
            quote_bid=(quote.bid if quote else None),
            quote_ask=(quote.ask if quote else None),
            quote_bid_size=(quote.bid_size if quote else None),
            quote_ask_size=(quote.ask_size if quote else None),
            quote_observed_at=(quote.observed_at if quote else None),
            spread_bps=spread,
            latest_completed_bar_end=(latest_bar.end_at if latest_bar else None),
            session_volume=session_volume,
            structural_invalidation=structure.structural_stop,
            targets=structure.targets,
            hard_gate_failures=_unique(failures),
            deferred_execution_gates=tuple(deferred),
        )

    def _setup_components(
        self,
        *,
        structure: PreparedStructure,
        latest_bar: CompletedBar | None,
        session_volume: int,
    ) -> Mapping[str, float]:
        minimum_volume = int(
            self.policy.config["scope"]["minimum_session_volume_inclusive"]
        )
        geometry = bool(
            latest_bar is not None
            and structure.structural_stop < structure.entry_limit
            and all(target > structure.entry_limit for target in structure.targets)
        )
        return {
            "liquidity": min(
                100.0,
                (float(session_volume) / max(1.0, float(minimum_volume))) * 100.0,
            ),
            "relative_volume": self._explicit_score(
                structure.payload, "relative_volume_score"
            ),
            "technical_structure_vwap": 100.0 if geometry else 0.0,
            "catalyst_context": self._explicit_score(
                structure.payload, "catalyst_context_score"
            ),
            "sector_market_sympathy": self._explicit_score(
                structure.payload, "sector_market_sympathy_score"
            ),
            "prior_90_day_behavior": self._explicit_score(
                structure.payload, "prior_90_day_behavior_score"
            ),
            "gap_behavior": self._explicit_score(
                structure.payload, "gap_behavior_score"
            ),
            "other_massive_data": 100.0 if latest_bar is not None else 0.0,
        }

    def _execution_components(
        self,
        *,
        quote: Quote | None,
        latest_bar: CompletedBar | None,
    ) -> Mapping[str, float]:
        max_spread_raw = self.policy.config["evidence"].get("max_spread_bps")
        max_spread = (
            decimal_value(max_spread_raw, "max_spread_bps")
            if max_spread_raw is not None
            else None
        )
        spread_score = 0.0
        if quote is not None and max_spread is not None and max_spread > 0:
            spread_score = max(
                0.0,
                100.0 * (1.0 - float(quote.spread_bps / max_spread)),
            )
        volatility = 0.0
        if latest_bar is not None:
            volatility = max(
                0.0,
                100.0
                * (
                    1.0
                    - float(
                        (latest_bar.high - latest_bar.low)
                        / max(latest_bar.close, Decimal("0.01"))
                    )
                ),
            )
        return {
            "spread": spread_score,
            "displayed_depth": (
                min(100.0, float(min(quote.bid_size, quote.ask_size)))
                if quote is not None
                else 0.0
            ),
            "projected_slippage": spread_score,
            "volatility": volatility,
            "order_size_liquidity": 0.0,
            "halt_risk": 0.0 if quote is None or quote.halted else 100.0,
        }

    @staticmethod
    def _explicit_score(payload: Mapping[str, Any], name: str) -> float:
        raw = payload.get(name)
        if isinstance(raw, bool):
            return 100.0 if raw else 0.0
        if isinstance(raw, (int, float, Decimal)):
            value = float(raw)
            if math.isfinite(value):
                return max(0.0, min(100.0, value))
        return 0.0

    def _session_start(self, now: datetime) -> datetime:
        zone = ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
        local = now.astimezone(zone)
        start = time.fromisoformat(
            str(self.policy.config["sessions"]["premarket_start"])
        )
        return datetime.combine(local.date(), start, zone).astimezone(timezone.utc)

    def _regular_session_open(self, now: datetime) -> datetime:
        zone = ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
        session = self.policy.calendar.session_times(now.astimezone(zone).date())
        if session is None:
            raise ValueError("verified regular session is missing")
        return session.open_at.astimezone(timezone.utc)

    @staticmethod
    def _candidate_payload(value: PremarketCandidateFact) -> Mapping[str, Any]:
        return {
            "rank": value.rank,
            "symbol": value.symbol,
            "source_plan_id": value.source_plan_id,
            "source_observed_at": value.source_observed_at.isoformat(),
            "shadow_ranking_score": str(value.shadow_ranking_score),
            "setup_score": value.setup_score,
            "execution_score": value.execution_score,
            "instrument_evidence_id": value.instrument_evidence_id,
            "instrument_id": value.instrument_id,
            "instrument_source": value.instrument_source,
            "instrument_observed_at": value.instrument_observed_at.isoformat()
            if value.instrument_observed_at is not None
            else None,
            "regular_session_eligibility_at": value.regular_session_eligibility_at.isoformat()
            if value.regular_session_eligibility_at is not None
            else None,
            "quote_bid": str(value.quote_bid) if value.quote_bid is not None else None,
            "quote_ask": str(value.quote_ask) if value.quote_ask is not None else None,
            "quote_bid_size": value.quote_bid_size,
            "quote_ask_size": value.quote_ask_size,
            "quote_observed_at": value.quote_observed_at.isoformat()
            if value.quote_observed_at is not None
            else None,
            "spread_bps": str(value.spread_bps)
            if value.spread_bps is not None
            else None,
            "latest_completed_bar_end": value.latest_completed_bar_end.isoformat()
            if value.latest_completed_bar_end is not None
            else None,
            "session_volume": value.session_volume,
            "structural_invalidation": str(value.structural_invalidation),
            "targets": [str(target) for target in value.targets],
            "hard_gate_failures": list(value.hard_gate_failures),
            "deferred_execution_gates": list(value.deferred_execution_gates),
            "execution_authority": False,
            "approved_to_buy": False,
        }

    @staticmethod
    def _blocked(
        now: datetime,
        schedule: PremarketAnalysisSchedule,
        blockers: Sequence[str],
    ) -> PremarketAnalysisResult:
        return PremarketAnalysisResult(
            status=PremarketAnalysisStatus.BLOCKED,
            schedule=schedule,
            analysis_id=None,
            observed_at=now,
            candidates=(),
            blockers=_unique(tuple(blockers)),
            message=(
                "Premarket analysis was blocked by missing read-only evidence; "
                "no order action is available."
            ),
        )


class FullLiveDiscoveryExecutor:
    """Lifecycle-compatible composition around :class:`FullLiveEntryPipeline`.

    ``ProductionLifecycleActions`` calls a discovery object with only the
    authoritative broker snapshot and current time.  This adapter supplies the
    remaining inputs from the configured read-only Massive source and from the
    durable account-day latch that the service writes earlier in the same
    tick.  It performs a signed-config gate before reading candidates and never
    converts missing provider evidence into authority.
    """

    def __init__(
        self,
        *,
        source: PreparedStructureSource,
        pipeline: FullLiveEntryPipeline,
    ) -> None:
        self.source = source
        self.pipeline = pipeline
        self.policy = pipeline.policy
        self.state = pipeline.state
        self.market_data = pipeline.market_data
        self._tradability = _InstrumentTradabilityView(
            pipeline.instrument_evidence,
            maximum_age_seconds=int(
                self.policy.config["evidence"]["quote_max_age_seconds"]
            ),
        )
        self._premarket_analysis = PremarketAnalysisExecutor(
            source=source,
            policy=self.policy,
            market_data=self.market_data,
            instrument_evidence=pipeline.instrument_evidence,
            tradability=self._tradability,
            thresholds=pipeline.thresholds,
        )

    def premarket_analysis_due(
        self,
        *,
        now: datetime,
        last_completed_slot: datetime | None = None,
    ) -> PremarketAnalysisSchedule:
        """Return the exact caller-owned 30-minute scheduling decision."""

        return self._premarket_analysis.due(
            now=now,
            last_completed_slot=last_completed_slot,
        )

    def analyze(
        self,
        *,
        now: datetime,
        last_completed_slot: datetime | None = None,
    ) -> PremarketAnalysisResult:
        """Run one due premarket analysis slot without any broker mutation API."""

        return self._premarket_analysis.analyze(
            now=now,
            last_completed_slot=last_completed_slot,
        )

    def target_evidence_snapshot(self, symbol: str, *, now: datetime) -> object:
        """Bootstrap and sample held-position evidence without discovery.

        This method is intentionally read-only and is callable while entry
        discovery is blocked.  The first call after a restart may return a
        pending readiness snapshot while the bounded REST backfill runs; the
        continuously owned stream then keeps the symbol available for later
        target-exit ticks.
        """

        current = _aware_utc(now, "target evidence time")
        normalized = str(symbol).strip().upper()
        if not normalized:
            raise ValueError("target evidence symbol is required")
        failures = self.source.ensure_risk_symbols(
            self.market_data,
            symbols=(normalized,),
            session_start=self._session_start(current),
            now=current,
            tradability=self._tradability,
        )
        if failures:
            raise RuntimeError(
                "TARGET_EVIDENCE_HYDRATION_BLOCKED:" + ",".join(
                    _unique(tuple(str(item) for item in failures))
                )
            )
        return self.source.evidence_snapshot(normalized, now=current)

    def final_entry_evidence_failures(
        self, *, plan: object, request: object, now: datetime
    ) -> tuple[str, ...]:
        """Recheck market/tradability after the final account refresh.

        The earlier discovery pass cannot authorize a mutation after a slow
        broker review.  This hook is called by the mutation authority only at
        BEFORE_PLACE and consumes the same release-bound source/provider and
        cached exact plan evidence at a newly sampled time.
        """

        current = _aware_utc(now, "final entry market recheck time")
        if not isinstance(plan, ExpiringPlan):
            return ("FINAL_ENTRY_PLAN_NOT_NORMALIZED",)
        if (
            getattr(request, "symbol", None) != plan.symbol
            or getattr(request, "quantity", None) != plan.quantity
            or getattr(request, "limit_price", None) != plan.entry_limit
        ):
            return ("FINAL_ENTRY_REQUEST_PLAN_MISMATCH",)
        failures: list[str] = []
        try:
            health = self.source.health(now=current)
        except Exception as exc:
            return (f"FINAL_MARKET_HEALTH_FAILED:{type(exc).__name__}",)
        try:
            session = health.session_state
            if not isinstance(session, MarketSessionState):
                session = MarketSessionState(str(session))
        except (AttributeError, ValueError):
            failures.append("FINAL_MARKET_SESSION_STATE_INVALID")
            session = MarketSessionState.WAITING_FOR_SESSION
        if getattr(health, "service_healthy", False) is not True:
            failures.append("FINAL_MARKET_SERVICE_UNHEALTHY")
        if session is not MarketSessionState.ENTRY_ELIGIBLE:
            failures.append("FINAL_MARKET_SESSION_NOT_ENTRY_ELIGIBLE")
        if getattr(health, "entry_evidence_ready", False) is not True:
            failures.append("FINAL_MARKET_EVIDENCE_NOT_READY")
        failures.extend(str(item) for item in getattr(health, "blockers", ()))
        failures.extend(
            str(item) for item in getattr(health, "entry_blockers", ())
        )

        decision = self.pipeline._market_decision(plan, current)
        failures.extend(decision.failures)
        instrument = self.pipeline.instrument_evidence.get_instrument_evidence(
            plan.symbol, now=current
        )
        if instrument is None:
            failures.append("FINAL_BROKER_TRADABILITY_EVIDENCE_MISSING")
        else:
            age = (current - _aware_utc(
                instrument.observed_at, "final instrument observed_at"
            )).total_seconds()
            if (
                instrument.symbol.strip().upper() != plan.symbol
                or instrument.instrument_id != plan.instrument_id
                or not instrument.evidence_id
                or not instrument.source
            ):
                failures.append("FINAL_INSTRUMENT_IDENTITY_MISMATCH")
            if age < -1 or age > int(
                self.policy.config["evidence"]["quote_max_age_seconds"]
            ):
                failures.append("FINAL_INSTRUMENT_EVIDENCE_STALE_OR_FUTURE")
            if (
                instrument.asset_type != "stock"
                or instrument.exchange_listed is not True
                or instrument.broker_tradable is not True
                or instrument.regular_hours_eligible is not True
                or instrument.eligibility_scope != "current_regular_session"
                or instrument.eligibility_at is None
                or abs((current - instrument.eligibility_at).total_seconds())
                > int(self.policy.config["evidence"]["quote_max_age_seconds"])
            ):
                failures.append("FINAL_BROKER_TRADABILITY_DENIED")
        return _unique(failures)

    def execute(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> tuple[str, ...]:
        current = _aware_utc(now, "now")

        # This repeats the service/lifecycle/coordinator interlocks on purpose.
        # Direct use of the adapter with the checked-in blocked config remains
        # non-mutating and does not even spend provider calls on candidates.
        authority_failures = self.pipeline._global_policy_failures(current, snapshot)
        if authority_failures:
            return (self._blocked(authority_failures),)

        latch, latch_failures = self._durable_latch(current)
        if latch is None or latch_failures:
            return (self._blocked(latch_failures or ("SESSION_LATCH_MISSING",)),)

        try:
            health = self.source.health(now=current)
        except Exception as exc:
            return (
                self._blocked((f"MASSIVE_HEALTH_FAILED:{type(exc).__name__}",)),
            )
        service_healthy = bool(
            getattr(health, "service_healthy", not bool(health.blockers))
        )
        if not service_healthy:
            return (self._blocked(tuple(str(item) for item in health.blockers)),)
        session_state = getattr(
            health, "session_state", MarketSessionState.ENTRY_ELIGIBLE
        )
        try:
            if not isinstance(session_state, MarketSessionState):
                session_state = MarketSessionState(str(session_state))
        except ValueError:
            return (self._blocked(("MARKET_SESSION_STATE_INVALID",)),)
        if session_state is MarketSessionState.WAITING_FOR_SESSION:
            return ("DISCOVERY:WAITING_FOR_SESSION",)
        entry_blockers = tuple(
            str(item) for item in getattr(health, "entry_blockers", ())
        )
        # Entry-health failures remain a hard no-entry gate for this cycle,
        # but candidate discovery and read-only cache hydration must still be
        # allowed to bootstrap the stream subscription that can clear them.
        # The gate is applied again below before ``pipeline.run_once``.
        entry_health_failures = (
            tuple(str(item) for item in health.blockers) + entry_blockers
        )
        if (
            getattr(health, "entry_evidence_ready", True) is not True
            and not entry_health_failures
        ):
            entry_health_failures = ("MARKET_ENTRY_EVIDENCE_NOT_READY",)

        try:
            structures = self.source.prepared_structures(
                now=current,
                limit=int(self.policy.config["market_data"]["max_active_candidates"]),
            )
        except Exception as exc:
            return (
                self._blocked((f"MASSIVE_CANDIDATE_READ_FAILED:{type(exc).__name__}",)),
            )
        if not structures:
            if entry_health_failures:
                return (self._blocked(entry_health_failures),)
            return ("DISCOVERY:NO_TRADE",)

        try:
            hydration_failures = self.source.hydrate_cache(
                self.market_data,
                structures=structures,
                session_start=self._session_start(current),
                now=current,
                tradability=self._tradability,
            )
        except Exception as exc:
            return (
                self._blocked((f"MASSIVE_CACHE_HYDRATION_FAILED:{type(exc).__name__}",)),
            )
        if hydration_failures:
            return (
                self._blocked(tuple(str(item) for item in hydration_failures)),
            )
        if entry_health_failures:
            return (self._blocked(entry_health_failures),)

        result = self.pipeline.run_once(
            structures=structures,
            broker_snapshot=snapshot,
            latch=latch,
            now=current,
        )
        selected = result.selected
        if selected is not None and selected.execution is not None:
            suffix = (
                ":" + ",".join(selected.execution.failure_codes)
                if selected.execution.failure_codes
                else ""
            )
            return (
                f"ENTRY:{selected.execution.status.value}:{selected.plan.plan_id}{suffix}",
            )
        if result.status is PipelineStatus.NO_TRADE:
            return ("DISCOVERY:NO_TRADE",)
        failures = _unique(
            tuple(
                failure
                for candidate in result.candidates
                for failure in candidate.failures
            )
        )
        return (self._blocked(failures or ("NO_CANDIDATE_PASSED_ALL_GATES",)),)

    def _durable_latch(
        self, now: datetime
    ) -> tuple[SessionLatch | None, tuple[str, ...]]:
        zone = ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
        trading_date = now.astimezone(zone).date()
        rows = self.state.rows(
            "SELECT * FROM session_latches WHERE account_key=? AND trading_date=?",
            (
                self.policy.account_key,
                trading_date.isoformat(),
            ),
        )
        if len(rows) != 1:
            return None, ("CURRENT_SESSION_LATCH_MISSING_OR_AMBIGUOUS",)
        row = rows[0]
        try:
            updated_at = _aware_utc(
                datetime.fromisoformat(str(row["updated_at"])),
                "session_latch.updated_at",
            )
            age = (now - updated_at).total_seconds()
            if age < -1 or age > int(
                self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
            ):
                return None, ("CURRENT_SESSION_LATCH_STALE_OR_FUTURE",)
            if bool(row["pause_new_entries"]) or bool(row["closeout_started"]):
                return None, ("CURRENT_SESSION_LATCH_BLOCKS_NEW_ENTRIES",)
            crossed_at = (
                _aware_utc(
                    datetime.fromisoformat(str(row["first_objective_crossed_at"])),
                    "session_latch.first_objective_crossed_at",
                )
                if row["first_objective_crossed_at"] is not None
                else None
            )
            return (
                SessionLatch(
                    trading_date=trading_date,
                    loss_lock=bool(row["loss_locked"]),
                    hard_kill=bool(row["hard_kill"]),
                    profit_goal_crossed=bool(row["objective_crossed"]),
                    first_profit_crossed_at=crossed_at,
                    highest_realized_pnl=_decimal_from_cents(
                        row["highest_realized_pnl_cents"]
                    ),
                ),
                (),
            )
        except (TypeError, ValueError) as exc:
            return None, ("CURRENT_SESSION_LATCH_INVALID",)

    def _session_start(self, now: datetime) -> datetime:
        zone = ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
        local = now.astimezone(zone)
        try:
            premarket = time.fromisoformat(
                str(self.policy.config["sessions"]["premarket_start"])
            )
        except ValueError as exc:
            raise ValueError("premarket_start must be HH:MM") from exc
        result = datetime.combine(local.date(), premarket, zone)
        if result >= local:
            raise ValueError("market session start must precede discovery time")
        return result

    @staticmethod
    def _blocked(failures: Sequence[str]) -> str:
        return "DISCOVERY:BLOCKED:" + ",".join(_unique(tuple(failures)))


__all__ = [
    "CandidateResult",
    "FullLiveDiscoveryExecutor",
    "FullLiveEntryPipeline",
    "InstrumentEvidence",
    "InstrumentEvidenceProvider",
    "LiveValidationEvidence",
    "PipelineRunResult",
    "PipelineStatus",
    "PipelineThresholds",
    "PremarketAnalysisExecutor",
    "PremarketAnalysisResult",
    "PremarketAnalysisSchedule",
    "PremarketAnalysisStatus",
    "PremarketCandidateFact",
    "PreparedStructureSource",
    "QualityEvidenceProvider",
    "REQUIRED_HARD_GATE_FACTS",
    "RobinhoodInstrumentEvidenceProvider",
    "RobinhoodInstrumentRecordReader",
    "build_account_risk_snapshot",
]
