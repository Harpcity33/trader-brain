"""Stable whole-account assembly over one authenticated IBKR read generation.

The TWS API read lane is a callback collection, not an atomic snapshot.  This
adapter performs two complete non-overlapping collections and accepts only a
materially stable second result.  The exact account identifier is retained
inside this object and is never returned by its public read API.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from .base import (
    AccountSnapshot,
    ClientRefRecoverySource,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
    OrderSnapshot,
)
from .ibkr_read import IbkrWholeAccountReadBridge
from .production import CollectedObservation, OrderFamilyPage


_CLOCK_SKEW = timedelta(seconds=2)


class IbkrStableAccountSnapshotReader:
    """Generation-current, double-collected account and order-family reader."""

    def __init__(
        self,
        *,
        reads: IbkrWholeAccountReadBridge,
        exact_account_id: str,
        account_masked: str,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(reads, IbkrWholeAccountReadBridge):
            raise TypeError("concrete IBKR whole-account read bridge required")
        if not isinstance(exact_account_id, str) or not exact_account_id:
            raise ValueError("exact IBKR account is required privately")
        if not isinstance(account_masked, str) or not account_masked:
            raise ValueError("masked IBKR account is required")
        if not callable(clock):
            raise TypeError("IBKR account reader clock must be callable")
        self._reads = reads
        self._exact_account_id = exact_account_id
        self.account_masked = account_masked
        self._clock = clock
        self._coverage: OrderCoverageContract | None = None

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        # The production transport inventories the shared read bridge exactly
        # once.  Re-enumerating it here would turn valid sharing into a release
        # ambiguity.  This adapter exclusively owns its distinct clock.
        return (("ibkr_stable_account_clock", self._clock, ("__call__",)),)

    @property
    def coverage(self) -> OrderCoverageContract:
        if self._coverage is None:
            raise RuntimeError("IBKR_STABLE_ACCOUNT_COVERAGE_UNAVAILABLE")
        return self._coverage

    def __call__(self) -> AccountSnapshot:
        first, first_pages, first_evidence = self._collect()
        second, second_pages, second_evidence = self._collect()
        if (
            second_evidence.request_started_at
            < first_evidence.request_completed_at - _CLOCK_SKEW
            or self._material(first) != self._material(second)
            or self._watermarks(first_pages) != self._watermarks(second_pages)
            or (
                first_evidence.order_event_watermark is not None
                and second_evidence.order_event_watermark is not None
                and first_evidence.order_event_watermark
                != second_evidence.order_event_watermark
            )
        ):
            raise RuntimeError("IBKR_STABLE_ACCOUNT_STATE_MOVED")
        self._coverage = self._coverage_from(second_pages, second.observed_at)
        return second

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("IBKR_STABLE_ACCOUNT_CLOCK_INVALID")
        return value.astimezone(timezone.utc)

    def _collect(
        self,
    ) -> tuple[
        AccountSnapshot,
        dict[OrderFamily, OrderFamilyPage],
        CollectedObservation,
    ]:
        started = self._now()
        evidence = self._reads.get_account_base(self._exact_account_id)
        completed = self._now()
        if (
            not isinstance(evidence, CollectedObservation)
            or evidence.request_started_at < started - _CLOCK_SKEW
            or evidence.request_completed_at > completed + _CLOCK_SKEW
            or evidence.snapshot.account_masked != self.account_masked
            or evidence.snapshot.equity_orders
            or evidence.snapshot.option_order_count
            or evidence.snapshot.advanced_order_count
        ):
            raise RuntimeError("IBKR_STABLE_ACCOUNT_BASE_INVALID")
        pages: dict[OrderFamily, OrderFamilyPage] = {}
        for family in OrderFamily:
            page = self._reads.list_order_family_page(
                self._exact_account_id, family, None
            )
            if (
                not isinstance(page, OrderFamilyPage)
                or page.account_masked != self.account_masked
                or page.family is not family
                or page.collection_id != evidence.collection_id
                or page.snapshot_token is not None
                or page.page_index != 0
                or page.page_complete is not True
                or page.next_cursor is not None
            ):
                raise RuntimeError("IBKR_STABLE_ACCOUNT_ORDER_FAMILY_INCOMPLETE")
            pages[family] = page
        standard = pages[OrderFamily.STANDARD_EQUITY]
        advanced = pages[OrderFamily.ADVANCED_EQUITY]
        option = pages[OrderFamily.OPTION]
        equity_orders = (*standard.orders, *advanced.orders)
        order_ids = tuple(item.broker_order_id for item in equity_orders)
        if len(order_ids) != len(set(order_ids)):
            raise RuntimeError("IBKR_STABLE_ACCOUNT_ORDER_ID_DUPLICATE")
        observed_at = min(
            [evidence.snapshot.observed_at]
            + [page.observed_at for page in pages.values()]
        )
        received_at = max(
            [evidence.snapshot.received_at]
            + [page.received_at for page in pages.values()]
        )
        # TWS exposes all current open orders plus only the current day's
        # completed-order roster.  This is a complete bounded collection, not
        # exhaustive cross-date history.  Keep every whole-account completion
        # bit false until a separate provider contract proves that stronger
        # property; positive records remain available for conservative
        # reconciliation and exact-reference recovery.
        snapshot = replace(
            evidence.snapshot,
            observed_at=observed_at,
            received_at=received_at,
            equity_orders=equity_orders,
            option_order_count=option.active_order_count,
            advanced_order_count=advanced.active_order_count,
            standard_equity_orders_complete=False,
            option_orders_complete=False,
            advanced_orders_complete=False,
        )
        return snapshot, pages, evidence

    @staticmethod
    def _order(order: OrderSnapshot) -> tuple[object, ...]:
        return (
            order.broker_order_id,
            order.symbol,
            order.side,
            order.order_type,
            order.state,
            order.requested_quantity,
            order.cumulative_filled_quantity,
            order.market_hours,
            order.time_in_force,
            order.limit_price,
            order.stop_price,
            order.client_ref_id,
            order.broker_updated_at,
            order.fills,
        )

    @classmethod
    def _material(cls, snapshot: AccountSnapshot) -> tuple[object, ...]:
        return (
            snapshot.account_masked,
            snapshot.account_state,
            snapshot.account_type,
            snapshot.funds,
            snapshot.equity_positions,
            tuple(sorted(cls._order(item) for item in snapshot.equity_orders)),
            snapshot.option_position_count,
            snapshot.option_order_count,
            snapshot.advanced_order_count,
            snapshot.standard_equity_positions_complete,
            snapshot.standard_equity_orders_complete,
            snapshot.option_positions_complete,
            snapshot.option_orders_complete,
            snapshot.advanced_orders_complete,
            snapshot.daily_realized_pnl,
            snapshot.risk_evidence_authoritative,
            snapshot.risk_evidence_source,
        )

    @staticmethod
    def _watermarks(
        pages: dict[OrderFamily, OrderFamilyPage],
    ) -> tuple[tuple[str, str | None, tuple[str, ...]], ...]:
        return tuple(
            sorted(
                (
                    family.value,
                    page.provider_watermark,
                    tuple(item.broker_order_id for item in page.orders),
                )
                for family, page in pages.items()
            )
        )

    @staticmethod
    def _coverage_from(
        pages: dict[OrderFamily, OrderFamilyPage], observed_at: datetime
    ) -> OrderCoverageContract:
        return OrderCoverageContract(
            contract_version="ibkr-open-plus-current-day-completed-v1",
            evidence_observed_at=observed_at,
            families=tuple(
                OrderFamilyCoverage(
                    family=family,
                    status=OrderFamilyCoverageStatus.INCOMPLETE,
                    evidence_id=pages[family].page_id,
                    broker_authoritative=True,
                    all_pages_consumed=True,
                    # reqAllOpenOrders includes working API orders regardless
                    # of submission date.  The missing dimension is terminal
                    # history before the current broker day.
                    includes_working_orders_across_dates=True,
                    includes_parent_child_conditional=(
                        family is OrderFamily.ADVANCED_EQUITY
                    ),
                )
                for family in OrderFamily
            ),
            client_ref_recovery_source=(
                ClientRefRecoverySource.CURRENT_DAY_ORDER_ROSTER
            ),
            broker_preserves_client_ref=True,
            negative_client_ref_results_authoritative=False,
        )


__all__ = ["IbkrStableAccountSnapshotReader"]
