#!/usr/bin/env python3
"""Replay synthetic observations in a temporary database. Never contact a broker."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from titan_brain.lifecycle_lab import Journal, OrderEvidence, daily_budget, protection_review


def main():
    now = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
    account, contract = 'synthetic-account-only', 'SYNTH261009C00100000'
    with TemporaryDirectory(prefix='trader-brain-observation-replay-') as folder:
        path = Path(folder) / 'evidence.sqlite3'
        journal = Journal(path, account=account)
        journal.expect('open-1', contract, 'buy', 2)
        event = OrderEvidence(account, 'open-1', 'broker-1', contract, 'buy', 2,
                              0, '0', '0', 'accepted', now)
        acknowledged = journal.observe(event, now=now)
        now += timedelta(seconds=1)
        event = replace(event, filled=1, notional='45', state='partially_filled', observed_at=now)
        first_fill = journal.observe(event, now=now)
        duplicate = journal.observe(event, now=now)
        now += timedelta(seconds=1)
        cancelled = journal.observe(replace(event, state='cancelled', observed_at=now), now=now)
        reopened = Journal(path, account=account)
        recovery = reopened.recover(account=account, positions={contract:1},
                                     observed_at=now, now=now, complete=True)
        review = protection_review(contract=contract, position_qty=1, position_at=now,
            quote_contract=contract, bid='.35', ask='.37', bid_size=3, quote_at=now,
            stop='.40', target='.70', close_cutoff=now+timedelta(hours=5), now=now,
            closing_order_pending=False, working_protection_confirmed=False)
        print(json.dumps({
            'mode': 'SYNTHETIC_OBSERVATION_REPLAY',
            'acknowledgement': acknowledged, 'partial_fill': first_fill,
            'duplicate': duplicate, 'cancelled_remainder': cancelled,
            'restart_recovery': recovery, 'protection_review_not_an_order': review,
            'daily_preferences': daily_budget(starting_equity='500', current_equity='475', net_external_flows='0'),
            'broker_connection_used': False, 'real_orders_sent': 0,
            'real_position_protection_provided': False,
        }, indent=2))


if __name__ == '__main__': main()
