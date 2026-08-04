# SuperMega Local Workcell

> Teach it once. Preview it safely. Run it locally. Prove what changed.

Product review date: 2026-08-04

## Product decision

The repository name remains `local-agent-company`, but the product is now
**SuperMega Local Workcell**. The ten-role "AI company" is a supporting
orchestrator, not the thing a customer buys. The product customers can
understand is a private workcell that learns bounded computer tasks, compiles
them into sealed workflows, replays them without paying for a model on every
run, verifies the result, and keeps an audit receipt.

The first supported substrate is the local Windows desktop. Coding through
OpenCode/Ollama and the existing business/data team remain available behind
the same launcher. Browser and Android substrates are roadmap items, not
current claims.

## The problem

Small businesses have work trapped between tools that do not expose useful
APIs: desktop accounting packages, supplier portals, spreadsheets, legacy
line-of-business applications, and repetitive browser forms. Conventional RPA
is powerful but expensive to configure and maintain. General AI agents can be
flexible, but they are costly, hard to audit, and still unreliable on long
unconstrained GUI tasks. Local models reduce inference cost but do not by
themselves create a dependable operator.

SuperMega Local Workcell targets the useful middle:

- A person demonstrates a narrow task once.
- The workcell compiles clicks, safe keys, and private-value placeholders into
  a canonical SHA-256-sealed workflow.
- A read-only preflight resolves every application and control before input.
- Stable runs are deterministic and model-free.
- The runner stops on window drift, unresolved targets, policy blockers, or a
  failed outcome check.
- Every run produces local, app-window-bounded evidence and a machine-readable
  receipt.
- Models are optional planners and repair assistants, never the source of
  truth for a successful run.

## Current proof, not a promise

Run this from the repository:

```powershell
.\local-ai.cmd automate doctor
.\local-ai.cmd automate prove --confirm "RUN LOCAL COMPUTER WORKFLOW"
```

The proof launches a built-in no-file/no-network Windows app, resolves its
controls, clicks the input, replaces its value through Windows input injection,
clicks **Apply locally**, checks the resulting window title, captures only the
app client area, writes hashes and a receipt, then closes the owned process. It
does not call a model or a paid API.

This proves the local execution substrate. It does not prove that an arbitrary
third-party workflow is reliable; every real workflow still needs its own
replay and outcome test.

## Market and technical review

| System | What it does well | Lesson for this product |
| --- | --- | --- |
| [OpenHands](https://docs.openhands.dev/openhands/usage/architecture/runtime) | Sandboxed coding runtime with mounted repositories and action/observation isolation. | Keep coding in a sandbox-capable specialist; do not rebuild a general coding agent in the desktop runner. |
| [Aider](https://aider.chat/docs/usage/commands.html) and [OpenCode](https://opencode.ai/docs/agents/) | Repository maps, visible diffs, tests, undo, specialist agents, and granular permissions. | Coding value comes from context, verification, and reversibility rather than a large roster of role prompts. |
| [Open Interpreter](https://www.openinterpreter.com/docs/desktop) | A broad desktop assistant spanning apps, files, browser tabs, and local model providers. | Natural-language computer control is useful, but our low-resource wedge should execute proven workflows without an LLM loop. |
| [Power Automate Desktop](https://learn.microsoft.com/en-us/power-automate/desktop-flows/ui-elements) | Mature UI-element capture, multiple selectors, selector testing, and attended RPA. | A workflow editor, selector repair, variables, branches, and per-step testing are table stakes for a sellable RPA product. |
| [OpenAdapt](https://docs.openadapt.ai/) | Demonstration-to-deterministic-replay, structural/visual resolution, effect checks, halt-on-drift, and teachable repair. | This is the closest prior art. We should learn from or integrate it, not claim to have invented demonstration compilation. Our narrower wedge is zero-download Windows operation plus SuperMega business packs and low-resource receipts. |
| [Microsoft UFO](https://github.com/microsoft/UFO) | Hybrid Windows UIA/Win32/API execution, application-specialist agents, and multi-device DAG orchestration. | Prefer structured app APIs and accessibility before vision; add a DAG only after single-workflow reliability is measured. |
| [UI-TARS Desktop](https://github.com/bytedance/UI-TARS-desktop) and [Agent S](https://github.com/simular-ai/Agent-S) | Screenshot-driven, cross-application autonomous GUI operation and increasingly capable GUI models. | Vision autonomy belongs in an optional planner/repair tier. It is too resource-heavy and probabilistic to be the default replay engine on the Ally. |
| [Cua](https://github.com/trycua/cua) | Common local/cloud sandbox API across Linux, macOS, Windows, and Android, with trajectory and benchmark tooling. | Integrate a sandbox backend when isolation is required instead of building VM infrastructure ourselves. |
| [Magentic-UI](https://github.com/microsoft/magentic-ui) | Transparent plans, progress, and human-in-the-loop control for web and coding tasks. | The operator must see the current step, stop the run, inspect evidence, and intervene without decoding raw logs. |
| [n8n with Ollama](https://n8n.io/integrations/ollama/) | Visual, self-hosted API/data workflows with a large integration ecosystem. | Use or interoperate with n8n for APIs; reserve computer use for applications with no safe structured integration. |
| [Windows Agent Arena](https://github.com/microsoft/WindowsAgentArena), [OSWorld](https://github.com/xlang-ai/OSWorld), and [AndroidWorld](https://github.com/google-research/android_world) | Reproducible task environments and execution-based evaluation. Windows Agent Arena recommends combining UIA with visual parsing. | Success must be measured by task-state verifiers, not by an agent saying it finished. UIA-first plus optional vision is the correct resolution ladder. |

## What is actually differentiated

The individual ingredients are established. There is no proven patentable
invention here yet. The credible differentiation is the complete operating
contract for low-resource, local-first business work:

1. **Compile once, replay cheaply.** A local model may help plan or repair, but
   repeat executions do not consume inference.
2. **Fail closed before input.** Preview resolves the exact application and
   every target, blocks known network and shell applications by default, and
   names required private inputs.
3. **Private values are runtime-only.** Demonstrations store placeholders, not
   typed characters; receipts do not store supplied values.
4. **Evidence is part of execution.** Workflows are sealed; receipts bind to
   the workflow and hashed app-only screenshots; success requires an
   observable postcondition.
5. **Low-resource Windows first.** The current engine uses built-in Win32,
   Windows UI Automation, System.Drawing, and PowerShell assemblies. It needs no
   vision model or extra Python automation package for its supported path.
6. **Business packs, not generic magic.** A sellable unit is a tested workflow
   pack with parameters, policy, expected effects, acceptance tests, training,
   and support.
7. **One owner boundary across AI and automation.** Sending, buying, deploying,
   publishing, credential use, and production writes remain explicit owner
   gates even when a workflow can technically click the control.

This combination may become defensible implementation know-how and a trusted
brand. It should not be marketed as universal autonomy or a novel foundation
model.

## Product architecture

```text
local-ai.cmd
    |
    +-- PROVE: one-command local execution proof
    +-- TEACH: demonstration -> typed placeholders + target observations
    +-- PREVIEW: integrity + app identity + selector + policy resolution
    +-- RUN: exact confirmation -> deterministic actions
    +-- VERIFY: expected window/control postconditions; effect checks planned
    +-- RECEIPT: hashes + bounded evidence + timings + halt stage
            |
            +-- Windows adapter (current: UIA -> window-relative fallback)
            +-- Browser adapter (planned: DOM/accessibility -> visual fallback)
            +-- Android adapter (planned: ADB/accessibility -> visual fallback)
            +-- Structured APIs (reuse n8n/MCP/app APIs when safer)

Optional local intelligence:
    Ollama/OpenCode -> plan, code, classify, or propose a repair
    OmniParser/local VLM -> resolve opaque controls when structured evidence fails
    Neither may mark a run successful; verifiers do that.
```

The resolution order is deliberate:

1. Application API, CLI, DOM, or accessibility selector.
2. Stable UI Automation identity and control attributes.
3. Optional local OCR/icon/vision anchor.
4. Window-relative geometry only when the exact app identity and bounds are
   known.
5. Halt rather than guess.

## SuperMega value

SuperMega is the first design partner and workflow-pack customer. The workcell
adds value where SuperMega currently loses time switching between local tools
or repeating verification:

- **Product and release evidence pack:** open a bounded local build, execute a
  known smoke path, capture app-only evidence, and return a release-review
  receipt. Deployment stays gated.
- **Shop operations pack:** transfer reviewed local invoice/order fields into a
  desktop system and verify the resulting status. Payable approval, sending,
  and production persistence stay gated.
- **Asset QA pack:** open local product assets, execute a fixed inspection path,
  and record which item reached the expected state. Publishing stays gated.
- **Lead-preparation pack:** normalize and stage owner-reviewed local lead data
  for a CRM or browser adapter. Outreach and connector writes stay gated.
- **Customer installation pack:** install the workcell, teach one expensive
  repeated workflow, establish a baseline, and deliver an integrity-checked
  acceptance report plus support terms.

The first SuperMega pack should be selected by measured pain, not imagination:
record task frequency, human minutes, error cost, application, external-effect
risk, and a machine-checkable success state. Choose the highest-time task that
has a stable local or test environment and no irreversible side effect.

## Sellable offer

Do not sell "an AI army." Sell an outcome-specific private workcell:

- Installation and machine readiness check.
- One mapped and recorded workflow.
- Named parameters and redaction rules.
- Preview and owner-gate policy.
- Twenty supervised acceptance runs against a fixed test set.
- Operator training, recovery instructions, and a support window.
- Optional local-model planning or document work, clearly separated from the
  deterministic automation SLA.

Revenue should initially come from setup, workflow engineering, training, and
support. A recurring maintenance plan becomes credible only after selector
drift and repair effort are measured. Do not promise savings, accuracy, or
unattended operation without customer-specific evidence.

## Reliability gates

A workflow is not "working" because it ran once. Promotion from experiment to
reusable pack requires all of these:

- A new operator can run the built-in proof from one documented command.
- The real workflow can be taught in at most five minutes.
- At least 20 supervised replays cover restart, moved window, changed window
  size, pre-filled fields, and one intentionally missing target.
- At least 90% complete on the stable supported UI before pilot use; the target
  becomes 99% before unattended use.
- Zero clicks in the wrong process or window.
- Every drift case halts before the unsafe action.
- Every run has a receipt and task-state verifier.
- Typed private values appear in neither workflow JSON nor receipt JSON.
- Human correction time and minutes saved are measured, not estimated.

## Roadmap and build/integrate decisions

### P0 - usable Windows proof and workflow engine (current slice)

- One-command visible proof.
- Window inventory and bounded UIA inspection.
- Demonstration capture with typed-value placeholders.
- Integrity-sealed workflow, read-only preflight, exact confirmation, target
  resolution, app-only evidence, postconditions, and halt-stage receipt.

### P1 - workflow studio and repair loop

- A simple visual timeline/editor instead of raw JSON.
- Explicit field targets and text modes, variables, retries, waits, branches,
  loops, and per-step effect checks.
- Selector ranking using recorded bounds and control ancestry.
- "Teach the repair" flow and versioned workflow migrations.
- A local evaluation runner with success rate, latency, drift, and correction
  dashboards.

### P2 - first SuperMega workflow pack

- Interview the actual operator and measure one repeated task.
- Record a safe test-environment demonstration.
- Build an outcome oracle and 20-run acceptance set.
- Package onboarding, runbook, policy, and support—not just the JSON workflow.

### P3 - browser substrate

- Prefer Playwright/DOM/accessibility and network-safe read operations.
- Reuse n8n or MCP for stable API steps.
- Keep final submissions, messages, purchases, credential use, and production
  writes behind owner confirmation.

### P4 - optional local visual resolver and training data

- Evaluate OmniParser or a smaller local UI grounding model only when UIA/DOM
  cannot identify the target.
- Store consented, redacted trajectories in a versioned dataset with app,
  screen, target, action, effect, and outcome labels.
- Compare structured-only, vision-only, and hybrid resolution on the same test
  cases before adding model weight to the product.

### P5 - Android/mobile adapter

- Use ADB plus Android accessibility in an emulator first.
- Adopt AndroidWorld-style task resets and state verifiers.
- Move to a real device only after emulator safety and recovery are repeatable.

### P6 - isolation and scale

- Evaluate Cua/OpenHands-style local sandboxes instead of implementing a VM
  platform.
- Add multi-machine scheduling only after one-machine workflows have leases,
  cancellation, idempotency, recovery, and passing acceptance tests.

## Paper, open source, and invention decision

A paper is worthwhile after there is a real dataset and comparative result. A
credible report would ask: *Can a UIA-first, model-free replay engine on a
low-resource Windows device match vision agents on stable business workflows
while reducing inference cost and unsafe actions?* Compare completion rate,
wrong-target rate, halt precision, repair time, latency, memory, and marginal
inference cost against Power Automate-style selectors, visual-only agents, and
the hybrid approach.

Until those results exist, publish no novelty claim. Keep the workflow packs,
customer data, and trajectory dataset private. An open technical report or
selected engine components can later support trust and recruiting; licensing,
trademark, prior-art review, and any patent decision require separate owner and
professional review.

## Immediate milestone

The next milestone is not more roles. It is one useful SuperMega workflow pack
that passes the 20-run reliability gate and saves measured human time. The
built-in proof establishes the engine baseline; the first real pack establishes
product value.
