# Consolidated full-live policy diff — owner decisions only

Prepared for the September 8, 2026 target. This is the single policy-delta
record for the full-live release. It does not grant order authority, activate a
service, or turn an unresolved integration into an approved one.

## Baseline preserved without another approval

The replacement release must retain all of the following exactly unless the
owner later makes a separately recorded policy change:

- Account binding: the currently authenticated, active Robinhood individual
  limited-margin account ending 7153. Cash and unleveraged buying power are the
  usable-capital ceiling; margin debit is forbidden. A suffix is for display
  only and the runtime must verify the private broker account identifier.
- Instruments: exchange-listed, Robinhood-tradeable long stock only; price
  strictly above $5; at least 750,000 shares of the configured session volume;
  whole shares only. Options, crypto, shorts, fractional shares, leverage,
  averaging down, ADD, re-entry, stop widening, and overnight exposure remain
  disabled.
- Sessions: attended premarket entry only from 07:00 through 09:25 ET; no new
  entry from 09:25 through 09:35; regular-hours entry from 09:35 through 15:30;
  closeout preparation ten minutes before the exchange-verified close and
  broker-confirmed flatness five minutes before it. Holidays, early closes,
  and DST come from an exchange calendar rather than fixed UTC assumptions.
- Orders and authority: exact current review plus the connector-required
  explicit confirmation for every mutation; whole-share limit entries; no
  inferred fill, automatic retry of an unknown submission, overlapping exits,
  or sell quantity beyond fresh broker-proven capacity.
- Risk: irreversible new-entry lock at broker-confirmed realized P&L of -$100
  or lower; +$150 remains an aspirational goal, never a forced trade; after the
  first broker-confirmed +$150 crossing, new risk must preserve at least +$125;
  every calculation includes a positive execution/slippage reserve.
- Protection and closeout: every confirmed fill delta creates a durable
  protection obligation; regular-hours GTC stop-market protection counts only
  after the broker reports the exact working quantity; cancellation/fill races
  require strictly newer reconciliation; failure to prove a safe exit fails
  closed and never widens the stop.

## Engineering facts that are not owner-policy changes

The following are implementation/evidence gates. They cannot be approved by
editing a boolean and therefore are deliberately excluded from the requested
owner policy patch:

- A supported, separately authenticated daemon transport with the provider's
  actual mutation semantics and renewal behavior must exist. The currently
  authenticated Robinhood MCP advertises attended confirmation requirements;
  an owner signature cannot remove that provider/platform contract.
- Whole-account order-family coverage and exact broker-preserved client
  reference recovery must be demonstrated from authoritative, fully paginated
  responses. Empty results and endpoint names are not proof.
- Massive market evidence, Robinhood tradability evidence, route-bound
  provider notification receipts, the shared account-writer boundary, and the
  legacy-writer retirement receipt must be collected by the running release.
- `local_mutation_interlock_enabled`, `supported_unattended_mutation`, provider
  names, and readiness booleans describe verified implementation state. They
  are not policy approvals and must not be hand-edited to clear readiness.

## One approval required for the genuinely missing policy

The owner should approve one completed version of the table below through the
release's signed setup/activation flow. Blank decisions remain hard blockers.
Values seen only in unit tests are shown for provenance but are **not**
proposals or defaults.

| Policy field | Current authoritative state | Decision required |
|---|---|---|
| Percentage risk overlay | `config/risk_limits.json` stages normal 3%, A+ 4%, premarket planned 2%, premarket stress 4%, single-trade stress 5%, total-open planned/stress 5%, daily entry lock 6% with the existing $100 ceiling, hard daily kill 8%, weekly lock 12%, and live drawdown review 20%. The repository and local authorization ledger do not prove these percentages were approved for the active equity strategy. | Approve this complete staged overlay, or supply one replacement set. Until then `risk.limits_live_provenance_verified` remains false and the existing dollar controls still govern the attended lane. |
| Normal live score floors | Component definitions exist, but no approved live floors exist. The `70` setup / `65` execution values occur in synthetic tests only. | Supply approved `discovery.minimum_setup_score` and `discovery.minimum_execution_score` on a 0–100 scale. |
| A+ sizing path | No approved A+ score cutoffs exist. The `95` setup / `90` execution values occur in synthetic tests only. | Choose either **A+ disabled** (normal risk cap applies to every entry) or supply both approved A+ cutoffs and approve the A+ risk percentage above. |
| Maximum spread | The active policy says spread must be acceptable but has no number. The `25 bps` value occurs in synthetic tests only. | Supply `evidence.max_spread_bps` and specify whether it is measured against the executable NBBO midpoint at order-boundary refresh. |
| Displayed liquidity | Displayed depth is required, but no approved number or measurement contract exists. Unit-test multipliers are synthetic. Robinhood's top-of-book sizes are not Level 2. | Supply `evidence.minimum_depth_multiple` and approve the measurable source semantics: executable-side top-of-book displayed shares divided by proposed whole-share quantity. If Level 2 is mandatory, identify an already-paid source; otherwise the gate remains blocked. |
| Sequential protection residual risk | Existing policy requires immediate verified broker-held protection, but the available equity workflow has no proven atomic bracket/OCO. | Approve or reject `sequential_verified` for regular hours: no further entries while any fill delta is uncovered, durable timing of the uncovered interval, immediate exception/close workflow on failure, and explicit acceptance that halts, gaps, transport outages, and the submission-to-ack interval remain non-atomic risks. Premarket remains attended-only. |
| Independent notification route | Existing heartbeat notifications are attended/model-mediated and local JSONL is storage only. No independent production destination is approved or authenticated. | Select one already-owned zero-incremental-cost route and destination, authorize its local credential setup, and choose whether a provider-delivery failure pauses **new entries** while reconciliation/protection/exits continue. Provider acceptance is not proof of phone display or reading. |

## Effective configuration patch after approval and evidence

No literal patch is emitted while values are blank. Once the single owner
decision above is complete, the setup command must render a canonical patch
containing only these owner-controlled paths and bind its SHA-256 into the
policy and activation records:

```text
config/risk_limits.json
  <the complete approved percentage/absolute overlay>

config/full_live.json
  risk.limits_live_provenance_verified
  evidence.max_spread_bps
  evidence.minimum_depth_multiple
  discovery.minimum_setup_score
  discovery.minimum_execution_score
  discovery.a_plus_setup_score                 # or explicit A+ disabled state
  discovery.a_plus_execution_score             # or explicit A+ disabled state
  execution.protection_mode = sequential_verified
  notifications.delivery_sink
  notifications.destination_fingerprint
  notifications.route_version
  notifications.pause_new_entries_on_failure
```

Provider capability, authentication, test, and readiness fields are populated
only from machine evidence after the corresponding integration is exercised.
The separate short-lived activation confirmation may start an eligible release;
it does not constitute the policy decision above and cannot fill missing values.
