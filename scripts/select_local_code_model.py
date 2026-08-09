from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_company.model_policy import PREFERRED_LOCAL_MODELS, SUPPORTED_LOCAL_MODELS  # noqa: E402


SCHEMA = "local-ai.code-model-selection.v1"
GIB = 1024**3
DEFAULT_MODELS = PREFERRED_LOCAL_MODELS
SUPPORTED_MODELS = SUPPORTED_LOCAL_MODELS
MINIMUM_AVAILABLE_BYTES = {
    "llama3.2:3b": 4 * GIB,
    # The 1B model occupies about 1.3 GiB on disk. Keep additional headroom for
    # its runtime allocation, OpenCode, the OS, and the owner's active apps.
    "llama3.2:1b": 5 * GIB // 2,
}
MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,79}$")


@dataclass(frozen=True)
class Selection:
    model: str
    reason: str
    available_memory_bytes: int
    minimum_available_bytes: int
    requested_model: str | None


def select_model(
    installed_models: set[str],
    available_memory_bytes: int,
    requested_model: str | None = None,
) -> Selection:
    if type(available_memory_bytes) is not int or available_memory_bytes < 0:
        raise ValueError("available_memory_invalid")
    if requested_model is not None:
        requested_model = requested_model.strip().lower()
        if not MODEL_NAME.fullmatch(requested_model) or requested_model not in SUPPORTED_MODELS:
            raise ValueError("requested_model_unsupported")
        if requested_model not in installed_models:
            raise ValueError("requested_model_not_installed")
        required = MINIMUM_AVAILABLE_BYTES[requested_model]
        if available_memory_bytes < required:
            raise ValueError("requested_model_memory_blocked")
        return Selection(requested_model, "explicit_request_admitted", available_memory_bytes, required, requested_model)
    for model in DEFAULT_MODELS:
        required = MINIMUM_AVAILABLE_BYTES[model]
        if model in installed_models and available_memory_bytes >= required:
            return Selection(model, "default_model_admitted", available_memory_bytes, required, None)
    if any(model in installed_models for model in DEFAULT_MODELS):
        raise ValueError("installed_models_memory_blocked")
    if any(model in installed_models for model in SUPPORTED_MODELS):
        raise ValueError("explicit_model_required")
    raise ValueError("supported_model_not_installed")


def available_memory_bytes() -> int:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise RuntimeError("available_memory_unavailable")
        return int(status.available_physical)
    page_size = os.sysconf("SC_PAGE_SIZE")
    pages = os.sysconf("SC_AVPHYS_PAGES")
    if type(page_size) is not int or type(pages) is not int or page_size <= 0 or pages < 0:
        raise RuntimeError("available_memory_unavailable")
    return page_size * pages


def installed_ollama_models() -> set[str]:
    executable = shutil.which("ollama")
    if not executable:
        raise RuntimeError("ollama_unavailable")
    completed = subprocess.run(
        [executable, "list"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or len(completed.stdout) > 256_000:
        raise RuntimeError("ollama_inventory_unavailable")
    models = set()
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if fields and MODEL_NAME.fullmatch(fields[0].lower()):
            models.add(fields[0].lower())
    return models


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Select a locally installed coding model that fits current memory.")
    result.add_argument("--model-only", action="store_true", help="Print only the admitted model name for the Windows launcher.")
    result.add_argument("--requested-model", default=os.getenv("LOCAL_CODE_MODEL"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    installed: set[str] | None = None
    available: int | None = None
    try:
        installed = installed_ollama_models()
        available = available_memory_bytes()
        selection = select_model(installed, available, args.requested_model)
        if args.model_only:
            print(selection.model)
        else:
            print(json.dumps({
                "schema": SCHEMA,
                "status": "ready",
                "model": selection.model,
                "reason": selection.reason,
                "requestedModel": selection.requested_model,
                "availableMemoryBytes": selection.available_memory_bytes,
                "minimumAvailableBytes": selection.minimum_available_bytes,
                "installedSupportedModels": sorted(installed.intersection(SUPPORTED_MODELS)),
                "controls": {
                    "paidApiRequired": False,
                    "modelLoaded": False,
                    "externalRequestPerformed": False,
                    "memoryBypassAllowed": False,
                },
            }, separators=(",", ":"), sort_keys=True))
        return 0
    except (RuntimeError, ValueError) as error:
        receipt: dict[str, object] = {
            "schema": SCHEMA, "status": "blocked", "reason": str(error),
            "controls": {
                "paidApiRequired": False, "modelLoaded": False,
                "externalRequestPerformed": False, "memoryBypassAllowed": False,
            },
        }
        if installed is not None:
            supported = sorted(installed.intersection(SUPPORTED_MODELS))
            receipt["installedSupportedModels"] = supported
            required = None
            requested = (args.requested_model or "").strip().lower()
            if requested in MINIMUM_AVAILABLE_BYTES and requested in installed:
                required = MINIMUM_AVAILABLE_BYTES[requested]
            elif supported:
                required = min(MINIMUM_AVAILABLE_BYTES[model] for model in supported)
            if required is not None:
                receipt["minimumAvailableBytes"] = required
                if available is not None:
                    receipt["memoryShortfallBytes"] = max(0, required - available)
        if available is not None:
            receipt["availableMemoryBytes"] = available
        if str(error) in {"installed_models_memory_blocked", "requested_model_memory_blocked"}:
            receipt["recommendedAction"] = "close_large_apps_then_rerun_check"
        elif str(error) == "explicit_model_required":
            receipt["recommendedAction"] = "set_local_code_model_explicitly"
        else:
            receipt["recommendedAction"] = "inspect_local_model_installation"
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
