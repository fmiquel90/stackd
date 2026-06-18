from __future__ import annotations

import json
import os
from pathlib import Path

from agent.main import _repo_environ, _secret_leak_in_outputs
from agent.masking import Masker
from agent.runner import run_hooks


class _FakeStreamer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        self.lines.extend(lines)


def test_repo_environ_strips_cloud_creds(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "akia-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "azp")
    monkeypatch.setenv("PROJECT_NAME", "demo")
    env = _repo_environ()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AWS_SESSION_TOKEN" not in env
    assert "AZURE_CLIENT_SECRET" not in env
    assert env.get("PROJECT_NAME") == "demo"


def test_repo_hook_sees_neither_cloud_nor_sensitive_env(tmp_path: Path) -> None:
    # Platform hooks get the full secret env; repo hooks must get only repo_env (§8.3).
    streamer = _FakeStreamer()
    results, aborted = run_hooks(
        [
            {
                "name": "repo-check",
                "source": "repo",
                "command": "echo AWS=[$AWS_SECRET_ACCESS_KEY] DB=[$DB_PASSWORD]",
            }
        ],
        tmp_path,
        platform_env={"AWS_SECRET_ACCESS_KEY": "akia-secret", "DB_PASSWORD": "p@ssw0rd"},
        repo_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROJECT": "demo"},
        phase="planning",
        streamer=streamer,
    )
    assert not aborted
    joined = "\n".join(streamer.lines)
    assert "akia-secret" not in joined and "p@ssw0rd" not in joined
    assert "AWS=[]" in joined and "DB=[]" in joined


def test_tripwire_flags_secret_in_non_sensitive_output() -> None:
    masker = Masker(["super-secret-value"])
    plan = json.dumps(
        {
            "planned_values": {
                "outputs": {
                    "leaked": {"sensitive": False, "value": "super-secret-value"},
                    "safe": {"sensitive": False, "value": "public"},
                }
            }
        }
    )
    leak = _secret_leak_in_outputs(plan, masker)
    assert leak is not None and "leaked" in leak and "safe" not in leak


def test_tripwire_ignores_sensitive_output() -> None:
    masker = Masker(["super-secret-value"])
    plan = json.dumps(
        {"planned_values": {"outputs": {"db": {"sensitive": True, "value": "super-secret-value"}}}}
    )
    assert _secret_leak_in_outputs(plan, masker) is None


def test_tripwire_scans_flat_output_json() -> None:
    # `tofu output -json` is a flat name→{value,sensitive,type} map (apply path).
    masker = Masker(["leaked-token"])
    doc = json.dumps({"url": {"sensitive": False, "value": "leaked-token", "type": "string"}})
    assert _secret_leak_in_outputs(doc, masker) is not None


def test_tripwire_no_secrets_is_noop() -> None:
    assert _secret_leak_in_outputs("{}", Masker([])) is None
    assert _secret_leak_in_outputs("not json", Masker(["x"])) is None
