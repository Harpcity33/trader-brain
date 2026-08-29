"""Fail-closed policy primitives for Titan's attended long-options lane.

This module deliberately has no broker-write client.  It can select, score, and
validate an exact reviewed tuple, but only the attended Robinhood workflow may
perform a mutation after its own current review and explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Mapping, Sequence


LEVEL_2 = "option_level_2"
LEVEL_3 = "option_level_3"
APPROVED_LEVELS = frozenset({LEVEL_2, LEVEL_3})
MULTILEG_ACCOUNT_TYPES = frozenset({"margin", "limited_margin"})
ALLOWED_MONEYNESS = frozenset({"ATM", "MODESTLY_ITM"})


class OptionsPolicyError(ValueError):
    """Raised when a candidate or review violates a fail-closed policy gate."""


def decimal(value: Decimal | str | int | float) -> Decimal:
    """Convert broker-style numeric input to a finite Decimal."""

    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OptionsPolicyError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise OptionsPolicyError(f"non-finite decimal value: {value!r}")
    return result


def _nonnegative(name: str, value: Decimal | str | int | float) -> Decimal:
    result = decimal(value)
    if result < 0:
        raise OptionsPolicyError(f"{name} must be nonnegative")
    return result


@dataclass(frozen=True)
class OptionsEligibilitySnapshot:
    """Fresh broker facts required before the options lane may discover risk."""

    account_last4: str
    account_state: str
    account_type: str
    account_accessible: bool
    option_level: str
    current_equity: Decimal
    buying_power: Decimal
    unleveraged_buying_power: Decimal
    equity_positions_reconciled: bool
    equity_orders_reconciled: bool
    option_positions_reconciled: bool
    option_orders_reconciled: bool
    options_buying_power: Decimal | None = None
    multi_leg_eligibility_verified: bool = False

    def __post_init__(self) -> None:
        for name in ("current_equity", "buying_power", "unleveraged_buying_power"):
            object.__setattr__(self, name, _nonnegative(name, getattr(self, name)))
        if self.options_buying_power is not None:
            object.__setattr__(
                self,
                "options_buying_power",
                _nonnegative("options_buying_power", self.options_buying_power),
            )

    @property
    def reconciliation_complete(self) -> bool:
        return all(
            (
                self.equity_positions_reconciled,
                self.equity_orders_reconciled,
                self.option_positions_reconciled,
                self.option_orders_reconciled,
            )
        )

    @property
    def account_gate_passes(self) -> bool:
        return (
            self.account_accessible
            and self.account_state == "active"
            and self.current_equity > 0
            and self.reconciliation_complete
        )

    @property
    def long_calls_puts_enabled(self) -> bool:
        return self.account_gate_passes and self.option_level in APPROVED_LEVELS

    @property
    def debit_spreads_enabled(self) -> bool:
        return (
            self.account_gate_passes
            and self.option_level == LEVEL_3
            and self.account_type in MULTILEG_ACCOUNT_TYPES
            and self.multi_leg_eligibility_verified
        )

    @property
    def dedicated_options_bp_verified(self) -> bool:
        return self.options_buying_power is not None

    @property
    def requires_review_affordability_check(self) -> bool:
        """True when the broker exposes only general/unleveraged buying power."""

        return self.options_buying_power is None


@dataclass(frozen=True)
class OptionContractSnapshot:
    option_id: str
    chain_symbol: str
    option_type: str
    expiration_date: str
    dte: int
    strike: Decimal
    delta: Decimal
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    implied_volatility: Decimal | None
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    open_interest: int
    volume: int
    moneyness: str
    quote_updated_at: datetime
    tradable: bool
    multiplier: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        for name in ("strike", "bid", "ask", "multiplier"):
            object.__setattr__(self, name, _nonnegative(name, getattr(self, name)))
        object.__setattr__(self, "delta", decimal(self.delta))
        for name in ("gamma", "theta", "vega", "implied_volatility"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, decimal(value))
        if self.option_type not in {"call", "put"}:
            raise OptionsPolicyError("option_type must be call or put")
        if self.ask < self.bid:
            raise OptionsPolicyError("ask cannot be below bid")
        if self.quote_updated_at.tzinfo is None:
            raise OptionsPolicyError("quote_updated_at must be timezone-aware")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_dollars(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        mid = self.midpoint
        if mid <= 0:
            return Decimal("Infinity")
        return self.spread_dollars / mid

    def preference_rejections(
        self,
        *,
        holding_profile: str = "intraday",
        min_abs_delta: Decimal = Decimal("0.50"),
        max_abs_delta: Decimal = Decimal("0.70"),
        max_spread_pct: Decimal = Decimal("0.15"),
        minimum_depth: int = 1,
    ) -> tuple[str, ...]:
        rejections: list[str] = []
        if not self.tradable:
            rejections.append("CONTRACT_NOT_TRADABLE")
        if holding_profile == "intraday":
            min_dte, max_dte = 7, 21
        elif holding_profile == "short_swing":
            min_dte, max_dte = 21, 45
        else:
            raise OptionsPolicyError("unknown holding_profile")
        if not min_dte <= self.dte <= max_dte:
            rejections.append("DTE_OUTSIDE_PREFERENCE")
        abs_delta = abs(self.delta)
        if not decimal(min_abs_delta) <= abs_delta <= decimal(max_abs_delta):
            rejections.append("DELTA_OUTSIDE_PREFERENCE")
        if self.moneyness not in ALLOWED_MONEYNESS:
            rejections.append("MONEYNESS_NOT_ALLOWED")
        if self.bid <= 0 or self.ask <= 0:
            rejections.append("NON_EXECUTABLE_BOOK")
        if self.spread_pct > decimal(max_spread_pct):
            rejections.append("OPTION_SPREAD_TOO_WIDE")
        if self.bid_size < minimum_depth or self.ask_size < minimum_depth:
            rejections.append("OPTION_DEPTH_INSUFFICIENT")
        return tuple(rejections)


@dataclass(frozen=True)
class LongOptionRiskDecision:
    allowed: bool
    planned_loss: Decimal
    maximum_premium_loss: Decimal
    planned_risk_pct: Decimal
    stress_risk_pct: Decimal
    remaining_trade_stress_capacity: Decimal
    remaining_portfolio_stress_capacity: Decimal
    reasons: tuple[str, ...]


def full_premium_stress_loss(
    limit_price: Decimal | str | int | float,
    quantity: int,
    multiplier: Decimal | str | int | float = Decimal("100"),
) -> Decimal:
    """Maximum long-option loss: price times quantity times contract multiplier."""

    if quantity <= 0:
        raise OptionsPolicyError("quantity must be positive")
    return _nonnegative("limit_price", limit_price) * quantity * _nonnegative(
        "multiplier", multiplier
    )


def evaluate_long_option_risk(
    *,
    current_equity: Decimal | str | int | float,
    limit_price: Decimal | str | int | float,
    quantity: int,
    tactical_planned_loss: Decimal | str | int | float,
    max_trade_stress_risk_pct: Decimal | str | int | float,
    max_total_open_stress_risk_pct: Decimal | str | int | float,
    existing_open_stress_risk: Decimal | str | int | float = Decimal("0"),
    multiplier: Decimal | str | int | float = Decimal("100"),
) -> LongOptionRiskDecision:
    equity = _nonnegative("current_equity", current_equity)
    if equity <= 0:
        raise OptionsPolicyError("current_equity must be positive")
    planned = _nonnegative("tactical_planned_loss", tactical_planned_loss)
    stress = full_premium_stress_loss(limit_price, quantity, multiplier)
    existing = _nonnegative("existing_open_stress_risk", existing_open_stress_risk)
    trade_limit = equity * _nonnegative(
        "max_trade_stress_risk_pct", max_trade_stress_risk_pct
    )
    portfolio_limit = equity * _nonnegative(
        "max_total_open_stress_risk_pct", max_total_open_stress_risk_pct
    )
    reasons: list[str] = []
    if planned > stress:
        reasons.append("PLANNED_LOSS_EXCEEDS_FULL_PREMIUM")
    if stress > trade_limit:
        reasons.append("FULL_PREMIUM_EXCEEDS_TRADE_STRESS_LIMIT")
    if existing + stress > portfolio_limit:
        reasons.append("FULL_PREMIUM_EXCEEDS_PORTFOLIO_STRESS_CAPACITY")
    return LongOptionRiskDecision(
        allowed=not reasons,
        planned_loss=planned,
        maximum_premium_loss=stress,
        planned_risk_pct=planned / equity,
        stress_risk_pct=stress / equity,
        remaining_trade_stress_capacity=max(Decimal("0"), trade_limit - stress),
        remaining_portfolio_stress_capacity=max(
            Decimal("0"), portfolio_limit - existing - stress
        ),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class EventVolatilityDecision:
    allowed: bool
    expected_underlying_move_pct: Decimal
    required_move_pct: Decimal
    edge_pct: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalLongOptionExecution:
    """Conservative long-option replay fills using the executable book sides."""

    entry_fill: Decimal
    exit_fill: Decimal
    entry_midpoint: Decimal
    exit_midpoint: Decimal
    gross_pnl_dollars: Decimal
    midpoint_bias_dollars: Decimal


def model_historical_long_option_execution(
    *,
    entry_bid: Decimal | str | int | float,
    entry_ask: Decimal | str | int | float,
    exit_bid: Decimal | str | int | float,
    exit_ask: Decimal | str | int | float,
    entry_slippage_per_share: Decimal | str | int | float,
    exit_slippage_per_share: Decimal | str | int | float,
    quantity: int = 1,
    multiplier: Decimal | str | int | float = Decimal("100"),
) -> HistoricalLongOptionExecution:
    """Replay a long contract at ask-plus/slippage in and bid-minus out.

    Midpoints are retained only to quantify optimistic midpoint bias; they are
    never used as the assumed executable fills.
    """

    if quantity <= 0:
        raise OptionsPolicyError("quantity must be positive")
    e_bid = _nonnegative("entry_bid", entry_bid)
    e_ask = _nonnegative("entry_ask", entry_ask)
    x_bid = _nonnegative("exit_bid", exit_bid)
    x_ask = _nonnegative("exit_ask", exit_ask)
    if e_ask < e_bid or x_ask < x_bid:
        raise OptionsPolicyError("ask cannot be below bid")
    entry_slippage = _nonnegative(
        "entry_slippage_per_share", entry_slippage_per_share
    )
    exit_slippage = _nonnegative(
        "exit_slippage_per_share", exit_slippage_per_share
    )
    units = _nonnegative("multiplier", multiplier) * quantity
    if units <= 0:
        raise OptionsPolicyError("multiplier must be positive")
    entry_fill = e_ask + entry_slippage
    exit_fill = max(Decimal("0"), x_bid - exit_slippage)
    entry_mid = (e_bid + e_ask) / Decimal("2")
    exit_mid = (x_bid + x_ask) / Decimal("2")
    executable_pnl = (exit_fill - entry_fill) * units
    midpoint_pnl = (exit_mid - entry_mid) * units
    return HistoricalLongOptionExecution(
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        entry_midpoint=entry_mid,
        exit_midpoint=exit_mid,
        gross_pnl_dollars=executable_pnl,
        midpoint_bias_dollars=midpoint_pnl - executable_pnl,
    )


def evaluate_event_volatility(
    *,
    expected_underlying_move_pct: Decimal | str | int | float | None,
    implied_move_pct: Decimal | str | int | float | None,
    spread_drag_pct: Decimal | str | int | float | None,
    expected_slippage_pct: Decimal | str | int | float | None,
    iv_crush_risk_pct: Decimal | str | int | float | None,
    uncertainty_reserve_pct: Decimal | str | int | float | None,
) -> EventVolatilityDecision:
    fields = {
        "expected_underlying_move_pct": expected_underlying_move_pct,
        "implied_move_pct": implied_move_pct,
        "spread_drag_pct": spread_drag_pct,
        "expected_slippage_pct": expected_slippage_pct,
        "iv_crush_risk_pct": iv_crush_risk_pct,
        "uncertainty_reserve_pct": uncertainty_reserve_pct,
    }
    unavailable = tuple(name.upper() + "_UNAVAILABLE" for name, value in fields.items() if value is None)
    if unavailable:
        return EventVolatilityDecision(
            allowed=False,
            expected_underlying_move_pct=Decimal("0"),
            required_move_pct=Decimal("0"),
            edge_pct=Decimal("0"),
            reasons=unavailable,
        )
    normalized = {name: _nonnegative(name, value) for name, value in fields.items()}
    expected = normalized["expected_underlying_move_pct"]
    required = sum(
        (
            normalized["implied_move_pct"],
            normalized["spread_drag_pct"],
            normalized["expected_slippage_pct"],
            normalized["iv_crush_risk_pct"],
            normalized["uncertainty_reserve_pct"],
        ),
        Decimal("0"),
    )
    allowed = expected > required
    return EventVolatilityDecision(
        allowed=allowed,
        expected_underlying_move_pct=expected,
        required_move_pct=required,
        edge_pct=expected - required,
        reasons=() if allowed else ("EXPECTED_MOVE_DOES_NOT_CLEAR_EVENT_COSTS",),
    )


@dataclass(frozen=True)
class OptionOrderIntent:
    account_last4: str
    chain_symbol: str
    option_id: str
    side: str
    position_effect: str
    quantity: int
    limit_price: Decimal
    time_in_force: str
    market_hours: str
    evidence_revision: str
    quote_updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "limit_price", _nonnegative("limit_price", self.limit_price))
        if self.quantity <= 0:
            raise OptionsPolicyError("quantity must be positive")
        if self.side not in {"buy", "sell"}:
            raise OptionsPolicyError("side must be buy or sell")
        if self.position_effect not in {"open", "close"}:
            raise OptionsPolicyError("position_effect must be open or close")
        if self.time_in_force not in {"gfd", "gtc"}:
            raise OptionsPolicyError("unsupported time_in_force")
        if self.market_hours != "regular_hours":
            raise OptionsPolicyError("initial live options lane is regular_hours only")
        if self.quote_updated_at.tzinfo is None:
            raise OptionsPolicyError("quote_updated_at must be timezone-aware")

    def canonical_tuple(self) -> Mapping[str, Any]:
        return {
            "account_last4": self.account_last4,
            "chain_symbol": self.chain_symbol,
            "option_id": self.option_id,
            "side": self.side,
            "position_effect": self.position_effect,
            "quantity": self.quantity,
            "type": "limit",
            "limit_price": format(self.limit_price, "f"),
            "time_in_force": self.time_in_force,
            "market_hours": self.market_hours,
            "evidence_revision": self.evidence_revision,
            "quote_updated_at": self.quote_updated_at.astimezone(timezone.utc).isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_tuple(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AttendedReviewEnvelope:
    """Locally bound envelope around one Robinhood review response.

    The current connector response has no broker review ID, expiry, or phrase.
    Titan therefore assigns a local reference/TTL/phrase and binds them to the
    reviewed tuple plus quote timestamp. This never weakens the connector's
    requirement to show its full preview and obtain explicit confirmation.
    """

    review_reference: str
    intent_fingerprint: str
    reviewed_at: datetime
    expires_at: datetime
    exact_confirmation_phrase: str
    connector_preview: Mapping[str, Any]
    order_checks: Mapping[str, Any]
    disclosures: Sequence[Mapping[str, Any]]

    def __post_init__(self) -> None:
        if not self.review_reference:
            raise OptionsPolicyError("review reference is required")
        if not self.exact_confirmation_phrase:
            raise OptionsPolicyError("exact confirmation phrase is required")
        if self.reviewed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise OptionsPolicyError("review timestamps must be timezone-aware")
        if self.expires_at <= self.reviewed_at:
            raise OptionsPolicyError("review expiry must follow review time")


def build_local_attended_review(
    *,
    intent: OptionOrderIntent,
    connector_preview: Mapping[str, Any],
    reviewed_at: datetime,
    ttl_seconds: int = 120,
    disclosures: Sequence[Mapping[str, Any]] = (),
) -> AttendedReviewEnvelope:
    """Wrap a displayed connector preview in Titan's exact local confirmation."""

    if reviewed_at.tzinfo is None:
        raise OptionsPolicyError("reviewed_at must be timezone-aware")
    if ttl_seconds <= 0:
        raise OptionsPolicyError("ttl_seconds must be positive")
    checks = connector_preview.get("order_checks", {})
    if not isinstance(checks, Mapping):
        raise OptionsPolicyError("connector order_checks must be an object")
    reference_seed = f"{intent.fingerprint}:{reviewed_at.astimezone(timezone.utc).isoformat()}"
    reference = "optrev-" + hashlib.sha256(reference_seed.encode("utf-8")).hexdigest()[:16]
    phrase = (
        f"CONFIRM OPTION {intent.side.upper()} {intent.chain_symbol} "
        f"{intent.quantity}x {intent.option_id} AT {format(intent.limit_price, 'f')} "
        f"LIMIT {intent.time_in_force.upper()} REGULAR_HOURS REVIEW {reference}"
    )
    return AttendedReviewEnvelope(
        review_reference=reference,
        intent_fingerprint=intent.fingerprint,
        reviewed_at=reviewed_at,
        expires_at=reviewed_at + timedelta(seconds=ttl_seconds),
        exact_confirmation_phrase=phrase,
        connector_preview=dict(connector_preview),
        order_checks=dict(checks),
        disclosures=tuple(disclosures),
    )


def validate_attended_confirmation(
    *,
    intent: OptionOrderIntent,
    review: AttendedReviewEnvelope,
    user_confirmation: str,
    now: datetime,
) -> str:
    """Validate, but never execute, an exact attended confirmation.

    The return value is Titan's local review reference for audit correlation.
    The outer attended connector adapter must still place the exact same tuple;
    no order mutation is implemented here.
    """

    if now.tzinfo is None:
        raise OptionsPolicyError("now must be timezone-aware")
    if now < review.reviewed_at:
        raise OptionsPolicyError("confirmation time precedes review")
    if now >= review.expires_at:
        raise OptionsPolicyError("review expired; obtain a fresh broker review")
    if review.intent_fingerprint != intent.fingerprint:
        raise OptionsPolicyError("review tuple does not match current intent/evidence")
    if user_confirmation != review.exact_confirmation_phrase:
        raise OptionsPolicyError("explicit confirmation phrase does not match exactly")
    return review.review_reference


@dataclass(frozen=True)
class OptionCandidateRecord:
    """Required option-route evidence written to the isolated live-options ledger."""

    setup_id: str
    underlying_setup_score: int
    option_execution_score: int
    contract: OptionContractSnapshot
    underlying_stop: Decimal
    expected_holding_period: str
    expected_underlying_move_pct: Decimal
    expected_option_move_pct: Decimal
    planned_option_loss: Decimal
    maximum_premium_loss: Decimal
    net_expected_r: Decimal

    def __post_init__(self) -> None:
        for name in ("underlying_setup_score", "option_execution_score"):
            value = getattr(self, name)
            if not 0 <= value <= 100:
                raise OptionsPolicyError(f"{name} must be between 0 and 100")
        for name in (
            "underlying_stop",
            "expected_underlying_move_pct",
            "expected_option_move_pct",
            "planned_option_loss",
            "maximum_premium_loss",
        ):
            object.__setattr__(self, name, _nonnegative(name, getattr(self, name)))
        object.__setattr__(self, "net_expected_r", decimal(self.net_expected_r))
        if not self.setup_id:
            raise OptionsPolicyError("setup_id is required")
        if self.maximum_premium_loss < self.planned_option_loss:
            raise OptionsPolicyError("maximum premium loss cannot be below planned loss")


__all__ = [
    "APPROVED_LEVELS",
    "AttendedReviewEnvelope",
    "EventVolatilityDecision",
    "HistoricalLongOptionExecution",
    "LEVEL_2",
    "LEVEL_3",
    "LongOptionRiskDecision",
    "OptionCandidateRecord",
    "OptionContractSnapshot",
    "OptionOrderIntent",
    "OptionsEligibilitySnapshot",
    "OptionsPolicyError",
    "build_local_attended_review",
    "evaluate_event_volatility",
    "evaluate_long_option_risk",
    "full_premium_stress_loss",
    "model_historical_long_option_execution",
    "validate_attended_confirmation",
]
