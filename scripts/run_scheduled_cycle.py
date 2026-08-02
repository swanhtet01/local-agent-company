from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


SCHEMA = "local-ai.scheduled-cycle-result.v1"
CYCLE_SCHEMA = "local-ai.cycle-result.v1"
TRIM_SCHEMA = "supermega.ally-working-set-trim.v1"
MAX_CAPTURE_CHARS = 262_144
MAX_JOURNAL_BYTES = 65_536
ALLOWED_CYCLE_FIELDS = (
    "status", "reason", "schedulesMaterialized", "queueId", "missionsRun",
    "modelCalled", "qualityPassed", "qualityScore", "modelUnloadedAfterRun",
    "recommendedAction", "availableMemoryBytes", "minimumAvailableBytes",
    "memoryShortfallBytes", "ownerGateCategories", "blockers",
)


def _cycle_receipt(stdout: str, stderr: str) -> dict[str, object] | None:
    if len(stdout) > MAX_CAPTURE_CHARS or len(stderr) > MAX_CAPTURE_CHARS:
        return None
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        if not line.startswith("{") or len(line) > MAX_JOURNAL_BYTES:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema") == CYCLE_SCHEMA:
            return {key: value[key] for key in ALLOWED_CYCLE_FIELDS if key in value}
    return None


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_JOURNAL_BYTES:
        raise RuntimeError("scheduled_cycle_journal_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _invoke_cycle(root: Path, launcher: Path) -> tuple[int, dict[str, object] | None]:
    completed = subprocess.run(
        [sys.executable, str(launcher), "cycle"],
        cwd=root, check=False, capture_output=True, text=True, timeout=7200,
    )
    return completed.returncode, _cycle_receipt(completed.stdout, completed.stderr)


def _recover_memory(root: Path) -> dict[str, object] | None:
    optimizer = root.parent / "supermega-platform" / "tools" / "trim_codex_working_sets.ps1"
    if not optimizer.is_file():
        return None
    try:
        completed = subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(optimizer), "-Apply", "-Json",
            ],
            cwd=optimizer.parent.parent, check=False, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0 or len(completed.stdout) > MAX_CAPTURE_CHARS:
        return None
    try:
        receipt = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    controls = receipt.get("controls") if isinstance(receipt, dict) else None
    counts = (
        receipt.get("targetCount"), receipt.get("trimSucceeded"), receipt.get("trimFailed"),
    ) if isinstance(receipt, dict) else (None, None, None)
    released = receipt.get("releasedWorkingSetMb") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("contract") != TRIM_SCHEMA
        or receipt.get("ok") is not True
        or receipt.get("mode") != "apply"
        or not isinstance(controls, dict)
        or controls.get("processMutation") is not True
        or controls.get("processTerminationCalls") != 0
        or controls.get("filesModified") != 0
        or controls.get("networkRequests") != 0
        or any(type(value) is not int or value < 0 for value in counts)
        or not isinstance(released, (int, float)) or isinstance(released, bool) or released < 0
    ):
        return None
    return {
        "attempted": True, "status": "completed", "targetCount": counts[0],
        "trimSucceeded": counts[1], "trimFailed": counts[2],
        "releasedWorkingSetMb": released, "processTerminationCalls": 0,
    }


def run_scheduled_cycle(
    project_root: Path,
    state_root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    settle: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, object]]:
    root = project_root.resolve(strict=True)
    launcher = (root / "scripts" / "local_ai.py").resolve(strict=True)
    journal_path = state_root.resolve() / "autopilot-cycle-result.json"
    try:
        process_exit_code, cycle = _invoke_cycle(root, launcher)
        if cycle is None:
            journal = {
                "schema": SCHEMA, "status": "error",
                "reason": "cycle_receipt_missing_or_oversized",
                "processExitCode": process_exit_code,
                "observedAt": now().astimezone(timezone.utc).isoformat(),
                "controls": {"rawOutputStored": False, "externalActionPerformed": False},
            }
            exit_code = process_exit_code or 2
        else:
            recovery = None
            if (
                process_exit_code == 0
                and cycle.get("status") == "blocked"
                and cycle.get("reason") == "insufficient_available_memory"
            ):
                recovery = _recover_memory(root)
                if recovery is None:
                    journal = {
                        "schema": SCHEMA, "status": "error",
                        "reason": "memory_recovery_invalid", "processExitCode": 2,
                        "observedAt": now().astimezone(timezone.utc).isoformat(),
                        "cycle": cycle,
                        "controls": {"rawOutputStored": False, "externalActionPerformed": False},
                    }
                    _atomic_write(journal_path, journal)
                    print(json.dumps(journal, separators=(",", ":"), sort_keys=True))
                    return 2, journal
                settle(3.0)
                process_exit_code, retried = _invoke_cycle(root, launcher)
                if retried is None:
                    journal = {
                        "schema": SCHEMA, "status": "error",
                        "reason": "cycle_retry_receipt_missing_or_oversized",
                        "processExitCode": process_exit_code or 2,
                        "observedAt": now().astimezone(timezone.utc).isoformat(),
                        "memoryRecovery": recovery,
                        "controls": {"rawOutputStored": False, "externalActionPerformed": False},
                    }
                    _atomic_write(journal_path, journal)
                    print(json.dumps(journal, separators=(",", ":"), sort_keys=True))
                    return process_exit_code or 2, journal
                cycle = retried
            journal = {
                "schema": SCHEMA, "status": "recorded",
                "processExitCode": process_exit_code,
                "observedAt": now().astimezone(timezone.utc).isoformat(),
                "cycle": cycle,
                "controls": {"rawOutputStored": False, "externalActionPerformed": False},
            }
            if recovery is not None:
                journal["memoryRecovery"] = recovery
            exit_code = process_exit_code
    except subprocess.TimeoutExpired:
        journal = {
            "schema": SCHEMA, "status": "error", "reason": "cycle_timeout",
            "processExitCode": 124,
            "observedAt": now().astimezone(timezone.utc).isoformat(),
            "controls": {"rawOutputStored": False, "externalActionPerformed": False},
        }
        exit_code = 124
    _atomic_write(journal_path, journal)
    print(json.dumps(journal, separators=(",", ":"), sort_keys=True))
    return exit_code, journal


def main() -> int:
    if len(sys.argv) != 1:
        print(json.dumps({"schema": SCHEMA, "status": "error", "reason": "arguments_not_allowed"}, separators=(",", ":"), sort_keys=True))
        return 2
    root = Path(__file__).resolve(strict=True).parents[1]
    state = root.parent / "supermega-local-company-state"
    return run_scheduled_cycle(root, state)[0]


if __name__ == "__main__":
    raise SystemExit(main())
