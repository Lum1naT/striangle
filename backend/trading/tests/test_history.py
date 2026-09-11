import csv
import io
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from trading.history import import_history
from trading.models import Candle, Job
from trading.providers import ProviderError, candle_batches
from trading.research import recover_jobs, work_one_job


BASE = 1_700_000_040_000


def raw_bar(i):
    return [BASE+i*60000, "100", "102", "99", "101", "5", BASE+(i+1)*60000-1]


def batch(start, end):
    return [{"opened_at": (BASE+i*60000)/1000, "closed_at": (BASE+(i+1)*60000)/1000,
             "open": "100", "high": "102", "low": "99", "close": "101", "volume": "5"} for i in range(start, end)]


class HistoryPaginationTests(SimpleTestCase):
    @patch("trading.providers.time.sleep")
    @patch("trading.providers.http_json")
    def test_more_than_5000_closed_candles_with_no_missing_page_boundary(self, transport, sleep):
        rows = [raw_bar(i) for i in range(7002)]
        requests = []
        def respond(url):
            if url.endswith("time"):
                return {"serverTime": BASE+7001*60000+30000}
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["symbol"], ["XRPUSDT"])
            limit, end = int(query["limit"][0]), int(query["endTime"][0])
            requests.append((limit, end))
            return [r for r in rows if r[0] <= end][-limit:]
        transport.side_effect = respond
        result = sorted((bar for page, _ in candle_batches("XRPUSDT", 6001) for bar in page), key=lambda b: b["opened_at"])
        self.assertEqual(len(result), 6001)
        self.assertEqual(result[0]["opened_at"], raw_bar(1000)[0]/1000)
        self.assertEqual(result[-1]["closed_at"], (BASE+7001*60000)/1000)
        self.assertTrue(all(b["opened_at"] == a["closed_at"] for a, b in zip(result, result[1:])))
        self.assertTrue(all(limit <= 1000 for limit, _ in requests))
        self.assertEqual(len(requests), 7)

    @patch("trading.providers.time.sleep")
    @patch("trading.providers.http_json")
    def test_fixed_past_end_and_resume_cursor_do_not_skip_one_minute(self, transport, sleep):
        rows = [raw_bar(i) for i in range(2500)]
        def respond(url):
            if url.endswith("time"):
                return {"serverTime": BASE+3000*60000}
            query = parse_qs(urlparse(url).query)
            return [r for r in rows if r[0] <= int(query["endTime"][0])][-int(query["limit"][0]):]
        transport.side_effect = respond
        first, cursor = next(candle_batches("SOLUSDT", 1000, end_ms=raw_bar(1999)[6]))
        older = [b for p, _ in candle_batches("SOLUSDT", 1000, end_ms=cursor) for b in p]
        self.assertEqual(older[-1]["closed_at"], first[0]["opened_at"])
        self.assertEqual(first[-1]["closed_at"], (raw_bar(1999)[6]+1)/1000)

    @patch("trading.providers.time.sleep")
    @patch("trading.providers.http_json")
    def test_provider_repeating_a_page_fails_instead_of_looping(self, transport, sleep):
        transport.side_effect = [{"serverTime": BASE+200*60000}, [raw_bar(i) for i in range(100)], [raw_bar(i) for i in range(100)]]
        with self.assertRaises(ProviderError):
            list(candle_batches("ETHUSDT", 150))


@override_settings(SECURE_SSL_REDIRECT=False)
class HistoryPersistenceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("historian")
        self.client.force_login(self.owner)
        self.job = Job.objects.create(owner=self.owner, kind="history", params={"symbol": "SOLUSDT", "count": 50, "end_ms": raw_bar(49)[6]})

    @patch("trading.history.candle_batches")
    def test_partial_failure_can_resume_without_duplicates_or_a_gap(self, pages):
        def interrupted(*args, **kwargs):
            yield batch(25, 50), raw_bar(25)[0]-1
            raise ProviderError("Temporary provider failure")
        pages.side_effect = interrupted
        self.assertTrue(work_one_job())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "failed")
        self.assertEqual(Candle.objects.count(), 25)
        self.assertEqual(self.job.result["candles"], 25)
        response = self.client.post(f"/api/jobs/{self.job.id}/resume/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 202)
        pages.side_effect = None
        pages.return_value = [(batch(0, 25), raw_bar(0)[0]-1)]
        work_one_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "completed")
        self.assertEqual(self.job.result["candles"], 50)
        self.assertEqual(self.job.result["missing_minutes"], 0)
        self.assertEqual(Candle.objects.count(), 50)
        self.assertEqual(pages.call_args.args, ("SOLUSDT", 25))
        self.assertEqual(pages.call_args.kwargs["end_ms"], raw_bar(25)[0]-1)
        pages.return_value = [(batch(0, 50), raw_bar(0)[0]-1)]
        import_history("SOLUSDT", 50)
        self.assertEqual(Candle.objects.count(), 50)
        exported = self.client.get("/api/candles.csv?symbol=SOLUSDT")
        rows = list(csv.DictReader(io.StringIO(b"".join(exported.streaming_content).decode())))
        self.assertEqual(len(rows), 50)
        self.assertTrue(all(a["time"] < b["time"] for a, b in zip(rows, rows[1:])))
        self.assertTrue(all(row["source"] == "binance_spot" and row["fetched_at"] > row["closed_at"] for row in rows))

    @patch("trading.history.candle_batches")
    def test_failed_checkpoint_transaction_cannot_skip_uncommitted_candles(self, pages):
        pages.return_value = [(batch(25, 50), raw_bar(25)[0]-1), (batch(0, 25), raw_bar(0)[0]-1)]
        original = Job.save
        def failing_save(job, *args, **kwargs):
            original(job, *args, **kwargs)
            if kwargs.get("update_fields") == ["result"] and job.result["candles"] == 50:
                raise RuntimeError("Failure before page commit")
        with patch.object(Job, "save", failing_save):
            work_one_job()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "failed")
        self.assertEqual(self.job.result["candles"], 25)
        self.assertEqual(Candle.objects.count(), 25)

    @patch("trading.history.candle_batches")
    def test_shutdown_saves_fetched_page_and_requeues_the_job(self, pages):
        stop = threading.Event()
        def interrupted(*args, **kwargs):
            stop.set()
            yield batch(25, 50), raw_bar(25)[0]-1
        pages.side_effect = interrupted
        work_one_job(stop)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertIsNone(self.job.finished_at)
        self.assertEqual(self.job.result["candles"], 25)
        self.assertEqual(Candle.objects.count(), 25)

    def test_restart_recovers_imports_but_does_not_reuse_interrupted_backtests(self):
        self.job.status = "running"
        self.job.result = {"candles": 25, "next_end_ms": raw_bar(25)[0]-1}
        self.job.save()
        research = Job.objects.create(owner=self.owner, kind="backtest", status="running")
        recover_jobs()
        self.job.refresh_from_db()
        research.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertEqual(self.job.result["candles"], 25)
        self.assertEqual(research.status, "failed")

    def test_export_and_resume_require_the_correct_account(self):
        other = get_user_model().objects.create_user("other-historian")
        self.client.force_login(other)
        response = self.client.post(f"/api/jobs/{self.job.id}/resume/", "{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get("/api/candles.csv?symbol=SOLUSDT").status_code, 401)
