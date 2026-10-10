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
| Trusted with secrets | yes | no |

**Execution order at each stage:** platform stack hooks → platform environment hooks → repo hooks.
Put your **critical security checks on the platform side**, not in the repo.

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

Each hook is one shell command run in the checkout (`cwd = project_root`), with `name`, `command`
and `on_failure`.

**Stages.** The plan job runs `before_init`, `after_init`, `before_plan` and `after_plan`. The apply
job, a fresh clone of the reviewed commit, runs `before_apply` and `after_apply`. A
[command run](commands.md) runs no hook. `plan.json` exists only at `after_plan`.

## Failures, warnings and timeouts

- **`on_failure: fail`** (the default): a failing hook stops the job at its stage, before the next
  tofu step — at every stage, platform or repo. The run fails (audited `run.check_failed`). A
  failing `before_apply` hook means **nothing is applied**.
- **`on_failure: warn`**: the job goes on and the warning is recorded. At a plan stage it forces the
  run to `unconfirmed`, even under autodeploy, so a human sees it (audited `run.check_warned`). At
  an apply stage the run is already confirmed: the apply goes on.
- **A failing `after_apply` hook** fails the run although the apply has already changed the
  infrastructure. Stackd still records the new outputs and clears the environment's drift flag,
  but nothing cascades from a failed run.
- **Timeout.** Each hook gets 10 minutes (`STACKD_HOOK_TIMEOUT_SECONDS` on the worker). Past it the
  hook and every process it started are killed, and it fails by its own `on_failure` (detail
  `timed out after <n>s`).
- **No background process.** When a hook's command exits, every process it left running in its
  process group (`cmd &`) is killed: nothing a hook starts outlives it. Run what a check needs in
  the foreground.
- **No plan JSON, no checks.** If tofu can't produce the plan's JSON document, the plan job fails
  (`plan JSON unavailable, not checked`): an `after_plan` check never passes on an empty plan.

!!! tip
    A `warn` check on `after_plan` is how you surface policy/cost findings without blocking — the
    run still stops for a human even under autodeploy.

## Check results

Every hook result is kept on the run, with the stage it ran at, across the plan and apply jobs:
`check_results` in `GET /api/v1/runs/{id}` (or `GET /api/v1/runs/{id}/checks`).

```json
"check_results": {
  "results": [
    {"name": "fmt",       "status": "ok",   "detail": null,     "stage": "before_plan"},
    {"name": "infracost", "status": "warn", "detail": "exit 1", "stage": "after_plan"}
  ],
  "checks": [
    {"name": "infracost", "status": "warn", "detail": "exit 1", "stage": "after_plan"}
  ]
}
```

`results` lists every check (`ok`, `warn`, `fail`), which the run page shows with each one's
stage; `checks` only the ones that need attention. A job that fails on something other than a
check (a tofu error, for example) still records the checks of the stages that ran, and a warning
among them is audited.

## What hooks can see

A repo hook runs code that anyone who can open a pull request controls, so it gets **nothing
secret**. A platform hook is trusted.

| | **Platform hook** | **Repo hook** |
|---|---|---|
| Process environment | the worker's, minus the agent's own `STACKD_*` settings | an allowlist of the worker's: `PATH`, `HOME`, locale, proxy and CA settings… |
| Environment variables of the run | all, sensitive ones included | the non-sensitive ones |
| Cloud credentials (OIDC) | yes | no |
| `plan.json` at `after_plan` | tofu's plan JSON as is | a masked copy: every sensitive value is `***` |

Terraform variables reach **no** hook directly: tofu reads them from variable files that exist
only while a tofu command runs (their values do show in `plan.json`, sensitive ones masked in a repo
hook's copy). Give a hook what it needs as an environment variable.

While a repo hook runs, none of these is on disk: the saved plan, the run's variable files, the
cloud token file, the repo token. Sensitive values are masked in every hook's output.

!!! warning "Not implemented yet"
    The specification also describes ignoring repo hooks at the `*_apply` stages of `prod`
    environments, and a per-environment opt-in giving repo hooks secrets or cloud credentials.
    Neither exists: repo hooks run at every stage, and never get secrets. The `prod` rule is
    pending a decision, since tiers are a configurable catalog. Until the
    [container runner](workers-and-scaling.md#runners) exists, hooks also run as the worker's own
    user, without isolation from other jobs on the same worker ([SPECS §8.3](../SPECS.md)).

## Managing platform hooks

Platform hooks are CRUD over the API, scoped to a stack or an environment:

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK_ID/hooks -H "$AUTH" -d '{
  "stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "fail"
}'
```

## See also

- [Runs & approvals](runs-and-approvals.md) · [Cloud credentials (OIDC)](cloud-credentials.md)
- [Workers & scaling](workers-and-scaling.md) · [Concepts](../CONCEPTS.md) · [SPECS §8](../SPECS.md)
