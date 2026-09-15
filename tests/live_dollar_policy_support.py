"""Copy the real public approval inputs into isolated policy test releases."""

from pathlib import Path
import shutil


def copy_dollar_policy_inputs(source: Path, destination: Path) -> None:
    for relative in (
        "config/risk_limits_ibkr_dollar_headroom.json",
        "validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md",
        "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md",
    ):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
