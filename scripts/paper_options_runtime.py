#!/usr/bin/env python3
"""Trader Brain Paper Options v1 runtime orchestrator.

Paper-only. Owns scheduling/state transitions but performs no live broker writes.
Fails closed whenever required market-data or decision-review dependencies fail.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time
from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
import time as sleep_time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "paper_options_v1_runtime.json"
LOCAL_ENV_PATH = Path.home() / ".config" / "trader-brain" / "paper-options.env"
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RuntimeState:
    paper_only: bool
    cadence_seconds: int
    weekly_drawdown_pct: float
    normal_trade_risk_pct: tuple[float, float]
    exceptional_trade_risk_pct_max: float


def load_local_env(path: Path = LOCAL_ENV_PATH) -> None:
    """Load simple KEY=VALUE secrets locally without echoing their values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("mode") != "paper_only":
        raise RuntimeError("paper runtime refuses non-paper mode")
    return cfg


def state_from_config(cfg: dict[str, Any]) -> RuntimeState:
    return RuntimeState(
        paper_only=True,
        cadence_seconds=int(cfg["heartbeat"]["cadence_seconds"]),
        weekly_drawdown_pct=float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]),
        normal_trade_risk_pct=tuple(float(v) for v in cfg["risk"]["normal_trade_risk_pct"]),
        exceptional_trade_risk_pct_max=float(cfg["risk"]["exceptional_trade_risk_pct_max"]),
    )


def in_regular_session(now: datetime) -> bool:
    local = now.astimezone(NY)
    return local.weekday() < 5 and time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)


def _json_request(req: urlrequest.Request, *, timeout: int = 20) -> dict[str, Any]:
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def probe_massive() -> dict[str, Any]:
    key = require_env("MASSIVE_API_KEY")
    base = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com").rstrip("/")
    url = f"{base}/v2/aggs/ticker/SPY/prev?adjusted=true&apiKey={urlparse.quote(key)}"
    req = urlrequest.Request(url, headers={"User-Agent": "TraderBrain/1.0"})
    data = _json_request(req)
    status = str(data.get("status", "")).upper()
    if status not in {"OK", "DELAYED"} or not data.get("results"):
        raise RuntimeError(f"Massive probe returned status={status or 'UNKNOWN'} without SPY data")
    bar = data["results"][0]
    return {
        "ok": True,
        "ticker": data.get("ticker", "SPY"),
        "status": status,
        "previous_close_present": bar.get("c") is not None,
        "volume_present": bar.get("v") is not None,
    }


def probe_openai() -> dict[str, Any]:
    key = require_env("OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "high")
    body = json.dumps({
        "model": model,
        "reasoning": {"effort": effort},
        "input": "Return exactly the word READY.",
        "max_output_tokens": 16,
    }).encode("utf-8")
    req = urlrequest.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "TraderBrain/1.0",
        },
    )
    data = _json_request(req, timeout=30)
    if not data.get("id"):
        raise RuntimeError("OpenAI probe returned no response id")
    return {"ok": True, "model": data.get("model", model), "response_id_present": True}


def probe_gmail(*, send_test: bool = False) -> dict[str, Any]:
    sender = require_env("TB_GMAIL_SENDER")
    password = require_env("TB_GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = require_env("TB_GMAIL_RECIPIENT")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as smtp:
        smtp.login(sender, password)
        if send_test:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = recipient
            msg["Subject"] = "Trader Brain — Local Runtime Test"
            msg.set_content(
                "Trader Brain Paper Options v1 local Gmail transport is working.\n"
                "Mode: PAPER ONLY\n"
                "No broker-write authority is enabled."
            )
            smtp.send_message(msg)
    return {"ok": True, "authenticated": True, "test_email_sent": send_test}


def doctor(*, send_test_email: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True
    for name, fn in (
        ("massive", probe_massive),
        ("openai", probe_openai),
    ):
        try:
            checks[name] = fn()
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)}
            ok = False
    try:
        checks["gmail"] = probe_gmail(send_test=send_test_email)
    except Exception as exc:
        checks["gmail"] = {"ok": False, "error": str(exc)}
        ok = False
    return {
        "status": "READY_FOR_RUNTIME" if ok else "NOT_READY",
        "paper_only": True,
        "broker_write_authority": False,
        "checks": checks,
    }


def heartbeat_once(cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One fail-closed orchestration tick.

    Full Massive scanning and High-reasoning decision adapters are activated only
    after dependency doctor passes. Until signal logic is connected, this remains
    NO_TRADE rather than manufacturing a candidate.
    """
    local = now.astimezone(NY)
    return {
        "timestamp": local.isoformat(),
        "mode": "paper_only",
        "session_open": in_regular_session(local),
        "cadence_seconds": cfg["heartbeat"]["cadence_seconds"],
        "lanes": cfg["lanes"],
        "decision": "NO_TRADE",
        "reason": "signal adapters not yet activated; dependency doctor must pass first",
        "fail_closed": True,
    }


def run_loop() -> int:
    cfg = load_config()
    state = state_from_config(cfg)
    dependency_state = doctor(send_test_email=False)
    if dependency_state["status"] != "READY_FOR_RUNTIME":
        print(json.dumps(dependency_state, sort_keys=True), file=sys.stderr, flush=True)
        raise RuntimeError("dependency doctor failed; refusing to start heartbeat loop")
    while True:
        now = datetime.now(tz=NY)
        result = heartbeat_once(cfg, now)
        print(json.dumps(result, sort_keys=True), flush=True)
        sleep_time.sleep(state.cadence_seconds)


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "doctor", "doctor-email", "once", "loop"))
    args = parser.parse_args()
    cfg = load_config()
    if args.command == "check":
        print(json.dumps({
            "status": "READY_FOR_ADAPTERS",
            "config": str(CONFIG_PATH),
            "local_env_found": LOCAL_ENV_PATH.is_file(),
            "state": state_from_config(cfg).__dict__,
        }, default=list, sort_keys=True))
        return 0
    if args.command == "doctor":
        result = doctor(send_test_email=False)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "READY_FOR_RUNTIME" else 2
    if args.command == "doctor-email":
        result = doctor(send_test_email=True)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "READY_FOR_RUNTIME" else 2
    if args.command == "once":
        print(json.dumps(heartbeat_once(cfg, datetime.now(tz=NY)), sort_keys=True))
        return 0
    return run_loop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}), file=sys.stderr)
        raise
