# Titan Momentum Additive Architecture

```text
                  immutable repository evidence
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
04:00 daily market study                    promoted edges (read-only)
Massive + prior-day artifacts                       │
        │                                           │
        └──────── daily strategy (expires EOD) ─────┘
                              │
               ┌──────────────┴──────────────┐
               │                             │
existing equity heartbeat            attended options heartbeat
(independent production core)         (independent, Level 2 only)
               │                             │
         exact equity review             exact option review
         + user confirmation              + user confirmation
               │                             │
       live-equity ledger              live-options ledger
               └──────────────┬──────────────┘
                              │
                     EOD R/process review
                              │
                  research observations only

TITAN_AGGRESSIVE_LAB ── paper-only ledger ── no live authority
```

The production equity heartbeat is not imported by, blocked on, or replaced by
new code. Research has no broker authority. Options failure produces options
NO TRADE only. Aggressive-paper evidence is never aggregated with live results.

## Control precedence

1. Fresh broker/account/session/tradability evidence.
2. Unknown-order and unresolved-exposure reconciliation.
3. Hard eligibility and deterministic risk gates.
4. SETUP_SCORE.
5. Instrument-specific EXECUTION_SCORE.
6. Conservative route expectancy after costs.
7. Exact broker review and exact attended confirmation.

No score can override a failed item above it. No component claims guaranteed
profit, bracket atomicity, or unattended order protection.

