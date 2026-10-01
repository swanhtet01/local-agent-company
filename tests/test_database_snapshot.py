import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("snapshot_database", Path(__file__).resolve().parents[1] / "deploy/snapshot_database.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SnapshotTests(unittest.TestCase):
    def test_permission_failure_prevents_data_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'company.db'
            target = Path(directory) / 'snapshot.db'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE private_records(value TEXT)')
                db.execute("INSERT INTO private_records VALUES ('private fixture')")
                db.commit()
            with patch.object(module, 'protect_destination', side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    module.snapshot(source, target)
            self.assertEqual(target.stat().st_size, 0)

    def test_hot_wal_restores_latest_committed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "company.db"
            target = Path(directory) / "snapshot.db"
            connection = sqlite3.connect(source)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE receipts(value TEXT)")
            connection.execute("INSERT INTO receipts VALUES ('accepted-receipt')")
            connection.commit()
            try:
                result = module.snapshot(source, target)
                self.assertEqual(result["status"], "PASS")
                restored = sqlite3.connect(target)
                self.assertEqual(restored.execute("SELECT value FROM receipts").fetchall(), [("accepted-receipt",)])
                restored.close()
                with self.assertRaises(FileExistsError):
                    module.snapshot(source, target)
                with self.assertRaises(ValueError):
                    module.snapshot(source, source)
            finally:
                connection.close()

    def test_missing_source_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing.db"
            target = Path(directory) / "snapshot.db"
            with self.assertRaises(FileNotFoundError):
                module.snapshot(source, target)
            self.assertFalse(source.exists())
            self.assertFalse(target.exists())
