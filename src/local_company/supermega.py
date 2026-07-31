import csv
import json
import hashlib
import io
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
VISION_PROSPECT_IMPORT_CONTRACT = "local-company.supermega-vision-prospect-import.v1"
VISION_PROSPECT_DRAFTS_CONTRACT = "local-company.supermega-vision-prospect-drafts.v1"
PROSPECT_RESEARCH_CONTRACT = "supermega.vision.prospect_research.v1"
PROSPECT_OUTREACH_RECEIPT_CONTRACT = "supermega.vision.prospect_outreach_receipt.v1"
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
PROSPECT_RESEARCH_DOMAIN = b"supermega.vision.prospect-research.v1\0"
MAX_PROSPECT_ROWS = 100
PROSPECT_FIELDS = (
    "rank", "organization", "route_type", "fit_score_10", "verified_public_signal",
    "public_contact", "source", "status",
)


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


def create_vision_sales_intake_fields(supplied: dict, sales_root: Path | None = None) -> dict:
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


def create_vision_sales_intake(input_path: Path, sales_root: Path | None = None) -> dict:
    source = input_path.expanduser()
    if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_INTAKE_FILE_BYTES:
        raise ValueError("vision_sales_intake_input_invalid")
    try:
        supplied = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("vision_sales_intake_input_invalid") from error
    return create_vision_sales_intake_fields(supplied, sales_root)


def _normalized_prospect_row(supplied: dict) -> dict:
    if not isinstance(supplied, dict) or set(supplied) != set(PROSPECT_FIELDS):
        raise ValueError("vision_prospect_fields_invalid")

    def whole_number(field: str, minimum: int, maximum: int) -> int:
        value = supplied.get(field)
        if isinstance(value, bool):
            raise ValueError(f"{field}_invalid")
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"{field}_invalid") from error
        if parsed < minimum or parsed > maximum or (isinstance(value, str) and value.strip() != str(parsed)):
            raise ValueError(f"{field}_invalid")
        return parsed

    rank = whole_number("rank", 1, MAX_PROSPECT_ROWS)
    score = whole_number("fit_score_10", 1, 10)
    organization = _bounded_text(supplied.get("organization"), "organization", 180)
    route_type = _bounded_text(supplied.get("route_type"), "route_type", 120)
    signal = _bounded_text(supplied.get("verified_public_signal"), "verified_public_signal", 1_000)
    contact = _bounded_text(supplied.get("public_contact"), "public_contact", 500)
    source = _bounded_text(supplied.get("source"), "source", 700)
    if not re.fullmatch(r"https://[^\s]+", source):
        raise ValueError("source_must_be_https")
    if supplied.get("status") != "researched_unsent_unqualified":
        raise ValueError("vision_prospect_status_invalid")
    return {
        "rank": rank,
        "organization": organization,
        "route_type": route_type,
        "fit_score_10": score,
        "verified_public_signal": signal,
        "public_contact": contact,
        "source": source,
        "status": "researched_unsent_unqualified",
    }


def _prospect_research_artifact(record: dict) -> tuple[str, bytes]:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    prospect_id = "PROSPECT-" + hashlib.sha256(PROSPECT_RESEARCH_DOMAIN + canonical).hexdigest()[:16].upper()
    artifact = {
        "contract": PROSPECT_RESEARCH_CONTRACT,
        "prospect_id": prospect_id,
        "record_sha256": hashlib.sha256(canonical).hexdigest(),
        "record": record,
    }
    encoded = (json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return prospect_id, encoded


def import_vision_prospects(input_path: Path, sales_root: Path | None = None) -> dict:
    source_path = input_path.expanduser()
    if not source_path.is_file() or source_path.is_symlink() or source_path.stat().st_size > MAX_STATUS_FILE_BYTES:
        raise ValueError("vision_prospect_input_invalid")
    try:
        raw = source_path.read_bytes()
        if len(raw) > MAX_STATUS_FILE_BYTES:
            raise ValueError("vision_prospect_input_invalid")
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if tuple(reader.fieldnames or ()) != PROSPECT_FIELDS:
            raise ValueError("vision_prospect_headers_invalid")
        supplied_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        raise ValueError("vision_prospect_input_invalid") from error
    if not 1 <= len(supplied_rows) <= MAX_PROSPECT_ROWS:
        raise ValueError("vision_prospect_row_count_invalid")

    records = [_normalized_prospect_row(row) for row in supplied_rows]
    ranks = [record["rank"] for record in records]
    organizations = [record["organization"].casefold() for record in records]
    if len(set(ranks)) != len(ranks) or len(set(organizations)) != len(organizations):
        raise ValueError("vision_prospect_duplicates_invalid")
    artifacts = [_prospect_research_artifact(record) for record in records]
    if len({prospect_id for prospect_id, _ in artifacts}) != len(artifacts):
        raise ValueError("vision_prospect_duplicates_invalid")

    requested_sales = (sales_root or default_vision_sales_root()).expanduser()
    if requested_sales.exists() and requested_sales.is_symlink():
        raise RuntimeError("vision_sales_root_unsafe")
    prospects = requested_sales.resolve() / "research" / "prospects"
    for directory in (prospects.parent, prospects):
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise RuntimeError("vision_prospect_store_unsafe")
    prospects.mkdir(parents=True, exist_ok=True)
    if prospects.is_symlink():
        raise RuntimeError("vision_prospect_store_unsafe")

    existing = set()
    for prospect_id, encoded in artifacts:
        destination = prospects / f"{prospect_id}.json"
        if destination.exists():
            if (
                not destination.is_file()
                or destination.is_symlink()
                or destination.stat().st_size != len(encoded)
                or destination.read_bytes() != encoded
            ):
                raise RuntimeError("vision_prospect_import_conflict")
            existing.add(prospect_id)

    created = 0
    for prospect_id, encoded in artifacts:
        if prospect_id in existing:
            continue
        destination = prospects / f"{prospect_id}.json"
        try:
            with destination.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            created += 1
        except FileExistsError:
            if (
                not destination.is_file()
                or destination.is_symlink()
                or destination.stat().st_size != len(encoded)
                or destination.read_bytes() != encoded
            ):
                raise RuntimeError("vision_prospect_import_conflict")

    return {
        "contract": VISION_PROSPECT_IMPORT_CONTRACT,
        "status": "ready",
        "created": created,
        "replayed": len(artifacts) - created,
        "researched_unsent_unqualified": len(artifacts),
        "next_action": "Review one researched prospect; do not count it as a lead or revenue until factual qualification.",
        "controls": {
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "input_files_modified": 0,
        },
    }


def _render_prospect_outreach_draft(record: dict) -> bytes:
    organization = " ".join(record["organization"].split())
    signal = " ".join(record["verified_public_signal"].split())
    contact = " ".join(record["public_contact"].split())
    audience = "your product team" if record["route_type"] == "direct product team" else "your team or one authorized client project"
    body = f"""DRAFT — OWNER REVIEW REQUIRED — NOT SENT

Prospect: {organization}
Public contact route: {contact}
Evidence source: {record['source']}
Research status: researched, unsent, and unqualified

Subject: Small local visual-QA pilot for {organization}

Hi {organization} team,

Your public product or service information describes: {signal}

We built SuperMega Vision, a local observation-only system that learns a small set of buyer-approved Windows or Android screen states and reports held-out accuracy, false positives, abstentions, and device latency. It does not need to click, send messages, accept payments, handle credentials, or change production data.

I am not assuming a fit. Is there one screen workflow that {audience} repeatedly checks by eye? If so, we can first verify screenshot rights, the state set, human fallback, device, workload, and acceptance thresholds. Only then would we decide whether a four-week paid founding pilot is worthwhile.

No data upload or production connection is required for the first conversation.

This local draft has not been sent. Its public claims and contact route must be rechecked before use.
"""
    return body.encode("utf-8")


def _prospect_outreach_artifacts(prospect_id: str, research_bytes: bytes, record: dict) -> tuple[bytes, bytes]:
    draft = _render_prospect_outreach_draft(record)
    receipt = {
        "contract": PROSPECT_OUTREACH_RECEIPT_CONTRACT,
        "prospect_id": prospect_id,
        "research_sha256": hashlib.sha256(research_bytes).hexdigest(),
        "draft_sha256": hashlib.sha256(draft).hexdigest(),
        "draft_file": f"{prospect_id}.txt",
        "status": "owner_review_required_unsent",
        "effects": {
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "lead_qualifications": 0,
        },
    }
    encoded_receipt = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return draft, encoded_receipt


def _write_exact_if_missing(path: Path, expected: bytes, conflict: str) -> bool:
    if path.exists():
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != len(expected)
            or path.read_bytes() != expected
        ):
            raise RuntimeError(conflict)
        return False
    try:
        with path.open("xb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except FileExistsError:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != len(expected)
            or path.read_bytes() != expected
        ):
            raise RuntimeError(conflict)
        return False


def create_vision_prospect_drafts(sales_root: Path | None = None) -> dict:
    requested_sales = (sales_root or default_vision_sales_root()).expanduser()
    if requested_sales.exists() and requested_sales.is_symlink():
        raise RuntimeError("vision_sales_root_unsafe")
    sales = requested_sales.resolve()
    research_root = sales / "research"
    prospects = research_root / "prospects"
    inventory, integrity_failures = _prospect_research_inventory(prospects)
    if integrity_failures:
        raise RuntimeError("vision_prospect_research_integrity_failed")

    drafts = research_root / "outreach-drafts"
    receipts = research_root / "outreach-receipts"
    for directory in (research_root, drafts, receipts):
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise RuntimeError("vision_prospect_outreach_store_unsafe")
    drafts.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)

    expected = []
    for prospect_id, (record, research_bytes) in sorted(inventory.items()):
        draft, receipt = _prospect_outreach_artifacts(prospect_id, research_bytes, record)
        expected.append((drafts / f"{prospect_id}.txt", draft, receipts / f"{prospect_id}.json", receipt))
    for draft_path, draft, receipt_path, receipt in expected:
        for path, content in ((draft_path, draft), (receipt_path, receipt)):
            if path.exists() and (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != len(content)
                or path.read_bytes() != content
            ):
                raise RuntimeError("vision_prospect_outreach_conflict")

    drafts_created = 0
    receipts_created = 0
    for draft_path, draft, receipt_path, receipt in expected:
        drafts_created += int(_write_exact_if_missing(draft_path, draft, "vision_prospect_outreach_conflict"))
        receipts_created += int(_write_exact_if_missing(receipt_path, receipt, "vision_prospect_outreach_conflict"))
    return {
        "contract": VISION_PROSPECT_DRAFTS_CONTRACT,
        "status": "ready",
        "researched_prospects": len(inventory),
        "drafts_created": drafts_created,
        "receipts_created": receipts_created,
        "drafts_replayed": len(inventory) - drafts_created,
        "next_action": "Review one local outreach draft; nothing has been sent, qualified, or priced as revenue.",
        "controls": {
            "model_calls": 0,
            "network_requests": 0,
            "external_sends": 0,
            "payments": 0,
            "lead_qualifications": 0,
            "research_mutations": 0,
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
            attention += 1
            continue
        if not path.is_file() or path.is_symlink():
            attention += 1
            continue
        files.append(path)
    return files, attention


def _prospect_research_inventory(directory: Path) -> tuple[dict[str, tuple[dict, bytes]], int]:
    files, integrity_failures = _regular_json_files(directory)
    inventory = {}
    for path in files:
        try:
            if path.stat().st_size > MAX_STATUS_FILE_BYTES:
                raise ValueError("prospect_artifact_too_large")
            encoded = path.read_bytes()
            artifact = json.loads(encoded.decode("utf-8"))
            record = artifact.get("record")
            normalized = _normalized_prospect_row(record)
            prospect_id, expected = _prospect_research_artifact(normalized)
            canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if (
                artifact.get("contract") != PROSPECT_RESEARCH_CONTRACT
                or artifact.get("prospect_id") != prospect_id
                or artifact.get("record_sha256") != hashlib.sha256(canonical).hexdigest()
                or record != normalized
                or path.name != f"{prospect_id}.json"
                or encoded != expected
            ):
                raise ValueError("prospect_artifact_invalid")
            inventory[prospect_id] = (normalized, encoded)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            integrity_failures += 1
    return inventory, integrity_failures


def _regular_text_files(directory: Path) -> tuple[list[Path], int]:
    if not directory.exists():
        return [], 0
    if not directory.is_dir() or directory.is_symlink():
        return [], 1
    entries = sorted(directory.iterdir(), key=lambda path: path.name.encode("utf-8"))
    attention = max(0, len(entries) - MAX_STATUS_FILES)
    files = []
    for path in entries[:MAX_STATUS_FILES]:
        if path.suffix.lower() != ".txt" or not path.is_file() or path.is_symlink():
            attention += 1
            continue
        files.append(path)
    return files, attention


def _prospect_outreach_status(research_root: Path, inventory: dict[str, tuple[dict, bytes]]) -> tuple[int, int]:
    receipt_files, integrity_failures = _regular_json_files(research_root / "outreach-receipts")
    draft_files, draft_attention = _regular_text_files(research_root / "outreach-drafts")
    integrity_failures += draft_attention
    drafts_by_name = {path.name: path for path in draft_files}
    referenced_drafts = set()
    ready = 0
    for receipt_path in receipt_files:
        try:
            if receipt_path.stat().st_size > MAX_STATUS_FILE_BYTES:
                raise ValueError("outreach_receipt_too_large")
            encoded_receipt = receipt_path.read_bytes()
            receipt = json.loads(encoded_receipt.decode("utf-8"))
            prospect_id = receipt.get("prospect_id")
            if not isinstance(prospect_id, str) or prospect_id not in inventory:
                raise ValueError("outreach_prospect_unknown")
            record, research_bytes = inventory[prospect_id]
            expected_draft, expected_receipt = _prospect_outreach_artifacts(prospect_id, research_bytes, record)
            draft_name = f"{prospect_id}.txt"
            draft_path = drafts_by_name.get(draft_name)
            if draft_path is not None:
                referenced_drafts.add(draft_name)
            if (
                receipt.get("contract") != PROSPECT_OUTREACH_RECEIPT_CONTRACT
                or receipt_path.name != f"{prospect_id}.json"
                or encoded_receipt != expected_receipt
                or draft_path is None
                or draft_path.stat().st_size != len(expected_draft)
                or draft_path.read_bytes() != expected_draft
            ):
                raise ValueError("outreach_artifact_invalid")
            ready += 1
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            integrity_failures += 1
    integrity_failures += len(set(drafts_by_name) - referenced_drafts)
    return ready, integrity_failures


def vision_sales_status(sales_root: Path | None = None) -> dict:
    sales = (sales_root or default_vision_sales_root()).expanduser().resolve()
    inbox = sales / "inbox"
    outbox = sales / "outbox"
    receipts = outbox / "receipts"
    proposals = outbox / "proposals"
    replies = outbox / "reply-drafts"
    rejections = outbox / "rejections"
    research_root = sales / "research"
    research_inventory, research_integrity_failures = _prospect_research_inventory(research_root / "prospects")
    outreach_drafts_ready, outreach_integrity_failures = _prospect_outreach_status(research_root, research_inventory)
    research_integrity_failures += outreach_integrity_failures
    researched = len(research_inventory)

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
    if integrity_failures or research_integrity_failures:
        next_action = "Inspect sales artifact integrity failures before using any draft."
    elif pending:
        next_action = "Run the bounded Vision sales worker for pending contact events."
    elif qualified:
        next_action = "Review qualified proposal and reply drafts; nothing has been sent."
    elif blocked:
        next_action = "Review blocked-lead questions and missing start gates; nothing has been sent."
    elif researched and outreach_drafts_ready < researched:
        next_action = "Generate the missing local prospect outreach drafts; nothing has been sent or qualified."
    elif outreach_drafts_ready:
        next_action = "Review one local outreach draft; nothing has been sent, qualified, or counted as revenue."
    else:
        next_action = "No Vision leads are ready; keep the local inbox available for contact events."

    return {
        "contract": VISION_SALES_STATUS_CONTRACT,
        "status": "attention" if integrity_failures or input_attention or research_integrity_failures else "ready",
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
        "research": {
            "researched_unsent_unqualified": researched,
            "outreach_drafts_ready": outreach_drafts_ready,
            "integrity_failures": research_integrity_failures,
            "value_label": "Research only; not a lead, proposal, booked revenue, or collected revenue.",
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
