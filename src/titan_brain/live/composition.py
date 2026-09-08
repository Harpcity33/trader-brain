"""Credential-neutral composition seam for supported runtime integrations.

The checked-in CLI must not discover credentials or silently swap adapters.
A separately packaged, provider-supported launcher may inject this object into
``cli.main``. The signed configuration still names the exact transport and
discovery composition identities, so injection alone never grants authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import inspect
import json
import math
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.request import urlopen as stdlib_urlopen

from .broker import BrokerClient
from .broker.factory import build_broker_client
from .broker.production import ProductionTransport
from .component_provenance import (
    ReleaseBoundComponentEvidence,
    UnsignedCompositionError,
    verify_release_bound_component,
)
from .control import ControlInbox, HmacControlAuthenticator
from .discovery_composition import (
    SUPPORTED_DISCOVERY_COMPOSITION_ID,
    SupportedDiscoveryProviderComposition,
)
from .notifications import (
    GMAIL_PROVIDER_COMPOSITION_ID,
    GmailProviderBinding,
    NotificationSink,
    build_notification_sink,
    destination_fingerprint,
)


class RuntimeCompositionError(RuntimeError):
    pass


_SAFE_ATTESTATION_ROLES = frozenset(
    {
        "control_authenticator",
        "discovery_executor",
        "discovery_provider",
        "gmail_authorizer",
        "instrument_evidence_provider",
        "market_source",
        "massive_candidate_provider",
        "massive_rest_authorizer",
        "massive_rest_transport",
        "massive_stream_transport",
        "notification_provider",
        "notification_sender",
        "notification_sink",
        "production_transport",
        "quality_evidence_provider",
        "quality_evidence_reader",
        "robinhood_instrument_reader",
        "session_state_provider",
    }
)


def _unsigned_composition_failure(error: UnsignedCompositionError) -> str:
    """Preserve the stable attestation code, never provider/path detail."""

    detail = str(getattr(error, "detail", "")).split(":", 1)[0].strip().upper()
    if not detail or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        for character in detail
    ):
        detail = "ATTESTATION_FAILED"
    raw_role = str(getattr(error, "role", ""))
    role = raw_role if raw_role in _SAFE_ATTESTATION_ROLES else "runtime"
    return f"UNSIGNED_COMPOSITION:{role}:{detail}"


def _profile_value(value: object) -> object:
    """Return deterministic, JSON-safe non-secret profile material."""

    if isinstance(value, Enum):
        return _profile_value(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeCompositionError("runtime profile contains a non-finite value")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RuntimeCompositionError("runtime profile contains a naive timestamp")
        return value.isoformat()
    if is_dataclass(value):
        return _profile_value(asdict(value))
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if not name or name in normalized:
                raise RuntimeCompositionError("runtime profile contains invalid keys")
            normalized[name] = _profile_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (tuple, list)):
        return [_profile_value(item) for item in value]
    raise RuntimeCompositionError(
        f"runtime profile contains unsupported {type(value).__name__} material"
    )


def _authorization_facts(evidence: object, *, provider: str) -> Mapping[str, object]:
    """Extract only normalized, non-secret authorization evidence."""

    observed_provider = str(getattr(evidence, "provider", ""))
    binding = str(getattr(evidence, "binding_id", ""))
    credential_source = str(getattr(evidence, "credential_source", ""))
    scopes = tuple(str(item) for item in getattr(evidence, "scopes", ()))
    authenticated = getattr(evidence, "authenticated", None)
    if (
        observed_provider != provider
        or len(binding) != 64
        or any(character not in "0123456789abcdef" for character in binding)
        or not credential_source
        or not scopes
        or authenticated is not True
    ):
        raise RuntimeCompositionError(
            f"{provider} authorization evidence is not production-normalized"
        )
    return {
        "provider": observed_provider,
        "binding_id": binding,
        "credential_source": credential_source,
        "scopes": scopes,
        "authenticated": True,
    }


@runtime_checkable
class DiscoveryProviderComposition(Protocol):
    """One owner-approved bundle of market, instrument, and entry providers."""

    @property
    def identity(self) -> str: ...

    @property
    def market_source(self) -> object: ...

    def tradability_ready(self, *, now: datetime) -> bool: ...

    def build_executor(
        self,
        *,
        policy: object,
        state: object,
        broker: BrokerClient,
        writer_lock: object,
        latency: object,
        authority: object,
    ) -> object: ...


class RuntimeComposition:
    """Reuse one exact broker/provider composition across probes and service."""

    def __init__(
        self,
        *,
        production_transport: ProductionTransport | None = None,
        discovery_provider: DiscoveryProviderComposition | None = None,
        notification_provider: GmailProviderBinding | None = None,
        control_authentication_key: bytes | None = None,
        control_authorization_binding_id: str | None = None,
    ) -> None:
        self.production_transport = production_transport
        self.discovery_provider = discovery_provider
        self.notification_provider = notification_provider
        if (control_authentication_key is None) != (
            control_authorization_binding_id is None
        ):
            raise ValueError(
                "control key and authorization binding must be injected together"
            )
        self._control_authenticator = (
            HmacControlAuthenticator(
                control_authentication_key,
                authorization_binding_id=str(control_authorization_binding_id),
            )
            if control_authentication_key is not None
            else None
        )
        self._control_key_fingerprint = (
            hashlib.sha256(bytes(control_authentication_key)).hexdigest()
            if control_authentication_key is not None
            else None
        )
        self._broker: BrokerClient | None = None
        self._broker_key: tuple[str, str, str] | None = None
        self._discovery: object | None = None
        self._notification_sink: NotificationSink | None = None
        self._notification_key: tuple[str, ...] | None = None
        self._release_binding: tuple[str, Path] | None = None
        self._configured_component_ids: dict[str, int] = {}
        self._release_component_ids: dict[str, int] = {}
        self._provenance: dict[str, ReleaseBoundComponentEvidence] = {}
        self._bound_manifest: Mapping[str, object] | None = None
        self._static_component_roles: set[str] = set()
        self._bound_runtime_profile_hash: str | None = None

    def control_inbox(
        self,
        root: str | Path,
        *,
        account_key: str,
        runtime_id: str,
        release_manifest_hash: str,
        max_snapshot_age: timedelta,
        execution_config: Mapping[str, object],
    ) -> ControlInbox:
        """Compose a release-bound control spool without persisting its key."""

        if (
            self._release_binding is None
            or str(release_manifest_hash) != self._release_binding[0]
        ):
            raise RuntimeCompositionError(
                "UNSIGNED_COMPOSITION:control_inbox:release provenance unavailable"
            )
        authenticator = self._control_authenticator
        production = (
            execution_config.get("broker_adapter")
            == "supported_production_transport"
        )
        if production and authenticator is None:
            raise RuntimeCompositionError(
                "production runtime has no authenticated managed-closeout control authority"
            )
        if not production and authenticator is not None:
            raise RuntimeCompositionError(
                "attended broker placeholder cannot receive autonomous control authority"
            )
        if authenticator is not None:
            self._verify_dynamic_component(
                authenticator,
                role="control_authenticator",
                semantic_members=(
                    "authorization_binding_id",
                    "sign",
                    "verify",
                ),
            )
            if (
                authenticator.authorization_binding_id
                != str(
                    execution_config.get("production_authorization_binding_id", "")
                )
            ):
                raise RuntimeCompositionError(
                    "injected control authorization differs from signed production authorization"
                )
        return ControlInbox(
            root,
            account_key=account_key,
            runtime_id=runtime_id,
            release_manifest_hash=release_manifest_hash,
            max_snapshot_age=max_snapshot_age,
            authenticator=authenticator,
        )

    def bind_release(
        self,
        manifest: Mapping[str, object],
        *,
        release_root: str | Path,
    ) -> tuple[ReleaseBoundComponentEvidence, ...]:
        """Bind executable integrations to exact release-inventory bytes.

        String provider IDs remain routing selectors, never executable-code
        authentication.  This check happens before any injected protocol
        property or method is invoked by readiness, doctor, or the service.
        """

        manifest_hash = str(manifest.get("release_manifest_hash", ""))
        root = Path(release_root).resolve()
        binding = (manifest_hash, root)
        components = (
            (
                "production_transport",
                self.production_transport,
                (
                    "descriptor",
                    "release_components",
                    "get_account_base",
                    "list_order_family_page",
                    "lookup_equity_orders_by_client_ref",
                    "review_equity_order",
                    "place_equity_order",
                    "cancel_equity_order",
                ),
            ),
            (
                "discovery_provider",
                self.discovery_provider,
                (
                    "identity",
                    "market_source",
                    "release_components",
                    "tradability_ready",
                    "build_executor",
                ),
            ),
            (
                "notification_provider",
                self.notification_provider,
                (
                    "implementation_id",
                    "authorization_binding_id",
                    "release_components",
                ),
            ),
            (
                "control_authenticator",
                self._control_authenticator,
                ("authorization_binding_id", "sign", "verify"),
            ),
        )
        component_ids = {
            role: id(component)
            for role, component, _members in components
            if component is not None
        }
        if self._release_binding is not None:
            if (
                self._release_binding != binding
                or self._configured_component_ids != component_ids
            ):
                raise RuntimeCompositionError(
                    "UNSIGNED_COMPOSITION:RUNTIME:release/component binding changed"
                )
            self.assert_runtime_profile(self._bound_runtime_profile_hash)
            return self.component_provenance

        evidence: dict[str, ReleaseBoundComponentEvidence] = {}
        release_component_ids: dict[str, int] = {}
        try:
            for role, component, members in components:
                if component is None:
                    continue
                self._bind_component_tree(
                    component,
                    role=role,
                    semantic_members=members,
                    manifest=manifest,
                    release_root=root,
                    evidence=evidence,
                    component_ids=release_component_ids,
                )
        except UnsignedCompositionError as exc:
            raise RuntimeCompositionError(_unsigned_composition_failure(exc)) from exc
        self._release_binding = binding
        self._configured_component_ids = dict(component_ids)
        self._release_component_ids = release_component_ids
        self._provenance = evidence
        self._static_component_roles = set(evidence)
        self._bound_manifest = deepcopy(dict(manifest))
        self._bound_runtime_profile_hash = self._calculate_runtime_profile_hash()
        return self.component_provenance

    @property
    def component_provenance(self) -> tuple[ReleaseBoundComponentEvidence, ...]:
        return tuple(self._provenance[key] for key in sorted(self._provenance))

    @property
    def component_provenance_hash(self) -> str | None:
        """Compatibility name for the immutable runtime-profile identity.

        Readiness schema v2 already carries this field.  Its value now binds
        the complete statically declared executable tree plus normalized
        instance facts, instead of a growing list of source files alone.
        """

        if self._release_binding is None:
            return None
        self.assert_runtime_profile(self._bound_runtime_profile_hash)
        return self._bound_runtime_profile_hash

    @property
    def runtime_profile_hash(self) -> str | None:
        return self.component_provenance_hash

    def assert_runtime_profile(self, expected_hash: str | None) -> None:
        if self._release_binding is None or self._bound_runtime_profile_hash is None:
            raise RuntimeCompositionError(
                "UNSIGNED_COMPOSITION:RUNTIME:release provenance unavailable"
            )
        if expected_hash is None or len(str(expected_hash)) != 64:
            raise RuntimeCompositionError(
                "UNSIGNED_COMPOSITION:RUNTIME:expected profile unavailable"
            )
        current = self._calculate_runtime_profile_hash()
        if current != self._bound_runtime_profile_hash:
            raise RuntimeCompositionError(
                "UNSIGNED_COMPOSITION:RUNTIME:bound instance profile changed"
            )
        if current != str(expected_hash):
            raise RuntimeCompositionError(
                "UNSIGNED_COMPOSITION:RUNTIME:profile differs from activation"
            )

    def _bind_component_tree(
        self,
        component: object,
        *,
        role: str,
        semantic_members: tuple[str, ...],
        manifest: Mapping[str, object],
        release_root: Path,
        evidence: dict[str, ReleaseBoundComponentEvidence],
        component_ids: dict[str, int],
    ) -> None:
        """Verify a declared executable dependency tree before any use."""

        normalized_role = str(role).strip()
        if not normalized_role or normalized_role in evidence:
            raise UnsignedCompositionError(
                normalized_role or "dependency", "DEPENDENCY_ROLE_DUPLICATE"
            )
        if id(component) in component_ids.values():
            raise UnsignedCompositionError(
                normalized_role, "DEPENDENCY_OBJECT_REUSED_UNDER_ANOTHER_ROLE"
            )
        members = tuple(str(item) for item in semantic_members)
        release_components = None
        declares_release_components = False
        if not inspect.isfunction(component):
            try:
                declared = inspect.getattr_static(type(component), "release_components")
            except AttributeError:
                declared = None
            if declared is not None:
                declares_release_components = True
                if "release_components" not in members:
                    members = (*members, "release_components")
        item_evidence = verify_release_bound_component(
            component,
            role=normalized_role,
            semantic_members=members,
            manifest=manifest,
            release_root=release_root,
        )
        evidence[normalized_role] = item_evidence
        component_ids[normalized_role] = id(component)
        if not declares_release_components:
            return
        # Resolve a potentially custom descriptor only after both its class
        # and enumerator implementation bytes have been release-verified.
        release_components = getattr(component, "release_components", None)
        if not callable(release_components):
            raise UnsignedCompositionError(
                normalized_role, "DEPENDENCY_ENUMERATOR_NOT_CALLABLE"
            )
        try:
            nested = tuple(release_components())
        except Exception as exc:
            raise UnsignedCompositionError(
                normalized_role, f"DEPENDENCY_ENUMERATION_FAILED:{type(exc).__name__}"
            ) from exc
        for item in nested:
            if (
                not isinstance(item, tuple)
                or len(item) != 3
                or not isinstance(item[0], str)
                or not isinstance(item[2], tuple)
            ):
                raise UnsignedCompositionError(
                    normalized_role, "DEPENDENCY_INVENTORY_INVALID"
                )
            child_role, child_component, child_members = item
            self._bind_component_tree(
                child_component,
                role=child_role,
                semantic_members=child_members,
                manifest=manifest,
                release_root=release_root,
                evidence=evidence,
                component_ids=component_ids,
            )

    def _runtime_profile_facts(self) -> Mapping[str, object]:
        facts: dict[str, object] = {}
        if self.production_transport is not None:
            descriptor = self.production_transport.descriptor
            capabilities = getattr(descriptor, "capabilities", None)
            if not is_dataclass(capabilities):
                raise RuntimeCompositionError(
                    "production capability descriptor is not normalized"
                )
            facts["production_transport"] = {
                "transport_id": str(getattr(descriptor, "transport_id", "")),
                "account_binding_fingerprint": str(
                    getattr(descriptor, "account_binding_fingerprint", "")
                ),
                "authorization_binding_id": str(
                    getattr(descriptor, "authorization_binding_id", "")
                ),
                "maximum_order_pages_per_family": getattr(
                    descriptor, "maximum_order_pages_per_family", None
                ),
                # The private exact account identifier is deliberately absent;
                # its signed non-reversible fingerprint is the persisted fact.
                "capabilities": asdict(capabilities),
            }
        if self.discovery_provider is not None:
            provider = self.discovery_provider
            discovery: dict[str, object] = {
                "identity": str(provider.identity),
                "provider_binding_id": str(
                    getattr(provider, "provider_binding_id", "")
                ),
                "timeout_seconds": getattr(provider, "timeout_seconds", None),
            }
            source = provider.market_source
            if isinstance(provider, SupportedDiscoveryProviderComposition):
                rest_authorization = getattr(
                    getattr(source, "rest", None), "authorization", None
                )
                stream_authorization = getattr(
                    getattr(source, "stream", None), "authorization", None
                )
                discovery["massive_rest_authorization"] = _authorization_facts(
                    rest_authorization, provider="massive"
                )
                discovery["massive_stream_authorization"] = _authorization_facts(
                    stream_authorization, provider="massive"
                )
                discovery["source_limits"] = {
                    name: getattr(source, name, None)
                    for name in (
                        "health_max_age_seconds",
                        "candidate_max_age_seconds",
                        "request_timeout_seconds",
                        "stream_drain_timeout_seconds",
                        "stream_batch_limit",
                    )
                }
            facts["discovery_provider"] = discovery
        if self.notification_provider is not None:
            provider = self.notification_provider
            authorization = provider.authorizer.evidence
            facts["notification_provider"] = {
                "implementation_id": provider.implementation_id,
                "authorization": _authorization_facts(
                    authorization, provider="gmail"
                ),
                "destination_fingerprint": destination_fingerprint(
                    "gmail", provider.destination
                ),
                "sender_fingerprint": hashlib.sha256(
                    provider.sender_address.strip().lower().encode("utf-8")
                ).hexdigest(),
            }
        if self._control_authenticator is not None:
            facts["control_authenticator"] = {
                "authorization_binding_id": (
                    self._control_authenticator.authorization_binding_id
                ),
                "key_fingerprint": self._control_key_fingerprint,
            }
        return facts

    def _calculate_runtime_profile_hash(self) -> str:
        if self._release_binding is None and self._bound_manifest is None:
            # During initial binding the caller has already verified the
            # supplied manifest; the release hash is taken from that input.
            manifest_hash = ""
        else:
            manifest_hash = (
                self._release_binding[0]
                if self._release_binding is not None
                else str(self._bound_manifest.get("release_manifest_hash", ""))
            )
        if not manifest_hash and self._bound_manifest is not None:
            manifest_hash = str(self._bound_manifest.get("release_manifest_hash", ""))
        payload = {
            "schema_version": "titan_runtime_profile_2026-09-08_v1",
            "release_manifest_hash": manifest_hash,
            "components": [
                {
                    "role": role,
                    "evidence_hash": self._provenance[role].evidence_hash,
                }
                for role in sorted(self._static_component_roles)
            ],
            "facts": self._runtime_profile_facts(),
        }
        normalized = _profile_value(payload)
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _require_release_bound(self, role: str, component: object) -> None:
        if (
            self._release_binding is None
            or self._release_component_ids.get(role) != id(component)
            or role not in self._provenance
        ):
            raise RuntimeCompositionError(
                f"UNSIGNED_COMPOSITION:{role}:release provenance unavailable"
            )

    def broker_client(
        self, execution_config: Mapping[str, object], *, account_masked: str
    ) -> BrokerClient:
        kind = str(execution_config.get("broker_adapter", ""))
        transport_id = str(execution_config.get("production_transport_id", ""))
        key = (str(account_masked), kind, transport_id)
        if self._broker is None:
            if self.production_transport is not None:
                self._require_release_bound(
                    "production_transport", self.production_transport
                )
            self._broker = build_broker_client(
                execution_config,
                account_masked=account_masked,
                production_transport=self.production_transport,
            )
            self._broker_key = key
        elif self._broker_key != key:
            raise RuntimeCompositionError(
                "runtime attempted to reuse one broker composition under a different signed identity"
            )
        return self._broker

    def market_source(self, discovery_config: Mapping[str, object]) -> object | None:
        provider = self._bound_discovery_provider(discovery_config)
        if provider is None:
            return None
        source = provider.market_source
        self._verify_dynamic_component(
            source, role="market_source", semantic_members=("health",)
        )
        return source

    def tradability_ready(
        self, discovery_config: Mapping[str, object], *, now: datetime
    ) -> bool:
        provider = self._bound_discovery_provider(discovery_config)
        return bool(provider is not None and provider.tradability_ready(now=now))

    def build_discovery_executor(
        self,
        *,
        policy: Any,
        state: object,
        broker: BrokerClient,
        writer_lock: object,
        latency: object,
        authority: object,
    ) -> object | None:
        discovery_config = policy.config["discovery"]
        provider = self._bound_discovery_provider(discovery_config)
        if provider is None:
            if policy.live_entries_configured:
                raise RuntimeCompositionError(
                    "live entries are configured without the signed discovery/provider composition"
                )
            return None
        if self._discovery is None:
            executor = provider.build_executor(
                policy=policy,
                state=state,
                broker=broker,
                writer_lock=writer_lock,
                latency=latency,
                authority=authority,
            )
            self._verify_dynamic_component(
                executor,
                role="discovery_executor",
                semantic_members=("execute", "final_entry_evidence_failures"),
            )
            pipeline = getattr(executor, "pipeline", None)
            if isinstance(provider, SupportedDiscoveryProviderComposition):
                instrument = getattr(pipeline, "instrument_evidence", None)
                quality = getattr(pipeline, "quality_evidence", None)
                if instrument is None or quality is None:
                    raise RuntimeCompositionError(
                        "supported discovery executor omitted evidence providers"
                    )
                self._verify_dynamic_component(
                    instrument,
                    role="instrument_evidence_provider",
                    semantic_members=("get_instrument_evidence",),
                )
                self._verify_dynamic_component(
                    quality,
                    role="quality_evidence_provider",
                    semantic_members=("revalidate_structure",),
                )
            if not callable(getattr(executor, "execute", None)):
                raise RuntimeCompositionError(
                    "discovery provider returned no lifecycle-compatible executor"
                )
            self._discovery = executor
        return self._discovery

    def notification_sink(
        self,
        notification_config: Mapping[str, object],
        *,
        local_jsonl_path: str | Path,
    ) -> NotificationSink:
        """Bind one exact signed route to its release-shipped implementation.

        Gmail authorization and addresses remain runtime-only.  The signed
        configuration binds the concrete implementation ID, the non-secret
        authorization identity, route version, and destination fingerprint;
        the release manifest binds the implementation module bytes.
        """

        sink_kind = str(notification_config.get("delivery_sink", ""))
        key = (
            sink_kind,
            str(notification_config.get("provider", "")),
            str(notification_config.get("destination_fingerprint", "")),
            str(notification_config.get("route_version", "")),
            str(notification_config.get("required_assurance", "")),
            str(notification_config.get("provider_composition_id", "")),
            str(notification_config.get("authorization_binding_id", "")),
            str(Path(local_jsonl_path).expanduser().resolve(strict=False)),
        )
        if self._notification_sink is not None:
            if self._notification_key != key:
                raise RuntimeCompositionError(
                    "runtime attempted to reuse one notification composition under a different signed identity"
                )
            return self._notification_sink

        provider: GmailProviderBinding | None = None
        if sink_kind == "gmail_api":
            provider = self.notification_provider
            if provider is None:
                raise RuntimeCompositionError(
                    "signed Gmail route has no injected runtime provider binding"
                )
            if type(provider) is not GmailProviderBinding:
                raise RuntimeCompositionError(
                    "signed Gmail route requires the release-shipped provider binding"
                )
            self._require_release_bound("notification_provider", provider)
            expected_implementation = str(
                notification_config.get("provider_composition_id", "")
            )
            if (
                expected_implementation != GMAIL_PROVIDER_COMPOSITION_ID
                or provider.implementation_id != expected_implementation
            ):
                raise RuntimeCompositionError(
                    "injected notification implementation differs from signed configuration"
                )
            expected_authorization = str(
                notification_config.get("authorization_binding_id", "")
            )
            if (
                not expected_authorization
                or provider.authorization_binding_id != expected_authorization
            ):
                raise RuntimeCompositionError(
                    "injected notification authorization differs from signed configuration"
                )
        try:
            composed = build_notification_sink(
                notification_config,
                local_jsonl_path=local_jsonl_path,
                gmail=provider,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeCompositionError(
                "signed notification route could not be composed"
            ) from exc
        if sink_kind == "gmail_api":
            self._verify_dynamic_component(
                composed,
                role="notification_sink",
                semantic_members=("send",),
            )
            sender = getattr(composed, "_sender", None)
            if sender is None:
                raise RuntimeCompositionError(
                    "UNSIGNED_COMPOSITION:notification_sender:sender unavailable"
                )
            if getattr(sender, "_opener", None) is not stdlib_urlopen:
                raise RuntimeCompositionError(
                    "UNSIGNED_COMPOSITION:notification_sender:custom opener"
                )
            self._verify_dynamic_component(
                sender,
                role="notification_sender",
                semantic_members=("__call__",),
            )
        self._notification_sink = composed
        self._notification_key = key
        return composed

    def _bound_discovery_provider(
        self, discovery_config: Mapping[str, object]
    ) -> DiscoveryProviderComposition | None:
        if discovery_config.get("pipeline_configured") is not True:
            return None
        expected = str(discovery_config.get("provider_composition_id", "")).strip()
        if not expected:
            raise RuntimeCompositionError(
                "configured discovery pipeline has no signed provider composition identity"
            )
        provider = self.discovery_provider
        if provider is None:
            return None
        self._require_release_bound("discovery_provider", provider)
        if not isinstance(provider, DiscoveryProviderComposition):
            raise RuntimeCompositionError(
                "signed discovery provider does not implement its runtime contract"
            )
        actual = str(provider.identity).strip()
        if not actual or actual != expected:
            raise RuntimeCompositionError(
                "injected discovery provider identity differs from signed configuration"
            )
        if expected == SUPPORTED_DISCOVERY_COMPOSITION_ID and not isinstance(
            provider, SupportedDiscoveryProviderComposition
        ):
            raise RuntimeCompositionError(
                "supported production discovery identity requires the release-shipped composition"
            )
        if expected == SUPPORTED_DISCOVERY_COMPOSITION_ID:
            expected_binding = str(
                discovery_config.get("provider_binding_id", "")
            ).strip()
            if (
                len(expected_binding) != 64
                or getattr(provider, "provider_binding_id", None)
                != expected_binding
            ):
                raise RuntimeCompositionError(
                    "injected discovery authorization differs from signed configuration"
                )
        release_components = getattr(provider, "release_components", None)
        if callable(release_components):
            try:
                nested = tuple(release_components())
            except Exception as exc:
                raise RuntimeCompositionError(
                    "signed discovery provider could not enumerate executable dependencies"
                ) from exc
            for item in nested:
                if (
                    not isinstance(item, tuple)
                    or len(item) != 3
                    or not isinstance(item[0], str)
                    or not isinstance(item[2], tuple)
                ):
                    raise RuntimeCompositionError(
                        "signed discovery provider dependency inventory is invalid"
                    )
                role, component, members = item
                self._verify_dynamic_component(
                    component,
                    role=role,
                    semantic_members=members,
                )
        return provider

    def _verify_dynamic_component(
        self,
        component: object,
        *,
        role: str,
        semantic_members: tuple[str, ...],
    ) -> None:
        if self._release_binding is None:
            raise RuntimeCompositionError(
                f"UNSIGNED_COMPOSITION:{role}:release provenance unavailable"
            )
        existing_id = self._release_component_ids.get(role)
        if existing_id is not None:
            if existing_id != id(component):
                raise RuntimeCompositionError(
                    f"UNSIGNED_COMPOSITION:{role}:runtime implementation changed"
                )
            return
        manifest_hash, release_root = self._release_binding
        # Reuse the exact inventory supplied during the original binding.  A
        # private copy prevents an external launcher from swapping mappings
        # between provider construction and executor/source creation.
        manifest = self._bound_manifest
        if manifest is None:
            raise RuntimeCompositionError(
                f"UNSIGNED_COMPOSITION:{role}:manifest inventory unavailable"
            )
        try:
            evidence = verify_release_bound_component(
                component,
                role=role,
                semantic_members=semantic_members,
                manifest=manifest,
                release_root=release_root,
            )
        except UnsignedCompositionError as exc:
            raise RuntimeCompositionError(_unsigned_composition_failure(exc)) from exc
        if evidence.release_manifest_hash != manifest_hash:
            raise RuntimeCompositionError(
                f"UNSIGNED_COMPOSITION:{role}:manifest binding changed"
            )
        self._release_component_ids[role] = id(component)
        self._provenance[role] = evidence


__all__ = [
    "DiscoveryProviderComposition",
    "SUPPORTED_DISCOVERY_COMPOSITION_ID",
    "SupportedDiscoveryProviderComposition",
    "RuntimeComposition",
    "RuntimeCompositionError",
]
