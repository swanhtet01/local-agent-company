from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_company.capacity import (
    build_capacity_snapshot,
    observe_cgroup_memory,
    parse_meminfo,
    parse_windows_listeners,
)
from local_company.cli import main
from local_company.core import Company, MockModel


def ready_inputs() -> dict[str, dict[str, object]]:
    return {
        "brief": {
            "status": "ready",
            "project_id": "0123456789ab",
            "next_action": "queue_or_schedule_reviewed_mission",
        },
        "focus": {
            "enabled": True,
            "projectId": "0123456789ab",
            "maxRoles": 4,
        },
        "health": {"active_jobs": 0, "running_missions": 0},
        "listeners": {
            "status": "ready",
            "counts": {"5173": 1, "8765": 1, "8788": 1, "11434": 1},
        },
        "loaded_models": {"status": "ready", "loaded_count": 0},
        "memory": {
            "status": "ready",
            "total_bytes": 16 * 1024**3,
            "available_bytes": 4 * 1024**3,
        },
        "service": {
            "status": "running",
            "live": True,
            "process_identity_status": "match",
        },
    }


class CapacityTests(unittest.TestCase):
    def test_ready_snapshot_proves_serial_zero_resident_role_contract(self) -> None:
        result = build_capacity_snapshot(**ready_inputs())
        self.assertEqual(result["schema"], "local-company.machine-capacity.v1")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["company"]["execution_parallelism"], 1)
        self.assertEqual(result["company"]["resident_role_processes"], 0)
        self.assertGreaterEqual(result["company"]["registered_roles"], 10)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["advisories"], [])
        self.assertFalse(any(result["effects"].values()))

    def test_retained_quality_failures_are_visible_without_deadlocking_idle_capacity(self) -> None:
        values = ready_inputs()
        values["brief"]["status"] = "attention_required"
        values["brief"]["next_action"] = "review_quality_failures_before_retry"
        result = build_capacity_snapshot(**values)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["advisories"], ["quality_failures_retained_for_review"])
        self.assertEqual(result["company_attention"]["status"], "attention_required")
        self.assertEqual(result["next_action"], "review_quality_failures_before_retry")

    def test_non_quality_company_attention_still_blocks_capacity(self) -> None:
        values = ready_inputs()
        values["brief"]["status"] = "attention_required"
        values["brief"]["next_action"] = "review_pending_approval"
        result = build_capacity_snapshot(**values)
        self.assertEqual(result["status"], "attention_required")
        self.assertIn("company_attention_required", result["blockers"])
        self.assertEqual(result["advisories"], [])

    def test_duplicate_listener_idle_model_and_low_memory_fail_closed(self) -> None:
        values = ready_inputs()
        values["listeners"]["counts"]["5173"] = 2
        values["loaded_models"]["loaded_count"] = 1
        values["memory"]["available_bytes"] = 512 * 1024**2
        result = build_capacity_snapshot(**values)
        self.assertEqual(result["status"], "attention_required")
        self.assertIn("duplicate_listener_5173", result["blockers"])
        self.assertIn("idle_model_loaded", result["blockers"])
        self.assertIn("memory_headroom_below_1gib", result["blockers"])
        self.assertEqual(result["next_action"], "review_duplicate_local_runtime")

    def test_unavailable_observation_is_indeterminate(self) -> None:
        values = ready_inputs()
        values["listeners"] = {
            "status": "unavailable",
            "counts": {"5173": None, "8765": None, "8788": None, "11434": None},
        }
        values["loaded_models"] = {"status": "unavailable", "loaded_count": None}
        result = build_capacity_snapshot(**values)
        self.assertEqual(result["status"], "indeterminate")
        self.assertIn("listener_inventory_unavailable", result["indeterminate"])
        self.assertIn("loaded_model_inventory_unavailable", result["indeterminate"])

    def test_netstat_parser_counts_unique_listener_owners(self) -> None:
        output = "\n".join([
            "  TCP    127.0.0.1:5173       0.0.0.0:0       LISTENING       100",
            "  TCP    127.0.0.1:5173       0.0.0.0:0       LISTENING       100",
            "  TCP    127.0.0.1:5173       0.0.0.0:0       LISTENING       101",
            "  TCP    127.0.0.1:8765       0.0.0.0:0       LISTENING       200",
            "  UDP    127.0.0.1:8788       *:*                             300",
        ])
        self.assertEqual(parse_windows_listeners(output), {
            5173: 2, 8765: 1, 8788: 0, 11434: 0,
        })

    def test_cli_exposes_capacity_without_model_or_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            company = Company(home, MockModel())
            company.create_project("SuperMega")
            expected = build_capacity_snapshot(**ready_inputs())
            argv = [
                "local-company", "--home", str(home), "capacity",
                "--project", "SuperMega",
            ]
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                patch("local_company.cli.machine_capacity_snapshot", return_value=expected),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(stdout.getvalue()), expected)


class PosixMemoryObservationTests(unittest.TestCase):
    def test_meminfo_parser_reports_available_not_free(self) -> None:
        observed = parse_meminfo(
            "MemTotal:        8039084 kB\n"
            "MemFree:          204112 kB\n"
            "MemAvailable:    6120044 kB\n"
            "Buffers:           82304 kB\n"
        )
        self.assertEqual(observed["status"], "ready")
        self.assertEqual(observed["total_bytes"], 8039084 * 1024)
        # MemAvailable, not the far smaller MemFree, is the admission reading.
        self.assertEqual(observed["available_bytes"], 6120044 * 1024)

    def test_meminfo_without_available_field_fails_closed(self) -> None:
        observed = parse_meminfo("MemTotal:        8039084 kB\nMemFree:          204112 kB\n")
        self.assertEqual(
            observed,
            {"status": "unavailable", "total_bytes": None, "available_bytes": None},
        )

    def test_malformed_and_oversized_meminfo_fail_closed(self) -> None:
        for output in (
            "MemTotal:        notanumber kB\nMemAvailable:    6120044 kB\n",
            "MemTotal:        8039084 kB\nMemAvailable:    9999999 kB\n",
            "MemTotal:              0 kB\nMemAvailable:          0 kB\n",
            "MemTotal: 8039084 pages\nMemAvailable: 6120044 pages\n",
            "x" * (64 * 1024 + 1),
        ):
            with self.subTest(output=output[:40]):
                self.assertEqual(parse_meminfo(output)["status"], "unavailable")

    def test_cgroup_v2_limit_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.max").write_text("2147483648", encoding="utf-8")
            (root / "memory.current").write_text("536870912", encoding="utf-8")
            self.assertEqual(
                observe_cgroup_memory(root),
                {"status": "ready", "total_bytes": 2147483648, "available_bytes": 1610612736},
            )

    def test_cgroup_v1_limit_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory").mkdir()
            (root / "memory" / "memory.limit_in_bytes").write_text("4294967296", encoding="utf-8")
            (root / "memory" / "memory.usage_in_bytes").write_text("1073741824", encoding="utf-8")
            self.assertEqual(
                observe_cgroup_memory(root),
                {"status": "ready", "total_bytes": 4294967296, "available_bytes": 3221225472},
            )

    def test_unlimited_or_absent_cgroup_defers_to_host_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(observe_cgroup_memory(root)["status"], "unavailable")
            (root / "memory.max").write_text("max", encoding="utf-8")
            (root / "memory.current").write_text("536870912", encoding="utf-8")
            self.assertEqual(observe_cgroup_memory(root)["status"], "unavailable")
            # cgroup v1 encodes "no limit" as a near-2^63 sentinel.
            (root / "memory.max").write_text("9223372036854771712", encoding="utf-8")
            self.assertEqual(observe_cgroup_memory(root)["status"], "unavailable")

    def test_corrupt_cgroup_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.max").write_text("2147483648", encoding="utf-8")
            (root / "memory.current").write_text("4294967296", encoding="utf-8")
            # Usage above the limit is impossible; refuse rather than report a negative.
            self.assertEqual(observe_cgroup_memory(root)["status"], "unavailable")
            (root / "memory.current").write_text("not-a-number", encoding="utf-8")
            self.assertEqual(observe_cgroup_memory(root)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
