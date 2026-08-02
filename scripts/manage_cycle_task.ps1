[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Status', 'Remove')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$taskName = 'SuperMega Local Product Cycle'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$python = Join-Path $root '.venv\Scripts\python.exe'
$runner = Join-Path $root 'scripts\run_scheduled_cycle.py'
$launcher = Join-Path $root 'scripts\local_ai.py'
$optimizer = Join-Path (Split-Path -Parent $root) 'supermega-platform\tools\trim_codex_working_sets.ps1'
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$runnerDigest = (Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash.ToLowerInvariant()
$launcherDigest = (Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant()
$optimizerDigest = (Get-FileHash -LiteralPath $optimizer -Algorithm SHA256).Hash.ToLowerInvariant()
$runnerLiteral = "'" + $runner.Replace("'", "''") + "'"
$launcherLiteral = "'" + $launcher.Replace("'", "''") + "'"
$optimizerLiteral = "'" + $optimizer.Replace("'", "''") + "'"
$pythonLiteral = "'" + $python.Replace("'", "''") + "'"
$guardCommand = @"
`$ErrorActionPreference = 'Stop'
`$runner = $runnerLiteral
`$launcher = $launcherLiteral
`$optimizer = $optimizerLiteral
if ((Get-FileHash -LiteralPath `$runner -Algorithm SHA256).Hash.ToLowerInvariant() -ne '$runnerDigest') { exit 90 }
if ((Get-FileHash -LiteralPath `$launcher -Algorithm SHA256).Hash.ToLowerInvariant() -ne '$launcherDigest') { exit 91 }
if ((Get-FileHash -LiteralPath `$optimizer -Algorithm SHA256).Hash.ToLowerInvariant() -ne '$optimizerDigest') { exit 92 }
& $pythonLiteral `$runner
exit `$LASTEXITCODE
"@
$encodedGuard = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($guardCommand))
$arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -EncodedCommand ' + $encodedGuard
$interval = 'PT6H'
$mutationCommitted = $false
$resultPath = Join-Path (Split-Path -Parent $root) 'supermega-local-company-state\autopilot-cycle-result.json'

function Get-CycleTask {
    return Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}

function Test-CycleTask([object]$Task) {
    if ($null -eq $Task) { return $false }
    $actions = @($Task.Actions)
    $triggers = @($Task.Triggers)
    if ($actions.Count -ne 1 -or $triggers.Count -ne 1) { return $false }
    $action = $actions[0]
    $trigger = $triggers[0]
    return (
        [string]::Equals([string]$action.Execute, $powerShell, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$action.Arguments, $arguments, [System.StringComparison]::Ordinal) -and
        [string]::Equals([string]$action.WorkingDirectory, $root, [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$trigger.Repetition.Interval, $interval, [System.StringComparison]::Ordinal) -and
        [string]::Equals([string]$Task.Principal.LogonType, 'Interactive', [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$Task.Principal.RunLevel, 'Limited', [System.StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$Task.Settings.MultipleInstances, 'IgnoreNew', [System.StringComparison]::OrdinalIgnoreCase) -and
        $Task.Settings.ExecutionTimeLimit -eq 'PT2H' -and
        $Task.Settings.Enabled
    )
}

function Write-Receipt([string]$Status, [object]$Task, [bool]$Changed) {
    $verified = Test-CycleTask $Task
    $info = $null
    if ($null -ne $Task) {
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    }
    $hasRun = $null -ne $info -and $info.LastTaskResult -ne 267011
    $lastCycle = $null
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        $resultFile = Get-Item -LiteralPath $resultPath
        if ($resultFile.Length -gt 0 -and $resultFile.Length -le 65536) {
            try {
                $result = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
                if ($result.schema -eq 'local-ai.scheduled-cycle-result.v1') {
                    $lastCycle = [ordered]@{
                        observedAt = [string]$result.observedAt
                        status = [string]$result.status
                        processExitCode = $result.processExitCode
                        cycleStatus = [string]$result.cycle.status
                        reason = [string]$result.cycle.reason
                        missionsRun = $result.cycle.missionsRun
                        modelCalled = $result.cycle.modelCalled
                        memoryRecoveryAttempted = $result.memoryRecovery.attempted
                        memoryRecoveryStatus = [string]$result.memoryRecovery.status
                        releasedWorkingSetMb = $result.memoryRecovery.releasedWorkingSetMb
                    }
                }
            }
            catch { $lastCycle = $null }
        }
    }
    [ordered]@{
        schema = 'local-ai.autonomy-task.v1'
        status = $Status
        taskName = $taskName
        installed = $null -ne $Task
        verified = $verified
        changed = $Changed
        cadenceHours = 6
        action = if ($verified) { 'pinned local-ai cycle' } else { $null }
        lastRunTime = if ($hasRun) { $info.LastRunTime.ToString('o') } else { $null }
        lastTaskResult = if ($hasRun) { $info.LastTaskResult } else { $null }
        nextRunTime = if ($null -ne $info -and $info.NextRunTime -gt [datetime]::MinValue) { $info.NextRunTime.ToString('o') } else { $null }
        lastCycle = $lastCycle
        controls = [ordered]@{
            localOnly = $true
            interactiveUser = $true
            limitedPrivilege = $true
            overlappingRunsAllowed = $false
            modelMemoryGateBytes = 2147483648
            sourceDigestsPinned = $true
            boundedMemoryRecovery = $true
            externalActionsAllowed = $false
        }
    } | ConvertTo-Json -Depth 6 -Compress
}

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'task_python_missing' }
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw 'task_runner_missing' }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'task_launcher_missing' }
    if (-not (Test-Path -LiteralPath $optimizer -PathType Leaf)) { throw 'task_optimizer_missing' }
    $existing = Get-CycleTask
    if ($Mode -eq 'Status') {
        Write-Receipt -Status $(if ($null -eq $existing) { 'not_installed' } elseif (Test-CycleTask $existing) { 'ready' } else { 'mismatch' }) -Task $existing -Changed $false
        if ($null -ne $existing -and -not (Test-CycleTask $existing)) { exit 1 }
        exit 0
    }
    if ($Mode -eq 'Remove') {
        if ($null -eq $existing) {
            Write-Receipt -Status 'not_installed' -Task $null -Changed $false
            exit 0
        }
        if (-not (Test-CycleTask $existing)) { throw 'task_remove_refused_unverified_definition' }
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        Write-Receipt -Status 'removed' -Task $null -Changed $true
        exit 0
    }
    if ($null -ne $existing) {
        if (-not (Test-CycleTask $existing)) { throw 'task_install_refused_unverified_definition' }
        Write-Receipt -Status 'ready' -Task $existing -Changed $false
        exit 0
    }
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Runs one source-pinned, bounded, memory-gated local product-company cycle every six hours.' -ErrorAction Stop | Out-Null
    $mutationCommitted = $true
    $installed = Get-CycleTask
    if (-not (Test-CycleTask $installed)) { throw 'task_install_verification_failed' }
    Write-Receipt -Status 'installed' -Task $installed -Changed $true
}
catch {
    [ordered]@{
        schema = 'local-ai.autonomy-task.v1'
        status = 'error'
        reason = $_.Exception.Message
        taskName = $taskName
        changed = $mutationCommitted
        modelCalled = $false
        externalActionPerformed = $false
    } | ConvertTo-Json -Compress
    exit 1
}
