from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.local_ai import explain, main, run_autopilot, run_code, run_company, run_cycle, run_work, switch_project, translate
from scripts.run_scheduled_cycle import SCHEMA as SCHEDULED_CYCLE_SCHEMA, run_scheduled_cycle


class LocalAiLaunchpadTests(unittest.TestCase):
    def test_friendly_modes_translate_to_existing_bounded_commands(self) -> None:
        self.assertEqual(translate(["plan", "Invent a product"]).command, ("preflight", "Invent a product"))
        self.assertEqual(translate(["work", "Build a plan"]).command, ("run", "Build a plan"))
        self.assertEqual(translate(["later", "Research market"]).command, ("queue", "add", "Research market"))
        self.assertEqual(translate(["next"]).command, ("queue", "preflight"))
        self.assertEqual(translate(["run-next"]).command, ("queue", "run-next"))
        self.assertEqual(translate(["cycle", "--model", "local"]).command, ("--model", "local"))
        self.assertEqual(translate(["dashboard"]).command, ("service", "start"))
        self.assertEqual(translate(["data", "list"]).command, ("datasets", "list"))
        self.assertEqual(translate(["new", "Future Lab"]).command, ("projects", "create", "Future Lab"))
        self.assertEqual(translate(["use", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["vision"]).command, ("--vision",))
        self.assertEqual(translate(["vision-lite", "--check"]).command, ("--vision-lite", "--check"))
        self.assertEqual(translate(["autopilot", "status"]).command, ("status",))

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
        cases = [(["unknown"], "launchpad_command_unknown"), (["work"], "work_objective_required"), (["company"], "company_command_required"), (["autopilot"], "autopilot_action_required")]
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

    def test_windows_wrapper_routes_everything_through_argument_safe_python_launcher(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-ai.cmd").read_text(encoding="utf-8")
        self.assertIn('python "%LAUNCHPAD_ROOT%scripts\\local_ai.py" %*', source)
        self.assertNotIn("shift", source.lower())

    def test_code_mode_forwards_exact_arguments_without_a_shell(self) -> None:
        action = translate([
            "code", "--run", "C:\\Project With Spaces", "TASK.md",
            "--test", "python", "-m", "unittest", "-v",
        ])
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "local-code.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess([], 2)
            self.assertEqual(run_code(action, root), 2)
            command = run.call_args.args[0]
            self.assertEqual(command[1:], list(action.command))
            self.assertEqual(command[2], "C:\\Project With Spaces")
            self.assertFalse(run.call_args.kwargs["check"])

    def test_autopilot_routes_only_fixed_task_manager_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "manage_cycle_task.ps1").write_text("# fixture\n", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(run_autopilot(translate(["autopilot", "install"]), root), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[-2:], ["-Mode", "Install"])
            self.assertEqual(command[-4], "-File")
            self.assertEqual(Path(command[-3]), root / "scripts" / "manage_cycle_task.ps1")
            self.assertFalse(run.call_args.kwargs["check"])

    def test_autopilot_task_contract_is_fixed_local_limited_and_non_overlapping(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "scripts" / "manage_cycle_task.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("$taskName = 'SuperMega Local Product Cycle'", source)
        self.assertIn("Get-FileHash -LiteralPath `$runner -Algorithm SHA256", source)
        self.assertIn("Get-FileHash -LiteralPath `$launcher -Algorithm SHA256", source)
        self.assertIn("Get-FileHash -LiteralPath `$optimizer -Algorithm SHA256", source)
        self.assertIn("-EncodedCommand ' + $encodedGuard", source)
        self.assertIn("{ exit 90 }", source)
        self.assertIn("{ exit 91 }", source)
        self.assertIn("{ exit 92 }", source)
        self.assertIn("$interval = 'PT6H'", source)
        self.assertIn("-LogonType Interactive -RunLevel Limited", source)
        self.assertIn("-MultipleInstances IgnoreNew", source)
        self.assertIn("modelMemoryGateBytes = 2147483648", source)
        self.assertIn("sourceDigestsPinned = $true", source)
        self.assertIn("boundedMemoryRecovery = $true", source)
        self.assertIn("memoryRecoveryAttempted = $result.memoryRecovery.attempted", source)
        self.assertIn("$Task.Settings.Enabled", source)
        self.assertIn("$info.LastTaskResult -ne 267011", source)
        self.assertIn("task_remove_refused_unverified_definition", source)

    def test_scheduled_cycle_journal_is_atomic_allowlisted_and_discards_raw_output(self) -> None:
        cycle = {
            "schema": "local-ai.cycle-result.v1", "status": "no_due_mission",
            "reason": "no_due_mission", "missionsRun": 0,
            "modelCalled": False, "memoryShortfallBytes": 123,
            "report": "C:\\private\\report.md", "secret": "SENTINEL",
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "raw SENTINEL\n" + json.dumps(cycle) + "\n", ""),
        ):
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 0)
            self.assertEqual(journal["schema"], SCHEDULED_CYCLE_SCHEMA)
            self.assertEqual(journal["cycle"]["reason"], "no_due_mission")
            stored = (state / "autopilot-cycle-result.json").read_text(encoding="utf-8")
            self.assertNotIn("SENTINEL", stored)
            self.assertNotIn("report.md", stored)
            self.assertFalse(json.loads(stored)["controls"]["rawOutputStored"])

    def test_scheduled_cycle_records_invalid_receipt_without_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "SENTINEL invalid", ""),
        ):
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 2)
            self.assertEqual(journal["reason"], "cycle_receipt_missing_or_oversized")
            self.assertNotIn("SENTINEL", (state / "autopilot-cycle-result.json").read_text(encoding="utf-8"))

    def test_scheduled_cycle_performs_one_validated_memory_trim_then_retries_once(self) -> None:
        blocked = json.dumps({
            "schema": "local-ai.cycle-result.v1", "status": "blocked",
            "reason": "insufficient_available_memory", "missionsRun": 0,
            "modelCalled": False,
        }) + "\n"
        ready = json.dumps({
            "schema": "local-ai.cycle-result.v1", "status": "no_due_mission",
            "reason": "no_due_mission", "missionsRun": 0, "modelCalled": False,
        }) + "\n"
        trim = json.dumps({
            "contract": "supermega.ally-working-set-trim.v1", "ok": True,
            "mode": "apply", "targetCount": 3, "trimSucceeded": 3,
            "trimFailed": 0, "releasedWorkingSetMb": 512.5,
            "controls": {
                "processMutation": True, "processTerminationCalls": 0,
                "filesModified": 0, "networkRequests": 0,
            },
        })
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
        ) as run:
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            optimizer = root.parent / "supermega-platform" / "tools" / "trim_codex_working_sets.ps1"
            optimizer.parent.mkdir(parents=True)
            optimizer.write_text("# fixture\n", encoding="utf-8")
            run.side_effect = [
                subprocess.CompletedProcess([], 0, blocked, ""),
                subprocess.CompletedProcess([], 0, trim, ""),
                subprocess.CompletedProcess([], 0, ready, ""),
            ]
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state, settle=lambda _seconds: None)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(journal["cycle"]["status"], "no_due_mission")
            self.assertEqual(journal["memoryRecovery"]["releasedWorkingSetMb"], 512.5)
            self.assertEqual(journal["memoryRecovery"]["processTerminationCalls"], 0)

    def test_vision_modes_default_to_installed_vision_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory) / "local-agent-company"
            root.mkdir()
            (root / "local-code.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(run_code(translate(["vision-lite", "--check"]), root), 0)
            self.assertEqual(
                run.call_args.args[0],
                [str(root / "local-code.cmd"), "--vision-lite", "--check", str(root.parent / "supermega-vision")],
            )
            self.assertEqual(run_code(translate(["vision", "C:\\Custom Vision"]), root), 0)
            self.assertEqual(
                run.call_args.args[0],
                [str(root / "local-code.cmd"), "--vision", "C:\\Custom Vision"],
            )

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

    def test_cycle_with_no_due_work_never_runs_a_model(self) -> None:
        preflight = {
            "schema": "local-company.queue-preflight.v1", "status": "no_due_mission",
            "queue_id": None, "blockers": ["no_due_mission"], "owner_gate_categories": [],
        }
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(preflight), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["missionsRun"], 0)
            self.assertFalse(receipt["modelCalled"])
            self.assertEqual(run.call_count, 2)

    def test_cycle_owner_gate_stops_before_execution(self) -> None:
        preflight = {
            "schema": "local-company.queue-preflight.v1", "status": "owner_gate_required",
            "queue_id": "0123456789ab", "blockers": [],
            "owner_gate_categories": ["external_send"],
        }
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(preflight), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["ownerGateCategories"], ["external_send"])
            self.assertEqual(run.call_count, 2)

    def test_cycle_runs_exactly_one_id_bound_ready_mission(self) -> None:
        queue_id, job_id = "0123456789ab", "abcdef012345"
        ready = {
            "schema": "local-company.queue-preflight.v1", "status": "ready",
            "queue_id": queue_id, "reviewed_queue_matches": None,
            "submission_allowed": True, "model_execution_ready": True,
            "owner_gate_categories": [],
        }
        bound = {**ready, "reviewed_queue_matches": True}
        detail = {
            "job": [job_id, "objective", "complete", "time", "C:\\report.md"],
            "evaluation": {"passed": True, "score": 100, "checks": {"model_stopped_cleanly": True}},
        }
        completion = f"Queue item {queue_id} completed as job {job_id}; quality=passed\nReport: C:\\report.md\n"
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run, patch(
            "scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3,
        ):
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 1 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
                subprocess.CompletedProcess([], 0, json.dumps(bound), ""),
                subprocess.CompletedProcess([], 0, completion, ""),
                subprocess.CompletedProcess([], 0, json.dumps(detail), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle", "--keep-alive", "0s"]), root), 0)
            receipt = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(receipt["missionsRun"], 1)
            self.assertEqual(receipt["qualityScore"], 100)
            execution = run.call_args_list[3].args[0]
            self.assertEqual(
                execution[-6:],
                ["queue", "run-next", "--queue-id", queue_id, "--keep-alive", "0s"],
            )

    def test_cycle_blocks_before_execution_when_memory_is_below_safe_floor(self) -> None:
        queue_id = "0123456789ab"
        ready = {
            "schema": "local-company.queue-preflight.v1", "status": "ready",
            "queue_id": queue_id, "reviewed_queue_matches": None,
            "submission_allowed": True, "model_execution_ready": True,
            "owner_gate_categories": [],
        }
        bound = {**ready, "reviewed_queue_matches": True}
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run, patch(
            "scripts.local_ai.available_memory_bytes", return_value=1024**3,
        ):
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
                subprocess.CompletedProcess([], 0, json.dumps(bound), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["reason"], "insufficient_available_memory")
            self.assertEqual(receipt["memoryShortfallBytes"], 1024**3)
            self.assertFalse(receipt["modelCalled"])
            self.assertEqual(run.call_count, 3)

    def test_cycle_rejects_malformed_preflight_and_user_selected_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle_queue_id"):
            translate(["cycle", "--queue-id", "0123456789ab"])
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, "not-json", ""),
            ]
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(run_cycle(translate(["cycle"]), root), 2)
            self.assertIn("queue_preflight_invalid", error.getvalue())

    def test_cycle_returns_nonzero_for_completed_quality_failure(self) -> None:
        queue_id, job_id = "0123456789ab", "abcdef012345"
        ready = {
            "schema": "local-company.queue-preflight.v1", "status": "ready",
            "queue_id": queue_id, "reviewed_queue_matches": None,
            "submission_allowed": True, "model_execution_ready": True,
            "owner_gate_categories": [],
        }
        detail = {
            "job": [job_id, "objective", "complete", "time", "C:\\report.md"],
            "evaluation": {"passed": False, "score": 75, "checks": {"model_stopped_cleanly": True}},
        }
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run, patch(
            "scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3,
        ):
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
                subprocess.CompletedProcess([], 0, json.dumps({**ready, "reviewed_queue_matches": True}), ""),
                subprocess.CompletedProcess([], 0, f"Queue item {queue_id} completed as job {job_id}; quality=failed\nReport: C:\\report.md\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(detail), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle"]), root), 1)
            receipt = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(receipt["status"], "quality_failed")
            self.assertEqual(receipt["missionsRun"], 1)
            self.assertEqual(receipt["qualityScore"], 75)


if __name__ == "__main__":
    unittest.main()
