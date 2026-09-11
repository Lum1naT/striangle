from unittest.mock import MagicMock, patch
import threading

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase

from trading.locking import single_worker


COMMAND = "trading.management.commands.wait_for_database"


class DatabaseStartupTests(SimpleTestCase):
    @patch(f"{COMMAND}.time.sleep")
    @patch(f"{COMMAND}.connection")
    def test_pre_migration_check_retries_connection_without_requiring_schema(self, connection, sleep):
        connection.cursor.side_effect = [OperationalError("starting"), MagicMock()]
        call_command("wait_for_database", connection_only=True)
        connection.close.assert_called_once()
        sleep.assert_called_once_with(2)
        connection.introspection.table_names.assert_not_called()

    @patch(f"{COMMAND}.time.sleep")
    @patch(f"{COMMAND}.connection")
    @patch(f"{COMMAND}.MigrationExecutor")
    def test_workers_wait_until_latest_code_migrations_are_applied(self, executor, connection, sleep):
        executor.return_value.migration_plan.side_effect = [["0007"], ["0008"], []]
        call_command("wait_for_database")
        self.assertEqual(sleep.call_count, 2)

    @patch(f"{COMMAND}.time.monotonic", side_effect=[0, 0, 301])
    @patch(f"{COMMAND}.time.sleep")
    @patch(f"{COMMAND}.connection")
    def test_unavailable_database_fails_after_bounded_wait(self, connection, sleep, monotonic):
        connection.cursor.side_effect = OperationalError("unreachable")
        with self.assertRaisesMessage(CommandError, "not accepting connections after 300 seconds"):
            call_command("wait_for_database", connection_only=True)


@patch("trading.locking.settings")
@patch("psycopg.connect")
class WorkerOwnershipTests(SimpleTestCase):
    def prepare(self, connect, settings):
        settings.DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "fixture"}}
        return connect.return_value.__enter__.return_value

    @patch("trading.locking.time.sleep")
    def test_replacement_does_no_work_until_previous_owner_releases(self, sleep, connect, settings):
        connection = self.prepare(connect, settings)
        connection.execute.return_value.fetchone.side_effect = [(False,), (True,)]
        processed = []
        sleep.side_effect = lambda _: self.assertEqual(processed, [])
        with single_worker("trader"):
            processed.append("work")
        self.assertEqual(processed, ["work"])
        sleep.assert_called_once_with(1)
        connect.return_value.__exit__.assert_called_once()

    @patch("trading.locking.time.monotonic", side_effect=[0, 421])
    def test_timeout_never_grants_ownership(self, monotonic, connect, settings):
        connection = self.prepare(connect, settings)
        connection.execute.return_value.fetchone.return_value = (False,)
        with self.assertRaisesMessage(CommandError, "still owns the lock"):
            with single_worker("trader"):
                self.fail("Work began without ownership")

    def test_shutdown_while_waiting_exits_without_processing(self, connect, settings):
        connection = self.prepare(connect, settings)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(SystemExit) as stopped:
            with single_worker("research", stop):
                self.fail("Work began during shutdown")
        self.assertEqual(stopped.exception.code, 0)
        connection.execute.assert_not_called()
