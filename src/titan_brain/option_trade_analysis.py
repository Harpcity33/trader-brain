"""Pure, analysis-only arithmetic for attended long-call/long-put candidates.

No broker connection, pricing forecast, execution authority, or live policy is
provided here. Scenario bids and dollar budgets must be supplied by the caller.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_CEILING, localcontext
import re
from zoneinfo import ZoneInfo


INPUT_SCHEMA = "attended_option_analysis_input_v1"
REPORT_SCHEMA = "attended_option_analysis_report_v1"
MAX_SEARCH_QUANTITY = 100_000
_NY = ZoneInfo("America/New_York")
_ZERO = Decimal(0)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_LIMITATIONS = [
    "Scenario exit bids are caller assumptions, not forecasts or executable quotes.",
    "No quote depth, liquidity, broker permissions, contract deliverable, or source provenance is verified.",
    "Budgets are caller-supplied dollar limits, not recommendations or evidence of affordability.",
    "No joint portfolio stress, correlated-position analysis, or independent commitment reconciliation is performed.",
    "Stops and modeled losses do not guarantee execution or bound losses; the full premium can be lost.",
    "This initial analyzer excludes 0DTE entries and requires a planned exit before the expiry date; an exit plan is not proof of an exit.",
    "Exercise or delivery can create additional exposure; this report does not model or authorize either.",
    "Maximum quantity is only the arithmetic maximum under supplied limits, not a recommended size.",
]


class _Invalid(ValueError):
    pass


def _fail(code: str) -> None:
    raise _Invalid(code)


def _mapping(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        _fail(f"{key.upper()}_MISSING_OR_INVALID")
    return value


def _value(parent: dict, key: str, prefix: str):
    if key not in parent or parent[key] is None:
        _fail(f"{prefix}_{key.upper()}_MISSING")
    return parent[key]


def _number(parent: dict, key: str, prefix: str, *, minimum=None, integer=False):
    value = _value(parent, key, prefix)
    code = f"{prefix}_{key.upper()}_INVALID"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(code)
    if integer and type(value) is not int:
        _fail(code)
    try:
        raw = str(value)
        if len(raw) > 160 or not _NUMBER.fullmatch(raw):
            _fail(code)
        number = Decimal(raw)
        if (not number.is_finite() or len(number.as_tuple().digits) > 128
                or abs(number.as_tuple().exponent) > 128 or number.adjusted() > 128):
            _fail(code)
    except (InvalidOperation, ValueError):
        _fail(code)
    if minimum is not None and number < minimum:
        _fail(code)
    if integer:
        if number != number.to_integral_value():
            _fail(code)
        return int(number)
    return number


def _text(parent: dict, key: str, prefix: str) -> str:
    value = _value(parent, key, prefix)
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        _fail(f"{prefix}_{key.upper()}_INVALID")
    return value.strip()


def _timestamp(parent: dict, key: str, prefix: str) -> datetime:
    value = _text(parent, key, prefix)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            _fail(f"{prefix}_{key.upper()}_INVALID")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail(f"{prefix}_{key.upper()}_INVALID")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _percent(value: Decimal, denominator: Decimal):
    if denominator <= 0:
        return None
    return _decimal_text((value * 100 / denominator).quantize(Decimal("0.000001")))


def _report() -> dict:
    return {
        "schema_version": REPORT_SCHEMA,
        "analysis_only": True,
        "live_authority": False,
        "executable": False,
        "analysis_complete": False,
        "reasons": [],
        "limitations": list(_LIMITATIONS),
        "maximum_quantity_under_supplied_limits": None,
        "requested_quantity_fits_limits": None,
        "figures": {},
        "scenario_results": [],
        "budget_checks": {},
        "search_cap_reached": False,
    }


def analyze_option_trade(payload: dict, *, now: datetime) -> dict:
    """Return fail-closed, JSON-compatible analysis without modifying any state.

    Monetary inputs accept finite decimal strings or integers, never floats or
    booleans. Percentages are rounded to six decimal places; monetary arithmetic
    is exact within the deliberately bounded numeric input representation.
    Incomplete reports never contain a maximum quantity or an execution signal.
    """
    report = _report()
    try:
        with localcontext(Context(prec=1024)):
            _analyze(payload, now, report)
    except _Invalid as exc:
        report = _report()
        report["reasons"] = [str(exc)]
    except (ValueError, TypeError, OverflowError, InvalidOperation, KeyError):
        report = _report()
        report["reasons"] = ["INPUT_INVALID"]
    return report


def _analyze(payload: dict, now: datetime, report: dict) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != INPUT_SCHEMA:
        _fail("INPUT_SCHEMA_INVALID")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail("EVALUATION_TIME_INVALID")
    now = now.astimezone(timezone.utc)
    account, contract, quote, trade, fees, budgets = (
        _mapping(payload, name) for name in ("account", "contract", "quote", "trade", "fees", "budgets")
    )
    if _text(account, "currency", "ACCOUNT") != "USD":
        _fail("ACCOUNT_CURRENCY_UNSUPPORTED")
    balances = {key: _number(account, key, "ACCOUNT") for key in (
        "equity", "cash", "settled_cash", "available_funds"
    )}
    pending = _number(account, "pending_debits_not_in_balances", "ACCOUNT", minimum=0)
    account_time = _timestamp(account, "observed_at", "ACCOUNT")
    quote_time = _timestamp(quote, "observed_at", "QUOTE")
    for prefix, stamp, ttl in (("ACCOUNT", account_time, 60), ("QUOTE", quote_time, 30)):
        age = (now - stamp).total_seconds()
        if age < 0:
            _fail(f"{prefix}_OBSERVATION_IN_FUTURE")
        if age > ttl:
            _fail(f"{prefix}_OBSERVATION_STALE")
    identity = {"con_id": _number(contract, "con_id", "CONTRACT", minimum=1, integer=True),
                "symbol": _text(contract, "symbol", "CONTRACT"),
                "right": _text(contract, "right", "CONTRACT"),
                "strike": _decimal_text(_number(contract, "strike", "CONTRACT", minimum=0)),
                "multiplier": _number(contract, "multiplier", "CONTRACT", minimum=1, integer=True),
                "currency": _text(contract, "currency", "CONTRACT")}
    if identity["right"] not in ("CALL", "PUT"):
        _fail("CONTRACT_RIGHT_UNSUPPORTED")
    if identity["currency"] != "USD":
        _fail("CONTRACT_CURRENCY_UNSUPPORTED")
    if Decimal(identity["strike"]) <= 0:
        _fail("CONTRACT_STRIKE_INVALID")
    expiry_text = _text(contract, "expiry", "CONTRACT")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expiry_text):
            _fail("CONTRACT_EXPIRY_INVALID")
        expiry = date.fromisoformat(expiry_text)
    except ValueError:
        _fail("CONTRACT_EXPIRY_INVALID")
    dte = (expiry - now.astimezone(_NY).date()).days
    if dte == 0:
        _fail("ZERO_DTE_UNSUPPORTED")
    if not 1 <= dte <= 10:
        _fail("CONTRACT_EXPIRY_OUTSIDE_ENTRY_WINDOW")
    latest_exit = _timestamp(trade, "latest_exit_at", "TRADE")
    if latest_exit <= now:
        _fail("LATEST_EXIT_NOT_IN_FUTURE")
    if latest_exit.astimezone(_NY).date() >= expiry:
        _fail("LATEST_EXIT_MUST_PRECEDE_EXPIRY_DATE")
    bid = _number(quote, "bid", "QUOTE", minimum=0)
    ask = _number(quote, "ask", "QUOTE", minimum=0)
    if ask <= 0 or bid > ask:
        _fail("QUOTE_PRICES_INVALID")
    quantity = _number(trade, "quantity", "TRADE", minimum=1, integer=True)
    if quantity > MAX_SEARCH_QUANTITY:
        _fail("TRADE_QUANTITY_EXCEEDS_ANALYSIS_CAP")
    entry = _number(trade, "entry_limit", "TRADE", minimum=0)
    if entry <= 0:
        _fail("TRADE_ENTRY_LIMIT_INVALID")
    thesis = _text(trade, "thesis", "TRADE")
    invalidation = _text(trade, "invalidation", "TRADE")
    fee = {key: _number(fees, key, "FEES", minimum=0) for key in (
        "entry_per_order", "entry_per_contract", "exit_per_order", "exit_per_contract"
    )}
    limits = {key: _number(budgets, key, "BUDGETS", minimum=0) for key in (
        "planned_loss", "stress_loss", "premium_exposure"
    )}
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 100:
        _fail("SCENARIOS_MISSING_OR_INVALID")
    parsed, names = [], set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            _fail("SCENARIO_INVALID")
        name = _text(scenario, "name", "SCENARIO")
        kind = _text(scenario, "kind", "SCENARIO")
        if name in names or kind not in ("planned", "stress", "upside"):
            _fail("SCENARIO_NAME_OR_KIND_INVALID")
        names.add(name)
        parsed.append((name, kind, _number(scenario, "exit_bid", "SCENARIO", minimum=0),
                       _text(scenario, "assumptions", "SCENARIO")))
    if not {"planned", "stress"}.issubset({item[1] for item in parsed}):
        _fail("PLANNED_AND_STRESS_SCENARIOS_REQUIRED")
    ceiling = max(_ZERO, min(balances.values()) - pending)
    multiplier = Decimal(identity["multiplier"])

    def calculate(n: int):
        entry_fee = fee["entry_per_order"] + n * fee["entry_per_contract"]
        exit_fee = fee["exit_per_order"] + n * fee["exit_per_contract"]
        total_fees = entry_fee + exit_fee
        premium = n * multiplier * entry
        losses = [max(_ZERO, n * multiplier * (entry - s[2]) + total_fees) for s in parsed]
        return {"purchase_premium": premium, "entry_fees": entry_fee, "exit_fee_reserve": exit_fee,
                "round_trip_fees": total_fees, "premium_exposure": premium + total_fees,
                "planned_loss": max(loss for loss, s in zip(losses, parsed) if s[1] == "planned"),
                "stress_loss": max(loss for loss, s in zip(losses, parsed) if s[1] == "stress")}

    def checks(figures: dict):
        return {"cash": figures["premium_exposure"] <= ceiling,
                **{key: figures[key] <= limits[key] for key in limits}}

    # A profitable scenario can offset fixed order fees as quantity increases.
    # Establish its lower bound first so the remaining feasibility search is
    # monotonic; otherwise small-quantity failures could hide valid larger sizes.
    minimum_quantity = 1
    fixed_fees = fee["entry_per_order"] + fee["exit_per_order"]
    variable_fees = fee["entry_per_contract"] + fee["exit_per_contract"]
    for _, kind, exit_bid, _ in parsed:
        if kind == "upside":
            continue
        slope = multiplier * (entry - exit_bid) + variable_fees
        if slope < 0 and fixed_fees > limits[kind + "_loss"]:
            required = (fixed_fees - limits[kind + "_loss"]) / -slope
            minimum_quantity = max(minimum_quantity, int(required.to_integral_value(rounding=ROUND_CEILING)))
    low = 0
    if minimum_quantity <= MAX_SEARCH_QUANTITY and all(checks(calculate(minimum_quantity)).values()):
        low, high = minimum_quantity, MAX_SEARCH_QUANTITY
        while low < high:
            middle = (low + high + 1) // 2
            if all(checks(calculate(middle)).values()):
                low = middle
            else:
                high = middle - 1
    figures = calculate(quantity)
    result_checks = checks(figures)
    equity = balances["equity"]
    report.update({"analysis_complete": True, "evaluated_at": now.isoformat(),
                   "account_observed_at": account_time.isoformat(), "quote_observed_at": quote_time.isoformat(),
                   "contract": {**identity, "expiry": expiry_text, "calendar_dte": dte},
                   "trade": {"quantity": quantity, "entry_limit": _decimal_text(entry),
                             "latest_exit_at": latest_exit.isoformat(), "thesis": thesis, "invalidation": invalidation},
                   "maximum_quantity_under_supplied_limits": low,
                   "requested_quantity_fits_limits": all(result_checks.values()),
                   "search_cap_reached": low == MAX_SEARCH_QUANTITY, "budget_checks": result_checks,
                   "supplied_budgets": {key: _decimal_text(value) for key, value in limits.items()},
                   "figures": {key: _decimal_text(value) for key, value in {
                       **figures, "account_equity": equity, "spend_ceiling": ceiling,
                       "pending_debits_not_in_balances": pending,
                       "requested_cash_requirement": figures["premium_exposure"]}.items()}})
    for key in ("planned_loss", "stress_loss", "premium_exposure"):
        report["figures"][key + "_equity_percent"] = _percent(figures[key], equity)
    for name, kind, exit_bid, assumptions in parsed:
        pnl = quantity * multiplier * (exit_bid - entry) - figures["round_trip_fees"]
        loss = max(_ZERO, -pnl)
        report["scenario_results"].append({"name": name, "kind": kind, "assumptions": assumptions,
            "exit_bid": _decimal_text(exit_bid), "net_pnl": _decimal_text(pnl), "loss": _decimal_text(loss),
            "premium_decline_percent": _percent(max(_ZERO, entry - exit_bid), entry),
            "loss_equity_percent": _percent(loss, equity)})
    report["reasons"] = [f"REQUESTED_QUANTITY_EXCEEDS_{key.upper()}_LIMIT"
                         for key, passes in result_checks.items() if not passes]
