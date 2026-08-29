# Rollback Procedure

1. Leave `robinhood-momentum-engine` ACTIVE.
2. Pause or delete only `robinhood-options-momentum-engine` if its independent
   lane misbehaves. Reconcile Robinhood first; never infer that exposure is flat.
3. Pause or delete only `titan-daily-market-study` if research fails. Existing
   immutable artifacts and live equity behavior remain untouched.
4. If the additive equity risk overlay must be reverted, restore the archived
   automation snapshot recorded before this upgrade while preserving its
   status, schedule, target task, and exact attended controls. Verify the remote
   SHA-256 and app-rendered definition after restoration.
5. Never delete historical ledgers, studies, reviews, observations, commits, or
   broker evidence during rollback.

Rollback is configuration-only. No rollback step may cancel or place a broker
order without its own current review and explicit confirmation.

