import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import run_tests


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_tests.py"


class TestRunnerTests(unittest.TestCase):
    def test_runner_is_cwd_independent_without_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONWARNINGS", None)

        with tempfile.TemporaryDirectory() as temporary_directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--pattern",
                    "test_live_build.py",
                ],
                cwd=temporary_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["schema"], run_tests.SCHEMA)
        self.assertEqual(summary["status"], "passed")
        self.assertGreater(summary["tests_run"], 0)
        self.assertEqual(summary["verbosity"], "concise")
        self.assertNotIn("test_fetch_health", completed.stderr)

    def test_verbose_mode_prints_individual_test_names(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONWARNINGS", None)

        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--pattern",
                "test_live_build.py",
                "--verbose",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["verbosity"], "verbose")
        self.assertIn("test_fetch_health", completed.stderr)

    def test_configure_import_paths_is_idempotent(self):
        src = str(ROOT / "src")
        root = str(ROOT)
        original = sys.path.copy()
        try:
            sys.path = [value for value in sys.path if value not in {src, root}]
            run_tests.configure_import_paths(ROOT)
            run_tests.configure_import_paths(ROOT)
            self.assertEqual(sys.path.count(src), 1)
            self.assertEqual(sys.path.count(root), 1)
        finally:
            sys.path = original


if __name__ == "__main__":
    unittest.main()
