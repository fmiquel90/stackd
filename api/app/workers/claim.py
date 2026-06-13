from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt
from app.enums import (
    AuditActorKind,
    JobPhase,
    RepoAuthKind,
    RunEventActor,
    RunState,
    VariableKind,
)
from app.errors import ProblemException
from app.models.environment import Environment
from app.models.run import Run
from app.models.stack import Stack
from app.models.worker import Worker
from app.runs.transition import transition
from app.variables.resolution import provenance_snapshot, resolve_variables
from app.workers.hooks import platform_hooks

# Candidate selection (SPECS §7.2). FOR UPDATE OF e SKIP LOCKED serializes claims per environment;
# the one_active_run_per_env unique index (caught as 23505 below) is the real correctness guard.
_SELECT_CANDIDATE = text(
    """
    SELECT r.id AS run_id, r.state AS run_state
    FROM runs r
    JOIN environments e ON e.id = r.environment_id
    WHERE r.state IN ('queued', 'confirmed')
      AND e.locked = false
      -- NULLIF guards against JSONB 'null' (SQLAlchemy writes Python None as JSON null by default).
      AND COALESCE(NULLIF(e.labels, 'null'::jsonb), '{}'::jsonb) <@ CAST(:labels AS jsonb)
      AND (
        r.state = 'queued'
        OR r.worker_id = :wid
        OR r.confirmed_at < now() - make_interval(secs => :affinity)
      )
      AND NOT EXISTS (
        SELECT 1 FROM runs o
        WHERE o.environment_id = r.environment_id AND o.id <> r.id
          AND o.state IN ('preparing','planning','checking','unconfirmed','confirmed','applying')
      )
    ORDER BY (r.state = 'confirmed' AND r.worker_id = :wid) DESC, r.created_at
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
    return run


async def build_job_payload(session: AsyncSession, run: Run) -> dict:
    """Construct the claim payload (SPECS §7.2). Resolves variables with secrets revealed; the
    agent masks them in logs. Snapshots provenance onto the run."""
    env = await session.get(Environment, run.environment_id)
    assert env is not None
    stack = await session.get(Stack, env.stack_id)
    assert stack is not None

    resolved = await resolve_variables(session, env, reveal_sensitive=True)
    tfvars_json: dict[str, str | None] = {}
    env_vars: dict[str, str | None] = {}
    sensitive_env: dict[str, str | None] = {}
    for rv in resolved:
        if rv.kind == VariableKind.terraform:
            tfvars_json[rv.name] = rv.value
        elif rv.sensitive:
            sensitive_env[rv.name] = rv.value
        else:
            env_vars[rv.name] = rv.value

    # Inject upstream dependency outputs / mocks (§9.3): real value > mock > error.
    from app.audit import record_audit
    from app.dependencies.service import resolve_dependency_inputs

    dep = await resolve_dependency_inputs(session, env)
    for name, value in {**dep.resolved_inputs, **dep.mock_inputs}.items():
        tfvars_json[name] = value
    run.used_mocks = dep.used_mocks
    run.resolved_inputs = dep.resolved_inputs
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
    run.variable_provenance = provenance

    repo_credentials: dict[str, str | None] = {"kind": stack.repo_auth_kind.value}
    if stack.repo_auth_kind != RepoAuthKind.none and stack.repo_secret_encrypted is not None:
        repo_credentials["token"] = decrypt(stack.repo_secret_encrypted)

    phase = JobPhase.plan if run.state == RunState.preparing else JobPhase.apply

    # Managed state (§11): hand the worker a scoped HTTP backend (RO for proposed runs).
    backend = None
    if env.managed_state:
        from app.config import get_settings
        from app.enums import RunType
        from app.statebackend.tokens import mint_state_token

        scope = "ro" if run.type == RunType.proposed else "rw"
        base = get_settings().internal_url.rstrip("/")  # worker-reachable, not the public URL
        token = mint_state_token(
            environment_id=env.id, run_id=run.id, scope=scope, ttl_seconds=6 * 3600
        )
        addr = f"{base}/state/v1/{env.id}"
        backend = {
            "type": "http",
            "address": addr,
            "lock_address": f"{addr}/lock",
            "unlock_address": f"{addr}/lock",
            "lock_method": "LOCK",
            "unlock_method": "UNLOCK",
            "username": "env",
            "password": token,
        }

    return {
        "job_id": str(run.id),
        "phase": phase.value,
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
        # Literal secret values the agent masks in ALL log output before sending (§5.1).
        "mask_values": [rv.value for rv in resolved if rv.sensitive and rv.value],
        "hooks": await platform_hooks(session, stack_id=stack.id, env_id=env.id),
        "backend": backend,  # §11
        "cloud_credentials": await _cloud_credentials(session, env, stack, run, phase),  # §10
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
