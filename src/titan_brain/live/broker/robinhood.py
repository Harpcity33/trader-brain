"""Documented boundary for the currently connected Robinhood connector.

The Robinhood tools available to this project are model-mediated and use a
native attended confirmation flow.  They are not a credential or SDK that may
be imported by a background process.  This adapter therefore exposes supplied
read evidence only and rejects every review/mutation call.  Doing so prevents
deployment code from converting tool availability into unauthorized daemon
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerOperationResult,
    ClientRefLookupResult,
    EquityOrderType,
    MarketHours,
    OrderRequest,
    ReviewReceipt,
    TimeInForce,
)


@dataclass(frozen=True)
class RobinhoodConnectorContract:
    """Audited capabilities and non-negotiable connector semantics.

    These values describe the connector observed on 2026-09-07, not a promise
    of future authentication or service availability.  A deployment must
    re-probe reads and must never infer write authority from this record.
    """

    contract_version: str = "robinhood-attended-2026-09-07-v1"
    account_masked: str = "••••7153"
    connector_context: str = "codex_model_mediated_attended"
    observed_window_utc: tuple[str, str] = (
        "2026-09-07T22:51:09.819Z",
        "2026-09-07T22:51:12.953Z",
    )
    supported_equity_order_types: tuple[EquityOrderType, ...] = (
        EquityOrderType.MARKET,
        EquityOrderType.LIMIT,
        EquityOrderType.STOP_MARKET,
        EquityOrderType.STOP_LIMIT,
    )
    supported_market_hours: tuple[MarketHours, ...] = (
        MarketHours.REGULAR,
        MarketHours.EXTENDED,
        MarketHours.ALL_DAY,
    )
    supported_time_in_force: tuple[TimeInForce, ...] = (
        TimeInForce.GFD,
        TimeInForce.GTC,
    )
    confirmation_requirements: tuple[str, ...] = (
        "Every reviewed equity tuple must be shown with all order_checks and the verbatim market-data disclosure.",
        "Every place call requires explicit confirmation of the immediately preceding exact review, even when order_checks is empty.",
        "Every cancel requires its own explicit confirmation.",
        "Any tuple, evidence, session tag, or review-expiry change requires a new review and confirmation under the approved policy.",
    )
    session_constraints: tuple[str, ...] = (
        "extended_hours and all_day_hours accept limit orders only",
        "market, stop_market, and stop_limit orders are regular_hours only",
        "fractional or dollar-based equity orders are regular-hours market orders only",
        "Robinhood stop and stop-limit orders do not protect premarket fills before 09:30 America/New_York",
    )
    cancellation_semantics: tuple[str, ...] = (
        "cancel acceptance is asynchronous and is not proof of cancellation",
        "the order must be polled to a conclusive broker state",
        "a fill can win the race with a cancel request",
    )
    submission_semantics: tuple[str, ...] = (
        "submission acknowledgement is not fill evidence",
        "a UUID ref_id may be reused only for a retry known to be transient",
        "an unknown submission must not be retried without newer broker reconciliation",
        "the exposed read surface has no documented lookup by ref_id",
    )
    funds_semantics: tuple[str, ...] = (
        "unleveraged_buying_power is the no-margin sizing ceiling",
        "unsettled_funds is informational, may lag, and is not an order-gating balance",
        "the approved strategy forbids margin and debit regardless of account buying-power labels",
    )
    unresolved_equity_states: tuple[str, ...] = (
        "pending",
        "queued",
        "confirmed",
        "unconfirmed",
        "partially_filled",
        "pending_cancelled",
        "locating",
        "locate_failed",
        "unknown",
    )
    unsupported_operations: tuple[str, ...] = (
        "advanced-order read on the active connector surface",
        "atomic bracket/OCO/OTO protection",
        "equity order replacement",
        "trailing stop",
        "broker-bound review receipt/token/expiry",
        "cancel-review endpoint",
        "streaming order/fill feed",
        "daemon authentication/login/refresh/challenge handling",
        "order lookup by client ref_id",
        "documented connector rate-limit contract",
    )

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            connector="robinhood-connected-tool",
            account_masked=self.account_masked,
            supports_account_read=True,
            supports_equity_position_read=True,
            supports_equity_order_read=True,
            supports_option_position_read=True,
            supports_option_order_read=True,
            supports_advanced_order_read=False,
            supports_equity_review=True,
            supports_equity_place=True,
            supports_equity_cancel=True,
            daemon_transport_configured=False,
            supports_daemon_writes=False,
            supports_unattended_writes=False,
            supports_atomic_protection=False,
            supports_equity_replace=False,
            supports_streaming=False,
            supports_auth_refresh=False,
            supports_ref_id_lookup=False,
            review_requires_explicit_confirmation=True,
            cancel_requires_explicit_confirmation=True,
            cancel_is_asynchronous=True,
            supported_order_types=self.supported_equity_order_types,
            supported_market_hours=self.supported_market_hours,
            supported_time_in_force=self.supported_time_in_force,
            unsupported_operations=self.unsupported_operations,
            notes=(
                "Successful reads prove authentication only at their evidence timestamp.",
                "Connector tools remain attended/model-mediated and are not callable by the production daemon.",
                "The connector review response is not a broker-bound autonomous execution token.",
            ),
        )


class RobinhoodBrokerAdapter:
    """Read-evidence adapter with an unconditional daemon mutation interlock."""

    def __init__(
        self,
        *,
        contract: RobinhoodConnectorContract | None = None,
        snapshot_provider: Callable[[str], AccountSnapshot] | None = None,
    ) -> None:
        self.contract = contract or RobinhoodConnectorContract()
        self._snapshot_provider = snapshot_provider
        self.read_requests: list[str] = []
        self.blocked_mutations: list[tuple[str, tuple[object, ...]]] = []

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self.contract.capabilities()

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        self._assert_account(account_masked)
        self.read_requests.append(account_masked)
        if self._snapshot_provider is None:
            raise BrokerCapabilityError(
                "no daemon-readable Robinhood transport is configured; supply independently acquired point-in-time evidence"
            )
        snapshot = self._snapshot_provider(account_masked)
        if not isinstance(snapshot, AccountSnapshot):
            raise BrokerContractViolation("snapshot provider returned an unnormalized payload")
        if snapshot.account_masked != self.contract.account_masked:
            raise BrokerContractViolation("snapshot account does not match connector contract")
        return snapshot

    def lookup_equity_orders_by_client_ref(
        self, account_masked: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        self._assert_account(account_masked)
        raise BrokerCapabilityError(
            "the attended Robinhood connector exposes no authoritative client-ref lookup"
        )

    def review_equity_order(self, request: OrderRequest) -> ReviewReceipt:
        self._assert_account(request.account_masked)
        self.blocked_mutations.append(("review_equity_order", request.exact_tuple))
        raise BrokerMutationBlocked(
            "Robinhood review is available only through the attended model-mediated flow; the daemon cannot create or fabricate a review receipt",
            detail={"account_masked": request.account_masked, "client_ref_id": request.client_ref_id},
        )

    def place_equity_order(
        self,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self._assert_account(request.account_masked)
        self.blocked_mutations.append(("place_equity_order", request.exact_tuple))
        raise BrokerMutationBlocked(
            "unattended/daemon Robinhood place is not authorized or technically configured; use the native attended exact-review confirmation flow",
            detail={"account_masked": request.account_masked, "client_ref_id": request.client_ref_id},
        )

    def cancel_equity_order(
        self,
        account_masked: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self._assert_account(account_masked)
        normalized_order_id = str(broker_order_id).strip()
        self.blocked_mutations.append(
            ("cancel_equity_order", (account_masked, normalized_order_id))
        )
        raise BrokerMutationBlocked(
            "unattended/daemon Robinhood cancellation is not authorized or technically configured; cancellation requires its own attended confirmation and conclusive polling",
            detail={"account_masked": account_masked, "broker_order_id": normalized_order_id},
        )

    def _assert_account(self, account_masked: str) -> None:
        if account_masked != self.contract.account_masked:
            raise BrokerContractViolation("account does not match Robinhood connector binding")


__all__ = ["RobinhoodBrokerAdapter", "RobinhoodConnectorContract"]
