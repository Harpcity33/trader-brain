"""Hash-sealed, state-backed order plans for the autonomous IBKR preflight.

The generic execution lifecycle durably writes a plan, a risk reservation and
an exact order intent before it asks a broker adapter for a review.  Those
tables intentionally do not contain all of the IBKR-local preview geometry
(most importantly the exact predeclared stop request and a separately stated
fee reserve).  Guessing either value at the broker boundary would weaken the
policy.

This module supplies the narrow bridge between those two representations.  A
producer must explicitly seal the complete :class:`IbkrAttendedOrderPlan`
against an already prepared lifecycle intent.  The seal is canonical-hashed
and appended to the existing immutable, hash-chained audit stream.  The reader
will return it only while that exact intent is PREPARED or SUBMITTING and all
durable plan/risk/tuple facts still agree.

Despite the historical class name, returning ``IbkrAttendedOrderPlan`` does
not grant attended or unattended mutation authority and does not contain a
confirmation phrase.  Provider authority, runtime activation, the writer
lease and the one-shot transport decision remain independent mandatory gates.
No configuration boolean is accepted by this module as authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping
from uuid import UUID, uuid5

from .broker.base import (
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderRequest,
    TimeInForce,
)
from .broker.ibkr_preflight import IbkrAttendedOrderPlan, IbkrOrderPurpose
from .models import IntentKind, IntentState, PlanState, ReservationState
from .money import from_cents
from .state import LiveStateStore, canonical_json, object_hash


AUTONOMOUS_IBKR_PLAN_SCHEMA = "titan_ibkr_autonomous_plan_seal_2026-09-14_v1"
AUTONOMOUS_IBKR_PLAN_EVENT = "IBKR_AUTONOMOUS_PLAN_SEALED"
_SEAL_NAMESPACE = UUID("99dd3e53-bec7-45f1-b6b5-ed7011ee3f02")
_STOP_NAMESPACE = UUID("5bf77f86-9607-4de1-939f-5fbf4c52b465")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENT_INTENT_STATES = frozenset(
    {IntentState.PREPARED.value, IntentState.SUBMITTING.value}
)
_REQUEST_FIELDS = frozenset(
    {
        "account_masked",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "market_hours",
        "time_in_force",
        "client_ref_id",
        "limit_price",
        "stop_price",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "seal_id",
        "intent_id",
        "runtime_id",
        "release_manifest_hash",
        "account_key",
        "strategy_id",
        "policy_hash",
        "config_hash",
        "created_at",
        "expires_at",
        "plan_id",
        "purpose",
        "request",
        "structural_stop",
        "targets",
        "execution_reserve",
        "fee_reserve",
        "required_stop_request",
    }
)


class AutonomousIbkrPlanError(RuntimeError):
    """A complete, current, exact durable plan could not be proved."""


def _deny(code: str) -> AutonomousIbkrPlanError:
    return AutonomousIbkrPlanError(code)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _deny(f"IBKR_AUTONOMOUS_PLAN_{field.upper()}_INVALID")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise _deny(f"IBKR_AUTONOMOUS_PLAN_{field.upper()}_INVALID") from exc
    return _aware(parsed, field)


def _request_payload(request: OrderRequest) -> dict[str, object]:
    return {
        "account_masked": request.account_masked,
        "symbol": request.symbol,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": request.quantity,
        "market_hours": request.market_hours.value,
        "time_in_force": request.time_in_force.value,
        "client_ref_id": request.client_ref_id,
        "limit_price": (
            format(request.limit_price, "f")
            if request.limit_price is not None
            else None
        ),
        "stop_price": (
            format(request.stop_price, "f")
            if request.stop_price is not None
            else None
        ),
    }


def _request_from_payload(raw: object) -> OrderRequest:
    if not isinstance(raw, Mapping) or set(raw) != _REQUEST_FIELDS:
        raise _deny("IBKR_AUTONOMOUS_PLAN_REQUEST_FIELDS_INVALID")
    try:
        return OrderRequest(
            account_masked=raw["account_masked"],
            symbol=raw["symbol"],
            side=BrokerSide(raw["side"]),
            order_type=EquityOrderType(raw["order_type"]),
            quantity=raw["quantity"],
            market_hours=MarketHours(raw["market_hours"]),
            time_in_force=TimeInForce(raw["time_in_force"]),
            client_ref_id=raw["client_ref_id"],
            limit_price=raw["limit_price"],
            stop_price=raw["stop_price"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _deny("IBKR_AUTONOMOUS_PLAN_REQUEST_INVALID") from exc


@dataclass(frozen=True)
class AutonomousIbkrPlanBindings:
    """Immutable release/policy identities; deliberately not an authority."""

    runtime_id: str
    release_manifest_hash: str
    account_key: str
    account_masked: str
    strategy_id: str
    policy_hash: str
    config_hash: str

    def __post_init__(self) -> None:
        for name in ("runtime_id", "account_key", "strategy_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        # Reuse OrderRequest's strict account-mask parser without inventing a
        # second display-account grammar.
        try:
            sentinel = OrderRequest(
                account_masked=self.account_masked,
                symbol="SPY",
                side=BrokerSide.BUY,
                order_type=EquityOrderType.LIMIT,
                quantity=1,
                market_hours=MarketHours.REGULAR,
                time_in_force=TimeInForce.GFD,
                client_ref_id="00000000-0000-4000-8000-000000000001",
                limit_price=Decimal("5.01"),
            )
        except ValueError as exc:
            raise ValueError("account_masked is invalid") from exc
        object.__setattr__(self, "account_masked", sentinel.account_masked)
        for name in ("release_manifest_hash", "policy_hash", "config_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class AutonomousIbkrPlanSeal:
    """Canonical complete plan envelope stored in the audit chain."""

    seal_id: str
    intent_id: str
    runtime_id: str
    release_manifest_hash: str
    account_key: str
    strategy_id: str
    policy_hash: str
    config_hash: str
    created_at: datetime
    expires_at: datetime
    plan: IbkrAttendedOrderPlan
    schema_version: str = AUTONOMOUS_IBKR_PLAN_SCHEMA

    def __post_init__(self) -> None:
        for name in ("intent_id", "runtime_id", "account_key", "strategy_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("release_manifest_hash", "policy_hash", "config_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.schema_version != AUTONOMOUS_IBKR_PLAN_SCHEMA:
            raise ValueError("unsupported autonomous IBKR plan seal schema")
        if not isinstance(self.plan, IbkrAttendedOrderPlan):
            raise ValueError("a complete exact IBKR order plan is required")
        if not _SHA256.fullmatch(self.plan.plan_id):
            raise ValueError("plan_id must be a canonical SHA-256 identifier")
        created = _aware(self.created_at, "seal_created_at")
        expires = _aware(self.expires_at, "seal_expires_at")
        if expires <= created:
            raise ValueError("autonomous IBKR plan seal must expire after creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        expected = self.recomputed_seal_id()
        if self.seal_id != expected:
            raise ValueError("autonomous IBKR plan seal hash does not match content")

    @classmethod
    def build(
        cls,
        *,
        intent_id: str,
        runtime_id: str,
        release_manifest_hash: str,
        account_key: str,
        strategy_id: str,
        policy_hash: str,
        config_hash: str,
        created_at: datetime,
        expires_at: datetime,
        plan: IbkrAttendedOrderPlan,
    ) -> "AutonomousIbkrPlanSeal":
        provisional = cls.__new__(cls)
        values = {
            "seal_id": "",
            "intent_id": intent_id,
            "runtime_id": runtime_id,
            "release_manifest_hash": release_manifest_hash,
            "account_key": account_key,
            "strategy_id": strategy_id,
            "policy_hash": policy_hash,
            "config_hash": config_hash,
            "created_at": _aware(created_at, "seal_created_at"),
            "expires_at": _aware(expires_at, "seal_expires_at"),
            "plan": plan,
            "schema_version": AUTONOMOUS_IBKR_PLAN_SCHEMA,
        }
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        values["seal_id"] = provisional.recomputed_seal_id()
        return cls(**values)

    @classmethod
    def from_payload(cls, raw: object) -> "AutonomousIbkrPlanSeal":
        if not isinstance(raw, Mapping) or set(raw) != _ENVELOPE_FIELDS:
            raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_FIELDS_INVALID")
        request = _request_from_payload(raw["request"])
        stop_raw = raw["required_stop_request"]
        stop = None if stop_raw is None else _request_from_payload(stop_raw)
        try:
            plan = IbkrAttendedOrderPlan(
                plan_id=str(raw["plan_id"]),
                purpose=IbkrOrderPurpose(str(raw["purpose"])),
                request=request,
                structural_stop=raw["structural_stop"],
                targets=tuple(raw["targets"]),
                execution_reserve=Decimal(str(raw["execution_reserve"])),
                fee_reserve=Decimal(str(raw["fee_reserve"])),
                required_stop_request=stop,
            )
            return cls(
                seal_id=str(raw["seal_id"]),
                intent_id=str(raw["intent_id"]),
                runtime_id=str(raw["runtime_id"]),
                release_manifest_hash=str(raw["release_manifest_hash"]),
                account_key=str(raw["account_key"]),
                strategy_id=str(raw["strategy_id"]),
                policy_hash=str(raw["policy_hash"]),
                config_hash=str(raw["config_hash"]),
                created_at=_parse_time(raw["created_at"], "seal_created_at"),
                expires_at=_parse_time(raw["expires_at"], "seal_expires_at"),
                plan=plan,
                schema_version=str(raw["schema_version"]),
            )
        except AutonomousIbkrPlanError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_INVALID") from exc

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "runtime_id": self.runtime_id,
            "release_manifest_hash": self.release_manifest_hash,
            "account_key": self.account_key,
            "strategy_id": self.strategy_id,
            "policy_hash": self.policy_hash,
            "config_hash": self.config_hash,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "plan_id": self.plan.plan_id,
            "purpose": self.plan.purpose.value,
            "request": _request_payload(self.plan.request),
            "structural_stop": (
                format(self.plan.structural_stop, "f")
                if self.plan.structural_stop is not None
                else None
            ),
            "targets": [format(target, "f") for target in self.plan.targets],
            "execution_reserve": format(self.plan.execution_reserve, "f"),
            "fee_reserve": format(self.plan.fee_reserve, "f"),
            "required_stop_request": (
                _request_payload(self.plan.required_stop_request)
                if self.plan.required_stop_request is not None
                else None
            ),
        }

    def recomputed_seal_id(self) -> str:
        return hashlib.sha256(canonical_json(self._body()).encode("utf-8")).hexdigest()

    def to_payload(self) -> dict[str, object]:
        return {"seal_id": self.seal_id, **self._body()}

    @property
    def event_id(self) -> str:
        return str(uuid5(_SEAL_NAMESPACE, self.seal_id))


def _row_request(raw: Mapping[str, Any]) -> OrderRequest:
    """Parse the common order fields from a durable lifecycle tuple."""

    required = {
        "account_masked",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "market_hours",
        "time_in_force",
        "limit_price",
        "stop_price",
        "client_ref_id",
    }
    if not required.issubset(raw):
        raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_TUPLE_FIELDS_INVALID")
    return _request_from_payload({key: raw[key] for key in _REQUEST_FIELDS})


def _prep_event_matches(
    state: LiveStateStore,
    *,
    intent: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bool:
    event_type = (
        "SUBMISSION_PREPARED"
        if intent["kind"] == IntentKind.ENTRY.value
        else "SAFETY_INTENT_PREPARED"
    )
    rows = state.rows(
        """SELECT * FROM audit_events
             WHERE event_type=? AND entity_type='order_intent' AND entity_id=?""",
        (event_type, intent["intent_id"]),
    )
    if len(rows) != 1:
        return False
    row = rows[0]
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        return False
    common = all(
        (
            row["stream"] == intent["account_key"],
            _parse_time(row["occurred_at"], "prepared_event_time")
            == _parse_time(intent["created_at"], "intent_created_at"),
            payload.get("plan_id") == intent["plan_id"],
            payload.get("client_ref") == intent["client_ref"],
            payload.get("tuple_hash") == intent["tuple_hash"],
        )
    )
    if not common:
        return False
    if event_type == "SUBMISSION_PREPARED":
        return all(
            (
                payload.get("reservation_id") == intent["reservation_id"],
                payload.get("policy_hash") == plan["policy_hash"],
                payload.get("config_hash") == plan["config_hash"],
                payload.get("evidence_hash") == plan["evidence_hash"],
            )
        )
    return payload.get("kind") == intent["kind"]


class StateBackedAutonomousIbkrPlanReader:
    """Return one exact sealed plan for a current durable lifecycle intent.

    PREPARED may be read for review and SUBMITTING may be read for the final
    transport revalidations.  Every other state, including UNKNOWN, is denied;
    therefore process replay cannot turn this reader into an automatic retry.
    """

    def __init__(
        self,
        *,
        state: LiveStateStore,
        bindings: AutonomousIbkrPlanBindings,
        clock,
    ) -> None:
        if not isinstance(state, LiveStateStore):
            raise TypeError("LiveStateStore is required")
        if not isinstance(bindings, AutonomousIbkrPlanBindings):
            raise TypeError("AutonomousIbkrPlanBindings are required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.state = state
        self.bindings = bindings
        self._clock = clock

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_autonomous_plan_state",
                self.state,
                (
                    "rows",
                    "verify_event_chain",
                    "autonomous_interlock_snapshot",
                ),
            ),
            ("ibkr_autonomous_plan_clock", self._clock, ("__call__",)),
        )

    def __call__(self, request: OrderRequest) -> IbkrAttendedOrderPlan:
        if not isinstance(request, OrderRequest):
            raise _deny("IBKR_AUTONOMOUS_PLAN_NORMALIZED_REQUEST_REQUIRED")
        current = _aware(self._clock(), "clock")
        valid_chain, expected_events, expected_head = self.state.verify_event_chain()
        if not valid_chain:
            raise _deny("IBKR_AUTONOMOUS_PLAN_AUDIT_CHAIN_INVALID")
        intents = self.state.rows(
            "SELECT * FROM order_intents WHERE client_ref=?",
            (request.client_ref_id,),
        )
        if len(intents) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_INTENT_UNAVAILABLE")
        intent = intents[0]
        seals = self.state.rows(
            """SELECT * FROM audit_events WHERE event_type=?
                 AND entity_type='order_intent' AND entity_id=?""",
            (AUTONOMOUS_IBKR_PLAN_EVENT, intent["intent_id"]),
        )
        if len(seals) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_SEAL_UNAVAILABLE")
        try:
            seal_sequence = int(seals[0]["sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_SEAL_INVALID") from exc
        # The selected seal must have been part of the exact chain snapshot
        # verified above.  A seal appended concurrently after that snapshot is
        # not execution authority, even if the append itself is hash-valid.
        if not 0 < seal_sequence <= expected_events:
            raise _deny("IBKR_AUTONOMOUS_PLAN_AUDIT_CHAIN_CHANGED")
        try:
            seal = AutonomousIbkrPlanSeal.from_payload(
                json.loads(str(seals[0]["payload_json"]))
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_SEAL_INVALID") from exc
        self._validate(
            request=request,
            intent=intent,
            seal_row=seals[0],
            seal=seal,
            current=current,
            require_current=True,
        )
        # Detect a concurrent append/tamper between selection and return.  The
        # state connection serializes local writes, but this second check also
        # makes the boundary fail closed if a foreign process bypasses it.
        valid_after, events_after, head_after = self.state.verify_event_chain()
        if (
            not valid_after
            or events_after != expected_events
            or head_after != expected_head
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_AUDIT_CHAIN_CHANGED")
        return seal.plan

    def _validate(
        self,
        *,
        request: OrderRequest,
        intent: Mapping[str, Any],
        seal_row: Mapping[str, Any] | None,
        seal: AutonomousIbkrPlanSeal,
        current: datetime,
        require_current: bool,
    ) -> None:
        bindings = self.bindings
        runtime = self.state.runtime_status()
        if runtime is None or any(
            (
                runtime["runtime_id"] != bindings.runtime_id,
                runtime["release_manifest_hash"]
                != bindings.release_manifest_hash,
                runtime["account_key"] != bindings.account_key,
                runtime["policy_hash"] != bindings.policy_hash,
                runtime["config_hash"] != bindings.config_hash,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_RUNTIME_BINDING_MISMATCH")
        if any(
            (
                seal.runtime_id != bindings.runtime_id,
                seal.release_manifest_hash != bindings.release_manifest_hash,
                seal.account_key != bindings.account_key,
                seal.strategy_id != bindings.strategy_id,
                seal.policy_hash != bindings.policy_hash,
                seal.config_hash != bindings.config_hash,
                seal.plan.request.account_masked != bindings.account_masked,
                seal.intent_id != intent["intent_id"],
                seal.plan.plan_id != intent["plan_id"],
                seal.plan.request.exact_tuple != request.exact_tuple,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_BINDING_MISMATCH")
        if seal_row is not None and any(
            (
                seal_row["event_id"] != seal.event_id,
                seal_row["stream"] != bindings.account_key,
                _parse_time(seal_row["occurred_at"], "seal_event_time")
                != seal.created_at,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_EVENT_MISMATCH")
        if require_current and intent["state"] not in _CURRENT_INTENT_STATES:
            raise _deny("IBKR_AUTONOMOUS_PLAN_INTENT_NOT_CURRENT")
        created = _parse_time(intent["created_at"], "intent_created_at")
        updated = _parse_time(intent["updated_at"], "intent_updated_at")
        deadline = _parse_time(
            intent["acknowledgement_deadline_at"], "intent_deadline"
        )
        if any(
            (
                intent["account_key"] != bindings.account_key,
                seal.created_at < created,
                seal.expires_at > deadline,
                created > current,
                updated > current,
                not seal.created_at <= current < seal.expires_at,
                current > deadline,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_INTENT_OR_SEAL_STALE")
        try:
            durable_tuple = json.loads(str(intent["order_tuple_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_TUPLE_INVALID") from exc
        if (
            not isinstance(durable_tuple, Mapping)
            or object_hash(durable_tuple) != intent["tuple_hash"]
            or _row_request(durable_tuple).exact_tuple != request.exact_tuple
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_TUPLE_MISMATCH")
        expected_kind = {
            IbkrOrderPurpose.ENTRY: IntentKind.ENTRY.value,
            IbkrOrderPurpose.PROTECTION: IntentKind.PROTECTION.value,
            IbkrOrderPurpose.EXIT: IntentKind.EXIT.value,
        }[seal.plan.purpose]
        if intent["kind"] != expected_kind:
            raise _deny("IBKR_AUTONOMOUS_PLAN_PURPOSE_INTENT_MISMATCH")
        if expected_kind == IntentKind.ENTRY.value:
            expected_fields = _REQUEST_FIELDS | {"account_key"}
        else:
            expected_fields = _REQUEST_FIELDS | {
                "account_key",
                "operation",
                "plan_id",
                "kind",
                "operation_key",
            }
            if any(
                (
                    durable_tuple.get("operation") != "place_equity_order",
                    durable_tuple.get("plan_id") != seal.plan.plan_id,
                    durable_tuple.get("kind") != expected_kind,
                    not str(durable_tuple.get("operation_key", "")).strip(),
                )
            ):
                raise _deny("IBKR_AUTONOMOUS_PLAN_SAFETY_TUPLE_INVALID")
        if set(durable_tuple) != expected_fields:
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_TUPLE_FIELDS_INVALID")
        plans = self.state.rows(
            "SELECT * FROM plans WHERE plan_id=?", (seal.plan.plan_id,)
        )
        if len(plans) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_PLAN_UNAVAILABLE")
        durable_plan = plans[0]
        self._validate_plan_row(seal.plan, durable_plan, current)
        if not _prep_event_matches(self.state, intent=intent, plan=durable_plan):
            raise _deny("IBKR_AUTONOMOUS_PLAN_PREPARED_AUDIT_MISMATCH")
        self._validate_reservation(seal.plan, durable_plan, intent)

    def _validate_plan_row(
        self,
        plan: IbkrAttendedOrderPlan,
        row: Mapping[str, Any],
        current: datetime,
    ) -> None:
        try:
            targets = tuple(Decimal(str(item)) for item in json.loads(row["targets_json"]))
            created = _parse_time(row["created_at"], "durable_plan_created_at")
            cutoff = _parse_time(row["evidence_cutoff_at"], "evidence_cutoff_at")
            expires = _parse_time(row["expires_at"], "durable_plan_expires_at")
            quantity = int(row["quantity"])
            limit_price = Decimal(str(row["limit_price"]))
            structural_stop = Decimal(str(row["structural_stop"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_PLAN_INVALID") from exc
        if any(
            (
                row["account_key"] != self.bindings.account_key,
                row["strategy_id"] != self.bindings.strategy_id,
                row["policy_hash"] != self.bindings.policy_hash,
                row["config_hash"] != self.bindings.config_hash,
                row["state"] != PlanState.VALIDATED.value,
                row["symbol"] != plan.request.symbol,
                not _SHA256.fullmatch(str(row["evidence_hash"])),
                cutoff > created,
                created > current,
                expires <= created,
                plan.request.quantity > quantity,
                limit_price <= Decimal("5"),
                structural_stop <= 0,
                structural_stop >= limit_price,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_PLAN_MISMATCH")
        if plan.purpose is IbkrOrderPurpose.ENTRY:
            if any(
                (
                    quantity != plan.request.quantity,
                    limit_price != plan.request.limit_price,
                    structural_stop != plan.structural_stop,
                    row["market_hours"] != plan.request.market_hours.value,
                    row["time_in_force"] != plan.request.time_in_force.value,
                    targets != plan.targets,
                    current >= expires,
                )
            ):
                raise _deny("IBKR_AUTONOMOUS_PLAN_ENTRY_GEOMETRY_MISMATCH")
        elif plan.purpose is IbkrOrderPurpose.PROTECTION:
            if any(
                (
                    structural_stop != plan.structural_stop,
                    plan.request.stop_price != structural_stop,
                    plan.request.quantity > quantity,
                )
            ):
                raise _deny("IBKR_AUTONOMOUS_PLAN_PROTECTION_GEOMETRY_MISMATCH")
        elif plan.structural_stop is not None and plan.structural_stop != structural_stop:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXIT_GEOMETRY_MISMATCH")

    def _validate_reservation(
        self,
        plan: IbkrAttendedOrderPlan,
        durable_plan: Mapping[str, Any],
        intent: Mapping[str, Any],
    ) -> None:
        reservations = self.state.rows(
            "SELECT * FROM risk_reservations WHERE plan_id=?", (plan.plan_id,)
        )
        if len(reservations) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_RISK_RESERVATION_UNAVAILABLE")
        reservation = reservations[0]
        try:
            planned = from_cents(int(reservation["planned_risk_cents"]))
            stress = from_cents(int(reservation["stress_risk_cents"]))
            execution = from_cents(int(reservation["execution_reserve_cents"]))
            notional = from_cents(int(reservation["notional_cents"]))
            origin_planned = (
                Decimal(str(durable_plan["limit_price"]))
                - Decimal(str(durable_plan["structural_stop"]))
            ) * int(durable_plan["quantity"])
            origin_notional = Decimal(str(durable_plan["limit_price"])) * int(
                durable_plan["quantity"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_RISK_RESERVATION_INVALID") from exc
        allowed_states = {
            ReservationState.RESERVED.value,
            ReservationState.BOUND.value,
        }
        if plan.purpose is not IbkrOrderPurpose.ENTRY:
            allowed_states.add(ReservationState.RELEASED.value)
        if any(
            (
                reservation["account_key"] != self.bindings.account_key,
                reservation["state"] not in allowed_states,
                planned != origin_planned,
                notional != origin_notional,
                execution != plan.execution_reserve,
                stress != planned + plan.execution_reserve + plan.fee_reserve,
                stress <= planned,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_RISK_RESERVATION_MISMATCH")
        if plan.purpose is IbkrOrderPurpose.ENTRY and any(
            (
                intent["reservation_id"] != reservation["reservation_id"],
                planned != plan.planned_downside,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_ENTRY_RISK_MISMATCH")


def autonomous_ibkr_stop_client_ref(
    *, plan_id: str, entry_client_ref_id: str
) -> str:
    """Derive the exact predeclared stop-template reference deterministically."""

    if not isinstance(plan_id, str) or not _SHA256.fullmatch(plan_id):
        raise ValueError("plan_id must be a lowercase SHA-256 digest")
    try:
        normalized_ref = str(UUID(str(entry_client_ref_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("entry_client_ref_id must be a UUID") from exc
    return str(
        uuid5(
            _STOP_NAMESPACE,
            ":".join(
                (
                    "titan-ibkr-autonomous-stop-template-v1",
                    plan_id,
                    normalized_ref,
                    "regular_hours",
                    "gtc",
                    "stop_market",
                )
            ),
        )
    )


class StateBackedAutonomousIbkrPlanProducer:
    """Build and seal exact IBKR plans from already prepared durable state.

    This producer never creates a lifecycle intent and never calls a broker.
    Its only write is the immutable plan-seal audit event.  Existing exact
    seals are reused after a crash; a changed or expired seal is denied.
    """

    def __init__(
        self,
        *,
        state: LiveStateStore,
        bindings: AutonomousIbkrPlanBindings,
        clock,
        seal_ttl_seconds: float,
    ) -> None:
        if not isinstance(state, LiveStateStore):
            raise TypeError("LiveStateStore is required")
        if not isinstance(bindings, AutonomousIbkrPlanBindings):
            raise TypeError("AutonomousIbkrPlanBindings are required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(seal_ttl_seconds, bool)
            or not isinstance(seal_ttl_seconds, (int, float))
            or not 0 < float(seal_ttl_seconds) <= 30
        ):
            raise ValueError("seal_ttl_seconds must be in (0, 30]")
        self.state = state
        self.bindings = bindings
        self._clock = clock
        self._ttl = float(seal_ttl_seconds)
        self.reader = StateBackedAutonomousIbkrPlanReader(
            state=state,
            bindings=bindings,
            clock=clock,
        )

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_autonomous_plan_reader",
                self.reader,
                ("release_components", "__call__"),
            ),
        )

    def __call__(
        self,
        *,
        intent_id: str,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest,
    ) -> None:
        self.seal(
            intent_id=intent_id,
            plan_id=plan_id,
            kind=kind,
            request=request,
        )
        return None

    def seal(
        self,
        *,
        intent_id: str,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest,
    ) -> AutonomousIbkrPlanSeal:
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise _deny("IBKR_AUTONOMOUS_PLAN_INTENT_ID_INVALID")
        if not isinstance(plan_id, str) or not _SHA256.fullmatch(plan_id):
            raise _deny("IBKR_AUTONOMOUS_PLAN_ID_INVALID")
        if not isinstance(request, OrderRequest):
            raise _deny("IBKR_AUTONOMOUS_PLAN_NORMALIZED_REQUEST_REQUIRED")
        try:
            normalized_kind = IntentKind(kind)
        except (TypeError, ValueError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_INTENT_KIND_INVALID") from exc
        if normalized_kind not in {
            IntentKind.ENTRY,
            IntentKind.PROTECTION,
            IntentKind.EXIT,
        }:
            raise _deny("IBKR_AUTONOMOUS_PLAN_INTENT_KIND_UNSUPPORTED")
        now = _aware(self._clock(), "clock")
        intents = self.state.rows(
            "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
        )
        if len(intents) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_INTENT_UNAVAILABLE")
        intent = intents[0]
        if any(
            (
                intent["plan_id"] != plan_id,
                intent["kind"] != normalized_kind.value,
                intent["client_ref"] != request.client_ref_id,
                intent["state"] != IntentState.PREPARED.value,
                intent["account_key"] != self.bindings.account_key,
            )
        ):
            raise _deny("IBKR_AUTONOMOUS_PLAN_PREPARED_INTENT_MISMATCH")
        rows = self.state.rows("SELECT * FROM plans WHERE plan_id=?", (plan_id,))
        reservations = self.state.rows(
            "SELECT * FROM risk_reservations WHERE plan_id=?", (plan_id,)
        )
        if len(rows) != 1 or len(reservations) != 1:
            raise _deny("IBKR_AUTONOMOUS_PLAN_OR_RISK_UNAVAILABLE")
        durable_plan = rows[0]
        reservation = reservations[0]
        try:
            targets = tuple(
                Decimal(str(item))
                for item in json.loads(str(durable_plan["targets_json"]))
            )
            structural_stop = Decimal(str(durable_plan["structural_stop"]))
            planned = from_cents(int(reservation["planned_risk_cents"]))
            stress = from_cents(int(reservation["stress_risk_cents"]))
            execution = from_cents(
                int(reservation["execution_reserve_cents"])
            )
            fee = stress - planned - execution
            deadline = _parse_time(
                intent["acknowledgement_deadline_at"], "intent_deadline"
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_DURABLE_GEOMETRY_INVALID") from exc
        if fee <= 0:
            raise _deny("IBKR_AUTONOMOUS_PLAN_POSITIVE_FEE_RESIDUAL_REQUIRED")
        purpose = {
            IntentKind.ENTRY: IbkrOrderPurpose.ENTRY,
            IntentKind.PROTECTION: IbkrOrderPurpose.PROTECTION,
            IntentKind.EXIT: IbkrOrderPurpose.EXIT,
        }[normalized_kind]
        stop: OrderRequest | None = None
        plan_targets = targets
        if normalized_kind is IntentKind.ENTRY:
            stop = OrderRequest(
                account_masked=request.account_masked,
                symbol=request.symbol,
                side=BrokerSide.SELL,
                order_type=EquityOrderType.STOP_MARKET,
                quantity=request.quantity,
                market_hours=MarketHours.REGULAR,
                time_in_force=TimeInForce.GTC,
                client_ref_id=autonomous_ibkr_stop_client_ref(
                    plan_id=plan_id,
                    entry_client_ref_id=request.client_ref_id,
                ),
                stop_price=structural_stop,
            )
        elif normalized_kind is IntentKind.PROTECTION:
            plan_targets = ()
        try:
            exact_plan = IbkrAttendedOrderPlan(
                plan_id=plan_id,
                purpose=purpose,
                request=request,
                structural_stop=structural_stop,
                targets=plan_targets,
                execution_reserve=execution,
                fee_reserve=fee,
                required_stop_request=stop,
            )
        except (TypeError, ValueError) as exc:
            raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_GEOMETRY_INVALID") from exc
        existing_rows = self.state.rows(
            """SELECT * FROM audit_events WHERE event_type=?
                 AND entity_type='order_intent' AND entity_id=?""",
            (AUTONOMOUS_IBKR_PLAN_EVENT, intent_id),
        )
        if existing_rows:
            if len(existing_rows) != 1:
                raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_CONFLICT")
            try:
                existing = AutonomousIbkrPlanSeal.from_payload(
                    json.loads(str(existing_rows[0]["payload_json"]))
                )
            except (TypeError, json.JSONDecodeError) as exc:
                raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_SEAL_INVALID") from exc
            if existing.plan != exact_plan:
                raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_CONFLICT")
            # Re-run all current durable and expiry checks.  This is the crash
            # replay path and must not append a second seal.
            if self.reader(request) != exact_plan:
                raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_CONFLICT")
            return existing
        expiry_candidates = [now + timedelta(seconds=self._ttl), deadline]
        if normalized_kind is IntentKind.ENTRY:
            expiry_candidates.append(
                _parse_time(
                    durable_plan["expires_at"], "durable_plan_expires_at"
                )
            )
        expires_at = min(expiry_candidates)
        if expires_at <= now:
            raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_WINDOW_EXPIRED")
        seal = AutonomousIbkrPlanSeal.build(
            intent_id=intent_id,
            runtime_id=self.bindings.runtime_id,
            release_manifest_hash=self.bindings.release_manifest_hash,
            account_key=self.bindings.account_key,
            strategy_id=self.bindings.strategy_id,
            policy_hash=self.bindings.policy_hash,
            config_hash=self.bindings.config_hash,
            created_at=now,
            expires_at=expires_at,
            plan=exact_plan,
        )
        seal_autonomous_ibkr_plan(
            state=self.state,
            bindings=self.bindings,
            seal=seal,
            clock=self._clock,
        )
        return seal


def seal_autonomous_ibkr_plan(
    *,
    state: LiveStateStore,
    bindings: AutonomousIbkrPlanBindings,
    seal: AutonomousIbkrPlanSeal,
    clock,
) -> bool:
    """Append one exact plan seal after its lifecycle intent is PREPARED.

    The operation is idempotent only for byte-identical content.  It cannot
    create a lifecycle plan, reservation or intent and therefore cannot turn a
    proposed order into executable authority.
    """

    if not isinstance(state, LiveStateStore):
        raise TypeError("LiveStateStore is required")
    if not isinstance(bindings, AutonomousIbkrPlanBindings):
        raise TypeError("AutonomousIbkrPlanBindings are required")
    if not isinstance(seal, AutonomousIbkrPlanSeal):
        raise TypeError("AutonomousIbkrPlanSeal is required")
    reader = StateBackedAutonomousIbkrPlanReader(
        state=state, bindings=bindings, clock=clock
    )
    now = _aware(clock(), "clock")
    intents = state.rows(
        "SELECT * FROM order_intents WHERE intent_id=?", (seal.intent_id,)
    )
    if len(intents) != 1:
        raise _deny("IBKR_AUTONOMOUS_PLAN_EXACT_INTENT_UNAVAILABLE")
    intent = intents[0]
    if intent["state"] != IntentState.PREPARED.value:
        raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_REQUIRES_PREPARED_INTENT")
    # Validate all durable joins before extending the audit chain.  Passing the
    # explicit seal as the source means no geometry is synthesized here.
    reader._validate(
        request=seal.plan.request,
        intent=intent,
        seal_row=None,
        seal=seal,
        current=now,
        require_current=True,
    )
    valid_chain, _events, _head = state.verify_event_chain()
    if not valid_chain:
        raise _deny("IBKR_AUTONOMOUS_PLAN_AUDIT_CHAIN_INVALID")
    prior = state.rows(
        """SELECT * FROM audit_events WHERE event_type=?
             AND entity_type='order_intent' AND entity_id=?""",
        (AUTONOMOUS_IBKR_PLAN_EVENT, seal.intent_id),
    )
    payload_json = canonical_json(seal.to_payload())
    if prior:
        if len(prior) == 1 and all(
            (
                prior[0]["event_id"] == seal.event_id,
                prior[0]["stream"] == bindings.account_key,
                prior[0]["occurred_at"] == seal.created_at.isoformat(),
                prior[0]["payload_json"] == payload_json,
            )
        ):
            return False
        raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_CONFLICT")
    try:
        state.append_event(
            stream=bindings.account_key,
            event_type=AUTONOMOUS_IBKR_PLAN_EVENT,
            entity_type="order_intent",
            entity_id=seal.intent_id,
            occurred_at=seal.created_at,
            payload=seal.to_payload(),
            event_id=seal.event_id,
        )
    except sqlite3.IntegrityError as exc:
        raise _deny("IBKR_AUTONOMOUS_PLAN_SEAL_CONFLICT") from exc
    return True


__all__ = [
    "AUTONOMOUS_IBKR_PLAN_EVENT",
    "AUTONOMOUS_IBKR_PLAN_SCHEMA",
    "AutonomousIbkrPlanBindings",
    "AutonomousIbkrPlanError",
    "AutonomousIbkrPlanSeal",
    "StateBackedAutonomousIbkrPlanProducer",
    "StateBackedAutonomousIbkrPlanReader",
    "autonomous_ibkr_stop_client_ref",
    "seal_autonomous_ibkr_plan",
]
