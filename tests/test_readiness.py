import contextlib
import http.client
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from scripts.check_live_build import LiveBuildError
from scripts.check_readiness import (
    MAX_OLLAMA_BYTES,
    OllamaProbeError,
    main,
    ollama_model_installed,
    run_readiness,
)
from scripts.stamp_build_manifest import ManifestError


MODEL = "qwen3.5:0.8b"
DIGEST = "a" * 64


class _Headers(dict):
    pass


class _Response:
    def __init__(self, payload: object, content_type: str = "application/json") -> None:
        self.status = 200
        self.headers = _Headers({"Content-Type": content_type})
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _disk() -> dict[str, object]:
    return {
        "schema": "local-company.runtime-build.v2",
        "package_version": "0.1.0",
        "build_id": "local-build-20260727.5",
        "source_sha256": "b" * 64,
    }


def _health() -> dict[str, object]:
    return {
        "status": "ready",
        "pid": 1234,
        "build": {
            **_disk(), "git_commit": None, "source_dirty": None,
        },
        "runtime": {
            "provider": "ollama", "model": MODEL, "endpoint": "loopback_default",
        },
        "health": {
            "active_jobs": 0,
            "queued_missions": 0,
            "running_missions": 0,
            "pending_approvals": 0,
            "pending_report_finalizations": 0,
            "pending_evaluations": 0,
        },
        "worker": {"status": "idle"},
        "secret_extra": "SENTINEL-SECRET",
        "jobs": [["C:\\private\\objective.txt"]],
    }


def _live(status: str = "match", idle: bool = True) -> dict[str, object]:
    return {
        "status": status,
        "restart_safe_now": idle,
        "service_pid": 1234,
        "disk_build": {"source_sha256": "SENTINEL-SOURCE-HASH"},
        "live_build": {"source_sha256": "SENTINEL-LIVE-HASH"},
        "work_state": {
            "worker_status": "idle",
            "worker_output": "SENTINEL-WORKER-OUTPUT",
        },
    }


class ReadinessTests(unittest.TestCase):
    def _run(
        self,
        *,
        health: dict[str, object] | None = None,
        live: dict[str, object] | None = None,
        installed: object = True,
    ) -> tuple[dict[str, object], int]:
        health = _health() if health is None else health
        live = _live() if live is None else live
        with patch("scripts.check_readiness.check_project", return_value=_disk()), patch(
            "scripts.check_readiness.fetch_health", return_value=health,
        ), patch(
            "scripts.check_readiness.compare_live_build", return_value=live,
        ), patch(
            "scripts.check_readiness.ollama_model_installed",
            side_effect=installed if isinstance(installed, Exception) else None,
            return_value=installed if not isinstance(installed, Exception) else None,
        ):
            return run_readiness(MODEL)

    def test_ready_output_is_allowlisted_bounded_and_does_not_claim_generation(self):
        payload, exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["generation_tested"])
        self.assertEqual(payload["required_model"], MODEL)
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["action"], "none")
        self.assertEqual(
            payload["components"], {
                "disk_manifest": "valid",
                "live_build": "match",
                "work_state": "idle",
                "worker": "enabled",
                "service_runtime": "match",
                "ollama_service": "reachable",
                "model_installed": "yes",
            },
        )
        rendered = json.dumps(payload, sort_keys=True)
        self.assertLess(len(rendered.encode("utf-8")), 2048)
        for forbidden in (
            "SENTINEL", "private", "source_sha256", "service_pid", "worker_output",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_real_build_comparison_preserves_disabled_worker_blocker(self):
        health = _health()
        health["worker"] = {"status": "disabled"}
        with patch("scripts.check_readiness.check_project", return_value=_disk()), patch(
            "scripts.check_readiness.fetch_health", return_value=health,
        ), patch(
            "scripts.check_readiness.ollama_model_installed", return_value=True,
        ):
            payload, exit_code = run_readiness(MODEL)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["components"]["worker"], "disabled")
        self.assertIn("worker_disabled", payload["blockers"])
        self.assertEqual(payload["action"], "start_worker_enabled_service")

    def test_known_build_work_runtime_and_model_blockers_return_action_required(self):
        cases = (
            (_live("restart_required", True), _health(), True,
             "service_restart_required", "restart_local_service"),
            (_live("restart_required", False), _health(), True,
             "service_busy_before_restart", "inspect_local_work"),
            (_live("disk_older", True), _health(), True,
             "disk_older_than_service", "inspect_build_provenance"),
            (_live("identity_conflict", True), _health(), True,
             "build_identity_conflict", "inspect_build_provenance"),
            (_live("match", False), _health(), True,
             "local_work_active", "inspect_local_work"),
        )
        for live, health, installed, blocker, action in cases:
            with self.subTest(blocker=blocker):
                payload, exit_code = self._run(
                    live=live, health=health, installed=installed,
                )
                self.assertEqual(exit_code, 1)
                self.assertIn(blocker, payload["blockers"])
                self.assertEqual(payload["action"], action)

        disabled_worker = _live()
        disabled_worker["work_state"] = {"worker_status": "disabled"}
        payload, exit_code = self._run(live=disabled_worker)
        self.assertEqual(exit_code, 1)
        self.assertIn("worker_disabled", payload["blockers"])
        self.assertEqual(payload["components"]["worker"], "disabled")
        self.assertEqual(payload["action"], "start_worker_enabled_service")

        provider_mismatch = _health()
        provider_mismatch["runtime"] = {
            "provider": "mock", "model": None, "endpoint": None,
        }
        payload, exit_code = self._run(health=provider_mismatch)
        self.assertEqual(exit_code, 1)
        self.assertIn("service_runtime_provider_mismatch", payload["blockers"])

        model_mismatch = _health()
        model_mismatch["runtime"] = {
            "provider": "ollama", "model": "other:latest",
            "endpoint": "loopback_default",
        }
        payload, exit_code = self._run(health=model_mismatch)
        self.assertEqual(exit_code, 1)
        self.assertIn("service_runtime_model_mismatch", payload["blockers"])

        endpoint_mismatch = _health()
        endpoint_mismatch["runtime"] = {
            "provider": "ollama", "model": MODEL, "endpoint": "nonlocal",
        }
        payload, exit_code = self._run(health=endpoint_mismatch)
        self.assertEqual(exit_code, 1)
        self.assertIn("service_runtime_endpoint_mismatch", payload["blockers"])

        runtime_unverified = _health()
        del runtime_unverified["runtime"]
        payload, exit_code = self._run(health=runtime_unverified)
        self.assertEqual(exit_code, 1)
        self.assertIn("service_runtime_unverified", payload["blockers"])

        payload, exit_code = self._run(installed=False)
        self.assertEqual(exit_code, 1)
        self.assertIn("model_not_installed", payload["blockers"])

        payload, exit_code = self._run(installed=OllamaProbeError("unavailable"))
        self.assertEqual(exit_code, 1)
        self.assertIn("ollama_unavailable", payload["blockers"])

        provider_mismatch["runtime"] = {
            "provider": "mock", "model": None, "endpoint": None,
        }
        payload, exit_code = self._run(
            health=provider_mismatch, installed=OllamaProbeError("unavailable"),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["action"], "start_ollama_locally")

    def test_indeterminate_and_manifest_failures_are_sanitized(self):
        for runtime in (
            {"provider": [], "model": MODEL, "endpoint": "loopback_default"},
            {"provider": "ollama\n", "model": MODEL, "endpoint": "loopback_default"},
            {"provider": "ollama", "model": None, "endpoint": "loopback_default"},
            {"provider": "ollama", "model": MODEL, "endpoint": []},
            {"provider": "mock", "model": MODEL, "endpoint": None},
            {"provider": "mock", "model": None, "endpoint": "loopback_default"},
        ):
            with self.subTest(runtime=runtime):
                malformed_runtime = _health()
                malformed_runtime["runtime"] = runtime
                payload, exit_code = self._run(health=malformed_runtime)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["blockers"], ["service_runtime_invalid"])

        payload, exit_code = self._run(installed=OllamaProbeError("invalid_response"))
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["blockers"], ["ollama_status_indeterminate"])

        malformed_live = _live()
        for work_state in (None, {}, {"worker_status": []}, {"worker_status": "unknown"}):
            with self.subTest(work_state=work_state):
                malformed_live["work_state"] = work_state
                payload, exit_code = self._run(live=malformed_live)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["blockers"], ["live_status_indeterminate"])

        for worker_status in ("running", "needs_approval", "completion_pending"):
            with self.subTest(inconsistent_worker_status=worker_status):
                inconsistent_live = _live(idle=True)
                inconsistent_live["work_state"] = {"worker_status": worker_status}
                payload, exit_code = self._run(live=inconsistent_live)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["blockers"], ["live_status_indeterminate"])

        for non_bool in (1, "yes", None):
            with self.subTest(non_bool_probe_result=non_bool):
                payload, exit_code = self._run(installed=non_bool)
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["blockers"], ["ollama_status_indeterminate"])

        with patch(
            "scripts.check_readiness.check_project",
            side_effect=ManifestError("C:\\secret\\manifest SENTINEL"),
        ):
            payload, exit_code = run_readiness(MODEL)
        self.assertEqual(exit_code, 3)
        self.assertEqual(payload["blockers"], ["disk_manifest_invalid"])
        self.assertEqual(payload["action"], "inspect_disk_manifest")
        self.assertNotIn("SENTINEL", json.dumps(payload))

        with patch("scripts.check_readiness.check_project", return_value=_disk()), patch(
            "scripts.check_readiness.fetch_health",
            side_effect=LiveBuildError("SENTINEL live failure"),
        ):
            payload, exit_code = run_readiness(MODEL)
        self.assertEqual(exit_code, 2)
        self.assertNotIn("SENTINEL", json.dumps(payload))

        for invalid_model in (
            "bad model name with spaces", "qwen::tag", "C:/private/model", "name//tag",
        ):
            payload, exit_code = run_readiness(invalid_model)
            self.assertEqual(exit_code, 2)
            self.assertEqual(payload["blockers"], ["invalid_model_name"])
            self.assertIsNone(payload["required_model"])

    def test_ollama_probe_is_fixed_bounded_proxy_free_and_strict(self):
        payload = {"models": [{"name": MODEL, "digest": DIGEST}]}
        opener = Mock()
        opener.open.return_value = _Response(payload)
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ) as build_opener:
            self.assertTrue(ollama_model_installed(MODEL))
        proxy_handler, redirect_handler = build_opener.call_args.args
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsNone(redirect_handler.redirect_request())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/tags")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

        opener.open.side_effect = urllib.error.URLError("SENTINEL unavailable")
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            with self.assertRaisesRegex(OllamaProbeError, "unavailable"):
                ollama_model_installed(MODEL)
        opener.open.side_effect = None

        exact = _Response(payload)
        base = exact.body
        exact.body = base + b" " * (MAX_OLLAMA_BYTES - len(base))
        opener.open.return_value = exact
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            self.assertTrue(ollama_model_installed(MODEL))

        alias = _Response({
            "models": [{"name": "gemma3:latest", "digest": DIGEST}],
        }, "Application/JSON; charset=utf-8")
        opener.open.return_value = alias
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            self.assertTrue(ollama_model_installed("gemma3"))

        oversized = _Response(payload)
        oversized.body = b"x" * (MAX_OLLAMA_BYTES + 1)
        opener.open.return_value = oversized
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            with self.assertRaisesRegex(OllamaProbeError, "invalid_response"):
                ollama_model_installed(MODEL)

        malformed_payloads = (
            [],
            {},
            {"models": "not-a-list"},
            {"models": ["not-an-object"]},
            {"models": [{"name": MODEL, "digest": "bad"}]},
            {"models": [{"name": MODEL + ":latest", "digest": DIGEST}]},
            {"models": [
                {"name": MODEL, "digest": DIGEST},
                {"name": MODEL, "digest": DIGEST},
            ]},
        )
        for malformed in malformed_payloads:
            with self.subTest(payload=malformed):
                opener.open.return_value = _Response(malformed)
                with patch(
                    "scripts.check_readiness.urllib.request.build_opener",
                    return_value=opener,
                ):
                    with self.assertRaises(OllamaProbeError):
                        ollama_model_installed(MODEL)

        for content_type in (
            "text/plain", "application/jsonp", "application/json-evil",
            "application/json garbage",
        ):
            with self.subTest(content_type=content_type):
                opener.open.return_value = _Response(payload, content_type)
                with patch(
                    "scripts.check_readiness.urllib.request.build_opener",
                    return_value=opener,
                ):
                    with self.assertRaises(OllamaProbeError):
                        ollama_model_installed(MODEL)

        duplicate_json_bodies = (
            b'{"models":[],"models":[{"name":"' + MODEL.encode() +
            b'","digest":"' + DIGEST.encode() + b'"}]}',
            b'{"models":[{"name":"other","name":"' + MODEL.encode() +
            b'","digest":"' + DIGEST.encode() + b'"}]}',
            b'{"models":[{"name":"' + MODEL.encode() +
            b'","digest":"' + DIGEST.encode() +
            b'","digest":"' + DIGEST.encode() + b'"}]}',
        )
        for body in duplicate_json_bodies:
            with self.subTest(duplicate_body=body[:30]):
                duplicate = _Response(payload)
                duplicate.body = body
                opener.open.return_value = duplicate
                with patch(
                    "scripts.check_readiness.urllib.request.build_opener",
                    return_value=opener,
                ):
                    with self.assertRaises(OllamaProbeError):
                        ollama_model_installed(MODEL)

        nested = _Response(payload)
        nested.body = b'{"models":' + b"[" * 20000 + b"]" * 20000 + b"}"
        opener.open.return_value = nested
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            with self.assertRaises(OllamaProbeError):
                ollama_model_installed(MODEL)

        truncated = _Response(payload)
        truncated.read = Mock(side_effect=http.client.IncompleteRead(b"{"))
        opener.open.return_value = truncated
        with patch(
            "scripts.check_readiness.urllib.request.build_opener", return_value=opener,
        ):
            with self.assertRaises(OllamaProbeError):
                ollama_model_installed(MODEL)

    def test_ollama_redirect_is_not_followed(self):
        target_hits = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                target_hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "13")
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

            def log_message(self, *_: object) -> None:
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        target_url = f"http://127.0.0.1:{target.server_address[1]}/api/tags"

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_: object) -> None:
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            with patch(
                "scripts.check_readiness.OLLAMA_TAGS_URL",
                f"http://127.0.0.1:{redirect.server_address[1]}/api/tags",
            ):
                with self.assertRaises(OllamaProbeError):
                    ollama_model_installed(MODEL)
            self.assertEqual(target_hits, [])
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=3)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=3)

    def test_main_sanitizes_unexpected_errors_without_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "scripts.check_readiness.run_readiness",
            side_effect=RuntimeError("SENTINEL C:\\secret"),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["--model", MODEL])
        self.assertEqual(exit_code, 3)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["blockers"], ["internal_error"])
        self.assertNotIn("SENTINEL", stdout.getvalue())
        self.assertLess(len(stdout.getvalue().encode("utf-8")), 2048)

        for arguments in (
            ["--unknown", "C:\\private\\SENTINEL-SECRET"],
            ["--model"],
        ):
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = main(arguments)
                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload["blockers"], ["invalid_arguments"])
                self.assertNotIn("SENTINEL", stdout.getvalue())
                self.assertNotIn("private", stdout.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
