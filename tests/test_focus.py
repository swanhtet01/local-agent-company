from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_company.cli import main, parser
from local_company.core import Company, MockModel
from local_company.focus import (
    EXECUTION_FOCUS_FILENAME,
    clear_execution_focus,
    enforce_execution_focus,
    enforce_execution_resource_envelope,
    read_execution_focus,
    set_execution_focus,
)


class ExecutionFocusTests(unittest.TestCase):
    def test_cli_focus_defaults_to_four_serial_roles(self):
        args = parser().parse_args(["focus", "set", "--project", "0123456789ab"])
        self.assertEqual(args.max_roles, 4)

    def test_focus_round_trip_is_bounded_and_clear_preserves_audit_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(read_execution_focus(home)["enabled"])
            active = set_execution_focus(home, "0123456789ab", "SuperMega", 6)
            self.assertTrue(active["enabled"])
            self.assertEqual(read_execution_focus(home), active)
            cleared = clear_execution_focus(home)
            self.assertFalse(cleared["enabled"])
            self.assertTrue((home / EXECUTION_FOCUS_FILENAME).is_file())
            self.assertEqual(read_execution_focus(home), cleared)

    def test_focus_rejects_wrong_project_missing_project_and_oversized_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            focus = set_execution_focus(Path(tmp), "0123456789ab", "SuperMega", 6)
            enforce_execution_focus(focus, "0123456789ab", ["a", "b", "c", "d", "e", "f"], "run")
            with self.assertRaisesRegex(RuntimeError, "requires a project"):
                enforce_execution_focus(focus, None, ["a"], "run")
            with self.assertRaisesRegex(RuntimeError, "requested project was denied"):
                enforce_execution_focus(focus, "abcdef012345", ["a"], "run")
            with self.assertRaisesRegex(RuntimeError, "at most 6 roles"):
                enforce_execution_focus(focus, "0123456789ab", ["a", "b", "c", "d", "e", "f", "g"], "run")

    def test_ollama_resource_envelope_requires_focus_and_scale_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, "focus must be enabled"):
                enforce_execution_resource_envelope(
                    read_execution_focus(home), "ollama", 4096, 768, "0s", "run",
                )
            focus = set_execution_focus(home, "0123456789ab", "SuperMega", 6)
            receipt = enforce_execution_resource_envelope(
                focus, "ollama", 4096, 768, "0s", "run",
            )
            self.assertEqual(receipt["schema"], "local-company.execution-resource-envelope.v1")
            self.assertEqual(receipt["maxRoles"], 6)
            self.assertTrue(receipt["modelLoadAllowed"])
            for values, message in [
                ((8192, 768, "0s"), "context exceeds 4096"),
                ((4096, 769, "0s"), "output exceeds 768"),
                ((4096, 768, "30s"), "keep-alive must be 0s"),
            ]:
                with self.assertRaisesRegex(RuntimeError, message):
                    enforce_execution_resource_envelope(
                        focus, "ollama", values[0], values[1], values[2], "run",
                    )
            mock = enforce_execution_resource_envelope(
                read_execution_focus(Path(tmp) / "mock"), "mock", 131072, 4096, "30s", "run",
            )
            self.assertFalse(mock["focusRequired"])
            self.assertFalse(mock["modelLoadAllowed"])

    def test_tampered_focus_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            set_execution_focus(home, "0123456789ab", "SuperMega", 6)
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
            set_execution_focus(home, focused_id, "SuperMega", 6)
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
                [
                    "chief-of-staff", "research", "finance", "marketing",
                    "sales", "operations", "quality",
                ],
                priority=90,
            )
            set_execution_focus(home, focused_id, "SuperMega", 6)
            stderr = io.StringIO()
            argv = [
                "local-company", "--home", str(home), "queue", "run-next",
                "--queue-id", queue_id, "--provider", "mock",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = main()
            self.assertEqual(result, 2)
            self.assertIn("at most 6 roles", stderr.getvalue())
            self.assertEqual([row[0] for row in company.queue_items("queued")], [queue_id])

    def test_cli_denies_oversized_ollama_runtime_before_queue_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            focused_id = company.create_project("SuperMega")
            queue_id = company.enqueue(
                "Prepare one bounded operating brief", "SuperMega", ["operations"], priority=90,
            )
            set_execution_focus(home, focused_id, "SuperMega", 6)
            cases = [
                (["--num-ctx", "8192", "--num-predict", "768", "--keep-alive", "0s"], "context exceeds 4096"),
                (["--num-ctx", "4096", "--num-predict", "769", "--keep-alive", "0s"], "output exceeds 768"),
                (["--num-ctx", "4096", "--num-predict", "768", "--keep-alive", "30s"], "keep-alive must be 0s"),
            ]
            for runtime_args, expected in cases:
                stderr = io.StringIO()
                argv = [
                    "local-company", "--home", str(home), "queue", "run-next",
                    "--queue-id", queue_id, "--provider", "ollama", *runtime_args,
                ]
                with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    result = main()
                self.assertEqual(result, 2)
                self.assertIn(expected, stderr.getvalue())
                self.assertEqual([row[0] for row in company.queue_items("queued")], [queue_id])
                self.assertEqual(company.health_snapshot()["active_jobs"], 0)

    def test_core_queue_preflight_and_claim_deny_unfocused_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            focused_id = company.create_project("SuperMega")
            company.create_project("SuperMega Vision")
            queue_id = company.enqueue(
                "Prepare a Vision brief", "SuperMega Vision", ["quality"], priority=90,
            )
            set_execution_focus(home, focused_id, "SuperMega", 6)

            preflight = company.queue_preflight(queue_id)
            self.assertEqual(preflight["status"], "blocked")
            self.assertIn("execution_focus_mismatch", preflight["blockers"])
            self.assertFalse(preflight["effects"]["state_mutated"])
            with self.assertRaisesRegex(RuntimeError, "execution_focus_mismatch"):
                company.claim_next_queue_item(queue_id)
            self.assertEqual([row[0] for row in company.queue_items("queued")], [queue_id])
            self.assertEqual(company.health_snapshot()["active_jobs"], 0)

    def test_core_run_denies_unfocused_project_before_job_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            focused_id = company.create_project("SuperMega")
            company.create_project("SuperMega Vision")
            set_execution_focus(home, focused_id, "SuperMega", 1)

            with self.assertRaisesRegex(RuntimeError, "denied before model load"):
                company.run("Prepare a concise brief", roles=["quality"], project="SuperMega Vision")
            self.assertEqual(company.health_snapshot()["active_jobs"], 0)


if __name__ == "__main__":
    unittest.main()
