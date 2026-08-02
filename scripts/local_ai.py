from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from .select_local_code_model import GIB, available_memory_bytes
    from .run_scheduled_cycle import _recover_memory
except ImportError:
    from select_local_code_model import GIB, available_memory_bytes
    from run_scheduled_cycle import _recover_memory


SCHEMA = "local-ai.launchpad.v1"
WORK_RESULT_SCHEMA = "local-ai.work-result.v1"
CYCLE_RESULT_SCHEMA = "local-ai.cycle-result.v1"
EXPERIMENT_RUN_SCHEMA = "local-ai.experiment-run.v1"
COMPANY_BRIEF_SCHEMA = "local-ai.company-brief.v1"
SUPERMEGA_WORKBENCH_SCHEMA = "local-ai.supermega-workbench.v1"
SUPERMEGA_QUEUE_PARK_SCHEMA = "local-ai.supermega-queue-park.v1"
OFFER_PACK_SCHEMA = "local-ai.offer-pack.v1"
VALIDATION_PACK_SCHEMA = "local-ai.validation-pack.v2"
MISSION_REVIEW_NEXT_SCHEMA = "local-ai.product-mission-review-next.v1"
MISSION_HUMAN_REVIEW_SCHEMA = "local-ai.product-mission-human-review.v1"
PENDING_EXPERIMENT_SCHEMA = "local-ai.pending-product-experiment.v1"
PENDING_EXPERIMENT_ID = re.compile(r"^[0-9a-f]{12}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_PATTERN = re.compile(r"^Completed job ([0-9a-f]{12})$", re.MULTILINE)
QUEUE_COMPLETION_PATTERN = re.compile(
    r"^Queue item ([0-9a-f]{12}) completed as job ([0-9a-f]{12}); quality=(passed|failed)$",
    re.MULTILINE,
)
SCHEDULE_TICK_PATTERN = re.compile(r"^Materialized (\d+) due schedule\(s\)\.$", re.MULTILINE)
CYCLE_MINIMUM_AVAILABLE_BYTES = 2 * GIB
SUPERMEGA_PROJECT_NAME = "SuperMega"
SUPERMEGA_REPOSITORY_DIRECTORY = "supermega-platform"
MAX_GIT_STATUS_BYTES = 64 * 1024
PRODUCT_OUTCOME_REASONS = (
    "none", "inaccurate", "incomplete", "not_actionable", "too_slow",
    "too_resource_heavy", "unsafe", "tool_failure", "other",
)


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
  local-ai.cmd supermega [ACTION] ...         Operate SuperMega without retyping paths
  local-ai.cmd ask "QUESTION"                 Ask the grounded read-only local assistant
  local-ai.cmd code [PROJECT_PATH]            Open a local coding agent in a project
  local-ai.cmd vision [--check] [PROJECT]     Use the full local Vision product agent
  local-ai.cmd vision-lite [--check] [PROJECT] Use the tiny Vision campaign agent
  local-ai.cmd plan "OBJECTIVE" [options]     Preview the team and gates; no model
  local-ai.cmd experiment [PROJECT]            Show the next measured product test; no model
  local-ai.cmd experiment-run [PROJECT] [--recover-memory]
                                              Run that test locally and return its receipt
  local-ai.cmd offer [PROJECT]                 Check whether evidence supports a sellable kit
  local-ai.cmd offer-pack [PROJECT]            Write an owner-review offer pack when proven
  local-ai.cmd validation-pack [PROJECT]       Write the current private validation dossier
  local-ai.cmd mission-candidate [PROJECT]     Show one sealed mission ready for human review
  local-ai.cmd mission-review [PROJECT]        Inspect and review that exact sealed mission
  local-ai.cmd experiment-pending              Inspect locally saved experiment receipts
  local-ai.cmd experiment-review-interactive   Review one pending receipt with local prompts
  local-ai.cmd experiment-review ID --decision accepted|rejected --corrections N
      --outcome-reason REASON --paid-setup yes|no|unknown --confirm-human-review
                                              Record one actual human review
  local-ai.cmd work "OBJECTIVE" [options]     Run one bounded local AI team
  local-ai.cmd later "OBJECTIVE" [options]    Add work to the durable local queue
  local-ai.cmd next [--queue-id ID]           Preview the exact next queued mission
  local-ai.cmd run-next [options]              Run one due queued mission
  local-ai.cmd cycle [--recover-memory] [model options]
                                              Materialize and run at most one mission
  local-ai.cmd autopilot install|status|repair|remove
                                              Manage the six-hour local cycle task
  local-ai.cmd dashboard [options]             Start the local dashboard on 127.0.0.1
  local-ai.cmd dashboard-status                Check the dashboard service
  local-ai.cmd stop                            Stop the verified local dashboard
  local-ai.cmd status                          Show machine, model, and queue health
  local-ai.cmd brief                           Show one code-owned company next action
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
  local-ai.cmd supermega
  local-ai.cmd supermega ask "What should I do next?"
  local-ai.cmd ask "What can this local company do?"
  local-ai.cmd supermega refresh
  local-ai.cmd supermega next
  local-ai.cmd supermega park-next
  local-ai.cmd supermega mission-candidate
  local-ai.cmd supermega mission-review
  local-ai.cmd supermega plan "Choose one verified internal next action"
  local-ai.cmd supermega later "Draft one evidence-grounded release gap brief"
  local-ai.cmd supermega code --check
  local-ai.cmd code C:\path\to\a-project
  local-ai.cmd vision-lite --check
  local-ai.cmd vision-lite
  local-ai.cmd plan "Design a product customers can buy" --project "New Product"
  local-ai.cmd experiment "New Product"
  local-ai.cmd experiment-run "New Product" --recover-memory
  local-ai.cmd offer "New Product"
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


def _ask_command(name: str, scope: str, values: list[str]) -> tuple[str, ...]:
    machine_readable = bool(values and values[0] == "--json")
    question_values = values[1:] if machine_readable else values
    question = " ".join(_require_tail(name, question_values))
    return (
        "--scope", scope, "--question", question,
        *(("--plain",) if not machine_readable else ()),
    )


def _supermega_objective(name: str, values: list[str]) -> list[str]:
    objective = _require_tail(name, values)
    if any(item == "--project" or item.startswith("--project=") for item in objective):
        raise ValueError("supermega_project_override_forbidden")
    return objective


def translate(argv: list[str]) -> LaunchAction | None:
    if not argv or argv[0].lower() in {"help", "-h", "--help"}:
        return None
    name = argv[0].lower()
    tail = argv[1:]
    if name == "check":
        return LaunchAction(("doctor", *tail), "check", "Check the local model dependency without generating text.", False, False)
    if name == "supermega":
        operation = tail[0].lower() if tail else "status"
        values = tail[1:]
        if operation == "status":
            if values:
                raise ValueError("supermega_status_accepts_no_arguments")
            return LaunchAction((), "supermega-status", "Show one pathless SuperMega operating status and next action.", False, False)
        if operation == "refresh":
            if values:
                raise ValueError("supermega_refresh_accepts_no_arguments")
            return LaunchAction(
                ("knowledge", "refresh", "--project", SUPERMEGA_PROJECT_NAME),
                "supermega-refresh", "Refresh registered SuperMega evidence from its exact local sources.",
                False, True,
            )
        if operation == "use":
            if values:
                raise ValueError("supermega_use_accepts_no_arguments")
            return LaunchAction((SUPERMEGA_PROJECT_NAME,), "use", "Select SuperMega for bounded model-backed work.", False, True)
        if operation == "next":
            if values:
                raise ValueError("supermega_next_accepts_no_arguments")
            return LaunchAction(
                ("queue", "preflight"), "supermega-next",
                "Inspect the exact next queued mission and its blockers without running it.",
                False, False,
            )
        if operation == "park-next":
            if values:
                raise ValueError("supermega_park_next_accepts_no_arguments")
            return LaunchAction(
                (), "supermega-park-next",
                "Reversibly park only an incompatible exact queue head so SuperMega can proceed.",
                False, True,
            )
        if operation == "proof":
            if values:
                raise ValueError("supermega_proof_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "experiment",
                "Preview the next category-balanced SuperMega product proof without loading a model.",
                False, False,
            )
        if operation == "prove":
            if values:
                raise ValueError("supermega_prove_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME, "--recover-memory"), "experiment-run",
                "Run one measured SuperMega product proof and preserve it for human review.",
                True, True,
            )
        if operation == "pending":
            if values:
                raise ValueError("supermega_pending_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "experiment-pending",
                "Inspect pending SuperMega product-proof receipts without changing state.",
                False, False,
            )
        if operation == "review":
            if values:
                raise ValueError("supermega_review_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "experiment-review-interactive",
                "Review one measured SuperMega proof through explicit local human prompts.",
                False, True,
            )
        if operation == "dossier":
            if values:
                raise ValueError("supermega_dossier_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "validation-pack",
                "Write the current private SuperMega product-proof dossier.",
                False, True,
            )
        if operation == "mission-candidate":
            if values:
                raise ValueError("supermega_mission_candidate_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "mission-candidate",
                "Inspect one sealed unreviewed SuperMega mission without changing state.",
                False, False,
            )
        if operation == "mission-review":
            if values:
                raise ValueError("supermega_mission_review_accepts_no_arguments")
            return LaunchAction(
                (SUPERMEGA_PROJECT_NAME,), "mission-review",
                "Inspect and explicitly record one actual human review of a sealed SuperMega mission.",
                False, True,
            )
        if operation == "ask":
            return LaunchAction(
                _ask_command("supermega_ask", "supermega", values), "ask",
                "Ask a read-only local model using verified SuperMega context.",
                True, False,
            )
        if operation in {"plan", "work", "later"}:
            objective = _supermega_objective(f"supermega_{operation}", values)
            prefix = {
                "plan": ("preflight",), "work": ("run",),
                "later": ("queue", "add"),
            }[operation]
            return LaunchAction(
                (*prefix, *objective, "--project", SUPERMEGA_PROJECT_NAME), operation,
                {
                    "plan": "Preview a SuperMega team, evidence, and owner gates without starting work.",
                    "work": "Run one bounded SuperMega team and write its auditable report.",
                    "later": "Queue one bounded SuperMega mission without running it.",
                }[operation],
                operation == "work", operation in {"work", "later"},
            )
        if operation == "code":
            if values not in ([], ["--check"]):
                raise ValueError("supermega_code_accepts_only_optional_check")
            return LaunchAction(
                tuple(values), "supermega-code", "Open or check the exact local SuperMega coding workspace.",
                not values, False,
            )
        if operation == "dashboard":
            if values:
                raise ValueError("supermega_dashboard_accepts_no_arguments")
            return LaunchAction(("service", "start"), "dashboard", "Start the owner-controlled SuperMega loopback dashboard.", False, True)
        raise ValueError("supermega_operation_unknown")
    if name == "ask":
        return LaunchAction(
            _ask_command(name, "company", tail), "ask",
            "Ask a read-only local model using verified active-company context.",
            True, False,
        )
    if name == "plan":
        return LaunchAction(("preflight", *_require_tail(name, tail)), "plan", "Preview the team, evidence, and owner gates without starting work.", False, False)
    if name == "experiment":
        if len(tail) > 1 or (tail and not tail[0].strip()):
            raise ValueError("experiment_accepts_at_most_one_project")
        return LaunchAction(tuple(tail), "experiment", "Plan the next category-balanced measured product test without loading a model.", False, False)
    if name == "experiment-run":
        recover_count = tail.count("--recover-memory")
        project_values = [item for item in tail if item != "--recover-memory"]
        if (
            recover_count > 1 or len(project_values) > 1
            or any(not item.strip() or item.startswith("--") for item in project_values)
        ):
            raise ValueError("experiment_run_accepts_at_most_one_project")
        command = tuple(project_values + (["--recover-memory"] if recover_count else []))
        return LaunchAction(command, "experiment-run", "Run the next planned product test locally, preserve an accepted receipt, and stop before human review.", True, True)
    if name == "offer":
        if len(tail) > 1 or (tail and not tail[0].strip()):
            raise ValueError("offer_accepts_at_most_one_project")
        return LaunchAction(tuple(tail), "offer", "Check whether repeatable measured evidence supports owner-reviewed offer packaging.", False, False)
    if name == "offer-pack":
        if len(tail) > 1 or (tail and not tail[0].strip()):
            raise ValueError("offer_pack_accepts_at_most_one_project")
        return LaunchAction(tuple(tail), "offer-pack", "Write a local owner-review pack only from evidence-gated offer claims.", False, True)
    if name == "validation-pack":
        if len(tail) > 1 or (tail and not tail[0].strip()):
            raise ValueError("validation_pack_accepts_at_most_one_project")
        return LaunchAction(tuple(tail), "validation-pack", "Write a private evidence scoreboard and exact next product experiment.", False, True)
    if name in {"mission-candidate", "mission-review"}:
        if len(tail) > 1 or (tail and not tail[0].strip()):
            raise ValueError(f"{name.replace('-', '_')}_accepts_at_most_one_project")
        return LaunchAction(
            tuple(tail), name,
            (
                "Show one integrity-checked sealed mission and its result without changing state."
                if name == "mission-candidate" else
                "Inspect and explicitly record one actual human review of a sealed mission."
            ),
            False, name == "mission-review",
        )
    if name == "experiment-pending":
        if tail:
            raise ValueError("experiment_pending_accepts_no_arguments")
        return LaunchAction((), "experiment-pending", "Inspect pending measured experiment receipts without changing state.", False, False)
    if name == "experiment-review":
        if not tail:
            raise ValueError("experiment_review_arguments_required")
        return LaunchAction(tuple(tail), "experiment-review", "Record one explicitly confirmed human review of a pending measured experiment.", False, True)
    if name == "experiment-review-interactive":
        if tail:
            raise ValueError("experiment_review_interactive_accepts_no_arguments")
        return LaunchAction((), "experiment-review-interactive", "Inspect and explicitly review one pending measured experiment through local prompts.", False, True)
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
        if tail.count("--recover-memory") > 1:
            raise ValueError("cycle_recover_memory_may_be_used_once")
        return LaunchAction(tuple(tail), "cycle", "Materialize due schedules and run at most one exact, gate-cleared local mission.", True, True)
    if name == "autopilot":
        if len(tail) != 1 or tail[0].lower() not in {"install", "status", "repair", "remove"}:
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
    if name == "brief":
        if tail:
            raise ValueError("brief_accepts_no_arguments")
        return LaunchAction((), "brief", "Combine local autonomy, queue, evidence, and offer gates into one next action.", False, False)
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


def run_ask(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    runner = project_root / "scripts" / "run_local_brief_assistant.py"
    completed = subprocess.run(
        [sys.executable, str(runner), *action.command],
        cwd=project_root, check=False,
    )
    return completed.returncode


def _resolved_product_project(action: LaunchAction, home: Path) -> str:
    project = next((item for item in action.command if item != "--recover-memory"), None)
    if project is not None:
        return project
    from local_company.focus import read_execution_focus

    focus = read_execution_focus(home)
    if focus.get("enabled") is not True:
        raise ValueError("product_project_required_or_enable_focus")
    candidate = focus.get("projectId") or focus.get("projectName")
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("product_focus_project_missing")
    return candidate


def _product_experiment_plan(action: LaunchAction, root: Path) -> dict[str, object]:
    source = str(root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError

    home = default_company_home()
    project = _resolved_product_project(action, home)
    try:
        return CompanyTools(home).product_experiment_next({"project": project})
    except ProtocolError as error:
        raise ValueError(error.message) from error


def run_experiment(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    result = _product_experiment_plan(action, project_root)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _mission_review_source(
    action: LaunchAction, project_root: Path,
) -> tuple[Path, object, dict[str, object]]:
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError

    home = default_company_home()
    project = _resolved_product_project(action, home)
    tools = CompanyTools(home)
    try:
        candidate_result = tools.product_evidence_next({"project": project})
    except ProtocolError as error:
        raise ValueError(error.message) from error
    if not isinstance(candidate_result, dict):
        raise ValueError("mission_review_candidate_invalid")
    if (
        candidate_result.get("schema") != "local-company.mcp-product-evidence-next.v1"
        or candidate_result.get("modelCalled") is not False
        or candidate_result.get("stateMutated") is not False
        or candidate_result.get("externalActionPerformed") is not False
        or candidate_result.get("status") not in {"candidate_ready", "no_candidate"}
    ):
        raise ValueError("mission_review_candidate_invalid")
    candidate = candidate_result.get("candidate")
    if candidate is None:
        if candidate_result["status"] != "no_candidate":
            raise ValueError("mission_review_candidate_invalid")
        return home, tools, {
            "schema": MISSION_REVIEW_NEXT_SCHEMA, "status": "no_candidate",
            "candidate": None, "jobResult": None,
            "evidenceProgress": candidate_result.get("evidenceProgress"),
            "humanReviewFields": candidate_result.get("humanReviewFields"),
            "nextAction": "run_and_review_another_bounded_product_mission",
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }
    if not isinstance(candidate, dict) or candidate_result["status"] != "candidate_ready":
        raise ValueError("mission_review_candidate_invalid")
    job_id = candidate.get("jobId")
    candidate_project = candidate.get("project")
    candidate_roles = candidate.get("roles")
    report_sha256 = candidate.get("reportSha256")
    evidence_sha256 = candidate.get("evidenceManifestSha256")
    if (
        not isinstance(job_id, str) or PENDING_EXPERIMENT_ID.fullmatch(job_id) is None
        or candidate.get("status") != "complete"
        or not isinstance(candidate.get("objective"), str)
        or not candidate["objective"].strip()
        or not isinstance(candidate_project, str) or not candidate_project.strip()
        or not isinstance(candidate_roles, list)
        or not candidate_roles
        or any(not isinstance(role, str) or not role.strip() for role in candidate_roles)
        or not isinstance(report_sha256, str)
        or SHA256_PATTERN.fullmatch(report_sha256) is None
        or not isinstance(evidence_sha256, str)
        or SHA256_PATTERN.fullmatch(evidence_sha256) is None
        or type(candidate.get("qualityPassed")) is not bool
    ):
        raise ValueError("mission_review_candidate_invalid")
    try:
        job_result = tools.job_result({"jobId": job_id})
    except ProtocolError as error:
        raise ValueError(error.message) from error
    if not isinstance(job_result, dict):
        raise ValueError("mission_review_job_result_invalid")
    job = job_result.get("job")
    quality = job_result.get("quality")
    checks = quality.get("checks") if isinstance(quality, dict) else None
    if (
        job_result.get("schema") != "local-company.mcp-job-result.v1"
        or job_result.get("status") != "ready"
        or job_result.get("modelCalled") is not False
        or job_result.get("stateMutated") is not False
        or job_result.get("externalActionPerformed") is not False
        or not isinstance(job, dict) or job.get("jobId") != job_id
        or job.get("status") != "complete" or job.get("reportAvailable") is not True
        or job.get("objective") != candidate.get("objective")
        or job.get("project") != candidate_project
        or job.get("roles") != candidate_roles
        or not isinstance(job.get("synthesis"), str) or not job["synthesis"].strip()
        or job.get("synthesisTruncated") is not False
        or job.get("reportSha256") != report_sha256
        or job.get("evidenceManifestSha256") != evidence_sha256
        or not isinstance(quality, dict) or not isinstance(checks, dict)
        or quality.get("passed") is not candidate.get("qualityPassed")
        or quality.get("score") != candidate.get("qualityScore")
        or checks.get("report_integrity_valid") is not True
        or checks.get("evidence_manifest_valid") is not True
        or checks.get("model_stopped_cleanly") is not True
    ):
        raise ValueError("mission_review_job_result_invalid")
    return home, tools, {
        "schema": MISSION_REVIEW_NEXT_SCHEMA, "status": "candidate_ready",
        "candidate": candidate, "jobResult": job_result,
        "evidenceProgress": candidate_result.get("evidenceProgress"),
        "humanReviewFields": candidate_result.get("humanReviewFields"),
        "nextAction": "inspect_result_then_run_mission_review",
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }


def run_mission_candidate(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    _, _, result = _mission_review_source(action, project_root)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if result["status"] == "candidate_ready" else 1


def _review_integer(value: str, *, minimum: int, maximum: int, reason: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(reason) from error
    if not minimum <= parsed <= maximum:
        raise ValueError(reason)
    return parsed


def run_mission_review(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    _, tools, source = _mission_review_source(action, project_root)
    if source["status"] != "candidate_ready":
        print(json.dumps(source, separators=(",", ":"), sort_keys=True))
        return 1
    candidate = source["candidate"]
    job_result = source["jobResult"]
    if not isinstance(candidate, dict) or not isinstance(job_result, dict):
        raise ValueError("mission_review_source_invalid")
    job = job_result.get("job")
    quality = job_result.get("quality")
    if not isinstance(job, dict) or not isinstance(quality, dict):
        raise ValueError("mission_review_source_invalid")
    roles = candidate.get("roles")
    if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
        raise ValueError("mission_review_source_invalid")

    print("\nSealed local mission ready for your actual human review:\n")
    print(f"Job ID: {candidate['jobId']}")
    print(f"Project: {candidate.get('project')}")
    print(f"Roles: {', '.join(roles)}")
    print(f"Automated quality: {'passed' if quality.get('passed') is True else 'failed'} ({quality.get('score')})")
    print(f"Report SHA-256: {candidate.get('reportSha256')}")
    print(f"Evidence SHA-256: {candidate.get('evidenceManifestSha256')}")
    print(f"\nObjective:\n{job.get('objective')}\n")
    print(f"Result:\n{job.get('synthesis')}\n")
    print("Automated quality is not human acceptance. Record only what you actually observed.\n")

    category = input("Workflow category (coding/business/data-research): ").strip().lower()
    if category not in {"coding", "business", "data-research"}:
        raise ValueError("mission_review_category_invalid")
    decision = input("Decision (accepted/rejected): ").strip().lower()
    if decision not in {"accepted", "rejected"}:
        raise ValueError("mission_review_decision_invalid")
    if decision == "accepted":
        outcome_reason = "none"
    else:
        print("Rejection reasons: " + ", ".join(PRODUCT_OUTCOME_REASONS[1:]))
        outcome_reason = input("Primary rejection reason: ").strip().lower()
        if outcome_reason not in PRODUCT_OUTCOME_REASONS[1:]:
            raise ValueError("mission_review_outcome_reason_invalid")
    corrections = _review_integer(
        input("Actual correction count (0-100): ").strip(),
        minimum=0, maximum=100, reason="mission_review_corrections_invalid",
    )
    paid_signal = input(
        "Did a real person indicate willingness to pay for setup? (yes/no/unknown): "
    ).strip().lower()
    if paid_signal not in {"yes", "no", "unknown"}:
        raise ValueError("mission_review_paid_signal_invalid")
    peak_raw = input("Measured peak memory MiB (blank if unavailable): ").strip()
    peak_memory = (
        None if not peak_raw else _review_integer(
            peak_raw, minimum=1, maximum=1_000_000,
            reason="mission_review_peak_memory_invalid",
        )
    )
    confirmation = input(
        "Type RECORD HUMAN PRODUCT EVIDENCE REVIEW to save this judgment: "
    ).strip()
    if confirmation != "RECORD HUMAN PRODUCT EVIDENCE REVIEW":
        raise ValueError("mission_review_confirmation_required")

    from local_company.mcp_server import PRODUCT_REVIEW_CONFIRMATION, ProtocolError
    try:
        refreshed = tools.product_evidence_next({"project": candidate.get("project")})
    except ProtocolError as error:
        raise ValueError("mission_review_candidate_recheck_failed") from error
    if not isinstance(refreshed, dict):
        raise ValueError("mission_review_candidate_changed")
    refreshed_candidate = refreshed.get("candidate")
    identity_fields = ("jobId", "reportSha256", "evidenceManifestSha256")
    if (
        refreshed.get("schema") != "local-company.mcp-product-evidence-next.v1"
        or refreshed.get("status") != "candidate_ready"
        or not isinstance(refreshed_candidate, dict)
        or any(refreshed_candidate.get(field) != candidate.get(field) for field in identity_fields)
        or refreshed.get("stateMutated") is not False
        or refreshed.get("modelCalled") is not False
        or refreshed.get("externalActionPerformed") is not False
    ):
        raise ValueError("mission_review_candidate_changed")
    try:
        recorded = tools.product_evidence_review({
            "jobId": candidate["jobId"], "category": category,
            "decision": decision, "outcomeReason": outcome_reason,
            "corrections": corrections, "paidSetupSignal": paid_signal,
            "peakMemoryMb": peak_memory,
            "reviewConfirmation": PRODUCT_REVIEW_CONFIRMATION,
        })
        progress = tools.product_evidence_status({
            "project": candidate.get("project"), "includeReviews": False,
        })
    except ProtocolError as error:
        raise ValueError(error.message) from error
    if (
        recorded.get("schema") != "local-company.mcp-product-evidence-review.v1"
        or recorded.get("status") != "recorded" or recorded.get("recorded") is not True
        or recorded.get("modelCalled") is not False
        or recorded.get("externalActionPerformed") is not False
        or progress.get("modelCalled") is not False
        or progress.get("stateMutated") is not False
        or progress.get("externalActionPerformed") is not False
    ):
        raise ValueError("mission_review_record_verification_failed")
    print(json.dumps({
        "schema": MISSION_HUMAN_REVIEW_SCHEMA, "status": "recorded",
        "recorded": True, "jobId": candidate["jobId"],
        "review": recorded.get("review"),
        "evidenceProgress": {
            "reviewedMissions": progress.get("reviewed_missions"),
            "remainingMissions": progress.get("remaining_missions"),
            "completeMeasurements": progress.get("complete_measurements"),
            "missingProof": progress.get("missing_proof"),
        },
        "modelCalled": False, "stateMutated": True,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _product_offer_result(action: LaunchAction, project_root: Path) -> tuple[Path, dict[str, object]]:
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError

    home = default_company_home()
    project = _resolved_product_project(action, home)
    try:
        result = CompanyTools(home).product_offer_next({"project": project})
    except ProtocolError as error:
        raise ValueError(error.message) from error
    return home, result


def run_offer(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    _, result = _product_offer_result(action, project_root)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if result.get("status") == "ready_for_owner_packaging" else 1


def _markdown_text(value: object, *, limit: int = 200, reason: str) -> str:
    normalized = " ".join(value.split()) if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit:
        raise ValueError(reason)
    return (
        normalized.replace("\\", "\\\\").replace("`", "\\`")
        .replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")
    )


def _store_private_markdown(
    home: Path, directory_name: str, filename_prefix: str, document_id: str,
    content: str, *, reason_prefix: str,
) -> tuple[Path, bool, str]:
    encoded = content.encode("utf-8")
    if len(encoded) > 65_536:
        raise ValueError(f"{reason_prefix}_too_large")
    directory = home / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"{reason_prefix}_directory_unsafe")
    target = directory / f"{filename_prefix}-{document_id}.md"
    stored = False
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
            raise ValueError(f"{reason_prefix}_collision")
    else:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=f".{filename_prefix}-",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            stored = True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return target, stored, hashlib.sha256(encoded).hexdigest()


def _offer_pack_text(result: dict[str, object], pack_id: str) -> str:
    project = result.get("project")
    offer = result.get("offer")
    if not isinstance(project, dict) or not isinstance(offer, dict):
        raise ValueError("offer_pack_source_invalid")

    project_name = _markdown_text(project.get("name"), limit=80, reason="offer_pack_source_invalid")
    workflow = _markdown_text(offer.get("workflow"), limit=80, reason="offer_pack_source_invalid")
    category = _markdown_text(offer.get("category"), limit=40, reason="offer_pack_source_invalid")
    evidence_runs = offer.get("evidenceRuns")
    corrections = offer.get("maximumCorrectionsObserved")
    peak_memory = offer.get("maximumPeakMemoryMbObserved")
    runtime_range = offer.get("runtimeSecondsRange")
    if (
        type(evidence_runs) is not int or evidence_runs < 2
        or type(corrections) is not int or corrections < 0
        or type(peak_memory) is not int or peak_memory < 1
        or not isinstance(runtime_range, dict)
        or type(runtime_range.get("minimum")) not in {int, float}
        or type(runtime_range.get("maximum")) not in {int, float}
    ):
        raise ValueError("offer_pack_source_invalid")

    def bullets(name: str) -> list[str]:
        values = offer.get(name)
        if (
            not isinstance(values, list) or not 1 <= len(values) <= 20
            or any(not isinstance(item, str) for item in values)
        ):
            raise ValueError("offer_pack_source_invalid")
        return [f"- {_markdown_text(item, reason='offer_pack_source_invalid')}" for item in values]

    return "\n".join([
        f"# Private Local AI Workflow Offer: {workflow}", "",
        "**Status: owner-review draft; not published and not authorized for external use.**", "",
        f"Offer pack ID: `{pack_id}`", f"Project: {project_name}",
        f"Workflow category: {category}", "",
        "## What the customer receives", "", *bullets("package"), "",
        "## Integrity-checked local evidence", "",
        f"- Supporting accepted runs: {evidence_runs}",
        f"- Maximum corrections observed: {corrections}",
        f"- Runtime observed: {runtime_range['minimum']} to {runtime_range['maximum']} seconds",
        f"- Maximum peak memory observed: {peak_memory} MiB", "",
        "## Claims supported by current evidence", "", *bullets("allowedClaims"), "",
        "## Claims this pack does not support", "", *bullets("prohibitedClaims"), "",
        "## Owner decisions still required", "",
        "- Confirm the target customer and their workflow baseline.",
        "- Choose scope, price, support terms, and acceptance test.",
        "- Review every claim before sharing or publishing.",
        "- Keep credentials, payments, deployment, and external messages behind explicit approval.", "",
    ])


def run_offer_pack(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    home, result = _product_offer_result(action, project_root)
    if result.get("status") != "ready_for_owner_packaging":
        print(json.dumps({
            "schema": OFFER_PACK_SCHEMA, "status": "evidence_required",
            "missingProof": result.get("missingProof"), "packStored": False,
            "ownerReviewRequired": True, "externalPublicationAuthorized": False,
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }, separators=(",", ":"), sort_keys=True))
        return 1
    canonical = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    pack_id = hashlib.sha256(b"local-ai.offer-pack.v1\0" + canonical).hexdigest()[:12]
    target, stored, digest = _store_private_markdown(
        home, "offer-packs", "offer-pack", pack_id,
        _offer_pack_text(result, pack_id), reason_prefix="offer_pack",
    )
    print(json.dumps({
        "schema": OFFER_PACK_SCHEMA, "status": "ready_for_owner_review",
        "offerPackId": pack_id, "offerPackPath": str(target),
        "offerPackSha256": digest,
        "packStored": stored, "ownerReviewRequired": True,
        "stateMutated": stored,
        "externalPublicationAuthorized": False, "modelCalled": False,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _product_validation_result(
    action: LaunchAction, project_root: Path,
) -> tuple[Path, dict[str, object]]:
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError

    home = default_company_home()
    project = _resolved_product_project(action, home)
    tools = CompanyTools(home)
    try:
        evidence = tools.product_evidence_status({
            "project": project, "includeReviews": True, "reviewLimit": 20,
        })
        experiment = tools.product_experiment_next({"project": project})
        offer = tools.product_offer_next({"project": project})
    except ProtocolError as error:
        raise ValueError(error.message) from error
    return home, {"evidence": evidence, "experiment": experiment, "offer": offer}


def _validation_pack_text(source: dict[str, object], dossier_id: str) -> str:
    evidence = source.get("evidence")
    experiment = source.get("experiment")
    offer = source.get("offer")
    if not all(isinstance(item, dict) for item in (evidence, experiment, offer)):
        raise ValueError("validation_pack_source_invalid")
    project = evidence.get("project")
    if not isinstance(project, dict):
        raise ValueError("validation_pack_source_invalid")
    project_name = _markdown_text(
        project.get("name"), limit=80, reason="validation_pack_source_invalid",
    )
    target = evidence.get("mission_target")
    reviewed = evidence.get("reviewed_missions")
    remaining = evidence.get("remaining_missions")
    measurements = evidence.get("complete_measurements")
    stale = evidence.get("stale_review_count")
    average = evidence.get("average_corrections")
    if (
        any(type(value) is not int or value < 0 for value in (target, reviewed, remaining, measurements, stale))
        or (average is not None and type(average) not in {int, float})
    ):
        raise ValueError("validation_pack_source_invalid")

    def counts(name: str, allowed: tuple[str, ...]) -> list[str]:
        values = evidence.get(name)
        if not isinstance(values, dict) or set(values) != set(allowed):
            raise ValueError("validation_pack_source_invalid")
        rows = []
        for label in allowed:
            count = values.get(label)
            if type(count) is not int or count < 0:
                raise ValueError("validation_pack_source_invalid")
            rows.append(f"| {label} | {count} |")
        return rows

    missing = evidence.get("missing_proof")
    reviews = evidence.get("reviews")
    gate_results = offer.get("gateResults")
    offer_missing = offer.get("missingProof")
    if (
        not isinstance(missing, list) or any(not isinstance(item, str) for item in missing)
        or not isinstance(reviews, list) or len(reviews) > 20
        or not isinstance(gate_results, dict)
        or not isinstance(offer_missing, list)
        or any(not isinstance(item, str) for item in offer_missing)
    ):
        raise ValueError("validation_pack_source_invalid")

    review_rows = []
    for item in reviews:
        if not isinstance(item, dict):
            raise ValueError("validation_pack_source_invalid")
        category = _markdown_text(item.get("category"), limit=40, reason="validation_pack_source_invalid")
        decision = _markdown_text(item.get("decision"), limit=20, reason="validation_pack_source_invalid")
        paid = _markdown_text(item.get("paid_setup_signal"), limit=20, reason="validation_pack_source_invalid")
        outcome_reason = _markdown_text(
            item.get("outcome_reason"), limit=40, reason="validation_pack_source_invalid",
        )
        corrections = item.get("corrections")
        runtime = item.get("runtime_seconds")
        peak = item.get("peak_memory_mb")
        if (
            type(corrections) is not int or corrections < 0
            or type(runtime) not in {int, float} or runtime < 0
            or (peak is not None and (type(peak) is not int or peak < 1))
        ):
            raise ValueError("validation_pack_source_invalid")
        label_value = item.get("label") or item.get("job_id") or item.get("source")
        if item.get("label") and isinstance(item.get("experiment_id"), str):
            label_value = f"{item['label']} ({item['experiment_id']})"
        label = _markdown_text(label_value, limit=80, reason="validation_pack_source_invalid")
        review_rows.append(
            f"| {label} | {category} | {decision} | {outcome_reason} | {corrections} | {paid} | "
            f"{runtime} | {peak if peak is not None else 'not recorded'} |"
        )
    if not review_rows:
        review_rows.append("| No reviewed runs yet | - | - | - | - | - | - | - |")

    gate_rows = []
    for name in sorted(gate_results):
        value = gate_results[name]
        if type(value) is not bool:
            raise ValueError("validation_pack_source_invalid")
        gate_rows.append(f"| {_markdown_text(name, reason='validation_pack_source_invalid')} | {'pass' if value else 'missing'} |")

    next_lines = ["Milestone reached; define the next owner-reviewed validation milestone."]
    planned = experiment.get("experiment")
    if planned is not None:
        if not isinstance(planned, dict):
            raise ValueError("validation_pack_source_invalid")
        next_lines = [
            f"Category: {_markdown_text(experiment.get('selectedCategory'), limit=40, reason='validation_pack_source_invalid')}",
            f"Workflow label: {_markdown_text(planned.get('label'), limit=80, reason='validation_pack_source_invalid')}",
            "Run locally: `local-ai.cmd experiment-run --recover-memory`",
            "Then review: `local-ai.cmd experiment-review-interactive`",
        ]

    return "\n".join([
        "# Local AI Product Validation Dossier", "",
        "**Status: private validation record; not a sales claim or publication-ready paper.**", "",
        f"Dossier ID: `{dossier_id}`", f"Project: {project_name}", "",
        "## Evidence scoreboard", "", "| Metric | Current | Target |", "|---|---:|---:|",
        f"| Human-reviewed missions | {reviewed} | {target} |",
        f"| Complete memory measurements | {measurements} | {target} |",
        f"| Remaining missions | {remaining} | 0 |",
        f"| Stale evidence bindings | {stale} | 0 |",
        f"| Average corrections | {average if average is not None else 'not available'} | lower is better |", "",
        "## Category coverage", "", "| Category | Reviews |", "|---|---:|",
        *counts("category_counts", ("business", "coding", "data-research")), "",
        "## Human outcomes", "", "| Outcome | Count |", "|---|---:|",
        *counts("decision_counts", ("accepted", "rejected")), "",
        "## Paid-setup observations", "", "| Signal | Count |", "|---|---:|",
        *counts("paid_setup_signal_counts", ("no", "unknown", "yes")), "",
        "## Outcome diagnostics", "", "| Reason | Count |", "|---|---:|",
        *counts("outcome_reason_counts", (
            "inaccurate", "incomplete", "legacy_unspecified", "none", "not_actionable",
            "other", "too_resource_heavy", "too_slow", "tool_failure", "unsafe",
        )), "",
        "## Reviewed runs", "",
        "| Workflow | Category | Decision | Reason | Corrections | Paid setup | Runtime seconds | Peak memory MiB |",
        "|---|---|---|---|---:|---|---:|---:|", *review_rows, "",
        "## Product milestone gaps", "",
        *([f"- {_markdown_text(item, reason='validation_pack_source_invalid')}" for item in missing] or ["- None"]), "",
        "## Offer gate", "", "| Gate | Result |", "|---|---|", *gate_rows, "",
        "Missing commercial proof:",
        *([f"- {_markdown_text(item, reason='validation_pack_source_invalid')}" for item in offer_missing] or ["- None"]), "",
        "## Exact next experiment", "", *next_lines, "",
        "## Boundary", "",
        "No customer demand, revenue, ROI, production readiness, or publication authorization is inferred by this dossier.", "",
    ])


def run_validation_pack(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    home, source = _product_validation_result(action, project_root)
    canonical = json.dumps(source, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    dossier_id = hashlib.sha256(
        VALIDATION_PACK_SCHEMA.encode("ascii") + b"\0" + canonical,
    ).hexdigest()[:12]
    target, stored, digest = _store_private_markdown(
        home, "validation-packs", "validation-pack", dossier_id,
        _validation_pack_text(source, dossier_id), reason_prefix="validation_pack",
    )
    evidence = source["evidence"]
    offer = source["offer"]
    print(json.dumps({
        "schema": VALIDATION_PACK_SCHEMA, "status": "ready_for_owner_review",
        "validationPackId": dossier_id, "validationPackPath": str(target),
        "validationPackSha256": digest, "packStored": stored,
        "reviewedMissions": evidence.get("reviewed_missions"),
        "missionTarget": evidence.get("mission_target"),
        "offerStatus": offer.get("status"), "stateMutated": stored,
        "externalPublicationAuthorized": False, "modelCalled": False,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _invoke_experiment_runner(
    project_root: Path, prompt: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, str(project_root / "scripts" / "run_local_company_prompt.py"), prompt],
        cwd=project_root, capture_output=True, text=True, check=False,
    )
    candidate = completed.stdout.strip() or completed.stderr.strip()
    if len(candidate) > 65_536:
        raise ValueError("experiment_runner_receipt_too_large")
    try:
        runner_receipt = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ValueError("experiment_runner_receipt_invalid") from error
    if not isinstance(runner_receipt, dict) or runner_receipt.get("schema") != "local-ai.company-prompt-result.v1":
        raise ValueError("experiment_runner_receipt_invalid")
    return completed, runner_receipt


def _pending_experiment_directory(home: Path, *, reviewed: bool = False) -> Path:
    name = "reviewed-product-experiments" if reviewed else "pending-product-experiments"
    return home.resolve() / name


def _pending_experiment_payload(
    plan: dict[str, object], experiment: dict[str, object],
    runner_receipt: dict[str, object],
) -> dict[str, object]:
    identity = {
        "project": plan["project"], "label": experiment["label"],
        "category": plan["selectedCategory"],
        "requiredActions": experiment["requiredActions"],
        "runnerReceipt": runner_receipt,
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    experiment_id = hashlib.sha256(
        b"local-ai.pending-product-experiment.v1\0" + canonical,
    ).hexdigest()[:12]
    return {"schema": PENDING_EXPERIMENT_SCHEMA, "experimentId": experiment_id, **identity}


def _pending_experiment_bytes(payload: dict[str, object]) -> bytes:
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > 65_536:
        raise ValueError("pending_experiment_too_large")
    return encoded


def _validate_pending_experiment(payload: object) -> dict[str, object]:
    required = {
        "schema", "experimentId", "project", "label", "category",
        "requiredActions", "runnerReceipt",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("pending_experiment_invalid")
    experiment_id = payload.get("experimentId")
    project = payload.get("project")
    if (
        payload.get("schema") != PENDING_EXPERIMENT_SCHEMA
        or not isinstance(experiment_id, str)
        or PENDING_EXPERIMENT_ID.fullmatch(experiment_id) is None
        or not isinstance(project, dict)
        or not isinstance(project.get("id"), str)
        or not isinstance(project.get("name"), str)
        or not isinstance(payload.get("label"), str)
        or payload.get("category") not in {"coding", "business", "data-research"}
        or not isinstance(payload.get("requiredActions"), list)
        or not isinstance(payload.get("runnerReceipt"), dict)
    ):
        raise ValueError("pending_experiment_invalid")
    expected = _pending_experiment_payload(
        {"project": project, "selectedCategory": payload["category"]},
        {"label": payload["label"], "requiredActions": payload["requiredActions"]},
        payload["runnerReceipt"],
    )
    if expected != payload:
        raise ValueError("pending_experiment_integrity_invalid")
    return dict(payload)


def _store_pending_experiment(home: Path, payload: dict[str, object]) -> tuple[str, bool]:
    validated = _validate_pending_experiment(payload)
    directory = _pending_experiment_directory(home)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("pending_experiment_directory_unsafe")
    experiment_id = str(validated["experimentId"])
    target = directory / f"pending-{experiment_id}.json"
    encoded = _pending_experiment_bytes(validated)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != encoded:
            raise ValueError("pending_experiment_collision")
        return experiment_id, False
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=".pending-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return experiment_id, True


def _load_pending_experiment(
    home: Path, experiment_id: str, *, reviewed: bool = False,
) -> tuple[Path, dict[str, object]]:
    if PENDING_EXPERIMENT_ID.fullmatch(experiment_id) is None:
        raise ValueError("pending_experiment_id_invalid")
    path = _pending_experiment_directory(home, reviewed=reviewed) / f"pending-{experiment_id}.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("pending_experiment_unknown") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65_536:
        raise ValueError("pending_experiment_file_unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("pending_experiment_invalid") from error
    validated = _validate_pending_experiment(payload)
    if validated["experimentId"] != experiment_id:
        raise ValueError("pending_experiment_integrity_invalid")
    return path, validated


def _pending_experiment_items(home: Path, *, reviewed: bool = False) -> list[dict[str, object]]:
    directory = _pending_experiment_directory(home, reviewed=reviewed)
    items: list[dict[str, object]] = []
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("pending_experiment_directory_unsafe")
        paths = sorted(directory.glob("pending-*.json"))
        if len(paths) > 100:
            raise ValueError("pending_experiment_limit_exceeded")
        for path in paths:
            match = re.fullmatch(r"pending-([0-9a-f]{12})\.json", path.name)
            if match is None:
                raise ValueError("pending_experiment_filename_invalid")
            _, payload = _load_pending_experiment(home, match.group(1), reviewed=reviewed)
            receipt = payload["runnerReceipt"]
            items.append({
                "experimentId": payload["experimentId"], "project": payload["project"],
                "label": payload["label"], "category": payload["category"],
                "response": receipt.get("response"), "model": receipt.get("model"),
                "wallSeconds": receipt.get("wallSeconds"),
                "peakIncrementalMemoryMb": receipt.get("peakIncrementalMemoryMb"),
            })
    return items


def _filter_product_experiment_items(
    items: list[dict[str, object]], command: tuple[str, ...],
) -> tuple[list[dict[str, object]], str | None]:
    if not command:
        return items, None
    if len(command) != 1 or not command[0].strip():
        raise ValueError("product_experiment_filter_invalid")
    target = command[0]
    filtered = []
    for item in items:
        project = item.get("project")
        if (
            isinstance(project, dict)
            and target in {project.get("id"), project.get("name")}
        ):
            filtered.append(item)
    return filtered, target


def run_pending_experiments(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home

    items, project_filter = _filter_product_experiment_items(
        _pending_experiment_items(default_company_home()), action.command,
    )
    print(json.dumps({
        "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
        "items": items, "count": len(items), "projectFilter": project_filter,
        "modelCalled": False,
        "stateMutated": False, "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def _experiment_review_values(command: tuple[str, ...]) -> dict[str, object]:
    if not command or PENDING_EXPERIMENT_ID.fullmatch(command[0]) is None:
        raise ValueError("experiment_review_id_invalid")
    values: dict[str, object] = {"experimentId": command[0]}
    index = 1
    while index < len(command):
        token = command[index]
        if token == "--confirm-human-review":
            if "confirmed" in values:
                raise ValueError("experiment_review_arguments_invalid")
            values["confirmed"] = True
            index += 1
            continue
        names = {
            "--decision": "decision", "--outcome-reason": "outcomeReason",
            "--corrections": "corrections", "--paid-setup": "paidSetupSignal",
        }
        name = names.get(token)
        if name is None or name in values or index + 1 >= len(command):
            raise ValueError("experiment_review_arguments_invalid")
        raw = command[index + 1]
        if name == "corrections":
            try:
                values[name] = int(raw)
            except ValueError as error:
                raise ValueError("experiment_review_corrections_invalid") from error
        else:
            values[name] = raw
        index += 2
    if set(values) != {
        "experimentId", "decision", "outcomeReason", "corrections",
        "paidSetupSignal", "confirmed",
    }:
        raise ValueError("experiment_review_arguments_required")
    if (
        values["outcomeReason"] not in PRODUCT_OUTCOME_REASONS
        or (values["decision"] == "accepted") != (values["outcomeReason"] == "none")
    ):
        raise ValueError("experiment_review_outcome_reason_invalid")
    return values


def run_experiment_review(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import (
        CompanyTools, PRODUCT_EXPERIMENT_CONFIRMATION, ProtocolError,
    )

    values = _experiment_review_values(action.command)
    home = default_company_home()
    was_pending = True
    try:
        source_path, pending = _load_pending_experiment(home, str(values["experimentId"]))
    except ValueError as error:
        if str(error) != "pending_experiment_unknown":
            raise
        was_pending = False
        source_path, pending = _load_pending_experiment(
            home, str(values["experimentId"]), reviewed=True,
        )
    project = pending["project"]
    try:
        result = CompanyTools(home).product_experiment_review({
            "experimentId": pending["experimentId"], "project": project["id"],
            "label": pending["label"], "category": pending["category"],
            "decision": values["decision"], "corrections": values["corrections"],
            "outcomeReason": values["outcomeReason"],
            "paidSetupSignal": values["paidSetupSignal"],
            "receipt": pending["runnerReceipt"],
            "reviewConfirmation": PRODUCT_EXPERIMENT_CONFIRMATION,
        })
    except ProtocolError as error:
        raise ValueError(error.message) from error
    if was_pending:
        reviewed_directory = _pending_experiment_directory(home, reviewed=True)
        reviewed_directory.mkdir(parents=True, exist_ok=True)
        if reviewed_directory.is_symlink() or not reviewed_directory.is_dir():
            raise ValueError("reviewed_experiment_directory_unsafe")
        destination = reviewed_directory / source_path.name
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != source_path.read_bytes():
                raise ValueError("reviewed_experiment_collision")
            source_path.unlink()
        else:
            os.replace(source_path, destination)
    print(json.dumps({
        "schema": "local-ai.product-experiment-human-review.v1",
        "status": "recorded", "recorded": True,
        "experimentId": pending["experimentId"], "pendingArchived": was_pending,
        "reviewUpdated": not was_pending,
        "review": result["review"], "modelCalled": False,
        "stateMutated": True, "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


def run_interactive_experiment_review(
    action: LaunchAction, root: Path | None = None,
) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home

    home = default_company_home()
    items, project_filter = _filter_product_experiment_items(
        _pending_experiment_items(home), action.command,
    )
    history_mode = False
    if not items:
        items, _ = _filter_product_experiment_items(
            _pending_experiment_items(home, reviewed=True), action.command,
        )
        history_mode = True
    if not items:
        print(json.dumps({
            "schema": "local-ai.product-experiment-interactive-review.v1",
            "status": "no_reviewable_experiment", "recorded": False,
            "projectFilter": project_filter,
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }, separators=(",", ":"), sort_keys=True))
        return 1
    print(
        "\nArchived measured product experiments (explicit re-review):\n"
        if history_mode else "\nPending measured product experiments:\n"
    )
    for item in items:
        print(f"ID: {item['experimentId']}  Category: {item['category']}  Label: {item['label']}")
        print(f"Response: {item['response']}\n")
    experiment_id = input("Experiment ID to review: ").strip().lower()
    decision = input("Decision (accepted/rejected): ").strip().lower()
    if decision == "accepted":
        outcome_reason = "none"
    else:
        print("Rejection reasons: " + ", ".join(PRODUCT_OUTCOME_REASONS[1:]))
        outcome_reason = input("Primary rejection reason: ").strip().lower()
    corrections = input("Actual correction count (0-100): ").strip()
    paid_signal = input("Would this justify a paid setup? (yes/no/unknown): ").strip().lower()
    confirmation = input("Type REVIEW to record this human judgment: ").strip()
    if confirmation != "REVIEW":
        raise ValueError("experiment_review_confirmation_required")
    review_action = LaunchAction((
        experiment_id, "--decision", decision, "--corrections", corrections,
        "--outcome-reason", outcome_reason, "--paid-setup", paid_signal,
        "--confirm-human-review",
    ), "experiment-review", "Record one interactive human product review.", False, True)
    return run_experiment_review(review_action, project_root)


def run_experiment_agent(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    plan = _product_experiment_plan(action, project_root)
    experiment = plan.get("experiment")
    if plan.get("status") != "experiment_ready" or not isinstance(experiment, dict):
        print(json.dumps({
            "schema": EXPERIMENT_RUN_SCHEMA, "ok": True, "status": "not_needed",
            "reason": "product_evidence_milestone_reached", "plan": plan,
            "modelCalled": False, "stateMutated": False,
            "externalActionPerformed": False,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    prompt = experiment.get("prompt")
    required_actions = experiment.get("requiredActions")
    if not isinstance(prompt, str) or not isinstance(required_actions, list):
        raise ValueError("experiment_plan_invalid")
    completed, runner_receipt = _invoke_experiment_runner(project_root, prompt)
    recovery = None
    attempts = 1
    if (
        "--recover-memory" in action.command
        and completed.returncode != 0
        and runner_receipt.get("status") == "blocked"
        and runner_receipt.get("reason") == "installed_models_memory_blocked"
        and runner_receipt.get("modelCalled") is False
    ):
        recovery = _recover_memory(project_root)
        if recovery is None:
            output = {
                "schema": EXPERIMENT_RUN_SCHEMA, "ok": False, "status": "error",
                "reason": "memory_recovery_invalid", "project": plan["project"],
                "selectedCategory": plan["selectedCategory"], "label": experiment.get("label"),
                "requiredActions": required_actions, "missingActions": required_actions,
                "runnerReceipt": runner_receipt, "attemptCount": attempts,
                "humanReviewRecorded": False, "modelCalled": False,
                "stateMutated": False, "externalActionPerformed": False,
                "nextAction": "inspect_verified_optimizer_before_retry",
            }
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return 2
        time.sleep(3.0)
        attempts += 1
        completed, runner_receipt = _invoke_experiment_runner(project_root, prompt)
    observed_actions = runner_receipt.get("toolActions", [])
    if not isinstance(observed_actions, list):
        observed_actions = []
    missing_actions = [name for name in required_actions if name not in observed_actions]
    preliminary_acceptance = (
        completed.returncode == 0
        and runner_receipt.get("ok") is True
        and runner_receipt.get("status") == "accepted"
        and runner_receipt.get("externalActionPerformed") is False
        and runner_receipt.get("paidApiUsed") is False
        and runner_receipt.get("autoPermissionsEnabled") is False
        and runner_receipt.get("modelCalled") is True
        and runner_receipt.get("modelUnloadedAfterRun") is True
        and runner_receipt.get("observedCost") == 0
        and not missing_actions
    )
    receipt_contract_valid = False
    if preliminary_acceptance:
        from local_company.mcp_server import CompanyTools, ProtocolError

        try:
            runner_receipt = CompanyTools._validated_company_prompt_receipt(runner_receipt)
            receipt_contract_valid = True
        except ProtocolError:
            receipt_contract_valid = False
    accepted = preliminary_acceptance and receipt_contract_valid
    status = (
        "accepted" if accepted else
        "rejected" if completed.returncode == 0 else
        runner_receipt.get("status", "blocked")
    )
    reason = (
        "accepted" if accepted else
        "required_company_actions_missing" if completed.returncode == 0 and missing_actions else
        "runner_receipt_not_reviewable" if completed.returncode == 0 and not receipt_contract_valid else
        runner_receipt.get("reason", "experiment_runner_failed")
    )
    pending_id = None
    pending_created = False
    if accepted:
        source = str(project_root / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        from local_company.config import default_company_home

        pending_payload = _pending_experiment_payload(plan, experiment, runner_receipt)
        pending_id, pending_created = _store_pending_experiment(
            default_company_home(), pending_payload,
        )
    output = {
        "schema": EXPERIMENT_RUN_SCHEMA, "ok": accepted, "status": status,
        "reason": reason, "project": plan["project"],
        "selectedCategory": plan["selectedCategory"], "label": experiment.get("label"),
        "requiredActions": required_actions, "missingActions": missing_actions,
        "runnerReceipt": runner_receipt, "attemptCount": attempts,
        "humanReviewRecorded": False, "pendingExperimentId": pending_id,
        "pendingReceiptStored": pending_id is not None,
        "pendingReceiptCreated": pending_created,
        "nextAction": (
            "inspect_experiment_pending_then_record_actual_human_review"
            if accepted else "resolve_runner_block_or_failed_acceptance_check"
        ),
        "modelCalled": runner_receipt.get("modelCalled") is True,
        "stateMutated": pending_id is not None,
        "externalActionPerformed": runner_receipt.get("externalActionPerformed") is True,
    }
    if recovery is not None:
        output["memoryRecovery"] = recovery
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if accepted else 1


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


def run_supermega_code(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    repository = project_root.parent / SUPERMEGA_REPOSITORY_DIRECTORY
    forwarded = LaunchAction(
        (*action.command, str(repository)), "code", action.description,
        action.model_may_run, False,
    )
    return run_code(forwarded, project_root)


def _supermega_repository_status(project_root: Path) -> dict[str, object]:
    candidate = project_root.parent / SUPERMEGA_REPOSITORY_DIRECTORY
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return {"status": "unavailable", "branch": None, "clean": None, "dirtyEntries": None}
        repository = candidate.resolve(strict=True)
        if repository.parent != project_root.parent.resolve(strict=True):
            return {"status": "unsafe", "branch": None, "clean": None, "dirtyEntries": None}
        environment = os.environ.copy()
        environment["GIT_NO_LAZY_FETCH"] = "1"
        completed = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1", "--branch"],
            check=False, capture_output=True, text=True, timeout=15, env=environment,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        return {"status": "unavailable", "branch": None, "clean": None, "dirtyEntries": None}
    encoded = (completed.stdout + completed.stderr).encode("utf-8", errors="replace")
    if completed.returncode != 0 or len(encoded) > MAX_GIT_STATUS_BYTES:
        return {"status": "unavailable", "branch": None, "clean": None, "dirtyEntries": None}
    lines = completed.stdout.splitlines()
    branch = None
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:].split("...", 1)[0].split(" ", 1)[0] or None
    dirty = sum(1 for line in lines[1:] if line.strip())
    return {"status": "ready", "branch": branch, "clean": dirty == 0, "dirtyEntries": dirty}


def run_supermega_status(action: LaunchAction, root: Path | None = None) -> int:
    del action
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    repository = _supermega_repository_status(project_root)
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.core import Company, MockModel, QUEUE_PREFLIGHT_SCHEMA
    from local_company.focus import read_execution_focus
    from local_company.mcp_server import CompanyTools, ProtocolError

    company_home = default_company_home()
    project: dict[str, object]
    knowledge: dict[str, object]
    focus: dict[str, object]
    queue: dict[str, object] = {
        "status": "unavailable", "queueId": None, "projectId": None,
        "blockers": [], "ownerGateCategories": [], "parkableForFocus": False,
        "parkedCount": None,
    }
    product_proof: dict[str, object] = {
        "status": "unavailable", "reviewedMissions": None,
        "missionTarget": None, "remainingMissions": None,
        "completeMeasurements": None, "pendingReviewCount": None,
        "milestoneReached": None, "nextCategory": None, "nextLabel": None,
    }
    company = None
    try:
        company = Company(company_home, MockModel())
        detail = company.project_detail(SUPERMEGA_PROJECT_NAME)
        project_row = detail["project"]
        project = {
            "status": "ready", "projectId": project_row[0], "name": project_row[1],
            "knowledgeSourceCount": len(detail["sources"]), "missionCount": len(detail["jobs"]),
        }
        freshness = company.knowledge_freshness(SUPERMEGA_PROJECT_NAME)
        knowledge = {
            "readyForUse": freshness["ready_for_use"],
            "sourceCount": freshness["source_count"],
            "statusCounts": freshness["status_counts"],
        }
        observed_focus = read_execution_focus(company_home)
        active = (
            observed_focus["enabled"] is True
            and observed_focus.get("projectId") == project_row[0]
        )
        focus = {
            "supermegaActive": active,
            "currentProject": observed_focus.get("projectName"),
            "maxRoles": observed_focus.get("maxRoles"),
        }
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        project = {"status": "unavailable", "projectId": None, "name": SUPERMEGA_PROJECT_NAME, "knowledgeSourceCount": None, "missionCount": None}
        knowledge = {"readyForUse": False, "sourceCount": None, "statusCounts": None}
        focus = {"supermegaActive": False, "currentProject": None, "maxRoles": None}

    if company is not None and project["status"] == "ready":
        try:
            preflight = company.queue_preflight()
            blockers = preflight.get("blockers")
            owner_gates = preflight.get("owner_gate_categories")
            preflight_effects = preflight.get("effects")
            queue = {
                "status": preflight.get("status"),
                "queueId": preflight.get("queue_id"),
                "projectId": preflight.get("project_id"),
                "blockers": list(blockers[:10]) if isinstance(blockers, list) else [],
                "ownerGateCategories": (
                    list(owner_gates[:10]) if isinstance(owner_gates, list) else []
                ),
                "parkableForFocus": (
                    preflight.get("schema") == QUEUE_PREFLIGHT_SCHEMA
                    and preflight.get("status") == "blocked"
                    and blockers == ["execution_focus_mismatch"]
                    and isinstance(preflight.get("queue_id"), str)
                    and PENDING_EXPERIMENT_ID.fullmatch(preflight["queue_id"])
                    is not None
                    and isinstance(preflight.get("project_id"), str)
                    and PENDING_EXPERIMENT_ID.fullmatch(preflight["project_id"])
                    is not None
                    and preflight.get("project_id") != project["projectId"]
                    and focus["supermegaActive"] is True
                    and preflight.get("submission_allowed") is False
                    and preflight.get("model_execution_ready") is False
                    and isinstance(preflight_effects, dict)
                    and bool(preflight_effects)
                    and all(value is False for value in preflight_effects.values())
                ),
                "parkedCount": company.queue_status_count("parked"),
            }
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            pass

        try:
            evidence = company.product_evidence_status(str(project["projectId"]))
            pending_items = _pending_experiment_items(company_home)
            pending_count = sum(
                1 for item in pending_items
                if isinstance(item.get("project"), dict)
                and item["project"].get("id") == project["projectId"]
            )
            plan = CompanyTools(company_home).product_experiment_next({
                "project": str(project["projectId"]),
            })
            experiment = plan.get("experiment")
            values = (
                evidence.get("reviewed_missions"),
                evidence.get("mission_target"),
                evidence.get("remaining_missions"),
                evidence.get("complete_measurements"),
            )
            milestone_reached = evidence.get("milestone_reached")
            if (
                any(type(value) is not int or value < 0 for value in values)
                or type(milestone_reached) is not bool
                or (not milestone_reached and not isinstance(experiment, dict))
            ):
                raise ValueError("supermega_product_proof_invalid")
            product_proof = {
                "status": "ready",
                "reviewedMissions": values[0], "missionTarget": values[1],
                "remainingMissions": values[2], "completeMeasurements": values[3],
                "pendingReviewCount": pending_count,
                "milestoneReached": milestone_reached,
                "nextCategory": plan.get("selectedCategory"),
                "nextLabel": experiment.get("label") if isinstance(experiment, dict) else None,
            }
        except (
            KeyError, OSError, ProtocolError, RuntimeError, TypeError, ValueError,
        ):
            pass

    try:
        available = available_memory_bytes()
    except (OSError, RuntimeError, ValueError):
        available = None
    memory_ready = available is not None and available >= CYCLE_MINIMUM_AVAILABLE_BYTES
    if repository["status"] != "ready":
        next_action = "restore_or_inspect_supermega_checkout"
        command = "local-ai.cmd supermega status"
    elif project["status"] != "ready":
        next_action = "restore_supermega_company_project"
        command = 'local-ai.cmd new "SuperMega"'
    elif knowledge["readyForUse"] is not True:
        next_action = "refresh_changed_supermega_evidence"
        command = "local-ai.cmd supermega refresh"
    elif focus["supermegaActive"] is not True:
        next_action = "activate_supermega_execution_focus"
        command = "local-ai.cmd supermega use"
    elif queue["status"] == "unavailable":
        next_action = "inspect_unavailable_queue_state"
        command = "local-ai.cmd supermega next"
    elif queue["parkableForFocus"] is True:
        next_action = "park_incompatible_queue_head"
        command = "local-ai.cmd supermega park-next"
    elif queue["status"] in {"blocked", "owner_gate_required"}:
        if queue["projectId"] != project["projectId"]:
            next_action = "inspect_incompatible_queue_head"
        elif queue["status"] == "owner_gate_required":
            next_action = "review_queue_owner_gate"
        else:
            next_action = "inspect_blocked_queue_head"
        command = "local-ai.cmd supermega next"
    elif product_proof["status"] != "ready":
        next_action = "inspect_supermega_product_proof"
        command = "local-ai.cmd company evidence status --project SuperMega"
    elif product_proof["pendingReviewCount"]:
        next_action = "review_pending_supermega_product_proof"
        command = "local-ai.cmd supermega review"
    elif repository["clean"] is not True:
        next_action = "review_existing_supermega_changes"
        command = "git status --short"
    elif (
        queue["status"] == "no_due_mission"
        and product_proof["milestoneReached"] is False
        and not memory_ready
    ):
        next_action = "preview_next_supermega_product_proof"
        command = "local-ai.cmd supermega proof"
    elif (
        queue["status"] == "no_due_mission"
        and product_proof["milestoneReached"] is False
    ):
        next_action = "run_next_supermega_product_proof"
        command = "local-ai.cmd supermega prove"
    elif not memory_ready:
        next_action = "free_memory_or_plan_without_model"
        command = 'local-ai.cmd supermega plan "OBJECTIVE"'
    else:
        next_action = "check_supermega_coding_agent"
        command = "local-ai.cmd supermega code --check"
    attention = (
        repository["status"] != "ready" or project["status"] != "ready"
        or knowledge["readyForUse"] is not True or focus["supermegaActive"] is not True
        or queue["status"] in {"unavailable", "blocked", "owner_gate_required"}
        or product_proof["status"] != "ready"
        or bool(product_proof["pendingReviewCount"])
        or repository["clean"] is not True or not memory_ready
    )
    print(json.dumps({
        "schema": SUPERMEGA_WORKBENCH_SCHEMA,
        "status": "attention" if attention else "ready",
        "repository": repository, "project": project, "knowledge": knowledge,
        "focus": focus, "queue": queue, "productProof": product_proof,
        "resources": {
            "availableMemoryBytes": available,
            "minimumModelMemoryBytes": CYCLE_MINIMUM_AVAILABLE_BYTES,
            "modelMemoryReady": memory_ready,
            "memoryShortfallBytes": (
                None if available is None or memory_ready
                else CYCLE_MINIMUM_AVAILABLE_BYTES - available
            ),
        },
        "nextAction": next_action, "command": command,
        "commands": {
            "status": "local-ai.cmd supermega",
            "refreshEvidence": "local-ai.cmd supermega refresh",
            "activateFocus": "local-ai.cmd supermega use",
            "inspectQueue": "local-ai.cmd supermega next",
            "parkIncompatibleQueueHead": "local-ai.cmd supermega park-next",
            "listParkedQueue": "local-ai.cmd company queue list --status parked",
            "previewProductProof": "local-ai.cmd supermega proof",
            "runProductProof": "local-ai.cmd supermega prove",
            "inspectPendingProof": "local-ai.cmd supermega pending",
            "reviewProductProof": "local-ai.cmd supermega review",
            "validationDossier": "local-ai.cmd supermega dossier",
            "plan": 'local-ai.cmd supermega plan "OBJECTIVE"',
            "queue": 'local-ai.cmd supermega later "OBJECTIVE"',
            "run": 'local-ai.cmd supermega work "OBJECTIVE"',
            "checkCode": "local-ai.cmd supermega code --check",
            "openCode": "local-ai.cmd supermega code",
            "dashboard": "local-ai.cmd supermega dashboard",
        },
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if repository["status"] == "ready" and project["status"] == "ready" else 1


def run_supermega_park_next(
    action: LaunchAction, root: Path | None = None,
) -> int:
    del action
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.core import Company, MockModel, QUEUE_PREFLIGHT_SCHEMA
    from local_company.focus import read_execution_focus

    company_home = default_company_home()
    try:
        company = Company(company_home, MockModel())
        detail = company.project_detail(SUPERMEGA_PROJECT_NAME)
        project_row = detail["project"]
        supermega_project_id = project_row[0]
        focus = read_execution_focus(company_home)
        preflight = company.queue_preflight()
    except (
        IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError,
    ) as error:
        raise ValueError("supermega_queue_state_unavailable") from error
    queue_id = preflight.get("queue_id")
    queue_project_id = preflight.get("project_id")
    blockers = preflight.get("blockers")
    effects = preflight.get("effects")
    safely_parkable = (
        preflight.get("schema") == QUEUE_PREFLIGHT_SCHEMA
        and preflight.get("status") == "blocked"
        and preflight.get("submission_allowed") is False
        and preflight.get("model_execution_ready") is False
        and blockers == ["execution_focus_mismatch"]
        and isinstance(queue_id, str)
        and PENDING_EXPERIMENT_ID.fullmatch(queue_id) is not None
        and isinstance(queue_project_id, str)
        and PENDING_EXPERIMENT_ID.fullmatch(queue_project_id) is not None
        and queue_project_id != supermega_project_id
        and focus.get("enabled") is True
        and focus.get("projectId") == supermega_project_id
        and isinstance(effects, dict)
        and bool(effects)
        and all(value is False for value in effects.values())
    )
    if not safely_parkable:
        raise ValueError("supermega_queue_head_not_safely_parkable")

    reason = (
        "Preserve an incompatible queue head while SuperMega remains the "
        "active execution focus."
    )
    try:
        parked = company.park_queue_item(
            queue_id, reason, source="supermega-workbench",
            require_due_head=True,
        )
    except RuntimeError as error:
        raise ValueError("supermega_queue_changed_before_park") from error
    try:
        next_preflight = company.queue_preflight()
    except (OSError, RuntimeError, TypeError, ValueError):
        next_preflight = {
            "status": "unavailable", "queue_id": None,
            "project_id": None, "blockers": [],
        }
    next_blockers = next_preflight.get("blockers")
    print(json.dumps({
        "schema": SUPERMEGA_QUEUE_PARK_SCHEMA,
        "status": "parked",
        "parkedQueueId": queue_id,
        "parkedProjectId": queue_project_id,
        "restorable": True,
        "restoreCommand": (
            f'local-ai.cmd company queue unpark {queue_id} --reason '
            '"Restore this preserved mission to its original queue position."'
        ),
        "nextQueue": {
            "status": next_preflight.get("status"),
            "queueId": next_preflight.get("queue_id"),
            "projectId": next_preflight.get("project_id"),
            "blockers": (
                list(next_blockers[:10])
                if isinstance(next_blockers, list) else []
            ),
        },
        "effects": parked["effects"],
        "modelCalled": False,
        "stateMutated": True,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


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


def _read_autopilot_status(project_root: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(project_root / "scripts" / "manage_cycle_task.ps1"),
            "-Mode", "Status",
        ],
        cwd=project_root, check=False, capture_output=True, text=True, timeout=30,
    )
    candidate = completed.stdout.strip() or completed.stderr.strip()
    if len(candidate) > 65_536:
        raise ValueError("autopilot_status_receipt_too_large")
    try:
        receipt = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ValueError("autopilot_status_receipt_invalid") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != "local-ai.autonomy-task.v1":
        raise ValueError("autopilot_status_receipt_invalid")
    return receipt


def _brief_next_action(
    autonomy: dict[str, object], queue_status: str, queue_blockers: list[str], pending_count: int,
    offer_status: str, focus_enabled: bool, available_memory: int | None,
) -> tuple[str, str]:
    if autonomy.get("verified") is not True or autonomy.get("status") != "ready":
        return "repair_autopilot", "local-ai.cmd autopilot repair"
    if autonomy.get("currentActivity") in {
        "waiting_for_idle_or_cycle_running", "queued_by_windows",
    }:
        return "wait_for_idle_or_cycle_completion", "local-ai.cmd brief"
    if queue_status == "owner_gate_required":
        return "review_queued_mission_owner_gate", "local-ai.cmd next"
    if queue_status == "blocked":
        if "knowledge_changed" in queue_blockers:
            return "review_changed_project_knowledge", "local-ai.cmd knowledge audit --project PROJECT"
        return "inspect_blocked_queued_mission", "local-ai.cmd next"
    if pending_count:
        return "review_pending_product_experiment", "local-ai.cmd experiment-review-interactive"
    if queue_status == "ready":
        if available_memory is None:
            return "inspect_memory_before_ready_mission", "local-ai.cmd status"
        if available_memory < CYCLE_MINIMUM_AVAILABLE_BYTES:
            return "free_memory_then_run_ready_mission", "local-ai.cmd cycle --recover-memory"
        return "run_ready_mission_now_or_await_autopilot", "local-ai.cmd cycle --recover-memory"
    if offer_status == "ready_for_owner_packaging":
        return "owner_review_sellable_offer", "local-ai.cmd offer"
    if not focus_enabled:
        return "select_active_product_project", "local-ai.cmd projects list"
    return "await_or_run_next_measured_product_experiment", "local-ai.cmd experiment"


def run_company_brief(action: LaunchAction, root: Path | None = None) -> int:
    del action
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError

    home = default_company_home()
    tools = CompanyTools(home)
    company_status = tools.status({})
    autonomy_raw = _read_autopilot_status(project_root)
    pending_count = len(_pending_experiment_items(home))
    focus = company_status["focus"]
    offer_status = "project_focus_required"
    offer_missing: list[str] = []
    if focus.get("enabled") is True and isinstance(focus.get("projectId"), str):
        try:
            offer = tools.product_offer_next({"project": focus["projectId"]})
            offer_status = str(offer["status"])
            missing = offer.get("missingProof")
            offer_missing = list(missing) if isinstance(missing, list) else []
        except ProtocolError:
            offer_status = "unavailable"
    autonomy = {
        "status": autonomy_raw.get("status"),
        "verified": autonomy_raw.get("verified") is True,
        "taskExecutionState": autonomy_raw.get("taskExecutionState"),
        "currentActivity": autonomy_raw.get("currentActivity"),
        "recommendedAction": autonomy_raw.get("recommendedAction"),
        "nextRunTime": autonomy_raw.get("nextRunTime"),
        "lastCycleCurrentForLastRun": autonomy_raw.get("lastCycleCurrentForLastRun"),
    }
    try:
        available_memory = available_memory_bytes()
    except RuntimeError:
        available_memory = None
    queue = company_status["queue"]
    queue_status = str(queue.get("status"))
    queue_blockers = queue.get("blockers")
    bounded_blockers = [
        item for item in queue_blockers
        if isinstance(queue_blockers, list) and isinstance(item, str)
    ] if isinstance(queue_blockers, list) else []
    next_action, command = _brief_next_action(
        autonomy, queue_status, bounded_blockers, pending_count, offer_status,
        focus.get("enabled") is True, available_memory,
    )
    if (
        next_action == "review_changed_project_knowledge"
        and isinstance(focus.get("projectId"), str)
    ):
        command = f'local-ai.cmd knowledge audit --project {focus["projectId"]}'
    print(json.dumps({
        "schema": COMPANY_BRIEF_SCHEMA, "status": "ready",
        "autonomy": autonomy,
        "queue": {
            "status": queue_status, "queueId": queue.get("queueId"),
            "blockers": bounded_blockers,
            "ownerGateCategories": queue.get("ownerGateCategories"),
        },
        "product": {
            "projectId": focus.get("projectId"), "projectName": focus.get("projectName"),
            "pendingExperimentCount": pending_count,
            "offerStatus": offer_status, "offerMissingProof": offer_missing,
        },
        "resources": {
            "availableMemoryBytes": available_memory,
            "minimumExecutionMemoryBytes": CYCLE_MINIMUM_AVAILABLE_BYTES,
            "memoryAdmissionReady": (
                available_memory >= CYCLE_MINIMUM_AVAILABLE_BYTES
                if available_memory is not None else None
            ),
            "memoryShortfallBytes": (
                max(0, CYCLE_MINIMUM_AVAILABLE_BYTES - available_memory)
                if available_memory is not None else None
            ),
        },
        "nextAction": next_action, "command": command,
        "modelCalled": False, "stateMutated": False,
        "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


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
    recovery = None
    if available < CYCLE_MINIMUM_AVAILABLE_BYTES and "--recover-memory" in action.command:
        recovery = _recover_memory(project_root)
        if recovery is None:
            print(json.dumps(_cycle_receipt(
                ok=False, status="error", reason="memory_recovery_invalid",
                schedulesMaterialized=materialized, queueId=queue_id,
                availableMemoryBytes=available,
                minimumAvailableBytes=CYCLE_MINIMUM_AVAILABLE_BYTES,
                memoryShortfallBytes=CYCLE_MINIMUM_AVAILABLE_BYTES - available,
            ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
            return 2
        try:
            available = available_memory_bytes()
        except RuntimeError:
            print(json.dumps(_cycle_receipt(
                ok=True, status="blocked", reason="available_memory_unavailable_after_recovery",
                schedulesMaterialized=materialized, queueId=queue_id,
                minimumAvailableBytes=CYCLE_MINIMUM_AVAILABLE_BYTES,
                memoryRecovery=recovery,
            ), separators=(",", ":"), sort_keys=True))
            return 0
    if available < CYCLE_MINIMUM_AVAILABLE_BYTES:
        print(json.dumps(_cycle_receipt(
            ok=True, status="blocked", reason="insufficient_available_memory",
            schedulesMaterialized=materialized, queueId=queue_id,
            availableMemoryBytes=available,
            minimumAvailableBytes=CYCLE_MINIMUM_AVAILABLE_BYTES,
            memoryShortfallBytes=CYCLE_MINIMUM_AVAILABLE_BYTES - available,
            recommendedAction=(
                "close_large_apps_then_run_cycle"
                if recovery is not None else "run_cycle_with_memory_recovery_or_close_large_apps"
            ),
            **({"memoryRecovery": recovery} if recovery is not None else {}),
        ), separators=(",", ":"), sort_keys=True))
        return 0

    execution_arguments = [item for item in action.command if item != "--recover-memory"]
    executed = _company_process(
        ["queue", "run-next", "--queue-id", queue_id, *execution_arguments],
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
        **({"memoryRecovery": recovery} if recovery is not None else {}),
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
        if action.mode == "supermega-status":
            return run_supermega_status(action)
        if action.mode == "supermega-park-next":
            return run_supermega_park_next(action)
        if action.mode == "supermega-code":
            return run_supermega_code(action)
        if action.mode == "ask":
            return run_ask(action)
        if action.mode in {"code", "vision", "vision-lite"}:
            return run_code(action)
        if action.mode == "autopilot":
            return run_autopilot(action)
        if action.mode == "brief":
            return run_company_brief(action)
        if action.mode == "use":
            return switch_project(action)
        if action.mode == "experiment":
            return run_experiment(action)
        if action.mode == "experiment-run":
            return run_experiment_agent(action)
        if action.mode == "experiment-pending":
            return run_pending_experiments(action)
        if action.mode == "experiment-review":
            return run_experiment_review(action)
        if action.mode == "experiment-review-interactive":
            return run_interactive_experiment_review(action)
        if action.mode == "mission-candidate":
            return run_mission_candidate(action)
        if action.mode == "mission-review":
            return run_mission_review(action)
        if action.mode == "offer":
            return run_offer(action)
        if action.mode == "offer-pack":
            return run_offer_pack(action)
        if action.mode == "validation-pack":
            return run_validation_pack(action)
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
