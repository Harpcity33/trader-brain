"""Strict, expiring trade plans bound to market evidence and policy hashes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
from typing import Any, Mapping

from .money import decimal_value, whole_shares
from .policy import PolicyBundle, sha256_json


PLAN_SCHEMA = "titan_expiring_equity_plan_2026-09-08_v1"
_FIELDS = frozenset(
    {
        "schema_version",
        "plan_id",
        "strategy_id",
        "policy_hash",
        "config_hash",
        "account_last4",
        "symbol",
        "instrument_id",
        "setup_id",
        "direction",
        "quantity",
        "entry_limit",
        "structural_stop",
        "targets",
        "execution_reserve_per_share",
        "market_hours",
        "time_in_force",
        "quality_tier",
        "completed_bar_end",
        "quote_observed_at",
        "created_at",
        "expires_at",
        "source_event_ids",
        "allow_add",
        "allow_reentry",
    }
)


def _dt(value: Any, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if result.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return result


@dataclass(frozen=True)
class ExpiringPlan:
    plan_id: str
    strategy_id: str
    policy_hash: str
    config_hash: str
    account_last4: str
    symbol: str
    instrument_id: str
    setup_id: str
    quantity: int
    entry_limit: Decimal
    structural_stop: Decimal
    targets: tuple[Decimal, ...]
    execution_reserve_per_share: Decimal
    market_hours: str
    time_in_force: str
    quality_tier: str
    completed_bar_end: datetime
    quote_observed_at: datetime
    created_at: datetime
    expires_at: datetime
    source_event_ids: tuple[str, ...]
    direction: str = "long"
    allow_add: bool = False
    allow_reentry: bool = False
    schema_version: str = PLAN_SCHEMA

    @classmethod
    def build(cls, **raw: Any) -> "ExpiringPlan":
        body = cls._normalize({**raw, "schema_version": PLAN_SCHEMA, "plan_id": ""})
        body["plan_id"] = sha256_json(cls._canonical_body(body))
        return cls(**body)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExpiringPlan":
        if not isinstance(raw, Mapping):
            raise ValueError("plan must be an object")
        missing = _FIELDS - set(raw)
        unknown = set(raw) - _FIELDS
        if missing or unknown:
            raise ValueError(f"plan fields mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")
        body = cls._normalize(dict(raw))
        supplied = str(body["plan_id"])
        expected = sha256_json(cls._canonical_body({**body, "plan_id": ""}))
        if supplied != expected:
            raise ValueError("plan_id does not match canonical plan content")
        return cls(**body)

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        required = _FIELDS - {"schema_version", "plan_id", "direction", "allow_add", "allow_reentry"}
        missing = required - set(raw)
        if missing:
            raise ValueError(f"missing plan fields: {sorted(missing)}")
        targets = raw["targets"]
        events = raw["source_event_ids"]
        if not isinstance(targets, (list, tuple)) or not targets:
            raise ValueError("at least one target is required")
        if not isinstance(events, (list, tuple)) or not events or any(not str(x).strip() for x in events):
            raise ValueError("source_event_ids must be a non-empty list")
        return {
            "schema_version": str(raw.get("schema_version", PLAN_SCHEMA)),
            "plan_id": str(raw.get("plan_id", "")),
            "strategy_id": str(raw["strategy_id"]),
            "policy_hash": str(raw["policy_hash"]),
            "config_hash": str(raw["config_hash"]),
            "account_last4": str(raw["account_last4"]),
            "symbol": str(raw["symbol"]).strip().upper(),
            "instrument_id": str(raw["instrument_id"]).strip(),
            "setup_id": str(raw["setup_id"]).strip(),
            "direction": str(raw.get("direction", "long")),
            "quantity": whole_shares(raw["quantity"]),
            "entry_limit": decimal_value(raw["entry_limit"], "entry_limit"),
            "structural_stop": decimal_value(raw["structural_stop"], "structural_stop"),
            "targets": tuple(decimal_value(value, "target") for value in targets),
            "execution_reserve_per_share": decimal_value(
                raw["execution_reserve_per_share"], "execution_reserve_per_share"
            ),
            "market_hours": str(raw["market_hours"]),
            "time_in_force": str(raw["time_in_force"]),
            "quality_tier": str(raw["quality_tier"]),
            "completed_bar_end": _dt(raw["completed_bar_end"], "completed_bar_end"),
            "quote_observed_at": _dt(raw["quote_observed_at"], "quote_observed_at"),
            "created_at": _dt(raw["created_at"], "created_at"),
            "expires_at": _dt(raw["expires_at"], "expires_at"),
            "source_event_ids": tuple(str(value) for value in events),
            "allow_add": raw.get("allow_add", False) is True,
            "allow_reentry": raw.get("allow_reentry", False) is True,
        }

    @staticmethod
    def _canonical_body(body: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": str(body["schema_version"]),
            "strategy_id": str(body["strategy_id"]),
            "policy_hash": str(body["policy_hash"]),
            "config_hash": str(body["config_hash"]),
            "account_last4": str(body["account_last4"]),
            "symbol": str(body["symbol"]),
            "instrument_id": str(body["instrument_id"]),
            "setup_id": str(body["setup_id"]),
            "direction": str(body["direction"]),
            "quantity": int(body["quantity"]),
            "entry_limit": format(decimal_value(body["entry_limit"], "entry_limit"), "f"),
            "structural_stop": format(decimal_value(body["structural_stop"], "structural_stop"), "f"),
            "targets": [format(decimal_value(x, "target"), "f") for x in body["targets"]],
            "execution_reserve_per_share": format(
                decimal_value(body["execution_reserve_per_share"], "reserve"), "f"
            ),
            "market_hours": str(body["market_hours"]),
            "time_in_force": str(body["time_in_force"]),
            "quality_tier": str(body["quality_tier"]),
            "completed_bar_end": _dt(body["completed_bar_end"], "completed_bar_end").isoformat(),
            "quote_observed_at": _dt(body["quote_observed_at"], "quote_observed_at").isoformat(),
            "created_at": _dt(body["created_at"], "created_at").isoformat(),
            "expires_at": _dt(body["expires_at"], "expires_at").isoformat(),
            "source_event_ids": list(body["source_event_ids"]),
            "allow_add": body["allow_add"] is True,
            "allow_reentry": body["allow_reentry"] is True,
        }

    def to_mapping(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, **self._canonical_body(self.__dict__)}

    def validate(self, policy: PolicyBundle, now: datetime) -> None:
        if self.schema_version != PLAN_SCHEMA:
            raise ValueError("unsupported plan schema")
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_id):
            raise ValueError("invalid plan id")
        expected = sha256_json(self._canonical_body(self.__dict__))
        if self.plan_id != expected:
            raise ValueError("plan content changed after signing")
        if (
            self.strategy_id != policy.strategy_id
            or self.policy_hash != policy.policy_hash
            or self.config_hash != policy.config_hash
            or self.account_last4 != policy.account_last4
        ):
            raise ValueError("plan policy/account binding mismatch")
        if self.direction != "long" or self.allow_add or self.allow_reentry:
            raise ValueError("plan attempts an unauthorized position transition")
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", self.symbol):
            raise ValueError("invalid symbol")
        if not self.instrument_id or not self.setup_id:
            raise ValueError("instrument and setup identifiers are required")
        if self.entry_limit <= Decimal("5"):
            raise ValueError("entry must be strictly above $5")
        if self.structural_stop <= 0 or self.structural_stop >= self.entry_limit:
            raise ValueError("structural stop must be below entry")
        if self.execution_reserve_per_share <= 0:
            raise ValueError("positive execution reserve is required")
        if any(target <= self.entry_limit for target in self.targets):
            raise ValueError("long-equity targets must be above entry")
        if self.quality_tier not in {"normal", "a_plus"}:
            raise ValueError("invalid quality tier")
        if self.completed_bar_end > self.created_at:
            raise ValueError("plan depends on an incomplete future bar")
        if self.quote_observed_at > self.created_at:
            raise ValueError("plan depends on a future quote")
        if self.expires_at <= self.created_at or now > self.expires_at:
            raise ValueError("plan expired")
        policy.require_entry_tuple(
            quantity=self.quantity,
            limit_price=self.entry_limit,
            market_hours=self.market_hours,
            order_type="limit",
            time_in_force=self.time_in_force,
            now=now,
        )

    @property
    def planned_risk(self) -> Decimal:
        return (self.entry_limit - self.structural_stop) * self.quantity

    @property
    def execution_reserve(self) -> Decimal:
        return self.execution_reserve_per_share * self.quantity

    @property
    def stress_risk(self) -> Decimal:
        return self.planned_risk + self.execution_reserve

    @property
    def notional(self) -> Decimal:
        return self.entry_limit * self.quantity


__all__ = ["ExpiringPlan", "PLAN_SCHEMA"]
