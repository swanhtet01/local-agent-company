from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .config import default_company_home
from .core import Company, MockModel, OllamaModel, PLAYBOOKS, ROLES


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

    sub.add_parser("status", help="List local jobs")
    show = sub.add_parser("show", help="Show a job plan and audit events")
    show.add_argument("job_id")
    retry = sub.add_parser("retry", help="Run a new job from a previous objective")
    retry.add_argument("job_id")
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
    health = sub.add_parser("health", help="Show local storage, model, queue, and runtime health")
    add_runtime_args(health)
    export = sub.add_parser("export", help="Write a portable audit JSON and SHA-256 manifest")
    export.add_argument("destination", type=Path)
    service = sub.add_parser("service", help="Manage the detached localhost dashboard service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_start = service_sub.add_parser("start", help="Start the localhost dashboard in the background")
    service_start.add_argument("--port", type=int, default=8765)
    add_runtime_args(service_start)
    service_start.set_defaults(num_predict=int(os.getenv("LOCAL_COMPANY_SERVICE_NUM_PREDICT", "2048")))
    service_sub.add_parser("status", help="Verify the recorded PID against localhost health")
    service_sub.add_parser("stop", help="Stop the recorded service using its local secret")
    return p


def add_runtime_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--provider", choices=("mock", "ollama"), default=DEFAULT_PROVIDER)
    command.add_argument("--model", default=DEFAULT_MODEL)
    command.add_argument("--num-ctx", type=int, default=int(os.getenv("LOCAL_COMPANY_NUM_CTX", "4096")))
    command.add_argument("--num-predict", type=int, default=int(os.getenv("LOCAL_COMPANY_NUM_PREDICT", "512")))
    command.add_argument("--keep-alive", default=os.getenv("LOCAL_COMPANY_KEEP_ALIVE", "30s"))


def selected_model(args: argparse.Namespace):
    return OllamaModel(
        args.model, num_ctx=args.num_ctx, num_predict=args.num_predict, keep_alive=args.keep_alive
    ) if getattr(args, "provider", "mock") == "ollama" else MockModel()


def main() -> int:
    try:
        args = parser().parse_args()
        company_home = args.home if args.home is not None else default_company_home()
        company = Company(company_home.resolve(), selected_model(args))
        if args.command == "init":
            company.initialize()
            print(f"Initialized local company at {company.home}")
        elif args.command == "roles":
            for name, purpose in ROLES.items():
                print(f"{name:16} {purpose}")
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
        elif args.command == "status":
            rows = company.jobs()
            if not rows:
                print("No jobs yet.")
            for job_id, status, created, objective in rows:
                print(f"{job_id}  {status:11}  {created}  {objective}")
        elif args.command == "show":
            print(json.dumps(company.job_detail(args.job_id), indent=2))
        elif args.command == "retry":
            job_id, output = company.retry(args.job_id)
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
                        f"[score={hit.score} authority={hit.authority}] "
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
