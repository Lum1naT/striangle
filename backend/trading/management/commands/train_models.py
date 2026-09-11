import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from trading.ml_training import train_symbol
from trading.models import Job


class Command(BaseCommand):
    help = "Train and save paper-only models on recorded history for all four assets"

    def add_arguments(self, parser):
        parser.add_argument("--symbol", choices=settings.SYMBOLS)
        parser.add_argument("--count", type=int, default=100000)
        parser.add_argument("--enqueue", action="store_true", help="Run on the research worker, preserving the web service's CPU and memory")

    def handle(self, *args, **options):
        cutoff = timezone.now()
        if not 5000 <= options["count"] <= 100000:
            from django.core.management.base import CommandError
            raise CommandError("Training count must be 5,000–100,000")
        for symbol in [options["symbol"]] if options["symbol"] else settings.SYMBOLS:
            if options["enqueue"]:
                job = Job.objects.filter(kind="train", status__in=["queued", "running"], params__symbol=symbol).first()
                if job is None:
                    job = Job.objects.create(kind="train", params={"symbol": symbol, "count": options["count"], "cutoff": cutoff.timestamp()})
                self.stdout.write(json.dumps({"symbol": symbol, "job_id": str(job.id), "status": job.status}))
                continue
            model = train_symbol(symbol, options["count"], cutoff=cutoff,
                progress=lambda stage, s=symbol: self.stdout.write(f"{s}: {stage}"))
            self.stdout.write(json.dumps({"symbol": symbol, "model_id": str(model.id), "version": model.version,
                "candles": model.report["candles"], "test": model.report["test"],
                "holdout_metrics": model.report["holdout"]["metrics"], "assessment": model.report["assessment"]}))
