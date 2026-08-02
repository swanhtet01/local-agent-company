from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.run_local_code_agent import main as run_agent_main
from scripts.select_local_code_model import GIB, main, select_model


class LocalCodeModelSelectionTests(unittest.TestCase):
    def test_prefers_quality_model_only_when_installed_and_memory_admitted(self) -> None:
        selected = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 6 * GIB)
        self.assertEqual(selected.model, "qwen3.5:4b")
        self.assertEqual(selected.reason, "quality_model_admitted")
        constrained = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 2 * GIB)
        self.assertEqual(constrained.model, "qwen3.5:0.8b")
        self.assertEqual(constrained.reason, "bootstrap_model_admitted")

    def test_explicit_model_request_still_obeys_installation_and_memory(self) -> None:
        selected = select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 6 * GIB, "qwen3.5:4b")
        self.assertEqual(selected.reason, "explicit_request_admitted")
        with self.assertRaisesRegex(ValueError, "requested_model_memory_blocked"):
            select_model({"qwen3.5:4b"}, 4 * GIB, "qwen3.5:4b")
        with self.assertRaisesRegex(ValueError, "requested_model_not_installed"):
            select_model({"qwen3.5:0.8b"}, 6 * GIB, "qwen3.5:4b")
        with self.assertRaisesRegex(ValueError, "requested_model_unsupported"):
            select_model({"other:latest"}, 20 * GIB, "other:latest")

    def test_fails_when_no_installed_model_fits_instead_of_overcommitting(self) -> None:
        with self.assertRaisesRegex(ValueError, "installed_models_memory_blocked"):
            select_model({"qwen3.5:4b", "qwen3.5:0.8b"}, 2 * GIB - 1)
        with self.assertRaisesRegex(ValueError, "supported_model_not_installed"):
            select_model(set(), 20 * GIB)

    def test_cli_receipt_is_read_only_and_model_only_output_is_exact(self) -> None:
        with patch("scripts.select_local_code_model.installed_ollama_models", return_value={"qwen3.5:0.8b"}), patch(
            "scripts.select_local_code_model.available_memory_bytes", return_value=2 * GIB,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([]), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["model"], "qwen3.5:0.8b")
            self.assertFalse(receipt["controls"]["modelLoaded"])
            self.assertFalse(receipt["controls"]["externalRequestPerformed"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--model-only"]), 0)
            self.assertEqual(output.getvalue(), "qwen3.5:0.8b\n")

    def test_cli_blocker_is_bounded_and_nonzero(self) -> None:
        with patch("scripts.select_local_code_model.installed_ollama_models", return_value={"qwen3.5:4b"}), patch(
            "scripts.select_local_code_model.available_memory_bytes", return_value=2 * GIB,
        ):
            error = io.StringIO()
            with redirect_stderr(error):
                self.assertEqual(main(["--requested-model", "qwen3.5:4b"]), 1)
            receipt = json.loads(error.getvalue())
            self.assertEqual(receipt["reason"], "requested_model_memory_blocked")
            self.assertEqual(receipt["availableMemoryBytes"], 2 * GIB)
            self.assertEqual(receipt["minimumAvailableBytes"], 5 * GIB)
            self.assertEqual(receipt["memoryShortfallBytes"], 3 * GIB)
            self.assertEqual(receipt["recommendedAction"], "close_large_apps_then_rerun_check")
            self.assertFalse(receipt["controls"]["modelLoaded"])

    def test_windows_launcher_routes_headless_mode_without_stale_errorlevel(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-code.cmd").read_text(
            encoding="utf-8",
        )
        self.assertIn('if /I "%~1"=="--run" goto RUN_HEADLESS', source)
        self.assertIn(':RUN_HEADLESS', source)
        self.assertIn('exit /b %ERRORLEVEL%', source)

    def test_windows_launcher_routes_low_memory_vision_campaign_agent(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-code.cmd").read_text(
            encoding="utf-8",
        )
        self.assertIn('if /I "%~1"=="--vision-lite" (', source)
        self.assertIn('set "OPENCODE_AGENT=vision-campaign"', source)
        self.assertIn('call "%OPENCODE_EXE%" . --model ollama/%LOCAL_MODEL% --agent "%OPENCODE_AGENT%"', source)

    def test_windows_launchers_route_dedicated_governed_company_agent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        code = (root / "local-code.cmd").read_text(encoding="utf-8")
        company = (root / "local-company-agent.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="--company" (', code)
        self.assertIn('set "OPENCODE_AGENT=local-company"', code)
        self.assertIn("get('local_company',{}).get('enabled') is True", company)
        self.assertIn("get('local_company_*') is True", company)
        self.assertIn('call "%COMPANY_ROOT%local-code.cmd" --company --check "%COMPANY_ROOT%"', company)
        self.assertIn("if errorlevel 1 exit /b 3", company)
        self.assertIn('call "%COMPANY_ROOT%local-code.cmd" --company "%COMPANY_ROOT%"', company)
        self.assertNotIn("--model openai/", company.lower())

    @staticmethod
    def _fixture(directory: str) -> tuple[Path, Path]:
        root = Path(directory) / "repo"
        root.mkdir()
        task = root / "TASK.md"
        test = root / "test_contract.py"
        task.write_text("Implement one local contract.\n", encoding="utf-8")
        test.write_text("print('tests pass')\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@invalid.example"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
        return root, test

    def test_headless_runner_rejects_zero_exit_when_agent_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, protected = self._fixture(directory)
            opencode = Path(directory) / "opencode.exe"
            opencode.write_bytes(b"fixture")
            output = io.StringIO()
            with patch(
                "scripts.run_local_code_agent.installed_ollama_models",
                return_value={"qwen3.5:0.8b"},
            ), patch(
                "scripts.run_local_code_agent.available_memory_bytes", return_value=3 * GIB,
            ), patch(
                "scripts.run_local_code_agent._run_agent",
                return_value=(0, "agent says done", "", 2 * GIB),
            ), patch(
                "scripts.run_local_code_agent.shutil.which", return_value=None,
            ), patch(
                "scripts.run_local_code_agent._unload_model", return_value=True,
            ), redirect_stdout(output):
                code = run_agent_main([
                    str(root), "TASK.md", "--protect", protected.name,
                    "--opencode", str(opencode), "--test", "python", "-c", "print('ok')",
                ])
            self.assertEqual(code, 1)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(receipt["reason"], "no_file_change")
            self.assertTrue(receipt["testsPassed"])
            self.assertEqual(receipt["changedFiles"], [])

    def test_headless_runner_accepts_only_changed_code_current_protection_and_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, protected = self._fixture(directory)
            opencode = Path(directory) / "opencode.exe"
            opencode.write_bytes(b"fixture")

            def change_code(*_args):
                (root / "implementation.py").write_text("VALUE = 1\n", encoding="utf-8")
                return 0, "implemented", "", 2 * GIB

            output = io.StringIO()
            with patch(
                "scripts.run_local_code_agent.installed_ollama_models",
                return_value={"qwen3.5:0.8b"},
            ), patch(
                "scripts.run_local_code_agent.available_memory_bytes", return_value=3 * GIB,
            ), patch(
                "scripts.run_local_code_agent._run_agent", side_effect=change_code,
            ), patch(
                "scripts.run_local_code_agent.shutil.which", return_value=None,
            ), patch(
                "scripts.run_local_code_agent._unload_model", return_value=True,
            ), redirect_stdout(output):
                code = run_agent_main([
                    str(root), "TASK.md", "--protect", protected.name,
                    "--opencode", str(opencode), "--test", "python", "-c", "print('ok')",
                ])
            self.assertEqual(code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(receipt["changedFiles"], ["implementation.py"])
            self.assertTrue(receipt["protectedFilesCurrent"])
            self.assertTrue(receipt["testsPassed"])


if __name__ == "__main__":
    unittest.main()
