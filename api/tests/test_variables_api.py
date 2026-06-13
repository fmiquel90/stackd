from __future__ import annotations

import httpx


async def _admin(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _stack(client: httpx.AsyncClient, h: dict[str, str], name: str) -> str:
    r = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={"name": name, "repo_url": "file:///repos/x", "tool_version": "1.12.0"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _env(
    client: httpx.AsyncClient, h: dict[str, str], stack_id: str, name: str, tier: str
) -> str:
    r = await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=h,
        json={"name": name, "tier": tier, "branch": "main"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_five_layer_resolution_and_provenance(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    stack_id = await _stack(client, h, "resolve-stack")
    env_id = await _env(client, h, stack_id, "dev", "dev")

    # auto_attach set (weakest) — defines `foo` and a set-only `baz`.
    low = (
        await client.post(
            "/api/v1/variable-sets", headers=h, json={"name": "low", "auto_attach": True}
        )
    ).json()
    for name, value in (("foo", "low-foo"), ("baz", "low-baz")):
        await client.post(
            f"/api/v1/variable-sets/{low['id']}/variables",
            headers=h,
            json={"kind": "terraform", "name": name, "value": value},
        )

    # env-attached set (stronger than auto) — overrides `foo`.
    high = (await client.post("/api/v1/variable-sets", headers=h, json={"name": "high"})).json()
    await client.post(
        f"/api/v1/variable-sets/{high['id']}/variables",
        headers=h,
        json={"kind": "terraform", "name": "foo", "value": "high-foo"},
    )
    await client.post(
        f"/api/v1/variable-sets/{high['id']}/attachments",
        headers=h,
        json={"target_kind": "environment", "target_id": env_id, "priority": 10},
    )

    # stack var `bar`, then env override of `bar` (env wins).
    await client.post(
        f"/api/v1/stacks/{stack_id}/variables",
        headers=h,
        json={"kind": "terraform", "name": "bar", "value": "stack-bar"},
    )
    await client.post(
        f"/api/v1/environments/{env_id}/variables",
        headers=h,
        json={"kind": "terraform", "name": "bar", "value": "env-bar"},
    )

    resolved = (
        await client.get(f"/api/v1/environments/{env_id}/resolved-variables", headers=h)
    ).json()
    by_name = {v["name"]: v for v in resolved}

    assert by_name["foo"]["value"] == "high-foo"
    assert by_name["foo"]["provenance"] == "set:high"  # env-attached beat auto_attach
    assert by_name["baz"]["provenance"] == "set:low"
    assert by_name["bar"]["value"] == "env-bar"
    assert by_name["bar"]["provenance"] == "env"  # env override wins over stack
    assert by_name["foo"]["injected_name"] == "TF_VAR_foo"


async def test_sensitive_variable_is_write_only(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    stack_id = await _stack(client, h, "secret-stack")

    created = await client.post(
        f"/api/v1/stacks/{stack_id}/variables",
        headers=h,
        json={
            "kind": "environment",
            "name": "DD_TOKEN",
            "value": "super-secret",
            "sensitive": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["value"] == "•••"  # never echoed back

    listed = (await client.get(f"/api/v1/stacks/{stack_id}/variables", headers=h)).json()
    secret = next(v for v in listed if v["name"] == "DD_TOKEN")
    assert secret["value"] == "•••" and secret["sensitive"] is True

    # The plaintext is nowhere in the API response.
    assert "super-secret" not in created.text


async def test_attached_set_delete_requires_detach(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    stack_id = await _stack(client, h, "detach-stack")
    vset = (await client.post("/api/v1/variable-sets", headers=h, json={"name": "pinned"})).json()
    attach = await client.post(
        f"/api/v1/variable-sets/{vset['id']}/attachments",
        headers=h,
        json={"target_kind": "stack", "target_id": stack_id},
    )
    assert attach.status_code == 201

    blocked = await client.delete(f"/api/v1/variable-sets/{vset['id']}", headers=h)
    assert blocked.status_code == 409
    assert blocked.json()["attachments"][0]["target_id"] == stack_id

    await client.delete(
        f"/api/v1/variable-sets/{vset['id']}/attachments/{attach.json()['id']}", headers=h
    )
    assert (
        await client.delete(f"/api/v1/variable-sets/{vset['id']}", headers=h)
    ).status_code == 204


async def test_protected_env_forces_no_autodeploy(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    stack_id = await _stack(client, h, "protected-stack")
    r = await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=h,
        json={
            "name": "prod",
            "tier": "prod",
            "branch": "main",
            "protected": True,
            "autodeploy": True,
        },
    )
    assert r.status_code == 201
    assert r.json()["protected"] is True
    assert r.json()["autodeploy"] is False  # protected forces manual confirmation (§3.2)
