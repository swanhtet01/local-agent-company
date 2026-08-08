"""Compose the sealed local runtime supervisor evidence without changing state."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_company.model_policy import (  # noqa: E402
    DEFAULT_LOCAL_MODEL, is_supported_local_model, require_local_llama_model,
)

if __package__:
    from .check_readiness import (  # noqa: E402
        READINESS_SCHEMA, CompanyIdentityError, _valid_model_name,
        read_company_identity, run_readiness,
    )
    from .runtime_guard import (  # noqa: E402
        OLLAMA_SHA256_PATTERN, READINESS_ACTION_MAP, RESULT_JOURNAL_NAME,
        RUNTIME_KEEP_ALIVE, RUNTIME_NUM_CTX, RUNTIME_NUM_PREDICT,
        GuardExecutableError, GuardExecutableHashMismatch,
        GuardResultJournalError, _normalized_company_home, _record_metadata,
        _render_result as _render_guard_result, _same_record,
        _valid_store_identity, _validated_ollama_executable,
        _verified_executable_sha256,
    )
    from .stamp_build_manifest import ManifestError, check_project  # noqa: E402
else:
    from check_readiness import (  # noqa: E402
        READINESS_SCHEMA, CompanyIdentityError, _valid_model_name,
        read_company_identity, run_readiness,
    )
    from runtime_guard import (  # noqa: E402
        OLLAMA_SHA256_PATTERN, READINESS_ACTION_MAP, RESULT_JOURNAL_NAME,
        RUNTIME_KEEP_ALIVE, RUNTIME_NUM_CTX, RUNTIME_NUM_PREDICT,
        GuardExecutableError, GuardExecutableHashMismatch,
        GuardResultJournalError, _normalized_company_home, _record_metadata,
        _render_result as _render_guard_result, _same_record,
        _valid_store_identity, _validated_ollama_executable,
        _verified_executable_sha256,
    )
    from stamp_build_manifest import ManifestError, check_project  # noqa: E402


SUPERVISOR_SCHEMA = "local-company.runtime-supervisor.v1"
TASK_SNAPSHOT_SCHEMA = "local-company.runtime-task-snapshot.v1"
EXPECTED_TASK_NAME = "SuperMega Local Runtime Guard"
EXPECTED_TASK_PATH = "\\"
PORT = 8765
NUM_CTX = RUNTIME_NUM_CTX
NUM_PREDICT = RUNTIME_NUM_PREDICT
KEEP_ALIVE = RUNTIME_KEEP_ALIVE
WAIT_SECONDS = 15
SCHEDULE_INTERVAL_SECONDS = 300
DISPATCH_GRACE_SECONDS = 120
EXECUTION_LIMIT_SECONDS = 180
CLOCK_SKEW_SECONDS = 2
MAX_READY_AGE_SECONDS = SCHEDULE_INTERVAL_SECONDS + DISPATCH_GRACE_SECONDS
MAX_CORRELATION_SECONDS = EXECUTION_LIMIT_SECONDS + 30
MAX_RESULT_JOURNAL_BYTES = 2048
MAX_TASK_ARGUMENT_BYTES = 4096
MAX_TASK_PATH_BYTES = 1024
MAX_TASK_SNAPSHOT_BYTES = 32 * 1024
MAX_RENDERED_RESULT_BYTES = 4096
TASK_QUERY_TIMEOUT_SECONDS = 10

_TOKEN_PATTERN = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_TASK_STATES = {"ready", "running", "queued", "disabled", "unknown"}
_FINAL_ACTIONS = {
    "none", "fix_command_arguments", "inspect_runtime_supervisor",
    "inspect_build_manifest", "inspect_company_store",
    "install_runtime_guard_task", "enable_runtime_guard_task",
    "wait_for_runtime_guard_task", "run_runtime_guard_task",
    "inspect_runtime_guard_task", "inspect_runtime_guard",
    "inspect_ollama_service", "retry_runtime_supervisor",
    *READINESS_ACTION_MAP.values(),
}


class SupervisorUsageError(ValueError):
    pass


class TaskSnapshotError(RuntimeError):
    pass


class JournalSnapshotError(RuntimeError):
    pass


class JournalMissingError(JournalSnapshotError):
    pass


class SupervisorSnapshotChanged(RuntimeError):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _: str) -> None:
        raise SupervisorUsageError("invalid arguments")


@dataclass(frozen=True)
class ExpectedTask:
    task_name: str
    python_executable: Path
    runtime_guard: Path
    company_home: Path
    ollama_executable: Path
    ollama_sha256: str
    model: str
    allow_windows_job_inheritance: bool

    @property
    def arguments(self) -> tuple[str, ...]:
        values = [
            str(self.runtime_guard), "--home", str(self.company_home),
            "--port", str(PORT), "--model", self.model,
            "--num-ctx", str(NUM_CTX), "--num-predict", str(NUM_PREDICT),
            "--keep-alive", KEEP_ALIVE, "--wait-seconds", str(WAIT_SECONDS),
            "--ollama-executable", str(self.ollama_executable),
            "--ollama-sha256", self.ollama_sha256,
        ]
        if self.allow_windows_job_inheritance:
            values.append("--allow-windows-job-inheritance")
        values.append("--record-result")
        return tuple(values)


@dataclass(frozen=True)
class TaskSnapshot:
    found: bool
    configuration: str
    enabled: bool
    state: str
    last_result: int | None
    last_run_utc: str | None
    next_run_utc: str | None


@dataclass(frozen=True)
class JournalSnapshot:
    payload: dict[str, object]
    signature: tuple[int, int, int, int, int, int]
    mtime_ns: int


POWERSHELL_TASK_SNAPSHOT = r'''trap { exit 1 }
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$module = $env:LOCAL_COMPANY_TASK_MODULE
Import-Module -Name $module -Force -ErrorAction Stop

if (-not ('LocalCompany.NativeArgv' -as [type])) {
    $source = @"
using System;
using System.Runtime.InteropServices;
namespace LocalCompany {
  public static class NativeArgv {
    [DllImport("shell32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    private static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr value);
    public static string[] Split(string commandLine) {
      int count;
      IntPtr block = CommandLineToArgvW(commandLine ?? "", out count);
      if (block == IntPtr.Zero || count < 0 || count > 64) throw new InvalidOperationException();
      try {
        string[] result = new string[count];
        for (int index = 0; index < count; index++) {
          IntPtr value = Marshal.ReadIntPtr(block, index * IntPtr.Size);
          result[index] = Marshal.PtrToStringUni(value);
        }
        return result;
      } finally { LocalFree(block); }
    }
  }
}
"@
    Add-Type -TypeDefinition $source -ErrorAction Stop
}

function Resolve-Sid([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -gt 1024) { return $null }
    try { return ([System.Security.Principal.SecurityIdentifier]$value).Value } catch {}
    try {
        $account = New-Object System.Security.Principal.NTAccount($value)
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}
function Same-Text([string]$left, [string]$right) {
    return [string]::Equals($left, $right, [System.StringComparison]::Ordinal)
}
function Same-Path([string]$left, [string]$right) {
    return [string]::Equals($left, $right, [System.StringComparison]::OrdinalIgnoreCase)
}

$taskName = $env:LOCAL_COMPANY_TASK_NAME
$expectedExecute = $env:LOCAL_COMPANY_TASK_EXECUTE
$expectedWorkdir = $env:LOCAL_COMPANY_TASK_WORKDIR
$expectedJson = $env:LOCAL_COMPANY_TASK_ARGUMENTS
if ($taskName -cne 'SuperMega Local Runtime Guard' -or
    [string]::IsNullOrEmpty($expectedExecute) -or $expectedExecute.Length -gt 1024 -or
    [string]::IsNullOrEmpty($expectedWorkdir) -or $expectedWorkdir.Length -gt 1024 -or
    [string]::IsNullOrEmpty($expectedJson) -or $expectedJson.Length -gt 4096) { throw 'invalid baseline' }
$expected = ConvertFrom-Json -InputObject $expectedJson -ErrorAction Stop
if ($expected.Count -lt 20 -or $expected.Count -gt 22) { throw 'invalid baseline' }

$matches = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object {
    $_.TaskName -ceq $taskName -and $_.TaskPath -ceq '\'
})
if ($matches.Count -eq 0) {
    [ordered]@{schema='local-company.runtime-task-snapshot.v1';found=$false;configuration='missing';enabled=$false;state='unknown';last_result=$null;last_run_utc=$null;next_run_utc=$null} | ConvertTo-Json -Compress
    exit 0
}
if ($matches.Count -ne 1) { throw 'ambiguous task' }
$task = $matches[0]
$info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\' -ErrorAction Stop
$configuration = $true

$actions = @($task.Actions)
if ($actions.Count -ne 1) { $configuration = $false }
if ($configuration) {
    $action = $actions[0]
    $rawArguments = [string]$action.Arguments
    if (([string]$action.Execute).Length -gt 1024 -or
        ([string]$action.WorkingDirectory).Length -gt 1024 -or
        $rawArguments.Length -gt 4096 -or
        -not (Same-Path ([string]$action.Execute) $expectedExecute) -or
        -not (Same-Path ([string]$action.WorkingDirectory) $expectedWorkdir)) {
        $configuration = $false
    } else {
        try { $actual = @([LocalCompany.NativeArgv]::Split($rawArguments)) } catch { $configuration = $false; $actual = @() }
        if ($actual.Count -ne $expected.Count) { $configuration = $false }
        if ($configuration) {
            for ($index = 0; $index -lt $expected.Count; $index++) {
                $pathToken = $index -in @(0, 2, 16)
                $same = if ($pathToken) { Same-Path ([string]$actual[$index]) ([string]$expected[$index]) } else { Same-Text ([string]$actual[$index]) ([string]$expected[$index]) }
                if (-not $same) { $configuration = $false; break }
            }
        }
    }
}

$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$principalSid = Resolve-Sid ([string]$task.Principal.UserId)
$logonType = [string]$task.Principal.LogonType
$runLevel = [string]$task.Principal.RunLevel
if ($principalSid -cne $currentSid -or $logonType -notin @('Interactive','InteractiveToken','3') -or $runLevel -notin @('Limited','0')) { $configuration = $false }

$settings = $task.Settings
if ([string]$settings.MultipleInstances -ne 'IgnoreNew' -or
    [string]$settings.ExecutionTimeLimit -cne 'PT3M' -or
    $settings.StartWhenAvailable -ne $true -or
    $settings.AllowHardTerminate -ne $false -or
    $settings.DisallowStartIfOnBatteries -ne $false -or
    $settings.StopIfGoingOnBatteries -ne $false) { $configuration = $false }

$triggers = @($task.Triggers)
if ($triggers.Count -ne 2) { $configuration = $false }
$logons = @($triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
$times = @($triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })
if ($logons.Count -ne 1 -or $times.Count -ne 1) { $configuration = $false }
if ($logons.Count -eq 1) {
    $triggerSid = Resolve-Sid ([string]$logons[0].UserId)
    if ($triggerSid -cne $currentSid -or $logons[0].Enabled -eq $false -or [string]$logons[0].Delay -cne 'PT30S') { $configuration = $false }
}
if ($times.Count -eq 1) {
    $timeTrigger = $times[0]
    try {
        $start = [DateTimeOffset]::Parse([string]$timeTrigger.StartBoundary).ToUniversalTime()
        $duration = [System.Xml.XmlConvert]::ToTimeSpan([string]$timeTrigger.Repetition.Duration)
        $longEnough = ($start + $duration) -gt ([DateTimeOffset]::UtcNow.AddDays(30))
        $begun = $start -le ([DateTimeOffset]::UtcNow.AddMinutes(5))
    } catch { $longEnough = $false; $begun = $false }
    if ($timeTrigger.Enabled -eq $false -or [string]$timeTrigger.Repetition.Interval -cne 'PT5M' -or
        $timeTrigger.Repetition.StopAtDurationEnd -ne $true -or -not $longEnough -or -not $begun) { $configuration = $false }
}

$lastRun = if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1900) { $info.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
$nextRun = if ($info.NextRunTime -and $info.NextRunTime.Year -gt 1900) { $info.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
$state = ([string]$task.State).ToLowerInvariant()
if ($state -notin @('ready','running','queued','disabled')) { $state = 'unknown' }
[ordered]@{
    schema='local-company.runtime-task-snapshot.v1'
    found=$true
    configuration=$(if ($configuration) {'match'} else {'mismatch'})
    enabled=[bool]$settings.Enabled
    state=$state
    last_result=[long]$info.LastTaskResult
    last_run_utc=$lastRun
    next_run_utc=$nextRun
} | ConvertTo-Json -Compress
exit 0
'''


def _empty_checks() -> dict[str, dict[str, object]]:
    return {
        "scheduled_task": {
            "configuration": "unknown", "state": "unknown",
            "last_result": "unknown", "freshness": "unknown",
        },
        "guard_journal": {
            "schema": "unknown", "status": "unknown",
            "freshness": "unknown", "correlation": "unknown",
        },
        "ollama_executable": {"pin": "unknown"},
        "readiness": {"status": "unknown", "action": "none"},
    }


def _payload(
    *, code: int, model: str | None, checks: dict[str, dict[str, object]] | None = None,
    blockers: list[str], action: str, task_age: int | None = None,
    journal_age: int | None = None,
) -> dict[str, object]:
    status = "ready" if code == 0 else "action_required" if code == 1 else "indeterminate"
    return {
        "schema": SUPERVISOR_SCHEMA,
        "status": status,
        "ready": code == 0,
        "required_model": model,
        "checks": _empty_checks() if checks is None else checks,
        "ages_seconds": {"task": task_age, "journal": journal_age},
        "blockers": list(dict.fromkeys(blockers)),
        "action": action,
        "generation_tested": False,
        "missions_started": 0,
        "models_pulled": 0,
        "mutations_performed": 0,
    }


def render_result(result: object) -> bytes:
    keys = {
        "schema", "status", "ready", "required_model", "checks", "ages_seconds",
        "blockers", "action", "generation_tested", "missions_started",
        "models_pulled", "mutations_performed",
    }
    if not isinstance(result, dict) or set(result) != keys:
        raise SupervisorUsageError("invalid result")
    checks = result.get("checks")
    ages = result.get("ages_seconds")
    blockers = result.get("blockers")
    action = result.get("action")
    model = result.get("required_model")
    if (
        result.get("schema") != SUPERVISOR_SCHEMA
        or result.get("status") not in {"ready", "action_required", "indeterminate"}
        or type(result.get("ready")) is not bool
        or result["ready"] is not (result["status"] == "ready")
        or not (
            model is None or (_valid_model_name(model) and is_supported_local_model(model))
        )
        or not isinstance(checks, dict)
        or set(checks) != {"scheduled_task", "guard_journal", "ollama_executable", "readiness"}
        or not isinstance(ages, dict) or set(ages) != {"task", "journal"}
        or any(value is not None and (type(value) is not int or value < 0 or value > 86400) for value in ages.values())
        or not isinstance(blockers, list) or len(blockers) > 12
        or any(type(item) is not str or _TOKEN_PATTERN.fullmatch(item) is None for item in blockers)
        or action not in _FINAL_ACTIONS
        or result.get("generation_tested") is not False
        or result.get("missions_started") != 0
        or result.get("models_pulled") != 0
        or result.get("mutations_performed") != 0
    ):
        raise SupervisorUsageError("invalid result")
    task = checks.get("scheduled_task")
    journal = checks.get("guard_journal")
    executable = checks.get("ollama_executable")
    readiness = checks.get("readiness")
    if (
        not isinstance(task, dict) or set(task) != {"configuration", "state", "last_result", "freshness"}
        or task.get("configuration") not in {"unknown", "missing", "match", "mismatch"}
        or task.get("state") not in _TASK_STATES
        or task.get("last_result") not in {"unknown", "success", "failure"}
        or task.get("freshness") not in {"unknown", "fresh", "stale", "future"}
        or not isinstance(journal, dict) or set(journal) != {"schema", "status", "freshness", "correlation"}
        or journal.get("schema") not in {"unknown", "missing", "valid", "invalid"}
        or journal.get("status") not in {"unknown", "ready", "action_required", "indeterminate"}
        or journal.get("freshness") not in {"unknown", "fresh", "stale", "future"}
        or journal.get("correlation") not in {"unknown", "match", "mismatch"}
        or not isinstance(executable, dict) or set(executable) != {"pin"}
        or executable.get("pin") not in {"unknown", "match", "mismatch", "invalid"}
        or not isinstance(readiness, dict) or set(readiness) != {"status", "action"}
        or readiness.get("status") not in {"unknown", "ready", "action_required", "indeterminate"}
        or readiness.get("action") not in _FINAL_ACTIONS
    ):
        raise SupervisorUsageError("invalid result")
    rendered = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(rendered) > MAX_RENDERED_RESULT_BYTES:
        raise SupervisorUsageError("result too large")
    return rendered


def _write_stdout(rendered: bytes) -> None:
    binary = getattr(sys.stdout, "buffer", None)
    if binary is not None:
        binary.write(rendered)
        binary.flush()
    else:
        sys.stdout.write(rendered.decode("utf-8"))
        sys.stdout.flush()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("invalid constant")


def _validated_local_file(path: Path) -> Path:
    return _validated_ollama_executable(path)


def _windows_system_directory() -> Path:
    if os.name != "nt":
        raise SupervisorUsageError("unsupported platform")
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
    function.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    function.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise TaskSnapshotError("system directory unavailable")
    return Path(buffer.value)


def _powershell_files() -> tuple[Path, Path]:
    system = _windows_system_directory()
    powershell = _validated_local_file(system / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    module = _validated_local_file(
        system / "WindowsPowerShell" / "v1.0" / "Modules" / "ScheduledTasks" / "ScheduledTasks.psd1"
    )
    return powershell, module


def _parse_task_snapshot(raw: bytes) -> TaskSnapshot:
    if not raw or len(raw) > MAX_TASK_SNAPSHOT_BYTES:
        raise TaskSnapshotError("task output invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise TaskSnapshotError("task output invalid") from exc
    expected = {
        "schema", "found", "configuration", "enabled", "state",
        "last_result", "last_run_utc", "next_run_utc",
    }
    if (
        not isinstance(value, dict) or set(value) != expected
        or value.get("schema") != TASK_SNAPSHOT_SCHEMA
        or type(value.get("found")) is not bool
        or value.get("configuration") not in {"missing", "match", "mismatch"}
        or type(value.get("enabled")) is not bool
        or value.get("state") not in _TASK_STATES
        or not (value.get("last_result") is None or type(value.get("last_result")) is int)
        or not (value.get("last_run_utc") is None or type(value.get("last_run_utc")) is str)
        or not (value.get("next_run_utc") is None or type(value.get("next_run_utc")) is str)
        or (not value["found"] and value["configuration"] != "missing")
    ):
        raise TaskSnapshotError("task output invalid")
    return TaskSnapshot(
        found=value["found"], configuration=value["configuration"],
        enabled=value["enabled"], state=value["state"],
        last_result=value["last_result"], last_run_utc=value["last_run_utc"],
        next_run_utc=value["next_run_utc"],
    )


def acquire_task_snapshot(expected: ExpectedTask) -> TaskSnapshot:
    powershell, module = _powershell_files()
    arguments_json = json.dumps(expected.arguments, ensure_ascii=True, separators=(",", ":"))
    if (
        len(arguments_json.encode("utf-8")) > MAX_TASK_ARGUMENT_BYTES
        or any(len(str(path).encode("utf-8")) > MAX_TASK_PATH_BYTES for path in (
            expected.python_executable, PROJECT_ROOT,
        ))
    ):
        raise SupervisorUsageError("task baseline too large")
    environment = os.environ.copy()
    environment.update({
        "LOCAL_COMPANY_TASK_MODULE": str(module),
        "LOCAL_COMPANY_TASK_NAME": expected.task_name,
        "LOCAL_COMPANY_TASK_EXECUTE": str(expected.python_executable),
        "LOCAL_COMPANY_TASK_WORKDIR": str(PROJECT_ROOT),
        "LOCAL_COMPANY_TASK_ARGUMENTS": arguments_json,
    })
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", POWERSHELL_TASK_SNAPSHOT,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT, env=environment, timeout=TASK_QUERY_TIMEOUT_SECONDS,
            check=False, shell=False, close_fds=True, creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise TaskSnapshotError("task query failed") from exc
    if completed.returncode != 0:
        raise TaskSnapshotError("task query failed")
    return _parse_task_snapshot(completed.stdout)


def read_result_journal(home: Path) -> JournalSnapshot:
    target = home / RESULT_JOURNAL_NAME
    try:
        before = _record_metadata(target)
        if before is None:
            raise JournalMissingError("result journal missing")
        if before.st_size < 1 or before.st_size > MAX_RESULT_JOURNAL_BYTES:
            raise JournalSnapshotError("result journal size invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            content = handle.read(MAX_RESULT_JOURNAL_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = _record_metadata(target)
        if (
            current is None or not _same_record(before, opened)
            or not _same_record(opened, after) or not _same_record(after, current)
            or len(content) != opened.st_size
        ):
            raise JournalSnapshotError("result journal changed")
        value = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        if _render_guard_result(value) != content:
            raise JournalSnapshotError("result journal is not canonical")
        signature = (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink,
            opened.st_size, opened.st_mtime_ns,
        )
        return JournalSnapshot(value, signature, opened.st_mtime_ns)
    except JournalSnapshotError:
        raise
    except (GuardResultJournalError, OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise JournalSnapshotError("result journal invalid") from exc


def _parse_utc(value: str | None) -> int | None:
    if type(value) is not str or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.astimezone(timezone.utc).timestamp()
        if timestamp < 0:
            return None
        return int(timestamp * 1_000_000_000)
    except (OverflowError, TypeError, ValueError):
        return None


def _age(now_ns: int, timestamp_ns: int | None) -> tuple[str, int | None]:
    if timestamp_ns is None:
        return "unknown", None
    delta = (now_ns - timestamp_ns) / 1_000_000_000
    if delta < -CLOCK_SKEW_SECONDS:
        return "future", None
    age = max(0, int(delta))
    return ("fresh" if delta <= MAX_READY_AGE_SECONDS else "stale"), min(age, 86400)


def _journal_status(payload: dict[str, object], model: str) -> tuple[str, int, str]:
    if payload.get("required_model") != model:
        return "indeterminate", 2, "inspect_runtime_guard"
    status = payload.get("status")
    blockers = payload.get("blockers")
    action = payload.get("action")
    if (
        status in {"ready", "recovered"} and payload.get("ready") is True
        and blockers == [] and action == "none"
    ):
        return "ready", 0, "none"
    if (
        status == "action_required" and payload.get("ready") is False
        and isinstance(blockers, list) and blockers and type(action) is str
    ):
        return "action_required", 1, action if action in _FINAL_ACTIONS else "inspect_runtime_guard"
    return "indeterminate", 2, "inspect_runtime_guard"


def _readiness_status(model: str, home: Path) -> tuple[str, int, str]:
    try:
        result, code = run_readiness(model, home)
    except Exception:
        return "indeterminate", 2, "inspect_runtime_supervisor"
    expected = {
        "schema", "status", "ready", "required_model", "components",
        "generation_tested", "blockers", "action",
    }
    if (
        not isinstance(result, dict) or set(result) != expected
        or result.get("schema") != READINESS_SCHEMA
        or result.get("required_model") != model
        or result.get("generation_tested") is not False
        or not isinstance(result.get("components"), dict)
        or not isinstance(result.get("blockers"), list)
        or any(type(item) is not str for item in result["blockers"])
        or type(result.get("action")) is not str
    ):
        return "indeterminate", 2, "inspect_runtime_supervisor"
    if code == 0 and result.get("status") == "ready" and result.get("ready") is True and result["blockers"] == [] and result["action"] == "none":
        return "ready", 0, "none"
    if code == 1 and result.get("status") == "action_required" and result.get("ready") is False and result["blockers"]:
        return "action_required", 1, READINESS_ACTION_MAP.get(result["action"], "inspect_runtime_supervisor")
    return "indeterminate", 2, "inspect_runtime_supervisor"


def _stable_task_result(
    expected: ExpectedTask, first: TaskSnapshot,
) -> TaskSnapshot:
    second = acquire_task_snapshot(expected)
    if second != first:
        raise SupervisorSnapshotChanged("task changed")
    return second


def _compose_once(
    expected: ExpectedTask, pinned_identity: dict[str, str],
    pinned_manifest: dict[str, object], now_ns: int,
) -> tuple[dict[str, object], int]:
    checks = _empty_checks()
    task = acquire_task_snapshot(expected)
    checks["scheduled_task"]["configuration"] = task.configuration
    checks["scheduled_task"]["state"] = task.state
    checks["scheduled_task"]["last_result"] = (
        "unknown" if task.last_result is None else "success" if task.last_result == 0 else "failure"
    )
    task_run_ns = _parse_utc(task.last_run_utc)
    task_freshness, task_age = _age(now_ns, task_run_ns)
    checks["scheduled_task"]["freshness"] = task_freshness

    if not task.found:
        _stable_task_result(expected, task)
        return _payload(code=1, model=expected.model, checks=checks, blockers=["scheduled_task_missing"], action="install_runtime_guard_task", task_age=task_age), 1
    if task.configuration != "match":
        _stable_task_result(expected, task)
        return _payload(code=2, model=expected.model, checks=checks, blockers=["scheduled_task_configuration_mismatch"], action="inspect_runtime_guard_task", task_age=task_age), 2
    if not task.enabled or task.state == "disabled":
        _stable_task_result(expected, task)
        return _payload(code=1, model=expected.model, checks=checks, blockers=["scheduled_task_disabled"], action="enable_runtime_guard_task", task_age=task_age), 1
    if task.state in {"running", "queued"}:
        _stable_task_result(expected, task)
        if task_age is not None and task_age > MAX_CORRELATION_SECONDS:
            return _payload(code=2, model=expected.model, checks=checks, blockers=["scheduled_task_run_overdue"], action="inspect_runtime_guard_task", task_age=task_age), 2
        return _payload(code=1, model=expected.model, checks=checks, blockers=["scheduled_task_in_progress"], action="wait_for_runtime_guard_task", task_age=task_age), 1
    if task.state != "ready":
        _stable_task_result(expected, task)
        return _payload(code=2, model=expected.model, checks=checks, blockers=["scheduled_task_state_unknown"], action="inspect_runtime_guard_task", task_age=task_age), 2

    next_run_ns = _parse_utc(task.next_run_utc)
    next_delta = None if next_run_ns is None else (next_run_ns - now_ns) / 1_000_000_000
    if task_freshness != "fresh" or next_delta is None or next_delta < -DISPATCH_GRACE_SECONDS or next_delta > SCHEDULE_INTERVAL_SECONDS + DISPATCH_GRACE_SECONDS:
        _stable_task_result(expected, task)
        return _payload(code=1, model=expected.model, checks=checks, blockers=["scheduled_task_stale"], action="run_runtime_guard_task", task_age=task_age), 1

    try:
        journal = read_result_journal(expected.company_home)
    except JournalMissingError:
        checks["guard_journal"]["schema"] = "missing"
        _stable_task_result(expected, task)
        return _payload(code=1, model=expected.model, checks=checks, blockers=["guard_journal_missing"], action="run_runtime_guard_task", task_age=task_age), 1
    except JournalSnapshotError:
        checks["guard_journal"]["schema"] = "invalid"
        _stable_task_result(expected, task)
        return _payload(code=2, model=expected.model, checks=checks, blockers=["guard_journal_invalid"], action="inspect_runtime_guard", task_age=task_age), 2

    checks["guard_journal"]["schema"] = "valid"
    journal_state, journal_code, journal_action = _journal_status(journal.payload, expected.model)
    checks["guard_journal"]["status"] = journal_state
    journal_freshness, journal_age = _age(now_ns, journal.mtime_ns)
    checks["guard_journal"]["freshness"] = journal_freshness
    correlation = bool(
        task_run_ns is not None
        and task_run_ns - CLOCK_SKEW_SECONDS * 1_000_000_000 <= journal.mtime_ns
        <= task_run_ns + MAX_CORRELATION_SECONDS * 1_000_000_000
    )
    checks["guard_journal"]["correlation"] = "match" if correlation else "mismatch"
    if journal_freshness != "fresh" or not correlation:
        _stable_task_result(expected, task)
        return _payload(code=2, model=expected.model, checks=checks, blockers=["guard_journal_uncorrelated"], action="inspect_runtime_guard", task_age=task_age, journal_age=journal_age), 2
    inferred_task_code = task.last_result if task.last_result in {0, 1, 2} else None
    if inferred_task_code != journal_code:
        _stable_task_result(expected, task)
        return _payload(code=2, model=expected.model, checks=checks, blockers=["guard_result_mismatch"], action="inspect_runtime_guard", task_age=task_age, journal_age=journal_age), 2
    if journal_code != 0:
        _stable_task_result(expected, task)
        return _payload(code=journal_code, model=expected.model, checks=checks, blockers=["runtime_guard_action_required"] if journal_code == 1 else ["runtime_guard_indeterminate"], action=journal_action, task_age=task_age, journal_age=journal_age), journal_code

    try:
        with _verified_executable_sha256(expected.ollama_executable, expected.ollama_sha256):
            checks["ollama_executable"]["pin"] = "match"
            readiness_state, readiness_code, readiness_action = _readiness_status(expected.model, expected.company_home)
            checks["readiness"]["status"] = readiness_state
            checks["readiness"]["action"] = readiness_action
            journal_after = read_result_journal(expected.company_home)
            task_after = acquire_task_snapshot(expected)
            try:
                identity_after = read_company_identity(expected.company_home)
            except (CompanyIdentityError, OSError, TypeError, ValueError, RecursionError):
                raise SupervisorSnapshotChanged("store changed")
            try:
                manifest_after = check_project(PROJECT_ROOT)
            except (ManifestError, OSError, TypeError, ValueError, RecursionError):
                raise SupervisorSnapshotChanged("manifest changed")
            if (
                task_after != task or journal_after != journal
                or identity_after != pinned_identity or manifest_after != pinned_manifest
            ):
                raise SupervisorSnapshotChanged("snapshot changed")
    except GuardExecutableHashMismatch:
        checks["ollama_executable"]["pin"] = "mismatch"
        return _payload(code=2, model=expected.model, checks=checks, blockers=["ollama_executable_pin_mismatch"], action="inspect_ollama_service", task_age=task_age, journal_age=journal_age), 2
    except GuardExecutableError:
        checks["ollama_executable"]["pin"] = "invalid"
        return _payload(code=2, model=expected.model, checks=checks, blockers=["ollama_executable_invalid"], action="inspect_ollama_service", task_age=task_age, journal_age=journal_age), 2
    except JournalSnapshotError:
        raise SupervisorSnapshotChanged("journal changed")

    if readiness_code == 0:
        return _payload(code=0, model=expected.model, checks=checks, blockers=[], action="none", task_age=task_age, journal_age=journal_age), 0
    blocker = "readiness_action_required" if readiness_code == 1 else "readiness_indeterminate"
    return _payload(code=readiness_code, model=expected.model, checks=checks, blockers=[blocker], action=readiness_action, task_age=task_age, journal_age=journal_age), readiness_code


def _prepare_expected(
    home: Path, python_executable: Path, ollama_executable: Path,
    ollama_sha256: str, task_name: str, model: str,
    allow_windows_job_inheritance: bool,
) -> tuple[ExpectedTask, dict[str, str]]:
    if (
        os.name != "nt" or task_name != EXPECTED_TASK_NAME
        or not _valid_model_name(model)
        or not is_supported_local_model(model)
        or type(ollama_sha256) is not str
        or OLLAMA_SHA256_PATTERN.fullmatch(ollama_sha256) is None
        or type(allow_windows_job_inheritance) is not bool
    ):
        raise SupervisorUsageError("invalid arguments")
    try:
        normalized_home = _normalized_company_home(Path(home))
        identity = read_company_identity(normalized_home)
        if not _valid_store_identity(identity):
            raise CompanyIdentityError("invalid identity")
        trusted_python = _validated_local_file(Path(python_executable))
        trusted_ollama = _validated_local_file(Path(ollama_executable))
        runtime_guard = _validated_local_file(PROJECT_ROOT / "scripts" / "runtime_guard.py")
    except (CompanyIdentityError, GuardExecutableError, OSError, TypeError, ValueError, RecursionError) as exc:
        raise SupervisorUsageError("invalid arguments") from exc
    if trusted_python.suffix.casefold() != ".exe" or trusted_ollama.suffix.casefold() != ".exe":
        raise SupervisorUsageError("invalid arguments")
    model = require_local_llama_model(model)
    return ExpectedTask(
        task_name, trusted_python, runtime_guard, normalized_home, trusted_ollama,
        ollama_sha256, model, allow_windows_job_inheritance,
    ), dict(identity)


def run_supervisor_check(
    home: Path, python_executable: Path, ollama_executable: Path,
    ollama_sha256: str, task_name: str = EXPECTED_TASK_NAME,
    model: str = DEFAULT_LOCAL_MODEL, allow_windows_job_inheritance: bool = False,
) -> tuple[dict[str, object], int]:
    expected, identity = _prepare_expected(
        home, python_executable, ollama_executable, ollama_sha256,
        task_name, model, allow_windows_job_inheritance,
    )
    try:
        manifest = check_project(PROJECT_ROOT)
        if not isinstance(manifest, dict) or manifest.get("status") != "ok":
            raise ManifestError("invalid manifest")
    except (ManifestError, OSError, TypeError, ValueError, RecursionError):
        return _payload(code=2, model=model, blockers=["disk_manifest_invalid"], action="inspect_build_manifest"), 2
    for attempt in range(2):
        try:
            return _compose_once(expected, identity, manifest, time.time_ns())
        except SupervisorSnapshotChanged:
            if attempt == 0:
                continue
            return _payload(code=2, model=model, blockers=["supervisor_snapshot_changed"], action="retry_runtime_supervisor"), 2
        except TaskSnapshotError:
            return _payload(code=2, model=model, blockers=["scheduled_task_query_failed"], action="inspect_runtime_guard_task"), 2
        except JournalSnapshotError:
            return _payload(code=2, model=model, blockers=["guard_journal_invalid"], action="inspect_runtime_guard"), 2
    raise AssertionError("unreachable")


def parser() -> argparse.ArgumentParser:
    result = _SanitizedArgumentParser(description=__doc__)
    result.add_argument("--home", type=Path, required=True)
    result.add_argument("--python-executable", type=Path, required=True)
    result.add_argument("--ollama-executable", type=Path, required=True)
    result.add_argument("--ollama-sha256", required=True)
    result.add_argument("--task-name", default=EXPECTED_TASK_NAME)
    result.add_argument("--model", default=DEFAULT_LOCAL_MODEL)
    result.add_argument("--allow-windows-job-inheritance", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result, code = run_supervisor_check(
            args.home, args.python_executable, args.ollama_executable,
            args.ollama_sha256, task_name=args.task_name, model=args.model,
            allow_windows_job_inheritance=args.allow_windows_job_inheritance,
        )
    except SupervisorUsageError:
        result = _payload(code=2, model=None, blockers=["invalid_arguments"], action="fix_command_arguments")
        code = 3
    except Exception:
        result = _payload(code=2, model=None, blockers=["internal_supervisor_error"], action="inspect_runtime_supervisor")
        code = 3
    try:
        rendered = render_result(result)
    except Exception:
        rendered = render_result(_payload(code=2, model=None, blockers=["internal_supervisor_error"], action="inspect_runtime_supervisor"))
        code = 3
    _write_stdout(rendered)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
