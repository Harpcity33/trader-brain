"""Staged IBKR execution transport; no connection, credentials or activation.

The SDK command boundary and durable intent ledger are concrete. Account-wide
collection and strategy preflight are explicit injected dependencies, not
fabricated from a socket handshake or a local ledger. Missing dependencies and
the default staged descriptor deny trading. No installed runtime selects this
transport merely because this module exists.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import re
from threading import RLock
from typing import Callable, Protocol, runtime_checkable
from uuid import uuid4

from .base import (
    AccountSnapshot, AttendedCancelReview, AttendedLocalReview, BrokerCapabilities,
    BrokerCapabilityError, BrokerContractViolation, BrokerMutationBlocked,
    BrokerOperationResult, BrokerUnknownSubmission,
    ClientRefLookupResult, ClientRefRecoverySource, EquityOrderType, MarketHours,
    LocalCancelDecision, LocalPreflightDecision,
    OperationStatus, OrderCoverageContract,
    OrderFamily, OrderFamilyCoverage, OrderFamilyCoverageStatus, OrderRequest,
    ReviewReceipt, TimeInForce,
)
from .ibkr_ledger import IbkrExecutionLedger, IntentRecord
from .ibkr_ledger_reconcile import IbkrLedgerReconciler
from .ibkr_risk_evidence import (
    IbkrRiskEvidenceAccountSnapshotReader,
    IbkrRiskEvidenceError,
)
from .ibkr_orders import (
    IbkrContractIdentity,
    attended_confirmation_phrase,
    attended_order_preview,
    build_ibkr_order_plan,
)
from .ibkr_sdk import IBKR_SDK_SEMANTIC_MEMBERS, IbkrSdkSession
from .ibkr_authority import IbkrDispatchAuthority, ibkr_intent_fingerprint
from .production import CollectedObservation, OrderFamilyPage, ProductionTransportDescriptor
from ..ibkr_autonomous_authority import (
    IbkrAutonomousAuthorityBindings,
    VerifiedIbkrAutonomousAuthority,
)


IBKR_TRANSPORT_ID = "ibkr-tws-api-10.50.2-v1"


def attended_ibkr_descriptor(
    *, exact_account_id: str, account_masked: str,
    account_binding_fingerprint: str, authorization_binding_id: str,
    coverage: OrderCoverageContract,
) -> ProductionTransportDescriptor:
    """Describe the regular-hours attended route after real read acceptance.

    Capability construction is deliberately separate from write activation.
    The descriptor permits a local direct API path, but marks every place and
    cancel as attended and denies unattended writes, atomic protection,
    replacement, extended-hours execution, and authoritative negative lookup.
    """

    if not isinstance(coverage, OrderCoverageContract):
        raise TypeError("IBKR order coverage contract required")
    if not coverage.proves_whole_account_order_coverage:
        raise BrokerCapabilityError("IBKR whole-account order coverage is incomplete")
    if coverage.negative_client_ref_results_authoritative:
        raise BrokerCapabilityError("IBKR cannot prove an absent API submission")
    capabilities = BrokerCapabilities(
        connector="ibkr-tws-api-attended",
        account_masked=account_masked,
        supports_account_read=True,
        supports_equity_position_read=True,
        supports_equity_order_read=True,
        supports_option_position_read=True,
        supports_option_order_read=True,
        supports_advanced_order_read=True,
        supports_equity_review=True,
        supports_equity_place=True,
        supports_equity_cancel=True,
        daemon_transport_configured=True,
        supports_daemon_writes=True,
        supports_unattended_writes=False,
        supports_atomic_protection=False,
        supports_equity_replace=False,
        supports_streaming=True,
        supports_auth_refresh=False,
        supports_ref_id_lookup=coverage.supports_exact_client_ref_recovery,
        review_requires_explicit_confirmation=True,
        cancel_requires_explicit_confirmation=True,
        cancel_is_asynchronous=True,
        supported_order_types=(
            EquityOrderType.MARKET,
            EquityOrderType.LIMIT,
            EquityOrderType.STOP_MARKET,
        ),
        supported_market_hours=(MarketHours.REGULAR,),
        supported_time_in_force=(TimeInForce.GFD, TimeInForce.GTC),
        order_coverage=coverage,
        unsupported_operations=(
            "unattended_writes",
            "extended_hours",
            "overnight",
            "global_cancel",
            "replace",
            "atomic_bracket_or_oca_protection",
        ),
        notes=(
            "Every place and cancel requires an exact unexpired attended review.",
            "SDK dispatch remains UNKNOWN until independent broker reconciliation.",
        ),
    )
    return ProductionTransportDescriptor(
        transport_id=IBKR_TRANSPORT_ID,
        exact_account_id=exact_account_id,
        account_binding_fingerprint=account_binding_fingerprint,
        authorization_binding_id=authorization_binding_id,
        capabilities=capabilities,
    )


def autonomous_ibkr_descriptor(
    *,
    exact_account_id: str,
    account_masked: str,
    account_binding_fingerprint: str,
    authorization_binding_id: str,
    coverage: OrderCoverageContract,
    authority: VerifiedIbkrAutonomousAuthority,
    authority_bindings: IbkrAutonomousAuthorityBindings,
    now: datetime,
) -> ProductionTransportDescriptor:
    """Describe autonomous writes only from a verified provider authority.

    The authenticated authority is not an activation record and does not
    authorize any particular order.  It establishes only that this exact
    release/account/API lane supports unattended regular-hours place/cancel
    without bypassing broker precautions.  Every order still needs current
    policy/provider evidence and the durable one-shot dispatch sequence.
    """

    if not isinstance(coverage, OrderCoverageContract):
        raise TypeError("IBKR order coverage contract required")
    if not isinstance(authority, VerifiedIbkrAutonomousAuthority) or not isinstance(
        authority_bindings, IbkrAutonomousAuthorityBindings
    ):
        raise BrokerCapabilityError(
            "IBKR autonomous writes require an authenticated authority artifact"
        )
    if not coverage.proves_whole_account_order_coverage:
        raise BrokerCapabilityError("IBKR whole-account order coverage is incomplete")
    if coverage.negative_client_ref_results_authoritative:
        raise BrokerCapabilityError("IBKR cannot prove an absent API submission")
    if any((
        authority_bindings.transport_id != IBKR_TRANSPORT_ID,
        authority_bindings.account_masked != account_masked,
        authority_bindings.account_binding_fingerprint
        != account_binding_fingerprint,
        authority_bindings.authorization_binding_id != authorization_binding_id,
    )):
        raise BrokerCapabilityError("IBKR autonomous descriptor binding mismatch")
    try:
        authority.assert_current(
            now,
            authority_bindings.release_manifest_hash,
            authority_bindings.config_hash,
            authority_bindings.policy_binding_id,
            authority_bindings.account_masked,
            authority_bindings.account_binding_fingerprint,
            authority_bindings.authorization_binding_id,
            authority_bindings.provider_contract_id,
            authority_bindings.transport_id,
            authority_bindings.environment,
            authority_bindings.client_id,
            expected_account_key=authority_bindings.account_key,
            expected_api_name=authority_bindings.api_name,
            expected_api_version=authority_bindings.api_version,
        )
    except Exception:
        raise BrokerCapabilityError(
            "IBKR autonomous authority is missing, expired, or changed"
        ) from None

    capabilities = BrokerCapabilities(
        connector="ibkr-tws-api-autonomous",
        account_masked=account_masked,
        supports_account_read=True,
        supports_equity_position_read=True,
        supports_equity_order_read=True,
        supports_option_position_read=True,
        supports_option_order_read=True,
        supports_advanced_order_read=True,
        supports_equity_review=True,
        supports_equity_place=True,
        supports_equity_cancel=True,
        daemon_transport_configured=True,
        supports_daemon_writes=True,
        supports_unattended_writes=True,
        supports_atomic_protection=False,
        supports_equity_replace=False,
        supports_streaming=True,
        supports_auth_refresh=False,
        supports_ref_id_lookup=coverage.supports_exact_client_ref_recovery,
        review_requires_explicit_confirmation=False,
        cancel_requires_explicit_confirmation=False,
        cancel_is_asynchronous=True,
        supported_order_types=(
            EquityOrderType.MARKET,
            EquityOrderType.LIMIT,
            EquityOrderType.STOP_MARKET,
        ),
        supported_market_hours=(MarketHours.REGULAR,),
        supported_time_in_force=(TimeInForce.GFD, TimeInForce.GTC),
        order_coverage=coverage,
        unsupported_operations=(
            "extended_hours",
            "overnight",
            "global_cancel",
            "replace",
            "atomic_bracket_or_oca_protection",
            "automatic_unknown_retry",
        ),
        notes=(
            "Autonomous place/cancel requires the current authenticated provider authority.",
            "Every mutation retains policy preflight, exact intent, reconciliation, and sequential verified protection.",
        ),
    )
    return ProductionTransportDescriptor(
        transport_id=IBKR_TRANSPORT_ID,
        exact_account_id=exact_account_id,
        account_binding_fingerprint=account_binding_fingerprint,
        authorization_binding_id=authorization_binding_id,
        capabilities=capabilities,
    )


def staged_ibkr_descriptor(
    *, exact_account_id: str, account_masked: str,
    account_binding_fingerprint: str, authorization_binding_id: str,
    observed_at: datetime,
) -> ProductionTransportDescriptor:
    """Describe an unaccepted integration honestly; this is never activation."""
    coverage = OrderCoverageContract(
        contract_version="ibkr-staged-unverified-v1",
        evidence_observed_at=observed_at,
        families=tuple(OrderFamilyCoverage(
            family=family, status=OrderFamilyCoverageStatus.UNKNOWN,
            evidence_id="ibkr-account-collection-not-accepted",
            broker_authoritative=False, all_pages_consumed=False,
            includes_working_orders_across_dates=False,
        ) for family in OrderFamily),
        client_ref_recovery_source=ClientRefRecoverySource.UNAVAILABLE,
        broker_preserves_client_ref=False,
        negative_client_ref_results_authoritative=False,
    )
    flags = {name: False for name in (
        "supports_account_read", "supports_equity_position_read",
        "supports_equity_order_read", "supports_option_position_read",
        "supports_option_order_read", "supports_advanced_order_read",
        "supports_equity_review", "supports_equity_place", "supports_equity_cancel",
        "daemon_transport_configured", "supports_daemon_writes",
        "supports_unattended_writes", "supports_atomic_protection",
        "supports_equity_replace", "supports_streaming", "supports_auth_refresh",
        "supports_ref_id_lookup", "review_requires_explicit_confirmation",
        "cancel_requires_explicit_confirmation",
    )}
    return ProductionTransportDescriptor(
        transport_id=IBKR_TRANSPORT_ID, exact_account_id=exact_account_id,
        account_binding_fingerprint=account_binding_fingerprint,
        authorization_binding_id=authorization_binding_id,
        capabilities=BrokerCapabilities(
            connector="ibkr-tws-api", account_masked=account_masked,
            **flags, cancel_is_asynchronous=True,
            supported_order_types=(), supported_market_hours=(),
            supported_time_in_force=(), order_coverage=coverage,
            unsupported_operations=("activation", "quotes", "global_cancel", "replace"),
            notes=("Staged source only; broker acceptance and runtime wiring absent.",),
        ),
    )


@runtime_checkable
class IbkrReadBridge(Protocol):
    """Whole-account normalized reads; a session order cache is insufficient."""

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]: ...
    def get_account_base(self, exact_account_id: str) -> CollectedObservation: ...
    def list_order_family_page(
        self, exact_account_id: str, family: OrderFamily, cursor: str | None,
    ) -> OrderFamilyPage: ...
    def lookup_equity_orders_by_client_ref(
        self, exact_account_id: str, client_refs: tuple[str, ...],
    ) -> ClientRefLookupResult: ...


@runtime_checkable
class IbkrPreflightBridge(Protocol):
    """Strategy/risk acceptance, separate from the SDK's dispatch authority.

    Implementations must revalidate current account, fees/commitments, session,
    contract eligibility, Massive evidence, and required protection. No default
    implementation silently asserts that these provider facts are established.
    """

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]: ...
    def review(
        self, request: OrderRequest
    ) -> AttendedLocalReview | LocalPreflightDecision: ...
    def contract_for(self, request: OrderRequest) -> IbkrContractIdentity: ...
    def revalidate(
        self,
        request: OrderRequest,
        review: AttendedLocalReview | LocalPreflightDecision,
    ) -> None: ...
    def authorize_cancel(self, client_ref_id: str, order_id: int) -> None: ...
    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview | LocalPreflightDecision: ...


@runtime_checkable
class IbkrAutonomousPreflightBridge(IbkrPreflightBridge, Protocol):
    """Additional one-shot decision required by the unattended cancel path."""

    def review_cancel(
        self,
        client_ref_id: str,
        order_id: int,
        broker_order_id: str,
    ) -> LocalCancelDecision: ...

    def finish_cancel(self, decision: LocalCancelDecision) -> None: ...


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BrokerContractViolation("IBKR clock must be timezone-aware")
    return value.astimezone(timezone.utc)


class IbkrProductionTransport:
    """One-shot local review -> durable intent -> guarded SDK dispatch.

    A successful SDK call means only a send attempt, never broker acceptance.
    The caller must reconcile callbacks through the independent read bridge.
    Broker order IDs are namespaced by originating nonzero API client ID.
    """

    def __init__(
        self, *, descriptor: ProductionTransportDescriptor,
        session: IbkrSdkSession, ledger: IbkrExecutionLedger,
        reads: IbkrReadBridge | None = None,
        preflight: IbkrPreflightBridge | None = None,
        contract_factory: Callable[[], object] | None = None,
        order_factory: Callable[[], object] | None = None,
        policy_binding_id: str | None = None,
        provider_contract_id: str | None = None,
        authority: IbkrDispatchAuthority | None = None,
        autonomous_authority: VerifiedIbkrAutonomousAuthority | None = None,
        autonomous_authority_bindings: IbkrAutonomousAuthorityBindings | None = None,
        account_snapshot_enricher: (
            Callable[[AccountSnapshot], AccountSnapshot] | None
        ) = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(descriptor, ProductionTransportDescriptor):
            raise TypeError("normalized IBKR descriptor required")
        if descriptor.transport_id != IBKR_TRANSPORT_ID:
            raise BrokerContractViolation("IBKR transport identity mismatch")
        if not isinstance(session, IbkrSdkSession) or not isinstance(ledger, IbkrExecutionLedger):
            raise TypeError("concrete guarded SDK session and durable IBKR ledger required")
        if descriptor.capabilities.supports_atomic_protection:
            raise BrokerCapabilityError("IBKR atomic partial-fill protection is not established")
        if descriptor.capabilities.supports_equity_replace:
            raise BrokerCapabilityError("IBKR replacement lifecycle is not implemented")
        caps = descriptor.capabilities
        if caps.supports_daemon_writes:
            attended = all((
                not caps.supports_unattended_writes,
                caps.review_requires_explicit_confirmation,
                caps.cancel_requires_explicit_confirmation,
            ))
            autonomous = all((
                caps.supports_unattended_writes,
                not caps.review_requires_explicit_confirmation,
                not caps.cancel_requires_explicit_confirmation,
                isinstance(autonomous_authority, VerifiedIbkrAutonomousAuthority),
                isinstance(
                    autonomous_authority_bindings,
                    IbkrAutonomousAuthorityBindings,
                ),
            ))
            if not (attended or autonomous) or caps.supported_market_hours != (
                MarketHours.REGULAR,
            ):
                raise BrokerCapabilityError(
                    "IBKR direct-write authority contract is incomplete"
                )
        elif (
            autonomous_authority is not None
            or autonomous_authority_bindings is not None
        ):
            raise BrokerCapabilityError(
                "IBKR autonomous authority cannot bind a read-only transport"
            )
        if not caps.supports_unattended_writes and (
            autonomous_authority is not None
            or autonomous_authority_bindings is not None
        ):
            raise BrokerCapabilityError(
                "IBKR attended transport cannot retain autonomous authority"
            )
        if descriptor.capabilities.order_coverage.negative_client_ref_results_authoritative:
            raise BrokerCapabilityError("IBKR session history cannot prove an absent submission")
        if reads is not None and not isinstance(reads, IbkrReadBridge):
            raise TypeError("IBKR read bridge contract required")
        if preflight is not None and not isinstance(preflight, IbkrPreflightBridge):
            raise TypeError("IBKR preflight bridge contract required")
        if caps.supports_unattended_writes and (
            preflight is None
            or not isinstance(preflight, IbkrAutonomousPreflightBridge)
        ):
            raise TypeError("IBKR autonomous preflight bridge contract required")
        if caps.supports_unattended_writes:
            attended_delegate = getattr(preflight, "_attended", None)
            snapshot_reader = getattr(attended_delegate, "_snapshot_reader", None)
            if (
                type(snapshot_reader) is not IbkrRiskEvidenceAccountSnapshotReader
                or not callable(account_snapshot_enricher)
                or getattr(account_snapshot_enricher, "__self__", None)
                is not snapshot_reader
                or getattr(account_snapshot_enricher, "__func__", None)
                is not type(snapshot_reader).enrich
            ):
                raise TypeError(
                    "IBKR autonomous transport requires the exact preflight-owned "
                    "risk-evidence enricher"
                )
        elif account_snapshot_enricher is not None:
            raise BrokerCapabilityError(
                "IBKR attended transport cannot retain autonomous risk enrichment"
            )
        self._descriptor = descriptor
        self.session = session
        self.ledger = ledger
        self.ledger_reconciler = IbkrLedgerReconciler(
            ledger,
            account_masked=descriptor.capabilities.account_masked,
        )
        self.reads = reads
        self.preflight = preflight
        self.contract_factory = contract_factory
        self.order_factory = order_factory
        self.policy_binding_id = policy_binding_id
        self.provider_contract_id = provider_contract_id
        self.authority = authority
        self.autonomous_authority = autonomous_authority
        self.autonomous_authority_bindings = autonomous_authority_bindings
        self._account_snapshot_enricher = account_snapshot_enricher
        self._clock = clock or session._clock
        if authority is not None:
            if (
                not isinstance(authority, IbkrDispatchAuthority)
                or authority.ledger is not ledger
                or authority.authorization_binding_id != descriptor.authorization_binding_id
                or authority.provider_contract_id != provider_contract_id
                or authority.policy_binding_id != policy_binding_id
                or authority._clock is not self._clock
                or session._clock is not self._clock
            ):
                raise BrokerContractViolation("IBKR dispatch authority binding mismatch")
            session.assert_dispatch_authorizer(authority)
        self._session_id = str(uuid4())
        self._lock = RLock()
        self._reviews: dict[str, AttendedLocalReview | LocalPreflightDecision] = {}
        self._cancel_reviews: dict[str, AttendedCancelReview] = {}
        self._assert_bindings()
        if caps.supports_unattended_writes:
            self._assert_autonomous_authority("transport_construction")

    @property
    def descriptor(self) -> ProductionTransportDescriptor:
        return self._descriptor

    def bind_entry_risk_activation(
        self,
        *,
        lineage_hash: str,
        minimum_peak: object,
    ) -> None:
        """Bind consumed activation evidence into the actual entry preflight."""

        if not self.descriptor.capabilities.supports_unattended_writes:
            raise BrokerMutationBlocked(
                "IBKR_ENTRY_ACTIVATION_BINDING_REQUIRES_AUTONOMOUS_TRANSPORT"
            )
        binder = getattr(self.preflight, "bind_entry_risk_activation", None)
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
        """Inventory the complete retained graph, or refuse release binding."""

        caps = self.descriptor.capabilities
        attended = all((
            not caps.supports_unattended_writes,
            caps.review_requires_explicit_confirmation,
            caps.cancel_requires_explicit_confirmation,
        ))
        autonomous = all((
            caps.supports_unattended_writes,
            not caps.review_requires_explicit_confirmation,
            not caps.cancel_requires_explicit_confirmation,
            isinstance(self.autonomous_authority, VerifiedIbkrAutonomousAuthority),
            isinstance(
                self.autonomous_authority_bindings,
                IbkrAutonomousAuthorityBindings,
            ),
        ))
        complete = all((
            caps.can_prove_whole_broker_reconciliation,
            caps.supports_ref_id_lookup,
            caps.supports_equity_review,
            caps.supports_equity_place,
            caps.supports_equity_cancel,
            caps.daemon_transport_configured,
            caps.supports_daemon_writes,
            attended or autonomous,
            caps.supported_market_hours == (MarketHours.REGULAR,),
            self.reads is not None,
            self.preflight is not None,
            self.contract_factory is not None,
            self.order_factory is not None,
            self.authority is not None,
            bool(self.policy_binding_id),
            bool(self.provider_contract_id),
        ))
        if not complete:
            raise BrokerCapabilityError(
                "IBKR deployment blocked: release dependency inventory is incomplete"
            )
        if autonomous:
            self._assert_autonomous_authority("release_inventory")
        try:
            session_inventory = tuple(self.session.release_components())
            authority_inventory = tuple(self.authority.release_components())
        except Exception:
            raise BrokerCapabilityError(
                "IBKR deployment blocked: nested dependency inventory is incomplete"
            ) from None
        expected_session_roles = {
            "ibkr_sdk_client",
            "ibkr_sdk_cancel_factory",
            "ibkr_sdk_mutation_interlock",
            "ibkr_sdk_dispatch_authorizer",
            "ibkr_sdk_clock",
        }
        expected_authority_roles = {
            "ibkr_execution_ledger",
            "ibkr_acceptance_verifier",
            "ibkr_revalidation_callback",
            "ibkr_cancel_authorizer",
        }
        inventory_shape_invalid = any(
                not isinstance(item, tuple)
                or len(item) != 3
                or not isinstance(item[0], str)
                or item[1] is None
                or not isinstance(item[2], tuple)
                or not item[2]
                for item in (*session_inventory, *authority_inventory)
        )
        if inventory_shape_invalid:
            raise BrokerCapabilityError(
                "IBKR deployment blocked: nested dependency inventory is incomplete"
            )
        if (
            {item[0] for item in session_inventory} != expected_session_roles
            or {item[0] for item in authority_inventory} != expected_authority_roles
            or next(item[1] for item in session_inventory
                    if item[0] == "ibkr_sdk_dispatch_authorizer") is not self.authority
            or next(item[1] for item in authority_inventory
                    if item[0] == "ibkr_execution_ledger") is not self.ledger
        ):
            raise BrokerCapabilityError(
                "IBKR deployment blocked: nested dependency inventory is incomplete"
            )
        inventory = (
            ("ibkr_sdk_session", self.session, IBKR_SDK_SEMANTIC_MEMBERS),
            ("ibkr_read_bridge", self.reads, (
                "release_components",
                "get_account_base", "list_order_family_page",
                "lookup_equity_orders_by_client_ref",
            )),
            ("ibkr_preflight_bridge", self.preflight, (
                "release_components", "review", "contract_for", "revalidate",
                "authorize_cancel", "review_protection",
                *((
                    "review_cancel",
                    "finish_cancel",
                    "bind_entry_risk_activation",
                ) if autonomous else ()),
            )),
            ("ibkr_contract_factory", self.contract_factory, ("__call__",)),
            ("ibkr_order_factory", self.order_factory, ("__call__",)),
            ("ibkr_ledger_reconciler", self.ledger_reconciler, (
                "release_components", "reconcile_orders",
            )),
        )
        if autonomous:
            inventory += ((
                "ibkr_autonomous_authority_contract",
                self.autonomous_authority,
                ("assert_current",),
            ), (
                "ibkr_autonomous_authority_bindings",
                self.autonomous_authority_bindings,
                ("release_manifest_hash", "config_hash", "policy_binding_id"),
            ))
        return inventory

    def _assert_bindings(self) -> None:
        expected = self.descriptor.account_binding_fingerprint
        if (
            self.session.account_binding_fingerprint != expected
            or self.ledger.account_fingerprint != expected
            or self.ledger.environment != self.session.environment
            or self.ledger.client_id != self.session.client_id
        ):
            raise BrokerContractViolation("IBKR account/environment/client binding mismatch")
        self.session.assert_account(self.descriptor.exact_account_id)

    def _assert_autonomous_authority(self, operation: str) -> None:
        """Recheck the sealed provider contract at each mutation boundary."""

        if not isinstance(operation, str) or not operation:
            raise BrokerMutationBlocked("IBKR autonomous operation identity is missing")
        if not self.descriptor.capabilities.supports_unattended_writes:
            return
        authority = self.autonomous_authority
        bindings = self.autonomous_authority_bindings
        if not isinstance(authority, VerifiedIbkrAutonomousAuthority) or not isinstance(
            bindings, IbkrAutonomousAuthorityBindings
        ):
            raise BrokerMutationBlocked("IBKR autonomous authority is unavailable")
        if any((
            bindings.transport_id != self.descriptor.transport_id,
            bindings.account_masked != self.descriptor.capabilities.account_masked,
            bindings.account_binding_fingerprint
            != self.descriptor.account_binding_fingerprint,
            bindings.authorization_binding_id
            != self.descriptor.authorization_binding_id,
            bindings.provider_contract_id != self.provider_contract_id,
            bindings.policy_binding_id != self.policy_binding_id,
            bindings.environment != self.session.environment,
            bindings.client_id != self.session.client_id,
        )):
            raise BrokerMutationBlocked("IBKR autonomous authority binding changed")
        try:
            authority.assert_current(
                _utc(self._clock()),
                bindings.release_manifest_hash,
                bindings.config_hash,
                bindings.policy_binding_id,
                bindings.account_masked,
                bindings.account_binding_fingerprint,
                bindings.authorization_binding_id,
                bindings.provider_contract_id,
                bindings.transport_id,
                bindings.environment,
                bindings.client_id,
                expected_account_key=bindings.account_key,
                expected_api_name=bindings.api_name,
                expected_api_version=bindings.api_version,
            )
        except Exception:
            raise BrokerMutationBlocked(
                f"IBKR autonomous authority is not current for {operation}"
            ) from None

    def _account(self, exact_account_id: str) -> None:
        if exact_account_id != self.descriptor.exact_account_id:
            raise BrokerContractViolation("IBKR private account binding mismatch")
        self._assert_bindings()

    def _request(self, request: OrderRequest) -> None:
        if not isinstance(request, OrderRequest):
            raise BrokerContractViolation("normalized IBKR order request required")
        caps = self.descriptor.capabilities
        if request.market_hours is not MarketHours.REGULAR:
            raise BrokerCapabilityError("IBKR execution is restricted to regular hours")
        if request.account_masked != caps.account_masked:
            raise BrokerContractViolation("IBKR request account mismatch")
        if (
            request.order_type not in caps.supported_order_types
            or request.market_hours not in caps.supported_market_hours
            or request.time_in_force not in caps.supported_time_in_force
        ):
            raise BrokerCapabilityError("IBKR requested order tuple is not accepted")

    def _read_bridge(self) -> IbkrReadBridge:
        if self.reads is None:
            raise BrokerCapabilityError("IBKR whole-account read integration is unavailable")
        return self.reads

    def get_account_base(self, exact_account_id: str) -> CollectedObservation:
        self._account(exact_account_id)
        evidence = self._read_bridge().get_account_base(exact_account_id)
        if not isinstance(evidence, CollectedObservation):
            raise BrokerContractViolation("IBKR requires non-atomic collection provenance")
        if evidence.snapshot.account_masked != self.descriptor.capabilities.account_masked:
            raise BrokerContractViolation("IBKR collection account mismatch")
        if self.descriptor.capabilities.supports_unattended_writes:
            # IBKR's AccountType summary is a broker classification, not the
            # approved risk-policy semantic.  Only the authenticated autonomous
            # authority contract can attest that this exact bound account has
            # no borrowing capacity.  Keep raw reads available after authority
            # expiry for reconciliation, while every mutation boundary still
            # rechecks current authority independently.
            authority = self.autonomous_authority
            bindings = self.autonomous_authority_bindings
            if (
                not isinstance(authority, VerifiedIbkrAutonomousAuthority)
                or not isinstance(bindings, IbkrAutonomousAuthorityBindings)
                or authority.account_masked
                != evidence.snapshot.account_masked
                or authority.account_binding_fingerprint
                != self.descriptor.account_binding_fingerprint
                or bindings.account_masked != evidence.snapshot.account_masked
                or bindings.account_binding_fingerprint
                != self.descriptor.account_binding_fingerprint
                or not evidence.snapshot.account_type.strip()
            ):
                raise BrokerContractViolation(
                    "IBKR no-borrow account-control evidence is unavailable"
                )

            raw = evidence.snapshot
            try:
                enriched = self._account_snapshot_enricher(raw)  # type: ignore[misc]
            except IbkrRiskEvidenceError:
                # Baseline authentication and the local peak-equity ledger are
                # entry-risk authority, not account visibility.  Preserve the
                # fresh TWS snapshot for reconciliation and reduce-only work,
                # while making it impossible for upstream or cached weekly /
                # peak values to authorize a new position.
                normalized = replace(
                    raw,
                    weekly_realized_pnl=None,
                    peak_equity=None,
                    weekly_realized_pnl_complete=False,
                    peak_equity_complete=False,
                    risk_baseline_identity_hash=None,
                    risk_baseline_receipt_hash=None,
                    risk_high_water_identity_hash=None,
                    risk_high_water_lineage_hash=None,
                    risk_high_water_receipt_hash=None,
                )
            else:
                if (
                    not isinstance(enriched, AccountSnapshot)
                    or enriched.account_masked
                    != self.descriptor.capabilities.account_masked
                    or not enriched.authenticated_entry_risk_evidence_ready
                ):
                    raise BrokerContractViolation(
                        "IBKR authenticated risk evidence is incomplete"
                    )
                normalized = enriched
            evidence = replace(
                evidence,
                snapshot=replace(
                    normalized,
                    account_type="no_borrow_margin",
                ),
            )
        return evidence

    def list_order_family_page(
        self, exact_account_id: str, family: OrderFamily, cursor: str | None,
    ) -> OrderFamilyPage:
        self._account(exact_account_id)
        page = self._read_bridge().list_order_family_page(exact_account_id, family, cursor)
        if not isinstance(page, OrderFamilyPage) or page.snapshot_token is not None:
            raise BrokerContractViolation("IBKR family page requires collection provenance")
        if page.account_masked != self.descriptor.capabilities.account_masked or page.family != family:
            raise BrokerContractViolation("IBKR family page account/family mismatch")
        self.ledger_reconciler.reconcile_orders(page.orders)
        return page

    def lookup_equity_orders_by_client_ref(
        self, exact_account_id: str, client_refs: tuple[str, ...],
    ) -> ClientRefLookupResult:
        self._account(exact_account_id)
        result = self._read_bridge().lookup_equity_orders_by_client_ref(exact_account_id, client_refs)
        if (
            not isinstance(result, ClientRefLookupResult)
            or result.account_masked != self.descriptor.capabilities.account_masked
            or result.requested_client_refs != client_refs
            or result.confirmed_absent_client_refs
        ):
            raise BrokerContractViolation("IBKR reference lookup cannot invent authoritative absence")
        self.ledger_reconciler.reconcile_orders(result.found_orders)
        return result

    def _preflight(self) -> IbkrPreflightBridge:
        if self.preflight is None or not self.policy_binding_id or not self.provider_contract_id:
            raise BrokerMutationBlocked("IBKR provider/risk preflight is not accepted")
        return self.preflight

    def _authority(self) -> IbkrDispatchAuthority:
        if self.authority is None:
            raise BrokerMutationBlocked("IBKR exact-intent dispatch authority is unavailable")
        if (
            self.authority.ledger is not self.ledger
            or self.authority.authorization_binding_id != self.descriptor.authorization_binding_id
            or self.authority.provider_contract_id != self.provider_contract_id
            or self.authority.policy_binding_id != self.policy_binding_id
            or self.authority._clock is not self._clock
            or self.session._clock is not self._clock
        ):
            raise BrokerMutationBlocked("IBKR dispatch authority binding changed")
        self.session.assert_dispatch_authorizer(self.authority)
        return self.authority

    @staticmethod
    def _approved(result: object) -> None:
        if result is not None:
            raise BrokerMutationBlocked("IBKR preflight must raise on denial, not return a boolean")

    def _check_review(
        self, request: OrderRequest, review: ReviewReceipt
    ) -> AttendedLocalReview | LocalPreflightDecision:
        now = _utc(self._clock())
        expected_preview = attended_order_preview(request)
        caps = self.descriptor.capabilities
        if not isinstance(review, (AttendedLocalReview, LocalPreflightDecision)):
            common_invalid = True
        else:
            common_invalid = any((
                review.request.exact_tuple != request.exact_tuple,
                review.reviewed_at > now,
                review.received_at > now,
                review.expires_at is None,
                review.expired_at(now),
                review.policy_binding_id != self.policy_binding_id,
                review.provider_contract_id != self.provider_contract_id,
                not re.fullmatch(r"[0-9a-f]{64}", review.evidence_collection_id),
                any(check.severity.upper() not in {"INFO"} for check in review.order_checks),
                not review.disclosure.strip(),
                any(
                    review.preview.get(key) != value
                    for key, value in expected_preview.items()
                ),
            ))
        attended_invalid = bool(
            caps.review_requires_explicit_confirmation
            and (
                not isinstance(review, AttendedLocalReview)
                or review.required_confirmation_phrase
                != attended_confirmation_phrase(request)
            )
        )
        autonomous_invalid = bool(
            not caps.review_requires_explicit_confirmation
            and (
                not caps.supports_unattended_writes
                or not isinstance(review, LocalPreflightDecision)
                or review.required_confirmation_phrase is not None
            )
        )
        if common_invalid or attended_invalid or autonomous_invalid:
            raise BrokerMutationBlocked(
                "IBKR review is missing, expired, not exact, or has wrong authority mode"
            )
        return review

    def review_equity_order(
        self, exact_account_id: str, request: OrderRequest
    ) -> AttendedLocalReview | LocalPreflightDecision:
        self._account(exact_account_id)
        self._request(request)
        with self._lock:
            if not self.descriptor.capabilities.supports_equity_review:
                raise BrokerMutationBlocked("IBKR review capability is not accepted")
            self._assert_autonomous_authority("entry_review")
            review = self._check_review(request, self._preflight().review(request))
            self._assert_autonomous_authority("entry_review")
            # Reserve the exact client/order identity while the generic
            # lifecycle intent is still PREPARED.  The lifecycle changes to
            # SUBMITTING only after this review returns, so a crash in that
            # boundary can always be recovered by account/client/orderRef/
            # orderId instead of leaving a state-only UNKNOWN with no IBKR
            # correlation.  This is not a send claim and grants no retry: the
            # ledger's separate mark_sending transition remains the one-shot
            # mutation boundary.
            self._approved(self._preflight().revalidate(request, review))
            intent, _contract = self._prepare_place_intent(request)
            retire_reserved = intent.can_transmit
            try:
                if review.decision_id in self._reviews:
                    raise BrokerContractViolation("IBKR preflight identity was reused")
                # Bound local storage; do not evict a valid receipt silently.
                self._reviews = {key: value for key, value in self._reviews.items()
                                 if not value.expired_at(_utc(self._clock()))}
                if len(self._reviews) >= 256:
                    raise BrokerMutationBlocked("IBKR outstanding preflight capacity reached")
                self._reviews[review.decision_id] = review
            except Exception:
                if retire_reserved:
                    self._finish_known_no_wire_place(
                        request.client_ref_id,
                        "review_equity_order",
                    )
                raise
            return review

    def review_protection_equity_order(
        self,
        exact_account_id: str,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview | LocalPreflightDecision:
        """Issue a local stop review only after broker-current fill recovery."""

        self._account(exact_account_id)
        self._request(source_request)
        self._request(stop_template)
        with self._lock:
            if not self.descriptor.capabilities.supports_equity_review:
                raise BrokerMutationBlocked("IBKR review capability is not accepted")
            self._assert_autonomous_authority("protection_review")
            review = self._preflight().review_protection(
                source_request,
                source_plan_id,
                stop_template,
                source_claimed_at,
            )
            checked = self._check_review(review.request, review)
            self._assert_autonomous_authority("protection_review")
            self._approved(self._preflight().revalidate(checked.request, checked))
            intent, _contract = self._prepare_place_intent(checked.request)
            retire_reserved = intent.can_transmit
            try:
                if checked.decision_id in self._reviews:
                    raise BrokerContractViolation("IBKR preflight identity was reused")
                self._reviews = {
                    key: value
                    for key, value in self._reviews.items()
                    if not value.expired_at(_utc(self._clock()))
                }
                if len(self._reviews) >= 256:
                    raise BrokerMutationBlocked("IBKR outstanding preflight capacity reached")
                self._reviews[checked.decision_id] = checked
            except Exception:
                if retire_reserved:
                    self._finish_known_no_wire_place(
                        checked.request.client_ref_id,
                        "review_protection_equity_order",
                    )
                raise
            return checked

    def place_equity_order(
        self, exact_account_id: str, request: OrderRequest, *, review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        with self._lock:
            checked: AttendedLocalReview | LocalPreflightDecision | None = None
            authority: IbkrDispatchAuthority | None = None
            intent: IntentRecord | None = None
            locally_issued = bool(
                isinstance(review, (AttendedLocalReview, LocalPreflightDecision))
                and self._reviews.get(review.decision_id) == review
            )
            cleanup_ref = (
                request.client_ref_id
                if isinstance(request, OrderRequest)
                else review.request.client_ref_id
                if locally_issued
                else None
            )
            lookup_failed = False
            try:
                existing = (
                    self.ledger.lookup(request.client_ref_id)
                    if isinstance(request, OrderRequest)
                    else None
                )
            except Exception:
                lookup_failed = True
                existing = None
            retire_reserved = bool(
                (existing is not None and existing.can_transmit)
                or (locally_issued and lookup_failed)
            )
            try:
                self._account(exact_account_id)
                self._request(request)
                caps = self.descriptor.capabilities
                attended = all((
                    not caps.supports_unattended_writes,
                    caps.review_requires_explicit_confirmation,
                    caps.cancel_requires_explicit_confirmation,
                ))
                autonomous = all((
                    caps.supports_unattended_writes,
                    not caps.review_requires_explicit_confirmation,
                    not caps.cancel_requires_explicit_confirmation,
                    self.autonomous_authority is not None,
                ))
                if not all((
                    caps.supports_equity_place,
                    caps.supports_daemon_writes,
                    attended or autonomous,
                )):
                    raise BrokerMutationBlocked("IBKR write capability is not accepted")
                self._assert_autonomous_authority("place_preflight")
                checked = self._check_review(request, review)
                if attended and explicit_confirmation != checked.required_confirmation_phrase:
                    raise BrokerMutationBlocked("IBKR place requires the exact attended confirmation")
                if autonomous and explicit_confirmation is not None:
                    raise BrokerMutationBlocked(
                        "IBKR autonomous place cannot consume ambient confirmation text"
                    )
                if self._reviews.pop(checked.decision_id, None) != checked:
                    raise BrokerMutationBlocked("IBKR requires an unused locally issued review")
                bridge = self._preflight()
                authority = self._authority()
                self._approved(bridge.revalidate(request, checked))
                intent, contract = self._prepare_place_intent(request)
                retire_reserved = retire_reserved or intent.can_transmit
                if not intent.can_transmit:
                    raise BrokerMutationBlocked(
                        "IBKR intent has prior transmission or broker evidence; "
                        "reconcile without resubmission"
                    )
                if self.contract_factory is None or self.order_factory is None:
                    raise BrokerMutationBlocked(
                        "IBKR SDK value constructors are unavailable"
                    )
                plan = build_ibkr_order_plan(
                    request, contract=contract, account_id=exact_account_id,
                    client_id=self.session.client_id, order_id=intent.order_id,
                    transmit=True,
                )
                sdk_contract, sdk_order = plan.to_sdk(
                    contract_factory=self.contract_factory,
                    order_factory=self.order_factory,
                )
                self._approved(bridge.revalidate(request, checked))
                self._check_review(request, checked)
                self.session.assert_write_binding(
                    self.descriptor.authorization_binding_id,
                    self.provider_contract_id,
                )
                authority.register_submission(
                    plan=plan, review=checked, generation=self.session.generation,
                    sdk_contract=sdk_contract, sdk_order=sdk_order,
                )
            except Exception:
                if authority is not None and intent is not None:
                    authority.finish(intent.order_id)
                if checked is not None:
                    self._reviews.pop(checked.decision_id, None)
                elif locally_issued:
                    self._reviews.pop(review.decision_id, None)
                if retire_reserved and cleanup_ref is not None:
                    self._finish_known_no_wire_place(
                        cleanup_ref,
                        "place_equity_order_pre_dispatch",
                    )
                raise
            if authority is None or intent is None:
                raise BrokerUnknownSubmission(
                    "IBKR pre-wire placement state is incomplete; reconcile possible exposure",
                    detail={"operation": "place_equity_order"},
                )
            try:
                # Crash after this commit is always a possibly-sent intent.
                try:
                    self.ledger.mark_sending(request.client_ref_id)
                except Exception:
                    self._finish_known_no_wire_place(
                        request.client_ref_id,
                        "place_equity_order_sending_claim",
                    )
                    raise
                try:
                    self._assert_autonomous_authority("place_dispatch")
                    self.session.submit(intent.order_id, sdk_contract, sdk_order)
                except BrokerMutationBlocked:
                    # The SDK boundary guarantees that this class means no
                    # socket handoff was attempted.  Commit a terminal local
                    # no-wire fact only if the ledger still contains no
                    # contradictory broker evidence.  A racing ACK/fill/other
                    # broker lifecycle fact is possible exposure, not a local
                    # mutation denial.
                    self._finish_known_no_wire_place(
                        request.client_ref_id,
                        "place_equity_order_known_no_wire_persistence",
                    )
                    raise
                except Exception:
                    self._persist_unknown(request.client_ref_id, "UNKNOWN", "place_equity_order")
                    raise BrokerUnknownSubmission(
                        "IBKR dispatch outcome is unresolved; reconcile the existing intent",
                        detail={"operation": "place_equity_order"},
                    ) from None
                self._persist_unknown(request.client_ref_id, "UNKNOWN", "place_equity_order")
                return self._unknown("place_equity_order")
            finally:
                authority.finish(intent.order_id)

    def _prepare_place_intent(
        self, request: OrderRequest
    ) -> tuple[IntentRecord, IbkrContractIdentity]:
        """Durably bind one exact unsent IBKR identity, idempotently.

        Allocation is deliberately earlier than the SDK dispatch claim.  A
        RESERVED record proves only a locally prepared identity; SENDING or
        any broker event permanently removes transmit authority.  Reopening a
        review for the same immutable request may recover the RESERVED record,
        but a changed tuple or any possibly-sent state is rejected by the
        ledger/place boundary.
        """

        bridge = self._preflight()
        contract = bridge.contract_for(request)
        if not isinstance(contract, IbkrContractIdentity):
            raise BrokerMutationBlocked("IBKR contract identity is not normalized")
        next_id = self.session.next_valid_id
        if next_id is None:
            raise BrokerMutationBlocked("IBKR next valid order ID has not arrived")
        fingerprint = ibkr_intent_fingerprint(
            request,
            contract,
            environment=self.session.environment,
            account_binding_fingerprint=self.descriptor.account_binding_fingerprint,
            client_id=self.session.client_id,
        )
        intent = self.ledger.allocate_intent(
            client_ref_id=request.client_ref_id,
            request_fingerprint=fingerprint,
            session_id=self._session_id,
            broker_next_valid_id=next_id,
        )
        return intent, contract

    @staticmethod
    def _has_contradictory_submission_evidence(intent: IntentRecord) -> bool:
        """Return whether a local no-wire conclusion is no longer sufficient."""

        return any((
            intent.acknowledgement_seen,
            intent.rejection_seen,
            intent.submission_unknown_seen,
            intent.cancel_started,
            intent.pending_cancel_seen,
            intent.cancellation_unknown_seen,
            intent.cancelled_seen,
            intent.fill_count > 0,
        ))

    def _finish_known_no_wire_place(
        self,
        client_ref_id: str,
        operation: str,
    ) -> None:
        """Terminally consume a place identity after a proven pre-wire stop.

        RESERVED is retired as ABORTED; the bare SENDING claim is resolved as
        SUBMIT_NOT_SENT.  If either transition races with broker evidence or
        cannot be made durable, append UNKNOWN when possible and surface
        possible exposure.  Thus a cleanup failure can never restore or leave
        reusable transmission authority.
        """

        try:
            current = self.ledger.lookup(client_ref_id)
        except Exception:
            raise BrokerUnknownSubmission(
                "IBKR known-no-wire intent state is unreadable; reconcile possible exposure",
                detail={"operation": operation},
            ) from None
        if current is None:
            raise BrokerUnknownSubmission(
                "IBKR known-no-wire intent is missing; reconcile possible exposure",
                detail={"operation": operation},
            )

        if self._has_contradictory_submission_evidence(current):
            self._persist_unknown(client_ref_id, "UNKNOWN", operation)
            raise BrokerUnknownSubmission(
                "IBKR broker evidence contradicts a local no-wire result; reconcile possible exposure",
                detail={"operation": operation},
            ) from None
        if current.status == "ABORTED":
            return

        try:
            if current.can_transmit:
                terminal = self.ledger.abort_reserved_intent(client_ref_id)
            elif current.status == "SENDING" and current.send_started:
                terminal = self.ledger.record_submit_not_sent(client_ref_id)
            else:
                self._persist_unknown(client_ref_id, "UNKNOWN", operation)
                raise BrokerUnknownSubmission(
                    "IBKR known-no-wire intent is not locally terminal; reconcile possible exposure",
                    detail={"operation": operation},
                ) from None
        except BrokerUnknownSubmission:
            raise
        except Exception:
            self._persist_unknown(client_ref_id, "UNKNOWN", operation)
            raise BrokerUnknownSubmission(
                "IBKR known-no-wire result could not be persisted; reconcile possible exposure",
                detail={"operation": operation},
            ) from None

        # Re-read after the terminal write so a broker callback which won the
        # cleanup race is surfaced as possible exposure rather than returning
        # the original known-no-accept exception.
        try:
            final = self.ledger.lookup(client_ref_id)
        except Exception:
            raise BrokerUnknownSubmission(
                "IBKR known-no-wire terminal state is unreadable; reconcile possible exposure",
                detail={"operation": operation},
            ) from None
        if (
            final is None
            or final.can_transmit
            or final.status != "ABORTED"
            or self._has_contradictory_submission_evidence(final)
        ):
            self._persist_unknown(client_ref_id, "UNKNOWN", operation)
            raise BrokerUnknownSubmission(
                "IBKR broker evidence raced with no-wire cleanup; reconcile possible exposure",
                detail={"operation": operation},
            ) from None

    def review_cancel_equity_order(
        self, exact_account_id: str, broker_order_id: str,
    ) -> AttendedCancelReview:
        """Prepare one short-lived exact cancellation review; never dispatch."""

        self._account(exact_account_id)
        with self._lock:
            caps = self.descriptor.capabilities
            if not all((
                caps.supports_equity_cancel,
                caps.supports_daemon_writes,
                caps.cancel_requires_explicit_confirmation,
            )):
                raise BrokerMutationBlocked("IBKR attended cancel capability is not accepted")
            order_id = self.parse_broker_order_id(broker_order_id)
            intent = self.ledger.lookup_order_id(order_id)
            if (
                intent is None
                or intent.status != "ACK"
                or not intent.can_cancel
            ):
                raise BrokerMutationBlocked(
                    "IBKR cancellation review requires a reconciled cancellable owned intent"
                )
            self._approved(self._preflight().authorize_cancel(intent.client_ref_id, order_id))
            now = _utc(self._clock())
            phrase = self.cancel_confirmation_phrase(broker_order_id)
            review = AttendedCancelReview(
                decision_id=str(uuid4()),
                account_masked=caps.account_masked,
                broker_order_id=broker_order_id,
                client_ref_id=intent.client_ref_id,
                reviewed_at=now,
                received_at=now,
                expires_at=now + timedelta(seconds=15),
                required_confirmation_phrase=phrase,
                disclosure=(
                    "Local IBKR cancellation preview; cancellation is asynchronous "
                    "and exposure remains until newer broker evidence confirms it."
                ),
                order_checks=(),
                preview={
                    "account_masked": caps.account_masked,
                    "action": "cancel",
                    "broker_order_id": broker_order_id,
                    "client_ref_id": intent.client_ref_id,
                },
            )
            self._cancel_reviews = {
                target: existing
                for target, existing in self._cancel_reviews.items()
                if not existing.expired_at(now)
            }
            if len(self._cancel_reviews) >= 256 and broker_order_id not in self._cancel_reviews:
                raise BrokerMutationBlocked("IBKR outstanding cancel review capacity reached")
            self._cancel_reviews[broker_order_id] = review
            return review

    def _review_autonomous_cancel(
        self,
        *,
        client_ref_id: str,
        order_id: int,
        broker_order_id: str,
    ) -> LocalCancelDecision:
        bridge = self._preflight()
        if not isinstance(bridge, IbkrAutonomousPreflightBridge):
            raise BrokerMutationBlocked(
                "IBKR autonomous cancel preflight is unavailable"
            )
        decision = bridge.review_cancel(
            client_ref_id,
            order_id,
            broker_order_id,
        )
        try:
            self._assert_autonomous_authority("cancel_review")
            now = _utc(self._clock())
            caps = self.descriptor.capabilities
            if (
                not isinstance(decision, LocalCancelDecision)
                or decision.account_masked != caps.account_masked
                or decision.broker_order_id != broker_order_id
                or decision.client_ref_id != client_ref_id
                or decision.reviewed_at > now
                or decision.received_at > now
                or decision.expired_at(now)
                or decision.policy_binding_id != self.policy_binding_id
                or decision.provider_contract_id != self.provider_contract_id
                or not re.fullmatch(
                    r"[0-9a-f]{64}", decision.evidence_collection_id
                )
                or any(
                    check.severity.upper() != "INFO"
                    for check in decision.order_checks
                )
                or not decision.disclosure.strip()
                or decision.preview.get("account_masked") != caps.account_masked
                or decision.preview.get("action") != "cancel"
                or decision.preview.get("broker_order_id") != broker_order_id
                or decision.preview.get("client_ref_id") != client_ref_id
            ):
                raise BrokerMutationBlocked(
                    "IBKR autonomous cancel decision is missing, expired or not exact"
                )
        except BaseException:
            if isinstance(decision, LocalCancelDecision):
                bridge.finish_cancel(decision)
            raise
        return decision

    def cancel_equity_order(
        self, exact_account_id: str, broker_order_id: str, *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self._account(exact_account_id)
        with self._lock:
            caps = self.descriptor.capabilities
            attended = all((
                not caps.supports_unattended_writes,
                caps.review_requires_explicit_confirmation,
                caps.cancel_requires_explicit_confirmation,
            ))
            autonomous = all((
                caps.supports_unattended_writes,
                not caps.review_requires_explicit_confirmation,
                not caps.cancel_requires_explicit_confirmation,
                self.autonomous_authority is not None,
            ))
            if not all((
                caps.supports_equity_cancel,
                caps.supports_daemon_writes,
                attended or autonomous,
            )):
                raise BrokerMutationBlocked("IBKR cancel capability is not accepted")
            self._assert_autonomous_authority("cancel_preflight")
            order_id = self.parse_broker_order_id(broker_order_id)
            intent = self.ledger.lookup_order_id(order_id)
            review: AttendedCancelReview | LocalCancelDecision | None = None
            if (
                autonomous
                and intent is not None
                and intent.cancellation_unknown_seen
            ):
                if explicit_confirmation is not None:
                    raise BrokerMutationBlocked(
                        "IBKR autonomous cancel cannot consume ambient confirmation text"
                    )
                # A second cancel is not a blind replay.  First obtain a new
                # positive whole-account read proving the exact owned order is
                # still working, then let the ledger compare its receipt time
                # with the prior ambiguity marker.
                review = self._review_autonomous_cancel(
                    client_ref_id=intent.client_ref_id,
                    order_id=order_id,
                    broker_order_id=broker_order_id,
                )
                try:
                    intent = self.ledger.authorize_cancel_retry(
                        intent.client_ref_id,
                        evidence_id=review.evidence_collection_id,
                        evidence_received_at=review.received_at,
                    )
                except Exception:
                    bridge = self._preflight()
                    if isinstance(bridge, IbkrAutonomousPreflightBridge):
                        bridge.finish_cancel(review)
                    raise BrokerMutationBlocked(
                        "IBKR cancel retry lacks strictly newer positive broker evidence"
                    ) from None
            if (
                intent is None
                or intent.status != "ACK"
                or not intent.can_cancel
            ):
                if isinstance(review, LocalCancelDecision):
                    bridge = self._preflight()
                    if isinstance(bridge, IbkrAutonomousPreflightBridge):
                        bridge.finish_cancel(review)
                raise BrokerMutationBlocked(
                    "IBKR cancellation requires a reconciled cancellable owned intent"
                )
            if attended:
                review = self._cancel_reviews.get(broker_order_id)
                now = _utc(self._clock())
                if (
                    review is None
                    or review.expired_at(now)
                    or review.account_masked != caps.account_masked
                    or review.broker_order_id != broker_order_id
                    or review.client_ref_id != intent.client_ref_id
                    or explicit_confirmation != review.required_confirmation_phrase
                    or explicit_confirmation
                    != self.cancel_confirmation_phrase(broker_order_id)
                ):
                    raise BrokerMutationBlocked(
                        "IBKR cancel requires the exact unexpired attended review confirmation"
                    )
                # Exact confirmation is one-shot even if a later safety check fails.
                del self._cancel_reviews[broker_order_id]
            else:
                if explicit_confirmation is not None:
                    if isinstance(review, LocalCancelDecision):
                        bridge = self._preflight()
                        if isinstance(bridge, IbkrAutonomousPreflightBridge):
                            bridge.finish_cancel(review)
                    raise BrokerMutationBlocked(
                        "IBKR autonomous cancel cannot consume ambient confirmation text"
                    )
                if review is None:
                    review = self._review_autonomous_cancel(
                        client_ref_id=intent.client_ref_id,
                        order_id=order_id,
                        broker_order_id=broker_order_id,
                    )
            cancel_registered = False
            authority: IbkrDispatchAuthority | None = None
            try:
                self._approved(
                    self._preflight().authorize_cancel(
                        intent.client_ref_id, order_id
                    )
                )
                self.session.assert_write_binding(
                    self.descriptor.authorization_binding_id,
                    self.provider_contract_id,
                )
                authority = self._authority()
                authority.register_cancel(
                    client_ref_id=intent.client_ref_id, order_id=order_id,
                    generation=self.session.generation, review=review,
                )
                cancel_registered = True
                self.ledger.claim_cancel(intent.client_ref_id)
                try:
                    self._assert_autonomous_authority("cancel_dispatch")
                    self.session.cancel(order_id)
                except BrokerMutationBlocked as denied:
                    try:
                        self.ledger.record_cancel_not_sent(
                            intent.client_ref_id
                        )
                    except Exception:
                        # A terminal/pending broker callback observed during
                        # the SDK authorization callback supersedes the local
                        # no-wire marker.  Retain the positive broker fact and
                        # do not invent cancellation ambiguity.
                        try:
                            current = self.ledger.lookup(intent.client_ref_id)
                        except Exception:
                            current = None
                        if current is not None and (
                            current.cancelled_seen
                            or current.pending_cancel_seen
                            or current.rejection_seen
                        ):
                            raise denied from None
                        self._persist_unknown(
                            intent.client_ref_id,
                            "CANCEL_UNKNOWN",
                            "cancel_equity_order_known_no_wire_persistence",
                        )
                        raise BrokerUnknownSubmission(
                            "IBKR cancel no-wire result could not be persisted; reconcile it",
                            detail={"operation": "cancel_equity_order"},
                        ) from None
                    raise
                except Exception:
                    self._persist_unknown(intent.client_ref_id, "CANCEL_UNKNOWN", "cancel_equity_order")
                    raise BrokerUnknownSubmission(
                        "IBKR cancellation outcome is unresolved; do not release reservations",
                        detail={"operation": "cancel_equity_order"},
                    ) from None
                self._persist_unknown(intent.client_ref_id, "CANCEL_UNKNOWN", "cancel_equity_order")
                return self._unknown("cancel_equity_order")
            finally:
                if cancel_registered and authority is not None:
                    authority.finish(order_id)
                if isinstance(review, LocalCancelDecision):
                    bridge = self._preflight()
                    if isinstance(bridge, IbkrAutonomousPreflightBridge):
                        bridge.finish_cancel(review)

    @staticmethod
    def cancel_confirmation_phrase(broker_order_id: str) -> str:
        if not isinstance(broker_order_id, str) or not re.fullmatch(
            r"ibkr:[1-9][0-9]*:[1-9][0-9]*", broker_order_id
        ):
            raise BrokerContractViolation("IBKR order ID is not namespaced")
        return f"CONFIRM CANCEL {broker_order_id}"

    def _persist_unknown(self, client_ref: str, kind: str, operation: str) -> None:
        try:
            self.ledger.record_event(client_ref, str(uuid4()), kind)
        except Exception:
            # The earlier SENDING/cancel claim remains authoritative uncertainty.
            # Never turn a post-send persistence failure into known rejection.
            raise BrokerUnknownSubmission(
                "IBKR dispatch may have occurred; outcome persistence failed",
                detail={"operation": operation},
            ) from None

    def broker_order_id(self, order_id: int) -> str:
        if type(order_id) is not int or order_id <= 0:
            raise BrokerContractViolation("IBKR order ID must be positive")
        return f"ibkr:{self.session.client_id}:{order_id}"

    def parse_broker_order_id(self, value: str) -> int:
        if not isinstance(value, str) or not re.fullmatch(r"ibkr:[1-9][0-9]*:[1-9][0-9]*", value):
            raise BrokerContractViolation("IBKR order ID is not namespaced")
        _, client, order = value.split(":")
        if int(client) != self.session.client_id:
            raise BrokerMutationBlocked("IBKR cannot cancel another client's order")
        return int(order)

    def _unknown(self, operation: str) -> BrokerOperationResult:
        try:
            now = _utc(self._clock())
        except Exception:
            raise BrokerUnknownSubmission(
                "IBKR dispatch may have occurred; local receipt clock unavailable",
                detail={"operation": operation},
            ) from None
        return BrokerOperationResult(
            operation=operation, status=OperationStatus.UNKNOWN,
            observed_at=now, received_at=now, accepted=None,
            message="SDK dispatch is not broker acceptance; account/order reconciliation required.",
        )


__all__ = [
    "IBKR_TRANSPORT_ID", "IbkrProductionTransport",
    "IbkrAutonomousPreflightBridge", "IbkrReadBridge", "IbkrPreflightBridge",
    "attended_confirmation_phrase", "attended_ibkr_descriptor",
    "autonomous_ibkr_descriptor", "attended_order_preview", "staged_ibkr_descriptor",
]
