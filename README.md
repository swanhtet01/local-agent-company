# Local Agent Company

A zero-subscription, local-first AI company for general work—not only development. It recruits role-based specialists, lets later specialists build on earlier work, retrieves local reference files, keeps an audit trail in SQLite, and writes reviewable Markdown reports.

Measured machine-specific results and current limitations are recorded in `ACCEPTANCE.md`.

This is an owner-controlled foundation. It plans and drafts locally; it does not autonomously message people, spend money, use credentials, browse, publish, deploy, or delete data.

## Start immediately

No package download is required:

```powershell
cd C:\Users\thesw\Projects\local-agent-company
.\local-company.cmd init
.\local-company.cmd doctor
.\local-company.cmd run "Design a 30-day launch plan for a local tyre shop"
```

The default provider is now the installed local Ollama runtime. Use `--provider mock` only when you intentionally want a fast workflow simulation without real model reasoning.

## Use a real local model

After installing Ollama, download a model once:

```powershell
ollama pull qwen3.5:0.8b
.\local-company.cmd doctor
.\local-company.cmd benchmark --num-predict 128
.\local-company.cmd run "Build a practical plan for my objective" --model qwen3.5:0.8b
```

After the model download, inference is local and does not use a paid API. The installed 0.8B model is the reliable bootstrap model. `qwen3.5:4b` remains the planned quality upgrade for this machine's roughly 12 GB of shared memory once its larger download succeeds.

An identical direct mission reuses a report for 24 hours only when the routed team, project, retrieved evidence, evaluator version, stable model identity/configuration, latest passing evaluation, and sealed report SHA-256 all match. Uncacheable models, changed/tampered reports, and failed or legacy evaluations always run fresh. Change the objective or evidence when the work genuinely changed; use the explicit retry command when a failed result needs another attempt.

You may set defaults for the current terminal:

```powershell
$env:LOCAL_COMPANY_PROVIDER = "ollama"
$env:LOCAL_COMPANY_MODEL = "qwen3.5:0.8b"
$env:LOCAL_COMPANY_NUM_CTX = "4096"
$env:LOCAL_COMPANY_NUM_PREDICT = "512"
$env:LOCAL_COMPANY_KEEP_ALIVE = "30s"
```

## Create a project workspace

Projects keep missions and source retrieval separated:

```powershell
.\local-company.cmd projects create "Yangon Tyre" --description "Local operations and growth work"
.\local-company.cmd projects list
.\local-company.cmd projects show "Yangon Tyre"
```

## Give the company knowledge

Only the file explicitly named is imported. Supported formats are Markdown, text, CSV, JSON, YAML, Python, PowerShell, JavaScript, and TypeScript; each file is capped at 2 MB.

```powershell
.\local-company.cmd knowledge add "C:\path\to\business-notes.md" --project "Yangon Tyre"
.\local-company.cmd knowledge add-dir "C:\path\to\approved-notes" --project "Yangon Tyre"
.\local-company.cmd knowledge list --project "Yangon Tyre"
.\local-company.cmd knowledge search "customer pricing" --project "Yangon Tyre"
```

Directory reads are non-recursive unless `--recursive` is explicitly supplied, capped at 100 supported files by default, and skip hidden paths, symlinks, dependency folders, unsupported types, and files over 2 MB. Relevant excerpts are included in agent prompts and source paths are recorded in the final report. Imported text is treated as reference material, not executable instructions.

## Run teams

Automatic routing:

```powershell
.\local-company.cmd run "Find ways to improve our shop profit and customer retention" --project "Yangon Tyre"
```

Explicit team:

```powershell
.\local-company.cmd run "Plan next quarter" --roles chief-of-staff,finance,marketing,operations,quality --provider ollama
```

Available functions:

```text
chief-of-staff  research  operations  finance  marketing
sales           product   engineering legal-risk quality
```

## Reusable playbooks and mission queue

Inspect the built-in cross-functional teams:

```powershell
.\local-company.cmd playbooks list
.\local-company.cmd playbooks show operations-improvement
```

Queue work without executing it, then manually run the highest-priority mission whose scheduled time has arrived:

```powershell
.\local-company.cmd queue add "Improve our daily stock process" --project "Yangon Tyre" --playbook operations-improvement --priority 80
.\local-company.cmd queue list --status queued
.\local-company.cmd queue run-next --queue-id REVIEWED_QUEUE_ID --num-predict 128
```

Priorities range from 0 to 100. `--scheduled-at` accepts an ISO-8601 timestamp; values without a timezone are treated as UTC. There is no autonomous daemon: queue execution is an explicit local operator command. `--queue-id` is optional in the CLI for compatibility, but when supplied it fails without mutation unless that exact reviewed ID is still the canonical highest-priority due mission. The dashboard always supplies the displayed ID and claims it synchronously before acknowledging the POST. A changed queue order returns a conflict and runs nothing. Sensitive objectives become `needs_approval` and are not executed. When execution begins, the queue claim and job ID are linked in the same database transaction before the first model response and share a revocable execution lease, so interrupted work remains attributable and a superseded worker cannot persist a late result.

Available playbooks are `business-launch`, `decision-brief`, `operations-improvement`, `product-build`, and `growth-plan`.

Before inference, every new mission freezes a versioned evidence manifest containing each retrieved source ID/path/hash, exact excerpt, character and line span, evidence ID, capture time, and a canonical manifest SHA-256. Prompts expose only those frozen `[EVIDENCE:id]` references. For objectives that request verified facts from imported evidence, a filename alone is insufficient: verification wording must carry a valid frozen evidence ID in the same sentence. A changed source, forged manifest, altered quote, invalid digest, or missing evidence citation fails closed. The dashboard shows the exact frozen excerpts for owner inspection.

After every completed mission, deterministic gates verify that all assignments completed and that the report contains a substantive synthesis, team plan, owner gate, intact report seal, and valid evidence manifest binding. A new queued mission is evaluated exactly once after sealing and before its token-fenced queue claim is finalized; a safely reused report is rechecked without repeating model inference. When the objective names them, the evaluator also enforces specialist and synthesis word limits, explicit verified-fact/assumption separation, requested operating concepts, exact source-filename and evidence-ID citations, naturally stopped model output, labeled percentage claims, and absence of unsupported deployment or scheduling claims. A source-limitation gate rejects completion claims whose specific terms overlap retrieved evidence that says the capability is pending, unavailable, incomplete, or not ready. These are conservative provenance and contradiction screens—not general fact verification. For objectives that explicitly require matching supplied evidence IDs, synthesis is schema-first and fail-closed: code owns verified facts, filename/ID pairs, labels, numbering, word budget, and the required ending; malformed, unsafe, over-budget, or unavailable structured output cannot fall back to a free-text editor. Ollama specialist drafts in this strict mode use a request-local ceiling of 512 generated tokens (or the lower configured limit), without changing the service-wide model setting or the structured executive budget. A cap hit is stored only as an explicit incomplete-output sentinel. Specialist drafts remain visibly unverified, whole source limitations are preserved atomically, and unsafe proposed actions are withheld. Legacy free-text missions retain section-aware compaction for overlong output:

The deterministic renderer also owns the success criterion, so model-generated past-tense
completion wording cannot define acceptance.
Strict proposal fields reject serialized-object fragments and source/evidence metadata keys;
malformed task text receives the same single local retry and then fails closed.
Task templates must begin with an action verb, failure modes must state an explicit failure
condition, and the evaluator reapplies both semantic checks to sealed reports.

```powershell
.\local-company.cmd quality JOB_ID
```

A report is written through a same-directory atomic replacement and sealed with SHA-256 before evaluation. Every recheck appends a versioned evaluation-history record; the latest result remains the dashboard projection. The evaluator reads and scans the exact sealed report bytes, so edits, moved paths, symlinks, and appended action claims fail closed. A queue item becomes `quality_failed` instead of `complete` when these gates fail. Use the dashboard **Recheck** control or `quality JOB_ID` after evaluator improvements. After correcting the cause, use the dashboard **Retry** control or `queue reset QUEUE_ID`; queued items can be stopped with `queue cancel QUEUE_ID`.

## Recurring local schedules

Schedules do not run autonomously. They create queue items only when the operator invokes `tick`:

```powershell
.\local-company.cmd schedules create "Morning health" "Review local runtime health and identify exceptions" --every-days 1 --next-run "2026-07-27T01:00:00+00:00" --project "Acceptance Lab" --playbook operations-improvement --priority 70
.\local-company.cmd schedules list
.\local-company.cmd schedules tick
```

Each due schedule produces at most one occurrence per tick and advances beyond the current time, preventing an overdue schedule from flooding the queue. Use `schedules disable SCHEDULE_ID` or `schedules enable SCHEDULE_ID` to control future materialization.

## Health and recoverable audit export

```powershell
.\local-company.cmd health
.\local-company.cmd export "C:\path\to\approved-export-directory"
```

Health reports local disk, database, reports, Ollama model storage, active work, queue depth, approvals, and metadata-only pending-completion phases. `pending_report_finalizations`, `pending_evaluations`, and `pending_completion` identify durable work that is between report preparation, sealing, evaluation, and queue reconciliation without exposing lease tokens, paths, or report bytes. Export writes a version-3 timestamped JSON audit plus a `.sha256` manifest, including report seals, evidence-manifest indexes, and append-only evaluation history. It includes source paths and content hashes but deliberately excludes imported source bodies and frozen source quotes.

## Inspect and recover work

```powershell
.\local-company.cmd status
.\local-company.cmd show JOB_ID
.\local-company.cmd recover --stale-minutes 60
.\local-company.cmd resume JOB_ID
.\local-company.cmd retry JOB_ID --provider ollama
```

Each completed assignment is checkpointed. Before a final report filename is published, the exact report bytes, digest, paths, and execution lease are committed to a private SQLite finalization journal. Windows publication uses replace-existing plus write-through semantics; other platforms replace in the same directory and flush that directory. If a process or machine stops before or immediately after file replacement, `recover` can recreate a missing temporary file or register an exact matching final report, seal it, run the deterministic evaluator, and reconcile its queue without rerunning the model. Transient filesystem sharing errors retain the intent and leases for a later retry. A readable mismatched path, lease, temporary file, or final file fails closed as `interrupted`; suspicious artifacts are never registered or overwritten. A sealed stale job that crashed before evaluation is evaluated before its queue is reconciled. A stale queue linked to a completed job receives a fresh deterministic recheck for that recovery attempt; recovery never decides from the unbound summary cache alone. Recovery is idempotent.

For other stale-heartbeat jobs, `recover` revokes their execution leases and reconciles stale queue claims to `failed` without resuming or rerunning a model. Linked job IDs are preserved; ambiguous legacy claims are never guessed while any job is still live. Any old model response arriving after recovery is audited and discarded. Use `resume` to issue a new lease and continue ordinary interrupted work deliberately, `retry` to create a new auditable child when a report artifact failed integrity checks, or `queue reset` to make a failed queue item eligible for an explicit later run. Lease tokens and pending report bytes are excluded from portable audit exports. Only one mission may run at a time, protecting shared RAM from competing local generations.

## Local operator dashboard and task intake

```powershell
.\local-company.cmd service start --port 8765
```

Open `http://127.0.0.1:8765`. The detached service binds only to localhost and shows an authenticated form for adding project-scoped tasks to the queue. The page does not auto-refresh while an objective is being drafted; use **Refresh** explicitly. Mission IDs open a local detail page containing the report, failed automated gates, source-conflict evidence, exact frozen evidence excerpts, report/manifest hashes, append-only evaluation history, assignments, and audit events. A visible **Mission completion pending** banner and row annotations distinguish durable report-finalization or evaluation phases from an ordinary failure, including after a service restart. Queued items may be cancelled before they start. Intake never runs a model or performs an external action. The separate run control names the exact next ID, priority, and objective it will claim. If priority order changes after rendering, the stale submission is refused and the operator must refresh. A database-wide execution-slot guard keeps losing concurrent queue or direct attempts untouched instead of consuming a second item.

The service uses a random local secret for queue changes, local execution, and shutdown; rejects non-loopback Host authorities and cross-site mutation origins; limits form bodies; escapes stored text; adds browser security headers; and writes queue lifecycle audit events. Service state is written through flush/fsync/atomic replacement, and an exclusive PID-aware startup lock prevents concurrent launches from overwriting service identity. A single-worker lock prevents competing Ollama jobs, shutdown is refused while a mission is running, and normalized sensitive-action patterns stop common email/funds/data-wipe wording at `needs_approval` before any model call. The secret is omitted from health and status output. Running `dashboard` directly, without the service secret, preserves the read-only view.

## Owner approval inbox

Sensitive wording in an objective fails closed and creates a pending request. You can also record a proposed action explicitly:

```powershell
.\local-company.cmd approvals request "Send email to the approved customer list" --job JOB_ID
.\local-company.cmd approvals list --status pending
.\local-company.cmd approvals approve REQUEST_ID --note "Draft reviewed"
.\local-company.cmd approvals reject REQUEST_ID --note "Not authorized"
```

Approval is a recorded decision only. This version deliberately has no side-effect executor, so approving a request still sends, spends, publishes, deploys, and deletes nothing.

## Local data

All runtime data stays under `.local-company` by default:

```text
.local-company/
  company.db       SQLite jobs, plans, sources, approvals, and events
  outputs/         Markdown reports
```

Use `--home D:\somewhere` before the command to choose another state directory.

## Architecture and next boundary

The coordinator is dependency-free Python using SQLite and Ollama's localhost HTTP API. Specialists work in sequence, then an executive-chair pass turns their outputs into one decision-ready synthesis. This PC remains the authority. Future worker machines should be named nodes receiving scoped assignments; they should never inherit blanket permissions.

The next safe capability is a read-only file/spreadsheet tool with per-path allowlists. External connectors and action executors should come later, one narrow tool at a time, with explicit approvals and durable receipts.
