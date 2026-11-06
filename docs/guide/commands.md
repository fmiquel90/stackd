# Running commands (import, state surgery…)

Some operations don't fit `plan → apply`: importing an existing resource, removing something from
state, re-tainting a resource. Stackd runs these as a **command run** — one allowlisted
tofu/terraform subcommand executed on the worker, in the environment, with the same audit and the
managed backend as any other run.

!!! note
    This is **not** arbitrary shell. The worker runs `<tool> <command> <args>` where `command` is
    taken verbatim from an allowlist, and options are limited to a short per-command allowlist.
    To remove a stuck lock on **Stackd-managed state**, use the dedicated force-unlock (admin only:
    UI **State** tab → **Force unlock…**, or `DELETE /api/v1/environments/{id}/state/lock`); the
    `force-unlock` command is for environments with their **own** backend (see
    [below](#force-unlock-own-backend)).

## Allowed commands

| Read-only (needs `writer`) | Mutating (needs apply rights) |
|---|---|
| `output`, `show`, `state list`, `state show`, `validate`, `providers` | `import`, `state rm`, `state mv`, `taint`, `untaint`, `refresh`, `force-unlock` (also **admin**) |

**Mutating commands change real state**, so they're gated by `can_apply(user, env)` — the same role
+ tier check as approving an apply (see [Runs & approvals](runs-and-approvals.md)). Read-only
commands only need `writer`.

## Run one

From the UI: open an environment → **More → Run a command…** → pick a command, add arguments, run.
You land on the run page with live logs.

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

Arguments are passed verbatim, one list item each. A command outside the allowlist is refused
(`400 Command not allowed`).

### Allowed options

An argument starting with `-` is refused (`400 Argument not allowed`) unless the command's option
allowlist names it:

| Command | Allowed options |
|---|---|
| `refresh` | `-target=<address>` — repeatable (at most 50), same address rules as a [targeted plan](runs-and-approvals.md#targeted-and-replace-plans) |
| `state rm`, `state mv` | `-dry-run` — show what would change, without writing state |
| every other command | none |

Options that could print sensitive values (`-json`, `-raw`), point the tool elsewhere (`-state`,
`-chdir`, `-backend-config`), skip the lock (`-lock=false`) or inject variables (`-var`,
`-var-file`) are never accepted. Stackd puts the options before the positional arguments (where
tofu expects them), whatever order you send:

```bash
# preview a state rm
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" \
  -H 'content-type: application/json' -d '{
  "command": "state rm", "args": ["aws_instance.old", "-dry-run"]
}'   # stored and run as: state rm -dry-run aws_instance.old

# refresh two resources only
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" \
  -H 'content-type: application/json' -d '{
  "command": "refresh", "args": ["-target=aws_s3_bucket.logs", "-target=aws_iam_role.ci"]
}'
```

In the UI, **Run a command…** shows a *Dry run* checkbox for `state rm` / `state mv` and a
*Target addresses* box (one per line) for `refresh`.

### Force-unlock (own backend)

An environment whose state lives in its **own** backend (`managed_state=false`, e.g. S3 +
DynamoDB) can be left with a stale lock by a run that died mid-way, outside Stackd's view.
`force-unlock` releases it:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/commands -H "$AUTH" \
  -H 'content-type: application/json' -d '{
  "command": "force-unlock", "args": ["3f2b8c1e-5a4d-4e7f-9b0a-1c2d3e4f5a6b"]
}'
```

- **Exactly one argument**: the lock ID tofu printed in its "Error acquiring the state lock"
  message (letters, digits, `.`, `_`, `:`, `-`; `400` otherwise).
- **Admin only** (like the managed lock's endpoint), and it is a mutating command: apply rights
  on the environment too, and it runs with the apply credentials.
- The worker runs `<tool> force-unlock -force <LOCK_ID>` — it adds `-force` itself so tofu never
  prompts; you never pass a flag.
- On an environment with **Stackd-managed state** it is refused with `409 Managed state lock`:
  Stackd owns that lock — release it with `DELETE /api/v1/environments/{id}/state/lock` (UI
  **State** tab → **Force unlock…**).

!!! warning
    Only unlock a lock you know is stale: releasing the lock of a run that is still writing state
    lets two writers race. Stackd itself never runs two jobs on one environment at once.

The response is a normal run (`type: command`). It flows `queued → preparing → running →
finished/failed`, holds the **one-active-run-per-env** lock (no command races a plan/apply), streams
logs, and uses the managed state backend. It can be stopped like any run
([Runs & approvals](runs-and-approvals.md#the-lifecycle)): a stopped read-only command ends
`canceled`; a stopped mutating command ends `failed` (`canceled_during_apply`), and stopping it
needs apply rights.

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
