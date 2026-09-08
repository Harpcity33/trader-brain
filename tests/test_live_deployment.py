from __future__ import annotations

import hashlib
import importlib.util
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

from titan_brain.live.release import (
    MANIFEST_SCHEMA,
    calculate_manifest_hash,
    load_release_manifest,
)
from titan_brain.live.policy import PolicyBundle
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
        self.result = self.build()
        self.application_root = self.base / "Application Support/Titan Momentum"
        self.install_root = self.application_root / "full-live"
        legacy = self.application_root / "runtime/legacy-sentinel.txt"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy-unchanged", encoding="utf-8")
        self.legacy = legacy

    def tearDown(self) -> None:
        super().tearDown()

    def install(self):
        return installer.install(
            Path(self.result["archive"]),
            self.install_root,
            python_executable=Path(sys.executable),
        )

    def test_install_is_isolated_paused_and_stages_disabled_launchd(self) -> None:
        record = self.install()
        self.assertEqual(record["installed_mode"], "PAUSED")
        self.assertFalse(record["broker_accessed"])
        self.assertFalse(record["legacy_runtime_modified"])
        self.assertFalse(record["launchd"]["installer_called_launchctl"])
        self.assertEqual(record["launchd"]["actual_loaded_state"], "NOT_QUERIED")
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

        completed = subprocess.run(
            [sys.executable, str(current / "scripts/titan-full-live"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        initialized = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "init-state",
                "--install-root",
                str(self.install_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        initialized_payload = json.loads(initialized.stdout)
        self.assertEqual(initialized_payload["runtime"]["mode"], "PAUSED")
        self.assertEqual(initialized_payload["runtime"]["authority_enabled"], 0)
        doctor = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "doctor",
                "--install-root",
                str(self.install_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(doctor.returncode, 2, doctor.stderr)
        doctor_payload = json.loads(doctor.stdout)
        self.assertFalse(doctor_payload["ready_for_owner_activation"])
        self.assertIn("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG", doctor_payload["blockers"])
        prepare = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "prepare-activation",
                "--install-root",
                str(self.install_root),
                "--ttl-seconds",
                "300",
            ],
            check=False,
            capture_output=True,
            text=True,
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
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE runtime_identity (singleton INTEGER PRIMARY KEY, mode TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO runtime_identity(singleton, mode) VALUES (1, 'PAUSED')")
        connection.commit()
        connection.close()
        second = self.install()
        self.assertEqual(first["release_id"], second["release_id"])

        connection = sqlite3.connect(database)
        connection.execute("UPDATE runtime_identity SET mode='ACTIVE' WHERE singleton=1")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(installer.InstallError, "requires PAUSED"):
            self.install()

    def test_existing_release_contamination_is_rejected(self) -> None:
        record = self.install()
        injected = Path(record["current_release"]) / "src/titan_brain/live/injected.py"
        injected.write_text("raise RuntimeError('unmanifested')\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "unmanifested file"):
            self.install()

    def test_account_writer_lock_blocks_release_switch(self) -> None:
        lock_directory = self.install_root / "state/locks"
        lock = AccountWriterLock(lock_directory, "ending-7153", owner_id="running-service")
        lock.acquire()
        try:
            with self.assertRaisesRegex(installer.InstallError, "interlock is held"):
                self.install()
            self.assertFalse((self.install_root / "current").exists())
        finally:
            lock.release()

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


if __name__ == "__main__":
    unittest.main()
