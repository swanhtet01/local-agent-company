---
name: Feature request
about: Propose a capability, or a change to how an existing one behaves
title: "feat: "
labels: ["enhancement"]
assignees: []
---

## The problem

<!-- What are you trying to do today, and where does it stop working?
     Describe the situation, not the solution. -->

## What you want to happen

## How you would know it worked

<!-- What receipt, exit code, JSON field, or test would prove it?
     This project treats verifiable evidence as part of the feature. -->

## Constraints this project keeps

Check that the proposal fits, or say plainly which one it strains and why the
trade is worth it:

- [ ] Stays stdlib-only - no third-party dependency (`dependencies = []`).
- [ ] Runs on Python 3.11+, on Windows and Linux.
- [ ] Local-first: no paid or hosted inference required by default, model
      servers stay bound to `127.0.0.1`.
- [ ] Owner-gated: nothing sends, deploys, spends, publishes, pushes, merges,
      messages anyone, or uses credentials without an explicit human
      confirmation. New reach-outward capability is off by default.
- [ ] Fails closed: an observation that cannot be made is reported as blocked,
      never assumed to be a pass.

## Alternatives you considered

<!-- Including "do nothing" and what that costs. -->
