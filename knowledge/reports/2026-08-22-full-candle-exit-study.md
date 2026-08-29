# Titan full-candle and later-exit decision — 2026-08-22

## Decision

The live strategy adopts the `+$150` net daily attack objective and the fixed
`-$100` account-day loss lock. It also adopts a more active opening-session
posture: mandatory discovery and planning from 09:30 through 10:30 ET, prompt
submission of fully qualified setups inside the existing authorized risk
envelope, and no default to token size when structure and remaining loss
headroom support normal authorized size.

The study does **not** authorize a blanket later-exit amendment. Existing
catastrophe stops, structural invalidations, full-quantity protection,
conditional runner management, and the no-overnight rule remain controlling.
The profit objective cannot delay an exit or override the loss lock.

## Complete source accounting

- Sealed one-minute source: `equity_bars_1m.parquet`
- Source SHA-256: `936d5cc22633e4ace7b6014424d0e2d284e8166431f0249829015a2be8165415`
- Rows scanned: 3,311,045 of 3,311,045
- Symbols: 1,992
- Sessions: 90, from 2026-04-13 through 2026-08-19
- Present ticker-sessions: 6,571
- Requested ticker-sessions: 6,578
- Observed 09:30–15:59 rows analyzed: 2,163,448
- Observed RTH rows excluded for validity: 0
- Duplicate keys, invalid OHLC rows, and timestamp conversion mismatches: 0

The source is not a dense 390-bar panel. It contains 84.42% of the possible RTH
minute slots for present ticker-sessions; 2,599 of 6,571 pairs have all 390
minutes and 399,242 minute slots are absent. Missing bars were accounted for
and were not synthesized. The seven wholly absent requested pairs are six
`ZVZZT` dates and one `ZWZZT` date, both test symbols with zero returned bars.

The provider `vw` value is outside its candle high/low on 271,122 RTH rows.
VWAP-control features therefore used a causal cumulative volume-weighted candle
typical price while retaining the original provider field unchanged.

## Prospective candle evidence

Primary cohorts were made mutually exclusive so outcome-selected leaders could
not contaminate the deployable population: 3,593 prospective gappers, 560
leader-enriched-only pairs, 623 control-only pairs, and 1,795 context-only
pairs.

For above-$5 prospective gappers:

- `ACCELERATING`, exact 15-minute forward return: 28,874 observations, median
  -0.030%, mean -0.023%, 47.7% positive.
- `BREAKOUT_ACCEPTED`, exact 15-minute return: 10,929 observations, median
  -0.046%, mean -0.053%, 48.5% positive.
- `BREAKOUT_ACCEPTED`, exact 30-minute return: median -0.095%, mean -0.223%,
  47.6% positive.
- Thirty-minute acceleration excursion was approximately symmetric: median
  MFE +1.00% versus median MAE -1.06%.
- The first acceleration state had median remaining-session MFE +3.83%, but
  median hold-to-close was only +0.07% and median peak-to-close giveback was
  3.90%.
- 40.3% of session highs occurred in the first 15 minutes and 57.9% occurred
  by 10:30 ET; 42.1% occurred later.

The same accepted-breakout label in the outcome-enriched-only leader cohort
showed median +0.476%, mean +0.864%, and 62.3% positive at 15 minutes. That
contrast demonstrates outcome-selection bias. It cannot be used as a live
prospective edge.

## Causal later-exit replay

All 37,432 source trigger rows were accounted for. A deterministic selector
allowed at most one next-bar executable trade per ticker-session, rejected
invalidated, discontinuous, or chase-ceiling entries, and produced 3,380
trades. The primary live-comparable cohort contained 1,160 prospective,
preferred, above-$5 trades. Dates were frozen chronologically into 54 train,
18 validation, and 18 test sessions. Exit signals used completed bars and the
next available open; stops included gap-through fills; same-bar ambiguity was
resolved stop-first; all exposure closed by 15:55.

Twenty-three exit rules were simulated at 0, 5, 10, and 20 basis points per
side. Candidate selection used train and validation only. At 5 basis points per
side, the validation baseline `PCS_PROXY_2OF4` had a 1%/99%-winsorized mean of
-0.077R and median hold of 11 minutes. Every genuinely later rule was worse:

| Later rule | Median hold | Validation delta vs baseline |
| --- | ---: | ---: |
| Acceleration decay after three bars | 14 min | -0.059R |
| 2.5 ATR trail | 13 min | -0.125R |
| Five-minute higher-low break | 23 min | -0.148R |
| Three-close VWAP grace | 15 min | -0.275R |
| Hold to 15:55 | 23 min | approximately -0.412R |

Baseline validation expectancy was +0.039R before costs, -0.102R at 5 basis
points per side, and -0.243R at 10 basis points per side. The worst primary
baseline result was -18.40R on ARCX after a gap through an unusually tight
0.081%-wide structural stop. A planned stop therefore cannot guarantee the
account-level loss ceiling.

No later rule passed the frozen train-and-validation eligibility test, so no
later candidate was selected for promotion and no test-period candidate was
opened after selection.

## Live interpretation

There is meaningful remaining-session upside after some acceleration events,
but the supplied candles do not identify those future peaks prospectively.
Blindly holding longer captures some outliers while giving back typical gains
and increasing gap exposure. The more aggressive live behavior is therefore:

1. search and prepare continuously during the opening attack window;
2. execute promptly when every live market, broker, structure, liquidity, and
   risk gate passes;
3. use the authorized size envelope rather than automatic token sizing;
4. retain conditional runners only while their already-defined structural and
   momentum conditions remain valid; and
5. never turn the `+$150` objective into a forced trade or a reason to violate
   the `-$100` lock.

This study isolates exit timing. It does not establish positive expectancy for
the entry strategy and does not guarantee either daily objective.

## Reproducibility

- Full candle audit: `work/candle-full-audit/audit_full_candles.py`
- Full candle report: `work/candle-full-audit/REPORT.md`
- Later-exit replay: `work/later-exit-study/analyze_later_exits.py`
- Later-exit report and manifest: `work/later-exit-study/REPORT.md` and
  `work/later-exit-study/manifest.json`

Both analyses were regenerated successfully on 2026-08-22 from the sealed
handoff. The later-exit manifest verifies every generated artifact by SHA-256.
