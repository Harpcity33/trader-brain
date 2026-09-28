from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from titan_brain.live.composition import RuntimeComposition
from titan_brain.live.broker.ibkr_instrument import IbkrInstrumentProvider
from titan_brain.live.broker.ibkr_read import IbkrWholeAccountReadBridge
from titan_brain.live.discovery_composition import (
    SUPPORTED_IBKR_DISCOVERY_COMPOSITION_ID,
)
from titan_brain.live.local_assembly import (
    DeterministicLocalQualityReader,
    LocalAssemblyError,
    LocalProviderAssembly,
    ProviderConnection,
)
from titan_brain.live.market_data import CompletedBar, Quote
from titan_brain.live.massive_adapter import (
    MassiveAuthorizationEvidence,
    MassiveSymbolEvidenceSnapshot,
    MassiveSymbolReadiness,
    PreparedStructure,
)
from titan_brain.live.notifications import NotificationDeliveryError
from titan_brain.live.provider_clients import (
    CredentialUnavailable,
    GMAIL_SEND_SCOPE,
    GmailDesktopOAuthAuthorizer,
    KeychainItem,
    KeychainMassiveAuthorizer,
    MacOSKeychain,
    MassiveWebSocketStreamTransport,
)


NOW = datetime(2026, 9, 8, 14, 2, 3, tzinfo=timezone.utc)
MINUTE_END = NOW.replace(second=0, microsecond=0)


class FakeKeychain:
    def __init__(self, values):
        self.values = dict(values)

    def read(self, item):
        value = self.values[item.service]
        return value if isinstance(value, bytes) else str(value).encode()

    def read_text(self, item):
        return self.read(item).decode()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _limit):
        return self.payload


class FakeMassiveAuthorizer:
    def __init__(self):
        self.evidence = MassiveAuthorizationEvidence(
            binding_id="a" * 64,
            credential_source="macos-keychain:test",
            scopes=("stocks:read",),
            authenticated=True,
        )

    def authorize(self, headers):
        return {**headers, "Authorization": "Bearer test-secret"}

    def stream_credential(self):
        return "test-secret"


class FakeSocket:
    def __init__(self, _url, *, timeout_seconds):
        self.timeout_seconds = timeout_seconds
        self.sent = []
        self.closed = False
        self.responses = [
            [{"ev": "status", "status": "connected"}],
            [{"ev": "status", "status": "auth_success"}],
            [
                {
                    "ev": "Q",
                    "sym": "XYZ",
                    "t": int((NOW - timedelta(seconds=1)).timestamp() * 1_000),
                },
                {
                    "ev": "Q",
                    "sym": "OTHER",
                    "t": int((NOW - timedelta(seconds=1)).timestamp() * 1_000),
                },
                {
                    "ev": "AM",
                    "sym": "XYZ",
                    "s": int((MINUTE_END - timedelta(minutes=1)).timestamp() * 1_000),
                    "e": int((MINUTE_END - timedelta(milliseconds=1)).timestamp() * 1_000),
                },
            ],
            None,
        ]

    def connect(self):
        return None

    def close(self):
        self.closed = True

    def send_json(self, value):
        self.sent.append(dict(value))

    def receive_json(self, *, timeout_seconds):
        return self.responses.pop(0) if self.responses else None


class FakeSource:
    def __init__(self, structure, snapshot):
        self.structure = structure
        self.snapshot = snapshot

    def prepared_structures(self, *, now, limit):
        return (self.structure,)[:limit]

    def evidence_snapshot(self, symbol, *, now):
        return self.snapshot


class FakeIbkrRuntime:
    def __init__(self):
        self.connected = False
        self.stopped = False
        self.instrument = object.__new__(IbkrInstrumentProvider)

    @property
    def account_binding_fingerprint(self):
        if not self.connected:
            raise RuntimeError("account not discovered")
        return "a" * 64

    @property
    def components(self):
        if not self.connected:
            raise RuntimeError("not connected")
        return SimpleNamespace(
            read_bridge=object.__new__(IbkrWholeAccountReadBridge),
            instrument_provider=self.instrument,
        )

    def connect_reads(self):
        self.connected = True
        return self.components

    def probe_reads(self, symbol="SPY"):
        self.connected = True
        return SimpleNamespace(
            phase="CONNECTED",
            connected=True,
            authenticated=True,
            observed_at=NOW,
            error_code=None,
            account_collection_id="account-receipt",
            instrument_evidence_id="instrument-receipt",
            symbol=symbol,
        )

    def status(self):
        return SimpleNamespace(
            state="READS_READY" if self.connected else "NEW",
            read_connected=self.connected,
            account_authenticated=self.connected,
            last_observed_at=NOW if self.connected else None,
            error_codes=(),
        )

    def stop(self):
        self.stopped = True


class LocalProviderClientTests(unittest.TestCase):
    def test_keychain_loader_never_includes_secret_or_stderr_in_failure(self):
        item = KeychainItem("titan-test", "owner")
        completed = subprocess.CompletedProcess(
            args=[], returncode=44, stdout=b"", stderr=b"secret metadata"
        )
        with patch("subprocess.run", return_value=completed) as invoked:
            with self.assertRaisesRegex(
                CredentialUnavailable, "CREDENTIAL_KEYCHAIN_ITEM_UNAVAILABLE"
            ) as caught:
                MacOSKeychain().read(item)
        self.assertEqual(
            invoked.call_args.args[0],
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "titan-test",
                "-a",
                "owner",
                "-w",
            ],
        )
        self.assertNotIn("metadata", str(caught.exception))

    def test_massive_authorizer_uses_keychain_and_exposes_only_stable_binding(self):
        secret = "massive-private-key"
        authorizer = KeychainMassiveAuthorizer(
            FakeKeychain({"massive": secret}), KeychainItem("massive")
        )
        headers = authorizer.authorize({"Accept": "application/json"})
        self.assertEqual(headers["Authorization"], "Bearer " + secret)
        serialized = json.dumps(authorizer.evidence.__dict__, default=str)
        self.assertNotIn(secret, serialized)
        self.assertEqual(authorizer.evidence.credential_source, "macos-keychain:massive")

    def test_massive_stream_authenticates_and_subscribes_only_bounded_active_set(self):
        sockets = []

        def factory(url, *, timeout_seconds):
            result = FakeSocket(url, timeout_seconds=timeout_seconds)
            sockets.append(result)
            return result

        stream = MassiveWebSocketStreamTransport(
            FakeMassiveAuthorizer(),
            websocket_factory=factory,
            clock=lambda: NOW,
        )
        stream.set_symbols(("XYZ",))
        events = stream.drain(limit=10, timeout_seconds=0.1)
        self.assertEqual([item["sym"] for item in events], ["XYZ", "XYZ"])
        self.assertEqual(sockets[0].sent[0], {"action": "auth", "params": "test-secret"})
        self.assertEqual(
            sockets[0].sent[1],
            {"action": "subscribe", "params": "Q.XYZ,AM.XYZ"},
        )
        status = stream.status(now=NOW)
        self.assertTrue(status.authenticated)
        self.assertTrue(status.snapshot_resynced)
        self.assertEqual(status.latest_completed_bar_at, MINUTE_END)
        stream.set_symbols(("ABC",))
        self.assertEqual(
            sockets[0].sent[-2:],
            [
                {"action": "unsubscribe", "params": "Q.XYZ,AM.XYZ"},
                {"action": "subscribe", "params": "Q.ABC,AM.ABC"},
            ],
        )

    def test_massive_stream_health_rejects_partial_or_missing_minute_end(self):
        class PartialSocket(FakeSocket):
            def __init__(self, url, *, timeout_seconds):
                super().__init__(url, timeout_seconds=timeout_seconds)
                start = int(
                    (MINUTE_END - timedelta(minutes=1)).timestamp() * 1_000
                )
                self.responses = [
                    [{"ev": "status", "status": "connected"}],
                    [{"ev": "status", "status": "auth_success"}],
                    [
                        {"ev": "AM", "sym": "XYZ", "s": start},
                        {
                            "ev": "AM",
                            "sym": "XYZ",
                            "s": start,
                            "e": int((NOW - timedelta(seconds=20)).timestamp() * 1_000),
                        },
                        {
                            "ev": "AM",
                            "sym": "XYZ",
                            "s": int(MINUTE_END.timestamp() * 1_000),
                            "e": int(
                                (
                                    MINUTE_END
                                    + timedelta(minutes=1)
                                    - timedelta(milliseconds=1)
                                ).timestamp()
                                * 1_000
                            ),
                        },
                    ],
                    None,
                ]

        stream = MassiveWebSocketStreamTransport(
            FakeMassiveAuthorizer(),
            websocket_factory=PartialSocket,
            clock=lambda: NOW,
        )
        stream.set_symbols(("XYZ",))
        self.assertEqual(len(stream.drain(limit=10, timeout_seconds=0.1)), 3)
        self.assertIsNone(stream.status(now=NOW).latest_completed_bar_at)
        stream.close()

    def test_gmail_desktop_authorizer_refreshes_and_allows_only_send_scope(self):
        client = {
            "installed": {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "client-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        keychain = FakeKeychain(
            {
                "client": json.dumps(client),
                "refresh": "refresh-secret",
                "consent": "production",
            }
        )
        requests = []

        def opener(request, *, timeout):
            requests.append((request, timeout))
            return FakeResponse(
                json.dumps(
                    {
                        "access_token": "ephemeral-access-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": GMAIL_SEND_SCOPE,
                    }
                ).encode()
            )

        auth = GmailDesktopOAuthAuthorizer(
            keychain,
            client_item=KeychainItem("client"),
            refresh_token_item=KeychainItem("refresh"),
            consent_status_item=KeychainItem("consent"),
            opener=opener,
            clock=lambda: NOW,
        )
        headers = auth.authorize({"Accept": "application/json"})
        self.assertEqual(headers["Authorization"], "Bearer ephemeral-access-token")
        self.assertEqual(len(requests), 1)
        posted = parse_qs(requests[0][0].data.decode())
        self.assertEqual(posted["grant_type"], ["refresh_token"])
        self.assertNotIn("refresh-secret", json.dumps(auth.evidence.__dict__, default=str))

    def test_gmail_refresh_rejects_an_expanded_scope(self):
        client = {
            "installed": {
                "client_id": "id",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        auth = GmailDesktopOAuthAuthorizer(
            FakeKeychain(
                {
                    "client": json.dumps(client),
                    "refresh": "refresh",
                    "consent": "production",
                }
            ),
            client_item=KeychainItem("client"),
            refresh_token_item=KeychainItem("refresh"),
            consent_status_item=KeychainItem("consent"),
            opener=lambda *_args, **_kwargs: FakeResponse(
                json.dumps(
                    {
                        "access_token": "token",
                        "expires_in": 3600,
                        "scope": GMAIL_SEND_SCOPE + " https://mail.google.com/",
                    }
                ).encode()
            ),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(
            NotificationDeliveryError, "NOTIFICATION_GMAIL_OAUTH_GRANT_INVALID"
        ):
            auth.authorize({})


class LocalAssemblyTests(unittest.TestCase):
    def test_stock_launcher_assembly_is_safe_when_live_providers_are_disabled(self):
        root = Path(__file__).resolve().parents[1]
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({}),
            clock=lambda: NOW,
        )
        self.assertIsInstance(assembly.runtime_composition(), RuntimeComposition)
        assembly.close()

    def test_notification_composition_never_constructs_broker_or_market_data(self):
        root = Path(__file__).resolve().parents[1]
        client = {
            "installed": {
                "client_id": "client-id.apps.googleusercontent.com",
                "client_secret": "client-secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        keychain = FakeKeychain(
            {
                "titan-full-live-ibkr-ending-3103-gmail-desktop-client": json.dumps(client),
                "titan-full-live-ibkr-ending-3103-gmail-refresh-token": "refresh-secret",
                "titan-full-live-ibkr-ending-3103-gmail-consent-status": "production",
                "titan-full-live-ibkr-ending-3103-gmail-destination": "owner@example.com",
                "titan-full-live-ibkr-ending-3103-gmail-sender": "sender@example.com",
            }
        )
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=keychain,
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
        )
        assembly.full_live["notifications"]["delivery_sink"] = "gmail_api"
        assembly.profile["ibkr_gmail"]["enabled"] = True
        with patch.object(
            assembly,
            "ibkr_read_components",
            side_effect=AssertionError("notification graph reached IBKR"),
        ) as ibkr, patch.object(
            assembly,
            "massive_source",
            side_effect=AssertionError("notification graph reached Massive"),
        ) as massive:
            composition = assembly.notification_runtime_composition()
        self.assertIsInstance(composition, RuntimeComposition)
        self.assertIsNotNone(composition.notification_provider)
        self.assertIsNone(composition.production_transport)
        self.assertIsNone(composition.discovery_provider)
        ibkr.assert_not_called()
        massive.assert_not_called()
        self.assertIsNone(assembly._ibkr_runtime)
        self.assertIsNone(assembly._massive_source)
        assembly.close()

    def test_coordinator_composition_survives_missing_gmail_and_massive_credentials(self):
        root = Path(__file__).resolve().parents[1]
        control_service = (
            "titan-full-live-ibkr-ending-3103-control-authentication-key"
        )
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain(
                {control_service: b"private-emergency-control-key-material-32-bytes"}
            ),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
        )
        execution = assembly.full_live["execution"]
        execution.update(
            {
                "broker_adapter": "supported_production_transport",
                "production_transport_id": "ibkr-tws-api-10.50.2-v1",
                "production_authorization_binding_id": "f" * 64,
            }
        )
        transport = object()
        credential_error = CredentialUnavailable(
            "CREDENTIAL_KEYCHAIN_ITEM_UNAVAILABLE"
        )
        with patch.object(
            assembly, "ibkr_reconciliation_transport", return_value=transport
        ), patch.object(
            assembly, "gmail_binding", side_effect=credential_error
        ) as gmail, patch.object(
            assembly, "massive_source", side_effect=credential_error
        ) as massive, patch.object(
            assembly,
            "ibkr_read_components",
            side_effect=AssertionError("coordinator graph connected discovery reads"),
        ) as discovery_reads:
            composition = assembly.coordinator_runtime_composition()

        self.assertIs(composition.production_transport, transport)
        self.assertIsNone(composition.notification_provider)
        self.assertIsNone(composition.discovery_provider)
        self.assertTrue(composition.managed_control_ready(execution))
        gmail.assert_not_called()
        massive.assert_not_called()
        discovery_reads.assert_not_called()
        self.assertIsNone(assembly._gmail)
        self.assertIsNone(assembly._massive_source)
        assembly.close()

    def test_discovery_composition_surfaces_massive_failure_without_reading_gmail(self):
        root = Path(__file__).resolve().parents[1]
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
        )
        assembly.full_live["execution"]["broker_adapter"] = (
            "supported_production_transport"
        )
        credential_error = CredentialUnavailable(
            "CREDENTIAL_KEYCHAIN_ITEM_UNAVAILABLE"
        )
        with patch.object(
            assembly, "ibkr_reconciliation_transport", return_value=object()
        ), patch.object(
            assembly,
            "ibkr_read_components",
            return_value=SimpleNamespace(
                instrument_provider=object(), read_bridge=object()
            ),
        ), patch(
            "titan_brain.live.local_assembly.IbkrPipelineInstrumentEvidenceProvider",
            return_value=object(),
        ), patch.object(
            assembly, "massive_source", side_effect=credential_error
        ) as massive, patch.object(
            assembly,
            "gmail_binding",
            side_effect=AssertionError("discovery graph read Gmail"),
        ) as gmail:
            with self.assertRaises(CredentialUnavailable):
                assembly.discovery_runtime_composition()

        massive.assert_called_once_with()
        gmail.assert_not_called()
        assembly.close()

    def test_control_composition_loads_only_exact_ibkr_hmac_authority(self):
        root = Path(__file__).resolve().parents[1]
        service = "titan-full-live-ibkr-ending-3103-control-authentication-key"
        keychain = FakeKeychain(
            {service: b"private-emergency-control-key-material-32-bytes"}
        )
        provider_factory = Mock(
            side_effect=AssertionError("control graph constructed IBKR runtime")
        )
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=keychain,
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=provider_factory,
        )
        execution = assembly.full_live["execution"]
        execution.update(
            {
                "broker_adapter": "supported_production_transport",
                "production_transport_id": "ibkr-tws-api-10.50.2-v1",
                "production_authorization_binding_id": "f" * 64,
            }
        )
        with patch.object(
            keychain, "read", wraps=keychain.read
        ) as key_reads, patch.object(
            assembly,
            "gmail_binding",
            side_effect=AssertionError("control graph constructed Gmail"),
        ) as gmail, patch.object(
            assembly,
            "massive_source",
            side_effect=AssertionError("control graph constructed Massive"),
        ) as massive, patch.object(
            assembly,
            "ibkr_read_components",
            side_effect=AssertionError("control graph connected IBKR reads"),
        ) as ibkr_reads:
            composition = assembly.control_runtime_composition()
        self.assertTrue(composition.managed_control_ready(execution))
        self.assertIsNone(composition.production_transport)
        self.assertIsNone(composition.discovery_provider)
        self.assertIsNone(composition.notification_provider)
        provider_factory.assert_not_called()
        gmail.assert_not_called()
        massive.assert_not_called()
        ibkr_reads.assert_not_called()
        key_reads.assert_called_once()
        item = key_reads.call_args.args[0]
        self.assertEqual(item.service, service)
        self.assertEqual(item.account, "ibkr-live-ending-3103")
        self.assertIsNone(assembly._ibkr_runtime)
        self.assertIsNone(assembly._massive_source)
        assembly.close()

    def test_nonnetwork_report_is_redacted_and_records_exact_attended_blocker(self):
        root = Path(__file__).resolve().parents[1]
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
        )
        report = assembly.connection_report(probe_network=False)
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn("private-massive-key", encoded)
        broker = next(
            item
            for item in report["connections"]
            if item["component"] == "robinhood_broker_and_tradability"
        )
        self.assertEqual(
            broker["error_code"], "UNATTENDED_UNSUPPORTED_ON_VERIFIED_ROUTE"
        )
        self.assertEqual(report["broker_mutations_invoked"], [])
        self.assertEqual(report["notification_messages_sent"], [])
        assembly.close()

    def test_provider_connection_never_calls_unprobed_available_authenticated(self):
        with self.assertRaisesRegex(ValueError, "authenticated connection"):
            ProviderConnection(
                component="x",
                implementation_id="x.v1",
                credential_source_label="macos-keychain:x",
                provider_binding_id="a" * 64,
                endpoint=None,
                status="AVAILABLE_UNPROBED",
                authenticated=True,
                last_successful_check=None,
                check_kind="constructor",
                error_code=None,
                action_required=None,
            )

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_staged_ibkr_config_neither_connects_nor_exposes_command_runtime(
        self, _validate_sdk
    ):
        root = Path(__file__).resolve().parents[1]
        runtime = FakeIbkrRuntime()
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return runtime

        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=factory,
        )
        self.assertEqual(assembly.ibkr_profile().account_last4, "3103")
        composition = assembly.runtime_composition()
        self.assertIsInstance(composition, RuntimeComposition)
        self.assertIsNone(composition.production_transport)
        self.assertIsNone(composition.discovery_provider)
        self.assertFalse(runtime.connected)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(LocalAssemblyError, "TRANSPORT_STAGED_ONLY"):
            assembly.attended_runtime()
        assembly.close()
        self.assertFalse(runtime.stopped)

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_ibkr_provider_status_distinguishes_installed_and_authenticated(
        self, _validate_sdk
    ):
        root = Path(__file__).resolve().parents[1]
        runtime = FakeIbkrRuntime()
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=lambda **_kwargs: runtime,
        )
        staged = assembly._ibkr_connections(probe_network=False, checked_at=NOW)
        self.assertEqual({item.status for item in staged}, {"STAGED"})
        self.assertFalse(any(item.authenticated for item in staged))
        self.assertTrue(all(item.provider_binding_id is None for item in staged))
        connected = assembly._ibkr_connections(probe_network=True, checked_at=NOW)
        self.assertEqual({item.status for item in connected}, {"CONNECTED"})
        self.assertTrue(all(item.authenticated for item in connected))
        self.assertEqual(
            {item.component for item in connected},
            {
                "ibkr_gateway_runtime",
                "ibkr_whole_account_read",
                "ibkr_contract_details",
            },
        )
        self.assertFalse(any("DU" in json.dumps(item.public_dict()) for item in connected))
        original_probe = runtime.probe_reads

        def after_hours_probe(symbol="SPY"):
            probe = original_probe(symbol)
            probe.instrument_evidence_id = None
            probe.contract_read_receipt_id = "metadata-receipt"
            probe.contract_regular_session_open = False
            return probe

        runtime.probe_reads = after_hours_probe
        after_hours = assembly._ibkr_connections(probe_network=True, checked_at=NOW)
        self.assertEqual({item.status for item in after_hours}, {"CONNECTED"})
        by_component = {item.component: item for item in after_hours}
        self.assertEqual(
            by_component["ibkr_contract_details"].check_kind,
            "authenticated_contract_metadata_read",
        )
        self.assertIn("eligibility is false", by_component["ibkr_contract_details"].action_required)
        self.assertIn("coverage", by_component["ibkr_whole_account_read"].action_required)
        assembly.close()

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_ibkr_connection_report_uses_ibkr_notification_binding_only(
        self, _validate_sdk
    ):
        root = Path(__file__).resolve().parents[1]
        runtime = FakeIbkrRuntime()
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=lambda **_kwargs: runtime,
        )
        report = assembly.connection_report(probe_network=False)
        components = {item["component"] for item in report["connections"]}
        self.assertIn("gmail_notification", components)
        self.assertNotIn("codex_heartbeat_notification", components)
        self.assertNotIn("robinhood_broker_and_tradability", components)
        notification = next(
            item
            for item in report["connections"]
            if item["component"] == "gmail_notification"
        )
        self.assertEqual(notification["status"], "NOT_CONFIGURED")
        self.assertEqual(
            notification["account_or_destination_binding"], "ending-3103"
        )
        self.assertNotIn("7153", json.dumps(report, sort_keys=True))
        self.assertEqual(notification["error_code"], "GMAIL_DELIVERY_SINK_NOT_CONFIGURED")
        # Selecting an intended sink is not authority to read credentials or
        # refresh OAuth, even if the underlying profile was enabled separately.
        assembly.profile["ibkr_gmail"]["enabled"] = True
        with patch.object(assembly, "gmail_binding", side_effect=AssertionError("must not compose")):
            still_staged = assembly.connection_report(probe_network=False)
        self.assertEqual(next(
            item["status"] for item in still_staged["connections"]
            if item["component"] == "gmail_notification"
        ), "NOT_CONFIGURED")
        assembly.close()

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_ibkr_provider_status_preserves_sanitized_probe_error_code(
        self, _validate_sdk
    ):
        root = Path(__file__).resolve().parents[1]
        runtime = FakeIbkrRuntime()
        runtime.probe_reads = lambda symbol="SPY": SimpleNamespace(
            phase="BLOCKED",
            connected=True,
            authenticated=False,
            observed_at=None,
            error_code="IBKR_RUNTIME_READ_SDK_CALLBACK_321",
            account_collection_id=None,
            instrument_evidence_id=None,
            symbol=symbol,
        )
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=lambda **_kwargs: runtime,
        )
        blocked = assembly._ibkr_connections(probe_network=True, checked_at=NOW)
        self.assertEqual(len(blocked), 3)
        by_component = {item.component: item for item in blocked}
        gateway = by_component["ibkr_gateway_runtime"]
        self.assertEqual(gateway.status, "CONNECTED")
        self.assertTrue(gateway.authenticated)
        whole_account = by_component["ibkr_whole_account_read"]
        self.assertEqual(whole_account.status, "BLOCKED")
        self.assertFalse(whole_account.authenticated)
        self.assertEqual(
            whole_account.error_code,
            "LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_321",
        )
        contracts = by_component["ibkr_contract_details"]
        self.assertEqual(contracts.status, "BLOCKED")
        self.assertEqual(
            contracts.error_code,
            "LOCAL_ASSEMBLY_IBKR_CONTRACT_READ_NOT_REACHED",
        )

        original_probe = runtime.probe_reads

        def classified_probe(symbol="SPY"):
            result = original_probe(symbol)
            result.error_code += "_API_READ_ONLY"
            return result

        runtime.probe_reads = classified_probe
        classified = assembly._ibkr_connections(probe_network=True, checked_at=NOW)
        classified_account = next(
            item for item in classified if item.component == "ibkr_whole_account_read"
        )
        self.assertEqual(classified_account.status, "BLOCKED")
        self.assertEqual(
            classified_account.error_code,
            "LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_321_API_READ_ONLY",
        )
        assembly.close()

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_ibkr_provider_status_preserves_successful_account_component(
        self, _validate_sdk
    ):
        root = Path(__file__).resolve().parents[1]
        runtime = FakeIbkrRuntime()
        runtime.probe_reads = lambda symbol="SPY": SimpleNamespace(
            phase="BLOCKED",
            connected=True,
            authenticated=True,
            observed_at=NOW,
            error_code="IBKR_RUNTIME_CONTRACT_SDK_CALLBACK_200",
            account_collection_id="account-receipt",
            instrument_evidence_id=None,
            symbol=symbol,
        )
        assembly = LocalProviderAssembly(
            release_root=root,
            install_root=root,
            keychain=FakeKeychain({"titan-massive-api": "private-massive-key"}),
            clock=lambda: NOW,
            full_live_config_name="full_live_ibkr.json",
            ibkr_runtime_factory=lambda **_kwargs: runtime,
        )
        blocked = assembly._ibkr_connections(probe_network=True, checked_at=NOW)
        by_component = {item.component: item for item in blocked}
        whole_account = by_component["ibkr_whole_account_read"]
        self.assertEqual(whole_account.status, "CONNECTED")
        self.assertTrue(whole_account.authenticated)
        contracts = by_component["ibkr_contract_details"]
        self.assertEqual(contracts.status, "BLOCKED")
        self.assertFalse(contracts.authenticated)
        self.assertEqual(
            contracts.error_code,
            "LOCAL_ASSEMBLY_IBKR_RUNTIME_CONTRACT_SDK_CALLBACK_200",
        )
        assembly.close()

    def test_deterministic_quality_does_not_invent_capacity_or_full_depth(self):
        structure = PreparedStructure(
            source_plan_id="plan-1",
            symbol="XYZ",
            observed_at=NOW - timedelta(seconds=1),
            setup_id="controlled_base_breakout",
            ranking_score=Decimal("80"),
            entry_limit=Decimal("10.05"),
            structural_stop=Decimal("9.85"),
            targets=(Decimal("10.45"),),
            payload={"trade_authority": False, "broker_authority": False},
            payload_hash="b" * 64,
        )
        quote = Quote.build(
            symbol="XYZ",
            bid="10.00",
            ask="10.02",
            bid_size=500,
            ask_size=600,
            venue_bid_at=NOW - timedelta(seconds=1),
            venue_ask_at=NOW - timedelta(seconds=1),
            observed_at=NOW - timedelta(milliseconds=500),
            source="massive_stream_nbbo_top_of_book+broker_instrument",
            tradable=True,
            size_source_version="massive-shares-effective-2025-11-03",
        )
        bar = CompletedBar.build(
            symbol="XYZ",
            start_at=NOW - timedelta(minutes=1, seconds=3),
            end_at=NOW - timedelta(seconds=3),
            open="9.95",
            high="10.04",
            low="9.94",
            close="10.01",
            volume=800000,
            sequence=1,
            source_event_id="massive:AM:XYZ:1",
        )
        snapshot = MassiveSymbolEvidenceSnapshot(
            sampled_at=NOW,
            symbol="XYZ",
            quote=quote,
            latest_completed_bar=bar,
            readiness=MassiveSymbolReadiness(
                symbol="XYZ",
                phase="READY",
                ready=True,
                quote_received_at=quote.observed_at,
                completed_bar_end=bar.end_at,
                blocker=None,
            ),
        )
        reader = DeterministicLocalQualityReader(
            FakeSource(structure, snapshot),
            provider_binding_id="a" * 64,
            quote_max_age_seconds=5,
            max_spread_bps=Decimal("25"),
            minimum_depth_multiple=Decimal("2"),
            minimum_session_volume=750000,
        )
        result = reader.get_quality_evidence(
            "plan-1", "XYZ", as_of=NOW, timeout_seconds=3
        )
        self.assertFalse(result["hard_gate_facts"]["remaining_capacity"])
        self.assertFalse(result["hard_gate_facts"]["adequate_displayed_depth"])
        self.assertEqual(
            result["deferred_hard_gate_facts"],
            ["adequate_displayed_depth", "remaining_capacity"],
        )
        self.assertEqual(result["shadow_proposal_grants_authority"], False)

    def test_deterministic_quality_ages_quote_by_venue_not_local_receipt(self):
        structure = PreparedStructure(
            source_plan_id="plan-stale",
            symbol="XYZ",
            observed_at=NOW - timedelta(seconds=1),
            setup_id="controlled_base_breakout",
            ranking_score=Decimal("80"),
            entry_limit=Decimal("10.05"),
            structural_stop=Decimal("9.85"),
            targets=(Decimal("10.45"),),
            payload={"trade_authority": False, "broker_authority": False},
            payload_hash="c" * 64,
        )
        quote = Quote.build(
            symbol="XYZ",
            bid="10.00",
            ask="10.02",
            bid_size=500,
            ask_size=600,
            venue_bid_at=NOW - timedelta(seconds=30),
            venue_ask_at=NOW - timedelta(seconds=30),
            observed_at=NOW - timedelta(milliseconds=100),
            source="massive_rest_delayed",
            tradable=True,
            size_source_version="massive-shares-effective-2025-11-03",
        )
        bar = CompletedBar.build(
            symbol="XYZ",
            start_at=NOW - timedelta(minutes=1, seconds=3),
            end_at=NOW - timedelta(seconds=3),
            open="9.95",
            high="10.04",
            low="9.94",
            close="10.01",
            volume=800000,
            sequence=1,
            source_event_id="massive:AM:XYZ:1",
        )
        snapshot = MassiveSymbolEvidenceSnapshot(
            sampled_at=NOW,
            symbol="XYZ",
            quote=quote,
            latest_completed_bar=bar,
            readiness=MassiveSymbolReadiness(
                symbol="XYZ",
                phase="READY",
                ready=True,
                quote_received_at=quote.observed_at,
                completed_bar_end=bar.end_at,
                blocker=None,
            ),
        )
        reader = DeterministicLocalQualityReader(
            FakeSource(structure, snapshot),
            provider_binding_id="a" * 64,
            quote_max_age_seconds=5,
            max_spread_bps=Decimal("25"),
            minimum_depth_multiple=Decimal("2"),
            minimum_session_volume=750000,
        )
        result = reader.get_quality_evidence(
            "plan-stale", "XYZ", as_of=NOW, timeout_seconds=3
        )
        self.assertFalse(result["hard_gate_facts"]["fresh_executable_quote"])

    def test_deterministic_quality_rejects_symbol_not_ready(self):
        structure = PreparedStructure(
            source_plan_id="plan-not-ready",
            symbol="XYZ",
            observed_at=NOW - timedelta(seconds=1),
            setup_id="controlled_base_breakout",
            ranking_score=Decimal("80"),
            entry_limit=Decimal("10.05"),
            structural_stop=Decimal("9.85"),
            targets=(Decimal("10.45"),),
            payload={"trade_authority": False, "broker_authority": False},
            payload_hash="d" * 64,
        )
        quote = Quote.build(
            symbol="XYZ",
            bid="10.00",
            ask="10.02",
            bid_size=500,
            ask_size=600,
            venue_bid_at=NOW - timedelta(seconds=1),
            venue_ask_at=NOW - timedelta(seconds=1),
            observed_at=NOW,
            source="massive_stream",
            tradable=True,
            size_source_version="massive-shares-effective-2025-11-03",
        )
        bar = CompletedBar.build(
            symbol="XYZ",
            start_at=NOW - timedelta(minutes=1, seconds=3),
            end_at=NOW - timedelta(seconds=3),
            open="9.95",
            high="10.04",
            low="9.94",
            close="10.01",
            volume=800000,
            sequence=1,
            source_event_id="massive:AM:XYZ:1",
        )
        snapshot = MassiveSymbolEvidenceSnapshot(
            sampled_at=NOW,
            symbol="XYZ",
            quote=quote,
            latest_completed_bar=bar,
            readiness=MassiveSymbolReadiness(
                symbol="XYZ",
                phase="RESYNC",
                ready=False,
                quote_received_at=quote.observed_at,
                completed_bar_end=bar.end_at,
                blocker="MASSIVE_GAP_RESYNC_PENDING",
            ),
        )
        reader = DeterministicLocalQualityReader(
            FakeSource(structure, snapshot),
            provider_binding_id="a" * 64,
            quote_max_age_seconds=5,
            max_spread_bps=Decimal("25"),
            minimum_depth_multiple=Decimal("2"),
            minimum_session_volume=750000,
        )
        with self.assertRaisesRegex(
            LocalAssemblyError, "LOCAL_ASSEMBLY_QUALITY_SYMBOL_NOT_READY"
        ):
            reader.get_quality_evidence(
                "plan-not-ready", "XYZ", as_of=NOW, timeout_seconds=3
            )


if __name__ == "__main__":
    unittest.main()
