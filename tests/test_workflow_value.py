from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.cli import _interactive_workflow_value, parser
from local_company.computer_use import WORKFLOW_SCHEMA, seal_workflow, workflow_root
from local_company.workflow_value import (
    OPPORTUNITY_CONFIRMATION,
    bind_workflow_opportunity,
    create_workflow_opportunity,
    create_workflow_value_pack,
    list_workflow_opportunities,
    next_workflow_opportunity,
    workflow_value_status,
)
from scripts.local_ai import explain, translate


def workflow_payload(title: str = "Completed locally") -> dict[str, object]:
    return seal_workflow({
        "schema": WORKFLOW_SCHEMA,
        "name": "invoice-entry",
        "createdAt": "2026-09-03T00:00:00+00:00",
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
                "processName": "business-app.exe",
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
        "expectedFinalWindowTitleContains": title,
        "expectedFinalControlTextContains": None,
    })


def store_workflow(home: Path, payload: dict[str, object]) -> Path:
    path = workflow_root(home, create=True) / "invoice-entry" / "workflow.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def create_opportunity(
    home: Path,
    *,
    currency: str = "USD",
    task: str = "Mark a verified local invoice as completed",
    environment: str = "local",
    machine_checkable_outcome: bool = True,
    external_effect_risk: str = "none",
    credentials_required: bool = False,
    setup_price: float = 500,
) -> dict[str, object]:
    return create_workflow_opportunity(
        home,
        "invoice-processing",
        project="SuperMega",
        task=task,
        application="Business App",
        observed_runs=3,
        observed_human_minutes_total=30,
        runs_per_week=6,
        observed_errors=1,
        observed_error_cost_total=30,
        operator_value_per_hour=30,
        currency=currency,
        machine_checkable_outcome=machine_checkable_outcome,
        outcome_label="Invoice status is Completed locally",
        environment=environment,
        external_effect_risk=external_effect_risk,
        credentials_required=credentials_required,
        setup_price=setup_price,
        monthly_support_price=50,
        delivery_hours=10,
        delivery_cost_per_hour=20,
        monthly_support_hours=1,
        confirmation=OPPORTUNITY_CONFIRMATION,
    )


def passing_pilot(
    workflow: dict[str, object],
    *,
    total_minutes: float = 30,
    net_minutes: float = 195.5,
) -> dict[str, object]:
    value_gate = net_minutes > 0
    return {
        "status": "passed" if value_gate else "collecting",
        "workflowSha256": workflow["workflowSha256"],
        "reliabilityGatePassed": True,
        "valueGatePassed": value_gate,
        "promotionGatePassed": value_gate,
        "consecutivePassingRuns": 20,
        "requiredConsecutivePassingRuns": 20,
        "baseline": {
            "observedRuns": 3,
            "observedHumanMinutesTotal": total_minutes,
            "runsPerWeek": 6,
            "observedErrors": 1,
            "observedErrorCostTotal": 30,
        },
        "outcome": {"label": "Invoice status is Completed locally"},
        "metrics": {
            "verifiedNetMinutesSaved": net_minutes,
            "verifiedPromotionWindowRunCount": 20,
            "verifiedPromotionWindowNetMinutesSaved": net_minutes,
            "meanVerifiedNetMinutesSavedPerRun": net_minutes / 20,
        },
        "nextAction": (
            "package_owner_review_offer_from_verified_pilot"
            if value_gate
            else "redesign_or_reject_nonpositive_savings_workflow"
        ),
        "nextCommand": "local-ai.cmd automate pilot-status invoice-entry",
    }


class WorkflowValueTests(unittest.TestCase):
    def test_qualified_scenario_calculates_mutual_value_but_requires_pilot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow = workflow_payload()
            store_workflow(home, workflow)
            created = create_opportunity(home)
            economics = created["economics"]
            self.assertTrue(economics["scenarioQualified"])
            self.assertEqual(economics["monthlyGrossWorkflowValue"], 390)
            self.assertEqual(economics["customerPaybackMonths"], 1.47)
            self.assertEqual(economics["customerFirstYearNetValue"], 3580)
            self.assertEqual(economics["superMegaFirstYearGrossProfit"], 660)
            self.assertEqual(economics["superMegaFirstYearGrossMarginPercent"], 60)
            self.assertFalse(economics["demandProven"])

            opportunity_id = str(created["opportunityId"])
            unbound = workflow_value_status(home, opportunity_id)
            self.assertEqual(unbound["bindingStatus"], "not_bound")
            self.assertFalse(unbound["offerCandidate"])
            bind_workflow_opportunity(home, opportunity_id, "invoice-entry")
            status = workflow_value_status(home, opportunity_id)
            self.assertEqual(status["pilotStatus"], "not_started")
            self.assertFalse(status["pilotQualified"])
            self.assertFalse(status["customerQuoteAuthorized"])

    def test_only_matching_twenty_run_pilot_unlocks_owner_review_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow = workflow_payload()
            store_workflow(home, workflow)
            opportunity_id = str(create_opportunity(
                home, setup_price=300,
            )["opportunityId"])
            bind_workflow_opportunity(home, opportunity_id, "invoice-entry")

            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=passing_pilot(workflow),
            ):
                status = workflow_value_status(home, opportunity_id)
            self.assertEqual(status["status"], "offer_candidate")
            self.assertTrue(status["pilotOpportunityEvidenceMatch"])
            self.assertTrue(status["pilotQualified"])
            self.assertTrue(status["offerCandidate"])
            self.assertTrue(status["measuredEconomics"]["qualified"])
            self.assertEqual(
                status["measuredEconomics"]["monthlyMeasuredErrorValue"], 0,
            )
            self.assertFalse(
                status["measuredEconomics"]["measuredErrorValueIncluded"],
            )
            self.assertFalse(status["customerQuoteAuthorized"])
            self.assertFalse(status["paidDemandProven"])

            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=passing_pilot(workflow, total_minutes=3),
            ):
                mismatch = workflow_value_status(home, opportunity_id)
            self.assertFalse(mismatch["pilotOpportunityEvidenceMatch"])
            self.assertFalse(mismatch["offerCandidate"])
            self.assertEqual(
                mismatch["nextAction"],
                "review_pilot_opportunity_evidence_mismatch",
            )

    def test_nonpositive_missing_or_unviable_measured_value_never_offers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow = workflow_payload()
            store_workflow(home, workflow)
            opportunity_id = str(create_opportunity(home)["opportunityId"])
            bind_workflow_opportunity(home, opportunity_id, "invoice-entry")

            for net_minutes in (0, -1):
                with self.subTest(net_minutes=net_minutes), patch(
                    "local_company.workflow_value.workflow_pilot_status",
                    return_value=passing_pilot(
                        workflow, net_minutes=net_minutes,
                    ),
                ):
                    status = workflow_value_status(home, opportunity_id)
                self.assertFalse(status["pilotQualified"])
                self.assertFalse(status["offerCandidate"])
                self.assertFalse(status["pilotEvidence"]["valueGatePassed"])
                self.assertFalse(status["measuredEconomics"]["qualified"])
                self.assertEqual(
                    status["nextAction"],
                    "redesign_or_reject_nonpositive_savings_workflow",
                )

            missing = passing_pilot(workflow)
            missing["metrics"].pop("meanVerifiedNetMinutesSavedPerRun")
            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=missing,
            ):
                status = workflow_value_status(home, opportunity_id)
            self.assertIsNone(status["measuredEconomics"])
            self.assertFalse(status["pilotQualified"])
            self.assertFalse(status["offerCandidate"])
            self.assertEqual(
                status["nextAction"],
                "inspect_incomplete_measured_value_evidence",
            )

            nonfinite = passing_pilot(workflow)
            nonfinite["metrics"]["verifiedPromotionWindowNetMinutesSaved"] = float("nan")
            nonfinite["metrics"]["meanVerifiedNetMinutesSavedPerRun"] = float("nan")
            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=nonfinite,
            ):
                status = workflow_value_status(home, opportunity_id)
            self.assertIsNone(status["measuredEconomics"])
            self.assertFalse(status["pilotQualified"])
            self.assertFalse(status["offerCandidate"])

            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=passing_pilot(workflow),
            ):
                unviable = workflow_value_status(home, opportunity_id)
            self.assertTrue(unviable["pilotQualified"])
            self.assertFalse(unviable["measuredEconomics"]["qualified"])
            self.assertFalse(unviable["offerCandidate"])
            self.assertEqual(
                unviable["nextAction"],
                "repair_measured_value_or_pricing_gates",
            )

    def test_unsafe_or_unverifiable_work_never_qualifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            created = create_opportunity(
                Path(directory),
                environment="production",
                machine_checkable_outcome=False,
                external_effect_risk="irreversible",
                credentials_required=True,
            )
            economics = created["economics"]
            self.assertFalse(economics["scenarioQualified"])
            for gate in (
                "localOrTestEnvironment",
                "machineCheckableOutcome",
                "noExternalEffectRisk",
                "noCredentialsRequired",
            ):
                self.assertFalse(economics["gates"][gate])
            self.assertEqual(created["nextAction"], "repair_value_or_safety_gates")

    def test_tampering_workflow_change_and_invalid_pilot_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow_path = store_workflow(home, workflow_payload())
            opportunity_id = str(create_opportunity(
                home, setup_price=300,
            )["opportunityId"])
            bind_workflow_opportunity(home, opportunity_id, "invoice-entry")

            pilot_path = workflow_path.parent / "pilot" / "pilot.json"
            pilot_path.parent.mkdir()
            pilot_path.write_text("{}", encoding="utf-8")
            invalid = workflow_value_status(home, opportunity_id)
            self.assertEqual(invalid["pilotStatus"], "invalid")
            self.assertEqual(invalid["nextAction"], "inspect_invalid_pilot_evidence")

            workflow_path.write_text(
                json.dumps(workflow_payload("A different outcome")), encoding="utf-8",
            )
            changed = workflow_value_status(home, opportunity_id)
            self.assertEqual(changed["bindingStatus"], "workflow_changed")
            self.assertFalse(changed["offerCandidate"])

            opportunity_path = (
                home / "workflow-value" / "opportunities" / f"{opportunity_id}.json"
            )
            opportunity = json.loads(opportunity_path.read_text(encoding="utf-8"))
            opportunity["pricingScenario"]["setupPrice"] = 1
            opportunity_path.write_text(json.dumps(opportunity), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workflow_value_seal_mismatch"):
                workflow_value_status(home, opportunity_id)

    def test_ranking_blocks_mixed_currencies_and_filters_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            usd = create_opportunity(home, currency="USD")
            eur = create_opportunity(home, currency="EUR")
            listed = list_workflow_opportunities(home, project="SuperMega")
            self.assertEqual(listed["count"], 2)
            mixed = next_workflow_opportunity(home, project="SuperMega")
            self.assertEqual(mixed["status"], "blocked")
            self.assertEqual(mixed["reason"], "mixed_currencies_not_comparable")
            explicit = next_workflow_opportunity(
                home, project="SuperMega", currency="USD",
            )
            self.assertEqual(
                explicit["candidate"]["opportunityId"], usd["opportunityId"],
            )
            self.assertNotEqual(usd["opportunityId"], eur["opportunityId"])

    def test_pack_is_local_hash_bound_and_explicitly_not_a_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workflow = workflow_payload()
            store_workflow(home, workflow)
            opportunity_id = str(create_opportunity(
                home, setup_price=300,
            )["opportunityId"])
            bind_workflow_opportunity(home, opportunity_id, "invoice-entry")
            with patch(
                "local_company.workflow_value.workflow_pilot_status",
                return_value=passing_pilot(workflow),
            ):
                pack = create_workflow_value_pack(home, opportunity_id)
            brief_path = Path(str(pack["briefPath"]))
            receipt_path = Path(str(pack["receiptPath"]))
            brief = brief_path.read_text(encoding="utf-8")
            self.assertIn("not a market quote", brief)
            self.assertIn("Setup price: 300.0 USD", brief)
            self.assertIn("Monthly support price: 50.0 USD", brief)
            self.assertIn("Measured economics qualified: `true`", brief)
            self.assertIn("Measured error value included: `false`", brief)
            self.assertIn("Paid demand proven: `false`", brief)
            self.assertIn("Customer quote authorized: `false`", brief)
            self.assertNotIn(str(home), brief)
            self.assertEqual(
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                pack["receiptSha256"],
            )
            self.assertFalse(pack["externalActionAuthorized"])
            self.assertFalse(pack["customerContactPerformed"])

    def test_pack_escapes_untrusted_markdown_in_observed_task_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            opportunity_id = str(create_opportunity(
                home,
                task="![remote](https://example.com/pixel)",
            )["opportunityId"])
            pack = create_workflow_value_pack(home, opportunity_id)
            brief = Path(str(pack["briefPath"])).read_text(encoding="utf-8")
            self.assertNotIn("![remote](https://", brief)
            self.assertIn(
                r"\!\[remote\]\(https://example\.com/pixel\)", brief,
            )

    def test_friendly_commands_are_model_free_and_parser_backed(self) -> None:
        action = translate(["value"])
        self.assertEqual(action.command, ("computer", "value-next"))
        self.assertFalse(explain(action)["effects"]["modelMayRun"])
        self.assertEqual(
            translate(["value", "status", "a" * 12]).command,
            ("computer", "value-status", "a" * 12),
        )
        self.assertTrue(translate(["value", "pack", "a" * 12]).local_state_may_change)
        self.assertEqual(
            translate(["value", "add"]).command,
            ("computer", "value-add-interactive"),
        )
        with patch.dict(os.environ, {"SUPERMEGA_PROJECT_SHORTCUTS_ENABLED": "1"}):
            self.assertEqual(
                translate(["supermega", "value"]).command,
                ("computer", "value-next", "--project", "SuperMega"),
            )
        parsed = parser().parse_args([
            "computer", "value-next", "--project", "SuperMega", "--currency", "USD",
        ])
        self.assertEqual(parsed.computer_command, "value-next")
        self.assertEqual(parsed.project, "SuperMega")

    def test_guided_value_capture_writes_the_same_verified_scenario(self) -> None:
        answers = iter([
            "invoice-processing",
            "SuperMega",
            "Mark a verified local invoice as completed",
            "Business App",
            "3",
            "30",
            "6",
            "1",
            "30",
            "30",
            "USD",
            "yes",
            "Invoice status is Completed locally",
            "",
            "",
            "",
            "500",
            "50",
            "10",
            "20",
            "1",
            "",
            "",
            OPPORTUNITY_CONFIRMATION,
        ])
        with tempfile.TemporaryDirectory() as directory:
            result = _interactive_workflow_value(
                Path(directory), input_fn=lambda _prompt: next(answers),
            )
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["economics"]["scenarioQualified"])
        self.assertFalse(result["customerQuoteAuthorized"])


if __name__ == "__main__":
    unittest.main()
