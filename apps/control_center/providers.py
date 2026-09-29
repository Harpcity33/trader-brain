"""Adapt Robinhood's verified contract/quote fields to the existing paper engine."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
import time

from .transport import Unavailable

SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,5}$")
OCC = re.compile(r"^O:([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")


def finite(value):
    if isinstance(value, bool):
        raise Unavailable("INVALID_NUMBER")
    try:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise Unavailable("INVALID_NUMBER") from None


def symbol_for(record):
    underlying = record["chain_symbol"]
    if not SYMBOL.fullmatch(underlying) or record["type"] not in {"call", "put"}:
        raise Unavailable("CONTRACT_IDENTITY_INVALID")
    expiry = date.fromisoformat(record["expiration_date"])
    try:
        strike = Decimal(record["strike_price"]) * 1000
        if not strike.is_finite() or strike != strike.to_integral_value() or not 0 < strike < 100_000_000:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise Unavailable("STRIKE_INVALID") from None
    return f"O:{underlying}{expiry:%y%m%d}{record['type'][0].upper()}{int(strike):08d}"


def normalize(record, quote):
    """Preserve source timestamps. Never promote an old quote to 'now'."""
    if (quote.get("instrument_id") != record.get("id") or record.get("state") != "active"
            or record.get("tradability") != "tradable" or record.get("underlying_type") != "equity"
            or finite(record.get("trade_value_multiplier")) != 100):
        raise Unavailable("CONTRACT_NOT_ELIGIBLE")
    timestamp = datetime.fromisoformat(quote["updated_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise Unavailable("QUOTE_TIMESTAMP_MISSING_ZONE")
    bid, ask = finite(quote["bid_price"]), finite(quote["ask_price"])
    if bid < 0 or ask < bid or ask <= 0:
        raise Unavailable("INVALID_OPTION_BOOK")
    counts = {}
    for k in ("bid_size", "ask_size", "open_interest", "volume"):
        n = finite(quote[k])
        if n < 0 or n != int(n):
            raise Unavailable("INVALID_ACTIVITY_OR_DEPTH")
        counts[k] = int(n)
    greeks = {k: finite(quote[k]) for k in ("delta", "gamma", "theta", "vega")}
    iv = finite(quote["implied_volatility"])
    if not 0 < iv < 10 or not -1 <= greeks["delta"] <= 1:
        raise Unavailable("INVALID_GREEKS")
    return {"details": {"ticker": symbol_for(record), "contract_type": record["type"],
                        "expiration_date": record["expiration_date"],
                        "strike_price": finite(record["strike_price"]), "shares_per_contract": 100},
            "last_quote": {"bid": bid, "ask": ask, "bid_size": counts["bid_size"],
                           "ask_size": counts["ask_size"], "last_updated": int(timestamp.timestamp() * 1e9),
                           "timeframe": "REAL-TIME"},
            "greeks": greeks, "implied_volatility": iv, "open_interest": counts["open_interest"],
            "day": {"volume": counts["volume"]},
            "provenance": {"provider": "robinhood_mcp", "instrument_id": record["id"],
                           "quote_updated_at": quote["updated_at"],
                           "timeframe_basis": "Robinhood get_option_quotes tool specification"}}


class HybridFeed:
    """Massive stocks/calendar; Robinhood options only. Never falls back to paid data."""
    def __init__(self, stocks, mcp, *, quote_candidates=100):
        self.stocks, self.mcp = stocks, mcp
        self.quote_candidates = quote_candidates
        self.contracts = {}
        self.last_options_read = None
        self.last_quote_timestamp = None
        self.selection = "First two eligible expirations; up to 100 strikes nearest prior stock close. Not exhaustive."

    def bars(self, *args): return self.stocks.bars(*args)
    def previous_bar(self, *args): return self.stocks.previous_bar(*args)
    def top_movers(self, *args): return self.stocks.top_movers(*args)
    def market_status(self): return self.stocks.market_status()
    def market_holidays(self): return self.stocks.market_holidays()

    def _instruments(self, **args):
        result, cursor, seen = [], None, set()
        for _ in range(40):
            page = self.mcp.call("get_option_instruments", {**args, **({"cursor": cursor} if cursor else {})})
            records = page.get("instruments")
            if not isinstance(records, list):
                raise Unavailable("INSTRUMENT_SCHEMA_CHANGED")
            result.extend(records)
            cursor = page.get("next")
            if not cursor:
                return result
            if cursor in seen or len(result) > 6000:
                break
            seen.add(cursor)
        raise Unavailable("CONTRACT_DISCOVERY_INCOMPLETE")

    def option_chain(self, underlying, start_date, end_date):
        chains = self.mcp.call("get_option_chains", {"underlying_symbol": underlying}).get("chains")
        if not isinstance(chains, list):
            raise Unavailable("CHAIN_SCHEMA_CHANGED")
        eligible = [c for c in chains if c.get("symbol") == underlying and finite(c.get("trade_value_multiplier")) == 100]
        expiries = sorted({d for c in eligible for d in c.get("expiration_dates", [])
                           if start_date.isoformat() <= d <= end_date.isoformat()})[:2]
        records = []
        for c in eligible:
            dates = [d for d in expiries if d in c["expiration_dates"]]
            if dates:
                records.extend(self._instruments(chain_id=c["id"], expiration_dates=",".join(dates),
                                                  state="active", tradability="tradable"))
        reference = finite(self.previous_bar(underlying)["c"])
        records = [r for r in records if r.get("chain_symbol") == underlying and r.get("type") in {"call", "put"}]
        records.sort(key=lambda r: abs(finite(r["strike_price"]) - reference))
        records = records[:self.quote_candidates]
        output = []
        for offset in range(0, len(records), 20):
            batch = records[offset:offset + 20]
            ids = {r["id"]: r for r in batch}
            result = self.mcp.call("get_option_quotes", {"instrument_ids": list(ids)})
            rows = result.get("results")
            if not isinstance(rows, list):
                raise Unavailable("QUOTE_SCHEMA_CHANGED")
            for item in rows:
                quote = item.get("quote", {})
                record = ids.get(quote.get("instrument_id"))
                if not record:
                    raise Unavailable("QUOTE_IDENTITY_MISMATCH")
                try:
                    normalized = normalize(record, quote)
                except (KeyError, ValueError, Unavailable):
                    continue  # missing/invalid contracts are ineligible, never guessed
                self.contracts[symbol_for(record)] = record
                output.append(normalized)
                self.last_quote_timestamp = quote["updated_at"]
        self.last_options_read = datetime.now(timezone.utc).isoformat()
        return output

    def option_snapshot(self, underlying, contract):
        record = self.contracts.get(contract)
        if record is None:
            match = OCC.fullmatch(contract)
            if not match or match[1] != underlying:
                raise Unavailable("OPTION_SYMBOL_INVALID")
            expiry = datetime.strptime(match[2], "%y%m%d").date().isoformat()
            records = self._instruments(chain_symbol=underlying, expiration_dates=expiry,
                                        strike_price=str(Decimal(match[4]) / 1000),
                                        type="call" if match[3] == "C" else "put",
                                        state="active", tradability="tradable")
            matching = [r for r in records if symbol_for(r) == contract]
            if len(matching) != 1:
                raise Unavailable("EXACT_CONTRACT_NOT_UNIQUE")
            record = matching[0]
            self.contracts[contract] = record
        result = self.mcp.call("get_option_quotes", {"instrument_ids": [record["id"]]})
        rows = result.get("results") or []
        if len(rows) != 1:
            raise Unavailable("EXACT_QUOTE_UNAVAILABLE")
        return {"results": normalize(record, rows[0]["quote"])}
