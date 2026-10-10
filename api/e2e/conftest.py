"""End-to-end scenario harness (DEV §7).

Unlike the unit tests (testcontainers + in-process ASGI), this suite drives the LIVE compose
stack over HTTP and relies on a real worker container processing jobs against the shared Postgres.
Run it with `task e2e` (which brings up the stack, migrates, seeds fixtures + demo graph).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

BASE_URL = os.environ.get("STACKD_E2E_BASE_URL", "http://localhost:8000")

# The demo environments the scenario drives.
SCENARIO_ENVS = ("demo-network/dev", "demo-network/prod", "demo-app/dev", "demo-app/prod")
_TERMINAL = frozenset({"finished", "failed", "discarded", "canceled"})
# The only states the public API can end a run from (SPECS §4.1, runs.service): a queued run is
# canceled before it ever runs, an unconfirmed plan is discarded. Every other non-terminal state
# is in flight and owned by a worker — the API refuses to cancel it.
_END_ACTION = {"queued": "cancel", "unconfirmed": "discard"}
_RESET_HINT = "Run `task reset` to start again from a clean database."


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


async def _login(http: httpx.AsyncClient, persona: str) -> dict[str, str]:
    r = await http.post("/api/v1/auth/dev/login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def idle_demo_envs(http: httpx.AsyncClient, envs: dict[str, str]) -> None:
    """Let the scenario re-run on a used database: end the runs an earlier attempt left active on
    the demo envs. A complete attempt leaves its cascade plans on demo-app/dev and demo-app/prod
    `unconfirmed`, an active state (one active run per env, SPECS §3.5): the next cascade run would
    sit in `queued` behind it until the scenario times out.

    Ends them through the public API, as alice (approver; discard/cancel need writer, and alice is
    who discards in the scenario) — never in the DB, so every end goes through `transition()` and
    is audited (CLAUDE invariants 1-2). Fails fast, before changing anything, when the envs cannot
    be made ready: never waits for a run to settle."""
    alice = await _login(http, "alice")

    # Step 4 plans demo-app/prod on the MOCK, which needs demo-network/prod never applied (real
    # value > mock, SPECS §9.3). No API clears an environment's outputs once captured.
    r = await http.get(f"/api/v1/environments/{envs['demo-network/prod']}/outputs", headers=alice)
    assert r.status_code == 200, r.text
    if any(o["name"] == "network_name" for o in r.json()):
        pytest.fail(
            "demo-network/prod already has a `network_name` output (an earlier attempt "
            "applied it), so step 4 can no longer plan demo-app/prod on the mock, and no API "
            f"clears an environment's outputs. {_RESET_HINT}",
            pytrace=False,
        )

    leftovers: list[tuple[str, dict[str, Any]]] = []
    for key in SCENARIO_ENVS:
        r = await http.get(f"/api/v1/environments/{envs[key]}/runs", headers=alice)
        assert r.status_code == 200, r.text
        leftovers += [(key, run) for run in r.json() if run["state"] not in _TERMINAL]

    in_flight = [
        f"{key}: run {run['id']} is {run['state']}"
        for key, run in leftovers
        if run["state"] not in _END_ACTION
    ]
    if in_flight:
        pytest.fail(
            "Runs left by an earlier attempt are in flight; the API can only end queued or "
            "unconfirmed runs:\n  " + "\n  ".join(in_flight) + f"\n{_RESET_HINT}",
            pytrace=False,
        )

    # Queued runs first: ending an env's unconfirmed run frees its slot, and the worker would claim
    # the run queued behind it before it could be canceled.
    for key, run in sorted(leftovers, key=lambda kr: kr[1]["state"] != "queued"):
        action = _END_ACTION[run["state"]]
        r = await http.post(f"/api/v1/runs/{run['id']}/{action}", headers=alice)
        if r.status_code != 200:
            pytest.fail(
                f"Could not {action} run {run['id']} left {run['state']} on {key}: "
                f"HTTP {r.status_code} {r.text}\n{_RESET_HINT}",
                pytrace=False,
            )
