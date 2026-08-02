from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .config import default_company_home
from .core import Company, MAX_OBJECTIVE_CHARS, MockModel, PLAYBOOKS
from .focus import enforce_execution_focus, execution_focus_digest, read_execution_focus


PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
PROTOCOL_VERSION = "2025-06-18"
MAX_MESSAGE_BYTES = 1_048_576
MAX_TOOL_CALLS = 128
SCHEMA = "local-company.mcp-capabilities.v1"


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
    def __init__(self, home: Path) -> None:
        self.company = Company(home.resolve(), MockModel())

    def status(self, arguments: Any) -> dict[str, Any]:
        _arguments(arguments, set())
        self.company.initialize()
        focus = read_execution_focus(self.company.home)
        queue = self.company.queue_preflight()
        schedules = self.company.schedules()
        work = self.company.work_state_snapshot()
        return {
            "schema": SCHEMA, "status": "ready", "transport": "stdio",
            "localOnly": True, "networkListener": False, "exposedTools": 4,
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


def _tools(company_tools: CompanyTools) -> tuple[Tool, ...]:
    objective = {"type": "string", "minLength": 1, "maxLength": MAX_OBJECTIVE_CHARS}
    project = {"type": "string", "minLength": 1, "maxLength": 80}
    playbook = {"type": "string", "enum": sorted(PLAYBOOKS)}
    return (
        Tool("status", "Report pathless local-company, focus, queue, and schedule state.", _schema([], {}), company_tools.status, True),
        Tool("projects", "List pathless local-company project identities and mission counts.", _schema([], {}), company_tools.projects, True),
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
                    "instructions": "Local-only company coordination. Preflight before queueing. No model execution or external actions are exposed.",
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
