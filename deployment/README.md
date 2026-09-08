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

The launchd template is deliberately disabled and has no `RunAtLoad` key. The
installer stages a rendered copy below the full-live install root; it never
copies that file into `~/Library/LaunchAgents`, calls `launchctl`, starts the
service, or accesses a broker.

Runtime mode is authoritative only in `state/full-live.sqlite3`, table
`runtime_identity`. Install metadata records `installed_mode: PAUSED` but is
not an activation control. See
`validation/full-live/2026-09-08/OPERATIONS.md` for the owner-controlled
readiness, cutover, activation, and rollback procedure.
