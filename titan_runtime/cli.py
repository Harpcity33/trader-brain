from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .config import RuntimeConfig
from .massive import MassiveREST, ProcessLock, TitanWatcher, load_api_key, within_runtime_window
from .storage import Store


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "titan-massive.json"


def output(value: object, pretty: bool = True) -> None:
    print(json.dumps(value, indent=2 if pretty else None, sort_keys=pretty, default=str))


def load(args: argparse.Namespace) -> RuntimeConfig:
    return RuntimeConfig.load(args.config)


def cmd_init(args: argparse.Namespace) -> int:
    config = load(args)
    store = Store(config.database_path)
    store.set_metadata("runtime_mode", config.mode)
    store.set_metadata("initialized_by", "titan_runtime.cli")
    store.close()
    output({"status": "initialized", "mode": config.mode, "database": str(config.database_path)})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load(args)
    checks: dict[str, object] = {
        "mode": config.mode,
        "websocket_url": config.websocket_url,
        "database_parent_exists": config.database_path.parent.exists(),
        "api_key_source": "unavailable",
        "api_key_present": False,
        "broker_capability_present": False,
    }
    try:
        key = load_api_key(config.keychain_service)
        checks["api_key_present"] = bool(key)
        checks["api_key_source"] = "environment" if os.environ.get("MASSIVE_API_KEY") else "macOS Keychain"
    except Exception as exc:
        checks["key_error"] = str(exc)
    try:
        store = Store(config.database_path)
        store.set_health("doctor", "healthy", {"database_write": True})
        store.close()
        checks["database_write"] = True
    except Exception as exc:
        checks["database_write"] = False
        checks["database_error"] = str(exc)
    checks["ready"] = bool(checks["api_key_present"] and checks.get("database_write"))
    output(checks)
    return 0 if checks["ready"] else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    config = load(args)
    key = load_api_key(config.keychain_service)
    rows = MassiveREST(config, key).full_snapshot()
    store = Store(config.database_path)
    count = store.upsert_snapshots(rows)
    top = store.top_snapshot_symbols(20)
    store.set_health("massive_rest", "healthy", {"snapshot_tickers": count})
    store.close()
    output({"status": "ok", "tickers": count, "top_gap_symbols": top})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = load(args)
    now_et = datetime.now(ZoneInfo(config.timezone))
    if not args.ignore_schedule and not within_runtime_window(config, now_et):
        output({"status": "outside_runtime_window", "current_time_et": now_et.isoformat()})
        return 0
    key = load_api_key(config.keychain_service)
    lock_path = config.database_path.parent / "titan-massive.lock"
    with ProcessLock(lock_path):
        TitanWatcher(config, key).run(
            max_seconds=args.seconds,
            stop_at_window_end=not args.ignore_schedule,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load(args)
    store = Store(config.database_path)
    value = {
        "mode": config.mode,
        "health": store.health(),
        "pending_event_count": len(store.pending_events(1000)),
        "leaders": [
            {
                "symbol": row["symbol"], "state": row["state"], "lane": row["lane"],
                "signal_strength": row["signal_strength"], "price": row["price"],
                "gap_pct": row["gap_pct"], "dollar_volume": row["dollar_volume"],
                "base_high": row["base_high"], "support": row["support"],
                "spread_pct": row["spread_pct"], "observed_at": row["observed_at"],
                "weighted_opportunity_score": json.loads(row["payload_json"]).get(
                    "weighted_opportunity_score"
                ),
                "modeled_move_capacity_pct": json.loads(row["payload_json"]).get(
                    "modeled_move_capacity_pct"
                ),
            }
            for row in store.leaderboard(args.limit)
        ],
    }
    store.close()
    output(value)
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    coverage = store.snapshot_coverage()
    coverage["promoted_candidates"] = len(store.all_candidate_symbols())
    coverage["prepared_plans"] = len(store.latest_prepared_trade_plans(100000))
    coverage["trade_authority"] = False
    store.close()
    output(coverage)
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    plans = store.latest_prepared_trade_plans(args.limit)
    store.close()
    output(plans)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    config = load(args)
    store = Store(config.database_path)
    if args.ack:
        decision_id = None
        if args.decision:
            details = json.loads(args.details_json) if args.details_json else {}
            if not isinstance(details, dict):
                raise ValueError("--details-json must decode to an object")
            decision_id = store.record_event_decision(
                args.ack, args.decision, args.reason or "Decision recorded before acknowledgement.", details
            )
        acknowledged = store.acknowledge_event(args.ack)
        output({
            "acknowledged": acknowledged,
            "event_id": args.ack,
            "decision_id": decision_id,
        })
        store.close()
        return 0 if acknowledged else 1
    events = store.pending_events(args.limit)
    store.close()
    if args.jsonl:
        for event in events:
            print(json.dumps(event, separators=(",", ":"), default=str))
    else:
        output(events)
    return 0


def cmd_decisions_list(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.event_decisions(args.limit)
    store.close()
    output(rows)
    return 0


def read_input_file(path_value: str) -> tuple[str, dict[str, object] | None, str]:
    path = Path(path_value).expanduser().resolve()
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("JSON input must be an object")
        return content, parsed, "json"
    return content, None, "markdown"


def cmd_lessons_ingest(args: argparse.Namespace) -> int:
    config = load(args)
    content, structured, content_format = read_input_file(args.file)
    lesson_date = args.date or datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    store = Store(config.database_path)
    lesson_id = store.ingest_lesson(
        lesson_date=lesson_date,
        source=args.source,
        content_text=content,
        content_format=content_format,
        structured=structured,
    )
    store.close()
    output({"status": "ingested", "lesson_id": lesson_id, "lesson_date": lesson_date})
    return 0


def cmd_lessons_latest(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    lessons = store.latest_lessons(args.limit)
    store.close()
    output(lessons)
    return 0


def cmd_changes_propose(args: argparse.Namespace) -> int:
    _, payload, _ = read_input_file(args.file)
    if payload is None:
        raise ValueError("strategy change proposals must be JSON")
    store = Store(load(args).database_path)
    change_id = store.propose_strategy_change(payload)
    store.close()
    output({
        "status": "proposed",
        "change_id": change_id,
        "production_change": False,
    })
    return 0


def cmd_changes_list(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    changes = store.strategy_changes(args.limit)
    store.close()
    output(changes)
    return 0


def cmd_research_plan(args: argparse.Namespace) -> int:
    _, payload, _ = read_input_file(args.file)
    if payload is None:
        raise ValueError("entry plans must be JSON")
    store = Store(load(args).database_path)
    plan_id = store.create_entry_plan(payload)
    store.close()
    output({
        "status": "entry_alternatives_frozen",
        "plan_id": plan_id,
        "trade_authority": False,
    })
    return 0


def cmd_research_outcome(args: argparse.Namespace) -> int:
    _, payload, _ = read_input_file(args.file)
    if payload is None:
        raise ValueError("entry outcomes must be JSON")
    store = Store(load(args).database_path)
    store.attach_entry_outcome(args.plan_id, payload)
    store.close()
    output({"status": "outcome_attached_and_locked", "plan_id": args.plan_id})
    return 0


def cmd_research_report(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.entry_comparisons(args.limit)
    store.close()
    output(rows)
    return 0


def cmd_campaign_record(args: argparse.Namespace) -> int:
    _, payload, _ = read_input_file(args.file)
    if payload is None:
        raise ValueError("position campaign state must be JSON")
    store = Store(load(args).database_path)
    campaign_id = store.upsert_position_campaign(payload)
    rows = [
        row for row in store.position_campaigns(include_terminal=True)
        if row["campaign_id"] == campaign_id
    ]
    store.close()
    output({
        "status": "local_management_state_recorded",
        "campaign_id": campaign_id,
        "campaign": rows[0] if rows else None,
        "trade_authority": False,
        "broker_confirmation_required": True,
    })
    return 0


def cmd_campaign_list(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.position_campaigns(include_terminal=args.all)
    store.close()
    output(rows)
    return 0


def cmd_campaign_history(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.position_campaign_events(args.campaign_id)
    store.close()
    output(rows)
    return 0


def cmd_risk_record(args: argparse.Namespace) -> int:
    _, payload, _ = read_input_file(args.file)
    if payload is None:
        raise ValueError("risk session state must be JSON")
    store = Store(load(args).database_path)
    row = store.upsert_risk_session(payload)
    store.close()
    output({
        "status": "broker_confirmed_risk_session_recorded",
        "risk_session": row,
        "trade_authority": False,
    })
    return 0


def cmd_risk_list(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.risk_sessions(args.limit)
    store.close()
    output(rows)
    return 0


def cmd_risk_release(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    row = store.release_risk_authorization(args.authorization_id, args.reason)
    store.close()
    output({
        "status": "risk_authorization_released",
        "authorization": row,
        "trade_authority": False,
    })
    return 0


def cmd_risk_authorizations(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    rows = store.risk_authorizations(args.limit)
    store.close()
    output(rows)
    return 0


def cmd_risk_gate(args: argparse.Namespace) -> int:
    store = Store(load(args).database_path)
    row = store.risk_session(args.account_key, args.session_date)
    if row is None:
        store.close()
        output({
            "new_entries_allowed": False,
            "reason": "broker-confirmed risk session is missing",
            "trade_authority": False,
        })
        return 2
    expected_raw = str(args.expected_broker_confirmed_at).replace("Z", "+00:00")
    try:
        expected = datetime.fromisoformat(expected_raw)
    except ValueError as error:
        raise ValueError(
            "--expected-broker-confirmed-at must be an ISO-8601 timestamp"
        ) from error
    if expected.tzinfo is None or expected.utcoffset() is None:
        raise ValueError("--expected-broker-confirmed-at must include a UTC offset")
    expected_utc = expected.astimezone(timezone.utc)
    actual = datetime.fromisoformat(str(row["broker_confirmed_at"]))
    now_utc = datetime.now(timezone.utc)
    age_seconds = (now_utc - actual).total_seconds()
    reasons = []
    if args.max_age_seconds <= 5 or args.max_age_seconds > 90:
        reasons.append("broker snapshot max age must be within 6..90 seconds")
    if actual != expected_utc:
        reasons.append("ledger snapshot does not match the just-confirmed broker timestamp")
    if age_seconds < -15:
        reasons.append("broker snapshot timestamp is in the future")
    if age_seconds > args.max_age_seconds:
        reasons.append("broker snapshot is stale")
    broker_state = row.get("broker_state") or {}
    for field in (
        "account_state_readable",
        "orders_reconciled",
        "positions_reconciled",
    ):
        if broker_state.get(field) is not True:
            reasons.append(f"broker evidence missing {field}")
    if row["loss_lock"]:
        reasons.append("irreversible account-day loss lock is active")
    if (
        row["profit_objective_reached"]
        and float(row["post_objective_new_risk_buffer"] or 0) <= 0
    ):
        reasons.append("post-objective new-risk buffer is not positive")
    entry_check = bool(getattr(args, "entry_check", False))
    raw_dynamic_inputs = {
        "reviewed_entry_price": getattr(args, "reviewed_entry_price", None),
        "structural_stop_price": getattr(args, "structural_stop_price", None),
        "quantity": getattr(args, "quantity", None),
        "contract_multiplier": getattr(args, "contract_multiplier", None),
        "modeled_execution_loss_dollars": getattr(
            args, "modeled_execution_loss_dollars", None
        ),
        "stress_tail_loss_dollars": getattr(
            args, "stress_tail_loss_dollars", None
        ),
        "existing_open_downside_dollars": getattr(
            args, "existing_open_downside_dollars", None
        ),
        "existing_pending_risk_dollars": getattr(
            args, "existing_pending_risk_dollars", None
        ),
        "execution_reserve_dollars": getattr(
            args, "execution_reserve_dollars", None
        ),
    }
    supplied_dynamic_inputs = {
        field for field, value in raw_dynamic_inputs.items() if value is not None
    }
    if entry_check:
        missing = [
            field for field, value in raw_dynamic_inputs.items() if value is None
        ]
        for field in (
            "instrument_key", "thesis_key", "risk_action",
        ):
            if getattr(args, field, None) in (None, ""):
                missing.append(field)
        if missing:
            raise ValueError(
                "--entry-check requires explicit values for: " + ", ".join(missing)
            )
    elif supplied_dynamic_inputs:
        raise ValueError(
            "dynamic risk inputs are valid only with --entry-check"
        )
    reviewed_entry_price = float(raw_dynamic_inputs["reviewed_entry_price"] or 0)
    structural_stop_price = float(raw_dynamic_inputs["structural_stop_price"] or 0)
    quantity = float(raw_dynamic_inputs["quantity"] or 0)
    contract_multiplier = float(raw_dynamic_inputs["contract_multiplier"] or 0)
    modeled_execution_loss = float(
        raw_dynamic_inputs["modeled_execution_loss_dollars"] or 0
    )
    stress_tail_loss = float(raw_dynamic_inputs["stress_tail_loss_dollars"] or 0)
    existing_open_downside = float(
        raw_dynamic_inputs["existing_open_downside_dollars"] or 0
    )
    existing_pending_risk = float(
        raw_dynamic_inputs["existing_pending_risk_dollars"] or 0
    )
    execution_reserve = float(raw_dynamic_inputs["execution_reserve_dollars"] or 0)
    raw_numeric_values = {
        "reviewed_entry_price": reviewed_entry_price,
        "structural_stop_price": structural_stop_price,
        "quantity": quantity,
        "contract_multiplier": contract_multiplier,
        "modeled_execution_loss_dollars": modeled_execution_loss,
        "stress_tail_loss_dollars": stress_tail_loss,
        "existing_open_downside_dollars": existing_open_downside,
        "existing_pending_risk_dollars": existing_pending_risk,
        "execution_reserve_dollars": execution_reserve,
    }
    for field, value in raw_numeric_values.items():
        if not isfinite(value) or value < 0:
            raise ValueError(f"{field} must be a finite nonnegative number")
    reviewed_notional = 0.0
    stop_defined_loss = 0.0
    proposed_new_risk = 0.0
    if entry_check:
        if reviewed_entry_price <= 0 or structural_stop_price <= 0:
            raise ValueError(
                "entry and structural-stop prices must be positive for --entry-check"
            )
        if structural_stop_price >= reviewed_entry_price:
            raise ValueError(
                "--structural-stop-price must be below --reviewed-entry-price"
            )
        if quantity <= 0:
            raise ValueError("--quantity must be positive for --entry-check")
        if contract_multiplier not in {1.0, 100.0}:
            raise ValueError("--contract-multiplier must be exactly 1 or 100")
        if execution_reserve < 5:
            raise ValueError(
                "--execution-reserve-dollars must be at least 5 for --entry-check"
            )
        reviewed_notional = reviewed_entry_price * quantity * contract_multiplier
        stop_defined_loss = (
            (reviewed_entry_price - structural_stop_price)
            * quantity
            * contract_multiplier
            + modeled_execution_loss
        )
        proposed_new_risk = max(stop_defined_loss, stress_tail_loss)
        if proposed_new_risk <= 0:
            raise ValueError("calculated proposed new risk must be positive")
    dynamic_inputs = {
        **raw_numeric_values,
        "reviewed_notional_dollars": reviewed_notional,
        "calculated_stop_defined_loss_dollars": stop_defined_loss,
        "proposed_new_risk_dollars": proposed_new_risk,
    }

    broker_balance_fields = (
        "unleveraged_buying_power_dollars",
        "current_gross_exposure_dollars",
        "working_entry_notional_dollars",
    )
    broker_balances = {}
    for field in broker_balance_fields:
        raw_value = broker_state.get(field)
        if entry_check and raw_value is None:
            raise ValueError(f"broker risk snapshot is missing {field}")
        value = float(raw_value or 0)
        if not isfinite(value) or value < 0:
            raise ValueError(f"broker {field} must be a finite nonnegative number")
        broker_balances[field] = value
    equity_notional_capacity = max(
        0.0,
        float(row["current_equity"])
        - broker_balances["current_gross_exposure_dollars"]
        - broker_balances["working_entry_notional_dollars"],
    )
    broker_new_notional_capacity = min(
        broker_balances["unleveraged_buying_power_dollars"],
        equity_notional_capacity,
    )
    post_order_gross_exposure = (
        broker_balances["current_gross_exposure_dollars"]
        + broker_balances["working_entry_notional_dollars"]
        + reviewed_notional
    )
    if entry_check and reviewed_notional > broker_new_notional_capacity + 0.005:
        reasons.append(
            "reviewed notional exceeds fresh unleveraged buying power or the "
            "no-leverage account-equity gross-exposure ceiling"
        )

    # LOSS_GAUGE intentionally does not credit unrealized gains.  Subtract only
    # the portion of current-mark-to-stop downside that can worsen LOSS_GAUGE;
    # otherwise the same unrealized gain would be withheld twice.  Pending and
    # proposed orders have no mark embedded in current equity, so their full
    # conservative risk is reserved.
    uncredited_open_profit = max(
        0.0, float(row["account_day_pnl"]) - float(row["loss_gauge"])
    )
    open_loss_gauge_degradation = max(
        0.0, existing_open_downside - uncredited_open_profit
    )
    loss_lock_capacity = max(
        0.0,
        float(row["loss_headroom_to_lock"])
        - open_loss_gauge_degradation
        - existing_pending_risk
        - execution_reserve,
    )
    floor_capacity = None
    controlling_headroom = float(row["loss_headroom_to_lock"])
    if row["profit_objective_reached"]:
        floor_capacity = max(
            0.0,
            float(row["post_objective_new_risk_buffer"] or 0)
            - existing_open_downside
            - existing_pending_risk
            - execution_reserve,
        )
        controlling_headroom = min(
            controlling_headroom,
            float(row["post_objective_new_risk_buffer"] or 0),
        )
    dynamic_new_risk_capacity = (
        min(loss_lock_capacity, floor_capacity)
        if floor_capacity is not None
        else loss_lock_capacity
    )
    if proposed_new_risk > dynamic_new_risk_capacity + 0.005:
        reasons.append(
            "proposed new risk exceeds broker-snapshot loss/floor headroom after "
            "open, pending, and execution reserves"
        )
    row["broker_snapshot_age_seconds"] = round(age_seconds, 3)
    row.update(dynamic_inputs)
    row.update(broker_balances)
    row["broker_new_notional_capacity_dollars"] = round(
        broker_new_notional_capacity, 4
    )
    row["post_order_gross_exposure_dollars"] = round(
        post_order_gross_exposure, 4
    )
    row["controlling_new_risk_headroom_dollars"] = round(
        controlling_headroom, 4
    )
    row["uncredited_open_profit_dollars"] = round(uncredited_open_profit, 4)
    row["open_loss_gauge_degradation_dollars"] = round(
        open_loss_gauge_degradation, 4
    )
    row["loss_lock_new_risk_capacity_dollars"] = round(loss_lock_capacity, 4)
    row["profit_floor_new_risk_capacity_dollars"] = (
        round(floor_capacity, 4) if floor_capacity is not None else None
    )
    row["dynamic_new_risk_capacity_dollars"] = round(
        dynamic_new_risk_capacity, 4
    )
    row["risk_gate_mode"] = "entry_authorization" if entry_check else "session_only"
    if entry_check and not reasons:
        checked_at = datetime.now(timezone.utc).isoformat()
        evidence = {
            "account_key": str(args.account_key),
            "session_date": str(args.session_date),
            "strategy_version": str(row["strategy_version"]),
            "broker_confirmed_at": str(row["broker_confirmed_at"]),
            "broker_snapshot_valid_until": (
                actual + timedelta(seconds=args.max_age_seconds)
            ).isoformat(),
            "checked_at": checked_at,
            "current_equity_dollars": round(float(row["current_equity"]), 4),
            "instrument_key": str(args.instrument_key),
            "thesis_key": str(args.thesis_key).upper(),
            "risk_action": str(args.risk_action).upper(),
            **{key: round(value, 4) for key, value in dynamic_inputs.items()},
            **{key: round(value, 4) for key, value in broker_balances.items()},
            "broker_new_notional_capacity_dollars": round(
                broker_new_notional_capacity, 4
            ),
            "post_order_gross_exposure_dollars": round(
                post_order_gross_exposure, 4
            ),
            "uncredited_open_profit_dollars": round(uncredited_open_profit, 4),
            "open_loss_gauge_degradation_dollars": round(
                open_loss_gauge_degradation, 4
            ),
            "loss_lock_new_risk_capacity_dollars": round(loss_lock_capacity, 4),
            "profit_floor_new_risk_capacity_dollars": (
                round(floor_capacity, 4) if floor_capacity is not None else None
            ),
            "dynamic_new_risk_capacity_dollars": round(
                dynamic_new_risk_capacity, 4
            ),
        }
        try:
            reservation = store.reserve_risk_authorization(evidence)
        except ValueError as error:
            reasons.append(str(error))
        else:
            if reservation["reserved"]:
                row["risk_gate_authorization"] = reservation["authorization"]
            else:
                reasons.append(str(reservation["reason"]))
                row["active_risk_authorization"] = reservation.get(
                    "active_authorization"
                )
    row["risk_gate_reasons"] = reasons
    row["new_entries_allowed"] = not reasons
    store.close()
    output(row)
    return 0 if row["new_entries_allowed"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Titan Massive market-intelligence runtime (shadow only)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to runtime JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Create the local intelligence database")
    init.set_defaults(func=cmd_init)

    doctor = commands.add_parser("doctor", help="Check key and local runtime readiness without exposing secrets")
    doctor.set_defaults(func=cmd_doctor)

    snapshot = commands.add_parser("snapshot", help="Fetch one Massive full-market snapshot")
    snapshot.set_defaults(func=cmd_snapshot)

    run = commands.add_parser("run", help="Run the WebSocket watcher")
    run.add_argument("--seconds", type=int, help="Stop after N seconds (connectivity testing)")
    run.add_argument("--ignore-schedule", action="store_true", help="Connect outside the configured session window")
    run.set_defaults(func=cmd_run)

    status = commands.add_parser("status", help="Show health and current market-data leaders")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(func=cmd_status)

    coverage = commands.add_parser("coverage", help="Audit eligible-universe and promoted-candidate coverage")
    coverage.set_defaults(func=cmd_coverage)

    plans = commands.add_parser("plans", help="Read latest weighted preliminary trade plans")
    plans.add_argument("--limit", type=int, default=100)
    plans.set_defaults(func=cmd_plans)

    events = commands.add_parser("events", help="Read or acknowledge the durable event queue")
    events.add_argument("--limit", type=int, default=50)
    events.add_argument("--jsonl", action="store_true")
    events.add_argument("--ack", help="Acknowledge one event ID")
    events.add_argument("--decision", help="Persist the contemporaneous decision before acknowledgement")
    events.add_argument("--reason", help="Reason for the decision")
    events.add_argument("--details-json", help="Optional JSON object with decision-time evidence")
    events.set_defaults(func=cmd_events)

    decisions = commands.add_parser("decisions", help="Read durable event decisions")
    decision_commands = decisions.add_subparsers(dest="decision_command", required=True)
    decision_list = decision_commands.add_parser("list", help="List recent event decisions")
    decision_list.add_argument("--limit", type=int, default=100)
    decision_list.set_defaults(func=cmd_decisions_list)

    lessons = commands.add_parser("lessons", help="Ingest or read external daily market research")
    lesson_commands = lessons.add_subparsers(dest="lesson_command", required=True)
    lesson_ingest = lesson_commands.add_parser("ingest", help="Ingest one Markdown or JSON lesson")
    lesson_ingest.add_argument("file")
    lesson_ingest.add_argument("--date", help="Lesson date in YYYY-MM-DD; defaults to today ET")
    lesson_ingest.add_argument("--source", default="trader_brain")
    lesson_ingest.set_defaults(func=cmd_lessons_ingest)
    lesson_latest = lesson_commands.add_parser("latest", help="Read latest lessons")
    lesson_latest.add_argument("--limit", type=int, default=5)
    lesson_latest.set_defaults(func=cmd_lessons_latest)

    changes = commands.add_parser("changes", help="Maintain the strategy change-control register")
    change_commands = changes.add_subparsers(dest="change_command", required=True)
    change_propose = change_commands.add_parser("propose", help="Record a JSON change proposal")
    change_propose.add_argument("file")
    change_propose.set_defaults(func=cmd_changes_propose)
    change_list = change_commands.add_parser("list", help="List recent proposals")
    change_list.add_argument("--limit", type=int, default=20)
    change_list.set_defaults(func=cmd_changes_list)

    research = commands.add_parser("research", help="Record pre-outcome entry plans and later outcomes")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_plan = research_commands.add_parser("plan", help="Freeze a JSON counterfactual entry plan")
    research_plan.add_argument("file")
    research_plan.set_defaults(func=cmd_research_plan)
    research_outcome = research_commands.add_parser("outcome", help="Attach and lock a later outcome")
    research_outcome.add_argument("--plan-id", required=True)
    research_outcome.add_argument("--file", required=True)
    research_outcome.set_defaults(func=cmd_research_outcome)
    research_report = research_commands.add_parser("report", help="Compare entry timing lanes")
    research_report.add_argument("--limit", type=int, default=20)
    research_report.set_defaults(func=cmd_research_report)

    campaigns = commands.add_parser(
        "campaigns", help="Persist broker-reconciled core/runner management state"
    )
    campaign_commands = campaigns.add_subparsers(dest="campaign_command", required=True)
    campaign_record = campaign_commands.add_parser(
        "record", help="Record one broker-reconciled JSON campaign snapshot"
    )
    campaign_record.add_argument("file")
    campaign_record.set_defaults(func=cmd_campaign_record)
    campaign_list = campaign_commands.add_parser(
        "list", help="List active core/runner campaigns"
    )
    campaign_list.add_argument("--all", action="store_true", help="Include terminal campaigns")
    campaign_list.set_defaults(func=cmd_campaign_list)
    campaign_history = campaign_commands.add_parser(
        "history", help="List append-only broker transition evidence"
    )
    campaign_history.add_argument("--campaign-id")
    campaign_history.set_defaults(func=cmd_campaign_history)

    risk = commands.add_parser(
        "risk", help="Persist the irreversible broker-confirmed account-day risk ledger"
    )
    risk_commands = risk.add_subparsers(dest="risk_command", required=True)
    risk_record = risk_commands.add_parser(
        "record", help="Record one broker-confirmed JSON account-day snapshot"
    )
    risk_record.add_argument("file")
    risk_record.set_defaults(func=cmd_risk_record)
    risk_list = risk_commands.add_parser(
        "list", help="List durable account-day risk sessions"
    )
    risk_list.add_argument("--limit", type=int, default=20)
    risk_list.set_defaults(func=cmd_risk_list)
    risk_authorizations = risk_commands.add_parser(
        "authorizations", help="List durable pending-order risk authorizations"
    )
    risk_authorizations.add_argument("--limit", type=int, default=20)
    risk_authorizations.set_defaults(func=cmd_risk_authorizations)
    risk_release = risk_commands.add_parser(
        "release", help="Release an unsubmitted active risk authorization"
    )
    risk_release.add_argument("--authorization-id", required=True)
    risk_release.add_argument("--reason", required=True)
    risk_release.set_defaults(func=cmd_risk_release)
    risk_gate = risk_commands.add_parser(
        "gate", help="Fail closed when the session is missing or loss-locked"
    )
    risk_gate.add_argument("--account-key", required=True)
    risk_gate.add_argument("--session-date", required=True)
    risk_gate.add_argument("--expected-broker-confirmed-at", required=True)
    risk_gate.add_argument("--max-age-seconds", type=int, default=90)
    risk_gate.add_argument(
        "--entry-check", action="store_true",
        help="Require and audit exact risk inputs immediately before an entry or add",
    )
    risk_gate.add_argument("--instrument-key")
    risk_gate.add_argument("--thesis-key")
    risk_gate.add_argument("--risk-action", choices=("ENTRY", "ADD"))
    risk_gate.add_argument("--reviewed-entry-price", type=float)
    risk_gate.add_argument("--structural-stop-price", type=float)
    risk_gate.add_argument("--quantity", type=float)
    risk_gate.add_argument("--contract-multiplier", type=float)
    risk_gate.add_argument("--modeled-execution-loss-dollars", type=float)
    risk_gate.add_argument("--stress-tail-loss-dollars", type=float)
    risk_gate.add_argument(
        "--existing-open-downside-dollars", type=float
    )
    risk_gate.add_argument("--existing-pending-risk-dollars", type=float)
    risk_gate.add_argument("--execution-reserve-dollars", type=float)
    risk_gate.set_defaults(func=cmd_risk_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
