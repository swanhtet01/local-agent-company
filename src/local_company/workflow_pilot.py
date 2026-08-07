from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .computer_use import (
    NETWORK_PROCESSES,
    RUN_SCHEMA,
    SHELL_PROCESSES,
    load_workflow,
)


PILOT_SCHEMA = "local-company.workflow-pilot.v1"
PILOT_REVIEW_SCHEMA = "local-company.workflow-pilot-review.v1"
PILOT_RUN_ANCHOR_SCHEMA = "local-company.workflow-pilot-run-anchor.v1"
PILOT_STATUS_SCHEMA = "local-company.workflow-pilot-status.v1"
BASELINE_CONFIRMATION = "RECORD MEASURED WORKFLOW BASELINE"
REVIEW_CONFIRMATION = "REVIEW MEASURED WORKFLOW RUN"
MINIMUM_BASELINE_OBSERVATIONS = 3
MINIMUM_ACCEPTANCE_RUNS = 20
MAXIMUM_JSON_BYTES = 2 * 1024 * 1024
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{12}Z$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id_floor() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _digest(schema: str, value: object) -> str:
    return hashlib.sha256(schema.encode("ascii") + b"\0" + _canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _exclusive_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def _number(value: object, reason: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(reason)
    observed = float(value)
    if not math.isfinite(observed) or not minimum <= observed <= maximum:
        raise ValueError(reason)
    return observed


def _pilot_path(workflow_path: Path) -> Path:
    return workflow_path.parent / "pilot" / "pilot.json"


def _seal_pilot(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed.pop("pilotSha256", None)
    sealed["pilotSha256"] = _digest(PILOT_SCHEMA, sealed)
    return _validate_pilot(sealed)


def _validate_pilot(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "workflow", "workflowSha256", "createdAt", "runIdFloor",
        "baseline", "outcome", "acceptance", "authority", "pilotSha256",
    }:
        raise ValueError("workflow_pilot_manifest_invalid")
    if value.get("schema") != PILOT_SCHEMA:
        raise ValueError("workflow_pilot_schema_invalid")
    workflow = value.get("workflow")
    workflow_sha = value.get("workflowSha256")
    if not isinstance(workflow, str) or not workflow or len(workflow) > 64:
        raise ValueError("workflow_pilot_workflow_invalid")
    if not isinstance(workflow_sha, str) or _SHA256.fullmatch(workflow_sha) is None:
        raise ValueError("workflow_pilot_workflow_seal_invalid")
    if not isinstance(value.get("createdAt"), str) or len(str(value["createdAt"])) > 64:
        raise ValueError("workflow_pilot_created_at_invalid")
    if not isinstance(value.get("runIdFloor"), str) or _RUN_ID.fullmatch(str(value["runIdFloor"])) is None:
        raise ValueError("workflow_pilot_run_floor_invalid")

    baseline = value.get("baseline")
    if not isinstance(baseline, dict) or set(baseline) != {
        "source", "humanObservationConfirmed", "observedRuns",
        "observedHumanMinutesTotal", "meanHumanMinutesPerRun", "runsPerWeek",
        "weeklyHumanMinutes", "observedErrors", "observedErrorCostTotal",
    }:
        raise ValueError("workflow_pilot_baseline_invalid")
    if baseline.get("source") != "owner_observed" or baseline.get("humanObservationConfirmed") is not True:
        raise ValueError("workflow_pilot_baseline_source_invalid")
    observed_runs = baseline.get("observedRuns")
    observed_errors = baseline.get("observedErrors")
    if type(observed_runs) is not int or not MINIMUM_BASELINE_OBSERVATIONS <= observed_runs <= 1000:
        raise ValueError("workflow_pilot_observed_runs_invalid")
    if type(observed_errors) is not int or not 0 <= observed_errors <= observed_runs:
        raise ValueError("workflow_pilot_observed_errors_invalid")
    total = _number(
        baseline.get("observedHumanMinutesTotal"),
        "workflow_pilot_human_minutes_invalid", 0.1, 1_000_000,
    )
    mean = _number(
        baseline.get("meanHumanMinutesPerRun"),
        "workflow_pilot_mean_minutes_invalid", 0.0001, 10_000,
    )
    weekly_runs = _number(
        baseline.get("runsPerWeek"), "workflow_pilot_runs_per_week_invalid", 0.01, 100_000,
    )
    weekly_minutes = _number(
        baseline.get("weeklyHumanMinutes"),
        "workflow_pilot_weekly_minutes_invalid", 0.0001, 100_000_000,
    )
    _number(
        baseline.get("observedErrorCostTotal"),
        "workflow_pilot_error_cost_invalid", 0, 1_000_000_000,
    )
    if abs(mean - total / observed_runs) > 0.000001:
        raise ValueError("workflow_pilot_mean_minutes_mismatch")
    if abs(weekly_minutes - mean * weekly_runs) > 0.000001:
        raise ValueError("workflow_pilot_weekly_minutes_mismatch")

    outcome = value.get("outcome")
    if not isinstance(outcome, dict) or set(outcome) != {
        "label", "verifier", "windowTitleAssertionConfigured",
        "controlTextAssertionConfigured",
    }:
        raise ValueError("workflow_pilot_outcome_invalid")
    label = outcome.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 500:
        raise ValueError("workflow_pilot_outcome_label_invalid")
    if outcome.get("verifier") != "sealed_workflow_final_postcondition":
        raise ValueError("workflow_pilot_outcome_verifier_invalid")
    title_gate = outcome.get("windowTitleAssertionConfigured")
    control_gate = outcome.get("controlTextAssertionConfigured")
    if type(title_gate) is not bool or type(control_gate) is not bool or not (title_gate or control_gate):
        raise ValueError("workflow_pilot_machine_outcome_required")

    acceptance = value.get("acceptance")
    if not isinstance(acceptance, dict) or set(acceptance) != {
        "requiredConsecutivePassingRuns", "humanReviewRequiredForEveryRun",
        "zeroWrongTargetActionsRequired", "zeroExternalEffectsRequired",
    }:
        raise ValueError("workflow_pilot_acceptance_invalid")
    required_runs = acceptance.get("requiredConsecutivePassingRuns")
    if type(required_runs) is not int or not MINIMUM_ACCEPTANCE_RUNS <= required_runs <= 100:
        raise ValueError("workflow_pilot_required_runs_invalid")
    if any(
        acceptance.get(key) is not True
        for key in (
            "humanReviewRequiredForEveryRun", "zeroWrongTargetActionsRequired",
            "zeroExternalEffectsRequired",
        )
    ):
        raise ValueError("workflow_pilot_acceptance_gate_invalid")

    authority = value.get("authority")
    if not isinstance(authority, dict) or authority != {
        "environment": "local_or_test_only",
        "networkAndShellAppsAllowed": False,
        "externalActionAuthorityGranted": False,
    }:
        raise ValueError("workflow_pilot_authority_invalid")
    recorded = value.get("pilotSha256")
    if not isinstance(recorded, str) or _SHA256.fullmatch(recorded) is None:
        raise ValueError("workflow_pilot_seal_invalid")
    unsealed = dict(value)
    unsealed.pop("pilotSha256")
    if _digest(PILOT_SCHEMA, unsealed) != recorded:
        raise ValueError("workflow_pilot_seal_mismatch")
    return value


def _load_pilot(workflow_path: Path) -> dict[str, object]:
    return _validate_pilot(_read_json(
        _pilot_path(workflow_path), "workflow_pilot_not_found_or_invalid",
    ))


def start_workflow_pilot(
    company_home: Path,
    name: str,
    *,
    observed_runs: int,
    observed_human_minutes_total: float,
    runs_per_week: float,
    observed_errors: int,
    observed_error_cost_total: float,
    outcome_label: str,
    confirmation: str,
    required_runs: int = MINIMUM_ACCEPTANCE_RUNS,
) -> dict[str, object]:
    if confirmation != BASELINE_CONFIRMATION:
        raise ValueError("workflow_pilot_baseline_confirmation_invalid")
    workflow_path, workflow = load_workflow(company_home, name)
    if workflow_path.is_symlink():
        raise ValueError("workflow_pilot_workflow_path_invalid")
    if type(observed_runs) is not int or not MINIMUM_BASELINE_OBSERVATIONS <= observed_runs <= 1000:
        raise ValueError("workflow_pilot_observed_runs_invalid")
    human_total = _number(
        observed_human_minutes_total, "workflow_pilot_human_minutes_invalid", 0.1, 1_000_000,
    )
    weekly_runs = _number(
        runs_per_week, "workflow_pilot_runs_per_week_invalid", 0.01, 100_000,
    )
    if type(observed_errors) is not int or not 0 <= observed_errors <= observed_runs:
        raise ValueError("workflow_pilot_observed_errors_invalid")
    error_cost = _number(
        observed_error_cost_total, "workflow_pilot_error_cost_invalid", 0, 1_000_000_000,
    )
    if not isinstance(outcome_label, str) or not outcome_label.strip() or len(outcome_label) > 500:
        raise ValueError("workflow_pilot_outcome_label_invalid")
    if type(required_runs) is not int or not MINIMUM_ACCEPTANCE_RUNS <= required_runs <= 100:
        raise ValueError("workflow_pilot_required_runs_invalid")
    title_gate = bool(workflow.get("expectedFinalWindowTitleContains"))
    control_gate = bool(workflow.get("expectedFinalControlTextContains"))
    if not (title_gate or control_gate):
        raise ValueError("workflow_pilot_machine_outcome_required")
    processes = {
        str(step["window"].get("processName", "")).lower()
        for step in workflow["steps"]
        if isinstance(step, dict) and isinstance(step.get("window"), dict)
    }
    if processes & (NETWORK_PROCESSES | SHELL_PROCESSES):
        raise ValueError("workflow_pilot_network_or_shell_app_forbidden")

    mean_minutes = human_total / observed_runs
    pilot = _seal_pilot({
        "schema": PILOT_SCHEMA,
        "workflow": workflow["name"],
        "workflowSha256": workflow["workflowSha256"],
        "createdAt": _now(),
        "runIdFloor": _run_id_floor(),
        "baseline": {
            "source": "owner_observed",
            "humanObservationConfirmed": True,
            "observedRuns": observed_runs,
            "observedHumanMinutesTotal": human_total,
            "meanHumanMinutesPerRun": mean_minutes,
            "runsPerWeek": weekly_runs,
            "weeklyHumanMinutes": mean_minutes * weekly_runs,
            "observedErrors": observed_errors,
            "observedErrorCostTotal": error_cost,
        },
        "outcome": {
            "label": outcome_label.strip(),
            "verifier": "sealed_workflow_final_postcondition",
            "windowTitleAssertionConfigured": title_gate,
            "controlTextAssertionConfigured": control_gate,
        },
        "acceptance": {
            "requiredConsecutivePassingRuns": required_runs,
            "humanReviewRequiredForEveryRun": True,
            "zeroWrongTargetActionsRequired": True,
            "zeroExternalEffectsRequired": True,
        },
        "authority": {
            "environment": "local_or_test_only",
            "networkAndShellAppsAllowed": False,
            "externalActionAuthorityGranted": False,
        },
    })
    destination = _pilot_path(workflow_path)
    _exclusive_json(destination, pilot)
    return {
        "schema": "local-company.workflow-pilot-start.v1",
        "status": "created",
        "workflow": workflow["name"],
        "workflowSha256": workflow["workflowSha256"],
        "pilotSha256": pilot["pilotSha256"],
        "baseline": pilot["baseline"],
        "acceptance": pilot["acceptance"],
        "pilotPath": str(destination),
        "stateMutated": True,
        "modelCalled": False,
        "externalActionPerformed": False,
        "externalActionAuthorityGranted": False,
        "nextCommand": f"local-ai.cmd automate pilot-status {workflow['name']}",
    }


def _validated_receipt(
    workflow_path: Path,
    pilot: dict[str, object],
    run_id: str,
) -> tuple[dict[str, object] | None, str | None, str | None]:
    if _RUN_ID.fullmatch(run_id) is None or run_id < str(pilot["runIdFloor"]):
        return None, None, "run_id_outside_pilot"
    run_root = workflow_path.parent / "runs" / run_id
    receipt_path = run_root / "receipt.json"
    sidecar_path = run_root / "receipt.sha256"
    if run_root.is_symlink() or receipt_path.is_symlink() or sidecar_path.is_symlink():
        return None, None, "run_receipt_symlink_forbidden"
    try:
        receipt = _read_json(receipt_path, "run_receipt_invalid")
        digest = _file_digest(receipt_path)
        sidecar = sidecar_path.read_text(encoding="ascii", errors="strict").strip()
    except (OSError, UnicodeError, ValueError):
        return None, None, "run_receipt_or_sidecar_invalid"
    if sidecar != f"{digest} *receipt.json":
        return None, digest, "run_receipt_digest_mismatch"
    if (
        receipt.get("schema") != RUN_SCHEMA
        or receipt.get("runId") != run_id
        or receipt.get("name") != pilot["workflow"]
        or receipt.get("workflowSha256") != pilot["workflowSha256"]
        or receipt.get("receiptSha256Sidecar") != "receipt.sha256"
    ):
        return None, digest, "run_receipt_contract_mismatch"
    return receipt, digest, None


def _seal_review(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed.pop("reviewSha256", None)
    sealed["reviewSha256"] = _digest(PILOT_REVIEW_SCHEMA, sealed)
    return _validate_review(sealed)


def _validate_review(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "pilotSha256", "workflowSha256", "runId",
        "runReceiptSha256", "reviewedAt", "human", "reviewSha256",
    }:
        raise ValueError("workflow_pilot_review_invalid")
    if value.get("schema") != PILOT_REVIEW_SCHEMA:
        raise ValueError("workflow_pilot_review_schema_invalid")
    for key in ("pilotSha256", "workflowSha256", "runReceiptSha256", "reviewSha256"):
        if not isinstance(value.get(key), str) or _SHA256.fullmatch(str(value[key])) is None:
            raise ValueError("workflow_pilot_review_digest_invalid")
    if not isinstance(value.get("runId"), str) or _RUN_ID.fullmatch(str(value["runId"])) is None:
        raise ValueError("workflow_pilot_review_run_id_invalid")
    if not isinstance(value.get("reviewedAt"), str) or len(str(value["reviewedAt"])) > 64:
        raise ValueError("workflow_pilot_review_time_invalid")
    human = value.get("human")
    if not isinstance(human, dict) or set(human) != {
        "confirmationRecorded", "observedOutcome", "correctionMinutes",
        "wrongTargetActions", "externalEffectObserved",
    }:
        raise ValueError("workflow_pilot_review_human_invalid")
    if human.get("confirmationRecorded") is not True:
        raise ValueError("workflow_pilot_review_confirmation_invalid")
    if human.get("observedOutcome") not in {"correct", "incorrect"}:
        raise ValueError("workflow_pilot_review_outcome_invalid")
    _number(
        human.get("correctionMinutes"),
        "workflow_pilot_review_correction_invalid", 0, 480,
    )
    wrong_targets = human.get("wrongTargetActions")
    if type(wrong_targets) is not int or not 0 <= wrong_targets <= 100:
        raise ValueError("workflow_pilot_review_wrong_target_invalid")
    if type(human.get("externalEffectObserved")) is not bool:
        raise ValueError("workflow_pilot_review_external_effect_invalid")
    recorded = str(value["reviewSha256"])
    unsealed = dict(value)
    unsealed.pop("reviewSha256")
    if _digest(PILOT_REVIEW_SCHEMA, unsealed) != recorded:
        raise ValueError("workflow_pilot_review_seal_mismatch")
    return value


def _review_path(workflow_path: Path, run_id: str) -> Path:
    return workflow_path.parent / "pilot" / "reviews" / f"{run_id}.json"


def _anchor_path(workflow_path: Path, run_id: str) -> Path:
    return workflow_path.parent / "pilot" / "run-anchors" / f"{run_id}.json"


def _seal_anchor(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed.pop("anchorSha256", None)
    sealed["anchorSha256"] = _digest(PILOT_RUN_ANCHOR_SCHEMA, sealed)
    return _validate_anchor(sealed)


def _validate_anchor(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "pilotSha256", "workflowSha256", "runId",
        "runReceiptSha256", "anchoredAt", "anchorSha256",
    }:
        raise ValueError("workflow_pilot_run_anchor_invalid")
    if value.get("schema") != PILOT_RUN_ANCHOR_SCHEMA:
        raise ValueError("workflow_pilot_run_anchor_schema_invalid")
    for key in (
        "pilotSha256", "workflowSha256", "runReceiptSha256", "anchorSha256",
    ):
        if not isinstance(value.get(key), str) or _SHA256.fullmatch(str(value[key])) is None:
            raise ValueError("workflow_pilot_run_anchor_digest_invalid")
    if not isinstance(value.get("runId"), str) or _RUN_ID.fullmatch(str(value["runId"])) is None:
        raise ValueError("workflow_pilot_run_anchor_id_invalid")
    if not isinstance(value.get("anchoredAt"), str) or len(str(value["anchoredAt"])) > 64:
        raise ValueError("workflow_pilot_run_anchor_time_invalid")
    recorded = str(value["anchorSha256"])
    unsealed = dict(value)
    unsealed.pop("anchorSha256")
    if _digest(PILOT_RUN_ANCHOR_SCHEMA, unsealed) != recorded:
        raise ValueError("workflow_pilot_run_anchor_seal_mismatch")
    return value


def _load_anchor(
    workflow_path: Path,
    pilot: dict[str, object],
    run_id: str,
    receipt_sha256: str | None,
) -> tuple[dict[str, object] | None, str | None]:
    path = _anchor_path(workflow_path, run_id)
    if not path.exists():
        return None, "missing"
    try:
        anchor = _validate_anchor(_read_json(
            path, "workflow_pilot_run_anchor_invalid",
        ))
    except (OSError, UnicodeError, ValueError):
        return None, "invalid"
    if (
        anchor["pilotSha256"] != pilot["pilotSha256"]
        or anchor["workflowSha256"] != pilot["workflowSha256"]
        or anchor["runId"] != run_id
        or receipt_sha256 is None
        or anchor["runReceiptSha256"] != receipt_sha256
    ):
        return None, "binding_mismatch"
    return anchor, None


def anchor_workflow_pilot_run(
    company_home: Path,
    name: str,
    run_id: str,
) -> dict[str, object]:
    """Bind one completed run receipt into the active pilot's append-only index."""
    workflow_path, workflow = load_workflow(company_home, name)
    pilot = _load_pilot(workflow_path)
    if workflow["workflowSha256"] != pilot["workflowSha256"]:
        raise ValueError("workflow_pilot_workflow_changed")
    receipt, receipt_sha, receipt_error = _validated_receipt(
        workflow_path, pilot, run_id,
    )
    if receipt is None or receipt_sha is None:
        raise ValueError("workflow_pilot_run_receipt_invalid:" + str(receipt_error))
    anchor = _seal_anchor({
        "schema": PILOT_RUN_ANCHOR_SCHEMA,
        "pilotSha256": pilot["pilotSha256"],
        "workflowSha256": pilot["workflowSha256"],
        "runId": run_id,
        "runReceiptSha256": receipt_sha,
        "anchoredAt": _now(),
    })
    _exclusive_json(_anchor_path(workflow_path, run_id), anchor)
    return {
        "status": "anchored",
        "runId": run_id,
        "receiptSha256": receipt_sha,
        "anchorSha256": anchor["anchorSha256"],
    }


def _load_review(
    workflow_path: Path,
    pilot: dict[str, object],
    run_id: str,
    receipt_sha256: str,
) -> tuple[dict[str, object] | None, str | None]:
    path = _review_path(workflow_path, run_id)
    if not path.exists():
        return None, "missing"
    try:
        review = _validate_review(_read_json(path, "workflow_pilot_review_invalid"))
    except (OSError, UnicodeError, ValueError):
        return None, "invalid"
    if (
        review["pilotSha256"] != pilot["pilotSha256"]
        or review["workflowSha256"] != pilot["workflowSha256"]
        or review["runId"] != run_id
        or review["runReceiptSha256"] != receipt_sha256
    ):
        return None, "binding_mismatch"
    return review, None


def review_workflow_pilot_run(
    company_home: Path,
    name: str,
    run_id: str,
    *,
    observed_outcome: str,
    correction_minutes: float,
    wrong_target_actions: int,
    external_effect_observed: bool,
    confirmation: str,
) -> dict[str, object]:
    if confirmation != REVIEW_CONFIRMATION:
        raise ValueError("workflow_pilot_review_confirmation_invalid")
    workflow_path, workflow = load_workflow(company_home, name)
    pilot = _load_pilot(workflow_path)
    if workflow["workflowSha256"] != pilot["workflowSha256"]:
        raise ValueError("workflow_pilot_workflow_changed")
    if observed_outcome not in {"correct", "incorrect"}:
        raise ValueError("workflow_pilot_review_outcome_invalid")
    correction = _number(
        correction_minutes, "workflow_pilot_review_correction_invalid", 0, 480,
    )
    if type(wrong_target_actions) is not int or not 0 <= wrong_target_actions <= 100:
        raise ValueError("workflow_pilot_review_wrong_target_invalid")
    if type(external_effect_observed) is not bool:
        raise ValueError("workflow_pilot_review_external_effect_invalid")
    receipt, receipt_sha, receipt_error = _validated_receipt(
        workflow_path, pilot, run_id,
    )
    if receipt is None or receipt_sha is None:
        raise ValueError("workflow_pilot_run_receipt_invalid:" + str(receipt_error))
    anchor, anchor_error = _load_anchor(
        workflow_path, pilot, run_id, receipt_sha,
    )
    if anchor is None:
        raise ValueError("workflow_pilot_run_anchor_invalid:" + str(anchor_error))
    review = _seal_review({
        "schema": PILOT_REVIEW_SCHEMA,
        "pilotSha256": pilot["pilotSha256"],
        "workflowSha256": pilot["workflowSha256"],
        "runId": run_id,
        "runReceiptSha256": receipt_sha,
        "reviewedAt": _now(),
        "human": {
            "confirmationRecorded": True,
            "observedOutcome": observed_outcome,
            "correctionMinutes": correction,
            "wrongTargetActions": wrong_target_actions,
            "externalEffectObserved": external_effect_observed,
        },
    })
    destination = _review_path(workflow_path, run_id)
    _exclusive_json(destination, review)
    status = workflow_pilot_status(company_home, name)
    return {
        "schema": "local-company.workflow-pilot-review-result.v1",
        "status": "reviewed",
        "workflow": workflow["name"],
        "runId": run_id,
        "executionStatus": receipt["status"],
        "machineOutcomeVerified": receipt.get("outcomeVerified") is True,
        "humanObservedOutcome": observed_outcome,
        "reviewSha256": review["reviewSha256"],
        "pilotStatus": status["status"],
        "consecutivePassingRuns": status["consecutivePassingRuns"],
        "requiredConsecutivePassingRuns": status["requiredConsecutivePassingRuns"],
        "promotionGatePassed": status["promotionGatePassed"],
        "stateMutated": True,
        "modelCalled": False,
        "externalActionPerformed": False,
        "externalActionAuthorityGranted": False,
    }


def workflow_pilot_status(company_home: Path, name: str) -> dict[str, object]:
    workflow_path, workflow = load_workflow(company_home, name)
    pilot = _load_pilot(workflow_path)
    required_runs = int(pilot["acceptance"]["requiredConsecutivePassingRuns"])
    if workflow["workflowSha256"] != pilot["workflowSha256"]:
        return {
            "schema": PILOT_STATUS_SCHEMA,
            "status": "blocked",
            "reason": "workflow_changed_after_pilot_start",
            "workflow": workflow["name"],
            "pilotSha256": pilot["pilotSha256"],
            "promotionGatePassed": False,
            "consecutivePassingRuns": 0,
            "requiredConsecutivePassingRuns": required_runs,
            "modelCalled": False,
            "stateMutated": False,
            "externalActionPerformed": False,
            "externalActionAuthorityGranted": False,
        }

    run_items: list[dict[str, object]] = []
    runs_root = workflow_path.parent / "runs"
    anchors_root = workflow_path.parent / "pilot" / "run-anchors"
    run_ids: set[str] = set()
    if runs_root.is_dir():
        run_ids.update(
            path.name for path in runs_root.iterdir()
            if path.is_dir() and _RUN_ID.fullmatch(path.name)
            and path.name >= str(pilot["runIdFloor"])
        )
    if anchors_root.is_dir():
        run_ids.update(
            path.stem for path in anchors_root.iterdir()
            if path.is_file() and path.suffix == ".json"
            and _RUN_ID.fullmatch(path.stem)
            and path.stem >= str(pilot["runIdFloor"])
        )
    for run_id in sorted(run_ids)[:2000]:
        receipt, receipt_sha, receipt_error = _validated_receipt(
            workflow_path, pilot, run_id,
        )
        anchor, anchor_error = _load_anchor(
            workflow_path, pilot, run_id, receipt_sha,
        )
        item: dict[str, object] = {
            "runId": run_id,
            "receiptIntegrity": "valid" if receipt is not None else "invalid",
            "receiptError": receipt_error,
            "receiptSha256": receipt_sha,
            "anchorIntegrity": "valid" if anchor is not None else "invalid",
            "anchorError": anchor_error,
            "anchorSha256": anchor.get("anchorSha256") if anchor else None,
            "executionStatus": receipt.get("status") if receipt else None,
            "machineOutcomeVerified": receipt.get("outcomeVerified") is True
            if receipt else False,
            "wallSeconds": receipt.get("wallSeconds") if receipt else None,
            "reviewStatus": "unavailable",
            "humanObservedOutcome": None,
            "correctionMinutes": None,
            "wrongTargetActions": None,
            "externalEffectObserved": None,
            "accepted": False,
        }
        if receipt is not None and receipt_sha is not None and anchor is not None:
            review, review_error = _load_review(
                workflow_path, pilot, run_id, receipt_sha,
            )
            item["reviewStatus"] = "valid" if review is not None else review_error
            if review is not None:
                human = review["human"]
                item.update({
                    "humanObservedOutcome": human["observedOutcome"],
                    "correctionMinutes": human["correctionMinutes"],
                    "wrongTargetActions": human["wrongTargetActions"],
                    "externalEffectObserved": human["externalEffectObserved"],
                })
                item["accepted"] = (
                    receipt.get("status") == "completed"
                    and receipt.get("outcomeVerified") is True
                    and human["observedOutcome"] == "correct"
                    and human["wrongTargetActions"] == 0
                    and human["externalEffectObserved"] is False
                )
        run_items.append(item)

    consecutive = 0
    for item in reversed(run_items):
        if item["accepted"] is not True:
            break
        consecutive += 1
    promotion = consecutive >= required_runs
    valid_receipts = [item for item in run_items if item["receiptIntegrity"] == "valid"]
    reviewed = [item for item in run_items if item["reviewStatus"] == "valid"]
    accepted = [item for item in run_items if item["accepted"] is True]
    invalid_receipts = [item for item in run_items if item["receiptIntegrity"] != "valid"]
    invalid_anchors = [item for item in run_items if item["anchorIntegrity"] != "valid"]
    invalid_reviews = [
        item for item in run_items
        if item["reviewStatus"] not in {"valid", "missing"}
    ]
    pending_review = next((
        str(item["runId"]) for item in run_items
        if item["receiptIntegrity"] == "valid"
        and item["anchorIntegrity"] == "valid"
        and item["reviewStatus"] == "missing"
    ), None)
    automation_seconds = sum(
        float(item["wallSeconds"])
        for item in valid_receipts
        if isinstance(item.get("wallSeconds"), (int, float))
        and not isinstance(item.get("wallSeconds"), bool)
    )
    correction_minutes = sum(
        float(item["correctionMinutes"])
        for item in reviewed
        if isinstance(item.get("correctionMinutes"), (int, float))
        and not isinstance(item.get("correctionMinutes"), bool)
    )
    mean_human_minutes = float(pilot["baseline"]["meanHumanMinutesPerRun"])
    completed_baseline_minutes = mean_human_minutes * len(accepted)
    verified_net_minutes = completed_baseline_minutes - automation_seconds / 60 - correction_minutes

    if promotion:
        next_action = "package_owner_review_offer_from_verified_pilot"
        next_command = f"local-ai.cmd automate pilot-status {workflow['name']}"
    elif pending_review is not None:
        next_action = "review_next_run"
        next_command = (
            f"local-ai.cmd automate pilot-review {workflow['name']} {pending_review} "
            f"--outcome correct --correction-minutes 0 --wrong-target-actions 0 "
            f'--confirm "{REVIEW_CONFIRMATION}"'
        )
    elif invalid_receipts or invalid_anchors or invalid_reviews:
        next_action = "inspect_integrity_failure_before_more_runs"
        next_command = f"local-ai.cmd automate pilot-status {workflow['name']}"
    else:
        next_action = "run_one_supervised_local_workflow"
        next_command = (
            f"local-ai.cmd automate run {workflow['name']} "
            f'--confirm "RUN LOCAL COMPUTER WORKFLOW"'
        )

    return {
        "schema": PILOT_STATUS_SCHEMA,
        "status": "passed" if promotion else "collecting",
        "workflow": workflow["name"],
        "workflowSha256": workflow["workflowSha256"],
        "pilotSha256": pilot["pilotSha256"],
        "baseline": pilot["baseline"],
        "outcome": pilot["outcome"],
        "authority": pilot["authority"],
        "totalRuns": len(run_items),
        "validRunReceipts": len(valid_receipts),
        "invalidRunReceipts": len(invalid_receipts),
        "validRunAnchors": len(run_items) - len(invalid_anchors),
        "invalidRunAnchors": len(invalid_anchors),
        "reviewedRuns": len(reviewed),
        "invalidReviews": len(invalid_reviews),
        "acceptedRuns": len(accepted),
        "consecutivePassingRuns": consecutive,
        "requiredConsecutivePassingRuns": required_runs,
        "promotionGatePassed": promotion,
        "commercialEvidenceStatus": "qualified" if promotion else "not_yet_proven",
        "metrics": {
            "measuredAutomationMinutes": round(automation_seconds / 60, 4),
            "measuredCorrectionMinutes": round(correction_minutes, 4),
            "verifiedCompletedBaselineMinutes": round(completed_baseline_minutes, 4),
            "verifiedNetMinutesSaved": round(verified_net_minutes, 4),
            "baselineObservedErrorRate": round(
                int(pilot["baseline"]["observedErrors"])
                / int(pilot["baseline"]["observedRuns"]), 6,
            ),
            "baselineObservedErrorCostTotal": pilot["baseline"]["observedErrorCostTotal"],
        },
        "nextReviewRunId": pending_review,
        "nextAction": next_action,
        "nextCommand": next_command,
        "runs": run_items,
        "modelCalled": False,
        "stateMutated": False,
        "externalActionPerformed": False,
        "externalActionAuthorityGranted": False,
    }
