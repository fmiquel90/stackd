# Hooks & `.stackd.yml`

Hooks are shell steps that run at lifecycle stages around `init` / `plan` / `apply`. They come from
**two sources** with very different trust levels: **platform hooks** (defined in the UI/API,
non-bypassable) and **repo hooks** (defined in a `.stackd.yml` in your repository).

## The two sources

| | **Platform hook** | **Repo hook** |
|---|---|---|
| Defined in | the UI/API, at stack or environment level | a `.stackd.yml` file in the repo, versioned |
| Who controls it | a `writer+` (audited) | anyone who can push code / open a PR |
| Purpose | imposed governance — **not bypassable by a PR** | project-specific logic (file generation, terragrunt, infracost…) |

**Execution order at each stage:** platform stack hooks → platform env hooks → repo hooks. Put your
**critical security checks on the platform side**, not in the repo.

## `.stackd.yml`

Place it at the root of the stack's `project_root` (so one repo can carry a different file per
stack). Only the `hooks:` key is read.

```yaml
version: 1
hooks:
  before_plan:
    - name: generate-locals
      command: ./scripts/gen-locals.sh
  after_plan:
    - name: infracost
      command: infracost breakdown --path plan.json --format table
      on_failure: warn          # fail | warn (default: fail)
    - name: no-destroy-prod
      command: jq -e '[.resource_changes[] | select(.change.actions | index("delete"))] | length == 0' plan.json
      on_failure: fail
```

**Stages:** `before_init`, `after_init`, `before_plan`, `after_plan`, `before_apply`, `after_apply`.
Each hook is one shell command run in the workspace (`cwd = project_root`), with `name`, `command`,
and `on_failure` (`fail` aborts the run; `warn` forces the run to `unconfirmed` so a human reviews).

!!! tip
    A `warn` check on `after_plan` is how you surface policy/cost findings without blocking — the
    run still stops for a human even under autodeploy.

## What hooks can see

The run's variables are injected as env vars for every hook. Two stages of guard protect repo hooks
(which a PR author controls):

- **No secrets by default.** `sensitive` variables (`sensitive_env`) are *not* passed to repo hooks
  unless the environment opts in (same logic as proposed runs).
- **No cloud credentials by default.** `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` go to
  **terraform only**, never to repo hooks (opt-in per env). Platform hooks always have access.
- **Sensitive values are masked** in hook stdout; hooks have a timeout and run in the run's
  ephemeral container.

!!! warning
    On a `tier=prod` environment, **repo hooks at the `*_apply` stages are forbidden** (ignored with
    a visible warning) — only platform hooks run there, because those stages carry the prod write
    role. The `*_init` / `*_plan` repo stages stay allowed (plan role = read-only). This stops a
    `.stackd.yml` pushed via PR from assuming the prod apply role.

## Managing platform hooks

Platform hooks are CRUD over the API, scoped to a stack or an environment:

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK_ID/hooks -H "$AUTH" -d '{
  "stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "fail"
}'
```

## See also

- [Runs & approvals](runs-and-approvals.md) · [Cloud credentials (OIDC)](cloud-credentials.md)
- [Concepts](../CONCEPTS.md) · [SPECS §8](../SPECS.md)
