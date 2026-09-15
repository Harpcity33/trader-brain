"""Release-shipped attended IBKR command dependencies.

This module turns durable, non-secret control artifacts into the concrete
dependencies consumed by :class:`LocalProviderAssembly`.  It never creates an
approval artifact and never treats a configuration boolean as authorization.
The plan is loaded only for an exact review request.  The account-global OS
writer lock and a consumed owner activation are checked at the wire boundary,
so composition/readiness cannot dispatch an order.  Entry authority is also
bound to the current exchange trading date; safety actions are deliberately
not disabled merely because the owner activation crossed midnight.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Mapping
from zoneinfo import ZoneInfo

from .activation import ActivationRecord
from .broker.base import AccountSnapshot, BrokerSide, EquityOrderType, MarketHours, OrderRequest, TimeInForce
from .broker.ibkr_preflight import IbkrAttendedOrderPlan, IbkrOrderPurpose
from .broker.ibkr_sdk import IbkrWriteEvidence
from .local_assembly import IbkrCommandAssemblyInputs
from .money import from_cents
from .policy import PolicyBundle, canonical_json
from .risk_runtime import dollar_headroom_capacity, daily_starting_equity_capacity, entry_lifecycle_fee_reserve
from .state import SCHEMA_VERSION, object_hash
from .writer_lock import (
    AccountWriterLock,
    WriterLockBusy,
    attended_coordinator_lock_key,
    user_account_writer_lock_directory,
)


PLAN_SCHEMA = "titan_ibkr_attended_plan_artifact_2026-09-14_v1"
AUTHORITY_SCHEMA = "titan_ibkr_command_authority_artifact_2026-09-14_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 1024 * 1024


class IbkrCommandInputError(RuntimeError):
    pass


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IbkrCommandInputError(f"IBKR_COMMAND_{field.upper()}_INVALID")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise IbkrCommandInputError(f"IBKR_COMMAND_{field.upper()}_INVALID") from exc
    return _utc(parsed, field)


def _artifact(path: Path, *, schema: str) -> Mapping[str, object]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_BYTES
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_ARTIFACT_UNSAFE")
        raw = json.loads(path.read_bytes())
    except IbkrCommandInputError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IbkrCommandInputError("IBKR_COMMAND_ARTIFACT_UNAVAILABLE") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != schema:
        raise IbkrCommandInputError("IBKR_COMMAND_ARTIFACT_SCHEMA_INVALID")
    digest = raw.get("artifact_hash")
    body = dict(raw)
    body.pop("artifact_hash", None)
    expected = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    if not isinstance(digest, str) or digest != expected:
        raise IbkrCommandInputError("IBKR_COMMAND_ARTIFACT_HASH_INVALID")
    return raw


def _request(raw: object) -> OrderRequest:
    expected = {
        "account_masked", "symbol", "side", "order_type", "quantity",
        "market_hours", "time_in_force", "client_ref_id", "limit_price",
        "stop_price",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise IbkrCommandInputError("IBKR_COMMAND_PLAN_REQUEST_INVALID")
    try:
        return OrderRequest(
            account_masked=str(raw["account_masked"]),
            symbol=str(raw["symbol"]),
            side=BrokerSide(str(raw["side"])),
            order_type=EquityOrderType(str(raw["order_type"])),
            quantity=raw["quantity"],
            market_hours=MarketHours(str(raw["market_hours"])),
            time_in_force=TimeInForce(str(raw["time_in_force"])),
            client_ref_id=str(raw["client_ref_id"]),
            limit_price=raw["limit_price"],
            stop_price=raw["stop_price"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IbkrCommandInputError("IBKR_COMMAND_PLAN_REQUEST_INVALID") from exc


class DurableIbkrPlanReader:
    """Load one exact immutable plan only when its request is reviewed."""

    def __init__(self, *, path: Path, policy: PolicyBundle, clock) -> None:
        self.path = Path(path)
        self.policy = policy
        self._clock = clock

    def __call__(self, request: OrderRequest) -> IbkrAttendedOrderPlan:
        raw = _artifact(self.path, schema=PLAN_SCHEMA)
        expected = {
            "schema_version", "artifact_hash", "plan_id", "config_hash",
            "policy_hash", "account_key", "account_masked", "created_at",
            "expires_at", "purpose", "request", "structural_stop", "targets",
            "execution_reserve", "fee_reserve", "required_stop_request",
        }
        if set(raw) != expected:
            raise IbkrCommandInputError("IBKR_COMMAND_PLAN_FIELDS_INVALID")
        now = _utc(self._clock(), "clock")
        if (
            raw["config_hash"] != self.policy.config_hash
            or raw["policy_hash"] != self.policy.policy_hash
            or raw["account_key"] != self.policy.account_key
            or raw["account_masked"] != f"****{self.policy.account_last4}"
            or not _SHA256.fullmatch(str(raw["plan_id"]))
            or not _parse_time(raw["created_at"], "plan_created") <= now
            < _parse_time(raw["expires_at"], "plan_expiry")
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_PLAN_BINDING_OR_EXPIRY_INVALID")
        exact = _request(raw["request"])
        if exact.exact_tuple != request.exact_tuple:
            raise IbkrCommandInputError("IBKR_COMMAND_PLAN_REQUEST_MISMATCH")
        stop_raw = raw["required_stop_request"]
        stop = None if stop_raw is None else _request(stop_raw)
        try:
            return IbkrAttendedOrderPlan(
                plan_id=str(raw["plan_id"]),
                purpose=IbkrOrderPurpose(str(raw["purpose"])),
                request=exact,
                structural_stop=raw["structural_stop"],
                targets=tuple(raw["targets"]),
                execution_reserve=Decimal(str(raw["execution_reserve"])),
                fee_reserve=Decimal(str(raw["fee_reserve"])),
                required_stop_request=stop,
            )
        except (TypeError, ValueError) as exc:
            raise IbkrCommandInputError("IBKR_COMMAND_PLAN_GEOMETRY_INVALID") from exc


class DurableIbkrRiskPolicyCheck:
    """Join broker-current P&L with durable latches and reservations."""

    def __init__(self, *, state_path: Path, policy: PolicyBundle) -> None:
        self.state_path = Path(state_path)
        self.policy = policy

    def __call__(
        self, snapshot: AccountSnapshot, plan: IbkrAttendedOrderPlan, now: datetime
    ) -> None:
        current = _utc(now, "risk_time")
        if plan.purpose is not IbkrOrderPurpose.ENTRY:
            return
        if not snapshot.daily_realized_pnl_ready or snapshot.daily_realized_pnl is None:
            raise IbkrCommandInputError("IBKR_COMMAND_DAILY_PNL_UNAVAILABLE")
        if self.policy.daily_starting_equity_risk:
            if not snapshot.daily_starting_equity_ready:
                raise IbkrCommandInputError("IBKR_COMMAND_DAILY_STARTING_EQUITY_UNPROVEN")
        else:
            daily_lock = Decimal(str(self.policy.config["risk"]["daily_realized_loss_lock_dollars"]))
            if snapshot.daily_realized_pnl <= -daily_lock:
                raise IbkrCommandInputError("IBKR_COMMAND_DAILY_LOSS_LOCKED")
        dollar_snapshot = None
        try:
            connection = sqlite3.connect(
                f"file:{self.state_path}?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            if self.policy.account_day_headroom_risk:
                # Latches, candidate identity, and every possible exposure
                # must come from one read-only durable-state snapshot.
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                if connection.execute(
                    "SELECT 1 FROM incidents WHERE account_key=? AND category=? "
                    "AND resolved_at IS NULL LIMIT 1",
                    (self.policy.account_key, "IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED"),
                ).fetchone() is not None:
                    raise IbkrCommandInputError("IBKR_COMMAND_RISK_OBSERVATION_RECOVERY_REQUIRED")
            trading_date = current.astimezone(
                ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
            ).date()
            latch = connection.execute(
                "SELECT * FROM session_latches WHERE account_key=? AND trading_date=?",
                (self.policy.account_key, trading_date.isoformat()),
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchone()
            reservation = connection.execute(
                """SELECT r.*,p.limit_price,p.structural_stop,p.quantity,p.policy_hash,
                          p.config_hash,p.expires_at,p.state AS plan_state,
                          i.order_tuple_json,i.state AS intent_state
                     FROM risk_reservations r JOIN plans p ON p.plan_id=r.plan_id
                     JOIN order_intents i ON i.plan_id=p.plan_id
                    WHERE p.plan_id=? AND i.client_ref=? AND i.kind='ENTRY'""",
                (plan.plan_id, plan.request.client_ref_id),
            ).fetchall()
            if self.policy.account_day_headroom_risk and len(reservation) == 1:
                dollar_snapshot = self._dollar_account_risk(
                    connection, snapshot, plan, current
                )
        except sqlite3.Error as exc:
            raise IbkrCommandInputError("IBKR_COMMAND_DURABLE_RISK_UNAVAILABLE") from exc
        finally:
            if "connection" in locals():
                connection.close()
        if (
            runtime is None
            or not int(runtime["authority_enabled"])
            or runtime["runtime_id"] != self.policy.runtime_id
            or runtime["account_key"] != self.policy.account_key
            or runtime["config_hash"] != self.policy.config_hash
            or runtime["policy_hash"] != self.policy.policy_hash
            or runtime["mode"] != "ACTIVE"
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_ENTRY_RUNTIME_NOT_ACTIVE")
        if latch is None or any(
            int(latch[name])
            for name in ("loss_locked", "pause_new_entries", "closeout_started", "hard_kill")
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_SESSION_RISK_LATCH_BLOCKED")
        if len(reservation) != 1:
            raise IbkrCommandInputError("IBKR_COMMAND_RISK_RESERVATION_NOT_UNIQUE")
        row = reservation[0]
        expected_order_tuple = {
            "account_key": self.policy.account_key,
            "account_masked": plan.request.account_masked,
            "symbol": plan.request.symbol,
            "side": plan.request.side.value,
            "order_type": plan.request.order_type.value,
            "quantity": plan.request.quantity,
            "market_hours": plan.request.market_hours.value,
            "time_in_force": plan.request.time_in_force.value,
            "limit_price": (
                format(plan.request.limit_price, "f")
                if plan.request.limit_price is not None
                else None
            ),
            "stop_price": (
                format(plan.request.stop_price, "f")
                if plan.request.stop_price is not None
                else None
            ),
            "client_ref_id": plan.request.client_ref_id,
        }
        try:
            durable_order_tuple = json.loads(str(row["order_tuple_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise IbkrCommandInputError("IBKR_COMMAND_DURABLE_RISK_MISMATCH") from exc
        planned = (plan.request.limit_price - plan.structural_stop) * plan.request.quantity
        notional = plan.request.limit_price * plan.request.quantity
        if (
            row["policy_hash"] != self.policy.policy_hash
            or row["config_hash"] != self.policy.config_hash
            or row["plan_state"] != "VALIDATED"
            or row["state"] not in ("RESERVED", "BOUND")
            or row["intent_state"] != "PREPARED"
            or durable_order_tuple != expected_order_tuple
            or _parse_time(row["expires_at"], "durable_plan_expiry") < current
            or Decimal(str(row["limit_price"])) != plan.request.limit_price
            or Decimal(str(row["structural_stop"])) != plan.structural_stop
            or int(row["quantity"]) != plan.request.quantity
            or from_cents(int(row["planned_risk_cents"])) != planned
            or from_cents(int(row["execution_reserve_cents"])) != plan.execution_reserve
            or from_cents(int(row["stress_risk_cents"])) < plan.stress_downside
            or from_cents(int(row["notional_cents"])) != notional
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_DURABLE_RISK_MISMATCH")
        if self.policy.account_day_headroom_risk:
            if (
                dollar_snapshot is None
                or plan.fee_reserve != entry_lifecycle_fee_reserve(
                    self.policy, quantity=plan.request.quantity
                )
                or from_cents(int(row["stress_risk_cents"])) != plan.stress_downside
            ):
                raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_RISK_FEE_MISMATCH")
            exposures = dollar_snapshot.exposures
            candidate_exposures = tuple(
                item for item in exposures
                if item.reference == f"reservation:{row['reservation_id']}"
            )
            if (
                len(candidate_exposures) != 1
                or candidate_exposures[0].stress_risk != plan.stress_downside
            ):
                raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_CANDIDATE_EXPOSURE_MISMATCH")
            if (
                not dollar_snapshot.account_active
                or dollar_snapshot.restricted
                or dollar_snapshot.usable_equity <= 0
                or any(item.category == "unknown" for item in exposures)
                or any(
                    item.category in {"open", "manual"} and not item.protected
                    for item in exposures
                )
            ):
                raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_EXPOSURE_UNRESOLVED")
            if self.policy.daily_starting_equity_risk:
                capacity = daily_starting_equity_capacity(
                    self.policy, starting_equity=snapshot.daily_starting_equity,
                    total_equity=snapshot.funds.total_value,
                    external_cash_flow=snapshot.daily_external_cash_flow,
                )
            else:
                capacity = dollar_headroom_capacity(
                    self.policy, realized_pnl=dollar_snapshot.daily_realized_pnl,
                    profit_goal_crossed=bool(int(latch["objective_crossed"])),
                )
            # The exact candidate reservation is already in this collection.
            # Count it once alongside every open/pending/unresolved obligation,
            # including execution and lifecycle-fee reserves.
            downside = sum((item.stress_risk for item in exposures), Decimal("0"))
            if downside > capacity:
                raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_HEADROOM_EXCEEDED")
            funds_reserved = sum(
                (item.notional + item.fee_reserve for item in exposures),
                Decimal("0"),
            )
            if funds_reserved > min(
                dollar_snapshot.cash, dollar_snapshot.unleveraged_buying_power
            ):
                raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_UNLEVERAGED_FUNDS_EXCEEDED")
        if not self.policy.daily_starting_equity_risk and int(latch["objective_crossed"]):
            floor = Decimal(str(self.policy.config["risk"]["post_goal_floor_dollars"]))
            if snapshot.daily_realized_pnl - plan.stress_downside < floor:
                raise IbkrCommandInputError("IBKR_COMMAND_POST_GOAL_FLOOR_BLOCKED")

    def _dollar_account_risk(self, connection, snapshot, plan, now):
        """Rebuild the final broker observation without opening a writable store."""
        from .pipeline import build_account_risk_snapshot

        class QueryOnlyState:
            def rows(self, statement, parameters=()):
                return connection.execute(statement, parameters).fetchall()

        if (
            not isinstance(snapshot, AccountSnapshot)
            or not snapshot.authenticated_entry_risk_evidence_ready
            or (self.policy.daily_starting_equity_risk and not snapshot.daily_starting_equity_ready)
            or snapshot.account_masked not in {
                f"****{self.policy.account_last4}", f"••••{self.policy.account_last4}"
            }
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_RISK_EVIDENCE_UNPROVEN")
        normalized = replace(snapshot, account_masked=f"••••{self.policy.account_last4}")
        rebuilt, failures = build_account_risk_snapshot(
            policy=self.policy,
            state=QueryOnlyState(),
            broker_snapshot=normalized,
            now=now,
            prices={plan.request.symbol: plan.request.limit_price},
        )
        if (
            rebuilt is None
            or failures
            or rebuilt.observed_at > now
            or not all((
                rebuilt.standard_orders_reconciled,
                rebuilt.option_orders_reconciled,
                rebuilt.advanced_orders_reconciled,
                rebuilt.positions_reconciled,
            ))
        ):
            raise IbkrCommandInputError("IBKR_COMMAND_DOLLAR_ACCOUNT_RISK_INCOMPLETE")
        return rebuilt


class DurableIbkrAcceptanceVerifier:
    """Re-read the separate expiring provider/owner authority artifact."""

    def __init__(self, *, path: Path, expected: IbkrWriteEvidence, policy: PolicyBundle, release_manifest_hash: str, clock) -> None:
        self.path = Path(path)
        self.expected = expected
        self.policy = policy
        self.release_manifest_hash = release_manifest_hash
        self._clock = clock

    def __call__(self, evidence: IbkrWriteEvidence) -> None:
        if evidence != self.expected:
            raise IbkrCommandInputError("IBKR_COMMAND_WRITE_EVIDENCE_CHANGED")
        loaded = _load_write_evidence(
            self.path,
            policy=self.policy,
            release_manifest_hash=self.release_manifest_hash,
            clock=self._clock,
        )
        if loaded != evidence:
            raise IbkrCommandInputError("IBKR_COMMAND_WRITE_EVIDENCE_CHANGED")


class OwnedIbkrWriterInterlock:
    """Serialize command sessions and require a live attended coordinator."""

    def __init__(
        self,
        *,
        lock: AccountWriterLock,
        coordinator_probe: AccountWriterLock,
        state_path: Path,
        policy: PolicyBundle,
        release_manifest_hash: str,
        clock,
    ) -> None:
        if lock.path == coordinator_probe.path:
            raise ValueError("IBKR writer and coordinator locks must be distinct")
        self.lock = lock
        self.coordinator_probe = coordinator_probe
        self.state_path = Path(state_path)
        self.policy = policy
        self.release_manifest_hash = release_manifest_hash
        self._clock = clock

    def release_components(self):
        return (
            ("ibkr_account_writer_lock", self.lock, ("acquire", "refresh", "release")),
            (
                "ibkr_attended_coordinator_probe",
                self.coordinator_probe,
                ("acquire", "holder_metadata", "release"),
            ),
        )

    def __call__(self) -> None:
        acquired_here = False
        if not self.lock.held:
            self.lock.acquire(blocking=False)
            acquired_here = True
        now = _utc(self._clock(), "interlock_time")
        try:
            connection = sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            runtime = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchone()
            if runtime is None or not int(runtime["authority_enabled"]):
                raise IbkrCommandInputError("IBKR_COMMAND_ACTIVATION_REQUIRED")
            lease = connection.execute(
                "SELECT * FROM account_writer_lease WHERE account_key=?",
                (self.policy.account_key,),
            ).fetchone()
            activated_at = runtime["activated_at"]
            rows = connection.execute(
                "SELECT * FROM activation_records WHERE account_key=? AND consumed_at=?",
                (self.policy.account_key, activated_at),
            ).fetchall()
        except sqlite3.Error as exc:
            if acquired_here:
                self.lock.release()
            raise IbkrCommandInputError("IBKR_COMMAND_ACTIVATION_UNAVAILABLE") from exc
        except Exception:
            if acquired_here:
                self.lock.release()
            raise
        finally:
            if "connection" in locals():
                connection.close()
        try:
            try:
                self.coordinator_probe.acquire(blocking=False)
            except WriterLockBusy:
                coordinator_holder = self.coordinator_probe.holder_metadata()
            else:
                self.coordinator_probe.release()
                raise IbkrCommandInputError(
                    "IBKR_COMMAND_ATTENDED_COORDINATOR_NOT_RUNNING"
                )
            if len(rows) != 1:
                raise IbkrCommandInputError("IBKR_COMMAND_ACTIVATION_NOT_UNIQUE")
            row = rows[0]
            try:
                raw = json.loads(str(row["record_json"]))
                record = ActivationRecord.from_payload(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise IbkrCommandInputError("IBKR_COMMAND_ACTIVATION_INVALID") from exc
            allowed_modes = {
                "RECONCILING",
                "ACTIVE",
                "PAUSE_NEW_ENTRIES",
                "MANAGED_CLOSEOUT",
                "INCIDENT",
            }
            maximum_lease_age = max(
                10.0,
                float(
                    self.policy.config["execution"]["reconcile_interval_seconds"]
                )
                * 3.0,
            )
            lease_heartbeat = _parse_time(lease["heartbeat_at"], "writer_heartbeat")
            if (
                runtime["runtime_id"] != self.policy.runtime_id
                or runtime["account_key"] != self.policy.account_key
                or runtime["release_manifest_hash"] != self.release_manifest_hash
                or runtime["config_hash"] != self.policy.config_hash
                or runtime["policy_hash"] != self.policy.policy_hash
                or runtime["mode"] not in allowed_modes
                or lease is None
                or lease["owner_id"] != "attended-read-coordinator"
                or lease["released_at"] is not None
                or coordinator_holder.get("owner_id")
                != "attended-read-coordinator"
                or coordinator_holder.get("account_fingerprint")
                != self.coordinator_probe.account_fingerprint
                or int(coordinator_holder.get("pid", 0))
                != int(lease["process_id"])
                or (now - lease_heartbeat).total_seconds() < -1
                or (now - lease_heartbeat).total_seconds() > maximum_lease_age
                or str(row["record_hash"]) != object_hash(raw)
                or record.activation_id != str(row["activation_id"])
                or record.recomputed_activation_id() != record.activation_id
                or record.release_manifest_hash != self.release_manifest_hash
                or record.config_hash != self.policy.config_hash
                or record.policy_hash != self.policy.policy_hash
                or record.account_key != self.policy.account_key
                or record.database_schema_version != SCHEMA_VERSION
                or record.readiness_evidence.execution_authority_mode
                != "attended_only"
                or record.readiness_evidence.attended_mutation_supported is not True
                or record.readiness_evidence.unattended_mutation_supported is not False
                or record.readiness_evidence.per_mutation_confirmation_required
                is not True
                or _parse_time(activated_at, "activation_time")
                .astimezone(
                    ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
                )
                .date()
                != now.astimezone(
                    ZoneInfo(str(self.policy.config["sessions"]["timezone"]))
                ).date()
            ):
                raise IbkrCommandInputError(
                    "IBKR_COMMAND_ACTIVATION_BINDING_INVALID"
                )
            self.lock.refresh()
        except (TypeError, KeyError, IndexError, ValueError):
            if acquired_here:
                self.lock.release()
            raise IbkrCommandInputError(
                "IBKR_COMMAND_COORDINATOR_LEASE_INVALID"
            ) from None
        except Exception:
            if acquired_here:
                self.lock.release()
            raise

    def close(self) -> None:
        self.lock.release()


def _load_write_evidence(path: Path, *, policy: PolicyBundle, release_manifest_hash: str, clock) -> IbkrWriteEvidence:
    raw = _artifact(path, schema=AUTHORITY_SCHEMA)
    expected_fields = {
        "schema_version", "artifact_hash", "release_manifest_hash", "config_hash",
        "policy_hash", "account_key", "account_masked", "authorization_binding_id",
        "account_binding_fingerprint", "provider_contract_id", "environment",
        "client_id", "issued_at", "expires_at",
    }
    if set(raw) != expected_fields or any(
        (
            raw["release_manifest_hash"] != release_manifest_hash,
            raw["config_hash"] != policy.config_hash,
            raw["policy_hash"] != policy.policy_hash,
            raw["account_key"] != policy.account_key,
            raw["account_masked"] != f"****{policy.account_last4}",
        )
    ):
        raise IbkrCommandInputError("IBKR_COMMAND_AUTHORITY_BINDING_INVALID")
    evidence = IbkrWriteEvidence(
        authorization_binding_id=str(raw["authorization_binding_id"]),
        account_binding_fingerprint=str(raw["account_binding_fingerprint"]),
        environment=str(raw["environment"]),
        client_id=int(raw["client_id"]),
        reviewed_contract_id=str(raw["provider_contract_id"]),
        issued_at=_parse_time(raw["issued_at"], "authority_issued"),
        expires_at=_parse_time(raw["expires_at"], "authority_expiry"),
    )
    now = _utc(clock(), "clock")
    if not evidence.issued_at <= now < evidence.expires_at:
        raise IbkrCommandInputError("IBKR_COMMAND_AUTHORITY_EXPIRED")
    return evidence


def build_release_bound_ibkr_command_inputs(*, release_root: Path, install_root: Path, full_live_config_name: str, release_manifest_hash: str, clock) -> IbkrCommandAssemblyInputs:
    """Build real dependencies only from a signed supported attended policy."""

    policy = PolicyBundle.load(
        release_root, config_relative=f"config/{full_live_config_name}"
    )
    execution = policy.config["execution"]
    if (
        execution.get("broker_adapter") != "supported_production_transport"
        or execution.get("execution_authority_mode") != "attended_only"
    ):
        raise IbkrCommandInputError("IBKR_COMMAND_SUPPORTED_ATTENDED_POLICY_REQUIRED")
    if not _SHA256.fullmatch(str(release_manifest_hash)):
        raise IbkrCommandInputError("IBKR_COMMAND_RELEASE_BINDING_INVALID")
    control = Path(install_root) / "control/ibkr"
    authority_path = control / "attended-command-authority.json"
    plan_path = control / "attended-plan.json"
    state_path = Path(install_root) / "state/full-live.sqlite3"
    evidence = _load_write_evidence(
        authority_path,
        policy=policy,
        release_manifest_hash=release_manifest_hash,
        clock=clock,
    )
    if (
        evidence.authorization_binding_id
        != execution.get("production_authorization_binding_id")
        or evidence.account_binding_fingerprint
        != execution.get("production_account_binding_fingerprint")
        or evidence.reviewed_contract_id != execution.get("ibkr_provider_contract_id")
    ):
        raise IbkrCommandInputError("IBKR_COMMAND_SIGNED_EXECUTION_BINDING_MISMATCH")
    lock_directory = user_account_writer_lock_directory()
    lock = AccountWriterLock(
        lock_directory,
        policy.account_key,
        broker_account_binding_fingerprint=evidence.account_binding_fingerprint,
        authorization_binding_id=evidence.authorization_binding_id,
    )
    coordinator_probe = AccountWriterLock(
        lock_directory,
        attended_coordinator_lock_key(policy.account_key),
        owner_id="attended-command-coordinator-probe",
    )
    interlock = OwnedIbkrWriterInterlock(
        lock=lock,
        coordinator_probe=coordinator_probe,
        state_path=state_path,
        policy=policy,
        release_manifest_hash=release_manifest_hash,
        clock=clock,
    )
    verifier = DurableIbkrAcceptanceVerifier(
        path=authority_path,
        expected=evidence,
        policy=policy,
        release_manifest_hash=release_manifest_hash,
        clock=clock,
    )
    return IbkrCommandAssemblyInputs(
        plan_reader=DurableIbkrPlanReader(path=plan_path, policy=policy, clock=clock),
        risk_policy_check=DurableIbkrRiskPolicyCheck(state_path=state_path, policy=policy),
        acceptance_verifier=verifier,
        mutation_interlock=interlock,
        write_evidence=evidence,
    )


__all__ = [
    "AUTHORITY_SCHEMA", "PLAN_SCHEMA", "DurableIbkrAcceptanceVerifier",
    "DurableIbkrPlanReader", "DurableIbkrRiskPolicyCheck",
    "IbkrCommandInputError", "OwnedIbkrWriterInterlock",
    "build_release_bound_ibkr_command_inputs",
]
