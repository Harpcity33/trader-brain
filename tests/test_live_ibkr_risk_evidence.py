"""Hermetic coverage for autonomous IBKR risk-evidence enrichment."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from titan_brain.live.broker.base import (
    AccountSnapshot,
    ClientRefRecoverySource,
    FundsSnapshot,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
)
from titan_brain.live.broker.ibkr_risk_evidence import (
    DailyIbkrRiskBaselineAuthenticator,
    IBKR_DAILY_RISK_BASELINE_SCHEMA,
    IbkrDailyRiskBindingProvider,
    IbkrRiskEvidenceAccountSnapshotReader,
    IbkrRiskEvidenceBindings,
    IbkrRiskEvidenceEnricher,
    IbkrRiskEvidenceError,
    IbkrRiskHighWaterLedger,
    VerifiedIbkrDailyRiskBaseline,
    load_verified_ibkr_daily_risk_baseline,
)
from titan_brain.live.calendar import ExchangeCalendar
from titan_brain.live.policy import canonical_json
from titan_brain.live.provider_clients import KeychainItem


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
TRADING_DATE = date(2026, 9, 14)
PRIOR_TRADING_DATE = date(2026, 9, 11)
SECRET = b"risk-baseline-test-secret-material-32-bytes-minimum"
RELEASE = "a" * 64
CONFIG = "b" * 64
POLICY = "c" * 64
RISK = "d" * 64
ACCOUNT_BINDING = "e" * 64
PROVIDER_RECEIPT = "f" * 64
MASK = "****3103"


class RecordingKeyReader:
    def __init__(self, secret: bytes = SECRET, error: Exception | None = None):
        self.secret = secret
        self.error = error
        self.calls: list[KeychainItem] = []

    def read(self, item: KeychainItem) -> bytes:
        self.calls.append(item)
        if self.error is not None:
            raise self.error
        return self.secret


class FixedClock:
    def __init__(self, value: datetime = NOW):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def bindings(
    *,
    release: str = RELEASE,
    risk: str = RISK,
    trading_date: date = TRADING_DATE,
    prior_trading_date: date = PRIOR_TRADING_DATE,
) -> IbkrRiskEvidenceBindings:
    return IbkrRiskEvidenceBindings(
        release_manifest_hash=release,
        config_hash=CONFIG,
        policy_binding_id=POLICY,
        risk_binding_id=risk,
        account_key="ibkr-live-ending-3103",
        account_masked=MASK,
        account_binding_fingerprint=ACCOUNT_BINDING,
        valid_for_trading_date=trading_date,
        prior_trading_date=prior_trading_date,
    )


def receipt_payload(
    expected: IbkrRiskEvidenceBindings,
    *,
    week_to_date: str = "-20.25",
    prior_high_water: str = "1200.00",
    provider_receipt: str = PROVIDER_RECEIPT,
    provider_source: str = "interactive-brokers:account-statement",
    issued_at: datetime = NOW - timedelta(minutes=1),
    provider_observed_at: datetime = NOW - timedelta(minutes=2),
    secret: bytes = SECRET,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": IBKR_DAILY_RISK_BASELINE_SCHEMA,
        "issued_at": issued_at.isoformat(),
        "bindings": {
            "release_manifest_hash": expected.release_manifest_hash,
            "config_hash": expected.config_hash,
            "policy_binding_id": expected.policy_binding_id,
            "risk_binding_id": expected.risk_binding_id,
            "account_key": expected.account_key,
            "account_masked": expected.account_masked,
            "account_binding_fingerprint": expected.account_binding_fingerprint,
            "valid_for_trading_date": expected.valid_for_trading_date.isoformat(),
            "prior_trading_date": expected.prior_trading_date.isoformat(),
        },
        "evidence": {
            "currency": "USD",
            "week_to_date_realized_pnl_through_prior_trading_day": week_to_date,
            "prior_high_water_equity": prior_high_water,
            "broker_authoritative": True,
            "account_scope": "exact_account",
            "provider_source": provider_source,
            "provider_observed_at": provider_observed_at.isoformat(),
            "provider_receipt_sha256": provider_receipt,
        },
    }
    body["hmac_sha256"] = hmac.new(
        secret,
        canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return body


def write_receipt(path: Path, payload: dict[str, object], *, canonical: bool = True) -> None:
    encoded = (
        canonical_json(payload) + "\n"
        if canonical
        else json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    path.write_text(encoded, encoding="utf-8")
    path.chmod(0o600)


def order_coverage() -> OrderCoverageContract:
    return OrderCoverageContract(
        contract_version="ibkr-current-day-roster-test-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.INCOMPLETE,
                evidence_id=f"page:{family.value}",
                broker_authoritative=True,
                all_pages_consumed=True,
                includes_working_orders_across_dates=True,
                includes_parent_child_conditional=(
                    family is OrderFamily.ADVANCED_EQUITY
                ),
            )
            for family in OrderFamily
        ),
        client_ref_recovery_source=ClientRefRecoverySource.CURRENT_DAY_ORDER_ROSTER,
        broker_preserves_client_ref=True,
        negative_client_ref_results_authoritative=False,
    )


def raw_snapshot(
    *,
    now: datetime = NOW,
    daily_realized: str = "-12.50",
    net_liquidation: str = "1000.00",
) -> AccountSnapshot:
    return AccountSnapshot(
        account_masked=MASK,
        observed_at=now,
        received_at=now,
        account_state="active",
        account_type="no_borrow_margin",
        funds=FundsSnapshot(
            total_value=Decimal(net_liquidation),
            cash=Decimal("900"),
            buying_power=Decimal("900"),
            unleveraged_buying_power=Decimal("900"),
        ),
        equity_positions=(),
        equity_orders=(),
        option_position_count=0,
        option_order_count=0,
        advanced_order_count=0,
        standard_equity_positions_complete=True,
        standard_equity_orders_complete=False,
        option_positions_complete=True,
        option_orders_complete=False,
        advanced_orders_complete=False,
        auth_point_in_time=True,
        daily_realized_pnl=Decimal(daily_realized),
        daily_realized_pnl_complete=True,
        weekly_realized_pnl_complete=False,
        peak_equity_complete=False,
        risk_evidence_authoritative=True,
        risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
        risk_evidence_as_of=now,
    )


class RawSnapshotReader:
    def __init__(self, snapshots: list[AccountSnapshot]):
        self.snapshots = snapshots
        self.coverage = order_coverage()
        self.calls = 0

    def release_components(self):
        return ()

    def __call__(self) -> AccountSnapshot:
        item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return item


class IbkrDailyRiskBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "daily-risk-baseline.json"
        self.expected = bindings()
        write_receipt(self.path, receipt_payload(self.expected))

    def assert_code(self, code: str, call) -> None:
        with self.assertRaises(IbkrRiskEvidenceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_authenticates_exact_private_canonical_provider_receipt(self) -> None:
        verified = load_verified_ibkr_daily_risk_baseline(
            self.path,
            secret=SECRET,
            expected=self.expected,
            now=NOW,
        )
        self.assertEqual(verified.bindings, self.expected)
        self.assertEqual(
            verified.week_to_date_realized_pnl_through_prior_trading_day,
            Decimal("-20.25"),
        )
        self.assertEqual(verified.prior_high_water_equity, Decimal("1200.00"))
        self.assertEqual(verified.currency, "USD")
        self.assertEqual(verified.provider_receipt_sha256, PROVIDER_RECEIPT)
        self.assertEqual(len(verified.receipt_hash), 64)
        self.assertEqual(verified.verified_at, NOW)

        with self.assertRaises(IbkrRiskEvidenceError):
            VerifiedIbkrDailyRiskBaseline(
                bindings=self.expected,
                issued_at=NOW,
                provider_observed_at=NOW,
                week_to_date_realized_pnl_through_prior_trading_day=Decimal("0"),
                prior_high_water_equity=Decimal("1"),
                currency="USD",
                provider_source="interactive-brokers:test",
                provider_receipt_sha256=PROVIDER_RECEIPT,
                receipt_hash="1" * 64,
                verified_at=NOW,
            )

    def test_authenticator_reads_key_per_use_and_inventories_dependencies(self) -> None:
        key_reader = RecordingKeyReader()
        clock = FixedClock()
        authenticator = DailyIbkrRiskBaselineAuthenticator(
            path=self.path,
            key_reader=key_reader,
            key_item=KeychainItem(
                service="titan-full-live-ibkr-risk-baseline",
                account="ibkr-live-ending-3103",
            ),
            expected=self.expected,
            clock=clock,
        )
        self.assertEqual(authenticator().receipt_hash, authenticator().receipt_hash)
        self.assertEqual(len(key_reader.calls), 2)
        self.assertEqual(
            authenticator.release_components(),
            (
                ("ibkr_risk_baseline_key_loader", key_reader, ("read",)),
                ("ibkr_risk_baseline_clock", clock, ("__call__",)),
            ),
        )

    def test_missing_stale_mismatched_tampered_or_noncanonical_fails_closed(self) -> None:
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BASELINE_FILE_UNAVAILABLE",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.root / "missing.json",
                secret=SECRET,
                expected=self.expected,
                now=NOW,
            ),
        )
        self.assert_code(
            "IBKR_RISK_EVIDENCE_TRADING_DATE_MISMATCH",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.path,
                secret=SECRET,
                expected=self.expected,
                now=NOW + timedelta(days=1),
            ),
        )
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BINDING_MISMATCH",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.path,
                secret=SECRET,
                expected=bindings(risk="9" * 64),
                now=NOW,
            ),
        )

        tampered = receipt_payload(self.expected)
        tampered["evidence"][  # type: ignore[index]
            "prior_high_water_equity"
        ] = "9999"
        write_receipt(self.path, tampered)
        self.assert_code(
            "IBKR_RISK_EVIDENCE_HMAC_INVALID",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.path, secret=SECRET, expected=self.expected, now=NOW
            ),
        )

        payload = receipt_payload(self.expected)
        write_receipt(self.path, payload, canonical=False)
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BASELINE_FILE_NOT_CANONICAL",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.path, secret=SECRET, expected=self.expected, now=NOW
            ),
        )
        write_receipt(self.path, payload)
        self.path.chmod(0o644)
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BASELINE_FILE_UNSAFE",
            lambda: load_verified_ibkr_daily_risk_baseline(
                self.path, secret=SECRET, expected=self.expected, now=NOW
            ),
        )

    def test_provider_authority_currency_and_receipt_identity_are_mandatory(self) -> None:
        for field, value, code in (
            ("currency", "EUR", "IBKR_RISK_EVIDENCE_CURRENCY_UNSUPPORTED"),
            (
                "broker_authoritative",
                False,
                "IBKR_RISK_EVIDENCE_BROKER_AUTHORITY_UNPROVEN",
            ),
            (
                "account_scope",
                "masked_account",
                "IBKR_RISK_EVIDENCE_ACCOUNT_SCOPE_UNPROVEN",
            ),
            (
                "provider_receipt_sha256",
                "not-a-hash",
                "IBKR_RISK_EVIDENCE_PROVIDER_RECEIPT_SHA256_INVALID",
            ),
        ):
            payload = receipt_payload(self.expected)
            payload["evidence"][field] = value  # type: ignore[index]
            unsigned = dict(payload)
            unsigned.pop("hmac_sha256")
            payload["hmac_sha256"] = hmac.new(
                SECRET,
                canonical_json(unsigned).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            write_receipt(self.path, payload)
            self.assert_code(
                code,
                lambda: load_verified_ibkr_daily_risk_baseline(
                    self.path, secret=SECRET, expected=self.expected, now=NOW
                ),
            )


class IbkrRiskEvidenceEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.receipt_path = self.root / "daily-risk-baseline.json"
        self.ledger_path = self.root / "risk-high-water.sqlite3"
        self.expected = bindings()
        self.clock = FixedClock()
        write_receipt(self.receipt_path, receipt_payload(self.expected))
        self.key_reader = RecordingKeyReader()
        self.authenticator = DailyIbkrRiskBaselineAuthenticator(
            path=self.receipt_path,
            key_reader=self.key_reader,
            key_item=KeychainItem(
                service="titan-full-live-ibkr-risk-baseline",
                account="ibkr-live-ending-3103",
            ),
            expected=self.expected,
            clock=self.clock,
        )
        self.ledger = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=self.expected.ledger_bindings,
            allow_create=True,
        )
        self.addCleanup(self.ledger.close)
        self.enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=self.authenticator,
            high_water_ledger=self.ledger,
            snapshot_max_age_seconds=5,
        )

    def assert_code(self, code: str, call) -> None:
        with self.assertRaises(IbkrRiskEvidenceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_combines_prior_week_to_date_with_fresh_reqpnl_and_peak(self) -> None:
        enriched = self.enricher.enrich(raw_snapshot())
        self.assertEqual(enriched.daily_realized_pnl, Decimal("-12.50"))
        self.assertEqual(enriched.weekly_realized_pnl, Decimal("-32.75"))
        self.assertEqual(enriched.peak_equity, Decimal("1200.00"))
        self.assertTrue(enriched.entry_risk_evidence_ready)
        self.assertIn(
            "ibkr:reqPnL.realizedPnL:current-day+authenticated-daily-baseline",
            enriched.risk_evidence_source,
        )
        self.assertIn(PROVIDER_RECEIPT, enriched.risk_evidence_source)
        self.assertEqual(enriched.risk_evidence_as_of, NOW)
        self.assertTrue(enriched.authenticated_entry_risk_evidence_ready)
        self.assertEqual(len(enriched.risk_baseline_identity_hash or ""), 64)
        self.assertEqual(len(enriched.risk_baseline_receipt_hash or ""), 64)
        self.assertEqual(len(enriched.risk_high_water_identity_hash or ""), 64)
        self.assertEqual(len(enriched.risk_high_water_receipt_hash or ""), 64)

    def test_reusable_enrich_interface_persists_monotone_peak_across_restart(self) -> None:
        first = self.enricher.enrich(raw_snapshot(net_liquidation="1250"))
        second = self.enricher.enrich(raw_snapshot(net_liquidation="1100"))
        self.assertEqual(first.peak_equity, Decimal("1250"))
        self.assertEqual(second.peak_equity, Decimal("1250"))
        self.ledger.close()

        reopened = IbkrRiskHighWaterLedger(
            self.ledger_path, bindings=self.expected.ledger_bindings
        )
        self.addCleanup(reopened.close)
        restarted = IbkrRiskEvidenceEnricher(
            baseline_authenticator=self.authenticator,
            high_water_ledger=reopened,
            snapshot_max_age_seconds=5,
        )
        after_restart = restarted.enrich(raw_snapshot(net_liquidation="1150"))
        self.assertEqual(after_restart.peak_equity, Decimal("1250"))

    def test_runtime_never_bootstraps_missing_ledger_and_recreation_changes_lineage(self) -> None:
        before = self.enricher.enrich(raw_snapshot(net_liquidation="1300"))
        self.assertEqual(before.peak_equity, Decimal("1300"))
        self.ledger.close()
        self.ledger_path.unlink()

        self.assert_code(
            "IBKR_RISK_EVIDENCE_LEDGER_MISSING",
            lambda: IbkrRiskHighWaterLedger(
                self.ledger_path,
                bindings=self.expected.ledger_bindings,
            ),
        )
        self.assertFalse(self.ledger_path.exists())

        replacement = IbkrRiskHighWaterLedger(
            self.ledger_path,
            bindings=self.expected.ledger_bindings,
            allow_create=True,
        )
        self.addCleanup(replacement.close)
        replaced = IbkrRiskEvidenceEnricher(
            baseline_authenticator=self.authenticator,
            high_water_ledger=replacement,
            snapshot_max_age_seconds=5,
        ).enrich(raw_snapshot(net_liquidation="1100"))
        self.assertEqual(replaced.peak_equity, Decimal("1200"))
        self.assertNotEqual(
            replaced.risk_high_water_lineage_hash,
            before.risk_high_water_lineage_hash,
        )
        self.assertNotEqual(
            replaced.risk_high_water_identity_hash,
            before.risk_high_water_identity_hash,
        )
        self.assertNotEqual(
            replaced.risk_high_water_receipt_hash,
            before.risk_high_water_receipt_hash,
        )

    def test_same_day_receipt_change_is_rejected(self) -> None:
        self.enricher.enrich(raw_snapshot())
        write_receipt(
            self.receipt_path,
            receipt_payload(
                self.expected,
                provider_receipt="8" * 64,
            ),
        )
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BASELINE_RECEIPT_CHANGED",
            lambda: self.enricher.enrich(raw_snapshot()),
        )

    def test_calendar_provider_hot_rotates_daily_receipts_without_restart(self) -> None:
        rotating_receipt = self.root / "rotating-daily-risk-baseline.json"
        rotating_ledger_path = self.root / "rotating-risk-high-water.sqlite3"
        calendar = ExchangeCalendar(
            {
                "calendar_year": 2026,
                "regular_open_et": "09:30",
                "regular_close_et": "16:00",
                "closed_dates": ["2026-09-07"],
                "early_close_dates": {},
                "source_url": "https://www.nyse.com/markets/hours-calendars",
            }
        )
        provider = IbkrDailyRiskBindingProvider(
            ledger_bindings=self.expected.ledger_bindings,
            calendar=calendar,
        )
        monday = provider(TRADING_DATE)
        self.assertEqual(monday.prior_trading_date, PRIOR_TRADING_DATE)
        write_receipt(rotating_receipt, receipt_payload(monday))
        authenticator = DailyIbkrRiskBaselineAuthenticator(
            path=rotating_receipt,
            key_reader=self.key_reader,
            key_item=self.authenticator.key_item,
            expected=provider,
            clock=self.clock,
        )
        rotating_ledger = IbkrRiskHighWaterLedger(
            rotating_ledger_path,
            bindings=self.expected.ledger_bindings,
            allow_create=True,
        )
        self.addCleanup(rotating_ledger.close)
        enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=authenticator,
            high_water_ledger=rotating_ledger,
            snapshot_max_age_seconds=5,
        )
        self.assertEqual(
            enricher.enrich(raw_snapshot(net_liquidation="1250")).peak_equity,
            Decimal("1250"),
        )

        self.clock.value = NOW + timedelta(days=1)
        tuesday = provider(date(2026, 9, 15))
        self.assertEqual(tuesday.prior_trading_date, TRADING_DATE)
        write_receipt(
            rotating_receipt,
            receipt_payload(
                tuesday,
                week_to_date="-32.75",
                prior_high_water="1250",
                provider_receipt="6" * 64,
                issued_at=self.clock.value - timedelta(minutes=1),
                provider_observed_at=self.clock.value - timedelta(minutes=2),
            ),
        )
        result = enricher.enrich(
            raw_snapshot(now=self.clock.value, daily_realized="10")
        )
        self.assertEqual(result.weekly_realized_pnl, Decimal("-22.75"))
        self.assertEqual(result.peak_equity, Decimal("1250"))
        self.assertEqual(
            tuple(item[0] for item in authenticator.release_components()),
            (
                "ibkr_risk_baseline_key_loader",
                "ibkr_risk_baseline_clock",
                "ibkr_daily_risk_binding_provider",
            ),
        )

        self.clock.value = NOW + timedelta(days=2)
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BINDING_MISMATCH",
            lambda: enricher.enrich(raw_snapshot(now=self.clock.value)),
        )

    def test_new_day_requires_new_receipt_and_cannot_regress_prior_peak(self) -> None:
        self.enricher.enrich(raw_snapshot(net_liquidation="1300"))
        self.clock.value = NOW + timedelta(days=1)
        self.assert_code(
            "IBKR_RISK_EVIDENCE_TRADING_DATE_MISMATCH",
            lambda: self.enricher.enrich(
                raw_snapshot(now=self.clock.value, net_liquidation="1200")
            ),
        )
        self.ledger.close()

        next_bindings = bindings(
            trading_date=date(2026, 9, 15),
            prior_trading_date=TRADING_DATE,
        )
        write_receipt(
            self.receipt_path,
            receipt_payload(
                next_bindings,
                week_to_date="-32.75",
                prior_high_water="1299",
                provider_receipt="7" * 64,
                issued_at=self.clock.value - timedelta(minutes=1),
                provider_observed_at=self.clock.value - timedelta(minutes=2),
            ),
        )
        next_authenticator = DailyIbkrRiskBaselineAuthenticator(
            path=self.receipt_path,
            key_reader=self.key_reader,
            key_item=self.authenticator.key_item,
            expected=next_bindings,
            clock=self.clock,
        )
        reopened = IbkrRiskHighWaterLedger(
            self.ledger_path, bindings=next_bindings.ledger_bindings
        )
        self.addCleanup(reopened.close)
        next_enricher = IbkrRiskEvidenceEnricher(
            baseline_authenticator=next_authenticator,
            high_water_ledger=reopened,
            snapshot_max_age_seconds=5,
        )
        self.assert_code(
            "IBKR_RISK_EVIDENCE_BASELINE_HIGH_WATER_REGRESSION",
            lambda: next_enricher.enrich(
                raw_snapshot(now=self.clock.value, net_liquidation="1200")
            ),
        )

    def test_incomplete_wrong_source_stale_and_already_enriched_snapshots_fail(self) -> None:
        cases = (
            (
                replace(raw_snapshot(), daily_realized_pnl_complete=False),
                "IBKR_RISK_EVIDENCE_CURRENT_DAY_REALIZED_PNL_UNPROVEN",
            ),
            (
                replace(
                    raw_snapshot(),
                    risk_evidence_source="ibkr:AccountSummary.RealizedPnL",
                ),
                "IBKR_RISK_EVIDENCE_CURRENT_DAY_REALIZED_PNL_UNPROVEN",
            ),
            (
                raw_snapshot(now=NOW - timedelta(seconds=6)),
                "IBKR_RISK_EVIDENCE_CURRENT_DAY_REALIZED_PNL_STALE",
            ),
            (
                replace(
                    raw_snapshot(),
                    weekly_realized_pnl=Decimal("0"),
                    weekly_realized_pnl_complete=True,
                ),
                "IBKR_RISK_EVIDENCE_RAW_SNAPSHOT_ALREADY_ENRICHED",
            ),
        )
        for snapshot, code in cases:
            with self.subTest(code=code):
                self.assert_code(code, lambda snapshot=snapshot: self.enricher.enrich(snapshot))

    def test_callable_wrapper_delegates_coverage_and_inventories_complete_graph(self) -> None:
        raw_reader = RawSnapshotReader([raw_snapshot()])
        wrapper = IbkrRiskEvidenceAccountSnapshotReader(
            snapshot_reader=raw_reader,
            enricher=self.enricher,
        )
        self.assertIs(wrapper.coverage, raw_reader.coverage)
        self.assertTrue(wrapper().entry_risk_evidence_ready)
        self.assertEqual(
            wrapper.enrich(raw_snapshot(net_liquidation="1100")).peak_equity,
            Decimal("1200.00"),
        )
        self.assertEqual(
            wrapper.release_components(),
            (
                (
                    "ibkr_raw_account_snapshot_reader",
                    raw_reader,
                    ("release_components", "coverage", "__call__"),
                ),
                (
                    "ibkr_risk_evidence_enricher",
                    self.enricher,
                    ("release_components", "enrich", "close"),
                ),
            ),
        )
        self.assertEqual(
            tuple(item[0] for item in self.enricher.release_components()),
            (
                "ibkr_daily_risk_baseline_authenticator",
                "ibkr_risk_high_water_ledger",
            ),
        )

    def test_callable_reader_preserves_raw_broker_facts_when_baseline_is_unusable(self) -> None:
        raw = replace(
            raw_snapshot(),
            weekly_realized_pnl=Decimal("999999"),
            peak_equity=Decimal("999999"),
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
        )
        raw_reader = RawSnapshotReader([raw])
        wrapper = IbkrRiskEvidenceAccountSnapshotReader(
            snapshot_reader=raw_reader,
            enricher=self.enricher,
        )

        for failure in ("missing", "tampered"):
            with self.subTest(failure=failure):
                if failure == "missing":
                    self.receipt_path.unlink(missing_ok=True)
                else:
                    payload = receipt_payload(self.expected)
                    payload["evidence"][  # type: ignore[index]
                        "prior_high_water_equity"
                    ] = "999999"
                    write_receipt(self.receipt_path, payload)

                available = wrapper()

                self.assertEqual(available.account_masked, raw.account_masked)
                self.assertEqual(available.funds, raw.funds)
                self.assertEqual(available.equity_positions, raw.equity_positions)
                self.assertEqual(available.equity_orders, raw.equity_orders)
                self.assertTrue(available.daily_realized_pnl_ready)
                self.assertEqual(
                    available.risk_evidence_source,
                    "ibkr:reqPnL.realizedPnL:current-day",
                )
                self.assertFalse(available.entry_risk_evidence_ready)
                self.assertFalse(available.authenticated_entry_risk_evidence_ready)
                self.assertIsNone(available.weekly_realized_pnl)
                self.assertIsNone(available.peak_equity)
                self.assertIsNone(available.risk_baseline_identity_hash)
                self.assertIsNone(available.risk_baseline_receipt_hash)
                self.assertIsNone(available.risk_high_water_identity_hash)
                self.assertIsNone(available.risk_high_water_receipt_hash)

                with self.assertRaises(IbkrRiskEvidenceError):
                    wrapper.enrich(raw_snapshot())

        write_receipt(self.receipt_path, receipt_payload(self.expected))
        ledger_failure_reader = IbkrRiskEvidenceAccountSnapshotReader(
            snapshot_reader=RawSnapshotReader([raw_snapshot()]),
            enricher=self.enricher,
        )
        with mock.patch.object(
            self.ledger,
            "observe",
            side_effect=IbkrRiskEvidenceError(
                "IBKR_RISK_EVIDENCE_LEDGER_WRITE_FAILED"
            ),
        ):
            unavailable_ledger = ledger_failure_reader()
        self.assertTrue(unavailable_ledger.daily_realized_pnl_ready)
        self.assertFalse(unavailable_ledger.entry_risk_evidence_ready)
        self.assertFalse(unavailable_ledger.authenticated_entry_risk_evidence_ready)
        self.assertIsNone(unavailable_ledger.weekly_realized_pnl)
        self.assertIsNone(unavailable_ledger.peak_equity)
        self.assertIsNone(unavailable_ledger.risk_baseline_identity_hash)
        self.assertIsNone(unavailable_ledger.risk_baseline_receipt_hash)
        self.assertIsNone(unavailable_ledger.risk_high_water_identity_hash)
        self.assertIsNone(unavailable_ledger.risk_high_water_receipt_hash)

    def test_ledger_reopen_with_different_release_binding_fails(self) -> None:
        self.enricher.enrich(raw_snapshot())
        self.ledger.close()
        self.assert_code(
            "IBKR_RISK_EVIDENCE_LEDGER_BINDING_MISMATCH",
            lambda: IbkrRiskHighWaterLedger(
                self.ledger_path,
                bindings=bindings(release="9" * 64).ledger_bindings,
            ),
        )


if __name__ == "__main__":
    unittest.main()
