"""Deterministic isolated-margin paper accounting; never an exchange adapter.

Funding is a declared adverse accrual allowance on entry notional, for either
direction. It is not passed off as actual exchange settlement history. A fixed
maintenance-margin assumption is used; venue tiers/ADL are not reconstructed.
"""
import math

from .auto_config import leverage_value


def new_wallet(capital):
    return {"cash": float(capital), "equity": float(capital), "peak": float(capital), "margin": 0.0,
        "quantity": 0.0, "side": 0, "entry": 0.0, "leverage": 1, "fees": 0.0, "funding": 0.0,
        "closed_trades": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0, "liquidations": 0,
        "max_drawdown_pct": 0.0, "halted": "", "day": None, "day_start": float(capital), "pending": None}


def accrue_funding(wallet, at, cfg):
    if not wallet["quantity"]:
        return 0.0
    elapsed = max(0, at-wallet["funded_at"])
    cost = wallet["quantity"]*wallet["entry"] * cfg["funding_bps_8h"]/10000 * elapsed/28800
    wallet["funded_at"] = max(at, wallet["funded_at"])
    wallet["margin"] -= cost
    wallet["funding"] += cost
    return cost


def liquidation_price(wallet, cfg):
    if not wallet["quantity"]:
        return None
    rate = (cfg["maintenance_margin_bps"]+cfg["fee_bps"])/10000
    direction, qty = wallet["side"], wallet["quantity"]
    return max(0, (wallet["entry"]-direction*wallet["margin"]/qty)/(1-direction*rate))


def liquidatable(wallet, mark_price, cfg):
    if not wallet["quantity"]:
        return False
    pnl = wallet["side"]*wallet["quantity"]*(mark_price-wallet["entry"])
    maintenance = wallet["quantity"]*mark_price*(cfg["maintenance_margin_bps"]+cfg["fee_bps"])/10000
    return wallet["margin"]+pnl <= maintenance


def mark(wallet, price, at, cfg):
    pnl = wallet["side"]*wallet["quantity"]*(price-wallet["entry"])
    estimated_exit = wallet["quantity"]*price*(cfg["fee_bps"]+cfg["slippage_bps"])/10000
    wallet["equity"] = wallet["cash"]+max(0, wallet["margin"]+pnl-estimated_exit)
    wallet["peak"] = max(wallet["peak"], wallet["equity"])
    dd = (wallet["peak"]-wallet["equity"])/wallet["peak"]*100
    wallet["max_drawdown_pct"] = max(wallet["max_drawdown_pct"], dd)
    day = int(at//86400)
    if day != wallet["day"]:
        wallet["day"], wallet["day_start"] = day, wallet["equity"]
        if wallet["halted"] == "daily loss limit":
            wallet["halted"] = ""
    if dd >= cfg["max_drawdown_pct"]:
        wallet["halted"] = "maximum drawdown limit"
    elif wallet["equity"] <= wallet["day_start"]*(1-cfg["daily_loss_pct"]/100):
        wallet["halted"] = wallet["halted"] or "daily loss limit"
    wallet["liquidation_price"] = liquidation_price(wallet, cfg)
    return wallet["equity"]


def entry_quantity(wallet, price, leverage, cfg, horizon_minutes=None):
    leverage = leverage_value(leverage, cfg["max_leverage"])
    horizon_minutes = cfg.get("horizon_minutes", 60) if horizon_minutes is None else horizon_minutes
    fee = cfg["fee_bps"]/10000
    margin_budget = max(0, wallet["cash"]*cfg["allocation_pct"]/100)
    loss = cfg["stop_pct"]/100+2*(cfg["fee_bps"]+cfg["slippage_bps"])/10000+cfg["funding_bps_8h"]/10000*horizon_minutes/480
    notional = min(margin_budget/(1/leverage+fee), wallet["equity"]*cfg["risk_per_trade_pct"]/100/loss)
    return max(0, notional/price)


def open_position(wallet, side, quantity, price, leverage, at, cfg):
    leverage = leverage_value(leverage, cfg["max_leverage"])
    if side not in (-1, 1) or wallet["quantity"] or wallet["halted"]:
        return None
    if not all(math.isfinite(v) and v > 0 for v in (quantity, price)):
        return None
    notional = quantity*price
    collateral, fee = notional/leverage, notional*cfg["fee_bps"]/10000
    # Recheck after depth/slippage, even when the caller already sized the order.
    if collateral+fee > wallet["cash"]*cfg["allocation_pct"]/100+1e-8:
        return None
    if quantity > entry_quantity(wallet, price, leverage, cfg)+1e-8:
        return None
    wallet.update(round_start=wallet["cash"], cash=wallet["cash"]-collateral-fee, margin=collateral,
        quantity=quantity, entry=price, side=side, leverage=leverage, entry_at=at, funded_at=at)
    wallet["fees"] += fee
    return {"action": "open_long" if side == 1 else "open_short", "price": price, "quantity": quantity,
        "leverage": leverage, "margin": collateral, "fee": fee, "pnl": None}


def close_position(wallet, price, at, cfg, *, quantity=None, liquidated=False):
    old = wallet["quantity"]
    if not old:
        return None
    quantity = old if quantity is None or liquidated else min(old, max(0, quantity))
    if quantity <= 0:
        return None
    fraction = quantity/old
    collateral = wallet["margin"]*fraction
    pnl = wallet["side"]*quantity*(price-wallet["entry"])
    fee = 0.0 if liquidated else quantity*price*cfg["fee_bps"]/10000
    released = 0.0 if liquidated else max(0, collateral+pnl-fee)
    wallet["cash"] += released
    wallet["margin"] -= collateral
    wallet["quantity"] = max(0, old-quantity)
    wallet["fees"] += fee
    wallet["liquidations"] += int(liquidated)
    result = {"action": "liquidation" if liquidated else "close_long" if wallet["side"] == 1 else "close_short",
        "price": price, "quantity": quantity, "leverage": wallet["leverage"], "fee": fee,
        "pnl": released-collateral, "partial": wallet["quantity"] > 1e-12}
    if wallet["quantity"] <= 1e-12:
        trip = wallet["cash"]-wallet["round_start"]
        wallet["closed_trades"] += 1
        wallet["wins"] += int(trip > 0)
        wallet["gross_profit"] += max(0, trip)
        wallet["gross_loss"] += max(0, -trip)
        wallet.update(side=0, quantity=0.0, margin=0.0, pending=None)
        result.update(round_pnl=trip, partial=False)
    mark(wallet, price, at, cfg)
    return result


def metrics(wallet, cfg):
    return {key: wallet[key] for key in ("equity", "margin", "quantity", "side", "leverage", "fees", "funding",
        "closed_trades", "liquidations", "max_drawdown_pct", "halted")} | {
        "return_pct": (wallet["equity"]/cfg["capital"]-1)*100,
        "win_rate": 100*wallet["wins"]/wallet["closed_trades"] if wallet["closed_trades"] else None,
        "profit_factor": wallet["gross_profit"]/wallet["gross_loss"] if wallet["gross_loss"] else None,
        "liquidation_price": wallet.get("liquidation_price")}
