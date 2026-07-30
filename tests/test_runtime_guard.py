import contextlib
import errno
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import runtime_guard as guard
from scripts.check_readiness import OllamaProbeError
from scripts.stamp_build_manifest import ManifestError


MODEL = "qwen3.5:0.8b"
IDENTITY = {
    "schema": "local-company.store.v1",
    "instance_id": "123e4567e89b42d3a456426614174000",
}


def _live_service_result(**extra: object) -> dict[str, object]:
    return {
        "status": "running",
        "live": True,
        "process_identity_status": "match",
        "port": 8765,
        "provider": "ollama",
        "model": MODEL,
        "num_ctx": 4096,
        "num_predict": 768,
        "keep_alive": "0s",
        **extra,
    }


def _locked_arguments(home: Path) -> dict[str, object]:
    return {
        "home": home,
        "pinned_identity": dict(IDENTITY),
        "port": 8765,
        "model": MODEL,
        "num_ctx": 4096,
        "num_predict": 768,
        "keep_alive": "0s",
        "wait_seconds": 1,
        "ollama_executable": None,
        "ollama_sha256": None,
        "allow_job_inheritance": False,
    }


def _ready_guard_result(*changes: str) -> dict[str, object]:
    return guard._payload(
        status="recovered" if changes else "ready",
        ready=True,
        components={
            "company_store": "valid",
            "disk_manifest": "valid",
            "service": "live",
            "process_identity": "match",
            "ollama": "reachable",
            "model": "installed",
            "readiness": "ready",
        },
        blockers=[],
        action="none",
        changes=list(changes),
        model=MODEL,
    )


class RuntimeGuardTests(unittest.TestCase):
    def test_preflight_rejects_arguments_and_store_changes_before_effects(self):
        effects = (
            "scripts.runtime_guard.check_project",
            "scripts.runtime_guard._probe_ollama",
            "scripts.runtime_guard._read_service",
            "scripts.runtime_guard._spawn_ollama",
            "scripts.runtime_guard.start_service",
        )
        with contextlib.ExitStack() as stack:
            spies = [stack.enter_context(patch(name)) for name in effects]
            identity_reader = stack.enter_context(
                patch("scripts.runtime_guard.read_company_identity")
            )
            with self.assertRaisesRegex(guard.GuardUsageError, "invalid runtime"):
                guard.guard_once(Path("ignored"), model="C:/private/SENTINEL")
            identity_reader.assert_not_called()
            for spy in spies:
                spy.assert_not_called()

        malformed_identities = (
            None,
            {},
            {"schema": "local-company.store.v1", "instance_id": "A" * 32},
            {**IDENTITY, "path": "C:/private/SENTINEL"},
        )
        for malformed in malformed_identities:
            with self.subTest(identity=repr(malformed)[:80]), patch(
                "scripts.runtime_guard.read_company_identity", return_value=malformed,
            ), patch("scripts.runtime_guard._runtime_guard_lock") as lock, patch(
                "scripts.runtime_guard.check_project",
            ) as manifest, patch("scripts.runtime_guard._probe_ollama") as probe, patch(
                "scripts.runtime_guard.start_service",
            ) as start:
                payload, code = guard.guard_once(Path("ignored"))
                self.assertEqual(code, 2)
                self.assertEqual(payload["blockers"], ["company_store_invalid"])
                self.assertEqual(payload["components"]["company_store"], "invalid")
                lock.assert_not_called()
                manifest.assert_not_called()
                probe.assert_not_called()
                start.assert_not_called()

        with patch(
            "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
        ), patch(
            "scripts.runtime_guard._runtime_guard_lock",
            return_value=contextlib.nullcontext(),
        ), patch(
            "scripts.runtime_guard._store_unchanged", return_value=False,
        ), patch("scripts.runtime_guard.check_project") as manifest, patch(
            "scripts.runtime_guard._probe_ollama",
        ) as probe, patch("scripts.runtime_guard._read_service") as service, patch(
            "scripts.runtime_guard._spawn_ollama",
        ) as spawn, patch("scripts.runtime_guard.start_service") as start:
            payload, code = guard.guard_once(Path("ignored"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["blockers"], ["company_store_changed"])
        manifest.assert_not_called()
        probe.assert_not_called()
        service.assert_not_called()
        spawn.assert_not_called()
        start.assert_not_called()

    def test_manifest_failure_blocks_service_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard._store_unchanged", return_value=True,
        ), patch(
            "scripts.runtime_guard.check_project",
            side_effect=ManifestError("SENTINEL C:/private/build"),
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard._read_service",
            return_value=("stale", "absent", False),
        ), patch("scripts.runtime_guard.start_service") as start:
            payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])
        self.assertIn("disk_manifest_invalid", payload["blockers"])
        self.assertNotIn("SENTINEL", json.dumps(payload))
        start.assert_not_called()

    def test_ollama_unavailable_is_refused_only_for_confirmed_connection_refusal(self):
        refused = OllamaProbeError("unavailable")
        refused.__cause__ = OSError(errno.ECONNREFUSED, "SENTINEL refused")
        with patch(
            "scripts.runtime_guard.ollama_model_installed", side_effect=refused,
        ):
            self.assertEqual(guard._probe_ollama(MODEL), ("refused", None))

        timeout = OllamaProbeError("unavailable")
        timeout.__cause__ = TimeoutError("SENTINEL timeout")
        with patch(
            "scripts.runtime_guard.ollama_model_installed", side_effect=timeout,
        ):
            self.assertEqual(guard._probe_ollama(MODEL), ("unavailable", None))

        invalid = OllamaProbeError("invalid_response")
        cases = (invalid, 1, None, "yes")
        for result in cases:
            with self.subTest(result=type(result).__name__):
                probe = Mock()
                if isinstance(result, Exception):
                    probe.side_effect = result
                else:
                    probe.return_value = result
                with patch(
                    "scripts.runtime_guard.ollama_model_installed", new=probe,
                ):
                    self.assertEqual(guard._probe_ollama(MODEL), ("invalid", None))

    def test_windows_timeout_requires_two_absent_listener_table_snapshots(self):
        timeout = TimeoutError("injected timeout")
        cases = (
            ([False, False], "refused"),
            ([False, True], "listening"),
            ([None, False], "indeterminate"),
        )
        for snapshots, expected in cases:
            with self.subTest(snapshots=snapshots), patch.object(
                guard.os, "name", "nt",
            ), patch(
                "scripts.runtime_guard.socket.create_connection", side_effect=timeout,
            ), patch(
                "scripts.runtime_guard._windows_listener_table_contains",
                side_effect=snapshots,
            ) as listener_table, patch("scripts.runtime_guard.time.sleep") as sleep:
                self.assertEqual(guard._ollama_port_state(), expected)
            self.assertEqual(listener_table.call_count, 2)
            sleep.assert_called_once_with(0.1)

        with patch.object(guard.os, "name", "posix"), patch(
            "scripts.runtime_guard.socket.create_connection", side_effect=timeout,
        ), patch(
            "scripts.runtime_guard._windows_listener_table_contains",
        ) as listener_table:
            self.assertEqual(guard._ollama_port_state(), "indeterminate")
        listener_table.assert_not_called()

    def test_ollama_spawn_requires_double_absence_and_a_confirmed_free_port(self):
        live = ("running", "match", True)
        unsafe_sequences = (
            (
                [("invalid", None), ("invalid", None), ("invalid", None)],
                "refused",
                2,
            ),
            (
                [
                    ("refused", None), ("reachable", True),
                    ("reachable", True), ("reachable", True),
                ],
                "refused",
                0,
            ),
            (
                [
                    ("refused", None), ("refused", None),
                    ("invalid", None), ("invalid", None),
                ],
                "listening",
                2,
            ),
            (
                [
                    ("refused", None), ("invalid", None),
                    ("invalid", None), ("invalid", None),
                ],
                "refused",
                2,
            ),
        )
        for probes, port_state, expected_code in unsafe_sequences:
            with self.subTest(probes=probes, port=port_state), tempfile.TemporaryDirectory() as tmp, patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama", side_effect=probes,
            ), patch(
                "scripts.runtime_guard.time.sleep",
            ), patch(
                "scripts.runtime_guard._ollama_port_state", return_value=port_state,
            ), patch(
                "scripts.runtime_guard._read_service", return_value=live,
            ), patch(
                "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
            ), patch(
                "scripts.runtime_guard._trusted_ollama_executable",
            ) as executable, patch(
                "scripts.runtime_guard._spawn_ollama",
            ) as spawn, patch("scripts.runtime_guard.start_service") as start:
                payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
            self.assertEqual(code, expected_code)
            executable.assert_not_called()
            spawn.assert_not_called()
            start.assert_not_called()
            self.assertEqual(payload["changes"], [])

        child = Mock()
        executable_path = Path("C:/trusted/ollama.exe")
        expected_sha256 = "a" * 64
        arguments = _locked_arguments(Path("ignored"))
        arguments.update({
            "ollama_executable": executable_path,
            "ollama_sha256": expected_sha256,
        })
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard._store_unchanged", return_value=True,
        ), patch(
            "scripts.runtime_guard.check_project", return_value={"status": "ok"},
        ), patch(
             "scripts.runtime_guard._probe_ollama",
             side_effect=[
                 ("unavailable", None), ("unavailable", None),
                 ("reachable", True), ("reachable", True),
             ],
        ), patch(
            "scripts.runtime_guard.time.sleep",
        ), patch(
            "scripts.runtime_guard._ollama_port_state", return_value="refused",
        ), patch(
            "scripts.runtime_guard._trusted_ollama_executable",
            return_value=executable_path,
        ), patch(
            "scripts.runtime_guard._spawn_ollama", return_value=child,
        ) as spawn, patch(
            "scripts.runtime_guard._wait_for_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard._read_service", return_value=live,
        ), patch(
            "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
        ), patch("scripts.runtime_guard.start_service") as start:
            arguments["home"] = Path(tmp)
            payload, code = guard._guard_locked(**arguments)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["changes"], ["ollama_started"])
        spawn.assert_called_once_with(
            executable_path, expected_sha256=expected_sha256,
        )
        start.assert_not_called()

    def test_confirmed_ollama_absence_requires_pin_before_launch_effects(self):
        for explicit in (True, False):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                executable = Path(tmp) / "SENTINEL-private-ollama.exe"
                arguments = _locked_arguments(Path(tmp))
                arguments["ollama_executable"] = executable if explicit else None
                with patch(
                    "scripts.runtime_guard._store_unchanged", return_value=True,
                ), patch(
                    "scripts.runtime_guard.check_project", return_value={"status": "ok"},
                ), patch(
                    "scripts.runtime_guard._probe_ollama",
                    return_value=("refused", None),
                ), patch(
                    "scripts.runtime_guard.time.sleep",
                ), patch(
                    "scripts.runtime_guard._ollama_port_state", return_value="refused",
                ), patch(
                    "scripts.runtime_guard._trusted_ollama_executable",
                ) as trusted, patch(
                    "scripts.runtime_guard._verified_executable_sha256",
                ) as verify, patch(
                    "scripts.runtime_guard._open_executable_descriptor",
                ) as open_executable, patch(
                    "scripts.runtime_guard._spawn_ollama",
                ) as spawn, patch(
                    "scripts.runtime_guard.subprocess.Popen",
                ) as popen, patch(
                    "scripts.runtime_guard._wait_for_ollama",
                ) as wait, patch(
                    "scripts.runtime_guard._read_service",
                    return_value=("stale", "absent", False),
                ), patch("scripts.runtime_guard.start_service") as start:
                    payload, code = guard._guard_locked(**arguments)

            self.assertEqual(code, 1)
            self.assertFalse(payload["ready"])
            self.assertIn("ollama_executable_pin_required", payload["blockers"])
            self.assertEqual(payload["action"], "configure_ollama_executable_pin")
            self.assertEqual(payload["changes"], [])
            trusted.assert_not_called()
            verify.assert_not_called()
            open_executable.assert_not_called()
            spawn.assert_not_called()
            popen.assert_not_called()
            wait.assert_not_called()
            start.assert_not_called()
            rendered = guard._render_result(payload).decode("utf-8")
            self.assertNotIn("SENTINEL", rendered)
            self.assertNotIn("private", rendered.lower())
            self.assertLess(len(rendered.encode("utf-8")), 2048)

    def test_failed_owned_ollama_child_is_cleaned_and_cleanup_failure_fails_closed(self):
        child = Mock()
        executable_path = Path("C:/trusted/ollama.exe")
        expected_sha256 = "b" * 64
        for cleaned in (True, False):
            arguments = _locked_arguments(Path("ignored"))
            arguments.update({
                "ollama_executable": executable_path,
                "ollama_sha256": expected_sha256,
            })
            with self.subTest(cleaned=cleaned), tempfile.TemporaryDirectory() as tmp, patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama", return_value=("refused", None),
            ), patch(
                "scripts.runtime_guard.time.sleep",
            ), patch(
                "scripts.runtime_guard._ollama_port_state", return_value="refused",
            ), patch(
                "scripts.runtime_guard._trusted_ollama_executable",
                return_value=executable_path,
            ), patch(
                "scripts.runtime_guard._spawn_ollama", return_value=child,
            ), patch(
                "scripts.runtime_guard._wait_for_ollama", return_value=("refused", None),
            ), patch(
                "scripts.runtime_guard._terminate_owned_child", return_value=cleaned,
            ) as cleanup, patch(
                "scripts.runtime_guard._read_service",
                return_value=("stale", "absent", False),
            ), patch("scripts.runtime_guard.start_service") as start:
                arguments["home"] = Path(tmp)
                payload, code = guard._guard_locked(**arguments)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ready"])
            self.assertNotIn("ollama_started", payload["changes"])
            cleanup.assert_called_once_with(child)
            if cleaned:
                self.assertIn("ollama_start_unconfirmed", payload["blockers"])
                self.assertNotIn("ollama_cleanup_failed", payload["blockers"])
            else:
                self.assertIn("ollama_cleanup_failed", payload["blockers"])
            start.assert_not_called()

    def test_missing_model_never_pulls_runs_a_mission_or_starts_service(self):
        forbidden = AssertionError("forbidden runtime effect")
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(patch(
                "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
            ))
            stack.enter_context(patch(
                "scripts.runtime_guard._runtime_guard_lock",
                return_value=contextlib.nullcontext(),
            ))
            stack.enter_context(patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ))
            stack.enter_context(patch(
                "scripts.runtime_guard._probe_ollama", return_value=("reachable", False),
            ))
            stack.enter_context(patch(
                "scripts.runtime_guard._read_service",
                return_value=("stale", "absent", False),
            ))
            start = stack.enter_context(patch("scripts.runtime_guard.start_service"))
            spawn = stack.enter_context(patch("scripts.runtime_guard._spawn_ollama"))
            for target in (
                "scripts.runtime_guard.subprocess.Popen",
                "scripts.runtime_guard.subprocess.run",
                "scripts.runtime_guard.subprocess.check_call",
                "scripts.runtime_guard.subprocess.check_output",
                "scripts.runtime_guard.os.system",
                "scripts.runtime_guard.os.kill",
                "local_company.core.Company.run",
                "local_company.core.Company.run_next_queue_item",
                "local_company.core.OllamaModel.complete",
            ):
                stack.enter_context(patch(target, side_effect=forbidden))
            payload, code = guard.guard_once(Path(tmp))
        self.assertEqual(code, 1)
        self.assertEqual(payload["action"], "install_configured_model")
        self.assertIn("model_not_installed", payload["blockers"])
        self.assertEqual(payload["models_pulled"], 0)
        self.assertEqual(payload["missions_started"], 0)
        start.assert_not_called()
        spawn.assert_not_called()

    def test_safe_service_states_start_once_only_with_consistent_identity(self):
        safe_states = (
            ("not_configured", "absent"),
            ("stale", "absent"),
            ("stale_pid_reused", "mismatch"),
            ("stopped", "absent"),
            ("stopped", "mismatch"),
            ("failed", "absent"),
            ("failed", "mismatch"),
        )
        for down in safe_states:
            with self.subTest(state=down), tempfile.TemporaryDirectory() as tmp, patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
            ), patch(
                "scripts.runtime_guard._read_service",
                side_effect=[
                    (*down, False), ("running", "match", True),
                    ("running", "match", True),
                ],
            ), patch(
                "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
            ), patch(
                "scripts.runtime_guard.start_service",
                return_value=_live_service_result(),
            ) as start:
                payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
            self.assertEqual(code, 0)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["status"], "recovered")
            self.assertEqual(payload["changes"], ["service_started"])
            start.assert_called_once_with(
                Path(tmp), port=8765, provider="ollama", model=MODEL,
                num_ctx=4096, num_predict=768, keep_alive="0s",
            )

    def test_full_readiness_vetoes_live_mixed_release_and_is_followed_by_exact_rechecks(self):
        events: list[str] = []

        def store_unchanged(home: Path, pinned: dict[str, str]) -> bool:
            self.assertEqual(pinned, IDENTITY)
            events.append("store")
            return True

        def read_service(home: Path, **_: object) -> tuple[str, str, bool]:
            events.append("service")
            return "running", "match", True

        def full_readiness(home: Path, model: str) -> tuple[str, str]:
            self.assertEqual(model, MODEL)
            events.append("full_readiness")
            return "action_required", "restart_local_service_manually"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard._store_unchanged", side_effect=store_unchanged,
        ), patch(
            "scripts.runtime_guard.check_project", return_value={
                "status": "ok", "build_id": "local-disk-release",
            },
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard._read_service", side_effect=read_service,
        ), patch(
            "scripts.runtime_guard._full_readiness", side_effect=full_readiness,
        ) as readiness, patch("scripts.runtime_guard.start_service") as start:
            payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))

        self.assertEqual(code, 1)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "action_required")
        self.assertEqual(payload["components"]["service"], "live")
        self.assertEqual(payload["components"]["disk_manifest"], "valid")
        self.assertEqual(payload["components"]["readiness"], "action_required")
        self.assertEqual(payload["blockers"], ["full_readiness_action_required"])
        self.assertEqual(payload["action"], "restart_local_service_manually")
        self.assertEqual(payload["changes"], [])
        readiness.assert_called_once_with(Path(tmp), MODEL)
        start.assert_not_called()
        self.assertEqual(events.count("service"), 3)
        self.assertEqual(events.count("store"), 3)
        gate = events.index("full_readiness")
        self.assertEqual(events[gate + 1:], ["service", "store"])

    def test_transient_missing_model_or_service_start_failure_leaves_no_stale_blocker(self):
        live = ("running", "match", True)
        cases = ("missing_model", "service_start_failure")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                stack.enter_context(patch(
                    "scripts.runtime_guard._store_unchanged", return_value=True,
                ))
                stack.enter_context(patch(
                    "scripts.runtime_guard.check_project", return_value={"status": "ok"},
                ))
                stack.enter_context(patch(
                    "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
                ))
                if case == "missing_model":
                    stack.enter_context(patch(
                        "scripts.runtime_guard._probe_ollama",
                        side_effect=[
                            ("reachable", False), ("reachable", True),
                            ("reachable", True),
                        ],
                    ))
                    stack.enter_context(patch(
                        "scripts.runtime_guard._read_service", return_value=live,
                    ))
                    start = stack.enter_context(patch(
                        "scripts.runtime_guard.start_service",
                    ))
                else:
                    stack.enter_context(patch(
                        "scripts.runtime_guard._probe_ollama",
                        return_value=("reachable", True),
                    ))
                    stack.enter_context(patch(
                        "scripts.runtime_guard._read_service",
                        side_effect=[
                            ("stale", "absent", False),
                            ("stale", "absent", False),
                            live,
                            live,
                        ],
                    ))
                    start = stack.enter_context(patch(
                        "scripts.runtime_guard.start_service",
                        side_effect=RuntimeError("SENTINEL transient start race"),
                    ))
                payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))

            self.assertEqual(code, 0)
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["blockers"], [])
            self.assertEqual(payload["changes"], [])
            self.assertEqual(payload["components"]["model"], "installed")
            self.assertEqual(payload["components"]["service"], "live")
            self.assertEqual(payload["components"]["readiness"], "ready")
            self.assertNotIn("model_not_installed", json.dumps(payload))
            self.assertNotIn("service_start_failed", json.dumps(payload))
            self.assertNotIn("SENTINEL", json.dumps(payload))
            if case == "missing_model":
                start.assert_not_called()
            else:
                start.assert_called_once()

    def test_raw_service_status_contradictions_are_classified_fail_closed(self):
        malformed = (
            {"status": "running", "live": False, "process_identity_status": "match"},
            {"status": "stale", "live": False, "process_identity_status": "mismatch"},
            {
                "status": "stale_pid_reused", "live": False,
                "process_identity_status": "absent",
            },
            {
                "status": "not_configured", "live": False,
                "process_identity_status": "legacy",
            },
            {"status": "unknown", "live": False, "process_identity_status": "absent"},
            {"status": "running", "live": "yes", "process_identity_status": "match"},
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                status, relation, live = guard._service_components(
                    raw, port=8765, model=MODEL, num_ctx=4096,
                    num_predict=768, keep_alive="0s",
                )
                self.assertEqual(status, "invalid")
                self.assertFalse(live)
                self.assertFalse(guard._safe_service_start(status, relation))

        wrong_configuration = _live_service_result(port=True)
        self.assertEqual(
            guard._service_components(
                wrong_configuration, port=8765, model=MODEL, num_ctx=4096,
                num_predict=768, keep_alive="0s",
            ),
            ("configuration_mismatch", "match", False),
        )

    def test_unsafe_or_contradictory_service_states_never_start(self):
        unsafe_states = (
            ("running", "match", False),
            ("unreachable", "match", False),
            ("endpoint_mismatch", "match", False),
            ("identity_indeterminate", "unavailable", False),
            ("identity_conflict", "match", False),
            ("legacy_unverified", "legacy", False),
            ("stopped", "legacy", False),
            ("failed", "unavailable", False),
            ("stale", "mismatch", False),
            ("stale_pid_reused", "absent", False),
            ("invalid", "unknown", False),
        )
        for service in unsafe_states:
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp, patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
            ), patch(
                "scripts.runtime_guard._read_service", return_value=service,
            ), patch("scripts.runtime_guard.start_service") as start:
                payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
            self.assertFalse(payload["ready"])
            self.assertNotEqual(code, 0)
            self.assertNotEqual(payload["action"], "none")
            self.assertNotIn("service_started", payload["changes"])
            start.assert_not_called()

    def test_failed_ollama_wait_never_terminates_or_kills_detached_child(self):
        process = Mock()
        process.poll.return_value = None
        with patch(
            "scripts.runtime_guard._probe_ollama", return_value=("refused", None),
        ), patch(
            "scripts.runtime_guard.time.monotonic", side_effect=[0.0, 1.1],
        ), patch("scripts.runtime_guard.time.sleep") as sleep:
            result = guard._wait_for_ollama(MODEL, process, 1)
        self.assertEqual(result, ("refused", None))
        process.poll.assert_called_once_with()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_not_called()
        sleep.assert_not_called()

    def test_executable_sha256_is_bounded_stable_and_returns_identity_signature(self):
        content = b"\x00ollama-binary\xff\r\n"
        expected = hashlib.sha256(content).hexdigest()
        read_sizes: list[int] = []
        real_fdopen = os.fdopen

        class ReadSpy:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def fileno(self):
                return self.handle.fileno()

            def read(self, size: int):
                read_sizes.append(size)
                return self.handle.read(size)

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "ollama.exe"
            executable.write_bytes(content)
            metadata = executable.stat()
            expected_identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            with patch.object(
                guard, "MAX_OLLAMA_EXECUTABLE_BYTES", len(content),
            ), patch(
                "scripts.runtime_guard.os.fdopen",
                side_effect=lambda descriptor, mode: ReadSpy(
                    real_fdopen(descriptor, mode),
                ),
            ):
                first = guard._verify_executable_sha256(executable, expected)
                second = guard._verify_executable_sha256(executable, expected)

            self.assertEqual(second, first)
            self.assertEqual(len(first), 6)
            self.assertTrue(stat.S_ISREG(first[2]))
            self.assertEqual(
                (first[0], first[1], first[3], first[4], first[5]),
                expected_identity,
            )
            self.assertTrue(read_sizes)
            self.assertEqual(
                set(read_sizes), {guard.OLLAMA_EXECUTABLE_HASH_CHUNK_BYTES},
            )

            with patch.object(
                guard, "MAX_OLLAMA_EXECUTABLE_BYTES", len(content) - 1,
            ), self.assertRaises(guard.GuardExecutableError):
                guard._verify_executable_sha256(executable, expected)

    def test_executable_sha256_rejects_a_changed_open_file_signature(self):
        content = b"stable executable bytes"
        expected = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "ollama.exe"
            executable.write_bytes(content)
            opened = executable.stat()
            changed = Mock(
                st_dev=opened.st_dev,
                st_ino=opened.st_ino,
                st_mode=opened.st_mode,
                st_nlink=opened.st_nlink,
                st_size=opened.st_size,
                st_mtime_ns=opened.st_mtime_ns + 1,
            )
            with patch(
                "scripts.runtime_guard.os.fstat", side_effect=[opened, changed],
            ), self.assertRaises(guard.GuardExecutableError):
                guard._verify_executable_sha256(executable, expected)

    def test_executable_sha256_rejects_reparse_metadata_at_each_checkpoint(self):
        content = b"trusted executable bytes"
        expected = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "ollama.exe"
            executable.write_bytes(content)
            actual = executable.stat()

            def metadata():
                return Mock(
                    st_dev=actual.st_dev,
                    st_ino=actual.st_ino,
                    st_mode=actual.st_mode,
                    st_nlink=actual.st_nlink,
                    st_size=actual.st_size,
                    st_mtime_ns=actual.st_mtime_ns,
                )

            for checkpoint in ("before", "opened", "current"):
                before, opened, after, current = (
                    metadata(), metadata(), metadata(), metadata(),
                )
                targeted = {
                    "before": before, "opened": opened, "current": current,
                }[checkpoint]

                def open_descriptor(_path):
                    return os.open(
                        executable,
                        os.O_RDONLY | getattr(os, "O_BINARY", 0),
                    )

                with self.subTest(checkpoint=checkpoint), patch(
                    "scripts.runtime_guard.os.stat",
                    side_effect=[before, current],
                ), patch(
                    "scripts.runtime_guard.os.fstat",
                    side_effect=[opened, after],
                ), patch(
                    "scripts.runtime_guard._open_executable_descriptor",
                    side_effect=open_descriptor,
                ), patch(
                    "scripts.runtime_guard._is_link_or_reparse_metadata",
                    side_effect=lambda item, target=targeted: item is target,
                ) as is_reparse, self.assertRaises(guard.GuardExecutableError):
                    guard._verify_executable_sha256(executable, expected)
                self.assertTrue(any(
                    call.args[0] is targeted for call in is_reparse.call_args_list
                ))

    def test_ollama_sha256_mismatch_fails_closed_before_process_spawn(self):
        expected = "0" * 64
        actual = hashlib.sha256(b"installed ollama bytes").hexdigest()
        child = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "SENTINEL-private-ollama.exe"
            executable.write_bytes(b"installed ollama bytes")
            arguments = _locked_arguments(Path(tmp))
            arguments.update({
                "ollama_executable": executable,
                "ollama_sha256": expected,
            })
            with patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama", return_value=("refused", None),
            ), patch(
                "scripts.runtime_guard.time.sleep",
            ), patch(
                "scripts.runtime_guard._ollama_port_state", return_value="refused",
            ), patch(
                "scripts.runtime_guard._trusted_ollama_executable",
                return_value=executable,
            ), patch(
                "scripts.runtime_guard.subprocess.Popen", return_value=child,
            ) as popen, patch(
                "scripts.runtime_guard._wait_for_ollama",
            ) as wait, patch(
                "scripts.runtime_guard._read_service",
                return_value=("stale", "absent", False),
            ), patch("scripts.runtime_guard.start_service") as start:
                payload, code = guard._guard_locked(**arguments)

        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])
        self.assertIn("ollama_executable_hash_mismatch", payload["blockers"])
        self.assertEqual(payload["changes"], [])
        popen.assert_not_called()
        wait.assert_not_called()
        start.assert_not_called()
        rendered = guard._render_result(payload).decode("utf-8")
        for forbidden in (
            "SENTINEL", "private", str(executable), expected, actual,
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertLess(len(rendered.encode("utf-8")), 2048)

    def test_matching_ollama_sha256_is_forwarded_only_at_spawn_boundary(self):
        executable = Path("C:/trusted/ollama.exe")
        expected = "a" * 64
        child = Mock()
        live = ("running", "match", True)
        with tempfile.TemporaryDirectory() as tmp:
            arguments = _locked_arguments(Path(tmp))
            arguments.update({
                "ollama_executable": executable,
                "ollama_sha256": expected,
            })
            with patch(
                "scripts.runtime_guard._store_unchanged", return_value=True,
            ), patch(
                "scripts.runtime_guard.check_project", return_value={"status": "ok"},
            ), patch(
                "scripts.runtime_guard._probe_ollama",
                side_effect=[
                    ("unavailable", None), ("unavailable", None),
                    ("reachable", True), ("reachable", True),
                ],
            ), patch(
                "scripts.runtime_guard.time.sleep",
            ), patch(
                "scripts.runtime_guard._ollama_port_state", return_value="refused",
            ), patch(
                "scripts.runtime_guard._trusted_ollama_executable",
                return_value=executable,
            ), patch(
                "scripts.runtime_guard._spawn_ollama", return_value=child,
            ) as spawn, patch(
                "scripts.runtime_guard._wait_for_ollama", return_value=("reachable", True),
            ), patch(
                "scripts.runtime_guard._read_service", return_value=live,
            ), patch(
                "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
            ), patch("scripts.runtime_guard.start_service") as start:
                payload, code = guard._guard_locked(**arguments)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["changes"], ["ollama_started"])
        spawn.assert_called_once_with(executable, expected_sha256=expected)
        start.assert_not_called()

    def test_reachable_ollama_never_hashes_or_opens_the_pinned_executable(self):
        executable = Path("C:/trusted/ollama.exe")
        arguments = _locked_arguments(Path("C:/company"))
        arguments.update({
            "ollama_executable": executable,
            "ollama_sha256": "a" * 64,
        })
        with patch(
            "scripts.runtime_guard._store_unchanged", return_value=True,
        ), patch(
            "scripts.runtime_guard.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard._read_service",
            return_value=("running", "match", True),
        ), patch(
            "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
        ), patch(
            "scripts.runtime_guard._trusted_ollama_executable",
        ) as trusted, patch(
            "scripts.runtime_guard._verified_executable_sha256",
        ) as verify, patch(
            "scripts.runtime_guard._open_executable_descriptor",
        ) as open_executable, patch(
            "scripts.runtime_guard.subprocess.Popen",
        ) as popen:
            payload, code = guard._guard_locked(**arguments)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        trusted.assert_not_called()
        verify.assert_not_called()
        open_executable.assert_not_called()
        popen.assert_not_called()

    def test_unpinned_healthy_runtime_remains_ready_and_observation_only(self):
        executable = Path("C:/trusted/ollama.exe")
        arguments = _locked_arguments(Path("C:/company"))
        arguments["ollama_executable"] = executable
        with patch(
            "scripts.runtime_guard._store_unchanged", return_value=True,
        ), patch(
            "scripts.runtime_guard.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard._read_service",
            return_value=("running", "match", True),
        ), patch(
            "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
        ), patch(
            "scripts.runtime_guard._trusted_ollama_executable",
        ) as trusted, patch(
            "scripts.runtime_guard._verified_executable_sha256",
        ) as verify, patch(
            "scripts.runtime_guard._open_executable_descriptor",
        ) as open_executable, patch(
            "scripts.runtime_guard._spawn_ollama",
        ) as spawn, patch(
            "scripts.runtime_guard.subprocess.Popen",
        ) as popen:
            payload, code = guard._guard_locked(**arguments)

        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["action"], "none")
        trusted.assert_not_called()
        verify.assert_not_called()
        open_executable.assert_not_called()
        spawn.assert_not_called()
        popen.assert_not_called()

    def test_pinned_spawn_rejects_a_path_swap_before_popen(self):
        executable = Path("C:/trusted/ollama.exe")
        expected = "a" * 64
        signature = (1, 2, stat.S_IFREG, 1, 3, 4)
        verification = contextlib.nullcontext(signature)
        with patch.object(guard.os, "name", "posix"), patch(
            "scripts.runtime_guard._verified_executable_sha256",
            return_value=verification,
        ) as verify, patch(
            "scripts.runtime_guard._assert_executable_signature",
            side_effect=guard.GuardExecutableError("path changed"),
        ) as assert_signature, patch(
            "scripts.runtime_guard.subprocess.Popen",
        ) as popen:
            with self.assertRaises(guard.GuardExecutableError):
                guard._spawn_ollama(executable, expected_sha256=expected)

        verify.assert_called_once_with(executable, expected)
        assert_signature.assert_called_once_with(executable, signature)
        popen.assert_not_called()

    def test_pinned_spawn_cleans_a_child_after_post_launch_lease_oserror(self):
        executable = Path("C:/trusted/ollama.exe")
        expected = "c" * 64
        signature = (1, 2, stat.S_IFREG, 1, 3, 4)
        secret = "SENTINEL-C:/private/post-launch-lease"

        @contextlib.contextmanager
        def failed_verification():
            yield signature
            raise OSError(secret)

        for cleaned in (True, False):
            child = Mock()
            with self.subTest(cleaned=cleaned), patch.object(
                guard.os, "name", "posix",
            ), patch(
                "scripts.runtime_guard._verified_executable_sha256",
                side_effect=lambda *_args: failed_verification(),
            ) as verify, patch(
                "scripts.runtime_guard._assert_executable_signature",
            ) as assert_signature, patch(
                "scripts.runtime_guard.subprocess.Popen", return_value=child,
            ) as popen, patch(
                "scripts.runtime_guard._terminate_owned_child", return_value=cleaned,
            ) as cleanup:
                with self.assertRaises(guard.GuardExecutableError) as raised:
                    guard._spawn_ollama(executable, expected_sha256=expected)

            self.assertEqual(
                isinstance(raised.exception, guard.GuardExecutableCleanupError),
                not cleaned,
            )
            self.assertNotIn("SENTINEL", str(raised.exception))
            verify.assert_called_once_with(executable, expected)
            assert_signature.assert_called_once_with(executable, signature)
            popen.assert_called_once()
            cleanup.assert_called_once_with(child)

    def test_locked_guard_surfaces_failed_pinned_launch_cleanup_without_leaking(self):
        executable = Path("C:/private/SENTINEL-ollama.exe")
        expected = "d" * 64
        secret = "SENTINEL-C:/private/post-launch-cleanup"
        arguments = _locked_arguments(Path("C:/company"))
        arguments.update({
            "ollama_executable": executable,
            "ollama_sha256": expected,
        })
        with patch(
            "scripts.runtime_guard._store_unchanged", return_value=True,
        ), patch(
            "scripts.runtime_guard.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("refused", None),
        ), patch(
            "scripts.runtime_guard.time.sleep",
        ), patch(
            "scripts.runtime_guard._ollama_port_state", return_value="refused",
        ), patch(
            "scripts.runtime_guard._trusted_ollama_executable",
            return_value=executable,
        ), patch(
            "scripts.runtime_guard._spawn_ollama",
            side_effect=guard.GuardExecutableCleanupError(secret),
        ), patch(
            "scripts.runtime_guard._wait_for_ollama",
        ) as wait, patch(
            "scripts.runtime_guard._read_service",
            return_value=("stale", "absent", False),
        ), patch("scripts.runtime_guard.start_service") as start:
            payload, code = guard._guard_locked(**arguments)

        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])
        self.assertIn("ollama_executable_invalid", payload["blockers"])
        self.assertIn("ollama_cleanup_failed", payload["blockers"])
        self.assertEqual(payload["action"], "inspect_ollama_service")
        self.assertEqual(payload["changes"], [])
        wait.assert_not_called()
        start.assert_not_called()
        rendered = guard._render_result(payload).decode("utf-8")
        for forbidden in ("SENTINEL", "private", str(executable), expected, secret):
            self.assertNotIn(forbidden, rendered)
        self.assertLess(len(rendered.encode("utf-8")), 2048)

    def test_pinned_windows_retry_keeps_verification_active_and_sets_executable(self):
        executable = Path("C:/trusted/ollama.exe")
        expected = "b" * 64
        signature = (1, 2, stat.S_IFREG, 1, 3, 4)
        child = Mock()

        class VerificationContext:
            active = False

            def __enter__(self):
                self.active = True
                return signature

            def __exit__(self, *args):
                self.active = False

        verification = VerificationContext()
        launch_states: list[bool] = []
        launch_kwargs: list[dict[str, object]] = []
        denied = PermissionError(13, "scheduler denied breakaway")
        denied.winerror = 5

        def launch(_argv, **kwargs):
            launch_states.append(verification.active)
            launch_kwargs.append(kwargs)
            if len(launch_states) == 1:
                raise denied
            return child

        with patch.object(guard.os, "name", "nt"), patch.object(
            guard.subprocess, "DETACHED_PROCESS", 0x01, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x02, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x04, create=True,
        ), patch(
            "scripts.runtime_guard._verified_executable_sha256",
            return_value=verification,
        ) as verify, patch(
            "scripts.runtime_guard._assert_executable_signature",
        ) as assert_signature, patch(
            "scripts.runtime_guard.subprocess.Popen", side_effect=launch,
        ) as popen:
            result = guard._spawn_ollama(
                executable, expected_sha256=expected,
                allow_job_inheritance=True,
            )

        self.assertIs(result, child)
        self.assertFalse(verification.active)
        self.assertEqual(launch_states, [True, True])
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(
            [kwargs["executable"] for kwargs in launch_kwargs],
            [str(executable), str(executable)],
        )
        self.assertEqual(
            [call.args[0] for call in popen.call_args_list],
            [[str(executable), "serve"], [str(executable), "serve"]],
        )
        verify.assert_called_once_with(executable, expected)
        self.assertEqual(assert_signature.call_count, 2)
        for call in assert_signature.call_args_list:
            self.assertEqual(call.args, (executable, signature))

    def test_windows_spawn_uses_exact_serve_command_and_breakaway_flags(self):
        executable = Path("C:/trusted/ollama.exe")
        expected = "e" * 64
        signature = (1, 2, stat.S_IFREG, 1, 3, 4)
        child = Mock()
        with patch.object(guard.os, "name", "nt"), patch.object(
            guard.subprocess, "DETACHED_PROCESS", 0x01, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x02, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x04, create=True,
        ), patch(
            "scripts.runtime_guard._verified_executable_sha256",
            return_value=contextlib.nullcontext(signature),
        ) as verify, patch(
            "scripts.runtime_guard._assert_executable_signature",
        ) as assert_signature, patch(
            "scripts.runtime_guard.subprocess.Popen", return_value=child,
        ) as popen:
            self.assertIs(guard._spawn_ollama(
                executable, expected_sha256=expected,
            ), child)
        popen.assert_called_once()
        verify.assert_called_once_with(executable, expected)
        assert_signature.assert_called_once_with(executable, signature)
        args, kwargs = popen.call_args
        self.assertEqual(args, ([str(executable), "serve"],))
        self.assertEqual(kwargs["cwd"], executable.parent)
        self.assertEqual(kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["creationflags"], 0x07)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("start_new_session", kwargs)

        with patch.object(guard.os, "name", "nt"), patch.object(
            guard.subprocess, "CREATE_BREAKAWAY_FROM_JOB", None,
        ), patch(
            "scripts.runtime_guard.subprocess.Popen",
        ) as unavailable_popen:
            with self.assertRaisesRegex(RuntimeError, "creation flags"):
                guard._spawn_ollama(executable, expected_sha256=expected)
        unavailable_popen.assert_not_called()

        denied = PermissionError(13, "scheduler denied breakaway")
        denied.winerror = 5
        with patch.object(guard.os, "name", "nt"), patch.object(
            guard.subprocess, "DETACHED_PROCESS", 0x01, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x02, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x04, create=True,
        ), patch(
            "scripts.runtime_guard._verified_executable_sha256",
            return_value=contextlib.nullcontext(signature),
        ), patch(
            "scripts.runtime_guard._assert_executable_signature",
        ), patch(
            "scripts.runtime_guard.subprocess.Popen",
            side_effect=[denied, child],
        ) as inherited_popen:
            result = guard._spawn_ollama(
                executable, expected_sha256=expected,
                allow_job_inheritance=True,
            )
        self.assertIs(result, child)
        self.assertEqual(inherited_popen.call_count, 2)
        self.assertEqual(inherited_popen.call_args_list[0].kwargs["creationflags"], 0x07)
        self.assertEqual(inherited_popen.call_args_list[1].kwargs["creationflags"], 0x03)

        denied_again = PermissionError(13, "scheduler denied breakaway")
        denied_again.winerror = 5
        with patch.object(guard.os, "name", "nt"), patch(
            "scripts.runtime_guard._verified_executable_sha256",
            return_value=contextlib.nullcontext(signature),
        ), patch(
            "scripts.runtime_guard._assert_executable_signature",
        ), patch(
            "scripts.runtime_guard.subprocess.Popen", side_effect=denied_again,
        ) as strict_popen:
            with self.assertRaises(PermissionError):
                guard._spawn_ollama(executable, expected_sha256=expected)
        strict_popen.assert_called_once()

    def test_opt_in_result_journal_is_fixed_matches_stdout_and_uses_mode_0600(self):
        result = _ready_guard_result()
        real_open = os.open
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        with patch(
            "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
        ), patch(
            "scripts.runtime_guard._guard_locked", return_value=(result, 0),
        ), patch(
            "scripts.runtime_guard.os.open", wraps=real_open,
        ) as open_spy, contextlib.redirect_stdout(
            stdout := io.StringIO()
        ), contextlib.redirect_stderr(stderr := io.StringIO()):
            code = guard.main(["--home", temporary.name, "--record-result"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        target = home / guard.RESULT_JOURNAL_NAME
        self.assertEqual(target.name, "runtime-guard-last.json")
        self.assertEqual(target.read_bytes(), stdout.getvalue().encode("utf-8"))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), result)
        metadata = target.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(metadata.st_nlink, 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        temporary_opens = [
            call for call in open_spy.call_args_list
            if Path(call.args[0]).name.startswith(
                f".{guard.RESULT_JOURNAL_NAME}.",
            )
        ]
        self.assertEqual(len(temporary_opens), 1)
        self.assertEqual(temporary_opens[0].args[2], 0o600)
        self.assertEqual(list(home.glob("runtime-guard-*.json")), [target])
        self.assertEqual(list(home.glob(f".{guard.RESULT_JOURNAL_NAME}.*.tmp")), [])

    def test_binary_stdout_is_lf_only_and_exactly_matches_recorded_journal(self):
        child_source = """
import contextlib
import sys
from pathlib import Path
from scripts import runtime_guard as guard

home = Path(sys.argv[1])
identity = {
    "schema": "local-company.store.v1",
    "instance_id": "123e4567e89b42d3a456426614174000",
}
result = guard._payload(
    status="ready",
    ready=True,
    components={
        "company_store": "valid",
        "disk_manifest": "valid",
        "service": "live",
        "process_identity": "match",
        "ollama": "reachable",
        "model": "installed",
        "readiness": "ready",
    },
    blockers=[],
    action="none",
    changes=[],
    model="qwen3.5:0.8b",
)
guard.read_company_identity = lambda selected_home: identity
guard._runtime_guard_lock = lambda selected_home: contextlib.nullcontext()
guard._guard_locked = lambda *args, **kwargs: (result, 0)
raise SystemExit(guard.main([
    "--home", str(home), "--record-result", "--wait-seconds", "1",
]))
"""
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, "-c", child_source, tmp],
                cwd=guard.PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            journal = Path(tmp) / guard.RESULT_JOURNAL_NAME
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(
                "utf-8", errors="replace",
            ))
            self.assertEqual(completed.stderr, b"")
            self.assertEqual(completed.stdout.count(b"\n"), 1)
            self.assertTrue(completed.stdout.endswith(b"\n"))
            self.assertNotIn(b"\r", completed.stdout)
            self.assertEqual(completed.stdout, journal.read_bytes())
            payload = json.loads(completed.stdout.decode("utf-8", errors="strict"))
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["status"], "ready")

    def test_default_and_untrusted_outcomes_do_not_write_result_journal(self):
        result = _ready_guard_result()
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
        ), patch(
            "scripts.runtime_guard._runtime_guard_lock",
            return_value=contextlib.nullcontext(),
        ), patch(
            "scripts.runtime_guard._guard_locked", return_value=(result, 0),
        ), patch("scripts.runtime_guard._write_result_journal") as journal:
            payload, code = guard.guard_once(Path(tmp))
        self.assertEqual(code, 0)
        self.assertEqual(payload, result)
        journal.assert_not_called()
        self.assertFalse((Path(tmp) / guard.RESULT_JOURNAL_NAME).exists())

        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard.read_company_identity", return_value=None,
        ), patch("scripts.runtime_guard._runtime_guard_lock") as lock, patch(
            "scripts.runtime_guard._write_result_journal",
        ) as journal:
            payload, code = guard.guard_once(Path(tmp), record_result=True)
        self.assertEqual(code, 2)
        self.assertEqual(payload["blockers"], ["company_store_invalid"])
        lock.assert_not_called()
        journal.assert_not_called()
        self.assertFalse((Path(tmp) / guard.RESULT_JOURNAL_NAME).exists())

        lifecycle_failures = (
            (guard.GuardBusyError("SENTINEL busy"), 1, "runtime_guard_busy"),
            (guard.GuardLockError("SENTINEL lock"), 2, "runtime_guard_lock_invalid"),
        )
        for failure, expected_code, blocker in lifecycle_failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp, patch(
                "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
            ), patch(
                "scripts.runtime_guard._runtime_guard_lock", side_effect=failure,
            ), patch("scripts.runtime_guard._write_result_journal") as journal:
                payload, code = guard.guard_once(Path(tmp), record_result=True)
            self.assertEqual(code, expected_code)
            self.assertEqual(payload["blockers"], [blocker])
            journal.assert_not_called()
            self.assertFalse((Path(tmp) / guard.RESULT_JOURNAL_NAME).exists())

        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
        ), patch(
            "scripts.runtime_guard._runtime_guard_lock",
            return_value=contextlib.nullcontext(),
        ), patch(
            "scripts.runtime_guard._guard_locked", return_value=(result, 0),
        ), patch(
            "scripts.runtime_guard._store_unchanged", return_value=False,
        ), patch("scripts.runtime_guard._write_result_journal") as journal:
            payload, code = guard.guard_once(Path(tmp), record_result=True)
        self.assertEqual(code, 2)
        self.assertEqual(payload["blockers"], ["company_store_changed"])
        self.assertEqual(payload["components"]["company_store"], "changed")
        journal.assert_not_called()
        self.assertFalse((Path(tmp) / guard.RESULT_JOURNAL_NAME).exists())

    def test_atomic_replace_failure_preserves_old_journal_and_completed_changes(self):
        result = _ready_guard_result("ollama_started", "service_started")
        old_bytes = b'{"old":"journal-sentinel"}\n'
        failure = OSError("SENTINEL C:/private/replace failure")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / guard.RESULT_JOURNAL_NAME
            target.write_bytes(old_bytes)
            with patch(
                "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
            ), patch(
                "scripts.runtime_guard._guard_locked", return_value=(result, 0),
            ), patch(
                "scripts.runtime_guard.os.replace", side_effect=failure,
            ), contextlib.redirect_stdout(
                stdout := io.StringIO()
            ), contextlib.redirect_stderr(stderr := io.StringIO()):
                code = guard.main(["--home", tmp, "--record-result"])

            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["status"], "indeterminate")
            self.assertEqual(payload["blockers"], ["result_journal_write_failed"])
            self.assertEqual(payload["action"], "inspect_runtime_guard")
            self.assertEqual(
                payload["changes"], ["ollama_started", "service_started"],
            )
            self.assertEqual(target.read_bytes(), old_bytes)
            self.assertEqual(
                list(home.glob(f".{guard.RESULT_JOURNAL_NAME}.*.tmp")), [],
            )
            self.assertNotIn("SENTINEL", stdout.getvalue())
            self.assertNotIn("private", stdout.getvalue().lower())
            self.assertLess(len(stdout.getvalue().encode("utf-8")), 2048)

    def test_hardlinked_result_destination_is_refused_without_write_through(self):
        result = _ready_guard_result("service_started")
        old_bytes = b"external-journal-sentinel\n"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            external = home / "external-sentinel.bin"
            target = home / guard.RESULT_JOURNAL_NAME
            external.write_bytes(old_bytes)
            try:
                os.link(external, target)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            self.assertEqual(target.stat().st_nlink, 2)
            with patch(
                "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
            ), patch(
                "scripts.runtime_guard._guard_locked", return_value=(result, 0),
            ), contextlib.redirect_stdout(
                stdout := io.StringIO()
            ), contextlib.redirect_stderr(stderr := io.StringIO()):
                code = guard.main(["--home", tmp, "--record-result"])

            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["blockers"], ["result_journal_write_failed"])
            self.assertEqual(payload["changes"], ["service_started"])
            self.assertEqual(external.read_bytes(), old_bytes)
            self.assertEqual(target.read_bytes(), old_bytes)
            self.assertTrue(os.path.samefile(external, target))
            self.assertEqual(
                list(home.glob(f".{guard.RESULT_JOURNAL_NAME}.*.tmp")), [],
            )

    def test_cli_ollama_sha256_is_exact_paired_and_preflight_only(self):
        valid = "a" * 64
        executable = "C:/private/SENTINEL-ollama.exe"
        invalid_cases = (
            ["--ollama-sha256", valid],
            ["--ollama-executable", executable, "--ollama-sha256", "A" * 64],
            ["--ollama-executable", executable, "--ollama-sha256", f" {valid}"],
            ["--ollama-executable", executable, "--ollama-sha256", valid[:-1]],
            ["--ollama-executable", executable, "--ollama-sha256", "g" * 64],
        )
        for argv in invalid_cases:
            with self.subTest(argv=argv), patch(
                "scripts.runtime_guard.read_company_identity",
            ) as identity, patch(
                "scripts.runtime_guard._runtime_guard_lock",
            ) as lock, patch(
                "scripts.runtime_guard.check_project",
            ) as manifest, patch(
                "scripts.runtime_guard._probe_ollama",
            ) as probe, patch(
                "scripts.runtime_guard.subprocess.Popen",
            ) as popen, contextlib.redirect_stdout(
                stdout := io.StringIO()
            ), contextlib.redirect_stderr(stderr := io.StringIO()):
                code = guard.main(["--home", "ignored", *argv])

            self.assertEqual(code, 3)
            self.assertEqual(stderr.getvalue(), "")
            rendered = stdout.getvalue()
            self.assertEqual(json.loads(rendered)["blockers"], ["invalid_arguments"])
            self.assertNotIn("SENTINEL", rendered)
            self.assertNotIn(valid, rendered)
            self.assertLess(len(rendered.encode("utf-8")), 2048)
            identity.assert_not_called()
            lock.assert_not_called()
            manifest.assert_not_called()
            probe.assert_not_called()
            popen.assert_not_called()

        result = _ready_guard_result()
        with patch(
            "scripts.runtime_guard.guard_once", return_value=(result, 0),
        ) as guard_once, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            code = guard.main([
                "--home", "ignored", "--ollama-executable", executable,
                "--ollama-sha256", valid,
            ])
        self.assertEqual(code, 0)
        self.assertEqual(guard_once.call_args.kwargs["ollama_executable"], Path(executable))
        self.assertEqual(guard_once.call_args.kwargs["ollama_sha256"], valid)

    def test_cli_json_is_allowlisted_bounded_and_sanitized(self):
        secret = "SENTINEL-C:/private/service-token"
        raw_service = _live_service_result(
            token=secret,
            process_birth="a" * 64,
            service_instance_id="b" * 32,
            home="C:/private/company",
            log_path="C:/private/service.log",
            oversized=secret * 10000,
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.runtime_guard.read_company_identity", return_value=IDENTITY,
        ), patch(
            "scripts.runtime_guard._runtime_guard_lock",
            return_value=contextlib.nullcontext(),
        ), patch(
            "scripts.runtime_guard.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.runtime_guard._probe_ollama", return_value=("reachable", True),
        ), patch(
            "scripts.runtime_guard.service_status", return_value=raw_service,
        ), patch(
            "scripts.runtime_guard._full_readiness", return_value=("ready", "none"),
        ), contextlib.redirect_stdout(stdout := io.StringIO()), contextlib.redirect_stderr(
            stderr := io.StringIO()
        ):
            code = guard.main(["--home", tmp, "--wait-seconds", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        rendered = stdout.getvalue()
        self.assertLess(len(rendered.encode("utf-8")), 2048)
        self.assertEqual(rendered.count("\n"), 1)
        for forbidden in (secret, "private", "a" * 64, "b" * 32, "oversized"):
            self.assertNotIn(forbidden, rendered)
        payload = json.loads(rendered)
        self.assertEqual(set(payload), {
            "schema", "status", "ready", "components", "blockers", "action",
            "changes", "required_model", "missions_started", "models_pulled",
        })
        self.assertEqual(set(payload["components"]), {
            "company_store", "disk_manifest", "service", "process_identity",
            "ollama", "model", "readiness",
        })
        self.assertEqual(payload["missions_started"], 0)
        self.assertEqual(payload["models_pulled"], 0)

        cases = (
            (["--unknown", "C:/private/SENTINEL"], 3, "invalid_arguments"),
            (["--model", "C:/private/SENTINEL"], 3, "invalid_arguments"),
            (["--result-path", "C:/private/SENTINEL"], 3, "invalid_arguments"),
        )
        for argv, expected_code, blocker in cases:
            with self.subTest(argv=argv), patch(
                "scripts.runtime_guard._write_result_journal",
            ) as journal, contextlib.redirect_stdout(
                stdout := io.StringIO()
            ), contextlib.redirect_stderr(stderr := io.StringIO()):
                code = guard.main(argv)
            self.assertEqual(code, expected_code)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(json.loads(stdout.getvalue())["blockers"], [blocker])
            self.assertNotIn("SENTINEL", stdout.getvalue())
            self.assertNotIn("private", stdout.getvalue().lower())
            self.assertLess(len(stdout.getvalue().encode("utf-8")), 2048)
            journal.assert_not_called()

        with patch(
            "scripts.runtime_guard.guard_once",
            side_effect=RuntimeError(secret * 10000),
        ), contextlib.redirect_stdout(stdout := io.StringIO()), contextlib.redirect_stderr(
            stderr := io.StringIO()
        ):
            code = guard.main(["--home", "C:/private/SENTINEL"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["blockers"], ["internal_guard_error"])
        self.assertNotIn("SENTINEL", stdout.getvalue())
        self.assertLess(len(stdout.getvalue().encode("utf-8")), 2048)


if __name__ == "__main__":
    unittest.main()
