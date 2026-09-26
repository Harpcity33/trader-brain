# Full-autonomous IBKR production deployment report

Document status: **FINAL — BUILT AND INSTALLED PAUSED; NOT ACTIVATED**

Report timestamp: `2026-09-14T17:25:34-04:00` America/New_York

Owner-policy approval recorded: `2026-09-14T19:47:43-04:00`
America/New_York; deployment state remains unchanged

Target account: IBKR Pro No Borrow Margin ending 3103

Target mode: full-autonomous regular-hours trading; premarket analysis-only

Activation state: **NOT ACTIVATED**

This report separates implemented code, local tests, authenticated connections,
the built archive, the PAUSED installation, and processes actually running.
No successful build or read-only connection is treated as order authority.

## Outcome

The clean source commit contains the independent engineering path for
full-autonomous IBKR operation: release-bound provider authority, a
confirmation-free autonomous preflight only inside that verified contract,
durable autonomous entry/protection/exit plans, conservative exact-reference
recovery, single-writer/lease interlocks, fee-aware risk reservations, and an
independent durable notification worker. Commit
`e3d99426010760aaef74ff3c315f0c9e96ece1be` passed both complete test
environments, produced a reproducible content-addressed archive, and is
installed at the isolated IBKR root in `PAUSED` mode with authority `0`.

This is not an attended-pilot substitution: full autonomy remains the target.
The installed signed config is deliberately a non-activatable staged profile
while the provider authority, exhaustive broker visibility, application of the
now-approved owner policy, risk provenance, notification route, and remaining
release-bound receipts are unresolved. No order was placed, canceled, or
modified; no IB Gateway setting was changed; and neither autonomous service
was loaded.

## Implemented in the candidate source tree

- Private HMAC-authenticated IBKR autonomous-authority receipt validation bound
  to exact release manifest, config, policy, account fingerprint,
  authorization binding, provider contract, transport, API/version,
  environment, and command client ID.
- A fail-closed autonomous command input loader that authenticates authority
  before opening state and revalidates it at each broker write edge.
- Activation-bound risk lineage and a monotone peak-equity floor now propagate
  through the retained production adapter, transport, autonomous preflight,
  and exact durable entry-risk checker. A validly signed replacement or
  regressed high-water ledger blocks BUY review/dispatch at the inner pre-wire
  edge while SELL protection/exit and cancellation remain available.
- A same-process interlock requiring an already-held exact account writer lock,
  a current durable database lease, and a consumed release-bound activation
  receipt; it cannot acquire or manufacture those prerequisites.
- Durable, hash-sealed ENTRY, PROTECTION, and EXIT plans persisted after a
  PREPARED intent and before broker review/dispatch, with exact tuple, risk,
  account, release, and audit joins and deterministic replay behavior.
- A regular-hours GTC stop-market protection template, immediate protection
  planning after confirmed fill deltas, and no credit for protection until
  strictly newer broker evidence proves the exact working quantity.
- IBKR order/fill normalization with positive `permId`, conservative recovery
  through exact account/client/orderRef/orderId/permId evidence, visibility of
  unowned external orders, and `UNKNOWN = reconcile without retry` behavior.
- Autonomous transport/preflight wiring that accepts no confirmation token in
  autonomous mode, keeps place/cancel serialized, and leaves ambiguous writes
  unresolved rather than inferring success. Placements are never retried. An
  ambiguous cancel can create a new bounded intent only after strictly newer
  positive whole-account evidence proves the exact order remains working; the
  resolving receipt itself cannot dispatch.
- SDK boundary denials that conclusively touch no wire are persisted as local
  known-not-sent outcomes rather than false broker uncertainty. Actual socket
  failures remain `UNKNOWN`, retain reservations, and require reconciliation.
- Entry risk/capacity reservation for a signed per-order commission floor,
  proposed at $1, with at least $2 reserved for the entry plus one
  protection/exit leg. Activation remains blocked until effective account
  pricing proves the selected floor conservative or the owner raises it.
- Gmail API delivery support through a durable local outbox and independent
  worker. Signed policy stores only non-secret route provenance; destination
  and OAuth material are injected from owner-authorized local bindings.
- The autonomous coordinator uses a separately attested safety-core provider
  graph containing the IBKR transport, control authenticator, and plan sealer,
  but no Gmail or Massive credential construction. Gmail or discovery startup
  failure therefore blocks only new entries; broker reconciliation,
  protection, exits, and closeout continue. The notification worker can start
  without importing the IBKR SDK.
- Release-composition attestation for autonomous plan-reader/sealer and writer
  components, including separation from read-only status/provider/notification
  commands.
- A dedicated premarket analysis-only executor and service scheduler: one
  deterministic ranking per exact 30-minute slot, broker reconciliation first,
  durable restart de-duplication in the append-only audit chain, and explicit
  rejection of any result or candidate claiming order authority. Premarket
  failures do not become regular-hours entry blockers.
- A durably latched first-target exit using only fresh aligned completed-minute
  bars, followed by the ordinary cancel-before-close path; unexplained broker
  flatness cannot clear the latch without matching durable sell-fill evidence.
- A conservative closeout-feasibility budget that starts the existing managed
  closeout early when unresolved cancel/exit/reconcile steps no longer fit
  before the exchange-calendar flat deadline.
- Post-session, premarket, weekend, or holiday discovery of an exposure whose
  same-session origin cannot be proved from exact durable broker evidence is
  closeout-owned rather than treated as a candidate for a new entry lifecycle.
- Release installation validates one immutable, bounded archive byte snapshot
  from a no-follow file descriptor and rejects path replacement before any
  install-root mutation. The builder rejects every untracked file (including
  ignored files), writes only outside the source repository, and reads payloads
  from the exact commit. The installer then requires a separately supplied
  trusted repository and exact revision, disables Git replacement objects, and
  reconstructs the complete manifest from that commit before it accepts the
  archive. It also verifies its own exact archive-attested bytes, runtime import
  closure, release-pinned SDK inventory, and PAUSED install state.
- The IBKR SDK dependency is pinned by the committed release config to exact
  inventory SHA-256
  `3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc`;
  matching package-version metadata alone is insufficient. Installation copies
  and revalidates only that byte inventory. This is a local reviewed dependency
  pin, not a claim that IBKR cryptographically signed those local files.

This list is the implementation inventory. The following sections state the
independent test, connection, build, installation, and runtime evidence.

## Locally exercised

| Check | Final result | Evidence source |
|---|---|---|
| Deployment/provenance security suite | **48 passed, 0 failed** in 45.152 s | Bundled Python 3.12.14: `python -B -m unittest -q tests.test_live_deployment`; `release-artifacts/test-logs/e3d9942-deployment-security.log` |
| Complete repository suite, bundled runtime | **1,021 passed, 2 expected SDK-dependent skips, 0 failed** in 53.909 s | `python -B -m unittest discover -s tests -q`; `release-artifacts/test-logs/e3d9942-bundled-full.log` |
| Complete repository suite, authorized SDK venv | **1,021 passed, 0 skipped, 0 failed** in 53.907 s | Authorized Python/IB API venv; `release-artifacts/test-logs/e3d9942-sdk-full.log` |
| Compilation/import validation | **PASS** | `python -B -m compileall -q src scripts tests`; installer also verified the release import closure before mutation |
| Repository validator | **PASS** | 154 Python, 24 JSON, and 4 TOML files; checked 6,906,313 bytes; validator manifest `c8185f5d2183842573984b5d750dc90ba94befdef4784254539e86576039c425` |
| `git diff --check` | **PASS** | Clean immediately before the implementation build |
| Clean committed-tree verification | **PASS** | Exact source commit `e3d99426010760aaef74ff3c315f0c9e96ece1be`; no tracked changes or untracked files |

Both complete suites exercised the same clean implementation commit used by
the release builder. The second environment contained the exact pinned IB API
and protobuf inventory; the bundled environment's two skips were the expected
absence of that external SDK from its interpreter.

## Authenticated connections

| Connection | Final result | Exact evidence |
|---|---|---|
| Massive REST | **CONNECTED / AUTHENTICATED** | Authenticated market-status read succeeded at `2026-09-14T21:25:47.536124+00:00`; Keychain credential label `titan-massive-api`; provider binding `e98f0191aaeaff9f41cd5f498bd943235d03da91391032884203f1b642804ff1` |
| Massive WebSocket | **CONNECTED / AUTHENTICATED** | `wss://socket.massive.com/stocks` authenticated handshake succeeded at `2026-09-14T21:25:47.678291+00:00`; same provider binding |
| IB Gateway socket/account discovery | **CONNECTED / AUTHENTICATED** | Official TWS API reached `127.0.0.1:4001` and matched the managed account ending 3103 at `2026-09-14T21:25:48.149607+00:00`; provider binding `1a7840d36d19e23ffd23ec1f44917c95c750074f6a37ae306429dc09324a5a04` |
| IBKR whole-account read | **BLOCKED** | Exhaustive account/order read failed closed with `LOCAL_ASSEMBLY_IBKR_RUNTIME_READ_SDK_CALLBACK_321`; completed-order coverage is not proven |
| IBKR contract/tradability read | **BLOCKED / NOT REACHED** | `LOCAL_ASSEMBLY_IBKR_CONTRACT_READ_NOT_REACHED` because the exhaustive account-read prerequisite failed |
| IBKR autonomous authority | `NOT AUTHENTICATED / NOT ISSUED` | Provider response and signed release-bound authority receipt remain absent. |
| Gmail destination route | `NOT AUTHENTICATED / NOT OWNER-TESTED` | Owner destination consent, authorization binding, and visible route test remain absent. |

These are fresh probes executed through the installed release. The provider
report records `broker_mutations_invoked: []` and
`notification_messages_sent: []`. The socket/account success does not prove
whole-broker visibility or write authority.

## Built

- Branch: `codex/full-live-2026-09-08`.
- Candidate source commit:
  `e3d99426010760aaef74ff3c315f0c9e96ece1be`.
- Remote publication: **BLOCKED ON THIS HOST**. `git push origin
  codex/full-live-2026-09-08` returned exit 128: `fatal: could not read Username
  for 'https://github.com': Device not configured`. The configured remote is
  `https://github.com/Harpcity33/trader-brain.git`; no `gh` executable is
  installed. Local commits are intact.
- Release ID:
  `4f3c543a4ccdc7a5d93f2c9a1b4be25590d4ccf7ad7107d9aac02141aa970f5b`.
- Archive:
  `/Users/harp/Documents/Codex/2026-08-18/v/release-artifacts/full-live-e3d9942/titan-full-live-4f3c543a4ccdc7a5d93f.tar.gz`.
- Archive SHA-256:
  `4ee93a5f74029f045e5f6e9cc4ff88e639d66959538b3ed20f33a33228b4ecae`.
- Release-manifest SHA-256:
  `e7b2a7ce016d64b732de1aa038c2fed635cfb0022e1a39445e50031156bac9df`.
- Reproducibility: a second clean build produced the identical release ID,
  archive SHA-256, manifest SHA-256, and byte-for-byte archive.
- Release-pinned SDK inventory: IB API `10.50.2`, protobuf `5.29.5`, inventory
  SHA-256 `3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc`.
- Installed SDK attestation: 303 files, 2,897,207 bytes, matching inventory
  hash, receipt SHA-256
  `996ce65c9f9ca192eae75609cfa1651745c0fc91e0372ec6d44a64bc0cfa45f3`.
- Twelve older ignored build artifacts were preserved outside the repository
  at `release-artifacts/trader-brain-pre-20260914`; none entered this release.

## Installed

Autonomous-capable candidate: **INSTALLED PAUSED; NOT ACTIVATABLE YET**.

- Install root:
  `/Users/harp/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`.
- New installed release:
  `4f3c543a4ccdc7a5d93f2c9a1b4be25590d4ccf7ad7107d9aac02141aa970f5b`.
- Installer verification: **PASS** under schema
  `titan_full_live_paused_install_2026-09-14_v4`; 98 release files matched the
  exact trusted Git commit with replacement objects disabled. Installer
  self-attestation SHA-256 is
  `8ae4b79592dbc6b23ec114182a7b4549a8ae54b171393f1b3bd5ded62c257435`.
- Installed at `2026-09-14T21:24:04.167495+00:00`.
- Installed mode: **PAUSED**.
- Authority value: **0**; no activation record is consumed.
- Coordinator launchd: plist staged disabled; label not loaded (`launchctl`
  exit 113).
- Notification-worker launchd: plist staged disabled; label not loaded
  (`launchctl` exit 113).
- Installed `status`: release valid, runtime bindings valid, audit generation
  2, no writer lease, 0 nonzero positions, 0 open incidents, 0 pending
  notifications, and 0 unknown/submitting intents in the local release state.
  This is not a broker-flatness assertion because exhaustive broker read is
  blocked.
- Installed `doctor`: release and seven-event audit chain valid, mode PAUSED,
  `ready_for_owner_activation: false`, and 38 explicit blockers. The staged
  profile remains `attended_only`, has no daemon transport, no autonomous
  authority, no live discovery/threshold/target/risk provenance, and no
  configured notification destination. Four end-of-session Massive freshness
  blockers were also present after the producer stopped normally.
- Installed `provider-status --probe-network`: Massive REST/WSS and IBKR
  managed-account discovery connected; exhaustive IBKR read and downstream
  contract read blocked as recorded above; no mutation or notification call.
- SDK status: **ATTESTED**, 303 files, IB API 10.50.2, protobuf 5.29.5, exact
  release-pinned inventory hash.
- Risk-high-water migration reported `RISK_LEDGER_NOT_CONFIGURED`: the staged
  attended config intentionally does not yet contain the target autonomous
  risk-evidence paths. The approved target policy requires the monotone ledger,
  but a new clean release must bind it while preserving the separate live
  provenance gate.

The prior release
`2555007a709b80a4a69ad3c4d902ecef3f4684ed99d39c137187e2c7bbf16b6d`
was replaced only as the `current` PAUSED pointer; it remains content-addressed
under the install root. Runtime identity was safely rebound in PAUSED mode.

## Actually running

Full-autonomous coordinator: **NOT RUNNING**.

IBKR command writer: **NOT RUNNING**.

Independent Gmail notification worker: **NOT RUNNING**.

- IB Gateway 10.45: **RUNNING**, PID 87079 at
  `2026-09-14T17:24:42-04:00`; listening on TCP port 4001. The successful
  managed-account probe proves the local API connection, not complete order
  visibility or write authority.
- Massive producer launchd `com.titan.momentum-watcher`: loaded but **not
  running** after the regular session, last exit code 0. Latest local quote was
  `2026-09-14T20:04:01.029000+00:00` and latest completed bar was
  `2026-09-14T20:04:00+00:00`; two existing Massive MCP server processes were
  observed (PIDs 10483 and 52643). Fresh REST and WebSocket connection probes
  independently succeeded.
- Analysis/desk heartbeat `IBKR Titan — Premarket Analysis + Live Desk`:
  **ACTIVE**. Schedule is read-only analysis at 07:00, 07:30, 08:00, 08:30,
  and 09:00 ET, then one-minute runs from 09:30 through 15:59 on weekdays. It
  is explicitly the attended compatibility fallback, not the autonomous
  coordinator and not evidence that full autonomy is running.
- Coordinator launchd label:
  `com.harpcity.trader-brain-full-live-ibkr-3103` — **NOT LOADED** and must
  remain unloaded until every gate passes and the owner activates.
- Notification launchd label:
  `com.harpcity.trader-brain-full-live-ibkr-3103-notifications` — **NOT
  LOADED** and must remain unloaded until the owner authorizes/tests the exact
  Gmail route.

## Proposed policy state

The owner approved the exact immutable contents of
`PROPOSED_OWNER_POLICY_2026-09-14.md`, SHA-256
`cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf`,
with the statement `Approved for policy`. The binding provenance and scope are
recorded in `OWNER_POLICY_APPROVAL_2026-09-14.md`. The proposal file retains
its historical **PROPOSED, NOT APPROVED, NOT APPLIED** heading so its approved
bytes and hash do not change. The approval is **NOT APPLIED** to the installed
config and does not activate any service or broker mutation.

The approved policy preserves the existing financial restrictions and fixes
the missing production choices at 25 bps maximum entry spread, 5x
executable-side top-of-book in shares, ranking-only scores with A+ disabled,
a first-target completed-minute full-position exit, sequential verified
regular-hours GTC stop-market protection, conservative closeout-feasibility
escalation, a $1 minimum commission floor per order and $2 minimum
entry-lifecycle reserve, and independent Gmail delivery with a durable outbox.
Delivery failure pauses new entries while reconciliation, protection, exits,
and closeout continue. Gmail delivery is at-least-once and can duplicate an
alert if the process loses the local receipt after provider acceptance.

The requested execution mode is genuinely autonomous, not the earlier attended
pilot. Per-order confirmation may be absent only when a verified IBKR
provider-authority receipt proves that exact external-market-data workflow is
supported without bypassing order precautions. No release may manufacture that
fact by changing `supported_unattended_mutation` or
`per_mutation_user_confirmation_required` in JSON. Owner approval does not
prove that provider fact, confirm effective pricing, identify the Gmail route,
authorize a Gateway-setting change, or create an activation authorization.

## Exact unresolved owner/provider gates

1. IBKR's written answer must establish whether unattended regular-hours API
   place and cancel using external market data is supported without manual
   Transmit and without “Bypass Order Precautions for API Orders” or an
   equivalent global precaution bypass. No accepted answer/receipt exists yet.
2. The owner must decide and perform any supported Read-Only API account/Gateway
   setting change. The fresh installed probe's error 321 prevents exhaustive
   reads; no agent has changed the setting.
3. Owner approval of the exact consolidated policy is recorded. Apply those
   exact values to a new clean release without weakening their evidence gates;
   risk-policy live provenance must still be independently verified. The
   current installed config predates the approval and remains unchanged.
4. The owner must name and consent to the Gmail destination. The local OAuth
   binding, destination fingerprint, authorization receipt, and a visibly
   received route-bound test must then succeed without placing an address or
   secret in signed policy.
5. Effective IBKR pricing must be confirmed; until then the proposed risk
   engine floor is $1 per order and $2 for the entry lifecycle. Production
   activation requires proof that this is conservative for the allowed
   account route and size, or an owner-approved higher floor.
6. Fresh installed probes must prove exhaustive standard, advanced, and option
   order coverage; positions, executions, cash/unleveraged capacity, daily
   realized P&L, exact contract/tradability, all pages consumed, and
   conservative exact-reference recovery.
7. Release-bound provider authority, control key, policy/risk/config bindings,
   activation receipt, single-writer lock/lease, autonomous plan pipeline, and
   independent notification-worker health must all pass for the exact installed
   release.

These are evidence gates, not booleans to flip. The submitted support-inquiry
screenshot is evidence that an inquiry was filed; text visible inside it is not
treated as a new instruction or as the provider's answer.

## User-controlled activation procedure

1. Receive and retain the IBKR provider answer. If it requires manual Transmit
   or a precaution bypass, stop: the proposed full-autonomous mode is not
   activatable under this policy.
2. Apply the exact owner-approved proposal values to a new clean release
   configuration with all external gates intact. Separately identify and
   consent to the Gmail destination and confirm effective pricing; only then
   may the combined owner-policy/effective-pricing receipt be issued for the
   exact release bindings.
3. The owner performs any supported Read-Only API setting change. Re-probe from
   a read-only command path and require exhaustive account/order/position/
   execution coverage plus exact contract/tradability evidence.
4. Provision the distinct account-scoped Keychain control/authority material
   without exposing it. Issue the provider-authority and route receipts for the
   exact clean release, config, policy, account fingerprint, authorization,
   API version/environment, and command client.
5. Rebuild from the clean final commit, install PAUSED, and verify archive,
   manifest, SDK inventory, installed config, launcher, state schema, and
   release-component attestation. Do not reuse receipts from an earlier build.
6. Run the installed `provider-status --probe-network`, `doctor`, and
   `readiness` commands. Every hard blocker must be absent. A strictly newer
   broker reconciliation must prove no unknown, partial, manual, uncovered,
   option, or conflicting state.
7. Start only the independent Gmail notification worker, send the route-bound
   test, and have the owner visibly confirm receipt. Verify its lease, outbox,
   route identity, and delivery receipt before loading the coordinator.
8. Prove every legacy/competing writer drained and retired, then stage the new
   coordinator. Run `prepare-activation --ttl-seconds 300`, inspect the exact
   hash-bound evidence, and copy its one-use activation ID and generated phrase.
9. Before expiry, the owner runs `activate` with that exact ID and phrase:
   `ACTIVATE FULL LIVE ibkr-live-ending-3103 <activation-id>`. Activation enters
   `RECONCILING`, not trading-active state, and submits no order by itself.
10. Only a strictly newer clean broker reconciliation may promote the exact
    installed release to autonomous operation. Thereafter eligible mutations
    do not request per-order confirmation, but every policy, evidence,
    authority, interlock, risk, protection, reconciliation, closeout, and
    notification gate remains enforced. Owner pause/revocation remains
    available at all times.

Final deployment evidence must state separately what is implemented, locally
exercised, authenticated, built, installed, and actually running. “Built” must
never be used as a synonym for “activated.”
