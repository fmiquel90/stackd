"""Repo-introspection safety: discovery only reads regular files inside the clone (in-clone
symlinks included), the clone cleanup stays off the event loop, git is killed on timeout, the repo
token is percent-encoded into the URL, and git output never carries it in any form (invariant 3)."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest

from app.enums import RepoAuthKind
from app.environments.router import _scan_clone
from app.errors import ProblemException
from app.stacks import git
from app.stacks.git import check_repo, clone_shallow, enforce_clone_budget, ls_remote_sha
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


# --- F5: only regular files inside the clone are read (in-clone symlinks included) ---


def _clone_with_links(base: Path) -> Path:
    """clone/env is the project root; only main.tf and the in-clone variables.tf are readable."""
    out = _outside(base)
    clone = base / "clone"
    (clone / "shared").mkdir(parents=True)
    (clone / "shared" / "vars.tf").write_text('variable "shared" { type = string }\n')
    os.mkfifo(clone / "fifo")  # stands in for /dev/zero: opening it would block, never done
    env = clone / "env"
    env.mkdir()
    (env / "main.tf").write_text('variable "region" { type = string }\n')
    (env / "variables.tf").symlink_to("../shared/vars.tf")  # in-clone shared file → read
    (env / "evil.tf").symlink_to(out / "leak.tf")  # escapes the clone
    (env / "pipe.tf").symlink_to("../fifo")  # in-clone but not a regular file
    os.mkfifo(env / "raw.tf")  # a FIFO itself, no link
    (env / "dir.tf").symlink_to("../shared", target_is_directory=True)  # a directory
    (env / "gone.tf").symlink_to("../nope.tf")  # dangling
    (env / "loop.tf").symlink_to("loop.tf")  # loops
    (env / "modules").symlink_to(out, target_is_directory=True)  # symlinked dir: never walked
    return clone


def test_tf_files_keeps_only_regular_files_inside_clone(tmp_path: Path) -> None:
    clone = _clone_with_links(tmp_path)
    env = clone / "env"
    in_clone = [(env / "main.tf").resolve(), (clone / "shared" / "vars.tf").resolve()]

    # tf_files never opens anything — check it first, so a regression can't hang on the FIFOs.
    assert tf_files(env, clone) == in_clone
    assert {i.name for i in parse_inputs(env, clone)} == {"region", "shared"}
    # Without a clone root the bound is the project root itself: ../shared is out of reach.
    assert tf_files(env, env) == in_clone[:1]
    assert {i.name for i in parse_inputs(env)} == {"region"}


def test_scan_clone_counts_and_reads_only_safe_tf(tmp_path: Path) -> None:
    clone = _clone_with_links(tmp_path)

    # Cap of 2 .tf files: main.tf + the in-clone variables.tf; the unsafe entries don't count.
    found = _scan_clone(clone, "env", max_mb=1, max_tf_files=2)
    assert {i.name for i in found} == {"region", "shared"}


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
    """A throwaway git repo (branch main); with `outside`, it also commits an in-repo symlinked
    variables.tf plus symlinks escaping the repo."""
    d = Path(tempfile.mkdtemp(prefix="stackd-test-repo-"))
    (d / "main.tf").write_text('variable "region" { type = string }\n')
    if outside is not None:
        (d / "shared").mkdir()
        (d / "shared" / "vars.tf").write_text('variable "shared" { type = string }\n')
        (d / "variables.tf").symlink_to("shared/vars.tf")
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


async def test_discover_inputs_follows_only_in_clone_symlinks(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    r = await _discover(client, _make_repo(_outside(tmp_path)), "symlink-discover-stack")
    assert r.status_code == 200, r.text
    body = r.json()
    # `shared` via the in-repo link is discovered; `leaked` lives outside → never read.
    assert set(body["created"]) == {"region", "shared"}
    assert body["required_total"] == 2


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


# --- the token is percent-encoded into the URL userinfo, and masked in both forms ---

# Each character that ends or splits a raw userinfo, a token that would re-point git at another
# host, and a literal `%25` (its raw form is a substring of its encoded form `%2525`).
_SPECIAL_TOKENS = [
    "tok@en",
    "tok:en",
    "tok/en",
    "tok%en",
    "tok#en",
    "tok?en",
    "tok%25",
    "x@127.0.0.2:1/#?%",
]
_HTTPS_REPOS = [
    "https://git.example.com/org/repo.git",
    "https://git.example.com:8443/org/repo.git",
    "https://[::1]:8443/org/repo.git",
    "https://someone@git.example.com/org/repo.git",
]


@pytest.mark.parametrize("repo_url", _HTTPS_REPOS)
@pytest.mark.parametrize("token", _SPECIAL_TOKENS)
def test_authed_url_percent_encodes_token(repo_url: str, token: str) -> None:
    orig = urlsplit(repo_url)
    authed = urlsplit(git._authed_url(repo_url, RepoAuthKind.token, token))
    # Same address git would dial without a token: nothing leaked out of the userinfo.
    for part in ("scheme", "hostname", "port", "path", "query", "fragment"):
        assert getattr(authed, part) == getattr(orig, part), part
    assert authed.username == "x-access-token"
    assert authed.password is not None and unquote(authed.password) == token


@pytest.mark.parametrize(
    ("repo_url", "auth_kind", "secret"),
    [
        ("file:///stackd-dev/repos/demo-app", RepoAuthKind.none, None),
        ("file:///stackd-dev/repos/demo-app", RepoAuthKind.token, "tok@en"),  # not HTTP(S)
        ("https://git.example.com/org/repo.git", RepoAuthKind.none, None),
        ("https://git.example.com/org/repo.git", RepoAuthKind.none, "tok@en"),
        ("https://git.example.com/org/repo.git", RepoAuthKind.token, None),
    ],
)
def test_authed_url_leaves_other_urls_unchanged(
    repo_url: str, auth_kind: RepoAuthKind, secret: str | None
) -> None:
    assert git._authed_url(repo_url, auth_kind, secret) == repo_url


def _fake_git_echoing(monkeypatch: pytest.MonkeyPatch, urls: list[str]) -> None:
    """git fails and echoes the token in both forms it holds: inside the authed URL it was given
    (percent-encoded) and decoded from it (raw)."""

    async def fake(*args: str, **_: Any) -> _FailingProc:
        url = next(a for a in args if "://" in a)
        urls.append(url)
        raw = unquote(urlsplit(url).password or "")
        return _FailingProc(f"fatal: unable to access '{url}/': 403 for {raw}".encode())

    monkeypatch.setattr(git.asyncio, "create_subprocess_exec", fake)


@pytest.mark.parametrize("op", ["check_repo", "clone_shallow"])
@pytest.mark.parametrize("token", _SPECIAL_TOKENS)
async def test_git_errors_mask_both_token_forms(
    monkeypatch: pytest.MonkeyPatch, op: str, token: str
) -> None:
    urls: list[str] = []
    _fake_git_echoing(monkeypatch, urls)
    repo_url = "https://git.example.com/org/repo.git"

    detail: str | None
    if op == "check_repo":
        ok, _, detail = await check_repo(repo_url, RepoAuthKind.token, token)
        assert not ok
    else:
        with pytest.raises(ProblemException) as exc:
            await clone_shallow(repo_url, RepoAuthKind.token, token, "main")
        assert exc.value.status == 502
        detail = exc.value.detail

    [url] = urls
    assert unquote(urlsplit(url).password or "") == token  # git really got the token
    # Exact match: no form of the token survives, not even the tail of a partially masked one.
    assert detail == (
        "fatal: unable to access 'https://x-access-token:•••@git.example.com/org/repo.git/': "
        "403 for •••"
    )


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_real_git_dials_the_repo_host_not_one_in_the_token() -> None:
    """Real git, loopback only: with a raw token, git would dial 127.0.0.2:1 (from the token)."""
    token, port = "x@127.0.0.2:1/#?%", _closed_port()

    ok, _, detail = await check_repo(
        f"https://127.0.0.1:{port}/org/repo.git", RepoAuthKind.token, token
    )
    assert not ok and detail is not None
    assert f"127.0.0.1 port {port}" in detail, detail  # curl's connection-refused message
    assert "127.0.0.2" not in detail
    assert token not in detail and quote(token, safe="") not in detail


# --- git is killed and reaped on timeout ---


@pytest.fixture
def hanging_git(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[asyncio.subprocess.Process]]:
    """Every git call becomes a real `sleep 30`, and every git timeout shrinks to 0.2s."""
    procs: list[asyncio.subprocess.Process] = []
    real_exec, real_timeout = asyncio.create_subprocess_exec, asyncio.timeout

    async def spawn(*_: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        procs.append(await real_exec("sleep", "30", **kwargs))
        return procs[-1]

    monkeypatch.setattr(git.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(git.asyncio, "timeout", lambda _: real_timeout(0.2))
    yield procs
    for p in procs:  # a regression must not leave sleeps behind
        if p.returncode is None:
            p.kill()


async def test_git_killed_and_reaped_on_timeout(
    hanging_git: list[asyncio.subprocess.Process], rmtree_calls: list[tuple[str, bool]]
) -> None:
    url = "https://git.example.com/org/repo.git"
    with pytest.raises(ProblemException) as exc:
        await clone_shallow(url, RepoAuthKind.none, None, "main")
    assert exc.value.status == 502
    assert len(rmtree_calls) == 1  # the temp clone dir is removed once git is dead
    ok, _, _ = await check_repo(url, RepoAuthKind.none, None)
    assert not ok
    assert await ls_remote_sha(url, RepoAuthKind.none, None, "main") is None

    # returncode is only set once the child is reaped: killed (SIGKILL) and awaited, all three.
    assert [p.returncode for p in hanging_git] == [-signal.SIGKILL] * 3
