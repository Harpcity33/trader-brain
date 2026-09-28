"""Read-only production transport for the attended IBKR coordinator.

The persistent coordinator owns one authenticated read client for exhaustive
reconciliation.  It never owns the command client or authority artifacts;
every mutation/review method is an explicit hard denial.
"""

from __future__ import annotations

from .base import (
    BrokerMutationBlocked,
    ClientRefLookupResult,
    OrderFamily,
    OrderRequest,
    ReviewReceipt,
)
from .ibkr_account import IbkrStableAccountSnapshotReader
from .ibkr_read import IbkrWholeAccountReadBridge
from .production import (
    AccountEvidence,
    OrderFamilyPage,
    ProductionTransportDescriptor,
)


class IbkrReconciliationTransport:
    """Expose exhaustive reads while denying coordinator command surfaces."""

    def __init__(
        self,
        *,
        descriptor: ProductionTransportDescriptor,
        reads: IbkrWholeAccountReadBridge,
        stable_reader: IbkrStableAccountSnapshotReader,
    ) -> None:
        if not isinstance(descriptor, ProductionTransportDescriptor):
            raise TypeError("normalized IBKR descriptor required")
        if not isinstance(reads, IbkrWholeAccountReadBridge):
            raise TypeError("concrete IBKR whole-account bridge required")
        if not isinstance(stable_reader, IbkrStableAccountSnapshotReader):
            raise TypeError("concrete IBKR stable account reader required")
        if not descriptor.capabilities.can_prove_whole_broker_reconciliation:
            raise ValueError("IBKR exhaustive order coverage is required")
        self._descriptor = descriptor
        self._reads = reads
        self._stable_reader = stable_reader

    @property
    def descriptor(self) -> ProductionTransportDescriptor:
        return self._descriptor

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_read_bridge",
                self._reads,
                (
                    "release_components",
                    "get_account_base",
                    "list_order_family_page",
                    "lookup_equity_orders_by_client_ref",
                ),
            ),
            (
                "ibkr_account_snapshot_reader",
                self._stable_reader,
                ("release_components", "coverage", "__call__"),
            ),
        )

    def get_account_base(self, exact_account_id: str) -> AccountEvidence:
        return self._reads.get_account_base(exact_account_id)

    def list_order_family_page(
        self,
        exact_account_id: str,
        family: OrderFamily,
        cursor: str | None,
    ) -> OrderFamilyPage:
        return self._reads.list_order_family_page(exact_account_id, family, cursor)

    def lookup_equity_orders_by_client_ref(
        self,
        exact_account_id: str,
        client_refs: tuple[str, ...],
    ) -> ClientRefLookupResult:
        return self._reads.lookup_equity_orders_by_client_ref(
            exact_account_id, client_refs
        )

    @staticmethod
    def _deny() -> None:
        raise BrokerMutationBlocked("IBKR_COORDINATOR_READ_ONLY")

    def review_equity_order(self, exact_account_id: str, request: OrderRequest):
        del exact_account_id, request
        self._deny()

    def place_equity_order(
        self,
        exact_account_id: str,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ):
        del exact_account_id, request, review, explicit_confirmation
        self._deny()

    def cancel_equity_order(
        self,
        exact_account_id: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ):
        del exact_account_id, broker_order_id, explicit_confirmation
        self._deny()


__all__ = ["IbkrReconciliationTransport"]
