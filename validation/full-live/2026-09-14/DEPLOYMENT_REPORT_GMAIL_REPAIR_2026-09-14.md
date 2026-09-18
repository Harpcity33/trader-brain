# Gmail repair and actual deployment — September 14, 2026 ET

## Outcome

The partial Gmail enrollment is repaired. The newly installed, isolated
notification worker made one real send, obtained a durable Gmail acceptance
receipt, and is now running under launchd. Autonomous trading is **not ready
for September 15**: the trading coordinator is not loaded, runtime authority
is zero, and the account-scoped installation remains PAUSED. No order was
submitted, cancelled, replaced, or otherwise mutated during this work.

General authorization was not treated as evidence of email receipt, a broker
capability, an authentic account balance/flow feed, or a waiver of precautions.

## New executable work

- `4ed6ad175b92ce28926d3fab943f5ffc9889ab92`: exact matching-client-only
  enrollment recovery, native interactive Keychain readback, and callback URL
  cleanup. The existing client was preserved; only the four missing entries
  were created after a fresh grant. No consumed code was replayed.
- `cec218e4e495f997c1dcf83f10ca3895ee9d55f8`: read-only native runtime custody
  restricted to the five exact IBKR Gmail locators. It requests failure
  instead of authentication UI per query; no ACL, global interaction policy,
  legacy account custody, or scope was widened.
- `1daf03371f5c07a69a71feddf799c47fd4dfbb20`: actual authenticated Gmail route
  binding; explicit, atomic owner-delivery acknowledgement; unique visible
  TEST token and event ID; exact release/account/config/policy/route binding;
  and readiness validation of the separate immutable acknowledgement event.
  Provider acceptance, forged owner assurance, old-release tests and local
  staging cannot satisfy owner-confirmed readiness.

The source notification route now uses Gmail, a ten-second request timeout,
and `OWNER_CONFIRMED` assurance. The financial policy, execution permissions,
precaution requirements and live-entry interlock were not relaxed. Broker-only
test fixtures explicitly model their disabled notification state rather than
depending on an outdated production configuration.

## Combined test and build evidence

- Official installed SDK: **1,206 tests passed in 62.037 seconds**, no skips.
- Bundled Python without SDK: **1,206 tests in 61.860 seconds**, passed with
  two explicitly SDK-dependent tests skipped.
- Independent review found and closed two readiness issues: missing separate
  owner-acknowledgement audit validation and indistinguishable test messages.
  The final bounded review found no blocking issues in those fixes.
- `git diff --check` passed.
- Two builds from a clean detached worktree at `1daf03371f5c07a69a71feddf799c47fd4dfbb20`
  produced identical archives.
- Archive SHA-256:
  `854479975034ca8067fa09ec0932f9bdf44a4aa3699085a177af7763e8bf9dd5`.
- Release ID:
  `73ab28f9330b4622eb582b3e3ea5ba1593a476498f1ca27b4ec11018eb7427c1`.
- Manifest file SHA-256:
  `7dbaa03ff18664ce662deb8cef99d81924978befb4a16206d62212d806304fcd`.

## Installed versus running

Installed at `2026-09-15T02:57:39.431706+00:00` (September 14 ET) under
`/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`.
The installer verified 106 release files and the existing 303-file SDK
snapshot, rebound the PAUSED identity to generation 9, and did not contact
IBKR or start services. Previous releases and state remain retained.

- Installed source: `1daf03371f5c07a69a71feddf799c47fd4dfbb20`.
- Interpreter: pinned CPython 3.12.14 with `-I -S -B`.
- Reused SDK: `ibapi` 10.50.2, protobuf 5.29.5; inventory SHA-256
  `3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc`.
- Trading runtime: PAUSED, authority 0, no activation record.
- Trading launchd service: absent (`launchctl print` exit 113).
- Notification launchd service:
  `com.harpcity.trader-brain-full-live-ibkr-3103-notifications`, actually
  running, PID 61264, started `2026-09-15T02:58:36.559262+00:00`.
- Notification lease: exact route, unreleased, generation 2. Observed heartbeat
  `2026-09-15T02:58:53.036945+00:00`, zero cycle failures; stderr file zero bytes.
- The outbox contained only the one delivered TEST, no pending work, before
  starting launchd. No other queued message was released by this setup.

Final local checks at approximately `2026-09-15T03:01:05+00:00` again verified
release integrity and runtime bindings, PAUSED generation 9, authority 0,
no activation and no trading writer lease. PID 61264 remained alive, its
unreleased notification lease had heartbeat `03:01:05.223603+00:00` and zero
failures. The outbox still contained exactly one DELIVERED/PROVIDER_ACCEPTED
TEST and no pending row. These local counts do not establish broker flatness.

The worker is a local logged-in-user LaunchAgent, not an always-on cloud
service. Sleep, logout, loss of networking, locked/unavailable Keychain,
revocation or Google limits can prevent delivery. Generic email retry is not
an exactly-once transport guarantee. No additional service spending occurred.

## Real notification connection and send

Enrollment exited with `KEYCHAIN_ENROLLMENT_COMPLETE`, all five values
verified, exact `gmail.send` scope and successful real refresh. A separately
bounded native background-reader probe also succeeded, without an observed
prompt, at `2026-09-15T02:45:33.717986+00:00` in about 0.16 seconds.

The installed notification-only command enqueued the TEST; the installed
worker was then invoked **once**, with batch limit one. It returned `sent: 1`,
`failed: 0`, exit zero. The response was reconciled before enabling its
supervised service, so an unknown setup send was not retried.

- Logical test: `gmail-3103-2026-09-14-live-route-test-v1`.
- Durable message: `ca14af5c-079e-51b3-9347-f28d79e07335`.
- Visible verification token: `D9D1CBDA0A7630CC`.
- Gmail message ID: `1a0a2ffd66be4573`.
- Provider accepted at `2026-09-15T02:57:50.988904+00:00`.
- Route: `5b6e68b621e5a3b99f601a4412137e5534864577c0f60514ae476cad41033d9f`.
- Provider receipt hash:
  `b0ab6d6cd1a6fd3f5e9494034663c9269baa48d562040be0b695b6b4af14efd1`.
- Assurance currently **PROVIDER_ACCEPTED**, not OWNER_CONFIRMED. The exact
  owner acknowledgement was requested separately after this actual send.

The owner can inspect the current acknowledgement challenge using the
installed `notification-confirm-receipt` command and that message ID, as
documented in OPERATIONS. Only actual receipt of the matching code permits
the exact acknowledgement. The existing five-minute freshness gate remains;
an expired test must not be made fresh by changing its timestamp.

## Fresh real provider checks and unresolved trading gates

The post-install network probe began `2026-09-15T02:58:54.486284+00:00`.
It performed no broker mutations and sent no additional notification.

| Connection | Actual latest result |
|---|---|
| Massive REST | Authenticated market-status read succeeded at 02:58:54.639961 UTC |
| Massive WSS | Authenticated handshake succeeded at 02:58:54.794508 UTC; this alone is not continuous live ingestion proof |
| IB Gateway `127.0.0.1:4001` | Exact managed-account discovery authenticated at 02:58:54.966725 UTC |
| IBKR bounded account collection | BLOCKED: `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_DAILY_REALIZED_PNL_TIMEOUT` |
| IBKR contract-details probe | Not reached in the latest probe because account collection failed |
| Gmail | Real OAuth refresh succeeded at 02:59:05.128791 UTC; the separate send evidence is above |

Earlier in this same work, bounded account/P&L and contract reads succeeded
at 02:36:04 UTC. The newer timeout supersedes that success for present
readiness. Missing P&L was not replaced with zero, and neither local empty
state nor the older snapshot is being reported as current broker flatness.

Actual installed `readiness` returned exit 2, `COMMAND_FAILED:BrokerFactoryError`:
the installed policy still selects the staged broker graph, not an armed,
supported production transport. This is not a successful activation dry run.

Remaining work is substantive, not a list of consent booleans:

1. Resolve the intermittent bounded account-read failure and prove all-client,
   all-order-family coverage plus historical exact-reference recovery. The
   current adapter deliberately does not equate current-day bounded callbacks
   with exhaustive account history.
2. Establish a genuine daily-starting whole-account equity/external-flow
   source and implement its supported timing contract. Midnight and a
   synchronized five-second valuation/flow watermark are current implementation
   assumptions, not separately approved owner words or IBKR guarantees. See
   `DAILY_PROVIDER_CONTRACT_REVIEW_2026-09-14.md`; signing invented values or
   widening freshness alone is not a solution.
3. Finish coherent open-position valuation, verified working-stop downside,
   pending/open fee reserves, and recovery evidence before adding risk.
4. Prove the account-specific no-borrow capacity, fee/entitlement and no-bypass
   programmatic transmission contract. A fresh authenticated Support view
   showed no messages in either Open Web Tickets or Closed Web Tickets;
   no resolving support answer was recovered or fabricated.
5. Select and configure the supported production graph, provision genuine
   release-bound authority/risk/scheduler evidence, and verify protection,
   unknown-outcome recovery and closeout end to end. Keys/signatures cannot
   establish missing provider facts. Then run readiness again before the
   separate user-controlled activation procedure in OPERATIONS.

The +15% daily aspiration and -10% account-day entry lock remain based on the
approved daily starting balance policy. Neither is a guaranteed outcome or a
guaranteed maximum realized loss. No premarket trading, borrowing, fractional
shares, shorting, options, averaging down or overnight exposure was authorized
by this repair. No trading activation was performed.

This report and the code commits are local; this work did not push to GitHub.
