from __future__ import annotations

import html
import hmac
import json
import math
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from .build_info import BUILD_ID, RUNTIME_BUILD_SCHEMA, SOURCE_SHA256
from .core import (
    Company, MockModel, OllamaModel, PLAYBOOKS, QueueClaim, ReportFinalizationPending,
)


MAX_FORM_BYTES = 16 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_build_identity() -> dict[str, object]:
    return {
        "schema": RUNTIME_BUILD_SCHEMA,
        "package_version": __version__,
        "build_id": BUILD_ID,
        "git_commit": None,
        "source_dirty": None,
        "source_sha256": SOURCE_SHA256,
    }


def runtime_model_identity(company: Company) -> dict[str, object]:
    """Return bounded non-secret model configuration for readiness checks."""
    if isinstance(company.model, OllamaModel):
        model_name = company.model.model
        return {
            "provider": "ollama",
            "model": (
                model_name
                if type(model_name) is str and 1 <= len(model_name) <= 256
                else None
            ),
            "endpoint": (
                "loopback_default"
                if company.model.host == "http://127.0.0.1:11434"
                else "nonlocal"
            ),
        }
    if isinstance(company.model, MockModel):
        return {"provider": "mock", "model": None, "endpoint": None}
    return {"provider": "unknown", "model": None, "endpoint": None}


class LocalQueueWorker:
    def __init__(self, company: Company) -> None:
        self.company = company
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict[str, object] = {"status": "idle"}

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return dict(self._state)

    def _set_state(self, **values: object) -> None:
        with self._state_lock:
            self._state = dict(values)

    def start(self, queue_id: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A local queue mission is already running")
        claim: QueueClaim | None = None
        try:
            if not queue_id:
                raise ValueError("Reviewed queue mission ID is required")
            self._set_state(status="running", queue_id=queue_id, started_at=_utc_now())
            claim = self.company.claim_next_queue_item(queue_id)
            threading.Thread(
                target=self._run, args=(claim,), name="local-company-worker", daemon=False,
            ).start()
        except Exception as exc:
            try:
                if claim is not None:
                    self.company.abandon_queue_claim(
                        claim, f"Local worker thread did not start: {type(exc).__name__}: {exc}",
                    )
                self._set_state(
                    status="failed", queue_id=claim.queue_id if claim else queue_id,
                    error=f"{type(exc).__name__}: {exc}", finished_at=_utc_now(),
                )
            finally:
                self._run_lock.release()
            raise

    def reserve_shutdown(self) -> bool:
        """Atomically exclude mission startup while an accepted shutdown completes."""
        return self._run_lock.acquire(blocking=False)

    def cancel_shutdown(self) -> None:
        """Release a shutdown reservation when the response could not be accepted."""
        self._run_lock.release()

    def _run(self, claim: QueueClaim) -> None:
        try:
            queue_id, job_id, output, passed = self.company.execute_queue_claim(claim)
            self._set_state(
                status="complete" if passed else "quality_failed",
                queue_id=queue_id,
                job_id=job_id,
                output=str(output),
                quality_passed=passed,
                finished_at=_utc_now(),
            )
        except PermissionError as exc:
            self._set_state(status="needs_approval", error=str(exc), finished_at=_utc_now())
        except ReportFinalizationPending as exc:
            job_id = None
            try:
                job_id = next(
                    (
                        item["job_id"] for item in self.company.pending_completion_items()
                        if item["queue_id"] == claim.queue_id
                    ),
                    None,
                )
            except Exception:
                pass
            state: dict[str, object] = {
                "status": "completion_pending", "queue_id": claim.queue_id,
                "error": str(exc), "finished_at": _utc_now(),
            }
            if job_id:
                state["job_id"] = job_id
            self._set_state(**state)
        except Exception as exc:
            self._set_state(
                status="failed", queue_id=claim.queue_id,
                error=f"{type(exc).__name__}: {exc}", finished_at=_utc_now()
            )
        finally:
            self._run_lock.release()


def dashboard_snapshot(
    company: Company, worker: LocalQueueWorker | None = None
) -> dict[str, object]:
    next_due = company.next_due_queue_item()
    queue_preflight = company.queue_preflight(
        str(next_due[0]) if next_due else None,
    )
    return {
        "projects": company.projects(),
        "jobs": company.jobs(),
        "queue": company.queue_items(),
        "schedules": company.schedules(),
        "datasets": company.dataset_quality_items(),
        "evaluations": company.recent_evaluations(),
        "pending_approvals": company.action_requests("pending"),
        "due_queue_item": bool(next_due),
        "next_due_queue_item": next_due,
        "queue_preflight": queue_preflight,
        "worker": worker.snapshot() if worker else {"status": "disabled"},
        "health": company.health_snapshot(),
    }


def build_status_snapshot(
    company: Company,
    worker: LocalQueueWorker | None,
    build_identity: dict[str, object],
    runtime_identity: dict[str, object] | None = None,
    company_identity: dict[str, str] | None = None,
) -> dict[str, object]:
    """Return the bounded state required to decide whether restart is safe."""
    work_state = company.work_state_snapshot()
    worker_state = worker.snapshot() if worker else {"status": "disabled"}
    return {
        "status": "ready",
        "pid": os.getpid(),
        "build": dict(build_identity),
        "runtime": dict(
            runtime_model_identity(company)
            if runtime_identity is None else runtime_identity
        ),
        "company": dict(
            company.company_identity()
            if company_identity is None else company_identity
        ),
        "health": work_state,
        "worker": {"status": worker_state.get("status")},
    }


def health_endpoint_snapshot(
    company: Company,
    worker: LocalQueueWorker | None,
    build_identity: dict[str, object],
    company_identity: dict[str, str],
    service_instance_id: str | None = None,
) -> dict[str, object]:
    """Return bounded operational telemetry without business records or paths."""
    observed = company.health_snapshot()

    def bounded_text(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 200:
            return None
        return value

    def bounded_count(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= (1 << 63) - 1 else None

    installed_models = observed.get("installed_models")
    worker_state = worker.snapshot() if worker else {"status": "disabled"}
    raw_worker_status = worker_state.get("status")
    worker_status = (
        raw_worker_status
        if isinstance(raw_worker_status, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", raw_worker_status)
        else "unknown"
    )
    health = {
        "python": bounded_text(observed.get("python")),
        "platform": bounded_text(observed.get("platform")),
        "database_bytes": bounded_count(observed.get("database_bytes")),
        "report_count": bounded_count(observed.get("report_count")),
        "report_bytes": bounded_count(observed.get("report_bytes")),
        "disk_free_bytes": bounded_count(observed.get("disk_free_bytes")),
        "disk_total_bytes": bounded_count(observed.get("disk_total_bytes")),
        "ollama_model_storage_bytes": bounded_count(
            observed.get("ollama_model_storage_bytes")
        ),
        "ollama_reachable": isinstance(installed_models, list),
        "installed_model_count": (
            len(installed_models) if isinstance(installed_models, list) else None
        ),
        "dataset_count": bounded_count(observed.get("dataset_count")),
        "active_jobs": bounded_count(observed.get("active_jobs")),
        "queued_missions": bounded_count(observed.get("queued_missions")),
        "running_missions": bounded_count(observed.get("running_missions")),
        "pending_approvals": bounded_count(observed.get("pending_approvals")),
        "pending_report_finalizations": bounded_count(
            observed.get("pending_report_finalizations")
        ),
        "pending_evaluations": bounded_count(observed.get("pending_evaluations")),
    }
    service_identity = (
        {"service_instance_id": service_instance_id}
        if service_instance_id is not None else {}
    )
    return {
        "schema": "local-company.health.v1",
        "status": "ready",
        "pid": os.getpid(),
        **service_identity,
        "build": dict(build_identity),
        "company": dict(company_identity),
        "health": health,
        "worker": {"status": worker_status},
    }


def render_dashboard(
    company: Company, service_token: str | None = None, notice: str = "",
    worker: LocalQueueWorker | None = None,
    build_identity: dict[str, object] | None = None,
    draft_fields: dict[str, str] | None = None,
) -> str:
    snapshot = dashboard_snapshot(company, worker)
    live_build = dict(
        runtime_build_identity() if build_identity is None else build_identity
    )

    def cell(value: object) -> str:
        return html.escape(str(value))

    draft = draft_fields if isinstance(draft_fields, dict) else {}
    draft_objective = (
        draft.get("objective", "")
        if isinstance(draft.get("objective", ""), str) else ""
    )[:4000]
    draft_project = (
        draft.get("project", "") if isinstance(draft.get("project", ""), str) else ""
    )
    draft_playbook = (
        draft.get("playbook", "")
        if isinstance(draft.get("playbook", ""), str) else ""
    )
    raw_priority = (
        draft.get("priority", "50")
        if isinstance(draft.get("priority", "50"), str) else "50"
    )
    draft_priority = (
        raw_priority
        if raw_priority.isdigit() and 0 <= int(raw_priority) <= 100 else "50"
    )

    completion_items = snapshot["health"]["pending_completion"]
    completion_by_job = {item["job_id"]: item["state"] for item in completion_items}
    completion_by_queue = {
        item["queue_id"]: item["state"] for item in completion_items if item["queue_id"]
    }

    def completion_label(state: object) -> str:
        return str(state).replace("_", " ")

    project_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}</td><td>{cell(row[3])}</td></tr>"
        for row in snapshot["projects"]
    ) or '<tr><td colspan="3" class="empty">No projects</td></tr>'
    quality_by_job = {row[0]: ("pass" if row[1] else "fail", row[2]) for row in snapshot["evaluations"]}

    def mission_link(job_id: object) -> str:
        if not job_id:
            return "-"
        escaped = cell(job_id)
        return f'<a href="/missions/{escaped}"><code>{escaped}</code></a>'

    def job_action(row: tuple[object, ...]) -> str:
        if not service_token or row[1] != "complete":
            return "-"
        return (
            '<form method="post" action="/jobs/quality" class="inline">'
            f'<input type="hidden" name="service_token" value="{cell(service_token)}">'
            f'<input type="hidden" name="job_id" value="{cell(row[0])}">'
            '<button type="submit">Recheck</button></form>'
        )

    job_rows = "".join(
        f'<tr><td>{mission_link(row[0])}</td><td><span class="status {cell(row[1])}">{cell(row[1])}</span>'
        + (
            f'<br><span class="completion-pending">{cell(completion_label(completion_by_job[row[0]]))}</span>'
            if row[0] in completion_by_job else ""
        )
        + '</td>'
        f"<td>{cell(quality_by_job.get(row[0], ('-', '-'))[0])} {cell(quality_by_job.get(row[0], ('-', '-'))[1])}</td>"
        f"<td>{cell(row[3])}</td><td>{cell(row[2])}</td><td>{job_action(row)}</td></tr>"
        for row in snapshot["jobs"][:30]
    ) or '<tr><td colspan="6" class="empty">No missions</td></tr>'
    def queue_action(row: tuple[object, ...]) -> str:
        if not service_token:
            return "-"
        if row[1] == "queued":
            endpoint, label, css = "/queue/cancel", "Cancel", "danger"
        elif row[1] in {"failed", "quality_failed"}:
            endpoint, label, css = "/queue/reset", "Retry", ""
        else:
            return "-"
        return (
            f'<form method="post" action="{endpoint}" class="inline">'
            f'<input type="hidden" name="service_token" value="{cell(service_token)}">'
            f'<input type="hidden" name="queue_id" value="{cell(row[0])}">'
            f'<button type="submit" class="{css}">{label}</button></form>'
        )

    queue_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}"
        + (
            f'<br><span class="completion-pending">{cell(completion_label(completion_by_queue[row[0]]))}</span>'
            if row[0] in completion_by_queue else ""
        )
        + f"</td><td>{cell(row[2])}</td>"
        f"<td>{cell(row[3])}</td><td>{cell(row[4] or '-')}</td><td>{cell(row[6])}</td>"
        f"<td>{mission_link(row[7])}</td><td>{cell(row[8] or '-')}</td><td>{queue_action(row)}</td></tr>"
        for row in snapshot["queue"][:30]
    ) or '<tr><td colspan="9" class="empty">Queue is empty</td></tr>'
    schedule_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}</td>"
        f"<td>{'enabled' if row[2] else 'disabled'}</td><td>{cell(row[3])}d</td><td>{cell(row[4])}</td></tr>"
        for row in snapshot["schedules"]
    ) or '<tr><td colspan="5" class="empty">No schedules</td></tr>'
    def dataset_quality_label(item: dict[str, object]) -> str:
        if item.get("profile_status") != "ready":
            return '<span class="unavailable">profile unavailable</span>'
        signal_count = item.get("quality_signal_count")
        if type(signal_count) is int and signal_count > 0:
            return f'<span class="review">review ({cell(signal_count)} signal categories)</span>'
        return '<span class="clear">no deterministic flags</span>'

    def dataset_contract_label(item: dict[str, object]) -> str:
        status = str(item.get("contract_status", "unavailable"))
        css = (
            "review" if status == "violations" else
            "clear" if status in {"conforms", "conforms profiled rows"} else
            "unavailable" if status == "unavailable" else ""
        )
        return f'<span class="{css}">{cell(status)}</span>'

    dataset_rows = "".join(
        f'<tr><td><a href="/datasets/{cell(item["id"])}"><code>{cell(item["id"])}</code></a></td>'
        f'<td>{cell(item["project"])}</td><td>{cell(item["format"])}</td>'
        f'<td>{cell(item["row_count"])}</td><td>{cell(item["column_count"])}</td>'
        f'<td>{dataset_quality_label(item)}</td><td>{cell(item["key_status"])}</td>'
        f'<td>{dataset_contract_label(item)}</td>'
        f'<td>{cell(item["added_at"])}</td></tr>'
        for item in snapshot["datasets"][:30]
    ) or '<tr><td colspan="9" class="empty">No datasets</td></tr>'
    approval_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[2])}</td><td>{cell(row[4])}</td></tr>"
        for row in snapshot["pending_approvals"]
    ) or '<tr><td colspan="3" class="empty">No pending approvals</td></tr>'

    project_options = "".join(
        f'<option value="{cell(row[0])}"'
        f'{" selected" if str(row[0]) == draft_project else ""}>{cell(row[1])}</option>'
        for row in snapshot["projects"]
    )
    playbook_options = "".join(
        f'<option value="{cell(name)}"'
        f'{" selected" if name == draft_playbook else ""}>'
        f'{cell(name)} - {cell(item["description"])}</option>'
        for name, item in PLAYBOOKS.items()
    )
    intake = ""
    if service_token:
        worker_status = str(snapshot["worker"].get("status", "idle"))
        next_due = snapshot["next_due_queue_item"]
        queue_preflight = snapshot["queue_preflight"]
        submission_allowed = queue_preflight.get("submission_allowed") is True
        run_disabled = (
            " disabled"
            if worker_status == "running" or completion_items or not next_due
            or not submission_allowed
            else ""
        )
        reviewed_queue_id = str(next_due[0]) if next_due else ""
        objective_preview = ""
        if next_due:
            objective_preview = str(next_due[6])
            if len(objective_preview) > 240:
                objective_preview = objective_preview[:237] + "..."
        if worker_status == "running":
            run_hint = "A local mission is running."
        elif completion_items:
            run_hint = (
                "Mission completion is pending; wait for the worker or recover it "
                "after its heartbeat is stale."
            )
        elif not next_due:
            run_hint = "No queued mission is due."
        else:
            run_hint = (
                f"Reviewed next mission {reviewed_queue_id} at priority {next_due[2]}, "
                f"project {next_due[4] or 'Unscoped'}, due {next_due[3]}: "
                f"{objective_preview}"
            )
            preflight_status = str(queue_preflight.get("status", "blocked"))
            if preflight_status == "ready":
                knowledge = queue_preflight.get("knowledge", {})
                source_count = (
                    knowledge.get("source_count", 0)
                    if isinstance(knowledge, dict) else 0
                )
                run_hint += (
                    f" Knowledge preflight ready: {source_count} registered "
                    "source(s) current; no work or model call started."
                )
            elif preflight_status == "owner_gate_required":
                categories = queue_preflight.get("owner_gate_categories", [])
                category_text = ", ".join(str(item) for item in categories)
                run_hint += (
                    " Owner gate required before model execution"
                    f" ({category_text}); submission records the request and calls no model."
                )
            else:
                blockers = queue_preflight.get("blockers", [])
                blocker_text = ", ".join(str(item) for item in blockers) or "not_ready"
                run_hint += (
                    f" Run blocked before claim: {blocker_text}. Audit and correct "
                    "the named local condition, then refresh this page."
                )
        run_label = (
            f"Request owner review for {reviewed_queue_id}"
            if next_due and queue_preflight.get("status") == "owner_gate_required"
            else f"Run {reviewed_queue_id} locally"
            if next_due else "Run reviewed mission locally"
        )
        intake = f"""
<section class="intake"><h2>Queue a SuperMega task</h2>
<p class="hint">Previewing is read-only and calls no model. Queuing records work only; neither action performs an external action.</p>
<form method="post" action="/queue/enqueue">
<input type="hidden" name="service_token" value="{cell(service_token)}">
<label>Objective<textarea name="objective" maxlength="4000" required placeholder="Describe the result, evidence, constraints, and owner gates.">{cell(draft_objective)}</textarea></label>
<div class="form-grid">
<label>Project<select name="project"><option value="">Unscoped</option>{project_options}</select></label>
<label>Playbook<select name="playbook"><option value="">Automatic routing</option>{playbook_options}</select></label>
<label>Priority (0-100)<input name="priority" type="number" min="0" max="100" value="{cell(draft_priority)}" required></label>
</div><button type="submit" formaction="/queue/preview-team">Preview team (no model)</button>
<button type="submit">Add to queue</button></form>
<h3>Run one local mission</h3><p class="hint">{cell(run_hint)}</p>
<form method="post" action="/queue/run-next">
<input type="hidden" name="service_token" value="{cell(service_token)}">
<input type="hidden" name="queue_id" value="{cell(reviewed_queue_id)}">
<button type="submit"{run_disabled}>{cell(run_label)}</button></form></section>"""
    notice_html = f'<p class="notice">{cell(notice)}</p>' if notice else ""
    completion_html = ""
    if completion_items:
        completion_rows = "".join(
            f"<li>{mission_link(item['job_id'])}: {cell(completion_label(item['state']))}"
            f"{f' (queue {cell(item['queue_id'])})' if item['queue_id'] else ''} "
            f"since {cell(item['since'])}</li>"
            for item in completion_items
        )
        completion_html = (
            '<section class="completion-banner"><h2>Mission completion pending</h2>'
            '<p>The durable local state is preserved. No external action is involved. '
            'Wait for an active worker; use stale recovery only after its heartbeat ages out.</p>'
            f"<ul>{completion_rows}</ul></section>"
        )
    build_html = (
        '<section class="build-banner"><h2>Live build</h2>'
        f'<p><strong>{cell(live_build.get("build_id", "unknown"))}</strong> &middot; '
        f'package {cell(live_build.get("package_version", "unknown"))} &middot; '
        f'{cell(live_build.get("schema", "unknown"))}</p>'
        f'<p class="build-hash">Operational source SHA-256: '
        f'<code>{cell(live_build.get("source_sha256", "unknown"))}</code></p>'
        f'<p class="hint">Git commit: '
        f'{cell(live_build.get("git_commit") or "unavailable")} &middot; '
        f'source dirty state: '
        f'{cell("unknown" if live_build.get("source_dirty") is None else live_build.get("source_dirty"))}</p>'
        '<p class="hint">Captured at service startup. Use the local live-build checker '
        'after a release to detect whether a restart is required. Compact build status: '
        '<a href="/build-status.json">build-status.json</a>; bounded health: '
        '<a href="/health.json">health.json</a>.</p></section>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Agent Company</title>
<style>
:root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; background:#0b1020; color:#e8ecf4; }}
body {{ max-width:1200px; margin:0 auto; padding:32px 20px 60px; }}
h1 {{ margin:0; font-size:28px; }} .sub {{ color:#9aa7bd; margin:6px 0 28px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:24px; }}
.card {{ background:#121a2d; border:1px solid #26324a; border-radius:12px; padding:18px; }}
.metric {{ font-size:28px; font-weight:700; }} .label {{ color:#9aa7bd; font-size:13px; }}
section {{ margin-top:26px; }} table {{ width:100%; border-collapse:collapse; background:#121a2d; border-radius:12px; overflow:hidden; }}
th,td {{ padding:11px 13px; border-bottom:1px solid #26324a; text-align:left; vertical-align:top; }}
th {{ color:#9aa7bd; font-size:12px; text-transform:uppercase; }} td {{ font-size:14px; }}
code {{ color:#8bd5ff; }} a {{ color:#8bd5ff; }} .refresh {{ margin-left:10px; }}
.status {{ padding:3px 8px; border-radius:999px; background:#26324a; }}
    .complete,.clear {{ color:#87e6a8; }} .failed,.interrupted,.unavailable {{ color:#ff9b9b; }} .running,.review {{ color:#ffd479; }}
.empty {{ color:#6f7d95; text-align:center; }} .gate,.completion-pending {{ color:#ffd479; }}
.notice {{ padding:12px 14px; background:#143520; border:1px solid #27693d; border-radius:9px; color:#a7f3bd; }}
.completion-banner {{ padding:12px 16px; border:1px solid #67582b; background:#2b2615; border-radius:9px; }}
.build-banner {{ padding:12px 16px; border:1px solid #31527a; background:#111f35; border-radius:9px; }}
.build-banner h2 {{ margin-bottom:8px; }} .build-banner p {{ margin:6px 0; }}
.build-hash code {{ overflow-wrap:anywhere; }}
.hint {{ color:#9aa7bd; margin-top:-6px; }} label {{ display:block; color:#cbd5e1; font-size:13px; }}
textarea,input,select {{ box-sizing:border-box; width:100%; margin-top:6px; padding:10px; color:#e8ecf4; background:#0b1020; border:1px solid #3a4864; border-radius:8px; }}
textarea {{ min-height:108px; resize:vertical; }} .form-grid {{ display:grid; grid-template-columns:2fr 2fr 1fr; gap:12px; margin:12px 0; }}
button {{ padding:9px 14px; color:#07111f; background:#8bd5ff; border:0; border-radius:8px; font-weight:700; cursor:pointer; }}
button.danger {{ padding:6px 10px; color:#ffd7d7; background:#5a1f2a; }} form.inline {{ display:inline; }}
button:disabled {{ cursor:not-allowed; opacity:.45; }}
@media(max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} th:nth-child(4),td:nth-child(4) {{ display:none; }} }}
</style></head><body>
<h1>Local Agent Company</h1><p class="sub">Owner-controlled task intake &middot; localhost only <a class="refresh" href="/">Refresh</a><br>Scores are automated format, safety, and evidence-consistency checks—not factual or production verification.</p>
{notice_html}{completion_html}{build_html}{intake}
<div class="grid">
<div class="card"><div class="metric">{len(snapshot['projects'])}</div><div class="label">Projects</div></div>
<div class="card"><div class="metric">{len(snapshot['jobs'])}</div><div class="label">Missions</div></div>
<div class="card"><div class="metric">{sum(1 for row in snapshot['queue'] if row[1] == 'queued')}</div><div class="label">Queued missions</div></div>
<div class="card"><div class="metric gate">{len(snapshot['pending_approvals'])}</div><div class="label">Pending owner approvals</div></div>
<div class="card"><div class="metric">{snapshot['health']['disk_free_bytes'] / (1024 ** 3):.1f}</div><div class="label">Free disk GiB</div></div>
<div class="card"><div class="metric">{snapshot['health']['ollama_model_storage_bytes'] / (1024 ** 3):.1f}</div><div class="label">Ollama model GiB</div></div>
<div class="card"><div class="metric">{len(snapshot['datasets'])}</div><div class="label">Profiled datasets</div></div>
<div class="card"><div class="metric">{cell(snapshot['worker'].get('status', 'disabled'))}</div><div class="label">Local worker</div></div>
</div>
<section><h2>Mission queue</h2><table><thead><tr><th>ID</th><th>Status</th><th>Priority</th><th>Scheduled UTC</th><th>Project</th><th>Objective</th><th>Report</th><th>Error</th><th>Action</th></tr></thead><tbody>{queue_rows}</tbody></table></section>
<section><h2>Recurring schedules</h2><table><thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Cadence</th><th>Next UTC</th></tr></thead><tbody>{schedule_rows}</tbody></table></section>
<section><h2>Dataset quality</h2><p class="hint">Stored aggregate profiles only; source paths and row values are withheld. Only explicitly declared contract rules are treated as business checks.</p><table><thead><tr><th>ID</th><th>Project</th><th>Format</th><th>Rows</th><th>Columns</th><th>Quality</th><th>Declared key</th><th>Contract</th><th>Profiled UTC</th></tr></thead><tbody>{dataset_rows}</tbody></table></section>
<section><h2>Recent missions</h2><table><thead><tr><th>ID</th><th>Report state</th><th>Automated checks</th><th>Objective</th><th>Created UTC</th><th>Action</th></tr></thead><tbody>{job_rows}</tbody></table></section>
<section><h2>Projects</h2><table><thead><tr><th>ID</th><th>Name</th><th>Missions</th></tr></thead><tbody>{project_rows}</tbody></table></section>
<section><h2>Approval inbox</h2><table><thead><tr><th>ID</th><th>Category</th><th>Proposed action</th></tr></thead><tbody>{approval_rows}</tbody></table></section>
</body></html>"""


def render_dataset_quality_detail(company: Company, dataset_id: str) -> str:
    detail = company.dataset_quality_detail(dataset_id)
    profile = detail["profile"] if isinstance(detail.get("profile"), dict) else {}

    def cell(value: object, limit: int = 180) -> str:
        rendered = str(value)
        if len(rendered) > limit:
            rendered = rendered[: max(0, limit - 3)] + "..."
        return html.escape(rendered)

    def count(value: object) -> str:
        return str(value) if type(value) is int and value >= 0 else "-"

    def number(value: object) -> str:
        if type(value) not in {int, float}:
            return "-"
        numeric = float(value)
        return f"{numeric:.6g}" if math.isfinite(numeric) else "-"

    def rate(value: object) -> str:
        if type(value) not in {int, float}:
            return "-"
        numeric = float(value)
        return f"{numeric:.2%}" if math.isfinite(numeric) and 0 <= numeric <= 1 else "-"

    def names(value: object, limit: int = 20) -> str:
        if not isinstance(value, list):
            return "none"
        safe_names = [item for item in value if isinstance(item, str)]
        rendered = ", ".join(cell(item) for item in safe_names[:limit])
        if len(safe_names) > limit:
            rendered += f" (+{len(safe_names) - limit} more)"
        return rendered or "none"

    ready = detail.get("profile_status") == "ready"
    if not ready:
        profile_html = (
            '<section class="warning"><h2>Aggregate profile unavailable</h2>'
            '<p>The stored profile is malformed or uses an unsupported schema. '
            'No source content was opened and no values are displayed.</p></section>'
        )
    else:
        columns = profile.get("columns") if isinstance(profile.get("columns"), dict) else {}
        visible_columns = list(columns.items())[:200]
        omitted_columns = max(0, len(columns) - len(visible_columns))

        def column_row(name: object, item: object) -> str:
            column = item if isinstance(item, dict) else {}
            type_counts = column.get("types") if isinstance(column.get("types"), dict) else {}
            type_parts = [
                f"{cell(type_name, 60)}={count(type_count)}"
                for type_name, type_count in list(type_counts.items())[:8]
            ]
            if len(type_counts) > 8:
                type_parts.append(f"+{len(type_counts) - 8} more")
            numeric = column.get("numeric") if isinstance(column.get("numeric"), dict) else None
            numeric_html = "-"
            if numeric is not None:
                numeric_html = (
                    f"n={count(numeric.get('count'))}; min={number(numeric.get('minimum'))}; "
                    f"p25={number(numeric.get('p25'))}; median={number(numeric.get('median'))}; "
                    f"p75={number(numeric.get('p75'))}; max={number(numeric.get('maximum'))}; "
                    f"mean={number(numeric.get('mean'))}; IQR outliers="
                    f"{count(numeric.get('iqr_outlier_count'))} "
                    f"({rate(numeric.get('iqr_outlier_rate'))})"
                )
            return (
                f"<tr><td><code>{cell(name)}</code></td>"
                f"<td>{', '.join(type_parts) or '-'}</td>"
                f"<td>{count(column.get('missing'))} ({rate(column.get('missing_rate'))})</td>"
                f"<td>{count(column.get('unique_non_missing'))} "
                f"({rate(column.get('unique_rate'))})</td>"
                f"<td>{'yes' if column.get('mixed_types') is True else 'no'}</td>"
                f"<td>{count(column.get('non_finite_numeric'))} / "
                f"{count(column.get('numeric_values_excluded'))}</td>"
                f"<td>{numeric_html}</td></tr>"
            )

        column_rows = "".join(column_row(name, item) for name, item in visible_columns)
        if not column_rows:
            column_rows = '<tr><td colspan="7" class="muted">No column aggregates stored.</td></tr>'
        omitted_html = (
            f'<p class="warning">{omitted_columns} additional columns are omitted from this bounded view.</p>'
            if omitted_columns else ""
        )
        flags = profile.get("quality_flags") if isinstance(profile.get("quality_flags"), dict) else {}
        quality_rows = "".join((
            f"<tr><td>Columns with missing values</td><td>{count(detail.get('missing_columns'))}</td></tr>",
            f"<tr><td>Columns with 1.5-IQR outliers</td><td>{count(detail.get('outlier_columns'))}</td></tr>",
            f"<tr><td>All-missing columns</td><td>{names(flags.get('all_missing_columns'))}</td></tr>",
            f"<tr><td>Mixed-type columns</td><td>{names(flags.get('mixed_type_columns'))}</td></tr>",
            f"<tr><td>Non-finite numeric columns</td><td>{names(flags.get('non_finite_numeric_columns'))}</td></tr>",
            f"<tr><td>Numeric-summary exclusion columns</td><td>{names(flags.get('numeric_values_excluded_columns'))}</td></tr>",
            f"<tr><td>Exact duplicate groups</td><td>{count(flags.get('duplicate_row_groups'))}</td></tr>",
            f"<tr><td>Excess exact duplicate rows</td><td>{count(flags.get('duplicate_rows'))}</td></tr>",
            f"<tr><td>Rows affected by exact duplicates</td><td>{count(flags.get('duplicate_rows_affected'))} "
            f"({rate(flags.get('duplicate_row_rate'))})</td></tr>",
            f"<tr><td>Profile truncated</td><td>{'yes' if flags.get('truncated') is True else 'no'}</td></tr>",
            f"<tr><td>Formula cells ignored</td><td>{count(flags.get('formula_cells_ignored'))}</td></tr>",
            f"<tr><td>Error cells ignored</td><td>{count(flags.get('error_cells_ignored'))}</td></tr>",
        ))
        key_check = profile.get("key_check") if isinstance(profile.get("key_check"), dict) else {}
        if key_check.get("configured") is True:
            key_html = (
                f'<p>Declared columns: <code>{names(key_check.get("columns"), 8)}</code></p>'
                '<table><thead><tr><th>Complete rows</th><th>Completeness</th>'
                '<th>Distinct complete values</th><th>Uniqueness</th><th>Missing rows</th>'
                '<th>Duplicate values</th><th>Rows affected</th></tr></thead><tbody><tr>'
                f'<td>{count(key_check.get("complete_rows"))}</td>'
                f'<td>{rate(key_check.get("completeness_rate"))}</td>'
                f'<td>{count(key_check.get("distinct_complete_values"))}</td>'
                f'<td>{rate(key_check.get("uniqueness_rate"))}</td>'
                f'<td>{count(key_check.get("missing_rows"))}</td>'
                f'<td>{count(key_check.get("duplicate_values"))}</td>'
                f'<td>{count(key_check.get("duplicate_rows"))}</td></tr></tbody></table>'
            )
        else:
            key_html = (
                '<p class="warning">No key was declared, so this profile makes no primary-key '
                'completeness or uniqueness claim.</p>'
            )
        contract_check = (
            profile.get("contract_check")
            if isinstance(profile.get("contract_check"), dict) else {}
        )
        if contract_check.get("configured") is True:
            contract_sections: list[str] = []
            required = contract_check.get("required", [])
            if required:
                required_rows = "".join(
                    f'<tr><td><code>{cell(item.get("column", ""))}</code></td>'
                    f'<td>{"yes" if item.get("column_present") is True else "no"}</td>'
                    f'<td>{count(item.get("missing_rows"))} '
                    f'({rate(item.get("missing_rate"))})</td>'
                    f'<td>{"conforms" if item.get("passed") is True else "violation"}</td></tr>'
                    for item in required if isinstance(item, dict)
                )
                contract_sections.append(
                    '<h3>Required columns</h3><table><thead><tr><th>Column</th>'
                    '<th>Present</th><th>Missing rows</th><th>Result</th></tr></thead>'
                    f'<tbody>{required_rows}</tbody></table>'
                )
            type_checks = contract_check.get("types", [])
            if type_checks:
                type_rows = "".join(
                    f'<tr><td><code>{cell(item.get("column", ""))}</code></td>'
                    f'<td>{"yes" if item.get("column_present") is True else "no"}</td>'
                    f'<td>{names(item.get("allowed_types"), 8)}</td>'
                    f'<td>{count(item.get("checked_non_missing_rows"))}</td>'
                    f'<td>{count(item.get("unexpected_type_rows"))} '
                    f'({rate(item.get("unexpected_type_rate"))})</td>'
                    f'<td>{"conforms" if item.get("passed") is True else "violation"}</td></tr>'
                    for item in type_checks if isinstance(item, dict)
                )
                contract_sections.append(
                    '<h3>Allowed types</h3><table><thead><tr><th>Column</th>'
                    '<th>Present</th><th>Allowed</th><th>Checked non-missing</th>'
                    '<th>Unexpected</th><th>Result</th></tr></thead>'
                    f'<tbody>{type_rows}</tbody></table>'
                )
            ranges = contract_check.get("numeric_ranges", [])
            if ranges:
                range_rows = "".join(
                    f'<tr><td><code>{cell(item.get("column", ""))}</code></td>'
                    f'<td>{"yes" if item.get("column_present") is True else "no"}</td>'
                    f'<td>{number(item.get("minimum"))} to {number(item.get("maximum"))}</td>'
                    f'<td>{count(item.get("checked_finite_rows"))}</td>'
                    f'<td>{count(item.get("uncheckable_non_missing_rows"))}</td>'
                    f'<td>{count(item.get("below_minimum_rows"))} / '
                    f'{count(item.get("above_maximum_rows"))}</td>'
                    f'<td>{count(item.get("violation_rows"))} '
                    f'({rate(item.get("violation_rate"))})</td>'
                    f'<td>{"conforms" if item.get("passed") is True else "violation"}</td></tr>'
                    for item in ranges if isinstance(item, dict)
                )
                contract_sections.append(
                    '<h3>Finite numeric ranges</h3><table><thead><tr><th>Column</th>'
                    '<th>Present</th><th>Inclusive bounds</th><th>Checked finite</th>'
                    '<th>Uncheckable</th><th>Below / above</th><th>Violations</th>'
                    f'<th>Result</th></tr></thead><tbody>{range_rows}</tbody></table>'
                )
            contract_html = (
                f'<p><strong>Status:</strong> {cell(contract_check.get("status", "unavailable"))} '
                f'&middot; Rules: {count(contract_check.get("rule_count"))} '
                f'&middot; Failed: {count(contract_check.get("failed_rules"))} '
                f'&middot; Full source rows: '
                f'{"yes" if contract_check.get("source_rows_complete") is True else "no"}</p>'
                + "".join(contract_sections)
            )
        else:
            contract_html = (
                '<p class="warning">No contract was declared, so this profile makes no '
                'required-column, allowed-type, or numeric-range conformance claim.</p>'
            )
        sheet_html = (
            f' &middot; Sheet: <code>{cell(profile["sheet"])}</code>'
            if isinstance(profile.get("sheet"), str) else ""
        )
        profile_html = f"""
<section><h2>Profile scope</h2>
<p class="meta">Schema: <code>{cell(detail.get('profile_schema') or 'unavailable')}</code>{sheet_html}<br>
Grain assumption: {cell(profile.get('grain_assumption', 'not recorded'))}</p></section>
<section><h2>Deterministic quality flags</h2>
<table><thead><tr><th>Signal</th><th>Aggregate result</th></tr></thead><tbody>{quality_rows}</tbody></table></section>
<section><h2>Declared key check</h2>{key_html}</section>
<section><h2>Declared dataset contract</h2>{contract_html}</section>
<section><h2>Column aggregates</h2>{omitted_html}
<table><thead><tr><th>Column</th><th>Types</th><th>Missing</th><th>Distinct non-missing</th>
<th>Mixed</th><th>Non-finite / excluded</th><th>Finite numeric summary</th></tr></thead>
<tbody>{column_rows}</tbody></table></section>"""

    quality_status = str(detail.get("quality_status", "unavailable"))
    quality_class = "review" if quality_status == "review" else "clear" if ready else "fail"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dataset {cell(detail['id'])}</title><style>
:root {{ color-scheme:dark; font-family:Inter,system-ui,sans-serif; background:#0b1020; color:#e8ecf4; }}
body {{ max-width:1200px; margin:0 auto; padding:28px 20px 60px; }} a,code {{ color:#8bd5ff; }}
.meta,.muted {{ color:#9aa7bd; }} .review,.warning {{ color:#ffd479; }} .clear {{ color:#87e6a8; }} .fail {{ color:#ff9b9b; }}
.warning {{ padding:12px; border:1px solid #67582b; background:#2b2615; border-radius:9px; }}
section {{ margin-top:26px; }} table {{ width:100%; border-collapse:collapse; background:#121a2d; }}
th,td {{ padding:10px 12px; border-bottom:1px solid #26324a; text-align:left; vertical-align:top; }}
th {{ color:#9aa7bd; font-size:12px; text-transform:uppercase; }} td {{ overflow-wrap:anywhere; }}
</style></head><body><p><a href="/">&larr; Dashboard</a></p>
<h1>Dataset <code>{cell(detail['id'])}</code></h1>
<p class="meta">Project: {cell(detail['project'])} &middot; Format: {cell(detail['format'])} &middot;
Rows: {count(detail['row_count'])} &middot; Columns: {count(detail['column_count'])} &middot;
Profiled UTC: {cell(detail['added_at'])}<br>Source SHA-256: <code>{cell(detail['sha256'], 80)}</code></p>
<p class="{quality_class}"><strong>Screening status:</strong> {cell(quality_status)}; declared key: {cell(detail.get('key_status', 'unavailable'))}; contract: {cell(detail.get('contract_status', 'unavailable'))}.</p>
<p class="warning">This localhost page displays stored aggregate statistics only. It withholds source and brief paths and never displays source row values. Only declared key and contract rules are checked; dates, units, allowed values, severity, freshness, and fitness for use still require owner definitions.</p>
{profile_html}</body></html>"""


def render_mission_detail(company: Company, job_id: str) -> str:
    detail = company.job_detail(job_id)
    job = detail["job"]
    evaluation = detail["evaluation"]

    def cell(value: object) -> str:
        return html.escape(str(value))

    assignment_rows = "".join(
        f"<tr><td>{cell(row[0])}</td><td>{cell(row[1])}</td><td>{cell(row[2])}</td>"
        f"<td>{cell(row[3])}</td></tr>" for row in detail["assignments"]
    ) or '<tr><td colspan="4" class="muted">No assignments recorded.</td></tr>'
    event_rows = "".join(
        f"<tr><td>{cell(row[2])}</td><td>{cell(row[0])}</td><td><code>{cell(row[1])}</code></td></tr>"
        for row in detail["events"][-80:]
    ) or '<tr><td colspan="3" class="muted">No audit events recorded.</td></tr>'
    history_rows = "".join(
        f"<tr><td>{cell(row[5])}</td><td>{'pass' if row[0] else 'fail'}</td><td>{cell(row[1])}</td>"
        f"<td><code>{cell(row[2])}</code></td><td><code>{cell(row[3] or '-')}</code></td>"
        f"<td><code>{cell(row[4] or '-')}</code></td></tr>"
        for row in detail["evaluation_history"]
    ) or '<tr><td colspan="6" class="muted">No append-only evaluation runs recorded.</td></tr>'
    manifest = detail["evidence_manifest"] or {}
    manifest_sources = {
        item.get("source_id"): item for item in manifest.get("sources", [])
        if isinstance(item, dict)
    }
    evidence_rows = "".join(
        f"<tr><td><code>[EVIDENCE:{cell(item.get('evidence_id', ''))}]</code></td>"
        f"<td><code>{cell(manifest_sources.get(item.get('source_id'), {}).get('path', '-'))}</code></td>"
        f"<td>{cell(item.get('line_start', '-'))}-{cell(item.get('line_end', '-'))}</td>"
        f"<td><pre class=\"quote\">{cell(item.get('quote', ''))}</pre></td></tr>"
        for item in manifest.get("evidence", []) if isinstance(item, dict)
    ) or '<tr><td colspan="4" class="muted">Legacy mission: no frozen evidence manifest.</td></tr>'

    if evaluation:
        outcome = "PASSED" if evaluation["passed"] else "FAILED"
        failed = [name.replace("_", " ") for name, passed in evaluation["checks"].items() if not passed]
        failed_html = (
            "<ul>" + "".join(f"<li>{cell(name)}</li>" for name in failed) + "</ul>"
            if failed else "<p>No automated gates failed.</p>"
        )
        conflicts = evaluation.get("source_conflicts", [])
        incomplete_roles = evaluation.get("incomplete_specialist_roles", [])
        incomplete_html = (
            '<p class="warning"><strong>Degraded specialist output safely withheld:</strong> '
            f'{cell(", ".join(incomplete_roles))}. This is visible but does not fail the '
            'deterministic synthesis when the isolation proof is intact.</p>'
            if incomplete_roles else ""
        )
        conflict_html = ""
        if conflicts:
            conflict_html = "<h3>Source conflicts</h3>" + "".join(
                "<article class=\"conflict\">"
                f"<p><strong>Claim:</strong> {cell(item.get('claim', ''))}</p>"
                f"<p><strong>Limiting evidence:</strong> {cell(item.get('limitation', ''))}</p>"
                f"<p><strong>Source:</strong> <code>{cell(item.get('source', ''))}</code></p></article>"
                for item in conflicts
            )
        quality_html = (
            f'<p class="outcome {"pass" if evaluation["passed"] else "fail"}">Automated checks {outcome}: '
            f'{cell(evaluation["score"])}/100</p>'
            f'<p class="meta">Evaluator: <code>{cell(evaluation.get("evaluator_version", "legacy"))}</code> · '
            f'Evaluated report SHA-256: <code>{cell(evaluation.get("report_sha256", "unsealed"))}</code><br>'
            f'Evidence manifest SHA-256: <code>{cell(evaluation.get("manifest_sha256", "legacy-unmanifested"))}</code></p>'
            '<p class="warning">This is a deterministic format, safety, and evidence-consistency screen. '
            'It is not factual, customer, production, or revenue verification.</p>'
            f"{incomplete_html}<h3>Failed gates</h3>{failed_html}{conflict_html}"
        )
    else:
        quality_html = '<p class="warning">This report has not been evaluated.</p>'

    if detail["report"]:
        report_html = f'<pre class="report">{cell(detail["report"])}</pre>'
    elif detail["report_error"]:
        report_html = f'<p class="fail">Report unavailable: {cell(detail["report_error"])}</p>'
    else:
        report_html = '<p class="muted">No report has been written.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mission {cell(job[0])}</title><style>
:root {{ color-scheme:dark; font-family:Inter,system-ui,sans-serif; background:#0b1020; color:#e8ecf4; }}
body {{ max-width:1100px; margin:0 auto; padding:28px 20px 60px; }} a,code {{ color:#8bd5ff; }}
.meta,.warning,.muted {{ color:#9aa7bd; }} .outcome {{ font-size:20px; font-weight:800; }}
.pass {{ color:#87e6a8; }} .fail {{ color:#ff9b9b; }} .warning,.conflict {{ padding:12px; border:1px solid #67582b; background:#2b2615; border-radius:9px; }}
section {{ margin-top:26px; }} table {{ width:100%; border-collapse:collapse; background:#121a2d; }}
th,td {{ padding:10px 12px; border-bottom:1px solid #26324a; text-align:left; vertical-align:top; }}
.report {{ white-space:pre-wrap; overflow-wrap:anywhere; padding:18px; background:#121a2d; border:1px solid #26324a; border-radius:10px; line-height:1.5; }}
.quote {{ white-space:pre-wrap; overflow-wrap:anywhere; max-width:520px; margin:0; font:12px/1.4 ui-monospace,monospace; }}
</style></head><body><p><a href="/">← Dashboard</a></p>
<h1>Mission <code>{cell(job[0])}</code></h1>
<p class="meta">Report state: {cell(job[2])} · Project: {cell(job[6] or 'unscoped')} · Created UTC: {cell(job[3])}</p>
<p>{cell(job[1])}</p>
<section><h2>Automated acceptance</h2>{quality_html}</section>
<section><h2>Report</h2><p class="meta">Local output: <code>{cell(job[4] or '-')}</code><br>Sealed SHA-256: <code>{cell(job[8] or 'legacy-unsealed')}</code></p>{report_html}</section>
<section><h2>Frozen evidence</h2><p class="meta">Manifest SHA-256: <code>{cell(job[9] or 'legacy-unmanifested')}</code></p><table><thead><tr><th>Evidence ID</th><th>Source</th><th>Lines</th><th>Exact captured excerpt</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>
<section><h2>Evaluation history</h2><table><thead><tr><th>UTC</th><th>Outcome</th><th>Score</th><th>Evaluator</th><th>Report SHA-256</th><th>Manifest SHA-256</th></tr></thead><tbody>{history_rows}</tbody></table></section>
<section><h2>Assignments</h2><table><thead><tr><th>#</th><th>Role</th><th>Status</th><th>Deliverable</th></tr></thead><tbody>{assignment_rows}</tbody></table></section>
<section><h2>Audit events</h2><table><thead><tr><th>UTC</th><th>Event</th><th>Detail</th></tr></thead><tbody>{event_rows}</tbody></table></section>
</body></html>"""


def create_dashboard_server(
    company: Company, port: int = 0, service_token: str | None = None,
    service_instance_id: str | None = None,
) -> ThreadingHTTPServer:
    if port < 0 or port > 65535:
        raise ValueError("Dashboard port must be between 0 and 65535")
    if service_instance_id is not None and re.fullmatch(
        r"[0-9a-f]{32}", service_instance_id,
    ) is None:
        raise ValueError("Service instance ID must be 32 lowercase hexadecimal characters")
    worker = LocalQueueWorker(company) if service_token else None
    build_identity = runtime_build_identity()
    runtime_identity = runtime_model_identity(company)
    company_identity = company.company_identity()

    class Handler(BaseHTTPRequestHandler):
        def _local_authorities(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _valid_local_host(self) -> bool:
            return self.headers.get("Host", "").lower() in self._local_authorities()

        def _valid_local_origin(self) -> bool:
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin.lower() in {f"http://{authority}" for authority in self._local_authorities()}

        def _reject(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _read_form(self) -> dict[str, str]:
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("Form content type required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid content length") from exc
            if length < 1 or length > MAX_FORM_BYTES:
                raise OverflowError("Form body is empty or too large")
            try:
                parsed = parse_qs(
                    self.rfile.read(length).decode("utf-8", errors="strict"),
                    keep_blank_values=True,
                    max_num_fields=20,
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("Invalid form data") from exc
            return {key: values[0] for key, values in parsed.items()}

        def _redirect(self, notice: str) -> None:
            self.send_response(303)
            self.send_header("Location", "/?notice=" + quote(notice, safe=""))
            self._send_security_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._valid_local_host():
                self._reject(421, "Local Host header required")
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                notice = parse_qs(parsed.query, max_num_fields=4).get("notice", [""])[0]
                body = render_dashboard(
                    company, service_token, notice, worker, build_identity,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif parsed.path == "/__service/health.json" and service_instance_id is not None:
                body = json.dumps(
                    {
                        "status": "ready", "pid": os.getpid(),
                        "service_instance_id": service_instance_id,
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif parsed.path == "/build-status.json" or (
                parsed.path == "/health.json" and parsed.query == "view=build-status"
            ):
                body = json.dumps(
                    build_status_snapshot(
                        company, worker, build_identity, runtime_identity, company_identity,
                    )
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif parsed.path == "/health.json":
                body = json.dumps(
                    health_endpoint_snapshot(
                        company, worker, build_identity, company_identity,
                        service_instance_id,
                    )
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif match := re.fullmatch(r"/datasets/([0-9a-f]{12})", parsed.path):
                try:
                    body = render_dataset_quality_detail(
                        company, match.group(1),
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                except ValueError:
                    body = b"Dataset not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
            elif match := re.fullmatch(r"/missions/([0-9a-f]{12})", parsed.path):
                try:
                    body = render_mission_detail(company, match.group(1)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                except ValueError:
                    body = b"Mission not found"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
            else:
                body = b"Not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._valid_local_host():
                self._reject(421, "Local Host header required")
                return
            if not self._valid_local_origin():
                self._reject(403, "Cross-site mutation refused")
                return
            if (
                self.path == "/__service/stop" and service_token
                and hmac.compare_digest(self.headers.get("X-Service-Token", ""), service_token)
                and (
                    service_instance_id is None
                    or hmac.compare_digest(
                        self.headers.get("X-Service-Instance", ""), service_instance_id,
                    )
                )
            ):
                shutdown_reserved = bool(worker and worker.reserve_shutdown())
                if worker and not shutdown_reserved:
                    self.send_error(409, "A local mission is running; shutdown refused")
                    return
                try:
                    body = b"Stopping"
                    self.send_response(202)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                except BaseException:
                    if worker and shutdown_reserved:
                        worker.cancel_shutdown()
                    raise
                return
            if self.path in {
                "/queue/preview-team", "/queue/enqueue", "/queue/cancel",
                "/queue/reset", "/queue/run-next", "/jobs/quality",
            } and service_token:
                try:
                    fields = self._read_form()
                except OverflowError as exc:
                    self.send_error(413, str(exc))
                    return
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                if not hmac.compare_digest(fields.get("service_token", ""), service_token):
                    self.send_error(403, "Invalid local service token")
                    return
                try:
                    if self.path == "/queue/preview-team":
                        preview = company.routing_preview(
                            fields.get("objective", ""),
                            fields.get("playbook") or None,
                        )
                        roles = ", ".join(str(role) for role in preview["roles"])
                        routing = str(preview["routing"]).replace("_", " ")
                        gate = preview["owner_gate"]
                        categories = gate["categories"] if isinstance(gate, dict) else []
                        gate_notice = (
                            " Owner gate required before execution: "
                            + ", ".join(str(value).replace("_", " ") for value in categories)
                            + "."
                            if categories else
                            " No sensitive-action category was detected by the wording screen."
                        )
                        notice = (
                            f"Team preview only ({routing}): {roles}."
                            f"{gate_notice} No model was called, no state changed, and no mission was queued."
                        )
                        body = render_dashboard(
                            company, service_token, notice, worker, build_identity,
                            draft_fields=fields,
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self._send_security_headers()
                        self.end_headers()
                        self.wfile.write(body)
                    elif self.path == "/queue/enqueue":
                        priority = int(fields.get("priority", "50"))
                        queue_id = company.enqueue(
                            fields.get("objective", ""),
                            fields.get("project") or None,
                            playbook=fields.get("playbook") or None,
                            priority=priority,
                            source="dashboard",
                        )
                        self._redirect(f"Queued mission {queue_id}; nothing was executed.")
                    elif self.path == "/queue/cancel":
                        queue_id = fields.get("queue_id", "")
                        company.cancel_queue_item(queue_id, source="dashboard")
                        self._redirect(f"Cancelled queued mission {queue_id}.")
                    elif self.path == "/queue/reset":
                        queue_id = fields.get("queue_id", "")
                        company.reset_queue_item(queue_id, source="dashboard")
                        self._redirect(f"Reset failed mission {queue_id}; it is queued again.")
                    elif self.path == "/queue/run-next":
                        if worker is None:
                            raise RuntimeError("Local queue worker is unavailable")
                        queue_id = fields.get("queue_id", "")
                        worker.start(queue_id)
                        self._redirect(f"Started reviewed local mission {queue_id}.")
                    else:
                        job_id = fields.get("job_id", "")
                        evaluation = company.evaluate_job(job_id)
                        outcome = "passed" if evaluation["passed"] else "failed"
                        self._redirect(
                            f"Rechecked job {job_id}: quality {outcome} at {evaluation['score']}/100."
                        )
                except (TypeError, ValueError) as exc:
                    self.send_error(400, str(exc))
                except RuntimeError as exc:
                    self.send_error(409, str(exc))
                return
            self.send_error(405, "Dashboard is read-only")

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_dashboard(company: Company, port: int = 8765) -> None:
    if port < 1 or port > 65535:
        raise ValueError("Dashboard port must be between 1 and 65535")
    server = create_dashboard_server(
        company, port, os.getenv("LOCAL_COMPANY_SERVICE_TOKEN"),
        os.getenv("LOCAL_COMPANY_SERVICE_INSTANCE_ID"),
    )
    print(f"Local dashboard: http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
