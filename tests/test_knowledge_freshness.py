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
            company = Company(root / "state", CountingModel())
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


if __name__ == "__main__":
    unittest.main()
