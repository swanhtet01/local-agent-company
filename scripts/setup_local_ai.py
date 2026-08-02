from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SCHEMA = "local-ai.setup.v1"
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
AGENT_MODEL = "qwen3.5:0.8b"
QUALITY_MODEL = "qwen3.5:4b"
ASK_MODEL = "qwen2.5-coder:0.5b"
STARTER_PROJECT = "Local AI Product Lab"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 256_000
MANAGED_LAUNCHER_MARKER = "rem Managed by SuperMega Local AI setup v1"

AGENT_PROMPT = (
    "Use only the local_company_company MCP router. Call it with one action and "
    "input object. Start with status, then use projects, playbooks, queue_list, "
    "schedule_list, product_evidence_status, and preflight as relevant. Use "
    "product_evidence_next to find a sealed unreviewed job, then job_result to "
    "show it before asking for real human measurements. Create a project only "
    "after showing its name and purpose and receiving the literal confirmation "
    "required by project_create. Add project knowledge only when the user supplied "
    "or approved the exact content and gives the literal knowledge_add confirmation; "
    "use knowledge_search to preview retrieval. Queue at most one internal mission "
    "only when explicitly requested and preflight is ready. Run at most one mission "
    "only after showing its exact queue ID and receiving RUN ONE LOCAL COMPANY "
    "MISSION. Create or change a schedule only after showing its cadence and "
    "receiving the corresponding literal confirmation. Record product evidence "
    "only from the user's actual human decision and measurements after showing the "
    "exact sealed job ID and receiving the literal review confirmation; never invent "
    "acceptance, corrections, memory, or paid interest. Import a headless receipt "
    "with product_experiment_review only after validating the exact receipt, showing "
    "its measured outcome, and receiving the literal experiment-review confirmation. "
    "Use jobs and job_result for actual outcomes. Never claim deployment, publication, "
    "customer contact, payment, credentials, or hosted changes."
)

DENIED_AGENT_PERMISSIONS = {
    "read": "deny", "edit": "deny", "glob": "deny", "grep": "deny",
    "list": "deny", "bash": "deny", "task": "deny",
    "external_directory": "deny", "webfetch": "deny", "websearch": "deny",
}


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class SetupPaths:
    root: Path
    config: Path
    company_home: Path
    desktop: Path | None


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


def _absolute(path: Path, reason: str) -> Path:
    if not path.is_absolute():
        raise SetupError(reason)
    return Path(os.path.abspath(path))


def _validate_existing_directory(path: Path, reason: str) -> Path:
    absolute = _absolute(path, reason)
    try:
        if _is_link_or_reparse(absolute):
            raise SetupError(reason)
        metadata = absolute.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SetupError(reason)
        return absolute.resolve(strict=True)
    except SetupError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SetupError(reason) from error


def _validate_creatable_directory(path: Path, reason: str) -> Path:
    absolute = _absolute(path, reason)
    current = absolute
    missing: list[str] = []
    while not current.exists():
        if current == current.parent:
            raise SetupError(reason)
        missing.append(current.name)
        current = current.parent
    base = _validate_existing_directory(current, reason)
    for name in reversed(missing):
        base /= name
    return base


def _validate_paths(
    root: Path, config: Path, company_home: Path, desktop: Path | None,
) -> SetupPaths:
    trusted_root = _validate_existing_directory(root, "setup_root_invalid")
    for name in (
        "company-mcp.cmd", "local-ai-menu.cmd", "local-ai.cmd",
        "local-company-agent.cmd",
    ):
        candidate = trusted_root / name
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except OSError as error:
            raise SetupError("setup_root_incomplete") from error
        if _is_link_or_reparse(candidate) or not stat.S_ISREG(metadata.st_mode):
            raise SetupError("setup_root_incomplete")

    config_target = _absolute(config, "opencode_config_path_invalid")
    if config_target.name.lower() != "opencode.json":
        raise SetupError("opencode_config_filename_invalid")
    config_parent = _validate_creatable_directory(
        config_target.parent, "opencode_config_parent_invalid",
    )
    config_target = config_parent / config_target.name
    if config_target.exists() and _is_link_or_reparse(config_target):
        raise SetupError("opencode_config_file_unsafe")

    state_home = _validate_creatable_directory(
        company_home, "company_home_invalid",
    )
    if state_home.exists() and _is_link_or_reparse(state_home):
        raise SetupError("company_home_invalid")

    desktop_target = None
    if desktop is not None:
        desktop_target = _validate_existing_directory(
            desktop, "desktop_directory_invalid",
        )
    return SetupPaths(trusted_root, config_target, state_home, desktop_target)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("opencode_config_duplicate_key")
        result[key] = value
    return result


def _load_config(path: Path) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            _is_link_or_reparse(path) or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 2 or metadata.st_size > MAX_CONFIG_BYTES
        ):
            raise SetupError("opencode_config_file_unsafe")
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except SetupError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SetupError("opencode_config_invalid") from error
    if not isinstance(value, dict):
        raise SetupError("opencode_config_invalid")
    return value, raw


def _dict(value: Any, reason: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SetupError(reason)
    return value


def _normalized_command_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _desired_mcp(root: Path) -> dict[str, Any]:
    return {
        "type": "local",
        "command": [
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
            "/d", "/c", str(root / "company-mcp.cmd"),
        ],
        "enabled": True,
        "timeout": 900_000,
        "environment": {"LOCAL_COMPANY_MCP_PROFILE": "compact"},
    }


def _desired_agent() -> dict[str, Any]:
    return {
        "description": (
            "Inspect and coordinate the governed local AI company without shell "
            "or external actions."
        ),
        "mode": "primary",
        "model": f"ollama/{AGENT_MODEL}",
        "prompt": AGENT_PROMPT,
        "permission": dict(DENIED_AGENT_PERMISSIONS),
        "tools": {"local_company_*": True},
    }


def _merge_config(existing: dict[str, Any], root: Path) -> tuple[dict[str, Any], list[str]]:
    merged = copy.deepcopy(existing)
    changes: list[str] = []
    schema = merged.get("$schema")
    if schema not in {None, OPENCODE_SCHEMA}:
        raise SetupError("opencode_schema_conflict")
    if schema is None:
        merged["$schema"] = OPENCODE_SCHEMA
        changes.append("schema_added")

    disabled = merged.get("disabled_providers")
    if disabled is not None:
        if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
            raise SetupError("opencode_disabled_providers_invalid")
        if "ollama" in disabled:
            raise SetupError("ollama_provider_disabled")
    enabled = merged.get("enabled_providers")
    if enabled is not None:
        if not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled):
            raise SetupError("opencode_enabled_providers_invalid")
        if "ollama" not in enabled:
            merged["enabled_providers"] = [*enabled, "ollama"]
            changes.append("ollama_enabled")

    providers = _dict(merged.get("provider"), "opencode_provider_config_invalid")
    merged["provider"] = providers
    ollama = _dict(providers.get("ollama"), "ollama_provider_config_invalid")
    if ollama:
        if ollama.get("npm") not in {None, "@ai-sdk/openai-compatible"}:
            raise SetupError("ollama_provider_package_conflict")
        options = ollama.get("options")
        if options is not None and options != {"baseURL": OLLAMA_BASE_URL}:
            raise SetupError("ollama_provider_endpoint_conflict")
        models = _dict(ollama.get("models"), "ollama_models_config_invalid")
    else:
        models = {}
    desired_models = {
        AGENT_MODEL: {"name": "Qwen 3.5 0.8B - fast local"},
        ASK_MODEL: {"name": "Qwen 2.5 Coder 0.5B - drafting only (no agent tools)"},
        QUALITY_MODEL: {"name": "Qwen 3.5 4B - local coding"},
    }
    for name, value in desired_models.items():
        if models.get(name) != value:
            models[name] = value
            changes.append(f"model_registered:{name}")
    desired_ollama = {
        **ollama,
        "npm": "@ai-sdk/openai-compatible",
        "name": "Ollama (local only)",
        "options": {"baseURL": OLLAMA_BASE_URL},
        "models": models,
    }
    if providers.get("ollama") != desired_ollama:
        providers["ollama"] = desired_ollama
        changes.append("ollama_provider_configured")

    mcp = _dict(merged.get("mcp"), "opencode_mcp_config_invalid")
    merged["mcp"] = mcp
    desired_mcp = _desired_mcp(root)
    current_mcp = mcp.get("local_company")
    if current_mcp is not None:
        if not isinstance(current_mcp, dict):
            raise SetupError("local_company_mcp_conflict")
        command = current_mcp.get("command")
        if (
            not isinstance(command, list) or len(command) != 4
            or any(not isinstance(item, str) for item in command)
            or command[1:3] != ["/d", "/c"]
            or _normalized_command_path(command[0])
            != _normalized_command_path(desired_mcp["command"][0])
            or _normalized_command_path(command[3])
            != _normalized_command_path(desired_mcp["command"][3])
        ):
            raise SetupError("local_company_mcp_conflict")
        # Preserve the existing spelling/casing of semantically identical paths.
        desired_mcp["command"][0] = command[0]
        desired_mcp["command"][3] = command[3]
    if current_mcp != desired_mcp:
        mcp["local_company"] = desired_mcp
        changes.append("local_company_mcp_configured")

    tools = _dict(merged.get("tools"), "opencode_tools_config_invalid")
    merged["tools"] = tools
    if tools.get("local_company_*") is not False:
        tools["local_company_*"] = False
        changes.append("global_company_tools_denied")

    agents = _dict(merged.get("agent"), "opencode_agent_config_invalid")
    merged["agent"] = agents
    desired_agent = _desired_agent()
    if agents.get("local-company") != desired_agent:
        agents["local-company"] = desired_agent
        changes.append("local_company_agent_configured")

    if "model" not in merged:
        merged["model"] = f"ollama/{AGENT_MODEL}"
        changes.append("default_model_added")
    if "small_model" not in merged:
        merged["small_model"] = f"ollama/{AGENT_MODEL}"
        changes.append("small_model_added")
    return merged, changes


def _encoded_config(config: dict[str, Any]) -> bytes:
    try:
        encoded = (json.dumps(
            config, ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SetupError("opencode_config_not_serializable") from error
    if len(encoded) > MAX_CONFIG_BYTES:
        raise SetupError("opencode_config_output_too_large")
    return encoded


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(path.parent) or (path.exists() and _is_link_or_reparse(path)):
        raise SetupError("setup_write_target_unsafe")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SetupError("setup_atomic_write_failed") from error


def _backup_config(path: Path, raw: bytes | None) -> tuple[Path | None, bool]:
    if raw is None:
        return None, False
    digest = hashlib.sha256(raw).hexdigest()
    backup = path.with_name(f"{path.name}.supermega-backup-{digest[:12]}")
    if backup.exists():
        try:
            if _is_link_or_reparse(backup) or backup.read_bytes() != raw:
                raise SetupError("opencode_config_backup_collision")
        except OSError as error:
            raise SetupError("opencode_config_backup_invalid") from error
        return backup, False
    try:
        with backup.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise SetupError("opencode_config_backup_failed") from error
    return backup, True


def _launcher_payloads(root: Path) -> dict[str, bytes]:
    menu = str(root / "local-ai-menu.cmd")

    def content(extra: str) -> bytes:
        return (
            "@echo off\r\n"
            f"{MANAGED_LAUNCHER_MARKER}\r\n"
            "setlocal\r\n"
            f"call \"{menu}\"{extra}\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        ).encode("utf-8")

    return {
        "SuperMega Local AI Lab.cmd": content(""),
        "SuperMega AI Workbench.cmd": content(" --supermega"),
    }


def _legacy_launcher_payloads(root: Path) -> dict[str, tuple[bytes, bytes]]:
    menu = str(root / "local-ai-menu.cmd")
    lab = (
        "@echo off\n"
        f"call \"{menu}\"\n"
        "exit /b %ERRORLEVEL%\n"
    )
    workbench = (
        "@echo off\n"
        "setlocal\n"
        "set \"SUPERMEGA_AI=%USERPROFILE%\\Projects\\local-agent-company\\local-ai-menu.cmd\"\n"
        "if not exist \"%SUPERMEGA_AI%\" (\n"
        "  echo ERROR: SuperMega Local AI is not installed in the expected Projects folder.\n"
        "  pause\n"
        "  exit /b 1\n"
        ")\n"
        "call \"%SUPERMEGA_AI%\" --supermega\n"
        "exit /b %ERRORLEVEL%\n"
    )

    def newline_variants(value: str) -> tuple[bytes, bytes]:
        return value.encode("utf-8"), value.replace("\n", "\r\n").encode("utf-8")

    return {
        "SuperMega Local AI Lab.cmd": newline_variants(lab),
        "SuperMega AI Workbench.cmd": newline_variants(workbench),
    }


def _launcher_plan(desktop: Path | None, root: Path) -> tuple[dict[str, str], list[str]]:
    if desktop is None:
        return {}, []
    statuses: dict[str, str] = {}
    conflicts: list[str] = []
    for name, expected in _launcher_payloads(root).items():
        path = desktop / name
        if not path.exists():
            statuses[name] = "create"
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
            if _is_link_or_reparse(path) or not stat.S_ISREG(metadata.st_mode):
                raise SetupError("desktop_launcher_unsafe")
            current = path.read_bytes()
        except OSError as error:
            raise SetupError("desktop_launcher_unavailable") from error
        if current == expected:
            statuses[name] = "current"
        elif current in _legacy_launcher_payloads(root)[name]:
            statuses[name] = "adopt"
        elif current.lower().startswith(
            ("@echo off\r\n" + MANAGED_LAUNCHER_MARKER).encode("utf-8").lower()
        ):
            statuses[name] = "update"
        else:
            statuses[name] = "conflict"
            conflicts.append(name)
    return statuses, conflicts


def _write_launchers(desktop: Path | None, root: Path, plan: dict[str, str]) -> int:
    if desktop is None:
        return 0
    changed = 0
    for name, payload in _launcher_payloads(root).items():
        if plan.get(name) in {"adopt", "create", "update"}:
            _atomic_write(desktop / name, payload)
            changed += 1
    return changed


def _state_status(home: Path) -> dict[str, Any]:
    database = home / "company.db"
    if not database.exists():
        return {"initialized": False, "projectCount": 0, "focusEnabled": False}
    try:
        if _is_link_or_reparse(database):
            raise SetupError("company_database_unsafe")
        metadata = database.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise SetupError("company_database_invalid")
        source = str(Path(__file__).resolve(strict=True).parents[1] / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        from local_company.config import read_validated_company_instance_id
        from local_company.focus import read_execution_focus

        uri = database.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            read_validated_company_instance_id(connection)
            row = connection.execute("SELECT COUNT(*) FROM projects").fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise SetupError("company_database_invalid")
        focus = read_execution_focus(home)
        return {
            "initialized": True, "projectCount": row[0],
            "focusEnabled": focus.get("enabled") is True,
        }
    except SetupError:
        raise
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        raise SetupError("company_database_invalid") from error


def _initialize_state(home: Path) -> tuple[bool, bool]:
    source = str(Path(__file__).resolve(strict=True).parents[1] / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.core import Company, MockModel
    from local_company.focus import set_execution_focus

    before = _state_status(home)
    if before["initialized"]:
        return False, False
    home.mkdir(parents=True, exist_ok=True)
    company = Company(home.resolve(strict=True), MockModel())
    company.initialize()
    project_id = company.create_project(
        STARTER_PROJECT,
        "Private local product, workflow, and agent experimentation.",
    )
    set_execution_focus(home, project_id, STARTER_PROJECT, 4)
    observed = _state_status(home)
    if (
        observed["initialized"] is not True
        or observed["projectCount"] != 1
        or observed["focusEnabled"] is not True
    ):
        raise SetupError("company_initialization_verification_failed")
    return True, True


def _dependency_status() -> dict[str, Any]:
    ollama = shutil.which("ollama")
    opencode = shutil.which("opencode.cmd") or shutil.which("opencode")
    models: set[str] = set()
    service_ready = False
    if ollama:
        try:
            completed = subprocess.run(
                [ollama, "list"], check=False, capture_output=True, text=True,
                timeout=15,
            )
            if completed.returncode == 0 and len(completed.stdout) <= MAX_PROCESS_OUTPUT_BYTES:
                service_ready = True
                for line in completed.stdout.splitlines()[1:]:
                    fields = line.split()
                    if fields:
                        models.add(fields[0].lower())
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "pythonReady": sys.version_info >= (3, 11),
        "pythonVersion": ".".join(str(item) for item in sys.version_info[:3]),
        "ollamaInstalled": ollama is not None,
        "ollamaServiceReady": service_ready,
        "openCodeInstalled": opencode is not None,
        "agentModelInstalled": AGENT_MODEL in models,
        "askModelInstalled": ASK_MODEL in models,
        "qualityModelInstalled": QUALITY_MODEL in models,
    }


def _actions(
    mode: str, changes: list[str], state: dict[str, Any],
    launchers: dict[str, str], dependencies: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if mode != "apply" and changes:
        actions.append("run_setup_apply")
    if mode != "apply" and state["initialized"] is not True:
        actions.append("initialize_local_company")
    if mode != "apply" and any(
        value in {"adopt", "create", "update"} for value in launchers.values()
    ):
        actions.append("create_or_refresh_desktop_launchers")
    if dependencies["ollamaInstalled"] is not True:
        actions.append("install_ollama")
    elif dependencies["ollamaServiceReady"] is not True:
        actions.append("start_ollama_locally")
    if dependencies["openCodeInstalled"] is not True:
        actions.append("install_opencode")
    if dependencies["agentModelInstalled"] is not True:
        actions.append(f"ollama_pull_{AGENT_MODEL}")
    if dependencies["askModelInstalled"] is not True:
        actions.append(f"ollama_pull_{ASK_MODEL}")
    if state["initialized"] is True and state["projectCount"] > 0 and state["focusEnabled"] is not True:
        actions.append("select_active_project")
    return actions


def _ready(
    changes: list[str], state: dict[str, Any], launchers: dict[str, str],
    dependencies: dict[str, Any],
) -> bool:
    return (
        not changes
        and state["initialized"] is True
        and state["projectCount"] > 0
        and state["focusEnabled"] is True
        and all(value == "current" for value in launchers.values())
        and dependencies["pythonReady"] is True
        and dependencies["ollamaInstalled"] is True
        and dependencies["ollamaServiceReady"] is True
        and dependencies["openCodeInstalled"] is True
        and dependencies["agentModelInstalled"] is True
        and dependencies["askModelInstalled"] is True
    )


def run_setup(
    mode: str, paths: SetupPaths,
    dependency_probe: Callable[[], dict[str, Any]] = _dependency_status,
) -> tuple[int, dict[str, Any]]:
    existing, raw = _load_config(paths.config)
    merged, changes = _merge_config(existing, paths.root)
    encoded = _encoded_config(merged)
    launcher_status, launcher_conflicts = _launcher_plan(paths.desktop, paths.root)
    if launcher_conflicts:
        raise SetupError("desktop_launcher_conflict")
    state = _state_status(paths.company_home)
    dependencies = dependency_probe()
    required_dependency_keys = {
        "pythonReady", "pythonVersion", "ollamaInstalled", "ollamaServiceReady",
        "openCodeInstalled", "agentModelInstalled", "askModelInstalled",
        "qualityModelInstalled",
    }
    if not isinstance(dependencies, dict) or set(dependencies) != required_dependency_keys:
        raise SetupError("dependency_probe_invalid")

    backup: Path | None = None
    backup_created = False
    config_written = False
    launchers_written = 0
    state_initialized = False
    starter_created = False
    if mode == "apply":
        if changes or raw is None:
            backup, backup_created = _backup_config(paths.config, raw)
            _atomic_write(paths.config, encoded)
            config_written = True
        launchers_written = _write_launchers(
            paths.desktop, paths.root, launcher_status,
        )
        if not state["initialized"]:
            state_initialized, starter_created = _initialize_state(paths.company_home)

        current, _ = _load_config(paths.config)
        _, remaining_changes = _merge_config(current, paths.root)
        if remaining_changes:
            raise SetupError("opencode_config_verification_failed")
        changes = []
        launcher_status, launcher_conflicts = _launcher_plan(paths.desktop, paths.root)
        if launcher_conflicts or any(
            value != "current" for value in launcher_status.values()
        ):
            raise SetupError("desktop_launcher_verification_failed")
        state = _state_status(paths.company_home)

    ready = _ready(changes, state, launcher_status, dependencies)
    actions = _actions(mode, changes, state, launcher_status, dependencies)
    status = (
        "ready" if ready else
        "preview" if mode == "preview" else
        "configured_attention" if mode == "apply" else
        "attention"
    )
    receipt = {
        "schema": SCHEMA, "ok": True, "status": status, "mode": mode,
        "ready": ready,
        "config": {
            "existsBefore": raw is not None,
            "changesNeeded": list(changes),
            "written": config_written,
            "backupCreated": backup_created,
            "backupName": backup.name if backup is not None else None,
            "unrelatedSettingsPreserved": True,
        },
        "companyState": {
            **state, "initializedBySetup": state_initialized,
            "starterProjectCreated": starter_created,
        },
        "desktopLaunchers": {
            "enabled": paths.desktop is not None,
            "statuses": launcher_status,
            "written": launchers_written,
        },
        "dependencies": dependencies,
        "actions": actions,
        "effects": {
            "stateMutated": mode == "apply" and (
                config_written or launchers_written > 0 or state_initialized
            ),
            "modelsPulled": 0,
            "applicationsInstalled": 0,
            "externalNetworkRequests": 0,
            "paidApiUsed": False,
            "externalActionPerformed": False,
            "credentialValuesReturned": False,
        },
    }
    return (0 if mode in {"preview", "apply"} or ready else 1), receipt


def _default_desktop() -> Path:
    if os.name == "nt":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(32_768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
                candidate = Path(buffer.value)
                if candidate.is_absolute() and candidate.is_dir():
                    return candidate
        except (AttributeError, OSError, ValueError):
            pass
    return Path.home() / "Desktop"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Preview, verify, or apply one local-only SuperMega AI setup.",
    )
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    result.add_argument("--config", type=Path)
    result.add_argument("--company-home", type=Path)
    result.add_argument("--desktop-dir", type=Path)
    result.add_argument("--no-desktop", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    mode = "apply" if args.apply else "check" if args.check else "preview"
    try:
        if args.no_desktop and args.desktop_dir is not None:
            raise SetupError("desktop_arguments_conflict")
        root = Path(__file__).resolve(strict=True).parents[1]
        config = args.config or Path.home() / ".config" / "opencode" / "opencode.json"
        if args.company_home is not None:
            company_home = args.company_home
        else:
            source = str(root / "src")
            if source not in sys.path:
                sys.path.insert(0, source)
            from local_company.config import default_company_home
            company_home = default_company_home()
        desktop = None if args.no_desktop else (args.desktop_dir or _default_desktop())
        paths = _validate_paths(root, config, company_home, desktop)
        code, receipt = run_setup(mode, paths)
        print(json.dumps(
            receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ))
        return code
    except SetupError as error:
        print(json.dumps({
            "schema": SCHEMA, "ok": False, "status": "blocked",
            "mode": mode, "reason": str(error),
            "effects": {
                "modelsPulled": 0, "applicationsInstalled": 0,
                "externalNetworkRequests": 0, "paidApiUsed": False,
                "externalActionPerformed": False,
                "credentialValuesReturned": False,
            },
        }, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
