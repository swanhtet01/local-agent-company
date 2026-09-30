import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("linux_capacity", Path(__file__).resolve().parents[1] / "deploy" / "inspect_linux_capacity.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LinuxCapacityTests(unittest.TestCase):
    def test_busy_large_host_is_rejected(self):
        result = module.assess("MemTotal: 33554432 kB\nMemAvailable: 2097152 kB", 100 * module.GIB)
        self.assertFalse(result["capacity_pass"])

    def test_sufficient_capacity_does_not_accept_deployment(self):
        result = module.assess("MemTotal: 16777216 kB\nMemAvailable: 8388608 kB", 20 * module.GIB)
        self.assertTrue(result["capacity_pass"])
        self.assertFalse(result["deployment_accepted"])
        self.assertFalse(result["workload_isolation_verified"])

    def test_insufficient_disk(self):
        self.assertFalse(module.assess("MemTotal: 16777216 kB\nMemAvailable: 8388608 kB", 19 * module.GIB)["capacity_pass"])

    def test_invalid_measurements_fail_closed(self):
        for value in ("", "MemTotal: 1 kB", "MemTotal: 1 MB\nMemAvailable: 1 kB", "MemTotal: 1 kB\nMemAvailable: 2 kB", "MemTotal: 1 kB\nMemAvailable: -1 kB", "MemTotal: 1 kB\nMemTotal: 1 kB\nMemAvailable: 1 kB"):
            with self.subTest(value=value), self.assertRaises((KeyError, ValueError)):
                module.assess(value, 100)

    def test_non_linux_receipt_is_redacted_and_failing(self):
        stream = io.StringIO()
        with patch.object(module.platform, "system", return_value="Windows"), contextlib.redirect_stdout(stream):
            self.assertEqual(module.main(), 1)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["status"], "inspection_unavailable")
        self.assertFalse(result["capacity_pass"])

    def test_io_failure_does_not_print_private_path(self):
        stream = io.StringIO()
        with patch.object(module.platform, "system", return_value="Linux"), patch.object(module.Path, "read_text", side_effect=OSError("private-path")), contextlib.redirect_stdout(stream):
            self.assertEqual(module.main(), 1)
        self.assertNotIn("private-path", stream.getvalue())
