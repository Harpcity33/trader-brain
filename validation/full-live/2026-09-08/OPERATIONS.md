# Titan full-live operations — September 8, 2026 target

## Status vocabulary

- **BUILT** means the source and deterministic release archive exist and pass
  tests. It does not mean they are installed.
- **INSTALLED_PAUSED** means a verified release exists below
  `~/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`, its durable runtime
  identity is `PAUSED` (or has not yet been initialized), and its disabled
  launchd plists are only staged under that subtree.
- **RUNNING_RECONCILE_ONLY** means the owner has explicitly started the new
  writer in its non-entry state and it is collecting fresh readiness evidence.
- **ACTIVE** means the one-use, release-bound activation record was consumed,
  the service entered `RECONCILING`, and the service itself advanced to
  `ACTIVE` only after a fresh clean broker reconciliation. Configuration text,
  installation, a loaded plist, or a running PID is not proof of this state.

As delivered from source, `config/full_live_ibkr.json` keeps live entries and
the local mutation interlock disabled. It contains explicit broker,
reconciliation, risk-provenance, live-evidence-provider, notification,
and all-in fee-bound blockers. Approved operational spread/depth and target
semantics are applied, with their immutable approval artifacts hash-bound into
the release. An
authenticated local IB Gateway read does not by itself prove a supported
unattended place/cancel contract. The selected profile remains staged until
IBKR's exact external-data/manual-Transmit contract is verified, the required
account settings are verified, exhaustive reads pass, and every
release-bound authority and notification receipt exists. Those evidence gates
cannot be waived with a confirmation phrase or cleared by editing booleans.

## Deterministic build

From a clean, reviewed repository checkout, record the exact `HEAD` commit and
build. `--source-revision` is an assertion, not a free-form manifest label: it
must resolve to the exact current `HEAD`. The builder rejects staged or
unstaged tracked changes and every untracked repository file, including
ignored files. The output directory must resolve outside the source
repository.

```sh
PYTHON=/absolute/path/to/python3.11-or-newer
"$PYTHON" scripts/build_full_live_release.py \
  --source-root /absolute/path/to/trader-brain \
  --output-dir /absolute/path/to/release-output \
  --source-revision EXACT_40_CHARACTER_GIT_COMMIT \
  --config config/full_live_ibkr.json
```

The output contains:

- `titan-full-live-<manifest-hash-prefix>.tar.gz`
- the same path plus `.sha256`
- the same path plus `.manifest.json`

The manifest schema is `titan_full_live_release_2026-09-14_v2`.
`release_manifest_hash` is SHA-256 of canonical JSON after dropping only that
field. `source_tree` is the canonical file-inventory hash. `config_hash` and
`policy_hash` use the runtime's policy hashing convention. The manifest is
excluded from its own file inventory. Repeating a build with identical content
and source revision must produce identical bytes. Payload bytes are read from
the recorded commit's Git blobs, so every packaged file digest is bound to
`source_commit`; an untracked repository file prevents the build.

## Paused installation

Installation is intentionally separate from activation. Use the same Python
3.11+ interpreter intended for the service:

```sh
PYTHON=/absolute/path/to/python3.11-or-newer
IBKR_SDK_VENV=/absolute/path/to/authorized-ibapi-10.50.2-venv
"$PYTHON" scripts/install_full_live_paused.py \
  /absolute/path/to/titan-full-live-<hash>.tar.gz \
  --root "$HOME/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103" \
  --trusted-source-root /absolute/path/to/reviewed/trader-brain \
  --expected-source-revision EXACT_40_CHARACTER_GIT_COMMIT \
  --python-executable "$PYTHON" \
  --ibkr-sdk-venv "$IBKR_SDK_VENV"
```

The installer verifies the sidecar archive hash, embedded manifest, complete
file inventory, and every file digest. The trusted repository and full source
commit are mandatory independent inputs. Before creating the install root, the
installer disables Git replacement objects and reconstructs the complete
deterministic manifest from that commit's blobs and executable modes. An
internally rehashed archive, a different or missing commit, an incomplete
inventory, or a replaced source therefore fails closed. It then installs a
content-addressed release, updates `current`, writes `release-manifest.json`,
and stages these two disabled plists only:

For the IBKR profile, the committed provider config also pins the exact
approved SDK/protobuf file-inventory SHA-256. The installer inventories the
supplied venv without importing it and refuses matching version metadata with
different bytes. The installed receipt and runtime revalidation must match that
release pin; the receipt proves local byte identity, not a vendor signature.

`~/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103/launchd/com.harpcity.trader-brain-full-live-ibkr-3103.plist`

`~/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103/launchd/com.harpcity.trader-brain-full-live-ibkr-3103-notifications.plist`

The first process is the trading coordinator. The second is the only process
permitted to claim durable outbox rows and deliver them to the signed provider
route. The coordinator only enqueues; it neither sends nor starts the worker.
The two labels, argument vectors, logs, and supervision lifecycles are distinct.

The installer does not call `launchctl`, start a process, access IBKR,
initialize a missing state database, copy anything to `~/Library/LaunchAgents`,
or modify the legacy `Titan Momentum` runtime. If a full-live state database
already exists, a release switch requires an exact recognized v1, v2, or v3
schema, an intact audit chain, exactly one unarmed `PAUSED` runtime identity for
the same account, and released writer and notification-worker leases. Under the
same fixed account interlock and one `BEGIN IMMEDIATE` transaction, the
installer upgrades recognized v1/v2 schemas to v3, expires all unconsumed
prior-release activation records, rebinds the durable runtime/config/policy
identity, increments its generation, and appends schema and release migration
events before switching `current`. Partial, altered, or unknown schemas fail
closed before the pointer switch.
Prior-release control requests remain invalid because their signed binding
contains the old release hash. The install record reports whether migration
occurred and how many activation records it invalidated.

If any proof fails, `current` is not changed. If power is lost after the SQLite
transaction commits but before `current` and its metadata are updated, all CLI
commands fail closed on the release/database identity mismatch; rerun the exact
verified paused installer to finish the pointer commit. Do not edit either
identity by hand.

Both the installer and coordinator use the same fixed per-user lock directory,
independent of `--install-root`:

`~/Library/Application Support/Titan Momentum/account-writer-locks`

There is no production CLI, environment, or signed-config override for this
path. The target account's nonsecret key is hashed into the lock filename.

## First initialization and inspection

Set paths explicitly and inspect before any scheduler action:

```sh
INSTALL_ROOT="$HOME/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103"
LAUNCHER="$INSTALL_ROOT/current/scripts/titan-full-live"
PYTHON=/absolute/path/to/the-same-python3.11-or-newer

"$PYTHON" "$LAUNCHER" init-state --install-root "$INSTALL_ROOT"
"$PYTHON" "$LAUNCHER" status --install-root "$INSTALL_ROOT"
"$PYTHON" "$LAUNCHER" doctor --install-root "$INSTALL_ROOT"
```

Expected initial runtime mode is `PAUSED`. Verify that the reported release,
configuration, policy, account suffix, and database-schema hashes match
`$INSTALL_ROOT/release-manifest.json`. A missing or mismatched value is a hard
stop.

### Independent notification setup inventory

Before provisioning or testing a route, run:

```sh
"$PYTHON" "$LAUNCHER" notification-setup-status --install-root "$INSTALL_ROOT"
```

This diagnostic validates the installed release and checks only the metadata
of five exact account-scoped Keychain locators. It does not read secret
contents, import/activate the IBKR SDK, compose a broker or market-data client,
make a network request, send an email, modify local state, or issue readiness
evidence. It works when the broker SDK is unavailable. Exit status 2 means
setup/evidence remains unverified, not that a broker probe was attempted.

For account ending 3103, the `ibkr_gmail` profile is distinct from the legacy
Robinhood `gmail` profile. Its credentials cannot be borrowed from account
ending 7153. A connected Gmail app session also does not establish that the
independent local worker has a durable OAuth authorization. The owner must
authorize the destination and a test; the local credential/consent binding and
an actually received route-bound test must then be verified. Metadata presence
alone does not satisfy any of those proofs. Do not send a connector-only test
and describe it as an independent-worker test.

### Exact local provider assembly and remaining prerequisites

The staged coordinator and independent notification worker invoke Python as
`python -I -S -B`, so neither service loads user/site customization or an
environment-supplied import path, and neither writes bytecode into a release.

The stock `scripts/titan-full-live` launcher constructs one release-contained
`LocalProviderAssembly` from the immutable nonsecret profile in
`config/provider_bindings.json`. Doctor, readiness, and the coordinator receive
the complete release-attested broker/discovery composition. The independent
notification worker and route-test command receive a delivery-only composition
and never instantiate IBKR or Massive clients, allowing them to coexist with
the fixed-ID broker reader. Private values are read only from the named macOS
Keychain items; connected-app tokens are never exported.

The current assembly contains concrete provider-supported Massive REST/WSS
clients, official local IBKR TWS API readers/contract evidence, a guarded IBKR
order transport, and a durable Gmail desktop OAuth sender implementation.
Gmail remains disabled until the owner authorizes its exact account and
destination. The IBKR writer is unavailable unless the immutable release and a
private authority artifact prove the supported endpoint/account/client and the
no-confirmation contract. Full-live readiness must remain blocked until the
following evidence is supplied:

- an approved `IbkrProductionTransport` for exhaustive reads and mutations
  whose provider contract permits the intended unattended lifecycle without
  manual Transmit or bypassing API precautions;
- `SupportedDiscoveryProviderComposition`, built around
  `MassiveRestStreamSource`, authenticated IBKR contract/tradability evidence,
  and the release-contained deterministic quality reader sharing the exact
  signed non-secret provider binding;
- an owner-authorized `GmailProviderBinding` with exact signed
  implementation, authorization, sender, and destination bindings;
- a runtime-only control authenticator with at least 256 bits of secret entropy,
  bound to that same signed production authorization receipt. The secret is
  never placed in the release, configuration, command line, or control request.

The executable transport, stream, candidate, instrument, quality, and
notification components must all reside in and match the same release
manifest. Tokens remain outside the release and are loaded through the
reviewed Keychain-backed clients. Until the broker route, owner policy,
tradability, notification destination, and control authentication are bound in
a rebuilt and reviewed release, `doctor`, `readiness`, `serve`, and
`notification-worker` fail closed. Do not patch the installed release or put a
token in configuration, an environment dump, a command line, or the
repository.

### Daily autonomous risk baseline

An unattended release also requires a private, canonical daily IBKR risk
baseline at:

```text
$INSTALL_ROOT/control/ibkr/daily-risk-baseline.json
```

The file must be a non-symlink regular file owned by the local user, have mode
`0400` or `0600`, and carry schema
`titan_ibkr_daily_risk_baseline_2026-09-14_v1`. Its HMAC key is a third,
distinct account-scoped Keychain item selected by the signed policy; it may not
reuse the provider-authority or owner-policy/pricing key. The receipt binds the
exact release-manifest, config, policy, risk, account, and account-fingerprint
identities plus the current and prior exchange-calendar trading dates. Its
evidence must be USD, exact-account and broker-authoritative, and must include
week-to-date realized P&L through the prior trading day, prior high-water
equity, an immutable provider receipt hash, provider source, and provider
observation time.

No release command creates or signs this provider fact. A trusted existing
control plane must derive it from authenticated IBKR account records and place
the exact signed file before readiness. The runtime authenticates it before
opening the command session and again on every broker snapshot, combines it
only with fresh current-day `reqPnL.realizedPnL`, and advances the separately
bound monotone ledger at
`$INSTALL_ROOT/state/ibkr-risk-high-water.sqlite3`. The calendar-backed binding
rotates at each trading date without a daemon restart; a missing, stale,
altered, future, non-trading-day, or regressing receipt blocks new write
authority. It never weakens reconciliation, protection, exit, or closeout
requirements.

## Readiness and owner-controlled cutover

Do not disable the existing account writer merely because package tests pass.
First verify any broker-held protection and establish an explicit rollback
window. The supported sequence is:

1. Run `doctor` and resolve every policy, broker-capability, authentication,
   market-data, notification, and release-integrity blocker. Numeric spread and
   depth thresholds require approved policy values; absence is not unlimited.
   Before an unattended probe, provision the exact release-bound daily risk
   baseline described above and verify that its Keychain locator and private
   file are distinct from the provider-authority and owner-policy/pricing
   receipts. A baseline from an earlier release or trading date cannot be
   reused.
2. Confirm all standard equity positions/orders, option positions/orders, and
   supported advanced-order state from strictly fresh broker evidence. Any
   unknown submission or uncovered quantity blocks cutover.
3. Test the exact signed notification route with its independent worker. The
   launcher must load the owner-authorized Keychain-backed Gmail binding and
   its implementation, authorization, sender, and destination binding IDs must
   match signed configuration. Without that exact binding, the worker fails
   closed before claiming a provider-bound row.

   As an explicit owner action, copy, enable, bootstrap, and start only the
   notification worker first. Do not start the trading coordinator here:

   ```sh
   install -m 600 \
     "$INSTALL_ROOT/launchd/com.harpcity.trader-brain-full-live-ibkr-3103-notifications.plist" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-ibkr-3103-notifications.plist"
   launchctl enable \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live-ibkr-3103-notifications"
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-ibkr-3103-notifications.plist"
   launchctl kickstart -k \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live-ibkr-3103-notifications"
   ```

   Then enqueue the redacted route test. The command and worker must record a
   structured provider receipt bound to the exact route ID, provider,
   destination fingerprint, route version, event key, payload hash, and signed
   assurance. Do not put secrets or full account identifiers in the payload:

   ```sh
   "$PYTHON" "$LAUNCHER" notification-test \
     --install-root "$INSTALL_ROOT" \
     --event-id "owner-readiness-2026-09-08"
   ```
   Verify the configured provider actually delivers the same event to the
   existing user destination. A `LOCAL_STAGED` receipt or local JSONL line never
   satisfies activation readiness. The test command only enqueues; it never
   sends synchronously. Readiness requires the independent worker's exact-route
   lease to be unreleased, its recorded PID to be alive, its heartbeat to be no
   more than 15 seconds old, its last cycle to have zero failures, the account
   outbox to have zero pending or claimed rows, and the exact structured
   provider receipt to be no more than 300 seconds old. The checked-in September
   8 configuration intentionally uses local staging and therefore cannot pass
   this gate.
4. Retire both same-account Codex automations through the Codex automation
   control plane, and wait for both to report `PAUSED` or `DISABLED` with zero
   running executions:

   - `robinhood-momentum-engine`
   - `robinhood-titan-premarket-deep-dive`

   Preserve any working broker-held protection; disabling a writer is not a
   closeout. The new writer must still be stopped here so the activation
   commands can prove exclusive ownership of the account lock. Do not edit an
   `automation.toml`: it is configuration evidence only and cannot prove the
   scheduler's loaded state or whether an execution is still running.

   The owner-authorized control-plane bridge must query both exact automation
   IDs in one operation and inject a canonical, HMAC-SHA256-signed snapshot at:

   ```text
   $INSTALL_ROOT/control/codex-scheduler-retirement-evidence.json
   ```

   The signer key is a minimum-256-bit generic-password value in macOS
   Keychain service `titan-full-live-codex-scheduler-control-plane`, account
   `ibkr-live-ending-3103`. The at-most-ten-second snapshot must bind the exact
   release-manifest, configuration, policy, runtime, and account hashes/IDs and
   must include, for each required automation, its scheduler runtime ID,
   status, configuration hash, active-execution count, observation time, and
   control-plane query-receipt hash. Its schema is
   `titan_codex_scheduler_retirement_evidence_2026-09-14_v1`; the HMAC covers
   the ASCII canonical JSON body (sorted keys, no insignificant whitespace),
   excluding only the top-level `hmac_sha256`, and the private file ends in one
   newline with mode `0400` or `0600`. The installed adapter only authenticates
   and reads this evidence; it never pauses an automation or writes scheduler
   configuration. Do not substitute a copied/hand-written JSON file, a process
   inference, or a local TOML digest.

   While the signed snapshot remains current, record the durable retirement
   and broker-drain receipt:

   ```sh
   "$PYTHON" "$LAUNCHER" record-legacy-retirement \
     --install-root "$INSTALL_ROOT"
   ```

   That command does not change either automation, signal a process, or mutate
   the broker. Every later `readiness`, `prepare-activation`, and `activate`
   invocation requires another fresh signed control-plane snapshot proving
   both automations remain retired and their combined running-execution count
   is zero.
5. Stop every account writer, then ask the installed runtime to collect a fresh
   diagnostic attestation. There is no readiness-file input: the command takes
   the kernel account lock and matching database lease, attempts an actual
   broker read, inspects the newest durable whole-broker reconciliation and
   audit chain, computes unknown and uncovered exposure, reads Massive health,
   verifies the user-destination delivery receipt, reads the installed legacy
   heartbeat configuration, and requires independent scheduler runtime evidence:

   ```sh
   "$PYTHON" "$LAUNCHER" readiness --install-root "$INSTALL_ROOT"
   ```

   Review the full evidence and its `readiness_hash`. Current production
   configuration must report blocked while the staged broker adapter,
   unresolved signed policy/provider gates, local-only notification sink, or
   competing legacy writer remains. Do not edit or copy this output back as
   input.
6. Prepare a short-lived, one-use activation record. The command independently
   repeats the machine probes, embeds the complete evidence, binds its SHA-256
   into the activation ID, and stages nothing unless every hard gate passes:

   ```sh
   "$PYTHON" "$LAUNCHER" prepare-activation \
     --install-root "$INSTALL_ROOT" \
     --ttl-seconds 300
   ```

   Review the complete output and exact phrase. Run the separate owner-controlled
   activation immediately: the record may have a five-minute outer TTL, but its
   broker snapshot, writer lease, quote, and reconciliation evidence retain the
   much shorter signed freshness limits. If any fact expires or changes, prepare
   a new record and review its new activation ID and phrase.

   ```sh
   ACTIVATION_ID=exact_64_character_id_from_prepare_output
     "$PYTHON" "$LAUNCHER" activate \
     --install-root "$INSTALL_ROOT" \
     --activation-id "$ACTIVATION_ID" \
     --confirm "ACTIVATE FULL LIVE ibkr-live-ending-3103 $ACTIVATION_ID"
   ```

   The activation command reacquires both writer authorities, repeats all
   machine probes, verifies the stored record's canonical bytes and both hashes,
   checks the exact owner phrase, and requires the same newest durable flat
   reconciliation before atomically consuming the one-use record. It may only
   persist `PAUSED -> RECONCILING`; it cannot submit an order. If it fails, do
   not start the new writer.
7. Immediately after successful activation, verify the independently supervised
   notification worker is still healthy. Then copy the staged disabled trading
   coordinator plist to `~/Library/LaunchAgents` only as an explicit owner
   action, then enable, bootstrap, and kick-start it. The plist has
   `Disabled=true`, no `RunAtLoad`,
   and `KeepAlive.SuccessfulExit=false` so launchd restarts an unexpected
   service failure after the owner starts it. The service has no CLI live-mode
   override.

   ```sh
   install -m 600 \
     "$INSTALL_ROOT/launchd/com.harpcity.trader-brain-full-live-ibkr-3103.plist" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-ibkr-3103.plist"
   launchctl enable "gui/$(id -u)/com.harpcity.trader-brain-full-live-ibkr-3103"
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-ibkr-3103.plist"
   launchctl kickstart -k \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live-ibkr-3103"
   "$PYTHON" "$LAUNCHER" status --install-root "$INSTALL_ROOT"
   ```
8. Verify a single new account-writer lock holder, fresh reconciliation,
   complete order coverage, zero unknown submissions, zero uncovered quantity,
   and a working notification route. If this cannot be established promptly,
   stop the new service and restore the prior writer without disturbing
   broker-held protection.
9. The running service—not
   the operator command—may transition `RECONCILING -> ACTIVE`, and only after
   another fresh clean startup reconciliation. Verify status reports `ACTIVE`
   before reporting live operation.

There is intentionally no edit-the-database, `--force`, acknowledge-blockers,
or `serve --mode live` escape hatch.

## Health evidence

For every operational check, capture and retain:

- release, config, policy, and database schema hashes;
- PID plus process start time, launchd label/state, and account-writer-lock
  holder/generation;
- runtime mode and durable loss/closeout latches;
- last broker reconciliation time and age, coverage of every order family,
  unknown submissions, positions, filled quantity, working protective quantity,
  and uncovered quantity;
- market-data connection/age and whether required quote/bar/depth/tradability
  evidence is available;
- notification outbox pending/failed age and last successful test/delivery;
- incident state and latency sample counts separately from performance claims.

An alive PID with stale reconciliation is unhealthy. An `ACTIVE` scheduler
configuration with no healthy process is not running live.

The coordinator repeats the exact-route worker lease, PID, heartbeat, failure,
and outbox-backlog checks immediately before its entry decision on every tick.
If that independent-delivery gate becomes unhealthy, it durably transitions to
`PAUSE_NEW_ENTRIES`; account reconciliation, protection, exits, closeout, and
outbox enqueueing remain available.

## Pause, closeout, and rollback

Use the durable runtime controls; do not kill the process to pause risk:

```sh
"$PYTHON" "$LAUNCHER" pause-new-entries \
  --install-root "$INSTALL_ROOT" \
  --reason "owner pause"

"$PYTHON" "$LAUNCHER" managed-closeout \
  --install-root "$INSTALL_ROOT" \
  --reason "owner requested broker-confirmed closeout"
```

These commands do not write the live database. They create private,
hash-bound, activation-bound control requests only after proving that the
kernel account lock and database writer lease belong to the running service.
`MANAGED_CLOSEOUT` additionally requires an HMAC-SHA256 generated by the
manifest-bound runtime control authenticator; rehashing or rewriting the JSON
is not authorization. A staged/read-only launcher has no such authenticator and
therefore cannot queue autonomous closeout authority.
Their output says `queued=true, applied=false`; wait for `status` and the audit
chain to prove consumption. A rejected or expired request is quarantined and
forces an active/reconciling runtime to pause new entries.

`PAUSE_NEW_ENTRIES` preserves existing exposure management, broker
reconciliation, protection, and urgent notifications. `MANAGED_CLOSEOUT`
retains broker/order reconciliation and must not overlap another exit. A cancel
request is not a cancellation and a timer is not proof of flatness.

Only after broker-confirmed flatness, zero working/unknown orders, zero
uncovered quantity, and a drained durable notification outbox may the owner:

1. queue deactivation using the exact, latest, complete broker-flatness
   snapshot ID and phrase while the service still owns the writer lock:

   ```sh
   FLAT_SNAPSHOT=exact_broker_snapshot_id
   "$PYTHON" "$LAUNCHER" deactivate \
     --install-root "$INSTALL_ROOT" \
     --flatness-snapshot-id "$FLAT_SNAPSHOT" \
     --reason "owner verified whole-broker flatness" \
     --confirm "DEACTIVATE FULL LIVE ibkr-live-ending-3103 FLAT $FLAT_SNAPSHOT"
   ```

   The service accepts it only if the snapshot is authoritative, is strictly
   newer than the activation and last mode transition, is the latest durable
   broker snapshot, has a corresponding position-reconciliation event, and is
   no older than the signed broker-evidence limit. The CLI response is not
   deactivation proof.
2. verify `status` reports an empty control inbox, no rejected request, revoked
   authority, and `PAUSED` mode;
3. boot out and disable `com.harpcity.trader-brain-full-live-ibkr-3103`;
4. restore the old writer if still desired and if doing so cannot create a
   second account writer;
5. after the durable outbox is drained or its unresolved provider state is
   explicitly retained as incident evidence, boot out and disable
   `com.harpcity.trader-brain-full-live-ibkr-3103-notifications`;
6. leave the full-live state database and audit/outbox evidence intact;
7. retarget `current` only via the paused installer after it proves the database
   is unarmed `PAUSED`, all runtime leases are released, and the audit chain is
   intact; review the emitted release-identity migration record.

If local power/network, credentials, broker transport, storage, or data fails
while exposure exists, broker-held protection remains the first line of defense.
Treat missing protection or unresolved closeout as an incident requiring owner
attention; never claim flatness or successful rollback without newer broker
evidence.
