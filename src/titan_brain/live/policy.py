"""Immutable production policy loading and activation gates.

The runtime identity is new, but it remains bound to the established attended
strategy and checked-in risk limits.  Unresolved authority or numeric execution
thresholds are explicit blockers; they are never interpreted as unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from titan_brain.risk import RiskLimits

from .calendar import ExchangeCalendar
from .money import decimal_value, whole_shares


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyBundle:
    root: Path
    config: Mapping[str, Any]
    risk_raw: Mapping[str, Any]
    calendar: ExchangeCalendar
    config_hash: str
    risk_hash: str
    policy_hash: str

    @classmethod
    def load(cls, root: str | Path) -> "PolicyBundle":
        base = Path(root).resolve()
        config_path = base / "config/full_live.json"
        calendar_path = base / "config/nyse_calendar_2026.json"
        with config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("full-live config must be an object")
        relative_risk = Path(str(config["risk"]["limits_path"]))
        if relative_risk.is_absolute() or ".." in relative_risk.parts:
            raise ValueError("risk limits path must stay inside the release")
        with (base / relative_risk).open("r", encoding="utf-8") as handle:
            risk_raw = json.load(handle)
        if not isinstance(risk_raw, dict):
            raise ValueError("risk limits must be an object")
        config_hash = sha256_json(config)
        risk_hash = sha256_json(risk_raw)
        policy_hash = sha256_json(
            {
                "account": config["account"],
                "scope": config["scope"],
                "sessions": config["sessions"],
                "risk": config["risk"],
                "risk_hash": risk_hash,
                "strategy_id": config["strategy_id"],
            }
        )
        bundle = cls(
            root=base,
            config=config,
            risk_raw=risk_raw,
            calendar=ExchangeCalendar.from_json(calendar_path),
            config_hash=config_hash,
            risk_hash=risk_hash,
            policy_hash=policy_hash,
        )
        bundle.validate()
        return bundle

    @property
    def runtime_id(self) -> str:
        return str(self.config["runtime_id"])

    @property
    def strategy_id(self) -> str:
        return str(self.config["strategy_id"])

    @property
    def account_last4(self) -> str:
        return str(self.config["account"]["required_last4"])

    @property
    def risk_limits(self) -> RiskLimits:
        return RiskLimits.from_mapping(self.risk_raw)

    @property
    def activation_blockers(self) -> tuple[str, ...]:
        blockers = list(self.config["authority"].get("blockers", []))
        execution = self.config["execution"]
        evidence = self.config["evidence"]
        if execution.get("supported_unattended_mutation") is not True:
            blockers.append("SUPPORTED_UNATTENDED_MUTATION_NOT_ATTESTED")
        if execution.get("per_mutation_user_confirmation_required") is True:
            blockers.append("PER_MUTATION_CONFIRMATION_STILL_REQUIRED")
        if execution.get("local_mutation_interlock_enabled") is not True:
            blockers.append("LOCAL_MUTATION_INTERLOCK_NOT_ENABLED")
        if evidence.get("max_spread_bps") is None:
            blockers.append("NUMERIC_SPREAD_GATE_UNRESOLVED")
        if evidence.get("minimum_depth_multiple") is None:
            blockers.append("NUMERIC_DEPTH_GATE_UNRESOLVED")
        if self.config["risk"].get("limits_live_provenance_verified") is not True:
            blockers.append("RISK_LIMITS_LIVE_PROVENANCE_NOT_VERIFIED")
        if self.config["notifications"].get("destination_bridge_configured") is not True:
            blockers.append("NOTIFICATION_DESTINATION_BRIDGE_NOT_CONFIGURED")
        discovery = self.config["discovery"]
        if discovery.get("pipeline_configured") is not True:
            blockers.append("LIVE_DISCOVERY_PIPELINE_NOT_CONFIGURED")
        if discovery.get("instrument_evidence_provider") == "unavailable":
            blockers.append("LIVE_INSTRUMENT_EVIDENCE_PROVIDER_UNAVAILABLE")
        if discovery.get("quality_revalidation_provider") == "unavailable":
            blockers.append("LIVE_QUALITY_REVALIDATION_PROVIDER_UNAVAILABLE")
        for field in (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        ):
            if discovery.get(field) is None:
                blockers.append(f"LIVE_SCORE_THRESHOLD_UNRESOLVED:{field}")
        return tuple(dict.fromkeys(str(item) for item in blockers))

    @property
    def live_entries_configured(self) -> bool:
        return self.config["authority"].get("live_entries_enabled") is True

    def validate(self) -> None:
        if self.config.get("schema_version") != "titan_full_live_config_2026-09-08_v1":
            raise ValueError("unsupported full-live configuration schema")
        if self.account_last4 != "7153" or len(self.account_last4) != 4:
            raise ValueError("full-live account binding mismatch")
        account = self.config["account"]
        if account.get("margin_debit_allowed") is not False:
            raise ValueError("margin debit must remain disabled")
        scope = self.config["scope"]
        if scope.get("allowed_instruments") != ["stock"]:
            raise ValueError("only stock is authorized in the full-live equity core")
        mandatory_false = (
            "options_enabled",
            "shorting_enabled",
            "fractional_enabled",
            "averaging_down_enabled",
            "add_enabled",
            "reentry_enabled",
            "stop_widening_enabled",
            "overnight_enabled",
        )
        if any(scope.get(field) is not False for field in mandatory_false):
            raise ValueError("an unauthorized scope expansion is configured")
        if decimal_value(scope["minimum_price_exclusive"], "minimum_price") != Decimal("5.00"):
            raise ValueError("minimum price policy changed")
        if int(scope["minimum_session_volume_inclusive"]) != 750_000:
            raise ValueError("minimum volume policy changed")
        if self.config["sessions"].get("premarket_mode") != "attended_only":
            raise ValueError("premarket cannot be promoted to unattended authority")
        risk = self.config["risk"]
        if decimal_value(risk["daily_realized_loss_lock_dollars"], "daily_lock") != Decimal("100.00"):
            raise ValueError("daily dollar lock changed")
        if decimal_value(risk["profit_goal_dollars"], "profit_goal") != Decimal("150.00"):
            raise ValueError("profit goal changed")
        if decimal_value(risk["post_goal_floor_dollars"], "post_goal_floor") != Decimal("125.00"):
            raise ValueError("post-goal floor changed")
        if not isinstance(risk.get("limits_live_provenance_verified"), bool):
            raise ValueError("risk-policy provenance gate must be boolean")
        notifications = self.config["notifications"]
        if notifications.get("delivery_sink") != "local_jsonl_staging":
            raise ValueError("unreviewed notification sink configured")
        if not isinstance(notifications.get("destination_bridge_configured"), bool):
            raise ValueError("notification destination bridge gate must be boolean")
        if self.config["execution"].get("automatic_retry_unknown_submission") is not False:
            raise ValueError("unknown submissions may never be retried automatically")
        if self.config["execution"].get("one_account_writer_required") is not True:
            raise ValueError("single-writer protection is mandatory")
        if not isinstance(
            self.config["execution"].get("local_mutation_interlock_enabled"), bool
        ):
            raise ValueError("local mutation interlock must be boolean")
        market_data = self.config.get("market_data")
        if not isinstance(market_data, Mapping):
            raise ValueError("full-live market-data configuration is missing")
        if (
            market_data.get("adapter") != "local_titan_massive_sqlite_read_only"
            or market_data.get("producer_book_mode") != "SHADOW"
            or market_data.get("source_is_execution_authority") is not False
        ):
            raise ValueError("Massive compatibility source must remain read-only shadow evidence")
        if int(market_data.get("health_max_age_seconds", 0)) <= 0:
            raise ValueError("Massive health age must be positive")
        if int(market_data.get("candidate_max_age_seconds", 0)) <= 0:
            raise ValueError("Massive candidate age must be positive")
        if int(market_data.get("max_active_candidates", 0)) <= 0:
            raise ValueError("Massive active-set size must be positive")
        discovery = self.config.get("discovery")
        if not isinstance(discovery, Mapping):
            raise ValueError("full-live discovery configuration is missing")
        if discovery.get("adapter") != "local_massive_full_live_pipeline":
            raise ValueError("unreviewed live discovery adapter configured")
        if not isinstance(discovery.get("pipeline_configured"), bool):
            raise ValueError("live discovery configuration gate must be boolean")
        for field in (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        ):
            value = discovery.get(field)
            if value is not None:
                number = float(value)
                if not 0 <= number <= 100:
                    raise ValueError(f"{field} must be in [0, 100]")
        values = {
            field: discovery.get(field)
            for field in (
                "minimum_setup_score",
                "minimum_execution_score",
                "a_plus_setup_score",
                "a_plus_execution_score",
            )
        }
        if all(value is not None for value in values.values()):
            if float(values["a_plus_setup_score"]) < float(
                values["minimum_setup_score"]
            ) or float(values["a_plus_execution_score"]) < float(
                values["minimum_execution_score"]
            ):
                raise ValueError("A+ score thresholds cannot be below live floors")
        # Fully parse the risk policy now so malformed/non-finite limits fail at load.
        self.risk_limits

    def require_account(self, account_number: str, account_type: str) -> None:
        value = str(account_number).strip()
        if not value.endswith(self.account_last4):
            raise ValueError("broker account does not match the policy binding")
        if account_type != str(self.config["account"]["allowed_type"]):
            raise ValueError("broker account type does not match the policy binding")

    def require_entry_tuple(
        self,
        *,
        quantity: int,
        limit_price: Decimal | str | int,
        market_hours: str,
        order_type: str,
        time_in_force: str,
        now: datetime,
    ) -> None:
        whole_shares(quantity)
        price = decimal_value(limit_price, "limit_price")
        if price <= Decimal("5"):
            raise ValueError("entry price must be strictly above $5")
        lane = self.calendar.lane(now)
        if lane == "premarket_attended":
            if market_hours != "extended_hours":
                raise ValueError("premarket entry must use extended_hours")
            raise ValueError("premarket entry is attended-only under the preserved policy")
        if lane != "regular_entry":
            raise ValueError(f"new entries are closed in lane {lane}")
        if (market_hours, order_type, time_in_force) != ("regular_hours", "limit", "gfd"):
            raise ValueError("regular entry must be a regular-hours GFD limit")

    def require_protection_tuple(
        self,
        *,
        quantity: int,
        stop_price: Decimal | str | int,
        original_stop: Decimal | str | int,
        entry_price: Decimal | str | int,
        market_hours: str,
        order_type: str,
        time_in_force: str,
    ) -> None:
        whole_shares(quantity)
        stop = decimal_value(stop_price, "stop_price")
        original = decimal_value(original_stop, "original_stop")
        entry = decimal_value(entry_price, "entry_price")
        if stop <= 0 or stop != original or stop >= entry:
            raise ValueError("protection stop is invalid or widened")
        expected = self.config["execution"]
        if (market_hours, order_type, time_in_force) != (
            expected["protection_market_hours"],
            expected["protection_order_type"],
            expected["protection_time_in_force"],
        ):
            raise ValueError("protection tuple violates the preserved policy")

    def require_activation_ready(self) -> None:
        if not self.live_entries_configured:
            raise ValueError("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
        if self.activation_blockers:
            raise ValueError("ACTIVATION_BLOCKED: " + ",".join(self.activation_blockers))


__all__ = ["PolicyBundle", "canonical_json", "sha256_json"]
