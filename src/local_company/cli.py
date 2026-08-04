from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .capacity import machine_capacity_snapshot
from .browser_operator import browser_doctor, run_browser_check, run_supermega_release_suite
from .computer_use import (
    RUN_CONFIRMATION,
    WindowsDesktopAdapter,
    capture_screen,
    inspect_window,
    learn_workflow,
    launch_workflow_lab,
    list_workflows,
    load_workflow,
    preview_workflow,
    prove_computer_use,
    run_workflow,
    windows_observation,
)
from .config import default_company_home
from .core import Company, MockModel, OllamaModel, PLAYBOOKS, ROLES
from .focus import (
    EXECUTION_FOCUS_HANDOFF_CONFIRMATION,
    EXECUTION_FOCUS_MAX_ROLES,
    clear_execution_focus,
    enforce_execution_focus,
    enforce_execution_resource_envelope,
    execution_focus_digest,
    handoff_execution_focus,
    read_execution_focus,
    set_execution_focus,
)
from .supermega import (
    create_vision_founding_pilot_packages,
    create_vision_sales_intake,
    create_vision_sales_intake_fields,
    create_vision_prospect_drafts,
    import_vision_prospects,
    run_vision_sales,
    vision_commercial_status,
    vision_product_status,
    vision_sales_status,
)


DEFAULT_PROVIDER = os.getenv("LOCAL_COMPANY_PROVIDER", "ollama")
DEFAULT_MODEL = os.getenv("LOCAL_COMPANY_MODEL", "qwen3.5:0.8b")


def find_ollama_executable() -> str | None:
    candidates = [
        shutil.which("ollama"),
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"),
        str(Path(os.getenv("ProgramFiles", "")) / "Ollama" / "ollama.exe"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="local-company", description="Run a local, owner-gated AI team")
    p.add_argument("--home", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create or upgrade the local company database")
    sub.add_parser("roles", help="List available company roles")
    computer = sub.add_parser(
        "computer", help="Learn, inspect, preview, and replay local Windows workflows"
    )
    computer_sub = computer.add_subparsers(dest="computer_command", required=True)
    computer_sub.add_parser("doctor", help="Check zero-download Windows computer-use readiness")
    computer_sub.add_parser("lab", help="Open a zero-file local app for safe workflow practice")
    computer_prove = computer_sub.add_parser(
        "prove", help="Visibly prove local click, typing, verification, and evidence end to end"
    )
    computer_prove.add_argument("--confirm", required=True, choices=[RUN_CONFIRMATION])
    computer_windows = computer_sub.add_parser("windows", help="List visible local windows")
    computer_windows.add_argument("--limit", type=int, default=50)
    computer_inspect = computer_sub.add_parser(
        "inspect", help="Inspect bounded Windows UI Automation controls in one visible window"
    )
    computer_inspect.add_argument("--title", required=True)
    computer_inspect.add_argument("--limit", type=int, default=100)
    computer_capture = computer_sub.add_parser(
        "capture", help="Capture one explicit local training screenshot"
    )
    computer_capture.add_argument("output", type=Path)
    computer_learn = computer_sub.add_parser(
        "learn", help="Learn clicks, safe keys, and private text placeholders from a demonstration"
    )
    computer_learn.add_argument("name")
    computer_learn.add_argument("--seconds", type=int, default=60)
    computer_learn.add_argument(
        "--screenshots", action="store_true",
        help="Capture post-demonstration app-window references; may contain visible private data",
    )
    computer_learn.add_argument("--expect-window-title")
    computer_learn.add_argument("--expect-control-text")
    computer_sub.add_parser("list", help="List sealed learned workflows")
    computer_show = computer_sub.add_parser("show", help="Show one sealed workflow")
    computer_show.add_argument("name")
    computer_preview = computer_sub.add_parser(
        "preview", help="Resolve every window and target without controlling the computer"
    )
    computer_preview.add_argument("name")
    computer_preview.add_argument("--allow-network-apps", action="store_true")
    computer_preview.add_argument("--allow-shells", action="store_true")
    computer_run = computer_sub.add_parser(
        "run", help="Replay one preview-ready workflow after exact local confirmation"
    )
    computer_run.add_argument("name")
    computer_run.add_argument("--confirm", required=True, choices=[RUN_CONFIRMATION])
    computer_run.add_argument("--allow-network-apps", action="store_true")
    computer_run.add_argument("--allow-shells", action="store_true")
    computer_run.add_argument("--no-evidence", action="store_true")
    browser = sub.add_parser(
        "browser", help="Run model-free, read-only website checks with local evidence"
    )
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)
    browser_sub.add_parser("doctor", help="Live-check the local browser automation runtime")
    browser_supermega = browser_sub.add_parser(
        "supermega-release", help="Check all four public SuperMega product setup pages"
    )
    browser_supermega.add_argument("--runs", type=int, default=1)
    browser_supermega.add_argument("--timeout-seconds", type=int, default=45)
    browser_check = browser_sub.add_parser(
        "check", help="Inspect one website and write a pass/fail evidence pack"
    )
    browser_check.add_argument("url")
    browser_check.add_argument("--expect-title")
    browser_check.add_argument("--expect-text", action="append", default=[])
    browser_check.add_argument("--fail-on-console-errors", action="store_true")
    browser_check.add_argument("--max-a11y-violations", type=int)
    browser_check.add_argument("--timeout-seconds", type=int, default=30)
    supermega = sub.add_parser("supermega", help="Run bounded local SuperMega operating capabilities")
    supermega_sub = supermega.add_subparsers(dest="supermega_command", required=True)
    vision_sales = supermega_sub.add_parser("vision-sales", help="Process local Vision leads into owner-review sales drafts")
    vision_sales.add_argument("--platform-root", type=Path)
    vision_sales.add_argument("--sales-root", type=Path)
    vision_status = supermega_sub.add_parser("vision-sales-status", help="Inspect the local Vision sales pipeline without processing it")
    vision_status.add_argument("--sales-root", type=Path)
    vision_intake = supermega_sub.add_parser("vision-sales-intake", help="Validate one local prospect file and queue a Vision contact event")
    vision_intake_source = vision_intake.add_mutually_exclusive_group(required=True)
    vision_intake_source.add_argument("--input", type=Path)
    vision_intake_source.add_argument("--interactive", action="store_true", help="Prompt locally without placing prospect data in command arguments")
    vision_intake.add_argument("--sales-root", type=Path)
    vision_prospect_import = supermega_sub.add_parser("vision-prospect-import", help="Import researched prospects without qualifying or contacting them")
    vision_prospect_import.add_argument("--input", type=Path, required=True)
    vision_prospect_import.add_argument("--sales-root", type=Path)
    vision_prospect_drafts = supermega_sub.add_parser("vision-prospect-drafts", help="Create integrity-checked local outreach drafts without sending them")
    vision_prospect_drafts.add_argument("--sales-root", type=Path)
    vision_pilot_packages = supermega_sub.add_parser(
        "vision-founding-pilot-packages",
        help="Create claim-safe internal founding-pilot packages without sending or pricing",
    )
    vision_pilot_packages.add_argument("--sales-root", type=Path)
    vision_product = supermega_sub.add_parser(
        "vision-product-status",
        help="Cross-check local Vision readiness and commercial-claim gates",
    )
    vision_product.add_argument("--product-root", type=Path)
    vision_commercial = supermega_sub.add_parser(
        "vision-commercial-status",
        help="Combine local Vision product and sales evidence into one offer gate",
    )
    vision_commercial.add_argument("--product-root", type=Path)
    vision_commercial.add_argument("--sales-root", type=Path)
    focus = sub.add_parser("focus", help="Constrain model-backed work to one project and role budget")
    focus_sub = focus.add_subparsers(dest="focus_command", required=True)
    focus_set = focus_sub.add_parser("set", help="Activate one local execution focus")
    focus_set.add_argument("--project", required=True)
    focus_set.add_argument("--max-roles", type=int, choices=range(1, EXECUTION_FOCUS_MAX_ROLES + 1), default=4)
    focus_handoff = focus_sub.add_parser("handoff", help="Atomically switch one active focus")
    focus_handoff.add_argument("--from-project", required=True)
    focus_handoff.add_argument("--project", required=True)
    focus_handoff.add_argument("--max-roles", type=int, choices=range(1, EXECUTION_FOCUS_MAX_ROLES + 1), default=4)
    focus_handoff.add_argument("--expected-focus-digest", required=True)
    focus_handoff.add_argument("--reason", required=True)
    focus_handoff.add_argument("--confirm", required=True, choices=[EXECUTION_FOCUS_HANDOFF_CONFIRMATION])
    focus_sub.add_parser("show", help="Show the bounded pathless execution focus")
    focus_clear = focus_sub.add_parser("clear", help="Disable active focus with a digest-bound audit record")
    focus_clear.add_argument("--expected-focus-digest", required=True)
    focus_clear.add_argument("--reason", required=True)
    focus_clear.add_argument("--confirm", required=True, choices=[EXECUTION_FOCUS_HANDOFF_CONFIRMATION])
    route = sub.add_parser(
        "route", help="Preview an automatic or playbook team without calling a model"
    )
    route.add_argument("objective")
    route.add_argument("--playbook", choices=tuple(PLAYBOOKS))
    preflight = sub.add_parser(
        "preflight",
        help="Preview team, owner gates, and aggregate knowledge readiness without queuing",
    )
    preflight.add_argument("objective")
    preflight.add_argument("--project")
    preflight.add_argument("--playbook", choices=tuple(PLAYBOOKS))

    run = sub.add_parser("run", help="Route and run a team on one objective")
    run.add_argument("objective")
    run.add_argument("--roles", help="Comma-separated roles; omit for automatic routing")
    add_runtime_args(run)
    run.add_argument("--project", help="Project name or ID; restricts retrieval and groups the report")

    projects = sub.add_parser("projects", help="Manage durable project workspaces")
    project_sub = projects.add_subparsers(dest="project_command", required=True)
    create_project = project_sub.add_parser("create", help="Create a project")
    create_project.add_argument("name")
    create_project.add_argument("--description", default="")
    project_sub.add_parser("list", help="List projects")
    show_project = project_sub.add_parser("show", help="Show project sources and missions")
    show_project.add_argument("project")

    playbooks = sub.add_parser("playbooks", help="Inspect reusable cross-functional team patterns")
    playbook_sub = playbooks.add_subparsers(dest="playbook_command", required=True)
    playbook_sub.add_parser("list", help="List built-in playbooks")
    show_playbook = playbook_sub.add_parser("show", help="Show one playbook")
    show_playbook.add_argument("name", choices=tuple(PLAYBOOKS))

    queue = sub.add_parser("queue", help="Manage the durable local mission queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_add = queue_sub.add_parser("add", help="Queue one mission without running it")
    queue_add.add_argument("objective")
    queue_add.add_argument("--project")
    queue_add.add_argument("--roles", help="Comma-separated explicit team")
    queue_add.add_argument("--playbook", choices=tuple(PLAYBOOKS))
    queue_add.add_argument("--priority", type=int, default=50)
    queue_add.add_argument("--scheduled-at", help="ISO-8601 timestamp; naive values are treated as UTC")
    queue_list = queue_sub.add_parser("list", help="List queued mission records")
    queue_list.add_argument("--status")
    queue_preflight = queue_sub.add_parser(
        "preflight", help="Preview the exact next claim gates without starting work"
    )
    queue_preflight.add_argument(
        "--queue-id", help="Compare with this exact reviewed mission ID"
    )
    queue_retry_preflight = queue_sub.add_parser(
        "retry-preflight",
        help="Preview one failed item's current-evidence retry gates without resetting it",
    )
    queue_retry_preflight.add_argument("queue_id")
    queue_run = queue_sub.add_parser(
        "run-next", help="Run the highest-priority due mission, optionally by reviewed ID"
    )
    queue_run.add_argument(
        "--queue-id", help="Fail unless this exact reviewed mission is still next"
    )
    add_runtime_args(queue_run)
    queue_reset = queue_sub.add_parser("reset", help="Return an incomplete queue item to queued")
    queue_reset.add_argument("queue_id")
    queue_supersede = queue_sub.add_parser(
        "supersede",
        help="Retire an obsolete quality failure while preserving its audit evidence",
    )
    queue_supersede.add_argument("queue_id")
    queue_supersede.add_argument("--successor-job", required=True)
    queue_supersede.add_argument("--reason", required=True)
    queue_supersession_preview = queue_sub.add_parser(
        "supersession-preview",
        help="Prove whether an exact current-passing retry can retire one failure",
    )
    queue_supersession_preview.add_argument("queue_id")
    queue_sub.add_parser(
        "supersession-list",
        help="Review current proof integrity for every bounded retired failure",
    )
    queue_cancel = queue_sub.add_parser("cancel", help="Cancel an item that has not started")
    queue_cancel.add_argument("queue_id")
    queue_park = queue_sub.add_parser(
        "park", help="Reversibly remove an unstarted item from queue selection"
    )
    queue_park.add_argument("queue_id")
    queue_park.add_argument("--reason", required=True)
    queue_unpark = queue_sub.add_parser(
        "unpark", help="Restore a parked item with its original priority and due time"
    )
    queue_unpark.add_argument("queue_id")
    queue_unpark.add_argument("--reason", required=True)

    schedules = sub.add_parser("schedules", help="Manage manually materialized recurring missions")
    schedule_sub = schedules.add_subparsers(dest="schedule_command", required=True)
    schedule_create = schedule_sub.add_parser("create", help="Create a recurring mission template")
    schedule_create.add_argument("name")
    schedule_create.add_argument("objective")
    schedule_create.add_argument("--every-days", type=int, required=True)
    schedule_create.add_argument("--next-run", required=True, help="ISO-8601 first due time")
    schedule_create.add_argument("--project")
    schedule_create.add_argument("--roles")
    schedule_create.add_argument("--playbook", choices=tuple(PLAYBOOKS))
    schedule_create.add_argument("--priority", type=int, default=50)
    schedule_sub.add_parser("list", help="List recurring mission templates")
    schedule_sub.add_parser("tick", help="Queue one occurrence for each due schedule")
    for action in ("enable", "disable"):
        schedule_action = schedule_sub.add_parser(action, help=f"{action.title()} a schedule")
        schedule_action.add_argument("schedule_id")

    evidence = sub.add_parser(
        "evidence", help="Record and inspect sellability evidence for completed missions"
    )
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_record = evidence_sub.add_parser(
        "record", help="Append one human review bound to a sealed completed job"
    )
    evidence_record.add_argument("job_id")
    evidence_record.add_argument(
        "--category", required=True,
        choices=("coding", "business", "data-research"),
    )
    evidence_record.add_argument(
        "--decision", required=True, choices=("accepted", "rejected"),
    )
    evidence_record.add_argument(
        "--outcome-reason",
        choices=("none", "inaccurate", "incomplete", "not_actionable", "too_slow",
                 "too_resource_heavy", "unsafe", "tool_failure", "other"),
        help="Use none for accepted evidence; classify why rejected evidence failed",
    )
    evidence_record.add_argument("--corrections", required=True, type=int)
    evidence_record.add_argument(
        "--paid-setup", required=True, choices=("yes", "no", "unknown"),
    )
    evidence_record.add_argument(
        "--peak-memory-mb", type=int,
        help="Measured peak memory in MiB; omit only when the measurement is unavailable",
    )
    evidence_experiment = evidence_sub.add_parser(
        "experiment", help="Append a sealed review from another local agent or tool"
    )
    evidence_experiment.add_argument("label")
    evidence_experiment.add_argument("--project", required=True)
    evidence_experiment.add_argument("--experiment-id")
    evidence_experiment.add_argument(
        "--category", required=True,
        choices=("coding", "business", "data-research"),
    )
    evidence_experiment.add_argument(
        "--decision", required=True, choices=("accepted", "rejected"),
    )
    evidence_experiment.add_argument(
        "--outcome-reason",
        choices=("none", "inaccurate", "incomplete", "not_actionable", "too_slow",
                 "too_resource_heavy", "unsafe", "tool_failure", "other"),
        help="Use none for accepted evidence; classify why rejected evidence failed",
    )
    evidence_experiment.add_argument("--corrections", required=True, type=int)
    evidence_experiment.add_argument(
        "--paid-setup", required=True, choices=("yes", "no", "unknown"),
    )
    evidence_experiment.add_argument("--runtime-seconds", required=True, type=float)
    evidence_experiment.add_argument("--peak-memory-mb", required=True, type=int)
    evidence_experiment.add_argument("--exit-code", required=True, type=int)
    checks = evidence_experiment.add_mutually_exclusive_group(required=True)
    checks.add_argument("--checks-passed", dest="checks_passed", action="store_true")
    checks.add_argument("--checks-failed", dest="checks_passed", action="store_false")
    evidence_experiment.add_argument("--runner", required=True)
    evidence_experiment.add_argument("--artifact-sha256", required=True)
    evidence_status = evidence_sub.add_parser(
        "status", help="Show integrity-checked progress toward the ten-mission milestone"
    )
    evidence_status.add_argument("--project")

    sub.add_parser("status", help="List local jobs")
    show = sub.add_parser("show", help="Show a job plan and audit events")
    show.add_argument("job_id")
    retry = sub.add_parser("retry", help="Run a new job from a previous objective")
    retry.add_argument("job_id")
    retry.add_argument(
        "--roles",
        help="Comma-separated bounded retry team; preserves objective and parent lineage",
    )
    add_runtime_args(retry)
    resume = sub.add_parser("resume", help="Continue incomplete assignments in a failed or interrupted job")
    resume.add_argument("job_id")
    add_runtime_args(resume)
    recover = sub.add_parser(
        "recover", help="Recover stale jobs and queue claims without rerunning models"
    )
    recover.add_argument("--stale-minutes", type=int, default=60)

    knowledge = sub.add_parser("knowledge", help="Manage local reference files")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    add = knowledge_sub.add_parser("add", help="Add or refresh one local text file")
    add.add_argument("path", type=Path)
    add.add_argument("--project", help="Attach the source to a project")
    add_dir = knowledge_sub.add_parser("add-dir", help="Read supported files from one explicit directory")
    add_dir.add_argument("path", type=Path)
    add_dir.add_argument("--project", required=True, help="Required project name or ID")
    add_dir.add_argument("--recursive", action="store_true", help="Explicitly allow nested directory reads")
    add_dir.add_argument("--max-files", type=int, default=100)
    list_knowledge = knowledge_sub.add_parser("list", help="List local reference files")
    list_knowledge.add_argument("--project")
    audit_knowledge = knowledge_sub.add_parser(
        "audit", help="Check registered source freshness without exposing paths"
    )
    audit_knowledge.add_argument("--project")
    refresh_knowledge = knowledge_sub.add_parser(
        "refresh", help="Atomically refresh every registered source for one project"
    )
    refresh_knowledge.add_argument("--project", required=True)
    authority_knowledge = knowledge_sub.add_parser(
        "authority", help="Set explicit project-scoped retrieval authority"
    )
    authority_knowledge.add_argument("source_id")
    authority_knowledge.add_argument("--project", required=True)
    authority_knowledge.add_argument("--level", type=int, required=True)
    search = knowledge_sub.add_parser("search", help="Preview local retrieval")
    search.add_argument("query")
    search.add_argument("--project")

    datasets = sub.add_parser(
        "datasets", help="Profile project-scoped CSV, JSON, and XLSX files read-only"
    )
    dataset_sub = datasets.add_subparsers(dest="dataset_command", required=True)
    dataset_add = dataset_sub.add_parser("add", help="Profile one explicit local dataset")
    dataset_add.add_argument("path", type=Path)
    dataset_add.add_argument("--project", required=True)
    dataset_add.add_argument(
        "--allow-root",
        type=Path,
        help="Constrain the source to this local directory; required for XLSX",
    )
    dataset_add.add_argument("--sheet", help="XLSX sheet name; defaults to the first visible sheet")
    dataset_add.add_argument(
        "--key",
        action="append",
        dest="key_columns",
        help="Declare one key column for completeness/uniqueness checks; repeat for a composite key",
    )
    dataset_add.add_argument(
        "--required",
        action="append",
        dest="required_columns",
        help="Require one column to exist with no missing rows; repeat for more columns",
    )
    dataset_add.add_argument(
        "--type",
        action="append",
        nargs=2,
        metavar=("COLUMN", "TYPE"),
        dest="allowed_type_rules",
        help=(
            "Allow one non-missing profile type for a column; repeat to allow more. "
            "Use numeric for integer or number"
        ),
    )
    dataset_add.add_argument(
        "--min",
        action="append",
        nargs=2,
        metavar=("COLUMN", "VALUE"),
        dest="numeric_minimum_rules",
        help="Declare an inclusive finite numeric minimum; repeat by column",
    )
    dataset_add.add_argument(
        "--max",
        action="append",
        nargs=2,
        metavar=("COLUMN", "VALUE"),
        dest="numeric_maximum_rules",
        help="Declare an inclusive finite numeric maximum; repeat by column",
    )
    dataset_list = dataset_sub.add_parser("list", help="List profiled datasets")
    dataset_list.add_argument("--project")
    dataset_show = dataset_sub.add_parser("show", help="Show one statistical profile")
    dataset_show.add_argument("dataset_id")

    approvals = sub.add_parser("approvals", help="Manage the non-executing owner approval inbox")
    approval_sub = approvals.add_subparsers(dest="approval_command", required=True)
    approval_list = approval_sub.add_parser("list", help="List action requests")
    approval_list.add_argument("--status", choices=("pending", "approved", "rejected"))
    request = approval_sub.add_parser("request", help="Record a proposed sensitive action")
    request.add_argument("description")
    request.add_argument("--job")
    for decision in ("approve", "reject"):
        decide = approval_sub.add_parser(decision, help=f"{decision.title()} a request without executing it")
        decide.add_argument("request_id")
        decide.add_argument("--note", default="")

    doctor = sub.add_parser(
        "doctor", help="Check local Python, Ollama service, and configured model dependency"
    )
    doctor.add_argument("--model", default=DEFAULT_MODEL)
    benchmark = sub.add_parser("benchmark", help="Measure one local model generation")
    benchmark.add_argument("--prompt", default="Give three concise rules for reliable local AI work.")
    benchmark.add_argument("--project", help="Required when an execution focus is active")
    add_runtime_args(benchmark)
    dashboard = sub.add_parser("dashboard", help="Serve a read-only operator view on 127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    add_runtime_args(dashboard)
    quality = sub.add_parser(
        "quality", help="Evaluate a report or inspect bounded stored quality failures"
    )
    quality.add_argument("job_id", nargs="?")
    quality.add_argument(
        "--summary", action="store_true",
        help="Show bounded stored failure gates and repair actions without re-evaluating",
    )
    quality.add_argument(
        "--preview", action="store_true",
        help="Preview the current evaluator on an isolated clone without changing local state",
    )
    quality.add_argument(
        "--failed", action="store_true",
        help="Show one pathless recovery overview for all quality-failed queue missions",
    )
    brief = sub.add_parser(
        "brief", help="Show one pathless project operating brief and next action"
    )
    brief.add_argument("--project", required=True)
    capacity = sub.add_parser(
        "capacity", help="Prove the bounded Ally runtime and company admission state"
    )
    capacity.add_argument("--project", required=True)
    health = sub.add_parser("health", help="Show local storage, model, queue, and runtime health")
    add_runtime_args(health)
    export = sub.add_parser("export", help="Write a portable audit JSON and SHA-256 manifest")
    export.add_argument("destination", type=Path)
    service = sub.add_parser("service", help="Manage the detached localhost dashboard service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_start = service_sub.add_parser("start", help="Start the localhost dashboard in the background")
    service_start.add_argument("--port", type=int, default=8765)
    add_runtime_args(service_start)
    service_start.set_defaults(num_predict=int(os.getenv("LOCAL_COMPANY_SERVICE_NUM_PREDICT", "768")))
    service_sub.add_parser("status", help="Verify the recorded PID against localhost health")
    service_sub.add_parser("stop", help="Stop the recorded service using its local secret")
    return p


def add_runtime_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--provider", choices=("mock", "ollama"), default=DEFAULT_PROVIDER)
    command.add_argument("--model", default=DEFAULT_MODEL)
    command.add_argument("--num-ctx", type=int, default=int(os.getenv("LOCAL_COMPANY_NUM_CTX", "4096")))
    command.add_argument("--num-predict", type=int, default=int(os.getenv("LOCAL_COMPANY_NUM_PREDICT", "512")))
    command.add_argument("--keep-alive", default=os.getenv("LOCAL_COMPANY_KEEP_ALIVE", "0s"))


def selected_model(args: argparse.Namespace):
    return OllamaModel(
        args.model, num_ctx=args.num_ctx, num_predict=args.num_predict, keep_alive=args.keep_alive
    ) if getattr(args, "provider", "mock") == "ollama" else MockModel()


def _project_identity(company: Company, project: str | None) -> tuple[str | None, str | None]:
    if project is None:
        return None, None
    detail = company.project_detail(project)["project"]
    return str(detail[0]), str(detail[1])


def _enforce_cli_execution_focus(company: Company, args: argparse.Namespace) -> None:
    model_backed_commands = {"run", "retry", "resume", "benchmark"}
    service_start = args.command == "service" and args.service_command == "start"
    if args.command == "queue":
        if args.queue_command != "run-next":
            return
    elif args.command not in model_backed_commands and not service_start:
        return
    focus = read_execution_focus(company.home)
    enforce_execution_resource_envelope(
        focus,
        getattr(args, "provider", "mock"),
        getattr(args, "num_ctx", 4096),
        getattr(args, "num_predict", 512),
        getattr(args, "keep_alive", "0s"),
        "service start" if service_start else (
            "queue run-next" if args.command == "queue" else args.command
        ),
    )
    if service_start or not focus["enabled"]:
        return
    project_id: str | None = None
    roles: list[str] = []
    action = args.command
    if args.command == "run":
        project_id, _ = _project_identity(company, args.project)
        roles = (
            [role.strip() for role in args.roles.split(",") if role.strip()]
            if args.roles else list(Company.routing_preview(args.objective)["roles"])
        )
    elif args.command == "queue" and args.queue_command == "run-next":
        preview = company.queue_preflight(args.queue_id)
        if preview["queue_id"] is None or preview["reviewed_queue_matches"] is False:
            return
        project_id = preview["project_id"]
        roles = list(preview["team"]["roles"])
        action = "queue run-next"
    elif args.command in {"retry", "resume"}:
        detail = company.job_detail(args.job_id)
        project_name = detail["job"][6]
        project_id, _ = _project_identity(company, project_name)
        roles = (
            [role.strip() for role in args.roles.split(",") if role.strip()]
            if args.command == "retry" and args.roles
            else [str(assignment[1]) for assignment in detail["assignments"]]
        )
    elif args.command == "benchmark":
        project_id, _ = _project_identity(company, args.project)
        roles = ["benchmark"]
    else:
        return
    enforce_execution_focus(focus, project_id, roles, action)


def _assert_focus_mutation_idle(company: Company) -> None:
    health = company.health_snapshot()
    if (
        health.get("active_jobs") != 0
        or health.get("running_missions") != 0
        or health.get("pending_report_finalizations") != 0
        or health.get("pending_evaluations") != 0
        or health.get("pending_completion") != []
    ):
        raise RuntimeError(
            "Execution focus cannot change while local work is active or completing"
        )


def _focus_output(focus: dict[str, object]) -> dict[str, object]:
    return {
        "contract": "local-company.execution-focus-observation.v1",
        "focus": focus,
        "focusDigest": execution_focus_digest(focus),
    }


def _interactive_vision_sales_intake(sales_root: Path | None, *, input_fn=None) -> dict:
    reader = input_fn or input

    def ask(prompt: str) -> str:
        try:
            return reader(prompt).strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise ValueError("vision_sales_intake_cancelled") from error

    def whole_number(prompt: str, field: str) -> int:
        try:
            return int(ask(prompt))
        except ValueError as error:
            if str(error) == "vision_sales_intake_cancelled":
                raise
            raise ValueError(f"{field}_invalid") from error

    def decimal_number(prompt: str, field: str, *, default: float | None = None) -> float:
        value = ask(prompt)
        if not value and default is not None:
            return default
        try:
            return float(value)
        except ValueError as error:
            raise ValueError(f"{field}_invalid") from error

    def yes_no(prompt: str, field: str) -> bool:
        value = ask(prompt).lower()
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        raise ValueError(f"{field}_must_be_yes_or_no")

    fields = {
        "name": ask("Prospect name: "),
        "email": ask("Prospect work email: "),
        "company": ask("Company: "),
        "goal": ask("Visual workflow that should run better: "),
        "platform": ask("Target device (windows/android/both): "),
        "state_count": whole_number("Visual states to recognize (1-12): ", "state_count"),
        "weekly_runs": whole_number("Runs per week: ", "weekly_runs"),
        "minutes_per_run": decimal_number("Minutes per run: ", "minutes_per_run"),
        "labor_hourly_usd": decimal_number("Estimated labor cost per hour in USD (blank for unknown): ", "labor_hourly_usd", default=0),
        "screenshot_rights": yes_no("Written rights to use the screenshots? (yes/no): ", "screenshot_rights"),
        "human_fallback": yes_no("Can a person review uncertain results? (yes/no): ", "human_fallback"),
        "observation_only": yes_no("May the first pilot be observation-only? (yes/no): ", "observation_only"),
    }
    return create_vision_sales_intake_fields(fields, sales_root)


def main() -> int:
    try:
        args = parser().parse_args()
        company_home = args.home if args.home is not None else default_company_home()
        company = Company(company_home.resolve(), selected_model(args))
        _enforce_cli_execution_focus(company, args)
        if args.command == "init":
            company.initialize()
            print(f"Initialized local company at {company.home}")
        elif args.command == "roles":
            for name, purpose in ROLES.items():
                print(f"{name:16} {purpose}")
        elif args.command == "computer":
            desktop = WindowsDesktopAdapter()
            if args.computer_command == "doctor":
                result = desktop.doctor()
            elif args.computer_command == "lab":
                result = launch_workflow_lab()
            elif args.computer_command == "prove":
                result = prove_computer_use(
                    company.home, args.confirm, adapter=desktop,
                )
            elif args.computer_command == "windows":
                result = windows_observation(desktop, args.limit)
            elif args.computer_command == "inspect":
                result = inspect_window(desktop, args.title, args.limit)
            elif args.computer_command == "capture":
                result = capture_screen(desktop, args.output)
            elif args.computer_command == "learn":
                print(
                    "Recording starts now. Demonstrate in the target app; press F8 to stop. "
                    "Typed characters are not stored in the event stream; private TEXT placeholders "
                    "are created. Screenshots are off by default; --screenshots captures bounded "
                    "post-demonstration app-window references that may contain visible private data.",
                    file=sys.stderr,
                )
                result = learn_workflow(
                    company.home, args.name, args.seconds,
                    screenshots=args.screenshots,
                    expected_final_title=args.expect_window_title,
                    expected_final_control_text=args.expect_control_text,
                    adapter=desktop,
                )
            elif args.computer_command == "list":
                result = list_workflows(company.home)
            elif args.computer_command == "show":
                _path, result = load_workflow(company.home, args.name)
            elif args.computer_command == "preview":
                result = preview_workflow(
                    company.home, args.name,
                    allow_network_apps=args.allow_network_apps,
                    allow_shells=args.allow_shells, adapter=desktop,
                )
            else:
                result = run_workflow(
                    company.home, args.name, args.confirm,
                    allow_network_apps=args.allow_network_apps,
                    allow_shells=args.allow_shells,
                    capture_evidence=not args.no_evidence,
                    adapter=desktop,
                )
            print(json.dumps(result, indent=2))
            if (
                args.computer_command == "run" and result["status"] != "completed"
            ) or (
                args.computer_command == "prove" and result["status"] != "passed"
            ):
                return 1
        elif args.command == "browser":
            if args.browser_command == "doctor":
                result = browser_doctor()
            elif args.browser_command == "supermega-release":
                result = run_supermega_release_suite(
                    company.home, runs=args.runs, timeout_seconds=args.timeout_seconds,
                )
            else:
                result = run_browser_check(
                    company.home,
                    args.url,
                    expected_title=args.expect_title,
                    expected_text=args.expect_text,
                    fail_on_console_errors=args.fail_on_console_errors,
                    max_a11y_violations=args.max_a11y_violations,
                    timeout_seconds=args.timeout_seconds,
                )
            print(json.dumps(result, indent=2))
            if result["status"] not in {"ready", "passed"}:
                return 1
        elif args.command == "supermega":
            if args.supermega_command == "vision-sales":
                print(json.dumps(run_vision_sales(args.platform_root, args.sales_root), indent=2))
            elif args.supermega_command == "vision-sales-status":
                print(json.dumps(vision_sales_status(args.sales_root), indent=2))
            elif args.supermega_command == "vision-sales-intake":
                result = (
                    _interactive_vision_sales_intake(args.sales_root)
                    if args.interactive
                    else create_vision_sales_intake(args.input, args.sales_root)
                )
                print(json.dumps(result, indent=2))
            elif args.supermega_command == "vision-prospect-import":
                print(json.dumps(import_vision_prospects(args.input, args.sales_root), indent=2))
            elif args.supermega_command == "vision-prospect-drafts":
                print(json.dumps(create_vision_prospect_drafts(args.sales_root), indent=2))
            elif args.supermega_command == "vision-founding-pilot-packages":
                print(json.dumps(create_vision_founding_pilot_packages(args.sales_root), indent=2))
            elif args.supermega_command == "vision-product-status":
                print(json.dumps(vision_product_status(args.product_root), indent=2))
            elif args.supermega_command == "vision-commercial-status":
                print(json.dumps(vision_commercial_status(
                    args.product_root, args.sales_root,
                ), indent=2))
        elif args.command == "focus":
            if args.focus_command == "set":
                project_id, project_name = _project_identity(company, args.project)
                print(json.dumps(_focus_output(set_execution_focus(
                    company.home, str(project_id), str(project_name), args.max_roles,
                )), indent=2))
            elif args.focus_command == "handoff":
                _assert_focus_mutation_idle(company)
                from_project_id, _ = _project_identity(company, args.from_project)
                to_project_id, to_project_name = _project_identity(company, args.project)
                print(json.dumps(_focus_output(handoff_execution_focus(
                    company.home,
                    str(from_project_id),
                    str(to_project_id),
                    str(to_project_name),
                    args.max_roles,
                    args.expected_focus_digest,
                    args.reason,
                )), indent=2))
            elif args.focus_command == "show":
                print(json.dumps(_focus_output(read_execution_focus(company.home)), indent=2))
            elif args.focus_command == "clear":
                _assert_focus_mutation_idle(company)
                print(json.dumps(_focus_output(clear_execution_focus(
                    company.home, args.expected_focus_digest, args.reason,
                )), indent=2))
        elif args.command == "route":
            print(json.dumps(Company.routing_preview(args.objective, args.playbook), indent=2))
        elif args.command == "preflight":
            print(json.dumps(company.mission_preflight(
                args.objective, args.project, args.playbook,
            ), indent=2))
        elif args.command == "run":
            roles = [role.strip() for role in args.roles.split(",") if role.strip()] if args.roles else None
            job_id, output = company.run(args.objective, roles, project=args.project)
            print(f"Completed job {job_id}\nReport: {output}")
        elif args.command == "projects":
            if args.project_command == "create":
                project_id = company.create_project(args.name, args.description)
                print(f"Created project {project_id}: {args.name}")
            elif args.project_command == "list":
                rows = company.projects()
                if not rows:
                    print("No projects yet.")
                for project_id, name, created, job_count in rows:
                    print(f"{project_id}  missions={job_count:<3}  {created}  {name}")
            elif args.project_command == "show":
                print(json.dumps(company.project_detail(args.project), indent=2))
        elif args.command == "playbooks":
            if args.playbook_command == "list":
                for name, item in PLAYBOOKS.items():
                    print(f"{name:24} {item['description']}")
            elif args.playbook_command == "show":
                print(json.dumps({"name": args.name, **PLAYBOOKS[args.name]}, indent=2))
        elif args.command == "queue":
            if args.queue_command == "add":
                roles = [role.strip() for role in args.roles.split(",") if role.strip()] if args.roles else None
                queue_id = company.enqueue(
                    args.objective, args.project, roles, args.playbook, args.priority, args.scheduled_at
                )
                print(f"Queued mission {queue_id}; nothing was executed.")
            elif args.queue_command == "list":
                rows = company.queue_items(args.status)
                if not rows:
                    print("No queue items found.")
                for item_id, status, priority, scheduled, project, playbook, objective, job_id, error in rows:
                    error_text = f"  error={error}" if error else ""
                    print(
                        f"{item_id}  {status:14}  p={priority:<3}  {scheduled}  "
                        f"project={project or '-'}  playbook={playbook or '-'}  job={job_id or '-'}  "
                        f"{objective}{error_text}"
                    )
            elif args.queue_command == "preflight":
                print(json.dumps(company.queue_preflight(args.queue_id), indent=2))
            elif args.queue_command == "retry-preflight":
                print(json.dumps(
                    company.queue_retry_preflight(args.queue_id), indent=2,
                ))
            elif args.queue_command == "run-next":
                queue_id, job_id, output, passed = company.run_next_queue_item(args.queue_id)
                print(
                    f"Queue item {queue_id} completed as job {job_id}; "
                    f"quality={'passed' if passed else 'failed'}\nReport: {output}"
                )
            elif args.queue_command == "reset":
                company.reset_queue_item(args.queue_id)
                print(f"Queue item {args.queue_id} reset; nothing was executed.")
            elif args.queue_command == "supersede":
                print(json.dumps(
                    company.supersede_quality_failure(
                        args.queue_id, args.reason, args.successor_job,
                    ),
                    indent=2,
                ))
            elif args.queue_command == "supersession-preview":
                print(json.dumps(
                    company.quality_supersession_preview(args.queue_id), indent=2,
                ))
            elif args.queue_command == "supersession-list":
                print(json.dumps(
                    company.quality_supersession_summaries(), indent=2,
                ))
            elif args.queue_command == "cancel":
                company.cancel_queue_item(args.queue_id)
                print(f"Queue item {args.queue_id} cancelled.")
            elif args.queue_command == "park":
                print(json.dumps(
                    company.park_queue_item(args.queue_id, args.reason), indent=2,
                ))
            elif args.queue_command == "unpark":
                print(json.dumps(
                    company.unpark_queue_item(args.queue_id, args.reason), indent=2,
                ))
        elif args.command == "schedules":
            if args.schedule_command == "create":
                roles = [role.strip() for role in args.roles.split(",") if role.strip()] if args.roles else None
                schedule_id = company.create_schedule(
                    args.name, args.objective, args.every_days, args.next_run,
                    args.project, roles, args.playbook, args.priority,
                )
                print(f"Created schedule {schedule_id}; no mission was queued or executed.")
            elif args.schedule_command == "list":
                rows = company.schedules()
                if not rows:
                    print("No schedules found.")
                for schedule_id, name, enabled, cadence, next_run, project, playbook, priority, objective in rows:
                    print(
                        f"{schedule_id}  {'enabled' if enabled else 'disabled':8}  every={cadence}d  "
                        f"next={next_run}  p={priority}  project={project or '-'}  "
                        f"playbook={playbook or '-'}  {name}: {objective}"
                    )
            elif args.schedule_command == "tick":
                created = company.materialize_due_schedules()
                print(f"Materialized {len(created)} due schedule(s).")
                for schedule_id, queue_id in created:
                    print(f"{schedule_id} -> queue {queue_id}")
            elif args.schedule_command in {"enable", "disable"}:
                enabled = args.schedule_command == "enable"
                company.set_schedule_enabled(args.schedule_id, enabled)
                print(f"Schedule {args.schedule_id} {'enabled' if enabled else 'disabled'}.")
        elif args.command == "evidence":
            if args.evidence_command == "record":
                result = company.record_product_evidence_review(
                    args.job_id, args.category, args.decision, args.corrections,
                    args.paid_setup, args.peak_memory_mb, args.outcome_reason,
                )
            elif args.evidence_command == "experiment":
                result = company.record_product_experiment_review(
                    args.project, args.label, args.category, args.decision,
                    args.corrections, args.paid_setup, args.runtime_seconds,
                    args.peak_memory_mb, args.exit_code, args.checks_passed,
                    args.runner, args.artifact_sha256, args.experiment_id,
                    args.outcome_reason,
                )
            else:
                result = company.product_evidence_status(args.project)
            print(json.dumps(result, indent=2))
        elif args.command == "status":
            rows = company.jobs()
            if not rows:
                print("No jobs yet.")
            for job_id, status, created, objective in rows:
                print(f"{job_id}  {status:11}  {created}  {objective}")
        elif args.command == "show":
            print(json.dumps(company.job_detail(args.job_id), indent=2))
        elif args.command == "retry":
            roles = (
                [role.strip() for role in args.roles.split(",") if role.strip()]
                if args.roles else None
            )
            job_id, output = company.retry(args.job_id, roles=roles)
            print(f"Completed retry job {job_id}\nReport: {output}")
        elif args.command == "resume":
            job_id, output = company.resume(args.job_id)
            print(f"Resumed and completed job {job_id}\nReport: {output}")
        elif args.command == "recover":
            recovered = company.recover_stale_jobs(args.stale_minutes * 60)
            print(
                f"Recovered {len(recovered)} stale job(s): "
                f"{', '.join(recovered) if recovered else 'none'}. "
                "Stale queue claims were reconciled without model reruns."
            )
        elif args.command == "knowledge":
            if args.knowledge_command == "add":
                item_id, changed = company.add_knowledge(args.path, args.project)
                print(f"Knowledge {item_id}: {'added or refreshed' if changed else 'already current'}")
            elif args.knowledge_command == "add-dir":
                changed, unchanged, skipped = company.add_knowledge_dir(
                    args.path, args.project, args.recursive, args.max_files
                )
                print(f"Directory read complete: changed={changed}, unchanged={unchanged}, skipped={skipped}")
            elif args.knowledge_command == "list":
                rows = company.knowledge_items(args.project)
                if not rows:
                    print("No knowledge sources yet.")
                for item_id, path, added in rows:
                    print(f"{item_id}  {added}  {path}")
            elif args.knowledge_command == "audit":
                print(json.dumps(company.knowledge_freshness(args.project), indent=2))
            elif args.knowledge_command == "refresh":
                print(json.dumps(
                    company.refresh_project_knowledge(args.project), indent=2,
                ))
            elif args.knowledge_command == "authority":
                print(json.dumps(company.set_knowledge_authority(
                    args.source_id, args.project, args.level,
                ), indent=2))
            elif args.knowledge_command == "search":
                hits = company.search_knowledge(args.query, project=args.project)
                if not hits:
                    print("No relevant local sources found.")
                for hit in hits:
                    print(
                        f"[score={hit.score} authority={hit.authority} "
                        f"rank={hit.score + hit.authority}] "
                        f"{hit.path}\n{hit.excerpt}\n"
                    )
        elif args.command == "datasets":
            if args.dataset_command == "add":
                dataset_id, brief_path, profile = company.profile_dataset(
                    args.path,
                    args.project,
                    allowed_root=args.allow_root,
                    sheet=args.sheet,
                    key_columns=args.key_columns,
                    required_columns=args.required_columns,
                    allowed_type_rules=args.allowed_type_rules,
                    numeric_minimum_rules=args.numeric_minimum_rules,
                    numeric_maximum_rules=args.numeric_maximum_rules,
                )
                print(
                    f"Dataset {dataset_id}: rows={profile['profiled_rows']}, columns={profile['column_count']}\n"
                    f"Contract: {profile['contract_check']['status']}, "
                    f"failed_rules={profile['contract_check']['failed_rules']}\n"
                    f"Brief: {brief_path}\nSource was read-only."
                )
            elif args.dataset_command == "list":
                rows = company.dataset_items(args.project)
                if not rows:
                    print("No datasets found.")
                for dataset_id, project, file_format, rows_count, columns, path, added in rows:
                    print(
                        f"{dataset_id}  {file_format:4}  rows={rows_count:<6}  columns={columns:<4}  "
                        f"project={project}  {added}  {path}"
                    )
            elif args.dataset_command == "show":
                print(json.dumps(company.dataset_detail(args.dataset_id), indent=2))
        elif args.command == "approvals":
            if args.approval_command == "request":
                request_id = company.request_action(args.description, args.job)
                print(f"Recorded pending request {request_id}; no action was executed.")
            elif args.approval_command in {"approve", "reject"}:
                decision = "approved" if args.approval_command == "approve" else "rejected"
                company.decide_action(args.request_id, decision, args.note)
                print(f"Request {args.request_id} marked {decision}; no action was executed.")
            elif args.approval_command == "list":
                rows = company.action_requests(args.status)
                if not rows:
                    print("No action requests found.")
                for request_id, status, category, created, description in rows:
                    print(f"{request_id}  {status:8}  {category:24}  {created}  {description}")
        elif args.command == "doctor":
            ollama = OllamaModel(args.model)
            try:
                models = ollama.models()
            except (AttributeError, TypeError, json.JSONDecodeError, RecursionError, UnicodeError):
                print("ERROR: Ollama model inventory is malformed", file=sys.stderr)
                return 2
            if models is not None and (
                type(models) is not list
                or any(type(name) is not str or not name for name in models)
            ):
                print("ERROR: Ollama model inventory is malformed", file=sys.stderr)
                return 2
            service_ready = models is not None
            model_ready = service_ready and args.model in models
            print(f"Python: {sys.version.split()[0]} (ready)")
            print(f"Database home: {company.home}")
            print(f"Ollama executable: {find_ollama_executable() or 'not detected'}")
            print(f"Ollama service: {'ready' if service_ready else 'not detected'}")
            print(
                "Installed models: " + (
                    ", ".join(models)
                    if models else
                    "none detected" if service_ready else "unknown (service unavailable)"
                )
            )
            print(f"Configured model: {args.model}")
            print(
                "Configured model status: " + (
                    "installed" if model_ready else
                    "not installed" if service_ready else
                    "unknown (service unavailable)"
                )
            )
            print(f"Doctor result: {'ready' if model_ready else 'action required'}")
            print(
                "Doctor action: " + (
                    "none" if model_ready else
                    "install_configured_model" if service_ready else
                    "start_ollama_locally"
                )
            )
            return 0 if model_ready else 1
        elif args.command == "benchmark":
            started = time.perf_counter()
            output = company.model.complete(
                "You are a concise local AI benchmark. Do not use tools or claim external actions.", args.prompt
            )
            elapsed = time.perf_counter() - started
            print(f"Elapsed wall seconds: {elapsed:.2f}")
            metrics = getattr(company.model, "last_metrics", {})
            if metrics:
                print("Model metrics: " + json.dumps(metrics, sort_keys=True))
            print("Output:\n" + output)
        elif args.command == "dashboard":
            from .dashboard import serve_dashboard
            serve_dashboard(company, args.port)
        elif args.command == "quality":
            if args.failed:
                if args.job_id is not None or args.summary or args.preview:
                    raise ValueError(
                        "--failed cannot be combined with JOB_ID, --summary, or --preview"
                    )
                result = company.quality_failure_summaries()
            else:
                if args.job_id is None:
                    raise ValueError("Provide JOB_ID or use --failed")
                if args.summary and args.preview:
                    raise ValueError("--summary cannot be combined with --preview")
                if args.preview:
                    result = company.quality_recheck_preview(args.job_id)
                elif args.summary:
                    result = company.quality_recovery_summary(args.job_id)
                else:
                    result = company.evaluate_job(args.job_id)
            print(json.dumps(result, indent=2))
        elif args.command == "brief":
            print(json.dumps(company.operator_brief(args.project), indent=2))
        elif args.command == "capacity":
            print(json.dumps(machine_capacity_snapshot(company, args.project), indent=2))
        elif args.command == "health":
            print(json.dumps(company.health_snapshot(), indent=2))
        elif args.command == "export":
            audit_path, hash_path, digest = company.export_audit(args.destination)
            print(f"Audit: {audit_path}\nSHA-256: {digest}\nManifest: {hash_path}")
        elif args.command == "service":
            from .service import service_status, start_service, stop_service
            if args.service_command == "start":
                print(json.dumps(start_service(
                    company.home, args.port, args.provider, args.model,
                    args.num_ctx, args.num_predict, args.keep_alive,
                ), indent=2))
            elif args.service_command == "status":
                print(json.dumps(service_status(company.home), indent=2))
            elif args.service_command == "stop":
                print(json.dumps(stop_service(company.home), indent=2))
    except (PermissionError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
