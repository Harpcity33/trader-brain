# Schedule Configuration

- Timezone: America/New_York
- Premarket research target: 03:55 on U.S. trading days
- Mode: configuration only; no scheduler is installed by this validation change
- Failure behavior: fail visibly and write a run manifest; never substitute fabricated or forward-filled values

A production scheduler must consult an exchange calendar, use an idempotent run ID, prevent concurrent duplicate runs, and record start/end/runtime. The Massive-backed adapter and scheduler remain required before system validation can pass.
