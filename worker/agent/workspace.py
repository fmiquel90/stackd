from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

from agent.cancel import JobCanceled
from agent.cancel import current as current_cancel
from agent.runner import LOCAL

# A git ref/SHA must start alphanumeric (never `-`, so it can't be read as a git option) and stay
# within a safe charset — blocks option/argument injection into git fetch/checkout. The API
# validates the same shape; this is the worker-side defence-in-depth.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
# A full commit SHA (SHA-1, or SHA-256 object format): what a pinned run must check out exactly.
_FULL_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# The run's variable files (§3.4), auto-loaded by tofu/terraform.
TFVARS_JSON = "stackd.auto.tfvars.json"
HCL_TFVARS = "zzz_stackd.auto.tfvars"
# The stack's deploy key (§3.1), in the job's secret area: outside the checkout.
_SSH_KEY_FILE = "repo_ssh_key"
# ssh:// / git@ remotes: never prompt, and trust a host key on first contact (TOFU) so a fresh
# worker — or a fresh runner container — reaches the host without a pre-seeded known_hosts. A host
# that doesn't answer within 30 s, or goes silent for a minute mid-transfer, fails the command
# instead of holding it until its timeout.
_SSH_OPTIONS = (
    "-o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    " -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
)
# The http(s) counterpart: a transfer under 1 KB/s for 60 s is abandoned.
_LOW_SPEED = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60"]
# Each git command's own cap (`STACKD_GIT_TIMEOUT_SECONDS`): past it git is killed, the job fails.
_GIT_TIMEOUT_SECONDS = 10 * 60
# What a server that serves no unadvertised commit by its SHA answers (protocol v0 without
# uploadpack.allowReachableSHA1InWant). "not our ref" is also any server's answer for a SHA it
# doesn't have: the fallback then finds no such commit and reports the fetch's own error.
_SHA_WANT_REFUSED = ("does not allow request for unadvertised object", "not our ref")


def ssh_command(key: Path | None = None) -> str:
    """GIT_SSH_COMMAND: with the stack's deploy key, that key only (`IdentitiesOnly` — never an
    ssh-agent's or ~/.ssh's)."""
    if key is None:
        return f"ssh {_SSH_OPTIONS}"
    return f"ssh -i {shlex.quote(str(key))} -o IdentitiesOnly=yes {_SSH_OPTIONS}"


def module_git_config(repo_url: str, token: str) -> dict[str, str]:
    """git config, as GIT_CONFIG_COUNT/KEY_n/VALUE_n env vars (git >= 2.31), that sends the repo
    token with every URL on the stack repo's own host — its `git@host:` and `ssh://git@host/`
    forms rewritten to HTTPS too (on an https repo): what lets `init` download the private modules
    hosted next to the repo (§8.3). Scoped to that scheme and host[:port]; any other host is left
    alone. Nothing reaches the disk: git rewrites at connection time and records the URL as written,
    so `.terraform/modules` keeps no token. {} for a repo that is not on http(s)."""
    parts = urlsplit(repo_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {}
    base = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}/"
    authed = _authed_url(base, token)
    sources = [base]
    if parts.scheme == "https":
        sources += [f"git@{parts.hostname}:", f"ssh://git@{parts.hostname}/"]
    env = {"GIT_CONFIG_COUNT": str(len(sources))}
    for i, source in enumerate(sources):
        env[f"GIT_CONFIG_KEY_{i}"] = f"url.{authed}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{i}"] = source
    return env


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


def _write_new(path: Path, data: bytes, owner: tuple[int, int] | None = None) -> Path:
    """Write `data` to `path` as a new 0600 file: whatever is there (a file, or a symlink pointing
    out of the workspace) is replaced, never written through. `owner`: the (uid, gid) the workspace
    was handed over to (`Workspace.hand_over`), if any."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    if owner is not None:
        os.fchown(fd, *owner)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    """Never follows a symlink: a link in the checkout can't redirect the chown out of it."""
    os.chown(root, uid, gid, follow_symlinks=False)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)


def _git_timeout() -> int:
    return int(os.environ.get("STACKD_GIT_TIMEOUT_SECONDS") or _GIT_TIMEOUT_SECONDS)


def _run_git(args: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run one git command as the `local` runner runs a command — its own process group, killed
    past `STACKD_GIT_TIMEOUT_SECONDS`, stopped by the job's cancel token (§4.1) — so a stalled Git
    host can't hold the job. Returns (exit code, its stdout and stderr merged)."""
    token = current_cancel()
    if token is not None and token.should_stop:
        raise JobCanceled
    timeout = _git_timeout()
    lines: list[str] = []
    rc, timed_out = LOCAL.stream(["git", *args], cwd, env, timeout=timeout, on_line=lines.append)
    if token is not None and token.interrupted and token.should_stop and rc != 0:
        raise JobCanceled
    if timed_out:
        return 124, f"timed out after {timeout}s"
    return rc, "\n".join(lines).strip()


class Workspace:
    """Ephemeral per-job working directory (SPECS §7.4)."""

    def __init__(self, root: str, job_id: str) -> None:
        self.path = Path(root) / job_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.path.chmod(0o700)  # workspace holds secrets/tfvars — not world-readable
        self.owner: tuple[int, int] | None = None

    def hand_over(self, uid: int, gid: int) -> None:
        """Give the workspace to another user (a root agent's `docker` runner, §7.4: the runner
        images' unprivileged user). The checkout is handed over once cloned, and every file the
        agent writes afterwards is created with that owner: no command needs a chown of the tree."""
        self.owner = (uid, gid)
        _chown_tree(self.path, uid, gid)

    def git_clone(
        self,
        repo_url: str,
        commit_sha: str | None,
        project_root: str,
        token: str | None = None,
        branch: str | None = None,
        ssh_key: str | None = None,
    ) -> Path:
        """Clone the repo (§7.4). `token` (repo_auth_kind=token) is injected into the HTTPS URL;
        `ssh_key` (deploy_key) is on disk only while git runs (`repo_credentials`)."""
        with self.repo_credentials(repo_url, ssh_key=ssh_key) as creds:
            git_env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": ssh_command(),
                **creds,
            }
            return self._clone(repo_url, commit_sha, project_root, token, branch, git_env)

    def _clone(
        self,
        repo_url: str,
        commit_sha: str | None,
        project_root: str,
        token: str | None,
        branch: str | None,
        git_env: dict[str, str],
    ) -> Path:
        # A source repo may be owned by another uid (e.g. CI bind mounts); the worker image trusts
        # all repos via `git config --system safe.directory '*'` (a `-c` override is ignored by git).
        if commit_sha:
            self._fetch_pinned(repo_url, commit_sha, token, git_env)
        else:
            self._clone_branch(repo_url, branch, token, git_env)
        if self.owner is not None:
            _chown_tree(self.path / "repo", *self.owner)
        # `project_root` comes from the server payload; a `..`-laden value would resolve outside the
        # clone and let terraform/secret writes escape the per-job workspace. Confine it.
        repo_root = (self.path / "repo").resolve()
        cwd = (repo_root / project_root).resolve()
        if not cwd.is_relative_to(repo_root):
            raise RuntimeError(f"project_root escapes the repository: {project_root!r}")
        return cwd

    def _clone_branch(
        self, repo_url: str, branch: str | None, token: str | None, git_env: dict[str, str]
    ) -> None:
        """No pinned commit: a shallow clone of the environment's branch head (the remote's
        default branch without one)."""
        clone_url = _authed_url(repo_url, token)
        clone = [*_LOW_SPEED, "clone", "--depth", "1"]
        if branch:
            if not _SAFE_REF.match(branch):
                raise RuntimeError(f"invalid branch: {branch!r}")
            clone += ["--branch", branch]
        rc, out = _run_git([*clone, clone_url, str(self.path / "repo")], self.path, git_env)
        if rc != 0:
            # The error is reported via job_failed (not the log masker) — redact the token here so a
            # failed authenticated clone can never leak it, in either form.
            raise RuntimeError(f"git clone failed: {_redact(out, token)}")
        if clone_url != repo_url:
            # git saved the authed URL as origin's in .git/config, where any repo hook could read
            # the token (§8.3): point origin back at the token-free URL.
            rc, out = _run_git(
                ["remote", "set-url", "origin", repo_url], self.path / "repo", git_env
            )
            if rc != 0:
                raise RuntimeError(f"git remote set-url failed: {_redact(out, token)}")

    def _fetch_pinned(
        self, repo_url: str, commit_sha: str, token: str | None, git_env: dict[str, str]
    ) -> None:
        """Exactly the pinned commit (a full SHA, or a ref), downloaded once: an empty repository
        whose `origin` is the token-free URL, a shallow fetch of that commit alone from the authed
        URL (so no token is written under .git/, §8.3), and a detached checkout of what was fetched
        (FETCH_HEAD — a fetched branch or tag gets no local ref to check out by name)."""
        if not _SAFE_REF.match(commit_sha):
            raise RuntimeError(f"invalid commit ref: {commit_sha!r}")
        repo = self.path / "repo"
        fetch_url = _authed_url(repo_url, token)
        full_sha = bool(_FULL_SHA.match(commit_sha.lower()))

        def init(object_format: str) -> None:
            shutil.rmtree(repo, ignore_errors=True)
            rc, out = _run_git(
                ["init", "-q", f"--object-format={object_format}", str(repo)], self.path, git_env
            )
            if rc == 0:
                rc, out = _run_git(["remote", "add", "origin", repo_url], repo, git_env)
            if rc != 0:
                raise RuntimeError(f"git init failed: {_redact(out, token)}")

        def fetch(*refspecs: str, shallow: bool = True) -> tuple[int, str]:
            depth = ["--depth", "1"] if shallow else []
            args = [*_LOW_SPEED, "fetch", "--no-tags", *depth, fetch_url, *refspecs]
            return _run_git(args, repo, git_env)

        # `clone` reads the repository's object format from the server; `init` must be told.
        object_format = "sha256" if full_sha and len(commit_sha) == 64 else "sha1"
        init(object_format)
        rc, out = fetch(commit_sha)
        if rc != 0 and "mismatched algorithms" in out and object_format == "sha1":
            init("sha256")  # a SHA-256 repository, pinned by a ref
            rc, out = fetch(commit_sha)
        target = "FETCH_HEAD"
        if rc != 0 and full_sha and any(refusal in out for refusal in _SHA_WANT_REFUSED):
            # This server serves no commit by its SHA: fetch its branches and tags in full, and
            # check the commit out if it is among them.
            heads, tags = "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*"
            if fetch(heads, tags, shallow=False)[0] == 0:
                found = _run_git(["cat-file", "-e", f"{commit_sha}^{{commit}}"], repo, git_env)
                if found[0] == 0:
                    rc, target = 0, commit_sha
        if rc != 0:
            raise RuntimeError(f"git fetch {commit_sha} failed: {_redact(out, token)}")
        # Checkout MUST succeed: anything else would plan/apply the wrong commit.
        rc, out = _run_git(["checkout", "-q", "--detach", target], repo, git_env)
        if rc != 0:
            raise RuntimeError(f"git checkout {commit_sha} failed: {_redact(out, token)}")
        head = self.head_sha()
        if full_sha and head != commit_sha.lower():
            raise RuntimeError(f"checked out {head}, expected the pinned commit {commit_sha}")

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
        _write_new(cwd / TFVARS_JSON, json.dumps(tfvars).encode(), self.owner)

    def write_hcl_tfvars(self, cwd: Path, hcl_tfvars: dict) -> None:
        # `hcl` vars are written verbatim (`name = <raw value>`) so real HCL syntax ({ a = "b" },
        # function calls, expressions) parses natively. They are excluded from the JSON tfvars
        # (§3.4) so a var is never defined twice. `zzz_` prefix → loads last among .auto.tfvars.
        if not hcl_tfvars:
            return
        body = "".join(f"{name} = {value}\n" for name, value in hcl_tfvars.items())
        _write_new(cwd / HCL_TFVARS, body.encode(), self.owner)

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
            for name in (TFVARS_JSON, HCL_TFVARS):
                (cwd / name).unlink(missing_ok=True)

    def var_file_problem(self, cwd: Path, files: list[str]) -> str | None:
        """Why the env's repo var files (§3.4) can't be passed to tofu, or None: each must resolve,
        symlinks followed, to a regular file inside the checkout — the API's check is lexical, and
        a file the repo symlinked out of it (the workspace's token file, the host's) is refused."""
        repo_root = (self.path / "repo").resolve()
        for name in files:
            path = (cwd / name).resolve()
            if not path.is_relative_to(repo_root):
                return f"var file escapes the repository: {name!r}"
            if not path.is_file():
                return f"var file not found: {name!r}"
        return None

    def write_backend_override(self, cwd: Path) -> None:
        # Only for managed_state envs (§11.3): the repo declares no backend; we inject http. Written
        # like the tfvars: a symlink the repo planted at that name is replaced, never written
        # through (§8.3) — it could point anywhere the agent can write.
        _write_new(
            cwd / "zzz_stackd_backend.tf", b'terraform {\n  backend "http" {}\n}\n', self.owner
        )

    @contextmanager
    def repo_credentials(
        self, repo_url: str, *, token: str | None = None, ssh_key: str | None = None
    ) -> Iterator[dict[str, str]]:
        """The git env that carries the stack's repo credential (§8.3), for the clone and for
        `init`'s module downloads only — never for a hook: the token as host-scoped git config
        (`module_git_config`), or the deploy key as a 0600 file in the secret area (outside the
        checkout, written fresh, removed on exit) named by GIT_SSH_COMMAND. {} without one."""
        env: dict[str, str] = {}
        if token:
            env = {"GIT_TERMINAL_PROMPT": "0", **module_git_config(repo_url, token)}
        if not ssh_key:
            yield env
            return
        key = ssh_key if ssh_key.endswith("\n") else ssh_key + "\n"  # ssh refuses it without
        with self.secret_file(_SSH_KEY_FILE, key) as path:
            yield {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": ssh_command(path)}

    def secret_path(self, name: str) -> Path:
        """Where `secret_file` puts `name`: in the job's workspace, outside the checkout."""
        return self.path.resolve() / name

    @contextmanager
    def secret_file(self, name: str, content: str) -> Iterator[Path]:
        """A secret file (the OIDC token §10.4, the deploy key §8.3) present only while what it
        wraps runs — never while a repo hook does (§8.3). 0600, written fresh (a file or symlink a
        hook left at its path is replaced, never written through), removed right after."""
        path = self.secret_path(name)
        try:
            yield _write_new(path, content.encode(), self.owner)
        finally:
            path.unlink(missing_ok=True)

    def write_plan(self, cwd: Path, data: bytes) -> Path:
        """Write the saved plan the apply job applies (§7.4): 0600 (it holds the run's variable
        values in clear), and as a new file — whatever the checkout has at that path (a file, or a
        symlink pointing out of the workspace) is replaced, never written through."""
        return _write_new(cwd / "plan.tfplan", data, self.owner)

    def write_plan_json(self, cwd: Path, doc: str) -> Path:
        """The plan JSON the after_plan hooks read (§8.3), written like `write_plan`: a symlink a
        repo planted at `plan.json` can't redirect it out of the tree."""
        return _write_new(cwd / "plan.json", doc.encode(), self.owner)

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
