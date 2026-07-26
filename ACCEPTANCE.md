# Local Runtime Acceptance Record

Date: 2026-07-26  
Machine: AMD Ryzen Z1 Extreme, approximately 11.7 GB shared memory  
Ollama: 0.32.4  
Validated model: `qwen3.5:0.8b`

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
- The 35-test offline regression suite covers routing, knowledge isolation, approvals, recovery, resume, concurrency, synthesis, queue priority/scheduling, playbooks, recurring materialization, source-limitation quality gates, audit integrity, health telemetry, report escaping, Host/origin rejection, and dashboard HTTP behavior.

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
- A direct `dashboard` process is intentionally read-only. The detached service uses a local bearer for mutations and additionally rejects non-loopback Host authorities and cross-site mutation origins.
- Automated scores are format, safety, and conservative evidence-consistency checks. They are not complete factual verification; important claims still require owner review and stronger evidence manifests.
- No external connector, browser, payment, credential, publishing, deployment, or destructive tool exists.

This evidence proves local execution and workflow behavior on this machine. It does not prove unattended business outcomes or authorize external actions.
