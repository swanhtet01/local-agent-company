from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


SCHEMA = "local-ai.scheduled-cycle-result.v1"
CYCLE_SCHEMA = "local-ai.cycle-result.v1"
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


def run_scheduled_cycle(
    project_root: Path,
    state_root: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[int, dict[str, object]]:
    root = project_root.resolve(strict=True)
    launcher = (root / "scripts" / "local_ai.py").resolve(strict=True)
    journal_path = state_root.resolve() / "autopilot-cycle-result.json"
    try:
        completed = subprocess.run(
            [sys.executable, str(launcher), "cycle"],
            cwd=root, check=False, capture_output=True, text=True, timeout=7200,
        )
        cycle = _cycle_receipt(completed.stdout, completed.stderr)
        if cycle is None:
            journal = {
                "schema": SCHEMA, "status": "error",
                "reason": "cycle_receipt_missing_or_oversized",
                "processExitCode": completed.returncode,
                "observedAt": now().astimezone(timezone.utc).isoformat(),
                "controls": {"rawOutputStored": False, "externalActionPerformed": False},
            }
            exit_code = completed.returncode or 2
        else:
            journal = {
                "schema": SCHEMA, "status": "recorded",
                "processExitCode": completed.returncode,
                "observedAt": now().astimezone(timezone.utc).isoformat(),
                "cycle": cycle,
                "controls": {"rawOutputStored": False, "externalActionPerformed": False},
            }
            exit_code = completed.returncode
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
