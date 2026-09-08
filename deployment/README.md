# Full-live deployment assets

This directory contains only deployment-time templates and contracts. Building
or installing a release does not authorize trading.

The release builder creates a content-addressed, deterministic `tar.gz` with
an embedded `release-manifest.json`. It accepts only the exact 40-character
commit currently at the repository's `HEAD`, rejects any tracked worktree
change or uncommitted release input, and reads archive payloads from that
commit's Git blobs rather than mutable worktree files. The paused installer
verifies every archive member against that manifest and installs only below:

`~/Library/Application Support/Titan Momentum/full-live`

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
See
`validation/full-live/2026-09-08/OPERATIONS.md` for the owner-controlled
readiness, cutover, activation, and rollback procedure.
