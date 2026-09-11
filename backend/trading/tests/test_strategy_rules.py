"""Causality, shared predicates, risk enforcement and durable baseline evidence."""
import random
from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from trading.auto_config import candidate_config, margin_config, validate_auto
from trading.auto_paper import choose, step_portfolio, tick
from trading.auto_rules import make_candidate, model_keys
from trading.auto_search import backtest, price_features, signal_series
from trading.auto_signals import signal, strategies
from trading.autonomy import configure, public_report, run_cycle, schedule_cycle
from trading.margin import entry_quantity, new_wallet, open_position
from trading.models import AutoCycle, AutoRecord, AutonomyPolicy, Candle, Event
from trading.tests.test_autonomy import event, portfolio
from trading.tests.test_ml import artifact, bars


class CombinationRulesTests(SimpleTestCase):
    def setUp(self):
        self.cfg = margin_config(validate_auto())
        self.rng = random.Random(19)

    def test_paper_predicates_match_historical_series_for_every_rule_family(self):
        data = bars(160)
        vectors = price_features(data)
        models = {f"{h}:{s}": artifact() for h in (15, 60) for s in (1, -1)}
        from trading.ml import predict_vector
        predictions = {k: [(predict_vector(a, v) or {}).get("probability", -1) for v in vectors] for k, a in models.items()}
        covered_entry, covered_exit = set(), set()
        for _ in range(50):
            c = make_candidate(self.rng, self.cfg)
            covered_entry.add(c["entry_kind"]); covered_exit.add(c["exit_kind"])
            series = signal_series(data, c, predictions, vectors)
            for i in (59, 60, 90, 159):
                actual = signal(c, [b["close"] for b in data[max(0, i-60):i+1]], models)
                self.assertEqual((actual["enter"], actual["exit_long"], actual["exit_short"]), tuple(s[i] for s in series))
        self.assertEqual(covered_entry, {"trend", "rsi", "breakout", "ml"})
        self.assertEqual(covered_exit, {"reverse", "rsi", "momentum", "confidence", "time"})

    def test_rules_cannot_raise_risk_limits_or_leverage(self):
        c = make_candidate(self.rng, self.cfg)
        for change in ({"leverage": 11}, {"stop_pct": 3}, {"take_pct": 5}, {"stop_pct": float("nan")},
                {"take_pct": True}, {"horizon": 10000}, {"horizon": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                candidate_config(self.cfg, c | change)
        config = candidate_config(self.cfg, c | {"stop_pct": .5, "take_pct": 1, "horizon": 240})
        for key in ("capital", "risk_per_trade_pct", "allocation_pct", "max_leverage", "max_drawdown_pct", "daily_loss_pct"):
            self.assertEqual(config[key], self.cfg[key])
        wallet = new_wallet(10000)
        short_hold = config | {"horizon_minutes": 15}
        self.assertLess(entry_quantity(wallet, 100, 10, config), entry_quantity(wallet, 100, 10, short_hold))
        self.assertIsNone(open_position(wallet, 1, entry_quantity(wallet, 100, 10, short_hold), 100, 10, 0, config))

    def test_selected_stop_and_horizon_apply_in_both_backtest_and_paper(self):
        c = make_candidate(self.rng, self.cfg) | {"stop_pct": .5, "take_pct": 1, "horizon": 240, "side": 1, "leverage": 2}
        data = [{"opened_at": i*60, "closed_at": (i+1)*60, "open": 100, "close": 100, "low": 100, "high": 100} for i in range(3)]
        data[1]["low"] = 99.4
        scores = backtest(data, ([1, 0, 0], [False]*3, [False]*3), c, self.cfg, 0, 180)
        self.assertEqual(scores["closed_trades"], 1)
        self.assertLess(scores["return_pct"], 0)
        now = timezone.now().timestamp()
        p = portfolio(now); p["candidate"] = c
        p["models"] = {key: artifact() for key in model_keys(c)}
        open_position(p["wallet"], 1, 1, 100, 2, now, candidate_config(self.cfg, c))
        p["market"]["book"]["bids"][0][0] = "99.4"
        action, reason, inputs = choose(p, self.cfg, now+1)
        self.assertEqual(action, 2); self.assertIn("stop", reason)
        self.assertEqual(inputs["stop_pct"], .5)
        p["market"].pop("book")
        self.assertIn("240-minute", choose(p, self.cfg, now+240*60)[1])

    def test_indicator_gap_blocks_combination_entries(self):
        now = timezone.now().timestamp(); p = portfolio(now)
        c = make_candidate(self.rng, self.cfg)
        p["candidate"], p["models"] = c, {key: artifact() for key in model_keys(c)}
        step_portfolio(p, event(1, "gap", {}, now), self.cfg, now)
        self.assertEqual(choose(p, self.cfg, now)[0], 0)
        self.assertIn("61 consecutive", signal(c, p["market"]["closes"], p["models"])["reason"])


@override_settings(SYMBOLS=["BTCUSDT"])
class SearchPersistenceTests(TestCase):
    def test_complete_cycle_freezes_choices_and_keeps_all_search_evidence(self):
        data = bars()
        cutoff = datetime.fromtimestamp(data[-1]["closed_at"]+1, dt_timezone.utc)
        policy = configure({"max_leverage": 2})
        cycle = AutoCycle.objects.create(config=policy.config, cutoff=cutoff, report={"trigger": "manual"})
        Candle.objects.bulk_create([Candle(symbol="BTCUSDT", interval="1m", payload=b,
            opened_at=datetime.fromtimestamp(b["opened_at"], dt_timezone.utc),
            closed_at=datetime.fromtimestamp(b["closed_at"], dt_timezone.utc), fetched_at=cutoff) for b in data])
        run_cycle(cycle.id)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, "ready")
        report = cycle.report
        self.assertEqual(report["search_trial_count"], 96)
        self.assertEqual(report["total_combinations"], 120)
        self.assertEqual(len(report["fitting"]["BTCUSDT"]["optimization"]["trials"]), 96)
        for row in report["comparisons"]["BTCUSDT"].values():
            if row:
                self.assertIn("holdout", row)
                self.assertEqual(row["holdout"]["quantity"], 0)
        if report["champion"]:
            self.assertEqual(report["champion"]["candidate"], report["comparisons"]["BTCUSDT"]["selected"]["candidate"])
            self.assertEqual(report["holdout"], report["comparisons"]["BTCUSDT"]["selected"]["holdout"])

    def test_manual_search_deduplicates_and_keeps_frozen_cutoff(self):
        policy = configure(); now = timezone.now()
        AutonomyPolicy.objects.filter(pk=1).update(last_cutoff=now)
        self.assertIsNone(schedule_cycle())
        cycle = schedule_cycle(force=True)
        self.assertEqual(cycle.report["trigger"], "manual")
        self.assertIsNone(schedule_cycle(force=True))
        original = cycle.cutoff
        cycle.refresh_from_db(); self.assertEqual(cycle.cutoff, original)

    def test_selected_and_baseline_decisions_both_survive_and_deduplicate(self):
        policy = configure(); now = timezone.now()
        candidate = strategies()[0] | {"leverage": 1}
        row = {"candidate": candidate}
        cycle = AutoCycle.objects.create(config=policy.config, cutoff=now, status="ready", artifacts={"BTCUSDT": {"15:1": artifact()}},
            report={"source": "binance_spot_proxy", "allocated_symbol": None, "by_asset": {"BTCUSDT": row},
                "comparisons": {"BTCUSDT": {"baseline": row}}})
        tick(); cycle.refresh_from_db()
        self.assertEqual(cycle.state["portfolios"]["BTCUSDT"]["wallet"]["cash"], cycle.state["portfolios"]["BTCUSDT"]["baseline"]["wallet"]["cash"])
        Event.objects.create(source="fixture", source_id="book", kind="perp_book", symbol="BTCUSDT", event_at=now,
            payload={"bids": [["100", "1"]], "asks": [["100.01", "1"]]})
        tick(); self.assertEqual(AutoRecord.objects.count(), 2)
        self.assertEqual(set(AutoRecord.objects.values_list("role", flat=True)), {"selected", "baseline"})
        tick(); self.assertEqual(AutoRecord.objects.count(), 2)

    def test_full_dashboard_omits_large_trial_ledger_but_keeps_round_evidence(self):
        report = {"candidates": [1], "fitting": {"BTCUSDT": {"models": {}, "optimization": {"trials": [1, 2], "rounds": [1]}}}}
        result = public_report(report)
        self.assertNotIn("candidates", result)
        self.assertNotIn("trials", result["fitting"]["BTCUSDT"]["optimization"])
        self.assertEqual(result["fitting"]["BTCUSDT"]["optimization"]["rounds"], [1])
        self.assertIn("trials", report["fitting"]["BTCUSDT"]["optimization"])
