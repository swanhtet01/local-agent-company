from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .select_local_code_model import GIB, available_memory_bytes
except ImportError:
    from select_local_code_model import GIB, available_memory_bytes


SCHEMA = "local-ai.lmstudio-code.v1"
MODEL_KEY = "qwen/qwen3.5-4b"
MODEL_IDENTIFIER = "supermega-qwen35-4b"
OPENCODE_MODEL = f"lmstudio/{MODEL_IDENTIFIER}"
BASE_URL = "http://127.0.0.1:1234/v1"
MINIMUM_AVAILABLE_BYTES = 5 * GIB
DEFAULT_LMS = Path.home() / ".lmstudio" / "bin" / "lms.exe"
DEFAULT_OPENCODE = Path(r"C:\Users\thesw\tools\node-v24.18.0-win-x64\opencode.cmd")
DEFAULT_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
MAX_OUTPUT_BYTES = 256_000


def _command(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=timeout,
    )
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES or len(completed.stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise RuntimeError("local_runtime_output_too_large")
    return completed


def _json_command(command: list[str], *, timeout: int = 30) -> Any:
    completed = _command(command, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError("local_runtime_command_failed")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("local_runtime_json_invalid") from exc


def _safe_executable(path: Path, suffixes: set[str]) -> Path:
    supplied = path.absolute()
    if supplied.is_symlink() or not supplied.is_file():
        raise ValueError("local_runtime_executable_invalid")
    resolved = supplied.resolve(strict=True)
    if resolved.suffix.casefold() not in suffixes:
        raise ValueError("local_runtime_executable_invalid")
    return resolved


def _project(path: Path) -> Path:
    supplied = path.absolute()
    if supplied.is_symlink() or not supplied.is_dir():
        raise ValueError("project_invalid")
    return supplied.resolve(strict=True)


def _model_installed(lms: Path) -> bool:
    value = _json_command([str(lms), "ls", "--llm", "--json"])
    if not isinstance(value, list) or len(value) > 128:
        raise RuntimeError("lmstudio_inventory_invalid")
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("modelKey"), str):
            raise RuntimeError("lmstudio_inventory_invalid")
    return any(item["modelKey"] == MODEL_KEY for item in value)


def _server_status(lms: Path) -> dict[str, Any]:
    value = _json_command([str(lms), "server", "status", "--json", "--quiet"])
    if not isinstance(value, dict) or type(value.get("running")) is not bool:
        raise RuntimeError("lmstudio_server_status_invalid")
    if value["running"] and value.get("port") not in {None, 1234}:
        raise RuntimeError("lmstudio_server_port_unexpected")
    return value


def _loaded_models(lms: Path) -> list[dict[str, Any]]:
    value = _json_command([str(lms), "ps", "--json"])
    if not isinstance(value, list) or len(value) > 16 or any(not isinstance(item, dict) for item in value):
        raise RuntimeError("lmstudio_loaded_models_invalid")
    return value


def _ollama_model_loaded() -> bool:
    executable = shutil.which("ollama")
    if executable is None:
        return False
    completed = _command([executable, "ps"], timeout=15)
    if completed.returncode != 0:
        raise RuntimeError("ollama_runtime_state_invalid")
    return bool(completed.stdout.splitlines()[1:])


def _validate_opencode_config(path: Path) -> None:
    supplied = path.absolute()
    if supplied.is_symlink() or not supplied.is_file() or supplied.stat().st_size > MAX_OUTPUT_BYTES:
        raise ValueError("opencode_lmstudio_config_invalid")
    try:
        value = json.loads(supplied.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("opencode_lmstudio_config_invalid") from exc
    provider = value.get("provider", {}).get("lmstudio") if isinstance(value, dict) else None
    if (
        not isinstance(provider, dict)
        or provider.get("npm") != "@ai-sdk/openai-compatible"
        or provider.get("options") != {"baseURL": BASE_URL}
        or not isinstance(provider.get("models"), dict)
        or MODEL_IDENTIFIER not in provider["models"]
    ):
        raise ValueError("opencode_lmstudio_config_invalid")


def _receipt(*, status: str, reason: str, available: int, **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "status": status, "reason": reason,
        "runtime": "lmstudio", "modelKey": MODEL_KEY,
        "modelIdentifier": MODEL_IDENTIFIER,
        "availableMemoryBytes": available,
        "minimumAvailableBytes": MINIMUM_AVAILABLE_BYTES,
        "projectPathReturned": False, "paidApiRequired": False,
        "loopbackOnly": True, "corsEnabled": False,
        "networkListenerStarted": False, "modelLoaded": False,
        "opencodeStarted": False, "cleanupConfirmed": False,
        **extra,
    }


def readiness(
    project: Path, lms: Path, opencode: Path, config: Path, available: int,
) -> tuple[Path, Path, Path]:
    if type(available) is not int or available < 0:
        raise ValueError("available_memory_invalid")
    root = _project(project)
    trusted_lms = _safe_executable(lms, {".exe"})
    trusted_opencode = _safe_executable(opencode, {".cmd", ".exe"})
    _validate_opencode_config(config)
    if _ollama_model_loaded():
        raise ValueError("ollama_model_already_loaded")
    if _server_status(trusted_lms)["running"]:
        raise ValueError("lmstudio_server_already_running")
    if _loaded_models(trusted_lms):
        raise ValueError("lmstudio_model_already_loaded")
    if not _model_installed(trusted_lms):
        raise ValueError("lmstudio_quality_model_not_installed")
    if available < MINIMUM_AVAILABLE_BYTES:
        raise ValueError("lmstudio_quality_model_memory_blocked")
    return root, trusted_lms, trusted_opencode


def _cleanup(lms: Path, *, load_attempted: bool, server_started: bool) -> tuple[bool, bool]:
    model_unloaded = not load_attempted
    server_stopped = not server_started
    if load_attempted:
        try:
            unloaded = _command([str(lms), "unload", MODEL_IDENTIFIER], timeout=60)
            model_unloaded = unloaded.returncode == 0 and not _loaded_models(lms)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            model_unloaded = False
    if server_started:
        try:
            stopped = _command([str(lms), "server", "stop"], timeout=30)
            server_stopped = stopped.returncode == 0 and not _server_status(lms)["running"]
        except (OSError, RuntimeError, subprocess.SubprocessError):
            server_stopped = False
    return model_unloaded, server_stopped


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run OpenCode on the existing LM Studio Qwen 3.5 4B model.")
    result.add_argument("--lmstudio", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--check", action="store_true")
    result.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    result.add_argument("--lms", type=Path, default=DEFAULT_LMS)
    result.add_argument("--opencode", type=Path, default=DEFAULT_OPENCODE)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = time.perf_counter()
    available = 0
    lms: Path | None = None
    server_started = False
    load_attempted = False
    try:
        available = available_memory_bytes()
        root, lms, opencode = readiness(
            args.project, args.lms, args.opencode, args.config, available,
        )
        if args.check:
            print(json.dumps(_receipt(
                status="ready", reason="quality_model_admitted", available=available,
                modelInstalled=True, opencodeConfigured=True,
            ), separators=(",", ":"), sort_keys=True))
            return 0
        server = _command([
            str(lms), "server", "start", "--port", "1234", "--bind", "127.0.0.1",
        ], timeout=30)
        if server.returncode != 0 or not _server_status(lms)["running"]:
            raise RuntimeError("lmstudio_server_start_failed")
        server_started = True
        load_attempted = True
        loaded = _command([
            str(lms), "load", MODEL_KEY, "--identifier", MODEL_IDENTIFIER,
            "--context-length", "4096", "--parallel", "1", "--ttl", "60", "--yes",
        ], timeout=180)
        loaded_values = _loaded_models(lms)
        if (
            loaded.returncode != 0
            or not any(item.get("identifier") == MODEL_IDENTIFIER for item in loaded_values)
        ):
            raise RuntimeError("lmstudio_model_load_failed")
        agent = subprocess.run(
            [str(opencode), ".", "--model", OPENCODE_MODEL], cwd=root, check=False,
        )
        model_unloaded, server_stopped = _cleanup(
            lms, load_attempted=load_attempted, server_started=server_started,
        )
        load_attempted = False
        server_started = False
        cleanup = model_unloaded and server_stopped
        print(json.dumps(_receipt(
            status="finished" if agent.returncode == 0 and cleanup else "attention",
            reason=(
                "agent_closed_cleanly" if agent.returncode == 0 and cleanup else
                "cleanup_failed" if not cleanup else "agent_failed"
            ),
            available=available, networkListenerStarted=True, modelLoaded=True,
            opencodeStarted=True, cleanupConfirmed=cleanup,
            modelUnloadedAfterRun=model_unloaded, serverStoppedAfterRun=server_stopped,
            agentExitCode=agent.returncode,
            wallSeconds=round(time.perf_counter() - started, 3),
        ), separators=(",", ":"), sort_keys=True))
        return 0 if agent.returncode == 0 and cleanup else 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        model_unloaded = not load_attempted
        server_stopped = not server_started
        if lms is not None and (load_attempted or server_started):
            model_unloaded, server_stopped = _cleanup(
                lms, load_attempted=load_attempted, server_started=server_started,
            )
        load_attempted = False
        server_started = False
        print(json.dumps(_receipt(
            status="blocked", reason=str(error), available=available,
            cleanupConfirmed=model_unloaded and server_stopped,
            modelUnloadedAfterRun=model_unloaded,
            serverStoppedAfterRun=server_stopped,
            wallSeconds=round(time.perf_counter() - started, 3),
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if lms is not None and (load_attempted or server_started):
            _cleanup(lms, load_attempted=load_attempted, server_started=server_started)


if __name__ == "__main__":
    raise SystemExit(main())
