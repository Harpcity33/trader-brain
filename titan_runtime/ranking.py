from __future__ import annotations

from typing import Any

from .config import SizingPolicy


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float | None, full_value: float) -> float | None:
    if value is None:
        return None
    return _clamp(float(value) / full_value * 100.0)


def apply_weighted_opportunity_scale(signal: dict[str, Any]) -> dict[str, Any]:
    """Add Trader Brain's weighted opportunity model without granting authority.

    Unknown catalyst, sector, historical, and float/dilution inputs remain unknown
    and contribute zero to the raw score.  They are never silently imputed.
    """
    dollar_volume = float(signal.get("dollar_volume") or 0)
    spread_pct = signal.get("spread_pct")
    relative_volume = signal.get("relative_volume")
    extension_atr = max(float(signal.get("extension_atr") or 0), 0.0)
    state = str(signal.get("state") or "BUILDING")

    liquidity = _clamp(dollar_volume / 25_000_000 * 100)
    if spread_pct is not None:
        lane_limit = 0.9375 if signal.get("lane") == "under5" else 0.35
        liquidity *= _clamp(1.0 - float(spread_pct) / max(lane_limit, 0.01), 0.0, 1.0)
        liquidity = _clamp(liquidity)
    relative = _scale(relative_volume, 5.0)
    remaining_capacity = _clamp((4.0 - extension_atr) / 4.0 * 100)
    technical = {
        "BREAKOUT": 90.0,
        "ACCELERATING": 82.0,
        "BUILDING": 65.0,
        "PARABOLIC": 25.0,
        "EXHAUSTED": 10.0,
        "FADING": 5.0,
    }.get(state, 40.0)
    if signal.get("base_high"):
        technical = min(100.0, technical + 10.0)

    factors: dict[str, float | None] = {
        "catalyst_quality": signal.get("catalyst_quality"),
        "liquidity": round(liquidity, 2),
        "relative_volume": round(relative, 2) if relative is not None else None,
        "sector_breadth": signal.get("sector_breadth"),
        "remaining_move_capacity": round(remaining_capacity, 2),
        "technical_structure": round(technical, 2),
        "historical_follow_through": signal.get("historical_follow_through"),
    }
    weights = {
        "catalyst_quality": 0.25,
        "liquidity": 0.15,
        "relative_volume": 0.10,
        "sector_breadth": 0.10,
        "remaining_move_capacity": 0.20,
        "technical_structure": 0.10,
        "historical_follow_through": 0.10,
    }
    known_weight = sum(weights[key] for key, value in factors.items() if value is not None)
    covered_contribution = sum(
        weights[key] * float(value)
        for key, value in factors.items()
        if value is not None
    )

    extension_risk = _clamp(extension_atr / 4.0 * 100)
    spread_risk = None
    if spread_pct is not None:
        lane_limit = 0.9375 if signal.get("lane") == "under5" else 0.35
        spread_risk = _clamp(float(spread_pct) / lane_limit * 100)
    penalties: dict[str, float | None] = {
        "extension_risk": round(extension_risk, 2),
        "gap_fade_risk": signal.get("gap_fade_risk"),
        "float_dilution_risk": signal.get("float_dilution_risk"),
        "spread_risk": round(spread_risk, 2) if spread_risk is not None else None,
    }
    penalty_weights = {
        "extension_risk": 0.30,
        "gap_fade_risk": 0.25,
        "float_dilution_risk": 0.25,
        "spread_risk": 0.20,
    }
    known_penalty_weight = sum(
        penalty_weights[key] for key, value in penalties.items() if value is not None
    )
    covered_penalty = sum(
        penalty_weights[key] * float(value)
        for key, value in penalties.items()
        if value is not None
    )
    available_score = (
        _clamp(covered_contribution / known_weight) if known_weight else 0.0
    )
    available_penalty = (
        _clamp(covered_penalty / known_penalty_weight)
        if known_penalty_weight
        else 0.0
    )
    final_score = _clamp(available_score - 0.45 * available_penalty)

    price = float(signal.get("price") or 0)
    atr = float(signal.get("short_atr") or 0)
    modeled_capacity_pct = (
        _clamp((4.0 - extension_atr), 0.0, 4.0) * atr / price * 100
        if price > 0 and atr > 0
        else None
    )
    signal.update(
        {
            "weighted_scale_version": "trader_brain_2026-08-22_v2",
            "weighted_factors": factors,
            "weighted_penalties": penalties,
            "weighted_opportunity_score": round(final_score, 2),
            "available_evidence_score": round(available_score, 2),
            "available_risk_score": round(available_penalty, 2),
            "raw_covered_contribution_score": round(covered_contribution, 2),
            "weighted_evidence_coverage_pct": round(known_weight * 100, 1),
            "weighted_risk_coverage_pct": round(known_penalty_weight * 100, 1),
            "missing_weighted_inputs": [key for key, value in factors.items() if value is None]
            + [key for key, value in penalties.items() if value is None],
            "modeled_move_capacity_pct": (
                round(modeled_capacity_pct, 3) if modeled_capacity_pct is not None else None
            ),
            "modeled_gain_disclaimer": (
                "ATR-based remaining-capacity estimate, not a return forecast or trade authority."
            ),
        }
    )
    return signal


def _resolve_sizing_policy(config: Any | None) -> tuple[SizingPolicy, str, str | None]:
    if isinstance(config, SizingPolicy):
        return config, "capital_flexible_live_preparation_2026-08-23_v2", None
    if config is not None and isinstance(getattr(config, "sizing_policy", None), SizingPolicy):
        return (
            config.sizing_policy,
            str(getattr(config, "policy_version", "unversioned")),
            getattr(config, "supersedes_policy_version", None),
        )
    return (
        SizingPolicy.defaults(),
        "capital_flexible_live_preparation_2026-08-23_v2",
        None,
    )


def build_preliminary_trade_plan(
    signal: dict[str, Any], config: Any | None = None
) -> dict[str, Any]:
    sizing, policy_version, supersedes_policy_version = _resolve_sizing_policy(config)
    symbol = str(signal["symbol"])
    direction = str(signal.get("direction") or "UP")
    lane = str(signal.get("lane") or "regular_equity")
    trigger = signal.get("base_high")
    stop = signal.get("invalidation")
    blockers = list(signal.get("entry_rejection_reasons") or [])
    context_missing = sorted(set(signal.get("missing_weighted_inputs") or []))
    context_notes: list[str] = []
    if direction == "DOWN":
        blockers.append("exact long-put contract, chain liquidity, debit and option stop are unresolved")
    if lane == "under5":
        context_notes.append(
            "Fresh catalyst is useful context but is not an unconditional eligibility gate; "
            "adverse filing, dilution, listing, manipulation, promotion, and security-status "
            "checks still apply."
        )
    if not trigger or stop is None:
        blockers.append("controlled base trigger and structural invalidation are not complete")
    if signal.get("exhaustion_lock"):
        blockers.append("exhaustion lock active")
    if not signal.get("quote_fresh"):
        blockers.append("fresh live quote unresolved")
    if not signal.get("preliminary_liquidity_pass"):
        blockers.append("live spread/liquidity gate unresolved")

    risk_per_share = None
    reference_entry_price = None
    t1 = None
    t2 = None
    t3 = None
    quantity_status = "STRUCTURE_REQUIRED"
    if lane == "under5":
        sizing_tier = "under5_broker_resolved_capital"
    else:
        sizing_tier = "broker_resolved_capital"
    if direction == "UP" and trigger and stop is not None:
        reference_entry_price = max(
            float(trigger),
            float(signal.get("limit_ceiling") or trigger),
        )
    if (
        direction == "UP"
        and reference_entry_price is not None
        and reference_entry_price > float(stop)
    ):
        risk_per_share = reference_entry_price - float(stop)
        t1 = reference_entry_price + risk_per_share
        t2 = reference_entry_price + 2 * risk_per_share
        t3 = reference_entry_price + 3 * risk_per_share
        quantity_status = "BROKER_CONFIRMED_LIMITS_REQUIRED"

    status = "PRELIMINARY"
    if blockers:
        status = "WATCH_ONLY"
    if direction == "DOWN":
        status = "UNDERLYING_WATCH_ONLY"
    return {
        "schema_version": 2,
        "policy_version": policy_version,
        "supersedes_policy_version": supersedes_policy_version,
        "sizing_policy_version": sizing.version,
        "weighted_scale_version": signal.get("weighted_scale_version"),
        "symbol": symbol,
        "observed_at": signal["observed_at"],
        "status": status,
        "direction": direction,
        "lane": lane,
        "setup": "controlled_base_breakout" if trigger else "structure_forming",
        "weighted_opportunity_score": signal.get("weighted_opportunity_score"),
        "available_evidence_score": signal.get("available_evidence_score"),
        "weighted_evidence_coverage_pct": signal.get("weighted_evidence_coverage_pct"),
        "modeled_move_capacity_pct": signal.get("modeled_move_capacity_pct"),
        "trigger": trigger,
        "review_limit_ceiling": signal.get("limit_ceiling"),
        "reference_entry_price": (
            round(reference_entry_price, 6)
            if reference_entry_price is not None
            else None
        ),
        "structural_stop": stop,
        "risk_per_share": round(risk_per_share, 6) if risk_per_share is not None else None,
        "t1": round(t1, 6) if t1 is not None else None,
        "t2": round(t2, 6) if t2 is not None else None,
        "t3": round(t3, 6) if t3 is not None else None,
        "targets": [
            {"name": "T1", "r_multiple": 1, "price": round(t1, 6) if t1 is not None else None},
            {"name": "T2", "r_multiple": 2, "price": round(t2, 6) if t2 is not None else None},
            {"name": "T3", "r_multiple": 3, "price": round(t3, 6) if t3 is not None else None},
        ],
        "sizing_tier": sizing_tier,
        "preliminary_quantity_cap": None,
        "preliminary_allocation_cap": None,
        "preliminary_risk_cap": None,
        "quantity_status": quantity_status,
        "position_notional_policy": {
            "fixed_notional_cap_dollars": None,
            "max_unleveraged_buying_power_fraction": (
                sizing.max_unleveraged_buying_power_fraction
            ),
            "single_setup_concentration_allowed": (
                sizing.single_setup_concentration_allowed
            ),
            "full_buying_power_requires_materially_best_available_setup": True,
            "materially_best_condition": (
                "highest-quality current executable opportunity after comparative "
                "ranking, live execution checks, and all risk checks"
            ),
            "concentration_is_permission_not_instruction": True,
            "confirmed_buying_power_source": "broker",
            "requires_fresh_broker_buying_power": True,
            "leverage_allowed": sizing.leverage_allowed,
            "notional_quantity_formula": (
                "floor(fresh_broker_unleveraged_buying_power * "
                "max_unleveraged_buying_power_fraction / reviewed_entry_price)"
            ),
            "reference_only": True,
        },
        "loss_at_stop_policy": {
            "fixed_per_trade_loss_cap_dollars": None,
            "account_day_loss_limit_dollars": sizing.account_day_loss_limit_dollars,
            "available_new_stressed_risk_capacity_source": (
                "exact_geometry_risk_gate_plus_broker_confirmed_reconciled_risk_session"
            ),
            "requires_fresh_reconciled_account_state": True,
            "requires_existing_open_risk_reservation": True,
            "requires_loss_lock_and_profit_floor_checks": True,
            "requires_unleveraged_buying_power_and_gross_exposure_check": True,
            "requires_max_of_stop_or_stress_tail_loss": True,
            "stressed_risk_quantity_formula": (
                "floor(dynamic_new_risk_capacity / max(stop_loss_including_execution, "
                "stress_tail_loss))"
            ),
            "final_quantity_formula": (
                "min(notional_quantity, stressed_risk_quantity, broker_order_limits)"
            ),
            "stop_execution_not_guaranteed": True,
            "reference_only": True,
        },
        "risk_campaign": {
            "fixed_initial_risk_dollars": None,
            "fixed_campaign_risk_cap_dollars": None,
            "account_day_loss_limit_dollars": sizing.account_day_loss_limit_dollars,
            "available_new_stressed_risk_capacity_source": (
                "exact_geometry_risk_gate_plus_broker_confirmed_reconciled_risk_session"
            ),
            "single_setup_may_consume_dynamic_new_stressed_risk_capacity": True,
            "reference_only": True,
        },
        "initial_entry_allocation_policy": {
            "initial_allocation_pct_range": list(
                sizing.initial_allocation_pct_range
            ),
            "full_initial_allocation_allowed": sizing.full_initial_allocation_allowed,
            "initial_entry_requires_profit_funding": False,
            "initial_entry_requires_staging": False,
            "adds_optional": sizing.adds_optional,
            "reference_only": True,
        },
        "core_runner_policy": {
            "at_1r": "no_automatic_trim; protect only at valid higher structure",
            "at_2r_dominant_expanding_trim_pct": [0, 15],
            "at_2r_healthy_opportunity_trim_pct": [25, 33],
            "extended_or_vulnerable_trim_pct": 50,
            "runner_pct": [20, 30],
            "runner_exit": "hard failure, broken continuation, or mandatory closeout",
            "reference_only": True,
        },
        "add_policy": {
            "adds_optional": sizing.adds_optional,
            "risk_constraint_logic": "OR",
            "allowed_when_any": [
                "add is profit-funded after protection",
                "aggregate open loss-at-stop does not increase",
            ],
            "requires_profitable_strengthened_structure": True,
            "may_not_widen_original_catastrophe_stop": True,
            "reference_only": True,
        },
        "blockers": sorted(set(blockers)),
        "context_missing": context_missing,
        "context_notes": context_notes,
        "skip_conditions": [
            "support or structural stop fails",
            "spread/depth deteriorates",
            "price exceeds chase ceiling",
            "volume pace fails",
            "exhaustion cluster or halt appears",
            "verified catalyst is contradicted or invalidated",
            "adverse filing, dilution, manipulation, promotion, listing, security-status, "
            "eligibility, broker, or risk check fails",
        ],
        "trade_authority": False,
        "broker_review_complete": False,
        "protection_confirmed": False,
    }
