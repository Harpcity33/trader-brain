# Daily Market Study Specification

The daily workflow is one standalone research run beginning at 04:00
America/New_York on authoritative U.S. trading days and completing by 06:30.
It has no broker or live-automation mutation authority.

## Required order of work

1. Read the prior trading day's immutable predictions, candidates, paper/live
   ledgers, rejections, fills, EOD review, errors, promoted-edge state, and raw
   observations.
2. Grade process and outcome separately, including timing, stops, MAE/MFE,
   exit efficiency, stock/options route performance, IV/theta, slippage, false
   positives, and missed qualifying opportunities.
3. Query Massive as primary market data and retain exact provider endpoints,
   parameters, response timestamps, and missing-data notes.
4. Evaluate SPY, QQQ, IWM, DIA when useful, sectors, breadth, trend,
   volatility, gap environment, risk-on/off behavior, index-relative momentum,
   and leadership/rotation.
5. Build the broad eligible U.S. equity universe. Professional-live hard gates
   are price strictly above $5, session volume at least 750,000 shares, fresh
   data, and current Robinhood tradability. News affects context only.
6. Supplement raw volume with 20-day median dollar volume, relative volume,
   current/premarket dollar volume, spread, depth, and historical spread
   behavior when available.
7. For serious candidates study 1-day, 5–7-day, 20–30-day, 90-day, six-month,
   and twelve-month behavior when available: gaps and continuation/fades,
   intraday volatility, opening range, VWAP, first pullback, HOD breakouts and
   failures, catalyst/post-earnings continuation, relative strength, volume,
   liquidity change, and typical MAE/MFE for matching setup IDs.
8. Use time-ordered walk-forward/out-of-sample evidence, rolling windows,
   regime segmentation, and realistic bid/ask execution. Yesterday diagnoses;
   it does not mechanically refit the model.
9. Produce one immutable daily strategy with EOD-expiring tactical adjustments,
   read-only permanent edges, and at most five standards-qualified candidates.

Every missing observation is `UNAVAILABLE`; no forward fill, inference, or
fabrication may turn missing evidence into a passing gate.

