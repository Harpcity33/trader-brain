"""Bind each SDK dispatch to an existing durable intent and exact preflight.

There is no default acceptance verifier. The installed runtime must supply one
that authenticates the reviewed broker/policy/release contract; dataclass fields
or configuration booleans cannot establish it. This module never opens a socket
or marks a broker order accepted. Contexts exist only for one dispatch attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import RLock
from typing import Callable

from .base import (
    AttendedCancelReview,
    AttendedLocalReview,
    BrokerMutationBlocked,
    LocalCancelDecision,
    LocalPreflightDecision,
    OrderRequest,
)
from .ibkr_ledger import IbkrExecutionLedger
from .ibkr_orders import IbkrContractIdentity, IbkrOrderPlan
from .ibkr_sdk import IbkrDispatchRequest, IbkrWriteEvidence, assert_sdk_order_matches_plan


def ibkr_intent_fingerprint(
    request: OrderRequest, contract: IbkrContractIdentity, *,
    account_binding_fingerprint: str, environment: str, client_id: int,
) -> str:
    """Hash an exact request/contract/binding, excluding plaintext account ID."""
    payload = {
        "request": request.exact_tuple,
        "contract": (contract.con_id, contract.symbol, contract.primary_exchange,
                     contract.sec_type, contract.currency, contract.exchange),
        "environment": environment, "account_binding": account_binding_fingerprint,
        "client_id": client_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class _DispatchContext:
    client_ref_id: str
    operation: str
    generation: int
    payload_fingerprint: str | None
    plan: IbkrOrderPlan | None = None
    review: AttendedLocalReview | LocalPreflightDecision | None = None
    cancel_review: AttendedCancelReview | LocalCancelDecision | None = None


class IbkrDispatchAuthority:
    """Concrete identity/risk join invoked before SDK call AND socket send.

    A verifier must raise on invalid acceptance and return exactly None on
    success. Revalidation/cancel callbacks have the same convention. Exceptions
    are sanitized by the SDK boundary. No context survives ``finish``; reconnect
    generations cannot reuse an earlier context or reviewed SDK write receipt.
    """

    def __init__(
        self, *, ledger: IbkrExecutionLedger, authorization_binding_id: str,
        provider_contract_id: str, policy_binding_id: str,
        verify_acceptance: Callable[[IbkrWriteEvidence], None] | None = None,
        revalidate: Callable[
            [OrderRequest, AttendedLocalReview | LocalPreflightDecision], None
        ] | None = None,
        authorize_cancel: Callable[[str, int], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(ledger, IbkrExecutionLedger):
            raise TypeError("IBKR authority requires its concrete durable ledger")
        for value in (authorization_binding_id, provider_contract_id, policy_binding_id):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("IBKR authority bindings must be SHA-256 receipts")
        self.ledger = ledger
        self.authorization_binding_id = authorization_binding_id
        self.provider_contract_id = provider_contract_id
        self.policy_binding_id = policy_binding_id
        self._verify = verify_acceptance
        self._revalidate = revalidate
        self._authorize_cancel = authorize_cancel
        self._clock = clock
        self._contexts: dict[int, _DispatchContext] = {}
        self._lock = RLock()

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory retained authorization leaves or refuse release binding."""

        if any(callback is None for callback in (
            self._verify,
            self._revalidate,
            self._authorize_cancel,
        )):
            raise BrokerMutationBlocked("IBKR authority dependency inventory is incomplete")
        # The session inventories the shared clock and this authority object.
        # The transport inventories the preflight object whose exact bound
        # methods are retained in _revalidate/_authorize_cancel.
        return (
            ("ibkr_execution_ledger", self.ledger, (
                "lookup", "lookup_order_id", "allocate_intent", "mark_sending",
                "record_submit_not_sent", "claim_cancel", "record_cancel_not_sent",
                "authorize_cancel_retry", "record_event", "close",
            )),
            ("ibkr_acceptance_verifier", self._verify, ("__call__",)),
            ("ibkr_revalidation_callback", self._revalidate, ("__call__",)),
            ("ibkr_cancel_authorizer", self._authorize_cancel, ("__call__",)),
        )

    @staticmethod
    def _approve(callback, *args) -> None:
        if callback is None or callback(*args) is not None:
            raise BrokerMutationBlocked("IBKR authenticated execution acceptance is unavailable")

    def _register(self, order_id: int, context: _DispatchContext) -> None:
        with self._lock:
            if type(context.generation) is not int or context.generation <= 0:
                raise BrokerMutationBlocked("IBKR dispatch generation is invalid")
            if order_id in self._contexts or len(self._contexts) >= 256:
                raise BrokerMutationBlocked("IBKR dispatch context is already present or full")
            self._contexts[order_id] = context

    def register_submission(
        self, *, plan: IbkrOrderPlan,
        review: AttendedLocalReview | LocalPreflightDecision,
        sdk_contract: object, sdk_order: object, generation: int,
    ) -> None:
        if not isinstance(plan, IbkrOrderPlan) or not isinstance(
            review, (AttendedLocalReview, LocalPreflightDecision)
        ):
            raise BrokerMutationBlocked("IBKR exact plan and normalized review are required")
        # Derive the digest only AFTER proving every SDK field matches the
        # immutable reviewed plan/pinned SDK defaults. A caller-supplied digest
        # would prove self-consistency, not that this is the reviewed quantity.
        payload_fingerprint = assert_sdk_order_matches_plan(
            plan, sdk_contract, sdk_order, self.ledger.account_fingerprint,
        )
        intent = self.ledger.lookup(plan.request.client_ref_id)
        if intent is None or intent.order_id != plan.order_id or not intent.can_transmit:
            raise BrokerMutationBlocked("IBKR submission has no unsent durable intent")
        if plan.client_id != self.ledger.client_id or review.request.exact_tuple != plan.request.exact_tuple:
            raise BrokerMutationBlocked("IBKR plan/preflight client or request mismatch")
        expected = ibkr_intent_fingerprint(
            plan.request, plan.contract, account_binding_fingerprint=self.ledger.account_fingerprint,
            environment=self.ledger.environment, client_id=self.ledger.client_id,
        )
        if intent.request_fingerprint != expected:
            raise BrokerMutationBlocked("IBKR durable request fingerprint mismatch")
        self._register(plan.order_id, _DispatchContext(
            plan.request.client_ref_id, "submit", generation, payload_fingerprint, plan, review,
        ))

    def register_cancel(
        self, *, client_ref_id: str, order_id: int, generation: int,
        review: AttendedCancelReview | LocalCancelDecision,
    ) -> None:
        intent = self.ledger.lookup(client_ref_id)
        attended_valid = bool(
            isinstance(review, AttendedCancelReview)
            and review.required_confirmation_phrase
            == f"CONFIRM CANCEL {review.broker_order_id}"
        )
        autonomous_valid = bool(
            isinstance(review, LocalCancelDecision)
            and review.policy_binding_id == self.policy_binding_id
            and review.provider_contract_id == self.provider_contract_id
            and re.fullmatch(r"[0-9a-f]{64}", review.evidence_collection_id)
        )
        if (
            not isinstance(review, (AttendedCancelReview, LocalCancelDecision))
            or intent is None
            or intent.order_id != order_id
            or not intent.can_cancel
            or review.client_ref_id != client_ref_id
            or review.broker_order_id != f"ibkr:{self.ledger.client_id}:{order_id}"
            or not (attended_valid or autonomous_valid)
            or any(check.severity.upper() != "INFO" for check in review.order_checks)
        ):
            raise BrokerMutationBlocked("IBKR cancellation has no cancellable owned intent")
        self._register(
            order_id,
            _DispatchContext(
                client_ref_id,
                "cancel",
                generation,
                None,
                cancel_review=review,
            ),
        )

    def finish(self, order_id: int) -> None:
        with self._lock:
            self._contexts.pop(order_id, None)

    def _current(
        self, context: _DispatchContext, dispatch: IbkrDispatchRequest,
        evidence: IbkrWriteEvidence,
    ) -> None:
        """Pure last-boundary checks, deliberately repeated after callbacks."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise BrokerMutationBlocked("IBKR dispatch clock is unavailable")
        if (
            self._contexts.get(dispatch.order_id) is not context
            or context.operation != dispatch.operation
            or context.generation != dispatch.generation
            or context.payload_fingerprint != dispatch.payload_fingerprint
            or not evidence.issued_at <= now < evidence.expires_at
            or evidence.authorization_binding_id != self.authorization_binding_id
            or evidence.reviewed_contract_id != self.provider_contract_id
        ):
            raise BrokerMutationBlocked("IBKR dispatch context or acceptance changed")
        intent = self.ledger.lookup(context.client_ref_id)
        if intent is None or intent.order_id != dispatch.order_id or not intent.send_started:
            raise BrokerMutationBlocked("IBKR durable dispatch claim is absent")
        if dispatch.operation == "cancel":
            review = context.cancel_review
            if (
                not isinstance(review, (AttendedCancelReview, LocalCancelDecision))
                or review.client_ref_id != context.client_ref_id
                or review.reviewed_at > now
                or review.received_at > now
                or review.expired_at(now)
                or not intent.cancel_claim_active
                or intent.cancelled_seen
                or intent.cancellation_unknown_seen
                or intent.rejection_seen
            ):
                raise BrokerMutationBlocked("IBKR cancel is terminal, uncertain or unclaimed")
            if isinstance(review, LocalCancelDecision) and (
                review.policy_binding_id != self.policy_binding_id
                or review.provider_contract_id != self.provider_contract_id
                or not re.fullmatch(r"[0-9a-f]{64}", review.evidence_collection_id)
            ):
                raise BrokerMutationBlocked("IBKR autonomous cancel binding changed")
            return
        plan, review = context.plan, context.review
        if (
            plan is None or review is None or intent.status != "SENDING"
            or review.policy_binding_id != self.policy_binding_id
            or review.provider_contract_id != self.provider_contract_id
            or review.expires_at is None or review.expired_at(now)
            or review.reviewed_at > now or review.received_at > now
            or intent.request_fingerprint != ibkr_intent_fingerprint(
                plan.request, plan.contract, account_binding_fingerprint=self.ledger.account_fingerprint,
                environment=self.ledger.environment, client_id=self.ledger.client_id,
            )
        ):
            raise BrokerMutationBlocked("IBKR durable intent/preflight no longer authorizes dispatch")

    def __call__(self, dispatch: IbkrDispatchRequest, evidence: IbkrWriteEvidence) -> None:
        with self._lock:
            if not isinstance(dispatch, IbkrDispatchRequest) or not isinstance(evidence, IbkrWriteEvidence):
                raise BrokerMutationBlocked("IBKR dispatch/evidence shape is invalid")
            if (
                dispatch.account_binding_fingerprint != self.ledger.account_fingerprint
                or dispatch.environment != self.ledger.environment
                or dispatch.client_id != self.ledger.client_id
                or evidence.authorization_binding_id != self.authorization_binding_id
                or evidence.reviewed_contract_id != self.provider_contract_id
                or evidence.account_binding_fingerprint != self.ledger.account_fingerprint
                or evidence.environment != self.ledger.environment
                or evidence.client_id != self.ledger.client_id
            ):
                raise BrokerMutationBlocked("IBKR dispatch acceptance/binding mismatch")
            self._approve(self._verify, evidence)
            context = self._contexts.get(dispatch.order_id)
            if (
                context is None or context.operation != dispatch.operation
                or context.generation != dispatch.generation
                or context.payload_fingerprint != dispatch.payload_fingerprint
            ):
                raise BrokerMutationBlocked("IBKR dispatch is not the exact registered request")
            self._current(context, dispatch, evidence)
            if dispatch.operation == "cancel":
                self._approve(self._authorize_cancel, context.client_ref_id, dispatch.order_id)
            else:
                self._approve(self._revalidate, context.plan.request, context.review)
            # Callbacks can consume time, revoke acceptance, remove the context
            # or process an incoming fill/cancel. Do not return an old approval.
            self._approve(self._verify, evidence)
            self._current(context, dispatch, evidence)


__all__ = ["IbkrDispatchAuthority", "ibkr_intent_fingerprint"]
