import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Wait for database connectivity or the web service's initial migration"

    def add_arguments(self, parser):
        parser.add_argument("--connection-only", action="store_true", help="Check connectivity before running migrations")

    def handle(self, *args, **options):
        deadline = time.monotonic()+300
        while time.monotonic() < deadline:
            try:
                if options["connection_only"]:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                    return
                executor = MigrationExecutor(connection)
                if not executor.migration_plan(executor.loader.graph.leaf_nodes()):
                    return
            except Exception:
                connection.close()
            time.sleep(2)
        if options["connection_only"]:
            raise CommandError("Database is not accepting connections after 300 seconds")
        raise CommandError("Database schema is not ready; inspect web pre-deploy migrations")
