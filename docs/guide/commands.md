# Running commands (import, state surgery…)

Some operations don't fit `plan → apply`: importing an existing resource, removing something from
state, re-tainting a resource. Stackd runs these as a **command run** — one allowlisted
tofu/terraform subcommand executed on the worker, in the environment, with the same audit and the
managed backend as any other run.

!!! note
    This is **not** arbitrary shell. The worker runs `<tool> <command> <args>` where `command` is
    taken verbatim from an allowlist. To remove a stuck lock, use the dedicated force-unlock
    (UI **State** tab, or `DELETE /api/v1/environments/{id}/state/lock`) — it isn't a command.

## Allowed commands

| Read-only (needs `writer`) | Mutating (needs apply rights) |
|---|---|
| `output`, `show`, `state list`, `state show`, `validate`, `providers` | `import`, `state rm`, `state mv`, `taint`, `untaint`, `refresh` |

**Mutating commands change real state**, so they're gated by `can_apply(user, env)` — the same role
+ tier check as approving an apply (see [Runs & approvals](runs-and-approvals.md)). Read-only
commands only need `writer`.

## Run one

From the UI: open an environment → the **Command** tab → pick a command, add arguments, run. You
land on the run page with live logs.

Over the API:

```bash
# import an existing bucket into state
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" \
  -H 'content-type: application/json' -d '{
  "command": "import",
  "args": ["aws_s3_bucket.logs", "my-existing-bucket"]
}'

# remove a resource from state
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" \
  -H 'content-type: application/json' -d '{
  "command": "state rm", "args": ["aws_instance.old"]
}'
```

The response is a normal run (`type: command`). It flows `queued → preparing → running →
finished/failed`, holds the **one-active-run-per-env** lock (no command races a plan/apply), streams
logs, and uses the managed state backend.

## What it runs with

- The worker clones the repo at the request's `commit_sha` (a full SHA, a branch or a tag) or, by
  default, the head of the environment's branch, `init`s with the env's backend, then runs the
  subcommand — so config-aware commands like `import` work. No hooks run.
- **Credentials follow the command.** The claim tells the worker the job's credential phase
  (`cred_phase`):

    | | Read-only command | Mutating command |
    |---|---|---|
    | `cred_phase` | `plan` | `apply` |
    | OIDC role | the **plan** (read-only) role | the **apply** role |
    | State token | **`ro`**: reads state only, never locks (its backend has no lock address) | **`rw`**: may lock and write state |
    | Token lifetime | 6 h | the apply window (apply budget + grace, 20 min by default) |
    | Time budget | the agent's default cap (2 h) | the **apply budget** (`STACKD_APPLY_TIMEOUT_SECONDS`, 15 min by default) |

    A mutating command killed at its budget fails with an "exceeded the …s time budget" error. If
    its worker disappears, the run is only reclaimed (`worker_lost`) after the apply window, by
    which time its tokens have expired — two workers never write the env's state at once. See
    [Workers & scaling](workers-and-scaling.md#worker-loss) and
    [Cloud credentials](cloud-credentials.md).
- **Audited**: `run.command_triggered` on trigger and `run.command_executed` on success; a failure
  is `run.apply_failed`.

!!! warning
    `import` needs to *read* the real resource, so the assumed role must have access to it. For an
    existing stack you're adopting, pair this with [Importing an existing stack](importing-existing-state.md).

## See also

- [Runs & approvals](runs-and-approvals.md) · [Importing an existing stack](importing-existing-state.md)
- [SPECS §4.3](../SPECS.md)
