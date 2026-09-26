# Paper runtime verification and deployment boundaries

This update remains PAPER ONLY. No OpenAI API calls, brokerage credentials, broker writes, or paid-model dependencies are added. Legacy live automation files and ChatGPT scheduled tasks are unchanged.

## What changed

- Actual mover-discovery method added; the old method call silently fell back because its implementation was missing.
- Both open positions now share one weekly full-premium risk budget. The 10% allowance is based on Monday starting equity and does not expand on midweek profits. Planned stop risk is not treated as a guaranteed maximum loss.
- Closed regular-session candles only, candle-end timestamps, 180-second signal expiry, required VWAP observations, and finite numeric validation.
- Fresh REAL-TIME option quotes, all required Greeks/IV, displayed depth, and standard 100-share contract multipliers are validated. Exact contract is rechecked before simulation. No silent use of delayed quotes or incomplete chains.
- Entry cutoff follows the earlier holiday close where applicable. Entries stop ten minutes before close; unresolved/stale positions remain recorded and are flagged instead of inventing exits.
- Paper state has an exclusive process lock and private, atomic, fsynced writes. Trade/send intent is durable before email. Ambiguous SMTP delivery is marked unknown rather than blindly duplicated; unresolved BUY delivery blocks subsequent entries pending review.
- Simulated entry/exit slippage and illustrative $0.65-per-contract-per-side fees are explicit. No actual broker fee schedule is asserted.
- Outcome reports distinguish recorded data from unknown coverage. No-trade days no longer receive an automatic F; unvalidated quality grades remain UNASSESSED. Lane statistics are separate. No autonomous strategy-learning promotion is claimed.
- Installer selects the actual Python 3.11+ interpreter and parses local settings rather than executing secret-file contents as shell code.

## Automated verification

GitHub Actions runs the complete unit suite, offline runtime acceptance tests, clean-source structural validation, and configuration check on standard Ubuntu and macOS runners. macOS also checks installer shell syntax. It does not install launchd on the hosted runner, use real market credentials, call an AI API, or send real email.

Offline acceptance tests cover discovery, candle freshness/completion, non-finite data, option depth/multiplier/Greek requirements, overlapping weekly risk, fee reserves, stale exits, market closures, entry cutoff, durable ledger/email ordering, failed sends, concurrent processes, corrupt state, and a synthetic full paper-entry path.

## Still must be verified on the actual host

A green CI run does not prove that the user's Mac has pulled this revision or restarted its launchd process. Record the actual local revision, running PID, and a new log line with runtime_version=paper-v1.1-safety before claiming deployment.

The earlier SPY previous-close test did not prove real-time OPTIONS quotes or Greeks are entitled. The revised doctor explicitly inspects option snapshot access; market-open validation must still establish actual quote freshness and service timing. No subscription upgrade is authorized.

Premarket output is a deterministic prior-session screen collected from 07:00 onward and delivered after 08:00, not an hour-long GPT research process. Comprehensive catalyst research, validated A-F quality scoring, exhaustive missed-opportunity replay, subscription-cost allocation, and statistical trading-edge validation are not completed by this patch.

This release must not be described as a profitable, proven, or fully verified live autonomous trader. Its state and sample results are simulated; no real-money authority is granted.
