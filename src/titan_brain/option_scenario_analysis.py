"""Thesis and joint scenario generation for attended option analysis (offline).

Milestone 4 of the attended-options plan.

This is a pure, offline scenario GENERATOR/VALIDATOR.  It takes an explicit
thesis (underlying invalidation level and a latest intended exit, both defined
BEFORE sizing) and a set of jointly-specified market scenarios -- each moving
underlying price, remaining time, implied volatility, and the liquidation
spread together -- and emits the exact scenario list the milestone-1 analyzer
(`analyze_option_trade`) consumes: ``{name, kind, exit_bid, assumptions}`` with
``kind`` in {planned, stress, upside} and at least one planned and one stress.

What this module deliberately does NOT do (per the plan):

* It invents NO probabilities, confidence scores, or expected-return precision.
  Every scenario is a supplied assumption, echoed transparently.
* Its exit-bid estimate is a simple, clearly-labelled CONSERVATIVE executable
  bid, not a pricing/edge model and not a guaranteed quote.  The caller supplies
  a per-scenario estimated option mid (or bid) and a liquidation spread; the
  generator takes the conservative side (subtracts half the spread from the mid,
  or accepts a directly-supplied executable bid) and never rounds in the
  trader's favour.
* It does not choose the thesis, choose budgets, size the trade, or authorize
  anything.

Money is Decimal end-to-end; unknown/malformed inputs fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


class ScenarioError(ValueError):
    """A well-formed, non-secret scenario/thesis error."""


_KINDS = frozenset({"planned", "stress", "upside"})


def _decimal(value: object, field_name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or type(value) not in (str, int):
        raise ScenarioError(f"{field_name} must be a string or integer, not float/other")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ScenarioError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ScenarioError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ScenarioError(f"{field_name} must be >= {minimum}")
    return parsed


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ScenarioError(f"{field_name} must be a non-empty string")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ScenarioError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class OptionThesis:
    """The trade thesis, defined before sizing.

    ``invalidation_underlying`` is the underlying price at/through which the
    thesis is wrong; ``latest_exit`` is the latest intended exit datetime (which
    the analyzer separately requires to precede the contract expiry).  Both are
    recorded and echoed so the attended ticket can bind them.
    """

    underlying_symbol: str
    direction: str                  # "long_call_thesis" / "long_put_thesis" (label only)
    invalidation_underlying: Decimal
    latest_exit: datetime
    thesis_note: str

    def __post_init__(self) -> None:
        _text(self.underlying_symbol, "underlying_symbol")
        _text(self.direction, "direction")
        _text(self.thesis_note, "thesis_note")
        object.__setattr__(self, "invalidation_underlying",
                           _decimal(self.invalidation_underlying, "invalidation_underlying", minimum=Decimal(0)))
        _aware(self.latest_exit, "latest_exit")


@dataclass(frozen=True)
class JointScenarioInput:
    """One jointly-specified scenario.

    The caller supplies, as explicit assumptions, the estimated option mid under
    this scenario and the liquidation spread; the executable exit bid is derived
    conservatively as ``mid - spread/2`` (floored at 0).  Alternatively the
    caller may supply ``executable_bid`` directly (already conservative), in
    which case ``estimated_mid``/``liquidation_spread`` are ignored for the bid.
    The remaining fields (underlying, dte, IV) are recorded in the assumptions
    text for transparency; they are inputs to the caller's estimate, not a model
    this module runs.
    """

    name: str
    kind: str                       # planned / stress / upside
    scenario_underlying: Decimal
    days_remaining: int
    implied_vol_percent: Decimal
    estimated_mid: object = None     # money or None if executable_bid supplied
    liquidation_spread: object = None  # money or None if executable_bid supplied
    executable_bid: object = None   # money or None
    note: str = ""


@dataclass(frozen=True)
class GeneratedScenarios:
    thesis: Mapping[str, object]
    scenarios: tuple[Mapping[str, object], ...]


def generate_scenarios(
    thesis: OptionThesis, inputs: Sequence[JointScenarioInput]
) -> GeneratedScenarios:
    """Validate thesis + joint inputs; emit analyzer-ready scenarios.

    Raises ScenarioError unless the inputs include at least one ``planned`` and
    one ``stress`` scenario, names are unique, and every kind is valid.  Each
    emitted scenario carries a conservative ``exit_bid`` and a transparent
    ``assumptions`` string describing the joint move; no probability is attached.
    """

    if not isinstance(thesis, OptionThesis):
        raise ScenarioError("thesis must be an OptionThesis")
    if not isinstance(inputs, (list, tuple)) or not 1 <= len(inputs) <= 100:
        raise ScenarioError("between 1 and 100 scenario inputs are required")

    names: set[str] = set()
    kinds: set[str] = set()
    emitted: list[Mapping[str, object]] = []
    for item in inputs:
        if not isinstance(item, JointScenarioInput):
            raise ScenarioError("every scenario input must be a JointScenarioInput")
        name = _text(item.name, "name")
        if name in names:
            raise ScenarioError("scenario names must be unique")
        names.add(name)
        if item.kind not in _KINDS:
            raise ScenarioError("kind must be planned, stress, or upside")
        kinds.add(item.kind)

        underlying = _decimal(item.scenario_underlying, "scenario_underlying", minimum=Decimal(0))
        if type(item.days_remaining) is not int or isinstance(item.days_remaining, bool) or item.days_remaining < 0:
            raise ScenarioError("days_remaining must be a non-negative integer")
        iv = _decimal(item.implied_vol_percent, "implied_vol_percent", minimum=Decimal(0))

        # Derive the conservative executable exit bid.
        if item.executable_bid is not None:
            exit_bid = _decimal(item.executable_bid, "executable_bid", minimum=Decimal(0))
            bid_basis = "supplied_executable_bid"
        else:
            if item.estimated_mid is None or item.liquidation_spread is None:
                raise ScenarioError(
                    "provide executable_bid, or both estimated_mid and liquidation_spread"
                )
            mid = _decimal(item.estimated_mid, "estimated_mid", minimum=Decimal(0))
            spread = _decimal(item.liquidation_spread, "liquidation_spread", minimum=Decimal(0))
            # Conservative: sell at the bid = mid minus half the spread, floored.
            half = spread / Decimal(2)
            exit_bid = mid - half
            if exit_bid < 0:
                exit_bid = Decimal(0)
            bid_basis = "conservative_mid_minus_half_spread"

        note = item.note.strip()
        assumptions = (
            f"underlying={underlying}; days_remaining={item.days_remaining}; "
            f"iv_percent={iv}; exit_bid_basis={bid_basis}"
            + (f"; {note}" if note else "")
            + "; ASSUMPTION_NOT_PROBABILITY"
        )
        # Emit exactly the analyzer's scenario shape (Decimal-safe strings).
        emitted.append(
            {
                "name": name,
                "kind": item.kind,
                "exit_bid": _num(exit_bid),
                "assumptions": assumptions,
            }
        )

    if not {"planned", "stress"}.issubset(kinds):
        raise ScenarioError("at least one planned and one stress scenario are required")

    thesis_view = {
        "underlying_symbol": thesis.underlying_symbol,
        "direction": thesis.direction,
        "invalidation_underlying": _num(thesis.invalidation_underlying),
        "latest_exit": _aware(thesis.latest_exit, "latest_exit").isoformat(),
        "thesis_note": thesis.thesis_note,
        "disclaimer": "scenarios are supplied assumptions; no probability or edge is asserted",
    }
    return GeneratedScenarios(thesis=thesis_view, scenarios=tuple(emitted))


def _num(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


__all__ = [
    "GeneratedScenarios",
    "JointScenarioInput",
    "OptionThesis",
    "ScenarioError",
    "generate_scenarios",
]
