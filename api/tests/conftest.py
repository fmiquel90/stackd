from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio


@pytest.fixture(scope="session")
def _database() -> Iterator[str]:
    """Real PostgreSQL 18 via testcontainers + Alembic migrations (CLAUDE §5 — no DB mocks)."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18", driver="asyncpg") as pg:
        os.environ["DATABASE_URL"] = pg.get_connection_url()
        os.environ.setdefault("STACKD_JWT_SECRET", "test-secret-of-sufficient-length-000000000")
        os.environ.setdefault("STACKD_ENCRYPTION_KEY", base64.b64encode(b"0" * 32).decode())
        os.environ["STACKD_DEV_AUTH"] = "true"
        os.environ["STACKD_ALLOWED_DOMAINS"] = "dev.local"
        os.environ.setdefault("AWS_REGION", "us-east-1")  # moto-friendly for state-backend tests
        os.environ["STACKD_RUN_SCHEDULER"] = "false"  # don't let the loop fail runs mid-test

        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        yield os.environ["DATABASE_URL"]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _seed(_database: str) -> None:
    # Default space must exist before stacks/variable sets can be created (SPECS §3.0).
    from app.seed import seed

    await seed()


@pytest_asyncio.fixture
async def client(_database: str) -> AsyncIterator[object]:
    import httpx
    from httpx import ASGITransport
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.main import app

    # The claim queue is global by design; clear runs so each test starts isolated.
    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM runs"))
        await session.commit()

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c
