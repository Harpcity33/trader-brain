#!/usr/bin/env python3
"""Fail-fast structural validation for the additive Titan upgrade."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PROTECTED_ACCOUNT_LAST4 = ("7153", "3103")


def fail(message: str) -> None:
    raise AssertionError(message)


def prohibited_content_patterns() -> dict[str, re.Pattern[str]]:
    protected_suffixes = "|".join(
        re.escape(value) for value in PROTECTED_ACCOUNT_LAST4
    )
    return {
        # Match any unmasked numeric account identifier ending in a protected
        # last four. Do not commit an exact full identifier merely to test this
        # scanner; tests construct synthetic digit sequences at runtime.
        "unmasked_target_broker_account_number": re.compile(
            rf"(?<!\d)\d{{5,20}}(?:{protected_suffixes})(?!\d)"
        ),
        # Detect a committed six-digit confirmation secret by its assignment
        # context without embedding any real or stale secret in the validator.
        "confirmation_code_assignment": re.compile(
            r"(?i)\b(?:confirmation|verification|otp)[_-]?(?:code|token)?\b"
            r"\s*[:=]\s*[\"']\d{6}[\"']"
        ),
        "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "github_token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]+"),
    }


def main() -> int:
    json_files = sorted(ROOT.rglob("*.json"))
    toml_files = sorted(ROOT.rglob("*.toml"))
    python_files = sorted(ROOT.rglob("*.py"))
    for path in json_files:
        json.loads(path.read_text(encoding="utf-8"))
    for path in toml_files:
        tomllib.loads(path.read_text(encoding="utf-8"))
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    risk = json.loads((ROOT / "config/risk_limits.json").read_text())
    expected_risk = {
        "normal_planned_risk_pct": 0.03,
        "a_plus_planned_risk_pct": 0.04,
        "premarket_planned_risk_pct": 0.02,
        "premarket_stress_risk_pct": 0.04,
        "max_total_open_planned_risk_pct": 0.05,
        "daily_new_entry_lock_pct": 0.06,
        "hard_daily_loss_kill_pct": 0.08,
        "weekly_loss_lock_pct": 0.12,
        "live_drawdown_review_pct": 0.20,
    }
    for key, value in expected_risk.items():
        if risk.get(key) != value:
            fail(f"risk mismatch: {key}")

    taxonomy = json.loads((ROOT / "config/setup_taxonomy.json").read_text())
    if len(taxonomy["setup_ids"]) != len(set(taxonomy["setup_ids"])):
        fail("duplicate setup_id")
    if "MOMENTUM" in taxonomy["setup_ids"]:
        fail("generic MOMENTUM setup is forbidden")

    research = tomllib.loads(
        (ROOT / "automations/titan-daily-market-study/automation.toml").read_text()
    )
    options = tomllib.loads(
        (ROOT / "automations/robinhood-options-momentum-engine/automation.toml").read_text()
    )
    if research["id"] == options["id"] or research["kind"] != "cron":
        fail("research/options automation isolation failed")
    if options["kind"] != "heartbeat":
        fail("options automation must be an independent heartbeat")
    if "never pause, edit, replace" not in research["prompt"]:
        fail("research live-core isolation language missing")
    if "never pause, alter, replace" not in options["prompt"]:
        fail("options live-core isolation language missing")

    prohibited = prohibited_content_patterns()
    checked_bytes = 0
    for path in sorted(item for item in ROOT.rglob("*") if item.is_file()):
        if "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        checked_bytes += len(content.encode("utf-8"))
        for label, pattern in prohibited.items():
            if pattern.search(content):
                fail(f"prohibited {label} in {path.relative_to(ROOT)}")

    manifest = hashlib.sha256()
    for path in sorted(item for item in ROOT.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        manifest.update(str(path.relative_to(ROOT)).encode("utf-8") + b"\0")
        manifest.update(path.read_bytes())
    print(
        json.dumps(
            {
                "status": "PASS",
                "json_files": len(json_files),
                "toml_files": len(toml_files),
                "python_files": len(python_files),
                "checked_bytes": checked_bytes,
                "manifest_sha256": manifest.hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": "REPOSITORY_VALIDATION_FAILED",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
