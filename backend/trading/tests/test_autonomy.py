"""Synthetic evidence for accounting, chronology and worker persistence only."""
import copy
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from trading.auto_config import candidate_config, leverage_value, margin_config, validate_auto
from trading.auto_paper import choose, step_portfolio, tick
from trading.auto_search import backtest, rank_candidates, search_asset
from trading.auto_signals import signal, strategies
from trading.autonomy import configure, history_query, schedule_cycle, summary
from trading.margin import accrue_funding, close_position, entry_quantity, liquidatable, liquidation_price, mark, new_wallet, open_position
from trading.models import AutoCycle, AutoRecord, AutonomyPolicy, Candle, Event, FuturesCandle, Job
from trading.providers import normalize_bybit
from trading.research import recover_jobs, work_one_job
from trading.tests.test_ml import artifact, bars


def portfolio(now, leverage=10):
    return {"candidate": strategies()[0] | {"leverage": leverage}, "models": {"15:1": artifact()}, "candle_kind": "candle",
        "wallet": new_wallet(10000), "market": {"closes": [100+i*.001 for i in range(61)], "last_opened": now-60,
            "last_closed_at": now, "tape": {str(int(now)): [10000, 10]}, "liquidations": {},
            "book": {"bids": [["100", "100"]], "asks": [["100.01", "20"]]}, "book_at": now,
            "mark_price": 100, "mark_at": now, "derivatives": {"funding_rate": .0001, "at": now}}}


def event(i, kind, body, at):
    return {"id": i, "kind": kind, "payload": body, "at": at, "event_at": at, "received_at": at}


class MarginTests(SimpleTestCase):
    def setUp(self):
        self.cfg = margin_config(validate_auto())

    def test_ten_times_cap_is_rechecked_in_sizing_and_order_accounting(self):
        for bad in (True, False, 0, 10.5, 11, 100, float("nan"), "Infinity"):
            with self.subTest(bad=bad):
                for operation in (lambda: validate_auto({"max_leverage": bad}), lambda: entry_quantity(new_wallet(10000), 100, bad, self.cfg),
                    lambda: open_position(new_wallet(10000), 1, 1, 100, bad, 0, self.cfg)):
                    with self.assertRaises(ValueError): operation()
        self.assertEqual(leverage_value(10, 100), 10)
        with self.assertRaises(ValueError): leverage_value(4, 3)

    def test_long_short_realize_symmetric_price_pnl_and_both_fees(self):
        for side in (1, -1):
            w = new_wallet(10000)
            self.assertIsNotNone(open_position(w, side, 1, 100, 10, 0, self.cfg))
            self.assertAlmostEqual(w["cash"], 9989.9)
            self.assertAlmostEqual(w["margin"], 10)
            result = close_position(w, 100+side*5, 60, self.cfg)
            self.assertAlmostEqual(w["cash"], 10000+5-.1-(100+side*5)*.001)
            self.assertEqual(w["closed_trades"], 1)
            self.assertEqual(w["margin"], 0)
            self.assertAlmostEqual(result["round_pnl"], w["cash"]-10000)

    def test_partial_close_and_funding_charge_only_remaining_notional(self):
        w = new_wallet(10000)
        open_position(w, 1, 2, 100, 10, 0, self.cfg)
        self.assertAlmostEqual(accrue_funding(w, 28800, self.cfg), .02)
        close_position(w, 105, 28800, self.cfg, quantity=1)
        self.assertEqual(w["closed_trades"], 0)
        self.assertAlmostEqual(w["margin"], 9.99)
        self.assertAlmostEqual(accrue_funding(w, 57600, self.cfg), .01)
        self.assertEqual(accrue_funding(w, 50000, self.cfg), 0)
        close_position(w, 95, 57600, self.cfg)
        self.assertAlmostEqual(w["cash"], 10000-.2-.105-.095-.03)
        self.assertEqual(w["closed_trades"], 1)

    def test_mark_liquidation_isolated_collateral_loss_preserves_free_cash(self):
        for side in (1, -1):
            w = new_wallet(10000)
            open_position(w, side, 1, 100, 10, 0, self.cfg)
            price = liquidation_price(w, self.cfg)-side*.01
            self.assertTrue(liquidatable(w, price, self.cfg))
            free = w["cash"]
            close_position(w, price, 1, self.cfg, liquidated=True)
            self.assertEqual(w["cash"], free)
            self.assertEqual(w["quantity"], 0)
            self.assertEqual(w["liquidations"], 1)

    def test_risk_budget_caps_notional_even_at_ten_times_leverage(self):
        w = new_wallet(10000)
        qty = entry_quantity(w, 100, 10, self.cfg)
        self.assertLess(qty*100, 2200)
        self.assertIsNone(open_position(w, 1, qty*1.1, 100, 10, 0, self.cfg))
        self.assertIsNotNone(open_position(w, 1, qty, 100, 10, 0, self.cfg))

    def test_daily_halt_resets_on_next_day_but_drawdown_halt_does_not(self):
        w = new_wallet(10000); mark(w, 100, 0, self.cfg)
        w["cash"] = 9600; mark(w, 100, 10, self.cfg)
        self.assertEqual(w["halted"], "daily loss limit")
        mark(w, 100, 86400, self.cfg); self.assertFalse(w["halted"])
        w["cash"] = 8000; mark(w, 100, 86401, self.cfg)
        mark(w, 100, 172800, self.cfg)
        self.assertEqual(w["halted"], "maximum drawdown limit")


class AutonomousSearchTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.data, cls.policy = bars(), validate_auto({"max_leverage": 2})
        cls.start, cls.end = cls.data[3000]["opened_at"], cls.data[4000]["opened_at"]
        cls.models, cls.fitting, cls.rows = search_asset("BTCUSDT", cls.data, cls.policy, cls.start, cls.end)

    def test_holdout_changes_cannot_change_models_candidate_results_or_ranking(self):
        changed = copy.deepcopy(self.data)
        for i, b in enumerate(changed[4000:]):
            for k in ("open", "close", "low", "high"): b[k] *= 1+i*.002
        models, fitting, rows = search_asset("BTCUSDT", changed, self.policy, self.start, self.end)
        self.assertEqual(models, self.models)
        self.assertEqual(rows, self.rows)
        self.assertEqual(fitting, self.fitting)
        for part in fitting["models"].values():
            self.assertLess(part["last_training_label"], fitting["optimization"]["search_start"])
            self.assertLess(part["last_validation_label"], fitting["optimization"]["search_start"])

    def test_search_contains_all_directions_and_each_allowed_leverage(self):
        self.assertEqual(sum(r["origin"] == "baseline" for r in self.rows), 24)
        self.assertEqual(sum(r["origin"] == "scikit_search" for r in self.rows), 12)
        self.assertEqual({r["candidate"]["leverage"] for r in self.rows}, {1, 2})
        self.assertEqual(set(self.models), {"15:1", "15:-1", "60:1", "60:-1"})
        for r in self.rows:
            self.assertEqual(r["metrics"]["quantity"], 0)
        for c in strategies():
            self.assertIn(signal(c, [b["close"] for b in self.data[:61]], self.models)["enter"], (-1, 0, 1))
        json.dumps(self.rows, allow_nan=False)

    def test_later_validation_cannot_train_the_search_or_price_models(self):
        changed = copy.deepcopy(self.data)
        for i, bar in enumerate(changed[3000:]):
            for key in ("open", "close", "high", "low"):
                bar[key] *= 1+i*.002
        models, fitting, rows = search_asset("BTCUSDT", changed, self.policy, self.start, self.end)
        self.assertEqual(models, self.models)
        self.assertEqual(fitting, self.fitting)
        self.assertEqual([r["candidate"] for r in rows], [r["candidate"] for r in self.rows])
        self.assertNotEqual([r["metrics"] for r in rows], [r["metrics"] for r in self.rows])

    def test_search_fits_feedback_model_and_keeps_exploring(self):
        search = self.fitting["optimization"]
        self.assertEqual(search["algorithm"], "ExtraTreesRegressor")
        self.assertEqual(search["trial_count"], 96)
        self.assertEqual([r["trials"] for r in search["rounds"]], [24, 48, 72, 96])
        self.assertEqual(sum(r["model_guided"] for r in search["rounds"]), 48)
        self.assertEqual(len({t["candidate"]["id"] for t in search["trials"]}), 96)
        self.assertTrue(any(p["importance"] > 0 for p in search["parameter_importance"]))
        for trial in search["trials"]:
            self.assertEqual(trial["surrogate"] is not None, trial["proposal"] == "model_guided")
            cfg = candidate_config(margin_config(self.policy), trial["candidate"])
            self.assertLessEqual(cfg["stop_pct"], self.policy["risk"]["stop_pct"])
            self.assertLessEqual(cfg["take_pct"], self.policy["risk"]["take_pct"])

    def test_ranking_excludes_liquidation_and_resolves_numerical_ties_with_lower_leverage(self):
        row = {"symbol": "BTCUSDT", "candidate": {"id": "fixture", "leverage": 1},
            "metrics": {"return_pct": 1, "max_drawdown_pct": .5, "closed_trades": 5, "liquidations": 0, "halted": ""}}
        high = copy.deepcopy(row); high["candidate"]["leverage"] = 10; high["metrics"]["return_pct"] += 1e-12
        ruined = copy.deepcopy(row); ruined["metrics"].update(liquidations=1, return_pct=100)
        self.assertEqual(rank_candidates([ruined, high, row], 5, 10), [row, high])

    def test_next_open_execution_and_ambiguous_liquidation_precedes_stop(self):
        data = [{"opened_at": i*60, "closed_at": (i+1)*60, "open": 100, "close": 100, "low": 100, "high": 100} for i in range(3)]
        data[1]["low"] = 80
        cfg = margin_config(validate_auto())
        result = backtest(data, ([1, 0, 0], [False]*3, [False]*3), {"leverage": 10, "horizon": 15}, cfg, 0, 180)
        self.assertEqual(result["liquidations"], 1)
        self.assertEqual(result["closed_trades"], 1)
        data[1]["opened_at"] += 60; data[1]["closed_at"] += 60
        result = backtest(data[:2], ([1, 0], [False]*2, [False]*2), {"leverage": 10, "horizon": 15}, cfg, 0, 240)
        self.assertEqual(result["closed_trades"], 0, "A missing candle must cancel the queued entry")


class FuturesPaperTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now().timestamp()
        self.p, self.cfg = portfolio(self.now), margin_config(validate_auto())

    def test_proposal_waits_for_next_book_rechecks_context_and_fills_once(self):
        book = self.p["market"]["book"]
        first = step_portfolio(self.p, event(1, "perp_book", book, self.now), self.cfg, self.now)
        self.assertEqual([r["kind"] for r in first], ["decision"])
        self.assertEqual(self.p["wallet"]["pending"]["side"], 1)
        second = step_portfolio(self.p, event(2, "perp_book", book, self.now+1), self.cfg, self.now+1)
        self.assertEqual(sum(r["kind"] == "fill" for r in second), 1)
        self.assertGreater(self.p["wallet"]["quantity"], 0)
        self.assertFalse(any(r["kind"] == "fill" for r in step_portfolio(self.p, event(3, "perp_book", book, self.now+2), self.cfg, self.now+2)))

    def test_stale_mark_and_gap_and_adverse_news_block_entry(self):
        self.assertEqual(choose(self.p, self.cfg, self.now)[0], 1)
        self.p["market"]["mark_at"] -= 11
        self.assertEqual(choose(self.p, self.cfg, self.now)[0], 0)
        self.p["market"]["mark_at"] = self.now
        self.p["market"]["assessment"] = {"score": -.9, "summary": "Fixture", "sources": [], "at": self.now}
        self.assertEqual(choose(self.p, self.cfg, self.now)[0], 0)
        self.p["market"].pop("assessment")
        step_portfolio(self.p, event(1, "perp_gap", {}, self.now), self.cfg, self.now)
        self.assertEqual(choose(self.p, self.cfg, self.now)[0], 0)
        self.assertNotIn("mark_price", self.p["market"])

    def test_liquidation_uses_mark_even_when_order_book_is_above_stop(self):
        open_position(self.p["wallet"], 1, 1, 100, 10, self.now, self.cfg)
        records = step_portfolio(self.p, event(1, "derivatives", {"mark_price": 89, "funding_rate": 0}, self.now+1), self.cfg, self.now+1)
        self.assertEqual(next(r for r in records if r["kind"] == "fill")["payload"]["action"], "liquidation")
        self.assertEqual(self.p["wallet"]["quantity"], 0)

    def test_flatten_can_exit_with_fresh_book_even_if_mark_feed_is_down(self):
        open_position(self.p["wallet"], 1, .1, 100, 10, self.now, self.cfg)
        book = self.p["market"]["book"]
        step_portfolio(self.p, event(1, "perp_book", book, self.now+20), self.cfg, self.now+20, closing=True)
        step_portfolio(self.p, event(2, "perp_book", book, self.now+21), self.cfg, self.now+21, closing=True)
        self.assertEqual(self.p["wallet"]["quantity"], 0)


@override_settings(SECURE_SSL_REDIRECT=False, SYMBOLS=["BTCUSDT", "XRPUSDT", "SOLUSDT", "ETHUSDT"])
class AutonomyPersistenceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="auto-fixture", is_staff=True)
        self.client.force_login(self.owner)

    def test_schedule_deduplicates_and_waits_for_new_candles_across_every_asset(self):
        policy = configure()
        first = schedule_cycle()
        self.assertIsNotNone(first)
        self.assertIsNone(schedule_cycle())
        self.assertEqual(Job.objects.filter(kind="autotrain").count(), 1)
        first.status = "completed"; first.save()
        now = timezone.now()
        AutonomyPolicy.objects.filter(pk=1).update(last_cutoff=now, next_run_at=now)
        self.assertIsNone(schedule_cycle(now+timedelta(seconds=1)))
        self.assertIn("fresh data", summary()["status"])

    def test_worker_recovers_same_frozen_cutoff_after_restart(self):
        configure(); cycle = schedule_cycle()
        Job.objects.update(status="running")
        AutoCycle.objects.update(status="training")
        recover_jobs()
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, "queued")
        self.assertEqual(Job.objects.get().params["cycle_id"], str(cycle.id))
        with patch("trading.autonomy.run_cycle", return_value={"fixture": True}) as run:
            self.assertTrue(work_one_job())
            self.assertEqual(run.call_args.args[0], str(cycle.id))

    def test_graceful_research_shutdown_requeues_same_cycle(self):
        configure(); cycle = schedule_cycle()
        with patch("trading.autonomy.run_cycle", side_effect=InterruptedError):
            self.assertTrue(work_one_job())
        self.assertEqual(Job.objects.get().status, "queued")
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, "queued")

    def test_snapshot_excludes_later_imports_for_spot_and_futures(self):
        cutoff = timezone.now()
        for table in (Candle, FuturesCandle):
            for i, delta in enumerate((-1, 1)):
                table.objects.create(symbol="BTCUSDT", opened_at=cutoff-timedelta(minutes=3-i), closed_at=cutoff-timedelta(minutes=2-i),
                    fetched_at=cutoff+timedelta(seconds=delta), payload={})
            self.assertEqual(history_query(table, "BTCUSDT", cutoff).count(), 1)

    def test_disable_and_config_change_cancel_queued_cycles_and_reenable_schedules(self):
        configure(); cycle = schedule_cycle()
        configure(enabled=False); cycle.refresh_from_db()
        self.assertEqual(cycle.status, "cancelled")
        self.assertIsNone(schedule_cycle())
        configure(enabled=True)
        self.assertIsNotNone(schedule_cycle())

    def test_staff_only_controls_and_cap_cannot_be_bypassed_by_json(self):
        def post(data): return self.client.post("/api/autonomy/control/", json.dumps(data), content_type="application/json")
        self.assertEqual(post({"enabled": True, "config": {"max_leverage": 11}}).status_code, 400)
        self.assertFalse(AutonomyPolicy.objects.exists())
        self.assertEqual(post({"enabled": True}).status_code, 200)
        self.owner.is_staff = False; self.owner.save()
        self.assertEqual(post({"enabled": False}).status_code, 403)
        self.client.logout()
        self.assertEqual(post({"enabled": False}).status_code, 401)

    def test_ready_cycle_seeds_no_historical_fills_and_continues_from_durable_event_id(self):
        policy = configure()
        now = timezone.now()
        cycle = AutoCycle.objects.create(config=policy.config, cutoff=now, status="ready", artifacts={},
            report={"source": "binance_spot_proxy", "allocated_symbol": None, "by_asset": {s: None for s in ("BTCUSDT", "XRPUSDT", "SOLUSDT", "ETHUSDT")}})
        tick(); cycle.refresh_from_db()
        self.assertEqual(cycle.status, "paper")
        self.assertEqual(len(cycle.state["portfolios"]), 4)
        self.assertFalse(AutoRecord.objects.exists())
        e = Event.objects.create(source="fixture", source_id="1", kind="perp_book", symbol="BTCUSDT", event_at=now,
            payload={"bids": [["100", "1"]], "asks": [["101", "1"]]})
        tick(); cycle.refresh_from_db(); saved = cycle.last_event_id
        self.assertEqual(saved, e.id)
        self.assertEqual(AutoRecord.objects.count(), 1)
        tick(); self.assertEqual(AutoRecord.objects.count(), 1)
        configure(enabled=False); tick(); cycle.refresh_from_db()
        self.assertEqual(cycle.status, "completed")
        self.assertTrue(all(m["return_pct"] == 0 for m in cycle.state["final_results"].values()))


class FuturesProviderTests(SimpleTestCase):
    def test_book_deltas_apply_even_between_emissions_delete_and_reset(self):
        cache = {}
        snap = {"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 100000,
            "data": {"u": 10, "seq": 10, "b": [["100", "2"], ["99", "3"]], "a": [["101", "4"]]}}
        self.assertEqual(normalize_bybit(snap, cache, 100)[0]["kind"], "perp_book")
        delta = {"topic": snap["topic"], "type": "delta", "ts": 100100,
            "data": {"u": 11, "seq": 11, "b": [["100", "0"]], "a": []}}
        self.assertEqual(normalize_bybit(delta, cache, 100.1), [])
        delta["data"] = {"u": 12, "seq": 12, "b": [], "a": [["101", "5"]]}
        row = normalize_bybit(delta, cache, 101.1)[0]
        self.assertEqual(row["payload"]["bids"], [["99", "3"]])
        self.assertEqual(row["payload"]["asks"], [["101", "5"]])
        delta["data"]["seq"] = 5
        self.assertEqual(normalize_bybit(delta, cache, 102.2), [])
        snap["data"]["u"] = 1; snap["data"]["seq"] = 1
        self.assertEqual(normalize_bybit(snap, cache, 103)[0]["payload"]["bids"][0][0], "100")

    def test_future_candles_only_confirmed_and_ticker_mark_delta_retained(self):
        message = {"topic": "kline.1.SOLUSDT", "data": [{"confirm": False}]}
        self.assertEqual(normalize_bybit(message, {}, 60), [])
        message["data"] = [{"confirm": True, "start": 0, "end": 59999, "open": "100", "high": "102", "low": "99", "close": "101", "volume": "1"}]
        self.assertEqual(normalize_bybit(message, {}, 60)[0]["payload"]["closed_at"], 60)
        cache = {}
        msg = {"topic": "tickers.SOLUSDT", "type": "snapshot", "ts": 60000, "data": {"markPrice": "100", "fundingRate": ".0001", "openInterest": "1000"}}
        normalize_bybit(msg, cache, 60)
        msg.update(type="delta", ts=61000, data={"openInterest": "1001"})
        self.assertEqual(normalize_bybit(msg, cache, 61)[0]["payload"]["mark_price"], 100)
