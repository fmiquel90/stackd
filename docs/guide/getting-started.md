# Getting started

Bring up a complete local Stackd in one command, log in, and drive a run from `plan` to `apply` —
no Google account or AWS needed.

## Prerequisites

Docker, [Task](https://taskfile.dev), [`uv`](https://docs.astral.sh/uv/), and `pnpm` (via
corepack).

## Launch the stack

```bash
task dev      # generates .env + dev keys, brings up the stack, migrates & seeds
```

This starts Postgres, an API, a worker, the front, and object storage (Garage), applies the
migrations, and seeds a demo. Open **<http://localhost:5173>**.

## Log in

In dev, authentication is a **dev login** with three personas of distinct tiers — no real IdP:

| Persona | Role | Apply ceiling |
|---|---|---|
| `admin` | admin | prod |
| `alice` | approver | prod |
| `bob` | writer | staging |

!!! note
    `bob` (writer) can **trigger** plans but cannot **approve** an apply — approving requires the
    `approver` or `admin` role. See [Runs & approvals](runs-and-approvals.md).

## Your first run

The seed creates a `demo-network` and a `demo-app` stack. From the UI:

1. Open a stack → an environment → **Plan**. A run is created and a worker picks it up.
2. Watch the **Run page**: the phase rail, the plan diff, the checks, the resolved inputs with
   their provenance, and the live log.
3. When it reaches **unconfirmed**, an approver clicks **Confirm & apply** (prod additionally needs
   a second person and a typed confirmation).

The same flow over the API:

```bash
# Dev-login as alice and capture the access token
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/dev/login \
  -H 'content-type: application/json' -d '{"persona":"alice"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# Trigger a plan on an environment, then confirm the resulting run
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/runs -H "$AUTH" -d '{}'
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/confirm -H "$AUTH"
```

## Useful tasks

```bash
task test     # unit + integration (real Postgres via testcontainers)
task e2e      # the full live scenario, a real worker running OpenTofu
task reset    # tear everything down and start fresh
task psql · task logs · task lint · task docs
```

## Next steps

- [Stacks & environments](stacks-and-environments.md) — model your own repos.
- [Variables & secrets](variables.md) — configuration in layers.
- [Cloud credentials (OIDC)](cloud-credentials.md) — wire up real cloud access.
- [Importing an existing stack](importing-existing-state.md) — bring your existing state in.
- [Deploying to AWS](deploying-to-aws.md) — go to production on ECS + RDS + CloudFront.

## See also

- [Concepts](../CONCEPTS.md) · [Dev environment](../DEV.md)
