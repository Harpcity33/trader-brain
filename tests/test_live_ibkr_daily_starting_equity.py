"""Hermetic authenticated day-start/flow evidence and final dispatch gates."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import BrokerMutationBlocked, FundsSnapshot
from titan_brain.live.broker.ibkr_risk_evidence import (
    IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA, DailyIbkrRiskBaselineAuthenticator,
    IbkrRiskEvidenceEnricher, IbkrRiskEvidenceAccountSnapshotReader, IbkrRiskHighWaterLedger,
)
from titan_brain.live.policy import PolicyBundle, canonical_json
from titan_brain.live.risk_evidence_binding import daily_starting_equity_receipt_hash
from tests import test_live_ibkr_risk_evidence as evidence_support
from tests.test_live_ibkr_risk_evidence import (
    NOW, SECRET, FixedClock, RecordingKeyReader,
    RawSnapshotReader, bindings, raw_snapshot, receipt_payload, write_receipt,
)
from tests.test_live_ibkr_risk_high_water_upgrade import installer
from tests import test_live_ibkr_dollar_final_gate as gate_support
from tests.test_live_ibkr_autonomous_interlock import autonomous_policy


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 14, 4, tzinfo=timezone.utc)


def sign(payload):
    body = dict(payload)
    body.pop("hmac_sha256", None)
    body["hmac_sha256"] = hmac.new(SECRET, canonical_json(body).encode(), hashlib.sha256).hexdigest()
    return body


def equity_payload(expected, *, total="1000", start="1000", flow="0", now=NOW):
    payload = receipt_payload(expected, issued_at=now)
    payload["schema_version"] = IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA
    payload["evidence"].update({
        "daily_starting_equity": start,
        "daily_starting_equity_as_of": START.isoformat(),
        "daily_starting_equity_provider_receipt_sha256": "1" * 64,
        "daily_external_cash_flow": flow,
        "daily_external_cash_flow_as_of": now.isoformat(),
        "daily_external_cash_flow_provider_receipt_sha256": "2" * 64,
        "valuation_total_equity": total,
        "valuation_observed_at": now.isoformat(),
    })
    return sign(payload)


class DailyStartingEquityEvidenceTests(unittest.TestCase):
    setUp = evidence_support.IbkrRiskEvidenceEnrichmentTests.setUp
    assert_code = evidence_support.IbkrRiskEvidenceEnrichmentTests.assert_code

    def write(self, **values):
        write_receipt(self.receipt_path, equity_payload(self.expected, **values))

    def test_verified_values_bind_start_cash_flow_and_exact_current_valuation(self):
        self.write(total="1150", flow="150")
        observed = self.enricher.enrich(raw_snapshot(net_liquidation="1150"))
        self.assertTrue(observed.daily_starting_equity_ready)
        self.assertEqual(observed.daily_starting_equity, Decimal("1000"))
        self.assertEqual(observed.daily_external_cash_flow, Decimal("150"))
        self.assertEqual(observed.funds.total_value - observed.daily_external_cash_flow - observed.daily_starting_equity, 0)
        self.assertFalse(replace(observed, daily_external_cash_flow=Decimal("0")).daily_starting_equity_ready)
        self.assertFalse(replace(observed, funds=replace(observed.funds, total_value=Decimal("1149"))).daily_starting_equity_ready)

    def test_legacy_v1_never_proves_starting_equity(self):
        observed = self.enricher.enrich(raw_snapshot())
        self.assertTrue(observed.authenticated_entry_risk_evidence_ready)
        self.assertFalse(observed.daily_starting_equity_ready)
        self.authenticator.required_schema = IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA
        self.assert_code("IBKR_RISK_EVIDENCE_BASELINE_SCHEMA_BINDING_MISMATCH", lambda: self.enricher.enrich(raw_snapshot()))

    def test_missing_flow_late_baseline_stale_flow_and_wrong_valuation_fail_closed(self):
        original = equity_payload(self.expected)
        variants = []
        for field, value in (("daily_starting_equity_as_of", NOW.isoformat()),
                             ("daily_external_cash_flow_as_of", (NOW - timedelta(seconds=6)).isoformat()),
                             ("valuation_total_equity", "1001")):
            candidate = {**original, "evidence": {**original["evidence"], field: value}}
            variants.append(sign(candidate))
        missing = {**original, "evidence": dict(original["evidence"])}
        del missing["evidence"]["daily_external_cash_flow"]
        variants.append(sign(missing))
        for payload in variants:
            with self.subTest(evidence=payload["evidence"]):
                write_receipt(self.receipt_path, payload)
                with self.assertRaises(Exception):
                    self.enricher.enrich(raw_snapshot())

    def test_refresh_flow_does_not_reset_baseline_and_rejects_rollback(self):
        self.write()
        first = self.enricher.enrich(raw_snapshot())
        later = NOW + timedelta(seconds=1)
        self.clock.value = later
        self.write(total="1200", flow="200", now=later)
        second = self.enricher.enrich(raw_snapshot(now=later, net_liquidation="1200"))
        self.assertEqual(first.risk_baseline_receipt_hash, second.risk_baseline_receipt_hash)
        self.assertNotEqual(first.daily_external_cash_flow_receipt_hash, second.daily_external_cash_flow_receipt_hash)
        self.write(total="1200", flow="0", now=later)
        self.assert_code("IBKR_RISK_EVIDENCE_CASH_FLOW_EVIDENCE_EQUIVOCATION", lambda: self.enricher.enrich(raw_snapshot(now=later, net_liquidation="1200")))
        self.clock.value = NOW
        self.write()
        self.assert_code("IBKR_RISK_EVIDENCE_CASH_FLOW_EVIDENCE_REGRESSION", lambda: self.enricher.enrich(raw_snapshot()))

    def test_frozen_baseline_survives_reopen_and_cannot_be_rebased(self):
        self.write()
        self.enricher.enrich(raw_snapshot())
        self.ledger.close()
        ledger = IbkrRiskHighWaterLedger(self.ledger_path, bindings=self.expected.ledger_bindings)
        self.addCleanup(ledger.close)
        fresh = IbkrRiskEvidenceEnricher(baseline_authenticator=self.authenticator, high_water_ledger=ledger, snapshot_max_age_seconds=5)
        self.write(start="1100")
        self.assert_code("IBKR_RISK_EVIDENCE_DAILY_STARTING_EQUITY_CHANGED", lambda: fresh.enrich(raw_snapshot()))

    def test_zero_equity_is_authenticatable_for_loss_control_not_entry_authority(self):
        self.write(total="0")
        observed = self.enricher.enrich(raw_snapshot(net_liquidation="0"))
        self.assertTrue(observed.daily_starting_equity_ready)
        self.assertEqual(observed.funds.total_value, 0)

    def test_failed_enrichment_clears_every_new_entry_field_preserves_raw_account(self):
        self.write()
        observed = self.enricher.enrich(raw_snapshot())
        reader = IbkrRiskEvidenceAccountSnapshotReader(snapshot_reader=RawSnapshotReader([observed]), enricher=self.enricher)
        degraded = reader()
        self.assertEqual(degraded.funds, observed.funds)
        self.assertFalse(degraded.daily_starting_equity_ready)
        self.assertTrue(all(getattr(degraded, key) is None for key in (
            "daily_starting_equity", "daily_external_cash_flow", "daily_starting_equity_as_of",
            "daily_external_cash_flow_as_of", "daily_external_cash_flow_receipt_hash", "daily_starting_equity_receipt_hash",
        )))

    def test_v4_release_rebind_retains_starting_equity_and_flow_watermark(self):
        self.write(total="1200", flow="200")
        self.enricher.enrich(raw_snapshot(net_liquidation="1200"))
        self.ledger.close()
        source = installer._read_ibkr_risk_ledger(self.ledger_path)
        target = replace(self.expected, release_manifest_hash="9" * 64)
        destination = self.root / "new-ledger.sqlite3"
        installer._create_ibkr_risk_ledger(
            destination, target=asdict(target.ledger_bindings), source=source,
            source_sha256=hashlib.sha256(self.ledger_path.read_bytes()).hexdigest(),
            archive_relative="state/ibkr-risk-high-water-archive/" + "a" * 64 + "-" + "b" * 64 + ".sqlite3",
            migrated_at=NOW,
        )
        retained = installer._read_ibkr_risk_ledger(destination)
        self.assertEqual(retained["daily_starting_equity"], source["daily_starting_equity"])
        ledger = IbkrRiskHighWaterLedger(destination, bindings=target.ledger_bindings)
        self.addCleanup(ledger.close)
        authenticator = DailyIbkrRiskBaselineAuthenticator(path=self.receipt_path, key_reader=self.key_reader,
            key_item=self.authenticator.key_item, expected=target, clock=self.clock)
        enricher = IbkrRiskEvidenceEnricher(baseline_authenticator=authenticator, high_water_ledger=ledger, snapshot_max_age_seconds=5)
        later = NOW + timedelta(seconds=1)
        self.clock.value = later
        write_receipt(self.receipt_path, equity_payload(target, total="1200", flow="200", start="1100", now=later))
        self.assert_code("IBKR_RISK_EVIDENCE_DAILY_STARTING_EQUITY_CHANGED", lambda: enricher.enrich(raw_snapshot(now=later, net_liquidation="1200")))
        write_receipt(self.receipt_path, equity_payload(target, total="1200", flow="200", now=later))
        self.assertTrue(enricher.enrich(raw_snapshot(now=later, net_liquidation="1200")).daily_starting_equity_ready)


class DailyStartingEquityFinalGateTests(unittest.TestCase):
    prepare = gate_support.DollarObservationPersistenceTests.prepare
    snapshot = gate_support.DollarObservationPersistenceTests.snapshot
    restart_risk = gate_support.DollarObservationPersistenceTests.restart_risk
    reopen_state = gate_support.DollarObservationPersistenceTests.reopen_state
    pending = gate_support.DollarObservationPersistenceTests.pending
    read = gate_support.DollarObservationPersistenceTests.read
    latch = gate_support.DollarObservationPersistenceTests.latch
    bridge = gate_support.DollarObservationPersistenceTests.bridge
    observe = gate_support.DollarObservationPersistenceTests.observe

    def setUp(self):
        self.policy = autonomous_policy(base_policy=PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json"))
        gate_support.DollarObservationPersistenceTests.setUp(self)

    def auth_snapshot(self, pnl="0", *, total="10000", start="10000", flow="0", **overrides):
        base = gate_support.DollarObservationPersistenceTests.auth_snapshot(self, pnl, **overrides)
        peak = max(Decimal(total), Decimal("10000"), self.risk._activation_peak_floor or Decimal("10000"))
        # Recompute the existing high-water evidence using the original fixture.
        if peak > base.peak_equity:
            from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
            base = replace(base, peak_equity=peak, risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash=base.risk_high_water_identity_hash, baseline_receipt_hash=base.risk_baseline_receipt_hash,
                lineage_hash=base.risk_high_water_lineage_hash, peak_equity=peak))
        base = replace(base, funds=replace(base.funds, total_value=Decimal(total)))
        flow_hash = "7" * 64
        return replace(base, daily_starting_equity=Decimal(start), daily_external_cash_flow=Decimal(flow),
            daily_starting_equity_as_of=START, daily_external_cash_flow_as_of=base.observed_at,
            daily_external_cash_flow_receipt_hash=flow_hash,
            daily_starting_equity_receipt_hash=daily_starting_equity_receipt_hash(
                baseline_receipt_hash=base.risk_baseline_receipt_hash, cash_flow_receipt_hash=flow_hash,
                starting_equity=Decimal(start), external_cash_flow=Decimal(flow),
                starting_equity_as_of=START, cash_flow_as_of=base.observed_at, total_equity=base.funds.total_value))

    def test_fixed_risk_budget_cannot_expand_with_profits(self):
        plan = self.prepare(quantity=500, entry="6", stop="5")
        self.value = self.auth_snapshot(total="20000")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertFalse(self.latch()["loss_locked"])

    def test_goal_is_aspirational_without_old_dollar_floor(self):
        plan = self.prepare(quantity=20)
        self.value = self.auth_snapshot(total="11500")
        self.assertEqual(self.bridge(plan).review(plan.request).request, plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 1)
        self.reopen_state()
        self.value = self.auth_snapshot(total="10000")
        self.assertEqual(self.bridge(plan).review(plan.request).request, plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 1)

    def test_deposit_does_not_manufacture_goal_and_withdrawal_does_not_trigger_loss(self):
        plan = self.prepare()
        self.value = self.auth_snapshot(total="12000", flow="2000")
        self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 0)
        self.value = self.auth_snapshot(total="8000", flow="-2000")
        self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["loss_locked"], 0)

    def test_total_equity_loss_crossing_persists_hard_kill_before_instrument_denial(self):
        plan = self.prepare()
        self.value = self.auth_snapshot(pnl="500", total="9000")
        self.requester.match_count = 0
        with self.assertRaisesRegex(BrokerMutationBlocked, "INSTRUMENT_REVALIDATION_FAILED"):
            self.bridge(plan).review(plan.request)
        self.assertTrue(all(self.latch()[key] for key in ("loss_locked", "hard_kill", "pause_new_entries", "closeout_started")))
        self.reopen_state()
        self.requester.match_count = 1
        self.value = self.auth_snapshot(total="10000")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)

    def test_zero_total_equity_still_persists_loss_control(self):
        plan = self.prepare()
        self.value = self.auth_snapshot(total="0")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["hard_kill"], 1)
        self.assertFalse(self.pending())

    def test_remaining_equity_headroom_counts_pending_fees_once(self):
        plan = self.prepare()
        other = self.prepare(quantity=4, symbol="NEXT")
        total = Decimal("9000") + plan.stress_downside + other.stress_downside
        self.value = self.auth_snapshot(total=str(total))
        self.bridge(plan).review(plan.request)
        self.value = self.auth_snapshot(total=str(total - Decimal(".01")))
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)

    def test_unproven_start_or_flow_retains_marker_and_blocks_entry(self):
        plan = self.prepare()
        self.value = replace(self.auth_snapshot(), daily_external_cash_flow=None)
        with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_COMMITTED"):
            self.bridge(plan).review(plan.request)
        self.assertTrue(self.pending())

    def test_flat_broker_read_cannot_hide_protected_filled_durable_reservation(self):
        from titan_brain.live.pipeline import build_account_risk_snapshot
        plan = self.prepare()
        other = self.prepare(symbol="RESIDUAL")
        stamp = NOW.isoformat()
        account = self.policy.account_key
        with self.store.transaction() as connection:
            entry_id = connection.execute("SELECT intent_id FROM order_intents WHERE plan_id=?", (other.plan_id,)).fetchone()[0]
            connection.execute("UPDATE order_intents SET state='ACKNOWLEDGED' WHERE intent_id=?", (entry_id,))
            connection.execute("UPDATE risk_reservations SET state='BOUND' WHERE plan_id=?", (other.plan_id,))
            connection.execute("INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("residual-entry", entry_id, account, "FILLED", 2, 2, 1, stamp, stamp, "a" * 64))
            connection.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                ("residual-fill", "residual-entry", account, 2, "12", stamp, stamp))
            # Retain a separate acknowledged protection intent/order so the
            # residual is fully protected, not blocked by the older uncovered gate.
            connection.execute("""INSERT INTO order_intents
                SELECT 'residual-stop-intent',plan_id,NULL,account_key,'PROTECTION',
                'residual-stop-ref',order_tuple_json,tuple_hash,created_at,
                acknowledgement_deadline_at,'ACKNOWLEDGED',updated_at
                FROM order_intents WHERE intent_id=?""", (entry_id,))
            connection.execute("INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("residual-stop", "residual-stop-intent", account, "CONFIRMED", 2, 0, 1, stamp, stamp, "b" * 64))
            connection.execute("INSERT INTO protection_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("residual-protection", "residual-fill", account, "RESIDUAL", 2, 2, "11.50", "WORKING", 1, stamp, "residual-stop"))
        self.value = self.auth_snapshot()
        self.assertEqual(self.value.equity_positions, ())
        risk, failures = build_account_risk_snapshot(
            policy=self.policy, state=self.store, broker_snapshot=replace(self.value, account_masked="••••3103"),
            now=NOW, prices={plan.request.symbol: plan.request.limit_price})
        self.assertIsNone(risk)
        self.assertIn("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED", failures)
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertFalse(self.latch()["loss_locked"])


if __name__ == "__main__":
    unittest.main()
