from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import encrypt
from app.errors import ProblemException
from app.models.secret_source import SecretSource
from app.models.stack import Stack
from app.models.variable import Variable
from app.models.variable_set import VariableSet
from app.variables.schemas import VariableCreate, VariableUpdate
from app.variables.values import set_reference, store_value


async def _require_source_in_space(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    stack_id: uuid.UUID | None,
    variable_set_id: uuid.UUID | None,
) -> None:
    """A variable references a secret source of its own space only (§15.1): the stack's, or the
    variable set's. Another space's source is answered like a missing one — 404, its existence
    not confirmed."""
    if variable_set_id is not None:
        vset = await session.get(VariableSet, variable_set_id)
        space_id = vset.space_id if vset is not None else None
    else:
        stack = await session.get(Stack, stack_id) if stack_id is not None else None
        space_id = stack.space_id if stack is not None else None
    source = await session.get(SecretSource, source_id)
    if source is None or space_id is None or source.space_id != space_id:
        raise ProblemException(
            404, "Secret source not found", "No such secret source in this variable's space."
        )


async def create_variable(
    session: AsyncSession,
    body: VariableCreate,
    *,
    stack_id: uuid.UUID | None = None,
    environment_id: uuid.UUID | None = None,
    variable_set_id: uuid.UUID | None = None,
) -> Variable:
    var = Variable(
        stack_id=stack_id,
        environment_id=environment_id,
        variable_set_id=variable_set_id,
        kind=body.kind,
        name=body.name,
        hcl=body.hcl,
    )
    if body.secret_source_id is not None:
        await _require_source_in_space(
            session, body.secret_source_id, stack_id=stack_id, variable_set_id=variable_set_id
        )
        set_reference(
            var,
            source_id=body.secret_source_id,
            secret_ref=body.secret_ref or "",
            fallback_mode=body.secret_fallback_mode,
            fallback_value=body.secret_fallback,
        )
    else:
        assert body.value is not None  # schema validator guarantees one source
        store_value(var, body.value, sensitive=body.sensitive)
    session.add(var)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemException(
            409,
            "Variable already exists",
            f"{body.kind.value} variable '{body.name}' is defined here.",
        ) from exc
    return var


async def get_variable(session: AsyncSession, var_id: uuid.UUID) -> Variable:
    var = await session.get(Variable, var_id)
    if var is None:
        raise ProblemException(404, "Variable not found", None)
    return var


async def update_variable(session: AsyncSession, var: Variable, body: VariableUpdate) -> None:
    if body.hcl is not None:
        var.hcl = body.hcl

    # Re-point at (or onto) an external secret reference (§15).
    if body.secret_source_id is not None:
        await _require_source_in_space(
            session,
            body.secret_source_id,
            stack_id=var.stack_id,
            variable_set_id=var.variable_set_id,
        )
        set_reference(
            var,
            source_id=body.secret_source_id,
            secret_ref=body.secret_ref or var.secret_ref or "",
            fallback_mode=body.secret_fallback_mode or var.secret_fallback_mode,
            fallback_value=body.secret_fallback,
        )
        return
    # Tune an existing reference in place (locator / fallback policy / value) without re-pointing.
    # Each field is only touched when supplied, so omitting `secret_fallback` keeps the stored one.
    if var.secret_source_id is not None and (
        body.secret_ref is not None
        or body.secret_fallback_mode is not None
        or body.secret_fallback is not None
    ):
        if body.secret_ref is not None:
            var.secret_ref = body.secret_ref
        if body.secret_fallback_mode is not None:
            var.secret_fallback_mode = body.secret_fallback_mode
        if body.secret_fallback is not None:
            var.secret_fallback_encrypted = encrypt(body.secret_fallback)
        return

    sensitive = body.sensitive if body.sensitive is not None else var.sensitive
    if var.secret_source_id is not None and body.sensitive is False:
        # An external reference is a secret by definition (§15): it is never shown nor masked off.
        raise ProblemException(
            422, "Secret reference", "A variable that references a secret source stays sensitive."
        )
    if var.sensitive and not sensitive and body.value is None:
        # Write-only (CLAUDE.md invariant 3): unflagging must replace the secret, never expose it —
        # a kept value would stop being masked in run logs and plan output.
        raise ProblemException(
            422,
            "New value required",
            "Turning a sensitive variable into a plain one requires a new value.",
        )
    if body.value is not None:
        store_value(var, body.value, sensitive=sensitive)
    elif body.sensitive is not None and body.sensitive != var.sensitive:
        # Toggling sensitivity without a new value: re-encrypt the existing plaintext when
        # turning a plain var into a secret; otherwise just flip the flag.
        if sensitive and var.value is not None:
            store_value(var, var.value, sensitive=True)
        else:
            var.sensitive = sensitive


async def variables_for(
    session: AsyncSession,
    *,
    stack_id: uuid.UUID | None = None,
    environment_id: uuid.UUID | None = None,
    variable_set_id: uuid.UUID | None = None,
) -> list[Variable]:
    stmt = select(Variable)
    if variable_set_id is not None:
        stmt = stmt.where(Variable.variable_set_id == variable_set_id)
    elif environment_id is not None:
        stmt = stmt.where(Variable.environment_id == environment_id)
    else:
        stmt = stmt.where(Variable.stack_id == stack_id, Variable.environment_id.is_(None))
    return list((await session.execute(stmt.order_by(Variable.name))).scalars().all())
