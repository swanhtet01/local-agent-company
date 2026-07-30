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
    EXECUTION_FOCUS_HANDOFF_CONFIRMATION,
    EXECUTION_FOCUS_FILENAME,
    clear_execution_focus,
    enforce_execution_focus,
    enforce_execution_resource_envelope,
    execution_focus_digest,
    handoff_execution_focus,
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
            self.assertEqual(active["revision"], 1)
            self.assertEqual(read_execution_focus(home), active)
            active_digest = execution_focus_digest(active)
            cleared = clear_execution_focus(
                home, active_digest, "Pause model-backed company work for maintenance.",
            )
            self.assertFalse(cleared["enabled"])
            self.assertEqual(cleared["revision"], 2)
            self.assertEqual(cleared["handoff"]["priorFocusDigest"], active_digest)
            self.assertTrue((home / EXECUTION_FOCUS_FILENAME).is_file())
            self.assertEqual(read_execution_focus(home), cleared)

    def test_active_focus_requires_digest_bound_handoff_and_rejects_stale_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            current = set_execution_focus(home, "0123456789ab", "SuperMega", 4)
            with self.assertRaisesRegex(RuntimeError, "explicit digest-bound handoff"):
                set_execution_focus(home, "abcdef012345", "Other", 4)
            current_digest = execution_focus_digest(current)
            handed_off = handoff_execution_focus(
                home,
                "0123456789ab",
                "abcdef012345",
                "Other",
                2,
                current_digest,
                "Move the single active company outcome to the reviewed project.",
            )
            self.assertEqual(handed_off["projectId"], "abcdef012345")
            self.assertEqual(handed_off["revision"], current["revision"] + 1)
            self.assertEqual(handed_off["handoff"]["priorFocusDigest"], current_digest)
            with self.assertRaisesRegex(RuntimeError, "changed; refresh"):
                handoff_execution_focus(
                    home,
                    "0123456789ab",
                    "fedcba543210",
                    "Stale target",
                    1,
                    current_digest,
                    "Attempt a stale concurrent handoff after the focus changed.",
                )

    def test_legacy_focus_is_normalized_without_rewriting_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / EXECUTION_FOCUS_FILENAME
            legacy = {
                "schema": "local-company.execution-focus.v1",
                "enabled": True,
                "projectId": "0123456789ab",
                "projectName": "SuperMega",
                "maxRoles": 4,
                "updatedAt": "2026-07-30T00:00:00+00:00",
                "controls": {
                    "modelBackedCommandsOnly": True,
                    "externalWritesAllowed": False,
                    "bypassAllowed": False,
                },
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")
            normalized = read_execution_focus(home)
            self.assertEqual(normalized["schema"], "local-company.execution-focus.v2")
            self.assertEqual(normalized["revision"], 1)
            self.assertIsNone(normalized["handoff"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), legacy)

    def test_focus_mutation_rejects_unsafe_lock_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".execution-focus.lock").mkdir()
            with self.assertRaises((IsADirectoryError, RuntimeError, PermissionError)):
                set_execution_focus(home, "0123456789ab", "SuperMega", 4)

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
            argv = [
                "local-company", "--home", str(home), "focus", "clear",
                "--expected-focus-digest", f"sha256:{'0' * 64}",
                "--reason", "Reject and investigate the tampered focus control.",
                "--confirm", EXECUTION_FOCUS_HANDOFF_CONFIRMATION,
            ]
            stderr = io.StringIO()
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(main(), 2)
            self.assertIn("controls are invalid", stderr.getvalue())
            self.assertTrue(path.is_file())

    def test_cli_handoff_requires_idle_runtime_and_exact_current_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            source_id = company.create_project("SuperMega")
            target_id = company.create_project("Reviewed next project")
            current = set_execution_focus(home, source_id, "SuperMega", 4)
            argv = [
                "local-company", "--home", str(home), "focus", "handoff",
                "--from-project", "SuperMega", "--project", "Reviewed next project",
                "--max-roles", "2", "--expected-focus-digest", execution_focus_digest(current),
                "--reason", "Move the reviewed company outcome after current work completed.",
                "--confirm", EXECUTION_FOCUS_HANDOFF_CONFIRMATION,
            ]
            stdout = io.StringIO()
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 0)
            output = json.loads(stdout.getvalue())
            self.assertEqual(output["contract"], "local-company.execution-focus-observation.v1")
            self.assertEqual(output["focus"]["projectId"], target_id)
            self.assertEqual(output["focus"]["maxRoles"], 2)

            with patch.object(Company, "health_snapshot", return_value={
                "active_jobs": 1,
                "running_missions": 0,
                "pending_report_finalizations": 0,
                "pending_evaluations": 0,
                "pending_completion": [],
            }):
                stderr = io.StringIO()
                with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    self.assertEqual(main(), 2)
                self.assertIn("while local work is active", stderr.getvalue())

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

    def test_retry_role_override_preserves_focus_enforcement_and_parent_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            original_job_id, _ = company.run(
                "Prepare one decision-ready local operating plan",
                roles=["chief-of-staff", "research", "finance", "legal-risk", "quality"],
                project=project_id,
            )
            set_execution_focus(home, project_id, "SuperMega", 4)
            job_count = len(company.jobs())

            with self.assertRaisesRegex(RuntimeError, "denied before model load"):
                company.retry(
                    original_job_id,
                    roles=["chief-of-staff", "research", "finance", "legal-risk", "quality"],
                )
            self.assertEqual(len(company.jobs()), job_count)

            retry_job_id, _ = company.retry(
                original_job_id,
                roles=["chief-of-staff", "operations", "finance", "quality"],
            )
            self.assertEqual(company.job_detail(retry_job_id)["job"][5], original_job_id)

    def test_cli_retry_focus_preflight_uses_explicit_role_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            project_id = company.create_project("SuperMega")
            original_job_id, _ = company.run(
                "Prepare one decision-ready local operating plan",
                roles=["chief-of-staff", "research", "finance", "legal-risk", "quality"],
                project=project_id,
            )
            set_execution_focus(home, project_id, "SuperMega", 4)
            argv = [
                "local-company", "--home", str(home), "retry", original_job_id,
                "--roles", "chief-of-staff,operations,finance,quality", "--provider", "mock",
            ]
            stdout = io.StringIO()
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 0)
            self.assertIn("Completed retry job", stdout.getvalue())
            retry_job_id = company.jobs()[0][0]
            detail = company.job_detail(retry_job_id)
            self.assertEqual(detail["job"][5], original_job_id)
            self.assertEqual(
                [assignment[1] for assignment in detail["assignments"]],
                ["chief-of-staff", "operations", "finance", "quality"],
            )


if __name__ == "__main__":
    unittest.main()
