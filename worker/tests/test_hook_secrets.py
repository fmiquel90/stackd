"""What a repo hook can read (SPECS §8.3). It runs repo-controlled code in the job's checkout, so
nothing secret may be there while it runs: not the saved plan, nor tofu's raw plan JSON (repo hooks
read a masked plan.json). Real git, real Workspace, real hooks (`sh -c`); tofu is a stand-in script
that behaves just enough like it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings

SECRET = "hunter2-<&>-s3cr3t"
# What `tofu show -json` prints for the plan: the variable values in clear, sensitive ones included.
RAW_PLAN_JSON = {
    "variables": {"db_password": {"value": SECRET}, "region": {"value": "eu-west-1"}},
    "resource_changes": [
        {
            "address": "terraform_data.db",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"input": {"password": SECRET, "region": "eu-west-1"}},
                "after_sensitive": {"input": {"password": True}},
            },
        }
    ],
    "configuration": {
        "root_module": {"variables": {"db_password": {"sensitive": True}, "region": {}}}
    },
}

# tofu: logs each call (and where it really wrote the plan file), writes a plan file that embeds
# the variable values (like a real one) behind a marker, prints RAW_PLAN_JSON for `show -json`, and
# keeps a copy of the plan file it applies.
_FAKE_TOFU = """#!/bin/sh
echo "$1" >> "$FAKE_TOFU/calls"
case "$1" in
plan)
  for a; do case "$a" in -out=*) out="${a#-out=}";; esac; done
  { echo RAW-PLAN; cat stackd.auto.tfvars.json 2>/dev/null; } > "$out"
  echo "wrote $(realpath "$out")" >> "$FAKE_TOFU/calls"
  echo '{"@level":"info","@message":"Plan: 1 to add.","type":"change_summary","changes":{"add":1}}'
  exit 2;;
show) if [ "$2" = -json ]; then cat "$FAKE_TOFU/plan.json"; else echo "the plan"; fi;;
apply) for a; do plan="$a"; done; cp "$plan" "$FAKE_TOFU/applied";;
output) echo '{}';;
esac
"""

STAGES = ("before_init", "after_init", "before_plan", "after_plan", "before_apply", "after_apply")


@pytest.fixture
def tofu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in tofu, first on PATH. Returns its directory (calls log, applied plan)."""
    home = tmp_path / "tofu"
    home.mkdir()
    (home / "tofu").write_text(_FAKE_TOFU)
    (home / "tofu").chmod(0o755)
    (home / "plan.json").write_text(json.dumps(RAW_PLAN_JSON))
    monkeypatch.setenv("FAKE_TOFU", str(home))
    return home


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _repo(tmp_path: Path, hooks: dict[str, list[str]]) -> tuple[str, str]:
    """A repository whose .stackd.yml declares `hooks` (stage → commands). Returns (url, sha)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.tf").write_text('variable "db_password" {\n  sensitive = true\n}\n')
    stackd = {
        stage: [{"name": f"repo-{stage}-{i}", "command": c} for i, c in enumerate(cmds)]
        for stage, cmds in hooks.items()
    }
    (src / ".stackd.yml").write_text(json.dumps({"version": 1, "hooks": stackd}))  # JSON is YAML
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c1")
    return f"file://{src}", _git(src, "rev-parse", "HEAD")


class _Client:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lines: list[str] = []
        self.uploads: dict[str, bytes] = {}

    def event(self, job_id: str, kind: str, **kw: Any) -> None:
        self.events.append((kind, kw))

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.lines.extend(line["msg"] for line in lines)

    def upload_artifact(self, job_id: str, name: str, data: bytes) -> None:
        self.uploads[name] = data

    def download_artifact(self, job_id: str, name: str) -> bytes:
        return self.uploads[name]


def _job(phase: str, url: str, sha: str | None, tofu: Path, **extra: Any) -> dict:
    return {
        "job_id": "run-1",
        "phase": phase,
        "environment": {
            "tool": "opentofu",
            "repo_url": url,
            "commit_sha": sha,
            "branch": "main",
            "project_root": ".",
        },
        "env": {"PATH": f"{tofu}:{os.environ['PATH']}"},
        "tfvars_json": {"db_password": SECRET, "region": "eu-west-1"},
        "mask_values": [SECRET],
        **extra,
    }


def _plan_and_apply(tmp_path: Path, url: str, sha: str, tofu: Path, **plan: Any) -> _Client:
    client = _Client()
    api = cast(ApiClient, client)
    settings = cast(
        Settings, SimpleNamespace(workspace_root=str(tmp_path / "ws"), leak_action="warn")
    )
    main.handle_plan(api, _job("plan", url, None, tofu, save_plan=True, **plan), settings)
    assert client.events[-1][0] == "phase_finished", client.events
    main.handle_apply(api, _job("apply", url, sha, tofu), settings)
    assert client.events[-1][0] == "phase_finished", client.events
    return client


def _scan_hook(tmp_path: Path, needles: list[str], names: list[str]) -> str:
    """A hook command that searches the whole job workspace (the checkout, .git/ included, and what
    lies beside it) for any of `needles` and any file named like `names`; prints what it finds."""
    (tmp_path / "needles").write_text("".join(f"{n}\n" for n in needles))
    find = " -o ".join(f"-name '{n}'" for n in names)
    (tmp_path / "scan.sh").write_text(
        "ws=$(cd .. && pwd)\n"
        f'hits=$(grep -rlaF -f {tmp_path / "needles"} "$ws" | sed "s|^$ws/||" | tr "\\n" " ")\n'
        f'files=$(find "$ws" {find} | sed "s|^$ws/||" | tr "\\n" " ")\n'
        'echo "SCAN $1 hits=[$hits] files=[$files]"\n'
    )
    return f"sh {tmp_path / 'scan.sh'}"


def test_repo_hooks_find_no_plan_in_the_workspace(tmp_path: Path, tofu: Path) -> None:
    scan = _scan_hook(tmp_path, ["RAW-PLAN"], ["plan.tfplan"])
    url, sha = _repo(tmp_path, {stage: [f"{scan} {stage}"] for stage in STAGES})
    client = _plan_and_apply(tmp_path, url, sha, tofu)

    scans = [line for line in client.lines if line.startswith("SCAN ")]
    assert scans == [f"SCAN {stage} hits=[] files=[]" for stage in STAGES], client.lines
    # The saved plan still reaches the apply, exactly as tofu made it.
    assert client.uploads["plan.tfplan"].startswith(b"RAW-PLAN\n")
    assert (tofu / "applied").read_bytes() == client.uploads["plan.tfplan"]


def test_after_plan_repo_hooks_read_a_masked_plan_json(tmp_path: Path, tofu: Path) -> None:
    copy = "cp plan.json {}"
    url, sha = _repo(tmp_path, {"after_plan": [copy.format(tmp_path / "repo.json")]})
    platform = {"name": "infracost", "command": copy.format(tmp_path / "platform.json")}
    client = _plan_and_apply(
        tmp_path, url, sha, tofu, hooks={"after_plan": [{**platform, "source": "platform"}]}
    )

    # Platform hooks are trusted with the run's secrets (§8.3): tofu's plan JSON, as is.
    assert json.loads((tmp_path / "platform.json").read_text()) == RAW_PLAN_JSON
    # A repo hook reads the masked copy: the sensitive value is gone, the rest is intact.
    repo_doc = (tmp_path / "repo.json").read_text()
    assert SECRET not in repo_doc
    masked = json.loads(repo_doc)
    assert masked["variables"] == {
        "db_password": {"value": "***"},
        "region": {"value": "eu-west-1"},
    }
    after = masked["resource_changes"][0]["change"]["after"]
    assert after == {"input": {"password": "***", "region": "eu-west-1"}}
    # The plan.json artifact is that masked copy too.
    assert client.uploads["plan.json"].decode() == repo_doc


def test_files_a_repo_hook_plants_never_receive_the_plan(tmp_path: Path, tofu: Path) -> None:
    # A before_plan hook plants symlinks where the plan and plan.json used to be written.
    loot = tmp_path / "loot"
    loot.mkdir()
    plant = (
        f"ln -s {loot}/a plan.tfplan && ln -s {loot}/b plan.json && ln -s {loot}/c ../plan.tfplan"
    )
    url, sha = _repo(tmp_path, {"before_plan": [plant], "after_plan": ["true"]})
    client = _plan_and_apply(tmp_path, url, sha, tofu)

    assert sorted(loot.iterdir()) == []  # nothing was written through them…
    [wrote] = [c for c in (tofu / "calls").read_text().splitlines() if c.startswith("wrote ")]
    assert wrote == f"wrote {tmp_path / 'ws' / 'run-1' / 'plan.tfplan'}"  # …not even for a while
    assert client.uploads["plan.tfplan"].startswith(b"RAW-PLAN\n")
