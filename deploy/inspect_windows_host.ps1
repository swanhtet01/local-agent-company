# Read-only capacity inventory. Run on the target host; no remoting or installs.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$cpus = @(Get-CimInstance Win32_Processor)
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [ordered]@{ drive = $_.DeviceID; sizeBytes = [long]$_.Size; freeBytes = [long]$_.FreeSpace }
})
$tools = [ordered]@{}
foreach ($name in @('python', 'git', 'ollama', 'opencode', 'docker', 'node')) {
    # Discover only: do not execute installed programs or start runtimes.
    $tools[$name] = [bool](Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)
}
$portStatus = 'observed'
$ports = @()
try {
    $ports = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -in @(22, 3389, 4173, 8765, 11434) } |
        ForEach-Object {
            [ordered]@{
                port = $_.LocalPort
                binding = $(if ($_.LocalAddress -in @('127.0.0.1', '::1')) { 'loopback' } else { 'non-loopback' })
            }
        })
} catch {
    # Missing permission or cmdlet must not be reported as zero listeners.
    $portStatus = 'unavailable'
}
# Count only recognized trading executables; never expose process arguments,
# account identifiers, window titles or executable paths. Absence is not proof
# that this host has no trading workload (services or renamed programs may exist).
$tradingStatus = 'observed'
$tradingCount = $null
$tradingWorkingSet = $null
try {
    $trading = @(Get-Process -ErrorAction Stop | Where-Object {
        $_.ProcessName -in @('terminal', 'terminal64', 'metaeditor', 'metaeditor64', 'metatester', 'metatester64')
    })
    $tradingCount = $trading.Count
    $tradingWorkingSet = [long]0
    foreach ($process in $trading) {
        $tradingWorkingSet += [long]$process.WorkingSet64
    }
} catch {
    $tradingStatus = 'unavailable'
    $tradingCount = $null
    $tradingWorkingSet = $null
}
[ordered]@{
    schema = 'local-company.windows-host-inventory.v1'
    observedAtUtc = [DateTime]::UtcNow.ToString('o')
    scope = 'executing-host-only'
    operatingSystem = $os.Caption
    osVersion = $os.Version
    logicalProcessors = ($cpus | Measure-Object NumberOfLogicalProcessors -Sum).Sum
    totalMemoryBytes = [long]$os.TotalVisibleMemorySize * 1024
    availableMemoryBytes = [long]$os.FreePhysicalMemory * 1024
    disks = $disks
    toolsOnPath = $tools
    listenerObservation = $portStatus
    relevantListeners = $ports
    tradingWorkloadObservation = $tradingStatus
    recognizedTradingProcessCount = $tradingCount
    recognizedTradingWorkingSetBytes = $tradingWorkingSet
    tradingInventoryComplete = $false
    deploymentReady = $null
    effects = @{ installs = $false; settingsChanged = $false; modelLoaded = $false; remoteConnection = $false }
} | ConvertTo-Json -Depth 6
