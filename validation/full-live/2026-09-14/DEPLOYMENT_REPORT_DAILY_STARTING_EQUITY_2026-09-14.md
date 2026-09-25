# IBKR daily starting-equity amendment — executable deployment report

Recorded September 14, 2026 America/New_York; installed at
2026-09-15 01:16:38 UTC. **Implemented, tested, installed PAUSED. Not
activation-ready; no autonomous trading or independent delivery worker is
running.** This report supersedes the prior report's dollar-risk terms and
unresolved daily-drawdown-policy decision. It does not supersede its still
unresolved provider, authority, notification, or activation gates.

## Approved policy now implemented

The owner requested a 15% daily increase and no more than a 10% loss, then
specified that these replace the previous goals and use daily starting
balance. The executable IBKR model is
`account_day_starting_equity_percentage`:

- Fixed authenticated beginning-of-day total account equity/NetLiquidation,
  effective 00:00 America/New_York; never cash, buying power, a later first
  read, or a movable intraday peak.
- Daily return uses current total equity minus net external cash flows minus
  that fixed starting equity. Unrealized P&L and incurred fees count through
  equity. Deposits and withdrawals do not manufacture performance.
- +15% is aspirational, not guaranteed, a forced trade/exit, or a profit ceiling.
- At or below −10%, irreversibly lock entries for the account day and route
  through the existing guarded closeout path. A later recovery or deposit
  cannot clear the latch.
- The old −$100 lock, +$150 goal and +$125 post-goal floor are no longer active
  IBKR rules. Their historical artifacts and legacy tests remain intact.
- Aggregate new/pending downside and reserves fit the lesser of the original
  10% daily budget and remaining adjusted-equity headroom. Profits do not
  automatically enlarge the original budget.

Example only: a $2,000 authenticated daily start gives a $300 profit
aspiration and a $200 loss-control threshold. Neither is a promised outcome.
Gaps, slippage, halts, failed protection or unavailable required approvals can
produce a larger actual loss. No new financial policy decision is needed to
restate the approved percentages.

The original owner approval is unchanged. The new immutable
[amendment](OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md) is
hash-bound into policy, builder and installer:
`78571bb3f19157d5f2a8d81976ba7a4a4f0bdc782683b276130b59ca1627c2ad`.

## New executable work, not a repackaging of prior work

1. Added strict percentage-policy schemas, verified approval-byte binding,
   fixed-baseline math, sizing, candidate/pending fee accounting and irreversible
   goal/loss observations. No approval boolean can replace the exact contract.
2. Added authenticated midnight-equity and current cash-flow evidence, bound
   to the exact account, date, current valuation and receipt time. Production
   authentication requires the new schema; old realized-P&L receipts cannot
   authorize it. Missing flow is not zero. Stale, mismatched, conflicting,
   regressing or rebased evidence blocks entries.
3. Added risk-ledger v4 support and tested migration/release continuity of the
   frozen baseline and cash-flow watermark. Copying a receipt never renews it.
4. Applied the new rule in discovery sizing, shared exposure construction,
   final attended/autonomous preflight and service loss/closeout routing.
   Failed persistence cannot silently succeed. Existing pause/closeout state
   survives, including a later incomplete observation.
5. Fixed a discovered profit-giveback risk: original entry-to-stop reserves
   are not a safe remaining-risk valuation after account equity moves.
   Additional entries now fail closed with
   `DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED` while open/manual exposure or
   an unreleased filled reservation remains. The final broker check cannot
   evade this using a newer flat position snapshot or a working original stop.
   Protection and exits retain their guarded paths.
6. Updated the existing IBKR heartbeat to the same policy and limitation.
   Preserved analysis-only premarket at 07:00, 07:30, 08:00, 08:30 and 09:00;
   one-minute 09:30–15:59 weekday cadence; no entries before 09:35 or after
   15:30; and exact attended confirmations. This is not daemon activation.

Earlier streaming, bar, reconciliation, protection/closeout, notification
outbox and activation implementations are carried forward, not claimed as new
in this amendment.

## Verification and exact deployment identity

Complete [machine-readable evidence](TEST_AND_DEPLOYMENT_DAILY_STARTING_EQUITY_2026-09-14.json)
contains test output, both build records, installer receipt, installed
diagnostics and verified heartbeat settings.

| Item | Verified result |
|---|---|
| Local executable commit | `184ccb1e3ae31e48ac227f4fd2ad35ad2567e5d0` |
| GitHub executable mirror | `94cc5a3c7ecd167557c38174465f69184adfae96` |
| Identical source tree | `1fea78de0d3330a41232ac8eb354563032428a7b` |
| Bundled Python suite | 1,132 tests, 59.984 s, PASS; 2 SDK-dependent skips |
| Official IBKR SDK suite | 1,132 tests, 60.199 s, PASS; no skips |
| Repository / whitespace validation | PASS / PASS |
| Clean deterministic builds | Two archives with identical SHA-256 |
| Archive SHA-256 | `e7b4fd73f439b41206e3008530eb3b0e4f75954d6491462c2dd29318521b0482` |
| Installed release ID | `def43832f745c04559fde9283fade91240212cd2177f72fd045c38800bc97843` |
| Manifest-file SHA-256 | `df2765ffcf5131b49db16fc4fd5fc0f7db855aa75d67fb0f64cfb8cd304b5613` |
| Runtime / authority / generation | `PAUSED` / `0` / `5` |
| Activation / writer lease | null / absent |
| Trading and notification launchd services | Both absent, `launchctl print` exit 113 |
| Official SDK | ibapi 10.50.2, protobuf 5.29.5; 303-file pinned inventory attested |
| `doctor` / `readiness` | Exit 2, blockers retained / exit 2, `BrokerFactoryError` |

Installed below
`/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`.
The installer accessed no broker and started no service. The PAUSED runtime
identity was rebound to the new release; the local risk ledger was **not**
migrated because the current staged profile returns `RISK_LEDGER_NOT_CONFIGURED`.
Ledger-v4 migration is implemented and exercised in tests, not represented as
a populated live ledger. Local zero-position counters are not broker-confirmed
flatness. Later report-only commits do not change the installed executable
commit. Local and GitHub commit IDs differ because retained ancestry differs;
the source trees were verified identical without a force update.

## Connections, remaining engineering and activation

This policy-only turn successfully used GitHub publication and the Codex
automation update service. It did **not** make fresh IBKR/Massive network
probes, change Gateway settings, provision credentials, send email or submit
orders. Prior authenticated reads are historical, not renewed by this report;
see [the preceding connection report](DEPLOYMENT_REPORT_REMAINING_GATES_2026-09-14.md).

Still required:

- **Trusted baseline/flow producer and refresh integration.** The verifier,
  binding and ledger exist; no authentic upstream issuer was provisioned.
  Receipt signatures are local application controls, not invented IBKR
  certificates. No arbitrary first-read baseline is allowed.
- **Current open-position remaining-risk valuation.** Fresh mark-to-stop and
  remaining-fee evidence is unfinished; additional entries block as described
  above. Pending unfilled orders still share the proved budget.
- **Broker/provider gates.** Reliable current-day P&L callbacks, exact
  No-Borrow/same-day-proceeds capacity, API acknowledgement/classification and
  entitlement, no-bypass external-data place/cancel contract, exhaustive
  client/reference coverage, all-in Tiered fee bound, and a fresh unchanged
  precaution-state check remain unproved. No broker control was bypassed.
- **Independent delivery.** Fresh metadata-only diagnostics show all five
  account-scoped Gmail Keychain items missing; no route is ready and no test
  delivery occurred. Owner-approved destination/test consent and authentic
  durable send authorization remain required.
- **Production wiring and recovery.** Supported provider selection and real
  release/account/control/route evidence require a reviewed rebuild. The
  previously disclosed orphan final-risk-observation recovery CLI remains
  unimplemented; deleting incidents is not recovery.
- **Live cutover.** Broker-backed protection/closeout and real independent
  delivery must be verified before owner-controlled activation. Full autonomy
  remains the objective, not the current running state.

All other instrument, no-borrow, whole-share, no-ADD/reentry/options/shorting,
session, fee/reserve, protection, reconciliation, notification and no-overnight
restrictions remain unchanged. Additional service spending: **$0**.

The user-controlled path remains in [OPERATIONS](../2026-09-08/OPERATIONS.md):
finish authentic provider setup and separate owner-only choices, rebuild and
install the supported profile PAUSED, explicitly start reconcile-only and the
independent delivery worker, verify the current route and broker safety paths,
collect real readiness evidence, and only then prepare and consume the exact
short-lived release-bound activation. **Do not run activation against this
staged release.** The saved attended heartbeat remains active and must be
retired through the verified single-writer cutover procedure, not silently.
