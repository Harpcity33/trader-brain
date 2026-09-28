# Owner policy approval — full-autonomous IBKR regular-hours trading

Status: **OWNER APPROVED — NOT APPLIED — NOT ACTIVATED**

Recorded at: `2026-09-14T19:47:43-04:00` America/New_York

Target account: authenticated IBKR Pro No Borrow Margin account ending 3103

Approval statement: `Approved for policy`

## Approved artifact identity

- Proposal:
  `validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md`
- Exact approved proposal SHA-256:
  `cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf`
- Exact proposal content commit:
  `2ce8136c3bc5eb849fb3ac7433a22dfb581962d5`
- Proposal commit timestamp: `2026-09-14T16:20:49-04:00`

The proposal is intentionally retained byte-for-byte with its historical
`PROPOSED — NOT APPROVED — NOT APPLIED` status. This separate record binds the
owner's later approval to that exact immutable content without changing the
approved hash.

## Approved decisions

The owner approved the complete proposal, including:

- every retained financial, session, risk, reconciliation, protection, and
  no-overnight restriction in the proposal;
- premarket analysis-only operation and regular-hours entries only from 09:35
  through 15:30 America/New_York;
- a `25.0 bps` maximum entry spread based on a fresh executable NBBO midpoint;
- executable-side displayed top-of-book depth of at least `5.0x` the proposed
  whole-share quantity, interpreted in shares;
- ranking-only scores and no A+ threshold, size uplift, or risk uplift;
- sequential verified regular-hours GTC stop-market protection;
- a first-target, completed-one-minute-bar, full-position exit with
  cancel-first and strictly newer cancellation evidence;
- conservative closeout-feasibility escalation;
- a minimum `$1.00` per-order commission reserve and `$2.00` entry-lifecycle
  fee reserve, subject to separate effective-pricing proof;
- Gmail API notifications through the durable local outbox, with notification
  failure pausing new entries while reconciliation, protection, risk-reducing
  exits, mandatory closeout, and durable retries continue; and
- unattended execution only inside an authentic, unexpired, release-bound
  IBKR provider-authority contract that affirmatively supports the exact
  account, endpoint, client, place/cancel, exhaustive-read, and
  no-per-mutation-confirmation workflow without disabling order precautions.

## Explicit exclusions

This approval does **not**:

- authorize an order placement, cancellation, replacement, or modification;
- authorize loading a writer, starting either autonomous service, consuming an
  activation receipt, or otherwise activating production;
- prove or create IBKR unattended authority, exhaustive broker-read coverage,
  exact-reference recovery, effective pricing, or any provider fact;
- authorize disabling IB Gateway Read-Only API or changing any other account,
  Gateway, TWS, or precaution setting;
- approve bypassing order precautions, manual-Transmit workarounds, warning
  suppression, UI automation, or automatic retry of an unknown placement;
- identify or consent to a Gmail destination, authorize OAuth material, or
  confirm a visibly received route-bound notification test;
- approve the unverified percentage-risk overlay or substitute for its live
  provenance; or
- authorize creating private HMAC keys or issuing authority, pricing, route,
  risk, release, or activation receipts before their independent prerequisites
  are genuinely satisfied.

## Permitted implementation effect

This record retires only the documentary owner-choice blocker for the exact
values above. Those values may be applied to a new clean release configuration
only with their ordinary fail-closed evidence gates intact. In particular,
`supported_unattended_mutation: true` and
`per_mutation_user_confirmation_required: false` remain imported provider
facts, not owner-selected booleans.

The installed PAUSED release predates this approval and remains unchanged.
The combined private HMAC owner-policy/effective-pricing receipt must not be
issued until effective IBKR pricing evidence is authenticated and all of its
release, config, policy, risk, account, authorization, provider, transport,
quality, target, and commission bindings can be populated from real evidence.
