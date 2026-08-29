# Titan daily market study

The daily study is an independent research workflow. It starts once at 04:00 America/New_York on an authoritative U.S. trading day and must finish by 06:30. It is not a one-minute heartbeat, has no broker mutation authority, and cannot edit, pause, replace, or gate the established live equity engine.

## Workflow contract

1. Fail closed when an authoritative market calendar is unavailable, the date is a holiday/weekend/unexpected closure, or the 06:30 deadline has passed.
2. Read the prior trading day's immutable Titan artifacts first: predictions, candidates, paper/live trades, options trades, rejections, no-trade decisions, fills, executions, EOD review, operational errors, promoted edges, and unvalidated observations.
3. Classify every structured prior signal into the four process/outcome quadrants. A profitable process violation is diagnosis material, never a rule to reinforce.
4. Analyze Massive-derived evidence for the prior day, 7 days, 30 days, 90 days, six months, and twelve months where data quality permits. Six months is the minimum structural horizon; twelve months is preferred.
5. Generate regime context, preferred/avoided setups, evidence-backed daily tactical adjustments, and no more than five candidates. Never pad the list.
6. Write `research/daily-strategy/YYYY-MM-DD.md` exactly once. Existing artifacts are never replaced.

The reference implementation is `src/titan_brain/research.py`. Collectors may be supplied as callables. Each is evaluated inside a failure boundary; a failed input becomes `UNAVAILABLE` while independent stages continue. This keeps a research or data failure from propagating to live trading.

## Tactical adjustment versus promoted edge

A `DAILY_TACTICAL_ADJUSTMENT` must state its parameter, value, reason, and evidence. The engine attaches an America/New_York end-of-day expiration and explicitly records that it has no promotion effect. Promoted-edge state is read-only in this workflow. Permanent changes remain subject to the separate versioned validation and promotion process.

## Immutable write behavior

The artifact writer uses same-directory temporary storage followed by an atomic create-only link. If another process or an earlier run already created the date's artifact, the engine returns `ALREADY_COMPLETED`; it never overwrites the existing evidence. Generated artifacts are set read-only after creation.

## Failure behavior

- Missing historical Massive data: mark the affected horizon `UNAVAILABLE`; continue other stages.
- Missing or malformed prior artifact: preserve its source identity, mark analytical answers `UNAVAILABLE`, and do not infer a lesson.
- Candidate/route collector failure: publish a partial artifact with the failure named; do not manufacture candidates.
- Research failure: never pause or alter live equity trading.
- Calendar failure: skip the run rather than guessing that a weekday is open.
