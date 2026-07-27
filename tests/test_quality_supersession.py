import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_company.cli import main as cli_main
from local_company.core import (
    Company, EVALUATOR_VERSION, MockModel, QUALITY_SUPERSESSION_LIST_SCHEMA,
    QUALITY_SUPERSESSION_PREVIEW_SCHEMA, QUEUE_SUPERSEDE_SCHEMA,
)
from local_company.dashboard import (
    create_dashboard_server, render_dashboard, render_quality_failure_overview,
    render_quality_supersession_overview, render_quality_supersession_preview,
)


class CountingMockModel(MockModel):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(system, prompt)


class QualitySupersessionTests(unittest.TestCase):
    @staticmethod
    def _seed_historical_failure(
        root: Path,
    ) -> tuple[Company, str, str, str, Path]:
        company = Company(root / "state", MockModel())
        objective = "Private supersession objective must never appear in proof output"
        queue_id = company.enqueue(objective, roles=["quality"], priority=92)
        observed_queue, job_id, report_path, passed = company.run_next_queue_item()
        if observed_queue != queue_id or not passed:
            raise AssertionError("Mock mission must begin with a passing sealed report")

        with closing(sqlite3.connect(company.db_path)) as db, db:
            checks = json.loads(db.execute(
                "SELECT checks_json FROM evaluations WHERE job_id=?", (job_id,),
            ).fetchone()[0])
            gate = "placeholder_artifacts_absent"
            if checks.get(gate) is not True:
                raise AssertionError("Selected historical gate must initially pass")
            checks[gate] = False
            score = round(sum(checks.values()) * 100 / len(checks))
            encoded = json.dumps(checks, sort_keys=True)
            db.execute(
                "UPDATE evaluations SET passed=0, score=?, checks_json=? WHERE job_id=?",
                (score, encoded, job_id),
            )
            db.execute(
                "UPDATE evaluation_history SET passed=0, score=?, checks_json=?, "
                "evaluator_version='local-quality-2026-07-01.1' WHERE id=("
                "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                (score, encoded, job_id),
            )
            db.execute(
                "UPDATE mission_queue SET status='quality_failed' WHERE id=?",
                (queue_id,),
            )
        company.model = CountingMockModel()
        return company, queue_id, job_id, objective, report_path

    @classmethod
    def _seed_eligible_failure(
        cls, root: Path,
    ) -> tuple[Company, str, str, str, str, Path, Path]:
        company, queue_id, job_id, objective, failed_report = (
            cls._seed_historical_failure(root)
        )
        company.model = MockModel()
        successor_job_id, successor_report = company.retry(job_id)
        with closing(sqlite3.connect(company.db_path)) as db:
            evaluation = db.execute(
                "SELECT passed, score FROM evaluations WHERE job_id=?",
                (successor_job_id,),
            ).fetchone()
        if evaluation != (1, 100):
            raise AssertionError("Exact retry must pass the current evaluator")
        company.model = CountingMockModel()
        return (
            company, queue_id, job_id, successor_job_id, objective,
            failed_report, successor_report,
        )

    def test_preview_is_stable_pathless_read_only_and_available_from_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                company, queue_id, job_id, successor_job_id, objective,
                failed_report, successor_report,
            ) = self._seed_eligible_failure(root)
            database_before = company.db_path.read_bytes()
            failed_report_before = failed_report.read_bytes()
            successor_report_before = successor_report.read_bytes()

            preview = company.quality_supersession_preview(queue_id)

            self.assertEqual(preview["schema"], QUALITY_SUPERSESSION_PREVIEW_SCHEMA)
            self.assertEqual(preview["queue_id"], queue_id)
            self.assertEqual(preview["failed_job_id"], job_id)
            self.assertEqual(preview["queue_status"], "quality_failed")
            self.assertEqual(preview["eligibility"], "eligible")
            self.assertEqual(preview["candidate_count"], 1)
            self.assertEqual(preview["checked_candidate_count"], 1)
            self.assertEqual(preview["successor"]["job_id"], successor_job_id)
            self.assertEqual(preview["successor"]["chain_depth"], 1)
            self.assertEqual(preview["successor"]["score"], 100)
            self.assertEqual(
                preview["successor"]["evaluator_version"], EVALUATOR_VERSION,
            )
            self.assertRegex(preview["proof_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(preview["blockers"], [])
            self.assertEqual(preview["next_action"], "supersede_with_successor_proof")
            self.assertTrue(all(value is False for value in preview["effects"].values()))
            self.assertEqual(company.model.calls, 0)

            rendered = json.dumps(preview, sort_keys=True)
            self.assertNotIn(objective, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("output_path", rendered)
            self.assertLess(len(rendered.encode("utf-8")), 4_096)

            output = io.StringIO()
            cli_model = CountingMockModel()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "queue",
                    "supersession-preview", queue_id,
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            cli_preview = json.loads(output.getvalue())
            self.assertEqual(cli_preview["proof_sha256"], preview["proof_sha256"])
            self.assertEqual(cli_model.calls, 0)

            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(failed_report.read_bytes(), failed_report_before)
            self.assertEqual(successor_report.read_bytes(), successor_report_before)

    def test_supersede_requires_and_audits_the_matching_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                company, queue_id, job_id, successor_job_id, objective,
                failed_report, _,
            ) = self._seed_eligible_failure(root)
            company.model = MockModel()
            unrelated_job_id, _ = company.run(objective, roles=["operations"])
            company.model = CountingMockModel()
            report_before = failed_report.read_bytes()
            with closing(sqlite3.connect(company.db_path)) as db:
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]

            database_before = company.db_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "matching current-passing"):
                company.supersede_quality_failure(
                    queue_id,
                    "An unrelated passing mission must never hide this failure.",
                    unrelated_job_id,
                )
            self.assertEqual(company.db_path.read_bytes(), database_before)

            error = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "queue",
                    "supersede", queue_id, "--reason",
                    "Missing proof must fail closed without changing local state.",
                ],
            ), patch("sys.stderr", error):
                with self.assertRaises(SystemExit) as missing:
                    cli_main()
            self.assertEqual(missing.exception.code, 2)
            self.assertIn("--successor-job", error.getvalue())

            reason = (
                "An exact current-passing retry now replaces this historical failure "
                "while preserving its audit evidence."
            )
            output = io.StringIO()
            cli_model = CountingMockModel()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "queue",
                    "supersede", queue_id, "--successor-job", successor_job_id,
                    "--reason", reason,
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["schema"], QUEUE_SUPERSEDE_SCHEMA)
            self.assertEqual(result["successor_job_id"], successor_job_id)
            self.assertRegex(result["proof_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                result["successor_input_fingerprint_sha256"], r"^[0-9a-f]{64}$",
            )
            self.assertEqual(result["status"], "superseded")
            self.assertEqual(cli_model.calls, 0)
            self.assertEqual(company.model.calls, 0)

            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                event = json.loads(db.execute(
                    "SELECT detail FROM events WHERE job_id=? "
                    "AND kind='queue_quality_failure_superseded'", (job_id,),
                ).fetchone()[0])
            self.assertEqual(history_after, history_before)
            self.assertEqual(failed_report.read_bytes(), report_before)
            self.assertEqual(event["successor_job_id"], successor_job_id)
            self.assertEqual(event["proof_sha256"], result["proof_sha256"])
            self.assertEqual(
                event["successor_input_fingerprint_sha256"],
                result["successor_input_fingerprint_sha256"],
            )
            self.assertEqual(event["proof_schema"], QUALITY_SUPERSESSION_PREVIEW_SCHEMA)
            after = company.quality_supersession_preview(queue_id)
            self.assertEqual(after["eligibility"], "already_superseded")
            self.assertEqual(after["next_action"], "none")

    def test_supersede_rolls_back_if_successor_files_cross_during_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (
                company, queue_id, job_id, successor_job_id, _, _, successor_report,
            ) = self._seed_eligible_failure(Path(tmp))
            database_before = company.db_path.read_bytes()
            report_before = successor_report.read_bytes()
            original_validate = company._validate_evidence_manifest

            def mutate_after_validation(
                observed_job_id: str, manifest_sha256: str | None,
            ) -> tuple[bool, dict[str, object] | None, str]:
                result = original_validate(observed_job_id, manifest_sha256)
                successor_report.write_bytes(
                    report_before + b"\nConcurrent local report change.\n"
                )
                return result

            with patch.object(
                company, "_validate_evidence_manifest",
                side_effect=mutate_after_validation,
            ), self.assertRaisesRegex(RuntimeError, "files changed during supersession"):
                company.supersede_quality_failure(
                    queue_id,
                    "A crossing report write must abort the queue lifecycle mutation.",
                    successor_job_id,
                )

            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.queue_items("quality_failed")[0][0], queue_id)
            self.assertNotEqual(successor_report.read_bytes(), report_before)
            with closing(sqlite3.connect(company.db_path)) as db:
                event_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? "
                    "AND kind='queue_quality_failure_superseded'", (job_id,),
                ).fetchone()[0]
            self.assertEqual(event_count, 0)
            self.assertEqual(company.model.calls, 0)

    def test_unresolved_and_racing_failures_remain_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company, queue_id, _, _, _ = self._seed_historical_failure(Path(tmp))
            database_before = company.db_path.read_bytes()

            preview = company.quality_supersession_preview(queue_id)
            self.assertEqual(preview["eligibility"], "ineligible")
            self.assertIsNone(preview["successor"])
            self.assertIsNone(preview["proof_sha256"])
            self.assertIn("no_exact_retry_descendant", preview["blockers"])
            with self.assertRaisesRegex(ValueError, "matching current-passing"):
                company.supersede_quality_failure(
                    queue_id,
                    "No successor exists, so this failure must remain visible.",
                    "a" * 12,
                )
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.queue_items("quality_failed")[0][0], queue_id)

            snapshot = company._quality_supersession_snapshot(queue_id)
            changed = dict(snapshot)
            changed["database_sha256"] = "0" * 64
            with patch.object(
                company, "_quality_supersession_snapshot",
                side_effect=[snapshot, changed],
            ), self.assertRaisesRegex(RuntimeError, "changed during observation"):
                company.quality_supersession_preview(queue_id)
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.model.calls, 0)

    def test_dashboard_proof_is_exact_pathless_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                company, queue_id, job_id, successor_job_id, objective, _, _,
            ) = self._seed_eligible_failure(root)
            database_before = company.db_path.read_bytes()

            page = render_quality_supersession_preview(company, queue_id)
            self.assertIn("Quality supersession proof", page)
            self.assertIn(QUALITY_SUPERSESSION_PREVIEW_SCHEMA, page)
            self.assertIn(queue_id, page)
            self.assertIn(job_id, page)
            self.assertIn(successor_job_id, page)
            self.assertIn("Read-only local proof", page)
            self.assertNotIn(objective, page)
            self.assertNotIn(str(root), page)
            self.assertLess(len(page.encode("utf-8")), 20_480)
            failure_page = render_quality_failure_overview(company)
            self.assertIn(f'href="/quality-supersession/{queue_id}"', failure_page)

            malformed = company.quality_supersession_preview(queue_id)
            malformed["effects"] = {"model_called": True}
            with patch.object(
                company, "quality_supersession_preview", return_value=malformed,
            ), self.assertRaisesRegex(ValueError, "preview is malformed"):
                render_quality_supersession_preview(company, queue_id)

            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(
                    base + f"/quality-supersession/{queue_id}", timeout=3,
                ) as response:
                    http_page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(
                        response.headers["Content-Type"], "text/html; charset=utf-8",
                    )
                self.assertEqual(http_page, page)

                for path in (
                    "/quality-supersession/aaaaaaaaaaaa",
                    f"/quality-supersession/{queue_id}?extra=1",
                    f"/quality-supersession/{queue_id}/extra",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        opener.open(base + path, timeout=3)
                    self.assertEqual(missing.exception.code, 404)
                    missing.exception.close()

                with patch.object(
                    company, "quality_supersession_preview",
                    side_effect=RuntimeError("private-race-detail"),
                ), self.assertRaises(urllib.error.HTTPError) as unstable:
                    opener.open(
                        base + f"/quality-supersession/{queue_id}", timeout=3,
                    )
                self.assertEqual(unstable.exception.code, 409)
                unstable_body = unstable.exception.read().decode("utf-8")
                unstable.exception.close()
                self.assertIn("retry after local state is stable", unstable_body)
                self.assertNotIn("private-race-detail", unstable_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.model.calls, 0)

    def test_retired_review_list_is_stable_pathless_and_available_from_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                company, queue_id, job_id, successor_job_id, objective,
                failed_report, successor_report,
            ) = self._seed_eligible_failure(root)
            retirement = company.supersede_quality_failure(
                queue_id,
                "The exact current retry replaces this result while audit history remains.",
                successor_job_id,
            )
            database_before = company.db_path.read_bytes()
            failed_report_before = failed_report.read_bytes()
            successor_report_before = successor_report.read_bytes()

            overview = company.quality_supersession_summaries()

            self.assertEqual(overview["schema"], QUALITY_SUPERSESSION_LIST_SCHEMA)
            self.assertEqual(overview["superseded_count"], 1)
            self.assertEqual(overview["verified_count"], 1)
            self.assertEqual(overview["review_required_count"], 0)
            self.assertEqual(overview["retirement_audit_review_required_count"], 0)
            self.assertEqual(overview["retirement_audit_counts"], {
                "input_fingerprint_bound": 1,
                "legacy_reason_only": 0,
                "malformed": 0,
                "successor_proof_bound": 0,
            })
            self.assertEqual(overview["next_action"], "none")
            item = overview["items"][0]
            self.assertEqual(item["queue_id"], queue_id)
            self.assertEqual(item["failed_job_id"], job_id)
            self.assertEqual(item["proof_status"], "verified")
            self.assertEqual(item["successor_job_id"], successor_job_id)
            self.assertEqual(item["successor_score"], 100)
            self.assertEqual(item["evaluator_version"], EVALUATOR_VERSION)
            self.assertRegex(item["proof_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(item["blockers"], [])
            self.assertEqual(
                item["retirement_audit_status"], "input_fingerprint_bound",
            )
            self.assertGreater(item["retirement_event_id"], 0)
            self.assertIsInstance(item["retirement_recorded_at"], str)
            self.assertEqual(
                item["retirement_successor_job_id"], successor_job_id,
            )
            self.assertEqual(
                item["retirement_proof_sha256"], retirement["proof_sha256"],
            )
            self.assertEqual(
                item["retirement_input_fingerprint_sha256"],
                retirement["successor_input_fingerprint_sha256"],
            )
            self.assertTrue(all(value is False for value in overview["effects"].values()))
            rendered = json.dumps(overview, sort_keys=True)
            self.assertNotIn(objective, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("output_path", rendered)

            output = io.StringIO()
            cli_model = CountingMockModel()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "queue",
                    "supersession-list",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            cli_overview = json.loads(output.getvalue())
            self.assertEqual(cli_overview, overview)
            self.assertEqual(cli_model.calls, 0)
            self.assertEqual(company.model.calls, 0)
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(failed_report.read_bytes(), failed_report_before)
            self.assertEqual(successor_report.read_bytes(), successor_report_before)

    def test_retired_review_surfaces_stale_proof_in_dashboard_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                company, queue_id, _, successor_job_id, objective, _, _,
            ) = self._seed_eligible_failure(root)
            company.supersede_quality_failure(
                queue_id,
                "The exact current retry replaces this result while audit history remains.",
                successor_job_id,
            )
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE evaluations SET passed=0 WHERE job_id=?",
                    (successor_job_id,),
                )
                db.execute(
                    "UPDATE evaluation_history SET passed=0 WHERE id=("
                    "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                    (successor_job_id,),
                )
            database_before = company.db_path.read_bytes()

            overview = company.quality_supersession_summaries()
            self.assertEqual(overview["verified_count"], 0)
            self.assertEqual(overview["review_required_count"], 1)
            self.assertEqual(overview["retirement_audit_review_required_count"], 0)
            self.assertEqual(overview["next_action"], "review_superseded_failures")
            item = overview["items"][0]
            self.assertEqual(item["proof_status"], "review_required")
            self.assertEqual(
                item["retirement_audit_status"], "input_fingerprint_bound",
            )
            self.assertIsNone(item["successor_job_id"])
            self.assertIsNone(item["proof_sha256"])
            self.assertEqual(
                item["blockers"], ["no_current_passing_exact_retry_descendant"],
            )

            page = render_quality_supersession_overview(company)
            self.assertIn("Retired failure proof review", page)
            self.assertIn(QUALITY_SUPERSESSION_LIST_SCHEMA, page)
            self.assertIn(queue_id, page)
            self.assertIn("review_required", page)
            self.assertIn("no_current_passing_exact_retry_descendant", page)
            self.assertIn("A current warning does not rewrite", page)
            self.assertNotIn(objective, page)
            self.assertNotIn(str(root), page)
            self.assertLess(len(page.encode("utf-8")), 32_768)
            self.assertIn('/quality-supersessions', render_dashboard(company))
            self.assertIn(
                'href="/quality-supersessions"',
                render_quality_failure_overview(company),
            )

            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/quality-supersessions", timeout=3) as response:
                    http_page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(
                        response.headers["Content-Type"], "text/html; charset=utf-8",
                    )
                self.assertEqual(http_page, page)

                with self.assertRaises(urllib.error.HTTPError) as unexpected_query:
                    opener.open(base + "/quality-supersessions?extra=1", timeout=3)
                self.assertEqual(unexpected_query.exception.code, 404)
                unexpected_query.exception.close()

                with patch.object(
                    company, "quality_supersession_summaries",
                    side_effect=RuntimeError("private-review-race"),
                ), self.assertRaises(urllib.error.HTTPError) as unstable:
                    opener.open(base + "/quality-supersessions", timeout=3)
                self.assertEqual(unstable.exception.code, 409)
                unstable_body = unstable.exception.read().decode("utf-8")
                unstable.exception.close()
                self.assertIn("retry after local state is stable", unstable_body)
                self.assertNotIn("private-review-race", unstable_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.model.calls, 0)

    def test_retired_review_distinguishes_bound_legacy_and_malformed_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company, queue_id, job_id, successor_job_id, objective, _, _ = (
                self._seed_eligible_failure(root)
            )
            retirement_reason = (
                "Private historical reason must never appear in the retired review."
            )
            company.supersede_quality_failure(
                queue_id, retirement_reason, successor_job_id,
            )
            base_keys = {
                "job_id", "previous_status", "project_id", "queue_id",
                "reason", "source",
            }
            with closing(sqlite3.connect(company.db_path)) as db, db:
                event_id, raw_detail = db.execute(
                    "SELECT id, detail FROM events WHERE job_id=? "
                    "AND kind='queue_quality_failure_superseded'", (job_id,),
                ).fetchone()
                detail = json.loads(raw_detail)
                proof_bound_detail = dict(detail)
                proof_bound_detail.pop("successor_input_fingerprint_sha256")
                db.execute(
                    "UPDATE events SET detail=? WHERE id=?",
                    (json.dumps(proof_bound_detail, sort_keys=True), event_id),
                )

            proof_bound = company.quality_supersession_summaries()
            proof_bound_item = proof_bound["items"][0]
            self.assertEqual(
                proof_bound_item["retirement_audit_status"],
                "successor_proof_bound",
            )
            self.assertEqual(
                proof_bound_item["retirement_successor_job_id"], successor_job_id,
            )
            self.assertRegex(
                proof_bound_item["retirement_proof_sha256"], r"^[0-9a-f]{64}$",
            )
            self.assertIsNone(
                proof_bound_item["retirement_input_fingerprint_sha256"],
            )
            self.assertEqual(
                proof_bound["retirement_audit_review_required_count"], 0,
            )
            self.assertEqual(proof_bound["next_action"], "none")

            with closing(sqlite3.connect(company.db_path)) as db, db:
                legacy_detail = {
                    key: value for key, value in detail.items() if key in base_keys
                }
                db.execute(
                    "UPDATE events SET detail=? WHERE id=?",
                    (json.dumps(legacy_detail, sort_keys=True), event_id),
                )

            legacy = company.quality_supersession_summaries()
            self.assertEqual(legacy["verified_count"], 1)
            self.assertEqual(legacy["review_required_count"], 0)
            self.assertEqual(legacy["retirement_audit_review_required_count"], 1)
            self.assertEqual(legacy["next_action"], "review_superseded_failures")
            legacy_item = legacy["items"][0]
            self.assertEqual(
                legacy_item["retirement_audit_status"], "legacy_reason_only",
            )
            self.assertEqual(legacy_item["retirement_event_id"], event_id)
            self.assertIsNone(legacy_item["retirement_successor_job_id"])
            self.assertIsNone(legacy_item["retirement_proof_sha256"])
            self.assertIsNone(
                legacy_item["retirement_input_fingerprint_sha256"],
            )
            rendered = json.dumps(legacy, sort_keys=True)
            self.assertNotIn(retirement_reason, rendered)
            self.assertNotIn(objective, rendered)
            legacy_page = render_quality_supersession_overview(company)
            self.assertIn("legacy_reason_only", legacy_page)
            self.assertNotIn(retirement_reason, legacy_page)
            self.assertNotIn(str(root), legacy_page)

            partial_detail = dict(legacy_detail)
            partial_detail["successor_job_id"] = successor_job_id
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE events SET detail=? WHERE id=?",
                    (json.dumps(partial_detail, sort_keys=True), event_id),
                )
            malformed = company.quality_supersession_summaries()
            malformed_item = malformed["items"][0]
            self.assertEqual(
                malformed_item["retirement_audit_status"], "malformed",
            )
            self.assertEqual(
                malformed["retirement_audit_counts"]["malformed"], 1,
            )
            self.assertEqual(
                malformed["retirement_audit_review_required_count"], 1,
            )
            self.assertIsNone(malformed_item["retirement_successor_job_id"])
            self.assertIn(
                "malformed", render_quality_supersession_overview(company),
            )
            self.assertEqual(company.model.calls, 0)

    def test_retired_review_refuses_crossing_state_and_malformed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company, queue_id, _, successor_job_id, _, _, _ = (
                self._seed_eligible_failure(Path(tmp))
            )
            company.supersede_quality_failure(
                queue_id,
                "The exact current retry replaces this result while audit history remains.",
                successor_job_id,
            )
            database_before = company.db_path.read_bytes()
            snapshot = company._quality_supersession_index_snapshot()
            changed = dict(snapshot)
            changed["database_sha256"] = "0" * 64
            with patch.object(
                company, "_quality_supersession_index_snapshot",
                side_effect=[snapshot, changed, snapshot],
            ), self.assertRaisesRegex(RuntimeError, "changed during observation"):
                company.quality_supersession_summaries()

            malformed = company.quality_supersession_summaries()
            malformed["effects"] = {"model_called": True}
            with patch.object(
                company, "quality_supersession_summaries", return_value=malformed,
            ), self.assertRaisesRegex(ValueError, "overview is malformed"):
                render_quality_supersession_overview(company)
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.model.calls, 0)


if __name__ == "__main__":
    unittest.main()
