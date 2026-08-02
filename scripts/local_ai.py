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
PENDING_EXPERIMENT_SCHEMA = "local-ai.pending-product-experiment.v1"
PENDING_EXPERIMENT_ID = re.compile(r"^[0-9a-f]{12}$")
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
  local-ai.cmd experiment [PROJECT]            Show the next measured product test; no model
  local-ai.cmd experiment-run [PROJECT] [--recover-memory]
                                              Run that test locally and return its receipt
  local-ai.cmd offer [PROJECT]                 Check whether evidence supports a sellable kit
  local-ai.cmd experiment-pending              Inspect locally saved experiment receipts
  local-ai.cmd experiment-review ID --decision accepted|rejected --corrections N
      --paid-setup yes|no|unknown --confirm-human-review
                                              Record one actual human review
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


def translate(argv: list[str]) -> LaunchAction | None:
    if not argv or argv[0].lower() in {"help", "-h", "--help"}:
        return None
    name = argv[0].lower()
    tail = argv[1:]
    if name == "check":
        return LaunchAction(("doctor", *tail), "check", "Check the local model dependency without generating text.", False, False)
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
    if name == "experiment-pending":
        if tail:
            raise ValueError("experiment_pending_accepts_no_arguments")
        return LaunchAction((), "experiment-pending", "Inspect pending measured experiment receipts without changing state.", False, False)
    if name == "experiment-review":
        if not tail:
            raise ValueError("experiment_review_arguments_required")
        return LaunchAction(tuple(tail), "experiment-review", "Record one explicitly confirmed human review of a pending measured experiment.", False, True)
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


def run_offer(action: LaunchAction, root: Path | None = None) -> int:
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
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
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if result.get("status") == "ready_for_owner_packaging" else 1


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


def _load_pending_experiment(home: Path, experiment_id: str) -> tuple[Path, dict[str, object]]:
    if PENDING_EXPERIMENT_ID.fullmatch(experiment_id) is None:
        raise ValueError("pending_experiment_id_invalid")
    path = _pending_experiment_directory(home) / f"pending-{experiment_id}.json"
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


def run_pending_experiments(action: LaunchAction, root: Path | None = None) -> int:
    del action
    project_root = root or Path(__file__).resolve(strict=True).parents[1]
    source = str(project_root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from local_company.config import default_company_home

    home = default_company_home()
    directory = _pending_experiment_directory(home)
    items = []
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
            _, payload = _load_pending_experiment(home, match.group(1))
            receipt = payload["runnerReceipt"]
            items.append({
                "experimentId": payload["experimentId"], "project": payload["project"],
                "label": payload["label"], "category": payload["category"],
                "response": receipt.get("response"), "model": receipt.get("model"),
                "wallSeconds": receipt.get("wallSeconds"),
                "peakIncrementalMemoryMb": receipt.get("peakIncrementalMemoryMb"),
            })
    print(json.dumps({
        "schema": "local-ai.pending-product-experiments.v1", "status": "ready",
        "items": items, "count": len(items), "modelCalled": False,
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
        names = {"--decision": "decision", "--corrections": "corrections", "--paid-setup": "paidSetupSignal"}
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
    if set(values) != {"experimentId", "decision", "corrections", "paidSetupSignal", "confirmed"}:
        raise ValueError("experiment_review_arguments_required")
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
    source_path, pending = _load_pending_experiment(home, str(values["experimentId"]))
    project = pending["project"]
    try:
        result = CompanyTools(home).product_experiment_review({
            "experimentId": pending["experimentId"], "project": project["id"],
            "label": pending["label"], "category": pending["category"],
            "decision": values["decision"], "corrections": values["corrections"],
            "paidSetupSignal": values["paidSetupSignal"],
            "receipt": pending["runnerReceipt"],
            "reviewConfirmation": PRODUCT_EXPERIMENT_CONFIRMATION,
        })
    except ProtocolError as error:
        raise ValueError(error.message) from error
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
        "experimentId": pending["experimentId"], "pendingArchived": True,
        "review": result["review"], "modelCalled": False,
        "stateMutated": True, "externalActionPerformed": False,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


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
        if action.mode == "experiment":
            return run_experiment(action)
        if action.mode == "experiment-run":
            return run_experiment_agent(action)
        if action.mode == "experiment-pending":
            return run_pending_experiments(action)
        if action.mode == "experiment-review":
            return run_experiment_review(action)
        if action.mode == "offer":
            return run_offer(action)
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
