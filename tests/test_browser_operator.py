from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.browser_operator import (
    browser_doctor,
    run_browser_check,
    run_supermega_release_suite,
)
from local_company.cli import parser


class FakeAgentBrowser:
    def __init__(
        self, *, fail_command: str | None = None, page_errors: bool = False,
        version: str = "agent-browser 0.33.2",
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.batches: list[list[list[str]]] = []
        self.fail_command = fail_command
        self.page_errors = page_errors
        self.version = version
        self.current_url = "about:blank"

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        environment = dict(kwargs.get("env", {}))
        self.calls.append((command, environment))
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, self.version + "\n", "")
        if "batch" in command:
            items = []
            exit_code = 0
            batch = json.loads(kwargs["input"])
            self.batches.append(batch)
            for arguments in batch:
                if arguments[0] == self.fail_command:
                    items.append({
                        "command": arguments, "success": False,
                        "result": None, "error": "synthetic failure",
                    })
                    exit_code = 1
                    break
                items.append({
                    "command": arguments, "success": True,
                    "result": self._result(arguments), "error": None,
                })
            return subprocess.CompletedProcess(command, exit_code, json.dumps(items), "")
        arguments = command[command.index("--json") + 1:]
        operation = arguments[0]
        if operation == self.fail_command:
            return subprocess.CompletedProcess(
                command, 1,
                json.dumps({"success": False, "data": None, "error": "synthetic failure"}),
                "",
            )
        data = self._result(arguments)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"success": True, "data": data, "error": None}), "",
        )

    def _result(self, arguments: list[str]) -> dict[str, object]:
        operation = arguments[0]
        if operation == "open":
            self.current_url = arguments[1]
            data = {"title": "SuperMega - Local Business Tools", "url": self.current_url}
        elif operation == "wait":
            data = {"state": "domcontentloaded"}
        elif operation == "get" and arguments[1] == "title":
            data = {"title": "SuperMega - Local Business Tools"}
        elif operation == "get" and arguments[1] == "url":
            data = {"url": self.current_url}
        elif operation == "get" and arguments[1:3] == ["text", "body"]:
            data = {"text": "SuperMega builds useful local business software."}
        elif operation == "snapshot":
            data = {"snapshot": '- heading "SuperMega" [level=1, ref=e1]'}
        elif operation == "network":
            data = {"requests": [{
                "method": "GET", "resourceType": "Document", "status": 200,
                "mimeType": "text/html", "url": self.current_url,
                "headers": {"Authorization": "must-not-be-saved"},
            }]}
        elif operation == "errors":
            data = {"errors": ["page exploded"] if self.page_errors else []}
        elif operation == "console":
            data = {"messages": [{"type": "warning", "text": "synthetic warning"}]}
        elif operation == "a11y":
            data = {
                "axeVersion": "4.12.1", "url": self.current_url,
                "counts": {"passes": 12, "incomplete": 0, "violations": 0},
                "violations": [],
            }
        elif operation == "vitals":
            data = {
                "url": self.current_url, "ttfb": 41.2, "fcp": 125.0,
                "lcp": {"startTime": 180.0}, "cls": {"score": 0.01}, "inp": None,
                "lifecycle": {"must": "not be stored"},
            }
        elif operation == "screenshot":
            path = Path(arguments[1])
            path.write_bytes(b"synthetic png")
            data = {"path": str(path)}
        elif operation == "close":
            data = {"closed": True}
        else:
            raise AssertionError(f"Unexpected command: {arguments}")
        return data


def fake_runtime(root: Path) -> tuple[Path, Path]:
    tool = (
        root / ".local-company-tools" / "node_modules" / "agent-browser" / "bin"
        / "agent-browser-win32-x64.exe"
    )
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"tool")
    browser = root / "browser.exe"
    browser.write_bytes(b"browser")
    return tool, browser


class BrowserOperatorTests(unittest.TestCase):
    def test_check_writes_hashed_pass_receipt_without_model_or_secret_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tool, browser = fake_runtime(root)
            runner = FakeAgentBrowser()
            target = "https://example.test/dashboard?token=secret#private"
            with patch.dict(
                "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)}, clear=False,
            ):
                result = run_browser_check(
                    root / "company", target,
                    expected_title="SuperMega", expected_text=["business software"],
                    max_a11y_violations=0, repository_root=root, runner=runner,
                )

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["externalReadPerformed"])
            self.assertFalse(result["externalWritePerformed"])
            self.assertFalse(result["modelCalled"])
            self.assertFalse(result["paidApiUsed"])
            self.assertTrue(result["isolatedBrowserConfigUsed"])
            self.assertNotIn("secret", result["targetUrl"])
            receipt = Path(result["receiptPath"])
            self.assertTrue(receipt.is_file())
            self.assertEqual(len(result["evidence"]), 8)
            for item in result["evidence"]:
                self.assertEqual(len(item["sha256"]), 64)
                self.assertGreater(item["bytes"], 0)
            stored = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in receipt.parent.iterdir() if path.suffix in {".json", ".txt"}
            )
            self.assertNotIn("must-not-be-saved", stored)
            self.assertNotIn("token=secret", stored)
            browser_calls = [call for call in runner.calls if "batch" in call[0]]
            sessions = {call[1]["AGENT_BROWSER_SESSION"] for call in browser_calls}
            executables = {call[1]["AGENT_BROWSER_EXECUTABLE_PATH"] for call in browser_calls}
            self.assertEqual(len(sessions), 1)
            self.assertEqual(executables, {str(browser.resolve())})
            configs = {call[1]["AGENT_BROWSER_CONFIG"] for call in browser_calls}
            self.assertEqual(len(configs), 1)
            self.assertFalse(Path(next(iter(configs))).exists())
            self.assertNotIn("AI_GATEWAY_API_KEY", browser_calls[0][1])
            self.assertEqual(runner.batches[-1][-1], ["close"])

    def test_page_error_is_a_failed_check_not_a_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tool, browser = fake_runtime(root)
            with patch.dict(
                "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)}, clear=False,
            ):
                result = run_browser_check(
                    root / "company", "https://example.test",
                    repository_root=root, runner=FakeAgentBrowser(page_errors=True),
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["pageErrorCount"], 1)
            check = next(item for item in result["checks"] if item["name"] == "no_page_errors")
            self.assertFalse(check["passed"])

    def test_tool_failure_halts_and_still_writes_a_receipt_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tool, browser = fake_runtime(root)
            runner = FakeAgentBrowser(fail_command="snapshot")
            with patch.dict(
                "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)}, clear=False,
            ):
                result = run_browser_check(
                    root / "company", "https://example.test",
                    repository_root=root, runner=runner,
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["haltStage"], "browser_batch")
            self.assertTrue(Path(result["receiptPath"]).is_file())
            self.assertEqual(runner.calls[-1][0][-1], "close")

    def test_doctor_performs_a_live_about_blank_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tool, browser = fake_runtime(root)
            runner = FakeAgentBrowser()
            with patch.dict(
                "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)}, clear=False,
            ):
                result = browser_doctor(repository_root=root, runner=runner)
            self.assertEqual(result["status"], "ready")
            self.assertTrue(result["liveLaunchPassed"])
            self.assertEqual(runner.batches[-1][0], ["open", "about:blank"])
            self.assertEqual(runner.batches[-1][-1], ["close"])

    def test_doctor_rejects_an_unpinned_agent_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _tool, browser = fake_runtime(root)
            runner = FakeAgentBrowser(version="agent-browser 9.9.9")
            with patch.dict(
                "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)}, clear=False,
            ):
                result = browser_doctor(repository_root=root, runner=runner)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "live_launch_failed")
            self.assertIn("version_mismatch", result["detail"])

    def test_cli_contract_is_short_and_validates_limits(self) -> None:
        args = parser().parse_args([
            "browser", "check", "https://supermega.dev",
            "--expect-text", "SuperMega", "--max-a11y-violations", "3",
        ])
        self.assertEqual(args.browser_command, "check")
        self.assertEqual(args.expect_text, ["SuperMega"])
        with self.assertRaisesRegex(ValueError, "browser_url_credentials_forbidden"):
            run_browser_check(Path("unused"), "https://user:secret@example.test")

    def test_supermega_suite_binds_four_child_receipts_and_keeps_ten_run_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls: list[tuple[str, list[str], bool]] = []

            def checker(company_home, url, **kwargs):
                calls.append((url, kwargs["expected_text"], kwargs["fail_on_console_errors"]))
                receipt = root / f"child-{len(calls)}.json"
                receipt.write_text(json.dumps({"call": len(calls)}), encoding="utf-8")
                return {
                    "status": "passed", "documentStatus": 200,
                    "title": "Setup | SuperMega", "pageErrorCount": 0,
                    "consoleErrorCount": 0, "accessibilityViolationCount": 0,
                    "wallSeconds": 1.2, "receiptPath": str(receipt),
                }

            result = run_supermega_release_suite(
                root / "company", runs=2, checker=checker,
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["pageChecks"], 8)
            self.assertEqual(result["passedPageChecks"], 8)
            self.assertEqual(result["consecutivePassingRuns"], 2)
            self.assertFalse(result["releaseGatePassed"])
            self.assertEqual(result["promotionStatus"], "baseline_only")
            self.assertEqual(calls[0][1], ["Set up Shop", "Open working sample"])
            self.assertEqual(calls[-1][1], ["Set up Ecommerce", "Open working sample"])
            self.assertTrue(all(call[2] for call in calls))
            self.assertTrue(Path(result["receiptPath"]).is_file())
            for run in result["runs"]:
                for page in run["pages"]:
                    self.assertEqual(len(page["receiptSha256"]), 64)


if __name__ == "__main__":
    unittest.main()
