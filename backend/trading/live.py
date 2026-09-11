"""Opt-in spot execution with an outbox and native exchange protection.

No callable web endpoint arms an account. The operator must run arm_live after
configuring the server and satisfying evidence gates. Tests inject a broker.
"""
import uuid
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .broker import BinanceSpot, BrokerError, order_parameters, protection_parameters
from .configuration import dec, fingerprint
from .engine import initial_state, mark, metrics, order_budget
from .models import Event, Fill, LiveOrder, Run
from .providers import normalize_book
from .services import readiness

DEFINITE_REJECTIONS = {-1013, -1100, -1102, -1111, -1121, -2010, -2014, -2015, -1021, -1022}


def broker_from_settings():
    return BinanceSpot(settings.BINANCE_API_KEY, settings.BINANCE_API_SECRET, settings.BINANCE_TESTNET)


def arm(paper, capital, broker=None):
    report = readiness(paper)
    if not report["eligible"]:
        raise ValueError("Live gates failed: "+", ".join(c["label"] for c in report["checks"] if not c["passed"]))
    if dec(capital) != dec(paper.config["capital"]):
        raise ValueError("Live capital must match the evaluated paper capital; evaluate changed sizing in a new paper run")
    broker = broker or broker_from_settings()
    permissions = broker.permissions()
    account = broker.account()
    if permissions.get("enableWithdrawals") is not False or permissions.get("enableSpotAndMarginTrading") is not True or not account.get("canTrade"):
        raise ValueError("A spot-trading key with withdrawals disabled is required")
    balances = {x["asset"]: dec(x["free"])+dec(x["locked"]) for x in account["balances"]}
    quote_free = next((dec(x["free"]) for x in account["balances"] if x["asset"] == "USDT"), dec(0))
    if quote_free < dec(capital):
        raise ValueError("Insufficient free USDT for the declared allocation")
    for symbol in settings.SYMBOLS:
        if broker.open_orders(symbol) or balances.get(symbol.removesuffix("USDT"), 0) != 0:
            raise ValueError("The dedicated account must start flat, with no BTC/ETH positions or open orders")
    broker.symbol_info(paper.symbol)
    with transaction.atomic():
        # Lock the single configured owner, including across competing arm calls.
        type(paper.owner).objects.select_for_update().get(pk=paper.owner_id)
        if Run.objects.filter(mode="live", status__in=["running", "reconciling"]).exists():
            raise ValueError("Only one live run may own the dedicated account")
        state = initial_state(paper.config, ("ai_trend",))
        latest = Event.objects.order_by("-id").first()
        state["last_id"] = latest.id if latest else 0
        state["exchange_trade_ids"] = []
        state["testnet"] = settings.BINANCE_TESTNET
        return Run.objects.create(owner=paper.owner, name=f"{paper.symbol} {'testnet' if settings.BINANCE_TESTNET else 'live'}", symbol=paper.symbol,
            mode="live", config=paper.config, config_hash=paper.config_hash, validation=paper, state=state, last_event_id=state["last_id"])


def prepare(run, kind, payload):
    client_id = "stg"+uuid.uuid4().hex
    key = "listClientOrderId" if kind == "protection" else "newClientOrderId"
    return LiveOrder.objects.create(run=run, client_id=client_id, kind=kind, request=payload | {key: client_id})


def send_once(order, broker):
    """Commit the sending state before I/O; a crash can never cause a resend."""
    claimed = LiveOrder.objects.filter(pk=order.pk, status="prepared").update(status="submitting")
    if not claimed:
        return reconcile(order, broker)
    try:
        response = broker.submit(order.kind, order.request)
        order.response, order.status, order.error = response, "accepted", ""
    except BrokerError as exc:
        order.status = "rejected" if exc.code in DEFINITE_REJECTIONS else "unknown"
        order.error = str(exc)
    except Exception:
        order.status, order.error = "unknown", "Unexpected transport failure; reconcile before any further order"
    order.save(update_fields=["response", "status", "error", "updated_at"])
    return order


def reconcile(order, broker):
    order.refresh_from_db()
    if order.status in ("rejected", "terminal"):
        return order
    try:
        order.response = broker.query(order.kind, order.run.symbol, order.client_id)
        order.status, order.error = "accepted", ""
    except BrokerError as exc:
        # Even an order-not-found reply after a timeout does not authorize a new
        # submission. Client IDs can be reused after fills on this exchange.
        order.status, order.error = "unknown", str(exc)
    order.save(update_fields=["response", "status", "error", "updated_at"])
    return order


def fee_in_quote(broker, trade, base):
    fee, asset = dec(trade["commission"]), trade["commissionAsset"]
    if fee == 0 or asset == "USDT":
        return fee
    if asset == base:
        return fee*dec(trade["price"])
    ticker = broker.call("GET", "/api/v3/ticker/price", {"symbol": asset+"USDT"}, False)
    return fee*dec(ticker["price"])


def account_trades(run_id, trades, broker):
    # Network enrichment occurs before the transaction. No ledger mutation is
    # committed if a fee conversion or response validation fails.
    enriched = []
    run = Run.objects.get(pk=run_id)
    base = run.symbol.removesuffix("USDT")
    for trade in trades:
        if dec(trade["qty"]) <= 0 or dec(trade["price"]) <= 0 or dec(trade["commission"]) < 0:
            raise ValueError("Invalid exchange fill")
        enriched.append((trade, fee_in_quote(broker, trade, base)))
    with transaction.atomic():
        run = Run.objects.select_for_update().get(pk=run_id)
        wallet = run.state["wallets"]["ai_trend"]
        seen = set(run.state.get("exchange_trade_ids", []))
        for trade, fee in enriched:
            trade_id = str(trade["id"])
            if trade_id in seen:
                continue
            quantity, gross = dec(trade["qty"]), dec(trade["quoteQty"])
            base_fee = dec(trade["commission"]) if trade["commissionAsset"] == base else dec(0)
            # Base fees reduce acquired units; charging them again to cash would
            # double count the cost. Third-asset fees reduce virtual allocation.
            cash_fee = dec(0) if base_fee else fee
            pnl = None
            if trade["isBuyer"]:
                net_qty = quantity-base_fee
                total_qty = dec(wallet["qty"])+net_qty
                if total_qty <= 0:
                    raise ValueError("Non-positive net acquired quantity")
                wallet.update(cash=str(dec(wallet["cash"])-gross-cash_fee), qty=str(total_qty),
                    cost_basis=str(dec(wallet["cost_basis"])+gross+cash_fee),
                    entry=str((dec(wallet["entry"])*dec(wallet["qty"])+gross)/total_qty))
            else:
                consumed = quantity+base_fee
                old_qty = dec(wallet["qty"])
                if consumed > old_qty or old_qty <= 0:
                    raise ValueError("Exchange fill exceeds the recorded position; account reconciliation required")
                cost = dec(wallet["cost_basis"])*consumed/old_qty
                pnl = gross-cash_fee-cost
                wallet.update(cash=str(dec(wallet["cash"])+gross-cash_fee), qty=str(old_qty-consumed),
                    cost_basis=str(dec(wallet["cost_basis"])-cost), round_pnl=str(dec(wallet["round_pnl"])+pnl))
                if dec(wallet["qty"]) == 0:
                    trip = dec(wallet["round_pnl"])
                    wallet["closed_trades"] += 1
                    wallet["wins"] += int(trip > 0)
                    wallet["gross_profit"] = str(dec(wallet["gross_profit"])+max(dec(0), trip))
                    wallet["gross_loss"] = str(dec(wallet["gross_loss"])+max(dec(0), -trip))
                    wallet["round_pnl"] = "0"
            wallet["fees"] = str(dec(wallet["fees"])+fee)
            Fill.objects.create(run=run, strategy="ai_trend", at=datetime.fromtimestamp(trade["time"]/1000, dt_timezone.utc),
                side="buy" if trade["isBuyer"] else "sell", quantity=quantity, price=dec(trade["price"]), fee=fee, pnl=pnl,
                reason="Confirmed exchange fill", details={"exchange_trade_id": trade_id, "order_id": trade["orderId"], "commission_asset": trade["commissionAsset"], "commission": trade["commission"], "third_asset_fee_conversion": trade["commissionAsset"] not in (base, "USDT")})
            seen.add(trade_id)
        run.state["exchange_trade_ids"] = sorted(seen)
        run.save(update_fields=["state"])


def reconcile_orders(run, broker):
    for order in run.orders.exclude(status__in=["terminal", "rejected"]).order_by("created_at"):
        if order.status == "prepared":
            # The creating invocation sends immediately. Finding a prepared
            # intent here means a restart happened before it was claimed. Do
            # not execute an old entry without current risk/data checks.
            order.status, order.error = "rejected", "Unsent intent abandoned after worker interruption"
            order.save(update_fields=["status", "error", "updated_at"])
        else:
            reconcile(order, broker)
        order.refresh_from_db()
        if order.status == "unknown":
            raise ValueError("An order has unknown status; only reconciliation is allowed")
        if order.status == "rejected":
            if order.kind == "protection":
                Run.objects.filter(pk=run.pk).update(entries_paused=True, flatten_requested=True, error="Protection rejected; flattening requested")
            continue
        response = order.response
        exchange_ids = [x["orderId"] for x in response.get("orders", [])] if order.kind == "protection" else [response["orderId"]]
        for order_id in exchange_ids:
            trades = broker.trades(run.symbol, order_id)
            if len(trades) >= 1000:
                raise ValueError("Fill pagination limit reached; manual reconciliation required")
            account_trades(run.pk, trades, broker)
        terminal = response.get("listOrderStatus") == "ALL_DONE" if order.kind == "protection" else response.get("status") in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")
        if terminal:
            for exchange_id in exchange_ids:
                status = broker.call("GET", "/api/v3/order", {"symbol": run.symbol, "orderId": exchange_id}) if order.kind == "protection" else response
                trades = broker.trades(run.symbol, exchange_id)
                if sum((dec(t["qty"]) for t in trades), dec(0)) != dec(status.get("executedQty", 0)):
                    raise ValueError("Exchange fills have not caught up with order quantity")
            order.status, order.accounted = "terminal", True
            order.save(update_fields=["status", "accounted", "updated_at"])


def live_tick(run_id, broker=None):
    run = Run.objects.get(pk=run_id)
    if run.mode != "live" or run.status not in ("running", "reconciling"):
        return
    if timezone.now().timestamp() < run.state.get("retry_after", 0):
        return
    broker = broker or broker_from_settings()
    try:
        # Never switch an existing order ledger between testnet and production.
        if run.state.get("testnet") != settings.BINANCE_TESTNET or str(run.owner_id) != settings.LIVE_OWNER_ID:
            raise ValueError("Execution account/environment changed; restore it before reconciliation")
        reconcile_orders(run, broker)
        run.refresh_from_db()
        if run.orders.exclude(kind="protection").exclude(status__in=["terminal", "rejected"]).exists():
            raise ValueError("Waiting for the exchange to finalize the IOC order before sizing another order")
        book = normalize_book(**{k: v for k, v in broker.book(run.symbol).items() if k in ("bids", "asks")})
        info, account = broker.symbol_info(run.symbol), broker.account()
        wallet = run.state["wallets"]["ai_trend"]
        now = timezone.now().timestamp()
        mark(wallet, book["bids"][0][0], now, run.config)
        qty = dec(wallet["qty"])
        balances = {x["asset"]: dec(x["free"])+dec(x["locked"]) for x in account["balances"]}
        if balances.get(info["baseAsset"], dec(0)) != qty:
            raise ValueError("Exchange position differs from the ledger; reconcile before new orders")
        run.status, run.error = "running", ""
        run.state["retry_after"], run.state["retry_count"] = 0, 0
        if not settings.LIVE_TRADING_ENABLED or run.config_hash != fingerprint(run.config, settings.OPENAI_MODEL):
            run.entries_paused = True
            Run.objects.filter(pk=run.pk).update(entries_paused=True)
        if not account.get("canTrade"):
            raise ValueError("Exchange account cannot trade")
        protection = run.orders.filter(kind="protection").exclude(status__in=["terminal", "rejected"]).first()
        pending = wallet.get("pending")
        want_exit = qty > 0 and (run.flatten_requested or wallet["halted"] or (pending and pending["action"] == "sell"))
        if protection and want_exit:
            # Persist cancellation uncertainty, then query the whole list and
            # apply any racing fills before sizing a reduce-only spot sell.
            protection.status = "cancelling"
            protection.save(update_fields=["status", "updated_at"])
            wallet["pending"] = {"action": "sell", "at": now, "event_id": run.last_event_id,
                                 "reason": wallet["halted"] or "Operator or strategy exit"}
            Run.objects.filter(pk=run.pk).update(state=run.state)
            broker.cancel_protection(run.symbol, protection.client_id)
            return
        if protection:
            run.status, run.error = "running", ""
        elif qty > 0 and not want_exit:
            payload = protection_parameters(info, qty, wallet["entry"], run.config)
            order = prepare(run, "protection", payload)
            send_once(order, broker)
            if order.status == "rejected":
                run.entries_paused, run.flatten_requested = True, True
                Run.objects.filter(pk=run.pk).update(entries_paused=True, flatten_requested=True)
                run.error = "Native protection was rejected; flattening at the next available opportunity"
        elif want_exit:
            payload = order_parameters(info, book, "SELL", qty, run.config["slippage_bps"])
            order = prepare(run, "exit", payload)
            send_once(order, broker)
        elif pending and pending["action"] == "buy":
            # Re-read operator controls after the network reads above.
            controls = Run.objects.values("entries_paused", "flatten_requested").get(pk=run.pk)
            run.entries_paused, run.flatten_requested = controls["entries_paused"], controls["flatten_requested"]
            age = now-pending["at"]
            market_age = now-run.state["market"].get("book_at", 0)
            bid, ask = dec(book["bids"][0][0]), dec(book["asks"][0][0])
            spread = (ask-bid)/((ask+bid)/2)*10000
            if not run.entries_paused and not run.flatten_requested and not wallet["halted"] and age <= run.config["max_signal_age_seconds"] and market_age <= run.config["max_book_age_seconds"] and spread <= dec(run.config["max_spread_bps"]):
                if dec(run.config["capital"]) > dec(settings.LIVE_MAX_CAPITAL):
                    raise ValueError("Configured capital exceeds the operator cap")
                permissions = broker.permissions()
                if permissions.get("enableWithdrawals") is not False:
                    raise ValueError("Key permits withdrawals; new entries disabled")
                free_usdt = next((dec(x["free"]) for x in account["balances"] if x["asset"] == "USDT"), dec(0))
                budget = min(free_usdt, order_budget(wallet, run.config))
                worst = ask*(1+dec(run.config["slippage_bps"])/10000)
                capacity = sum(dec(q) for _, q in book["asks"])*dec(run.config["max_participation_pct"])/100
                quantity = min(capacity, budget/(worst*(1+dec(run.config["fee_bps"])/10000)))
                payload = order_parameters(info, book, "BUY", quantity, run.config["slippage_bps"])
                send_once(prepare(run, "entry", payload), broker)
            wallet["pending"] = None
        if qty == 0 and run.flatten_requested and not run.orders.exclude(status__in=["terminal", "rejected"]).exists():
            run.status, run.ended_at = "stopped", timezone.now()
        run.results = metrics(run.state, run.config)
        run.save(update_fields=["state", "results", "status", "error", "ended_at"])
    except Exception as exc:
        with transaction.atomic():
            current = Run.objects.select_for_update().get(pk=run_id)
            count = min(6, current.state.get("retry_count", 0)+1)
            current.state["retry_count"] = count
            current.state["retry_after"] = timezone.now().timestamp()+min(60, 2**count)
            current.status, current.entries_paused = "reconciling", True
            current.error = str(exc)[:300] if isinstance(exc, (ValueError, BrokerError)) else f"Live execution paused ({type(exc).__name__})"
            current.save(update_fields=["state", "status", "entries_paused", "error"])
