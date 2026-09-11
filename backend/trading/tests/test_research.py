from copy import deepcopy

from django.test import SimpleTestCase

from trading.configuration import validate_config
from trading.research import bar_test, candidate_configs, chronological_research, replay


def bars(count=200):
    result = []
    for i in range(count):
        price = 100+i*.15 if i % 40 < 20 else 100+(count-i)*.1
        result.append({"opened_at": i*60, "closed_at": (i+1)*60, "open": str(price), "high": str(price+1), "low": str(price-1), "close": str(price+.2), "volume": "100"})
    return result


class ResearchTests(SimpleTestCase):
    def test_changed_holdout_cannot_change_selected_parameters(self):
        configs = [validate_config({"fast": 2, "slow": 4}), validate_config({"fast": 5, "slow": 10})]
        original = bars()
        changed = deepcopy(original)
        for b in changed[140:]:
            for key in ("open", "high", "low", "close"):
                b[key] = str(float(b[key])*10)
        choice_a, a = chronological_research(original, configs, "candles", "trend")
        choice_b, b = chronological_research(changed, configs, "candles", "trend")
        self.assertEqual(choice_a, choice_b)
        self.assertEqual(a["train"], b["train"])
        self.assertLess(a["train_end"], a["holdout_start"])

    def test_historical_candle_mode_does_not_invent_ai_results(self):
        result = bar_test(bars(), validate_config({"fast": 2, "slow": 4}))
        self.assertEqual(set(result["metrics"]), {"trend", "rsi"})
        self.assertGreater(result["metrics"]["trend"]["fees"], 0)
        self.assertEqual(result["metrics"]["trend"]["open_quantity"], 0)
        with self.assertRaises(ValueError):
            chronological_research(bars(), [validate_config()], "candles", "ai_trend")

    def test_entry_is_next_open_not_same_close(self):
        data = [{"opened_at": i*60, "closed_at": (i+1)*60, "open": str(100+i), "high": str(100+i), "low": str(100+i), "close": str(100+i), "volume": "1000"} for i in range(10)]
        result = bar_test(data, validate_config({"fast": 2, "slow": 3, "fee_bps": 0, "spread_bps": 0, "slippage_bps": 0, "stop_pct": 20, "take_pct": 50}))
        first = next(f for f in result["fills"] if f["side"] == "buy")
        self.assertEqual(first["at"], 180)
        self.assertEqual(float(first["price"]), 103)

    def test_parameter_search_is_bounded_and_validated(self):
        with self.assertRaises(ValueError):
            candidate_configs(validate_config(), list(range(10)), list(range(10)))
        self.assertEqual(len(candidate_configs(validate_config(), [2, 2], [3, 3])), 1)

    def test_replay_empty_observations_does_not_create_trades(self):
        result = replay([], validate_config())
        self.assertEqual(result["coverage"]["fills"], 0)
        self.assertEqual(result["metrics"]["ai_trend"]["equity"], 10000)
