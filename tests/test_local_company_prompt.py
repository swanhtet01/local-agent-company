from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.run_local_company_prompt import EXPECTED_TOOL, main, parse_events
from scripts.select_local_code_model import GIB


def event_stream(*, action: str = "status", response: str = "Company ready.", cost: float = 0) -> str:
    events = [
        {
            "type": "tool_use",
            "part": {
                "tool": EXPECTED_TOOL,
                "state": {
                    "status": "completed", "input": {"action": action},
                    "output": json.dumps({"status": "ready", "externalActionPerformed": False}),
                },
            },
        },
        {"type": "text", "part": {"text": response}},
        {"type": "step_finish", "part": {"cost": cost}},
    ]
    return "\n".join(json.dumps(item) for item in events)


class LocalCompanyPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        # --requested-model defaults to the LOCAL_CODE_MODEL environment
        # variable, so an operator machine that sets it would otherwise steer
        # these tests down the explicit-request branch and change the receipt
        # they assert on. Model admission is supplied by the mocks below.
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LOCAL_CODE_MODEL", None)

    def test_windows_launcher_exposes_bounded_headless_mode(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "local-company-agent.cmd"
        ).read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="--run" (', source)
        self.assertIn('python "%COMPANY_ROOT%scripts\\run_local_company_prompt.py" %*', source)
        self.assertIn("if errorlevel 2 exit /b 2", source)
        self.assertIn("if errorlevel 1 exit /b 1", source)

    def test_event_parser_requires_governed_tool_evidence(self) -> None:
        parsed = parse_events(event_stream(action="projects"))
        self.assertEqual(parsed["actions"], ["projects"])
        self.assertEqual(parsed["response"], "Company ready.")
        self.assertEqual(parsed["cost"], 0)
        self.assertFalse(parsed["externalActionPerformed"])
        self.assertFalse(parsed["toolError"])
        wrong = json.dumps({
            "type": "tool_use", "part": {
                "tool": "bash", "state": {"status": "completed", "input": {}, "output": "{}"},
            },
        })
        self.assertTrue(parse_events(wrong)["toolError"])

    def test_runner_accepts_only_zero_cost_tool_backed_response_and_unload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            opencode = Path(directory) / "opencode.cmd"
            opencode.write_text("@exit /b 0\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, event_stream(), "")
            output = io.StringIO()
            with patch(
                "scripts.run_local_company_prompt.installed_ollama_models",
                return_value={"llama3.2:1b"},
            ), patch(
                "scripts.run_local_company_prompt.available_memory_bytes", return_value=3 * GIB,
            ), patch(
                "scripts.run_local_company_prompt._invoke_agent",
                return_value=(completed, 2 * GIB),
            ), patch(
                "scripts.run_local_company_prompt._unload_model", return_value=True,
            ), patch(
                "scripts.run_local_company_prompt.shutil.which", return_value="ollama",
            ), redirect_stdout(output):
                code = main(["--opencode", str(opencode), "Show", "company", "status"])
            self.assertEqual(code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(receipt["toolActions"], ["status"])
            self.assertEqual(receipt["observedCost"], 0)
            self.assertTrue(receipt["modelUnloadedAfterRun"])
            self.assertFalse(receipt["externalActionPerformed"])
            self.assertEqual(receipt["admissionAvailableBytes"], 3 * GIB)
            self.assertEqual(receipt["minimumAvailableBytesObserved"], 2 * GIB)
            self.assertEqual(receipt["peakIncrementalMemoryBytes"], GIB)
            self.assertEqual(receipt["peakIncrementalMemoryMb"], 1024.0)

    def test_paid_or_tool_free_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            opencode = Path(directory) / "opencode.cmd"
            opencode.write_text("@exit /b 0\n", encoding="utf-8")
            for stdout, reason in (
                (event_stream(cost=0.01), "paid_cost_observed"),
                (json.dumps({"type": "text", "part": {"text": "Guess"}}), "company_tool_not_used"),
            ):
                output = io.StringIO()
                with patch(
                    "scripts.run_local_company_prompt.installed_ollama_models",
                    return_value={"llama3.2:1b"},
                ), patch(
                    "scripts.run_local_company_prompt.available_memory_bytes", return_value=3 * GIB,
                ), patch(
                    "scripts.run_local_company_prompt._invoke_agent",
                    return_value=(subprocess.CompletedProcess([], 0, stdout, ""), 2 * GIB),
                ), patch(
                    "scripts.run_local_company_prompt._unload_model", return_value=True,
                ), redirect_stdout(output):
                    code = main(["--opencode", str(opencode), "Inspect status"])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue())["reason"], reason)

    def test_memory_block_reports_admission_and_exact_shortfall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            opencode = Path(directory) / "opencode.cmd"
            opencode.write_text("@exit /b 0\n", encoding="utf-8")
            error = io.StringIO()
            with patch(
                "scripts.run_local_company_prompt.installed_ollama_models",
                return_value={"llama3.2:1b"},
            ), patch(
                "scripts.run_local_company_prompt.available_memory_bytes",
                return_value=GIB,
            ), redirect_stderr(error):
                code = main(["--opencode", str(opencode), "Inspect status"])
            self.assertEqual(code, 2)
            receipt = json.loads(error.getvalue())
            self.assertEqual(receipt["reason"], "installed_models_memory_blocked")
            self.assertEqual(receipt["admissionAvailableBytes"], GIB)
            self.assertEqual(receipt["minimumAvailableBytes"], 5 * GIB // 2)
            self.assertEqual(receipt["memoryShortfallBytes"], 3 * GIB // 2)
            self.assertFalse(receipt["modelCalled"])


if __name__ == "__main__":
    unittest.main()
