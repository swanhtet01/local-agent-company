# Operator Runbook

## Daily loop

1. Check runtime: `.\local-company.cmd doctor`; use `benchmark --num-predict 128` after model or runtime changes.
2. Run `health`, then `schedules tick` to materialize due recurring work without executing it.
3. Select or create a project, then add or refresh only its relevant source files.
4. Queue one concrete objective with a measurable outcome, project, playbook, priority, and optional schedule. Use the localhost service form or `queue add`; both record work without running it.
5. Run one due mission with the dashboard's **Run next locally** button or `queue run-next`; the single-mission lease protects local memory.
6. Open the mission ID in the dashboard; compare its report and evidence-manifest SHA-256 values; inspect every frozen excerpt, failed gate, and source conflict; then read the Markdown report and verify its assumptions yourself. Verified wording must cite the displayed `[EVIDENCE:id]`, but the citation remains evidence for owner review rather than proof of a business outcome. Use **Recheck** after quality-rule changes; each run is appended to evaluation history and a prior score is not permanent proof. Use **Retry** only after the failed constraint or model problem is corrected.
7. Record sensitive proposed actions in the approval inbox.
8. Periodically write an audit export to an approved local or removable destination and verify its SHA-256 manifest.
9. Perform approved real-world actions yourself until a separately audited executor exists.

The default 4K context and 30-second model keep-alive are the Ally-safe profile. Increase either only for one measured mission, then return to the defaults so an idle local team does not reserve shared memory.

Repeated direct runs reuse a prior 24-hour report only when its inputs, stable model identity/configuration, current evaluator pass, and sealed bytes all match. Legacy, failed, changed, moved, or uncacheable work runs fresh. Use `retry JOB_ID` deliberately when the prior result—not the inputs—needs another model attempt.

## Good objectives

- `Create a 14-day inventory improvement plan with daily checks and a maximum budget of 300.`
- `Compare three positioning options using the imported customer interview notes.`
- `Design and test a local Python tool that reconciles these two CSV formats.`

Avoid vague instructions such as `run everything`. The coordinator can recruit specialists more accurately when the outcome, evidence, deadline, and constraints are explicit.

## Incident handling

- Ollama unavailable: run `doctor`, confirm Ollama is running, and confirm the requested model has been pulled.
- Model call fails: the job becomes `failed`; inspect it with `show`, fix the local runtime, then use `retry`.
- Process stops: use `recover --stale-minutes 60`. It interrupts stale jobs, revokes their execution leases, and marks stale queue claims failed without a model rerun; late responses are discarded. Then use `resume JOB_ID` deliberately to issue a new lease and preserve completed assignments, or reset the failed queue item after review.
- Another mission is running: wait for it. Only use `recover` when its heartbeat is genuinely stale.
- Stale queue claim: inspect its preserved job ID and error in `queue list`. Recovery never guesses an unlinked legacy claim while another job is live and is safe to repeat.
- Bad source: refresh it with `knowledge add`; the path is updated by content hash.
- Source leaked across projects: project runs retrieve only sources explicitly attached to that project; inspect with `projects show`.
- Wrong approval: decisions are immutable in the CLI. Create a new request documenting the correction instead of erasing history.
- Dashboard intake rejected: confirm `service status` is live. A direct `dashboard` process is intentionally read-only; authenticated intake is available only through `service start`.
- Dashboard worker is already running: wait for its status to leave `running`. Duplicate launches and service shutdown fail closed while a mission is active.
- Service startup already in progress: wait for the recorded PID. The exclusive local startup lock prevents a second launcher from replacing its PID/token state; a stale lock is removed only after its owner PID is gone.
- Dashboard page rejected with HTTP 421: use the exact local address printed by the service (`http://127.0.0.1:PORT` or `http://localhost:PORT`); arbitrary Host headers are refused.

## Safety invariant

Reports are proposals, not proof that work happened. Approval records are decisions, not execution. Keep credentials out of objectives and knowledge files.
