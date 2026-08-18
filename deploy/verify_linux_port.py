"""Prove one Linux container port of the local company runtime before trusting it.

Run this INSIDE the coordinator container, before the first mission:

    docker compose exec coordinator python /app/deploy/verify_linux_port.py

Emits one JSON object and exits 0 on pass, 1 otherwise. Every check fails
closed: an observation that cannot be made is a blocker, never a pass.

The check that earns this script's existence is `memory`.
capacity.observe_memory() had no POSIX branch and returned status
"unavailable" on Linux. Nothing crashed. The MCP execution path simply
answered {"status": "blocked", "reason": "available_memory_unavailable"} for
every request forever, and the machine-capacity snapshot reported
"indeterminate" rather than a failure. A port can look completely healthy while
being incapable of running a single mission, so the byte count is asserted to
be present AND plausible here, not merely non-crashing.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


PORT_SCHEMA = "local-company.linux-port.v1"
MINIMUM_PYTHON = (3, 11)
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_MINIMUM_AVAILABLE_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_OLLAMA_BYTES = 256 * 1024
MAX_MODELS = 4096
MAX_HOST_LENGTH = 255
MAX_PATH_PARTS = 64
MAX_DETAIL_CHARS = 200
MAX_TRACEBACK_FRAMES = 64
# A total below this is not a machine anyone runs a model on; a total above it
# is a misread rather than a reading. Both mean "do not trust the gate".
MIN_PLAUSIBLE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PLAUSIBLE_TOTAL_BYTES = 2 * 1024 ** 4

# Lowercased substrings that mark a path segment as cloud-synchronised storage.
# SQLite over a sync client corrupts under concurrent write; the company store
# must never live inside one. Deliberately excludes bare "box", which collides
# with ordinary directory names.
CLOUD_SYNC_MARKERS = (
    "onedrive", "dropbox", "google drive", "googledrive", "gdrive",
    "icloud", "nextcloud", "owncloud", "pcloud", "box sync", "syncthing",
    "mega sync", "megasync", "yandex.disk", "yandexdisk",
    "creative cloud files", "sharepoint",
)


class PortUsageError(ValueError):
    pass


class OllamaProbeError(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _: str) -> None:
        raise PortUsageError("invalid arguments")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def _bounded_detail(value: object) -> str:
    text = str(value)
    if len(text) > MAX_DETAIL_CHARS:
        return text[:MAX_DETAIL_CHARS] + "..."
    return text


def _is_link_or_reparse_metadata(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
        or getattr(metadata, "st_reparse_tag", 0)
    )


def _failing_modules(exception: BaseException) -> list[str]:
    """Name the files that raised, so the operator does not have to guess."""
    names: list[str] = []
    traceback = exception.__traceback__
    frames = 0
    while traceback is not None and frames < MAX_TRACEBACK_FRAMES:
        filename = getattr(traceback.tb_frame.f_code, "co_filename", "")
        if type(filename) is str and filename:
            leaf = Path(filename).name
            if leaf not in names:
                names.append(leaf)
        traceback = traceback.tb_next
        frames += 1
    return names[-6:]


def check_python() -> dict[str, object]:
    info = sys.version_info
    supported = (info.major, info.minor) >= MINIMUM_PYTHON
    return {
        "status": "ready" if supported else "unsupported",
        "version": f"{info.major}.{info.minor}.{info.micro}",
        "minimum": f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}",
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
    }


def check_platform() -> dict[str, object]:
    linux = os.name == "posix" and sys.platform.startswith("linux")
    return {
        "status": "linux" if linux else "not_linux",
        "os_name": os.name,
        "sys_platform": sys.platform,
        "machine": platform.machine(),
        "containerized": _containerized(),
    }


def _containerized() -> bool | None:
    """Best-effort container detection; None when it cannot be determined."""
    if os.name != "posix":
        return None
    try:
        if Path("/.dockerenv").exists():
            return True
    except OSError:
        return None
    try:
        raw = Path("/proc/1/cgroup").read_bytes()[:65536]
    except OSError:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except UnicodeError:
        return None
    return any(marker in text for marker in ("docker", "containerd", "kubepods", "lxc"))


def check_memory() -> dict[str, object]:
    """Assert the admission gate reads a real, plausible byte count.

    This is the trap. A "unavailable" here does not raise, it just blocks every
    MCP-driven execution silently, so it is asserted rather than reported.
    """
    try:
        from local_company import capacity
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reason": "capacity_import_failed",
            "error": type(exc).__name__,
            "detail": _bounded_detail(exc),
            "total_bytes": None,
            "available_bytes": None,
        }

    minimum = getattr(
        capacity, "MIN_AVAILABLE_MEMORY_BYTES", DEFAULT_MINIMUM_AVAILABLE_MEMORY_BYTES,
    )
    if type(minimum) is not int or minimum <= 0:
        minimum = DEFAULT_MINIMUM_AVAILABLE_MEMORY_BYTES

    try:
        observation = capacity.observe_memory()
    except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
        return {
            "status": "unavailable",
            "reason": "observe_memory_raised",
            "error": type(exc).__name__,
            "detail": _bounded_detail(exc),
            "total_bytes": None,
            "available_bytes": None,
            "minimum_available_bytes": minimum,
        }

    if type(observation) is not dict:
        return {
            "status": "unavailable",
            "reason": "observe_memory_shape_invalid",
            "total_bytes": None,
            "available_bytes": None,
            "minimum_available_bytes": minimum,
        }

    total = observation.get("total_bytes")
    available = observation.get("available_bytes")
    result: dict[str, object] = {
        "status": "unavailable",
        "reason": "none",
        "observed_status": observation.get("status"),
        "total_bytes": total if type(total) is int else None,
        "available_bytes": available if type(available) is int else None,
        "minimum_available_bytes": minimum,
        "cgroup": _cgroup_reading(capacity),
    }

    if observation.get("status") != "ready":
        # The exact historical failure: no POSIX branch, so status stayed
        # "unavailable" and every execution request was refused.
        result["reason"] = "memory_status_not_ready"
        return result
    if type(total) is not int or type(available) is not int:
        result["reason"] = "memory_bytes_not_integers"
        return result
    if total < MIN_PLAUSIBLE_TOTAL_BYTES or total > MAX_PLAUSIBLE_TOTAL_BYTES:
        result["reason"] = "memory_total_implausible"
        return result
    if available < 0 or available > total:
        result["reason"] = "memory_available_out_of_range"
        return result

    result["status"] = "ready"
    result["headroom_ok"] = available >= minimum
    if not result["headroom_ok"]:
        # Real reading, not enough room. Reported separately from a broken
        # reading because the remedy is different (stop something vs fix code).
        result["reason"] = "memory_headroom_below_minimum"
    return result


def _cgroup_reading(capacity_module: object) -> dict[str, object]:
    observer = getattr(capacity_module, "observe_cgroup_memory", None)
    if observer is None:
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}
    try:
        observation = observer()
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}
    if type(observation) is not dict:
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}
    total = observation.get("total_bytes")
    available = observation.get("available_bytes")
    return {
        "status": observation.get("status"),
        "total_bytes": total if type(total) is int else None,
        "available_bytes": available if type(available) is int else None,
    }


def _cloud_sync_segments(candidate: Path) -> list[str]:
    hits: list[str] = []
    for part in candidate.parts[:MAX_PATH_PARTS]:
        lowered = part.lower()
        for marker in CLOUD_SYNC_MARKERS:
            if marker in lowered and part not in hits:
                hits.append(part)
                break
    return hits


def check_company_home() -> dict[str, object]:
    configured = os.getenv("LOCAL_COMPANY_HOME")
    result: dict[str, object] = {
        "status": "unavailable",
        "reason": "none",
        "configured": configured is not None,
        "path": None,
        "writable": False,
        "cloud_sync_segments": [],
        "persistent_mount": None,
        "company_db_present": None,
    }
    if configured is None or configured == "":
        result["reason"] = "local_company_home_unset"
        return result
    if len(configured) > 4096:
        result["reason"] = "local_company_home_too_long"
        return result

    try:
        from local_company.config import default_company_home
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        default_company_home = None  # type: ignore[assignment]

    try:
        if default_company_home is None:
            home = Path(configured).expanduser()
        else:
            home = default_company_home()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        result["reason"] = "local_company_home_rejected"
        result["detail"] = _bounded_detail(exc)
        return result

    result["path"] = str(home)
    if not home.is_absolute():
        result["reason"] = "local_company_home_not_absolute"
        return result

    hits = _cloud_sync_segments(home)
    result["cloud_sync_segments"] = hits
    if hits:
        # SQLite plus a sync daemon is silent corruption, not a slow disk.
        result["reason"] = "company_home_inside_cloud_sync"
        return result

    parts = home.parts
    if not parts or len(parts) > MAX_PATH_PARTS:
        result["reason"] = "company_home_path_invalid"
        return result
    current = Path(parts[0])
    try:
        for part in parts[1:]:
            current /= part
            metadata = os.lstat(current)
            if _is_link_or_reparse_metadata(metadata):
                result["reason"] = "company_home_path_contains_link"
                return result
            if not stat.S_ISDIR(metadata.st_mode):
                result["reason"] = "company_home_not_a_directory"
                return result
    except OSError as exc:
        result["reason"] = "company_home_missing"
        result["detail"] = _bounded_detail(exc)
        return result

    probe = home / f".local-company-port-probe-{os.getpid()}"
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        result["reason"] = "company_home_not_writable"
        result["detail"] = _bounded_detail(exc)
        return result
    try:
        os.write(descriptor, b"1")
    except OSError as exc:
        result["reason"] = "company_home_write_failed"
        result["detail"] = _bounded_detail(exc)
        return result
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(probe)
        except OSError:
            pass
    result["writable"] = True

    try:
        result["persistent_mount"] = os.path.ismount(home)
    except OSError:
        result["persistent_mount"] = None
    try:
        database_metadata = os.lstat(home / "company.db")
    except OSError:
        result["company_db_present"] = False
    else:
        result["company_db_present"] = stat.S_ISREG(database_metadata.st_mode)

    result["status"] = "ready"
    return result


def _validated_ollama_host(host: object) -> str:
    if type(host) is not str or not 1 <= len(host) <= MAX_HOST_LENGTH:
        raise OllamaProbeError("invalid_host")
    parsed = urllib.parse.urlsplit(host.rstrip("/"))
    if parsed.scheme not in {"http", "https"}:
        raise OllamaProbeError("invalid_host")
    if not parsed.netloc or "@" in parsed.netloc:
        raise OllamaProbeError("invalid_host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise OllamaProbeError("invalid_host")
    return f"{parsed.scheme}://{parsed.netloc}"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json key")
        result[key] = value
    return result


def _reject_json_constant(name: str) -> object:
    raise ValueError(f"unexpected json constant: {name}")


def _fetch_ollama_models(host: str) -> list[str]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        f"{host}/api/tags",
        headers={"Accept": "application/json", "User-Agent": "local-company-port/1"},
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
        raise OllamaProbeError("unreachable") from exc
    try:
        with response:
            if response.status != 200:
                raise OllamaProbeError("invalid_response")
            media_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise OllamaProbeError("invalid_response")
            body = response.read(MAX_OLLAMA_BYTES + 1)
        if len(body) > MAX_OLLAMA_BYTES:
            raise OllamaProbeError("invalid_response")
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except OllamaProbeError:
        raise
    except (
        AttributeError, OSError, RecursionError, TypeError, UnicodeError, ValueError,
        http.client.HTTPException,
    ) as exc:
        raise OllamaProbeError("invalid_response") from exc

    if type(payload) is not dict or type(payload.get("models")) is not list:
        raise OllamaProbeError("invalid_response")
    models = payload["models"]
    if len(models) > MAX_MODELS:
        raise OllamaProbeError("invalid_response")
    names: list[str] = []
    for model in models:
        if type(model) is not dict:
            raise OllamaProbeError("invalid_response")
        name = model.get("name")
        if type(name) is not str or not 1 <= len(name) <= 256 or name in names:
            raise OllamaProbeError("invalid_response")
        names.append(name)
    return sorted(names)


def _loopback_ollama_host() -> str:
    """The exact host string the readiness gate treats as "loopback_default"."""
    try:
        from local_company.core import LOOPBACK_OLLAMA_HOST
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return DEFAULT_OLLAMA_HOST
    if type(LOOPBACK_OLLAMA_HOST) is not str or not LOOPBACK_OLLAMA_HOST:
        return DEFAULT_OLLAMA_HOST
    return LOOPBACK_OLLAMA_HOST


def check_ollama(host: str, *, offline: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "skipped" if offline else "unavailable",
        "reason": "offline_requested" if offline else "none",
        "host": host,
        "loopback_host": None,
        "model_count": None,
        "supported_model_installed": None,
    }
    try:
        validated = _validated_ollama_host(host)
    except OllamaProbeError as exc:
        result["status"] = "unavailable"
        result["reason"] = exc.kind
        return result
    result["host"] = validated
    # dashboard.runtime_model_identity() labels any host other than
    # core.LOOPBACK_OLLAMA_HOST "nonlocal", and check_readiness turns that into
    # an endpoint_mismatch blocker. Compared against the live constant rather
    # than a copy so this stays true if the constant moves. Surfaced so a
    # container deployment is not surprised by a readiness failure it cannot
    # explain: reaching Ollama by service name is exactly what trips it.
    result["loopback_host"] = validated == _loopback_ollama_host()
    if offline:
        return result

    try:
        names = _fetch_ollama_models(validated)
    except OllamaProbeError as exc:
        result["reason"] = exc.kind
        return result

    result["model_count"] = len(names)
    try:
        from local_company.model_policy import is_supported_local_model
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        result["status"] = "reachable"
        result["reason"] = "model_policy_unavailable"
        return result

    def supported(name: str) -> bool:
        # `ollama pull llama3.2:1b` can register the tag as "llama3.2:1b" or,
        # for an untagged pull, as "<model>:latest". Accept both spellings.
        if is_supported_local_model(name):
            return True
        base, separator, suffix = name.rpartition(":")
        return bool(separator) and suffix == "latest" and is_supported_local_model(base)

    installed = [name for name in names if supported(name)]
    result["supported_model_installed"] = bool(installed)
    result["supported_models"] = sorted(installed)
    result["status"] = "reachable"
    if not installed:
        result["reason"] = "supported_model_not_installed"
    return result


def _import_probe(module_name: str) -> tuple[bool, str, str, list[str]]:
    try:
        __import__(module_name)
    except (
        AttributeError, ImportError, OSError, RuntimeError, SystemError, TypeError,
        ValueError,
    ) as exc:
        return False, type(exc).__name__, _bounded_detail(exc), _failing_modules(exc)
    return True, "none", "", []


def check_windows_module() -> dict[str, object]:
    """The WinAPI workcell must be inert on Linux - and must not drag the CLI down.

    computer_use.py does `from ctypes import wintypes` at module scope.
    ctypes.wintypes defines VARIANT_BOOL with _type_ = "v", a format code the
    CPython _ctypes build only registers on Windows, so that import raises
    ValueError on Linux. That is the CORRECT outcome for the module itself. It
    is NOT correct for anything that imports it eagerly, which is why
    cli_entrypoint is checked separately.
    """
    posix = os.name != "nt"
    imported, error, detail, frames = _import_probe("local_company.computer_use")
    result: dict[str, object] = {
        "status": "unavailable",
        "reason": "none",
        "importable": imported,
        "error": error,
        "detail": detail,
        "failing_modules": frames,
    }
    if posix and not imported:
        result["status"] = "excluded"
        return result
    if posix and imported:
        # WinAPI call paths reachable on Linux means the platform guard is gone.
        result["status"] = "unexpectedly_importable"
        result["reason"] = "windows_module_importable_on_posix"
        return result
    if not posix and imported:
        result["status"] = "present_on_windows"
        return result
    result["status"] = "missing_on_windows"
    result["reason"] = "windows_module_unavailable_on_windows"
    return result


def check_cli_entrypoint() -> dict[str, object]:
    """The console entry point must import on the target platform.

    cli.py imports .computer_use at module scope, so on Linux this is where the
    ctypes.wintypes ValueError surfaces and takes down `local-company` in its
    entirety - dashboard, queue, health, everything, including the subprocess
    the MCP server spawns to execute a mission.
    """
    imported, error, detail, frames = _import_probe("local_company.cli")
    result: dict[str, object] = {
        "status": "importable" if imported else "import_failed",
        "reason": "none",
        "error": error,
        "detail": detail,
        "failing_modules": frames,
    }
    if imported:
        return result
    if any(leaf in {"wintypes.py", "computer_use.py"} for leaf in frames):
        result["reason"] = "cli_imports_windows_only_module"
    else:
        result["reason"] = "cli_import_failed"
    return result


def run_verification(*, ollama_host: str, offline: bool) -> tuple[dict[str, object], int]:
    checks = {
        "python": check_python(),
        "platform": check_platform(),
        "memory": check_memory(),
        "company_home": check_company_home(),
        "ollama": check_ollama(ollama_host, offline=offline),
        "windows_module": check_windows_module(),
        "cli_entrypoint": check_cli_entrypoint(),
    }

    blockers: list[str] = []
    advisories: list[str] = []
    # Advisories about cgroups and mount points only mean something on the
    # target platform; on a Windows dev box they are pure noise.
    on_linux = checks["platform"]["status"] == "linux"

    if checks["python"]["status"] != "ready":
        blockers.append("python_version_unsupported")
    if checks["platform"]["status"] != "linux":
        blockers.append("platform_not_linux")
    if checks["platform"].get("containerized") is False:
        advisories.append("not_running_in_a_container")

    memory = checks["memory"]
    if memory["status"] != "ready":
        blockers.append("memory_observation_" + str(memory.get("reason", "unavailable")))
    elif memory.get("headroom_ok") is not True:
        blockers.append("memory_headroom_below_minimum")
    if on_linux and memory.get("cgroup", {}).get("status") != "ready":
        # Without a cgroup ceiling the gate sees the whole host and admits work
        # the container cannot actually run.
        advisories.append("no_cgroup_memory_limit_visible")

    home = checks["company_home"]
    if home["status"] != "ready":
        blockers.append("company_home_" + str(home.get("reason", "unavailable")))
    elif on_linux and home.get("persistent_mount") is not True:
        # State that is not on a mount lives in the container's writable layer
        # and dies with the container.
        advisories.append("company_home_not_on_a_mount")

    ollama = checks["ollama"]
    if ollama["status"] == "skipped":
        advisories.append("ollama_probe_skipped")
    elif ollama["status"] != "reachable":
        blockers.append("ollama_" + str(ollama.get("reason", "unavailable")))
    elif ollama.get("supported_model_installed") is not True:
        blockers.append("ollama_supported_model_not_installed")
    if ollama.get("loopback_host") is False:
        advisories.append("ollama_host_reports_nonlocal_to_readiness")

    if checks["windows_module"]["status"] not in {"excluded", "present_on_windows"}:
        blockers.append(str(checks["windows_module"].get("reason", "windows_module_invalid")))
    if checks["cli_entrypoint"]["status"] != "importable":
        blockers.append(str(checks["cli_entrypoint"].get("reason", "cli_import_failed")))

    blockers = list(dict.fromkeys(blockers))
    advisories = list(dict.fromkeys(advisories))

    if "cli_imports_windows_only_module" in blockers:
        action = "make_the_computer_use_import_lazy_in_cli"
    elif any(item.startswith("memory_observation_") for item in blockers):
        action = "fix_posix_memory_observation_before_running_any_mission"
    elif any(item.startswith("company_home_") for item in blockers):
        action = "mount_a_writable_non_synced_state_volume"
    elif any(item.startswith("ollama_") for item in blockers):
        action = "start_ollama_and_pull_a_supported_model"
    elif blockers:
        action = "review_port_blockers"
    else:
        action = "none"

    payload = {
        "schema": PORT_SCHEMA,
        "status": "pass" if not blockers else "fail",
        "pass": not blockers,
        "checks": checks,
        "blockers": blockers,
        "advisories": advisories,
        "action": action,
        "effects": {
            "database_mutated": False,
            "model_called": False,
            "process_started": False,
        },
    }
    return payload, 0 if not blockers else 1


def _failure_payload(blocker: str, action: str) -> dict[str, object]:
    return {
        "schema": PORT_SCHEMA,
        "status": "fail",
        "pass": False,
        "checks": {},
        "blockers": [blocker],
        "advisories": [],
        "action": action,
        "effects": {
            "database_mutated": False,
            "model_called": False,
            "process_started": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = _SanitizedArgumentParser(description="Verify the Linux container port.")
    parser.add_argument(
        "--ollama-host",
        default=os.getenv("LOCAL_COMPANY_OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        help="Base URL of the Ollama endpoint to probe.",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip the Ollama probe (still reports the configured host).",
    )
    try:
        args = parser.parse_args(argv)
        payload, exit_code = run_verification(
            ollama_host=args.ollama_host, offline=bool(args.offline),
        )
    except PortUsageError:
        payload, exit_code = _failure_payload(
            "invalid_arguments", "fix_command_arguments",
        ), 1
    except Exception:  # fail closed: an unverified port is a failed port
        payload, exit_code = _failure_payload(
            "internal_error", "inspect_port_verification_tool",
        ), 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
