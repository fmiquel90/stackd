# Importing an existing stack

Already running Terraform with remote state? You can onboard it into Stackd two ways, depending on
whether you want to keep your own backend or hand state over to Stackd.

## Path 1 — keep your backend (`managed_state: false`)

The zero-migration path. Set `managed_state: false` on the environment: Stackd injects nothing, your
repo keeps its existing `backend "s3"` (or other) block, and Terraform keeps using your remote state
exactly as before. Stackd still orchestrates the run (plan → approve → apply, audit, OIDC, cascade).

```bash
# create the env with managed state off
curl -s -X POST localhost:8000/api/v1/stacks/$STACK_ID/environments -H "$AUTH" -d '{
  "name": "prod", "tier": "prod", "branch": "main", "managed_state": false
}'
```

The first `plan` reads your real state — a **no-op diff confirms** the import is correct before you
apply anything.

!!! note
    The worker reaches your backend with the per-run [OIDC role](cloud-credentials.md), so that role
    must have access to your state bucket (and lock table, if any). You keep your own locking; you
    don't get Stackd's scoped state tokens / visible locking for this env.

## Path 2 — adopt Stackd-managed state (`managed_state: true`)

Hand state to Stackd (state in S3 behind its HTTP backend, per-job scoped tokens, visible locking,
serial-regression guard). Migrate your current state **once** with a standard
`tofu init -migrate-state` — Stackd hands you a scoped credential to do it.

**1. Set the environment to managed state** (`managed_state: true`).

**2. Create an import session** (admin) — mints a short-lived `rw` state token and a ready backend
config:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/state/import-session -H "$AUTH"
```
```json
{
  "expires_in": 1800,
  "current_serial": null,
  "backend": {
    "type": "http",
    "address": "https://stackd.example/state/v1/<env_id>",
    "lock_address": "https://stackd.example/state/v1/<env_id>/lock",
    "unlock_address": "https://stackd.example/state/v1/<env_id>/lock",
    "lock_method": "LOCK", "unlock_method": "UNLOCK",
    "username": "env", "password": "<scoped-token>"
  },
  "instructions": [ "..." ]
}
```

**3. Migrate your state** with the returned config. In a local checkout, switch the backend block
to `terraform { backend "http" {} }`, then run once:

```bash
TF_HTTP_ADDRESS="$ADDRESS" \
TF_HTTP_LOCK_ADDRESS="$ADDRESS/lock" \
TF_HTTP_UNLOCK_ADDRESS="$ADDRESS/lock" \
TF_HTTP_LOCK_METHOD=LOCK \
TF_HTTP_UNLOCK_METHOD=UNLOCK \
TF_HTTP_USERNAME=env \
TF_HTTP_PASSWORD="$TOKEN" \
tofu init -migrate-state
```

The settings go through `TF_HTTP_*` environment variables rather than `-backend-config`: tofu writes
`-backend-config` values, the token included, into `.terraform/`, while environment variables never
touch disk. Stackd's own workers do the same.

Terraform LOCKs, uploads your current state through Stackd's backend (stored as a `state_version`),
then UNLOCKs.

**4. Remove the backend block from the committed code.** On a managed-state environment the worker
adds its own `backend "http" {}` block and hands the address and its job token to tofu through
`TF_HTTP_*` environment variables, so the repo must declare **no** backend: a second backend block
makes `init` fail. Future runs then use the managed backend automatically.

!!! tip "Clean up the local checkout"
    Delete the local `.terraform/` once the migration is done, and don't commit the temporary
    `backend "http" {}` block (step 4).

!!! warning
    The import session is **admin-only**, the token expires in 30 minutes, and creating it is
    audited (`state.import_session_created`). The endpoint refuses an env where `managed_state` is
    off (409). The returned addresses use `STACKD_PUBLIC_URL`, since you run `tofu` from your own
    machine.

In the local dev stack, the imported state lands in the Garage bucket that `task dev` provisions;
`task s3-ls` lists it under `states/<environment_id>/`.

## Which to choose?

- **Just want orchestration over your existing state?** → Path 1 (`managed_state: false`). Instant.
- **Want Stackd to own state (scoped tokens, visible locking)?** → Path 2, one `init -migrate-state`.

## See also

- [Exit & data portability](leaving.md) — the reverse: getting your state back out.
- [Stacks & environments](stacks-and-environments.md) · [Cloud credentials (OIDC)](cloud-credentials.md)
- [SPECS §11](../SPECS.md)
