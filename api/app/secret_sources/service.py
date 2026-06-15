from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.crypto import decrypt
from app.enums import AuditActorKind, SecretFallback
from app.models.run import Run
from app.models.secret_source import SecretSource
from app.secret_sources import providers
from app.variables.resolution import ResolvedVariable


async def resolve_references(
    session: AsyncSession,
    run: Run,
    resolved: list[ResolvedVariable],
    *,
    overrides: dict[str, str],
) -> dict[str, str]:
    """Resolve every external-secret reference among `resolved` to its live value (§15.2).

    Precedence per reference: live provider value > configured fallback > error. A fallback (static
    value or break-glass override) flags `run.used_secret_fallback`, which blocks apply unless the
    environment opts in (§15.5). Mutates `rv.value` in place and returns the provenance overrides to
    fold into the run snapshot. Raises providers.SecretUnavailable when a reference cannot resolve
    and no fallback applies — the caller fails the run closed.
    """
    provenance: dict[str, str] = {}
    used_fallback = False
    # build_job_payload runs at both the plan and the apply claim; the run keeps the flag once set,
    # so we only emit fallback audit on the first pass that flips it (avoids a duplicate per claim).
    first_pass = not run.used_secret_fallback

    for rv in resolved:
        if not rv.is_reference:
            continue
        source = await session.get(SecretSource, rv.secret_source_id)
        assert source is not None  # FK ON DELETE RESTRICT guarantees the source still exists
        try:
            rv.value = await providers.fetch_secret(source, rv.secret_ref or "")
            provenance[rv.injected_name] = f"secret:{source.name}"
            continue
        except providers.SecretUnavailable as exc:
            unavailable = exc

        if rv.secret_fallback_mode == SecretFallback.static and rv.secret_fallback_encrypted:
            rv.value = decrypt(rv.secret_fallback_encrypted)
            provenance[rv.injected_name] = f"secret_fallback:{source.name}"
            used_fallback = True
            if first_pass:
                await record_audit(
                    session,
                    action="secret.fallback_used",
                    actor_kind=AuditActorKind.worker,
                    target_kind="run",
                    target_id=run.id,
                    context={"variable": rv.name, "source": source.name},
                )
        elif rv.secret_fallback_mode == SecretFallback.break_glass and rv.name in overrides:
            rv.value = overrides[rv.name]
            provenance[rv.injected_name] = f"secret_override:{source.name}"
            used_fallback = True
            if first_pass:
                await record_audit(
                    session,
                    action="secret.fallback_overridden",
                    actor_kind=AuditActorKind.worker,
                    target_kind="run",
                    target_id=run.id,
                    context={"variable": rv.name, "source": source.name},  # never the value (§6.1)
                )
        else:
            # Fail closed. The single run.apply_failed audit (written by the claim endpoint when it
            # catches this) owns the failure; we don't stage a second audit here, and the provider's
            # raw error never reaches the audit context — only a stable, var-scoped marker does.
            raise providers.SecretUnavailable(f"secret_unavailable:{rv.name}") from unavailable

    if used_fallback:
        run.used_secret_fallback = True
    return provenance
