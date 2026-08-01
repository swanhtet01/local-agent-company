from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from scripts.select_local_code_model import (
        available_memory_bytes,
        installed_ollama_models,
        select_model,
    )
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from select_local_code_model import (  # type: ignore[no-redef]
        available_memory_bytes,
        installed_ollama_models,
        select_model,
    )


SCHEMA = "local-ai.coding-run.v1"
DEFAULT_OPENCODE = Path(r"C:\Users\thesw\tools\node-v24.18.0-win-x64\opencode.cmd")
MAX_TASK_BYTES = 32_000
MAX_OUTPUT_CHARS = 64_000
MAX_CHANGED_FILES = 50


def _receipt(**values: object) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "externalActionPerformed": False,
        "paidApiUsed": False,
        "autoPermissionsEnabled": False,
        **values,
    }


def _run(
    command: list[str], root: Path, *, timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, cwd=root, check=False, capture_output=True, text=True,
        timeout=timeout,
    )
    if len(completed.stdout) > MAX_OUTPUT_CHARS or len(completed.stderr) > MAX_OUTPUT_CHARS:
        raise RuntimeError("command_output_limit_exceeded")
    return completed


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


def _git_output(root: Path, *arguments: str) -> str:
    completed = _run(["git", *arguments], root)
    if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT_CHARS:
        raise RuntimeError("git_observation_failed")
    return completed.stdout


def validate_inputs(
    project: Path, task_file: Path, protected: list[Path], test_command: list[str],
) -> tuple[Path, Path, list[Path]]:
    root = project.resolve(strict=True)
    if project.is_symlink() or not root.is_dir():
        raise ValueError("project_invalid")
    git_root = Path(_git_output(root, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    if git_root != root:
        raise ValueError("project_must_be_git_root")
    task_candidate = task_file if task_file.is_absolute() else root / task_file
    if task_candidate.is_symlink():
        raise ValueError("task_file_invalid")
    task = task_candidate.resolve(strict=True)
    if not task.is_file() or not task.is_relative_to(root):
        raise ValueError("task_file_invalid")
    task_bytes = task.read_bytes()
    if not task_bytes or len(task_bytes) > MAX_TASK_BYTES or b"\0" in task_bytes:
        raise ValueError("task_file_invalid")
    try:
        task_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("task_file_invalid") from exc
    if not test_command or len(test_command) > 64:
        raise ValueError("test_command_required")
    if any(not value or len(value) > 1_000 or "\0" in value for value in test_command):
        raise ValueError("test_command_invalid")
    protected_paths = [task]
    for item in protected:
        item_candidate = item if item.is_absolute() else root / item
        if item_candidate.is_symlink():
            raise ValueError("protected_file_invalid")
        candidate = item_candidate.resolve(strict=True)
        if not candidate.is_file() or not candidate.is_relative_to(root):
            raise ValueError("protected_file_invalid")
        if candidate not in protected_paths:
            protected_paths.append(candidate)
    return root, task, protected_paths


def _hash_files(paths: list[Path]) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _changed_files(root: Path) -> list[str]:
    output = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    files: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            raise RuntimeError("git_status_invalid")
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        value = value.strip('"')
        if not value or value.startswith("../") or Path(value).is_absolute():
            raise RuntimeError("git_status_invalid")
        files.append(value)
    if len(files) > MAX_CHANGED_FILES:
        raise RuntimeError("changed_file_limit_exceeded")
    return sorted(set(files))


def _run_agent(
    command: list[str], root: Path, timeout: int, baseline_memory: int,
) -> tuple[int, str, str, int]:
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    stop = threading.Event()
    minimum = [baseline_memory]

    def sample() -> None:
        while not stop.wait(0.25):
            try:
                minimum[0] = min(minimum[0], available_memory_bytes())
            except RuntimeError:
                pass

    sampler = threading.Thread(target=sample, name="local-code-memory-sampler", daemon=True)
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
        stdout, stderr = process.communicate()
        raise RuntimeError("agent_timeout")
    finally:
        stop.set()
        sampler.join(timeout=2)
    if len(stdout) > MAX_OUTPUT_CHARS or len(stderr) > MAX_OUTPUT_CHARS:
        raise RuntimeError("agent_output_limit_exceeded")
    return process.returncode, stdout, stderr, minimum[0]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run one receipt-bound local coding attempt.")
    result.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("project", type=Path)
    result.add_argument("task_file", type=Path)
    result.add_argument("--protect", action="append", default=[], type=Path)
    result.add_argument("--timeout-seconds", type=int, default=300)
    result.add_argument("--requested-model", default=os.getenv("LOCAL_CODE_MODEL"))
    result.add_argument("--opencode", type=Path, default=DEFAULT_OPENCODE)
    result.add_argument("--test", nargs=argparse.REMAINDER, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    model: str | None = None
    ollama = shutil.which("ollama")
    cleanup_done = False
    started = time.perf_counter()
    try:
        if not 30 <= args.timeout_seconds <= 1_800:
            raise ValueError("timeout_invalid")
        root, task, protected = validate_inputs(
            args.project, args.task_file, args.protect, args.test,
        )
        if _changed_files(root):
            raise ValueError("worktree_must_be_clean")
        protected_before = _hash_files(protected)
        opencode = args.opencode.resolve(strict=True)
        if not opencode.is_file() or opencode.suffix.lower() not in {".cmd", ".exe"}:
            raise ValueError("opencode_invalid")
        selection = select_model(
            installed_ollama_models(), available_memory_bytes(), args.requested_model,
        )
        model = selection.model
        prompt = (
            f"Read {task.relative_to(root)} and implement that one task inside this repository. "
            f"Do not modify protected files. Run this exact validation before finishing: "
            f"{' '.join(args.test)}. Do not access the network, share the session, deploy, or "
            "perform external actions."
        )
        command = [
            str(opencode), "run", prompt, "--model", f"ollama/{model}",
            "--dir", str(root), "--format", "default",
        ]
        agent_code, agent_stdout, agent_stderr, minimum_memory = _run_agent(
            command, root, args.timeout_seconds, selection.available_memory_bytes,
        )
        changed_files = _changed_files(root)
        protected_current = protected_before == _hash_files(protected)
        test = _run(args.test, root, timeout=min(args.timeout_seconds, 600))
        tests_passed = test.returncode == 0
        cleanup_done = _unload_model(ollama, model)
        ok = (
            agent_code == 0 and bool(changed_files) and protected_current
            and tests_passed and cleanup_done
        )
        print(json.dumps(_receipt(
            ok=ok,
            status="accepted" if ok else "rejected",
            reason=(
                "accepted" if ok else "no_file_change" if not changed_files else
                "protected_file_changed" if not protected_current else
                "tests_failed" if not tests_passed else
                "model_unload_failed" if not cleanup_done else "agent_failed"
            ),
            model=model,
            agentExitCode=agent_code,
            changedFiles=changed_files,
            protectedFilesCurrent=protected_current,
            testCommand=args.test,
            testExitCode=test.returncode,
            testsPassed=tests_passed,
            modelUnloadedAfterRun=cleanup_done,
            wallSeconds=round(time.perf_counter() - started, 3),
            admissionAvailableBytes=selection.available_memory_bytes,
            minimumAvailableBytesObserved=minimum_memory,
            peakIncrementalMemoryBytes=max(
                0, selection.available_memory_bytes - minimum_memory,
            ),
            agentOutput=agent_stdout[-4_000:],
            agentError=agent_stderr[-4_000:],
            testOutput=(test.stdout + test.stderr)[-4_000:],
        ), separators=(",", ":"), sort_keys=True))
        return 0 if ok else 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps(_receipt(
            ok=False, status="blocked", reason=str(error),
            wallSeconds=round(time.perf_counter() - started, 3),
        ), separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if model and not cleanup_done:
            _unload_model(ollama, model)


if __name__ == "__main__":
    raise SystemExit(main())
