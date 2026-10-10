from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crypto import decrypt
from app.enums import (
    ACTIVE_STATES,
    AuditActorKind,
    JobPhase,
    RepoAuthKind,
    RunEventActor,
    RunState,
    RunType,
    VariableKind,
)
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.worker import Worker
from app.runs.artifacts import SAVED_PLAN_TYPES
from app.runs.commands import is_mutating
from app.runs.transition import transition
from app.statebackend.tokens import StateScope, mint_state_token
from app.variables.resolution import ResolvedVariable, provenance_snapshot, resolve_variables
from app.workers.hooks import platform_hooks

# Derived from ACTIVE_STATES so the claim guard can never drift from the one_active_run_per_env
# index / the enum (a past bug: `running` was added to both but not here).
_ACTIVE_SQL = ", ".join(f"'{s.value}'" for s in ACTIVE_STATES)

# Candidate selection (SPECS §7.2). FOR UPDATE OF e SKIP LOCKED serializes claims per environment;
# the one_active_run_per_env unique index (caught as 23505 below) is the real correctness guard.
_SELECT_CANDIDATE = text(
    f"""
    SELECT r.id AS run_id, r.state AS run_state
    FROM runs r
    JOIN environments e ON e.id = r.environment_id
    WHERE r.state IN ('queued', 'confirmed')
      AND e.locked = false
      -- NULLIF guards against JSONB 'null' (SQLAlchemy writes Python None as JSON null by default).
      AND COALESCE(NULLIF(e.labels, 'null'::jsonb), '{{}}'::jsonb) <@ CAST(:labels AS jsonb)
      AND (
        r.state = 'queued'
        OR r.worker_id = :wid
        OR r.confirmed_at < now() - make_interval(secs => :affinity)
      )
      AND NOT EXISTS (
        SELECT 1 FROM runs o
        WHERE o.environment_id = r.environment_id AND o.id <> r.id
          AND o.state IN ({_ACTIVE_SQL})
      )
    -- Affinity first, then user runs ahead of background drift plans (§19), then FIFO.
    ORDER BY (r.state = 'confirmed' AND r.worker_id = :wid) DESC, r.is_drift, r.created_at
    FOR UPDATE OF e SKIP LOCKED
    LIMIT 1
    """
)


async def claim_one(session: AsyncSession, worker: Worker, affinity_seconds: int) -> Run | None:
    """Atomically claim the next eligible run for this worker, or return None."""
    row = (
        await session.execute(
            _SELECT_CANDIDATE.bindparams(
                wid=worker.id, labels=json.dumps(worker.labels or {}), affinity=affinity_seconds
            )
        )
    ).first()
    if row is None:
        return None

    run = await session.get(Run, row.run_id)
    assert run is not None
    target = RunState.preparing if run.state == RunState.queued else RunState.applying
    try:
        await transition(
            session,
            run,
            target,
            actor=RunEventActor.worker,
            actor_id=worker.id,
            fields={"worker_id": worker.id, "claimed_at": datetime.now(UTC)},
        )
    except (ProblemException, IntegrityError):
        # Lost the race (another worker won, or one_active_run_per_env 23505) → nothing to claim.
        await session.rollback()
        return None
    # Claim latency (§H): how long the run waited in queue before a worker picked it up.
    from app.observability import metrics

    metrics.claim_latency.observe((datetime.now(UTC) - run.created_at).total_seconds())
    return run


def holds_apply_credentials(run: Run) -> bool:
    """True when the run's claimed job holds apply (write) credentials: an apply, or a mutating
    command run (§4.3). The ONE definition of a "mutating job", shared by the job's credential
    phase (apply OIDC role + cap, apply-window state token) and the worker-lost reclaim cutoff
    (scheduler), so a job's write credentials expire by the time another run can take its env."""
    if run.type == RunType.command:
        return is_mutating((run.command or {}).get("name", ""))
    return run.state == RunState.applying


def _state_scope(run: Run, cred_phase: JobPhase) -> StateScope:
    """The job's state-token scope, bound at mint time (§11.2): only a job holding apply credentials
    writes state. A plan job reads and locks — `tofu plan` (`-refresh-only` included) takes the lock
    but never persists state. A proposed run (drift included) and a read-only command only read:
    they are handed no lock address, and `output`/`show`/`state list|show`/`providers`/`validate`
    never lock anyway."""
    if run.type == RunType.proposed:
        return "ro"
    if cred_phase == JobPhase.apply:
        return "rw"
    return "ro" if run.type == RunType.command else "plan"


def withholds_secrets(run: Run) -> bool:
    """True when the run's job gets none of the environment's secrets (§13): a proposed run. Its
    code is a pull request's, which nobody has reviewed, and repo code runs inside tofu (`data
    "external"`, provisioners) with everything tofu gets. A drift run is a proposed run too, but it
    plans the environment's last applied commit (§19.2) — code that already ran with those secrets
    — so it keeps them. The per-environment opt-in of §13 is not implemented: always withheld."""
    return run.type == RunType.proposed and not run.is_drift


def _is_secret(rv: ResolvedVariable) -> bool:
    """A sensitive variable, of either kind, or an external secret reference (§15.1: always
    sensitive, whatever its flag says)."""
    return rv.sensitive or rv.is_reference


def _is_hcl_tfvar(rv: ResolvedVariable) -> bool:
    """True when a terraform var must be written verbatim to the HCL tfvars file (§3.4, Phase D): an
    `hcl`-typed var with a value. It is then excluded from the JSON tfvars to avoid a double
    definition. Everything else (plain strings, dependency outputs) stays JSON-typed."""
    return rv.kind == VariableKind.terraform and rv.hcl and rv.value is not None


async def build_job_payload(session: AsyncSession, run: Run) -> dict:
    """Construct the claim payload (SPECS §7.2). Resolves variables with secrets revealed; the
    agent masks them in logs. Snapshots provenance onto the run. A proposed run gets no secret
    (§13, `withholds_secrets`)."""
    env = await session.get(Environment, run.environment_id)
    assert env is not None
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None

    withhold = withholds_secrets(run)
    resolved = await resolve_variables(session, env, reveal_sensitive=not withhold)
    if withhold:
        # Dropped before anything below sees them: no sensitive variable reaches `sensitive_env`,
        # the tfvars, `mask_values` or the provenance snapshot, and no external reference is
        # fetched. One the plan needs gets no value: tofu falls back to the variable's default in
        # the repo, or fails with "No value for required variable" — never a stand-in value.
        resolved = [rv for rv in resolved if not _is_secret(rv)]

    # Resolve external secret references (§15): fetch live values, or apply the configured fallback.
    # Raises SecretUnavailable (caught by the claim endpoint → run failed) when a reference can't
    # resolve and no fallback applies. Done before any run mutation so a failure leaves nothing half
    # written. Break-glass override values were sealed onto the run at trigger time (§15.4).
    from app.secret_sources.service import resolve_references

    overrides: dict[str, str] = {}
    if run.secret_overrides_encrypted is not None:
        overrides = json.loads(decrypt(run.secret_overrides_encrypted))
    secret_provenance = await resolve_references(session, run, resolved, overrides=overrides)

    # §15.5: the confirm-time fallback gate was checked against the plan-time resolution. If a
    # reference newly resolves via fallback at the apply claim (e.g. the provider was up at plan and
    # is down now), re-enforce the gate here so a non-real secret can't slip into an apply.
    if run.state == RunState.applying and run.used_secret_fallback and not env.allow_fallback_apply:
        from app.secret_sources.providers import SecretUnavailable

        raise SecretUnavailable("secret_unavailable:fallback_apply_disabled")

    tfvars_json: dict[str, object] = {}
    hcl_tfvars: dict[str, str] = {}  # §3.4: written verbatim to a generated HCL .auto.tfvars
    env_vars: dict[str, str | None] = {}
    sensitive_env: dict[str, str | None] = {}
    for rv in resolved:
        if _is_hcl_tfvar(rv):
            assert rv.value is not None
            hcl_tfvars[rv.name] = rv.value
        elif rv.kind == VariableKind.terraform:
            tfvars_json[rv.name] = rv.value
        elif _is_secret(rv):
            sensitive_env[rv.name] = rv.value
        else:
            env_vars[rv.name] = rv.value

    # Inject upstream dependency outputs / mocks (§9.3): real value > mock > error.
    from app.audit import record_audit
    from app.dependencies.service import DependencyError, resolve_dependency_inputs

    dep = await resolve_dependency_inputs(session, env)
    for name, value in {**dep.resolved_inputs, **dep.mock_inputs}.items():
        tfvars_json[name] = value
    run.used_mocks = dep.used_mocks
    run.resolved_inputs = dep.resolved_inputs

    # §9.3: re-enforce the mock-apply gate at the apply claim, mirroring the secret-fallback gate
    # above. The confirm-time gate checked the plan-time resolution; if a dependency input newly
    # falls back to a mock between confirm and apply (the upstream output expired or its env was
    # destroyed), a non-real value must not slip into an apply.
    if run.state == RunState.applying and dep.used_mocks and not env.allow_mock_apply:
        raise DependencyError("dependency_mock:mock_apply_disabled")
    if dep.consumed_mocks:
        await record_audit(
            session,
            action="dependency.mock_consumed",
            actor_kind=AuditActorKind.worker,
            target_kind="run",
            target_id=run.id,
            context={"refs": dep.consumed_mocks},
        )

    # Provenance snapshot (§3.4): layered resolution, then dependency/mock overrides.
    provenance = provenance_snapshot(resolved)
    provenance.update(dep.provenance)
    provenance.update(secret_provenance)  # §15: secret:/secret_fallback:/secret_override:
    run.variable_provenance = provenance

    # The repo token stays with a proposed run: it clones the pull request's commit, and reaches
    # neither tofu nor a hook (§8.3 — it is in no environment and nowhere under the checkout).
    repo_credentials: dict[str, str | None] = {"kind": stack.repo_auth_kind.value}
    if stack.repo_auth_kind != RepoAuthKind.none and stack.repo_secret_encrypted is not None:
        repo_credentials["token"] = decrypt(stack.repo_secret_encrypted)

    # An apply or a mutating subcommand gets the apply role; a plan or a read-only subcommand the
    # (read-only) plan role. A plan/apply job's type is its credential phase.
    cred_phase = JobPhase.apply if holds_apply_credentials(run) else JobPhase.plan
    phase = JobPhase.command if run.type == RunType.command else cred_phase

    # Managed state (§11): hand the worker an HTTP backend with a job-scoped state token.
    settings = get_settings()
    backend = None
    if env.managed_state:
        scope = _state_scope(run, cred_phase)
        base = settings.internal_url.rstrip("/")  # worker-reachable, not the public URL
        # A job holding apply credentials (an apply, or a mutating command) gets a state token that
        # lives only for the apply budget + grace — the same window as its OIDC cap and its
        # worker-lost reclaim cutoff (§4.2) — so a reaped/partitioned worker loses state-write
        # access before another run can take the env. Plan/read-only jobs keep the generous window.
        state_ttl = (
            settings.apply_reclaim_window_seconds if cred_phase == JobPhase.apply else 6 * 3600
        )
        token = mint_state_token(
            environment_id=env.id, run_id=run.id, scope=scope, ttl_seconds=state_ttl
        )
        addr = f"{base}/state/v1/{env.id}"
        backend = {"type": "http", "address": addr, "username": "env", "password": token}
        if scope != "ro":
            # A `ro` job gets no lock address: tofu's http backend then skips locking altogether
            # instead of sending a LOCK its token is refused (which failed every proposed plan).
            backend.update(
                lock_address=f"{addr}/lock",
                unlock_address=f"{addr}/lock",
                lock_method="LOCK",
                unlock_method="UNLOCK",
            )

    # §10: a proposed run keeps its OIDC credentials — those of the plan role, read-only by design
    # (§10.3: "a PR plan physically cannot modify the infra"), which a plan needs to refresh.
    cloud_credentials = await _cloud_credentials(session, env, stack, run, cred_phase)

    # Literal secret values the agent masks in ALL log output (§5.1) — sensitive variables, the
    # short-lived state-backend and OIDC tokens, and the repo auth token (it rides inside the clone
    # URL, so a failed `git clone` could otherwise echo it to logs).
    mask_values = [rv.value for rv in resolved if _is_secret(rv) and rv.value]
    if backend:
        mask_values.append(backend["password"])
    if cloud_credentials and cloud_credentials.get("oidc_token"):
        mask_values.append(cloud_credentials["oidc_token"])
    if repo_credentials.get("token"):
        mask_values.append(repo_credentials["token"])

    return {
        "job_id": str(run.id),
        "phase": phase.value,
        # The job's credential phase (§4.3/§7.2): "apply" when it holds apply credentials (an apply,
        # a mutating command), else "plan". Sent explicitly so the worker never re-derives it.
        "cred_phase": cred_phase.value,
        # Hard apply budget (§4.2): the worker kills `apply` — and a cred_phase=apply command — past
        # this. API-owned so the cap and the worker-lost reclaim window can never drift apart.
        "apply_timeout_seconds": settings.stackd_apply_timeout_seconds,
        "refresh_only": run.is_drift,  # §19: drift plan uses `-refresh-only` (state vs reality)
        # §7.4: a destroy run plans with `-destroy`; its saved plan carries that to the apply.
        "destroy": run.type == RunType.destroy,
        # §7.4: the plan job uploads its plan file (when it has changes) for every run that can
        # later be applied; the apply job downloads and applies exactly it.
        "save_plan": phase == JobPhase.plan and run.type in SAVED_PLAN_TYPES,
        "command": run.command,  # set only for RunType.command jobs
        "environment": {
            "id": str(env.id),
            "name": env.name,
            "stack_name": stack.name,
            "repo_url": stack.repo_url,
            "commit_sha": run.commit_sha,
            "branch": env.branch,
            "project_root": stack.project_root,
            "tool": stack.tool.value,
            "tool_version": stack.tool_version,
        },
        "repo_credentials": repo_credentials,
        "env": env_vars,
        "sensitive_env": sensitive_env,
        "tfvars_json": tfvars_json,
        "hcl_tfvars": hcl_tfvars,  # §3.4 — verbatim HCL values, written to a separate .auto.tfvars
        "mask_values": mask_values,
        "hooks": await platform_hooks(session, stack_id=stack.id, env_id=env.id),
        "backend": backend,  # §11 — the managed HTTP backend (None when unmanaged)
        # Unmanaged state only: a repo-relative `-backend-config=<file>` and/or inline key=value
        # `-backend-config` settings passed at init.
        "backend_config_file": None if env.managed_state else env.backend_config_file,
        "backend_config": None if env.managed_state else env.backend_config,
        "cloud_credentials": cloud_credentials,
        "resolved_inputs": {f"TF_VAR_{k}": v for k, v in dep.resolved_inputs.items()},
        "mock_inputs": {f"TF_VAR_{k}": v for k, v in dep.mock_inputs.items()},
    }


async def _cloud_credentials(session, env, stack, run, phase) -> dict | None:  # type: ignore[no-untyped-def]
    """OIDC workload credentials for the phase (§10): plan vs apply assume different roles."""
    from sqlalchemy import select

    from app.models.oidc import CloudIntegration
    from app.oidc.issuer import sign_workload_token

    ci = (
        await session.execute(
            select(CloudIntegration).where(CloudIntegration.environment_id == env.id)
        )
    ).scalar_one_or_none()
    if ci is None:
        return None
    token = await sign_workload_token(session, env, stack, run, phase, ttl=ci.session_duration)
    role_arn = ci.plan_role_arn if phase == JobPhase.plan else ci.apply_role_arn
    return {
        "provider": ci.provider.value,
        "oidc_token": token,
        "role_arn": role_arn,
        "region": ci.region,
    }
