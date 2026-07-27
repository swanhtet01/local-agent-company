from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from local_company.cli import main as cli_main
from local_company.core import Company, MockModel, OPERATOR_BRIEF_SCHEMA
from local_company.dashboard import (
    create_dashboard_server,
    render_dashboard,
    render_operator_brief,
)


class CountingModel(MockModel):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(system, prompt)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OperatorBriefTests(unittest.TestCase):
    def test_brief_is_pathless_read_only_and_prioritizes_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project_name = "Private Project Name"
            project_id = company.create_project(project_name)
            source = root / "private-source.md"
            source.write_text("old private source content", encoding="utf-8")
            company.add_knowledge(source, project_id)
            source.write_text("new private source content", encoding="utf-8")

            queued_objective = "private queued objective must stay hidden"
            quality_objective = "private failed objective must stay hidden"
            company.enqueue(queued_objective, project=project_id, priority=80)
            quality_queue = company.enqueue(
                quality_objective, project=project_id, priority=70,
            )
            job_id = "a" * 12
            now = datetime.now(timezone.utc).isoformat()
            with closing(company._connect(immediate=True)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, project_id) "
                    "VALUES (?, ?, 'failed', ?, ?)",
                    (job_id, "private failed job objective", now, project_id),
                )
                db.execute(
                    "UPDATE mission_queue SET status='quality_failed', job_id=? WHERE id=?",
                    (job_id, quality_queue),
                )
            approval_text = "private credential rotation proposal"
            company.request_action(approval_text, job_id)
            company.create_schedule(
                "Private due schedule", "private schedule objective", 1,
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                project=project_id,
            )
            dataset = root / "private-data.csv"
            dataset.write_text("sku,qty\nA,-1\nB,5\n", encoding="utf-8")
            company.profile_dataset(
                dataset, project_id, allowed_root=root,
                numeric_minimum_rules=[("qty", 0)],
            )
            before = file_sha256(company.db_path)

            result = company.operator_brief(project_id)

            self.assertEqual(result["schema"], OPERATOR_BRIEF_SCHEMA)
            self.assertEqual(result["status"], "attention_required")
            self.assertEqual(result["project_id"], project_id)
            self.assertEqual(result["knowledge"]["status_counts"]["changed"], 1)
            self.assertFalse(result["knowledge"]["ready_for_use"])
            self.assertEqual(result["counts"]["queued_missions"], 1)
            self.assertEqual(result["counts"]["quality_failed_missions"], 1)
            self.assertEqual(result["counts"]["failed_or_interrupted_jobs"], 1)
            self.assertEqual(result["counts"]["project_pending_owner_approvals"], 1)
            self.assertEqual(result["counts"]["due_schedules"], 1)
            self.assertEqual(result["counts"]["dataset_count"], 1)
            self.assertEqual(result["counts"]["dataset_contract_violations"], 1)
            self.assertEqual(result["attention"][0]["code"], "knowledge_changed")
            self.assertEqual(
                result["next_action"], "review_then_refresh_changed_project_sources",
            )
            self.assertTrue(all(value is False for value in result["effects"].values()))
            rendered = json.dumps(result, sort_keys=True)
            for private_value in (
                str(root), str(source), str(dataset), project_name,
                queued_objective, quality_objective, approval_text,
                "old private source content", "new private source content",
                "private schedule objective", "private failed job objective",
            ):
                self.assertNotIn(private_value, rendered)
            self.assertLess(len(rendered.encode("utf-8")), 8_192)
            self.assertEqual(file_sha256(company.db_path), before)
            self.assertEqual(model.calls, 0)

    def test_ready_brief_has_one_safe_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", CountingModel())
            project_id = company.create_project("Idle Project")
            source = root / "current.md"
            source.write_text("current local evidence", encoding="utf-8")
            company.add_knowledge(source, project_id)

            result = company.operator_brief(project_id)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["attention"], [])
            self.assertEqual(result["next_action"], "queue_or_schedule_reviewed_mission")
            self.assertTrue(result["knowledge"]["ready_for_use"])

    def test_brief_rejects_database_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Race Project")
            source = root / "current.md"
            source.write_text("current local evidence", encoding="utf-8")
            company.add_knowledge(source, project_id)
            original = company.knowledge_freshness

            def race(project: str | None = None) -> dict[str, object]:
                result = original(project)
                company.enqueue("private race objective", project=project_id)
                return result

            with patch.object(company, "knowledge_freshness", side_effect=race):
                with self.assertRaisesRegex(RuntimeError, "changed during observation"):
                    company.operator_brief(project_id)
            self.assertEqual(model.calls, 0)

    def test_cli_and_dashboard_expose_bounded_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            model = CountingModel()
            company = Company(state, model)
            project_name = "CLI Private Project"
            project_id = company.create_project(project_name)
            source = root / "source.md"
            source.write_text("private source body", encoding="utf-8")
            company.add_knowledge(source, project_id)
            database_before = company.db_path.read_bytes()

            output = io.StringIO()
            cli_model = CountingModel()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "brief",
                    "--project", project_id,
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            cli_brief = json.loads(output.getvalue())
            self.assertEqual(cli_brief["schema"], OPERATOR_BRIEF_SCHEMA)
            self.assertEqual(cli_brief["project_id"], project_id)
            self.assertNotIn(project_name, output.getvalue())
            self.assertNotIn(str(root), output.getvalue())
            self.assertEqual(cli_model.calls, 0)

            main_page = render_dashboard(company)
            self.assertIn(
                f'href="/operator-brief?project={project_id}"', main_page,
            )
            page = render_operator_brief(company, project_id)
            self.assertIn("Project operator brief", page)
            self.assertIn(OPERATOR_BRIEF_SCHEMA, page)
            self.assertNotIn(project_name, page)
            self.assertNotIn(str(root), page)
            self.assertNotIn("private source body", page)
            self.assertLess(len(page.encode("utf-8")), 32_768)
            malformed = company.operator_brief(project_id)
            malformed["status"] = "attention_required"
            with patch.object(company, "operator_brief", return_value=malformed):
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    render_operator_brief(company, project_id)

            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(
                    base + f"/operator-brief?project={project_id}", timeout=3,
                ) as response:
                    http_page = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Content-Type"], "text/html; charset=utf-8",
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn(OPERATOR_BRIEF_SCHEMA, http_page)
                self.assertIn(project_id, http_page)
                self.assertNotIn(project_name, http_page)
                self.assertNotIn(str(root), http_page)
                self.assertNotIn("private source body", http_page)
                for bad_path in (
                    "/operator-brief",
                    f"/operator-brief?project={project_id}&extra=1",
                    f"/operator-brief?project={project_id}&project={project_id}",
                    "/operator-brief/extra",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        opener.open(base + bad_path, timeout=3)
                    self.assertEqual(missing.exception.code, 404)
                    missing.exception.close()
                with patch.object(
                    company, "operator_brief",
                    side_effect=RuntimeError("private-race-detail"),
                ), self.assertRaises(urllib.error.HTTPError) as unstable:
                    opener.open(
                        base + f"/operator-brief?project={project_id}", timeout=3,
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
            self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()
