# Failure-Isolation Contract

| Failure | Required result | Equity live core effect |
| --- | --- | --- |
| 04:00 study fails | Research unavailable or partial artifact with unavailable fields | None |
| Authoritative calendar unavailable | Study stops fail-closed | None |
| Massive history gap | Mark exact fields UNAVAILABLE | None |
| Option chain/quote failure | OPTIONS NO TRADE | None |
| Options eligibility unresolved | OPTIONS NO TRADE | None |
| Option review expires or tuple changes | Fresh review and confirmation required | None |
| Unknown option submission | Never retry; reconcile strictly newer evidence | None |
| Aggressive-lab error | Paper experiment stops only | None |

The existing `robinhood-momentum-engine` remains a separate process and may not
import, depend on, pause for, or be replaced by any new research/options
component. Rollback disables only the new automation IDs and reverts only the
additive deterministic-risk prompt section.

