import http.client
import json
import unittest
from unittest.mock import Mock, patch

from scripts.check_live_build import (
    BUILD_FIELDS, BUILD_STATUS_URL, IDLE_FIELDS, MAX_BUILD_STATUS_BYTES,
    LiveBuildError, compare_live_build, fetch_health,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status = 200
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class LiveBuildTests(unittest.TestCase):
    def _disk(self) -> dict[str, object]:
        return {
            "schema": "local-company.runtime-build.v2",
            "package_version": "0.1.0",
            "build_id": "local-build-20260727.2",
            "source_sha256": "a" * 64,
        }

    def _health(self) -> dict[str, object]:
        disk = self._disk()
        return {
            "status": "ready",
            "pid": 1234,
            "build": {
                **{field: disk[field] for field in BUILD_FIELDS},
                "git_commit": None,
                "source_dirty": None,
            },
            "health": {field: 0 for field in IDLE_FIELDS},
            "worker": {"status": "idle"},
        }

    def test_compare_reports_match_or_safe_restart_without_guessing(self):
        matched = compare_live_build(self._disk(), self._health())
        self.assertEqual(matched["status"], "match")
        self.assertFalse(matched["restart_required"])
        self.assertTrue(matched["restart_safe_now"])
        self.assertEqual(matched["mismatched_fields"], [])
        self.assertFalse(matched["legacy_status_payload"])

        stale = self._health()
        stale["build"] = dict(stale["build"], build_id="local-build-20260727.1")
        mismatched = compare_live_build(self._disk(), stale)
        self.assertEqual(mismatched["status"], "restart_required")
        self.assertEqual(mismatched["mismatched_fields"], ["build_id"])
        self.assertTrue(mismatched["restart_safe_now"])

        stale["health"] = dict(stale["health"], active_jobs=1)
        busy = compare_live_build(self._disk(), stale)
        self.assertFalse(busy["restart_safe_now"])
        self.assertIn("Wait", busy["recommendation"])

        stale["health"] = {field: 0 for field in IDLE_FIELDS}
        stale["worker"] = {"status": "running"}
        worker_race = compare_live_build(self._disk(), stale)
        self.assertFalse(worker_race["restart_safe_now"])
        self.assertIn("Wait", worker_race["recommendation"])

        stale["worker"] = {"status": "completion_pending"}
        completion_pending = compare_live_build(self._disk(), stale)
        self.assertFalse(completion_pending["restart_safe_now"])
        self.assertIn("Wait", completion_pending["recommendation"])

        stale["worker"] = {"status": "needs_approval"}
        needs_approval = compare_live_build(self._disk(), stale)
        self.assertFalse(needs_approval["restart_safe_now"])

        newer_live = self._health()
        newer_live["build"] = dict(
            newer_live["build"], build_id="local-build-20260727.3",
        )
        older_disk = compare_live_build(self._disk(), newer_live)
        self.assertEqual(older_disk["status"], "disk_older")
        self.assertFalse(older_disk["restart_required"])
        self.assertIn("Do not restart", older_disk["recommendation"])

        conflicting = self._health()
        conflicting["build"] = dict(conflicting["build"], source_sha256="b" * 64)
        conflict = compare_live_build(self._disk(), conflicting)
        self.assertEqual(conflict["status"], "identity_conflict")
        self.assertFalse(conflict["restart_required"])

        provenance = self._health()
        provenance["build"] = dict(provenance["build"], source_dirty=False)
        provenance_conflict = compare_live_build(self._disk(), provenance)
        self.assertEqual(provenance_conflict["status"], "identity_conflict")
        self.assertIn("source_dirty", provenance_conflict["mismatched_fields"])

        stale = self._health()
        stale["health"] = dict(stale["health"], active_jobs=True)
        with self.assertRaises(LiveBuildError):
            compare_live_build(self._disk(), stale)

        legacy = self._health()
        del legacy["health"]["running_missions"]
        legacy["queue"] = [
            ["running-queue", "running"], ["queued-queue", "queued"],
        ]
        legacy_result = compare_live_build(self._disk(), legacy)
        self.assertTrue(legacy_result["legacy_status_payload"])
        self.assertEqual(legacy_result["work_state"]["running_missions"], 1)
        self.assertEqual(legacy_result["work_state"]["queued_missions"], 1)
        self.assertFalse(legacy_result["restart_safe_now"])

        legacy["queue"] = [["queue-id", "unknown"]]
        with self.assertRaises(LiveBuildError):
            compare_live_build(self._disk(), legacy)

        malformed_worker = self._health()
        malformed_worker["worker"] = {"status": []}
        with self.assertRaises(LiveBuildError):
            compare_live_build(self._disk(), malformed_worker)

    def test_compare_rejects_malformed_live_build_identity(self):
        malformed_values = (
            ("schema", ""),
            ("schema", "local-company.runtime-build.v1"),
            ("package_version", ""),
            ("package_version", "0.1.0 unsafe"),
            ("source_sha256", "not-a-sha"),
            ("source_sha256", "A" * 64),
        )
        for field, value in malformed_values:
            with self.subTest(field=field, value=value):
                health = self._health()
                health["build"] = dict(health["build"], **{field: value})
                with self.assertRaises(LiveBuildError):
                    compare_live_build(self._disk(), health)

    def test_fetch_health_is_fixed_loopback_bounded_and_proxy_free(self):
        payload = self._health()
        opener = Mock()
        opener.open.return_value = _Response(payload)
        with patch(
            "scripts.check_live_build.urllib.request.build_opener",
            return_value=opener,
        ) as build_opener:
            self.assertEqual(fetch_health(), payload)
        build_opener.assert_called_once()
        proxy_handler, redirect_handler = build_opener.call_args.args
        self.assertEqual(proxy_handler.proxies, {})
        self.assertIsNone(redirect_handler.redirect_request())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, BUILD_STATUS_URL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

        oversized = Mock()
        oversized.status = 200
        oversized.__enter__ = Mock(return_value=oversized)
        oversized.__exit__ = Mock(return_value=None)
        oversized.read.return_value = b"x" * (MAX_BUILD_STATUS_BYTES + 1)
        opener.open.return_value = oversized
        with patch(
            "scripts.check_live_build.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaises(LiveBuildError):
                fetch_health()

        deeply_nested = Mock()
        deeply_nested.status = 200
        deeply_nested.__enter__ = Mock(return_value=deeply_nested)
        deeply_nested.__exit__ = Mock(return_value=None)
        deeply_nested.read.return_value = (
            b'{"x":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}"
        )
        opener.open.return_value = deeply_nested
        with patch(
            "scripts.check_live_build.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaises(LiveBuildError):
                fetch_health()

        truncated = Mock()
        truncated.status = 200
        truncated.__enter__ = Mock(return_value=truncated)
        truncated.__exit__ = Mock(return_value=None)
        truncated.read.side_effect = http.client.IncompleteRead(b"{")
        opener.open.return_value = truncated
        with patch(
            "scripts.check_live_build.urllib.request.build_opener",
            return_value=opener,
        ):
            with self.assertRaises(LiveBuildError):
                fetch_health()


if __name__ == "__main__":
    unittest.main()
