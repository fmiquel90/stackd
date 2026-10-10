"""End-to-end scenario harness (DEV §7).

Unlike the unit tests (testcontainers + in-process ASGI), this suite drives the LIVE compose
stack over HTTP and relies on a real worker container processing jobs against the shared Postgres.
Run it with `task e2e` (which brings up the stack, migrates, seeds fixtures + demo graph).

The scenario never touches the seeded demo graph: `graph` builds a throwaway copy of it per run
(`e2e-<ts>-<tag>-network` / `-app`, same space, repos, tiers, settings, dependency + mock)
through the public API, so every run starts from never-applied environments and the suite is
re-runnable on a used database without `task reset`.
"""

from __future__ import annotations

import os
import secrets
import warnings
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

BASE_URL = os.environ.get("STACKD_E2E_BASE_URL", "http://localhost:8000")

# The seeded stacks the scenario's graph is copied from (DEV §7), and the prefix of the copies.
DEMO_STACKS = ("demo-network", "demo-app")
_DEMO_PREFIX = "demo-"
_TERMINAL = frozenset({"finished", "failed", "discarded", "canceled"})
# The only states the public API can end a run from (SPECS §4.1, runs.service): a queued run is
# canceled before it ever runs, an unconfirmed plan is discarded. Every other non-terminal state
# is in flight and owned by a worker; the API refuses to cancel it.
_END_ACTION = {"queued": "cancel", "unconfirmed": "discard"}

_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Keep the test's call-phase report, so `graph`'s teardown knows whether the scenario
    passed."""
    report = yield
    if report.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as c:
        yield c


async def _login(http: httpx.AsyncClient, persona: str) -> dict[str, str]:
    r = await http.post("/api/v1/auth/dev/login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@dataclass
class _Blueprint:
    """The seeded demo graph, as the create payloads that rebuild it."""

    stacks: dict[str, dict[str, Any]] = field(default_factory=dict)  # name → StackCreate body
    envs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # stack → EnvCreate bodies
    # (upstream "stack/env", downstream "stack/env", DependencyCreate body minus upstream_env_id)
    deps: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # (variable_set_id, target "stack" or "stack/env", AttachmentCreate body minus target_id)
    attachments: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


async def _read_demo_blueprint() -> _Blueprint:
    """Read the seeded demo graph straight from the live database, read-only: the API hides
    output-reference mock values, and the seed is what the copy must match. Every write goes
    through the public API."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.dependencies.schemas import OutputReferenceIn
    from app.environments.schemas import EnvironmentCreate
    from app.models.dependency import EnvDependency, OutputReference
    from app.models.environment import Environment
    from app.models.space import Space
    from app.models.stack import Stack
    from app.models.variable_set import VariableSetAttachment
    from app.stacks.schemas import StackCreate

    bp = _Blueprint()
    async with SessionLocal() as s:
        space = (await s.execute(select(Space).where(Space.name == "demo"))).scalar_one_or_none()
        assert space is not None, "demo space not seeded — run `task seed`"
        stacks = (
            (
                await s.execute(
                    select(Stack).where(Stack.space_id == space.id, Stack.name.in_(DEMO_STACKS))
                )
            )
            .scalars()
            .all()
        )
        assert {st.name for st in stacks} == set(DEMO_STACKS), "demo graph not seeded"
        # A repo secret is write-only (CLAUDE invariant 3): the copy could not carry it.
        assert all(st.repo_secret_encrypted is None for st in stacks), "seeded repo secret"
        names: dict[Any, str] = {}  # stack or env id → "stack" / "stack/env"
        for st in stacks:
            names[st.id] = st.name
            # The fields a create carries (space, repo, project root, tool, labels…), as seeded.
            body = {f: getattr(st, f) for f in StackCreate.model_fields if f != "repo_secret"}
            bp.stacks[st.name] = {**body, "space_id": str(st.space_id)}
            envs = (
                (await s.execute(select(Environment).where(Environment.stack_id == st.id)))
                .scalars()
                .all()
            )
            bp.envs[st.name] = []
            for e in envs:
                names[e.id] = f"{st.name}/{e.name}"
                bp.envs[st.name].append({f: getattr(e, f) for f in EnvironmentCreate.model_fields})

        env_ids = [i for i, n in names.items() if "/" in n]
        for dep in (
            (
                await s.execute(
                    select(EnvDependency).where(EnvDependency.downstream_env_id.in_(env_ids))
                )
            )
            .scalars()
            .all()
        ):
            assert dep.upstream_env_id in names, "demo dependency from outside the demo graph"
            refs = (
                (
                    await s.execute(
                        select(OutputReference).where(OutputReference.dependency_id == dep.id)
                    )
                )
                .scalars()
                .all()
            )
            body = {
                "trigger_policy": dep.trigger_policy.value,
                "references": [
                    {f: getattr(r, f) for f in OutputReferenceIn.model_fields} for r in refs
                ],
            }
            bp.deps.append((names[dep.upstream_env_id], names[dep.downstream_env_id], body))

        for a in (
            (
                await s.execute(
                    select(VariableSetAttachment).where(
                        VariableSetAttachment.target_id.in_(list(names))
                    )
                )
            )
            .scalars()
            .all()
        ):
            body = {"target_kind": a.target_kind.value, "priority": a.priority}
            bp.attachments.append((str(a.variable_set_id), names[a.target_id], body))
    return bp


@dataclass
class _Graph:
    stack_ids: list[str] = field(default_factory=list)
    attachments: list[tuple[str, str]] = field(default_factory=list)  # (set id, attachment id)
    envs: dict[str, str] = field(default_factory=dict)  # "network/dev" → env id


async def _build(
    http: httpx.AsyncClient, h: dict[str, str], bp: _Blueprint, prefix: str, g: _Graph
) -> None:
    """Create the copy through the public API, recording each piece in `g` as it lands, so the
    teardown still knows what exists after a failed build."""
    ids: dict[str, str] = {}  # blueprint name ("demo-app", "demo-app/dev") → new id
    for name, body in bp.stacks.items():
        new_name = f"{prefix}-{name.removeprefix(_DEMO_PREFIX)}"
        r = await http.post("/api/v1/stacks", headers=h, json={**body, "name": new_name})
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]
        g.stack_ids.append(ids[name])
        for env in bp.envs[name]:
            r = await http.post(f"/api/v1/stacks/{ids[name]}/environments", headers=h, json=env)
            assert r.status_code == 201, r.text
            ids[f"{name}/{env['name']}"] = r.json()["id"]
            g.envs[f"{name.removeprefix(_DEMO_PREFIX)}/{env['name']}"] = r.json()["id"]
    for upstream, downstream, body in bp.deps:
        r = await http.post(
            f"/api/v1/environments/{ids[downstream]}/dependencies",
            headers=h,
            json={**body, "upstream_env_id": ids[upstream]},
        )
        assert r.status_code == 201, r.text
    for set_id, target, body in bp.attachments:
        r = await http.post(
            f"/api/v1/variable-sets/{set_id}/attachments",
            headers=h,
            json={**body, "target_id": ids[target]},
        )
        assert r.status_code == 201, r.text
        g.attachments.append((set_id, r.json()["id"]))


async def _end_active_runs(
    http: httpx.AsyncClient, h: dict[str, str], env_ids: dict[str, str]
) -> list[str]:
    """End what the scenario left active, through the API (audited `transition()`s). Returns
    the runs still not terminal (in flight, or lost a race with the worker)."""
    active: list[tuple[str, dict[str, Any]]] = []
    for key, env_id in env_ids.items():
        r = await http.get(f"/api/v1/environments/{env_id}/runs", headers=h)
        assert r.status_code == 200, r.text
        active += [(key, run) for run in r.json() if run["state"] not in _TERMINAL]
    left: list[str] = []
    # Queued runs first: ending an env's unconfirmed run frees its slot, and the worker would claim
    # the run queued behind it before it could be canceled.
    for key, run in sorted(active, key=lambda kr: kr[1]["state"] != "queued"):
        action = _END_ACTION.get(run["state"])
        if action is not None:
            r = await http.post(f"/api/v1/runs/{run['id']}/{action}", headers=h)
            if r.status_code == 200:
                continue
        left.append(f"{key}: run {run['id']} ({run['state']})")
    return left


def _keep_reason(built: bool, report: pytest.TestReport | None, left: list[str]) -> str | None:
    if not built:
        return "its build failed"
    if report is None or not report.passed:
        return "the scenario did not pass"
    if left:
        return f"runs still in flight: {left}"
    return None


async def _delete(http: httpx.AsyncClient, h: dict[str, str], g: _Graph) -> None:
    """Detach the variable sets (attachments point at their target by id, with no FK), then
    delete the stacks: the database cascades their envs, dependencies, runs and outputs."""
    for set_id, attachment_id in g.attachments:
        r = await http.delete(
            f"/api/v1/variable-sets/{set_id}/attachments/{attachment_id}", headers=h
        )
        assert r.status_code == 204, r.text
    for stack_id in g.stack_ids:
        r = await http.delete(f"/api/v1/stacks/{stack_id}", headers=h)
        assert r.status_code == 204, r.text


@pytest_asyncio.fixture
async def graph(
    http: httpx.AsyncClient, request: pytest.FixtureRequest
) -> AsyncIterator[dict[str, str]]:
    """A fresh copy of the seeded demo graph for this run, keyed `network/dev`, `network/prod`,
    `app/dev`, `app/prod`. Built and torn down as admin (instance admin, every space). It lives in
    the demo space, and permissions are per space (membership role, allowed tiers, destroy grant),
    so alice and bob hold exactly the rights the scenario relies on, as on the seeded stacks.

    Teardown ends what the scenario left active, then deletes the copy (cascading its envs, runs
    and outputs) once the scenario passed and nothing is in flight. Otherwise it keeps the copy
    for inspection and says so; its unique `e2e-` name never blocks a later run."""
    admin = await _login(http, "admin")
    bp = await _read_demo_blueprint()
    # Timestamp to find it again, plus a random tag so two copies never share a name.
    prefix = f"e2e-{datetime.now(UTC):%m%d%H%M%S}-{secrets.token_hex(2)}"
    g = _Graph()
    built = False
    try:
        await _build(http, admin, bp, prefix, g)
        built = True
        yield g.envs
    finally:
        left = await _end_active_runs(http, admin, g.envs)
        why = _keep_reason(built, request.node.stash.get(_CALL_REPORT, None), left)
        if why is not None:
            warnings.warn(f"kept the e2e graph `{prefix}-*` for inspection: {why}", stacklevel=1)
        else:
            await _delete(http, admin, g)
