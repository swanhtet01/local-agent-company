import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from local_company.cli import parser
from local_company.core import Company, MockModel, OllamaModel, PLAYBOOKS, source_limitation_conflicts
from local_company.dashboard import create_dashboard_server, render_dashboard
from local_company.service import _read_state, _startup_lock, _write_state


class RecordingModel:
    def __init__(self):
        self.prompts = []

    def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        return f"result-{len(self.prompts)}"


class CountingMockModel(MockModel):
    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        return super().complete(system, prompt)


class UncacheableMockModel:
    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        return MockModel().complete(system, prompt)


class VersionedMockModel(CountingMockModel):
    def __init__(self, runtime_version):
        super().__init__()
        self.runtime_version = runtime_version

    def cache_identity(self):
        return {**super().cache_identity(), "runtime_version": self.runtime_version}


class FailOnceModel(RecordingModel):
    def __init__(self):
        super().__init__()
        self.failed = False

    def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        if len(self.prompts) == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("simulated model interruption")
        return f"result-{len(self.prompts)}"


class TruncatedModel(MockModel):
    def complete(self, system, prompt):
        result = super().complete(system, prompt)
        self.last_metrics = {"done_reason": "length", "output_tokens": 64}
        return result


class BlockingModel(MockModel):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, system, prompt):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("blocking model test timed out")
        return super().complete(system, prompt)


class ConstraintModel(MockModel):
    def __init__(self, valid):
        self.valid = valid

    def complete(self, system, prompt):
        if "executive chair" not in system and "report editor" not in system:
            if self.valid:
                return (
                    "Verified local evidence is limited. Proposed work remains internal and reversible. "
                    "Assumptions require owner review before use."
                )
            return ("word " * 105) + "Ready to deploy. Scheduled: today."
        if self.valid:
            return (
                "Verified facts: the local queue and health evidence are available. "
                "Assumptions: operator adoption remains unmeasured. "
                "Task templates: Task template intake, Task template review, Task template audit. "
                "Daily review cadence: inspect once each day. "
                "Success checks: one accepted report and zero bypassed gates. "
                "Failure modes: truncation or unsupported claims. Owner gates: review every proposed action. "
                "Owner review required."
            )
        return (
            "The system achieved a 50% adoption gain and is ready to deploy. "
            "Approved and deployed immediately via file://path/to/[UNK_PLAN]. Owner review required."
        )


class RevisionModel(MockModel):
    def complete(self, system, prompt):
        if "report editor" in system:
            sections = [
                ("Verified facts", "local evidence remains available for careful internal review"),
                ("Assumptions", "operator adoption remains unmeasured and requires validation"),
                ("Task templates (three internal options)", "use intake review and audit templates for reversible work"),
                ("Daily review cadence", "inspect queue health and reports once every day"),
                ("Success checks (must pass)", "accept one grounded report with zero bypassed gates"),
                ("Failure modes", "watch for truncation missing evidence and unsupported claims"),
                ("Owner gates", "review every proposed action before any real execution"),
            ]
            return "\n\n".join(
                f"{label}: " + ((content + " ") * 5).strip() for label, content in sections
            ) + "\n\nOwner review required."
        if "executive chair" in system:
            return ("unstructured " * 220) + "Owner review required."
        return "specialist " * 130


class CitationModel(MockModel):
    def __init__(self, cited):
        self.cited = cited

    def complete(self, system, prompt):
        if "executive chair" not in system and "report editor" not in system:
            evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
            suffix = f" {evidence.group(0)}" if self.cited and evidence else ""
            return "Review the supplied local evidence and preserve every owner gate." + suffix
        evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
        evidence_tag = f" {evidence.group(0)}" if self.cited and evidence else ""
        citation = (
            f"notes.md confirms the imported operating fields{evidence_tag}"
            if self.cited else "Local evidence exists"
        )
        return (
            f"Verified facts: {citation}. Assumptions: adoption remains unmeasured. "
            "Task templates: intake review and audit. Daily review cadence: inspect work every day. "
            "Success checks: one accepted grounded report. Failure modes: missing evidence or truncation. "
            "Owner gates: review every proposed action. Owner review required."
        )


class ContradictingSourceModel(MockModel):
    def complete(self, system, prompt):
        return (
            "Verified facts: PostHog and Sentry are confirmed ready and connected for scaling client "
            "templates. Assumptions: operator adoption remains unmeasured and requires owner review. "
            "Proposed work stays internal and reversible. Owner review required."
        )


class EvidenceCitingModel(MockModel):
    def complete(self, system, prompt):
        evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
        evidence_tag = evidence.group(0) if evidence else "[EVIDENCE:missing]"
        if "executive chair" in system or "report editor" in system:
            return (
                f"Verified facts: notes.md records the local inventory baseline {evidence_tag}. "
                "Assumptions: future demand remains unknown and requires owner review. "
                "Recommendations remain local, reversible, and unexecuted. Owner review required."
            )
        return (
            f"Verified local evidence in notes.md records the inventory baseline {evidence_tag}. "
            "Future demand is an assumption, and all recommendations remain local and reversible."
        )


class FilenameOnlyCitationModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            return (
                "Verified facts: notes.md records the local inventory baseline [EVIDENCE:not-a-real-id]. "
                "Assumptions: future demand remains unknown and requires owner review. "
                "Recommendations remain local and reversible. Owner review required."
            )
        return "Review notes.md as local evidence, label assumptions, and preserve owner review."


class CompanyTests(unittest.TestCase):
    def test_service_state_is_atomic_and_startup_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = {"status": "starting", "pid": 1, "token": "local-test-token"}
            second = {"status": "running", "pid": 2, "token": "replacement-token"}
            _write_state(home, first)
            _write_state(home, second)
            self.assertEqual(_read_state(home), second)
            self.assertEqual(list(home.glob(".service.json.*.tmp")), [])

            with _startup_lock(home):
                self.assertTrue((home / "service.start.lock").is_file())
                with self.assertRaisesRegex(RuntimeError, "startup is already in progress"):
                    with _startup_lock(home):
                        pass
            self.assertFalse((home / "service.start.lock").exists())

    def test_negative_evidence_claim_is_not_misclassified_as_completion(self):
        findings = source_limitation_conflicts(
            "Verified facts: No telemetry or hosted activation is ready in the current evidence.",
            [("activation.md", "This is a local release gate, not hosted activation.")],
        )
        self.assertEqual(findings, [])

    def test_default_local_runtime_releases_idle_model_memory(self):
        with patch.dict(os.environ, {}, clear=True):
            service_args = parser().parse_args(["service", "start"])
        self.assertEqual(service_args.num_ctx, 4096)
        self.assertEqual(service_args.num_predict, 2048)
        self.assertEqual(service_args.keep_alive, "30s")

        model = OllamaModel("qwen3.5:0.8b")
        self.assertEqual(model.num_ctx, 4096)
        self.assertEqual(model.num_predict, 512)
        self.assertEqual(model.keep_alive, "30s")

    def test_routes_specialists_and_persists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, report = company.run("Create a profitable marketing launch for a local bakery")
            text = report.read_text(encoding="utf-8")
            self.assertIn(job_id, text)
            self.assertIn("finance", text)
            self.assertIn("marketing", text)
            self.assertEqual(company.jobs()[0][1], "complete")

    def test_recent_identical_direct_mission_reuses_output_without_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            first_job, first_report = company.run("Review  local inventory")
            calls_after_first = model.calls

            second_job, second_report = company.run(" Review local inventory ")

            self.assertEqual(second_job, first_job)
            self.assertEqual(second_report, first_report)
            self.assertEqual(model.calls, calls_after_first)
            detail = company.job_detail(first_job)
            self.assertEqual(sum(1 for event in detail["events"] if event[0] == "job_reused"), 1)

    def test_quality_failed_report_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            first_job, _ = company.run("Review  local inventory")
            second_job, _ = company.run(" Review local inventory ")
            self.assertNotEqual(second_job, first_job)

    def test_uncacheable_model_and_runtime_identity_change_disable_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uncacheable = UncacheableMockModel()
            company = Company(root / "uncacheable", uncacheable)
            first_job, _ = company.run("Review local inventory")
            second_job, _ = company.run("Review local inventory")
            self.assertNotEqual(second_job, first_job)

            company = Company(root / "versioned", VersionedMockModel("v1"))
            first_job, _ = company.run("Review local inventory")
            company.model = VersionedMockModel("v2")
            second_job, _ = company.run("Review local inventory")
            self.assertNotEqual(second_job, first_job)

    def test_report_integrity_tampering_fails_and_evaluations_are_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, report = company.run("Review local inventory")
            initial = company.job_detail(job_id)["evaluation"]
            self.assertTrue(initial["passed"])
            self.assertTrue(initial["checks"]["report_integrity_valid"])
            with closing(sqlite3.connect(company.db_path)) as db:
                sealed_hash = db.execute(
                    "SELECT report_sha256 FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(), sealed_hash)
            self.assertEqual(list(report.parent.glob(".*.tmp")), [])

            report.write_text(
                report.read_text(encoding="utf-8") + "\nApproved and deployed immediately.\n",
                encoding="utf-8",
            )
            rechecked = company.evaluate_job(job_id)

            self.assertFalse(rechecked["passed"])
            self.assertFalse(rechecked["checks"]["report_integrity_valid"])
            self.assertFalse(rechecked["checks"]["unperformed_action_claims_absent"])
            with closing(sqlite3.connect(company.db_path)) as db:
                history = list(db.execute(
                    "SELECT passed, evaluator_version, report_sha256 FROM evaluation_history "
                    "WHERE job_id=? ORDER BY id", (job_id,),
                ))
            self.assertEqual([row[0] for row in history], [1, 0])
            self.assertEqual(len({row[1] for row in history}), 1)
            self.assertNotEqual(history[0][2], history[1][2])
            replacement_job, _ = company.run("Review local inventory")
            self.assertNotEqual(replacement_job, job_id)

    def test_report_outside_output_storage_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingMockModel()
            company = Company(root / "state", model)
            job_id, report = company.run("Review local inventory")
            outside = root / "moved-report.md"
            outside.write_bytes(report.read_bytes())
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute("UPDATE jobs SET output_path=? WHERE id=?", (str(outside), job_id))

            replacement_job, _ = company.run("Review local inventory")

            self.assertNotEqual(replacement_job, job_id)
            events = company.job_detail(job_id)["events"]
            self.assertTrue(any(event[0] == "job_reuse_rejected" for event in events))

    def test_changed_retrieved_source_invalidates_recent_job_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inventory.md"
            source.write_text("inventory baseline is 10 units", encoding="utf-8")
            model = RecordingModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Inventory")
            company.add_knowledge(source, project=project_id)
            first_job, _ = company.run("Review inventory baseline", project=project_id)

            source.write_text("inventory baseline is 14 units", encoding="utf-8")
            company.add_knowledge(source, project=project_id)
            second_job, _ = company.run("Review inventory baseline", project=project_id)

            self.assertNotEqual(second_job, first_job)

    def test_evidence_manifest_freezes_exact_excerpt_and_detects_stale_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text(
                "Inventory baseline is 10 local units. Future demand is unknown.", encoding="utf-8"
            )
            company = Company(root / "state", EvidenceCitingModel())
            project_id = company.create_project("Evidence")
            company.add_knowledge(source, project=project_id)

            job_id, report = company.run(
                "Using imported notes about inventory, separate verified facts from assumptions.",
                project=project_id,
            )
            detail = company.job_detail(job_id)
            evaluation = detail["evaluation"]
            manifest = detail["evidence_manifest"]

            self.assertTrue(evaluation["passed"])
            self.assertTrue(evaluation["checks"]["evidence_manifest_valid"])
            self.assertTrue(evaluation["checks"]["verified_facts_evidence_cited"])
            self.assertTrue(evaluation["checks"]["verification_claims_evidence_bound"])
            evidence = manifest["evidence"][0]
            self.assertEqual(
                evidence["quote"], source.read_text(encoding="utf-8")[
                    evidence["char_start"]:evidence["char_end"]
                ],
            )
            self.assertIn(f"[EVIDENCE:{evidence['evidence_id']}]", report.read_text(encoding="utf-8"))
            basis = dict(manifest)
            recorded_digest = basis.pop("manifest_sha256")
            self.assertEqual(
                hashlib.sha256(company._canonical_json(basis).encode("utf-8")).hexdigest(),
                recorded_digest,
            )

            source.write_text("Inventory baseline changed after capture.", encoding="utf-8")
            rechecked = company.evaluate_job(job_id)
            self.assertFalse(rechecked["passed"])
            self.assertFalse(rechecked["checks"]["evidence_manifest_valid"])
            self.assertEqual(rechecked["manifest_reason"], "source_stale")

    def test_filename_only_verification_and_forged_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text("Inventory baseline is local and current.", encoding="utf-8")
            company = Company(root / "state", FilenameOnlyCitationModel())
            project_id = company.create_project("Evidence")
            company.add_knowledge(source, project=project_id)
            job_id, _ = company.run(
                "Using imported notes about inventory, separate verified facts from assumptions.",
                project=project_id,
            )
            evaluation = company.job_detail(job_id)["evaluation"]
            self.assertTrue(evaluation["checks"]["verified_facts_cited"])
            self.assertFalse(evaluation["checks"]["verified_facts_evidence_cited"])
            self.assertFalse(evaluation["checks"]["verification_claims_evidence_bound"])
            self.assertFalse(evaluation["checks"]["evidence_ids_valid"])

            with closing(sqlite3.connect(company.db_path)) as db, db:
                raw = db.execute(
                    "SELECT manifest_json FROM evidence_manifests WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                forged = json.loads(raw)
                forged["generator"] = "forged"
                db.execute(
                    "UPDATE evidence_manifests SET manifest_json=? WHERE job_id=?",
                    (json.dumps(forged, sort_keys=True), job_id),
                )
            rechecked = company.evaluate_job(job_id)
            self.assertFalse(rechecked["checks"]["evidence_manifest_valid"])
            self.assertEqual(rechecked["manifest_reason"], "digest_mismatch")

    def test_quality_rejects_completion_claim_that_conflicts_with_retrieved_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent_team_system.json"
            source.write_text(
                '{"gaps":["Telemetry is not fully wired. PostHog and Sentry should become '
                'standard before scaling client templates."]}',
                encoding="utf-8",
            )
            company = Company(root / "state", ContradictingSourceModel())
            project_id = company.create_project("Grounding")
            company.add_knowledge(source, project=project_id)

            job_id, _ = company.run(
                "Assess telemetry readiness for PostHog and Sentry", project=project_id
            )
            evaluation = company.evaluate_job(job_id)

            self.assertFalse(evaluation["passed"])
            self.assertFalse(evaluation["checks"]["source_limitations_respected"])
            self.assertIn("posthog", evaluation["source_conflicts"][0]["shared_terms"])
            quality_events = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "quality_evaluated"
            ]
            self.assertTrue(quality_events[-1]["source_conflicts"])

    def test_sensitive_action_fails_closed_and_creates_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaises(PermissionError):
                company.run("Send email to every prospect")
            requests = company.action_requests("pending")
            self.assertEqual(len(requests), 1)
            company.decide_action(requests[0][0], "rejected", "Not authorized")
            self.assertEqual(company.action_requests("rejected")[0][0], requests[0][0])

    def test_sensitive_action_normalization_blocks_common_bypass_wording(self):
        categories = Company.sensitive_categories(
            "Email every prospect and wire funds, then wipe the database"
        )
        self.assertEqual(categories, ["destructive", "external_communication", "money"])
        self.assertEqual(
            Company.sensitive_categories("Draft an email template for owner review"), []
        )
        self.assertEqual(
            Company.sensitive_categories("Prepare a deployment plan without executing it"), []
        )

    def test_knowledge_is_retrieved_and_cited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "shop.md"
            source.write_text("The bakery's signature product is tamarind sourdough. Marketing budget is 500.", encoding="utf-8")
            company = Company(root / "state", MockModel())
            _, changed = company.add_knowledge(source)
            self.assertTrue(changed)
            hits = company.search_knowledge("bakery marketing budget")
            self.assertEqual(hits[0].path, str(source.resolve()))
            _, report = company.run("Create a bakery marketing budget")
            self.assertIn(str(source.resolve()), report.read_text(encoding="utf-8"))

    def test_unknown_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaises(ValueError):
                company.run("Plan inventory", ["wizard"])

    def test_interrupted_job_is_recovered_on_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.initialize()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('deadjob', 'x', 'running', '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00')"
                )
            self.assertEqual(company.recover_stale_jobs(0), ["deadjob"])
            self.assertEqual(company.job_detail("deadjob")["job"][2], "interrupted")

    def test_stale_orphaned_queue_claim_fails_closed_once_without_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local queue health")
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            recovered = company.queue_items("failed")[0]
            self.assertEqual(recovered[0], queue_id)
            self.assertEqual(recovered[7], "")
            self.assertIn("no linked job", recovered[8])
            self.assertEqual(model.calls, 0)
            with closing(sqlite3.connect(company.db_path)) as db:
                event_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_claim_recovered'"
                ).fetchone()[0]
            self.assertEqual(event_count, 1)

            self.assertEqual(company.recover_stale_jobs(60), [])
            with closing(sqlite3.connect(company.db_path)) as db:
                repeated_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_claim_recovered'"
                ).fetchone()[0]
            self.assertEqual(repeated_count, 1)
            self.assertEqual(company.jobs(), [])

    def test_stale_linked_queue_and_job_are_recovered_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review interrupted work")
            queue_started_at = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('linkedjob', 'x', 'running', ?, ?)",
                    ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
                )
                db.execute(
                    "INSERT INTO assignments(job_id, role, brief, status) "
                    "VALUES ('linkedjob', 'operations', 'x', 'running')"
                )
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, job_id='linkedjob' "
                    "WHERE id=?",
                    (queue_started_at, queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), ["linkedjob"])
            self.assertEqual(company.job_detail("linkedjob")["job"][2], "interrupted")
            recovered = company.queue_items("failed")[0]
            self.assertEqual(recovered[7], "linkedjob")
            self.assertIn("interrupted job linkedjob", recovered[8])
            with closing(sqlite3.connect(company.db_path)) as db:
                assignment_status = db.execute(
                    "SELECT status FROM assignments WHERE job_id='linkedjob'"
                ).fetchone()[0]
            self.assertEqual(assignment_status, "failed")

    def test_stale_unlinked_queue_is_not_guessed_while_a_job_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review ambiguous work")
            company.initialize()
            observed_at = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('livejob', 'other work', 'running', ?, ?)",
                    (observed_at, observed_at),
                )
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            self.assertEqual(company.queue_items("running")[0][0], queue_id)

    def test_completed_job_queue_recovery_waits_for_fresh_finalization_then_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run("Review finalization recovery", roles=["operations"])
            queue_id = company.enqueue("Review finalization recovery", roles=["operations"])
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, job_id=?, run_token=? "
                    "WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job_id, "queue-lease", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            self.assertEqual(company.queue_items("running")[0][0], queue_id)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job_id),
                )
            self.assertEqual(company.recover_stale_jobs(60), [])
            reconciled = company.queue_items("complete")[0]
            self.assertEqual(reconciled[0], queue_id)
            self.assertEqual(reconciled[7], job_id)
            self.assertEqual(reconciled[8], "")
            with closing(sqlite3.connect(company.db_path)) as db:
                event = db.execute(
                    "SELECT detail FROM events WHERE kind='queue_claim_recovered' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            self.assertEqual(json.loads(event)["resulting_status"], "complete")

    def test_queue_job_is_linked_before_the_first_model_call_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local queue health", roles=["operations"])
            outcome = {}

            def run_queue():
                try:
                    outcome["result"] = company.run_next_queue_item()
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_queue)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))
            with closing(sqlite3.connect(company.db_path)) as db:
                queue_status, job_id, queue_token = db.execute(
                    "SELECT status, job_id, run_token FROM mission_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
                job_status, job_token = db.execute(
                    "SELECT status, run_token FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
            self.assertEqual(queue_status, "running")
            self.assertRegex(job_id, r"^[0-9a-f]{12}$")
            self.assertEqual(job_status, "running")
            self.assertRegex(queue_token, r"^[0-9a-f]{32}$")
            self.assertEqual(job_token, queue_token)

            model.release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertEqual(outcome["result"][0], queue_id)
            self.assertEqual(outcome["result"][1], job_id)

    def test_recovery_revokes_worker_lease_and_discards_its_late_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review revocable work", roles=["operations"])
            outcome = {}

            def run_queue():
                try:
                    outcome["result"] = company.run_next_queue_item()
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_queue)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))
            with closing(sqlite3.connect(company.db_path)) as db:
                job_id = db.execute(
                    "SELECT job_id FROM mission_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            model.release.set()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertNotIn("result", outcome)
            self.assertRegex(str(outcome["error"]), "recovered or superseded")
            self.assertEqual(company.job_detail(job_id)["job"][2], "interrupted")
            recovered_queue = company.queue_items("failed")[0]
            self.assertEqual(recovered_queue[0], queue_id)
            self.assertEqual(recovered_queue[7], job_id)
            self.assertFalse((company.output_dir / f"{job_id}.md").exists())
            with closing(sqlite3.connect(company.db_path)) as db:
                job_token = db.execute(
                    "SELECT run_token FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
                queue_token = db.execute(
                    "SELECT run_token FROM mission_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
                discarded = db.execute(
                    "SELECT COUNT(*) FROM events "
                    "WHERE job_id=? AND kind='late_result_discarded'",
                    (job_id,),
                ).fetchone()[0]
            self.assertIsNone(job_token)
            self.assertIsNone(queue_token)
            self.assertEqual(discarded, 1)

    def test_queue_reuse_is_linked_without_repeating_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            job_id, report = company.run(
                "Review local inventory", roles=["operations", "quality"],
            )
            calls_after_direct_run = model.calls
            queue_id = company.enqueue(
                "Review local inventory", roles=["operations", "quality"],
            )

            observed_queue, observed_job, observed_report, passed = (
                company.run_next_queue_item()
            )

            self.assertEqual(observed_queue, queue_id)
            self.assertEqual(observed_job, job_id)
            self.assertEqual(observed_report, report)
            self.assertTrue(passed)
            self.assertEqual(model.calls, calls_after_direct_run)
            self.assertEqual(company.queue_items("complete")[0][7], job_id)
            linked_events = [
                json.loads(detail)
                for kind, detail, _ in company.job_detail(job_id)["events"]
                if kind == "queue_job_linked"
            ]
            self.assertEqual(linked_events[-1], {"queue_id": queue_id, "reused": True})

    def test_ollama_metrics_are_isolated_per_worker_thread(self):
        model = OllamaModel("qwen3.5:0.8b")
        observed = {}
        ready = threading.Barrier(2)

        def record(name, output_tokens):
            model.last_metrics = {"output_tokens": output_tokens}
            ready.wait(timeout=3)
            observed[name] = model.last_metrics["output_tokens"]

        first = threading.Thread(target=record, args=("first", 11))
        second = threading.Thread(target=record, args=("second", 22))
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertEqual(observed, {"first": 11, "second": 22})
        self.assertEqual(model.last_metrics, {})

    def test_project_directory_ingestion_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "sources"
            sources.mkdir()
            (sources / "pricing.md").write_text("Project Atlas price target is 42 units.", encoding="utf-8")
            nested = sources / "nested"
            nested.mkdir()
            (nested / "private.md").write_text("Nested secret phrase.", encoding="utf-8")
            company = Company(root / "state", MockModel())
            atlas = company.create_project("Atlas", "Pricing work")
            company.create_project("Beacon")
            changed, unchanged, _ = company.add_knowledge_dir(sources, "Atlas")
            self.assertEqual((changed, unchanged), (1, 0))
            self.assertTrue(company.search_knowledge("price target", project=atlas))
            self.assertFalse(company.search_knowledge("price target", project="Beacon"))
            self.assertFalse(company.search_knowledge("secret phrase", project="Atlas"))
            _, report = company.run("Analyze the Atlas price target", project="Atlas")
            text = report.read_text(encoding="utf-8")
            self.assertIn("Project: Atlas", text)
            self.assertIn("pricing.md", text)

    def test_executive_synthesis_receives_team_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingModel()
            company = Company(Path(tmp), model)
            _, report = company.run("Plan inventory", roles=["operations", "quality"])
            self.assertEqual(len(model.prompts), 3)
            self.assertIn("OPERATIONS", model.prompts[-1][1])
            self.assertIn("QUALITY", model.prompts[-1][1])
            self.assertIn("Executive synthesis", report.read_text(encoding="utf-8"))

    def test_resume_keeps_completed_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FailOnceModel()
            company = Company(Path(tmp), model)
            with self.assertRaisesRegex(RuntimeError, "simulated model interruption"):
                company.run("Plan inventory", roles=["operations", "quality"])
            job_id = company.jobs()[0][0]
            self.assertEqual(company.jobs()[0][1], "failed")
            resumed_id, report = company.resume(job_id)
            self.assertEqual(resumed_id, job_id)
            self.assertEqual(len(model.prompts), 4)
            self.assertTrue(report.exists())
            detail = company.job_detail(job_id)
            self.assertEqual(detail["job"][2], "complete")
            self.assertEqual([row[2] for row in detail["assignments"]], ["complete", "complete"])

    def test_second_concurrent_mission_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.initialize()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('activejob', 'existing work', 'running', 'now', 'now')"
                )
            with self.assertRaisesRegex(RuntimeError, "already running"):
                company.run("Start another mission")

    def test_dashboard_is_read_only_snapshot_and_escapes_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.create_project("<script>alert(1)</script>")
            company.request_action("Review <unsafe> text")
            page = render_dashboard(company)
            self.assertIn("Local Agent Company", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
            self.assertIn("Review &lt;unsafe&gt; text", page)
            self.assertNotIn("<script>alert(1)</script>", page)

    def test_dashboard_http_is_local_and_rejects_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            server = create_dashboard_server(company, 0)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/health.json", timeout=3) as response:
                    self.assertIn(b'"status": "ready"', response.read())
                request = urllib.request.Request(base + "/", data=b"", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(request, timeout=3)
                self.assertEqual(raised.exception.code, 405)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_rejects_rebound_host_and_cross_site_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                rebound = urllib.request.Request(base + "/", headers={"Host": "attacker.example"})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(rebound, timeout=3)
                self.assertEqual(rejected.exception.code, 421)
                rejected.exception.close()

                cross_site = urllib.request.Request(
                    base + "/queue/enqueue",
                    data=urllib.parse.urlencode({
                        "service_token": "test-secret", "objective": "Review operations",
                    }).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "http://attacker.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(cross_site, timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertEqual(company.queue_items(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_preserves_drafts_and_opens_escaped_mission_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            job_id, report = company.run("Review local inventory")
            report.write_text(report.read_text(encoding="utf-8") + "\n<script>alert(1)</script>", encoding="utf-8")
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/", timeout=3) as response:
                    page = response.read().decode("utf-8")
                self.assertNotIn("http-equiv=\"refresh\"", page)
                self.assertIn(f'/missions/{job_id}', page)
                self.assertIn("not factual or production verification", page)

                with opener.open(base + f"/missions/{job_id}", timeout=3) as response:
                    detail = response.read().decode("utf-8")
                self.assertIn("Automated checks FAILED", detail)
                self.assertIn("model stopped cleanly", detail)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", detail)
                self.assertNotIn("<script>alert(1)</script>", detail)

                with self.assertRaises(urllib.error.HTTPError) as missing:
                    opener.open(base + "/missions/deadbeef0000", timeout=3)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_accepts_only_authenticated_service_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                bad = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "wrong"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(bad, timeout=3)
                self.assertEqual(rejected.exception.code, 405)
                rejected.exception.close()
                good = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "test-secret"},
                )
                with opener.open(good, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                server.server_close()

    def test_dashboard_authenticated_queue_intake_and_cancel_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("SuperMega")
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path, fields):
                return urllib.request.Request(
                    base + path,
                    data=urllib.parse.urlencode(fields).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(base + "/", timeout=3) as response:
                    page = response.read()
                    self.assertIn(b"Queue a SuperMega task", page)
                    self.assertIn(b"test-secret", page)
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                with opener.open(base + "/health.json", timeout=3) as response:
                    self.assertNotIn(b"test-secret", response.read())

                bad = post(
                    "/queue/enqueue",
                    {"service_token": "wrong", "objective": "Review operations", "priority": "60"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(bad, timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertEqual(company.queue_items(), [])

                enqueue = post(
                    "/queue/enqueue",
                    {
                        "service_token": "test-secret",
                        "objective": "Review local SuperMega operations",
                        "project": project_id,
                        "playbook": "operations-improvement",
                        "priority": "75",
                    },
                )
                with opener.open(enqueue, timeout=3) as response:
                    self.assertIn(b"nothing was executed", response.read())
                queued = company.queue_items("queued")
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0][2], 75)
                self.assertEqual(queued[0][4], "SuperMega")
                self.assertEqual(len(company.jobs()), 0)

                cancel = post(
                    "/queue/cancel",
                    {"service_token": "test-secret", "queue_id": queued[0][0]},
                )
                with opener.open(cancel, timeout=3) as response:
                    self.assertIn(b"Cancelled queued mission", response.read())
                self.assertEqual(company.queue_items("queued"), [])
                self.assertEqual(company.queue_items("cancelled")[0][0], queued[0][0])
                with closing(sqlite3.connect(company.db_path)) as db:
                    events = list(db.execute(
                        "SELECT kind, detail FROM events WHERE kind LIKE 'queue_%' ORDER BY id"
                    ))
                self.assertEqual([row[0] for row in events], ["queue_enqueued", "queue_cancelled"])
                self.assertTrue(all('"source": "dashboard"' in row[1] for row in events))

                too_large = urllib.request.Request(
                    base + "/queue/enqueue", data=b"x" * 17000, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(too_large, timeout=3)
                self.assertEqual(rejected.exception.code, 413)
                rejected.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_run_next_is_explicit_single_worker_and_refuses_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            company.enqueue("Review local inventory", priority=80)
            server = create_dashboard_server(company, 0, service_token="worker-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def run_request():
                return urllib.request.Request(
                    base + "/queue/run-next",
                    data=urllib.parse.urlencode({"service_token": "worker-secret"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(run_request(), timeout=3) as response:
                    self.assertIn(b"Started one local queued mission", response.read())
                self.assertTrue(model.started.wait(timeout=3))

                with self.assertRaises(urllib.error.HTTPError) as duplicate:
                    opener.open(run_request(), timeout=3)
                self.assertEqual(duplicate.exception.code, 409)
                duplicate.exception.close()

                stop = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "worker-secret"},
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    opener.open(stop, timeout=3)
                self.assertEqual(refused.exception.code, 409)
                refused.exception.close()

                model.release.set()
                deadline = time.monotonic() + 5
                status = "running"
                while status == "running" and time.monotonic() < deadline:
                    with opener.open(base + "/health.json", timeout=3) as response:
                        status = json.load(response)["worker"]["status"]
                    if status == "running":
                        time.sleep(0.05)
                self.assertEqual(status, "complete")
                self.assertEqual(len(company.queue_items("complete")), 1)
                self.assertEqual(len(company.jobs()), 1)
            finally:
                model.release.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_worker_preserves_sensitive_action_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.enqueue("Send email to every prospect", priority=90)
            server = create_dashboard_server(company, 0, service_token="gate-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            request = urllib.request.Request(
                base + "/queue/run-next",
                data=urllib.parse.urlencode({"service_token": "gate-secret"}).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with opener.open(request, timeout=3) as response:
                    response.read()
                deadline = time.monotonic() + 3
                status = "running"
                while status == "running" and time.monotonic() < deadline:
                    with opener.open(base + "/health.json", timeout=3) as response:
                        status = json.load(response)["worker"]["status"]
                    if status == "running":
                        time.sleep(0.05)
                self.assertEqual(status, "needs_approval")
                self.assertEqual(len(company.queue_items("needs_approval")), 1)
                self.assertEqual(len(company.action_requests("pending")), 1)
                self.assertEqual(company.jobs(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_queue_objective_length_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaisesRegex(ValueError, "cannot exceed 4000"):
                company.enqueue("x" * 4001)

    def test_queue_runs_highest_priority_due_playbook_and_passes_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("Queue Lab")
            low_id = company.enqueue("Low priority work", project_id, priority=10)
            future_id = company.enqueue(
                "Future urgent work", project_id, priority=100,
                scheduled_at="2999-01-01T00:00:00+00:00",
            )
            high_id = company.enqueue(
                "Improve daily operations", project_id, playbook="operations-improvement", priority=80
            )
            queue_id, job_id, report, passed = company.run_next_queue_item()
            self.assertEqual(queue_id, high_id)
            self.assertTrue(passed)
            self.assertTrue(report.exists())
            self.assertEqual({row[0] for row in company.queue_items("queued")}, {low_id, future_id})
            self.assertEqual(company.queue_items("complete")[0][7], job_id)
            self.assertEqual(company.evaluate_job(job_id)["score"], 100)
            detail = company.job_detail(job_id)
            expected_roles = PLAYBOOKS["operations-improvement"]["roles"]
            self.assertEqual([row[1] for row in detail["assignments"]], expected_roles)

    def test_sensitive_queue_item_needs_approval_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Send email to every prospect", priority=90)
            with self.assertRaises(PermissionError):
                company.run_next_queue_item()
            self.assertEqual(company.queue_items("needs_approval")[0][0], queue_id)
            self.assertEqual(len(company.jobs()), 0)
            self.assertEqual(len(company.action_requests("pending")), 1)

    def test_due_schedule_materializes_once_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            schedule_id = company.create_schedule(
                "Daily check", "Review local health", 1, "2020-01-01T00:00:00+00:00",
                playbook="operations-improvement", priority=70,
            )
            observed = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
            created = company.materialize_due_schedules(observed)
            self.assertEqual(created[0][0], schedule_id)
            self.assertEqual(len(company.queue_items("queued")), 1)
            self.assertEqual(company.materialize_due_schedules(observed), [])
            next_run = datetime.fromisoformat(company.schedules()[0][4])
            self.assertGreater(next_run, observed)
            company.set_schedule_enabled(schedule_id, False)
            self.assertEqual(company.schedules()[0][2], 0)

    def test_audit_export_has_matching_hash_and_excludes_source_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text("private local reference body", encoding="utf-8")
            company = Company(root / "state", MockModel())
            company.add_knowledge(source)
            company.run("Plan inventory", roles=["operations", "quality"])
            company.enqueue("Queued audit record")
            audit_path, hash_path, digest = company.export_audit(root / "exports")
            self.assertEqual(hashlib.sha256(audit_path.read_bytes()).hexdigest(), digest)
            self.assertIn(digest, hash_path.read_text(encoding="ascii"))
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "local-agent-company-audit-v3")
            self.assertRegex(payload["jobs"][0]["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(payload["evaluation_history"][0]["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(payload["evaluation_history"][0]["evaluator_version"])
            self.assertRegex(payload["evaluation_history"][0]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(payload["evidence_manifests"][0]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("run_token", payload["jobs"][0])
            self.assertNotIn("run_token", payload["queue"][0])
            self.assertNotIn("content", payload["knowledge_index"][0])
            self.assertNotIn("private local reference body", audit_path.read_text(encoding="utf-8"))

    def test_health_snapshot_reports_local_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            health = company.health_snapshot()
            self.assertGreater(health["disk_free_bytes"], 0)
            self.assertIn("database_bytes", health)
            self.assertEqual(health["active_jobs"], 0)

    def test_csv_dataset_profile_is_read_only_and_generates_project_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sales.csv"
            source.write_text(
                "id,value,note\n1,10,a\n2,11,\n3,oops,b\n3,oops,b\n", encoding="utf-8"
            )
            original = source.read_bytes()
            company = Company(root / "state", MockModel())
            company.create_project("Data Lab")
            dataset_id, brief, profile = company.profile_dataset(source, "Data Lab")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(profile["profiled_rows"], 4)
            self.assertEqual(profile["quality_flags"]["duplicate_rows"], 1)
            self.assertIn("value", profile["quality_flags"]["mixed_type_columns"])
            self.assertEqual(profile["columns"]["note"]["missing"], 1)
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Mixed-type columns: value", brief_text)
            self.assertNotIn("oops", brief_text)
            self.assertEqual(company.dataset_items("Data Lab")[0][0], dataset_id)
            self.assertTrue(company.search_knowledge("duplicate mixed type", project="Data Lab"))

    def test_json_dataset_requires_objects_and_profiles_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "records.json"
            source.write_text('[{"active": true, "count": 1}, {"active": false, "count": 2}]', encoding="utf-8")
            company = Company(root / "state", MockModel())
            company.create_project("JSON Lab")
            dataset_id, _, profile = company.profile_dataset(source, "JSON Lab")
            self.assertEqual(profile["columns"]["active"]["types"], {"boolean": 2})
            self.assertEqual(company.dataset_detail(dataset_id)["project"], "JSON Lab")

    def test_quality_rejects_model_length_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            job_id, _ = company.run("Plan inventory", roles=["operations", "quality"])
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["passed"])
            self.assertFalse(evaluation["checks"]["model_stopped_cleanly"])

    def test_quality_enforces_explicit_objective_constraints_and_claim_safety(self):
        objective = (
            "Define three task templates, a daily review cadence, success checks, failure modes, "
            "and owner gates. Separate verified facts from assumptions. Each specialist must use "
            "at most 100 words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(False))
            queue_id = company.enqueue(objective, playbook="operations-improvement")
            observed_queue, job_id, _, passed = company.run_next_queue_item()
            self.assertEqual(observed_queue, queue_id)
            self.assertFalse(passed)
            evaluation = company.evaluate_job(job_id)
            self.assertTrue(evaluation["checks"]["specialists_within_word_limit"])
            self.assertFalse(evaluation["checks"]["facts_assumptions_separated"])
            self.assertFalse(evaluation["checks"]["requested_concepts_present"])
            self.assertFalse(evaluation["checks"]["unperformed_action_claims_absent"])
            self.assertFalse(evaluation["checks"]["numeric_claims_labeled"])
            self.assertFalse(evaluation["checks"]["placeholder_artifacts_absent"])
            self.assertEqual(company.queue_items("quality_failed")[0][0], queue_id)
            detail = company.job_detail(job_id)
            self.assertTrue(any(
                event[0] == "objective_constraint_applied" and "word limit" in event[1]
                for event in detail["events"]
            ))

        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(True))
            queue_id = company.enqueue(objective, playbook="operations-improvement")
            _, _, _, passed = company.run_next_queue_item()
            self.assertTrue(passed)
            evaluation = company.recent_evaluations()[0]
            self.assertEqual(evaluation[0], company.queue_items("complete")[0][7])
            self.assertEqual(evaluation[1:3], (1, 100))

    def test_bounded_editor_repairs_structure_and_audits_word_caps(self):
        objective = (
            "Define task templates, a daily review cadence, success checks, failure modes, and owner gates. "
            "Separate verified facts from assumptions. Each specialist must use at most 100 words. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), RevisionModel())
            queue_id = company.enqueue(objective, roles=["operations", "quality"])
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertTrue(passed)
            detail = company.job_detail(job_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                results = [row[0] for row in db.execute(
                    "SELECT result FROM assignments WHERE job_id=? ORDER BY sequence", (job_id,),
                )]
            self.assertTrue(all(
                len(re.findall(r"\b[\w'-]+\b", result)) <= 100 for result in results
            ))
            self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", detail["job"][7])), 180)
            event_kinds = [event[0] for event in detail["events"]]
            self.assertIn("synthesis_revision_started", event_kinds)
            self.assertIn("objective_constraint_applied", event_kinds)
            self.assertEqual(company.queue_items("complete")[0][0], queue_id)

    def test_imported_verified_facts_require_exact_source_filename(self):
        objective = (
            "Using imported evidence, define task templates, a daily review cadence, success checks, "
            "failure modes, and owner gates. Separate verified facts from assumptions. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        for cited, expected in ((False, False), (True, True)):
            with self.subTest(cited=cited), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "notes.md"
                source.write_text(
                    "Imported adoption task templates daily review cadence success checks failure modes owner gates.",
                    encoding="utf-8",
                )
                company = Company(root / "state", CitationModel(cited))
                project_id = company.create_project("Evidence")
                company.add_knowledge(source, project_id)
                job_id, _ = company.run(objective, roles=["operations", "quality"], project=project_id)
                evaluation = company.evaluate_job(job_id)
                self.assertEqual(evaluation["checks"]["verified_facts_cited"], expected)
                self.assertEqual(evaluation["passed"], expected)

    def test_dashboard_can_recheck_completed_job_quality(self):
        objective = (
            "Define task templates, a daily review cadence, success checks, failure modes, and owner gates. "
            "Separate verified facts from assumptions. Each specialist must use at most 100 words. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(False))
            queue_id = company.enqueue(objective, roles=["operations", "quality"])
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertFalse(passed)
            server = create_dashboard_server(company, 0, service_token="quality-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            request = urllib.request.Request(
                base + "/jobs/quality",
                data=urllib.parse.urlencode({
                    "service_token": "quality-secret", "job_id": job_id,
                }).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with opener.open(request, timeout=3) as response:
                    body = response.read()
                self.assertIn(b"quality failed", body)
                self.assertEqual(company.recent_evaluations()[0][1], 0)
                with closing(sqlite3.connect(company.db_path)) as db:
                    detail = db.execute(
                        "SELECT detail FROM events WHERE job_id=? AND kind='quality_evaluated' "
                        "ORDER BY id DESC LIMIT 1", (job_id,),
                    ).fetchone()[0]
                self.assertFalse(json.loads(detail)["passed"])

                reset = urllib.request.Request(
                    base + "/queue/reset",
                    data=urllib.parse.urlencode({
                        "service_token": "quality-secret", "queue_id": queue_id,
                    }).encode(),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with opener.open(reset, timeout=3) as response:
                    self.assertIn(b"queued again", response.read())
                self.assertEqual(company.queue_items("queued")[0][0], queue_id)
                with closing(sqlite3.connect(company.db_path)) as db:
                    reset_detail = db.execute(
                        "SELECT detail FROM events WHERE kind='queue_reset' ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                self.assertEqual(json.loads(reset_detail)["source"], "dashboard")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_required_objective_ending_is_applied_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run(
                "Plan inventory and end with: OWNER REVIEW REQUIRED",
                roles=["operations", "quality"],
            )
            evaluation = company.evaluate_job(job_id)
            self.assertTrue(evaluation["passed"])
            self.assertTrue(evaluation["checks"]["required_ending_present"])
            detail = company.job_detail(job_id)
            self.assertTrue(detail["job"][7].endswith("OWNER REVIEW REQUIRED"))
            self.assertTrue(any(event[0] == "objective_constraint_applied" for event in detail["events"]))


if __name__ == "__main__":
    unittest.main()
