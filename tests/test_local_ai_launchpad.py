from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.local_ai import explain, main, run_company, run_work, switch_project, translate


class LocalAiLaunchpadTests(unittest.TestCase):
    def test_friendly_modes_translate_to_existing_bounded_commands(self) -> None:
        self.assertEqual(translate(["plan", "Invent a product"]).command, ("preflight", "Invent a product"))
        self.assertEqual(translate(["work", "Build a plan"]).command, ("run", "Build a plan"))
        self.assertEqual(translate(["later", "Research market"]).command, ("queue", "add", "Research market"))
        self.assertEqual(translate(["next"]).command, ("queue", "preflight"))
        self.assertEqual(translate(["run-next"]).command, ("queue", "run-next"))
        self.assertEqual(translate(["dashboard"]).command, ("service", "start"))
        self.assertEqual(translate(["data", "list"]).command, ("datasets", "list"))
        self.assertEqual(translate(["new", "Future Lab"]).command, ("projects", "create", "Future Lab"))
        self.assertEqual(translate(["use", "Future Lab"]).command, ("Future Lab",))

    def test_effect_receipt_is_explicit_about_model_state_and_external_authority(self) -> None:
        planned = explain(translate(["plan", "Explore an idea"]))
        self.assertFalse(planned["effects"]["modelMayRun"])
        self.assertFalse(planned["effects"]["localStateMayChange"])
        worked = explain(translate(["work", "Explore an idea"]))
        self.assertTrue(worked["effects"]["modelMayRun"])
        self.assertTrue(worked["effects"]["localStateMayChange"])
        self.assertFalse(worked["effects"]["externalActionAllowed"])
        self.assertFalse(worked["effects"]["paidApiRequired"])

    def test_help_and_explain_never_launch_a_process(self) -> None:
        with patch("scripts.local_ai.subprocess.run", side_effect=AssertionError("process launched")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)
            self.assertIn("Local AI Launchpad", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["explain", "work", "Create a roadmap"]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "work")

    def test_unknown_or_incomplete_commands_fail_closed(self) -> None:
        cases = [(["unknown"], "launchpad_command_unknown"), (["work"], "work_objective_required"), (["company"], "company_command_required")]
        for args, reason in cases:
            error = io.StringIO()
            with self.subTest(args=args), redirect_stderr(error):
                self.assertEqual(main(args), 2)
                self.assertEqual(json.loads(error.getvalue())["reason"], reason)

    def test_company_execution_is_repository_anchored_and_argument_safe(self) -> None:
        action = translate(["plan", "Quote ; & $() exactly", "--project", "Future Product"])
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(run_company(action, root), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[-4:], ["preflight", "Quote ; & $() exactly", "--project", "Future Product"])
            self.assertEqual(run.call_args.kwargs["cwd"], root)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_windows_wrapper_routes_code_to_existing_local_launcher(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-ai.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="code"', source)
        self.assertIn('call "%LAUNCHPAD_ROOT%local-code.cmd" %*', source)
        self.assertIn('python "%LAUNCHPAD_ROOT%scripts\\local_ai.py" %*', source)

    def test_use_project_performs_digest_bound_handoff_through_existing_cli(self) -> None:
        observed = {
            "focus": {"enabled": True, "projectId": "111111111111", "projectName": "Old Project"},
            "focusDigest": f"sha256:{'a' * 64}",
        }
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, json.dumps(observed), ""),
                subprocess.CompletedProcess([], 0),
            ]
            self.assertEqual(switch_project(translate(["use", "Future Lab"]), root), 0)
            mutation = run.call_args_list[1].args[0]
            self.assertIn("handoff", mutation)
            self.assertIn("111111111111", mutation)
            self.assertIn(f"sha256:{'a' * 64}", mutation)
            self.assertIn("HANDOFF ACTIVE EXECUTION FOCUS", mutation)

    def test_work_returns_nonzero_and_a_receipt_when_quality_fails(self) -> None:
        completed = "Completed job 0123456789ab\nReport: C:\\private\\report.md\n"
        inspected = {
            "job": ["0123456789ab", "private objective", "complete", "time", "C:\\private\\report.md"],
            "evaluation": {"passed": False, "score": 88, "checks": {"model_stopped_cleanly": False}},
        }
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, completed, ""),
                subprocess.CompletedProcess([], 0, json.dumps(inspected), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_work(translate(["work", "Bounded task"]), root), 1)
            lines = output.getvalue().splitlines()
            receipt = json.loads(lines[-1])
            self.assertFalse(receipt["ok"])
            self.assertFalse(receipt["qualityPassed"])
            self.assertEqual(receipt["qualityScore"], 88)
            self.assertFalse(receipt["externalActionPerformed"])

    def test_work_fails_closed_when_completion_cannot_be_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.return_value = subprocess.CompletedProcess([], 0, "unexpected output", "")
            error = io.StringIO()
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(run_work(translate(["work", "Bounded task"]), root), 2)
            self.assertEqual(output.getvalue(), "unexpected output")
            self.assertIn("completed_job_id_missing", error.getvalue())


if __name__ == "__main__":
    unittest.main()
