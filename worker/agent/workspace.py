from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml


class Workspace:
    """Ephemeral per-job working directory (SPECS §7.4)."""

    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        self.path.mkdir(parents=True, exist_ok=True)

    def git_clone(self, repo_url: str, commit_sha: str | None, project_root: str) -> Path:
        # A source repo may be owned by another uid (e.g. CI bind mounts); the worker image trusts
        # all repos via `git config --system safe.directory '*'` (a `-c` override is ignored by git).
        # Surface git's stderr so a clone failure isn't an opaque "exit status 128".
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(self.path / "repo")],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        if commit_sha:
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", commit_sha],
                cwd=self.path / "repo",
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", commit_sha], cwd=self.path / "repo", capture_output=True
            )
        return (self.path / "repo" / project_root).resolve()

    def write_tfvars(self, cwd: Path, tfvars: dict) -> None:
        # JSON tfvars are always valid HCL2 inputs; auto-loaded by terraform/tofu.
        (cwd / "stackd.auto.tfvars.json").write_text(json.dumps(tfvars))

    def write_backend_override(self, cwd: Path) -> None:
        # Only for managed_state envs (§11.3): the repo declares no backend; we inject http.
        (cwd / "zzz_stackd_backend.tf").write_text('terraform {\n  backend "http" {}\n}\n')

    def write_secret(self, name: str, content: str) -> str:
        path = self.path / name
        path.write_text(content)
        path.chmod(0o600)
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
