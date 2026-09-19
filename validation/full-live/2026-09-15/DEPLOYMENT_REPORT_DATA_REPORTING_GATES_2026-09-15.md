# Data, reporting and broker-warning repairs — September 15, 2026

## Scope

This continues the owner's request to repair the IBKR desk while the trading
heartbeat stays paused. It is new executable work after the earlier
reconciliation repair, not a republication of that package. No order, broker
precaution, financial policy, paid service, or trading activation was changed.
Local state counts are not evidence of a flat broker account.

The +15% daily aspiration / irreversible −10% new-entry lock, fixed starting
whole-account balance basis, no borrowing/debit, whole-share long listed US
equities above $5, no premarket trades and no overnight exposure are retained.
The percentages are not guaranteed returns or guaranteed maximum losses.

## Newly implemented

1. **Authenticated collector health.** A dependency-free tracker ties health to
   the current authenticated socket and successfully processed, validated,
   advancing market messages. It writes on real message receipt at most once
   per five seconds. Status/ping traffic cannot fabricate freshness or clear a
   failed connection. Existing quote, bar, depth and entry gates are unchanged.
   An exact-version patch preserves the newer installed collector instead of
   overwriting it with the older workspace copy.

2. **Calendar-aware diagnostics.** The fallback doctor now passes the signed
   policy calendar to its data source. Premarket preparation, the open
   transition, holidays and closed hours no longer default to entry-eligible.
   The output separately identifies session and entry-evidence readiness.
   Regular-session stale evidence still blocks.

3. **Daily-risk schema wiring.** Unattended IBKR assembly now selects the schema
   required by the actual policy rather than hardcoding the obsolete schema.
   This corrects composition only. It does not create a starting balance,
   authenticate external flows, amend the policy or enable unattended authority.

4. **Executable Flex reporting setup.** `flex-setup-status`, `flex-enroll` and
   `flex-probe --date` now wire the existing reader into an installed CLI path
   independent of the TWS SDK. The exact account-scoped Keychain item is
   create-only. Token and Query ID use hidden owner-terminal prompts that fail
   before an echoed-input fallback. The full account ID is supplied privately
   at probe time and never persisted. One local lock serializes probes; one
   generation ticket is reused for at most three retrievals on provider 1019,
   with six-second pacing. Secrets, raw reports and linkable report hashes are
   not printed. Fixed output fields cannot be overridden into live risk
   authority. No real Flex enrollment, request or report is claimed.

5. **Notification evidence wording.** The credential probe no longer asserts
   that the earlier delivery test is missing. It explicitly says it does not
   inspect prior delivery/acknowledgement and directs verification to the
   durable outbox and current release-bound delivery gate. It does not clear
   that gate or resend the acknowledged message.

6. **Read-side broker warnings cannot prove protection.** The order reader now
   copies only bounded status/time fields and a warning-presence boolean rather
   than retaining the raw order-state object. A nonempty, missing, unreadable,
   malformed or oversized warning field downgrades otherwise working Submitted
   states to UNKNOWN. Only an explicit empty/whitespace warning string is clear.
   Independent broker cancellation and execution-proven completed fills remain
   terminal: a warning cannot hide a canceled stop that requires safe-close
   handling or turn proved fills back into pending orders.
   Duplicate callbacks cannot erase warning presence within one complete
   collection, including backwards receipt clocks. Presence changes affect the
   order-fact fingerprint; changes to private warning text alone do not. A new
   complete broker collection is required to establish a changed clear fact.
   Such UNKNOWN stops do not count as verified working protection or authorize
   an overlapping replacement. Unreadable completion times fail collection
   without leaking the provider text.

## Collector installed and exercised separately

The previous collector was stopped gracefully through its existing launchd
label; its old process was observed gone before patching. Exact recoverable
backup:

`/Users/harp/Library/Application Support/Titan Momentum/collector-health-backup.sTs9Cr/massive.py`

No helper previously existed. `apply_patch` applied only the reviewed target
and helper; the older canonical workspace collector remains untouched.

| Artifact | SHA-256 |
|---|---|
| Installed collector before | `0be1d3f0d6be20f92cac97a913636e8299ba122300dcbfe244e003f17791b2e3` |
| Installed collector after | `6d43ef4eb89efec5a89ec6cfbaa8edcf4dd8ecb89eac190d73eb99ebe234163b` |
| Standalone health helper | `0e787f84f0188ee1ffd015eca2f814f25e990216b6b2bc8a49de0e53abc3254f` |

Both modules compiled with the collector's existing Python. The existing
`com.titan.momentum-watcher` was restarted, not duplicated. Observed PID:
77363. No subscription configuration or credentials changed.

Real observations on September 15 (UTC):

- 11:58:44.813: WebSocket authenticated; health remained degraded awaiting data.
- 11:59:00.951379: first validated AM health pulse.
- 12:00:05.057297: validated Q pulse.
- 12:00:35.126416: later Q pulse, exceeding the old 15-second expiry interval.
- 12:00:37.476821: separate market-data freshness row still advancing.
- 12:00:10.002535: 38 promoted candidates with Q/A subscriptions.
- 12:00:56.267281: latest observed quote receipt, provider timestamp
  `1789473656243` milliseconds.
- 12:00:04.062316: latest observed completed-minute-bar receipt, provider end
  `1789473600000` milliseconds. Receipt and completed provider interval are
  distinct; neither is rewritten as the other.

Startup briefly lacked fresh quote subscriptions until candidate warmup. That
was not relabeled healthy entry evidence. These observations establish this
collector repair's actual use; they do not establish every symbol's acceptable
spread/depth or authorize a trade. Runtime rollback instructions and the exact
patch manifest are in `integrations/legacy_massive_health/v1/README.md`.

## Build evidence

Executable commits this turn: `c874a79` (collector, calendar, schema and Flex
CLI), `62d76f4` (immutable reporting-only fields), and `1ca93e6` (warning/protection
semantics and delivery-evidence wording).

| Field | Verified value |
|---|---|
| Exact executable source | `1ca93e6e3b668e3531e0acaf7fc0f323bb33502b` |
| Release ID | `1f6cc34ff5b1877572d07d31382f6d4326d7ec7543be4887c28dddb982ee9abf` |
| Archive SHA-256 | `7ab69f08f61802a8d82c2cce64dd051fe9f255f51c0a3fc6d6c17f392f211a05` |
| Manifest-file SHA-256 | `159ac90e8ac1867569674ad8b62820a6dea7a4845ed3566311f68fda661a2d04` |
| Unchanged policy hash | `f54292e461f5c27f721b9ffe939a267539af0358e8d43a994520ac94ee7e984e` |
| Unchanged config hash | `c756c6f75c895d14b6e3b4b3349c6655bbf50d1d39001ab385b233fc67c61cdd` |

Built twice from the same clean detached source; archives have identical
SHA-256 and `cmp` succeeded. Structural validator passed on 175 Python files,
33 JSON files and four TOML files. The source contains no new service purchase,
credential value or policy/activation flip. Test fixtures are synthetic and
not live broker certification.

Final combined suite on that exact detached commit: **1,297 tests passed in
61.657 seconds**, with no skips reported. This supersedes the preliminary
1,289-test run before the warning fix. Independently reviewed warning/account/
protection/runtime suites passed 96 tests; the root's combined focused check
including local assembly passed 121 tests.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/titan-gmail-recovery.MXOgVX/sdk-venv/bin/python -B -m unittest discover -s tests
```

## Final local installation and actual running state

Installed at `2026-09-15T12:06:47.197755+00:00` (08:06 ET) under:

`/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`

The source-verified installer checked all **110 release files**, copied the
existing SDK into its pinned release snapshot, and rebound paused runtime
identity to generation **11**. Python remains 3.12.14, ibapi 10.50.2 and protobuf
5.29.5; SDK inventory remains
`3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc`.
The prior release and state were preserved. The installer called neither the
broker nor launchctl. No risk ledger was fabricated or cleared.

The notification worker was separately stopped after confirming no pending
messages. PID 71488 exited and its lease was released at 12:06:34.495783 UTC
before installation. Its staged and existing LaunchAgent files compared equal;
the existing notification-only service was restored. New PID **79576**, lease
generation **4**, heartbeat 12:07:10.425545 UTC, no release/exit at observation.
The outbox remains one `DELIVERED / PROVIDER_ACCEPTED` test, with its original
delivery timestamp, and no pending message. No resend occurred.

| Component | Final distinction |
|---|---|
| Trading runtime | New code installed; PAUSED, authority 0, no activation stamp, no service writer lease |
| Trading coordinator | Not loaded; no autonomous order execution running |
| Trading heartbeats | All three relevant legacy/IBKR automation records remain PAUSED |
| Massive collector | Patched, authenticated and actually ingesting, PID 77363 |
| Independent notifications | Existing delivery service restored on new release, PID 79576 |
| Flex reporting CLI | Installed and metadata-smoke-tested; exact item MISSING; not enrolled/authenticated |
| Gateway/account | Not connected/authenticated; fresh positions, P&L and broker contract not reached |

Post-install `status` verified the exact source/release, valid runtime bindings,
PAUSED/authority 0, and no writer lease. `doctor` verified a valid audit chain
and returned `ready_for_owner_activation=false`, as required. Its data
diagnostic had no market-health blockers and correctly returned
`WAITING_FOR_SESSION`, `entry_evidence_ready=false` during premarket; this is
preparation-only, not entry permission. The internal legacy calendar lane name
`premarket_attended` does not enable premarket trades.

The doctor's static staged-profile labels still include
`IBKR_MARKET_DATA_API_ACKNOWLEDGEMENT_INCOMPLETE` and
`NOTIFICATION_DESTINATION_BRIDGE_UNPROVEN`. These do **not** establish that the
owner failed to submit the legal declarations or did not acknowledge the email.
The former needs authenticated account verification; the latter requires the
current release-bound delivery evidence to be validated. Their booleans were
not changed to manufacture readiness. Staged transport/discovery/interlock
settings likewise remain off pending genuine qualification.

Fresh post-install read-only connection check started at 12:07:10.135725 UTC:

| Real connection | Result |
|---|---|
| Massive REST | Authenticated market-status read succeeded 12:07:10.271487 UTC |
| Massive WSS probe | Authentication succeeded 12:07:10.413073 UTC; separate collector observations establish actual ingestion |
| Gmail | OAuth refresh succeeded 12:07:10.712048 UTC; no message sent |
| IBKR Gateway | `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_CONNECTION_NOT_ESTABLISHED`; account/P&L/contract reads not reached |
| Flex | Exact metadata lookup returned `IBKR_FLEX_SETUP_MISSING`; no secret or provider request |

The final provider probe reported empty broker-mutation and message-send lists.
These source and report commits are local; no GitHub push was made this turn.

## Remaining live gates

- **Gateway authentication / account read:** the last local check still found
  no listener at `127.0.0.1:4001`. The owner has been asked to sign into live
  Gateway without sharing credentials. The earlier P&L-timeout repair cannot
  be certified against this account until that session is available.
- **Exhaustive order and reference coverage:** current-open plus current-day
  completed coverage does not prove absence of every historical/unknown
  reference. Unknown submissions are never retried or classified absent from
  an empty result. New live evidence is required.
- **Authentic starting balance / external flows:** Flex query and reporting
  token still require owner setup. Historical NAV and cash transactions cannot
  supply a live complete-through external-flow watermark. Empty reports or
  owner promises not to transfer cannot be promoted to authenticated zero.
- **Remaining-position risk:** provider valuation times, a common whole-NLV
  epoch, no-borrow capacity and an all-in remaining-fee bound remain unproved.
  Diagnostic arithmetic is not authenticated risk authority.
- **No-bypass execution, protection and closeout:** supported account-specific
  transmission without manual Transmit, warning suppression or precaution
  bypass remains unresolved. Public API documentation, socket connectivity
  and WhatIf are not working-order or stop-protection proof. `PreSubmitted`
  remains queued rather than verified working protection.

## The one pending owner measurement choice

The existing implementation assumed midnight NAV and an aligned five-second
whole-account flow watermark; those assumptions were not separately approved
by the owner's phrase “daily starting balance.” The proposed amendment remains
**preceding completed broker daily accounting-period ending NAV**, frozen
before entries with its actual provider period boundary, while maintaining
the +15% aspiration, irreversible −10% lock and all instrument/exposure rules.
External deposits, withdrawals and asset transfers must be authentically
reconciled and must not count as performance or reset a lock. Missing evidence
blocks entries. “Let's proceed” was not treated as approval of this boundary.
Approving the boundary alone cannot solve the live-flow completeness gap.

## User-controlled continuation

1. Sign into the live IB Gateway endpoint and complete its authentication.
   Repeat read-only broker reconciliation, P&L, coverage and reference checks.
2. Follow `IBKR_FLEX_DAILY_EVIDENCE_SETUP_2026-09-15.md` for the existing Portal
   Activity query/token. Run the installed reporting CLI with `--install-root`
   from a private owner terminal; never put secrets in chat or shell arguments.
3. Resolve the measurement choice separately from factual provider evidence.
   Establish the supported no-bypass broker contract and real position-risk
   sources. No boolean or confirmation bypass can substitute.
4. Only after all release-bound readiness checks genuinely pass may the owner
   use the supported activation procedure. Until then, no activation command
   or trading-heartbeat resume is appropriate. Recheck the ET/DST schedule
   before eventual resumption: premarket analysis every 30 minutes, regular
   hours heartbeat every minute, and no premarket trading.

The existing independent notification test is acknowledged and remains
`DELIVERED / PROVIDER_ACCEPTED`. No new test or trading alert was sent here.
The trading heartbeat remains PAUSED. Data collection and notification-worker
operation must not be described as autonomous trading.
