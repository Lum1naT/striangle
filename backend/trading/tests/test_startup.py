from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase


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
    def test_workers_wait_until_both_required_tables_exist(self, connection, sleep):
        connection.introspection.table_names.side_effect = [[], ["trading_event"], ["trading_event", "trading_liveorder"]]
        call_command("wait_for_database")
        self.assertEqual(sleep.call_count, 2)

    @patch(f"{COMMAND}.time.monotonic", side_effect=[0, 0, 301])
    @patch(f"{COMMAND}.time.sleep")
    @patch(f"{COMMAND}.connection")
    def test_unavailable_database_fails_after_bounded_wait(self, connection, sleep, monotonic):
        connection.cursor.side_effect = OperationalError("unreachable")
        with self.assertRaisesMessage(CommandError, "not accepting connections after 300 seconds"):
            call_command("wait_for_database", connection_only=True)
