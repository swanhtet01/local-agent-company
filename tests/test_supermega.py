import csv
import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_company.cli import _interactive_vision_sales_intake, parser
from local_company.supermega import (
    _vision_sales_bundle_digest,
    create_vision_founding_pilot_packages,
    create_vision_prospect_drafts,
    create_vision_sales_intake,
    import_vision_prospects,
    run_vision_sales,
    vision_commercial_status,
    vision_product_status,
    vision_sales_status,
)


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

    def _write_product_evidence(self, *, ready=False):
        product = self.root / "vision-product"
        product.mkdir()
        dataset_digest = "a" * 64
        spec_digest = "b" * 64
        samples = 90 if ready else 13
        blockers = [] if ready else ["total_samples", "split_samples:test"]
        required = [] if ready else [
            {"kind": "split_samples"}, {"kind": "total_samples"},
        ]
        readiness = {
            "schema": "supermega.vision.dataset-readiness.v1",
            "status": "ready" if ready else "attention",
            "ready": ready,
            "dataset": {"digest": dataset_digest},
            "spec": {"digest": spec_digest},
            "counts": {"samples": samples},
            "thresholds": {"min_total_samples": 90},
            "gates": {"total_samples": ready},
            "blockers": blockers,
            "network_requests": 0,
            "actions_performed": 0,
        }
        readiness["receipt_id"] = hashlib.sha256(json.dumps(
            readiness, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:24]
        receipt_id = readiness["receipt_id"]
        plan = {
            "schema": "supermega.vision.dataset-collection-plan.v1",
            "status": "ready" if ready else "attention",
            "readiness_receipt_id": receipt_id,
            "dataset": {"digest": dataset_digest},
            "spec": {"digest": spec_digest},
            "required": required,
            "recommended": [],
            "summary": {
                "ready": ready,
                "required_tasks": len(required),
                "recommended_tasks": 0,
                "minimum_new_samples_lower_bound": 0 if ready else 77,
            },
            "privacy": {
                "annotations_included": False,
                "paths_included": False,
                "pixels_included": False,
                "sample_ids_included": False,
            },
            "network_requests": 0,
            "actions_performed": 0,
        }
        plan["plan_id"] = hashlib.sha256(json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:24]
        (product / "dataset-readiness-reviewed-013-v2.json").write_text(
            json.dumps(readiness), encoding="utf-8",
        )
        (product / "collection-plan-reviewed-013-v2.json").write_text(
            json.dumps(plan), encoding="utf-8",
        )
        return product, readiness, plan

    @staticmethod
    def _passing_plan_verifier(readiness, plan):
        return lambda _: {
            "schema": "supermega.vision.dataset-collection-plan-verification.v1",
            "status": "verified",
            "passed": True,
            "plan_id": plan["plan_id"],
            "ready": readiness["ready"],
            "required_tasks": len(plan["required"]),
            "recommended_tasks": len(plan["recommended"]),
            "network_requests": 0,
            "actions_performed": 0,
        }

    def test_vision_product_status_cross_checks_pathless_commercial_gate(self):
        product, readiness, plan = self._write_product_evidence()
        verifier = self._passing_plan_verifier(readiness, plan)
        result = vision_product_status(product, plan_verifier=verifier)
        self.assertEqual(result["contract"], "local-company.supermega-vision-product-status.v1")
        self.assertEqual(result["status"], "collection_required")
        self.assertFalse(result["dataset_ready"])
        self.assertEqual(result["commercial_status"], "hold")
        self.assertEqual(result["commercial_reason"], "dataset_readiness_blocked")
        self.assertEqual(result["dataset"]["samples"], 13)
        self.assertEqual(result["dataset"]["minimum_samples"], 90)
        self.assertEqual(result["dataset"]["minimum_new_samples_lower_bound"], 77)
        self.assertEqual(result["evidence"]["blocking_gate_count"], 2)
        self.assertFalse(result["controls"]["paths_included"])
        self.assertEqual(result["controls"]["network_requests"], 0)
        self.assertNotIn(str(self.root), json.dumps(result))

        plan_path = product / "collection-plan-reviewed-013-v2.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["dataset"]["digest"] = "e" * 64
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        invalid = vision_product_status(product, plan_verifier=verifier)
        self.assertEqual(invalid["status"], "unavailable")
        self.assertEqual(invalid["commercial_status"], "hold")
        self.assertEqual(invalid["reason"], "vision_product_evidence_inconsistent")
        self.assertNotIn(str(self.root), json.dumps(invalid))

    def test_dataset_ready_still_requires_held_out_model_evidence(self):
        product, readiness, plan = self._write_product_evidence(ready=True)
        result = vision_product_status(
            product, plan_verifier=self._passing_plan_verifier(readiness, plan),
        )
        self.assertEqual(result["status"], "dataset_ready")
        self.assertTrue(result["dataset_ready"])
        self.assertEqual(result["commercial_status"], "hold")
        self.assertEqual(result["commercial_reason"], "held_out_model_evidence_required")
        self.assertEqual(
            result["next_action"], "train_and_verify_held_out_model_evidence",
        )

    def test_vision_product_status_fails_closed_when_source_replay_fails(self):
        product, _, _ = self._write_product_evidence()

        def failed_verifier(_):
            raise RuntimeError("private verifier detail")

        result = vision_product_status(product, plan_verifier=failed_verifier)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["commercial_status"], "hold")
        self.assertEqual(
            result["reason"], "vision_product_source_verification_failed",
        )
        self.assertNotIn("private verifier detail", json.dumps(result))

    def test_source_replay_rejects_coordinated_identity_rehash(self):
        product, original_readiness, original_plan = self._write_product_evidence()
        original_verifier = self._passing_plan_verifier(
            original_readiness, original_plan,
        )
        readiness_path = product / "dataset-readiness-reviewed-013-v2.json"
        plan_path = product / "collection-plan-reviewed-013-v2.json"
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        readiness["counts"]["samples"] = 14
        readiness.pop("receipt_id")
        readiness["receipt_id"] = hashlib.sha256(json.dumps(
            readiness, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:24]
        plan["readiness_receipt_id"] = readiness["receipt_id"]
        plan.pop("plan_id")
        plan["plan_id"] = hashlib.sha256(json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:24]
        readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        result = vision_product_status(
            product, plan_verifier=original_verifier,
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(
            result["reason"], "vision_product_source_verification_failed",
        )
        self.assertEqual(result["commercial_status"], "hold")

    def test_commercial_status_allows_only_owner_reviewed_founding_pilot(self):
        product = {
            "contract": "local-company.supermega-vision-product-status.v1",
            "status": "collection_required",
            "commercial_status": "hold",
            "dataset_ready": False,
            "dataset": {
                "samples": 13, "minimum_samples": 90,
                "minimum_new_samples_lower_bound": 77,
            },
        }
        sales = {
            "contract": "local-company.supermega-vision-sales-status.v1",
            "status": "ready",
            "pipeline": {
                "qualified_drafts": 0, "blocked_drafts": 0,
                "integrity_failures": 0, "input_attention": 0,
            },
            "research": {
                "researched_unsent_unqualified": 5,
                "outreach_drafts_ready": 5,
                "founding_pilot_packages_ready": 0,
                "integrity_failures": 0,
            },
        }
        result = vision_commercial_status(product=product, sales=sales)
        self.assertEqual(result["status"], "owner_review_ready")
        self.assertEqual(
            result["offer"]["name"],
            "founding_pilot_owned_data_collection_and_held_out_evaluation",
        )
        self.assertEqual(
            result["offer"]["stage"],
            "founding_pilot_draft_internal_review",
        )
        self.assertFalse(result["offer"]["external_send_authorized"])
        self.assertFalse(result["offer"]["pricing_committed"])
        self.assertTrue(result["offer"]["owner_review_required"])
        self.assertFalse(result["product"]["accuracy_claim_allowed"])
        self.assertEqual(result["sales"]["outreach_drafts_ready"], 5)
        self.assertEqual(result["sales"]["draft_claim_review_required"], 5)
        self.assertEqual(result["sales"]["booked_revenue_usd"], 0)
        self.assertIn("current_validated_accuracy", result["claims"]["prohibited"])
        self.assertEqual(result["controls"]["external_sends"], 0)

        sales["research"]["founding_pilot_packages_ready"] = 3
        packaged = vision_commercial_status(product=product, sales=sales)
        self.assertEqual(
            packaged["offer"]["stage"],
            "founding_pilot_package_internal_review",
        )
        self.assertEqual(packaged["sales"]["founding_pilot_packages_ready"], 3)
        self.assertEqual(
            packaged["next_action"],
            "review_one_evidence_gated_founding_pilot_package_without_sending_or_pricing",
        )

        unavailable = dict(product)
        unavailable["status"] = "unavailable"
        attention = vision_commercial_status(product=unavailable, sales=sales)
        self.assertEqual(attention["status"], "attention")
        self.assertEqual(
            attention["offer"]["stage"], "product_evidence_restore_required",
        )

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

        intake_args = parser().parse_args([
            "supermega", "vision-sales-intake", "--input", str(self.root / "prospect.json"),
            "--sales-root", str(self.sales),
        ])
        self.assertEqual(intake_args.supermega_command, "vision-sales-intake")
        self.assertEqual(intake_args.input, self.root / "prospect.json")
        self.assertEqual(intake_args.sales_root, self.sales)

        interactive_args = parser().parse_args([
            "supermega", "vision-sales-intake", "--interactive", "--sales-root", str(self.sales),
        ])
        self.assertTrue(interactive_args.interactive)
        self.assertIsNone(interactive_args.input)

        prospect_args = parser().parse_args([
            "supermega", "vision-prospect-import", "--input", str(self.root / "prospects.csv"),
            "--sales-root", str(self.sales),
        ])
        self.assertEqual(prospect_args.supermega_command, "vision-prospect-import")
        self.assertEqual(prospect_args.input, self.root / "prospects.csv")

        draft_args = parser().parse_args([
            "supermega", "vision-prospect-drafts", "--sales-root", str(self.sales),
        ])
        self.assertEqual(draft_args.supermega_command, "vision-prospect-drafts")
        self.assertEqual(draft_args.sales_root, self.sales)

        package_args = parser().parse_args([
            "supermega", "vision-founding-pilot-packages",
            "--sales-root", str(self.sales),
        ])
        self.assertEqual(
            package_args.supermega_command, "vision-founding-pilot-packages",
        )
        self.assertEqual(package_args.sales_root, self.sales)

        product_args = parser().parse_args([
            "supermega", "vision-product-status", "--product-root", str(self.root),
        ])
        self.assertEqual(product_args.supermega_command, "vision-product-status")
        self.assertEqual(product_args.product_root, self.root)

        commercial_args = parser().parse_args([
            "supermega", "vision-commercial-status",
            "--product-root", str(self.root), "--sales-root", str(self.sales),
        ])
        self.assertEqual(
            commercial_args.supermega_command, "vision-commercial-status",
        )
        self.assertEqual(commercial_args.product_root, self.root)
        self.assertEqual(commercial_args.sales_root, self.sales)

    def _write_prospect_csv(self, rows=None):
        rows = rows or [
            {
                "rank": 1, "organization": "Example POS", "route_type": "direct product team",
                "fit_score_10": 10, "verified_public_signal": "Android and web POS",
                "public_contact": "support@example.com", "source": "https://example.com/product",
                "status": "researched_unsent_unqualified",
            },
            {
                "rank": 2, "organization": "Example Apps", "route_type": "agency or channel partner",
                "fit_score_10": 8, "verified_public_signal": "Native Android development",
                "public_contact": "+95 9 000000000", "source": "https://example.org/services",
                "status": "researched_unsent_unqualified",
            },
        ]
        path = self.root / "prospects.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "rank", "organization", "route_type", "fit_score_10", "verified_public_signal",
                "public_contact", "source", "status",
            ])
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_researched_prospect_import_is_idempotent_and_separate_from_leads(self):
        source = self._write_prospect_csv()
        original = source.read_bytes()

        first = import_vision_prospects(source, self.sales)
        second = import_vision_prospects(source, self.sales)
        status = vision_sales_status(self.sales)

        self.assertEqual(first["contract"], "local-company.supermega-vision-prospect-import.v1")
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["replayed"], 0)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["replayed"], 2)
        self.assertNotIn("Example POS", json.dumps(first))
        self.assertEqual(status["research"], {
            "researched_unsent_unqualified": 2,
            "outreach_drafts_ready": 0,
            "founding_pilot_packages_ready": 0,
            "integrity_failures": 0,
            "value_label": "Research only; not a lead, proposal, booked revenue, or collected revenue.",
        })
        self.assertEqual(status["pipeline"]["qualified_drafts"], 0)
        self.assertEqual(status["pipeline"]["draft_pipeline_value_usd"], 0)
        self.assertIn("nothing has been sent or qualified", status["next_action"])
        self.assertEqual(source.read_bytes(), original)

        artifact = next((self.sales / "research" / "prospects").glob("*.json"))
        artifact.write_text("{}", encoding="utf-8")
        attention = vision_sales_status(self.sales)
        self.assertEqual(attention["status"], "attention")
        self.assertEqual(attention["research"]["integrity_failures"], 1)
        (self.sales / "research" / "prospects" / "unexpected.txt").write_text("not a prospect", encoding="utf-8")
        self.assertEqual(vision_sales_status(self.sales)["research"]["integrity_failures"], 2)

    def test_researched_prospect_import_validates_every_row_before_writing(self):
        rows = [
            {
                "rank": 1, "organization": "Example POS", "route_type": "direct product team",
                "fit_score_10": 10, "verified_public_signal": "Android and web POS",
                "public_contact": "support@example.com", "source": "https://example.com/product",
                "status": "researched_unsent_unqualified",
            },
            {
                "rank": 2, "organization": "Bad Prospect", "route_type": "direct product team",
                "fit_score_10": 8, "verified_public_signal": "Unverified",
                "public_contact": "nobody@example.com", "source": "http://insecure.example.com",
                "status": "qualified",
            },
        ]
        with self.assertRaisesRegex(ValueError, "source_must_be_https"):
            import_vision_prospects(self._write_prospect_csv(rows), self.sales)
        self.assertFalse((self.sales / "research").exists())

    def test_outreach_drafts_are_local_idempotent_and_integrity_checked(self):
        import_vision_prospects(self._write_prospect_csv(), self.sales)
        prospect_files = sorted((self.sales / "research" / "prospects").glob("*.json"))
        research_before = {path.name: path.read_bytes() for path in prospect_files}

        first = create_vision_prospect_drafts(self.sales)
        second = create_vision_prospect_drafts(self.sales)
        status = vision_sales_status(self.sales)

        self.assertEqual(first["contract"], "local-company.supermega-vision-prospect-drafts.v1")
        self.assertEqual(first["drafts_created"], 2)
        self.assertEqual(first["receipts_created"], 2)
        self.assertEqual(second["drafts_created"], 0)
        self.assertEqual(second["drafts_replayed"], 2)
        self.assertEqual(first["controls"]["external_sends"], 0)
        self.assertEqual(first["controls"]["lead_qualifications"], 0)
        self.assertEqual(status["research"]["outreach_drafts_ready"], 2)
        self.assertEqual(status["pipeline"]["qualified_drafts"], 0)
        self.assertEqual(status["pipeline"]["draft_pipeline_value_usd"], 0)
        self.assertIn("nothing has been sent, qualified, or counted as revenue", status["next_action"])
        self.assertEqual({path.name: path.read_bytes() for path in prospect_files}, research_before)

        draft = next((self.sales / "research" / "outreach-drafts").glob("*.txt"))
        draft_text = draft.read_text(encoding="utf-8")
        self.assertIn("DRAFT — OWNER REVIEW REQUIRED — NOT SENT", draft_text)
        self.assertIn("I am not assuming a fit", draft_text)
        self.assertIn("This local draft has not been sent", draft_text)
        draft.write_text("tampered", encoding="utf-8")
        attention = vision_sales_status(self.sales)
        self.assertEqual(attention["status"], "attention")
        self.assertEqual(attention["research"]["integrity_failures"], 1)
        with self.assertRaisesRegex(RuntimeError, "outreach_conflict"):
            create_vision_prospect_drafts(self.sales)

    def test_founding_pilot_packages_are_claim_safe_idempotent_and_integrity_checked(self):
        import_vision_prospects(self._write_prospect_csv(), self.sales)
        create_vision_prospect_drafts(self.sales)
        drafts = self.sales / "research" / "outreach-drafts"
        drafts_before = {path.name: path.read_bytes() for path in drafts.glob("*.txt")}

        first = create_vision_founding_pilot_packages(self.sales)
        second = create_vision_founding_pilot_packages(self.sales)
        status = vision_sales_status(self.sales)

        self.assertEqual(
            first["contract"],
            "local-company.supermega-vision-founding-pilot-packages.v1",
        )
        self.assertEqual(first["packages_created"], 2)
        self.assertEqual(first["receipts_created"], 2)
        self.assertEqual(second["packages_created"], 0)
        self.assertEqual(second["packages_replayed"], 2)
        self.assertEqual(first["controls"]["external_sends"], 0)
        self.assertEqual(first["controls"]["pricing_commitments"], 0)
        self.assertEqual(status["research"]["founding_pilot_packages_ready"], 2)
        self.assertIn("nothing has been sent, priced", status["next_action"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in drafts.glob("*.txt")},
            drafts_before,
        )

        package = next((self.sales / "research" / "founding-pilot-packages").glob("*.txt"))
        text = package.read_text(encoding="utf-8")
        self.assertIn("We do not currently claim validated accuracy", text)
        self.assertIn("price remain uncommitted", text)
        self.assertIn("Nothing has been sent, sold, booked, or collected", text)
        self.assertNotIn("reports held-out accuracy", text)
        package.write_text("tampered", encoding="utf-8")
        self.assertEqual(vision_sales_status(self.sales)["status"], "attention")
        with self.assertRaisesRegex(RuntimeError, "package_conflict"):
            create_vision_founding_pilot_packages(self.sales)

    def test_interactive_intake_collects_locally_without_command_argument_data(self):
        answers = iter([
            "Mya", "mya@example.com", "Example Works",
            "Review an owned application screen before release", "windows",
            "6", "10", "30", "", "yes", "y", "yes",
        ])

        result = _interactive_vision_sales_intake(self.sales, input_fn=lambda prompt: next(answers))

        self.assertEqual(result["status"], "created")
        event = json.loads((self.sales / "inbox" / result["inbox_file"]).read_text(encoding="utf-8"))
        self.assertEqual(event["record"]["email"], "mya@example.com")
        self.assertEqual(event["record"]["raw"]["vision"]["labor_hourly_usd"], 0)
        self.assertEqual(result["controls"]["network_requests"], 0)
        self.assertEqual(result["controls"]["external_sends"], 0)

    def test_interactive_intake_fails_closed_on_cancel_or_invalid_answer(self):
        def cancelled(prompt):
            raise EOFError

        with self.assertRaisesRegex(ValueError, "vision_sales_intake_cancelled"):
            _interactive_vision_sales_intake(self.sales, input_fn=cancelled)
        self.assertFalse((self.sales / "inbox").exists())

        answers = iter([
            "Mya", "mya@example.com", "Example Works", "Review a screen", "windows",
            "6", "10", "30", "8", "maybe",
        ])
        with self.assertRaisesRegex(ValueError, "screenshot_rights_must_be_yes_or_no"):
            _interactive_vision_sales_intake(self.sales, input_fn=lambda prompt: next(answers))
        self.assertFalse((self.sales / "inbox").exists())

    def _write_intake(self, **overrides):
        prospect = {
            "name": "Mya",
            "email": "mya@example.com",
            "company": "Example Works",
            "goal": "Review an owned application screen before release",
            "platform": "windows",
            "state_count": 6,
            "weekly_runs": 10,
            "minutes_per_run": 30,
            "labor_hourly_usd": 8,
            "screenshot_rights": True,
            "human_fallback": True,
            "observation_only": True,
        }
        prospect.update(overrides)
        source = self.root / "prospect.json"
        source.write_text(json.dumps(prospect), encoding="utf-8")
        return source

    def test_manual_intake_creates_one_worker_compatible_idempotent_event(self):
        source = self._write_intake()
        original = source.read_bytes()

        first = create_vision_sales_intake(source, self.sales)
        second = create_vision_sales_intake(source, self.sales)

        self.assertEqual(first["contract"], "local-company.supermega-vision-sales-intake.v1")
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "replayed")
        self.assertEqual(first["lead_id"], second["lead_id"])
        self.assertEqual(first["controls"]["external_sends"], 0)
        self.assertEqual(first["controls"]["local_files_created"], 1)
        self.assertEqual(second["controls"]["local_files_created"], 0)
        event_path = self.sales / "inbox" / first["inbox_file"]
        event = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "supermega.contact.created")
        self.assertEqual(event["record"]["workflow"], "vision")
        self.assertEqual(event["record"]["lead_id"], first["lead_id"])
        self.assertEqual(event["record"]["raw"]["vision"]["state_count"], 6)
        self.assertEqual(source.read_bytes(), original)

    def test_manual_intake_rejects_invalid_or_conflicting_input(self):
        source = self._write_intake(email="not-an-email")
        with self.assertRaisesRegex(ValueError, "email_invalid"):
            create_vision_sales_intake(source, self.sales)

        source = self._write_intake(minutes_per_run=float("nan"))
        with self.assertRaisesRegex(ValueError, "minutes_per_run_invalid"):
            create_vision_sales_intake(source, self.sales)

        source = self._write_intake()
        created = create_vision_sales_intake(source, self.sales)
        destination = self.sales / "inbox" / created["inbox_file"]
        destination.write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "intake_conflict"):
            create_vision_sales_intake(source, self.sales)

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
        self.assertEqual(result["research"], {
            "researched_unsent_unqualified": 0,
            "outreach_drafts_ready": 0,
            "founding_pilot_packages_ready": 0,
            "integrity_failures": 0,
            "value_label": "Research only; not a lead, proposal, booked revenue, or collected revenue.",
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
