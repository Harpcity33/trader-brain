"""Bounded, non-authorizing bridge from approved measurement to real inputs.

This command reads the installed profile and its attested SDK, but uses the
source/release containing this module for the new owner-policy contract. It
does not install that contract, freeze a production baseline, open a command
connection, or create activation evidence. An explicit optional mode writes a
new, separate diagnostic state database; it never writes production risk state.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

from .session_trading_policy import MODEL, load_session_trading_policy_from_root


_REPORT_FIELDS = frozenset({
    "schema_version", "checked_at", "model", "diagnostic_only", "live_authority",
    "baseline_frozen", "production_policy_changed", "policy_binding_verified",
    "collection_completed", "mode", "measurement_policy_sha256", "owner_amendment_sha256",
    "installed_release_manifest_sha256", "inputs", "failure_phase", "error_code",
    "runtime_error_code", "command_connected_before_cleanup", "read_connected_after_cleanup",
    "command_connected_after_cleanup", "cleanup_completed",
})
_INPUT_FIELDS = frozenset({
    "collection_id", "read_generation", "collection_started_at", "collection_completed_at",
    "timing_basis", "net_liquidation_currency", "position_count", "execution_count", "order_count",
    "active_order_count", "completed_reads", "commission_conflict_observed", "orphan_commission_report_count",
    "daily_pnl_status", "diagnostic_only", "live_authority", "read_client_id", "prior_collection_id",
    "unobserved_interval_since_prior_collection", "sticky_read_gap", "blockers", "baseline_authority",
    "session_measurement_authority", "whole_account_coverage_verified", "first_collection_id",
    "second_collection_id", "observed_at", "starting_balance_candidate_present", "observation_preconditions_met",
    "baseline_frozen", "execution_time_bases", "account_values_source",
    "full_session_pnl_established", "diagnostic_baseline_recorded", "diagnostic_risk_state_recorded",
    "pending_read_persisted_before_capture", "state_revision", "audit_head_sha256",
    "unresolved_observation_count", "state_reopened_and_matched", "arithmetic_available",
    "material_blocker_count", "source_blockers", "risk_blockers", "observation_failed", "state_integrity_scope",
    "observation_failure_phase", "read_diagnostic_available", "read_elapsed_ms", "read_missing_channels",
    "read_normalization_completed", "read_commission_reports_missing",
    "capture_failure_code",
    "cumulative_observation_count", "evidence_ledger_head_sha256", "evidence_reopened_and_matched",
    "accounting_check_completed", "observed_cash_identity_matched", "accounting_material_blocker_count", "accounting_evidence_sha256",
})
_PUBLIC_BLOCKERS = frozenset({
    "ACCOUNT_VALUES_ECONOMIC_TIME_UNAVAILABLE", "ACTUAL_COMMISSION_USD_NOT_ESTABLISHED",
    "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", "CONTINUOUS_EVENT_COVERAGE_UNPROVEN",
    "EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", "EXECUTION_OUTSIDE_USD_WHOLE_SHARE_SCOPE",
    "EXECUTION_PROVIDER_TIME_FUTURE", "EXECUTION_PROVIDER_TIME_UNAVAILABLE",
    "EXECUTION_RECEIPT_OUTSIDE_COLLECTION", "EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN",
    "EXECUTION_TIME_BASIS_INVALID", "EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE",
    "NET_LIQUIDATION_USD_NOT_ESTABLISHED", "ORDER_WARNING_REQUIRES_RECONCILIATION",
    "ORPHAN_COMMISSION_SCOPE_UNKNOWN", "POSITION_OUTSIDE_USD_WHOLE_SHARE_LONG_SCOPE",
    "PREVIOUS_EXECUTION_NOT_IN_CURRENT_BOUNDED_READ", "PRE_ENTRY_ACCOUNT_OR_GENERATION_MOVED",
    "PRE_ENTRY_ACTIVE_OR_UNKNOWN_ORDERS_OBSERVED", "PRE_ENTRY_COLLECTION_CROSSES_SESSION_DAY",
    "PRE_ENTRY_COLLECTION_WINDOW_TOO_WIDE", "PRE_ENTRY_CURRENT_DAY_EXECUTIONS_OBSERVED",
    "PRE_ENTRY_FLAT_POSITIONS_NOT_OBSERVED", "PRE_ENTRY_NLV_NOT_STABLE", "PRE_ENTRY_USD_NLV_UNPROVEN",
    "READ_GAP_REQUIRES_RECONCILIATION", "SESSION_DAY_CHANGED_REQUIRES_NEW_BASELINE_WORKFLOW",
    "STARTING_NLV_NOT_POSITIVE_FINITE",
})


def _public_value(key: str, value: object) -> bool:
    """Only status-shaped scalar/list values, never arbitrary nested payloads."""
    if key in {"command_connected_before_cleanup", "runtime_error_code", "prior_collection_id", "read_elapsed_ms", "capture_failure_code", "accounting_evidence_sha256"} and value is None:
        return True
    if key == "capture_failure_code":
        from .broker.ibkr_session_inputs import is_public_session_input_failure_code
        return is_public_session_input_failure_code(value)
    if key.endswith("_sha256") or key in {"collection_id", "prior_collection_id", "first_collection_id", "second_collection_id"}:
        return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None
    if key in {"checked_at", "collection_started_at", "collection_completed_at", "observed_at"}:
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
            return parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None
        except ValueError:
            return False
    enums = {
        "schema_version": {"titan_session_input_probe_2026-09-18_v1"}, "model": {MODEL},
        "mode": {"single_read", "pre_entry_candidate", "persisted_session_rehearsal"},
        "failure_phase": {"policy_binding", "installed_profile", "read_connection", "finite_input_collection"},
        "error_code": {"SESSION_INPUT_PROBE_FAILED", "SESSION_INPUT_PROBE_CLEANUP_FAILED", "SESSION_INPUT_PROBE_WORKER_TIMEOUT", "SESSION_INPUT_PROBE_WORKER_FAILED"},
        "timing_basis": {"local_receipt_not_atomic_broker_valuation"},
        "net_liquidation_currency": {"", "BASE", "USD"}, "daily_pnl_status": {"not_requested"},
        "account_values_source": {"IBKR_ACCOUNT_UPDATES_MULTI_V1"},
        "state_integrity_scope": {"LOCAL_UNKEYED_CONSISTENCY_NOT_SOURCE_AUTHENTICATION_OR_ROLLBACK_PROOF"},
        "observation_failure_phase": {"none", "capture", "evidence_persistence", "accounting", "calculation"},
    }
    if key in enums:
        return isinstance(value, str) and value in enums[key]
    if key == "runtime_error_code":
        return isinstance(value, str) and re.fullmatch(r"(?:IBKR_|SESSION_INPUT_)[A-Z0-9_:,]{1,140}", value) is not None
    list_enums = {
        "blockers": _PUBLIC_BLOCKERS,
        "completed_reads": {"account_updates_multi", "completed_orders", "executions", "open_orders", "positions"},
        "execution_time_bases": {"PROVIDER_EXPLICIT_ZONE", "CONFIGURED_SESSION_ZONE_INTERPRETATION", "ABSENT"},
        "source_blockers": _PUBLIC_BLOCKERS | {"SOURCE_CAPTURE_FAILED", "EVIDENCE_PERSISTENCE_FAILED", "ACCOUNTING_CHECK_FAILED", "CALCULATION_REJECTED"},
        "read_missing_channels": {"account_updates_multi", "completed_orders", "executions", "open_orders", "positions"},
        "risk_blockers": {"UNRESOLVED_OBSERVATION_INCIDENT", "SESSION_LOSS_LATCHED", "SESSION_DATE_MISMATCH", "SESSION_MEASUREMENT_MISSING", "SESSION_MEASUREMENT_STALE"},
    }
    if key in list_enums:
        return type(value) is list and all(isinstance(item, str) and item in list_enums[key] for item in value)
    if key.endswith("_count") or key in {"read_generation", "read_client_id", "state_revision", "read_elapsed_ms"}:
        return type(value) is int and value >= 0
    return type(value) is bool


def _public_report_shape(report: object) -> bool:
    if type(report) is not dict or not set(report) <= _REPORT_FIELDS:
        return False
    if any(not _public_value(key, value) for key, value in report.items() if key != "inputs"):
        return False
    if report.get("schema_version") != "titan_session_input_probe_2026-09-18_v1" or report.get("diagnostic_only") is not True:
        return False
    if any(report.get(key) is not False for key in ("live_authority", "baseline_frozen", "production_policy_changed")):
        return False
    if type(report.get("collection_completed")) is not bool or type(report.get("cleanup_completed")) is not bool:
        return False
    for key in ("error_code", "runtime_error_code"):
        code = report.get(key)
        if code is not None and (not isinstance(code, str) or re.fullmatch(r"[A-Z0-9_:,]{1,160}", code) is None):
            return False
    if "inputs" in report:
        inputs = report["inputs"]
        if type(inputs) is not dict or not set(inputs) <= _INPUT_FIELDS:
            return False
        if any(not _public_value(key, value) for key, value in inputs.items()):
            return False
        if inputs.get("diagnostic_only") is not True or inputs.get("live_authority") is not False:
            return False
        if inputs.get("observation_failed") is True and report.get("collection_completed") is True:
            return False
        for key in ("baseline_authority", "session_measurement_authority", "whole_account_coverage_verified", "baseline_frozen", "full_session_pnl_established"):
            if key in inputs and inputs[key] is not False:
                return False
    return True


def _status_or_none(runtime):
    try:
        return runtime.status()
    except Exception:
        return None


def run_probe(*, source_root: Path, install_root: Path, single_read: bool = False, diagnostic_session_store: Path | None = None) -> dict[str, object]:
    """Collect status-only inputs. The isolated launcher enforces a hard timeout."""
    result: dict[str, object] = {
        "schema_version": "titan_session_input_probe_2026-09-18_v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "diagnostic_only": True,
        "live_authority": False,
        "baseline_frozen": False,
        "production_policy_changed": False,
        "policy_binding_verified": False,
        "collection_completed": False,
        "cleanup_completed": False,
        "mode": "persisted_session_rehearsal" if diagnostic_session_store is not None else ("single_read" if single_read else "pre_entry_candidate"),
    }
    runtime = None
    phase = "policy_binding"
    prior_logging_disable = logging.root.manager.disable
    # Broker SDK diagnostics may contain private payloads. Never serialize
    # those logs or arbitrary exception text into this public status command.
    logging.disable(sys.maxsize)
    try:
        with open(os.devnull, "w") as discarded, contextlib.redirect_stdout(discarded), contextlib.redirect_stderr(discarded):
            try:
                if diagnostic_session_store is not None and (
                    single_read or not diagnostic_session_store.is_absolute()
                    or diagnostic_session_store.resolve().is_relative_to(install_root.resolve())
                    or diagnostic_session_store.exists()
                ):
                    raise ValueError("diagnostic store requires a new path outside production installation")
                chosen_policy = load_session_trading_policy_from_root(source_root)
                result["policy_binding_verified"] = True
                result["measurement_policy_sha256"] = chosen_policy.policy_sha256
                result["owner_amendment_sha256"] = chosen_policy.amendment_sha256
                phase = "installed_profile"
                from .release import load_release_manifest
                from .policy import PolicyBundle
                from .provider_profile import IbkrLocalProviderProfile

                release_root = (install_root / "current").resolve(strict=True)
                manifest = load_release_manifest(
                    install_root / "release-manifest.json", verify_files_root=release_root
                )
                installed_policy = PolicyBundle.load(release_root, config_relative=manifest["config_path"])
                profile = IbkrLocalProviderProfile.from_config(installed_policy.config)
                if profile is None:
                    raise ValueError("profile unavailable")
                result["installed_release_manifest_sha256"] = manifest["release_manifest_hash"]
                phase = "read_connection"
                from .broker.ibkr_runtime import build_ibkr_official_runtime
                from .broker.ibkr_session_inputs import IbkrSessionInputAdapter

                runtime = build_ibkr_official_runtime(profile=profile, install_root=install_root)
                runtime.connect_reads()
                if runtime.status().command_connected:
                    raise ValueError("unexpected command connection")
                phase = "finite_input_collection"
                adapter = IbkrSessionInputAdapter(runtime)
                if diagnostic_session_store is not None:
                    from .session_trading_rehearsal import record_fresh_session_rehearsal
                    observation = record_fresh_session_rehearsal(
                        policy=chosen_policy, adapter=adapter,
                        store_path=diagnostic_session_store, now=lambda: datetime.now(timezone.utc),
                    )
                else:
                    observation = adapter.capture() if single_read else adapter.capture_pre_entry_candidate()
                result["inputs"] = observation.public_dict()
                result["collection_completed"] = result["inputs"].get("observation_failed") is not True
                if not result["collection_completed"]:
                    result["failure_phase"] = phase
                    result["error_code"] = "SESSION_INPUT_PROBE_FAILED"
                    status = _status_or_none(runtime)
                    result["runtime_error_code"] = status.runtime_error_code if status is not None else "SESSION_INPUT_STATUS_UNAVAILABLE"
            except Exception:
                result["failure_phase"] = phase
                result["error_code"] = "SESSION_INPUT_PROBE_FAILED"
                if runtime is not None:
                    # This property is an existing sanitized runtime code,
                    # never the raw SDK message or a caught exception string.
                    status = _status_or_none(runtime)
                    result["runtime_error_code"] = status.runtime_error_code if status is not None else "SESSION_INPUT_STATUS_UNAVAILABLE"
            finally:
                if runtime is not None:
                    status = _status_or_none(runtime)
                    result["command_connected_before_cleanup"] = status.command_connected if status is not None else None
                    try:
                        runtime.stop()
                        status = _status_or_none(runtime)
                        if status is None:
                            raise ValueError("cleanup status unavailable")
                        result["read_connected_after_cleanup"] = status.read_connected
                        result["command_connected_after_cleanup"] = status.command_connected
                        result["cleanup_completed"] = not status.read_connected and not status.command_connected
                    except Exception:
                        result["cleanup_completed"] = False
                        result["error_code"] = "SESSION_INPUT_PROBE_CLEANUP_FAILED"
    finally:
        logging.disable(prior_logging_disable)
    return result


def run_isolated_probe(*, source_root: Path, argv: list[str], timeout_seconds: float = 45) -> int:
    """Hard-bound the complete SDK worker, including connect and shutdown.

    A timed-out worker is killed and reaped by subprocess.run. Its sockets are
    closed by process exit, but graceful SDK cleanup is explicitly unconfirmed.
    Never forward arbitrary child stderr, tracebacks or malformed stdout.
    """
    failure = {
        "schema_version": "titan_session_input_probe_2026-09-18_v1",
        "diagnostic_only": True,
        "live_authority": False,
        "baseline_frozen": False,
        "production_policy_changed": False,
        "collection_completed": False,
        "cleanup_completed": False,
    }
    worker = (
        "import sys; from pathlib import Path; "
        "root = Path(sys.argv.pop(1)); sys.path.insert(0, str(root / 'src')); "
        "from titan_brain.live.session_input_probe import main; "
        "raise SystemExit(main(source_root=root))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", worker, str(source_root), *argv],
            capture_output=True, text=True, input="", timeout=timeout_seconds, check=False,
        )
        report = json.loads(completed.stdout)
        if not _public_report_shape(report) or completed.returncode not in (0, 2):
            raise ValueError("unexpected worker output")
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if completed.returncode == 0 and report.get("collection_completed") is True and report.get("cleanup_completed") is True else 2
    except subprocess.TimeoutExpired:
        failure["error_code"] = "SESSION_INPUT_PROBE_WORKER_TIMEOUT"
    except Exception:
        failure["error_code"] = "SESSION_INPUT_PROBE_WORKER_FAILED"
    print(json.dumps(failure, sort_keys=True, allow_nan=False))
    return 2


def main(*, source_root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only broker inputs; optional separate diagnostic state rehearsal; never activates trading.")
    parser.add_argument("--install-root", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--single-read", action="store_true", help="Collect one observation instead of a flat pre-entry double-read candidate.")
    modes.add_argument("--diagnostic-session-store", type=Path, help="Create a NEW diagnostic SQLite file outside the installation and exercise durable pending-before-read state; never a production baseline.")
    args = parser.parse_args(argv)
    kwargs = {"diagnostic_session_store": args.diagnostic_session_store} if args.diagnostic_session_store is not None else {}
    report = run_probe(source_root=source_root, install_root=args.install_root, single_read=args.single_read, **kwargs)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    # Success only means the finite diagnostic completed, not baseline or
    # trading readiness. Explicitly false authority fields remain mandatory.
    return 0 if report["collection_completed"] and report.get("cleanup_completed") else 2


if __name__ == "__main__":
    raise SystemExit(main(source_root=Path(__file__).resolve().parents[3]))
