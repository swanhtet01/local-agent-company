import contextlib
import errno
import io
import json
import subprocess
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
        "num_predict": 2048,
        "keep_alive": "30s",
        **extra,
    }


def _locked_arguments(home: Path) -> dict[str, object]:
    return {
        "home": home,
        "pinned_identity": dict(IDENTITY),
        "port": 8765,
        "model": MODEL,
        "num_ctx": 4096,
        "num_predict": 2048,
        "keep_alive": "30s",
        "wait_seconds": 1,
        "ollama_executable": None,
    }


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
            payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["changes"], ["ollama_started"])
        spawn.assert_called_once_with(executable_path)
        start.assert_not_called()

    def test_failed_owned_ollama_child_is_cleaned_and_cleanup_failure_fails_closed(self):
        child = Mock()
        executable_path = Path("C:/trusted/ollama.exe")
        for cleaned in (True, False):
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
                payload, code = guard._guard_locked(**_locked_arguments(Path(tmp)))
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
                num_ctx=4096, num_predict=2048, keep_alive="30s",
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
                    num_predict=2048, keep_alive="30s",
                )
                self.assertEqual(status, "invalid")
                self.assertFalse(live)
                self.assertFalse(guard._safe_service_start(status, relation))

        wrong_configuration = _live_service_result(port=True)
        self.assertEqual(
            guard._service_components(
                wrong_configuration, port=8765, model=MODEL, num_ctx=4096,
                num_predict=2048, keep_alive="30s",
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

    def test_windows_spawn_uses_exact_serve_command_and_breakaway_flags(self):
        executable = Path("C:/trusted/ollama.exe")
        child = Mock()
        with patch.object(guard.os, "name", "nt"), patch.object(
            guard.subprocess, "DETACHED_PROCESS", 0x01, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x02, create=True,
        ), patch.object(
            guard.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x04, create=True,
        ), patch(
            "scripts.runtime_guard.subprocess.Popen", return_value=child,
        ) as popen:
            self.assertIs(guard._spawn_ollama(executable), child)
        popen.assert_called_once()
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
                guard._spawn_ollama(executable)
        unavailable_popen.assert_not_called()

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
        )
        for argv, expected_code, blocker in cases:
            with self.subTest(argv=argv), contextlib.redirect_stdout(
                stdout := io.StringIO()
            ), contextlib.redirect_stderr(stderr := io.StringIO()):
                code = guard.main(argv)
            self.assertEqual(code, expected_code)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(json.loads(stdout.getvalue())["blockers"], [blocker])
            self.assertNotIn("SENTINEL", stdout.getvalue())
            self.assertNotIn("private", stdout.getvalue().lower())
            self.assertLess(len(stdout.getvalue().encode("utf-8")), 2048)

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
