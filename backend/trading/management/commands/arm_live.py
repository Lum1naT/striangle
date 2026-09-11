from django.core.management.base import BaseCommand, CommandError

from trading.live import arm
from trading.models import Run


class Command(BaseCommand):
    help = "Explicitly arm the configured exchange account after paper/holdout evidence gates pass"

    def add_arguments(self, parser):
        parser.add_argument("--paper-run", required=True)
        parser.add_argument("--capital", required=True, help="Must exactly match evaluated paper capital in USDT")
        parser.add_argument("--acknowledge-real-orders", action="store_true")

    def handle(self, *args, **options):
        if not options["acknowledge_real_orders"]:
            raise CommandError("Supply --acknowledge-real-orders after reviewing the account and environment")
        try:
            paper = Run.objects.select_related("owner", "validation").get(pk=options["paper_run"], mode="paper")
            run = arm(paper, options["capital"])
        except (ValueError, Run.DoesNotExist) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Armed run {run.id}. Environment: {'TESTNET' if run.state['testnet'] else 'PRODUCTION'}")
