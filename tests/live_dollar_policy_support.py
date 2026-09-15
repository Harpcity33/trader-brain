"""Copy the real public approval inputs into isolated policy test releases."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil

from titan_brain.live.policy import PolicyBundle, sha256_json


def legacy_dollar_policy(source: Path) -> PolicyBundle:
    """Keep historical dollar tests independent of the current IBKR model."""

    current = PolicyBundle.load(source, config_relative="config/full_live_ibkr.json")
    config = copy.deepcopy(current.config)
    config.pop("owner_risk_policy_amendment", None)
    config["risk"] = {
        "model": "account_day_dollar_headroom",
        "limits_path": "config/risk_limits_ibkr_dollar_headroom.json",
        "limits_live_provenance_verified": False,
        "limits_provenance_state": "retained_owner_approved_dollar_headroom_bound_to_2026-09-14_approval",
        "daily_realized_loss_lock_dollars": "100.00",
        "profit_goal_dollars": "150.00",
        "post_goal_floor_dollars": "125.00",
        "positive_execution_reserve_required": True,
        "loss_lock_is_irreversible_for_session": True,
    }
    config["execution"]["ibkr_daily_risk_baseline_schema"] = (
        "titan_ibkr_daily_risk_baseline_2026-09-14_v1"
    )
    with (source / config["risk"]["limits_path"]).open(encoding="utf-8") as handle:
        risk_raw = json.load(handle)
    risk_hash = sha256_json(risk_raw)
    result = replace(
        current, config=config, risk_raw=risk_raw, risk_hash=risk_hash,
        config_hash=sha256_json(config),
        policy_hash=sha256_json({
            "account": config["account"], "scope": config["scope"],
            "sessions": config["sessions"], "risk": config["risk"],
            "risk_hash": risk_hash, "strategy_id": config["strategy_id"],
        }),
    )
    result.validate()
    return result


def copy_dollar_policy_inputs(source: Path, destination: Path) -> None:
    """Copy legacy and current public risk inputs for isolated release tests."""

    for relative in (
        "config/risk_limits_ibkr_dollar_headroom.json",
        "config/risk_limits_ibkr_daily_starting_equity.json",
        "validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md",
        "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md",
        "validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md",
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
