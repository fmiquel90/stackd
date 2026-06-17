# SPECS_A_UPDATE.md — VCS feedback loop (Phase A)

> Plan: `PLAN_A_UPDATE.md`. Folds into `docs/SPECS.md` when shipped. Conventions unchanged (UUIDv7,
> `timestamptz` UTC `_at`, RFC 9457, Pydantic ≠ ORM, state only via `transition()`, audit in tx,
> secrets never logged). **Decision: PAT** (the stack's repo token), not a GitHub App — see bottom.

## Data model
```
runs  (add)
  pr_number        int  null        -- the PR that spawned a `proposed` run
  vcs_provider     text null        -- 'github' (enum-by-string; gitlab/bitbucket later)
  vcs_comment_id   bigint null      -- the posted PR comment, for idempotent edit
  vcs_head_sha     text null        -- PR head commit the status is reported against
```

## Auth — PAT
Reuse the stack's `repo_secret` (already used to clone). For post-back it must additionally carry
**`pull_requests:write` + commit `statuses:write`** (classic `repo`, or a fine-grained PAT with
those + `contents:read`). No GitHub App, no extra instance config. Posts appear as the token's user.
A `repo_secret` without write scope → post-back fails soft (run unaffected, logged + run warning).

## Webhook ingestion (`webhooks/router.py`)
On `pull_request` (`opened`/`synchronize`/`reopened`): create the `proposed` run as today **and**
persist `pr_number`, `vcs_provider='github'`, `vcs_head_sha = pr.head.sha`. On `closed`: best-effort
discard the still-in-flight proposed run for that PR.

## Post-back (new `app/vcs/` module — transactional outbox, like notifications §17)
Enqueued on the run `transition()` in the SAME txn (**no network I/O there** — a rolled-back
transition never posts); drained by the scheduler dispatcher (best-effort, retried). Only runs with
`vcs_provider` set (PR-originated `proposed` runs) post back. A `proposed` run is **plan-only and
terminal at `finished`** (never `unconfirmed`). Mapping:

- **Commit status** (Status API — PAT-compatible) on `vcs_head_sha`:
  `queued|preparing|planning|checking → pending`; `finished → success` ("plan ready — review", or a
  `success` with a "⚠ checks" note if a `warn` after_plan check fired); `failed → failure`;
  `canceled|discarded → error`.
  `POST /repos/{o}/{r}/statuses/{sha}` with `{state, target_url=<ui>/runs/{id}, context="stackd/plan",
  description="+a ~c −d"}`. (The richer **Checks API** is App-only → deferred.)
- **PR comment**: one comment per run, **edited in place** (`vcs_comment_id`): the `+a ~c −d`
  summary, mocked/fallback badges, after_plan check results, deep link to `/runs/{id}`. Create on
  first plan completion (`POST .../issues/{pr}/comments`), update on the terminal transition
  (`PATCH .../issues/comments/{id}`).
- A VCS failure **never** fails the run (logged + surfaced as a run warning).

## Endpoints
- `POST /api/v1/webhooks/github` — unchanged contract, now also persists PR metadata.
- `POST /api/v1/runs/{id}/vcs/resync` (writer) — re-post the status/comment (manual recovery).

## Migration
`runs.pr_number, vcs_provider, vcs_comment_id, vcs_head_sha` (next free revision at impl time).

## Invariants
Post-back is a side-effect of `transition()`, never a source of truth; a VCS outage leaves the run
correct. Sensitive plan values stay masked in the comment (reuse the artifact masking).

## Decisions (resolved)
- **PAT** (stack `repo_secret`) — fast, self-hosted-friendly. GitHub App (bot identity + Checks API,
  least-privilege short-lived tokens, can unify clone+webhook+post-back) is deferred to Phase I.
- **One comment edited in place** (not append) — less PR noise.
