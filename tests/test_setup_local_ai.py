from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.setup_local_ai import (
    AGENT_MODEL, ASK_MODEL, MANAGED_LAUNCHER_MARKER, SetupError,
    _legacy_launcher_payloads, _load_config, _validate_paths, run_setup,
)
from scripts.check_company_mcp import SCHEMA as MCP_SELF_TEST_SCHEMA


def ready_dependencies() -> dict[str, object]:
    return {
        "pythonReady": True, "pythonVersion": "3.14.6",
        "ollamaInstalled": True, "ollamaServiceReady": True,
        "openCodeInstalled": True, "agentModelInstalled": True,
        "askModelInstalled": True, "qualityModelInstalled": False,
    }


def ready_mcp(_: Path) -> tuple[int, dict[str, object]]:
    return 0, {
        "schema": MCP_SELF_TEST_SCHEMA, "status": "ready", "ready": True,
        "transport": "stdio", "profile": "compact",
        "serverName": "local-agent-company", "protocolVersion": "2025-06-18",
        "buildId": "local-build-20260802.40", "sourceSha256": "b" * 64,
        "operationalSourceVerified": True,
        "processExited": True, "processExitCode": 0,
        "roundTripCompleted": True, "pingReady": True,
        "toolCount": 1, "actionCount": 25,
        "toolSchemaSha256": "a" * 64, "durationMs": 1,
        "modelCalled": False, "networkListenerOpened": False,
        "persistentStateMutated": False, "externalActionPerformed": False,
        "paidApiUsed": False, "credentialValuesReturned": False,
    }


class SetupLocalAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def paths(self, base: Path):
        desktop = base / "Desktop"
        desktop.mkdir()
        return _validate_paths(
            self.root,
            base / ".config" / "opencode" / "opencode.json",
            base / "company",
            desktop,
        )

    def test_preview_of_fresh_machine_is_read_only_and_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = self.paths(base)
            code, receipt = run_setup(
                "preview", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "preview")
            self.assertFalse(receipt["ready"])
            self.assertIn("run_setup_apply", receipt["actions"])
            self.assertIn("initialize_local_company", receipt["actions"])
            self.assertFalse(receipt["effects"]["stateMutated"])
            self.assertEqual(receipt["effects"]["modelsPulled"], 0)
            self.assertFalse(paths.config.exists())
            self.assertFalse(paths.company_home.exists())
            self.assertEqual(list(paths.desktop.iterdir()), [])

    def test_apply_enforces_llama_only_config_backs_up_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = self.paths(base)
            paths.config.parent.mkdir(parents=True)
            original = {
                "$schema": "https://opencode.ai/config.json",
                "theme": "system",
                "model": "private_fixture/cloud-model",
                "provider": {
                    "private_fixture": {
                        "options": {"apiKey": "DO-NOT-RETURN"},
                    },
                },
            }
            original_bytes = (json.dumps(original, indent=2) + "\n").encode()
            paths.config.write_bytes(original_bytes)

            code, receipt = run_setup(
                "apply", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(code, 0)
            self.assertTrue(receipt["ready"])
            self.assertEqual(receipt["status"], "ready")
            self.assertTrue(receipt["config"]["written"])
            self.assertTrue(receipt["config"]["backupCreated"])
            self.assertTrue(receipt["companyState"]["initializedBySetup"])
            self.assertTrue(receipt["companyState"]["starterProjectCreated"])
            self.assertEqual(receipt["desktopLaunchers"]["written"], 2)
            self.assertNotIn("DO-NOT-RETURN", json.dumps(receipt))

            configured = json.loads(paths.config.read_text(encoding="utf-8"))
            self.assertEqual(configured["theme"], "system")
            self.assertEqual(configured["model"], f"ollama/{AGENT_MODEL}")
            self.assertEqual(configured["small_model"], f"ollama/{AGENT_MODEL}")
            self.assertEqual(configured["enabled_providers"], ["ollama"])
            self.assertEqual(set(configured["provider"]), {"ollama"})
            self.assertNotIn("DO-NOT-RETURN", json.dumps(configured))
            self.assertIn(AGENT_MODEL, configured["provider"]["ollama"]["models"])
            self.assertIn(ASK_MODEL, configured["provider"]["ollama"]["models"])
            self.assertFalse(configured["tools"]["local_company_*"])
            self.assertTrue(
                configured["agent"]["local-company"]["tools"]["local_company_*"],
            )
            self.assertEqual(
                configured["agent"]["local-company"]["model"],
                f"ollama/{AGENT_MODEL}",
            )
            backup = paths.config.with_name(receipt["config"]["backupName"])
            self.assertEqual(backup.read_bytes(), original_bytes)
            self.assertTrue((paths.company_home / "company.db").is_file())
            for launcher in paths.desktop.iterdir():
                text = launcher.read_text(encoding="utf-8")
                self.assertIn(MANAGED_LAUNCHER_MARKER, text)
                self.assertIn(str(self.root / "local-ai-menu.cmd"), text)

            second_code, second = run_setup(
                "apply", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(second_code, 0)
            self.assertTrue(second["ready"])
            self.assertFalse(second["effects"]["stateMutated"])
            self.assertFalse(second["config"]["written"])
            self.assertEqual(second["desktopLaunchers"]["written"], 0)
            self.assertFalse(second["companyState"]["initializedBySetup"])
            self.assertEqual(len(list(paths.config.parent.glob("*.supermega-backup-*"))), 1)

    def test_remote_ollama_conflict_fails_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = self.paths(base)
            paths.config.parent.mkdir(parents=True)
            raw = json.dumps({
                "provider": {"ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "https://example.invalid/v1"},
                    "models": {},
                }},
            }).encode()
            paths.config.write_bytes(raw)
            with self.assertRaisesRegex(SetupError, "ollama_provider_endpoint_conflict"):
                run_setup(
                    "apply", paths, dependency_probe=ready_dependencies,
                    mcp_probe=ready_mcp,
                )
            self.assertEqual(paths.config.read_bytes(), raw)
            self.assertFalse(paths.company_home.exists())
            self.assertEqual(list(paths.desktop.iterdir()), [])

    def test_unmanaged_desktop_launcher_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = self.paths(base)
            unmanaged = paths.desktop / "SuperMega AI Workbench.cmd"
            unmanaged.write_text("@echo off\r\necho user-owned\r\n", encoding="utf-8")
            with self.assertRaisesRegex(SetupError, "desktop_launcher_conflict"):
                run_setup(
                    "apply", paths, dependency_probe=ready_dependencies,
                    mcp_probe=ready_mcp,
                )
            self.assertIn("user-owned", unmanaged.read_text(encoding="utf-8"))
            self.assertFalse(paths.config.exists())
            self.assertFalse(paths.company_home.exists())

    def test_exact_legacy_desktop_launchers_are_safely_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            for name, payloads in _legacy_launcher_payloads(self.root).items():
                (paths.desktop / name).write_bytes(payloads[0])

            preview_code, preview = run_setup(
                "preview", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(preview_code, 0)
            self.assertEqual(
                set(preview["desktopLaunchers"]["statuses"].values()), {"adopt"},
            )
            self.assertFalse(preview["effects"]["stateMutated"])

            apply_code, applied = run_setup(
                "apply", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(apply_code, 0)
            self.assertTrue(applied["ready"])
            self.assertEqual(applied["desktopLaunchers"]["written"], 2)
            for launcher in paths.desktop.iterdir():
                self.assertIn(
                    MANAGED_LAUNCHER_MARKER,
                    launcher.read_text(encoding="utf-8"),
                )

    def test_check_distinguishes_configuration_from_missing_dependencies(self) -> None:
        missing = ready_dependencies()
        missing["openCodeInstalled"] = False
        missing["agentModelInstalled"] = False
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            apply_code, applied = run_setup(
                "apply", paths, dependency_probe=lambda: missing,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(apply_code, 0)
            self.assertEqual(applied["status"], "configured_attention")
            self.assertFalse(applied["ready"])
            self.assertIn("install_opencode", applied["actions"])
            self.assertIn(f"ollama_pull_{AGENT_MODEL}", applied["actions"])

            check_code, checked = run_setup(
                "check", paths, dependency_probe=lambda: missing,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(check_code, 1)
            self.assertEqual(checked["status"], "attention")
            self.assertFalse(checked["effects"]["stateMutated"])

    def test_check_requires_a_safe_real_mcp_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.paths(Path(directory))
            apply_code, _ = run_setup(
                "apply", paths, dependency_probe=ready_dependencies,
                mcp_probe=ready_mcp,
            )
            self.assertEqual(apply_code, 0)
            config_before = paths.config.read_bytes()
            database_before = (paths.company_home / "company.db").read_bytes()

            def blocked(_: Path):
                return 1, {
                    "schema": MCP_SELF_TEST_SCHEMA,
                    "status": "blocked", "ready": False,
                    "reason": "mcp_process_failed",
                    "transport": "stdio", "profile": "compact",
                    "modelCalled": False, "networkListenerOpened": False,
                    "persistentStateMutated": False,
                    "externalActionPerformed": False, "paidApiUsed": False,
                    "credentialValuesReturned": False,
                }

            check_code, checked = run_setup(
                "check", paths, dependency_probe=ready_dependencies,
                mcp_probe=blocked,
            )
            self.assertEqual(check_code, 1)
            self.assertEqual(checked["status"], "attention")
            self.assertFalse(checked["ready"])
            self.assertEqual(checked["mcpSelfTest"]["status"], "blocked")
            self.assertIn("inspect_company_mcp", checked["actions"])
            self.assertFalse(checked["effects"]["stateMutated"])
            self.assertEqual(paths.config.read_bytes(), config_before)
            self.assertEqual((paths.company_home / "company.db").read_bytes(), database_before)

            unsafe_code, unsafe = ready_mcp(paths.root)
            unsafe["externalActionPerformed"] = True
            with self.assertRaisesRegex(SetupError, "mcp_self_test_receipt_invalid"):
                run_setup(
                    "check", paths, dependency_probe=ready_dependencies,
                    mcp_probe=lambda _: (unsafe_code, unsafe),
                )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "opencode.json"
            config.write_text('{"provider":{},"provider":{}}', encoding="utf-8")
            with self.assertRaisesRegex(SetupError, "opencode_config_duplicate_key"):
                _load_config(config)


if __name__ == "__main__":
    unittest.main()
