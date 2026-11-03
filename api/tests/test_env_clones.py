"""Environment clones (SPECS §21.2): one environment declared across a space's stacks, modelled on
a reference environment — settings, governance and dependency edges — never its secrets."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.environments.clones import CloneIn, _integrity_problem
from app.models.audit import AuditEvent
from app.models.environment import Environment
from app.models.hook import Hook
from app.models.variable import Variable
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
        {
            "stack": "clone-full-app",
            "environment": "prod-euw3",
            "name": "DB_PASSWORD",
            "falls_back_to": None,  # nothing below the env provides it: the input stays unset
        }
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


async def _bob_in(client, admin, space_id: str, role: str) -> dict:
    users = (await client.get("/api/v1/users", headers=admin)).json()
    bob_id = next(u["id"] for u in users if u["email"] == "bob@dev.local")
    r = await client.put(
        f"/api/v1/spaces/{space_id}/members",
        headers=admin,
        json={"user_id": bob_id, "role": role, "allowed_tiers": []},
    )
    assert r.status_code in (200, 201), r.text
    return await login(client, "bob")


async def test_a_space_admin_may_not_copy_cloud_integrations(client: httpx.AsyncClient) -> None:
    """Attaching role ARNs needs the instance admin role, as PUT …/cloud-integration does."""
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-spadmin")
    bob = await _bob_in(client, admin, ids["space"], "admin")
    r = await _clone(client, bob, ids["space"], copy_cloud_integration=True)
    assert r.status_code == 403 and "instance admin" in r.text


async def test_a_shared_upstream_needs_access_to_its_space(client: httpx.AsyncClient) -> None:
    """Wiring a new consumer of another space's outputs needs writer there too (§3.8)."""
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-xspace")
    other = await _space(client, admin, "clone-xspace-other")
    foreign = await _env(client, admin, await _stack(client, admin, other, "xs-dns"), "global")
    await _edge(client, admin, ids["net_prod"], foreign)  # by an admin of both spaces
    bob = await _bob_in(client, admin, ids["space"], "writer")
    r = await _clone(client, bob, ids["space"])
    assert r.status_code == 403
    assert (await _clone(client, admin, ids["space"])).status_code == 201


async def _audits(**where) -> list[AuditEvent]:
    async with SessionLocal() as session:
        q = select(AuditEvent)
        for k, v in where.items():
            q = q.where(getattr(AuditEvent, k) == v)
        return list((await session.execute(q)).scalars().all())


async def _set(client, admin, space_id: str, name: str, variables: dict[str, str], **extra) -> str:
    r = await client.post(
        "/api/v1/variable-sets", headers=admin, json={"name": name, "space_id": space_id, **extra}
    )
    assert r.status_code == 201, r.text
    for k, v in variables.items():
        added = await client.post(
            f"/api/v1/variable-sets/{r.json()['id']}/variables",
            headers=admin,
            json={"kind": "terraform", "name": k, "value": v},
        )
        assert added.status_code == 201, added.text
    return r.json()["id"]


async def _var(client, admin, env_id: str, name: str, value: str, **extra) -> None:
    r = await client.post(
        f"/api/v1/environments/{env_id}/variables",
        headers=admin,
        json={"kind": "terraform", "name": name, "value": value, **extra},
    )
    assert r.status_code == 201, r.text


async def test_null_overrides_are_rejected(client: httpx.AsyncClient) -> None:
    """Every override is optional but never null: a null tier would skip the tier check and break
    the NOT NULL column (it used to surface as a misleading 409 "Created concurrently")."""
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-null")
    for overrides in ({"tier": None}, {"branch": None}, {"protected": None}, {"labels": None}):
        r = await _clone(client, admin, ids["space"], overrides=overrides)
        assert r.status_code == 422, (overrides, r.text)
        assert r.headers["content-type"].startswith("application/problem+json")
    unknown = await _clone(client, admin, ids["space"], overrides={"tier": "nope"})
    assert unknown.status_code == 422 and "Unknown tier" in unknown.text
    envs = (await client.get(f"/api/v1/stacks/{ids['app']}/environments", headers=admin)).json()
    assert [e["name"] for e in envs] == ["prod"]


async def test_integrity_errors_are_named_accurately(client: httpx.AsyncClient) -> None:
    """Only the (stack, name) unique violation reads "created concurrently"; a tier deleted between
    the check and the insert is an unknown tier, anything else a generic conflict."""
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-integrity")
    body = CloneIn(reference_name="prod", name="prod-euw3", overrides={"tier": "gone"})

    async def violation(**fields) -> IntegrityError:
        async with SessionLocal() as session:
            session.add(Environment(stack_id=ids["app"], branch="main", **fields))
            try:
                await session.flush()
            except IntegrityError as exc:
                return exc
        raise AssertionError("no violation")

    dup = _integrity_problem(await violation(name="prod", tier="prod"), body)
    assert (dup.status, dup.title) == (409, "Environment exists")
    fk = _integrity_problem(await violation(name="x", tier="gone"), body)
    assert (fk.status, fk.title) == (422, "Unknown tier") and "gone" in (fk.detail or "")


async def test_autodeploy_is_forced_off_while_something_is_left_to_fill(
    client: httpx.AsyncClient,
) -> None:
    """An unfilled secret would fall back to a lower-scope value of the same name (§3.4): a push
    must not plan and apply the clone with, e.g., the stack-wide dev password."""
    admin = await login(client, "admin")
    space = await _space(client, admin, "clone-autodeploy")
    db = await _stack(client, admin, space, "clone-autodeploy-db")
    web = await _stack(client, admin, space, "clone-autodeploy-web")
    db_prod = await _env(client, admin, db, "prod", autodeploy=True)
    await _env(client, admin, web, "prod", autodeploy=True)
    r = await client.post(
        f"/api/v1/stacks/{db}/variables",
        headers=admin,
        json={"kind": "terraform", "name": "db_password", "value": "dev-pw", "sensitive": True},
    )
    assert r.status_code == 201, r.text
    await _var(client, admin, db_prod, "db_password", "prod-pw", sensitive=True)

    r = await _clone(client, admin, space)
    assert r.status_code == 201, r.text
    out = r.json()
    by_stack = {e["stack_name"]: e for e in out["environments"]}
    assert by_stack["clone-autodeploy-db"]["autodeploy"] is False
    assert by_stack["clone-autodeploy-db"]["autodeploy_off_reason"] == "left_to_fill"
    assert by_stack["clone-autodeploy-web"]["autodeploy"] is True  # nothing left to fill there
    assert by_stack["clone-autodeploy-web"]["autodeploy_off_reason"] is None
    assert out["missing_secrets"] == [
        {
            "stack": "clone-autodeploy-db",
            "environment": "prod-euw3",
            "name": "db_password",
            "falls_back_to": "stack",
        }
    ]
    assert "prod-pw" not in r.text and "dev-pw" not in r.text
    new_db = by_stack["clone-autodeploy-db"]["id"]
    got = (await client.get(f"/api/v1/environments/{new_db}", headers=admin)).json()
    assert got["autodeploy"] is False
    created = await _audits(action="environment.created", target_id=uuid.UUID(new_db))
    assert created[0].context["autodeploy_off_reason"] == "left_to_fill"


async def test_protected_forces_autodeploy_off(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    space = await _space(client, admin, "clone-protect")
    stack = await _stack(client, admin, space, "clone-protect-app")
    await _env(client, admin, stack, "prod", autodeploy=True)
    r = await _clone(client, admin, space, overrides={"protected": True})
    assert r.status_code == 201, r.text
    env = r.json()["environments"][0]
    assert (env["autodeploy"], env["autodeploy_off_reason"]) == (False, "protected")
    got = (await client.get(f"/api/v1/environments/{env['id']}", headers=admin)).json()
    assert got["protected"] is True and got["autodeploy"] is False


async def test_items_shadowing_a_label_set_are_not_copied(client: httpx.AsyncClient) -> None:
    """Recipe C (§9.5): the region comes from a set selecting `region=…`, at the weakest layer
    (§3.4). A copied env variable (layer 6) or explicit attachment (layer 4) providing the same key
    would silently keep the model's region — they are left out and listed instead."""
    admin = await login(client, "admin")
    space = await _space(client, admin, "clone-shadow")
    stack = await _stack(client, admin, space, "clone-shadow-net")
    prod = await _env(client, admin, stack, "prod", labels={"region": "eu-west-1", "team": "core"})
    await _set(
        client,
        admin,
        space,
        "region-euw1",
        {"region": "eu-west-1"},
        selector={"region": "eu-west-1"},
    )
    await _set(
        client,
        admin,
        space,
        "region-euw3",
        {"region": "eu-west-3", "azs": "3"},
        selector={"region": "eu-west-3"},
    )
    await _set(client, admin, space, "team-core", {"log_level": "warn"}, selector={"team": "core"})
    pinned = await _set(client, admin, space, "pinned-euw1", {"azs": "2", "ami": "ami-1"})
    att = await client.post(
        f"/api/v1/variable-sets/{pinned}/attachments",
        headers=admin,
        json={"target_kind": "environment", "target_id": prod, "priority": 1},
    )
    assert att.status_code == 201, att.text
    await _var(client, admin, prod, "region", "eu-west-1")
    await _var(client, admin, prod, "log_level", "info")  # overrides a set the model has too

    preview = await _clone(
        client, admin, space, dry_run=True, overrides={"labels": {"region": "eu-west-3"}}
    )
    assert preview.status_code == 201, preview.text
    out = preview.json()
    assert out["environments"][0]["label_sets"] == [
        {"name": "region-euw3", "new": True},
        {"name": "team-core", "new": False},
    ]
    where = {"stack": "clone-shadow-net", "environment": "prod-euw3", "reason": "shadows_label_set"}
    assert out["not_copied"] == [
        {
            **where,
            "item": "variable",
            "name": "region",
            "keys": ["region"],
            "label_sets": ["region-euw3"],
        },
        {
            **where,
            "item": "variable_set",
            "name": "pinned-euw1",
            "keys": ["azs"],
            "label_sets": ["region-euw3"],
        },
    ]

    r = await _clone(client, admin, space, overrides={"labels": {"region": "eu-west-3"}})
    assert r.status_code == 201, r.text
    new = r.json()["environments"][0]["id"]
    resolved = {
        v["name"]: v
        for v in (
            await client.get(f"/api/v1/environments/{new}/resolved-variables", headers=admin)
        ).json()
    }
    assert resolved["region"]["value"] == "eu-west-3"
    assert resolved["region"]["provenance"] == "set:region-euw3"
    assert resolved["azs"]["provenance"] == "set:region-euw3"
    assert (resolved["log_level"]["value"], resolved["log_level"]["provenance"]) == ("info", "env")
    assert "ami" not in resolved  # the shadowing attachment is left out whole
    summary = await _audits(action="environment.cloned")
    ctx = next(e.context for e in summary if e.context["stacks"] == ["clone-shadow-net"])
    assert len(ctx["not_copied"]) == 2


async def test_admin_copies_the_cloud_integration(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-cloud")
    r = await client.put(
        f"/api/v1/environments/{ids['app_prod']}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": PLAN_ROLE, "region": "eu-west-1"},
    )
    assert r.status_code == 200, r.text
    r = await _clone(client, admin, ids["space"], copy_cloud_integration=True)
    assert r.status_code == 201, r.text
    assert r.json()["to_configure"] == []
    new_app = next(e["id"] for e in r.json()["environments"] if e["stack_name"].endswith("-app"))
    ci = await client.get(f"/api/v1/environments/{new_app}/cloud-integration", headers=admin)
    assert ci.status_code == 200, ci.text
    assert ci.json()["plan_role_arn"] == PLAN_ROLE
    audits = await _audits(action="cloud_integration.created", target_id=uuid.UUID(new_app))
    assert len(audits) == 1 and audits[0].context["cloned_from"] == ids["app_prod"]


async def test_per_item_copies_and_their_audit(client: httpx.AsyncClient) -> None:
    """Notification targets and secret references are copied; a reference's static fallback is a
    literal secret — not copied (mode reset to `error`), listed to configure. Each copied item has
    its own audit event, not only the summary."""
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-items")
    app_prod = ids["app_prod"]
    nt = await client.post(
        f"/api/v1/environments/{app_prod}/notifications",
        headers=admin,
        json={"name": "ops", "kind": "slack", "url": "https://hooks.example/ops"},
    )
    assert nt.status_code == 201, nt.text
    src = await client.post(
        f"/api/v1/spaces/{ids['space']}/secret-sources",
        headers=admin,
        json={"name": "vault", "provider": "proton_pass", "bootstrap_secret": "pat_xyz"},
    )
    assert src.status_code == 201, src.text
    ref = await client.post(
        f"/api/v1/environments/{app_prod}/variables",
        headers=admin,
        json={
            "kind": "terraform",
            "name": "api_key",
            "secret_source_id": src.json()["id"],
            "secret_ref": "pass://vault/api_key/value",
            "secret_fallback_mode": "static",
            "secret_fallback": "fallback-s3cret",
        },
    )
    assert ref.status_code == 201, ref.text
    hook = {"stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "fail"}
    assert (
        await client.post(f"/api/v1/environments/{app_prod}/hooks", headers=admin, json=hook)
    ).status_code == 201
    vset = await _set(client, admin, ids["space"], "items-extra", {"x": "1"})
    assert (
        await client.post(
            f"/api/v1/variable-sets/{vset}/attachments",
            headers=admin,
            json={"target_kind": "environment", "target_id": app_prod},
        )
    ).status_code == 201

    r = await _clone(client, admin, ids["space"])
    assert r.status_code == 201, r.text
    assert "fallback-s3cret" not in r.text
    out = r.json()
    new = {e["stack_name"].split("-")[-1]: e["id"] for e in out["environments"]}
    assert {
        "stack": "clone-items-app",
        "environment": "prod-euw3",
        "item": "fallback of api_key",
    } in (out["to_configure"])
    targets = (
        await client.get(f"/api/v1/environments/{new['app']}/notifications", headers=admin)
    ).json()
    assert [(t["name"], t["url"]) for t in targets] == [("ops", "https://hooks.example/ops")]
    variables = (
        await client.get(f"/api/v1/environments/{new['app']}/variables", headers=admin)
    ).json()
    copied = next(v for v in variables if v["name"] == "api_key")
    assert copied["secret_ref"] == "pass://vault/api_key/value"
    assert copied["secret_fallback_mode"] == "error"
    async with SessionLocal() as session:
        row = (
            await session.execute(select(Variable).where(Variable.id == uuid.UUID(copied["id"])))
        ).scalar_one()
    assert row.secret_fallback_encrypted is None and row.value_encrypted is None

    app_id, net_id = uuid.UUID(new["app"]), uuid.UUID(new["network"])
    by_action: dict[str, list[AuditEvent]] = {}
    for e in await _audits(actor_kind="user"):
        by_action.setdefault(e.action, []).append(e)
    created = {e.target_id for e in by_action["environment.created"]}
    assert {app_id, net_id} <= created
    assert any(
        e.target_id == app_id and e.context["upstream_env_id"] == new["network"]
        for e in by_action["dependency.created"]
    )
    assert any(
        e.target_id == net_id and e.context["upstream_env_id"] == ids["dns_global"]
        for e in by_action["dependency.created"]
    )
    assert any(e.target_id == uuid.UUID(copied["id"]) for e in by_action["variable.created"])
    assert any(e.context.get("target_id") == new["app"] for e in by_action["variable_set.attached"])
    assert any(
        e.target_id == uuid.UUID(targets[0]["id"]) for e in by_action["notification_target.created"]
    )
    new_hooks = (await client.get(f"/api/v1/environments/{new['app']}/hooks", headers=admin)).json()
    assert any(e.target_id == uuid.UUID(new_hooks[0]["id"]) for e in by_action["hook.created"])


async def test_dry_run_writes_no_rows_and_no_audit(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, "clone-dryrows")
    await _var(client, admin, ids["app_prod"], "LOG_LEVEL", "info")
    hook = {"stage": "after_plan", "name": "tfsec", "command": "tfsec .", "on_failure": "fail"}
    assert (
        await client.post(f"/api/v1/environments/{ids['app_prod']}/hooks", headers=admin, json=hook)
    ).status_code == 201

    async def counts() -> list[int]:
        async with SessionLocal() as session:
            return [
                (await session.execute(select(func.count()).select_from(m))).scalar_one()
                for m in (AuditEvent, Environment, Variable, Hook)
            ]

    before = await counts()
    r = await _clone(client, admin, ids["space"], dry_run=True)
    assert r.status_code == 201 and r.json()["dry_run"] is True
    assert await counts() == before
