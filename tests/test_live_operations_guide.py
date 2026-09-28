from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "validation/full-live/2026-09-08/OPERATIONS.md"


class FullLiveOperationsGuideTests(unittest.TestCase):
    def test_ibkr_build_install_and_activation_commands_are_exact(self) -> None:
        guide = OPERATIONS.read_text(encoding="utf-8")

        self.assertIn("--config config/full_live_ibkr.json", guide)
        self.assertIn(
            "IBKR_SDK_VENV=/absolute/path/to/authorized-ibapi-10.50.2-venv",
            guide,
        )
        self.assertIn('--ibkr-sdk-venv "$IBKR_SDK_VENV"', guide)
        self.assertIn(
            '--confirm "ACTIVATE FULL LIVE ibkr-live-ending-3103 '
            '$ACTIVATION_ID"',
            guide,
        )
        self.assertNotIn("ACTIVATE FULL LIVE ending-7153", guide)

    def test_scheduler_cutover_names_both_control_plane_automations(self) -> None:
        guide = OPERATIONS.read_text(encoding="utf-8")

        self.assertIn("`robinhood-momentum-engine`", guide)
        self.assertIn("`robinhood-titan-premarket-deep-dive`", guide)
        self.assertIn(
            "$INSTALL_ROOT/control/codex-scheduler-retirement-evidence.json",
            guide,
        )
        self.assertIn("Do not edit an\n   `automation.toml`", guide)


if __name__ == "__main__":
    unittest.main()
