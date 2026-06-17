# PLAN_A_UPDATE.md — Phase A: VCS feedback loop (PR comment + commit status)

> Status: **todo** · Prio P1 · Effort M · Risk M. Spec: `SPECS_A_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename this file to `PLAN_A_UPDATE.md.done` (and `SPECS_A_UPDATE.md.done`).

**Goal**: a PR shows the plan result *in GitHub* — a commit **status** + a PR comment — closing the
review loop. Reuses the `proposed` run created on `pull_request` (`webhooks/router.py`).

- **In**: persist the PR number / head SHA on the run; a post-back service (transactional outbox →
  scheduler dispatch) that sets a **commit status** (Status API) and posts/edits **one PR comment**
  with the `+a ~c −d` summary + run link. Auth = the stack's **PAT** (`repo_secret`).
- **Out**: GitHub App + Checks API (bot identity, rich checks) — deferred; GitLab/Bitbucket
  (interface designed for it, GitHub first); inline plan-line comments.
- **Touches**: `webhooks/`, `runs/` (enqueue post-back in the transition outbox), new `vcs/` module,
  `scheduler/` (dispatch), 1 migration (`runs.pr_number`, `runs.vcs_*`).
- **Acceptance**: open a PR on a fixture repo → a pending status appears, a comment is posted, both
  update to success/failure when the proposed run reaches `finished`/`failed`. e2e extended with a
  mock VCS server.
