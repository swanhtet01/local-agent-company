import json
import hashlib
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable


VISION_SALES_CONTRACT = "local-company.supermega-vision-sales.v1"
VISION_SALES_STATUS_CONTRACT = "local-company.supermega-vision-sales-status.v1"
VISION_SALES_INTAKE_CONTRACT = "local-company.supermega-vision-sales-intake.v1"
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
MAX_STATUS_FILES = 1_000
MAX_STATUS_FILE_BYTES = 256 * 1024
MAX_INTAKE_FILE_BYTES = 64 * 1024
MANUAL_INTAKE_DOMAIN = b"supermega.vision.manual-intake.v1\0"


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


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(character) < 32 and character not in "\t\n\r" for character in normalized):
        raise ValueError(f"{field}_invalid")
    return normalized


def _bounded_number(value: object, field: str, minimum: float, maximum: float, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_invalid")
    if (isinstance(value, float) and not math.isfinite(value)) or value < minimum or value > maximum or (integer and not isinstance(value, int)):
        raise ValueError(f"{field}_invalid")
    return int(value) if integer else value


def create_vision_sales_intake(input_path: Path, sales_root: Path | None = None) -> dict:
    source = input_path.expanduser()
    if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_INTAKE_FILE_BYTES:
        raise ValueError("vision_sales_intake_input_invalid")
    try:
        supplied = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("vision_sales_intake_input_invalid") from error
    allowed = {
        "name", "email", "company", "goal", "platform", "state_count", "weekly_runs",
        "minutes_per_run", "labor_hourly_usd", "screenshot_rights", "human_fallback", "observation_only",
    }
    if not isinstance(supplied, dict) or set(supplied) - allowed:
        raise ValueError("vision_sales_intake_fields_invalid")

    name = _bounded_text(supplied.get("name"), "name", 120)
    email = _bounded_text(supplied.get("email"), "email", 180).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("email_invalid")
    company = _bounded_text(supplied.get("company"), "company", 180)
    goal = _bounded_text(supplied.get("goal"), "goal", 4_000)
    platform = _bounded_text(supplied.get("platform"), "platform", 20).lower()
    if platform not in {"windows", "android", "both"}:
        raise ValueError("platform_invalid")
    state_count = _bounded_number(supplied.get("state_count"), "state_count", 1, 12, integer=True)
    weekly_runs = _bounded_number(supplied.get("weekly_runs"), "weekly_runs", 1, 10_000, integer=True)
    minutes_per_run = _bounded_number(supplied.get("minutes_per_run"), "minutes_per_run", 1, 1_440)
    labor_hourly_usd = _bounded_number(supplied.get("labor_hourly_usd", 0), "labor_hourly_usd", 0, 10_000)
    gates = {}
    for field in ("screenshot_rights", "human_fallback", "observation_only"):
        value = supplied.get(field)
        if type(value) is not bool:
            raise ValueError(f"{field}_invalid")
        gates[field] = value

    normalized = {
        "name": name,
        "email": email,
        "company": company,
        "goal": goal,
        "platform": platform,
        "state_count": state_count,
        "weekly_runs": weekly_runs,
        "minutes_per_run": minutes_per_run,
        "labor_hourly_usd": labor_hourly_usd,
        **gates,
    }
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    lead_id = "LEAD-" + hashlib.sha256(MANUAL_INTAKE_DOMAIN + canonical).hexdigest()[:16].upper()
    event = {
        "event": "supermega.contact.created",
        "record": {
            "lead_id": lead_id,
            "source": "supermega-local-manual-intake",
            "name": name,
            "email": email,
            "company": company,
            "workflow": "vision",
            "requested_package": "vision-founding-pilot",
            "goal": goal,
            "lead_stage": "new",
            "status": "new",
            "owner": "SuperMega",
            "next_step": "Run local Vision qualification and review the generated drafts.",
            "raw": {"vision": {
                "platform": platform,
                "state_count": state_count,
                "weekly_runs": weekly_runs,
                "minutes_per_run": minutes_per_run,
                "labor_hourly_usd": labor_hourly_usd,
                **gates,
            }},
        },
    }
    encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    requested_sales = (sales_root or default_vision_sales_root()).expanduser()
    if requested_sales.exists() and requested_sales.is_symlink():
        raise RuntimeError("vision_sales_root_unsafe")
    sales = requested_sales.resolve()
    inbox = sales / "inbox"
    if inbox.exists() and (not inbox.is_dir() or inbox.is_symlink()):
        raise RuntimeError("vision_sales_inbox_unsafe")
    inbox.mkdir(parents=True, exist_ok=True)
    if inbox.is_symlink():
        raise RuntimeError("vision_sales_inbox_unsafe")
    destination = inbox / f"{lead_id}.json"
    created = False
    try:
        with destination.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        created = True
    except FileExistsError:
        if (
            not destination.is_file()
            or destination.is_symlink()
            or destination.stat().st_size != len(encoded)
            or destination.read_bytes() != encoded
        ):
            raise RuntimeError("vision_sales_intake_conflict")

    return {
        "contract": VISION_SALES_INTAKE_CONTRACT,
        "status": "created" if created else "replayed",
        "lead_id": lead_id,
        "inbox_file": destination.name,
        "event_sha256": hashlib.sha256(encoded).hexdigest(),
        "next_action": "Run `local-company.cmd supermega vision-sales`, then review the proposal and reply draft.",
        "controls": {
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "input_files_modified": 0,
            "local_files_created": 1 if created else 0,
        },
    }


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


def _regular_json_files(directory: Path) -> tuple[list[Path], int]:
    if not directory.exists():
        return [], 0
    if not directory.is_dir() or directory.is_symlink():
        return [], 1
    entries = sorted(directory.iterdir(), key=lambda path: path.name.encode("utf-8"))
    attention = max(0, len(entries) - MAX_STATUS_FILES)
    files = []
    for path in entries[:MAX_STATUS_FILES]:
        if path.suffix.lower() != ".json":
            continue
        if not path.is_file() or path.is_symlink():
            attention += 1
            continue
        files.append(path)
    return files, attention


def vision_sales_status(sales_root: Path | None = None) -> dict:
    sales = (sales_root or default_vision_sales_root()).expanduser().resolve()
    inbox = sales / "inbox"
    outbox = sales / "outbox"
    receipts = outbox / "receipts"
    proposals = outbox / "proposals"
    replies = outbox / "reply-drafts"
    rejections = outbox / "rejections"

    receipt_files, integrity_failures = _regular_json_files(receipts)
    artifact_directories_safe = all(
        not directory.exists() or (directory.is_dir() and not directory.is_symlink())
        for directory in (proposals, replies)
    )
    if not artifact_directories_safe:
        integrity_failures += 1
        receipt_files = []
    known_leads = set()
    qualified = 0
    blocked = 0
    draft_pipeline_value_usd = 0
    for receipt_path in receipt_files:
        try:
            if receipt_path.stat().st_size > MAX_STATUS_FILE_BYTES:
                raise ValueError("receipt_too_large")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            lead_id = receipt.get("lead_id")
            if (
                receipt.get("contract") != "supermega.vision.lead_proposal_receipt.v2"
                or not isinstance(lead_id, str)
                or not re.fullmatch(r"LEAD-[A-F0-9]{16}", lead_id)
                or receipt_path.name != f"{lead_id}.json"
                or receipt.get("proposal_file") != f"{lead_id}.proposal.md"
                or receipt.get("reply_file") != f"{lead_id}.reply.txt"
                or not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("proposal_sha256", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("reply_sha256", "")))
                or type(receipt.get("qualified")) is not bool
                or not isinstance(receipt.get("blockers"), list)
                or any(not isinstance(item, str) for item in receipt["blockers"])
                or type(receipt.get("price_usd")) is not int
                or not 1_500 <= receipt["price_usd"] <= 4_000
            ):
                raise ValueError("receipt_contract_invalid")
            proposal_path = proposals / receipt["proposal_file"]
            reply_path = replies / receipt["reply_file"]
            if (
                not proposal_path.is_file() or proposal_path.is_symlink()
                or not reply_path.is_file() or reply_path.is_symlink()
                or proposal_path.stat().st_size > MAX_STATUS_FILE_BYTES
                or reply_path.stat().st_size > MAX_STATUS_FILE_BYTES
                or hashlib.sha256(proposal_path.read_bytes()).hexdigest() != receipt["proposal_sha256"]
                or hashlib.sha256(reply_path.read_bytes()).hexdigest() != receipt["reply_sha256"]
            ):
                raise ValueError("sales_artifact_integrity_failed")
            known_leads.add(lead_id)
            if receipt["qualified"]:
                qualified += 1
                draft_pipeline_value_usd += receipt["price_usd"]
            else:
                blocked += 1
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            integrity_failures += 1

    inbox_files, input_attention = _regular_json_files(inbox)
    pending = 0
    for input_path in inbox_files:
        try:
            if input_path.stat().st_size > MAX_STATUS_FILE_BYTES:
                raise ValueError("input_too_large")
            event = json.loads(input_path.read_text(encoding="utf-8"))
            if event.get("event") != "supermega.contact.created" or event.get("record", {}).get("workflow") != "vision":
                continue
            lead_id = event["record"].get("lead_id")
            if not isinstance(lead_id, str) or not re.fullmatch(r"LEAD-[A-F0-9]{16}", lead_id):
                raise ValueError("lead_id_invalid")
            if lead_id not in known_leads:
                pending += 1
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            input_attention += 1

    rejection_files, rejection_attention = _regular_json_files(rejections)
    integrity_failures += rejection_attention
    if integrity_failures:
        next_action = "Inspect sales artifact integrity failures before using any draft."
    elif pending:
        next_action = "Run the bounded Vision sales worker for pending contact events."
    elif qualified:
        next_action = "Review qualified proposal and reply drafts; nothing has been sent."
    elif blocked:
        next_action = "Review blocked-lead questions and missing start gates; nothing has been sent."
    else:
        next_action = "No Vision leads are ready; keep the local inbox available for contact events."

    return {
        "contract": VISION_SALES_STATUS_CONTRACT,
        "status": "attention" if integrity_failures or input_attention else "ready",
        "pipeline": {
            "pending_events": pending,
            "qualified_drafts": qualified,
            "blocked_drafts": blocked,
            "rejection_receipts": len(rejection_files),
            "integrity_failures": integrity_failures,
            "input_attention": input_attention,
            "draft_pipeline_value_usd": draft_pipeline_value_usd,
            "value_label": "Draft proposal value only; not booked or collected revenue.",
        },
        "next_action": next_action,
        "controls": {
            "read_only": True,
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "files_modified": 0,
        },
    }
