from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from titan_brain.live.release import (
    MANIFEST_SCHEMA,
    calculate_manifest_hash,
    load_release_manifest,
)
from titan_brain.live import cli as live_cli
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import AccountWriterLock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_script("build_full_live_release", "scripts/build_full_live_release.py")
installer = load_script("install_full_live_paused", "scripts/install_full_live_paused.py")


class GitReleaseFixture:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source_root = self.base / "source"
        release_paths = set(builder.FIXED_RELEASE_PATHS)
        for pattern in (
            "config/*.json",
            "src/titan_brain/**/*.py",
            "deployment/*.md",
            "deployment/*.plist.in",
        ):
            release_paths.update(
                path.relative_to(ROOT).as_posix()
                for path in ROOT.glob(pattern)
                if path.is_file()
            )
        for relative in sorted(release_paths):
            source = ROOT / relative
            destination = self.source_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Titan Release Test")
        self.git("config", "user.email", "titan-release-test@example.invalid")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "release fixture")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.source_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def build(self, output: str = "dist") -> dict[str, str]:
        return builder.build(
            self.source_root,
            self.base / output,
            source_revision=self.source_revision,
        )


class FullLiveReleaseTests(GitReleaseFixture, unittest.TestCase):

    def test_repeated_build_is_byte_for_byte_deterministic(self) -> None:
        first = self.build("one")
        second = self.build("two")
        first_archive = Path(first["archive"])
        second_archive = Path(second["archive"])
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        self.assertEqual(
            Path(first["manifest"]).read_bytes(), Path(second["manifest"]).read_bytes()
        )

    def test_manifest_matches_runtime_contract_and_source_files(self) -> None:
        result = self.build()
        manifest_path = Path(result["manifest"])
        manifest = load_release_manifest(
            manifest_path, verify_files_root=self.source_root
        )
        self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA)
        self.assertEqual(
            manifest["release_manifest_hash"], calculate_manifest_hash(manifest)
        )
        self.assertEqual(manifest["default_mode"], "PAUSED")
        self.assertEqual(manifest["source_commit"], self.source_revision)
        self.assertEqual(result["release_id"], manifest["release_manifest_hash"])
        policy = PolicyBundle.load(self.source_root)
        self.assertEqual(manifest["config_hash"], policy.config_hash)
        self.assertEqual(manifest["policy_hash"], policy.policy_hash)
        inventory = {entry["path"] for entry in manifest["files"]}
        self.assertNotIn("release-manifest.json", inventory)
        self.assertIn("config/full_live.json", inventory)
        self.assertIn("config/risk_limits.json", inventory)
        self.assertIn("src/titan_brain/live/state.py", inventory)
        self.assertIn("scripts/titan-full-live", inventory)
        self.assertEqual(
            manifest["notification_launchd_template"],
            "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in",
        )
        self.assertIn(
            "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in",
            inventory,
        )

    def test_manifest_semantics_and_source_tree_fail_closed(self) -> None:
        result = self.build()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        payloads = {
            entry["path"]: (self.source_root / entry["path"]).read_bytes()
            for entry in manifest["files"]
        }
        manifest["default_mode"] = "ACTIVE"
        body = dict(manifest)
        body.pop("release_manifest_hash")
        manifest["release_manifest_hash"] = hashlib.sha256(
            builder.canonical_json(body)
        ).hexdigest()
        with self.assertRaisesRegex(installer.InstallError, "default_mode"):
            installer._verify_manifest(manifest, payloads)

        manifest["default_mode"] = "PAUSED"
        manifest["source_tree"] = "f" * 64
        body = dict(manifest)
        body.pop("release_manifest_hash")
        manifest["release_manifest_hash"] = hashlib.sha256(
            builder.canonical_json(body)
        ).hexdigest()
        with self.assertRaisesRegex(installer.InstallError, "source_tree"):
            installer._verify_manifest(manifest, payloads)

    def test_archive_has_only_normalized_regular_members(self) -> None:
        result = self.build()
        with tarfile.open(result["archive"], "r:gz") as bundle:
            names = [member.name for member in bundle.getmembers()]
            self.assertEqual(names[0], "release-manifest.json")
            self.assertEqual(names[1:], sorted(names[1:]))
            for member in bundle.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual(member.mtime, 0)
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertIn(member.mode, {0o644, 0o755})
                self.assertNotIn(".git", member.name)
                self.assertNotIn("__pycache__", member.name)

    def test_source_launcher_reaches_operator_cli(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/titan-full-live"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in (
            "doctor",
            "status",
            "serve",
            "prepare-activation",
            "activate",
            "pause-new-entries",
            "managed-closeout",
            "deactivate",
            "notification-worker",
        ):
            self.assertIn(command, completed.stdout)

    def test_invalid_source_revision_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "40-character Git commit"):
            builder.build(
                self.source_root, self.base / "invalid", source_revision="main"
            )

    def test_arbitrary_well_formed_revision_is_rejected(self) -> None:
        arbitrary = "a" * 40
        self.assertNotEqual(arbitrary, self.source_revision)
        with self.assertRaisesRegex(ValueError, "exactly to repository HEAD"):
            builder.build(
                self.source_root,
                self.base / "arbitrary",
                source_revision=arbitrary,
            )

    def test_omitted_revision_discovers_and_binds_exact_head(self) -> None:
        result = builder.build(self.source_root, self.base / "discovered")
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["source_commit"], self.source_revision)
        self.assertEqual(manifest["source_commit"], self.source_revision)

    def test_non_head_commit_is_rejected_even_when_it_resolves(self) -> None:
        prior = self.source_revision
        readme = self.source_root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nRelease fixture update.\n",
            encoding="utf-8",
        )
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "new fixture head")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(ValueError, "exactly to repository HEAD"):
            builder.build(
                self.source_root,
                self.base / "stale",
                source_revision=prior,
            )

    def test_dirty_tracked_source_is_rejected(self) -> None:
        readme = self.source_root / "README.md"
        readme.write_text("dirty tracked source\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "tracked worktree must be clean"):
            self.build()

    def test_uncommitted_release_input_is_rejected(self) -> None:
        uncommitted = self.source_root / "src/titan_brain/live/uncommitted.py"
        uncommitted.write_text("UNCOMMITTED = True\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be committed"):
            self.build()

    def test_archive_payloads_are_bound_to_exact_head_blobs(self) -> None:
        # Unrelated untracked files are not release inputs and cannot affect the
        # archive. Every actual member must equal the blob stored at HEAD.
        (self.source_root / "operator-notes.txt").write_text(
            "not a release input\n", encoding="utf-8"
        )
        result = self.build()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        with tarfile.open(result["archive"], "r:gz") as bundle:
            for entry in manifest["files"]:
                relative = entry["path"]
                member = bundle.extractfile(relative)
                self.assertIsNotNone(member)
                archived = member.read() if member is not None else b""
                committed = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.source_root),
                        "show",
                        f"{self.source_revision}:{relative}",
                    ],
                    check=True,
                    capture_output=True,
                ).stdout
                self.assertEqual(archived, committed, relative)


class PausedInstallerTests(GitReleaseFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixed_lock_directory = self.base / "fixed-user-locks"
        self.lock_directory_patch = mock.patch.object(
            installer,
            "_user_account_writer_lock_directory",
            return_value=self.fixed_lock_directory,
        )
        self.lock_directory_patch.start()
        self.runtime_lock_directory_patch = mock.patch.object(
            live_cli,
            "user_account_writer_lock_directory",
            return_value=self.fixed_lock_directory,
        )
        self.runtime_lock_directory_patch.start()
        self.result = self.build()
        self.application_root = self.base / "Application Support/Titan Momentum"
        self.install_root = self.application_root / "full-live"
        legacy = self.application_root / "runtime/legacy-sentinel.txt"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy-unchanged", encoding="utf-8")
        self.legacy = legacy

    def tearDown(self) -> None:
        self.runtime_lock_directory_patch.stop()
        self.lock_directory_patch.stop()
        super().tearDown()

    def install(self):
        return installer.install(
            Path(self.result["archive"]),
            self.install_root,
            python_executable=Path(sys.executable),
        )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Exercise command behavior with an in-process test lock resolver.

        The installed launcher itself is subprocess-tested only with ``--help``
        because production intentionally has no flag or environment override
        for the fixed per-user lock location.
        """

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = live_cli.main(arguments)
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=returncode,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def downgrade_state_schema(self, version: int) -> None:
        """Create the exact historical full v1 or v2 shape from a fresh v3 DB."""

        if version not in {1, 2}:
            raise ValueError("test downgrade supports only historical v1/v2")
        database = self.install_root / "state/full-live.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE notification_worker_lease")
        connection.execute(
            "ALTER TABLE notification_outbox RENAME TO notification_outbox_current"
        )
        connection.execute(
            """CREATE TABLE notification_outbox (
                message_id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                account_key TEXT NOT NULL,
                template TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                last_attempt_at TEXT,
                next_attempt_at TEXT,
                delivered_at TEXT,
                last_error TEXT,
                delivery_receipt TEXT
            )"""
        )
        v1_columns = (
            "message_id,event_key,account_key,template,payload_json,created_at,"
            "state,attempt_count,last_attempt_at,next_attempt_at,delivered_at,"
            "last_error,delivery_receipt"
        )
        connection.execute(
            f"""INSERT INTO notification_outbox({v1_columns})
                  SELECT {v1_columns} FROM notification_outbox_current"""
        )
        connection.execute("DROP TABLE notification_outbox_current")
        connection.execute(
            """CREATE INDEX outbox_pending
                 ON notification_outbox(state, created_at)"""
        )
        connection.execute(
            "UPDATE schema_meta SET version=1,applied_at=? WHERE singleton=1",
            ("2026-09-08T00:00:00+00:00",),
        )
        connection.execute("PRAGMA user_version=1")
        if version == 2:
            for name, column_type in installer._STATE_V2_OUTBOX_COLUMNS:
                connection.execute(
                    f"ALTER TABLE notification_outbox ADD COLUMN {name} {column_type}"
                )
            connection.execute(
                """CREATE INDEX outbox_claimable
                     ON notification_outbox(
                         state, next_attempt_at, claim_expires_at, created_at
                     )"""
            )
            connection.execute(
                "UPDATE schema_meta SET version=2,applied_at=? WHERE singleton=1",
                ("2026-09-08T00:01:00+00:00",),
            )
            connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()

    def build_next_release(self, marker: str) -> dict[str, str]:
        readme = self.source_root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + f"\n{marker}\n",
            encoding="utf-8",
        )
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", marker)
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        return self.build(marker.replace(" ", "-").lower())

    def test_install_is_isolated_paused_and_stages_independent_disabled_launchd(self) -> None:
        record = self.install()
        self.assertEqual(record["installed_mode"], "PAUSED")
        self.assertFalse(record["broker_accessed"])
        self.assertFalse(record["legacy_runtime_modified"])
        self.assertFalse(record["launchd"]["installer_called_launchctl"])
        self.assertEqual(record["launchd"]["actual_loaded_state"], "NOT_QUERIED")
        self.assertEqual(
            record["launchd"]["process_role"],
            "trading_coordinator_enqueue_only",
        )
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "legacy-unchanged")

        current = self.install_root / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(current.resolve(), Path(record["current_release"]))
        manifest = load_release_manifest(
            self.install_root / "release-manifest.json", verify_files_root=current
        )
        self.assertEqual(manifest["release_manifest_hash"], record["release_id"])
        self.assertFalse((self.install_root / "state/full-live.sqlite3").exists())

        plist_path = Path(record["launchd"]["staged_plist"])
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertIs(plist["Disabled"], True)
        self.assertNotIn("RunAtLoad", plist)
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertIn("serve", plist["ProgramArguments"])
        self.assertIn("--install-root", plist["ProgramArguments"])
        self.assertEqual(plist["ProgramArguments"][0], str(Path(sys.executable).resolve()))
        self.assertEqual(plist["ProgramArguments"][1:4], ["-I", "-S", "-B"])

        notification_metadata = record["launchd"]["notification_worker"]
        self.assertFalse(notification_metadata["installer_called_launchctl"])
        self.assertEqual(notification_metadata["actual_loaded_state"], "NOT_QUERIED")
        self.assertEqual(
            notification_metadata["process_role"],
            "independent_notification_outbox_delivery",
        )
        notification_path = Path(notification_metadata["staged_plist"])
        self.assertNotEqual(notification_path, plist_path)
        with notification_path.open("rb") as handle:
            notification_plist = plistlib.load(handle)
        self.assertEqual(
            notification_plist["Label"],
            "com.harpcity.trader-brain-full-live-notifications",
        )
        self.assertIs(notification_plist["Disabled"], True)
        self.assertNotIn("RunAtLoad", notification_plist)
        self.assertEqual(
            notification_plist["KeepAlive"], {"SuccessfulExit": False}
        )
        self.assertIn("notification-worker", notification_plist["ProgramArguments"])
        self.assertNotIn("serve", notification_plist["ProgramArguments"])
        self.assertEqual(
            notification_plist["ProgramArguments"][0],
            str(Path(sys.executable).resolve()),
        )
        self.assertEqual(
            notification_plist["ProgramArguments"][1:4], ["-I", "-S", "-B"]
        )
        self.assertNotEqual(
            notification_plist["StandardOutPath"], plist["StandardOutPath"]
        )
        self.assertNotEqual(
            notification_plist["StandardErrorPath"], plist["StandardErrorPath"]
        )

        completed = subprocess.run(
            [sys.executable, str(current / "scripts/titan-full-live"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        initialized = self.run_cli(
            "init-state",
            "--install-root",
            str(self.install_root),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        initialized_payload = json.loads(initialized.stdout)
        self.assertEqual(initialized_payload["runtime"]["mode"], "PAUSED")
        self.assertEqual(initialized_payload["runtime"]["authority_enabled"], 0)
        notification_test = self.run_cli(
            "notification-test",
            "--install-root",
            str(self.install_root),
            "--event-id",
            "paused-install-route-test",
        )
        self.assertEqual(notification_test.returncode, 0, notification_test.stderr)
        notification_test_payload = json.loads(notification_test.stdout)
        self.assertTrue(notification_test_payload["queued"])
        self.assertFalse(
            notification_test_payload["delivery_attempted_by_command"]
        )
        self.assertFalse((self.install_root / "state/notifications.jsonl").exists())
        connection = sqlite3.connect(self.install_root / "state/full-live.sqlite3")
        state = connection.execute(
            "SELECT state,attempt_count FROM notification_outbox"
        ).fetchone()
        connection.close()
        self.assertEqual(state, ("PENDING", 0))

        notification_worker = self.run_cli(
            "notification-worker",
            "--install-root",
            str(self.install_root),
            "--once",
        )
        self.assertEqual(
            notification_worker.returncode, 0, notification_worker.stderr
        )
        worker_payload = json.loads(notification_worker.stdout)
        self.assertEqual(worker_payload["sent"], 1)
        self.assertEqual(worker_payload["failed"], 0)
        self.assertTrue((self.install_root / "state/notifications.jsonl").is_file())
        doctor = self.run_cli(
            "doctor",
            "--install-root",
            str(self.install_root),
        )
        self.assertEqual(doctor.returncode, 2, doctor.stderr)
        doctor_payload = json.loads(doctor.stdout)
        self.assertFalse(doctor_payload["ready_for_owner_activation"])
        self.assertIn("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG", doctor_payload["blockers"])
        prepare = self.run_cli(
            "prepare-activation",
            "--install-root",
            str(self.install_root),
            "--ttl-seconds",
            "300",
        )
        self.assertEqual(prepare.returncode, 2)
        self.assertIn("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG", prepare.stderr)
        # The launcher must not contaminate an immutable release with bytecode.
        installer._verify_existing_release(
            Path(record["current_release"]),
            manifest,
            (self.install_root / "release-manifest.json").read_bytes(),
        )

    def test_reinstall_is_idempotent_only_while_database_proves_paused(self) -> None:
        first = self.install()
        database = self.install_root / "state/full-live.sqlite3"
        initialized = self.run_cli(
            "init-state",
            "--install-root",
            str(self.install_root),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        second = self.install()
        self.assertEqual(first["release_id"], second["release_id"])
        self.assertFalse(second["runtime_identity_migration"]["performed"])
        self.assertEqual(
            second["runtime_identity_migration"]["reason"],
            "IDENTITY_ALREADY_MATCHED",
        )
        self.assertFalse(
            second["runtime_identity_migration"]["schema_migration"]["performed"]
        )

        connection = sqlite3.connect(database)
        connection.execute("UPDATE runtime_identity SET mode='ACTIVE' WHERE singleton=1")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(installer.InstallError, "requires PAUSED"):
            self.install()

    def test_genuine_v1_upgrade_preserves_outbox_and_audit_before_rebind(self) -> None:
        first = self.install()
        initialized = self.run_cli(
            "init-state", "--install-root", str(self.install_root)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.downgrade_state_schema(1)
        database = self.install_root / "state/full-live.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            """INSERT INTO notification_outbox(
                   message_id,event_key,account_key,template,payload_json,
                   created_at,state,attempt_count
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "historical-v1-message",
                "historical-v1-event",
                "ending-7153",
                "SELL_EXIT_REVIEW_REQUIRED",
                '{"redacted":true}',
                "2026-09-08T12:00:00+00:00",
                "PENDING",
                2,
            ),
        )
        connection.execute(
            """INSERT INTO activation_records(
                   activation_id,account_key,record_hash,created_at,expires_at,
                   consumed_at,record_json
               ) VALUES(?,?,?,?,?,NULL,?)""",
            (
                "historical-v1-activation",
                "ending-7153",
                "b" * 64,
                "2026-09-08T12:00:00+00:00",
                "2026-09-08T12:05:00+00:00",
                "{}",
            ),
        )
        connection.commit()
        connection.close()
        old_pointer = (self.install_root / "current").resolve()
        next_result = self.build_next_release("genuine v1 migration fixture")

        upgraded = installer.install(
            Path(next_result["archive"]),
            self.install_root,
            python_executable=Path(sys.executable),
        )
        self.assertNotEqual(first["release_id"], upgraded["release_id"])
        self.assertNotEqual((self.install_root / "current").resolve(), old_pointer)
        migration = upgraded["runtime_identity_migration"]
        self.assertTrue(migration["performed"])
        self.assertEqual(migration["schema_migration"]["path"], [1, 2, 3])
        self.assertTrue(migration["schema_migration"]["performed"])
        self.assertEqual(migration["invalidated_activation_records"], 1)

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
        metadata = connection.execute(
            "SELECT version FROM schema_meta WHERE singleton=1"
        ).fetchone()
        message = connection.execute(
            "SELECT * FROM notification_outbox WHERE message_id=?",
            ("historical-v1-message",),
        ).fetchone()
        activation = connection.execute(
            "SELECT consumed_at FROM activation_records WHERE activation_id=?",
            ("historical-v1-activation",),
        ).fetchone()
        runtime = connection.execute(
            "SELECT generation,release_manifest_hash FROM runtime_identity WHERE singleton=1"
        ).fetchone()
        event_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM audit_events ORDER BY sequence"
            ).fetchall()
        ]
        worker_columns = [
            row[1]
            for row in connection.execute(
                'PRAGMA table_info("notification_worker_lease")'
            ).fetchall()
        ]
        connection.close()
        self.assertEqual(metadata["version"], 3)
        self.assertEqual(message["event_key"], "historical-v1-event")
        self.assertEqual(message["attempt_count"], 2)
        self.assertIsNone(message["claim_owner"])
        self.assertIsNone(message["delivery_route_id"])
        self.assertIsNotNone(activation["consumed_at"])
        self.assertEqual(runtime["generation"], 1)
        self.assertEqual(runtime["release_manifest_hash"], upgraded["release_id"])
        self.assertEqual(
            worker_columns, list(installer._STATE_V3_WORKER_COLUMNS)
        )
        self.assertEqual(
            event_types,
            [
                "RUNTIME_INITIALIZED_PAUSED",
                "STATE_SCHEMA_UPGRADED",
                "RUNTIME_RELEASE_IDENTITY_MIGRATED_PAUSED",
            ],
        )
        with LiveStateStore(database) as state:
            self.assertTrue(state.verify_event_chain()[0])

    def test_genuine_v2_schema_only_upgrade_invalidates_old_activation(self) -> None:
        self.install()
        initialized = self.run_cli(
            "init-state", "--install-root", str(self.install_root)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.downgrade_state_schema(2)
        database = self.install_root / "state/full-live.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            """INSERT INTO activation_records(
                   activation_id,account_key,record_hash,created_at,expires_at,
                   consumed_at,record_json
               ) VALUES(?,?,?,?,?,NULL,?)""",
            (
                "historical-v2-activation",
                "ending-7153",
                "c" * 64,
                "2026-09-08T12:00:00+00:00",
                "2026-09-08T12:05:00+00:00",
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO notification_outbox(
                   message_id,event_key,account_key,template,payload_json,
                   created_at,state,attempt_count,claim_owner,claim_expires_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "historical-v2-claimed-message",
                "historical-v2-claimed-event",
                "ending-7153",
                "BUY_REVIEW_REQUIRED",
                '{"redacted":true}',
                "2026-09-08T12:00:00+00:00",
                "PENDING",
                0,
                "unproven-old-worker",
                "2026-09-08T12:05:00+00:00",
            ),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            installer.InstallError, "notification claims released"
        ):
            self.install()
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            connection.execute(
                "SELECT generation FROM runtime_identity WHERE singleton=1"
            ).fetchone()[0],
            0,
        )
        connection.execute(
            """UPDATE notification_outbox
                  SET claim_owner=NULL,claim_expires_at=NULL
                WHERE message_id=?""",
            ("historical-v2-claimed-message",),
        )
        connection.commit()
        connection.close()

        upgraded = self.install()
        migration = upgraded["runtime_identity_migration"]
        self.assertTrue(migration["performed"])
        self.assertEqual(migration["reason"], "PAUSED_SCHEMA_AUTHORITY_REBOUND")
        self.assertEqual(migration["schema_migration"]["path"], [2, 3])
        self.assertEqual(migration["invalidated_activation_records"], 1)
        connection = sqlite3.connect(database)
        runtime = connection.execute(
            "SELECT generation,mode,authority_enabled FROM runtime_identity WHERE singleton=1"
        ).fetchone()
        activation = connection.execute(
            "SELECT consumed_at FROM activation_records WHERE activation_id=?",
            ("historical-v2-activation",),
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM audit_events ORDER BY sequence"
        ).fetchall()
        connection.close()
        self.assertEqual(runtime, (1, "PAUSED", 0))
        self.assertIsNotNone(activation[0])
        self.assertEqual(
            events,
            [
                ("RUNTIME_INITIALIZED_PAUSED",),
                ("STATE_SCHEMA_UPGRADED",),
                ("RUNTIME_RELEASE_IDENTITY_MIGRATED_PAUSED",),
            ],
        )

        repeated = self.install()
        self.assertFalse(repeated["runtime_identity_migration"]["performed"])
        connection = sqlite3.connect(database)
        self.assertEqual(
            connection.execute(
                "SELECT generation FROM runtime_identity WHERE singleton=1"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            3,
        )
        connection.close()

    def test_partial_or_unexpected_legacy_schema_is_rejected_without_mutation(self) -> None:
        self.install()
        initialized = self.run_cli(
            "init-state", "--install-root", str(self.install_root)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.downgrade_state_schema(1)
        database = self.install_root / "state/full-live.sqlite3"
        pointer = (self.install_root / "current").resolve()
        connection = sqlite3.connect(database)
        connection.execute(
            "ALTER TABLE notification_outbox ADD COLUMN claim_owner TEXT"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(installer.InstallError, "columns differ"):
            self.install()
        self.assertEqual((self.install_root / "current").resolve(), pointer)
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT version FROM schema_meta WHERE singleton=1"
            ).fetchone()[0],
            1,
        )
        worker = connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
                 WHERE type='table' AND name='notification_worker_lease'"""
        ).fetchone()[0]
        connection.close()
        self.assertEqual(worker, 0)

    def test_unexpected_v3_schema_object_is_rejected_before_pointer_change(self) -> None:
        self.install()
        initialized = self.run_cli(
            "init-state", "--install-root", str(self.install_root)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        database = self.install_root / "state/full-live.sqlite3"
        pointer = (self.install_root / "current").resolve()
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE VIEW unsupported_runtime_view AS SELECT mode FROM runtime_identity"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(installer.InstallError, "unsupported views"):
            self.install()
        self.assertEqual((self.install_root / "current").resolve(), pointer)
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            1,
        )
        connection.close()

    def test_paused_release_switch_migrates_identity_and_invalidates_authority(self) -> None:
        first = self.install()
        initialized = self.run_cli(
            "init-state",
            "--install-root",
            str(self.install_root),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        database = self.install_root / "state/full-live.sqlite3"
        old_release = Path(first["current_release"])
        old_manifest = json.loads(
            (self.install_root / "release-manifest.json").read_text(encoding="utf-8")
        )

        connection = sqlite3.connect(database)
        connection.execute(
            """INSERT INTO activation_records(
                   activation_id,account_key,record_hash,created_at,expires_at,
                   consumed_at,record_json
               ) VALUES(?,?,?,?,?,NULL,?)""",
            (
                "pending-prior-release-activation",
                "ending-7153",
                "a" * 64,
                "2026-09-08T12:00:00+00:00",
                "2026-09-08T12:05:00+00:00",
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO account_writer_lease(
                   account_key,owner_id,process_id,generation,acquired_at,
                   heartbeat_at,released_at
               ) VALUES(?,?,?,?,?,?,NULL)""",
            (
                "ending-7153",
                "prior-release-owner",
                4242,
                1,
                "2026-09-08T12:00:00+00:00",
                "2026-09-08T12:00:01+00:00",
            ),
        )
        connection.commit()
        connection.close()

        readme = self.source_root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8")
            + "\nRelease-identity migration fixture.\n",
            encoding="utf-8",
        )
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "next release fixture")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        next_result = self.build("next-release")

        with self.assertRaisesRegex(
            installer.InstallError, "all runtime leases released"
        ):
            installer.install(
                Path(next_result["archive"]),
                self.install_root,
                python_executable=Path(sys.executable),
            )
        self.assertEqual((self.install_root / "current").resolve(), old_release)
        connection = sqlite3.connect(database)
        unchanged = connection.execute(
            "SELECT release_manifest_hash,generation FROM runtime_identity WHERE singleton=1"
        ).fetchone()
        connection.close()
        self.assertEqual(unchanged, (old_manifest["release_manifest_hash"], 0))

        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE account_writer_lease SET released_at=? WHERE account_key=?",
            ("2026-09-08T12:00:02+00:00", "ending-7153"),
        )
        connection.execute(
            """INSERT INTO notification_worker_lease(
                   account_key,worker_id,process_id,generation,route_id,provider,
                   destination_fingerprint,route_version,started_at,heartbeat_at,
                   last_sent_count,last_failed_count,released_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                "ending-7153",
                "prior-notification-worker",
                4343,
                1,
                "route-test",
                "test-provider",
                "d" * 64,
                "v1",
                "2026-09-08T12:00:00+00:00",
                "2026-09-08T12:00:01+00:00",
                0,
                0,
            ),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            installer.InstallError, "all runtime leases released"
        ):
            installer.install(
                Path(next_result["archive"]),
                self.install_root,
                python_executable=Path(sys.executable),
            )
        self.assertEqual((self.install_root / "current").resolve(), old_release)

        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE notification_worker_lease SET released_at=? WHERE account_key=?",
            ("2026-09-08T12:00:03+00:00", "ending-7153"),
        )
        connection.commit()
        connection.close()

        migrated = installer.install(
            Path(next_result["archive"]),
            self.install_root,
            python_executable=Path(sys.executable),
        )
        migration = migrated["runtime_identity_migration"]
        next_manifest = json.loads(
            (self.install_root / "release-manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(migration["required"])
        self.assertTrue(migration["performed"])
        self.assertEqual(migration["reason"], "PAUSED_IDENTITY_REBOUND")
        self.assertEqual(migration["invalidated_activation_records"], 1)
        self.assertEqual(migration["generation"], 1)
        self.assertNotEqual(migrated["release_id"], first["release_id"])
        self.assertEqual(
            (self.install_root / "current").resolve(),
            Path(migrated["current_release"]),
        )

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        runtime = connection.execute(
            "SELECT * FROM runtime_identity WHERE singleton=1"
        ).fetchone()
        activation = connection.execute(
            "SELECT consumed_at FROM activation_records WHERE activation_id=?",
            ("pending-prior-release-activation",),
        ).fetchone()
        event = connection.execute(
            """SELECT event_type,entity_id,payload_json
                 FROM audit_events ORDER BY sequence DESC LIMIT 1"""
        ).fetchone()
        connection.close()
        self.assertEqual(runtime["release_manifest_hash"], migrated["release_id"])
        self.assertEqual(runtime["config_hash"], next_manifest["config_hash"])
        self.assertEqual(runtime["policy_hash"], next_manifest["policy_hash"])
        self.assertEqual(runtime["mode"], "PAUSED")
        self.assertEqual(runtime["authority_enabled"], 0)
        self.assertIsNone(runtime["activated_at"])
        self.assertEqual(runtime["generation"], 1)
        self.assertIsNotNone(activation["consumed_at"])
        self.assertEqual(event["event_type"], "RUNTIME_RELEASE_IDENTITY_MIGRATED_PAUSED")
        self.assertEqual(event["entity_id"], migrated["release_id"])
        event_payload = json.loads(event["payload_json"])
        self.assertEqual(event_payload["invalidated_activation_records"], 1)
        self.assertTrue(
            event_payload["prior_release_controls_invalidated_by_release_binding"]
        )
        with LiveStateStore(database) as state:
            self.assertTrue(state.verify_event_chain()[0])

        reinitialized = self.run_cli(
            "init-state",
            "--install-root",
            str(self.install_root),
        )
        self.assertEqual(reinitialized.returncode, 0, reinitialized.stderr)
        self.assertFalse(json.loads(reinitialized.stdout)["created"])

    def test_existing_release_contamination_is_rejected(self) -> None:
        record = self.install()
        injected = Path(record["current_release"]) / "src/titan_brain/live/injected.py"
        injected.write_text("raise RuntimeError('unmanifested')\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "unmanifested file"):
            self.install()

    def test_account_writer_lock_blocks_release_switch(self) -> None:
        lock = AccountWriterLock(
            self.fixed_lock_directory,
            "ending-7153",
            owner_id="running-service",
        )
        lock.acquire()
        try:
            with self.assertRaisesRegex(installer.InstallError, "interlock is held"):
                self.install()
            self.assertFalse((self.install_root / "current").exists())
        finally:
            lock.release()

    def test_parallel_install_roots_share_one_account_global_interlock(self) -> None:
        first = self.base / "installation-a/full-live"
        second = self.base / "unrelated/installation-b/full-live"
        for root in (first, second):
            (root / "control").mkdir(parents=True)

        with installer._deployment_interlock(first, "ending-7153"):
            expected = self.fixed_lock_directory / (
                "account-"
                + hashlib.sha256(b"ending-7153").hexdigest()[:24]
                + ".writer.lock"
            )
            self.assertTrue(expected.is_file())
            self.assertNotEqual(expected.parent, first.parent)
            self.assertNotEqual(expected.parent, second.parent)
            with self.assertRaisesRegex(installer.InstallError, "interlock is held"):
                with installer._deployment_interlock(second, "ending-7153"):
                    self.fail("parallel install acquired a partitioned writer lock")

    def test_symlinked_install_subdirectory_is_rejected(self) -> None:
        self.install_root.mkdir(parents=True)
        outside = self.base / "outside"
        outside.mkdir()
        (self.install_root / "launchd").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "not a real directory"):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_checksum_corruption_and_broad_target_fail_closed(self) -> None:
        archive = Path(self.result["archive"])
        corrupt = self.base / archive.name
        payload = bytearray(archive.read_bytes())
        payload[len(payload) // 2] ^= 1
        corrupt.write_bytes(payload)
        with self.assertRaisesRegex(installer.InstallError, "checksum mismatch"):
            installer.install(
                corrupt,
                self.install_root,
                expected_archive_sha256=self.result["archive_sha256"],
            )
        with self.assertRaisesRegex(installer.InstallError, "must be under"):
            installer.install(
                archive,
                self.application_root,
                python_executable=Path(sys.executable),
            )
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "legacy-unchanged")

    def test_installer_has_no_service_or_process_launch_path(self) -> None:
        source = (ROOT / "scripts/install_full_live_paused.py").read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("Popen", source)
        template = plistlib.loads(
            (ROOT / "deployment/com.harpcity.trader-brain-full-live.plist.in").read_bytes()
        )
        self.assertIs(template["Disabled"], True)
        self.assertNotIn("RunAtLoad", template)
        notification_template = plistlib.loads(
            (
                ROOT
                / "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
            ).read_bytes()
        )
        self.assertIs(notification_template["Disabled"], True)
        self.assertNotIn("RunAtLoad", notification_template)
        self.assertNotEqual(notification_template["Label"], template["Label"])
        self.assertIn(
            "notification-worker", notification_template["ProgramArguments"]
        )
        self.assertIn("serve", template["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
