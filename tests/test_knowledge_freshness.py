from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from local_company.cli import main as cli_main
from local_company.core import (
    KNOWLEDGE_FRESHNESS_SCHEMA,
    KNOWLEDGE_REFRESH_SCHEMA,
    Company,
    MockModel,
)
from local_company.spreadsheet import SpreadsheetError


class CountingModel(MockModel):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        return super().complete(system, prompt)


class FailFirstModel(CountingModel):
    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated first model failure")
        return MockModel().complete(system, prompt)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def knowledge_rows(company: Company) -> dict[str, tuple[str, str, str]]:
    with closing(company._connect()) as db:
        return {
            item_id: (digest, content, added_at)
            for item_id, digest, content, added_at in db.execute(
                "SELECT id, sha256, content, added_at FROM knowledge ORDER BY id"
            )
        }


class KnowledgeFreshnessTests(unittest.TestCase):
    def test_audit_is_project_scoped_pathless_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            alpha = company.create_project("Alpha")
            beta = company.create_project("Beta")
            current = root / "current.md"
            changed = root / "changed.md"
            missing = root / "missing.md"
            other = root / "other.md"
            current.write_text("current private phrase", encoding="utf-8")
            changed.write_text("old private phrase", encoding="utf-8")
            missing.write_text("soon missing", encoding="utf-8")
            other.write_text("other project", encoding="utf-8")
            current_id, _ = company.add_knowledge(current, alpha)
            changed_id, _ = company.add_knowledge(changed, alpha)
            missing_id, _ = company.add_knowledge(missing, alpha)
            other_id, _ = company.add_knowledge(other, beta)
            changed.write_text("new private phrase", encoding="utf-8")
            missing.unlink()
            before = file_sha256(company.db_path)

            result = company.knowledge_freshness("Alpha")

            self.assertEqual(result["schema"], KNOWLEDGE_FRESHNESS_SCHEMA)
            self.assertEqual(result["project_id"], alpha)
            self.assertEqual(result["source_count"], 3)
            self.assertFalse(result["ready_for_use"])
            self.assertEqual(
                result["status_counts"],
                {"current": 1, "changed": 1, "missing": 1, "unavailable": 0},
            )
            self.assertEqual(
                {item["id"]: item["status"] for item in result["items"]},
                {current_id: "current", changed_id: "changed", missing_id: "missing"},
            )
            self.assertNotIn(other_id, {item["id"] for item in result["items"]})
            rendered = json.dumps(result, sort_keys=True)
            for private_value in (
                str(current.resolve()), str(changed.resolve()), str(missing.resolve()),
                "current private phrase", "new private phrase", "sha256",
            ):
                self.assertNotIn(private_value, rendered)
            self.assertEqual(
                result["effects"],
                {
                    "knowledge_records_mutated": False,
                    "model_called": False,
                    "work_started": False,
                },
            )
            self.assertEqual(file_sha256(company.db_path), before)
            self.assertEqual(model.calls, 0)

    def test_refresh_updates_only_changed_rows_and_never_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Refresh Lab")
            changed = root / "changed.md"
            current = root / "current.md"
            changed.write_text("old indexed phrase", encoding="utf-8")
            current.write_text("stable indexed phrase", encoding="utf-8")
            changed_id, _ = company.add_knowledge(changed, project)
            current_id, _ = company.add_knowledge(current, project)
            before_rows = knowledge_rows(company)
            changed.write_text("brand new searchable phrase", encoding="utf-8")
            source_hashes = {path: file_sha256(path) for path in (changed, current)}

            result = company.refresh_project_knowledge("Refresh Lab")

            self.assertEqual(result["schema"], KNOWLEDGE_REFRESH_SCHEMA)
            self.assertEqual(result["project_id"], project)
            self.assertEqual(result["source_count"], 2)
            self.assertEqual(result["refreshed_count"], 1)
            self.assertEqual(result["unchanged_count"], 1)
            self.assertEqual(result["refreshed_ids"], [changed_id])
            after_rows = knowledge_rows(company)
            self.assertNotEqual(after_rows[changed_id], before_rows[changed_id])
            self.assertEqual(after_rows[current_id], before_rows[current_id])
            self.assertIn("brand new searchable phrase", after_rows[changed_id][1])
            self.assertEqual(
                {path: file_sha256(path) for path in (changed, current)}, source_hashes,
            )
            self.assertTrue(company.knowledge_freshness(project)["ready_for_use"])
            hits = company.search_knowledge("brand new searchable phrase", project=project)
            self.assertEqual(hits[0].path, str(changed.resolve()))
            rendered = json.dumps(result, sort_keys=True)
            self.assertNotIn(str(changed.resolve()), rendered)
            self.assertNotIn("brand new searchable phrase", rendered)
            self.assertEqual(model.calls, 0)

    def test_missing_source_refuses_entire_refresh_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", CountingModel())
            project = company.create_project("Rollback Lab")
            changed = root / "changed.md"
            missing = root / "missing.md"
            changed.write_text("old", encoding="utf-8")
            missing.write_text("present", encoding="utf-8")
            changed_id, _ = company.add_knowledge(changed, project)
            company.add_knowledge(missing, project)
            changed.write_text("new", encoding="utf-8")
            missing.unlink()
            before = file_sha256(company.db_path)

            with self.assertRaisesRegex(RuntimeError, "refused before mutation"):
                company.refresh_project_knowledge(project)

            self.assertEqual(file_sha256(company.db_path), before)
            self.assertEqual(knowledge_rows(company)[changed_id][1], "old")

    def test_database_error_rolls_back_every_changed_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", CountingModel())
            project = company.create_project("Atomic Lab")
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("first old", encoding="utf-8")
            second.write_text("second old", encoding="utf-8")
            first_id, _ = company.add_knowledge(first, project)
            second_id, _ = company.add_knowledge(second, project)
            first.write_text("first new", encoding="utf-8")
            second.write_text("second new", encoding="utf-8")
            before_rows = knowledge_rows(company)
            ordered_ids = sorted((first_id, second_id))
            with closing(company._connect(immediate=True)) as db, db:
                db.execute(
                    "CREATE TRIGGER refuse_second_refresh BEFORE UPDATE ON knowledge "
                    f"WHEN OLD.id='{ordered_ids[1]}' "
                    "BEGIN SELECT RAISE(ABORT, 'test refusal'); END"
                )

            with self.assertRaisesRegex(RuntimeError, "no partial refresh was committed"):
                company.refresh_project_knowledge(project)

            self.assertEqual(knowledge_rows(company), before_rows)

    def test_source_change_between_preflight_reads_refuses_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", CountingModel())
            project = company.create_project("Unstable Lab")
            source = root / "source.md"
            source.write_text("indexed", encoding="utf-8")
            company.add_knowledge(source, project)
            source.write_text("first candidate", encoding="utf-8")
            before = file_sha256(company.db_path)
            original = company._read_knowledge_snapshot
            reads = 0

            def changing_read(path: Path, *, retain_content: bool):
                nonlocal reads
                snapshot = original(path, retain_content=retain_content)
                reads += 1
                if reads == 1:
                    source.write_text("second candidate with new size", encoding="utf-8")
                return snapshot

            with patch.object(
                company, "_read_knowledge_snapshot", side_effect=changing_read,
            ), self.assertRaisesRegex(RuntimeError, "changed during preflight"):
                company.refresh_project_knowledge(project)

            self.assertEqual(file_sha256(company.db_path), before)
            self.assertEqual(knowledge_rows(company)[next(iter(knowledge_rows(company)))][1], "indexed")

    def test_unsafe_reader_failure_does_not_register_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.md"
            source.write_text("private", encoding="utf-8")
            company = Company(root / "state", CountingModel())
            company.initialize()
            before = file_sha256(company.db_path)

            with patch(
                "local_company.core.read_stable_local_file",
                side_effect=SpreadsheetError("source changed while reading"),
            ), self.assertRaisesRegex(ValueError, "unavailable or unsafe"):
                company.add_knowledge(source)

            self.assertEqual(company.knowledge_items(), [])
            self.assertEqual(file_sha256(company.db_path), before)

    def test_audit_refuses_more_than_bounded_source_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Bounded Lab")
            with closing(company._connect(immediate=True)) as db, db:
                for index in range(65):
                    item_id = f"item-{index:02d}"
                    db.execute(
                        "INSERT INTO knowledge VALUES (?, ?, ?, ?, ?)",
                        (item_id, str(root / f"{index}.md"), "0" * 64, "", "now"),
                    )
                    db.execute(
                        "INSERT INTO project_knowledge VALUES (?, ?)",
                        (project, item_id),
                    )

            with self.assertRaisesRegex(ValueError, "at most 64"):
                company.knowledge_freshness(project)
            with self.assertRaisesRegex(ValueError, "at most 64"):
                company.run("Review the bounded source scope", project=project)
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])

    def test_cli_audit_and_refresh_emit_versioned_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            source = root / "source.md"
            source.write_text("old CLI value", encoding="utf-8")
            company = Company(state, CountingModel())
            company.create_project("CLI Lab")
            company.add_knowledge(source, "CLI Lab")
            source.write_text("new CLI value", encoding="utf-8")

            audit_output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "knowledge", "audit",
                    "--project", "CLI Lab",
                ],
            ), patch("sys.stdout", audit_output):
                self.assertEqual(cli_main(), 0)
            audit = json.loads(audit_output.getvalue())
            self.assertEqual(audit["schema"], KNOWLEDGE_FRESHNESS_SCHEMA)
            self.assertEqual(audit["status_counts"]["changed"], 1)

            refresh_output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "knowledge", "refresh",
                    "--project", "CLI Lab",
                ],
            ), patch("sys.stdout", refresh_output):
                self.assertEqual(cli_main(), 0)
            refresh = json.loads(refresh_output.getvalue())
            self.assertEqual(refresh["schema"], KNOWLEDGE_REFRESH_SCHEMA)
            self.assertEqual(refresh["refreshed_count"], 1)

    def test_direct_execution_refuses_stale_knowledge_without_state_or_model_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Execution Gate")
            changed = root / "private-changed.md"
            missing = root / "private-missing.md"
            changed.write_text("private old baseline", encoding="utf-8")
            missing.write_text("private disappearing baseline", encoding="utf-8")
            company.add_knowledge(changed, project)
            company.add_knowledge(missing, project)
            changed.write_text("private new baseline", encoding="utf-8")
            missing.unlink()
            before = file_sha256(company.db_path)

            with self.assertRaisesRegex(
                RuntimeError, "changed=1, missing=1, unavailable=0",
            ) as caught:
                company.run("Review the private baseline", project=project)

            rendered = str(caught.exception)
            self.assertNotIn(str(changed.resolve()), rendered)
            self.assertNotIn("private old baseline", rendered)
            self.assertNotIn("private new baseline", rendered)
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])
            self.assertEqual(file_sha256(company.db_path), before)

    def test_queue_preflight_leaves_stale_item_queued_and_unclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Queue Gate")
            source = root / "queue.md"
            source.write_text("queued current baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            queue_id = company.enqueue(
                "Review queued current baseline", project=project,
            )
            source.write_text("queued changed baseline", encoding="utf-8")
            before = file_sha256(company.db_path)

            with self.assertRaisesRegex(RuntimeError, "before model work"):
                company.run_next_queue_item(queue_id)

            row = company.queue_items()[0]
            self.assertEqual(row[0], queue_id)
            self.assertEqual(row[1], "queued")
            self.assertEqual(row[7], "")
            self.assertEqual(row[8], "")
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])
            self.assertEqual(file_sha256(company.db_path), before)

    def test_owner_gate_is_not_masked_by_stale_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Owner Gate")
            source = root / "owner.md"
            source.write_text("owner gate baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            queue_id = company.enqueue(
                "Send email to every prospect", project=project,
            )
            source.write_text("stale owner gate baseline", encoding="utf-8")

            with self.assertRaisesRegex(PermissionError, "Approval request"):
                company.run_next_queue_item(queue_id)

            self.assertEqual(company.queue_items()[0][1], "needs_approval")
            self.assertEqual(len(company.action_requests("pending")), 1)
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])

    def test_source_change_between_execution_scans_refuses_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Execution Race")
            source = root / "race.md"
            source.write_text("current race baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            before = file_sha256(company.db_path)
            original = company._read_knowledge_snapshot
            reads = 0

            def change_after_first_scan(path: Path, *, retain_content: bool):
                nonlocal reads
                snapshot = original(path, retain_content=retain_content)
                reads += 1
                if reads == 1:
                    source.write_text("changed during execution preflight", encoding="utf-8")
                return snapshot

            with patch.object(
                company, "_read_knowledge_snapshot", side_effect=change_after_first_scan,
            ), self.assertRaisesRegex(RuntimeError, "before model work"):
                company.run("Review the current race baseline", project=project)

            self.assertGreaterEqual(reads, 2)
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])
            self.assertEqual(file_sha256(company.db_path), before)

    def test_source_change_during_cache_inspection_blocks_reuse_and_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Cache Gate")
            source = root / "cache.md"
            source.write_text("cache baseline is current", encoding="utf-8")
            company.add_knowledge(source, project)
            first_job, _ = company.run("Review cache baseline", project=project)
            calls_after_first = model.calls
            before = file_sha256(company.db_path)
            original_reader = company._read_local_report_bytes
            mutated = False

            def read_then_mutate(path: str | None) -> bytes:
                nonlocal mutated
                report = original_reader(path)
                if not mutated:
                    source.write_text("cache baseline changed during reuse", encoding="utf-8")
                    mutated = True
                return report

            with patch.object(
                company, "_read_local_report_bytes", side_effect=read_then_mutate,
            ), self.assertRaisesRegex(RuntimeError, "before model work"):
                company.run("Review cache baseline", project=project)

            self.assertTrue(mutated)
            self.assertEqual(model.calls, calls_after_first)
            self.assertEqual([row[0] for row in company.jobs()], [first_job])
            self.assertEqual(file_sha256(company.db_path), before)

    def test_retry_refuses_stale_knowledge_without_second_model_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = FailFirstModel()
            company = Company(root / "state", model)
            project = company.create_project("Retry Gate")
            source = root / "retry.md"
            source.write_text("retry inventory baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            with self.assertRaisesRegex(RuntimeError, "simulated first model failure"):
                company.run("Review retry inventory baseline", project=project)
            failed_job = company.jobs()[0][0]
            source.write_text("retry inventory changed", encoding="utf-8")
            calls_before_retry = model.calls

            with self.assertRaisesRegex(RuntimeError, "before model work"):
                company.retry(failed_job)

            self.assertEqual(model.calls, calls_before_retry)
            self.assertEqual(len(company.jobs()), 1)
            self.assertEqual(company.jobs()[0][1], "failed")

    def test_resume_requires_current_scope_and_matching_frozen_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = FailFirstModel()
            company = Company(root / "state", model)
            project = company.create_project("Resume Gate")
            source = root / "resume.md"
            source.write_text("resume inventory baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            with self.assertRaisesRegex(RuntimeError, "simulated first model failure"):
                company.run("Review resume inventory baseline", project=project)
            failed_job = company.jobs()[0][0]
            source.write_text("resume inventory changed", encoding="utf-8")
            calls_before_resume = model.calls

            with self.assertRaisesRegex(RuntimeError, "before model work"):
                company.resume(failed_job)
            self.assertEqual(model.calls, calls_before_resume)
            self.assertEqual(company.jobs()[0][1], "failed")

            company.refresh_project_knowledge(project)
            with self.assertRaisesRegex(RuntimeError, "use retry") as caught:
                company.resume(failed_job)
            self.assertNotIn(str(source.resolve()), str(caught.exception))
            self.assertEqual(model.calls, calls_before_resume)
            self.assertEqual(company.jobs()[0][1], "failed")

    def test_unprojected_execution_gates_every_globally_searchable_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Global Gate")
            source = root / "global.md"
            source.write_text("global indexed baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            source.write_text("global changed baseline", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed=1"):
                company.run("Review globally searchable baseline")

            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])

    def test_evidence_validation_uses_the_stable_safe_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Evidence Reader")
            source = root / "evidence.md"
            source.write_text("evidence reader baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            job_id, _ = company.run("Review evidence reader baseline", project=project)
            calls_before_recheck = model.calls

            with patch(
                "local_company.core.read_stable_local_file",
                side_effect=SpreadsheetError("source changed while reading"),
            ):
                evaluation = company.evaluate_job(job_id)

            self.assertFalse(evaluation["checks"]["evidence_manifest_valid"])
            self.assertEqual(evaluation["manifest_reason"], "source_stale")
            self.assertEqual(model.calls, calls_before_recheck)

    def test_unsafe_execution_source_is_pathless_and_starts_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingModel()
            company = Company(root / "state", model)
            project = company.create_project("Unsafe Gate")
            source = root / "unsafe-private.md"
            source.write_text("unsafe private baseline", encoding="utf-8")
            company.add_knowledge(source, project)
            before = file_sha256(company.db_path)

            with patch(
                "local_company.core.read_stable_local_file",
                side_effect=SpreadsheetError("unsafe path internals"),
            ), self.assertRaisesRegex(RuntimeError, "unavailable=1") as caught:
                company.run("Review unsafe private baseline", project=project)

            self.assertNotIn(str(source.resolve()), str(caught.exception))
            self.assertNotIn("unsafe private baseline", str(caught.exception))
            self.assertEqual(model.calls, 0)
            self.assertEqual(company.jobs(), [])
            self.assertEqual(file_sha256(company.db_path), before)


if __name__ == "__main__":
    unittest.main()
