from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

import httpx


def _make_repo(tf: str) -> str:
    """A throwaway git repo (branch main) with a single main.tf — cloned by discover-inputs."""
    d = tempfile.mkdtemp(prefix="stackd-test-repo-")
    pathlib.Path(d, "main.tf").write_text(tf)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-q", "-m", "init"], env=env, check=True)
    return d


_TF = """
variable "region" { type = string }
variable "instance_count" { type = number }
variable "tags" { type = map(string) }
variable "db_password" {
  type      = string
  sensitive = true
}
variable "az" {
  type    = string
  default = "eu-west-1a"
}
"""


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


async def test_selector_auto_attaches_by_stack_label(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    r = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={
            "name": "selector-stack",
            "repo_url": "file:///repos/x",
            "tool_version": "1.12.0",
            "labels": {"team": "payments"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["labels"] == {"team": "payments"}
    stack_id = r.json()["id"]
    env_id = await _env(client, h, stack_id, "dev", "dev")

    # A set whose selector matches the stack label auto-attaches (no attachment row).
    match = (
        await client.post(
            "/api/v1/variable-sets",
            headers=h,
            json={"name": "team-payments", "selector": {"team": "payments"}},
        )
    ).json()
    assert match["selector"] == {"team": "payments"}
    await client.post(
        f"/api/v1/variable-sets/{match['id']}/variables",
        headers=h,
        json={"kind": "terraform", "name": "region", "value": "eu-west-1"},
    )
    # A non-matching selector must NOT apply.
    other = (
        await client.post(
            "/api/v1/variable-sets",
            headers=h,
            json={"name": "team-billing", "selector": {"team": "billing"}},
        )
    ).json()
    await client.post(
        f"/api/v1/variable-sets/{other['id']}/variables",
        headers=h,
        json={"kind": "terraform", "name": "region", "value": "us-east-1"},
    )

    resolved = (
        await client.get(f"/api/v1/environments/{env_id}/resolved-variables", headers=h)
    ).json()
    by_name = {v["name"]: v for v in resolved}
    assert by_name["region"]["value"] == "eu-west-1"
    assert by_name["region"]["provenance"] == "set:team-payments"


async def test_selector_matches_env_label(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    stack_id = await _stack(client, h, "env-label-stack")
    env_id = await _env(client, h, stack_id, "edge", "dev")
    # Effective labels = stack + env, so a selector can target an env-level label too.
    await client.patch(
        f"/api/v1/environments/{env_id}", headers=h, json={"labels": {"zone": "edge"}}
    )
    s = (
        await client.post(
            "/api/v1/variable-sets",
            headers=h,
            json={"name": "edge-set", "selector": {"zone": "edge"}},
        )
    ).json()
    await client.post(
        f"/api/v1/variable-sets/{s['id']}/variables",
        headers=h,
        json={"kind": "terraform", "name": "cdn", "value": "on"},
    )

    resolved = (
        await client.get(f"/api/v1/environments/{env_id}/resolved-variables", headers=h)
    ).json()
    by_name = {v["name"]: v for v in resolved}
    assert by_name["cdn"]["provenance"] == "set:edge-set"


async def test_attach_set_to_tier(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    tiers = (await client.get("/api/v1/tiers", headers=h)).json()
    prod = next(t for t in tiers if t["name"] == "prod")
    stack_id = await _stack(client, h, "tier-attach-stack")
    prod_env = await _env(client, h, stack_id, "prod", "prod")
    dev_env = await _env(client, h, stack_id, "dev", "dev")

    vset = (
        await client.post("/api/v1/variable-sets", headers=h, json={"name": "prod-creds"})
    ).json()
    await client.post(
        f"/api/v1/variable-sets/{vset['id']}/variables",
        headers=h,
        json={"kind": "terraform", "name": "endpoint", "value": "prod.internal"},
    )
    attach = await client.post(
        f"/api/v1/variable-sets/{vset['id']}/attachments",
        headers=h,
        json={"target_kind": "tier", "target_id": prod["id"]},
    )
    assert attach.status_code == 201, attach.text

    # The prod env (matching tier) gets the set; the dev env does not.
    prod_vars = (
        await client.get(f"/api/v1/environments/{prod_env}/resolved-variables", headers=h)
    ).json()
    assert {v["name"]: v["provenance"] for v in prod_vars}.get("endpoint") == "set:prod-creds"
    dev_vars = (
        await client.get(f"/api/v1/environments/{dev_env}/resolved-variables", headers=h)
    ).json()
    assert "endpoint" not in {v["name"] for v in dev_vars}


def test_parse_inputs_unit() -> None:
    from app.variables.discovery import parse_inputs

    d = pathlib.Path(_make_repo(_TF))
    by_name = {i.name: i for i in parse_inputs(d)}
    assert by_name["region"].required and not by_name["region"].hcl
    assert by_name["tags"].required and by_name["tags"].hcl  # map(...) → injected as HCL
    assert by_name["db_password"].sensitive
    assert not by_name["az"].required  # has a default


async def test_discover_inputs_creates_required(client: httpx.AsyncClient) -> None:
    h = await _admin(client)
    repo = _make_repo(_TF)
    stack_id = (
        await client.post(
            "/api/v1/stacks",
            headers=h,
            json={"name": "discover-stack", "repo_url": f"file://{repo}", "tool_version": "1.12.0"},
        )
    ).json()["id"]
    env_id = await _env(client, h, stack_id, "dev", "dev")

    # `region` is already provided → must be skipped, not duplicated.
    await client.post(
        f"/api/v1/environments/{env_id}/variables",
        headers=h,
        json={"kind": "terraform", "name": "region", "value": "eu-west-1"},
    )

    r = await client.post(f"/api/v1/environments/{env_id}/discover-inputs", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["required_total"] == 4  # region, instance_count, tags, db_password (az has default)
    assert "region" in body["skipped"]
    assert set(body["created"]) == {"instance_count", "tags", "db_password"}

    vars_ = (await client.get(f"/api/v1/environments/{env_id}/variables", headers=h)).json()
    by_name = {v["name"]: v for v in vars_}
    assert "az" not in by_name  # optional inputs are not created
    assert by_name["tags"]["hcl"] is True
    assert by_name["db_password"]["sensitive"] is True and by_name["db_password"]["value"] == "•••"


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
