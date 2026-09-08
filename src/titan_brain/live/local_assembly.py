"""Reviewed local provider assembly shared by every full-live command.

The release launcher constructs exactly one :class:`LocalProviderAssembly`
and asks it for the :class:`RuntimeComposition` passed to doctor, readiness,
the coordinator, and the independent notification worker.  The checked-in
profile contains only endpoint identities and private credential *labels*.
Secrets remain in the local user's macOS Keychain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .calendar import ExchangeCalendar
from .composition import RuntimeComposition
from .discovery_composition import QualityEvidenceRecordReader
from .market_data import MarketSessionState
from .massive_adapter import (
    LocalMassiveReadOnlySource,
    MassiveRestStreamSource,
    PreparedStructure,
    UrllibMassiveRestTransport,
)
from .notifications import GmailProviderBinding
from .pipeline import REQUIRED_HARD_GATE_FACTS
from .provider_clients import (
    CredentialUnavailable,
    GMAIL_SEND_SCOPE,
    GmailDesktopOAuthAuthorizer,
    KeychainItem,
    KeychainMassiveAuthorizer,
    MacOSKeychain,
    MassiveWebSocketStreamTransport,
)


PROVIDER_PROFILE_SCHEMA = "titan_local_provider_bindings_2026-09-08_v1"
CONNECTION_REPORT_SCHEMA = "titan_local_provider_connection_report_2026-09-08_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LocalAssemblyError(RuntimeError):
    """A stable assembly error that never embeds provider response text."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"LOCAL_ASSEMBLY_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("local assembly error code is invalid")
        self.code = normalized
        super().__init__(normalized)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _safe_error(error: BaseException) -> str:
    if isinstance(error, (CredentialUnavailable, LocalAssemblyError)):
        return str(error)
    normalized = re.sub(r"[^A-Z0-9]+", "_", type(error).__name__.upper()).strip("_")
    return f"LOCAL_ASSEMBLY_PROVIDER_{normalized or 'ERROR'}"[:128]


@dataclass(frozen=True)
class ProviderConnection:
    component: str
    implementation_id: str
    credential_source_label: str
    provider_binding_id: str | None
    endpoint: str | None
    status: str
    authenticated: bool
    last_successful_check: datetime | None
    check_kind: str
    error_code: str | None
    action_required: str | None
    account_or_destination_binding: str | None = None

    def __post_init__(self) -> None:
        if self.provider_binding_id is not None and not _SHA256.fullmatch(
            self.provider_binding_id
        ):
            raise ValueError("provider connection binding must be SHA-256")
        if self.last_successful_check is not None:
            object.__setattr__(
                self,
                "last_successful_check",
                _aware(self.last_successful_check, "provider check time"),
            )
        if self.status not in {"CONNECTED", "AVAILABLE_UNPROBED", "BLOCKED", "NOT_CONFIGURED"}:
            raise ValueError("provider connection status is invalid")
        if self.authenticated and self.status != "CONNECTED":
            raise ValueError("authenticated connection must be CONNECTED")

    def public_dict(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["last_successful_check"] = (
            self.last_successful_check.isoformat()
            if self.last_successful_check is not None
            else None
        )
        return payload


class CalendarMarketSession:
    """Manifest-bound session-state callable over the checked-in NYSE calendar."""

    def __init__(self, calendar: ExchangeCalendar) -> None:
        self.calendar = calendar

    def __call__(self, now: datetime) -> MarketSessionState:
        lane = self.calendar.lane(_aware(now, "market session time"))
        return (
            MarketSessionState.ENTRY_ELIGIBLE
            if lane == "regular_entry"
            else MarketSessionState.WAITING_FOR_SESSION
        )


class DeterministicLocalQualityReader(QualityEvidenceRecordReader):
    """Recompute quality evidence from the exact in-memory Massive snapshot.

    Scores are deterministic and deliberately conservative.  Missing context
    maps to zero rather than a fabricated neutral score.  Account-capacity is
    never asserted here because it belongs to the fresh broker risk snapshot;
    the current pipeline therefore remains fail-closed until that architectural
    gate is joined at the critical boundary.
    """

    def __init__(
        self,
        source: MassiveRestStreamSource,
        *,
        provider_binding_id: str,
        quote_max_age_seconds: float,
        max_spread_bps: Decimal | None,
        minimum_depth_multiple: Decimal | None,
        minimum_session_volume: int,
    ) -> None:
        if not _SHA256.fullmatch(provider_binding_id):
            raise ValueError("quality provider binding is invalid")
        self.source = source
        self.provider_binding_id = provider_binding_id
        self.quote_max_age_seconds = float(quote_max_age_seconds)
        self.max_spread_bps = max_spread_bps
        self.minimum_depth_multiple = minimum_depth_multiple
        self.minimum_session_volume = int(minimum_session_volume)

    def readiness(self, *, as_of: datetime, timeout_seconds: float) -> Mapping[str, Any]:
        current = _aware(as_of, "quality readiness time")
        configured = (
            self.max_spread_bps is not None
            and self.max_spread_bps > 0
            and self.minimum_depth_multiple is not None
            and self.minimum_depth_multiple > 0
            and self.minimum_session_volume > 0
        )
        return {
            "ready": configured,
            "authenticated": True,
            "provider_binding_id": self.provider_binding_id,
            "observed_at": current,
            "implementation_id": "titan.deterministic_local_quality.massive_evidence.v1",
            "blocker": None if configured else "OWNER_QUALITY_THRESHOLDS_UNRESOLVED",
        }

    @staticmethod
    def _explicit_score(payload: Mapping[str, Any], name: str) -> float:
        raw = payload.get(name)
        if isinstance(raw, bool):
            return 100.0 if raw else 0.0
        if isinstance(raw, (int, float, Decimal)):
            value = float(raw)
            return max(0.0, min(100.0, value))
        return 0.0

    def get_quality_evidence(
        self,
        source_plan_id: str,
        symbol: str,
        *,
        as_of: datetime,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        current = _aware(as_of, "quality evidence time")
        # Source candidates are local shadow hints.  Resolve the exact plan ID
        # before using its geometry; a symbol-only match is insufficient.
        candidates = self.source.prepared_structures(now=current, limit=64)
        matches = [
            value
            for value in candidates
            if value.source_plan_id == source_plan_id
            and value.symbol == str(symbol).strip().upper()
        ]
        if len(matches) != 1:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_QUALITY_PLAN_NOT_UNIQUE")
        structure: PreparedStructure = matches[0]
        snapshot = self.source.evidence_snapshot(structure.symbol, now=current)
        if not snapshot.readiness.ready:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_QUALITY_SYMBOL_NOT_READY")
        quote = snapshot.quote
        bar = snapshot.latest_completed_bar
        if quote is None or bar is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_QUALITY_MARKET_EVIDENCE_MISSING")
        # Executability freshness is a venue-time property.  A delayed REST
        # response can have a new local receipt without making its quote fresh.
        quote_age = (current - quote.newest_venue_at).total_seconds()
        spread_ok = (
            self.max_spread_bps is not None
            and self.max_spread_bps > 0
            and quote.spread_bps <= self.max_spread_bps
        )
        # Top-of-book sizes are current Massive *shares*.  Without an already
        # sized order this component is descriptive only and the hard gate is
        # false; downstream code must not treat it as full depth.
        depth_score = min(100.0, float(min(quote.bid_size, quote.ask_size)))
        risk = structure.entry_limit - structure.structural_stop
        reward = min(structure.targets) - structure.entry_limit
        favorable = risk > 0 and reward >= risk * Decimal("2")
        geometry = (
            bar.end_at <= current
            and structure.structural_stop < structure.entry_limit
            and all(target > structure.entry_limit for target in structure.targets)
        )
        setup_components = {
            "liquidity": min(
                100.0,
                (float(bar.volume) / max(1.0, float(self.minimum_session_volume))) * 100.0,
            ),
            "relative_volume": self._explicit_score(structure.payload, "relative_volume_score"),
            "technical_structure_vwap": 100.0 if geometry else 0.0,
            "catalyst_context": self._explicit_score(structure.payload, "catalyst_context_score"),
            "sector_market_sympathy": self._explicit_score(structure.payload, "sector_market_sympathy_score"),
            "prior_90_day_behavior": self._explicit_score(structure.payload, "prior_90_day_behavior_score"),
            "gap_behavior": self._explicit_score(structure.payload, "gap_behavior_score"),
            "other_massive_data": 100.0 if snapshot.readiness.ready else 0.0,
        }
        spread_score = (
            max(0.0, 100.0 * (1.0 - float(quote.spread_bps / self.max_spread_bps)))
            if spread_ok and self.max_spread_bps is not None
            else 0.0
        )
        bar_range = bar.high - bar.low
        execution_components = {
            "spread": spread_score,
            "displayed_depth": depth_score,
            "projected_slippage": spread_score,
            "volatility": max(
                0.0,
                100.0 * (1.0 - float(bar_range / max(bar.close, Decimal("0.01")))),
            ),
            "order_size_liquidity": 0.0,
            "halt_risk": 0.0 if quote.halted else 100.0,
        }
        hard_gates = {name: False for name in REQUIRED_HARD_GATE_FACTS}
        hard_gates.update(
            {
                "independent_geometry_revalidation": geometry,
                "causal_completed_bar_structure": bar.end_at <= current,
                "fresh_executable_quote": 0 <= quote_age <= self.quote_max_age_seconds,
                "robinhood_tradable": quote.tradable,
                "acceptable_spread": spread_ok,
                "adequate_displayed_depth": False,
                "acceptable_extension": quote.ask <= structure.entry_limit,
                "favorable_reward_risk": favorable,
                "remaining_capacity": False,
                "current_session_eligible": snapshot.readiness.ready,
            }
        )
        return {
            "evidence_id": "local-quality:" + _hash(
                {
                    "plan": structure.source_plan_id,
                    "quote": (
                        str(quote.bid),
                        str(quote.ask),
                        quote.newest_venue_at.isoformat(),
                        quote.observed_at.isoformat(),
                    ),
                    "bar": bar.digest,
                    "sampled_at": snapshot.sampled_at.isoformat(),
                }
            ),
            "source_plan_id": structure.source_plan_id,
            "symbol": structure.symbol,
            "observed_at": snapshot.sampled_at,
            "completed_bar_end": bar.end_at,
            "entry_limit": str(structure.entry_limit),
            "structural_stop": str(structure.structural_stop),
            "targets": [str(value) for value in structure.targets],
            "execution_reserve_per_share": str(max(quote.ask - quote.bid, Decimal("0.01"))),
            "setup_components": setup_components,
            "execution_components": execution_components,
            "hard_gate_facts": hard_gates,
            "shadow_proposal_grants_authority": False,
        }


class LocalProviderAssembly:
    """Load one immutable public profile and cache its concrete provider objects."""

    def __init__(
        self,
        *,
        release_root: str | Path,
        install_root: str | Path,
        keychain: MacOSKeychain | None = None,
        clock: Any | None = None,
    ) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.install_root = Path(install_root).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.keychain = keychain or MacOSKeychain()
        self.profile = self._load_json(
            self.release_root / "config/provider_bindings.json",
            expected_schema=PROVIDER_PROFILE_SCHEMA,
        )
        self.full_live = self._load_json(
            self.release_root / "config/full_live.json",
            expected_schema="titan_full_live_config_2026-09-08_v1",
        )
        self.calendar = ExchangeCalendar.from_json(
            self.release_root / "config/nyse_calendar_2026.json"
        )
        self._massive_source: MassiveRestStreamSource | None = None
        self._gmail: GmailProviderBinding | None = None

    @staticmethod
    def _load_json(path: Path, *, expected_schema: str) -> Mapping[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_PROFILE_UNAVAILABLE") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != expected_schema:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_PROFILE_INVALID")
        return dict(raw)

    @staticmethod
    def _item(config: Mapping[str, Any], field: str) -> KeychainItem:
        if config.get("credential_backend") != "macos_keychain":
            raise LocalAssemblyError("LOCAL_ASSEMBLY_CREDENTIAL_BACKEND_UNSUPPORTED")
        account = config.get("credential_account")
        return KeychainItem(
            service=str(config.get(field, "")),
            account=(str(account) if account is not None else None),
        )

    def massive_source(self) -> MassiveRestStreamSource:
        if self._massive_source is not None:
            return self._massive_source
        config = self.profile["massive"]
        if not isinstance(config, Mapping):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_MASSIVE_PROFILE_INVALID")
        rest_authorizer = KeychainMassiveAuthorizer(
            self.keychain,
            self._item(config, "credential_service"),
            scopes=tuple(config.get("scopes", ())),
        )
        stream_authorizer = KeychainMassiveAuthorizer(
            self.keychain,
            self._item(config, "credential_service"),
            scopes=tuple(config.get("scopes", ())),
        )
        rest = UrllibMassiveRestTransport(
            rest_authorizer, base_url=str(config.get("rest_base_url", ""))
        )
        stream = MassiveWebSocketStreamTransport(
            stream_authorizer,
            websocket_url=str(config.get("websocket_url", "")),
            connect_timeout_seconds=float(config.get("stream_connect_timeout_seconds", 5)),
            maximum_symbols=int(self.full_live["market_data"]["max_active_candidates"]),
        )
        market = self.full_live["market_data"]
        candidates = LocalMassiveReadOnlySource(
            market["database_path"],
            pilot_id=str(market["producer_pilot_id"]),
            book_mode=str(market["producer_book_mode"]),
            decision_contract_hash=str(market["producer_decision_contract_hash"]),
            health_max_age_seconds=int(market["health_max_age_seconds"]),
            candidate_max_age_seconds=int(market["candidate_max_age_seconds"]),
            session_state=CalendarMarketSession(self.calendar),
        )
        self._massive_source = MassiveRestStreamSource(
            candidates=candidates,
            rest=rest,
            stream=stream,
            session_state=CalendarMarketSession(self.calendar),
            health_max_age_seconds=int(market["health_max_age_seconds"]),
            candidate_max_age_seconds=int(market["candidate_max_age_seconds"]),
            request_timeout_seconds=float(config.get("request_timeout_seconds", 3)),
            stream_drain_timeout_seconds=0.05,
            stream_batch_limit=1000,
            backfill_concurrency=int(config.get("backfill_concurrency", 4)),
        )
        return self._massive_source

    def gmail_binding(self) -> GmailProviderBinding:
        if self._gmail is not None:
            return self._gmail
        config = self.profile["gmail"]
        if not isinstance(config, Mapping) or config.get("enabled") is not True:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_GMAIL_NOT_CONFIGURED")
        if tuple(config.get("scopes", ())) != (GMAIL_SEND_SCOPE,):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_GMAIL_SCOPE_INVALID")
        authorizer = GmailDesktopOAuthAuthorizer(
            self.keychain,
            client_item=self._item(config, "desktop_client_service"),
            refresh_token_item=self._item(config, "refresh_token_service"),
            consent_status_item=self._item(config, "consent_status_service"),
        )
        destination = self.keychain.read_text(
            self._item(config, "destination_service")
        ).strip()
        sender = self.keychain.read_text(self._item(config, "sender_service")).strip()
        if not destination or not sender or any("\n" in value or "\r" in value for value in (destination, sender)):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_GMAIL_ADDRESS_INVALID")
        self._gmail = GmailProviderBinding(
            authorizer=authorizer,
            destination=destination,
            sender_address=sender,
        )
        return self._gmail

    def runtime_composition(self) -> RuntimeComposition:
        execution = self.full_live["execution"]
        discovery = self.full_live["discovery"]
        notifications = self.full_live["notifications"]
        if execution.get("broker_adapter") == "supported_production_transport":
            # No supported standalone Robinhood contract was verified.  Never
            # map the attended Codex OAuth session into a daemon credential.
            raise LocalAssemblyError("LOCAL_ASSEMBLY_SUPPORTED_BROKER_UNAVAILABLE")
        if discovery.get("pipeline_configured") is True:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_ROBINHOOD_TRADABILITY_UNAVAILABLE")
        gmail = (
            self.gmail_binding()
            if notifications.get("delivery_sink") == "gmail_api"
            else None
        )
        return RuntimeComposition(notification_provider=gmail)

    def connection_report(self, *, probe_network: bool) -> Mapping[str, object]:
        checked_at = _aware(self._clock(), "connection report clock")
        connections: list[ProviderConnection] = []
        massive = self.profile["massive"]
        source_label = self._item(massive, "credential_service").source_label
        try:
            source = self.massive_source()
            binding = source.rest.authorization.binding_id
            if probe_network:
                source.rest.get_json(
                    "/v1/marketstatus/now",
                    parameters={},
                    timeout_seconds=float(massive.get("request_timeout_seconds", 3)),
                )
                rest_checked = _aware(self._clock(), "Massive REST receipt")
                connections.append(
                    ProviderConnection(
                        component="massive_rest",
                        implementation_id=str(massive["implementation_id"]),
                        credential_source_label=source_label,
                        provider_binding_id=binding,
                        endpoint=str(massive["rest_base_url"]),
                        status="CONNECTED",
                        authenticated=True,
                        last_successful_check=rest_checked,
                        check_kind="authenticated_market_status_read",
                        error_code=None,
                        action_required=None,
                    )
                )
                stream_checked = source.stream.probe_authentication(
                    timeout_seconds=float(massive.get("stream_connect_timeout_seconds", 5))
                )
                connections.append(
                    ProviderConnection(
                        component="massive_stream",
                        implementation_id=str(massive["implementation_id"]),
                        credential_source_label=source_label,
                        provider_binding_id=binding,
                        endpoint=str(massive["websocket_url"]),
                        status="CONNECTED",
                        authenticated=True,
                        last_successful_check=stream_checked,
                        check_kind="authenticated_wss_handshake",
                        error_code=None,
                        action_required=None,
                    )
                )
            else:
                for component, endpoint in (
                    ("massive_rest", massive["rest_base_url"]),
                    ("massive_stream", massive["websocket_url"]),
                ):
                    connections.append(
                        ProviderConnection(
                            component=component,
                            implementation_id=str(massive["implementation_id"]),
                            credential_source_label=source_label,
                            provider_binding_id=binding,
                            endpoint=str(endpoint),
                            status="AVAILABLE_UNPROBED",
                            authenticated=False,
                            last_successful_check=None,
                            check_kind="credential_presence_only",
                            error_code=None,
                            action_required="run provider-status --probe-network",
                        )
                    )
        except Exception as exc:
            connections.append(
                ProviderConnection(
                    component="massive_rest_and_stream",
                    implementation_id=str(massive["implementation_id"]),
                    credential_source_label=source_label,
                    provider_binding_id=None,
                    endpoint=None,
                    status="BLOCKED",
                    authenticated=False,
                    last_successful_check=None,
                    check_kind="authenticated_read" if probe_network else "credential_presence",
                    error_code=_safe_error(exc),
                    action_required="restore the existing Massive Keychain item or provider access",
                )
            )

        robinhood = self.profile["robinhood"]
        connections.append(
            ProviderConnection(
                component="robinhood_broker_and_tradability",
                implementation_id=str(robinhood["implementation_id"]),
                credential_source_label=str(robinhood["authorization_source"]),
                provider_binding_id=None,
                endpoint=str(robinhood["endpoint"]),
                status="BLOCKED",
                authenticated=False,
                last_successful_check=None,
                check_kind="verified_client_contract",
                error_code=str(robinhood["decision"]),
                action_required=(
                    "provider-supported standalone daemon authorization and unattended contract required"
                ),
                account_or_destination_binding=str(robinhood["account_binding"]),
            )
        )

        quality = self.profile["quality"]
        connections.append(
            ProviderConnection(
                component="deterministic_local_quality",
                implementation_id=str(quality["implementation_id"]),
                credential_source_label="none-release-contained",
                provider_binding_id=None,
                endpoint=None,
                status="BLOCKED",
                authenticated=False,
                last_successful_check=None,
                check_kind="release_implementation_present",
                error_code="OWNER_QUALITY_THRESHOLDS_AND_BROKER_CAPACITY_JOIN_REQUIRED",
                action_required="approve proposed thresholds and bind fresh broker capacity at final gate",
            )
        )

        gmail = self.profile["gmail"]
        gmail_source = "+".join(
            self._item(gmail, field).source_label
            for field in (
                "desktop_client_service",
                "refresh_token_service",
                "consent_status_service",
                "sender_service",
                "destination_service",
            )
        )
        if gmail.get("enabled") is not True:
            connections.append(
                ProviderConnection(
                    component="gmail_notification",
                    implementation_id=str(gmail["implementation_id"]),
                    credential_source_label=gmail_source,
                    provider_binding_id=None,
                    endpoint=GmailDesktopOAuthAuthorizer.TOKEN_ENDPOINT,
                    status="NOT_CONFIGURED",
                    authenticated=False,
                    last_successful_check=None,
                    check_kind="configuration",
                    error_code="GMAIL_OWNER_ROUTE_NOT_CONFIGURED",
                    action_required=(
                        "owner must authorize durable desktop OAuth gmail.send and approve exact destination"
                    ),
                )
            )
        else:
            try:
                binding = self.gmail_binding()
                if probe_network:
                    successful = binding.authorizer.probe_refresh()
                    status = "CONNECTED"
                    authenticated = True
                    check_kind = "oauth_refresh_without_send"
                else:
                    successful = None
                    status = "AVAILABLE_UNPROBED"
                    authenticated = False
                    check_kind = "credential_presence_only"
                connections.append(
                    ProviderConnection(
                        component="gmail_notification",
                        implementation_id=str(gmail["implementation_id"]),
                        credential_source_label=gmail_source,
                        provider_binding_id=binding.authorization_binding_id,
                        endpoint=GmailDesktopOAuthAuthorizer.TOKEN_ENDPOINT,
                        status=status,
                        authenticated=authenticated,
                        last_successful_check=successful,
                        check_kind=check_kind,
                        error_code=None,
                        action_required=(
                            "owner-authorized destination delivery test still required; no message was sent"
                        ),
                        account_or_destination_binding=_hash(
                            {
                                "provider": "gmail",
                                "destination": binding.destination.strip().lower(),
                            }
                        ),
                    )
                )
            except Exception as exc:
                connections.append(
                    ProviderConnection(
                        component="gmail_notification",
                        implementation_id=str(gmail["implementation_id"]),
                        credential_source_label=gmail_source,
                        provider_binding_id=None,
                        endpoint=GmailDesktopOAuthAuthorizer.TOKEN_ENDPOINT,
                        status="BLOCKED",
                        authenticated=False,
                        last_successful_check=None,
                        check_kind="oauth_refresh_without_send" if probe_network else "credential_presence",
                        error_code=_safe_error(exc),
                        action_required="repair the owner-approved Gmail desktop OAuth Keychain items",
                    )
                )
        return {
            "schema_version": CONNECTION_REPORT_SCHEMA,
            "checked_at": checked_at.isoformat(),
            "probe_network": bool(probe_network),
            "broker_mutations_invoked": [],
            "notification_messages_sent": [],
            "connections": [value.public_dict() for value in connections],
        }

    def close(self) -> None:
        if self._massive_source is not None:
            self._massive_source.close(wait=False)
            close = getattr(self._massive_source.stream, "close", None)
            if callable(close):
                close()


__all__ = [
    "CONNECTION_REPORT_SCHEMA",
    "CalendarMarketSession",
    "DeterministicLocalQualityReader",
    "LocalAssemblyError",
    "LocalProviderAssembly",
    "ProviderConnection",
]
