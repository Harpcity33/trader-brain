# Robinhood Options Eligibility — Read-only Snapshot

- Captured: `2026-08-29T15:37:26Z`
- Account: ending `7153`
- Broker account state: `active`
- Brokerage trading type: `limited_margin`
- Brokerage account type: `individual`
- Options approval returned by Robinhood: `option_level_2`
- Portfolio value: `$930.17`
- Cash: `$930.17`
- Broker buying power: `$930.17`
- Unleveraged buying power: `$930.17`
- Open equity positions: `0`
- Option positions: `0`
- Option orders returned by an unfiltered account query: `0`
- Equity orders since 2026-08-28: two terminal filled PD orders; no open equity
  position was returned.

## Eligibility decision

Robinhood's account response documents `option_level_2` as supporting long
calls and long puts. The account is active and accessible to the connected
agent, so the single-leg live attended review path is eligible, subject to a
fresh startup reconciliation and the exact order review's current checks.

Debit spreads are **not eligible** in this snapshot. They require
`option_level_3` plus an eligible margin/limited-margin account and fresh
positive multi-leg verification. The account type condition is satisfied, but
the broker returned only `option_level_2`. Robinhood returned an upgrade
application route; no upgrade was requested or performed during this
validation.

## Connector capability inventory

Read-only tools available: accounts, portfolio, option approval upgrade info,
option chains, instruments, quotes/Greeks, historicals, positions, and orders.
The connector also exposes attended `review_option_order` and
`place_option_order`; neither was called for this inventory. The review schema
supports single-leg Level 2 orders and multi-leg Level 3 orders, with order
checks (including structured alert details), quotes/Greeks, and
fees/collateral where requested. Its guide explicitly requires showing the
preview and getting user confirmation before placement.

The current review response schema does **not** expose a broker review ID,
expiry, confirmation phrase, or separate disclosure field. Titan therefore
adds a local 120-second review reference, expiry, and exact phrase bound to the
full reviewed tuple, quote timestamp, and evidence revision. The UI must state
when no disclosure was returned. This local envelope is an extra control, not a
claim of a native Robinhood token, and it never permits placement without the
connector-required explicit confirmation.

No dedicated options-buying-power field was exposed by the read-only portfolio
schema. The engine must not invent one: it records broker buying power and
unleveraged buying power, then requires the exact order review's `order_checks`
to confirm affordability for the precise tuple.

## Read-only assurance and limitations

No order was reviewed, placed, cancelled, replaced, exercised, or otherwise
mutated. This snapshot is not durable eligibility: account approval, buying
power, positions, orders, quotes, and contract tradability must be refreshed at
startup and immediately before every attended review. No market-hours contract
quote was fetched because no specific underlying/setup was selected and the
snapshot was taken on a Saturday; live contract execution quality remains to be
validated on an actual candidate during a regular-session attended review.
