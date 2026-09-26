"""A1.2 evidence: captured finite facts route to an ALWAYS-BLOCKED shadow result.

The production shadow evaluator (ibkr_session_pnl_shadow) is a sealed
diagnostic: a repository guard
(test_no_other_production_module_imports_or_names_shadow_module) forbids ANY
production module from importing or even naming it. So the fact->observation
adapter used to demonstrate the honest-refusal property lives HERE, in the test
layer, which is permitted to reference the shadow.

The property proven: any real captured finite session facts, converted to a
shadow SessionObservation with honestly-UNKNOWN coverage (the finite read
cannot prove all-client / continuous / corrections-reconciled coverage
offline), ALWAYS produce a blocked ShadowResult (session_pnl=None) and can
never authorize an entry; malformed rows fail closed rather than being coerced.
No credential, network, or broker access.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.live.broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts,
    SessionExecutionFact,
    SessionInputObservation,
    SessionOrderFact,
    SessionPositionFact,
)
from titan_brain.live.broker.ibkr_session_pnl_shadow import (
    EvidenceStatus,
    Execution,
    ExecutionCoverage,
    ExecutionFee,
    InputOrigin,
    Position,
    SessionBaseline,
    SessionObservation,
    evaluate_session_pnl,
)


D = Decimal
START = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
DONE = START + timedelta(seconds=2)
NOW = START + timedelta(seconds=3)
ACCOUNT = "synthetic-account-binding"


class AdapterError(ValueError):
    """Fail-closed marker for the test-local fact->shadow adapter."""


def _whole_shares(quantity):
    if type(quantity) is not Decimal or not quantity.is_finite() or quantity != quantity.to_integral_value():
        raise AdapterError("NON_WHOLE_SHARE")
    return int(quantity)


def facts_to_shadow_observation(observation):
    """Test-local honest adapter: emits ONLY UNKNOWN coverage, never CONFIRMED.

    Mirrors what a production observation->policy path must guarantee, but kept
    in the test layer because production code may not reference the shadow.
    """
    facts = observation.facts
    binding = observation.account_binding_fingerprint
    unknown = EvidenceStatus.UNKNOWN

    executions, fees = [], []
    for row in facts.executions:
        if row.contract_id is None or row.side not in ("BUY", "SELL"):
            raise AdapterError("EXECUTION_INVALID")
        if row.currency != "USD" or row.commission_currency != "USD":
            raise AdapterError("NON_USD")
        executions.append(Execution(
            account_binding=binding, exec_id=row.exec_id, contract_id=row.contract_id,
            side=row.side, quantity=_whole_shares(row.quantity), price=row.price,
            executed_at=row.source_executed_at or row.received_at,
            currency="USD", security_type=row.security_type,
        ))
        fees.append(ExecutionFee(binding, row.exec_id, row.commission, row.commission_received_at, "USD"))

    positions = []
    for row in facts.positions:
        if row.contract_id is None or row.currency != "USD":
            raise AdapterError("POSITION_INVALID")
        positions.append(Position(binding, row.contract_id, _whole_shares(row.quantity), "USD", row.security_type))

    return SessionObservation(
        account_binding=binding,
        as_of=facts.collection_completed_at,
        origin=InputOrigin.OBSERVED_UNVERIFIED,
        source_evidence_id=facts.collection_id,
        executions=tuple(executions),
        fees=tuple(fees),
        positions=tuple(positions),
        marks=(),
        coverage=ExecutionCoverage(
            facts.collection_started_at, facts.collection_completed_at,
            unknown, unknown, unknown, facts.collection_id,
        ),
        positions_reconciled=unknown,
        orders_exposure_reconciled=unknown,
        accounting_reconciled=unknown,
        unexplained_accounting_delta=None,
        reported_external_cash_flow=None,
    )


def _exec(**changes):
    value = SessionExecutionFact(
        exec_id="0001.0002.01.03", contract_id=123, symbol="ABC", security_type="STK",
        currency="USD", side="BUY", quantity=D("100"), price=D("50"),
        source_executed_at=START + timedelta(seconds=1),
        source_time_basis="PROVIDER_EXPLICIT_ZONE", received_at=START + timedelta(seconds=1),
        commission=D("1.00"), commission_currency="USD",
        commission_received_at=START + timedelta(seconds=1),
    )
    return replace(value, **changes)


def _facts(**changes):
    value = IbkrFiniteSessionFacts(
        generation=1, collection_id="collection-1",
        collection_started_at=START, collection_completed_at=DONE,
        net_liquidation=D("10000"), net_liquidation_currency="USD", net_liquidation_received_at=DONE,
        positions=(SessionPositionFact(123, "ABC", "STK", "USD", D("100"), DONE),),
        executions=(_exec(),),
        orders=(SessionOrderFact("o-1", 123, "SIMPLE", True, "Filled", False),),
        completed_reads=("account_updates_multi", "executions", "positions"),
        commission_conflict_observed=False, orphan_commission_report_count=0,
    )
    return replace(value, **changes)


def _observation(facts=None):
    return SessionInputObservation(
        account_binding_fingerprint=ACCOUNT,
        facts=facts if facts is not None else _facts(),
        read_client_id=19735, prior_collection_id=None,
        unobserved_interval_since_prior_collection=False,
        sticky_read_gap=False, blockers=(),
    )


def _baseline():
    return SessionBaseline(
        account_binding=ACCOUNT, frozen_at=START, starting_nlv=D("10000"),
        source_evidence_id="synthetic-start", origin=InputOrigin.OBSERVED_UNVERIFIED,
        flat_start=EvidenceStatus.CONFIRMED, pre_entry=EvidenceStatus.CONFIRMED,
        initial_exposure_reconciled=EvidenceStatus.CONFIRMED,
    )


class SessionInputShadowRoutingTests(unittest.TestCase):
    def test_adapter_emits_only_unknown_coverage_and_observed_origin(self):
        obs = facts_to_shadow_observation(_observation())
        self.assertIs(obs.origin, InputOrigin.OBSERVED_UNVERIFIED)
        for status in (obs.coverage.all_clients, obs.coverage.continuous,
                       obs.coverage.corrections_reconciled, obs.positions_reconciled,
                       obs.orders_exposure_reconciled, obs.accounting_reconciled):
            self.assertIs(status, EvidenceStatus.UNKNOWN)
        self.assertIsNone(obs.unexplained_accounting_delta)
        self.assertEqual(obs.executions[0].quantity, 100)
        self.assertEqual(obs.fees[0].amount, D("1.00"))
        self.assertEqual(obs.positions[0].quantity, 100)

    def test_output_always_blocks_and_never_authorizes(self):
        result = evaluate_session_pnl(_baseline(), facts_to_shadow_observation(_observation()), now=NOW)
        self.assertIsNone(result.session_pnl)
        self.assertIsNone(result.performance_fraction)
        for blocker in ("ALL_CLIENT_EXECUTIONS_UNPROVEN", "EXECUTION_CONTINUITY_UNPROVEN",
                        "CORRECTIONS_UNRECONCILED", "POSITIONS_UNRECONCILED",
                        "ORDER_EXPOSURE_UNRECONCILED", "ACCOUNTING_UNRECONCILED",
                        "ACCOUNTING_DELTA_UNKNOWN"):
            self.assertIn(blocker, result.blockers)

    def test_even_a_flat_empty_session_still_blocks(self):
        facts = _facts(positions=(), executions=(), orders=())
        result = evaluate_session_pnl(_baseline(), facts_to_shadow_observation(_observation(facts)), now=NOW)
        self.assertIsNone(result.session_pnl)
        self.assertIn("EXECUTION_CONTINUITY_UNPROVEN", result.blockers)

    def test_missing_contract_id_fails_closed(self):
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(executions=(_exec(contract_id=None),))))
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(
                positions=(SessionPositionFact(None, "ABC", "STK", "USD", D("100"), DONE),))))

    def test_fractional_share_quantity_fails_closed(self):
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(executions=(_exec(quantity=D("100.5")),))))

    def test_non_usd_currency_fails_closed(self):
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(executions=(_exec(currency="EUR"),))))
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(executions=(_exec(commission_currency="EUR"),))))

    def test_unsupported_side_fails_closed(self):
        with self.assertRaises(AdapterError):
            facts_to_shadow_observation(_observation(_facts(executions=(_exec(side="SHORT"),))))


if __name__ == "__main__":
    unittest.main()
