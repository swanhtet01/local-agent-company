from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

try:
    from scripts.run_scheduled_cycle import _recover_memory
    from scripts.select_local_code_model import (
        available_memory_bytes,
        installed_ollama_models,
    )
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    from run_scheduled_cycle import _recover_memory  # type: ignore[no-redef]
    from select_local_code_model import (  # type: ignore[no-redef]
        available_memory_bytes,
        installed_ollama_models,
    )


SCHEMA = "local-ai.grounded-assistant-result.v1"
MODEL = "llama3.2:1b"
MIB = 1024**2
MINIMUM_AVAILABLE_BYTES = 960 * MIB
MAX_QUESTION_CHARS = 2_000
MAX_CONTEXT_CHARS = 24_000
MAX_RESPONSE_BYTES = 64_000
MAX_ANSWER_CHARS = 6_000
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _receipt(**values: object) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "model": MODEL,
        "paidApiUsed": False,
        "externalActionPerformed": False,
        "actionExecuted": False,
        "answerTrust": "model_summary_of_verified_local_context",
        "controls": {
            "endpoint": OLLAMA_CHAT_URL,
            "loopbackOnly": True,
            "modelMayChooseActions": False,
            "modelMayExecuteActions": False,
            "memoryBypassAllowed": False,
        },
        **values,
    }


def _emit(receipt: dict[str, object], *, plain: bool, error: bool = False) -> None:
    target = sys.stderr if error else sys.stdout
    if not plain:
        print(json.dumps(
            receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ), file=target)
        return
    if receipt.get("ok") is not True:
        print(
            f"Local assistant {receipt.get('status', 'failed')}: "
            f"{receipt.get('reason', 'unknown_error')}",
            file=target,
        )
        if receipt.get("verifiedCommand"):
            print(f"Verified command: {receipt['verifiedCommand']}", file=target)
        return
    print(str(receipt["answer"]), file=target)
    print(file=target)
    if receipt.get("verifiedNextAction"):
        print(f"Verified next action: {receipt['verifiedNextAction']}", file=target)
    if receipt.get("verifiedCommand"):
        print(f"Verified command: {receipt['verifiedCommand']}", file=target)
    sources = receipt.get("usedSources")
    if isinstance(sources, list):
        print(f"Verified sources: {', '.join(str(item) for item in sources)}", file=target)
    print(f"Context SHA-256: {receipt['contextSha256']}", file=target)
    print(
        f"Local proof: {receipt['model']} | $0 paid API | "
        f"{receipt['peakIncrementalMemoryMb']} MiB peak | model unloaded",
        file=target,
    )
    if receipt.get("deterministicFallbackUsed") is True:
        print("Draft mode: deterministic safety fallback", file=target)


def _capture_json(function: Any, *arguments: object) -> dict[str, Any]:
    output = io.StringIO()
    with redirect_stdout(output):
        code = function(*arguments)
    raw = output.getvalue()
    if code != 0 or len(raw) > MAX_CONTEXT_CHARS:
        raise RuntimeError("verified_context_unavailable")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("verified_context_invalid") from error
    if (
        not isinstance(value, dict)
        or value.get("modelCalled") is not False
        or value.get("stateMutated") is not False
        or value.get("externalActionPerformed") is not False
    ):
        raise RuntimeError("verified_context_effects_invalid")
    return value


def collect_context(root: Path, scope: str) -> dict[str, Any]:
    source = str(root / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from local_company.config import default_company_home
    from local_company.mcp_server import CompanyTools, ProtocolError
    from scripts.local_ai import (
        LaunchAction,
        run_company_brief,
        run_supermega_status,
    )

    if scope == "supermega":
        launchpad = _capture_json(
            run_supermega_status,
            LaunchAction((), "supermega-status", "Read SuperMega status.", False, False),
            root,
        )
        project_name: str | None = "SuperMega"
        launchpad_source = "supermega_status"
    else:
        launchpad = _capture_json(
            run_company_brief,
            LaunchAction((), "brief", "Read company brief.", False, False),
            root,
        )
        product = launchpad.get("product")
        observed = product.get("projectName") if isinstance(product, dict) else None
        project_name = observed if isinstance(observed, str) and observed else None
        launchpad_source = "company_brief"

    tools = CompanyTools(default_company_home())
    projects = tools.projects({})
    playbooks = tools.playbooks({})
    overview: dict[str, Any] | None = None
    if project_name:
        try:
            overview = tools.project_overview({"project": project_name})
        except ProtocolError:
            overview = None

    context: dict[str, Any] = {
        "scope": scope,
        "sources": {
            launchpad_source: launchpad,
            "projects": projects,
            "playbooks": playbooks,
        },
    }
    if overview is not None:
        context["sources"]["project_overview"] = overview
    encoded = json.dumps(
        context, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    if len(encoded) > MAX_CONTEXT_CHARS:
        raise RuntimeError("verified_context_limit_exceeded")
    return context


def _loaded_models(ollama: str) -> set[str]:
    completed = subprocess.run(
        [ollama, "ps"], check=False, capture_output=True, text=True, timeout=15,
    )
    if completed.returncode != 0 or len(completed.stdout) > 256_000:
        raise RuntimeError("ollama_loaded_models_unavailable")
    models: set[str] = set()
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if fields:
            models.add(fields[0].lower())
    return models


def _unload_model(ollama: str, model: str) -> bool:
    try:
        completed = subprocess.run(
            [ollama, "stop", model], check=False, capture_output=True,
            text=True, timeout=30,
        )
        if completed.returncode != 0:
            return False
        return model not in _loaded_models(ollama)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def _parse_structured_answer(raw: str, allowed_sources: set[str]) -> dict[str, Any]:
    value = raw.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("assistant_response_invalid") from error
    if not isinstance(payload, dict) or set(payload) != {
        "answer", "usedSources", "limitations",
    }:
        raise ValueError("assistant_response_invalid")
    answer = payload["answer"]
    used_sources = payload["usedSources"]
    limitations = payload["limitations"]
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or len(answer) > MAX_ANSWER_CHARS
        or not isinstance(used_sources, list)
        or len(used_sources) > len(allowed_sources)
        or any(
            not isinstance(item, str) or item not in allowed_sources
            for item in used_sources
        )
        or len(set(used_sources)) != len(used_sources)
        or not isinstance(limitations, list)
        or len(limitations) > 3
        or any(not isinstance(item, str) or len(item) > 500 for item in limitations)
    ):
        raise ValueError("assistant_response_invalid")
    return {
        "answer": answer.strip(),
        "usedSources": used_sources,
        "limitations": limitations,
    }


def _chat(model: str, question: str, context: dict[str, Any]) -> dict[str, Any]:
    context_text = json.dumps(
        context, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    source_names = sorted(context["sources"])
    format_schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "usedSources": {
                "type": "array", "items": {"type": "string", "enum": source_names},
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["answer", "usedSources", "limitations"],
        "additionalProperties": False,
    }
    body = json.dumps({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a small, private, read-only local assistant. Answer only from "
                    "VERIFIED_CONTEXT. If the answer is absent, say it is not in the verified "
                    "local context. Never claim you executed, deployed, sent, sold, changed, or "
                    "approved anything. Suggest only an exact command already present in context. "
                    "Keep the answer concise and put source keys in usedSources."
                ),
            },
            {
                "role": "user",
                "content": f"VERIFIED_CONTEXT:\n{context_text}\n\nQUESTION:\n{question}",
            },
        ],
        "format": format_schema,
        "stream": False,
        "keep_alive": 0,
        "options": {
            "num_ctx": 3072, "num_predict": 256, "temperature": 0,
        },
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_CHAT_URL, data=body, headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=180) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("assistant_http_response_limit_exceeded")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("assistant_http_response_invalid") from error
    message = payload.get("message") if isinstance(payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("assistant_response_missing")
    try:
        answer = _parse_structured_answer(content, set(source_names))
        answer["modelDraftAccepted"] = True
        answer["deterministicFallbackUsed"] = False
        return answer
    except ValueError as error:
        next_action, command = _verified_next(context, str(context.get("scope")))
        source = (
            "supermega_status"
            if context.get("scope") == "supermega" else "company_brief"
        )
        if next_action and command and source in source_names:
            fallback = (
                f"The verified local next action is {next_action}. "
                f"Use this exact command: {command}."
            )
            used_sources = [source]
        else:
            fallback = (
                "The small-model draft could not be validated, and the verified "
                "local context does not contain a bounded next action for this question."
            )
            used_sources = []
        return {
            "answer": fallback,
            "usedSources": used_sources,
            "limitations": [
                "The model draft failed structured validation; unvalidated prose was withheld."
            ],
            "modelDraftAccepted": False,
            "deterministicFallbackUsed": True,
            "fallbackReason": str(error),
        }


def _verified_next(context: dict[str, Any], scope: str) -> tuple[str | None, str | None]:
    sources = context.get("sources")
    key = "supermega_status" if scope == "supermega" else "company_brief"
    source = sources.get(key) if isinstance(sources, dict) else None
    action = source.get("nextAction") if isinstance(source, dict) else None
    command = source.get("command") if isinstance(source, dict) else None

    def bounded(value: object) -> str | None:
        if (
            isinstance(value, str) and value and len(value) <= 500
            and "\0" not in value and "\r" not in value and "\n" not in value
        ):
            return value
        return None

    return bounded(action), bounded(command)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Ask one read-only question grounded in verified local company context.",
    )
    result.add_argument("--scope", choices=("company", "supermega"), default="company")
    result.add_argument("--question", required=True)
    result.add_argument("--no-recover-memory", action="store_true")
    result.add_argument("--plain", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = time.perf_counter()
    root = Path(__file__).resolve(strict=True).parents[1]
    model_called = False
    model_unloaded = False
    recovery: dict[str, object] | None = None
    admission_memory: int | None = None
    minimum_memory: list[int] | None = None
    ollama = shutil.which("ollama")
    context_sha256: str | None = None
    verified_next_action: str | None = None
    verified_command: str | None = None
    try:
        question = " ".join(args.question.split())
        if not question or len(question) > MAX_QUESTION_CHARS or "\0" in args.question:
            raise ValueError("question_invalid")
        if not ollama:
            raise RuntimeError("ollama_unavailable")
        installed = installed_ollama_models()
        if MODEL not in installed:
            raise RuntimeError("grounded_assistant_model_not_installed")
        loaded = _loaded_models(ollama)
        if loaded:
            raise RuntimeError("another_local_model_is_loaded")

        context = collect_context(root, args.scope)
        context_text = json.dumps(
            context, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        context_sha256 = hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        verified_next_action, verified_command = _verified_next(context, args.scope)

        admission_memory = available_memory_bytes()
        if admission_memory < MINIMUM_AVAILABLE_BYTES and not args.no_recover_memory:
            recovery = _recover_memory(root)
            if recovery is None:
                raise RuntimeError("memory_recovery_invalid")
            admission_memory = available_memory_bytes()
        if admission_memory < MINIMUM_AVAILABLE_BYTES:
            raise RuntimeError("grounded_assistant_memory_blocked")

        minimum_memory = [admission_memory]
        stop = threading.Event()

        def sample() -> None:
            while not stop.wait(0.1):
                try:
                    minimum_memory[0] = min(
                        minimum_memory[0], available_memory_bytes(),
                    )
                except RuntimeError:
                    pass

        sampler = threading.Thread(
            target=sample, name="grounded-assistant-memory-sampler", daemon=True,
        )
        sampler.start()
        try:
            model_called = True
            answer = _chat(MODEL, question, context)
        finally:
            stop.set()
            sampler.join(timeout=2)
            model_unloaded = _unload_model(ollama, MODEL)

        if not model_unloaded:
            raise RuntimeError("model_unload_failed")
        observed_minimum = minimum_memory[0]
        peak = max(0, admission_memory - observed_minimum)
        model_draft_accepted = answer.get("modelDraftAccepted", True) is True
        deterministic_fallback = answer.get("deterministicFallbackUsed", False) is True
        receipt = _receipt(
            ok=True, status="accepted",
            reason=(
                "accepted" if model_draft_accepted
                else "accepted_deterministic_fallback"
            ),
            answerTrust=(
                "model_summary_of_verified_local_context"
                if model_draft_accepted
                else "deterministic_verified_context_fallback"
            ),
            scope=args.scope,
            question=question, answer=answer["answer"],
            usedSources=answer["usedSources"], limitations=answer["limitations"],
            modelDraftAccepted=model_draft_accepted,
            deterministicFallbackUsed=deterministic_fallback,
            **({"fallbackReason": answer["fallbackReason"]}
               if deterministic_fallback and "fallbackReason" in answer else {}),
            contextSha256=context_sha256, contextSourceCount=len(context["sources"]),
            verifiedNextAction=verified_next_action,
            verifiedCommand=verified_command,
            modelCalled=True, modelUnloadedAfterRun=True,
            admissionAvailableBytes=admission_memory,
            minimumAvailableBytes=MINIMUM_AVAILABLE_BYTES,
            minimumAvailableBytesObserved=observed_minimum,
            peakIncrementalMemoryBytes=peak,
            peakIncrementalMemoryMb=round(peak / MIB, 1),
            wallSeconds=round(time.perf_counter() - started, 3),
            **({"memoryRecovery": recovery} if recovery is not None else {}),
        )
        _emit(receipt, plain=args.plain)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError,
            urllib.error.URLError) as error:
        if model_called and not model_unloaded and ollama:
            model_unloaded = _unload_model(ollama, MODEL)
        fields: dict[str, object] = {}
        if admission_memory is not None:
            fields["admissionAvailableBytes"] = admission_memory
            fields["minimumAvailableBytes"] = MINIMUM_AVAILABLE_BYTES
            fields["memoryShortfallBytes"] = max(
                0, MINIMUM_AVAILABLE_BYTES - admission_memory,
            )
        if minimum_memory is not None:
            fields["minimumAvailableBytesObserved"] = minimum_memory[0]
        if context_sha256 is not None:
            fields["contextSha256"] = context_sha256
        if verified_next_action is not None:
            fields["verifiedNextAction"] = verified_next_action
        if verified_command is not None:
            fields["verifiedCommand"] = verified_command
        if recovery is not None:
            fields["memoryRecovery"] = recovery
        receipt = _receipt(
            ok=False, status="blocked" if not model_called else "rejected",
            reason=str(error), scope=args.scope, modelCalled=model_called,
            modelUnloadedAfterRun=model_unloaded if model_called else None,
            wallSeconds=round(time.perf_counter() - started, 3), **fields,
        )
        _emit(receipt, plain=args.plain, error=True)
        return 2 if not model_called else 1


if __name__ == "__main__":
    raise SystemExit(main())
