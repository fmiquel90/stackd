from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

# A git ref/SHA must start alphanumeric (never `-`, so it can't be read as a git option) and stay
# within a safe charset — blocks option/argument injection into git fetch/checkout. The API
# validates the same shape; this is the worker-side defence-in-depth.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
# A full commit SHA (SHA-1, or SHA-256 object format): what a pinned run must check out exactly.
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# The run's variable files (§3.4), auto-loaded by tofu/terraform.
_TFVARS_JSON = "stackd.auto.tfvars.json"
_HCL_TFVARS = "zzz_stackd.auto.tfvars"


def _authed_url(repo_url: str, token: str | None) -> str:
    """Inject an HTTPS token credential the way the API does (x-access-token@). SSH / no-token URLs
    pass through unchanged. Mirrors app.stacks.git._authed_url so the worker clones what the API
    validated: each userinfo component is percent-encoded (a raw `@ : / ? # %` in the token would
    end the userinfo early, so git would contact the wrong host or carry part of the token in the
    host or path), and the host[:port] is kept verbatim (any userinfo already in `repo_url` is
    replaced), so IPv6 brackets and the port survive."""
    if token and repo_url.startswith("http"):
        parts = urlsplit(repo_url)
        userinfo = ":".join(quote(p, safe="") for p in ("x-access-token", token))
        netloc = f"{userinfo}@{parts.netloc.rpartition('@')[2]}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return repo_url


def _redact(text: str, token: str | None) -> str:
    """Mask the repo token in what git reports: the authed URL (percent-encoded form) can appear
    verbatim in git's errors, the raw form anywhere else. Longest form first: the raw token can be
    a substring of its encoded form (a literal `%25` in it), and masking that first would leave the
    rest of the encoded token behind. Mirrors app.stacks.git._redact."""
    if token:
        for form in sorted({token, quote(token, safe="")}, key=len, reverse=True):
            text = text.replace(form, "•••")
    return text


def _write_new(path: Path, data: bytes) -> Path:
    """Write `data` to `path` as a new 0600 file: whatever is there (a file, or a symlink pointing
    out of the workspace) is replaced, never written through."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class Workspace:
    """Ephemeral per-job working directory (SPECS §7.4)."""

    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)  # workspace holds secrets/tfvars — not world-readable

    def git_clone(
        self,
        repo_url: str,
        commit_sha: str | None,
        project_root: str,
        token: str | None = None,
        branch: str | None = None,
    ) -> Path:
        # A source repo may be owned by another uid (e.g. CI bind mounts); the worker image trusts
        # all repos via `git config --system safe.directory '*'` (a `-c` override is ignored by git).
        # `token` (repo_auth_kind=token) is injected into the HTTPS URL so private repos can clone.
        clone_url = _authed_url(repo_url, token)
        git_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            # ssh:// / git@ repos: never prompt, and trust a host key on first contact (TOFU)
            # so a fresh worker can clone without a pre-seeded known_hosts.
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        }
        clone = ["git", "clone", "--depth", "1"]
        if branch and not commit_sha:
            # No pinned commit: the head of the environment's branch, not the remote's default.
            if not _SAFE_REF.match(branch):
                raise RuntimeError(f"invalid branch: {branch!r}")
            clone += ["--branch", branch]
        # Surface git's stderr so a clone failure isn't an opaque "exit status 128".
        result = subprocess.run(
            [*clone, clone_url, str(self.path / "repo")],
            capture_output=True,
            text=True,
            env=git_env,
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            # The error is reported via job_failed (not the log masker) — redact the token here so a
            # failed authenticated clone can never leak it, in either form.
            msg = _redact(msg, token)
            raise RuntimeError(f"git clone failed: {msg}")
        if clone_url != repo_url:
            # git saved the authed URL as origin's in .git/config, where any repo hook could read
            # the token (§8.3): point origin back at the token-free URL. The pinned fetch below is
            # handed the authed URL itself.
            reset = subprocess.run(
                ["git", "remote", "set-url", "origin", repo_url],
                cwd=self.path / "repo",
                capture_output=True,
                text=True,
            )
            if reset.returncode != 0:
                msg = _redact(reset.stderr.strip() or reset.stdout.strip(), token)
                raise RuntimeError(f"git remote set-url failed: {msg}")
        if commit_sha:
            if not _SAFE_REF.match(commit_sha):
                raise RuntimeError(f"invalid commit ref: {commit_sha!r}")
            # The shallow clone holds only the default branch's tip: fetch exactly the pinned
            # commit (a full SHA, or a ref), then check out what was fetched (FETCH_HEAD — a
            # fetched branch or tag gets no local ref to check out by name).
            fetch = subprocess.run(
                ["git", "fetch", "--depth", "1", clone_url, commit_sha],
                cwd=self.path / "repo",
                capture_output=True,
                text=True,
                env=git_env,
            )
            if fetch.returncode != 0:
                msg = _redact(fetch.stderr.strip() or fetch.stdout.strip(), token)
                raise RuntimeError(f"git fetch {commit_sha} failed: {msg}")
            # Checkout MUST succeed: silently staying on the default-branch HEAD would plan/apply
            # the wrong commit. Fail loudly with git's stderr.
            checkout = subprocess.run(
                ["git", "checkout", "--detach", "FETCH_HEAD"],
                cwd=self.path / "repo",
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                raise RuntimeError(
                    f"git checkout {commit_sha} failed: "
                    f"{checkout.stderr.strip() or checkout.stdout.strip()}"
                )
            head = self.head_sha()
            if _FULL_SHA.match(commit_sha.lower()) and head != commit_sha.lower():
                raise RuntimeError(f"checked out {head}, expected the pinned commit {commit_sha}")
        # `project_root` comes from the server payload; a `..`-laden value would resolve outside the
        # clone and let terraform/secret writes escape the per-job workspace. Confine it.
        repo_root = (self.path / "repo").resolve()
        cwd = (repo_root / project_root).resolve()
        if not cwd.is_relative_to(repo_root):
            raise RuntimeError(f"project_root escapes the repository: {project_root!r}")
        return cwd

    def head_sha(self) -> str:
        """The full SHA of the checked-out commit. A plan job reports it so the API pins the run to
        it and the apply job clones exactly that commit (§7.2)."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.path / "repo", capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def write_tfvars(self, cwd: Path, tfvars: dict) -> None:
        # JSON tfvars are always valid HCL2 inputs; auto-loaded by terraform/tofu.
        _write_new(cwd / _TFVARS_JSON, json.dumps(tfvars).encode())

    def write_hcl_tfvars(self, cwd: Path, hcl_tfvars: dict) -> None:
        # `hcl` vars are written verbatim (`name = <raw value>`) so real HCL syntax ({ a = "b" },
        # function calls, expressions) parses natively. They are excluded from the JSON tfvars
        # (§3.4) so a var is never defined twice. `zzz_` prefix → loads last among .auto.tfvars.
        if not hcl_tfvars:
            return
        body = "".join(f"{name} = {value}\n" for name, value in hcl_tfvars.items())
        _write_new(cwd / _HCL_TFVARS, body.encode())

    @contextmanager
    def tfvars(
        self, cwd: Path, tfvars: dict[str, object], hcl_tfvars: dict[str, str]
    ) -> Iterator[None]:
        """The run's variable files, in the checkout only while one tofu command runs (§8.3): tofu
        auto-loads them as before (same precedence among the repo's own .auto.tfvars, and every
        subcommand reads them), and they are gone before any hook runs."""
        try:
            self.write_tfvars(cwd, tfvars)
            self.write_hcl_tfvars(cwd, hcl_tfvars)
            yield
        finally:
            for name in (_TFVARS_JSON, _HCL_TFVARS):
                (cwd / name).unlink(missing_ok=True)

    def write_backend_override(self, cwd: Path) -> None:
        # Only for managed_state envs (§11.3): the repo declares no backend; we inject http.
        (cwd / "zzz_stackd_backend.tf").write_text('terraform {\n  backend "http" {}\n}\n')

    def secret_path(self, name: str) -> Path:
        """Where `secret_file` puts `name`: in the job's workspace, outside the checkout."""
        return self.path.resolve() / name

    @contextmanager
    def secret_file(self, name: str, content: str) -> Iterator[Path]:
        """A secret file (the OIDC token, §10.4) present only while the command it wraps runs —
        never while a repo hook does (§8.3). 0600, written fresh (a file or symlink a hook left at
        its path is replaced, never written through), removed right after."""
        path = self.secret_path(name)
        try:
            yield _write_new(path, content.encode())
        finally:
            path.unlink(missing_ok=True)

    def write_plan(self, cwd: Path, data: bytes) -> Path:
        """Write the saved plan the apply job applies (§7.4): 0600 (it holds the run's variable
        values in clear), and as a new file — whatever the checkout has at that path (a file, or a
        symlink pointing out of the workspace) is replaced, never written through."""
        return _write_new(cwd / "plan.tfplan", data)

    def write_plan_json(self, cwd: Path, doc: str) -> Path:
        """The plan JSON the after_plan hooks read (§8.3), written like `write_plan`: a symlink a
        repo planted at `plan.json` can't redirect it out of the tree."""
        return _write_new(cwd / "plan.json", doc.encode())

    def private_path(self, name: str) -> Path:
        """A path in the job's workspace but outside the checkout, for a file no repo hook may find
        in the tree it runs in (the plan job's saved plan, §8.3). Whatever is there is removed
        first, so a file or symlink a hook left there is never written through."""
        path = self.secret_path(name)
        path.unlink(missing_ok=True)
        return path

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
