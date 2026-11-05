# Exit & data portability

Stackd is designed for **low lock-in**. It's self-hosted (no SaaS holds your data), your IaC stays
in your Git repos, your state is plain `tfstate`, and your cloud access is standard IAM. The real
question isn't "vendor lock-in" — it's switching cost, and it's small. This page documents exactly
how to leave and get everything back.

## What is never captive

| Data | Where it lives | Getting it back |
|---|---|---|
| **Your IaC code** | your Git repos | Stackd only references `repo_url@commit` — the code never moved. |
| **State (`managed_state: false`)** | your own S3/backend | Stackd never touches it. Stop using Stackd; `tofu` keeps working against your backend. **No migration.** |
| **State (`managed_state: true`)** | Stackd's S3, **standard tfstate format** | Pull it out via the HTTP backend (below). No proprietary format. |
| **Cloud credentials** | your cloud account (IAM OIDC provider + roles) | Standard IAM — delete or repoint anytime. |
| **Repo hooks** | your repo (`.stackd.yml`) | Versioned with your code. |

## What lives only in Stackd (but is exportable)

- **Orchestration config** — stacks, environments, tiers, dependencies, variable sets. Exportable
  via the API (`GET /stacks`, `/stacks/{id}/environments`, `/environments/{id}`,
  `/environments/{id}/dependencies`, `/graph`, `/variable-sets`,
  `/environments/{id}/resolved-variables`). Re-creatable; it isn't infrastructure.
- **History** — runs, logs, audit. Runs list per environment (`GET /environments/{id}/runs`); a
  run's logs download as plain text (`GET /runs/{id}/logs/download`), and once archived they also
  sit in your bucket under `logs/<run_id>/<phase>.log.gz`. The audit trail exports to CSV
  (`GET /api/v1/audit/export`, admin, same filters as `GET /audit`). Export it before it ages out
  if your instance purges it (`AUDIT_RETENTION_DAYS`, 730 days, with a purge role configured).
- **Sensitive variable values** — write-only by design (AES-GCM, never returned in clear). Keep
  your own source of truth for secrets — you should anyway; secrets read from an
  [external secret source](external-secrets.md) already live in your own secret manager.
  Non-sensitive values come back via `/resolved-variables` (sensitive ones come back masked).

!!! tip "Want zero state exit cost?"
    Stay on `managed_state: false`. You get the full orchestration (plan → approval → apply, audit,
    OIDC, cascade) while your state stays entirely in your own backend — nothing to migrate if you
    ever leave.

## The exit procedure (rollback)

This is the exact reverse of [importing an existing stack](importing-existing-state.md).

### 1. Pull managed state back out

Only for environments with `managed_state: true`. Mint a session (admin; it returns a scoped `rw`
token + the HTTP backend config, valid 30 minutes). In a local checkout, add a
`terraform { backend "http" {} }` block, export the session's settings as `TF_HTTP_*` variables
(exactly as in [importing, step 3](importing-existing-state.md)) and run `tofu init`. Then:

```bash
# get a scoped token + backend address
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/state/import-session -H "$AUTH"

# option A — pull the raw tfstate (it's standard) and push it anywhere
tofu state pull > terraform.tfstate

# option B — migrate in place: replace the `http` backend block with your own
# (`s3`, local…), then:
tofu init -migrate-state
```

The state versions are also plain tfstate objects in Stackd's bucket, at
`states/<environment_id>/<version_id>.tfstate` — readable with your own S3 credentials, no API
involved. `GET /api/v1/environments/{id}/state/versions` lists them (id, serial, lineage), newest
first.

### 2. Repoint terraform / CI at the repos directly

Your code never moved — point your existing CI or local `tofu` at the repos and backends and you're
running on your own again.

### 3. Re-supply variables & secrets

Export the non-sensitive resolved variables; re-provide sensitive ones from your own secret store
(Stackd can't hand them back in clear — by design).

### 4. (Optional) Archive config & audit

```bash
curl -s localhost:8000/api/v1/audit/export -H "$AUTH" > audit.csv
curl -s localhost:8000/api/v1/stacks -H "$AUTH" > stacks.json
```

### 5. Decommission

Delete the IAM OIDC provider + roles in your cloud account if you no longer want them, then stop
Stackd.

## The bottom line

At any moment, **`tofu` alone against your state is enough to take back control**. The only real
exit work is migrating managed state (one standard `tofu init -migrate-state`, fully reversible) and
re-creating the orchestration config elsewhere — neither touches your infrastructure.

## See also

- [Importing an existing stack](importing-existing-state.md) · [Running commands](commands.md)
- [SPECS §11](../SPECS.md)
