import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable


VISION_SALES_CONTRACT = "local-company.supermega-vision-sales.v1"
WORKER_CONTRACT = "supermega.vision.lead_inbox_run.v1"
ZERO_EFFECTS = {
    "external_requests": 0,
    "messages_sent": 0,
    "payments_accepted": 0,
    "input_files_modified": 0,
}


def default_supermega_platform_root() -> Path:
    configured = os.getenv("SUPERMEGA_PLATFORM_ROOT")
    return Path(configured) if configured else Path.home() / "Projects" / "supermega-platform"


def default_vision_sales_root() -> Path:
    configured = os.getenv("SUPERMEGA_VISION_SALES_ROOT")
    if configured:
        return Path(configured)
    local = os.getenv("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "state"
    return base / "SuperMega" / "vision-sales"


def _validated_worker_result(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("vision_sales_worker_output_ambiguous")
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise RuntimeError("vision_sales_worker_output_invalid") from error
    if not isinstance(result, dict) or result.get("contract") != WORKER_CONTRACT:
        raise RuntimeError("vision_sales_worker_contract_invalid")
    for field in ("processed", "replayed", "rejected", "ignored"):
        value = result.get(field)
        if type(value) is not int or value < 0 or value > 100:
            raise RuntimeError("vision_sales_worker_count_invalid")
    if result.get("effects") != ZERO_EFFECTS:
        raise RuntimeError("vision_sales_worker_effects_invalid")
    return result


def run_vision_sales(
    platform_root: Path | None = None,
    sales_root: Path | None = None,
    *,
    node_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    platform = (platform_root or default_supermega_platform_root()).expanduser().resolve()
    sales = (sales_root or default_vision_sales_root()).expanduser().resolve()
    worker = (platform / "tools" / "process_vision_lead_inbox.mjs").resolve()
    try:
        worker.relative_to(platform)
    except ValueError as error:
        raise RuntimeError("vision_sales_worker_path_unsafe") from error
    if not worker.is_file() or worker.is_symlink():
        raise RuntimeError("vision_sales_worker_missing_or_unsafe")

    node = node_executable or shutil.which("node")
    if not node or not Path(node).is_file():
        raise RuntimeError("node_runtime_unavailable")

    inbox = sales / "inbox"
    outbox = sales / "outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    completed = runner(
        [str(Path(node).resolve()), str(worker), "--inbox", str(inbox), "--outbox", str(outbox)],
        cwd=platform,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("vision_sales_worker_failed")
    worker_result = _validated_worker_result(completed.stdout)
    return {
        "contract": VISION_SALES_CONTRACT,
        "status": "ready",
        "worker": worker_result,
        "workspace": {
            "inbox": "inbox",
            "outbox": "outbox",
            "proposals": "outbox/proposals",
            "reply_drafts": "outbox/reply-drafts",
            "receipts": "outbox/receipts",
            "rejections": "outbox/rejections",
        },
        "controls": {
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "input_mutations": 0,
            "serial_execution": True,
        },
    }
