from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_company.mcp_server import (
    MAX_TOOL_CALLS,
    KNOWLEDGE_ADD_CONFIRMATION,
    PROJECT_CREATE_CONFIRMATION,
    RUN_CONFIRMATION,
    SCHEDULE_CHANGE_CONFIRMATION,
    SCHEDULE_CREATE_CONFIRMATION,
    McpSession,
)


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
                {
                    "status", "projects", "preflight", "queue_list", "queue_add", "queue_run",
                    "jobs", "job_result", "schedule_list", "schedule_create",
                    "schedule_set_enabled", "playbooks", "project_create", "project_overview",
                    "knowledge_list", "knowledge_add", "knowledge_search",
                },
            )
            self.assertEqual(len(tools), 17)
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

    def test_jobs_and_job_result_return_pathless_bounded_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root)
            job_id, _ = session.company_tools.company.run("Prepare one internal product decision")
            jobs = self._call(session, "jobs", {"status": "complete", "limit": 1}, 3)
            history = jobs["result"]["structuredContent"]
            self.assertEqual(history["jobs"][0]["jobId"], job_id)
            result = self._call(session, "job_result", {"jobId": job_id}, 4)
            receipt = result["result"]["structuredContent"]
            self.assertEqual(receipt["job"]["jobId"], job_id)
            self.assertTrue(receipt["quality"]["passed"])
            self.assertTrue(receipt["job"]["reportAvailable"])
            self.assertFalse(receipt["job"]["synthesisTruncated"])
            self.assertNotIn(str(root), str(receipt))
            self.assertFalse(receipt["modelCalled"])
            self.assertFalse(receipt["stateMutated"])
            unknown = self._call(session, "job_result", {"jobId": "f" * 12}, 5)
            self.assertEqual(unknown["error"]["message"], "unknown_job")

    def test_recurring_mission_requires_confirmation_and_can_be_paused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            request = {
                "name": "Weekly product review",
                "objective": "Prepare one internal product decision",
                "cadenceDays": 7, "nextRunAt": "2030-01-01T09:00:00+00:00",
            }
            refused = self._call(session, "schedule_create", request, 3)
            self.assertEqual(refused["error"]["message"], "schedule_confirmation_required")
            created = self._call(session, "schedule_create", {
                **request, "scheduleConfirmation": SCHEDULE_CREATE_CONFIRMATION,
            }, 4)["result"]["structuredContent"]
            self.assertTrue(created["created"])
            listed = self._call(session, "schedule_list", {"limit": 1}, 5)
            schedule = listed["result"]["structuredContent"]["schedules"][0]
            self.assertEqual(schedule["scheduleId"], created["scheduleId"])
            self.assertTrue(schedule["enabled"])
            paused = self._call(session, "schedule_set_enabled", {
                "scheduleId": created["scheduleId"], "enabled": False,
                "scheduleConfirmation": SCHEDULE_CHANGE_CONFIRMATION,
            }, 6)["result"]["structuredContent"]
            self.assertTrue(paused["changed"])
            self.assertFalse(paused["enabled"])
            self.assertFalse(paused["modelCalled"])
            self.assertFalse(paused["externalActionPerformed"])

    def test_recurring_external_action_is_blocked_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            result = self._call(session, "schedule_create", {
                "name": "Customer outreach", "objective": "Send an email to every customer",
                "cadenceDays": 1, "nextRunAt": "2030-01-01T09:00:00+00:00",
                "scheduleConfirmation": SCHEDULE_CREATE_CONFIRMATION,
            }, 3)["result"]["structuredContent"]
            self.assertEqual(result["status"], "blocked")
            self.assertIn("external_communication", result["preflight"]["owner_gate_categories"])
            self.assertEqual(session.company_tools.company.schedules(), [])

    def test_project_workspace_creation_is_confirmed_and_overview_is_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root)
            refused = self._call(session, "project_create", {"name": "Invention Lab"}, 3)
            self.assertEqual(refused["error"]["message"], "project_confirmation_required")
            self.assertEqual(session.company_tools.company.projects(), [])
            created = self._call(session, "project_create", {
                "name": "  Invention   Lab  ", "description": "Private product experiments.",
                "projectConfirmation": PROJECT_CREATE_CONFIRMATION,
            }, 4)["result"]["structuredContent"]
            self.assertEqual(created["name"], "Invention Lab")
            overview = self._call(session, "project_overview", {
                "project": created["projectId"],
            }, 5)["result"]["structuredContent"]
            self.assertEqual(overview["project"]["name"], "Invention Lab")
            self.assertEqual(overview["project"]["knowledgeSourceCount"], 0)
            self.assertEqual(overview["project"]["missionCount"], 0)
            self.assertNotIn(str(root), str(overview))
            self.assertFalse(overview["stateMutated"])

    def test_playbooks_are_model_free_and_expose_specialist_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
            result = self._call(session, "playbooks", {}, 3)["result"]["structuredContent"]
            product = next(item for item in result["playbooks"] if item["name"] == "product-build")
            self.assertIn("engineering", product["roles"])
            self.assertEqual(product["roleCount"], len(product["roles"]))
            self.assertFalse(result["modelCalled"])
            self.assertFalse(result["stateMutated"])

    def test_confirmed_inline_knowledge_is_retrievable_and_pathless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = self._session(root)
            project = self._call(session, "project_create", {
                "name": "Vision Lab", "projectConfirmation": PROJECT_CREATE_CONFIRMATION,
            }, 3)["result"]["structuredContent"]
            refused = self._call(session, "knowledge_add", {
                "project": project["projectId"], "title": "Pilot premise",
                "content": "The pilot milestone is ten reviewed workflows.",
            }, 4)
            self.assertEqual(refused["error"]["message"], "knowledge_confirmation_required")
            self.assertEqual(session.company_tools.company.knowledge_items(project["projectId"]), [])
            added = self._call(session, "knowledge_add", {
                "project": project["projectId"], "title": "Pilot premise",
                "content": "The pilot milestone is ten reviewed workflows.",
                "knowledgeConfirmation": KNOWLEDGE_ADD_CONFIRMATION,
            }, 5)["result"]["structuredContent"]
            self.assertTrue(added["added"])
            listed = self._call(session, "knowledge_list", {
                "project": project["projectId"],
            }, 6)["result"]["structuredContent"]
            self.assertEqual(listed["sources"][0]["sourceId"], added["sourceId"])
            self.assertTrue(listed["sources"][0]["managedInline"])
            searched = self._call(session, "knowledge_search", {
                "project": project["projectId"], "query": "pilot milestone reviewed workflows",
            }, 7)["result"]["structuredContent"]
            self.assertEqual(searched["hits"][0]["sourceId"], added["sourceId"])
            self.assertIn("ten reviewed workflows", searched["hits"][0]["excerpt"])
            self.assertNotIn(str(root), str(listed) + str(searched))
            preflight = self._call(session, "preflight", {
                "objective": "Review the pilot milestone", "project": project["projectId"],
            }, 8)["result"]["structuredContent"]
            self.assertEqual(preflight["status"], "ready")
            self.assertTrue(preflight["model_execution_ready"])

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
