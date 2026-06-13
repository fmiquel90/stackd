from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from tests.conftest_phase2 import login, make_env

REPO = "https://github.com/acme/demo"
SECRET = "whsecret123"


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _stack_with_webhook(client, admin, name: str) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={"name": name, "repo_url": REPO, "tool_version": "1.12.0"},
    )
    stack_id = r.json()["id"]
    await client.patch(f"/api/v1/stacks/{stack_id}", headers=admin, json={"webhook_secret": SECRET})
    env_id = await make_env(client, admin, stack_id, "main", "dev")
    return stack_id, env_id


async def test_push_updates_head_and_triggers(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-push")

    payload = {
        "ref": "refs/heads/main",
        "after": "abc1234def",
        "repository": {"clone_url": REPO + ".git", "html_url": REPO},
        "commits": [{"modified": ["main.tf"]}],
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sig(body),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["triggered"] == 1

    env = (await client.get(f"/api/v1/environments/{env_id}", headers=admin)).json()
    assert env["head_sha"] == "abc1234def"
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert runs[0]["triggered_by"] == "webhook"
    assert runs[0]["commit_sha"] == "abc1234def"


async def test_invalid_signature_rejected(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    await _stack_with_webhook(client, admin, "wh-bad")
    payload = {"ref": "refs/heads/main", "after": "x", "repository": {"clone_url": REPO}}
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


async def test_pull_request_creates_proposed_run(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    _stack, env_id = await _stack_with_webhook(client, admin, "wh-pr")
    payload = {
        "action": "opened",
        "repository": {"clone_url": REPO},
        "pull_request": {"base": {"ref": "main"}, "head": {"sha": "prsha999"}},
    }
    body = json.dumps(payload).encode()
    resp = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)},
    )
    assert resp.status_code == 200
    runs = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()
    assert any(r["type"] == "proposed" and r["commit_sha"] == "prsha999" for r in runs)
