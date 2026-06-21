# SPECS_C_UPDATE.md — Security hardening (Phase C)

> Plan: `PLAN_C_UPDATE.md`. Folds into `docs/SPECS.md` (§5/§8/§13) when shipped. No schema change.

## Masking (`worker/agent/masking.py`, `claim.py`)
- **Already covered, keep**: `mask_values` (claim.py) is built from *every* sensitive resolved value
  (`rv.sensitive`, all kinds — incl. env-kind secrets) plus the backend password, OIDC token and repo
  token. No gap there. The residual gap is a *transformed* secret (base64/substring) and a
  non-sensitive output that echoes a secret.
- **Cleartext tripwire** (the real add): after a phase, if a known sensitive value appears verbatim
  where it shouldn't (a non-sensitive output, or `plan.json` outside an expected field), flag the run
  with a `secret_leak_suspected` warning. Default = warn (configurable to hard-fail — open decision).
- Don't stream a raw `show` of sensitive attributes — rely on terraform's `(sensitive value)`.
- Documented residual limit (kept): value-based masking can't catch a *transformed* secret. The
  tripwire narrows, doesn't eliminate, this.

## Runner trust model (`worker/agent/runner.py`, `main.py`, deploy)
- **`docker` runner contract** (prod): one ephemeral container per job; image carries **no long-lived
  cloud creds**; the OIDC token is written to a 0600 file, mounted read-only, deleted in `finally`;
  workspace removed after the job; optional egress allowlist via the run network.
- **Repo hooks run untrusted**: `sh -c <repo command>` receives `repo_env` only — **no `AWS_*`, no
  sensitive vars, no cloud creds** (already the design, §8.3) — add a regression test asserting it.
- Dev (`local` runner + mounted `~/.aws`) is explicitly **"trusted dev only"** — documented, never
  prod.

## Open decision
Cleartext tripwire: **warn** (default) vs hard-fail the run.

## Invariants
Reinforces §13 (secrets) and §8.3 (hook isolation). No change to `can_apply`/four-eyes.
