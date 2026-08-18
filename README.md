# SuperMega Local Workcell

**A local-first AI agent company that runs entirely on your own machine, and
never acts on the outside world without you.** It plans work with role-based
agent teams against a small local model, teaches and replays bounded desktop
tasks, checks websites read-only, and writes a hash-sealed receipt for
everything it does — but it does not send, spend, deploy, publish, or delete
anything by itself, ever.

The repository is named `local-agent-company`. The product is the workcell.

---

## Why this exists

Most agent frameworks assume a paid API and a network. This one assumes
neither.

- **No API costs.** Inference runs on [Ollama](https://ollama.com) on your own
  computer. A run costs electricity, not credits.
- **No data leaves the machine.** The model is local, the database is a local
  SQLite file, the dashboard binds to `127.0.0.1`, and the only outbound traffic
  is to the local model and to URLs you explicitly ask it to read.
- **Nothing runs on its own.** There is no daemon that decides to act. Every
  mission is started by a person typing a command.
- **Everything is receipted.** Reports, evidence files, and taught workflows are
  sealed with SHA-256 and recorded in an append-only audit ledger, so you can
  check afterwards what actually happened.
- **Zero third-party Python dependencies.** The whole system is the standard
  library. The only exception is the optional browser QA lane, which installs one
  pinned npm package and reuses the Edge you already have.

It is built for a low-resource Windows handheld, so it is careful about memory,
and it will refuse to start work rather than thrash your machine.

## What works, and what does not

Honesty matters more here than a good first impression. This is the current
state, not a plan.

| Capability | State | Notes |
| --- | --- | --- |
| Role-based agent teams, SQLite ledger, sealed reports | Works | 15 roles, 10 playbooks, deterministic team routing |
| Owner approval inbox | Works, and deliberately inert | Approving records a decision. Nothing executes it. |
| MCP server (25 governed actions) | Works | stdio JSON-RPC; mutations need exact confirmation strings |
| Localhost dashboard and task intake | Works | **No authentication.** Loopback only. See [Safety](#the-safety-model). |
| Read-only browser QA with evidence | Works on Windows | Needs an npm package and installed Edge; drives no clicks or forms |
| Windows teach-and-replay desktop automation | Works on Windows only | ctypes/WinAPI; not portable |
| Local dataset profiling (CSV / JSON / XLSX) | Works | Stdlib reader, read-only, path-allowlisted |
| Running on Linux (Docker / VPS) | New and unproven | See below |
| Browser QA on Linux | Does not work | Needs a substrate swap and a fresh acceptance run |
| Desktop automation on Linux or macOS | Does not work | Excluded by platform, by design |
| Any autonomous external action | Does not exist | Not "not yet" — see [Safety](#the-safety-model) |

Four more things you should know before you invest time:

1. **CI exists but has never run.** GitHub Actions workflows for the test matrix
   (Windows and Linux, Python 3.11 to 3.13) and for CodeQL are in the repository,
   but the repository has no remote yet, so not one of those jobs has ever
   executed. The Linux legs are marked advisory on purpose: **whether the suite
   passes on Linux has never been observed by anyone.** Three test modules are
   still Windows-only and will not pass cleanly there. Today the honest
   verification story is "run `python scripts/run_tests.py` yourself on Windows",
   plus [`deploy/verify_linux_port.py`](deploy/verify_linux_port.py), which you
   run by hand inside the container.
2. **Model quality is bounded by a 1-billion-parameter model.** The policy in
   [`src/local_company/model_policy.py`](src/local_company/model_policy.py)
   allows exactly `llama3.2:1b` and `llama3.2:3b`, and there is no environment
   variable that widens it. A 1B model is fast and private. It is not smart.
   Treat every generated report as a draft for a human to check.
3. **There are no proven customer deployments.** Nobody is running this in
   production for money. The recorded local measurements are in
   [`ACCEPTANCE.md`](ACCEPTANCE.md) (last updated July 2026, so parts of it are
   already stale) and [`PRODUCT.md`](PRODUCT.md). There are no benchmarks against
   other systems, no users to cite, and no testimonials.
4. **This is pre-1.0.** Only the latest commit is supported. Interfaces change.

## Quickstart

You need **Python 3.11 or newer** and, for anything model-backed,
**[Ollama](https://ollama.com)**.

```bash
# 1. A local model. The 1b model is the default and the smallest.
ollama pull llama3.2:1b

# 2. Get the code. There is nothing to pip install.
cd local-agent-company

# 3. Verify the build before you trust it.
#    No network, no model, no state created. Around 70 seconds.
python scripts/run_tests.py

# 4. Create the local store. This is the first real command.
python -m local_company.cli init

# 5. Check that the runtime is actually ready.
python scripts/check_readiness.py --model llama3.2:1b

# 6. Run one mission.
python -m local_company.cli run "Design a 30-day launch plan for a local tyre shop"
```

Launchers in the repository root wrap those commands on both platforms. On
Windows use the `.cmd` files — `.\local-company.cmd init`, `.\local-ai.cmd help`.
On Linux and macOS use the extensionless shell scripts beside them:

```bash
./local-ai brief        # friendly status: one next action, in plain English
./local-ai help
./local-company init    # the full coordinator CLI
./company-mcp           # the governed MCP server, over stdio
```

Each one prefers `.venv/bin/python` if present and otherwise falls back to
`python3`. `./local-ai brief` is the best first command: it reads the current
state, prints one recommended next action, and calls no model.

If you install the package with `pip install -e .`, the console script
`local-company` becomes available and is equivalent to
`python -m local_company.cli`. Note that the friendly `local-ai` launchpad lives
in `scripts/` and is not installed by pip — run it from a clone.

The test suite is around 490 tests, takes a minute or two, and prints the exact
count when it finishes. Run it before you file a bug.

All state lives under `~/.local-company` by default: `company.db` for the ledger
and `outputs/` for the Markdown reports. Use `--home /some/path` to put it
somewhere else.

## The safety model

This is the most distinctive thing about the project, so read it before you use
it.

**The system never sends, spends, deploys, publishes, or deletes anything
autonomously. There is deliberately no external-action executor.**

This is not a policy in a prompt, and it is not a permission flag. It is a gap in
the code. There is no SMTP client, no payment code, no deploy path, and no
API-key usage anywhere in the source tree — the browser lane actively strips
`OPENAI_API_KEY` and friends out of the environment it passes to its child
process.

How it behaves in practice:

- **Sensitive wording fails closed.** An objective that talks about sending
  email, moving money, or wiping data becomes a pending approval request before
  any model is called.
- **Approval is a record, not an execution.** `approvals approve` writes a
  decision to the ledger and stops. It sends no email, moves no money, deploys
  nothing. A human performs the real action.
- **Verification is deterministic, not generated.** A model may draft a report,
  but a model never decides that a run succeeded. Observable postconditions and
  code-owned checks do that.
- **Runs halt rather than guess.** Desktop replay stops on window drift or an
  unresolved target and records the failure stage instead of claiming success.
- **Evidence is part of execution.** Reports, evidence manifests, taught
  workflows, and suite manifests are sealed with SHA-256.

Four honest limits on that model:

- **The dashboard has no authentication at all.** No login, no password, no
  users. Its mutation token is embedded in the rendered HTML, so read access is
  write access whenever a service token is set. It binds `127.0.0.1` by hardcode.
  **Never expose it to a network or port-forward it publicly.** Use an SSH tunnel
  or Tailscale if you need remote access.
- **A seal is a drift detector, not a signature.** SHA-256 seals prove bytes have
  not changed. They do not prove who wrote them or who approved them. There is no
  PKI here.
- **Receipts and screenshots can contain private data.** Review them yourself
  before sharing any of them.
- **Loopback has one deliberate escape hatch.** `LOCAL_COMPANY_OLLAMA_HOST` lets
  the coordinator reach an Ollama sidecar in Docker Compose. It is validated
  strictly and fails closed, but it can point at a non-local host. Browser QA
  also reaches the URLs you give it, and `browser install` reaches the npm
  registry once.

The full threat model is in [SECURITY.md](SECURITY.md).

## Architecture

Dependency-free Python, one SQLite database, one local model over Ollama's
loopback HTTP API. Specialists run in sequence, then a chair role turns their
output into one decision-ready synthesis.

| Module | What it does |
| --- | --- |
| [`core.py`](src/local_company/core.py) | The engine: `Company`, the SQLite schema and audit ledger, roles, playbooks, routing, owner gates, mission execution, evidence manifests, report sealing, and the Ollama client |
| [`cli.py`](src/local_company/cli.py) | The `local-company` command line, and the only entry point |
| [`dashboard.py`](src/local_company/dashboard.py) | The loopback HTML dashboard, server-rendered forms, and the queue worker |
| [`service.py`](src/local_company/service.py) | Background service supervisor: start, stop, status, and process identity checks |
| [`mcp_server.py`](src/local_company/mcp_server.py) | MCP stdio server exposing 25 governed actions (or one router tool in `compact` profile) |
| [`computer_use.py`](src/local_company/computer_use.py) | Windows teach-and-replay desktop workcell (ctypes/WinAPI) |
| [`browser_operator.py`](src/local_company/browser_operator.py) | Read-only browser QA and sealed suite manifests |
| [`workflow_pilot.py`](src/local_company/workflow_pilot.py) | Sealed pilot runs over learned workflows, with human review |
| [`model_policy.py`](src/local_company/model_policy.py) | The two-model allowlist and its enforcement |
| [`focus.py`](src/local_company/focus.py) | Execution focus: one project, a role budget, digest-bound handoff |
| [`capacity.py`](src/local_company/capacity.py) | Memory and listener admission checks before any model loads |
| [`spreadsheet.py`](src/local_company/spreadsheet.py) | Hardened stdlib CSV / JSON / XLSX reader |
| [`config.py`](src/local_company/config.py) | Company home resolution and store-identity validation |
| [`build_info.py`](src/local_company/build_info.py) | Generated build identity. Do not edit by hand — see [CONTRIBUTING.md](CONTRIBUTING.md) |

Resolution order for automation is deliberate: application API or accessibility
selector first, then stable UI Automation identity, then window-relative
geometry only when the exact application and bounds are known — and halt rather
than guess.

## Where to go next

- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — the full operator reference.
  Every command, every flag, every failure mode. This was the old README and it
  is preserved word for word. Read it when you actually start operating the
  system.
- **[OPERATOR.md](OPERATOR.md)** — the daily loop, incident handling, and the
  runtime guard.
- **[deploy/README.md](deploy/README.md)** — Docker and VPS deployment on Linux,
  including a blunt list of what does not work there yet.
- **[PRODUCT.md](PRODUCT.md)** — why the product is shaped this way, the review
  of prior art (OpenAdapt is the closest), and what would have to be true before
  any of it is sold.
- **[ACCEPTANCE.md](ACCEPTANCE.md)** — recorded local measurements. Dated; read
  the date.
- **[AGENTS.md](AGENTS.md)** — the operating guide for coding agents working in
  this repository.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, tests, code style, and the
  build-manifest rule that will otherwise bite you.
- **[SECURITY.md](SECURITY.md)** — threat model and private reporting.
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** — Contributor Covenant 2.1.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

Copyright 2026 Swan Htet.

Apache-2.0 was chosen over MIT for its explicit patent grant. This project sits
next to real prior art in demonstration-to-replay automation, and an explicit
grant is clearer for everyone than silence.
