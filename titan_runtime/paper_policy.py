from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import floor, isfinite
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PaperPolicy:
    """Versioned controls for the Trader Brain $500 cash-account experiment.

    This policy has no brokerage capability. It validates simulated paper-order
    evidence and keeps settled-cash, notional, downside, and deployment accounting
    separate from Titan live state.
    """

    version: str
    starting_settled_cash_dollars: float
    minimum_daily_entry_notional_dollars: float
    same_day_sale_proceeds_reusable: bool
    fractional_shares_allowed: bool
    leverage_allowed: bool
    default_overnight_hold_allowed: bool
    full_initial_allocation_allowed: bool
    target_stop_defined_risk_dollars: float
    target_risk_is_hard_gate: bool
    max_spread_to_structural_risk: float
    quote_max_age_seconds: float
    chase_ceiling_short_atr: float
    live_broker_authority: bool
    production_change_authority: bool

    @classmethod
    def load(cls, path: str | Path) -> "PaperPolicy":
        raw: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
        account = raw["account_model"]
        risk = raw["risk_model"]
        authority = raw["authority"]
        policy = cls(
            version=str(raw["policy_version"]),
            starting_settled_cash_dollars=float(
                account["starting_settled_cash_dollars"]
            ),
            minimum_daily_entry_notional_dollars=float(
                account["minimum_gross_new_entry_notional_per_trading_day_dollars"]
            ),
            same_day_sale_proceeds_reusable=bool(
                account["same_day_sale_proceeds_reusable"]
            ),
            fractional_shares_allowed=bool(account["fractional_shares_allowed"]),
            leverage_allowed=bool(account["leverage_allowed"]),
            default_overnight_hold_allowed=bool(
                account["default_overnight_hold_allowed"]
            ),
            full_initial_allocation_allowed=bool(
                account["full_initial_allocation_allowed"]
            ),
            target_stop_defined_risk_dollars=float(
                risk["target_total_stop_defined_risk_dollars"]
            ),
            target_risk_is_hard_gate=bool(risk["target_is_hard_gate"]),
            max_spread_to_structural_risk=float(
                risk["spread_to_structural_risk_max"]
            ),
            quote_max_age_seconds=float(risk["quote_max_age_seconds"]),
            chase_ceiling_short_atr=float(risk["chase_ceiling_short_atr"]),
            live_broker_authority=bool(authority["live_broker_authority"]),
            production_change_authority=bool(
                authority["production_change_authority"]
            ),
        )
        if raw.get("mode") != "paper_only":
            raise ValueError("paper policy mode must remain paper_only")
        policy.validate()
        return policy

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("paper policy version cannot be empty")
        exact_positive = {
            "starting_settled_cash_dollars": self.starting_settled_cash_dollars,
            "minimum_daily_entry_notional_dollars": (
                self.minimum_daily_entry_notional_dollars
            ),
            "target_stop_defined_risk_dollars": (
                self.target_stop_defined_risk_dollars
            ),
            "quote_max_age_seconds": self.quote_max_age_seconds,
            "chase_ceiling_short_atr": self.chase_ceiling_short_atr,
        }
        for field, value in exact_positive.items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{field} must be a positive finite number")
        if self.starting_settled_cash_dollars != 500.0:
            raise ValueError("paper starting settled cash must remain exactly $500")
        if self.minimum_daily_entry_notional_dollars != 500.0:
            raise ValueError("paper daily gross new-entry notional must remain $500")
        if self.target_stop_defined_risk_dollars != 35.0:
            raise ValueError("paper stop-defined risk target must remain $35")
        if self.target_risk_is_hard_gate:
            raise ValueError(
                "the $35 paper risk value is a visible target, not an invented hard lock"
            )
        if self.same_day_sale_proceeds_reusable:
            raise ValueError("same-day sale proceeds cannot be reused in the paper cash account")
        if self.leverage_allowed:
            raise ValueError("paper leverage is prohibited")
        if self.default_overnight_hold_allowed:
            raise ValueError("paper positions must default to intraday only")
        if not self.full_initial_allocation_allowed:
            raise ValueError("the paper project permits full initial allocation")
        if self.live_broker_authority or self.production_change_authority:
            raise ValueError("paper policy cannot grant broker or production authority")
        if (
            not isfinite(self.max_spread_to_structural_risk)
            or not 0 < self.max_spread_to_structural_risk <= 1
        ):
            raise ValueError("max spread-to-risk ratio must be in (0, 1]")
        if self.quote_max_age_seconds > 15:
            raise ValueError("paper entry quotes may not be older than 15 seconds")
        if self.chase_ceiling_short_atr > 0.625:
            raise ValueError("paper chase ceiling may not exceed 0.625 short ATR")


@dataclass(frozen=True)
class PaperEntryDecision:
    authorized: bool
    reviewed_notional_dollars: float
    available_settled_cash_dollars: float
    stop_defined_loss_dollars: float
    proposed_risk_dollars: float
    spread_to_structural_risk: float | None
    chase_extension_atr: float | None
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    same_day_sale_proceeds_ignored_dollars: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "reviewed_notional_dollars": self.reviewed_notional_dollars,
            "available_settled_cash_dollars": self.available_settled_cash_dollars,
            "stop_defined_loss_dollars": self.stop_defined_loss_dollars,
            "proposed_risk_dollars": self.proposed_risk_dollars,
            "spread_to_structural_risk": self.spread_to_structural_risk,
            "chase_extension_atr": self.chase_extension_atr,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "same_day_sale_proceeds_ignored_dollars": (
                self.same_day_sale_proceeds_ignored_dollars
            ),
            "trade_authority": "paper_simulation_only",
            "live_broker_authority": False,
        }


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite nonnegative number") from error
    if not isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return result


def _aware_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def evaluate_paper_entry(
    policy: PaperPolicy,
    *,
    settled_cash_dollars: float,
    committed_new_entry_notional_dollars: float,
    same_day_sale_proceeds_dollars: float,
    reviewed_entry_price: float,
    structural_stop_price: float,
    quantity: float,
    trigger_price: float,
    short_atr: float,
    bid: float,
    ask: float,
    quote_age_seconds: float,
    modeled_execution_loss_dollars: float,
    conservative_stress_tail_loss_dollars: float,
    decision_timestamp: str,
    source_timestamp: str,
    paper_submission_recorded: bool,
) -> PaperEntryDecision:
    """Evaluate one simulated paper entry without creating a fill.

    Same-day sale proceeds are deliberately ignored when determining new buying
    capacity. The caller must separately append the decision and, if authorized,
    a paper SUBMITTED record before any simulated fill can be recorded.
    """

    policy.validate()
    blockers: list[str] = []
    warnings: list[str] = []

    settled_cash = _finite_nonnegative(settled_cash_dollars, "settled_cash_dollars")
    committed_notional = _finite_nonnegative(
        committed_new_entry_notional_dollars,
        "committed_new_entry_notional_dollars",
    )
    ignored_sale_proceeds = _finite_nonnegative(
        same_day_sale_proceeds_dollars,
        "same_day_sale_proceeds_dollars",
    )
    entry = _finite_nonnegative(reviewed_entry_price, "reviewed_entry_price")
    stop = _finite_nonnegative(structural_stop_price, "structural_stop_price")
    qty = _finite_nonnegative(quantity, "quantity")
    trigger = _finite_nonnegative(trigger_price, "trigger_price")
    atr = _finite_nonnegative(short_atr, "short_atr")
    bid_value = _finite_nonnegative(bid, "bid")
    ask_value = _finite_nonnegative(ask, "ask")
    quote_age = _finite_nonnegative(quote_age_seconds, "quote_age_seconds")
    execution_loss = _finite_nonnegative(
        modeled_execution_loss_dollars,
        "modeled_execution_loss_dollars",
    )
    stress_loss = _finite_nonnegative(
        conservative_stress_tail_loss_dollars,
        "conservative_stress_tail_loss_dollars",
    )

    decision_time = _aware_timestamp(decision_timestamp, "decision_timestamp")
    source_time = _aware_timestamp(source_timestamp, "source_timestamp")
    if source_time > decision_time:
        blockers.append("source evidence is timestamped after the paper decision")
    if not paper_submission_recorded:
        blockers.append("paper SUBMITTED record must exist before a simulated fill")
    if entry <= 0 or stop <= 0 or qty <= 0 or trigger <= 0 or atr <= 0:
        blockers.append("entry geometry requires positive price, stop, quantity, trigger, and ATR")
    if stop >= entry:
        blockers.append("structural stop must be below reviewed long entry")
    if ask_value < bid_value:
        blockers.append("ask cannot be below bid")
    if ask_value < trigger:
        blockers.append("fresh ask has not crossed the exact trigger")
    if ask_value > entry + 0.000001:
        blockers.append("fresh ask exceeds the reviewed entry ceiling")
    if quote_age > policy.quote_max_age_seconds:
        blockers.append("quote is stale")
    if not policy.fractional_shares_allowed and abs(qty - round(qty)) > 1e-9:
        blockers.append("fractional quantity is not allowed")

    available_cash = max(0.0, settled_cash - committed_notional)
    reviewed_notional = entry * qty
    if reviewed_notional > available_cash + 0.005:
        blockers.append("reviewed notional exceeds remaining settled cash")

    structural_risk_per_share = entry - stop
    spread_to_risk: float | None = None
    chase_extension: float | None = None
    stop_defined_loss = execution_loss
    if structural_risk_per_share > 0:
        spread_to_risk = (ask_value - bid_value) / structural_risk_per_share
        stop_defined_loss += structural_risk_per_share * qty
        if spread_to_risk > policy.max_spread_to_structural_risk + 1e-12:
            blockers.append("quoted spread exceeds the structural-risk ratio limit")
    if atr > 0:
        chase_extension = max(0.0, (ask_value - trigger) / atr)
        if chase_extension > policy.chase_ceiling_short_atr + 1e-12:
            blockers.append("entry exceeds the short-ATR chase ceiling")

    proposed_risk = max(stop_defined_loss, stress_loss)
    if proposed_risk > policy.target_stop_defined_risk_dollars + 0.005:
        warning = (
            "proposed risk exceeds the $35 paper target; this is visible but not "
            "an invented hard lock"
        )
        if policy.target_risk_is_hard_gate:
            blockers.append(warning)
        else:
            warnings.append(warning)
    if ignored_sale_proceeds > 0:
        warnings.append(
            "same-day sale proceeds were excluded from available settled cash"
        )

    return PaperEntryDecision(
        authorized=not blockers,
        reviewed_notional_dollars=round(reviewed_notional, 6),
        available_settled_cash_dollars=round(available_cash, 6),
        stop_defined_loss_dollars=round(stop_defined_loss, 6),
        proposed_risk_dollars=round(proposed_risk, 6),
        spread_to_structural_risk=(
            round(spread_to_risk, 6) if spread_to_risk is not None else None
        ),
        chase_extension_atr=(
            round(chase_extension, 6) if chase_extension is not None else None
        ),
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
        same_day_sale_proceeds_ignored_dollars=round(ignored_sale_proceeds, 6),
    )


def recommended_quantity_for_target_risk(
    policy: PaperPolicy,
    *,
    settled_cash_dollars: float,
    committed_new_entry_notional_dollars: float,
    reviewed_entry_price: float,
    structural_stop_price: float,
    modeled_execution_loss_per_share_dollars: float = 0.0,
    conservative_stress_tail_loss_per_share_dollars: float | None = None,
) -> float:
    """Return a reference quantity bounded by settled cash and the $35 target.

    This is sizing guidance, not a fill or an order. The daily $500 deployment
    requirement is graded separately and may be impossible to satisfy while also
    staying within the risk target for a particular setup.
    """

    policy.validate()
    settled = _finite_nonnegative(settled_cash_dollars, "settled_cash_dollars")
    committed = _finite_nonnegative(
        committed_new_entry_notional_dollars,
        "committed_new_entry_notional_dollars",
    )
    entry = _finite_nonnegative(reviewed_entry_price, "reviewed_entry_price")
    stop = _finite_nonnegative(structural_stop_price, "structural_stop_price")
    execution_per_share = _finite_nonnegative(
        modeled_execution_loss_per_share_dollars,
        "modeled_execution_loss_per_share_dollars",
    )
    stress_per_share = (
        _finite_nonnegative(
            conservative_stress_tail_loss_per_share_dollars,
            "conservative_stress_tail_loss_per_share_dollars",
        )
        if conservative_stress_tail_loss_per_share_dollars is not None
        else None
    )
    if entry <= 0 or stop <= 0 or stop >= entry:
        raise ValueError("reference sizing requires entry > stop > 0")
    available = max(0.0, settled - committed)
    notional_quantity = available / entry
    stop_risk_per_share = entry - stop + execution_per_share
    risk_per_share = max(
        stop_risk_per_share,
        stress_per_share if stress_per_share is not None else 0.0,
    )
    if risk_per_share <= 0:
        raise ValueError("reference risk per share must be positive")
    risk_quantity = policy.target_stop_defined_risk_dollars / risk_per_share
    quantity = min(notional_quantity, risk_quantity)
    if policy.fractional_shares_allowed:
        return max(0.0, round(quantity, 6))
    return float(max(0, floor(quantity)))


def deployment_status(
    policy: PaperPolicy,
    gross_new_entry_notional_dollars: float,
) -> dict[str, Any]:
    deployed = _finite_nonnegative(
        gross_new_entry_notional_dollars,
        "gross_new_entry_notional_dollars",
    )
    passed = deployed + 0.005 >= policy.minimum_daily_entry_notional_dollars
    return {
        "status": "PASS" if passed else "DEPLOYMENT FAILURE",
        "gross_new_entry_notional_dollars": round(deployed, 6),
        "required_gross_new_entry_notional_dollars": (
            policy.minimum_daily_entry_notional_dollars
        ),
        "shortfall_dollars": round(
            max(0.0, policy.minimum_daily_entry_notional_dollars - deployed),
            6,
        ),
        "hindsight_or_fabricated_fill_authorized": False,
    }
