"""Release-bound construction of the autonomous IBKR command lane.

Configuration selects a supported route but never supplies authority.  This
module joins the signed policy to a private HMAC-authenticated provider
contract, a separately keyed owner-policy/effective-pricing receipt, and the
already-existing durable runtime state.  Both receipts are verified before the
state store or account-writer lock is constructed, so missing or invalid
external provenance cannot create state or reach activation logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from typing import Mapping, Protocol
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from .broker.base import AccountSnapshot
from .broker.ibkr_sdk import IbkrWriteEvidence
from .broker.ibkr_preflight import IbkrOrderPurpose, _AUTONOMOUS_RISK_SOURCE
from .broker.ibkr_transport import IBKR_TRANSPORT_ID
from .ibkr_autonomous_authority import (
    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
    IbkrAutonomousAuthorityBindings,
    VerifiedIbkrAutonomousAuthority,
    load_verified_ibkr_autonomous_authority,
)
from .ibkr_autonomous_interlock import AutonomousIbkrWriterInterlock
from .ibkr_autonomous_policy_receipt import (
    IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
    IbkrAutonomousPolicyReceiptBindings,
    VerifiedIbkrAutonomousPolicyReceipt,
    load_verified_ibkr_autonomous_policy_receipt,
)
from .ibkr_autonomous_plans import (
    AutonomousIbkrPlanBindings,
    StateBackedAutonomousIbkrPlanProducer,
)
from .ibkr_command_inputs import DurableIbkrRiskPolicyCheck
from .local_assembly import IbkrCommandAssemblyInputs
from .models import Incident, IncidentSeverity, SessionLatch as DurableSessionLatch
from .money import from_cents
from .policy import PolicyBundle
from .provider_clients import KeychainItem, MacOSKeychain
from .provider_profile import IbkrLocalProviderProfile
from .risk_runtime import SessionLatch, update_session_latch
from .state import LiveStateStore, SCHEMA_VERSION, StateConflict
from .writer_lock import AccountWriterLock, user_account_writer_lock_directory


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ERROR = re.compile(r"IBKR_AUTONOMOUS_INPUT_[A-Z0-9_]{1,96}\Z")
_RISK_OBSERVATION_PENDING = "IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED"


class IbkrAutonomousInputError(RuntimeError):
    """Stable, redacted autonomous-input construction failure."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if _ERROR.fullmatch(normalized) is None:
            raise ValueError("invalid autonomous IBKR input error code")
        self.code = normalized
        super().__init__(normalized)


class KeychainReader(Protocol):
    def read(self, item: KeychainItem) -> bytes: ...


def _failure(code: str) -> IbkrAutonomousInputError:
    return IbkrAutonomousInputError(f"IBKR_AUTONOMOUS_INPUT_{code}")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _failure("CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _policy_bindings(
    policy: PolicyBundle, release_manifest_hash: str
) -> IbkrAutonomousAuthorityBindings:
    execution = policy.config["execution"]
    try:
        return IbkrAutonomousAuthorityBindings(
            release_manifest_hash=release_manifest_hash,
            config_hash=policy.config_hash,
            policy_binding_id=policy.policy_hash,
            account_key=policy.account_key,
            account_masked=f"****{policy.account_last4}",
            account_binding_fingerprint=(
                execution["production_account_binding_fingerprint"]
            ),
            authorization_binding_id=(
                execution["production_authorization_binding_id"]
            ),
            provider_contract_id=execution["ibkr_provider_contract_id"],
            transport_id=execution["production_transport_id"],
            api_name=execution["ibkr_autonomous_api_name"],
            api_version=execution["ibkr_autonomous_api_version"],
            environment=execution["ibkr_autonomous_environment"],
            client_id=execution["ibkr_autonomous_client_id"],
        )
    except Exception:
        raise _failure("AUTHORITY_BINDING_INVALID") from None


def _positive_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise _failure(f"{field.upper()}_INVALID")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _failure(f"{field.upper()}_INVALID") from None
    if not parsed.is_finite() or parsed <= 0:
        raise _failure(f"{field.upper()}_INVALID")
    return parsed


def _policy_receipt_bindings(
    policy: PolicyBundle,
    release_manifest_hash: str,
) -> IbkrAutonomousPolicyReceiptBindings:
    try:
        execution = policy.config["execution"]
        quality = policy.config["evidence"]
        exits = policy.config["exits"]
        return IbkrAutonomousPolicyReceiptBindings(
            release_manifest_hash=release_manifest_hash,
            config_hash=policy.config_hash,
            policy_hash=policy.policy_hash,
            risk_hash=policy.risk_hash,
            account_key=policy.account_key,
            account_masked=f"****{policy.account_last4}",
            account_binding_fingerprint=(
                execution["production_account_binding_fingerprint"]
            ),
            authorization_binding_id=(
                execution["production_authorization_binding_id"]
            ),
            provider_contract_id=execution["ibkr_provider_contract_id"],
            transport_id=execution["production_transport_id"],
            max_spread_bps=_positive_decimal(
                quality["max_spread_bps"], "max_spread_bps"
            ),
            spread_denominator=quality["spread_denominator"],
            minimum_depth_multiple=_positive_decimal(
                quality["minimum_depth_multiple"], "minimum_depth_multiple"
            ),
            depth_source=quality["depth_source"],
            quote_size_unit=quality["quote_size_unit"],
            target_exit_mode=exits["target_exit_mode"],
            target_index=exits["target_index"],
            target_trigger=exits["target_trigger"],
            target_quantity=exits["quantity"],
            cancel_working_sells_before_exit=exits[
                "cancel_working_sells_before_exit"
            ],
            require_strictly_newer_cancel_evidence=exits[
                "require_strictly_newer_cancel_evidence"
            ],
            deadline_feasibility_gate=exits["deadline_feasibility_gate"],
            minimum_commission_reserve_per_order_dollars=_positive_decimal(
                execution["minimum_commission_reserve_per_order_dollars"],
                "minimum_commission_reserve_per_order_dollars",
            ),
        )
    except IbkrAutonomousInputError:
        raise
    except Exception:
        raise _failure("POLICY_RECEIPT_BINDING_INVALID") from None


class DurableIbkrAutonomousAcceptanceVerifier:
    """Re-authenticate renewable authority plus policy at every write edge.

    The owner-policy receipt and durable activation remain pinned.  A newer
    HMAC-authenticated provider-authority observation may replace an expiring
    one only when every stable release/account/transport binding is identical.
    This gives a continuously owned runtime a safe next-session refresh path
    without treating provider evidence rotation as a new owner activation.
    """

    def __init__(
        self,
        *,
        path: Path,
        keychain: KeychainReader,
        key_item: KeychainItem,
        bindings: IbkrAutonomousAuthorityBindings,
        authority: VerifiedIbkrAutonomousAuthority,
        policy_receipt_path: Path,
        policy_receipt_key_item: KeychainItem,
        policy_receipt_bindings: IbkrAutonomousPolicyReceiptBindings,
        policy_receipt: VerifiedIbkrAutonomousPolicyReceipt,
        expected_evidence: IbkrWriteEvidence,
        clock,
    ) -> None:
        if not isinstance(authority, VerifiedIbkrAutonomousAuthority):
            raise _failure("ACCEPTANCE_AUTHORITY_INVALID")
        if type(bindings) is not IbkrAutonomousAuthorityBindings:
            raise _failure("ACCEPTANCE_BINDINGS_INVALID")
        if not isinstance(policy_receipt, VerifiedIbkrAutonomousPolicyReceipt):
            raise _failure("ACCEPTANCE_POLICY_RECEIPT_INVALID")
        if type(policy_receipt_bindings) is not IbkrAutonomousPolicyReceiptBindings:
            raise _failure("ACCEPTANCE_POLICY_BINDINGS_INVALID")
        if not isinstance(expected_evidence, IbkrWriteEvidence):
            raise _failure("ACCEPTANCE_EVIDENCE_INVALID")
        if not callable(clock) or not callable(getattr(keychain, "read", None)):
            raise _failure("ACCEPTANCE_DEPENDENCY_INVALID")
        self.path = Path(path)
        self.keychain = keychain
        self.key_item = key_item
        self.bindings = bindings
        self._authority_hash = authority.authority_hash
        self._authority_issued_at = authority.issued_at
        self._authority_lock = RLock()
        self.policy_receipt_path = Path(policy_receipt_path)
        self.policy_receipt_key_item = policy_receipt_key_item
        self.policy_receipt_bindings = policy_receipt_bindings
        self.policy_receipt_hash = policy_receipt.receipt_hash
        self.expected_evidence = expected_evidence
        self._clock = clock

    @property
    def authority_hash(self) -> str:
        with self._authority_lock:
            return self._authority_hash

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory the retained private-key loader without duplicating clocks.

        The shared autonomous clock is already owned by the durable plan-reader
        graph.  Re-enumerating that same callable under a second role would
        correctly violate the release graph's one-object/one-owner invariant.
        """

        return (("ibkr_autonomous_receipt_key_loader", self.keychain, ("read",)),)

    def _secret(
        self,
        item: KeychainItem,
        unavailable_failure: str,
        invalid_failure: str,
    ) -> bytes:
        try:
            secret = self.keychain.read(item)
        except Exception:
            raise _failure(unavailable_failure) from None
        if type(secret) is not bytes or len(secret) < 32:
            secret = b""
            raise _failure(invalid_failure)
        return secret

    def verify_policy_receipt(self) -> None:
        """Re-read policy and pricing evidence for every risk acceptance."""

        secret = self._secret(
            self.policy_receipt_key_item,
            "POLICY_RECEIPT_KEYCHAIN_UNAVAILABLE",
            "POLICY_RECEIPT_KEYCHAIN_SECRET_INVALID",
        )
        try:
            refreshed = load_verified_ibkr_autonomous_policy_receipt(
                self.policy_receipt_path,
                secret=secret,
                expected=self.policy_receipt_bindings,
                now=_utc(self._clock()),
            )
        except IbkrAutonomousInputError:
            raise
        except Exception:
            raise _failure("POLICY_RECEIPT_NOT_CURRENT") from None
        finally:
            secret = b""
        if refreshed.receipt_hash != self.policy_receipt_hash:
            raise _failure("POLICY_RECEIPT_CHANGED")
        return None

    def __call__(self, evidence: IbkrWriteEvidence) -> None:
        if not isinstance(evidence, IbkrWriteEvidence) or (
            evidence != self.expected_evidence
        ):
            raise _failure("WRITE_EVIDENCE_MISMATCH")
        with self._authority_lock:
            secret = self._secret(
                self.key_item,
                "KEYCHAIN_UNAVAILABLE",
                "KEYCHAIN_SECRET_INVALID",
            )
            try:
                refreshed = load_verified_ibkr_autonomous_authority(
                    self.path,
                    secret=secret,
                    expected=self.bindings,
                    now=_utc(self._clock()),
                )
            except IbkrAutonomousInputError:
                raise
            except Exception:
                raise _failure("AUTHORITY_NOT_CURRENT") from None
            finally:
                # The immutable bytes cannot be zeroized in place, but the
                # verifier never retains or exports the object.
                secret = b""
            if refreshed.issued_at < self._authority_issued_at:
                raise _failure("AUTHORITY_ROLLBACK")
            if (
                refreshed.issued_at == self._authority_issued_at
                and refreshed.authority_hash != self._authority_hash
            ):
                raise _failure("AUTHORITY_EQUIVOCATION")
            # Validate the separately keyed, pinned owner policy before
            # committing a provider-observation rotation in memory.
            self.verify_policy_receipt()
            self._authority_hash = refreshed.authority_hash
            self._authority_issued_at = refreshed.issued_at
        return None


class DurableIbkrAutonomousRiskPolicyCheck:
    """Require owner-policy evidence for entries, never reduce-only actions."""

    def __init__(self, *, delegate, receipt_verifier, session_latch_interlock=None) -> None:
        if not callable(delegate) or not isinstance(
            receipt_verifier, DurableIbkrAutonomousAcceptanceVerifier
        ):
            raise _failure("RISK_POLICY_DEPENDENCY_INVALID")
        self.delegate = delegate
        self.receipt_verifier = receipt_verifier
        if session_latch_interlock is not None and (
            not isinstance(session_latch_interlock, AutonomousIbkrWriterInterlock)
            or not isinstance(delegate, DurableIbkrRiskPolicyCheck)
            or delegate.policy is not session_latch_interlock.policy
            or delegate.state_path != session_latch_interlock.state_path
        ):
            raise _failure("RISK_OBSERVER_BINDING_INVALID")
        self._session_latch_interlock = session_latch_interlock
        self._pending_observations: dict[str, datetime] = {}
        self._last_observation = None
        self._observation_storage_failed = False
        self._activation_lineage_hash: str | None = None
        self._activation_peak_floor: Decimal | None = None
        self._activation_lock = RLock()

    def release_components(self):
        # The state and writer are the exact objects already inventoried by
        # the plan producer and SDK mutation interlock, not new stores/locks.
        return (("ibkr_durable_risk_policy_check", self.delegate, ("__call__",)),)

    def _dollar_policy(self):
        policy = getattr(self.delegate, "policy", None)
        return policy if isinstance(policy, PolicyBundle) and policy.dollar_headroom_risk else None

    def begin_account_snapshot(self, now, *, entry: bool):
        """Durably arm one exact broker read before it can expose risk facts.

        A crash or failed observation leaves an unresolved incident.  A later
        recovered P&L reading must never clear that earlier unknown interval.
        """
        policy = self._dollar_policy()
        if policy is None:
            return None
        with self._activation_lock:
            try:
                current = _utc(now)
                interlock = self._session_latch_interlock
                if interlock is None:
                    raise _failure("RISK_OBSERVER_UNAVAILABLE")
                interlock()
                self.receipt_verifier.verify_policy_receipt()
                if entry and (
                    self._observation_storage_failed
                    or interlock.state.rows(
                        "SELECT incident_id FROM incidents WHERE account_key=? "
                        "AND category=? AND resolved_at IS NULL",
                        (policy.account_key, _RISK_OBSERVATION_PENDING),
                    )
                ):
                    raise _failure("RISK_OBSERVATION_RECOVERY_REQUIRED")
                token = f"ibkr-risk-observation-{uuid4()}"
                if interlock.state.record_incident(Incident(
                    incident_id=token, account_key=policy.account_key,
                    category=_RISK_OBSERVATION_PENDING, severity=IncidentSeverity.CRITICAL,
                    opened_at=current,
                    detail={
                        "release_manifest_hash": interlock.release_manifest_hash,
                        "config_hash": policy.config_hash, "policy_hash": policy.policy_hash,
                        "trading_date": current.astimezone(interlock.session_timezone).date().isoformat(),
                    },
                )) is not True:
                    raise _failure("RISK_OBSERVATION_MARKER_NOT_PERSISTED")
                self._pending_observations[token] = current
                return token
            except Exception:
                # If storage itself is unavailable, risk reduction remains
                # possible, but new entries require operator recovery.  This
                # memory flag is explicitly not a crash-safe storage claim.
                self._observation_storage_failed = True
                raise _failure("RISK_OBSERVATION_BEGIN_FAILED") from None

    def observe_account_snapshot(self, snapshot, now, *, token, entry: bool) -> None:
        """Commit authenticated account-day latches before later order denials."""
        policy = self._dollar_policy()
        if policy is None:
            return None
        with self._activation_lock:
            try:
                current = _utc(now)
                interlock = self._session_latch_interlock
                if interlock is None or token not in self._pending_observations:
                    raise _failure("RISK_OBSERVATION_TOKEN_UNPROVEN")
                started = self._pending_observations[token]
                local_zone = ZoneInfo(str(policy.config["sessions"]["timezone"]))
                local_date = current.astimezone(local_zone).date()
                peak = self._check_activated_entry_snapshot(snapshot)
                maximum_age = float(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
                if (
                    snapshot.account_masked not in {f"****{policy.account_last4}", f"••••{policy.account_last4}"}
                    or snapshot.auth_point_in_time is not True
                    or not snapshot.whole_broker_reconciled
                    or not isinstance(snapshot.risk_evidence_source, str)
                    or _AUTONOMOUS_RISK_SOURCE.fullmatch(snapshot.risk_evidence_source) is None
                    or snapshot.risk_evidence_as_of is None
                    or snapshot.risk_evidence_as_of.astimezone(local_zone).date() != local_date
                    or started.astimezone(local_zone).date() != local_date
                    or not policy.calendar.is_trading_day(local_date)
                    or snapshot.funds.total_value <= 0
                    or any(
                        not 0 <= (current - stamp).total_seconds() <= maximum_age
                        for stamp in (snapshot.observed_at, snapshot.received_at, snapshot.risk_evidence_as_of, started)
                    )
                ):
                    raise _failure("RISK_OBSERVATION_EVIDENCE_INVALID")
                self.receipt_verifier.verify_policy_receipt()
                for _attempt in range(3):
                    # Authorize outside the transaction using the exact held
                    # state handle.  apply_session_latch atomically enforces
                    # monotonicity if the service observes concurrently.
                    interlock()
                    rows = interlock.state.rows(
                        "SELECT * FROM session_latches WHERE account_key=? AND trading_date=?",
                        (policy.account_key, local_date.isoformat()),
                    )
                    row = rows[0] if rows else None
                    prior = SessionLatch(
                        trading_date=local_date,
                        loss_lock=bool(row["loss_locked"]) if row else False,
                        hard_kill=bool(row["hard_kill"]) if row else False,
                        profit_goal_crossed=bool(row["objective_crossed"]) if row else False,
                        first_profit_crossed_at=(datetime.fromisoformat(row["first_objective_crossed_at"])
                            if row and row["first_objective_crossed_at"] else None),
                        highest_realized_pnl=from_cents(int(row["highest_realized_pnl_cents"])) if row else Decimal("0"),
                    )
                    updated = update_session_latch(
                        policy, prior, realized_pnl=snapshot.daily_realized_pnl,
                        usable_equity=snapshot.funds.total_value,
                        observed_at=snapshot.risk_evidence_as_of.astimezone(local_zone),
                    )
                    durable = DurableSessionLatch(
                        account_key=policy.account_key, trading_date=local_date,
                        loss_locked=updated.loss_lock, objective_crossed=updated.profit_goal_crossed,
                        pause_new_entries=bool(row and row["pause_new_entries"]) or updated.loss_lock or updated.hard_kill,
                        closeout_started=bool(row and row["closeout_started"]) or updated.hard_kill,
                        hard_kill=updated.hard_kill, highest_realized_pnl=updated.highest_realized_pnl,
                        first_objective_crossed_at=updated.first_profit_crossed_at,
                        revision=int(row["revision"]) + 1 if row else 0,
                        updated_at=max(current, datetime.fromisoformat(row["updated_at"])) if row else current,
                    )
                    try:
                        if interlock.state.apply_session_latch(durable) is True:
                            break
                    except StateConflict:
                        continue
                else:
                    raise _failure("RISK_OBSERVATION_LATCH_NOT_PERSISTED")
                self.receipt_verifier.verify_policy_receipt()
                interlock()
                if interlock.state.resolve_incident(token, resolved_at=current) is not True:
                    raise _failure("RISK_OBSERVATION_MARKER_NOT_RESOLVED")
                del self._pending_observations[token]
                self._last_observation = (snapshot, current)
                assert self._activation_peak_floor is not None
                self._activation_peak_floor = max(self._activation_peak_floor, peak)
                return None
            except Exception:
                raise _failure("RISK_OBSERVATION_PERSISTENCE_FAILED") from None

    def bind_entry_risk_activation(
        self,
        *,
        lineage_hash: str,
        minimum_peak: object,
    ) -> None:
        """One-time bind the live ENTRY edge to consumed activation evidence."""

        lineage = str(lineage_hash)
        if _SHA256.fullmatch(lineage) is None:
            raise _failure("ACTIVATION_RISK_LINEAGE_INVALID")
        try:
            peak = Decimal(str(minimum_peak))
        except (InvalidOperation, TypeError, ValueError):
            raise _failure("ACTIVATION_RISK_PEAK_INVALID") from None
        if (
            isinstance(minimum_peak, (bool, float))
            or not peak.is_finite()
            or peak <= 0
        ):
            raise _failure("ACTIVATION_RISK_PEAK_INVALID")
        with self._activation_lock:
            current = (self._activation_lineage_hash, self._activation_peak_floor)
            requested = (lineage, peak)
            if current == (None, None):
                self._activation_lineage_hash, self._activation_peak_floor = requested
            elif current != requested:
                raise _failure("ACTIVATION_RISK_BINDING_CHANGED")
        return None

    def _check_activated_entry_snapshot(self, snapshot: object) -> Decimal:
        lineage = self._activation_lineage_hash
        floor = self._activation_peak_floor
        if lineage is None or floor is None:
            raise _failure("ACTIVATION_RISK_BINDING_MISSING")
        if (
            not isinstance(snapshot, AccountSnapshot)
            or not snapshot.authenticated_entry_risk_evidence_ready
            or snapshot.risk_high_water_lineage_hash != lineage
            or snapshot.peak_equity is None
            or snapshot.peak_equity < floor
        ):
            raise _failure("ACTIVATION_RISK_EVIDENCE_CHANGED")
        return snapshot.peak_equity

    def __call__(self, snapshot, plan, now) -> None:
        try:
            purpose = IbkrOrderPurpose(plan.purpose)
        except (AttributeError, TypeError, ValueError):
            raise _failure("RISK_POLICY_PLAN_INVALID") from None
        entry = purpose is IbkrOrderPurpose.ENTRY
        if entry:
            # Serialize the evidence/floor join so one concurrent entry cannot
            # continue after another observation has advanced the in-process
            # high-water floor.  This is the actual preflight called again by
            # transport revalidation immediately before dispatch.
            with self._activation_lock:
                if self._dollar_policy() is not None and (
                    self._observation_storage_failed
                    or self._last_observation is None
                    or self._last_observation[0] is not snapshot
                    or not 0 <= (_utc(now) - self._last_observation[1]).total_seconds()
                    <= float(self._dollar_policy().config["evidence"]["broker_snapshot_max_age_seconds"])
                ):
                    raise _failure("RISK_OBSERVATION_NOT_COMMITTED")
                observed_peak = self._check_activated_entry_snapshot(snapshot)
                self.receipt_verifier.verify_policy_receipt()
                result = self.delegate(snapshot, plan, now)
                self.receipt_verifier.verify_policy_receipt()
                assert self._activation_peak_floor is not None
                self._activation_peak_floor = max(
                    self._activation_peak_floor,
                    observed_peak,
                )
        else:
            result = self.delegate(snapshot, plan, now)
        if result is not None:
            raise _failure("RISK_POLICY_MUST_RAISE_ON_DENIAL")
        return None


def _existing_state(
    path: Path,
    *,
    runtime_id: str,
    account_key: str,
    release_manifest_hash: str,
    config_hash: str,
    policy_hash: str,
) -> tuple[LiveStateStore, tuple[int, int]]:
    try:
        resolved = path.resolve(strict=True)
        before = path.lstat()
    except OSError:
        raise _failure("STATE_UNAVAILABLE") from None
    if (
        resolved != path
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise _failure("STATE_UNSAFE")
    expected = (
        runtime_id,
        account_key,
        release_manifest_hash,
        config_hash,
        policy_hash,
    )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path), safe='/')}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema = connection.execute(
            "SELECT singleton,version,applied_at FROM schema_meta"
        ).fetchall()
        runtimes = connection.execute("SELECT * FROM runtime_identity").fetchall()
        connection.rollback()
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        raise _failure("STATE_SCHEMA_OR_OPEN_INVALID") from None
    finally:
        if connection is not None:
            connection.close()
    if (
        type(version) is not int
        or version != SCHEMA_VERSION
        or len(schema) != 1
        or schema[0]["singleton"] != 1
        or type(schema[0]["version"]) is not int
        or schema[0]["version"] != SCHEMA_VERSION
        or len(runtimes) != 1
    ):
        raise _failure("STATE_SCHEMA_OR_OPEN_INVALID")
    try:
        applied_at = datetime.fromisoformat(str(schema[0]["applied_at"]))
        preflight_identity = tuple(
            runtimes[0][name]
            for name in (
                "runtime_id",
                "account_key",
                "release_manifest_hash",
                "config_hash",
                "policy_hash",
            )
        )
    except (KeyError, TypeError, ValueError):
        raise _failure("STATE_RUNTIME_BINDING_INVALID") from None
    if applied_at.tzinfo is None:
        raise _failure("STATE_SCHEMA_OR_OPEN_INVALID")
    if preflight_identity != expected:
        raise _failure("STATE_RUNTIME_BINDING_INVALID")
    store: LiveStateStore | None = None
    try:
        store = LiveStateStore(path)
        after = path.lstat()
    except Exception:
        if store is not None:
            store.close()
        raise _failure("STATE_SCHEMA_OR_OPEN_INVALID") from None
    identity = (before.st_dev, before.st_ino)
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != identity
    ):
        store.close()
        raise _failure("STATE_CHANGED")
    assert store is not None
    runtime = store.runtime_status()
    try:
        observed = (
            None
            if runtime is None
            else tuple(
                runtime[name]
                for name in (
                    "runtime_id",
                    "account_key",
                    "release_manifest_hash",
                    "config_hash",
                    "policy_hash",
                )
            )
        )
    except (KeyError, TypeError):
        store.close()
        raise _failure("STATE_RUNTIME_BINDING_INVALID") from None
    if store.schema_version != SCHEMA_VERSION or observed != expected:
        store.close()
        raise _failure("STATE_RUNTIME_BINDING_INVALID")
    return store, identity


def build_release_bound_ibkr_autonomous_inputs(
    *,
    release_root: Path,
    install_root: Path,
    full_live_config_name: str,
    release_manifest_hash: str,
    clock,
    keychain: KeychainReader | None = None,
    connect_command_session: bool = True,
    authorize_command_writes: bool | None = None,
) -> IbkrCommandAssemblyInputs:
    """Build autonomous dependencies only after exact external evidence.

    This function neither creates nor consumes an activation and never
    acquires the writer lock.  The service runner remains the sole owner of
    both the exact returned lock and its durable lease.
    """

    if (
        type(full_live_config_name) is not str
        or not full_live_config_name
        or Path(full_live_config_name).name != full_live_config_name
        or Path(full_live_config_name).suffix != ".json"
    ):
        raise _failure("CONFIG_NAME_INVALID")
    if type(release_manifest_hash) is not str or _SHA256.fullmatch(
        release_manifest_hash
    ) is None:
        raise _failure("RELEASE_BINDING_INVALID")
    if not callable(clock):
        raise _failure("CLOCK_INVALID")
    if type(connect_command_session) is not bool:
        raise _failure("COMMAND_SESSION_SELECTION_INVALID")
    if authorize_command_writes is None:
        authorize_command_writes = connect_command_session
    if (
        type(authorize_command_writes) is not bool
        or authorize_command_writes and not connect_command_session
    ):
        raise _failure("COMMAND_WRITE_AUTHORITY_SELECTION_INVALID")

    try:
        policy = PolicyBundle.load(
            Path(release_root),
            config_relative=f"config/{full_live_config_name}",
        )
    except Exception:
        raise _failure("POLICY_INVALID") from None
    execution = policy.config["execution"]
    sessions = policy.config.get("sessions")
    account_policy = policy.config.get("account")
    risk = policy.config.get("risk")
    if (
        not isinstance(execution, Mapping)
        or not isinstance(sessions, Mapping)
        or not isinstance(account_policy, Mapping)
        or not isinstance(risk, Mapping)
        or execution.get("broker_adapter") != "supported_production_transport"
        or policy.execution_authority_mode != "unattended"
        or execution.get("production_transport_id") != IBKR_TRANSPORT_ID
        or execution.get("supported_unattended_mutation") is not True
        or execution.get("per_mutation_user_confirmation_required") is not False
        or execution.get("local_mutation_interlock_enabled") is not True
        or execution.get("one_account_writer_required") is not True
        or execution.get("durable_intent_before_submit") is not True
        or execution.get("automatic_retry_unknown_submission") is not False
        or sessions.get("premarket_mode") != "analysis_only"
        or sessions.get("premarket_orders_enabled") is not False
        or account_policy.get("allowed_type") != "no_borrow_margin"
        or account_policy.get("margin_debit_allowed") is not False
        or not policy.risk_provenance_verified
    ):
        raise _failure("SUPPORTED_POLICY_REQUIRED")
    if execution.get("ibkr_autonomous_authority_schema") != (
        IBKR_AUTONOMOUS_AUTHORITY_SCHEMA
    ):
        raise _failure("AUTHORITY_SCHEMA_INVALID")
    if execution.get("ibkr_autonomous_policy_receipt_schema") != (
        IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA
    ):
        raise _failure("POLICY_RECEIPT_SCHEMA_INVALID")
    policy_receipt_bindings = _policy_receipt_bindings(
        policy,
        release_manifest_hash,
    )
    try:
        profile = IbkrLocalProviderProfile.from_config(policy.config)
    except Exception:
        raise _failure("PROVIDER_PROFILE_INVALID") from None
    if profile is None or any(
        (
            execution.get("ibkr_autonomous_authority_key_source")
            != "macos_keychain",
            execution.get("ibkr_autonomous_policy_receipt_key_source")
            != "macos_keychain",
            execution.get("ibkr_autonomous_api_name")
            != "official_tws_python_api",
            execution.get("ibkr_autonomous_api_version") != profile.sdk_version,
            execution.get("ibkr_autonomous_environment") != profile.environment,
            execution.get("ibkr_autonomous_client_id")
            != profile.command_client_id,
            profile.environment != "live",
        )
    ):
        raise _failure("AUTONOMOUS_CONFIG_INVALID")

    try:
        install = Path(install_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise _failure("INSTALL_ROOT_UNAVAILABLE") from None
    if not install.is_dir():
        raise _failure("INSTALL_ROOT_UNAVAILABLE")
    relative_raw = execution.get("ibkr_autonomous_authority_relative_path")
    if type(relative_raw) is not str:
        raise _failure("AUTHORITY_PATH_INVALID")
    relative = Path(relative_raw)
    authority_path = install / relative
    if (
        relative.is_absolute()
        or relative_raw != relative.as_posix()
        or "\\" in relative_raw
        or not relative.parts
        or any(part in {".", ".."} for part in relative.parts)
        or relative.parent != Path("control/ibkr")
        or relative.suffix != ".json"
    ):
        raise _failure("AUTHORITY_PATH_INVALID")
    try:
        resolved_authority = authority_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _failure("AUTHORITY_UNAVAILABLE") from None
    try:
        resolved_authority.relative_to(install)
    except ValueError:
        raise _failure("AUTHORITY_PATH_UNSAFE") from None
    if resolved_authority != authority_path:
        raise _failure("AUTHORITY_PATH_UNSAFE")
    policy_receipt_relative_raw = execution.get(
        "ibkr_autonomous_policy_receipt_relative_path"
    )
    if type(policy_receipt_relative_raw) is not str:
        raise _failure("POLICY_RECEIPT_PATH_INVALID")
    policy_receipt_relative = Path(policy_receipt_relative_raw)
    policy_receipt_path = install / policy_receipt_relative
    if (
        policy_receipt_relative.is_absolute()
        or policy_receipt_relative_raw != policy_receipt_relative.as_posix()
        or "\\" in policy_receipt_relative_raw
        or not policy_receipt_relative.parts
        or any(part in {".", ".."} for part in policy_receipt_relative.parts)
        or policy_receipt_relative.parent != Path("control/ibkr")
        or policy_receipt_relative.suffix != ".json"
        or policy_receipt_path == authority_path
    ):
        raise _failure("POLICY_RECEIPT_PATH_INVALID")
    try:
        resolved_policy_receipt = policy_receipt_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _failure("POLICY_RECEIPT_UNAVAILABLE") from None
    try:
        resolved_policy_receipt.relative_to(install)
    except ValueError:
        raise _failure("POLICY_RECEIPT_PATH_UNSAFE") from None
    if resolved_policy_receipt != policy_receipt_path:
        raise _failure("POLICY_RECEIPT_PATH_UNSAFE")
    bindings = _policy_bindings(policy, release_manifest_hash)
    service = execution.get("ibkr_autonomous_authority_key_service")
    key_account = execution.get("ibkr_autonomous_authority_key_account")
    if (
        type(service) is not str
        or type(key_account) is not str
        or not service
        or not key_account
    ):
        raise _failure("KEYCHAIN_LOCATOR_INVALID")
    try:
        key_item = KeychainItem(service=service, account=key_account)
    except Exception:
        raise _failure("KEYCHAIN_LOCATOR_INVALID") from None
    policy_receipt_service = execution.get(
        "ibkr_autonomous_policy_receipt_key_service"
    )
    policy_receipt_key_account = execution.get(
        "ibkr_autonomous_policy_receipt_key_account"
    )
    if (
        type(policy_receipt_service) is not str
        or type(policy_receipt_key_account) is not str
        or not policy_receipt_service
        or not policy_receipt_key_account
    ):
        raise _failure("POLICY_RECEIPT_KEYCHAIN_LOCATOR_INVALID")
    try:
        policy_receipt_key_item = KeychainItem(
            service=policy_receipt_service,
            account=policy_receipt_key_account,
        )
    except Exception:
        raise _failure("POLICY_RECEIPT_KEYCHAIN_LOCATOR_INVALID") from None
    if policy_receipt_key_item == key_item:
        raise _failure("POLICY_RECEIPT_KEYCHAIN_LOCATOR_NOT_DISTINCT")
    key_reader = keychain if keychain is not None else MacOSKeychain()
    if not callable(getattr(key_reader, "read", None)):
        raise _failure("KEY_LOADER_INVALID")

    # Do not touch state or construct a lock until the private contract has
    # authenticated successfully and is current for the exact release.
    try:
        secret = key_reader.read(key_item)
    except Exception:
        raise _failure("KEYCHAIN_UNAVAILABLE") from None
    if type(secret) is not bytes or len(secret) < 32:
        secret = b""
        raise _failure("KEYCHAIN_SECRET_INVALID")
    try:
        now = _utc(clock())
        authority = load_verified_ibkr_autonomous_authority(
            authority_path,
            secret=secret,
            expected=bindings,
            now=now,
        )
    except IbkrAutonomousInputError:
        raise
    except Exception:
        raise _failure("AUTHORITY_REJECTED") from None
    finally:
        secret = b""

    # A true provenance flag and a positive configured reserve remain only
    # policy declarations.  Require a separately keyed owner/pricing receipt
    # before opening mutable state or constructing the writer lock.
    try:
        policy_receipt_secret = key_reader.read(policy_receipt_key_item)
    except Exception:
        raise _failure("POLICY_RECEIPT_KEYCHAIN_UNAVAILABLE") from None
    if type(policy_receipt_secret) is not bytes or len(policy_receipt_secret) < 32:
        policy_receipt_secret = b""
        raise _failure("POLICY_RECEIPT_KEYCHAIN_SECRET_INVALID")
    try:
        policy_receipt = load_verified_ibkr_autonomous_policy_receipt(
            policy_receipt_path,
            secret=policy_receipt_secret,
            expected=policy_receipt_bindings,
            now=now,
        )
    except IbkrAutonomousInputError:
        raise
    except Exception:
        raise _failure("POLICY_RECEIPT_REJECTED") from None
    finally:
        policy_receipt_secret = b""
    # This is the stable SDK acceptance envelope, not a cached assertion that
    # the provider observation remains current.  The verifier above
    # authenticates a current (at most eight-hour) authority artifact twice at
    # every socket edge and accepts only monotonic same-binding rotations.  The
    # envelope is bounded by the pinned owner-policy receipt so renewal cannot
    # outlive that owner's exact release/policy/pricing approval.
    evidence = IbkrWriteEvidence(
        authorization_binding_id=bindings.authorization_binding_id,
        account_binding_fingerprint=bindings.account_binding_fingerprint,
        environment=bindings.environment,
        client_id=bindings.client_id,
        reviewed_contract_id=bindings.provider_contract_id,
        issued_at=max(authority.support_confirmed_at, policy_receipt.issued_at),
        expires_at=policy_receipt.expires_at,
    )

    state_path = install / "state/full-live.sqlite3"
    state, state_identity = _existing_state(
        state_path,
        runtime_id=policy.runtime_id,
        account_key=policy.account_key,
        release_manifest_hash=release_manifest_hash,
        config_hash=policy.config_hash,
        policy_hash=policy.policy_hash,
    )
    try:
        lock = AccountWriterLock(
            user_account_writer_lock_directory(),
            policy.account_key,
            # The durable lease owner is also the per-service-start nonce.  A
            # crashed lease from this PID can therefore never be mistaken for
            # the next command composition in the same long-lived process.
            owner_id=f"autonomous-full-live-service-{uuid4()}",
            broker_account_binding_fingerprint=(
                bindings.account_binding_fingerprint
            ),
            authorization_binding_id=bindings.authorization_binding_id,
        )
        interlock = AutonomousIbkrWriterInterlock(
            lock=lock,
            state=state,
            state_path=state_path,
            state_identity=state_identity,
            policy=policy,
            release_manifest_hash=release_manifest_hash,
            authority_bindings=bindings,
            clock=clock,
        )
        plan_bindings = AutonomousIbkrPlanBindings(
            runtime_id=policy.runtime_id,
            release_manifest_hash=release_manifest_hash,
            account_key=policy.account_key,
            account_masked=bindings.account_masked,
            strategy_id=policy.strategy_id,
            policy_hash=policy.policy_hash,
            config_hash=policy.config_hash,
        )
        producer = StateBackedAutonomousIbkrPlanProducer(
            state=state,
            bindings=plan_bindings,
            clock=clock,
            seal_ttl_seconds=float(policy.config["evidence"]["plan_ttl_seconds"]),
        )
        verifier = DurableIbkrAutonomousAcceptanceVerifier(
            path=authority_path,
            keychain=key_reader,
            key_item=key_item,
            bindings=bindings,
            authority=authority,
            policy_receipt_path=policy_receipt_path,
            policy_receipt_key_item=policy_receipt_key_item,
            policy_receipt_bindings=policy_receipt_bindings,
            policy_receipt=policy_receipt,
            expected_evidence=evidence,
            clock=clock,
        )
        risk_policy_check = DurableIbkrAutonomousRiskPolicyCheck(
            delegate=DurableIbkrRiskPolicyCheck(
                state_path=state_path,
                policy=policy,
            ),
            receipt_verifier=verifier,
            session_latch_interlock=interlock,
        )
        return IbkrCommandAssemblyInputs(
            plan_reader=producer.reader,
            risk_policy_check=risk_policy_check,
            acceptance_verifier=verifier,
            mutation_interlock=interlock,
            write_evidence=evidence,
            authority_mode="unattended",
            autonomous_authority=authority,
            autonomous_authority_bindings=bindings,
            autonomous_policy_receipt=policy_receipt,
            autonomous_policy_receipt_bindings=policy_receipt_bindings,
            service_writer_lock=lock,
            plan_sealer=producer,
            owned_resource=state,
            connect_command_session=connect_command_session,
            authorize_command_writes=authorize_command_writes,
        )
    except IbkrAutonomousInputError:
        state.close()
        raise
    except Exception:
        state.close()
        raise _failure("RESOURCE_ASSEMBLY_FAILED") from None


__all__ = [
    "DurableIbkrAutonomousAcceptanceVerifier",
    "DurableIbkrAutonomousRiskPolicyCheck",
    "IbkrAutonomousInputError",
    "KeychainReader",
    "build_release_bound_ibkr_autonomous_inputs",
]
