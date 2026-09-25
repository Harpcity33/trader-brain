# Full-live deployment assets

This directory contains only deployment-time templates and contracts. Building
or installing a release does not authorize trading.

The release builder creates a content-addressed, deterministic `tar.gz` with
an embedded `release-manifest.json`. It accepts only the exact 40-character
commit currently at the repository's `HEAD`, rejects any tracked worktree
change or untracked repository file, and reads archive payloads from that
commit's Git blobs rather than mutable worktree files. The release output
directory must resolve outside the source repository. The paused installer
verifies every archive member against that manifest and installs only at the
exact install subtree signed into the selected release profile, currently one
of:

`~/Library/Application Support/Titan Momentum/full-live`

`~/Library/Application Support/Titan Momentum/full-live-ibkr-ending-3103`

Installation additionally requires the reviewed Git repository and exact
40-character commit as independent inputs. The installer reconstructs the
manifest from that commit and rejects an internally rehashed archive. For the
IBKR profile, the committed config pins the complete local SDK dependency
inventory; matching package names and versions with different bytes is
rejected.

Release installation and runtime writing contend on one non-configurable
per-user account lock below
`~/Library/Application Support/Titan Momentum/account-writer-locks`. The lock
location is independent of the selected release/install root.

The two launchd templates are deliberately disabled and have no `RunAtLoad`
key. One supervises only the trading coordinator; the other supervises only
the transactional notification-outbox worker. The coordinator never performs
provider delivery and never spawns the worker. The installer stages rendered
copies below the full-live install root; it never copies either file into
`~/Library/LaunchAgents`, calls `launchctl`, starts either process, or accesses
a broker. Both staged services invoke Python with `-I -S -B`, excluding
user/site initialization, environment-supplied import paths, and bytecode
writes.

The checked-in launcher is credential-neutral. A signed provider route remains
blocked unless an owner-reviewed, release-manifest-bound composition injects
the already-authorized provider client; neither plist discovers credentials.

Runtime mode is authoritative only in `state/full-live.sqlite3`, table
`runtime_identity`. Install metadata records `installed_mode: PAUSED` but is
not an activation control. When that database already exists, a release switch
requires an unarmed `PAUSED` identity, an exact recognized schema (v1, v2, or
v3), an intact audit chain, and no live writer or notification-worker lease.
Under the same fixed account lock and one immediate SQLite transaction, the
installer upgrades recognized v1/v2 schemas to v3, rebinds the identity to the
new verified release, expires every unconsumed prior-release activation,
increments the generation, and appends hash-chained migration events before
changing `current`. Partial, altered, or unknown schemas fail closed without a
pointer change. A crash between the database transaction and pointer update
makes CLI identity checks fail closed until the same paused install is rerun.
Prior-release control requests remain invalid because they are
cryptographically bound to the prior release hash.

The autonomous IBKR risk high-water ledger has a separate release identity and
is never rebound in place. During an eligible paused upgrade, after the fixed
account lock and zero-live-lease checks above, the installer validates and
checkpoints the old ledger, copies it to the content-addressed archive
`state/ibkr-risk-high-water-archive/<source-release>-<ledger-sha256>.sqlite3`,
and atomically replaces the active path with a v3 ledger bound to the new
release. The new ledger contains an immutable carry-forward row for the old
latest trading date, old global equity peak, archive digest, and source
bindings. It treats that peak as a floor. A same-session upgrade may combine a
new release-authenticated prior-day baseline with the carried intraday peak;
on a later trading day, a signed baseline below the carried peak fails closed.
An account key, masked account, or account-binding fingerprint change is never
migrated. The installer neither reads the baseline HMAC key nor fabricates a
baseline receipt, and it leaves the service stopped and `PAUSED`.

See
`validation/full-live/2026-09-08/OPERATIONS.md` for the owner-controlled
readiness, cutover, activation, and rollback procedure.
