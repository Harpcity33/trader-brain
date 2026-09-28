"""Attended, fail-closed IBKR local preview and place-boundary preflight.

IBKR's direct TWS API does not expose a broker-native preview token.  This
module therefore creates an explicitly local :class:`AttendedLocalReview` and
requires its exact phrase.  It grants no unattended authority and performs no
order mutation.  Every revalidation re-reads contract identity, complete
broker state, session state, cash commitments, and the injected approved risk
policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time as wall_time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import re
from threading import RLock
from typing import Callable, Protocol, runtime_checkable
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from ..models import BrokerOrderState
from .base import (
    AccountSnapshot,
    AttendedLocalReview,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerSide,
    EquityOrderType,
    LocalCancelDecision,
    LocalPreflightDecision,
    MarketHours,
    OrderCheck,
    OrderRequest,
    TimeInForce,
)
from .ibkr_instrument import IbkrInstrumentEvidence, IbkrInstrumentProvider
from .ibkr_orders import (
    IbkrContractIdentity,
    attended_confirmation_phrase,
    attended_order_preview,
)
from ..ibkr_autonomous_authority import (
    IbkrAutonomousAuthorityBindings,
    VerifiedIbkrAutonomousAuthority,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_MASK = re.compile(r"^(?:\*{4}|•{4})[0-9]{4}$")
_DAILY_PNL_SOURCE = "ibkr:reqPnL.realizedPnL:current-day"
_AUTONOMOUS_RISK_SOURCE = re.compile(
    r"^ibkr:reqPnL\.realizedPnL:current-day\+authenticated-daily-baseline:"
    r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,159}:[0-9a-f]{64}:[0-9a-f]{64}$"
)
_MAX_ORDER_ID = 2**31 - 1
_CANCELLABLE_BROKER_STATES = frozenset(
    {
        BrokerOrderState.PENDING,
        BrokerOrderState.QUEUED,
        BrokerOrderState.CONFIRMED,
        BrokerOrderState.PARTIALLY_FILLED,
    }
)


class IbkrOrderPurpose(str, Enum):
    ENTRY = "entry"
    PROTECTION = "protection"
    EXIT = "exit"


@dataclass(frozen=True)
class IbkrAttendedOrderPlan:
    """Owner/pipeline-issued exact risk geometry; no values are defaulted."""

    plan_id: str
    purpose: IbkrOrderPurpose
    request: OrderRequest
    structural_stop: Decimal | None
    targets: tuple[Decimal, ...]
    execution_reserve: Decimal
    fee_reserve: Decimal
    required_stop_request: OrderRequest | None

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise ValueError("IBKR attended plan ID is required")
        object.__setattr__(self, "purpose", IbkrOrderPurpose(self.purpose))
        if not isinstance(self.request, OrderRequest):
            raise ValueError("IBKR attended plan requires an exact OrderRequest")
        object.__setattr__(self, "targets", tuple(Decimal(str(item)) for item in self.targets))
        if any(not item.is_finite() or item <= 0 for item in self.targets):
            raise ValueError("targets must be positive finite prices")
        object.__setattr__(self, "execution_reserve", Decimal(str(self.execution_reserve)))
        object.__setattr__(self, "fee_reserve", Decimal(str(self.fee_reserve)))
        if not self.execution_reserve.is_finite() or self.execution_reserve <= 0:
            raise ValueError("positive execution reserve is required")
        if not self.fee_reserve.is_finite() or self.fee_reserve <= 0:
            raise ValueError("positive fee reserve is required")
        if self.request.market_hours is not MarketHours.REGULAR:
            raise ValueError("IBKR attended orders are regular-hours only")
        stop = None if self.structural_stop is None else Decimal(str(self.structural_stop))
        if stop is not None and (not stop.is_finite() or stop <= 0):
            raise ValueError("structural stop must be positive")
        object.__setattr__(self, "structural_stop", stop)
        if self.purpose is IbkrOrderPurpose.ENTRY:
            self._validate_entry()
        elif self.purpose is IbkrOrderPurpose.PROTECTION:
            self._validate_protection()
        else:
            self._validate_exit()

    def _validate_entry(self) -> None:
        request = self.request
        if (
            request.side is not BrokerSide.BUY
            or request.order_type is not EquityOrderType.LIMIT
            or request.time_in_force is not TimeInForce.GFD
            or request.limit_price is None
            or request.limit_price <= Decimal("5")
            or self.structural_stop is None
            or self.structural_stop >= request.limit_price
            or not self.targets
            or any(target <= request.limit_price for target in self.targets)
        ):
            raise ValueError("IBKR entry risk geometry is invalid")
        stop = self.required_stop_request
        if (
            not isinstance(stop, OrderRequest)
            or stop.account_masked != request.account_masked
            or stop.symbol != request.symbol
            or stop.side is not BrokerSide.SELL
            or stop.order_type is not EquityOrderType.STOP_MARKET
            or stop.quantity != request.quantity
            or stop.market_hours is not MarketHours.REGULAR
            or stop.time_in_force is not TimeInForce.GTC
            or stop.stop_price != self.structural_stop
        ):
            raise ValueError("entry requires the exact regular-hours GTC stop-market plan")

    def _validate_protection(self) -> None:
        request = self.request
        if (
            request.side is not BrokerSide.SELL
            or request.order_type is not EquityOrderType.STOP_MARKET
            or request.time_in_force is not TimeInForce.GTC
            or request.stop_price is None
            or request.stop_price != self.structural_stop
            or self.required_stop_request is not None
            or self.targets
        ):
            raise ValueError("IBKR protection plan is invalid")

    def _validate_exit(self) -> None:
        request = self.request
        if (
            request.side is not BrokerSide.SELL
            or request.order_type not in {EquityOrderType.MARKET, EquityOrderType.LIMIT}
            or request.time_in_force is not TimeInForce.GFD
            or self.required_stop_request is not None
        ):
            raise ValueError("IBKR exit plan is invalid")

    @property
    def planned_downside(self) -> Decimal:
        if self.purpose is not IbkrOrderPurpose.ENTRY:
            return Decimal("0")
        assert self.request.limit_price is not None and self.structural_stop is not None
        return (self.request.limit_price - self.structural_stop) * self.request.quantity

    @property
    def stress_downside(self) -> Decimal:
        return self.planned_downside + self.execution_reserve + self.fee_reserve


@runtime_checkable
class WholeAccountSnapshotReader(Protocol):
    def __call__(self) -> AccountSnapshot: ...


@runtime_checkable
class AttendedPlanReader(Protocol):
    def __call__(self, request: OrderRequest) -> IbkrAttendedOrderPlan: ...


@runtime_checkable
class ApprovedRiskPolicyCheck(Protocol):
    def __call__(
        self,
        snapshot: AccountSnapshot,
        plan: IbkrAttendedOrderPlan,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class _Evaluation:
    snapshot: AccountSnapshot
    instrument: IbkrInstrumentEvidence
    plan: IbkrAttendedOrderPlan
    working_cash_commitment: Decimal
    candidate_cash_commitment: Decimal
    no_borrow_capacity: Decimal
    evidence_collection_id: str


@dataclass(frozen=True)
class _ProtectionBinding:
    """Source facts that must be re-proved at every stop review boundary."""

    source_request: OrderRequest
    source_plan_id: str
    stop_template: OrderRequest
    source_claimed_at: datetime


class IbkrAttendedPreflightBridge:
    """Concrete attended preflight compatible with ``IbkrPreflightBridge``."""

    def __init__(
        self,
        *,
        account_snapshot: WholeAccountSnapshotReader,
        instruments: IbkrInstrumentProvider,
        plan_reader: AttendedPlanReader,
        risk_policy_check: ApprovedRiskPolicyCheck,
        session_is_entry_eligible: Callable[[datetime, object], bool],
        account_masked: str,
        command_client_id: int,
        policy_binding_id: str,
        provider_contract_id: str,
        account_max_age_seconds: float,
        instrument_max_age_seconds: float,
        review_ttl_seconds: float,
        existing_order_reserve: Decimal,
        plan_reader_role: str = "ibkr_attended_plan_reader",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for value, name in (
            (account_snapshot, "account_snapshot"),
            (plan_reader, "plan_reader"),
            (risk_policy_check, "risk_policy_check"),
            (session_is_entry_eligible, "session_is_entry_eligible"),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be injected")
        if not isinstance(instruments, IbkrInstrumentProvider):
            raise TypeError("concrete IBKR instrument provider is required")
        if not isinstance(account_masked, str) or not _ACCOUNT_MASK.fullmatch(account_masked):
            raise ValueError("account_masked must expose exactly four trailing digits")
        if (
            type(command_client_id) is not int
            or not 0 < command_client_id <= _MAX_ORDER_ID
        ):
            raise ValueError("command_client_id must be a positive signed 32-bit integer")
        for value, name in (
            (policy_binding_id, "policy_binding_id"),
            (provider_contract_id, "provider_contract_id"),
        ):
            if not isinstance(value, str) or not _DIGEST.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 receipt")
        for value, name in (
            (account_max_age_seconds, "account_max_age_seconds"),
            (instrument_max_age_seconds, "instrument_max_age_seconds"),
            (review_ttl_seconds, "review_ttl_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be explicitly positive")
        reserve = Decimal(str(existing_order_reserve))
        if not reserve.is_finite() or reserve <= 0:
            raise ValueError("existing_order_reserve must be explicitly positive")
        if plan_reader_role not in {
            "ibkr_attended_plan_reader",
            "ibkr_autonomous_plan_reader",
        }:
            raise ValueError("plan_reader_role is not a supported release role")
        self._snapshot_reader = account_snapshot
        self._instruments = instruments
        self._plan_reader = plan_reader
        self._risk_policy_check = risk_policy_check
        self._session_check = session_is_entry_eligible
        self._account_masked = account_masked
        self._command_client_id = command_client_id
        self.policy_binding_id = policy_binding_id
        self.provider_contract_id = provider_contract_id
        self._account_age = float(account_max_age_seconds)
        self._instrument_age = float(instrument_max_age_seconds)
        self._review_ttl = float(review_ttl_seconds)
        self._existing_order_reserve = reserve
        self._plan_reader_role = plan_reader_role
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._issued: dict[
            str,
            tuple[
                AttendedLocalReview,
                IbkrContractIdentity,
                IbkrAttendedOrderPlan,
                _ProtectionBinding | None,
            ],
        ] = {}

    @property
    def account_masked(self) -> str:
        """Expose only the redacted account binding for outer authority joins."""

        return self._account_masked

    @property
    def command_client_id(self) -> int:
        """Return the exact command-client identity used by cancel proofs."""

        return self._command_client_id

    def current_time(self) -> datetime:
        """Return the delegate's release-inventoried UTC clock reading.

        The autonomous wrapper deliberately shares this clock instead of
        retaining a second timing authority.  That keeps receipt, authority,
        and cancellation freshness checks on one attested time source.
        """

        return self._now()

    def bind_entry_risk_activation(
        self,
        *,
        lineage_hash: str,
        minimum_peak: object,
    ) -> None:
        """Bind the autonomous risk checker used by every entry evaluation."""

        binder = getattr(
            self._risk_policy_check,
            "bind_entry_risk_activation",
            None,
        )
        if not callable(binder):
            raise BrokerMutationBlocked(
                "IBKR_ENTRY_ACTIVATION_RISK_BINDING_UNAVAILABLE"
            )
        result = binder(
            lineage_hash=lineage_hash,
            minimum_peak=minimum_peak,
        )
        if result is not None:
            raise BrokerMutationBlocked(
                "IBKR_ENTRY_ACTIVATION_RISK_BINDING_INVALID"
            )
        return None

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory every retained executable preflight dependency."""
        snapshot_members = (
            ("release_components", "coverage", "enrich", "__call__")
            if callable(getattr(self._snapshot_reader, "enrich", None))
            else ("__call__",)
        )
        risk_members = (
            ("__call__", "bind_entry_risk_activation")
            if callable(
                getattr(
                    self._risk_policy_check,
                    "bind_entry_risk_activation",
                    None,
                )
            )
            else ("__call__",)
        )
        risk_members += tuple(
            member for member in ("begin_account_snapshot", "observe_account_snapshot")
            if callable(getattr(self._risk_policy_check, member, None))
        )
        return (
            ("ibkr_account_snapshot_reader", self._snapshot_reader, snapshot_members),
            (
                "ibkr_instrument_provider",
                self._instruments,
                (
                    "release_components",
                    "open_generation",
                    "get_instrument",
                    "contract_for",
                ),
            ),
            (
                self._plan_reader_role,
                self._plan_reader,
                (
                    ("release_components", "__call__")
                    if self._plan_reader_role == "ibkr_autonomous_plan_reader"
                    else ("__call__",)
                ),
            ),
            ("ibkr_risk_policy_check", self._risk_policy_check, risk_members),
            (
                "ibkr_session_eligibility_check",
                self._session_check,
                ("__call__",),
            ),
            ("ibkr_preflight_clock", self._clock, ("__call__",)),
        )

    def review(self, request: OrderRequest) -> AttendedLocalReview:
        current = self._now()
        evaluation = self._evaluate(request, current)
        return self._issue_review(
            request,
            evaluation=evaluation,
            current=current,
            protection_binding=None,
        )

    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview:
        """Review only the broker-proved uncovered fill for one confirmed entry.

        The source decision UUID is intentionally irrelevant.  The caller
        supplies the release-bound plan ID and exact stop tuple from its
        tamper-evident review record; this method re-reads that plan and a new
        whole-account broker snapshot before issuing any stop review.
        """

        current = self._now()
        claimed_at = self._aware_utc(source_claimed_at)
        binding = _ProtectionBinding(
            source_request=source_request,
            source_plan_id=str(source_plan_id),
            stop_template=stop_template,
            source_claimed_at=claimed_at,
        )
        request, protection_plan = self._derive_protection_request(binding, current)
        evaluation = self._evaluate(request, current, bound_plan=protection_plan)
        if (
            evaluation.plan.purpose is not IbkrOrderPurpose.PROTECTION
            or evaluation.plan.plan_id != binding.source_plan_id
        ):
            raise BrokerMutationBlocked("IBKR_PROTECTION_PLAN_BINDING_CHANGED")
        return self._issue_review(
            request,
            evaluation=evaluation,
            current=current,
            protection_binding=binding,
        )

    def _issue_review(
        self,
        request: OrderRequest,
        *,
        evaluation: _Evaluation,
        current: datetime,
        protection_binding: _ProtectionBinding | None,
    ) -> AttendedLocalReview:
        phrase = self._confirmation_phrase(request)
        stop = evaluation.plan.required_stop_request
        checks = [
            OrderCheck("IBKR_SOURCE", "INFO", "Contract and eligibility were read from IBKR."),
            OrderCheck("REGULAR_SESSION", "INFO", "Current time is inside the approved regular-hours lane for this action."),
            OrderCheck("WHOLE_ACCOUNT", "INFO", "Positions and all configured order families were reconciled."),
            OrderCheck("DAILY_PNL", "INFO", "Broker-confirmed daily realized P&L evidence is authoritative and fresh."),
            OrderCheck("NO_BORROW", "INFO", "Cash capacity includes all unresolved buy commitments and positive reserves."),
        ]
        if evaluation.plan.purpose is IbkrOrderPurpose.ENTRY:
            checks.append(
                OrderCheck("PROTECTION", "INFO", "The exact regular-hours GTC stop plan is predeclared.")
            )
        else:
            checks.append(
                OrderCheck("REDUCE_ONLY", "INFO", "Current uncommitted shares cover this sell quantity.")
            )
        if protection_binding is not None:
            checks.append(
                OrderCheck(
                    "FILL_DELTA",
                    "INFO",
                    "A fresh whole-account reconciliation exact-matched the entry fill and uncovered quantity.",
                )
            )
        preview = {
            **dict(attended_order_preview(request)),
            "provider": "IBKR TWS API (local attended preview; not broker acceptance)",
            "account": request.account_masked,
            "session": "regular_hours",
            "order": self._request_preview(request),
            "contract": {
                "source": evaluation.instrument.source,
                "con_id": evaluation.instrument.identity.con_id,
                "symbol": evaluation.instrument.identity.symbol,
                "security_type": "STK",
                "currency": "USD",
                "routing_exchange": "SMART",
                "primary_exchange": evaluation.instrument.identity.primary_exchange,
                "exchange_listed": True,
                "regular_hours_eligible": True,
            },
            "risk": {
                "plan_id": evaluation.plan.plan_id,
                "purpose": evaluation.plan.purpose.value,
                "structural_stop": self._money(evaluation.plan.structural_stop),
                "targets": tuple(self._money(item) for item in evaluation.plan.targets),
                "planned_downside": self._money(evaluation.plan.planned_downside),
                "execution_reserve": self._money(evaluation.plan.execution_reserve),
                "fee_reserve": self._money(evaluation.plan.fee_reserve),
                "stress_downside": self._money(evaluation.plan.stress_downside),
            },
            "capacity": {
                "no_borrow_capacity": self._money(evaluation.no_borrow_capacity),
                "working_buy_commitments": self._money(evaluation.working_cash_commitment),
                "candidate_commitment": self._money(evaluation.candidate_cash_commitment),
                "remaining_after_candidate": self._money(
                    evaluation.no_borrow_capacity
                    - evaluation.working_cash_commitment
                    - evaluation.candidate_cash_commitment
                ),
            },
            "required_stop": self._request_preview(stop) if stop is not None else None,
            "protection_source": (
                {
                    "entry_client_ref_id": protection_binding.source_request.client_ref_id,
                    "entry_plan_id": protection_binding.source_plan_id,
                    "entry_claimed_at": protection_binding.source_claimed_at.isoformat(),
                    "broker_confirmed_uncovered_quantity": request.quantity,
                }
                if protection_binding is not None
                else None
            ),
            "alerts": (
                "This is a local attended preview, not a broker-native review or acceptance.",
                "IBKR submission remains unresolved until a newer broker reconciliation.",
                "An entry fill is not protected until its separately reviewed GTC stop is confirmed working.",
            ),
        }
        review = AttendedLocalReview(
            request=request,
            reviewed_at=current,
            received_at=current,
            expires_at=current + timedelta(seconds=self._review_ttl),
            disclosure=(
                "Attended IBKR direct-API preview only. Exact confirmation authorizes one "
                "attempt; it does not prove acceptance, fill, or protection."
            ),
            order_checks=tuple(checks),
            required_confirmation_phrase=phrase,
            broker_review_id=None,
            broker_bound=False,
            preview=preview,
            decision_id=str(uuid4()),
            policy_binding_id=self.policy_binding_id,
            evidence_collection_id=evaluation.evidence_collection_id,
            provider_contract_id=self.provider_contract_id,
        )
        with self._lock:
            self._issued = {
                key: item
                for key, item in self._issued.items()
                if not item[0].expired_at(current)
            }
            if len(self._issued) >= 256:
                raise BrokerMutationBlocked("IBKR_ATTENDED_REVIEW_CAPACITY_REACHED")
            if request.client_ref_id in self._issued:
                raise BrokerMutationBlocked("IBKR_ATTENDED_REVIEW_ALREADY_OUTSTANDING")
            self._issued[request.client_ref_id] = (
                review,
                evaluation.instrument.identity,
                evaluation.plan,
                protection_binding,
            )
        return review

    def contract_for(self, request: OrderRequest) -> IbkrContractIdentity:
        with self._lock:
            issued = self._issued.get(request.client_ref_id)
            if issued is None or issued[0].request.exact_tuple != request.exact_tuple:
                raise BrokerMutationBlocked("IBKR_EXACT_ATTENDED_REVIEW_REQUIRED")
            return issued[1]

    def revalidate(
        self, request: OrderRequest, review: AttendedLocalReview
    ) -> None:
        current = self._now()
        with self._lock:
            issued = self._issued.get(request.client_ref_id)
        if (
            issued is None
            or issued[0] != review
            or review.request.exact_tuple != request.exact_tuple
            or review.expired_at(current)
        ):
            raise BrokerMutationBlocked("IBKR_EXACT_UNEXPIRED_ATTENDED_REVIEW_REQUIRED")
        protection_binding = issued[3]
        if protection_binding is not None:
            derived, protection_plan = self._derive_protection_request(
                protection_binding, current
            )
            if derived.exact_tuple != request.exact_tuple:
                raise BrokerMutationBlocked(
                    "IBKR_PROTECTION_FILL_OR_COVERAGE_CHANGED_REVIEW_REQUIRED"
                )
            evaluation = self._evaluate(
                request, current, bound_plan=protection_plan
            )
        else:
            evaluation = self._evaluate(request, current)
        if evaluation.instrument.identity != issued[1] or evaluation.plan != issued[2]:
            raise BrokerMutationBlocked("IBKR_ATTENDED_FACTS_CHANGED_REVIEW_REQUIRED")
        if review.required_confirmation_phrase != self._confirmation_phrase(request):
            raise BrokerMutationBlocked("IBKR_ATTENDED_CONFIRMATION_PHRASE_CHANGED")
        return None

    def cancel_evidence(self, client_ref_id: str, order_id: int) -> str:
        """Prove one fresh, exact, command-client broker cancellation target.

        The transport independently proves that the same reference/order ID is
        a locally journaled ACK intent.  This bridge contributes the other half
        of the join: a new complete account collection in which exactly that
        order is still visibly cancellable.  Missing or contradictory evidence
        never becomes an absent-order or retry claim.  The returned digest is a
        receipt over the broker facts actually checked here; it is not a random
        decision identifier or a claim that cancellation has occurred.
        """

        try:
            normalized_ref = str(UUID(str(client_ref_id)))
        except (ValueError, TypeError, AttributeError):
            raise BrokerMutationBlocked("IBKR_CANCEL_CLIENT_REFERENCE_INVALID") from None
        if type(order_id) is not int or not 0 < order_id <= _MAX_ORDER_ID:
            raise BrokerMutationBlocked("IBKR_CANCEL_ORDER_ID_INVALID")
        now = self._now()
        self._cancel_session(now)
        observation_token = self._begin_account_snapshot(now, entry=False)
        try:
            snapshot = self._snapshot_reader()
        except Exception:
            raise BrokerMutationBlocked("IBKR_CANCEL_WHOLE_ACCOUNT_READ_FAILED") from None
        now = self._now()
        self._snapshot(snapshot, now, expected_account_masked=self._account_masked,
                       observation_token=observation_token)
        self._cancel_session(now)

        exact_broker_order_id = (
            f"ibkr:{self._command_client_id}:{order_id}"
        )
        reference_matches = tuple(
            order
            for order in snapshot.equity_orders
            if order.client_ref_id == normalized_ref
        )
        identity_matches = tuple(
            order
            for order in snapshot.equity_orders
            if order.broker_order_id == exact_broker_order_id
        )
        if (
            len(reference_matches) != 1
            or len(identity_matches) != 1
            or reference_matches[0] is not identity_matches[0]
        ):
            raise BrokerMutationBlocked("IBKR_CANCEL_EXACT_BROKER_ORDER_UNRESOLVED")
        order = reference_matches[0]
        receipt_age = (now - order.received_at).total_seconds()
        if receipt_age < -1 or receipt_age > self._account_age:
            raise BrokerMutationBlocked("IBKR_CANCEL_ORDER_EVIDENCE_STALE_OR_FUTURE")
        if (
            order.account_masked != self._account_masked
            or order.market_hours is not MarketHours.REGULAR
            or order.state not in _CANCELLABLE_BROKER_STATES
            or order.state.terminal
            or order.cumulative_filled_quantity >= order.requested_quantity
        ):
            raise BrokerMutationBlocked(
                "IBKR_CANCEL_ORDER_TERMINAL_AMBIGUOUS_OR_OUTSIDE_POLICY"
            )
        facts = (
            "titan-ibkr-cancel-evidence-v1",
            snapshot.account_masked,
            snapshot.account_state,
            snapshot.observed_at.isoformat(),
            snapshot.received_at.isoformat(),
            exact_broker_order_id,
            normalized_ref,
            order.symbol,
            order.side.value,
            order.order_type.value,
            order.state.value,
            format(order.requested_quantity, "f"),
            format(order.cumulative_filled_quantity, "f"),
            order.market_hours.value,
            order.time_in_force.value,
            "" if order.limit_price is None else format(order.limit_price, "f"),
            "" if order.stop_price is None else format(order.stop_price, "f"),
            order.broker_updated_at.isoformat(),
            order.received_at.isoformat(),
        )
        return hashlib.sha256("\x1f".join(facts).encode("utf-8")).hexdigest()

    def authorize_cancel(self, client_ref_id: str, order_id: int) -> None:
        """Retain the attended callback contract while discarding its receipt."""

        self.cancel_evidence(client_ref_id, order_id)
        return None

    def _derive_protection_request(
        self, binding: _ProtectionBinding, now: datetime
    ) -> tuple[OrderRequest, IbkrAttendedOrderPlan]:
        source = binding.source_request
        template = binding.stop_template
        if (
            not isinstance(source, OrderRequest)
            or source.account_masked != self._account_masked
            or source.side is not BrokerSide.BUY
            or source.order_type is not EquityOrderType.LIMIT
            or source.time_in_force is not TimeInForce.GFD
            or source.market_hours is not MarketHours.REGULAR
            or source.limit_price is None
            or not _DIGEST.fullmatch(binding.source_plan_id)
            or not isinstance(template, OrderRequest)
            or template.account_masked != source.account_masked
            or template.symbol != source.symbol
            or template.side is not BrokerSide.SELL
            or template.order_type is not EquityOrderType.STOP_MARKET
            or template.quantity != source.quantity
            or template.market_hours is not MarketHours.REGULAR
            or template.time_in_force is not TimeInForce.GTC
            or template.stop_price is None
            or template.client_ref_id == source.client_ref_id
            or binding.source_claimed_at > now + timedelta(seconds=1)
        ):
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_BINDING_INVALID")
        try:
            source_plan = self._plan_reader(source)
        except Exception:
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_PLAN_UNAVAILABLE") from None
        if (
            not isinstance(source_plan, IbkrAttendedOrderPlan)
            or source_plan.plan_id != binding.source_plan_id
            or source_plan.purpose is not IbkrOrderPurpose.ENTRY
            or source_plan.request.exact_tuple != source.exact_tuple
            or not isinstance(source_plan.required_stop_request, OrderRequest)
            or source_plan.required_stop_request.exact_tuple != template.exact_tuple
        ):
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_PLAN_CHANGED")
        observation_token = self._begin_account_snapshot(now, entry=False)
        try:
            snapshot = self._snapshot_reader()
        except Exception:
            raise BrokerMutationBlocked("IBKR_PROTECTION_WHOLE_ACCOUNT_READ_FAILED") from None
        now = self._now()
        self._snapshot(snapshot, now, expected_account_masked=self._account_masked,
                       observation_token=observation_token)
        matches = tuple(
            order
            for order in snapshot.equity_orders
            if order.client_ref_id == source.client_ref_id
        )
        if len(matches) != 1:
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_ORDER_UNRESOLVED")
        order = matches[0]
        order_age = (now - order.received_at).total_seconds()
        if order_age < -1 or order_age > self._account_age:
            raise BrokerMutationBlocked("IBKR_PROTECTION_ORDER_EVIDENCE_STALE_OR_FUTURE")
        broker_identity = order.broker_order_id.split(":")
        source_order_owned = bool(
            len(broker_identity) == 3
            and broker_identity[0] == "ibkr"
            and broker_identity[1] == str(self._command_client_id)
            and broker_identity[2].isdigit()
            and 0 < int(broker_identity[2]) <= _MAX_ORDER_ID
        )
        if not source_order_owned:
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_ORDER_NOT_OWNED")
        if (
            order.account_masked != source.account_masked
            or order.symbol != source.symbol
            or order.side is not source.side
            or order.order_type is not source.order_type
            or order.requested_quantity != Decimal(source.quantity)
            or order.market_hours is not source.market_hours
            or order.time_in_force is not source.time_in_force
            or order.limit_price != source.limit_price
            or order.stop_price is not None
            or order.cumulative_filled_quantity <= 0
            or not order.fills
            or any(
                fill.executed_at < binding.source_claimed_at - timedelta(seconds=2)
                or fill.executed_at > now + timedelta(seconds=1)
                for fill in order.fills
            )
        ):
            raise BrokerMutationBlocked("IBKR_PROTECTION_EXACT_FILL_DELTA_UNPROVEN")
        uncovered = order.cumulative_filled_quantity
        if uncovered != uncovered.to_integral_value() or uncovered > Decimal(source.quantity):
            raise BrokerMutationBlocked("IBKR_PROTECTION_WHOLE_SHARE_FILL_REQUIRED")
        positions = tuple(
            position
            for position in snapshot.equity_positions
            if position.symbol == source.symbol
        )
        active_sells = tuple(
            candidate
            for candidate in snapshot.equity_orders
            if candidate.symbol == source.symbol
            and candidate.side is BrokerSide.SELL
            and not candidate.state.terminal
        )
        if len(positions) != 1:
            raise BrokerMutationBlocked("IBKR_PROTECTION_UNCOVERED_QUANTITY_UNPROVEN")
        position = positions[0]
        covered = Decimal("0")
        for candidate in active_sells:
            if (
                candidate.account_masked != template.account_masked
                or candidate.order_type is not template.order_type
                or candidate.market_hours is not template.market_hours
                or candidate.time_in_force is not template.time_in_force
                or candidate.limit_price is not None
                or candidate.stop_price != template.stop_price
                or candidate.state is not BrokerOrderState.CONFIRMED
                or candidate.cumulative_filled_quantity != 0
                or candidate.client_ref_id is None
            ):
                raise BrokerMutationBlocked("IBKR_PROTECTION_CONFLICTING_SELL_COVERAGE")
            covered += candidate.requested_quantity
        if covered > uncovered:
            raise BrokerMutationBlocked("IBKR_PROTECTION_SELL_COVERAGE_EXCEEDS_FILL")
        uncovered -= covered
        if (
            uncovered <= 0
            or position.quantity != order.cumulative_filled_quantity
            or position.sellable_quantity != uncovered
            or position.held_for_sells != covered
        ):
            raise BrokerMutationBlocked("IBKR_PROTECTION_UNCOVERED_QUANTITY_UNPROVEN")
        client_ref_id = template.client_ref_id
        if covered:
            client_ref_id = str(
                uuid5(
                    UUID(template.client_ref_id),
                    ":".join(
                        (
                            "titan-ibkr-incremental-protection-v1",
                            binding.source_plan_id,
                            str(order.cumulative_filled_quantity),
                            str(covered),
                            str(uncovered),
                        )
                    ),
                )
            )
        request = replace(
            template,
            quantity=int(uncovered),
            client_ref_id=client_ref_id,
        )
        plan = IbkrAttendedOrderPlan(
            plan_id=source_plan.plan_id,
            purpose=IbkrOrderPurpose.PROTECTION,
            request=request,
            structural_stop=source_plan.structural_stop,
            targets=(),
            execution_reserve=source_plan.execution_reserve,
            fee_reserve=source_plan.fee_reserve,
            required_stop_request=None,
        )
        return request, plan

    def _evaluate(
        self,
        request: OrderRequest,
        now: datetime,
        *,
        bound_plan: IbkrAttendedOrderPlan | None = None,
    ) -> _Evaluation:
        if not isinstance(request, OrderRequest):
            raise BrokerMutationBlocked("IBKR_NORMALIZED_ORDER_REQUEST_REQUIRED")
        if bound_plan is None:
            try:
                plan = self._plan_reader(request)
            except Exception:
                raise BrokerMutationBlocked("IBKR_EXACT_RISK_PLAN_UNAVAILABLE") from None
        else:
            plan = bound_plan
        if not isinstance(plan, IbkrAttendedOrderPlan) or plan.request.exact_tuple != request.exact_tuple:
            raise BrokerMutationBlocked("IBKR_EXACT_RISK_PLAN_MISMATCH")
        self._session(plan, now)
        observation_token = self._begin_account_snapshot(
            now, entry=plan.purpose is IbkrOrderPurpose.ENTRY
        )
        try:
            snapshot = self._snapshot_reader()
        except Exception:
            raise BrokerMutationBlocked("IBKR_WHOLE_ACCOUNT_READ_FAILED") from None
        now = self._now()
        self._snapshot(
            snapshot,
            now,
            expected_account_masked=request.account_masked,
            require_daily_risk_evidence=(
                plan.purpose is IbkrOrderPurpose.ENTRY
            ),
            require_entry_risk_evidence=(
                self._plan_reader_role == "ibkr_autonomous_plan_reader"
                and plan.purpose is IbkrOrderPurpose.ENTRY
            ),
            observation_token=observation_token,
        )
        self._session(plan, now)
        try:
            instrument = self._instruments.get_instrument(request.symbol, now=now)
        except Exception:
            raise BrokerMutationBlocked("IBKR_INSTRUMENT_REVALIDATION_FAILED") from None
        now = self._now()
        self._snapshot(
            snapshot, now, expected_account_masked=request.account_masked,
            require_daily_risk_evidence=plan.purpose is IbkrOrderPurpose.ENTRY,
            require_entry_risk_evidence=(self._plan_reader_role == "ibkr_autonomous_plan_reader"
                                         and plan.purpose is IbkrOrderPurpose.ENTRY),
            observe_risk=False,
        )
        self._session(plan, now)
        self._instrument(instrument, request, now)
        try:
            result = self._risk_policy_check(snapshot, plan, now)
        except Exception:
            raise BrokerMutationBlocked("IBKR_APPROVED_RISK_POLICY_DENIED") from None
        if result is not None:
            raise BrokerMutationBlocked("IBKR_RISK_POLICY_CHECK_MUST_RAISE_ON_DENIAL")
        working = self._working_cash_commitment(snapshot)
        capacity = min(snapshot.funds.cash, snapshot.funds.unleveraged_buying_power)
        candidate = self._candidate_cash_commitment(plan)
        if request.side is BrokerSide.BUY:
            if any(
                position.symbol == request.symbol and position.quantity > 0
                for position in snapshot.equity_positions
            ):
                raise BrokerMutationBlocked("IBKR_ADD_PROHIBITED")
            if working + candidate > capacity:
                raise BrokerMutationBlocked("IBKR_NO_BORROW_CAPACITY_INSUFFICIENT")
        else:
            position = next(
                (item for item in snapshot.equity_positions if item.symbol == request.symbol),
                None,
            )
            if position is None or Decimal(request.quantity) > position.sellable_quantity:
                raise BrokerMutationBlocked("IBKR_REDUCE_ONLY_SHARES_UNAVAILABLE")
        if any(order.client_ref_id == request.client_ref_id for order in snapshot.equity_orders):
            raise BrokerMutationBlocked("IBKR_CLIENT_REFERENCE_ALREADY_VISIBLE")
        evidence_id = hashlib.sha256(
            repr(
                (
                    snapshot.account_masked,
                    snapshot.observed_at.isoformat(),
                    snapshot.received_at.isoformat(),
                    instrument.evidence_id,
                    plan.plan_id,
                    request.exact_tuple,
                    str(working),
                    str(candidate),
                    str(capacity),
                )
            ).encode("ascii")
        ).hexdigest()
        return _Evaluation(snapshot, instrument, plan, working, candidate, capacity, evidence_id)

    def _session(self, plan: IbkrAttendedOrderPlan, now: datetime) -> None:
        request = plan.request
        local = now.astimezone(ZoneInfo("America/New_York"))
        local_clock = local.time().replace(tzinfo=None)
        inside = (
            wall_time(9, 35) <= local_clock < wall_time(15, 30)
            if plan.purpose is IbkrOrderPurpose.ENTRY
            else wall_time(9, 30) <= local_clock < wall_time(16, 0)
        )
        try:
            provider_session_eligible = self._session_check(now, plan.purpose)
        except Exception:
            raise BrokerMutationBlocked("IBKR_SESSION_PROVIDER_FAILED") from None
        if (
            request.market_hours is not MarketHours.REGULAR
            or not inside
            or provider_session_eligible is not True
        ):
            raise BrokerMutationBlocked("IBKR_REGULAR_ACTION_SESSION_CLOSED")

    def _cancel_session(self, now: datetime) -> None:
        local_clock = now.astimezone(ZoneInfo("America/New_York")).time().replace(
            tzinfo=None
        )
        try:
            provider_session_eligible = self._session_check(now, "cancel")
        except Exception:
            raise BrokerMutationBlocked("IBKR_SESSION_PROVIDER_FAILED") from None
        if not (
            wall_time(9, 30) <= local_clock < wall_time(16, 0)
            and provider_session_eligible is True
        ):
            raise BrokerMutationBlocked("IBKR_REGULAR_ACTION_SESSION_CLOSED")

    def _begin_account_snapshot(self, now: datetime, *, entry: bool):
        if self._plan_reader_role != "ibkr_autonomous_plan_reader":
            return None
        begin = getattr(self._risk_policy_check, "begin_account_snapshot", None)
        observer = getattr(self._risk_policy_check, "observe_account_snapshot", None)
        if entry and (not callable(begin) or not callable(observer)):
            raise BrokerMutationBlocked("IBKR_RISK_OBSERVATION_HOOKS_UNAVAILABLE")
        if callable(begin):
            try:
                return begin(now, entry=entry)
            except Exception:
                if entry:
                    raise BrokerMutationBlocked("IBKR_RISK_OBSERVATION_NOT_ARMED") from None
                # Storage/receipt failure cannot strand protection or exits.
                # The writer-owned observer retains its recovery blocker.
        return None

    def _snapshot(
        self,
        snapshot: object,
        now: datetime,
        *,
        expected_account_masked: str,
        require_daily_risk_evidence: bool = False,
        require_entry_risk_evidence: bool = False,
        observation_token=None,
        observe_risk: bool = True,
    ) -> None:
        if not isinstance(snapshot, AccountSnapshot):
            raise BrokerMutationBlocked("IBKR_ACCOUNT_SNAPSHOT_NOT_NORMALIZED")
        if snapshot.account_masked != expected_account_masked:
            raise BrokerMutationBlocked("IBKR_ACCOUNT_SNAPSHOT_BINDING_MISMATCH")
        ages = (
            (now - snapshot.received_at).total_seconds(),
            (now - snapshot.observed_at).total_seconds(),
        )
        if any(age < -1 or age > self._account_age for age in ages):
            raise BrokerMutationBlocked("IBKR_ACCOUNT_SNAPSHOT_STALE_OR_FUTURE")
        if not snapshot.whole_broker_reconciled:
            raise BrokerMutationBlocked("IBKR_WHOLE_ACCOUNT_RECONCILIATION_INCOMPLETE")
        if snapshot.auth_point_in_time is not True:
            raise BrokerMutationBlocked("IBKR_ACCOUNT_AUTH_POINT_IN_TIME_MISSING")
        if require_daily_risk_evidence or require_entry_risk_evidence:
            if not snapshot.daily_realized_pnl_ready:
                raise BrokerMutationBlocked(
                    "IBKR_DAILY_REALIZED_PNL_NOT_AUTHORITATIVE"
                )
            source = snapshot.risk_evidence_source
            raw_daily_source = source == _DAILY_PNL_SOURCE
            authenticated_composite_source = bool(
                isinstance(source, str)
                and _AUTONOMOUS_RISK_SOURCE.fullmatch(source)
                and snapshot.authenticated_entry_risk_evidence_ready
            )
            if not (raw_daily_source or authenticated_composite_source):
                raise BrokerMutationBlocked("IBKR_DAILY_REALIZED_PNL_SOURCE_INVALID")
            if (
                require_entry_risk_evidence
                and not snapshot.authenticated_entry_risk_evidence_ready
            ):
                raise BrokerMutationBlocked(
                    "IBKR_ENTRY_RISK_EVIDENCE_INCOMPLETE"
                )
            assert snapshot.risk_evidence_as_of is not None
            risk_age = (now - snapshot.risk_evidence_as_of).total_seconds()
            if risk_age < -1 or risk_age > self._account_age:
                raise BrokerMutationBlocked(
                    "IBKR_DAILY_REALIZED_PNL_STALE_OR_FUTURE"
                )
        if observe_risk and self._plan_reader_role == "ibkr_autonomous_plan_reader":
            observer = getattr(self._risk_policy_check, "observe_account_snapshot", None)
            if callable(observer):
                try:
                    result = observer(snapshot, now, token=observation_token,
                                      entry=require_entry_risk_evidence)
                    if result is not None:
                        raise ValueError("risk observer must return no value")
                except Exception:
                    if require_entry_risk_evidence:
                        raise BrokerMutationBlocked("IBKR_RISK_OBSERVATION_NOT_COMMITTED") from None
        if snapshot.option_position_count or snapshot.option_order_count:
            raise BrokerMutationBlocked("IBKR_OPTIONS_EXPOSURE_OUTSIDE_APPROVED_SCOPE")
        if any(position.is_fractional for position in snapshot.equity_positions):
            raise BrokerMutationBlocked("IBKR_FRACTIONAL_POSITION_OUTSIDE_APPROVED_SCOPE")

    def _instrument(
        self, evidence: IbkrInstrumentEvidence, request: OrderRequest, now: datetime
    ) -> None:
        age = (now - evidence.received_at).total_seconds()
        if (
            evidence.source != "ibkr:tws-contract-details"
            or evidence.identity.symbol != request.symbol
            or evidence.identity.sec_type != "STK"
            or evidence.identity.currency != "USD"
            or evidence.identity.exchange != "SMART"
            or evidence.exchange_listed is not True
            or evidence.regular_hours_eligible is not True
            or age < -1
            or age > self._instrument_age
        ):
            raise BrokerMutationBlocked("IBKR_INSTRUMENT_EVIDENCE_INVALID")

    def _working_cash_commitment(self, snapshot: AccountSnapshot) -> Decimal:
        total = Decimal("0")
        for order in snapshot.equity_orders:
            if order.state.terminal or order.side is not BrokerSide.BUY:
                continue
            remaining = order.requested_quantity - order.cumulative_filled_quantity
            prices = tuple(
                item for item in (order.limit_price, order.stop_price) if item is not None
            )
            price = max(prices) if prices else None
            if price is None or remaining < 0:
                raise BrokerMutationBlocked("IBKR_UNRESOLVED_BUY_COMMITMENT_UNBOUNDED")
            total += remaining * price + self._existing_order_reserve
        return total

    @staticmethod
    def _candidate_cash_commitment(plan: IbkrAttendedOrderPlan) -> Decimal:
        if plan.request.side is not BrokerSide.BUY:
            return Decimal("0")
        if plan.request.limit_price is None:
            raise BrokerMutationBlocked("IBKR_BUY_COMMITMENT_PRICE_UNAVAILABLE")
        return (
            plan.request.limit_price * plan.request.quantity
            + plan.execution_reserve
            + plan.fee_reserve
        )

    @staticmethod
    def _confirmation_phrase(request: OrderRequest) -> str:
        return attended_confirmation_phrase(request)

    @classmethod
    def _request_preview(cls, request: OrderRequest | None) -> dict[str, object] | None:
        if request is None:
            return None
        return {
            "side": request.side.value,
            "symbol": request.symbol,
            "quantity": request.quantity,
            "order_type": request.order_type.value,
            "limit_price": cls._money(request.limit_price),
            "stop_price": cls._money(request.stop_price),
            "time_in_force": request.time_in_force.value,
            "market_hours": request.market_hours.value,
            "client_ref_id": request.client_ref_id,
        }

    @staticmethod
    def _money(value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerMutationBlocked("IBKR_PROTECTION_SOURCE_CLAIM_TIME_INVALID")
        return value.astimezone(timezone.utc)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerContractViolation("IBKR preflight clock must be timezone-aware")
        return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _AutonomousReviewBinding:
    decision: LocalPreflightDecision
    attended: AttendedLocalReview


@dataclass(frozen=True)
class _AutonomousCancelBinding:
    decision: LocalCancelDecision
    client_ref_id: str
    order_id: int


class IbkrAutonomousPolicyPreflightBridge:
    """Autonomous wrapper over the same exact attended policy checks.

    The wrapper does not reinterpret a confirmation phrase as authority.  It
    first requires an authenticated provider-authority contract that expressly
    permits unattended place and cancel operations, then delegates every
    account, instrument, session, risk, fill-delta and cancellation proof to the
    existing fail-closed implementation.  The attended receipt stays private;
    callers receive a distinct local policy decision with no confirmation text.
    """

    _AUTHORITY_OPERATIONS = frozenset(
        {
            "initialize",
            "review",
            "review_protection",
            "contract_for",
            "revalidate",
            "review_cancel",
            "authorize_cancel",
        }
    )

    def __init__(
        self,
        *,
        attended: IbkrAttendedPreflightBridge,
        authority: VerifiedIbkrAutonomousAuthority,
        expected_bindings: IbkrAutonomousAuthorityBindings,
        cancel_ttl_seconds: float,
    ) -> None:
        if not isinstance(attended, IbkrAttendedPreflightBridge):
            raise TypeError("concrete IBKR attended preflight delegate required")
        if type(authority) is not VerifiedIbkrAutonomousAuthority:
            raise TypeError("verified IBKR autonomous authority required")
        if type(expected_bindings) is not IbkrAutonomousAuthorityBindings:
            raise TypeError("exact IBKR autonomous authority bindings required")
        if (
            isinstance(cancel_ttl_seconds, bool)
            or not isinstance(cancel_ttl_seconds, (int, float))
            or not 0 < float(cancel_ttl_seconds) <= 30
        ):
            raise ValueError("autonomous cancel decision TTL must be in (0, 30] seconds")
        if (
            attended.account_masked != expected_bindings.account_masked
            or attended.command_client_id != expected_bindings.client_id
            or attended.policy_binding_id != expected_bindings.policy_binding_id
            or attended.provider_contract_id
            != expected_bindings.provider_contract_id
        ):
            raise BrokerMutationBlocked(
                "IBKR_AUTONOMOUS_PREFLIGHT_DELEGATE_BINDING_MISMATCH"
            )
        self._attended = attended
        self._authority = authority
        self._expected = expected_bindings
        self._cancel_ttl = float(cancel_ttl_seconds)
        self._lock = RLock()
        self._issued: dict[str, _AutonomousReviewBinding] = {}
        self._cancel_issued: dict[str, _AutonomousCancelBinding] = {}
        self._assert_authority("initialize", self._now())

    @property
    def authority_contract_id(self) -> str:
        return self._authority.contract_id

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory executable dependencies retained only by this wrapper.

        The outer production transport inventories the same immutable authority
        object once as a separate release component.  Re-enumerating it here
        would make the release graph reject legitimate object sharing.
        """

        return (
            (
                "ibkr_attended_preflight_delegate",
                self._attended,
                (
                    "release_components",
                    "review",
                    "review_protection",
                    "bind_entry_risk_activation",
                    "contract_for",
                    "revalidate",
                    "cancel_evidence",
                    "authorize_cancel",
                    "current_time",
                ),
            ),
        )

    def bind_entry_risk_activation(
        self,
        *,
        lineage_hash: str,
        minimum_peak: object,
    ) -> None:
        result = self._attended.bind_entry_risk_activation(
            lineage_hash=lineage_hash,
            minimum_peak=minimum_peak,
        )
        if result is not None:
            raise BrokerMutationBlocked(
                "IBKR_ENTRY_ACTIVATION_RISK_BINDING_INVALID"
            )
        return None

    def review(self, request: OrderRequest) -> LocalPreflightDecision:
        return self._review_from_delegate(
            request,
            operation="review",
            issue=lambda: self._attended.review(request),
        )

    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> LocalPreflightDecision:
        return self._review_from_delegate(
            None,
            operation="review_protection",
            issue=lambda: self._attended.review_protection(
                source_request,
                source_plan_id,
                stop_template,
                source_claimed_at,
            ),
        )

    def _review_from_delegate(
        self,
        expected_request: OrderRequest | None,
        *,
        operation: str,
        issue: Callable[[], AttendedLocalReview],
    ) -> LocalPreflightDecision:
        with self._lock:
            current = self._now()
            self._prune(current)
            if len(self._issued) >= 256:
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_DECISION_CAPACITY_REACHED"
                )
            if (
                expected_request is not None
                and expected_request.client_ref_id in self._issued
            ):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_DECISION_ALREADY_OUTSTANDING"
                )
            self._assert_authority(operation, current)
            attended = issue()
            post_review = self._now()
            self._assert_authority(operation, post_review)
            if not isinstance(attended, AttendedLocalReview):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_ATTENDED_DELEGATE_RECEIPT_INVALID"
                )
            if (
                expected_request is not None
                and attended.request.exact_tuple != expected_request.exact_tuple
            ):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_ATTENDED_DELEGATE_TUPLE_CHANGED"
                )
            if (
                attended.request.account_masked != self._expected.account_masked
                or attended.policy_binding_id != self._expected.policy_binding_id
                or attended.provider_contract_id
                != self._expected.provider_contract_id
                or attended.broker_bound
                or attended.broker_review_id is not None
                or attended.required_confirmation_phrase
                != attended_confirmation_phrase(attended.request)
                or not _DIGEST.fullmatch(attended.evidence_collection_id)
                or attended.expires_at is None
                or attended.reviewed_at > post_review
                or attended.received_at > post_review
                or attended.expired_at(post_review)
                or any(check.severity.upper() != "INFO" for check in attended.order_checks)
            ):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_ATTENDED_DELEGATE_RECEIPT_INVALID"
                )
            client_ref_id = attended.request.client_ref_id
            if client_ref_id in self._issued:
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_DECISION_ALREADY_OUTSTANDING"
                )
            preview = dict(attended.preview)
            preview["provider"] = (
                "IBKR TWS API (local autonomous policy preflight; not broker acceptance)"
            )
            preview["execution_authority"] = {
                "mode": "unattended",
                "authority_contract_id": self._authority.contract_id,
                "per_order_confirmation_required": False,
            }
            preview["alerts"] = (
                "This is a local autonomous policy decision, not broker acceptance.",
                "IBKR submission remains unresolved until a newer broker reconciliation.",
                "An entry fill is not protected until its separately submitted GTC stop is confirmed working.",
            )
            decision_expiry = min(attended.expires_at, self._authority.expires_at)
            if decision_expiry <= attended.received_at:
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_AUTHORITY_EXPIRES_TOO_SOON"
                )
            decision = LocalPreflightDecision(
                request=attended.request,
                reviewed_at=attended.reviewed_at,
                received_at=attended.received_at,
                expires_at=decision_expiry,
                disclosure=(
                    "Autonomous IBKR policy preflight only. A separately authenticated "
                    "provider-authority contract permits one exact API attempt; this "
                    "decision does not prove acceptance, fill, or protection."
                ),
                order_checks=(
                    *attended.order_checks,
                    OrderCheck(
                        "AUTONOMOUS_AUTHORITY",
                        "INFO",
                        "The authenticated provider-authority contract is current and exact-bound.",
                    ),
                ),
                required_confirmation_phrase=None,
                broker_review_id=None,
                broker_bound=False,
                preview=preview,
                decision_id=str(uuid4()),
                policy_binding_id=self._expected.policy_binding_id,
                evidence_collection_id=attended.evidence_collection_id,
                provider_contract_id=self._expected.provider_contract_id,
            )
            self._issued[client_ref_id] = _AutonomousReviewBinding(
                decision=decision,
                attended=attended,
            )
            return decision

    def contract_for(self, request: OrderRequest) -> IbkrContractIdentity:
        if not isinstance(request, OrderRequest):
            raise BrokerMutationBlocked("IBKR_NORMALIZED_ORDER_REQUEST_REQUIRED")
        with self._lock:
            current = self._now()
            self._prune(current)
            binding = self._issued.get(request.client_ref_id)
            if (
                binding is None
                or binding.decision.request.exact_tuple != request.exact_tuple
                or binding.decision.expired_at(current)
            ):
                raise BrokerMutationBlocked(
                    "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_DECISION_REQUIRED"
                )
            self._assert_authority("contract_for", current)
            contract = self._attended.contract_for(request)
            self._assert_authority("contract_for", self._now())
            if not isinstance(contract, IbkrContractIdentity):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_CONTRACT_IDENTITY_INVALID"
                )
            return contract

    def revalidate(
        self,
        request: OrderRequest,
        decision: LocalPreflightDecision,
    ) -> None:
        if not isinstance(request, OrderRequest) or not isinstance(
            decision, LocalPreflightDecision
        ):
            raise BrokerMutationBlocked(
                "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_DECISION_REQUIRED"
            )
        with self._lock:
            current = self._now()
            self._prune(current)
            binding = self._issued.get(request.client_ref_id)
            if (
                binding is None
                or binding.decision != decision
                or decision.request.exact_tuple != request.exact_tuple
                or decision.expired_at(current)
            ):
                raise BrokerMutationBlocked(
                    "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_DECISION_REQUIRED"
                )
            self._assert_authority("revalidate", current)
            result = self._attended.revalidate(request, binding.attended)
            if result is not None:
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_DELEGATE_MUST_RAISE_ON_DENIAL"
                )
            after = self._now()
            self._assert_authority("revalidate", after)
            if decision.expired_at(after):
                raise BrokerMutationBlocked(
                    "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_DECISION_REQUIRED"
                )
            return None

    def review_cancel(
        self,
        client_ref_id: str,
        order_id: int,
        broker_order_id: str,
    ) -> LocalCancelDecision:
        try:
            normalized_ref = str(UUID(str(client_ref_id)))
        except (ValueError, TypeError, AttributeError):
            raise BrokerMutationBlocked(
                "IBKR_CANCEL_CLIENT_REFERENCE_INVALID"
            ) from None
        if type(order_id) is not int or not 0 < order_id <= _MAX_ORDER_ID:
            raise BrokerMutationBlocked("IBKR_CANCEL_ORDER_ID_INVALID")
        expected_order_id = f"ibkr:{self._expected.client_id}:{order_id}"
        if broker_order_id != expected_order_id:
            raise BrokerMutationBlocked("IBKR_CANCEL_BROKER_ORDER_ID_INVALID")
        with self._lock:
            current = self._now()
            self._prune(current)
            if (
                len(self._cancel_issued) >= 256
                or broker_order_id in self._cancel_issued
            ):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_CANCEL_DECISION_ALREADY_OUTSTANDING_OR_FULL"
                )
            self._assert_authority("review_cancel", current)
            evidence_id = self._attended.cancel_evidence(normalized_ref, order_id)
            after = self._now()
            self._assert_authority("review_cancel", after)
            if not isinstance(evidence_id, str) or not _DIGEST.fullmatch(evidence_id):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_CANCEL_EVIDENCE_INVALID"
                )
            expires_at = min(
                after + timedelta(seconds=self._cancel_ttl),
                self._authority.expires_at,
            )
            if expires_at <= after:
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_AUTHORITY_EXPIRES_TOO_SOON"
                )
            decision = LocalCancelDecision(
                decision_id=str(uuid4()),
                account_masked=self._expected.account_masked,
                broker_order_id=broker_order_id,
                client_ref_id=normalized_ref,
                reviewed_at=current,
                received_at=after,
                expires_at=expires_at,
                disclosure=(
                    "Autonomous IBKR cancellation policy decision only. Cancellation "
                    "is asynchronous and exposure remains until newer broker evidence "
                    "confirms a terminal order state."
                ),
                order_checks=(
                    OrderCheck(
                        "AUTONOMOUS_AUTHORITY",
                        "INFO",
                        "The authenticated unattended-cancel contract is current and exact-bound.",
                    ),
                    OrderCheck(
                        "EXACT_BROKER_ORDER",
                        "INFO",
                        "A fresh whole-account read proved the exact owned order remains cancellable.",
                    ),
                ),
                policy_binding_id=self._expected.policy_binding_id,
                evidence_collection_id=evidence_id,
                provider_contract_id=self._expected.provider_contract_id,
                preview={
                    "provider": "IBKR TWS API (local autonomous cancellation preflight)",
                    "account_masked": self._expected.account_masked,
                    "action": "cancel",
                    "broker_order_id": broker_order_id,
                    "client_ref_id": normalized_ref,
                    "authority_contract_id": self._authority.contract_id,
                },
            )
            self._cancel_issued[broker_order_id] = _AutonomousCancelBinding(
                decision=decision,
                client_ref_id=normalized_ref,
                order_id=order_id,
            )
            return decision

    def authorize_cancel(self, client_ref_id: str, order_id: int) -> None:
        try:
            normalized_ref = str(UUID(str(client_ref_id)))
        except (ValueError, TypeError, AttributeError):
            raise BrokerMutationBlocked(
                "IBKR_CANCEL_CLIENT_REFERENCE_INVALID"
            ) from None
        if type(order_id) is not int or not 0 < order_id <= _MAX_ORDER_ID:
            raise BrokerMutationBlocked("IBKR_CANCEL_ORDER_ID_INVALID")
        broker_order_id = f"ibkr:{self._expected.client_id}:{order_id}"
        with self._lock:
            current = self._now()
            self._prune(current)
            binding = self._cancel_issued.get(broker_order_id)
            if (
                binding is None
                or binding.client_ref_id != normalized_ref
                or binding.order_id != order_id
                or binding.decision.expired_at(current)
            ):
                raise BrokerMutationBlocked(
                    "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_CANCEL_DECISION_REQUIRED"
                )
            self._assert_authority("authorize_cancel", current)
            evidence_id = self._attended.cancel_evidence(normalized_ref, order_id)
            if not isinstance(evidence_id, str) or not _DIGEST.fullmatch(evidence_id):
                raise BrokerMutationBlocked(
                    "IBKR_AUTONOMOUS_CANCEL_EVIDENCE_INVALID"
                )
            after = self._now()
            self._assert_authority("authorize_cancel", after)
            if binding.decision.expired_at(after):
                raise BrokerMutationBlocked(
                    "IBKR_EXACT_UNEXPIRED_AUTONOMOUS_CANCEL_DECISION_REQUIRED"
                )
            return None

    def finish_cancel(self, decision: LocalCancelDecision) -> None:
        """Consume the exact in-memory cancel decision after one SDK attempt."""

        if not isinstance(decision, LocalCancelDecision):
            raise BrokerMutationBlocked("IBKR_CANCEL_DECISION_INVALID")
        with self._lock:
            binding = self._cancel_issued.get(decision.broker_order_id)
            if binding is not None and binding.decision == decision:
                del self._cancel_issued[decision.broker_order_id]

    def _assert_authority(self, operation: str, now: datetime) -> None:
        if operation not in self._AUTHORITY_OPERATIONS:
            raise BrokerMutationBlocked("IBKR_AUTONOMOUS_OPERATION_OUTSIDE_CONTRACT")
        expected = self._expected
        try:
            result = self._authority.assert_current(
                now,
                expected.release_manifest_hash,
                expected.config_hash,
                expected.policy_binding_id,
                expected.account_masked,
                expected.account_binding_fingerprint,
                expected.authorization_binding_id,
                expected.provider_contract_id,
                expected.transport_id,
                expected.environment,
                expected.client_id,
                expected_account_key=expected.account_key,
                expected_api_name=expected.api_name,
                expected_api_version=expected.api_version,
            )
        except Exception:
            raise BrokerMutationBlocked(
                "IBKR_AUTONOMOUS_AUTHORITY_NOT_CURRENT"
            ) from None
        if result is not None:
            raise BrokerMutationBlocked(
                "IBKR_AUTONOMOUS_AUTHORITY_MUST_RAISE_ON_DENIAL"
            )

    def _prune(self, now: datetime) -> None:
        self._issued = {
            key: binding
            for key, binding in self._issued.items()
            if not binding.decision.expired_at(now)
        }
        self._cancel_issued = {
            key: binding
            for key, binding in self._cancel_issued.items()
            if not binding.decision.expired_at(now)
        }

    def _now(self) -> datetime:
        value = self._attended.current_time()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerContractViolation(
                "IBKR autonomous preflight clock must be timezone-aware"
            )
        return value.astimezone(timezone.utc)


__all__ = [
    "ApprovedRiskPolicyCheck",
    "AttendedPlanReader",
    "IbkrAutonomousPolicyPreflightBridge",
    "IbkrAttendedOrderPlan",
    "IbkrAttendedPreflightBridge",
    "IbkrOrderPurpose",
    "WholeAccountSnapshotReader",
]
