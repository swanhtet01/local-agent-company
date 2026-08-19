from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.browser_operator import (
    browser_doctor,
    create_suite_template,
    discover_agent_browser,
    discover_browser_executable,
    install_browser_operator,
    load_sealed_suite_manifest,
    run_browser_check,
    run_browser_suite,
    run_supermega_release_suite,
    seal_suite_manifest,
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
    def test_discover_agent_browser_finds_a_real_linux_install(self) -> None:
        # Every functional test in this file bypasses discovery entirely via
        # LOCAL_COMPANY_BROWSER_EXECUTABLE/fake_runtime()'s Windows-only
        # fixture, so discovery itself was never actually exercised. A real
        # `npm install agent-browser` on Linux produces
        # bin/agent-browser-linux-x64 plus a POSIX .bin/agent-browser shim,
        # not the Windows .exe/.cmd names this used to look for exclusively.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = (
                root / ".local-company-tools" / "node_modules" / "agent-browser" / "bin"
                / "agent-browser-linux-x64"
            )
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"tool")
            with patch.dict("os.environ", {}, clear=True):
                found = discover_agent_browser(repository_root=root)
            self.assertEqual(found, binary.resolve())

    def test_discover_browser_executable_falls_back_to_path_on_linux(self) -> None:
        # ProgramFiles/ProgramFiles(x86)/LOCALAPPDATA are all Windows-only,
        # so on Linux every literal candidate path is empty and this used to
        # always return None even with a real Chrome/Chromium on PATH.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "google-chrome"
            browser.write_bytes(b"browser")
            with patch.dict(
                "os.environ",
                {"ProgramFiles": "", "ProgramFiles(x86)": "", "LOCALAPPDATA": ""},
                clear=True,
            ), patch(
                "local_company.browser_operator.shutil.which",
                side_effect=lambda name: str(browser) if name == "google-chrome" else None,
            ):
                found = discover_browser_executable()
            self.assertEqual(found, browser.resolve())

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
        suite_args = parser().parse_args([
            "browser", "suite", "customer.json", "--runs", "3",
        ])
        self.assertEqual(suite_args.browser_command, "suite")
        self.assertEqual(suite_args.manifest, Path("customer.json"))
        self.assertEqual(suite_args.runs, 3)
        with self.assertRaisesRegex(ValueError, "browser_url_credentials_forbidden"):
            run_browser_check(Path("unused"), "https://user:secret@example.test")

    def test_supermega_release_check_is_absent_unless_explicitly_enabled(self) -> None:
        # supermega-release checks four fixed app.supermega.dev pages -- the
        # maintainer's own site, not a general capability -- but it used to
        # register as a first-class sibling of the genuinely generic
        # `browser check`/`suite`/`suite-template` commands regardless.
        with patch.dict("os.environ", {}, clear=True):
            built = parser()
            check_args = built.parse_args(["browser", "check", "https://example.test"])
            self.assertEqual(check_args.browser_command, "check")  # sanity: the rest of the group still parses
            with self.assertRaises(SystemExit):
                built.parse_args(["browser", "supermega-release"])
        with patch.dict(
            "os.environ", {"SUPERMEGA_RELEASE_CHECK_ENABLED": "1"}, clear=True,
        ):
            enabled_args = parser().parse_args(["browser", "supermega-release", "--runs", "5"])
        self.assertEqual(enabled_args.browser_command, "supermega-release")
        self.assertEqual(enabled_args.runs, 5)

    def test_template_is_sealed_ready_to_run_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "example-suite.json"
            result = create_suite_template(manifest)
            definition, digest = load_sealed_suite_manifest(manifest)

            self.assertEqual(result["status"], "created")
            self.assertEqual(result["manifestSha256"], digest)
            self.assertEqual(definition["requiredRuns"], 1)
            self.assertEqual(len(definition["pages"]), 1)
            self.assertFalse(result["externalActionPerformed"])
            with self.assertRaises(FileExistsError):
                create_suite_template(manifest)

    def test_edited_manifest_can_be_resealed_to_a_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.json"
            editable = root / "customer.json"
            create_suite_template(original)
            value = json.loads(original.read_text(encoding="utf-8"))
            value.pop("seal")
            value["name"] = "Customer public website"
            value["pages"][0]["expectText"] = ["Example Domain"]
            editable.write_text(json.dumps(value), encoding="utf-8")

            sealed = seal_suite_manifest(editable)
            destination = Path(str(sealed["path"]))
            definition, digest = load_sealed_suite_manifest(destination)
            self.assertEqual(definition["name"], "Customer public website")
            self.assertEqual(sealed["manifestSha256"], digest)
            self.assertNotEqual(destination, editable)
            with self.assertRaises(FileExistsError):
                seal_suite_manifest(editable)

    def test_tampered_seal_fails_before_browser_or_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "tampered.json"
            create_suite_template(manifest)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["pages"][0]["expectText"] = ["changed after approval"]
            manifest.write_text(json.dumps(value), encoding="utf-8")

            def should_not_run(*_args, **_kwargs):
                raise AssertionError("browser opened before manifest integrity check")

            company_home = root / "company"
            with self.assertRaisesRegex(ValueError, "browser_suite_manifest_seal_mismatch"):
                run_browser_suite(company_home, manifest, checker=should_not_run)
            self.assertFalse((company_home / "browser-suites").exists())

    def test_manifest_rejects_more_than_twenty_pages_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "too-many.json"
            pages = [{
                "id": f"page-{index}", "url": f"https://example.com/{index}",
                "expectTitle": "Example", "expectText": [],
                "failOnConsoleErrors": True, "maxA11yViolations": 0,
            } for index in range(21)]
            source.write_text(json.dumps({
                "schema": "supermega.browser-suite-manifest.v1",
                "name": "Too many", "requiredRuns": 1, "pages": pages,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "browser_suite_manifest_page_count_invalid"):
                seal_suite_manifest(source)

            pages.pop()
            pages[0]["surprise"] = True
            source.write_text(json.dumps({
                "schema": "supermega.browser-suite-manifest.v1",
                "name": "Unknown field", "requiredRuns": 1, "pages": pages,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "browser_suite_manifest_page_unknown_field"):
                seal_suite_manifest(source)

    def test_generic_suite_binds_receipts_and_writes_portable_hashed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "customer.json"
            create_suite_template(manifest, required_runs=2)
            calls: list[str] = []

            def checker(_company_home, url, **_kwargs):
                calls.append(url)
                child = root / f"child-{len(calls)}.json"
                child.write_text(json.dumps({"call": len(calls)}), encoding="utf-8")
                return {
                    "status": "passed", "documentStatus": 200,
                    "title": "Example Domain", "pageErrorCount": 0,
                    "consoleErrorCount": 0, "accessibilityViolationCount": 2,
                    "wallSeconds": 0.2, "receiptPath": str(child),
                    "checks": [{"name": "expected_text_1", "passed": True}],
                    "evidence": [{"file": "page.png", "sha256": "a" * 64}],
                }

            result = run_browser_suite(
                root / "company", manifest, runs=2, checker=checker,
            )
            suite_dir = Path(str(result["suiteDirectory"]))
            portable_path = suite_dir / "portable-summary.json"
            portable_text = portable_path.read_text(encoding="utf-8")
            portable = json.loads(portable_text)

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["releaseGatePassed"])
            self.assertEqual(result["pageChecks"], 2)
            self.assertEqual(len(calls), 2)
            self.assertNotIn(str(root), portable_text)
            self.assertNotIn("receiptPath", portable_text)
            self.assertEqual(portable["manifestSha256"], result["manifest"]["sha256"])
            self.assertEqual(portable["runs"][0]["pages"][0]["screenshotSha256"], "a" * 64)
            self.assertTrue((suite_dir / "portable-summary.sha256").is_file())
            self.assertTrue((suite_dir / "suite-receipt.sha256").is_file())

    def test_generic_suite_never_promotes_a_failed_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "customer.json"
            create_suite_template(manifest)

            def checker(_company_home, _url, **_kwargs):
                child = root / "failed-child.json"
                child.write_text("{}", encoding="utf-8")
                return {
                    "status": "failed", "documentStatus": 200,
                    "title": "Example Domain", "pageErrorCount": 0,
                    "consoleErrorCount": 0, "accessibilityViolationCount": 2,
                    "wallSeconds": 0.2, "receiptPath": str(child),
                    "checks": [{"name": "expected_text_1", "passed": False}],
                    "evidence": [],
                }

            result = run_browser_suite(root / "company", manifest, checker=checker)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["releaseGatePassed"])
            self.assertEqual(result["failedPageChecks"], 1)
            self.assertEqual(
                result["runs"][0]["pages"][0]["failedCheckNames"],
                ["expected_text_1"],
            )

    def test_installer_can_bootstrap_the_pinned_cli_without_downloading_a_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = root / "browser.exe"
            browser.write_bytes(b"browser")
            fake = FakeAgentBrowser()
            install_commands: list[list[str]] = []

            def runner(command, **kwargs):
                values = [str(item) for item in command]
                if "install" in values:
                    install_commands.append(values)
                    tool = (
                        root / ".local-company-tools" / "node_modules" /
                        "agent-browser" / "bin" / "agent-browser-win32-x64.exe"
                    )
                    tool.parent.mkdir(parents=True)
                    tool.write_bytes(b"tool")
                    return subprocess.CompletedProcess(values, 0, "installed", "")
                return fake(values, **kwargs)

            def which(name: str):
                return "C:\\fake\\npm.cmd" if name in {"npm.cmd", "npm"} else None

            with (
                patch.dict(
                    "os.environ", {"LOCAL_COMPANY_BROWSER_EXECUTABLE": str(browser)},
                    clear=False,
                ),
                patch("local_company.browser_operator.shutil.which", side_effect=which),
            ):
                result = install_browser_operator(repository_root=root, runner=runner)

            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["reason"], "installed_and_ready")
            self.assertTrue(result["installAttempted"])
            self.assertTrue(result["networkAccessAttempted"])
            self.assertFalse(result["browserDownloadAttempted"])
            self.assertEqual(len(install_commands), 1)
            self.assertIn("agent-browser@0.33.2", install_commands[0])
            self.assertIn("--ignore-scripts", install_commands[0])

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
