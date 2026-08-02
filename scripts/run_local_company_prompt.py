from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from scripts.select_local_code_model import (
        MINIMUM_AVAILABLE_BYTES,
        available_memory_bytes,
        installed_ollama_models,
        select_model,
    )
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from select_local_code_model import (  # type: ignore[no-redef]
        MINIMUM_AVAILABLE_BYTES,
        available_memory_bytes,
        installed_ollama_models,
        select_model,
    )


SCHEMA = "local-ai.company-prompt-result.v1"
MAX_PROMPT_CHARS = 4_000
MAX_OUTPUT_CHARS = 131_072
EXPECTED_TOOL = "local_company_company"


def default_opencode() -> Path:
    configured = os.getenv("LOCAL_OPENCODE")
    if configured:
        return Path(configured)
    discovered = shutil.which("opencode.cmd") or shutil.which("opencode")
    if discovered:
        return Path(discovered)
    return Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd"


def _receipt(**values: object) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "externalActionPerformed": False,
        "paidApiUsed": False,
        "autoPermissionsEnabled": False,
        **values,
    }


def _unload_model(ollama: str | None, model: str) -> bool:
    if not ollama:
        return False
    try:
        completed = subprocess.run(
            [ollama, "stop", model], check=False, capture_output=True,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _invoke_agent(
    opencode: Path, model: str, prompt: str, root: Path, timeout: int,
    baseline_memory: int,
) -> tuple[subprocess.CompletedProcess[str], int]:
    command = [
        str(opencode), "run", "--agent", "local-company", "--model",
        f"ollama/{model}", "--format", "json", prompt,
    ]
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stop = threading.Event()
    minimum = [baseline_memory]

    def sample() -> None:
        while not stop.wait(0.25):
            try:
                minimum[0] = min(minimum[0], available_memory_bytes())
            except RuntimeError:
                pass

    sampler = threading.Thread(
        target=sample, name="local-company-memory-sampler", daemon=True,
    )
    sampler.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False, capture_output=True, text=True, timeout=30,
            )
        else:
            process.kill()
        process.communicate()
        raise
    finally:
        stop.set()
        sampler.join(timeout=2)
    return subprocess.CompletedProcess(
        command, process.returncode, stdout, stderr,
    ), minimum[0]


def parse_events(output: str) -> dict[str, Any]:
    if len(output) > MAX_OUTPUT_CHARS:
        raise ValueError("agent_output_limit_exceeded")
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("agent_event_invalid") from error
        if not isinstance(value, dict):
            raise ValueError("agent_event_invalid")
        events.append(value)
    actions: list[str] = []
    responses: list[str] = []
    cost = 0.0
    external_action = False
    tool_error = False
    for event in events:
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        if event.get("type") == "tool_use":
            if part.get("tool") != EXPECTED_TOOL:
                tool_error = True
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") != "completed":
                tool_error = True
                continue
            inputs = state.get("input")
            action = inputs.get("action") if isinstance(inputs, dict) else None
            if not isinstance(action, str) or not action:
                tool_error = True
                continue
            actions.append(action)
            raw_result = state.get("output")
            if isinstance(raw_result, str):
                try:
                    result = json.loads(raw_result)
                except json.JSONDecodeError:
                    tool_error = True
                else:
                    if isinstance(result, dict) and result.get("externalActionPerformed") is True:
                        external_action = True
        elif event.get("type") == "text" and isinstance(part.get("text"), str):
            responses.append(part["text"])
        elif event.get("type") == "step_finish":
            observed_cost = part.get("cost")
            if isinstance(observed_cost, (int, float)) and not isinstance(observed_cost, bool):
                cost += float(observed_cost)
    return {
        "actions": actions,
        "response": "\n".join(responses)[-8_000:],
        "cost": cost,
        "externalActionPerformed": external_action,
        "toolError": tool_error,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run one governed local-company prompt.")
    result.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("prompt", nargs="+")
    result.add_argument("--timeout-seconds", type=int, default=600)
    result.add_argument("--requested-model", default=os.getenv("LOCAL_CODE_MODEL"))
    result.add_argument("--opencode", type=Path, default=default_opencode())
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = time.perf_counter()
    model: str | None = None
    admission_memory: int | None = None
    required_memory: int | None = None
    cleanup_done = False
    ollama = shutil.which("ollama")
    try:
        prompt = " ".join(args.prompt).strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS or "\0" in prompt:
            raise ValueError("prompt_invalid")
        if not 30 <= args.timeout_seconds <= 1_800:
            raise ValueError("timeout_invalid")
        opencode = args.opencode.resolve(strict=True)
        if not opencode.is_file() or opencode.suffix.lower() not in {".cmd", ".exe"}:
            raise ValueError("opencode_invalid")
        installed = installed_ollama_models()
        admission_memory = available_memory_bytes()
        supported_installed = [
            name for name in installed if name in MINIMUM_AVAILABLE_BYTES
        ]
        if args.requested_model in supported_installed:
            required_memory = MINIMUM_AVAILABLE_BYTES[args.requested_model]
        elif supported_installed:
            required_memory = min(MINIMUM_AVAILABLE_BYTES[name] for name in supported_installed)
        selection = select_model(installed, admission_memory, args.requested_model)
        model = selection.model
        completed, minimum_memory = _invoke_agent(
            opencode, model, prompt, Path(__file__).resolve(strict=True).parents[1],
            args.timeout_seconds, selection.available_memory_bytes,
        )
        if len(completed.stderr) > MAX_OUTPUT_CHARS:
            raise ValueError("agent_output_limit_exceeded")
        evidence = parse_events(completed.stdout)
        cleanup_done = _unload_model(ollama, model)
        ok = (
            completed.returncode == 0
            and bool(evidence["actions"])
            and bool(evidence["response"])
            and evidence["cost"] == 0
            and evidence["externalActionPerformed"] is False
            and evidence["toolError"] is False
            and cleanup_done
        )
        reason = (
            "accepted" if ok else
            "agent_failed" if completed.returncode != 0 else
            "company_tool_not_used" if not evidence["actions"] else
            "response_missing" if not evidence["response"] else
            "paid_cost_observed" if evidence["cost"] != 0 else
            "external_action_observed" if evidence["externalActionPerformed"] else
            "tool_evidence_invalid" if evidence["toolError"] else
            "model_unload_failed"
        )
        print(json.dumps(_receipt(
            ok=ok, status="accepted" if ok else "rejected", reason=reason,
            model=model, modelCalled=True, toolActions=evidence["actions"],
            toolCallCount=len(evidence["actions"]), response=evidence["response"],
            observedCost=evidence["cost"],
            externalActionPerformed=evidence["externalActionPerformed"],
            modelUnloadedAfterRun=cleanup_done,
            agentExitCode=completed.returncode,
            wallSeconds=round(time.perf_counter() - started, 3),
            admissionAvailableBytes=selection.available_memory_bytes,
            minimumAvailableBytesObserved=minimum_memory,
            peakIncrementalMemoryBytes=max(
                0, selection.available_memory_bytes - minimum_memory,
            ),
            peakIncrementalMemoryMb=round(
                max(0, selection.available_memory_bytes - minimum_memory) / (1024 * 1024),
                1,
            ),
        ), separators=(",", ":"), sort_keys=True))
        return 0 if ok else 1
    except subprocess.TimeoutExpired:
        print(json.dumps(_receipt(
            ok=False, status="blocked", reason="agent_timeout", modelCalled=model is not None,
            wallSeconds=round(time.perf_counter() - started, 3),
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        memory_fields: dict[str, object] = {}
        if admission_memory is not None:
            memory_fields["admissionAvailableBytes"] = admission_memory
        if required_memory is not None:
            memory_fields["minimumAvailableBytes"] = required_memory
            if admission_memory is not None:
                memory_fields["memoryShortfallBytes"] = max(
                    0, required_memory - admission_memory,
                )
        print(json.dumps(_receipt(
            ok=False, status="blocked", reason=str(error), modelCalled=model is not None,
            wallSeconds=round(time.perf_counter() - started, 3),
            **memory_fields,
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if model and not cleanup_done:
            _unload_model(ollama, model)


if __name__ == "__main__":
    raise SystemExit(main())
