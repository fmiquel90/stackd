"""An environment's repo var files (SPECS §3.4): validated on every write path, audited, carried by
the claim payload (managed state or not), the export/import document and an environment clone."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models.audit import AuditEvent
from tests.conftest_phase2 import login, make_proposed_run, register_worker


async def _space(client: httpx.AsyncClient, h: dict, name: str) -> str:
    r = await client.post("/api/v1/spaces", headers=h, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _stack(
    client: httpx.AsyncClient, h: dict, name: str, project_root: str, space_id: str | None = None
) -> str:
    body = {
        "name": name,
        "repo_url": "file:///repos/x",
        "tool_version": "1.12.0",
        "project_root": project_root,
        **({"space_id": space_id} if space_id else {}),
    }
    r = await client.post("/api/v1/stacks", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_env(
    client: httpx.AsyncClient, h: dict, stack_id: str, var_files: object, **extra: object
) -> httpx.Response:
    return await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=h,
        json={"name": "prod", "tier": "prod", "branch": "main", "var_files": var_files, **extra},
    )


async def test_round_trip_and_patch(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack_id = await _stack(client, admin, f"vf-{uuid.uuid4().hex[:6]}", "stacks/app")
    files = ["envs/prod.tfvars", "../shared/prod.tfvars", "./common.tfvars"]
    r = await _create_env(client, admin, stack_id, files)
    assert r.status_code == 201, r.text
    assert r.json()["var_files"] == files  # the order is kept
    env_id = r.json()["id"]

    # Default: none, for an env created without the field.
    other = await client.post(
        f"/api/v1/stacks/{stack_id}/environments",
        headers=admin,
        json={"name": "dev", "tier": "dev", "branch": "main"},
    )
    assert other.json()["var_files"] == []

    upd = await client.patch(
        f"/api/v1/environments/{env_id}",
        headers=admin,
        json={"var_files": ["envs/prod-eu.tfvars", "envs/prod.tfvars"]},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["var_files"] == ["envs/prod-eu.tfvars", "envs/prod.tfvars"]
    async with SessionLocal() as s:
        audit = (
            (
                await s.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "environment.updated",
                        AuditEvent.target_id == uuid.UUID(env_id),
                    )
                    .order_by(AuditEvent.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert audit is not None
        assert audit.context["fields"] == ["var_files"]
        assert audit.context["var_files"] == ["envs/prod-eu.tfvars", "envs/prod.tfvars"]

    # A PATCH of another field leaves them alone; null or [] clears them.
    r = await client.patch(f"/api/v1/environments/{env_id}", headers=admin, json={"branch": "x"})
    assert r.json()["var_files"] == ["envs/prod-eu.tfvars", "envs/prod.tfvars"]
    r = await client.patch(
        f"/api/v1/environments/{env_id}", headers=admin, json={"var_files": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["var_files"] == []


@pytest.mark.parametrize(
    ("var_files", "status"),
    [
        (["/etc/prod.tfvars"], 422),  # absolute
        (["a.tfvars", "a.tfvars"], 422),  # listed twice
        ([""], 422),
        (["a\nb.tfvars"], 422),  # control character
        (["x" * 257], 422),
        ([f"f{i}.tfvars" for i in range(17)], 422),  # bounded
        (["../../outside.tfvars"], 422),  # escapes the checkout from stacks/app
        (["../../stacks/../../x.tfvars"], 422),
        (["../../stacks/app/prod.tfvars"], 201),  # goes up, stays inside
        ([f"f{i}.tfvars" for i in range(16)], 201),
    ],
)
async def test_validation(client: httpx.AsyncClient, var_files: list[str], status: int) -> None:
    admin = await login(client, "admin")
    stack_id = await _stack(client, admin, f"vf-val-{uuid.uuid4().hex[:6]}", "stacks/app")
    r = await _create_env(client, admin, stack_id, var_files)
    assert r.status_code == status, r.text


async def test_escape_is_judged_from_the_stack_project_root(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    stack_id = await _stack(client, admin, f"vf-root-{uuid.uuid4().hex[:6]}", ".")
    r = await _create_env(client, admin, stack_id, ["../shared/prod.tfvars"])
    assert r.status_code == 422 and "outside the repository" in r.text
    env_id = (await _create_env(client, admin, stack_id, ["prod.tfvars"])).json()["id"]
    r = await client.patch(
        f"/api/v1/environments/{env_id}", headers=admin, json={"var_files": ["../x.tfvars"]}
    )
    assert r.status_code == 422, r.text
    r = await client.patch(
        f"/api/v1/environments/{env_id}", headers=admin, json={"var_files": ["/x.tfvars"]}
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("managed_state", [True, False])
async def test_claim_payload_carries_them(client: httpx.AsyncClient, managed_state: bool) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"pool-vf-{uuid.uuid4().hex[:6]}")
    stack_id = await _stack(client, admin, f"vf-claim-{uuid.uuid4().hex[:6]}", "stacks/app")
    files = ["envs/prod.tfvars", "../shared/prod.tfvars"]
    r = await _create_env(client, admin, stack_id, files, managed_state=managed_state)
    assert r.status_code == 201, r.text
    await make_proposed_run(r.json()["id"])
    claim = await client.post("/worker/v1/jobs/claim", headers=wh)
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["var_files"] == files
    assert (payload["backend"] is not None) is managed_state


async def test_export_import_round_trip(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    space = await _space(client, admin, f"vf-src-{uuid.uuid4().hex[:6]}")
    stack_id = await _stack(client, admin, "app", "stacks/app", space)
    files = ["envs/prod.tfvars", "../shared/prod.tfvars"]
    assert (await _create_env(client, admin, stack_id, files)).status_code == 201

    doc = (await client.get(f"/api/v1/spaces/{space}/export", headers=admin)).json()
    [env_doc] = doc["stacks"][0]["environments"]
    assert env_doc["var_files"] == files

    target = await _space(client, admin, f"vf-dst-{uuid.uuid4().hex[:6]}")
    r = await client.post(f"/api/v1/spaces/{target}/import", headers=admin, json=doc)
    assert r.status_code == 200, r.text
    again = (await client.get(f"/api/v1/spaces/{target}/export", headers=admin)).json()
    assert again["stacks"][0]["environments"][0]["var_files"] == files

    # A changed list is an update of that field, audited like a PATCH.
    env_doc["var_files"] = ["envs/prod.tfvars"]
    rep = (await client.post(f"/api/v1/spaces/{target}/import", headers=admin, json=doc)).json()
    assert {
        "op": "updated",
        "kind": "environment",
        "path": "stacks/app/environments/prod",
        "fields": ["var_files"],
    } in rep["changes"]

    # Validated as on the API: shape (422 by the document model), and the checkout boundary.
    for bad in (["/abs.tfvars"], ["../../../x.tfvars"]):
        env_doc["var_files"] = bad
        r = await client.post(f"/api/v1/spaces/{target}/import", headers=admin, json=doc)
        assert r.status_code == 422, r.text
    assert "stacks/app/environments/prod" in r.text  # the boundary error names the env


async def test_a_clone_copies_them(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    space = await _space(client, admin, f"vf-clone-{uuid.uuid4().hex[:6]}")
    stack_id = await _stack(client, admin, f"vf-clone-{uuid.uuid4().hex[:6]}", ".", space)
    assert (await _create_env(client, admin, stack_id, ["prod.tfvars"])).status_code == 201
    r = await client.post(
        f"/api/v1/spaces/{space}/environment-clones",
        headers=admin,
        json={"reference_name": "prod", "name": "prod-eu"},
    )
    assert r.status_code in (200, 201), r.text
    envs = (await client.get(f"/api/v1/stacks/{stack_id}/environments", headers=admin)).json()
    clone = next(e for e in envs if e["name"] == "prod-eu")
    assert clone["var_files"] == ["prod.tfvars"]
