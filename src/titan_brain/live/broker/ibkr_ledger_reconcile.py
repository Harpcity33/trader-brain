"""Broker-authoritative IBKR facts into the one-shot execution ledger.

This bridge only adopts positive, identity-complete order and execution facts.
An order missing from a collection is never a rejection and never grants retry
authority.  API order IDs are accepted only in the namespace of the ledger's
permanently bound client ID and must agree with the locally allocated intent and
the UUID carried by IBKR ``orderRef``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable

from ..models import BrokerOrderState
from .base import BrokerContractViolation, OrderSnapshot
from .ibkr_ledger import IbkrExecutionLedger, IntentRecord


_REJECTED = frozenset({
    BrokerOrderState.REJECTED,
    BrokerOrderState.FAILED,
    BrokerOrderState.VOIDED,
    BrokerOrderState.LOCATE_FAILED,
})
_CANCELLED = frozenset({
    BrokerOrderState.CANCELLED,
    BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
})


@dataclass(frozen=True)
class IbkrLedgerReconciliationReport:
    inspected_orders: int
    owned_orders: int
    recorded_fills: int
    reconciled_commissions: int
    acknowledged_orders: int
    rejected_orders: int
    pending_cancel_orders: int
    cancelled_orders: int


class IbkrLedgerReconciler:
    """Monotonically append exact IBKR evidence without inferring absence."""

    def __init__(
        self,
        ledger: IbkrExecutionLedger,
        *,
        account_masked: str,
    ) -> None:
        if not isinstance(ledger, IbkrExecutionLedger):
            raise TypeError("concrete IBKR execution ledger required")
        if not isinstance(account_masked, str) or not re.fullmatch(
            r"(?:\*{4}|•{4})[0-9]{4}", account_masked
        ):
            raise ValueError("masked IBKR account binding required")
        self.ledger = ledger
        self.account_masked = account_masked

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Declare no additional ownership of the shared execution ledger.

        ``IbkrProductionTransport`` already inventories the exact ledger via
        its dispatch authority.  Repeating that same object below this helper
        would make a sound release graph look like two independently owned
        dependencies.  The reconciler itself is still release-attested by the
        transport, including this enumerator and ``reconcile_orders``.
        """

        return ()

    @staticmethod
    def _event_id(order: OrderSnapshot, kind: str, perm_id: int | None) -> str:
        digest = hashlib.sha256(
            repr((
                order.broker_order_id,
                order.client_ref_id,
                kind,
                perm_id,
                order.state.value,
                str(order.cumulative_filled_quantity),
                order.broker_updated_at.isoformat(),
            )).encode("ascii")
        ).hexdigest()
        return f"reconcile-{kind.lower()}-{digest}"

    @staticmethod
    def _commission_report_id(
        *, exec_id: str, commission: object, currency: str
    ) -> str:
        """Create an idempotent fact identity for an IBKR commission report.

        The socket callback exposes an execution ID, amount, and currency but
        no separate immutable report ID.  Repeated collections of the same
        fact therefore share this digest, while a later correction or rebate
        has a distinct append-only identity.
        """

        digest = hashlib.sha256(
            repr((exec_id, str(commission), currency)).encode("ascii")
        ).hexdigest()
        return f"ibkr-commission-{digest}"

    def _owned_intent(self, order: OrderSnapshot) -> IntentRecord | None:
        if not isinstance(order, OrderSnapshot):
            raise BrokerContractViolation("IBKR_LEDGER_ORDER_TYPE_INVALID")
        if order.account_masked != self.account_masked:
            raise BrokerContractViolation("IBKR_LEDGER_ORDER_ACCOUNT_MISMATCH")

        by_ref = (
            self.ledger.lookup(order.client_ref_id)
            if order.client_ref_id is not None
            else None
        )
        match = re.fullmatch(r"ibkr:([0-9]+):([1-9][0-9]*)", order.broker_order_id)
        if match is None:
            if by_ref is not None:
                raise BrokerContractViolation("IBKR_LEDGER_OWNED_ORDER_IDENTITY_CHANGED")
            return None
        client_id, order_id = (int(value) for value in match.groups())
        if client_id != self.ledger.client_id:
            if by_ref is not None:
                raise BrokerContractViolation("IBKR_LEDGER_OWNED_ORDER_CLIENT_CHANGED")
            return None

        by_order_id = self.ledger.lookup_order_id(order_id)
        if by_order_id is None:
            if by_ref is not None:
                raise BrokerContractViolation("IBKR_LEDGER_OWNED_ORDER_ID_CHANGED")
            return None
        if order.client_ref_id != by_order_id.client_ref_id:
            raise BrokerContractViolation("IBKR_LEDGER_ORDER_REF_MISMATCH")
        if by_ref is None or by_ref.client_ref_id != by_order_id.client_ref_id:
            raise BrokerContractViolation("IBKR_LEDGER_ORDER_IDENTITY_CONFLICT")
        if not by_order_id.send_started:
            raise BrokerContractViolation("IBKR_LEDGER_ORDER_PRECEDES_SEND_CLAIM")
        return by_order_id

    @staticmethod
    def _perm_id(order: OrderSnapshot) -> int | None:
        ids = {
            value
            for value in (
                order.broker_perm_id,
                *(fill.broker_perm_id for fill in order.fills),
            )
            if value is not None
        }
        if len(ids) > 1:
            raise BrokerContractViolation("IBKR_LEDGER_PERMANENT_ID_CONFLICT")
        return next(iter(ids), None)

    def reconcile_orders(
        self, orders: Iterable[OrderSnapshot]
    ) -> IbkrLedgerReconciliationReport:
        normalized = tuple(orders)
        owned: list[tuple[OrderSnapshot, IntentRecord, int | None]] = []
        seen_refs: set[str] = set()
        for order in normalized:
            intent = self._owned_intent(order)
            if intent is None:
                continue
            if intent.client_ref_id in seen_refs:
                raise BrokerContractViolation("IBKR_LEDGER_DUPLICATE_OWNED_ORDER")
            seen_refs.add(intent.client_ref_id)
            perm_id = self._perm_id(order)
            if intent.perm_id is not None and perm_id is not None and intent.perm_id != perm_id:
                raise BrokerContractViolation("IBKR_LEDGER_PERMANENT_ID_CHANGED")
            if order.fills and perm_id is None:
                raise BrokerContractViolation("IBKR_LEDGER_FILL_PERMANENT_ID_MISSING")
            if order.state in _REJECTED and order.fills:
                raise BrokerContractViolation("IBKR_LEDGER_REJECTED_ORDER_HAS_EXECUTIONS")
            owned.append((order, intent, perm_id))

        fills = commissions = acknowledged = rejected = pending_cancel = cancelled = 0
        try:
            # The complete positive-evidence batch has one commit point.  A
            # conflict in any later order/fill rolls back every earlier append
            # from this broker snapshot rather than exposing a prefix as if the
            # snapshot had reconciled successfully.
            with self.ledger.reconciliation_batch():
                for order, intent, perm_id in owned:
                    ref = intent.client_ref_id
                    if order.state in _REJECTED:
                        self.ledger.record_event(
                            ref,
                            self._event_id(order, "REJECT", perm_id),
                            "REJECT",
                            perm_id=perm_id,
                        )
                        rejected += 1
                        continue

                    self.ledger.record_event(
                        ref,
                        self._event_id(order, "ACK", perm_id),
                        "ACK",
                        perm_id=perm_id,
                    )
                    acknowledged += 1
                    for fill in order.fills:
                        self.ledger.record_fill(
                            ref,
                            fill.fill_id,
                            fill.quantity,
                            fill.price,
                            perm_id=fill.broker_perm_id or perm_id,  # type: ignore[arg-type]
                            executed_at=fill.executed_at,
                        )
                        fills += 1
                        if (
                            fill.provider_commission is None
                            or fill.provider_commission_currency is None
                        ):
                            raise BrokerContractViolation(
                                "IBKR_LEDGER_FILL_COMMISSION_EVIDENCE_MISSING"
                            )
                        self.ledger.record_commission(
                            fill.fill_id,
                            self._commission_report_id(
                                exec_id=fill.fill_id,
                                commission=fill.provider_commission,
                                currency=fill.provider_commission_currency,
                            ),
                            fill.provider_commission,
                            fill.provider_commission_currency,
                        )
                        commissions += 1

                    if order.state is BrokerOrderState.PENDING_CANCELLED:
                        self.ledger.record_event(
                            ref,
                            self._event_id(order, "PENDING_CANCEL", perm_id),
                            "PENDING_CANCEL",
                            perm_id=perm_id,
                        )
                        pending_cancel += 1
                    elif order.state in _CANCELLED:
                        self.ledger.record_event(
                            ref,
                            self._event_id(order, "CANCELLED", perm_id),
                            "CANCELLED",
                            perm_id=perm_id,
                        )
                        cancelled += 1
        except BrokerContractViolation:
            raise
        except Exception:
            raise BrokerContractViolation("IBKR_LEDGER_RECONCILIATION_CONFLICT") from None

        return IbkrLedgerReconciliationReport(
            inspected_orders=len(normalized),
            owned_orders=len(owned),
            recorded_fills=fills,
            reconciled_commissions=commissions,
            acknowledged_orders=acknowledged,
            rejected_orders=rejected,
            pending_cancel_orders=pending_cancel,
            cancelled_orders=cancelled,
        )


__all__ = ["IbkrLedgerReconciler", "IbkrLedgerReconciliationReport"]
