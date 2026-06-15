from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import encrypt
from app.errors import ProblemException
from app.models.variable import Variable
from app.variables.schemas import VariableCreate, VariableUpdate
from app.variables.values import set_reference, store_value


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


def update_variable(var: Variable, body: VariableUpdate) -> None:
    if body.hcl is not None:
        var.hcl = body.hcl

    # Re-point at (or onto) an external secret reference (§15).
    if body.secret_source_id is not None:
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
