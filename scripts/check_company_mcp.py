"""Prove the bundled governed company MCP server over its real stdio launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .stamp_build_manifest import ManifestError, check_project, parse_build_id
else:
    from stamp_build_manifest import ManifestError, check_project, parse_build_id


SCHEMA = "local-company.mcp-self-test.v1"
PROTOCOL_VERSION = "2025-06-18"
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 4 * 1024
DEFAULT_TIMEOUT_SECONDS = 15
EXPECTED_ACTIONS = frozenset({
    "status", "projects", "preflight", "queue_list", "queue_add", "queue_run",
    "queue_park", "queue_unpark", "jobs", "job_result", "schedule_list",
    "schedule_create", "schedule_set_enabled", "playbooks", "project_create",
    "project_overview", "knowledge_list", "knowledge_add", "knowledge_search",
    "product_evidence_status", "product_evidence_review", "product_evidence_next",
    "product_experiment_next", "product_offer_next", "product_experiment_review",
})
EXPECTED_LAUNCHER = (
    "@echo off\n"
    "setlocal\n"
    "set \"COMPANY_ROOT=%~dp0\"\n"
    "set \"COMPANY_PY=%COMPANY_ROOT%.venv\\Scripts\\python.exe\"\n"
    "if not exist \"%COMPANY_PY%\" set \"COMPANY_PY=python\"\n"
    "set \"PYTHONPATH=%COMPANY_ROOT%src;%PYTHONPATH%\"\n"
    "\"%COMPANY_PY%\" -m local_company.mcp_server\n"
    "exit /b %ERRORLEVEL%\n"
)


class McpSelfTestError(RuntimeError):
    pass


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction) and is_junction(path):
        return True
    try:
        return bool(getattr(path.lstat(), "st_reparse_tag", 0))
    except FileNotFoundError:
        return False


def _normal_file(path: Path, reason: str) -> Path:
    try:
        if _is_link_or_reparse(path):
            raise McpSelfTestError(reason)
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise McpSelfTestError(reason)
        return path.resolve(strict=True)
    except McpSelfTestError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise McpSelfTestError(reason) from error


def _validated_root(project_root: Path) -> Path:
    try:
        if not project_root.is_absolute() or _is_link_or_reparse(project_root):
            raise McpSelfTestError("project_root_invalid")
        metadata = project_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise McpSelfTestError("project_root_invalid")
        root = project_root.resolve(strict=True)
    except McpSelfTestError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise McpSelfTestError("project_root_invalid") from error
    launcher = _normal_file(root / "company-mcp.cmd", "company_mcp_launcher_invalid")
    try:
        payload = launcher.read_bytes()
    except OSError as error:
        raise McpSelfTestError("company_mcp_launcher_invalid") from error
    expected_lf = EXPECTED_LAUNCHER.encode("utf-8")
    expected_crlf = EXPECTED_LAUNCHER.replace("\n", "\r\n").encode("utf-8")
    if payload not in {expected_lf, expected_crlf}:
        raise McpSelfTestError("company_mcp_launcher_invalid")
    _normal_file(root / "src" / "local_company" / "mcp_server.py", "company_mcp_source_invalid")
    return root


def _validated_manifest(
    root: Path, manifest_checker: Callable[[Path], dict[str, Any]],
) -> tuple[str, str]:
    try:
        value = manifest_checker(root)
        if not isinstance(value, dict) or value.get("status") != "ok":
            raise McpSelfTestError("mcp_build_manifest_invalid")
        build_id = value.get("build_id")
        source_digest = value.get("source_sha256")
        if not isinstance(build_id, str):
            raise McpSelfTestError("mcp_build_manifest_invalid")
        parse_build_id(build_id)
        if (
            not isinstance(source_digest, str) or len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)
        ):
            raise McpSelfTestError("mcp_build_manifest_invalid")
        return build_id, source_digest
    except McpSelfTestError:
        raise
    except (ManifestError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise McpSelfTestError("mcp_build_manifest_invalid") from error


def _command(root: Path) -> list[str]:
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        command = _normal_file(
            system_root / "System32" / "cmd.exe", "windows_command_host_invalid",
        )
        return [str(command), "/d", "/c", str(root / "company-mcp.cmd")]
    return [sys.executable, "-m", "local_company.mcp_server"]


def _environment(root: Path, ephemeral_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["LOCAL_COMPANY_HOME"] = str(ephemeral_home)
    environment["LOCAL_COMPANY_MCP_PROFILE"] = "compact"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONNOUSERSITE"] = "1"
    if os.name != "nt":
        current = environment.get("PYTHONPATH")
        source = str(root / "src")
        environment["PYTHONPATH"] = source if not current else source + os.pathsep + current
    for name in ("PYTHONINSPECT", "PYTHONSTARTUP", "PYTHONWARNINGS"):
        environment.pop(name, None)
    return environment


def _requests() -> bytes:
    requests = (
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "supermega-mcp-self-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    return b"".join(
        (json.dumps(request, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        for request in requests
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("nonstandard_json_constant")


def _response_map(stdout: bytes) -> dict[int, dict[str, Any]]:
    if not stdout or len(stdout) > MAX_STDOUT_BYTES or not stdout.endswith(b"\n"):
        raise McpSelfTestError("mcp_stdout_invalid")
    try:
        lines = stdout.splitlines()
        values = [
            json.loads(
                line.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            for line in lines
        ]
    except (UnicodeError, ValueError, RecursionError) as error:
        raise McpSelfTestError("mcp_response_invalid") from error
    if len(values) != 3 or any(not isinstance(value, dict) for value in values):
        raise McpSelfTestError("mcp_response_count_invalid")
    responses: dict[int, dict[str, Any]] = {}
    for value in values:
        if set(value) != {"jsonrpc", "id", "result"} or value.get("jsonrpc") != "2.0":
            raise McpSelfTestError("mcp_response_shape_invalid")
        identifier = value.get("id")
        if type(identifier) is not int or identifier not in {1, 2, 3} or identifier in responses:
            raise McpSelfTestError("mcp_response_id_invalid")
        responses[identifier] = value
    if set(responses) != {1, 2, 3}:
        raise McpSelfTestError("mcp_response_id_invalid")
    return responses


def _validate_initialize(response: dict[str, Any]) -> None:
    result = response["result"]
    if not isinstance(result, dict):
        raise McpSelfTestError("mcp_initialize_invalid")
    server = result.get("serverInfo")
    capabilities = result.get("capabilities")
    if (
        result.get("protocolVersion") != PROTOCOL_VERSION
        or not isinstance(server, dict)
        or server.get("name") != "local-agent-company"
        or not isinstance(capabilities, dict)
        or capabilities.get("tools") != {"listChanged": False}
    ):
        raise McpSelfTestError("mcp_initialize_invalid")


def _validate_tools(response: dict[str, Any]) -> tuple[int, str]:
    result = response["result"]
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or len(tools) != 1:
        raise McpSelfTestError("mcp_compact_profile_invalid")
    tool = tools[0]
    if not isinstance(tool, dict) or tool.get("name") != "company":
        raise McpSelfTestError("mcp_compact_profile_invalid")
    schema = tool.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    action = properties.get("action") if isinstance(properties, dict) else None
    actions = action.get("enum") if isinstance(action, dict) else None
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or schema.get("required") != ["action"]
        or not isinstance(actions, list)
        or any(not isinstance(value, str) for value in actions)
        or len(actions) != len(EXPECTED_ACTIONS)
        or set(actions) != EXPECTED_ACTIONS
    ):
        raise McpSelfTestError("mcp_action_contract_invalid")
    encoded = json.dumps(tool, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return len(actions), hashlib.sha256(encoded).hexdigest()


def check_company_mcp(
    project_root: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    manifest_checker: Callable[[Path], dict[str, Any]] = check_project,
) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise McpSelfTestError("timeout_invalid")
    root = _validated_root(project_root)
    build_id, source_digest = _validated_manifest(root, manifest_checker)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="supermega-mcp-self-test-") as directory:
        ephemeral_home = Path(directory) / "company"
        try:
            completed = runner(
                _command(root),
                input=_requests(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=root,
                env=_environment(root, ephemeral_home),
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise McpSelfTestError("mcp_process_timeout") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise McpSelfTestError("mcp_process_unavailable") from error
        if type(completed.returncode) is not int or completed.returncode != 0:
            raise McpSelfTestError("mcp_process_failed")
        if not isinstance(completed.stdout, bytes) or not isinstance(completed.stderr, bytes):
            raise McpSelfTestError("mcp_process_output_invalid")
        if completed.stderr or len(completed.stderr) > MAX_STDERR_BYTES:
            raise McpSelfTestError("mcp_stderr_not_empty")
        responses = _response_map(completed.stdout)
        _validate_initialize(responses[1])
        if responses[2]["result"] != {}:
            raise McpSelfTestError("mcp_ping_invalid")
        action_count, tool_digest = _validate_tools(responses[3])
        if ephemeral_home.exists():
            raise McpSelfTestError("mcp_self_test_mutated_company_state")
    duration_ms = max(0, round((time.monotonic() - started) * 1000))
    return {
        "schema": SCHEMA,
        "status": "ready",
        "ready": True,
        "transport": "stdio",
        "profile": "compact",
        "serverName": "local-agent-company",
        "buildId": build_id,
        "sourceSha256": source_digest,
        "operationalSourceVerified": True,
        "protocolVersion": PROTOCOL_VERSION,
        "processExited": True,
        "processExitCode": 0,
        "roundTripCompleted": True,
        "pingReady": True,
        "toolCount": 1,
        "actionCount": action_count,
        "toolSchemaSha256": tool_digest,
        "durationMs": duration_ms,
        "modelCalled": False,
        "networkListenerOpened": False,
        "persistentStateMutated": False,
        "externalActionPerformed": False,
        "paidApiUsed": False,
        "credentialValuesReturned": False,
    }


def blocked_receipt(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "blocked",
        "ready": False,
        "reason": reason,
        "transport": "stdio",
        "profile": "compact",
        "modelCalled": False,
        "networkListenerOpened": False,
        "persistentStateMutated": False,
        "externalActionPerformed": False,
        "paidApiUsed": False,
        "credentialValuesReturned": False,
    }


def run_company_mcp_self_test(
    project_root: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    manifest_checker: Callable[[Path], dict[str, Any]] = check_project,
) -> tuple[int, dict[str, Any]]:
    try:
        return 0, check_company_mcp(
            project_root, timeout_seconds=timeout_seconds, runner=runner,
            manifest_checker=manifest_checker,
        )
    except McpSelfTestError as error:
        return 1, blocked_receipt(str(error))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return 2, blocked_receipt("mcp_self_test_internal_error")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run a bounded, model-free stdio proof of the company MCP server.",
    )
    result.add_argument("--project-root", type=Path)
    result.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.project_root or Path(__file__).resolve(strict=True).parents[1]
    code, receipt = run_company_mcp_self_test(
        root, timeout_seconds=args.timeout_seconds,
    )
    output = sys.stdout if code == 0 else sys.stderr
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True), file=output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
