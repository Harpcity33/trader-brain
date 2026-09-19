"""External-change invalidation for a sealed autonomous plan.

A sealed ``AutonomousIbkrPlanSeal`` today proves it is complete, exact, and
time-bounded (``created_at``/``expires_at``), and it self-validates its own
fields. It does NOT, by itself, notice that the account's exposure changed
out-of-band after the plan was built — a manual fill, an external cancellation,
a position that moved — which can make an otherwise-unexpired plan act on stale
assumptions (overselling, an exit against a position that is no longer there).

This module adds the missing guard as a PURE pre-dispatch predicate, in the
same style as ``session_trading_policy`` / ``handoff``: it recomputes a
deterministic fingerprint of the observed account exposure the plan depends on,
compares it to the fingerprint the plan was bound to, and refuses the plan when
they differ or when the plan is outside its own validity window. It reads no
credentials, opens no connection, and issues no order. The live wiring — pulling
current positions/open-orders from ibkr_read and passing the observed
fingerprint here before dispatch — belongs to the production integration
(Area 5); this module only decides.

The plan-bound fingerprint is not part of the hash-committed seal envelope (that
schema is deliberately left byte-for-byte unchanged); it is supplied alongside
the seal by the caller that built the plan, recorded in the same durable audit
step. This keeps the sealed-plan schema and its strict tests untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re


_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class PlanFreshnessError(ValueError):
    """Fixed-code contract error; never carries private text."""


def _fail(reason: str) -> None:
    raise PlanFreshnessError("PLAN_FRESHNESS_" + reason)


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")
    return value.astimezone(timezone.utc)


def _whole(value: object) -> int:
    if isinstance(value, bool) or type(value) is not int:
        _fail("QUANTITY_INVALID")
    return value


def _decimal_text(value: object) -> str:
    if type(value) is not Decimal or not value.is_finite():
        _fail("PRICE_INVALID")
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class ObservedPosition:
    contract_id: int
    quantity: int  # signed whole shares; sign matters for overselling

    def __post_init__(self) -> None:
        cid = _whole(self.contract_id)
        if cid <= 0:
            _fail("CONTRACT_ID_INVALID")
        _whole(self.quantity)


@dataclass(frozen=True)
class ObservedOpenOrder:
    order_identity: str
    contract_id: int
    side: str
    quantity: int
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.order_identity, str) or _TOKEN.fullmatch(self.order_identity) is None:
            _fail("ORDER_IDENTITY_INVALID")
        cid = _whole(self.contract_id)
        if cid <= 0:
            _fail("CONTRACT_ID_INVALID")
        if self.side not in ("BUY", "SELL"):
            _fail("SIDE_INVALID")
        if _whole(self.quantity) <= 0:
            _fail("QUANTITY_INVALID")
        if self.limit_price is not None:
            _decimal_text(self.limit_price)


def exposure_fingerprint(
    positions: tuple[ObservedPosition, ...],
    open_orders: tuple[ObservedOpenOrder, ...],
) -> str:
    """Deterministic SHA-256 of the exposure a plan depends on.

    Order-independent: positions and orders are sorted by their identities so
    two observations of the same account state fingerprint identically.
    Duplicate contract-id positions or duplicate order identities fail closed
    (an ambiguous observation must not silently pick one).
    """
    if type(positions) is not tuple or type(open_orders) is not tuple:
        _fail("EXPOSURE_INVALID")
    pos_ids = [p.contract_id for p in positions]
    if len(set(pos_ids)) != len(pos_ids):
        _fail("DUPLICATE_POSITION")
    ord_ids = [o.order_identity for o in open_orders]
    if len(set(ord_ids)) != len(ord_ids):
        _fail("DUPLICATE_ORDER")
    body = {
        "positions": sorted(
            ({"contract_id": p.contract_id, "quantity": p.quantity} for p in positions),
            key=lambda row: row["contract_id"],
        ),
        "open_orders": sorted(
            (
                {
                    "order_identity": o.order_identity,
                    "contract_id": o.contract_id,
                    "side": o.side,
                    "quantity": o.quantity,
                    "limit_price": None if o.limit_price is None else _decimal_text(o.limit_price),
                }
                for o in open_orders
            ),
            key=lambda row: row["order_identity"],
        ),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_plan_freshness(
    *,
    plan_bound_fingerprint: str,
    observed_fingerprint: str,
    created_at: datetime,
    expires_at: datetime,
    now: datetime,
) -> tuple[str, ...]:
    """Return the sorted blocker reasons; an EMPTY tuple means the plan is fresh.

    A non-empty result means the plan must NOT be dispatched. Reasons:
      PLAN_EXPIRED                    now is at/after expires_at
      PLAN_NOT_YET_VALID              now is before created_at
      PLAN_VALIDITY_WINDOW_INVALID    expires_at <= created_at
      EXTERNAL_EXPOSURE_CHANGED       observed exposure fingerprint != plan's
    """
    for value in (plan_bound_fingerprint, observed_fingerprint):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            _fail("FINGERPRINT_INVALID")
    created = _aware(created_at)
    expires = _aware(expires_at)
    current = _aware(now)
    blockers: set[str] = set()
    if expires <= created:
        blockers.add("PLAN_VALIDITY_WINDOW_INVALID")
    if current < created:
        blockers.add("PLAN_NOT_YET_VALID")
    if current >= expires:
        blockers.add("PLAN_EXPIRED")
    if observed_fingerprint != plan_bound_fingerprint:
        blockers.add("EXTERNAL_EXPOSURE_CHANGED")
    return tuple(sorted(blockers))
