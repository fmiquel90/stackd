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
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" -d '{
  "command": "import",
  "args": ["aws_s3_bucket.logs", "my-existing-bucket"]
}'

# remove a resource from state
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" -d '{
  "command": "state rm", "args": ["aws_instance.old"]
}'
```

The response is a normal run (`type: command`). It flows `queued → preparing → running →
finished/failed`, holds the **one-active-run-per-env** lock (no command races a plan/apply), streams
logs, and uses the managed state backend.

## What it runs with

- The worker clones the repo at the pinned commit, `init`s with the env's backend, then runs the
  subcommand — so config-aware commands like `import` work.
- **Credentials**: a mutating command gets the env's **apply** OIDC role; a read-only command gets
  the **plan** (read-only) role. See [Cloud credentials](cloud-credentials.md).
- **Audited**: `run.command_triggered` on trigger and `run.command_executed` on success.

!!! warning
    `import` needs to *read* the real resource, so the assumed role must have access to it. For an
    existing stack you're adopting, pair this with [Importing an existing stack](importing-existing-state.md).

## See also

- [Runs & approvals](runs-and-approvals.md) · [Importing an existing stack](importing-existing-state.md)
- [SPECS §4.3](../SPECS.md)
