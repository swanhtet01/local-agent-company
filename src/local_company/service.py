from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from .config import restrict_file_to_current_user
from .model_policy import DEFAULT_LOCAL_MODEL, require_local_llama_model


SERVICE_STATE_SCHEMA = "local-company.service.v2"
PROCESS_BIRTH_SCHEMA = "local-company.process-birth.v1"
_ACTIVE_STATES = {"starting", "running", "cleanup_failed"}
_STATE_STATUSES = _ACTIVE_STATES | {"stopped", "failed"}
_MAX_STATE_BYTES = 64 * 1024
_MAX_HEALTH_BYTES = 64 * 1024
_SERVICE_PROBE_TIMEOUT_SECONDS = 6
_STARTUP_HEALTH_ATTEMPTS = 120
_STARTUP_HEALTH_DEADLINE_SECONDS = 60
_LOWER_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_URLSAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,512}\Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(home: Path) -> Path:
    return home / "service.json"


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_state(home: Path) -> dict[str, object] | None:
    path = _state_path(home)
    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Could not read local service state") from exc
    if len(payload) > _MAX_STATE_BYTES:
        raise RuntimeError("Local service state is too large")
    try:
        state = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise RuntimeError("Local service state is malformed") from exc
    if not isinstance(state, dict):
        raise RuntimeError("Local service state must be a JSON object")
    return state


def _write_state(home: Path, state: dict[str, object]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = _state_path(home)
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > _MAX_STATE_BYTES:
        raise RuntimeError("Local service state is too large")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # service.json carries the plaintext bearer token that authenticates
        # every mutating dashboard action. On Windows, chmod(0o600) above is
        # a no-op for actually restricting other accounts -- it only toggles
        # the read-only attribute -- so it is not a real confidentiality
        # control there the way it is on POSIX. This is the genuine one.
        restrict_file_to_current_user(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _valid_process_birth(value: object) -> bool:
    return isinstance(value, str) and _LOWER_HEX_64.fullmatch(value) is not None


def _valid_service_instance_id(value: object) -> bool:
    return isinstance(value, str) and _LOWER_HEX_32.fullmatch(value) is not None


def _valid_service_token(value: object) -> bool:
    return isinstance(value, str) and _URLSAFE_TOKEN.fullmatch(value) is not None


def _validated_state(state: dict[str, object]) -> dict[str, object]:
    status = state.get("status")
    if not isinstance(status, str) or status not in _STATE_STATUSES:
        raise RuntimeError("Local service state has an invalid status")
    pid = state.get("pid")
    if not _valid_int(pid, 1, 0xFFFFFFFF):
        raise RuntimeError("Local service state has an invalid PID")
    port = state.get("port")
    if not _valid_int(port, 1, 65535):
        raise RuntimeError("Local service state has an invalid port")
    token = state.get("token")
    if not _valid_service_token(token):
        raise RuntimeError("Local service state has an invalid service token")

    identity_keys = {
        "state_schema", "process_birth_schema", "process_birth", "service_instance_id",
    }
    if any(key in state for key in identity_keys):
        if state.get("state_schema") != SERVICE_STATE_SCHEMA:
            raise RuntimeError("Local service state has an unsupported schema")
        if state.get("process_birth_schema") != PROCESS_BIRTH_SCHEMA:
            raise RuntimeError("Local service state has an unsupported process identity schema")
        if not _valid_process_birth(state.get("process_birth")):
            raise RuntimeError("Local service state has an invalid process identity")
        if not _valid_service_instance_id(state.get("service_instance_id")):
            raise RuntimeError("Local service state has an invalid service instance ID")
    return state


@dataclass(frozen=True)
class _ProcessObservation:
    status: Literal["present", "absent", "unavailable"]
    birth: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"present", "absent", "unavailable"}:
            raise ValueError("Invalid process observation status")
        if self.status == "present":
            if not _valid_process_birth(self.birth):
                raise ValueError("A present process observation requires a valid birth fingerprint")
        elif self.birth is not None:
            raise ValueError("A non-present process observation cannot include a fingerprint")


class _ProcessGuard:
    def __init__(
        self, pid: int, observation: _ProcessObservation,
        wait_exact: Callable[[float], Literal["exited", "alive", "unavailable"]] | None = None,
        close_exact: Callable[[], None] | None = None,
    ) -> None:
        self.pid = pid
        self.observation = observation
        self._wait_exact = wait_exact
        self._close_exact = close_exact
        self._closed = False

    def wait_for_exit(self, timeout: float) -> Literal["exited", "alive", "unavailable"]:
        if self._wait_exact is not None:
            return self._wait_exact(timeout)
        if self.observation.status == "absent":
            return "exited"
        if self.observation.status == "unavailable":
            return "unavailable"
        expected = self.observation.birth
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            current = _observe_process(self.pid)
            if current.status == "absent" or (
                current.status == "present" and current.birth != expected
            ):
                return "exited"
            if current.status == "unavailable":
                return "unavailable"
            if time.monotonic() >= deadline:
                return "alive"
            time.sleep(min(0.05, max(deadline - time.monotonic(), 0.0)))

    def close(self) -> None:
        if not self._closed and self._close_exact is not None:
            self._close_exact()
        self._closed = True

    def __enter__(self) -> _ProcessGuard:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _birth_digest(platform: str, pid: int, *kernel_values: str) -> str:
    digest = hashlib.sha256()
    for value in (PROCESS_BIRTH_SCHEMA, platform, str(pid), *kernel_values):
        encoded = value.encode("utf-8", errors="strict")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _open_windows_process_guard(pid: int) -> _ProcessGuard:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error in {87, 1168}:
            return _ProcessGuard(pid, _ProcessObservation("absent"))
        return _ProcessGuard(pid, _ProcessObservation("unavailable"))

    def close_exact() -> None:
        kernel32.CloseHandle(handle)

    def wait_exact(timeout: float) -> Literal["exited", "alive", "unavailable"]:
        milliseconds = min(max(int(max(timeout, 0.0) * 1000), 0), 0xFFFFFFFE)
        result = int(kernel32.WaitForSingleObject(handle, milliseconds))
        if result == 0:
            return "exited"
        if result == 0x102:
            return "alive"
        return "unavailable"

    initial_wait = wait_exact(0.0)
    if initial_wait == "exited":
        return _ProcessGuard(pid, _ProcessObservation("absent"), wait_exact, close_exact)
    if initial_wait != "alive":
        return _ProcessGuard(pid, _ProcessObservation("unavailable"), wait_exact, close_exact)

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    creation = FILETIME()
    exit_time = FILETIME()
    kernel_time = FILETIME()
    user_time = FILETIME()
    if not kernel32.GetProcessTimes(
        handle, ctypes.byref(creation), ctypes.byref(exit_time),
        ctypes.byref(kernel_time), ctypes.byref(user_time),
    ):
        return _ProcessGuard(pid, _ProcessObservation("unavailable"), wait_exact, close_exact)
    image = ctypes.create_unicode_buffer(32768)
    image_size = wintypes.DWORD(len(image))
    if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(image_size)):
        return _ProcessGuard(pid, _ProcessObservation("unavailable"), wait_exact, close_exact)
    creation_value = (int(creation.high) << 32) | int(creation.low)
    normalized_image = os.path.normcase(image.value)
    image_digest = hashlib.sha256(normalized_image.encode("utf-8")).hexdigest()
    birth = _birth_digest("windows", pid, f"{creation_value:016x}", image_digest)
    return _ProcessGuard(pid, _ProcessObservation("present", birth), wait_exact, close_exact)


def _bounded_text(path: Path, maximum: int) -> str:
    with path.open("rb") as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("Kernel process identity field is too large")
    return payload.decode("ascii", errors="strict").strip()


def _linux_process_observation(pid: int) -> _ProcessObservation:
    try:
        boot_id = _bounded_text(Path("/proc/sys/kernel/random/boot_id"), 128)
        if re.fullmatch(r"[0-9a-fA-F-]{32,40}", boot_id) is None:
            return _ProcessObservation("unavailable")
    except (OSError, UnicodeDecodeError, ValueError):
        return _ProcessObservation("unavailable")
    try:
        process_stat = _bounded_text(Path(f"/proc/{pid}/stat"), 4096)
        close_paren = process_stat.rfind(")")
        if close_paren < 1:
            return _ProcessObservation("unavailable")
        fields = process_stat[close_paren + 1:].split()
        if len(fields) <= 19 or not fields[19].isdigit():
            return _ProcessObservation("unavailable")
        start_ticks = fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return _ProcessObservation("absent")
    except (OSError, UnicodeDecodeError, ValueError):
        return _ProcessObservation("unavailable")
    return _ProcessObservation("present", _birth_digest("linux", pid, boot_id.lower(), start_ticks))


def _open_process_guard(pid: int) -> _ProcessGuard:
    if not _valid_int(pid, 1, 0xFFFFFFFF):
        return _ProcessGuard(0, _ProcessObservation("absent"))
    if os.name == "nt":
        return _open_windows_process_guard(pid)
    if sys.platform.startswith("linux"):
        return _ProcessGuard(pid, _linux_process_observation(pid))
    return _ProcessGuard(pid, _ProcessObservation("unavailable"))


def _process_identity_supported() -> bool:
    return os.name == "nt" or sys.platform.startswith("linux")


def _observe_process(pid: int) -> _ProcessObservation:
    with _open_process_guard(pid) as guard:
        return guard.observation


def _pid_exists(pid: int) -> bool:
    return _observe_process(pid).status != "absent"


def _open_regular_lock_file(path: Path) -> int:
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise RuntimeError("Local service lifecycle lock is unavailable") from exc
    if before is not None and (
        not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
    ):
        raise RuntimeError("Local service lifecycle lock is not a private regular file")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise RuntimeError("Local service lifecycle lock is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RuntimeError("Local service lifecycle lock changed or is not a regular file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _startup_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "service.start.lock"
    descriptor = _open_regular_lock_file(path)
    locked = False
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (OSError, AttributeError):
            # AttributeError: fchmod is absent on Windows on every standard
            # CPython build except a small number of unusually recent ones --
            # already best-effort, so treat its absence the same as its failure.
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
            raise RuntimeError("Local company service lifecycle change is already in progress") from exc
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


def _probe(port: int) -> dict[str, object] | None:
    if not _valid_int(port, 1, 65535):
        return None
    try:
        with _loopback_opener().open(
            f"http://127.0.0.1:{port}/__service/health.json",
            timeout=_SERVICE_PROBE_TIMEOUT_SECONDS,
        ) as response:
            payload = response.read(_MAX_HEALTH_BYTES + 1)
        if len(payload) > _MAX_HEALTH_BYTES:
            return None
        health = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        return health if isinstance(health, dict) else None
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
    except (
        urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError,
        json.JSONDecodeError, ValueError, RecursionError,
    ):
        return None


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _service_python_executable() -> str:
    # On Windows, a venv python.exe can remain as a launcher parent while the
    # base interpreter becomes the HTTP server. Spawn the base executable
    # directly so the captured child PID is also the serving PID.
    raw = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError("Service Python executable is not a trusted regular file")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Service Python executable is unavailable") from exc
    # Deliberately no candidate.is_symlink() rejection: that check ran BEFORE
    # resolution and rejected sys.executable outright on any interpreter
    # reached through a symlink -- which is not a rare edge case on Linux, it
    # is the norm. GitHub Actions' own hosted Python (bin/python -> a
    # versioned binary) is a symlink, and so is virtually every pyenv,
    # Homebrew, or Linux-distro-packaged Python. Resolving the FULL chain to
    # its real target with strict=True and then validating THAT target below
    # is the safe way to trust a possibly-symlinked path; rejecting the
    # symlink before ever resolving it added no safety the resolution does
    # not already provide, while breaking the common case outright.
    if not resolved.is_file():
        raise RuntimeError("Service Python executable is not a trusted regular file")
    return str(resolved)


def _has_current_identity(state: dict[str, object]) -> bool:
    return state.get("state_schema") == SERVICE_STATE_SCHEMA


def _identity_relation(
    state: dict[str, object], observation: _ProcessObservation,
) -> Literal["match", "mismatch", "absent", "unavailable", "legacy"]:
    if not _has_current_identity(state):
        return "legacy"
    if observation.status != "present":
        return observation.status
    return "match" if observation.birth == state.get("process_birth") else "mismatch"


def _health_matches(state: dict[str, object], health: dict[str, object] | None) -> bool:
    instance_id = health.get("service_instance_id") if health else None
    return bool(
        health
        and health.get("status") == "ready"
        and type(health.get("pid")) is int
        and health.get("pid") == state.get("pid")
        and _valid_service_instance_id(instance_id)
        and secrets.compare_digest(
            instance_id, str(state.get("service_instance_id", "")),
        )
    )


def _public_state(state: dict[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in state.items()
        if key not in {"token", "process_birth"}
    }


def _status_result(
    state: dict[str, object], relation: str, health: dict[str, object] | None = None,
) -> dict[str, object]:
    recorded_status = str(state["status"])
    live = False
    public_health = None
    if relation == "legacy":
        status = "legacy_unverified" if recorded_status in _ACTIVE_STATES else recorded_status
    elif relation == "match":
        if recorded_status not in _ACTIVE_STATES:
            status = "identity_conflict"
        elif _health_matches(state, health):
            status = "running"
            live = True
            public_health = health
        else:
            status = "unreachable" if health is None else "endpoint_mismatch"
    elif relation == "mismatch":
        status = "stale_pid_reused" if recorded_status in _ACTIVE_STATES else recorded_status
    elif relation == "absent":
        status = "stale" if recorded_status in _ACTIVE_STATES else recorded_status
    else:
        status = "identity_indeterminate"
    return {
        **_public_state(state),
        "status": status,
        "live": live,
        "process_identity_status": relation,
        "health": public_health,
    }


def service_status(home: Path) -> dict[str, object]:
    home = home.resolve()
    state = _read_state(home)
    if state is None:
        return {"status": "not_configured", "home": str(home), "live": False}
    state = _validated_state(state)
    if not _has_current_identity(state):
        return _status_result(state, "legacy")
    first = _observe_process(int(state["pid"]))
    relation = _identity_relation(state, first)
    health = _probe(int(state["port"])) if relation == "match" else None
    if relation == "match":
        second = _observe_process(int(state["pid"]))
        relation = _identity_relation(state, second)
        if relation != "match":
            health = None
    return _status_result(state, relation, health)


def _terminate_owned_child(process: subprocess.Popen[bytes]) -> bool:
    try:
        if process.poll() is not None:
            return True
    except OSError:
        pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
            return True
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass
    try:
        return process.poll() is not None
    except OSError:
        return False


def _windows_detached_creation_flags() -> int:
    names = (
        "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB",
    )
    values = tuple(getattr(subprocess, name, None) for name in names)
    if any(type(value) is not int or value <= 0 for value in values):
        raise RuntimeError("Required detached-process creation flags are unavailable")
    return values[0] | values[1] | values[2]


def _windows_inherited_creation_flags() -> int:
    names = ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP")
    values = tuple(getattr(subprocess, name, None) for name in names)
    if any(type(value) is not int or value <= 0 for value in values):
        raise RuntimeError("Required inherited-process creation flags are unavailable")
    return values[0] | values[1]


def _windows_breakaway_denied(error: OSError) -> bool:
    return getattr(error, "winerror", None) == 5


def _existing_state_blocks_start(state: dict[str, object]) -> tuple[bool, str]:
    observation = _observe_process(int(state["pid"]))
    if not _has_current_identity(state):
        if state["status"] in _ACTIVE_STATES and observation.status != "absent":
            return True, "legacy service identity is still present or cannot be verified"
        return False, "legacy service process is absent"
    relation = _identity_relation(state, observation)
    if relation == "match":
        return True, "recorded service process is still alive"
    if relation == "unavailable":
        return True, "recorded service process identity is unavailable"
    return False, f"recorded service process is {relation}"


def start_service(
    home: Path, port: int = 8765, provider: str = "ollama", model: str = DEFAULT_LOCAL_MODEL,
    num_ctx: int = 4096, num_predict: int = 2048, keep_alive: str = "30s",
    allow_job_inheritance: bool = False,
) -> dict[str, object]:
    home = home.resolve()
    if not _valid_int(port, 1, 65535):
        raise ValueError("Service port must be between 1 and 65535")
    if provider not in {"mock", "ollama"}:
        raise ValueError("Service provider must be mock or ollama")
    if provider == "ollama":
        model = require_local_llama_model(model)
    if not _valid_int(num_ctx, 1024, 131072):
        raise ValueError("num_ctx must be between 1024 and 131072")
    if not _valid_int(num_predict, 32, 4096):
        raise ValueError("num_predict must be between 32 and 4096")
    if type(allow_job_inheritance) is not bool:
        raise ValueError("allow_job_inheritance must be a boolean")
    if not _process_identity_supported():
        raise RuntimeError("Detached service process identity is supported only on Windows and Linux")
    with _startup_lock(home):
        recorded = _read_state(home)
        if recorded is not None:
            recorded = _validated_state(recorded)
            blocked, reason = _existing_state_blocks_start(recorded)
            if blocked:
                raise RuntimeError(f"Local company service cannot start: {reason}")
        if _port_in_use(port):
            raise RuntimeError(f"Local company service cannot start: loopback port {port} is in use")

        token = secrets.token_urlsafe(32)
        service_instance_id = secrets.token_hex(16)
        log_path = home / "service.log"
        project_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(project_root / "src") + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        environment["LOCAL_COMPANY_SERVICE_TOKEN"] = token
        environment["LOCAL_COMPANY_SERVICE_INSTANCE_ID"] = service_instance_id
        command = [
            _service_python_executable(), "-m", "local_company.cli", "--home", str(home),
            "dashboard", "--port", str(port), "--provider", provider, "--model", model,
            "--num-ctx", str(num_ctx), "--num-predict", str(num_predict),
            "--keep-alive", keep_alive,
        ]
        creationflags = 0
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            creationflags = _windows_detached_creation_flags()
        else:
            popen_kwargs["start_new_session"] = True
        process: subprocess.Popen[bytes] | None = None
        state: dict[str, object] | None = None
        readiness_failure = "startup_timeout"
        try:
            with log_path.open("ab") as log:
                try:
                    process = subprocess.Popen(
                        command, cwd=project_root, env=environment,
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, close_fds=True,
                        creationflags=creationflags, **popen_kwargs,
                    )
                except OSError as exc:
                    if not (
                        os.name == "nt" and allow_job_inheritance
                        and _windows_breakaway_denied(exc)
                    ):
                        raise
                    process = subprocess.Popen(
                        command, cwd=project_root, env=environment,
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, close_fds=True,
                        creationflags=_windows_inherited_creation_flags(),
                    )
            if process.poll() is not None:
                raise RuntimeError("Dashboard child exited before identity capture")
            observation = _observe_process(process.pid)
            if observation.status != "present" or process.poll() is not None:
                raise RuntimeError(
                    "Dashboard child process identity could not be captured safely"
                )
            state = {
                "state_schema": SERVICE_STATE_SCHEMA,
                "status": "starting", "pid": process.pid, "port": port, "home": str(home),
                "token": token, "service_instance_id": service_instance_id,
                "process_birth_schema": PROCESS_BIRTH_SCHEMA,
                "process_birth": observation.birth,
                "log_path": str(log_path), "started_at": _utc_now(),
                "provider": provider, "model": model, "num_ctx": num_ctx,
                "num_predict": num_predict, "keep_alive": keep_alive,
            }
            _write_state(home, state)
            # Low-power Windows hosts can spend longer importing the dashboard
            # and scheduling a loopback response after a cold working-set trim.
            # Keep both attempts and elapsed startup time bounded.
            startup_deadline = time.monotonic() + _STARTUP_HEALTH_DEADLINE_SECONDS
            for _ in range(_STARTUP_HEALTH_ATTEMPTS):
                if time.monotonic() >= startup_deadline:
                    break
                if process.poll() is not None:
                    readiness_failure = "child_exited"
                    break
                current = _observe_process(process.pid)
                if _identity_relation(state, current) != "match":
                    readiness_failure = "identity_changed"
                    break
                health = _probe(port)
                if _health_matches(state, health):
                    confirmed = _observe_process(process.pid)
                    if process.poll() is None and _identity_relation(state, confirmed) == "match":
                        state["status"] = "running"
                        _write_state(home, state)
                        return _status_result(state, "match", health)
                    readiness_failure = "identity_changed"
                    break
                readiness_failure = (
                    "endpoint_unavailable" if health is None
                    else "endpoint_identity_mismatch"
                )
                time.sleep(0.5)
        except BaseException as exc:
            cleaned = process is None or _terminate_owned_child(process)
            if not cleaned and process is not None:
                if state is None:
                    state = {
                        "status": "cleanup_failed", "pid": process.pid, "port": port,
                        "home": str(home), "token": token,
                        "log_path": str(log_path), "started_at": _utc_now(),
                        "provider": provider, "model": model, "num_ctx": num_ctx,
                        "num_predict": num_predict, "keep_alive": keep_alive,
                    }
                else:
                    state["status"] = "cleanup_failed"
                state["failed_at"] = _utc_now()
                try:
                    _write_state(home, state)
                except Exception:
                    pass
                if isinstance(exc, Exception):
                    raise RuntimeError(
                        f"Dashboard launch failed and its child could not be reaped; inspect {log_path}"
                    ) from exc
            raise

        assert process is not None and state is not None
        cleaned = _terminate_owned_child(process)
        state["status"] = "failed" if cleaned else "cleanup_failed"
        state["failed_at"] = _utc_now()
        _write_state(home, state)
        if not cleaned:
            raise RuntimeError(
                f"Dashboard failed to become ready and its child could not be reaped; inspect {log_path}"
            )
        raise RuntimeError(
            f"Dashboard service failed to become ready ({readiness_failure}); "
            f"inspect {log_path}"
        )


def stop_service(home: Path) -> dict[str, object]:
    home = home.resolve()
    with _startup_lock(home):
        state = _read_state(home)
        if state is None:
            raise ValueError("Local company service is not configured")
        state = _validated_state(state)
        if not _has_current_identity(state):
            raise RuntimeError(
                "Legacy service identity cannot be stopped safely; stop it with the prior build, then restart"
            )
        with _open_process_guard(int(state["pid"])) as guard:
            relation = _identity_relation(state, guard.observation)
            if relation in {"absent", "mismatch"}:
                return _status_result(state, relation)
            if relation != "match":
                raise RuntimeError("Local service process identity is unavailable; shutdown refused")
            health = _probe(int(state["port"]))
            if not _health_matches(state, health):
                raise RuntimeError("Local service endpoint identity does not match; shutdown refused")
            exact_state = guard.wait_for_exit(0.0)
            if exact_state == "exited":
                return _status_result(state, "absent")
            if exact_state != "alive":
                raise RuntimeError("Could not verify exact local service process liveness")
            request = urllib.request.Request(
                f"http://127.0.0.1:{state['port']}/__service/stop", data=b"", method="POST",
                headers={
                    "X-Service-Token": str(state["token"]),
                    "X-Service-Instance": str(state["service_instance_id"]),
                },
            )
            try:
                with _loopback_opener().open(request, timeout=3) as response:
                    if response.status != 202:
                        raise RuntimeError(
                            f"Service rejected shutdown with HTTP {response.status}"
                        )
            except urllib.error.HTTPError as exc:
                exc.close()
                raise RuntimeError("Could not stop local service") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError("Could not stop local service") from exc
            exit_result = guard.wait_for_exit(5.0)
            if exit_result == "unavailable":
                raise RuntimeError("Could not verify exact local service process exit")
            if exit_result != "exited":
                raise RuntimeError("Local service did not stop within five seconds")
        state["status"] = "stopped"
        state["stopped_at"] = _utc_now()
        _write_state(home, state)
        return _status_result(state, "absent")
