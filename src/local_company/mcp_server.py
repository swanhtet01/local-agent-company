from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .capacity import observe_memory
from .config import default_company_home
from .core import Company, MAX_OBJECTIVE_CHARS, MockModel, PLAYBOOKS
from .focus import enforce_execution_focus, execution_focus_digest, read_execution_focus


PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
PROTOCOL_VERSION = "2025-06-18"
MAX_MESSAGE_BYTES = 1_048_576
MAX_TOOL_CALLS = 128
SCHEMA = "local-company.mcp-capabilities.v1"
MINIMUM_EXECUTION_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
RUN_CONFIRMATION = "RUN ONE LOCAL COMPANY MISSION"
QUEUE_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
QUEUE_COMPLETION_PATTERN = re.compile(
    r"^Queue item ([0-9a-f]{12}) completed as job ([0-9a-f]{12}); quality=(passed|failed)$",
    re.MULTILINE,
)
QUEUE_STATUSES = {
    "queued", "running", "complete", "failed", "quality_failed", "needs_approval",
    "cancelled", "superseded",
}
JOB_STATUSES = {"running", "complete", "failed", "interrupted"}
MAX_SYNTHESIS_CHARS = 32_768


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[Any], dict[str, Any]]
    read_only: bool

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": False,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


def _schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": required, "properties": properties,
    }


def _arguments(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value).issubset(allowed):
        raise ProtocolError(-32602, "invalid_arguments")
    return value


class CompanyTools:
    def __init__(
        self, home: Path,
        executor: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.company = Company(home.resolve(), MockModel())
        self.executor = executor or self._execute_next

    def status(self, arguments: Any) -> dict[str, Any]:
        _arguments(arguments, set())
        self.company.initialize()
        focus = read_execution_focus(self.company.home)
        queue = self.company.queue_preflight()
        schedules = self.company.schedules()
        work = self.company.work_state_snapshot()
        return {
            "schema": SCHEMA, "status": "ready", "transport": "stdio",
            "localOnly": True, "networkListener": False, "exposedTools": 8,
            "modelCalled": False, "externalActionPerformed": False,
            "focus": {
                "enabled": focus["enabled"], "projectId": focus.get("projectId"),
                "projectName": focus.get("projectName"), "maxRoles": focus.get("maxRoles"),
                "digest": execution_focus_digest(focus),
            },
            "work": work,
            "queue": {
                "status": queue["status"], "queueId": queue["queue_id"],
                "blockers": queue["blockers"],
                "ownerGateCategories": queue["owner_gate_categories"],
                "modelExecutionReady": queue["model_execution_ready"],
            },
            "schedules": {
                "count": len(schedules),
                "enabled": sum(1 for row in schedules if row[2]),
            },
        }

    def projects(self, arguments: Any) -> dict[str, Any]:
        _arguments(arguments, set())
        rows = self.company.projects()
        return {
            "schema": "local-company.mcp-projects.v1", "status": "ready",
            "projects": [
                {"id": row[0], "name": row[1], "createdAt": row[2], "missionCount": row[3]}
                for row in rows
            ],
            "count": len(rows), "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }

    def preflight(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(arguments, {"objective", "project", "playbook"})
        objective = value.get("objective")
        project = value.get("project")
        playbook = value.get("playbook")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > MAX_OBJECTIVE_CHARS:
            raise ProtocolError(-32602, "invalid_objective")
        if project is not None and (not isinstance(project, str) or not project.strip() or len(project) > 80):
            raise ProtocolError(-32602, "invalid_project")
        if playbook is not None and playbook not in PLAYBOOKS:
            raise ProtocolError(-32602, "invalid_playbook")
        result = self.company.mission_preflight(objective, project, playbook)
        return {**result, "externalActionPerformed": False}

    def queue_list(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(arguments, {"status", "limit"})
        status = value.get("status")
        limit = value.get("limit", 20)
        if status is not None and status not in QUEUE_STATUSES:
            raise ProtocolError(-32602, "invalid_queue_status")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ProtocolError(-32602, "invalid_limit")
        rows = self.company.queue_items(status)[:limit]
        return {
            "schema": "local-company.mcp-queue-list.v1", "status": "ready",
            "items": [
                {
                    "queueId": row[0], "status": row[1], "priority": row[2],
                    "scheduledAt": row[3], "project": row[4] or None,
                    "playbook": row[5] or None, "objective": row[6],
                    "jobId": row[7] or None, "error": row[8] or None,
                }
                for row in rows
            ],
            "count": len(rows), "limit": limit, "modelCalled": False,
            "stateMutated": False, "externalActionPerformed": False,
        }

    def jobs(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(arguments, {"status", "limit"})
        status = value.get("status")
        limit = value.get("limit", 20)
        if status is not None and status not in JOB_STATUSES:
            raise ProtocolError(-32602, "invalid_job_status")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ProtocolError(-32602, "invalid_limit")
        rows = [row for row in self.company.jobs() if status is None or row[1] == status][:limit]
        return {
            "schema": "local-company.mcp-jobs.v1", "status": "ready",
            "jobs": [
                {"jobId": row[0], "status": row[1], "createdAt": row[2], "objective": row[3]}
                for row in rows
            ],
            "count": len(rows), "limit": limit, "modelCalled": False,
            "stateMutated": False, "externalActionPerformed": False,
        }

    def job_result(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(arguments, {"jobId"})
        job_id = value.get("jobId")
        if not isinstance(job_id, str) or QUEUE_ID_PATTERN.fullmatch(job_id) is None:
            raise ProtocolError(-32602, "invalid_job_id")
        try:
            detail = self.company.job_detail(job_id)
        except ValueError as error:
            raise ProtocolError(-32602, "unknown_job") from error
        job = detail["job"]
        evaluation = detail.get("evaluation")
        synthesis = job[7] if isinstance(job[7], str) else ""
        truncated = len(synthesis) > MAX_SYNTHESIS_CHARS
        result = {
            "schema": "local-company.mcp-job-result.v1", "status": "ready",
            "job": {
                "jobId": job[0], "objective": job[1], "status": job[2],
                "createdAt": job[3], "parentJobId": job[5], "project": job[6],
                "roles": [row[1] for row in detail["assignments"]],
                "synthesis": synthesis[:MAX_SYNTHESIS_CHARS], "synthesisTruncated": truncated,
                "reportSha256": job[8], "evidenceManifestSha256": job[9],
                "reportAvailable": bool(detail.get("report")),
            },
            "quality": None,
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }
        if isinstance(evaluation, dict):
            checks = evaluation.get("checks")
            result["quality"] = {
                "passed": evaluation.get("passed") is True,
                "score": evaluation.get("score"),
                "evaluatedAt": evaluation.get("evaluated_at"),
                "evaluatorVersion": evaluation.get("evaluator_version"),
                "checks": checks if isinstance(checks, dict) else {},
            }
        return result

    def queue_add(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(
            arguments, {"objective", "project", "playbook", "priority", "queueConfirmed"},
        )
        if value.get("queueConfirmed") is not True:
            raise ProtocolError(-32602, "queue_confirmation_required")
        priority = value.get("priority", 50)
        if type(priority) is not int or not 0 <= priority <= 100:
            raise ProtocolError(-32602, "invalid_priority")
        preflight = self.preflight({
            key: value[key] for key in ("objective", "project", "playbook") if key in value
        })
        if preflight["status"] != "ready" or preflight["model_execution_ready"] is not True:
            return {
                "schema": "local-company.mcp-queue-add.v1", "status": "blocked",
                "queued": False, "preflight": preflight,
                "modelCalled": False, "externalActionPerformed": False,
            }
        focus = read_execution_focus(self.company.home)
        enforce_execution_focus(
            focus, preflight["project_id"], preflight["team"]["roles"], "mcp queue_add",
        )
        queue_id = self.company.enqueue(
            value["objective"], value.get("project"), playbook=value.get("playbook"),
            priority=priority, source="mcp",
        )
        return {
            "schema": "local-company.mcp-queue-add.v1", "status": "queued",
            "queued": True, "queueId": queue_id, "preflight": preflight,
            "modelCalled": False, "externalActionPerformed": False,
        }

    def _execute_next(self, queue_id: str) -> dict[str, Any]:
        memory = observe_memory()
        available = memory.get("available_bytes")
        if memory.get("status") != "ready" or type(available) is not int:
            return {
                "status": "blocked", "reason": "available_memory_unavailable",
                "minimumAvailableBytes": MINIMUM_EXECUTION_MEMORY_BYTES,
                "modelCalled": False,
            }
        if available < MINIMUM_EXECUTION_MEMORY_BYTES:
            return {
                "status": "blocked", "reason": "insufficient_available_memory",
                "availableMemoryBytes": available,
                "minimumAvailableBytes": MINIMUM_EXECUTION_MEMORY_BYTES,
                "memoryShortfallBytes": MINIMUM_EXECUTION_MEMORY_BYTES - available,
                "modelCalled": False,
            }
        root = Path(__file__).resolve(strict=True).parents[2]
        environment = os.environ.copy()
        source = str(root / "src")
        current = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = source if not current else os.pathsep.join((source, current))
        try:
            completed = subprocess.run(
                [
                    sys.executable, "-m", "local_company.cli", "--home", str(self.company.home),
                    "queue", "run-next", "--queue-id", queue_id,
                    "--provider", "ollama", "--model", os.getenv("LOCAL_COMPANY_MODEL", "qwen3.5:0.8b"),
                    "--num-ctx", "4096", "--num-predict", "512", "--keep-alive", "0s",
                ],
                cwd=root, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=840, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": "execution_timeout", "modelCalled": True}
        if len(completed.stdout) > 262_144 or len(completed.stderr) > 262_144:
            return {"status": "error", "reason": "execution_output_too_large", "modelCalled": None}
        match = QUEUE_COMPLETION_PATTERN.search(completed.stdout)
        if completed.returncode not in {0, 1} or match is None or match.group(1) != queue_id:
            return {
                "status": "error", "reason": "queue_execution_failed",
                "processExitCode": completed.returncode, "modelCalled": None,
            }
        job_id = match.group(2)
        detail = self.company.job_detail(job_id)
        evaluation = detail.get("evaluation")
        checks = evaluation.get("checks", {}) if isinstance(evaluation, dict) else {}
        passed = bool(isinstance(evaluation, dict) and evaluation.get("passed") is True)
        return {
            "status": "completed" if passed else "quality_failed", "queueId": queue_id,
            "jobId": job_id, "qualityPassed": passed,
            "qualityScore": evaluation.get("score") if isinstance(evaluation, dict) else None,
            "modelCalled": True,
            "modelUnloadedAfterRun": checks.get("model_stopped_cleanly") is True,
        }

    def queue_run(self, arguments: Any) -> dict[str, Any]:
        value = _arguments(arguments, {"expectedQueueId", "runConfirmation"})
        queue_id = value.get("expectedQueueId")
        if not isinstance(queue_id, str) or QUEUE_ID_PATTERN.fullmatch(queue_id) is None:
            raise ProtocolError(-32602, "invalid_queue_id")
        if value.get("runConfirmation") != RUN_CONFIRMATION:
            raise ProtocolError(-32602, "run_confirmation_required")
        preflight = self.company.queue_preflight(queue_id)
        ready = (
            preflight["status"] == "ready"
            and preflight["queue_id"] == queue_id
            and preflight["reviewed_queue_matches"] is True
            and preflight["model_execution_ready"] is True
            and preflight["owner_gate_categories"] == []
        )
        if not ready:
            return {
                "schema": "local-company.mcp-queue-run.v1", "status": "blocked",
                "reason": "bound_preflight_not_ready", "preflight": preflight,
                "modelCalled": False, "externalActionPerformed": False,
            }
        focus = read_execution_focus(self.company.home)
        enforce_execution_focus(
            focus, preflight["project_id"], preflight["team"]["roles"], "mcp queue_run",
        )
        result = self.executor(queue_id)
        return {
            "schema": "local-company.mcp-queue-run.v1", **result,
            "externalActionPerformed": False, "paidApiUsed": False,
        }


def _tools(company_tools: CompanyTools) -> tuple[Tool, ...]:
    objective = {"type": "string", "minLength": 1, "maxLength": MAX_OBJECTIVE_CHARS}
    project = {"type": "string", "minLength": 1, "maxLength": 80}
    playbook = {"type": "string", "enum": sorted(PLAYBOOKS)}
    return (
        Tool("status", "Report pathless local-company, focus, queue, and schedule state.", _schema([], {}), company_tools.status, True),
        Tool("projects", "List pathless local-company project identities and mission counts.", _schema([], {}), company_tools.projects, True),
        Tool(
            "queue_list", "List bounded local mission queue records without changing state.",
            _schema([], {
                "status": {"type": "string", "enum": sorted(QUEUE_STATUSES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }),
            company_tools.queue_list, True,
        ),
        Tool(
            "jobs", "List bounded local mission history without reading report files into the client.",
            _schema([], {
                "status": {"type": "string", "enum": sorted(JOB_STATUSES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }),
            company_tools.jobs, True,
        ),
        Tool(
            "job_result", "Read one pathless synthesis and quality receipt by exact job ID.",
            _schema(["jobId"], {
                "jobId": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
            }),
            company_tools.job_result, True,
        ),
        Tool(
            "preflight", "Preview one team, knowledge state, and owner gates without queuing or calling a model.",
            _schema(["objective"], {"objective": objective, "project": project, "playbook": playbook}),
            company_tools.preflight, True,
        ),
        Tool(
            "queue_add", "Queue one confirmed internal mission only after a current safe preflight.",
            _schema(
                ["objective", "queueConfirmed"],
                {
                    "objective": objective, "project": project, "playbook": playbook,
                    "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                    "queueConfirmed": {"const": True},
                },
            ),
            company_tools.queue_add, False,
        ),
        Tool(
            "queue_run", "Run exactly one reviewed, gate-cleared local mission using Ollama.",
            _schema(
                ["expectedQueueId", "runConfirmation"],
                {
                    "expectedQueueId": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                    "runConfirmation": {"const": RUN_CONFIRMATION},
                },
            ),
            company_tools.queue_run, False,
        ),
    )


class McpSession:
    def __init__(self, home: Path) -> None:
        self.company_tools = CompanyTools(home)
        self.tools = _tools(self.company_tools)
        self.by_name = {tool.name: tool for tool in self.tools}
        self.initialized = False
        self.protocol_version: str | None = None
        self.calls = 0

    @staticmethod
    def _response(identifier: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier}
        value["error" if error is not None else "result"] = error if error is not None else result
        return value

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._response(None, error={"code": -32600, "message": "invalid_request"})
        identifier = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return self._response(identifier, error={"code": -32600, "message": "invalid_request"})
        if identifier is None:
            if method == "notifications/initialized" and self.protocol_version is not None:
                self.initialized = True
            return None
        try:
            if method == "initialize":
                params = request.get("params")
                if self.protocol_version is not None or not isinstance(params, dict):
                    raise ProtocolError(-32600, "invalid_initialize")
                requested = params.get("protocolVersion")
                if not isinstance(requested, str):
                    raise ProtocolError(-32602, "invalid_protocol_version")
                self.protocol_version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSION
                return self._response(identifier, {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "local-agent-company", "title": "Local Agent Company", "version": "1"},
                    "instructions": "Local-only company coordination. Review queue_list and preflight before mutations. queue_run requires the exact reviewed queue ID and owner confirmation. Use jobs and job_result to inspect outcomes. External actions are never exposed.",
                })
            if not self.initialized:
                raise ProtocolError(-32002, "server_not_initialized")
            if method == "ping":
                return self._response(identifier, {})
            if method == "tools/list":
                return self._response(identifier, {"tools": [tool.listing() for tool in self.tools]})
            if method == "tools/call":
                self.calls += 1
                if self.calls > MAX_TOOL_CALLS:
                    raise ProtocolError(-32000, "tool_call_limit_exceeded")
                params = request.get("params")
                if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"}:
                    raise ProtocolError(-32602, "invalid_tool_call")
                name = params.get("name")
                tool = self.by_name.get(name) if isinstance(name, str) else None
                if tool is None:
                    raise ProtocolError(-32602, "unknown_tool")
                result = tool.handler(params.get("arguments", {}))
                return self._response(identifier, {
                    "content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"), sort_keys=True)}],
                    "structuredContent": result, "isError": False,
                })
            raise ProtocolError(-32601, "method_not_found")
        except ProtocolError as error:
            return self._response(identifier, error={"code": error.code, "message": error.message})
        except (OSError, RuntimeError, ValueError):
            return self._response(identifier, error={"code": -32603, "message": "local_company_operation_failed"})


def serve(input_stream: BinaryIO, output_stream: BinaryIO, *, home: Path | None = None) -> int:
    session = McpSession(home or default_company_home())
    while True:
        line = input_stream.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            response = McpSession._response(None, error={"code": -32700, "message": "parse_error"})
        else:
            try:
                response = session.handle(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = McpSession._response(None, error={"code": -32700, "message": "parse_error"})
        if response is not None:
            output_stream.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
            output_stream.flush()


def main() -> int:
    return serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
