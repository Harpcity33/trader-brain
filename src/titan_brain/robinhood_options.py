"""Read-only Robinhood options through Codex's authenticated MCP transport.

No model turns or API inference. Codex owns OAuth storage and token refresh.
The app-server API is experimental; transport failures always fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import tomllib
from typing import Any

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
ALLOWED_TOOLS = frozenset({"get_option_chains", "get_option_instruments", "get_option_quotes"})


class OptionsUnavailable(RuntimeError):
    """Safe-to-log failure containing no upstream payload or credential."""


class RobinhoodOptions:
    def __init__(self, *, timeout: float = 45):
        self.timeout = timeout
        self.process = None
        self.messages = queue.Queue()
        self.sequence = 0
        self.thread_id = None

    def __enter__(self):
        home = Path.home()
        config_path = home / ".codex/config.toml"
        config = tomllib.loads(config_path.read_text())
        server = config.get("mcp_servers", {}).get("robinhood", {})
        if server.get("url") != ENDPOINT:
            raise OptionsUnavailable("Robinhood MCP endpoint is missing or incorrect")
        cli = home / ".local/bin/codex"
        if not cli.is_file():
            raise OptionsUnavailable("Official Codex CLI unavailable at ~/.local/bin/codex")
        args = [str(cli), "app-server", "-c", "features.apps=false"]
        for name in config.get("mcp_servers", {}):
            if name != "robinhood":
                if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    raise OptionsUnavailable("Unsupported MCP server name in local configuration")
                args += ["-c", f"mcp_servers.{name}.enabled=false"]
        args += ["-c", "mcp_servers.robinhood.enabled=true", "-c",
                 'mcp_servers.robinhood.enabled_tools=' + json.dumps(sorted(ALLOWED_TOOLS))]
        # Raw transport stderr can contain credential-bearing upstream errors.
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self._request("initialize", {
                "clientInfo": {"name": "trader_brain_paper", "version": "1.1.0"},
                "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized"})
            result = self._request("thread/start", {
                "ephemeral": True, "cwd": str(Path(__file__).resolve().parents[2]),
                "approvalPolicy": "never", "sandbox": "read-only"})
            self.thread_id = result["thread"]["id"]
            return self
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put(None)
        finally:
            self.messages.put(None)

    def _send(self, payload):
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise OptionsUnavailable("Codex MCP transport disconnected") from None

    def _request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise OptionsUnavailable("Codex MCP request timed out") from None
            if message is None:
                raise OptionsUnavailable("Codex MCP transport exited or returned invalid data")
            if message.get("method") and "id" in message:
                # Never approve arbitrary actions or interactive prompts in the daemon.
                self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "Paper runtime does not approve actions"}})
                raise OptionsUnavailable("Interactive approval required; run codex mcp login robinhood")
            if message.get("id") == request_id:
                if "error" in message:
                    raise OptionsUnavailable("Robinhood MCP request failed; check local authentication")
                return message.get("result", {})

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise OptionsUnavailable("Tool forbidden by paper-only market-data allowlist")
        result = self._request("mcpServer/tool/call", {
            "threadId": self.thread_id, "server": "robinhood", "tool": tool,
            "arguments": arguments})
        if result.get("isError"):
            raise OptionsUnavailable("Robinhood options tool returned an error")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            for part in result.get("content", []):
                if part.get("type") == "text":
                    try:
                        payload = json.loads(part["text"])
                        break
                    except (ValueError, KeyError):
                        continue
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise OptionsUnavailable("Robinhood options response has no structured data")
        return payload["data"]

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def __exit__(self, *_):
        self.close()


def validate_quote(quote: dict, instrument_id: str, now: datetime) -> dict:
    if quote.get("instrument_id") != instrument_id:
        raise OptionsUnavailable("Robinhood quote contract mismatch")
    fields = ("bid_price", "ask_price", "delta", "gamma", "theta", "vega", "implied_volatility")
    try:
        values = {key: float(quote[key]) for key in fields}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError
        stamp = datetime.fromisoformat(quote["updated_at"].replace("Z", "+00:00"))
        age = (now - stamp).total_seconds()
        if age < -30:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OptionsUnavailable("Robinhood quote is missing valid prices, Greeks, or timestamp") from None
    usable = (values["ask_price"] >= values["bid_price"] > 0 and 0 <= age <= 30)
    return {"quote_timestamp": stamp.isoformat(), "quote_age_seconds": round(age),
            "prices_present": True, "greeks_present": True, "quote_usable_for_entry": usable}


def probe_robinhood_options(underlying_price: float, *, client=None, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    if client is None:
        with RobinhoodOptions() as connected:
            return probe_robinhood_options(underlying_price, client=connected, now=now)
    chains = client.call("get_option_chains", {"underlying_symbol": "SPY"}).get("chains") or []
    chains = [c for c in chains if c and c.get("symbol") == "SPY" and c.get("id")]
    target = (now + timedelta(days=7)).date().isoformat()
    choices = sorted((d, c["id"]) for c in chains for d in c.get("expiration_dates", []) if d >= target)
    if not choices:
        raise OptionsUnavailable("Robinhood returned no upcoming SPY options chain")
    expiration, chain_id = choices[0]
    contracts = client.call("get_option_instruments", {
        "chain_id": chain_id, "expiration_dates": expiration,
        "strike_price": str(round(underlying_price / 5) * 5),
        "type": "call", "state": "active", "tradability": "tradable"}).get("instruments") or []
    contracts = [c for c in contracts if c and c.get("id") and c.get("chain_id") == chain_id
                 and c.get("expiration_date") == expiration and c.get("state") == "active"
                 and c.get("tradability") == "tradable"]
    if not contracts:
        raise OptionsUnavailable("Robinhood returned no matching active SPY options contract")
    instrument_id = contracts[0]["id"]
    rows = client.call("get_option_quotes", {"instrument_ids": [instrument_id]}).get("results") or []
    quote = next((r.get("quote") for r in rows if r and r.get("quote", {})
                  and r["quote"].get("instrument_id") == instrument_id), None)
    if not quote:
        raise OptionsUnavailable("Robinhood returned no matching options quote")
    return {"ok": True, "provider": "Robinhood", "chain_access": True, "contract_access": True,
            "quote_access": True, "symbol": "SPY", "expiration": expiration,
            **validate_quote(quote, instrument_id, now)}


def option_symbol(instrument: dict) -> str:
    """Canonical OCC identifier; keep existing paper ledgers provider-independent."""
    from decimal import Decimal
    try:
        symbol = instrument['chain_symbol']
        if not re.fullmatch(r'[A-Z0-9.]{1,6}', symbol):
            raise ValueError
        kind = {'call': 'C', 'put': 'P'}[instrument['type']]
        expiration = datetime.strptime(instrument['expiration_date'], '%Y-%m-%d')
        strike = Decimal(instrument['strike_price']) * 1000
        if not strike.is_finite() or strike <= 0 or strike != strike.to_integral_value():
            raise ValueError
        return f"O:{symbol}{expiration:%y%m%d}{kind}{int(strike):08d}"
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise OptionsUnavailable('Invalid option contract identity') from None


def normalized_snapshot(instrument: dict, quote: dict, underlying: str) -> dict:
    if (instrument.get('chain_symbol') != underlying or instrument.get('state') != 'active'
            or instrument.get('tradability') != 'tradable'
            or quote.get('instrument_id') != instrument.get('id')):
        raise OptionsUnavailable('Option instrument or quote identity mismatch')
    try:
        stamp = datetime.fromisoformat(quote['updated_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError
        return {
            'provider': 'Robinhood', 'instrument_id': instrument['id'],
            'details': {'ticker': option_symbol(instrument), 'contract_type': instrument['type'],
                        'expiration_date': instrument['expiration_date'],
                        'strike_price': instrument['strike_price'],
                        'shares_per_contract': instrument['trade_value_multiplier']},
            'last_quote': {'bid': quote['bid_price'], 'ask': quote['ask_price'],
                           'bid_size': quote['bid_size'], 'ask_size': quote['ask_size'],
                           'last_updated': int(stamp.timestamp() * 1e9), 'timeframe': 'REAL-TIME'},
            'greeks': {k: quote.get(k) for k in ('delta', 'gamma', 'theta', 'vega')},
            'implied_volatility': quote.get('implied_volatility'),
            'open_interest': quote.get('open_interest'), 'day': {'volume': quote.get('volume')},
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        raise OptionsUnavailable('Incomplete Robinhood options record') from None


def instruments(client, arguments):
    """Complete bounded pagination; never treat partial inventory as complete."""
    result, cursors = [], set()
    params = dict(arguments)
    for _ in range(20):
        data = client.call('get_option_instruments', params)
        result.extend(item for item in data.get('instruments') or [] if item)
        cursor = data.get('next')
        if not cursor:
            return result
        if cursor in cursors:
            raise OptionsUnavailable('Repeated Robinhood options cursor')
        cursors.add(cursor)
        params['cursor'] = cursor
    raise OptionsUnavailable('Robinhood options inventory incomplete')


def quoted_snapshots(client, contracts, underlying):
    snapshots = []
    for offset in range(0, len(contracts), 20):
        batch = contracts[offset:offset + 20]
        rows = client.call('get_option_quotes', {'instrument_ids': [c['id'] for c in batch]}).get('results') or []
        quotes = {r['quote']['instrument_id']: r['quote'] for r in rows
                  if r and r.get('quote') and r['quote'].get('instrument_id')}
        for contract in batch:
            if contract['id'] not in quotes:
                continue  # Ineligible candidate: never invent a quote or fill.
            snapshots.append(normalized_snapshot(contract, quotes[contract['id']], underlying))
    if contracts and not snapshots:
        raise OptionsUnavailable('Robinhood returned no requested options quotes')
    return snapshots


class PaperMarketData:
    """Explicit routing: stock endpoints on Massive, options exclusively on Robinhood."""
    def __init__(self, stocks, options_factory=RobinhoodOptions):
        self.stocks, self.options_factory = stocks, options_factory

    def previous_bar(self, ticker):
        return self.stocks.previous_bar(ticker)

    def bars(self, ticker, minutes, day):
        return self.stocks.bars(ticker, minutes, day)

    def market_status(self):
        return self.stocks.market_status()

    def market_holidays(self):
        return self.stocks.market_holidays()

    def top_movers(self, direction):
        return self.stocks.top_movers(direction)

    def option_chain(self, underlying, start_date, end_date, *, reference_price=None):
        # Bounded near-price candidate screen, not a claim of exhaustive chain coverage.
        price = float(reference_price if reference_price is not None else self.stocks.previous_bar(underlying)['c'])
        if not math.isfinite(price) or price <= 0:
            raise OptionsUnavailable('No valid underlying reference price')
        with self.options_factory() as client:
            chains = client.call('get_option_chains', {'underlying_symbol': underlying}).get('chains') or []
            contracts = []
            for chain in chains:
                if not chain or chain.get('symbol') != underlying:
                    continue
                dates = sorted(d for d in chain.get('expiration_dates') or []
                               if start_date.isoformat() <= d <= end_date.isoformat())
                if not dates:
                    continue
                step = 5 if price >= 200 else 1 if price >= 25 else .5
                center = round(price / step) * step
                for strike in (center - step, center, center + step):
                    if strike <= 0:
                        continue
                    found = instruments(client, {'chain_id': chain['id'], 'expiration_dates': ','.join(dates),
                                                 'strike_price': str(strike), 'state': 'active',
                                                 'tradability': 'tradable'})
                    contracts.extend(c for c in found if c.get('chain_id') == chain['id']
                                     and c.get('chain_symbol') == underlying and c.get('expiration_date') in dates
                                     and float(c.get('strike_price', 0)) == strike
                                     and c.get('state') == 'active' and c.get('tradability') == 'tradable')
            if not contracts:
                return []
            # Balance call/put inventory so either signal direction has candidates.
            candidates = []
            for kind in ('call', 'put'):
                eligible = [c for c in contracts if c.get('type') == kind]
                eligible.sort(key=lambda c: (abs(float(c['strike_price']) - price), c['expiration_date']))
                candidates.extend(eligible[:20])
            return quoted_snapshots(client, candidates, underlying)

    def option_snapshot(self, underlying, contract):
        match = re.fullmatch(r'O:([A-Z0-9.]{1,6})(\d{6})([CP])(\d{8})', contract)
        if not match or match[1] != underlying:
            raise OptionsUnavailable('Invalid paper position option identifier')
        expiration = datetime.strptime(match[2], '%y%m%d').date().isoformat()
        from decimal import Decimal
        strike = str(Decimal(match[4]) / 1000)
        with self.options_factory() as client:
            found = instruments(client, {'chain_symbol': underlying, 'expiration_dates': expiration,
                                         'strike_price': strike, 'type': 'call' if match[3] == 'C' else 'put',
                                         'state': 'active', 'tradability': 'tradable'})
            exact = [c for c in found if option_symbol(c) == contract]
            if len(exact) != 1:
                raise OptionsUnavailable('Exact option contract is missing or ambiguous')
            return {'results': quoted_snapshots(client, exact, underlying)[0]}
