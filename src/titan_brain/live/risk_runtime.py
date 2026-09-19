"""Exact-money, account-wide risk enforcement for the execution writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from .money import decimal_value
from .plans import ExpiringPlan
from .policy import PolicyBundle
from .session_trading_policy import SessionTradingState, evaluate_session_state


ZERO = Decimal("0")
# In addition to one entry, reserve one protection order for every possible
# one-share partial fill and one contingency exit/replacement order. The fixed
# legs are lifecycle shape, not a price or an authority grant.
AUTONOMOUS_ENTRY_FIXED_ORDER_LEGS = 2
OPEN_POSITION_REMAINING_FEE_SCOPE = (
    "all_in_commission_and_regulatory_fees_for_every_remaining_order"
)


def _stop_allocation_facts(
    *,
    quantity: int,
    current_market_value: object,
    stop_allocations: Iterable[tuple[str, int, object]],
) -> tuple[Decimal, tuple[tuple[str, int, Decimal], ...]]:
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("remaining position quantity must be positive whole shares")
    value = decimal_value(current_market_value, "current_market_value")
    if value <= ZERO:
        raise ValueError("remaining position value must be positive")
    allocations = tuple(stop_allocations)
    covered, proceeds, identifiers = 0, ZERO, set()
    normalized: list[tuple[str, int, Decimal]] = []
    for allocation in allocations:
        if not isinstance(allocation, tuple) or len(allocation) != 3:
            raise ValueError("stop allocations must be exact three-field tuples")
        order_id, shares, stop = allocation
        if (
            type(order_id) is not str
            or not order_id
            or order_id != order_id.strip()
            or order_id in identifiers
            or type(shares) is not int
            or shares <= 0
        ):
            raise ValueError("stop allocations must be unique exact positive whole quantities")
        price = decimal_value(stop, "stop_price")
        if price <= ZERO or price * quantity >= value:
            raise ValueError("each working stop must be positive and below current accounting mark")
        identifiers.add(order_id)
        covered += shares
        proceeds += price * shares
        normalized.append((order_id, shares, price))
    if covered != quantity:
        raise ValueError("working stop quantities must cover the position exactly")
    return proceeds, tuple(normalized)


def remaining_position_stop_downside(
    *, quantity: int, current_market_value: object,
    stop_allocations: Iterable[tuple[str, int, object]],
    execution_reserve: object, remaining_fee_reserve: object,
) -> Decimal:
    """Exact remaining-risk arithmetic, not an evidence or authority gate.

    A caller must independently prove the current accounting valuation, exact
    working stop allocations, and all remaining fees against the same complete
    broker observation. The connected TWS diagnostic cannot yet prove a common
    valuation epoch, so the production open-position entry blocker remains.
    Incurred fees already in account equity are not accepted as an input here.
    """
    value = decimal_value(current_market_value, "current_market_value")
    reserve = decimal_value(execution_reserve, "execution_reserve")
    fees = decimal_value(remaining_fee_reserve, "remaining_fee_reserve")
    if reserve <= ZERO or fees <= ZERO:
        raise ValueError("remaining execution and fee reserves must be positive")
    proceeds, _normalized = _stop_allocation_facts(
        quantity=quantity,
        current_market_value=value,
        stop_allocations=stop_allocations,
    )
    return value - proceeds + reserve + fees


def minimum_remaining_position_fee_reserve(
    policy: PolicyBundle, *, working_stop_order_count: int
) -> Decimal:
    """Return the approved floor for still-live stop and contingency orders.

    This is deliberately only a floor.  It is not promoted to an all-in fee
    upper bound; an authenticated open-position evidence source must supply
    that bound separately before current mark-to-stop risk can authorize an
    additional entry.
    """

    if type(working_stop_order_count) is not int or working_stop_order_count <= 0:
        raise ValueError("working stop order count must be a positive integer")
    raw = policy.config["execution"].get(
        "minimum_commission_reserve_per_order_dollars"
    )
    if raw is None:
        if policy.account_day_headroom_risk or policy.session_trading_risk:
            raise ValueError("remaining-position commission reserve is required")
        return ZERO
    per_order = decimal_value(
        raw,
        "minimum_commission_reserve_per_order_dollars",
    )
    if per_order <= ZERO:
        raise ValueError(
            "minimum_commission_reserve_per_order_dollars must be positive"
        )
    # Every currently working stop can still incur its order charge.  One
    # additional contingency exit/replacement remains reserved by policy.
    if policy.session_trading_risk:
        with localcontext() as context:
            context.prec = 80
            return per_order * Decimal(working_stop_order_count + 1)
    return per_order * Decimal(working_stop_order_count + 1)


@dataclass(frozen=True)
class OpenPositionRiskCalculationInputs:
    """Shape-checked inputs for diagnostic remaining-risk arithmetic only.

    These inputs carry no receipt, source-time, account-valuation, or provider
    authenticity claim. The current IBKR diagnostic cannot supply those facts,
    and constructing this type can never authorize another entry.
    """

    quantity: int
    current_market_value: Decimal
    stop_allocations: tuple[tuple[str, int, Decimal], ...]
    execution_reserve: Decimal
    remaining_fee_reserve: Decimal
    remaining_fee_scope: str

    def __post_init__(self) -> None:
        market_value = decimal_value(
            self.current_market_value, "current_market_value"
        )
        execution = decimal_value(self.execution_reserve, "execution_reserve")
        remaining_fees = decimal_value(
            self.remaining_fee_reserve, "remaining_fee_reserve"
        )
        if market_value <= ZERO or execution <= ZERO or remaining_fees <= ZERO:
            raise ValueError("open-position value and remaining reserves must be positive")
        object.__setattr__(self, "current_market_value", market_value)
        object.__setattr__(self, "execution_reserve", execution)
        object.__setattr__(self, "remaining_fee_reserve", remaining_fees)
        if self.remaining_fee_scope != OPEN_POSITION_REMAINING_FEE_SCOPE:
            raise ValueError("remaining-position fee input is not explicitly all-in")
        _proceeds, allocations = _stop_allocation_facts(
            quantity=self.quantity,
            current_market_value=market_value,
            stop_allocations=self.stop_allocations,
        )
        object.__setattr__(self, "stop_allocations", allocations)

    @property
    def planned_downside(self) -> Decimal:
        proceeds = sum(
            (stop * Decimal(shares) for _order_id, shares, stop in self.stop_allocations),
            ZERO,
        )
        return self.current_market_value - proceeds

    def stress_downside(self) -> Decimal:
        return remaining_position_stop_downside(
            quantity=self.quantity,
            current_market_value=self.current_market_value,
            stop_allocations=self.stop_allocations,
            execution_reserve=self.execution_reserve,
            remaining_fee_reserve=self.remaining_fee_reserve,
        )

    def meets_policy_fee_floor(self, policy: PolicyBundle) -> bool:
        """Check only the configured floor, never fee-bound authenticity."""

        return self.remaining_fee_reserve >= minimum_remaining_position_fee_reserve(
            policy, working_stop_order_count=len(self.stop_allocations)
        )

    @property
    def authorizes_entry(self) -> bool:
        """A caller-constructed calculation input is never entry authority."""

        return False


def entry_lifecycle_fee_reserve(
    policy: PolicyBundle, *, quantity: int
) -> Decimal:
    """Return a quantity-aware worst-case commission reserve for one entry.

    Legacy/attended policies may omit the field and retain their historical
    zero-fee risk representation.  A present value is never defaulted or
    coerced: it must be a positive finite amount.  Policy validation separately
    requires it for the unattended supported-production IBKR mode. Worst-case
    order count is ``1 entry + quantity one-share protections + 1 contingency``.
    """

    if type(quantity) is not int or quantity <= 0:
        raise ValueError("entry lifecycle fee quantity must be a positive integer")
    raw = policy.config["execution"].get(
        "minimum_commission_reserve_per_order_dollars"
    )
    if raw is None:
        if policy.account_day_headroom_risk or policy.session_trading_risk:
            raise ValueError("dollar-headroom commission reserve is required")
        return ZERO
    per_order = decimal_value(
        raw,
        "minimum_commission_reserve_per_order_dollars",
    )
    if per_order <= ZERO:
        raise ValueError(
            "minimum_commission_reserve_per_order_dollars must be positive"
        )
    if policy.session_trading_risk:
        with localcontext() as context:
            context.prec = 80
            reserve = per_order * Decimal(quantity + AUTONOMOUS_ENTRY_FIXED_ORDER_LEGS)
    else:
        reserve = per_order * Decimal(quantity + AUTONOMOUS_ENTRY_FIXED_ORDER_LEGS)
    if policy.account_day_headroom_risk or policy.session_trading_risk:
        lifecycle_minimum = decimal_value(
            policy.config["execution"].get("minimum_entry_lifecycle_fee_reserve_dollars"),
            "minimum_entry_lifecycle_fee_reserve_dollars",
        )
        if per_order < Decimal("1") or lifecycle_minimum < Decimal("2"):
            raise ValueError("dollar-headroom commission reserve is below the approved minimum")
        reserve = max(reserve, lifecycle_minimum)
    return reserve


@dataclass(frozen=True)
class RiskExposure:
    reference: str
    category: str
    planned_risk: Decimal
    stress_risk: Decimal
    execution_reserve: Decimal
    fee_reserve: Decimal
    notional: Decimal
    protected: bool

    @classmethod
    def build(
        cls,
        *,
        reference: str,
        category: str,
        planned_risk: object,
        stress_risk: object,
        execution_reserve: object,
        fee_reserve: object,
        notional: object,
        protected: bool,
    ) -> "RiskExposure":
        values = {
            name: decimal_value(value, name)
            for name, value in (
                ("planned_risk", planned_risk),
                ("stress_risk", stress_risk),
                ("execution_reserve", execution_reserve),
                ("fee_reserve", fee_reserve),
                ("notional", notional),
            )
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("risk exposure values cannot be negative")
        if values["stress_risk"] != (
            values["planned_risk"]
            + values["execution_reserve"]
            + values["fee_reserve"]
        ):
            raise ValueError("stress risk must equal planned, execution, and fee risk")
        if not reference or category not in {"open", "pending", "unknown", "manual"}:
            raise ValueError("invalid risk exposure identity")
        return cls(reference=reference, category=category, protected=protected is True, **values)


@dataclass(frozen=True)
class AccountRiskSnapshot:
    account_last4: str
    observed_at: datetime
    usable_equity: Decimal
    unleveraged_buying_power: Decimal
    cash: Decimal
    daily_realized_pnl: Decimal
    weekly_realized_pnl: Decimal
    peak_equity: Decimal
    exposures: tuple[RiskExposure, ...]
    account_active: bool
    restricted: bool
    standard_orders_reconciled: bool
    option_orders_reconciled: bool
    advanced_orders_reconciled: bool
    positions_reconciled: bool
    total_equity: Decimal | None = None
    daily_starting_equity: Decimal | None = None
    daily_external_cash_flow: Decimal | None = None

    @classmethod
    def build(cls, **raw: object) -> "AccountRiskSnapshot":
        observed = raw["observed_at"]
        if not isinstance(observed, datetime) or observed.tzinfo is None:
            raise ValueError("broker risk snapshot time must be timezone-aware")
        exposures = raw.get("exposures", ())
        if not isinstance(exposures, (list, tuple)) or any(
            not isinstance(item, RiskExposure) for item in exposures
        ):
            raise ValueError("exposures must contain RiskExposure records")
        money = {
            name: decimal_value(raw[name], name)
            for name in (
                "usable_equity",
                "unleveraged_buying_power",
                "cash",
                "daily_realized_pnl",
                "weekly_realized_pnl",
                "peak_equity",
            )
        }
        for name in ("usable_equity", "unleveraged_buying_power", "cash"):
            if money[name] < 0:
                raise ValueError(f"{name} cannot be negative")
        if money["peak_equity"] <= 0:
            raise ValueError("peak_equity must be positive")
        if money["peak_equity"] < money["usable_equity"]:
            raise ValueError("peak_equity cannot be below usable_equity")
        daily_fields = {
            name: None if raw.get(name) is None else decimal_value(raw[name], name)
            for name in ("total_equity", "daily_starting_equity", "daily_external_cash_flow")
        }
        if daily_fields["daily_starting_equity"] is not None and daily_fields["daily_starting_equity"] <= ZERO:
            raise ValueError("daily starting equity must be positive")
        if daily_fields["total_equity"] is not None and daily_fields["total_equity"] < ZERO:
            raise ValueError("total equity cannot be negative")
        return cls(
            account_last4=str(raw["account_last4"]),
            observed_at=observed,
            exposures=tuple(exposures),
            account_active=raw.get("account_active") is True,
            restricted=raw.get("restricted") is True,
            standard_orders_reconciled=raw.get("standard_orders_reconciled") is True,
            option_orders_reconciled=raw.get("option_orders_reconciled") is True,
            advanced_orders_reconciled=raw.get("advanced_orders_reconciled") is True,
            positions_reconciled=raw.get("positions_reconciled") is True,
            **daily_fields,
            **money,
        )


def _session_amount(value: object) -> Decimal:
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 30 or value.as_tuple().exponent < -12
            or value.adjusted() > 18 or value < ZERO):
        raise ValueError("SESSION_RISK_AMOUNT_INVALID")
    return value


@dataclass(frozen=True)
class SessionAccountRiskSnapshot:
    """Separate, non-authenticating session-risk calculation inputs.

    No legacy realized/weekly/high-water/NAV-flow evidence is synthesized.
    Exposure amounts must describe remaining downside from the SAME accounting
    bid marks to proven working stops, plus future execution/fee reserves. This
    type checks arithmetic, not those provider facts. The production builder and
    entry authority boundary must independently prove that basis and coverage.
    Existing exposures exclude the exact proposed reservation only if its
    unfilled identity has separately been proven; this evaluator never guesses
    a duplicate from amounts or symbols.
    """

    account_last4: str
    account_binding_sha256: str
    observed_at: datetime
    usable_equity: Decimal
    unleveraged_buying_power: Decimal
    cash: Decimal
    exposures: tuple[RiskExposure, ...]
    account_active: bool
    restricted: bool
    standard_orders_reconciled: bool
    option_orders_reconciled: bool
    advanced_orders_reconciled: bool
    positions_reconciled: bool
    session_state: SessionTradingState

    def __post_init__(self) -> None:
        if (type(self.account_last4) is not str or re.fullmatch(r"[0-9]{4}", self.account_last4) is None
                or type(self.account_binding_sha256) is not str
                or re.fullmatch(r"[a-f0-9]{64}", self.account_binding_sha256) is None
                or type(self.observed_at) is not datetime or self.observed_at.tzinfo is None
                or self.observed_at.utcoffset() is None):
            raise ValueError("SESSION_RISK_IDENTITY_OR_TIME_INVALID")
        if type(self.session_state) is not SessionTradingState:
            raise ValueError("SESSION_RISK_STATE_REQUIRED")
        state = self.session_state
        state.baseline.__post_init__()
        for item in state.incidents:
            item.__post_init__()
        if state.last_measurement is not None:
            state.last_measurement.__post_init__()
        state.__post_init__()
        if state.baseline.account_binding_sha256 != self.account_binding_sha256:
            raise ValueError("SESSION_RISK_ACCOUNT_BINDING_MISMATCH")
        for amount in (self.usable_equity, self.unleveraged_buying_power, self.cash):
            _session_amount(amount)
        if any(type(value) is not bool for value in (
                self.account_active, self.restricted, self.standard_orders_reconciled,
                self.option_orders_reconciled, self.advanced_orders_reconciled, self.positions_reconciled)):
            raise ValueError("SESSION_RISK_STATUS_INVALID")
        if (type(self.exposures) is not tuple or len(self.exposures) > 100_000
                or any(type(item) is not RiskExposure for item in self.exposures)):
            raise ValueError("SESSION_RISK_EXPOSURES_INVALID")
        references = set()
        with localcontext() as context:
            context.prec = 80
            for item in self.exposures:
                if (type(item.reference) is not str or not item.reference or item.reference in references
                        or item.category not in {"open", "pending", "unknown", "manual"}
                        or type(item.protected) is not bool):
                    raise ValueError("SESSION_RISK_EXPOSURE_IDENTITY_INVALID")
                references.add(item.reference)
                for amount in (item.planned_risk, item.stress_risk, item.execution_reserve,
                               item.fee_reserve, item.notional):
                    _session_amount(amount)
                if item.stress_risk != item.planned_risk + item.execution_reserve + item.fee_reserve:
                    raise ValueError("SESSION_RISK_EXPOSURE_SUM_INVALID")

    @classmethod
    def build(cls, **raw: object) -> "SessionAccountRiskSnapshot":
        values = dict(raw)
        for name in ("usable_equity", "unleveraged_buying_power", "cash"):
            values[name] = decimal_value(values[name], name)
        exposures = values.get("exposures", ())
        if type(exposures) not in (tuple, list):
            raise ValueError("SESSION_RISK_EXPOSURES_INVALID")
        values["exposures"] = tuple(exposures)
        return cls(**values)

    @property
    def live_authority(self) -> bool:
        return False


@dataclass(frozen=True)
class SessionLatch:
    trading_date: date
    loss_lock: bool = False
    hard_kill: bool = False
    profit_goal_crossed: bool = False
    first_profit_crossed_at: datetime | None = None
    highest_realized_pnl: Decimal = ZERO


@dataclass(frozen=True)
class RiskDecision:
    """A risk constraint result, never broker/source or execution authority."""
    allowed: bool
    failures: tuple[str, ...]
    proposal_planned_risk: Decimal
    proposal_stress_risk: Decimal
    proposal_reserve: Decimal
    remaining_daily_headroom: Decimal
    remaining_portfolio_headroom: Decimal
    remaining_stress_headroom: Decimal
    remaining_buying_power: Decimal
    remaining_cash_headroom: Decimal

    @property
    def live_authority(self) -> bool:
        return False


def _cap(policy: PolicyBundle, equity: Decimal, percent_name: str, absolute_name: str) -> Decimal:
    percent = decimal_value(policy.risk_raw[percent_name], percent_name)
    result = equity * percent
    absolute = policy.risk_raw.get("absolute_dollar_ceilings", {}).get(absolute_name)
    if absolute is not None:
        result = min(result, decimal_value(absolute, absolute_name))
    return result


def dollar_headroom_capacity(
    policy: PolicyBundle, *, realized_pnl: Decimal, profit_goal_crossed: bool
) -> Decimal:
    """Approved aggregate downside budget before existing exposure is charged."""

    pnl = decimal_value(realized_pnl, "realized_pnl")
    capacity = pnl + decimal_value(
        policy.config["risk"]["daily_realized_loss_lock_dollars"], "daily_lock"
    )
    goal = decimal_value(policy.config["risk"]["profit_goal_dollars"], "profit_goal")
    if profit_goal_crossed or pnl >= goal:
        capacity = min(capacity, pnl - decimal_value(
            policy.config["risk"]["post_goal_floor_dollars"], "profit_floor"
        ))
    return capacity


def daily_starting_equity_performance(
    *, starting_equity: object, total_equity: object, external_cash_flow: object
) -> Decimal:
    """Account-wide net performance from an authenticated fixed day baseline.

    External deposits/withdrawals are excluded, not interpreted as trading
    profits/losses. Callers must authenticate and freeze the baseline and prove
    the cash-flow coverage; absent values never default to zero.
    """

    start = decimal_value(starting_equity, "daily_starting_equity")
    total = decimal_value(total_equity, "total_equity")
    flow = decimal_value(external_cash_flow, "daily_external_cash_flow")
    if start <= ZERO or total < ZERO:
        raise ValueError("positive starting equity and nonnegative total equity required")
    return total - flow - start


def daily_starting_equity_capacity(
    policy: PolicyBundle, *, starting_equity: object, total_equity: object,
    external_cash_flow: object,
) -> Decimal:
    """Fixed daily risk allowance with no profit-funded intraday expansion."""

    pnl = daily_starting_equity_performance(
        starting_equity=starting_equity, total_equity=total_equity,
        external_cash_flow=external_cash_flow,
    )
    start = decimal_value(starting_equity, "daily_starting_equity")
    allowance = start * decimal_value(policy.config["risk"]["daily_loss_fraction"], "daily_loss_fraction")
    return min(allowance, allowance + pnl)


def update_session_latch(
    policy: PolicyBundle,
    prior: SessionLatch,
    *,
    realized_pnl: object,
    usable_equity: object,
    observed_at: datetime,
    daily_starting_equity: object = None,
    daily_external_cash_flow: object = None,
    total_equity: object = None,
) -> SessionLatch:
    if policy.session_trading_risk:
        raise ValueError("SESSION_RISK_REQUIRES_SEPARATE_DURABLE_STATE")
    if observed_at.tzinfo is None or observed_at.date() != prior.trading_date:
        raise ValueError("latch update must use the same trading date and aware time")
    pnl = decimal_value(realized_pnl, "realized_pnl")
    equity = decimal_value(usable_equity, "usable_equity")
    if equity <= 0 and not policy.daily_starting_equity_risk:
        raise ValueError("usable equity must be positive")
    if policy.daily_starting_equity_risk:
        performance = daily_starting_equity_performance(
            starting_equity=daily_starting_equity, total_equity=total_equity,
            external_cash_flow=daily_external_cash_flow,
        )
        start = decimal_value(daily_starting_equity, "daily_starting_equity")
        breached = performance <= -(start * decimal_value(
            policy.config["risk"]["daily_loss_fraction"], "daily_loss_fraction"
        ))
        crossed = prior.profit_goal_crossed or performance >= start * decimal_value(
            policy.config["risk"]["daily_profit_aspiration_fraction"], "daily_profit_aspiration_fraction"
        )
        return SessionLatch(
            trading_date=prior.trading_date,
            loss_lock=prior.loss_lock or breached,
            hard_kill=prior.hard_kill or breached,
            profit_goal_crossed=crossed,
            first_profit_crossed_at=(prior.first_profit_crossed_at or observed_at) if crossed else None,
            # Retain the existing storage column's realized-P&L meaning.
            highest_realized_pnl=max(prior.highest_realized_pnl, pnl),
        )
    absolute_daily = decimal_value(
        policy.config["risk"]["daily_realized_loss_lock_dollars"], "daily_lock"
    )
    daily_cap = absolute_daily if policy.dollar_headroom_risk else min(
        absolute_daily,
        _cap(
            policy,
            equity,
            "daily_new_entry_lock_pct",
            "daily_new_entry_lock_dollars",
        ),
    )
    hard_kill = prior.hard_kill
    if not policy.dollar_headroom_risk:
        hard_cap = _cap(
            policy, equity, "hard_daily_loss_kill_pct", "hard_daily_loss_kill_dollars"
        )
        hard_kill = hard_kill or pnl <= -hard_cap
    goal = decimal_value(policy.config["risk"]["profit_goal_dollars"], "profit_goal")
    crossed = prior.profit_goal_crossed or pnl >= goal
    first = prior.first_profit_crossed_at
    if crossed and first is None:
        first = observed_at
    return SessionLatch(
        trading_date=prior.trading_date,
        loss_lock=prior.loss_lock or pnl <= -daily_cap,
        hard_kill=hard_kill,
        profit_goal_crossed=crossed,
        first_profit_crossed_at=first,
        highest_realized_pnl=max(prior.highest_realized_pnl, pnl),
    )


def _session_risk_rejected(*codes: str) -> RiskDecision:
    return RiskDecision(False, tuple(dict.fromkeys(codes)), *((ZERO,) * 8))


def _evaluate_session_entry(
    *, policy: PolicyBundle, snapshot: SessionAccountRiskSnapshot,
    plan: ExpiringPlan, latch: SessionLatch, now: datetime,
) -> RiskDecision:
    """Approved session budget only; provider authenticity stays external."""
    if type(snapshot) is not SessionAccountRiskSnapshot:
        return _session_risk_rejected("SESSION_RISK_SNAPSHOT_REQUIRED")
    if type(snapshot.session_state) is not SessionTradingState:
        return _session_risk_rejected("SESSION_RISK_STATE_REQUIRED")
    if (type(plan) is not ExpiringPlan or type(latch) is not SessionLatch
            or type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None
            or type(latch.trading_date) is not date
            or any(type(value) is not bool for value in (latch.loss_lock, latch.hard_kill, latch.profit_goal_crossed))):
        return _session_risk_rejected("SESSION_RISK_INPUT_INVALID")
    try:
        snapshot.__post_init__()
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return _session_risk_rejected("SESSION_RISK_SNAPSHOT_INVALID")
    state = snapshot.session_state
    failures = []
    if (snapshot.account_last4 != policy.account_last4 or plan.account_last4 != policy.account_last4
            or snapshot.account_binding_sha256 != policy.config["execution"].get("production_account_binding_fingerprint")):
        failures.append("ACCOUNT_POLICY_MISMATCH")
    if state.baseline.policy_sha256 != policy.risk_hash:
        failures.append("SESSION_RISK_POLICY_BINDING_MISMATCH")
    if (plan.strategy_id != policy.strategy_id or plan.policy_hash != policy.policy_hash
            or plan.config_hash != policy.config_hash):
        failures.append("PLAN_POLICY_BINDING_MISMATCH")
    age = (now - snapshot.observed_at).total_seconds()
    if not 0 <= age <= int(policy.config["evidence"]["broker_snapshot_max_age_seconds"]):
        failures.append("BROKER_RISK_SNAPSHOT_STALE")
    if not snapshot.account_active or snapshot.restricted:
        failures.append("ACCOUNT_INACTIVE_OR_RESTRICTED")
    if not all((snapshot.standard_orders_reconciled, snapshot.option_orders_reconciled,
                snapshot.advanced_orders_reconciled, snapshot.positions_reconciled)):
        failures.append("WHOLE_BROKER_RECONCILIATION_INCOMPLETE")
    if snapshot.usable_equity <= ZERO:
        failures.append("USABLE_EQUITY_NOT_POSITIVE")
    if latch.trading_date != now.astimezone(ZoneInfo("America/New_York")).date():
        failures.append("SESSION_LATCH_DATE_MISMATCH")
    # Preserve historical safety flags without reading/relabeling their old
    # highest_realized_pnl column or introducing the old post-goal floor.
    if latch.loss_lock:
        failures.append("IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK")
    if latch.hard_kill:
        failures.append("HARD_DAILY_LOSS_KILL")
    if any(item.category == "unknown" for item in snapshot.exposures):
        failures.append("UNKNOWN_POSSIBLE_EXPOSURE")
    if any(item.category in {"open", "manual"} and not item.protected for item in snapshot.exposures):
        failures.append("UNPROTECTED_OPEN_EXPOSURE")
    if any(item.category in {"open", "manual"} for item in snapshot.exposures):
        # Generic RiskExposure cannot prove fresh bid-to-stop valuation and all
        # remaining fees. Original entry-to-stop risk is NOT a safe substitute.
        failures.append("SESSION_OPEN_RISK_REVALUATION_REQUIRED")
    if any(item.execution_reserve <= ZERO or item.fee_reserve <= ZERO
           for item in snapshot.exposures if item.category != "unknown"):
        failures.append("EXISTING_EXPOSURE_POSITIVE_RESERVES_REQUIRED")
    with localcontext() as context:
        context.prec = 80
        try:
            if type(plan.quantity) is not int or plan.quantity <= 0:
                raise ValueError("SESSION_RISK_PLAN_QUANTITY_INVALID")
            for amount in (plan.entry_limit, plan.structural_stop, plan.execution_reserve_per_share):
                _session_amount(amount)
            plan.validate(policy, now)
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            return _session_risk_rejected(*failures, "SESSION_RISK_PLAN_INVALID")
        session = evaluate_session_state(state, now=now)
        failures.extend(session.entry_blockers)
        capacity = session.aggregate_headroom_before_exposure_and_reserves
        planned, reserve = plan.planned_risk, plan.execution_reserve
        try:
            fee = entry_lifecycle_fee_reserve(policy, quantity=plan.quantity)
            _session_amount(fee)
        except (ValueError, TypeError, ArithmeticError):
            return _session_risk_rejected(*failures, "SESSION_RISK_FEE_RESERVE_UNAVAILABLE")
        stress = planned + reserve + fee
        existing_stress = sum((item.stress_risk for item in snapshot.exposures), ZERO)
        existing_funds = sum((item.notional + item.fee_reserve for item in snapshot.exposures), ZERO)
        remaining = capacity - existing_stress - stress
        funds = existing_funds + plan.notional + fee
        remaining_buying_power = snapshot.unleveraged_buying_power - funds
        remaining_cash = snapshot.cash - funds
        if planned > capacity:
            failures.append("PLANNED_RISK_CAP_EXCEEDED")
        if stress > capacity:
            failures.append("STRESS_RISK_CAP_EXCEEDED")
        if remaining < ZERO:
            failures.extend(("INSUFFICIENT_DAILY_RISK_HEADROOM", "INSUFFICIENT_PORTFOLIO_RISK_HEADROOM",
                             "INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM"))
        if remaining_buying_power < ZERO or remaining_cash < ZERO:
            failures.append("INSUFFICIENT_UNLEVERAGED_FUNDS")
    return RiskDecision(
        allowed=not failures, failures=tuple(dict.fromkeys(failures)),
        proposal_planned_risk=planned, proposal_stress_risk=stress, proposal_reserve=reserve,
        remaining_daily_headroom=remaining, remaining_portfolio_headroom=remaining,
        remaining_stress_headroom=remaining, remaining_buying_power=remaining_buying_power,
        remaining_cash_headroom=remaining_cash,
    )


def evaluate_entry(
    *,
    policy: PolicyBundle,
    snapshot: AccountRiskSnapshot | SessionAccountRiskSnapshot,
    plan: ExpiringPlan,
    latch: SessionLatch,
    now: datetime,
) -> RiskDecision:
    if policy.session_trading_risk:
        return _evaluate_session_entry(policy=policy, snapshot=snapshot, plan=plan, latch=latch, now=now)
    if isinstance(snapshot, SessionAccountRiskSnapshot):
        raise ValueError("SESSION_RISK_POLICY_MODEL_MISMATCH")
    failures: list[str] = []
    if now.tzinfo is None or snapshot.observed_at.tzinfo is None:
        raise ValueError("risk times must be timezone-aware")
    if snapshot.account_last4 != policy.account_last4 or plan.account_last4 != policy.account_last4:
        failures.append("ACCOUNT_POLICY_MISMATCH")
    if (
        plan.strategy_id != policy.strategy_id
        or plan.policy_hash != policy.policy_hash
        or plan.config_hash != policy.config_hash
    ):
        failures.append("PLAN_POLICY_BINDING_MISMATCH")
    age = (now - snapshot.observed_at).total_seconds()
    if age < 0 or age > int(policy.config["evidence"]["broker_snapshot_max_age_seconds"]):
        failures.append("BROKER_RISK_SNAPSHOT_STALE")
    if not snapshot.account_active or snapshot.restricted:
        failures.append("ACCOUNT_INACTIVE_OR_RESTRICTED")
    if not all(
        (
            snapshot.standard_orders_reconciled,
            snapshot.option_orders_reconciled,
            snapshot.advanced_orders_reconciled,
            snapshot.positions_reconciled,
        )
    ):
        failures.append("WHOLE_BROKER_RECONCILIATION_INCOMPLETE")
    if snapshot.usable_equity <= 0:
        failures.append("USABLE_EQUITY_NOT_POSITIVE")
    if latch.trading_date != now.date():
        failures.append("SESSION_LATCH_DATE_MISMATCH")
    if latch.loss_lock:
        failures.append("IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK")
    if latch.hard_kill:
        failures.append("HARD_DAILY_LOSS_KILL")
    if any(item.category == "unknown" for item in snapshot.exposures):
        failures.append("UNKNOWN_POSSIBLE_EXPOSURE")
    if any(item.category in {"open", "manual"} and not item.protected for item in snapshot.exposures):
        failures.append("UNPROTECTED_OPEN_EXPOSURE")
    if policy.daily_starting_equity_risk and any(
        item.category in {"open", "manual"} for item in snapshot.exposures
    ):
        # The current supported providers do not authenticate a per-position
        # source valuation time/common NLV epoch and all-in remaining-fee bound.
        # A caller-shaped calculation input is not authority to credit open P&L.
        failures.append("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED")

    existing_planned = sum(
        (
            item.planned_risk + item.execution_reserve + item.fee_reserve
            for item in snapshot.exposures
        ),
        ZERO,
    )
    existing_stress = sum((item.stress_risk for item in snapshot.exposures), ZERO)
    existing_notional = sum((item.notional for item in snapshot.exposures), ZERO)
    existing_fees = sum((item.fee_reserve for item in snapshot.exposures), ZERO)
    planned = plan.planned_risk
    reserve = plan.execution_reserve
    fee_reserve = entry_lifecycle_fee_reserve(policy, quantity=plan.quantity)
    stress = plan.stress_risk + fee_reserve
    equity = snapshot.usable_equity
    if policy.daily_starting_equity_risk:
        try:
            aggregate_cap = daily_starting_equity_capacity(
                policy, starting_equity=snapshot.daily_starting_equity,
                total_equity=snapshot.total_equity,
                external_cash_flow=snapshot.daily_external_cash_flow,
            )
            performance = daily_starting_equity_performance(
                starting_equity=snapshot.daily_starting_equity,
                total_equity=snapshot.total_equity,
                external_cash_flow=snapshot.daily_external_cash_flow,
            )
            if performance <= -(snapshot.daily_starting_equity * decimal_value(
                policy.config["risk"]["daily_loss_fraction"], "daily_loss_fraction"
            )):
                failures.append("DAILY_STARTING_EQUITY_LOSS_LIMIT_REACHED")
        except (ValueError, TypeError):
            failures.append("DAILY_STARTING_EQUITY_EVIDENCE_INCOMPLETE")
            aggregate_cap = ZERO
        daily_cap = per_trade = stress_cap = portfolio_cap = portfolio_stress_cap = aggregate_cap
    elif policy.dollar_headroom_risk:
        daily_cap = dollar_headroom_capacity(
            policy, realized_pnl=snapshot.daily_realized_pnl, profit_goal_crossed=False
        )
        aggregate_cap = dollar_headroom_capacity(
            policy, realized_pnl=snapshot.daily_realized_pnl,
            profit_goal_crossed=latch.profit_goal_crossed,
        )
        per_trade = stress_cap = portfolio_cap = portfolio_stress_cap = aggregate_cap
    else:
        quality_prefix = "a_plus" if plan.quality_tier == "a_plus" else "normal"
        per_trade = _cap(
            policy, equity, f"{quality_prefix}_planned_risk_pct",
            f"{quality_prefix}_planned_risk_dollars",
        )
        stress_cap = _cap(
            policy, equity, "max_single_trade_stress_risk_pct",
            "max_single_trade_stress_risk_dollars",
        )
        daily_cap = min(
            decimal_value(policy.config["risk"]["daily_realized_loss_lock_dollars"], "daily_lock"),
            _cap(policy, equity, "daily_new_entry_lock_pct", "daily_new_entry_lock_dollars"),
        )
        portfolio_cap = _cap(
            policy, equity, "max_total_open_planned_risk_pct",
            "max_total_open_planned_risk_dollars",
        )
        portfolio_stress_cap = _cap(
            policy, equity, "max_total_open_stress_risk_pct",
            "max_total_open_stress_risk_dollars",
        )
    if planned > per_trade:
        failures.append("PLANNED_RISK_CAP_EXCEEDED")
    if stress > stress_cap:
        failures.append("STRESS_RISK_CAP_EXCEEDED")
    realized_loss = ZERO if policy.account_day_headroom_risk else max(ZERO, -snapshot.daily_realized_pnl)
    remaining_daily = (
        daily_cap
        - realized_loss
        - existing_planned
        - planned
        - reserve
        - fee_reserve
    )
    remaining_portfolio = (
        portfolio_cap
        - existing_planned
        - planned
        - reserve
        - fee_reserve
    )
    remaining_stress = portfolio_stress_cap - existing_stress - stress
    total_funds_reserved = (
        existing_notional + existing_fees + plan.notional + fee_reserve
    )
    remaining_buying_power = snapshot.unleveraged_buying_power - total_funds_reserved
    remaining_cash = snapshot.cash - total_funds_reserved
    if remaining_daily < 0:
        failures.append("INSUFFICIENT_DAILY_RISK_HEADROOM")
    if remaining_portfolio < 0:
        failures.append("INSUFFICIENT_PORTFOLIO_RISK_HEADROOM")
    if remaining_stress < 0:
        failures.append("INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM")
    if remaining_buying_power < 0 or remaining_cash < 0:
        failures.append("INSUFFICIENT_UNLEVERAGED_FUNDS")
    if not policy.account_day_headroom_risk:
        weekly_cap = _cap(policy, equity, "weekly_loss_lock_pct", "weekly_loss_lock_dollars")
        if snapshot.weekly_realized_pnl <= -weekly_cap:
            failures.append("WEEKLY_LOSS_LOCK")
        drawdown_cap = _cap(
            policy, equity, "live_drawdown_review_pct", "live_drawdown_review_dollars"
        )
        if snapshot.peak_equity - snapshot.usable_equity >= drawdown_cap:
            failures.append("LIVE_DRAWDOWN_REVIEW_REQUIRED")
    if not policy.daily_starting_equity_risk and (latch.profit_goal_crossed or (
        policy.dollar_headroom_risk
        and snapshot.daily_realized_pnl >= decimal_value(
            policy.config["risk"]["profit_goal_dollars"], "profit_goal"
        )
    )):
        floor = decimal_value(policy.config["risk"]["post_goal_floor_dollars"], "profit_floor")
        if (
            snapshot.daily_realized_pnl
            - existing_planned
            - planned
            - reserve
            - fee_reserve
            < floor
        ):
            failures.append("POST_GOAL_125_FLOOR_NOT_PRESERVED")
    return RiskDecision(
        allowed=not failures,
        failures=tuple(dict.fromkeys(failures)),
        proposal_planned_risk=planned,
        proposal_stress_risk=stress,
        proposal_reserve=reserve,
        remaining_daily_headroom=remaining_daily,
        remaining_portfolio_headroom=remaining_portfolio,
        remaining_stress_headroom=remaining_stress,
        remaining_buying_power=remaining_buying_power,
        remaining_cash_headroom=remaining_cash,
    )


__all__ = [
    "AUTONOMOUS_ENTRY_FIXED_ORDER_LEGS",
    "AccountRiskSnapshot",
    "SessionAccountRiskSnapshot",
    "OPEN_POSITION_REMAINING_FEE_SCOPE",
    "OpenPositionRiskCalculationInputs",
    "RiskDecision",
    "RiskExposure",
    "SessionLatch",
    "daily_starting_equity_capacity",
    "daily_starting_equity_performance",
    "entry_lifecycle_fee_reserve",
    "minimum_remaining_position_fee_reserve",
    "remaining_position_stop_downside",
    "evaluate_entry",
    "update_session_latch",
]
