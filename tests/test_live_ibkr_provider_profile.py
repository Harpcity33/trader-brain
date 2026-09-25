"""Offline bounds for prospective non-binding IBKR client-zero readers."""

from dataclasses import replace
import json
from pathlib import Path
import unittest

from titan_brain.live.provider_profile import (
    IbkrLocalProviderProfile,
    ProviderProfileError,
)


ROOT = Path(__file__).resolve().parents[1]


class IbkrReadClientProfileTests(unittest.TestCase):
    def config(self):
        return json.loads((ROOT / "config/full_live_ibkr.json").read_text())

    def test_default_configuration_is_unchanged(self):
        profile = IbkrLocalProviderProfile.from_config(self.config())
        self.assertIsNotNone(profile)
        self.assertEqual(profile.read_client_id, 19735)
        self.assertEqual(profile.command_client_id, 19736)
        self.assertEqual(profile.attended_read_client_id, 19737)

    def test_read_zero_is_supported_without_changing_command_or_attended_ids(self):
        config = self.config()
        config["local_provider_profile"]["read_client_id"] = 0
        profile = IbkrLocalProviderProfile.from_config(config)
        self.assertEqual(profile.read_client_id, 0)
        self.assertEqual(profile.command_client_id, 19736)
        self.assertEqual(profile.attended_read_client_id, 19737)
        attended = profile.for_attended_command()
        self.assertEqual(attended.read_client_id, 19737)
        self.assertEqual(attended.command_client_id, 19736)
        self.assertEqual(profile.read_client_id, 0)
        self.assertEqual(self.config()["local_provider_profile"]["read_client_id"], 19735)

    def test_invalid_ids_equal_ids_and_attended_collision_are_rejected(self):
        for read, command in (
            (-1, 19736), (False, 19736), (0.0, 19736), ("0", 19736),
            (0, 0), (0, False), (0, 1.0), (0, "1"),
            (19736, 19736), (19737, 19736), (2_147_483_648, 19736),
            (0, 2_147_483_647),
        ):
            with self.subTest(read=read, command=command):
                config = self.config()
                config["local_provider_profile"]["read_client_id"] = read
                config["local_provider_profile"]["command_client_id"] = command
                with self.assertRaises(ProviderProfileError):
                    IbkrLocalProviderProfile.from_config(config)

    def test_attended_id_derivation_is_positive_bounded_and_collision_checked(self):
        original = IbkrLocalProviderProfile.from_config(self.config())
        for read, command in (
            (0, 0), (0, -1), (0, True), (0, 1.0),
            (0, 2_147_483_647), (19736, 19736), (19737, 19736),
            (-1, 19736), (False, 19736),
        ):
            with self.subTest(read=read, command=command):
                invalid = replace(original, read_client_id=read, command_client_id=command)
                with self.assertRaises(ProviderProfileError):
                    invalid.for_attended_command()
        maximum = replace(original, read_client_id=0, command_client_id=2_147_483_646)
        self.assertEqual(maximum.attended_read_client_id, 2_147_483_647)
        minimum = replace(original, read_client_id=0, command_client_id=1)
        self.assertEqual(minimum.attended_read_client_id, 2)


if __name__ == "__main__":
    unittest.main()
