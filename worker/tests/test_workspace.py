import json
import os
import subprocess
from pathlib import Path

import pytest

from agent.workspace import Workspace, _authed_url


def test_a_handed_over_workspace_is_chowned_once_then_file_by_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root agent's docker runner (§7.4): the tree is handed to the runner user once, after the
    clone; each file the agent writes later is created with that owner — no tree walk per command."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tf").write_text("")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run([*git, "add", "-A"], cwd=src, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "c1"], cwd=src, check=True)
    chowned: list[str] = []
    fchowned: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, **kw: chowned.append(str(path)))
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: fchowned.append((uid, gid)))

    ws = Workspace(str(tmp_path / "ws"), "job")
    ws.hand_over(10001, 10001)
    assert chowned == [str(ws.path)]
    cwd = ws.git_clone(f"file://{src}", None, ".")
    assert str(cwd / "main.tf") in chowned and str(cwd / ".git" / "HEAD") in chowned
    chowned.clear()

    with ws.tfvars(cwd, {"a": 1}, {"b": "2"}), ws.secret_file("oidc_token", "tok"):
        ws.write_backend_override(cwd)
        ws.write_plan(cwd, b"plan")
        ws.write_plan_json(cwd, "{}")
    assert fchowned == [(10001, 10001)] * 6 and chowned == []


def test_a_workspace_not_handed_over_chowns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fchown", lambda *a: pytest.fail("chowned"))
    ws = Workspace(str(tmp_path), "job")
    cwd = tmp_path / "wd"
    cwd.mkdir()
    ws.write_tfvars(cwd, {"a": 1})


def test_write_hcl_tfvars_verbatim(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "job1")
    cwd = tmp_path / "wd"
    cwd.mkdir()
    ws.write_tfvars(cwd, {"region": "eu-west-1"})
    ws.write_hcl_tfvars(cwd, {"tags": '{ team = "infra" }', "azs": '["a", "b"]'})
    hcl = (cwd / "zzz_stackd.auto.tfvars").read_text()
    assert 'tags = { team = "infra" }' in hcl
    assert 'azs = ["a", "b"]' in hcl
    # hcl vars are excluded from the JSON tfvars (no double definition).
    j = json.loads((cwd / "stackd.auto.tfvars.json").read_text())
    assert j == {"region": "eu-west-1"}


def test_write_hcl_tfvars_skips_when_empty(tmp_path: Path) -> None:
    ws = Workspace(str(tmp_path), "job2")
    cwd = tmp_path / "wd"
    cwd.mkdir()
    ws.write_hcl_tfvars(cwd, {})
    assert not (cwd / "zzz_stackd.auto.tfvars").exists()


def test_backend_override_never_written_through_a_symlink(tmp_path: Path) -> None:
    # A repo can commit `zzz_stackd_backend.tf` as a symlink pointing out of the checkout (§8.3):
    # the override replaces it, it never writes to the symlink's target.
    ws = Workspace(str(tmp_path), "job3")
    cwd = tmp_path / "wd"
    cwd.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (cwd / "zzz_stackd_backend.tf").symlink_to(outside)
    ws.write_backend_override(cwd)
    assert outside.read_text() == "keep"
    path = cwd / "zzz_stackd_backend.tf"
    assert not path.is_symlink()
    assert path.read_text() == 'terraform {\n  backend "http" {}\n}\n'


def test_authed_url_injects_https_token():
    assert (
        _authed_url("https://github.com/acme/infra.git", "ghp_secret")
        == "https://x-access-token:ghp_secret@github.com/acme/infra.git"
    )


def test_authed_url_preserves_port_and_path():
    assert (
        _authed_url("https://git.example.com:8443/acme/infra", "tok")
        == "https://x-access-token:tok@git.example.com:8443/acme/infra"
    )


def test_authed_url_passthrough_without_token():
    url = "https://github.com/acme/infra.git"
    assert _authed_url(url, None) == url


def test_authed_url_passthrough_for_ssh_and_file():
    # Token only applies to http(s); SSH uses a deploy key, file:// fixtures need nothing.
    assert _authed_url("git@github.com:acme/infra.git", "tok") == "git@github.com:acme/infra.git"
    assert _authed_url("file:///srv/fixtures/infra", "tok") == "file:///srv/fixtures/infra"
