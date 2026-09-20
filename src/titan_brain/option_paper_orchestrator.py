"""End-to-end PAPER orchestrator for an attended option trade (offline).

Milestone 9 (part a) of the attended-options plan.

This composes the milestone 1-8 building blocks into a single paper-mode flow:

  identity (M2) -> funding evidence (M3) -> scenarios (M4) -> analyzer (M1) ->
  risk policy (M5) -> attended ticket (M6) -> SIMULATED paper fill ->
  lifecycle / cumulative P&L (M7),  with the manual-intervention gate (M8)
  guarding new risk.

It is PAPER-ONLY and offline.  The execution step is a deterministic in-memory
simulator (``simulate_paper_fill``); there is NO broker connection, NO real
order, and NO live authority anywhere in this module.  Every gate that fails
closed upstream still fails closed here -- the orchestrator stops at the first
blocking stage and reports why.  A completed paper run is a demonstration of the
lifecycle, never evidence of live readiness, liquidity, or permission.

It also carries the small ADAPTER that bridges the analyzer's contract schema
(right ``CALL``/``PUT``, expiry ``YYYY-MM-DD``) to the canonical
``IbkrOptionContractIdentity`` (right ``C``/``P``, expiry ``YYYYMMDD``) noted as
the milestone-4 integration gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Sequence

from .option_trade_analysis import analyze_option_trade
from .option_risk_policy import (
    OptionsRiskPolicy,
    CorrelatedExposure,
    assess_options_trade_risk,
)
from .option_attended_ticket import (
    AttendedOptionTicket,
    approve_ticket,
    approval_still_binds,
)
from .option_lifecycle import LifecycleEvent, replay_lifecycle
from .option_reconciliation_gate import (
    ReconciliationInput,
    reconcile_account_activity,
)
from .live.broker.ibkr_option_orders import IbkrOptionContractIdentity


class PaperOrchestratorError(ValueError):
    """A well-formed, non-secret orchestrator error."""


# --- schema adapter (M4 gap): analyzer contract dict -> canonical identity ----

_RIGHT_TO_CANON = {"CALL": "C", "PUT": "P"}


def analyzer_contract_to_identity(
    contract: Mapping[str, object], *, trading_class: str, deliverable: str,
    exercise_style: str, settlement_type: str,
) -> IbkrOptionContractIdentity:
    """Bridge the analyzer's looser contract dict to the canonical identity.

    The analyzer uses right CALL/PUT and expiry YYYY-MM-DD; the canonical
    identity uses C/P and YYYYMMDD and carries the full contract spec.  The
    extra fields (trading_class, deliverable, exercise/settlement) are supplied
    explicitly -- they are NOT inferred from the analyzer payload.
    """

    right = _RIGHT_TO_CANON.get(str(contract.get("right", "")).upper())
    if right is None:
        raise PaperOrchestratorError("analyzer contract right must be CALL or PUT")
    expiry_raw = str(contract.get("expiry", ""))
    expiry = expiry_raw.replace("-", "")
    multiplier_raw = contract.get("multiplier")
    try:
        multiplier = int(multiplier_raw)
    except (TypeError, ValueError) as exc:
        raise PaperOrchestratorError("analyzer contract multiplier must be an integer") from exc
    return IbkrOptionContractIdentity(
        con_id=int(contract["con_id"]),
        symbol=str(contract["symbol"]),
        right=right,
        strike=str(contract["strike"]),
        expiry=expiry,
        trading_class=trading_class,
        multiplier=multiplier,
        deliverable=deliverable,
        exercise_style=exercise_style,
        settlement_type=settlement_type,
        currency=str(contract.get("currency", "USD")),
        exchange=str(contract.get("exchange", "SMART")),
    )


# --- paper fill simulator -----------------------------------------------------


@dataclass(frozen=True)
class PaperFillResult:
    filled_quantity: int
    fill_price: str
    status: str  # FILLED / PARTIAL / REJECTED / UNKNOWN


def simulate_paper_fill(
    *, requested_quantity: int, limit_price: str, available_liquidity: int,
    marketable: bool, outcome: str = "auto",
) -> PaperFillResult:
    """Deterministic in-memory paper fill. Never contacts a broker.

    ``outcome`` forces a fault path for testing: 'reject', 'unknown', 'partial',
    or 'auto' (fill up to available_liquidity when marketable).
    """

    if type(requested_quantity) is not int or requested_quantity <= 0:
        raise PaperOrchestratorError("requested_quantity must be a positive integer")
    if outcome == "reject" or not marketable:
        return PaperFillResult(filled_quantity=0, fill_price="0", status="REJECTED")
    if outcome == "unknown":
        # Ambiguous submission: not known filled, not known rejected.
        return PaperFillResult(filled_quantity=0, fill_price="0", status="UNKNOWN")
    fillable = min(requested_quantity, max(0, available_liquidity))
    if fillable == 0:
        return PaperFillResult(filled_quantity=0, fill_price="0", status="REJECTED")
    if fillable < requested_quantity or outcome == "partial":
        fillable = min(fillable, requested_quantity)
        status = "PARTIAL" if fillable < requested_quantity else "FILLED"
        return PaperFillResult(filled_quantity=fillable, fill_price=limit_price, status=status)
    return PaperFillResult(filled_quantity=fillable, fill_price=limit_price, status="FILLED")


# --- orchestrated result ------------------------------------------------------


@dataclass(frozen=True)
class PaperRunResult:
    ok: bool
    stopped_at: str
    blockers: tuple[str, ...]
    analysis: Mapping[str, object] = field(default_factory=dict)
    policy_figures: Mapping[str, object] = field(default_factory=dict)
    approval_bound: object = None
    fill_status: object = None
    lifecycle: Mapping[str, object] = field(default_factory=dict)
    paper_only: bool = True
    live_authority: bool = False


def run_paper_trade(
    *,
    analyzer_payload: Mapping[str, object],
    now: datetime,
    policy: OptionsRiskPolicy,
    correlated: Sequence[CorrelatedExposure] = (),
    account_equity: str,
    ticket: AttendedOptionTicket,
    reconciliation: ReconciliationInput,
    paper_fill: PaperFillResult,
    pretrade_baseline_cash: str,
) -> PaperRunResult:
    """Walk the full attended paper lifecycle, stopping at the first blocker.

    Stages, each fail-closed: (1) manual-intervention reconciliation, (2) M1
    analysis, (3) M5 policy budgets, (4) M6 attended-ticket approval binding,
    (5) SIMULATED paper fill, (6) M7 lifecycle/P&L.  Returns where it stopped
    and why.  No broker I/O; live_authority is always False.
    """

    # Stage 1: reconcile account-wide activity before any new risk (M8).
    recon = reconcile_account_activity(reconciliation)
    if not recon.reconciled:
        return PaperRunResult(ok=False, stopped_at="reconciliation", blockers=recon.blockers)

    # Stage 2: analyze (M1).
    analysis = analyze_option_trade(dict(analyzer_payload), now=now)
    if not analysis.get("analysis_complete"):
        return PaperRunResult(ok=False, stopped_at="analysis",
                              blockers=tuple(analysis.get("reasons", ())), analysis=analysis)

    figures = analysis.get("figures", {})
    # Stage 3: policy budgets on the analyzed losses (M5).
    assessment = assess_options_trade_risk(
        policy=policy,
        underlying_symbol=str(analyzer_payload["contract"]["symbol"]),
        planned_loss=figures.get("planned_loss", "0"),
        stress_loss=figures.get("stress_loss", "0"),
        premium_exposure=figures.get("premium_exposure", "0"),
        account_equity=account_equity,
        correlated=correlated,
    )
    if not assessment.ok:
        return PaperRunResult(ok=False, stopped_at="policy", blockers=assessment.blockers,
                              analysis=analysis, policy_figures=assessment.figures)

    # Stage 4: attended-ticket approval binding (M6).
    approval = approve_ticket(ticket, now=now)
    check = approval_still_binds(approval=approval, current_ticket=ticket, now=now)
    if not check.binds:
        return PaperRunResult(ok=False, stopped_at="ticket", blockers=check.reasons,
                              analysis=analysis, policy_figures=assessment.figures,
                              approval_bound=False)

    # Stage 5: SIMULATED paper fill (never a broker).
    if paper_fill.status in ("REJECTED", "UNKNOWN"):
        # An unknown/rejected paper submission does not open a position; it is
        # reported and stops the run (no silent retry).
        return PaperRunResult(ok=False, stopped_at="paper_fill", blockers=(paper_fill.status,),
                              analysis=analysis, policy_figures=assessment.figures,
                              approval_bound=True, fill_status=paper_fill.status)

    # Stage 6: lifecycle / cumulative P&L from the paper fill (M7).
    events = [
        LifecycleEvent(event_type="open_fill", at=now,
                       multiplier=int(analyzer_payload["contract"]["multiplier"]),
                       quantity=paper_fill.filled_quantity, price=paper_fill.fill_price),
    ]
    lifecycle = replay_lifecycle(events, pretrade_baseline_cash=pretrade_baseline_cash)
    return PaperRunResult(
        ok=True, stopped_at="complete", blockers=(),
        analysis=analysis, policy_figures=assessment.figures, approval_bound=True,
        fill_status=paper_fill.status,
        lifecycle={
            "open_contracts": lifecycle.open_contracts,
            "lifetime_pnl": lifecycle.lifetime_pnl,
            "realized_pnl": lifecycle.realized_pnl,
        },
    )


__all__ = [
    "PaperFillResult",
    "PaperOrchestratorError",
    "PaperRunResult",
    "analyzer_contract_to_identity",
    "run_paper_trade",
    "simulate_paper_fill",
]
