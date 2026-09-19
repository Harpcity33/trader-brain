"""Offline composition test for the signed attended IBKR command lane."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from titan_brain.live.broker.ibkr_runtime import IbkrOfficialRuntime
from titan_brain.live.broker.base import (
    BrokerMutationBlocked,
    ClientRefRecoverySource,
    OrderCoverageContract,
    OrderFamilyCoverageStatus,
    OrderRequest,
)
from titan_brain.live.broker.ibkr_account import IbkrStableAccountSnapshotReader
from titan_brain.live.broker.ibkr_reconciliation import IbkrReconciliationTransport
from titan_brain.live.broker.production import SupportedProductionBrokerAdapter
from titan_brain.live.broker.ibkr_transport import (
    IBKR_TRANSPORT_ID,
    IbkrProductionTransport,
)
from titan_brain.live.discovery_composition import (
    SupportedIbkrDiscoveryProviderComposition,
)
from titan_brain.live.local_assembly import (
    LocalAssemblyError,
    LocalProviderAssembly,
)
from titan_brain.live.ibkr_command_inputs import (
    AUTHORITY_SCHEMA,
    IbkrCommandInputError,
    OwnedIbkrWriterInterlock,
    build_release_bound_ibkr_command_inputs,
)
from titan_brain.live.policy import PolicyBundle, canonical_json
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import (
    AccountWriterLock,
    attended_coordinator_lock_key,
)
from tests.live_activation_support import (
    activate_canonical_runtime,
    record_flat_reconciliation,
)
from tests.live_dollar_policy_support import copy_dollar_policy_inputs
from tests.test_live_ibkr_runtime import (
    FakeEClient,
    NOW,
    SYNTHETIC_ACCOUNT,
    bundle,
)


AUTH = "b" * 64
PROVIDER = "d" * 64
IBKR_CONTROL_SERVICE = (
    "titan-full-live-ibkr-ending-3103-control-authentication-key"
)
IBKR_CONTROL_ACCOUNT = "ibkr-live-ending-3103"


_REAL_ACCOUNT_READER_CALL = IbkrStableAccountSnapshotReader.__call__


def _synthetic_exhaustive_account_reader_call(
    reader: IbkrStableAccountSnapshotReader,
):
    """Upgrade bounded TWS evidence only for command-graph unit tests.

    Raw TWS completed-order history is deliberately incomplete across dates.
    These tests exercise command composition and client-ID serialization, not
    that provider limitation, so the harness explicitly substitutes the
    stronger external-provider contract production still requires.
    """

    snapshot = _REAL_ACCOUNT_READER_CALL(reader)
    bounded = reader.coverage
    reader._coverage = OrderCoverageContract(  # noqa: SLF001 - explicit test seam
        contract_version="synthetic-exhaustive-command-assembly-test-v1",
        evidence_observed_at=bounded.evidence_observed_at,
        families=tuple(
            replace(item, status=OrderFamilyCoverageStatus.COMPLETE_GENERAL)
            for item in bounded.families
        ),
        client_ref_recovery_source=ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY,
        broker_preserves_client_ref=True,
        negative_client_ref_results_authoritative=False,
    )
    return replace(
        snapshot,
        standard_equity_orders_complete=True,
        option_orders_complete=True,
        advanced_orders_complete=True,
    )


class _Keychain:
    def __init__(self, *, control_key=b"synthetic-ibkr-control-key-material-32-bytes"):
        self.control_key = control_key

    def read(self, item):
        if item.service == "titan-massive-api":
            return b"synthetic-market-key"
        if item.service == IBKR_CONTROL_SERVICE:
            if item.account != IBKR_CONTROL_ACCOUNT or self.control_key is None:
                raise KeyError(item.source_label)
            return self.control_key
        raise KeyError(item.service)

    def read_text(self, item):
        return self.read(item).decode()


def _account_binding() -> str:
    material = "\0".join(
        (
            "titan-ibkr-account-binding-v1",
            "ibkr-local-live-ending-3103-v1",
            "ibkr-live-ending-3103",
            "live",
            SYNTHETIC_ACCOUNT,
        )
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _release_manifest(root: Path) -> dict[str, object]:
    sources = list((root / "src/titan_brain").rglob("*.py"))
    sources.extend(
        (
            Path(__file__).resolve(),
            root / "tests/test_live_ibkr_runtime.py",
        )
    )
    files = []
    for source in sorted(set(sources)):
        data = source.read_bytes()
        files.append(
            {
                "path": source.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return {"release_manifest_hash": "8" * 64, "files": files}


class IbkrCommandAssemblyTests(unittest.TestCase):
    def setUp(self):
        FakeEClient.instances = []
        FakeEClient.accounts = SYNTHETIC_ACCOUNT
        FakeEClient.emit_error = False
        FakeEClient.emit_order = False
        FakeEClient.suppress_callbacks = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source_root = Path(__file__).resolve().parents[1]
        self.release_root = Path(self.tmp.name) / "release"
        self.install_root = Path(self.tmp.name) / "install"
        shutil.copytree(self.source_root / "config", self.release_root / "config")
        bindings_path = self.release_root / "config/provider_bindings.json"
        bindings = json.loads(bindings_path.read_text())
        bindings["ibkr_gmail"].update(
            {"enabled": False, "send_probe_authorized": False}
        )
        bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
        copy_dollar_policy_inputs(self.source_root, self.release_root)
        (self.install_root / "state").mkdir(parents=True)
        config_path = self.release_root / "config/full_live_supported_test.json"
        config = json.loads(
            (self.release_root / "config/full_live_ibkr.json").read_text()
        )
        # Broker command fixtures do not enroll or exercise a Gmail route.
        config["notifications"].update(
            {
                "delivery_sink": "local_jsonl_staging",
                "destination_bridge_configured": False,
            }
        )
        config["risk"]["limits_live_provenance_verified"] = True
        config["execution"].update(
            {
                "broker_adapter": "supported_production_transport",
                "production_transport_id": IBKR_TRANSPORT_ID,
                "production_account_binding_fingerprint": _account_binding(),
                "production_authorization_binding_id": AUTH,
                "ibkr_provider_contract_id": PROVIDER,
                "ibkr_ledger_relative_path": "state/ibkr-execution.sqlite3",
                "ibkr_existing_order_reserve_dollars": "0.25",
                "local_mutation_interlock_enabled": True,
            }
        )
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.sdk_patch = patch(
            "titan_brain.live.broker.ibkr_runtime._load_attested_sdk",
            return_value=bundle(),
        )
        self.sdk_patch.start()
        self.addCleanup(self.sdk_patch.stop)
        # The real bounded-reader behavior is asserted in test_live_ibkr_account.
        # This file needs an explicit exhaustive-provider stand-in so a command
        # composition test cannot silently redefine what raw TWS proves.
        self.exhaustive_reader_patch = patch.object(
            IbkrStableAccountSnapshotReader,
            "__call__",
            _synthetic_exhaustive_account_reader_call,
        )
        self.exhaustive_reader_patch.start()
        self.addCleanup(self.exhaustive_reader_patch.stop)

    def _write_authority(self, *, release_manifest_hash: str) -> None:
        policy = PolicyBundle.load(
            self.release_root,
            config_relative="config/full_live_supported_test.json",
        )
        body = {
            "schema_version": AUTHORITY_SCHEMA,
            "release_manifest_hash": release_manifest_hash,
            "config_hash": policy.config_hash,
            "policy_hash": policy.policy_hash,
            "account_key": policy.account_key,
            "account_masked": "****3103",
            "authorization_binding_id": AUTH,
            "account_binding_fingerprint": _account_binding(),
            "provider_contract_id": PROVIDER,
            "environment": "live",
            "client_id": 19736,
            "issued_at": (NOW - timedelta(seconds=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
        body["artifact_hash"] = hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest()
        path = self.install_root / "control/ibkr/attended-command-authority.json"
        path.parent.mkdir(parents=True)
        path.write_text(canonical_json(body), encoding="utf-8")
        path.chmod(0o600)

    def _activate_attended_state(
        self, *, manifest_hash: str, lock_root: Path, hold_coordinator: bool = True
    ):
        policy = PolicyBundle.load(
            self.release_root,
            config_relative="config/full_live_supported_test.json",
        )
        state_path = self.install_root / "state/full-live.sqlite3"
        store = LiveStateStore(state_path)
        self.addCleanup(store.close)
        store.initialize_runtime(
            runtime_id=policy.runtime_id,
            account_key=policy.account_key,
            release_manifest_hash=manifest_hash,
            config_hash=policy.config_hash,
            policy_hash=policy.policy_hash,
            initialized_at=NOW - timedelta(seconds=10),
        )
        activate_canonical_runtime(
            store,
            created_at=NOW - timedelta(seconds=4),
            activated_at=NOW - timedelta(seconds=3),
            expires_at=NOW + timedelta(minutes=1),
            writer_owner_id="attended-read-coordinator",
            readiness_overrides={
                "execution_authority_mode": "attended_only",
                "attended_mutation_supported": True,
                "unattended_mutation_supported": False,
                "per_mutation_confirmation_required": True,
            },
        )
        record_flat_reconciliation(
            store,
            account_key=policy.account_key,
            received_at=NOW - timedelta(seconds=1),
            label="attended-active",
        )
        store.set_runtime_mode(
            "ACTIVE", occurred_at=NOW, reason="fresh attended reconciliation"
        )
        coordinator = AccountWriterLock(
            lock_root,
            attended_coordinator_lock_key(policy.account_key),
            owner_id="attended-read-coordinator",
        )
        if hold_coordinator:
            coordinator.acquire()
            self.addCleanup(coordinator.release)
        return policy, store, coordinator

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_supported_selection_builds_attended_graph_without_order_mutation(
        self, _validate_sdk
    ):
        runtimes = []

        def runtime_factory(**kwargs):
            runtime = IbkrOfficialRuntime(
                profile=kwargs["profile"],
                install_root=kwargs["install_root"],
                read_timeout_seconds=0.25,
                instrument_timeout_seconds=0.25,
                connect_timeout_seconds=0.25,
                shutdown_timeout_seconds=0.25,
                clock=kwargs["clock"],
            )
            runtimes.append(runtime)
            return runtime

        # This is the exact assembly shape used by serve/readiness: supported
        # reads are available, but omission of command-scoped inputs means no
        # command client and no production transport exist.
        read_assembly = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=runtime_factory,
        )
        read_composition = read_assembly.runtime_composition()
        self.assertIsInstance(
            read_composition.production_transport, IbkrReconciliationTransport
        )
        self.assertTrue(
            read_composition.managed_control_ready(read_assembly.full_live["execution"])
        )
        self.assertEqual(len(FakeEClient.instances), 1)
        self.assertFalse(FakeEClient.instances[0].mutations)
        read_broker = SupportedProductionBrokerAdapter(
            read_composition.production_transport,
            clock=lambda: NOW,
        )
        snapshot = read_broker.get_account_snapshot("****3103")
        self.assertTrue(snapshot.whole_broker_reconciled)
        self.assertTrue(read_broker.capabilities.supports_ref_id_lookup)
        with self.assertRaisesRegex(BrokerMutationBlocked, "COORDINATOR_READ_ONLY"):
            read_broker.review_equity_order(
                OrderRequest(
                    account_masked="****3103",
                    symbol="SPY",
                    side="buy",
                    order_type="limit",
                    quantity=1,
                    market_hours="regular_hours",
                    time_in_force="gfd",
                    client_ref_id="10000000-0000-4000-8000-000000000010",
                    limit_price="10.00",
                )
            )
        read_roles: set[str] = set()
        read_objects: set[int] = set()

        def walk_read(role, component):
            self.assertNotIn(role, read_roles)
            self.assertNotIn(id(component), read_objects)
            read_roles.add(role)
            read_objects.add(id(component))
            enumerator = getattr(component, "release_components", None)
            if callable(enumerator):
                for child_role, child, _child_members in enumerator():
                    walk_read(child_role, child)

        walk_read("production_transport", read_composition.production_transport)
        walk_read("discovery_provider", read_composition.discovery_provider)
        self.assertIn("ibkr_read_bridge", read_roles)
        self.assertIn("ibkr_account_snapshot_reader", read_roles)
        self.assertIn("ibkr_stable_account_clock", read_roles)
        bound = read_composition.bind_release(
            _release_manifest(self.source_root),
            release_root=self.source_root,
        )
        bound_roles = {item.role for item in bound}
        self.assertIn("production_transport", bound_roles)
        self.assertIn("control_authenticator", bound_roles)
        self.assertNotIn(
            "review_protection_equity_order",
            IbkrReconciliationTransport.__dict__,
        )
        read_assembly.close()
        FakeEClient.instances = []
        runtimes.clear()

        release_manifest_hash = "9" * 64
        self._write_authority(release_manifest_hash=release_manifest_hash)
        lock_directory = Path(self.tmp.name) / "locks"
        self._activate_attended_state(
            manifest_hash=release_manifest_hash,
            lock_root=lock_directory,
        )
        with patch(
            "titan_brain.live.ibkr_command_inputs.user_account_writer_lock_directory",
            return_value=lock_directory,
        ):
            inputs = build_release_bound_ibkr_command_inputs(
                release_root=self.release_root,
                install_root=self.install_root,
                full_live_config_name="full_live_supported_test.json",
                release_manifest_hash=release_manifest_hash,
                clock=lambda: NOW,
            )
        assembly = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=runtime_factory,
            ibkr_command_inputs=inputs,
        )
        self.addCleanup(assembly.close)
        composition = assembly.runtime_composition()
        self.assertIsInstance(
            composition.production_transport, IbkrProductionTransport
        )
        self.assertIsInstance(
            composition.discovery_provider,
            SupportedIbkrDiscoveryProviderComposition,
        )
        self.assertTrue(
            composition.managed_control_ready(assembly.full_live["execution"])
        )
        facade = assembly.attended_runtime()
        self.assertEqual(facade.account_masked, "****3103")
        self.assertFalse(facade.capabilities.supports_unattended_writes)
        self.assertTrue(facade.capabilities.review_requires_explicit_confirmation)
        self.assertEqual(len(FakeEClient.instances), 2)
        self.assertEqual(
            {client.clientId for client in FakeEClient.instances},
            {19736, 19737},
        )
        self.assertNotIn(19735, {client.clientId for client in FakeEClient.instances})
        self.assertTrue(all(not client.mutations for client in FakeEClient.instances))

        # The combined transport/discovery graph must not inventory one object
        # under two roles. This is the same ambiguity the release binder denies.
        roles: set[str] = set()
        objects: set[int] = set()

        def walk(role, component, members):
            self.assertNotIn(role, roles)
            self.assertNotIn(id(component), objects)
            roles.add(role)
            objects.add(id(component))
            enumerator = getattr(component, "release_components", None)
            if callable(enumerator):
                for child_role, child, child_members in enumerator():
                    walk(child_role, child, child_members)

        walk("production_transport", composition.production_transport, ())
        walk("discovery_provider", composition.discovery_provider, ())
        self.assertIn("ibkr_stable_account_clock", roles)
        self.assertIn("ibkr_command_callback_router", roles)
        self.assertIn("ibkr_read_callback_router", roles)

    def test_attended_command_lock_coexists_with_active_read_coordinator(self):
        policy = PolicyBundle.load(
            self.release_root,
            config_relative="config/full_live_supported_test.json",
        )
        manifest_hash = "9" * 64
        state_path = self.install_root / "state/full-live.sqlite3"
        store = LiveStateStore(state_path)
        self.addCleanup(store.close)
        store.initialize_runtime(
            runtime_id=policy.runtime_id,
            account_key=policy.account_key,
            release_manifest_hash=manifest_hash,
            config_hash=policy.config_hash,
            policy_hash=policy.policy_hash,
            initialized_at=NOW - timedelta(seconds=10),
        )
        activate_canonical_runtime(
            store,
            created_at=NOW - timedelta(seconds=4),
            activated_at=NOW - timedelta(seconds=3),
            expires_at=NOW + timedelta(minutes=1),
            writer_owner_id="attended-read-coordinator",
            readiness_overrides={
                "execution_authority_mode": "attended_only",
                "attended_mutation_supported": True,
                "unattended_mutation_supported": False,
                "per_mutation_confirmation_required": True,
            },
        )
        record_flat_reconciliation(
            store,
            account_key=policy.account_key,
            received_at=NOW - timedelta(seconds=1),
            label="attended-active",
        )
        store.set_runtime_mode(
            "ACTIVE", occurred_at=NOW, reason="fresh attended reconciliation"
        )

        lock_root = Path(self.tmp.name) / "global-locks"
        coordinator = AccountWriterLock(
            lock_root,
            attended_coordinator_lock_key(policy.account_key),
            owner_id="attended-read-coordinator",
        )
        coordinator_probe = AccountWriterLock(
            lock_root,
            attended_coordinator_lock_key(policy.account_key),
            owner_id="attended-command-coordinator-probe",
        )
        broker_writer = AccountWriterLock(
            lock_root,
            policy.account_key,
            broker_account_binding_fingerprint=_account_binding(),
            authorization_binding_id=AUTH,
        )
        coordinator.acquire()
        self.addCleanup(coordinator.release)
        interlock = OwnedIbkrWriterInterlock(
            lock=broker_writer,
            coordinator_probe=coordinator_probe,
            state_path=state_path,
            policy=policy,
            release_manifest_hash=manifest_hash,
            clock=lambda: NOW,
        )
        self.addCleanup(interlock.close)
        interlock()
        self.assertTrue(coordinator.held)
        self.assertTrue(broker_writer.held)
        self.assertNotEqual(coordinator.path, broker_writer.path)

    def test_attended_command_rejects_fresh_lease_without_kernel_coordinator(self):
        manifest_hash = "9" * 64
        lock_root = Path(self.tmp.name) / "global-locks"
        policy, _store, _coordinator = self._activate_attended_state(
            manifest_hash=manifest_hash,
            lock_root=lock_root,
            hold_coordinator=False,
        )
        broker_writer = AccountWriterLock(
            lock_root,
            policy.account_key,
            broker_account_binding_fingerprint=_account_binding(),
            authorization_binding_id=AUTH,
        )
        coordinator_probe = AccountWriterLock(
            lock_root,
            attended_coordinator_lock_key(policy.account_key),
            owner_id="attended-command-coordinator-probe",
        )
        interlock = OwnedIbkrWriterInterlock(
            lock=broker_writer,
            coordinator_probe=coordinator_probe,
            state_path=self.install_root / "state/full-live.sqlite3",
            policy=policy,
            release_manifest_hash=manifest_hash,
            clock=lambda: NOW,
        )
        self.addCleanup(interlock.close)
        with self.assertRaisesRegex(
            IbkrCommandInputError,
            "IBKR_COMMAND_ATTENDED_COORDINATOR_NOT_RUNNING",
        ):
            interlock()
        self.assertFalse(broker_writer.held)

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_attended_command_sessions_are_serialized_before_fixed_client_ids_connect(
        self, _validate_sdk
    ):
        manifest_hash = "9" * 64
        self._write_authority(release_manifest_hash=manifest_hash)
        lock_root = Path(self.tmp.name) / "global-locks"
        self._activate_attended_state(
            manifest_hash=manifest_hash,
            lock_root=lock_root,
        )

        def runtime_factory(**kwargs):
            return IbkrOfficialRuntime(
                profile=kwargs["profile"],
                install_root=kwargs["install_root"],
                read_timeout_seconds=0.25,
                instrument_timeout_seconds=0.25,
                connect_timeout_seconds=0.25,
                shutdown_timeout_seconds=0.25,
                clock=kwargs["clock"],
            )

        def command_inputs():
            with patch(
                "titan_brain.live.ibkr_command_inputs.user_account_writer_lock_directory",
                return_value=lock_root,
            ):
                return build_release_bound_ibkr_command_inputs(
                    release_root=self.release_root,
                    install_root=self.install_root,
                    full_live_config_name="full_live_supported_test.json",
                    release_manifest_hash=manifest_hash,
                    clock=lambda: NOW,
                )

        first = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=runtime_factory,
            ibkr_command_inputs=command_inputs(),
        )
        second = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=runtime_factory,
            ibkr_command_inputs=command_inputs(),
        )
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        first.runtime_composition()
        self.assertEqual(
            {client.clientId for client in FakeEClient.instances},
            {19736, 19737},
        )
        with self.assertRaisesRegex(
            LocalAssemblyError,
            "LOCAL_ASSEMBLY_IBKR_COMMAND_SESSION_INTERLOCK_UNAVAILABLE",
        ):
            second.runtime_composition()
        self.assertEqual(len(FakeEClient.instances), 2)

        first.close()
        second.runtime_composition()
        self.assertEqual(len(FakeEClient.instances), 4)

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_supported_selection_fails_before_socket_when_ibkr_control_key_absent(
        self, _validate_sdk
    ):
        factory_called = False

        def forbidden_factory(**_kwargs):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("IBKR runtime must not be constructed")

        assembly = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(control_key=None),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=forbidden_factory,
        )
        with self.assertRaisesRegex(
            LocalAssemblyError,
            "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_KEY_UNAVAILABLE",
        ):
            assembly.runtime_composition()
        self.assertFalse(factory_called)
        self.assertEqual(FakeEClient.instances, [])

    @patch("titan_brain.live.local_assembly.validate_installed_sdk")
    def test_supported_selection_rejects_legacy_7153_control_locator(
        self, _validate_sdk
    ):
        bindings_path = self.release_root / "config/provider_bindings.json"
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
        bindings["ibkr_control"]["credential_account"] = "ending-7153"
        bindings_path.write_text(json.dumps(bindings), encoding="utf-8")

        factory_called = False

        def forbidden_factory(**_kwargs):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("IBKR runtime must not be constructed")

        assembly = LocalProviderAssembly(
            release_root=self.release_root,
            install_root=self.install_root,
            keychain=_Keychain(),
            clock=lambda: NOW,
            full_live_config_name="full_live_supported_test.json",
            ibkr_runtime_factory=forbidden_factory,
        )
        with self.assertRaisesRegex(
            LocalAssemblyError,
            "LOCAL_ASSEMBLY_IBKR_MANAGED_CONTROL_PROFILE_INVALID",
        ):
            assembly.runtime_composition()
        self.assertFalse(factory_called)
        self.assertEqual(FakeEClient.instances, [])


if __name__ == "__main__":
    unittest.main()
