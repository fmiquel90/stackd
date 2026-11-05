import json
from pathlib import Path

from agent.workspace import Workspace, _authed_url


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
