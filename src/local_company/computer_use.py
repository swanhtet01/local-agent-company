from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


WORKFLOW_SCHEMA = "local-company.computer-workflow.v1"
PREVIEW_SCHEMA = "local-company.computer-workflow-preview.v1"
RUN_SCHEMA = "local-company.computer-workflow-run.v1"
DOCTOR_SCHEMA = "local-company.computer-use-doctor.v1"
WINDOWS_SCHEMA = "local-company.computer-windows.v1"
INSPECT_SCHEMA = "local-company.computer-inspection.v1"
LEARN_SCHEMA = "local-company.computer-workflow-learn.v1"
PROOF_SCHEMA = "local-company.computer-use-proof.v1"
RUN_CONFIRMATION = "RUN LOCAL COMPUTER WORKFLOW"
STOP_KEY = "F8"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VALUE_REF = re.compile(r"^TEXT_[1-9][0-9]{0,2}$")

NETWORK_PROCESSES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "outlook.exe", "olk.exe", "teams.exe", "ms-teams.exe", "slack.exe",
    "discord.exe", "telegram.exe", "whatsapp.exe", "thunderbird.exe",
    "zoom.exe", "skype.exe", "chatgpt.exe", "codex.exe", "notion.exe",
}
SHELL_PROCESSES = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "wsl.exe",
    "bash.exe", "python.exe", "pythonw.exe",
}
SAFE_KEYS = {
    0x08: "BACKSPACE", 0x09: "TAB", 0x0D: "ENTER", 0x1B: "ESC",
    0x21: "PAGEUP", 0x22: "PAGEDOWN", 0x23: "END", 0x24: "HOME",
    0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT", 0x28: "DOWN",
    0x2E: "DELETE",
}
VIRTUAL_KEYS = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "DELETE": 0x2E, "CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="ascii")
    os.replace(temporary, path)


def _safe_name(name: str) -> str:
    value = name.strip().lower()
    if not _NAME.fullmatch(value):
        raise ValueError("workflow_name_must_use_lowercase_letters_numbers_dash_or_underscore")
    return value


def workflow_root(company_home: Path, *, create: bool = False) -> Path:
    root = company_home.resolve() / "computer-workflows"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _workflow_path(company_home: Path, name: str) -> Path:
    return workflow_root(company_home) / _safe_name(name) / "workflow.json"


def _window_signature(window: dict[str, object]) -> dict[str, object]:
    bounds = window.get("bounds")
    if not isinstance(bounds, list) or len(bounds) != 4:
        bounds = [0, 0, 0, 0]
    return {
        "title": str(window.get("title", ""))[:512],
        "className": str(window.get("className", ""))[:256],
        "processName": str(window.get("processName", "")).lower()[:256],
        "recordedBounds": [int(value) for value in bounds],
    }


def _validate_window(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("workflow_window_invalid")
    title = value.get("title")
    process_name = value.get("processName")
    bounds = value.get("recordedBounds")
    if not isinstance(title, str) or not title or len(title) > 512:
        raise ValueError("workflow_window_title_invalid")
    if not isinstance(process_name, str) or len(process_name) > 256:
        raise ValueError("workflow_window_process_invalid")
    if (
        not isinstance(bounds, list) or len(bounds) != 4
        or any(type(item) is not int for item in bounds)
    ):
        raise ValueError("workflow_window_bounds_invalid")
    return value


def _validate_target(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("workflow_target_invalid")
    allowed = {"name", "automationId", "controlType", "className", "recordedBounds"}
    if set(value) - allowed:
        raise ValueError("workflow_target_unknown_field")
    for key in allowed - {"recordedBounds"}:
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 512:
            raise ValueError("workflow_target_field_invalid")
    bounds = value.get("recordedBounds", [0, 0, 0, 0])
    if (
        not isinstance(bounds, list) or len(bounds) != 4
        or any(type(item) not in {int, float} for item in bounds)
    ):
        raise ValueError("workflow_target_bounds_invalid")
    return value


def validate_workflow(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema") != WORKFLOW_SCHEMA:
        raise ValueError("workflow_schema_invalid")
    if _safe_name(str(payload.get("name", ""))) != payload.get("name"):
        raise ValueError("workflow_name_not_canonical")
    if payload.get("platform") != "windows":
        raise ValueError("workflow_platform_invalid")
    created = payload.get("createdAt")
    if not isinstance(created, str) or len(created) > 64:
        raise ValueError("workflow_created_at_invalid")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("workflow_steps_must_contain_one_to_one_hundred_items")
    expected_refs: list[str] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("id") != index:
            raise ValueError("workflow_step_id_invalid")
        action = step.get("action")
        if action not in {"click", "key", "text"}:
            raise ValueError("workflow_step_action_invalid")
        _validate_window(step.get("window"))
        delay = step.get("delayBeforeMs")
        if type(delay) is not int or not 0 <= delay <= 30_000:
            raise ValueError("workflow_step_delay_invalid")
        if action == "click":
            _validate_target(step.get("target", {}))
            relative = step.get("relativePoint")
            if (
                not isinstance(relative, list) or len(relative) != 2
                or any(type(item) not in {int, float} for item in relative)
                or any(not 0 <= float(item) <= 1 for item in relative)
            ):
                raise ValueError("workflow_click_relative_point_invalid")
        elif action == "key":
            key = step.get("key")
            if not isinstance(key, str) or not key or len(key) > 32:
                raise ValueError("workflow_key_invalid")
            parts = key.split("+")
            if any(part not in VIRTUAL_KEYS and not (len(part) == 1 and part.isalnum()) for part in parts):
                raise ValueError("workflow_key_unsupported")
        else:
            value_ref = step.get("valueRef")
            if not isinstance(value_ref, str) or not _VALUE_REF.fullmatch(value_ref):
                raise ValueError("workflow_text_reference_invalid")
            expected_refs.append(value_ref)
    if len(expected_refs) != len(set(expected_refs)):
        raise ValueError("workflow_text_reference_reused")
    final_title = payload.get("expectedFinalWindowTitleContains")
    if final_title is not None and (
        not isinstance(final_title, str) or not final_title or len(final_title) > 512
    ):
        raise ValueError("workflow_final_title_expectation_invalid")
    final_control = payload.get("expectedFinalControlTextContains")
    if final_control is not None and (
        not isinstance(final_control, str) or not final_control or len(final_control) > 512
    ):
        raise ValueError("workflow_final_control_expectation_invalid")
    recorded = payload.get("workflowSha256")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        raise ValueError("workflow_seal_invalid")
    unsealed = dict(payload)
    unsealed.pop("workflowSha256", None)
    if _digest(unsealed) != recorded:
        raise ValueError("workflow_seal_mismatch")
    return payload


def seal_workflow(payload: dict[str, object]) -> dict[str, object]:
    sealed = dict(payload)
    sealed.pop("workflowSha256", None)
    sealed["workflowSha256"] = _digest(sealed)
    return validate_workflow(sealed)


def load_workflow(company_home: Path, name: str) -> tuple[Path, dict[str, object]]:
    path = _workflow_path(company_home, name)
    if not path.is_file():
        raise ValueError("workflow_not_found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("workflow_file_invalid") from error
    return path, validate_workflow(payload)


def list_workflows(company_home: Path) -> dict[str, object]:
    root = workflow_root(company_home)
    items: list[dict[str, object]] = []
    if root.is_dir():
        for path in sorted(root.glob("*/workflow.json"))[:200]:
            try:
                payload = validate_workflow(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                items.append({"name": path.parent.name, "status": "invalid"})
                continue
            items.append({
                "name": payload["name"], "status": "ready",
                "stepCount": len(payload["steps"]),
                "createdAt": payload["createdAt"],
                "workflowSha256": payload["workflowSha256"],
            })
    return {
        "schema": "local-company.computer-workflow-list.v1",
        "status": "ready", "workflows": items, "workflowCount": len(items),
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }


class WindowsDesktopAdapter:
    def __init__(self) -> None:
        self.is_windows = os.name == "nt"
        self.powershell = shutil.which("powershell")
        if self.is_windows:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except (AttributeError, OSError):
                    pass

    def _require_windows(self) -> None:
        if not self.is_windows:
            raise RuntimeError("computer_use_requires_windows")

    def _powershell_json(
        self, script: str, values: dict[str, object] | None = None, *, timeout: int = 20,
    ) -> object:
        self._require_windows()
        if not self.powershell:
            raise RuntimeError("windows_powershell_not_found")
        environment = os.environ.copy()
        for key, value in (values or {}).items():
            environment[f"LC_{key.upper()}"] = str(value)
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=environment, timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("windows_automation_probe_failed")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("windows_automation_probe_invalid") from error

    def doctor(self) -> dict[str, object]:
        details: dict[str, object] = {
            "windows": self.is_windows, "powershell": bool(self.powershell),
            "uiAutomation": False, "screenCapture": False, "inputInjection": self.is_windows,
        }
        if self.is_windows and self.powershell:
            script = r"""
$result = [ordered]@{uiAutomation=$false;screenCapture=$false}
try { Add-Type -AssemblyName UIAutomationClient; Add-Type -AssemblyName UIAutomationTypes; $result.uiAutomation=$true } catch {}
try { Add-Type -AssemblyName System.Drawing; Add-Type -AssemblyName System.Windows.Forms; $result.screenCapture=$true } catch {}
$result | ConvertTo-Json -Compress
"""
            try:
                observed = self._powershell_json(script)
                if isinstance(observed, dict):
                    details.update(observed)
            except RuntimeError:
                pass
        ready = all(details.values())
        return {
            "schema": DOCTOR_SCHEMA, "status": "ready" if ready else "blocked",
            "platform": platform.platform(), "capabilities": details,
            "workflow": "demonstrate_preview_confirm_replay",
            "typedCharactersStoredDuringLearning": False,
            "networkAndShellAppsBlockedByDefault": True,
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }

    @staticmethod
    def _process_path(process_id: int) -> str:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def _window_record(self, handle: int) -> dict[str, object] | None:
        self._require_windows()
        user32 = ctypes.windll.user32
        if not user32.IsWindowVisible(handle):
            return None
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return None
        title_buffer = ctypes.create_unicode_buffer(min(length + 1, 2048))
        user32.GetWindowTextW(handle, title_buffer, len(title_buffer))
        title = title_buffer.value.strip()
        if not title:
            return None
        class_buffer = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(handle, class_buffer, len(class_buffer))
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        process_path = self._process_path(process_id.value)
        process_name = Path(process_path).name.lower() if process_path else ""
        identity = _digest({
            "title": title, "class": class_buffer.value,
            "process": process_name, "pid": process_id.value,
        })[:12]
        return {
            "windowRef": identity, "handle": int(handle), "title": title,
            "className": class_buffer.value, "processId": int(process_id.value),
            "processName": process_name, "bounds": [
                int(rect.left), int(rect.top), int(rect.right), int(rect.bottom),
            ],
        }

    def windows(self, limit: int = 50) -> list[dict[str, object]]:
        self._require_windows()
        if not 1 <= limit <= 200:
            raise ValueError("window_limit_must_be_between_one_and_two_hundred")
        records: list[dict[str, object]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(handle: int, _parameter: int) -> bool:
            if len(records) < limit:
                item = self._window_record(handle)
                if item is not None:
                    records.append(item)
            return len(records) < limit

        ctypes.windll.user32.EnumWindows(callback, 0)
        return records

    def foreground_window(self) -> dict[str, object] | None:
        self._require_windows()
        handle = ctypes.windll.user32.GetForegroundWindow()
        return self._window_record(handle) if handle else None

    def find_window(self, signature: dict[str, object]) -> dict[str, object] | None:
        title = str(signature.get("title", "")).casefold()
        process_name = str(signature.get("processName", "")).casefold()
        candidates = [
            item for item in self.windows(200)
            if (not process_name or str(item["processName"]).casefold() == process_name)
        ]
        exact = [item for item in candidates if str(item["title"]).casefold() == title]
        if len(exact) == 1:
            return exact[0]
        contains = [item for item in candidates if title and title in str(item["title"]).casefold()]
        if len(contains) == 1:
            return contains[0]
        class_name = str(signature.get("className", "")).casefold()
        structural = [
            item for item in candidates
            if not class_name or str(item["className"]).casefold() == class_name
        ]
        if len(structural) == 1:
            return structural[0]
        return None

    def inspect_controls(
        self, window: dict[str, object], limit: int = 100,
    ) -> list[dict[str, object]]:
        if not 1 <= limit <= 200:
            raise ValueError("control_limit_must_be_between_one_and_two_hundred")
        script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$env:LC_HANDLE)
$items = @()
if ($null -ne $root) {
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
  $maximum = [Math]::Min([int]$env:LC_LIMIT, $all.Count)
  for ($i=0; $i -lt $maximum; $i++) {
    try {
      $c = $all.Item($i).Current; $b = $c.BoundingRectangle
      $items += [pscustomobject][ordered]@{name=$c.Name;automationId=$c.AutomationId;controlType=$c.ControlType.ProgrammaticName;className=$c.ClassName;enabled=$c.IsEnabled;offscreen=$c.IsOffscreen;bounds=@($b.Left,$b.Top,$b.Right,$b.Bottom)}
    } catch {}
  }
}
ConvertTo-Json -InputObject @($items) -Compress
"""
        observed = self._powershell_json(
            script, {"handle": window["handle"], "limit": limit}, timeout=30,
        )
        if observed is None:
            return []
        if isinstance(observed, dict):
            return [observed]
        if not isinstance(observed, list):
            raise RuntimeError("windows_control_inventory_invalid")
        return [item for item in observed if isinstance(item, dict)]

    def element_at_point(self, x: int, y: int) -> dict[str, object]:
        script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase
$point = New-Object System.Windows.Point([double]$env:LC_X,[double]$env:LC_Y)
$element = [System.Windows.Automation.AutomationElement]::FromPoint($point)
if ($null -eq $element) { @{} | ConvertTo-Json -Compress; exit }
$c=$element.Current; $b=$c.BoundingRectangle
[ordered]@{name=$c.Name;automationId=$c.AutomationId;controlType=$c.ControlType.ProgrammaticName;className=$c.ClassName;recordedBounds=@($b.Left,$b.Top,$b.Right,$b.Bottom)} | ConvertTo-Json -Compress
"""
        observed = self._powershell_json(script, {"x": x, "y": y})
        return observed if isinstance(observed, dict) else {}

    def screenshot(
        self, output: Path, window: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_windows()
        path = output.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() != ".png":
            raise ValueError("screenshot_output_must_be_png")
        script = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$bounds=[System.Windows.Forms.SystemInformation]::VirtualScreen
if ($env:LC_LEFT -and $env:LC_TOP -and $env:LC_WIDTH -and $env:LC_HEIGHT) {
  $bounds=New-Object System.Drawing.Rectangle([int]$env:LC_LEFT,[int]$env:LC_TOP,[int]$env:LC_WIDTH,[int]$env:LC_HEIGHT)
}
$bitmap=New-Object System.Drawing.Bitmap($bounds.Width,$bounds.Height)
$graphics=[System.Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.CopyFromScreen($bounds.Left,$bounds.Top,0,0,$bounds.Size)
  $bitmap.Save($env:LC_OUTPUT,[System.Drawing.Imaging.ImageFormat]::Png)
} finally { $graphics.Dispose(); $bitmap.Dispose() }
[ordered]@{left=$bounds.Left;top=$bounds.Top;width=$bounds.Width;height=$bounds.Height} | ConvertTo-Json -Compress
"""
        values: dict[str, object] = {"output": path}
        if window is not None:
            handle = int(window.get("handle", 0))
            client = wintypes.RECT()
            origin = wintypes.POINT()
            far_corner = wintypes.POINT()
            user32 = ctypes.windll.user32
            if (
                not handle
                or not user32.GetClientRect(handle, ctypes.byref(client))
            ):
                raise RuntimeError("screenshot_window_client_bounds_unavailable")
            origin.x, origin.y = client.left, client.top
            far_corner.x, far_corner.y = client.right, client.bottom
            if (
                not user32.ClientToScreen(handle, ctypes.byref(origin))
                or not user32.ClientToScreen(handle, ctypes.byref(far_corner))
            ):
                raise RuntimeError("screenshot_window_client_coordinates_unavailable")
            left, top, right, bottom = (
                origin.x, origin.y, far_corner.x, far_corner.y,
            )
            if right <= left or bottom <= top:
                raise ValueError("screenshot_window_bounds_invalid")
            values.update({
                "left": left, "top": top,
                "width": right - left, "height": bottom - top,
            })
        observed = self._powershell_json(script, values, timeout=30)
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("screen_capture_failed")
        return {
            "path": str(path), "sha256": _file_digest(path),
            "bytes": path.stat().st_size, "screen": observed,
            "captureArea": "window_client" if window is not None else "virtual_screen",
        }

    def resolve_target(
        self, window: dict[str, object], target: dict[str, object],
    ) -> dict[str, object] | None:
        script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$target = $env:LC_TARGET | ConvertFrom-Json
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$env:LC_HANDLE)
$found = @()
if ($null -ne $root) {
  $all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
  for($i=0;$i -lt [Math]::Min($all.Count,1000);$i++) {
    try {
      $c=$all.Item($i).Current; $score=0
      if ($target.automationId -and $c.AutomationId -eq $target.automationId) {$score+=100}
      if ($target.name -and $c.Name -eq $target.name) {$score+=30}
      if ($target.controlType -and $c.ControlType.ProgrammaticName -eq $target.controlType) {$score+=10}
      if ($target.className -and $c.ClassName -eq $target.className) {$score+=5}
      if ($score -gt 0) { $b=$c.BoundingRectangle; $found += [pscustomobject][ordered]@{score=$score;bounds=@($b.Left,$b.Top,$b.Right,$b.Bottom)} }
    } catch {}
  }
}
if ($found.Count -eq 0) { @{} | ConvertTo-Json -Compress; exit }
$ordered=@($found | Sort-Object score -Descending); $top=$ordered[0]
$ties=@($ordered | Where-Object {$_.score -eq $top.score}).Count
[ordered]@{score=$top.score;ties=$ties;bounds=$top.bounds} | ConvertTo-Json -Compress
"""
        observed = self._powershell_json(
            script, {
                "handle": window["handle"],
                "target": json.dumps(target, separators=(",", ":"), ensure_ascii=False),
            }, timeout=30,
        )
        if not isinstance(observed, dict) or not observed:
            return None
        bounds = observed.get("bounds")
        if (
            observed.get("ties") != 1 or not isinstance(bounds, list) or len(bounds) != 4
        ):
            return None
        return observed

    @staticmethod
    def _focus(window: dict[str, object]) -> None:
        handle = int(window["handle"])
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        target_thread = user32.GetWindowThreadProcessId(handle, None)
        attached: list[int] = []
        try:
            for thread_id in {foreground_thread, target_thread}:
                if thread_id and thread_id != current_thread:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached.append(thread_id)
            user32.ShowWindowAsync(handle, 9)
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
        finally:
            for thread_id in reversed(attached):
                user32.AttachThreadInput(current_thread, thread_id, False)
        time.sleep(0.12)
        if user32.GetForegroundWindow() != handle:
            raise RuntimeError("workflow_window_could_not_be_focused")

    def click(
        self, window: dict[str, object], target: dict[str, object], relative: list[float],
    ) -> dict[str, object]:
        self._focus(window)
        resolved = self.resolve_target(window, target)
        if resolved is not None:
            left, top, right, bottom = [float(item) for item in resolved["bounds"]]
            x, y = round((left + right) / 2), round((top + bottom) / 2)
            method = "uia_anchor"
        else:
            left, top, right, bottom = [int(item) for item in window["bounds"]]
            x = round(left + (right - left) * float(relative[0]))
            y = round(top + (bottom - top) * float(relative[1]))
            method = "window_relative_fallback"
        user32 = ctypes.windll.user32
        if not user32.SetCursorPos(x, y):
            raise RuntimeError("workflow_pointer_move_failed")
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        return {"method": method, "point": [x, y]}

    @staticmethod
    def _virtual_key(part: str) -> int:
        if part in VIRTUAL_KEYS:
            return VIRTUAL_KEYS[part]
        if len(part) == 1 and part.isalnum():
            return ord(part.upper())
        raise ValueError("workflow_key_unsupported")

    def keypress(self, window: dict[str, object], key: str) -> dict[str, object]:
        self._focus(window)
        parts = key.split("+")
        codes = [self._virtual_key(part) for part in parts]
        user32 = ctypes.windll.user32
        for code in codes:
            user32.keybd_event(code, 0, 0, 0)
        for code in reversed(codes):
            user32.keybd_event(code, 0, 0x0002, 0)
        return {"key": key}

    def type_text(self, window: dict[str, object], value: str) -> dict[str, object]:
        self._focus(window)
        if len(value) > 4096:
            raise ValueError("workflow_text_value_too_long")
        user32 = ctypes.windll.user32
        user32.keybd_event(VIRTUAL_KEYS["CTRL"], 0, 0, 0)
        user32.keybd_event(ord("A"), 0, 0, 0)
        user32.keybd_event(ord("A"), 0, 0x0002, 0)
        user32.keybd_event(VIRTUAL_KEYS["CTRL"], 0, 0x0002, 0)
        time.sleep(0.05)

        ulong_ptr = wintypes.WPARAM

        class KeyInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr),
            ]

        class HardwareInput(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]

        units = value.encode("utf-16-le")
        events: list[Input] = []
        for offset in range(0, len(units), 2):
            unit = int.from_bytes(units[offset:offset + 2], "little")
            events.append(Input(type=1, ki=KeyInput(0, unit, 0x0004, 0, 0)))
            events.append(Input(type=1, ki=KeyInput(0, unit, 0x0004 | 0x0002, 0, 0)))
        if events:
            array = (Input * len(events))(*events)
            sent = user32.SendInput(len(events), array, ctypes.sizeof(Input))
            if sent != len(events):
                raise RuntimeError("workflow_text_input_incomplete")
        return {"characters": len(value), "inputMode": "replace_focused_value"}


def windows_observation(adapter: WindowsDesktopAdapter, limit: int = 50) -> dict[str, object]:
    windows = adapter.windows(limit)
    return {
        "schema": WINDOWS_SCHEMA, "status": "ready", "windows": windows,
        "windowCount": len(windows), "modelCalled": False,
        "stateMutated": False, "externalActionPerformed": False,
    }


def inspect_window(
    adapter: WindowsDesktopAdapter, title: str, limit: int = 100,
) -> dict[str, object]:
    query = title.strip().casefold()
    if not query or len(query) > 512:
        raise ValueError("window_title_query_invalid")
    matches = [
        item for item in adapter.windows(200)
        if query in str(item["title"]).casefold()
    ]
    if len(matches) != 1:
        raise ValueError("window_title_must_match_exactly_one_visible_window")
    controls = adapter.inspect_controls(matches[0], limit)
    return {
        "schema": INSPECT_SCHEMA, "status": "ready", "window": matches[0],
        "controls": controls, "controlCount": len(controls),
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }


def capture_screen(adapter: WindowsDesktopAdapter, output: Path) -> dict[str, object]:
    path = output.resolve()
    if path.exists():
        raise ValueError("screenshot_output_already_exists")
    evidence = adapter.screenshot(path)
    return {
        "schema": "local-company.computer-screen-capture.v1", "status": "captured",
        "evidence": evidence, "modelCalled": False, "stateMutated": True,
        "externalActionPerformed": False,
    }


def _start_workflow_lab_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "local_company.workflow_lab"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def launch_workflow_lab() -> dict[str, object]:
    process = _start_workflow_lab_process()
    return {
        "schema": "local-company.computer-workflow-lab.v1",
        "status": "started", "processId": process.pid,
        "windowTitle": "SuperMega Workflow Lab",
        "writesFiles": False, "networkRequests": 0,
        "modelCalled": False, "externalActionPerformed": False,
        "nextCommand": (
            "local-ai.cmd automate learn my-first-workflow --seconds 60 "
            "--expect-window-title \"Verified locally\""
        ),
    }


def _control_target(control: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(control.get("name", "")),
        "automationId": str(control.get("automationId", "")),
        "controlType": str(control.get("controlType", "")),
        "className": str(control.get("className", "")),
        "recordedBounds": [float(item) for item in control["bounds"]],
    }


def _relative_center(
    control: dict[str, object], window: dict[str, object],
) -> list[float]:
    left, top, right, bottom = [float(item) for item in window["bounds"]]
    c_left, c_top, c_right, c_bottom = [float(item) for item in control["bounds"]]
    width, height = max(1.0, right - left), max(1.0, bottom - top)
    return [
        round(min(1.0, max(0.0, ((c_left + c_right) / 2 - left) / width)), 6),
        round(min(1.0, max(0.0, ((c_top + c_bottom) / 2 - top) / height)), 6),
    ]


def _close_owned_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return process.poll() is not None


def prove_computer_use(
    company_home: Path,
    confirmation: str,
    *,
    adapter: WindowsDesktopAdapter | None = None,
    process_launcher: Callable[[], subprocess.Popen[bytes]] | None = None,
) -> dict[str, object]:
    """Run one visible, deterministic, self-contained proof on the local desktop."""
    if confirmation != RUN_CONFIRMATION:
        raise ValueError("computer_workflow_confirmation_invalid")
    desktop = adapter or WindowsDesktopAdapter()
    doctor = desktop.doctor()
    if doctor["status"] != "ready":
        raise RuntimeError("computer_use_doctor_blocked")
    process = (process_launcher or _start_workflow_lab_process)()
    proof_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    proof_home = company_home.resolve() / "computer-proofs" / proof_id
    run: dict[str, object] | None = None
    lab_closed = False
    try:
        deadline = time.monotonic() + 10
        window: dict[str, object] | None = None
        while time.monotonic() < deadline:
            matches = [
                item for item in desktop.windows(200)
                if int(item.get("processId", -1)) == process.pid
                and str(item.get("title", "")).startswith("SuperMega Workflow Lab")
            ]
            if len(matches) == 1:
                window = matches[0]
                break
            time.sleep(0.1)
        if window is None:
            raise RuntimeError("computer_use_proof_lab_window_unavailable")
        controls = desktop.inspect_controls(window, 100)
        entry_candidates = []
        button_candidates = []
        for control in controls:
            bounds = control.get("bounds")
            if not isinstance(bounds, list) or len(bounds) != 4:
                continue
            left, top, right, bottom = [float(item) for item in bounds]
            width, height = right - left, bottom - top
            class_name = str(control.get("className", ""))
            if class_name == "TkChild" and width >= 200 and 20 <= height <= 100:
                entry_candidates.append(control)
            if class_name == "Button" and width >= 50 and height >= 20:
                button_candidates.append(control)
        if len(entry_candidates) != 1 or len(button_candidates) != 1:
            raise RuntimeError("computer_use_proof_controls_ambiguous")
        entry, button = entry_candidates[0], button_candidates[0]
        signature = _window_signature(window)
        workflow = seal_workflow({
            "schema": WORKFLOW_SCHEMA,
            "name": "system-computer-proof",
            "createdAt": _now(),
            "platform": "windows",
            "learning": {
                "source": "built_in_verified_lab",
                "typedCharactersStoredInEventStream": False,
                "screenshotsCaptured": False,
                "stopReason": "compiled_proof",
            },
            "steps": [
                {
                    "id": 1, "action": "click", "delayBeforeMs": 0,
                    "window": signature, "target": _control_target(entry),
                    "relativePoint": _relative_center(entry, window),
                },
                {
                    "id": 2, "action": "text", "delayBeforeMs": 0,
                    "window": signature, "valueRef": "TEXT_1",
                },
                {
                    "id": 3, "action": "click", "delayBeforeMs": 0,
                    "window": signature, "target": _control_target(button),
                    "relativePoint": _relative_center(button, window),
                },
            ],
            "expectedFinalWindowTitleContains": "Verified locally",
            "expectedFinalControlTextContains": None,
        })
        workflow_path = (
            workflow_root(proof_home, create=True)
            / str(workflow["name"])
            / "workflow.json"
        )
        _atomic_json(workflow_path, workflow)
        run = run_workflow(
            proof_home,
            str(workflow["name"]),
            confirmation,
            adapter=desktop,
            value_provider=lambda _reference: "LOCAL COMPUTER USE WORKS",
        )
    finally:
        lab_closed = _close_owned_process(process)
    if run is None:
        raise RuntimeError("computer_use_proof_did_not_run")
    evidence_path = (
        Path(str(run["receiptPath"])).parent
        / f"step-{int(run['totalSteps']):03d}.png"
    )
    return {
        "schema": PROOF_SCHEMA,
        "status": "passed" if run["status"] == "completed" else "failed",
        "proofId": proof_id,
        "claim": "local Windows actions reached the built-in verified state",
        "doctor": doctor,
        "run": run,
        "evidencePath": str(evidence_path),
        "labProcessId": process.pid,
        "labClosedAfterProof": lab_closed,
        "modelCalled": False,
        "paidApiUsed": False,
        "stateMutated": True,
        "externalActionPerformed": False,
        "externalActionVerification": "built_in_lab_has_no_file_or_network_actions",
    }


def _current_window_or_error(adapter: WindowsDesktopAdapter) -> dict[str, object]:
    window = adapter.foreground_window()
    if window is None:
        raise RuntimeError("no_visible_foreground_window")
    return window


def _step_delay(last_event: float, current: float) -> int:
    return min(30_000, max(0, round((current - last_event) * 1000)))


def _record_input_events(seconds: int, maximum: int = 500) -> tuple[list[dict[str, object]], str]:
    if os.name != "nt":
        raise RuntimeError("computer_use_requires_windows")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    events: list[dict[str, object]] = []
    keys_down: set[int] = set()
    state = {"stop": False, "reason": "duration_elapsed"}

    class KeyboardEvent(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MouseEvent(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    hook_proc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    )

    @hook_proc
    def keyboard_hook(code: int, message: int, data: int) -> int:
        if code >= 0:
            observed = ctypes.cast(data, ctypes.POINTER(KeyboardEvent)).contents
            key = int(observed.vkCode)
            if message in {0x0100, 0x0104}:
                if key not in keys_down:
                    keys_down.add(key)
                    if key == 0x77:
                        state["stop"] = True
                        state["reason"] = "f8_pressed"
                    elif key in SAFE_KEYS or 0x30 <= key <= 0x5A or key == 0xE7:
                        events.append({
                            "kind": "key", "keyCode": key,
                            "ctrl": 0x11 in keys_down,
                            "handle": int(user32.GetForegroundWindow()),
                            "time": time.monotonic(),
                        })
            elif message in {0x0101, 0x0105}:
                keys_down.discard(key)
        if len(events) >= maximum:
            state["stop"] = True
            state["reason"] = "event_limit_reached"
        return int(user32.CallNextHookEx(None, code, message, data))

    @hook_proc
    def mouse_hook(code: int, message: int, data: int) -> int:
        if code >= 0 and message == 0x0201:
            observed = ctypes.cast(data, ctypes.POINTER(MouseEvent)).contents
            events.append({
                "kind": "click", "x": int(observed.pt.x), "y": int(observed.pt.y),
                "handle": int(user32.GetForegroundWindow()),
                "time": time.monotonic(),
            })
        if len(events) >= maximum:
            state["stop"] = True
            state["reason"] = "event_limit_reached"
        return int(user32.CallNextHookEx(None, code, message, data))

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.CallNextHookEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    module = kernel32.GetModuleHandleW(None)
    keyboard_handle = user32.SetWindowsHookExW(13, keyboard_hook, module, 0)
    mouse_handle = user32.SetWindowsHookExW(14, mouse_hook, module, 0)
    if not keyboard_handle or not mouse_handle:
        if keyboard_handle:
            user32.UnhookWindowsHookEx(keyboard_handle)
        if mouse_handle:
            user32.UnhookWindowsHookEx(mouse_handle)
        raise RuntimeError("computer_input_hooks_unavailable")
    started = time.monotonic()
    message = wintypes.MSG()
    try:
        while not state["stop"] and time.monotonic() - started < seconds:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0x0001):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.005)
    finally:
        user32.UnhookWindowsHookEx(keyboard_handle)
        user32.UnhookWindowsHookEx(mouse_handle)
    return sorted(events, key=lambda item: float(item["time"])), str(state["reason"])


def learn_workflow(
    company_home: Path,
    name: str,
    seconds: int,
    *,
    screenshots: bool = False,
    expected_final_title: str | None = None,
    expected_final_control_text: str | None = None,
    adapter: WindowsDesktopAdapter | None = None,
) -> dict[str, object]:
    workflow_name = _safe_name(name)
    if not 5 <= seconds <= 600:
        raise ValueError("learning_seconds_must_be_between_five_and_six_hundred")
    desktop = adapter or WindowsDesktopAdapter()
    desktop._require_windows()
    root = workflow_root(company_home, create=True)
    destination = root / workflow_name
    if destination.exists():
        raise ValueError("workflow_already_exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{workflow_name}-recording-", dir=root))
    evidence_dir = staging / "evidence"
    if screenshots:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    steps: list[dict[str, object]] = []
    started = time.monotonic()
    last_event = started
    last_text = -100.0
    text_number = 0
    try:
        raw_events, stop_reason = _record_input_events(seconds)
        for event in raw_events:
            if len(steps) >= 100:
                stop_reason = "step_limit_reached"
                break
            current = float(event["time"])
            window = desktop._window_record(int(event["handle"]))
            if window is None:
                continue
            if event["kind"] == "click":
                x, y = int(event["x"]), int(event["y"])
                left, top, right, bottom = [int(item) for item in window["bounds"]]
                width, height = max(1, right - left), max(1, bottom - top)
                target = desktop.element_at_point(x, y)
                step: dict[str, object] = {
                    "id": len(steps) + 1, "action": "click",
                    "delayBeforeMs": _step_delay(last_event, current),
                    "window": _window_signature(window), "target": target,
                    "relativePoint": [
                        round(min(1.0, max(0.0, (x - left) / width)), 6),
                        round(min(1.0, max(0.0, (y - top) / height)), 6),
                    ],
                }
                if screenshots:
                    image = evidence_dir / f"step-{len(steps) + 1:03d}.png"
                    observed = desktop.screenshot(image, window)
                    step["screenshot"] = str(image.relative_to(staging)).replace("\\", "/")
                    step["screenshotSha256"] = observed["sha256"]
                steps.append(step)
                last_event = current
                last_text = -100.0
                continue
            code = int(event["keyCode"])
            if code in SAFE_KEYS:
                steps.append({
                    "id": len(steps) + 1, "action": "key",
                    "delayBeforeMs": _step_delay(last_event, current),
                    "window": _window_signature(window), "key": SAFE_KEYS[code],
                })
                last_event = current
                last_text = -100.0
            elif bool(event.get("ctrl")):
                steps.append({
                    "id": len(steps) + 1, "action": "key",
                    "delayBeforeMs": _step_delay(last_event, current),
                    "window": _window_signature(window), "key": f"CTRL+{chr(code)}",
                })
                last_event = current
                last_text = -100.0
            elif current - last_text > 0.8:
                text_number += 1
                steps.append({
                    "id": len(steps) + 1, "action": "text",
                    "delayBeforeMs": _step_delay(last_event, current),
                    "window": _window_signature(window),
                    "valueRef": f"TEXT_{text_number}",
                })
                last_event = current
                last_text = current
            else:
                last_text = current
        if not steps:
            raise ValueError("workflow_learning_captured_no_actions")
        final_window = desktop.foreground_window()
        final_expectation = expected_final_title.strip() if expected_final_title else (
            str(final_window["title"]) if final_window is not None else None
        )
        payload = seal_workflow({
            "schema": WORKFLOW_SCHEMA, "name": workflow_name,
            "createdAt": _now(), "platform": "windows",
            "learning": {
                "stopReason": stop_reason, "durationSeconds": round(time.monotonic() - started, 3),
                "typedCharactersStoredInEventStream": False,
                "screenshotsCaptured": screenshots,
                "screenshotsMayContainVisiblePrivateContent": screenshots,
                "screenshotCaptureTiming": (
                    "post_demonstration_reference_only" if screenshots else "disabled"
                ),
                "stopKey": STOP_KEY,
            },
            "steps": steps,
            "expectedFinalWindowTitleContains": final_expectation,
            "expectedFinalControlTextContains": (
                expected_final_control_text.strip() if expected_final_control_text else None
            ),
        })
        _atomic_json(staging / "workflow.json", payload)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "schema": LEARN_SCHEMA, "status": "learned", "name": workflow_name,
        "stepCount": len(steps), "workflowSha256": payload["workflowSha256"],
        "workflowPath": str(destination / "workflow.json"),
        "typedCharactersStoredInEventStream": False,
        "screenshotsCaptured": screenshots,
        "screenshotsMayContainVisiblePrivateContent": screenshots,
        "nextCommand": f"local-ai.cmd automate preview {workflow_name}",
        "modelCalled": False, "stateMutated": True,
        "externalActionPerformed": False,
    }


def _process_blockers(
    process_name: str, title: str, *, allow_network_apps: bool, allow_shells: bool,
) -> list[str]:
    name = process_name.casefold()
    if name in {"python.exe", "pythonw.exe"} and title.startswith("SuperMega Workflow Lab"):
        return []
    blockers: list[str] = []
    if name in NETWORK_PROCESSES and not allow_network_apps:
        blockers.append(f"network_app_requires_explicit_flag:{name}")
    if name in SHELL_PROCESSES and not allow_shells:
        blockers.append(f"shell_app_requires_explicit_flag:{name}")
    return blockers


def preview_workflow(
    company_home: Path,
    name: str,
    *,
    allow_network_apps: bool = False,
    allow_shells: bool = False,
    adapter: WindowsDesktopAdapter | None = None,
) -> dict[str, object]:
    _path, payload = load_workflow(company_home, name)
    desktop = adapter or WindowsDesktopAdapter()
    blockers: list[str] = []
    observations: list[dict[str, object]] = []
    required_inputs: list[str] = []
    cache: dict[str, dict[str, object] | None] = {}
    for step in payload["steps"]:
        signature = step["window"]
        key = _digest(signature)
        if key not in cache:
            cache[key] = desktop.find_window(signature)
        window = cache[key]
        process_name = str(signature.get("processName", ""))
        blockers.extend(_process_blockers(
            process_name, str(signature.get("title", "")),
            allow_network_apps=allow_network_apps,
            allow_shells=allow_shells,
        ))
        observation: dict[str, object] = {
            "step": step["id"], "action": step["action"],
            "windowReady": window is not None,
        }
        if window is None:
            blockers.append(f"step_{step['id']}_window_not_found")
        elif step["action"] == "click":
            resolved = desktop.resolve_target(window, step["target"])
            observation["resolution"] = (
                "uia_anchor" if resolved is not None else "window_relative_fallback"
            )
        elif step["action"] == "text":
            required_inputs.append(str(step["valueRef"]))
        observations.append(observation)
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema": PREVIEW_SCHEMA,
        "status": "ready" if not blockers else "blocked",
        "name": payload["name"], "workflowSha256": payload["workflowSha256"],
        "stepCount": len(payload["steps"]), "steps": observations,
        "requiredSecretInputs": required_inputs, "blockers": blockers,
        "confirmationRequired": RUN_CONFIRMATION,
        "networkAppsAllowed": allow_network_apps, "shellsAllowed": allow_shells,
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }


def run_workflow(
    company_home: Path,
    name: str,
    confirmation: str,
    *,
    allow_network_apps: bool = False,
    allow_shells: bool = False,
    capture_evidence: bool = True,
    adapter: WindowsDesktopAdapter | None = None,
    value_provider: Callable[[str], str] | None = None,
) -> dict[str, object]:
    if confirmation != RUN_CONFIRMATION:
        raise ValueError("computer_workflow_confirmation_invalid")
    workflow_path, payload = load_workflow(company_home, name)
    desktop = adapter or WindowsDesktopAdapter()
    preview = preview_workflow(
        company_home, name,
        allow_network_apps=allow_network_apps, allow_shells=allow_shells,
        adapter=desktop,
    )
    if preview["status"] != "ready":
        raise RuntimeError("computer_workflow_preview_blocked:" + ",".join(preview["blockers"]))
    provider = value_provider or (
        lambda reference: getpass.getpass(f"Private value for {reference} (not stored): ")
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = workflow_path.parent / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    started_at = _now()
    started = time.perf_counter()
    outcomes: list[dict[str, object]] = []
    status = "completed"
    error_code: str | None = None
    active_step: int | None = None
    failure_stage: str | None = None
    try:
        if capture_evidence:
            failure_stage = "before_evidence"
            before_window = desktop.find_window(payload["steps"][0]["window"])
            if before_window is None:
                raise RuntimeError("workflow_window_changed_before_evidence")
            desktop.screenshot(run_root / "before.png", before_window)
        for step in payload["steps"]:
            active_step = int(step["id"])
            failure_stage = "step_delay"
            delay = int(step["delayBeforeMs"])
            if delay:
                time.sleep(min(delay, 5_000) / 1000)
            failure_stage = "window_resolution"
            window = desktop.find_window(step["window"])
            if window is None:
                raise RuntimeError(f"step_{step['id']}_window_changed")
            action = str(step["action"])
            failure_stage = f"{action}_action"
            if action == "click":
                detail = desktop.click(window, step["target"], step["relativePoint"])
            elif action == "key":
                detail = desktop.keypress(window, str(step["key"]))
            else:
                value = provider(str(step["valueRef"]))
                detail = desktop.type_text(window, value)
            outcomes.append({
                "step": step["id"], "action": action, "status": "completed",
                "detail": detail,
            })
            if capture_evidence:
                failure_stage = "step_evidence"
                observed = desktop.screenshot(
                    run_root / f"step-{int(step['id']):03d}.png", window,
                )
                outcomes[-1]["screenshotSha256"] = observed["sha256"]
        active_step = None
        failure_stage = "final_window_postcondition"
        expected = payload.get("expectedFinalWindowTitleContains")
        final_window = desktop.find_window(payload["steps"][-1]["window"])
        if expected and (
            final_window is None
            or str(expected).casefold() not in str(final_window["title"]).casefold()
        ):
            raise RuntimeError("final_window_postcondition_failed")
        expected_control = payload.get("expectedFinalControlTextContains")
        if expected_control:
            failure_stage = "final_control_postcondition"
            if final_window is None:
                raise RuntimeError("final_control_postcondition_failed")
            controls = desktop.inspect_controls(final_window, 200)
            if not any(
                str(expected_control).casefold() in str(item.get("name", "")).casefold()
                for item in controls
            ):
                raise RuntimeError("final_control_postcondition_failed")
        failure_stage = None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        status = "halted"
        error_code = "computer_workflow_halted"
    receipt: dict[str, object] = {
        "schema": RUN_SCHEMA, "runId": run_id, "status": status,
        "name": payload["name"], "workflowSha256": payload["workflowSha256"],
        "startedAt": started_at, "wallSeconds": round(time.perf_counter() - started, 3),
        "completedSteps": len(outcomes), "totalSteps": len(payload["steps"]),
        "steps": outcomes, "errorCode": error_code,
        "haltedAtStep": active_step if status == "halted" else None,
        "failureStage": failure_stage if status == "halted" else None,
        "evidenceCaptured": capture_evidence,
        "typedValuesStoredInEventLog": False,
        "screenshotsMayContainVisiblePrivateContent": capture_evidence,
        "modelCalled": False,
        "localComputerActionsPerformed": bool(outcomes),
        "externalActionPerformed": None,
        "externalActionVerification": "not_proven_by_ui_replay",
        "externalActionBoundary": "known network and shell apps are blocked unless explicitly enabled",
        "outcomeVerified": (
            status == "completed"
            and bool(
                payload.get("expectedFinalWindowTitleContains")
                or payload.get("expectedFinalControlTextContains")
            )
        ),
        "outcomeVerifier": {
            "windowTitleAssertionConfigured": bool(
                payload.get("expectedFinalWindowTitleContains")
            ),
            "controlTextAssertionConfigured": bool(
                payload.get("expectedFinalControlTextContains")
            ),
        },
        "receiptSha256Sidecar": "receipt.sha256",
    }
    receipt_path = run_root / "receipt.json"
    _atomic_json(receipt_path, receipt)
    receipt_sha256 = _file_digest(receipt_path)
    _atomic_text(
        run_root / "receipt.sha256",
        f"{receipt_sha256} *{receipt_path.name}\n",
    )
    receipt["receiptPath"] = str(receipt_path)
    receipt["receiptSha256"] = receipt_sha256
    pilot_path = workflow_path.parent / "pilot" / "pilot.json"
    receipt["pilotRunAnchorRequired"] = pilot_path.is_file()
    receipt["pilotRunAnchorStatus"] = "not_required"
    if pilot_path.is_file():
        try:
            from .workflow_pilot import anchor_workflow_pilot_run

            anchor = anchor_workflow_pilot_run(company_home, name, run_id)
            receipt["pilotRunAnchorStatus"] = "anchored"
            receipt["pilotRunAnchorSha256"] = anchor["anchorSha256"]
        except (OSError, UnicodeError, ValueError) as error:
            receipt["pilotRunAnchorStatus"] = "failed"
            receipt["pilotRunAnchorError"] = str(error)[:200]
    return receipt
