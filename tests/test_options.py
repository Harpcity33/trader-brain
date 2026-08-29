from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.options import (
    AttendedReviewEnvelope,
    OptionContractSnapshot,
    OptionOrderIntent,
    OptionsEligibilitySnapshot,
    OptionsPolicyError,
    build_local_attended_review,
    evaluate_event_volatility,
    evaluate_long_option_risk,
    full_premium_stress_loss,
    model_historical_long_option_execution,
    validate_attended_confirmation,
)


UTC = timezone.utc


def eligibility(**overrides):
    values = {
        "account_last4": "7153",
        "account_state": "active",
        "account_type": "limited_margin",
        "account_accessible": True,
        "option_level": "option_level_2",
        "current_equity": Decimal("930.17"),
        "buying_power": Decimal("930.17"),
        "unleveraged_buying_power": Decimal("930.17"),
        "equity_positions_reconciled": True,
        "equity_orders_reconciled": True,
        "option_positions_reconciled": True,
        "option_orders_reconciled": True,
    }
    values.update(overrides)
    return OptionsEligibilitySnapshot(**values)


class EligibilityTests(unittest.TestCase):
    def test_level_two_allows_long_calls_puts_but_not_spreads(self):
        snapshot = eligibility()
        self.assertTrue(snapshot.long_calls_puts_enabled)
        self.assertFalse(snapshot.debit_spreads_enabled)
        self.assertTrue(snapshot.requires_review_affordability_check)

    def test_level_three_still_requires_explicit_multileg_verification(self):
        unverified = eligibility(option_level="option_level_3")
        verified = eligibility(
            option_level="option_level_3", multi_leg_eligibility_verified=True
        )
        self.assertFalse(unverified.debit_spreads_enabled)
        self.assertTrue(verified.debit_spreads_enabled)

    def test_unknown_order_reconciliation_fails_account_gate(self):
        snapshot = eligibility(option_orders_reconciled=False)
        self.assertFalse(snapshot.long_calls_puts_enabled)


class RiskTests(unittest.TestCase):
    def test_full_premium_is_stress_loss(self):
        self.assertEqual(
            full_premium_stress_loss("0.45", 1, "100"), Decimal("45.00")
        )

    def test_full_premium_must_fit_trade_and_portfolio_caps(self):
        allowed = evaluate_long_option_risk(
            current_equity="1000",
            limit_price="0.40",
            quantity=1,
            tactical_planned_loss="20",
            max_trade_stress_risk_pct="0.05",
            max_total_open_stress_risk_pct="0.08",
            existing_open_stress_risk="20",
        )
        rejected = evaluate_long_option_risk(
            current_equity="1000",
            limit_price="0.60",
            quantity=1,
            tactical_planned_loss="20",
            max_trade_stress_risk_pct="0.05",
            max_total_open_stress_risk_pct="0.08",
            existing_open_stress_risk="30",
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.maximum_premium_loss, Decimal("40.00"))
        self.assertFalse(rejected.allowed)
        self.assertIn("FULL_PREMIUM_EXCEEDS_TRADE_STRESS_LIMIT", rejected.reasons)
        self.assertIn(
            "FULL_PREMIUM_EXCEEDS_PORTFOLIO_STRESS_CAPACITY", rejected.reasons
        )


class EventVolatilityTests(unittest.TestCase):
    def test_event_move_must_clear_every_cost_and_reserve(self):
        passing = evaluate_event_volatility(
            expected_underlying_move_pct="0.10",
            implied_move_pct="0.05",
            spread_drag_pct="0.01",
            expected_slippage_pct="0.005",
            iv_crush_risk_pct="0.02",
            uncertainty_reserve_pct="0.01",
        )
        failing = evaluate_event_volatility(
            expected_underlying_move_pct="0.09",
            implied_move_pct="0.05",
            spread_drag_pct="0.01",
            expected_slippage_pct="0.005",
            iv_crush_risk_pct="0.02",
            uncertainty_reserve_pct="0.01",
        )
        self.assertTrue(passing.allowed)
        self.assertFalse(failing.allowed)

    def test_missing_event_input_fails_closed(self):
        result = evaluate_event_volatility(
            expected_underlying_move_pct=None,
            implied_move_pct="0.05",
            spread_drag_pct="0.01",
            expected_slippage_pct="0.005",
            iv_crush_risk_pct="0.02",
            uncertainty_reserve_pct="0.01",
        )
        self.assertFalse(result.allowed)
        self.assertIn("EXPECTED_UNDERLYING_MOVE_PCT_UNAVAILABLE", result.reasons)


class HistoricalExecutionTests(unittest.TestCase):
    def test_replay_uses_ask_in_and_bid_out_not_midpoints(self):
        result = model_historical_long_option_execution(
            entry_bid="1.00",
            entry_ask="1.10",
            exit_bid="1.40",
            exit_ask="1.50",
            entry_slippage_per_share="0.02",
            exit_slippage_per_share="0.02",
        )
        self.assertEqual(result.entry_fill, Decimal("1.12"))
        self.assertEqual(result.exit_fill, Decimal("1.38"))
        self.assertEqual(result.gross_pnl_dollars, Decimal("26.00"))
        self.assertEqual(result.midpoint_bias_dollars, Decimal("14.00"))


class ContractTests(unittest.TestCase):
    def test_contract_preferences_are_separate_hard_rejections(self):
        contract = OptionContractSnapshot(
            option_id="contract-1",
            chain_symbol="XYZ",
            option_type="call",
            expiration_date="2026-09-11",
            dte=13,
            strike=Decimal("10"),
            delta=Decimal("0.60"),
            gamma=Decimal("0.08"),
            theta=Decimal("-0.03"),
            vega=Decimal("0.02"),
            implied_volatility=Decimal("0.50"),
            bid=Decimal("1.00"),
            ask=Decimal("1.10"),
            bid_size=10,
            ask_size=12,
            open_interest=500,
            volume=100,
            moneyness="ATM",
            quote_updated_at=datetime(2026, 8, 29, 15, 0, tzinfo=UTC),
            tradable=True,
        )
        self.assertEqual(contract.preference_rejections(), ())
        self.assertEqual(contract.midpoint, Decimal("1.05"))


class AttendedReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
        self.intent = OptionOrderIntent(
            account_last4="7153",
            chain_symbol="XYZ",
            option_id="contract-1",
            side="buy",
            position_effect="open",
            quantity=1,
            limit_price=Decimal("1.05"),
            time_in_force="gfd",
            market_hours="regular_hours",
            evidence_revision="evidence-123",
            quote_updated_at=self.now,
        )
        self.review = AttendedReviewEnvelope(
            review_reference="review-123",
            intent_fingerprint=self.intent.fingerprint,
            reviewed_at=self.now,
            expires_at=self.now + timedelta(minutes=2),
            exact_confirmation_phrase="Confirm option order review-123",
            connector_preview={"order_checks": {}},
            order_checks={},
            disclosures=(),
        )

    def test_exact_confirmation_returns_review_reference_only(self):
        reference = validate_attended_confirmation(
            intent=self.intent,
            review=self.review,
            user_confirmation="Confirm option order review-123",
            now=self.now + timedelta(seconds=30),
        )
        self.assertEqual(reference, "review-123")

    def test_changed_tuple_or_expiry_requires_fresh_review(self):
        changed = OptionOrderIntent(
            **{**self.intent.__dict__, "limit_price": Decimal("1.06")}
        )
        with self.assertRaises(OptionsPolicyError):
            validate_attended_confirmation(
                intent=changed,
                review=self.review,
                user_confirmation="Confirm option order review-123",
                now=self.now + timedelta(seconds=30),
            )
        with self.assertRaises(OptionsPolicyError):
            validate_attended_confirmation(
                intent=self.intent,
                review=self.review,
                user_confirmation="Confirm option order review-123",
                now=self.now + timedelta(minutes=2),
            )

    def test_local_review_phrase_is_bound_to_tuple_and_ttl(self):
        review = build_local_attended_review(
            intent=self.intent,
            connector_preview={"order_checks": {}, "price": "1.05"},
            reviewed_at=self.now,
            ttl_seconds=120,
        )
        self.assertIn("1x contract-1 AT 1.05", review.exact_confirmation_phrase)
        self.assertIn(review.review_reference, review.exact_confirmation_phrase)
        self.assertEqual(review.expires_at, self.now + timedelta(seconds=120))


if __name__ == "__main__":
    unittest.main()
