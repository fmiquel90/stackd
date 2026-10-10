"""What a repo hook can read (SPECS §8.3). It runs repo-controlled code in the job's checkout, so
nothing secret may be there while it runs: not the saved plan, nor tofu's raw plan JSON (repo hooks
read a masked plan.json), nor the run's tfvars, nor the repo token; and of the worker's own
environment it gets only what a shell command needs. Real git (over HTTP with a token,
where it matters), real Workspace, real hooks (`sh -c`); tofu is a stand-in script that behaves just
enough like it — or the real one, when it is installed."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import pytest

import agent.main as main
from agent.client import ApiClient
from agent.config import Settings
from agent.workspace import Workspace

SECRET = "hunter2-<&>-s3cr3t"
HCL_SECRET = "hcl-s3cr3t"  # in an hcl-typed variable, written to its own .auto.tfvars (§3.4)
TFVARS_FILES = ["stackd.auto.tfvars.json", "zzz_stackd.auto.tfvars"]
TOKEN = "ghp_t0k@en/%25"  # the repo token: on disk, git would hold it percent-encoded
TOKEN_FORMS = [TOKEN, quote(TOKEN, safe="")]
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

# tofu: logs each call (whether the run's tfvars were there, and where it really wrote the plan
# file), writes a plan file that embeds the variable values (like a real one) behind a marker,
# prints RAW_PLAN_JSON for `show -json`, and keeps a copy of the plan file it applies.
_FAKE_TOFU = """#!/bin/sh
tfvars=no; [ -f stackd.auto.tfvars.json ] && [ -f zzz_stackd.auto.tfvars ] && tfvars=yes
echo "$1 tfvars=$tfvars" >> "$FAKE_TOFU/calls"
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


class _GitHttp(BaseHTTPRequestHandler):
    """Smart-HTTP git (`git http-backend`, as CGI) serving the repositories under `root`, behind
    Basic auth: only the repo token gets in, like a private repository."""

    root = Path()

    def _serve(self) -> None:
        expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != f"Basic {expected}":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        cgi = subprocess.run(
            ["git", "http-backend"],
            input=body,
            capture_output=True,
            env={
                "PATH": os.environ["PATH"],
                "GIT_PROJECT_ROOT": str(self.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REMOTE_USER": "x-access-token",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            },
        )
        head, _, payload = cgi.stdout.partition(b"\r\n\r\n")
        headers = [line.decode().split(":", 1) for line in head.split(b"\r\n") if line]
        status = next((int(v.split()[0]) for k, v in headers if k.lower() == "status"), 200)
        self.send_response(status)
        for k, v in headers:
            if k.lower() != "status":
                self.send_header(k, v.strip())
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def git_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The base URL of a private git server for the repositories under tmp_path (`_repo` makes
    one at `<url>/src`). git_clone inherits os.environ: no proxy between it and the loopback."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    handler = type("_Handler", (_GitHttp,), {"root": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _repo(
    tmp_path: Path, hooks: dict[str, list[str]], links: dict[str, Path] | None = None
) -> tuple[str, str]:
    """A repository whose .stackd.yml declares `hooks` (stage → commands), with symlinks `links`
    (name → target) committed at its root. Returns (url, sha)."""
    src = tmp_path / "src"
    src.mkdir()
    for name, target in (links or {}).items():
        (src / name).symlink_to(target)
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
        "hcl_tfvars": {"tags": f'{{ team = "{HCL_SECRET}" }}'},
        "mask_values": [SECRET, HCL_SECRET, TOKEN],
        **extra,
    }


def _plan_and_apply(
    tmp_path: Path, url: str, sha: str, tofu: Path, repo_credentials: Any = None, **plan: Any
) -> _Client:
    client = _Client()
    api = cast(ApiClient, client)
    settings = cast(
        Settings, SimpleNamespace(workspace_root=str(tmp_path / "ws"), leak_action="warn")
    )
    creds = {"repo_credentials": repo_credentials}
    main.handle_plan(api, _job("plan", url, None, tofu, save_plan=True, **creds, **plan), settings)
    assert client.events[-1][0] == "phase_finished", client.events
    main.handle_apply(api, _job("apply", url, sha, tofu, **creds), settings)
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


def _calls(tofu: Path) -> list[str]:
    return [c for c in (tofu / "calls").read_text().splitlines() if not c.startswith("wrote ")]


def test_repo_hooks_find_no_secret_in_the_workspace(
    tmp_path: Path, tofu: Path, git_http: str
) -> None:
    needles = ["RAW-PLAN", SECRET, HCL_SECRET, *TOKEN_FORMS]
    scan = _scan_hook(tmp_path, needles, ["plan.tfplan", *TFVARS_FILES])
    _, sha = _repo(tmp_path, {stage: [f"{scan} {stage}"] for stage in STAGES})
    # A private repository: both jobs clone it with the token (the apply also fetches its commit).
    creds = {"kind": "token", "token": TOKEN}
    client = _plan_and_apply(tmp_path, f"{git_http}/src", sha, tofu, repo_credentials=creds)

    scans = [line for line in client.lines if line.startswith("SCAN ")]
    assert scans == [f"SCAN {stage} hits=[] files=[]" for stage in STAGES], client.lines
    # tofu still reads the run's variables wherever it needs them — at apply too, so it still
    # refuses a saved plan whose inputs changed (§7.3) — and only there.
    assert _calls(tofu) == [
        "init tfvars=yes",
        "plan tfvars=yes",
        "show tfvars=no",
        "show tfvars=no",
        "init tfvars=yes",
        "apply tfvars=yes",
        "output tfvars=no",
    ]
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


def test_files_a_repo_plants_never_receive_a_secret(tmp_path: Path, tofu: Path) -> None:
    # The repo commits symlinks where the tfvars are written, and a before_plan hook plants more
    # where the plan and plan.json used to be.
    loot = tmp_path / "loot"
    loot.mkdir()
    links = {name: loot / name for name in TFVARS_FILES}
    plant = (
        f"ln -s {loot}/a plan.tfplan && ln -s {loot}/b plan.json && ln -s {loot}/c ../plan.tfplan"
    )
    url, sha = _repo(tmp_path, {"before_plan": [plant], "after_plan": ["true"]}, links)
    client = _plan_and_apply(tmp_path, url, sha, tofu)

    assert sorted(loot.iterdir()) == []  # nothing was written through them…
    [wrote] = [c for c in (tofu / "calls").read_text().splitlines() if c.startswith("wrote ")]
    assert wrote == f"wrote {tmp_path / 'ws' / 'run-1' / 'plan.tfplan'}"  # …not even for a while
    assert client.uploads["plan.tfplan"].startswith(b"RAW-PLAN\n")


def test_command_run_reads_the_tfvars(tmp_path: Path, tofu: Path) -> None:
    # A command (refresh, import, …) needs the run's variables like a plan does.
    url, sha = _repo(tmp_path, {})
    client = _Client()
    job = _job("command", url, sha, tofu, cred_phase="apply", command={"name": "refresh"})
    main.handle_command_run(
        cast(ApiClient, client), job, cast(Settings, SimpleNamespace(workspace_root=str(tmp_path)))
    )
    assert client.events[-1][0] == "phase_finished", client.events
    assert _calls(tofu) == ["init tfvars=yes", "refresh tfvars=yes"]


_REAL_MAIN_TF = """terraform {
  backend "local" { path = "%(state)s" }
}
variable "db_password" {
  type      = string
  sensitive = true
}
variable "region" { type = string }
variable "tags" { type = map(string) }
resource "terraform_data" "db" {
  input = { password = var.db_password, region = var.region, team = var.tags.team }
}
output "region" { value = var.region }
"""


@pytest.mark.skipif(shutil.which("tofu") is None, reason="needs OpenTofu on PATH")
def test_real_tofu_plans_applies_and_runs_commands(tmp_path: Path) -> None:
    """The real tofu: init/plan/apply and commands still read the run's variables, the apply takes
    the saved plan, and repo hooks find no secret and read a masked plan.json. The state lives
    outside the workspace (a real job's is behind the HTTP backend, §11)."""
    scan = _scan_hook(tmp_path, [SECRET, HCL_SECRET], ["plan.tfplan", *TFVARS_FILES])
    hooks = {stage: [f"{scan} {stage}"] for stage in STAGES}
    hooks["after_plan"].append(f"cp plan.json {tmp_path / 'repo.json'}")
    url, sha = _repo(tmp_path, hooks)
    src = tmp_path / "src"
    (src / "main.tf").write_text(_REAL_MAIN_TF % {"state": tmp_path / "state" / "tfstate"})
    _git(src, "commit", "-q", "-am", "real config")
    sha = _git(src, "rev-parse", "HEAD")

    tofu = Path(str(shutil.which("tofu"))).parent
    client = _plan_and_apply(tmp_path, url, sha, tofu)
    planned, applied = [kw["result"] for kind, kw in client.events if kind == "phase_finished"]
    assert planned["summary"] == {"add": 1, "change": 0, "destroy": 0}, planned
    assert applied["outputs"]["region"]["value"] == "eu-west-1", applied
    scans = [line for line in client.lines if line.startswith("SCAN ")]
    assert scans == [f"SCAN {stage} hits=[] files=[]" for stage in STAGES], client.lines

    masked = json.loads((tmp_path / "repo.json").read_text())
    assert masked["variables"]["db_password"] == {"value": "***"}
    assert masked["variables"]["region"] == {"value": "eu-west-1"}
    after = masked["resource_changes"][0]["change"]["after"]["input"]
    assert after == {"password": "***", "region": "eu-west-1", "team": "***"}

    for name, args in (("refresh", ["-input=false"]), ("state list", [])):
        job = _job("command", url, sha, tofu, cred_phase="apply")
        job["command"] = {"name": name, "args": args}
        main.handle_command_run(
            cast(ApiClient, client),
            job,
            cast(Settings, SimpleNamespace(workspace_root=str(tmp_path / "ws"))),
        )
        assert client.events[-1] == ("phase_finished", {"result": {"command": name}}), name


def test_clone_keeps_no_token_in_the_checkout(tmp_path: Path, git_http: str) -> None:
    _, sha = _repo(tmp_path, {})
    _git(tmp_path / "src", "commit", "-q", "--allow-empty", "-m", "c2")  # the pinned one isn't tip
    url = f"{git_http}/src"
    ws = Workspace(str(tmp_path / "ws"), "job")
    ws.git_clone(url, sha, ".", token=TOKEN, branch="main")

    assert ws.head_sha() == sha  # the pinned commit, fetched with the token
    hits = subprocess.run(
        ["grep", "-rlaF", *(f"-e{form}" for form in TOKEN_FORMS), str(ws.path)],
        capture_output=True,
        text=True,
    )
    assert hits.stdout == "", hits.stdout  # nowhere: not in .git/config, FETCH_HEAD, reflogs, …
    assert _git(ws.path / "repo", "remote", "get-url", "origin") == url


# All a repo hook may get of the worker's own environment (§8.3), and what `sh` sets itself.
_ALLOWED_WORKER_ENV = {
    *("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG", "LANGUAGE", "TZ"),
    *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"),
    *("http_proxy", "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"),
}
_SHELL_SET = {"PWD", "OLDPWD", "SHLVL", "_"}


def test_repo_hooks_get_only_allowlisted_worker_env(
    tmp_path: Path, tofu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever secrets an operator's worker host carries in its environment.
    secrets = {
        "STACKD_ENCRYPTION_KEY": "enc-key-1",
        "STACKD_JWT_SECRET": "jwt-secret-2",
        "STACKD_POOL_TOKEN": "pool-token-3",
        "DATABASE_URL": "postgresql://stackd:db-pw-4@db/stackd",
        "MY_TOKEN": "my-token-5",
        "AWS_SECRET_ACCESS_KEY": "aws-key-6",
    }
    basics = {
        "HOME": "/home/worker",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(tmp_path),
        "TERM": "dumb",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "NO_PROXY": "localhost",
    }
    for name, value in {**secrets, **basics}.items():
        monkeypatch.setenv(name, value)
    dump = "env | sed 's/^/{} /'"
    url, sha = _repo(tmp_path, {"before_init": [dump.format("REPO")]})
    platform = {"name": "governance", "command": dump.format("PLATFORM"), "source": "platform"}
    client = _plan_and_apply(tmp_path, url, sha, tofu, hooks={"before_init": [platform]})

    def seen(who: str) -> dict[str, str]:
        prefix = f"{who} "
        lines = [line.removeprefix(prefix) for line in client.lines if line.startswith(prefix)]
        return dict(line.split("=", 1) for line in lines if "=" in line)

    repo, platform_env = seen("REPO"), seen("PLATFORM")
    assert not set(secrets) & set(repo), sorted(repo)  # names only: never print a value
    assert not any(v in line for v in secrets.values() for line in client.lines if "REPO" in line)
    assert {k: repo.get(k) for k in basics} == basics
    assert repo["PATH"] == f"{tofu}:{os.environ['PATH']}"  # the run's env still layers on top
    run_env = _job("plan", url, None, tofu)["env"]
    others = {
        k
        for k in repo
        if k not in _ALLOWED_WORKER_ENV | _SHELL_SET | set(run_env) and not k.startswith("LC_")
    }
    assert others == set(), others  # nothing else of the worker's environment
    # Platform hooks keep the worker's environment (§8.3).
    assert {k: platform_env.get(k) for k in secrets} == secrets
