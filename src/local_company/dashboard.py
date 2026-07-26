from __future__ import annotations

import html
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from .core import Company, PLAYBOOKS


MAX_FORM_BYTES = 16 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def start(self) -> None:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A local queue mission is already running")
        try:
            if not self.company.has_due_queue_item():
                raise ValueError("No queued mission is due")
            self._set_state(status="running", started_at=_utc_now())
            threading.Thread(target=self._run, name="local-company-worker", daemon=False).start()
        except Exception:
            self._run_lock.release()
            raise

    def _run(self) -> None:
        try:
            queue_id, job_id, output, passed = self.company.run_next_queue_item()
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
        except Exception as exc:
            self._set_state(
                status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=_utc_now()
            )
        finally:
            self._run_lock.release()


def dashboard_snapshot(
    company: Company, worker: LocalQueueWorker | None = None
) -> dict[str, object]:
    return {
        "projects": company.projects(),
        "jobs": company.jobs(),
        "queue": company.queue_items(),
        "schedules": company.schedules(),
        "datasets": company.dataset_items(),
        "evaluations": company.recent_evaluations(),
        "pending_approvals": company.action_requests("pending"),
        "due_queue_item": company.has_due_queue_item(),
        "worker": worker.snapshot() if worker else {"status": "disabled"},
        "health": company.health_snapshot(),
    }


def render_dashboard(
    company: Company, service_token: str | None = None, notice: str = "",
    worker: LocalQueueWorker | None = None,
) -> str:
    snapshot = dashboard_snapshot(company, worker)

    def cell(value: object) -> str:
        return html.escape(str(value))

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
        f'<tr><td>{mission_link(row[0])}</td><td><span class="status {cell(row[1])}">{cell(row[1])}</span></td>'
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
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}</td><td>{cell(row[2])}</td>"
        f"<td>{cell(row[3])}</td><td>{cell(row[4] or '-')}</td><td>{cell(row[6])}</td>"
        f"<td>{mission_link(row[7])}</td><td>{cell(row[8] or '-')}</td><td>{queue_action(row)}</td></tr>"
        for row in snapshot["queue"][:30]
    ) or '<tr><td colspan="9" class="empty">Queue is empty</td></tr>'
    schedule_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}</td>"
        f"<td>{'enabled' if row[2] else 'disabled'}</td><td>{cell(row[3])}d</td><td>{cell(row[4])}</td></tr>"
        for row in snapshot["schedules"]
    ) or '<tr><td colspan="5" class="empty">No schedules</td></tr>'
    dataset_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[1])}</td><td>{cell(row[2])}</td>"
        f"<td>{cell(row[3])}</td><td>{cell(row[4])}</td><td>{cell(row[5])}</td></tr>"
        for row in snapshot["datasets"][:30]
    ) or '<tr><td colspan="6" class="empty">No datasets</td></tr>'
    approval_rows = "".join(
        f"<tr><td><code>{cell(row[0])}</code></td><td>{cell(row[2])}</td><td>{cell(row[4])}</td></tr>"
        for row in snapshot["pending_approvals"]
    ) or '<tr><td colspan="3" class="empty">No pending approvals</td></tr>'

    project_options = "".join(
        f'<option value="{cell(row[0])}">{cell(row[1])}</option>' for row in snapshot["projects"]
    )
    playbook_options = "".join(
        f'<option value="{cell(name)}">{cell(name)} - {cell(item["description"])}</option>'
        for name, item in PLAYBOOKS.items()
    )
    intake = ""
    if service_token:
        worker_status = str(snapshot["worker"].get("status", "idle"))
        run_disabled = " disabled" if worker_status == "running" or not snapshot["due_queue_item"] else ""
        run_hint = (
            "A local mission is running." if worker_status == "running" else
            "No queued mission is due." if not snapshot["due_queue_item"] else
            "Starts exactly one due mission with the configured local Ollama model."
        )
        intake = f"""
<section class="intake"><h2>Queue a SuperMega task</h2>
<p class="hint">This records work only. It does not run a model or perform an external action.</p>
<form method="post" action="/queue/enqueue">
<input type="hidden" name="service_token" value="{cell(service_token)}">
<label>Objective<textarea name="objective" maxlength="4000" required placeholder="Describe the result, evidence, constraints, and owner gates."></textarea></label>
<div class="form-grid">
<label>Project<select name="project"><option value="">Unscoped</option>{project_options}</select></label>
<label>Playbook<select name="playbook"><option value="">Automatic routing</option>{playbook_options}</select></label>
<label>Priority (0-100)<input name="priority" type="number" min="0" max="100" value="50" required></label>
</div><button type="submit">Add to queue</button></form>
<h3>Run one local mission</h3><p class="hint">{cell(run_hint)}</p>
<form method="post" action="/queue/run-next">
<input type="hidden" name="service_token" value="{cell(service_token)}">
<button type="submit"{run_disabled}>Run next locally</button></form></section>"""
    notice_html = f'<p class="notice">{cell(notice)}</p>' if notice else ""

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
.complete {{ color:#87e6a8; }} .failed,.interrupted {{ color:#ff9b9b; }} .running {{ color:#ffd479; }}
.empty {{ color:#6f7d95; text-align:center; }} .gate {{ color:#ffd479; }}
.notice {{ padding:12px 14px; background:#143520; border:1px solid #27693d; border-radius:9px; color:#a7f3bd; }}
.hint {{ color:#9aa7bd; margin-top:-6px; }} label {{ display:block; color:#cbd5e1; font-size:13px; }}
textarea,input,select {{ box-sizing:border-box; width:100%; margin-top:6px; padding:10px; color:#e8ecf4; background:#0b1020; border:1px solid #3a4864; border-radius:8px; }}
textarea {{ min-height:108px; resize:vertical; }} .form-grid {{ display:grid; grid-template-columns:2fr 2fr 1fr; gap:12px; margin:12px 0; }}
button {{ padding:9px 14px; color:#07111f; background:#8bd5ff; border:0; border-radius:8px; font-weight:700; cursor:pointer; }}
button.danger {{ padding:6px 10px; color:#ffd7d7; background:#5a1f2a; }} form.inline {{ display:inline; }}
button:disabled {{ cursor:not-allowed; opacity:.45; }}
@media(max-width:760px) {{ .grid {{ grid-template-columns:1fr; }} th:nth-child(4),td:nth-child(4) {{ display:none; }} }}
</style></head><body>
<h1>Local Agent Company</h1><p class="sub">Owner-controlled task intake &middot; localhost only <a class="refresh" href="/">Refresh</a><br>Scores are automated format, safety, and evidence-consistency checks—not factual or production verification.</p>
{notice_html}{intake}
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
<section><h2>Project datasets</h2><table><thead><tr><th>ID</th><th>Project</th><th>Format</th><th>Rows</th><th>Columns</th><th>Source</th></tr></thead><tbody>{dataset_rows}</tbody></table></section>
<section><h2>Recent missions</h2><table><thead><tr><th>ID</th><th>Report state</th><th>Automated checks</th><th>Objective</th><th>Created UTC</th><th>Action</th></tr></thead><tbody>{job_rows}</tbody></table></section>
<section><h2>Projects</h2><table><thead><tr><th>ID</th><th>Name</th><th>Missions</th></tr></thead><tbody>{project_rows}</tbody></table></section>
<section><h2>Approval inbox</h2><table><thead><tr><th>ID</th><th>Category</th><th>Proposed action</th></tr></thead><tbody>{approval_rows}</tbody></table></section>
</body></html>"""


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

    if evaluation:
        outcome = "PASSED" if evaluation["passed"] else "FAILED"
        failed = [name.replace("_", " ") for name, passed in evaluation["checks"].items() if not passed]
        failed_html = (
            "<ul>" + "".join(f"<li>{cell(name)}</li>" for name in failed) + "</ul>"
            if failed else "<p>No automated gates failed.</p>"
        )
        conflicts = evaluation.get("source_conflicts", [])
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
            '<p class="warning">This is a deterministic format, safety, and evidence-consistency screen. '
            'It is not factual, customer, production, or revenue verification.</p>'
            f"<h3>Failed gates</h3>{failed_html}{conflict_html}"
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
</style></head><body><p><a href="/">← Dashboard</a></p>
<h1>Mission <code>{cell(job[0])}</code></h1>
<p class="meta">Report state: {cell(job[2])} · Project: {cell(job[6] or 'unscoped')} · Created UTC: {cell(job[3])}</p>
<p>{cell(job[1])}</p>
<section><h2>Automated acceptance</h2>{quality_html}</section>
<section><h2>Report</h2><p class="meta">Local output: <code>{cell(job[4] or '-')}</code></p>{report_html}</section>
<section><h2>Assignments</h2><table><thead><tr><th>#</th><th>Role</th><th>Status</th><th>Deliverable</th></tr></thead><tbody>{assignment_rows}</tbody></table></section>
<section><h2>Audit events</h2><table><thead><tr><th>UTC</th><th>Event</th><th>Detail</th></tr></thead><tbody>{event_rows}</tbody></table></section>
</body></html>"""


def create_dashboard_server(
    company: Company, port: int = 0, service_token: str | None = None
) -> ThreadingHTTPServer:
    if port < 0 or port > 65535:
        raise ValueError("Dashboard port must be between 0 and 65535")
    worker = LocalQueueWorker(company) if service_token else None

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
                body = render_dashboard(company, service_token, notice, worker).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif parsed.path == "/health.json":
                body = json.dumps(
                    {"status": "ready", "pid": os.getpid(), **dashboard_snapshot(company, worker)}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
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
            ):
                if worker and worker.snapshot().get("status") == "running":
                    self.send_error(409, "A local mission is running; shutdown refused")
                    return
                body = b"Stopping"
                self.send_response(202)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path in {
                "/queue/enqueue", "/queue/cancel", "/queue/reset", "/queue/run-next", "/jobs/quality"
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
                    if self.path == "/queue/enqueue":
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
                        worker.start()
                        self._redirect("Started one local queued mission.")
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
    server = create_dashboard_server(company, port, os.getenv("LOCAL_COMPANY_SERVICE_TOKEN"))
    print(f"Local dashboard: http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
