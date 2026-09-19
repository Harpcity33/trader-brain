# Proposed owner policy — full-autonomous IBKR regular-hours trading

Status: **PROPOSED — NOT APPROVED — NOT APPLIED**

Target account: authenticated IBKR Pro No Borrow Margin account ending 3103

Operating objective: genuine full-autonomous discovery, sizing, entry,
protection, reconciliation, exits, closeout, and independent notifications

Session scope: premarket analysis-only; regular-hours orders only

This is one consolidated proposal for the owner decisions that are genuinely
missing. It does not authorize an order, activate a service, alter IB Gateway,
disable Read-Only API, create a broker-support fact, install credentials, or
turn readiness blockers into configuration booleans. Until the owner approves
this policy and every separately required provider, account, release, and
delivery receipt exists, the installed desk remains PAUSED and mutation
incapable.

## Existing approved restrictions retained unchanged

- Long, exchange-listed, IBKR-tradeable U.S. stock only; price strictly above
  $5; at least 750,000 session shares; whole shares only.
- Cash and broker-confirmed unleveraged buying power only. Margin debit,
  borrowing, shorting, options, crypto, and fractional shares remain disabled.
- No ADD, re-entry, averaging down, stop widening, or overnight exposure.
- Premarket is analysis-only. No premarket or after-hours order may be placed,
  modified, or canceled merely because premarket analysis runs.
- New entries may occur only from 09:35 through 15:30 America/New_York on a
  valid U.S. trading session. Closeout preparation begins ten minutes before
  the exchange close, and broker-confirmed equity flatness is required five
  minutes before the close, including early-close sessions.
- The account-day new-entry lock remains irreversible once broker-confirmed
  realized P&L is -$100 or lower. New stop-defined downside plus all open,
  pending, uncovered, and unresolved downside, commissions, and a positive
  execution/slippage reserve must fit the remaining dollar headroom.
- Daily realized P&L must be derived from fresh current-day IBKR `reqPnL`
  evidence joined to an authenticated prior week-to-date baseline. A separate
  durable high-water ledger must monotonically preserve the account-equity
  peak used by the existing live-drawdown review rule across restarts and
  release changes. The +$150/+125 rule is independently latched from
  broker-confirmed current-day realized P&L. Missing, stale, mismatched,
  regressing, or unauthenticated risk evidence blocks new entries.
- The +$150 daily profit objective is aspirational, not guaranteed and not a
  ceiling. After the first broker-confirmed +$150 crossing, any new risk must
  preserve at least +$125.
- One account writer, durable intent before every submit, exact account and
  contract binding, exhaustive order/position/execution reconciliation, no
  overlapping exits, and strictly newer broker evidence for fills, working
  protection, cancels, and flatness remain mandatory.
- A missing, stale, partial, non-finite, conflicting, unauthenticated, or
  ambiguous fact fails closed. An `UNKNOWN` placement is unresolved exposure;
  it is reconciled by exact references and is never automatically retried. An
  ambiguous cancel is never replayed from absence or the original receipt. A
  later bounded cancel identity is allowed only after one strictly newer,
  positive whole-account broker observation proves that exact owned order is
  still working and a second fresh receipt passes the ordinary cancel gates.

This proposal does not approve the unverified percentage-risk overlay in the
checked-in staged `risk_limits.json`. Its provenance gate must still be
satisfied independently, and the approved restrictions above remain the
binding financial floor and ceiling.

## Consolidated proposed decisions

| Decision | Proposed production value | Binding interpretation |
|---|---:|---|
| Execution authority | `unattended`, conditionally | Effective only after an authentic, unexpired, release-bound IBKR provider-authority receipt proves the exact supported endpoint, account, environment, client, place/cancel scope, exhaustive reads, and no-confirmation contract. Owner policy approval alone cannot make this true. |
| Per-order confirmation | `false`, only inside the verified unattended contract | The autonomous writer may omit per-mutation user confirmation only if IBKR affirmatively supports that exact workflow without disabling order precautions. Outside that attested scope the writer remains unavailable; there is no attended fallback inside the autonomous daemon. |
| API precautions | bypass forbidden | “Bypass Order Precautions for API Orders,” global warning suppression, automatic transmit hacks, UI automation, and any equivalent precaution bypass remain prohibited. If IBKR requires such a bypass, this autonomous policy cannot activate. |
| Session | premarket `analysis_only`; regular orders `09:35–15:30` | No extended-hours orders. The same verified writer manages protection, exits, and closeout during regular hours; closeout timing follows the exchange calendar. |
| Maximum entry spread | `25.0 bps` | Computed from a fresh executable NBBO midpoint. Missing, stale, locked, or crossed quotes reject a new entry. |
| Minimum displayed liquidity | `5.0x` proposed whole-share quantity | Ask size for a buy and bid size for a normal sell, with quote size interpreted in **shares**, not round lots. This entry/quality gate must never prevent an already-required protection, risk-reducing exit, or mandatory closeout. |
| Score treatment | `ranking_only` | Scores rank otherwise eligible candidates; they never authorize a trade or override structure, completed-bar causality, extension, spread/depth, tradability, risk, session, or protection gates. |
| A+ treatment | disabled | No A+ threshold, size uplift, or risk uplift is approved. |
| Protection | `sequential_verified`, regular-hours GTC stop-market | After each broker-confirmed entry fill delta, persist the uncovered quantity, seal the exact protection plan, submit the separate stop, and credit protection only after strictly newer broker evidence proves the exact working quantity and tuple. New entries remain paused while any exposure is uncovered or unresolved; a failed protection path proceeds to a guarded safe close. Atomic bracket/OCO protection is not claimed. |
| Profit-target exit | `first_target_completed_minute_full_exit` | A fresh, aligned, completed one-minute Massive bar closing at or above the first target sealed in the owned entry plan initiates a full-position safe close. The existing working stop is canceled first; the exit is not sent until strictly newer broker evidence proves cancellation and available sell capacity. Missing or degraded target evidence pauses new entries but never blocks protection, a risk-reducing exit, or mandatory closeout. |
| Closeout feasibility | conservative obligation/latency budget | Before the mandatory closeout window, compare unresolved cancel/exit steps with the conservative per-step reconciliation budget. Pause new entries when the remaining session cannot safely absorb them; at the flat deadline, any remaining obligation is a durable incident. This is an escalation rule, not a guarantee of execution or flatness. |
| Unknown mutation | place: reconcile without retry; cancel: bounded positive-evidence recovery | Persist `UNKNOWN`, reserve full exposure/capacity, recover only through exact account/client/orderRef/orderId/permId evidence, and never treat absence as cancellation, rejection, or flatness. A placement is never resubmitted. An ambiguous cancel may produce one new bounded intent only after a strictly newer positive whole-account read proves the exact owned order is still working; that resolving receipt itself cannot dispatch, so another fresh receipt and all cancel gates are required. |
| Daily risk evidence | authenticated baseline plus broker delta and monotone high-water | Join fresh current-day `reqPnL.realizedPnL` to a canonical private HMAC receipt for the prior week-to-date baseline. Bind both the receipt and a distinct SQLite high-water ledger to the exact release, config, policy, risk, account, authorization, provider, trading date, and calendar. Rotate by trading date without restarting. The runtime verifies and consumes this evidence; it never generates or fabricates the upstream baseline. |
| Commission reserve | signed floor of at least `$1.00` per order | Capacity and risk reserve the configured per-order floor. Activation remains blocked until the account's effective pricing is confirmed and proves that floor conservative for the allowed size/route, or the owner raises it. The implementation does not derive an all-in fee from an unverified public-price illustration. |
| Entry lifecycle fee reserve | at least `$2.00` | One entry leg plus one protection/exit leg are reserved before entry. Additional contingent exit/cancel/replacement costs remain conservatively reserved when applicable. |
| Independent notifications | Gmail API plus durable local outbox | Uses the existing zero-additional-spend Gmail route. The destination stays out of signed policy and is injected through an owner-authorized binding. Production requires owner consent, a destination fingerprint, authorization receipt, and one visibly received route-bound test. |
| Delivery failure | pause new entries; exits continue | A failed/stale notification worker, invalid receipt, route mismatch, or unhealthy backlog locks new entries. Reconciliation, protection, risk-reducing exits, mandatory closeout, and durable outbox retries continue. |

## Conditional executable policy patch

The values below describe the **approved target state after evidence is
issued**. They are not instructions to edit the staged config. Exact receipt
hashes, Keychain locators, destination fingerprints, and authorization binding
IDs must come from the verified release and owner/provider ceremonies; they
must not be invented or replaced with placeholders in an activatable build.

```json
{
  "sessions": {
    "premarket_mode": "analysis_only",
    "premarket_orders_enabled": false,
    "premarket_analysis_interval_minutes": 30,
    "regular_entry_start": "09:35",
    "regular_entry_cutoff": "15:30",
    "closeout_start_minutes_before_close": 10,
    "flat_deadline_minutes_before_close": 5
  },
  "discovery": {
    "score_policy": "ranking_only",
    "minimum_setup_score": null,
    "minimum_execution_score": null,
    "a_plus_enabled": false,
    "a_plus_setup_score": null,
    "a_plus_execution_score": null
  },
  "evidence": {
    "max_spread_bps": 25.0,
    "spread_denominator": "executable_nbbo_midpoint",
    "minimum_depth_multiple": 5.0,
    "depth_source": "fresh_executable_side_top_of_book",
    "quote_size_unit": "shares"
  },
  "execution": {
    "execution_authority_mode": "unattended",
    "supported_unattended_mutation": true,
    "per_mutation_user_confirmation_required": false,
    "order_precaution_bypass_allowed": false,
    "one_account_writer_required": true,
    "durable_intent_before_submit": true,
    "automatic_retry_unknown_submission": false,
    "unknown_submission_behavior": "reconcile_without_retry",
    "ambiguous_cancel_behavior": "positive_working_evidence_then_new_bounded_intent",
    "ambiguous_cancel_absence_is_retry_evidence": false,
    "ambiguous_cancel_resolving_receipt_can_dispatch": false,
    "ibkr_daily_risk_baseline_schema": "titan_ibkr_daily_risk_baseline_2026-09-14_v1",
    "ibkr_daily_risk_baseline_relative_path": "control/ibkr/daily-risk-baseline.json",
    "ibkr_daily_risk_baseline_key_source": "macos_keychain",
    "ibkr_daily_risk_baseline_key_service": "titan-full-live-ibkr-daily-risk-baseline",
    "ibkr_daily_risk_baseline_key_account": "ibkr-live-ending-3103",
    "ibkr_risk_high_water_ledger_relative_path": "state/ibkr-risk-high-water.sqlite3",
    "protection_mode": "sequential_verified",
    "protection_order_type": "stop_market",
    "protection_time_in_force": "gtc",
    "protection_market_hours": "regular_hours",
    "block_new_entries_while_unprotected_or_unresolved": true,
    "minimum_commission_reserve_per_order_dollars": 1.0,
    "minimum_entry_lifecycle_fee_reserve_dollars": 2.0
  },
  "exits": {
    "target_exit_mode": "first_target_completed_minute_full_exit",
    "target_index": 0,
    "target_trigger": "fresh_aligned_completed_one_minute_close_at_or_above_target",
    "quantity": "full_broker_confirmed_sellable_position",
    "cancel_working_sells_before_exit": true,
    "require_strictly_newer_cancel_evidence": true,
    "deadline_feasibility_gate": true
  },
  "notifications": {
    "delivery_sink": "gmail_api",
    "provider": "gmail",
    "provider_composition_id": "titan.gmail_api.rfc2822.oauth_injected.v1",
    "durable_outbox_required": true,
    "owner_destination_consent_required": true,
    "route_bound_visible_test_required": true,
    "pause_new_entries_on_delivery_failure": true,
    "continue_reconciliation_protection_exits_and_closeout": true,
    "suppress_scan_chatter": true,
    "redact_account_to_last4": true,
    "additional_paid_services": 0
  }
}
```

`supported_unattended_mutation: true` and
`per_mutation_user_confirmation_required: false` are facts imported from a
verified provider-authority receipt, not owner-selected toggles. The authority
receipt must additionally prove Read-Only API is disabled by the owner,
No-Borrow Margin account scope, exhaustive standard/advanced/option order and
execution visibility, exact-reference recovery, regular-hours-only stock
scope, daemon place/cancel support, and that the external-market-data workflow
does not require manual transmit or a precaution bypass. Any contradiction,
expiry, release/config mismatch, API version change, session reauthentication,
or account mismatch revokes writer availability.

## Owner approval requested

One approval may accept or revise the numeric and operational decisions in the
table: 25 bps, 5x top-of-book in shares, ranking-only/A+ disabled, a
first-target completed-minute full exit, $1 per order and $2 entry-lifecycle
fee reserve, sequential verified GTC stop protection, conservative
closeout-feasibility escalation, and Gmail delivery/failure behavior. The
owner must separately identify and consent to the Gmail destination and
confirm the visible delivery test. Gmail delivery is at-least-once; a crash
after provider acceptance but before the local receipt commit can produce a
duplicate alert.

Approval does **not** waive the external gates. Full-autonomous activation
remains unavailable until IBKR provides the exact supported unattended
confirmation contract; the owner makes the required account-setting decision;
whole-account reads and exact-reference recovery pass; signed authority,
policy, risk-provenance, route, release, and activation receipts all match; and
a fresh broker reconciliation proves the account safe. If IBKR only supports
manual Transmit or requires disabling precautions, the system stays PAUSED
rather than silently reverting to an attended pilot or weakening controls.
