"""Configuration export / import (SPECS §22): a space's configuration as a document, by names,
never a secret; an import merges it into a space — all or nothing, audited, dry-runnable."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import select

from app.crypto import decrypt
from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.models.environment import Environment
from app.models.variable import Variable
from tests.conftest_phase2 import login

PLAN_ROLE = "arn:aws:iam::123456789012:role/plan"
APPLY_ROLE = "arn:aws:iam::123456789012:role/apply"
SLACK = "https://hooks.slack.com/services/T000/B000/xyz"


async def _post(client: httpx.AsyncClient, h: dict, url: str, body: dict) -> dict:
    r = await client.post(url, headers=h, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _space(client: httpx.AsyncClient, admin: dict, name: str) -> str:
    return (await _post(client, admin, "/api/v1/spaces", {"name": name}))["id"]


async def _world(client: httpx.AsyncClient, admin: dict, prefix: str) -> dict[str, str]:
    """A space with: a secret source, a variable set attached to a stack and a tier, two stacks
    (network → app), plain / sensitive / referenced variables, hooks, a notification target, a
    cloud integration, a dependency with a mock."""
    ids: dict[str, str] = {"space": await _space(client, admin, prefix)}
    sp = ids["space"]
    ids["source"] = (
        await _post(
            client,
            admin,
            f"/api/v1/spaces/{sp}/secret-sources",
            {"name": "vault", "provider": "proton_pass", "bootstrap_secret": "pat_xyz"},
        )
    )["id"]
    for st in ("network", "app"):
        ids[st] = (
            await _post(
                client,
                admin,
                "/api/v1/stacks",
                {
                    "space_id": sp,
                    "name": st,
                    "repo_url": "https://git.example.com/infra.git",
                    "repo_auth_kind": "token",
                    "repo_secret": "ghp_secret",
                    "project_root": st,
                    "tool_version": "1.12",
                    "labels": {"team": "core"},
                },
            )
        )["id"]
        ids[f"{st}_prod"] = (
            await _post(
                client,
                admin,
                f"/api/v1/stacks/{ids[st]}/environments",
                {"name": "prod", "tier": "prod", "branch": "main", "protected": True},
            )
        )["id"]
        ids[f"{st}_dev"] = (
            await _post(
                client,
                admin,
                f"/api/v1/stacks/{ids[st]}/environments",
                {"name": "dev", "tier": "dev", "branch": "main", "autodeploy": True},
            )
        )["id"]
    net, net_prod = ids["network"], ids["network_prod"]
    await _post(
        client,
        admin,
        f"/api/v1/stacks/{net}/variables",
        {"kind": "terraform", "name": "region", "value": "eu-west-1"},
    )
    await _post(
        client,
        admin,
        f"/api/v1/environments/{net_prod}/variables",
        {"kind": "terraform", "name": "db_password", "value": "hunter2", "sensitive": True},
    )
    await _post(
        client,
        admin,
        f"/api/v1/environments/{net_prod}/variables",
        {
            "kind": "terraform",
            "name": "api_key",
            "secret_source_id": ids["source"],
            "secret_ref": "pass://infra/api/key",
        },
    )
    vs = await _post(
        client, admin, "/api/v1/variable-sets", {"space_id": sp, "name": "common", "selector": None}
    )
    ids["set"] = vs["id"]
    await _post(
        client,
        admin,
        f"/api/v1/variable-sets/{vs['id']}/variables",
        {"kind": "environment", "name": "AWS_REGION", "value": "eu-west-1"},
    )
    await _post(
        client,
        admin,
        f"/api/v1/variable-sets/{vs['id']}/attachments",
        {"target_kind": "stack", "target_id": ids["app"], "priority": 3},
    )
    await _post(
        client,
        admin,
        f"/api/v1/stacks/{net}/hooks",
        {"stage": "after_plan", "name": "no-destroy", "command": "jq -e . plan.json"},
    )
    await _post(
        client,
        admin,
        f"/api/v1/environments/{net_prod}/notifications",
        {"name": "slack", "kind": "slack", "url": SLACK, "on_states": ["unconfirmed"]},
    )
    r = await client.put(
        f"/api/v1/environments/{net_prod}/cloud-integration",
        headers=admin,
        json={"plan_role_arn": PLAN_ROLE, "apply_role_arn": APPLY_ROLE},
    )
    assert r.status_code == 200, r.text
    await _post(
        client,
        admin,
        f"/api/v1/environments/{ids['app_prod']}/dependencies",
        {
            "upstream_env_id": net_prod,
            "trigger_policy": "always",
            "references": [
                {"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": "vpc-0"}
            ],
        },
    )
    return ids


async def _export(client: httpx.AsyncClient, h: dict, space_id: str) -> dict:
    r = await client.get(f"/api/v1/spaces/{space_id}/export", headers=h)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    return r.json()


async def _import(
    client: httpx.AsyncClient, h: dict, space_id: str, doc: dict, *, dry_run: bool = False
) -> httpx.Response:
    return await client.post(
        f"/api/v1/spaces/{space_id}/import",
        headers=h,
        params={"dry_run": str(dry_run).lower()},
        json=doc,
    )


async def test_export_is_by_name_and_never_carries_a_secret(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, f"exp-{uuid.uuid4().hex[:6]}")
    doc = await _export(client, admin, ids["space"])

    assert doc["format"] == "stackd.config" and doc["format_version"] == 1
    raw = str(doc)
    for secret in ("hunter2", "ghp_secret", "pat_xyz"):
        assert secret not in raw

    network = next(s for s in doc["stacks"] if s["name"] == "network")
    assert network["has_repo_secret"] is True
    prod = next(e for e in network["environments"] if e["name"] == "prod")
    by_name = {v["name"]: v for v in prod["variables"]}
    assert by_name["db_password"] == {
        "kind": "terraform",
        "name": "db_password",
        "hcl": False,
        "sensitive": True,
        "value": None,
        "secret": None,
    }
    assert by_name["api_key"]["secret"] == {
        "source": "vault",
        "ref": "pass://infra/api/key",
        "fallback_mode": "error",
    }
    assert prod["cloud_integration"]["apply_role_arn"] == APPLY_ROLE
    assert prod["notification_targets"][0]["url"] == SLACK
    assert network["hooks"][0]["name"] == "no-destroy"
    assert {t["name"] for t in doc["tiers"]} == {"dev", "prod"}
    assert doc["variable_sets"][0]["attachments"] == [
        {"target": "stack", "stack": "app", "environment": None, "tier": None, "priority": 3}
    ]
    assert doc["dependencies"] == [
        {
            "upstream": {"space": None, "stack": "network", "environment": "prod"},
            "downstream": {"space": None, "stack": "app", "environment": "prod"},
            "trigger_policy": "always",
            "references": [
                {"output_name": "vpc_id", "input_name": "vpc_id", "mock_value": "vpc-0"}
            ],
        }
    ]
    missing = {
        (m.get("stack"), m.get("environment"), m["item"], m.get("name"))
        for m in doc["missing_secrets"]
    }
    assert ("network", "prod", "variable", "db_password") in missing
    assert ("network", None, "repo_secret", None) in missing

    async with SessionLocal() as s:
        audit = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "space.config_exported",
                    AuditEvent.target_id == uuid.UUID(ids["space"]),
                )
            )
        ).scalar_one()
        assert audit.context["stacks"] == 2


async def test_round_trip_into_a_new_space(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, f"src-{uuid.uuid4().hex[:6]}")
    doc = await _export(client, admin, ids["space"])

    # The target space has no secret source yet: the reference is skipped, reported.
    target = await _space(client, admin, f"dst-{uuid.uuid4().hex[:6]}")
    r = await _import(client, admin, target, doc)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["dry_run"] is False
    assert rep["summary"]["created"] > 10
    skipped = {(s["kind"], s["reason"]) for s in rep["skipped"]}
    assert ("secret_source", "bootstrap_secret_required") in skipped
    assert ("variable", "secret_source_missing") in skipped
    assert ("variable", "value_required") in skipped  # db_password: no value to create it with
    items = {m["item"] for m in rep["missing_secrets"]}
    assert {"bootstrap_secret", "variable", "repo_secret"} <= items
    assert rep["id_map"]["environments"][ids["network_prod"]] != ids["network_prod"]

    # Re-exporting the target gives back the same configuration (minus what was skipped).
    again = await _export(client, admin, target)
    assert [s["name"] for s in again["stacks"]] == ["app", "network"]
    network = next(s for s in again["stacks"] if s["name"] == "network")
    prod = next(e for e in network["environments"] if e["name"] == "prod")
    assert {v["name"] for v in prod["variables"]} == set()  # both secrets left to fill
    assert prod["protected"] is True
    assert prod["cloud_integration"]["plan_role_arn"] == PLAN_ROLE
    assert again["dependencies"] == doc["dependencies"]
    assert again["variable_sets"] == doc["variable_sets"]
    # A secret left to fill: the new autodeploy env is created with autodeploy off (§22.3).
    dev = next(e for e in network["environments"] if e["name"] == "dev")
    assert dev["autodeploy"] is False
    assert any(w["kind"] == "autodeploy_off" for w in rep["warnings"])

    async with SessionLocal() as s:
        actions = {
            e.action
            for e in (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.context["via"].astext == "import")
                )
            ).scalars()
        }
        assert {
            "stack.created",
            "environment.created",
            "variable.created",
            "variable_set.attached",
            "dependency.created",
            "cloud_integration.created",
            "space.config_imported",
        } <= actions


async def test_import_is_idempotent_and_merges(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, f"idem-{uuid.uuid4().hex[:6]}")
    doc = await _export(client, admin, ids["space"])

    # Same space, same document: nothing to do.
    rep = (await _import(client, admin, ids["space"], doc)).json()
    assert rep["summary"]["created"] == 0
    assert rep["summary"]["updated"] == 0, rep["changes"]

    # The existing sensitive value is kept (the document has none); a changed setting is applied.
    network = next(s for s in doc["stacks"] if s["name"] == "network")
    network["environments"][0]["branch"] = "release"
    network["variables"][0]["value"] = "eu-west-3"
    rep = (await _import(client, admin, ids["space"], doc)).json()
    updated = {c["path"]: c.get("fields") for c in rep["changes"] if c["op"] == "updated"}
    assert updated == {
        "stacks/network/environments/dev": ["branch"],
        "stacks/network/variables/terraform/region": ["value"],
    }
    async with SessionLocal() as s:
        secret = (
            await s.execute(
                select(Variable).where(
                    Variable.environment_id == uuid.UUID(ids["network_prod"]),
                    Variable.name == "db_password",
                )
            )
        ).scalar_one()
        assert secret.value_encrypted is not None and decrypt(secret.value_encrypted) == "hunter2"


async def test_dry_run_reports_and_writes_nothing(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    ids = await _world(client, admin, f"dry-{uuid.uuid4().hex[:6]}")
    doc = await _export(client, admin, ids["space"])
    target = await _space(client, admin, f"dry-dst-{uuid.uuid4().hex[:6]}")

    rep = (await _import(client, admin, target, doc, dry_run=True)).json()
    assert rep["dry_run"] is True and rep["summary"]["created"] > 0
    assert rep["worker_pool_tokens"] == []
    assert (await _export(client, admin, target))["stacks"] == []


async def test_hand_filled_secret_is_stored_encrypted(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    target = await _space(client, admin, f"fill-{uuid.uuid4().hex[:6]}")
    doc = {
        "format": "stackd.config",
        "format_version": 1,
        "stacks": [
            {
                "name": "s",
                "repo_url": "file:///r",
                "tool_version": "1.12",
                "environments": [
                    {
                        "name": "dev",
                        "tier": "dev",
                        "branch": "main",
                        "autodeploy": True,
                        "variables": [
                            {
                                "kind": "terraform",
                                "name": "pw",
                                "sensitive": True,
                                "value": "s3cr3t",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    rep = (await _import(client, admin, target, doc)).json()
    assert rep["missing_secrets"] == []
    async with SessionLocal() as s:
        var = (await s.execute(select(Variable).where(Variable.name == "pw"))).scalars().all()
        mine = [
            v
            for v in var
            if v.value_encrypted is not None and decrypt(v.value_encrypted) == "s3cr3t"
        ]
        assert mine and all(v.value is None for v in mine)
        env = (
            await s.execute(select(Environment).where(Environment.id == mine[0].environment_id))
        ).scalar_one()
        assert env.autodeploy is True  # nothing left to fill: autodeploy as declared


async def test_import_validation(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    target = await _space(client, admin, f"val-{uuid.uuid4().hex[:6]}")
    stack = {"name": "s", "repo_url": "file:///r", "tool_version": "1.12"}

    # Unknown field → 422 (a typo must not be silently dropped).
    r = await _import(client, admin, target, {"stacks": [{**stack, "tool_verison": "1.12"}]})
    assert r.status_code == 422
    # Duplicate natural keys → 422.
    r = await _import(client, admin, target, {"stacks": [stack, stack]})
    assert r.status_code == 422 and "duplicate stack" in r.text
    # An env on a tier neither in the catalog nor in the document → 422, nothing written.
    env = {"name": "x", "tier": "nope", "branch": "main"}
    r = await _import(client, admin, target, {"stacks": [{**stack, "environments": [env]}]})
    assert r.status_code == 422 and "nope" in r.text
    assert (await _export(client, admin, target))["stacks"] == []
    # A dependency cycle aborts the whole import.
    envs = [
        {"name": "a", "tier": "dev", "branch": "m"},
        {"name": "b", "tier": "dev", "branch": "m"},
    ]
    deps = [
        {
            "upstream": {"stack": "s", "environment": "a"},
            "downstream": {"stack": "s", "environment": "b"},
        },
        {
            "upstream": {"stack": "s", "environment": "b"},
            "downstream": {"stack": "s", "environment": "a"},
        },
    ]
    r = await _import(
        client, admin, target, {"stacks": [{**stack, "environments": envs}], "dependencies": deps}
    )
    assert r.status_code == 422 and "cycle" in r.text.lower()
    assert (await _export(client, admin, target))["stacks"] == []


async def test_permissions(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    bob = await login(client, "bob")  # a writer: not a space admin
    spaces = (await client.get("/api/v1/spaces", headers=admin)).json()
    default = next(s for s in spaces if s["name"] == "default")["id"]
    assert (await client.get(f"/api/v1/spaces/{default}/export", headers=bob)).status_code == 403
    assert (await _import(client, bob, default, {})).status_code == 403

    # A space admin who is not an instance admin: catalog-wide items are left out, never forced.
    sp = await _space(client, admin, f"perm-{uuid.uuid4().hex[:6]}")
    bob_id = (await client.get("/api/v1/auth/me", headers=bob)).json()["id"]
    r = await client.put(
        f"/api/v1/spaces/{sp}/members",
        headers=admin,
        json={"user_id": bob_id, "role": "admin", "allowed_tiers": [], "can_destroy": False},
    )
    assert r.status_code in (200, 201), r.text
    stack = {"name": "s", "repo_url": "file:///r", "tool_version": "1.12"}
    env = {
        "name": "prod",
        "tier": "prod",
        "branch": "main",
        "cloud_integration": {"plan_role_arn": PLAN_ROLE, "apply_role_arn": APPLY_ROLE},
    }
    doc = {"stacks": [{**stack, "environments": [env]}], "worker_pools": [{"name": f"p-{sp}"}]}
    rep = (await _import(client, bob, sp, doc)).json()
    skipped = {(s["kind"], s["reason"]) for s in rep["skipped"]}
    assert ("cloud_integration", "instance_admin_required") in skipped
    assert ("worker_pool", "instance_admin_required") in skipped
    # Creating a tier is instance-wide: refused outright for a space admin.
    r = await _import(client, bob, sp, {"tiers": [{"name": f"t{uuid.uuid4().hex[:6]}"}]})
    assert r.status_code == 403

    # An instance admin gets the pool created and its token, once.
    rep = (await _import(client, admin, sp, doc)).json()
    assert [t["name"] for t in rep["worker_pool_tokens"]] == [f"p-{sp}"]


async def test_review_fixes(client: httpx.AsyncClient) -> None:
    """A space admin can't re-point a secret source through an import (PATCH is instance admin);
    a skipped variable is left untouched (no unaudited `hcl` change); any space name exports."""
    admin = await login(client, "admin")
    bob = await login(client, "bob")
    sp = await _space(client, admin, f'生产-"q"-{uuid.uuid4().hex[:6]}')
    bob_id = (await client.get("/api/v1/auth/me", headers=bob)).json()["id"]
    r = await client.put(
        f"/api/v1/spaces/{sp}/members",
        headers=admin,
        json={"user_id": bob_id, "role": "admin", "allowed_tiers": [], "can_destroy": False},
    )
    assert r.status_code == 200, r.text
    await _post(
        client,
        admin,
        f"/api/v1/spaces/{sp}/secret-sources",
        {"name": "vault", "provider": "proton_pass", "bootstrap_secret": "pat_xyz"},
    )

    r = await client.get(f"/api/v1/spaces/{sp}/export", headers=bob)
    assert r.status_code == 200, r.text
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    doc = r.json()

    doc["secret_sources"][0]["config"] = {"server": "https://evil.example"}
    rep = (await _import(client, bob, sp, doc)).json()
    assert {
        "kind": "secret_source",
        "path": "secret_sources/vault",
        "reason": "instance_admin_required",
    } in rep["skipped"]
    again = (await client.get(f"/api/v1/spaces/{sp}/export", headers=admin)).json()
    assert again["secret_sources"][0]["config"] == {}

    # An existing referenced variable whose source the document names wrongly: skipped, untouched.
    stack = (
        await _post(
            client,
            admin,
            "/api/v1/stacks",
            {"space_id": sp, "name": "s", "repo_url": "file:///r", "tool_version": "1.12"},
        )
    )["id"]
    src = again["secret_sources"]
    assert src
    sources = (await client.get(f"/api/v1/spaces/{sp}/secret-sources", headers=admin)).json()
    await _post(
        client,
        admin,
        f"/api/v1/stacks/{stack}/variables",
        {
            "kind": "terraform",
            "name": "k",
            "secret_source_id": sources[0]["id"],
            "secret_ref": "p://a",
        },
    )
    doc = (await client.get(f"/api/v1/spaces/{sp}/export", headers=admin)).json()
    var = doc["stacks"][0]["variables"][0]
    var["hcl"] = True
    var["secret"]["source"] = "nope"
    rep = (await _import(client, admin, sp, doc)).json()
    assert any(s["reason"] == "secret_source_missing" for s in rep["skipped"])
    async with SessionLocal() as s:
        row = (
            await s.execute(select(Variable).where(Variable.stack_id == uuid.UUID(stack)))
        ).scalar_one()
        assert row.hcl is False


async def test_export_skips_a_reference_to_another_spaces_source(client: httpx.AsyncClient) -> None:
    """A legacy row referencing another space's source is left out (and the source not named),
    never a 500."""
    from app.models.secret_source import SecretSource

    admin = await login(client, "admin")
    a = await _space(client, admin, f"ref-a-{uuid.uuid4().hex[:6]}")
    b = await _space(client, admin, f"ref-b-{uuid.uuid4().hex[:6]}")
    src = await _post(
        client,
        admin,
        f"/api/v1/spaces/{b}/secret-sources",
        {"name": "other-vault", "provider": "proton_pass", "bootstrap_secret": "pat"},
    )
    stack = (
        await _post(
            client,
            admin,
            "/api/v1/stacks",
            {"space_id": a, "name": "s", "repo_url": "file:///r", "tool_version": "1.12"},
        )
    )["id"]
    async with SessionLocal() as s:
        assert await s.get(SecretSource, uuid.UUID(src["id"])) is not None
        s.add(
            Variable(
                stack_id=uuid.UUID(stack),
                kind="terraform",
                name="legacy",
                sensitive=True,
                secret_source_id=uuid.UUID(src["id"]),
                secret_ref="pass://x/y/z",
            )
        )
        await s.commit()
    doc = await _export(client, admin, a)
    assert doc["stacks"][0]["variables"] == []
    assert {"stack": "s", "item": "secret_reference_out_of_space", "name": "legacy"} in doc[
        "missing_secrets"
    ]
    assert "other-vault" not in str(doc)
