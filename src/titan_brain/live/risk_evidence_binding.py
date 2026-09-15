"""Pure canonical bindings shared by activation and broker risk evidence."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _peak_text(value: object) -> str:
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("peak_equity must be a finite positive decimal")
    try:
        peak = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("peak_equity must be a finite positive decimal") from None
    if not peak.is_finite() or peak <= 0:
        raise ValueError("peak_equity must be a finite positive decimal")
    text = format(peak, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def risk_high_water_receipt_hash(
    *,
    identity_hash: str,
    baseline_receipt_hash: str,
    lineage_hash: str,
    peak_equity: Decimal,
) -> str:
    """Return the stable receipt for one lineage-bound high-water value."""

    for name, value in (
        ("identity_hash", identity_hash),
        ("baseline_receipt_hash", baseline_receipt_hash),
        ("lineage_hash", lineage_hash),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(f"{name} must be lowercase SHA-256")
    payload = {
        "schema_version": "titan_ibkr_risk_high_water_receipt_2026-09-14_v2",
        "identity_hash": identity_hash,
        "baseline_receipt_hash": baseline_receipt_hash,
        "lineage_hash": lineage_hash,
        "peak_equity": _peak_text(peak_equity),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["risk_high_water_receipt_hash"]
