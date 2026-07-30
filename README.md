# Local Agent Company

A zero-subscription, local-first AI company for general work—not only development. It recruits role-based specialists, lets later specialists build on earlier work, retrieves local reference files, keeps an audit trail in SQLite, and writes reviewable Markdown reports.

Measured machine-specific results and current limitations are recorded in `ACCEPTANCE.md`.

This is an owner-controlled foundation. It plans and drafts locally; it does not autonomously message people, spend money, use credentials, browse, publish, deploy, or delete data.

## Verify locally

Run the complete dependency-free suite from any working directory with the
repository-anchored runner:

```powershell
python C:\Users\thesw\Projects\local-agent-company\scripts\run_tests.py
```

The runner supplies the repository and `src` import roots, treats warnings as
errors, and does not require an activated environment or a `PYTHONPATH` value.
It does not initialize or mutate company state, start a service, or call a
model. Use `--pattern test_readiness.py` only for a focused development check;
the command without a pattern is the release suite. Passing runs are concise by
default; add `--verbose` when individual test names are needed for diagnosis.
An unmatched `--pattern` fails closed with exit 2 instead of treating zero
discovered tests as a successful verification.

## Start immediately

No package download is required:

```powershell
cd C:\Users\thesw\Projects\local-agent-company
.\local-company.cmd init
.\local-company.cmd service start --port 8765
python .\scripts\check_readiness.py --model qwen3.5:0.8b
.\local-company.cmd run "Design a 30-day launch plan for a local tyre shop"
```

The default provider is now the installed local Ollama runtime. Use `--provider mock` only when you intentionally want a fast workflow simulation without real model reasoning.
The readiness command is the authoritative release gate for accepting a new local mission. `doctor` checks only the local Python/Ollama/model dependency: exit 0 means that dependency is ready, exit 1 names a known setup action, and exit 2 is an invalid or indeterminate diagnostic. It does not check build identity, company work state, or the queue worker.
The company home no longer depends on the current directory. An explicit `--home` wins, then `LOCAL_COMPANY_HOME`, then the fixed per-user `~\.local-company` default. A relative environment value is anchored under the user home; root-relative, drive-relative, and parent-traversal environment values are rejected. A relative explicit `--home` remains relative to the invoking directory for compatibility.

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
$env:LOCAL_COMPANY_KEEP_ALIVE = "0s"
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
.\local-company.cmd knowledge audit --project "Yangon Tyre"
.\local-company.cmd knowledge refresh --project "Yangon Tyre"
.\local-company.cmd knowledge search "customer pricing" --project "Yangon Tyre"
```

Directory reads are non-recursive unless `--recursive` is explicitly supplied, capped at 100 supported files by default, and skip hidden paths, symlinks, dependency folders, unsupported types, and files over 2 MB. Relevant excerpts are included in agent prompts and source paths are recorded in the final report. Imported text is treated as reference material, not executable instructions.

`knowledge audit` is a bounded, read-only freshness check over at most 64 registered sources. Its JSON reports only source IDs, statuses, and current byte counts; it withholds paths, content, and digests, starts no work, and calls no model. `knowledge refresh` requires exactly one project and rereads all of that project's registered sources twice before opening one database transaction. A missing, unavailable, unsafe, over-limit, or changing source refuses the entire refresh. A successful refresh updates only changed index records and never writes to source files.

Every model-backed direct, queued, or retry execution now checks the complete retrieval scope before reading indexed excerpts and again under the job transaction. A known stale queue item remains queued and unclaimed; no job or model call starts. Unprojected missions check every globally searchable source. Resume additionally requires its frozen evidence manifest to still match the safe current source and index; when it does not, use `retry` to create a new job with new frozen evidence. The error is pathless and directs the operator to audit and deliberately refresh or re-add the affected source. A source changing after the first scan still fails before job creation, cache reuse, or fresh model work.

## Run teams

Preview the automatically selected team before spending any local inference
time or creating company state:

```powershell
.\local-company.cmd route "Improve supplier controls, inventory flow, and customer retention metrics"
.\local-company.cmd route "Compare supplier options" --playbook procurement-review
.\local-company.cmd preflight "Compare supplier options" --project "Yangon Tyre" --playbook procurement-review
```

The versioned JSON preview shows the fixed chair and quality roles, at most four
automatically selected specialists, each exact matched signal, any matched
departments omitted by the automatic cap, and explicit `model_called`,
`state_mutated`, and `work_started` false effects. A named playbook previews its
exact fixed team instead of applying the automatic cap. The preview also reports
sensitive-action categories that require an owner gate before execution.
Matching uses normalized whole words and phrases, so text such as `approval`
cannot accidentally match the engineering signal `app`. The preview does not
call Ollama, initialize a store, queue work, approve a gate, or authorize an
action.

`preflight` adds the selected initialized store's aggregate evidence readiness
to that deterministic routing result. Its pathless
`local-company.mission-preflight.v1` JSON withholds the objective, project name,
source IDs, paths, contents, and digests. It reports only the opaque project ID,
team roles, owner-gate categories, fixed blocker tokens, aggregate source/status
counts, queueing eligibility, model-execution readiness, and false effect flags.
Known drift blocks model readiness before queue creation while still permitting
record-only queueing; owner-gated wording skips evidence reads because it cannot
reach model execution. The private dashboard's **Preview team (no model)** step
shows the same readiness result and keeps the draft objective, project,
playbook, and priority ready for review. Neither preflight starts work or calls
Ollama, and `/health.json` remains unchanged.

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
sales           product   engineering legal-risk analytics
customer-success people-ops procurement strategy quality
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
.\local-company.cmd queue preflight --queue-id REVIEWED_QUEUE_ID
.\local-company.cmd queue retry-preflight FAILED_QUEUE_ID
.\local-company.cmd queue run-next --queue-id REVIEWED_QUEUE_ID --num-predict 128
```

Priorities range from 0 to 100. `--scheduled-at` accepts an ISO-8601 timestamp; values without a timezone are treated as UTC. There is no autonomous daemon: queue execution is an explicit local operator command. Before running, `queue preflight` returns the pathless `local-company.queue-preflight.v1` contract for the canonical next-due mission. It confirms the exact reviewed ID, selected team, current knowledge counts, owner-gate categories, and whether submission and model execution are ready without exposing the objective, source paths, source contents, or digests. It starts no model, job, or queue claim and does not mutate state. A `blocked` result must be corrected and reviewed again; `owner_gate_required` remains eligible only to create a local approval request and never starts model work.

Before resetting a `failed`, `quality_failed`, or `superseded` item, `queue retry-preflight QUEUE_ID` returns `local-company.queue-retry-preflight.v1` over the still-failed record. It proves reset eligibility, current knowledge status, routed team, retry policy, owner gates, execution-slot readiness, and exactly one next action without exposing the objective or evidence. The CLI and dashboard retry-preflight page never reset or claim the queue, create a job, call a model, mutate state, or start work. Only after reviewing a `ready` result should an operator use `queue reset QUEUE_ID`; the ordinary next-due preflight still runs again before execution.

`--queue-id` is optional in the CLI for compatibility, but when supplied it fails without mutation unless that exact reviewed ID is still the canonical highest-priority due mission. The dashboard displays the same preflight, disables model submission on blockers, and leaves the owner-review request available for gated work. It always supplies the displayed ID and claims it synchronously before acknowledging the POST. A changed queue order returns a conflict and runs nothing. Sensitive objectives become `needs_approval` and are not executed. When execution begins, the queue claim and job ID are linked in the same database transaction before the first model response and share a revocable execution lease, so interrupted work remains attributable and a superseded worker cannot persist a late result.

Available playbooks are `business-launch`, `decision-brief`,
`operations-improvement`, `product-build`, `growth-plan`,
`customer-retention`, `people-operations`, `procurement-review`, and
`metrics-review`, and `strategy-review`. The new departments remain planning and review functions:
customer-success never contacts customers, people operations does not make
employment decisions, procurement never places orders or commits spend, and
analytics never invents missing data; strategy never presents forecasts or
assumptions as facts.

Before inference, every new mission freezes a versioned evidence manifest containing each retrieved source ID/path/hash, exact excerpt, character and line span, evidence ID, capture time, and a canonical manifest SHA-256. Prompts expose only those frozen `[EVIDENCE:id]` references. For objectives that request verified facts from imported evidence, a filename alone is insufficient: verification wording must carry a valid frozen evidence ID in the same sentence. A changed source, forged manifest, altered quote, invalid digest, or missing evidence citation fails closed. The dashboard shows the exact frozen excerpts for owner inspection.

Project retrieval uses deterministic term frequency by default. When two current files carry different authority (for example, a sealed release handoff superseding an older operating note), set an explicit bounded project-scoped score adjustment with `knowledge authority SOURCE_ID --project PROJECT --level -100..100`. Explicitly named filenames still rank first; otherwise retrieval ranks by term score plus the authority adjustment, so strongly relevant evidence can still outrank a preferred but weak match. Level `0` removes the override. Search previews and frozen evidence expose the term, authority, and combined rank scores, and this command never calls a model or changes a source file.

After every completed mission, deterministic gates verify that all assignments completed and that the report contains a substantive synthesis, team plan, owner gate, intact report seal, and valid evidence manifest binding. A new queued mission is evaluated exactly once after sealing and before its token-fenced queue claim is finalized; a safely reused report is rechecked without repeating model inference. When the objective names them, the evaluator also enforces specialist and synthesis word limits, explicit verified-fact/assumption separation, requested operating concepts, exact source-filename and evidence-ID citations, naturally stopped model output, labeled percentage claims, and absence of unsupported deployment or scheduling claims. A source-limitation gate rejects completion claims whose specific terms overlap retrieved evidence that says the capability is pending, unavailable, incomplete, or not ready. These are conservative provenance and contradiction screens—not general fact verification. For objectives that explicitly require matching supplied evidence IDs, synthesis is schema-first and fail-closed: code owns verified facts, filename/ID pairs, labels, numbering, word budget, and the required ending; malformed, unsafe, over-budget, or unavailable structured output cannot fall back to a free-text editor. Ollama specialist drafts in this strict mode use a request-local ceiling of 512 generated tokens (or the lower configured limit), without changing the service-wide model setting or the structured executive budget. A cap hit is stored only as an explicit incomplete-output sentinel. Specialist drafts remain visibly unverified, whole source limitations are preserved atomically, and unsafe proposed actions are withheld. Legacy free-text missions retain section-aware compaction for overlong output:

The deterministic renderer also owns the success criterion, so model-generated past-tense
completion wording cannot define acceptance.
Strict proposal fields reject serialized-object fragments and source/evidence metadata keys;
malformed task text receives the same single local retry and then fails closed.
Task templates must begin with a listed action verb. In strict grounded runs, failure-mode prose is
code-owned and deterministically rendered instead of accepted from the model. The evaluator keeps a
conservative semantic check for legacy and non-structured reports.
Retry and final-rejection audit events record only stable code-owned validation codes, never
the rejected model payload. Persisted model metrics use a fixed key, type, range, and enum
allowlist.

```powershell
.\local-company.cmd quality JOB_ID
.\local-company.cmd quality JOB_ID --summary
.\local-company.cmd quality JOB_ID --preview
.\local-company.cmd quality --failed
.\local-company.cmd queue supersession-list
.\local-company.cmd queue supersession-preview QUEUE_ID
.\local-company.cmd queue supersede QUEUE_ID --successor-job RETRY_JOB_ID --reason "Exact current retry accepted; preserve the historical failure for audit."
```

A report is written through a same-directory atomic replacement and sealed with SHA-256 before evaluation. Every recheck appends a versioned evaluation-history record; the latest result remains the dashboard projection. The evaluator reads and scans the exact sealed report bytes, so edits, moved paths, symlinks, and appended action claims fail closed. A queue item becomes `quality_failed` instead of `complete` when these gates fail. `quality JOB_ID --summary` is a bounded, pathless read of the latest stored result: it lists exact failed-check tokens, grouped repair actions, source-conflict count, queue linkage, and explicit zero-effect flags without appending an evaluation, calling a model, or changing queue state. `quality JOB_ID --preview` uses `local-company.quality-recheck-preview.v2` to run the exact current evaluator against a disposable clone of the sealed report and database, compare it with the stored result, then delete the clone. It reports evaluator, outcome, score, and failed-gate changes without changing the real database, report, queue, or history and refuses the result if the database, report, or frozen source files change during observation. Invalid sealed-report or frozen-evidence integrity directs the operator to preserve history and retry with current evidence; content-only failures retain repair-before-retry guidance. `quality --failed` uses `local-company.quality-recovery-list.v4` for every active failure in priority order. Each item keeps its stored result as history, runs an individually race-checked disposable current preview, exposes score and gate deltas, and derives displayed repair actions and common-gate counts from the current evaluator rather than stale checks. The aggregate double-snapshots the full database and fails closed on malformed preview output or crossing store changes. A current pass remains review-only and does not change the queue or append an evaluation. Use **Recheck** or `quality JOB_ID` only when you intend to append the reviewed result. After correcting the cause, use the dashboard **Retry** control or `queue reset QUEUE_ID`.

`queue supersession-preview QUEUE_ID` and the dashboard **Supersession proof** link perform a second mutation-free check before any failure can be retired. They require a completed descendant in the original retry lineage with the exact objective and project, current passing evaluation history, unchanged report seal, and valid report-bound evidence manifest. The output withholds objectives, reports, paths, source text, and claims and includes a SHA-256 binding for the selected successor. `queue supersession-list` and the dashboard **Retired failure proofs** page use `local-company.quality-supersession-list.v2`, repeat that current proof twice for every bounded retired record, and fail closed if the database, report, manifest, source, or audit inputs cross during observation. They separately classify the original retirement event as `input_fingerprint_bound`, `successor_proof_bound`, `legacy_reason_only`, or `malformed` without exposing its reason. Unrelated-job history is excluded before the bounded scan, while an unreadable or invalidly routed record for the same failed job invalidates the audit binding instead of being skipped in favor of older proof. Thus a currently valid successor does not disguise weak or corrupted historical evidence, and a stale or legacy record remains preserved but receives explicit review attention. `queue supersede` requires the exact eligible successor through `--successor-job`; schema `local-company.queue-supersede.v2` revalidates lineage, evaluation, seals, manifest, and proof inside the mutation transaction, then double-fingerprints the report and manifest source files around final validation. A crossing filesystem write rolls back the queue change and appends no supersession event. A reason alone cannot hide an unresolved failure. Supersession changes only the queue lifecycle and appends its proof plus the opaque input fingerprint to the audit event; it deletes no report, evaluation, queue record, or history and remains reversible with `queue reset`. Queued items can be stopped with `queue cancel QUEUE_ID`.

## Project operator brief

Start each local review with one deterministic project summary:

```powershell
.\local-company.cmd brief --project "PROJECT"
```

The `local-company.operator-brief.v1` result combines project-scoped knowledge
freshness, mission and completion state, owner approvals, due schedules, failed
quality gates, and aggregate dataset-quality signals. It emits an ordered
attention queue and exactly one `next_action`. The brief withholds objectives,
project names, reports, paths, evidence text, claims, and model output; it calls
no model and changes no database, queue item, schedule, or work state. It scans
registered source freshness and double-snapshots every database input, returning
an error instead of mixing states when either side changes during observation.

Each project row on the localhost dashboard has a **Brief** link to the same
bounded view. That page is no-store, accepts only one exact project ID, returns
404 for malformed or extra routes, and returns a sanitized conflict response if
the local state changes while it is rendered.

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

CLI health reports local disk, database, reports, Ollama model storage, active work, queue depth, approvals, and metadata-only pending-completion phases. The dashboard's `/health.json` uses schema `local-company.health.v1` and exposes only its PID and optional listener instance, startup-cached build and opaque company-store identities, allowlisted resource/work counters, Ollama reachability and installed-model count, and worker status. It excludes projects, objectives, queue rows, schedules, evaluations, approval descriptions, dataset records, model names, pending item IDs, paths, secrets, and worker output. Its size therefore does not grow with business-record or worker-output history. The embedded release identity contains package version, build ID, and one release-validated SHA-256. That digest uses versioned framing over every operational package Python file plus the fixed `check_live_build.py`, `check_readiness.py`, `check_runtime_supervisor.py`, `runtime_guard.py`, and `stamp_build_manifest.py` lifecycle scripts, all labeled by canonical project-relative paths. Only the generated `src/local_company/build_info.py` manifest is excluded to avoid self-reference. `/build-status.json` and the compatibility URL `/health.json?view=build-status` return only that identity, the service PID, direct SQLite work counters, worker status, a bounded startup runtime attestation, and an opaque company-store identity. The store object contains only schema `local-company.store.v1` and a random UUID; no home path, database filename, token, or environment value is exposed. Runtime build identity performs no filesystem, environment, Git reads, or process launches; unavailable `git_commit` and `source_dirty` values deliberately remain `null` instead of being guessed. `pending_report_finalizations` and `pending_evaluations` identify durable work between report preparation, sealing, evaluation, and queue reconciliation without exposing lease tokens, paths, report bytes, or item IDs. Export writes a version-3 timestamped JSON audit plus a `.sha256` manifest, including report seals, evidence-manifest indexes, and append-only evaluation history. It includes source paths and content hashes but deliberately excludes imported source bodies and frozen source quotes.

Verify the embedded manifest before tests or release. After changing any covered package file or fixed lifecycle script, refresh it with a new, explicit, monotonic build ID and check it again. A missing, linked, escaped, oversized, or unstable required script fails closed; unrelated docs, tests, and helper scripts are not silently added to the trust boundary. These commands touch no company state or service process:

```powershell
python .\scripts\stamp_build_manifest.py --check
python .\scripts\stamp_build_manifest.py --write --build-id local-build-YYYYMMDD.N
python .\scripts\stamp_build_manifest.py --check
```

This identity detects checked on-disk drift; it is not a signature and cannot establish authenticity against an actor able to rewrite both the verifier and manifest. Future lifecycle scripts must be added explicitly to the code-owned allowlist and released with a new build ID.

Run stamping only while local source edits are paused; its double scan detects ordinary concurrent changes but is not a lock against a malicious filesystem writer. A write failure with `replacement_committed: true` means the atomic replacement reached disk before a later check failed: run `--check`, inspect the result, and do not restart the service until it passes. The live dashboard caches its embedded identity at process startup, so a validated and committed build becomes live only after a separate deliberate service restart.

After commit or restart, compare that checked disk manifest with the fixed loopback build-status view. The checker is read-only, caps the response at 64 KiB, bypasses proxies and redirects, validates provenance fields, reports exact mismatches, and refuses to recommend an immediate restart while a job, queued/running mission, approval, report finalization, evaluation, or worker transition is active. It blocks downgrade advice when disk is older or a build ID conflicts. During the first upgrade, the query-form URL accepts a pre-compact build's legacy health response only when it fits the 64 KiB client cap; current builds answer it with the compact snapshot:

```powershell
python .\scripts\check_live_build.py
```

Use the composed readiness gate before accepting a new local mission:

```powershell
python .\scripts\check_readiness.py --model qwen3.5:0.8b
```

It returns exit 0 only when the selected local company home has a valid identity matching the live service, the disk manifest is valid, the live build exactly matches it, local work is idle, the queue worker is enabled, the service is startup-attested to the requested Ollama model on the fixed loopback endpoint, Ollama is reachable, and that exact model is installed. The gate rereads the selected identity immediately before a ready result and fails closed if it changed during the check. Use the same optional `--home` value as the CLI. On Windows, readiness accepts only an existing normal local-drive path and rejects UNC, mapped-remote, device, and reparse-point paths before opening SQLite. A valid store mismatch returns the neutral `align_company_home` action and never switches, initializes, or relaunches either store. Exit 1 reports a bounded, known local action; exit 2 means state, live, or dependency status is unavailable or malformed; exit 3 means the disk manifest or checker itself is indeterminate. Follow the JSON `action`, then rerun the gate. The Ollama tags probe does not generate text, so `generation_tested` remains false; use `benchmark` separately when an inference proof is required.

The opaque store ID identifies database lineage, not an exact path, freshness, or authenticity. Copies, hardlinks, and restored backups retain it, and a local writer could spoof it. The live process pins its first valid identity and keeps each identity check in the same SQLite transaction as the corresponding read or mutation, so a different valid store cannot be used silently during that operation.

## One-shot local runtime guard

`runtime_guard.py` is a bounded, fail-closed lifecycle check for an already initialized company store. It is not a mission scheduler or worker. It never runs a queued mission, materializes a schedule, calls a model, pulls a model, invokes service shutdown, or kills an existing recorded process. Run it manually with explicit local paths before using it from an operating-system task:

```powershell
python .\scripts\runtime_guard.py `
  --home "C:\Users\YOUR-NAME\Projects\supermega-local-company-state" `
  --ollama-executable "C:\Users\YOUR-NAME\AppData\Local\Programs\Ollama\ollama.exe" `
  --ollama-sha256 "REPLACE-WITH-VERIFIED-64-CHAR-LOWERCASE-SHA256" `
  --record-result
```

The guard validates the selected store read-only, pins its opaque identity, checks the stamped operational build, and uses a private singleton lock. All HTTP checks remain on loopback: Ollama is fixed at `http://127.0.0.1:11434/api/tags`, and automatic dashboard starts are fixed at `http://127.0.0.1:8765`; the service port argument accepts only `8765`. Proxies and redirects are disabled. Ollama is launched at most once, with `OLLAMA_HOST=127.0.0.1:11434`, only after two strict probes plus a socket check confirm that the listener is absent. On Windows systems that silently time out closed loopback connects, two native IPv4-and-IPv6 TCP listener-table snapshots must both confirm that no listener owns the port. A reset, unexpected HTTP response, malformed or oversized inventory, unsafe executable, unavailable listener table, or other ambiguous result is never treated as permission to launch. If the newly owned Ollama child cannot establish a valid listener, the guard reaps only that exact child and reports an inspection action; it never kills by PID or process name. A reachable Ollama instance without the configured model also starts nothing: install that model deliberately with `ollama pull`, then rerun the guard.

For automatic recovery, record an independently reviewed Ollama executable's SHA-256 and pass it with `--ollama-sha256`; the pin requires an explicit `--ollama-executable` and accepts exactly 64 lowercase hexadecimal characters. Omitting the pin remains valid for a healthy observation, but if Ollama absence is confirmed and no higher-priority store, build, listener, or service state needs attention, the guard returns exit `1`, `ollama_executable_pin_required`, and `configure_ollama_executable_pin` before executable lookup or process creation. There is no unpinned automatic-launch fallback. With a pin, the guard hashes the bounded executable only after absence is confirmed and immediately before launch. On Windows it keeps a read-only handle that denies write and delete sharing through process creation, so an updater or replacement cannot swap the checked bytes before launch. A digest mismatch or pre-launch verification failure returns exit `2` with a fixed inspection blocker before any child or service is started. On POSIX, a replacement in the final pathname-to-process gap is detected after process creation; the guard immediately reaps only that owned child, starts no service, and reports cleanup failure explicitly if reaping cannot be confirmed. This is a local byte pin, not publisher-authenticity proof, and it does not attest DLLs, models, or configuration. Deliberately verify and update the stored pin after an approved Ollama upgrade; do not calculate it afresh inside every scheduled invocation.

The local service is started only when its checked state is `not_configured`, safely stopped or failed with an absent or mismatched process, stale with an absent process, or `stale_pid_reused` with a mismatched process. A live matching service is left alone. Legacy, unreachable, endpoint-mismatched, configuration-mismatched, PID-ambiguous, or malformed state fails closed; the guard never tries to repair it by stopping or replacing a process. Before any ready result, the guard also runs the composed readiness gate for live-build equality, live company/runtime attestation, idle work state, worker availability, Ollama, and the model. It then rechecks the exact service process, store, build, and Ollama snapshots once more.

The command writes one compact `local-company.runtime-guard.v1` JSON object. Exit `0` means the complete runtime was confirmed ready, with `status` showing whether recovery occurred. Exit `1` reports a bounded operator action such as installing the configured model, starting a component manually, or waiting for the other guard instance. Exit `2` is an indeterminate store, build, listener, service identity, result-journal, or internal result; exit `3` means invalid arguments. Output includes fixed `missions_started: 0` and `models_pulled: 0` counters and does not expose service tokens or process fingerprints. Follow the returned `action`, rerun the guard, and still use `check_readiness.py` as the authoritative gate before accepting a mission.

`--record-result` atomically records the exact stdout JSON bytes at the fixed `<validated-company-home>/runtime-guard-last.json` path while the guard lock remains held. It accepts no output path, never creates or selects a company home, and fails nonzero if the requested commit is unsafe or unavailable. Invalid-store, invalid-argument, busy-lock, and invalid-lock runs leave an existing record untouched. On POSIX the temporary record is created with mode `0600`; on Windows the record inherits the validated company home's ACL, so keep that home inside a current-user-controlled directory and do not treat the journal as stronger than the store's own access boundary. The file is only the last completed result for a valid, uncontended store and can legitimately be stale; its age and the current task result must be checked, and it is never an authorization signal or a substitute for authoritative readiness.

The repository does not register a Windows scheduled task by itself. The intended cutover is a current-user, least-privilege task that invokes this one-shot command every five minutes with absolute Python, script, company-home, and Ollama-executable paths, the reviewed `--ollama-sha256` pin, and `--record-result`. Configure **Run only when user is logged on**, **Do not start a new instance**, **Start the task as soon as possible after a scheduled start is missed**, and a bounded execution limit. Do not run it as `SYSTEM` or an administrator: Ollama models and the company store belong to the selected user. The default launch requires `CREATE_BREAKAWAY_FROM_JOB`. Use `--allow-windows-job-inheritance` only on a machine where an isolated scheduled-task probe has both demonstrated error 5 for breakaway and confirmed that a detached inherited child remains alive after the action exits; the fallback is never attempted for another error. Register and test that task only after a manual exit-0 run; see `OPERATOR.md` for the cutover checklist. The task maintains local listeners only and does not make queue execution autonomous.

## Read-only runtime supervisor

After the Windows task is installed, use one read-only command to validate the complete local supervision chain. The example below is the explicit accepted profile for this machine; change a path or digest only after deliberately revalidating the replacement:

```powershell
python .\scripts\check_runtime_supervisor.py `
  --home "C:\Users\thesw\Projects\supermega-local-company-state" `
  --task-name "SuperMega Local Runtime Guard" `
  --python-executable "C:\Users\thesw\AppData\Local\Python\pythoncore-3.14-64\python.exe" `
  --ollama-executable "C:\Users\thesw\AppData\Local\Programs\Ollama\ollama.exe" `
  --ollama-sha256 "9648169dfef645752ff8b25fded65d57e4b519fda9b0c9710a938af025cec2a1" `
  --model qwen3.5:0.8b `
  --allow-windows-job-inheritance
```

`check_runtime_supervisor.py` validates the sealed current-user, least-privilege task definition and action, its latest Task Scheduler result, the fixed atomic guard journal, the reviewed Ollama executable pin, the checked disk and live build, and the authoritative readiness result. The task name defaults to `SuperMega Local Runtime Guard`; supplying `--task-name` accepts only that exact value. The task action is required to use the explicit local values above plus the sealed Ally profile: port `8765`, context `4096`, prediction limit `768`, keep-alive `0s`, wait limit `15` seconds, and `--record-result`. The zero-second keep-alive releases model memory after every completed request; specialist roles still execute serially, up to the active focus limit.

The supervisor is observational only. It does not enable, disable, register, start, stop, or rewrite the task; change the guard journal; start or stop a service; execute or queue a mission; advance a schedule; call a model; or pull a model. It emits one bounded, sanitized JSON object and does not return the supplied paths or digest. Exit `0` means the sealed task, a successful fresh journaled run, the Ollama pin, build identity, and authoritative readiness all agree. Exit `1` reports a determinate local action, such as enabling the task, waiting for a current run, or correcting stale state. Exit `2` means the observation is unavailable, malformed, inconsistent, or changed repeatedly during the check. Exit `3` means invalid command usage or an internal supervisor failure. Follow `action` and `blockers`; never treat the journal alone as authorization to run work.

Freshness is fixed to the sealed five-minute task: a `300`-second interval, `180`-second execution limit, `120`-second dispatch grace, and `2`-second clock-skew allowance. A ready task result and journal may be at most `420` seconds old. The journal timestamp must correlate from two seconds before the latest task start through `210` seconds after it, and the canonical journal is capped at `2048` bytes. A disabled task is action-required, a running or queued task is transient action-required, and an unknown task state is indeterminate. The checker snapshots the task and journal around the build and readiness checks and retries a crossing scheduled run once instead of reporting a false component failure.

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

Open `http://127.0.0.1:8765`. The detached service binds only to localhost and shows an authenticated form for adding project-scoped tasks to the queue. The page does not auto-refresh while an objective is being drafted; use **Refresh** explicitly. Each project row opens its deterministic, pathless operator brief. The **Failed mission recovery** card opens a bounded, pathless priority view that separates stored history from current-evaluator status, score, failed gates, integrity booleans, gate deltas, and code-owned repair actions for every active failure. Current common gates and actions therefore remain useful after evaluator changes. It withholds objectives, reports, sources, evidence, and model output and performs no evaluation append, model call, queue change, or work side effect. Its per-mission **Current preview detail** links show the same comparison in isolation. Mission IDs open a local detail page containing the report, failed automated gates, source-conflict evidence, exact frozen evidence excerpts, report/manifest hashes, append-only evaluation history, assignments, and audit events. A visible **Mission completion pending** banner and row annotations distinguish durable report-finalization or evaluation phases from an ordinary failure, including after a service restart. Queued items may be cancelled before they start. Intake never runs a model or performs an external action. The separate run control names the exact next ID, priority, and objective it will claim. If priority order changes after rendering, the stale submission is refused and the operator must refresh. A database-wide execution-slot guard keeps losing concurrent queue or direct attempts untouched instead of consuming a second item.

Dataset IDs on the dashboard open a read-only aggregate quality page. The list and
detail view expose dimensions, deterministic flag categories, declared-key checks,
per-column missing/type/distinct counts, and finite numeric summaries. They do not
expose source paths, generated-brief paths, or source row values. `/health.json`
reports only the total dataset count, never dataset records. Detail rendering is capped at 200 columns and
reports the omitted count. A malformed or unsupported stored profile fails closed to
an unavailable notice instead of displaying arbitrary stored fields. These screens
do not infer business validity, required fields, date semantics, units, or freshness.

The service uses a random local secret for queue changes, local execution, and shutdown; rejects non-loopback Host authorities and cross-site mutation origins; limits form bodies; escapes stored text; adds browser security headers; and writes queue lifecycle audit events. Service state is written through flush/fsync/atomic replacement. A persistent OS-backed lifecycle lock serializes the complete start and stop operations. Each launch records an opaque OS process-birth fingerprint and a separate random listener instance ID; status requires both the exact process and a constant-size private loopback handshake to match. Shutdown atomically excludes new mission starts, disables proxies and redirects, sends its secret only after both identities match, and waits for the exact original process to exit. PID reuse, a listener collision, malformed state, or an indeterminate process query therefore cannot claim a verified live service or receive a shutdown request. The fingerprint and secret are omitted from status output. Detached service identity is currently supported on Windows and Linux; direct read-only dashboard mode remains available elsewhere. A single-worker lock prevents competing Ollama jobs, and normalized sensitive-action patterns stop common email/funds/data-wipe wording at `needs_approval` before any model call. Running `dashboard` directly, without the service secret, preserves the read-only view.

When upgrading from a service state created before `local-company.service.v2`, stop the running service with the prior build before switching source, then start it once with the current build. Current code labels an active legacy record `legacy_unverified` and refuses to guess that a PID belongs to the old service.

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

Profile an explicitly selected CSV, JSON, or standard `.xlsx` workbook without
modifying the source:

```powershell
.\local-company.cmd datasets add "C:\approved-data\sales.xlsx" `
  --project "Yangon Tyre" `
  --allow-root "C:\approved-data" `
  --sheet "Sales" `
  --key "invoice_id" `
  --required "invoice_id" `
  --required "amount" `
  --type "amount" "numeric" `
  --min "amount" "0" `
  --max "amount" "100000"
.\local-company.cmd datasets list --project "Yangon Tyre"
```

XLSX input requires `--allow-root`; the resolved workbook must stay inside that
normal local directory. The dependency-free reader rejects links, reparse
points, remote roots, path-traversing or encrypted ZIP members, unsupported
workbook types, unsafe XML declarations, oversized expansion, more than 512
profiled columns, and malformed relationships. It reads at most 10,000 data
rows from one explicitly named sheet, or the first visible sheet. Formula and
error cells are counted but ignored: formulas, macros, links, and cached formula
results are never executed or treated as data. Generated briefs contain only
statistics, not copied source rows. Profiles include missing and uniqueness
rates, exact-duplicate impact, compact numeric min/quartile/median/max/mean,
zero/negative rates, and IQR outlier counts. Repeat `--key` for a composite key
to measure declared-grain completeness and uniqueness without storing key
values.

Dataset contracts are owner-declared on each profiling command. Repeat
`--required COLUMN` to require presence and no missing rows; repeat
`--type COLUMN TYPE` to allow multiple non-missing types (`array`, `boolean`,
`integer`, `number`, `numeric`, `object`, or `string`). `numeric` accepts both
integer and number observations. `--min COLUMN VALUE` and `--max COLUMN VALUE`
set inclusive finite numeric bounds. Missing values are checked only by
`--required`; non-missing values that cannot be checked as finite numbers count
as range violations. An absent declared column is a violation. A clean check on
a source capped at 10,000 profiled rows is labeled `conforms_profiled_rows`, not
full-source conformance. Reprofiling the same source replaces its prior declared
contract with the flags supplied on that command.

Contract storage and the private dashboard contain declared column names,
types, bounds, counts, rates, and results only—never source row values. Business
keys, units, date semantics, allowed values, severity, freshness limits, and
fitness for use are never guessed. `--allow-root` is optional but available as
the same containment check for CSV and JSON; `--sheet` is XLSX-only.

All runtime data stays under `.local-company` by default:

```text
.local-company/
  company.db       SQLite jobs, plans, sources, approvals, and events
  outputs/         Markdown reports
```

Use `--home D:\somewhere` before the command to choose another state directory.

## Architecture and next boundary

The coordinator is dependency-free Python using SQLite and Ollama's localhost HTTP API. Specialists work in sequence, then an executive-chair pass turns their outputs into one decision-ready synthesis. This PC remains the authority. Future worker machines should be named nodes receiving scoped assignments; they should never inherit blanket permissions.

The read-only spreadsheet boundary is local and path-allowlisted. External connectors and action executors should come later, one narrow tool at a time, with explicit approvals and durable receipts.
