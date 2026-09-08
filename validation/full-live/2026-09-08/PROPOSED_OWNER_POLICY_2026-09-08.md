# Proposed owner policy — one decision set for full-live activation

Status: **PROPOSED, NOT APPROVED, NOT APPLIED**
Target: September 8, 2026
Account binding: the authenticated individual limited-margin account ending
7153; the runtime must still verify the private full identifier.

This is the one completed proposal for fields that the existing approved Titan
policy does not quantify. It does not replace any established restriction,
authorize an order, enable the local mutation interlock, or activate a service.
The owner may approve this version once or return one edited replacement.

## Established policy retained verbatim

- Long, exchange-listed, broker-tradeable stock only; price strictly above $5;
  at least 750,000 session shares; whole shares; cash and unleveraged buying
  power only.
- No margin debit, options, crypto, shorting, fractional shares, ADD, re-entry,
  averaging down, stop widening, or overnight exposure.
- Attended premarket entry only from 07:00 through 09:25 ET; transition lock
  through 09:35; regular-hours entries stop at 15:30; exchange-calendar-aware
  closeout and broker-confirmed flatness remain mandatory.
- The irreversible broker-confirmed realized-P&L new-entry lock remains the
  smaller of 6% of usable equity and $100. The +$150 goal is aspirational; after
  its first confirmed crossing, new risk must preserve at least +$125.
- Exact review/confirmation requirements advertised by the selected broker and
  every stronger existing Titan confirmation requirement remain mandatory.
  Unknown submissions are never automatically retried; exits never overlap;
  protection never counts until the broker proves its exact working quantity.

## Exact proposed values

| Decision | Proposed executable value | Rationale |
|---|---:|---|
| Normal planned risk per entry | `3.00%` of broker-confirmed usable equity | Retains the checked-in staged normal cap; every cash, aggregate, daily, and stress cap below still applies. |
| A+ sizing | `disabled` | Avoids granting an unvalidated score tier more risk. Every setup uses the normal 3% cap; the staged 4% A+ value is not activated. |
| Premarket planned risk | `2.00%` | Keeps attended premarket risk below regular-hours risk while retaining the staged value. |
| Premarket stress risk | `4.00%` | Includes the mandatory positive liquidity/slippage reserve and remains below the single-trade stress ceiling. |
| Maximum single-trade stress risk | `5.00%` | Retains the staged hard per-trade stress ceiling. |
| Maximum aggregate open planned risk | `5.00%` | Prevents several individually valid plans from exceeding the staged account-level cap. |
| Maximum aggregate open stress risk | `5.00%` | No diversification credit is assumed during stress. |
| Daily new-entry lock | `min(6.00%, $100.00)` realized loss | Preserves the existing irreversible $100 ceiling and the staged percentage overlay. |
| Hard daily loss kill | `8.00%` | Retains the staged emergency value; it cannot reopen the earlier $100 new-entry lock. Only protection/exit work continues. |
| Weekly new-entry lock | `12.00%` | Retains the staged weekly limit. |
| Live drawdown review lock | `20.00%` | Retains the staged drawdown review gate; resumption requires a separately recorded owner review. |
| Normal setup score floor | `70.0 / 100` | Uses the repository's deterministic weighted score contract and excludes weaker structures. |
| Normal execution score floor | `65.0 / 100` | Keeps execution quality independently binding without letting setup score compensate for poor liquidity. |
| Maximum spread | `25.0 bps` | Measured at every order-boundary refresh as `(ask - bid) / ((ask + bid) / 2) * 10,000` from a fresh, executable NBBO. Missing/crossed/locked/stale quotes block entry. |
| Minimum displayed liquidity | `5.0x` proposed whole-share quantity | For a buy, use current executable ask size; for a sell/close, use current executable bid size. Sizes are shares, not round lots. This is SIP top-of-book evidence; Level 2 is not required or implied. |
| Protection mode | `sequential_verified`, regular hours only | The fill delta is persisted first, then a GTC regular-hours stop-market obligation is submitted and reconciled. This explicitly accepts non-atomic halt, gap, transport, and submission-to-ack risk only under the controls below. Premarket remains attended-only. |
| Independent notification route | `gmail_api_send_v1`, authenticated Gmail primary address to itself | Uses an already-owned, zero-incremental-cost route; the raw address stays outside Git and the policy binds `SHA-256(lowercase(trim(address)))`. |
| Notification failure policy | `pause_new_entries_on_failure=true` | Reconciliation, protection, and exits continue. Authentication/permanent failures pause immediately; transient delivery gets three retries at 1/2/4 seconds and pauses entries if provider acceptance is still absent after 15 seconds. |

All percentages use broker-confirmed current usable equity. When a dollar ceiling
is present, the smaller limit wins. Planned downside includes the structural
stop plus a strictly positive execution/slippage reserve. Open positions,
pending orders, manual orders, and unresolved submissions reserve aggregate
risk until newer authoritative broker evidence releases it.

## Sequential-protection operating contract

1. No new entry may start while any fill delta is uncovered or any protection,
   cancel, replacement, close, or submission outcome is unresolved.
2. The fill event and protection obligation are journaled before the protection
   call. Local preparation must start immediately; the existing 10-second broker
   acknowledgment deadline remains the outer limit, with two-second normal and
   one-second unknown-state reconciliation intervals.
3. A working stop is credited only from strictly newer broker evidence matching
   account, symbol, side, stop, time in force, session, and exact uncovered
   quantity. A local preflight is never broker-native approval.
4. Rejection or authoritative absence enters the safe-close flow. Ambiguous or
   eventually-consistent absence remains `NOT_SEEN_YET`, retains the reservation,
   and is not resent. Halts or transport outages do not permit a stop widening.
5. The mode is ineligible unless the selected broker contract supports every
   required mutation under the preserved confirmation policy. The currently
   verified Robinhood Codex route does not meet that unattended lifecycle gate.

## Notification binding and proof

The daemon must complete supported desktop OAuth using Gmail's send-only scope,
store client/token material in the configured private credential store, refresh
without exposing tokens, and record provider acceptance in the durable outbox.
The destination is the authenticated Gmail account's own primary address. One
owner-observed delivery establishes the route; a destination fingerprint or
route-version change invalidates that evidence. Provider acceptance is not a
claim that a phone displayed or the owner read the message.

## Canonical proposed patch

```json
{
  "risk": {
    "normal_planned_risk_pct": 0.03,
    "a_plus_enabled": false,
    "a_plus_planned_risk_pct": 0.03,
    "premarket_planned_risk_pct": 0.02,
    "premarket_stress_risk_pct": 0.04,
    "max_single_trade_stress_risk_pct": 0.05,
    "max_total_open_planned_risk_pct": 0.05,
    "max_total_open_stress_risk_pct": 0.05,
    "daily_new_entry_lock_pct": 0.06,
    "daily_new_entry_lock_dollars": 100.0,
    "hard_daily_loss_kill_pct": 0.08,
    "weekly_loss_lock_pct": 0.12,
    "live_drawdown_review_pct": 0.20
  },
  "discovery": {
    "minimum_setup_score": 70.0,
    "minimum_execution_score": 65.0,
    "a_plus_enabled": false
  },
  "evidence": {
    "max_spread_bps": 25.0,
    "spread_denominator": "executable_nbbo_midpoint",
    "minimum_depth_multiple": 5.0,
    "depth_source": "fresh_sip_nbbo_executable_side",
    "quote_size_unit": "shares"
  },
  "execution": {
    "protection_mode": "sequential_verified",
    "protection_scope": "regular_hours_only",
    "order_ack_timeout_seconds": 10,
    "reconcile_interval_seconds": 2,
    "unknown_reconcile_interval_seconds": 1,
    "block_new_entries_while_uncovered_or_unresolved": true
  },
  "notifications": {
    "delivery_sink": "gmail_api_send_v1",
    "destination": "authenticated_primary_address_self",
    "destination_storage": "sha256_normalized_address_only",
    "pause_new_entries_on_failure": true,
    "transient_retry_seconds": [1, 2, 4],
    "provider_acceptance_deadline_seconds": 15
  }
}
```

Approval of this proposal still cannot make an unavailable provider capability
true. Activation separately requires authenticated provider evidence, a
supported broker contract, a destination-bound delivery test, the one-writer
cutover, a clean release build, and a short-lived user-controlled activation
record.
