from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 runtime compatibility.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        from pip._vendor import tomli as tomllib  # type: ignore[no-redef]


WORKSPACE = Path(__file__).resolve().parent.parent
STAGED = WORKSPACE / "work" / "shadow-policy"
sys.path.insert(0, str(STAGED))

from titan_runtime.config import RuntimeConfig  # noqa: E402
from titan_runtime.ranking import build_preliminary_trade_plan  # noqa: E402


DATABASE = Path.home() / "Library/Application Support/Titan Momentum/runtime/titan-intelligence.sqlite3"
CONFIG = STAGED / "config" / "titan-massive.json"
AUTOMATION = WORKSPACE / "work" / "live-autonomous-policy" / "automation.toml"
REPO_AUTOMATION = (
    WORKSPACE / "work" / "repo-additions" / "config" / "live-automation"
    / "robinhood-momentum-engine.toml"
)
ADAPTATION = (
    WORKSPACE / "work" / "repo-additions" / "knowledge" / "adaptations"
    / "2026-08-23-pilot-operating-system.json"
)
REPO_PILOT_ROOT = WORKSPACE / "work" / "repo-additions" / "config" / "pilots"
STAGED_PILOT_ROOT = STAGED / "config" / "pilots"
PILOT_REGISTRY = REPO_PILOT_ROOT / "registry-2026-08-23-v1.json"
LIVE_DECISION_CONTRACT = REPO_PILOT_ROOT / "titan-momentum-equity.json"
SCHEMA_ROOT = WORKSPACE / "work" / "repo-additions" / "schemas"
STORAGE = STAGED / "titan_runtime" / "storage.py"
CLI = STAGED / "titan_runtime" / "cli.py"
CONFIG_MODULE = STAGED / "titan_runtime" / "config.py"
MASSIVE_MODULE = STAGED / "titan_runtime" / "massive.py"
INSTALLED_ROOT = Path.home() / "Library/Application Support/Titan Momentum"
INSTALLED_STORAGE = INSTALLED_ROOT / "titan_runtime" / "storage.py"
INSTALLED_CLI = INSTALLED_ROOT / "titan_runtime" / "cli.py"
INSTALLED_AUTOMATION = (
    Path.home() / ".codex" / "automations" / "robinhood-momentum-engine"
    / "automation.toml"
)
STRATEGY_VERSION = "titan_live_canonical_2026-08-23_v3"
GRADE_RUBRIC_VERSION = "titan_daily_performance_2026-08-23_v1"
PILOT_ID = "titan_momentum_equity"
BOOK_MODE = "SHADOW"
LIVE_BOOK_MODE = "LIVE"
DECISION_CONTRACT_VERSION = "titan_momentum_equity_2026-08-23_v1"
PROTECTED_CONTROL_MANIFEST = (
    ("ACCOUNT_KEY", "ending-7153"),
    ("BROKER_TRUTH", "ROBINHOOD_ONLY"),
    ("MASSIVE_AUTHORITY", "DATA_ONLY"),
    ("DAY_LOSS_LOCK_DOLLARS", "-100_IRREVERSIBLE"),
    ("DAILY_OBJECTIVE_DOLLARS", "150_NOT_GUARANTEED"),
    ("POST_OBJECTIVE_FLOOR_DOLLARS", "125_AFTER_FIRST_150_CROSSING"),
    ("LEVERAGE_OR_DEBIT", "PROHIBITED"),
    ("OVERNIGHT_EXPOSURE", "PROHIBITED"),
    ("AVERAGE_DOWN", "PROHIBITED"),
    ("WIDEN_ORIGINAL_STOP", "PROHIBITED"),
    ("FILLED_QUANTITY_PROTECTION", "REQUIRED"),
    ("LIVE_PILOT_ID", "titan_momentum_equity"),
    ("DECISION_CONTRACT_VERSION", "titan_momentum_equity_2026-08-23_v1"),
    (
        "DECISION_CONTRACT_HASH",
        "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21",
    ),
    ("LIVE_BROKER_AUTHORITY", "TITAN_MOMENTUM_EQUITY_ONLY"),
    ("SHADOW_PILOT_AUTHORITY", "ZERO_TRADE_RISK_ALLOCATION"),
    ("TRUTH_BOOKS", "LIVE_PAPER_SHADOW_SEPARATE"),
    ("LEADERBOARD_AUTHORITY", "REPORTING_ONLY"),
    ("EMERGENCY_ENTRY_STOP", "PROTECTED_OPERATOR_DURABLE_FAIL_CLOSED"),
    ("EMERGENCY_ENTRY_STOP_RELEASE", "UNAVAILABLE_IN_AUTOMATION_RUNTIME"),
    ("BROKER_ACK_DEADLINE_SECONDS", "10"),
    ("BUYING_POWER_MISMATCH_TOLERANCE", "MAX_5_DOLLARS_OR_1PCT_EQUITY"),
    ("GRADE_RESEARCH_PROPOSAL_AUTHORITY", "UNTRUSTED_DATA_ONLY"),
    ("IMMEDIATE_SAFE_SCOPE", "DRAFT_NONEXECUTABLE_ONLY"),
    (
        "POST_GRADE_PRODUCTION_ACTIVATION",
        "DISABLED_UNTIL_DURABLE_GUARD_LEASE",
    ),
)
CONTRACT_FACT_SHEET_METRIC_MAPPING = {
    "after_cost_expectancy_r": ("metrics.net_expectancy_r_after_costs",),
    "ticker_session_clustered_95pct_ci": (
        "metrics.clustered_95pct_lower_bound_expectancy_r",
        "metrics.clustered_95pct_upper_bound_expectancy_r",
    ),
    "maximum_drawdown_r": ("metrics.max_drawdown_r",),
    "profit_factor": ("metrics.profit_factor",),
    "win_rate_pct": ("metrics.win_rate_pct",),
    "execution_shortfall_bps": ("metrics.execution_shortfall_bps",),
    "entry_slippage_bps": ("metrics.entry_slippage_bps",),
    "exit_slippage_bps": ("metrics.exit_slippage_bps",),
    "effective_independent_sample_size": (
        "metrics.effective_independent_sample_size",
    ),
    "distinct_ticker_session_count": ("metrics.distinct_ticker_session_count",),
    "trading_session_count": ("metrics.session_count",),
    "distinct_underlying_count": ("metrics.underlying_count",),
    "evidence_coverage_pct": ("metrics.evidence_coverage_pct",),
    "policy_hash": ("policy_hash",),
    "decision_contract_hash": ("decision_contract_hash",),
    "known_failure_modes": ("known_failure_modes",),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected_semantics_sha256(prompt: str) -> str:
    heading = "MACHINE-VALIDATED PROTECTED CONTROL MANIFEST"
    lines = prompt.splitlines()
    require(lines.count(heading) == 1, "protected control manifest heading must occur once")
    start = lines.index(heading) + 1
    actual: list[str] = []
    for line in lines[start:]:
        if not line.startswith("- `"):
            break
        actual.append(line)
    expected = [f"- `{key}={value}`" for key, value in PROTECTED_CONTROL_MANIFEST]
    require(actual == expected, "protected control manifest differs from canonical semantics")
    canonical = json.dumps(
        dict(PROTECTED_CONTROL_MANIFEST), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def main() -> None:
    automation_bytes = AUTOMATION.read_bytes()
    repo_automation_bytes = REPO_AUTOMATION.read_bytes()
    require(
        automation_bytes == repo_automation_bytes,
        "staged and repository automation artifacts are not byte-identical",
    )
    automation = tomllib.loads(automation_bytes.decode("utf-8"))
    prompt = automation["prompt"]
    require(automation["id"] == "robinhood-momentum-engine", "unexpected automation id")
    require(automation["kind"] == "heartbeat", "unexpected automation kind")
    require(automation["status"] == "ACTIVE", "automation is not active")
    require(STRATEGY_VERSION in prompt, "strategy version is missing")
    require(GRADE_RUBRIC_VERSION in prompt, "grade rubric version is missing")
    require("0.80 * Process + 0.20 * Outcome" in prompt, "grade weights differ")
    require("performance grade FILE" in prompt, "grade command is missing")
    require("IMMEDIATE_SAFE" in prompt, "IMMEDIATE_SAFE tier is missing")
    require("SHADOW_FIRST" in prompt, "SHADOW_FIRST tier is missing")
    require("PROTECTED_USER_ONLY" in prompt, "PROTECTED_USER_ONLY tier is missing")
    semantics_sha256 = protected_semantics_sha256(prompt)
    exact_grade_semantics = (
        "ACCOUNT_DAY_PNL = CURRENT_EQUITY - START_OF_DAY_EQUITY - CONFIRMED_CASH_FLOW_ADJUSTMENT",
        "NET_R = ACCOUNT_DAY_PNL / AUTHORIZED_FILLED_RISK_DOLLARS",
        "CAPTURE_RATIO_PCT = 100 * REALIZED_AFTER_COST_PROFIT_DOLLARS / (EXECUTED_AFTER_COST_FAVORABLE_OPPORTUNITY_DOLLARS + MISSED_AFTER_COST_FAVORABLE_OPPORTUNITY_DOLLARS)",
        "FINAL_POSITION_COUNT=0",
        "FINAL_WORKING_ORDER_COUNT=0",
        "FINAL_WORKING_ENTRY_ORDER_COUNT=0",
        "FINAL_WORKING_EXIT_ORDER_COUNT=0",
        "FINAL_ACTIVE_CAMPAIGN_COUNT=0",
        "FINAL_ACTIVE_OR_CONSUMED_AUTHORIZATION_COUNT=0",
        "no_change_reason",
        "SELF-REPORTED / NOT INDEPENDENTLY SEALED",
        "NO PRODUCTION OR CHANGE AUTHORITY",
        "Grade corrections are disabled until an independent evidence sealer is installed",
        "GRADE_RESEARCH_PROPOSAL_AUTHORITY=UNTRUSTED_DATA_ONLY",
        "POST_GRADE_PRODUCTION_ACTIVATION=DISABLED_UNTIL_DURABLE_GUARD_LEASE",
        "fresh read-only Robinhood reconciliation immediately before the atomic activation",
        "do not merge, copy, install, schedule, deploy, activate",
        "pilot_id=titan_momentum_equity",
        "book_mode=LIVE",
        "decision_contract_version=titan_momentum_equity_2026-08-23_v1",
        "decision_contract_hash=dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21",
        "LIVE contains only Robinhood-confirmed orders, fills, positions, protection, and P&L",
        "PAPER contains only causal simulated decisions and fills",
        "SHADOW contains only causal zero-authority observations and counterfactual outcomes",
        "entry-stop status --limit 1",
        "entry-stop engage --reason REASON --changed-by OPERATOR_ID",
        "Emergency-stop release is unavailable to this automation and unavailable through the Titan runtime CLI",
        "separate stopped-service, explicitly user-approved maintenance outside this CLI",
        "risk submission-unknown --authorization-id AUTHORIZATION_ID --attempted-at ISO_TIMESTAMP --reason broker_submission_started",
        "--symbol SYMBOL",
        "--direction UP_OR_DOWN",
        "--asset-class EQUITY_OR_OPTION",
        "--maximum-acceptable-slippage-dollars MAXIMUM_ACCEPTABLE_SLIPPAGE",
        "--expected-unleveraged-buying-power-dollars PREVIEW_BP",
        "--preview-id PREVIEW_ID",
        "--preview-confirmed-at PREVIEW_TIMESTAMP",
        "--preview-account-key ending-7153",
        "--preview-instrument-key INSTRUMENT_KEY",
        "--preview-side BUY",
        "--preview-order-quantity QUANTITY",
        "--preview-limit-price WORST_LIMIT_PRICE",
        "--preview-equity-dollars PREVIEW_EQUITY",
        "--preview-current-gross-exposure-dollars PREVIEW_GROSS",
        "--preview-working-entry-notional-dollars PREVIEW_WORKING_ENTRY_NOTIONAL",
        "--preview-projected-cost-dollars PREVIEW_PROJECTED_COST",
        "--broker-ack-timeout-seconds 10",
        "risk_gate_authorization` byte-for-byte equal to `pretrade_risk_facts",
        "max($5, 1% of current broker-confirmed equity)",
        "submission_intent_at + 10 seconds",
        "submission_intent.intent_id",
        "broker_state.submission_unknown_resolutions",
        "ORDER_FOUND",
        "NO_ORDER_CONFIRMED",
        "entry_submission_intent_id",
        "entry_order_resolution_key",
        "add_submission_intent_id",
        "add_order_resolution_key",
        "RECONCILED_NO_ORDER",
        "pilots record FILE",
        "pilots list --pilot-id PILOT_ID --book-mode BOOK_MODE --limit 1",
        "REGISTERED_INSUFFICIENT_EVIDENCE",
    )
    for semantic in exact_grade_semantics:
        require(semantic in prompt, f"missing grade/change-control semantic: {semantic}")
    require(
        "pilots show --pilot-id" not in prompt,
        "prompt uses unsupported Pilot show filters instead of pilots list",
    )
    require(
        "entry-stop release --" not in prompt,
        "prompt exposes an unauthenticated emergency-stop release command",
    )
    require(
        "before writing any newer `risk record`" not in prompt,
        "prompt binds a found order before its required durable ORDER_FOUND resolution",
    )
    preview_sequence = prompt.index(
        "obtain a final Robinhood preview for the exact account"
    )
    entry_gate_sequence = prompt.index(
        "risk gate --account-key ending-7153 --session-date YYYY-MM-DD "
        "--expected-broker-confirmed-at TIMESTAMP --max-age-seconds 90 --entry-check"
    )
    submission_intent_sequence = prompt.index(
        "risk submission-unknown --authorization-id AUTHORIZATION_ID"
    )
    require(
        preview_sequence < entry_gate_sequence < submission_intent_sequence,
        "pre-submit order must be broker preview, entry risk gate, then durable intent",
    )
    require(
        "automatic next-session promotion under" not in prompt,
        "prompt still grants automatic production promotion",
    )
    require(
        "prompt-clarity, or non-authoritative metadata" not in prompt,
        "IMMEDIATE_SAFE still includes executable prompt changes",
    )
    protected_invariants = (
        "Trade only account ending 7153",
        "Robinhood is the sole authority",
        "Massive as data-only",
        "hard session loss lock triggers immediately and irreversibly",
        "primary daily attack objective is exactly +$150 net",
        "Gross exposure may never exceed actual account equity or unleveraged buying power",
        "full-quantity GTC stop-market protection",
        "Never average down",
        "Never widen the original catastrophe or thesis stop",
        "Never merge the draft PR automatically",
        "sole live and broker-authoritative Pilot is `titan_momentum_equity`",
        "There are no fixed Pilot silos, quotas, or capital budgets",
        "leaderboard is reporting-only",
        "Emergency-stop release is unavailable to this automation",
    )
    for invariant in protected_invariants:
        require(invariant in prompt, f"missing protected invariant: {invariant}")

    automation_sha256 = hashlib.sha256(automation_bytes).hexdigest()
    registry = json.loads(PILOT_REGISTRY.read_text(encoding="utf-8"))
    require(
        registry["registry_version"] == "titan_pilot_registry_2026-08-23_v1",
        "unexpected Pilot registry version",
    )
    require(registry["sole_live_pilot_id"] == PILOT_ID, "unexpected sole LIVE Pilot")
    require(
        registry["sole_broker_authority_pilot_id"] == PILOT_ID,
        "unexpected sole broker-authority Pilot",
    )
    require(registry["fixed_module_budgets"] is False, "fixed Pilot budgets enabled")
    require(
        registry["leaderboard_authority"] == "REPORTING_ONLY",
        "leaderboard grants non-reporting authority",
    )
    require(
        registry["truth_book_rules"]["cross_book_aggregation"] == "PROHIBITED",
        "registry permits cross-book aggregation",
    )
    contract_required = {
        "schema_version", "pilot_id", "display_name", "decision_contract_version",
        "strategy_version", "provenance", "status", "allowed_book_modes",
        "broker_authority", "trade_authority", "truth_authorities", "causal_inputs",
        "decision_rules", "instrument_scope", "lane_risk_translation",
        "cost_execution_model", "session_calendar_rules", "capital_policy",
        "record_identity", "suspension_criteria", "evidence_standard",
        "fact_sheet_rules", "promotion_policy", "known_failure_modes",
    }
    registry_contracts = registry["contracts"]
    require(len(registry_contracts) == 5, "Pilot registry must contain exactly five contracts")
    for item in registry_contracts:
        repo_contract = WORKSPACE / "work" / "repo-additions" / item["path"]
        staged_contract = STAGED / item["path"]
        require(repo_contract.is_file(), f"missing repository Pilot contract: {item['path']}")
        require(staged_contract.is_file(), f"missing staged Pilot contract: {item['path']}")
        require(
            repo_contract.read_bytes() == staged_contract.read_bytes(),
            f"staged Pilot contract differs: {item['path']}",
        )
        contract = json.loads(repo_contract.read_text(encoding="utf-8"))
        require(contract_required <= set(contract), f"incomplete Pilot contract: {item['pilot_id']}")
        require(contract["pilot_id"] == item["pilot_id"], "Pilot contract identity mismatch")
        require(
            contract["decision_contract_version"] == item["decision_contract_version"],
            "Pilot contract version mismatch",
        )
        require(
            file_sha256(repo_contract) == item["decision_contract_hash"],
            f"Pilot contract hash mismatch: {item['pilot_id']}",
        )
        require(contract["provenance"]["canonical_path"] == item["path"], "bad provenance path")
        require(contract["provenance"]["immutable_exact_bytes"] is True, "contract is mutable")
        require(contract["capital_policy"]["fixed_module_budget"] is False, "fixed budget found")
        require(
            contract["capital_policy"]["live_notional_range_pct"] == [0, 100],
            "Pilot contract changed dynamic notional range",
        )
        require(
            contract["capital_policy"]["leaderboard_can_reallocate_capital"] is False,
            "Pilot leaderboard may reallocate capital",
        )
        require(
            contract["record_identity"]["required_fields"]
            == ["pilot_id", "book_mode", "decision_contract_version", "decision_contract_hash"],
            "Pilot record identity fields differ",
        )
        require(
            contract["fact_sheet_rules"]["combined_performance"] is None,
            "Pilot contract permits combined book performance",
        )
        require(
            contract["fact_sheet_rules"]["authority"] == "REPORTING_ONLY",
            "Pilot fact sheet grants authority",
        )
        require(
            set(contract["fact_sheet_rules"]["required_metrics"])
            == set(CONTRACT_FACT_SHEET_METRIC_MAPPING),
            f"Pilot fact-sheet conceptual metrics differ: {item['pilot_id']}",
        )
        if item["pilot_id"] == PILOT_ID:
            require(item["status"] == "ACTIVE_LIVE", "incumbent Pilot is not ACTIVE_LIVE")
            require(item["broker_authority"] is True, "incumbent lost broker authority")
            require(item["trade_authority"] is True, "incumbent lost trade authority")
        else:
            require(item["status"] == "SHADOW_ONLY", "research Pilot is not SHADOW_ONLY")
            require(item["book_modes"] == ["SHADOW"], "research Pilot book is not SHADOW")
            require(item["broker_authority"] is False, "research Pilot has broker authority")
            require(item["trade_authority"] is False, "research Pilot has trade authority")
            require(contract["broker_authority"] is False, "shadow contract has broker authority")
            require(contract["trade_authority"] is False, "shadow contract has trade authority")

    require(
        file_sha256(LIVE_DECISION_CONTRACT)
        == "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21",
        "canonical live decision contract hash changed",
    )
    schema_names = (
        "pilot-contract-2026-08-23-v1.schema.json",
        "pilot-fact-sheet-2026-08-23-v1.schema.json",
        "pilot-leaderboard-2026-08-23-v1.schema.json",
        "live-pretrade-risk-facts-2026-08-23-v1.schema.json",
    )
    schemas: dict[str, dict] = {}
    for schema_name in schema_names:
        schema_text = (SCHEMA_ROOT / schema_name).read_text(encoding="utf-8")
        schemas[schema_name] = json.loads(schema_text)
        require("decision_contract_sha256" not in schema_text, f"stale hash field in {schema_name}")
    fact_sheet_schema = schemas["pilot-fact-sheet-2026-08-23-v1.schema.json"]
    fact_sheet_required = {
        "pilot_id", "pilot_name", "book_mode", "fact_sheet_version",
        "decision_contract_version", "decision_contract_hash", "policy_hash",
        "measured_through", "evidence_status", "metrics",
        "known_failure_modes", "evidence",
    }
    require(
        set(fact_sheet_schema["required"]) == fact_sheet_required,
        "Pilot fact-sheet schema differs from the runtime per-book payload",
    )
    require(
        fact_sheet_schema["properties"]["book_mode"]["enum"]
        == ["LIVE", "PAPER", "SHADOW"],
        "Pilot fact-sheet schema does not isolate truth books",
    )
    require(
        "performance" not in fact_sheet_schema["properties"],
        "Pilot fact sheet contains a stale cross-book performance object",
    )
    expected_metric_mapping = {
        key: list(value) for key, value in CONTRACT_FACT_SHEET_METRIC_MAPPING.items()
    }
    require(
        fact_sheet_schema["x-trader-brain-contract-metric-mapping"]
        == expected_metric_mapping,
        "Pilot fact-sheet conceptual/runtime metric mapping differs",
    )
    fact_sheet_metric_fields = set(
        fact_sheet_schema["$defs"]["metrics"]["required"]
    )
    for conceptual_name, targets in CONTRACT_FACT_SHEET_METRIC_MAPPING.items():
        for target in targets:
            if target.startswith("metrics."):
                require(
                    target.removeprefix("metrics.") in fact_sheet_metric_fields,
                    f"contract metric {conceptual_name} maps to a missing runtime metric",
                )
            else:
                require(
                    target in fact_sheet_required,
                    f"contract metric {conceptual_name} maps to a missing fact-sheet field",
                )
    storage_tree = ast.parse(STORAGE.read_text(encoding="utf-8"))
    runtime_fact_sheet_metric_fields: set[str] = set()
    for node in ast.walk(storage_tree):
        if isinstance(node, ast.FunctionDef) and node.name == "record_pilot_fact_sheet":
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                targets = {
                    target.id for target in statement.targets
                    if isinstance(target, ast.Name)
                }
                if targets.intersection({"analytical_metrics", "evidence_metrics"}):
                    runtime_fact_sheet_metric_fields.update(ast.literal_eval(statement.value))
            break
    require(
        runtime_fact_sheet_metric_fields == fact_sheet_metric_fields,
        "storage and versioned Pilot fact-sheet metric fields have drifted",
    )
    leaderboard_schema = schemas["pilot-leaderboard-2026-08-23-v1.schema.json"]
    require(
        set(leaderboard_schema["required"])
        == {
            "book_mode", "ranked_pilots", "unranked_pilots", "ranking_basis",
            "raw_pnl_or_win_rate_used", "books_combined", "reporting_only",
            "trade_authority", "capital_reallocation_authority",
        },
        "Pilot leaderboard schema differs from runtime output",
    )
    require(
        leaderboard_schema["properties"]["books_combined"]["const"] is False,
        "Pilot leaderboard schema permits cross-book rankings",
    )
    for authority_field, expected in (
        ("reporting_only", True),
        ("trade_authority", False),
        ("capital_reallocation_authority", False),
    ):
        require(
            leaderboard_schema["properties"][authority_field]["const"] is expected,
            f"Pilot leaderboard schema changes {authority_field}",
        )
    require(
        set(leaderboard_schema["$defs"]["metrics"]["required"])
        == fact_sheet_metric_fields,
        "leaderboard and Pilot fact-sheet metric fields have drifted",
    )
    pretrade_schema = schemas["live-pretrade-risk-facts-2026-08-23-v1.schema.json"]
    pretrade_required = {
        "schema_version", "authorization_id", "pilot_id", "book_mode",
        "decision_contract_version", "decision_contract_hash", "strategy_version",
        "account_key", "session_date", "broker_confirmed_at",
        "broker_snapshot_valid_until", "broker_snapshot_hash", "checked_at",
        "reservation_expires_at", "reservation_scope", "current_equity_dollars",
        "instrument_key", "symbol", "thesis_key", "direction", "asset_class",
        "risk_action", "preview_id", "preview_confirmed_at",
        "preview_account_key", "preview_instrument_key", "preview_side",
        "preview_order_quantity", "preview_limit_price", "preview_equity_dollars",
        "preview_current_gross_exposure_dollars",
        "preview_working_entry_notional_dollars", "preview_projected_cost_dollars",
        "reviewed_entry_price", "structural_stop_price", "quantity",
        "contract_multiplier", "modeled_execution_loss_dollars",
        "maximum_acceptable_slippage_dollars", "stress_tail_loss_dollars",
        "reviewed_notional_dollars", "estimated_slippage_dollars",
        "maximum_contractual_loss_dollars", "notional_pct_of_current_equity",
        "calculated_stop_defined_loss_dollars", "proposed_new_risk_dollars",
        "existing_open_downside_dollars", "existing_pending_risk_dollars",
        "execution_reserve_dollars", "unleveraged_buying_power_dollars",
        "expected_unleveraged_buying_power_dollars",
        "buying_power_mismatch_dollars", "buying_power_mismatch_tolerance_dollars",
        "buying_power_mismatch_detected", "emergency_entry_stop_generation",
        "emergency_entry_stop_state_hash", "broker_ack_timeout_seconds",
        "current_gross_exposure_dollars", "working_entry_notional_dollars",
        "broker_new_notional_capacity_dollars", "post_order_gross_exposure_dollars",
        "projected_remaining_buying_power_dollars", "uncredited_open_profit_dollars",
        "open_loss_gauge_degradation_dollars", "loss_lock_new_risk_capacity_dollars",
        "profit_floor_new_risk_capacity_dollars", "dynamic_new_risk_capacity_dollars",
        "account_day_loss_headroom_dollars", "submission_intent_at",
        "broker_ack_deadline_at",
    }
    require(
        set(pretrade_schema["required"]) == pretrade_required,
        "canonical live pretrade-risk schema fields differ from runtime contract",
    )
    runtime_pretrade_required: set[str] | None = None
    for node in ast.walk(storage_tree):
        if isinstance(node, ast.FunctionDef) and (
            node.name == "_validate_risk_gate_authorization"
        ):
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if any(
                    isinstance(target, ast.Name) and target.id == "required"
                    for target in statement.targets
                ):
                    runtime_pretrade_required = set(ast.literal_eval(statement.value))
                    break
            break
    require(
        runtime_pretrade_required == pretrade_required,
        "storage and versioned pretrade-risk schema required fields have drifted",
    )
    require(
        pretrade_schema["properties"]["schema_version"]["const"]
        == "titan_live_pretrade_risk_facts_2026-08-23_v1",
        "canonical pretrade-risk schema version differs",
    )
    require(
        pretrade_schema["properties"]["broker_ack_timeout_seconds"]["const"] == 10,
        "pretrade-risk schema changes the ten-second acknowledgment deadline",
    )
    for timestamp_field in ("submission_intent_at", "broker_ack_deadline_at"):
        require(
            pretrade_schema["properties"][timestamp_field]["type"]
            == ["string", "null"],
            f"{timestamp_field} must be null until submission intent is marked",
        )
    require(
        pretrade_schema["properties"]["maximum_contractual_loss_dollars"]["type"]
        == ["number", "null"],
        "maximum contractual loss must support the equity null sentinel",
    )
    maximum_contractual_loss_rules = {
        branch["if"]["properties"]["asset_class"]["const"]:
        branch["then"]["properties"]["maximum_contractual_loss_dollars"]
        for branch in pretrade_schema["allOf"]
    }
    require(
        maximum_contractual_loss_rules
        == {
            "EQUITY": {"const": None},
            "OPTION": {"type": "number", "minimum": 0},
        },
        "pretrade-risk schema does not enforce lane-specific contractual loss",
    )

    adaptation = json.loads(ADAPTATION.read_text(encoding="utf-8"))
    require(
        adaptation["canonical_live_pilot"]["decision_contract_hash"]
        == file_sha256(LIVE_DECISION_CONTRACT),
        "adaptation live contract hash mismatch",
    )
    adaptation_shadows = {
        item["pilot_id"]: item for item in adaptation["registered_shadow_pilots"]
    }
    require(len(adaptation_shadows) == 4, "adaptation must register four shadow Pilots")
    for item in registry_contracts:
        if item["pilot_id"] == PILOT_ID:
            continue
        shadow = adaptation_shadows[item["pilot_id"]]
        require(shadow["decision_contract_hash"] == item["decision_contract_hash"], "shadow hash mismatch")
        require(shadow["status"] == "REGISTERED_INSUFFICIENT_EVIDENCE", "shadow status overclaims")
        require(shadow["trade_authority"] is False, "shadow adaptation grants trade authority")
        require(shadow["risk_authorization_authority"] is False, "shadow adaptation grants risk authority")
        require(shadow["capital_allocation_authority"] is False, "shadow adaptation grants capital")

    final_hashes = adaptation["pending_final_integration_hashes"]
    require(
        final_hashes["canonical_automation_sha256"] == automation_sha256,
        "adaptation automation hash does not match staged policy",
    )
    require(
        final_hashes["runtime_storage_sha256"] == file_sha256(STORAGE),
        "adaptation storage hash does not match staged runtime",
    )
    require(
        final_hashes["runtime_cli_sha256"] == file_sha256(CLI),
        "adaptation CLI hash does not match staged runtime",
    )
    require(
        final_hashes["runtime_config_sha256"] == file_sha256(CONFIG_MODULE),
        "adaptation config module hash does not match staged runtime",
    )
    require(
        final_hashes["runtime_massive_sha256"] == file_sha256(MASSIVE_MODULE),
        "adaptation Massive module hash does not match staged runtime",
    )
    require(
        final_hashes["validator_sha256"] == file_sha256(Path(__file__)),
        "adaptation validator hash does not match this validator",
    )
    require(
        final_hashes["protected_semantics_sha256"] == semantics_sha256,
        "adaptation protected-semantics hash does not match policy",
    )
    deployment_status = adaptation["deployment"]["status"]
    require(
        deployment_status in {"pending", "deployed"},
        "adaptation deployment status must be pending or deployed",
    )
    if deployment_status == "deployed":
        require(
            file_sha256(INSTALLED_STORAGE) == file_sha256(STORAGE),
            "installed storage module differs from the reviewed artifact",
        )
        require(
            file_sha256(INSTALLED_CLI) == file_sha256(CLI),
            "installed CLI module differs from the reviewed artifact",
        )
        require(
            file_sha256(INSTALLED_AUTOMATION) == automation_sha256,
            "installed automation differs from the reviewed artifact",
        )

    connection = sqlite3.connect(
        f"file:{DATABASE}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    if deployment_status == "deployed":
        installed_grade_schema = {
            str(row["name"])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type IN ('table','trigger')
                     AND name IN (
                       'daily_performance_grades',
                       'daily_performance_grades_no_update',
                       'daily_performance_grades_no_delete'
                     )"""
            ).fetchall()
        }
        require(
            installed_grade_schema == {
                "daily_performance_grades",
                "daily_performance_grades_no_update",
                "daily_performance_grades_no_delete",
            },
            "installed daily-grade schema or append-only triggers are incomplete",
        )
    config_payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    live_operational_contract = config_payload["live_operational_contract"]
    require(
        live_operational_contract["pretrade_risk_facts_schema_version"]
        == "titan_live_pretrade_risk_facts_2026-08-23_v1",
        "runtime config uses a stale pretrade-risk schema",
    )
    require(
        live_operational_contract["pre_submit_sequence"]
        == [
            "FRESH_BROKER_RECONCILIATION", "EMERGENCY_ENTRY_STOP_STATUS",
            "EXACT_ROBINHOOD_PREVIEW", "PREVIEW_COMPARISON",
            "ENTRY_RISK_GATE_AND_RESERVATION", "DURABLE_SUBMISSION_INTENT",
            "BROKER_ORDER_REQUEST",
        ],
        "runtime config changes the TOCTOU-safe pre-submit sequence",
    )
    require(
        set(live_operational_contract["required_preview_fields"])
        == {
            "preview_id", "preview_confirmed_at", "preview_account_key",
            "preview_instrument_key", "preview_side", "preview_order_quantity",
            "preview_limit_price", "preview_equity_dollars",
            "preview_current_gross_exposure_dollars",
            "preview_working_entry_notional_dollars", "preview_projected_cost_dollars",
        },
        "runtime config omits exact Robinhood preview facts",
    )
    require(
        live_operational_contract["broker_ack_deadline_anchor"]
        == "SUBMISSION_INTENT_AT"
        and live_operational_contract["submission_intent_append_only"] is True
        and live_operational_contract["submission_unknown_resolution_states"]
        == ["ORDER_FOUND", "NO_ORDER_CONFIRMED"]
        and live_operational_contract[
            "order_found_remains_serialized_until_campaign_bind"
        ] is True,
        "runtime config weakens submission-intent serialization",
    )
    require(
        live_operational_contract["emergency_entry_stop_release_interface"]
        == "UNAVAILABLE_IN_AUTOMATION_RUNTIME",
        "runtime config exposes emergency-entry-stop release",
    )
    require(
        live_operational_contract["emergency_entry_stop_release_authority"]
        == "STOPPED_SERVICE_EXPLICIT_USER_APPROVAL_ONLY",
        "runtime config weakens protected release authority",
    )
    cli_text = CLI.read_text(encoding="utf-8")
    require(
        "cmd_entry_stop_release" not in cli_text
        and 'entry_stop_commands.add_parser(\n        "release"' not in cli_text,
        "runtime CLI still exposes emergency-entry-stop release",
    )
    for required_cli_flag in (
        "--instrument-key", "--symbol", "--direction", "--asset-class",
        "--thesis-key", "--risk-action", "--pilot-id", "--book-mode",
        "--decision-contract-version", "--decision-contract-hash",
        "--reviewed-entry-price", "--structural-stop-price", "--quantity",
        "--contract-multiplier", "--modeled-execution-loss-dollars",
        "--maximum-acceptable-slippage-dollars", "--stress-tail-loss-dollars",
        "--existing-open-downside-dollars", "--existing-pending-risk-dollars",
        "--execution-reserve-dollars",
        "--expected-unleveraged-buying-power-dollars", "--preview-id",
        "--preview-confirmed-at", "--preview-account-key",
        "--preview-instrument-key", "--preview-side", "--preview-order-quantity",
        "--preview-limit-price", "--preview-equity-dollars",
        "--preview-current-gross-exposure-dollars",
        "--preview-working-entry-notional-dollars",
        "--preview-projected-cost-dollars", "--broker-ack-timeout-seconds",
    ):
        require(
            f'"{required_cli_flag}"' in cli_text,
            f"runtime CLI omits required entry-gate argument {required_cli_flag}",
        )
    config = RuntimeConfig.load(CONFIG)
    require(config.pilot_id == PILOT_ID, "watcher Pilot identity differs")
    require(config.book_mode == BOOK_MODE, "watcher book mode is not SHADOW")
    require(
        config.decision_contract_version == DECISION_CONTRACT_VERSION,
        "watcher decision contract version differs",
    )
    require(
        config.decision_contract_hash == file_sha256(LIVE_DECISION_CONTRACT),
        "watcher decision contract hash differs",
    )
    require(
        not any(
            (
                config.pilot_trade_authority,
                config.pilot_broker_authority,
                config.pilot_risk_authorization_authority,
                config.pilot_buying_power_reservation_authority,
                config.pilot_capital_allocation_authority,
            )
        ),
        "Massive watcher Pilot attribution grants authority",
    )

    candidate_rows = connection.execute(
        "SELECT symbol, observed_at, payload_json FROM candidates ORDER BY symbol"
    ).fetchall()
    old_plan_rows = connection.execute(
        """
        SELECT symbol, status, payload_json
        FROM (
            SELECT
                symbol,
                status,
                payload_json,
                ROW_NUMBER() OVER (
                    PARTITION BY symbol
                    ORDER BY created_at DESC, observed_at DESC, plan_id DESC
                ) AS row_number
            FROM prepared_trade_plans
            WHERE symbol IN (SELECT symbol FROM candidates)
        )
        WHERE row_number = 1
        """
    ).fetchall()
    old_plans = {
        row["symbol"]: {
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in old_plan_rows
    }

    new_plans: dict[str, dict] = {}
    for row in candidate_rows:
        signal = json.loads(row["payload_json"])
        plan = build_preliminary_trade_plan(signal, config)
        new_plans[row["symbol"]] = plan

        symbol = row["symbol"]
        require(plan["trade_authority"] is False, f"{symbol}: plan grants trade authority")
        require(plan["pilot_id"] == PILOT_ID, f"{symbol}: plan Pilot identity mismatch")
        require(plan["book_mode"] == BOOK_MODE, f"{symbol}: plan book is not SHADOW")
        require(
            plan["decision_contract_version"] == DECISION_CONTRACT_VERSION,
            f"{symbol}: plan contract version mismatch",
        )
        require(
            plan["decision_contract_hash"] == file_sha256(LIVE_DECISION_CONTRACT),
            f"{symbol}: plan contract hash mismatch",
        )
        for authority_field in (
            "trade_authority",
            "broker_authority",
            "risk_authorization_authority",
            "buying_power_reservation_authority",
            "capital_allocation_authority",
        ):
            require(plan[authority_field] is False, f"{symbol}: {authority_field} enabled")
        require(
            plan["broker_review_complete"] is False,
            f"{symbol}: preliminary plan claims broker review",
        )
        require(
            plan["protection_confirmed"] is False,
            f"{symbol}: preliminary plan claims protection",
        )
        require(
            not any(
                blocker.startswith("weighted input unresolved:")
                for blocker in plan["blockers"]
            ),
            f"{symbol}: contextual input is still a hard blocker",
        )
        require(
            not any(
                "fresh independently verified catalyst" in blocker
                for blocker in plan["blockers"]
            ),
            f"{symbol}: fresh catalyst is still mandatory",
        )
        require(
            plan["policy_version"] == config.policy_version,
            f"{symbol}: policy version mismatch",
        )
        require(
            plan["sizing_policy_version"] == config.sizing_policy.version,
            f"{symbol}: sizing policy version mismatch",
        )
        require(plan["preliminary_quantity_cap"] is None, f"{symbol}: fixed quantity cap")
        require(plan["preliminary_allocation_cap"] is None, f"{symbol}: fixed allocation cap")
        require(plan["preliminary_risk_cap"] is None, f"{symbol}: fixed risk cap")
        require(
            plan["risk_campaign"]["fixed_initial_risk_dollars"] is None,
            f"{symbol}: fixed initial dollar risk",
        )
        require(
            plan["risk_campaign"]["fixed_campaign_risk_cap_dollars"] is None,
            f"{symbol}: fixed campaign dollar risk",
        )
        require(
            plan["risk_campaign"]["account_day_loss_limit_dollars"] == 100.0,
            f"{symbol}: account-day loss limit changed",
        )
        require(
            plan["initial_entry_allocation_policy"]["initial_allocation_pct_range"]
            == [0, 100],
            f"{symbol}: initial allocation range changed",
        )
        require(
            plan["initial_entry_allocation_policy"]["full_initial_allocation_allowed"]
            is True,
            f"{symbol}: full initial allocation unexpectedly disabled",
        )
        require(
            plan["initial_entry_allocation_policy"]["initial_entry_requires_profit_funding"]
            is False,
            f"{symbol}: initial allocation incorrectly requires profit funding",
        )
        require(
            plan["initial_entry_allocation_policy"]["initial_entry_requires_staging"]
            is False,
            f"{symbol}: initial entry incorrectly requires staging",
        )
        require(
            plan["initial_entry_allocation_policy"]["adds_optional"] is True,
            f"{symbol}: adds became mandatory",
        )
        require(
            plan["position_notional_policy"]["max_unleveraged_buying_power_fraction"]
            == 1.0,
            f"{symbol}: unleveraged notional fraction changed",
        )
        require(
            plan["position_notional_policy"]["leverage_allowed"] is False,
            f"{symbol}: leverage unexpectedly allowed",
        )
        require(
            plan["add_policy"]["risk_constraint_logic"] == "OR",
            f"{symbol}: add risk constraint changed",
        )
        require("build_tranches_pct" not in plan, f"{symbol}: fixed tranches returned")
        if plan["risk_per_share"] is not None:
            risk = plan["risk_per_share"]
            entry = plan["reference_entry_price"]
            require(
                round(plan["t1"] - entry, 6) == round(risk, 6),
                f"{symbol}: T1 is not 1R",
            )
            require(
                round(plan["t2"] - entry, 6) == round(2 * risk, 6),
                f"{symbol}: T2 is not 2R",
            )
            require(
                round(plan["t3"] - entry, 6) == round(3 * risk, 6),
                f"{symbol}: T3 is not 3R",
            )
            require(
                plan["quantity_status"] == "BROKER_CONFIRMED_LIMITS_REQUIRED",
                f"{symbol}: preliminary quantity claims authority",
            )

    old_status = Counter(item["status"] for item in old_plans.values())
    new_status = Counter(item["status"] for item in new_plans.values())
    old_context_blockers = sum(
        sum(
            blocker.startswith("weighted input unresolved:")
            for blocker in item["payload"].get("blockers", [])
        )
        for item in old_plans.values()
    )
    removed_catalyst_blockers = sum(
        any(
            "fresh independently verified catalyst" in blocker
            for blocker in item["payload"].get("blockers", [])
        )
        for item in old_plans.values()
    )
    context_fields = sum(len(plan["context_missing"]) for plan in new_plans.values())
    executable_reference_plans = sum(
        plan["risk_per_share"] is not None for plan in new_plans.values()
    )

    result = {
        "mode": "read_only_immutable_replay",
        "database": str(DATABASE),
        "candidate_count": len(candidate_rows),
        "old_latest_plan_count": len(old_plans),
        "old_status_counts": dict(sorted(old_status.items())),
        "new_status_counts": dict(sorted(new_status.items())),
        "old_context_blockers_removed": old_context_blockers,
        "old_under5_catalyst_blockers_removed": removed_catalyst_blockers,
        "new_context_missing_fields_preserved": context_fields,
        "plans_with_complete_reference_geometry": executable_reference_plans,
        "trade_authority_count": sum(plan["trade_authority"] for plan in new_plans.values()),
        "policy_version": config.policy_version,
        "sizing_policy_version": config.sizing_policy.version,
        "targets": "fixed_1R_2R_3R",
        "initial_allocation_pct_range": [0, 100],
        "fixed_dollar_caps": None,
        "adds_optional": True,
        "checks": "PASS",
        "strategy_version": STRATEGY_VERSION,
        "pilot_id": PILOT_ID,
        "watcher_book_mode": BOOK_MODE,
        "live_book_mode": LIVE_BOOK_MODE,
        "decision_contract_version": DECISION_CONTRACT_VERSION,
        "decision_contract_hash": file_sha256(LIVE_DECISION_CONTRACT),
        "pilot_registry_sha256": file_sha256(PILOT_REGISTRY),
        "registered_shadow_pilot_count": 4,
        "shadow_pilot_trade_authority_count": 0,
        "fixed_pilot_budgets": False,
        "leaderboard_authority": "REPORTING_ONLY",
        "broker_ack_deadline_seconds": 10,
        "buying_power_mismatch_tolerance": "max($5,1%_broker_confirmed_equity)",
        "emergency_entry_stop_release": "UNAVAILABLE_IN_AUTOMATION_RUNTIME",
        "grade_rubric_version": GRADE_RUBRIC_VERSION,
        "automation_sha256": automation_sha256,
        "repository_automation_sha256": hashlib.sha256(
            repo_automation_bytes
        ).hexdigest(),
        "protected_semantics_sha256": semantics_sha256,
        "storage_sha256": file_sha256(STORAGE),
        "cli_sha256": file_sha256(CLI),
        "config_sha256": file_sha256(CONFIG_MODULE),
        "massive_sha256": file_sha256(MASSIVE_MODULE),
        "daily_grade_process_weight_pct": 80,
        "daily_grade_outcome_weight_pct": 20,
        "post_grade_production_activation": "DISABLED_UNTIL_DURABLE_GUARD_LEASE",
        "post_grade_change_tiers": [
            "IMMEDIATE_SAFE", "SHADOW_FIRST", "PROTECTED_USER_ONLY"
        ],
    }
    connection.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
