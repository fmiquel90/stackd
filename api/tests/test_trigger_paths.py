"""`stacks.trigger_paths` (SPECS §3.1, §9.6): extra repo-root-relative globs that concern a stack
besides its `project_root` — the matcher, its validation, and the push webhook filter."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models.audit import AuditEvent
from app.stacks.paths import (
    MAX_TRIGGER_PATH_LENGTH,
    MAX_TRIGGER_PATHS,
    matches_trigger_path,
    normalize_trigger_paths,
    touches_stack,
)
from tests.conftest_phase2 import login, make_env

SECRET = "whsecret-trigger"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        # `**` as a whole segment spans zero or more directories.
        ("modules/**", "modules/vpc/main.tf", True),
        ("modules/**", "modules/main.tf", True),
        ("modules/**", "modulesx/main.tf", False),
        ("**/*.tf", "main.tf", True),
        ("**/*.tf", "a/b/c.tf", True),
        ("**/*.tf", "a/b/c.tfvars", False),
        ("a/**/b.hcl", "a/b.hcl", True),
        ("a/**/b.hcl", "a/x/y/b.hcl", True),
        # `*` and `?` stay within one segment.
        ("shared/*.hcl", "shared/common.hcl", True),
        ("shared/*.hcl", "shared/sub/common.hcl", False),
        ("env/?.tfvars", "env/a.tfvars", True),
        ("env/?.tfvars", "env/ab.tfvars", False),
        # `**` inside a segment is a plain `*`.
        ("a**b/x", "aZZb/x", True),
        ("a**b/x", "a/b/x", False),
        # Character classes; an unclosed `[` is literal.
        ("env/[ps]*.tfvars", "env/prod.tfvars", True),
        ("env/[ps]*.tfvars", "env/dev.tfvars", False),
        ("weird/[x", "weird/[x", True),
        # A pattern matching a directory covers everything under it.
        ("modules/vpc", "modules/vpc/main.tf", True),
        ("modules/vpc", "modules/vpc", True),
        ("modules/vpc", "modules/vpc2/main.tf", False),
        ("modules/*", "modules/vpc/deep/main.tf", True),
        # Dotfiles are ordinary names; matching is case-sensitive.
        ("*", ".terraform.lock.hcl", True),
        ("modules/**", "modules/.hidden/x.tf", True),
        ("Modules/**", "modules/x.tf", False),
    ],
)
def test_matcher(pattern: str, path: str, expected: bool) -> None:
    assert matches_trigger_path(pattern, path) is expected


def test_touches_stack_combines_root_and_trigger_paths() -> None:
    assert touches_stack(["stacks/app/main.tf"], "stacks/app", [])
    assert touches_stack(["stacks/app/main.tf"], "./stacks/app/", [])
    assert not touches_stack(["stacks/application/main.tf"], "stacks/app", [])
    assert not touches_stack(["modules/vpc/main.tf"], "stacks/app", [])
    assert touches_stack(["modules/vpc/main.tf"], "stacks/app", ["modules/**"])
    assert touches_stack(["README.md", "modules/vpc/main.tf"], "stacks/app", ["modules/vpc"])
    assert touches_stack(["anything"], ".", [])
    assert not touches_stack([], ".", ["**"])  # no path, nothing touched
    # A rename out of the stack: the old path (given alongside the new one) still concerns it.
    assert touches_stack(["stacks/app/old.tf", "archive/old.tf"], "stacks/app", [])


def test_normalize_trigger_paths() -> None:
    assert normalize_trigger_paths(None) == []
    assert normalize_trigger_paths([" modules/** ", "shared/", "modules/**"]) == [
        "modules/**",
        "shared",
    ]
    for bad in ["", "  ", "/abs", "../up", "a/../b", "./a", "a//b", "a\\b", "a\nb"]:
        with pytest.raises(ValueError):
            normalize_trigger_paths([bad])
    with pytest.raises(ValueError):
        normalize_trigger_paths(["x" * (MAX_TRIGGER_PATH_LENGTH + 1)])
    with pytest.raises(ValueError):
        normalize_trigger_paths([f"p{i}" for i in range(MAX_TRIGGER_PATHS + 1)])
    assert len(normalize_trigger_paths([f"p{i}" for i in range(MAX_TRIGGER_PATHS)])) == 32


async def test_stack_api_validates_normalizes_and_audits(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "name": f"tp-{uuid.uuid4().hex[:6]}",
            "repo_url": "file:///r",
            "tool_version": "1.12",
            "trigger_paths": ["modules/**", "shared/"],
        },
    )
    assert r.status_code == 201, r.text
    stack = r.json()
    assert stack["trigger_paths"] == ["modules/**", "shared"]

    r = await client.post(
        "/api/v1/stacks",
        headers=admin,
        json={
            "name": f"tp-{uuid.uuid4().hex[:6]}",
            "repo_url": "file:///r",
            "tool_version": "1.12",
            "trigger_paths": ["/etc/**"],
        },
    )
    assert r.status_code == 422

    url = f"/api/v1/stacks/{stack['id']}"
    r = await client.patch(url, headers=admin, json={"trigger_paths": ["../escape"]})
    assert r.status_code == 422
    r = await client.patch(url, headers=admin, json={"trigger_paths": ["config/**"]})
    assert r.status_code == 200 and r.json()["trigger_paths"] == ["config/**"]
    r = await client.patch(url, headers=admin, json={"trigger_paths": None})
    assert r.status_code == 200 and r.json()["trigger_paths"] == []

    async with SessionLocal() as s:
        events = (
            (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "stack.updated",
                        AuditEvent.target_id == uuid.UUID(stack["id"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.context["fields"] for e in events] == [["trigger_paths"], ["trigger_paths"]]


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _push(client: httpx.AsyncClient, repo: str, after: str, commit: dict) -> int:
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "after": after,
            "repository": {"clone_url": repo},
            "commits": [commit],
        }
    ).encode()
    r = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sig(body),
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 200, r.text
    return int(r.json()["triggered"])


async def test_push_under_a_trigger_path_triggers_the_stack(client: httpx.AsyncClient) -> None:
    """Two stacks of one monorepo: a change under the shared module folder triggers only the
    stack that lists it; a change elsewhere triggers neither; both heads always advance."""
    admin = await login(client, "admin")
    repo = f"https://github.com/acme/mono-{uuid.uuid4().hex[:6]}"
    env_ids = {}
    for name, triggers in (("app", ["modules/**"]), ("net", [])):
        r = await client.post(
            "/api/v1/stacks",
            headers=admin,
            json={
                "name": f"{name}-{uuid.uuid4().hex[:6]}",
                "repo_url": repo,
                "tool_version": "1.12",
                "project_root": f"stacks/{name}",
                "trigger_paths": triggers,
            },
        )
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        await client.patch(f"/api/v1/stacks/{sid}", headers=admin, json={"webhook_secret": SECRET})
        env_ids[name] = await make_env(client, admin, sid, "main", "dev")

    async def runs(name: str) -> list[dict]:
        r = await client.get(f"/api/v1/environments/{env_ids[name]}/runs", headers=admin)
        return list(r.json())

    assert await _push(client, repo, "sha1", {"modified": ["modules/vpc/main.tf"]}) == 1
    assert [r["commit_sha"] for r in await runs("app")] == ["sha1"]
    assert await runs("net") == []
    net = (await client.get(f"/api/v1/environments/{env_ids['net']}", headers=admin)).json()
    assert net["head_sha"] == "sha1"  # not concerned, yet its head advances (§9.6)

    assert await _push(client, repo, "sha2", {"modified": ["docs/README.md"]}) == 0

    # A rename out of the shared folder lists the old path as removed: still concerns `app`.
    assert (
        await _push(
            client, repo, "sha3", {"added": ["archive/vpc.tf"], "removed": ["modules/vpc.tf"]}
        )
        == 1
    )
