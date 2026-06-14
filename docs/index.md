# Stackd

**Ship infrastructure changes with confidence.** Stackd is a self-hostable control plane for
Terraform & OpenTofu: every change becomes a reviewable **`plan → human approval → apply`** run,
executed on disposable pull-based workers, with a full audit trail and short-lived cloud
credentials minted per run.

> No static cloud secrets. No shared state. No one running `apply` from their laptop.

## Why Stackd?

Running Terraform by hand doesn't scale: secrets live in CI variables, `apply` happens on
someone's machine, the state file is a shared landmine, and "who changed prod?" has no good
answer. Managed SaaS solves it — but you hand over your state, credentials, and audit log.

Stackd is the **self-hosted middle ground**: the API is the single source of truth, workers are
stateless and disposable, every state change is one auditable event, and cloud credentials expire
with the run.

## Start here

- 🚀 **[Getting started](guide/getting-started.md)** — bring the stack up and drive your first run.
- 🧱 **[Stacks & environments](guide/stacks-and-environments.md)** — the template/instance model.
- 🛡️ **[Runs & approvals](guide/runs-and-approvals.md)** — tiers, four-eyes, the apply gate.
- 🪝 **[Hooks & `.stackd.yml`](guide/hooks.md)** — platform and repo hooks.
- 📥 **[Importing an existing stack](guide/importing-existing-state.md)** — adopt existing remote state.
- 🔑 **[Cloud credentials (OIDC)](guide/cloud-credentials.md)** — per-run roles, no static keys.

New to the model? The **[Concepts](CONCEPTS.md)** guide explains everything with worked examples.
For the exhaustive technical truth, see the **[Specification](SPECS.md)**.
