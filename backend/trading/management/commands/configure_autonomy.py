import json

from django.core.management.base import BaseCommand, CommandError

from trading.autonomy import configure, schedule_cycle
from trading.models import AutonomyPolicy


class Command(BaseCommand):
    help = "Configure and start or stop automatic model research and futures PAPER trading"

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--enable", action="store_true")
        action.add_argument("--disable", action="store_true")
        parser.add_argument("--max-leverage", type=int)
        parser.add_argument("--interval-hours", type=int)
        parser.add_argument("--history-candles", type=int)
        parser.add_argument("--min-new-candles", type=int)

    def handle(self, *args, **options):
        current = AutonomyPolicy.objects.filter(pk=1).first()
        config = dict(current.config) if current else {}
        config.update({k: options[k] for k in ("max_leverage", "interval_hours", "history_candles", "min_new_candles") if options[k] is not None})
        try:
            policy = configure(config, enabled=options["enable"])
            cycle = schedule_cycle() if policy.enabled else None
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps({"enabled": policy.enabled, "max_leverage": policy.config["max_leverage"],
            "interval_hours": policy.config["interval_hours"], "cycle_id": str(cycle.id) if cycle else None,
            "execution": "paper_only", "status": policy.status}))
