"""Synthetic fixtures verify causality/accounting, never advertised performance."""
import copy
import json
import math
import random
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from trading.configuration import fingerprint, validate_config
from trading.engine import features, initial_state, proposal, step
from trading.ml import FEATURE_NAMES, FEATURE_VERSION, MODEL_VERSION, feature_vector, predict_vector, prediction
from trading.ml_training import fit_model, samples, train_symbol
from trading.models import Candle, Job, MarketModel
from trading.services import create_paper, readiness


def bars(count=5000):
    rng, price, result = random.Random(41), 100, []
    for i in range(count):
        opened = price
        price *= math.exp(rng.gauss(0, .0025))
        result.append({"opened_at": 1700000040+i*60, "closed_at": 1700000040+(i+1)*60,
            "open": opened, "close": price, "low": min(opened, price)*.999, "high": max(opened, price)*1.001})
    return result


def artifact(config=None):
    return {"algorithm": MODEL_VERSION, "feature_version": FEATURE_VERSION, "features": FEATURE_NAMES,
        "weights": [0.0]*10, "intercept": 4.0, "mean": [0.0]*10, "scale": [1.0]*10,
        "version": "fixture-model-v1", "config": config or validate_config(), "threshold": .55,
        "horizon_minutes": 15, "expires_at": timezone.now().timestamp()+86400}


class ModelTrainingTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bars = bars()
        cls.config = validate_config()
        cls.model, cls.report = fit_model(cls.bars, cls.config)

    def test_outcome_labels_are_purged_at_both_chronological_boundaries(self):
        parts = self.report["partitions"]
        self.assertLess(parts["train"]["last_label_at"], self.bars[3000]["opened_at"])
        self.assertLess(parts["validation"]["last_label_at"], self.bars[4000]["opened_at"])
        self.assertGreaterEqual(parts["validation"]["decision_start"], self.bars[3000]["opened_at"])
        self.assertGreaterEqual(parts["test"]["decision_start"], self.bars[4000]["opened_at"])
        self.assertGreater(self.report["test"]["samples"], 500)

    def test_altering_test_future_cannot_change_scaler_weights_or_candidate_selection(self):
        changed = copy.deepcopy(self.bars)
        for i, bar in enumerate(changed[4000:]):
            for key in ("open", "close", "high", "low"):
                bar[key] *= 1+i*.0001
        other, report = fit_model(changed, self.config)
        for key in ("weights", "mean", "scale", "intercept", "C", "threshold"):
            self.assertEqual(self.model[key], other[key])
        self.assertEqual(self.report["candidates"], report["candidates"])

    def test_exported_json_prediction_matches_training_formula_and_is_explainable(self):
        model = json.loads(json.dumps(self.model, allow_nan=False))
        values = feature_vector([b["close"] for b in self.bars[:61]])
        result = predict_vector(model, values)
        import numpy as np
        from scipy.special import expit
        expected = expit(np.dot((np.asarray(values)-np.asarray(model["mean"]))/np.asarray(model["scale"]), model["weights"])+model["intercept"])
        self.assertAlmostEqual(result["probability"], expected, places=13)
        self.assertEqual(len(result["drivers"]), 3)
        self.assertEqual(set(result["inputs"]), set(FEATURE_NAMES))

    def test_cost_labels_charge_both_sides_and_reject_windows_crossing_missing_minutes(self):
        data = [{"opened_at": i*60, "closed_at": (i+1)*60, "open": 100+i*.01, "close": 100+i*.01} for i in range(150)]
        free = samples(data, self.config | {"fee_bps": 0, "slippage_bps": 0, "spread_bps": 0})
        paid = samples(data, self.config)
        self.assertTrue(all(free[1]))
        self.assertFalse(any(paid[1]))
        broken = samples(data[:75]+data[76:], self.config)
        self.assertLess(len(broken[0]), len(paid[0])-1)

    def test_holdout_compares_same_capital_and_costs_against_both_baselines(self):
        metrics = self.report["holdout"]["metrics"]
        self.assertEqual(set(metrics), {"trend", "rsi", "ml"})
        self.assertTrue(all(m["open_quantity"] == 0 for m in metrics.values()))
        for m in metrics.values():
            self.assertAlmostEqual(m["return_pct"], m["net"]/self.config["capital"]*100)
            if m["closed_trades"]:
                self.assertGreater(m["fees"], 0)


class ModelPaperTests(SimpleTestCase):
    def setUp(self):
        self.config = validate_config()
        self.now = timezone.now().timestamp()
        self.state = initial_state(self.config, ("ml",))
        self.market = self.state["market"]
        self.market.update(ml_model=artifact(self.config), closes=[100+i*.01 for i in range(61)],
            last_closed_at=self.now, last_opened=self.now-60,
            book={"bids": [["100", "100"]], "asks": [["100.01", "10"]]}, book_at=self.now,
            tape={str(int(self.now)): [1000, 10]}, derivatives={"funding_rate": .0001, "at": self.now})

    def test_expired_stale_incompatible_cost_and_gap_context_block_predictions(self):
        self.assertIn("probability", prediction(self.market, self.now, self.config))
        self.assertIn("unavailable", prediction(self.market, self.now+91, self.config))
        self.assertIn("unavailable", prediction(self.market, self.now, self.config | {"fee_bps": 11}))
        self.assertIn("unavailable", prediction(self.market, self.market["ml_model"]["expires_at"]+1, self.config))
        step(self.state, {"id": 1, "kind": "gap", "at": self.now, "payload": {}}, self.config)
        self.assertNotIn("ml_prediction", self.market)
        self.assertIn("unavailable", prediction(self.market, self.now, self.config))

    def test_model_entry_requires_a_later_book_and_fresh_context_then_exits_at_horizon(self):
        book = self.market["book"]
        decisions, fills = step(self.state, {"id": 1, "kind": "book", "at": self.now, "payload": book}, self.config)
        self.assertEqual(decisions[0]["action"], "buy")
        self.assertEqual(fills, [])
        _, fills = step(self.state, {"id": 2, "kind": "book", "at": self.now+1, "payload": book}, self.config)
        self.assertEqual(fills[0]["side"], "buy")
        wallet = self.state["wallets"]["ml"]
        action, reason = proposal("ml", wallet, self.market, features(self.market, self.now+901, self.config), self.now+901, self.config)
        self.assertEqual(action, "sell")
        self.assertIn("horizon", reason)

    def test_missing_live_context_blocks_a_positive_model(self):
        self.market.pop("derivatives")
        f = features(self.market, self.now, self.config)
        action, reason = proposal("ml", self.state["wallets"]["ml"], self.market, f, self.now, self.config)
        self.assertEqual(action, "hold")
        self.assertIn("funding_rate", reason)


@override_settings(SECURE_SSL_REDIRECT=False, OPENAI_MODEL="")
class ModelPersistenceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="model-fixture")
        self.client.force_login(self.owner)

    def saved_model(self, version):
        now = timezone.now()
        return MarketModel.objects.create(symbol="BTCUSDT", version=version,
            artifact=artifact() | {"version": version}, report={"holdout": {}}, data_end=now-timedelta(minutes=1))

    def test_paper_freezes_exact_model_and_retraining_cannot_replace_it(self):
        first = self.saved_model("version-one")
        run = create_paper(self.owner, "BTCUSDT", {})
        self.saved_model("version-two")
        run.refresh_from_db()
        self.assertEqual(run.market_model_id, first.id)
        self.assertEqual(run.state["market"]["ml_model"]["version"], first.version)
        self.assertEqual(set(run.state["wallets"]), {"trend", "rsi", "ai_trend", "ml"})
        self.assertEqual(run.config_hash, fingerprint(run.config, "", first.version))
        self.assertFalse(readiness(run)["eligible"])
        report = readiness(run)
        self.assertFalse(next(c for c in report["checks"] if c["label"] == "Market-model execution scope")["passed"])

    @patch("trading.ml_training.fit_model")
    def test_training_snapshot_excludes_candles_imported_after_request_cutoff(self, fit):
        cutoff = timezone.now()-timedelta(seconds=5)
        for i, fetched in enumerate([cutoff-timedelta(seconds=1), cutoff+timedelta(seconds=1)]):
            opened = cutoff-timedelta(minutes=3-i)
            Candle.objects.create(symbol="BTCUSDT", opened_at=opened, closed_at=opened+timedelta(minutes=1), fetched_at=fetched,
                payload={"opened_at": opened.timestamp(), "closed_at": (opened+timedelta(minutes=1)).timestamp(), "open": "100", "close": "100", "low": "100", "high": "100"})
        fit.return_value = artifact(), {"test": {}, "holdout": {"metrics": {}}, "assessment": "Fixture only"}
        saved = train_symbol("BTCUSDT", cutoff=cutoff)
        self.assertEqual(len(fit.call_args.args[0]), 1)
        self.assertEqual(saved.report["provenance"]["cutoff"], cutoff.timestamp())

    def test_training_queue_is_authenticated_bounded_and_deduplicated_per_asset(self):
        def post(count):
            return self.client.post("/api/jobs/", json.dumps({"kind": "train", "symbol": "BTCUSDT", "count": count}), content_type="application/json")
        for count in (True, 4999, 100001):
            self.assertEqual(post(count).status_code, 400)
        self.assertEqual(post(100000).status_code, 202)
        self.assertEqual(post(100000).status_code, 400)
        self.assertEqual(Job.objects.get().owner, self.owner)
        self.client.logout()
        self.assertEqual(post(100000).status_code, 401)
