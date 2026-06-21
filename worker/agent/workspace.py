from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

# A git ref/SHA must start alphanumeric (never `-`, so it can't be read as a git option) and stay
# within a safe charset — blocks option/argument injection into git fetch/checkout. The API
# validates the same shape; this is the worker-side defence-in-depth.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


def _authed_url(repo_url: str, token: str | None) -> str:
    """Inject an HTTPS token credential the way the API does (x-access-token@). SSH / no-token URLs
    pass through unchanged. Mirrors app.stacks.git._authed_url so the worker clones what the API
    validated."""
    if token and repo_url.startswith("http"):
        parts = urlsplit(repo_url)
        netloc = f"x-access-token:{token}@{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return repo_url


class Workspace:
    """Ephemeral per-job working directory (SPECS §7.4)."""

    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)  # workspace holds secrets/tfvars — not world-readable

    def git_clone(
        self, repo_url: str, commit_sha: str | None, project_root: str, token: str | None = None
    ) -> Path:
        # A source repo may be owned by another uid (e.g. CI bind mounts); the worker image trusts
        # all repos via `git config --system safe.directory '*'` (a `-c` override is ignored by git).
        # `token` (repo_auth_kind=token) is injected into the HTTPS URL so private repos can clone.
        clone_url = _authed_url(repo_url, token)
        # Surface git's stderr so a clone failure isn't an opaque "exit status 128".
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(self.path / "repo")],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                # ssh:// / git@ repos: never prompt, and trust a host key on first contact (TOFU)
                # so a fresh worker can clone without a pre-seeded known_hosts.
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
            },
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            # The error is reported via job_failed (not the log masker) — redact the token here so a
            # failed authenticated clone can never leak it.
            if token:
                msg = msg.replace(token, "•••")
            raise RuntimeError(f"git clone failed: {msg}")
        if commit_sha:
            if not _SAFE_REF.match(commit_sha):
                raise RuntimeError(f"invalid commit ref: {commit_sha!r}")
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", commit_sha],
                cwd=self.path / "repo",
                capture_output=True,
            )
            # Checkout MUST succeed: silently staying on the default-branch HEAD would plan/apply
            # the wrong commit. Fail loudly with git's stderr.
            checkout = subprocess.run(
                ["git", "checkout", commit_sha],
                cwd=self.path / "repo",
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"git checkout {commit_sha} failed: "
                    f"{checkout.stderr.strip() or checkout.stdout.strip()}"
                )
        # `project_root` comes from the server payload; a `..`-laden value would resolve outside the
        # clone and let terraform/secret writes escape the per-job workspace. Confine it.
        repo_root = (self.path / "repo").resolve()
        cwd = (repo_root / project_root).resolve()
        if not cwd.is_relative_to(repo_root):
            raise RuntimeError(f"project_root escapes the repository: {project_root!r}")
        return cwd

    def write_tfvars(self, cwd: Path, tfvars: dict) -> None:
        # JSON tfvars are always valid HCL2 inputs; auto-loaded by terraform/tofu.
        (cwd / "stackd.auto.tfvars.json").write_text(json.dumps(tfvars))

    def write_hcl_tfvars(self, cwd: Path, hcl_tfvars: dict) -> None:
        # `hcl` vars are written verbatim (`name = <raw value>`) so real HCL syntax ({ a = "b" },
        # function calls, expressions) parses natively. They are excluded from the JSON tfvars
        # (§3.4) so a var is never defined twice. `zzz_` prefix → loads last among .auto.tfvars.
        if not hcl_tfvars:
            return
        body = "".join(f"{name} = {value}\n" for name, value in hcl_tfvars.items())
        (cwd / "zzz_stackd.auto.tfvars").write_text(body)

    def write_backend_override(self, cwd: Path) -> None:
        # Only for managed_state envs (§11.3): the repo declares no backend; we inject http.
        (cwd / "zzz_stackd_backend.tf").write_text('terraform {\n  backend "http" {}\n}\n')

    def write_secret(self, name: str, content: str) -> str:
        # Create with 0600 atomically — no world-readable window between write and chmod.
        path = self.path / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return str(path)

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        f = cwd / ".stackd.yml"
        if not f.exists():
            return {}
        data = yaml.safe_load(f.read_text()) or {}
        hooks = data.get("hooks", {})
        # Tag repo source so platform hooks remain distinguishable (§8).
        return {stage: [{**h, "source": "repo"} for h in items] for stage, items in hooks.items()}

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
