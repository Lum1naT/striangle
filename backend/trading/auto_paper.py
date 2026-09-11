"""Forward-only futures paper portfolios; no exchange order-placement code."""
from datetime import timedelta
import json
import logging

from django.db import transaction
from django.utils import timezone

from .auto_config import leverage_value, margin_config
from .auto_signals import signal
from .engine import features
from .margin import accrue_funding, close_position, entry_quantity, liquidatable, mark, metrics, new_wallet, open_position
from .models import AutoCycle, AutoRecord, AutonomyPolicy, Candle, Event, FuturesCandle
from .research import as_event

log = logging.getLogger(__name__)


def fresh(value, now, age):
    return value is not None and 0 <= now-value <= age


def choose(portfolio, cfg, now, closing=False):
    wallet, market, candidate = portfolio["wallet"], portfolio["market"], portfolio["candidate"]
    if candidate is None:
        return 0, "No eligible strategy for this asset; virtual capital stays in cash.", {}
    cache = portfolio.get("cached_signal", {})
    if cache.get("closed_at") != market.get("last_closed_at") or "value" not in cache:
        cache = {"closed_at": market.get("last_closed_at"), "value": signal(candidate, market.get("closes", []), portfolio["models"])}
        portfolio["cached_signal"] = cache
    strategy = cache["value"]
    f = features(market, now, cfg)
    f.update(strategy=strategy, leverage=candidate["leverage"], margin=wallet["margin"], liquidation_price=wallet.get("liquidation_price"),
        mark_price=market.get("mark_price"), mark_age=now-market["mark_at"] if market.get("mark_at") is not None else None)
    if wallet["quantity"]:
        if closing or wallet["halted"]:
            return 2, "Flattening: " + (wallet["halted"] or "cycle ended or automatic research stopped"), f
        if now-wallet["entry_at"] >= candidate["horizon"]*60:
            return 2, f"The {candidate['horizon']}-minute holding horizon has elapsed.", f
        book = market.get("book")
        if book and fresh(market.get("book_at"), now, cfg["max_book_age_seconds"]):
            price = float(book["bids" if wallet["side"] == 1 else "asks"][0][0])
            change = wallet["side"]*(price/wallet["entry"]-1)
            if change <= -cfg["stop_pct"]/100:
                return 2, "Protective stop triggered on the futures order book.", f
            if change >= cfg["take_pct"]/100:
                return 2, "Take-profit threshold reached on the futures order book.", f
        if fresh(market.get("last_closed_at"), now, 90) and strategy["exit_long" if wallet["side"] == 1 else "exit_short"]:
            return 2, "Closed-candle strategy exit: " + strategy["reason"], f
        return 0, "Holding; futures-book protective exits and mark-price liquidation checks remain active.", f
    if closing or wallet["halted"]:
        return 0, "New entries blocked: " + (wallet["halted"] or "cycle is closing"), f
    side = strategy["enter"]
    if not side:
        return 0, strategy["reason"], f
    leverage_value(candidate["leverage"], cfg["max_leverage"])
    if not fresh(market.get("last_closed_at"), now, 90):
        return 0, "Waiting for fresh completed candles.", f
    if not fresh(market.get("book_at"), now, cfg["max_book_age_seconds"]) or not fresh(market.get("mark_at"), now, 10):
        return 0, "Waiting for a fresh futures book and exchange mark price.", f
    if f["spread_bps"] is None or f["spread_bps"] > cfg["max_spread_bps"]:
        return 0, "Futures spread exceeds the entry limit.", f
    if f["flow_imbalance"] is None or f["funding_rate"] is None:
        return 0, "Waiting for current executed flow and derivatives context.", f
    if side*f["imbalance"] < cfg["min_imbalance"] or side*f["flow_imbalance"] < -.1:
        return 0, "Depth or executed flow opposes the proposed direction.", f
    if side*f["funding_rate"] > cfg["max_funding_rate"]:
        return 0, "Funding indicates crowding in the proposed direction.", f
    same = f["liquidated_shorts_usd" if side == 1 else "liquidated_longs_usd"]
    other = f["liquidated_longs_usd" if side == 1 else "liquidated_shorts_usd"]
    if same > max(1000000, other*3):
        return 0, "A large liquidation squeeze makes chasing this move unsuitable.", f
    if f["news_score"] is not None and side*f["news_score"] < -cfg["ai_min_score"]:
        return 0, "Fresh adverse news vetoed the model's proposed direction.", f
    if f["news_score"] is not None and market.get("assessment", {}).get("event_risk") == "high":
        return 0, "Fresh news indicates high event risk; entry vetoed.", f
    if cfg["require_heatmap"] and f["heatmap"] is None:
        return 0, "The configured liquidation heatmap is unavailable.", f
    if wallet.get("exit_candle") == market.get("last_opened"):
        return 0, "Waiting for the next completed candle after an exit.", f
    return side, strategy["reason"]+f" Isolated margin leverage {candidate['leverage']}x, within the risk budget.", f


def depth_fill(portfolio, pending, book, cfg, now):
    wallet = portfolio["wallet"]
    accrue_funding(wallet, now, cfg)
    closing = pending["side"] == 2
    execution_side = -wallet["side"] if closing else pending["side"]
    levels = book["asks" if execution_side == 1 else "bids"]
    slip = 1+execution_side*cfg["slippage_bps"]/10000
    worst = max(float(p) for p, _ in levels)*slip
    requested = wallet["quantity"] if closing else entry_quantity(wallet, worst, portfolio["candidate"]["leverage"], cfg)
    left = min(requested, sum(float(q) for _, q in levels)*cfg["max_participation_pct"]/100)
    quantity, value = 0.0, 0.0
    for price, available in levels:
        take = min(left, float(available))
        value += take*float(price); quantity += take; left -= take
        if left <= 1e-12:
            break
    if not quantity:
        return None
    price = value/quantity*slip
    fill = close_position(wallet, price, now, cfg, quantity=quantity) if closing else open_position(
        wallet, pending["side"], quantity, price, portfolio["candidate"]["leverage"], now, cfg)
    if fill:
        fill.update(reason=pending["reason"], execution="recorded_bybit_depth_with_participation_and_adverse_slippage")
        if closing and wallet["quantity"]:
            wallet["pending"] = pending
        else:
            wallet["pending"] = None
        if closing and not wallet["quantity"]:
            wallet["exit_candle"] = portfolio["market"].get("last_opened")
    return fill


def step_portfolio(portfolio, event, cfg, now, *, closing=False):
    market, wallet = portfolio["market"], portfolio["wallet"]
    kind, body, at = event["kind"], event["payload"], event["at"]
    observed = min(event.get("received_at", at), event.get("event_at", at))
    candle_kind = portfolio["candle_kind"]
    records = []
    if kind == candle_kind:
        opened = body["opened_at"]
        previous = market.get("last_opened")
        if previous is not None and opened <= previous:
            return []
        if previous is not None and opened-previous != 60:
            market["closes"] = []
        market.update(last_opened=opened, last_closed_at=body["closed_at"], closes=(market["closes"]+[float(body["close"])])[-201:])
    elif kind == "perp_book":
        market.update(book=body, book_at=observed)
    elif kind == "derivatives":
        market["derivatives"] = body | {"at": observed}
        if body.get("mark_price"):
            market.update(mark_price=body["mark_price"], mark_at=observed)
    elif kind in ("assessment", "heatmap"):
        market[kind] = body | {"at": observed, "id": event["id"]}
    elif kind == "flow":
        market["tape"][str(int(at))] = [float(body["buy_notional"]), float(body["sell_notional"])]
        market["tape"] = {t: v for t, v in market["tape"].items() if float(t) >= at-60}
    elif kind == "liquidation":
        bucket = market["liquidations"].setdefault(str(int(at)), [0, 0])
        bucket[0 if body["position_side"] == "long" else 1] += float(body["price"])*float(body["quantity"])
        market["liquidations"] = {t: v for t, v in market["liquidations"].items() if float(t) >= at-60}
    elif kind == "perp_gap":
        for field in ("book", "book_at", "mark_price", "mark_at", "derivatives"):
            market.pop(field, None)
        if wallet.get("pending") and wallet["pending"]["side"] != 2:
            wallet["pending"] = None
    elif kind == "gap":
        market["tape"] = {}
    else:
        return []
    if (kind == "gap" and candle_kind == "candle") or (kind == "perp_gap" and candle_kind == "perp_candle"):
        market["closes"] = []
        market.pop("last_closed_at", None)
        portfolio.pop("cached_signal", None)
    if fresh(market.get("mark_at"), now, 10):
        accrue_funding(wallet, now, cfg)
        mark(wallet, market["mark_price"], now, cfg)
        if liquidatable(wallet, market["mark_price"], cfg):
            fill = close_position(wallet, market["mark_price"], now, cfg, liquidated=True)
            fill.update(reason="Exchange mark price breached modeled isolated maintenance margin.", execution="modeled_liquidation_at_recorded_mark")
            records.append({"kind": "fill", "payload": fill})
            wallet["exit_candle"] = market.get("last_opened")
    pending = wallet.get("pending")
    if pending and pending["side"] != 2 and (closing or now-pending["at"] > cfg["max_signal_age_seconds"]):
        wallet["pending"], pending = None, None
    if kind == "perp_book" and pending and event["id"] > pending["event_id"] and fresh(observed, now, cfg["max_book_age_seconds"]) and (pending["side"] == 2 or fresh(market.get("mark_at"), now, 10)):
        allowed = pending["side"] == 2 or choose(portfolio, cfg, now, closing)[0] == pending["side"]
        if allowed:
            fill = depth_fill(portfolio, pending, body, cfg, now)
            if fill:
                records.append({"kind": "fill", "payload": fill})
        else:
            wallet["pending"] = None
    action, reason, inputs = choose(portfolio, cfg, now, closing)
    pending = wallet.get("pending")
    if pending and pending["side"] == 2:
        action, reason = 2, pending["reason"]
    if action and not wallet.get("pending"):
        wallet["pending"] = {"side": action, "event_id": event["id"], "at": now, "reason": reason}
    elif action == 0 and pending and pending["side"] != 2:
        wallet["pending"] = None
    label = {0: "hold", 1: "long", -1: "short", 2: "close"}[action]
    current = {"action": label, "reason": reason, "at": now, "inputs": inputs}
    previous = portfolio.get("signal", {})
    if (label, reason) != (previous.get("action"), previous.get("reason")) or now-portfolio.get("journal_at", 0) >= 60:
        records.append({"kind": "decision", "payload": current})
        portfolio["journal_at"] = now
    portfolio["signal"] = current
    return records


def seed_cycle(cycle, started):
    from .autonomy import history_query
    report = cycle.report
    state = {"portfolios": {}, "allocated_symbol": report["allocated_symbol"]}
    table = FuturesCandle if report["source"] == "bybit_perpetual_candles" else Candle
    for symbol, row in report["by_asset"].items():
        candidate = row["candidate"] if row else None
        market = {"closes": [], "tape": {}, "liquidations": {}}
        recent = list(history_query(table, symbol, started)[:201])
        consecutive = []
        for candle in recent:
            if consecutive and consecutive[-1].opened_at != candle.closed_at:
                break
            consecutive.append(candle)
        if consecutive:
            market.update(closes=[float(c.payload["close"]) for c in reversed(consecutive)],
                last_opened=consecutive[0].opened_at.timestamp(), last_closed_at=consecutive[0].closed_at.timestamp())
        models = {candidate["model_key"]: cycle.artifacts[symbol][candidate["model_key"]]} if candidate and candidate["family"] == "ml" else {}
        state["portfolios"][symbol] = {"candidate": candidate, "models": models, "market": market,
            "wallet": new_wallet(cycle.config["risk"]["capital"]), "candle_kind": "perp_candle" if table is FuturesCandle else "candle"}
    return state


def tick():
    now = timezone.now()
    with transaction.atomic():
        policy = AutonomyPolicy.objects.select_for_update().filter(pk=1).first()
        if not policy:
            return False
        cycle = AutoCycle.objects.select_for_update().defer("artifacts", "report").filter(status__in=["paper", "closing"]).order_by("created_at").first()
        if not cycle and policy.enabled:
            cycle = AutoCycle.objects.select_for_update().filter(status="ready").order_by("created_at").first()
            if cycle and (cycle.config != policy.config or now-cycle.cutoff > timedelta(days=7)):
                cycle.status, cycle.error = "cancelled", "Settings changed or research snapshot is more than seven days old"
                cycle.save(update_fields=["status", "error"])
                cycle = None
            if cycle:
                cycle.state = seed_cycle(cycle, now)
                latest = Event.objects.filter(received_at__lte=now, available_at__lte=now).order_by("-id").first()
                cycle.last_event_id = latest.id if latest else 0
                cycle.forward_start, cycle.forward_end = now, now+timedelta(hours=cycle.config["interval_hours"])
                cycle.status = "paper"
                cycle.save(update_fields=["state", "last_event_id", "forward_start", "forward_end", "status"])
                policy.status = "Forward paper test active; next training cycle runs automatically"
                policy.save(update_fields=["status"])
        if not cycle:
            return False
        if not policy.enabled or now >= cycle.forward_end:
            cycle.status = "closing"
        cfg = margin_config(cycle.config)
        rows = list(Event.objects.filter(symbol__in=list(cycle.state["portfolios"]), id__gt=cycle.last_event_id, available_at__lte=now, received_at__lte=now)
            .exclude(kind="ai_attempt").order_by("id")[:1000])
        records = []
        for row in rows:
            now = timezone.now()
            portfolio = cycle.state["portfolios"][row.symbol]
            for record in step_portfolio(portfolio, as_event(row), cfg, now.timestamp(), closing=cycle.status == "closing"):
                records.append(AutoRecord(cycle=cycle, symbol=row.symbol, kind=record["kind"], event_id=row.id, at=now, payload=record["payload"]))
            cycle.last_event_id = row.id
        if cycle.status == "closing" and all(not p["wallet"]["quantity"] for p in cycle.state["portfolios"].values()):
            cycle.status, cycle.finished_at = "completed", now
            cycle.state["final_results"] = {s: metrics(p["wallet"], cfg) for s, p in cycle.state["portfolios"].items()}
        if rows or cycle.status in ("closing", "completed"):
            if rows:
                cycle.state["processed_at"] = now.timestamp()
                if now.timestamp()-cycle.state.get("last_log_at", 0) >= 60:
                    cycle.state["last_log_at"] = now.timestamp()
                    report = {"cycle": str(cycle.id), "status": cycle.status, "selected_symbol": cycle.state.get("allocated_symbol"),
                        "portfolios": {s: {"strategy": p["candidate"]["id"] if p["candidate"] else "cash", "leverage": p["candidate"]["leverage"] if p["candidate"] else 1,
                            "return_pct": metrics(p["wallet"], cfg)["return_pct"], "quantity": p["wallet"]["quantity"],
                            "reason": p.get("signal", {}).get("reason"), "book_age": now.timestamp()-p["market"]["book_at"] if p["market"].get("book_at") else None,
                            "mark_age": now.timestamp()-p["market"]["mark_at"] if p["market"].get("mark_at") else None} for s, p in cycle.state["portfolios"].items()}}
                    transaction.on_commit(lambda report=report: log.info("auto_paper_progress %s", json.dumps(report)))
            AutoRecord.objects.bulk_create(records, ignore_conflicts=True)
            cycle.save(update_fields=["state", "last_event_id", "status", "finished_at"])
        return len(rows) == 1000
