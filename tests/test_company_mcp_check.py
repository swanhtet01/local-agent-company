from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.mcp_server import McpSession
from scripts.check_company_mcp import (
    EXPECTED_ACTIONS, MAX_STDOUT_BYTES, SCHEMA, _requests,
    check_company_mcp, run_company_mcp_self_test,
)


def valid_stdout() -> bytes:
    session = McpSession(Path(tempfile.gettempdir()) / "unused-mcp-self-test", profile="compact")
    responses = []
    for line in _requests().splitlines():
        response = session.handle(json.loads(line))
        if response is not None:
            responses.append(response)
    return b"".join(
        (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
        for response in responses
    )


def valid_manifest(_: Path) -> dict[str, object]:
    return {
        "status": "ok",
        "build_id": "local-build-20260802.40",
        "source_sha256": "b" * 64,
    }


class CompanyMcpCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_real_launcher_round_trip_is_compact_model_free_and_state_free(self) -> None:
        receipt = check_company_mcp(self.root)
        self.assertEqual(receipt["schema"], SCHEMA)
        self.assertTrue(receipt["ready"])
        self.assertTrue(receipt["roundTripCompleted"])
        self.assertTrue(receipt["processExited"])
        self.assertEqual(receipt["profile"], "compact")
        self.assertTrue(receipt["operationalSourceVerified"])
        self.assertTrue(receipt["buildId"].startswith("local-build-"))
        self.assertEqual(receipt["toolCount"], 1)
        self.assertEqual(receipt["actionCount"], len(EXPECTED_ACTIONS))
        self.assertFalse(receipt["modelCalled"])
        self.assertFalse(receipt["networkListenerOpened"])
        self.assertFalse(receipt["persistentStateMutated"])
        self.assertFalse(receipt["externalActionPerformed"])

    def test_runner_is_bounded_uses_ephemeral_home_and_never_uses_shell(self) -> None:
        observed: dict[str, object] = {}

        def runner(command, **kwargs):
            observed.update(kwargs)
            observed["command"] = command
            home = Path(kwargs["env"]["LOCAL_COMPANY_HOME"])
            observed["ephemeralHome"] = home
            self.assertFalse(home.exists())
            self.assertEqual(kwargs["env"]["LOCAL_COMPANY_MCP_PROFILE"], "compact")
            self.assertNotIn("shell", kwargs)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.PIPE)
            self.assertEqual(kwargs["input"], _requests())
            return subprocess.CompletedProcess(command, 0, valid_stdout(), b"")

        with patch.dict(os.environ, {"SUPERMEGA_PRIVATE_SENTINEL": "DO-NOT-RETURN"}):
            receipt = check_company_mcp(
                self.root, runner=runner, manifest_checker=valid_manifest,
            )
        self.assertTrue(receipt["ready"])
        self.assertEqual(receipt["buildId"], "local-build-20260802.40")
        self.assertEqual(receipt["sourceSha256"], "b" * 64)
        self.assertNotIn("DO-NOT-RETURN", json.dumps(receipt))
        self.assertFalse(Path(observed["ephemeralHome"]).exists())

    def test_timeout_oversize_and_temporary_mutation_fail_closed(self) -> None:
        def timeout(*_, **__):
            raise subprocess.TimeoutExpired("company-mcp", 15)

        code, receipt = run_company_mcp_self_test(
            self.root, runner=timeout, manifest_checker=valid_manifest,
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["reason"], "mcp_process_timeout")
        self.assertFalse(receipt["ready"])

        oversized = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, b"x" * (MAX_STDOUT_BYTES + 1), b"",
        )
        code, receipt = run_company_mcp_self_test(
            self.root, runner=oversized, manifest_checker=valid_manifest,
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["reason"], "mcp_stdout_invalid")

        def mutating(command, **kwargs):
            Path(kwargs["env"]["LOCAL_COMPANY_HOME"]).mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, valid_stdout(), b"")

        code, receipt = run_company_mcp_self_test(
            self.root, runner=mutating, manifest_checker=valid_manifest,
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["reason"], "mcp_self_test_mutated_company_state")
        self.assertFalse(receipt["persistentStateMutated"])

    def test_unexpected_tool_contract_is_rejected(self) -> None:
        responses = [json.loads(line) for line in valid_stdout().splitlines()]
        tool = responses[2]["result"]["tools"][0]
        tool["inputSchema"]["properties"]["action"]["enum"].append("external_send")
        stdout = b"".join(
            (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            for response in responses
        )

        def runner(command, **_):
            return subprocess.CompletedProcess(command, 0, stdout, b"")

        code, receipt = run_company_mcp_self_test(
            self.root, runner=runner, manifest_checker=valid_manifest,
        )
        self.assertEqual(code, 1)
        self.assertEqual(receipt["reason"], "mcp_action_contract_invalid")
        self.assertFalse(receipt["externalActionPerformed"])

    def test_modified_launcher_or_invalid_manifest_blocks_before_execution(self) -> None:
        calls = 0

        def runner(command, **kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(command, 0, valid_stdout(), b"")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "local_company"
            source.mkdir(parents=True)
            (source / "mcp_server.py").write_text("# fixture\n", encoding="utf-8")
            (root / "company-mcp.cmd").write_text(
                "@echo off\necho modified\n", encoding="utf-8",
            )
            code, receipt = run_company_mcp_self_test(
                root, runner=runner, manifest_checker=valid_manifest,
            )
            self.assertEqual(code, 1)
            self.assertEqual(receipt["reason"], "company_mcp_launcher_invalid")
            self.assertEqual(calls, 0)

            (root / "company-mcp.cmd").write_bytes(
                (self.root / "company-mcp.cmd").read_bytes(),
            )
            code, receipt = run_company_mcp_self_test(
                root,
                runner=runner,
                manifest_checker=lambda _: (_ for _ in ()).throw(ValueError("private")),
            )
            self.assertEqual(code, 1)
            self.assertEqual(receipt["reason"], "mcp_build_manifest_invalid")
            self.assertNotIn("private", json.dumps(receipt))
            self.assertEqual(calls, 0)

    def test_cli_is_cwd_independent_and_emits_one_bounded_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, str(self.root / "scripts" / "check_company_mcp.py")],
                cwd=directory,
                check=False,
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(completed.stdout.count(b"\n"), 1)
        self.assertLess(len(completed.stdout), 2_048)
        receipt = json.loads(completed.stdout)
        self.assertTrue(receipt["ready"])
        self.assertEqual(receipt["actionCount"], 25)


if __name__ == "__main__":
    unittest.main()
