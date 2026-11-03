"""Environment clones (SPECS §21.2): one environment declared across a space's stacks, modelled on
a reference environment — settings, governance and dependency edges — never its secrets."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.models.audit import AuditEvent
from tests.conftest_phase2 import login

PLAN_ROLE = "arn:aws:iam::123456789012:role/plan"


async def _space(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    r = await client.post("/api/v1/spaces", headers=admin, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stack(client: httpx.AsyncClient, admin: dict, space_id: str, name: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"space_id": space_id, "name": name, "repo_url": "file:///r", "tool_version": "1.12"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _env(client, admin, stack_id: str, name: str, **extra) -> str:
    body = {"name": name, "tier": "prod", "branch": "main", **extra}
    r = await client.post(f"/api/v1/stacks/{stack_id}/environments", headers=admin, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _edge(client, admin, down: str, up: str) -> None:
    refs = [{"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": "vpc-mock"}]
    r = await client.post(
        f"/api/v1/environments/{down}/dependencies",
        headers=admin,
        json={"upstream_env_id": up, "trigger_policy": "always", "references": refs},
    )
    assert r.status_code == 201, r.text


async def _world(client: httpx.AsyncClient, admin: dict, prefix: str) -> dict[str, str]:
    """network/prod → app/prod, and a shared dns/global → network/prod (dns is not cloned)."""
    ids = {"space": await _space(client, admin, prefix)}
    for st in ("network", "app", "dns"):
        ids[st] = await _stack(client, admin, ids["space"], f"{prefix}-{st}")
    ids["net_prod"] = await _env(
        client,
        admin,
        ids["network"],
        "prod",
        labels={"region": "eu-west-1", "team": "core"},
        protected=True,
    )
    ids["app_prod"] = await _env(client, admin, ids["app"], "prod", allow_mock_apply=True)
    ids["dns_global"] = await _env(client, admin, ids["dns"], "global")
    await _edge(client, admin, ids["app_prod"], ids["net_prod"])
    await _edge(client, admin, ids["net_prod"], ids["dns_global"])
    return ids


async def _clone(client, h, space_id: str, **body) -> httpx.Response:
    return await client.post(
        f"/api/v1/spaces/{space_id}/environment-clones",
        headers=h,
        json={"reference_name": "prod", "name": "prod-euw3", **body},
    )


async def test_clone_copies_settings_governance_and_edges(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-full")
    app_prod = ids["app_prod"]
    for var in (
        {"kind": "environment", "name": "LOG_LEVEL", "value": "info"},
        {"kind": "environment", "name": "DB_PASSWORD", "value": "s3cret", "sensitive": True},
    ):
        r = await client.post(f"/api/v1/environments/{app_prod}/variables", headers=admin, json=var)
        assert r.status_code == 201, r.text
    hook = {"stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "fail"}
    assert (
        await client.post(f"/api/v1/environments/{app_prod}/hooks", headers=admin, json=hook)
    ).status_code == 201
    vset = await client.post(
        "/api/v1/variable-sets", headers=admin, json={"name": "app-extra", "space_id": ids["space"]}
    )
    assert vset.status_code == 201, vset.text
    att = await client.post(
        f"/api/v1/variable-sets/{vset.json()['id']}/attachments",
        headers=admin,
        json={"target_kind": "environment", "target_id": app_prod, "priority": 5},
    )
    assert att.status_code == 201, att.text
    r = await client.put(
        f"/api/v1/environments/{app_prod}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": PLAN_ROLE},
    )
    assert r.status_code == 200

    r = await _clone(client, admin, ids["space"], overrides={"labels": {"region": "eu-west-3"}})
    assert r.status_code == 201, r.text
    out = r.json()
    new = {e["stack_name"].split("-")[-1]: e["id"] for e in out["environments"]}
    assert set(new) == {"network", "app"}  # dns has no `prod`: not a target

    net = (await client.get(f"/api/v1/environments/{new['network']}", headers=admin)).json()
    assert net["labels"] == {"region": "eu-west-3", "team": "core"}  # merged, override wins
    assert net["protected"] is True and net["tier"] == "prod"
    app = (await client.get(f"/api/v1/environments/{new['app']}", headers=admin)).json()
    assert app["allow_mock_apply"] is False  # never copied (§9.3)

    # Edges: app' ← network', network' ← the shared dns/global; nothing into the old graph.
    ups = {
        down: [
            d["upstream_env_id"]
            for d in (
                await client.get(f"/api/v1/environments/{down}/dependencies", headers=admin)
            ).json()
        ]
        for down in (new["app"], new["network"])
    }
    assert ups[new["app"]] == [new["network"]] and ups[new["network"]] == [ids["dns_global"]]

    variables = (
        await client.get(f"/api/v1/environments/{new['app']}/variables", headers=admin)
    ).json()
    assert [v["name"] for v in variables] == ["LOG_LEVEL"]  # the literal secret is not copied
    assert out["missing_secrets"] == [
        {"stack": "clone-full-app", "environment": "prod-euw3", "name": "DB_PASSWORD"}
    ]
    assert "s3cret" not in r.text
    assert {"stack": "clone-full-app", "environment": "prod-euw3", "item": "cloud integration"} in (
        out["to_configure"]
    )
    hooks = (await client.get(f"/api/v1/environments/{new['app']}/hooks", headers=admin)).json()
    assert [h["name"] for h in hooks] == ["tfsec"]
    atts = (
        await client.get(f"/api/v1/variable-sets/{vset.json()['id']}/attachments", headers=admin)
    ).json()
    assert {a["target_id"] for a in atts} == {app_prod, new["app"]}

    async with SessionLocal() as session:
        summary = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.action == "environment.cloned")
                )
            )
            .scalars()
            .all()
        )
    assert any((e.context or {}).get("name") == "prod-euw3" for e in summary)


async def test_clone_is_all_or_nothing(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-atomic")
    other = await _stack(client, admin, ids["space"], "clone-atomic-legacy")
    await _env(client, admin, other, "prod", managed_state=False)

    r = await _clone(client, admin, ids["space"])
    assert r.status_code == 422 and "clone-atomic-legacy" in r.text  # would share its state
    for stack in ("network", "app"):
        names = [
            e["name"]
            for e in (
                await client.get(f"/api/v1/stacks/{ids[stack]}/environments", headers=admin)
            ).json()
        ]
        assert "prod-euw3" not in names  # nothing was created anywhere

    ok = await _clone(client, admin, ids["space"], stack_ids=[ids["network"], ids["app"]])
    assert ok.status_code == 201, ok.text
    again = await _clone(client, admin, ids["space"], stack_ids=[ids["network"]])
    assert again.status_code == 422  # prod-euw3 already exists there
    missing = await _clone(client, admin, ids["space"], stack_ids=[ids["dns"]])
    assert missing.status_code == 422 and "clone-atomic-dns" in missing.text


async def test_clone_permissions(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-perms")
    bob = await login(client, "bob")
    users = (await client.get("/api/v1/users", headers=admin)).json()
    bob_id = next(u["id"] for u in users if u["email"] == "bob@dev.local")

    r = await client.put(
        f"/api/v1/spaces/{ids['space']}/members",
        headers=admin,
        json={"user_id": bob_id, "role": "reader", "allowed_tiers": []},
    )
    assert r.status_code in (200, 201), r.text
    assert (await _clone(client, bob, ids["space"])).status_code == 403  # a reader

    r = await client.put(
        f"/api/v1/spaces/{ids['space']}/members",
        headers=admin,
        json={"user_id": bob_id, "role": "writer", "allowed_tiers": []},
    )
    assert r.status_code in (200, 201), r.text
    denied = await _clone(client, bob, ids["space"], copy_cloud_integration=True)
    assert denied.status_code == 403  # copying role ARNs is an admin's call
    assert (await _clone(client, bob, ids["space"])).status_code == 201


async def test_dry_run_previews_without_creating(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-dry")
    r = await _clone(client, admin, ids["space"], dry_run=True)
    assert r.status_code == 201, r.text
    assert r.json()["dry_run"] is True and len(r.json()["environments"]) == 2
    assert len(r.json()["edges"]) == 2
    names = [
        e["name"]
        for e in (
            await client.get(f"/api/v1/stacks/{ids['app']}/environments", headers=admin)
        ).json()
    ]
    assert names == ["prod"]
