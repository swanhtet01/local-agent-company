from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.mcp_server import MAX_TOOL_CALLS, RUN_CONFIRMATION, McpSession


class LocalCompanyMcpTests(unittest.TestCase):
    def _session(self, root: Path) -> McpSession:
        session = McpSession(root)
        initialized = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        })
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "local-agent-company")
        self.assertIsNone(session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        return session

    @staticmethod
    def _call(session: McpSession, name: str, arguments: dict | None = None, identifier: int = 2):
        return session.handle({
            "jsonrpc": "2.0", "id": identifier, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def test_profile_is_small_pathless_and_model_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            listed = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = listed["result"]["tools"]
            self.assertEqual(
                {tool["name"] for tool in tools},
                {"status", "projects", "preflight", "queue_list", "queue_add", "queue_run"},
            )
            self.assertEqual(len(tools), 6)
            self.assertFalse(next(tool for tool in tools if tool["name"] == "queue_add")["annotations"]["readOnlyHint"])
            status = self._call(session, "status")["result"]["structuredContent"]
            self.assertTrue(status["localOnly"])
            self.assertFalse(status["modelCalled"])
            self.assertFalse(status["externalActionPerformed"])
            self.assertNotIn(str(Path(directory)), str(status))

    def test_preflight_surfaces_owner_gate_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            result = self._call(session, "preflight", {"objective": "Send email to every customer"})
            receipt = result["result"]["structuredContent"]
            self.assertEqual(receipt["status"], "owner_gate_required")
            self.assertIn("external_communication", receipt["owner_gate_categories"])
            self.assertFalse(receipt["effects"]["mission_queued"])

    def test_confirmed_safe_queue_add_mutates_only_the_local_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            refused = self._call(session, "queue_add", {"objective": "Prepare one internal product brief"})
            self.assertEqual(refused["error"]["message"], "queue_confirmation_required")
            queued = self._call(session, "queue_add", {
                "objective": "Prepare one internal product brief", "queueConfirmed": True,
                "priority": 60,
            }, 3)["result"]["structuredContent"]
            self.assertTrue(queued["queued"])
            self.assertFalse(queued["modelCalled"])
            self.assertFalse(queued["externalActionPerformed"])
            self.assertEqual(session.company_tools.company.queue_preflight()["queue_id"], queued["queueId"])

    def test_queue_list_is_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            first = self._call(session, "queue_add", {
                "objective": "Draft an internal product hypothesis", "queueConfirmed": True,
            })["result"]["structuredContent"]
            listed = self._call(session, "queue_list", {"status": "queued", "limit": 1}, 3)
            receipt = listed["result"]["structuredContent"]
            self.assertEqual(receipt["count"], 1)
            self.assertEqual(receipt["items"][0]["queueId"], first["queueId"])
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            invalid = self._call(session, "queue_list", {"limit": 51}, 4)
            self.assertEqual(invalid["error"]["message"], "invalid_limit")

    def test_queue_run_requires_bound_confirmation_and_uses_injected_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls: list[str] = []
            session = McpSession(Path(directory))
            session.company_tools.executor = lambda queue_id: (
                calls.append(queue_id) or {
                    "status": "completed", "queueId": queue_id, "jobId": "b" * 12,
                    "qualityPassed": True, "modelCalled": True,
                }
            )
            initialized = session.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            })
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "local-agent-company")
            session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            queued = self._call(session, "queue_add", {
                "objective": "Draft an internal product hypothesis", "queueConfirmed": True,
            })["result"]["structuredContent"]
            refused = self._call(session, "queue_run", {
                "expectedQueueId": queued["queueId"], "runConfirmation": "yes",
            }, 3)
            self.assertEqual(refused["error"]["message"], "run_confirmation_required")
            self.assertEqual(calls, [])
            executed = self._call(session, "queue_run", {
                "expectedQueueId": queued["queueId"], "runConfirmation": RUN_CONFIRMATION,
            }, 4)["result"]["structuredContent"]
            self.assertEqual(executed["status"], "completed")
            self.assertEqual(calls, [queued["queueId"]])
            self.assertFalse(executed["externalActionPerformed"])
            self.assertFalse(executed["paidApiUsed"])

    def test_queue_run_blocks_before_subprocess_when_memory_is_below_two_gib(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            queued = self._call(session, "queue_add", {
                "objective": "Draft an internal product hypothesis", "queueConfirmed": True,
            })["result"]["structuredContent"]
            with patch("local_company.mcp_server.observe_memory", return_value={
                "status": "ready", "available_bytes": 1024**3,
            }), patch("local_company.mcp_server.subprocess.run") as run:
                result = self._call(session, "queue_run", {
                    "expectedQueueId": queued["queueId"],
                    "runConfirmation": RUN_CONFIRMATION,
                }, 3)["result"]["structuredContent"]
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "insufficient_available_memory")
            self.assertFalse(result["modelCalled"])
            run.assert_not_called()

    def test_protocol_fails_closed_before_initialization_and_on_unknown_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = McpSession(Path(directory))
            before = session.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            self.assertEqual(before["error"]["message"], "server_not_initialized")
            session = self._session(Path(directory))
            hidden = self._call(session, "run", {})
            self.assertEqual(hidden["error"], {"code": -32602, "message": "unknown_tool"})
            session.calls = MAX_TOOL_CALLS
            limited = self._call(session, "status", {}, 4)
            self.assertEqual(limited["error"]["message"], "tool_call_limit_exceeded")

    def test_newer_client_protocol_negotiates_to_supported_server_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = McpSession(Path(directory))
            initialized = session.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "future", "version": "1"}},
            })
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")


if __name__ == "__main__":
    unittest.main()
