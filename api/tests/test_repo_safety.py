"""Repo-introspection safety: discovery never reads through a symlink out of the clone, the clone
cleanup stays off the event loop, and git output never carries the repo token (invariant 3)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.enums import RepoAuthKind
from app.environments.router import _scan_clone
from app.errors import ProblemException
from app.stacks import git
from app.stacks.git import check_repo, clone_shallow, enforce_clone_budget
from app.variables.discovery import parse_inputs, tf_files

_TOKEN = "ghp_FAKEtoken0123456789abcdef"


def _outside(base: Path) -> Path:
    """A directory OUTSIDE the clone holding a .tf and a large file a symlink could point at."""
    out = base / "outside"
    out.mkdir()
    (out / "leak.tf").write_text('variable "leaked" { type = string }\n')
    (out / "big.bin").write_bytes(b"0" * 4096)
    return out


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def rmtree_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Record each shutil.rmtree call as (path, ran ON the event loop?), then delegate."""
    calls: list[tuple[str, bool]] = []
    real = shutil.rmtree

    def spy(path: Any, *args: Any, **kwargs: Any) -> None:
        calls.append((str(path), _on_loop()))
        real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", spy)
    return calls


# --- F5: symlinks never followed out of the clone ---


def test_parse_inputs_skips_symlinks(tmp_path: Path) -> None:
    out = _outside(tmp_path)
    root = tmp_path / "clone"
    root.mkdir()
    (root / "main.tf").write_text('variable "region" { type = string }\n')
    (root / "evil.tf").symlink_to(out / "leak.tf")  # file symlink escaping the clone
    (root / "dir.tf").symlink_to(out, target_is_directory=True)  # symlinked dir named *.tf
    (root / "modules").symlink_to(out, target_is_directory=True)

    assert tf_files(root) == [root / "main.tf"]
    assert {i.name for i in parse_inputs(root)} == {"region"}


def test_scan_clone_ignores_symlinked_tf(tmp_path: Path) -> None:
    out = _outside(tmp_path)
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "main.tf").write_text('variable "region" { type = string }\n')
    (dest / "evil.tf").symlink_to(out / "leak.tf")

    # Cap of 1 .tf file: the symlink is neither counted nor parsed.
    found = _scan_clone(dest, ".", max_mb=1, max_tf_files=1)
    assert [i.name for i in found] == ["region"]


@pytest.mark.parametrize("project_root", ["infra", "../clone2"])
def test_scan_clone_rejects_project_root_outside_clone(tmp_path: Path, project_root: str) -> None:
    out = _outside(tmp_path)
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "infra").symlink_to(out, target_is_directory=True)  # symlinked dir escaping the clone
    # A sibling sharing the clone's name as a string prefix (the old startswith check admitted it).
    (tmp_path / "clone2").mkdir()
    (tmp_path / "clone2" / "x.tf").write_text('variable "sibling" {}\n')

    with pytest.raises(ProblemException) as exc:
        _scan_clone(dest, project_root, max_mb=1, max_tf_files=10)
    assert exc.value.status == 400


def test_clone_budget_does_not_follow_symlinks(tmp_path: Path) -> None:
    out = _outside(tmp_path)
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "empty.tf").write_text("")
    (dest / "big.bin").symlink_to(out / "big.bin")
    (dest / "linked").symlink_to(out, target_is_directory=True)

    # 0 MB cap: any byte counted would trip it — the 4 KiB behind both symlinks must not be.
    enforce_clone_budget(dest, max_mb=0)


def _git(*args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], env=env, check=True)


def _make_repo(outside: Path | None = None) -> str:
    """A throwaway git repo (branch main); with `outside`, it also commits symlinks escaping it."""
    d = Path(tempfile.mkdtemp(prefix="stackd-test-repo-"))
    (d / "main.tf").write_text('variable "region" { type = string }\n')
    if outside is not None:
        (d / "leak.tf").symlink_to(outside / "leak.tf")
        (d / "modules").symlink_to(outside, target_is_directory=True)
        assert (d / "leak.tf").is_symlink()
    _git("init", "-q", "-b", "main", str(d))
    _git("-C", str(d), "add", ".")
    _git("-C", str(d), "commit", "-q", "-m", "init")
    return str(d)


async def _discover(client: httpx.AsyncClient, repo: str, name: str) -> httpx.Response:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post(
        "/api/v1/stacks",
        headers=h,
        json={"name": name, "repo_url": f"file://{repo}", "tool_version": "1.12.0"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/api/v1/stacks/{r.json()['id']}/environments",
        headers=h,
        json={"name": "dev", "tier": "dev", "branch": "main"},
    )
    assert r.status_code == 201, r.text
    return await client.post(f"/api/v1/environments/{r.json()['id']}/discover-inputs", headers=h)


async def test_discover_inputs_ignores_cloned_symlinks(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    r = await _discover(client, _make_repo(_outside(tmp_path)), "symlink-discover-stack")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == ["region"]  # `leaked` lives behind the symlink → never read
    assert body["required_total"] == 1


# --- F15: clone cleanup off the event loop ---


async def test_discover_inputs_cleanup_off_loop(
    client: httpx.AsyncClient, rmtree_calls: list[tuple[str, bool]]
) -> None:
    r = await _discover(client, _make_repo(), "cleanup-discover-stack")
    assert r.status_code == 200, r.text
    assert rmtree_calls and not any(on_loop for _, on_loop in rmtree_calls)


# --- F2: the repo token never surfaces in git errors ---


class _FailingProc:
    returncode = 128

    def __init__(self, err: bytes) -> None:
        self._err = err

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", self._err


def _fake_git(monkeypatch: pytest.MonkeyPatch, argv: list[str], *, pad: int = 0) -> None:
    """git fails and echoes the (authed) URL it was given — `pad` pushes it to the 500-char cut."""

    async def fake(*args: str, **_: Any) -> _FailingProc:
        argv.extend(args)
        url = next(a for a in args if "://" in a)
        return _FailingProc(f"{'x' * pad}fatal: unable to access '{url}/': 403".encode())

    monkeypatch.setattr(git.asyncio, "create_subprocess_exec", fake)


@pytest.mark.parametrize("pad", [0, 445])  # 445: the token straddles the 500-char cut
async def test_check_repo_masks_token(monkeypatch: pytest.MonkeyPatch, pad: int) -> None:
    argv: list[str] = []
    _fake_git(monkeypatch, argv, pad=pad)

    ok, branches, detail = await check_repo(
        "https://git.example.com/org/repo.git", RepoAuthKind.token, _TOKEN
    )
    assert any(_TOKEN in a for a in argv)  # git really got the token-bearing URL
    assert not ok and branches == []
    assert detail is not None and "•••" in detail
    assert _TOKEN[:6] not in detail  # masked before truncation: no partial token either


async def test_clone_shallow_masks_token_and_cleans_up_off_loop(
    monkeypatch: pytest.MonkeyPatch, rmtree_calls: list[tuple[str, bool]]
) -> None:
    argv: list[str] = []
    _fake_git(monkeypatch, argv)

    with pytest.raises(ProblemException) as exc:
        await clone_shallow(
            "https://git.example.com/org/repo.git", RepoAuthKind.token, _TOKEN, "main"
        )
    assert exc.value.status == 502
    assert exc.value.detail is not None
    assert _TOKEN not in exc.value.detail and "•••" in exc.value.detail
    assert rmtree_calls == [(argv[-1], False)]  # the temp clone dir is removed, off the loop
