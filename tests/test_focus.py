from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_company.cli import main
from local_company.core import Company, MockModel
from local_company.focus import (
    EXECUTION_FOCUS_FILENAME,
    clear_execution_focus,
    enforce_execution_focus,
    read_execution_focus,
    set_execution_focus,
)


class ExecutionFocusTests(unittest.TestCase):
    def test_focus_round_trip_is_bounded_and_clear_preserves_audit_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(read_execution_focus(home)["enabled"])
            active = set_execution_focus(home, "0123456789ab", "SuperMega", 4)
            self.assertTrue(active["enabled"])
            self.assertEqual(read_execution_focus(home), active)
            cleared = clear_execution_focus(home)
            self.assertFalse(cleared["enabled"])
            self.assertTrue((home / EXECUTION_FOCUS_FILENAME).is_file())
            self.assertEqual(read_execution_focus(home), cleared)

    def test_focus_rejects_wrong_project_missing_project_and_oversized_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            focus = set_execution_focus(Path(tmp), "0123456789ab", "SuperMega", 4)
            enforce_execution_focus(focus, "0123456789ab", ["a", "b", "c", "d"], "run")
            with self.assertRaisesRegex(RuntimeError, "requires a project"):
                enforce_execution_focus(focus, None, ["a"], "run")
            with self.assertRaisesRegex(RuntimeError, "requested project was denied"):
                enforce_execution_focus(focus, "abcdef012345", ["a"], "run")
            with self.assertRaisesRegex(RuntimeError, "at most 4 roles"):
                enforce_execution_focus(focus, "0123456789ab", ["a", "b", "c", "d", "e"], "run")

    def test_tampered_focus_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            set_execution_focus(home, "0123456789ab", "SuperMega", 4)
            path = home / EXECUTION_FOCUS_FILENAME
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["controls"]["bypassAllowed"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controls are invalid"):
                read_execution_focus(home)
            argv = ["local-company", "--home", str(home), "focus", "clear"]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertFalse(read_execution_focus(home)["enabled"])

    def test_cli_denies_unfocused_queue_before_claim_or_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            focused_id = company.create_project("SuperMega")
            company.create_project("SuperMega Vision")
            queue_id = company.enqueue(
                "Prepare a Vision brief", "SuperMega Vision", ["chief-of-staff", "quality"], priority=90,
            )
            set_execution_focus(home, focused_id, "SuperMega", 4)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "local-company", "--home", str(home), "queue", "run-next",
                "--queue-id", queue_id, "--provider", "mock",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
                result = main()
            self.assertEqual(result, 2)
            self.assertIn("denied before model load", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual([row[0] for row in company.queue_items("queued")], [queue_id])
            self.assertEqual(company.health_snapshot()["active_jobs"], 0)

    def test_cli_denies_matching_project_when_team_exceeds_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            focused_id = company.create_project("SuperMega")
            queue_id = company.enqueue(
                "Prepare a broad company brief", "SuperMega",
                ["chief-of-staff", "research", "operations", "marketing", "quality"],
                priority=90,
            )
            set_execution_focus(home, focused_id, "SuperMega", 4)
            stderr = io.StringIO()
            argv = [
                "local-company", "--home", str(home), "queue", "run-next",
                "--queue-id", queue_id, "--provider", "mock",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = main()
            self.assertEqual(result, 2)
            self.assertIn("at most 4 roles", stderr.getvalue())
            self.assertEqual([row[0] for row in company.queue_items("queued")], [queue_id])


if __name__ == "__main__":
    unittest.main()
