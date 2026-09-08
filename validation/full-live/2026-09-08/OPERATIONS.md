# Titan full-live operations — September 8, 2026 target

## Status vocabulary

- **BUILT** means the source and deterministic release archive exist and pass
  tests. It does not mean they are installed.
- **INSTALLED_PAUSED** means a verified release exists below
  `~/Library/Application Support/Titan Momentum/full-live`, its durable runtime
  identity is `PAUSED` (or has not yet been initialized), and its disabled
  launchd plists are only staged under that subtree.
- **RUNNING_RECONCILE_ONLY** means the owner has explicitly started the new
  writer in its non-entry state and it is collecting fresh readiness evidence.
- **ACTIVE** means the one-use, release-bound activation record was consumed,
  the service entered `RECONCILING`, and the service itself advanced to
  `ACTIVE` only after a fresh clean broker reconciliation. Configuration text,
  installation, a loaded plist, or a running PID is not proof of this state.

As delivered from source, `config/full_live.json` keeps live entries and the
local mutation interlock disabled. It contains explicit broker,
reconciliation, staged-risk-provenance, live-evidence-provider, notification
bridge, and numeric liquidity/score blockers. That is deliberate: the current
supported Robinhood connector is attended and is not a daemon-capable
unattended mutation transport. Those blockers cannot be waived with a
confirmation phrase. Until a reviewed replacement release resolves them, the
activation command must fail closed.

## Deterministic build

From a clean, reviewed repository checkout, record the exact `HEAD` commit and
build. `--source-revision` is an assertion, not a free-form manifest label: it
must resolve to the exact current `HEAD`. The builder rejects staged or
unstaged tracked changes and any uncommitted path that would belong to the
release inventory.

```sh
PYTHON=/absolute/path/to/python3.11-or-newer
"$PYTHON" scripts/build_full_live_release.py \
  --source-root /absolute/path/to/trader-brain \
  --output-dir /absolute/path/to/release-output \
  --source-revision EXACT_40_CHARACTER_GIT_COMMIT
```

The output contains:

- `titan-full-live-<manifest-hash-prefix>.tar.gz`
- the same path plus `.sha256`
- the same path plus `.manifest.json`

The manifest schema is `titan_full_live_release_2026-09-08_v1`.
`release_manifest_hash` is SHA-256 of canonical JSON after dropping only that
field. `source_tree` is the canonical file-inventory hash. `config_hash` and
`policy_hash` use the runtime's policy hashing convention. The manifest is
excluded from its own file inventory. Repeating a build with identical content
and source revision must produce identical bytes. Payload bytes are read from
the recorded commit's Git blobs, so every packaged file digest is bound to
`source_commit`; unrelated untracked files are never packaged.

## Paused installation

Installation is intentionally separate from activation. Use the same Python
3.11+ interpreter intended for the service:

```sh
PYTHON=/absolute/path/to/python3.11-or-newer
"$PYTHON" scripts/install_full_live_paused.py \
  /absolute/path/to/titan-full-live-<hash>.tar.gz \
  --root "$HOME/Library/Application Support/Titan Momentum/full-live" \
  --python-executable "$PYTHON"
```

The installer verifies the sidecar archive hash, embedded manifest, complete
file inventory, and every file digest. It installs a content-addressed release,
updates `current`, writes `release-manifest.json`, and stages these two disabled
plists only:

`~/Library/Application Support/Titan Momentum/full-live/launchd/com.harpcity.trader-brain-full-live.plist`

`~/Library/Application Support/Titan Momentum/full-live/launchd/com.harpcity.trader-brain-full-live-notifications.plist`

The first process is the trading coordinator. The second is the only process
permitted to claim durable outbox rows and deliver them to the signed provider
route. The coordinator only enqueues; it neither sends nor starts the worker.
The two labels, argument vectors, logs, and supervision lifecycles are distinct.

The installer does not call `launchctl`, start a process, access Robinhood,
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
INSTALL_ROOT="$HOME/Library/Application Support/Titan Momentum/full-live"
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

### Exact provider-injection prerequisite

The staged coordinator and independent notification worker invoke Python as
`python -I -S -B`, so neither service loads user/site customization or an
environment-supplied import path, and neither writes bytecode into a release.

The stock `scripts/titan-full-live` launcher intentionally constructs no live
provider clients and performs no credential discovery. It therefore remains a
hard blocker for a signed production transport, Massive/Robinhood discovery
composition, or Gmail route. A production-capable release must include and
manifest-bind an owner-reviewed launcher that passes one `RuntimeComposition`
to `titan_brain.live.cli.main`. That composition must contain:

- the approved `ProductionTransport` for broker reads and mutations;
- `SupportedDiscoveryProviderComposition`, built around
  `MassiveRestStreamSource`, an authenticated Robinhood instrument reader, and
  an independently recomputing quality-evidence reader sharing the exact
  signed non-secret provider binding;
- `GmailProviderBinding` using the existing authorized account and exact signed
  implementation/authorization binding IDs.
- a runtime-only control authenticator with at least 256 bits of secret entropy,
  bound to that same signed production authorization receipt. The secret is
  never placed in the release, configuration, command line, or control request.

The executable transport, stream, candidate, instrument, quality, and
notification components must all reside in and match the same release
manifest. Tokens remain outside the release and are supplied only through the
already-authorized injected clients. Until such a launcher and matching signed
configuration are committed, rebuilt, reviewed, and installed paused,
`doctor`, `readiness`, `serve`, and `notification-worker` fail closed. Do not
patch the installed release or put a token in configuration, an environment
dump, a command line, or the repository.

## Readiness and owner-controlled cutover

Do not disable the existing account writer merely because package tests pass.
First verify any broker-held protection and establish an explicit rollback
window. The supported sequence is:

1. Run `doctor` and resolve every policy, broker-capability, authentication,
   market-data, notification, and release-integrity blocker. Numeric spread and
   depth thresholds require approved policy values; absence is not unlimited.
2. Confirm all standard equity positions/orders, option positions/orders, and
   supported advanced-order state from strictly fresh broker evidence. Any
   unknown submission or uncovered quantity blocks cutover.
3. Test the exact signed notification route with its independent worker. The
   stock launcher performs no credential discovery. A reviewed production
   release must supply a manifest-bound runtime composition that injects the
   already-authorized provider token/client and whose implementation and
   authorization binding IDs match signed configuration. Without that
   injection, the worker fails closed before claiming a provider-bound row.

   As an explicit owner action, copy, enable, bootstrap, and start only the
   notification worker first. Do not start the trading coordinator here:

   ```sh
   install -m 600 \
     "$INSTALL_ROOT/launchd/com.harpcity.trader-brain-full-live-notifications.plist" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-notifications.plist"
   launchctl enable \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live-notifications"
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live-notifications.plist"
   launchctl kickstart -k \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live-notifications"
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
4. Confirm the old heartbeat/account writer has been disabled. Preserve any
   working broker-held protection; disabling a writer is not a closeout. The
   new writer must still be stopped here so the activation commands can prove
   exclusive ownership of the account lock.
   A paused or disabled `automation.toml` is configuration evidence only. The
   `record-legacy-retirement` command additionally requires a fresh read from
   the Codex automation control plane proving the exact scheduler runtime ID,
   automation ID, matching configuration hash and status, zero active
   executions, and a query-receipt hash. This release has no supported local
   control-plane status adapter, so the stock launcher returns
   `SCHEDULER_RUNTIME_IDENTITY_UNAVAILABLE` and cannot record retirement. Do
   not substitute a file, process-list inference, or hand-written receipt.
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
   configuration must report blocked while the attended-only broker adapter,
   unresolved signed policy gates, local-only notification sink, or active
   legacy heartbeat remains. Do not edit or copy this output back as input.
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
     --confirm "ACTIVATE FULL LIVE ending-7153 $ACTIVATION_ID"
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
     "$INSTALL_ROOT/launchd/com.harpcity.trader-brain-full-live.plist" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live.plist"
   launchctl enable "gui/$(id -u)/com.harpcity.trader-brain-full-live"
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.harpcity.trader-brain-full-live.plist"
   launchctl kickstart -k \
     "gui/$(id -u)/com.harpcity.trader-brain-full-live"
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
is not authorization. The attended stock launcher has no such authenticator
and therefore cannot queue autonomous closeout authority.
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
     --confirm "DEACTIVATE FULL LIVE ending-7153 FLAT $FLAT_SNAPSHOT"
   ```

   The service accepts it only if the snapshot is authoritative, is strictly
   newer than the activation and last mode transition, is the latest durable
   broker snapshot, has a corresponding position-reconciliation event, and is
   no older than the signed broker-evidence limit. The CLI response is not
   deactivation proof.
2. verify `status` reports an empty control inbox, no rejected request, revoked
   authority, and `PAUSED` mode;
3. boot out and disable `com.harpcity.trader-brain-full-live`;
4. restore the old writer if still desired and if doing so cannot create a
   second account writer;
5. after the durable outbox is drained or its unresolved provider state is
   explicitly retained as incident evidence, boot out and disable
   `com.harpcity.trader-brain-full-live-notifications`;
6. leave the full-live state database and audit/outbox evidence intact;
7. retarget `current` only via the paused installer after it proves the database
   is unarmed `PAUSED`, all runtime leases are released, and the audit chain is
   intact; review the emitted release-identity migration record.

If local power/network, credentials, broker transport, storage, or data fails
while exposure exists, broker-held protection remains the first line of defense.
Treat missing protection or unresolved closeout as an incident requiring owner
attention; never claim flatness or successful rollback without newer broker
evidence.
