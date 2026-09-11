import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from trading.configuration import fingerprint, validate_config
from trading.models import Event, Job, Run
from trading.research import as_event
from trading.services import create_paper, process_run, readiness


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False, OPENAI_MODEL="", LIVE_TRADING_ENABLED=False)
class APITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password="fixture-long-password")
        self.other = get_user_model().objects.create_user(username="other", password="fixture-other-password")
        self.client.force_login(self.user)
        self.config = validate_config()

    def post(self, path, payload):
        return self.client.post(path, json.dumps(payload), content_type="application/json")

    def test_authentication_and_private_cache_headers(self):
        self.client.logout()
        response = self.client.get("/api/dashboard/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response["Cache-Control"])

    def test_csrf_is_required_for_login_and_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.get("/api/session/")
        payload = json.dumps({"username": "owner", "password": "fixture-long-password"})
        self.assertEqual(client.post("/api/login/", payload, content_type="application/json").status_code, 403)
        token = client.cookies["csrftoken"].value
        response = client.post("/api/login/", payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/dashboard/").status_code, 200)
        self.assertEqual(client.post("/api/runs/", '{"symbol":"BTCUSDT"}', content_type="application/json").status_code, 403)

    def test_login_throttle_persists_across_requests(self):
        self.client.logout()
        for _ in range(10):
            self.assertEqual(self.post("/api/login/", {"username": "owner", "password": "incorrect"}).status_code, 401)
        self.assertEqual(self.post("/api/login/", {"username": "owner", "password": "incorrect"}).status_code, 429)

    def test_user_cannot_read_control_or_export_another_users_run(self):
        run = create_paper(self.other, "BTCUSDT", {})
        for path in (f"/api/runs/{run.id}/", f"/api/runs/{run.id}/fills.csv", f"/api/runs/{run.id}/readiness/"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.post(f"/api/runs/{run.id}/control/", {"action": "stop"}).status_code, 404)
        self.assertEqual(self.client.get("/api/dashboard/").json()["runs"], [])

    def test_api_creates_only_paper_runs_with_frozen_configuration(self):
        response = self.post("/api/runs/", {"symbol": "BTCUSDT", "config": {"capital": 1000}})
        self.assertEqual(response.status_code, 201)
        run = Run.objects.get(pk=response.json()["id"])
        self.assertEqual(run.mode, "paper")
        self.assertEqual(len(run.state["wallets"]), 3)
        self.assertEqual(run.config_hash, fingerprint(run.config, ""))
        self.assertEqual(self.post("/api/runs/", {"symbol": "BTCUSDT", "mode": "live"}).status_code, 400)

    def test_invalid_validation_id_is_a_client_error(self):
        self.assertEqual(self.post("/api/runs/", {"symbol": "BTCUSDT", "validation_id": "bad-id"}).status_code, 400)

    def test_jobs_are_bounded_before_queuing(self):
        self.assertEqual(self.post("/api/jobs/", {"kind": "history", "symbol": "BTCUSDT", "count": 10000000}).status_code, 400)
        self.assertEqual(self.post("/api/jobs/", {"kind": "history", "symbol": "BTCUSDT", "count": 1000}).status_code, 202)
        self.assertEqual(Job.objects.count(), 1)

    def test_all_four_assets_can_be_viewed_and_used_for_paper(self):
        symbols = ["BTCUSDT", "XRPUSDT", "SOLUSDT", "ETHUSDT"]
        self.assertEqual([m["symbol"] for m in self.client.get("/api/dashboard/").json()["markets"]], symbols)
        for symbol in symbols:
            response = self.post("/api/runs/", {"symbol": symbol})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["symbol"], symbol)

    def test_large_training_imports_are_queued_for_all_assets_with_a_fixed_cutoff(self):
        symbols = ["BTCUSDT", "XRPUSDT", "SOLUSDT", "ETHUSDT"]
        response = self.post("/api/jobs/", {"kind": "history", "symbols": symbols, "count": 525600, "end": 1700000000})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(response.json()["ids"]), 4)
        for job in Job.objects.all():
            self.assertEqual(job.params["count"], 525600)
            self.assertEqual(job.params["end_ms"], 1699999999999)
        for count in (True, 1000001, -1):
            self.assertEqual(self.post("/api/jobs/", {"kind": "history", "symbol": "SOLUSDT", "count": count}).status_code, 400)

    def test_stop_flat_paper_run_is_persisted(self):
        run = create_paper(self.user, "BTCUSDT", {})
        self.assertEqual(self.post(f"/api/runs/{run.id}/control/", {"action": "stop"}).status_code, 200)
        process_run(run.id)
        run.refresh_from_db()
        self.assertEqual(run.status, "stopped")
        self.assertIsNotNone(run.ended_at)

    def test_live_readiness_fails_when_evidence_or_operator_enablement_is_missing(self):
        run = create_paper(self.user, "BTCUSDT", {})
        report = readiness(run)
        self.assertFalse(report["eligible"])
        self.assertFalse(next(c for c in report["checks"] if c["label"] == "Operator enablement")["passed"])

    def test_imported_publication_time_is_not_knowledge_time(self):
        now = timezone.now()
        row = Event.objects.create(source="rss", source_id="fixture", symbol="BTCUSDT", kind="news", event_at=now-timedelta(days=1), received_at=now-timedelta(seconds=1), available_at=now, payload={})
        self.assertEqual(as_event(row)["at"], now.timestamp())

    def test_paper_resume_does_not_backfill_preexisting_events(self):
        now = timezone.now()
        row = Event.objects.create(source="fixture", source_id="before-start", symbol="BTCUSDT", kind="book", event_at=now, payload={"bids": [["100", "10"]], "asks": [["101", "10"]]})
        run = create_paper(self.user, "BTCUSDT", {})
        self.assertEqual(run.last_event_id, row.id)
        process_run(run.id)
        self.assertEqual(run.fills.count(), 0)
