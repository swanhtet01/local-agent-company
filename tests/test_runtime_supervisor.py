import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import check_runtime_supervisor as supervisor
from scripts import runtime_guard


MODEL = "llama3.2:1b"
TASK_NAME = "SuperMega Local Runtime Guard"
NOW_NS = 2_000_000_000_000_000_000
IDENTITY = {
    "schema": "local-company.store.v1",
    "instance_id": "123e4567e89b42d3a456426614174000",
}


def _guard_result() -> dict[str, object]:
    return runtime_guard._payload(
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
        model=MODEL,
    )


def _readiness_result() -> dict[str, object]:
    return {
        "schema": "local-company.readiness.v1",
        "status": "ready",
        "ready": True,
        "required_model": MODEL,
        "components": {
            "disk_manifest": "valid",
            "live_build": "match",
            "work_state": "idle",
            "worker": "enabled",
            "company_store": "match",
            "service_runtime": "match",
            "ollama_service": "reachable",
            "model_installed": "yes",
        },
        "generation_tested": False,
        "blockers": [],
        "action": "none",
    }


class RuntimeSupervisorTests(unittest.TestCase):
    def test_supervisor_uses_guard_scale_to_zero_profile(self):
        self.assertEqual(supervisor.NUM_CTX, runtime_guard.RUNTIME_NUM_CTX)
        self.assertEqual(supervisor.NUM_PREDICT, 768)
        self.assertEqual(supervisor.KEEP_ALIVE, "0s")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.home = root / "company"
        self.home.mkdir()
        self.python = root / "python.exe"
        self.python.write_bytes(b"python sentinel")
        self.ollama = root / "ollama.exe"
        self.ollama.write_bytes(b"reviewed ollama bytes")
        self.digest = "a" * 64
        self.last_run_ns = NOW_NS - 60_000_000_000
        self.journal_mtime_ns = self.last_run_ns + 30_000_000_000

    @staticmethod
    def _utc(timestamp_ns: int) -> str:
        return datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000, tz=timezone.utc,
        ).isoformat()

    def _expected(self) -> supervisor.ExpectedTask:
        return supervisor.ExpectedTask(
            TASK_NAME,
            self.python,
            supervisor.PROJECT_ROOT / "scripts" / "runtime_guard.py",
            self.home,
            self.ollama,
            self.digest,
            MODEL,
            False,
        )

    def _task(self, **overrides: object) -> supervisor.TaskSnapshot:
        values = {
            "found": True,
            "configuration": "match",
            "enabled": True,
            "state": "ready",
            "last_result": 0,
            "last_run_utc": self._utc(self.last_run_ns),
            "next_run_utc": self._utc(NOW_NS + 240_000_000_000),
            **overrides,
        }
        return supervisor.TaskSnapshot(**values)

    def _journal(self, **overrides: object) -> supervisor.JournalSnapshot:
        payload = _guard_result()
        rendered = runtime_guard._render_result(payload)
        values = {
            "payload": payload,
            "signature": (
                1, 2, stat.S_IFREG | 0o600, 1, len(rendered),
                self.journal_mtime_ns,
            ),
            "mtime_ns": self.journal_mtime_ns,
            **overrides,
        }
        return supervisor.JournalSnapshot(**values)

    def _run(self):
        return supervisor.run_supervisor_check(
            self.home,
            self.python,
            self.ollama,
            self.digest,
            task_name=TASK_NAME,
            model=MODEL,
            allow_windows_job_inheritance=False,
        )

    def test_ready_requires_one_stable_correlated_composition(self):
        events: list[str] = []
        task = self._task()
        journal = self._journal()
        signature = (1, 2, stat.S_IFREG, 1, 24, 25)
        task_calls = 0
        journal_calls = 0

        def acquire(_name):
            nonlocal task_calls
            task_calls += 1
            events.append(f"task{task_calls}")
            return task

        def read(_home, **_kwargs):
            nonlocal journal_calls
            journal_calls += 1
            events.append(f"journal{journal_calls}")
            return journal

        @contextlib.contextmanager
        def verified(_path, _digest):
            events.append("verify_enter")
            yield signature
            events.append("verify_exit")

        def readiness(model, home):
            self.assertEqual((model, home), (MODEL, self.home))
            events.append("readiness")
            return _readiness_result(), 0

        with patch(
            "scripts.check_runtime_supervisor._prepare_expected",
            return_value=(self._expected(), dict(IDENTITY)),
        ), patch(
            "scripts.check_runtime_supervisor.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.check_runtime_supervisor.time.time_ns", return_value=NOW_NS,
        ), patch(
            "scripts.check_runtime_supervisor.acquire_task_snapshot",
            side_effect=acquire,
        ), patch(
            "scripts.check_runtime_supervisor.read_result_journal",
            side_effect=read,
        ), patch(
            "scripts.check_runtime_supervisor._verified_executable_sha256",
            side_effect=verified,
        ), patch(
            "scripts.check_runtime_supervisor.run_readiness",
            side_effect=readiness,
        ), patch(
            "scripts.check_runtime_supervisor.read_company_identity",
            return_value=IDENTITY,
        ):
            payload, code = self._run()

        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["action"], "none")
        self.assertEqual(payload["checks"], {
            "scheduled_task": {
                "configuration": "match", "state": "ready",
                "last_result": "success", "freshness": "fresh",
            },
            "guard_journal": {
                "schema": "valid", "status": "ready",
                "freshness": "fresh", "correlation": "match",
            },
            "ollama_executable": {"pin": "match"},
            "readiness": {"status": "ready", "action": "none"},
        })
        self.assertEqual(payload["ages_seconds"], {"task": 60, "journal": 30})
        self.assertEqual(events, [
            "task1", "journal1", "verify_enter", "readiness",
            "journal2", "task2", "verify_exit",
        ])
        rendered = supervisor.render_result(payload)
        self.assertIsInstance(rendered, bytes)
        self.assertLessEqual(len(rendered), supervisor.MAX_RENDERED_RESULT_BYTES)
        self.assertEqual(rendered.count(b"\n"), 1)
        self.assertTrue(rendered.endswith(b"\n"))
        for forbidden in (str(self.home), str(self.python), str(self.ollama), self.digest):
            self.assertNotIn(forbidden, rendered.decode("utf-8"))

    def test_repeated_task_or_journal_race_retries_once_then_exits_two(self):
        base_task = self._task()
        changed_task = self._task(
            last_run_utc=self._utc(self.last_run_ns + 1_000_000_000),
        )
        base_journal = self._journal()
        changed_journal = self._journal(signature=(
            1, 9, stat.S_IFREG | 0o600, 1,
            len(runtime_guard._render_result(base_journal.payload)),
            self.journal_mtime_ns + 1,
        ))

        cases = (
            (
                "task",
                [base_task, changed_task, base_task, changed_task],
                [base_journal] * 4,
            ),
            (
                "journal",
                [base_task] * 4,
                [base_journal, changed_journal, base_journal, changed_journal],
            ),
        )
        for name, tasks, journals in cases:
            with self.subTest(race=name), patch(
                "scripts.check_runtime_supervisor._prepare_expected",
                return_value=(self._expected(), dict(IDENTITY)),
            ), patch(
                "scripts.check_runtime_supervisor.check_project",
                return_value={"status": "ok"},
            ), patch(
                "scripts.check_runtime_supervisor.time.time_ns", return_value=NOW_NS,
            ), patch(
                "scripts.check_runtime_supervisor.acquire_task_snapshot",
                side_effect=tasks,
            ) as acquire, patch(
                "scripts.check_runtime_supervisor.read_result_journal",
                side_effect=journals,
            ) as read, patch(
                "scripts.check_runtime_supervisor._verified_executable_sha256",
                side_effect=lambda *_args: contextlib.nullcontext((1, 2, 3, 4, 5, 6)),
            ), patch(
                "scripts.check_runtime_supervisor.run_readiness",
                return_value=(_readiness_result(), 0),
            ), patch(
                "scripts.check_runtime_supervisor.read_company_identity",
                return_value=IDENTITY,
            ):
                payload, code = self._run()

            self.assertEqual(code, 2)
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["blockers"], ["supervisor_snapshot_changed"])
            self.assertEqual(payload["action"], "retry_runtime_supervisor")
            self.assertEqual(acquire.call_count, 4)
            self.assertEqual(read.call_count, 4)

    def test_result_journal_rejects_unsafe_stale_future_and_noncanonical_files(self):
        canonical = runtime_guard._render_result(_guard_result())
        invalid_cases = (
            ("noncanonical", json.dumps(_guard_result(), indent=2).encode("utf-8")),
            ("oversized", b"x" * (supervisor.MAX_RESULT_JOURNAL_BYTES + 1)),
        )
        for name, content in invalid_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                target = home / supervisor.RESULT_JOURNAL_NAME
                target.write_bytes(content)
                with self.assertRaises(supervisor.JournalSnapshotError):
                    supervisor.read_result_journal(home)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            external = home / "external.bin"
            target = home / supervisor.RESULT_JOURNAL_NAME
            external.write_bytes(canonical)
            try:
                os.link(external, target)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            with self.assertRaises(supervisor.JournalSnapshotError):
                supervisor.read_result_journal(home)
            self.assertEqual(external.read_bytes(), canonical)

        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.check_runtime_supervisor._record_metadata",
            side_effect=runtime_guard.GuardResultJournalError("unsafe reparse"),
        ):
            home = Path(tmp)
            target = home / supervisor.RESULT_JOURNAL_NAME
            target.write_bytes(canonical)
            with self.assertRaises(supervisor.JournalSnapshotError):
                supervisor.read_result_journal(home)

        freshness_cases = (
            (
                "stale",
                NOW_NS - (supervisor.MAX_READY_AGE_SECONDS + 1) * 1_000_000_000,
            ),
            (
                "future",
                NOW_NS + (supervisor.CLOCK_SKEW_SECONDS + 1) * 1_000_000_000,
            ),
        )
        for freshness, mtime_ns in freshness_cases:
            rendered = runtime_guard._render_result(_guard_result())
            journal = self._journal(
                mtime_ns=mtime_ns,
                signature=(
                    1, 2, stat.S_IFREG | 0o600, 1, len(rendered), mtime_ns,
                ),
            )
            task = self._task()
            with self.subTest(freshness=freshness), patch(
                "scripts.check_runtime_supervisor._prepare_expected",
                return_value=(self._expected(), dict(IDENTITY)),
            ), patch(
                "scripts.check_runtime_supervisor.check_project",
                return_value={"status": "ok"},
            ), patch(
                "scripts.check_runtime_supervisor.time.time_ns", return_value=NOW_NS,
            ), patch(
                "scripts.check_runtime_supervisor.acquire_task_snapshot",
                side_effect=[task, task],
            ), patch(
                "scripts.check_runtime_supervisor.read_result_journal",
                return_value=journal,
            ), patch(
                "scripts.check_runtime_supervisor._verified_executable_sha256",
            ) as verify, patch(
                "scripts.check_runtime_supervisor.run_readiness",
            ) as readiness:
                payload, code = self._run()

            self.assertEqual(code, 2)
            self.assertFalse(payload["ready"])
            self.assertEqual(payload["blockers"], ["guard_journal_uncorrelated"])
            self.assertEqual(
                payload["checks"]["guard_journal"]["freshness"], freshness,
            )
            verify.assert_not_called()
            readiness.assert_not_called()

    def test_executable_verification_context_spans_readiness_and_rechecks(self):
        active = False
        task = self._task()
        journal = self._journal()
        final_checks: list[bool] = []

        @contextlib.contextmanager
        def changed_after_use(_path, _digest):
            nonlocal active
            active = True
            yield (1, 2, 3, 4, 5, 6)
            active = False
            raise runtime_guard.GuardExecutableError(
                "SENTINEL C:/private/executable changed",
            )

        task_calls = 0
        journal_calls = 0

        def acquire(_name):
            nonlocal task_calls
            task_calls += 1
            if task_calls % 2 == 0:
                final_checks.append(active)
            return task

        def read(_home, **_kwargs):
            nonlocal journal_calls
            journal_calls += 1
            if journal_calls % 2 == 0:
                final_checks.append(active)
            return journal

        def readiness(_model, _home):
            self.assertTrue(active)
            return _readiness_result(), 0

        with patch(
            "scripts.check_runtime_supervisor._prepare_expected",
            return_value=(self._expected(), dict(IDENTITY)),
        ), patch(
            "scripts.check_runtime_supervisor.check_project", return_value={"status": "ok"},
        ), patch(
            "scripts.check_runtime_supervisor.time.time_ns", return_value=NOW_NS,
        ), patch(
            "scripts.check_runtime_supervisor.acquire_task_snapshot",
            side_effect=acquire,
        ), patch(
            "scripts.check_runtime_supervisor.read_result_journal",
            side_effect=read,
        ), patch(
            "scripts.check_runtime_supervisor._verified_executable_sha256",
            side_effect=changed_after_use,
        ), patch(
            "scripts.check_runtime_supervisor.run_readiness",
            side_effect=readiness,
        ), patch(
            "scripts.check_runtime_supervisor.read_company_identity",
            return_value=IDENTITY,
        ):
            payload, code = self._run()

        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["blockers"], ["ollama_executable_invalid"])
        self.assertEqual(payload["action"], "inspect_ollama_service")
        self.assertEqual(final_checks, [True, True])
        rendered = supervisor.render_result(payload).decode("utf-8")
        self.assertNotIn("SENTINEL", rendered)
        self.assertNotIn("private", rendered.lower())

    def test_main_and_task_acquisition_are_bounded_and_sanitized(self):
        expected = self._expected()
        powershell = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        module = Path("C:/Windows/System32/WindowsPowerShell/Modules/ScheduledTasks.psd1")
        oversized = subprocess.CompletedProcess(
            args=["powershell"], returncode=0,
            stdout=b"x" * (supervisor.MAX_TASK_SNAPSHOT_BYTES + 1), stderr=b"",
        )
        with patch(
            "scripts.check_runtime_supervisor._powershell_files",
            return_value=(powershell, module),
        ), patch(
            "scripts.check_runtime_supervisor.subprocess.run", return_value=oversized,
        ) as run:
            with self.assertRaises(supervisor.TaskSnapshotError):
                supervisor.acquire_task_snapshot(expected)
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command, [
            str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command", supervisor.POWERSHELL_TASK_SNAPSHOT,
        ])
        for untrusted in (str(self.home), str(self.python), str(self.ollama), self.digest):
            self.assertNotIn(untrusted, command[-1])
        self.assertEqual(options["env"]["LOCAL_COMPANY_TASK_NAME"], TASK_NAME)
        self.assertEqual(
            options["env"]["LOCAL_COMPANY_TASK_MODULE"], str(module),
        )
        self.assertIs(options.get("shell"), False)
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 10)
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertIs(options["check"], False)
        self.assertTrue(options["close_fds"])

        secret = "SENTINEL-C:/private/task-output"
        argv = [
            "--home", str(self.home),
            "--task-name", TASK_NAME,
            "--python-executable", str(self.python),
            "--model", MODEL,
            "--ollama-executable", str(self.ollama),
            "--ollama-sha256", self.digest,
        ]
        with patch(
            "scripts.check_runtime_supervisor.run_supervisor_check",
            side_effect=RuntimeError(secret * 1000),
        ), contextlib.redirect_stdout(
            stdout := io.StringIO()
        ), contextlib.redirect_stderr(stderr := io.StringIO()):
            code = supervisor.main(argv)

        self.assertEqual(code, 3)
        self.assertEqual(stderr.getvalue(), "")
        rendered = stdout.getvalue()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertLessEqual(
            len(rendered.encode("utf-8")), supervisor.MAX_RENDERED_RESULT_BYTES,
        )
        self.assertNotIn("SENTINEL", rendered)
        self.assertNotIn("private", rendered.lower())
        self.assertNotIn(self.digest, rendered)
        payload = json.loads(rendered)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["blockers"], ["internal_supervisor_error"])


if __name__ == "__main__":
    unittest.main()
