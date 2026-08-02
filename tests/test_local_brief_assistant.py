from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_local_brief_assistant as assistant


class LocalBriefAssistantTests(unittest.TestCase):
    def test_structured_answer_is_bounded_to_known_context_sources(self) -> None:
        parsed = assistant._parse_structured_answer(
            json.dumps({
                "answer": "Review the verified next action.",
                "usedSources": ["company_brief"],
                "limitations": ["No repository files were inspected."],
            }),
            {"company_brief", "playbooks"},
        )
        self.assertEqual(parsed["usedSources"], ["company_brief"])
        with self.assertRaisesRegex(ValueError, "assistant_response_invalid"):
            assistant._parse_structured_answer(
                json.dumps({
                    "answer": "Invented.", "usedSources": ["internet"],
                    "limitations": [],
                }),
                {"company_brief"},
            )

    def test_context_capture_refuses_mutating_or_model_backed_receipts(self) -> None:
        def unsafe(*_args: object) -> int:
            print(json.dumps({
                "modelCalled": False, "stateMutated": True,
                "externalActionPerformed": False,
            }))
            return 0

        with self.assertRaisesRegex(RuntimeError, "verified_context_effects_invalid"):
            assistant._capture_json(unsafe)

    def test_invalid_model_draft_becomes_deterministic_verified_fallback(self) -> None:
        class Response(io.BytesIO):
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                self.close()

        opener = Mock()
        opener.open.return_value = Response(json.dumps({
            "message": {"content": "unstructured model prose"},
        }).encode("utf-8"))
        context = {
            "scope": "supermega",
            "sources": {"supermega_status": {
                "nextAction": "plan_without_model",
                "command": 'local-ai.cmd supermega plan "OBJECTIVE"',
            }},
        }
        with patch.object(assistant.urllib.request, "build_opener", return_value=opener):
            answer = assistant._chat(assistant.MODEL, "What next?", context)
        self.assertFalse(answer["modelDraftAccepted"])
        self.assertTrue(answer["deterministicFallbackUsed"])
        self.assertEqual(answer["usedSources"], ["supermega_status"])
        self.assertIn("local-ai.cmd supermega plan", answer["answer"])
        self.assertNotIn("unstructured model prose", answer["answer"])

    def test_successful_answer_is_local_grounded_measured_and_unloaded(self) -> None:
        context = {
            "scope": "company",
            "sources": {"company_brief": {
                "nextAction": "inspect_queue",
                "command": "local-ai.cmd next",
            }},
        }
        answer = {
            "answer": "Inspect the queue.",
            "usedSources": ["company_brief"],
            "limitations": [],
        }
        output = io.StringIO()
        with (
            patch.object(assistant.shutil, "which", return_value=r"C:\Ollama\ollama.exe"),
            patch.object(assistant, "installed_ollama_models", return_value={assistant.MODEL}),
            patch.object(assistant, "_loaded_models", return_value=set()),
            patch.object(assistant, "collect_context", return_value=context),
            patch.object(assistant, "available_memory_bytes", return_value=2 * 1024**3),
            patch.object(assistant, "_chat", return_value=answer) as chat,
            patch.object(assistant, "_unload_model", return_value=True) as unload,
            redirect_stdout(output),
        ):
            self.assertEqual(assistant.main([
                "--scope", "company", "--question", "What next?",
            ]), 0)
        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["answer"], "Inspect the queue.")
        self.assertEqual(receipt["usedSources"], ["company_brief"])
        self.assertEqual(receipt["verifiedNextAction"], "inspect_queue")
        self.assertEqual(receipt["verifiedCommand"], "local-ai.cmd next")
        self.assertTrue(receipt["modelCalled"])
        self.assertTrue(receipt["modelUnloadedAfterRun"])
        self.assertFalse(receipt["paidApiUsed"])
        self.assertFalse(receipt["externalActionPerformed"])
        self.assertFalse(receipt["actionExecuted"])
        self.assertTrue(receipt["modelDraftAccepted"])
        self.assertFalse(receipt["deterministicFallbackUsed"])
        self.assertEqual(receipt["controls"]["endpoint"], assistant.OLLAMA_CHAT_URL)
        chat.assert_called_once_with(assistant.MODEL, "What next?", context)
        unload.assert_called_once_with(r"C:\Ollama\ollama.exe", assistant.MODEL)

    def test_low_memory_without_recovery_blocks_before_model_call(self) -> None:
        error = io.StringIO()
        with (
            patch.object(assistant.shutil, "which", return_value=r"C:\Ollama\ollama.exe"),
            patch.object(assistant, "installed_ollama_models", return_value={assistant.MODEL}),
            patch.object(assistant, "_loaded_models", return_value=set()),
            patch.object(assistant, "collect_context", return_value={
                "scope": "supermega", "sources": {"supermega_status": {}},
            }),
            patch.object(
                assistant, "available_memory_bytes",
                return_value=assistant.MINIMUM_AVAILABLE_BYTES - 1,
            ),
            patch.object(assistant, "_chat", side_effect=AssertionError("model called")),
            redirect_stderr(error),
        ):
            self.assertEqual(assistant.main([
                "--scope", "supermega", "--question", "What next?",
                "--no-recover-memory",
            ]), 2)
        receipt = json.loads(error.getvalue())
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["reason"], "grounded_assistant_memory_blocked")
        self.assertFalse(receipt["modelCalled"])
        self.assertEqual(receipt["memoryShortfallBytes"], 1)

    def test_default_path_uses_validated_memory_recovery_once(self) -> None:
        readings = iter([
            assistant.MINIMUM_AVAILABLE_BYTES - 1,
            assistant.MINIMUM_AVAILABLE_BYTES + 100 * 1024**2,
        ])

        def memory() -> int:
            return next(readings, assistant.MINIMUM_AVAILABLE_BYTES + 100 * 1024**2)

        recovery = {
            "attempted": True, "status": "completed", "targetCount": 4,
            "trimSucceeded": 4, "trimFailed": 0,
            "releasedWorkingSetMb": 512.0, "processTerminationCalls": 0,
        }
        output = io.StringIO()
        with (
            patch.object(assistant.shutil, "which", return_value=r"C:\Ollama\ollama.exe"),
            patch.object(assistant, "installed_ollama_models", return_value={assistant.MODEL}),
            patch.object(assistant, "_loaded_models", return_value=set()),
            patch.object(assistant, "collect_context", return_value={
                "scope": "company", "sources": {"company_brief": {}},
            }),
            patch.object(assistant, "available_memory_bytes", side_effect=memory),
            patch.object(assistant, "_recover_memory", return_value=recovery) as recover,
            patch.object(assistant, "_chat", return_value={
                "answer": "Ready.", "usedSources": ["company_brief"],
                "limitations": [],
            }),
            patch.object(assistant, "_unload_model", return_value=True),
            redirect_stdout(output),
        ):
            self.assertEqual(assistant.main([
                "--question", "Can I use this?",
            ]), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["memoryRecovery"], recovery)
        recover.assert_called_once_with(Path(assistant.__file__).resolve().parents[1])


if __name__ == "__main__":
    unittest.main()
