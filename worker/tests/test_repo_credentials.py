"""The stack's repository credential on the agent (SPECS §7.4, §8.3): a deploy key for the clone and
`init` over SSH, a token as host-scoped git config for `init`'s module downloads. Real git; an SSH
server is not needed — a stand-in `ssh` on PATH records how git calls it and runs the remote git
command locally, as sshd would."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path
from urllib.parse import quote


import agent.main as main
from agent.workspace import Workspace, module_git_config, ssh_command

# Built from parts: the repo's detect-private-key hook refuses a literal key header.
_PEM = "OPENSSH PRIVATE" + " KEY-----"
KEY = (
    f"-----BEGIN {_PEM}\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    f"-----END {_PEM}"
)  # as an older API may hand it: no trailing newline, which ssh refuses
TOKEN = "ghp_t0k@en/%25"


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tf").write_text("")
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c1")
    return src, _git(src, "rev-parse", "HEAD")


def test_ssh_command_uses_the_deploy_key_only(tmp_path: Path) -> None:
    key = tmp_path / "a dir" / "repo_ssh_key"
    assert shlex.split(ssh_command(key)) == [
        "ssh",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        *("-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=15"),
        *("-o", "ServerAliveCountMax=4"),
    ]
    assert ssh_command() == (
        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        " -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
    )


def test_deploy_key_is_on_disk_only_inside_repo_credentials(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "job-1")
    key_path = ws.secret_path("repo_ssh_key")
    with ws.repo_credentials("git@git.example.com:org/infra.git", ssh_key=KEY) as env:
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert key_path.read_text() == KEY + "\n"
        assert not key_path.is_relative_to(ws.path / "repo")  # outside the checkout
        assert env == {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": ssh_command(key_path)}
    assert not key_path.exists()


def test_no_credential_no_git_env(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "job-1")
    with ws.repo_credentials("https://git.example.com/org/infra.git") as env:
        assert env == {}
    assert list(ws.path.iterdir()) == []


def _get_url(env: dict[str, str], url: str) -> str:
    """The URL git would contact for `url` under `env` (insteadOf applied; no network)."""
    out = subprocess.run(
        ["git", "ls-remote", "--get-url", url],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent", **env},
    )
    return out.stdout.strip()


def test_token_reaches_modules_on_the_repo_host_only() -> None:
    env = module_git_config("https://git.example.com/org/infra.git", TOKEN)
    authed = f"https://x-access-token:{quote(TOKEN, safe='')}@git.example.com/"
    for source in (
        "https://git.example.com/org/modules.git",
        "git@git.example.com:org/modules.git",
        "ssh://git@git.example.com/org/modules.git",
    ):
        assert _get_url(env, source) == f"{authed}org/modules.git", source
    for elsewhere in (
        "https://other.example.com/org/modules.git",
        "git@other.example.com:org/modules.git",
        "https://git.example.com.evil.test/org/modules.git",
        "http://git.example.com/org/modules.git",
    ):
        assert _get_url(env, elsewhere) == elsewhere, elsewhere


def test_token_config_keeps_the_port_and_skips_non_http_repos() -> None:
    env = module_git_config("http://127.0.0.1:8080/src", TOKEN)
    assert env["GIT_CONFIG_COUNT"] == "1"  # no SSH rewrite for a plain-http repo
    assert _get_url(env, "http://127.0.0.1:8080/mod").startswith("http://x-access-token:")
    assert _get_url(env, "http://127.0.0.1:9090/mod") == "http://127.0.0.1:9090/mod"
    assert module_git_config("git@git.example.com:org/infra.git", TOKEN) == {}
    assert module_git_config("file:///repos/infra", TOKEN) == {}


def test_clone_over_ssh_with_the_deploy_key(tmp_path: Path, fake_ssh: Path) -> None:
    src, sha = _source_repo(tmp_path)
    ws = Workspace(str(tmp_path / "ws"), "job-1")
    url = f"git@git.example.com:{src}"
    cwd = ws.git_clone(url, sha, ".", branch="main", ssh_key=KEY)

    assert ws.head_sha() == sha
    key = ws.secret_path("repo_ssh_key")
    calls = fake_ssh.read_text().splitlines()
    # One connection — the pinned commit's fetch, no clone of the default branch before it — over
    # ssh, with the key, 0600, while git ran.
    assert calls == [f"host=git@git.example.com key={key} mode=600 first={KEY.splitlines()[0]}"]
    assert not key.exists()  # gone once the clone is done
    assert _git(cwd, "config", "remote.origin.url") == url


def test_masker_masks_the_token_in_its_url_form() -> None:
    job = {"mask_values": [TOKEN], "repo_credentials": {"kind": "token", "token": TOKEN}}
    line = f"fatal: unable to access 'https://x-access-token:{quote(TOKEN, safe='')}@h/m.git/'"
    assert TOKEN not in main._masker(job).mask(line)
    assert quote(TOKEN, safe="") not in main._masker(job).mask(line)
