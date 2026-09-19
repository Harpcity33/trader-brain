# Kiro handoff: finish the Titan / IBKR session-risk integration

## Start here

This is a **source-code handoff, not a trade-ready deployment**. The owner wants
the remaining engineering completed one area at a time, with concrete
acceptance evidence. The owner also wants the option to intervene manually;
the account must not be treated as exclusive to Titan.

The implementation checkpoint before this handoff document is
`cc3d3e4fd9aa33291839500f59d77380197742e5` (September 18, 2026). The GitHub
handoff is a source-tree mirror on `handoff/kiro-2026-09-18`, based on the
existing remote full-live branch without modifying `main`. Its publication
commit records the local source commit and identical Git tree; local historical
commit IDs need not be reachable in the mirrored GitHub ancestry. Historical
test/build claims below refer to that implementation checkpoint, not to a
future edited checkout. Inspect the current Git revision and worktree before
working. Preserve unrelated changes.

Do not activate trading, place test orders, replace credentials, clear safety
incidents, reset a same-day baseline, or weaken a readiness gate to make the
project appear complete. Finish and test the missing implementation. A new
model-selection flag is not a substitute for its required consumers and real
evidence. Final live activation is a distinct owner action after acceptance.

## What the owner has already decided

The selected measurement is **session trading P&L against the fixed pre-trade
balance**, not midnight whole-account return. The authoritative versioned
amendment is
[`OWNER_SESSION_TRADING_PNL_AMENDMENT_2026-09-18.md`](validation/full-live/2026-09-18/OWNER_SESSION_TRADING_PNL_AMENDMENT_2026-09-18.md),
with SHA-256
`da60ce0060764aa4cffdb04c1de04e4af89ffeb914b2ef479d3c90f2e1db1577`.
Read it in full before changing risk behavior. Its corresponding contract is
[`config/risk_limits_ibkr_session_trading.json`](config/risk_limits_ibkr_session_trading.json).

In brief:

- Freeze positive USD net liquidation value from genuine, reconciled, flat
  pre-entry broker observations, bound to the exact account and New York day.
  Never reset or enlarge it because of restart, reauthentication, deposits,
  profits, or a release change.
- Trading P&L is sale execution proceeds minus purchase execution costs plus
  fresh residual long-inventory bid value minus actual signed execution fees,
  with duplicate/corrected fills and fees reconciled and counted once.
- Non-trading adjustments stay outside that numerator and require separate
  accounting reconciliation. Unknown adjustments, assets, or coverage are not
  zero. Withdrawals and obligations still reduce spendable capacity.
- At P&L at or below minus 10% of the fixed balance, irreversibly lock entries
  for the day and invoke the ordinary guarded closeout path.
- Before the loss boundary, aggregate downside capacity is
  `max(0, min(0.10 * baseline, 0.10 * baseline + session_pnl))`. Charge all open,
  pending, uncovered, unresolved and proposed downside, future fees and
  positive execution reserves. Do not double-charge incurred fees already in
  P&L. Gains cannot enlarge the original 10% ceiling.
- The 15% profit figure is an aspiration, not a promise, forced trade, forced
  exit, profit ceiling, or post-goal floor. The post-goal floor remains null.
- Preserve pending-before-read ordering, immutable history, sticky data
  incidents and the loss latch. A later healthy observation does not prove a
  blind interval was safe and cannot silently clear it.
- Other approved long-stock, whole-share, no-borrowing, cash-capacity,
  no-ADD/re-entry, quality, protection, no-overnight and notification rules
  remain in force. Keep original approval files byte-for-byte unchanged.

Ten percent is a control trigger, not a guaranteed maximum loss under gaps,
halts, outages or slippage. Fifteen percent is not an expected daily return.
Do not represent the new model using invented legacy weekly/realized-P&L,
midnight-equity, external-flow or high-water evidence.

## Observed deployment and Gateway state

The packaging-time passive installed-runtime check at **18:54 EDT on September 18**
reported valid older release
`55348ca21c29c8dd431510e7c3e04e557b3df399f2a937cae18aeb147a1ba357`,
generation 16, **PAUSED**, authority disabled, no activation timestamp and no
writer lease. This is a dated observation, not a current broker-flatness or
readiness claim. The newer source checkpoint was not installed. The checked-in
IBKR profile still selects the legacy risk model, staged transport,
attended-only execution, disabled mutation interlock and unconfigured automated
discovery. Do not simply toggle these to live.

Subsequently, the owner approved a reporting-only Gateway change. The settings
UI was observed with **Master API client ID 19735**, bulk-data timeout **30**,
and **Component Exch. Separator blank**. Apply and then OK were clicked and the
dialog closed without an error. No other trading control was changed. A
post-restart persistence check and functional account-wide reporting proof
have **not** been recorded. This observation supersedes older documents saying
the Master field was blank or authorization was pending.

Master-client reporting is not by itself proof of complete manual/TWS/FIX or
other-client visibility. Determine and test actual supported scope; do not
silently switch to client 0, bind orders, or alter execution ownership. The
owner explicitly wants manual intervention when necessary.

**Pause-new-entries is not a full manual takeover.** Existing protection,
reconciliation, exits and closeout can continue while entries are paused.
Before launch, implement and prove a handoff/resume workflow that cannot
produce competing exits, duplicate orders, overselling or stale-plan reuse,
and that preserves the fixed baseline, loss latch and incident state. Any
required broker-control change must be surfaced separately; the reporting
change is not blanket authority to change order ownership.

## Repairs already implemented and their evidence

The implementation record is
[`SESSION_CAPTURE_AND_RISK_REPAIR_2026-09-18.md`](validation/full-live/2026-09-18/SESSION_CAPTURE_AND_RISK_REPAIR_2026-09-18.md).
Its earlier Master-field paragraph is superseded by the observation above.

- The new session capture path uses matching exact-account
  `reqAccountUpdatesMulti` callbacks/end/cancel, not repeated capped
  account-summary requests or `reqPnL`. Cleanup uncertainty fails closed.
  Legacy readers retain their own scope and are not silently redefined.
- Same-collection exposure capture preserves actual account values, positions
  and order-family pages without promoting them to authenticated legacy risk
  or exhaustive history.
- Strict evidence-based validation recognizes eligible `PreSubmitted` stops;
  a generic queued or ambiguous order is still unprotected. This recognizes
  supported broker facts, not a fill guarantee.
- Session risk store v2 preserves all audit events, pending/failed incidents,
  unique tokens and the irreversible loss latch while compacting healthy
  in-memory state. V1 is rejected rather than silently reset or migrated.
- A separate typed observation ledger saves actual observations. The risk
  pending marker commits before capture, evidence commits before calculation,
  and crash windows retain unresolved risk. Full reopen verifies the chain.
  The unkeyed local chain is not broker-source authentication or malicious
  rollback prevention.
- Cash/fill/fee reconciliation retains unexplained residuals; it does not
  substitute NLV for cash or invent external adjustments.
- Separate policy, pipeline-builder and risk-evaluator paths support the
  approved session model without fabricated legacy fields. The cent-rounding
  regression is covered under low Decimal precision. These components are
  **partial integration**, not the complete production path.

Historical validation at the implementation checkpoint:

- **1,698 tests passed in 84.341 seconds**, including the pinned official SDK
  wire test. Repository validation and whitespace checks passed. This is not a
  fresh test result for a later checkout or an endorsement of live readiness.
- Two actual bounded, read-only three-capture rehearsals passed at **11:15 and
  11:26 EDT on September 18**. The previously failing third read completed in
  598 ms in both. The later rehearsal matched the observed cash identity; raw
  evidence and risk stores reopened identically. Command connections remained
  closed. These are not full-day or all-client coverage tests.
- A compressed store benchmark committed 11,700 begin/complete pairs, 23,401
  FULL-sync events, about 10.3 seconds append and 1.5 seconds cold replay,
  preserving restart state and loss latch. It does not qualify the separate
  cumulative observation ledger/calculator or a full broker-day workload.
- Two clean-checkout builds were byte-identical: archive SHA-256
  `09d2096fc1c294f6507f378c776655e1a90cb159e5228ac8f5b63015a00e5ae0`,
  release ID
  `bbb9a18e895012b9c2018a934c1a49faa52de50bb57f961854b6ecbcf6602333`.
  They retained the legacy selected profile and were not installed. A new
  publication commit can legitimately produce different build identifiers.

Private broker observations, diagnostic databases, credentials and live state
are intentionally not supplied with this handoff. Do not recreate them from
the hashes, turn historical prose into fresh receipts, adopt diagnostic
baselines into production, or erase the preserved earlier failure incidents.

### Packaging-time recheck

The ordinary offline suite was rerun for this documentation-only handoff:
1,698 tests in 85.826 seconds, `OK (skipped=3)`. The three skips were the
optional SDK wire test and two SDK-data-class tests whose dependencies were
not available to that interpreter. Both SDK test modules were then rerun using
the installed pinned SDK environment: **50 tests plus 1 wire test passed with
no skips**. Repository validation and whitespace checks passed. No executable
source or owner-approval file was changed for packaging.

A bounded publication audit reviewed the implementation's 57 ancestor commits
and 687 unique text blobs. It found no real credential/private-key signatures,
tracked runtime databases, raw broker reports or SDK bundles; apparent secret
literals were synthetic test fixtures. This is not a proof against every
possible secret. The repository remains private because historical material
contains masked operational metadata, paths and contact information. New
ignore rules help keep future local credentials/state out of version control.

## Remaining work: complete these seven areas in dependency order

### 1. Account visibility, continuity and manual intervention

Finish continuous, account-scoped acquisition and cumulative execution/fee
history with explicit effective client scope. Confirm the reporting setting
persisted when a controlled restart is appropriate. Implement blind-interval
recovery and safe manual takeover/resume, not just entry pause.

Acceptance: demonstrated coverage of the permitted API and manual activity;
duplicate/corrected/late callbacks reconcile exactly once; disconnect,
reconnect, process crash and restart retain history and incidents; unexplained
gaps cannot authorize entries; external/manual changes invalidate stale plans;
handoff/resume preserves risk state and permits only one effective order
controller without overlapping exits. A finite request-end marker is not
exhaustive history. Start with offline fixtures and a safe non-live integration
environment; no live order is an acceptance shortcut.

### 2. Broker subscription lifecycle integration

Carry the repaired new reader into the continuous production acquisition
design and qualify its lifecycle. Do not reopen the already resolved bounded
third-read failure as if no repair exists, or claim legacy paths are repaired.

Acceptance: sustained and reconnect/failure-injected request/cancel tests show
bounded ownership and no accumulation; wrong request/account/model, missing
end and uncertain cleanup remain closed; protocol and real read-only tests
agree. Preserve all failed diagnostic states. No arbitrary sleeps, swallowed
provider errors or stale-value substitution to pass a rehearsal.

### 3. Accounting and current market marks

Connect authentic account adjustments and spendable-cash evidence to the new
reconciler. Supply fresh, correctly scoped bid marks for residual permitted
inventory and current bid-to-stop downside; do not reuse original entry-risk
numbers as current open risk.

Acceptance: deposits/withdrawals, interest/dividends, fees and corrected fills
do not leak into the wrong numerator or double count; unknown residuals block
new risk; stale/wrong-currency/wrong-contract prices fail; replay produces the
same P&L and capacity; account cash and obligations bound spending independently
of P&L. Finite matching cash observations do not establish complete adjustment
coverage.

### 4. Account-effective fees and protective-order evidence

Establish current account-effective all-in fees and remaining lifecycle fee
reserves, then connect strict protective-stop evidence to production exposure
and closeout logic. A recorded pricing-plan label is not a full fee bound.

Acceptance: real fee sources are versioned/scoped; incurred fees and future
obligations are distinct; fills, partial fills, corrections and refunds reconcile;
only qualified accepted stops offset supported exposure; held, rejected,
conditional, conflicting, stale or queued orders do not; cancellation and
replacement cannot cause overlapping sales or an unreserved protection gap.

### 5. Complete production session-risk integration

Add the model-specific source verifier, authenticated measurement boundary and
state/evidence path across service, sizing, preflight, final dispatch, recovery,
loss closeout and activation. Only then select the new model through reviewed
configuration and migration. Preserve legacy paths and their unchanged gates.

Acceptance: every consumer receives the same account/day/policy-bound fixed
baseline and monotone state; a stale or changed measurement cannot dispatch;
all existing/pending/unresolved downside and remaining costs are reserved;
loss-trigger closeout and restarts preserve the latch; no hidden legacy-field
fallback or skipped prerequisite exists. The explicit
`SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE` block is removed only after
its actual integration requirements pass, never by deleting the check alone.

### 6. Discovery and supported execution composition

Configure the existing discovery/provider composition using verified current
bindings, entitlements, contract evidence and fresh candidate/market data. Wire
the supported command path to the completed risk contract, preserving broker
precautions, one-writer ownership and durable-intent-before-submit behavior.

Acceptance: qualified candidates produce deterministic, expiring whole-share
plans; no candidate means no trade; stale/missing evidence blocks; unknown
submission is reconciled without blind retry; protection, cancellation and
closeout remain independent of discovery failures; staged transport cannot be
mistaken for supported autonomous execution. Do not manufacture candidates or
relax freshness. Paid services or new broker permissions require a specific
owner decision if actually needed.

### 7. Deployment, failure/recovery qualification and owner acceptance

Complete end-to-end fault and realistic capacity tests for the entire ledger,
calculator and lifecycle. Produce a reviewed immutable release and an explicit
state-preserving migration, then install **paused** on the intended host.

Acceptance: crash windows, stale writers, reconnect gaps, late fees, manual
intervention, notification failure, market close and restart are covered;
baseline/history/incident/loss state survives; full-day volume and latency are
qualified; clean builds are reproducible; installed source/config hashes match;
single-writer ownership, actual notification delivery and current broker/data
readiness are proven against that release. Owner activation remains separate.
Do not erase existing state to make migration or readiness succeed.

## Source map

| Concern | Starting points |
| --- | --- |
| Policy and approvals | `config/risk_limits_ibkr_session_trading.json`, `config/full_live_ibkr.json`, `live/policy.py`, `live/session_trading_policy.py`, dated owner amendments under `validation/full-live/` |
| Broker capture and scope | `live/broker/ibkr_read.py`, `ibkr_runtime.py`, `ibkr_session_inputs.py`; corresponding `tests/test_live_ibkr_*` |
| Durable observations and risk | `live/session_observation_ledger.py`, `session_trading_store.py`, `session_trading_calculation.py` |
| Accounting and bounded diagnostics | `live/session_accounting_reconciliation.py`, `session_trading_rehearsal.py`, `session_input_probe.py`, `scripts/titan-session-inputs-probe` |
| Sizing and session consumers | `live/pipeline.py`, `risk_runtime.py`, `service.py`, `ibkr_autonomous_inputs.py`, `ibkr_autonomous_plans.py`, `ibkr_autonomous_authority.py` |
| Protection and lifecycle | `live/broker/ibkr_protection_evidence.py`, `live/protection.py`, `execution.py`, `lifecycle_actions.py`, `state.py` |
| Market data and discovery | `live/massive_adapter.py`, `live/ibkr_instrument_provider.py`, discovery composition and pipeline tests |
| Deployment and authority | `live/activation.py`, `writer_lock.py`, `release.py`, `cli.py`, `scripts/build_full_live_release.py`, `scripts/install_full_live_paused.py` |

Paths beginning `live/` are relative to `src/titan_brain/`. Read
[`README.md`](README.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the relevant
implementation/tests before editing. Some older prose describes earlier model
or deployment states; use the dated precedence above, not historical readiness
language, to determine this handoff's scope.

## Portable offline setup and checks

Python **3.11 or newer** and Git are required. The application declares no
general Python dependencies; the ordinary suite uses the standard library.
No brokerage account, token, paid service, Gateway session or network-enabled
trading is required for these commands. Run from a clean repository checkout:

```sh
python3 --version
git status --short
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests
python3 -I -S -B scripts/validate_repository.py
git diff --check
```

The optional test in `tests/test_live_ibkr_account_updates_sdk.py` skips unless
`TITAN_TEST_IBKR_SDK_ROOT` identifies a separately obtained, suitable SDK
`site-packages` directory. It checks official **ibapi 10.50.2**, protobuf request
encoding/decoding for protocol 223, and forbids sockets. The pinned production
profile also specifies protobuf **5.29.5**. Do not substitute an arbitrary
package/version or represent a skipped test as the historical SDK-inclusive
pass. To include it after independently preparing the required SDK:

```sh
TITAN_TEST_IBKR_SDK_ROOT='/absolute/path/to/verified/sdk/site-packages' \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -B -m unittest discover -s tests
```

The example path is a placeholder, not a supplied SDK or attestation. Both
SDK and required protobuf imports must be available to the isolated test
subprocess. Tests use synthetic broker data; do not replace them with real
account identifiers or secrets.

Two tests in `tests/test_live_ibkr_sdk.py` also skip when the executing Python
environment cannot import the official SDK data classes. Inspect the reported
skip reasons and include the SDK test modules in the prepared SDK environment;
do not report an SDK-free run as if those checks executed.

For an offline release build **after committing reviewed changes**:

```sh
titan_package_dir="$(mktemp -d)"
python3 -I -S -B scripts/build_full_live_release.py \
  --config config/full_live_ibkr.json \
  --output-dir "$titan_package_dir"
```

The builder requires exact clean Git provenance, rejects untracked files
(including ignored artifacts), and requires output outside the source tree.
Keep virtual environments, bytecode, SDKs, runtime databases and build outputs
outside the checkout. Prefer a fresh clean worktree for reproducibility checks.
This build is not installation or activation and does not select the new
session model automatically.

## Portability, security and handoff boundaries

- Core offline tests are portable Python; deployed credential custody and
  service launch are macOS Keychain/launchd integrations, with local Gateway
  and market-data bindings. A successful Linux/Windows test run does not make
  those host integrations portable.
- Publication transfers source and reviewed documents only. It does **not**
  migrate Keychain items, private signing/control keys, broker sessions,
  full production account identifiers, OAuth tokens, market-data credentials, local SQLite
  state, activation artifacts or delivery consent. Do not ask the owner to
  paste secrets into GitHub issues, prompts or committed files.
- The checked-in profile contains host/account-specific masked metadata and
  binding hashes; it is not a generic ready-to-use profile for another host or
  account. Establish any replacement binding through the existing reviewed
  setup path, not by inventing matching receipt fields.
- Source hashes and stored hash chains establish bounded integrity claims,
  not current broker authority, complete history, notification delivery or
  protection against every rollback threat.
- Do not run live `doctor`, readiness, rehearsal or setup commands as if they
  were offline tests. Inspect each command's side effects first; some readiness
  paths can acquire ownership, reconcile production state or contact brokers.
- At each milestone, report changed files, tests actually run and their
  results, remaining evidence limitations and whether anything was deployed.
  Do not label the project live-ready until the release-specific acceptance
  conditions above have genuinely passed.
