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

Hand state to Stackd (state in S3 behind its HTTP backend, scoped per-run tokens, visible locking,
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

**3. Migrate your state** with the returned config. In your repo, switch the backend block to
`terraform { backend "http" {} }`, then run once:

```bash
tofu init -migrate-state \
  -backend-config="address=$ADDRESS" \
  -backend-config="lock_address=$ADDRESS/lock" \
  -backend-config="unlock_address=$ADDRESS/lock" \
  -backend-config="lock_method=LOCK" \
  -backend-config="unlock_method=UNLOCK" \
  -backend-config="username=env" \
  -backend-config="password=$TOKEN"
```

Terraform LOCKs, uploads your current state through Stackd's backend (stored as a `state_version`),
then UNLOCKs. Future runs use the managed backend automatically.

!!! warning
    The import session is **admin-only**, the token expires in 30 minutes, and creating it is
    audited (`state.import_session_created`). The endpoint refuses an env where `managed_state` is
    off (409).

## Which to choose?

- **Just want orchestration over your existing state?** → Path 1 (`managed_state: false`). Instant.
- **Want Stackd to own state (scoped tokens, visible locking)?** → Path 2, one `init -migrate-state`.

## See also

- [Stacks & environments](stacks-and-environments.md) · [Cloud credentials (OIDC)](cloud-credentials.md)
- [SPECS §11](../SPECS.md)
