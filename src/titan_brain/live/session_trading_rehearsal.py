"""One-shot diagnostic integration of real inputs, calculation and durable state.

This is a rehearsal, not a production baseline or risk-evidence issuer. A new,
exclusive diagnostic database is required. No order, activation, credentials,
legacy risk fields or production database are accessed. Collection limitations
stay unresolved; arithmetic over visible records never becomes complete P&L.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
import uuid
from zoneinfo import ZoneInfo

from .broker.ibkr_session_inputs import IbkrSessionInputAdapter
from .broker.ibkr_read import IbkrReadCollectionDiagnostic
from .session_trading_calculation import (
    calculate_session_trading_pnl,
    session_baseline_evidence_sha256,
    validate_session_baseline_observations,
)
from .session_trading_policy import (
    ObservationStatus,
    SessionTradingBaseline,
    SessionTradingPolicy,
    evaluate_session_state,
)
from .session_trading_store import SessionTradingStore
from .session_observation_ledger import SessionObservationLedger
from .session_accounting_reconciliation import reconcile_session_accounting


@dataclass(frozen=True)
class SessionRehearsalResult:
    state_revision: int
    audit_head_sha256: str
    unresolved_observation_count: int
    state_reopened_and_matched: bool
    arithmetic_available: bool
    material_blockers: tuple[str, ...]
    source_blockers: tuple[str, ...]
    risk_blockers: tuple[str, ...]
    observation_failed: bool
    observation_failure_phase: str
    capture_failure_code: str | None
    read_diagnostic: IbkrReadCollectionDiagnostic | None
    cumulative_observation_count: int
    evidence_ledger_head_sha256: str
    evidence_reopened_and_matched: bool
    accounting_check_completed: bool
    observed_cash_identity_matched: bool
    accounting_material_blocker_count: int
    accounting_evidence_sha256: str | None

    def public_dict(self) -> dict[str, object]:
        # Actual values and exact account/contract/execution identifiers remain
        # local. A persisted diagnostic baseline is not a frozen live baseline.
        return {
            "diagnostic_only": True,
            "live_authority": False,
            "baseline_authority": False,
            "baseline_frozen": False,
            "session_measurement_authority": False,
            "full_session_pnl_established": False,
            "diagnostic_baseline_recorded": True,
            "diagnostic_risk_state_recorded": True,
            "pending_read_persisted_before_capture": True,
            "state_revision": self.state_revision,
            "audit_head_sha256": self.audit_head_sha256,
            "unresolved_observation_count": self.unresolved_observation_count,
            "state_reopened_and_matched": self.state_reopened_and_matched,
            "cumulative_observation_count": self.cumulative_observation_count,
            "evidence_ledger_head_sha256": self.evidence_ledger_head_sha256,
            "evidence_reopened_and_matched": self.evidence_reopened_and_matched,
            "accounting_check_completed": self.accounting_check_completed,
            "observed_cash_identity_matched": self.observed_cash_identity_matched,
            "accounting_material_blocker_count": self.accounting_material_blocker_count,
            "accounting_evidence_sha256": self.accounting_evidence_sha256,
            "arithmetic_available": self.arithmetic_available,
            "material_blocker_count": len(self.material_blockers),
            "source_blockers": self.source_blockers,
            "risk_blockers": self.risk_blockers,
            "observation_failed": self.observation_failed,
            "observation_failure_phase": self.observation_failure_phase,
            "capture_failure_code": self.capture_failure_code,
            "read_diagnostic_available": self.read_diagnostic is not None,
            "read_elapsed_ms": None if self.read_diagnostic is None else self.read_diagnostic.elapsed_ms,
            "read_missing_channels": () if self.read_diagnostic is None else tuple(
                row.channel for row in self.read_diagnostic.channels
                if row.channel != "daily_realized_pnl" and row.end_callback_ms is None
            ),
            "read_normalization_completed": self.read_diagnostic is not None and self.read_diagnostic.normalization_completed,
            "read_commission_reports_missing": self.read_diagnostic is not None and self.read_diagnostic.commission_reports_missing,
            "state_integrity_scope": "LOCAL_UNKEYED_CONSISTENCY_NOT_SOURCE_AUTHENTICATION_OR_ROLLBACK_PROOF",
        }


def record_fresh_session_rehearsal(
    *, policy: SessionTradingPolicy, adapter: IbkrSessionInputAdapter,
    store_path: Path, now: Callable[[], datetime],
) -> SessionRehearsalResult:
    """Exercise persist-before-read and reopen without granting any authority.

    This deliberately refuses to reuse an existing file. Recovery of a real
    trading session needs its source history and external authentication; a
    fresh finite read cannot silently restart an old baseline or clear a gap.
    """
    if type(policy) is not SessionTradingPolicy or type(adapter) is not IbkrSessionInputAdapter:
        raise TypeError("concrete session policy and read-only adapter required")
    calculation = None
    accounting = None
    observation_failed = False
    observation_failure_phase = "none"
    capture_failure_code = None
    read_diagnostic = None
    evidence_path = store_path.with_name(store_path.name + ".observations")
    if evidence_path.exists() or evidence_path.is_symlink():
        raise ValueError("SESSION_REHEARSAL_EVIDENCE_ALREADY_EXISTS")
    with ExitStack() as resources:
        store = resources.enter_context(SessionTradingStore(store_path, create=True))
        first = adapter.capture()
        evidence_identity = {
            "account_binding_sha256": first.account_binding_fingerprint,
            "session_date": first.facts.collection_completed_at.astimezone(ZoneInfo("America/New_York")).date(),
        }
        ledger = resources.enter_context(SessionObservationLedger(evidence_path, **evidence_identity, create=True))
        evidence_head = ledger.append(first, expected_previous_receipt=ledger.head_receipt)
        second = adapter.capture()
        evidence_head = ledger.append(second, expected_previous_receipt=evidence_head)
        validate_session_baseline_observations(first, second, now=now())
        baseline = SessionTradingBaseline(
            policy_sha256=policy.policy_sha256,
            account_binding_sha256=second.account_binding_fingerprint,
            evidence_sha256=session_baseline_evidence_sha256(first, second),
            frozen_at=second.facts.collection_completed_at,
            starting_nlv=second.facts.net_liquidation,
            flat_start=True, pre_entry=True, initial_exposure_reconciled=True,
        )
        stored = store.start_session(policy, baseline)
        identity = {
            "account_binding_sha256": baseline.account_binding_sha256,
            "session_date": baseline.frozen_at.astimezone(ZoneInfo("America/New_York")).date(),
        }
        token = "rehearsal-" + uuid.uuid4().hex
        pending = store.begin_observation(
            **identity, expected_revision=stored.revision, token=token, now=now(),
        )
        # begin_observation commits before this next broker request. A crash or
        # watchdog kill must leave the pending incident on disk, not erase it.
        phase = "capture"
        preceding_diagnostic = adapter.last_capture_diagnostic
        try:
            observation = adapter.capture()
            phase = "evidence_persistence"
            evidence_head = ledger.append(observation, expected_previous_receipt=evidence_head)
            history = ledger.history()
            calculated_at = now()
            phase = "accounting"
            accounting = reconcile_session_accounting(
                baseline_observations=history[:2], prior_observations=history[2:-1],
                observation=history[-1], now=calculated_at,
            )
            phase = "calculation"
            calculation = calculate_session_trading_pnl(
                policy, baseline, baseline_observations=history[:2],
                prior_observations=history[2:-1], observation=history[-1], now=calculated_at,
            )
        except Exception:
            observation_failed = True
            observation_failure_phase = phase
            if phase == "capture":
                capture_failure_code = adapter.last_capture_failure_code
            stored = store.fail_observation(
                **identity, expected_revision=pending.revision, token=token, now=now(), gap=True,
            )
        else:
            # This conversion is always incomplete. Persist the incident; do
            # not fabricate complete evidence from arithmetic over finite reads.
            stored = store.complete_observation(
                **identity, expected_revision=pending.revision, token=token,
                measurement=calculation.to_incomplete_measurement(), now=now(),
            )
        latest_diagnostic = adapter.last_capture_diagnostic
        if latest_diagnostic is not preceding_diagnostic:
            read_diagnostic = latest_diagnostic
        evidence_history = ledger.history()
        if ledger.head_receipt != evidence_head:
            raise ValueError("SESSION_REHEARSAL_EVIDENCE_HEAD_MOVED")
    with SessionTradingStore(store_path) as reopened:
        recovered = reopened.load(**identity)
    if recovered is None or recovered != stored:
        raise ValueError("SESSION_REHEARSAL_REOPEN_MISMATCH")
    with SessionObservationLedger(evidence_path, **evidence_identity) as reopened_evidence:
        if reopened_evidence.head_receipt != evidence_head or reopened_evidence.history() != evidence_history:
            raise ValueError("SESSION_REHEARSAL_EVIDENCE_REOPEN_MISMATCH")
    decision = evaluate_session_state(recovered.state, now=now())
    return SessionRehearsalResult(
        state_revision=recovered.revision,
        audit_head_sha256=recovered.audit_head_sha256,
        unresolved_observation_count=sum(
            row.status is not ObservationStatus.COMPLETED for row in recovered.state.incidents
        ),
        state_reopened_and_matched=True,
        arithmetic_available=calculation is not None and calculation.calculated_pnl is not None,
        material_blockers=() if calculation is None else calculation.material_blockers,
        source_blockers=(({
            "capture": "SOURCE_CAPTURE_FAILED",
            "evidence_persistence": "EVIDENCE_PERSISTENCE_FAILED",
            "accounting": "ACCOUNTING_CHECK_FAILED",
            "calculation": "CALCULATION_REJECTED",
        }[observation_failure_phase],) if calculation is None else calculation.source_blockers),
        risk_blockers=decision.entry_blockers,
        observation_failed=observation_failed,
        observation_failure_phase=observation_failure_phase,
        capture_failure_code=capture_failure_code,
        read_diagnostic=read_diagnostic,
        cumulative_observation_count=len(evidence_history),
        evidence_ledger_head_sha256=evidence_head,
        evidence_reopened_and_matched=True,
        accounting_check_completed=accounting is not None,
        observed_cash_identity_matched=accounting is not None and accounting.observed_cash_identity_matched,
        accounting_material_blocker_count=0 if accounting is None else len(accounting.material_blockers),
        accounting_evidence_sha256=None if accounting is None else accounting.evidence_sha256,
    )
