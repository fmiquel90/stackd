from __future__ import annotations

import base64
import json
import uuid

import httpx
from moto import mock_aws

from tests.conftest_phase2 import login, make_env, make_stack


def _basic(token: str) -> dict[str, str]:
    raw = base64.b64encode(f"env:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _state_doc(serial: int) -> bytes:
    return json.dumps({"version": 4, "serial": serial, "lineage": "abc", "outputs": {}}).encode()


async def test_state_lock_post_get_unlock(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    rw = mint_state_token(
        environment_id=uuid.UUID(env_id), run_id=uuid.uuid4(), scope="rw", ttl_seconds=3600
    )

    with mock_aws():
        lock_body = json.dumps({"ID": "lock-1", "Operation": "OperationTypePlan"})
        first = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert first.status_code == 200

        # Second lock holder → 423 with the current holder info.
        second = await client.request(
            "LOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert second.status_code == 423

        post = await client.post(
            f"/state/v1/{env_id}?ID=lock-1", headers=_basic(rw), content=_state_doc(1)
        )
        assert post.status_code == 200

        # Regressive serial refused.
        regressive = await client.post(
            f"/state/v1/{env_id}?ID=lock-1", headers=_basic(rw), content=_state_doc(0)
        )
        assert regressive.status_code == 409

        got = await client.get(f"/state/v1/{env_id}", headers=_basic(rw))
        assert got.status_code == 200
        assert json.loads(got.content)["serial"] == 1

        unlock = await client.request(
            "UNLOCK", f"/state/v1/{env_id}/lock", headers=_basic(rw), content=lock_body
        )
        assert unlock.status_code == 200


async def test_readonly_token_cannot_write(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-ro-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    ro = mint_state_token(
        environment_id=uuid.UUID(env_id), run_id=uuid.uuid4(), scope="ro", ttl_seconds=3600
    )
    with mock_aws():
        post = await client.post(f"/state/v1/{env_id}", headers=_basic(ro), content=_state_doc(1))
        assert post.status_code == 403  # proposed runs get read-only state (§13)


async def test_state_token_scoped_to_env(client: httpx.AsyncClient) -> None:
    from app.statebackend.tokens import mint_state_token

    admin = await login(client, "admin")
    stack = await make_stack(client, admin, "state-scope-stack")
    env_id = await make_env(client, admin, stack, "dev", "dev")
    other = mint_state_token(
        environment_id=uuid.uuid4(), run_id=uuid.uuid4(), scope="rw", ttl_seconds=3600
    )
    got = await client.get(f"/state/v1/{env_id}", headers=_basic(other))
    assert got.status_code == 403
