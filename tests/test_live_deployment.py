from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

from titan_brain.live.release import (
    MANIFEST_SCHEMA,
    calculate_manifest_hash,
    load_release_manifest,
)
from titan_brain.live import cli as live_cli
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.provider_profile import (
    IbkrLocalProviderProfile,
    ProviderProfileError,
)
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import (
    AccountWriterLock,
    attended_coordinator_lock_key,
)


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
validator = load_script("validate_repository", "scripts/validate_repository.py")


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

    def build(
        self,
        output: str = "dist",
        *,
        config_path: str = "config/full_live.json",
    ) -> dict[str, str]:
        return builder.build(
            self.source_root,
            self.base / output,
            source_revision=self.source_revision,
            config_path=config_path,
        )


class FullLiveReleaseTests(GitReleaseFixture, unittest.TestCase):

    def test_approved_policy_artifacts_are_bound_and_packaged(self) -> None:
        result = self.build(config_path="config/full_live_ibkr.json")
        manifest = load_release_manifest(result["manifest"], verify_files_root=self.source_root)
        records = {item["path"]: item for item in manifest["files"]}
        config = json.loads((self.source_root / "config/full_live_ibkr.json").read_text())
        approval = config["owner_policy_approval"]
        self.assertEqual(records[approval["proposal_path"]]["sha256"], approval["proposal_sha256"])
        self.assertEqual(records[approval["approval_record_path"]]["sha256"], approval["approval_record_sha256"])
        amendment = config["owner_risk_policy_amendment"]
        self.assertEqual(records[amendment["amendment_path"]]["sha256"], amendment["amendment_sha256"])
        self.assertEqual(config["execution"]["per_mutation_user_confirmation_required"], True)

    def test_flex_reporting_code_and_non_authorizing_setup_are_packaged(self) -> None:
        result = self.build(config_path="config/full_live_ibkr.json")
        manifest = load_release_manifest(result["manifest"], verify_files_root=self.source_root)
        paths = {item["path"] for item in manifest["files"]}
        setup = "validation/full-live/2026-09-15/IBKR_FLEX_DAILY_EVIDENCE_SETUP_2026-09-15.md"
        self.assertIn("src/titan_brain/live/broker/ibkr_flex.py", paths)
        self.assertIn(setup, paths)
        self.assertIn(setup, installer._FIXED_RELEASE_PATHS)
        self.assertEqual(manifest["default_mode"], "PAUSED")
        config = json.loads((self.source_root / "config/full_live_ibkr.json").read_text())
        self.assertNotEqual(config["owner_policy_approval"]["approval_record_path"], setup)
        self.assertNotEqual(config["owner_risk_policy_amendment"]["amendment_path"], setup)

    def test_session_measurement_amendment_and_probe_are_packaged_without_selection(self) -> None:
        result = self.build(config_path="config/full_live_ibkr.json")
        manifest = load_release_manifest(result["manifest"], verify_files_root=self.source_root)
        records = {item["path"]: item for item in manifest["files"]}
        limits_path = "config/risk_limits_ibkr_session_trading.json"
        proposed = json.loads((self.source_root / limits_path).read_text())
        amendment = proposed["owner_risk_amendment"]
        self.assertEqual(records[amendment["amendment_path"]]["sha256"], amendment["amendment_sha256"])
        self.assertIn(amendment["amendment_path"], installer._FIXED_RELEASE_PATHS)
        self.assertIn("scripts/titan-session-inputs-probe", records)
        self.assertIn("scripts/titan-session-inputs-probe", installer._FIXED_RELEASE_PATHS)
        for module in ("session_trading_calculation.py", "session_trading_store.py", "session_trading_rehearsal.py"):
            self.assertIn("src/titan_brain/live/" + module, records)
        config = json.loads((self.source_root / "config/full_live_ibkr.json").read_text())
        self.assertNotEqual(config["risk"]["limits_path"], limits_path)
        self.assertEqual(manifest["default_mode"], "PAUSED")
        self.assertFalse(config["execution"]["local_mutation_interlock_enabled"])

    def test_committed_daily_percentage_amendment_tampering_blocks_release(self) -> None:
        path = self.source_root / "validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md"
        path.write_text(path.read_text() + "\nUnapproved alteration.\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "alter amendment without binding")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(ValueError, "owner risk policy amendment artifact binding"):
            self.build(config_path="config/full_live_ibkr.json")

    def test_committed_approval_artifact_tampering_blocks_release(self) -> None:
        approval_path = self.source_root / "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md"
        approval_path.write_text(approval_path.read_text() + "\nUnapproved amendment.\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "alter approval without a new binding")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(ValueError, "owner policy approval artifact binding"):
            self.build(config_path="config/full_live_ibkr.json")

    def test_approval_path_outside_packaged_artifacts_blocks_release(self) -> None:
        config_path = self.source_root / "config/full_live_ibkr.json"
        config = json.loads(config_path.read_text())
        config["owner_policy_approval"]["proposal_path"] = "../../owner-approval.md"
        config_path.write_text(json.dumps(config))
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "unpackaged approval binding")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(ValueError, "owner policy approval artifact binding"):
            self.build(config_path="config/full_live_ibkr.json")

    def test_repository_validator_protects_every_target_account_suffix(self) -> None:
        account_pattern = validator.prohibited_content_patterns()[
            "unmasked_target_broker_account_number"
        ]
        for suffix in validator.PROTECTED_ACCOUNT_LAST4:
            synthetic_unmasked = ("9" * 5) + suffix
            self.assertIsNotNone(account_pattern.search(synthetic_unmasked))
            self.assertIsNone(account_pattern.search(f"ending-{suffix}"))
            self.assertIsNone(account_pattern.search(f"****{suffix}"))

    def test_ibkr_profile_reserves_distinct_attended_read_client_id(self) -> None:
        config = json.loads(
            (ROOT / "config/full_live_ibkr.json").read_text(encoding="utf-8")
        )
        profile = IbkrLocalProviderProfile.from_config(config)
        assert profile is not None
        self.assertEqual(profile.read_client_id, 19735)
        self.assertEqual(profile.command_client_id, 19736)
        self.assertEqual(profile.attended_read_client_id, 19737)
        self.assertEqual(profile.for_attended_command().read_client_id, 19737)
        self.assertEqual(
            profile.sdk_inventory_hash,
            "3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc",
        )

    def test_ibkr_profile_refuses_full_account_identifier_field(self) -> None:
        config = json.loads(
            (ROOT / "config/full_live_ibkr.json").read_text(encoding="utf-8")
        )
        config["account"]["account_number"] = "U0000000"
        with self.assertRaisesRegex(ProviderProfileError, "unapproved field"):
            IbkrLocalProviderProfile.from_config(config)

    def test_installer_accepts_read_zero_without_selecting_it(self) -> None:
        config = json.loads((ROOT / "config/full_live_ibkr.json").read_text())
        self.assertEqual(config["local_provider_profile"]["read_client_id"], 19735)
        config["local_provider_profile"]["read_client_id"] = 0
        requirements = installer._ibkr_profile_requirements(config)
        self.assertEqual(requirements["read_client_id"], 0)
        self.assertEqual(requirements["command_client_id"], 19736)

    def test_installer_rejects_unsafe_client_id_pairs(self) -> None:
        for read_id, command_id in ((0, 0), (1, 1), (2, 1), (0, 2147483647), (-1, 1), (False, 1)):
            with self.subTest(read_id=read_id, command_id=command_id):
                config = json.loads((ROOT / "config/full_live_ibkr.json").read_text())
                config["local_provider_profile"]["read_client_id"] = read_id
                config["local_provider_profile"]["command_client_id"] = command_id
                with self.assertRaisesRegex(installer.InstallError, "profile is invalid"):
                    installer._ibkr_profile_requirements(config)

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
        self.assertIn("src/titan_brain/live/attended_control.py", inventory)
        self.assertIn("scripts/install_full_live_paused.py", inventory)
        self.assertIn("scripts/titan-full-live", inventory)
        installer_record = next(
            item
            for item in manifest["files"]
            if item["path"] == "scripts/install_full_live_paused.py"
        )
        self.assertEqual(installer_record["mode"], "0755")
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

    def test_manifest_rejects_missing_internal_runtime_import(self) -> None:
        result = self.build()
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        payloads = {
            entry["path"]: (self.source_root / entry["path"]).read_bytes()
            for entry in manifest["files"]
        }
        missing = "src/titan_brain/live/activation.py"
        manifest["files"] = [
            entry for entry in manifest["files"] if entry["path"] != missing
        ]
        payloads.pop(missing)
        manifest["source_tree"] = hashlib.sha256(
            builder.canonical_json(manifest["files"])
        ).hexdigest()
        body = dict(manifest)
        body.pop("release_manifest_hash")
        manifest["release_manifest_hash"] = hashlib.sha256(
            builder.canonical_json(body)
        ).hexdigest()
        with self.assertRaisesRegex(
            installer.InstallError, "internal import is missing"
        ):
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
            "flex-setup-status",
            "flex-enroll",
            "flex-probe",
        ):
            self.assertIn(command, completed.stdout)

    def test_flex_reporting_commands_do_not_load_the_tws_sdk(self) -> None:
        namespace = runpy.run_path(
            str(ROOT / "scripts/titan-full-live"),
            run_name="titan_full_live_flex_launcher_test",
        )
        flex_commands = namespace["_FLEX_REPORTING_COMMANDS"]
        requires_sdk = namespace["_requires_installed_broker_sdk"]
        self.assertEqual(
            flex_commands,
            frozenset({"flex-enroll", "flex-probe", "flex-setup-status"}),
        )
        for command in flex_commands:
            self.assertFalse(requires_sdk([command, "--install-root", "/unused"]))
        self.assertTrue(
            requires_sdk(["provider-status", "--install-root", "/unused"])
        )
        self.assertTrue(
            requires_sdk(["attended-review", "--install-root", "/unused"])
        )

    def test_launcher_recognizes_attended_command_only_in_command_position(self) -> None:
        namespace = runpy.run_path(
            str(ROOT / "scripts/titan-full-live"),
            run_name="titan_full_live_launcher_test",
        )
        selector = namespace["_release_bound_command_inputs"]
        self.assertIsNone(
            selector(
                release_root=ROOT / "does-not-need-to-exist",
                install_root=ROOT / "also-does-not-need-to-exist",
                full_live_config_name="full_live.json",
                release_manifest_hash=None,
                arguments=("status", "--install-root", "attended-review"),
            )
        )

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

    def test_untracked_test_file_is_rejected(self) -> None:
        uncommitted = self.source_root / "tests/test_uncommitted.py"
        uncommitted.parent.mkdir(parents=True, exist_ok=True)
        uncommitted.write_text("raise AssertionError('not reviewed')\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be committed or removed"):
            self.build()

    def test_untracked_non_release_script_is_rejected_even_when_ignored(self) -> None:
        exclude = self.source_root / ".git/info/exclude"
        exclude.write_text("scripts/uncommitted-helper.py\n", encoding="utf-8")
        uncommitted = self.source_root / "scripts/uncommitted-helper.py"
        uncommitted.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.assertEqual(
            self.git("ls-files", "--others", "--exclude-standard").stdout,
            "",
        )
        with self.assertRaisesRegex(ValueError, "must be committed or removed"):
            self.build()

    def test_output_directory_must_be_outside_repository(self) -> None:
        for output in (self.source_root, self.source_root / "dist/full-live"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "must be outside"):
                    builder.build(
                        self.source_root,
                        output,
                        source_revision=self.source_revision,
                    )

    def test_archive_payloads_are_bound_to_exact_head_blobs(self) -> None:
        # Even a worktree change deliberately hidden from Git status cannot
        # contaminate the archive: payloads come from the exact HEAD tree.
        readme = self.source_root / "README.md"
        self.git("update-index", "--assume-unchanged", "README.md")
        readme.write_text(
            "mutable worktree bytes that must never be packaged\n", encoding="utf-8"
        )
        self.assertEqual(self.git("status", "--short").stdout, "")
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
                if relative == "README.md":
                    self.assertNotEqual(archived, readme.read_bytes())


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
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
            python_executable=Path(sys.executable),
        )

    def write_forged_archive(
        self,
        name: str,
        manifest: dict[str, object],
        payloads: dict[str, bytes],
    ) -> Path:
        descriptor = dict(manifest)
        descriptor.pop("release_manifest_hash", None)
        manifest["release_manifest_hash"] = hashlib.sha256(
            builder.canonical_json(descriptor)
        ).hexdigest()
        manifest_bytes = builder.canonical_json(manifest) + b"\n"
        archive = self.base / f"{name}.tar.gz"
        builder.write_archive(archive, manifest, manifest_bytes, payloads)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        Path(str(archive) + ".sha256").write_text(
            f"{digest}  {archive.name}\n", encoding="ascii"
        )
        return archive

    def test_install_record_attests_running_installer_and_interpreter(self) -> None:
        record = self.install()
        expected = hashlib.sha256(
            (ROOT / "scripts/install_full_live_paused.py").read_bytes()
        ).hexdigest()
        self.assertEqual(record["installer_sha256"], expected)
        self.assertEqual(record["installer_schema"], installer.INSTALL_SCHEMA)
        self.assertEqual(record["installer_source_commit"], self.source_revision)
        self.assertEqual(
            record["source_provenance"],
            {
                "repository": str(self.source_root.resolve()),
                "expected_revision": self.source_revision,
                "verified_release_files": len(
                    json.loads(Path(self.result["manifest"]).read_text())["files"]
                ),
                "git_replacement_objects_disabled": True,
            },
        )
        self.assertEqual(
            Path(record["installer_python_executable"]),
            Path(sys.executable).resolve(),
        )
        self.assertEqual(
            Path(record["runtime_python_executable"]),
            Path(sys.executable).resolve(),
        )
        self.assertIsNone(record["sdk_receipt_sha256"])

    def test_mismatched_running_installer_is_rejected_before_install_mutation(self) -> None:
        other = self.base / "unattested-installer.py"
        other.write_text("raise SystemExit(1)\n", encoding="utf-8")
        other.chmod(0o755)
        with mock.patch.object(installer, "__file__", str(other)):
            with self.assertRaisesRegex(
                installer.InstallError, "does not match the release-attested installer"
            ):
                self.install()
        self.assertFalse(self.install_root.exists())

    def test_install_requires_explicit_trusted_repository_and_revision(self) -> None:
        with self.assertRaisesRegex(TypeError, "trusted_source_root"):
            installer.install(Path(self.result["archive"]), self.install_root)
        self.assertFalse(self.install_root.exists())

    def test_expected_revision_must_match_archive_before_install_mutation(self) -> None:
        with self.assertRaisesRegex(installer.InstallError, "source_commit"):
            installer.install(
                Path(self.result["archive"]),
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision="f" * 40,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_recomputed_forged_archive_payload_is_rejected_by_git(self) -> None:
        manifest, _manifest_bytes, payloads = installer._read_archive(
            Path(self.result["archive"]).read_bytes()
        )
        forged = payloads["README.md"] + b"\nforged but internally rehashed\n"
        payloads["README.md"] = forged
        for record in manifest["files"]:
            if record["path"] == "README.md":
                record["size"] = len(forged)
                record["sha256"] = hashlib.sha256(forged).hexdigest()
        manifest["source_tree"] = hashlib.sha256(
            builder.canonical_json(manifest["files"])
        ).hexdigest()
        archive = self.write_forged_archive("rehashed-payload", manifest, payloads)
        installer._verify_manifest(manifest, payloads)

        with self.assertRaisesRegex(
            installer.InstallError, "differs from trusted Git commit: README.md"
        ):
            installer.install(
                archive,
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_recomputed_forged_manifest_metadata_is_rejected_by_git(self) -> None:
        manifest, _manifest_bytes, payloads = installer._read_archive(
            Path(self.result["archive"]).read_bytes()
        )
        manifest["policy_hash"] = "f" * 64
        archive = self.write_forged_archive("rehashed-metadata", manifest, payloads)
        installer._verify_manifest(manifest, payloads)

        with self.assertRaisesRegex(
            installer.InstallError, "descriptor differs from trusted Git commit"
        ):
            installer.install(
                archive,
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_recomputed_forged_manifest_mode_is_rejected_by_git(self) -> None:
        manifest, _manifest_bytes, payloads = installer._read_archive(
            Path(self.result["archive"]).read_bytes()
        )
        for record in manifest["files"]:
            if record["path"] == "README.md":
                record["mode"] = "0755"
        manifest["source_tree"] = hashlib.sha256(
            builder.canonical_json(manifest["files"])
        ).hexdigest()
        archive = self.write_forged_archive("rehashed-mode", manifest, payloads)
        installer._verify_manifest(manifest, payloads)

        with self.assertRaisesRegex(
            installer.InstallError, "mode differs from trusted Git commit: README.md"
        ):
            installer.install(
                archive,
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_missing_trusted_repository_is_rejected_before_install_mutation(self) -> None:
        with self.assertRaisesRegex(installer.InstallError, "repository is missing"):
            installer.install(
                Path(self.result["archive"]),
                self.install_root,
                trusted_source_root=self.base / "missing-trusted-repository",
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_git_replace_cannot_substitute_expected_source_commit(self) -> None:
        original_revision = self.source_revision
        manifest, _manifest_bytes, payloads = installer._read_archive(
            Path(self.result["archive"]).read_bytes()
        )
        readme = self.source_root / "README.md"
        forged = readme.read_bytes() + b"\nreplacement-object payload\n"
        readme.write_bytes(forged)
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "replacement source")
        replacement_revision = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("replace", original_revision, replacement_revision)

        payloads["README.md"] = forged
        for record in manifest["files"]:
            if record["path"] == "README.md":
                record["size"] = len(forged)
                record["sha256"] = hashlib.sha256(forged).hexdigest()
        manifest["source_tree"] = hashlib.sha256(
            builder.canonical_json(manifest["files"])
        ).hexdigest()
        archive = self.write_forged_archive("git-replace-forgery", manifest, payloads)

        with self.assertRaisesRegex(
            installer.InstallError, "differs from trusted Git commit: README.md"
        ):
            installer.install(
                archive,
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision=original_revision,
                python_executable=Path(sys.executable),
            )
        self.assertFalse(self.install_root.exists())

    def test_git_environment_cannot_redirect_trusted_source_queries(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.base / "untrusted.git"),
                "GIT_OBJECT_DIRECTORY": str(self.base / "untrusted-objects"),
                "GIT_REPLACE_REF_BASE": "refs/untrusted/replace/",
            },
        ):
            record = self.install()
        self.assertEqual(
            record["source_provenance"]["expected_revision"],
            self.source_revision,
        )

    def make_ibkr_sdk_venv(self) -> Path:
        venv = self.base / "authorized-ibkr-sdk-venv"
        site = (
            venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        files = {
            "ibapi/__init__.py": '__version__ = "10.50.2"\n',
            "ibapi/client.py": "class EClient: pass\n",
            "ibapi-10.50.2.dist-info/METADATA": (
                "Metadata-Version: 2.4\nName: ibapi\nVersion: 10.50.2\n"
                "Requires-Dist: protobuf==5.29.5\n"
            ),
            "google/protobuf/__init__.py": '__version__ = "5.29.5"\n',
            "google/protobuf/message.py": "class Message: pass\n",
            "google/_upb/_message.abi3.so": "synthetic-test-binary",
            "protobuf-5.29.5.dist-info/METADATA": (
                "Metadata-Version: 2.4\nName: protobuf\nVersion: 5.29.5\n"
            ),
        }
        for relative, body in files.items():
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return venv

    def pin_fixture_sdk_inventory(self, venv: Path) -> str:
        site = (
            venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        records = installer._sdk_source_inventory(
            site,
            ibapi_version="10.50.2",
            protobuf_version="5.29.5",
        )
        inventory_hash = hashlib.sha256(
            installer.canonical_json(records)
        ).hexdigest()
        config_path = self.source_root / "config/full_live_ibkr.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["local_provider_profile"]["sdk"][
            "expected_inventory_sha256"
        ] = inventory_hash
        config_path.write_text(
            json.dumps(config, indent=2) + "\n",
            encoding="utf-8",
        )
        self.git("add", "config/full_live_ibkr.json")
        self.git("commit", "--quiet", "-m", "pin synthetic SDK inventory")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        return inventory_hash

    def test_ibkr_profile_installs_only_to_isolated_root_with_attested_sdk(self) -> None:
        sdk_venv = self.make_ibkr_sdk_venv()
        unpinned = self.build(
            "ibkr-profile-unpinned",
            config_path="config/full_live_ibkr.json",
        )
        ibkr_root = self.application_root / "full-live-ibkr-ending-3103"
        with self.assertRaisesRegex(
            installer.InstallError, "does not match the release-pinned dependency"
        ):
            installer.install(
                Path(unpinned["archive"]),
                ibkr_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
                ibkr_sdk_venv=sdk_venv,
            )
        self.assertFalse((ibkr_root / "current").exists())

        pinned_inventory = self.pin_fixture_sdk_inventory(sdk_venv)
        result = self.build(
            "ibkr-profile",
            config_path="config/full_live_ibkr.json",
        )
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_path"], "config/full_live_ibkr.json")
        self.assertEqual(
            manifest["install_subtree"],
            "Application Support/Titan Momentum/full-live-ibkr-ending-3103",
        )
        with self.assertRaisesRegex(installer.InstallError, "signed subtree"):
            installer.install(
                Path(result["archive"]),
                self.install_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
                ibkr_sdk_venv=sdk_venv,
            )
        record = installer.install(
            Path(result["archive"]),
            ibkr_root,
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
            python_executable=Path(sys.executable),
            ibkr_sdk_venv=sdk_venv,
        )
        self.assertEqual(record["deployment_profile_id"], "ibkr-local-live-ending-3103-v1")
        self.assertEqual(record["config_path"], "config/full_live_ibkr.json")
        self.assertEqual(
            record["launchd"]["label"],
            "com.harpcity.trader-brain-full-live-ibkr-3103",
        )
        self.assertEqual(
            record["launchd"]["notification_worker"]["label"],
            "com.harpcity.trader-brain-full-live-ibkr-3103-notifications",
        )
        external = record["external_dependencies"]
        self.assertEqual(external["kind"], "ibkr_release_pinned_sdk_snapshot")
        self.assertEqual(external["inventory_hash"], pinned_inventory)
        self.assertFalse(external["source_venv_persisted"])
        current = Path(record["current_release"])
        policy = PolicyBundle.load(
            current,
            config_relative="config/full_live_ibkr.json",
        )
        self.assertEqual(policy.account_key, "ibkr-live-ending-3103")
        self.assertEqual(policy.account_last4, "3103")
        self.assertEqual(policy.config["local_provider_profile"]["endpoint"]["port"], 4001)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = live_cli.main(
                ["local-profile-status", "--install-root", str(ibkr_root)]
            )
        self.assertEqual(code, 0)
        status = json.loads(stdout.getvalue())
        self.assertEqual(status["sdk"]["status"], "ATTESTED")
        self.assertEqual(status["account_masked"], "ending-3103")
        self.assertEqual(status["endpoint"], {
            "host": "127.0.0.1",
            "loopback_only": True,
            "network_probe_performed": False,
            "port": 4001,
        })
        self.assertEqual(status["client_ids"], {
            "read": 19735,
            "command": 19736,
            "isolated": True,
        })
        launched = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "local-profile-status",
                "--install-root",
                str(ibkr_root),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        launched_status = json.loads(launched.stdout)
        self.assertEqual(launched_status["account_key"], "ibkr-live-ending-3103")
        self.assertEqual(launched_status["account_masked"], "ending-3103")
        self.assertEqual(launched_status["endpoint"]["port"], 4001)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            attended_code = live_cli.main(
                [
                    "attended-review",
                    "--install-root",
                    str(ibkr_root),
                    "--purpose",
                    "entry",
                    "--side",
                    "buy",
                    "--symbol",
                    "XYZ",
                    "--quantity",
                    "10",
                    "--order-type",
                    "limit",
                    "--time-in-force",
                    "gfd",
                    "--limit-price",
                    "10.00",
                ]
            )
        self.assertEqual(attended_code, 2)
        self.assertIn("IBKR_ATTENDED_SIGNED_TRANSPORT_NOT_SUPPORTED", stderr.getvalue())
        self.assertFalse((self.install_root / "state/full-live.sqlite3").exists())

    def test_ibkr_sdk_snapshot_tamper_fails_closed(self) -> None:
        sdk_venv = self.make_ibkr_sdk_venv()
        pinned_inventory = self.pin_fixture_sdk_inventory(sdk_venv)
        result = self.build(
            "ibkr-profile-tamper",
            config_path="config/full_live_ibkr.json",
        )
        ibkr_root = self.application_root / "full-live-ibkr-ending-3103"
        record = installer.install(
            Path(result["archive"]),
            ibkr_root,
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
            python_executable=Path(sys.executable),
            ibkr_sdk_venv=sdk_venv,
        )
        self.assertEqual(
            record["external_dependencies"]["inventory_hash"],
            pinned_inventory,
        )
        initialized = self.run_cli(
            "init-state", "--install-root", str(ibkr_root)
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        receipt = json.loads(
            (ibkr_root / "control/ibkr-sdk-attestation.json").read_text(
                encoding="utf-8"
            )
        )
        target = ibkr_root / receipt["import_root"] / receipt["files"][0]["path"]
        target.chmod(0o644)
        target.write_text("tampered\n", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = live_cli.main(
                ["local-profile-status", "--install-root", str(ibkr_root)]
            )
        self.assertEqual(code, 2)
        status = json.loads(stdout.getvalue())
        self.assertEqual(status["sdk"]["status"], "BLOCKED")
        self.assertEqual(
            record["external_dependencies"]["receipt_sha256"],
            hashlib.sha256(
                (ibkr_root / "control/ibkr-sdk-attestation.json").read_bytes()
            ).hexdigest(),
        )
        current = Path(record["current_release"])
        notification_test = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "notification-test",
                "--install-root",
                str(ibkr_root),
                "--event-id",
                "tampered-sdk-independent-route-test",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            notification_test.returncode, 0, notification_test.stderr
        )
        self.assertTrue(json.loads(notification_test.stdout)["queued"])
        notification_worker = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "notification-worker",
                "--install-root",
                str(ibkr_root),
                "--once",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            notification_worker.returncode, 0, notification_worker.stderr
        )
        self.assertEqual(json.loads(notification_worker.stdout)["sent"], 1)

        autonomous = subprocess.run(
            [
                sys.executable,
                str(current / "scripts/titan-full-live"),
                "doctor",
                "--install-root",
                str(ibkr_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(autonomous.returncode, 0)
        self.assertIn(
            "IBKR SDK snapshot file failed attestation", autonomous.stderr
        )

    def test_installed_notification_setup_works_with_missing_sdk_without_provider_access(self) -> None:
        sdk_venv = self.make_ibkr_sdk_venv()
        self.pin_fixture_sdk_inventory(sdk_venv)
        result = self.build("notification-setup", config_path="config/full_live_ibkr.json")
        ibkr_root = self.application_root / "full-live-ibkr-ending-3103"
        record = installer.install(
            Path(result["archive"]), ibkr_root,
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
            python_executable=Path(sys.executable), ibkr_sdk_venv=sdk_venv,
        )
        current = Path(record["current_release"])
        receipt = json.loads((ibkr_root / "control/ibkr-sdk-attestation.json").read_text())
        sdk_root = ibkr_root / receipt["import_root"]
        sdk_root.parent.chmod(0o755)
        sdk_root.rename(sdk_root.with_name(sdk_root.name + "-offline"))
        self.assertFalse(sdk_root.exists())
        state_before = {
            p.relative_to(ibkr_root).as_posix(): p.read_bytes()
            for p in (ibkr_root / "state").rglob("*") if p.is_file()
        }
        # Run the installed launcher and its installed modules in an isolated
        # process. Reject all provider/secret access; only metadata is simulated.
        harness = textwrap.dedent("""\
            import runpy, socket, subprocess, sys, urllib.request
            from pathlib import Path
            release = Path(sys.argv[1])
            install = sys.argv[2]
            sys.path.insert(0, str(release / "src"))
            def forbidden(*args, **kwargs):
                raise AssertionError("provider or secret access from setup diagnostics")
            socket.create_connection = forbidden
            socket.socket.connect = forbidden
            urllib.request.urlopen = forbidden
            subprocess.run = forbidden
            from titan_brain.live import cli, provider_clients, provider_profile
            cli._runtime_composition = forbidden
            provider_profile.activate_installed_sdk = forbidden
            provider_clients.MacOSKeychain.read = forbidden
            provider_clients.MacOSKeychain.read_text = forbidden
            provider_clients.MacOSKeychain.metadata_status = lambda self, item: "MISSING"
            sys.argv = [str(release / "scripts/titan-full-live"),
                        "notification-setup-status", "--install-root", install]
            runpy.run_path(sys.argv[0], run_name="__main__")
        """)
        launched = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", harness, str(current), str(ibkr_root)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(launched.returncode, 2, launched.stderr)
        self.assertEqual(launched.stderr, "")
        report = json.loads(launched.stdout)
        self.assertEqual(report["selected_profile"], "ibkr_gmail")
        self.assertEqual(report["credential_account"], "ibkr-live-ending-3103")
        self.assertEqual(len(report["keychain_items"]), 5)
        for field in (
            "credential_contents_read", "broker_checks_performed",
            "market_data_checks_performed", "network_checks_performed",
            "delivery_attempted", "readiness_evidence_issued", "ready_for_delivery",
        ):
            self.assertIs(report[field], False)
        self.assertEqual(state_before, {
            p.relative_to(ibkr_root).as_posix(): p.read_bytes()
            for p in (ibkr_root / "state").rglob("*") if p.is_file()
        })

    def test_installer_uses_configured_opaque_account_key(self) -> None:
        config_path = self.source_root / "config/full_live.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["account"]["account_key"] = "ibkr-live-7153"
        config_path.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        self.git("add", "config/full_live.json")
        self.git("commit", "--quiet", "-m", "parameterize account namespace")
        self.source_revision = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.build("parameterized-account")
        root = self.install_root
        installer.install(
            Path(result["archive"]),
            root,
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
            python_executable=Path(sys.executable),
        )
        initialized = self.run_cli("init-state", "--install-root", str(root))
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        with LiveStateStore(root / "state/full-live.sqlite3") as state:
            runtime = state.runtime_status()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(runtime["account_key"], "ibkr-live-7153")

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
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
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
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
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
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
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
            trusted_source_root=self.source_root,
            expected_source_revision=self.source_revision,
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

    def test_attended_coordinator_lock_blocks_release_switch(self) -> None:
        lock = AccountWriterLock(
            self.fixed_lock_directory,
            attended_coordinator_lock_key("ending-7153"),
            owner_id="running-attended-coordinator",
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
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                expected_archive_sha256=self.result["archive_sha256"],
            )
        with self.assertRaisesRegex(installer.InstallError, "must be under"):
            installer.install(
                archive,
                self.application_root,
                trusted_source_root=self.source_root,
                expected_source_revision=self.source_revision,
                python_executable=Path(sys.executable),
            )
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "legacy-unchanged")

    def test_archive_path_replacement_during_verification_fails_closed(self) -> None:
        archive = Path(self.result["archive"])
        replacement = Path(
            self.build_next_release("archive replacement race")["archive"]
        )
        assert_identity = installer._assert_archive_path_identity
        first_check_complete = False

        def replace_after_first_identity_check(path, expected):
            nonlocal first_check_complete
            assert_identity(path, expected)
            if not first_check_complete:
                first_check_complete = True
                replacement.replace(path)

        with mock.patch.object(
            installer,
            "_assert_archive_path_identity",
            side_effect=replace_after_first_identity_check,
        ):
            with self.assertRaisesRegex(
                installer.InstallError, "release archive changed"
            ):
                installer.install(
                    archive,
                    self.install_root,
                    trusted_source_root=self.source_root,
                    expected_source_revision=self.source_revision,
                    expected_archive_sha256=self.result["archive_sha256"],
                    python_executable=Path(sys.executable),
                )
        self.assertTrue(first_check_complete)
        self.assertFalse(self.install_root.exists())

    def test_installer_has_no_service_launch_path(self) -> None:
        source = (ROOT / "scripts/install_full_live_paused.py").read_text(encoding="utf-8")
        self.assertIn('["git", "--no-replace-objects"', source)
        self.assertNotIn('["launchctl"', source)
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
