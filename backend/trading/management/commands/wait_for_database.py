import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Wait for the web service's initial database migration before starting workers"

    def handle(self, *args, **options):
        deadline = time.monotonic()+180
        while time.monotonic() < deadline:
            try:
                if "trading_event" in connection.introspection.table_names() and "trading_liveorder" in connection.introspection.table_names():
                    return
            except Exception:
                connection.close()
            time.sleep(2)
        raise CommandError("Database schema is not ready; inspect web pre-deploy migrations")
