from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.local_ai import explain, main, run_autopilot, run_code, run_company, run_cycle, run_experiment, run_experiment_agent, run_experiment_review, run_interactive_experiment_review, run_offer, run_pending_experiments, run_work, switch_project, translate
from scripts.run_scheduled_cycle import SCHEMA as SCHEDULED_CYCLE_SCHEMA, run_scheduled_cycle


def accepted_prompt_receipt(actions: list[str]) -> dict[str, object]:
    return {
        "schema": "local-ai.company-prompt-result.v1",
        "autoPermissionsEnabled": False, "externalActionPerformed": False,
        "modelCalled": True, "ok": True, "paidApiUsed": False,
        "reason": "accepted", "status": "accepted", "wallSeconds": 42.5,
        "model": "qwen3.5:0.8b", "toolActions": actions,
        "toolCallCount": len(actions), "response": "Grounded local review.",
        "observedCost": 0.0, "modelUnloadedAfterRun": True, "agentExitCode": 0,
        "admissionAvailableBytes": 3 * 1024**3,
        "minimumAvailableBytesObserved": 2 * 1024**3,
        "peakIncrementalMemoryBytes": 1024**3,
        "peakIncrementalMemoryMb": 1024.0,
    }


class LocalAiLaunchpadTests(unittest.TestCase):
    def test_friendly_modes_translate_to_existing_bounded_commands(self) -> None:
        self.assertEqual(translate(["plan", "Invent a product"]).command, ("preflight", "Invent a product"))
        self.assertEqual(translate(["experiment"]).command, ())
        self.assertEqual(translate(["experiment", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["experiment-run", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(
            translate(["experiment-run", "--recover-memory", "Future Lab"]).command,
            ("Future Lab", "--recover-memory"),
        )
        self.assertEqual(translate(["offer", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["experiment-pending"]).command, ())
        self.assertEqual(translate(["experiment-review-interactive"]).command, ())
        self.assertEqual(translate([
            "experiment-review", "a" * 12, "--decision", "accepted",
            "--corrections", "0", "--paid-setup", "unknown",
            "--confirm-human-review",
        ]).mode, "experiment-review")
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
        self.assertEqual(translate(["autopilot", "repair"]).command, ("repair",))

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

    def test_experiment_planner_runs_without_model_or_state_mutation(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            project_id = Company(home, MockModel()).create_project("Future Lab")
            output = io.StringIO()
            with patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}), redirect_stdout(output):
                self.assertEqual(
                    run_experiment(translate(["experiment", "Future Lab"])), 0,
                )
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "experiment_ready")
            self.assertEqual(receipt["selectedCategory"], "coding")
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])

    def test_experiment_runner_binds_receipt_to_planned_tool_actions(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            Company(home, MockModel()).create_project("Future Lab")
            runner_receipt = accepted_prompt_receipt(["status", "project_overview"])
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(runner_receipt), stderr="",
            )
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=completed),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_experiment_agent(translate(["experiment-run", "Future Lab"])), 1,
                )
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(receipt["reason"], "required_company_actions_missing")
            self.assertEqual(receipt["missingActions"], ["playbooks"])
            self.assertFalse(receipt["humanReviewRecorded"])
            self.assertFalse(receipt["stateMutated"])

    def test_experiment_runner_can_trim_safely_and_retry_exactly_once(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            Company(home, MockModel()).create_project("Future Lab")
            blocked = subprocess.CompletedProcess([], 2, stdout="", stderr=json.dumps({
                "schema": "local-ai.company-prompt-result.v1", "ok": False,
                "status": "blocked", "reason": "installed_models_memory_blocked",
                "modelCalled": False, "externalActionPerformed": False,
            }))
            accepted = subprocess.CompletedProcess(
                [], 0,
                stdout=json.dumps(accepted_prompt_receipt([
                    "status", "project_overview", "playbooks",
                ])), stderr="",
            )
            recovery = {
                "attempted": True, "status": "completed", "targetCount": 2,
                "trimSucceeded": 2, "trimFailed": 0,
                "releasedWorkingSetMb": 512.0, "processTerminationCalls": 0,
            }
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", side_effect=[blocked, accepted]) as run,
                patch("scripts.local_ai._recover_memory", return_value=recovery) as recover,
                patch("scripts.local_ai.time.sleep") as sleep,
                redirect_stdout(output),
            ):
                self.assertEqual(run_experiment_agent(translate([
                    "experiment-run", "Future Lab", "--recover-memory",
                ])), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(receipt["attemptCount"], 2)
            self.assertEqual(receipt["memoryRecovery"], recovery)
            self.assertTrue(receipt["pendingReceiptStored"])
            self.assertTrue(receipt["stateMutated"])
            self.assertEqual(run.call_count, 2)
            recover.assert_called_once()
            sleep.assert_called_once_with(3.0)

    def test_pending_experiment_can_be_inspected_and_human_reviewed_once(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            project_id = Company(home, MockModel()).create_project("Future Lab")
            completed = subprocess.CompletedProcess(
                [], 0,
                stdout=json.dumps(accepted_prompt_receipt([
                    "status", "project_overview", "playbooks",
                ])), stderr="",
            )
            run_output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=completed),
                redirect_stdout(run_output),
            ):
                self.assertEqual(
                    run_experiment_agent(translate(["experiment-run", "Future Lab"])), 0,
                )
            pending_id = json.loads(run_output.getvalue())["pendingExperimentId"]

            pending_output = io.StringIO()
            with patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}), redirect_stdout(pending_output):
                self.assertEqual(run_pending_experiments(translate(["experiment-pending"])), 0)
            pending = json.loads(pending_output.getvalue())
            self.assertEqual(pending["count"], 1)
            self.assertEqual(pending["items"][0]["experimentId"], pending_id)
            self.assertEqual(pending["items"][0]["response"], "Grounded local review.")

            review_output = io.StringIO()
            review_action = translate([
                "experiment-review", pending_id, "--decision", "accepted",
                "--corrections", "0", "--paid-setup", "unknown",
                "--confirm-human-review",
            ])
            with patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}), redirect_stdout(review_output):
                self.assertEqual(run_experiment_review(review_action), 0)
            review = json.loads(review_output.getvalue())
            self.assertTrue(review["recorded"])
            self.assertTrue(review["pendingArchived"])
            self.assertEqual(review["experimentId"], pending_id)
            status = Company(home, MockModel()).product_evidence_status(project_id)
            self.assertEqual(status["reviewed_missions"], 1)
            self.assertEqual(status["reviews"][0]["experiment_id"], pending_id)

    def test_interactive_review_requires_human_values_and_records_locally(self) -> None:
        from local_company.core import Company, MockModel
        from scripts.local_ai import _pending_experiment_payload, _store_pending_experiment

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            project_id = Company(home, MockModel()).create_project("Future Lab")
            payload = _pending_experiment_payload(
                {
                    "project": {"id": project_id, "name": "Future Lab"},
                    "selectedCategory": "coding",
                },
                {"label": "Coding operability review", "requiredActions": ["status"]},
                accepted_prompt_receipt(["status"]),
            )
            pending_id, _ = _store_pending_experiment(home, payload)
            output = io.StringIO()
            answers = iter([pending_id, "accepted", "1", "unknown", "REVIEW"])
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_interactive_experiment_review(
                        translate(["experiment-review-interactive"]),
                    ), 0,
                )
            self.assertIn("Pending measured product experiments", output.getvalue())
            self.assertIn('"status":"recorded"', output.getvalue())

    def test_offer_command_reports_missing_proof_without_model_or_mutation(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            Company(home, MockModel()).create_project("Future Lab")
            output = io.StringIO()
            with patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}), redirect_stdout(output):
                self.assertEqual(run_offer(translate(["offer", "Future Lab"])), 1)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "evidence_required")
            self.assertFalse(receipt["externalPublicationAuthorized"])
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])

    def test_windows_wrapper_routes_everything_through_argument_safe_python_launcher(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-ai.cmd").read_text(encoding="utf-8")
        self.assertIn('python "%LAUNCHPAD_ROOT%scripts\\local_ai.py" %*', source)
        self.assertNotIn("shift", source.lower())

    def test_desktop_menu_exposes_only_bounded_local_entrypoints(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "local-ai-menu.cmd").read_text(encoding="utf-8")
        self.assertIn('local-company-agent.cmd"', source)
        self.assertIn('local-ai.cmd" experiment', source)
        self.assertIn('local-ai.cmd" experiment-run --recover-memory', source)
        self.assertIn('local-ai.cmd" offer', source)
        self.assertIn('local-ai.cmd" code "%LOCAL_AI_PROJECT_PATH%"', source)
        self.assertIn('local-company-agent.cmd" --check', source)
        self.assertIn('local-ai.cmd" dashboard', source)
        self.assertNotIn("taskkill", source.lower())
        self.assertNotIn("powershell", source.lower())

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
        self.assertIn("Test-CycleTaskRepairable", source)
        self.assertIn("task_repair_refused_untrusted_definition", source)
        self.assertIn("[regex]::Matches($actualGuard, $digestPattern).Count -ne 3", source)
        self.assertIn("Unregister-ScheduledTask -TaskName $taskName -Confirm:$false", source)
        self.assertIn("boundedMemoryRecovery = $true", source)
        self.assertIn("-RunOnlyIfIdle -IdleDuration (New-TimeSpan -Minutes 10)", source)
        self.assertIn("-IdleWaitTimeout (New-TimeSpan -Hours 6) -DontStopOnIdleEnd", source)
        self.assertIn("$Task.Settings.RunOnlyIfIdle", source)
        self.assertIn("idleWindowMinutes = 10", source)
        self.assertIn("stopsWhenUserReturns = $false", source)
        self.assertIn("memoryRecoveryAttempted = $result.memoryRecovery.attempted", source)
        self.assertIn("taskExecutionState = $taskState", source)
        self.assertIn("lastCycleCurrentForLastRun = $journalCurrentForLastRun", source)
        self.assertIn("wait_for_idle_or_cycle_completion", source)
        self.assertIn("lastCycleFreshnessChecked = $true", source)
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
        pending = {
            "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
            "count": 1, "items": [{"response": "SENTINEL"}],
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
        ) as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "raw SENTINEL\n" + json.dumps(cycle) + "\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(pending), ""),
            ]
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 0)
            self.assertEqual(journal["schema"], SCHEDULED_CYCLE_SCHEMA)
            self.assertEqual(journal["cycle"]["reason"], "no_due_mission")
            self.assertEqual(journal["experiment"]["status"], "awaiting_human_review")
            self.assertEqual(run.call_count, 2)
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
        pending = json.dumps({
            "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
            "count": 1, "items": [], "modelCalled": False,
            "stateMutated": False, "externalActionPerformed": False,
        })
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
                subprocess.CompletedProcess([], 0, pending, ""),
            ]
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state, settle=lambda _seconds: None)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 4)
            self.assertEqual(journal["cycle"]["status"], "no_due_mission")
            self.assertEqual(journal["memoryRecovery"]["releasedWorkingSetMb"], 512.5)
            self.assertEqual(journal["memoryRecovery"]["processTerminationCalls"], 0)
            self.assertEqual(journal["experiment"]["status"], "awaiting_human_review")

    def test_scheduled_cycle_runs_one_experiment_only_when_queue_and_inbox_are_empty(self) -> None:
        cycle = json.dumps({
            "schema": "local-ai.cycle-result.v1", "status": "no_due_mission",
            "reason": "no_due_mission", "missionsRun": 0, "modelCalled": False,
        })
        pending = json.dumps({
            "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
            "count": 0, "items": [], "modelCalled": False,
            "stateMutated": False, "externalActionPerformed": False,
        })
        experiment = json.dumps({
            "schema": "local-ai.experiment-run.v1", "status": "accepted",
            "reason": "accepted", "ok": True, "selectedCategory": "coding",
            "label": "Coding operability review", "pendingExperimentId": "a" * 12,
            "pendingReceiptStored": True, "attemptCount": 1,
            "modelCalled": True, "stateMutated": True,
            "externalActionPerformed": False,
            "runnerReceipt": {"response": "PRIVATE SENTINEL"},
        })
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
        ) as run:
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            run.side_effect = [
                subprocess.CompletedProcess([], 0, cycle, ""),
                subprocess.CompletedProcess([], 0, pending, ""),
                subprocess.CompletedProcess([], 0, experiment, ""),
            ]
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0][-2:], ["experiment-run", "--recover-memory"])
            self.assertEqual(journal["experiment"]["status"], "accepted")
            self.assertEqual(journal["experiment"]["pendingExperimentId"], "a" * 12)
            self.assertEqual(journal["controls"]["maximumExperimentsPerCycle"], 1)
            self.assertTrue(journal["controls"]["humanReviewRequired"])
            stored = (state / "autopilot-cycle-result.json").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE SENTINEL", stored)

    def test_scheduled_cycle_never_runs_experiment_after_a_mission(self) -> None:
        completed_cycle = json.dumps({
            "schema": "local-ai.cycle-result.v1", "status": "completed",
            "reason": "completed", "queueId": "a" * 12, "missionsRun": 1,
            "modelCalled": True, "qualityPassed": True,
            "modelUnloadedAfterRun": True,
        })
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, completed_cycle, ""),
        ) as run:
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(journal["cycle"]["missionsRun"], 1)
            self.assertNotIn("experiment", journal)

    def test_scheduled_cycle_rejects_unsafe_experiment_receipt_without_raw_output(self) -> None:
        cycle = json.dumps({
            "schema": "local-ai.cycle-result.v1", "status": "no_due_mission",
            "reason": "no_due_mission", "missionsRun": 0, "modelCalled": False,
        })
        pending = json.dumps({
            "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
            "count": 0, "items": [], "modelCalled": False,
            "stateMutated": False, "externalActionPerformed": False,
        })
        unsafe = json.dumps({
            "schema": "local-ai.experiment-run.v1", "status": "accepted",
            "reason": "accepted", "modelCalled": True, "stateMutated": True,
            "externalActionPerformed": True, "response": "PRIVATE SENTINEL",
        })
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.run_scheduled_cycle.subprocess.run",
        ) as run:
            root = Path(directory) / "local-agent-company"
            state = Path(directory) / "state"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "local_ai.py").write_text("# fixture\n", encoding="utf-8")
            run.side_effect = [
                subprocess.CompletedProcess([], 0, cycle, ""),
                subprocess.CompletedProcess([], 0, pending, ""),
                subprocess.CompletedProcess([], 0, unsafe, ""),
            ]
            with redirect_stdout(io.StringIO()):
                code, journal = run_scheduled_cycle(root, state)
            self.assertEqual(code, 2)
            self.assertEqual(journal["reason"], "scheduled_experiment_receipt_invalid")
            stored = (state / "autopilot-cycle-result.json").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE SENTINEL", stored)

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
