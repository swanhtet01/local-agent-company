import json
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable


VISION_SALES_CONTRACT = "local-company.supermega-vision-sales.v1"
WORKER_CONTRACT = "supermega.vision.lead_inbox_run.v1"
VISION_SALES_BUNDLE_DOMAIN = b"supermega.vision-sales-worker.v1\0"
VISION_SALES_BUNDLE_PATHS = (
    "tools/create_vision_pilot_proposal.mjs",
    "tools/process_vision_lead_inbox.mjs",
)
VISION_SALES_BUNDLE_SHA256 = "e46bda95703b7255200eefb3719465a5878e942ed13d4841b6eab3389d9d0252"
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


def _vision_sales_bundle_digest(platform: Path) -> str:
    digest = hashlib.sha256()
    digest.update(VISION_SALES_BUNDLE_DOMAIN)
    for relative in VISION_SALES_BUNDLE_PATHS:
        path = (platform / relative).resolve()
        try:
            path.relative_to(platform)
        except ValueError as error:
            raise RuntimeError("vision_sales_bundle_path_unsafe") from error
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("vision_sales_bundle_file_missing_or_unsafe")
        relative_bytes = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def run_vision_sales(
    platform_root: Path | None = None,
    sales_root: Path | None = None,
    *,
    node_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    expected_bundle_sha256: str = VISION_SALES_BUNDLE_SHA256,
) -> dict:
    platform = (platform_root or default_supermega_platform_root()).expanduser().resolve()
    sales = (sales_root or default_vision_sales_root()).expanduser().resolve()
    worker = (platform / VISION_SALES_BUNDLE_PATHS[-1]).resolve()
    bundle_before = _vision_sales_bundle_digest(platform)
    if bundle_before != expected_bundle_sha256:
        raise RuntimeError("vision_sales_bundle_digest_mismatch")

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
    bundle_after = _vision_sales_bundle_digest(platform)
    if bundle_after != bundle_before:
        raise RuntimeError("vision_sales_bundle_changed_during_run")
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
        "integrity": {
            "worker_bundle_sha256": bundle_before,
            "pinned": True,
            "stable_during_run": True,
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
