from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SCHEMA = "local-ai.launchpad.v1"


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
  local-ai.cmd plan "OBJECTIVE" [options]     Preview the team and gates; no model
  local-ai.cmd work "OBJECTIVE" [options]     Run one bounded local AI team
  local-ai.cmd later "OBJECTIVE" [options]    Add work to the durable local queue
  local-ai.cmd next [--queue-id ID]           Preview the exact next queued mission
  local-ai.cmd run-next [options]              Run one due queued mission
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
  local-ai.cmd explain COMMAND ...             Show effects without running it
  local-ai.cmd company ...                     Access the complete advanced CLI

Examples:
  local-ai.cmd code C:\path\to\a-project
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
    }
    if name in passthrough:
        mutating = name in {"projects", "knowledge", "approvals", "schedules", "focus", "export"}
        return LaunchAction((passthrough[name], *tail), name, f"Use the {name} capability.", False, mutating)
    if name == "company":
        if not tail:
            raise ValueError("company_command_required")
        model_may_run = tail[0] in {"run", "retry", "resume"} or tail[:2] == ["queue", "run-next"]
        return LaunchAction(tuple(tail), "advanced", "Use the complete Local Agent Company CLI.", model_may_run, True)
    if name == "code":
        return LaunchAction(tuple(tail), "code", "Open OpenCode in one local project through Ollama.", True, False)
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
        if action.mode == "code":
            raise ValueError("code_mode_requires_local_ai_cmd")
        if action.mode == "use":
            return switch_project(action)
        return run_company(action)
    except ValueError as error:
        print(json.dumps({"schema": SCHEMA, "ok": False, "reason": str(error)}, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
