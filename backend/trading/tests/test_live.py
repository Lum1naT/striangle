from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trading.broker import BrokerError, order_parameters, protection_parameters
from trading.configuration import dec, fingerprint, validate_config
from trading.engine import initial_state
from trading.live import account_trades, arm, live_tick, prepare, reconcile_orders, send_once
from trading.models import Run


@override_settings(OPENAI_MODEL="", LIVE_TRADING_ENABLED=False)
class LiveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator")
        self.config = validate_config({"capital": 1000})
        state = initial_state(self.config, ("ai_trend",))
        state["exchange_trade_ids"] = []
        self.run = Run.objects.create(owner=self.user, name="Fixture only", symbol="BTCUSDT", mode="live", config=self.config, config_hash=fingerprint(self.config, ""), state=state)
        self.broker = Mock()

    def test_confirmed_submission_is_never_repeated(self):
        order = prepare(self.run, "entry", {"symbol": "BTCUSDT", "side": "BUY"})
        self.broker.submit.return_value = {"orderId": 1, "status": "FILLED", "executedQty": "1"}
        self.broker.query.return_value = self.broker.submit.return_value
        send_once(order, self.broker)
        send_once(order, self.broker)
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_timeout_then_not_found_does_not_resubmit(self):
        order = prepare(self.run, "entry", {"symbol": "BTCUSDT", "side": "BUY"})
        self.broker.submit.side_effect = BrokerError(-1007)
        self.broker.query.side_effect = BrokerError(-2013)
        send_once(order, self.broker)
        send_once(order, self.broker)
        order.refresh_from_db()
        self.assertEqual(order.status, "unknown")
        self.assertEqual(self.broker.submit.call_count, 1)

    def test_restart_does_not_execute_old_unsubmitted_entry(self):
        order = prepare(self.run, "entry", {"symbol": "BTCUSDT", "side": "BUY"})
        reconcile_orders(self.run, self.broker)
        order.refresh_from_db()
        self.assertEqual(order.status, "rejected")
        self.broker.submit.assert_not_called()

    def trade(self, **changes):
        return {"id": 1, "orderId": 123, "qty": "1", "quoteQty": "100", "price": "100", "commission": ".001", "commissionAsset": "BTC", "isBuyer": True, "time": 60000, **changes}

    def test_base_asset_fee_reduces_quantity_without_double_charging_cash(self):
        account_trades(self.run.pk, [self.trade()], self.broker)
        self.run.refresh_from_db()
        wallet = self.run.state["wallets"]["ai_trend"]
        self.assertEqual(dec(wallet["cash"]), 900)
        self.assertEqual(dec(wallet["qty"]), dec(".999"))
        self.assertEqual(dec(wallet["cost_basis"]), 100)
        self.assertEqual(dec(wallet["fees"]), dec(".1"))

    def test_duplicate_fill_reconciliation_is_idempotent(self):
        account_trades(self.run.pk, [self.trade()], self.broker)
        account_trades(self.run.pk, [self.trade()], self.broker)
        self.run.refresh_from_db()
        self.assertEqual(dec(self.run.state["wallets"]["ai_trend"]["cash"]), 900)
        self.assertEqual(self.run.fills.count(), 1)

    def test_partial_buy_fills_aggregate_before_exit(self):
        trades = [self.trade(id=i, qty=".5", quoteQty="50", commission=".05", commissionAsset="USDT") for i in (1, 2)]
        account_trades(self.run.pk, trades, self.broker)
        self.run.refresh_from_db()
        wallet = self.run.state["wallets"]["ai_trend"]
        self.assertEqual(dec(wallet["qty"]), 1)
        self.assertEqual(dec(wallet["cash"]), dec("899.90"))
        self.assertEqual(dec(wallet["entry"]), 100)
        account_trades(self.run.pk, [self.trade(id=3, isBuyer=False, qty="1", quoteQty="110", price="110", commission=".11", commissionAsset="USDT")], self.broker)
        self.run.refresh_from_db()
        wallet = self.run.state["wallets"]["ai_trend"]
        self.assertEqual(dec(wallet["cash"]), dec("1009.79"))
        self.assertEqual(wallet["closed_trades"], 1)

    def test_disabled_live_gate_does_not_even_contact_broker(self):
        with self.assertRaises(ValueError):
            arm(self.run, 1000, self.broker)
        self.broker.account.assert_not_called()
        self.broker.submit.assert_not_called()

    def test_quantity_filters_and_native_oco_are_constructed(self):
        info = {"symbol": "BTCUSDT", "filters": [{"filterType": "LOT_SIZE", "stepSize": ".001", "minQty": ".001", "maxQty": "100"}, {"filterType": "PRICE_FILTER", "tickSize": ".1"}, {"filterType": "NOTIONAL", "minNotional": "5"}]}
        params = order_parameters(info, {"bids": [["100", "2"]], "asks": [["100.1", "2"]]}, "BUY", ".12345", 10)
        self.assertEqual(params["timeInForce"], "IOC")
        self.assertEqual(params["quantity"], "0.123")
        oco = protection_parameters(info, ".12345", "100", self.config)
        self.assertEqual(oco["belowType"], "STOP_LOSS")
        self.assertEqual(dec(oco["belowStopPrice"]), 98)
        with self.assertRaises(ValueError):
            order_parameters(info, {"bids": [["100", "2"]], "asks": [["101", "2"]]}, "BUY", ".00001", 5)

    def test_complete_entry_protection_cancel_and_exit_lifecycle(self):
        now = timezone.now().timestamp()
        self.run.state["testnet"] = True
        self.run.state["market"]["book_at"] = now
        self.run.state["wallets"]["ai_trend"]["pending"] = {"action": "buy", "at": now, "event_id": 1, "reason": "Fixture signal"}
        self.run.save(update_fields=["state"])
        fake = LifecycleBroker()
        with override_settings(LIVE_TRADING_ENABLED=True, BINANCE_TESTNET=True, LIVE_OWNER_ID=str(self.user.id), LIVE_MAX_CAPITAL="1000"):
            live_tick(self.run.pk, fake)  # IOC entry submitted
            self.assertEqual(fake.submitted, ["entry"])
            live_tick(self.run.pk, fake)  # fills reconciled, native OCO submitted
            self.run.refresh_from_db()
            self.assertEqual(self.run.error, "")
            self.assertEqual(fake.submitted, ["entry", "protection"])
            live_tick(self.run.pk, fake)  # protection retained, no duplicate order
            self.assertEqual(len(fake.submitted), 2)
            Run.objects.filter(pk=self.run.pk).update(flatten_requested=True, entries_paused=True)
            live_tick(self.run.pk, fake)  # cancel protective list
            self.assertTrue(fake.cancelled)
            live_tick(self.run.pk, fake)  # reconcile cancellation before exit sizing
            self.assertEqual(fake.submitted, ["entry", "protection", "exit"])
            live_tick(self.run.pk, fake)  # confirmed exit, flat and stopped
        self.run.refresh_from_db()
        self.assertEqual(self.run.error, "")
        self.assertEqual(self.run.status, "stopped")
        self.assertEqual(dec(self.run.state["wallets"]["ai_trend"]["qty"]), 0)
        self.assertEqual(self.run.fills.count(), 2)
        self.assertEqual(self.run.state["wallets"]["ai_trend"]["closed_trades"], 1)


class LifecycleBroker:
    """An exchange fixture with actual balance transitions and OCO state."""
    def __init__(self):
        self.qty, self.cash = dec(0), dec(1000)
        self.orders, self.fill_rows, self.submitted = {}, {}, []
        self.cancelled = False
        self.info = {"symbol": "BTCUSDT", "baseAsset": "BTC", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": ".00001", "minQty": ".00001", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": ".01"}, {"filterType": "NOTIONAL", "minNotional": "5"}]}

    def account(self):
        return {"canTrade": True, "balances": [{"asset": "BTC", "free": str(self.qty), "locked": "0"}, {"asset": "USDT", "free": str(self.cash), "locked": "0"}]}

    def symbol_info(self, symbol):
        return self.info

    def book(self, symbol):
        return {"bids": [["100", "100"]], "asks": [["100.1", "100"]]}

    def permissions(self):
        return {"enableWithdrawals": False}

    def submit(self, kind, payload):
        self.submitted.append(kind)
        if kind == "protection":
            response = {"listOrderStatus": "EXECUTING", "orders": [{"orderId": 2}, {"orderId": 3}]}
            self.orders[payload["listClientOrderId"]] = response
            return response
        quantity = dec(payload["quantity"])
        price = dec("100.1") if kind == "entry" else dec("100")
        gross, order_id = quantity*price, 1 if kind == "entry" else 4
        fee = gross/1000
        self.qty += quantity if kind == "entry" else -quantity
        self.cash += -gross-fee if kind == "entry" else gross-fee
        trade = {"id": order_id, "orderId": order_id, "qty": str(quantity), "price": str(price), "quoteQty": str(gross),
                 "commission": str(fee), "commissionAsset": "USDT", "isBuyer": kind == "entry", "time": int(timezone.now().timestamp()*1000)}
        self.fill_rows[order_id] = [trade]
        response = {"orderId": order_id, "status": "FILLED", "executedQty": str(quantity)}
        self.orders[payload["newClientOrderId"]] = response
        return response

    def query(self, kind, symbol, client_id):
        return self.orders[client_id]

    def trades(self, symbol, order_id):
        return self.fill_rows.get(order_id, [])

    def cancel_protection(self, symbol, client_id):
        self.orders[client_id]["listOrderStatus"] = "ALL_DONE"
        self.cancelled = True

    def call(self, method, path, params):
        return {"orderId": params["orderId"], "executedQty": "0", "status": "CANCELED"}
