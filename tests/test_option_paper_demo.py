"""Test the runnable end-to-end paper demo (milestone 9b)."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import unittest


def _load_demo():
    path = Path(__file__).resolve().parents[1] / "scripts" / "titan-option-paper-demo"
    loader = SourceFileLoader("titan_option_paper_demo", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PaperDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.demo = _load_demo()

    def test_scenarios_produce_expected_outcomes(self) -> None:
        results = {name: r for name, r in self.demo.scenarios()}
        # Happy path completes with an open paper position.
        self.assertTrue(results["happy_path"].ok)
        self.assertEqual(results["happy_path"].stopped_at, "complete")
        self.assertEqual(results["happy_path"].lifecycle["open_contracts"], 2)
        # Faults each fail closed at the right stage.
        self.assertFalse(results["stale_quote_data"].ok)
        self.assertEqual(results["insufficient_budget"].stopped_at, "policy")
        self.assertEqual(results["manual_intervention"].stopped_at, "reconciliation")
        self.assertEqual(results["rejected_fill"].stopped_at, "paper_fill")
        self.assertEqual(results["unknown_submission"].stopped_at, "paper_fill")

    def test_all_scenarios_are_paper_only_and_unauthorized(self) -> None:
        for name, r in self.demo.scenarios():
            self.assertTrue(r.paper_only, name)
            self.assertFalse(r.live_authority, name)

    def test_main_runs_and_returns_zero(self) -> None:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.demo.main(), 0)


if __name__ == "__main__":
    unittest.main()
