# Stacks & environments

A **stack** is a template (a Git repo + a folder + a tool); an **environment** is an executable instance of that template with its own state, variables and protections. This page shows how to define both via the API.

## The mental model: template vs instance

| | **Stack** | **Environment** |
|---|---|---|
| Is | the template (code) | an instance (a deployable target) |
| Holds | `repo_url`, `project_root`, `tool`, `tool_version`, repo auth | `tier`, `branch`, state, variables, protections, labels |
| Runs? | never | a run always belongs to an environment |

A stack carries **no branch, no state, no autodeploy** — all of that lives on the environment. See [Concepts §2](../CONCEPTS.md).

## Defining a stack

A stack points at a Git repo, a subfolder within it (`project_root`, default `.`), and the IaC tool. Repo auth is one of `none`, `token`, or `deploy_key`.

```bash
STACK=$(curl -s -X POST localhost:8000/api/v1/stacks \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"core-network","repo_url":"https://github.com/acme/infra.git",
       "project_root":"network","tool":"opentofu","tool_version":"1.12.0"}' | jq -r .id)
```

!!! tip "One repo, several stacks"
    A single repo can host many stacks by giving each a distinct `project_root` (e.g. `network`, `db`, `app`). The push webhook is configured **per repo** with one shared secret; on receipt the API resolves matching stacks and filters environments by branch and `project_root`. See [SPECS §3.1](../SPECS.md).

### Validating the repo

Before creating environments, check that Stackd can reach the repo with the configured auth:

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/check-repo \
  -H "Authorization: Bearer $TOKEN"
```

## Defining environments

Each environment is an instance of the stack. The same code runs against physically separate state and variables.

```bash
# dev — permissive
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/environments \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"dev","tier":"dev","branch":"main"}'

# prod — protected, second pair of eyes
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/environments \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"prod","tier":"prod","branch":"main",
       "protected":true,"require_second_pair_of_eyes":true}'
```

### Fields that matter

- **`tier`** (`dev < staging < prod`) — carries apply/destroy permissions: `can_apply` requires `max_apply_tier >= env.tier`. See [Runs & approvals](runs-and-approvals.md).
- **`branch`** — the branch this environment tracks for staleness and webhook-driven runs.
- **`protected`** — forces confirmation and 4-eyes; it is **not** the access control (that is `tier`).
- **`require_second_pair_of_eyes`** — the triggerer cannot confirm (already implied for `tier=prod`; useful on staging).
- **`managed_state`** — when true, Terraform talks to Stackd's HTTP backend (state bytes in S3). Set `false` to keep your own `backend "s3"`. See [Concepts §7](../CONCEPTS.md).
- **`allow_mock_apply`** — allow applying a run that consumed mock outputs (default `false`). See [Variables](variables.md).
- **`labels`** — JSON tags used to target a worker pool.

!!! warning "Why not Terraform workspaces"
    Workspaces share code, backend and credentials and only suffix the state key — easy to target the wrong one. A Stackd environment is **physically isolated**: its own state, variables, protections and worker pool.

### Refreshing the tracked head

Staleness compares the last applied commit to the branch head. `head_sha` updates via push webhook or 15-min polling; force it manually:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV/refresh-head \
  -H "Authorization: Bearer $TOKEN"
```

`GET /api/v1/environments/{id}` then exposes `head_sha`, `commits_ahead`, `affects_project_root` and `stale`. See [SPECS §9.6](../SPECS.md).

## See also

- [Variables](variables.md) — variable sets, resolution and sensitive values
- [Runs & approvals](runs-and-approvals.md) — triggering plans and confirming applies
- [Concepts](../CONCEPTS.md) — the full mental model
- [SPECS](../SPECS.md) — data model and API reference
