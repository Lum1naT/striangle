"""Deterministic event replay and paper accounting. No I/O or model calls here.

All contextual facts enter in recorded receipt order. Signals from a closed bar
can fill only on a later book observation, never at that bar's historical open.
"""
from decimal import Decimal
from statistics import mean

from .configuration import STRATEGIES, dec

ZERO = Decimal(0)


def initial_state(config, strategies=STRATEGIES):
    return {"market": {"closes": [], "tape": {}, "liquidations": {}}, "wallets": {
        name: {"cash": str(config["capital"]), "qty": "0", "cost_basis": "0", "entry": "0",
               "equity": str(config["capital"]), "peak": str(config["capital"]), "max_dd": 0,
               "fees": "0", "closed_trades": 0, "wins": 0, "gross_profit": "0", "gross_loss": "0",
               "round_pnl": "0", "day": "", "day_start": str(config["capital"]), "halted": "", "pending": None}
        for name in strategies}, "curve": [], "last_id": 0}


def rsi(closes, period):
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes[-period-1:-1], closes[-period:])]
    up = sum(max(x, 0) for x in changes)
    down = -sum(min(x, 0) for x in changes)
    return 50 if up == down == 0 else 100 if down == 0 else 100 - 100 / (1 + up / down)


def features(market, at, config):
    closes = market.get("closes", [])
    result = {"fast_sma": mean(closes[-config["fast"]:]) if len(closes) >= config["fast"] else None,
              "slow_sma": mean(closes[-config["slow"]:]) if len(closes) >= config["slow"] else None,
              "rsi": rsi(closes, config["rsi_period"]), "book_age": None, "imbalance": None,
              "spread_bps": None, "flow_imbalance": None, "funding_rate": None,
              "open_interest": None, "liquidated_longs_usd": 0, "liquidated_shorts_usd": 0,
              "news_score": None, "news_summary": "", "news_sources": [], "assessment_id": None,
              "heatmap": None, "missing": []}
    book = market.get("book")
    if book:
        bids, asks = book["bids"], book["asks"]
        age = at - market["book_at"]
        result["book_age"] = age
        if 0 <= age <= config["max_book_age_seconds"]:
            bid, ask = float(bids[0][0]), float(asks[0][0])
            b = sum(float(p) * float(q) for p, q in bids)
            a = sum(float(p) * float(q) for p, q in asks)
            result.update(imbalance=(b-a)/(b+a) if b+a else 0, spread_bps=(ask-bid)/((ask+bid)/2)*10000)
    tape = [v for t, v in market.get("tape", {}).items() if at - 60 <= float(t) <= at]
    buys, sells = sum(v[0] for v in tape), sum(v[1] for v in tape)
    if buys + sells:
        result["flow_imbalance"] = (buys-sells)/(buys+sells)
    deriv = market.get("derivatives", {})
    if 0 <= at - deriv.get("at", -1e20) <= 60:
        result.update(funding_rate=deriv.get("funding_rate"), open_interest=deriv.get("open_interest"))
    for t, v in market.get("liquidations", {}).items():
        if at - 60 <= float(t) <= at:
            result["liquidated_longs_usd"] += v[0]
            result["liquidated_shorts_usd"] += v[1]
    assessment = market.get("assessment", {})
    if 0 <= at - assessment.get("at", -1e20) <= config["ai_max_age_seconds"]:
        result.update(news_score=assessment["score"], news_summary=assessment["summary"],
                      news_sources=assessment["sources"], assessment_id=assessment.get("id"))
    heatmap = market.get("heatmap", {})
    if 0 <= at - heatmap.get("at", -1e20) <= 300:
        result["heatmap"] = heatmap.get("levels", [])
    for field in ("imbalance", "flow_imbalance", "funding_rate", "news_score"):
        if result[field] is None:
            result["missing"].append(field)
    return result


def signal(strategy, f, holding, config):
    if f["slow_sma"] is None:
        return "hold", f"Warming up: need {config['slow']} completed, consecutive 1-minute candles."
    rising = f["fast_sma"] > f["slow_sma"]
    if holding:
        if strategy == "rsi":
            return ("sell", "RSI reached the exit threshold.") if f["rsi"] is not None and f["rsi"] >= config["overbought"] else ("hold", "RSI exit has not triggered.")
        if not rising:
            return "sell", "Fast average fell below the slow average."
        if strategy == "ai_trend" and f["news_score"] is not None and f["news_score"] < -config["ai_min_score"]:
            return "sell", "Fresh news assessment is adverse to the open position."
        return "hold", "Trend remains positive; protective exits remain active."
    if strategy == "rsi":
        return ("buy", "RSI is below the entry threshold.") if f["rsi"] is not None and f["rsi"] <= config["oversold"] else ("hold", "RSI entry has not triggered.")
    if not rising:
        return "hold", "Fast average is not above the slow average."
    if strategy == "trend":
        return "buy", "Fast average is above the slow average."
    if f["missing"]:
        return "hold", "AI strategy waiting for fresh inputs: " + ", ".join(f["missing"]) + "."
    if config["require_heatmap"] and f["heatmap"] is None:
        return "hold", "Estimated liquidation heatmap is required but unavailable."
    if f["news_score"] < config["ai_min_score"]:
        return "hold", "News assessment does not support an entry. The score is not a win probability."
    if f["imbalance"] < config["min_imbalance"] or f["flow_imbalance"] < -0.1:
        return "hold", "Order-book depth or executed flow does not support an entry."
    if f["funding_rate"] > config["max_funding_rate"]:
        return "hold", "Derivatives funding exceeds the crowding threshold."
    # Recent forced selling is contextual confirmation; very large opposing
    # forced buying is a reason to wait, rather than chase a short squeeze.
    if f["liquidated_shorts_usd"] > max(1000000, f["liquidated_longs_usd"] * 3):
        return "hold", "Recent short liquidations suggest a crowded squeeze; entry deferred."
    if config["require_heatmap"] and f["heatmap"]:
        price = f["fast_sma"]
        near_above = sum(float(x["weight"]) for x in f["heatmap"] if price < float(x["price"]) <= price * 1.02)
        near_below = sum(float(x["weight"]) for x in f["heatmap"] if price * 0.98 <= float(x["price"]) < price)
        if near_below > near_above * 2:
            return "hold", "Estimated liquidation concentration below price exceeds the configured entry filter."
    return "buy", "Positive trend, supportive depth and flow, acceptable funding and supportive news."


def equity(wallet, bid, config):
    return dec(wallet["cash"]) + dec(wallet["qty"]) * dec(bid) * (1-dec(config["fee_bps"])/10000) * (1-dec(config["slippage_bps"])/10000)


def mark(wallet, bid, at, config):
    current = equity(wallet, bid, config)
    wallet["equity"] = str(current)
    wallet["peak"] = str(max(dec(wallet["peak"]), current))
    drawdown = float((dec(wallet["peak"]) - current) / dec(wallet["peak"]) * 100)
    wallet["max_dd"] = max(wallet["max_dd"], drawdown)
    day = str(int(at // 86400))
    if wallet["day"] != day:
        wallet["day"], wallet["day_start"] = day, str(current)
        if wallet["halted"] == "daily loss limit":
            wallet["halted"] = ""
    if drawdown >= config["max_drawdown_pct"]:
        wallet["halted"] = "maximum drawdown limit"
    elif current <= dec(wallet["day_start"]) * (1-dec(config["daily_loss_pct"])/100):
        wallet["halted"] = wallet["halted"] or "daily loss limit"


def order_budget(wallet, config):
    risk_fraction = dec(config["risk_per_trade_pct"]) / 100
    estimated_loss = dec(config["stop_pct"])/100 + 2*(dec(config["fee_bps"])+dec(config["slippage_bps"]))/10000
    return max(ZERO, min(dec(wallet["cash"]) * dec(config["allocation_pct"])/100,
                         dec(wallet["equity"]) * risk_fraction / estimated_loss))


def simulate_fill(wallet, side, book, config, at, reason):
    """Consume recorded levels; cap participation and retain any unfilled exit."""
    levels = book["asks" if side == "buy" else "bids"]
    slip, fee_rate = dec(config["slippage_bps"])/10000, dec(config["fee_bps"])/10000
    capacity = sum(dec(q) for _, q in levels) * dec(config["max_participation_pct"])/100
    if side == "buy":
        budget = order_budget(wallet, config)
        # Worst displayed price avoids overspending when consuming deeper levels.
        quantity = min(capacity, budget / (dec(levels[-1][0]) * (1+slip) * (1+fee_rate)))
    else:
        quantity = min(capacity, dec(wallet["qty"]))
    if quantity <= 0:
        return None
    left, gross = quantity, ZERO
    for price, size in levels:
        take = min(left, dec(size))
        gross += take * dec(price)
        left -= take
        if left <= 0:
            break
    quantity -= left
    if quantity <= 0:
        return None
    gross *= 1+slip if side == "buy" else 1-slip
    fee, pnl = gross*fee_rate, None
    wallet["fees"] = str(dec(wallet["fees"])+fee)
    if side == "buy":
        wallet.update(cash=str(dec(wallet["cash"])-gross-fee), qty=str(quantity),
                      cost_basis=str(gross+fee), entry=str(gross/quantity), round_pnl="0")
    else:
        old_qty = dec(wallet["qty"])
        cost = dec(wallet["cost_basis"])*quantity/old_qty
        pnl = gross-fee-cost
        remainder = old_qty-quantity
        wallet.update(cash=str(dec(wallet["cash"])+gross-fee), qty=str(remainder),
                      cost_basis=str(dec(wallet["cost_basis"])-cost), round_pnl=str(dec(wallet["round_pnl"])+pnl))
        if remainder == 0:
            trip = dec(wallet["round_pnl"])
            wallet["closed_trades"] += 1
            wallet["wins"] += int(trip > 0)
            wallet["gross_profit"] = str(dec(wallet["gross_profit"])+max(ZERO, trip))
            wallet["gross_loss"] = str(dec(wallet["gross_loss"])+max(ZERO, -trip))
    return {"at": at, "side": side, "quantity": str(quantity), "price": str(gross/quantity), "fee": str(fee),
            "pnl": str(pnl) if pnl is not None else None, "reason": reason,
            "details": {"partial": side == "sell" and dec(wallet["qty"]) > 0, "model": "recorded_depth_with_participation_and_adverse_slippage"}}


def step(state, event, config, *, paused=False, flatten=False, execution_now=None, execute=True):
    """Return audit records and fills while updating the serializable state."""
    at, kind, body = event["at"], event["kind"], event["payload"]
    if event["id"] <= state["last_id"]:
        return [], []
    state["last_id"] = event["id"]
    market, decisions, fills = state["market"], [], []
    now = at if execution_now is None else execution_now
    fresh_execution = 0 <= now-at <= config["max_book_age_seconds"]
    if kind == "trade":
        key = str(int(at))
        bucket = market["tape"].setdefault(key, [0, 0])
        bucket[0 if body["taker_buy"] else 1] += float(body["price"])*float(body["quantity"])
        market["tape"] = {t: v for t, v in market["tape"].items() if float(t) >= at-60}
    elif kind == "liquidation":
        bucket = market["liquidations"].setdefault(str(int(at)), [0, 0])
        bucket[0 if body["position_side"] == "long" else 1] += float(body["price"])*float(body["quantity"])
        market["liquidations"] = {t: v for t, v in market["liquidations"].items() if float(t) >= at-60}
    elif kind == "flow":
        market["tape"][str(int(at))] = [float(body["buy_notional"]), float(body["sell_notional"])]
        market["tape"] = {t: v for t, v in market["tape"].items() if float(t) >= at-60}
    elif kind in ("derivatives", "assessment", "heatmap"):
        market[kind] = body | {"at": at, "id": event["id"]}
    elif kind == "gap":
        market.pop("book", None)
        market["closes"] = []
        market["tape"] = {}
        for wallet in state["wallets"].values():
            wallet["pending"] = None
    elif kind == "book":
        market["book"], market["book_at"] = body, at
        bid, ask = dec(body["bids"][0][0]), dec(body["asks"][0][0])
        spread = (ask-bid)/((ask+bid)/2)*10000
        for name, wallet in state["wallets"].items():
            if not execute:
                # Live balances and protective exits use the execution venue's
                # actual book, fills and native orders in live.py.
                continue
            mark(wallet, bid, at, config)
            qty = dec(wallet["qty"])
            exit_reason = ""
            if qty > 0:
                if flatten:
                    exit_reason = "Operator requested flatten and stop."
                elif wallet["halted"]:
                    exit_reason = wallet["halted"]
                elif bid <= dec(wallet["entry"])*(1-dec(config["stop_pct"])/100):
                    exit_reason = "Stop loss triggered."
                elif bid >= dec(wallet["entry"])*(1+dec(config["take_pct"])/100):
                    exit_reason = "Take profit triggered."
            pending = wallet.get("pending")
            if exit_reason and (not pending or pending["action"] != "sell"):
                pending = wallet["pending"] = {"action": "sell", "at": at, "event_id": event["id"], "reason": exit_reason}
                decisions.append({"strategy": name, "event_id": event["id"], "at": now, "action": "sell", "reason": exit_reason,
                                  "features": {"bid": str(bid), "entry": wallet["entry"], "equity": wallet["equity"], "max_drawdown_pct": wallet["max_dd"]}})
            if pending and pending["action"] == "buy" and (paused or flatten or wallet["halted"] or now-pending["at"] > config["max_signal_age_seconds"]):
                decisions.append({"strategy": name, "event_id": event["id"], "at": now, "action": "hold", "reason": "Pending entry cancelled: operator controls, loss limit or expired signal.", "features": {"signal_age_seconds": now-pending["at"], "halted": wallet["halted"]}})
                wallet["pending"] = None
                pending = None
            if execute and fresh_execution and pending and event["id"] > pending["event_id"]:
                if pending["action"] == "buy" and spread > dec(config["max_spread_bps"]):
                    decisions.append({"strategy": name, "event_id": event["id"], "at": now, "action": "hold", "reason": "Entry cancelled: spread widened beyond its limit.", "features": {"spread_bps": float(spread)}})
                    wallet["pending"] = None
                    continue
                fill = simulate_fill(wallet, pending["action"], body, config, now, pending["reason"])
                if fill:
                    fill["strategy"] = name
                    fills.append(fill)
                    if pending["action"] == "buy" or dec(wallet["qty"]) == 0:
                        wallet["pending"] = None
                    mark(wallet, bid, at, config)
    elif kind == "candle":
        opened = body["opened_at"]
        previous = market.get("last_opened")
        if previous is not None and opened <= previous:
            return [], []
        if previous is not None and opened-previous != 60:
            market["closes"] = []
        market["last_opened"] = opened
        market["closes"] = (market["closes"]+[float(body["close"])])[-201:]
        f = features(market, at, config)
        for name, wallet in state["wallets"].items():
            action, reason = signal(name, f, dec(wallet["qty"]) > 0, config)
            stale_bar = now-float(body["closed_at"]) > config["max_signal_age_seconds"]
            if action == "buy" and (paused or flatten or wallet["halted"] or stale_bar):
                action, reason = "hold", "New entries blocked: " + (wallet["halted"] or "paused, stopping, or delayed market data") + "."
            if action == "buy" and (f["spread_bps"] is None or f["spread_bps"] > config["max_spread_bps"]):
                action, reason = "hold", "Fresh order book with acceptable spread is required."
            if action != "hold" and not wallet.get("pending"):
                wallet["pending"] = {"action": action, "at": now, "event_id": event["id"], "reason": reason}
            decisions.append({"strategy": name, "event_id": event["id"], "at": now, "action": action, "reason": reason, "features": f})
        state["curve"].append({"at": at, **{name: float(w["equity"]) for name, w in state["wallets"].items()}})
        if len(state["curve"]) > 2000:
            state["curve"] = state["curve"][::2]
    return decisions, fills


def metrics(state, config):
    result = {}
    for name, w in state["wallets"].items():
        profit, loss = dec(w["gross_profit"]), dec(w["gross_loss"])
        result[name] = {"equity": float(w["equity"]), "net": float(dec(w["equity"])-dec(config["capital"])),
                        "return_pct": float((dec(w["equity"])/dec(config["capital"])-1)*100),
                        "max_drawdown_pct": w["max_dd"], "closed_trades": w["closed_trades"],
                        "win_rate": w["wins"]/w["closed_trades"]*100 if w["closed_trades"] else 0,
                        "profit_factor": float(profit/loss) if loss else None,
                        "fees": float(w["fees"]), "open_quantity": float(w["qty"]), "halted": w["halted"]}
    return result
