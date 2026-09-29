"""Optional Alpaca PAPER transport. No live URL, market-data upsell or AI dependency.

Not selected by the default engine. Intended for a separately authorized execution
benchmark after local paper credentials and account-level reconciliation are checked.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib import parse

from .transport import HTTPS, Unavailable, decode_json

PAPER = "https://paper-api.alpaca.markets"
OPTION = re.compile(r"^[A-Z][A-Z0-9.]{0,5}\d{6}[CP]\d{8}$")

class AlpacaPaper:
    def __init__(self, key, secret, account_id, ledger: Path, *, http=None):
        if not key or not secret or not account_id:
            raise Unavailable("ALPACA_PAPER_CREDENTIALS_REQUIRED")
        self.key, self.secret, self.account_id = key, secret, account_id
        self.http, self.ledger = http or HTTPS(), ledger
        ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, symbol TEXT NOT NULL, reserved_cents INTEGER NOT NULL, state TEXT NOT NULL, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), account TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES (1,?)", (account_id,))
            if db.execute("SELECT account FROM metadata WHERE id=1").fetchone()[0] != account_id:
                raise Unavailable("PAPER_ACCOUNT_MISMATCH")
        ledger.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.ledger, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db: yield db
        finally: db.close()

    def request(self, path, method="GET", body=None, missing_ok=False):
        if not path.startswith("/v2/") or path.startswith("//") or ".." in path:
            raise Unavailable("ALPACA_PATH_BLOCKED")
        status, _, raw = self.http.exchange(PAPER + path, method=method, body=body,
                      headers={"APCA-API-KEY-ID":self.key,"APCA-API-SECRET-KEY":self.secret})
        if status == 404 and missing_ok: return None
        if not 200 <= status < 300: raise Unavailable(f"ALPACA_PAPER_HTTP_{status}")
        return decode_json(raw) if raw else None

    def readiness(self):
        account = self.request("/v2/account")
        if (account.get("id") != self.account_id or account.get("status") != "ACTIVE"
                or account.get("trading_blocked") or account.get("account_blocked")
                or int(account.get("options_trading_level", 0)) < 2):
            raise Unavailable("ALPACA_PAPER_ACCOUNT_NOT_READY")
        return {"paper_endpoint":True,"options_level":int(account["options_trading_level"])}

    def submit_open(self, *, signal_id, symbol, limit_price, quote, now, remaining_weekly_cents):
        """Explicit PAPER order only. Reserve before HTTP; never repeat an ambiguous send.

        remaining_weekly_cents is supplied by the independent benchmark risk ledger.
        Filled exposure stays reserved until reconciliation of a closing fill; this
        conservative adapter never releases filled exposure automatically.
        """
        if not OPTION.fullmatch(symbol) or now.tzinfo is None:
            raise Unavailable("OPTION_ORDER_IDENTITY_INVALID")
        if quote.get("source") not in {"robinhood_mcp", "alpaca_opra"} or quote.get("indicative", True):
            raise Unavailable("EXECUTABLE_QUOTES_REQUIRED")
        ts = datetime.fromisoformat(quote["updated_at"].replace("Z", "+00:00"))
        if ts.tzinfo is None or not 0 <= (now-ts).total_seconds() <= 30:
            raise Unavailable("OPTION_QUOTE_STALE")
        price, ask, bid = map(Decimal, (str(limit_price), str(quote["ask"]), str(quote["bid"])))
        if (not all(x.is_finite() for x in (price, ask, bid)) or not 0 < bid <= ask <= price
                or price.quantize(Decimal(".01")) != price):
            raise Unavailable("OPTION_PRICE_INVALID")
        reserved = int(price * 10000) + 130  # 1 contract, reserve illustrative round-trip fees
        if not 0 <= remaining_weekly_cents <= 10000 or reserved > remaining_weekly_cents:
            raise Unavailable("PAPER_WEEKLY_BUDGET_EXCEEDED")
        self.readiness()
        identifier = "tbp-" + hashlib.sha256(signal_id.encode()).hexdigest()[:40]
        body = {"symbol":symbol,"qty":"1","side":"buy","type":"limit","limit_price":str(price),
                "time_in_force":"day","position_intent":"buy_to_open","client_order_id":identifier}
        fingerprint = hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT fingerprint,state,result FROM orders WHERE id=?",(identifier,)).fetchone()
            if existing:
                if existing[0] != fingerprint: raise Unavailable("IDEMPOTENCY_CONFLICT")
                return {"client_order_id":identifier,"state":existing[1],"resubmitted":False}
            reserved_total = db.execute("SELECT COALESCE(SUM(reserved_cents),0) FROM orders WHERE state NOT IN ('rejected','canceled_unfilled','expired_unfilled')").fetchone()[0]
            if reserved + reserved_total > remaining_weekly_cents:
                raise Unavailable("PAPER_PENDING_EXPOSURE_EXCEEDED")
            db.execute("INSERT INTO orders VALUES (?,?,?,?,?,NULL)",(identifier,fingerprint,symbol,reserved,"sending"))
        try:
            result = self.request("/v2/orders", "POST", body)
            if result.get("client_order_id") != identifier:
                raise Unavailable("PAPER_ORDER_ID_MISMATCH")
            state = str(result.get("status", "unknown"))
        except Exception:
            with self.db() as db: db.execute("UPDATE orders SET state='unknown' WHERE id=?",(identifier,))
            raise Unavailable("PAPER_ORDER_DELIVERY_UNKNOWN_RECONCILE") from None
        with self.db() as db:
            db.execute("UPDATE orders SET state=?,result=? WHERE id=?", (state,json.dumps(result),identifier))
        return {"client_order_id":identifier,"state":state,"resubmitted":False}

    def reconcile(self, identifier):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM orders WHERE id=?",(identifier,)).fetchone():
                raise Unavailable("UNKNOWN_CLIENT_ORDER_ID")
        result = self.request("/v2/orders:by_client_order_id?"+parse.urlencode({"client_order_id":identifier}), missing_ok=True)
        if result is None: return {"client_order_id":identifier,"state":"unknown","retry_post":False}
        if result.get("client_order_id") != identifier:
            raise Unavailable("PAPER_ORDER_ID_MISMATCH")
        state = result.get("status", "unknown")
        filled = Decimal(str(result.get("filled_qty", "0")))
        if not filled.is_finite() or not 0 <= filled <= 1:
            raise Unavailable("PAPER_FILL_QUANTITY_INVALID")
        if state in {"canceled", "expired"} and filled == 0: state += "_unfilled"
        if state == "rejected" and filled > 0: state = "rejected_with_fills"
        with self.db() as db:
            db.execute("UPDATE orders SET state=?,result=? WHERE id=?",(state,json.dumps(result),identifier))
        return {"client_order_id":identifier,"state":state,"filled_qty":str(filled),
                "filled_avg_price":result.get("filled_avg_price"),"retry_post":False}
