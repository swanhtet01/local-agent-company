"""Recover confirmed-missing local runtime components without running company work."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_company.config import default_company_home  # noqa: E402
from local_company.service import (  # noqa: E402
    _open_regular_lock_file, _terminate_owned_child, _windows_breakaway_denied,
    _windows_detached_creation_flags, _windows_inherited_creation_flags,
    service_status, start_service,
)

if __package__:
    from .check_readiness import (  # noqa: E402
        READINESS_SCHEMA, CompanyIdentityError, OllamaProbeError,
        _is_link_or_reparse_metadata, _valid_model_name, _windows_drive_type,
        ollama_model_installed, read_company_identity, run_readiness,
    )
    from .stamp_build_manifest import ManifestError, check_project  # noqa: E402
else:
    from check_readiness import (  # noqa: E402
        READINESS_SCHEMA, CompanyIdentityError, OllamaProbeError,
        _is_link_or_reparse_metadata, _valid_model_name, _windows_drive_type,
        ollama_model_installed, read_company_identity, run_readiness,
    )
    from stamp_build_manifest import ManifestError, check_project  # noqa: E402


GUARD_SCHEMA = "local-company.runtime-guard.v1"
RESULT_JOURNAL_NAME = "runtime-guard-last.json"
MAX_RENDERED_RESULT_BYTES = 2048
KNOWN_PROCESS_RELATIONS = {
    "match", "mismatch", "absent", "unavailable", "legacy",
}
OLLAMA_PORT = 11434
OLLAMA_HOST = "127.0.0.1:11434"
READINESS_ACTION_MAP = {
    "align_company_home": "inspect_company_store",
    "inspect_build_provenance": "inspect_build_manifest",
    "inspect_company_home_selection": "inspect_company_store",
    "inspect_disk_manifest": "inspect_build_manifest",
    "inspect_local_service": "inspect_local_service",
    "inspect_local_work": "inspect_local_work",
    "inspect_ollama_service": "inspect_ollama_service",
    "install_configured_model": "install_configured_model",
    "relaunch_service_with_ollama_model": "relaunch_service_manually",
    "restart_local_service": "restart_local_service_manually",
    "retry_readiness": "retry_runtime_guard",
    "start_ollama_locally": "start_ollama_manually",
    "start_worker_enabled_service": "relaunch_service_manually",
}


class GuardUsageError(ValueError):
    pass


class GuardBusyError(RuntimeError):
    pass


class GuardLockError(RuntimeError):
    pass


class GuardExecutableError(RuntimeError):
    pass


class GuardResultJournalError(RuntimeError):
    pass


class GuardStoreChangedError(RuntimeError):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _: str) -> None:
        raise GuardUsageError("invalid arguments")


def _empty_components() -> dict[str, str]:
    return {
        "company_store": "unknown",
        "disk_manifest": "unknown",
        "service": "unknown",
        "process_identity": "unknown",
        "ollama": "unknown",
        "model": "unknown",
        "readiness": "unknown",
    }


def _payload(
    *, status: str, ready: bool, components: dict[str, str],
    blockers: list[str], action: str, changes: list[str], model: str | None,
) -> dict[str, object]:
    return {
        "schema": GUARD_SCHEMA,
        "status": status,
        "ready": ready,
        "components": dict(components),
        "blockers": list(dict.fromkeys(blockers)),
        "action": action,
        "changes": list(dict.fromkeys(changes)),
        "required_model": model,
        "missions_started": 0,
        "models_pulled": 0,
    }


def _valid_runtime_arguments(
    port: object, model: object, num_ctx: object, num_predict: object,
    keep_alive: object, wait_seconds: object,
) -> bool:
    return bool(
        type(port) is int and port == 8765
        and _valid_model_name(model)
        and type(num_ctx) is int and 1024 <= num_ctx <= 131072
        and type(num_predict) is int and 32 <= num_predict <= 4096
        and type(keep_alive) is str
        and re.fullmatch(r"[1-9][0-9]{0,4}[smh]", keep_alive)
        and type(wait_seconds) is int and 1 <= wait_seconds <= 30
    )


def _valid_store_identity(identity: object) -> bool:
    return bool(
        isinstance(identity, dict)
        and set(identity) == {"schema", "instance_id"}
        and identity.get("schema") == "local-company.store.v1"
        and type(identity.get("instance_id")) is str
        and re.fullmatch(r"[0-9a-f]{32}", identity["instance_id"])
    )


def _normalized_company_home(home: Path) -> Path:
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
    return Path(os.path.abspath(os.fspath(candidate)))


def _safe_result_parts(
    result: object,
) -> tuple[dict[str, str], list[str]]:
    components = _empty_components()
    changes: list[str] = []
    if not isinstance(result, dict):
        return components, changes
    raw_components = result.get("components")
    if (
        isinstance(raw_components, dict)
        and set(raw_components) == set(components)
        and all(
            type(value) is str
            and re.fullmatch(r"[a-z][a-z_]{0,39}", value) is not None
            for value in raw_components.values()
        )
    ):
        components = dict(raw_components)
    raw_changes = result.get("changes")
    if isinstance(raw_changes, list):
        changes = [
            value for value in raw_changes
            if value in {"ollama_started", "service_started"}
        ][:2]
    return components, list(dict.fromkeys(changes))


def _result_journal_failure(
    result: object, model: str,
) -> tuple[dict[str, object], int]:
    components, changes = _safe_result_parts(result)
    return _payload(
        status="indeterminate", ready=False, components=components,
        blockers=["result_journal_write_failed"], action="inspect_runtime_guard",
        changes=changes, model=model,
    ), 2


def _store_changed_after_result(
    result: object, model: str,
) -> tuple[dict[str, object], int]:
    components, changes = _safe_result_parts(result)
    components["company_store"] = "changed"
    return _payload(
        status="indeterminate", ready=False, components=components,
        blockers=["company_store_changed"], action="inspect_company_store",
        changes=changes, model=model,
    ), 2


def _render_result(result: object) -> bytes:
    expected_keys = {
        "schema", "status", "ready", "components", "blockers", "action",
        "changes", "required_model", "missions_started", "models_pulled",
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        raise GuardResultJournalError("invalid result payload")
    components = result.get("components")
    blockers = result.get("blockers")
    changes = result.get("changes")
    required_model = result.get("required_model")
    if (
        result.get("schema") != GUARD_SCHEMA
        or result.get("status") not in {
            "ready", "recovered", "action_required", "indeterminate",
        }
        or type(result.get("ready")) is not bool
        or not isinstance(components, dict)
        or set(components) != set(_empty_components())
        or any(
            type(value) is not str
            or re.fullmatch(r"[a-z][a-z_]{0,39}", value) is None
            for value in components.values()
        )
        or not isinstance(blockers, list) or len(blockers) > 16
        or any(
            type(value) is not str
            or re.fullmatch(r"[a-z][a-z_]{0,63}", value) is None
            for value in blockers
        )
        or type(result.get("action")) is not str
        or re.fullmatch(r"[a-z][a-z_]{0,63}", result["action"]) is None
        or not isinstance(changes, list) or len(changes) > 2
        or any(value not in {"ollama_started", "service_started"} for value in changes)
        or not (required_model is None or _valid_model_name(required_model))
        or result.get("missions_started") != 0
        or result.get("models_pulled") != 0
    ):
        raise GuardResultJournalError("invalid result payload")
    try:
        rendered = (
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise GuardResultJournalError("invalid result payload") from exc
    if len(rendered) > MAX_RENDERED_RESULT_BYTES:
        raise GuardResultJournalError("result payload is too large")
    return rendered


def _write_stdout(rendered: bytes) -> None:
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(rendered)
        binary.flush()
        return
    sys.stdout.write(rendered.decode("utf-8", errors="strict"))
    sys.stdout.flush()


def _record_metadata(path: Path) -> os.stat_result | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if (
        _is_link_or_reparse_metadata(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise GuardResultJournalError("unsafe result journal")
    return metadata


def _same_record(metadata: os.stat_result, current: os.stat_result) -> bool:
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink,
        metadata.st_size, metadata.st_mtime_ns,
    ) == (
        current.st_dev, current.st_ino, current.st_mode, current.st_nlink,
        current.st_size, current.st_mtime_ns,
    )


def _write_result_journal(
    home: Path, pinned_identity: dict[str, str], rendered: bytes,
) -> None:
    if not isinstance(rendered, bytes) or not rendered or len(rendered) > MAX_RENDERED_RESULT_BYTES:
        raise GuardResultJournalError("invalid rendered result")
    target = home / RESULT_JOURNAL_NAME
    temporary: Path | None = None
    try:
        home_metadata = os.lstat(home)
        if (
            _is_link_or_reparse_metadata(home_metadata)
            or not stat.S_ISDIR(home_metadata.st_mode)
        ):
            raise GuardResultJournalError("unsafe company home")
        previous = _record_metadata(target)
        temporary = home / f".{RESULT_JOURNAL_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_metadata = os.lstat(temporary)
        if (
            _is_link_or_reparse_metadata(temporary_metadata)
            or not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_nlink != 1
            or temporary_metadata.st_size != len(rendered)
        ):
            raise GuardResultJournalError("unsafe result journal temporary file")
        current_home = os.lstat(home)
        if (
            _is_link_or_reparse_metadata(current_home)
            or not stat.S_ISDIR(current_home.st_mode)
            or (current_home.st_dev, current_home.st_ino)
            != (home_metadata.st_dev, home_metadata.st_ino)
        ):
            raise GuardResultJournalError("company home changed before commit")
        current = _record_metadata(target)
        if (previous is None) != (current is None) or (
            previous is not None and current is not None
            and not _same_record(previous, current)
        ):
            raise GuardResultJournalError("result journal changed before commit")
        if not _store_unchanged(home, pinned_identity):
            raise GuardStoreChangedError("company store changed before journal commit")
        os.replace(temporary, target)
        temporary = None
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(home, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except (GuardResultJournalError, GuardStoreChangedError):
        raise
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise GuardResultJournalError("could not write result journal") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _runtime_guard_lock(home: Path):
    try:
        descriptor = _open_regular_lock_file(home / "runtime.guard.lock")
    except RuntimeError as exc:
        raise GuardLockError("runtime guard lock is unavailable") from exc
    locked = False
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError) as exc:
            raise GuardBusyError("runtime guard is already active") from exc
        yield
    finally:
        if locked:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _connection_was_refused(error: BaseException) -> bool:
    pending: list[object] = [error]
    seen: set[int] = set()
    for _ in range(12):
        if not pending:
            break
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError) and (
            current.errno == errno.ECONNREFUSED or getattr(current, "winerror", None) == 10061
        ):
            return True
        if isinstance(current, urllib.error.URLError):
            pending.append(current.reason)
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
    return False


def _probe_ollama(model: str) -> tuple[str, bool | None]:
    try:
        installed = ollama_model_installed(model)
        if type(installed) is not bool:
            return "invalid", None
        return "reachable", installed
    except OllamaProbeError as exc:
        if exc.kind == "unavailable":
            state = "refused" if _connection_was_refused(exc) else "unavailable"
            return state, None
        return "invalid", None
    except (AttributeError, TypeError, UnicodeError, ValueError, RecursionError):
        return "invalid", None


def _windows_listener_table_contains(port: int) -> bool | None:
    try:
        import ctypes
        from ctypes import wintypes

        class _Tcp4Row(ctypes.Structure):
            _fields_ = [
                ("state", wintypes.DWORD),
                ("local_address", wintypes.DWORD),
                ("local_port", wintypes.DWORD),
                ("remote_address", wintypes.DWORD),
                ("remote_port", wintypes.DWORD),
                ("owning_pid", wintypes.DWORD),
            ]

        class _Tcp6Row(ctypes.Structure):
            _fields_ = [
                ("local_address", ctypes.c_ubyte * 16),
                ("local_scope_id", wintypes.DWORD),
                ("local_port", wintypes.DWORD),
                ("remote_address", ctypes.c_ubyte * 16),
                ("remote_scope_id", wintypes.DWORD),
                ("remote_port", wintypes.DWORD),
                ("state", wintypes.DWORD),
                ("owning_pid", wintypes.DWORD),
            ]

        get_table = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
        get_table.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
            wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
        ]
        get_table.restype = wintypes.DWORD

        def contains(address_family: int, row_type: type[ctypes.Structure]) -> bool:
            size = wintypes.DWORD(0)
            result = get_table(None, ctypes.byref(size), False, address_family, 3, 0)
            if result not in {0, 122} or size.value > 16 * 1024 * 1024:
                raise OSError("TCP listener table size is unavailable")
            if size.value == 0:
                return False
            buffer = ctypes.create_string_buffer(size.value)
            result = get_table(buffer, ctypes.byref(size), False, address_family, 3, 0)
            if result != 0 or size.value < ctypes.sizeof(wintypes.DWORD):
                raise OSError("TCP listener table is unavailable")
            count = wintypes.DWORD.from_buffer_copy(buffer).value
            row_size = ctypes.sizeof(row_type)
            if count > 1_000_000 or 4 + count * row_size > size.value:
                raise OSError("TCP listener table is invalid")
            for index in range(count):
                row = row_type.from_buffer_copy(buffer, 4 + index * row_size)
                if socket.ntohs(int(row.local_port) & 0xFFFF) == port:
                    return True
            return False

        ipv4 = contains(socket.AF_INET, _Tcp4Row)
        ipv6 = contains(socket.AF_INET6, _Tcp6Row)
        return ipv4 or ipv6
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _ollama_port_state() -> str:
    try:
        with socket.create_connection(("127.0.0.1", OLLAMA_PORT), timeout=0.5):
            return "listening"
    except OSError as exc:
        if _connection_was_refused(exc):
            return "refused"
        if os.name != "nt":
            return "indeterminate"
        first = _windows_listener_table_contains(OLLAMA_PORT)
        time.sleep(0.1)
        second = _windows_listener_table_contains(OLLAMA_PORT)
        if first is True or second is True:
            return "listening"
        return "refused" if first is False and second is False else "indeterminate"


def _validated_ollama_executable(raw_path: str | Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise GuardExecutableError("executable path must be absolute")
    if os.name == "nt":
        normalized = str(candidate).replace("/", "\\")
        if not candidate.drive or not candidate.root or normalized.startswith(
            ("\\\\", "\\?\\", "\\.\\", "\\??\\"),
        ):
            raise GuardExecutableError("unsafe executable path")
        try:
            if _windows_drive_type(candidate.anchor) != 3:
                raise GuardExecutableError("executable is not on a fixed local drive")
            current = Path(candidate.parts[0])
            for part in candidate.parts[1:]:
                current /= part
                metadata = os.lstat(current)
                if _is_link_or_reparse_metadata(metadata):
                    raise GuardExecutableError("executable path contains a link")
        except GuardExecutableError:
            raise
        except (CompanyIdentityError, OSError, ValueError) as exc:
            raise GuardExecutableError("executable path is unavailable") from exc
    try:
        resolved = candidate.resolve(strict=True)
        metadata = os.stat(resolved)
    except OSError as exc:
        raise GuardExecutableError("executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardExecutableError("executable is not a regular file")
    return resolved


def _trusted_ollama_executable(explicit: Path | None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    elif os.name == "nt":
        for variable in ("LOCALAPPDATA", "ProgramFiles"):
            root = os.getenv(variable)
            if root:
                candidates.append(Path(root) / "Programs" / "Ollama" / "ollama.exe")
                candidates.append(Path(root) / "Ollama" / "ollama.exe")
    else:
        discovered = shutil.which("ollama")
        if discovered:
            candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            return _validated_ollama_executable(candidate)
        except GuardExecutableError:
            if explicit is not None:
                raise
    return None


def _spawn_ollama(
    executable: Path, *, allow_job_inheritance: bool = False,
) -> subprocess.Popen[bytes]:
    creationflags = 0
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        creationflags = _windows_detached_creation_flags()
    else:
        popen_kwargs["start_new_session"] = True
    environment = os.environ.copy()
    environment["OLLAMA_HOST"] = OLLAMA_HOST
    arguments: dict[str, object] = {
        "cwd": executable.parent, "env": environment,
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True,
        "creationflags": creationflags, **popen_kwargs,
    }
    try:
        return subprocess.Popen([str(executable), "serve"], **arguments)
    except OSError as exc:
        if not (
            os.name == "nt" and allow_job_inheritance
            and _windows_breakaway_denied(exc)
        ):
            raise
        arguments["creationflags"] = _windows_inherited_creation_flags()
        return subprocess.Popen([str(executable), "serve"], **arguments)


def _wait_for_ollama(
    model: str, process: subprocess.Popen[bytes], wait_seconds: int,
) -> tuple[str, bool | None]:
    deadline = time.monotonic() + wait_seconds
    while True:
        state, installed = _probe_ollama(model)
        try:
            exited = process.poll() is not None
        except OSError:
            return "invalid", None
        if state not in {"refused", "unavailable"}:
            if state != "reachable" or exited:
                return "invalid", None
            return state, installed
        if exited or time.monotonic() >= deadline:
            return state, None
        time.sleep(min(0.25, max(deadline - time.monotonic(), 0.0)))


def _service_components(
    result: dict[str, object], *, port: int, model: str, num_ctx: int,
    num_predict: int, keep_alive: str,
) -> tuple[str, str, bool]:
    status = result.get("status")
    live_value = result.get("live")
    if type(status) is not str or len(status) > 64 or type(live_value) is not bool:
        return "invalid", "unknown", False
    default_relation = "absent" if status == "not_configured" else "unknown"
    relation = result.get("process_identity_status", default_relation)
    if relation not in KNOWN_PROCESS_RELATIONS:
        relation = "unknown"
    live = live_value is True
    if live:
        if status != "running" or relation != "match":
            return "invalid", str(relation), False
        expected = {
            "port": port, "provider": "ollama", "model": model,
            "num_ctx": num_ctx, "num_predict": num_predict, "keep_alive": keep_alive,
        }
        if any(type(result.get(key)) is not type(value) or result.get(key) != value for key, value in expected.items()):
            return "configuration_mismatch", str(relation), False
    elif status == "running":
        return "invalid", str(relation), False
    elif status == "not_configured" and relation != "absent":
        return "invalid", str(relation), False
    elif status == "stale" and relation != "absent":
        return "invalid", str(relation), False
    elif status == "stale_pid_reused" and relation != "mismatch":
        return "invalid", str(relation), False
    elif status not in {
        "not_configured", "stale", "stale_pid_reused", "stopped", "failed",
        "legacy_unverified", "unreachable", "endpoint_mismatch",
        "identity_indeterminate", "identity_conflict",
    }:
        return "invalid", str(relation), False
    return status, str(relation), live


def _safe_service_start(status: str, relation: str) -> bool:
    return bool(
        (status == "not_configured" and relation == "absent")
        or (status == "stale" and relation == "absent")
        or (status == "stale_pid_reused" and relation == "mismatch")
        or (status in {"stopped", "failed"} and relation in {"absent", "mismatch"})
    )


def _read_service(
    home: Path, *, port: int, model: str, num_ctx: int,
    num_predict: int, keep_alive: str,
) -> tuple[str, str, bool]:
    try:
        result = service_status(home)
        if not isinstance(result, dict):
            raise TypeError("invalid service status")
        return _service_components(
            result, port=port, model=model, num_ctx=num_ctx,
            num_predict=num_predict, keep_alive=keep_alive,
        )
    except (OSError, RuntimeError, TypeError, ValueError, RecursionError):
        return "invalid", "unknown", False


def _store_unchanged(home: Path, pinned: dict[str, str]) -> bool:
    try:
        current = read_company_identity(home)
    except (CompanyIdentityError, OSError, TypeError, ValueError, RecursionError):
        return False
    return _valid_store_identity(current) and current == pinned


def _read_valid_manifest() -> dict[str, object] | None:
    try:
        manifest = check_project(PROJECT_ROOT)
        if not isinstance(manifest, dict) or manifest.get("status") != "ok":
            raise ManifestError("invalid manifest result")
        return manifest
    except (ManifestError, OSError, TypeError, ValueError, RecursionError):
        return None


def _manifest_unchanged(pinned: dict[str, object]) -> bool:
    current = _read_valid_manifest()
    return current is not None and current == pinned


def _full_readiness(home: Path, model: str) -> tuple[str, str]:
    try:
        result, code = run_readiness(model, home)
    except Exception:
        return "indeterminate", "inspect_local_runtime"
    expected_keys = {
        "schema", "status", "ready", "required_model", "components",
        "generation_tested", "blockers", "action",
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        return "indeterminate", "inspect_local_runtime"
    blockers = result.get("blockers")
    action = result.get("action")
    if (
        result.get("schema") != READINESS_SCHEMA
        or result.get("required_model") != model
        or result.get("generation_tested") is not False
        or not isinstance(result.get("components"), dict)
        or not isinstance(blockers, list)
        or any(type(item) is not str for item in blockers)
        or type(action) is not str
    ):
        return "indeterminate", "inspect_local_runtime"
    if (
        code == 0 and result.get("status") == "ready"
        and result.get("ready") is True and blockers == [] and action == "none"
    ):
        return "ready", "none"
    if (
        code == 1 and result.get("status") == "action_required"
        and result.get("ready") is False and blockers
    ):
        return "action_required", READINESS_ACTION_MAP.get(
            action, "inspect_local_runtime",
        )
    return "indeterminate", "inspect_local_runtime"


def _guard_locked(
    home: Path, pinned_identity: dict[str, str], *, port: int, model: str,
    num_ctx: int, num_predict: int, keep_alive: str, wait_seconds: int,
    ollama_executable: Path | None, allow_job_inheritance: bool,
) -> tuple[dict[str, object], int]:
    components = _empty_components()
    components["company_store"] = "valid"
    indeterminate: list[str] = []
    changes: list[str] = []
    attempt_events: set[str] = set()

    if not _store_unchanged(home, pinned_identity):
        components["company_store"] = "changed"
        return _payload(
            status="indeterminate", ready=False, components=components,
            blockers=["company_store_changed"], action="inspect_company_store",
            changes=changes, model=model,
        ), 2

    pinned_manifest = _read_valid_manifest()
    if pinned_manifest is None:
        components["disk_manifest"] = "invalid"
        return _payload(
            status="indeterminate", ready=False, components=components,
            blockers=["disk_manifest_invalid"], action="inspect_build_manifest",
            changes=changes, model=model,
        ), 2
    components["disk_manifest"] = "valid"

    ollama_state, model_installed = _probe_ollama(model)
    if ollama_state == "invalid":
        indeterminate.append("ollama_response_invalid")
    if ollama_state in {"refused", "unavailable"}:
        time.sleep(0.1)
        confirmation_state, confirmation_installed = _probe_ollama(model)
        if confirmation_state == "reachable":
            ollama_state, model_installed = confirmation_state, confirmation_installed
        elif confirmation_state not in {"refused", "unavailable"}:
            ollama_state, model_installed = "invalid", None
            indeterminate.append("ollama_response_invalid")
        elif _ollama_port_state() != "refused":
            ollama_state, model_installed = "invalid", None
            indeterminate.append("ollama_presence_indeterminate")
        else:
            process: subprocess.Popen[bytes] | None = None
            try:
                if not _store_unchanged(home, pinned_identity):
                    components["company_store"] = "changed"
                    return _payload(
                        status="indeterminate", ready=False, components=components,
                        blockers=["company_store_changed"], action="inspect_company_store",
                        changes=changes, model=model,
                    ), 2
                if not _manifest_unchanged(pinned_manifest):
                    components["disk_manifest"] = "invalid"
                    return _payload(
                        status="indeterminate", ready=False, components=components,
                        blockers=["disk_manifest_changed"], action="inspect_build_manifest",
                        changes=changes, model=model,
                    ), 2
                executable = _trusted_ollama_executable(ollama_executable)
                if executable is None:
                    attempt_events.add("ollama_executable_missing")
                else:
                    if allow_job_inheritance:
                        process = _spawn_ollama(
                            executable, allow_job_inheritance=True,
                        )
                    else:
                        process = _spawn_ollama(executable)
                    ollama_state, model_installed = _wait_for_ollama(
                        model, process, wait_seconds,
                    )
                    if ollama_state == "reachable":
                        changes.append("ollama_started")
                    else:
                        if not _terminate_owned_child(process):
                            indeterminate.append("ollama_cleanup_failed")
                        if ollama_state == "invalid":
                            indeterminate.append("ollama_response_invalid")
                        else:
                            indeterminate.append("ollama_start_unconfirmed")
            except GuardExecutableError:
                ollama_state, model_installed = "refused", None
                indeterminate.append("ollama_executable_invalid")
            except (OSError, subprocess.SubprocessError):
                if process is not None and not _terminate_owned_child(process):
                    indeterminate.append("ollama_cleanup_failed")
                ollama_state, model_installed = "refused", None
                attempt_events.add("ollama_start_failed")
            except Exception:
                if process is not None and not _terminate_owned_child(process):
                    indeterminate.append("ollama_cleanup_failed")
                ollama_state, model_installed = "refused", None
                indeterminate.append("ollama_start_failed")

    if ollama_state == "reachable":
        components["ollama"] = "reachable"
        components["model"] = "installed" if model_installed else "missing"
    elif ollama_state == "invalid":
        components["ollama"] = "invalid"
        components["model"] = "unknown"
    else:
        components["ollama"] = "unavailable"
        components["model"] = "unknown"

    backend_ready = components["ollama"] == "reachable" and components["model"] == "installed"
    service_state, relation, service_live = _read_service(
        home, port=port, model=model, num_ctx=num_ctx,
        num_predict=num_predict, keep_alive=keep_alive,
    )
    safe_start = _safe_service_start(service_state, relation)
    if safe_start and backend_ready and components["disk_manifest"] == "valid":
        if not _store_unchanged(home, pinned_identity):
            components["company_store"] = "changed"
            indeterminate.append("company_store_changed")
        elif not _manifest_unchanged(pinned_manifest):
            components["disk_manifest"] = "invalid"
            indeterminate.append("disk_manifest_changed")
        else:
            try:
                start_arguments = {
                    "port": port, "provider": "ollama", "model": model,
                    "num_ctx": num_ctx, "num_predict": num_predict,
                    "keep_alive": keep_alive,
                }
                if allow_job_inheritance:
                    start_arguments["allow_job_inheritance"] = True
                started = start_service(home, **start_arguments)
                state, started_relation, started_live = _service_components(
                    started, port=port, model=model, num_ctx=num_ctx,
                    num_predict=num_predict, keep_alive=keep_alive,
                )
                if started_live:
                    changes.append("service_started")
                else:
                    attempt_events.add("service_start_failed")
                service_state, relation, service_live = state, started_relation, started_live
            except (OSError, RuntimeError, TypeError, ValueError, RecursionError):
                service_state, relation, service_live = _read_service(
                    home, port=port, model=model, num_ctx=num_ctx,
                    num_predict=num_predict, keep_alive=keep_alive,
                )
                if not service_live:
                    attempt_events.add("service_start_failed")

    pre_ollama_state, pre_model_installed = _probe_ollama(model)
    pre_service_state, pre_relation, pre_service_live = _read_service(
        home, port=port, model=model, num_ctx=num_ctx,
        num_predict=num_predict, keep_alive=keep_alive,
    )
    pre_store_valid = _store_unchanged(home, pinned_identity)
    pre_manifest_valid = _manifest_unchanged(pinned_manifest)
    preliminary_ready = bool(
        pre_ollama_state == "reachable" and pre_model_installed is True
        and pre_service_live and pre_service_state == "running"
        and pre_relation == "match" and pre_store_valid and pre_manifest_valid
        and not indeterminate
    )
    readiness_action = "inspect_local_runtime"
    if preliminary_ready:
        components["readiness"], readiness_action = _full_readiness(home, model)
    else:
        components["readiness"] = "not_run"

    final_ollama_state, final_model_installed = _probe_ollama(model)
    service_state, relation, service_live = _read_service(
        home, port=port, model=model, num_ctx=num_ctx,
        num_predict=num_predict, keep_alive=keep_alive,
    )
    blockers: list[str] = []
    if final_ollama_state == "reachable":
        components["ollama"] = "reachable"
        components["model"] = "installed" if final_model_installed else "missing"
        if final_model_installed is False:
            blockers.append("model_not_installed")
    elif final_ollama_state == "invalid":
        components["ollama"] = "invalid"
        components["model"] = "unknown"
        indeterminate.append("ollama_response_invalid")
    else:
        components["ollama"] = "unavailable"
        components["model"] = "unknown"
        blockers.append("ollama_unavailable")
        if "ollama_executable_missing" in attempt_events:
            blockers.append("ollama_executable_missing")
        if "ollama_start_failed" in attempt_events:
            blockers.append("ollama_start_failed")

    components["service"] = "live" if service_live else service_state
    components["process_identity"] = relation
    if not _store_unchanged(home, pinned_identity):
        components["company_store"] = "changed"
        indeterminate.append("company_store_changed")
    if not _manifest_unchanged(pinned_manifest):
        components["disk_manifest"] = "invalid"
        indeterminate.append("disk_manifest_changed")
    final_backend_ready = (
        components["ollama"] == "reachable" and components["model"] == "installed"
    )
    final_safe_start = _safe_service_start(service_state, relation)
    if not service_live:
        if service_state == "legacy_unverified":
            blockers.append("legacy_service_requires_migration")
        elif service_state == "configuration_mismatch":
            blockers.append("service_configuration_mismatch")
        elif relation in {"legacy", "unavailable", "unknown"} or service_state in {
            "invalid", "unreachable", "endpoint_mismatch", "identity_indeterminate",
            "identity_conflict",
        }:
            indeterminate.append("service_identity_or_endpoint_indeterminate")
        elif final_safe_start and not final_backend_ready:
            blockers.append("service_waiting_for_ready_runtime")
        elif final_safe_start and components["disk_manifest"] != "valid":
            blockers.append("service_waiting_for_valid_build")
        elif "service_start_failed" in attempt_events:
            blockers.append("service_start_failed")
        else:
            blockers.append("service_not_live")

    post_snapshot_ready = bool(
        service_live and components["disk_manifest"] == "valid"
        and components["ollama"] == "reachable" and components["model"] == "installed"
        and components["company_store"] == "valid"
    )
    if components["readiness"] == "action_required":
        blockers.append("full_readiness_action_required")
    elif components["readiness"] == "indeterminate":
        indeterminate.append("full_readiness_indeterminate")
    elif components["readiness"] != "ready" and post_snapshot_ready:
        indeterminate.append("full_readiness_not_confirmed")

    ready = bool(
        post_snapshot_ready and components["readiness"] == "ready"
        and not indeterminate and not blockers
    )
    if ready:
        status = "recovered" if changes else "ready"
        action = "none"
        code = 0
    elif indeterminate:
        status = "indeterminate"
        if "company_store_changed" in indeterminate:
            action = "inspect_company_store"
        elif "disk_manifest_invalid" in indeterminate or "disk_manifest_changed" in indeterminate:
            action = "inspect_build_manifest"
        elif "service_identity_or_endpoint_indeterminate" in indeterminate:
            action = "inspect_local_service"
        elif (
            "ollama_response_invalid" in indeterminate
            or "ollama_executable_invalid" in indeterminate
            or "ollama_cleanup_failed" in indeterminate
            or "ollama_presence_indeterminate" in indeterminate
            or "ollama_start_unconfirmed" in indeterminate
            or "ollama_start_failed" in indeterminate
        ):
            action = "inspect_ollama_service"
        else:
            action = "inspect_local_runtime"
        code = 2
    else:
        status = "action_required"
        if "legacy_service_requires_migration" in blockers:
            action = "migrate_legacy_service"
        elif "service_configuration_mismatch" in blockers:
            action = "relaunch_service_manually"
        elif "full_readiness_action_required" in blockers:
            action = readiness_action
        elif "ollama_executable_missing" in blockers or "ollama_start_failed" in blockers or "ollama_unavailable" in blockers:
            action = "start_ollama_manually"
        elif "model_not_installed" in blockers:
            action = "install_configured_model"
        elif "service_start_failed" in blockers or "service_not_live" in blockers:
            action = "start_local_service_manually"
        else:
            action = "inspect_local_runtime"
        code = 1
    return _payload(
        status=status, ready=ready, components=components,
        blockers=indeterminate + blockers, action=action, changes=changes, model=model,
    ), code


def guard_once(
    home: Path, *, port: int = 8765, model: str = "qwen3.5:0.8b",
    num_ctx: int = 4096, num_predict: int = 2048, keep_alive: str = "30s",
    wait_seconds: int = 10, ollama_executable: Path | None = None,
    allow_job_inheritance: bool = False,
    record_result: bool = False,
) -> tuple[dict[str, object], int]:
    if (
        type(allow_job_inheritance) is not bool
        or type(record_result) is not bool
        or (allow_job_inheritance and os.name != "nt")
        or not _valid_runtime_arguments(
            port, model, num_ctx, num_predict, keep_alive, wait_seconds,
        )
    ):
        raise GuardUsageError("invalid runtime arguments")
    components = _empty_components()
    try:
        normalized_home = _normalized_company_home(Path(home))
        identity = read_company_identity(normalized_home)
        if not _valid_store_identity(identity):
            raise CompanyIdentityError("invalid_identity")
        pinned_identity = dict(identity)
        components["company_store"] = "valid"
    except (CompanyIdentityError, OSError, TypeError, ValueError, RecursionError):
        components["company_store"] = "invalid"
        return _payload(
            status="indeterminate", ready=False, components=components,
            blockers=["company_store_invalid"], action="inspect_company_store",
            changes=[], model=model,
        ), 2
    try:
        with _runtime_guard_lock(normalized_home):
            result, code = _guard_locked(
                normalized_home, pinned_identity, port=port, model=model,
                num_ctx=num_ctx, num_predict=num_predict, keep_alive=keep_alive,
                wait_seconds=wait_seconds, ollama_executable=ollama_executable,
                allow_job_inheritance=allow_job_inheritance,
            )
            result_components = result.get("components") if isinstance(result, dict) else None
            if (
                record_result and isinstance(result_components, dict)
                and result_components.get("company_store") == "valid"
            ):
                if not _store_unchanged(normalized_home, pinned_identity):
                    return _store_changed_after_result(result, model)
                try:
                    _write_result_journal(
                        normalized_home, pinned_identity, _render_result(result),
                    )
                except GuardStoreChangedError:
                    return _store_changed_after_result(result, model)
                except GuardResultJournalError:
                    return _result_journal_failure(result, model)
            return result, code
    except GuardBusyError:
        return _payload(
            status="action_required", ready=False, components=components,
            blockers=["runtime_guard_busy"], action="wait_for_runtime_guard",
            changes=[], model=model,
        ), 1
    except GuardLockError:
        return _payload(
            status="indeterminate", ready=False, components=components,
            blockers=["runtime_guard_lock_invalid"], action="inspect_runtime_guard",
            changes=[], model=model,
        ), 2


def parser() -> argparse.ArgumentParser:
    result = _SanitizedArgumentParser(
        description="Recover confirmed-missing local runtime components without running missions.",
    )
    result.add_argument("--home", type=Path)
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--model", default="qwen3.5:0.8b")
    result.add_argument("--num-ctx", type=int, default=4096)
    result.add_argument("--num-predict", type=int, default=2048)
    result.add_argument("--keep-alive", default="30s")
    result.add_argument("--wait-seconds", type=int, default=10)
    result.add_argument("--ollama-executable", type=Path)
    result.add_argument("--allow-windows-job-inheritance", action="store_true")
    result.add_argument("--record-result", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        home = default_company_home() if args.home is None else args.home
        result, code = guard_once(
            home, port=args.port, model=args.model, num_ctx=args.num_ctx,
            num_predict=args.num_predict, keep_alive=args.keep_alive,
            wait_seconds=args.wait_seconds, ollama_executable=args.ollama_executable,
            allow_job_inheritance=args.allow_windows_job_inheritance,
            record_result=args.record_result,
        )
    except GuardUsageError:
        result = _payload(
            status="indeterminate", ready=False, components=_empty_components(),
            blockers=["invalid_arguments"], action="fix_command_arguments",
            changes=[], model=None,
        )
        code = 3
    except Exception:
        result = _payload(
            status="indeterminate", ready=False, components=_empty_components(),
            blockers=["internal_guard_error"], action="inspect_runtime_guard",
            changes=[], model=None,
        )
        code = 2
    try:
        rendered = _render_result(result)
    except GuardResultJournalError:
        result = _payload(
            status="indeterminate", ready=False, components=_empty_components(),
            blockers=["internal_guard_error"], action="inspect_runtime_guard",
            changes=[], model=None,
        )
        code = 2
        rendered = _render_result(result)
    _write_stdout(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
