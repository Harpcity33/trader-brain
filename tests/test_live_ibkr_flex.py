"""Synthetic, hermetic Flex protocol tests; not live broker feed proof."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from titan_brain.live.broker.ibkr_flex import (
    FLEX_BASE_URL, IbkrFlexError, IbkrFlexQuery, IbkrFlexReportReader,
    _NoRedirects, parse_ibkr_flex_activity_report,
)


NOW = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
DAY = date(2026, 9, 14)
ACCOUNT = "U900004567"  # Synthetic; deliberately not a protected account suffix.
TOKEN = "123456789012345678901234"
QUERY = IbkrFlexQuery("123456", ACCOUNT, DAY, DAY)
SUCCESS = b'''<FlexStatementResponse><Status>Success</Status>
<ReferenceCode>987654321</ReferenceCode><url>https://attacker.invalid/collect</url>
</FlexStatementResponse>'''
REPORT = f'''<FlexQueryResponse queryName="synthetic" type="AF">
<FlexStatements count="1"><FlexStatement accountId="{ACCOUNT}" fromDate="20260914" toDate="20260914">
<AccountInformation accountId="{ACCOUNT}" currency="USD"/>
<ChangeInNAV accountId="{ACCOUNT}" fromDate="20260914" toDate="20260914"
startingValue="1000.00" endingValue="1155.00" depositsWithdrawals="150"
internalCashTransfers="0" assetTransfers="0"/>
<CashTransactions>
<CashTransaction accountId="{ACCOUNT}" currency="USD" fxRateToBase="1"
reportDate="20260914" dateTime="20260913;101010" amount="150" type="Deposits/Withdrawals"/>
<CashTransaction accountId="{ACCOUNT}" currency="USD" fxRateToBase="1"
reportDate="20260914" dateTime="20260913;111111" amount="5" type="Dividends"/>
</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>'''.encode()


def parse(raw=REPORT, *, query=QUERY, now=NOW):
    return parse_ibkr_flex_activity_report(raw, query=query, received_at=now)


class Response:
    def __init__(self, raw):
        self.raw = raw
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, limit):
        self.read_limits.append(limit)
        return self.raw[:limit]


class FlexParserTests(unittest.TestCase):
    def test_period_values_are_preserved_without_live_authority(self):
        report = parse()
        self.assertEqual(report.nav.starting_value, Decimal("1000.00"))
        self.assertEqual(report.nav.ending_value, Decimal("1155.00"))
        self.assertEqual(report.nav.deposits_withdrawals, Decimal("150"))
        self.assertEqual([row.amount for row in report.cash_transactions], [Decimal("150"), Decimal("5")])
        self.assertEqual(report.cash_transactions[1].transaction_type, "Dividends")
        self.assertEqual(
            [row.provider_report_date for row in report.cash_transactions],
            [DAY, DAY],
        )
        self.assertEqual(
            [row.provider_date_time for row in report.cash_transactions],
            ["20260913;101010", "20260913;111111"],
        )
        self.assertEqual(
            [row.source_row_ordinal for row in report.cash_transactions], [1, 2]
        )
        self.assertFalse(report.daily_starting_equity_ready)
        with self.assertRaisesRegex(IbkrFlexError, "LIVE_DAILY_EVIDENCE_UNSUPPORTED"):
            report.require_live_daily_evidence()
        self.assertEqual(report.response_sha256, hashlib.sha256(REPORT).hexdigest())
        self.assertEqual(report.received_at, NOW)
        self.assertEqual(report.response_origin, "local_unverified_bytes")
        summary = report.diagnostic_summary()
        self.assertIsNone(summary["live_cash_flow_complete_through"])
        self.assertIsNone(summary["midnight_valuation_at"])
        self.assertNotIn(ACCOUNT, json.dumps(summary))
        self.assertNotIn(ACCOUNT, repr(report))
        self.assertNotIn("provider_report_date", json.dumps(summary))
        self.assertNotIn("provider_report_date", repr(report))
        self.assertFalse(hasattr(report, "account_scope_sha256"))
        self.assertTrue(
            all(
                not hasattr(row, "source_row_sha256")
                for row in report.cash_transactions
            )
        )

    def test_no_account_or_row_fingerprint_is_exposed(self):
        report = parse()
        public = json.dumps(report.diagnostic_summary(), sort_keys=True)
        old_account_fingerprint = hashlib.sha256(
            json.dumps(
                {"provider": "ibkr_activity_flex", "account_id": ACCOUNT},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotIn("account_scope_sha256", public)
        self.assertNotIn(old_account_fingerprint, public)
        self.assertNotIn(ACCOUNT, public)
        for ordinal, row in enumerate(report.cash_transactions, start=1):
            self.assertEqual(row.source_row_ordinal, ordinal)
            self.assertFalse(hasattr(row, "source_row_sha256"))
        # The report-wide digest is retained only as a private exact-byte
        # integrity/equality identifier.  It is deliberately not described as
        # redaction, anonymization, or an account-scope proof.
        self.assertEqual(report.response_sha256, hashlib.sha256(REPORT).hexdigest())

    def test_missing_transfer_fields_are_unknown_not_zero(self):
        for field in (b'depositsWithdrawals="150"', b'internalCashTransfers="0"', b'assetTransfers="0"'):
            report = parse(REPORT.replace(field, b""))
            self.assertIn(None, (report.nav.deposits_withdrawals, report.nav.internal_cash_transfers, report.nav.asset_transfers))
            self.assertFalse(report.daily_starting_equity_ready)

    def test_empty_and_absent_cash_sections_are_distinct_and_neither_proves_zero_flows(self):
        before, rest = REPORT.split(b"<CashTransactions>")
        _, after = rest.split(b"</CashTransactions>")
        for section, present in ((b"<CashTransactions/>", True), (b"", False)):
            report = parse(before + section + after)
            self.assertEqual(report.cash_transactions, ())
            self.assertEqual(report.cash_transactions_section_present, present)
            self.assertFalse(report.daily_starting_equity_ready)

    def test_rejects_wrong_account_even_with_same_last_four(self):
        with self.assertRaisesRegex(IbkrFlexError, "ACCOUNT_MISMATCH"):
            parse(REPORT.replace(ACCOUNT.encode(), b"U800004567"))
        with self.assertRaisesRegex(IbkrFlexError, "ACCOUNT_MISMATCH"):
            parse(REPORT.replace(b'<CashTransaction accountId="' + ACCOUNT.encode(), b'<CashTransaction accountId="U800004567', 1))

    def test_rejects_multiple_duplicate_model_and_foreign_account_scopes(self):
        variants = (
            REPORT.replace(b'count="1"', b'count="2"'),
            REPORT.replace(b"</FlexStatements>", b'<FlexStatement accountId="U800004567"/></FlexStatements>'),
            REPORT.replace(b"</FlexQueryResponse>", b"<FlexStatements count=\"0\"/></FlexQueryResponse>"),
            REPORT.replace(b'<ChangeInNAV ', b'<ChangeInNAV model="model1" '),
            REPORT.replace(b"</FlexStatement>", b'<IgnoredSection accountId="U800004567"/></FlexStatement>'),
        )
        for raw in variants:
            with self.subTest(raw_hash=hashlib.sha256(raw).hexdigest()), self.assertRaises(IbkrFlexError):
                parse(raw)

    def test_requires_actual_period_and_usd_account_information(self):
        variants = (
            REPORT.replace(b'fromDate="20260914"', b'fromDate="20260913"', 1),
            REPORT.replace(b'fromDate="20260914"', b'fromDate="20260931"'),
            REPORT.replace(b'currency="USD"', b'currency="EUR"', 1),
            REPORT.replace(b"AccountInformation", b"MissingAccountInformation"),
            REPORT.replace(b"ChangeInNAV", b"MissingChangeInNAV"),
            REPORT.replace(b'type="AF"', b'type="TC"'),
        )
        for raw in variants:
            with self.subTest(raw_hash=hashlib.sha256(raw).hexdigest()), self.assertRaises(IbkrFlexError):
                parse(raw)

    def test_duplicate_nav_and_cash_sections_are_rejected_not_selected_or_combined(self):
        nav_start = REPORT.index(b"<ChangeInNAV ")
        nav_end = REPORT.index(b"/>", nav_start) + 2
        nav = REPORT[nav_start:nav_end]
        for raw, code in (
            (REPORT.replace(nav, nav + nav), "PERIOD_NAV_REQUIRED"),
            (REPORT.replace(b"</CashTransactions>", b"</CashTransactions><CashTransactions/>"), "CASH_SECTION_AMBIGUOUS"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(IbkrFlexError, code):
                parse(raw)

    def test_report_date_controls_period_while_provider_date_time_is_preserved(self):
        report = parse()
        self.assertEqual(report.cash_transactions[0].provider_report_date, DAY)
        self.assertEqual(report.cash_transactions[0].provider_date_time, "20260913;101010")
        self.assertFalse(report.daily_starting_equity_ready)
        summary = report.diagnostic_summary()
        self.assertTrue(summary["reporting_only"])
        self.assertFalse(summary["daily_starting_equity_ready"])
        self.assertIsNone(summary["live_cash_flow_complete_through"])
        self.assertIsNone(summary["midnight_valuation_at"])

    def test_rejects_missing_malformed_or_out_of_period_cash_report_date(self):
        for replacement in (
            b'',
            b'reportDate="2026-09-14" ',
            b'reportDate="20260931" ',
            b'reportDate="20260913" ',
            b'reportDate="20260915" ',
        ):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(
                IbkrFlexError, "CASH_(?:REPORT_DATE_INVALID|PERIOD_MISMATCH)"
            ):
                parse(REPORT.replace(b'reportDate="20260914" ', replacement, 1))

    def test_rejects_invalid_numbers_date_times_fx_and_missing_required_cash_fields(self):
        for field, replacement in (
            (b'startingValue="1000.00"', b'startingValue="NaN"'),
            (b'endingValue="1155.00"', b'endingValue="1e3"'),
            (b'amount="150"', b'amount=""'),
            (b'fxRateToBase="1"', b'fxRateToBase="0"'),
            (b'fxRateToBase="1"', b'fxRateToBase="2"'),
            (b'fxRateToBase="1"', b''),
            (b'20260913;101010', b'20260913-101010'),
            (b'20260913;101010', b'20260913;251010'),
            (b'20260913;101010', b'20260913;106010'),
            (b'20260913;101010', b'20260931;101010'),
            (b'20260913;101010', b'20260229;101010'),
            (b'type="Dividends"', b'type=""'),
        ):
            with self.subTest(replacement=replacement), self.assertRaises(IbkrFlexError):
                parse(REPORT.replace(field, replacement))

    def test_multicurrency_rows_are_not_silently_summed(self):
        raw = REPORT.replace(b'currency="USD" fxRateToBase="1"', b'currency="EUR" fxRateToBase="1.1"', 1)
        report = parse(raw)
        self.assertEqual(report.cash_transactions[0].currency, "EUR")
        self.assertEqual(report.cash_transactions[0].amount, Decimal("150"))
        self.assertEqual(report.cash_transactions[0].fx_rate_to_base, Decimal("1.1"))
        self.assertFalse(hasattr(report, "daily_external_cash_flow"))

    def test_untrusted_xml_is_bounded_and_rejected_without_disclosing_input(self):
        for raw in (b"", b"x" * (8 * 1024 * 1024 + 1), b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///private">]><x>&x;</x>', b"<xml>", b"\xff", REPORT.replace(b"<FlexQueryResponse", b"<FlexQueryResponse\x00")):
            with self.assertRaises(IbkrFlexError) as caught:
                parse(raw)
            self.assertNotIn(ACCOUNT, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_later_download_never_relabels_statement_date_as_midnight(self):
        report = parse(now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(report.from_date, DAY)
        self.assertEqual(report.to_date, DAY)
        self.assertFalse(report.daily_starting_equity_ready)
        with self.assertRaisesRegex(IbkrFlexError, "CLOCK_INVALID"):
            parse(now=NOW.replace(tzinfo=None))


class FlexReaderTests(unittest.TestCase):
    def setUp(self):
        self.elapsed = 100.0
        self.responses = [Response(SUCCESS), Response(REPORT)]
        self.requests = []
        self.token_reads = 0

        def token_reader():
            self.token_reads += 1
            return TOKEN

        def opener(request, timeout):
            self.requests.append((request, timeout))
            return self.responses.pop(0)

        self.reader = IbkrFlexReportReader(token_reader=token_reader, opener=opener,
            clock=lambda: NOW, monotonic=lambda: self.elapsed)

    def test_two_stage_protocol_uses_fixed_endpoint_and_exact_reference_with_no_constructor_io(self):
        self.assertEqual(self.token_reads, 0)
        ticket = self.reader.request_report(QUERY)
        self.assertNotIn(ACCOUNT, repr(ticket))
        self.assertNotIn(TOKEN, repr(self.reader))
        self.elapsed += 6
        report = self.reader.retrieve_report(ticket)
        self.assertEqual(len(self.requests), 2)
        first, second = (urlsplit(request.full_url) for request, _ in self.requests)
        self.assertEqual(first.netloc, urlsplit(FLEX_BASE_URL).netloc)
        self.assertEqual(second.netloc, first.netloc)
        self.assertTrue(first.path.endswith("/SendRequest"))
        self.assertTrue(second.path.endswith("/GetStatement"))
        self.assertEqual(parse_qs(first.query), {"t": [TOKEN], "q": [QUERY.query_id], "fd": ["20260914"], "td": ["20260914"], "v": ["3"]})
        self.assertEqual(parse_qs(second.query)["q"], ["987654321"])
        self.assertTrue(all(request.get_method() == "GET" and timeout == 10 for request, timeout in self.requests))
        self.assertEqual(report.response_origin, "injected_transport_unverified_bytes")
        self.assertEqual(report.generation_response_sha256, hashlib.sha256(SUCCESS).hexdigest())
        self.assertFalse(report.daily_starting_equity_ready)

    def test_pacing_blocks_without_waiting_or_reading_token(self):
        ticket = self.reader.request_report(QUERY)
        for elapsed in (100, 105.99, 99):
            self.elapsed = elapsed
            with self.assertRaisesRegex(IbkrFlexError, "PACING_WAIT_REQUIRED"):
                self.reader.retrieve_report(ticket)
        self.assertEqual(self.token_reads, 1)
        self.assertEqual(len(self.requests), 1)

    def test_not_ready_is_explicit_and_retry_reuses_ticket_not_new_report(self):
        self.responses.insert(1, Response(b'<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode><ErrorMessage>secret</ErrorMessage></FlexStatementResponse>'))
        ticket = self.reader.request_report(QUERY)
        self.elapsed += 6
        with self.assertRaisesRegex(IbkrFlexError, "PROVIDER_1019"):
            self.reader.retrieve_report(ticket)
        self.assertEqual(len(self.requests), 2)
        with self.assertRaisesRegex(IbkrFlexError, "PACING_WAIT_REQUIRED"):
            self.reader.retrieve_report(ticket)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.token_reads, 2)
        self.elapsed += 6
        self.reader.retrieve_report(ticket)
        self.assertEqual([urlsplit(item.full_url).path.rsplit("/", 1)[-1] for item, _ in self.requests], ["SendRequest", "GetStatement", "GetStatement"])

    def test_failed_generation_produces_no_ticket_or_automatic_retry(self):
        self.responses.insert(0, Response(b'<FlexStatementResponse><Status>Fail</Status><ErrorCode>1001</ErrorCode></FlexStatementResponse>'))
        with self.assertRaisesRegex(IbkrFlexError, "PROVIDER_1001"):
            self.reader.request_report(QUERY)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaisesRegex(IbkrFlexError, "PACING_WAIT_REQUIRED"):
            self.reader.request_report(QUERY)
        self.elapsed += 6
        ticket = self.reader.request_report(QUERY)
        self.elapsed += 6
        self.assertFalse(self.reader.retrieve_report(ticket).daily_starting_equity_ready)
        self.assertEqual([urlsplit(item.full_url).path.rsplit("/", 1)[-1] for item, _ in self.requests], ["SendRequest", "SendRequest", "GetStatement"])

    def test_errors_cannot_echo_token_url_body_or_account(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError(f"https://bad/?t={TOKEN}&account={ACCOUNT}")
        for kwargs, expected in (({"opener": broken}, "TRANSPORT_FAILED"), ({"token_reader": broken}, "TOKEN_UNAVAILABLE")):
            params = {"token_reader": lambda: TOKEN, "clock": lambda: NOW, **kwargs}
            reader = IbkrFlexReportReader(**params)
            with self.assertRaises(IbkrFlexError) as caught:
                reader.request_report(QUERY)
            self.assertEqual(str(caught.exception), f"IBKR_FLEX_{expected}")
            self.assertTrue(caught.exception.__suppress_context__)

    def test_provider_errors_malformed_success_and_oversized_responses_are_not_reports(self):
        for raw in (
            b'<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>secret</ErrorMessage></FlexStatementResponse>',
            SUCCESS.replace(b"Success", b"Unknown"),
            SUCCESS.replace(b"<Status>Success</Status>", b""),
            SUCCESS.replace(
                b"<ReferenceCode>",
                b"<ErrorCode>1015</ErrorCode><ErrorMessage>secret</ErrorMessage><ReferenceCode>",
            ),
            SUCCESS.replace(b"987654321", b"bad?token=secret"),
            SUCCESS.replace(b"</FlexStatementResponse>", b"<ReferenceCode>1</ReferenceCode></FlexStatementResponse>"),
            b"x" * 65537,
        ):
            reader = IbkrFlexReportReader(token_reader=lambda: TOKEN, opener=lambda *_args, **_kwargs: Response(raw), clock=lambda: NOW)
            with self.assertRaises(IbkrFlexError) as caught:
                reader.request_report(QUERY)
            self.assertNotIn("secret", str(caught.exception))

    def test_redirects_are_refused_and_foreign_tickets_never_read_credentials(self):
        with self.assertRaisesRegex(IbkrFlexError, "REDIRECT_REFUSED"):
            _NoRedirects().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")
        ticket = self.reader.request_report(QUERY)
        other = IbkrFlexReportReader(token_reader=lambda: self.fail("must not read token"))
        with self.assertRaisesRegex(IbkrFlexError, "TICKET_MISMATCH"):
            other.retrieve_report(ticket)

    def test_invalid_configuration_or_current_new_york_day_does_not_make_requests(self):
        for timeout in (0, -1, 31, float("nan"), float("inf"), True):
            with self.assertRaisesRegex(IbkrFlexError, "TIMEOUT_INVALID"):
                IbkrFlexReportReader(token_reader=lambda: TOKEN, timeout_seconds=timeout)
        for query in (replace(QUERY, to_date=NOW.date()),):
            with self.assertRaisesRegex(IbkrFlexError, "HISTORICAL_PERIOD_REQUIRED"):
                self.reader.request_report(query)
        reader = IbkrFlexReportReader(token_reader=lambda: self.fail("must not read token"), clock=lambda: datetime(2026, 9, 15, 1, tzinfo=timezone.utc))
        with self.assertRaisesRegex(IbkrFlexError, "HISTORICAL_PERIOD_REQUIRED"):
            reader.request_report(QUERY)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
