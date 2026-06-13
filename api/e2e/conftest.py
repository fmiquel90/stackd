"""End-to-end scenario harness (DEV §7).

Unlike the unit tests (testcontainers + in-process ASGI), this suite drives the LIVE compose
stack over HTTP and relies on a real worker container processing jobs against the shared Postgres.
Run it with `task e2e` (which brings up the stack, migrates, seeds fixtures + demo graph).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio

BASE_URL = os.environ.get("STACKD_E2E_BASE_URL", "http://localhost:8000")


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        yield c


@pytest_asyncio.fixture
async def envs() -> dict[str, str]:
    """Resolve the seeded demo environment ids by `<stack>/<env>` name, read straight from the
    live database (the stacks live in the `demo` space, which the public API does not list)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.environment import Environment
    from app.models.space import Space
    from app.models.stack import Stack

    async with SessionLocal() as s:
        demo = (await s.execute(select(Space).where(Space.name == "demo"))).scalar_one()
        stacks = {
            st.id: st.name
            for st in (await s.execute(select(Stack).where(Stack.space_id == demo.id))).scalars()
        }
        out: dict[str, str] = {}
        for e in (
            await s.execute(select(Environment).where(Environment.stack_id.in_(list(stacks))))
        ).scalars():
            out[f"{stacks[e.stack_id]}/{e.name}"] = str(e.id)
    assert out, "demo graph not seeded — run `task seed`"
    return out
