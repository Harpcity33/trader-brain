# Full-live connection and hot-path deployment status

Recorded: 2026-09-08 08:51 UTC
Account binding: ending 7153
Reviewed base: `f7d0accaf38d4a6cd93a22cc2cd3229bc728bdfa`

This report covers only the executable work completed after the reviewed base.
It does not relabel the earlier package as new work and does not claim that a
PAUSED installation is live autonomous trading.

## Git and release identity

- Local tested implementation commit:
  `41754dc4c3e95407ef062e7b2a62398276df1e06`.
- Published implementation commit:
  `c3336f1e8b02bfc6f521cc23a32cd3918c95a7cb`.
- Published branch: `codex/full-live-2026-09-08`.
- The published commit has exactly one parent, the reviewed base `f7d0acc`, and
  is one commit ahead and zero commits behind it.
- Both implementation commits have the identical Git source tree
  `fcefe173710bb201c260de6b72c5a728a54b35bc`. The distinct commit IDs are only
  a consequence of the local checkout lacking native GitHub credentials; the
  authenticated GitHub integration created the canonical commit directly on
  the reviewed remote parent.
- Installed release ID:
  `45f83b40a1db48b26df383f9141e618825d8ee52a4781e01333a0a3acc527ecb`.
- Archive SHA-256:
  `e8f56d29184f18a2f3c689e0c4b381bf0a1c20aeb1118317c29899e3c50cf545`.
- Release-manifest SHA-256:
  `7d235084ab489cf1b4e0e945972aaa29a5a746af11168cec65b1b8ed64a2f37b`.

## Newly implemented after `f7d0acc`

1. **Independent continuous market-data hot path.** A bounded stream consumer
   drains Massive continuously while cold-start and exact gap/reconnect history
   requests run separately with bounded concurrency. History is requested only
   for newly watched symbols or demonstrated gaps. Per-symbol readiness keeps a
   cold symbol from stopping other candidates, reconciliation, protection, or
   exits. Metrics expose cold/gap/steady REST calls, backlog, queue age, drain
   and processing percentiles, readiness, and latest receipts.
2. **Causal receipts and completed minutes.** REST receipt time is sampled only
   after each response and stream receipt time only after each received batch;
   venue time remains separate. Second aggregates (`A`) cannot enter the minute
   sequence. A minute aggregate (`AM`) must have a minute-aligned `s`, a final
   `e` in the last second, and local receipt after minute end before either the
   cache or stream health can call it complete. Quote freshness uses venue time,
   never a newer local receipt. Current Massive quote sizes remain shares;
   historical round-lot conversion is versioned explicitly.
3. **Conservative broker evidence and recovery.** Genuine provider snapshots
   are distinct from non-atomic collected observations. Non-atomic account
   collection requires stable material rereads, complete page/cursor bindings,
   and request/receipt bounds. Broker-native reviews are distinct from local
   preflight decisions. Exact client-reference positive recovery is preserved,
   while eventually consistent absence becomes `NOT_SEEN_YET`, retains risk and
   `SUBMITTING`, and never triggers a resend or false negative release.
4. **Concrete local provider assembly.** The release launcher now creates one
   immutable `LocalProviderAssembly` shared by provider status, readiness,
   coordinator, and notification worker. It includes Keychain-backed standard
   library Massive REST/WSS clients, deterministic local quality computation,
   and a durable Gmail desktop OAuth/send-only implementation. It deliberately
   does not translate a connected-app token into daemon authority.
5. **One completed owner proposal.** The repository now contains one proposed,
   unapplied policy for genuinely missing numeric score, spread, depth,
   aggregate-risk, sequential-protection, and notification decisions. It keeps
   all established instrument, cash-only, loss-lock, time-window, flatness, and
   broker-confirmation restrictions, and disables optional A+ risk uplift.

## Test and build evidence

- Focused hot-path/provider suite: 21 tests, 21 passed in 0.197 seconds.
- Full Python 3.12 suite: 398 tests, 398 passed in 13.270 seconds.
- Repository validator: PASS across 95 Python, 22 JSON, and 4 TOML files;
  `git diff --check`: PASS.
- Two independent deterministic builds were byte-for-byte identical, including
  archive, manifest, and checksum sidecar.
- The PAUSED installer verified the archive and manifest, preserved the valid
  audit chain, did not access the broker, did not call `launchctl`, and did not
  modify the legacy runtime.

## Built, installed, and actually running

| Layer | Exact status |
|---|---|
| Source and tests | Built and published on the reviewed branch; tests pass. |
| Immutable release | Installed at `/Users/harp/Library/Application Support/Titan Momentum/full-live/releases/45f83b40a1db48b26df383f9141e618825d8ee52a4781e01333a0a3acc527ecb`. |
| Runtime authority | `PAUSED`; `authority_enabled=0`; ready for owner activation is false. |
| Full-live coordinator | Not loaded in launchd and no process is running. |
| Independent notification worker | Not loaded in launchd and no process is running. |
| Legacy Titan runtime | Still running and untouched. It has not been drained or retired because an authorized cutover has not occurred. |

## Real connection results

| Connection | Result | Evidence and remaining action |
|---|---|---|
| Massive REST | **CONNECTED** | Installed client authenticated through Keychain label `titan-massive-api` and completed a market-status read at `2026-09-08T08:49:33.548076Z`. |
| Massive stocks WSS | **CONNECTED** | Installed client authenticated to `wss://socket.massive.com/stocks` at `2026-09-08T08:49:33.691867Z`. It shares redacted binding `e98f0191…04ff1` with REST. |
| Robinhood attended connected route | **AUTHENTICATED READS SUCCEEDED** | At `2026-09-08T08:50:07.733Z`, the Codex-managed OAuth route read the active individual limited-margin account ending 7153, portfolio, equity/option positions, and equity/option orders. No review or mutation was called. This proves the attended connection, not daemon credentials. |
| Robinhood local full-live daemon | **BLOCKED** | The supported endpoint is `https://agent.robinhood.com/mcp/trading`, but its advertised place, review, and cancel lifecycle requires explicit user confirmation and Codex-managed OAuth is not exported. Exact decision: `UNATTENDED_UNSUPPORTED_ON_VERIFIED_ROUTE`. |
| Deterministic local quality | **IMPLEMENTED, BLOCKED** | Requires owner approval or amendment of the proposed thresholds and a fresh broker-capacity/tradability join at the final boundary. It cannot manufacture capacity or full-book depth. |
| Gmail notification | **IMPLEMENTED, NOT CONFIGURED** | Needs owner desktop OAuth consent with only `gmail.send`, durable production consent state, sender/destination Keychain bindings, and one owner-observed delivery test. No email was sent. |

The Robinhood restriction comes from the authenticated server-advertised
contract itself: `place_equity_order` says to get explicit confirmation before
calling; `cancel_equity_order` says to always confirm; the review guide requires
explicit confirmation before placement. The same requirements are advertised
for options, and `get_advanced_orders` is absent from the authenticated 44-tool
inventory. The local helper traversed every client-visible page; no client
pagination divergence was observed, while the origin server's raw cursor was
not exposed. The implementation therefore does not assume a pagination bug and
does not bypass confirmation.

## Exact remaining decisions and authentication

1. The owner must approve or edit the single
   `PROPOSED_OWNER_POLICY_2026-09-08.md`; it is not applied by this deployment.
2. Robinhood must identify a separately supported local-client authorization
   whose documented contract permits unattended place/cancel, exact-reference
   lookup, exhaustive conditional-order reconciliation, durable renewal, and
   broker tradability. If it does not, the owner must explicitly select and
   authorize another supported broker. No account switch, funding, or paid
   service has been assumed.
3. The owner must complete Gmail desktop OAuth and bind the exact send-to-self
   destination. Provider acceptance and owner-observed delivery must be tested
   once before cutover.
4. Only after those bindings exist may a reviewed config enable the concrete
   broker, discovery, notification, and local-mutation gates. Changing booleans
   without the matching provider evidence remains rejected.

## User-controlled activation procedure

The currently installed release is intentionally not activatable. After the
three owner/provider items above are resolved, rebuild and install the resulting
reviewed release PAUSED, then perform this sequence:

1. Run installed `provider-status --probe-network`, `doctor`, and `readiness`.
   Every hard blocker must be absent, including notification-route proof,
   exhaustive broker reconciliation, exact protection capability, approved
   policy provenance, and the one-writer interlock.
2. Reconcile the whole account from fresh broker evidence. Any pending,
   partial, unknown, manual, conditional, option, or uncovered state blocks
   cutover.
3. Start and verify the independent notification worker first, including the
   owner-approved destination test. It must fail closed for entries without
   interrupting reconciliation/protection/exits.
4. At the actual cutover only, drain and retire the legacy writer while
   preserving any required market-data producer. Prove there is one account
   writer before proceeding.
5. Run `prepare-activation --ttl-seconds 300`. Review the complete output and
   copy its one-use 64-character activation ID.
6. As the owner, run `activate --activation-id <ID> --confirm "ACTIVATE FULL LIVE ending-7153 <ID>"` before expiry. This enters `RECONCILING`, not `ACTIVE`, and submits no order.
7. Load the coordinator launchd plist only after activation. The service may
   advance to `ACTIVE` only after its own clean reconciliation and verified
   provider/protection/notification state.

No activation record was prepared, no launchd service was loaded, no broker
mutation or review was called, no email was sent, no financial restriction was
changed, and no additional paid service was added during this deployment.
