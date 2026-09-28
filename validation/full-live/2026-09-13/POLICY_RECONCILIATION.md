# Existing equity policy versus staged full-live requirements

Audit date: September 13, 2026. Scope: local, read-only policy comparison;
the companion edit corrects only the September 8 proposal's classification of
the 6% daily entry-lock overlay. No configuration, risk/strategy code, broker
state, scheduler, or activation change is authorized by this document.

## Conclusion

The existing attended equity lane already has a dollar-headroom risk policy.
Its discovery evidence treats scores as ranking/context rather than standalone
entry permission or vetoes. The replacement's mandatory score floors and
percentage-overlay provenance fields are implementation requirements of that
staged replacement, not proof that the existing strategy lacked risk controls.
Automating the existing strategy does not inherently require approval of new
percentage limits or score cutoffs. Supporting the existing semantics is an
engineering alternative; this audit does not implement or activate it.

Acceptable spread and adequate displayed depth remain real existing gates.
Their exact current-lane deterministic measurement contract is not resolved by
this audit. Missing data or policy provenance cannot be treated as a passed
gate, unlimited liquidity, or authority to invent a numerical threshold.

## Evidence and authority limits

The observed active prompt is
`titan_live_attended_compat_premarket_2026-08-27_v2`.
[Its opening statement](/Users/harp/.codex/automations/robinhood-momentum-engine/automation.toml:6)
expressly says the prompt alone never authorizes an order mutation. Persisted
prompts, configuration, filenames, hashes, and AI-authored reports establish
what was recorded or installed; they do not independently establish owner
approval or current broker capability.

The [August 26 local authorization record](/Users/harp/Documents/Codex/2026-08-18/v/work/live-minute-v1.1/USER_AUTHORIZATION_2026-08-26.json:9)
quotes an owner instruction requesting a $100 daily loss cap, a $150 goal that
does not cap opportunity, and remediation for the next session. Lines 18–25
record the derived $125 post-goal floor, removal of the old $35 cap, and
dollar-headroom/risk components. **The raw original user-role transcript was
not verified in this bounded audit.** The quoted record is corroboration, not
a newly authenticated approval of every derived field. No new owner authority
is asserted here.

Earlier [V5 activation text](/Users/harp/Documents/Codex/2026-08-18/v/work/v5-live-activation/CURRENT_THREAD_AUTHORIZATION_CONFIRMATION.txt:1)
is limited to exact hashes, one-use dates, and stated non-grants. It cannot be
reused as present activation, strategy-change, or broker-mutation authority.

## Policy reconciliation

### Dollar headroom and capital

- [Active heartbeat, item 8](/Users/harp/.codex/automations/robinhood-momentum-engine/automation.toml:20): irreversible new-entry lock at broker-confirmed realized P&L of -$100 or lower; new stop-defined downside, open/pending downside, and a positive execution reserve must fit remaining headroom. The +$150 goal is aspirational, not a ceiling; after its first confirmed crossing, new risk must preserve +$125.
- [Installed sizing configuration](</Users/harp/Library/Application Support/Titan Momentum/config/titan-massive.json:44>): up to 100% unleveraged buying power and full initial allocation are represented in the installed design. This is a ceiling subject to risk/execution checks, not an allocation target or permission to spend the full account. The [same watcher's identity](</Users/harp/Library/Application Support/Titan Momentum/config/titan-massive.json:61>) is SHADOW with no trade, broker, risk-authorization, reservation, or capital-allocation authority.
- [August 29 implementation status](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/validation/IMPLEMENTATION_STATUS_2026-08-29.md:50), [September 8 policy map](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/validation/full-live/2026-09-08/POLICY_MAP.md:19), and [full-live configuration](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/config/full_live.json:36) classify the percentage overlay as staged/not applied and set its live provenance false. That is not a reason to describe the attended dollar policy as absent.

The September 8 proposal incorrectly placed `min(6%, $100)` under “Established
policy retained verbatim.” The companion correction retains the existing
-$100 boundary and identifies 6% as proposed. All other staged percentages
remain proposals; no value is approved or applied by this correction.

### Ranking, news, and A+ sizing

- [Installed configuration](</Users/harp/Library/Application Support/Titan Momentum/config/titan-massive.json:32>) has `score_is_entry_gate=false`, `fresh_news_required=false`, and `state_is_entry_gate=false`. The [read-only discovery policy implementation](</Users/harp/Library/Application Support/Titan Momentum/titan_runtime/policy.py:37>) actually bypasses those optional gates when false; it still checks its structural/discovery conditions.
- [Historical V5 prompt, lines 74–76](/Users/harp/Documents/Codex/2026-08-18/v/work/v5-direct-risk/automation.toml:74) explicitly keeps scores, news, labels, and contextual inputs descriptive; missing context remains UNKNOWN, while failed liquidity, adverse security evidence, or a contradicted thesis can reject an entry. This corroborates earlier semantics, not authority to restore V5's broader instrument/session scope.
- [Repository scoring contract](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/config/scoring.json:4) defines component semantics, labels the setup weights an initial hypothesis, and supplies no minimum entry score. Lines 40–42 preserve hard gates and make news contextual.
- [Replacement discovery configuration](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/config/full_live.json:69) instead has four unset normal/A+ threshold fields; [POLICY_MAP's score row](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/validation/full-live/2026-09-08/POLICY_MAP.md:21) calls their approval mandatory. That describes a staged architecture choice, not an established ranking-only rule. New 70/65 floors or A+ uplift are not necessary to preserve the existing strategy.

Preserving ranking-only behavior does not permit a high score to override real
structure, tradability, liquidity, risk, protection, or session failures.

### Spread, depth, and evidence

[Active heartbeat item 3](/Users/harp/.codex/automations/robinhood-momentum-engine/automation.toml:15)
requires fresh executable quotes, acceptable spread and displayed depth,
completed-bar structure, capacity, extension control, structural invalidation,
targets, and favorable reward/risk. Items 5–6 require adequate premarket
liquidity and materially smaller premarket risk with a positive reserve. The
active prompt supplies no exact spread cap, depth multiple, or quantitative
definition of “materially smaller.”

The older installed [watcher configuration](</Users/harp/Library/Application Support/Titan Momentum/config/titan-massive.json:29>)
contains 0.75% spread (75 bps) and, at line 40, a 0.15 spread-to-structural-risk
ratio. The older [pilot's spread contract](</Users/harp/Library/Application Support/Titan Momentum/config/pilots/titan-momentum-equity.json:109>)
also mentions Level 2 and 15% of entry-to-stop distance. These are evidence of
older design, not verified approval to import isolated numbers into today's
narrower attended lane. That pilot also includes options, under-$5 handling,
adds, and different premarket times, unlike the active prompt.

The [September 8 proposal](/Users/harp/Documents/Codex/2026-08-18/v/work/trader-brain/validation/full-live/2026-09-08/PROPOSED_OWNER_POLICY_2026-09-08.md:47)
offers 70/65 score floors, 25 bps spread, and 5x displayed liquidity under its
explicit NOT APPROVED status. Neither 25 bps nor 5x was traced to a verified
current-lane owner instruction. The source/side/size-unit/freshness semantics
and the applicable current-lane acceptance rule still need to be resolved;
top-of-book evidence must not be relabeled full-book depth. This unresolved
liquidity contract is distinct from requiring unrelated percentage limits.

## Minimum preservation boundary

Retain existing dollar headroom, unleveraged cash capacity, ranking-only
context, and all actual execution-critical gates. Preserve the active prompt's
above-$5 whole-share long-equity scope, no ADD/reentry, session boundaries,
reconciliation before entry, unknown-submission non-retry rule, positive
execution reserve, exact required broker reviews/confirmations, verified
protection, and closeout. [Items 1–10](/Users/harp/.codex/automations/robinhood-momentum-engine/automation.toml:10)
are the observed baseline; research and older pilot files cannot widen it.

Missing broker capability, fresh evidence, liquidity-policy provenance, or
activation evidence remains missing. This document does not clear a readiness
boolean, remove an existing live safeguard, approve autonomous execution, or
claim that any strategy will be profitable.

## Source fingerprints at audit

Raw SHA-256 values identify inspected bytes only; they are not signatures or
approval evidence. The source locations are linked above.

These fingerprints precede the separately reviewed September 13 data-watcher
maintenance. That repair changes only `start_time_et` from 04:00 to 03:55 in
the installed watcher configuration; its post-repair SHA-256 is
`baa955e8ab578024211b17a24146c2e897eedbb67f4043eff3a44780de88c8f9`.
The policy fields discussed above are unchanged.

| Source | SHA-256 |
|---|---|
| Active attended automation | `57813b0b26ce1e72d5d396e53f925c874e5ca998732970366ada5fd75464e579` |
| Installed watcher configuration | `275885b3ce8a44999ab68d2dd9ff4a49bd3edbdd18f5cc2e0f5f259fa09d90c7` |
| Installed older equity pilot | `0b1cfbd1fb9524f7dbda47a046422258c23978e396d0f9d4b150ede888128350` |
| Historical V5 prompt | `c314f4ccd2eb7174c2e25af4906b38ee876f7daad173eefe2ea93f8253bda65b` |
| August 26 local authorization record | `a8887256fa1b449d7c6cc8bdd6c804dd6f9a7a311099ea8e57b26998f29df0f8` |
