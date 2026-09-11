from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from trading.configuration import dec, validate_config
from trading.engine import initial_state, simulate_fill, step
from trading.models import Candle, Event
from trading.services import create_paper, process_run
from trading.tests.test_engine import book, event


class ContinuousEngineTests(SimpleTestCase):
    def setUp(self):
        self.config = validate_config({"fast": 2, "slow": 3, "rsi_period": 2})
        self.state = initial_state(self.config)
        self.state["market"].update(closes=[98, 99, 100], last_opened=540, last_closed_at=600)

    def supportive_context(self):
        self.state["market"].update(tape={"600": [1000, 0]}, derivatives={"at": 600, "funding_rate": 0, "open_interest": 100},
            assessment={"at": 600, "score": 1, "summary": "Recorded support", "sources": []})
        self.config["min_imbalance"] = -1

    def test_fresh_book_can_signal_between_candles_but_never_fill_itself(self):
        decisions, fills = step(self.state, event(1, "book", 601, book()), self.config)
        self.assertEqual(next(d for d in decisions if d["strategy"] == "trend")["action"], "buy")
        self.assertEqual(fills, [])
        _, fills = step(self.state, event(2, "book", 602, book()), self.config)
        self.assertEqual(next(f for f in fills if f["strategy"] == "trend")["side"], "buy")
        self.assertEqual(self.state["market"]["closes"], [98, 99, 100])

    def test_adverse_news_reacts_without_waiting_for_a_candle(self):
        self.supportive_context()
        wallet = self.state["wallets"]["ai_trend"]
        simulate_fill(wallet, "buy", book(), self.config, 600, "Earlier entry")
        step(self.state, event(1, "book", 601, book()), self.config)
        decisions, fills = step(self.state, event(2, "assessment", 601.1, {"score": -1, "summary": "Adverse update", "sources": []}), self.config)
        self.assertEqual(next(d for d in decisions if d["strategy"] == "ai_trend")["action"], "sell")
        self.assertEqual(fills, [])
        _, fills = step(self.state, event(3, "book", 601.2, book()), self.config)
        self.assertEqual(next(f for f in fills if f["strategy"] == "ai_trend")["side"], "sell")

    def test_entry_is_revalidated_against_flow_before_filling(self):
        self.supportive_context()
        step(self.state, event(1, "book", 601, book()), self.config)
        self.assertEqual(self.state["wallets"]["ai_trend"]["pending"]["action"], "buy")
        step(self.state, event(2, "flow", 601.1, {"buy_notional": 0, "sell_notional": 10000}), self.config)
        _, fills = step(self.state, event(3, "book", 601.2, book()), self.config)
        self.assertFalse(any(f["strategy"] == "ai_trend" for f in fills))
        self.assertIsNone(self.state["wallets"]["ai_trend"]["pending"])

    def test_database_delay_does_not_make_an_old_book_fresh(self):
        step(self.state, event(1, "book", 601, book()), self.config)
        delayed = event(2, "book", 602, book()) | {"received_at": 580}
        _, fills = step(self.state, delayed, self.config, execution_now=602)
        self.assertEqual(fills, [])
        self.assertEqual(dec(self.state["wallets"]["trend"]["qty"]), 0)

    def test_fresh_quotes_do_not_hide_a_stalled_candle_feed(self):
        decisions, fills = step(self.state, event(1, "book", 700, book()), self.config)
        self.assertFalse(any(d["action"] == "buy" for d in decisions))
        self.assertEqual(fills, [])

    def test_protective_exits_bypass_signal_throttle_and_cannot_immediately_reenter(self):
        self.config["decision_interval_seconds"] = 60
        wallet = self.state["wallets"]["trend"]
        simulate_fill(wallet, "buy", book(), self.config, 600, "Earlier entry")
        step(self.state, event(1, "book", 601, book()), self.config)
        decisions, _ = step(self.state, event(2, "book", 601.1, book("90", "90.1")), self.config)
        self.assertTrue(any(d["reason"] == "Stop loss triggered." for d in decisions))
        _, fills = step(self.state, event(3, "book", 601.2, book("90", "90.1")), self.config)
        self.assertTrue(any(f["side"] == "sell" and f["strategy"] == "trend" for f in fills))
        self.assertIsNone(wallet["pending"])

    def test_unchanged_holds_update_live_status_without_flooding_journal(self):
        audit = []
        for n in range(1, 30):
            decisions, _ = step(self.state, event(n, "book", 600+n, book()), self.config)
            audit.extend(d for d in decisions if d["strategy"] == "ai_trend")
        self.assertEqual(len(audit), 1)
        self.assertEqual(self.state["latest_signals"]["ai_trend"]["at"], 629)

    def test_old_frozen_runs_keep_their_candle_close_cadence(self):
        self.config.pop("decision_interval_seconds")
        decisions, fills = step(self.state, event(1, "book", 601, book()), self.config)
        self.assertEqual((decisions, fills), ([], []))


@override_settings(SECURE_SSL_REDIRECT=False, OPENAI_MODEL="")
class RealtimeAPITests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("realtime-owner")
        self.other = get_user_model().objects.create_user("realtime-other")
        self.client.force_login(self.owner)
        self.now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
        self.config = validate_config({"fast": 2, "slow": 3, "rsi_period": 2})

    def candle(self, minutes_ago, close, *, fetched=None):
        end = self.now-timedelta(minutes=minutes_ago)
        return Candle.objects.create(symbol="BTCUSDT", opened_at=end-timedelta(minutes=1), closed_at=end,
            fetched_at=fetched or self.now-timedelta(seconds=1), payload={"close": str(close)})

    def start(self):
        with patch("trading.services.timezone.now", return_value=self.now):
            return create_paper(self.owner, "BTCUSDT", self.config)

    def quote(self, n, seconds=0):
        at = self.now+timedelta(seconds=seconds)
        return Event.objects.create(source="fixture", source_id=str(n), symbol="BTCUSDT", kind="book",
            event_at=at, received_at=at, available_at=at, payload=book())

    def test_startup_uses_only_already_available_completed_consecutive_candles(self):
        for minutes, close in ((3, 98), (2, 99), (1, 100)):
            self.candle(minutes, close)
        self.candle(0, 999, fetched=self.now+timedelta(seconds=1))
        self.candle(-1, 999)
        run = self.start()
        self.assertEqual(run.state["market"]["closes"], [98, 99, 100])
        self.assertEqual(run.state["warmup"]["seeded_candles"], 3)
        self.assertEqual(run.fills.count(), 0)
        self.assertEqual(run.decisions.count(), 0)
        self.assertEqual(run.state["curve"], [])
        self.assertNotIn("evidence", run.state)

    def test_warmup_stops_at_a_missing_minute(self):
        for minutes in (0, 1, 3, 4):
            self.candle(minutes, 100)
        self.assertEqual(self.start().state["warmup"]["seeded_candles"], 2)

    def test_warmed_run_trades_only_on_new_books_and_exposes_processing_delay(self):
        for minutes, close in ((2, 98), (1, 99), (0, 100)):
            self.candle(minutes, close)
        old = self.quote(0, -1)
        run = self.start()
        self.assertEqual(run.last_event_id, old.id)
        self.quote(1, 1)
        with patch("trading.services.timezone.now", return_value=self.now+timedelta(seconds=1.2)):
            process_run(run.id)
        self.assertEqual(run.fills.count(), 0)
        self.quote(2, 2)
        with patch("trading.services.timezone.now", return_value=self.now+timedelta(seconds=2.2)):
            process_run(run.id)
        self.assertTrue(run.fills.filter(strategy="trend", side="buy").exists())
        detail = self.client.get(f"/api/realtime/?run_id={run.id}").json()["detail"]
        self.assertAlmostEqual(detail["runtime"]["processing_delay_seconds"], .2, places=5)
        self.assertEqual(detail["runtime"]["seeded_candles"], 3)

    def test_idle_worker_does_not_rewrite_portfolio_state(self):
        run = self.start()
        before = deepcopy(run.state)
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(process_run(run.id))
        run.refresh_from_db()
        self.assertEqual(run.state, before)
        self.assertFalse(any(q["sql"].startswith("UPDATE") for q in queries))

    def test_live_endpoint_skips_historical_dataset_queries_and_protects_ownership(self):
        run = self.start()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/realtime/?run_id={run.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["markets"]), 4)
        self.assertFalse(any('"trading_candle"' in q["sql"] for q in queries))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/api/realtime/?run_id={run.id}").status_code, 404)
        self.client.logout()
        response = self.client.get("/api/realtime/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response["Cache-Control"])
