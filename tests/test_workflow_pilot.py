from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_company.cli import parser
from local_company.computer_use import (
    RUN_CONFIRMATION,
    WORKFLOW_SCHEMA,
    run_workflow,
    seal_workflow,
    workflow_root,
)
from local_company.workflow_pilot import (
    BASELINE_CONFIRMATION,
    REVIEW_CONFIRMATION,
    review_workflow_pilot_run,
    start_workflow_pilot,
    workflow_pilot_status,
)
from scripts.local_ai import explain, translate


def workflow_payload(
    *,
    name: str = "invoice-entry",
    process_name: str = "business-app.exe",
    expected_title: str | None = "Completed locally",
) -> dict[str, object]:
    return seal_workflow({
        "schema": WORKFLOW_SCHEMA,
        "name": name,
        "createdAt": "2026-08-05T00:00:00+00:00",
        "platform": "windows",
        "learning": {
            "source": "test",
            "typedCharactersStored": False,
            "screenshotsCaptured": False,
        },
        "steps": [{
            "id": 1,
            "action": "click",
            "delayBeforeMs": 0,
            "window": {
                "title": "Business App",
                "className": "BusinessWindow",
                "processName": process_name,
                "recordedBounds": [10, 10, 500, 400],
            },
            "target": {
                "name": "Apply",
                "automationId": "apply",
                "controlType": "ControlType.Button",
                "className": "Button",
                "recordedBounds": [100, 100, 200, 140],
            },
            "relativePoint": [0.3, 0.3],
        }],
        "expectedFinalWindowTitleContains": expected_title,
        "expectedFinalControlTextContains": None,
    })


def store_workflow(home: Path, payload: dict[str, object]) -> Path:
    path = workflow_root(home, create=True) / str(payload["name"]) / "workflow.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class PilotDesktop:
    def __init__(self) -> None:
        self.window = {
            "handle": 1,
            "title": "Completed locally",
            "className": "BusinessWindow",
            "processName": "business-app.exe",
            "processId": 99,
            "bounds": [10, 10, 500, 400],
        }

    def find_window(self, _signature):
        return self.window

    def resolve_target(self, _window, _target):
        return {"score": 100, "ties": 1, "bounds": [100, 100, 200, 140]}

    def click(self, _window, _target, _relative):
        return {"method": "uia_anchor", "point": [150, 120]}

    def inspect_controls(self, _window, _limit):
        return []


def start_pilot(home: Path) -> dict[str, object]:
    return start_workflow_pilot(
        home,
        "invoice-entry",
        observed_runs=3,
        observed_human_minutes_total=15,
        runs_per_week=10,
        observed_errors=1,
        observed_error_cost_total=25,
        outcome_label="Invoice status is Completed locally",
        confirmation=BASELINE_CONFIRMATION,
    )


def run_once(home: Path) -> dict[str, object]:
    return run_workflow(
        home,
        "invoice-entry",
        RUN_CONFIRMATION,
        adapter=PilotDesktop(),
        capture_evidence=False,
    )


def review_correct(home: Path, run_id: str) -> dict[str, object]:
    return review_workflow_pilot_run(
        home,
        "invoice-entry",
        run_id,
        observed_outcome="correct",
        correction_minutes=0,
        wrong_target_actions=0,
        external_effect_observed=False,
        confirmation=REVIEW_CONFIRMATION,
    )


class WorkflowPilotTests(unittest.TestCase):
    def test_start_requires_real_baseline_machine_outcome_and_safe_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            with self.assertRaisesRegex(ValueError, "workflow_pilot_observed_runs_invalid"):
                start_workflow_pilot(
                    home,
                    "invoice-entry",
                    observed_runs=2,
                    observed_human_minutes_total=10,
                    runs_per_week=5,
                    observed_errors=0,
                    observed_error_cost_total=0,
                    outcome_label="Completed",
                    confirmation=BASELINE_CONFIRMATION,
                )
            created = start_pilot(home)
            self.assertEqual(created["status"], "created")
            self.assertEqual(created["baseline"]["meanHumanMinutesPerRun"], 5)
            self.assertEqual(created["baseline"]["weeklyHumanMinutes"], 50)
            self.assertFalse(created["externalActionAuthorityGranted"])
            with self.assertRaises(FileExistsError):
                start_pilot(home)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload(expected_title=None))
            with self.assertRaisesRegex(ValueError, "workflow_pilot_machine_outcome_required"):
                start_pilot(home)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload(process_name="msedge.exe"))
            with self.assertRaisesRegex(ValueError, "workflow_pilot_network_or_shell_app_forbidden"):
                start_pilot(home)

    def test_run_receipt_is_hash_bound_and_pending_review_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            start_pilot(home)
            run = run_once(home)
            receipt = Path(str(run["receiptPath"]))
            sidecar = receipt.with_name("receipt.sha256")
            self.assertTrue(run["outcomeVerified"])
            self.assertEqual(len(run["receiptSha256"]), 64)
            self.assertEqual(
                sidecar.read_text(encoding="ascii").strip(),
                f"{run['receiptSha256']} *receipt.json",
            )
            status = workflow_pilot_status(home, "invoice-entry")
            self.assertEqual(status["status"], "collecting")
            self.assertEqual(status["nextReviewRunId"], run["runId"])
            self.assertEqual(status["reviewedRuns"], 0)
            self.assertFalse(status["promotionGatePassed"])

    def test_twenty_integrity_bound_human_reviewed_runs_pass_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            start_pilot(home)
            for _index in range(20):
                run = run_once(home)
                review_correct(home, str(run["runId"]))

            status = workflow_pilot_status(home, "invoice-entry")
            serialized = json.dumps(status)
            self.assertEqual(status["status"], "passed")
            self.assertEqual(status["totalRuns"], 20)
            self.assertEqual(status["reviewedRuns"], 20)
            self.assertEqual(status["acceptedRuns"], 20)
            self.assertEqual(status["consecutivePassingRuns"], 20)
            self.assertTrue(status["promotionGatePassed"])
            self.assertEqual(status["commercialEvidenceStatus"], "qualified")
            self.assertGreater(status["metrics"]["verifiedNetMinutesSaved"], 99)
            self.assertNotIn(str(home), serialized)
            self.assertNotIn("receiptPath", serialized)

    def test_human_rejection_breaks_the_streak_and_reviews_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            start_pilot(home)
            run = run_once(home)
            review_workflow_pilot_run(
                home,
                "invoice-entry",
                str(run["runId"]),
                observed_outcome="incorrect",
                correction_minutes=3,
                wrong_target_actions=1,
                external_effect_observed=False,
                confirmation=REVIEW_CONFIRMATION,
            )
            with self.assertRaises(FileExistsError):
                review_correct(home, str(run["runId"]))
            status = workflow_pilot_status(home, "invoice-entry")
            self.assertEqual(status["consecutivePassingRuns"], 0)
            self.assertEqual(status["acceptedRuns"], 0)
            self.assertEqual(status["metrics"]["verifiedCompletedBaselineMinutes"], 0)
            self.assertLess(status["metrics"]["verifiedNetMinutesSaved"], 0)

    def test_tampered_receipt_and_changed_workflow_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow_path = store_workflow(home, workflow_payload())
            start_pilot(home)
            run = run_once(home)
            receipt = Path(str(run["receiptPath"]))
            value = json.loads(receipt.read_text(encoding="utf-8"))
            value["status"] = "halted"
            receipt.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "run_receipt_digest_mismatch"):
                review_correct(home, str(run["runId"]))
            status = workflow_pilot_status(home, "invoice-entry")
            self.assertEqual(status["invalidRunReceipts"], 1)
            self.assertEqual(status["nextAction"], "inspect_integrity_failure_before_more_runs")

            changed = workflow_payload()
            changed["expectedFinalWindowTitleContains"] = "Different outcome"
            changed = seal_workflow(changed)
            workflow_path.write_text(json.dumps(changed), encoding="utf-8")
            status = workflow_pilot_status(home, "invoice-entry")
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["reason"], "workflow_changed_after_pilot_start")

    def test_cli_and_friendly_launcher_expose_measured_pilot_commands(self) -> None:
        parsed = parser().parse_args([
            "computer", "pilot-start", "invoice-entry",
            "--observed-runs", "3",
            "--observed-human-minutes-total", "15",
            "--runs-per-week", "10",
            "--outcome", "Invoice completed",
            "--confirm", BASELINE_CONFIRMATION,
        ])
        self.assertEqual(parsed.computer_command, "pilot-start")
        self.assertEqual(parsed.required_runs, 20)
        status = translate(["automate", "pilot-status", "invoice-entry"])
        self.assertEqual(status.command, ("computer", "pilot-status", "invoice-entry"))
        self.assertFalse(explain(status)["effects"]["localStateMayChange"])
        review = translate([
            "automate", "pilot-review", "invoice-entry", "20260805T000000000000Z",
            "--outcome", "correct", "--correction-minutes", "0",
            "--wrong-target-actions", "0", "--confirm", REVIEW_CONFIRMATION,
        ])
        self.assertEqual(review.command[:3], ("computer", "pilot-review", "invoice-entry"))
        self.assertTrue(explain(review)["effects"]["localStateMayChange"])


if __name__ == "__main__":
    unittest.main()
