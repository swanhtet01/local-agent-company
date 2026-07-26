from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(home: Path) -> Path:
    return home / "service.json"


def _read_state(home: Path) -> dict[str, object] | None:
    path = _state_path(home)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(home: Path, state: dict[str, object]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = _state_path(home)
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
    finally:
        if temporary.exists():
            temporary.unlink()


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def _startup_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "service.start.lock"
    descriptor = None
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            break
        except FileExistsError:
            try:
                owner_pid = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                owner_pid = 0
            if _pid_exists(owner_pid):
                raise RuntimeError(f"Local company service startup is already in progress as PID {owner_pid}")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    if descriptor is None:
        raise RuntimeError("Could not acquire local service startup lock")
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _probe(port: int) -> dict[str, object] | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health.json", timeout=2) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def service_status(home: Path) -> dict[str, object]:
    home = home.resolve()
    state = _read_state(home)
    if not state:
        return {"status": "not_configured", "home": str(home)}
    port = int(state.get("port", 8765))
    health = _probe(port)
    recorded_pid = int(state.get("pid", 0))
    live = bool(health and int(health.get("pid", -1)) == recorded_pid)
    return {
        **{key: value for key, value in state.items() if key != "token"},
        "status": "running" if live else state.get("status", "stopped"),
        "live": live,
        "health": health if live else None,
    }


def start_service(
    home: Path, port: int = 8765, provider: str = "ollama", model: str = "qwen3.5:0.8b",
    num_ctx: int = 4096, num_predict: int = 2048, keep_alive: str = "30s",
) -> dict[str, object]:
    home = home.resolve()
    if port < 1 or port > 65535:
        raise ValueError("Service port must be between 1 and 65535")
    if provider not in {"mock", "ollama"}:
        raise ValueError("Service provider must be mock or ollama")
    if num_ctx < 1024 or num_ctx > 131072:
        raise ValueError("num_ctx must be between 1024 and 131072")
    if num_predict < 32 or num_predict > 4096:
        raise ValueError("num_predict must be between 32 and 4096")
    with _startup_lock(home):
        existing = service_status(home)
        if existing.get("live"):
            raise RuntimeError(f"Local company service is already running as PID {existing.get('pid')}")
        recorded = _read_state(home) or {}
        recorded_pid = int(recorded.get("pid", 0))
        if recorded.get("status") in {"starting", "running"} and _pid_exists(recorded_pid):
            raise RuntimeError(
                f"Local company service is already {recorded.get('status')} as PID {recorded_pid}"
            )

        token = secrets.token_urlsafe(32)
        log_path = home / "service.log"
        project_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(project_root / "src") + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        environment["LOCAL_COMPANY_SERVICE_TOKEN"] = token
        command = [
            sys.executable, "-m", "local_company.cli", "--home", str(home),
            "dashboard", "--port", str(port), "--provider", provider, "--model", model,
            "--num-ctx", str(num_ctx), "--num-predict", str(num_predict),
            "--keep-alive", keep_alive,
        ]
        creationflags = 0
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command, cwd=project_root, env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                creationflags=creationflags, **popen_kwargs,
            )
        state: dict[str, object] = {
            "status": "starting", "pid": process.pid, "port": port, "home": str(home),
            "token": token, "log_path": str(log_path), "started_at": _utc_now(),
            "provider": provider, "model": model, "num_ctx": num_ctx,
            "num_predict": num_predict, "keep_alive": keep_alive,
        }
        _write_state(home, state)
    for _ in range(30):
        health = _probe(port)
        if health and int(health.get("pid", -1)) == process.pid:
            state["status"] = "running"
            _write_state(home, state)
            return service_status(home)
        if process.poll() is not None:
            break
        time.sleep(0.5)
    state["status"] = "failed"
    state["failed_at"] = _utc_now()
    _write_state(home, state)
    raise RuntimeError(f"Dashboard service failed to become ready; inspect {log_path}")


def stop_service(home: Path) -> dict[str, object]:
    home = home.resolve()
    state = _read_state(home)
    if not state:
        raise ValueError("Local company service is not configured")
    port = int(state.get("port", 8765))
    health = _probe(port)
    if not health or int(health.get("pid", -1)) != int(state.get("pid", 0)):
        state["status"] = "stopped"
        state["stopped_at"] = _utc_now()
        _write_state(home, state)
        return service_status(home)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/__service/stop", data=b"", method="POST",
        headers={"X-Service-Token": str(state.get("token", ""))},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            if response.status != 202:
                raise RuntimeError(f"Service rejected shutdown with HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not stop local service: {exc}") from exc
    for _ in range(20):
        if _probe(port) is None:
            state["status"] = "stopped"
            state["stopped_at"] = _utc_now()
            _write_state(home, state)
            return service_status(home)
        time.sleep(0.25)
    raise RuntimeError("Local service did not stop within five seconds")
