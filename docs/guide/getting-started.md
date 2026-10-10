# Getting started

Bring up a complete local Stackd in one command, log in, and drive a run from `plan` to `apply` —
no Google account or AWS needed.

## Prerequisites

Docker, [Task](https://taskfile.dev), [`uv`](https://docs.astral.sh/uv/), and `pnpm` (via
corepack).

## Launch the stack

```bash
task dev      # generates .env + dev keys, provisions the dev S3, brings up the stack, migrates & seeds
```

This starts Postgres, an API, a worker, the front, and object storage (Garage), applies the
migrations, and seeds a demo. Open **<http://localhost:5173>**.

Before the containers start, `task dev` provisions Garage (`scripts/bootstrap-s3.sh`, also
`task s3`): it assigns the single-node layout, writes a generated access key into `.env`
(`S3_ACCESS_KEY` / `S3_SECRET_KEY`, only when both are empty), and creates the `stackd` bucket.
The API needs it from the first plan: every tracked plan with changes is saved there, encrypted,
until its run ends. Every step checks before it acts, so re-running `task dev` is safe. See
[Dev environment §2](../DEV.md#2-docker-compose-stack).

## Log in

In dev, authentication is a **dev login** with three personas of distinct tiers — no real IdP:

| Persona | Role | Allowed tiers | Destroy |
|---|---|---|---|
| `admin` | admin | `dev`, `staging`, `prod` | ✅ |
| `alice` | approver | `dev`, `staging`, `prod` | ❌ |
| `bob` | writer | `dev`, `staging` | ❌ |

!!! note
    `bob` (writer) can **trigger** plans but cannot **approve** an apply — approving requires the
    `approver` or `admin` role. See [Runs & approvals](runs-and-approvals.md).

## Your first run

The seed creates a `demo-network` and a `demo-app` stack in the `demo` space, each with a `dev`
and a `prod` environment; `demo-app` consumes `demo-network`'s output. From the UI:

1. Open a stack → an environment → **Plan**. A run is created and a worker picks up its plan job.
2. Watch the **Run page**: the phase rail, the plan diff, the checks, the resolved inputs with
   their provenance, and the live log.
3. When it reaches **unconfirmed**, an approver clicks **Confirm & apply**. On `prod`, that
   approver must be someone other than the person who triggered the run (four-eyes), and types the
   environment's name to confirm. The apply runs exactly the plan you reviewed, on the
   commit it was made on — see [Runs & approvals](runs-and-approvals.md#what-gets-applied-is-what-was-reviewed).

The same flow over the API:

```bash
# Dev-login as alice and capture the access token
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/dev/login \
  -H 'content-type: application/json' -d '{"persona":"alice"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# Trigger a plan on an environment, then confirm the resulting run
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/runs -H "$AUTH" \
  -H 'content-type: application/json' -d '{}'
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/confirm -H "$AUTH"
```

## Useful tasks

```bash
task test     # API unit + integration (real Postgres via testcontainers) + front vitest
task e2e      # the full live scenario, a real worker running OpenTofu (re-runnable)
task reset    # tear everything down (DB + Garage volumes) and start fresh; .env keeps its keys
task s3-ls    # list the dev bucket (saved plans, managed states)
task psql · task logs · task lint · task docs
```

!!! tip "A plan fails while saving the plan file"
    The API could not reach the dev S3. Run `task s3` (idempotent), then `task dev` again so the
    `api` container is recreated with the key from `.env`.

## Next steps

- [Stacks & environments](stacks-and-environments.md) — model your own repos.
- [Variables & secrets](variables.md) — configuration in layers.
- [Cloud credentials (OIDC)](cloud-credentials.md) — wire up real cloud access.
- [Importing an existing stack](importing-existing-state.md) — bring your existing state in.

## See also

- [Concepts](../CONCEPTS.md) · [Dev environment](../DEV.md)
