from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.local_ai import _brief_next_action, explain, main, run_autopilot, run_code, run_company, run_company_brief, run_cycle, run_experiment, run_experiment_agent, run_experiment_review, run_interactive_experiment_review, run_mission_candidate, run_mission_review, run_offer, run_offer_pack, run_pending_experiments, run_supermega_code, run_supermega_park_next, run_supermega_status, run_validation_pack, run_work, switch_project, translate
from scripts.run_scheduled_cycle import SCHEMA as SCHEDULED_CYCLE_SCHEMA, run_scheduled_cycle


def accepted_prompt_receipt(actions: list[str]) -> dict[str, object]:
    return {
        "schema": "local-ai.company-prompt-result.v1",
        "autoPermissionsEnabled": False, "externalActionPerformed": False,
        "modelCalled": True, "ok": True, "paidApiUsed": False,
        "reason": "accepted", "status": "accepted", "wallSeconds": 42.5,
        "model": "llama3.2:1b", "toolActions": actions,
        "toolCallCount": len(actions), "response": "Grounded local review.",
        "observedCost": 0.0, "modelUnloadedAfterRun": True, "agentExitCode": 0,
        "admissionAvailableBytes": 3 * 1024**3,
        "minimumAvailableBytesObserved": 2 * 1024**3,
        "peakIncrementalMemoryBytes": 1024**3,
        "peakIncrementalMemoryMb": 1024.0,
    }


class LocalAiLaunchpadTests(unittest.TestCase):
    def setUp(self) -> None:
        # `supermega ...` is gated behind an explicit opt-in (see
        # test_supermega_shortcuts_are_absent_and_unmentioned_by_default
        # below) since it's a shortcut around one project the maintainer
        # happens to have, not a general Local Workcell feature. Every
        # other test in this class exercises its *behavior*, which is
        # unaffected by the gate, so enable it once here rather than
        # patching each test individually.
        self._supermega_shortcuts_patcher = patch.dict(
            os.environ, {"SUPERMEGA_PROJECT_SHORTCUTS_ENABLED": "1"},
        )
        self._supermega_shortcuts_patcher.start()
        self.addCleanup(self._supermega_shortcuts_patcher.stop)

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
        self.assertEqual(translate(["offer-pack", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["validation-pack", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["mission-candidate", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["mission-review", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["experiment-pending"]).command, ())
        self.assertEqual(translate(["experiment-review-interactive"]).command, ())
        self.assertEqual(translate([
            "experiment-review", "a" * 12, "--decision", "accepted",
            "--outcome-reason", "none", "--corrections", "0",
            "--paid-setup", "unknown",
            "--confirm-human-review",
        ]).mode, "experiment-review")
        self.assertEqual(translate(["work", "Build a plan"]).command, ("run", "Build a plan"))
        self.assertEqual(translate(["later", "Research market"]).command, ("queue", "add", "Research market"))
        self.assertEqual(translate(["next"]).command, ("queue", "preflight"))
        self.assertEqual(translate(["run-next"]).command, ("queue", "run-next"))
        self.assertEqual(translate(["cycle", "--model", "local"]).command, ("--model", "local"))
        self.assertEqual(translate(["cycle", "--recover-memory"]).command, ("--recover-memory",))
        with self.assertRaisesRegex(ValueError, "cycle_recover_memory_may_be_used_once"):
            translate(["cycle", "--recover-memory", "--recover-memory"])
        self.assertEqual(translate(["dashboard"]).command, ("service", "start"))
        self.assertEqual(translate(["data", "list"]).command, ("datasets", "list"))
        self.assertEqual(translate(["new", "Future Lab"]).command, ("projects", "create", "Future Lab"))
        self.assertEqual(translate(["use", "Future Lab"]).command, ("Future Lab",))
        self.assertEqual(translate(["vision"]).command, ("--vision",))
        self.assertEqual(translate(["vision-lite", "--check"]).command, ("--vision-lite", "--check"))
        self.assertEqual(translate(["web", "doctor"]).command, ("browser", "doctor"))
        self.assertEqual(translate(["web", "install"]).command, ("browser", "install"))
        self.assertEqual(
            translate(["web", "template", "customer.json"]).command,
            ("browser", "suite-template", "customer.json"),
        )
        self.assertEqual(
            translate(["web", "seal", "customer.json", "--output", "approved.json"]).command,
            ("browser", "suite-seal", "customer.json", "--output", "approved.json"),
        )
        self.assertEqual(
            translate(["web", "suite", "approved.json", "--runs", "3"]).command,
            ("browser", "suite", "approved.json", "--runs", "3"),
        )
        self.assertEqual(
            translate(["web", "https://supermega.dev", "--expect-text", "SuperMega"]).command,
            ("browser", "check", "https://supermega.dev", "--expect-text", "SuperMega"),
        )
        with patch.dict(os.environ, {"SUPERMEGA_RELEASE_CHECK_ENABLED": "1"}):
            self.assertEqual(
                translate(["web", "supermega", "--runs", "10"]).command,
                ("browser", "supermega-release", "--runs", "10"),
            )
        self.assertEqual(translate(["autopilot", "status"]).command, ("status",))
        self.assertEqual(translate(["autopilot", "repair"]).command, ("repair",))
        self.assertEqual(translate(["brief"]).command, ())
        self.assertEqual(
            translate(["ask", "What", "next?"]).command,
            ("--scope", "company", "--question", "What next?", "--plain"),
        )
        self.assertEqual(
            translate(["ask", "--json", "What next?"]).command,
            ("--scope", "company", "--question", "What next?"),
        )
        self.assertEqual(translate(["supermega"]).mode, "supermega-status")
        self.assertEqual(
            translate(["supermega", "ask", "What next?"]).command,
            ("--scope", "supermega", "--question", "What next?", "--plain"),
        )
        self.assertEqual(translate(["supermega", "next"]).command, ("queue", "preflight"))
        self.assertEqual(translate(["supermega", "park-next"]).mode, "supermega-park-next")
        self.assertEqual(translate(["supermega", "proof"]).command, ("SuperMega",))
        self.assertEqual(
            translate(["supermega", "prove"]).command,
            ("SuperMega", "--recover-memory"),
        )
        self.assertEqual(translate(["supermega", "pending"]).command, ("SuperMega",))
        self.assertEqual(translate(["supermega", "review"]).command, ("SuperMega",))
        self.assertEqual(translate(["supermega", "dossier"]).command, ("SuperMega",))
        self.assertEqual(
            translate(["supermega", "mission-candidate"]).mode, "mission-candidate",
        )
        self.assertEqual(
            translate(["supermega", "mission-review"]).mode, "mission-review",
        )
        self.assertEqual(
            translate(["supermega", "plan", "Review release proof"]).command,
            ("preflight", "Review release proof", "--project", "SuperMega"),
        )
        self.assertEqual(
            translate(["supermega", "later", "Review release proof"]).command,
            ("queue", "add", "Review release proof", "--project", "SuperMega"),
        )
        self.assertEqual(translate(["supermega", "code", "--check"]).command, ("--check",))

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
        cases = [(["unknown"], "launchpad_command_unknown"), (["ask"], "ask_objective_required"), (["work"], "work_objective_required"), (["company"], "company_command_required"), (["autopilot"], "autopilot_action_required"), (["web"], "web_url_or_doctor_required"), (["supermega", "ask"], "supermega_ask_objective_required"), (["supermega", "plan"], "supermega_plan_objective_required"), (["supermega", "unknown"], "supermega_operation_unknown")]
        for args, reason in cases:
            error = io.StringIO()
            with self.subTest(args=args), redirect_stderr(error):
                self.assertEqual(main(args), 2)
                self.assertEqual(json.loads(error.getvalue())["reason"], reason)

    def test_supermega_shortcuts_are_absent_and_unmentioned_by_default(self) -> None:
        # `supermega ...` is a convenience layer around one project the
        # maintainer happens to have, not a general Local Workcell feature.
        # A general user of this now-public tool has no project named
        # "SuperMega" -- the command must be unreachable AND unmentioned in
        # --help unless explicitly enabled, the same as every other test in
        # this class enables it via setUp.
        #
        # HELP text is printed through _localize_command(), which rewrites
        # the "local-ai.cmd" placeholder to "./local-ai" on POSIX -- so
        # assertions here must go through it too rather than hardcoding the
        # Windows spelling directly.
        import scripts.local_ai as launchpad

        localize = launchpad._localize_command
        with patch.dict(os.environ, {"SUPERMEGA_PROJECT_SHORTCUTS_ENABLED": ""}):
            with self.assertRaises(ValueError):
                translate(["supermega"])
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(["supermega", "ask", "What next?"]), 2)
            self.assertEqual(
                json.loads(error.getvalue())["reason"], "launchpad_command_unknown",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)
            help_text = output.getvalue()
            self.assertIn("Local AI Launchpad", help_text)  # sanity: help still renders
            self.assertNotIn(localize("local-ai.cmd supermega"), help_text)
            self.assertNotIn(localize("local-ai.cmd web supermega"), help_text)
            self.assertIn(localize("local-ai.cmd ask"), help_text)  # sanity: unrelated lines survive
            # An unrelated example line uses supermega.dev as a demo URL for
            # the general `web` audit command -- that's not part of either
            # gated block and must survive untouched.
            self.assertIn("web https://supermega.dev", help_text)

        # The explicit opt-in restores it exactly as every other test here
        # already relies on via setUp.
        self.assertEqual(translate(["supermega"]).mode, "supermega-status")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn(localize("local-ai.cmd supermega"), output.getvalue())

    def test_web_supermega_fails_closed_by_default_instead_of_reaching_argparse(self) -> None:
        # `web supermega` is a distinct gate from the `supermega ...`
        # project-shortcut namespace above (SUPERMEGA_RELEASE_CHECK_ENABLED,
        # not SUPERMEGA_PROJECT_SHORTCUTS_ENABLED) but was previously
        # ungated in translate() even though _visible_help() already hid it
        # and cli.py never registered the "supermega-release" browser
        # subcommand without the same env var. Before the fix, a disabled
        # install would build a LaunchAction anyway, main() would shell out
        # to local_company.cli, and argparse would reject the unregistered
        # subcommand with a raw "invalid choice" usage dump and exit 2 --
        # not the launchpad's normal structured JSON error.
        with patch.dict(os.environ, {"SUPERMEGA_RELEASE_CHECK_ENABLED": ""}):
            with self.assertRaises(ValueError):
                translate(["web", "supermega"])
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(["web", "supermega"]), 2)
            self.assertEqual(
                json.loads(error.getvalue())["reason"], "launchpad_command_unknown",
            )

        with patch.dict(os.environ, {"SUPERMEGA_RELEASE_CHECK_ENABLED": "1"}):
            self.assertEqual(
                translate(["web", "supermega", "--runs", "10"]).command,
                ("browser", "supermega-release", "--runs", "10"),
            )

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
                "--outcome-reason", "none", "--corrections", "0",
                "--paid-setup", "unknown",
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
            self.assertEqual(status["reviews"][0]["outcome_reason"], "none")

            update_output = io.StringIO()
            update_action = translate([
                "experiment-review", pending_id, "--decision", "rejected",
                "--outcome-reason", "not_actionable", "--corrections", "1",
                "--paid-setup", "no", "--confirm-human-review",
            ])
            with patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}), redirect_stdout(update_output):
                self.assertEqual(run_experiment_review(update_action), 0)
            updated = json.loads(update_output.getvalue())
            self.assertFalse(updated["pendingArchived"])
            self.assertTrue(updated["reviewUpdated"])
            status = Company(home, MockModel()).product_evidence_status(project_id)
            self.assertEqual(status["reviews"][0]["outcome_reason"], "not_actionable")

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

            history_output = io.StringIO()
            history_answers = iter([
                pending_id, "rejected", "incomplete", "2", "no", "REVIEW",
            ])
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("builtins.input", side_effect=lambda _prompt: next(history_answers)),
                redirect_stdout(history_output),
            ):
                self.assertEqual(
                    run_interactive_experiment_review(
                        translate(["experiment-review-interactive"]),
                    ), 0,
                )
            self.assertIn("Archived measured product experiments", history_output.getvalue())
            status = Company(home, MockModel()).product_evidence_status(project_id)
            self.assertEqual(status["reviews"][0]["outcome_reason"], "incomplete")

    def test_sealed_mission_candidate_is_pathless_read_only_and_integrity_checked(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            company = Company(home, MockModel())
            project_id = company.create_project("Future Lab")
            job_id, _ = company.run("Review business workflow", project=project_id)
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_mission_candidate(
                        translate(["mission-candidate", "Future Lab"]),
                    ), 0,
                )
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "candidate_ready")
            self.assertEqual(receipt["candidate"]["jobId"], job_id)
            self.assertEqual(receipt["jobResult"]["job"]["jobId"], job_id)
            self.assertRegex(receipt["candidate"]["reportSha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                receipt["candidate"]["evidenceManifestSha256"], r"^[0-9a-f]{64}$",
            )
            self.assertNotIn(str(home), output.getvalue())
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])
            self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 0)

    def test_sealed_mission_review_records_only_confirmed_human_values(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            company = Company(home, MockModel())
            project_id = company.create_project("Future Lab")
            job_id, _ = company.run("Review business workflow", project=project_id)
            answers = iter([
                "business", "accepted", "0", "unknown", "512",
                "RECORD HUMAN PRODUCT EVIDENCE REVIEW",
            ])
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_mission_review(translate(["mission-review", "Future Lab"])), 0,
                )
            receipt = json.loads(output.getvalue().strip().splitlines()[-1])
            self.assertEqual(receipt["status"], "recorded")
            self.assertEqual(receipt["jobId"], job_id)
            self.assertTrue(receipt["recorded"])
            self.assertFalse(receipt["modelCalled"])
            self.assertTrue(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])
            status = company.product_evidence_status(project_id)
            self.assertEqual(status["reviewed_missions"], 1)
            self.assertEqual(status["complete_measurements"], 1)
            self.assertEqual(status["reviews"][0]["peak_memory_mb"], 512)

    def test_sealed_mission_review_cancellation_records_nothing(self) -> None:
        from local_company.core import Company, MockModel

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            company = Company(home, MockModel())
            project_id = company.create_project("Future Lab")
            company.run("Review coding workflow", project=project_id)
            answers = iter([
                "coding", "rejected", "incomplete", "1", "no", "", "CANCEL",
            ])
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(
                    ValueError, "mission_review_confirmation_required",
                ),
            ):
                run_mission_review(translate(["mission-review", "Future Lab"]))
            self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 0)

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

    def test_offer_pack_writes_only_a_ready_deterministic_owner_draft(self) -> None:
        blocked = {
            "status": "evidence_required", "missingProof": ["tenMeasuredCrossCategoryReviews"],
        }
        ready = {
            "schema": "local-company.mcp-product-offer-next.v1",
            "status": "ready_for_owner_packaging",
            "project": {"id": "1" * 12, "name": "Future Lab"},
            "missingProof": [],
            "offer": {
                "workflow": "Repository safety audit", "category": "coding",
                "evidenceRuns": 2, "maximumCorrectionsObserved": 1,
                "maximumPeakMemoryMbObserved": 768,
                "runtimeSecondsRange": {"minimum": 12.5, "maximum": 21.0},
                "package": ["private local installation", "operator training"],
                "allowedClaims": ["2 integrity-checked local runs were accepted"],
                "prohibitedClaims": ["guaranteed customer ROI"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            root = Path(directory) / "repo"
            root.mkdir()
            output = io.StringIO()
            with patch("scripts.local_ai._product_offer_result", return_value=(home, blocked)), redirect_stdout(output):
                self.assertEqual(run_offer_pack(translate(["offer-pack"]), root), 1)
            self.assertFalse((home / "offer-packs").exists())
            blocked_receipt = json.loads(output.getvalue())
            self.assertFalse(blocked_receipt["packStored"])
            self.assertFalse(blocked_receipt["stateMutated"])

            receipts = []
            with patch("scripts.local_ai._product_offer_result", return_value=(home, ready)):
                for _ in range(2):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(run_offer_pack(translate(["offer-pack"]), root), 0)
                    receipts.append(json.loads(output.getvalue()))
            self.assertTrue(receipts[0]["packStored"])
            self.assertTrue(receipts[0]["stateMutated"])
            self.assertFalse(receipts[1]["packStored"])
            self.assertFalse(receipts[1]["stateMutated"])
            self.assertEqual(receipts[0]["offerPackId"], receipts[1]["offerPackId"])
            target = Path(receipts[0]["offerPackPath"])
            content = target.read_text(encoding="utf-8")
            self.assertIn("Status: owner-review draft", content)
            self.assertIn("Claims this pack does not support", content)
            self.assertFalse(receipts[0]["externalPublicationAuthorized"])

    def test_validation_pack_writes_current_scoreboard_and_next_experiment(self) -> None:
        source = {
            "evidence": {
                "project": {"id": "1" * 12, "name": "Future Lab"},
                "mission_target": 10, "reviewed_missions": 3,
                "remaining_missions": 7, "complete_measurements": 2,
                "stale_review_count": 0, "average_corrections": 1.0,
                "category_counts": {"business": 1, "coding": 1, "data-research": 1},
                "decision_counts": {"accepted": 2, "rejected": 1},
                "paid_setup_signal_counts": {"no": 1, "unknown": 2, "yes": 0},
                "outcome_reason_counts": {
                    "inaccurate": 0, "incomplete": 0, "legacy_unspecified": 0,
                    "none": 2, "not_actionable": 1, "other": 0,
                    "too_resource_heavy": 0, "too_slow": 0,
                    "tool_failure": 0, "unsafe": 0,
                },
                "missing_proof": ["ten_current_reviewed_missions"],
                "reviews": [{
                    "source": "external_experiment", "label": "Coding operability review",
                    "category": "coding", "decision": "accepted", "corrections": 1,
                    "paid_setup_signal": "unknown", "runtime_seconds": 15.5,
                    "peak_memory_mb": 512, "outcome_reason": "none",
                }],
            },
            "experiment": {
                "selectedCategory": "business",
                "experiment": {"label": "Business evidence decision"},
            },
            "offer": {
                "status": "evidence_required",
                "gateResults": {
                    "tenMeasuredCrossCategoryReviews": False,
                    "repeatedAcceptedPaidSetupWorkflow": False,
                    "staleBindingsAbsent": True,
                },
                "missingProof": ["tenMeasuredCrossCategoryReviews"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            root = Path(directory) / "repo"
            root.mkdir()
            receipts = []
            with patch("scripts.local_ai._product_validation_result", return_value=(home, source)):
                for _ in range(2):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(run_validation_pack(translate(["validation-pack"]), root), 0)
                    receipts.append(json.loads(output.getvalue()))
            self.assertTrue(receipts[0]["packStored"])
            self.assertFalse(receipts[1]["packStored"])
            self.assertEqual(receipts[0]["validationPackId"], receipts[1]["validationPackId"])
            content = Path(receipts[0]["validationPackPath"]).read_text(encoding="utf-8")
            self.assertIn("| Human-reviewed missions | 3 | 10 |", content)
            self.assertIn("local-ai.cmd experiment-run --recover-memory", content)
            self.assertIn("not a sales claim", content)
            self.assertFalse(receipts[0]["externalPublicationAuthorized"])

    def test_company_brief_prioritizes_trust_activity_review_queue_offer_and_experiment(self) -> None:
        ready = {"status": "ready", "verified": True, "currentActivity": "idle"}
        cases = [
            ({"status": "mismatch", "verified": False}, "none", [], 0, "evidence_required", True, "repair_autopilot"),
            ({**ready, "currentActivity": "queued_by_windows"}, "ready", [], 1, "ready_for_owner_packaging", True, "wait_for_idle_or_cycle_completion"),
            (ready, "blocked", ["knowledge_changed"], 1, "evidence_required", True, "review_changed_project_knowledge"),
            (ready, "owner_gate_required", [], 1, "evidence_required", True, "review_queued_mission_owner_gate"),
            (ready, "none", [], 1, "evidence_required", True, "review_pending_product_experiment"),
            (ready, "ready", [], 0, "ready_for_owner_packaging", True, 3 * 1024**3, "run_ready_mission_now_or_await_autopilot"),
            (ready, "ready", [], 0, "evidence_required", True, 1024**3, "free_memory_then_run_ready_mission"),
            (ready, "ready", [], 0, "evidence_required", True, None, "inspect_memory_before_ready_mission"),
            (ready, "none", [], 0, "ready_for_owner_packaging", True, "owner_review_sellable_offer"),
            (ready, "none", [], 0, "project_focus_required", False, "select_active_product_project"),
            (ready, "none", [], 0, "evidence_required", True, "await_or_run_next_measured_product_experiment"),
        ]
        cases = [
            (*case[:6], 3 * 1024**3, case[6]) if len(case) == 7 else case
            for case in cases
        ]
        for autonomy, queue, blockers, pending, offer, focus, available, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    _brief_next_action(
                        autonomy, queue, blockers, pending, offer, focus, available,
                    )[0], expected,
                )

    def test_company_brief_is_pathless_model_free_and_queue_actionable(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "company"
            company = Company(home, MockModel())
            project_id = company.create_project("Future Lab")
            set_execution_focus(home, project_id, "Future Lab", 4)
            company.enqueue("Prepare one internal product decision", project_id)
            autonomy = {
                "schema": "local-ai.autonomy-task.v1", "status": "ready",
                "verified": True, "taskExecutionState": "Ready",
                "currentActivity": "idle", "recommendedAction": "none",
                "nextRunTime": "2030-01-01T00:00:00+00:00",
                "lastCycleCurrentForLastRun": True,
            }
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai._read_autopilot_status", return_value=autonomy),
                patch("scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3),
                redirect_stdout(output),
            ):
                self.assertEqual(run_company_brief(translate(["brief"])), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["nextAction"], "run_ready_mission_now_or_await_autopilot")
            self.assertTrue(receipt["resources"]["memoryAdmissionReady"])
            self.assertEqual(receipt["product"]["projectName"], "Future Lab")
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])
            self.assertNotIn(str(home), str(receipt))

    def test_human_brief_renders_next_action_without_leaking_paths(self) -> None:
        from scripts.local_ai import _render_brief_text

        payload = {
            "schema": "local-ai.company-brief.v1", "status": "ready",
            "autonomy": {"status": "not_installed"},
            "queue": {"status": "blocked", "blockers": ["knowledge_changed"]},
            "product": {
                "projectName": "Future Lab", "offerStatus": "evidence_required",
                "offerMissingProof": ["tenMeasuredCrossCategoryReviews"],
                "pendingExperimentCount": 2,
            },
            "resources": {
                "availableMemoryBytes": 784187392,
                "minimumExecutionMemoryBytes": 2 * 1024**3,
                "memoryAdmissionReady": False,
                "memoryShortfallBytes": 1363296256,
            },
            "nextAction": "repair_autopilot",
            "command": "local-ai.cmd autopilot repair",
        }
        text = _render_brief_text(payload)
        self.assertIn("Repair the autopilot task", text)
        self.assertIn("local-ai.cmd autopilot repair", text)
        self.assertIn("Future Lab", text)
        self.assertIn("0.7 GiB free", text)
        self.assertIn("1.3 GiB short", text)
        self.assertIn("ten measured cross category reviews", text)
        self.assertIn("2 experiments waiting for your review", text)
        # Raw identifiers are the machine contract, not the human display.
        self.assertNotIn("repair_autopilot", text)
        self.assertNotIn("availableMemoryBytes", text)
        # A queue blocker must sit under Queue, never under the Offer row.
        queue_line = text.index("Queue")
        self.assertLess(queue_line, text.index("blocked by"))
        self.assertLess(text.index("blocked by"), text.index("Offer"))

    def test_human_brief_degrades_on_unknown_values_instead_of_failing(self) -> None:
        from scripts.local_ai import _render_brief_text

        text = _render_brief_text({
            "autonomy": {"status": "someBrandNewState"},
            "queue": {"status": None, "blockers": "not-a-list"},
            "product": {},
            "resources": {},
            "nextAction": "an_unmapped_next_action",
            "command": "",
        })
        self.assertIn("some brand new state", text)
        self.assertIn("An unmapped next action", text)
        self.assertIn("cannot be measured", text)
        self.assertIn("Local company", text)

    def test_suggested_commands_name_a_launcher_that_exists_on_this_platform(self) -> None:
        import scripts.local_ai as launchpad

        root = Path(__file__).resolve().parents[1]
        # Both launchers must ship, or one platform gets told to run a file
        # that is not there.
        self.assertTrue((root / "local-ai.cmd").is_file())
        self.assertTrue((root / "local-ai").is_file())

        original = launchpad.LAUNCHER
        try:
            launchpad.LAUNCHER = "./local-ai"
            self.assertEqual(
                launchpad._localize_command("local-ai.cmd autopilot repair"),
                "./local-ai autopilot repair",
            )
            self.assertNotIn("local-ai.cmd", launchpad._localize_command(launchpad.HELP))
            self.assertIsNone(launchpad._localize_command(None))
            launchpad.LAUNCHER = "local-ai.cmd"
            self.assertEqual(
                launchpad._localize_command("local-ai.cmd brief"), "local-ai.cmd brief",
            )
        finally:
            launchpad.LAUNCHER = original

    def test_posix_launchers_are_executable_shell_scripts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name, module in (
            ("local-ai", "scripts/local_ai.py"),
            ("local-company", "local_company.cli"),
            ("company-mcp", "local_company.mcp_server"),
        ):
            with self.subTest(launcher=name):
                source = (root / name).read_text(encoding="utf-8")
                self.assertTrue(source.startswith("#!/bin/sh"))
                self.assertIn(module, source)
                # A caller's PYTHONPATH must survive, and the venv is preferred.
                self.assertIn(".venv/bin/python", source)
                self.assertNotIn("\r\n", source)

    def test_brief_defaults_to_json_for_programmatic_callers(self) -> None:
        # run_local_brief_assistant parses this receipt; the human renderer
        # must never become the default for in-process callers.
        from scripts.local_ai import run_company_brief

        signature = inspect.signature(run_company_brief)
        self.assertEqual(signature.parameters["render"].default, "json")
        self.assertEqual(translate(["brief"]).command, ())
        self.assertEqual(translate(["brief", "--json"]).command, ("--json",))
        with self.assertRaises(ValueError):
            translate(["brief", "--pretty"])

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
        self.assertIn('local-ai.cmd" validation-pack', source)
        self.assertIn('local-ai.cmd" offer-pack', source)
        self.assertIn('local-ai.cmd" code "%LOCAL_AI_PROJECT_PATH%"', source)
        self.assertIn('local-company-agent.cmd" --check', source)
        self.assertIn('local-ai.cmd" cycle --recover-memory', source)
        self.assertIn('local-ai.cmd" dashboard', source)
        self.assertIn('local-ai.cmd" ask "%LOCAL_AI_QUESTION%"', source)
        self.assertIn('local-ai.cmd" supermega', source)
        self.assertIn(
            'local-ai.cmd" supermega ask "%SUPERMEGA_QUESTION%"', source,
        )
        self.assertIn('local-ai.cmd" supermega next', source)
        self.assertIn('local-ai.cmd" supermega park-next', source)
        self.assertIn('local-ai.cmd" supermega proof', source)
        self.assertIn('local-ai.cmd" supermega prove', source)
        self.assertIn('local-ai.cmd" supermega review', source)
        self.assertIn('local-ai.cmd" supermega mission-review', source)
        self.assertIn('local-ai.cmd" supermega dossier', source)
        self.assertIn('if /I "%~1"=="--supermega" goto supermega_menu', source)
        self.assertNotIn("taskkill", source.lower())
        self.assertNotIn("powershell", source.lower())

    def test_supermega_status_is_pathless_grounded_and_read_only(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            repository = base / "supermega-platform"
            home = base / "company"
            root.mkdir()
            repository.mkdir()
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, project_id)
            set_execution_focus(home, project_id, "SuperMega", 4)
            git_status = subprocess.CompletedProcess([], 0, "## main...origin/main\n", "")
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=git_status),
                patch("scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3),
                redirect_stdout(output),
            ):
                self.assertEqual(run_supermega_status(translate(["supermega"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["repository"]["branch"], "main")
            self.assertTrue(receipt["repository"]["clean"])
            self.assertEqual(receipt["project"]["projectId"], project_id)
            self.assertTrue(receipt["knowledge"]["readyForUse"])
            self.assertTrue(receipt["focus"]["supermegaActive"])
            self.assertEqual(receipt["queue"]["status"], "no_due_mission")
            self.assertEqual(receipt["productProof"]["reviewedMissions"], 0)
            self.assertEqual(receipt["productProof"]["missionTarget"], 10)
            self.assertEqual(receipt["productProof"]["pendingReviewCount"], 0)
            self.assertEqual(receipt["sealedMissionReview"]["status"], "no_candidate")
            self.assertEqual(receipt["nextAction"], "run_next_supermega_product_proof")
            self.assertEqual(receipt["command"], "local-ai.cmd supermega prove")
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])
            self.assertNotIn(str(base), output.getvalue())

    def test_supermega_status_surfaces_incompatible_queue_head(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            repository = base / "supermega-platform"
            home = base / "company"
            root.mkdir()
            repository.mkdir()
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, project_id)
            set_execution_focus(home, project_id, "SuperMega", 4)
            git_status = subprocess.CompletedProcess([], 0, "## main...origin/main\n", "")
            blocked = {
                "schema": "local-company.queue-preflight.v1",
                "status": "blocked", "queue_id": "a" * 12,
                "project_id": "b" * 12,
                "blockers": ["execution_focus_mismatch"],
                "owner_gate_categories": [],
                "submission_allowed": False,
                "model_execution_ready": False,
                "effects": {
                    "queue_claimed": False, "job_created": False,
                    "model_called": False, "state_mutated": False,
                    "work_started": False,
                },
            }
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=git_status),
                patch("scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3),
                patch.object(Company, "queue_preflight", return_value=blocked),
                redirect_stdout(output),
            ):
                self.assertEqual(run_supermega_status(translate(["supermega"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "attention")
            self.assertEqual(receipt["queue"]["queueId"], "a" * 12)
            self.assertTrue(receipt["queue"]["parkableForFocus"])
            self.assertEqual(receipt["queue"]["parkedCount"], 0)
            self.assertEqual(receipt["nextAction"], "park_incompatible_queue_head")
            self.assertEqual(receipt["command"], "local-ai.cmd supermega park-next")
            self.assertNotIn(str(base), output.getvalue())

    def test_supermega_status_prioritizes_pending_human_product_review(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            repository = base / "supermega-platform"
            home = base / "company"
            root.mkdir()
            repository.mkdir()
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, project_id)
            set_execution_focus(home, project_id, "SuperMega", 4)
            pending = [{
                "experimentId": "a" * 12,
                "project": {"id": project_id, "name": "SuperMega"},
                "label": "Coding operability review", "category": "coding",
                "response": "private response", "model": "local",
                "wallSeconds": 1.0, "peakIncrementalMemoryMb": 10,
            }]
            git_status = subprocess.CompletedProcess([], 0, "## main...origin/main\n", "")
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=git_status),
                patch("scripts.local_ai.available_memory_bytes", return_value=3 * 1024**3),
                patch("scripts.local_ai._pending_experiment_items", return_value=pending),
                redirect_stdout(output),
            ):
                self.assertEqual(run_supermega_status(translate(["supermega"]), root), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "attention")
            self.assertEqual(receipt["productProof"]["pendingReviewCount"], 1)
            self.assertEqual(
                receipt["nextAction"], "review_pending_supermega_product_proof",
            )
            self.assertEqual(receipt["command"], "local-ai.cmd supermega review")
            self.assertNotIn("private response", output.getvalue())
            self.assertNotIn(str(base), output.getvalue())

    def test_supermega_status_prioritizes_sealed_mission_review_without_model(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            repository = base / "supermega-platform"
            home = base / "company"
            root.mkdir()
            repository.mkdir()
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, project_id)
            set_execution_focus(home, project_id, "SuperMega", 4)
            job_id, _ = company.run("Review business workflow", project=project_id)
            git_status = subprocess.CompletedProcess(
                [], 0, "## main...origin/main\n", "",
            )
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                patch("scripts.local_ai.subprocess.run", return_value=git_status),
                patch(
                    "scripts.local_ai.available_memory_bytes",
                    return_value=512 * 1024**2,
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(run_supermega_status(translate(["supermega"]), root), 0)
            receipt = json.loads(output.getvalue())
            review = receipt["sealedMissionReview"]
            self.assertEqual(receipt["status"], "attention")
            self.assertEqual(review["status"], "candidate_ready")
            self.assertEqual(review["jobId"], job_id)
            self.assertTrue(review["humanReviewRequired"])
            self.assertRegex(review["reportSha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                receipt["nextAction"], "review_sealed_supermega_mission",
            )
            self.assertEqual(
                receipt["command"], "local-ai.cmd supermega mission-review",
            )
            self.assertNotIn("Review business workflow", output.getvalue())
            self.assertNotIn(str(base), output.getvalue())

    def test_supermega_can_park_only_an_exact_incompatible_queue_head(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            home = base / "company"
            root.mkdir()
            company = Company(home, MockModel())
            older_id = company.create_project("Older Lab")
            supermega_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, older_id)
            company.add_knowledge(evidence, supermega_id)
            blocked_queue = company.enqueue(
                "Prepare one internal advisory brief", older_id, priority=70,
            )
            active_queue = company.enqueue(
                "Prepare one internal SuperMega advisory brief",
                supermega_id, priority=50,
            )
            set_execution_focus(home, supermega_id, "SuperMega", 4)
            output = io.StringIO()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    run_supermega_park_next(
                        translate(["supermega", "park-next"]), root,
                    ),
                    0,
                )
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "parked")
            self.assertEqual(receipt["parkedQueueId"], blocked_queue)
            self.assertEqual(receipt["nextQueue"]["queueId"], active_queue)
            self.assertEqual(receipt["nextQueue"]["status"], "ready")
            self.assertTrue(receipt["restorable"])
            self.assertFalse(receipt["modelCalled"])
            self.assertTrue(receipt["stateMutated"])
            self.assertFalse(receipt["externalActionPerformed"])
            self.assertEqual(company.queue_items("parked")[0][0], blocked_queue)
            self.assertNotIn("Prepare one", output.getvalue())
            self.assertNotIn(str(base), output.getvalue())

    def test_supermega_refuses_to_park_its_own_ready_queue_head(self) -> None:
        from local_company.core import Company, MockModel
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "local-agent-company"
            home = base / "company"
            root.mkdir()
            company = Company(home, MockModel())
            supermega_id = company.create_project("SuperMega")
            evidence = base / "evidence.md"
            evidence.write_text("Current local evidence.\n", encoding="utf-8")
            company.add_knowledge(evidence, supermega_id)
            queue_id = company.enqueue(
                "Prepare one internal SuperMega advisory brief", supermega_id,
            )
            set_execution_focus(home, supermega_id, "SuperMega", 4)
            database_before = company.db_path.read_bytes()
            with (
                patch.dict("os.environ", {"LOCAL_COMPANY_HOME": str(home)}),
                self.assertRaisesRegex(
                    ValueError, "supermega_queue_head_not_safely_parkable",
                ),
            ):
                run_supermega_park_next(
                    translate(["supermega", "park-next"]), root,
                )
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.queue_items("queued")[0][0], queue_id)

    def test_supermega_pending_view_filters_other_projects(self) -> None:
        items = [
            {
                "experimentId": "a" * 12,
                "project": {"id": "1" * 12, "name": "SuperMega"},
                "label": "SuperMega proof", "category": "coding",
                "response": "supermega", "model": "local",
                "wallSeconds": 1.0, "peakIncrementalMemoryMb": 10,
            },
            {
                "experimentId": "b" * 12,
                "project": {"id": "2" * 12, "name": "Other Lab"},
                "label": "Other proof", "category": "business",
                "response": "other", "model": "local",
                "wallSeconds": 1.0, "peakIncrementalMemoryMb": 10,
            },
        ]
        output = io.StringIO()
        with (
            patch("scripts.local_ai._pending_experiment_items", return_value=items),
            redirect_stdout(output),
        ):
            self.assertEqual(
                run_pending_experiments(translate(["supermega", "pending"])), 0,
            )
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["count"], 1)
        self.assertEqual(receipt["projectFilter"], "SuperMega")
        self.assertEqual(receipt["items"][0]["experimentId"], "a" * 12)
        self.assertNotIn("other", output.getvalue())

    def test_supermega_code_targets_exact_sibling_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("scripts.local_ai.subprocess.run") as run:
            root = Path(directory) / "local-agent-company"
            root.mkdir()
            run.return_value = subprocess.CompletedProcess([], 0)
            action = translate(["supermega", "code", "--check"])
            self.assertEqual(run_supermega_code(action, root), 0)
            self.assertEqual(
                run.call_args.args[0],
                [str(root / "local-code.cmd"), "--check", str(root.parent / "supermega-platform")],
            )

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

    def test_code_mode_rejects_arguments_that_desync_cmd_exes_quote_tracking(self) -> None:
        # Windows launches a .cmd target by handing the whole command line
        # to cmd.exe -- CreateProcess falls back to it for any .bat/.cmd
        # target even under subprocess.run's argv-list form with
        # shell=False -- and cmd.exe re-tokenizes that line for its own
        # quoting. Its quote-tracking toggles on every literal '"'
        # regardless of context, unlike the MSVCRT-style escaping
        # subprocess.list2cmdline() assumes it's producing, so one
        # embedded '"' can desync the parser and expose a trailing '&'/'|'
        # as a separate command cmd.exe actually executes -- reproduced by
        # hand: subprocess.run([local-code.cmd, "--run", 'proj" & echo
        # INJECTED & echo "']) really does execute the injected echo.
        # argv-list/shell=False alone is not a sufficient defense for .cmd
        # targets, so run_code() must reject these characters itself.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local-code.cmd").write_text("@exit /b 0\n", encoding="utf-8")
            unsafe_arguments = [
                'proj" & echo INJECTED & echo "',
                "safe1 | safe2",
                "safe1 & safe2",
                "safe1 < safe2",
                "safe1 > safe2",
                "safe1 ^ safe2",
                "safe1\r\nsafe2",
            ]
            for unsafe in unsafe_arguments:
                with self.subTest(unsafe=unsafe), patch(
                    "scripts.local_ai.subprocess.run",
                ) as run:
                    action = translate(["code", unsafe])
                    with self.assertRaisesRegex(
                        ValueError, "argument_contains_unsafe_characters",
                    ):
                        run_code(action, root)
                    run.assert_not_called()

            # A legitimate path with spaces, still no shell metacharacters,
            # must keep working exactly as before.
            action = translate(["code", "C:\\Project With Spaces"])
            with patch("scripts.local_ai.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0)
                self.assertEqual(run_code(action, root), 0)
            run.assert_called_once()

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

    def test_cycle_can_recover_memory_once_without_forwarding_control_flag(self) -> None:
        queue_id, job_id = "0123456789ab", "abcdef012345"
        ready = {
            "schema": "local-company.queue-preflight.v1", "status": "ready",
            "queue_id": queue_id, "reviewed_queue_matches": None,
            "submission_allowed": True, "model_execution_ready": True,
            "owner_gate_categories": [],
        }
        detail = {
            "job": [job_id, "objective", "complete", "time", "C:\\report.md"],
            "evaluation": {"passed": True, "score": 100, "checks": {"model_stopped_cleanly": True}},
        }
        completion = f"Queue item {queue_id} completed as job {job_id}; quality=passed\nReport: C:\\report.md\n"
        recovery = {
            "attempted": True, "status": "completed", "targetCount": 2,
            "trimSucceeded": 2, "trimFailed": 0, "releasedWorkingSetMb": 900.0,
            "processTerminationCalls": 0,
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.local_ai.subprocess.run",
        ) as run, patch(
            "scripts.local_ai.available_memory_bytes", side_effect=[1024**3, 3 * 1024**3],
        ), patch("scripts.local_ai._recover_memory", return_value=recovery) as recover:
            root = Path(directory)
            (root / "src").mkdir()
            run.side_effect = [
                subprocess.CompletedProcess([], 0, "Materialized 0 due schedule(s).\n", ""),
                subprocess.CompletedProcess([], 0, json.dumps(ready), ""),
                subprocess.CompletedProcess([], 0, json.dumps({**ready, "reviewed_queue_matches": True}), ""),
                subprocess.CompletedProcess([], 0, completion, ""),
                subprocess.CompletedProcess([], 0, json.dumps(detail), ""),
            ]
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(run_cycle(translate(["cycle", "--recover-memory"]), root), 0)
            receipt = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(receipt["memoryRecovery"], recovery)
            self.assertNotIn("--recover-memory", run.call_args_list[3].args[0])
            recover.assert_called_once_with(root)

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
