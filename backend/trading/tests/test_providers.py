import json
from unittest.mock import Mock

from django.test import SimpleTestCase

from trading.ai import assess_news
from trading.providers import ProviderError, candle_payload, normalize_binance, normalize_book, normalize_bybit, parse_rss


class ProviderTests(SimpleTestCase):
    def test_only_closed_candles_are_recorded(self):
        message = {"stream": "btcusdt@kline_1m", "data": {"e": "kline", "k": {"x": False}}}
        self.assertIsNone(normalize_binance(message, 60.1))
        message["data"]["k"] = {"x": True, "t": 0, "T": 59999, "o": "100", "h": "102", "l": "99", "c": "101", "v": "5"}
        row = normalize_binance(message, 60.1)
        self.assertEqual(row["payload"]["closed_at"], 60)
        self.assertEqual(row["received_at"], 60.1)

    def test_binance_maker_flag_is_not_taker_buy(self):
        message = {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "p": "100", "q": "1", "m": True, "a": 5, "T": 60000}}
        self.assertFalse(normalize_binance(message, 60.1)["payload"]["taker_buy"])

    def test_partial_book_is_a_sorted_snapshot(self):
        body = normalize_book([["99", "1"], ["100", "2"]], [["102", "3"], ["101", "4"]])
        self.assertEqual(body["bids"][0][0], "100")
        self.assertEqual(body["asks"][0][0], "101")
        self.assertEqual(body["coverage"], "top_20_snapshot")
        with self.assertRaises(ValueError):
            normalize_book([["102", "1"]], [["101", "1"]])

    def test_liquidation_side_and_price_basis(self):
        message = {"topic": "allLiquidation.BTCUSDT", "ts": 60000, "data": [{"s": "BTCUSDT", "p": "100", "v": "2", "S": "Buy", "T": 59999}]}
        row = normalize_bybit(message, {}, 60.1)[0]
        self.assertEqual(row["payload"]["position_side"], "long")
        self.assertEqual(row["payload"]["price_basis"], "bankruptcy_price")

    def test_derivative_delta_merges_snapshot(self):
        cache = {}
        first = {"topic": "tickers.BTCUSDT", "ts": 60000, "type": "snapshot", "data": {"fundingRate": ".001", "openInterest": "100"}}
        normalize_bybit(first, cache, 60)
        second = {"topic": "tickers.BTCUSDT", "ts": 61000, "type": "delta", "data": {"openInterest": "102"}}
        row = normalize_bybit(second, cache, 61)[0]
        self.assertEqual(row["payload"]["funding_rate"], .001)
        self.assertEqual(row["payload"]["open_interest"], 102)

    def test_rss_rejects_unsafe_links_and_future_publications(self):
        xml = b'<rss><channel><item><title>Safe</title><link>https://news.example/a</link><pubDate>Thu, 01 Jan 1970 00:00:00 GMT</pubDate></item><item><title>Bad</title><link>javascript:alert(1)</link><pubDate>Thu, 01 Jan 1970 00:00:00 GMT</pubDate></item></channel></rss>'
        self.assertEqual(len(parse_rss(xml, 100)), 1)

    def test_ai_refuses_unknown_sources_and_incomplete_output(self):
        articles = [{"id": 1, "title": "Example", "url": "https://news.example/a"}]
        content = {"score": .5, "summary": "Context", "source_ids": [2], "event_risk": "low"}
        transport = Mock(return_value={"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(content)}]}]})
        with self.assertRaises(ProviderError):
            assess_news("BTCUSDT", articles, api_key="fixture", model="fixture", transport=transport)
        transport.return_value = {"status": "incomplete", "output": []}
        with self.assertRaises(ProviderError):
            assess_news("BTCUSDT", articles, api_key="fixture", model="fixture", transport=transport)

    def test_ai_output_cannot_change_risk_and_high_event_risk_vetoes_entry(self):
        content = {"score": .8, "summary": "Acute event", "source_ids": [1], "event_risk": "high"}
        transport = Mock(return_value={"status": "completed", "id": "fixture", "model": "fixture-snapshot", "output": [{"content": [{"type": "output_text", "text": json.dumps(content)}]}]})
        result = assess_news("BTCUSDT", [{"id": 1, "title": "Example", "url": "https://news.example/a"}], api_key="fixture", model="fixture", transport=transport)
        self.assertEqual(result["score"], 0)
        request = transport.call_args.kwargs["payload"]
        self.assertNotIn("tools", request)
        self.assertFalse(request["store"])
        self.assertEqual(result["model"], "fixture-snapshot")
