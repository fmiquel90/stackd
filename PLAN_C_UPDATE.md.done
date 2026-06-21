# PLAN_C_UPDATE.md — Phase C: Security hardening (masking + runner trust)

> Status: **todo** · Prio P1 · Effort M · Risk M. Spec: `SPECS_C_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_C_UPDATE.md.done` (+ `SPECS_C_UPDATE.md.done`).

**Goal**: shrink the secret-leak surface and pin down the untrusted-code trust model.

- **In**:
  - **Masking**: env-kind secret values are **already** masked (`mask_values` covers every sensitive
    resolved value). Real adds: (a) a "suspicious cleartext" tripwire that flags a run if a known
    sensitive value appears un-transformed where it shouldn't (non-sensitive output, plan.json),
    (b) don't stream a raw `show` of sensitive attributes — rely on terraform's `(sensitive value)`.
  - **Runner trust**: formalize the `docker` runner contract — ephemeral per-job container, **no
    long-lived creds baked in**, OIDC token file mounted read-only + removed after, optional egress
    allowlist; assert (test) that repo hooks (`sh -c`) get `repo_env` only (no `AWS_*`/secret env).
- **Out**: full sandbox/microVM (gVisor/firecracker) — later option.
- **Touches**: `worker/agent/{masking,runner,main}.py`, deploy docs, `docs/SPECS.md` §5/§8/§13.
- **Acceptance**: tests prove (a) env secret values masked in logs, (b) a repo hook gets no
  `AWS_*`/sensitive env, (c) the docker runner leaves no creds/workspace behind.
