"""Authenticated loopback API and client for the local agent workcell.

The HTTP transport deliberately reuses the existing dashboard service and its
single execution lock.  Many logical missions may be queued, but only one
model-backed or computer-use operation is admitted at a time on the Ally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from . import __version__
from .computer_use import RUN_CONFIRMATION, list_workflows, preview_workflow, run_workflow
from .config import default_company_home
from .core import Company, EVALUATOR_VERSION, OllamaModel, PLAYBOOKS, ROLES
from .focus import (
    enforce_execution_focus, enforce_execution_resource_envelope,
    read_execution_focus,
)
from .model_policy import DEFAULT_LOCAL_MODEL


AGENT_CARD_SCHEMA = "local-company.agent-card.v1"
AGENT_CATALOG_SCHEMA = "local-company.agent-catalog.v1"
AGENT_MISSION_SCHEMA = "local-company.agent-mission.v1"
AGENT_MISSION_LIST_SCHEMA = "local-company.agent-mission-list.v1"
AGENT_SUBMISSION_SCHEMA = "local-company.agent-submission.v1"
AGENT_COMPUTER_RUN_SCHEMA = "local-company.agent-computer-run.v1"
MISSION_RUN_CONFIRMATION = "RUN LOCAL AGENT MISSION"
MAX_JSON_BYTES = 64 * 1024
MAX_SYNTHESIS_CHARS = 32_000
MAX_ERROR_CHARS = 500
MISSION_ID_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
SERVICE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,512}\Z")
TERMINAL_MISSION_STATES = frozenset({
    "complete", "failed", "quality_failed", "needs_approval", "cancelled", "superseded",
})
_UNSAFE_AGENT_AUTHORITY_PATTERNS = (
    re.compile(
        r"\bi can\s+(?:initiate|execute|perform|make|send|contact|message|email|charge|pay|"
        r"purchase|buy|transfer|deploy|publish|release|delete|erase|access|use)\b.{0,80}\b"
        r"(?:transactions?|payments?|system access|credentials?|emails?|messages?|customers?|"
        r"money|purchases?|production|websites?|files?|data)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bi\s+(?:sent|contacted|messaged|emailed|charged|paid|purchased|bought|transferred|"
        r"deployed|published|released|deleted|erased)\b",
        re.IGNORECASE,
    ),
)


def _unsafe_agent_authority_claims(text: str) -> list[str]:
    """Flag model text that claims sensitive real-world authority or completion."""
    if not isinstance(text, str) or not text:
        return []
    return [
        f"claimed_sensitive_action_authority_{index}"
        for index, pattern in enumerate(_UNSAFE_AGENT_AUTHORITY_PATTERNS, start=1)
        if pattern.search(text) is not None
    ]


class AgentWorker(Protocol):
    def snapshot(self) -> dict[str, object]: ...
    def start(self, queue_id: str) -> None: ...
    def run_exclusive(
        self, kind: str, target: str, operation: Callable[[], dict[str, object]],
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class AgentAPIResponse:
    status: int
    body: dict[str, object]


class AgentAPIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = " ".join(message.split())[:MAX_ERROR_CHARS]

    def payload(self) -> dict[str, object]:
        return {
            "schema": "local-company.agent-api-error.v1",
            "status": "error",
            "error": self.code,
            "message": self.message,
        }


def _profile_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [{
        "id": "auto",
        "name": "Automatic local team",
        "description": (
            "Routes the objective to a bounded specialist team, then adds a chief of staff "
            "and deterministic quality review."
        ),
        "roles": ["chief-of-staff", "dynamically-routed-specialists", "quality"],
        "kind": "knowledge-worker-team",
    }]
    for name, value in sorted(PLAYBOOKS.items()):
        rows.append({
            "id": name,
            "name": name.replace("-", " ").title(),
            "description": value["description"],
            "roles": list(value["roles"]),
            "kind": "knowledge-worker-team",
        })
    return rows


def agent_card(base_url: str) -> dict[str, object]:
    """Return public, path-free discovery metadata; this is not an A2A claim."""
    return {
        "schema": AGENT_CARD_SCHEMA,
        "name": "SuperMega Local Agent Server",
        "version": __version__,
        "description": (
            "A local-first queue of Ollama knowledge-worker teams and supervised Windows "
            "computer workflows."
        ),
        "baseUrl": base_url.rstrip("/"),
        "transport": "authenticated-loopback-json",
        "protocol": "local-company.agent-api.v1",
        "complementaryProtocol": "MCP over stdio via company-mcp.cmd",
        "capabilities": {
            "knowledgeWorkerTeams": True,
            "durableMissionQueue": True,
            "missionResults": True,
            "supervisedComputerUse": os.name == "nt",
            "externalActions": False,
            "streaming": False,
        },
        "limits": {
            "physicalConcurrentExecutions": 1,
            "logicalQueuedMissions": "many",
            "modelProvider": "local Ollama",
            "networkBinding": "127.0.0.1 only",
        },
        "endpoints": {
            "catalog": "/api/v1/catalog",
            "missions": "/api/v1/missions",
            "computerWorkflows": "/api/v1/computer-workflows",
        },
        "authentication": {
            "type": "Bearer",
            "source": "private local service state",
        },
        "compatibility": {
            "a2a": "not_claimed",
            "note": "A future adapter can map this durable task lifecycle to A2A after TCK validation.",
        },
    }


def _require_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AgentAPIError(400, "invalid_json_body", "Request body must be a JSON object")
    return value


def _strict_keys(value: dict[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AgentAPIError(
            400, "unknown_fields", "Unknown request fields: " + ", ".join(unknown),
        )


def _clean_workflow_name(path_value: str) -> str:
    try:
        name = urllib.parse.unquote(path_value, errors="strict")
    except UnicodeError as exc:
        raise AgentAPIError(400, "invalid_workflow_name", "Workflow name is not valid UTF-8") from exc
    if not name or len(name) > 80 or any(character in name for character in "/\\\x00"):
        raise AgentAPIError(400, "invalid_workflow_name", "Workflow name is invalid")
    return name


class AgentAPI:
    def __init__(self, company: Company, worker: AgentWorker | None) -> None:
        self.company = company
        self.worker = worker

    def catalog(self) -> dict[str, object]:
        workflows = list_workflows(self.company.home)
        return {
            "schema": AGENT_CATALOG_SCHEMA,
            "status": "ready",
            "profiles": _profile_rows(),
            "roles": [
                {"id": name, "purpose": purpose}
                for name, purpose in sorted(ROLES.items())
            ],
            "computerUse": {
                "available": os.name == "nt",
                "mode": "supervised-sealed-replay",
                "networkAppsAllowedByApi": False,
                "shellsAllowedByApi": False,
                "workflowCount": workflows["workflowCount"],
                "workflows": workflows["workflows"],
            },
            "runtime": {
                "worker": self.worker.snapshot() if self.worker else {"status": "disabled"},
                "physicalConcurrentExecutions": 1,
                "queueBacked": True,
                "scaleToZero": True,
                "deterministicResultSafetyProjection": True,
            },
            "confirmations": {
                "runMission": MISSION_RUN_CONFIRMATION,
                "runComputerWorkflow": RUN_CONFIRMATION,
            },
            "externalActionsPerformed": False,
        }

    @staticmethod
    def _queue_row(company: Company, mission_id: str) -> tuple[object, ...]:
        if MISSION_ID_PATTERN.fullmatch(mission_id) is None:
            raise AgentAPIError(400, "invalid_mission_id", "Mission ID is invalid")
        row = next(
            (item for item in company.queue_items() if str(item[0]) == mission_id),
            None,
        )
        if row is None:
            raise AgentAPIError(404, "mission_not_found", "Mission was not found")
        return row

    def _mission(self, row: tuple[object, ...]) -> dict[str, object]:
        job_id = str(row[7]) if row[7] else None
        item: dict[str, object] = {
            "schema": AGENT_MISSION_SCHEMA,
            "missionId": str(row[0]),
            "status": str(row[1]),
            "priority": int(row[2]),
            "scheduledAt": str(row[3]),
            "project": str(row[4]) if row[4] else None,
            "profile": str(row[5]) if row[5] else "auto",
            "objective": str(row[6]),
            "jobId": job_id,
            "error": str(row[8])[:MAX_ERROR_CHARS] if row[8] else None,
            "terminal": str(row[1]) in TERMINAL_MISSION_STATES,
            "resultAvailable": False,
            "resultUsable": False,
        }
        if job_id:
            try:
                detail = self.company.job_detail(job_id)
            except ValueError:
                detail = None
            if detail is not None:
                job = detail["job"]
                synthesis = job[7] if isinstance(job[7], str) else ""
                evaluation = detail.get("evaluation")
                safety_alerts = _unsafe_agent_authority_claims(synthesis)
                quality_passed = bool(
                    isinstance(evaluation, dict) and evaluation.get("passed") is True
                    and evaluation.get("evaluator_version") == EVALUATOR_VERSION
                )
                report_sha = job[8] if isinstance(job[8], str) else None
                manifest_sha = job[9] if isinstance(job[9], str) else None
                seals_present = bool(
                    report_sha and re.fullmatch(r"[0-9a-f]{64}", report_sha)
                    and manifest_sha and re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
                )
                evidence_current = False
                if str(row[1]) == "complete" and quality_passed and seals_present:
                    report = detail.get("report")
                    report_current = bool(
                        detail.get("report_error") == ""
                        and isinstance(report, str)
                        and hashlib.sha256(report.encode("utf-8")).hexdigest() == report_sha
                        and evaluation.get("report_sha256") == report_sha
                        and evaluation.get("manifest_sha256") == manifest_sha
                    )
                    if report_current:
                        try:
                            evidence_current = self.company._validate_evidence_manifest(
                                job_id, manifest_sha,
                            )[0]
                        except (OSError, RuntimeError, ValueError):
                            evidence_current = False
                item["resultAvailable"] = bool(synthesis)
                metric_events = sum(
                    1 for event in detail.get("events", [])
                    if isinstance(event, (list, tuple)) and event
                    and event[0] == "model_metrics"
                )
                item["execution"] = {
                    "modelCalled": True if metric_events else None,
                    "recordedModelMetricEvents": metric_events,
                    "evidence": "job_events" if metric_events else "not_recorded",
                }
                item["resultUsable"] = bool(
                    str(row[1]) == "complete" and synthesis
                    and len(synthesis) <= MAX_SYNTHESIS_CHARS
                    and quality_passed and not safety_alerts and seals_present
                    and evidence_current
                )
                item["result"] = {
                    "synthesis": synthesis[:MAX_SYNTHESIS_CHARS] if synthesis else None,
                    "synthesisTruncated": len(synthesis) > MAX_SYNTHESIS_CHARS,
                    "reportSha256": report_sha,
                    "evidenceManifestSha256": manifest_sha,
                    "quality": (
                        {
                            "passed": evaluation.get("passed"),
                            "score": evaluation.get("score"),
                            "evaluatedAt": evaluation.get("evaluated_at"),
                            "evaluatorVersion": evaluation.get("evaluator_version"),
                        }
                        if isinstance(evaluation, dict) else None
                    ),
                    "safety": {
                        "scope": "deterministic_synthesis_review",
                        "passed": not safety_alerts,
                        "alerts": safety_alerts,
                        "modelCalled": False,
                    },
                }
        return item

    def mission(self, mission_id: str) -> dict[str, object]:
        return self._mission(self._queue_row(self.company, mission_id))

    def missions(self) -> dict[str, object]:
        all_rows = self.company.queue_items()
        active = [row for row in all_rows if str(row[1]) not in TERMINAL_MISSION_STATES]
        history = sorted(
            (row for row in all_rows if str(row[1]) in TERMINAL_MISSION_STATES),
            key=lambda row: str(row[3]),
            reverse=True,
        )
        rows = (active + history)[:50]
        return {
            "schema": AGENT_MISSION_LIST_SCHEMA,
            "status": "ready",
            "missionCount": len(all_rows),
            "returnedCount": len(rows),
            "missions": [
                {
                    "missionId": str(row[0]),
                    "status": str(row[1]),
                    "priority": int(row[2]),
                    "scheduledAt": str(row[3]),
                    "project": str(row[4]) if row[4] else None,
                    "profile": str(row[5]) if row[5] else "auto",
                    "objectivePreview": str(row[6])[:280],
                    "objectiveTruncated": len(str(row[6])) > 280,
                    "jobId": str(row[7]) if row[7] else None,
                    "error": str(row[8])[:200] if row[8] else None,
                    "terminal": str(row[1]) in TERMINAL_MISSION_STATES,
                }
                for row in rows
            ],
        }

    def submit(self, payload: object) -> AgentAPIResponse:
        value = _require_mapping(payload)
        _strict_keys(value, {"objective", "profile", "project", "priority", "start", "runConfirmation"})
        objective = value.get("objective")
        profile = value.get("profile", "auto")
        project = value.get("project")
        priority = value.get("priority", 50)
        start = value.get("start", False)
        if not isinstance(objective, str) or not objective.strip():
            raise AgentAPIError(400, "invalid_objective", "Objective must be non-empty text")
        if not isinstance(profile, str) or profile not in {"auto", *PLAYBOOKS.keys()}:
            raise AgentAPIError(400, "invalid_profile", "Unknown agent profile")
        if project is not None and (not isinstance(project, str) or not project.strip()):
            raise AgentAPIError(400, "invalid_project", "Project must be non-empty text when provided")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise AgentAPIError(400, "invalid_priority", "Priority must be an integer from 0 to 100")
        if type(start) is not bool:
            raise AgentAPIError(400, "invalid_start", "start must be a boolean")
        if start and value.get("runConfirmation") != MISSION_RUN_CONFIRMATION:
            raise AgentAPIError(
                400, "run_confirmation_required",
                f"Starting now requires runConfirmation={MISSION_RUN_CONFIRMATION!r}",
            )
        playbook = None if profile == "auto" else profile
        try:
            preflight = self.company.mission_preflight(
                objective, project.strip() if isinstance(project, str) else None, playbook,
            )
        except ValueError as exc:
            raise AgentAPIError(400, "mission_preflight_invalid", str(exc)) from exc
        if preflight.get("status") != "ready":
            categories = preflight.get("owner_gate_categories", [])
            blockers = preflight.get("blockers", [])
            reason = categories if categories else blockers
            raise AgentAPIError(
                409, "mission_preflight_blocked",
                "Mission cannot enter autonomous local execution: "
                + (", ".join(str(item) for item in reason) or "not_ready"),
            )
        if start:
            try:
                focus = read_execution_focus(self.company.home)
                enforce_execution_focus(
                    focus, preflight.get("project_id"),
                    preflight.get("team", {}).get("roles", []), "agent-api mission",
                )
                if isinstance(self.company.model, OllamaModel):
                    enforce_execution_resource_envelope(
                        focus, "ollama", self.company.model.num_ctx,
                        self.company.model.num_predict, self.company.model.keep_alive,
                        "agent-api mission",
                    )
            except (RuntimeError, TypeError, ValueError) as exc:
                raise AgentAPIError(
                    409, "mission_focus_blocked",
                    "Local execution focus or resource envelope is not ready; inspect focus locally",
                ) from exc
        if self.worker is None:
            raise AgentAPIError(503, "worker_disabled", "Local agent worker is disabled")
        try:
            queue_id = self.company.enqueue(
                objective, project.strip() if isinstance(project, str) else None,
                playbook=playbook, priority=priority, source="agent-api",
            )
        except ValueError as exc:
            raise AgentAPIError(400, "mission_submission_invalid", str(exc)) from exc
        started = False
        start_reason: str | None = None
        if start:
            next_due = self.company.next_due_queue_item()
            if next_due is None or str(next_due[0]) != queue_id:
                start_reason = "another_due_mission_is_ahead"
            else:
                try:
                    self.worker.start(queue_id)
                    started = True
                except (RuntimeError, ValueError):
                    start_reason = "worker_busy_or_mission_changed"
        return AgentAPIResponse(202, {
            "schema": AGENT_SUBMISSION_SCHEMA,
            "status": "accepted",
            "missionId": queue_id,
            "profile": profile,
            "queued": True,
            "started": started,
            "startReason": start_reason,
            "preflight": {
                "status": preflight.get("status"),
                "roles": preflight.get("team", {}).get("roles", []),
                "knowledge": preflight.get("knowledge"),
            },
            "externalActionsPerformed": False,
        })

    def run_mission(self, mission_id: str, payload: object) -> AgentAPIResponse:
        value = _require_mapping(payload)
        _strict_keys(value, {"confirmation"})
        if value.get("confirmation") != MISSION_RUN_CONFIRMATION:
            raise AgentAPIError(
                400, "run_confirmation_required",
                f"Run requires confirmation={MISSION_RUN_CONFIRMATION!r}",
            )
        if self.worker is None:
            raise AgentAPIError(503, "worker_disabled", "Local agent worker is disabled")
        row = self._queue_row(self.company, mission_id)
        if row[1] != "queued":
            raise AgentAPIError(409, "mission_not_queued", "Only a queued mission can start")
        try:
            self.worker.start(mission_id)
        except (RuntimeError, ValueError) as exc:
            raise AgentAPIError(409, "mission_start_blocked", str(exc)) from exc
        return AgentAPIResponse(202, {
            "schema": AGENT_MISSION_SCHEMA,
            "missionId": mission_id,
            "status": "running",
            "externalActionsPerformed": False,
        })

    def computer_workflows(self) -> dict[str, object]:
        result = list_workflows(self.company.home)
        return {
            **result,
            "apiMode": "supervised-local-only",
            "networkAppsAllowedByApi": False,
            "shellsAllowedByApi": False,
            "physicalConcurrentExecutions": 1,
        }

    def computer_preview(self, name: str) -> dict[str, object]:
        try:
            return preview_workflow(
                self.company.home, name, allow_network_apps=False, allow_shells=False,
            )
        except ValueError as exc:
            raise AgentAPIError(404, "workflow_not_found_or_invalid", str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise AgentAPIError(409, "workflow_preview_blocked", str(exc)) from exc

    def run_computer_workflow(self, name: str, payload: object) -> AgentAPIResponse:
        value = _require_mapping(payload)
        _strict_keys(value, {"confirmation", "captureEvidence"})
        if value.get("confirmation") != RUN_CONFIRMATION:
            raise AgentAPIError(
                400, "computer_confirmation_required",
                f"Run requires confirmation={RUN_CONFIRMATION!r}",
            )
        capture = value.get("captureEvidence", True)
        if type(capture) is not bool:
            raise AgentAPIError(400, "invalid_capture_evidence", "captureEvidence must be a boolean")
        if self.worker is None:
            raise AgentAPIError(503, "worker_disabled", "Local agent worker is disabled")
        preview = self.computer_preview(name)
        if preview.get("status") != "ready":
            raise AgentAPIError(
                409, "workflow_preview_blocked",
                ", ".join(str(item) for item in preview.get("blockers", [])) or "not_ready",
            )
        required_inputs = preview.get("requiredSecretInputs", [])
        if required_inputs:
            raise AgentAPIError(
                409, "interactive_input_required",
                "This workflow needs private interactive values and must run from the local terminal",
            )

        def operation() -> dict[str, object]:
            return run_workflow(
                self.company.home, name, RUN_CONFIRMATION,
                allow_network_apps=False, allow_shells=False, capture_evidence=capture,
            )

        try:
            result = self.worker.run_exclusive("computer-use", name, operation)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AgentAPIError(409, "computer_run_blocked", str(exc)) from exc
        return AgentAPIResponse(200, {
            "schema": AGENT_COMPUTER_RUN_SCHEMA,
            "status": result.get("status", "unknown"),
            "workflow": name,
            "receipt": result,
            "networkAppsAllowed": False,
            "shellsAllowed": False,
        })

    def get(self, path: str) -> AgentAPIResponse:
        if path == "/api/v1/catalog":
            return AgentAPIResponse(200, self.catalog())
        if path == "/api/v1/missions":
            return AgentAPIResponse(200, self.missions())
        if match := re.fullmatch(r"/api/v1/missions/([0-9a-f]{12})", path):
            return AgentAPIResponse(200, self.mission(match.group(1)))
        if path == "/api/v1/computer-workflows":
            return AgentAPIResponse(200, self.computer_workflows())
        if match := re.fullmatch(r"/api/v1/computer-workflows/([^/]+)/preview", path):
            return AgentAPIResponse(200, self.computer_preview(_clean_workflow_name(match.group(1))))
        raise AgentAPIError(404, "endpoint_not_found", "Agent API endpoint was not found")

    def post(self, path: str, payload: object) -> AgentAPIResponse:
        if path == "/api/v1/missions":
            return self.submit(payload)
        if match := re.fullmatch(r"/api/v1/missions/([0-9a-f]{12})/run", path):
            return self.run_mission(match.group(1), payload)
        if match := re.fullmatch(r"/api/v1/computer-workflows/([^/]+)/run", path):
            return self.run_computer_workflow(_clean_workflow_name(match.group(1)), payload)
        raise AgentAPIError(404, "endpoint_not_found", "Agent API endpoint was not found")


def decode_json_body(payload: bytes) -> object:
    if not payload or len(payload) > MAX_JSON_BYTES:
        raise AgentAPIError(413, "invalid_body_size", "JSON body is empty or too large")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"Unsupported JSON constant: {value}")

    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise AgentAPIError(400, "malformed_json", "Request body is not strict JSON") from exc


def _read_service_connection(home: Path) -> tuple[str, str]:
    path = home / "service.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("Local agent server is not configured; run local-agent.cmd up") from exc
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise RuntimeError("Local agent server state is invalid")
    try:
        state = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Local agent server state is invalid") from exc
    if not isinstance(state, dict) or state.get("status") != "running":
        raise RuntimeError("Local agent server is not running; run local-agent.cmd up")
    port = state.get("port")
    token = state.get("token")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError("Local agent server state has an invalid port")
    if not isinstance(token, str) or SERVICE_TOKEN_PATTERN.fullmatch(token) is None:
        raise RuntimeError("Local agent server state has an invalid token")
    return f"http://127.0.0.1:{port}", token


def _client_request(
    home: Path, method: str, path: str, payload: dict[str, object] | None = None,
    *, timeout: float = 30,
) -> dict[str, object]:
    base, token = _read_service_connection(home)
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_JSON_BYTES + 1)
            try:
                problem = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                problem = {"status": "error", "error": f"http_{exc.code}"}
            raise RuntimeError(json.dumps(problem, sort_keys=True)) from exc
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Local agent server is unavailable: {exc}") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise RuntimeError("Local agent server response is too large")
    try:
        result = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Local agent server returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Local agent server returned an invalid result")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-agent",
        description=(
            "Use the authenticated SuperMega local agent server. Queue many logical "
            "knowledge workers; one physical Ollama/computer worker runs at a time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  local-agent.cmd up
  local-agent.cmd catalog
  local-agent.cmd ask decision-brief "Compare two local options" --project MyProject
  local-agent.cmd submit decision-brief "Compare two local options"
  local-agent.cmd submit product-build "Design an offline workflow" --start
  local-agent.cmd missions
  local-agent.cmd wait MISSION_ID
  local-agent.cmd computer list
  local-agent.cmd computer preview WORKFLOW_NAME

The server is an orchestration layer over a local Ollama model, not a newly
trained foundation model. Codex/OpenCode clients can also use company-mcp.cmd.
No endpoint sends, spends, deploys, publishes, or enables network computer use.
""",
    )
    parser.add_argument("--home", type=Path, default=default_company_home())
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="Start or reuse the one loopback agent server")
    up.add_argument("--port", type=int, default=8765)
    up.add_argument("--model", default=DEFAULT_LOCAL_MODEL)
    up.add_argument("--num-ctx", type=int, default=4096)
    up.add_argument("--num-predict", type=int, default=768)
    sub.add_parser("down", help="Stop the exact owned loopback agent server")
    sub.add_parser("server-status", help="Check process and endpoint identity")
    sub.add_parser("catalog", help="List deployable knowledge teams and computer workflows")
    sub.add_parser("missions", help="List local agent missions")
    ask = sub.add_parser("ask", help="Run one local mission and show only an accepted draft")
    ask.add_argument("profile", choices=("auto", *sorted(PLAYBOOKS)))
    ask.add_argument("objective")
    ask.add_argument("--project")
    ask.add_argument("--priority", type=int, default=50)
    ask.add_argument("--timeout", type=int, default=900)
    submit = sub.add_parser("submit", help="Queue one local knowledge-worker mission")
    submit.add_argument("profile", choices=("auto", *sorted(PLAYBOOKS)))
    submit.add_argument("objective")
    submit.add_argument("--project")
    submit.add_argument("--priority", type=int, default=50)
    submit.add_argument("--start", action="store_true")
    status = sub.add_parser("status", help="Read one mission and its result")
    status.add_argument("mission_id")
    run = sub.add_parser("run", help="Start the next reviewed queued mission")
    run.add_argument("mission_id")
    wait = sub.add_parser("wait", help="Wait for one mission to reach a terminal state")
    wait.add_argument("mission_id")
    wait.add_argument("--timeout", type=int, default=900)
    computer = sub.add_parser("computer", help="List, preview, or run sealed computer workflows")
    computer_sub = computer.add_subparsers(dest="computer_command", required=True)
    computer_sub.add_parser("list")
    preview = computer_sub.add_parser("preview")
    preview.add_argument("name")
    computer_run = computer_sub.add_parser("run")
    computer_run.add_argument("name")
    computer_run.add_argument("--no-evidence", action="store_true")
    return parser


def _wait_for_mission(home: Path, mission_id: str, timeout: int) -> dict[str, object]:
    if timeout < 1 or timeout > 86_400:
        raise ValueError("--timeout must be between 1 and 86400 seconds")
    if MISSION_ID_PATTERN.fullmatch(mission_id) is None:
        raise RuntimeError("Local agent server returned an invalid mission ID")
    deadline = time.monotonic() + timeout
    while True:
        result = _client_request(home, "GET", f"/api/v1/missions/{mission_id}", timeout=30)
        if result.get("missionId") != mission_id:
            raise RuntimeError("Local agent server returned the wrong mission")
        if result.get("terminal") is True or time.monotonic() >= deadline:
            return result
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.command in {"up", "down", "server-status"}:
            from .service import service_status, start_service, stop_service

            try:
                current = service_status(home)
            except ValueError:
                current = None
            if args.command == "up":
                result = (
                    current
                    if current is not None and current.get("live") is True
                    else start_service(
                        home, args.port, "ollama", args.model,
                        args.num_ctx, args.num_predict, "0s",
                    )
                )
            elif args.command == "down":
                if current is None:
                    raise ValueError("Local agent server is not configured")
                result = stop_service(home) if current.get("live") is True else current
            else:
                if current is None:
                    raise ValueError("Local agent server is not configured")
                result = current
        elif args.command == "catalog":
            result = _client_request(home, "GET", "/api/v1/catalog")
        elif args.command == "missions":
            result = _client_request(home, "GET", "/api/v1/missions")
        elif args.command == "ask":
            accepted = _client_request(home, "POST", "/api/v1/missions", {
                "profile": args.profile,
                "objective": args.objective,
                "project": args.project,
                "priority": args.priority,
                "start": True,
                "runConfirmation": MISSION_RUN_CONFIRMATION,
            })
            mission_id = accepted.get("missionId")
            if not isinstance(mission_id, str) or MISSION_ID_PATTERN.fullmatch(mission_id) is None:
                raise RuntimeError("Local agent server returned an invalid mission ID")
            print(f"Mission {mission_id} accepted.", flush=True)
            if accepted.get("started") is not True:
                print(
                    "Queued, but not started: "
                    f"{accepted.get('startReason') or 'worker_not_available'}. "
                    f"Inspect with local-agent.cmd status {mission_id}."
                )
                return 1
            mission = _wait_for_mission(home, mission_id, args.timeout)
            if mission.get("terminal") is not True:
                print(f"Mission {mission_id} is still running; inspect it with local-agent.cmd status {mission_id}.")
                return 1
            result_detail = mission.get("result")
            synthesis = (
                result_detail.get("synthesis") if isinstance(result_detail, dict) else None
            )
            quality = result_detail.get("quality") if isinstance(result_detail, dict) else None
            safety = result_detail.get("safety") if isinstance(result_detail, dict) else None
            if not (
                mission.get("status") == "complete"
                and mission.get("resultAvailable") is True
                and mission.get("resultUsable") is True
                and isinstance(result_detail, dict)
                and result_detail.get("synthesisTruncated") is False
                and isinstance(synthesis, str) and synthesis.strip()
                and isinstance(quality, dict) and quality.get("passed") is True
                and isinstance(safety, dict) and safety.get("passed") is True
            ):
                alerts = safety.get("alerts", []) if isinstance(safety, dict) else []
                print(
                    f"Mission {mission_id} ended as {mission.get('status', 'unknown')}; "
                    "no accepted draft is available."
                )
                if alerts:
                    print("Safety review flags: " + ", ".join(str(item) for item in alerts))
                print(f"Inspect with local-agent.cmd status {mission_id}; owner review required.")
                return 1
            print(f"Mission {mission_id}: local draft for owner review.\n")
            print(synthesis)
            print("\nThis is a model draft, not independently verified fact.")
            return 0
        elif args.command == "submit":
            result = _client_request(home, "POST", "/api/v1/missions", {
                "profile": args.profile,
                "objective": args.objective,
                "project": args.project,
                "priority": args.priority,
                "start": args.start,
                **({"runConfirmation": MISSION_RUN_CONFIRMATION} if args.start else {}),
            })
        elif args.command == "status":
            result = _client_request(home, "GET", f"/api/v1/missions/{args.mission_id}")
        elif args.command == "run":
            result = _client_request(home, "POST", f"/api/v1/missions/{args.mission_id}/run", {
                "confirmation": MISSION_RUN_CONFIRMATION,
            })
        elif args.command == "wait":
            result = _wait_for_mission(home, args.mission_id, args.timeout)
            if result.get("terminal") is not True:
                print(json.dumps(result, indent=2))
                print("ERROR: timed out waiting for local mission", file=sys.stderr)
                return 1
        elif args.computer_command == "list":
            result = _client_request(home, "GET", "/api/v1/computer-workflows")
        elif args.computer_command == "preview":
            name = urllib.parse.quote(args.name, safe="")
            result = _client_request(home, "GET", f"/api/v1/computer-workflows/{name}/preview")
        else:
            name = urllib.parse.quote(args.name, safe="")
            result = _client_request(home, "POST", f"/api/v1/computer-workflows/{name}/run", {
                "confirmation": RUN_CONFIRMATION,
                "captureEvidence": not args.no_evidence,
            }, timeout=3600)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
