from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXECUTION_FOCUS_SCHEMA = "local-company.execution-focus.v2"
LEGACY_EXECUTION_FOCUS_SCHEMA = "local-company.execution-focus.v1"
EXECUTION_FOCUS_FILENAME = "execution-focus.json"
EXECUTION_FOCUS_MAX_BYTES = 4096
EXECUTION_FOCUS_MAX_ROLES = 6
EXECUTION_RESOURCE_ENVELOPE_SCHEMA = "local-company.execution-resource-envelope.v1"
EXECUTION_RESOURCE_MAX_NUM_CTX = 4096
EXECUTION_RESOURCE_MAX_NUM_PREDICT = 768
EXECUTION_RESOURCE_KEEP_ALIVE = "0s"
EXECUTION_FOCUS_HANDOFF_CONFIRMATION = "HANDOFF ACTIVE EXECUTION FOCUS"
_PROJECT_ID = re.compile(r"^[0-9a-f]{12}$")
_FOCUS_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXACT_KEYS = {
    "schema", "revision", "enabled", "projectId", "projectName", "maxRoles",
    "updatedAt", "handoff", "controls",
}
_LEGACY_EXACT_KEYS = {
    "schema", "enabled", "projectId", "projectName", "maxRoles", "updatedAt", "controls",
}
_CONTROL_KEYS = {"modelBackedCommandsOnly", "externalWritesAllowed", "bypassAllowed"}
_HANDOFF_KEYS = {
    "fromProjectId", "fromProjectName", "reason", "priorFocusDigest", "authorizedAt",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _disabled_focus(
    updated_at: str | None = None, revision: int = 0,
    handoff: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": EXECUTION_FOCUS_SCHEMA,
        "revision": revision,
        "enabled": False,
        "projectId": None,
        "projectName": None,
        "maxRoles": 0,
        "updatedAt": updated_at,
        "handoff": handoff,
        "controls": {
            "modelBackedCommandsOnly": True,
            "externalWritesAllowed": False,
            "bypassAllowed": False,
        },
    }


def _canonical_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise RuntimeError("Execution focus timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("Execution focus timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise RuntimeError("Execution focus timestamp is invalid")
    return value


def validate_execution_focus(value: object) -> dict[str, object]:
    if type(value) is dict and value.get("schema") == LEGACY_EXECUTION_FOCUS_SCHEMA:
        if set(value) != _LEGACY_EXACT_KEYS:
            raise RuntimeError("Execution focus contract is invalid")
        value = {
            **value,
            "schema": EXECUTION_FOCUS_SCHEMA,
            "revision": 1 if value.get("enabled") else 0,
            "handoff": None,
        }
    if type(value) is not dict or set(value) != _EXACT_KEYS:
        raise RuntimeError("Execution focus contract is invalid")
    controls = value.get("controls")
    if type(controls) is not dict or set(controls) != _CONTROL_KEYS:
        raise RuntimeError("Execution focus controls are invalid")
    if controls != {
        "modelBackedCommandsOnly": True,
        "externalWritesAllowed": False,
        "bypassAllowed": False,
    }:
        raise RuntimeError("Execution focus controls are invalid")
    revision = value.get("revision")
    if (
        value.get("schema") != EXECUTION_FOCUS_SCHEMA
        or type(revision) is not int
        or revision < 0
        or type(value.get("enabled")) is not bool
    ):
        raise RuntimeError("Execution focus contract is invalid")
    handoff = value.get("handoff")
    if handoff is not None:
        if type(handoff) is not dict or set(handoff) != _HANDOFF_KEYS:
            raise RuntimeError("Execution focus handoff is invalid")
        if (
            type(handoff.get("fromProjectId")) is not str
            or not _PROJECT_ID.fullmatch(handoff["fromProjectId"])
            or type(handoff.get("fromProjectName")) is not str
            or not handoff["fromProjectName"].strip()
            or handoff["fromProjectName"] != handoff["fromProjectName"].strip()
            or len(handoff["fromProjectName"]) > 80
            or type(handoff.get("reason")) is not str
            or handoff["reason"] != handoff["reason"].strip()
            or not 8 <= len(handoff["reason"]) <= 240
            or any(ord(character) < 32 or ord(character) == 127 for character in handoff["reason"])
            or type(handoff.get("priorFocusDigest")) is not str
            or not _FOCUS_DIGEST.fullmatch(handoff["priorFocusDigest"])
        ):
            raise RuntimeError("Execution focus handoff is invalid")
        _canonical_timestamp(handoff.get("authorizedAt"))
    updated_at = _canonical_timestamp(value.get("updatedAt"))
    enabled = value["enabled"]
    project_id = value.get("projectId")
    project_name = value.get("projectName")
    max_roles = value.get("maxRoles")
    if enabled:
        if type(project_id) is not str or not _PROJECT_ID.fullmatch(project_id):
            raise RuntimeError("Execution focus project is invalid")
        if type(project_name) is not str or not project_name.strip() or project_name != project_name.strip() or len(project_name) > 80:
            raise RuntimeError("Execution focus project is invalid")
        if type(max_roles) is not int or not 1 <= max_roles <= EXECUTION_FOCUS_MAX_ROLES:
            raise RuntimeError("Execution focus role budget is invalid")
        if updated_at is None:
            raise RuntimeError("Execution focus timestamp is invalid")
    elif project_id is not None or project_name is not None or max_roles != 0:
        raise RuntimeError("Disabled execution focus is invalid")
    return {
        "schema": EXECUTION_FOCUS_SCHEMA,
        "revision": revision,
        "enabled": enabled,
        "projectId": project_id,
        "projectName": project_name,
        "maxRoles": max_roles,
        "updatedAt": updated_at,
        "handoff": dict(handoff) if handoff is not None else None,
        "controls": dict(controls),
    }


def execution_focus_path(company_home: Path) -> Path:
    return company_home.resolve() / EXECUTION_FOCUS_FILENAME


def execution_focus_digest(focus: dict[str, object]) -> str:
    validated = validate_execution_focus(focus)
    encoded = json.dumps(
        validated, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@contextmanager
def _focus_write_lock(company_home: Path):
    home = company_home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".execution-focus.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        opened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_ino != linked.st_ino
        ):
            raise RuntimeError("Execution focus lock is unsafe")
        handle = os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise
    try:
        if opened.st_size == 0:
            handle.write(b"0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Execution focus mutation is busy") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def read_execution_focus(company_home: Path) -> dict[str, object]:
    path = execution_focus_path(company_home)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _disabled_focus()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("Execution focus file is unsafe")
    if metadata.st_size < 2 or metadata.st_size > EXECUTION_FOCUS_MAX_BYTES:
        raise RuntimeError("Execution focus file size is invalid")
    try:
        raw = path.read_bytes()
        after = path.lstat()
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
            or len(raw) != metadata.st_size
        ):
            raise RuntimeError("Execution focus file changed during read")
        return validate_execution_focus(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Execution focus file is invalid") from exc


def _write_execution_focus_unlocked(company_home: Path, payload: dict[str, object]) -> dict[str, object]:
    home = company_home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    validated = validate_execution_focus(payload)
    target = execution_focus_path(home)
    temporary = home / f".{EXECUTION_FOCUS_FILENAME}.{uuid.uuid4().hex}.tmp"
    encoded = (json.dumps(validated, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    if len(encoded) > EXECUTION_FOCUS_MAX_BYTES:
        raise RuntimeError("Execution focus file size is invalid")
    try:
        with temporary.open("xb") as handle:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated


def set_execution_focus(
    company_home: Path, project_id: str, project_name: str, max_roles: int,
) -> dict[str, object]:
    home = company_home.resolve()
    with _focus_write_lock(home):
        current = read_execution_focus(home)
        if current["enabled"]:
            if (
                current["projectId"] == project_id
                and current["projectName"] == project_name
                and current["maxRoles"] == max_roles
            ):
                return current
            raise RuntimeError(
                "Active execution focus requires an explicit digest-bound handoff"
            )
        return _write_execution_focus_unlocked(home, {
            "schema": EXECUTION_FOCUS_SCHEMA,
            "revision": current["revision"] + 1,
            "enabled": True,
            "projectId": project_id,
            "projectName": project_name,
            "maxRoles": max_roles,
            "updatedAt": _timestamp(),
            "handoff": current["handoff"],
            "controls": {
                "modelBackedCommandsOnly": True,
                "externalWritesAllowed": False,
                "bypassAllowed": False,
            },
        })


def handoff_execution_focus(
    company_home: Path, from_project_id: str, to_project_id: str,
    to_project_name: str, max_roles: int, expected_focus_digest: str, reason: str,
) -> dict[str, object]:
    if not _FOCUS_DIGEST.fullmatch(expected_focus_digest):
        raise RuntimeError("Expected execution focus digest is invalid")
    if type(reason) is not str or reason != reason.strip() or not 8 <= len(reason) <= 240:
        raise RuntimeError("Execution focus handoff reason is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in reason):
        raise RuntimeError("Execution focus handoff reason is invalid")
    home = company_home.resolve()
    with _focus_write_lock(home):
        current = read_execution_focus(home)
        current_digest = execution_focus_digest(current)
        if current_digest != expected_focus_digest:
            raise RuntimeError("Execution focus changed; refresh before handoff")
        if not current["enabled"] or current["projectId"] != from_project_id:
            raise RuntimeError("Execution focus handoff source is not active")
        if to_project_id == from_project_id:
            raise RuntimeError("Execution focus handoff target must be different")
        authorized_at = _timestamp()
        next_focus = _write_execution_focus_unlocked(home, {
            "schema": EXECUTION_FOCUS_SCHEMA,
            "revision": current["revision"] + 1,
            "enabled": True,
            "projectId": to_project_id,
            "projectName": to_project_name,
            "maxRoles": max_roles,
            "updatedAt": authorized_at,
            "handoff": {
                "fromProjectId": current["projectId"],
                "fromProjectName": current["projectName"],
                "reason": reason,
                "priorFocusDigest": current_digest,
                "authorizedAt": authorized_at,
            },
            "controls": dict(current["controls"]),
        })
        return next_focus


def clear_execution_focus(
    company_home: Path, expected_focus_digest: str, reason: str,
) -> dict[str, object]:
    if not _FOCUS_DIGEST.fullmatch(expected_focus_digest):
        raise RuntimeError("Expected execution focus digest is invalid")
    if type(reason) is not str or reason != reason.strip() or not 8 <= len(reason) <= 240:
        raise RuntimeError("Execution focus clear reason is invalid")
    home = company_home.resolve()
    with _focus_write_lock(home):
        current = read_execution_focus(home)
        current_digest = execution_focus_digest(current)
        if current_digest != expected_focus_digest:
            raise RuntimeError("Execution focus changed; refresh before clearing")
        if not current["enabled"]:
            return current
        authorized_at = _timestamp()
        handoff = {
            "fromProjectId": current["projectId"],
            "fromProjectName": current["projectName"],
            "reason": reason,
            "priorFocusDigest": current_digest,
            "authorizedAt": authorized_at,
        }
        return _write_execution_focus_unlocked(
            home,
            _disabled_focus(authorized_at, current["revision"] + 1, handoff),
        )


def enforce_execution_focus(
    focus: dict[str, object], project_id: str | None, roles: Iterable[str], action: str,
) -> None:
    validated = validate_execution_focus(focus)
    if not validated["enabled"]:
        return
    if project_id is None:
        raise RuntimeError(
            f"Execution focus requires a project for {action}; no model was called"
        )
    if project_id != validated["projectId"]:
        raise RuntimeError(
            f"Execution focus allows project {validated['projectName']} only; "
            "the requested project was denied before model load"
        )
    role_count = len(list(roles))
    if role_count < 1:
        raise RuntimeError(
            f"Execution focus requires an explicit bounded team for {action}; no model was called"
        )
    if role_count > validated["maxRoles"]:
        raise RuntimeError(
            f"Execution focus allows at most {validated['maxRoles']} roles; "
            f"the requested {role_count} were denied before model load"
        )


def enforce_execution_resource_envelope(
    focus: dict[str, object], provider: str, num_ctx: int,
    num_predict: int, keep_alive: str, action: str,
) -> dict[str, object]:
    validated = validate_execution_focus(focus)
    if provider == "mock":
        return {
            "schema": EXECUTION_RESOURCE_ENVELOPE_SCHEMA,
            "provider": "mock",
            "focusRequired": False,
            "modelLoadAllowed": False,
        }
    if provider != "ollama":
        raise RuntimeError(f"Execution provider is not allowed for {action}; no model was loaded")
    if not validated["enabled"]:
        raise RuntimeError(
            f"Execution focus must be enabled for Ollama {action}; no model was loaded"
        )
    if type(num_ctx) is not int or not 1024 <= num_ctx <= EXECUTION_RESOURCE_MAX_NUM_CTX:
        raise RuntimeError(
            f"Execution context exceeds {EXECUTION_RESOURCE_MAX_NUM_CTX} tokens; "
            "no model was loaded"
        )
    if type(num_predict) is not int or not 32 <= num_predict <= EXECUTION_RESOURCE_MAX_NUM_PREDICT:
        raise RuntimeError(
            f"Execution output exceeds {EXECUTION_RESOURCE_MAX_NUM_PREDICT} tokens; "
            "no model was loaded"
        )
    if keep_alive != EXECUTION_RESOURCE_KEEP_ALIVE:
        raise RuntimeError(
            f"Execution keep-alive must be {EXECUTION_RESOURCE_KEEP_ALIVE}; no model was loaded"
        )
    return {
        "schema": EXECUTION_RESOURCE_ENVELOPE_SCHEMA,
        "provider": "ollama",
        "focusRequired": True,
        "maxRoles": EXECUTION_FOCUS_MAX_ROLES,
        "maxNumCtx": EXECUTION_RESOURCE_MAX_NUM_CTX,
        "maxNumPredict": EXECUTION_RESOURCE_MAX_NUM_PREDICT,
        "keepAlive": EXECUTION_RESOURCE_KEEP_ALIVE,
        "modelLoadAllowed": True,
    }
