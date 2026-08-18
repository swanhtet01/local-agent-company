# Local Runtime Acceptance Record

> **Historical record — superseded in part. Read this first.**
>
> This file is preserved unchanged as the acceptance evidence captured on the
> dates below. It is deliberately not rewritten, because an evidence record
> that gets edited after the fact is not evidence. Three things have since
> moved, so do not read the measurements below as current:
>
> - **The validated model no longer exists in this project.** `qwen3.5:0.8b`
>   was the model measured here. `src/local_company/model_policy.py` now
>   allowlists only `llama3.2:1b` and `llama3.2:3b` and would *reject* the
>   model named below. Every throughput number here was produced by a model
>   the code will no longer load.
> - **The test count has moved on**, from the 259 recorded here to roughly 490
>   at the time of writing. Run `python scripts/run_tests.py` for the real
>   figure rather than quoting any document.
> - **Every measurement here is single-machine**, taken on the founder's ROG
>   Ally. Nothing in this file has been reproduced on a second machine, and
>   the Linux legs of CI have never run.
>
> A fresh acceptance pass is required before any of these numbers are used in a
> customer-facing claim.

Date: 2026-07-26  
Current source verification: 2026-07-28
Machine: AMD Ryzen Z1 Extreme, approximately 11.7 GB shared memory  
Ollama: 0.32.4  
Validated model: `qwen3.5:0.8b` (no longer permitted by `model_policy.py`; see the note above)

## Evidence

- Ollama executable discovered at `C:\Users\thesw\AppData\Local\Programs\Ollama\ollama.exe` even though the current shell PATH was stale.
- Ollama localhost API reached with proxy bypass and reported the installed model.
- Direct cold health generation returned exactly `LOCAL MODEL READY` in 40.94 seconds.
- Warm 128-token benchmark completed in 6.27 seconds at 42.58 output tokens/second; model load was 3.73 seconds.
- Real project mission `f81ba29b42c2` completed three specialist assignments plus executive synthesis in 20.6 seconds.
- Real mission stage rates were 41.02 to 43.01 output tokens/second with 128-token caps.
- Real queued playbook mission `5efc64ed2f6c` became job `3a2b6ad4f49a`, completed in 25.3 seconds with 96-token caps, and passed all deterministic quality gates.
- Recurring schedule `b927e2e9fce4` materialized exactly one queue item `7f346752b8e2` and advanced its next due time by one day without executing it.
- Audit export SHA-256 `b5edcb1aad656c1965d04b84582989c19e713cefbc90c2a2e938ff4a73750d03` matched an independent local `Get-FileHash` calculation.
- Health telemetry reported zero active jobs, one queued mission, zero pending approvals, two reports, and approximately 40.4 GB free disk.
- Report `outputs\d8b2fac7ec0f\f81ba29b42c2.md` contains the project, team plan, three role outputs, executive synthesis, local source record, and owner gate.
- Dashboard integration binds to `127.0.0.1`, returns its health snapshot, and rejects POST with HTTP 405.
- The 259-test warning-strict offline regression suite covers routing, knowledge isolation, normalized sensitive-action gating, approvals, idempotent stale job/queue recovery, durable report-intent migration and recovery across both sides of file replacement, transient sharing-violation preservation, Windows write-through replacement, superseded-intent lease fencing, single-pass token-bound queued evaluation, late-evaluation rejection without audit side effects, metadata-only pending-completion visibility including reused jobs, post-seal evaluation recovery, fresh deterministic stale-queue reconciliation instead of an unbound cache decision, stale-evaluation race rejection, exact reviewed-item claiming, stale-page conflict refusal, database-wide execution-slot isolation, worker-start claim cleanup, durable pre-inference queue-to-job linkage, revocable execution leases and late-result rejection, thread-local model metrics, linked no-work queue reuse, ambiguous-claim protection, resume, concurrency, synthesis, queue priority/scheduling, playbooks, recurring materialization, source-limitation gates, frozen evidence manifests, stale/forged evidence rejection, evidence-ID claim binding, atomic report sealing, tamper/path detection, append-only evaluations, safe model-aware reuse, mutation-free current-evaluator previews on disposable clones, preview input-race refusal, stored-versus-current aggregate recovery, current-pass review without queue mutation, malformed aggregate-preview refusal, bounded preview CLI/dashboard/HTTP disclosure, exact retry-lineage supersession proofs, successor-bound mutation enforcement, stable aggregate retired-proof review, current-versus-historical proof separation, legacy and malformed retirement-event visibility, unrelated-history isolation, unreadable-newer-event corruption precedence, stale-supersession visibility, transaction-time report and source race refusal, supersession race refusal, reversible proof-audited retirement without evidence deletion, audit-v3 integrity, atomic service state/startup locking, health telemetry, report escaping, Host/origin rejection, dashboard HTTP behavior, allow-rooted XLSX profiling, aggregate data-quality contracts, bounded health output, stable pathless operator briefs, database-race refusal, sanitized operator-brief HTTP conflicts, explainable whole-word team routing, five guarded business departments, fixed-playbook previews, sensitive-category previewing, draft preservation, and zero-state/zero-model preview guarantees.
- The versioned team preview emits only code-owned roles, purposes, matched signals, routing metadata, and owner-gate categories. Focused CLI and HTTP tests prove it does not echo the objective in JSON, initialize a new store, mutate an existing database, create a queue item or approval, start work, or call a model.

## Operational defaults

- One mission at a time.
- Context window: 4096 tokens by default; increase it only for one measured mission.
- Maximum generated tokens per role: 512; use 128 for quick work.
- Model keep-alive: 30 seconds after the final call.
- Identical direct missions reuse the recent evidence-bound report for 24 hours; changed retrieved evidence creates a new job.
- Sensitive actions remain record-only approval requests with no executor.

## Known limits

- The 0.8B bootstrap model is fast but has limited reasoning depth; important work requires owner review.
- The preferred `qwen3.5:4b` quality model is not installed. Its remote 3.4 GB download repeatedly reset; partial cache was preserved.
- Source retrieval is deterministic term overlap rather than semantic embeddings.
- Automatic team routing is deterministic normalized whole-word and phrase matching rather than semantic classification. It exposes its selected and omitted departments for owner review; fixed playbooks remain available when exact coverage matters.
- A direct `dashboard` process is intentionally read-only. The detached service uses a local bearer for mutations and additionally rejects non-loopback Host authorities and cross-site mutation origins.
- Automated scores are format, safety, and conservative evidence-consistency checks. They are not complete factual verification; important claims still require owner review and stronger evidence manifests.
- Historical reports are deliberately not backfilled with seals because their current bytes cannot prove their original bytes. Re-run them to produce a current sealed artifact.
- Filesystem replacement and SQLite commits cannot be one physical transaction. The durable finalization journal makes every surviving, exact-token report recoverable without another model call; missing files can be recreated from committed bytes. Any path, content, digest, or lease mismatch remains unregistered and fails closed for inspection or a new auditable retry.
- No external connector, browser, payment, credential, publishing, deployment, or destructive tool exists.

This evidence proves local execution and workflow behavior on this machine. It does not prove unattended business outcomes or authorize external actions.
