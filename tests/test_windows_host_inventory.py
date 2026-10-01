"""Read-only inventory contract with synthetic host responses."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell required")
class WindowsInventoryTests(unittest.TestCase):
    def inventory(self, processes):
        script = Path(__file__).resolve().parents[1] / "deploy" / "inspect_windows_host.ps1"
        command = r"""
function Get-CimInstance {
 param($ClassName, $Filter)
 switch ($ClassName) {
  Win32_OperatingSystem { [pscustomobject]@{Caption='Synthetic';Version='1';TotalVisibleMemorySize=8388608;FreePhysicalMemory=4194304} }
  Win32_Processor { [pscustomobject]@{NumberOfLogicalProcessors=4} }
  Win32_LogicalDisk { [pscustomobject]@{DeviceID='C:';Size=1000000;FreeSpace=500000} }
 }
}
function Get-NetTCPConnection { throw 'Unavailable' }
function Get-Process { PROCESS_BODY }
& 'SCRIPT_PATH'
""".replace("PROCESS_BODY", processes).replace("SCRIPT_PATH", str(script).replace("'", "''"))
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=30)
        if "PSSecurityException" in result.stderr:
            self.skipTest("Host execution policy blocks script execution; no bypass")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_counts_only_recognized_workloads(self):
        result = self.inventory("[pscustomobject]@{ProcessName='terminal64';WorkingSet64=1200}; [pscustomobject]@{ProcessName='metatester64';WorkingSet64=300}; [pscustomobject]@{ProcessName='private-app';WorkingSet64=9999}")
        self.assertEqual(result['recognizedTradingProcessCount'], 2)
        self.assertEqual(result['recognizedTradingWorkingSetBytes'], 1500)
        self.assertEqual(result['tradingWorkloadObservation'], 'observed')
        self.assertFalse(result['tradingInventoryComplete'])
        self.assertIsNone(result['deploymentReady'])
        self.assertNotIn('private-app', json.dumps(result))
        self.assertEqual(result['listenerObservation'], 'unavailable')

    def test_failure_is_unknown_not_zero(self):
        result = self.inventory("throw 'Access unavailable'")
        self.assertEqual(result['tradingWorkloadObservation'], 'unavailable')
        self.assertIsNone(result['recognizedTradingProcessCount'])
        self.assertIsNone(result['recognizedTradingWorkingSetBytes'])

    def test_empty_observation_is_not_complete_inventory(self):
        result = self.inventory("@()")
        self.assertEqual(result['recognizedTradingProcessCount'], 0)
        self.assertEqual(result['recognizedTradingWorkingSetBytes'], 0)
        self.assertFalse(result['tradingInventoryComplete'])

    def test_script_parses_without_execution(self):
        script = Path(__file__).resolve().parents[1] / "deploy" / "inspect_windows_host.ps1"
        command = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + str(script).replace("'", "''") + "', [ref]$tokens, [ref]$errors); if ($errors.Count) { exit 1 }"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
