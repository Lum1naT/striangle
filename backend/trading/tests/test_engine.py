from copy import deepcopy

from django.test import SimpleTestCase

from trading.configuration import dec, validate_config
from trading.engine import features, initial_state, metrics, signal, simulate_fill, step


def event(n, kind, at, payload):
    return {"id": n, "kind": kind, "at": at, "payload": payload}


def book(bid="100", ask="100.1", size="100"):
    return {"bids": [[bid, size]], "asks": [[ask, size]]}


class EngineTests(SimpleTestCase):
    def setUp(self):
        self.config = validate_config({"fast": 2, "slow": 3, "rsi_period": 2, "capital": 1000})
        self.state = initial_state(self.config)
        self.state["market"]["closes"] = [98, 99, 100]

    def decision(self, n=2, at=600.1):
        return step(self.state, event(n, "candle", at, {"opened_at": 540, "closed_at": 600, "close": "102"}), self.config)

    def test_signal_cannot_fill_on_its_own_candle(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        decisions, fills = self.decision()
        self.assertEqual(next(d for d in decisions if d["strategy"] == "trend")["action"], "buy")
        self.assertEqual(fills, [])
        self.assertEqual(dec(self.state["wallets"]["trend"]["qty"]), 0)
        _, fills = step(self.state, event(3, "book", 600.2, book()), self.config)
        self.assertEqual(fills[0]["side"], "buy")
        self.assertGreater(dec(fills[0]["price"]), dec("100.1"))

    def test_missing_ai_does_not_fall_back_to_baseline(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        decisions, _ = self.decision()
        ai = next(d for d in decisions if d["strategy"] == "ai_trend")
        self.assertEqual(ai["action"], "hold")
        self.assertIn("news_score", ai["features"]["missing"])

    def test_future_assessment_does_not_change_prior_decision(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        decisions, _ = self.decision()
        original = deepcopy(decisions)
        step(self.state, event(3, "assessment", 610, {"score": 1, "summary": "later", "sources": []}), self.config)
        self.assertEqual(decisions, original)
        self.assertIsNone(next(d for d in decisions if d["strategy"] == "ai_trend")["features"]["assessment_id"])

    def test_stale_book_blocks_entry(self):
        step(self.state, event(1, "book", 590, book()), self.config)
        decisions, _ = self.decision()
        self.assertEqual(next(d for d in decisions if d["strategy"] == "trend")["action"], "hold")

    def test_pending_entry_expires_during_outage(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        self.decision()
        _, fills = step(self.state, event(3, "book", 640, book()), self.config)
        self.assertEqual(fills, [])
        self.assertIsNone(self.state["wallets"]["trend"]["pending"])

    def test_delayed_worker_cannot_backfill_live_paper_orders(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        self.decision()
        _, fills = step(self.state, event(3, "book", 600.2, book()), self.config, execution_now=650)
        self.assertEqual(fills, [])

    def test_duplicate_event_is_idempotent(self):
        e = event(1, "book", 600, book())
        step(self.state, e, self.config)
        before = deepcopy(self.state)
        self.assertEqual(step(self.state, e, self.config), ([], []))
        self.assertEqual(self.state, before)

    def test_gap_resets_order_book_and_warmup(self):
        step(self.state, event(1, "book", 600, book()), self.config)
        self.decision()
        step(self.state, event(3, "gap", 601, {}), self.config)
        self.assertNotIn("book", self.state["market"])
        self.assertEqual(self.state["market"]["closes"], [])
        self.assertIsNone(self.state["wallets"]["trend"]["pending"])

    def test_round_trip_cash_includes_both_fees_and_slippage(self):
        wallet = self.state["wallets"]["trend"]
        buy = simulate_fill(wallet, "buy", book(), self.config, 1, "entry")
        sell = simulate_fill(wallet, "sell", book("105", "105.1"), self.config, 2, "exit")
        expected = dec(1000)-dec(buy["quantity"])*dec(buy["price"])-dec(buy["fee"])+dec(sell["quantity"])*dec(sell["price"])-dec(sell["fee"])
        self.assertAlmostEqual(dec(wallet["cash"]), expected, places=20)
        self.assertEqual(wallet["closed_trades"], 1)
        self.assertEqual(dec(wallet["qty"]), 0)
        self.assertEqual(dec(wallet["fees"]), dec(buy["fee"])+dec(sell["fee"]))

    def test_partial_exit_preserves_remaining_position_and_cost(self):
        wallet = self.state["wallets"]["trend"]
        simulate_fill(wallet, "buy", book(), self.config, 1, "entry")
        old_qty, old_cost = dec(wallet["qty"]), dec(wallet["cost_basis"])
        fill = simulate_fill(wallet, "sell", book("99", "100", "0.1"), self.config, 2, "exit")
        self.assertTrue(fill["details"]["partial"])
        self.assertEqual(dec(wallet["qty"]), old_qty-dec(fill["quantity"]))
        self.assertAlmostEqual(dec(wallet["cost_basis"])/dec(wallet["qty"]), old_cost/old_qty, places=20)
        self.assertEqual(wallet["closed_trades"], 0)

    def test_pause_blocks_entry_but_allows_protective_exit(self):
        wallet = self.state["wallets"]["trend"]
        simulate_fill(wallet, "buy", book(), self.config, 1, "entry")
        _, fills = step(self.state, event(1, "book", 600, book("90", "90.1")), self.config, paused=True)
        self.assertEqual(fills, [])
        _, fills = step(self.state, event(2, "book", 601, book("89", "89.1")), self.config, paused=True)
        self.assertEqual(fills[0]["side"], "sell")
        self.assertLess(dec(fills[0]["price"]), dec(89))

    def test_ai_context_gate_uses_fresh_real_features(self):
        f = {"slow_sma": 100, "fast_sma": 101, "rsi": 55, "missing": [], "news_score": .6,
             "imbalance": .2, "flow_imbalance": .1, "funding_rate": .0001, "heatmap": None,
             "liquidated_shorts_usd": 0, "liquidated_longs_usd": 0}
        self.assertEqual(signal("ai_trend", f, False, self.config)[0], "buy")
        self.assertEqual(signal("ai_trend", f | {"funding_rate": .01}, False, self.config)[0], "hold")
        self.assertEqual(signal("ai_trend", f | {"liquidated_shorts_usd": 2000000}, False, self.config)[0], "hold")

    def test_live_event_processing_does_not_simulate_real_fills(self):
        step(self.state, event(1, "book", 600, book()), self.config, execute=False)
        self.decision()
        _, fills = step(self.state, event(3, "book", 600.2, book()), self.config, execute=False)
        self.assertEqual(fills, [])
        self.assertEqual(dec(self.state["wallets"]["trend"]["qty"]), 0)

    def test_nan_and_unbounded_settings_are_rejected(self):
        for raw in ({"capital": float("nan")}, {"allocation_pct": 101}, {"fast": 2.5}, {"execute": "anything"}):
            with self.assertRaises(ValueError):
                validate_config(raw)
