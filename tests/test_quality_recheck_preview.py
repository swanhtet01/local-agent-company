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
    Company, EVALUATOR_VERSION, MockModel, QUALITY_RECHECK_PREVIEW_SCHEMA,
)
from local_company.dashboard import (
    create_dashboard_server, render_dashboard, render_quality_failure_overview,
    render_quality_recheck_preview, render_queue_retry_preflight,
)


class CountingMockModel(MockModel):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(system, prompt)


class QualityRecheckPreviewTests(unittest.TestCase):
    @staticmethod
    def _seed_historical_failure(root: Path) -> tuple[Company, str, str, str, int]:
        company = Company(root / "state", MockModel())
        objective = "Private historical objective must never appear in the preview"
        queue_id = company.enqueue(objective, roles=["quality"], priority=91)
        observed_queue, job_id, _, passed = company.run_next_queue_item()
        if observed_queue != queue_id or not passed:
            raise AssertionError("Mock mission must begin with a passing sealed report")

        with closing(sqlite3.connect(company.db_path)) as db, db:
            checks = json.loads(db.execute(
                "SELECT checks_json FROM evaluations WHERE job_id=?", (job_id,),
            ).fetchone()[0])
            gate = "placeholder_artifacts_absent"
            if checks.get(gate) is not True:
                raise AssertionError("Selected historical gate must pass under the current evaluator")
            checks[gate] = False
            historical_score = round(sum(checks.values()) * 100 / len(checks))
            encoded = json.dumps(checks, sort_keys=True)
            db.execute(
                "UPDATE evaluations SET passed=0, score=?, checks_json=? WHERE job_id=?",
                (historical_score, encoded, job_id),
            )
            db.execute(
                "UPDATE evaluation_history SET passed=0, score=?, checks_json=?, "
                "evaluator_version='local-quality-2026-07-01.1' WHERE id=("
                "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                (historical_score, encoded, job_id),
            )
            db.execute(
                "UPDATE mission_queue SET status='quality_failed' WHERE id=?", (queue_id,),
            )
        company.model = CountingMockModel()
        return company, queue_id, job_id, objective, historical_score

    def test_preview_uses_current_evaluator_without_mutating_real_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company, queue_id, job_id, objective, historical_score = (
                self._seed_historical_failure(root)
            )
            with closing(sqlite3.connect(company.db_path)) as db:
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                report_path = Path(db.execute(
                    "SELECT output_path FROM jobs WHERE id=?", (job_id,),
                ).fetchone()[0])
            database_before = company.db_path.read_bytes()
            report_before = report_path.read_bytes()

            preview = company.quality_recheck_preview(job_id)

            self.assertEqual(preview["schema"], QUALITY_RECHECK_PREVIEW_SCHEMA)
            self.assertEqual(preview["job_id"], job_id)
            self.assertEqual(preview["stored"]["quality_status"], "failed")
            self.assertEqual(preview["stored"]["score"], historical_score)
            self.assertEqual(preview["stored"]["queue_id"], queue_id)
            self.assertEqual(preview["stored"]["queue_status"], "quality_failed")
            self.assertEqual(preview["current_preview"]["quality_status"], "passed")
            self.assertEqual(preview["current_preview"]["score"], 100)
            self.assertEqual(
                preview["current_preview"]["evaluator_version"], EVALUATOR_VERSION,
            )
            self.assertEqual(preview["current_preview"]["failed_checks"], [])
            self.assertTrue(preview["current_preview"]["report_integrity_valid"])
            self.assertTrue(preview["current_preview"]["evidence_manifest_valid"])
            self.assertTrue(preview["comparison"]["evaluator_changed"])
            self.assertTrue(preview["comparison"]["outcome_changed"])
            self.assertTrue(preview["comparison"]["result_changed"])
            self.assertEqual(
                preview["comparison"]["resolved_failed_checks"],
                ["placeholder_artifacts_absent"],
            )
            self.assertEqual(preview["comparison"]["new_failed_checks"], [])
            self.assertEqual(preview["next_action"], "review_then_run_quality_evaluation")
            self.assertTrue(preview["observed_state_stable"])
            self.assertTrue(all(value is False for value in preview["effects"].values()))
            self.assertEqual(company.model.calls, 0)

            rendered = json.dumps(preview, sort_keys=True)
            self.assertNotIn(objective, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("output_path", rendered)
            self.assertLess(len(rendered.encode("utf-8")), 8_192)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                queue_after = db.execute(
                    "SELECT status FROM mission_queue WHERE id=?", (queue_id,),
                ).fetchone()[0]
            self.assertEqual(history_after, history_before)
            self.assertEqual(queue_after, "quality_failed")
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(report_path.read_bytes(), report_before)

    def test_invalid_integrity_preserves_history_and_directs_current_evidence_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company, _, job_id, _, _ = self._seed_historical_failure(root)
            with closing(sqlite3.connect(company.db_path)) as db:
                report_path = Path(db.execute(
                    "SELECT output_path FROM jobs WHERE id=?", (job_id,),
                ).fetchone()[0])
            report_path.write_bytes(report_path.read_bytes() + b"\nlocal drift\n")
            database_before = company.db_path.read_bytes()
            report_before = report_path.read_bytes()

            preview = company.quality_recheck_preview(job_id)

            self.assertEqual(
                preview["next_action"],
                "preserve_history_then_retry_with_current_evidence",
            )
            self.assertFalse(preview["current_preview"]["report_integrity_valid"])
            overview = company.quality_failure_summaries()
            item = overview["items"][0]
            self.assertIn(
                "preserve_history_and_retry_with_current_evidence",
                item["current_preview"]["repair_actions"],
            )
            self.assertNotIn(
                "repair_sealed_report_or_evidence_integrity_before_retry",
                item["current_preview"]["repair_actions"],
            )
            self.assertEqual(
                overview["next_action"],
                "review_then_retry_highest_priority_with_current_evidence",
            )
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(report_path.read_bytes(), report_before)
            self.assertEqual(company.model.calls, 0)

    def test_cli_and_dashboard_preview_are_bounded_exact_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company, queue_id, job_id, objective, _ = self._seed_historical_failure(root)
            database_before = company.db_path.read_bytes()
            cli_model = CountingMockModel()
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "quality", job_id,
                    "--preview",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            cli_preview = json.loads(output.getvalue())
            self.assertEqual(cli_preview["schema"], QUALITY_RECHECK_PREVIEW_SCHEMA)
            self.assertEqual(cli_preview["job_id"], job_id)
            self.assertEqual(cli_model.calls, 0)

            page = render_quality_recheck_preview(company, job_id)
            self.assertIn("Current quality preview", page)
            self.assertIn(QUALITY_RECHECK_PREVIEW_SCHEMA, page)
            self.assertIn(job_id, page)
            self.assertIn("placeholder_artifacts_absent", page)
            self.assertIn("No evaluation was appended", page)
            self.assertNotIn(objective, page)
            self.assertNotIn(str(root), page)
            self.assertLess(len(page.encode("utf-8")), 24_576)
            failure_page = render_quality_failure_overview(company)
            self.assertIn(f'href="/quality-preview/{job_id}"', failure_page)
            self.assertIn(
                f'href="/queue-retry-preflight/{queue_id}"', failure_page,
            )
            self.assertIn(queue_id, failure_page)
            retry_page = render_queue_retry_preflight(company, queue_id)
            self.assertIn("Queue retry preflight", retry_page)
            self.assertIn("local-company.queue-retry-preflight.v1", retry_page)
            self.assertIn("review_then_reset_for_current_evidence_retry", retry_page)
            self.assertIn("did not reset or claim the queue", retry_page)
            self.assertNotIn(objective, retry_page)
            self.assertNotIn(str(root), retry_page)
            self.assertLess(len(retry_page.encode("utf-8")), 16_384)
            mission_page = render_dashboard(company)
            self.assertNotIn(objective, page)
            self.assertIn("Failed mission recovery", mission_page)

            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + f"/quality-preview/{job_id}", timeout=3) as response:
                    http_page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(http_page, page)

                with opener.open(
                    base + f"/queue-retry-preflight/{queue_id}", timeout=3,
                ) as response:
                    http_retry_page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(
                        response.headers["Content-Type"],
                        "text/html; charset=utf-8",
                    )
                self.assertEqual(http_retry_page, retry_page)

                with self.assertRaises(urllib.error.HTTPError) as missing:
                    opener.open(base + "/quality-preview/aaaaaaaaaaaa", timeout=3)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()

                with self.assertRaises(urllib.error.HTTPError) as unexpected_query:
                    opener.open(base + f"/quality-preview/{job_id}?extra=1", timeout=3)
                self.assertEqual(unexpected_query.exception.code, 404)
                unexpected_query.exception.close()

                with self.assertRaises(urllib.error.HTTPError) as missing_retry:
                    opener.open(
                        base + "/queue-retry-preflight/aaaaaaaaaaaa", timeout=3,
                    )
                self.assertEqual(missing_retry.exception.code, 404)
                missing_retry.exception.close()

                with patch.object(
                    company, "quality_recheck_preview",
                    side_effect=RuntimeError("private-race-detail"),
                ), self.assertRaises(urllib.error.HTTPError) as unstable:
                    opener.open(base + f"/quality-preview/{job_id}", timeout=3)
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

    def test_preview_refuses_races_malformed_ids_and_flag_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            company, _, job_id, _, _ = self._seed_historical_failure(Path(tmp))
            with patch.object(
                company, "_quality_recheck_source_fingerprint",
                side_effect=["before", "after"],
            ), self.assertRaisesRegex(RuntimeError, "changed during observation"):
                company.quality_recheck_preview(job_id)
            with self.assertRaisesRegex(ValueError, "Invalid job ID"):
                company.quality_recheck_preview("../unsafe")

            malformed = company.quality_recheck_preview(job_id)
            malformed["effects"] = {"model_called": True}
            with patch.object(
                company, "quality_recheck_preview", return_value=malformed,
            ), self.assertRaisesRegex(ValueError, "preview is malformed"):
                render_quality_recheck_preview(company, job_id)

            for arguments, message in (
                (["quality", job_id, "--summary", "--preview"], "cannot be combined"),
                (["quality", "--failed", "--preview"], "cannot be combined"),
            ):
                error = io.StringIO()
                model = CountingMockModel()
                with patch(
                    "sys.argv", ["local-company", "--home", str(company.home), *arguments],
                ), patch(
                    "local_company.cli.selected_model", return_value=model,
                ), patch("sys.stderr", error):
                    self.assertEqual(cli_main(), 2)
                self.assertIn(message, error.getvalue())
                self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()
