"""Reviewed local provider assembly shared by every full-live command.

The release launcher constructs exactly one :class:`LocalProviderAssembly`
and asks it for a command-scoped :class:`RuntimeComposition`.  Doctor,
readiness, and the coordinator receive the complete broker/discovery graph;
notification commands receive only delivery, and emergency controls receive
only their private authenticator.  The checked-in profile contains only
endpoint identities and private credential *labels*. Secrets remain in the
local user's macOS Keychain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .calendar import ExchangeCalendar
from .broker.ibkr_authority import IbkrDispatchAuthority
from .broker.ibkr_ledger import IbkrExecutionLedger
from .broker.ibkr_risk_evidence import (
    IBKR_DAILY_RISK_BASELINE_SCHEMA,
    IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH,
    DailyIbkrRiskBaselineAuthenticator,
    IbkrDailyRiskBindingProvider,
    IbkrRiskEvidenceAccountSnapshotReader,
    IbkrRiskEvidenceEnricher,
    IbkrRiskEvidenceError,
    IbkrRiskHighWaterLedger,
    IbkrRiskLedgerBindings,
)
from .broker.ibkr_preflight import (
    IbkrAutonomousPolicyPreflightBridge,
    IbkrAttendedPreflightBridge,
    IbkrOrderPurpose,
)
from .broker.ibkr_reconciliation import IbkrReconciliationTransport
from .broker.ibkr_sdk import IbkrWriteEvidence
from .broker.ibkr_transport import IBKR_TRANSPORT_ID, IbkrProductionTransport
from .composition import RuntimeComposition
from .discovery_composition import (
    QualityEvidenceRecordReader,
    SupportedIbkrDiscoveryProviderComposition,
)
from .ibkr_instrument_provider import IbkrPipelineInstrumentEvidenceProvider
from .market_data import MarketSessionState
from .massive_adapter import (
    LocalMassiveReadOnlySource,
    MassiveRestStreamSource,
    PreparedStructure,
    UrllibMassiveRestTransport,
)
from .notifications import GmailProviderBinding
from .pipeline import POST_SIZING_HARD_GATE_FACTS, REQUIRED_HARD_GATE_FACTS
from .provider_clients import (
    CredentialUnavailable,
    GMAIL_SEND_SCOPE,
    GmailDesktopOAuthAuthorizer,
    KeychainItem,
    KeychainMassiveAuthorizer,
    MacOSKeychain,
    MassiveWebSocketStreamTransport,
    select_account_gmail_profile,
)
from .provider_profile import (
    IbkrLocalProviderProfile,
    ProviderProfileError,
    validate_installed_sdk,
)
from .policy import PolicyBundle
from .ibkr_autonomous_authority import (
    IbkrAutonomousAuthorityBindings,
    VerifiedIbkrAutonomousAuthority,
)
from .ibkr_autonomous_policy_receipt import (
    IbkrAutonomousPolicyReceiptBindings,
    VerifiedIbkrAutonomousPolicyReceipt,
)
from .writer_lock import AccountWriterLock


PROVIDER_PROFILE_SCHEMA = "titan_local_provider_bindings_2026-09-08_v1"
CONNECTION_REPORT_SCHEMA = "titan_local_provider_connection_report_2026-09-08_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IBKR_INFORMATIONAL_CODES = frozenset({1101, 1102, 2104, 2106, 2107, 2108, 2158})


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
        if self.status not in {
            "STAGED",
            "CONNECTED",
            "AVAILABLE_UNPROBED",
            "BLOCKED",
            "NOT_CONFIGURED",
        }:
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


class IbkrRegularHoursEligibility:
    """Calendar-bound entry versus management session eligibility."""

    def __init__(self, calendar: ExchangeCalendar) -> None:
        self.calendar = calendar

    def __call__(self, now: datetime, purpose: object) -> bool:
        current = _aware(now, "IBKR session time")
        if purpose is IbkrOrderPurpose.ENTRY:
            return self.calendar.lane(current) == "regular_entry"
        local = current.astimezone(ZoneInfo("America/New_York"))
        session = self.calendar.session_times(local.date())
        return bool(
            session is not None
            and session.open_at <= local < session.close_at
        )


class IbkrPreflightClock:
    """Distinct release role over the assembly's trusted clock source."""

    def __init__(self, source: Any) -> None:
        if not callable(source):
            raise TypeError("IBKR preflight clock source must be callable")
        self.source = source

    def __call__(self) -> datetime:
        return _aware(self.source(), "IBKR preflight clock")


@dataclass(frozen=True)
class IbkrCommandAssemblyInputs:
    """Explicit owner/runtime evidence which configuration cannot invent."""

    plan_reader: Any
    risk_policy_check: Any
    acceptance_verifier: Any
    mutation_interlock: Any
    write_evidence: IbkrWriteEvidence
    authority_mode: str = "attended_only"
    autonomous_authority: VerifiedIbkrAutonomousAuthority | None = None
    autonomous_authority_bindings: IbkrAutonomousAuthorityBindings | None = None
    autonomous_policy_receipt: VerifiedIbkrAutonomousPolicyReceipt | None = None
    autonomous_policy_receipt_bindings: (
        IbkrAutonomousPolicyReceiptBindings | None
    ) = None
    service_writer_lock: AccountWriterLock | None = None
    plan_sealer: Any | None = None
    owned_resource: Any | None = None
    connect_command_session: bool = True
    authorize_command_writes: bool = True

    def __post_init__(self) -> None:
        for name in (
            "plan_reader",
            "risk_policy_check",
            "acceptance_verifier",
            "mutation_interlock",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be an injected callable")
        if not isinstance(self.write_evidence, IbkrWriteEvidence):
            raise TypeError("authenticated IBKR write evidence must be injected")
        if type(self.connect_command_session) is not bool:
            raise TypeError("IBKR command-session selection must be boolean")
        if type(self.authorize_command_writes) is not bool:
            raise TypeError("IBKR command write-authority selection must be boolean")
        if self.authorize_command_writes and not self.connect_command_session:
            raise TypeError("IBKR write authority requires a connected command session")
        if self.authority_mode not in {"attended_only", "unattended"}:
            raise TypeError("IBKR command authority mode is invalid")
        if self.authority_mode == "attended_only":
            if not self.connect_command_session or not self.authorize_command_writes:
                raise TypeError(
                    "attended IBKR commands require an authorized command session"
                )
            if any(
                value is not None
                for value in (
                    self.autonomous_authority,
                    self.autonomous_authority_bindings,
                    self.autonomous_policy_receipt,
                    self.autonomous_policy_receipt_bindings,
                    self.service_writer_lock,
                    self.plan_sealer,
                    self.owned_resource,
                )
            ):
                raise TypeError("attended IBKR inputs cannot retain autonomous authority")
        else:
            if (
                not isinstance(
                    self.autonomous_authority,
                    VerifiedIbkrAutonomousAuthority,
                )
                or not isinstance(
                    self.autonomous_authority_bindings,
                    IbkrAutonomousAuthorityBindings,
                )
                or not isinstance(
                    self.autonomous_policy_receipt,
                    VerifiedIbkrAutonomousPolicyReceipt,
                )
                or not isinstance(
                    self.autonomous_policy_receipt_bindings,
                    IbkrAutonomousPolicyReceiptBindings,
                )
                or self.autonomous_policy_receipt.bindings
                != self.autonomous_policy_receipt_bindings
                or any(
                    (
                        self.autonomous_policy_receipt_bindings.release_manifest_hash
                        != self.autonomous_authority_bindings.release_manifest_hash,
                        self.autonomous_policy_receipt_bindings.config_hash
                        != self.autonomous_authority_bindings.config_hash,
                        self.autonomous_policy_receipt_bindings.policy_hash
                        != self.autonomous_authority_bindings.policy_binding_id,
                        self.autonomous_policy_receipt_bindings.account_key
                        != self.autonomous_authority_bindings.account_key,
                        self.autonomous_policy_receipt_bindings.account_masked
                        != self.autonomous_authority_bindings.account_masked,
                        self.autonomous_policy_receipt_bindings.account_binding_fingerprint
                        != self.autonomous_authority_bindings.account_binding_fingerprint,
                        self.autonomous_policy_receipt_bindings.authorization_binding_id
                        != self.autonomous_authority_bindings.authorization_binding_id,
                        self.autonomous_policy_receipt_bindings.provider_contract_id
                        != self.autonomous_authority_bindings.provider_contract_id,
                        self.autonomous_policy_receipt_bindings.transport_id
                        != self.autonomous_authority_bindings.transport_id,
                    )
                )
                or not isinstance(self.service_writer_lock, AccountWriterLock)
                or not callable(self.plan_sealer)
                or self.owned_resource is None
            ):
                raise TypeError("autonomous IBKR inputs are incomplete")


class DeterministicLocalQualityReader(QualityEvidenceRecordReader):
    """Recompute quality evidence from the exact in-memory Massive snapshot.

    Scores are deterministic and deliberately conservative.  Missing context
    maps to zero rather than a fabricated neutral score.  Account-capacity is
    never asserted here because it belongs to the fresh broker risk snapshot.
    Capacity and order-sized depth are explicitly deferred to the pipeline's
    risk and market validation after sizing; neither is silently marked true.
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
        quote_age = (current - quote.oldest_venue_at).total_seconds()
        newest_quote_age = (current - quote.newest_venue_at).total_seconds()
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
                "fresh_executable_quote": (
                    newest_quote_age >= 0
                    and quote_age <= self.quote_max_age_seconds
                ),
                "broker_tradable": quote.tradable,
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
                        quote.venue_bid_at.isoformat(),
                        quote.venue_ask_at.isoformat(),
                        quote.observed_at.isoformat(),
                    ),
                    "bar": bar.digest,
                    "sampled_at": snapshot.sampled_at.isoformat(),
                    "deferred_hard_gate_facts": sorted(POST_SIZING_HARD_GATE_FACTS),
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
            "deferred_hard_gate_facts": sorted(POST_SIZING_HARD_GATE_FACTS),
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
        full_live_config_name: str = "full_live.json",
        ibkr_runtime_factory: Any | None = None,
        ibkr_command_inputs: IbkrCommandAssemblyInputs | None = None,
    ) -> None:
        self.release_root = Path(release_root).expanduser().resolve()
        self.install_root = Path(install_root).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.keychain = keychain or MacOSKeychain()
        self.profile = self._load_json(
            self.release_root / "config/provider_bindings.json",
            expected_schema=PROVIDER_PROFILE_SCHEMA,
        )
        if (
            not isinstance(full_live_config_name, str)
            or not re.fullmatch(r"full_live(?:_[a-z0-9_-]+)?\.json", full_live_config_name)
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_FULL_LIVE_CONFIG_INVALID")
        self.full_live = self._load_json(
            self.release_root / "config" / full_live_config_name,
            expected_schema="titan_full_live_config_2026-09-08_v1",
        )
        self._full_live_config_name = full_live_config_name
        self.calendar = ExchangeCalendar.from_json(
            self.release_root / "config/nyse_calendar_2026.json"
        )
        self._massive_source: MassiveRestStreamSource | None = None
        self._gmail: GmailProviderBinding | None = None
        self._ibkr_runtime_factory = ibkr_runtime_factory
        if ibkr_command_inputs is not None and not isinstance(
            ibkr_command_inputs, IbkrCommandAssemblyInputs
        ):
            raise TypeError("IBKR command assembly inputs are invalid")
        self._ibkr_command_inputs = ibkr_command_inputs
        self._ibkr_runtime: object | None = None
        self._ibkr_transport: IbkrProductionTransport | None = None
        self._ibkr_reconciliation_transport: IbkrReconciliationTransport | None = None
        self._ibkr_ledger: IbkrExecutionLedger | None = None
        self._ibkr_risk_snapshot_reader: (
            IbkrRiskEvidenceAccountSnapshotReader | None
        ) = None

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

    def _ibkr_managed_control_authority(
        self,
        *,
        ibkr_profile: IbkrLocalProviderProfile,
        execution: Mapping[str, Any],
    ) -> tuple[bytes, str]:
        """Load the IBKR-only managed-control HMAC key from Keychain.

        The checked-in binding is only a public credential locator.  The
        secret is loaded into the in-memory runtime composition and is never
        persisted.  Requiring a distinct service and account label prevents
        the older ending-7153 control credential from silently authorizing
        this IBKR deployment.
        """

        raw = self.profile.get("ibkr_control")
        required_fields = {
            "implementation_id",
            "credential_backend",
            "authentication_key_service",
            "credential_account",
            "account_key",
            "authorization_binding_source",
        }
        if not isinstance(raw, Mapping) or set(raw) != required_fields:
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_PROFILE_INVALID"
            )
        if (
            raw.get("implementation_id")
            != "titan.hmac_control.ibkr.keychain.v1"
            or raw.get("credential_backend") != "macos_keychain"
            or raw.get("authorization_binding_source")
            != "signed_execution.production_authorization_binding_id"
            or str(raw.get("account_key", "")) != ibkr_profile.account_key
            or str(raw.get("credential_account", "")) != ibkr_profile.account_key
            or ibkr_profile.account_key == "ending-7153"
            or execution.get("production_transport_id") != IBKR_TRANSPORT_ID
        ):
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_PROFILE_INVALID"
            )

        legacy = self.profile.get("control")
        if isinstance(legacy, Mapping) and (
            str(raw.get("authentication_key_service", ""))
            == str(legacy.get("authentication_key_service", ""))
            or str(raw.get("credential_account", ""))
            == str(legacy.get("credential_account", ""))
        ):
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_PROFILE_INVALID"
            )
        try:
            item = self._item(raw, "authentication_key_service")
        except (LocalAssemblyError, TypeError, ValueError):
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_PROFILE_INVALID"
            ) from None
        try:
            key = bytes(self.keychain.read(item))
        except Exception:
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_KEY_UNAVAILABLE"
            ) from None
        if not 32 <= len(key) <= 4096 or b"\x00" in key:
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_KEY_INVALID"
            )

        authorization_binding = str(
            execution.get("production_authorization_binding_id", "")
        )
        if not _SHA256.fullmatch(authorization_binding):
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_BINDING_INVALID"
            )
        return key, authorization_binding

    def ibkr_profile(self) -> IbkrLocalProviderProfile | None:
        try:
            return IbkrLocalProviderProfile.from_config(self.full_live)
        except ProviderProfileError as exc:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_PROFILE_INVALID") from exc

    def ibkr_runtime(self) -> object:
        """Return the cached profile-selected runtime without opening a socket."""
        if self._ibkr_runtime is not None:
            return self._ibkr_runtime
        profile = self.ibkr_profile()
        if profile is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_NOT_SELECTED")
        try:
            validate_installed_sdk(self.install_root, profile)
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_SDK_NOT_ATTESTED") from None
        factory = self._ibkr_runtime_factory
        if factory is None:
            try:
                from .broker.ibkr_runtime import build_ibkr_official_runtime
            except (ImportError, AttributeError):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_OFFICIAL_RUNTIME_UNAVAILABLE"
                ) from None
            factory = build_ibkr_official_runtime
        if not callable(factory):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_RUNTIME_FACTORY_INVALID")
        runtime_profile = profile
        if (
            self._ibkr_command_inputs is not None
            and self._ibkr_command_inputs.authority_mode == "attended_only"
        ):
            runtime_profile = profile.for_attended_command()
        try:
            runtime = factory(
                profile=runtime_profile,
                install_root=self.install_root,
                clock=self._clock,
            )
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_RUNTIME_CONSTRUCTION_FAILED") from None
        for member in ("connect_reads", "status", "stop"):
            if not callable(getattr(runtime, member, None)):
                raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_RUNTIME_CONTRACT_INVALID")
        self._ibkr_runtime = runtime
        return runtime

    def ibkr_read_components(self) -> object:
        """Connect/authenticate the read lane and return its redacted bundle."""
        runtime = self.ibkr_runtime()
        try:
            try:
                components = runtime.components
            except Exception:
                components = runtime.connect_reads()
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_READ_CONNECTION_FAILED") from None
        read_bridge = getattr(components, "read_bridge", None)
        instruments = getattr(components, "instrument_provider", None)
        if read_bridge is None or instruments is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_READ_COMPONENTS_INCOMPLETE")
        return components

    def attended_runtime(self) -> object:
        """Expose attended control only after a future signed supported selection."""
        if self.full_live["execution"].get("broker_adapter") != "supported_production_transport":
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_TRANSPORT_STAGED_ONLY")
        self.ibkr_production_transport()
        runtime = self.ibkr_runtime()
        facade = getattr(runtime, "attended_runtime", None)
        if not callable(facade):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ATTENDED_RUNTIME_UNAVAILABLE")
        try:
            return facade()
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ATTENDED_RUNTIME_UNAVAILABLE") from None

    def ibkr_reconciliation_transport(self) -> IbkrReconciliationTransport:
        """Build the command-free exhaustive coordinator read transport."""

        if self._ibkr_reconciliation_transport is not None:
            return self._ibkr_reconciliation_transport
        execution = self.full_live["execution"]
        if (
            execution.get("broker_adapter") != "supported_production_transport"
            or execution.get("production_transport_id") != IBKR_TRANSPORT_ID
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_SUPPORTED_READS_NOT_SELECTED")
        authorization_binding = str(
            execution.get("production_authorization_binding_id", "")
        )
        account_binding = str(
            execution.get("production_account_binding_fingerprint", "")
        )
        if not all(_SHA256.fullmatch(value) for value in (authorization_binding, account_binding)):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_SIGNED_BINDINGS_INVALID")
        runtime = self.ibkr_runtime()
        components = self.ibkr_read_components()
        reader = getattr(components, "account_snapshot_reader", None)
        reads = getattr(components, "read_bridge", None)
        if not callable(reader):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_STABLE_ACCOUNT_READER_UNAVAILABLE")
        try:
            snapshot = reader()
            descriptor = runtime.attended_descriptor(
                authorization_binding_id=authorization_binding,
                coverage=reader.coverage,
            )
            transport = IbkrReconciliationTransport(
                descriptor=descriptor,
                reads=reads,
                stable_reader=reader,
            )
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_RECONCILIATION_TRANSPORT_FAILED") from None
        if (
            snapshot.account_masked != descriptor.capabilities.account_masked
            or descriptor.account_binding_fingerprint != account_binding
            or descriptor.authorization_binding_id != authorization_binding
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ACCOUNT_BINDING_INVALID")
        self._ibkr_reconciliation_transport = transport
        return transport

    def ibkr_production_transport(self) -> IbkrProductionTransport:
        """Assemble the exact signed attended or autonomous IBKR command lane."""

        if self._ibkr_transport is not None:
            return self._ibkr_transport
        execution = self.full_live["execution"]
        evidence = self.full_live["evidence"]
        if execution.get("broker_adapter") != "supported_production_transport":
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_TRANSPORT_STAGED_ONLY")
        authority_mode = str(execution.get("execution_authority_mode", ""))
        if authority_mode not in {"attended_only", "unattended"}:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTHORITY_MODE_INVALID")
        attended_mode = authority_mode == "attended_only"
        mode_contract_ready = (
            execution.get("supported_unattended_mutation") is False
            and execution.get("per_mutation_user_confirmation_required") is True
            if attended_mode
            else execution.get("supported_unattended_mutation") is True
            and execution.get("per_mutation_user_confirmation_required") is False
        )
        if (
            execution.get("production_transport_id") != IBKR_TRANSPORT_ID
            or not mode_contract_ready
            or execution.get("local_mutation_interlock_enabled") is not True
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_EXECUTION_POLICY_UNAPPROVED")
        try:
            policy = PolicyBundle.load(
                self.release_root,
                config_relative=f"config/{self._full_live_config_name}",
            )
        except Exception:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_POLICY_INVALID") from None
        if not policy.risk_provenance_verified:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_RISK_PROVENANCE_UNVERIFIED")
        inputs = self._ibkr_command_inputs
        if inputs is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_COMMAND_EVIDENCE_UNAVAILABLE")
        if inputs.authority_mode != authority_mode:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTHORITY_INPUT_MISMATCH")
        if not attended_mode and (
            getattr(inputs.mutation_interlock, "lock", None)
            is not inputs.service_writer_lock
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_LOCK_MISMATCH")
        profile = self.ibkr_profile()
        if profile is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_NOT_SELECTED")
        authorization_binding = str(
            execution.get("production_authorization_binding_id", "")
        )
        account_binding = str(
            execution.get("production_account_binding_fingerprint", "")
        )
        provider_contract = str(execution.get("ibkr_provider_contract_id", ""))
        if not all(
            _SHA256.fullmatch(value)
            for value in (authorization_binding, account_binding, provider_contract)
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_SIGNED_BINDINGS_INVALID")
        relative_ledger = Path(str(execution.get("ibkr_ledger_relative_path", "")))
        if (
            relative_ledger.is_absolute()
            or not relative_ledger.parts
            or ".." in relative_ledger.parts
            or relative_ledger.suffix != ".sqlite3"
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_LEDGER_PATH_INVALID")
        ledger_path = self.install_root / relative_ledger
        if not ledger_path.parent.is_dir() or ledger_path.is_symlink():
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_LEDGER_PARENT_UNAVAILABLE")
        try:
            existing_order_reserve = Decimal(
                str(execution["ibkr_existing_order_reserve_dollars"])
            )
        except (KeyError, ValueError, InvalidOperation):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ORDER_RESERVE_INVALID") from None
        if not existing_order_reserve.is_finite() or existing_order_reserve <= 0:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ORDER_RESERVE_INVALID")

        runtime = None
        ledger = None
        risk_snapshot_reader = None
        command_lock_acquired = False
        try:
            # Reserve the broker-global command lane before either of the
            # fixed attended client IDs is connected.  This serializes review
            # and confirm processes as one short-lived command session; the
            # same interlock is revalidated immediately at every SDK write.
            if attended_mode:
                try:
                    inputs.mutation_interlock()
                    command_lock_acquired = True
                except Exception:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_COMMAND_SESSION_INTERLOCK_UNAVAILABLE"
                    ) from None

            runtime = self.ibkr_runtime()
            components = self.ibkr_read_components()
            snapshot_reader = getattr(components, "account_snapshot_reader", None)
            if not callable(snapshot_reader):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_STABLE_ACCOUNT_READER_UNAVAILABLE"
                )
            if not attended_mode:
                authority_bindings = inputs.autonomous_authority_bindings
                if not isinstance(
                    authority_bindings, IbkrAutonomousAuthorityBindings
                ):
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_BINDINGS_INVALID"
                    )
                baseline_relative = Path(
                    str(execution.get("ibkr_daily_risk_baseline_relative_path", ""))
                )
                high_water_relative = Path(
                    str(execution.get("ibkr_risk_high_water_ledger_relative_path", ""))
                )
                if (
                    execution.get("ibkr_daily_risk_baseline_schema")
                    != IBKR_DAILY_RISK_BASELINE_SCHEMA
                    or execution.get("ibkr_daily_risk_baseline_key_source")
                    != "macos_keychain"
                    or baseline_relative.is_absolute()
                    or baseline_relative.parent != Path("control/ibkr")
                    or baseline_relative.suffix != ".json"
                    or high_water_relative.is_absolute()
                    or high_water_relative.parent != Path("state")
                    or high_water_relative.suffix != ".sqlite3"
                    or ".." in baseline_relative.parts
                    or ".." in high_water_relative.parts
                    or high_water_relative == relative_ledger
                    or high_water_relative
                    != IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH
                ):
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_RISK_EVIDENCE_PATH_INVALID"
                    )
                baseline_path = self.install_root / baseline_relative
                # Configuration must attest the canonical location, but it
                # never selects the durable high-water database filename.
                high_water_path = (
                    self.install_root
                    / IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH
                )
                try:
                    baseline_path.parent.resolve(strict=True)
                    high_water_path.parent.resolve(strict=True)
                    if (
                        baseline_path.parent.resolve(strict=True)
                        != (self.install_root / "control/ibkr").resolve(strict=True)
                        or high_water_path.parent.resolve(strict=True)
                        != (self.install_root / "state").resolve(strict=True)
                    ):
                        raise ValueError("risk-evidence parent mismatch")
                    baseline_key_item = KeychainItem(
                        service=str(
                            execution.get(
                                "ibkr_daily_risk_baseline_key_service", ""
                            )
                        ),
                        account=str(
                            execution.get(
                                "ibkr_daily_risk_baseline_key_account", ""
                            )
                        ),
                    )
                    stable_risk_bindings = IbkrRiskLedgerBindings(
                        release_manifest_hash=(
                            authority_bindings.release_manifest_hash
                        ),
                        config_hash=policy.config_hash,
                        policy_binding_id=policy.policy_hash,
                        risk_binding_id=policy.risk_hash,
                        account_key=policy.account_key,
                        account_masked=authority_bindings.account_masked,
                        account_binding_fingerprint=account_binding,
                    )
                    binding_provider = IbkrDailyRiskBindingProvider(
                        ledger_bindings=stable_risk_bindings,
                        calendar=self.calendar,
                    )
                    authenticator = DailyIbkrRiskBaselineAuthenticator(
                        path=baseline_path,
                        key_reader=self.keychain,
                        key_item=baseline_key_item,
                        expected=binding_provider,
                        clock=IbkrPreflightClock(self._clock),
                        required_schema=policy.daily_risk_baseline_schema,
                    )
                    # Baseline or ledger failure denies only entry-risk
                    # authority.  Keep the exact release-profiled wrapper and
                    # raw TWS reader alive so reconciliation, protection and
                    # exits remain available.  Runtime code never bootstraps a
                    # missing ledger; only the PAUSED installer may do that.
                    risk_failure: IbkrRiskEvidenceError | None = None
                    try:
                        authenticator()
                    except IbkrRiskEvidenceError as exc:
                        risk_failure = exc
                    if risk_failure is None:
                        try:
                            high_water_ledger = IbkrRiskHighWaterLedger(
                                high_water_path,
                                bindings=stable_risk_bindings,
                                allow_create=False,
                            )
                        except IbkrRiskEvidenceError as exc:
                            risk_failure = exc
                    if risk_failure is not None:
                        high_water_ledger = IbkrRiskHighWaterLedger.fail_closed(
                            high_water_path,
                            bindings=stable_risk_bindings,
                            error=risk_failure,
                        )
                    risk_snapshot_reader = IbkrRiskEvidenceAccountSnapshotReader(
                        snapshot_reader=snapshot_reader,
                        enricher=IbkrRiskEvidenceEnricher(
                            baseline_authenticator=authenticator,
                            high_water_ledger=high_water_ledger,
                            snapshot_max_age_seconds=float(
                                evidence["broker_snapshot_max_age_seconds"]
                            ),
                        ),
                    )
                    snapshot_reader = risk_snapshot_reader
                except LocalAssemblyError:
                    raise
                except Exception:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_RISK_EVIDENCE_UNAVAILABLE"
                    ) from None
            try:
                snapshot = snapshot_reader()
                coverage = snapshot_reader.coverage
            except Exception:
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_STABLE_ACCOUNT_READ_FAILED"
                ) from None
            if (
                snapshot.account_masked != f"****{profile.account_last4}"
                or not snapshot.whole_broker_reconciled
                or runtime.account_binding_fingerprint != account_binding
            ):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_ACCOUNT_BINDING_INVALID"
                )
            descriptor_factory = getattr(
                runtime,
                "attended_descriptor" if attended_mode else "autonomous_descriptor",
                None,
            )
            if not callable(descriptor_factory):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_DESCRIPTOR_FACTORY_UNAVAILABLE"
                )
            try:
                descriptor_arguments: dict[str, object] = {
                    "authorization_binding_id": authorization_binding,
                    "coverage": coverage,
                }
                if not attended_mode:
                    descriptor_arguments.update(
                        authority=inputs.autonomous_authority,
                        authority_bindings=inputs.autonomous_authority_bindings,
                        now=_aware(self._clock(), "IBKR autonomous descriptor clock"),
                    )
                descriptor = descriptor_factory(**descriptor_arguments)
            except Exception:
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_DESCRIPTOR_UNAVAILABLE"
                ) from None
            if (
                descriptor.transport_id != IBKR_TRANSPORT_ID
                or descriptor.account_binding_fingerprint != account_binding
                or descriptor.authorization_binding_id != authorization_binding
            ):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_DESCRIPTOR_BINDING_MISMATCH"
                )

            write_evidence = inputs.write_evidence
            now = _aware(self._clock(), "IBKR command assembly clock")
            if (
                write_evidence.authorization_binding_id != authorization_binding
                or write_evidence.account_binding_fingerprint != account_binding
                or write_evidence.environment != profile.environment
                or write_evidence.client_id != profile.command_client_id
                or write_evidence.reviewed_contract_id != provider_contract
                or not write_evidence.issued_at <= now < write_evidence.expires_at
            ):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_WRITE_EVIDENCE_INVALID"
                )
            try:
                accepted = inputs.acceptance_verifier(write_evidence)
            except Exception:
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_WRITE_EVIDENCE_REJECTED"
                ) from None
            if accepted is not None:
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_ACCEPTANCE_MUST_RAISE_ON_DENIAL"
                )

            ledger = IbkrExecutionLedger(
                ledger_path,
                account_fingerprint=account_binding,
                environment=profile.environment,
                client_id=profile.command_client_id,
            )
            attended_preflight = IbkrAttendedPreflightBridge(
                account_snapshot=snapshot_reader,
                instruments=getattr(components, "instrument_provider"),
                plan_reader=inputs.plan_reader,
                risk_policy_check=inputs.risk_policy_check,
                session_is_entry_eligible=IbkrRegularHoursEligibility(self.calendar),
                account_masked=snapshot.account_masked,
                command_client_id=profile.command_client_id,
                policy_binding_id=policy.policy_hash,
                provider_contract_id=provider_contract,
                account_max_age_seconds=float(
                    evidence["broker_snapshot_max_age_seconds"]
                ),
                instrument_max_age_seconds=float(
                    evidence["broker_snapshot_max_age_seconds"]
                ),
                review_ttl_seconds=float(evidence["plan_ttl_seconds"]),
                existing_order_reserve=existing_order_reserve,
                plan_reader_role=(
                    "ibkr_attended_plan_reader"
                    if attended_mode
                    else "ibkr_autonomous_plan_reader"
                ),
                clock=IbkrPreflightClock(self._clock),
            )
            preflight = attended_preflight
            if not attended_mode:
                try:
                    preflight = IbkrAutonomousPolicyPreflightBridge(
                        attended=attended_preflight,
                        authority=inputs.autonomous_authority,
                        expected_bindings=inputs.autonomous_authority_bindings,
                        cancel_ttl_seconds=float(evidence["plan_ttl_seconds"]),
                    )
                except Exception:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_PREFLIGHT_INVALID"
                    ) from None
            authority = IbkrDispatchAuthority(
                ledger=ledger,
                authorization_binding_id=authorization_binding,
                provider_contract_id=provider_contract,
                policy_binding_id=policy.policy_hash,
                verify_acceptance=inputs.acceptance_verifier,
                revalidate=preflight.revalidate,
                authorize_cancel=preflight.authorize_cancel,
                clock=self._clock,
            )
            session_factory = (
                runtime.connect_command
                if inputs.connect_command_session
                else getattr(runtime, "prepare_disconnected_command", None)
            )
            if not callable(session_factory):
                raise LocalAssemblyError(
                    "LOCAL_ASSEMBLY_IBKR_COMMAND_SESSION_FACTORY_UNAVAILABLE"
                )
            session = session_factory(
                mutation_interlock=inputs.mutation_interlock,
                authorize_dispatch=authority,
            )
            if inputs.authorize_command_writes:
                session.authorize_writes(write_evidence)
            transport = IbkrProductionTransport(
                descriptor=descriptor,
                session=session,
                ledger=ledger,
                reads=getattr(components, "read_bridge"),
                preflight=preflight,
                contract_factory=getattr(components, "contract_factory"),
                order_factory=getattr(components, "order_factory"),
                policy_binding_id=policy.policy_hash,
                provider_contract_id=provider_contract,
                authority=authority,
                autonomous_authority=(
                    inputs.autonomous_authority if not attended_mode else None
                ),
                autonomous_authority_bindings=(
                    inputs.autonomous_authority_bindings
                    if not attended_mode
                    else None
                ),
                account_snapshot_enricher=(
                    snapshot_reader.enrich if not attended_mode else None
                ),
                clock=self._clock,
            )
            if attended_mode:
                runtime.bind_attended_transport(transport=transport)
        except Exception as exc:
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass
            if ledger is not None:
                ledger.close()
            if risk_snapshot_reader is not None:
                try:
                    risk_snapshot_reader.close()
                except Exception:
                    pass
            if command_lock_acquired:
                close_interlock = getattr(inputs.mutation_interlock, "close", None)
                if callable(close_interlock):
                    try:
                        close_interlock()
                    except Exception:
                        pass
            if isinstance(exc, LocalAssemblyError):
                raise
            raise LocalAssemblyError(
                "LOCAL_ASSEMBLY_IBKR_COMMAND_ASSEMBLY_FAILED"
            ) from None
        self._ibkr_ledger = ledger
        self._ibkr_risk_snapshot_reader = risk_snapshot_reader
        self._ibkr_transport = transport
        return transport

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
            maximum_watched_symbols=int(
                self.full_live["market_data"]["max_active_candidates"]
            ),
        )
        return self._massive_source

    def gmail_binding(self) -> GmailProviderBinding:
        if self._gmail is not None:
            return self._gmail
        try:
            _profile_key, config = select_account_gmail_profile(
                self.profile, self.full_live
            )
        except (TypeError, ValueError):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_GMAIL_ACCOUNT_PROFILE_INVALID") from None
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

    def quality_reader(self) -> DeterministicLocalQualityReader:
        source = self.massive_source()
        evidence = self.full_live["evidence"]
        return DeterministicLocalQualityReader(
            source,
            provider_binding_id=source.rest.authorization.binding_id,
            quote_max_age_seconds=float(evidence["quote_max_age_seconds"]),
            max_spread_bps=(
                Decimal(str(evidence["max_spread_bps"]))
                if evidence.get("max_spread_bps") is not None
                else None
            ),
            minimum_depth_multiple=(
                Decimal(str(evidence["minimum_depth_multiple"]))
                if evidence.get("minimum_depth_multiple") is not None
                else None
            ),
            minimum_session_volume=int(
                self.full_live["scope"]["minimum_session_volume_inclusive"]
            ),
        )

    def service_writer_lock(self) -> AccountWriterLock | None:
        """Return the exact autonomous lock later acquired by ServiceRunner.

        The assembly never acquires it.  Attended/read-only compositions return
        ``None`` so the CLI keeps its existing unprivileged coordinator lock.
        """

        inputs = self._ibkr_command_inputs
        if inputs is None or inputs.authority_mode != "unattended":
            return None
        lock = inputs.service_writer_lock
        if not isinstance(lock, AccountWriterLock):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_LOCK_UNAVAILABLE")
        if getattr(inputs.mutation_interlock, "lock", None) is not lock:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_LOCK_MISMATCH")
        return lock

    def autonomous_plan_sealer(self) -> Any | None:
        """Return the release-bound durable plan sealer for autonomous service."""

        inputs = self._ibkr_command_inputs
        if inputs is None or inputs.authority_mode != "unattended":
            return None
        if not callable(inputs.plan_sealer):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_AUTONOMOUS_PLAN_SEALER_UNAVAILABLE")
        return inputs.plan_sealer

    def control_runtime_composition(self) -> RuntimeComposition:
        """Build only the private, release-bound emergency-control graph.

        Control producers need the IBKR HMAC authority but never broker,
        market-data, or notification clients.  Loading the dedicated Keychain
        item here preserves authenticated managed closeout while keeping every
        provider factory and socket outside the emergency command path.
        """

        execution = self.full_live["execution"]
        if execution.get("broker_adapter") != "supported_production_transport":
            return RuntimeComposition()
        profile = self.ibkr_profile()
        if profile is None:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_NOT_SELECTED")
        if profile.account_key != str(
            self.full_live["account"].get("account_key", "")
        ):
            raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_ACCOUNT_BINDING_INVALID")
        control_key, control_binding = self._ibkr_managed_control_authority(
            ibkr_profile=profile,
            execution=execution,
        )
        return RuntimeComposition(
            control_authentication_key=control_key,
            control_authorization_binding_id=control_binding,
        )

    def notification_runtime_composition(self) -> RuntimeComposition:
        """Build the independent delivery graph without broker dependencies.

        The notification worker and route-test command must coexist with the
        coordinator.  In particular they must not instantiate the fixed-ID
        IBKR read client or the Massive stream merely to obtain the configured
        delivery sink.
        """

        notifications = self.full_live["notifications"]
        gmail = (
            self.gmail_binding()
            if notifications.get("delivery_sink") == "gmail_api"
            else None
        )
        return RuntimeComposition(notification_provider=gmail)

    def coordinator_runtime_composition(self) -> RuntimeComposition:
        """Build the broker safety graph without entry-provider dependencies.

        The persistent coordinator must always be able to reconcile, protect,
        exit, and close out after it has acquired the sole writer authority.
        Gmail and Massive credentials belong to independently gated entry and
        delivery paths, so neither is read while this graph is constructed.
        """

        execution = self.full_live["execution"]
        discovery = self.full_live["discovery"]
        ibkr_profile = self.ibkr_profile()
        if ibkr_profile is not None:
            supported_transport = (
                execution.get("broker_adapter") == "supported_production_transport"
            )
            if not supported_transport:
                if discovery.get("pipeline_configured") is True:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_STAGED_DISCOVERY_DISABLED"
                    )
                return RuntimeComposition()
            control_key, control_binding = self._ibkr_managed_control_authority(
                ibkr_profile=ibkr_profile,
                execution=execution,
            )
            command_lane = self._ibkr_command_inputs is not None
            try:
                production_transport = (
                    self.ibkr_production_transport()
                    if command_lane
                    else self.ibkr_reconciliation_transport()
                )
            except Exception:
                if command_lane:
                    self.close()
                raise
            return RuntimeComposition(
                production_transport=production_transport,
                autonomous_plan_sealer=(
                    self.autonomous_plan_sealer()
                    if command_lane
                    and self._ibkr_command_inputs.authority_mode == "unattended"
                    else None
                ),
                control_authentication_key=control_key,
                control_authorization_binding_id=control_binding,
            )
        if execution.get("broker_adapter") == "supported_production_transport":
            raise LocalAssemblyError("LOCAL_ASSEMBLY_SUPPORTED_BROKER_UNAVAILABLE")
        if discovery.get("pipeline_configured") is True:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_ROBINHOOD_TRADABILITY_UNAVAILABLE")
        return RuntimeComposition()

    def discovery_runtime_composition(self) -> RuntimeComposition:
        """Build the optional entry graph without notification dependencies.

        This graph is constructed only after the coordinator safety graph is
        release-bound.  A missing Massive credential or local data source may
        therefore disable discovery without tearing down the broker lifecycle
        graph that already owns reconciliation and risk reduction.
        """

        execution = self.full_live["execution"]
        discovery = self.full_live["discovery"]
        ibkr_profile = self.ibkr_profile()
        if ibkr_profile is not None:
            if execution.get("broker_adapter") != "supported_production_transport":
                if discovery.get("pipeline_configured") is True:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_STAGED_DISCOVERY_DISABLED"
                    )
                return RuntimeComposition()
            command_lane = self._ibkr_command_inputs is not None
            production_transport = (
                self.ibkr_production_transport()
                if command_lane
                else self.ibkr_reconciliation_transport()
            )
            components = self.ibkr_read_components()
            instrument = IbkrPipelineInstrumentEvidenceProvider(
                getattr(components, "instrument_provider"),
                inventory_native_provider=not command_lane,
            )
            source = self.massive_source()
            provider = SupportedIbkrDiscoveryProviderComposition(
                source=source,
                read_bridge=getattr(components, "read_bridge"),
                instrument_evidence=instrument,
                quality_reader=self.quality_reader(),
                provider_binding_id=source.rest.authorization.binding_id,
                timeout_seconds=float(
                    self.profile["massive"].get("request_timeout_seconds", 3)
                ),
                shared_dependencies_owned_by_transport=True,
            )
            return RuntimeComposition(
                production_transport=production_transport,
                discovery_provider=provider,
                autonomous_plan_sealer=(
                    self.autonomous_plan_sealer()
                    if command_lane
                    and self._ibkr_command_inputs.authority_mode == "unattended"
                    else None
                ),
            )
        if execution.get("broker_adapter") == "supported_production_transport":
            raise LocalAssemblyError("LOCAL_ASSEMBLY_SUPPORTED_BROKER_UNAVAILABLE")
        if discovery.get("pipeline_configured") is True:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_ROBINHOOD_TRADABILITY_UNAVAILABLE")
        return RuntimeComposition()

    def runtime_composition(self) -> RuntimeComposition:
        execution = self.full_live["execution"]
        discovery = self.full_live["discovery"]
        notifications = self.full_live["notifications"]
        ibkr_profile = self.ibkr_profile()
        gmail = (
            self.gmail_binding()
            if notifications.get("delivery_sink") == "gmail_api"
            else None
        )
        if ibkr_profile is not None:
            supported_transport = (
                execution.get("broker_adapter") == "supported_production_transport"
            )
            if not supported_transport:
                # The checked-in IBKR profile is only an installed/staged
                # description.  Starting an ordinary service composition must
                # neither authenticate nor open even the read socket.  An
                # explicit provider-status probe is the only staged network
                # operation, and the attended facade remains unavailable.
                if discovery.get("pipeline_configured") is True:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_STAGED_DISCOVERY_DISABLED"
                    )
                return RuntimeComposition(notification_provider=gmail)
            control_key, control_binding = self._ibkr_managed_control_authority(
                ibkr_profile=ibkr_profile,
                execution=execution,
            )
            command_lane = supported_transport and self._ibkr_command_inputs is not None
            attended_command_lane = bool(
                command_lane
                and self._ibkr_command_inputs.authority_mode == "attended_only"
            )
            if attended_command_lane:
                # Reserve the command lane before ibkr_read_components opens
                # the command-scoped read client ID.  A competing attended
                # process therefore cannot connect either fixed client ID.
                try:
                    self._ibkr_command_inputs.mutation_interlock()
                except Exception:
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_COMMAND_SESSION_INTERLOCK_UNAVAILABLE"
                    ) from None
            try:
                components = self.ibkr_read_components()
                instrument = IbkrPipelineInstrumentEvidenceProvider(
                    getattr(components, "instrument_provider"),
                    inventory_native_provider=not command_lane,
                )
                source = self.massive_source()
                provider = SupportedIbkrDiscoveryProviderComposition(
                    source=source,
                    read_bridge=getattr(components, "read_bridge"),
                    instrument_evidence=instrument,
                    quality_reader=self.quality_reader(),
                    provider_binding_id=source.rest.authorization.binding_id,
                    timeout_seconds=float(
                        self.profile["massive"].get("request_timeout_seconds", 3)
                    ),
                    shared_dependencies_owned_by_transport=True,
                )
                production_transport = (
                    self.ibkr_production_transport()
                    if command_lane
                    else self.ibkr_reconciliation_transport()
                )
            except Exception:
                if command_lane:
                    self.close()
                raise
            return RuntimeComposition(
                production_transport=production_transport,
                discovery_provider=provider,
                notification_provider=gmail,
                autonomous_plan_sealer=(
                    self.autonomous_plan_sealer()
                    if command_lane
                    and self._ibkr_command_inputs.authority_mode == "unattended"
                    else None
                ),
                control_authentication_key=control_key,
                control_authorization_binding_id=control_binding,
            )
        if execution.get("broker_adapter") == "supported_production_transport":
            # No supported standalone Robinhood contract was verified.  Never
            # map the attended Codex OAuth session into a daemon credential.
            raise LocalAssemblyError("LOCAL_ASSEMBLY_SUPPORTED_BROKER_UNAVAILABLE")
        if discovery.get("pipeline_configured") is True:
            raise LocalAssemblyError("LOCAL_ASSEMBLY_ROBINHOOD_TRADABILITY_UNAVAILABLE")
        return RuntimeComposition(notification_provider=gmail)

    def _ibkr_connections(
        self, *, probe_network: bool, checked_at: datetime
    ) -> list[ProviderConnection]:
        profile = self.ibkr_profile()
        if profile is None:
            return []
        endpoint = f"{profile.host}:{profile.port}"
        implementation = "titan.ibkr.official_tws_api.local_runtime.v1"
        source = "installer-attested-official-tws-api"
        try:
            runtime = self.ibkr_runtime()
            probe = None
            if probe_network:
                self.ibkr_read_components()
                probe_method = getattr(runtime, "probe_reads", None)
                if not callable(probe_method):
                    raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_READ_PROBE_UNAVAILABLE")
                probe = probe_method(symbol="SPY")
            status = runtime.status()
            connected = getattr(status, "read_connected", False) is True
            authenticated = getattr(status, "account_authenticated", False) is True
            state = str(getattr(status, "state", "NEW")).upper()
            phase = (
                "BLOCKED"
                if state == "FAILED"
                else "CONNECTED"
                if connected and authenticated
                else "STAGED"
            )
            observed = getattr(status, "last_observed_at", None)
            errors = getattr(status, "error_codes", ())
            error_code = None
            material_errors = tuple(
                item
                for item in errors
                if (
                    isinstance(item, tuple)
                    and len(item) == 4
                    and type(item[2]) is int
                    and item[2] not in _IBKR_INFORMATIONAL_CODES
                )
            )
            if material_errors:
                connection, _request, code, scope = material_errors[-1]
                error_code = (
                    f"IBKR_RUNTIME_{str(connection).upper()}_"
                    f"{str(scope).upper()}_{int(code)}"
                )
            if observed is not None:
                observed = _aware(observed, "IBKR runtime status receipt")
            # Account discovery is intentionally deferred until an authenticated
            # read connection.  Merely constructing the installed runtime must
            # remain reportable as STAGED without asking it to disclose or
            # synthesize an account-binding receipt.
            binding = None
            if connected and authenticated:
                binding = str(
                    getattr(runtime, "account_binding_fingerprint", "")
                )
                if not _SHA256.fullmatch(binding):
                    raise LocalAssemblyError(
                        "LOCAL_ASSEMBLY_IBKR_ACCOUNT_BINDING_INVALID"
                    )
            if probe_network:
                if not connected or not authenticated:
                    raise LocalAssemblyError("LOCAL_ASSEMBLY_IBKR_NOT_AUTHENTICATED")
                probe_phase = getattr(probe, "phase", None)
                probe_connected = getattr(probe, "connected", None)
                probe_authenticated = getattr(probe, "authenticated", None)
                probe_observed = getattr(probe, "observed_at", None)
                account_receipt = getattr(probe, "account_collection_id", None)
                instrument_receipt = getattr(probe, "contract_read_receipt_id", None)
                if instrument_receipt is None:
                    instrument_receipt = getattr(probe, "instrument_evidence_id", None)
                regular_session_open = getattr(probe, "contract_regular_session_open", None)
                probe_error_code = getattr(probe, "error_code", None)
                if isinstance(probe, Mapping):
                    probe_phase = probe.get("phase", probe_phase)
                    probe_connected = probe.get("connected", probe_connected)
                    probe_authenticated = probe.get(
                        "authenticated", probe_authenticated
                    )
                    probe_observed = probe.get("observed_at", probe_observed)
                    account_receipt = probe.get(
                        "account_collection_id", account_receipt
                    )
                    instrument_receipt = (
                        probe.get("contract_read_receipt_id")
                        or probe.get("instrument_evidence_id", instrument_receipt)
                    )
                    regular_session_open = probe.get(
                        "contract_regular_session_open", regular_session_open
                    )
                    probe_error_code = probe.get("error_code", probe_error_code)
                if (
                    probe_phase != "CONNECTED"
                    or probe_connected is not True
                    or probe_authenticated is not True
                    or not isinstance(probe_observed, datetime)
                    or probe_observed.tzinfo is None
                    or not isinstance(account_receipt, str)
                    or not account_receipt
                    or not isinstance(instrument_receipt, str)
                    or not instrument_receipt
                ):
                    safe_probe_error = "LOCAL_ASSEMBLY_IBKR_READ_PROBE_INCOMPLETE"
                    # The runtime may append the fixed API_READ_ONLY reason
                    # only after classifying the provider's validation cause.
                    # Preserve it here; a bare numeric 321 is inconclusive.
                    if isinstance(probe_error_code, str) and re.fullmatch(
                        r"IBKR_RUNTIME_[A-Z0-9_]{1,80}", probe_error_code
                    ):
                        safe_probe_error = (
                            f"LOCAL_ASSEMBLY_{probe_error_code}"
                        )
                    account_complete = bool(
                        isinstance(account_receipt, str) and account_receipt
                    )
                    contract_complete = bool(
                        isinstance(instrument_receipt, str) and instrument_receipt
                    )
                    successful = observed
                    return [
                        ProviderConnection(
                            component="ibkr_gateway_runtime",
                            implementation_id=implementation,
                            credential_source_label=source,
                            provider_binding_id=binding,
                            endpoint=endpoint,
                            status="CONNECTED",
                            authenticated=True,
                            last_successful_check=successful,
                            check_kind="authenticated_managed_account_discovery",
                            error_code=None,
                            action_required=(
                                "resolve account and contract read blockers before activation"
                            ),
                            account_or_destination_binding=(
                                f"ending-{profile.account_last4}"
                            ),
                        ),
                        ProviderConnection(
                            component="ibkr_whole_account_read",
                            implementation_id=implementation,
                            credential_source_label=source,
                            provider_binding_id=binding,
                            endpoint=endpoint,
                            status=("CONNECTED" if account_complete else "BLOCKED"),
                            authenticated=account_complete,
                            last_successful_check=(successful if account_complete else None),
                            check_kind="authenticated_bounded_account_callback_collection",
                            error_code=(None if account_complete else safe_probe_error),
                            action_required=(
                                "verify cross-client and historical coverage before activation"
                                if account_complete
                                else "resolve the reported account callback failure and rerun the probe"
                            ),
                            account_or_destination_binding=(
                                f"ending-{profile.account_last4}"
                            ),
                        ),
                        ProviderConnection(
                            component="ibkr_contract_details",
                            implementation_id=implementation,
                            credential_source_label=source,
                            provider_binding_id=binding,
                            endpoint=endpoint,
                            status=("CONNECTED" if contract_complete else "BLOCKED"),
                            authenticated=contract_complete,
                            last_successful_check=(successful if contract_complete else None),
                            check_kind="authenticated_contract_details_read",
                            error_code=(
                                None
                                if contract_complete
                                else (
                                    safe_probe_error
                                    if account_complete
                                    else "LOCAL_ASSEMBLY_IBKR_CONTRACT_READ_NOT_REACHED"
                                )
                            ),
                            action_required=(
                                None
                                if contract_complete
                                else (
                                    "resolve the reported contract metadata failure and rerun the probe"
                                    if account_complete
                                    else "resolve the account-read blocker and rerun the contract probe"
                                )
                            ),
                            account_or_destination_binding=(
                                f"ending-{profile.account_last4}"
                            ),
                        ),
                    ]
                display_status = "CONNECTED"
                check_kind = "authenticated_bounded_account_and_contract_metadata_read"
                successful = _aware(probe_observed, "IBKR read probe receipt")
            else:
                display_status = "STAGED" if phase == "STAGED" else (
                    "CONNECTED" if connected else "BLOCKED"
                )
                check_kind = "installed_runtime_without_socket_probe"
                successful = observed if connected else None
            return [
                ProviderConnection(
                    component=component,
                    implementation_id=implementation,
                    credential_source_label=source,
                    provider_binding_id=binding,
                    endpoint=endpoint,
                    status=display_status,
                    authenticated=(authenticated if display_status == "CONNECTED" else False),
                    last_successful_check=successful,
                    check_kind=(
                        "authenticated_bounded_account_callback_collection"
                        if probe_network and component == "ibkr_whole_account_read"
                        else "authenticated_contract_metadata_read"
                        if probe_network and component == "ibkr_contract_details"
                        else check_kind
                    ),
                    error_code=(str(error_code) if error_code else None),
                    action_required=(
                        (
                            "verify cross-client and historical coverage before activation"
                            if component == "ibkr_whole_account_read"
                            else "regular-session eligibility is false; metadata connectivity grants no trading authority"
                            if component == "ibkr_contract_details" and regular_session_open is False
                            else None
                        )
                        if probe_network
                        else "run provider-status --probe-network with TWS or Gateway open"
                    ),
                    account_or_destination_binding=f"ending-{profile.account_last4}",
                )
                for component in (
                    "ibkr_gateway_runtime",
                    "ibkr_whole_account_read",
                    "ibkr_contract_details",
                )
            ]
        except Exception as exc:
            return [
                ProviderConnection(
                    component="ibkr_gateway_read_and_contracts",
                    implementation_id=implementation,
                    credential_source_label=source,
                    provider_binding_id=None,
                    endpoint=endpoint,
                    status="BLOCKED",
                    authenticated=False,
                    last_successful_check=None,
                    check_kind=(
                        "authenticated_bounded_account_and_contract_metadata_read"
                        if probe_network
                        else "installed_runtime_without_socket_probe"
                    ),
                    error_code=_safe_error(exc),
                    action_required=(
                        "install the attested official SDK and open the approved local TWS/Gateway read lane"
                    ),
                    account_or_destination_binding=f"ending-{profile.account_last4}",
                )
            ]

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

        if self.ibkr_profile() is not None:
            connections.extend(
                self._ibkr_connections(
                    probe_network=probe_network, checked_at=checked_at
                )
            )
        else:
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
                error_code=(
                    "FRESH_BROKER_CAPACITY_AND_QUALITY_JOIN_REQUIRED"
                    if self.full_live["discovery"].get("score_policy") == "ranking_only"
                    else "OWNER_QUALITY_THRESHOLDS_AND_BROKER_CAPACITY_JOIN_REQUIRED"
                ),
                action_required="bind the approved policy to fresh broker capacity at the final gate",
            )
        )

        notifications = self.full_live["notifications"]
        if (
            notifications.get("delivery_sink") != "gmail_api"
            and notifications.get("intended_delivery_sink") != "gmail_api"
        ):
            connections.append(
                ProviderConnection(
                    component="codex_heartbeat_notification",
                    implementation_id="titan.codex_heartbeat.local_jsonl_outbox.v1",
                    credential_source_label="none-local-staging",
                    provider_binding_id=None,
                    endpoint=None,
                    status=(
                        "AVAILABLE_UNPROBED"
                        if notifications.get("destination_bridge_configured") is True
                        else "NOT_CONFIGURED"
                    ),
                    authenticated=False,
                    last_successful_check=None,
                    check_kind="configuration",
                    error_code=(
                        None
                        if notifications.get("destination_bridge_configured") is True
                        else "CODEX_HEARTBEAT_DESTINATION_BRIDGE_UNPROVEN"
                    ),
                    action_required=(
                        "bind the durable outbox to the active Codex heartbeat and complete a visible delivery test"
                    ),
                    account_or_destination_binding=str(
                        self.full_live["account"]["masked_identifier"]
                    ),
                )
            )
        else:
            try:
                _profile_key, gmail = select_account_gmail_profile(
                    self.profile, self.full_live
                )
            except (TypeError, ValueError):
                raise LocalAssemblyError("LOCAL_ASSEMBLY_GMAIL_ACCOUNT_PROFILE_INVALID") from None
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
            if (
                gmail.get("enabled") is not True
                or notifications.get("delivery_sink") != "gmail_api"
            ):
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
                        error_code=(
                            "GMAIL_DELIVERY_SINK_NOT_CONFIGURED"
                            if notifications.get("delivery_sink") != "gmail_api"
                            else "GMAIL_OWNER_ROUTE_NOT_CONFIGURED"
                        ),
                        action_required=(
                            "owner must authorize durable desktop OAuth gmail.send and approve exact destination"
                        ),
                        account_or_destination_binding=str(
                            self.full_live["account"]["masked_identifier"]
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
        if self._ibkr_runtime is not None:
            try:
                self._ibkr_runtime.stop()
            except Exception:
                pass
        if self._ibkr_ledger is not None:
            try:
                self._ibkr_ledger.close()
            except Exception:
                pass
            self._ibkr_ledger = None
        if self._ibkr_risk_snapshot_reader is not None:
            try:
                self._ibkr_risk_snapshot_reader.close()
            except Exception:
                pass
            self._ibkr_risk_snapshot_reader = None
        self._ibkr_transport = None
        self._ibkr_reconciliation_transport = None
        if self._ibkr_command_inputs is not None:
            close = getattr(self._ibkr_command_inputs.mutation_interlock, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            resource_close = getattr(
                self._ibkr_command_inputs.owned_resource,
                "close",
                None,
            )
            if callable(resource_close):
                try:
                    resource_close()
                except Exception:
                    pass
        if self._massive_source is not None:
            self._massive_source.close(wait=False)
            close = getattr(self._massive_source.stream, "close", None)
            if callable(close):
                close()


__all__ = [
    "CONNECTION_REPORT_SCHEMA",
    "CalendarMarketSession",
    "DeterministicLocalQualityReader",
    "IbkrCommandAssemblyInputs",
    "IbkrPreflightClock",
    "IbkrRegularHoursEligibility",
    "LocalAssemblyError",
    "LocalProviderAssembly",
    "ProviderConnection",
]
