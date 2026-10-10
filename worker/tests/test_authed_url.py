"""The repo token is percent-encoded into the clone URL userinfo, and a failed clone never reports
it in any form (invariant 3). Mirrors the API cases in api/tests/test_repo_safety.py."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import pytest

from agent import workspace
from agent.workspace import Workspace, _authed_url

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
    authed = urlsplit(_authed_url(repo_url, token))
    # Same address git would dial without a token: nothing leaked out of the userinfo.
    for part in ("scheme", "hostname", "port", "path", "query", "fragment"):
        assert getattr(authed, part) == getattr(orig, part), part
    assert authed.username == "x-access-token"
    assert authed.password is not None and unquote(authed.password) == token


@pytest.mark.parametrize(
    ("repo_url", "token"),
    [
        ("file:///stackd-dev/repos/demo-app", None),
        ("file:///stackd-dev/repos/demo-app", "tok@en"),  # not HTTP(S)
        ("git@github.com:acme/infra.git", "tok@en"),  # SSH: a deploy key, never a token
        ("https://git.example.com/org/repo.git", None),
    ],
)
def test_authed_url_leaves_other_urls_unchanged(repo_url: str, token: str | None) -> None:
    assert _authed_url(repo_url, token) == repo_url


@pytest.mark.parametrize("token", _SPECIAL_TOKENS)
def test_clone_error_masks_both_token_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str
) -> None:
    """git fails and echoes the token in both forms it holds: inside the authed URL it was given
    (percent-encoded) and decoded from it (raw)."""
    urls: list[str] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        url = next(a for a in argv if "://" in a)
        urls.append(url)
        raw = unquote(urlsplit(url).password or "")
        err = f"fatal: unable to access '{url}/': 403 for {raw}"
        return subprocess.CompletedProcess(argv, 128, "", err)

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        Workspace(str(tmp_path), "job").git_clone(
            "https://git.example.com/org/repo.git", None, ".", token=token
        )
    [url] = urls
    assert unquote(urlsplit(url).password or "") == token  # git really got the token
    # Exact match: no form of the token survives, not even the tail of a partially masked one.
    assert str(exc.value) == (
        "git clone failed: fatal: unable to access "
        "'https://x-access-token:•••@git.example.com/org/repo.git/': 403 for •••"
    )


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_real_git_dials_the_repo_host_not_one_in_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real git, loopback only: with a raw token, git would dial 127.0.0.2:1 (from the token)."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)  # git_clone inherits os.environ: dial directly
    token, port = "x@127.0.0.2:1/#?%", _closed_port()

    with pytest.raises(RuntimeError) as exc:
        Workspace(str(tmp_path), "job").git_clone(
            f"https://127.0.0.1:{port}/org/repo.git", None, ".", token=token
        )
    detail = str(exc.value)
    assert f"127.0.0.1 port {port}" in detail, detail  # curl's connection-refused message
    assert "127.0.0.2" not in detail
    assert token not in detail and quote(token, safe="") not in detail
