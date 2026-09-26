# Reconciliation repair and paused trading desk — September 15, 2026

## Scope and current disposition

The owner requested that the trading heartbeat remain paused until autonomous
trading is genuinely ready, and requested fixes for the P&L timeout, order
coverage, daily starting-equity/external-flow evidence, remaining position risk
and no-bypass execution contract. This report distinguishes new engineering
from the pre-existing Gmail installation and unproved broker capabilities.
No broker order or broker precaution was changed during this repair.

The supported Codex automation update returned `PAUSED` for
`robinhood-titan-premarket-deep-dive` (display name **IBKR Titan — Premarket
Analysis + Live Desk**), and the persisted status was checked. The older
`robinhood-momentum-engine` and `robinhood-titan-quiet-briefs` were already
paused. Separate research/operations automations were not changed. The desk
must not be resumed just because a build passes. Its ET/DST schedule also needs
verification before resumption; the previously observed out-of-window triggers
must not be fixed with a permanent UTC offset that ignores daylight saving.

## Fresh connection evidence before the repair build

The installed read-only probe began at `2026-09-15T10:58:07.570758+00:00`:

| Component | Observed result |
|---|---|
| Massive REST | Authenticated market-status request succeeded at 10:58:07.717756 UTC |
| Massive WSS | Authentication handshake succeeded at 10:58:07.858644 UTC; not continuous-ingestion proof |
| IB Gateway | `LOCAL_ASSEMBLY_IBKR_READ_CONNECTION_FAILED`, before account/P&L/contract collection |
| Gmail | Actual OAuth refresh succeeded at 10:58:08.145372 UTC; no new message sent |

The local port check found no listener at `127.0.0.1:4001`. Native Gateway
10.45 was at its login screen, not an authenticated API session. The owner was
asked to sign in and complete authentication without sharing a password or
code. This supersedes the earlier successful Gateway discovery; it does not
prove why the preceding session ended. It is a different failure from the
September 14 P&L timeout. No broker flatness claim can be made from local zero
position counts.

The separate notification LaunchAgent was observed running as PID 61264 with
an advancing worker lease. Its durable outbox contained one
`DELIVERED / PROVIDER_ACCEPTED` test and no pending message. The owner has now
explicitly stated **“Test email acknowledged.”** That is recorded here as
receipt of the earlier test, not a new delivery, a fresh readiness timestamp,
or a fabricated exact CLI confirmation. The earlier message remains
`ca14af5c-079e-51b3-9347-f28d79e07335`; no resend was made in this repair.

The pre-existing `com.titan.momentum-watcher` was separately observed running
as PID 63065. Its installed `run` command calls `TitanWatcher` for Massive data
collection, not the IBKR trading coordinator; its diagnostic explicitly has
no broker capability. It was not newly installed, restarted or granted trading
authority by this repair. Pausing the trading heartbeat does not disable that
separate data collector or the independent notification delivery worker.

## Executable repairs, and limits of their evidence

| Requested gate | Newly implemented | Still not proved |
|---|---|---|
| P&L timeout | One fresh-request-ID read retry inside the original overall deadline; no callback versus explicit unavailable, dispatch, cancellation, deadline and disconnect failures distinguished; final freeze rechecks health and authentication atomically | This account's fresh P&L collection, because Gateway is logged out |
| Order coverage / recovery | A newer failed read invalidates cached pages and exact-reference evidence; stable account collection requires matching family watermarks; late callbacks from a retired P&L subscription cannot satisfy the retry | Current-open plus current-day-completed coverage is bounded, not exhaustive history or authoritative proof an unknown submission never existed |
| Starting balance / flows | New fixed-endpoint HTTPS Activity Flex reader and exact-account XML parser; original accounting period, NAV transfer components and transactions preserved; missing fields remain unknown | Actual configured query/token/report; an approved measurement boundary; authentic intraday complete-through transfer evidence |
| Remaining open-position risk | Diagnostic stop-allocation arithmetic, duplicate/quantity checks and remaining-order fee-floor checks; separate error codes identify the authentic valuation-time, common-NLV-epoch and all-in-fee gaps | Caller-shaped inputs do not authenticate broker positions/stops or ownership. Production new-entry blocks remain, including unknown/submitting replay exclusions and unreleased filled reservations |
| No-bypass execution | Command order callbacks record redacted warning presence without retaining warning text or treating it as acceptance | Account-specific supported no-bypass transmission and protection/closeout behavior with existing data entitlements |

The P&L retry is a read-only subscription retry, **not an order resubmission**.
Unknown order submissions are not retried. `PreSubmitted` remains queued, not
verified working protection. A clean local state database is not proof of a
flat brokerage account. All policy bindings, financial limits, mandatory
confirmations and activation interlocks are unchanged.

Independent review additionally caught and repaired an account-fingerprint
privacy leak in the new Flex diagnostics and a potential cross-policy
UNKNOWN-classification regression before deployment. Raw report digests remain
private, linkable integrity identifiers, not anonymization. UNKNOWN precedence
is preserved for legacy policies, while the daily builder independently blocks
filled unknown reservations even when a broker snapshot appears flat. Idle
authentication loss now also retires earlier valuation/page/reference inputs.

The Flex reader is new executable library code and is included in the release;
it is not yet wired to an enrolled reporting credential, CLI or live risk issuer.
Its historical-report readiness method deliberately cannot authorize trading.
The setup document explains that distinction rather than manufacturing an
authenticated zero-flow assertion from an empty report.

## Broker contract: what documentation does and does not establish

Current IBKR documentation says precautionary warnings can require manual
acknowledgement and transmission. Its separate API-precautions page describes
bypass settings; none was enabled here. General owner authorization cannot
prove that this account's existing entitlements and external market-data route
avoid those warnings while required precautions remain enabled.
[Order precautions](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/understanding-order-precautions),
[API precautions](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/order-precautions).

The exact remaining answer is whether account ending 3103 can submit the
approved regular-hours whole-share limit entries, protective stop-market
orders and closeout orders with the existing no-new-cost data arrangement,
without manual Transmit, warning suppression, percentage-constraint override
or advanced-error override. A provider-confirmed supported route and verified
account settings are still required. Public API existence, a WhatIf margin
preview or a socket handoff does not prove live order acceptance, working
protection, no-borrow capacity or an all-in fee upper bound. No live order was
used as a diagnostic.

IBKR also documents periodic reauthentication. Autonomous strategy decisions
do not eliminate brokerage login requirements; recurring human authentication
cannot be represented as permanently solved by a daemon.
[Reauthentication](https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication).

## One proposed measurement amendment, not an approval

The existing financial restrictions remain unchanged: +15% daily aspiration
and an irreversible entry lock at −10%, measured against fixed daily starting
whole-account equity; no borrowing/debit, restricted instruments or overnight
exposure; no premarket trading; and no added paid service. Neither percentage
is a guaranteed outcome or a guaranteed maximum realized loss.

The implementation added a midnight valuation and aligned five-second
whole-account valuation/flow watermark. Those are not established by the
owner's words “daily starting balance,” nor by the TWS sources checked here.
The single proposed amendment is to freeze **preceding completed broker daily
accounting-period ending NAV** as the next trading day's starting balance,
retaining its actual period boundary rather than relabeling it midnight. All
external cash/asset flows must still be authentically reconciled through the
valuation used for decisions, and must never count as trading gains or reset
an existing lock. Missing or corrected flow evidence blocks new entries.

This is a genuine measurement-boundary choice for the owner, not permission
to assume zero flows. It alone cannot clear the live-flow blocker. The new
[Flex setup and evidence boundary](IBKR_FLEX_DAILY_EVIDENCE_SETUP_2026-09-15.md)
details the supported reporting source, exact private token/query setup, and
the remaining need for a real account-specific report and an intraday
complete-through feed. No policy amendment or new authority was activated.

## Verification and deployment

Code and tests are committed as
`651acfea7ac35699a2e9378ab00692b6980ffedd` on
`codex/full-live-2026-09-08`. The deployment report is a later documentation
commit and is not misrepresented as the executable release's source revision.

Two builds from a clean detached worktree produced identical bytes:

- Release: `c7776fbf2d240c9fae3adaccaefb1c7e7a63fa88164d51c5c000e3b8d93f7c50`
- Archive SHA-256: `114ace0eafce162694e054939f7d4662f227931d0e051b156803380364ae9eb2`
- Manifest-file SHA-256: `2e2d7415a127a82d29895599c789e02b3776c45882c23ea3f939b9220615743b`
- Packaged files: 108; default mode `PAUSED`.
- Unchanged config hash: `c756c6f75c895d14b6e3b4b3349c6655bbf50d1d39001ab385b233fc67c61cdd`
- Unchanged policy hash: `f54292e461f5c27f721b9ffe939a267539af0358e8d43a994520ac94ee7e984e`

Clean-source repository validation passed (171 Python, 32 JSON, 4 TOML files),
with validation manifest
`a5be3491f03ad9cec1228961f8ceb7f8329e3cbfded18859d1ca05718ff57172`.
The exact committed source then passed **1,254 tests in 60.830 seconds**, no
failures or skips, using Python 3.12.14 and the existing official IBKR SDK:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/titan-gmail-recovery.MXOgVX/sdk-venv/bin/python -B -m unittest discover -s tests
```

That command ran from the clean detached worktree at
`/tmp/titan-reconciliation-repair.hKjH0e/source`; it remained clean afterward.
Earlier 1,248-test working-tree runs passed with the SDK and with two expected
SDK-availability skips in the plain interpreter. Those earlier runs are not
substituted for the final exact-commit result. All are hermetic tests, not
claims of live broker acceptance or production latency.

The supported paused installer completed at
`2026-09-15T11:25:01.929713+00:00` into:

```text
/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103
```

It verified all 108 committed release files and reused the pinned official
`ibapi 10.50.2` / `protobuf 5.29.5` snapshot, inventory
`3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc`.
It retained the state and prior release, rebound identity from generation 9 to
10, and made no broker call or launchctl call. Post-install `status` verified
the new release and runtime bindings, `mode=PAUSED`, `authority_enabled=0`, no
activation timestamp and no trading service writer. The trading coordinator
LaunchAgent is not loaded. No readiness or activation record was fabricated.

Before installation, the existing email worker was gracefully stopped and its
lease release was verified at `11:24:38.989453 UTC`; the outbox had no pending
messages. After installation, its existing live plist was byte-identical to
the newly staged plist. The email worker alone was restarted: PID 71488,
notification lease generation 3, no prior exit, and an advancing heartbeat.
The one existing delivered test remained unchanged. No test was resent, no
new email was sent, and no historical acknowledgement was backdated into a
fresh release-bound activation receipt.

The new installed read-only provider probe began at
`2026-09-15T11:25:18.919814+00:00`:

| Connection | Actual result |
|---|---|
| Massive REST | Authenticated market-status read at 11:25:19.191116 UTC |
| Massive WSS | Authenticated handshake at 11:25:19.572600 UTC, not sustained-stream qualification |
| Gmail | Actual OAuth refresh at 11:25:19.909399 UTC, no send |
| IBKR Gateway | `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_CONNECTION_NOT_ESTABLISHED`; account, P&L and contract reads not reached |
| Flex reporting | Not configured or authenticated; executable library is installed only |

The probe's generic Gmail delivery-test note is not a new finding that the
owner failed to receive the acknowledged test: this connectivity probe does
not consult the durable delivery outbox or owner confirmation. Historical
delivery, current OAuth connectivity, and a fresh activation-bound delivery
receipt are separate facts.

Installed `readiness` exited 2 with `COMMAND_FAILED:BrokerFactoryError` and did
not produce passing evidence. Installed `doctor` also exited 2, while proving
release validity and a valid 33-event audit chain. Its unchanged signed
configuration still selects `ibkr_local_gateway_staged`, `attended_only`,
required per-mutation confirmation, disabled mutation interlock and no live
discovery pipeline. These were not flipped to clear blockers. Its static
blocker labels for API acknowledgement, notification destination and other
staged items are not fresh proof that the owner's earlier declarations or
acknowledged email did not occur; they identify evidence/configuration still
unbound to activation. Dynamic provider checks are reported separately above.

The market-data diagnostic observed a completed minute bar at `11:25:00 UTC`
and a quote at `11:25:48.824 UTC`, but still returned
`MASSIVE_HEALTH_STALE:massive_websocket` / `producer_fresh=false`. A running
collector or successful new handshake does not clear that producer-health
gate. Read-only follow-up identified the mismatch in the separate legacy
collector: its `massive_websocket.checked_at` advances on status frames, while
ordinary quote/bar frames update the separate `market_data_freshness` row.
At `11:27:33 UTC`, the latter was healthy with age 0 seconds but the websocket
status was 22.9 seconds old against a 15-second adapter limit. This observation
does not establish stale prices. The doctor's fallback also lacks calendar
context and defaults to entry-eligible timing during premarket.

The durable repair for that separate collector is a rate-limited heartbeat
from validated data on an authenticated connection, preserving degraded/error
states and the independent quote/bar checks; restarting merely resets the old
status timestamp temporarily. This follow-up was diagnostic only: the legacy
collector was not patched, restarted or relabeled healthy. Qualification of
the selected full-live producer remains required. No full-live coordinator or
market-data readiness is claimed.

## Required path to genuine readiness — not an activation instruction

1. Restore the existing Gateway login and complete required authentication.
   Re-run installed read-only account/P&L, all-client order and contract probes;
   retain every pending/unknown reference and prove actual exposure. The P&L
   retry has not yet been exercised against this account after installation.
2. Resolve the single proposed daily accounting boundary with the owner,
   configure the private Flex token/query and validate a real report. Establish
   a supported intraday complete-through external cash/asset-flow source; do
   not promote historical reporting, current balance or assumed zero flows.
3. Obtain and verify the account-specific no-bypass order/transmission contract,
   unchanged precaution settings, current entitlements, no-borrow capacity,
   all-in fee bounds, exhaustive reconciliation/recovery coverage and actual
   stop/closeout state semantics. Generic API documentation is insufficient.
4. Bind authentic open-position valuation time, common account-NLV epoch and
   remaining fee evidence, and repair/qualify producer-health freshness before
   crediting open P&L or enabling the live discovery/quality join. The new
   diagnostic arithmetic alone does not satisfy this step.
5. Only after all supported providers and contracts pass, qualify the complete
   installed composition and independent delivery receipt, issue the genuine
   bound authority artifacts, and use the existing guarded activation flow.
   Resume the trading heartbeat only after autonomous readiness is actually
   proved, with its ET/DST schedule verified. Do not bypass confirmations,
   clear static booleans as evidence, erase unresolved state, or promise
   permanent login availability.

**Installed:** the repaired release and existing pinned SDK. **Authenticated:**
the actual Massive and Gmail checks above. **Running:** independent notification
delivery and the pre-existing data collector. **Not running:** autonomous
trading or the trading heartbeat. No GitHub push was performed in this repair.
