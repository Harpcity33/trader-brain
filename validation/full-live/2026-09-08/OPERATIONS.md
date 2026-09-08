# Titan full-live operations — September 8, 2026 target

## Status vocabulary

- **BUILT** means the source and deterministic release archive exist and pass
  tests. It does not mean they are installed.
- **INSTALLED_PAUSED** means a verified release exists below
  `~/Library/Application Support/Titan Momentum/full-live`, its durable runtime
  identity is `PAUSED` (or has not yet been initialized), and its disabled
  launchd plist is only staged under that subtree.
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
updates `current`, writes `release-manifest.json`, and stages this disabled
plist only:

`~/Library/Application Support/Titan Momentum/full-live/launchd/com.harpcity.trader-brain-full-live.plist`

It does not call `launchctl`, start a process, access Robinhood, initialize the
state database, copy anything to `~/Library/LaunchAgents`, or modify the legacy
`Titan Momentum` runtime. If an existing full-live state database is present,
the installer will switch releases only when a read-only query proves
`runtime_identity.mode = PAUSED`.

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
3. Test both notification layers. The command below proves only durable
   outbox-to-local-JSONL delivery; it deliberately reports no user-destination
   receipt. Do not put secrets or full account identifiers in the payload:

   ```sh
   "$PYTHON" "$LAUNCHER" notification-test \
     --install-root "$INSTALL_ROOT" \
     --event-id "owner-readiness-2026-09-08"
   ```
   Separately verify the configured bridge actually delivers the same redacted
   event to the existing user destination and durably records its delivery
   receipt. A local JSONL line alone never satisfies activation readiness.
4. Confirm the old heartbeat/account writer has been disabled. Preserve any
   working broker-held protection; disabling a writer is not a closeout. The
   new writer must still be stopped here so the activation commands can prove
   exclusive ownership of the account lock.
5. Stop every account writer, then ask the installed runtime to collect a fresh
   diagnostic attestation. There is no readiness-file input: the command takes
   the kernel account lock and matching database lease, attempts an actual
   broker read, inspects the newest durable whole-broker reconciliation and
   audit chain, computes unknown and uncovered exposure, reads Massive health,
   verifies the user-destination delivery receipt, and reads the installed
   legacy heartbeat status itself:

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
7. Immediately after successful activation, copy the staged disabled plist to
   `~/Library/LaunchAgents` only as an explicit owner action, then enable,
   bootstrap, and kick-start it. The plist has `Disabled=true`, no `RunAtLoad`,
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
5. leave the full-live state database and audit/outbox evidence intact;
6. retarget `current` only via the paused installer after it proves DB mode
   `PAUSED`.

If local power/network, credentials, broker transport, storage, or data fails
while exposure exists, broker-held protection remains the first line of defense.
Treat missing protection or unresolved closeout as an incident requiring owner
attention; never claim flatness or successful rollback without newer broker
evidence.
