from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from titan_brain.live.broker.ibkr_risk_evidence import (
    DailyIbkrRiskBaselineAuthenticator,
    IbkrRiskEvidenceBindings,
    IbkrRiskEvidenceEnricher,
    IbkrRiskEvidenceError,
    IbkrRiskHighWaterLedger,
)
from titan_brain.live.policy import canonical_json
from titan_brain.live.provider_clients import KeychainItem
from tests.test_live_ibkr_risk_evidence import (
    ACCOUNT_BINDING,
    FixedClock,
    NOW,
    PRIOR_TRADING_DATE,
    RecordingKeyReader,
    SECRET,
    TRADING_DATE,
    bindings,
    raw_snapshot,
    receipt_payload,
    write_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    source = ROOT / "scripts/install_full_live_paused.py"
    spec = importlib.util.spec_from_file_location(
        "install_full_live_paused_risk_upgrade_test", source
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load paused installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_installer()


class IbkrRiskHighWaterUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.install_root = Path(self.temporary.name).resolve()
        (self.install_root / "state").mkdir(mode=0o700)
        self.ledger_path = self.install_root / "state/ibkr-risk-high-water.sqlite3"
        self.receipt_path = self.install_root / "old-baseline.json"
        self.old = bindings()
        write_receipt(self.receipt_path, receipt_payload(self.old))
        authenticator = DailyIbkrRiskBaselineAuthenticator(
            path=self.receipt_path,
            key_reader=RecordingKeyReader(),
            key_item=KeychainItem(service="test-risk", account="test-account"),
            expected=self.old,
            clock=FixedClock(),
        )
        ledger = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=self.old.ledger_bindings,
            allow_create=True,
        )
        enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=authenticator,
            high_water_ledger=ledger,
            snapshot_max_age_seconds=5,
        )
        self.assertEqual(
            enricher.enrich(raw_snapshot(net_liquidation="1300")).peak_equity,
            1300,
        )
        ledger.close()

        # Recreate the exact v1 on-disk shape that predates release-scoped
        # carry-forward support and the v3 persistent lineage binding.
        connection = sqlite3.connect(self.ledger_path)
        connection.execute("ALTER TABLE binding RENAME TO binding_v3")
        connection.execute(
            "CREATE TABLE binding ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "release_manifest_hash TEXT NOT NULL,config_hash TEXT NOT NULL,"
            "policy_binding_id TEXT NOT NULL,risk_binding_id TEXT NOT NULL,"
            "account_key TEXT NOT NULL,account_masked TEXT NOT NULL,"
            "account_binding_fingerprint TEXT NOT NULL,"
            "latest_trading_date TEXT,highest_equity TEXT)"
        )
        connection.execute(
            "INSERT INTO binding SELECT singleton,release_manifest_hash,"
            "config_hash,policy_binding_id,risk_binding_id,account_key,"
            "account_masked,account_binding_fingerprint,latest_trading_date,"
            "highest_equity FROM binding_v3"
        )
        connection.execute("DROP TABLE binding_v3")
        connection.execute("DROP TABLE carry_forward")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        self.ledger_path.chmod(0o600)

        self.risk = {"max_trade_risk_dollars": "10.00"}
        self.risk_bytes = canonical_json(self.risk).encode("utf-8")
        self.config = {
            "runtime_id": "full-live-ibkr-ending-3103",
            "strategy_id": "titan-momentum-v1",
            "account": {
                "account_key": "ibkr-live-ending-3103",
                "masked_identifier": "ending-3103",
                "required_last4": "3103",
                "allowed_type": "no_borrow_margin",
                "margin_debit_allowed": False,
            },
            "scope": {},
            "sessions": {"timezone": "America/New_York"},
            "risk": {"limits_path": "config/risk_limits.json"},
            "execution": {
                "execution_authority_mode": "unattended",
                "production_account_binding_fingerprint": ACCOUNT_BINDING,
                "ibkr_risk_high_water_ledger_relative_path": (
                    "state/ibkr-risk-high-water.sqlite3"
                ),
            },
        }
        self.target_release = "9" * 64
        self.target_config = hashlib.sha256(
            canonical_json(self.config).encode("utf-8")
        ).hexdigest()
        self.target_policy = "e" * 64
        self.target_risk = hashlib.sha256(
            canonical_json(self.risk).encode("utf-8")
        ).hexdigest()
        self.manifest = {
            "release_manifest_hash": self.target_release,
            "config_hash": self.target_config,
            "policy_hash": self.target_policy,
        }
        self.payloads = {"config/risk_limits.json": self.risk_bytes}

    def target_bindings(self) -> IbkrRiskEvidenceBindings:
        return IbkrRiskEvidenceBindings(
            release_manifest_hash=self.target_release,
            config_hash=self.target_config,
            policy_binding_id=self.target_policy,
            risk_binding_id=self.target_risk,
            account_key=self.old.account_key,
            account_masked=self.old.account_masked,
            account_binding_fingerprint=self.old.account_binding_fingerprint,
            valid_for_trading_date=TRADING_DATE,
            prior_trading_date=PRIOR_TRADING_DATE,
        )

    def migrate(self):
        return installer._migrate_ibkr_risk_high_water_ledger(
            self.install_root,
            manifest=self.manifest,
            config=self.config,
            payloads=self.payloads,
            migrated_at=datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc),
        )

    def lineage_hash(self) -> str:
        connection = sqlite3.connect(self.ledger_path)
        try:
            return str(
                connection.execute(
                    "SELECT lineage_hash FROM binding WHERE singleton=1"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def test_v1_release_upgrade_archives_and_carries_same_day_peak(self) -> None:
        migration = self.migrate()
        self.assertTrue(migration["performed"])
        self.assertEqual(
            migration["reason"],
            "PAUSED_RELEASE_SCOPED_ARCHIVE_CARRY_FORWARD",
        )
        self.assertEqual(migration["source_schema_version"], 1)
        self.assertEqual(migration["target_schema_version"], 3)
        self.assertEqual(migration["highest_equity"], "1300")
        first_lineage = self.lineage_hash()
        self.assertRegex(first_lineage, r"^[0-9a-f]{64}$")
        archive = self.install_root / migration["archive_relative_path"]
        self.assertTrue(archive.is_file())
        self.assertEqual(
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            migration["source_ledger_sha256"],
        )
        archived = installer._read_ibkr_risk_ledger(archive)
        self.assertEqual(archived["highest_equity"], 1300)

        target = self.target_bindings()
        receipt = self.install_root / "new-baseline.json"
        # Same-session prior-day evidence may be below today's already-seen
        # intraday peak.  The carried floor still wins.
        write_receipt(
            receipt,
            receipt_payload(target, prior_high_water="1200"),
        )
        ledger = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=target.ledger_bindings,
        )
        self.addCleanup(ledger.close)
        enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=DailyIbkrRiskBaselineAuthenticator(
                path=receipt,
                key_reader=RecordingKeyReader(secret=SECRET),
                key_item=KeychainItem(
                    service="test-new-risk", account="test-new-account"
                ),
                expected=target,
                clock=FixedClock(NOW),
            ),
            high_water_ledger=ledger,
            snapshot_max_age_seconds=5,
        )
        self.assertEqual(
            enricher.enrich(raw_snapshot(net_liquidation="1100")).peak_equity,
            1300,
        )
        ledger.close()
        connection = sqlite3.connect(self.ledger_path)
        carry = connection.execute(
            "SELECT source_ledger_sha256,source_highest_equity "
            "FROM carry_forward WHERE singleton=1"
        ).fetchone()
        connection.close()
        self.assertEqual(carry, (migration["source_ledger_sha256"], "1300"))

        repeated = self.migrate()
        self.assertFalse(repeated["performed"])
        self.assertEqual(repeated["reason"], "RISK_LEDGER_ALREADY_BOUND")
        self.assertEqual(self.lineage_hash(), first_lineage)

    def test_v2_release_upgrade_is_accepted_and_migrated_to_v3(self) -> None:
        connection = sqlite3.connect(self.ledger_path)
        connection.execute(
            "CREATE TABLE carry_forward ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "source_release_manifest_hash TEXT NOT NULL,"
            "source_config_hash TEXT NOT NULL,"
            "source_policy_binding_id TEXT NOT NULL,"
            "source_risk_binding_id TEXT NOT NULL,"
            "source_account_key TEXT NOT NULL,"
            "source_account_masked TEXT NOT NULL,"
            "source_account_binding_fingerprint TEXT NOT NULL,"
            "source_latest_trading_date TEXT NOT NULL,"
            "source_highest_equity TEXT NOT NULL,"
            "source_ledger_sha256 TEXT NOT NULL,"
            "archive_relative_path TEXT NOT NULL,"
            "migrated_at TEXT NOT NULL)"
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()

        migration = self.migrate()

        self.assertTrue(migration["performed"])
        self.assertEqual(migration["source_schema_version"], 2)
        self.assertEqual(migration["target_schema_version"], 3)
        self.assertEqual(migration["highest_equity"], "1300")
        self.assertRegex(self.lineage_hash(), r"^[0-9a-f]{64}$")

    def test_missing_configured_ledger_is_bootstrapped_with_fresh_lineage(self) -> None:
        self.ledger_path.unlink()

        first = self.migrate()

        self.assertTrue(first["required"])
        self.assertTrue(first["performed"])
        self.assertEqual(first["reason"], "PAUSED_RISK_LEDGER_BOOTSTRAPPED")
        self.assertEqual(first["target_schema_version"], 3)
        first_lineage = self.lineage_hash()
        self.assertRegex(first_lineage, r"^[0-9a-f]{64}$")
        installed = installer._read_ibkr_risk_ledger(self.ledger_path)
        self.assertEqual(installed["schema_version"], 3)
        self.assertIsNone(installed["highest_equity"])
        ledger = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=self.target_bindings().ledger_bindings,
        )
        ledger.close()

        self.ledger_path.unlink()
        second = self.migrate()
        self.assertEqual(second["reason"], "PAUSED_RISK_LEDGER_BOOTSTRAPPED")
        self.assertNotEqual(self.lineage_hash(), first_lineage)

    def test_release_rotation_replaces_v3_lineage_while_preserving_floor(self) -> None:
        self.migrate()
        first_lineage = self.lineage_hash()
        self.manifest["release_manifest_hash"] = "8" * 64

        migration = self.migrate()

        self.assertTrue(migration["performed"])
        self.assertEqual(migration["source_schema_version"], 3)
        self.assertEqual(migration["target_schema_version"], 3)
        self.assertEqual(migration["highest_equity"], "1300")
        self.assertNotEqual(self.lineage_hash(), first_lineage)

    def test_v3_lineage_must_be_canonical_lowercase_sha256(self) -> None:
        self.migrate()
        connection = sqlite3.connect(self.ledger_path)
        connection.execute(
            "UPDATE binding SET lineage_hash=? WHERE singleton=1", ("A" * 64,)
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(installer.InstallError, "lineage hash is invalid"):
            installer._read_ibkr_risk_ledger(self.ledger_path)

    def test_account_binding_change_fails_before_archive_or_replacement(self) -> None:
        before = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        self.config["execution"]["production_account_binding_fingerprint"] = "f" * 64
        self.manifest["config_hash"] = hashlib.sha256(
            canonical_json(self.config).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            installer.InstallError, "cannot change account binding"
        ):
            self.migrate()
        self.assertEqual(
            hashlib.sha256(self.ledger_path.read_bytes()).hexdigest(), before
        )
        self.assertFalse(
            (self.install_root / "state/ibkr-risk-high-water-archive").exists()
        )

    def test_path_rename_is_rejected_before_old_peak_can_be_reset(self) -> None:
        before = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        replacement = self.install_root / "state/renamed-risk-high-water.sqlite3"
        self.config["execution"][
            "ibkr_risk_high_water_ledger_relative_path"
        ] = replacement.relative_to(self.install_root).as_posix()
        self.manifest["config_hash"] = hashlib.sha256(
            canonical_json(self.config).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            installer.InstallError,
            "must use canonical path state/ibkr-risk-high-water.sqlite3",
        ):
            self.migrate()

        self.assertEqual(
            hashlib.sha256(self.ledger_path.read_bytes()).hexdigest(), before
        )
        self.assertFalse(replacement.exists())
        self.assertFalse(
            (self.install_root / "state/ibkr-risk-high-water-archive").exists()
        )

    def test_unattended_install_cannot_omit_canonical_ledger_path(self) -> None:
        before = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        self.config["execution"]["broker_adapter"] = "supported_production_transport"
        self.config["execution"].pop(
            "ibkr_risk_high_water_ledger_relative_path"
        )
        self.manifest["config_hash"] = hashlib.sha256(
            canonical_json(self.config).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            installer.InstallError,
            "must use canonical path state/ibkr-risk-high-water.sqlite3",
        ):
            self.migrate()

        self.assertEqual(
            hashlib.sha256(self.ledger_path.read_bytes()).hexdigest(), before
        )

    def test_carried_peak_rejects_lower_baseline_on_later_trading_day(self) -> None:
        self.migrate()
        next_time = NOW + timedelta(days=1)
        next_bindings = IbkrRiskEvidenceBindings(
            release_manifest_hash=self.target_release,
            config_hash=self.target_config,
            policy_binding_id=self.target_policy,
            risk_binding_id=self.target_risk,
            account_key=self.old.account_key,
            account_masked=self.old.account_masked,
            account_binding_fingerprint=self.old.account_binding_fingerprint,
            valid_for_trading_date=date(2026, 9, 15),
            prior_trading_date=TRADING_DATE,
        )
        receipt = self.install_root / "next-day-baseline.json"
        write_receipt(
            receipt,
            receipt_payload(
                next_bindings,
                prior_high_water="1299",
                provider_receipt="8" * 64,
                issued_at=next_time - timedelta(minutes=1),
                provider_observed_at=next_time - timedelta(minutes=2),
            ),
        )
        ledger = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=next_bindings.ledger_bindings,
        )
        self.addCleanup(ledger.close)
        enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=DailyIbkrRiskBaselineAuthenticator(
                path=receipt,
                key_reader=RecordingKeyReader(secret=SECRET),
                key_item=KeychainItem(
                    service="test-next-risk", account="test-next-account"
                ),
                expected=next_bindings,
                clock=FixedClock(next_time),
            ),
            high_water_ledger=ledger,
            snapshot_max_age_seconds=5,
        )
        with self.assertRaises(IbkrRiskEvidenceError) as caught:
            enricher.enrich(
                raw_snapshot(now=next_time, net_liquidation="1200")
            )
        self.assertEqual(
            caught.exception.code,
            "IBKR_RISK_EVIDENCE_BASELINE_HIGH_WATER_REGRESSION",
        )


if __name__ == "__main__":
    unittest.main()
