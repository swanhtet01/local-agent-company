from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.computer_use import (
    RUN_CONFIRMATION,
    WORKFLOW_SCHEMA,
    WindowsDesktopAdapter,
    _atomic_json,
    list_workflows,
    load_workflow,
    preview_workflow,
    prove_computer_use,
    run_workflow,
    seal_workflow,
    validate_workflow,
    workflow_root,
)
from scripts.local_ai import explain, translate


def workflow_payload(name: str = "invoice-entry") -> dict[str, object]:
    window = {
        "title": "SuperMega Workflow Lab",
        "className": "LabWindow",
        "processName": "lab.exe",
        "recordedBounds": [100, 100, 900, 700],
    }
    return seal_workflow({
        "schema": WORKFLOW_SCHEMA,
        "name": name,
        "createdAt": "2026-08-04T00:00:00+00:00",
        "platform": "windows",
        "learning": {
            "stopReason": "f8_pressed",
            "durationSeconds": 4.2,
            "typedCharactersStored": False,
            "screenshotsCaptured": True,
            "stopKey": "F8",
        },
        "steps": [
            {
                "id": 1,
                "action": "click",
                "delayBeforeMs": 0,
                "window": window,
                "target": {
                    "name": "Run",
                    "automationId": "run-button",
                    "controlType": "ControlType.Button",
                    "className": "Button",
                    "recordedBounds": [200, 200, 300, 240],
                },
                "relativePoint": [0.2, 0.25],
            },
            {
                "id": 2,
                "action": "key",
                "delayBeforeMs": 0,
                "window": window,
                "key": "TAB",
            },
            {
                "id": 3,
                "action": "text",
                "delayBeforeMs": 0,
                "window": window,
                "valueRef": "TEXT_1",
            },
        ],
        "expectedFinalWindowTitleContains": "Workflow Lab",
        "expectedFinalControlTextContains": "Verified locally:",
    })


def store_workflow(home: Path, payload: dict[str, object]) -> Path:
    path = workflow_root(home, create=True) / str(payload["name"]) / "workflow.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeDesktop:
    def __init__(self, *, title: str = "SuperMega Workflow Lab") -> None:
        self.window = {
            "handle": 44,
            "title": title,
            "className": "LabWindow",
            "processName": "lab.exe",
            "processId": 77,
            "bounds": [120, 120, 920, 720],
        }
        self.actions: list[tuple[str, object]] = []
        self.screenshots: list[Path] = []
        self.screenshot_windows: list[dict[str, object] | None] = []

    def find_window(self, signature):
        return self.window if signature["processName"] == "lab.exe" else None

    def resolve_target(self, window, target):
        if target.get("automationId") == "run-button":
            return {"score": 145, "ties": 1, "bounds": [210, 210, 320, 250]}
        return None

    def click(self, window, target, relative):
        self.actions.append(("click", target["automationId"]))
        return {"method": "uia_anchor", "point": [265, 230]}

    def keypress(self, window, key):
        self.actions.append(("key", key))
        return {"key": key}

    def type_text(self, window, value):
        self.actions.append(("text", value))
        return {"characters": len(value), "inputMode": "replace_focused_value"}

    def screenshot(self, output: Path, window=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"local screenshot")
        self.screenshots.append(output)
        self.screenshot_windows.append(window)
        return {"path": str(output), "sha256": "a" * 64, "bytes": 16}

    def inspect_controls(self, window, limit):
        return [{"name": "Verified locally: private-customer-value"}]

    def foreground_window(self):
        return self.window


class FakeProcess:
    def __init__(self, pid: int = 77) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class ProofDesktop(FakeDesktop):
    def __init__(self) -> None:
        super().__init__()
        self.window.update({
            "title": "SuperMega Workflow Lab",
            "className": "TkTopLevel",
            "processName": "python.exe",
            "processId": 77,
            "bounds": [100, 100, 920, 599],
        })

    def doctor(self):
        return {"schema": "doctor", "status": "ready"}

    def windows(self, limit):
        return [self.window]

    def find_window(self, signature):
        return self.window if signature["processName"] == "python.exe" else None

    def inspect_controls(self, window, limit):
        return [
            {
                "name": "", "automationId": "", "controlType": "ControlType.Pane",
                "className": "TkChild", "enabled": True, "offscreen": False,
                "bounds": [140, 280, 880, 326],
            },
            {
                "name": "", "automationId": "", "controlType": "ControlType.Pane",
                "className": "Button", "enabled": True, "offscreen": False,
                "bounds": [140, 346, 300, 402],
            },
        ]

    def click(self, window, target, relative):
        self.actions.append(("click", target["className"]))
        if target["className"] == "Button":
            self.window["title"] = "SuperMega Workflow Lab - Verified locally"
        return {"method": "uia_anchor", "point": [220, 374]}


class ComputerUseWorkflowTests(unittest.TestCase):
    def test_atomic_json_flushes_and_fsyncs_before_the_rename(self) -> None:
        # _atomic_json backs workflow.json (minutes of a user's desktop
        # demonstration) and receipt.json (re-hashed and permanently bound
        # into the pilot's append-only acceptance ledger). Path.write_text()
        # only hands bytes to the OS write cache; os.replace() can report
        # success while the data is still only cached in memory, so a
        # crash/power-loss shortly after can revert the file to stale or
        # absent content. Every other durability-critical writer in this
        # codebase explicitly flushes and fsyncs before its rename -- this
        # one didn't.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "workflow.json"
            with patch("local_company.computer_use.os.fsync") as fsync:
                _atomic_json(target, {"steps": ["one"]})
            fsync.assert_called_once()
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"steps": ["one"]})

            # A second write (the real-world case: re-recording, or
            # run_workflow overwriting a prior receipt) must also fsync,
            # and must leave no stray temp file behind either way.
            with patch("local_company.computer_use.os.fsync") as fsync:
                _atomic_json(target, {"steps": ["two"]})
            fsync.assert_called_once()
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"steps": ["two"]})
            self.assertEqual(list(Path(tmp).glob(".workflow.json.*.tmp")), [])

    def test_workflow_is_integrity_sealed_and_tampering_fails(self) -> None:
        payload = workflow_payload()
        self.assertEqual(validate_workflow(payload)["workflowSha256"], payload["workflowSha256"])
        tampered = json.loads(json.dumps(payload))
        tampered["steps"][0]["relativePoint"] = [0.9, 0.9]
        with self.assertRaisesRegex(ValueError, "workflow_seal_mismatch"):
            validate_workflow(tampered)

    def test_key_step_rejects_a_single_non_ascii_character(self) -> None:
        # str.isalnum() is Unicode-aware, so a hand-edited-then-resealed
        # workflow.json with e.g. key '日' used to pass this check even
        # though _virtual_key() can only turn A-Z0-9 into a real Win32
        # virtual-key code -- ord('日'.upper()) is 26085, far outside the
        # BYTE range keybd_event expects, with no argtypes declared to catch
        # it. seal_workflow() calls validate_workflow() internally, so this
        # must be caught at the earliest point a tampered file is resealed.
        raw = workflow_payload()
        raw["steps"][1]["key"] = "日"
        with self.assertRaisesRegex(ValueError, "workflow_key_unsupported"):
            seal_workflow(raw)

    def test_preview_resolves_windows_and_uia_without_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            desktop = FakeDesktop()
            result = preview_workflow(home, "invoice-entry", adapter=desktop)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["requiredSecretInputs"], ["TEXT_1"])
            self.assertEqual(result["steps"][0]["resolution"], "uia_anchor")
            self.assertEqual(desktop.actions, [])
            self.assertFalse(result["modelCalled"])
            self.assertFalse(result["stateMutated"])
            self.assertFalse(result["externalActionPerformed"])

    def test_run_requires_confirmation_and_never_stores_private_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            desktop = FakeDesktop()
            with self.assertRaisesRegex(ValueError, "computer_workflow_confirmation_invalid"):
                run_workflow(home, "invoice-entry", "GO", adapter=desktop)
            result = run_workflow(
                home,
                "invoice-entry",
                RUN_CONFIRMATION,
                adapter=desktop,
                value_provider=lambda _reference: "private-customer-value",
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                desktop.actions,
                [("click", "run-button"), ("key", "TAB"), ("text", "private-customer-value")],
            )
            self.assertEqual(result["completedSteps"], 3)
            self.assertTrue(result["outcomeVerified"])
            self.assertEqual(len(result["receiptSha256"]), 64)
            self.assertTrue(
                Path(result["receiptPath"]).with_name("receipt.sha256").is_file()
            )
            self.assertTrue(result["localComputerActionsPerformed"])
            receipt = Path(result["receiptPath"]).read_text(encoding="utf-8")
            workflow = (workflow_root(home) / "invoice-entry" / "workflow.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("private-customer-value", receipt)
            self.assertNotIn("private-customer-value", workflow)
            self.assertEqual(len(desktop.screenshots), 4)
            self.assertTrue(all(window is desktop.window for window in desktop.screenshot_windows))
            self.assertIsNone(result["externalActionPerformed"])
            self.assertEqual(
                result["externalActionVerification"], "not_proven_by_ui_replay",
            )

    def test_run_halts_when_final_postcondition_is_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            desktop = FakeDesktop(title="Unexpected Screen")
            result = run_workflow(
                home,
                "invoice-entry",
                RUN_CONFIRMATION,
                adapter=desktop,
                value_provider=lambda _reference: "value",
                capture_evidence=False,
            )
            self.assertEqual(result["status"], "halted")
            self.assertEqual(result["errorCode"], "computer_workflow_halted")
            self.assertEqual(result["completedSteps"], 3)
            self.assertEqual(result["failureStage"], "final_window_postcondition")
            self.assertIsNone(result["haltedAtStep"])
            # The generic error_code alone doesn't say WHY it halted; the
            # exception's own message is the only thing that does.
            self.assertEqual(result["errorMessage"], "final_window_postcondition_failed")

    def test_network_and_shell_apps_are_blocked_by_default(self) -> None:
        for process_name, flag in (("chrome.exe", "network"), ("powershell.exe", "shell")):
            with self.subTest(process_name=process_name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                payload = workflow_payload()
                for step in payload["steps"]:
                    step["window"]["processName"] = process_name
                payload = seal_workflow(payload)
                store_workflow(home, payload)
                desktop = FakeDesktop()
                desktop.window["processName"] = process_name
                desktop.find_window = lambda _signature: desktop.window
                result = preview_workflow(home, "invoice-entry", adapter=desktop)
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(any(flag in blocker for blocker in result["blockers"]))

    def test_one_command_proof_controls_a_closed_local_lab_and_retains_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            desktop = ProofDesktop()
            process = FakeProcess()
            result = prove_computer_use(
                home,
                RUN_CONFIRMATION,
                adapter=desktop,
                process_launcher=lambda: process,
            )
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["labClosedAfterProof"])
            self.assertEqual(process.returncode, 0)
            self.assertEqual(result["run"]["completedSteps"], 3)
            self.assertEqual(
                desktop.actions,
                [
                    ("click", "TkChild"),
                    ("text", "LOCAL COMPUTER USE WORKS"),
                    ("click", "Button"),
                ],
            )
            self.assertTrue(Path(result["evidencePath"]).is_file())
            receipt = Path(result["run"]["receiptPath"]).read_text(encoding="utf-8")
            self.assertNotIn("LOCAL COMPUTER USE WORKS", receipt)
            self.assertFalse(result["externalActionPerformed"])

    def test_list_reports_ready_and_invalid_workflows_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store_workflow(home, workflow_payload())
            invalid = workflow_root(home) / "broken" / "workflow.json"
            invalid.parent.mkdir()
            invalid.write_text("not json", encoding="utf-8")
            result = list_workflows(home)
            self.assertEqual(result["workflowCount"], 2)
            self.assertEqual(
                {item["name"]: item["status"] for item in result["workflows"]},
                {"broken": "invalid", "invoice-entry": "ready"},
            )
            path, loaded = load_workflow(home, "invoice-entry")
            self.assertTrue(path.is_file())
            self.assertEqual(loaded["name"], "invoice-entry")

    def test_friendly_launcher_exposes_one_automate_entrypoint(self) -> None:
        doctor = translate(["automate"])
        self.assertEqual(doctor.command, ("computer", "doctor"))
        self.assertFalse(explain(doctor)["effects"]["localStateMayChange"])
        learned = translate(["automate", "learn", "invoice-entry", "--seconds", "30"])
        self.assertEqual(
            learned.command,
            ("computer", "learn", "invoice-entry", "--seconds", "30"),
        )
        self.assertTrue(explain(learned)["effects"]["localStateMayChange"])
        proof = translate([
            "automate", "prove", "--confirm", RUN_CONFIRMATION,
        ])
        self.assertEqual(
            proof.command,
            ("computer", "prove", "--confirm", RUN_CONFIRMATION),
        )
        self.assertTrue(explain(proof)["effects"]["localStateMayChange"])
        with self.assertRaisesRegex(ValueError, "automate_operation_unknown"):
            translate(["automate", "guess"])

    def test_click_keypress_type_text_fail_closed_off_windows(self) -> None:
        # These three don't call self._require_windows() before touching
        # ctypes.windll/wintypes -- unlike windows(), foreground_window(),
        # and screenshot(), which do. Today every real caller reaches them
        # only after find_window() already guarded, so this is currently
        # masked rather than exploitable; a future caller that skips that
        # step would hit an unhandled AttributeError on non-Windows instead
        # of the documented computer_use_requires_windows failure.
        adapter = WindowsDesktopAdapter()
        adapter.is_windows = False
        window = {
            "title": "Any", "className": "Any", "processName": "any.exe",
            "recordedBounds": [0, 0, 100, 100], "handle": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "computer_use_requires_windows"):
            adapter.click(window, {}, [0.5, 0.5])
        with self.assertRaisesRegex(RuntimeError, "computer_use_requires_windows"):
            adapter.keypress(window, "ENTER")
        with self.assertRaisesRegex(RuntimeError, "computer_use_requires_windows"):
            adapter.type_text(window, "value")


class NonWindowsImportTests(unittest.TestCase):
    """The coordinator must survive on a host without ctypes.wintypes.

    cli.py and workflow_pilot import computer_use unconditionally, and
    mcp_server runs missions by shelling out to `python -m local_company.cli`,
    so an import-time failure here takes the whole company down on Linux rather
    than just the Windows desktop workcell.
    """

    def test_core_modules_import_without_wintypes(self) -> None:
        program = textwrap.dedent(
            """
            import sys, importlib.abc

            class BlockWintypes(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "ctypes.wintypes":
                        # What CPython raises on Linux: wintypes declares
                        # VARIANT_BOOL with the Windows-only "v" format code.
                        raise ValueError("_type_ 'v' not supported")
                    return None

            sys.meta_path.insert(0, BlockWintypes())
            import local_company.computer_use as computer_use
            import local_company.cli
            import local_company.workflow_pilot
            import local_company.mcp_server
            assert computer_use.wintypes is None
            assert callable(local_company.cli.main)
            print("ok")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"import failed without wintypes:\n{completed.stderr}",
        )
        self.assertIn("ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
