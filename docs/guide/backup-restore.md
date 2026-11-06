# Backup & restore

Stackd keeps everything that matters in **three places**, and a backup is only complete when it
covers all three:

| What | Where | Holds | Lose it and… |
|---|---|---|---|
| **Database** | PostgreSQL | all configuration (spaces, stacks, environments, variables, sets, hooks, dependencies, tiers, users, memberships), run history, the **audit trail**, the state *index* (which version is the latest, the lock), encrypted secrets, the local OIDC signing key | everything is gone but your code and your state files |
| **Object storage** | the S3 bucket (`S3_BUCKET`) | `states/` (every state version, plain tfstate), `logs/` (archived run logs), `artifacts/` (saved plans, transient) | managed-state environments lose their state — the worst case: the next plan wants to re-create everything |
| **Keys** | your secret manager / deployment config | `STACKD_ENCRYPTION_KEY` (master key), `STACKD_JWT_SECRET`, the KMS signing key (if `STACKD_OIDC_SIGNER=kms`), the Google OAuth client | see below — the master key is the one you can't lose |

!!! danger "The master key is part of the backup"
    `STACKD_ENCRYPTION_KEY` encrypts (AES-256-GCM) every sensitive variable, every repo token and
    webhook secret, every secret source credential, every static secret fallback, the local OIDC
    signing key and the saved plans. A database restored **without the same key** has all of them
    unreadable — there is no recovery path. Back it up, but **separately** from the database dumps:
    a dump and its key together are every secret in clear.

What you can lose without harm:

- `STACKD_JWT_SECRET` — it signs short-lived tokens only (sessions, worker tokens, per-job state
  tokens). A new value logs everyone out and makes in-flight jobs fail their next state call; the
  workers re-register by themselves (on a `401`) with their **pool token**, which is stored hashed
  in the database and survives.
- `artifacts/` — saved plans live only while their run can be applied; a restored run whose saved
  plan is gone fails closed (re-plan it).
- Hot log rows older than 7 days are already archived in `logs/`.

## What to back up, and how

### 1. PostgreSQL

Use what you already trust for Postgres. Two tiers:

- **Point-in-time recovery** (recommended in production): your managed service's automated
  backups (RDS/Aurora, Cloud SQL, Azure Database…) or WAL archiving (pgBackRest, WAL-G), retained
  at least as long as your audit requirements.
- **Logical dumps**, also as a portable second copy:

  ```bash
  # DATABASE_URL is SQLAlchemy's form (postgresql+asyncpg://…): drop the "+asyncpg" for libpq tools
  PGURL="${DATABASE_URL/+asyncpg/}"
  pg_dump --format=custom --no-owner --file="stackd-$(date -u +%Y%m%dT%H%M%SZ).dump" "$PGURL"
  # roles are cluster objects, not in pg_dump: keep their definitions too
  pg_dumpall --globals-only --no-role-passwords -d "$PGURL" > stackd-roles.sql
  ```

  The roles matter: the audit trail is append-only through grants (the application role holds
  `INSERT, SELECT` only on `audit_events`) and the purge goes through the separate
  `stackd_audit_purge` role ([SPECS §6.1](../SPECS.md)). Re-apply those grants after a restore.

### 2. The S3 bucket

- **Turn versioning on** (the API enables it when it creates the bucket itself; set it yourself on
  a bucket you provisioned). With versioning, a state version is never lost to an overwrite or a
  delete, and an older database restore still finds every object it points to.
- **Replicate or copy** it elsewhere: cross-region / cross-account replication on AWS S3, or a
  periodic copy for any S3-compatible store:

  ```bash
  aws s3 sync "s3://$S3_BUCKET" "s3://$BACKUP_BUCKET/stackd/" --exclude "artifacts/*"
  ```

- Keep the bucket's encryption (SSE-KMS or SSE-S3) on the copy, and restrict who can read it:
  **state files can contain secrets of their own** (any value a provider writes in clear).

### 3. The keys

Store `STACKD_ENCRYPTION_KEY` (and `STACKD_JWT_SECRET`, the Google client secret) in your secret
manager, with its own access control and its own backup. If you use the KMS signer, never schedule
the deletion of a KMS key still listed in the issuer's keys (`GET /api/v1/oidc/keys`): AWS keeps
validating tokens against the JWKS it fetched, and a retired key may still be needed until its last
tokens expired.

### Order and consistency

The database points to state objects in S3, never the reverse. **Back up the database first, then
the bucket**: a bucket copy newer than the dump only adds unreferenced objects (harmless), while a
bucket copy *older* than the dump would leave the database pointing at states that don't exist. With
bucket versioning on, any combination is consistent.

## Restoring

1. **Stop the API and the workers** (scale to zero). Nothing must claim or write while you restore.
2. **Restore the roles, then the database** into an empty database:

   ```bash
   psql "$PGURL_ADMIN" -f stackd-roles.sql          # then set the roles' passwords
   pg_restore --no-owner --dbname="$PGURL" stackd-<timestamp>.dump
   ```

   Re-apply the audit grants (`REVOKE UPDATE, DELETE ON audit_events FROM <app role>`).
3. **Restore the bucket** if it was lost — at least `states/` and `logs/`.
4. **Start the API with the same `STACKD_ENCRYPTION_KEY`.** If the image is newer than the backup,
   run the migrations first (`alembic upgrade head`), as for an upgrade.
5. **Start the workers.** They re-register on their own.
6. **Check before applying anything:**
   - runs that were executing at backup time are failed by the scheduler (`worker_lost`) after the
     usual windows; runs waiting for a human are still there — **discard** them rather than
     confirming a plan made against a world that has moved on;
   - a state lock held at backup time is still recorded: release it with **Force unlock** (admin,
     environment → *State*);
   - compare each managed-state environment's latest state version
     (`GET /api/v1/environments/{id}/state/versions`) with what you expect: **if an apply ran
     after the backup point, the restored database does not know its state version** — with
     versioning on, the object is still in the bucket under `states/<environment_id>/`; push it
     back with an import session ([Importing an existing stack](importing-existing-state.md))
     before any plan, or the next plan will try to re-create what that apply built;
   - let drift detection run, or trigger plans, and review them before the first apply.

!!! tip "Rehearse it"
    Restore into a scratch instance (its own database and bucket, the same master key) on a
    schedule. A backup you never restored is a hope, not a backup.

## Configuration export: the logical complement

Besides the physical backup, each space's configuration can be exported as a JSON document and
imported back ([SPECS §22](../SPECS.md)):

```bash
# export (space admin)
curl -s "$STACKD/api/v1/spaces/$SPACE_ID/export" -H "$AUTH" > space-prod.json

# preview an import, then apply it
curl -s -X POST "$STACKD/api/v1/spaces/$TARGET_SPACE_ID/import?dry_run=true" \
  -H "$AUTH" -H 'content-type: application/json' --data @space-prod.json | jq .summary
curl -s -X POST "$STACKD/api/v1/spaces/$TARGET_SPACE_ID/import" \
  -H "$AUTH" -H 'content-type: application/json' --data @space-prod.json > import-report.json
```

It is **not** a backup: it carries no secret (sensitive values, tokens, credentials are listed in
`missing_secrets`, never exported), no state, no run history, no audit, no users. What it is good
for:

- **Configuration as a reviewable file** — commit the export to Git: the next export's diff shows
  exactly what changed in the space (it is sorted and stable). Schedule it with a **service
  account** that is admin of the space ([API › Automation](../api.md#authentication)): its token
  can export, never confirm an apply.
- **Re-creating a space after a mistake** — the import merges by name: it re-creates what was
  deleted and resets what was changed, and **never deletes** anything that is not in the file.
- **Moving or cloning a space to another instance** — export → create the secret sources on the
  target (their credentials are not exported) → import with `dry_run=true`, read the report →
  import → fill `missing_secrets` (sensitive variables, repo tokens, webhook secrets) → move each
  managed-state environment's state with an import session, using the report's `id_map`
  (source environment id → new id) → only then let runs flow. A new environment declared with
  `autodeploy: true` stays off while secrets are missing.

### Reading an import report

```json
{
  "dry_run": false,
  "summary": {"created": 14, "updated": 1, "unchanged": 3, "skipped": 2},
  "changes": [{"op": "updated", "kind": "environment", "path": "stacks/network/environments/prod", "fields": ["branch"]}],
  "skipped": [{"kind": "variable", "path": "stacks/network/environments/prod/variables/terraform/db_password", "reason": "value_required"}],
  "missing_secrets": [{"path": "stacks/network", "item": "repo_secret"}],
  "warnings": [{"kind": "autodeploy_off", "path": "stacks/network/environments/dev", "reason": "left_to_fill"}],
  "id_map": {"stacks": {"…": "…"}, "environments": {"<source env id>": "<new env id>"}},
  "worker_pool_tokens": []
}
```

| `skipped.reason` | Meaning |
|---|---|
| `value_required` | a sensitive variable with no value in the file and none on the target: set it through the API or the UI (or fill `value` in the file by hand — it is then stored encrypted) |
| `secret_source_missing` / `bootstrap_secret_required` | create the secret source (with its credential) on the target first, then re-import |
| `instance_admin_required` | cloud integrations and worker pools need an instance admin; tiers too (refused with 403) |
| `target_not_found`, `downstream_not_found`, `upstream_not_found`, `upstream_forbidden` | an attachment or a dependency names something that is not there, or a space you can't write |
| `ambiguous_name`, `name_taken_in_another_space` | two hooks / notification targets share a name on the target; a worker pool name is used by another space |

## See also

- [Exit & data portability](leaving.md) · [Importing an existing stack](importing-existing-state.md)
- [SPECS §6.1 (audit), §11 (state), §13 (security), §22 (export/import)](../SPECS.md)
