# Fresh deployment report — remaining IBKR gates, September 14, 2026

Report compiled after the September 14 evening setup work (final installed
checks at September 15 00:34–00:35 UTC, September 14 20:34–20:35 ET).

**Executable changes are implemented, committed, tested and installed PAUSED.
Full-live autonomy is not running and is not activation-ready.**

This supersedes the earlier deployment-status snapshot, not the immutable
owner approval. It is new implementation/evidence, not another copy of the
September 8 handoff. No order was placed, modified or canceled; no precaution
bypass, paid subscription, email send, service start or activation occurred.

## Version and installation identity

| Item | Exact value |
|---|---|
| Tested local source | `c0f35cf248be39648a31433e99622eb20ea43a1a` |
| GitHub executable mirror | [`455e1ceb4c01c72e02d8ce8a8f52d2004632816d`](https://github.com/Harpcity33/trader-brain/commit/455e1ceb4c01c72e02d8ce8a8f52d2004632816d) |
| Identical source tree | `a6db7ab779d4702f035cbafd8855ccd60592cbd7` |
| Installed release ID | `77be61e3872ab650c84522225bb5c0db1b3d932aa32c6a308e756f2ed671f7a2` |
| Archive SHA-256 | `38e46d8eb767cf6a74468dc72c9a83cc39778b0cddfce3f73a3f771d94b0ea8e` |
| Manifest file SHA-256 | `1af1e8cf70b117a7289e833356eda7497e1e90a41fc3ef21b01601b4ae6025d9` |
| Installed at | `2026-09-15T00:34:05.955878Z` |
| Install root | `/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103` |
| Broker runtime | Official `ibapi 10.50.2`, `protobuf 5.29.5`, release-pinned 303-file SDK snapshot |
| Pinned interpreter | Python `3.12.14` |
| Runtime | `PAUSED`, authority `0`, generation `4`, activation `null`, no writer lease |
| Trading coordinator / independent notification worker | Both launchd services absent: `launchctl print` exit `113` |

Local and GitHub commit IDs differ because their retained prior histories
differ. Their source **trees are identical**; no reset or force update is
needed. The later report-only commit does not change the tested executable
tree or installed source. The local release archive is installed locally;
the archive itself is not a GitHub release asset.

Use the pinned Python interpreter to invoke the installed launcher, as in
OPERATIONS. A bare shebang invocation resolves this Mac's older default
Python and refuses to start (exit 70); it is not the pinned launchd runtime.
The correctly invoked installed diagnostics below were exercised.

## New executable work in this follow-through

Local commits:
`282e05c` (approved policy and diagnostic gates),
`d9a296b` (durable final risk observations),
`c0f35cf` (healthy service-wiring test fixture).

- Applied the already-approved 25 bps, 5x share-depth, ranking-only/no-A+,
  sequential verified stop, completed-minute first-target full-exit,
  cancel-first, session and fee-reserve decisions to the IBKR policy.
  Build/install now preserve and hash-check the exact immutable proposal and
  approval bytes. Conditional authority/transport/interlock selection remains
  staged; none was made true by policy approval.
- Added an exact approved dollar-headroom risk contract and evaluator:
  fresh realized P&L plus $100 before the goal crossing, realized P&L minus
  $125 after the latched $150 crossing, and an irreversible account-day
  -$100 entry lock. All open, pending, uncovered and unresolved downside,
  candidate reservation, conservative contingent fees and positive execution
  reserve are included. No unapproved percentage overlay was imported.
- Final command preflight independently recomputes headroom from its fresh
  broker snapshot and the durable exposure ledger. It accounts for the exact
  candidate once and retains the conservative contingent fee count.
- Added writer-owned, durable risk-observation incident markers before
  autonomous broker reads. Current-day authenticated loss/goal crossings are
  latched even when later instrument, scope or session checks reject the
  order. Failed reads/writes leave entries blocked across restart; later
  recovered P&L cannot erase an unresolved prior observation. Readiness and
  service entry checks use the same account-wide incidents across releases
  and dates. Safety actions are not blocked solely by the entry-risk latch.
- Receipt timestamps and session checks now refresh after broker/contract
  I/O, rather than backdating fresh receipts or widening future-time tolerance.
- Added safe read-client-zero support, strict reader/command/attended-client
  collision checks, and guards against manual-order-binding API calls.
  **Client zero/Master zero was not selected or tested against that Gateway
  configuration**; current IDs remain read 19735, command 19736.
- Improved the Read-Only API diagnostic to require both error 321 and the
  canonical cause, including the actual dot-delimited prefix. Generic 321
  remains generic; raw broker error text is not persisted.
- Split metadata-only contract read receipts from executable tradability
  evidence. A successful after-hours contract lookup no longer falsely
  implies regular-session eligibility.
- Added independent Gmail setup diagnostics that require neither SDK nor
  broker/Massive/network access. They inspect only the five named,
  account-scoped Keychain items' metadata, not secret contents, and report the
  intended Gmail route separately from the still-selected local staging sink.
  IBKR credentials are isolated from the older Robinhood account namespace.

Previously implemented stream/backfill separation, completed-minute causal
processing, protection/closeout state machines, exact-reference recovery,
durable outbox and guarded activation remain in the combined codebase and
tests. They are **carried forward**, not represented as newly authored here.

A bounded failure limitation remains: if durable storage is wholly unavailable
before an observation marker can be created, entry reads are stopped.
Safety reads may continue with an in-memory entry lock, but no crash-safe
persistence can be claimed for an observation that storage never accepted.
Operator recovery and fresh reconciliation are required; this is not a
guaranteed-loss or guaranteed-flatness system.

## Combined verification

[Machine-readable test results](TEST_RESULTS_REMAINING_GATES_2026-09-14.json)

- Bundled Python: **1,088 tests**, 59.267 seconds, PASS; two SDK-dependent
  skips.
- Official SDK environment: **1,088 tests**, 59.215 seconds, PASS; zero skips.
- Repository validator: PASS; 160 Python files, 26 JSON files, four TOML
  files, 7,388,430 checked bytes at the tested source.
- Whitespace check: PASS.
- Two independent clean builds: byte-identical archives.
- New regression coverage includes final-snapshot headroom, candidate/fee
  accounting, transient loss/goal crossing, restart, denial after observation,
  advancing receipt clocks, absent hooks/interlocks, persistence failures,
  global readiness incidents, read-client-zero non-binding, and independent
  notification setup without broker dependencies.

An intermediate full run caught an outdated service-wiring mock that did not
return a valid incident count. The fixture was corrected, not the production
fail-closed check; both final full suites above were rerun against c0f35cf.
Tests do not constitute live-money protection, exit, closeout or unattended
broker-contract demonstrations.

## Real connections and what each proves

Complete fresh outputs:
[installed gate checks](INSTALLED_GATE_CHECKS_REMAINING_GATES_2026-09-14.json).
Historical setup observations:
[broker evidence](BROKER_GATE_EVIDENCE_2026-09-14.md).

| Connection/check | Latest final-release result | Boundary |
|---|---|---|
| Massive REST | Authenticated at 00:34:31.884327 UTC | Market-status read; not continuous fresh tick/depth proof |
| Massive WSS | Authenticated at 00:34:32.031805 UTC | Handshake; not a running subscribed trading feed |
| IB Gateway loopback 127.0.0.1:4001 | Account ending 3103 authenticated at 00:34:32.190139 UTC | Managed-account discovery, not order authority |
| IBKR account callback collection | BLOCKED: `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_DAILY_REALIZED_PNL_TIMEOUT` | Missing current P&L is not zero; no fresh whole-account flatness claim |
| Separate IBKR SPY contract metadata | CONNECTED at 00:34:49.269332 UTC; receipt `80e52ec7243b7f6d143a35601421d93c3fac82afddbc953b60b6406cd8902b00` | Metadata only; regular session closed; no trading eligibility |
| Authenticated IBKR Portal | No Borrow Margin, IBKR Pro/Stocks Tiered, fee-waived non-consolidated streaming subscription observed | Portal values delayed; no API certification/entitlement inferred |
| Gmail daemon route | NOT CONFIGURED; all five named Keychain items missing | Connected Gmail read access is not durable daemon send authorization |
| `doctor` | Exit 2, detailed blockers retained | Not activation-ready |
| `readiness` | Exit 2, `COMMAND_FAILED:BrokerFactoryError` | Staged broker composition prevented a readiness attestation |

The owner-approved **Read-Only API** checkbox change was applied. Its
pre-change canonical error disappeared and a subsequent source account-read
collection completed once. Later source and installed reads timed out on
daily realized P&L, so the earlier success is not used as current evidence.
No other Gateway setting was intentionally changed. A fresh post-Apply visual
check of **Bypass Order Precautions for API Orders** is still outstanding
because the native settings surface became unavailable; successful reads
do not establish its state.

## Remaining gates: ownership and exact evidence

Already-approved financial/operational choices and Read-Only API permission
are **not** being requested again. Existing dollar limits, no borrowing,
long whole-share equities only, no reentry/ADD/options/shorting, no premarket
or after-hours orders and no overnight exposure remain unchanged.

| Remaining gate | What is actually required |
|---|---|
| API legal acknowledgement/subscriber classification | Owner personally reviews/submits the API supplement and accurate classification if accepted. The agent did not sign. No new paid subscription is authorized. Then verify actual API entitlement; fee-waived TWS display access is not sufficient. |
| No-bypass unattended broker contract | Human IBKR confirmation for the exact external-data regular-hours place/cancel workflow without manual Transmit or precaution bypass, plus fresh observed unchanged precautions and actual endpoint behavior. Existing AI-generated support reply recommended bypass; that was rejected, not implemented. |
| No Borrow Margin capacity | Exact authenticated API tags/semantics for available unleveraged cash and same-day proceeds. Do not treat generic buying power as approved spend capacity. |
| Reliable current-day P&L and risk evidence | Resolve the observed callback timeout, then bind current broker P&L to an authenticated prior week-to-date baseline and durable equity high-water evidence. Neither zero P&L nor a signed baseline was manufactured. |
| Live drawdown review rule | The approved proposal retained an “existing” rule but no approved threshold/semantics was recovered. Dollar mode deliberately retains `LIVE_DRAWDOWN_REVIEW_POLICY_UNVERIFIED`; the staged 20% rule is not approval. The exact rule must be recovered/approved and implemented/validated before release readiness. |
| Exhaustive cross-client coverage | Decide/authorize the exact reader/Master configuration; current Master ID was blank. The client-zero code is built, but changing Master/read IDs requires separate permission and fresh non-binding/coverage tests. Current-day completed orders and absent historical references are not exhaustive history. |
| Effective fees | Prove the exact Tiered all-in upper bound for allowed sizes/routes or obtain approval for a higher reserve. The verified plan name alone does not prove the approved $1/order floor conservative. |
| Independent notifications | Exact owner-approved destination and test consent; local durable `gmail.send` OAuth; bound sender/destination/route; independent worker startup and one visibly received route-bound test. No email was sent. |
| Evidence provisioning and production wiring | Real release/account/provider/risk/control/route receipts and their authorized signing/key lifecycle; then select the supported provider composition in an evidence-backed rebuild. Receipts are local application controls, not custom IBKR-issued certificates. Staged booleans must not be flipped to simulate these facts. |
| Live protection/closeout and cutover | Complete broker-backed reconciliation/recovery/protection/closeout verification under the supported contract and separate authorized activation. Unit tests and local zero-position counts are not live broker proof. |

A human-support follow-up is available in
[the unsent draft](IBKR_HUMAN_SUPPORT_FOLLOWUP_DRAFT.md).
No reply was sent in this turn.

### Unfinished engineering and integration, not merely approvals

- **Dollar-mode live drawdown:** `policy.py` deliberately blocks every dollar
  policy, and `risk_runtime.py` evaluates the existing percentage/dollar
  drawdown fields only in legacy percentage mode. Once the actual retained
  trigger and response are recovered/approved, its dollar-mode evaluator and
  verifiable provenance path still need implementation and boundary tests.
  Removing the unconditional blocker alone would not implement that rule.
- **Trusted issuance/refresh:** the baseline and scheduler modules consume
  authenticated external receipts; they do not fetch/sign/rotate them. An
  owner-authorized trusted source and issuance/refresh integration still need
  provisioning and connection. The repo does not ship a CLI that magically
  issues these provider facts.
- **Orphan observation recovery:** a persisted
  `IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED` marker survives a crash. Only the
  originating in-memory observation token can complete the ordinary commit
  path. There is no shipped owner-recovery CLI for an orphan. A separately
  reviewed, evidence-backed recovery procedure/tool remains necessary;
  deleting the incident or using a later recovered P&L is not recovery proof.
- **Production composition:** supported broker/discovery/Gmail code exists,
  but the final authenticated configuration and receipt bindings still need
  a reviewed rebuild and end-to-end verification. They are not installed as
  selected live providers in this release.

The active IBKR desk's saved ID was checked against the scheduler consumer:
`robinhood-titan-premarket-deep-dive` is included alongside
`robinhood-momentum-engine`. This resolves ID mapping only, not live scheduler
retirement or absence of running executions. Local TOML is not a signed
control-plane runtime receipt.

## Built vs installed vs running

- **Built:** full-autonomy-targeted engine with the changes above and
  fail-closed production boundaries; deterministic local archive. The
  explicitly listed engineering/integration gates are still unfinished.
- **Installed:** final tested c0f35cf release, SDK snapshot, state bindings and
  disabled service definitions; PAUSED and authority 0 after all probes.
  Previous releases are retained.
- **Authenticated:** only the successful bounded connections explicitly
  listed above. No authenticated unattended command lane or live order proof.
- **Running:** no full-autonomous coordinator or notification worker. The
  separate Codex task “IBKR Titan — Premarket Analysis + Live Desk” remains
  ACTIVE on the requested premarket-30-minute/open-one-minute schedule, but
  its saved prompt is still attended compatibility, not the autonomous
  service. The original `robinhood-momentum-engine` is PAUSED. No scheduler
  was changed in this turn.

## User-controlled activation procedure — unavailable on this staged release

Use [OPERATIONS](../2026-09-08/OPERATIONS.md), not configuration/database
edits or copied readiness output.

1. Resolve the gates above and rebuild/install a clean PAUSED, fully
   evidence-bound release. Check fresh broker exposure and working
   protection; arrange a safe rollback window.
2. With explicit owner authority, start only the independent notification
   worker first, enqueue its test and verify visible delivery of the exact
   route-bound event.
3. Retire competing same-account writers/legacy automations and obtain fresh
   signed control-plane evidence of zero running executions. Preserve
   broker-held protection and prove the account lock is exclusive.
4. Run installed `readiness`; it must generate current passing evidence.
   Run `prepare-activation --ttl-seconds 300` and review its full exact
   short-lived record and confirmation phrase. Expired/changed facts require
   a new record, even within the five-minute outer TTL.
5. Owner separately invokes `activate --activation-id EXACT_ID --confirm
   "ACTIVATE FULL LIVE ibkr-live-ending-3103 EXACT_ID"`. This only permits
   `PAUSED -> RECONCILING`, not an order or direct `ACTIVE` state.
6. Only after successful activation, explicitly start the coordinator,
   confirm one writer and healthy independent notifications, and wait for
   the service's strictly newer clean broker reconciliation to permit
   `ACTIVE`.

No activation record was prepared or consumed here. Full autonomy remains the
objective; an attended pilot is not being substituted as completion.
