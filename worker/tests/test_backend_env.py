"""The managed HTTP backend's settings reach tofu through TF_HTTP_* only (SPECS §11.2): never the
init command line (tofu persists -backend-config values, the state token included, under
.terraform/), and never a hook's environment (§8.3)."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent.main import _init_cmd, _repo_environ, _tofu_env

TOKEN = "state-token-abc123"
BACKEND = {
    "type": "http",
    "address": "http://api/state/v1/env",
    "lock_address": "http://api/state/v1/env/lock",
    "unlock_address": "http://api/state/v1/env/lock",
    "lock_method": "LOCK",
    "unlock_method": "UNLOCK",
    "username": "env",
    "password": TOKEN,
}


def test_managed_init_command_carries_no_backend_settings() -> None:
    cmd = _init_cmd("tofu", BACKEND)
    assert cmd == ["tofu", "init", "-input=false"]
    assert not any(TOKEN in arg for arg in cmd)


def test_tofu_env_carries_the_managed_backend() -> None:
    env = _tofu_env({"PATH": "/bin"}, BACKEND)
    assert env == {
        "PATH": "/bin",
        "TF_HTTP_ADDRESS": "http://api/state/v1/env",
        "TF_HTTP_LOCK_ADDRESS": "http://api/state/v1/env/lock",
        "TF_HTTP_UNLOCK_ADDRESS": "http://api/state/v1/env/lock",
        "TF_HTTP_LOCK_METHOD": "LOCK",
        "TF_HTTP_UNLOCK_METHOD": "UNLOCK",
        "TF_HTTP_USERNAME": "env",
        "TF_HTTP_PASSWORD": TOKEN,
    }


def test_tofu_env_read_only_backend_never_locks() -> None:
    # A `ro` job's backend has no lock address (§11.2): tofu must then skip locking.
    ro = {k: v for k, v in BACKEND.items() if not k.startswith(("lock", "unlock"))}
    env = _tofu_env({}, ro)
    assert "TF_HTTP_LOCK_ADDRESS" not in env and "TF_HTTP_UNLOCK_ADDRESS" not in env


def test_tofu_env_drops_inherited_backend_settings_for_managed_state() -> None:
    # Neither the worker's env nor a run variable can redirect or weaken the managed backend.
    base = {
        "TF_HTTP_ADDRESS": "http://evil/state",
        "TF_HTTP_LOCK_ADDRESS": "http://evil/lock",
        "TF_HTTP_UPDATE_METHOD": "PUT",
        "TF_HTTP_SKIP_CERT_VERIFICATION": "true",
    }
    ro = {"address": "http://api/state/v1/env", "username": "env", "password": TOKEN}
    env = _tofu_env(base, ro)
    assert env == {
        "TF_HTTP_ADDRESS": "http://api/state/v1/env",
        "TF_HTTP_USERNAME": "env",
        "TF_HTTP_PASSWORD": TOKEN,
    }


def test_tofu_env_unmanaged_is_unchanged() -> None:
    # The repo's own http backend may legitimately be configured through TF_HTTP_* (e.g. GitLab).
    base = {"TF_HTTP_ADDRESS": "https://gitlab/state", "TF_HTTP_PASSWORD": "glpat"}
    assert _tofu_env(base, None) == base


def test_repo_environ_strips_state_backend_settings(monkeypatch) -> None:
    monkeypatch.setenv("TF_HTTP_PASSWORD", TOKEN)
    monkeypatch.setenv("TF_HTTP_ADDRESS", "http://api/state/v1/env")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = _repo_environ()
    assert not any(k.startswith("TF_HTTP_") for k in env)
    assert env.get("PATH") == "/usr/bin:/bin"


# --- through the plan / command handlers: tofu gets the token, hooks never do -----------------


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lines: list[str] = []

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, *args: Any) -> None:
        pass

    def download_artifact(self, *args: Any) -> bytes:
        return b"PK-plan"


_HOOK = 'echo "HOOK {name} PW=[$TF_HTTP_PASSWORD] ADDR=[$TF_HTTP_ADDRESS]"'
_STAGES = ("before_init", "after_init", "before_plan", "after_plan", "before_apply", "after_apply")


class _FakeWorkspace:
    """Clone faked; the repo declares a hook at every plan stage (they run for real, `sh -c`)."""

    def __init__(self, root: str, job_id: str) -> None:
        self.root = Path(root)

    def git_clone(self, *args: Any, **kw: Any) -> Path:
        return self.root

    def head_sha(self) -> str:
        return "a" * 40

    def tfvars(self, *args: Any) -> AbstractContextManager[None]:
        return nullcontext()

    def write_backend_override(self, *args: Any) -> None:
        pass

    def write_plan(self, cwd: Path, data: bytes) -> Path:
        (cwd / "plan.tfplan").write_bytes(data)
        return cwd / "plan.tfplan"

    def private_path(self, name: str) -> Path:
        return self.root / name

    def write_plan_json(self, cwd: Path, doc: str) -> Path:
        (cwd / "plan.json").write_text(doc)
        return cwd / "plan.json"

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {
            s: [{"name": f"repo-{s}", "command": _HOOK.format(name=s), "source": "repo"}]
            for s in _STAGES
        }

    def cleanup(self) -> None:
        pass


def _job(phase: str) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "cred_phase": "plan",
        "command": {"name": "state list", "args": []},
        "environment": {
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": ".",
        },
        "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "mask_values": [],  # unmasked on purpose: a leak must show up verbatim in the hook output
        "backend": BACKEND,
        # A platform hook at every plan stage too: trusted, but it needs no state credentials.
        "hooks": {
            s: [{"name": f"platform-{s}", "command": _HOOK.format(name=s), "source": "platform"}]
            for s in _STAGES
        },
    }


def _run(
    monkeypatch, tmp_path: Path, handler: str, phase: str, run_env: dict[str, str] | None = None
) -> tuple[list, _FakeClient]:
    """Run `handler` with tofu faked; returns each tofu call (argv, env) and the client. `run_env`
    is added to the run's `env` variables."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run_command(cmd: list[str], cwd: Path, env: dict[str, str], **kw: Any) -> int:
        calls.append((cmd, env))
        return 0

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict[str, str], **kw: Any) -> tuple:
        calls.append((cmd, env))
        return (2 if cmd[1] == "plan" else 0), [{"type": "change_summary", "changes": {"add": 1}}]

    def fake_subprocess_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.delenv("TF_HTTP_PASSWORD", raising=False)
    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", fake_run_command)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(main.subprocess, "run", fake_subprocess_run)
    (tmp_path / "plan.tfplan").write_bytes(b"PK-plan")
    client = _FakeClient()
    job = _job(phase)
    job["env"].update(run_env or {})
    getattr(main, handler)(
        client, job, SimpleNamespace(workspace_root=str(tmp_path), leak_action="warn")
    )
    return calls, client


def test_plan_job_gives_state_credentials_to_tofu_only(monkeypatch, tmp_path: Path) -> None:
    calls, client = _run(monkeypatch, tmp_path, "handle_plan", "plan")
    assert client.events[-1][0] == "phase_finished", client.events

    by_subcommand = {cmd[1]: env for cmd, env in calls}
    for sub in ("init", "plan"):  # the commands that reach the backend
        assert by_subcommand[sub]["TF_HTTP_PASSWORD"] == TOKEN, sub
        assert by_subcommand[sub]["TF_HTTP_ADDRESS"] == BACKEND["address"], sub
    for cmd, _env in calls:
        assert not any(TOKEN in arg for arg in cmd), cmd

    # Every hook ran — platform and repo, at every stage — and none saw the backend settings.
    hooks = [line for line in client.lines if line.startswith("HOOK ")]
    assert len(hooks) == 8, client.lines
    assert all("PW=[] ADDR=[]" in line for line in hooks), hooks
    assert TOKEN not in "\n".join(client.lines)


def test_apply_job_gives_state_credentials_to_tofu_only(monkeypatch, tmp_path: Path) -> None:
    calls, client = _run(monkeypatch, tmp_path, "handle_apply", "apply")
    assert client.events[-1][0] == "phase_finished", client.events

    by_subcommand = {cmd[1]: env for cmd, env in calls}
    for sub in ("init", "apply", "output"):  # the commands that reach the backend
        assert by_subcommand[sub]["TF_HTTP_PASSWORD"] == TOKEN, sub
    for cmd, _env in calls:
        assert not any(TOKEN in arg for arg in cmd), cmd

    hooks = [line for line in client.lines if line.startswith("HOOK ")]
    assert len(hooks) == 4, client.lines  # platform + repo, before_apply + after_apply
    assert all("PW=[] ADDR=[]" in line for line in hooks), hooks
    assert TOKEN not in "\n".join(client.lines)


def test_command_job_gives_state_credentials_to_tofu(monkeypatch, tmp_path: Path) -> None:
    calls, client = _run(monkeypatch, tmp_path, "handle_command_run", "command")
    assert client.events[-1][0] == "phase_finished", client.events
    assert [cmd[1] for cmd, _ in calls] == ["init", "state"]
    for cmd, env in calls:
        assert env["TF_HTTP_PASSWORD"] == TOKEN
        assert not any(TOKEN in arg for arg in cmd), cmd


@pytest.mark.parametrize(
    ("handler", "phase"),
    [("handle_plan", "plan"), ("handle_apply", "apply"), ("handle_command_run", "command")],
)
def test_tofu_env_carries_none_of_the_agents_own_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, handler: str, phase: str
) -> None:
    # Repo code runs inside tofu (`data "external"`, provisioners): the agent's own settings, and
    # any platform secret the worker host carries, must not reach it…
    worker_own = {
        "STACKD_POOL_TOKEN": "pool-token-1",
        "STACKD_POOL_TOKEN_FILE": "/stackd/pool.token",
        "STACKD_API_URL": "http://api:8000",
        "STACKD_ENCRYPTION_KEY": "enc-key-2",
    }
    # …while all a provider may need still does, and the run's own variables too.
    kept = {
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca.pem",
        "TF_PLUGIN_CACHE_DIR": "/cache/tofu-plugins",
        "AWS_PROFILE": "ops",
        "GOOGLE_APPLICATION_CREDENTIALS": "/secrets/sa.json",
    }
    run_env = {"APP_TEAM": "core", "STACKD_RUN_LABEL": "set by the run"}
    for name, value in {**worker_own, **kept}.items():
        monkeypatch.setenv(name, value)
    calls, client = _run(monkeypatch, tmp_path, handler, phase, run_env)
    assert client.events[-1][0] == "phase_finished", client.events

    assert len(calls) >= 2  # init, then the job's own tofu commands
    for cmd, env in calls:
        assert not set(worker_own) & set(env), (cmd, sorted(env))
        assert {k: env.get(k) for k in {**kept, **run_env}} == {**kept, **run_env}, cmd
        assert env["PATH"] and env["TF_HTTP_PASSWORD"] == TOKEN, cmd
