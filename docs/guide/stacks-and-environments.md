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
    A single repo can host many stacks by giving each a distinct `project_root` (e.g. `network`, `db`, `app`). One GitHub webhook per repo (`POST /api/v1/webhooks/github`) serves them all, but each stack holds **its own** HMAC secret (`webhook_secret`, write-only, set with `PATCH /api/v1/stacks/{id}` or in the stack's **Settings → General**): a delivery acts only on the stacks of that repo whose secret verifies its signature, so give the stacks fed by one webhook the same secret. On a push, the API then picks the environments tracking the pushed branch and triggers a run only when the push touches the stack's `project_root`. See [SPECS §3.1](../SPECS.md).

!!! note "Redelivering a GitHub webhook"
    Each delivery is acted on once: the API remembers its `X-GitHub-Delivery` id for **5 minutes**. A delivery GitHub sends again within that window — including **Redeliver** in the repo's webhook settings, which reuses the id — gets a `200` with `"duplicate": true` and does nothing. Past 5 minutes a Redeliver is processed again; a delivery that failed (5xx) can be redelivered at once. A request without the `X-GitHub-Delivery` header is refused (`400`). See [SPECS §13](../SPECS.md).

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

- **`tier`** (a configurable catalog, e.g. `dev`/`staging`/`prod`/`qa`, listed by `GET /api/v1/tiers`) — carries apply/destroy permissions: `can_apply` requires `env.tier ∈ user.allowed_tiers`. The tier must exist in the catalog (`422 Unknown tier` otherwise). See [Runs & approvals](runs-and-approvals.md).
- **`branch`** — the branch this environment tracks for staleness and webhook-driven runs.
- **`autodeploy`** — confirm a clean plan without a human (default `false`). See [Runs & approvals](runs-and-approvals.md#autodeploy-checks).
- **`protected`** — forces confirmation (never autodeploys: `autodeploy` is forced off) and four-eyes; it is **not** the access control (that is `tier`).
- **`require_second_pair_of_eyes`** — four-eyes alone: the person who triggered a run cannot confirm it, and the environment may still autodeploy (already implied on a tier flagged `requires_four_eyes`, like the seeded `prod`, and on a `protected` environment; useful on staging).
- **`managed_state`** (default `true`) — when true, Terraform talks to Stackd's HTTP backend (state bytes in S3) and the repo must declare **no** `backend` block: the worker adds `backend "http" {}` itself. Set `false` to keep your own `backend "s3"`. See [Concepts §8](../CONCEPTS.md#8-managed-state-s3-behind-an-http-backend).
- **`allow_mock_apply`** — allow applying a run that consumed mock outputs (default `false`). See [Dependencies & mocks](dependencies-and-mocks.md).
- **`allow_fallback_apply`** — allow applying a run whose external secret came from a fallback (default `false`). See [External secret sources](external-secrets.md#fallback-blocks-apply-by-default).
- **`drift_check_enabled`** — include the environment in the periodic drift check (default `true`).
- **`labels`** — JSON tags: a run is only claimed by a worker whose labels contain all of them (see [Workers & scaling](workers-and-scaling.md#dedicate-a-pool-with-labels): its pool's labels plus its own `STACKD_WORKER_LABELS`); with the stack's labels they also select variable sets (see [Variables](variables.md)).

!!! warning "Why not Terraform workspaces"
    Workspaces share code, backend and credentials and only suffix the state key — easy to target the wrong one. A Stackd environment is **physically isolated**: its own state, variables, protections and worker pool.

### Refreshing the tracked head

Staleness compares the commit of the last finished tracked run (every run is pinned to the commit it planned) to the branch head. `head_sha` updates via the push webhook or the 15-min `git ls-remote` polling; force it manually (audited `environment.head_refreshed`):

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV/refresh-head \
  -H "Authorization: Bearer $TOKEN"
```

`GET /api/v1/environments/{id}` then exposes `head_sha`, `commits_ahead`, `affects_project_root` and `stale`. An environment with nothing applied yet is never `stale`. Changing an environment's `branch` clears its recorded head until the next poll, and a push that deletes the branch clears it too. See [SPECS §9.6](../SPECS.md).

## Deleting a stack or an environment

`DELETE /api/v1/stacks/{id}` and `DELETE /api/v1/environments/{id}` are **refused with `409 Active
run`** while one of the environments involved has a run in an active state (`preparing` through
`applying`, or a command `running`); the problem body names the `run_id`. Cancel or discard it, or
let it finish, then retry. Queued runs are deleted with their environment. They are also refused
with **`409 Active rollout`** (naming the `rollout_id`) while an environment involved belongs to a
[rollout](runs-and-approvals.md#rollouts) in progress: cancel the rollout, or let it end, first.

The deletion also removes, in the same transaction and each audited with the reason
(`stack_deleted` or `environment_deleted`), everything that points at the stack or its
environments:

- variable-set attachments (`variable_set.detached`) — the set itself stays;
- platform hooks (`hook.deleted`);
- notification targets (`notification_target.deleted`).

An attach racing the deletion either fails with `404` or is removed with the rest: no orphan is
left behind.

## Cloning an environment across stacks

To stand up a new environment (a new region, a new stage) in several stacks of a space at once,
declare it **once**, modelled on the environment each stack already has under a reference name. In
the UI: **New environment from…** (on the overview, or an environment's **More** menu), which
shows a review before anything is created. Over the API:

```bash
curl -s -X POST localhost:8000/api/v1/spaces/$SPACE/environment-clones \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"reference_name":"prod","name":"prod-euw3",
       "overrides":{"labels":{"region":"eu-west-3"}},"dry_run":true}'
```

- **Targets**: every stack of the space that has a `reference_name` environment, or only
  `stack_ids`. A target stack without the reference, or already holding `name`, is refused (`422`).
- **`overrides`**: `tier`, `branch`, `labels` (merged onto the reference's, yours win per key),
  `autodeploy`, `protected`, `require_second_pair_of_eyes`, `drift_check_enabled`. Omit a key to keep
  the reference's value; `null` is refused.
- **`dry_run: true`** computes and validates everything, then rolls back: the response is the preview.
- **All or nothing**, in one transaction, every created item audited (`environment.created` with
  `cloned_from`, then one `environment.cloned` summary).

What a new environment takes from its reference: its settings (tier, branch, protections,
`autodeploy`, `managed_state`, `drift_check_enabled`, labels, position), its non-sensitive
env-level variables, its external secret references, its variable-set attachments, its platform
hooks, its notification targets and its dependency edges (re-wired to the new environments when both
ends are cloned). It never takes `allow_mock_apply`/`allow_fallback_apply` (always `false`), nor
a reference with `managed_state=false` (`422`: it would share the reference's state). **Sensitive
values are not copied**: the response lists them in `missing_secrets` (and what else needs a hand —
a static secret fallback, the cloud integration unless `copy_cloud_integration`, an admin-only
option — in `to_configure`). While anything is left to fill, the new environment is created with
`autodeploy` off. The response also lists `not_copied` items (an env value a newly selected label
set should provide) and `warnings` (an edge still wired to the old region's upstream, a changed
tier) — review them before applying. See [SPECS §21.2](../SPECS.md).

To then plan and apply the new environments in dependency order under one approval, start a
[rollout](runs-and-approvals.md#rollouts).

## See also

- [Variables](variables.md) — variable sets, resolution and sensitive values
- [Runs & approvals](runs-and-approvals.md) — triggering plans and confirming applies
- [Concepts](../CONCEPTS.md) — the full mental model
- [SPECS](../SPECS.md) — data model and API reference
