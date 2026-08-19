import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from local_company import __version__
from local_company.build_info import (
    BUILD_ID, RUNTIME_BUILD_SCHEMA, SOURCE_SHA256,
)
from local_company.cli import main as cli_main, parser
from local_company.config import (
    COMPANY_DB_SCHEMA_VERSION, COMPANY_STORE_SCHEMA, default_company_home,
    restrict_file_to_current_user, valid_company_instance_id,
)
from local_company.core import (
    Company, EVALUATOR_VERSION, ExecutionLeaseLost, MockModel, OllamaModel,
    PLAYBOOKS, ROLES,
    QUALITY_RECOVERY_LIST_SCHEMA, QUALITY_RECOVERY_SCHEMA, QUEUE_PARK_SCHEMA,
    QUEUE_SUPERSEDE_SCHEMA,
    ReportFinalizationPending, SourceHit,
    _failure_mode_is_substantive,
    _required_ending_from_objective,
    _requires_strict_grounded_synthesis,
    bounded_context_blocks,
    compact_labeled_sections,
    count_words,
    product_experiment_observation_digest,
    evidence_filename_pairs_valid,
    extract_labeled_sections,
    mark_unverified_advisory,
    mark_unverified_draft,
    render_structured_synthesis,
    sequential_numbered_items,
    source_limitation_conflicts,
    truncate_words,
)
from local_company.dashboard import (
    LocalQueueWorker, build_status_snapshot, create_dashboard_server, dashboard_snapshot,
    health_endpoint_snapshot, render_dashboard, render_dataset_quality_detail,
    render_mission_detail, render_product_review, render_quality_failure_overview,
    render_vision_capture_fixture,
    runtime_build_identity, runtime_model_identity,
)
from local_company.service import (
    PROCESS_BIRTH_SCHEMA, SERVICE_STATE_SCHEMA, _ProcessObservation,
    _SERVICE_PROBE_TIMEOUT_SECONDS, _STARTUP_HEALTH_ATTEMPTS,
    _STARTUP_HEALTH_DEADLINE_SECONDS, _observe_process, _probe, _read_state,
    _service_python_executable, _startup_lock, _write_state, service_status,
    start_service, stop_service,
)
from scripts.local_ai import translate


class RecordingModel:
    def __init__(self):
        self.prompts = []

    def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        return f"result-{len(self.prompts)}"


class CountingMockModel(MockModel):
    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        return super().complete(system, prompt)


class UncacheableMockModel:
    def __init__(self):
        self.calls = 0

    def complete(self, system, prompt):
        self.calls += 1
        return MockModel().complete(system, prompt)


class VersionedMockModel(CountingMockModel):
    def __init__(self, runtime_version):
        super().__init__()
        self.runtime_version = runtime_version

    def cache_identity(self):
        return {**super().cache_identity(), "runtime_version": self.runtime_version}


class FailOnceModel(RecordingModel):
    def __init__(self):
        super().__init__()
        self.failed = False

    def complete(self, system, prompt):
        self.prompts.append((system, prompt))
        if len(self.prompts) == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("simulated model interruption")
        return f"result-{len(self.prompts)}"


class TruncatedModel(MockModel):
    def complete(self, system, prompt):
        result = super().complete(system, prompt)
        self.last_metrics = {"done_reason": "length", "output_tokens": 64}
        return result


class OfflineOllamaModel(OllamaModel):
    def cache_identity(self):
        return {
            "provider": "offline-test-ollama",
            "implementation": type(self).__qualname__,
            "num_predict": self.num_predict,
        }


class BlockingModel(MockModel):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, system, prompt):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("blocking model test timed out")
        return super().complete(system, prompt)


class SimulatedProcessCrash(BaseException):
    pass


class ConstraintModel(MockModel):
    def __init__(self, valid):
        self.valid = valid

    def complete(self, system, prompt):
        if "executive chair" not in system and "report editor" not in system:
            if self.valid:
                return (
                    "Verified local evidence is limited. Proposed work remains internal and reversible. "
                    "Assumptions require owner review before use."
                )
            return ("word " * 105) + "Ready to deploy. Scheduled: today."
        if self.valid:
                return (
                    "Verified facts: the local queue and health evidence are available. "
                    "Assumptions: operator adoption remains unmeasured. "
                    "Task templates: 1. Intake preserves evidence and scope. "
                    "2. Review checks frozen sources and constraints. "
                    "3. Audit records results and owner gates. "
                    "Daily review cadence: inspect once each day. "
                "Success checks: one accepted report and zero bypassed gates. "
                "Failure modes: unsupported claims block report acceptance. "
                "Owner gates: review every proposed action. "
                "Owner review required."
            )
        return (
            "The system achieved a 50% adoption gain and is ready to deploy. "
            "Approved and deployed immediately via file://path/to/[UNK_PLAN]. Owner review required."
        )


class RevisionModel(MockModel):
    def complete(self, system, prompt):
        if "report editor" in system:
            sections = [
                ("Verified facts", "local evidence remains available for careful internal review"),
                ("Assumptions", "operator adoption remains unmeasured and requires validation"),
                ("Task templates (three internal options)", "use intake review and audit templates for reversible work"),
                ("Daily review cadence", "inspect queue health and reports once every day"),
                ("Success checks (must pass)", "accept one grounded report with zero bypassed gates"),
                ("Failure modes", "missing evidence blocks local report acceptance"),
                ("Owner gates", "review every proposed action before any real execution"),
            ]
            return "\n\n".join(
                f"{label}: " + ((content + " ") * 5).strip() for label, content in sections
            ) + "\n\nOwner review required."
        if "executive chair" in system:
            return ("unstructured " * 220) + "Owner review required."
        return "specialist " * 130


class CitationModel(MockModel):
    def __init__(self, cited):
        self.cited = cited

    def complete(self, system, prompt):
        if "executive chair" not in system and "report editor" not in system:
            evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
            suffix = f" {evidence.group(0)}" if self.cited and evidence else ""
            return "Review the supplied local evidence and preserve every owner gate." + suffix
        evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
        evidence_tag = f" {evidence.group(0)}" if self.cited and evidence else ""
        citation = (
            f"notes.md confirms the imported operating fields{evidence_tag}"
            if self.cited else "Local evidence exists"
        )
        return (
            f"Verified facts: {citation}. Assumptions: adoption remains unmeasured. "
            "Task templates: intake review and audit. Daily review cadence: inspect work every day. "
            "Success checks: one accepted grounded report. "
            "Failure modes: missing evidence blocks report acceptance. "
            "Owner gates: review every proposed action. Owner review required."
        )


class ContradictingSourceModel(MockModel):
    def complete(self, system, prompt):
        return (
            "Verified facts: PostHog and Sentry are confirmed ready and connected for scaling client "
            "templates. Assumptions: operator adoption remains unmeasured and requires owner review. "
            "Proposed work stays internal and reversible. Owner review required."
        )


class EvidenceCitingModel(MockModel):
    def complete(self, system, prompt):
        evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
        evidence_tag = evidence.group(0) if evidence else "[EVIDENCE:missing]"
        if "executive chair" in system or "report editor" in system:
            return (
                f"Verified facts: notes.md records the local inventory baseline {evidence_tag}. "
                "Assumptions: future demand remains unknown and requires owner review. "
                "Recommendations remain local, reversible, and unexecuted. Owner review required."
            )
        return (
            f"Verified local evidence in notes.md records the inventory baseline {evidence_tag}. "
            "Future demand is an assumption, and all recommendations remain local and reversible."
        )


class FilenameOnlyCitationModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            return (
                "Verified facts: notes.md records the local inventory baseline [EVIDENCE:not-a-real-id]. "
                "Assumptions: future demand remains unknown and requires owner review. "
                "Recommendations remain local and reversible. Owner review required."
            )
        return "Review notes.md as local evidence, label assumptions, and preserve owner review."


class MismatchedEvidencePairModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
            evidence_tag = evidence.group(0) if evidence else "[EVIDENCE:missing]"
            return (
                "Verified facts: beta.md is verified as the captured local baseline "
                f"{evidence_tag}. Assumptions: future adoption remains unknown and must stay "
                "subject to owner review before any external action. Recommendations remain "
                "local, reversible, and unexecuted."
            )
        return "Review the frozen local sources without claiming execution or external change."


class CrossSwappedEvidencePairModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            evidence = list(dict.fromkeys(re.findall(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)))
            first = evidence[0] if evidence else "[EVIDENCE:missing-one]"
            second = evidence[1] if len(evidence) > 1 else "[EVIDENCE:missing-two]"
            return (
                f"Verified facts: alpha.md {second} and beta.md {first} are verified frozen local "
                "baselines for review. Assumptions: future adoption remains unknown and requires "
                "owner validation. Recommendations remain local, reversible, and unexecuted."
            )
        return "Review the frozen local sources without claiming execution or external change."


class CorrectPairWithAssumptionCitationModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            evidence = re.search(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)
            evidence_tag = evidence.group(0) if evidence else "[EVIDENCE:missing]"
            return (
                f"Verified facts (current):\nalpha.md {evidence_tag} is verified as the frozen local baseline.\n\n"
                f"Assumptions (unverified):\nFuture adoption may reference {evidence_tag} but remains unknown "
                "and requires owner validation. Recommendations remain local and unexecuted."
            )
        return "Review the frozen local source without claiming execution or external change."


class PiggybackEvidencePairModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            evidence = list(dict.fromkeys(re.findall(r"\[EVIDENCE:[0-9a-f]{16}\]", prompt)))
            second = evidence[1] if len(evidence) > 1 else "[EVIDENCE:missing]"
            return (
                "Verified facts: alpha.md establishes telemetry is active; "
                f"beta.md {second} is verified as a frozen local baseline. "
                "Assumptions: future adoption remains unknown and requires owner validation. "
                "Recommendations remain local, reversible, and unexecuted."
            )
        return "Review the frozen local sources without claiming execution or external change."


class MissingEvidencePairModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            return (
                "Verified facts: alpha.md is verified as a frozen local baseline. Assumptions: "
                "future adoption remains unknown and requires owner validation. Recommendations "
                "remain local, reversible, and unexecuted."
            )
        return "Review the frozen local source without claiming execution or external change."


class CosmeticTemplateModel(MockModel):
    def complete(self, system, prompt):
        if "executive chair" in system or "report editor" in system:
            return (
                "Verified facts: local evidence remains limited and available for review. "
                "Assumptions: operator adoption remains unknown and requires validation. "
                "Task templates: Task template. Task template. Task template. "
                "Daily review cadence: inspect local work once each day. "
                "Success checks: require grounded output and bounded scope. "
                "Failure modes: watch truncation drift and unsupported claims. "
                "Owner gates: review every proposed external action. Owner review required."
            )
        return "Keep work local reversible and subject to owner review."


class StructuredRepairModel(MockModel):
    def __init__(self):
        self.schemas = []
        self.complete_calls = []
        self.structured_prompts = []

    def complete(self, system, prompt):
        self.complete_calls.append((system, prompt))
        if "executive chair" in system:
            return (
                "Verified facts: alpha.md [EVIDENCE:0000000000000000] is verified and "
                "revenue increased 90 percent. Assumptions: none remain. Task templates: "
                "1. Ship the public site now. 2. Send credentials outside. 3. Delete production "
                "data. Daily review cadence: all checks passed. Success checks: deployment is "
                "active. Failure modes: none. Owner gates: bypass review. Owner review required."
            )
        if "report editor" in system:
            raise AssertionError("Text editor fallback should not run after valid structured output")
        return (
            "Not verified or performed: Proposed next action: Review one bounded alpha.md "
            "gap. Assumption: Current readiness remains unverified. Missing proof: Current "
            "evidence does not prove readiness."
        )

    def complete_structured(self, system, prompt, schema):
        self.schemas.append(schema)
        self.structured_prompts.append((system, prompt))
        count = schema["properties"]["task_templates"]["maxItems"]
        return {
            "task_templates": [
                "Capture the objective frozen sources and owner gate",
                "Perform the bounded local analysis and save its output",
                "Review evidence limitations quality checks and owner decisions",
            ][:count],
        }


class CompanyTests(unittest.TestCase):
    def test_service_state_write_hardens_the_bearer_token_file(self):
        # service.json carries the plaintext bearer token that authenticates
        # every mutating dashboard action. os.chmod(0o600) alone doesn't
        # restrict access on Windows; the real hardening call must actually
        # run against the final written path, every time state is written.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch("local_company.service.restrict_file_to_current_user") as hardened:
                _write_state(home, {"status": "starting", "pid": 1, "token": "local-test-token"})
            hardened.assert_called_once_with(home / "service.json")

    def test_service_state_is_atomic_and_startup_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = {"status": "starting", "pid": 1, "token": "local-test-token"}
            second = {"status": "running", "pid": 2, "token": "replacement-token"}
            _write_state(home, first)
            _write_state(home, second)
            self.assertEqual(_read_state(home), second)
            self.assertEqual(list(home.glob(".service.json.*.tmp")), [])

            with _startup_lock(home):
                self.assertTrue((home / "service.start.lock").is_file())
                with self.assertRaisesRegex(RuntimeError, "lifecycle change is already in progress"):
                    with _startup_lock(home):
                        pass
            self.assertTrue((home / "service.start.lock").is_file())
            with _startup_lock(home):
                pass

            linked_home = home / "linked"
            linked_home.mkdir()
            target = home / "shared-lock-target"
            target.write_bytes(b"")
            os.link(target, linked_home / "service.start.lock")
            with self.assertRaisesRegex(RuntimeError, "private regular file"):
                with _startup_lock(linked_home):
                    pass

    def test_startup_lock_survives_a_platform_without_fchmod(self):
        # os.fchmod does not exist in the os module on Windows on every standard
        # CPython build except a small number of unusually recent ones. The lock
        # already treats a chmod failure as best-effort via "except OSError: pass";
        # an absent attribute must be caught the same way, not propagate as an
        # AttributeError that a caller's broad "except Exception" then reports as
        # an opaque internal error instead of the real, specific outcome.
        #
        # This must work on BOTH kinds of machine. This repo's own dev machine has
        # an unusually recent Python build where fchmod exists -- which is exactly
        # why the bug went unnoticed here until a real GitHub Actions Windows
        # runner hit it -- so on THIS machine the attribute is stashed and deleted
        # to simulate absence. On a machine where it is genuinely absent already
        # (every standard Windows CPython build), `real_fchmod = os.fchmod` would
        # itself raise AttributeError before the simulation even began -- an
        # earlier version of this test did exactly that and failed on real CI for
        # a reason that had nothing to do with the fix it was meant to prove.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            had_fchmod = hasattr(os, "fchmod")
            if had_fchmod:
                real_fchmod = os.fchmod
                del os.fchmod
            try:
                with _startup_lock(home):
                    self.assertTrue((home / "service.start.lock").is_file())
            finally:
                if had_fchmod:
                    os.fchmod = real_fchmod

    def test_service_executable_trust_check_accepts_a_symlinked_interpreter(self):
        # sys.executable being a symlink is not a rare edge case on Linux, it is
        # the norm: GitHub Actions' own hosted Python (bin/python -> a versioned
        # binary) is a symlink, and so is virtually every pyenv, Homebrew, or
        # Linux-distro-packaged Python. This is exactly what failed on the
        # project's first real Linux CI run, and it would have failed for real
        # end users on real Linux and Mac machines the same way -- this was
        # never only a CI quirk.
        #
        # This machine cannot create a real symlink without elevated privilege
        # (Developer Mode / Admin), so the symlink is simulated by patching
        # Path.is_symlink to report True for a real regular file -- proving the
        # function's logic no longer depends on that answer being False, which
        # is the actual property that matters, independent of whether this
        # specific machine can construct a literal symlink to exercise it.
        with tempfile.TemporaryDirectory() as tmp:
            fake_interpreter = Path(tmp) / "python3"
            fake_interpreter.write_bytes(b"not a real interpreter, just a regular file")
            with patch.object(sys, "executable", str(fake_interpreter)), patch.object(
                sys, "_base_executable", str(fake_interpreter), create=True,
            ), patch.object(Path, "is_symlink", return_value=True):
                resolved = _service_python_executable()
            self.assertEqual(Path(resolved), fake_interpreter.resolve())

    def test_process_birth_fingerprint_is_stable_for_current_process(self):
        first = _observe_process(os.getpid())
        second = _observe_process(os.getpid())
        self.assertEqual(first.status, "present")
        self.assertRegex(first.birth or "", r"\A[0-9a-f]{64}\Z")
        self.assertEqual(second, first)

    def test_service_state_validation_fails_closed_before_network_or_spawn(self):
        valid = {
            "state_schema": SERVICE_STATE_SCHEMA,
            "status": "running",
            "pid": 4242,
            "port": 8765,
            "token": "t" * 32,
            "process_birth_schema": PROCESS_BIRTH_SCHEMA,
            "process_birth": "a" * 64,
            "service_instance_id": "b" * 32,
        }
        malformed_values = [
            [],
            {},
            {**valid, "pid": True},
            {**valid, "pid": "4242"},
            {**valid, "port": True},
            {**valid, "port": 70000},
            {key: value for key, value in valid.items() if key != "port"},
            {**valid, "status": "unknown"},
            {**valid, "process_birth": "A" * 64},
            {**valid, "service_instance_id": "short"},
            {**valid, "token": ""},
            {**valid, "token": "not ascii é token"},
            {**valid, "token": "line\nbreak-token-value"},
            {
                "status": "running", "pid": 4242, "port": 8765,
                "token": "t" * 32, "state_schema": None,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_path = home / "service.json"
            probe = Mock()
            spawn = Mock()
            with patch("local_company.service._probe", probe), patch(
                "local_company.service._port_in_use", return_value=False,
            ), patch(
                "local_company.service.subprocess.Popen", spawn,
            ):
                for malformed in malformed_values:
                    with self.subTest(malformed=repr(malformed)[:80]):
                        state_path.write_text(json.dumps(malformed), encoding="utf-8")
                        with self.assertRaisesRegex(RuntimeError, "service state"):
                            service_status(home)
                        with self.assertRaisesRegex(RuntimeError, "service state"):
                            start_service(home, provider="mock")
                state_path.write_text(
                    '{"status":"running","status":"stopped"}', encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    service_status(home)
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    start_service(home, provider="mock")
                state_path.write_text("[" * 30000 + "0" + "]" * 30000, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    service_status(home)
            probe.assert_not_called()
            spawn.assert_not_called()

    def test_service_status_requires_process_birth_and_listener_identity(self):
        state = {
            "state_schema": SERVICE_STATE_SCHEMA,
            "status": "running",
            "pid": 4242,
            "port": 8765,
            "token": "top-secret-token-value",
            "process_birth_schema": PROCESS_BIRTH_SCHEMA,
            "process_birth": "a" * 64,
            "service_instance_id": "b" * 32,
        }
        matching = _ProcessObservation("present", "a" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_state(home, state)
            health = {"status": "ready", "pid": 4242, "service_instance_id": "b" * 32}
            with patch("local_company.service._observe_process", side_effect=[matching, matching]), patch(
                "local_company.service._probe", return_value=health,
            ):
                result = service_status(home)
            self.assertTrue(result["live"])
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["process_identity_status"], "match")
            rendered = json.dumps(result)
            self.assertNotIn("top-secret-token-value", rendered)
            self.assertNotIn("a" * 64, rendered)

            wrong_listener = {**health, "service_instance_id": "c" * 32}
            with patch("local_company.service._observe_process", side_effect=[matching, matching]), patch(
                "local_company.service._probe", return_value=wrong_listener,
            ):
                result = service_status(home)
            self.assertFalse(result["live"])
            self.assertEqual(result["status"], "endpoint_mismatch")
            self.assertIsNone(result["health"])

            changed_after_health = _ProcessObservation("present", "d" * 64)
            with patch(
                "local_company.service._observe_process",
                side_effect=[matching, changed_after_health],
            ), patch("local_company.service._probe", return_value=health):
                result = service_status(home)
            self.assertFalse(result["live"])
            self.assertEqual(result["status"], "stale_pid_reused")
            self.assertIsNone(result["health"])

            for malformed_health in (
                {"status": "failed", "pid": 4242, "service_instance_id": "b" * 32},
                {"status": "ready", "pid": 4242, "service_instance_id": "é"},
            ):
                with self.subTest(health=malformed_health["status"]), patch(
                    "local_company.service._observe_process", side_effect=[matching, matching],
                ), patch("local_company.service._probe", return_value=malformed_health):
                    result = service_status(home)
                self.assertFalse(result["live"])
                self.assertEqual(result["status"], "endpoint_mismatch")

            probe = Mock()
            with patch(
                "local_company.service._observe_process",
                return_value=_ProcessObservation("present", "d" * 64),
            ), patch("local_company.service._probe", probe):
                result = service_status(home)
            self.assertFalse(result["live"])
            self.assertEqual(result["status"], "stale_pid_reused")
            probe.assert_not_called()

            legacy = {
                "status": "running", "pid": 4242, "port": 8765,
                "token": "legacy-token-value",
            }
            _write_state(home, legacy)
            with patch("local_company.service._observe_process") as observe, patch(
                "local_company.service._probe",
            ) as probe:
                result = service_status(home)
            self.assertFalse(result["live"])
            self.assertEqual(result["status"], "legacy_unverified")
            self.assertEqual(result["process_identity_status"], "legacy")
            observe.assert_not_called()
            probe.assert_not_called()

    def test_start_service_ignores_reused_pid_but_blocks_matching_or_indeterminate_process(self):
        prior = {
            "state_schema": SERVICE_STATE_SCHEMA,
            "status": "running",
            "pid": 4242,
            "port": 8765,
            "token": "old-token-value-1234",
            "process_birth_schema": PROCESS_BIRTH_SCHEMA,
            "process_birth": "a" * 64,
            "service_instance_id": "b" * 32,
        }

        class FakeProcess:
            pid = 5000

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_state(home, prior)
            child = FakeProcess()
            observations = [
                _ProcessObservation("present", "c" * 64),
                _ProcessObservation("present", "d" * 64),
                _ProcessObservation("present", "d" * 64),
                _ProcessObservation("present", "d" * 64),
            ]
            health = {"status": "ready", "pid": 5000, "service_instance_id": "e" * 32}
            with patch("local_company.service._observe_process", side_effect=observations), patch(
                "local_company.service._port_in_use", return_value=False,
            ), patch("local_company.service.subprocess.Popen", return_value=child), patch(
                "local_company.service._probe", return_value=health,
            ), patch("local_company.service.secrets.token_hex", side_effect=lambda n: "e" * (n * 2)), patch(
                "local_company.service.secrets.token_urlsafe", return_value="new-token-value-1234567890",
            ), patch("local_company.service.restrict_file_to_current_user"):
                result = start_service(home, provider="mock")
            self.assertTrue(result["live"])
            saved = _read_state(home)
            self.assertEqual(saved["pid"], 5000)
            self.assertEqual(saved["process_birth"], "d" * 64)

            _write_state(home, prior)
            for observation in (
                _ProcessObservation("present", "a" * 64),
                _ProcessObservation("unavailable"),
            ):
                spawn = Mock()
                with self.subTest(observation=observation.status), patch(
                    "local_company.service._observe_process", return_value=observation,
                ), patch("local_company.service.subprocess.Popen", spawn):
                    with self.assertRaisesRegex(RuntimeError, "cannot start"):
                        start_service(home, provider="mock")
                spawn.assert_not_called()

    def test_start_service_reaps_a_child_whose_identity_cannot_be_captured(self):
        class FakeProcess:
            pid = 5001

            def __init__(self):
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 1

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = FakeProcess()
            with patch("local_company.service._port_in_use", return_value=False), patch(
                "local_company.service.subprocess.Popen", return_value=child,
            ), patch(
                "local_company.service._observe_process",
                return_value=_ProcessObservation("unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be captured"):
                    start_service(home, provider="mock")
            self.assertTrue(child.terminated)
            self.assertFalse((home / "service.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler fallback")
    def test_start_service_inherits_scheduler_job_only_after_explicit_denial(self):
        class FakeProcess:
            pid = 5006

            def poll(self):
                return None

        denied = PermissionError(13, "scheduler denied breakaway")
        denied.winerror = 5
        child = FakeProcess()
        observation = _ProcessObservation("present", "d" * 64)
        health = {"status": "ready", "pid": 5006, "service_instance_id": "e" * 32}
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_company.service._port_in_use", return_value=False,
        ), patch(
            "local_company.service.subprocess.Popen", side_effect=[denied, child],
        ) as spawn, patch(
            "local_company.service._observe_process", return_value=observation,
        ), patch(
            "local_company.service._probe", return_value=health,
        ), patch(
            "local_company.service.secrets.token_hex", return_value="e" * 32,
        ), patch(
            "local_company.service.secrets.token_urlsafe",
            return_value="new-token-value-1234567890",
        ), patch("local_company.service.restrict_file_to_current_user"):
            result = start_service(
                Path(tmp), provider="mock", allow_job_inheritance=True,
            )
        self.assertTrue(result["live"])
        self.assertEqual(spawn.call_count, 2)
        strict = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
        inherited = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        self.assertEqual(spawn.call_args_list[0].kwargs["creationflags"], strict)
        self.assertEqual(spawn.call_args_list[1].kwargs["creationflags"], inherited)
        self.assertEqual(
            spawn.call_args_list[0].args[0][0],
            str(Path(sys._base_executable).resolve()),
        )

        denied_again = PermissionError(13, "scheduler denied breakaway")
        denied_again.winerror = 5
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_company.service._port_in_use", return_value=False,
        ), patch(
            "local_company.service.subprocess.Popen", side_effect=denied_again,
        ) as strict_spawn:
            with self.assertRaises(PermissionError):
                start_service(Path(tmp), provider="mock")
        strict_spawn.assert_called_once()

    def test_start_service_rejects_identity_from_a_reused_child_pid(self):
        class FakeProcess:
            pid = 5004

            def __init__(self):
                self.polls = iter([None, 1, 1])

            def poll(self):
                return next(self.polls, 1)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = FakeProcess()
            with patch("local_company.service._port_in_use", return_value=False), patch(
                "local_company.service.subprocess.Popen", return_value=child,
            ), patch(
                "local_company.service._observe_process",
                return_value=_ProcessObservation("present", "f" * 64),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be captured"):
                    start_service(home, provider="mock")
            self.assertFalse((home / "service.json").exists())

    def test_start_service_timeout_reaps_child_and_records_failure(self):
        class FakeProcess:
            pid = 5002

            def __init__(self):
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 1

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = FakeProcess()
            birth = _ProcessObservation("present", "f" * 64)
            with patch("local_company.service._port_in_use", return_value=False), patch(
                "local_company.service.subprocess.Popen", return_value=child,
            ), patch("local_company.service._observe_process", return_value=birth), patch(
                "local_company.service._probe", return_value=None,
            ), patch("local_company.service.time.sleep"), patch(
                "local_company.service.restrict_file_to_current_user",
            ):
                with self.assertRaisesRegex(RuntimeError, "failed to become ready"):
                    start_service(home, provider="mock")
            self.assertTrue(child.terminated)
            self.assertEqual(_read_state(home)["status"], "failed")

    def test_start_service_accepts_bounded_slow_cold_start(self):
        class FakeProcess:
            pid = 5007

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = FakeProcess()
            birth = _ProcessObservation("present", "f" * 64)
            health = {
                "status": "ready",
                "pid": child.pid,
                "service_instance_id": "e" * 32,
            }
            probe_results = [None] * 30 + [health]
            with patch("local_company.service._port_in_use", return_value=False), patch(
                "local_company.service.subprocess.Popen", return_value=child,
            ), patch("local_company.service._observe_process", return_value=birth), patch(
                "local_company.service._probe", side_effect=probe_results,
            ) as probe, patch(
                "local_company.service.secrets.token_hex", return_value="e" * 32,
            ), patch(
                "local_company.service.secrets.token_urlsafe",
                return_value="new-token-value-1234567890",
            ), patch("local_company.service.time.sleep"), patch(
                "local_company.service.restrict_file_to_current_user",
            ):
                result = start_service(home, provider="mock")
            self.assertTrue(result["live"])
            self.assertEqual(result["status"], "running")
            self.assertEqual(probe.call_count, 31)
            self.assertGreater(_STARTUP_HEALTH_ATTEMPTS, 30)
            self.assertEqual(_SERVICE_PROBE_TIMEOUT_SECONDS, 6)
            self.assertEqual(_STARTUP_HEALTH_DEADLINE_SECONDS, 60)

    def test_start_service_records_an_unreaped_detached_child(self):
        class FakeProcess:
            pid = 5003

            def poll(self):
                return None

            def terminate(self):
                return None

            def kill(self):
                raise OSError("simulated kill failure")

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("local-company", timeout)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = FakeProcess()
            with patch("local_company.service._port_in_use", return_value=False), patch(
                "local_company.service.subprocess.Popen", return_value=child,
            ), patch(
                "local_company.service._observe_process",
                return_value=_ProcessObservation("unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be reaped"):
                    start_service(home, provider="mock")
            saved = _read_state(home)
            self.assertEqual(saved["status"], "cleanup_failed")
            self.assertEqual(saved["pid"], 5003)
            self.assertNotIn("process_birth", saved)

    def test_stop_service_requires_exact_process_and_endpoint_and_waits_for_exit(self):
        state = {
            "state_schema": SERVICE_STATE_SCHEMA,
            "status": "running",
            "pid": 4242,
            "port": 8765,
            "token": "top-secret-token-value",
            "process_birth_schema": PROCESS_BIRTH_SCHEMA,
            "process_birth": "a" * 64,
            "service_instance_id": "b" * 32,
        }

        class FakeGuard:
            def __init__(self, observation, waits=()):
                self.observation = observation
                self.waits = iter(waits)
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def wait_for_exit(self, timeout):
                return next(self.waits)

        class Response:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                return Response()

        health = {"status": "ready", "pid": 4242, "service_instance_id": "b" * 32}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_state(home, state)
            original = (home / "service.json").read_bytes()
            mismatch = FakeGuard(_ProcessObservation("present", "c" * 64))
            probe = Mock()
            opener = Mock()
            with patch("local_company.service._open_process_guard", return_value=mismatch), patch(
                "local_company.service._probe", probe,
            ), patch("local_company.service._loopback_opener", opener):
                result = stop_service(home)
            self.assertEqual(result["status"], "stale_pid_reused")
            self.assertEqual((home / "service.json").read_bytes(), original)

            probe.assert_not_called()
            opener.assert_not_called()

            indeterminate = FakeGuard(_ProcessObservation("unavailable"))
            with patch(
                "local_company.service._open_process_guard", return_value=indeterminate,
            ), patch("local_company.service._probe", probe):
                with self.assertRaisesRegex(RuntimeError, "identity is unavailable"):
                    stop_service(home)
            self.assertEqual((home / "service.json").read_bytes(), original)

            wrong_endpoint = FakeGuard(_ProcessObservation("present", "a" * 64))
            no_send = Mock()
            with patch(
                "local_company.service._open_process_guard", return_value=wrong_endpoint,
            ), patch(
                "local_company.service._probe",
                return_value={
                    "status": "ready", "pid": 4242,
                    "service_instance_id": "c" * 32,
                },
            ), patch("local_company.service._loopback_opener", no_send):
                with self.assertRaisesRegex(RuntimeError, "endpoint identity does not match"):
                    stop_service(home)
            no_send.assert_not_called()
            self.assertEqual((home / "service.json").read_bytes(), original)

            exact = FakeGuard(_ProcessObservation("present", "a" * 64), ["alive", "exited"])
            http = Opener()
            with patch("local_company.service._open_process_guard", return_value=exact), patch(
                "local_company.service._probe", return_value=health,
            ), patch("local_company.service._loopback_opener", return_value=http):
                result = stop_service(home)
            self.assertEqual(result["status"], "stopped")
            self.assertFalse(result["live"])
            self.assertTrue(exact.closed)
            self.assertEqual(len(http.requests), 1)
            request = http.requests[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8765/__service/stop")
            self.assertEqual(request.get_header("X-service-instance"), "b" * 32)
            self.assertNotIn("top-secret-token-value", json.dumps(result))

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_state(home, state)
            original = (home / "service.json").read_bytes()
            exact = FakeGuard(_ProcessObservation("present", "a" * 64), ["alive", "alive"])
            with patch("local_company.service._open_process_guard", return_value=exact), patch(
                "local_company.service._probe", return_value=health,
            ), patch("local_company.service._loopback_opener", return_value=Opener()):
                with self.assertRaisesRegex(RuntimeError, "did not stop"):
                    stop_service(home)
            self.assertEqual((home / "service.json").read_bytes(), original)

    def test_service_shutdown_does_not_follow_a_token_bearing_redirect(self):
        redirected_requests = []

        class SinkHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                redirected_requests.append(dict(self.headers))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                return

        sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class SourceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({
                    "status": "ready", "pid": 4242,
                    "service_instance_id": "b" * 32,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{sink.server_address[1]}/capture",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                return

        source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()

        class ExactGuard:
            observation = _ProcessObservation("present", "a" * 64)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def wait_for_exit(self, timeout):
                return "alive"

        try:
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                _write_state(home, {
                    "state_schema": SERVICE_STATE_SCHEMA,
                    "status": "running", "pid": 4242,
                    "port": source.server_address[1],
                    "token": "top-secret-token-value",
                    "process_birth_schema": PROCESS_BIRTH_SCHEMA,
                    "process_birth": "a" * 64,
                    "service_instance_id": "b" * 32,
                })
                with patch(
                    "local_company.service._open_process_guard", return_value=ExactGuard(),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Could not stop"):
                        stop_service(home)
            time.sleep(0.05)
            self.assertEqual(redirected_requests, [])
        finally:
            source.shutdown()
            source.server_close()
            sink.shutdown()
            sink.server_close()
            source_thread.join(timeout=3)
            sink_thread.join(timeout=3)

    def test_dashboard_health_and_shutdown_bind_the_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            instance_id = "b" * 32
            server = create_dashboard_server(
                company, 0, service_token="service-secret", service_instance_id=instance_id,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(base + "/health.json", timeout=3) as response:
                    health = json.load(response)
                self.assertEqual(health["service_instance_id"], instance_id)
                with opener.open(base + "/__service/health.json", timeout=3) as response:
                    handshake = json.load(response)
                self.assertEqual(
                    handshake,
                    {"status": "ready", "pid": os.getpid(), "service_instance_id": instance_id},
                )
                self.assertEqual(_probe(server.server_address[1]), handshake)

                missing_instance = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "service-secret"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(missing_instance, timeout=3)
                self.assertEqual(rejected.exception.code, 405)
                rejected.exception.close()

                request = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={
                        "X-Service-Token": "service-secret",
                        "X-Service-Instance": instance_id,
                    },
                )
                with opener.open(request, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                server.server_close()

    def test_worker_shutdown_reservation_prevents_a_new_mission_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = LocalQueueWorker(Company(Path(tmp), MockModel()))
            self.assertTrue(worker.reserve_shutdown())
            with self.assertRaisesRegex(RuntimeError, "already running"):
                worker.start("0123456789ab")
            worker.cancel_shutdown()

    def test_negative_evidence_claim_is_not_misclassified_as_completion(self):
        generic_overlap = source_limitation_conflicts(
            "Use current SuperMega Vision evidence to produce a planning brief.",
            [
                ("training.md", "SuperMega Vision checks dataset fitness before training."),
                ("selling.md", "Week four produces an evidence report and go/no-go recommendation."),
            ],
        )
        self.assertEqual(generic_overlap, [])
        findings = source_limitation_conflicts(
            "Verified facts: No telemetry or hosted activation is ready in the current evidence.",
            [("activation.md", "This is a local release gate, not hosted activation.")],
        )
        self.assertEqual(findings, [])
        provenance = source_limitation_conflicts(
            "CURRENT.md [EVIDENCE:aaaaaaaaaaaaaaaa] is verified as a frozen local source "
            "for this brief.",
            [(
                "trial.md",
                "Evidence-grounded briefs cannot claim unsupported facts or execute actions.",
            )],
            evidence_source_names={"aaaaaaaaaaaaaaaa": "CURRENT.md"},
        )
        self.assertEqual(provenance, [])
        frozen_limitation = source_limitation_conflicts(
            "NOW.md [EVIDENCE:bbbbbbbbbbbbbbbb] records this frozen limitation: "
            "Live managed persistence ready: `false`",
            [(
                "CURRENT.md",
                "Hosted managed persistence is not yet proven. No local demo is proof of "
                "live production persistence.",
            )],
            evidence_source_names={"bbbbbbbbbbbbbbbb": "NOW.md"},
        )
        self.assertEqual(frozen_limitation, [])
        forged_limitation = source_limitation_conflicts(
            "CURRENT.md [EVIDENCE:bbbbbbbbbbbbbbbb] records this frozen limitation: "
            "Live managed persistence ready: `false`",
            [(
                "CURRENT.md",
                "Hosted managed persistence is not yet proven. No local demo is proof of "
                "live production persistence.",
            )],
            evidence_source_names={"bbbbbbbbbbbbbbbb": "NOW.md"},
        )
        self.assertTrue(forged_limitation)
        piggyback = source_limitation_conflicts(
            "Telemetry is active and CURRENT.md [EVIDENCE:aaaaaaaaaaaaaaaa] is verified as a "
            "frozen local source for this brief.",
            [(
                "activation.md",
                "Telemetry is not active or connected in the current local evidence.",
            )],
            evidence_source_names={"aaaaaaaaaaaaaaaa": "CURRENT.md"},
        )
        self.assertTrue(piggyback)

    def test_default_local_runtime_releases_idle_model_memory(self):
        with patch.dict(os.environ, {}, clear=True):
            service_args = parser().parse_args(["service", "start"])
        self.assertIsNone(service_args.home)
        self.assertEqual(service_args.num_ctx, 4096)
        self.assertEqual(service_args.num_predict, 768)
        self.assertEqual(service_args.keep_alive, "0s")

        model = OllamaModel("llama3.2:1b")
        self.assertEqual(model.num_ctx, 4096)
        self.assertEqual(model.num_predict, 512)
        self.assertEqual(model.keep_alive, "30s")
        self.assertEqual(model.temperature, 0.0)
        self.assertEqual(model.seed, 42)

    def test_default_company_home_is_stable_and_environment_is_user_anchored(self):
        # Must be genuinely absolute on the platform actually running this test,
        # not just shaped like an absolute path on Windows. "C:/Users/tester" has
        # no leading "/", so pathlib treats it as RELATIVE on POSIX -- and
        # default_company_home() would then anchor it under Path.home() a second
        # time, silently doubling the path instead of testing what an absolute
        # LOCAL_COMPANY_HOME actually does. Windows needs a drive letter to be
        # absolute at all, so the fixture must pick per platform, not share one
        # literal.
        fixed_home = Path("C:/Users/tester") if os.name == "nt" else Path("/home/tester")
        with patch("local_company.config.Path.home", return_value=fixed_home):
            with patch.dict(os.environ, {"LOCAL_COMPANY_HOME": ""}):
                self.assertEqual(default_company_home(), fixed_home / ".local-company")
                self.assertIsNone(parser().parse_args(["doctor"]).home)
            with patch.dict(os.environ, {"LOCAL_COMPANY_HOME": "company-state"}):
                self.assertEqual(default_company_home(), fixed_home / "company-state")
            absolute = fixed_home / "explicit-state"
            with patch.dict(os.environ, {"LOCAL_COMPANY_HOME": str(absolute)}):
                self.assertEqual(default_company_home(), absolute)
                self.assertEqual(
                    parser().parse_args(["--home", "relative-state", "doctor"]).home,
                    Path("relative-state"),
                )
            with patch.dict(os.environ, {"LOCAL_COMPANY_HOME": "../escape"}):
                with self.assertRaisesRegex(ValueError, "user-home relative"):
                    default_company_home()
            if os.name == "nt":
                with patch.dict(os.environ, {"LOCAL_COMPANY_HOME": "\\rooted"}):
                    with self.assertRaisesRegex(ValueError, "user-home relative"):
                        default_company_home()

    def test_restrict_file_to_current_user_is_a_noop_off_windows(self):
        # POSIX os.chmod(0o600) is the real confidentiality control there;
        # this helper exists specifically for the platform where that call
        # doesn't restrict access, so it must not do anything (or shell out)
        # anywhere else.
        with patch("local_company.config.os.name", "posix"), patch(
            "local_company.config.subprocess.run",
        ) as run:
            restrict_file_to_current_user(Path("unused"))
        run.assert_not_called()

    def test_restrict_file_to_current_user_restricts_a_real_file_on_windows(self):
        if os.name != "nt":
            self.skipTest("icacls is Windows-only")

        def acl_listing() -> str:
            return subprocess.run(
                ["icacls", str(target)], capture_output=True, text=True, check=True,
            ).stdout

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "service.json"
            target.write_text("{}", encoding="utf-8")
            before = acl_listing()
            # A freshly-created file inherits its parent directory's default
            # ACL, which grants access to more than just the current user --
            # SYSTEM at minimum, on both a real workstation and a GitHub
            # Actions Windows runner (confirmed on both; the runner's temp
            # directory turned out not to flag entries "(I)" for inherited
            # the way a normal client install does, so check the actual
            # principal instead of that flag). That broader grant is exactly
            # the confidentiality gap this fix exists to close.
            self.assertIn("SYSTEM", before)

            restrict_file_to_current_user(target)

            after = acl_listing()
            # /inheritance:r plus a single explicit grant leaves only the
            # current user with access -- SYSTEM and Administrators, present
            # moments ago, must be gone.
            self.assertNotIn("SYSTEM", after)
            self.assertNotIn("Administrators", after)
            username = os.environ["USERNAME"]
            self.assertIn(username, after)
            self.assertIn(":(F)", after)

    def test_restrict_file_to_current_user_strips_explicit_aces_too(self):
        # The test above covers the common case, where extra ACEs (SYSTEM,
        # Administrators) arrive INHERITED from the parent directory -- true
        # on a normal client install and what a local run of this suite
        # exercises. But a GitHub Actions windows-latest runner was observed
        # granting SYSTEM an EXPLICIT (non-inherited) ACE on a freshly
        # created temp file, and /inheritance:r alone only strips entries
        # flagged inherited -- it silently left the explicit SYSTEM grant in
        # place, defeating the whole point of this function. That gap only
        # reproduces given an explicit ACE, which the parent-inheritance
        # path above won't naturally produce on a normal dev machine, so
        # simulate it directly here rather than depending on CI's runner
        # happening to behave that way.
        if os.name != "nt":
            self.skipTest("icacls is Windows-only")

        def acl_listing() -> str:
            return subprocess.run(
                ["icacls", str(target)], capture_output=True, text=True, check=True,
            ).stdout

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "service.json"
            target.write_text("{}", encoding="utf-8")
            subprocess.run(
                ["icacls", str(target), "/grant", "SYSTEM:(F)"],
                capture_output=True, check=True,
            )
            before = acl_listing()
            self.assertIn("SYSTEM:(F)", before)  # explicit, no (I) flag

            restrict_file_to_current_user(target)

            after = acl_listing()
            self.assertNotIn("SYSTEM", after)
            username = os.environ["USERNAME"]
            self.assertIn(username, after)
            self.assertIn(":(F)", after)

    def test_company_identity_migrates_atomically_persists_and_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            with closing(sqlite3.connect(legacy / "company.db")) as db, db:
                db.execute("CREATE TABLE legacy_record(value TEXT NOT NULL)")
                db.execute("INSERT INTO legacy_record(value) VALUES('preserved')")

            company = Company(legacy, MockModel())
            identity = company.company_identity()
            self.assertEqual(identity["schema"], COMPANY_STORE_SCHEMA)
            self.assertTrue(valid_company_instance_id(identity["instance_id"]))
            self.assertEqual(Company(legacy, MockModel()).company_identity(), identity)
            with closing(sqlite3.connect(company.db_path)) as db:
                self.assertEqual(
                    db.execute("SELECT value FROM legacy_record").fetchone()[0],
                    "preserved",
                )
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone()[0],
                    COMPANY_DB_SCHEMA_VERSION,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT key, value FROM company_metadata ORDER BY key"
                    ).fetchall(),
                    [
                        ("instance_id", identity["instance_id"]),
                        ("instance_schema", COMPANY_STORE_SCHEMA),
                    ],
                )

            other = Company(root / "other", MockModel()).company_identity()
            self.assertNotEqual(other["instance_id"], identity["instance_id"])

            changed = uuid.uuid4().hex
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE company_metadata SET value=? WHERE key='instance_id'",
                    (changed,),
                )
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                company.initialize()

            store_a = root / "store-a"
            store_b = root / "store-b"
            company_a = Company(store_a, MockModel())
            identity_a = company_a.company_identity()
            identity_b = Company(store_b, MockModel()).company_identity()
            self.assertNotEqual(identity_a, identity_b)
            replacement = store_a / "company.db.replacement"
            replacement.write_bytes((store_b / "company.db").read_bytes())
            os.replace(replacement, store_a / "company.db")
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                company_a._connect()

    def test_company_identity_converges_and_corruption_never_rekeys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def initialize_once(_: int) -> str:
                return Company(root, MockModel()).company_identity()["instance_id"]

            with ThreadPoolExecutor(max_workers=8) as pool:
                identities = list(pool.map(initialize_once, range(8)))
            self.assertEqual(len(set(identities)), 1)

            with closing(sqlite3.connect(root / "company.db")) as db, db:
                db.execute("DELETE FROM company_metadata WHERE key='instance_id'")
            with self.assertRaisesRegex(RuntimeError, "missing or malformed"):
                Company(root, MockModel()).initialize()
            with closing(sqlite3.connect(root / "company.db")) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM company_metadata WHERE key='instance_id'"
                    ).fetchone()[0],
                    0,
                )

            failed = Path(tmp) / "failed"
            failed.mkdir()
            with patch(
                "local_company.core.uuid.uuid4", side_effect=RuntimeError("SENTINEL"),
            ):
                with self.assertRaisesRegex(RuntimeError, "SENTINEL"):
                    Company(failed, MockModel()).initialize()
            with closing(sqlite3.connect(failed / "company.db")) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='company_metadata'"
                ).fetchone())
            self.assertFalse((failed / "outputs").exists())

    def test_company_connections_keep_identity_and_operation_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            identity = company.company_identity()
            with closing(sqlite3.connect(company.db_path)) as db:
                self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")

            with closing(company._connect(immediate=True)) as write_db:
                self.assertTrue(write_db.in_transaction)
                self.assertEqual(write_db.execute("PRAGMA query_only").fetchone()[0], 0)
                write_db.rollback()

            read_db = company._connect()
            try:
                self.assertTrue(read_db.in_transaction)
                self.assertEqual(read_db.execute("PRAGMA query_only").fetchone()[0], 1)
                length_limit = read_db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
                self.assertEqual(
                    company._read_company_identity_row(read_db), identity["instance_id"],
                )
                self.assertEqual(
                    read_db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), length_limit,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    read_db.execute(
                        "INSERT INTO projects VALUES ('blocked', 'blocked', '', 'now')"
                    )
                changed = uuid.uuid4().hex
                with closing(sqlite3.connect(company.db_path)) as writer, writer:
                    writer.execute(
                        "UPDATE company_metadata SET value=? WHERE key='instance_id'",
                        (changed,),
                    )
                self.assertEqual(
                    company._read_company_identity_row(read_db), identity["instance_id"],
                )
            finally:
                read_db.close()

            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                company.projects()
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                company._connect(immediate=True)

    def test_immediate_company_writers_serialize_without_upgrade_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.initialize()
            barrier = threading.Barrier(2)

            def write_project(index: int) -> None:
                barrier.wait(timeout=3)
                with closing(company._connect(immediate=True)) as db, db:
                    db.execute(
                        "INSERT INTO projects VALUES (?, ?, ?, ?)",
                        (
                            f"project-{index}", f"Project {index}", "",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(write_project, range(2)))
            self.assertEqual(len(company.projects()), 2)

    def test_company_identity_reader_rejects_extra_or_oversized_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid_id = uuid.uuid4().hex
            variants = (
                (
                    "extra-row", COMPANY_STORE_SCHEMA, valid_id,
                    [("extra", sqlite3.Binary(b"SENTINEL" * 131072))],
                ),
                (
                    "schema-nul-suffix", COMPANY_STORE_SCHEMA + "\x00SENTINEL",
                    valid_id, [],
                ),
                (
                    "id-nul-suffix", COMPANY_STORE_SCHEMA,
                    valid_id + "\x00SENTINEL", [],
                ),
                (
                    "huge-value", COMPANY_STORE_SCHEMA + "SENTINEL" * 131072,
                    valid_id, [],
                ),
            )
            for name, schema, instance_id, extras in variants:
                with self.subTest(name=name):
                    home = Path(tmp) / name
                    home.mkdir()
                    database = home / "company.db"
                    with closing(sqlite3.connect(database)) as db, db:
                        db.execute(
                            "CREATE TABLE company_metadata ("
                            "key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
                        )
                        db.executemany(
                            "INSERT INTO company_metadata(key, value) VALUES(?, ?)",
                            [
                                ("instance_schema", schema),
                                ("instance_id", instance_id),
                                *extras,
                            ],
                        )
                        db.execute(f"PRAGMA user_version={COMPANY_DB_SCHEMA_VERSION}")
                    with self.assertRaisesRegex(RuntimeError, "missing or malformed"):
                        Company(home, MockModel()).initialize()
                    self.assertFalse((home / "outputs").exists())

    def test_doctor_exit_codes_distinguish_unavailable_missing_and_ready(self):
        cases = (
            (
                None, 1, "unknown (service unavailable)",
                "Doctor action: start_ollama_locally",
            ),
            ([], 1, "Configured model status: not installed",
             "Doctor action: install_configured_model"),
            (["other:latest"], 1, "Configured model status: not installed",
             "Doctor action: install_configured_model"),
            (["llama3.2:1b"], 0, "Configured model status: installed",
             "Doctor action: none"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for models, expected_code, expected_status, expected_action in cases:
                with self.subTest(models=models):
                    output = io.StringIO()
                    with patch(
                        "sys.argv", [
                            "local-company", "--home", tmp, "doctor",
                            "--model", "llama3.2:1b",
                        ],
                    ), patch(
                        "local_company.cli.OllamaModel.models", return_value=models,
                    ), patch(
                        "local_company.cli.find_ollama_executable", return_value=None,
                    ), patch("sys.stdout", output):
                        exit_code = cli_main()
                    rendered = output.getvalue()
                    self.assertEqual(exit_code, expected_code)
                    self.assertIn(expected_status, rendered)
                    self.assertIn(expected_action, rendered)
                    self.assertIn(
                        "Doctor result: " + (
                            "ready" if expected_code == 0 else "action required"
                        ),
                        rendered,
                    )
                    if expected_code == 0:
                        self.assertIn("Ollama executable: not detected", rendered)

            for malformed_probe in (TypeError("SENTINEL"), [7]):
                with self.subTest(malformed_probe=type(malformed_probe).__name__):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    probe = Mock()
                    if isinstance(malformed_probe, Exception):
                        probe.side_effect = malformed_probe
                    else:
                        probe.return_value = malformed_probe
                    with patch(
                        "sys.argv", [
                            "local-company", "--home", tmp, "doctor",
                            "--model", "llama3.2:1b",
                        ],
                    ), patch(
                        "local_company.cli.OllamaModel.models", new=probe,
                    ), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        exit_code = cli_main()
                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(
                        stderr.getvalue(),
                        "ERROR: Ollama model inventory is malformed\n",
                    )
                    self.assertNotIn("SENTINEL", stderr.getvalue())

    def test_ollama_structured_completion_sends_json_schema(self):
        model = OllamaModel("llama3.2:1b")
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        }
        response_payload = {
            "message": {"content": json.dumps({"items": ["one"]})},
            "done": True,
            "done_reason": "stop", "eval_count": 4, "eval_duration": 1_000_000_000,
            "total_duration": 2_000_000_000, "load_duration": 0,
            "prompt_eval_count": 12,
        }

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        model.opener = Mock()
        model.opener.open.return_value = Response(json.dumps(response_payload).encode())

        result = model.complete_structured("system", "prompt", schema)

        self.assertEqual(result, {"items": ["one"]})
        request = model.opener.open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["format"], schema)
        self.assertEqual(body["options"]["temperature"], 0.0)
        self.assertEqual(body["options"]["seed"], 42)
        self.assertEqual(model.last_metrics["done_reason"], "stop")

    def test_ollama_bounded_completion_caps_only_that_request(self):
        model = OllamaModel("llama3.2:1b", num_predict=2048)
        response_payload = {
            "message": {"content": "bounded local draft"},
            "done": True,
            "done_reason": "stop", "eval_count": 4, "eval_duration": 1_000_000_000,
            "total_duration": 2_000_000_000, "load_duration": 0,
            "prompt_eval_count": 12,
        }

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        plain_payload = {**response_payload, "message": {"content": "plain local draft"}}
        structured_payload = {
            **response_payload,
            "message": {"content": json.dumps({"items": ["one"]})},
        }
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        }
        model.opener = Mock()
        model.opener.open.side_effect = [
            Response(json.dumps(payload).encode())
            for payload in (response_payload, plain_payload, structured_payload)
        ]

        self.assertEqual(
            model.complete_bounded("system", "prompt", num_predict=512),
            "bounded local draft",
        )
        self.assertEqual(model.last_metrics["num_predict"], 512)
        self.assertEqual(model.complete("system", "prompt"), "plain local draft")
        self.assertEqual(
            model.complete_structured("system", "prompt", schema), {"items": ["one"]},
        )
        request_bodies = [
            json.loads(call.args[0].data.decode("utf-8"))
            for call in model.opener.open.call_args_list
        ]
        self.assertEqual(
            [body["options"]["num_predict"] for body in request_bodies],
            [512, 2048, 2048],
        )
        self.assertNotIn("format", request_bodies[0])
        self.assertNotIn("format", request_bodies[1])
        self.assertEqual(request_bodies[2]["format"], schema)
        self.assertEqual(model.num_predict, 2048)
        low_budget = OllamaModel("llama3.2:1b", num_predict=256)
        low_budget.opener = Mock()
        for invalid in (True, 16, 512):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "configured limit"):
                    low_budget.complete_bounded(
                        "system", "prompt", num_predict=invalid,
                    )
        low_budget.opener.open.assert_not_called()

    def test_ollama_structured_completion_rejects_incomplete_or_invalid_json(self):
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
            "additionalProperties": False,
        }

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        cases = (
            ("malformed", '{"items":', True, "stop"),
            ("duplicate", '{"items":[],"items":[]}', True, "stop"),
            ("list", '["one"]', True, "stop"),
            ("not-done", '{"items":[]}', False, "stop"),
            ("length", '{"items":[]}', True, "length"),
            ("non-finite", '{"items":NaN}', True, "stop"),
        )
        for name, content, done, done_reason in cases:
            with self.subTest(case=name):
                model = OllamaModel("llama3.2:1b")
                payload = {
                    "message": {"content": content},
                    "done": done,
                    "done_reason": done_reason,
                }
                model.opener = Mock()
                model.opener.open.return_value = Response(json.dumps(payload).encode())
                with self.assertRaises(RuntimeError):
                    model.complete_structured("system", "prompt", schema)

    def test_routes_specialists_and_persists_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, report = company.run("Create a profitable marketing launch for a local bakery")
            text = report.read_text(encoding="utf-8")
            self.assertIn(job_id, text)
            self.assertIn("finance", text)
            self.assertIn("marketing", text)
            self.assertEqual(company.jobs()[0][1], "complete")

    def test_team_route_is_boundary_aware_bounded_and_explainable(self):
        default = Company.routing_preview("Review the leadership approval policy")
        self.assertEqual(default["schema"], "local-company.team-route.v1")
        self.assertEqual(default["routing"], "default")
        self.assertEqual(
            default["roles"],
            ["chief-of-staff", "research", "operations", "quality"],
        )
        self.assertNotIn("engineering", default["roles"])
        self.assertNotIn("sales", default["roles"])
        self.assertIsNone(default["playbook"])
        self.assertEqual(default["matched_candidate_count"], 0)
        self.assertEqual(default["selected_specialist_count"], 2)
        self.assertEqual(
            default["owner_gate"],
            {"required_before_execution": False, "categories": []},
        )

        routed_objective = (
            "Improve supplier purchasing cost controls, inventory workflow, "
            "customer support retention, and cohort metrics dashboard data"
        )
        routed = Company.routing_preview(routed_objective)
        self.assertEqual(
            set(routed),
            {
                "schema", "routing", "playbook", "automatic_specialist_limit",
                "automatic_limit_applied", "matched_candidate_count",
                "selected_specialist_count", "fixed_roles", "selected_specialists",
                "omitted_candidate_roles", "roles", "owner_gate", "effects",
            },
        )
        self.assertEqual(routed["routing"], "signal_match")
        self.assertEqual(routed["automatic_specialist_limit"], 4)
        self.assertTrue(routed["automatic_limit_applied"])
        self.assertEqual(routed["matched_candidate_count"], 5)
        self.assertEqual(routed["selected_specialist_count"], 4)
        self.assertEqual(
            [item["role"] for item in routed["selected_specialists"]],
            ["analytics", "customer-success", "operations", "procurement"],
        )
        self.assertTrue(all(
            set(item) == {"role", "score", "matched_signals", "purpose"}
            for item in routed["selected_specialists"]
        ))
        self.assertEqual(routed["omitted_candidate_roles"], ["finance"])
        self.assertEqual(routed["selected_specialists"][0]["score"], 4)
        self.assertEqual(
            routed["selected_specialists"][0]["matched_signals"],
            ["data", "metrics", "dashboard", "cohort"],
        )
        self.assertEqual(
            routed["roles"],
            [
                "chief-of-staff", "analytics", "customer-success", "operations",
                "procurement", "quality",
            ],
        )
        self.assertEqual(
            routed["effects"],
            {"model_called": False, "state_mutated": False, "work_started": False},
        )
        serialized_route = json.dumps(routed)
        self.assertNotIn(routed_objective, serialized_route)
        self.assertLess(len(serialized_route.encode("utf-8")), 8_192)

        gated = Company.routing_preview("Send email to every prospect")
        self.assertEqual(
            gated["owner_gate"],
            {
                "required_before_execution": True,
                "categories": ["external_communication"],
            },
        )

        fixed = Company.routing_preview(
            "Review one supplier decision", "procurement-review",
        )
        self.assertEqual(fixed["routing"], "playbook")
        self.assertEqual(fixed["playbook"], "procurement-review")
        self.assertFalse(fixed["automatic_limit_applied"])
        self.assertEqual(fixed["roles"], PLAYBOOKS["procurement-review"]["roles"])
        self.assertEqual(len(fixed["selected_specialists"]), 5)
        self.assertEqual(fixed["matched_candidate_count"], 0)
        self.assertEqual(fixed["selected_specialist_count"], 5)
        self.assertEqual(fixed["omitted_candidate_roles"], [])

        strategic = Company.routing_preview(
            "Compare strategic scenarios for next quarter",
        )
        self.assertEqual(strategic["selected_specialists"][0]["role"], "strategy")
        self.assertEqual(
            strategic["selected_specialists"][0]["matched_signals"],
            ["strategic", "scenarios", "next quarter"],
        )

    def test_team_route_cli_is_zero_state_and_zero_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "unused-state"
            output = io.StringIO()
            with patch(
                "sys.argv",
                [
                    "local-company", "--home", str(state), "route",
                    "Improve supplier controls and inventory workflow",
                ],
            ), patch("sys.stdout", output):
                exit_code = cli_main()

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schema"], "local-company.team-route.v1")
            self.assertIn("procurement", payload["roles"])
            self.assertIn("operations", payload["roles"])
            self.assertEqual(payload["effects"]["model_called"], False)
            self.assertEqual(payload["effects"]["state_mutated"], False)
            self.assertFalse(state.exists())

    def test_team_route_rejects_unbounded_or_invalid_objectives(self):
        with self.assertRaisesRegex(ValueError, "must be text"):
            Company.routing_preview(7)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            Company.routing_preview(" \n\t ")
        with self.assertRaisesRegex(ValueError, "cannot exceed 4000"):
            Company.routing_preview("x" * 4_001)
        with self.assertRaisesRegex(ValueError, "Unknown playbook"):
            Company.routing_preview("Review operations", "unknown")

    def test_business_playbooks_cover_new_guarded_departments(self):
        expected = {
            "customer-retention": {"analytics", "customer-success", "product"},
            "people-operations": {"people-ops", "operations", "legal-risk"},
            "procurement-review": {"procurement", "finance", "legal-risk"},
            "metrics-review": {"analytics", "finance", "operations"},
            "strategy-review": {"strategy", "analytics", "legal-risk"},
        }
        for playbook, required_roles in expected.items():
            with self.subTest(playbook=playbook):
                roles = PLAYBOOKS[playbook]["roles"]
                self.assertEqual(roles[0], "chief-of-staff")
                self.assertEqual(roles[-1], "quality")
                self.assertTrue(required_roles.issubset(roles))
                self.assertTrue(set(roles).issubset(ROLES))
        for role in (
            "analytics", "customer-success", "people-ops", "procurement", "strategy",
        ):
            self.assertRegex(ROLES[role].lower(), r"never|do not")

    def test_new_business_playbooks_run_complete_mock_teams(self):
        objectives = {
            "customer-retention": "Review the customer retention workflow",
            "people-operations": "Review the staff training workflow",
            "procurement-review": "Review supplier selection controls",
            "metrics-review": "Review local operating metrics",
            "strategy-review": "Compare strategic scenarios for next quarter",
        }
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            for playbook, objective in objectives.items():
                with self.subTest(playbook=playbook):
                    queue_id = company.enqueue(objective, playbook=playbook)
                    claimed_id, job_id, report, passed = company.run_next_queue_item(
                        queue_id,
                    )
                    self.assertEqual(claimed_id, queue_id)
                    self.assertTrue(passed)
                    self.assertTrue(report.exists())
                    self.assertEqual(
                        [row[1] for row in company.job_detail(job_id)["assignments"]],
                        PLAYBOOKS[playbook]["roles"],
                    )

    def test_product_evidence_reviews_are_append_only_bound_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, report = company.run("Review local inventory")
            first = company.record_product_evidence_review(
                job_id, "business", "accepted", 2, "unknown",
            )
            second = company.record_product_evidence_review(
                job_id, "business", "accepted", 0, "yes", 512,
            )
            self.assertGreater(second["review_id"], first["review_id"])
            self.assertFalse(second["model_called"])
            status = company.product_evidence_status()
            self.assertEqual(status["reviewed_missions"], 1)
            self.assertEqual(status["stored_reviewed_missions"], 1)
            self.assertEqual(status["corrections_total"], 0)
            self.assertEqual(status["complete_measurements"], 1)
            self.assertEqual(status["promotion_candidate_job_ids"], [job_id])
            self.assertFalse(status["milestone_reached"])
            audit_path, _, _ = company.export_audit(Path(tmp) / "audit")
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(len(audit["product_evidence_reviews"]), 2)
            self.assertEqual(audit["product_experiment_reviews"], [])

            report.write_text(
                report.read_text(encoding="utf-8") + "\nchanged after review\n",
                encoding="utf-8",
            )
            stale = company.product_evidence_status()
            self.assertEqual(stale["reviewed_missions"], 0)
            self.assertEqual(stale["stale_review_count"], 1)
            self.assertIn("stale_review_bindings", stale["missing_proof"])

            failed_company = Company(Path(tmp) / "failed", TruncatedModel())
            failed_job, _ = failed_company.run("Review local inventory")
            with self.assertRaisesRegex(ValueError, "quality-failed"):
                failed_company.record_product_evidence_review(
                    failed_job, "business", "accepted", 1, "no", 512,
                )

    def test_external_product_experiment_is_sealed_and_failed_checks_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("Experiment Lab")
            artifact_sha256 = "a" * 64
            with self.assertRaisesRegex(ValueError, "failed product experiment"):
                company.record_product_experiment_review(
                    project_id, "Coding attempt", "coding", "accepted", 0,
                    "unknown", 12.5, 256, 0, False, "local-agent/model",
                    artifact_sha256,
                )
            result = company.record_product_experiment_review(
                project_id, "Coding attempt", "coding", "rejected", 0,
                "unknown", 12.5, 256, 0, False, "local-agent/model",
                artifact_sha256, outcome_reason="tool_failure",
            )
            self.assertRegex(result["experiment_id"], r"^[0-9a-f]{12}$")
            self.assertRegex(result["observation_sha256"], r"^[0-9a-f]{64}$")
            status = company.product_evidence_status("Experiment Lab")
            self.assertEqual(status["reviewed_missions"], 1)
            self.assertEqual(status["complete_measurements"], 1)
            self.assertEqual(status["category_counts"]["coding"], 1)
            self.assertEqual(status["reviews"][0]["integrity"], "sealed_observation")
            self.assertFalse(status["reviews"][0]["checks_passed"])
            self.assertEqual(status["reviews"][0]["outcome_reason"], "tool_failure")
            self.assertEqual(status["outcome_reason_counts"]["tool_failure"], 1)
            legacy = {
                key: value for key, value in result.items()
                if key in {
                    "artifact_sha256", "category", "checks_passed", "corrections",
                    "decision", "exit_code", "experiment_id", "label",
                    "paid_setup_signal", "peak_memory_mb", "project_id", "runner",
                    "runtime_seconds",
                }
            }
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE product_experiment_reviews SET outcome_reason=NULL, "
                    "observation_sha256=? WHERE id=?",
                    (product_experiment_observation_digest(legacy), result["review_id"]),
                )
            legacy_status = company.product_evidence_status("Experiment Lab")
            self.assertEqual(legacy_status["reviewed_missions"], 1)
            self.assertEqual(legacy_status["legacy_outcome_reason_count"], 1)
            self.assertIn("classified_review_outcomes", legacy_status["missing_proof"])
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE product_experiment_reviews SET runtime_seconds=99 WHERE id=?",
                    (result["review_id"],),
                )
            stale = company.product_evidence_status("Experiment Lab")
            self.assertEqual(stale["reviewed_missions"], 0)
            self.assertEqual(stale["stale_review_count"], 1)

    def test_product_evidence_milestone_requires_ten_measured_cross_category_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("Product Lab")
            categories = ("coding", "business", "data-research")
            for index in range(10):
                job_id, _ = company.run(
                    f"Review local inventory experiment {index}",
                    roles=["operations"], project=project_id,
                )
                company.record_product_evidence_review(
                    job_id, categories[index % len(categories)], "accepted", 0,
                    "unknown", 512,
                )
            status = company.product_evidence_status("Product Lab")
            self.assertTrue(status["milestone_reached"])
            self.assertEqual(status["reviewed_missions"], 10)
            self.assertEqual(status["complete_measurements"], 10)
            self.assertEqual(status["missing_categories"], [])
            self.assertEqual(status["missing_proof"], [])

            parsed = parser().parse_args([
                "evidence", "record", "0123456789ab", "--category", "coding",
                "--decision", "accepted", "--corrections", "0",
                "--paid-setup", "unknown", "--peak-memory-mb", "512",
            ])
            self.assertEqual(parsed.evidence_command, "record")
            self.assertEqual(parsed.peak_memory_mb, 512)
            experiment = parser().parse_args([
                "evidence", "experiment", "Coding attempt", "--project", "Product Lab",
                "--category", "coding", "--decision", "rejected", "--corrections", "0",
                "--paid-setup", "unknown", "--runtime-seconds", "12.5",
                "--peak-memory-mb", "256", "--exit-code", "0", "--checks-failed",
                "--runner", "local-agent/model", "--artifact-sha256", "a" * 64,
            ])
            self.assertEqual(experiment.evidence_command, "experiment")
            self.assertFalse(experiment.checks_passed)
            self.assertEqual(
                translate(["evidence", "status"]).command,
                ("evidence", "status"),
            )

    def test_recent_identical_direct_mission_reuses_output_without_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            first_job, first_report = company.run("Review  local inventory")
            calls_after_first = model.calls

            second_job, second_report = company.run(" Review local inventory ")

            self.assertEqual(second_job, first_job)
            self.assertEqual(second_report, first_report)
            self.assertEqual(model.calls, calls_after_first)
            detail = company.job_detail(first_job)
            self.assertEqual(sum(1 for event in detail["events"] if event[0] == "job_reused"), 1)

    def test_quality_failed_report_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            first_job, _ = company.run("Review  local inventory")
            second_job, _ = company.run(" Review local inventory ")
            self.assertNotEqual(second_job, first_job)

    def test_uncacheable_model_and_runtime_identity_change_disable_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uncacheable = UncacheableMockModel()
            company = Company(root / "uncacheable", uncacheable)
            first_job, _ = company.run("Review local inventory")
            second_job, _ = company.run("Review local inventory")
            self.assertNotEqual(second_job, first_job)

            company = Company(root / "versioned", VersionedMockModel("v1"))
            first_job, _ = company.run("Review local inventory")
            company.model = VersionedMockModel("v2")
            second_job, _ = company.run("Review local inventory")
            self.assertNotEqual(second_job, first_job)

    def test_report_integrity_tampering_fails_and_evaluations_are_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, report = company.run("Review local inventory")
            initial = company.job_detail(job_id)["evaluation"]
            self.assertTrue(initial["passed"])
            self.assertTrue(initial["checks"]["report_integrity_valid"])
            with closing(sqlite3.connect(company.db_path)) as db:
                sealed_hash = db.execute(
                    "SELECT report_sha256 FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(), sealed_hash)
            self.assertEqual(list(report.parent.glob(".*.tmp")), [])

            report.write_text(
                report.read_text(encoding="utf-8") + "\nApproved and deployed immediately.\n",
                encoding="utf-8",
            )
            rechecked = company.evaluate_job(job_id)

            self.assertFalse(rechecked["passed"])
            self.assertFalse(rechecked["checks"]["report_integrity_valid"])
            self.assertFalse(rechecked["checks"]["unperformed_action_claims_absent"])
            with closing(sqlite3.connect(company.db_path)) as db:
                history = list(db.execute(
                    "SELECT passed, evaluator_version, report_sha256 FROM evaluation_history "
                    "WHERE job_id=? ORDER BY id", (job_id,),
                ))
            self.assertEqual([row[0] for row in history], [1, 0])
            self.assertEqual(len({row[1] for row in history}), 1)
            self.assertNotEqual(history[0][2], history[1][2])
            replacement_job, _ = company.run("Review local inventory")
            self.assertNotEqual(replacement_job, job_id)

    def test_recovery_finishes_prepared_report_without_model_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=SimulatedProcessCrash("before report replacement"),
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    company.run("Review local inventory")

            job_id = company.jobs()[0][0]
            calls_before_recovery = model.calls
            with closing(sqlite3.connect(company.db_path)) as db:
                job = db.execute(
                    "SELECT status, output_path, report_sha256, run_token FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                intent = db.execute(
                    "SELECT temporary_path, output_path, byte_count, length(report_content), "
                    "run_token FROM report_finalizations WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            self.assertEqual(job[:3], ("running", None, None))
            self.assertIsNotNone(job[3])
            self.assertEqual(intent[2], intent[3])
            self.assertEqual(intent[4], job[3])
            self.assertTrue(Path(intent[0]).is_file())
            self.assertFalse(Path(intent[1]).exists())

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            detail = company.job_detail(job_id)
            self.assertEqual(detail["job"][2], "complete")
            self.assertTrue(detail["evaluation"]["passed"])
            event_kinds = [event[0] for event in detail["events"]]
            self.assertEqual(event_kinds.count("report_finalization_recovered"), 1)
            self.assertEqual(event_kinds.count("report_sealed"), 1)
            self.assertEqual(event_kinds.count("job_complete"), 1)
            with closing(sqlite3.connect(company.db_path)) as db:
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM report_finalizations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                history_count = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(intent_count, 0)
            self.assertEqual(history_count, 1)

            self.assertEqual(company.recover_stale_jobs(0), [])
            with closing(sqlite3.connect(company.db_path)) as db:
                repeated_history = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(repeated_history, 1)

    def test_finalization_paths_survive_a_resolve_that_mutates_the_string_form(self):
        # On a Windows volume with 8.3 short-name generation enabled -- off by
        # default on client Windows, on by default on the Windows Server image
        # GitHub Actions runs, and NOT reproducible on this development machine's
        # own filesystem -- Path.resolve() can silently substitute an 8-character
        # alias for a longer directory component. This is exactly what surfaced
        # on the project's own first real CI run: three unrelated-looking test
        # failures that were all this one root cause. Both forms open the identical
        # file, so the function's SECURITY check (containment, no symlink escape) must still
        # run against the resolved form -- but its RETURN VALUE must be the
        # caller's original, un-mangled strings, or every other reference to the
        # same report in the ledger permanently diverges by exact string.
        #
        # Reproduced here by mocking resolve() to behave exactly like an
        # 8.3-enabled volume would, since this machine's own filesystem cannot
        # exhibit the real behavior to test against directly.
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            job_id = "finaliz1234"
            long_dir = company.output_dir / "LongDirectoryName"
            long_dir.mkdir(parents=True)
            output_path = long_dir / f"{job_id}.md"
            temporary_path = long_dir / f".{job_id}.md.{'a' * 32}.tmp"
            output_path.write_bytes(b"report")
            temporary_path.write_bytes(b"report")

            real_resolve = Path.resolve

            def short_name_resolve(self, *args, **kwargs):
                resolved = real_resolve(self, *args, **kwargs)
                # Simulate 8.3 mangling of exactly the one long component this
                # test introduced -- nothing else in the path is touched.
                mangled = str(resolved).replace("LongDirectoryName", "LONGDI~1")
                return Path(mangled)

            with patch.object(Path, "resolve", short_name_resolve):
                result = company._validated_report_finalization_paths(
                    job_id, str(output_path), str(temporary_path),
                )
            self.assertIsNotNone(result, "the safety check must still pass under the mocked resolve")
            returned_output, returned_temporary = result
            self.assertEqual(
                str(returned_output), str(output_path),
                "the returned path must be the caller's original string, not the mangled resolved form",
            )
            self.assertEqual(str(returned_temporary), str(temporary_path))
            self.assertNotIn("LONGDI~1", str(returned_output))
            self.assertNotIn("LONGDI~1", str(returned_temporary))

    def test_recovery_registers_replaced_report_after_precommit_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            real_replace = company._durable_replace_report

            def replace_then_crash(source, destination):
                real_replace(source, destination)
                raise SimulatedProcessCrash("after report replacement")

            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=replace_then_crash,
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    company.run("Review local inventory")

            job_id = company.jobs()[0][0]
            calls_before_recovery = model.calls
            with closing(sqlite3.connect(company.db_path)) as db:
                job = db.execute(
                    "SELECT status, output_path, report_sha256 FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                intent = db.execute(
                    "SELECT output_path, report_sha256, byte_count FROM report_finalizations "
                    "WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            self.assertEqual(job, ("running", None, None))
            report = Path(intent[0])
            self.assertTrue(report.is_file())
            self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(), intent[1])
            self.assertEqual(report.stat().st_size, intent[2])

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            with closing(sqlite3.connect(company.db_path)) as db:
                sealed = db.execute(
                    "SELECT status, output_path, report_sha256, run_token FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                evaluation_count = db.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(sealed, ("complete", str(report), intent[1], None))
            self.assertEqual(evaluation_count, 1)

    def test_pending_queue_report_recovers_before_queue_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local inventory", roles=["operations"])
            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=PermissionError("simulated Windows sharing violation"),
            ):
                with self.assertRaises(ReportFinalizationPending):
                    company.run_next_queue_item(queue_id)

            calls_before_recovery = model.calls
            running_queue = company.queue_items("running")[0]
            job_id = running_queue[7]
            self.assertEqual(running_queue[0], queue_id)
            self.assertEqual(company.job_detail(job_id)["job"][2], "running")
            with closing(sqlite3.connect(company.db_path)) as db:
                queue_token, job_token = db.execute(
                    "SELECT q.run_token, j.run_token FROM mission_queue q "
                    "JOIN jobs j ON j.id=q.job_id WHERE q.id=?",
                    (queue_id,),
                ).fetchone()
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM report_finalizations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                intent_paths_and_content = db.execute(
                    "SELECT output_path, temporary_path, report_content "
                    "FROM report_finalizations WHERE job_id=?",
                    (job_id,),
                ).fetchone()
            self.assertEqual(queue_token, job_token)
            self.assertIsNotNone(job_token)
            self.assertEqual(intent_count, 1)
            pending_health = company.health_snapshot()
            self.assertEqual(pending_health["pending_report_finalizations"], 1)
            self.assertEqual(pending_health["pending_evaluations"], 0)
            self.assertEqual(len(pending_health["pending_completion"]), 1)
            pending_item = pending_health["pending_completion"][0]
            self.assertEqual(pending_item["job_id"], job_id)
            self.assertEqual(pending_item["state"], "report_finalization_pending")
            self.assertEqual(pending_item["queue_id"], queue_id)
            self.assertTrue(pending_item["since"])
            health_json = json.dumps(pending_health)
            self.assertNotIn(job_token, health_json)
            self.assertNotIn(intent_paths_and_content[0], health_json)
            self.assertNotIn(intent_paths_and_content[1], health_json)
            self.assertNotIn("Local Agent Company Report", health_json)
            pending_page = render_dashboard(company, service_token="local-review")
            self.assertIn("Mission completion pending", pending_page)
            self.assertIn("report finalization pending", pending_page)
            audit_path, _, _ = company.export_audit(Path(tmp) / "exports")
            audit_text = audit_path.read_text(encoding="utf-8")
            self.assertNotIn(job_token, audit_text)
            self.assertNotIn("report_finalizations", json.loads(audit_text))

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            evaluation = company.job_detail(job_id)["evaluation"]
            expected_status = "complete" if evaluation["passed"] else "quality_failed"
            reconciled = company.queue_items(expected_status)[0]
            self.assertEqual(reconciled[0], queue_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                tokens = db.execute(
                    "SELECT q.run_token, j.run_token FROM mission_queue q "
                    "JOIN jobs j ON j.id=q.job_id WHERE q.id=?",
                    (queue_id,),
                ).fetchone()
                queue_events = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_recovery_claimed' "
                    "AND detail LIKE ?",
                    (f'%"queue_id": "{queue_id}"%',),
                ).fetchone()[0]
            self.assertEqual(tokens, (None, None))
            self.assertEqual(queue_events, 1)
            recovered_health = company.health_snapshot()
            self.assertEqual(recovered_health["pending_report_finalizations"], 0)
            self.assertEqual(recovered_health["pending_evaluations"], 0)
            self.assertEqual(recovered_health["pending_completion"], [])

    def test_transient_report_read_failure_preserves_recovery_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=SimulatedProcessCrash("leave prepared report"),
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    company.run("Review local inventory")

            job_id = company.jobs()[0][0]
            calls_before_recovery = model.calls
            with patch.object(
                company, "_read_local_report_bytes",
                side_effect=PermissionError("simulated Windows sharing violation"),
            ):
                self.assertEqual(company.recover_stale_jobs(0), [])
            self.assertEqual(model.calls, calls_before_recovery)
            with closing(sqlite3.connect(company.db_path)) as db:
                job = db.execute(
                    "SELECT status, output_path, report_sha256, run_token FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM report_finalizations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                deferred = db.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? "
                    "AND kind='report_finalization_recovery_deferred'",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(job[0:3], ("running", None, None))
            self.assertIsNotNone(job[3])
            self.assertEqual(intent_count, 1)
            self.assertEqual(deferred, 1)

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            self.assertEqual(company.job_detail(job_id)["job"][2], "complete")

    @unittest.skipUnless(os.name == "nt", "Windows write-through flags")
    def test_windows_report_replace_requests_write_through(self):
        move_file = Mock(return_value=1)
        kernel = Mock()
        kernel.MoveFileExW = move_file
        with patch("ctypes.WinDLL", return_value=kernel):
            Company._durable_replace_report(Path("prepared.tmp"), Path("report.md"))
        move_file.assert_called_once_with("prepared.tmp", "report.md", 0x1 | 0x8)

    def test_recovery_evaluates_sealed_queue_job_after_evaluator_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local inventory", roles=["operations"])
            with patch.object(
                company, "evaluate_job",
                side_effect=RuntimeError("after report seal"),
            ):
                with self.assertRaises(ReportFinalizationPending):
                    company.run_next_queue_item(queue_id)

            calls_before_recovery = model.calls
            running_queue = company.queue_items("running")[0]
            job_id = running_queue[7]
            with closing(sqlite3.connect(company.db_path)) as db:
                job_status = db.execute(
                    "SELECT status FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
                evaluation_count = db.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(job_status, "complete")
            self.assertEqual(evaluation_count, 0)
            pending_health = company.health_snapshot()
            self.assertEqual(pending_health["pending_report_finalizations"], 0)
            self.assertEqual(pending_health["pending_evaluations"], 1)
            self.assertEqual(pending_health["pending_completion"][0]["job_id"], job_id)
            self.assertEqual(
                pending_health["pending_completion"][0]["state"], "evaluation_pending"
            )
            self.assertEqual(pending_health["pending_completion"][0]["queue_id"], queue_id)
            pending_page = render_dashboard(company, service_token="local-review")
            self.assertIn("Mission completion pending", pending_page)
            self.assertIn("evaluation pending", pending_page)

            self.assertEqual(company.recover_stale_jobs(0), [])
            self.assertEqual(model.calls, calls_before_recovery)
            evaluation = company.job_detail(job_id)["evaluation"]
            expected_status = "complete" if evaluation["passed"] else "quality_failed"
            self.assertEqual(company.queue_items(expected_status)[0][0], queue_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_count = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(history_count, 1)
            self.assertEqual(company.recover_stale_jobs(0), [])
            with closing(sqlite3.connect(company.db_path)) as db:
                repeated_history = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(repeated_history, 1)
            self.assertEqual(company.health_snapshot()["pending_completion"], [])

    def test_stale_queue_recovery_rechecks_instead_of_trusting_evaluation_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review local inventory", roles=["operations"])
            _, job_id, report, _ = company.run_next_queue_item(queue_id)
            report.write_text(
                report.read_text(encoding="utf-8") + "\nApproved and deployed immediately.\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE evaluations SET passed=1, score=100 WHERE job_id=?", (job_id,)
                )
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, completed_at=NULL, "
                    "run_token=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", "stale-queue-token", queue_id),
                )
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]

            self.assertEqual(company.recover_stale_jobs(0), [])
            self.assertEqual(company.queue_items("quality_failed")[0][0], queue_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                latest_passed = db.execute(
                    "SELECT passed FROM evaluation_history WHERE job_id=? ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(history_after, history_before + 1)
            self.assertEqual(latest_passed, 0)

    def test_recovered_queue_lease_discards_late_evaluation_without_audit_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local inventory", roles=["operations"])
            original_run = company.run

            def recover_after_seal(*args, **kwargs):
                result = original_run(*args, **kwargs)
                company.recover_stale_jobs(0)
                return result

            with patch.object(company, "run", side_effect=recover_after_seal):
                with self.assertRaises(ExecutionLeaseLost):
                    company.run_next_queue_item(queue_id)

            completed = company.queue_items("complete")[0]
            job_id = completed[7]
            with closing(sqlite3.connect(company.db_path)) as db:
                history_count = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                quality_events = db.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='quality_evaluated'",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(history_count, 1)
            self.assertEqual(quality_events, 1)

    def test_recovery_does_not_finalize_from_a_stale_evaluation_return_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review local inventory", roles=["operations"])
            _, job_id, report, _ = company.run_next_queue_item(queue_id)
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, completed_at=NULL, "
                    "run_token=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", "old-recovery-token", queue_id),
                )
            real_evaluate = company.evaluate_job

            def return_older_result(observed_job_id, *, _queue_claim=None):
                older = real_evaluate(observed_job_id)
                report.write_text(
                    report.read_text(encoding="utf-8")
                    + "\nApproved and deployed immediately.\n",
                    encoding="utf-8",
                )
                newer = real_evaluate(observed_job_id)
                self.assertFalse(newer["passed"])
                return older

            with patch.object(company, "evaluate_job", side_effect=return_older_result):
                self.assertEqual(company.recover_stale_jobs(0), [])

            self.assertEqual(company.queue_items("running")[0][0], queue_id)
            self.assertEqual(company.recent_evaluations()[0][1], 0)

    def test_reused_queue_evaluation_failure_remains_durably_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            job_id, _ = company.run("Review local inventory", roles=["operations"])
            calls_after_direct = model.calls
            queue_id = company.enqueue("Review local inventory", roles=["operations"])

            with patch.object(
                company, "evaluate_job", side_effect=RuntimeError("simulated evaluator pause"),
            ):
                with self.assertRaises(ReportFinalizationPending):
                    company.run_next_queue_item(queue_id)

            self.assertEqual(model.calls, calls_after_direct)
            self.assertEqual(company.queue_items("running")[0][7], job_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM evaluations WHERE job_id=?", (job_id,)
                    ).fetchone()[0],
                    1,
                )
                queue_started_at = db.execute(
                    "SELECT started_at FROM mission_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
            pending = company.health_snapshot()["pending_completion"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["job_id"], job_id)
            self.assertEqual(pending[0]["queue_id"], queue_id)
            self.assertEqual(pending[0]["state"], "evaluation_pending")
            self.assertEqual(pending[0]["since"], queue_started_at)
            self.assertIn(
                "Mission completion pending",
                render_dashboard(company, service_token="local-review"),
            )

            self.assertEqual(company.recover_stale_jobs(0), [])
            self.assertEqual(model.calls, calls_after_direct)
            self.assertEqual(company.health_snapshot()["pending_completion"], [])
            self.assertEqual(company.queue_items("complete")[0][0], queue_id)

    def test_tampered_prepared_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=SimulatedProcessCrash("leave prepared temp"),
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    company.run("Review local inventory")

            job_id = company.jobs()[0][0]
            with closing(sqlite3.connect(company.db_path)) as db:
                temporary_path = Path(db.execute(
                    "SELECT temporary_path FROM report_finalizations WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0])
            temporary_path.write_bytes(b"tampered pending report")
            calls_before_recovery = model.calls

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            with closing(sqlite3.connect(company.db_path)) as db:
                job = db.execute(
                    "SELECT status, output_path, report_sha256, run_token FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM report_finalizations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                evaluation_count = db.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                abandoned = db.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? "
                    "AND kind='report_finalization_abandoned'",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(job, ("interrupted", None, None, None))
            self.assertEqual(intent_count, 0)
            self.assertEqual(evaluation_count, 0)
            self.assertEqual(abandoned, 1)
            self.assertEqual(temporary_path.read_bytes(), b"tampered pending report")

    def test_superseded_report_intent_cannot_seal_under_a_new_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            with patch(
                "local_company.core.Company._durable_replace_report",
                side_effect=SimulatedProcessCrash("leave old report intent"),
            ):
                with self.assertRaises(SimulatedProcessCrash):
                    company.run("Review local inventory")

            job_id = company.jobs()[0][0]
            with closing(sqlite3.connect(company.db_path)) as db, db:
                old_token, temporary_path = db.execute(
                    "SELECT run_token, temporary_path FROM report_finalizations WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                db.execute(
                    "UPDATE jobs SET run_token='new-lease', heartbeat_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job_id),
                )
            calls_before_recovery = model.calls

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            self.assertEqual(model.calls, calls_before_recovery)
            with closing(sqlite3.connect(company.db_path)) as db:
                job = db.execute(
                    "SELECT status, output_path, report_sha256, run_token FROM jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM report_finalizations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                evaluation_count = db.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.assertEqual(job, ("interrupted", None, None, None))
            self.assertEqual(intent_count, 0)
            self.assertEqual(evaluation_count, 0)
            self.assertTrue(Path(temporary_path).is_file())

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                late_seal = company._seal_report_finalization(
                    db, job_id, old_token, datetime.now(timezone.utc).isoformat(),
                    recovered=True,
                )
            self.assertFalse(late_seal)
            self.assertEqual(company.job_detail(job_id)["job"][2], "interrupted")

    def test_report_outside_output_storage_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = CountingMockModel()
            company = Company(root / "state", model)
            job_id, report = company.run("Review local inventory")
            outside = root / "moved-report.md"
            outside.write_bytes(report.read_bytes())
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute("UPDATE jobs SET output_path=? WHERE id=?", (str(outside), job_id))

            replacement_job, _ = company.run("Review local inventory")

            self.assertNotEqual(replacement_job, job_id)
            events = company.job_detail(job_id)["events"]
            self.assertTrue(any(event[0] == "job_reuse_rejected" for event in events))

    def test_changed_retrieved_source_invalidates_recent_job_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inventory.md"
            source.write_text("inventory baseline is 10 units", encoding="utf-8")
            model = RecordingModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Inventory")
            company.add_knowledge(source, project=project_id)
            first_job, _ = company.run("Review inventory baseline", project=project_id)

            source.write_text("inventory baseline is 14 units", encoding="utf-8")
            company.add_knowledge(source, project=project_id)
            second_job, _ = company.run("Review inventory baseline", project=project_id)

            self.assertNotEqual(second_job, first_job)

    def test_live_source_drift_blocks_before_reuse_or_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inventory.md"
            source.write_text("inventory baseline is 10 units", encoding="utf-8")
            model = CountingMockModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Inventory")
            company.add_knowledge(source, project=project_id)
            first_job, _ = company.run("Review inventory baseline", project=project_id)
            calls_after_first = model.calls

            source.write_text("inventory baseline is 14 units", encoding="utf-8")
            before = hashlib.sha256(company.db_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "before model work"):
                company.run("Review inventory baseline", project=project_id)

            self.assertEqual(model.calls, calls_after_first)
            self.assertEqual([row[0] for row in company.jobs()], [first_job])
            self.assertEqual(
                hashlib.sha256(company.db_path.read_bytes()).hexdigest(), before,
            )
            self.assertNotIn(
                "job_reuse_rejected",
                {event[0] for event in company.job_detail(first_job)["events"]},
            )

    def test_source_mutation_during_reuse_report_check_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "inventory.md"
            source.write_text("inventory baseline is 10 units", encoding="utf-8")
            model = CountingMockModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Inventory")
            company.add_knowledge(source, project=project_id)
            first_job, _ = company.run("Review inventory baseline", project=project_id)
            calls_after_first = model.calls
            before = hashlib.sha256(company.db_path.read_bytes()).hexdigest()
            original_reader = company._read_local_report_bytes
            mutated = False

            def read_then_mutate(path):
                nonlocal mutated
                report_bytes = original_reader(path)
                if not mutated:
                    source.write_text("inventory baseline is 14 units", encoding="utf-8")
                    mutated = True
                return report_bytes

            with patch.object(
                company, "_read_local_report_bytes", side_effect=read_then_mutate,
            ), self.assertRaisesRegex(RuntimeError, "before model work"):
                company.run(
                    "Review inventory baseline", project=project_id,
                )

            self.assertTrue(mutated)
            self.assertEqual(model.calls, calls_after_first)
            self.assertEqual([row[0] for row in company.jobs()], [first_job])
            self.assertEqual(
                hashlib.sha256(company.db_path.read_bytes()).hexdigest(), before,
            )
            self.assertNotIn(
                "job_reuse_rejected",
                {event[0] for event in company.job_detail(first_job)["events"]},
            )

    def test_evidence_manifest_freezes_exact_excerpt_and_detects_stale_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text(
                "Inventory baseline is 10 local units. Future demand is unknown.", encoding="utf-8"
            )
            company = Company(root / "state", EvidenceCitingModel())
            project_id = company.create_project("Evidence")
            company.add_knowledge(source, project=project_id)

            job_id, report = company.run(
                "Using imported notes about inventory, separate verified facts from assumptions.",
                project=project_id,
            )
            detail = company.job_detail(job_id)
            evaluation = detail["evaluation"]
            manifest = detail["evidence_manifest"]

            self.assertTrue(evaluation["passed"])
            self.assertTrue(evaluation["checks"]["evidence_manifest_valid"])
            self.assertTrue(evaluation["checks"]["verified_facts_evidence_cited"])
            self.assertTrue(evaluation["checks"]["verification_claims_evidence_bound"])
            evidence = manifest["evidence"][0]
            self.assertEqual(
                evidence["quote"], source.read_text(encoding="utf-8")[
                    evidence["char_start"]:evidence["char_end"]
                ],
            )
            self.assertIn(f"[EVIDENCE:{evidence['evidence_id']}]", report.read_text(encoding="utf-8"))
            basis = dict(manifest)
            recorded_digest = basis.pop("manifest_sha256")
            self.assertEqual(
                hashlib.sha256(company._canonical_json(basis).encode("utf-8")).hexdigest(),
                recorded_digest,
            )

            source.write_text("Inventory baseline changed after capture.", encoding="utf-8")
            rechecked = company.evaluate_job(job_id)
            self.assertFalse(rechecked["passed"])
            self.assertFalse(rechecked["checks"]["evidence_manifest_valid"])
            self.assertEqual(rechecked["manifest_reason"], "source_stale")

    def test_filename_only_verification_and_forged_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text("Inventory baseline is local and current.", encoding="utf-8")
            company = Company(root / "state", FilenameOnlyCitationModel())
            project_id = company.create_project("Evidence")
            company.add_knowledge(source, project=project_id)
            job_id, _ = company.run(
                "Using imported notes about inventory, separate verified facts from assumptions.",
                project=project_id,
            )
            evaluation = company.job_detail(job_id)["evaluation"]
            self.assertTrue(evaluation["checks"]["verified_facts_cited"])
            self.assertFalse(evaluation["checks"]["verified_facts_evidence_cited"])
            self.assertFalse(evaluation["checks"]["verification_claims_evidence_bound"])
            self.assertFalse(evaluation["checks"]["evidence_ids_valid"])

            with closing(sqlite3.connect(company.db_path)) as db, db:
                raw = db.execute(
                    "SELECT manifest_json FROM evidence_manifests WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                forged = json.loads(raw)
                forged["generator"] = "forged"
                db.execute(
                    "UPDATE evidence_manifests SET manifest_json=? WHERE job_id=?",
                    (json.dumps(forged, sort_keys=True), job_id),
                )
            rechecked = company.evaluate_job(job_id)
            self.assertFalse(rechecked["checks"]["evidence_manifest_valid"])
            self.assertEqual(rechecked["manifest_reason"], "digest_mismatch")

    def test_quality_rejects_completion_claim_that_conflicts_with_retrieved_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent_team_system.json"
            source.write_text(
                '{"gaps":["Telemetry is not fully wired. PostHog and Sentry should become '
                'standard before scaling client templates."]}',
                encoding="utf-8",
            )
            company = Company(root / "state", ContradictingSourceModel())
            project_id = company.create_project("Grounding")
            company.add_knowledge(source, project=project_id)

            job_id, _ = company.run(
                "Assess telemetry readiness for PostHog and Sentry", project=project_id
            )
            evaluation = company.evaluate_job(job_id)

            self.assertFalse(evaluation["passed"])
            self.assertFalse(evaluation["checks"]["source_limitations_respected"])
            self.assertIn("posthog", evaluation["source_conflicts"][0]["shared_terms"])
            quality_events = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "quality_evaluated"
            ]
            self.assertTrue(quality_events[-1]["source_conflicts"])

    def test_sensitive_action_fails_closed_and_creates_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaises(PermissionError):
                company.run("Send email to every prospect")
            requests = company.action_requests("pending")
            self.assertEqual(len(requests), 1)
            company.decide_action(requests[0][0], "rejected", "Not authorized")
            self.assertEqual(company.action_requests("rejected")[0][0], requests[0][0])

    def test_sensitive_action_normalization_blocks_common_bypass_wording(self):
        categories = Company.sensitive_categories(
            "Email every prospect and wire funds, then wipe the database"
        )
        self.assertEqual(categories, ["destructive", "external_communication", "money"])
        self.assertEqual(
            Company.sensitive_categories("Draft an email template for owner review"), []
        )
        self.assertEqual(
            Company.sensitive_categories("Prepare a deployment plan without executing it"), []
        )

    def test_knowledge_is_retrieved_and_cited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "shop.md"
            source.write_text("The bakery's signature product is tamarind sourdough. Marketing budget is 500.", encoding="utf-8")
            company = Company(root / "state", MockModel())
            _, changed = company.add_knowledge(source)
            self.assertTrue(changed)
            hits = company.search_knowledge("bakery marketing budget")
            self.assertEqual(hits[0].path, str(source.resolve()))
            _, report = company.run("Create a bakery marketing budget")
            self.assertIn(str(source.resolve()), report.read_text(encoding="utf-8"))

    def test_knowledge_retrieval_selects_distant_exact_headings_instead_of_opening_boilerplate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "product.md"
            source.write_text(
                "# Product\n\nGeneral project evidence and product overview.\n\n"
                + ("general local product context\n" * 80)
                + "## Stage 1: one-machine product\n\nUse the launchpad. Record correction effort. Track memory.\n\n"
                + ("general platform context\n" * 80)
                + "## Next measurable milestone\n\nRun ten real missions across three categories.\n",
                encoding="utf-8",
            )
            company = Company(root / "state", MockModel())
            project = company.create_project("Retrieval Lab")
            company.add_knowledge(source, project)

            hits = company.search_knowledge(
                "Identify the Stage 1 actions and next measurable milestone",
                limit=4,
                project=project,
            )

            self.assertEqual(len(hits), 2)
            excerpts = "\n".join(hit.excerpt for hit in hits)
            self.assertIn("## Stage 1: one-machine product", excerpts)
            self.assertIn("## Next measurable milestone", excerpts)
            self.assertNotIn("# Product\n\nGeneral project evidence", excerpts)
            self.assertNotEqual(hits[0].evidence_id, hits[1].evidence_id)

    def test_named_knowledge_sources_are_reserved_and_run_context_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", MockModel())
            project_id = company.create_project("Named Evidence")
            named = ["alpha.md", "beta.json", "gamma.md", "delta.json"]
            for index, filename in enumerate(named):
                source = root / filename
                source.write_text(f"opaque datum {index}", encoding="utf-8")
                company.add_knowledge(source, project_id)
            for index in range(6):
                source = root / f"generic-{index}.md"
                source.write_text(("build release brief " * (20 - index)).strip(), encoding="utf-8")
                company.add_knowledge(source, project_id)

            objective = (
                "Using alpha.md, beta.json, gamma.md, and delta.json, build release brief"
            )
            hits = company.search_knowledge(objective, limit=8, project=project_id)

            self.assertEqual([Path(hit.path).name for hit in hits[:4]], named)
            self.assertEqual(len(hits), 8)
            job_id, _ = company.run(objective, roles=["operations"], project=project_id)
            manifest = company._load_evidence_manifest(job_id)
            manifest_names = {Path(item["path"]).name for item in manifest["sources"]}
            self.assertTrue(set(named).issubset(manifest_names))
            self.assertLessEqual(len(manifest["evidence"]), 8)

    def test_named_sources_use_filename_boundaries_and_fail_closed_over_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", MockModel())
            project_id = company.create_project("Bounded Names")
            named = ["other-report.md"] + [f"n{index}.md" for index in range(1, 8)]
            for filename in named + ["report.md", "n8.md"]:
                source = root / filename
                content = "generic release context " * 40 if filename == "report.md" else "opaque"
                source.write_text(content, encoding="utf-8")
                company.add_knowledge(source, project_id)
            objective = "Use " + ", ".join(named) + " for the bounded release context"

            hits = company.search_knowledge(objective, limit=8, project=project_id)

            self.assertEqual([Path(hit.path).name for hit in hits], named)
            self.assertNotIn("report.md", {Path(hit.path).name for hit in hits})
            with self.assertRaisesRegex(ValueError, "exceeding the bounded context limit"):
                company.search_knowledge(
                    objective + ", n8.md", limit=8, project=project_id,
                )

    def test_project_knowledge_authority_precedes_frequency_but_not_explicit_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", MockModel())
            project = company.create_project("Authority Lab")
            stale = root / "stale.md"
            current = root / "current.md"
            stale.write_text("current live release commit " * 5, encoding="utf-8")
            current.write_text("current live release commit is new", encoding="utf-8")
            stale_id, _ = company.add_knowledge(stale, project)
            current_id, _ = company.add_knowledge(current, project)

            self.assertEqual(
                Path(company.search_knowledge(
                    "current live release commit", project=project,
                )[0].path).name,
                "stale.md",
            )
            result = company.set_knowledge_authority(current_id, project, 20)
            self.assertEqual(result["authority"], 20)
            self.assertFalse(result["effects"]["model_called"])
            current_hit = company.search_knowledge(
                "current live release commit", project=project,
            )[0]
            self.assertEqual(Path(current_hit.path).name, "current.md")
            self.assertEqual(current_hit.authority, 20)
            unchanged = company.set_knowledge_authority(current_id, project, 20)
            self.assertFalse(unchanged["effects"]["knowledge_authority_mutated"])
            self.assertEqual(
                Path(company.search_knowledge(
                    "Use stale.md for the current live release commit", project=project,
                )[0].path).name,
                "stale.md",
            )
            strongest = root / "strongest.md"
            strongest.write_text("current live release commit " * 30, encoding="utf-8")
            company.add_knowledge(strongest, project)
            self.assertEqual(
                Path(company.search_knowledge(
                    "current live release commit", project=project,
                )[0].path).name,
                "strongest.md",
            )
            company.set_knowledge_authority(current_id, project, 0)
            reset_hits = company.search_knowledge(
                "current live release commit", project=project,
            )
            self.assertEqual(Path(reset_hits[0].path).name, "strongest.md")
            self.assertEqual(next(
                hit.authority for hit in reset_hits
                if Path(hit.path).name == "current.md"
            ), 0)
            with self.assertRaisesRegex(ValueError, "between -100 and 100"):
                company.set_knowledge_authority(current_id, project, 101)
            with self.assertRaisesRegex(ValueError, "not attached"):
                company.set_knowledge_authority("missing", project, 1)

    def test_unknown_role_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaises(ValueError):
                company.run("Plan inventory", ["wizard"])

    def test_interrupted_job_is_recovered_on_initialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.initialize()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('deadjob', 'x', 'running', '2000-01-01T00:00:00+00:00', '2000-01-01T00:00:00+00:00')"
                )
            self.assertEqual(company.recover_stale_jobs(0), ["deadjob"])
            self.assertEqual(company.job_detail("deadjob")["job"][2], "interrupted")

    def test_initialize_migrates_report_finalization_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("Existing State")
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute("DROP TABLE report_finalizations")

            company.initialize()

            with closing(sqlite3.connect(company.db_path)) as db:
                columns = [
                    row[1] for row in db.execute("PRAGMA table_info(report_finalizations)")
                ]
                retained_project = db.execute(
                    "SELECT id FROM projects WHERE id=?", (project_id,)
                ).fetchone()
            self.assertEqual(
                columns,
                [
                    "job_id", "run_token", "output_path", "temporary_path",
                    "report_sha256", "byte_count", "report_content", "prepared_at",
                ],
            )
            self.assertEqual(retained_project, (project_id,))

    def test_stale_orphaned_queue_claim_fails_closed_once_without_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local queue health")
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            recovered = company.queue_items("failed")[0]
            self.assertEqual(recovered[0], queue_id)
            self.assertEqual(recovered[7], "")
            self.assertIn("no linked job", recovered[8])
            self.assertEqual(model.calls, 0)
            with closing(sqlite3.connect(company.db_path)) as db:
                event_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_claim_recovered'"
                ).fetchone()[0]
            self.assertEqual(event_count, 1)

            self.assertEqual(company.recover_stale_jobs(60), [])
            with closing(sqlite3.connect(company.db_path)) as db:
                repeated_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_claim_recovered'"
                ).fetchone()[0]
            self.assertEqual(repeated_count, 1)
            self.assertEqual(company.jobs(), [])

    def test_stale_linked_queue_and_job_are_recovered_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review interrupted work")
            queue_started_at = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('linkedjob', 'x', 'running', ?, ?)",
                    ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
                )
                db.execute(
                    "INSERT INTO assignments(job_id, role, brief, status) "
                    "VALUES ('linkedjob', 'operations', 'x', 'running')"
                )
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, job_id='linkedjob' "
                    "WHERE id=?",
                    (queue_started_at, queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), ["linkedjob"])
            self.assertEqual(company.job_detail("linkedjob")["job"][2], "interrupted")
            recovered = company.queue_items("failed")[0]
            self.assertEqual(recovered[7], "linkedjob")
            self.assertIn("interrupted job linkedjob", recovered[8])
            with closing(sqlite3.connect(company.db_path)) as db:
                assignment_status = db.execute(
                    "SELECT status FROM assignments WHERE job_id='linkedjob'"
                ).fetchone()[0]
            self.assertEqual(assignment_status, "failed")

    def test_stale_unlinked_queue_is_not_guessed_while_a_job_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review ambiguous work")
            company.initialize()
            observed_at = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('livejob', 'other work', 'running', ?, ?)",
                    (observed_at, observed_at),
                )
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            self.assertEqual(company.queue_items("running")[0][0], queue_id)

    def test_completed_job_queue_recovery_waits_for_fresh_finalization_then_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run("Review finalization recovery", roles=["operations"])
            queue_id = company.enqueue("Review finalization recovery", roles=["operations"])
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, job_id=?, run_token=? "
                    "WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job_id, "queue-lease", queue_id),
                )

            self.assertEqual(company.recover_stale_jobs(60), [])
            self.assertEqual(company.queue_items("running")[0][0], queue_id)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", job_id),
                )
            self.assertEqual(company.recover_stale_jobs(60), [])
            reconciled = company.queue_items("complete")[0]
            self.assertEqual(reconciled[0], queue_id)
            self.assertEqual(reconciled[7], job_id)
            self.assertEqual(reconciled[8], "")
            with closing(sqlite3.connect(company.db_path)) as db:
                recovery_events = list(db.execute(
                    "SELECT kind, detail FROM events WHERE kind IN "
                    "('queue_recovery_claimed', 'queue_execution_finished') "
                    "ORDER BY id"
                ))
            self.assertEqual(
                [event[0] for event in recovery_events[-2:]],
                ["queue_recovery_claimed", "queue_execution_finished"],
            )
            self.assertTrue(json.loads(recovery_events[-1][1])["quality_passed"])

    def test_queue_job_is_linked_before_the_first_model_call_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local queue health", roles=["operations"])
            outcome = {}

            def run_queue():
                try:
                    outcome["result"] = company.run_next_queue_item()
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_queue)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))
            with closing(sqlite3.connect(company.db_path)) as db:
                queue_status, job_id, queue_token = db.execute(
                    "SELECT status, job_id, run_token FROM mission_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
                job_status, job_token = db.execute(
                    "SELECT status, run_token FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
            self.assertEqual(queue_status, "running")
            self.assertRegex(job_id, r"^[0-9a-f]{12}$")
            self.assertEqual(job_status, "running")
            self.assertRegex(queue_token, r"^[0-9a-f]{32}$")
            self.assertEqual(job_token, queue_token)

            model.release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertEqual(outcome["result"][0], queue_id)
            self.assertEqual(outcome["result"][1], job_id)

    def test_blocking_model_call_renews_execution_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "local_company.core.EXECUTION_HEARTBEAT_SECONDS", 0.01,
        ):
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local queue health", roles=["operations"])
            outcome = {}

            def run_queue():
                try:
                    outcome["result"] = company.run_next_queue_item()
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_queue)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))
            with closing(sqlite3.connect(company.db_path)) as db, db:
                job_id = db.execute(
                    "SELECT job_id FROM mission_queue WHERE id=?", (queue_id,),
                ).fetchone()[0]
                db.execute(
                    "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (job_id,),
                )

            time.sleep(0.08)
            self.assertEqual(company.recover_stale_jobs(1), [])
            model.release.set()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertEqual(outcome["result"][1], job_id)

    def test_recovery_revokes_worker_lease_and_discards_its_late_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review revocable work", roles=["operations"])
            outcome = {}

            def run_queue():
                try:
                    outcome["result"] = company.run_next_queue_item()
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_queue)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))
            with closing(sqlite3.connect(company.db_path)) as db:
                job_id = db.execute(
                    "SELECT job_id FROM mission_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]

            self.assertEqual(company.recover_stale_jobs(0), [job_id])
            model.release.set()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertNotIn("result", outcome)
            self.assertRegex(str(outcome["error"]), "recovered or superseded")
            self.assertEqual(company.job_detail(job_id)["job"][2], "interrupted")
            recovered_queue = company.queue_items("failed")[0]
            self.assertEqual(recovered_queue[0], queue_id)
            self.assertEqual(recovered_queue[7], job_id)
            self.assertFalse((company.output_dir / f"{job_id}.md").exists())
            with closing(sqlite3.connect(company.db_path)) as db:
                job_token = db.execute(
                    "SELECT run_token FROM jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
                queue_token = db.execute(
                    "SELECT run_token FROM mission_queue WHERE id=?", (queue_id,)
                ).fetchone()[0]
                discarded = db.execute(
                    "SELECT COUNT(*) FROM events "
                    "WHERE job_id=? AND kind='late_result_discarded'",
                    (job_id,),
                ).fetchone()[0]
            self.assertIsNone(job_token)
            self.assertIsNone(queue_token)
            self.assertEqual(discarded, 1)

    def test_queue_reuse_is_linked_without_repeating_model_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            job_id, report = company.run(
                "Review local inventory", roles=["operations", "quality"],
            )
            calls_after_direct_run = model.calls
            queue_id = company.enqueue(
                "Review local inventory", roles=["operations", "quality"],
            )

            observed_queue, observed_job, observed_report, passed = (
                company.run_next_queue_item()
            )

            self.assertEqual(observed_queue, queue_id)
            self.assertEqual(observed_job, job_id)
            self.assertEqual(observed_report, report)
            self.assertTrue(passed)
            self.assertEqual(model.calls, calls_after_direct_run)
            self.assertEqual(company.queue_items("complete")[0][7], job_id)
            linked_events = [
                json.loads(detail)
                for kind, detail, _ in company.job_detail(job_id)["events"]
                if kind == "queue_job_linked"
            ]
            self.assertEqual(linked_events[-1], {"queue_id": queue_id, "reused": True})

    def test_reviewed_queue_id_rejects_priority_change_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            reviewed_id = company.enqueue("Reviewed work", priority=20)
            self.assertEqual(company.next_due_queue_item()[0], reviewed_id)
            urgent_id = company.enqueue("New urgent work", priority=90)

            with self.assertRaisesRegex(RuntimeError, "Queue changed"):
                company.run_next_queue_item(reviewed_id)

            self.assertEqual(
                {row[0] for row in company.queue_items("queued")},
                {reviewed_id, urgent_id},
            )
            self.assertEqual(company.jobs(), [])
            self.assertEqual(model.calls, 0)
            with closing(sqlite3.connect(company.db_path)) as db:
                started = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_execution_started'"
                ).fetchone()[0]
            self.assertEqual(started, 0)

    def test_running_queue_claim_does_not_consume_a_second_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            first_id = company.enqueue("First reviewed work", roles=["operations"], priority=90)
            second_id = company.enqueue("Second reviewed work", roles=["operations"], priority=10)
            outcome = {}

            def run_first():
                try:
                    outcome["result"] = company.run_next_queue_item(first_id)
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))

            with self.assertRaisesRegex(RuntimeError, "already running"):
                company.run_next_queue_item(second_id)
            self.assertEqual(company.queue_items("queued")[0][0], second_id)

            model.release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertEqual(outcome["result"][0], first_id)

    def test_running_direct_job_does_not_consume_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Queued work remains untouched", priority=80)
            outcome = {}

            def run_direct():
                try:
                    outcome["result"] = company.run("Active direct work", roles=["operations"])
                except Exception as exc:  # pragma: no cover - asserted below
                    outcome["error"] = exc

            thread = threading.Thread(target=run_direct)
            thread.start()
            self.assertTrue(model.started.wait(timeout=3))

            with self.assertRaisesRegex(RuntimeError, "Mission .* is already running"):
                company.run_next_queue_item(queue_id)
            self.assertEqual(company.queue_items("queued")[0][0], queue_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                started = db.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='queue_execution_started'"
                ).fetchone()[0]
            self.assertEqual(started, 0)

            model.release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertIn("result", outcome)

    def test_retry_accepts_explicit_bounded_roles_and_preserves_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            original_job_id, _ = company.run(
                "Review one bounded local operating change",
                roles=["chief-of-staff", "research", "finance", "legal-risk", "quality"],
            )

            retry_job_id, _ = company.retry(
                original_job_id,
                roles=["chief-of-staff", "operations", "finance", "quality"],
            )
            detail = company.job_detail(retry_job_id)

            self.assertEqual(detail["job"][5], original_job_id)
            self.assertEqual(
                [assignment[1] for assignment in detail["assignments"]],
                ["chief-of-staff", "operations", "finance", "quality"],
            )

    def test_orphan_running_queue_claim_blocks_a_direct_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Claimed queue work")
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE mission_queue SET status='running', started_at=?, run_token=? "
                    "WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), "active-queue", queue_id),
                )

            with self.assertRaisesRegex(RuntimeError, f"Queue mission {queue_id} is already running"):
                company.run("Unrelated direct work")
            self.assertEqual(company.jobs(), [])

    def test_ollama_metrics_are_isolated_per_worker_thread(self):
        model = OllamaModel("llama3.2:1b")
        observed = {}
        ready = threading.Barrier(2)

        def record(name, output_tokens):
            model.last_metrics = {"output_tokens": output_tokens}
            ready.wait(timeout=3)
            observed[name] = model.last_metrics["output_tokens"]

        first = threading.Thread(target=record, args=("first", 11))
        second = threading.Thread(target=record, args=("second", 22))
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)

        self.assertEqual(observed, {"first": 11, "second": 22})
        self.assertEqual(model.last_metrics, {})

    def test_project_directory_ingestion_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / "sources"
            sources.mkdir()
            (sources / "pricing.md").write_text("Project Atlas price target is 42 units.", encoding="utf-8")
            nested = sources / "nested"
            nested.mkdir()
            (nested / "private.md").write_text("Nested secret phrase.", encoding="utf-8")
            company = Company(root / "state", MockModel())
            atlas = company.create_project("Atlas", "Pricing work")
            company.create_project("Beacon")
            changed, unchanged, _ = company.add_knowledge_dir(sources, "Atlas")
            self.assertEqual((changed, unchanged), (1, 0))
            self.assertTrue(company.search_knowledge("price target", project=atlas))
            self.assertFalse(company.search_knowledge("price target", project="Beacon"))
            self.assertFalse(company.search_knowledge("secret phrase", project="Atlas"))
            _, report = company.run("Analyze the Atlas price target", project="Atlas")
            text = report.read_text(encoding="utf-8")
            self.assertIn("Project: Atlas", text)
            self.assertIn("pricing.md", text)

    def test_executive_synthesis_receives_team_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = RecordingModel()
            company = Company(Path(tmp), model)
            _, report = company.run("Plan inventory", roles=["operations", "quality"])
            self.assertEqual(len(model.prompts), 3)
            self.assertIn("OPERATIONS", model.prompts[-1][1])
            self.assertIn("QUALITY", model.prompts[-1][1])
            self.assertIn("Executive synthesis", report.read_text(encoding="utf-8"))

    def test_resume_keeps_completed_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = FailOnceModel()
            company = Company(Path(tmp), model)
            with self.assertRaisesRegex(RuntimeError, "simulated model interruption"):
                company.run("Plan inventory", roles=["operations", "quality"])
            job_id = company.jobs()[0][0]
            self.assertEqual(company.jobs()[0][1], "failed")
            resumed_id, report = company.resume(job_id)
            self.assertEqual(resumed_id, job_id)
            self.assertEqual(len(model.prompts), 4)
            self.assertTrue(report.exists())
            detail = company.job_detail(job_id)
            self.assertEqual(detail["job"][2], "complete")
            self.assertEqual([row[2] for row in detail["assignments"]], ["complete", "complete"])

    def test_resume_reconciles_the_mission_queue_row_it_came_from(self):
        # A queue-driven mission that fails leaves its mission_queue row at
        # status='failed'. evaluate_job()'s queue-sync only finalizes a row
        # that is 'running' under a matching claim, and until now resume()
        # never reconnected to the claim it originally came from - so a
        # successful resume left the queue row stuck at 'failed' forever,
        # permanently misrepresenting a job that actually finished. Whether
        # the resumed mission passes or fails quality is a different axis
        # entirely (FailOnceModel's placeholder text fails it here) - either
        # way the row must land on a real terminal state, not stay orphaned.
        with tempfile.TemporaryDirectory() as tmp:
            model = FailOnceModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Plan inventory", roles=["operations", "quality"])
            with self.assertRaisesRegex(RuntimeError, "simulated model interruption"):
                company.run_next_queue_item(queue_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                queue_status, job_id = db.execute(
                    "SELECT status, job_id FROM mission_queue WHERE id=?", (queue_id,),
                ).fetchone()
            self.assertEqual(queue_status, "failed")
            self.assertEqual(company.job_detail(job_id)["job"][2], "failed")

            resumed_id, report = company.resume(job_id)
            self.assertEqual(resumed_id, job_id)
            self.assertTrue(report.exists())
            self.assertEqual(company.job_detail(job_id)["job"][2], "complete")

            with closing(sqlite3.connect(company.db_path)) as db:
                final_status, final_job_id, final_run_token = db.execute(
                    "SELECT status, job_id, run_token FROM mission_queue WHERE id=?",
                    (queue_id,),
                ).fetchone()
            self.assertEqual(final_status, "quality_failed")
            self.assertEqual(final_job_id, job_id)
            self.assertIsNone(final_run_token)
            self.assertEqual(company.queue_items("running"), [])
            self.assertEqual(company.queue_items("failed"), [])
            self.assertEqual(len(company.queue_items("quality_failed")), 1)

    def test_resumed_job_is_never_reused_under_a_different_runtime_identity(self):
        class RuntimeStructuredModel(MockModel):
            def __init__(self, identity, fail=False):
                self.identity = identity
                self.fail = fail
                self.calls = 0

            def cache_identity(self):
                return {"provider": "test", "identity": self.identity}

            def complete(self, system, prompt):
                self.calls += 1
                return "Review frozen evidence locally and preserve owner control."

            def complete_structured(self, system, prompt, schema):
                self.calls += 1
                if self.fail:
                    raise RuntimeError("structured runtime failed")
                return {}

        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Every verified "
            "claim must name its exact source filename and matching supplied evidence ID."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "The frozen local baseline exists while adoption remains unknown.",
                encoding="utf-8",
            )
            model_a_failed = RuntimeStructuredModel("runtime-a", fail=True)
            company = Company(root / "state", model_a_failed)
            project = company.create_project("Resume Identity")
            company.add_knowledge(source, project)
            with self.assertRaisesRegex(RuntimeError, "failed closed"):
                company.run(objective, roles=["quality"], project=project)
            failed_job_id = company.jobs()[0][0]

            model_b = RuntimeStructuredModel("runtime-b")
            company.model = model_b
            resumed_job_id, _ = company.resume(failed_job_id)
            self.assertEqual(resumed_job_id, failed_job_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                fingerprint = db.execute(
                    "SELECT input_fingerprint FROM jobs WHERE id=?", (failed_job_id,),
                ).fetchone()[0]
            self.assertIsNone(fingerprint)
            self.assertIn(
                "cache_invalidated",
                {event[0] for event in company.job_detail(failed_job_id)["events"]},
            )

            model_a_fresh = RuntimeStructuredModel("runtime-a")
            company.model = model_a_fresh
            new_job_id, _ = company.run(
                objective, roles=["quality"], project=project,
            )
            self.assertNotEqual(new_job_id, resumed_job_id)
            self.assertGreater(model_a_fresh.calls, 0)

    def test_second_concurrent_mission_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.initialize()
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "INSERT INTO jobs(id, objective, status, created_at, heartbeat_at) "
                    "VALUES ('activejob', 'existing work', 'running', 'now', 'now')"
                )
            with self.assertRaisesRegex(RuntimeError, "already running"):
                company.run("Start another mission")

    def test_dashboard_is_read_only_snapshot_and_escapes_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.create_project("<script>alert(1)</script>")
            company.request_action("Review <unsafe> text")
            with patch("local_company.dashboard.vision_product_status", return_value={
                "contract": "local-company.supermega-vision-product-status.v1",
                "status": "collection_required",
                "commercial_status": "hold",
                "dataset_ready": False,
                "dataset": {
                    "samples": 13, "minimum_samples": 90,
                    "minimum_new_samples_lower_bound": 77,
                },
                "evidence": {
                    "readiness_receipt_id": "c" * 24,
                    "blocking_gate_count": 18,
                },
                "next_action": "collect_review_and_reassess_owned_screenshots",
            }), patch("local_company.dashboard.vision_sales_status", return_value={
                "contract": "local-company.supermega-vision-sales-status.v1",
                "status": "ready",
                "pipeline": {
                    "qualified_drafts": 0, "blocked_drafts": 0,
                    "integrity_failures": 0, "input_attention": 0,
                },
                "research": {
                    "researched_unsent_unqualified": 5,
                    "outreach_drafts_ready": 5,
                    "founding_pilot_packages_ready": 2,
                    "integrity_failures": 0,
                },
            }):
                page = render_dashboard(company, build_identity={
                    "schema": "local-company.runtime-build.v2",
                    "package_version": "0.1.0",
                    "build_id": "<script>build</script>",
                    "source_sha256": "a" * 64,
                })
            self.assertIn("Local Agent Company", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
            self.assertIn("Review &lt;unsafe&gt; text", page)
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;build&lt;/script&gt;", page)
            self.assertNotIn("<script>build</script>", page)
            self.assertIn("SuperMega Vision product evidence", page)
            self.assertIn("13/90", page)
            self.assertIn("Owned verified samples", page)
            self.assertIn("commercial claims: <span class=\"gate\">hold</span>", page)
            self.assertIn("collect_review_and_reassess_owned_screenshots", page)
            self.assertIn("founding_pilot_owned_data_collection_and_held_out_evaluation", page)
            self.assertIn("founding_pilot_package_internal_review", page)
            self.assertIn("local drafts ready: 5", page)
            self.assertIn("claim-safe packages ready: 2", page)
            self.assertIn("Vision claim-safe pilot packages", page)
            self.assertIn("external send authorized: False", page)

    def test_dashboard_hides_the_vision_banner_when_no_vision_product_is_configured(self):
        # vision_product_status() defaults to a machine-wide product root
        # (default_vision_product_root(), not this company's home) that most
        # installs of this now-public, general-purpose tool never configure.
        # That is the REAL default shape it returns in that case - the
        # dashboard used to render the whole "SuperMega Vision product
        # evidence" banner regardless, showing internal sales jargon on
        # every fresh install.
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with patch("local_company.dashboard.vision_product_status", return_value={
                "contract": "local-company.supermega-vision-product-status.v1",
                "status": "unavailable",
                "dataset_ready": False,
                "commercial_status": "hold",
                "reason": "vision_product_root_unavailable",
                "next_action": "restore_and_verify_local_vision_product_evidence",
                "controls": {
                    "model_calls": 0, "network_requests": 0, "external_sends": 0,
                    "payments": 0, "files_written": 0, "paths_included": False,
                    "pixels_included": False, "annotations_included": False,
                },
            }):
                page = render_dashboard(company, build_identity={
                    "schema": "local-company.runtime-build.v2",
                    "package_version": "0.1.0",
                    "build_id": "test-build",
                    "source_sha256": "a" * 64,
                })
            self.assertIn("Local Agent Company", page)
            self.assertNotIn("SuperMega Vision product evidence", page)
            self.assertNotIn('class="vision-banner"', page)
            self.assertNotIn("founding_pilot_owned_data_collection_and_held_out_evaluation", page)
            self.assertNotIn("Vision reviewed samples / minimum", page)
            self.assertNotIn("Vision local drafts ready, unsent", page)
            self.assertNotIn("Vision claim-safe pilot packages", page)

    def test_vision_capture_lab_is_static_local_and_contains_no_company_data(self):
        rendered = {
            state: render_vision_capture_fixture(state, index)
            for index, state in enumerate(
                ("ready", "loading", "error", "degraded"), 1,
            )
        }
        self.assertEqual(len({hashlib.sha256(value.encode()).hexdigest() for value in rendered.values()}), 4)
        for state, page in rendered.items():
            self.assertIn("CONTROLLED VISION FIXTURE", page)
            self.assertIn(f"/ {state.upper()} /", page)
            self.assertIn("No customer or operator data", page)
            self.assertIn("not production evidence", page)
            self.assertNotIn("<script", page)
        with self.assertRaisesRegex(ValueError, "Unknown Vision"):
            render_vision_capture_fixture("private", 1)
        with self.assertRaisesRegex(ValueError, "between 1 and 32"):
            render_vision_capture_fixture("ready", 33)

        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            company.create_project("PRIVATE PROJECT MUST NOT APPEAR")
            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(
                    base + "/vision-capture-lab/error/7", timeout=3,
                ) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(
                        response.headers["X-SuperMega-Vision-Fixture"],
                        "controlled-owned-local",
                    )
                    self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("CONTROLLED VISION FIXTURE", body)
                self.assertNotIn("PRIVATE PROJECT MUST NOT APPEAR", body)
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    opener.open(base + "/vision-capture-lab/ready/33", timeout=3)
                self.assertEqual(invalid.exception.code, 404)
                invalid.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_port_cannot_be_silently_shadowed_by_a_second_process(self):
        # http.server.HTTPServer sets allow_reuse_address=True in the stdlib.
        # On Windows that lets an unrelated process bind the SAME loopback
        # address:port while the first is still actively listening, rather
        # than only easing TIME_WAIT reuse the way it does on POSIX -- this
        # dashboard's entire "no auth, loopback only" safety pitch depends on
        # actually owning that port. A collision must fail loudly, not
        # coexist silently.
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            first = create_dashboard_server(company, 0)
            self.assertFalse(first.allow_reuse_address)
            bound_port = first.server_address[1]
            try:
                with self.assertRaises(OSError):
                    create_dashboard_server(company, bound_port)
            finally:
                first.server_close()

    def test_dataset_quality_dashboard_withholds_paths_rows_and_handles_bad_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "private-records.csv"
            source.write_text(
                "<script>column</script>,id,amount\n"
                "never-render-row-alpha,7,10\n"
                "never-render-row-beta,7,100\n",
                encoding="utf-8",
            )
            original = source.read_bytes()
            company = Company(root / "state", MockModel())
            company.create_project("Data <Lab>")
            dataset_id, brief_path, profile = company.profile_dataset(
                source, "Data <Lab>", key_columns=["id"],
            )

            summary = company.dataset_quality_items()[0]
            self.assertEqual(summary["id"], dataset_id)
            self.assertEqual(summary["profile_status"], "ready")
            self.assertEqual(summary["quality_status"], "review")
            self.assertEqual(summary["key_status"], "review")
            self.assertEqual(summary["quality_signal_count"], 1)
            serialized_summary = json.dumps(summary)
            self.assertNotIn(str(source), serialized_summary)
            self.assertNotIn(str(brief_path), serialized_summary)
            self.assertNotIn("never-render-row", serialized_summary)

            snapshot = dashboard_snapshot(company)
            snapshot_text = json.dumps(snapshot)
            self.assertNotIn(str(source), snapshot_text)
            self.assertNotIn(str(brief_path), snapshot_text)
            self.assertNotIn("never-render-row", snapshot_text)
            page = render_dashboard(company)
            self.assertIn(f'/datasets/{dataset_id}', page)
            self.assertIn("Data &lt;Lab&gt;", page)
            self.assertNotIn(str(source), page)
            self.assertNotIn(str(brief_path), page)
            self.assertNotIn("never-render-row", page)

            detail_payload = company.dataset_quality_detail(dataset_id)
            serialized_detail = json.dumps(detail_payload)
            self.assertNotIn(str(source), serialized_detail)
            self.assertNotIn(str(brief_path), serialized_detail)
            self.assertNotIn("never-render-row", serialized_detail)
            detail_page = render_dataset_quality_detail(company, dataset_id)
            self.assertIn("stored aggregate statistics only", detail_page)
            self.assertIn("Declared key check", detail_page)
            self.assertIn("Rows affected", detail_page)
            self.assertIn("&lt;script&gt;column&lt;/script&gt;", detail_page)
            self.assertNotIn("<script>column</script>", detail_page)
            self.assertNotIn(str(source), detail_page)
            self.assertNotIn(str(brief_path), detail_page)
            self.assertNotIn("never-render-row", detail_page)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(profile["key_check"]["duplicate_rows"], 2)

            legacy_profile = dict(profile)
            legacy_profile["schema"] = "local-company.dataset-profile.v2"
            legacy_profile.pop("contract_check", None)
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE datasets SET profile_json=? WHERE id=?",
                    (json.dumps(legacy_profile), dataset_id),
                )
            legacy = company.dataset_quality_items()[0]
            self.assertEqual(legacy["profile_status"], "ready")
            self.assertEqual(legacy["profile_schema"], "local-company.dataset-profile.v2")
            self.assertEqual(legacy["contract_status"], "not configured")

            bounded_profile = dict(profile)
            bounded_profile["columns"] = {
                f"bounded-column-{index:03}": {
                    "missing": 0,
                    "missing_rate": 0.0,
                    "unique_non_missing": 1,
                    "unique_rate": 1.0,
                    "types": {"string": 1},
                    "mixed_types": False,
                    "non_finite_numeric": 0,
                    "numeric_values_excluded": 0,
                }
                for index in range(205)
            }
            bounded_profile["column_count"] = 205
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE datasets SET profile_json=?, column_count=? WHERE id=?",
                    (json.dumps(bounded_profile), 205, dataset_id),
                )
            bounded_page = render_dataset_quality_detail(company, dataset_id)
            self.assertIn("5 additional columns are omitted", bounded_page)
            self.assertIn("bounded-column-199", bounded_page)
            self.assertNotIn("bounded-column-200", bounded_page)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE datasets SET profile_json=? WHERE id=?",
                    (json.dumps({
                        "schema": "local-company.dataset-profile.v2",
                        "columns": {}, "quality_flags": {}, "key_check": {},
                    }), dataset_id),
                )
            structurally_invalid = company.dataset_quality_items()[0]
            self.assertEqual(structurally_invalid["profile_status"], "unavailable")
            self.assertIn(
                "Aggregate profile unavailable",
                render_dataset_quality_detail(company, dataset_id),
            )

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE datasets SET profile_json=? WHERE id=?",
                    ('{"schema":', dataset_id),
                )
            unavailable = company.dataset_quality_items()[0]
            self.assertEqual(unavailable["profile_status"], "unavailable")
            corrupted_page = render_dataset_quality_detail(company, dataset_id)
            self.assertIn("Aggregate profile unavailable", corrupted_page)
            self.assertNotIn(str(source), corrupted_page)

    def test_dataset_quality_http_route_is_local_read_only_and_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "records.json"
            source.write_text(
                '[{"id": 1, "label": "row-secret"}, {"id": 2, "label": "other-secret"}]',
                encoding="utf-8",
            )
            company = Company(root / "state", MockModel())
            company.create_project("Quality Lab")
            dataset_id, brief_path, _ = company.profile_dataset(
                source, "Quality Lab", key_columns=["id"],
            )
            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + f"/datasets/{dataset_id}", timeout=3) as response:
                    detail = response.read().decode("utf-8")
                self.assertIn("complete and unique", detail)
                self.assertNotIn("row-secret", detail)
                self.assertNotIn("other-secret", detail)
                self.assertNotIn(str(source), detail)
                self.assertNotIn(str(brief_path), detail)

                with opener.open(base + "/health.json", timeout=3) as response:
                    health_text = response.read().decode("utf-8")
                self.assertNotIn("row-secret", health_text)
                self.assertNotIn(str(source), health_text)
                self.assertNotIn(str(brief_path), health_text)

                for missing_path in (
                    "/datasets/deadbeef0000",
                    f"/datasets/{dataset_id}/extra",
                    "/datasets/../../private-records.csv",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        opener.open(base + missing_path, timeout=3)
                    self.assertEqual(missing.exception.code, 404)
                    missing.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_health_endpoint_is_bounded_and_withholds_business_records(self):
        class NoisyWorker:
            def snapshot(self):
                return {
                    "status": "running",
                    "output": "health-private-worker-output" * 100_000,
                    "error": "health-private-worker-error",
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "health-private-source.json"
            source.write_text(
                '[{"id": 1, "label": "health-private-row-value"}]',
                encoding="utf-8",
            )
            company = Company(root / "state", MockModel())
            project_id = company.create_project("health-private-project")
            company.enqueue("health-private-queue-objective", project=project_id)
            company.request_action("health-private-approval-description")
            company.profile_dataset(source, "health-private-project", key_columns=["id"])
            worker = NoisyWorker()
            service_instance_id = "c" * 32
            direct = health_endpoint_snapshot(
                company,
                worker,
                {"build_id": "health-private-build-secret"},
                company.company_identity(),
                service_instance_id,
            )
            self.assertEqual(direct["worker"], {"status": "running"})
            self.assertNotIn("worker-output", json.dumps(direct))
            self.assertNotIn("worker-error", json.dumps(direct))
            server = create_dashboard_server(
                company,
                0,
                service_token="health-private-service-token",
                service_instance_id=service_instance_id,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/health.json", timeout=3) as response:
                    payload_bytes = response.read()
                    self.assertLess(int(response.headers["Content-Length"]), 4096)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                payload = json.loads(payload_bytes)
                self.assertEqual(set(payload), {
                    "schema", "status", "pid", "service_instance_id", "build",
                    "company", "health", "worker",
                })
                self.assertEqual(payload["schema"], "local-company.health.v1")
                self.assertEqual(payload["service_instance_id"], service_instance_id)
                self.assertEqual(payload["worker"], {"status": "idle"})
                self.assertEqual(set(payload["health"]), {
                    "python", "platform", "database_bytes", "report_count",
                    "report_bytes", "disk_free_bytes", "disk_total_bytes",
                    "ollama_model_storage_bytes", "ollama_reachable",
                    "installed_model_count", "dataset_count", "active_jobs",
                    "queued_missions", "running_missions", "pending_approvals",
                    "pending_report_finalizations", "pending_evaluations",
                })
                self.assertEqual(payload["health"]["dataset_count"], 1)
                self.assertEqual(payload["health"]["queued_missions"], 1)
                self.assertEqual(payload["health"]["pending_approvals"], 1)
                self.assertFalse(payload["health"]["ollama_reachable"])
                self.assertIsNone(payload["health"]["installed_model_count"])
                serialized = payload_bytes.decode("utf-8")
                self.assertNotIn("health-private", serialized)
                self.assertNotIn(str(source), serialized)
                self.assertNotIn(str(company.home), serialized)
                self.assertNotIn("pending_completion", serialized)
                self.assertNotIn('"projects":', serialized)
                self.assertNotIn('"queue":', serialized)
                self.assertNotIn('"datasets":', serialized)

                with opener.open(base + "/", timeout=3) as response:
                    page = response.read().decode("utf-8")
                self.assertIn("health-private-project", page)
                self.assertIn("health-private-queue-objective", page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_build_status_snapshot_is_bounded_and_covers_worker_transition(self):
        class Worker:
            status = "idle"

            def snapshot(self):
                return {"status": self.status, "output": "x" * (2 * 1024 * 1024)}

        worker = Worker()

        class CompanyState:
            def work_state_snapshot(self):
                worker.status = "running"
                return {
                    "active_jobs": 0,
                    "queued_missions": 0,
                    "running_missions": 0,
                    "pending_approvals": 0,
                    "pending_report_finalizations": 0,
                    "pending_evaluations": 0,
                }

        snapshot = build_status_snapshot(
            CompanyState(), worker, {
                "schema": "local-company.runtime-build.v2",
                "package_version": "0.1.0",
                "build_id": "local-build-20260727.4",
                "git_commit": None,
                "source_dirty": None,
                "source_sha256": "a" * 64,
            },
            {
                "provider": "ollama", "model": "llama3.2:1b",
                "endpoint": "loopback_default",
            },
            {
                "schema": COMPANY_STORE_SCHEMA,
                "instance_id": "123e4567e89b42d3a456426614174000",
            },
        )
        self.assertEqual(snapshot["worker"], {"status": "running"})
        self.assertEqual(
            snapshot["runtime"], {
                "provider": "ollama", "model": "llama3.2:1b",
                "endpoint": "loopback_default",
            },
        )
        self.assertEqual(
            snapshot["company"], {
                "schema": COMPANY_STORE_SCHEMA,
                "instance_id": "123e4567e89b42d3a456426614174000",
            },
        )
        self.assertLess(len(json.dumps(snapshot).encode("utf-8")), 2048)

        with tempfile.TemporaryDirectory() as tmp:
            ollama_company = Company(Path(tmp), OllamaModel("llama3.2:1b"))
            self.assertEqual(
                runtime_model_identity(ollama_company),
                {
                    "provider": "ollama", "model": "llama3.2:1b",
                    "endpoint": "loopback_default",
                },
            )
            ollama_company.model.model = "x" * 257
            self.assertEqual(runtime_model_identity(ollama_company)["model"], None)
            external_company = Company(
                Path(tmp), OllamaModel("llama3.2:1b", host="https://example.invalid"),
            )
            self.assertEqual(runtime_model_identity(external_company)["endpoint"], "nonlocal")

    def test_dashboard_http_is_local_and_rejects_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            build_identity = {
                "schema": "local-company.runtime-build.v2",
                "package_version": "0.1.0",
                "build_id": "local-build-test",
                "git_commit": None,
                "source_dirty": None,
                "source_sha256": "b" * 64,
            }
            runtime_identity = {
                "provider": "ollama", "model": "llama3.2:1b",
                "endpoint": "loopback_default",
            }
            company_identity = company.company_identity()
            with patch(
                "local_company.dashboard.runtime_build_identity",
                return_value=build_identity,
            ) as build_snapshot, patch(
                "local_company.dashboard.runtime_model_identity",
                return_value=runtime_identity,
            ) as runtime_snapshot:
                with patch.object(
                    company, "company_identity", return_value=company_identity,
                ) as company_snapshot:
                    server = create_dashboard_server(company, 0)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/health.json", timeout=3) as response:
                    health = json.load(response)
                self.assertEqual(health["status"], "ready")
                self.assertEqual(health["build"], build_identity)
                self.assertEqual(health["company"], company_identity)
                with opener.open(
                    base + "/health.json?view=build-status", timeout=3,
                ) as response:
                    build_status = json.load(response)
                    self.assertLess(int(response.headers["Content-Length"]), 4096)
                self.assertEqual(
                    set(build_status),
                    {
                        "status", "pid", "build", "runtime", "company", "health",
                        "worker",
                    },
                )
                self.assertEqual(build_status["build"], build_identity)
                self.assertEqual(build_status["runtime"], runtime_identity)
                self.assertEqual(build_status["company"], company_identity)
                self.assertNotIn(str(company.home), json.dumps(build_status))
                self.assertEqual(build_status["worker"], {"status": "disabled"})
                with opener.open(base + "/build-status.json", timeout=3) as response:
                    self.assertEqual(json.load(response), build_status)
                with opener.open(base + "/health.json", timeout=3) as response:
                    self.assertEqual(json.load(response)["build"], build_identity)
                with opener.open(base + "/", timeout=3) as response:
                    page = response.read().decode("utf-8")
                self.assertIn("local-build-test", page)
                self.assertIn("b" * 64, page)
                self.assertIn("/build-status.json", page)
                build_snapshot.assert_called_once_with()
                runtime_snapshot.assert_called_once_with(company)
                company_snapshot.assert_called_once_with()
                request = urllib.request.Request(base + "/", data=b"", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(request, timeout=3)
                self.assertEqual(raised.exception.code, 405)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_embedded_runtime_build_identity_matches_operational_source(self):
        project_root = Path(__file__).parents[1]
        project_metadata = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(project_metadata["project"]["version"], __version__)

        source_root = project_root / "src" / "local_company"
        release_files = [
            path for path in source_root.rglob("*.py")
            if path.relative_to(source_root).as_posix() != "build_info.py"
        ] + [
            project_root / relative for relative in (
                "scripts/check_company_mcp.py",
                "scripts/check_live_build.py",
                "scripts/check_readiness.py",
                "scripts/check_runtime_supervisor.py",
                "scripts/local_ai.py",
                "scripts/manage_cycle_task.ps1",
                "scripts/run_local_brief_assistant.py",
                "scripts/run_local_company_prompt.py",
                "scripts/run_scheduled_cycle.py",
                "scripts/runtime_guard.py", "scripts/setup_local_ai.py",
                "scripts/stamp_build_manifest.py",
            )
        ]
        release_files.sort(
            key=lambda path: path.relative_to(project_root).as_posix().encode("utf-8"),
        )
        self.assertEqual(
            {path.relative_to(project_root).as_posix() for path in release_files},
            {
                "scripts/check_company_mcp.py", "scripts/check_live_build.py",
                "scripts/check_readiness.py",
                "scripts/check_runtime_supervisor.py",
                "scripts/local_ai.py", "scripts/manage_cycle_task.ps1",
                "scripts/run_local_brief_assistant.py",
                "scripts/run_local_company_prompt.py",
                "scripts/run_scheduled_cycle.py",
                "scripts/runtime_guard.py", "scripts/setup_local_ai.py",
                "scripts/stamp_build_manifest.py",
                "src/local_company/__init__.py", "src/local_company/cli.py",
                "src/local_company/browser_operator.py",
                "src/local_company/capacity.py",
                "src/local_company/computer_use.py",
                "src/local_company/config.py", "src/local_company/core.py",
                "src/local_company/dashboard.py", "src/local_company/service.py",
                "src/local_company/focus.py", "src/local_company/spreadsheet.py",
                "src/local_company/model_policy.py",
                "src/local_company/supermega.py", "src/local_company/mcp_server.py",
                "src/local_company/workflow_lab.py",
                "src/local_company/workflow_pilot.py",
            },
        )
        expected = hashlib.sha256()
        expected.update(b"local-company.release-source.v1\0")
        for path in release_files:
            relative = path.relative_to(project_root).as_posix().encode("utf-8")
            content = path.read_bytes()
            expected.update(len(relative).to_bytes(4, "big"))
            expected.update(relative)
            expected.update(len(content).to_bytes(8, "big"))
            expected.update(content)
        self.assertEqual(SOURCE_SHA256, expected.hexdigest())

        with (
            patch("builtins.open", side_effect=AssertionError("runtime file read")),
            patch("os.open", side_effect=AssertionError("runtime file read")),
            patch("subprocess.run", side_effect=AssertionError("runtime process launch")),
        ):
            identity = runtime_build_identity()
            second = runtime_build_identity()
        self.assertEqual(identity, {
            "schema": RUNTIME_BUILD_SCHEMA,
            "package_version": __version__,
            "build_id": BUILD_ID,
            "git_commit": None,
            "source_dirty": None,
            "source_sha256": SOURCE_SHA256,
        })
        self.assertEqual(second, identity)
        self.assertIsNot(second, identity)
        self.assertRegex(BUILD_ID, r"\Alocal-build-[0-9]{8}\.[0-9]+\Z")
        self.assertRegex(SOURCE_SHA256, r"\A[0-9a-f]{64}\Z")
        self.assertIsNone(identity["git_commit"])
        self.assertIsNone(identity["source_dirty"])

    def test_dashboard_rejects_rebound_host_and_cross_site_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                rebound = urllib.request.Request(base + "/", headers={"Host": "attacker.example"})
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(rebound, timeout=3)
                self.assertEqual(rejected.exception.code, 421)
                rejected.exception.close()

                cross_site = urllib.request.Request(
                    base + "/queue/enqueue",
                    data=urllib.parse.urlencode({
                        "service_token": "test-secret", "objective": "Review operations",
                    }).encode(),
                    method="POST",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "http://attacker.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(cross_site, timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertEqual(company.queue_items(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_surfaces_sealed_review_without_model_or_result_exposure(self):
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "company"
            model = CountingMockModel()
            company = Company(home, model)
            project_id = company.create_project("SuperMega")
            set_execution_focus(home, project_id, "SuperMega", 4)
            objective = "Review business workflow and owner evidence"
            job_id, _ = company.run(objective, project=project_id)
            detail = company.job_detail(job_id)
            synthesis = detail["job"][7]
            calls_before = model.calls

            snapshot = dashboard_snapshot(company)
            self.assertEqual(snapshot["product_review"]["status"], "candidate_ready")
            self.assertEqual(snapshot["product_review"]["jobId"], job_id)
            self.assertNotIn("objective", snapshot["product_review"])
            self.assertNotIn("synthesis", snapshot["product_review"])
            snapshot_text = json.dumps(snapshot)
            self.assertNotIn(synthesis, snapshot_text)

            main_page = render_dashboard(company, service_token="review-secret")
            self.assertIn("Human product evidence review ready", main_page)
            self.assertIn('href="/product-review"', main_page)
            self.assertIn(job_id, main_page)
            self.assertNotIn(synthesis, main_page)

            read_only = render_product_review(company)
            self.assertIn(objective, read_only)
            self.assertIn(synthesis, read_only)
            self.assertIn("dashboard instance is read-only", read_only)
            self.assertNotIn('action="/product-review/record"', read_only)

            review_page = render_product_review(company, "review-secret")
            self.assertIn('action="/product-review/record"', review_page)
            self.assertIn(f'name="job_id" value="{job_id}"', review_page)
            self.assertIn(detail["job"][8], review_page)
            self.assertIn(detail["job"][9], review_page)
            self.assertIn("RECORD HUMAN PRODUCT EVIDENCE REVIEW", review_page)
            self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 0)
            self.assertEqual(model.calls, calls_before)

    def test_dashboard_product_review_requires_auth_and_confirmation_then_blocks_replay(self):
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "company"
            model = CountingMockModel()
            company = Company(home, model)
            project_id = company.create_project("SuperMega")
            set_execution_focus(home, project_id, "SuperMega", 4)
            job_id, _ = company.run("Review business workflow", project=project_id)
            detail = company.job_detail(job_id)
            report_sha256 = detail["job"][8]
            manifest_sha256 = detail["job"][9]
            calls_before = model.calls
            server = create_dashboard_server(
                company, 0, service_token="review-secret",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(token: str, confirmation: str) -> urllib.request.Request:
                return urllib.request.Request(
                    base + "/product-review/record",
                    data=urllib.parse.urlencode({
                        "service_token": token, "job_id": job_id,
                        "project": "SuperMega", "report_sha256": report_sha256,
                        "evidence_manifest_sha256": manifest_sha256,
                        "category": "business", "decision": "accepted",
                        "outcome_reason": "none", "corrections": "0",
                        "paid_setup_signal": "unknown", "peak_memory_mb": "512",
                        "review_confirmation": confirmation,
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(base + "/product-review", timeout=3) as response:
                    page = response.read().decode("utf-8")
                    self.assertIn(job_id, page)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(request("wrong", "RECORD HUMAN PRODUCT EVIDENCE REVIEW"), timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()

                with self.assertRaises(urllib.error.HTTPError) as cancelled:
                    opener.open(request("review-secret", "CANCEL"), timeout=3)
                self.assertEqual(cancelled.exception.code, 400)
                cancelled.exception.close()
                self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 0)

                accepted = request(
                    "review-secret", "RECORD HUMAN PRODUCT EVIDENCE REVIEW",
                )
                with opener.open(accepted, timeout=3) as response:
                    result = response.read().decode("utf-8")
                self.assertIn("Recorded actual human review", result)
                self.assertIn("No model or external action was performed", result)
                status = company.product_evidence_status(project_id)
                self.assertEqual(status["reviewed_missions"], 1)
                self.assertEqual(status["complete_measurements"], 1)
                self.assertEqual(status["reviews"][0]["job_id"], job_id)
                self.assertEqual(status["reviews"][0]["peak_memory_mb"], 512)
                self.assertEqual(model.calls, calls_before)

                with self.assertRaises(urllib.error.HTTPError) as replayed:
                    opener.open(request(
                        "review-secret", "RECORD HUMAN PRODUCT EVIDENCE REVIEW",
                    ), timeout=3)
                self.assertEqual(replayed.exception.code, 409)
                replayed.exception.close()
                self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_product_review_rejects_report_tampering_without_recording(self):
        from local_company.focus import set_execution_focus

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "company"
            model = CountingMockModel()
            company = Company(home, model)
            project_id = company.create_project("Integrity Lab")
            set_execution_focus(home, project_id, "Integrity Lab", 4)
            job_id, report = company.run(
                "Review one integrity-bound workflow", project=project_id,
            )
            detail = company.job_detail(job_id)
            calls_before = model.calls
            server = create_dashboard_server(
                company, 0, service_token="integrity-secret",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/product-review", timeout=3) as response:
                    self.assertIn(job_id.encode("utf-8"), response.read())
                report.write_text(
                    report.read_text(encoding="utf-8") + "\ntampered after review render\n",
                    encoding="utf-8",
                )
                submitted = urllib.request.Request(
                    base + "/product-review/record",
                    data=urllib.parse.urlencode({
                        "service_token": "integrity-secret", "job_id": job_id,
                        "project": "Integrity Lab", "report_sha256": detail["job"][8],
                        "evidence_manifest_sha256": detail["job"][9],
                        "category": "business", "decision": "rejected",
                        "outcome_reason": "inaccurate", "corrections": "1",
                        "paid_setup_signal": "no", "peak_memory_mb": "",
                        "review_confirmation": "RECORD HUMAN PRODUCT EVIDENCE REVIEW",
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(submitted, timeout=3)
                self.assertEqual(rejected.exception.code, 409)
                rejected.exception.close()
                self.assertEqual(company.product_evidence_status(project_id)["reviewed_missions"], 0)
                self.assertEqual(model.calls, calls_before)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_preserves_drafts_and_opens_escaped_mission_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            job_id, report = company.run("Review local inventory")
            report.write_text(report.read_text(encoding="utf-8") + "\n<script>alert(1)</script>", encoding="utf-8")
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/", timeout=3) as response:
                    page = response.read().decode("utf-8")
                self.assertNotIn("http-equiv=\"refresh\"", page)
                self.assertIn(f'/missions/{job_id}', page)
                self.assertIn("not factual or production verification", page)

                with opener.open(base + f"/missions/{job_id}", timeout=3) as response:
                    detail = response.read().decode("utf-8")
                self.assertIn("Automated checks FAILED", detail)
                self.assertIn("model stopped cleanly", detail)
                self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", detail)
                self.assertNotIn("<script>alert(1)</script>", detail)

                with self.assertRaises(urllib.error.HTTPError) as missing:
                    opener.open(base + "/missions/deadbeef0000", timeout=3)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_accepts_only_authenticated_service_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                bad = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "wrong"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(bad, timeout=3)
                self.assertEqual(rejected.exception.code, 405)
                rejected.exception.close()
                good = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "test-secret"},
                )
                with opener.open(good, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                server.server_close()

    def test_dashboard_authenticated_queue_intake_and_cancel_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("SuperMega")
            server = create_dashboard_server(company, 0, service_token="test-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path, fields):
                return urllib.request.Request(
                    base + path,
                    data=urllib.parse.urlencode(fields).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(base + "/", timeout=3) as response:
                    page = response.read()
                    self.assertIn(b"Queue a SuperMega task", page)
                    self.assertIn(b"test-secret", page)
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                with opener.open(base + "/health.json", timeout=3) as response:
                    self.assertNotIn(b"test-secret", response.read())

                bad = post(
                    "/queue/enqueue",
                    {"service_token": "wrong", "objective": "Review operations", "priority": "60"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(bad, timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertEqual(company.queue_items(), [])

                enqueue = post(
                    "/queue/enqueue",
                    {
                        "service_token": "test-secret",
                        "objective": "Review local SuperMega operations",
                        "project": project_id,
                        "playbook": "operations-improvement",
                        "priority": "75",
                    },
                )
                with opener.open(enqueue, timeout=3) as response:
                    self.assertIn(b"nothing was executed", response.read())
                queued = company.queue_items("queued")
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0][2], 75)
                self.assertEqual(queued[0][4], "SuperMega")
                self.assertEqual(len(company.jobs()), 0)

                cancel = post(
                    "/queue/cancel",
                    {"service_token": "test-secret", "queue_id": queued[0][0]},
                )
                with opener.open(cancel, timeout=3) as response:
                    self.assertIn(b"Cancelled queued mission", response.read())
                self.assertEqual(company.queue_items("queued"), [])
                self.assertEqual(company.queue_items("cancelled")[0][0], queued[0][0])
                with closing(sqlite3.connect(company.db_path)) as db:
                    events = list(db.execute(
                        "SELECT kind, detail FROM events WHERE kind LIKE 'queue_%' ORDER BY id"
                    ))
                self.assertEqual([row[0] for row in events], ["queue_enqueued", "queue_cancelled"])
                self.assertTrue(all('"source": "dashboard"' in row[1] for row in events))

                too_large = urllib.request.Request(
                    base + "/queue/enqueue", data=b"x" * 17000, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(too_large, timeout=3)
                self.assertEqual(rejected.exception.code, 413)
                rejected.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_team_preview_preserves_draft_without_work_or_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            project_id = company.create_project("Preview Lab")
            source = Path(tmp) / "preview-private.md"
            source.write_text("private dashboard preview datum", encoding="utf-8")
            company.add_knowledge(source, project_id)
            server = create_dashboard_server(company, 0, service_token="preview-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(objective: str, *, token: str = "preview-secret"):
                return urllib.request.Request(
                    base + "/queue/preview-team",
                    data=urllib.parse.urlencode({
                        "service_token": token,
                        "objective": objective,
                        "project": project_id,
                        "playbook": "procurement-review",
                        "priority": "74",
                    }).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(base + "/", timeout=3) as response:
                    self.assertIn(b"Preview team (no model)", response.read())
                before_digest = hashlib.sha256(company.db_path.read_bytes()).hexdigest()
                objective = "Improve supplier <script> controls and inventory workflow"
                with opener.open(post(objective), timeout=3) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertIn("Team preview only (playbook)", page)
                self.assertIn("procurement", page)
                self.assertIn("legal-risk", page)
                self.assertIn("No model was called", page)
                self.assertIn("no mission was queued", page)
                self.assertIn(
                    "Knowledge preflight ready: 1 registered source(s) current", page,
                )
                self.assertIn("&lt;script&gt;", page)
                self.assertNotIn("<script>", page)
                self.assertIn(f'value="{project_id}" selected', page)
                self.assertIn('value="procurement-review" selected', page)
                self.assertIn('value="74"', page)
                self.assertEqual(model.calls, 0)
                self.assertEqual(company.queue_items(), [])
                self.assertEqual(company.jobs(), [])
                self.assertEqual(company.action_requests(), [])
                self.assertEqual(
                    hashlib.sha256(company.db_path.read_bytes()).hexdigest(),
                    before_digest,
                )

                source.write_text("private dashboard preview changed", encoding="utf-8")
                with opener.open(post("Review inventory"), timeout=3) as response:
                    blocked_page = response.read().decode("utf-8")
                self.assertIn(
                    "Model execution preflight blocked: knowledge_changed", blocked_page,
                )
                self.assertIn("Queuing remains record-only", blocked_page)
                self.assertNotIn(str(source.resolve()), blocked_page)
                self.assertNotIn("private dashboard preview datum", blocked_page)
                self.assertNotIn("private dashboard preview changed", blocked_page)

                with opener.open(post("Send email to every prospect"), timeout=3) as response:
                    gated_page = response.read().decode("utf-8")
                self.assertIn("Owner gate required before execution", gated_page)
                self.assertIn("external communication", gated_page)
                self.assertIn(
                    "Knowledge was not checked because the owner gate stops", gated_page,
                )
                self.assertEqual(company.action_requests(), [])
                self.assertEqual(model.calls, 0)

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(post("Review inventory", token="wrong"), timeout=3)
                self.assertEqual(rejected.exception.code, 403)
                rejected.exception.close()
                self.assertEqual(company.queue_items(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_run_next_is_explicit_single_worker_and_refuses_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel()
            company = Company(Path(tmp), model)
            queue_id = company.enqueue("Review local inventory", priority=80)
            server = create_dashboard_server(company, 0, service_token="worker-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def run_request():
                return urllib.request.Request(
                    base + "/queue/run-next",
                    data=urllib.parse.urlencode({
                        "service_token": "worker-secret", "queue_id": queue_id,
                    }).encode(),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

            try:
                with opener.open(run_request(), timeout=3) as response:
                    self.assertIn(
                        f"Started reviewed local mission {queue_id}".encode(), response.read(),
                    )
                self.assertTrue(model.started.wait(timeout=3))

                with self.assertRaises(urllib.error.HTTPError) as duplicate:
                    opener.open(run_request(), timeout=3)
                self.assertEqual(duplicate.exception.code, 409)
                duplicate.exception.close()

                stop = urllib.request.Request(
                    base + "/__service/stop", data=b"", method="POST",
                    headers={"X-Service-Token": "worker-secret"},
                )
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    opener.open(stop, timeout=3)
                self.assertEqual(refused.exception.code, 409)
                refused.exception.close()

                model.release.set()
                deadline = time.monotonic() + 5
                status = "running"
                while status == "running" and time.monotonic() < deadline:
                    with opener.open(base + "/health.json", timeout=3) as response:
                        status = json.load(response)["worker"]["status"]
                    if status == "running":
                        time.sleep(0.05)
                self.assertEqual(status, "complete")
                self.assertEqual(len(company.queue_items("complete")), 1)
                self.assertEqual(len(company.jobs()), 1)
            finally:
                model.release.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_dashboard_runs_only_the_exact_reviewed_next_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            project_id = company.create_project("Reviewed Project")
            reviewed_id = company.enqueue(
                "Reviewed dashboard work", project=project_id, priority=20,
            )
            rendered = render_dashboard(company, service_token="review-secret")
            self.assertIn(
                f'name="queue_id" value="{reviewed_id}"', rendered,
            )
            self.assertIn(f"Run {reviewed_id} locally", rendered)
            self.assertIn("Reviewed dashboard work", rendered)
            self.assertIn("project Reviewed Project, due", rendered)

            server = create_dashboard_server(company, 0, service_token="review-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            urgent_id = company.enqueue("New higher-priority work", priority=90)
            request = urllib.request.Request(
                base + "/queue/run-next",
                data=urllib.parse.urlencode({
                    "service_token": "review-secret", "queue_id": reviewed_id,
                }).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with self.assertRaises(urllib.error.HTTPError) as changed:
                    opener.open(request, timeout=3)
                self.assertEqual(changed.exception.code, 409)
                changed.exception.close()
                self.assertEqual(
                    {row[0] for row in company.queue_items("queued")},
                    {reviewed_id, urgent_id},
                )
                self.assertEqual(company.jobs(), [])
                self.assertEqual(model.calls, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_worker_thread_start_failure_fails_claim_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Recover worker startup")
            worker = LocalQueueWorker(company)

            with patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
                with self.assertRaisesRegex(RuntimeError, "no thread"):
                    worker.start(queue_id)

            failed = company.queue_items("failed")[0]
            self.assertEqual(failed[0], queue_id)
            self.assertIn("did not start", failed[8])
            company.reset_queue_item(queue_id)
            worker.start(queue_id)
            deadline = time.monotonic() + 5
            while worker.snapshot()["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(worker.snapshot()["status"], "complete")

    def test_worker_is_non_idle_before_queue_claim_mutation(self):
        company = Mock()
        worker = LocalQueueWorker(company)

        def fail_claim(_queue_id):
            self.assertEqual(worker.snapshot()["status"], "running")
            raise ValueError("claim refused")

        company.claim_next_queue_item.side_effect = fail_claim
        with self.assertRaisesRegex(ValueError, "claim refused"):
            worker.start("reviewed-queue")
        self.assertEqual(worker.snapshot()["status"], "failed")
        company.abandon_queue_claim.assert_not_called()

    def test_worker_exposes_durable_completion_pending_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Review completion visibility", roles=["operations"])
            worker = LocalQueueWorker(company)
            with patch.object(
                company, "evaluate_job", side_effect=RuntimeError("simulated evaluator pause"),
            ):
                worker.start(queue_id)
                deadline = time.monotonic() + 5
                while worker.snapshot()["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.05)

            worker_state = worker.snapshot()
            self.assertEqual(worker_state["status"], "completion_pending")
            self.assertEqual(worker_state["queue_id"], queue_id)
            self.assertIn("simulated evaluator pause", worker_state["error"])
            self.assertEqual(company.health_snapshot()["pending_evaluations"], 1)
            rendered = render_dashboard(company, service_token="local-review", worker=worker)
            self.assertIn("completion_pending", rendered)
            self.assertIn("Mission completion pending", rendered)

    def test_dashboard_worker_preserves_sensitive_action_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Send email to every prospect", priority=90)
            server = create_dashboard_server(company, 0, service_token="gate-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            request = urllib.request.Request(
                base + "/queue/run-next",
                data=urllib.parse.urlencode({
                    "service_token": "gate-secret", "queue_id": queue_id,
                }).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with opener.open(request, timeout=3) as response:
                    response.read()
                deadline = time.monotonic() + 3
                status = "running"
                while status == "running" and time.monotonic() < deadline:
                    with opener.open(base + "/health.json", timeout=3) as response:
                        status = json.load(response)["worker"]["status"]
                    if status == "running":
                        time.sleep(0.05)
                self.assertEqual(status, "needs_approval")
                self.assertEqual(len(company.queue_items("needs_approval")), 1)
                self.assertEqual(len(company.action_requests("pending")), 1)
                self.assertEqual(company.jobs(), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_queue_objective_length_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            with self.assertRaisesRegex(ValueError, "cannot exceed 4000"):
                company.enqueue("x" * 4001)

    def test_queue_item_can_be_parked_and_restored_without_work_or_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = CountingMockModel()
            company = Company(Path(tmp), model)
            older_project = company.create_project("Older Lab")
            active_project = company.create_project("Active Lab")
            parked_id = company.enqueue(
                "Prepare the older advisory brief", older_project, priority=80,
            )
            active_id = company.enqueue(
                "Prepare the active advisory brief", active_project, priority=50,
            )
            scheduled_before = company.queue_items("queued")[0][3]
            reason = "Preserve this mission while another project is the active focus."

            parked = company.park_queue_item(
                parked_id, reason, source="unit-test",
            )

            self.assertEqual(parked["schema"], QUEUE_PARK_SCHEMA)
            self.assertEqual(parked["queue_id"], parked_id)
            self.assertEqual(parked["project_id"], older_project)
            self.assertEqual(parked["previous_status"], "queued")
            self.assertEqual(parked["status"], "parked")
            self.assertEqual(parked["reason"], reason)
            self.assertTrue(parked["effects"]["database_mutated"])
            self.assertTrue(parked["effects"]["queue_changed"])
            self.assertTrue(all(
                parked["effects"][key] is False for key in (
                    "model_called", "work_started", "objective_changed",
                    "schedule_changed", "queue_history_deleted",
                )
            ))
            self.assertEqual(company.next_due_queue_item()[0], active_id)
            self.assertEqual(company.queue_status_count("parked"), 1)
            parked_row = company.queue_items("parked")[0]
            self.assertEqual(parked_row[0], parked_id)
            self.assertEqual(parked_row[3], scheduled_before)
            self.assertEqual(parked_row[6], "Prepare the older advisory brief")
            self.assertEqual(model.calls, 0)

            restored = company.unpark_queue_item(
                parked_id,
                "Restore the preserved mission for its original queue position.",
                source="unit-test",
            )

            self.assertEqual(restored["schema"], QUEUE_PARK_SCHEMA)
            self.assertEqual(restored["previous_status"], "parked")
            self.assertEqual(restored["status"], "queued")
            self.assertEqual(company.next_due_queue_item()[0], parked_id)
            self.assertEqual(company.queue_status_count("parked"), 0)
            self.assertEqual(company.queue_items("queued")[0][3], scheduled_before)
            self.assertEqual(model.calls, 0)
            with closing(sqlite3.connect(company.db_path)) as db:
                events = list(db.execute(
                    "SELECT kind, detail FROM events "
                    "WHERE kind IN ('queue_parked', 'queue_unparked') ORDER BY id"
                ))
            self.assertEqual(
                [event[0] for event in events],
                ["queue_parked", "queue_unparked"],
            )
            self.assertEqual(json.loads(events[0][1])["reason"], reason)
            self.assertTrue(all(
                json.loads(event[1])["source"] == "unit-test"
                for event in events
            ))

    def test_queue_park_lifecycle_fails_closed_and_cli_is_model_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            model = CountingMockModel()
            company = Company(state, model)
            queue_id = company.enqueue("Preserve one queued advisory mission")
            database_before = company.db_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "20 to 240"):
                company.park_queue_item(queue_id, "too short")
            self.assertEqual(company.db_path.read_bytes(), database_before)
            with self.assertRaisesRegex(ValueError, "12 lowercase"):
                company.park_queue_item("not-an-id", "A sufficiently detailed audit reason for parking.")
            lower_id = company.enqueue(
                "A lower priority advisory mission", priority=1,
            )
            with self.assertRaisesRegex(RuntimeError, "no longer the due head"):
                company.park_queue_item(
                    lower_id,
                    "Do not park a reviewed item after its queue position changes.",
                    require_due_head=True,
                )
            self.assertEqual(company.queue_items("queued")[1][0], lower_id)

            output = io.StringIO()
            reason = "Temporarily preserve this mission while focused work proceeds."
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "queue", "park",
                    queue_id, "--reason", reason,
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "parked")
            self.assertEqual(model.calls, 0)

            with self.assertRaisesRegex(ValueError, "queued item"):
                company.park_queue_item(queue_id, reason)
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "queue", "unpark",
                    queue_id, "--reason",
                    "Return this preserved mission to the executable queue.",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "queued")
            self.assertEqual(model.calls, 0)
            with self.assertRaisesRegex(ValueError, "parked item"):
                company.unpark_queue_item(
                    queue_id,
                    "A detailed reason that should fail because it is already queued.",
                )

    def test_queue_runs_highest_priority_due_playbook_and_passes_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            project_id = company.create_project("Queue Lab")
            low_id = company.enqueue("Low priority work", project_id, priority=10)
            future_id = company.enqueue(
                "Future urgent work", project_id, priority=100,
                scheduled_at="2999-01-01T00:00:00+00:00",
            )
            high_id = company.enqueue(
                "Improve daily operations", project_id, playbook="operations-improvement", priority=80
            )
            queue_id, job_id, report, passed = company.run_next_queue_item()
            self.assertEqual(queue_id, high_id)
            self.assertTrue(passed)
            self.assertTrue(report.exists())
            self.assertEqual({row[0] for row in company.queue_items("queued")}, {low_id, future_id})
            self.assertEqual(company.queue_items("complete")[0][7], job_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_count = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                quality_event_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='quality_evaluated'",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(history_count, 1)
            self.assertEqual(quality_event_count, 1)
            self.assertEqual(company.evaluate_job(job_id)["score"], 100)
            detail = company.job_detail(job_id)
            expected_roles = PLAYBOOKS["operations-improvement"]["roles"]
            self.assertEqual([row[1] for row in detail["assignments"]], expected_roles)

    def test_sensitive_queue_item_needs_approval_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            queue_id = company.enqueue("Send email to every prospect", priority=90)
            with self.assertRaises(PermissionError):
                company.run_next_queue_item()
            self.assertEqual(company.queue_items("needs_approval")[0][0], queue_id)
            self.assertEqual(len(company.jobs()), 0)
            self.assertEqual(len(company.action_requests("pending")), 1)

    def test_due_schedule_materializes_once_and_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            schedule_id = company.create_schedule(
                "Daily check", "Review local health", 1, "2020-01-01T00:00:00+00:00",
                playbook="operations-improvement", priority=70,
            )
            observed = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
            created = company.materialize_due_schedules(observed)
            self.assertEqual(created[0][0], schedule_id)
            self.assertEqual(len(company.queue_items("queued")), 1)
            self.assertEqual(company.materialize_due_schedules(observed), [])
            next_run = datetime.fromisoformat(company.schedules()[0][4])
            self.assertGreater(next_run, observed)
            company.set_schedule_enabled(schedule_id, False)
            self.assertEqual(company.schedules()[0][2], 0)

    def test_audit_export_has_matching_hash_and_excludes_source_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "notes.md"
            source.write_text("private local reference body", encoding="utf-8")
            company = Company(root / "state", MockModel())
            company.add_knowledge(source)
            company.run("Plan inventory", roles=["operations", "quality"])
            company.enqueue("Queued audit record")
            audit_path, hash_path, digest = company.export_audit(root / "exports")
            self.assertEqual(hashlib.sha256(audit_path.read_bytes()).hexdigest(), digest)
            self.assertIn(digest, hash_path.read_text(encoding="ascii"))
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "local-agent-company-audit-v3")
            self.assertRegex(payload["jobs"][0]["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(payload["evaluation_history"][0]["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(payload["evaluation_history"][0]["evaluator_version"])
            self.assertRegex(payload["evaluation_history"][0]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(payload["evidence_manifests"][0]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("run_token", payload["jobs"][0])
            self.assertNotIn("run_token", payload["queue"][0])
            self.assertNotIn("content", payload["knowledge_index"][0])
            self.assertNotIn("private local reference body", audit_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["project_knowledge_authority"], [])

    def test_health_snapshot_reports_local_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            health = company.health_snapshot()
            self.assertGreater(health["disk_free_bytes"], 0)
            self.assertIn("database_bytes", health)
            self.assertEqual(health["active_jobs"], 0)
            self.assertEqual(health["pending_report_finalizations"], 0)
            self.assertEqual(health["pending_evaluations"], 0)
            self.assertEqual(health["pending_completion"], [])

    def test_csv_dataset_profile_is_read_only_and_generates_project_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sales.csv"
            source.write_text(
                "id,value,note\n1,10,a\n2,11,\n3,oops,b\n3,oops,b\n", encoding="utf-8"
            )
            original = source.read_bytes()
            company = Company(root / "state", MockModel())
            company.create_project("Data Lab")
            dataset_id, brief, profile = company.profile_dataset(
                source, "Data Lab", key_columns=["id"]
            )
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(profile["schema"], "local-company.dataset-profile.v3")
            self.assertEqual(profile["contract_check"]["status"], "not_configured")
            self.assertEqual(profile["profiled_rows"], 4)
            self.assertEqual(profile["quality_flags"]["duplicate_rows"], 1)
            self.assertEqual(profile["quality_flags"]["duplicate_rows_affected"], 2)
            self.assertEqual(profile["quality_flags"]["duplicate_row_rate"], 0.5)
            self.assertIn("value", profile["quality_flags"]["mixed_type_columns"])
            self.assertEqual(profile["columns"]["note"]["missing"], 1)
            self.assertEqual(profile["columns"]["note"]["missing_rate"], 0.25)
            self.assertEqual(profile["columns"]["value"]["numeric"]["count"], 2)
            self.assertEqual(profile["columns"]["value"]["numeric"]["mean"], 10.5)
            self.assertEqual(profile["columns"]["value"]["numeric"]["median"], 10.5)
            self.assertEqual(
                profile["columns"]["value"]["numeric"]["rate_of_non_missing"], 0.5
            )
            self.assertEqual(profile["key_check"]["duplicate_rows"], 2)
            self.assertEqual(profile["key_check"]["uniqueness_rate"], 0.75)
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Mixed-type columns: value", brief_text)
            self.assertIn("Rows affected by duplicate keys: 2", brief_text)
            self.assertNotIn("oops", brief_text)
            self.assertEqual(company.dataset_items("Data Lab")[0][0], dataset_id)
            self.assertTrue(company.search_knowledge("duplicate mixed type", project="Data Lab"))

    def test_json_dataset_requires_objects_and_profiles_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "records.json"
            source.write_text('[{"active": true, "count": 1}, {"active": false, "count": 2}]', encoding="utf-8")
            company = Company(root / "state", MockModel())
            company.create_project("JSON Lab")
            dataset_id, _, profile = company.profile_dataset(source, "JSON Lab")
            self.assertEqual(profile["columns"]["active"]["types"], {"boolean": 2})
            self.assertEqual(profile["columns"]["count"]["numeric"]["mean"], 1.5)
            self.assertEqual(company.dataset_detail(dataset_id)["project"], "JSON Lab")

            invalid = root / "invalid.json"
            invalid.write_text('[{"count": NaN}]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite number"):
                company.profile_dataset(invalid, "JSON Lab")

    def test_dataset_numeric_profile_uses_iqr_and_explicit_composite_grain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "measures.csv"
            source.write_text(
                "region,id,amount\n"
                "north,1,1\n"
                "north,2,2\n"
                "north,3,3\n"
                "north,4,100\n"
                "north,  ,5\n",
                encoding="utf-8",
            )
            company = Company(root / "state", MockModel())
            company.create_project("Measure Lab")
            dataset_id, brief, profile = company.profile_dataset(
                source,
                "Measure Lab",
                allowed_root=root,
                key_columns=["region", "id"],
            )

            numeric = profile["columns"]["amount"]["numeric"]
            self.assertEqual(numeric["minimum"], 1)
            self.assertEqual(numeric["median"], 3)
            self.assertEqual(numeric["maximum"], 100)
            self.assertEqual(numeric["mean"], 22.2)
            self.assertEqual(numeric["iqr_outlier_count"], 1)
            self.assertEqual(numeric["iqr_outlier_rate"], 0.2)
            self.assertEqual(profile["key_check"]["missing_rows"], 1)
            self.assertEqual(profile["key_check"]["completeness_rate"], 0.8)
            self.assertEqual(profile["key_check"]["uniqueness_rate"], 1.0)
            overview = company.dataset_quality_items()[0]
            self.assertEqual(overview["id"], dataset_id)
            self.assertEqual(overview["outlier_columns"], 1)
            self.assertEqual(overview["quality_status"], "review")
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Declared key: region, id", brief_text)
            self.assertIn("Complete key rows: 4 (80.00%)", brief_text)
            self.assertNotIn("north", brief_text)

    def test_dataset_non_finite_text_is_flagged_and_never_emitted_as_json_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "non-finite.csv"
            source.write_text(
                "value\nNaN\nInfinity\n-Infinity\n1\n", encoding="utf-8"
            )
            company = Company(root / "state", MockModel())
            company.create_project("Finite Lab")
            _, brief, profile = company.profile_dataset(source, "Finite Lab")

            value_profile = profile["columns"]["value"]
            self.assertEqual(value_profile["non_finite_numeric"], 3)
            self.assertEqual(value_profile["numeric"]["count"], 1)
            self.assertEqual(
                profile["quality_flags"]["non_finite_numeric_columns"], ["value"]
            )
            json.dumps(profile, allow_nan=False)
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Non-finite numeric columns: value", brief_text)
            self.assertNotIn("Infinity", brief_text)

    def test_dataset_contract_checks_explicit_rules_without_storing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "contract.csv"
            source.write_text(
                "id,amount,note,label\n"
                "1,10,ok,raw-secret-alpha\n"
                "2,100,,raw-secret-beta\n"
                "3,oops,present,raw-secret-gamma\n",
                encoding="utf-8",
            )
            original = source.read_bytes()
            company = Company(root / "state", MockModel())
            company.create_project("Contract Lab")
            dataset_id, brief, profile = company.profile_dataset(
                source,
                "Contract Lab",
                required_columns=["id", "note", "absent_required"],
                allowed_type_rules=[
                    ("amount", "numeric"),
                    ("label", "string"),
                    ("absent_typed", "string"),
                ],
                numeric_minimum_rules=[("amount", "0"), ("absent_numeric", "0")],
                numeric_maximum_rules=[("amount", "50")],
            )

            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(profile["schema"], "local-company.dataset-profile.v3")
            contract = profile["contract_check"]
            self.assertEqual(contract["schema"], "local-company.dataset-contract.v1")
            self.assertEqual(contract["status"], "violations")
            self.assertTrue(contract["source_rows_complete"])
            self.assertEqual(contract["rule_count"], 8)
            self.assertEqual(contract["failed_rules"], 6)
            self.assertEqual(contract["required"][1]["missing_rows"], 1)
            self.assertEqual(contract["required"][1]["missing_rate"], 0.333333)
            self.assertFalse(contract["required"][2]["column_present"])
            self.assertEqual(contract["required"][2]["missing_rows"], 3)
            self.assertEqual(contract["types"][0]["checked_non_missing_rows"], 3)
            self.assertEqual(contract["types"][0]["unexpected_type_rows"], 1)
            self.assertEqual(contract["types"][0]["unexpected_type_rate"], 0.333333)
            self.assertFalse(contract["types"][2]["column_present"])
            amount_range = contract["numeric_ranges"][0]
            self.assertEqual(amount_range["checked_finite_rows"], 2)
            self.assertEqual(amount_range["uncheckable_non_missing_rows"], 1)
            self.assertEqual(amount_range["below_minimum_rows"], 0)
            self.assertEqual(amount_range["above_maximum_rows"], 1)
            self.assertEqual(amount_range["violation_rows"], 2)
            self.assertEqual(amount_range["violation_rate"], 0.666667)
            self.assertFalse(contract["numeric_ranges"][1]["column_present"])
            serialized_profile = json.dumps(profile, allow_nan=False)
            self.assertNotIn("raw-secret", serialized_profile)
            brief_text = brief.read_text(encoding="utf-8")
            self.assertIn("Declared contract: violations", brief_text)
            self.assertIn("Failed rules: 6", brief_text)
            self.assertIn("uncheckable_non_missing=1", brief_text)
            self.assertNotIn("raw-secret", brief_text)

            overview = next(
                item for item in company.dataset_quality_items() if item["id"] == dataset_id
            )
            self.assertEqual(overview["contract_status"], "violations")
            self.assertEqual(overview["quality_status"], "review")
            detail_payload = company.dataset_quality_detail(dataset_id)
            serialized_detail = json.dumps(detail_payload)
            self.assertNotIn(str(source), serialized_detail)
            self.assertNotIn("raw-secret", serialized_detail)
            detail_page = render_dataset_quality_detail(company, dataset_id)
            self.assertIn("Declared dataset contract", detail_page)
            self.assertIn("Required columns", detail_page)
            self.assertIn("Finite numeric ranges", detail_page)
            self.assertIn("violations", detail_page)
            self.assertNotIn(str(source), detail_page)
            self.assertNotIn("raw-secret", detail_page)

            clean_source = root / "clean.json"
            clean_source.write_text(
                '[{"id": 1, "amount": 10}, {"id": 2, "amount": 20}]',
                encoding="utf-8",
            )
            clean_id, _, clean_profile = company.profile_dataset(
                clean_source,
                "Contract Lab",
                required_columns=["id"],
                allowed_type_rules=[("amount", "numeric")],
                numeric_minimum_rules=[("amount", 0)],
                numeric_maximum_rules=[("amount", "20.0000000000004")],
            )
            self.assertEqual(clean_profile["contract_check"]["status"], "conforms")
            self.assertEqual(clean_profile["contract_check"]["failed_rules"], 0)
            self.assertEqual(
                clean_profile["contract_check"]["numeric_ranges"][0]["maximum"],
                20.0000000000004,
            )
            clean_overview = next(
                item for item in company.dataset_quality_items() if item["id"] == clean_id
            )
            self.assertEqual(clean_overview["contract_status"], "conforms")

            corrupted_profile = json.loads(json.dumps(profile))
            corrupted_profile["contract_check"]["required"][0]["column_present"] = False
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE datasets SET profile_json=? WHERE id=?",
                    (json.dumps(corrupted_profile), dataset_id),
                )
            corrupted = next(
                item for item in company.dataset_quality_items() if item["id"] == dataset_id
            )
            self.assertEqual(corrupted["profile_status"], "unavailable")

    def test_dataset_contract_labels_clean_truncated_data_as_profiled_rows_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "truncated.csv"
            source.write_text(
                "id\n" + "".join(f"{index}\n" for index in range(10_001)),
                encoding="utf-8",
            )
            company = Company(root / "state", MockModel())
            company.create_project("Truncated Contract Lab")

            dataset_id, _, profile = company.profile_dataset(
                source,
                "Truncated Contract Lab",
                required_columns=["id"],
                allowed_type_rules=[("id", "integer")],
                numeric_minimum_rules=[("id", 0)],
            )

            contract = profile["contract_check"]
            self.assertEqual(profile["profiled_rows"], 10_000)
            self.assertTrue(profile["quality_flags"]["truncated"])
            self.assertFalse(contract["source_rows_complete"])
            self.assertEqual(contract["status"], "conforms_profiled_rows")
            self.assertEqual(contract["failed_rules"], 0)
            overview = next(
                item for item in company.dataset_quality_items() if item["id"] == dataset_id
            )
            self.assertEqual(overview["contract_status"], "conforms profiled rows")

    def test_dataset_contract_declarations_fail_closed_before_source_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "rules.csv"
            source.write_text("id,amount\n1,5\n", encoding="utf-8")
            original = source.read_bytes()
            company = Company(root / "state", MockModel())
            company.create_project("Rules Lab")
            cases = (
                ({"required_columns": ["id", "id"]}, "required-column declarations"),
                ({"allowed_type_rules": [("id", "mystery")]}, "type must be one of"),
                ({
                    "allowed_type_rules": [("id", "numeric"), ("id", "numeric")],
                }, "type declarations must be unique"),
                ({"numeric_minimum_rules": [("amount", "NaN")]}, "finite numbers"),
                ({
                    "numeric_minimum_rules": [("amount", 10)],
                    "numeric_maximum_rules": [("amount", 1)],
                }, "minimum exceeds maximum"),
                ({"allowed_type_rules": [("id",)]}, "require COLUMN and VALUE"),
                ({
                    "required_columns": [f"column-{index}" for index in range(65)],
                }, "at most 64 columns"),
                ({
                    "required_columns": [f"column-{index}" for index in range(257)],
                }, "at most 256 declarations"),
            )
            for kwargs, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        company.profile_dataset(source, "Rules Lab", **kwargs)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(company.dataset_items(), [])

    def test_dataset_contract_cli_flags_are_repeatable_and_persist_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            source = root / "cli-contract.csv"
            source.write_text("id,amount,note\n1,10,ok\n2,20,\n", encoding="utf-8")
            original = source.read_bytes()
            Company(state, MockModel()).create_project("CLI Contract Lab")
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(state), "datasets", "add",
                    str(source), "--project", "CLI Contract Lab",
                    "--required", "id", "--required", "note",
                    "--type", "amount", "numeric",
                    "--min", "amount", "0", "--max", "amount", "15",
                ],
            ), patch("sys.stdout", output):
                exit_code = cli_main()
            self.assertEqual(exit_code, 0)
            self.assertIn("Contract: violations, failed_rules=2", output.getvalue())
            self.assertIn("Source was read-only", output.getvalue())
            self.assertEqual(source.read_bytes(), original)
            company = Company(state, MockModel())
            dataset_id = company.dataset_items()[0][0]
            contract = company.dataset_detail(dataset_id)["profile"]["contract_check"]
            self.assertEqual(contract["rule_count"], 4)
            self.assertEqual(contract["failed_rules"], 2)
            self.assertEqual(contract["required"][1]["missing_rows"], 1)
            self.assertEqual(contract["numeric_ranges"][0]["above_maximum_rows"], 1)

    def test_quality_rejects_model_length_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), TruncatedModel())
            job_id, _ = company.run("Plan inventory", roles=["operations", "quality"])
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["passed"])
            self.assertFalse(evaluation["checks"]["model_stopped_cleanly"])

    def test_quality_fails_conservatively_on_malformed_incomplete_metric_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run("Plan inventory", roles=["operations"])
            with closing(sqlite3.connect(company.db_path)) as db, db:
                company._event(
                    db, job_id, "specialist_draft_isolated",
                    json.dumps({"role": "operations", "status": []}),
                )
                company._event(
                    db, job_id, "model_metrics",
                    json.dumps({"stage": [], "done_reason": "length"}),
                )
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["passed"])
            self.assertFalse(evaluation["checks"]["model_stopped_cleanly"])

    def test_nonstrict_ollama_generation_keeps_the_configured_budget_path(self):
        class RoutingOllamaModel(OfflineOllamaModel):
            def __init__(self):
                super().__init__("llama3.2:1b", num_predict=2048)
                self.complete_calls = 0
                self.bounded_calls = 0

            def complete(self, system, prompt):
                self.complete_calls += 1
                self.last_metrics = {
                    "done": True, "done_reason": "stop", "num_predict": self.num_predict,
                }
                return MockModel().complete(system, prompt)

            def complete_bounded(self, system, prompt, *, num_predict):
                self.bounded_calls += 1
                return super().complete_bounded(
                    system, prompt, num_predict=num_predict,
                )

        with tempfile.TemporaryDirectory() as tmp:
            model = RoutingOllamaModel()
            company = Company(Path(tmp), model)
            company.run("Plan local inventory", roles=["operations"])
            self.assertEqual(model.complete_calls, 2)
            self.assertEqual(model.bounded_calls, 0)
            self.assertNotIn(
                "specialist_generation_policy",
                {event[0] for event in company.job_detail(company.jobs()[0][0])["events"]},
            )

    def test_strict_bounded_transport_failure_records_policy_without_result_proof(self):
        class FailingBoundedOllama(OfflineOllamaModel):
            def __init__(self):
                super().__init__("llama3.2:1b", num_predict=256)
                self.bounded_caps = []

            def complete(self, system, prompt):
                raise AssertionError("strict Ollama specialist must use bounded completion")

            def complete_bounded(self, system, prompt, *, num_predict):
                self.bounded_caps.append(num_predict)
                raise RuntimeError("bounded transport failure")

        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Every verified "
            "claim must name its exact source filename and matching supplied evidence ID."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Hosted activation remains pending owner review.", encoding="utf-8",
            )
            model = FailingBoundedOllama()
            company = Company(root / "state", model)
            project = company.create_project("Bounded Transport Failure")
            company.add_knowledge(source, project)
            with self.assertRaisesRegex(RuntimeError, "bounded transport failure"):
                company.run(objective, roles=["operations"], project=project)
            self.assertEqual(model.bounded_caps, [256])
            job_id = company.jobs()[0][0]
            detail = company.job_detail(job_id)
            self.assertEqual(detail["job"][2], "failed")
            self.assertEqual(detail["assignments"][0][2], "failed")
            kinds = [event[0] for event in detail["events"]]
            self.assertIn("specialist_generation_policy", kinds)
            self.assertNotIn("specialist_draft_isolated", kinds)
            self.assertNotIn("model_metrics", kinds)
            self.assertFalse(detail["report"])
            policy = next(
                json.loads(event[1]) for event in detail["events"]
                if event[0] == "specialist_generation_policy"
            )
            self.assertEqual(policy["configured_num_predict"], 256)
            self.assertEqual(policy["effective_num_predict"], 256)

    def test_strict_quality_withholds_and_excludes_incomplete_specialist_draft(self):
        class IncompleteSpecialistModel(OfflineOllamaModel):
            def __init__(self):
                super().__init__("llama3.2:1b", num_predict=2048)
                self.bounded_caps = []
                self.specialist_systems = []
                self.specialist_prompts = []
                self.structured_prompts = []

            def complete(self, system, prompt):
                raise AssertionError("strict Ollama specialist must use bounded completion")

            def complete_bounded(self, system, prompt, *, num_predict):
                self.bounded_caps.append(num_predict)
                self.specialist_systems.append(system)
                self.specialist_prompts.append(prompt)
                self.last_metrics = {
                    "done": True, "done_reason": "length", "output_tokens": num_predict,
                    "num_predict": num_predict,
                }
                return "UNTRUSTED_RAW_PARTIAL must never enter trusted synthesis " * 20

            def complete_structured(self, system, prompt, schema):
                self.structured_prompts.append(prompt)
                self.last_metrics = {
                    "done": True, "done_reason": "stop", "output_tokens": 60,
                    "num_predict": self.num_predict,
                }
                return {
                    "task_templates": [
                        "Capture the objective frozen inputs and local owner gate",
                        "Perform bounded analysis and preserve the local output",
                        "Review evidence limitations checks and owner decisions",
                    ],
                }

        objective = (
            "Using imported alpha.md, prepare a plan for the next 7-day period and separate "
            "verified facts from assumptions. Define three "
            "reusable task templates, a daily review cadence, success checks, failure modes, and "
            "owner gates. Each specialist must use at most 90 words. Executive synthesis "
            "at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Hosted activation remains pending owner review.", encoding="utf-8",
            )
            model = IncompleteSpecialistModel()
            company = Company(root / "state", model)
            project = company.create_project("Incomplete Isolation")
            company.add_knowledge(source, project)
            job_id, report_path = company.run(
                objective, roles=["operations"], project=project,
            )
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            self.assertFalse(evaluation["checks"]["specialist_advisories_complete"])
            self.assertTrue(evaluation["checks"]["evidence_filename_pairs_valid"])
            self.assertTrue(evaluation["checks"]["model_stopped_cleanly"])
            self.assertEqual(evaluation["incomplete_specialist_roles"], ["operations"])
            self.assertEqual(model.bounded_caps, [768])
            self.assertNotIn("Relevant local sources:", model.specialist_prompts[0])
            self.assertNotIn("[EVIDENCE:", model.specialist_prompts[0])
            self.assertNotIn(
                "Hosted activation remains pending owner review.",
                model.specialist_prompts[0],
            )
            self.assertNotIn("UNTRUSTED_RAW_PARTIAL", model.structured_prompts[0])
            self.assertNotIn(
                "UNTRUSTED_RAW_PARTIAL", report_path.read_text(encoding="utf-8"),
            )
            with closing(sqlite3.connect(company.db_path)) as db:
                result = db.execute(
                    "SELECT result FROM assignments WHERE job_id=?", (job_id,),
                ).fetchone()[0]
            self.assertIn("withheld after incomplete model output", result)
            isolated = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "specialist_draft_isolated"
            ]
            self.assertEqual(isolated[0]["status"], "incomplete_withheld")
            metrics = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "model_metrics"
            ]
            operations_metrics = next(
                metric for metric in metrics if metric.get("stage") == "operations"
            )
            self.assertEqual(operations_metrics["num_predict"], 768)
            self.assertEqual(operations_metrics["done_reason"], "length")
            policy_events = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "specialist_generation_policy"
            ]
            self.assertEqual(policy_events, [{
                "configured_num_predict": 2048,
                "effective_num_predict": 768,
                "policy": "strict-bounded-v2",
                "role": "operations",
                "source_context_included": False,
            }])
            specialist_system = model.specialist_systems[0]
            self.assertIn("Proposed next action", specialist_system)
            self.assertIn("Missing proof", specialist_system)
            self.assertIn(
                "Return exactly one plain-text line and nothing else", specialist_system,
            )
            self.assertIn(
                "Not verified or performed: Proposed next action: review "
                "[one bounded local gap]. Assumption: [one unverified premise]. "
                "Missing proof: [one named proof item].",
                specialist_system,
            )
            self.assertIn(
                "Missing proof must name a concrete unresolved proof", specialist_system,
            )
            self.assertIn(
                "Never include Owner review required in the specialist line", specialist_system,
            )
            self.assertNotIn("must carry a supplied [EVIDENCE:id]", specialist_system)
            projected = company.job_detail(job_id)["evaluation"]
            self.assertEqual(projected["incomplete_specialist_roles"], ["operations"])
            mission_page = render_mission_detail(company, job_id)
            self.assertIn("Degraded specialist output safely withheld", mission_page)
            self.assertIn("operations", mission_page)

    def test_strict_resume_rebinds_legacy_incomplete_specialist_draft(self):
        class LegacyIncompleteModel(OfflineOllamaModel):
            def __init__(self):
                super().__init__("llama3.2:1b", num_predict=2048)
                self.fail_structured = True
                self.bounded_calls = 0

            def complete(self, system, prompt):
                raise AssertionError("strict Ollama specialist must use bounded completion")

            def complete_bounded(self, system, prompt, *, num_predict):
                self.bounded_calls += 1
                self.last_metrics = {
                    "done": True, "done_reason": "length", "output_tokens": num_predict,
                    "num_predict": num_predict,
                }
                return "Legacy partial claim that must be withheld on recovery."

            def complete_structured(self, system, prompt, schema):
                self.last_metrics = {
                    "done": True, "done_reason": "stop", "output_tokens": 60,
                }
                if self.fail_structured:
                    raise RuntimeError("legacy structured interruption")
                return {
                    "task_templates": [
                        "Capture the objective frozen inputs and local owner gate",
                        "Perform bounded analysis and preserve the local output",
                        "Review evidence limitations checks and owner decisions",
                    ],
                }

        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Define three "
            "reusable task templates, a daily review cadence, success checks, failure modes, and "
            "owner gates. Every verified claim must name its exact source filename and matching "
            "supplied evidence ID. Each specialist must use at most 90 words. Executive synthesis "
            "at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Hosted activation remains pending owner review.", encoding="utf-8",
            )
            model = LegacyIncompleteModel()
            company = Company(root / "state", model)
            project = company.create_project("Legacy Incomplete Recovery")
            company.add_knowledge(source, project)
            with self.assertRaisesRegex(RuntimeError, "failed closed"):
                company.run(objective, roles=["operations"], project=project)
            job_id = company.jobs()[0][0]
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE assignments SET result=? WHERE job_id=? AND role='operations'",
                    ("Legacy partial claim that must not survive resume.", job_id),
                )
                db.execute(
                    "DELETE FROM events WHERE job_id=? AND kind='specialist_draft_isolated'",
                    (job_id,),
                )
            model.fail_structured = False
            resumed_id, _ = company.resume(job_id)
            self.assertEqual(resumed_id, job_id)
            self.assertEqual(model.bounded_calls, 1)
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            self.assertTrue(evaluation["checks"]["model_stopped_cleanly"])
            self.assertFalse(evaluation["checks"]["specialist_advisories_complete"])
            self.assertIn(
                "regenerate_one_complete_bounded_specialist_advisory_before_retry",
                company.quality_recovery_summary(job_id)["repair_actions"],
            )
            with closing(sqlite3.connect(company.db_path)) as db:
                result = db.execute(
                    "SELECT result FROM assignments WHERE job_id=? AND role='operations'",
                    (job_id,),
                ).fetchone()[0]
            self.assertEqual(
                result,
                "Not verified or performed: specialist draft withheld after incomplete model "
                "output.",
            )
            isolated = [
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "specialist_draft_isolated"
            ]
            self.assertEqual(
                isolated[-1], {"role": "operations", "status": "incomplete_withheld"},
            )

    def test_quality_enforces_explicit_objective_constraints_and_claim_safety(self):
        objective = (
            "Define three task templates, a daily review cadence, success checks, failure modes, "
            "and owner gates. Separate verified facts from assumptions. Each specialist must use "
            "at most 100 words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(False))
            queue_id = company.enqueue(objective, playbook="operations-improvement")
            observed_queue, job_id, _, passed = company.run_next_queue_item()
            self.assertEqual(observed_queue, queue_id)
            self.assertFalse(passed)
            evaluation = company.evaluate_job(job_id)
            self.assertTrue(evaluation["checks"]["specialists_within_word_limit"])
            self.assertFalse(evaluation["checks"]["facts_assumptions_separated"])
            self.assertFalse(evaluation["checks"]["requested_concepts_present"])
            self.assertFalse(evaluation["checks"]["unperformed_action_claims_absent"])
            self.assertFalse(evaluation["checks"]["numeric_claims_labeled"])
            self.assertFalse(evaluation["checks"]["placeholder_artifacts_absent"])
            self.assertEqual(company.queue_items("quality_failed")[0][0], queue_id)
            detail = company.job_detail(job_id)
            self.assertTrue(any(
                event[0] == "objective_constraint_applied" and "word limit" in event[1]
                for event in detail["events"]
            ))

        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(True))
            queue_id = company.enqueue(objective, playbook="operations-improvement")
            _, _, _, passed = company.run_next_queue_item()
            self.assertTrue(passed)
            evaluation = company.recent_evaluations()[0]
            self.assertEqual(evaluation[0], company.queue_items("complete")[0][7])
            self.assertEqual(evaluation[1:3], (1, 100))

    def test_quality_summary_is_bounded_pathless_and_read_only(self):
        objective = (
            "Define three task templates, a daily review cadence, success checks, failure modes, "
            "and owner gates. Separate verified facts from assumptions. Each specialist must use "
            "at most 100 words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", ConstraintModel(False))
            queue_id = company.enqueue(objective, playbook="operations-improvement")
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertFalse(passed)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
            database_before = company.db_path.read_bytes()

            summary = company.quality_recovery_summary(job_id)
            self.assertEqual(summary["schema"], QUALITY_RECOVERY_SCHEMA)
            self.assertEqual(summary["quality_status"], "failed")
            self.assertEqual(summary["queue_id"], queue_id)
            self.assertEqual(summary["queue_status"], "quality_failed")
            self.assertEqual(summary["failed_checks"], sorted(summary["failed_checks"]))
            self.assertIn("facts_assumptions_separated", summary["failed_checks"])
            self.assertIn(
                "make_requested_sections_counts_labels_and_ending_explicit",
                summary["repair_actions"],
            )
            self.assertIn(
                "label_assumptions_and_remove_unperformed_or_placeholder_claims",
                summary["repair_actions"],
            )
            self.assertEqual(summary["next_action"], "review_then_queue_revised_mission")
            self.assertTrue(all(value is False for value in summary["effects"].values()))

            cli_model = CountingMockModel()
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "quality", job_id,
                    "--summary",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            rendered = output.getvalue()
            cli_summary = json.loads(rendered)
            self.assertEqual(cli_summary, summary)
            self.assertEqual(cli_model.calls, 0)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(objective, rendered)
            self.assertLess(len(rendered.encode("utf-8")), 4096)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
            self.assertEqual(history_after, history_before)
            self.assertEqual(company.db_path.read_bytes(), database_before)

    def test_quality_summary_handles_unevaluated_and_malformed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run("Review local inventory", roles=["quality"])
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute("DELETE FROM evaluations WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM evaluation_history WHERE job_id=?", (job_id,))
            summary = company.quality_recovery_summary(job_id)
            self.assertEqual(summary["quality_status"], "not_evaluated")
            self.assertEqual(summary["next_action"], "run_quality_evaluation")
            self.assertEqual(summary["failed_checks"], [])
            with self.assertRaisesRegex(ValueError, "Invalid job ID"):
                company.quality_recovery_summary("../unsafe")

            company.evaluate_job(job_id)
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE evaluations SET checks_json='[]' WHERE job_id=?", (job_id,),
                )
            with self.assertRaisesRegex(ValueError, "Stored quality checks are malformed"):
                company.quality_recovery_summary(job_id)

    def test_quality_failure_overview_is_ordered_aggregated_and_read_only(self):
        objective = (
            "Define three task templates, a daily review cadence, success checks, failure modes, "
            "and owner gates. Separate verified facts from assumptions. Each specialist must use "
            "at most 100 words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", ConstraintModel(False))
            low_queue = company.enqueue(
                "For the local alpha review, " + objective, playbook="operations-improvement",
                priority=20,
            )
            high_queue = company.enqueue(
                "For the local beta review, " + objective, playbook="operations-improvement",
                priority=90,
            )
            observed_high, high_job, _, high_passed = company.run_next_queue_item()
            observed_low, low_job, _, low_passed = company.run_next_queue_item()
            self.assertEqual((observed_high, observed_low), (high_queue, low_queue))
            self.assertFalse(high_passed)
            self.assertFalse(low_passed)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                low_checks = json.loads(db.execute(
                    "SELECT checks_json FROM evaluations WHERE job_id=?", (low_job,),
                ).fetchone()[0])
                self.assertFalse(low_checks["facts_assumptions_separated"])
                low_checks["facts_assumptions_separated"] = True
                low_score = round(sum(low_checks.values()) * 100 / len(low_checks))
                encoded = json.dumps(low_checks, sort_keys=True)
                db.execute(
                    "UPDATE evaluations SET score=?, checks_json=? WHERE job_id=?",
                    (low_score, encoded, low_job),
                )
                db.execute(
                    "UPDATE evaluation_history SET score=?, checks_json=?, "
                    "evaluator_version='local-quality-2026-07-01.1' WHERE id=("
                    "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                    (low_score, encoded, low_job),
                )
                db.execute(
                    "UPDATE jobs SET objective=? WHERE id=?",
                    (
                        "Using imported evidence, prepare a 7-day plan and separate "
                        "verified facts from assumptions.",
                        high_job,
                    ),
                )
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history"
                ).fetchone()[0]
            database_before = company.db_path.read_bytes()

            overview = company.quality_failure_summaries()
            self.assertEqual(overview["schema"], QUALITY_RECOVERY_LIST_SCHEMA)
            self.assertEqual(overview["quality_failed_count"], 2)
            self.assertEqual(
                [item["queue_id"] for item in overview["items"]],
                [high_queue, low_queue],
            )
            self.assertEqual([item["priority"] for item in overview["items"]], [90, 20])
            self.assertEqual(overview["current_failed_count"], 2)
            self.assertEqual(overview["current_passed_count"], 0)
            self.assertEqual(overview["current_preview_changed_count"], 2)
            self.assertEqual(overview["strict_retry_policy_count"], 1)
            stored_counts = {
                item["check"]: item["count"]
                for item in overview["common_stored_failed_checks"]
            }
            current_counts = {
                item["check"]: item["count"]
                for item in overview["common_current_failed_checks"]
            }
            action_counts = {
                item["action"]: item["count"]
                for item in overview["common_current_repair_actions"]
            }
            self.assertEqual(stored_counts["facts_assumptions_separated"], 1)
            self.assertEqual(current_counts["facts_assumptions_separated"], 2)
            self.assertEqual(
                action_counts[
                    "label_assumptions_and_remove_unperformed_or_placeholder_claims"
                ],
                2,
            )
            self.assertEqual(
                overview["next_action"],
                "repair_highest_priority_current_failed_checks",
            )
            low_item = next(item for item in overview["items"] if item["job_id"] == low_job)
            self.assertEqual(
                low_item["stored_result"]["evaluator_version"],
                "local-quality-2026-07-01.1",
            )
            self.assertEqual(
                low_item["current_preview"]["evaluator_version"], EVALUATOR_VERSION,
            )
            self.assertIn(
                "facts_assumptions_separated",
                low_item["comparison"]["new_failed_checks"],
            )
            self.assertTrue(low_item["comparison"]["evaluator_changed"])
            self.assertEqual(low_item["retry_policy"], "standard")
            self.assertEqual(high_job, overview["items"][0]["job_id"])
            self.assertEqual(overview["items"][0]["retry_policy"], "strict_grounded")
            self.assertTrue(all(value is False for value in overview["effects"].values()))

            cli_model = CountingMockModel()
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "quality", "--failed",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)
            rendered = output.getvalue()
            self.assertEqual(json.loads(rendered), overview)
            self.assertEqual(cli_model.calls, 0)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("local alpha review", rendered)
            self.assertNotIn("local beta review", rendered)
            self.assertNotIn("objective", rendered)
            self.assertNotIn("output_path", rendered)
            self.assertLess(len(rendered.encode("utf-8")), 16_384)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history"
                ).fetchone()[0]
            self.assertEqual(history_after, history_before)
            self.assertEqual(company.db_path.read_bytes(), database_before)

    def test_quality_failure_overview_is_empty_bounded_and_race_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            empty = company.quality_failure_summaries()
            self.assertEqual(empty["quality_failed_count"], 0)
            self.assertEqual(empty["current_failed_count"], 0)
            self.assertEqual(empty["current_passed_count"], 0)
            self.assertEqual(empty["strict_retry_policy_count"], 0)
            self.assertEqual(empty["items"], [])
            self.assertEqual(empty["next_action"], "none")

            snapshot = company._quality_failed_queue_snapshot()
            changed = dict(snapshot)
            changed["database_sha256"] = "0" * 64
            with patch.object(
                company, "_quality_failed_queue_snapshot",
                side_effect=[snapshot, changed],
            ), self.assertRaisesRegex(RuntimeError, "changed during observation"):
                company.quality_failure_summaries()

            row = (
                "a" * 12, "b" * 12, 50,
                "2026-07-28T00:00:00+00:00", 1, "Bounded objective",
            )
            overflow = {"database_sha256": "1" * 64, "rows": (row,) * 101}
            with patch.object(
                company, "_quality_failed_queue_snapshot", return_value=overflow,
            ), self.assertRaisesRegex(ValueError, "More than 100 quality failures"):
                company.quality_failure_summaries()

            cli_model = CountingMockModel()
            error = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "quality", "--failed",
                    "--summary",
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=cli_model,
            ), patch("sys.stderr", error):
                self.assertEqual(cli_main(), 2)
            self.assertIn("--failed cannot be combined", error.getvalue())
            self.assertEqual(cli_model.calls, 0)

    def test_quality_failure_overview_surfaces_current_pass_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", MockModel())
            queue_id = company.enqueue(
                "Private historical failure for current aggregate verification",
                roles=["quality"], priority=77,
            )
            observed, job_id, _, passed = company.run_next_queue_item()
            self.assertEqual(observed, queue_id)
            self.assertTrue(passed)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                checks = json.loads(db.execute(
                    "SELECT checks_json FROM evaluations WHERE job_id=?", (job_id,),
                ).fetchone()[0])
                self.assertTrue(checks["placeholder_artifacts_absent"])
                checks["placeholder_artifacts_absent"] = False
                score = round(sum(checks.values()) * 100 / len(checks))
                encoded = json.dumps(checks, sort_keys=True)
                db.execute(
                    "UPDATE evaluations SET passed=0, score=?, checks_json=? "
                    "WHERE job_id=?", (score, encoded, job_id),
                )
                db.execute(
                    "UPDATE evaluation_history SET passed=0, score=?, checks_json=?, "
                    "evaluator_version='local-quality-2026-07-01.1' WHERE id=("
                    "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                    (score, encoded, job_id),
                )
                db.execute(
                    "UPDATE mission_queue SET status='quality_failed' WHERE id=?",
                    (queue_id,),
                )
            company.model = CountingMockModel()
            database_before = company.db_path.read_bytes()

            overview = company.quality_failure_summaries()

            self.assertEqual(overview["quality_failed_count"], 1)
            self.assertEqual(overview["current_failed_count"], 0)
            self.assertEqual(overview["current_passed_count"], 1)
            self.assertEqual(overview["current_preview_changed_count"], 1)
            self.assertEqual(
                overview["next_action"], "review_current_passes_before_queue_change",
            )
            item = overview["items"][0]
            self.assertEqual(item["stored_result"]["quality_status"], "failed")
            self.assertEqual(item["current_preview"]["quality_status"], "passed")
            self.assertEqual(item["current_preview"]["failed_checks"], [])
            self.assertEqual(item["current_preview"]["repair_actions"], [])
            self.assertTrue(item["comparison"]["outcome_changed"])
            self.assertEqual(
                item["comparison"]["resolved_failed_checks"],
                ["placeholder_artifacts_absent"],
            )
            self.assertEqual(item["next_action"], "review_then_run_quality_evaluation")
            self.assertEqual(
                company.quality_recovery_summary(job_id)["queue_status"],
                "quality_failed",
            )
            malformed_preview = company.quality_recheck_preview(job_id)
            malformed_preview["effects"] = {"model_called": True}
            with patch.object(
                company, "quality_recheck_preview", return_value=malformed_preview,
            ), self.assertRaisesRegex(
                ValueError, "Current quality recovery preview is malformed",
            ):
                company.quality_failure_summaries()
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.model.calls, 0)

    def test_quality_failure_dashboard_is_pathless_read_only_and_http_exact(self):
        objective = (
            "Private dashboard objective <script>never-render</script>: define three task "
            "templates, a daily review cadence, success checks, failure modes, and owner gates. "
            "Separate verified facts from assumptions. Each specialist must use at most 100 "
            "words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", ConstraintModel(False))
            queue_id = company.enqueue(
                objective + f" Local private root: {root}",
                playbook="operations-improvement", priority=88,
            )
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertFalse(passed)
            no_model = CountingMockModel()
            company.model = no_model
            with closing(sqlite3.connect(company.db_path)) as db:
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history"
                ).fetchone()[0]
            database_before = company.db_path.read_bytes()

            page = render_quality_failure_overview(company)
            self.assertIn("Quality failure recovery", page)
            self.assertIn(QUALITY_RECOVERY_LIST_SCHEMA, page)
            self.assertIn("Stored result", page)
            self.assertIn("Current common failed gates", page)
            self.assertIn("Retry policy", page)
            self.assertIn("strictly grounded on retry", page)
            self.assertIn("standard", page)
            self.assertIn(queue_id, page)
            self.assertIn(f'href="/missions/{job_id}"', page)
            self.assertIn("facts_assumptions_separated", page)
            self.assertIn(
                "label_assumptions_and_remove_unperformed_or_placeholder_claims", page,
            )
            self.assertNotIn("never-render", page)
            self.assertNotIn(str(root), page)
            self.assertNotIn("Private dashboard objective", page)
            self.assertLess(len(page.encode("utf-8")), 32_768)
            main_page = render_dashboard(company)
            self.assertIn('href="/quality-failures"', main_page)
            self.assertIn("Failed mission recovery", main_page)
            malformed = company.quality_failure_summaries()
            malformed["items"] = [None]
            malformed["quality_failed_count"] = 1
            with patch.object(
                company, "quality_failure_summaries", return_value=malformed,
            ), self.assertRaisesRegex(ValueError, "overview is malformed"):
                render_quality_failure_overview(company)

            server = create_dashboard_server(company, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with opener.open(base + "/quality-failures", timeout=3) as response:
                    http_page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(http_page, page)
                self.assertNotIn("never-render", http_page)
                self.assertNotIn(str(root), http_page)

                with self.assertRaises(urllib.error.HTTPError) as missing:
                    opener.open(base + "/quality-failures/extra", timeout=3)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()

                with patch.object(
                    company, "quality_failure_summaries",
                    side_effect=RuntimeError("private-race-detail"),
                ), self.assertRaises(urllib.error.HTTPError) as unstable:
                    opener.open(base + "/quality-failures", timeout=3)
                self.assertEqual(unstable.exception.code, 409)
                unstable_body = unstable.exception.read().decode("utf-8")
                unstable.exception.close()
                self.assertIn("retry after local state is stable", unstable_body)
                self.assertNotIn("private-race-detail", unstable_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history"
                ).fetchone()[0]
            self.assertEqual(history_after, history_before)
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(no_model.calls, 0)

    def test_obsolete_quality_failure_can_be_superseded_without_losing_evidence(self):
        objective = (
            "Define three task templates, a daily review cadence, success checks, failure modes, "
            "and owner gates. Separate verified facts from assumptions. Each specialist must use "
            "at most 100 words. The executive synthesis must use at most 180 words and end with: "
            "Owner review required."
        )
        reason = (
            "Obsolete after current SuperMega evidence refresh; preserve the historical "
            "evaluation for audit."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            company = Company(root / "state", ConstraintModel(False))
            project_id = company.create_project("SuperMega")
            queue_id = company.enqueue(
                objective, project=project_id,
                playbook="operations-improvement", priority=90,
            )
            _, job_id, report, passed = company.run_next_queue_item()
            self.assertFalse(passed)
            report_before = report.read_bytes()
            with closing(sqlite3.connect(company.db_path)) as db:
                history_before = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]

            company.model = ConstraintModel(True)
            successor_job_id, _ = company.retry(job_id)

            no_model = CountingMockModel()
            output = io.StringIO()
            with patch(
                "sys.argv", [
                    "local-company", "--home", str(company.home), "queue",
                    "supersede", queue_id, "--successor-job", successor_job_id,
                    "--reason", reason,
                ],
            ), patch(
                "local_company.cli.selected_model", return_value=no_model,
            ), patch("sys.stdout", output):
                self.assertEqual(cli_main(), 0)

            result = json.loads(output.getvalue())
            self.assertEqual(result["schema"], QUEUE_SUPERSEDE_SCHEMA)
            self.assertEqual(result["queue_id"], queue_id)
            self.assertEqual(result["job_id"], job_id)
            self.assertEqual(result["project_id"], project_id)
            self.assertEqual(result["previous_status"], "quality_failed")
            self.assertEqual(result["status"], "superseded")
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["successor_job_id"], successor_job_id)
            self.assertRegex(result["proof_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(result["effects"]["database_mutated"])
            self.assertTrue(result["effects"]["queue_changed"])
            self.assertTrue(all(
                result["effects"][key] is False for key in (
                    "model_called", "work_started", "report_deleted",
                    "evaluation_deleted", "queue_history_deleted",
                )
            ))
            self.assertEqual(no_model.calls, 0)
            superseded = company.queue_items("superseded")[0]
            self.assertEqual(superseded[0], queue_id)
            self.assertEqual(superseded[7], job_id)
            self.assertEqual(report.read_bytes(), report_before)
            with closing(sqlite3.connect(company.db_path)) as db:
                history_after = db.execute(
                    "SELECT COUNT(*) FROM evaluation_history WHERE job_id=?", (job_id,),
                ).fetchone()[0]
                event = db.execute(
                    "SELECT detail FROM events WHERE job_id=? "
                    "AND kind='queue_quality_failure_superseded'", (job_id,),
                ).fetchone()
            self.assertEqual(history_after, history_before)
            self.assertIsNotNone(event)
            self.assertEqual(json.loads(event[0])["reason"], reason)
            self.assertEqual(
                json.loads(event[0])["successor_job_id"], successor_job_id,
            )
            self.assertEqual(company.quality_failure_summaries()["quality_failed_count"], 0)
            brief = company.operator_brief(project_id)
            self.assertEqual(brief["counts"]["quality_failed_missions"], 0)
            self.assertNotIn("quality_failed_missions", {
                item["code"] for item in brief["attention"]
            })
            dashboard = render_dashboard(company)
            self.assertIn("No active queue items", dashboard)
            self.assertNotIn(queue_id, dashboard)
            self.assertNotIn('href="/quality-failures"', dashboard)

            company.reset_queue_item(queue_id)
            reset = company.queue_items("queued")[0]
            self.assertEqual(reset[0], queue_id)
            self.assertEqual(reset[7], "")
            self.assertEqual(report.read_bytes(), report_before)

    def test_quality_failure_supersede_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), CountingMockModel())
            queue_id = company.enqueue("Review a bounded local operating change")
            database_before = company.db_path.read_bytes()

            successor_job_id = "a" * 12
            with self.assertRaisesRegex(ValueError, "Stored quality-failed queue link"):
                company.supersede_quality_failure(
                    queue_id,
                    "This queued mission is not eligible for superseding.",
                    successor_job_id,
                )
            with self.assertRaisesRegex(ValueError, "12 lowercase hexadecimal"):
                company.supersede_quality_failure(
                    "../unsafe",
                    "This malformed identifier must not change queue state.",
                    successor_job_id,
                )
            with self.assertRaisesRegex(ValueError, "20 to 240"):
                company.supersede_quality_failure(
                    queue_id, "too short", successor_job_id,
                )
            with self.assertRaisesRegex(ValueError, "control characters"):
                company.supersede_quality_failure(
                    queue_id,
                    "This reason contains a forbidden null character.\x00",
                    successor_job_id,
                )
            with self.assertRaisesRegex(ValueError, "Successor job ID"):
                company.supersede_quality_failure(
                    queue_id,
                    "This successor identifier must fail before state changes.",
                    "../unsafe",
                )
            self.assertEqual(company.db_path.read_bytes(), database_before)
            self.assertEqual(company.queue_items("queued")[0][0], queue_id)
            self.assertEqual(company.model.calls, 0)

    def test_bounded_editor_repairs_structure_and_audits_word_caps(self):
        objective = (
            "Define task templates, a daily review cadence, success checks, failure modes, and owner gates. "
            "Separate verified facts from assumptions. Each specialist must use at most 100 words. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), RevisionModel())
            queue_id = company.enqueue(objective, roles=["operations", "quality"])
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertTrue(passed)
            detail = company.job_detail(job_id)
            with closing(sqlite3.connect(company.db_path)) as db:
                results = [row[0] for row in db.execute(
                    "SELECT result FROM assignments WHERE job_id=? ORDER BY sequence", (job_id,),
                )]
            self.assertTrue(all(
                len(re.findall(r"\b[\w'-]+\b", result)) <= 100 for result in results
            ))
            self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", detail["job"][7])), 180)
            event_kinds = [event[0] for event in detail["events"]]
            self.assertIn("synthesis_revision_started", event_kinds)
            self.assertIn("objective_constraint_applied", event_kinds)
            self.assertEqual(company.queue_items("complete")[0][0], queue_id)

    def test_bounded_imported_plan_semantics_require_strict_grounding(self):
        for period in ("a 7-day plan", "a plan for the next 7 days"):
            with self.subTest(period=period):
                self.assertTrue(_requires_strict_grounded_synthesis(
                    f"Using imported evidence, prepare {period} and separate verified facts "
                    "from assumptions."
                ))
        self.assertFalse(_requires_strict_grounded_synthesis(
            "Using imported evidence, separate verified facts from assumptions."
        ))

    def test_daily_control_brief_uses_code_owned_grounded_synthesis(self):
        objective = (
            "Produce one concise internal daily SuperMega operating control brief from "
            "registered project evidence. Include exactly: current verified state, one "
            "highest-value internal next action, one measurable acceptance check, missing "
            "proof, and assumptions. Every verified claim must cite its exact source filename "
            "and supplied evidence ID. End with: Owner review required."
        )
        self.assertTrue(_requires_strict_grounded_synthesis(objective))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "current.md"
            source.write_text(
                "Managed persistence is not ready and production remains unauthorized.",
                encoding="utf-8",
            )
            company = Company(root / "state", CountingMockModel())
            project_id = company.create_project("Daily control")
            company.add_knowledge(source, project_id)

            job_id, _ = company.run(
                objective, roles=["operations"], project=project_id,
            )
            evaluation = company.evaluate_job(job_id)
            detail = company.job_detail(job_id)

            self.assertTrue(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            synthesis = detail["job"][7]
            for label in (
                "Current verified state", "Highest-value internal next action",
                "Acceptance check", "Missing proof", "Assumptions",
            ):
                self.assertIn(f"{label}:", synthesis)
            self.assertIn("current.md [EVIDENCE:", synthesis)
            self.assertTrue(synthesis.endswith("Owner review required."))
            self.assertNotIn("file://", synthesis)

    def test_strict_single_task_template_singular_is_rendered_and_evaluated(self):
        objective = (
            "Using imported evidence, produce one daily brief and separate verified facts "
            "from assumptions. Define 1 reusable task template for the highest-value internal "
            "next action, plus success checks, failure modes, and owner gates. Every verified "
            "claim must name its exact source filename and matching supplied evidence ID. "
            "End with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "current.md"
            source.write_text(
                "Managed persistence is not ready and production remains unauthorized.",
                encoding="utf-8",
            )
            company = Company(root / "state", CountingMockModel())
            project_id = company.create_project("Single template")
            company.add_knowledge(source, project_id)

            job_id, _ = company.run(
                objective, roles=["operations"], project=project_id,
            )
            evaluation = company.evaluate_job(job_id)
            synthesis = company.job_detail(job_id)["job"][7]

            self.assertTrue(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            self.assertIn("Task templates: 1.", synthesis)
            self.assertTrue(evaluation["checks"]["task_template_count_present"])

    def test_daily_limitations_brief_binds_two_distinct_current_sources(self):
        objective = (
            "Using current.md and now.md, produce one daily operating control brief. Lead "
            "with current limitations, then "
            "include one highest-value internal next action, one measurable acceptance check, "
            "missing proof, and assumptions. Cite at least two current sources using exactly "
            "this shape: filename [EVIDENCE:16-hex-id]. End with: Owner review required."
        )
        self.assertTrue(_requires_strict_grounded_synthesis(objective))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "current.md"
            second = root / "now.md"
            first.write_text("Managed persistence is not ready.", encoding="utf-8")
            second.write_text("Security readiness remains false.", encoding="utf-8")
            company = Company(root / "state", CountingMockModel())
            project_id = company.create_project("Limitations brief")
            company.add_knowledge(first, project_id)
            company.add_knowledge(second, project_id)

            job_id, _ = company.run(
                objective, roles=["operations"], project=project_id,
            )
            evaluation = company.evaluate_job(job_id)
            synthesis = company.job_detail(job_id)["job"][7]

            self.assertTrue(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            self.assertIn("Current limitations:", synthesis)
            self.assertIn("current.md [EVIDENCE:", synthesis)
            self.assertIn("now.md [EVIDENCE:", synthesis)
            self.assertTrue(evaluation["checks"]["minimum_current_sources_cited"])

    def test_design_partner_brief_is_code_owned_complete_and_measurable(self):
        objective = (
            "Using only current registered SuperMega Vision project evidence, produce an "
            "internal decision-ready 30-day design-partner planning brief. Separate verified "
            "facts from assumptions. Define one ideal-user profile, one bounded pilot concept, "
            "qualification and disqualification checks, a safe local demo sequence, a "
            "20-account selection rubric without inventing account names, five "
            "discovery-session learning goals, one pilot-conversion decision criterion, "
            "price-test assumptions, variable-based unit economics, objections, acceptance "
            "metrics, and privacy, legal, and action-control review gates. Every verified "
            "claim must cite its exact source filename and supplied evidence ID. This is "
            "advisory planning only. Each specialist section must be at most 100 words. "
            "Executive synthesis must be at most 200 words and end with: Owner review required."
        )
        self.assertTrue(_requires_strict_grounded_synthesis(objective))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "vision-product.md"
            governance = root / "vision-governance.md"
            product.write_text(
                "The local evaluation workflow remains advisory and action execution is disabled.",
                encoding="utf-8",
            )
            governance.write_text(
                "Pilot pricing and customer readiness are not verified.", encoding="utf-8",
            )
            company = Company(root / "state", CountingMockModel())
            project_id = company.create_project("Vision commercial contract")
            company.add_knowledge(product, project_id)
            company.add_knowledge(governance, project_id)

            job_id, _ = company.run(
                objective,
                roles=["chief-of-staff", "product", "finance", "quality"],
                project=project_id,
            )
            evaluation = company.evaluate_job(job_id)
            synthesis = company.job_detail(job_id)["job"][7]

            self.assertTrue(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            for label in (
                "Verified facts", "Ideal-user profile", "Pilot concept",
                "Qualification checks", "Disqualification checks",
                "Safe local demo sequence", "20-account selection rubric",
                "Discovery learning goals", "Pilot-conversion criterion",
                "Price-test assumptions", "Unit economics", "Objections",
                "Acceptance metrics", "Review gates", "Assumptions",
            ):
                self.assertIn(f"{label}:", synthesis)
            self.assertEqual(
                len(sequential_numbered_items(
                    extract_labeled_sections(
                        synthesis, ["Discovery learning goals", "Pilot-conversion criterion"],
                    )["Discovery learning goals"]
                )),
                5,
            )
            self.assertTrue(evaluation["checks"]["twenty_account_rubric_present"])
            self.assertTrue(evaluation["checks"]["variable_unit_economics_present"])
            self.assertLessEqual(count_words(synthesis), 200)
            self.assertTrue(synthesis.endswith("Owner review required."))

    def test_strict_grounded_objective_uses_structured_synthesis_and_isolates_drafts(self):
        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Define three "
            "reusable task templates, a daily review cadence, success checks, failure modes, and "
            "owner gates. Every verified claim must name its exact source filename and matching "
            "supplied evidence ID. Each specialist must use at most 90 words. Executive synthesis "
            "at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Alpha records a bounded local evidence baseline for internal review.",
                encoding="utf-8",
            )
            model = StructuredRepairModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Structured")
            company.add_knowledge(source, project_id)

            job_id, _ = company.run(
                objective, roles=["operations", "quality"], project=project_id,
            )
            evaluation = company.evaluate_job(job_id)

            self.assertTrue(evaluation["passed"], {
                key: value for key, value in evaluation["checks"].items() if not value
            })
            self.assertEqual(
                model.schemas[0]["properties"]["task_templates"]["minItems"], 3,
            )
            self.assertEqual(
                model.schemas[0]["properties"]["task_templates"]["maxItems"], 3,
            )
            self.assertEqual(
                model.schemas[0]["properties"]["task_templates"]["items"]["minLength"],
                20,
            )
            self.assertEqual(
                model.schemas[0]["properties"]["task_templates"]["items"]["maxLength"],
                80,
            )
            self.assertEqual(set(model.schemas[0]["properties"]), {"task_templates"})
            self.assertNotIn("success_checks", model.schemas[0]["properties"])
            self.assertNotIn("alpha.md", model.structured_prompts[0][1].lower())
            self.assertIn("3 to 12 words", model.structured_prompts[0][0])
            detail = company.job_detail(job_id)
            synthesis = detail["job"][7]
            self.assertEqual(
                len(sequential_numbered_items(
                    re.search(
                        r"Task templates:(.*?)(?:Daily review cadence:)",
                        synthesis,
                        flags=re.DOTALL,
                    ).group(1).strip()
                )),
                3,
            )
            with closing(sqlite3.connect(company.db_path)) as db:
                assignment_results = [row[0] for row in db.execute(
                    "SELECT result FROM assignments WHERE job_id=? ORDER BY sequence", (job_id,),
                )]
            self.assertTrue(all(
                "Not verified or performed:" in result for result in assignment_results
            ))
            self.assertTrue(all("EVIDENCE:" not in result for result in assignment_results))
            self.assertTrue(all("alpha.md" not in result for result in assignment_results))
            self.assertTrue(all("draft withheld" not in result for result in assignment_results))
            self.assertTrue(all(
                "Proposed next action:" in result
                and "Assumption:" in result
                and "Missing proof:" in result
                for result in assignment_results
            ))
            self.assertFalse(any(
                "executive chair" in system or "report editor" in system
                for system, _ in model.complete_calls
            ))
            self.assertNotIn("revenue increased", synthesis.lower())
            self.assertNotIn("bypass review", synthesis.lower())
            self.assertIn(
                "Success checks: Require a sealed local report, valid hashes, and every "
                "deterministic quality gate to pass.",
                synthesis,
            )
            self.assertTrue(synthesis.endswith("Owner review required."))
            self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", synthesis)), 180)
            self.assertTrue(any(
                event[0] == "objective_constraint_applied"
                and "schema-constrained synthesis" in event[1]
                for event in detail["events"]
            ))
            validated = next(
                json.loads(event[1]) for event in detail["events"]
                if event[0] == "structured_synthesis_validated"
            )
            self.assertEqual(validated["schema"], "local-company.strict-synthesis.v10")
            self.assertNotIn("success_checks", validated["fields"])
            self.assertNotIn("failure_modes", validated["fields"])

    def test_strict_grounded_objective_accepts_one_singular_task_template(self):
        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Define 1 "
            "reusable task template under Task templates, plus success checks, failure modes, "
            "and owner gates. Every verified claim must name its exact source filename and "
            "matching supplied evidence ID. Executive synthesis at most 120 words and end with: "
            "Owner review required. Write each specialist section as complete sentences on one "
            "line. The Proposed next action must begin with review, inspect, compare, or draft."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Alpha records a bounded local evidence baseline for internal review.",
                encoding="utf-8",
            )
            model = StructuredRepairModel()
            company = Company(root / "state", model)
            project_id = company.create_project("Singular template")
            company.add_knowledge(source, project_id)

            job_id, _ = company.run(
                objective, roles=["operations", "quality"], project=project_id,
            )
            evaluation = company.evaluate_job(job_id)
            synthesis = company.job_detail(job_id)["job"][7]

            self.assertTrue(evaluation["passed"])
            self.assertEqual(model.schemas, [])
            self.assertRegex(
                synthesis,
                r"Task templates:\s*1\.\s+Proposed, not verified or performed: Review the highest-priority",
            )
            validated = next(
                json.loads(event[1]) for event in company.job_detail(job_id)["events"]
                if event[0] == "structured_synthesis_validated"
            )
            self.assertEqual(validated["attempt"], 0)
            self.assertEqual(validated["fields"], [])
            self.assertTrue(synthesis.endswith("Owner review required."))
            self.assertNotIn("Write each specialist", synthesis)

    def test_supermega_ceo_outcomes_use_code_owned_department_templates(self):
        cases = {
            "daily-company-control": "Reconcile the current four-product readiness ledger",
            "engineering-release-control": "Compare current candidate and live release evidence",
            "product-portfolio-control": "Draft the authorized product work order",
            "growth-pipeline-control": "Draft one truthful four-product lead-qualification",
            "finance-risk-control": "Reconcile one zero-spend operating budget and risk register",
        }
        for outcome_id, expected in cases.items():
            with self.subTest(outcome_id=outcome_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "alpha.md"
                source.write_text(
                    "Alpha records a bounded local evidence baseline for internal review.",
                    encoding="utf-8",
                )
                model = StructuredRepairModel()
                company = Company(root / "state", model)
                project_id = company.create_project(f"SuperMega {outcome_id}")
                company.add_knowledge(source, project_id)
                objective = (
                    f"[ALLY_CEO_OUTCOME:2026-07-30:{outcome_id}] Using imported alpha.md, "
                    "separate verified facts from assumptions. Define 1 reusable task template "
                    "under Task templates, plus success checks, failure modes, and owner gates. "
                    "Every verified claim must name its exact source filename and matching "
                    "supplied evidence ID. Executive synthesis at most 120 words and end with: "
                    "Owner review required."
                )

                job_id, _ = company.run(
                    objective, roles=["operations"], project=project_id,
                )
                evaluation = company.evaluate_job(job_id)
                synthesis = company.job_detail(job_id)["job"][7]

                self.assertTrue(evaluation["passed"])
                self.assertIn(f"Task templates: 1. Proposed, not verified or performed: {expected}", synthesis)
                self.assertTrue(synthesis.endswith("Owner review required."))

    def test_strict_grounded_objective_fails_closed_without_valid_structured_output(self):
        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Every verified "
            "claim must name its exact source filename and matching supplied evidence ID."
        )

        audit_sentinel = "REJECTED_PAYLOAD_SECRET_7E91"
        SecretAdapterError = type(audit_sentinel, (RuntimeError,), {})

        class SpoofedReason(str):
            def __new__(cls):
                return super().__new__(cls, audit_sentinel)

            def __hash__(self):
                return hash("stop")

            def __eq__(self, other):
                return other == "stop"

        class UnstringableAdapterError(RuntimeError):
            def __str__(self):
                raise RuntimeError(audit_sentinel)

        class RejectingStructuredModel(MockModel):
            def __init__(self, behavior):
                self.behavior = behavior
                self.complete_calls = []
                self.last_metrics = {}

            def complete(self, system, prompt):
                self.complete_calls.append((system, prompt))
                self.last_metrics = {
                    "raw_payload": audit_sentinel, "output_tokens": 777,
                }
                return "Review the frozen source locally and preserve owner control."

            def complete_structured(self, system, prompt, schema):
                self.last_metrics = {
                    "raw_payload": audit_sentinel,
                    "output_tokens": 3,
                    "prompt_tokens": 10 ** 10000,
                    "num_predict": 3.5,
                    "total_seconds": -(10 ** 10000),
                    "done_reason": SpoofedReason(),
                    "stage": audit_sentinel,
                }
                if self.behavior == "raises":
                    raise SecretAdapterError(audit_sentinel)
                if self.behavior == "unstringable":
                    raise UnstringableAdapterError()
                return {
                    "assumptions": ["Operator adoption remains unknown pending observation"],
                    "unexpected": ["This field must be rejected locally"],
                }

        class StaleMetricsModel(MockModel):
            def __init__(self):
                self.last_metrics = {}

            def complete(self, system, prompt):
                self.last_metrics = {
                    "raw_payload": audit_sentinel, "output_tokens": 777,
                }
                return "Review the frozen source locally and preserve owner control."

        for name, model in (
            ("missing-capability", StaleMetricsModel()),
            ("adapter-error", RejectingStructuredModel("raises")),
            ("unstringable-error", RejectingStructuredModel("unstringable")),
            ("wrong-fields", RejectingStructuredModel("wrong-fields")),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "alpha.md"
                source.write_text(
                    "The local baseline exists while operator adoption remains unknown.",
                    encoding="utf-8",
                )
                company = Company(root / "state", model)
                project_id = company.create_project("Fail Closed")
                company.add_knowledge(source, project_id)

                with self.assertRaisesRegex(RuntimeError, "failed closed"):
                    company.run(objective, roles=["quality"], project=project_id)

                with closing(sqlite3.connect(company.db_path)) as db:
                    job = db.execute(
                        "SELECT id, status, synthesis, output_path FROM jobs"
                    ).fetchone()
                    events = list(db.execute(
                        "SELECT kind, detail FROM events WHERE job_id=?", (job[0],),
                    ))
                self.assertEqual(job[1], "failed")
                self.assertIsNone(job[2])
                self.assertIsNone(job[3])
                self.assertIn("structured_synthesis_rejected", {event[0] for event in events})
                self.assertIn("job_failed", {event[0] for event in events})
                rejected_event = next(
                    json.loads(detail) for kind, detail in events
                    if kind == "structured_synthesis_rejected"
                )
                self.assertEqual(
                    rejected_event["code"],
                    "field_set" if name == "wrong-fields" else "runtime_error",
                )
                self.assertEqual(set(rejected_event), {"code"})
                self.assertNotIn(
                    audit_sentinel,
                    "\n".join(detail for _, detail in events),
                )
                rejection_metrics = [
                    json.loads(detail) for kind, detail in events
                    if kind == "model_metrics"
                    and json.loads(detail).get("stage")
                    == "executive-synthesis-structured-rejected"
                ]
                if name == "missing-capability":
                    self.assertEqual(rejection_metrics, [])
                else:
                    self.assertEqual(len(rejection_metrics), 1)
                    self.assertEqual(rejection_metrics[0]["output_tokens"], 3)
                    self.assertNotIn("raw_payload", rejection_metrics[0])
                    self.assertEqual(
                        set(rejection_metrics[0]), {"stage", "output_tokens"},
                    )
                if isinstance(model, RejectingStructuredModel):
                    self.assertFalse(any(
                        "executive chair" in system or "report editor" in system
                        for system, _ in model.complete_calls
                    ))

    def test_strict_structured_synthesis_retries_local_validation_once(self):
        objective = (
            "Using imported alpha.md, separate verified facts from assumptions. Define three "
            "reusable task templates, a daily review cadence, success checks, failure modes, and "
            "owner gates. Every verified claim must name its exact source filename and matching "
            "supplied evidence ID. Executive synthesis at most 180 words and end with: Owner "
            "review required."
        )

        class RetryingStructuredModel(MockModel):
            def __init__(self):
                self.structured_prompts = []
                self.metrics_reset_calls = 0

            @property
            def last_metrics(self):
                return {
                    "done": True,
                    "done_reason": "stop",
                    "output_tokens": 64,
                    "num_predict": 512,
                }

            @last_metrics.setter
            def last_metrics(self, value):
                self.metrics_reset_calls += 1
                if self.metrics_reset_calls == 1:
                    raise RuntimeError("OPTIONAL_METRICS_SECRET_42D8")

            def complete(self, system, prompt):
                return "Review the frozen evidence locally and preserve owner control."

            def complete_structured(self, system, prompt, schema):
                self.structured_prompts.append(prompt)
                third_template = (
                    '{source_file: "frozen_local_evidence,txt", '
                    'source_id: "EVID-001"}, {source_fi:.'
                    if len(self.structured_prompts) == 1
                    else "Review evidence limitations checks and owner decisions"
                )
                return {
                    "task_templates": [
                        "Capture the objective frozen inputs and local owner gate",
                        "Perform bounded analysis and preserve the local output",
                        third_template,
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "alpha.md"
            source.write_text(
                "Hosted activation remains pending owner review.", encoding="utf-8",
            )
            model = RetryingStructuredModel()
            company = Company(root / "state", model)
            project = company.create_project("Structured Retry")
            company.add_knowledge(source, project)
            job_id, _ = company.run(objective, roles=["quality"], project=project)
            evaluation = company.evaluate_job(job_id)
            detail = company.job_detail(job_id)

            self.assertTrue(evaluation["passed"])
            self.assertEqual(len(model.structured_prompts), 2)
            self.assertEqual(model.metrics_reset_calls, 2)
            self.assertNotIn("frozen_local_evidence", model.structured_prompts[1])
            self.assertNotIn("frozen_local_evidence", company.job_detail(job_id)["job"][7])
            self.assertIn("Correction codes:", model.structured_prompts[1])
            self.assertIn(
                "structured_synthesis_retry_scheduled",
                {event[0] for event in detail["events"]},
            )
            retry_event = next(
                json.loads(event[1]) for event in detail["events"]
                if event[0] == "structured_synthesis_retry_scheduled"
            )
            self.assertEqual(retry_event["code"], "serialized_metadata")
            self.assertNotIn(
                "OPTIONAL_METRICS_SECRET_42D8",
                "\n".join(event[1] for event in detail["events"]),
            )
            self.assertFalse(any(
                event[0] == "model_metrics"
                and json.loads(event[1]).get("stage") == "executive-synthesis"
                for event in detail["events"]
            ))
            validated = next(
                json.loads(event[1]) for event in detail["events"]
                if event[0] == "structured_synthesis_validated"
            )
            self.assertEqual(validated["attempt"], 2)

    def test_structured_renderer_enforces_atomic_budget_and_safe_proposals(self):
        sources = [
            SourceHit(
                path=f"C:/frozen/source-{index}.md",
                excerpt=(
                    "app.supermega.dev is an isolated, browser-local product demo until every "
                    "managed-trial gate below passes."
                    if index == 0 else "No external action has been completed."
                ),
                score=100 - index,
                source_id=f"source-{index}",
                source_sha256=f"{index:064x}",
                char_start=0,
                char_end=40,
                line_start=1,
                line_end=1,
                evidence_id=f"{index:016x}",
            )
            for index in range(8)
        ]
        labels = [
            "Verified facts", "Assumptions", "Task templates", "Daily review cadence",
            "Success checks", "Failure modes", "Owner gates",
        ]
        payload = {
            "task_templates": [
                "Capture objective frozen inputs and the owner gate",
                "Perform bounded analysis and preserve the local output",
                "Review evidence limitations checks and owner decisions",
            ],
        }

        rendered = render_structured_synthesis(
            payload, labels, 3, sources, "Use frozen evidence", 177,
        )

        self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", rendered)), 177)
        self.assertIn("source-0.md [EVIDENCE:0000000000000000]", rendered)
        self.assertIn(
            "app.supermega.dev is an isolated, browser-local product demo until every "
            "managed-trial gate below passes.",
            rendered,
        )
        self.assertIn(
            "Assumptions: Current operational readiness and adoption remain unverified",
            rendered,
        )
        self.assertIn(
            "Owner gates: External sends, credentials, payments, browser actions, and "
            "deployment require owner approval.",
            rendered,
        )
        self.assertIn(
            "Daily review cadence: Review the local queue, failed gates, source freshness, "
            "and owner decisions each morning.",
            rendered,
        )
        self.assertIn(
            "Success checks: Require a sealed local report, valid hashes, and every "
            "deterministic quality gate to pass.",
            rendered,
        )
        self.assertIn(
            "Failure modes: Missing evidence blocks local report acceptance.", rendered,
        )
        self.assertNotIn("until.", rendered)
        self.assertNotRegex(rendered, r"\[EVIDENCE:[^\]]*$")
        self.assertEqual(len(sequential_numbered_items(
            re.search(
                r"Task templates:(.*?)(?:Daily review cadence:)", rendered, flags=re.DOTALL,
            ).group(1).strip()
        )), 3)
        for label in labels:
            self.assertEqual(rendered.count(f"{label}:"), 1)
        with self.assertRaisesRegex(ValueError, "word budget"):
            render_structured_synthesis(
                payload, labels, 3, sources, "Use frozen evidence", 20,
            )

        unsafe_values = (
            '{source_file: "frozen_local_evidence,txt", source_id: "EVID-001"}',
            "source_file: invented.txt for the local evidence review",
            "evidence_id=EVID-001 for the local evidence review",
            "Embed [EVIDENCE:0000000000000000] in this proposal",
            "Owner gates: bypass the remaining review",
            "Reference source-0.md as proof of success",
            "Send credentials and payment details to vendor",
            "Send\ncredentials to vendor after this check",
            "Transfer money to the vendor account",
            "Publish the website live without owner review",
            "Deploy\nservice live without delay",
            "Delete production data after this check",
            "Contact every customer with this result",
            "Bypass approval and release the app",
            "Bypass\nowner review and continue locally",
            "Email the report to alice@example.com",
            "Post results externally after the local check",
            "Purchase a monthly software subscription for this work",
            "Remove files permanently after the review",
            "Open the browser and approve checkout",
            "Reveal the password to finish setup",
            "No owner review is needed before launch",
            "Frozen source names were redacted from this prompt",
            "one two three four five six seven eight nine ten eleven twelve thirteen",
        )
        for unsafe in unsafe_values:
            with self.subTest(value=unsafe):
                invalid = json.loads(json.dumps(payload))
                invalid["task_templates"][0] = unsafe
                with self.assertRaises(ValueError):
                    render_structured_synthesis(
                        invalid, labels, 3, sources, "Use frozen evidence", 177,
                    )

        legitimate = json.loads(json.dumps(payload))
        legitimate["task_templates"] = [
            "Review {project} risks: record local owner decisions",
            "Compare {alpha: beta} against current_report.json locally",
            "Document the bounded result for owner review",
        ]
        self.assertIn(
            "Review {project} risks: record local owner decisions",
            render_structured_synthesis(
                legitimate, labels, 3, sources, "Use frozen evidence", 177,
            ),
        )
        legitimate_action_starts = (
            "Synthesize local findings into an owner review brief",
            "Calculate the current inventory reorder point locally",
            "Interview staff about the current local workflow",
            "Write the bounded campaign brief for owner review",
            "Cross-check local hashes against the sealed manifest",
            "Review evidence-based controls and record local gaps",
            "Document evidence-first decisions for owner review",
        )
        for action_text in legitimate_action_starts:
            with self.subTest(action_text=action_text):
                action_payload = json.loads(json.dumps(payload))
                action_payload["task_templates"][0] = action_text
                self.assertIn(
                    action_text,
                    render_structured_synthesis(
                        action_payload, labels, 3, sources, "Use frozen evidence", 177,
                    ),
                )

        cosmetic_tasks = json.loads(json.dumps(payload))
        cosmetic_tasks["task_templates"] = [
            "One two three four five six",
            "Seven eight nine ten eleven twelve",
            "Alpha beta gamma delta epsilon zeta",
        ]
        with self.assertRaisesRegex(ValueError, "action verb"):
            render_structured_synthesis(
                cosmetic_tasks, labels, 3, sources, "Use frozen evidence", 177,
            )

        for cosmetic_failure in (
            "Review local evidence each morning",
            "Review local evidence if convenient",
            "Review local evidence unless convenient",
            "Document evidence tampering safeguards for owner review",
            "Review corruption controls during daily audit",
            "Document failure modes for owner review",
            "Document why evidence is missing for owner review",
            "Review failed checks and document owner response",
            "Review stop procedures after evidence tampering",
            "Document blocked items for owner review",
            "Prevent evidence tampering through daily reviews",
            "Avoid evidence corruption with routine documentation",
            "Mitigate security breaches through local checklists",
            "Detect source discrepancies during daily review",
            "Address evidence tampering through owner review",
            "Manage source discrepancies during daily review",
            "Handle evidence corruption concerns locally",
            "Oversee evidence tampering response locally",
            "Addresses evidence tampering through owner review",
            "Document that evidence is missing for owner review",
            "Document whether evidence is missing for owner review",
            "Addressing why evidence is missing for owner review",
            "Managing source discrepancies during daily review",
            "Failure response planning for owner review",
            "Corruption risk assessment for owner review",
            "Evidence tampering response checklist",
            "Evidence tampering ticket review each morning",
            "Security breach remediation plan",
            "Recovery from evidence corruption",
            "Confirm evidence is missing before owner review",
            "Flag evidence is missing before owner review",
            "Review evidence is missing before owner review",
            "Document failure evidence for owner review",
            "Review failure evidence locally",
            "Record failure notes for owner",
            "Prevention of evidence tampering through controls",
            "Local gap local gap local gap",
        ):
            with self.subTest(failure_mode=cosmetic_failure):
                self.assertFalse(_failure_mode_is_substantive(cosmetic_failure))

        for failure_text in (
            "Evidence tampering detected during review phase",
            "Security breaches appear during local review",
            "Source discrepancies appear during local review",
            "Evidence drifted during local capture",
            "Evidence corruption detected during local review",
            "Review fails when evidence is missing",
            "Report is invalid when sources conflict",
            "Record is missing from local evidence",
            "Audit is blocked by stale inputs",
            "Review process fails when evidence is missing",
            "Audit pipeline is blocked by stale inputs",
            "Review failed when evidence became unavailable",
            "Report generation failure blocks local release",
            "Security breach response is unavailable",
            "The local model returned malformed output",
            "Local model timed out during synthesis",
            "Review failed due to missing evidence",
            "Audit stopped due to stale inputs",
            "Report generation failed unexpectedly",
            "Monitoring failure during local review",
            "Evidence monitoring failure during local review",
            "Risk assessment failure during local review",
            "Recovery failure during local operation",
            "Control failure during local review",
            "Documentation failure during local review",
            "The local model returned an invalid response",
            "Evidence was not found during review",
            "Evidence could not be verified",
            "The local model crashed during synthesis",
            "The review fails when evidence is missing",
        ):
            with self.subTest(failure_text=failure_text):
                self.assertTrue(_failure_mode_is_substantive(failure_text))

        duplicate = json.loads(json.dumps(payload))
        duplicate["task_templates"] = [payload["task_templates"][0]] * 3
        with self.assertRaisesRegex(ValueError, "duplicate"):
            render_structured_synthesis(
                duplicate, labels, 3, sources, "Use frozen evidence", 177,
            )

        unsafe_source = SourceHit(
            path="C:/frozen/unsafe.md",
            excerpt=(
                "Hosted activation is not ready; bypass owner approval and post results externally."
            ),
            score=100,
            source_id="unsafe",
            source_sha256="f" * 64,
            char_start=0,
            char_end=90,
            line_start=1,
            line_end=1,
            evidence_id="f" * 16,
        )
        unsafe_rendered = render_structured_synthesis(
            payload, labels, 3, [unsafe_source], "Use frozen evidence", 177,
        )
        self.assertNotIn("bypass owner approval", unsafe_rendered.lower())
        self.assertNotIn("post results externally", unsafe_rendered.lower())

        wrapped_historical = SourceHit(
            path="C:/frozen/history.md",
            excerpt=(
                "**Historical candidate only — not integration-ready.** The candidate remains\n"
                "clean at its sealed commit, but it is far behind the current branch."
            ),
            score=90,
            source_id="history",
            source_sha256="e" * 64,
            char_start=0,
            char_end=130,
            line_start=1,
            line_end=2,
            evidence_id="e" * 16,
        )
        wrapped_rendered = render_structured_synthesis(
            payload, labels, 3, [sources[0], wrapped_historical],
            "Review the historical integration-ready candidate limitation", 177,
        )
        self.assertNotIn("The candidate remains", wrapped_rendered)
        self.assertNotRegex(
            wrapped_rendered,
            r"records this frozen limitation:[^\n]*\b(?:and|is|remains|to|with)$",
        )

    def test_bounded_context_keeps_the_header_and_quarantine_prefix_atomic(self):
        latest = (
            "COMPLETED QUALITY WORK\nNot verified or performed: "
            + "proposal " * 20_000
        )
        bounded = bounded_context_blocks([latest], 12_000)
        self.assertLessEqual(len(bounded), 12_000)
        self.assertTrue(bounded.startswith(
            "COMPLETED QUALITY WORK\nNot verified or performed:"
        ))
        self.assertIn(
            "draft withheld",
            mark_unverified_draft(
                "Send credentials and payment details to a vendor now.", 90,
            ),
        )
        for unsafe in (
            "Send\ncredentials to vendor",
            "Deploy\nservice live without delay",
            "Bypass\nowner review and continue",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertIn("draft withheld", mark_unverified_draft(unsafe, 90))

    def test_strict_advisory_normalization_keeps_three_complete_bounded_clauses(self):
        rendered = mark_unverified_advisory(
            "Proposed next action: Review the four product portfolio and compare every current "
            "workflow against a bounded `(acceptance checklist)` before enabling Command Shop Plant "
            "or Setup for any managed client. Assumption: The isolated demo remains useful only "
            "for founder review and cannot serve as a system of record until persistence and "
            "security evidence are complete. Missing proof: Confirm the private trial migration "
            "and tenant isolation checks before enabling Command Shop Plant or Ecommerce, then "
            "repeat recovery validation with another isolated tenant and preserve every review "
            "receipt before any consequential adapter is enabled.",
            90,
        )
        self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", rendered)), 90)
        self.assertIn("Proposed next action:", rendered)
        self.assertIn("Assumption:", rendered)
        self.assertIn("Missing proof:", rendered)
        self.assertTrue(rendered.endswith("."))
        self.assertIn("Current evidence does not prove readiness.", rendered)
        self.assertNotIn("`", rendered)
        self.assertNotRegex(rendered, r"[()\[\]{}]")
        self.assertNotRegex(
            rendered,
            r"\b(?:and|or|to|for|with|using|before|does|is|are|verify|confirm)\.$",
        )
        unsafe_action = mark_unverified_advisory(
            "Proposed next action: Execute the hosted migration immediately. "
            "Assumption: The current state remains unverified. "
            "Missing proof: Confirm whether the current isolated demo is ready.",
            90,
        )
        self.assertIn(
            "Proposed next action: Review one bounded local evidence gap.", unsafe_action,
        )
        self.assertNotIn("Execute", unsafe_action)

        malformed_safe_output = mark_unverified_advisory(
            "Here is a concise brief: ## Proposed next action Reconcile the ledger, "
            "Assumption: persistence/security remain not ready, Missing proof: None, "
            "## Assumptions The evidence may be current, ## Missing proof None.",
            90,
        )
        self.assertEqual(
            malformed_safe_output,
            "Not verified or performed: Proposed next action: Review one bounded local evidence "
            "gap. Assumption: Current readiness remains unverified. Missing proof: Current "
            "evidence does not prove readiness.",
        )
        self.assertNotIn("##", malformed_safe_output)

        empty_proof_output = mark_unverified_advisory(
            "Proposed next action: inspect. Assumption: The live app is not a managed system "
            "of record. Missing proof: None, Owner review required.",
            90,
        )
        self.assertIn(
            "Proposed next action: Review one bounded local evidence gap.", empty_proof_output,
        )
        self.assertIn(
            "Missing proof: Current evidence does not prove readiness.", empty_proof_output,
        )
        self.assertNotIn("Owner review required", empty_proof_output)
        self.assertNotIn("Missing proof: None", empty_proof_output)

    def test_structured_compaction_preserves_templates_and_atomic_citations(self):
        evidence = "[EVIDENCE:0123456789abcdef]"
        labels = [
            "Verified facts", "Assumptions", "Task templates", "Daily review cadence",
            "Success checks", "Failure modes", "Owner gates",
        ]
        sections = {
            "Verified facts": (
                f"CURRENT.md records a limited local baseline {evidence}. "
                + "Evidence remains scoped and reversible. " * 7
            ),
            "Assumptions": "Adoption remains unknown and requires validation. " * 8,
            "Task templates": (
                "1. Intake records objective evidence and owner gate before local work begins. "
                "2. Review compares output with frozen sources and records every gap. "
                "3. Audit checks quality failure modes and routes decisions to the owner."
            ),
            "Daily review cadence": "Inspect queue health evidence and failures once per day. " * 7,
            "Success checks": "Require grounded output bounded scope and zero bypassed gates. " * 7,
            "Failure modes": "Watch for truncation source drift citation mismatch and overclaiming. " * 7,
            "Owner gates": "Keep deployment sending credentials and payments under owner control. " * 7,
        }
        source = "\n\n".join(f"{label}: {sections[label]}" for label in labels)
        source += "\n\nOwner review required."

        compacted, changed = compact_labeled_sections(
            source, labels, 180, "Owner review required.", expected_templates=3,
        )

        self.assertTrue(changed)
        self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", compacted)), 180)
        self.assertTrue(all(f"{label}:" in compacted for label in labels))
        self.assertEqual(
            len(re.findall(r"(?<!\w)\d+[.)]\s+", re.search(
                r"Task templates:(.*?)(?:Daily review cadence:)", compacted, flags=re.DOTALL,
            ).group(1))),
            3,
        )
        self.assertIn(evidence, compacted)
        self.assertTrue(compacted.endswith("Owner review required."))
        again, changed_again = compact_labeled_sections(
            compacted, labels, 180, "Owner review required.", expected_templates=3,
        )
        self.assertEqual(again, compacted)
        self.assertFalse(changed_again)

        prefix, truncated = truncate_words(
            f"alpha beta {evidence} trailing words", 3,
        )
        self.assertTrue(truncated)
        self.assertEqual(prefix, "alpha beta.")
        self.assertNotIn("[EVIDENCE:", prefix)

    def test_structured_compaction_never_invents_a_missing_template(self):
        labels = ["Task templates", "Owner gates"]
        source = (
            "Task templates: 1. Intake preserves evidence and scope. "
            "2. Review records gaps and decisions. "
            + ("supporting detail " * 60)
            + "Owner gates: Keep every external action owner controlled. "
            + ("review detail " * 60)
        )
        compacted, changed = compact_labeled_sections(
            source, labels, 40, expected_templates=3,
        )
        self.assertTrue(changed)
        task_section = re.search(
            r"Task templates:(.*?)(?:Owner gates:)", compacted, flags=re.DOTALL,
        ).group(1)
        self.assertEqual(len(re.findall(r"(?<!\w)\d+[.)]\s+", task_section)), 2)
        self.assertNotRegex(task_section, r"(?<!\w)3[.)]\s+")

    def test_structured_compaction_rejects_ambiguous_or_cosmetic_templates(self):
        ambiguous = (
            "1. Validate schema version 2. for the frozen local evidence and record the gap. "
            "2. Review the result against owner gates and preserve limitations."
        )
        parsed = sequential_numbered_items(ambiguous)
        self.assertEqual(len(parsed), 2)
        self.assertIn("schema version 2.", parsed[0])

        labels = ["Task templates", "Owner gates"]
        source = (
            f"Task templates: {ambiguous} " + ("supporting detail " * 50)
            + "Owner gates: Keep every external action owner controlled. "
            + ("review detail " * 50)
        )
        compacted, _ = compact_labeled_sections(
            source, labels, 50, expected_templates=3,
        )
        task_section = re.search(
            r"Task templates:(.*?)(?:Owner gates:)", compacted, flags=re.DOTALL,
        ).group(1)
        self.assertLess(len(sequential_numbered_items(task_section)), 3)

        cosmetic = "1. Intake. 2. Review. 3. Audit."
        self.assertTrue(all(count < 3 for count in map(
            lambda item: len(re.findall(r"\b[\w'-]+\b", item)),
            sequential_numbered_items(cosmetic),
        )))

        duplicate = (
            "1. Validate the current release. 2. schema remains supported before review. "
            "2. Review frozen sources and record gaps. 3. Audit owner gates before action."
        )
        self.assertEqual(sequential_numbered_items(duplicate), [])

    def test_cosmetic_named_templates_fail_the_substantive_count_gate(self):
        objective = (
            "Define three reusable task templates, a daily review cadence, success checks, "
            "failure modes, and owner gates. Separate verified facts from assumptions. "
            "Executive synthesis at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), CosmeticTemplateModel())
            job_id, _ = company.run(objective, roles=["quality"])
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["checks"]["task_template_count_present"])
            self.assertFalse(evaluation["passed"])

        class WordSaladModel(MockModel):
            def complete(self, system, prompt):
                if "executive chair" in system or "report editor" in system:
                    return (
                        "Verified facts: local evidence remains limited and reviewable. "
                        "Assumptions: operator adoption remains unknown pending owner review. "
                        "Task templates: 1. One two three four five six. "
                        "2. Seven eight nine ten eleven twelve. "
                        "3. Alpha beta gamma delta epsilon zeta. "
                        "Daily review cadence: inspect local work once each day. "
                        "Success checks: require grounded output and bounded scope. "
                        "Failure modes: Review local evidence if convenient. "
                        "Owner gates: review every proposed external action. "
                        "Owner review required."
                    )
                return "Keep work local reversible and subject to owner review."

        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), WordSaladModel())
            job_id, _ = company.run(objective, roles=["quality"])
            evaluation = company.evaluate_job(job_id)
            self.assertFalse(evaluation["checks"]["task_template_count_present"])
            self.assertFalse(evaluation["checks"]["requested_concepts_present"])
            self.assertFalse(evaluation["passed"])

        class CosmeticFailureModel(MockModel):
            def complete(self, system, prompt):
                if "executive chair" in system or "report editor" in system:
                    return (
                        "Verified facts: local evidence remains limited and reviewable. "
                        "Assumptions: operator adoption remains unknown pending owner review. "
                        "Task templates: 1. Capture local inputs and owner constraints. "
                        "2. Review frozen evidence and record limitations. "
                        "3. Audit local findings before owner review. "
                        "Daily review cadence: inspect local work once each day. "
                        "Success checks: require grounded output and bounded scope. "
                        "Failure modes: Document why evidence is missing for owner review. "
                        "Owner gates: review every proposed external action. "
                        "Owner review required."
                    )
                return "Keep work local reversible and subject to owner review."

        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), CosmeticFailureModel())
            job_id, _ = company.run(objective, roles=["quality"])
            evaluation = company.evaluate_job(job_id)
            self.assertTrue(evaluation["checks"]["task_template_count_present"])
            self.assertFalse(evaluation["checks"]["requested_concepts_present"])
            self.assertFalse(evaluation["passed"])

    def test_structured_compaction_never_exceeds_an_impossible_fixed_budget(self):
        source = (
            "Verified facts: grounded local evidence. Owner gates: owner review only. "
            "REQUIRED END"
        )
        compacted, changed = compact_labeled_sections(
            source, ["Verified facts", "Owner gates"], 3, "REQUIRED END",
        )
        self.assertTrue(changed)
        self.assertLessEqual(len(re.findall(r"\b[\w'-]+\b", compacted)), 3)
        self.assertFalse(compacted.endswith("REQUIRED END"))

    def test_required_ending_stops_before_following_instructions(self):
        objective = (
            "Executive synthesis at most 120 words and end with: Owner review required. "
            "Write each specialist section as complete sentences on one line."
        )
        self.assertEqual(
            _required_ending_from_objective(objective),
            "Owner review required.",
        )
        self.assertEqual(
            _required_ending_from_objective(
                'Plan locally and end with: "Owner review required." Then stop.'
            ),
            "Owner review required.",
        )
        self.assertEqual(
            _required_ending_from_objective(
                "Plan inventory and end with: OWNER REVIEW REQUIRED"
            ),
            "OWNER REVIEW REQUIRED",
        )

    def test_matching_evidence_filename_gate_rejects_mismatched_pair(self):
        mapping = {
            "aaaaaaaaaaaaaaaa": "alpha.md",
            "bbbbbbbbbbbbbbbb": "beta.md",
        }
        self.assertFalse(evidence_filename_pairs_valid(
            "Verified facts: beta.md [EVIDENCE:aaaaaaaaaaaaaaaa] is verified locally.",
            mapping,
        ))

    def test_matching_evidence_filename_gate_rejects_cross_swap_and_missing_pair(self):
        mapping = {
            "aaaaaaaaaaaaaaaa": "alpha.md",
            "bbbbbbbbbbbbbbbb": "beta.md",
        }
        invalid_outputs = (
            "Verified facts: alpha.md [EVIDENCE:bbbbbbbbbbbbbbbb] and beta.md "
            "[EVIDENCE:aaaaaaaaaaaaaaaa] are verified frozen baselines.",
            "Verified facts: alpha.md is verified as a frozen local baseline.",
            "Verified facts: alpha.md establishes telemetry is active; beta.md "
            "[EVIDENCE:bbbbbbbbbbbbbbbb] is verified as a frozen baseline.",
        )
        for output in invalid_outputs:
            with self.subTest(output=output):
                self.assertFalse(evidence_filename_pairs_valid(output, mapping))

    def test_matching_pair_gate_ignores_citations_in_nonclaim_assumptions(self):
        evidence_id = "aaaaaaaaaaaaaaaa"
        output = (
            f"Verified facts: alpha.md [EVIDENCE:{evidence_id}] is verified as the frozen local "
            f"baseline. Assumptions: Future adoption may reference [EVIDENCE:{evidence_id}] but "
            "remains unknown and requires owner validation."
        )
        self.assertTrue(evidence_filename_pairs_valid(
            output, {evidence_id: "alpha.md"},
        ))

    def test_imported_verified_facts_require_exact_source_filename(self):
        objective = (
            "Using imported evidence, define task templates, a daily review cadence, success checks, "
            "failure modes, and owner gates. Separate verified facts from assumptions. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        for cited, expected in ((False, False), (True, True)):
            with self.subTest(cited=cited), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "notes.md"
                source.write_text(
                    "Imported adoption task templates daily review cadence success checks failure modes owner gates.",
                    encoding="utf-8",
                )
                company = Company(root / "state", CitationModel(cited))
                project_id = company.create_project("Evidence")
                company.add_knowledge(source, project_id)
                job_id, _ = company.run(objective, roles=["operations", "quality"], project=project_id)
                evaluation = company.evaluate_job(job_id)
                self.assertEqual(evaluation["checks"]["verified_facts_cited"], expected)
                self.assertEqual(evaluation["passed"], expected)

    def test_job_detail_sanitizes_malformed_quality_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run("Plan local inventory", roles=["operations"])
            durable_findings = {
                "incomplete_specialist_roles": ["operations", 7, "<script>"],
                "manifest_reason": ["invalid nested value"],
                "source_conflicts": [
                    {
                        "claim": "A bounded local claim",
                        "limitation": "The capability remains pending.",
                        "source": "alpha.md",
                    },
                    "invalid conflict",
                ],
            }
            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE evaluation_history SET findings_json=? WHERE id=("
                    "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                    (json.dumps(durable_findings), job_id),
                )
                company._event(
                    db, job_id, "quality_evaluated",
                    json.dumps({
                        "incomplete_specialist_roles": "operations",
                        "source_conflicts": "invalid",
                    }),
                )
                company._event(db, job_id, "quality_evaluated", "[]")
                company._event(db, job_id, "quality_evaluated", "{malformed")

            detail = company.job_detail(job_id)
            self.assertEqual(
                detail["evaluation"]["incomplete_specialist_roles"], ["operations"],
            )
            self.assertEqual(len(detail["evaluation"]["source_conflicts"]), 1)
            self.assertIsNone(detail["evaluation"]["manifest_reason"])
            page = render_mission_detail(company, job_id)
            self.assertIn("Degraded specialist output safely withheld", page)
            self.assertIn("A bounded local claim", page)
            self.assertNotIn("&lt;script&gt;", page)

            with closing(sqlite3.connect(company.db_path)) as db, db:
                db.execute(
                    "UPDATE evaluation_history SET findings_json='[]' WHERE id=("
                    "SELECT MAX(id) FROM evaluation_history WHERE job_id=?)",
                    (job_id,),
                )
            fallback = company.job_detail(job_id)["evaluation"]
            self.assertEqual(fallback["incomplete_specialist_roles"], [])
            self.assertEqual(fallback["source_conflicts"], [])
            render_mission_detail(company, job_id)

    def test_dashboard_can_recheck_completed_job_quality(self):
        objective = (
            "Define task templates, a daily review cadence, success checks, failure modes, and owner gates. "
            "Separate verified facts from assumptions. Each specialist must use at most 100 words. "
            "The executive synthesis must use at most 180 words and end with: Owner review required."
        )
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), ConstraintModel(False))
            queue_id = company.enqueue(objective, roles=["operations", "quality"])
            _, job_id, _, passed = company.run_next_queue_item()
            self.assertFalse(passed)
            server = create_dashboard_server(company, 0, service_token="quality-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            request = urllib.request.Request(
                base + "/jobs/quality",
                data=urllib.parse.urlencode({
                    "service_token": "quality-secret", "job_id": job_id,
                }).encode(),
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with opener.open(request, timeout=3) as response:
                    body = response.read()
                self.assertIn(b"quality failed", body)
                self.assertEqual(company.recent_evaluations()[0][1], 0)
                with closing(sqlite3.connect(company.db_path)) as db:
                    detail = db.execute(
                        "SELECT detail FROM events WHERE job_id=? AND kind='quality_evaluated' "
                        "ORDER BY id DESC LIMIT 1", (job_id,),
                    ).fetchone()[0]
                self.assertFalse(json.loads(detail)["passed"])

                reset = urllib.request.Request(
                    base + "/queue/reset",
                    data=urllib.parse.urlencode({
                        "service_token": "quality-secret", "queue_id": queue_id,
                    }).encode(),
                    method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with opener.open(reset, timeout=3) as response:
                    self.assertIn(b"queued again", response.read())
                self.assertEqual(company.queue_items("queued")[0][0], queue_id)
                with closing(sqlite3.connect(company.db_path)) as db:
                    reset_detail = db.execute(
                        "SELECT detail FROM events WHERE kind='queue_reset' ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                self.assertEqual(json.loads(reset_detail)["source"], "dashboard")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_required_objective_ending_is_applied_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            company = Company(Path(tmp), MockModel())
            job_id, _ = company.run(
                "Plan inventory and end with: OWNER REVIEW REQUIRED",
                roles=["operations", "quality"],
            )
            evaluation = company.evaluate_job(job_id)
            self.assertTrue(evaluation["passed"])
            self.assertTrue(evaluation["checks"]["required_ending_present"])
            detail = company.job_detail(job_id)
            self.assertTrue(detail["job"][7].endswith("OWNER REVIEW REQUIRED"))
            self.assertTrue(any(event[0] == "objective_constraint_applied" for event in detail["events"]))


if __name__ == "__main__":
    unittest.main()
