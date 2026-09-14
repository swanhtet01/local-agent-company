"""Offline contract checks for the Linux container verifier."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from deploy import verify_linux_port as port


class LinuxPortVerifierTests(unittest.TestCase):
    def test_guarded_windows_module_is_excluded_but_importable_on_linux(self) -> None:
        with patch.object(
            port, "_import_probe", return_value=(True, "none", "", []),
        ), patch.object(port, "_windows_entrypoint_guarded", return_value=True):
            result = port.check_windows_module(target_os="posix")
        self.assertEqual(result["status"], "excluded")
        self.assertEqual(result["reason"], "guarded_on_posix")
        self.assertTrue(result["importable"])

    def test_linux_import_failure_or_missing_guard_blocks_the_port(self) -> None:
        with patch.object(
            port, "_import_probe", return_value=(False, "ValueError", "", []),
        ):
            missing = port.check_windows_module(target_os="posix")
        self.assertEqual(missing["status"], "import_failed_on_posix")

        with patch.object(
            port, "_import_probe", return_value=(True, "none", "", []),
        ), patch.object(port, "_windows_entrypoint_guarded", return_value=False):
            unguarded = port.check_windows_module(target_os="posix")
        self.assertEqual(unguarded["status"], "guard_missing_on_posix")
        self.assertEqual(unguarded["reason"], "computer_use_guard_missing_on_posix")

    def test_compose_sidecar_is_admitted_without_false_readiness_advisory(self) -> None:
        with patch.object(
            port, "check_python", return_value={"status": "ready"},
        ), patch.object(
            port, "check_platform",
            return_value={"status": "linux", "containerized": True},
        ), patch.object(
            port, "check_memory", return_value={
                "status": "ready", "headroom_ok": True, "cgroup": {"status": "ready"},
            },
        ), patch.object(
            port, "check_company_home",
            return_value={"status": "ready", "persistent_mount": True},
        ), patch.object(
            port, "_fetch_ollama_models", return_value=["llama3.2:1b"],
        ) as probe, patch.object(
            port, "check_windows_module", return_value={"status": "excluded"},
        ), patch.object(
            port, "check_cli_entrypoint", return_value={"status": "importable"},
        ):
            result, code = port.run_verification(
                ollama_host="http://ollama:11434", offline=False,
            )
        self.assertEqual(code, 0)
        self.assertTrue(result["pass"])
        self.assertEqual(result["checks"]["ollama"]["readiness_endpoint"],
                         "configured_compose_sidecar")
        self.assertEqual(result["advisories"], [])
        probe.assert_called_once_with("http://ollama:11434")

    def test_unadmitted_host_never_receives_a_verifier_request(self) -> None:
        with patch.object(port, "_fetch_ollama_models") as probe:
            result = port.check_ollama("https://example.invalid", offline=False)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "host_not_admitted_by_readiness")
        self.assertEqual(result["readiness_endpoint"], "nonlocal")
        self.assertIsNone(result["host"])
        probe.assert_not_called()

        with patch.object(
            port, "check_python", return_value={"status": "ready"},
        ), patch.object(
            port, "check_platform",
            return_value={"status": "linux", "containerized": True},
        ), patch.object(
            port, "check_memory", return_value={
                "status": "ready", "headroom_ok": True, "cgroup": {"status": "ready"},
            },
        ), patch.object(
            port, "check_company_home",
            return_value={"status": "ready", "persistent_mount": True},
        ), patch.object(
            port, "check_windows_module", return_value={"status": "excluded"},
        ), patch.object(
            port, "check_cli_entrypoint", return_value={"status": "importable"},
        ), patch.object(port, "_fetch_ollama_models") as forbidden:
            verification, code = port.run_verification(
                ollama_host="https://example.invalid", offline=False,
            )
        self.assertEqual(code, 1)
        self.assertEqual(verification["action"], "configure_admitted_ollama_host")
        self.assertIn("ollama_host_not_admitted_by_readiness", verification["blockers"])
        forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
