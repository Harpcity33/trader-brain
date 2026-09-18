"""Immutable production policy loading and activation gates.

The runtime identity is new, but it remains bound to the established attended
strategy and checked-in risk limits.  Unresolved authority or numeric execution
thresholds are explicit blockers; they are never interpreted as unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from titan_brain.risk import RiskLimits

from .calendar import ExchangeCalendar
from .money import decimal_value, whole_shares
from .provider_profile import IbkrLocalProviderProfile, ProviderProfileError
from .session_trading_policy import (
    MODEL as SESSION_TRADING_MODEL,
    OWNER_AMENDMENT_PATH as _SESSION_AMENDMENT_PATH,
    OWNER_AMENDMENT_SHA256 as _SESSION_AMENDMENT_SHA256,
    POLICY_RELATIVE_PATH as _SESSION_LIMITS_PATH,
    load_session_trading_policy,
    load_session_trading_policy_from_root,
)


_IBKR_AUTONOMOUS_AUTHORITY_SCHEMA = (
    "titan_ibkr_autonomous_provider_authority_2026-09-14_v1"
)
_IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA = (
    "titan_ibkr_autonomous_owner_policy_pricing_receipt_2026-09-14_v1"
)
_IBKR_DAILY_RISK_BASELINE_SCHEMA = (
    "titan_ibkr_daily_risk_baseline_2026-09-14_v1"
)
_IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA = (
    "titan_ibkr_daily_starting_equity_risk_baseline_2026-09-14_v1"
)
IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH = Path(
    "state/ibkr-risk-high-water.sqlite3"
)
_NONSECRET_LOCATOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{2,127}\Z")
DOLLAR_HEADROOM_MODEL = "account_day_dollar_headroom"
DAILY_STARTING_EQUITY_MODEL = "account_day_starting_equity_percentage"
_DOLLAR_POLICY_APPROVAL = {
    "proposal_path": "validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md",
    "proposal_sha256": "cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf",
    "approval_record_path": "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md",
    "approval_record_sha256": "d27a1cc0c79629292f1353652440c984d3c7d510bcf3e08c4c595f25da7f4aae",
}
_DOLLAR_RISK_CONTRACT = {
    "schema_version": "titan_account_day_dollar_headroom_2026-09-14_v1",
    "model": DOLLAR_HEADROOM_MODEL,
    "currency": "USD",
    "daily_realized_loss_lock_dollars": "100.00",
    "profit_goal_dollars": "150.00",
    "post_goal_floor_dollars": "125.00",
    "capacity_basis": "broker_confirmed_current_day_realized_pnl",
    "all_open_pending_uncovered_unresolved_downside_required": True,
    "positive_execution_reserve_required": True,
    "commission_reserve_required": True,
    "unleveraged_cash_and_buying_power_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "live_drawdown_review_policy": "unverified_existing_rule_no_percentage_assumed",
    "owner_approval": _DOLLAR_POLICY_APPROVAL,
}
_DAILY_STARTING_EQUITY_AMENDMENT = {
    "amendment_path": "validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md",
    "amendment_sha256": "78571bb3f19157d5f2a8d81976ba7a4a4f0bdc782683b276130b59ca1627c2ad",
}
_DAILY_STARTING_EQUITY_RISK_CONTRACT = {
    "schema_version": "titan_account_day_starting_equity_percentage_2026-09-14_v1",
    "model": DAILY_STARTING_EQUITY_MODEL,
    "currency": "USD",
    "daily_loss_fraction": "0.10",
    "daily_profit_aspiration_fraction": "0.15",
    "post_goal_floor": None,
    "capacity_basis": "authenticated_fixed_account_day_starting_total_equity",
    "daily_start_time": "00:00",
    "daily_start_timezone": "America/New_York",
    "daily_performance_basis": "current_total_equity_minus_external_net_cash_flow_minus_daily_starting_equity",
    "daily_loss_action": "irreversible_entry_lock_and_guarded_closeout",
    "profit_goal_action": "aspirational_only_no_forced_trade_or_profit_floor",
    "maximum_entry_risk_capacity": "min_daily_loss_budget_and_remaining_adjusted_equity_headroom",
    "includes_open_pnl": True,
    "includes_incurred_fees": True,
    "authenticated_daily_starting_equity_required": True,
    "authenticated_daily_external_cash_flow_required": True,
    "intraday_baseline_reset_allowed": False,
    "all_open_pending_uncovered_unresolved_downside_required": True,
    "positive_execution_reserve_required": True,
    "commission_reserve_required": True,
    "unleveraged_cash_and_buying_power_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "owner_approval": _DOLLAR_POLICY_APPROVAL,
    "owner_risk_amendment": _DAILY_STARTING_EQUITY_AMENDMENT,
}
_DAILY_STARTING_EQUITY_CONFIG_CONTRACT = {
    "model": DAILY_STARTING_EQUITY_MODEL,
    "limits_path": "config/risk_limits_ibkr_daily_starting_equity.json",
    "daily_loss_fraction": "0.10",
    "daily_profit_aspiration_fraction": "0.15",
    "post_goal_floor": None,
    "positive_execution_reserve_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "daily_loss_requires_guarded_closeout": True,
    "authenticated_daily_starting_equity_required": True,
    "authenticated_daily_external_cash_flow_required": True,
}
_SESSION_TRADING_AMENDMENT = {
    "amendment_path": _SESSION_AMENDMENT_PATH,
    "amendment_sha256": _SESSION_AMENDMENT_SHA256,
}
_SESSION_TRADING_CONFIG_CONTRACT = {
    "model": SESSION_TRADING_MODEL,
    "limits_path": _SESSION_LIMITS_PATH,
    "daily_loss_fraction": "0.10",
    "daily_profit_aspiration_fraction": "0.15",
    "post_goal_floor": None,
    "positive_execution_reserve_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "daily_loss_requires_guarded_closeout": True,
    "authenticated_session_baseline_required": True,
    "authenticated_session_measurement_required": True,
    "missing_data_incident_policy": "pending_before_read_failed_or_gap_sticky",
}


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
    def load(
        cls,
        root: str | Path,
        *,
        config_relative: str = "config/full_live.json",
    ) -> "PolicyBundle":
        base = Path(root).resolve()
        relative_config = Path(str(config_relative))
        if (
            relative_config.is_absolute()
            or relative_config.parent != Path("config")
            or relative_config.suffix != ".json"
            or ".." in relative_config.parts
        ):
            raise ValueError("full-live config path must be one JSON file in config")
        config_path = base / relative_config
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
    def account_key(self) -> str:
        """Return the signed, non-secret state/control-plane account key.

        Older releases used ``masked_identifier`` for both the display binding
        and the database/lock namespace.  Keep that interpretation when the
        explicit key is absent so an installed ending-7153 release remains
        readable, while allowing a new broker route to use an independently
        configured opaque key.
        """

        account = self.config["account"]
        return str(account.get("account_key", account["masked_identifier"]))

    @property
    def risk_limits(self) -> RiskLimits:
        if self.account_day_headroom_risk or self.session_trading_risk:
            raise ValueError("account-day headroom policy has no legacy percentage limits")
        return RiskLimits.from_mapping(self.risk_raw)

    @property
    def dollar_headroom_risk(self) -> bool:
        return self.config["risk"].get("model") == DOLLAR_HEADROOM_MODEL

    @property
    def daily_starting_equity_risk(self) -> bool:
        return self.config["risk"].get("model") == DAILY_STARTING_EQUITY_MODEL

    @property
    def session_trading_risk(self) -> bool:
        """Recognize the distinct metric; never reinterpret legacy risk fields."""
        return self.config["risk"].get("model") == SESSION_TRADING_MODEL

    @property
    def account_day_headroom_risk(self) -> bool:
        return self.dollar_headroom_risk or self.daily_starting_equity_risk

    @property
    def daily_risk_baseline_schema(self) -> str:
        if self.session_trading_risk:
            raise ValueError("session-trading policy has no legacy daily-risk baseline schema")
        return (
            _IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA
            if self.daily_starting_equity_risk
            else _IBKR_DAILY_RISK_BASELINE_SCHEMA
        )

    def _daily_starting_equity_config_matches(self) -> bool:
        risk = self.config["risk"]
        metadata_fields = {"limits_live_provenance_verified", "limits_provenance_state"}
        return (
            set(risk) == set(_DAILY_STARTING_EQUITY_CONFIG_CONTRACT) | metadata_fields
            and all(
                canonical_json(risk.get(field)) == canonical_json(value)
                for field, value in _DAILY_STARTING_EQUITY_CONFIG_CONTRACT.items()
            )
        )

    def _session_trading_config_matches(self) -> bool:
        risk = self.config["risk"]
        metadata_fields = {"limits_live_provenance_verified", "limits_provenance_state"}
        return (
            set(risk) == set(_SESSION_TRADING_CONFIG_CONTRACT) | metadata_fields
            and all(canonical_json(risk.get(key)) == canonical_json(value)
                    for key, value in _SESSION_TRADING_CONFIG_CONTRACT.items())
        )

    def _session_trading_provenance_verified(self) -> bool:
        """Verify approved policy bytes, not broker facts or runtime readiness."""
        approval = self.config.get("owner_policy_approval")
        amendment = self.config.get("owner_risk_policy_amendment")
        if (not self._session_trading_config_matches()
                or self.account_key != "ibkr-live-ending-3103"
                or self.account_last4 != "3103"
                or self.config["sessions"].get("timezone") != "America/New_York"
                or not isinstance(approval, Mapping)
                or any(approval.get(key) != value for key, value in _DOLLAR_POLICY_APPROVAL.items())
                or not isinstance(amendment, Mapping)
                or canonical_json(amendment) != canonical_json(_SESSION_TRADING_AMENDMENT)):
            return False
        try:
            parsed = load_session_trading_policy(self.risk_raw)
            verified = load_session_trading_policy_from_root(self.root)
            return parsed == verified and self.risk_hash == verified.policy_sha256
        except (OSError, ValueError, TypeError):
            return False

    @property
    def risk_provenance_verified(self) -> bool:
        """Recognize exact approved risk contracts, never an approval boolean.

        The release builder and manifest verifier bind the referenced original
        approval bytes. This check also binds the executable risk semantics to
        those fixed identities. Broker risk evidence and private policy/pricing
        receipts remain independently mandatory.
        """

        if self.session_trading_risk:
            return self._session_trading_provenance_verified()
        if not self.account_day_headroom_risk:
            return self.config["risk"].get("limits_live_provenance_verified") is True
        approval = self.config.get("owner_policy_approval")
        contract = (
            _DAILY_STARTING_EQUITY_RISK_CONTRACT
            if self.daily_starting_equity_risk
            else _DOLLAR_RISK_CONTRACT
        )
        if not (
            canonical_json(self.risk_raw) == canonical_json(contract)
            and isinstance(approval, Mapping)
            and all(approval.get(k) == v for k, v in _DOLLAR_POLICY_APPROVAL.items())
            and self.account_key == "ibkr-live-ending-3103"
        ):
            return False
        if self.daily_starting_equity_risk:
            amendment = self.config.get("owner_risk_policy_amendment")
            if (
                not self._daily_starting_equity_config_matches()
                or not isinstance(amendment, Mapping)
                or any(
                    amendment.get(key) != value
                    for key, value in _DAILY_STARTING_EQUITY_AMENDMENT.items()
                )
            ):
                return False
        try:
            original_verified = all(
                hashlib.sha256(
                    (self.root / _DOLLAR_POLICY_APPROVAL[f"{name}_path"]).read_bytes()
                ).hexdigest() == _DOLLAR_POLICY_APPROVAL[f"{name}_sha256"]
                for name in ("proposal", "approval_record")
            )
            return original_verified and (
                not self.daily_starting_equity_risk
                or hashlib.sha256(
                    (self.root / _DAILY_STARTING_EQUITY_AMENDMENT["amendment_path"]).read_bytes()
                ).hexdigest() == _DAILY_STARTING_EQUITY_AMENDMENT["amendment_sha256"]
            )
        except OSError:
            return False

    @property
    def execution_authority_mode(self) -> str:
        """Return the manifest-bound broker-mutation authority contract.

        Absence is deliberately not interpreted as autonomous authority.  The
        validator rejects the empty value before a policy can be loaded.
        """

        value = self.config["execution"].get("execution_authority_mode")
        return value if isinstance(value, str) else ""

    @property
    def activation_blockers(self) -> tuple[str, ...]:
        blockers = list(self.config["authority"].get("blockers", []))
        if self.session_trading_risk:
            # Recognizing immutable owner policy does not install an evidence
            # verifier, select this model, or make legacy consumers compatible.
            # Replace only alongside a tested, mode-specific runtime composition.
            blockers.append("SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE")
        execution = self.config["execution"]
        evidence = self.config["evidence"]
        if execution.get("broker_adapter") == "supported_production_transport":
            if not str(
                execution.get("production_account_binding_fingerprint", "")
            ).strip():
                blockers.append("BROKER_EXACT_ACCOUNT_BINDING_UNAVAILABLE")
            if not str(
                execution.get("production_authorization_binding_id", "")
            ).strip():
                blockers.append("BROKER_AUTHORIZATION_BINDING_UNAVAILABLE")
        if execution.get("broker_adapter") == "ibkr_local_gateway_staged":
            blockers.append("IBKR_LOCAL_GATEWAY_TRANSPORT_STAGED_ONLY")
        if self.execution_authority_mode == "unattended":
            if execution.get("supported_unattended_mutation") is not True:
                blockers.append("SUPPORTED_UNATTENDED_MUTATION_NOT_ATTESTED")
            if execution.get("per_mutation_user_confirmation_required") is not False:
                blockers.append("PER_MUTATION_CONFIRMATION_STILL_REQUIRED")
        else:
            if execution.get("supported_unattended_mutation") is not False:
                blockers.append("ATTENDED_MODE_EXPOSES_UNATTENDED_MUTATION")
            if execution.get("per_mutation_user_confirmation_required") is not True:
                blockers.append("ATTENDED_CONFIRMATION_NOT_REQUIRED")
        if execution.get("local_mutation_interlock_enabled") is not True:
            blockers.append("LOCAL_MUTATION_INTERLOCK_NOT_ENABLED")
        if evidence.get("max_spread_bps") is None:
            blockers.append("NUMERIC_SPREAD_GATE_UNRESOLVED")
        if evidence.get("minimum_depth_multiple") is None:
            blockers.append("NUMERIC_DEPTH_GATE_UNRESOLVED")
        exits = self.config.get("exits")
        if (
            not isinstance(exits, Mapping)
            or exits.get("target_exit_mode")
            != "first_target_completed_minute_full_exit"
        ):
            blockers.append("AUTONOMOUS_TARGET_EXIT_POLICY_UNRESOLVED")
        if (
            not isinstance(exits, Mapping)
            or exits.get("deadline_feasibility_gate") is not True
        ):
            blockers.append("CLOSEOUT_DEADLINE_FEASIBILITY_GATE_UNAVAILABLE")
        if not self.risk_provenance_verified:
            blockers.append("RISK_LIMITS_LIVE_PROVENANCE_NOT_VERIFIED")
        if self.dollar_headroom_risk:
            blockers.append("LIVE_DRAWDOWN_REVIEW_POLICY_UNVERIFIED")
        if (
            self.config["notifications"].get("destination_bridge_configured") is not True
            or self.config["notifications"].get("delivery_sink") == "local_jsonl_staging"
        ):
            blockers.append("NOTIFICATION_DESTINATION_BRIDGE_NOT_CONFIGURED")
        discovery = self.config["discovery"]
        if (
            self.live_entries_configured
            and discovery.get("provider_composition_id")
            not in {
                "titan.massive_rest_stream.robinhood_instrument.quality.v1",
                "titan.massive_rest_stream.ibkr_contract.quality.v1",
            }
        ):
            blockers.append("SUPPORTED_DISCOVERY_COMPOSITION_NOT_CONFIGURED")
        if discovery.get("pipeline_configured") is not True:
            blockers.append("LIVE_DISCOVERY_PIPELINE_NOT_CONFIGURED")
        if discovery.get("instrument_evidence_provider") == "unavailable":
            blockers.append("LIVE_INSTRUMENT_EVIDENCE_PROVIDER_UNAVAILABLE")
        if discovery.get("quality_revalidation_provider") == "unavailable":
            blockers.append("LIVE_QUALITY_REVALIDATION_PROVIDER_UNAVAILABLE")
        if discovery.get("provider_composition_id") in {
            "titan.massive_rest_stream.robinhood_instrument.quality.v1",
            "titan.massive_rest_stream.ibkr_contract.quality.v1",
        }:
            binding = str(discovery.get("provider_binding_id", ""))
            if len(binding) != 64 or any(
                value not in "0123456789abcdef" for value in binding
            ):
                blockers.append("DISCOVERY_PROVIDER_BINDING_ID_MISSING_OR_INVALID")
        score_fields = (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        )
        score_policy = discovery.get("score_policy", "threshold_gated")
        a_plus_enabled = discovery.get("a_plus_enabled", True)
        if score_policy == "ranking_only":
            if a_plus_enabled is not False:
                blockers.append("RANKING_ONLY_REQUIRES_A_PLUS_DISABLED")
            if any(discovery.get(field) is not None for field in score_fields):
                blockers.append("RANKING_ONLY_CONTAINS_HIDDEN_SCORE_FLOOR")
        elif score_policy == "threshold_gated":
            if a_plus_enabled is not True:
                blockers.append("THRESHOLD_GATED_REQUIRES_A_PLUS_ENABLED")
            for field in score_fields:
                if discovery.get(field) is None:
                    blockers.append(f"LIVE_SCORE_THRESHOLD_UNRESOLVED:{field}")
        else:
            blockers.append("UNSUPPORTED_SCORE_POLICY")
        return tuple(dict.fromkeys(str(item) for item in blockers))

    @property
    def live_entries_configured(self) -> bool:
        return self.config["authority"].get("live_entries_enabled") is True

    def validate(self) -> None:
        if self.config.get("schema_version") != "titan_full_live_config_2026-09-08_v1":
            raise ValueError("unsupported full-live configuration schema")
        account = self.config["account"]
        if not isinstance(account, Mapping):
            raise ValueError("full-live account binding is invalid")
        if not self.account_last4.isascii() or not self.account_last4.isdecimal() or len(self.account_last4) != 4:
            raise ValueError("full-live account last4 is invalid")
        masked_identifier = str(account.get("masked_identifier", ""))
        if masked_identifier != f"ending-{self.account_last4}":
            raise ValueError("full-live masked account binding mismatch")
        account_key = self.account_key
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", account_key):
            raise ValueError("full-live account key is invalid")
        # The signed policy is not a credential store.  An opaque control
        # plane key can distinguish the broker account, but may not contain a
        # full numeric broker identifier.
        if re.search(r"[0-9]{5,}", account_key):
            raise ValueError("full-live account key exposes a broker identifier")
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
        sessions = self.config["sessions"]
        premarket_mode = sessions.get("premarket_mode")
        if premarket_mode not in {"attended_only", "analysis_only"}:
            raise ValueError("premarket cannot be promoted to unattended authority")
        if premarket_mode == "analysis_only":
            if sessions.get("premarket_orders_enabled") is not False:
                raise ValueError("analysis-only premarket cannot permit orders")
            if int(sessions.get("premarket_analysis_interval_minutes", 0)) != 30:
                raise ValueError("premarket analysis interval must remain 30 minutes")
            if sessions.get("regular_entry_start") != "09:35":
                raise ValueError("regular-hours entry start must remain 09:35")
        if sessions.get("regular_entry_cutoff") != "15:30":
            raise ValueError("regular-hours entry cutoff must remain 15:30")
        if int(sessions.get("closeout_start_minutes_before_close", 0)) != 10:
            raise ValueError("closeout must begin at 15:50 on a normal session")
        if int(sessions.get("flat_deadline_minutes_before_close", 0)) != 5:
            raise ValueError("flatness deadline must remain 15:55 on a normal session")
        exits = self.config.get("exits")
        if exits is not None and not isinstance(exits, Mapping):
            raise ValueError("full-live exit policy is invalid")
        if isinstance(exits, Mapping):
            target_exit_mode = exits.get("target_exit_mode")
            if target_exit_mode not in {
                "disabled_pending_owner_approval",
                "first_target_completed_minute_full_exit",
            }:
                raise ValueError("unsupported full-live target exit mode")
            if exits.get("deadline_feasibility_gate") is not True:
                raise ValueError("closeout deadline feasibility gate is mandatory")
            if target_exit_mode == "first_target_completed_minute_full_exit":
                expected_target_exit = {
                    "target_index": 0,
                    "target_trigger": (
                        "fresh_aligned_completed_one_minute_close_at_or_above_target"
                    ),
                    "quantity": "full_broker_confirmed_sellable_position",
                    "cancel_working_sells_before_exit": True,
                    "require_strictly_newer_cancel_evidence": True,
                    "deadline_feasibility_gate": True,
                }
                if any(
                    exits.get(field) != value
                    for field, value in expected_target_exit.items()
                ):
                    raise ValueError(
                        "full-live target exit contract is incomplete or changed"
                    )
        risk = self.config["risk"]
        if self.session_trading_risk:
            if not self._session_trading_config_matches():
                raise ValueError("session-trading risk configuration differs from the approved amendment")
        elif self.daily_starting_equity_risk:
            if not self._daily_starting_equity_config_matches():
                raise ValueError("daily starting-equity risk configuration differs from the approved amendment")
        else:
            if decimal_value(risk["daily_realized_loss_lock_dollars"], "daily_lock") != Decimal("100.00"):
                raise ValueError("daily dollar lock changed")
            if decimal_value(risk["profit_goal_dollars"], "profit_goal") != Decimal("150.00"):
                raise ValueError("profit goal changed")
            if decimal_value(risk["post_goal_floor_dollars"], "post_goal_floor") != Decimal("125.00"):
                raise ValueError("post-goal floor changed")
        if not isinstance(risk.get("limits_live_provenance_verified"), bool):
            raise ValueError("risk-policy provenance gate must be boolean")
        notifications = self.config["notifications"]
        if not isinstance(notifications.get("destination_bridge_configured"), bool):
            raise ValueError("notification destination bridge gate must be boolean")
        notification_sink = notifications.get("delivery_sink")
        if notification_sink == "local_jsonl_staging":
            # Staging remains valid for a paused install but never constitutes
            # a production destination bridge.
            if notifications.get("destination_bridge_configured") is not False:
                raise ValueError("local notification staging is not a destination bridge")
        elif notification_sink == "gmail_api":
            if notifications.get("destination_bridge_configured") is not True:
                raise ValueError("Gmail notification route is not enabled")
            if notifications.get("provider") != "gmail":
                raise ValueError("Gmail notification provider identity is invalid")
            fingerprint = str(notifications.get("destination_fingerprint", ""))
            if len(fingerprint) != 64 or any(item not in "0123456789abcdef" for item in fingerprint):
                raise ValueError("notification destination fingerprint is invalid")
            route_version = str(notifications.get("route_version", ""))
            if not route_version or len(route_version) > 128 or any(
                item.isspace() for item in route_version
            ):
                raise ValueError("notification route version is invalid")
            if notifications.get("required_assurance") not in {
                "PROVIDER_ACCEPTED",
                "OWNER_CONFIRMED",
            }:
                raise ValueError("production notification assurance is invalid")
            if (
                notifications.get("provider_composition_id")
                != "titan.gmail_api.rfc2822.oauth_injected.v1"
            ):
                raise ValueError(
                    "Gmail notification implementation identity is invalid"
                )
            authorization_binding = str(
                notifications.get("authorization_binding_id", "")
            )
            if len(authorization_binding) != 64 or any(
                item not in "0123456789abcdef" for item in authorization_binding
            ):
                raise ValueError(
                    "notification authorization binding identity is invalid"
                )
            timeout = float(notifications.get("timeout_seconds", 0))
            if not 0 < timeout <= 30:
                raise ValueError("notification timeout must be in (0, 30]")
            if any(
                field in notifications
                for field in ("destination", "sender_address", "access_token", "refresh_token")
            ):
                raise ValueError("notification secrets/addresses cannot be stored in policy")
        else:
            raise ValueError("unreviewed notification sink configured")
        execution = self.config["execution"]
        if self.execution_authority_mode not in {"unattended", "attended_only"}:
            raise ValueError("unreviewed execution authority mode configured")
        for field in (
            "supported_unattended_mutation",
            "per_mutation_user_confirmation_required",
            "local_mutation_interlock_enabled",
        ):
            if not isinstance(execution.get(field), bool):
                raise ValueError(f"execution authority gate {field} must be boolean")
        if self.execution_authority_mode == "attended_only":
            if execution.get("supported_unattended_mutation") is not False:
                raise ValueError("attended-only mode cannot attest unattended mutation")
            if execution.get("per_mutation_user_confirmation_required") is not True:
                raise ValueError("attended-only mode requires per-mutation confirmation")
        if execution.get("automatic_retry_unknown_submission") is not False:
            raise ValueError("unknown submissions may never be retried automatically")
        if execution.get("one_account_writer_required") is not True:
            raise ValueError("single-writer protection is mandatory")
        if execution.get("broker_adapter") not in {
            "robinhood_codex_connector",
            "supported_production_transport",
            "ibkr_local_gateway_staged",
        }:
            raise ValueError("unreviewed broker adapter configured")
        if (
            execution.get("broker_adapter") == "ibkr_local_gateway_staged"
            and self.live_entries_configured
        ):
            raise ValueError("staged IBKR transport cannot enable live entries")
        if execution.get("broker_adapter") == "supported_production_transport":
            if not str(execution.get("production_transport_id", "")).strip():
                raise ValueError(
                    "supported production broker requires a signed transport identity"
                )
            for field in (
                "production_account_binding_fingerprint",
                "production_authorization_binding_id",
            ):
                value = str(execution.get(field, ""))
                if len(value) != 64 or any(
                    character not in "0123456789abcdef" for character in value
                ):
                    raise ValueError(
                        f"supported production broker {field} must be a nonsecret 256-bit receipt"
                    )
            if any(
                forbidden in execution
                for forbidden in (
                    "exact_account_id",
                    "broker_account_number",
                    "access_token",
                    "refresh_token",
                    "client_secret",
                    "ibkr_autonomous_authority_key",
                    "ibkr_autonomous_authority_hmac_key",
                    "ibkr_autonomous_policy_receipt_key",
                    "ibkr_autonomous_policy_receipt_hmac_key",
                    "ibkr_daily_risk_baseline_key",
                    "ibkr_daily_risk_baseline_hmac_key",
                    "hmac_key",
                )
            ):
                raise ValueError(
                    "broker identifiers and secrets cannot be stored in signed policy"
                )
        try:
            ibkr_profile = IbkrLocalProviderProfile.from_config(self.config)
        except ProviderProfileError as exc:
            raise ValueError(str(exc)) from exc
        if (
            execution.get("broker_adapter") == "supported_production_transport"
            and ibkr_profile is not None
        ):
            if execution.get("production_transport_id") != "ibkr-tws-api-10.50.2-v1":
                raise ValueError("IBKR supported production transport identity is invalid")
            provider_contract = str(execution.get("ibkr_provider_contract_id", ""))
            if len(provider_contract) != 64 or any(
                character not in "0123456789abcdef"
                for character in provider_contract
            ):
                raise ValueError("IBKR provider contract must be a nonsecret 256-bit receipt")
            ledger_path = Path(str(execution.get("ibkr_ledger_relative_path", "")))
            if (
                ledger_path.is_absolute()
                or not ledger_path.parts
                or ".." in ledger_path.parts
                or ledger_path.suffix != ".sqlite3"
            ):
                raise ValueError("IBKR execution ledger path must stay under the install root")
            try:
                existing_order_reserve = Decimal(
                    str(execution["ibkr_existing_order_reserve_dollars"])
                )
            except (KeyError, ValueError):
                raise ValueError("IBKR existing-order reserve must be positive") from None
            if (
                not existing_order_reserve.is_finite()
                or existing_order_reserve <= 0
            ):
                raise ValueError("IBKR existing-order reserve must be positive")
            if execution.get("local_mutation_interlock_enabled") is not True:
                raise ValueError("IBKR supported transport requires the local mutation interlock")
            if not self.risk_provenance_verified:
                raise ValueError("IBKR supported transport requires verified risk provenance")
            if execution.get("durable_intent_before_submit") is not True:
                raise ValueError(
                    "IBKR supported transport requires a durable intent before submit"
                )
            if self.execution_authority_mode == "unattended":
                if (
                    not isinstance(exits, Mapping)
                    or exits.get("target_exit_mode")
                    != "first_target_completed_minute_full_exit"
                ):
                    raise ValueError(
                        "IBKR unattended transport requires an approved target exit policy"
                    )
                try:
                    minimum_commission_reserve = Decimal(
                        str(
                            execution[
                                "minimum_commission_reserve_per_order_dollars"
                            ]
                        )
                    )
                except (InvalidOperation, KeyError, ValueError):
                    raise ValueError(
                        "IBKR unattended minimum commission reserve must be positive"
                    ) from None
                if (
                    not minimum_commission_reserve.is_finite()
                    or minimum_commission_reserve <= 0
                ):
                    raise ValueError(
                        "IBKR unattended minimum commission reserve must be positive"
                    )
                if execution.get("supported_unattended_mutation") is not True:
                    raise ValueError(
                        "IBKR unattended transport requires supported unattended mutation"
                    )
                if execution.get("per_mutation_user_confirmation_required") is not False:
                    raise ValueError(
                        "IBKR unattended transport cannot require per-mutation confirmation"
                    )
                if (
                    execution.get("ibkr_autonomous_authority_schema")
                    != _IBKR_AUTONOMOUS_AUTHORITY_SCHEMA
                ):
                    raise ValueError(
                        "IBKR unattended authority schema is missing or invalid"
                    )
                authority_relative_raw = execution.get(
                    "ibkr_autonomous_authority_relative_path"
                )
                if type(authority_relative_raw) is not str:
                    raise ValueError(
                        "IBKR unattended authority path must be a relative JSON file"
                    )
                authority_relative = Path(authority_relative_raw)
                if (
                    not authority_relative_raw
                    or authority_relative.is_absolute()
                    or authority_relative_raw != authority_relative.as_posix()
                    or "\\" in authority_relative_raw
                    or not authority_relative.parts
                    or any(part in {".", ".."} for part in authority_relative.parts)
                    or authority_relative.parent != Path("control/ibkr")
                    or authority_relative.suffix != ".json"
                ):
                    raise ValueError(
                        "IBKR unattended authority path must be a private JSON file under control/ibkr"
                    )
                if execution.get("ibkr_autonomous_authority_key_source") != "macos_keychain":
                    raise ValueError(
                        "IBKR unattended authority key source must be macos_keychain"
                    )
                for field in (
                    "ibkr_autonomous_authority_key_service",
                    "ibkr_autonomous_authority_key_account",
                ):
                    value = execution.get(field)
                    if type(value) is not str or _NONSECRET_LOCATOR.fullmatch(value) is None:
                        raise ValueError(
                            f"IBKR unattended {field} must be a nonsecret keychain locator"
                        )
                if (
                    execution.get("ibkr_autonomous_policy_receipt_schema")
                    != _IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA
                ):
                    raise ValueError(
                        "IBKR unattended owner-policy/pricing receipt schema is missing or invalid"
                    )
                policy_receipt_relative_raw = execution.get(
                    "ibkr_autonomous_policy_receipt_relative_path"
                )
                if type(policy_receipt_relative_raw) is not str:
                    raise ValueError(
                        "IBKR unattended owner-policy/pricing receipt path must be a relative JSON file"
                    )
                policy_receipt_relative = Path(policy_receipt_relative_raw)
                if (
                    not policy_receipt_relative_raw
                    or policy_receipt_relative.is_absolute()
                    or policy_receipt_relative_raw
                    != policy_receipt_relative.as_posix()
                    or "\\" in policy_receipt_relative_raw
                    or not policy_receipt_relative.parts
                    or any(
                        part in {".", ".."}
                        for part in policy_receipt_relative.parts
                    )
                    or policy_receipt_relative.parent != Path("control/ibkr")
                    or policy_receipt_relative.suffix != ".json"
                    or policy_receipt_relative == authority_relative
                ):
                    raise ValueError(
                        "IBKR unattended owner-policy/pricing receipt path must be a distinct private JSON file under control/ibkr"
                    )
                if (
                    execution.get("ibkr_autonomous_policy_receipt_key_source")
                    != "macos_keychain"
                ):
                    raise ValueError(
                        "IBKR unattended owner-policy/pricing receipt key source must be macos_keychain"
                    )
                for field in (
                    "ibkr_autonomous_policy_receipt_key_service",
                    "ibkr_autonomous_policy_receipt_key_account",
                ):
                    value = execution.get(field)
                    if (
                        type(value) is not str
                        or _NONSECRET_LOCATOR.fullmatch(value) is None
                    ):
                        raise ValueError(
                            f"IBKR unattended {field} must be a nonsecret keychain locator"
                        )
                if (
                    execution["ibkr_autonomous_policy_receipt_key_service"],
                    execution["ibkr_autonomous_policy_receipt_key_account"],
                ) == (
                    execution["ibkr_autonomous_authority_key_service"],
                    execution["ibkr_autonomous_authority_key_account"],
                ):
                    raise ValueError(
                        "IBKR unattended owner-policy/pricing receipt key must be distinct"
                    )
                if not self.session_trading_risk:
                    if (
                        execution.get("ibkr_daily_risk_baseline_schema")
                        != self.daily_risk_baseline_schema
                    ):
                        raise ValueError(
                            "IBKR unattended daily risk baseline schema is missing or invalid"
                        )
                    baseline_relative_raw = execution.get(
                        "ibkr_daily_risk_baseline_relative_path"
                    )
                    if type(baseline_relative_raw) is not str:
                        raise ValueError(
                            "IBKR unattended daily risk baseline path must be a relative JSON file"
                        )
                    baseline_relative = Path(baseline_relative_raw)
                    if (
                        not baseline_relative_raw
                        or baseline_relative.is_absolute()
                        or baseline_relative_raw != baseline_relative.as_posix()
                        or "\\" in baseline_relative_raw
                        or not baseline_relative.parts
                        or any(part in {".", ".."} for part in baseline_relative.parts)
                        or baseline_relative.parent != Path("control/ibkr")
                        or baseline_relative.suffix != ".json"
                        or baseline_relative in {
                            authority_relative,
                            policy_receipt_relative,
                        }
                    ):
                        raise ValueError(
                            "IBKR unattended daily risk baseline path must be a distinct private JSON file under control/ibkr"
                        )
                    if (
                        execution.get("ibkr_daily_risk_baseline_key_source")
                        != "macos_keychain"
                    ):
                        raise ValueError(
                            "IBKR unattended daily risk baseline key source must be macos_keychain"
                        )
                    for field in (
                        "ibkr_daily_risk_baseline_key_service",
                        "ibkr_daily_risk_baseline_key_account",
                    ):
                        value = execution.get(field)
                        if (
                            type(value) is not str
                            or _NONSECRET_LOCATOR.fullmatch(value) is None
                        ):
                            raise ValueError(
                                f"IBKR unattended {field} must be a nonsecret keychain locator"
                            )
                    baseline_key = (
                        execution["ibkr_daily_risk_baseline_key_service"],
                        execution["ibkr_daily_risk_baseline_key_account"],
                    )
                    if (
                        baseline_key
                        in {
                            (
                                execution["ibkr_autonomous_authority_key_service"],
                                execution["ibkr_autonomous_authority_key_account"],
                            ),
                            (
                                execution[
                                    "ibkr_autonomous_policy_receipt_key_service"
                                ],
                                execution[
                                    "ibkr_autonomous_policy_receipt_key_account"
                                ],
                            ),
                        }
                        or baseline_key[1] != self.account_key
                    ):
                        raise ValueError(
                            "IBKR unattended daily risk baseline key must be distinct and account-bound"
                        )
                    high_water_relative_raw = execution.get(
                        "ibkr_risk_high_water_ledger_relative_path"
                    )
                    if type(high_water_relative_raw) is not str:
                        raise ValueError(
                            "IBKR unattended risk high-water ledger path must be relative"
                        )
                    high_water_relative = Path(high_water_relative_raw)
                    if (
                        not high_water_relative_raw
                        or high_water_relative.is_absolute()
                        or high_water_relative_raw != high_water_relative.as_posix()
                        or "\\" in high_water_relative_raw
                        or not high_water_relative.parts
                        or any(part in {".", ".."} for part in high_water_relative.parts)
                        or high_water_relative.parent != Path("state")
                        or high_water_relative.suffix != ".sqlite3"
                        or high_water_relative == ledger_path
                        or high_water_relative
                        != IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH
                    ):
                        raise ValueError(
                            "IBKR unattended risk high-water ledger must use the canonical "
                            "state/ibkr-risk-high-water.sqlite3 path"
                        )
                if execution.get("ibkr_autonomous_api_name") != "official_tws_python_api":
                    raise ValueError("IBKR unattended API name is invalid")
                if execution.get("ibkr_autonomous_api_version") != ibkr_profile.sdk_version:
                    raise ValueError("IBKR unattended API version differs from the signed SDK")
                if execution.get("ibkr_autonomous_environment") != ibkr_profile.environment:
                    raise ValueError("IBKR unattended environment differs from the signed profile")
                autonomous_client_id = execution.get("ibkr_autonomous_client_id")
                if (
                    type(autonomous_client_id) is not int
                    or autonomous_client_id != ibkr_profile.command_client_id
                ):
                    raise ValueError(
                        "IBKR unattended client id differs from the signed command lane"
                    )
        if execution.get("broker_adapter") == "ibkr_local_gateway_staged":
            if ibkr_profile is None:
                raise ValueError("staged IBKR transport requires a signed local profile")
            deployment = self.config.get("deployment")
            assert isinstance(deployment, Mapping)
            if deployment.get("profile_id") != ibkr_profile.profile_id:
                raise ValueError("IBKR deployment/profile identity mismatch")
            evidence = self.config["evidence"]
            if evidence.get("require_robinhood_tradability") is not False:
                raise ValueError("IBKR profile cannot require Robinhood tradability")
            if evidence.get("require_broker_contract_tradability") is not True:
                raise ValueError("IBKR profile requires broker contract tradability")
        discovery = self.config["discovery"]
        if (
            self.live_entries_configured
            and discovery.get("provider_composition_id")
            not in {
                "titan.massive_rest_stream.robinhood_instrument.quality.v1",
                "titan.massive_rest_stream.ibkr_contract.quality.v1",
            }
        ):
            raise ValueError(
                "live entries require the release-shipped discovery composition"
            )
        if discovery.get("pipeline_configured") is True and not str(
            discovery.get("provider_composition_id", "")
        ).strip():
            raise ValueError(
                "configured discovery pipeline requires a signed provider composition identity"
            )
        if discovery.get("provider_composition_id") in {
            "titan.massive_rest_stream.robinhood_instrument.quality.v1",
            "titan.massive_rest_stream.ibkr_contract.quality.v1",
        }:
            binding = str(discovery.get("provider_binding_id", ""))
            if len(binding) != 64 or any(
                value not in "0123456789abcdef" for value in binding
            ):
                raise ValueError(
                    "supported production discovery requires a nonsecret 256-bit provider binding"
                )
        market_data = self.config.get("market_data")
        if not isinstance(market_data, Mapping):
            raise ValueError("full-live market-data configuration is missing")
        market_adapter = market_data.get("adapter")
        if market_data.get("source_is_execution_authority") is not False:
            raise ValueError("Massive evidence can never grant execution authority")
        if market_adapter == "local_titan_massive_sqlite_read_only":
            if market_data.get("producer_book_mode") != "SHADOW":
                raise ValueError("Massive compatibility producer must remain SHADOW")
        elif market_adapter == "massive_rest_stream_injected_auth":
            if market_data.get("provider") != "massive":
                raise ValueError("production Massive provider identity is invalid")
            if market_data.get("authorization_mode") != "runtime_injected_existing":
                raise ValueError("production Massive authorization must be runtime-injected")
        else:
            raise ValueError("unreviewed Massive market-data adapter configured")
        if int(market_data.get("health_max_age_seconds", 0)) <= 0:
            raise ValueError("Massive health age must be positive")
        if int(market_data.get("candidate_max_age_seconds", 0)) <= 0:
            raise ValueError("Massive candidate age must be positive")
        if int(market_data.get("max_active_candidates", 0)) <= 0:
            raise ValueError("Massive active-set size must be positive")
        discovery = self.config.get("discovery")
        if not isinstance(discovery, Mapping):
            raise ValueError("full-live discovery configuration is missing")
        if discovery.get("adapter") not in {
            "local_massive_full_live_pipeline",
            "massive_rest_stream_full_live_pipeline",
        }:
            raise ValueError("unreviewed live discovery adapter configured")
        if not isinstance(discovery.get("pipeline_configured"), bool):
            raise ValueError("live discovery configuration gate must be boolean")
        score_fields = (
            "minimum_setup_score",
            "minimum_execution_score",
            "a_plus_setup_score",
            "a_plus_execution_score",
        )
        for field in score_fields:
            value = discovery.get(field)
            if value is not None:
                number = float(value)
                if not 0 <= number <= 100:
                    raise ValueError(f"{field} must be in [0, 100]")
        values = {
            field: discovery.get(field)
            for field in score_fields
        }
        score_policy = discovery.get("score_policy", "threshold_gated")
        a_plus_enabled = discovery.get("a_plus_enabled", True)
        if type(a_plus_enabled) is not bool:
            raise ValueError("a_plus_enabled must be boolean")
        if score_policy == "ranking_only":
            if any(value is not None for value in values.values()):
                raise ValueError(
                    "ranking-only score policy cannot contain numeric score floors"
                )
            if a_plus_enabled is not False:
                raise ValueError(
                    "ranking-only score policy requires a_plus_enabled=false"
                )
        elif score_policy == "threshold_gated":
            populated = tuple(value is not None for value in values.values())
            if any(populated) and not all(populated):
                raise ValueError(
                    "threshold-gated score policy cannot contain partial thresholds"
                )
            if a_plus_enabled is not True:
                raise ValueError(
                    "threshold-gated score policy requires a_plus_enabled=true"
                )
        else:
            raise ValueError("unsupported signed score policy")
        if score_policy == "threshold_gated" and all(
            value is not None for value in values.values()
        ):
            if float(values["a_plus_setup_score"]) < float(
                values["minimum_setup_score"]
            ) or float(values["a_plus_execution_score"]) < float(
                values["minimum_execution_score"]
            ):
                raise ValueError("A+ score thresholds cannot be below live floors")
        # Separate schemas prevent staged percentages from becoming hidden
        # dollar-mode limits, or a config boolean from approving altered risk.
        risk_model = risk.get("model", "percentage_overlay")
        if risk_model in {DOLLAR_HEADROOM_MODEL, DAILY_STARTING_EQUITY_MODEL, SESSION_TRADING_MODEL}:
            if not self.risk_provenance_verified:
                raise ValueError("account-day risk provenance must match the exact approved contract")
            if (
                risk.get("positive_execution_reserve_required") is not True
                or risk.get("loss_lock_is_irreversible_for_session") is not True
            ):
                raise ValueError("account-day reserve and irreversible loss lock are mandatory")
            for field, minimum in (
                ("minimum_commission_reserve_per_order_dollars", Decimal("1")),
                ("minimum_entry_lifecycle_fee_reserve_dollars", Decimal("2")),
            ):
                if decimal_value(execution.get(field), field) < minimum:
                    raise ValueError("account-day commission reserve is below the approved minimum")
        elif risk_model == "percentage_overlay":
            self.risk_limits
        else:
            raise ValueError("unsupported risk policy model")

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
