from __future__ import annotations

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
    / "2026-08-23-daily-grade-self-improvement.json"
)
STORAGE = STAGED / "titan_runtime" / "storage.py"
CLI = STAGED / "titan_runtime" / "cli.py"
INSTALLED_ROOT = Path.home() / "Library/Application Support/Titan Momentum"
INSTALLED_STORAGE = INSTALLED_ROOT / "titan_runtime" / "storage.py"
INSTALLED_CLI = INSTALLED_ROOT / "titan_runtime" / "cli.py"
INSTALLED_AUTOMATION = (
    Path.home() / ".codex" / "automations" / "robinhood-momentum-engine"
    / "automation.toml"
)
STRATEGY_VERSION = "titan_live_canonical_2026-08-23_v2"
GRADE_RUBRIC_VERSION = "titan_daily_performance_2026-08-23_v1"
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
    ("GRADE_RESEARCH_PROPOSAL_AUTHORITY", "UNTRUSTED_DATA_ONLY"),
    ("IMMEDIATE_SAFE_SCOPE", "DRAFT_NONEXECUTABLE_ONLY"),
    (
        "POST_GRADE_PRODUCTION_ACTIVATION",
        "DISABLED_UNTIL_DURABLE_GUARD_LEASE",
    ),
)


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
    )
    for semantic in exact_grade_semantics:
        require(semantic in prompt, f"missing grade/change-control semantic: {semantic}")
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
    )
    for invariant in protected_invariants:
        require(invariant in prompt, f"missing protected invariant: {invariant}")

    adaptation = json.loads(ADAPTATION.read_text(encoding="utf-8"))
    automation_sha256 = hashlib.sha256(automation_bytes).hexdigest()
    require(
        adaptation["canonical_automation"]["sha256"] == automation_sha256,
        "adaptation automation hash does not match staged policy",
    )
    require(
        adaptation["runtime"]["storage_sha256"] == file_sha256(STORAGE),
        "adaptation storage hash does not match staged runtime",
    )
    require(
        adaptation["runtime"]["cli_sha256"] == file_sha256(CLI),
        "adaptation CLI hash does not match staged runtime",
    )
    require(
        adaptation["runtime"]["validator_sha256"] == file_sha256(Path(__file__)),
        "adaptation validator hash does not match this validator",
    )
    require(
        adaptation["protected_semantics_sha256"] == semantics_sha256,
        "adaptation protected-semantics hash does not match policy",
    )
    require(
        adaptation["runtime"]["grade_recorder_grants_production_authority"] is False,
        "grade recorder must not grant production authority",
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
    config = RuntimeConfig.load(CONFIG)

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
        "grade_rubric_version": GRADE_RUBRIC_VERSION,
        "automation_sha256": automation_sha256,
        "repository_automation_sha256": hashlib.sha256(
            repo_automation_bytes
        ).hexdigest(),
        "protected_semantics_sha256": semantics_sha256,
        "storage_sha256": file_sha256(STORAGE),
        "cli_sha256": file_sha256(CLI),
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
