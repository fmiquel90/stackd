"""repo_auth_kind=deploy_key (SPECS §3.1, §7.2, §13): the key is validated, write-only, encrypted at
rest, never returned nor exported, handed to the job's claim (and masked there), used by the API's
own git calls through a 0600 file that does not outlive them, and never sent to the VCS API."""

from __future__ import annotations

import base64
import shutil
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import select

from app.crypto import decrypt
from app.db import SessionLocal
from app.enums import RepoAuthKind
from app.errors import ProblemException
from app.models.audit import AuditEvent
from app.models.stack import Stack
from app.stacks import git
from app.stacks.credentials import credential_mask_values, normalize_deploy_key
from tests.conftest_phase2 import event, login, make_env, register_worker


def _openssh_key(cipher: bytes = b"none") -> str:
    """An `openssh-key-v1` blob with `cipher`: what the validator reads of it."""
    blob = b"openssh-key-v1\0" + len(cipher).to_bytes(4, "big") + cipher + b"\0" * 64
    body = base64.b64encode(blob).decode()
    lines = [body[i : i + 70] for i in range(0, len(body), 70)]
    return "\n".join(
        ["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----"]
    )


KEY = _openssh_key() + "\n"
SSH_URL = "git@git.example.com:org/infra.git"


def test_a_private_key_without_a_passphrase_is_accepted_and_normalized() -> None:
    crlf = KEY.strip().replace("\n", "\r\n")
    assert normalize_deploy_key(f"  {crlf}  ") == KEY  # LF, one trailing newline (ssh needs it)
    pkcs8 = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"
    assert normalize_deploy_key(pkcs8) == pkcs8 + "\n"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
@pytest.mark.parametrize("key_type", ["ed25519", "rsa", "ecdsa"])
def test_keys_from_ssh_keygen(tmp_path: Path, key_type: str) -> None:
    for name, passphrase in (("plain", ""), ("locked", "pass phrase")):
        path = tmp_path / f"{key_type}-{name}"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", key_type, "-N", passphrase, "-f", str(path)], check=True
        )
        if passphrase:
            with pytest.raises(ProblemException, match="passphrase"):
                normalize_deploy_key(path.read_text())
        else:
            assert normalize_deploy_key(path.read_text()) == path.read_text()
        with pytest.raises(ProblemException, match="public key"):
            normalize_deploy_key((tmp_path / f"{key_type}-{name}.pub").read_text())


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (_openssh_key(b"aes256-ctr"), "passphrase"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\n"
            "abc\n-----END RSA PRIVATE KEY-----",
            "passphrase",
        ),
        ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----", "pass"),
        ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA user@host", "public key"),
        ("ghp_not_a_key", "Expected an SSH private key"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\n!!!\n-----END OPENSSH PRIVATE KEY-----", "base64"),
        ("-----BEGIN DSA PRIVATE KEY-----\nabc\n-----END DSA PRIVATE KEY-----", "Unsupported"),
    ],
)
def test_what_is_not_a_usable_deploy_key_is_refused(text: str, why: str) -> None:
    with pytest.raises(ProblemException, match=why) as exc:
        normalize_deploy_key(text)
    assert exc.value.status == 422
    assert "abc" not in str(exc.value.detail) and "AAAA" not in str(exc.value.detail)


def test_mask_values_cover_the_key_line_by_line_and_the_token_url_encoded() -> None:
    values = credential_mask_values(RepoAuthKind.deploy_key, KEY)
    assert values[0] == KEY.strip()
    body = [line for line in KEY.splitlines() if not line.startswith("-----")]
    assert [v for v in body if len(v) >= 16] == values[1:]
    assert not any(v.startswith("-----") for v in values[1:])
    token = "ghp_t0k@en/%25"
    assert credential_mask_values(RepoAuthKind.token, token) == [token, quote(token, safe="")]


async def _stack(client: httpx.AsyncClient, h: dict, name: str, **extra: object) -> httpx.Response:
    body = {"name": name, "repo_url": SSH_URL, "tool_version": "1.12", **extra}
    return await client.post("/api/v1/stacks", headers=h, json=body)


async def test_deploy_key_is_write_only_and_encrypted(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    name = f"dk-{uuid.uuid4().hex[:6]}"
    r = await _stack(client, admin, name, repo_auth_kind="deploy_key", repo_secret=KEY.strip())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["repo_auth_kind"] == "deploy_key" and out["has_repo_secret"] is True
    assert "repo_secret" not in out and "PRIVATE KEY" not in r.text
    got = await client.get(f"/api/v1/stacks/{out['id']}", headers=admin)
    assert "PRIVATE KEY" not in got.text and got.json()["has_repo_secret"] is True

    async with SessionLocal() as s:
        stack = await s.get(Stack, uuid.UUID(out["id"]))
        assert stack is not None and stack.repo_secret_encrypted is not None
        assert b"PRIVATE KEY" not in stack.repo_secret_encrypted
        assert decrypt(stack.repo_secret_encrypted) == KEY  # normalized: trailing newline
        audit = (
            await s.execute(select(AuditEvent).where(AuditEvent.target_id == stack.id))
        ).scalars()
        assert all("PRIVATE KEY" not in str(a.context) for a in audit)

    # Rotation: a new key replaces it, still write-only; the audit names the field only.
    rotated = _openssh_key() + "\n"
    r = await client.patch(
        f"/api/v1/stacks/{out['id']}", headers=admin, json={"repo_secret": rotated}
    )
    assert r.status_code == 200 and "PRIVATE KEY" not in r.text


async def test_deploy_key_needs_an_ssh_url_and_a_real_key(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    https = await _stack(
        client,
        admin,
        f"dk-{uuid.uuid4().hex[:6]}",
        repo_url="https://git.example.com/org/infra.git",
        repo_auth_kind="deploy_key",
        repo_secret=KEY,
    )
    assert https.status_code == 422 and "SSH" in https.json()["detail"]
    bad = await _stack(
        client, admin, f"dk-{uuid.uuid4().hex[:6]}", repo_auth_kind="deploy_key", repo_secret="x"
    )
    assert bad.status_code == 422
    as_token = await _stack(
        client,
        admin,
        f"dk-{uuid.uuid4().hex[:6]}",
        repo_url="https://git.example.com/org/infra.git",
        repo_auth_kind="token",
        repo_secret=KEY,
    )
    assert as_token.status_code == 422 and "deploy_key" in as_token.json()["detail"]
    assert "PRIVATE KEY" not in https.text + bad.text + as_token.text

    ok = await _stack(
        client, admin, f"dk-{uuid.uuid4().hex[:6]}", repo_auth_kind="deploy_key", repo_secret=KEY
    )
    sid = ok.json()["id"]
    # Moving a deploy_key stack to an HTTPS URL, or to token auth without a token: refused.
    moved = await client.patch(
        f"/api/v1/stacks/{sid}", headers=admin, json={"repo_url": "https://h.example/x.git"}
    )
    assert moved.status_code == 422
    switched = await client.patch(
        f"/api/v1/stacks/{sid}",
        headers=admin,
        json={"repo_auth_kind": "token", "repo_url": "https://h.example/x.git"},
    )
    assert switched.status_code == 422 and "repo_secret" in switched.json()["detail"]
    with_token = await client.patch(
        f"/api/v1/stacks/{sid}",
        headers=admin,
        json={
            "repo_auth_kind": "token",
            "repo_url": "https://h.example/x.git",
            "repo_secret": "ghp_new",
        },
    )
    assert with_token.status_code == 200, with_token.text


async def test_claim_hands_the_key_to_the_job_and_masks_it(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"dk-pool-{uuid.uuid4().hex[:6]}")
    r = await _stack(
        client, admin, f"dk-{uuid.uuid4().hex[:6]}", repo_auth_kind="deploy_key", repo_secret=KEY
    )
    env_id = await make_env(client, admin, r.json()["id"], "dev", "dev")
    await client.post(f"/api/v1/environments/{env_id}/runs", headers=admin, json={})
    job = (await client.post("/worker/v1/jobs/claim", headers=wh)).json()

    assert job["repo_credentials"] == {"kind": "deploy_key", "ssh_private_key": KEY}
    assert set(credential_mask_values(RepoAuthKind.deploy_key, KEY)) <= set(job["mask_values"])
    assert job["environment"]["repo_url"] == SSH_URL


async def test_export_never_carries_the_key(client: httpx.AsyncClient) -> None:
    admin = await login(client, "admin")
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": uuid.uuid4().hex[:8]})
    sp = space.json()["id"]
    r = await _stack(
        client, admin, "net", space_id=sp, repo_auth_kind="deploy_key", repo_secret=KEY
    )
    assert r.status_code == 201, r.text
    doc = (await client.get(f"/api/v1/spaces/{sp}/export", headers=admin)).json()

    assert "PRIVATE KEY" not in str(doc) and KEY.splitlines()[1] not in str(doc)
    [stack] = doc["stacks"]
    assert (stack["repo_auth_kind"], stack["has_repo_secret"]) == ("deploy_key", True)
    assert {"stack": "net", "item": "repo_secret"}.items() <= next(
        m for m in doc["missing_secrets"] if m["item"] == "repo_secret"
    ).items()


def test_api_git_reaches_the_repo_with_the_key_in_a_transient_file() -> None:
    with git._git_access(SSH_URL, RepoAuthKind.deploy_key, KEY.strip()) as (url, env):
        assert url == SSH_URL  # nothing injected in the URL
        ssh = env["GIT_SSH_COMMAND"].split()
        assert ssh[:2] == ["ssh", "-i"] and "IdentitiesOnly=yes" in ssh
        key = Path(ssh[2])
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
        assert key.read_text() == KEY
    assert not key.exists() and not key.parent.exists()


async def test_check_repo_reports_ssh_failures_without_the_key() -> None:
    ok, branches, detail = await git.check_repo(
        "ssh://git@127.0.0.1:1/org/none.git", RepoAuthKind.deploy_key, KEY
    )
    assert not ok and branches == []
    assert detail and "PRIVATE KEY" not in detail


async def test_vcs_post_back_never_uses_a_deploy_key(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import update

    from app.vcs import dispatcher, github
    from tests.test_vcs import _pr
    from tests.test_vcs import _stack as vcs_stack

    admin = await login(client, "admin")
    wh = await register_worker(client, admin, f"vcs-dk-pool-{uuid.uuid4().hex[:6]}")
    sid, env_id, repo = await vcs_stack(client, admin, f"vcs-dk-{uuid.uuid4().hex[:6]}")
    async with SessionLocal() as s:
        await s.execute(
            update(Stack)
            .where(Stack.id == uuid.UUID(sid))
            .values(repo_auth_kind=RepoAuthKind.deploy_key)
        )
        await s.commit()
    assert (await _pr(client, repo, sha="prsha-dk", number=3)).status_code == 200
    run_id = (await client.get(f"/api/v1/environments/{env_id}/runs", headers=admin)).json()[0][
        "id"
    ]
    assert (await client.post("/worker/v1/jobs/claim", headers=wh)).status_code == 200
    await event(client, wh, run_id, "phase_started", phase="planning")

    tokens: list[str] = []

    async def spy(token, *args, **kw):  # type: ignore[no-untyped-def]
        tokens.append(token)
        return 1

    monkeypatch.setattr(github, "set_status", spy)
    monkeypatch.setattr(github, "upsert_comment", spy)
    async with SessionLocal() as s:
        assert await dispatcher.dispatch_vcs(s, datetime.now(UTC)) >= 1  # rows were there…
    assert tokens == []  # …and dropped: no PAT to post with


async def test_import_switching_the_kind_drops_the_stored_credential(
    client: httpx.AsyncClient,
) -> None:
    admin = await login(client, "admin")
    space = await client.post("/api/v1/spaces", headers=admin, json={"name": uuid.uuid4().hex[:8]})
    sp = space.json()["id"]
    r = await _stack(
        client,
        admin,
        "net",
        space_id=sp,
        repo_url="https://git.example.com/org/infra.git",
        repo_auth_kind="token",
        repo_secret="ghp_old",
    )
    assert r.status_code == 201, r.text
    doc = (await client.get(f"/api/v1/spaces/{sp}/export", headers=admin)).json()

    https_key = {**doc["stacks"][0], "repo_auth_kind": "deploy_key"}
    bad = await client.post(
        f"/api/v1/spaces/{sp}/import", headers=admin, json={**doc, "stacks": [https_key]}
    )
    assert bad.status_code == 422  # a deploy key needs an SSH URL

    switched = {**https_key, "repo_url": SSH_URL}
    rep = await client.post(
        f"/api/v1/spaces/{sp}/import", headers=admin, json={**doc, "stacks": [switched]}
    )
    assert rep.status_code == 200, rep.text
    assert any(m["item"] == "repo_secret" for m in rep.json()["missing_secrets"])
    async with SessionLocal() as s:
        stack = await s.get(Stack, uuid.UUID(r.json()["id"]))
        assert stack is not None
        assert stack.repo_auth_kind == RepoAuthKind.deploy_key
        assert stack.repo_secret_encrypted is None  # the token can't serve as a key
