"""Golden + validation tests for the canonical IBKR option contract identity.

Mirrors the style of ``tests/test_live_ibkr_sdk.py``: a stable ``sha256`` wire
fingerprint, equivalent inputs hashing identically, and strict rejection of
ambiguous / adjusted / unsupported contracts.  These tests do not import the
broker SDK and open no socket.
"""

from __future__ import annotations

from decimal import Decimal
import unittest

from titan_brain.live.broker.ibkr_option_orders import (
    IbkrOptionContractIdentity,
    OptionContractError,
    WIRE_SCHEMA,
)


def _identity(**overrides):
    fields = dict(
        con_id=765432109,
        symbol="TEST",
        right="C",
        strike="12.50",
        expiry="20261016",
        trading_class="TEST",
        multiplier=100,
        deliverable="100_SHARES",
        exercise_style="AMERICAN",
        settlement_type="PHYSICAL",
        currency="USD",
        exchange="SMART",
    )
    fields.update(overrides)
    return IbkrOptionContractIdentity(**fields)


class IbkrOptionIdentityTests(unittest.TestCase):
    def test_valid_identity_builds_and_exposes_fields(self) -> None:
        ident = _identity()
        self.assertEqual(ident.con_id, 765432109)
        self.assertEqual(ident.right, "C")
        self.assertEqual(ident.strike, Decimal("12.50"))
        self.assertEqual(ident.multiplier, 100)
        canonical = ident.canonical_dict()
        self.assertEqual(canonical["schema"], WIRE_SCHEMA)
        self.assertEqual(canonical["secType"], "OPT")
        self.assertEqual(canonical["lastTradeDateOrContractMonth"], "20261016")

    def test_fingerprint_is_stable_hex_sha256(self) -> None:
        ident = _identity()
        first = ident.wire_fingerprint()
        second = ident.wire_fingerprint()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, second)

    def test_equivalent_strike_representations_fingerprint_equally(self) -> None:
        # "12.50" and "12.5" and 12 (for a whole strike) must normalize.
        self.assertEqual(
            _identity(strike="12.50").wire_fingerprint(),
            _identity(strike="12.5").wire_fingerprint(),
        )
        self.assertEqual(
            _identity(strike="15").wire_fingerprint(),
            _identity(strike="15.00").wire_fingerprint(),
        )

    def test_distinct_contracts_fingerprint_differently(self) -> None:
        base = _identity().wire_fingerprint()
        self.assertNotEqual(base, _identity(right="P").wire_fingerprint())
        self.assertNotEqual(base, _identity(strike="13.00").wire_fingerprint())
        self.assertNotEqual(base, _identity(expiry="20261023").wire_fingerprint())
        self.assertNotEqual(base, _identity(con_id=765432110).wire_fingerprint())

    def test_float_strike_is_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "strike"):
            _identity(strike=12.5)

    def test_bad_right_is_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "right"):
            _identity(right="X")

    def test_zero_and_negative_strike_rejected(self) -> None:
        with self.assertRaises(OptionContractError):
            _identity(strike="0")
        with self.assertRaises(OptionContractError):
            _identity(strike="-1")

    def test_bad_expiry_calendar_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "expiry"):
            _identity(expiry="20260231")  # Feb 31 is not real
        with self.assertRaisesRegex(OptionContractError, "expiry"):
            _identity(expiry="2026-10-16")  # wrong format

    def test_non_usd_currency_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "USD"):
            _identity(currency="EUR")

    def test_bad_con_id_rejected(self) -> None:
        with self.assertRaises(OptionContractError):
            _identity(con_id=0)
        with self.assertRaises(OptionContractError):
            _identity(con_id=-5)
        with self.assertRaises(OptionContractError):
            _identity(con_id=True)  # bool is not a valid id

    def test_adjusted_contract_rejected_standard_deliverable_nonstandard_multiplier(self) -> None:
        # A standard "100_SHARES" deliverable at a non-100 multiplier is an
        # adjusted contract -- must be rejected, never silently mispriced.
        with self.assertRaisesRegex(OptionContractError, "adjusted"):
            _identity(deliverable="100_SHARES", multiplier=50)

    def test_nonstandard_deliverable_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "adjusted|non-standard"):
            _identity(deliverable="90_SHARES_PLUS_CASH", multiplier=90)

    def test_bad_exercise_style_and_settlement_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "exercise_style"):
            _identity(exercise_style="BERMUDAN")
        with self.assertRaisesRegex(OptionContractError, "settlement_type"):
            _identity(settlement_type="NET")

    def test_bad_symbol_and_trading_class_rejected(self) -> None:
        with self.assertRaisesRegex(OptionContractError, "symbol"):
            _identity(symbol="bad lower")
        with self.assertRaisesRegex(OptionContractError, "trading_class"):
            _identity(trading_class="has space")

    def test_multiplier_must_be_stated(self) -> None:
        with self.assertRaises(TypeError):
            IbkrOptionContractIdentity(  # type: ignore[call-arg]
                con_id=1,
                symbol="TEST",
                right="C",
                strike="12.50",
                expiry="20261016",
                trading_class="TEST",
                deliverable="100_SHARES",
                exercise_style="AMERICAN",
                settlement_type="PHYSICAL",
            )

    def test_put_and_call_are_distinct_and_valid(self) -> None:
        call = _identity(right="C")
        put = _identity(right="P")
        self.assertEqual(call.right, "C")
        self.assertEqual(put.right, "P")
        self.assertNotEqual(call.wire_fingerprint(), put.wire_fingerprint())


if __name__ == "__main__":
    unittest.main()
