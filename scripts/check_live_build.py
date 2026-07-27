"""Compare the checked local build manifest with the running loopback service."""

from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__:
    from .stamp_build_manifest import ManifestError, check_project, parse_build_id
else:
    from stamp_build_manifest import ManifestError, check_project, parse_build_id


BUILD_STATUS_URL = "http://127.0.0.1:8765/health.json?view=build-status"
MAX_BUILD_STATUS_BYTES = 64 * 1024
BUILD_FIELDS = ("schema", "package_version", "build_id", "source_sha256")
NULLABLE_BUILD_FIELDS = ("git_commit", "source_dirty")
IDLE_FIELDS = (
    "active_jobs",
    "queued_missions",
    "running_missions",
    "pending_approvals",
    "pending_report_finalizations",
    "pending_evaluations",
)


class LiveBuildError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def fetch_health() -> dict[str, object]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        BUILD_STATUS_URL,
        headers={"Accept": "application/json", "User-Agent": "local-company-build-check/1"},
    )
    try:
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise LiveBuildError("Loopback health endpoint did not return HTTP 200")
            body = response.read(MAX_BUILD_STATUS_BYTES + 1)
        if len(body) > MAX_BUILD_STATUS_BYTES:
            raise LiveBuildError("Loopback build-status response exceeds its byte limit")
        payload = json.loads(body.decode("utf-8", errors="strict"))
    except LiveBuildError:
        raise
    except (
        OSError, UnicodeError, ValueError, RecursionError, urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        raise LiveBuildError(f"Could not read loopback health: {exc}") from exc
    if not isinstance(payload, dict):
        raise LiveBuildError("Loopback health response is not a JSON object")
    return payload


def compare_live_build(
    disk: dict[str, object], health: dict[str, object],
) -> dict[str, object]:
    if health.get("status") != "ready":
        raise LiveBuildError("Local service is not ready")
    pid = health.get("pid")
    live = health.get("build")
    counters = health.get("health")
    worker = health.get("worker")
    if type(pid) is not int or pid <= 0:
        raise LiveBuildError("Loopback health has no valid service PID")
    if (
        not isinstance(live, dict)
        or not isinstance(counters, dict)
        or not isinstance(worker, dict)
    ):
        raise LiveBuildError("Loopback health is missing build or work-state metadata")

    mismatches = []
    disk_build: dict[str, object] = {}
    live_build: dict[str, object] = {}
    for field in BUILD_FIELDS:
        disk_value = disk.get(field)
        live_value = live.get(field)
        if type(disk_value) is not str or type(live_value) is not str:
            raise LiveBuildError(f"Build field {field} is missing or malformed")
        disk_build[field] = disk_value
        live_build[field] = live_value
        if disk_value != live_value:
            mismatches.append(field)
    if live_build["schema"] != disk_build["schema"]:
        raise LiveBuildError("Build field schema is unsupported by the checked disk build")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.!+_-]{0,127}", live_build["package_version"]):
        raise LiveBuildError("Build field package_version is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", live_build["source_sha256"]):
        raise LiveBuildError("Build field source_sha256 is malformed")
    git_commit = live.get("git_commit")
    source_dirty = live.get("source_dirty")
    if git_commit is not None and not (
        type(git_commit) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git_commit)
    ):
        raise LiveBuildError("Build field git_commit is malformed")
    if source_dirty is not None and type(source_dirty) is not bool:
        raise LiveBuildError("Build field source_dirty is malformed")
    disk_build["git_commit"] = None
    disk_build["source_dirty"] = None
    live_build["git_commit"] = git_commit
    live_build["source_dirty"] = source_dirty
    for field in NULLABLE_BUILD_FIELDS:
        if live_build[field] != disk_build[field]:
            mismatches.append(field)

    work_state: dict[str, object] = {}
    legacy_status_payload = "running_missions" not in counters
    if legacy_status_payload:
        legacy_queue = health.get("queue")
        if not isinstance(legacy_queue, list):
            raise LiveBuildError("Work-state field running_missions is missing or malformed")
        queued_missions = 0
        running_missions = 0
        for row in legacy_queue:
            if not isinstance(row, list) or len(row) < 2 or type(row[1]) is not str:
                raise LiveBuildError("Legacy queue work-state metadata is malformed")
            queue_status = row[1]
            if queue_status not in {
                "queued", "running", "cancelled", "failed", "needs_approval",
                "complete", "quality_failed",
            }:
                raise LiveBuildError("Legacy queue status is malformed")
            queued_missions += queue_status == "queued"
            running_missions += queue_status == "running"
        reported_queued = counters.get("queued_missions")
        if type(reported_queued) is not int or reported_queued < 0:
            raise LiveBuildError("Work-state field queued_missions is missing or malformed")
        counters = dict(
            counters,
            queued_missions=max(reported_queued, queued_missions),
            running_missions=running_missions,
        )
    for field in IDLE_FIELDS:
        value = counters.get(field)
        if type(value) is not int or value < 0:
            raise LiveBuildError(f"Work-state field {field} is missing or malformed")
        work_state[field] = value
    worker_status = worker.get("status")
    if type(worker_status) is not str or worker_status not in {
        "idle", "running", "disabled", "complete", "quality_failed",
        "needs_approval", "failed", "completion_pending",
    }:
        raise LiveBuildError("Worker status is missing or malformed")
    work_state["worker_status"] = worker_status
    idle = (
        all(work_state[field] == 0 for field in IDLE_FIELDS)
        and worker_status not in {"running", "completion_pending", "needs_approval"}
    )
    try:
        disk_order = parse_build_id(disk_build["build_id"])
        live_order = parse_build_id(live_build["build_id"])
    except ManifestError as exc:
        raise LiveBuildError("Live or disk build ID is malformed") from exc
    if not mismatches:
        status = "match"
        restart_required = False
        recommendation = "No restart is required."
    elif disk_order > live_order:
        status = "restart_required"
        restart_required = True
        recommendation = (
            "Restart the local service, then rerun this check."
            if idle else
            "Wait for local work to become idle before restarting the service."
        )
    elif disk_order < live_order:
        status = "disk_older"
        restart_required = False
        recommendation = "Do not restart: the checked disk build is older than the live build."
    else:
        status = "identity_conflict"
        restart_required = False
        recommendation = "Do not restart automatically: inspect the same-ID build mismatch."
    return {
        "status": status,
        "service_pid": pid,
        "restart_required": restart_required,
        "restart_safe_now": idle,
        "mismatched_fields": mismatches,
        "disk_build": disk_build,
        "live_build": live_build,
        "work_state": work_state,
        "legacy_status_payload": legacy_status_payload,
        "recommendation": recommendation,
    }


def main() -> int:
    try:
        project_root = Path(__file__).resolve().parents[1]
        disk = check_project(project_root)
    except (ManifestError, OSError) as exc:
        print(json.dumps({
            "status": "error",
            "stage": "disk_manifest",
            "error": str(exc),
        }, sort_keys=True), file=sys.stderr)
        return 3
    try:
        result = compare_live_build(disk, fetch_health())
    except LiveBuildError as exc:
        print(json.dumps({
            "status": "error",
            "stage": "live_health",
            "error": str(exc),
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "match" else 1


if __name__ == "__main__":
    raise SystemExit(main())
