# Local Agent Company product direction

## What this is

Local Agent Company is a private, auditable AI workbench that runs specialist teams on a user's own computer. It already supports general business missions, coding, project knowledge, datasets, reusable playbooks, queues, schedules, approvals, quality evaluation, reports, and a local dashboard. Ollama provides free local inference after the model is downloaded; OpenCode provides repository tools for coding work.

The defensible product is not “another chatbot.” It is the control layer around local models: scoped projects, explicit roles, evidence retrieval, durable work, evaluation, auditability, owner gates, and predictable resource limits.

## What you can use it for now

- Product research, requirements, roadmaps, pricing drafts, and launch planning.
- Operations reviews, supplier comparisons, SOPs, risk registers, and KPI design.
- Local dataset profiling and evidence-backed analysis.
- Marketing concepts, content plans, sales drafts, and customer-research preparation.
- Coding in any local repository through OpenCode and Ollama.
- Queued missions and manually materialized recurring work with an owner-controlled worker.

Small local models are best for bounded transformations and first drafts. Complex architecture, high-stakes decisions, computer control, and final commercial claims still require stronger-model or human review.

## Products that can be sold

1. **Private AI Team for small businesses** — install the workbench, configure their projects and documents, deliver role playbooks, and charge setup plus support.
2. **Vertical agent kits** — package repeatable workflows for shops, factories, agencies, clinics, or accounting teams. Sell the workflow, evidence contract, templates, and onboarding rather than raw model access.
3. **Local coding appliance** — provide a preconfigured Ollama/OpenCode environment with repository rules, testing, model selection, and maintenance for teams avoiding per-token costs.
4. **Auditable document and data workbench** — local research, dataset profiling, reports, approvals, and export for organizations that cannot upload private files to cloud AI.
5. **Agent workflow SDK** — let developers define roles, playbooks, gates, evaluators, and private connectors while this runtime supplies the queue, audit, resource, and approval layers.

## What is potentially inventive

The individual ingredients—local models, role prompts, queues, retrieval, and approvals—are established techniques. A patentable invention is not proven. The stronger original contribution is the specific combination of evidence-bound execution, fail-closed knowledge freshness, digest-bound reports, owner-gated consequential actions, resource-aware single-machine orchestration, and portable vertical workflow contracts.

Treat this as product engineering and trade-secret know-how today. Before publishing a paper or filing a patent, run a prior-art search and document which mechanisms are genuinely novel, technically non-obvious, and implemented—not merely planned.

## Scale path

### Stage 1: one-machine product

- Use `local-ai.cmd` as the single entry point.
- Upgrade from the bootstrap 0.8B model to a measured 4B model for better work quality.
- Record task success, runtime, correction effort, and memory use across real missions.
- Turn the best repeated missions into versioned playbooks and acceptance tests.

### Stage 2: sellable pilot

- Build and verify the deterministic private pilot bundle; keep it owner-only
  until licensing, signing, customer scope, and acceptance terms are chosen.
- Choose one customer type and one expensive repeated workflow.
- Install on one isolated customer machine or approved private server.
- Establish a human-reviewed baseline and a fixed acceptance test.
- Charge for installation, workflow configuration, training, and support.
- Keep sending, payments, production writes, and credentials behind explicit integrations and owner approval.

### Stage 3: multiple machines

- Keep one coordinator as authority and register other machines as named workers.
- Give each worker one scoped assignment, bounded resources, and a signed evidence/result contract.
- Add mutual authentication, encrypted transport, revocation, replay protection, and version compatibility before accepting remote work.
- Do not share one writable checkout or database across machines.

### Stage 4: platform

- Publish a stable workflow schema and SDK.
- Add a signed playbook marketplace and installer.
- Offer paid support, enterprise policy packs, private connectors, and managed updates.
- Preserve a fully local tier; make cloud inference and hosted coordination optional.

## Autonomy boundary

Useful autonomy means repeatedly completing reviewed local work, recovering safely, and presenting decisions with evidence. It does not mean unlimited permissions. The current platform deliberately requires explicit local commands to materialize schedules and run queued missions. A future always-on executor should be added only after mission budgets, cancellation, leases, recovery, resource admission, and owner gates are proven under real use.

The product now exposes `local-ai.cmd cycle` as its safe autonomous unit: materialize due schedules, bind review to the exact next queue ID, and execute no more than one local mission if every current gate passes. Each invocation stops after one decision and one possible mission. Scaling means scheduling more bounded cycles or moving admitted work to additional machines, not creating an uncontrolled infinite agent.

## Next measurable milestone

Use the launchpad for ten real missions across at least three categories—coding, business planning, and data or research. Record whether each result was accepted, how many corrections it required, runtime, peak memory, and whether the task would have justified a paid setup. Promote only the workflows with repeatable acceptance evidence into sellable kits.

The zero-model command `local-ai.cmd experiment` and its local-company MCP
action `product_experiment_next` turn this milestone into the next runnable
experiment. They balance category coverage, provide the
exact local runner prompt and objective checks, and leaves acceptance,
corrections, and paid-setup demand as explicit human observations. This keeps
autonomous iteration useful without manufacturing product-market evidence.
`local-ai.cmd experiment-run` can execute that planned prompt and bind the
runner receipt to its required tool actions, but it deliberately stops before
the human acceptance and paid-setup judgment.

The read-only MCP action `product_offer_next` permits owner-reviewed packaging
only after the full ten-run measured cross-category milestone and at least two
integrity-checked runs of the same labeled workflow were accepted with a
positive paid-setup signal and no more than one correction each. It generates
bounded evidence claims and explicit prohibited claims; it never authorizes
publication, outreach, pricing promises, deployment, or revenue claims.
