# Operator Runbook

## Daily loop

1. Check runtime: `.\local-company.cmd doctor`; use `benchmark --num-predict 128` after model or runtime changes.
2. Run `health`, then `schedules tick` to materialize due recurring work without executing it.
3. Select or create a project, add only its relevant source files, then run `knowledge audit --project "PROJECT"`. If registered indexed text changed, use `knowledge refresh --project "PROJECT"`; the command preflights every source twice and either atomically refreshes the changed index records or changes none.
4. Preview one concrete objective with `route "OBJECTIVE" [--playbook NAME]` or the dashboard's **Preview team (no model)** button. Review selected and omitted departments plus any owner-gate categories; previewing starts no work and preserves the dashboard draft. Then queue it with a measurable outcome, project, playbook, priority, and optional schedule. Use the localhost service form or `queue add`; both record work without running it.
5. Review the exact ID, priority, due time, project, and objective shown above the dashboard run button, then run that mission. If the queue changed, refresh and review again. For CLI use, pass `queue run-next --queue-id REVIEWED_QUEUE_ID`; the database-wide execution slot protects local memory and leaves losing concurrent attempts queued.
6. Open the mission ID in the dashboard; compare its report and evidence-manifest SHA-256 values; inspect every frozen excerpt, failed gate, and source conflict; then read the Markdown report and verify its assumptions yourself. Verified wording must cite the displayed `[EVIDENCE:id]`, but the citation remains evidence for owner review rather than proof of a business outcome. Use **Recheck** after quality-rule changes; each run is appended to evaluation history and a prior score is not permanent proof. Use **Retry** only after the failed constraint or model problem is corrected.
7. Record sensitive proposed actions in the approval inbox.
8. Periodically write an audit export to an approved local or removable destination and verify its SHA-256 manifest.
9. Perform approved real-world actions yourself until a separately audited executor exists.

The default 4K context and 30-second model keep-alive are the Ally-safe profile. Increase either only for one measured mission, then return to the defaults so an idle local team does not reserve shared memory.

Repeated direct runs reuse a prior 24-hour report only when its inputs, stable model identity/configuration, current evaluator pass, and sealed bytes all match. Legacy, failed, changed, moved, or uncacheable work runs fresh. Use `retry JOB_ID` deliberately when the prior result—not the inputs—needs another model attempt.

## One-shot runtime guard and Windows cutover

Use the runtime guard only to converge the local Ollama listener and detached dashboard around an existing validated store. It never runs missions, advances schedules, pulls models, invokes service shutdown, or kills an existing recorded process:

```powershell
cd C:\Users\YOUR-NAME\Projects\local-agent-company
python .\scripts\runtime_guard.py `
  --home "C:\Users\YOUR-NAME\Projects\supermega-local-company-state" `
  --ollama-executable "C:\Users\YOUR-NAME\AppData\Local\Programs\Ollama\ollama.exe" `
  --ollama-sha256 "REPLACE-WITH-VERIFIED-64-CHAR-LOWERCASE-SHA256" `
  --record-result
```

Replace the example paths with absolute fixed-local-drive paths for this machine. Exit `0` means Ollama, the configured model, the checked build, live-build identity, store/runtime attestations, idle work state, worker availability, and exact service identity are ready. Exit `1` names a determinate manual action. Exit `2` means the runtime is indeterminate; the only possible cleanup mutation is reaping the exact Ollama child launched by that guard run. Exit `3` means the arguments are invalid. Inspect the single JSON object's `action`, `blockers`, and `changes`; do not infer success merely because a process exists. Then run `python .\scripts\check_readiness.py --home "COMPANY-HOME" --model qwen3.5:0.8b` independently before accepting work.

With `--record-result`, the exact bounded stdout object is committed atomically to the fixed `<validated-company-home>/runtime-guard-last.json` file before the guard lock is released. A requested write failure returns exit `2` with `result_journal_write_failed`. Invalid-store, invalid-argument, busy-lock, and invalid-lock invocations do not replace an older file. POSIX creates the temporary record as mode `0600`; Windows inherits the validated company home's ACL, which must remain current-user controlled. Treat the file as a possibly stale observation only: check its modification time and Task Scheduler's current result, then rerun authoritative readiness before accepting any mission.

Automatic recovery is deliberately narrow. The guard launches Ollama only when two fixed `127.0.0.1:11434` probes and a socket check confirm no listener. It does not launch on a timeout, malformed response, HTTP error, missing digest pin, digest mismatch, pre-launch executable failure, or missing model. A supplied `--ollama-sha256` must be an independently reviewed 64-character lowercase digest and requires the explicit executable path. A healthy unpinned invocation may observe the already-running runtime; when absence is confirmed and no higher-priority store, build, listener, or service condition applies, a missing pin returns `configure_ollama_executable_pin` before executable lookup. Unpinned recovery is unavailable. Hashing is lazy, bounded, and immediately precedes launch; on Windows the verified executable remains open with write/delete sharing denied through process creation. On POSIX, a pathname change in the final launch gap is handled by reaping only the newly owned child and reporting any cleanup failure; the local service is not started. The pin proves only equality to the reviewed local bytes, not publisher authenticity or the identity of DLLs, models, or configuration. After an approved Ollama update, verify the new binary deliberately and update the task pin before expecting automatic recovery. It starts the `127.0.0.1:8765` service only for a confirmed absent, stale, safely stopped, or safely failed state. A legacy PID, matching but unreachable process, endpoint mismatch, configuration mismatch, or unavailable identity requires inspection and remains untouched.

The Windows task is an operator cutover procedure; it is not registered or claimed live by this documentation:

1. Pause source edits, validate and commit the stamped build, run the guard manually with the final absolute paths, and require exit `0` followed by readiness exit `0`.
2. In Task Scheduler, create one task owned by the current user with **Run only when user is logged on** and the least-privilege run level. Never select `SYSTEM`, another account, or **Run with highest privileges**.
3. Use an **At log on** trigger with a short startup delay and repeat it every five minutes indefinitely. Enable **Run task as soon as possible after a scheduled start is missed** and set the multiple-instance rule to **Do not start a new instance**.
4. Set the action to an absolute `python.exe`, pass the absolute `runtime_guard.py`, `--home`, and `--ollama-executable` paths plus the reviewed `--ollama-sha256` value and `--record-result`, and set **Start in** to the repository root. Put no credentials, service tokens, or other secrets in the task or arguments. Set a bounded execution limit of about three minutes. Keep the default breakaway-required launch unless a controlled task probe on this exact machine proves both Scheduler error 5 and post-task descendant persistence; only after that proof add `--allow-windows-job-inheritance`.
5. Run the task once from Task Scheduler. Require a successful last-run result, then rerun the guard and readiness manually to confirm the same store, fixed endpoints, installed model, and live service.
6. If this replaces separate Ollama or dashboard logon launchers, disable those older launchers only after the new task passes the prior step. Keep one automatic lifecycle mechanism; the guard's own lock is a second defense, not a reason to keep duplicate launchers.

The five-minute task checks lifecycle only. Queue review and mission execution remain explicit owner actions in the daily loop.

## Read-only scheduled-runtime supervisor

Run the supervisor after task installation or a deliberate runtime update, and before relying on automatic recovery. This is the exact accepted local profile for this machine:

```powershell
cd C:\Users\thesw\Projects\local-agent-company
python .\scripts\check_runtime_supervisor.py `
  --home "C:\Users\thesw\Projects\supermega-local-company-state" `
  --task-name "SuperMega Local Runtime Guard" `
  --python-executable "C:\Users\thesw\AppData\Local\Python\pythoncore-3.14-64\python.exe" `
  --ollama-executable "C:\Users\thesw\AppData\Local\Programs\Ollama\ollama.exe" `
  --ollama-sha256 "9648169dfef645752ff8b25fded65d57e4b519fda9b0c9710a938af025cec2a1" `
  --model qwen3.5:0.8b `
  --allow-windows-job-inheritance
```

The supervisor reads but never mutates Task Scheduler, the guard journal, the company store, services, processes, queues, or schedules. It never runs a mission, calls a model, or pulls a model. It requires the sealed current-user `InteractiveToken`/least-privilege task, exact action and runtime profile (`8765`, `4096`, `2048`, `30s`, `15` seconds, reviewed pin, inheritance switch, and `--record-result`), a successful correlated `runtime-guard-last.json`, equality between checked disk and live build, equality between the task pin and the bounded stable Ollama executable hash, and a fresh authoritative readiness result. Its JSON omits the supplied paths and digest.

Exit `0` is the only ready result. Exit `1` names a determinate action; a disabled task, a current queued/running task, a nonzero latest result, or stale evidence is not ready. Exit `2` means task, journal, build, pin, runtime, or snapshot evidence is unavailable, malformed, inconsistent, or repeatedly changed during checking. Exit `3` means usage is invalid or the supervisor itself failed internally. Follow the returned `action` and `blockers`, correct only the named local condition, and rerun the command.

The fixed timing envelope is: five-minute (`300`-second) interval, three-minute (`180`-second) execution limit, `120` seconds of dispatch grace, and `2` seconds of clock-skew tolerance. A ready latest run and journal must be no older than `420` seconds. The bounded canonical journal must be `1` to `2048` bytes and its timestamp must fall from two seconds before the latest task start through `210` seconds after it. Missed-run count, author text, task description, idle defaults, and the exact time-trigger start timestamp are not readiness gates; the supervisor validates the operational and security fields without treating benign Task Scheduler serialization differences as failures.

## Good objectives

- `Create a 14-day inventory improvement plan with daily checks and a maximum budget of 300.`
- `Compare three positioning options using the imported customer interview notes.`
- `Design and test a local Python tool that reconciles these two CSV formats.`

Avoid vague instructions such as `run everything`. The coordinator can recruit specialists more accurately when the outcome, evidence, deadline, and constraints are explicit.

## Incident handling

- Ollama unavailable: run `doctor`, confirm Ollama is running, and confirm the requested model has been pulled.
- Runtime guard reports `install_configured_model`: pull that exact model manually, then rerun the guard. The guard never downloads it.
- Runtime guard reports an Ollama inspection action: do not launch another copy automatically. A listener existed or the fixed-loopback response was ambiguous; inspect Ollama locally and rerun the guard.
- Runtime guard reports a service inspection or migration action: do not delete service state or force a restart. Follow the service identity incidents below, then rerun the guard and readiness checks.
- Runtime guard reports `runtime_guard_busy`: wait for the current one-shot check. The lock file may remain after exit and is not itself evidence that a guard is running.
- Runtime guard reports `result_journal_write_failed`: the lifecycle result was not durably recorded as requested. Inspect the fixed journal path without deleting the company store, correct only the unsafe or unavailable record target, then rerun the guard and authoritative readiness.
- Model call fails: the job becomes `failed`; inspect it with `show`, fix the local runtime, then use `retry`.
- Process stops: use `recover --stale-minutes 60`. It interrupts stale jobs, revokes their execution leases, and marks stale queue claims failed without a model rerun; late responses are discarded. Then use `resume JOB_ID` deliberately to issue a new lease and preserve completed assignments, or reset the failed queue item after review.
- Another mission is running: wait for it. Only use `recover` when its heartbeat is genuinely stale.
- Stale queue claim: inspect its preserved job ID and error in `queue list`. Recovery never guesses an unlinked legacy claim while another job is live and is safe to repeat.
- Bad source: run `knowledge audit --project "PROJECT"`. Use `knowledge refresh --project "PROJECT"` only when every registered source is present and trusted; missing, unsafe, unstable, or over-limit sources fail before index mutation. Use `knowledge add PATH --project "PROJECT"` only to register a new source deliberately.
- Source leaked across projects: project runs retrieve only sources explicitly attached to that project; inspect with `projects show`.
- Wrong approval: decisions are immutable in the CLI. Create a new request documenting the correction instead of erasing history.
- Dashboard intake rejected: confirm `service status` is live. A direct `dashboard` process is intentionally read-only; authenticated intake is available only through `service start`.
- Dashboard worker is already running: wait for its status to leave `running`. Duplicate launches and service shutdown fail closed while a mission is active.
- Mission completion pending: inspect the dashboard banner or `health` metadata. `report_finalization_pending` means exact report bytes are still journaled; `evaluation_pending` means the report is sealed but its deterministic result is not committed. Wait if the worker is active. If the state remains after the heartbeat is genuinely stale, use `recover --stale-minutes 60`; recovery does not rerun the model.
- Service lifecycle change already in progress: wait for the current start or stop command. The persistent OS-backed lock is released automatically when its owning command exits; its small lock file remains in place and is not evidence of a stuck command.
- Service status is `legacy_unverified`: stop the still-running service with the prior build before switching source, then start it once with the current build. The current build will not attach a new identity to an arbitrary legacy PID.
- Service identity is indeterminate or the endpoint mismatches: do not force shutdown through the recorded port. Inspect the recorded process locally, close the owning application if appropriate, and rerun `service status`; no shutdown secret is sent while identity is uncertain.
- Dashboard page rejected with HTTP 421: use the exact local address printed by the service (`http://127.0.0.1:PORT` or `http://localhost:PORT`); arbitrary Host headers are refused.

## Safety invariant

Reports are proposals, not proof that work happened. Approval records are decisions, not execution. Keep credentials out of objectives and knowledge files.
