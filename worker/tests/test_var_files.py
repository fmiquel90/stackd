"""An environment's repo var files (SPECS §3.4): passed in order as `-var-file=` to the commands
that read variables — the plan, `import`, `refresh` — with Stackd's own files explicitly after them
so the run's resolved variables keep winning; never to init, the saved plan's apply or the other
commands. A file missing from the checkout, or escaping it, fails the job before any tofu step."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent.workspace import HCL_TFVARS, TFVARS_JSON, Workspace

PLAN = b"PK\x03\x04 plan"


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, *args: Any, **kw: Any) -> None:
        pass

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        pass

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return PLAN


class _FakeWorkspace(Workspace):
    """A real workspace whose clone is a plain directory holding the files a test puts there; the
    project root is `stacks/app` inside it."""

    files: dict[str, str] = {}

    def git_clone(self, *args: Any, **kw: Any) -> Path:
        repo = self.path / "repo"
        for name, body in self.files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(body)
        cwd = repo / "stacks" / "app"
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def head_sha(self) -> str:
        return "c" * 40

    def load_stackd_yml(self, cwd: Path) -> dict[str, list[dict]]:
        return {}


def _job(phase: str, var_files: list[str], **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "environment": {
            "name": "prod",
            "tool": "opentofu",
            "repo_url": "file:///repos/x",
            "commit_sha": "c" * 40,
            "branch": "main",
            "project_root": "stacks/app",
        },
        "var_files": var_files,
        "tfvars_json": {"region": "eu-west-3"},
        "hcl_tfvars": {},
        **extra,
    }


def _patch(monkeypatch, files: dict[str, str]) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_stream_json(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> tuple:
        calls.append(cmd)
        if cmd[1] == "plan":
            out = next(a.removeprefix("-out=") for a in cmd if a.startswith("-out="))
            Path(out).write_bytes(PLAN)
            return 2, []
        return 0, []

    _FakeWorkspace.files = files
    monkeypatch.setattr(main, "Workspace", _FakeWorkspace)
    monkeypatch.setattr(main, "run_command", lambda cmd, *a, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(main, "stream_json_command", fake_stream_json)
    monkeypatch.setattr(
        main.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "{}", "")
    )
    return calls


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(runner="local", workspace_root=str(tmp_path), leak_action="warn")


def _var_flags(cmd: list[str]) -> list[str]:
    return [a for a in cmd if a.startswith("-var-file")]


REPO_FILES = {
    "stacks/app/envs/prod.tfvars": 'size = "large"\n',
    "shared/prod.tfvars": 'owner = "core"\n',
}


@pytest.mark.parametrize(
    ("hcl", "own"),
    [({}, [TFVARS_JSON]), ({"tags": '{ team = "core" }'}, [TFVARS_JSON, HCL_TFVARS])],
)
def test_plan_gets_repo_files_in_order_then_stackds(
    monkeypatch, tmp_path: Path, hcl: dict, own: list[str]
) -> None:
    calls = _patch(monkeypatch, REPO_FILES)
    job = _job(
        "plan",
        ["envs/prod.tfvars", "../../shared/prod.tfvars"],
        hcl_tfvars=hcl,
        plan_options={"targets": ["a.b"]},
    )
    main.handle_plan(_FakeClient(), job, _settings(tmp_path))
    [plan] = [c for c in calls if c[1] == "plan"]
    assert _var_flags(plan) == [
        "-var-file=envs/prod.tfvars",
        "-var-file=../../shared/prod.tfvars",
        *(f"-var-file={f}" for f in own),
    ]
    assert "-target=a.b" in plan
    # init, show: no variables flag (init still has Stackd's auto-loaded files, as before).
    assert all(not _var_flags(c) for c in calls if c[1] != "plan")


@pytest.mark.parametrize("extra", [{"refresh_only": True}, {"destroy": True}])
def test_drift_and_destroy_plans_get_them_too(monkeypatch, tmp_path: Path, extra: dict) -> None:
    calls = _patch(monkeypatch, REPO_FILES)
    main.handle_plan(
        _FakeClient(), _job("plan", ["envs/prod.tfvars"], **extra), _settings(tmp_path)
    )
    [plan] = [c for c in calls if c[1] == "plan"]
    assert _var_flags(plan) == ["-var-file=envs/prod.tfvars", f"-var-file={TFVARS_JSON}"]


def test_no_var_files_leaves_the_plan_as_it_was(monkeypatch, tmp_path: Path) -> None:
    # Stackd's files stay auto-loaded only: the precedence among the repo's *.auto.tfvars is kept.
    calls = _patch(monkeypatch, REPO_FILES)
    main.handle_plan(_FakeClient(), _job("plan", []), _settings(tmp_path))
    assert all(not _var_flags(c) for c in calls)


def test_the_apply_of_a_saved_plan_gets_none(monkeypatch, tmp_path: Path) -> None:
    calls = _patch(monkeypatch, REPO_FILES)
    main.handle_apply(_FakeClient(), _job("apply", ["envs/prod.tfvars"]), _settings(tmp_path))
    [apply] = [c for c in calls if c[1] == "apply"]
    assert apply == ["tofu", "apply", "-input=false", "-json", "plan.tfplan"]
    assert all(not _var_flags(c) for c in calls)


@pytest.mark.parametrize(
    ("missing", "problem"),
    [
        ("envs/staging.tfvars", "var file not found: 'envs/staging.tfvars'"),
        ("../../../outside.tfvars", "var file escapes the repository: '../../../outside.tfvars'"),
        ("envs", "var file not found: 'envs'"),  # a directory is not a var file
    ],
)
def test_a_bad_var_file_fails_the_plan_before_any_tofu_step(
    monkeypatch, tmp_path: Path, missing: str, problem: str
) -> None:
    calls = _patch(monkeypatch, REPO_FILES)
    with pytest.raises(RuntimeError) as exc:
        main.handle_plan(
            _FakeClient(), _job("plan", ["envs/prod.tfvars", missing]), _settings(tmp_path)
        )
    assert str(exc.value) == (
        f"{problem} (environment 'prod', relative to the project root 'stacks/app')"
    )
    assert calls == []  # not even init


def test_a_symlink_out_of_the_checkout_is_refused(monkeypatch, tmp_path: Path) -> None:
    secret = tmp_path / "oidc_token"
    secret.write_text("token")
    calls = _patch(monkeypatch, {})
    ws = _FakeWorkspace(str(tmp_path), "run-1")
    cwd = ws.git_clone()
    (cwd / "prod.tfvars").symlink_to(secret)
    assert ws.var_file_problem(cwd, ["prod.tfvars"]) == (
        "var file escapes the repository: 'prod.tfvars'"
    )
    assert calls == []


@pytest.mark.parametrize("name", ["import", "refresh"])
def test_commands_reading_variables_get_them_before_their_args(
    monkeypatch, tmp_path: Path, name: str
) -> None:
    calls = _patch(monkeypatch, REPO_FILES)
    job = _job("command", ["envs/prod.tfvars"], command={"name": name, "args": ["-target=a.b"]})
    main.handle_command_run(_FakeClient(), job, _settings(tmp_path))
    assert calls[0][1] == "init" and not _var_flags(calls[0])
    assert calls[-1] == [
        "tofu",
        name,
        "-var-file=envs/prod.tfvars",
        f"-var-file={TFVARS_JSON}",
        "-target=a.b",
    ]


@pytest.mark.parametrize("name", ["state rm", "state list", "output", "validate", "force-unlock"])
def test_other_commands_get_none_and_do_not_need_the_files(
    monkeypatch, tmp_path: Path, name: str
) -> None:
    calls = _patch(monkeypatch, {})  # the var file isn't even there: not needed
    job = _job("command", ["envs/prod.tfvars"], command={"name": name, "args": ["x"]})
    client = _FakeClient()
    main.handle_command_run(client, job, _settings(tmp_path))
    assert client.events[-1][0] == "phase_finished", client.events
    assert all(not _var_flags(c) for c in calls)


def test_a_missing_var_file_fails_an_import_before_init(monkeypatch, tmp_path: Path) -> None:
    calls = _patch(monkeypatch, {})
    job = _job("command", ["envs/prod.tfvars"], command={"name": "import", "args": ["a.b", "id"]})
    with pytest.raises(RuntimeError, match="var file not found: 'envs/prod.tfvars'"):
        main.handle_command_run(_FakeClient(), job, _settings(tmp_path))
    assert calls == []


_TOOL = shutil.which("tofu") or shutil.which("terraform")


@pytest.mark.skipif(_TOOL is None, reason="needs tofu or terraform on PATH")
def test_precedence_with_the_real_tool(tmp_path: Path) -> None:
    """End to end on the real tool, offline (no provider): later repo files beat earlier ones and
    the repo's auto-loaded files; Stackd's JSON and HCL files beat every repo file."""
    assert _TOOL is not None
    ws = Workspace(str(tmp_path), "job")
    cwd = ws.path / "repo"
    (cwd / "envs").mkdir(parents=True)
    names = ("auto", "first", "second", "stackd", "stackd_hcl")
    (cwd / "main.tf").write_text(
        "".join(f'variable "{n}" {{ type = string }}\n' for n in names)
        + 'output "o" { value = { '
        + ", ".join(f"{n} = var.{n}" for n in names)
        + " } }\n"
    )
    (cwd / "repo.auto.tfvars").write_text(
        "".join(f'{n} = "repo-auto"\n' for n in names)
    )  # every variable, weakest
    (cwd / "envs" / "first.tfvars").write_text(
        'first = "first"\nsecond = "first"\nstackd = "first"\nstackd_hcl = "first"\n'
    )
    (cwd / "envs" / "second.tfvars").write_text('second = "second"\nstackd = "second"\n')
    job = {
        "var_files": ["envs/first.tfvars", "envs/second.tfvars"],
        "hcl_tfvars": {"stackd_hcl": '"stackd-hcl"'},
        "environment": {"name": "prod", "project_root": "."},
    }
    flags = main._var_file_flags(ws, cwd, job)

    def tool(*args: str) -> str:
        done = subprocess.run(
            [_TOOL, *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=False
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    tool("init", "-input=false", "-backend=false")
    with ws.tfvars(cwd, {"stackd": "stackd-json"}, job["hcl_tfvars"]):
        tool("apply", "-input=false", "-auto-approve", *flags)
    assert json.loads(tool("output", "-json", "o")) == {
        "auto": "repo-auto",
        "first": "first",
        "second": "second",
        "stackd": "stackd-json",
        "stackd_hcl": "stackd-hcl",
    }
