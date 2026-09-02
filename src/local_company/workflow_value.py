from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .computer_use import load_workflow
from .workflow_pilot import workflow_pilot_status


OPPORTUNITY_SCHEMA = "local-company.workflow-value-opportunity.v1"
BINDING_SCHEMA = "local-company.workflow-value-binding.v1"
STATUS_SCHEMA = "local-company.workflow-value-status.v1"
PACK_SCHEMA = "local-company.workflow-value-pack.v1"
OPPORTUNITY_CONFIRMATION = "RECORD MEASURED WORKFLOW OPPORTUNITY"
WEEKS_PER_MONTH = 52 / 12
MAXIMUM_JSON_BYTES = 256 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_IDENTIFIER = re.compile(r"^[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _digest(schema: str, value: object) -> str:
    return hashlib.sha256(schema.encode("ascii") + b"\0" + _canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: object, reason: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(reason)
    observed = float(value)
    if not math.isfinite(observed) or not minimum <= observed <= maximum:
        raise ValueError(reason)
    return observed


def _text(value: object, reason: str, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(reason)
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or any(ord(char) < 32 for char in cleaned):
        raise ValueError(reason)
    return cleaned


def _safe_name(value: str) -> str:
    cleaned = value.strip().lower()
    if _NAME.fullmatch(cleaned) is None:
        raise ValueError("workflow_value_name_invalid")
    return cleaned


def _safe_identifier(value: str) -> str:
    cleaned = value.strip().lower()
    if _IDENTIFIER.fullmatch(cleaned) is None:
        raise ValueError("workflow_value_opportunity_id_invalid")
    return cleaned


def _markdown_text(value: object) -> str:
    """Render owner/customer text without activating links, images, or HTML."""
    return re.sub(r"([\\`*_{}\[\]<>#+\-.!|()])", r"\\\1", str(value))


def _root(company_home: Path) -> Path:
    return company_home.resolve() / "workflow-value"


def _opportunity_path(company_home: Path, opportunity_id: str) -> Path:
    return _root(company_home) / "opportunities" / f"{_safe_identifier(opportunity_id)}.json"


def _binding_path(company_home: Path, opportunity_id: str) -> Path:
    return _root(company_home) / "bindings" / f"{_safe_identifier(opportunity_id)}.json"


def _exclusive_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path, reason: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(reason)
    raw = path.read_bytes()
    if not raw or len(raw) > MAXIMUM_JSON_BYTES:
        raise ValueError(reason)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(reason) from error
    if not isinstance(value, dict):
        raise ValueError(reason)
    return value


def _seal_opportunity(payload: dict[str, object]) -> dict[str, object]:
    unsealed = dict(payload)
    unsealed.pop("opportunitySha256", None)
    sealed = {**unsealed, "opportunitySha256": _digest(OPPORTUNITY_SCHEMA, unsealed)}
    return _validate_opportunity(sealed)


def _validate_opportunity(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "opportunityId", "name", "project", "task", "application",
        "createdAt", "currency", "baseline", "technical", "pricingScenario",
        "decisionPolicy", "opportunitySha256",
    }:
        raise ValueError("workflow_value_opportunity_invalid")
    if value.get("schema") != OPPORTUNITY_SCHEMA:
        raise ValueError("workflow_value_schema_invalid")
    opportunity_id = value.get("opportunityId")
    if not isinstance(opportunity_id, str) or _IDENTIFIER.fullmatch(opportunity_id) is None:
        raise ValueError("workflow_value_opportunity_id_invalid")
    name = value.get("name")
    if not isinstance(name, str) or _safe_name(name) != name:
        raise ValueError("workflow_value_name_invalid")
    _text(value.get("project"), "workflow_value_project_invalid", 120)
    _text(value.get("task"), "workflow_value_task_invalid", 500)
    _text(value.get("application"), "workflow_value_application_invalid", 200)
    if not isinstance(value.get("createdAt"), str) or len(str(value["createdAt"])) > 64:
        raise ValueError("workflow_value_created_at_invalid")
    currency = value.get("currency")
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        raise ValueError("workflow_value_currency_invalid")

    baseline = value.get("baseline")
    if not isinstance(baseline, dict) or set(baseline) != {
        "source", "humanObservationConfirmed", "observedRuns",
        "observedHumanMinutesTotal", "runsPerWeek", "observedErrors",
        "observedErrorCostTotal", "operatorValuePerHour",
    }:
        raise ValueError("workflow_value_baseline_invalid")
    if baseline.get("source") != "owner_observed" or baseline.get("humanObservationConfirmed") is not True:
        raise ValueError("workflow_value_baseline_source_invalid")
    observed_runs = baseline.get("observedRuns")
    observed_errors = baseline.get("observedErrors")
    if type(observed_runs) is not int or not 3 <= observed_runs <= 1000:
        raise ValueError("workflow_value_observed_runs_invalid")
    if type(observed_errors) is not int or not 0 <= observed_errors <= observed_runs:
        raise ValueError("workflow_value_observed_errors_invalid")
    _number(
        baseline.get("observedHumanMinutesTotal"),
        "workflow_value_human_minutes_invalid", 0.1, 1_000_000,
    )
    _number(
        baseline.get("runsPerWeek"), "workflow_value_runs_per_week_invalid", 0.01, 100_000,
    )
    _number(
        baseline.get("observedErrorCostTotal"),
        "workflow_value_error_cost_invalid", 0, 1_000_000_000,
    )
    _number(
        baseline.get("operatorValuePerHour"),
        "workflow_value_operator_value_invalid", 0.01, 10_000_000,
    )

    technical = value.get("technical")
    if not isinstance(technical, dict) or set(technical) != {
        "environment", "machineCheckableOutcome", "outcomeLabel",
        "externalEffectRisk", "credentialsRequired",
    }:
        raise ValueError("workflow_value_technical_invalid")
    if technical.get("environment") not in {"local", "test", "staging", "production"}:
        raise ValueError("workflow_value_environment_invalid")
    if type(technical.get("machineCheckableOutcome")) is not bool:
        raise ValueError("workflow_value_machine_outcome_invalid")
    _text(technical.get("outcomeLabel"), "workflow_value_outcome_invalid", 500)
    if technical.get("externalEffectRisk") not in {"none", "reversible", "irreversible"}:
        raise ValueError("workflow_value_external_risk_invalid")
    if type(technical.get("credentialsRequired")) is not bool:
        raise ValueError("workflow_value_credentials_invalid")

    scenario = value.get("pricingScenario")
    if not isinstance(scenario, dict) or set(scenario) != {
        "setupPrice", "monthlySupportPrice", "deliveryHours",
        "deliveryCostPerHour", "monthlySupportHours",
    }:
        raise ValueError("workflow_value_pricing_scenario_invalid")
    for key, reason, maximum in (
        ("setupPrice", "workflow_value_setup_price_invalid", 1_000_000_000),
        ("monthlySupportPrice", "workflow_value_support_price_invalid", 100_000_000),
        ("deliveryHours", "workflow_value_delivery_hours_invalid", 10_000),
        ("deliveryCostPerHour", "workflow_value_delivery_cost_invalid", 10_000_000),
        ("monthlySupportHours", "workflow_value_support_hours_invalid", 1_000),
    ):
        _number(scenario.get(key), reason, 0, maximum)

    policy = value.get("decisionPolicy")
    if not isinstance(policy, dict) or set(policy) != {
        "maximumCustomerPaybackMonths", "minimumSuperMegaGrossMarginPercent",
    }:
        raise ValueError("workflow_value_decision_policy_invalid")
    _number(
        policy.get("maximumCustomerPaybackMonths"),
        "workflow_value_payback_policy_invalid", 1, 60,
    )
    _number(
        policy.get("minimumSuperMegaGrossMarginPercent"),
        "workflow_value_margin_policy_invalid", 0, 100,
    )
    recorded = value.get("opportunitySha256")
    if not isinstance(recorded, str) or _SHA256.fullmatch(recorded) is None:
        raise ValueError("workflow_value_seal_invalid")
    unsealed = dict(value)
    unsealed.pop("opportunitySha256")
    if _digest(OPPORTUNITY_SCHEMA, unsealed) != recorded:
        raise ValueError("workflow_value_seal_mismatch")
    expected_id = _digest(OPPORTUNITY_SCHEMA, {
        key: item for key, item in unsealed.items()
        if key not in {"opportunityId", "createdAt"}
    })[:12]
    if opportunity_id != expected_id:
        raise ValueError("workflow_value_opportunity_id_mismatch")
    return value


def _load_opportunity(company_home: Path, opportunity_id: str) -> dict[str, object]:
    return _validate_opportunity(_read_json(
        _opportunity_path(company_home, opportunity_id),
        "workflow_value_opportunity_not_found_or_invalid",
    ))


def _economics(opportunity: dict[str, object]) -> dict[str, object]:
    baseline = opportunity["baseline"]
    scenario = opportunity["pricingScenario"]
    policy = opportunity["decisionPolicy"]
    observed_runs = int(baseline["observedRuns"])
    mean_minutes = float(baseline["observedHumanMinutesTotal"]) / observed_runs
    runs_per_week = float(baseline["runsPerWeek"])
    monthly_runs = runs_per_week * WEEKS_PER_MONTH
    manual_hours_month = mean_minutes * monthly_runs / 60
    labor_value_month = manual_hours_month * float(baseline["operatorValuePerHour"])
    errors = int(baseline["observedErrors"])
    error_rate = errors / observed_runs
    mean_error_cost = (
        float(baseline["observedErrorCostTotal"]) / errors if errors else 0.0
    )
    error_exposure_month = error_rate * monthly_runs * mean_error_cost
    gross_workflow_value_month = labor_value_month + error_exposure_month
    support_price = float(scenario["monthlySupportPrice"])
    setup_price = float(scenario["setupPrice"])
    customer_monthly_value_after_support = gross_workflow_value_month - support_price
    payback_months = (
        setup_price / customer_monthly_value_after_support
        if customer_monthly_value_after_support > 0 else None
    )
    customer_first_year_net = (
        gross_workflow_value_month * 12 - setup_price - support_price * 12
    )
    setup_delivery_cost = (
        float(scenario["deliveryHours"]) * float(scenario["deliveryCostPerHour"])
    )
    annual_support_cost = (
        float(scenario["monthlySupportHours"])
        * float(scenario["deliveryCostPerHour"]) * 12
    )
    first_year_revenue = setup_price + support_price * 12
    first_year_cost = setup_delivery_cost + annual_support_cost
    first_year_gross_profit = first_year_revenue - first_year_cost
    gross_margin_percent = (
        first_year_gross_profit / first_year_revenue * 100
        if first_year_revenue > 0 else None
    )
    technical = opportunity["technical"]
    gates = {
        "ownerObservedAtLeastThreeRuns": observed_runs >= 3,
        "localOrTestEnvironment": technical["environment"] in {"local", "test"},
        "machineCheckableOutcome": technical["machineCheckableOutcome"] is True,
        "noExternalEffectRisk": technical["externalEffectRisk"] == "none",
        "noCredentialsRequired": technical["credentialsRequired"] is False,
        "positiveCustomerFirstYearNetValue": customer_first_year_net > 0,
        "customerPaybackWithinPolicy": (
            payback_months is not None
            and payback_months <= float(policy["maximumCustomerPaybackMonths"])
        ),
        "positiveSuperMegaFirstYearGrossProfit": first_year_gross_profit > 0,
        "superMegaGrossMarginAtOrAbovePolicy": (
            gross_margin_percent is not None
            and gross_margin_percent
            >= float(policy["minimumSuperMegaGrossMarginPercent"])
        ),
    }
    return {
        "currency": opportunity["currency"],
        "meanHumanMinutesPerRun": round(mean_minutes, 4),
        "monthlyRuns": round(monthly_runs, 4),
        "manualHoursPerMonth": round(manual_hours_month, 4),
        "monthlyLaborCapacityValue": round(labor_value_month, 2),
        "baselineObservedErrorRate": round(error_rate, 6),
        "meanObservedErrorCost": round(mean_error_cost, 2),
        "monthlyObservedErrorExposure": round(error_exposure_month, 2),
        "monthlyGrossWorkflowValue": round(gross_workflow_value_month, 2),
        "setupPrice": round(setup_price, 2),
        "monthlySupportPrice": round(support_price, 2),
        "deliveryHours": round(float(scenario["deliveryHours"]), 2),
        "deliveryCostPerHour": round(float(scenario["deliveryCostPerHour"]), 2),
        "monthlySupportHours": round(float(scenario["monthlySupportHours"]), 2),
        "customerMonthlyValueAfterSupport": round(customer_monthly_value_after_support, 2),
        "customerPaybackMonths": round(payback_months, 2) if payback_months is not None else None,
        "customerFirstYearNetValue": round(customer_first_year_net, 2),
        "superMegaFirstYearRevenue": round(first_year_revenue, 2),
        "superMegaFirstYearDeliveryCost": round(first_year_cost, 2),
        "superMegaFirstYearGrossProfit": round(first_year_gross_profit, 2),
        "superMegaFirstYearGrossMarginPercent": (
            round(gross_margin_percent, 2) if gross_margin_percent is not None else None
        ),
        "mutualFirstYearNetValue": round(
            customer_first_year_net + first_year_gross_profit, 2,
        ),
        "gates": gates,
        "scenarioQualified": all(gates.values()),
        "pricingBasis": "owner_supplied_scenario_not_market_quote",
        "demandProven": False,
    }


def _measured_economics(
    opportunity: dict[str, object],
    pilot: dict[str, object],
) -> dict[str, object] | None:
    metrics = pilot.get("metrics")
    if not isinstance(metrics, dict):
        return None
    run_count = metrics.get("verifiedPromotionWindowRunCount")
    net_minutes = metrics.get("verifiedPromotionWindowNetMinutesSaved")
    mean_net_minutes = metrics.get("meanVerifiedNetMinutesSavedPerRun")
    required_runs = pilot.get("requiredConsecutivePassingRuns")
    if (
        type(run_count) is not int
        or run_count <= 0
        or type(required_runs) is not int
        or required_runs <= 0
        or run_count != required_runs
        or isinstance(net_minutes, bool)
        or not isinstance(net_minutes, (int, float))
        or not math.isfinite(float(net_minutes))
        or isinstance(mean_net_minutes, bool)
        or not isinstance(mean_net_minutes, (int, float))
        or not math.isfinite(float(mean_net_minutes))
        or not math.isclose(
            float(mean_net_minutes) * run_count,
            float(net_minutes),
            rel_tol=1e-6,
            abs_tol=0.01,
        )
    ):
        return None

    baseline = opportunity["baseline"]
    mean_baseline_minutes = (
        float(baseline["observedHumanMinutesTotal"])
        / int(baseline["observedRuns"])
    )
    if float(mean_net_minutes) > mean_baseline_minutes:
        return None
    scenario = opportunity["pricingScenario"]
    policy = opportunity["decisionPolicy"]
    monthly_runs = float(baseline["runsPerWeek"]) * WEEKS_PER_MONTH
    measured_labor_value_month = (
        float(mean_net_minutes) * monthly_runs / 60
        * float(baseline["operatorValuePerHour"])
    )
    support_price = float(scenario["monthlySupportPrice"])
    setup_price = float(scenario["setupPrice"])
    customer_monthly_value_after_support = measured_labor_value_month - support_price
    payback_months = (
        setup_price / customer_monthly_value_after_support
        if customer_monthly_value_after_support > 0 else None
    )
    customer_first_year_net = (
        measured_labor_value_month * 12 - setup_price - support_price * 12
    )
    setup_delivery_cost = (
        float(scenario["deliveryHours"]) * float(scenario["deliveryCostPerHour"])
    )
    annual_support_cost = (
        float(scenario["monthlySupportHours"])
        * float(scenario["deliveryCostPerHour"]) * 12
    )
    first_year_revenue = setup_price + support_price * 12
    first_year_cost = setup_delivery_cost + annual_support_cost
    first_year_gross_profit = first_year_revenue - first_year_cost
    gross_margin_percent = (
        first_year_gross_profit / first_year_revenue * 100
        if first_year_revenue > 0 else None
    )
    gates = {
        "reliabilityGatePassed": pilot.get("reliabilityGatePassed") is True,
        "valueGatePassed": pilot.get("valueGatePassed") is True,
        "positiveVerifiedPromotionWindowSavings": float(net_minutes) > 0,
        "positiveMeanVerifiedSavingsPerRun": float(mean_net_minutes) > 0,
        "positiveCustomerFirstYearNetValue": customer_first_year_net > 0,
        "customerPaybackWithinPolicy": (
            payback_months is not None
            and payback_months <= float(policy["maximumCustomerPaybackMonths"])
        ),
        "positiveSuperMegaFirstYearGrossProfit": first_year_gross_profit > 0,
        "superMegaGrossMarginAtOrAbovePolicy": (
            gross_margin_percent is not None
            and gross_margin_percent
            >= float(policy["minimumSuperMegaGrossMarginPercent"])
        ),
    }
    return {
        "currency": opportunity["currency"],
        "promotionWindowRuns": run_count,
        "verifiedPromotionWindowNetMinutesSaved": round(float(net_minutes), 4),
        "meanVerifiedNetMinutesSavedPerRun": round(float(mean_net_minutes), 4),
        "monthlyRuns": round(monthly_runs, 4),
        "monthlyMeasuredLaborCapacityValue": round(measured_labor_value_month, 2),
        "monthlyMeasuredErrorValue": 0.0,
        "measuredErrorValueIncluded": False,
        "monthlyGrossWorkflowValue": round(measured_labor_value_month, 2),
        "setupPrice": round(setup_price, 2),
        "monthlySupportPrice": round(support_price, 2),
        "customerMonthlyValueAfterSupport": round(customer_monthly_value_after_support, 2),
        "customerPaybackMonths": round(payback_months, 2) if payback_months is not None else None,
        "customerFirstYearNetValue": round(customer_first_year_net, 2),
        "superMegaFirstYearRevenue": round(first_year_revenue, 2),
        "superMegaFirstYearDeliveryCost": round(first_year_cost, 2),
        "superMegaFirstYearGrossProfit": round(first_year_gross_profit, 2),
        "superMegaFirstYearGrossMarginPercent": (
            round(gross_margin_percent, 2) if gross_margin_percent is not None else None
        ),
        "mutualFirstYearNetValue": round(
            customer_first_year_net + first_year_gross_profit, 2,
        ),
        "gates": gates,
        "qualified": all(gates.values()),
        "pricingBasis": (
            "verified_trailing_promotion_window_time_savings_"
            "with_owner_supplied_prices"
        ),
        "demandProven": False,
    }


def create_workflow_opportunity(
    company_home: Path,
    name: str,
    *,
    project: str,
    task: str,
    application: str,
    observed_runs: int,
    observed_human_minutes_total: float,
    runs_per_week: float,
    observed_errors: int,
    observed_error_cost_total: float,
    operator_value_per_hour: float,
    currency: str,
    machine_checkable_outcome: bool,
    outcome_label: str,
    environment: str,
    external_effect_risk: str,
    credentials_required: bool,
    setup_price: float,
    monthly_support_price: float,
    delivery_hours: float,
    delivery_cost_per_hour: float,
    monthly_support_hours: float,
    confirmation: str,
    maximum_customer_payback_months: float = 6,
    minimum_supermega_gross_margin_percent: float = 50,
) -> dict[str, object]:
    if confirmation != OPPORTUNITY_CONFIRMATION:
        raise ValueError("workflow_value_confirmation_invalid")
    safe_name = _safe_name(name)
    core = {
        "schema": OPPORTUNITY_SCHEMA,
        "name": safe_name,
        "project": _text(project, "workflow_value_project_invalid", 120),
        "task": _text(task, "workflow_value_task_invalid", 500),
        "application": _text(application, "workflow_value_application_invalid", 200),
        "currency": currency.strip().upper(),
        "baseline": {
            "source": "owner_observed",
            "humanObservationConfirmed": True,
            "observedRuns": observed_runs,
            "observedHumanMinutesTotal": observed_human_minutes_total,
            "runsPerWeek": runs_per_week,
            "observedErrors": observed_errors,
            "observedErrorCostTotal": observed_error_cost_total,
            "operatorValuePerHour": operator_value_per_hour,
        },
        "technical": {
            "environment": environment,
            "machineCheckableOutcome": machine_checkable_outcome,
            "outcomeLabel": _text(
                outcome_label, "workflow_value_outcome_invalid", 500,
            ),
            "externalEffectRisk": external_effect_risk,
            "credentialsRequired": credentials_required,
        },
        "pricingScenario": {
            "setupPrice": setup_price,
            "monthlySupportPrice": monthly_support_price,
            "deliveryHours": delivery_hours,
            "deliveryCostPerHour": delivery_cost_per_hour,
            "monthlySupportHours": monthly_support_hours,
        },
        "decisionPolicy": {
            "maximumCustomerPaybackMonths": maximum_customer_payback_months,
            "minimumSuperMegaGrossMarginPercent": minimum_supermega_gross_margin_percent,
        },
    }
    opportunity_id = _digest(OPPORTUNITY_SCHEMA, core)[:12]
    opportunity = _seal_opportunity({
        **core,
        "opportunityId": opportunity_id,
        "createdAt": _now(),
    })
    destination = _opportunity_path(company_home, opportunity_id)
    _exclusive_json(destination, opportunity)
    economics = _economics(opportunity)
    return {
        "schema": "local-company.workflow-value-create.v1",
        "status": "created",
        "opportunityId": opportunity_id,
        "name": safe_name,
        "project": opportunity["project"],
        "economics": economics,
        "nextAction": (
            "teach_and_bind_selected_workflow"
            if economics["scenarioQualified"] else "repair_value_or_safety_gates"
        ),
        "stateMutated": True,
        "modelCalled": False,
        "externalActionPerformed": False,
        "customerContactPerformed": False,
        "customerQuoteAuthorized": False,
        "paidDemandProven": False,
    }


def _seal_binding(payload: dict[str, object]) -> dict[str, object]:
    unsealed = dict(payload)
    unsealed.pop("bindingSha256", None)
    sealed = {**unsealed, "bindingSha256": _digest(BINDING_SCHEMA, unsealed)}
    return _validate_binding(sealed)


def _validate_binding(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "opportunityId", "opportunitySha256", "workflow",
        "workflowSha256", "boundAt", "bindingSha256",
    }:
        raise ValueError("workflow_value_binding_invalid")
    if value.get("schema") != BINDING_SCHEMA:
        raise ValueError("workflow_value_binding_schema_invalid")
    for key in ("opportunitySha256", "workflowSha256", "bindingSha256"):
        if not isinstance(value.get(key), str) or _SHA256.fullmatch(str(value[key])) is None:
            raise ValueError("workflow_value_binding_digest_invalid")
    if not isinstance(value.get("opportunityId"), str) or _IDENTIFIER.fullmatch(str(value["opportunityId"])) is None:
        raise ValueError("workflow_value_binding_id_invalid")
    _safe_name(str(value.get("workflow", "")))
    if not isinstance(value.get("boundAt"), str) or len(str(value["boundAt"])) > 64:
        raise ValueError("workflow_value_binding_time_invalid")
    unsealed = dict(value)
    recorded = str(unsealed.pop("bindingSha256"))
    if _digest(BINDING_SCHEMA, unsealed) != recorded:
        raise ValueError("workflow_value_binding_seal_mismatch")
    return value


def _load_binding(company_home: Path, opportunity_id: str) -> dict[str, object] | None:
    path = _binding_path(company_home, opportunity_id)
    if not path.exists():
        return None
    return _validate_binding(_read_json(path, "workflow_value_binding_invalid"))


def bind_workflow_opportunity(
    company_home: Path,
    opportunity_id: str,
    workflow_name: str,
) -> dict[str, object]:
    opportunity = _load_opportunity(company_home, opportunity_id)
    _path, workflow = load_workflow(company_home, workflow_name)
    if not (
        workflow.get("expectedFinalWindowTitleContains")
        or workflow.get("expectedFinalControlTextContains")
    ):
        raise ValueError("workflow_value_bound_workflow_outcome_missing")
    binding = _seal_binding({
        "schema": BINDING_SCHEMA,
        "opportunityId": opportunity["opportunityId"],
        "opportunitySha256": opportunity["opportunitySha256"],
        "workflow": workflow["name"],
        "workflowSha256": workflow["workflowSha256"],
        "boundAt": _now(),
    })
    _exclusive_json(_binding_path(company_home, opportunity_id), binding)
    return {
        "schema": "local-company.workflow-value-bind.v1",
        "status": "bound",
        "opportunityId": opportunity["opportunityId"],
        "workflow": workflow["name"],
        "workflowSha256": workflow["workflowSha256"],
        "bindingSha256": binding["bindingSha256"],
        "nextAction": "start_measured_workflow_pilot",
        "stateMutated": True,
        "modelCalled": False,
        "externalActionPerformed": False,
        "customerQuoteAuthorized": False,
    }


def workflow_value_status(company_home: Path, opportunity_id: str) -> dict[str, object]:
    opportunity = _load_opportunity(company_home, opportunity_id)
    economics = _economics(opportunity)
    binding = _load_binding(company_home, opportunity_id)
    binding_status = "not_bound"
    pilot: dict[str, object] | None = None
    pilot_status = "not_started"
    pilot_evidence_match = False
    pilot_qualified = False
    measured_economics: dict[str, object] | None = None
    if binding is not None:
        if (
            binding["opportunitySha256"] != opportunity["opportunitySha256"]
            or binding["opportunityId"] != opportunity["opportunityId"]
        ):
            binding_status = "invalid"
        else:
            try:
                workflow_path, workflow = load_workflow(
                    company_home, str(binding["workflow"]),
                )
            except ValueError:
                binding_status = "workflow_missing_or_invalid"
            else:
                if workflow["workflowSha256"] != binding["workflowSha256"]:
                    binding_status = "workflow_changed"
                else:
                    binding_status = "valid"
                    pilot_path = workflow_path.parent / "pilot" / "pilot.json"
                    try:
                        pilot = workflow_pilot_status(
                            company_home, str(binding["workflow"]),
                        )
                    except ValueError:
                        # Absence is a normal pre-pilot state. A present pilot
                        # that cannot be validated is an integrity failure and
                        # must never be presented as a fresh start.
                        pilot_status = "invalid" if pilot_path.exists() else "not_started"
                    else:
                        pilot_status = str(pilot.get("status", "invalid"))
                        pilot_baseline = pilot.get("baseline")
                        pilot_outcome = pilot.get("outcome")
                        opportunity_baseline = opportunity["baseline"]
                        pilot_evidence_match = (
                            isinstance(pilot_baseline, dict)
                            and isinstance(pilot_outcome, dict)
                            and pilot_baseline.get("observedRuns")
                            == opportunity_baseline["observedRuns"]
                            and math.isclose(
                                float(pilot_baseline.get("observedHumanMinutesTotal", -1)),
                                float(opportunity_baseline["observedHumanMinutesTotal"]),
                            )
                            and math.isclose(
                                float(pilot_baseline.get("runsPerWeek", -1)),
                                float(opportunity_baseline["runsPerWeek"]),
                            )
                            and pilot_baseline.get("observedErrors")
                            == opportunity_baseline["observedErrors"]
                            and math.isclose(
                                float(pilot_baseline.get("observedErrorCostTotal", -1)),
                                float(opportunity_baseline["observedErrorCostTotal"]),
                            )
                            and pilot_outcome.get("label")
                            == opportunity["technical"]["outcomeLabel"]
                        )
                        if pilot_evidence_match:
                            measured_economics = _measured_economics(
                                opportunity, pilot,
                            )
                        metrics = pilot.get("metrics")
                        verified_window_savings = (
                            metrics.get("verifiedPromotionWindowNetMinutesSaved")
                            if isinstance(metrics, dict) else None
                        )
                        pilot_qualified = (
                            pilot_status == "passed"
                            and pilot.get("reliabilityGatePassed") is True
                            and pilot.get("valueGatePassed") is True
                            and pilot.get("promotionGatePassed") is True
                            and pilot.get("workflowSha256") == binding["workflowSha256"]
                            and pilot_evidence_match
                            and isinstance(verified_window_savings, (int, float))
                            and not isinstance(verified_window_savings, bool)
                            and math.isfinite(float(verified_window_savings))
                            and float(verified_window_savings) > 0
                            and measured_economics is not None
                        )
    offer_candidate = (
        economics["scenarioQualified"] is True
        and pilot_qualified
        and measured_economics is not None
        and measured_economics["qualified"] is True
    )
    if economics["scenarioQualified"] is not True:
        next_action = "repair_value_or_safety_gates"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif binding_status == "not_bound":
        next_action = "teach_and_bind_selected_workflow"
        next_command = f"local-ai.cmd value bind {opportunity_id} WORKFLOW_NAME"
    elif binding_status != "valid":
        next_action = "review_and_rebind_changed_workflow"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif pilot_status == "not_started":
        next_action = "start_measured_workflow_pilot"
        next_command = f"local-ai.cmd automate pilot-start {binding['workflow']} ..."
    elif pilot_status == "invalid":
        next_action = "inspect_invalid_pilot_evidence"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif pilot_status == "passed" and not pilot_evidence_match:
        next_action = "review_pilot_opportunity_evidence_mismatch"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif (
        pilot_evidence_match
        and pilot is not None
        and pilot.get("reliabilityGatePassed") is True
        and pilot.get("valueGatePassed") is not True
    ):
        next_action = str(
            pilot.get(
                "nextAction",
                "redesign_or_reject_nonpositive_savings_workflow",
            )
        )
        next_command = str(
            pilot.get(
                "nextCommand",
                f"local-ai.cmd automate pilot-status {binding['workflow']}",
            )
        )
    elif (
        pilot_evidence_match
        and pilot is not None
        and pilot.get("reliabilityGatePassed") is True
        and measured_economics is None
    ):
        next_action = "inspect_incomplete_measured_value_evidence"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif (
        pilot_evidence_match
        and measured_economics is not None
        and measured_economics["qualified"] is not True
    ):
        next_action = "repair_measured_value_or_pricing_gates"
        next_command = f"local-ai.cmd value status {opportunity_id}"
    elif not pilot_qualified:
        next_action = str(pilot.get("nextAction", "continue_measured_workflow_pilot"))
        next_command = str(
            pilot.get(
                "nextCommand",
                f"local-ai.cmd automate pilot-status {binding['workflow']}",
            )
        )
    else:
        next_action = "create_owner_review_business_case"
        next_command = f"local-ai.cmd value pack {opportunity_id}"
    return {
        "schema": STATUS_SCHEMA,
        "status": "offer_candidate" if offer_candidate else "evidence_required",
        "opportunityId": opportunity["opportunityId"],
        "name": opportunity["name"],
        "project": opportunity["project"],
        "task": opportunity["task"],
        "application": opportunity["application"],
        "createdAt": opportunity["createdAt"],
        "opportunitySha256": opportunity["opportunitySha256"],
        "economics": economics,
        "measuredEconomics": measured_economics,
        "bindingStatus": binding_status,
        "workflow": binding.get("workflow") if binding else None,
        "workflowSha256": binding.get("workflowSha256") if binding else None,
        "pilotStatus": pilot_status,
        "pilotOpportunityEvidenceMatch": pilot_evidence_match,
        "pilotQualified": pilot_qualified,
        "pilotEvidence": {
            "promotionGatePassed": pilot.get("promotionGatePassed") if pilot else False,
            "reliabilityGatePassed": (
                pilot.get("reliabilityGatePassed") if pilot else False
            ),
            "valueGatePassed": pilot.get("valueGatePassed") if pilot else False,
            "consecutivePassingRuns": pilot.get("consecutivePassingRuns") if pilot else 0,
            "requiredConsecutivePassingRuns": (
                pilot.get("requiredConsecutivePassingRuns") if pilot else 20
            ),
            "verifiedNetMinutesSaved": (
                pilot.get("metrics", {}).get("verifiedNetMinutesSaved")
                if pilot and isinstance(pilot.get("metrics"), dict) else None
            ),
            "verifiedPromotionWindowNetMinutesSaved": (
                pilot.get("metrics", {}).get(
                    "verifiedPromotionWindowNetMinutesSaved",
                )
                if pilot and isinstance(pilot.get("metrics"), dict) else None
            ),
            "verifiedPromotionWindowRunCount": (
                pilot.get("metrics", {}).get(
                    "verifiedPromotionWindowRunCount",
                )
                if pilot and isinstance(pilot.get("metrics"), dict) else 0
            ),
            "meanVerifiedNetMinutesSavedPerRun": (
                pilot.get("metrics", {}).get(
                    "meanVerifiedNetMinutesSavedPerRun",
                )
                if pilot and isinstance(pilot.get("metrics"), dict) else None
            ),
        },
        "offerCandidate": offer_candidate,
        "customerQuoteAuthorized": False,
        "paidDemandProven": False,
        "externalActionAuthorized": False,
        "nextAction": next_action,
        "nextCommand": next_command,
        "modelCalled": False,
        "stateMutated": False,
        "externalActionPerformed": False,
    }


def list_workflow_opportunities(
    company_home: Path,
    *,
    project: str | None = None,
    currency: str | None = None,
) -> dict[str, object]:
    project_filter = _text(project, "workflow_value_project_invalid", 120) if project else None
    currency_filter = currency.strip().upper() if currency else None
    if currency_filter is not None and _CURRENCY.fullmatch(currency_filter) is None:
        raise ValueError("workflow_value_currency_invalid")
    items: list[dict[str, object]] = []
    source = _root(company_home) / "opportunities"
    if source.is_dir():
        for path in sorted(source.glob("*.json"))[:200]:
            try:
                opportunity = _validate_opportunity(_read_json(
                    path, "workflow_value_opportunity_invalid",
                ))
            except (OSError, UnicodeError, ValueError):
                items.append({"opportunityId": path.stem, "status": "invalid"})
                continue
            if project_filter is not None and opportunity["project"] != project_filter:
                continue
            if currency_filter is not None and opportunity["currency"] != currency_filter:
                continue
            items.append(workflow_value_status(
                company_home, str(opportunity["opportunityId"]),
            ))
    return {
        "schema": "local-company.workflow-value-list.v1",
        "status": "ready",
        "project": project_filter,
        "currency": currency_filter,
        "count": len(items),
        "opportunities": items,
        "modelCalled": False,
        "stateMutated": False,
        "externalActionPerformed": False,
    }


def next_workflow_opportunity(
    company_home: Path,
    *,
    project: str | None = None,
    currency: str | None = None,
) -> dict[str, object]:
    listed = list_workflow_opportunities(
        company_home, project=project, currency=currency,
    )
    valid = [
        item for item in listed["opportunities"]
        if isinstance(item, dict) and item.get("status") != "invalid"
    ]
    currencies = sorted({str(item["economics"]["currency"]) for item in valid})
    if len(currencies) > 1 and currency is None:
        return {
            "schema": "local-company.workflow-value-next.v1",
            "status": "blocked",
            "reason": "mixed_currencies_not_comparable",
            "currencies": currencies,
            "candidate": None,
            "nextAction": "rerun_with_explicit_currency",
            "modelCalled": False,
            "stateMutated": False,
            "externalActionPerformed": False,
        }
    if not valid:
        return {
            "schema": "local-company.workflow-value-next.v1",
            "status": "empty",
            "reason": "no_measured_opportunities",
            "candidate": None,
            "nextAction": "observe_one_repeated_task_at_least_three_times",
            "nextCommand": "local-ai.cmd value add",
            "modelCalled": False,
            "stateMutated": False,
            "externalActionPerformed": False,
        }
    ranked = sorted(
        valid,
        key=lambda item: (
            item["offerCandidate"] is True,
            item["economics"]["scenarioQualified"] is True,
            float(item["economics"]["superMegaFirstYearGrossProfit"]),
            float(item["economics"]["customerFirstYearNetValue"]),
            float(item["economics"]["monthlyGrossWorkflowValue"]),
            str(item["opportunityId"]),
        ),
        reverse=True,
    )
    candidate = ranked[0]
    return {
        "schema": "local-company.workflow-value-next.v1",
        "status": "candidate_selected",
        "reason": "highest_comparable_profit_after_mutual_qualification",
        "candidate": candidate,
        "consideredCount": len(valid),
        "rankingPolicy": [
            "offer_candidate_first",
            "scenario_qualified_first",
            "supermega_first_year_gross_profit_descending",
            "customer_first_year_net_value_descending",
            "monthly_gross_workflow_value_descending",
        ],
        "nextAction": candidate["nextAction"],
        "nextCommand": candidate["nextCommand"],
        "modelCalled": False,
        "stateMutated": False,
        "externalActionPerformed": False,
    }


def _pack_markdown(status: dict[str, object]) -> str:
    economics = status["economics"]
    measured = status["measuredEconomics"]
    project = _markdown_text(status["project"])
    task = _markdown_text(status["task"])
    application = _markdown_text(status["application"])
    failed_gates = [
        key for key, passed in economics["gates"].items() if passed is not True
    ]
    gate_lines = "\n".join(
        f"- [{'x' if passed else ' '}] `{key}`"
        for key, passed in economics["gates"].items()
    )
    evidence_lines = (
        f"- Binding: `{status['bindingStatus']}`\n"
        f"- Pilot: `{status['pilotStatus']}`\n"
        f"- Consecutive accepted runs: "
        f"{status['pilotEvidence']['consecutivePassingRuns']}/"
        f"{status['pilotEvidence']['requiredConsecutivePassingRuns']}\n"
        f"- Reliability gate passed: "
        f"`{str(status['pilotEvidence']['reliabilityGatePassed']).lower()}`\n"
        f"- Positive-value gate passed: "
        f"`{str(status['pilotEvidence']['valueGatePassed']).lower()}`\n"
        f"- Verified trailing-window net minutes saved: "
        f"{status['pilotEvidence']['verifiedPromotionWindowNetMinutesSaved']}\n"
        f"- Mean verified net minutes saved per run: "
        f"{status['pilotEvidence']['meanVerifiedNetMinutesSavedPerRun']}"
    )
    if isinstance(measured, dict):
        measured_failed_gates = [
            key for key, passed in measured["gates"].items() if passed is not True
        ]
        measured_lines = f"""- Basis: `{measured['pricingBasis']}`
- Promotion window: {measured['promotionWindowRuns']} runs
- Monthly measured labor-capacity value: {measured['monthlyMeasuredLaborCapacityValue']} {measured['currency']}
- Measured error value included: `false`
- Customer first-year net value: {measured['customerFirstYearNetValue']} {measured['currency']}
- Customer payback: {measured['customerPaybackMonths']} months
- SuperMega first-year gross profit: {measured['superMegaFirstYearGrossProfit']} {measured['currency']}
- SuperMega gross margin: {measured['superMegaFirstYearGrossMarginPercent']}%
- Measured economics qualified: `{str(measured['qualified']).lower()}`
- Failed measured gates: {', '.join(measured_failed_gates) if measured_failed_gates else 'none'}"""
    else:
        measured_lines = (
            "Measured economics are unavailable until a matching, complete "
            "trailing reliability window exists."
        )
    return f"""# Owner-review workflow business case

Status: **{status['status']}**
Project: **{project}**
Opportunity: **{status['name']}** (`{status['opportunityId']}`)

## Outcome being considered

{task}

Application: `{application}`

## Owner-supplied pricing scenario

This is a planning scenario, not a market quote, customer promise, or proof of demand.

- Currency: `{economics['currency']}`
- Manual effort: {economics['meanHumanMinutesPerRun']} minutes/run; {economics['monthlyRuns']} runs/month
- Gross workflow value: {economics['monthlyGrossWorkflowValue']} {economics['currency']}/month
- Setup price: {economics['setupPrice']} {economics['currency']}
- Monthly support price: {economics['monthlySupportPrice']} {economics['currency']}
- Delivery basis: {economics['deliveryHours']} setup hours at {economics['deliveryCostPerHour']} {economics['currency']}/hour; {economics['monthlySupportHours']} support hours/month
- Customer first-year net value: {economics['customerFirstYearNetValue']} {economics['currency']}
- Customer payback: {economics['customerPaybackMonths']} months
- SuperMega first-year revenue: {economics['superMegaFirstYearRevenue']} {economics['currency']}
- SuperMega first-year delivery cost: {economics['superMegaFirstYearDeliveryCost']} {economics['currency']}
- SuperMega first-year gross profit: {economics['superMegaFirstYearGrossProfit']} {economics['currency']}
- SuperMega gross margin: {economics['superMegaFirstYearGrossMarginPercent']}%

## Scenario gates

{gate_lines}

Failed gates: {', '.join(failed_gates) if failed_gates else 'none'}

## Technical and measured evidence

{evidence_lines}

## Measured economics

{measured_lines}

## Commercial boundary

- Offer candidate: `{str(status['offerCandidate']).lower()}`
- Paid demand proven: `false`
- Customer quote authorized: `false`
- External action authorized: `false`
- Customer contact performed: `false`

## Next action

`{status['nextCommand']}`

No outreach, payment, deployment, credential use, production write, or customer promise is authorized by this document.
"""


def create_workflow_value_pack(
    company_home: Path,
    opportunity_id: str,
) -> dict[str, object]:
    status = workflow_value_status(company_home, opportunity_id)
    pack_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-"
        + str(status["opportunityId"]) + "-" + uuid.uuid4().hex[:8]
    )
    output = _root(company_home) / "packs" / pack_id
    output.mkdir(parents=True, exist_ok=False)
    brief_path = output / "owner-review-business-case.md"
    brief_path.write_text(_pack_markdown(status), encoding="utf-8")
    brief_sha = _file_digest(brief_path)
    (output / "owner-review-business-case.sha256").write_text(
        f"{brief_sha} *{brief_path.name}\n", encoding="ascii",
    )
    receipt: dict[str, object] = {
        "schema": PACK_SCHEMA,
        "status": (
            "owner_review_offer_candidate"
            if status["offerCandidate"] else "owner_review_discovery_brief"
        ),
        "packId": pack_id,
        "opportunityId": status["opportunityId"],
        "opportunitySha256": status["opportunitySha256"],
        "offerCandidate": status["offerCandidate"],
        "brief": {
            "file": brief_path.name,
            "bytes": brief_path.stat().st_size,
            "sha256": brief_sha,
            "sha256Sidecar": "owner-review-business-case.sha256",
        },
        "customerQuoteAuthorized": False,
        "paidDemandProven": False,
        "externalActionAuthorized": False,
        "customerContactPerformed": False,
        "modelCalled": False,
        "stateMutated": True,
        "externalActionPerformed": False,
    }
    receipt_path = output / "pack-receipt.json"
    _write_json(receipt_path, receipt)
    receipt_sha = _file_digest(receipt_path)
    (output / "pack-receipt.sha256").write_text(
        f"{receipt_sha} *{receipt_path.name}\n", encoding="ascii",
    )
    return {
        **receipt,
        "packDirectory": str(output),
        "briefPath": str(brief_path),
        "receiptPath": str(receipt_path),
        "receiptSha256": receipt_sha,
        "nextAction": status["nextAction"],
        "nextCommand": status["nextCommand"],
    }
