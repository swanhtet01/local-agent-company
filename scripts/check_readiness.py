"""Report whether the local SuperMega components are ready for a new mission."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_company.config import (  # noqa: E402
    COMPANY_STORE_SCHEMA, default_company_home,
    read_validated_company_instance_id, valid_company_instance_id,
)
from local_company.model_policy import (  # noqa: E402
    DEFAULT_LOCAL_MODEL, is_supported_local_model, require_local_llama_model,
)

if __package__:
    from .check_live_build import (
        LiveBuildError, compare_live_build, fetch_health, reject_json_constant,
        unique_json_object,
    )
    from .stamp_build_manifest import ManifestError, check_project
else:
    from check_live_build import (
        LiveBuildError, compare_live_build, fetch_health, reject_json_constant,
        unique_json_object,
    )
    from stamp_build_manifest import ManifestError, check_project


READINESS_SCHEMA = "local-company.readiness.v1"
DEFAULT_MODEL = os.getenv("LOCAL_COMPANY_MODEL", DEFAULT_LOCAL_MODEL)
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
MAX_OLLAMA_BYTES = 256 * 1024
MAX_MODELS = 4096
MODEL_NAME_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/+:-]{0,127}")


class OllamaProbeError(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class ReadinessUsageError(ValueError):
    pass


class CompanyIdentityError(RuntimeError):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _: str) -> None:
        raise ReadinessUsageError("invalid arguments")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _valid_model_name(model: object) -> bool:
    if type(model) is not str or MODEL_NAME_PATTERN.fullmatch(model) is None:
        return False
    if model.startswith("/") or model.endswith("/") or "//" in model:
        return False
    if re.match(r"^[A-Za-z]:/", model):
        return False
    final_segment = model.rsplit("/", 1)[-1]
    return (
        final_segment.count(":") <= 1
        and not final_segment.startswith(":")
        and not final_segment.endswith(":")
    )


def _is_link_or_reparse_metadata(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
        or getattr(metadata, "st_reparse_tag", 0)
    )


def _windows_drive_type(root: str) -> int:
    try:
        import ctypes

        function = ctypes.windll.kernel32.GetDriveTypeW
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_uint
        return int(function(root))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise CompanyIdentityError("drive_check_unavailable") from exc


def _validated_company_database(home: Path) -> Path:
    candidate = Path(home).expanduser()
    raw = str(candidate)
    if os.name == "nt":
        windows_raw = raw.replace("/", "\\")
        if windows_raw.startswith(("\\\\", "\\?\\", "\\.\\", "\\??\\")):
            raise CompanyIdentityError("nonlocal_home")
        if candidate.drive and not candidate.root:
            raise CompanyIdentityError("unsafe_home")
        if candidate.root and not candidate.drive:
            raise CompanyIdentityError("unsafe_home")
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(os.fspath(candidate)))
    if os.name == "nt":
        normalized = str(candidate).replace("/", "\\")
        if re.fullmatch(r"[A-Za-z]:\\.*", normalized) is None:
            raise CompanyIdentityError("nonlocal_home")
        if _windows_drive_type(candidate.anchor) not in {2, 3, 6}:
            raise CompanyIdentityError("nonlocal_home")

    parts = candidate.parts
    if not parts:
        raise CompanyIdentityError("invalid_home")
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        metadata = os.lstat(current)
        if _is_link_or_reparse_metadata(metadata):
            raise CompanyIdentityError("unsafe_home")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CompanyIdentityError("invalid_home")

    database = candidate / "company.db"
    database_metadata = os.lstat(database)
    if _is_link_or_reparse_metadata(database_metadata):
        raise CompanyIdentityError("unsafe_database")
    if not stat.S_ISREG(database_metadata.st_mode):
        raise CompanyIdentityError("invalid_database")
    return database


def read_company_identity(home: Path) -> dict[str, str]:
    """Read one initialized local store identity without creating or migrating it."""
    try:
        database = _validated_company_database(home)
        uri = database.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            try:
                instance_id = read_validated_company_instance_id(db)
            except ValueError as exc:
                raise CompanyIdentityError("invalid_identity") from exc
        return {"schema": COMPANY_STORE_SCHEMA, "instance_id": instance_id}
    except CompanyIdentityError:
        raise
    except (
        AttributeError, OSError, OverflowError, RuntimeError, TypeError, UnicodeError,
        ValueError, sqlite3.Error,
    ) as exc:
        raise CompanyIdentityError("identity_unavailable") from exc


def ollama_model_installed(required_model: str) -> bool:
    """Check one exact model name through a bounded fixed loopback request."""
    if not _valid_model_name(required_model):
        raise OllamaProbeError("invalid_model_name")
    if not is_supported_local_model(required_model):
        raise OllamaProbeError("unsupported_model")
    required_model = require_local_llama_model(required_model)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        OLLAMA_TAGS_URL,
        headers={"Accept": "application/json", "User-Agent": "local-company-readiness/1"},
        method="GET",
    )
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OllamaProbeError("invalid_response") from exc
    except http.client.HTTPException as exc:
        raise OllamaProbeError("invalid_response") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise OllamaProbeError("unavailable") from exc
    try:
        with response:
            if response.status != 200:
                raise OllamaProbeError("invalid_response")
            content_type = response.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise OllamaProbeError("invalid_response")
            body = response.read(MAX_OLLAMA_BYTES + 1)
        if len(body) > MAX_OLLAMA_BYTES:
            raise OllamaProbeError("invalid_response")
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_json_object,
            parse_constant=reject_json_constant,
        )
    except OllamaProbeError:
        raise
    except (
        AttributeError, OSError, TypeError, UnicodeError, ValueError, RecursionError,
        http.client.HTTPException,
    ) as exc:
        raise OllamaProbeError("invalid_response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise OllamaProbeError("invalid_response")
    models = payload["models"]
    if len(models) > MAX_MODELS:
        raise OllamaProbeError("invalid_response")
    names: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            raise OllamaProbeError("invalid_response")
        name = model.get("name")
        digest = model.get("digest")
        if (
            not _valid_model_name(name)
            or type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or name in names
        ):
            raise OllamaProbeError("invalid_response")
        names.add(name)
    accepted_names = {required_model}
    if ":" not in required_model.rsplit("/", 1)[-1]:
        accepted_names.add(f"{required_model}:latest")
    return bool(names & accepted_names)


def _empty_components() -> dict[str, str]:
    return {
        "disk_manifest": "unknown",
        "live_build": "unknown",
        "work_state": "unknown",
        "worker": "unknown",
        "company_store": "unknown",
        "service_runtime": "unknown",
        "ollama_service": "unknown",
        "model_installed": "unknown",
    }


def _payload(
    status: str,
    components: dict[str, str],
    blockers: list[str],
    action: str,
    required_model: str | None = None,
) -> dict[str, object]:
    return {
        "schema": READINESS_SCHEMA,
        "status": status,
        "ready": status == "ready",
        "required_model": required_model,
        "components": components,
        "generation_tested": False,
        "blockers": blockers,
        "action": action,
    }


def _runtime_status(health: dict[str, object], required_model: str) -> str:
    runtime = health.get("runtime")
    if runtime is None:
        return "unverified"
    if not isinstance(runtime, dict):
        return "invalid"
    provider = runtime.get("provider")
    model = runtime.get("model")
    endpoint = runtime.get("endpoint")
    if type(provider) is not str or provider not in {"ollama", "mock", "unknown"}:
        return "invalid"
    if provider == "ollama":
        if not _valid_model_name(model) or not is_supported_local_model(model):
            return "invalid"
        if type(endpoint) is not str or endpoint not in {"loopback_default", "nonlocal"}:
            return "invalid"
        if endpoint != "loopback_default":
            return "endpoint_mismatch"
    elif model is not None or endpoint is not None:
        return "invalid"
    if provider != "ollama":
        return "provider_mismatch"
    if model != required_model:
        return "model_mismatch"
    return "match"


def _company_store_status(
    health: dict[str, object], local_identity: dict[str, str],
) -> str:
    live_identity = health.get("company")
    if live_identity is None:
        return "unverified"
    if type(live_identity) is not dict or set(live_identity) != {
        "schema", "instance_id",
    }:
        return "invalid"
    schema = live_identity.get("schema")
    instance_id = live_identity.get("instance_id")
    if schema != COMPANY_STORE_SCHEMA or not valid_company_instance_id(instance_id):
        return "invalid"
    return "match" if live_identity == local_identity else "mismatch"


def run_readiness(
    required_model: str = DEFAULT_MODEL, company_home: Path | None = None,
) -> tuple[dict[str, object], int]:
    components = _empty_components()
    if not _valid_model_name(required_model):
        components["model_installed"] = "invalid"
        return _payload(
            "indeterminate", components, ["invalid_model_name"], "fix_model_name",
        ), 2
    if not is_supported_local_model(required_model):
        components["model_installed"] = "invalid"
        return _payload(
            "indeterminate", components, ["unsupported_model"],
            "use_supported_llama_model",
        ), 2
    required_model = require_local_llama_model(required_model)

    try:
        disk = check_project(PROJECT_ROOT)
    except (ManifestError, OSError):
        components["disk_manifest"] = "invalid"
        return _payload(
            "indeterminate", components, ["disk_manifest_invalid"],
            "inspect_disk_manifest", required_model,
        ), 3
    components["disk_manifest"] = "valid"

    selected_home = default_company_home() if company_home is None else company_home
    try:
        local_identity = read_company_identity(selected_home)
    except CompanyIdentityError:
        components["company_store"] = "invalid_or_unavailable"
        return _payload(
            "indeterminate", components, ["company_store_indeterminate"],
            "inspect_company_home_selection", required_model,
        ), 2
    components["company_store"] = "local_valid"

    try:
        health = fetch_health()
        live = compare_live_build(disk, health)
    except LiveBuildError:
        components["live_build"] = "invalid_or_unavailable"
        return _payload(
            "indeterminate", components, ["live_status_indeterminate"],
            "inspect_local_service", required_model,
        ), 2

    live_status = live.get("status")
    if type(live_status) is not str or live_status not in {
        "match", "restart_required", "disk_older", "identity_conflict",
    }:
        components["live_build"] = "invalid"
        return _payload(
            "indeterminate", components, ["live_status_indeterminate"],
            "inspect_local_service", required_model,
        ), 2
    components["live_build"] = (
        live_status if type(live_status) is str else "invalid"
    )
    restart_safe = live.get("restart_safe_now")
    if type(restart_safe) is not bool:
        components["work_state"] = "unknown"
        return _payload(
            "indeterminate", components, ["live_status_indeterminate"],
            "inspect_local_service", required_model,
        ), 2
    components["work_state"] = "idle" if restart_safe else "busy"
    work_state = live.get("work_state")
    worker_status = (
        work_state.get("worker_status") if isinstance(work_state, dict) else None
    )
    if type(worker_status) is not str or worker_status not in {
        "idle", "running", "disabled", "complete", "quality_failed",
        "needs_approval", "failed", "completion_pending",
    }:
        return _payload(
            "indeterminate", components, ["live_status_indeterminate"],
            "inspect_local_service", required_model,
        ), 2
    if restart_safe and worker_status in {
        "running", "needs_approval", "completion_pending",
    }:
        return _payload(
            "indeterminate", components, ["live_status_indeterminate"],
            "inspect_local_service", required_model,
        ), 2
    components["worker"] = "disabled" if worker_status == "disabled" else "enabled"
    company_status = _company_store_status(health, local_identity)
    components["company_store"] = company_status
    if company_status == "invalid":
        return _payload(
            "indeterminate", components, ["company_store_indeterminate"],
            "inspect_local_service", required_model,
        ), 2
    if company_status == "mismatch":
        return _payload(
            "action_required", components, ["company_store_mismatch"],
            "align_company_home", required_model,
        ), 1
    if company_status == "unverified" and not (
        live_status == "restart_required" and restart_safe
    ):
        return _payload(
            "indeterminate", components, ["company_store_unverified"],
            "inspect_local_service", required_model,
        ), 2
    components["service_runtime"] = _runtime_status(health, required_model)
    if components["service_runtime"] == "invalid":
        return _payload(
            "indeterminate", components, ["service_runtime_invalid"],
            "inspect_local_service", required_model,
        ), 2

    try:
        installed = ollama_model_installed(required_model)
    except OllamaProbeError as exc:
        if exc.kind == "unavailable":
            components["ollama_service"] = "unavailable"
            components["model_installed"] = "unknown"
        else:
            components["ollama_service"] = "invalid"
            components["model_installed"] = "unknown"
            return _payload(
                "indeterminate", components, ["ollama_status_indeterminate"],
                "inspect_ollama_service", required_model,
            ), 2
    else:
        if type(installed) is not bool:
            components["ollama_service"] = "invalid"
            components["model_installed"] = "unknown"
            return _payload(
                "indeterminate", components, ["ollama_status_indeterminate"],
                "inspect_ollama_service", required_model,
            ), 2
        components["ollama_service"] = "reachable"
        components["model_installed"] = "yes" if installed else "no"

    blockers: list[str] = []
    action = "none"
    if live_status != "match":
        if live_status == "restart_required":
            blocker = (
                "service_restart_required"
                if restart_safe else "service_busy_before_restart"
            )
            action = "restart_local_service" if restart_safe else "inspect_local_work"
        elif live_status == "disk_older":
            blocker, action = "disk_older_than_service", "inspect_build_provenance"
        else:
            blocker, action = "build_identity_conflict", "inspect_build_provenance"
        blockers.append(blocker)
    elif not restart_safe:
        blockers.append("local_work_active")
        action = "inspect_local_work"

    if components["worker"] == "disabled":
        blockers.append("worker_disabled")
        if action == "none":
            action = "start_worker_enabled_service"

    if components["company_store"] == "unverified":
        blockers.append("company_store_unverified")

    runtime_status = components["service_runtime"]
    if runtime_status != "match":
        blockers.append("service_runtime_" + runtime_status)
        if action == "none":
            action = "relaunch_service_with_ollama_model"

    if components["ollama_service"] == "unavailable":
        blockers.append("ollama_unavailable")
        action = "start_ollama_locally"
    elif components["model_installed"] == "no":
        blockers.append("model_not_installed")
        action = "install_configured_model"

    if blockers:
        return _payload(
            "action_required", components, blockers, action, required_model,
        ), 1
    try:
        final_identity = read_company_identity(selected_home)
    except CompanyIdentityError:
        components["company_store"] = "invalid_or_unavailable"
        return _payload(
            "indeterminate", components, ["company_store_indeterminate"],
            "inspect_company_home_selection", required_model,
        ), 2
    if final_identity != local_identity:
        components["company_store"] = "changed_during_check"
        return _payload(
            "indeterminate", components, ["company_store_changed_during_check"],
            "retry_readiness", required_model,
        ), 2
    return _payload("ready", components, [], "none", required_model), 0


def main(argv: list[str] | None = None) -> int:
    parser = _SanitizedArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--home", type=Path)
    try:
        args = parser.parse_args(argv)
        payload, exit_code = run_readiness(args.model, args.home)
    except ReadinessUsageError:
        payload, exit_code = _payload(
            "indeterminate", _empty_components(), ["invalid_arguments"],
            "fix_command_arguments",
        ), 2
    except Exception:
        payload, exit_code = _payload(
            "indeterminate", _empty_components(), ["internal_error"],
            "inspect_readiness_tool",
        ), 3
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
