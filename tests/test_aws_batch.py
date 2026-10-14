"""Verify the cloud runner's failure, locking, and restore safeguards."""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from infra.aws import batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = (True,)
        self.environment = patch.dict(os.environ, {
            "DB_HOST": "database", "DB_USER": "user", "DB_PASSWORD": "password",
            "DB_NAME": "hecate", "HECATE_EXPORT_BUCKET": "backups", "DB_AUTH_MODE": "password",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, arguments):
        with patch.object(sys, "argv", ["batch", *arguments]), patch.object(batch, "wait_for_database", return_value=self.connection), patch.object(batch.boto3, "client"):
            return batch.main()

    def test_failed_pipeline_still_backs_up_and_fails(self):
        with patch.object(batch, "run_command", side_effect=subprocess.CalledProcessError(1, "pipeline")), patch.object(batch, "backup") as backup:
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke([])
            backup.assert_called_once()
        self.connection.close.assert_called_once()

    def test_overlap_does_not_run_or_backup(self):
        self.cursor.fetchone.return_value = (False,)
        with patch.object(batch, "run_command") as run, patch.object(batch, "backup") as backup:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.invoke([])
            run.assert_not_called()
            backup.assert_not_called()
        self.connection.close.assert_called_once()

    def test_restore_rejects_nonempty_database(self):
        self.cursor.fetchone.side_effect = [(True,), (5,)]
        with patch.object(batch, "run_command") as run:
            with self.assertRaisesRegex(RuntimeError, "empty warehouse"):
                self.invoke(["--restore-key", "migration.dump"])
            run.assert_not_called()
        self.connection.close.assert_called_once()

    def test_iam_token_refreshed_for_child_process(self):
        environment = {"DB_AUTH_MODE": "iam", "DB_HOST": "database", "DB_USER": "user"}
        with patch.object(batch.boto3, "client") as client, patch.object(batch.subprocess, "run"):
            client.return_value.generate_db_auth_token.return_value = "fresh-token"
            batch.run_command(["dbt"], environment)
        self.assertEqual(environment["DB_PASSWORD"], "fresh-token")
        self.assertEqual(environment["PGPASSWORD"], "fresh-token")
