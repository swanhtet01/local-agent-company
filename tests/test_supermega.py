import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_company.cli import parser
from local_company.supermega import _vision_sales_bundle_digest, run_vision_sales, vision_sales_status


class SuperMegaCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "supermega-platform"
        self.worker = self.platform / "tools" / "process_vision_lead_inbox.mjs"
        self.worker.parent.mkdir(parents=True)
        self.worker.write_text("// fixed test worker\n", encoding="utf-8")
        self.proposal = self.platform / "tools" / "create_vision_pilot_proposal.mjs"
        self.proposal.write_text("// fixed test proposal\n", encoding="utf-8")
        self.bundle_sha256 = _vision_sales_bundle_digest(self.platform.resolve())
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"test runtime")
        self.sales = self.root / "sales"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def worker_result(**overrides):
        value = {
            "contract": "supermega.vision.lead_inbox_run.v1",
            "processed": 1,
            "replayed": 2,
            "rejected": 0,
            "ignored": 3,
            "effects": {
                "external_requests": 0,
                "messages_sent": 0,
                "payments_accepted": 0,
                "input_files_modified": 0,
            },
        }
        value.update(overrides)
        return value

    def test_runs_only_the_fixed_local_worker_and_returns_pathless_controls(self):
        observed = {}

        def runner(command, **options):
            observed["command"] = command
            observed["options"] = options
            return subprocess.CompletedProcess(command, 0, json.dumps(self.worker_result()) + "\n", "")

        result = run_vision_sales(
            self.platform, self.sales, node_executable=str(self.node), runner=runner,
            expected_bundle_sha256=self.bundle_sha256,
        )

        self.assertEqual(result["contract"], "local-company.supermega-vision-sales.v1")
        self.assertEqual(result["worker"]["processed"], 1)
        self.assertEqual(result["controls"], {
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "input_mutations": 0,
            "serial_execution": True,
        })
        self.assertEqual(result["workspace"]["reply_drafts"], "outbox/reply-drafts")
        self.assertEqual(result["integrity"], {
            "worker_bundle_sha256": self.bundle_sha256,
            "pinned": True,
            "stable_during_run": True,
        })
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(observed["command"], [
            str(self.node.resolve()), str(self.worker.resolve()),
            "--inbox", str((self.sales / "inbox").resolve()),
            "--outbox", str((self.sales / "outbox").resolve()),
        ])
        self.assertEqual(observed["options"]["cwd"], self.platform.resolve())
        self.assertEqual(observed["options"]["timeout"], 60)
        self.assertTrue((self.sales / "inbox").is_dir())
        self.assertTrue((self.sales / "outbox").is_dir())

    def test_rejects_ambiguous_or_unsafe_worker_results(self):
        def unsafe_effects(command, **options):
            result = self.worker_result(effects={"external_requests": 1})
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

        with self.assertRaisesRegex(RuntimeError, "effects_invalid"):
            run_vision_sales(self.platform, self.sales, node_executable=str(self.node), runner=unsafe_effects, expected_bundle_sha256=self.bundle_sha256)

        def extra_output(command, **options):
            payload = json.dumps(self.worker_result())
            return subprocess.CompletedProcess(command, 0, f"noise\n{payload}\n", "")

        with self.assertRaisesRegex(RuntimeError, "output_ambiguous"):
            run_vision_sales(self.platform, self.sales, node_executable=str(self.node), runner=extra_output, expected_bundle_sha256=self.bundle_sha256)

        def failed(command, **options):
            return subprocess.CompletedProcess(command, 1, "", "sensitive details")

        with self.assertRaisesRegex(RuntimeError, "worker_failed"):
            run_vision_sales(self.platform, self.sales, node_executable=str(self.node), runner=failed, expected_bundle_sha256=self.bundle_sha256)

    def test_rejects_unpinned_or_changed_worker_bundle(self):
        calls = []

        def runner(command, **options):
            calls.append(command)
            self.proposal.write_text("// changed during execution\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, json.dumps(self.worker_result()), "")

        with self.assertRaisesRegex(RuntimeError, "digest_mismatch"):
            run_vision_sales(self.platform, self.sales, node_executable=str(self.node), runner=runner, expected_bundle_sha256="0" * 64)
        self.assertEqual(calls, [])

        with self.assertRaisesRegex(RuntimeError, "changed_during_run"):
            run_vision_sales(self.platform, self.sales, node_executable=str(self.node), runner=runner, expected_bundle_sha256=self.bundle_sha256)
        self.assertEqual(len(calls), 1)

    def test_cli_exposes_one_bounded_supermega_vision_sales_command(self):
        args = parser().parse_args([
            "supermega", "vision-sales", "--platform-root", str(self.platform),
            "--sales-root", str(self.sales),
        ])
        self.assertEqual(args.command, "supermega")
        self.assertEqual(args.supermega_command, "vision-sales")
        self.assertEqual(args.platform_root, self.platform)
        self.assertEqual(args.sales_root, self.sales)

        status_args = parser().parse_args([
            "supermega", "vision-sales-status", "--sales-root", str(self.sales),
        ])
        self.assertEqual(status_args.supermega_command, "vision-sales-status")
        self.assertEqual(status_args.sales_root, self.sales)

    def _write_sales_artifact(self, lead_id, *, qualified, price, blockers):
        proposals = self.sales / "outbox" / "proposals"
        replies = self.sales / "outbox" / "reply-drafts"
        receipts = self.sales / "outbox" / "receipts"
        for directory in (proposals, replies, receipts):
            directory.mkdir(parents=True, exist_ok=True)
        proposal = f"proposal for {lead_id}\n".encode()
        reply = f"reply for {lead_id}\n".encode()
        (proposals / f"{lead_id}.proposal.md").write_bytes(proposal)
        (replies / f"{lead_id}.reply.txt").write_bytes(reply)
        receipt = {
            "contract": "supermega.vision.lead_proposal_receipt.v2",
            "lead_id": lead_id,
            "proposal_sha256": hashlib.sha256(proposal).hexdigest(),
            "reply_sha256": hashlib.sha256(reply).hexdigest(),
            "qualified": qualified,
            "blockers": blockers,
            "price_usd": price,
            "proposal_file": f"{lead_id}.proposal.md",
            "reply_file": f"{lead_id}.reply.txt",
        }
        (receipts / f"{lead_id}.json").write_text(json.dumps(receipt), encoding="utf-8")

    def test_status_verifies_pipeline_value_and_remains_read_only(self):
        qualified_id = "LEAD-AAAAAAAAAAAAAAAA"
        blocked_id = "LEAD-BBBBBBBBBBBBBBBB"
        pending_id = "LEAD-CCCCCCCCCCCCCCCC"
        self._write_sales_artifact(qualified_id, qualified=True, price=1_500, blockers=[])
        self._write_sales_artifact(
            blocked_id, qualified=False, price=2_250,
            blockers=["written_screenshot_rights_required"],
        )
        inbox = self.sales / "inbox"
        inbox.mkdir(parents=True)
        for lead_id in (qualified_id, pending_id):
            event = {"event": "supermega.contact.created", "record": {"workflow": "vision", "lead_id": lead_id}}
            (inbox / f"{lead_id}.json").write_text(json.dumps(event), encoding="utf-8")
        rejections = self.sales / "outbox" / "rejections"
        rejections.mkdir(parents=True)
        (rejections / "one.json").write_text("{}", encoding="utf-8")
        before = {path.relative_to(self.sales).as_posix(): path.read_bytes() for path in self.sales.rglob("*") if path.is_file()}

        result = vision_sales_status(self.sales)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["pipeline"], {
            "pending_events": 1,
            "qualified_drafts": 1,
            "blocked_drafts": 1,
            "rejection_receipts": 1,
            "integrity_failures": 0,
            "input_attention": 0,
            "draft_pipeline_value_usd": 1_500,
            "value_label": "Draft proposal value only; not booked or collected revenue.",
        })
        self.assertIn("Run the bounded Vision sales worker", result["next_action"])
        self.assertEqual(result["controls"]["files_modified"], 0)
        after = {path.relative_to(self.sales).as_posix(): path.read_bytes() for path in self.sales.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

        reply = self.sales / "outbox" / "reply-drafts" / f"{qualified_id}.reply.txt"
        reply.write_text("tampered", encoding="utf-8")
        (inbox / "malformed.json").write_text("{not json", encoding="utf-8")
        attention = vision_sales_status(self.sales)
        self.assertEqual(attention["status"], "attention")
        self.assertEqual(attention["pipeline"]["integrity_failures"], 1)
        self.assertEqual(attention["pipeline"]["input_attention"], 1)
        self.assertEqual(attention["pipeline"]["draft_pipeline_value_usd"], 0)
        self.assertIn("integrity failures", attention["next_action"])


if __name__ == "__main__":
    unittest.main()
