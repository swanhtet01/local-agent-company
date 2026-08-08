from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.run_lmstudio_code import GIB, MODEL_IDENTIFIER, _validate_opencode_config, main


class LmStudioLocalCodeTests(unittest.TestCase):
    def test_windows_launcher_blocks_lmstudio_and_routes_vision_to_ollama(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "local-code.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="--lmstudio" (', source)
        self.assertIn('LM Studio is disabled by the Llama-only Ollama policy', source)
        self.assertIn('set "OPENCODE_AGENT=vision-product"', source)
        self.assertNotIn(':RUN_VISION', source)
        self.assertNotIn(':RUN_LMSTUDIO', source)

    def _files(self, directory: str) -> tuple[Path, Path, Path, Path]:
        root = Path(directory) / "project"
        root.mkdir()
        lms = Path(directory) / "lms.exe"
        opencode = Path(directory) / "opencode.cmd"
        lms.write_bytes(b"fixture")
        opencode.write_bytes(b"fixture")
        config = Path(directory) / "opencode.json"
        config.write_text(json.dumps({
            "provider": {"lmstudio": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://127.0.0.1:1234/v1"},
                "models": {MODEL_IDENTIFIER: {"name": "fixture"}},
            }},
            "mcp": {"vision_product": {
                "type": "local", "enabled": True, "command": ["vision.cmd", "mcp"],
                "environment": {"SUPERMEGA_VISION_MCP_PROFILE": "product"},
            }},
            "agent": {"vision-product": {
                "mode": "primary",
                "permission": {
                    name: "deny" for name in {
                        "read", "edit", "glob", "grep", "list", "bash", "task",
                        "external_directory", "webfetch", "websearch",
                    }
                },
                "tools": {"vision_product_*": True},
            }},
        }), encoding="utf-8")
        return root, lms, opencode, config

    def test_product_agent_config_is_separate_restricted_and_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _root, _lms, _opencode, config = self._files(directory)
            _validate_opencode_config(config, "vision-product")
            value = json.loads(config.read_text(encoding="utf-8"))
            del value["agent"]["vision-product"]["permission"]["bash"]
            config.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "opencode_vision_product_config_invalid"):
                _validate_opencode_config(config, "vision-product")

    @patch("scripts.run_lmstudio_code._model_installed", return_value=True)
    @patch("scripts.run_lmstudio_code._loaded_models", return_value=[])
    @patch("scripts.run_lmstudio_code._server_status", return_value={"running": False})
    @patch("scripts.run_lmstudio_code._ollama_model_loaded", return_value=False)
    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=6 * GIB)
    def test_check_proves_existing_model_and_starts_nothing(
        self, _memory, _ollama, _server, _loaded, _installed,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, config = self._files(directory)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "--check", str(root), "--lms", str(lms),
                    "--opencode", str(opencode), "--config", str(config),
                ])
            self.assertEqual(code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "ready")
            self.assertFalse(receipt["networkListenerStarted"])
            self.assertFalse(receipt["modelLoaded"])
            self.assertFalse(receipt["opencodeStarted"])
            self.assertFalse(receipt["projectPathReturned"])

    @patch("scripts.run_lmstudio_code._model_installed", return_value=True)
    @patch("scripts.run_lmstudio_code._loaded_models", return_value=[])
    @patch("scripts.run_lmstudio_code._server_status", return_value={"running": False})
    @patch("scripts.run_lmstudio_code._ollama_model_loaded", return_value=False)
    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=4 * GIB)
    def test_memory_gate_blocks_before_start(
        self, _memory, _ollama, _server, _loaded, _installed,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, config = self._files(directory)
            error = io.StringIO()
            with patch("scripts.run_lmstudio_code._command") as command, redirect_stderr(error):
                code = main([
                    "--check", str(root), "--lms", str(lms),
                    "--opencode", str(opencode), "--config", str(config),
                ])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(error.getvalue())["reason"], "lmstudio_quality_model_memory_blocked")
            command.assert_not_called()

    @patch("scripts.run_lmstudio_code._model_installed", return_value=True)
    @patch("scripts.run_lmstudio_code._loaded_models", return_value=[])
    @patch("scripts.run_lmstudio_code._server_status", return_value={"running": False})
    @patch("scripts.run_lmstudio_code._ollama_model_loaded", return_value=True)
    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=6 * GIB)
    def test_runtime_conflict_fails_closed(
        self, _memory, _ollama, _server, _loaded, _installed,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, config = self._files(directory)
            error = io.StringIO()
            with redirect_stderr(error):
                code = main([
                    "--check", str(root), "--lms", str(lms),
                    "--opencode", str(opencode), "--config", str(config),
                ])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(error.getvalue())["reason"], "ollama_model_already_loaded")

    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=6 * GIB)
    @patch("scripts.run_lmstudio_code.readiness")
    @patch("scripts.run_lmstudio_code._loaded_models")
    @patch("scripts.run_lmstudio_code._server_status")
    @patch("scripts.run_lmstudio_code._command")
    @patch("scripts.run_lmstudio_code.subprocess.run")
    def test_run_uses_loopback_4b_and_confirms_cleanup(
        self, run, command, server_status, loaded_models, readiness, _memory,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, _config = self._files(directory)
            readiness.return_value = (root, lms, opencode)
            command.return_value = MagicMock(returncode=0, stdout="", stderr="")
            server_status.side_effect = [{"running": True, "port": 1234}, {"running": False}]
            loaded_models.side_effect = [[{"identifier": MODEL_IDENTIFIER}], []]
            run.return_value = MagicMock(returncode=0)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(root), "--lms", str(lms), "--opencode", str(opencode)])
            self.assertEqual(code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "finished")
            self.assertTrue(receipt["cleanupConfirmed"])
            commands = [item.args[0] for item in command.call_args_list]
            self.assertIn([str(lms), "server", "start", "--port", "1234", "--bind", "127.0.0.1"], commands)
            self.assertIn([str(lms), "unload", MODEL_IDENTIFIER], commands)
            self.assertIn([str(lms), "server", "stop"], commands)
            run.assert_called_once_with(
                [str(opencode), ".", "--model", f"lmstudio/{MODEL_IDENTIFIER}"],
                cwd=root, check=False,
            )

    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=6 * GIB)
    @patch("scripts.run_lmstudio_code.readiness")
    @patch("scripts.run_lmstudio_code._loaded_models")
    @patch("scripts.run_lmstudio_code._server_status")
    @patch("scripts.run_lmstudio_code._command")
    @patch("scripts.run_lmstudio_code.subprocess.run")
    def test_product_agent_uses_quality_model_and_explicit_agent(
        self, run, command, server_status, loaded_models, readiness, _memory,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, _config = self._files(directory)
            readiness.return_value = (root, lms, opencode)
            command.return_value = MagicMock(returncode=0, stdout="", stderr="")
            server_status.side_effect = [{"running": True, "port": 1234}, {"running": False}]
            loaded_models.side_effect = [[{"identifier": MODEL_IDENTIFIER}], []]
            run.return_value = MagicMock(returncode=0)
            with redirect_stdout(io.StringIO()):
                code = main([
                    str(root), "--agent", "vision-product",
                    "--lms", str(lms), "--opencode", str(opencode),
                ])
            self.assertEqual(code, 0)
            readiness.assert_called_once_with(
                root, lms, opencode,
                Path.home() / ".config" / "opencode" / "opencode.json",
                6 * GIB, "vision-product",
            )
            run.assert_called_once_with(
                [
                    str(opencode), ".", "--model", f"lmstudio/{MODEL_IDENTIFIER}",
                    "--agent", "vision-product",
                ],
                cwd=root, check=False,
            )

    @patch("scripts.run_lmstudio_code.available_memory_bytes", return_value=6 * GIB)
    @patch("scripts.run_lmstudio_code.readiness")
    @patch("scripts.run_lmstudio_code._loaded_models", return_value=[])
    @patch("scripts.run_lmstudio_code._server_status")
    @patch("scripts.run_lmstudio_code._command")
    def test_load_failure_still_unloads_and_stops_owned_runtime(
        self, command, server_status, _loaded_models, readiness, _memory,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, lms, opencode, _config = self._files(directory)
            readiness.return_value = (root, lms, opencode)
            server_status.side_effect = [{"running": True, "port": 1234}, {"running": False}]
            command.side_effect = [
                MagicMock(returncode=0), MagicMock(returncode=1),
                MagicMock(returncode=0), MagicMock(returncode=0),
            ]
            error = io.StringIO()
            with redirect_stderr(error):
                code = main([str(root), "--lms", str(lms), "--opencode", str(opencode)])
            self.assertEqual(code, 2)
            receipt = json.loads(error.getvalue())
            self.assertEqual(receipt["reason"], "lmstudio_model_load_failed")
            self.assertTrue(receipt["cleanupConfirmed"])
            commands = [item.args[0] for item in command.call_args_list]
            self.assertIn([str(lms), "unload", MODEL_IDENTIFIER], commands)
            self.assertIn([str(lms), "server", "stop"], commands)


if __name__ == "__main__":
    unittest.main()
