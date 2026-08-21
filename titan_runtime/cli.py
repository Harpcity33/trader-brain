from __future__ import annotations

import argparse
from datetime import datetime
import json
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
