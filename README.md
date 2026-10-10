<div align="center">

<img src="docs/assets/logo.png#gh-light-mode-only" alt="StackD" width="340">
<img src="docs/assets/logo-dark.png#gh-dark-mode-only" alt="StackD" width="340">

### Ship infrastructure changes with confidence.

**Stackd is a self-hosted control plane for Terraform and OpenTofu.**<br>
Every change is planned, reviewed by a human, then applied by your own workers —<br>
with a complete audit trail and no long-lived cloud keys.

[![CI](https://github.com/fmiquel90/stackd/actions/workflows/ci.yml/badge.svg)](https://github.com/fmiquel90/stackd/actions/workflows/ci.yml)
[![e2e](https://github.com/fmiquel90/stackd/actions/workflows/e2e.yml/badge.svg)](https://github.com/fmiquel90/stackd/actions/workflows/e2e.yml)
![OpenTofu](https://img.shields.io/badge/OpenTofu-first-FFDA18?logo=opentofu&logoColor=black)
![License](https://img.shields.io/badge/license-Apache%202.0-blue)

📖 **[Read the documentation →](https://fmiquel90.github.io/stackd/)**

</div>

---

## The problem

Terraform is easy for one person and hard for a team:

- **Applies run from laptops or unreviewed CI jobs**, with admin cloud keys stored in CI variables.
- **The state file is shared and fragile**: two concurrent applies or one wrong `state rm`, and you
  are restoring backups.
- **"Who changed prod, and why?"** has no reliable answer.

Managed platforms solve this, but you hand them your state, your cloud credentials and your audit
log. **Stackd gives you the same guarantees on your own infrastructure.**

## What you get

| | |
|---|---|
| ✅ **Every change is reviewed** | Each run stops after `plan`. Someone allowed to approve reads the diff and confirms; sensitive tiers can require a second approver. The apply then runs exactly the reviewed plan. |
| 🔐 **No static cloud keys** | Each run receives short-lived cloud credentials (OIDC) that expire with it. Nothing to store, leak or rotate. |
| 📜 **A complete audit trail** | Who triggered, approved and applied what, on which commit, is recorded with every action. |
| 🧬 **Environments that depend on each other** | One environment's outputs feed the next (network → app). When an upstream apply changes them, the dependents re-plan. |
| 🏠 **Runs where your infrastructure lives** | Self-hosted. Workers pull their work over outbound HTTPS: no inbound ports, and no third party holding your state. |
| 🔓 **Nothing to unlearn, easy to leave** | Code stays in Git, state is plain `tfstate`, IAM roles are standard. Leaving is one `tofu init -migrate-state`. |

## How it works

1. **A push, a pull request or a click starts a run.** Pull requests get a read-only plan, posted
   back to the PR.
2. **A worker on your infrastructure plans the change**, using credentials minted for this run only.
3. **The run waits for a human.** The reviewer sees the diff, the checks and every input with its
   origin, then confirms or discards.
4. **A worker applies the reviewed plan.** If its outputs changed, the environments that depend on it
   are re-planned under their own approval rules.

What the reviewer sees:

```text
┌ core-network / prod / run #142 ─────────────────────────────── [ unconfirmed ] ┐
│ PHASES             │ PLAN   +3 ~1 -0                                            │
│ ● Preparing   12s  │   + aws_vpc.main                                           │
│ ● Planning     8s  │   ~ aws_subnet.app          (tier change)                 │
│ ● Checking     3s  │   - aws_eip.legacy                                         │
│ ○ Apply            │ CHECKS  tfsec ✔   infracost ✔ +$12.40/mo   policy ⚠ warn  │
│                    │ INPUTS  region        = eu-west-1            [set:common]  │
│                    │         network_name  = core-net-prod       [dependency]  │
│                    │         db_password   = ••••••••            [env · secret] │
├────────────────────┴─────────────────────────────────────────────────────────┤
│  ⚠ prod · second approval required      [ Discard ]     [ Confirm & apply ▸ ]  │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Quick start

> **Prerequisites:** Docker, [Task](https://taskfile.dev), [`uv`](https://docs.astral.sh/uv/) and
> `pnpm` (via corepack).

```bash
task dev    # brings up the full stack, migrates and seeds demo data
```

Open **<http://localhost:5173>** and use **Dev login** as `admin`, `alice` or `bob`. No cloud
account is needed: the demo stacks run real OpenTofu against local fixtures.

```bash
task test   # unit and integration tests (real Postgres)
task e2e    # the full plan → approve → apply → cascade scenario
task reset  # start from scratch
```

## Learn more

| | |
|---|---|
| 🚀 [Getting started](https://fmiquel90.github.io/stackd/guide/getting-started/) | Bring the stack up and drive your first run |
| 🧠 [Concepts](docs/CONCEPTS.md) | Every concept, explained with examples |
| 🏗️ [Architecture](docs/ARCHITECTURE.md) | How it works inside, with diagrams |
| 📐 [Specification](docs/SPECS.md) | The technical source of truth |
| 🗺️ [Plan & scope](docs/PLAN.md) | What is built, what is next, what is out of scope |
| 🛠️ [Dev environment](docs/DEV.md) · 🧪 [Testing](docs/TESTING.md) | Working on Stackd itself |
| 🤖 [`CLAUDE.md`](CLAUDE.md) | Working rules and invariants — read before changing behaviour |

## Repository layout

```
api/      FastAPI (Python 3.13) — REST + worker API, auth, audit, OIDC issuer, state backend
worker/   Pull-model agent (Python)
front/    React 19 + Vite + TypeScript
deploy/   Local stack (docker compose)
docs/     User guide and reference docs (published with MkDocs)
```

---

<div align="center">

**OpenTofu first** (Terraform supported) · **self-hosted** · **Apache 2.0**

</div>
