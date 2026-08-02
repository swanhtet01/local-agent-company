from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .select_local_code_model import GIB, available_memory_bytes
except ImportError:
    from select_local_code_model import GIB, available_memory_bytes


SCHEMA = "local-ai.launchpad.v1"
WORK_RESULT_SCHEMA = "local-ai.work-result.v1"
CYCLE_RESULT_SCHEMA = "local-ai.cycle-result.v1"
JOB_ID_PATTERN = re.compile(r"^Completed job ([0-9a-f]{12})$", re.MULTILINE)
QUEUE_COMPLETION_PATTERN = re.compile(
    r"^Queue item ([0-9a-f]{12}) completed as job ([0-9a-f]{12}); quality=(passed|failed)$",
    re.MULTILINE,
)
SCHEDULE_TICK_PATTERN = re.compile(r"^Materialized (\d+) due schedule\(s\)\.$", re.MULTILINE)
CYCLE_MINIMUM_AVAILABLE_BYTES = 2 * GIB


@dataclass(frozen=True)
class LaunchAction:
    command: tuple[str, ...]
    mode: str
    description: str
    model_may_run: bool
    local_state_may_change: bool
    external_action_allowed: bool = False


HELP = r"""Local AI Launchpad

Use one command for local coding, business teams, research, planning, and queued work.

  local-ai.cmd check [model options]          Check Ollama and the configured model
  local-ai.cmd code [PROJECT_PATH]            Open a local coding agent in a project
  local-ai.cmd vision [--check] [PROJECT]     Use the full local Vision product agent
  local-ai.cmd vision-lite [--check] [PROJECT] Use the tiny Vision campaign agent
  local-ai.cmd plan "OBJECTIVE" [options]     Preview the team and gates; no model
  local-ai.cmd work "OBJECTIVE" [options]     Run one bounded local AI team
  local-ai.cmd later "OBJECTIVE" [options]    Add work to the durable local queue
  local-ai.cmd next [--queue-id ID]           Preview the exact next queued mission
  local-ai.cmd run-next [options]              Run one due queued mission
  local-ai.cmd cycle [model options]           Materialize and run at most one mission
  local-ai.cmd autopilot install|status|remove Manage the six-hour local cycle task
  local-ai.cmd dashboard [options]             Start the local dashboard on 127.0.0.1
  local-ai.cmd dashboard-status                Check the dashboard service
  local-ai.cmd stop                            Stop the verified local dashboard
  local-ai.cmd status                          Show machine, model, and queue health
  local-ai.cmd jobs                            List mission history
  local-ai.cmd new "PROJECT" [--description]  Create a general project workspace
  local-ai.cmd use "PROJECT"                  Select it for model-backed work
  local-ai.cmd projects ...                    Create, list, or inspect projects
  local-ai.cmd knowledge ...                   Add or search project reference files
  local-ai.cmd data ...                        Profile local datasets read-only
  local-ai.cmd playbooks ...                   List reusable specialist teams
  local-ai.cmd roles                           List available business roles
  local-ai.cmd evidence ...                    Record and inspect product proof
  local-ai.cmd explain COMMAND ...             Show effects without running it
  local-ai.cmd company ...                     Access the complete advanced CLI

Examples:
  local-ai.cmd code C:\path\to\a-project
  local-ai.cmd vision-lite --check
  local-ai.cmd vision-lite
  local-ai.cmd plan "Design a product customers can buy" --project "New Product"
  local-ai.cmd work "Create a 30-day launch plan" --project "New Product"
  local-ai.cmd later "Review pricing and customer risks" --project "New Product"
  local-ai.cmd new "New Product" --description "Private product R&D"
  local-ai.cmd use "New Product"
  local-ai.cmd dashboard

Everything runs locally by default. Sensitive objectives stop at an owner gate.
No command silently sends messages, spends money, deploys, or exposes a model server.
"""


def _require_tail(name: str, values: list[str]) -> list[str]:
    if not values or not values[0].strip():
        raise ValueError(f"{name}_objective_required")
    return values


def translate(argv: list[str]) -> LaunchAction | None:
    if not argv or argv[0].lower() in {"help", "-h", "--help"}:
        return None
    name = argv[0].lower()
    tail = argv[1:]
    if name == "check":
        return LaunchAction(("doctor", *tail), "check", "Check the local model dependency without generating text.", False, False)
    if name == "plan":
        return LaunchAction(("preflight", *_require_tail(name, tail)), "plan", "Preview the team, evidence, and owner gates without starting work.", False, False)
    if name == "work":
        return LaunchAction(("run", *_require_tail(name, tail)), "work", "Run one bounded local team and write its auditable report.", True, True)
    if name == "later":
        return LaunchAction(("queue", "add", *_require_tail(name, tail)), "later", "Record one mission in the durable local queue without running it.", False, True)
    if name == "next":
        return LaunchAction(("queue", "preflight", *tail), "next", "Preview the exact next due mission without claiming it.", False, False)
    if name == "run-next":
        return LaunchAction(("queue", "run-next", *tail), "run-next", "Claim and run at most one due local mission.", True, True)
    if name == "cycle":
        if "--queue-id" in tail:
            raise ValueError("cycle_queue_id_is_selected_by_verified_preflight")
        return LaunchAction(tuple(tail), "cycle", "Materialize due schedules and run at most one exact, gate-cleared local mission.", True, True)
    if name == "autopilot":
        if len(tail) != 1 or tail[0].lower() not in {"install", "status", "remove"}:
            raise ValueError("autopilot_action_required")
        operation = tail[0].lower()
        return LaunchAction(
            (operation,), "autopilot",
            f"{operation.title()} the verified six-hour local cycle task.",
            False, operation != "status",
        )
    if name == "dashboard":
        return LaunchAction(("service", "start", *tail), "dashboard", "Start the owner-controlled loopback dashboard and one local worker.", False, True)
    if name == "dashboard-status":
        return LaunchAction(("service", "status", *tail), "dashboard-status", "Verify the recorded loopback dashboard process.", False, False)
    if name == "stop":
        return LaunchAction(("service", "stop", *tail), "stop", "Stop only the verified local dashboard process.", False, True)
    if name == "status":
        return LaunchAction(("health", *tail), "status", "Show bounded local runtime, queue, and model health.", False, False)
    if name == "jobs":
        return LaunchAction(("status", *tail), "jobs", "List local mission history.", False, False)
    if name == "new":
        return LaunchAction(("projects", "create", *_require_tail(name, tail)), "new", "Create one durable general project workspace.", False, True)
    if name == "use":
        if len(tail) != 1 or not tail[0].strip():
            raise ValueError("use_project_required")
        return LaunchAction((tail[0],), "use", "Select one existing project for bounded model-backed work.", False, True)
    passthrough = {
        "projects": "projects", "knowledge": "knowledge", "data": "datasets",
        "playbooks": "playbooks", "roles": "roles", "approvals": "approvals",
        "schedules": "schedules", "focus": "focus", "export": "export",
        "evidence": "evidence",
    }
    if name in passthrough:
        mutating = name in {"projects", "knowledge", "approvals", "schedules", "focus", "export", "evidence"}
        return LaunchAction((passthrough[name], *tail), name, f"Use the {name} capability.", False, mutating)
    if name == "company":
        if not tail:
            raise ValueError("company_command_required")
        model_may_run = tail[0] in {"run", "retry", "resume"} or tail[:2] == ["queue", "run-next"]
        return LaunchAction(tuple(tail), "advanced", "Use the complete Local Agent Company CLI.", model_may_run, True)
    if name == "code":
        return LaunchAction(tuple(tail), "code", "Open OpenCode in one local project through Ollama.", True, False)
    if name == "vision":
        return LaunchAction(("--vision", *tail), "vision", "Open the governed Vision product agent through the bounded local 4B runtime.", True, False)
    if name == "vision-lite":
        return LaunchAction(("--vision-lite", *tail), "vision-lite", "Open the three-tool Vision campaign agent through Ollama.", True, False)
    raise ValueError("launchpad_command_unknown")


def explain(action: LaunchAction) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "mode": action.mode,
        "description": action.description,
        "translatedCommand": list(action.command),
        "effects": {
            "modelMayRun": action.model_may_run,
            "localStateMayChange": action.local_state_may_change,
            "externalActionAllowed": action.external_action_allowed,
            "paidApiRequired": False,
        },
    }


def run_company(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    environment = os.environ.copy()
    source = str(project_root / "src")
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source if not current else os.pathsep.join((source, current))
    completed = subprocess.run(
        [sys.executable, "-m", "local_company.cli", *action.command],
        cwd=project_root,
        env=environment,
        check=False,
    )
    return completed.returncode


def run_code(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    command = list(action.command)
    if action.mode in {"vision", "vision-lite"}:
        default_project = str(project_root.parent / "supermega-vision")
        if len(command) == 1 or command[1:] == ["--check"]:
            command.append(default_project)
    completed = subprocess.run(
        [str(project_root / "local-code.cmd"), *command],
        cwd=project_root, check=False,
    )
    return completed.returncode


def run_autopilot(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    mode = action.command[0].title()
    completed = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(project_root / "scripts" / "manage_cycle_task.ps1"),
            "-Mode", mode,
        ],
        cwd=project_root, check=False,
    )
    return completed.returncode


def run_work(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    environment = os.environ.copy()
    source = str(project_root / "src")
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source if not current else os.pathsep.join((source, current))
    completed = subprocess.run(
        [sys.executable, "-m", "local_company.cli", *action.command],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return completed.returncode
    matched = JOB_ID_PATTERN.search(completed.stdout)
    if not matched:
        print(json.dumps({"schema": WORK_RESULT_SCHEMA, "ok": False, "reason": "completed_job_id_missing"}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    job_id = matched.group(1)
    inspected = subprocess.run(
        [sys.executable, "-m", "local_company.cli", "show", job_id],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        sys.stderr.write(inspected.stderr)
        print(json.dumps({"schema": WORK_RESULT_SCHEMA, "ok": False, "jobId": job_id, "reason": "quality_receipt_unavailable"}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    try:
        detail = json.loads(inspected.stdout)
        evaluation = detail["evaluation"]
        passed = evaluation["passed"] is True
        score = evaluation["score"]
        report_path = detail["job"][4]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        print(json.dumps({"schema": WORK_RESULT_SCHEMA, "ok": False, "jobId": job_id, "reason": "quality_receipt_invalid"}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    receipt = {
        "schema": WORK_RESULT_SCHEMA,
        "ok": passed,
        "jobId": job_id,
        "executionCompleted": True,
        "qualityPassed": passed,
        "qualityScore": score,
        "report": report_path,
        "recommendedAction": "review_accepted_report" if passed else "review_failure_then_retry_with_better_model_or_tighter_task",
        "modelUnloadedAfterRun": evaluation.get("checks", {}).get("model_stopped_cleanly") is True,
        "externalActionPerformed": False,
    }
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return 0 if passed else 1


def _company_process(
    command: list[str], project_root: Path, environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "local_company.cli", *command],
        cwd=project_root, env=environment, check=False,
        capture_output=True, text=True,
    )


def _cycle_receipt(**values: object) -> dict[str, object]:
    return {
        "schema": CYCLE_RESULT_SCHEMA,
        "externalActionPerformed": False,
        "paidApiUsed": False,
        "missionsRun": 0,
        "modelCalled": False,
        **values,
    }


def run_cycle(action: LaunchAction, root: Path | None = None) -> int:
    """Materialize due schedules and execute no more than one verified queue item."""
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    environment = os.environ.copy()
    source = str(project_root / "src")
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source if not current else os.pathsep.join((source, current))

    tick = _company_process(["schedules", "tick"], project_root, environment)
    tick_match = SCHEDULE_TICK_PATTERN.search(tick.stdout) if tick.returncode == 0 else None
    if tick.returncode != 0 or tick_match is None:
        sys.stderr.write(tick.stderr)
        print(json.dumps(_cycle_receipt(
            ok=False, status="error",
            reason="schedule_tick_failed" if tick.returncode else "schedule_tick_receipt_invalid",
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return tick.returncode or 2
    materialized = int(tick_match.group(1))

    preview = _company_process(["queue", "preflight"], project_root, environment)
    try:
        preflight = json.loads(preview.stdout)
        status = preflight["status"]
        queue_id = preflight["queue_id"]
        schema_valid = preflight["schema"] == "local-company.queue-preflight.v1"
    except (KeyError, TypeError, json.JSONDecodeError):
        status, queue_id, schema_valid = None, None, False
    if preview.returncode != 0 or not schema_valid or status not in {
        "no_due_mission", "blocked", "owner_gate_required", "ready",
    }:
        sys.stderr.write(preview.stderr)
        print(json.dumps(_cycle_receipt(
            ok=False, status="error", reason="queue_preflight_invalid",
            schedulesMaterialized=materialized,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return preview.returncode or 2
    if status != "ready":
        blockers = preflight.get("blockers")
        owner_gates = preflight.get("owner_gate_categories")
        print(json.dumps(_cycle_receipt(
            ok=True, status=status, reason=status,
            schedulesMaterialized=materialized,
            blockers=blockers if isinstance(blockers, list) else [],
            ownerGateCategories=owner_gates if isinstance(owner_gates, list) else [],
        ), separators=(",", ":"), sort_keys=True))
        return 0
    if (
        not isinstance(queue_id, str)
        or re.fullmatch(r"[0-9a-f]{12}", queue_id) is None
        or preflight.get("submission_allowed") is not True
        or preflight.get("model_execution_ready") is not True
        or preflight.get("owner_gate_categories") != []
    ):
        print(json.dumps(_cycle_receipt(
            ok=False, status="error", reason="ready_preflight_inconsistent",
            schedulesMaterialized=materialized,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2

    bound = _company_process(
        ["queue", "preflight", "--queue-id", queue_id], project_root, environment,
    )
    try:
        bound_preflight = json.loads(bound.stdout)
        bound_ready = (
            bound_preflight["schema"] == "local-company.queue-preflight.v1"
            and bound_preflight["status"] == "ready"
            and bound_preflight["queue_id"] == queue_id
            and bound_preflight["reviewed_queue_matches"] is True
            and bound_preflight["submission_allowed"] is True
            and bound_preflight["model_execution_ready"] is True
            and bound_preflight["owner_gate_categories"] == []
        )
    except (KeyError, TypeError, json.JSONDecodeError):
        bound_ready = False
    if bound.returncode != 0 or not bound_ready:
        sys.stderr.write(bound.stderr)
        print(json.dumps(_cycle_receipt(
            ok=False, status="blocked", reason="bound_preflight_changed",
            schedulesMaterialized=materialized, queueId=queue_id,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2

    try:
        available = available_memory_bytes()
    except RuntimeError:
        print(json.dumps(_cycle_receipt(
            ok=True, status="blocked", reason="available_memory_unavailable",
            schedulesMaterialized=materialized, queueId=queue_id,
            minimumAvailableBytes=CYCLE_MINIMUM_AVAILABLE_BYTES,
        ), separators=(",", ":"), sort_keys=True))
        return 0
    if available < CYCLE_MINIMUM_AVAILABLE_BYTES:
        print(json.dumps(_cycle_receipt(
            ok=True, status="blocked", reason="insufficient_available_memory",
            schedulesMaterialized=materialized, queueId=queue_id,
            availableMemoryBytes=available,
            minimumAvailableBytes=CYCLE_MINIMUM_AVAILABLE_BYTES,
            memoryShortfallBytes=CYCLE_MINIMUM_AVAILABLE_BYTES - available,
            recommendedAction="close_large_apps_then_run_cycle",
        ), separators=(",", ":"), sort_keys=True))
        return 0

    executed = _company_process(
        ["queue", "run-next", "--queue-id", queue_id, *action.command],
        project_root, environment,
    )
    sys.stdout.write(executed.stdout)
    sys.stderr.write(executed.stderr)
    completion = QUEUE_COMPLETION_PATTERN.search(executed.stdout)
    if executed.returncode != 0 or completion is None or completion.group(1) != queue_id:
        print(json.dumps(_cycle_receipt(
            ok=False, status="execution_failed",
            reason="queue_execution_failed" if executed.returncode else "queue_completion_receipt_invalid",
            schedulesMaterialized=materialized, queueId=queue_id,
            missionsRun=None, modelCalled=None,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return executed.returncode or 2
    job_id = completion.group(2)
    inspected = _company_process(["show", job_id], project_root, environment)
    try:
        detail = json.loads(inspected.stdout)
        evaluation = detail["evaluation"]
        passed = evaluation["passed"] is True
        score = evaluation["score"]
        report_path = detail["job"][4]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        print(json.dumps(_cycle_receipt(
            ok=False, status="error", reason="quality_receipt_invalid",
            schedulesMaterialized=materialized, queueId=queue_id, jobId=job_id,
            missionsRun=1, modelCalled=True,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(_cycle_receipt(
        ok=passed,
        status="completed" if passed else "quality_failed",
        schedulesMaterialized=materialized, queueId=queue_id, jobId=job_id,
        missionsRun=1, modelCalled=True, qualityPassed=passed, qualityScore=score,
        report=report_path,
        modelUnloadedAfterRun=evaluation.get("checks", {}).get("model_stopped_cleanly") is True,
        recommendedAction="review_accepted_report" if passed else "review_failure_before_any_retry",
    ), separators=(",", ":"), sort_keys=True))
    return 0 if passed else 1


def switch_project(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    environment = os.environ.copy()
    source = str(project_root / "src")
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = source if not current else os.pathsep.join((source, current))
    observed = subprocess.run(
        [sys.executable, "-m", "local_company.cli", "focus", "show"],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if observed.returncode != 0:
        sys.stdout.write(observed.stdout)
        sys.stderr.write(observed.stderr)
        return observed.returncode
    try:
        receipt = json.loads(observed.stdout)
        focus = receipt["focus"]
        digest = receipt["focusDigest"]
        enabled = focus["enabled"] is True
    except (KeyError, TypeError, json.JSONDecodeError):
        print(json.dumps({"schema": SCHEMA, "ok": False, "reason": "focus_observation_invalid"}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    target = action.command[0]
    if enabled and target in {focus.get("projectId"), focus.get("projectName")}:
        print(observed.stdout, end="" if observed.stdout.endswith("\n") else "\n")
        return 0
    if enabled:
        mutation = LaunchAction((
            "focus", "handoff",
            "--from-project", str(focus.get("projectId", "")),
            "--project", target,
            "--max-roles", "4",
            "--expected-focus-digest", str(digest),
            "--reason", "Explicit local-ai use command selected a new active project.",
            "--confirm", "HANDOFF ACTIVE EXECUTION FOCUS",
        ), "use", action.description, False, True)
    else:
        mutation = LaunchAction(("focus", "set", "--project", target, "--max-roles", "4"), "use", action.description, False, True)
    return run_company(mutation, project_root)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0].lower() == "explain":
            action = translate(args[1:])
            if action is None:
                raise ValueError("explain_command_required")
            print(json.dumps(explain(action), separators=(",", ":"), sort_keys=True))
            return 0
        action = translate(args)
        if action is None:
            print(HELP)
            return 0
        if action.mode in {"code", "vision", "vision-lite"}:
            return run_code(action)
        if action.mode == "autopilot":
            return run_autopilot(action)
        if action.mode == "use":
            return switch_project(action)
        if action.mode == "work":
            return run_work(action)
        if action.mode == "cycle":
            return run_cycle(action)
        return run_company(action)
    except ValueError as error:
        print(json.dumps({"schema": SCHEMA, "ok": False, "reason": str(error)}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
