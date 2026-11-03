"""The `docker` runner (SPECS §7.4): one hardened, ephemeral container per command.

The spec tests need no daemon. The integration tests run real containers (an `alpine` image stands
in for a runner image: they exercise the isolation, not tofu) and are skipped without a Docker
daemon."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from agent.docker_engine import DockerEngine, LineSplitter, demux
from agent.docker_runner import DockerLimits, DockerRunner
from agent.main import _job_environ, _repo_environ
from agent.runner import _stream_proc

IMAGE = os.environ.get("STACKD_TEST_DOCKER_IMAGE", "alpine:3.20")


def _runner(ws: Path, engine: DockerEngine | None = None, **limits: object) -> DockerRunner:
    return DockerRunner(
        engine or DockerEngine(),
        IMAGE,
        ws,
        "job-1",
        DockerLimits(**limits),  # type: ignore[arg-type]
    )


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "job-1"
    (ws / "repo" / "infra").mkdir(parents=True)
    return ws


# --- the container spec (no daemon) ---


def test_job_container_is_hardened_and_mounts_the_workspace(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    spec = _runner(ws, memory_bytes=2**30, nano_cpus=2 * 10**9, runtime="runsc").spec(
        ["tofu", "plan"], ws / "repo" / "infra", {"TF_VAR_x": "secret-value"}, "10001:10001"
    )
    host = spec["HostConfig"]
    assert spec["Cmd"] == ["tofu", "plan"] and spec["Entrypoint"] == []
    assert host["Binds"] == [f"{ws.resolve()}:{ws.resolve()}:rw"]
    assert spec["WorkingDir"] == str((ws / "repo" / "infra").resolve())
    assert host["ReadonlyRootfs"] is True and "/tmp" in host["Tmpfs"]
    assert host["CapDrop"] == ["ALL"] and host["Privileged"] is False
    assert "no-new-privileges:true" in host["SecurityOpt"] and host["Init"] is True
    assert host["Memory"] == 2**30 and host["NanoCpus"] == 2 * 10**9 and host["Runtime"] == "runsc"
    assert spec["User"] == "10001:10001"
    # The env travels in the API body, never on a command line.
    assert "TF_VAR_x=secret-value" in spec["Env"] and "HOME=/tmp" in spec["Env"]
    assert not any("secret-value" in part for part in spec["Cmd"])


def test_repo_hook_container_mounts_the_checkout_only(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    repo = _runner(ws).for_repo()
    spec = repo.spec(["sh", "-c", "true"], ws / "repo" / "infra", {}, "1:1")
    checkout = (ws / "repo").resolve()
    assert spec["HostConfig"]["Binds"] == [f"{checkout}:{checkout}:rw"]
    with pytest.raises(RuntimeError, match="outside the mounted"):
        repo.spec(["sh"], ws, {}, "1:1")  # the workspace itself (token file, saved plan): refused


def test_docker_containers_get_none_of_the_hosts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAHOST")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("PATH", "/opt/venv/bin:/usr/bin")
    for env in (_job_environ("docker"), _repo_environ("docker")):
        assert env.get("HTTPS_PROXY") == "http://proxy:3128"
        assert not {"AWS_ACCESS_KEY_ID", "DATABASE_URL", "PATH"} & set(env)


def test_demux_reassembles_lines_across_frames() -> None:
    def frame(kind: int, data: bytes) -> bytes:
        return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data

    raw = frame(1, b"hel") + frame(2, b"err\n") + frame(1, b"lo\nwor") + frame(1, b"ld")
    chunks = [raw[:5], raw[5:17], raw[17:]]  # frame headers split across reads
    got: list[tuple[int, str]] = []
    lines = LineSplitter(lambda k, line: got.append((k, line)))
    for kind, data in demux(iter(chunks)):
        lines.feed(kind, data)
    lines.flush()
    assert got == [(2, "err"), (1, "hello"), (1, "world")]


# --- real containers ---


def _engine() -> DockerEngine:
    engine = DockerEngine()
    try:
        engine.ping()
    except (httpx.HTTPError, OSError) as exc:
        pytest.skip(f"no Docker daemon: {exc}")
    if not engine.image_exists(IMAGE):
        pytest.skip(f"test image {IMAGE} not present (docker pull {IMAGE})")
    return engine


class _Streamer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        self.lines.extend(lines)


def _run(runner: DockerRunner, cmd: str, cwd: Path, env: dict | None = None, timeout: int = 60):
    streamer = _Streamer()
    rc = _stream_proc(
        ["sh", "-c", cmd],
        cwd,
        env or {},
        phase="planning",
        section=None,
        streamer=streamer,  # type: ignore[arg-type]
        timeout=timeout,
        transform=lambda line: line,
        runner=runner,
    )
    return rc, streamer.lines


def _leftovers(engine: DockerEngine) -> list:
    resp = engine._http.get(
        "/containers/json", params={"all": "1", "filters": '{"label":["io.stackd.job=job-1"]}'}
    )
    return resp.json()


@pytest.mark.docker
def test_command_output_env_and_exit_code(tmp_path: Path) -> None:
    engine = _engine()
    ws = _ws(tmp_path)
    rc, lines = _run(
        _runner(ws, engine), 'echo "v=$MY_VAR"; echo oops >&2; exit 3', ws / "repo", {"MY_VAR": "1"}
    )
    # stdout and stderr are two streams: docker keeps each in order, not their interleaving.
    assert rc == 3 and sorted(lines) == ["oops", "v=1"]
    assert _leftovers(engine) == []  # removed once the command exited


@pytest.mark.docker
def test_timeout_kills_the_container(tmp_path: Path) -> None:
    engine = _engine()
    ws = _ws(tmp_path)
    rc, lines = _run(_runner(ws, engine), "echo start; sleep 30", ws / "repo", timeout=2)
    assert rc == 124 and lines[0] == "start" and "timed out" in lines[-1]
    assert _leftovers(engine) == []


@pytest.mark.docker
def test_nothing_a_command_starts_outlives_it(tmp_path: Path) -> None:
    """The case the local runner can't contain (§8.3): a `setsid` daemon left by a hook."""
    engine = _engine()
    ws = _ws(tmp_path)
    runner = _runner(ws, engine)
    rc, _ = _run(runner.for_repo(), "setsid sh -c 'sleep 60' & echo spawned", ws / "repo")
    assert rc == 0 and _leftovers(engine) == []
    rc, lines = _run(runner, "ps -eo comm | grep -c sleep || true", ws / "repo")
    assert lines == ["0"]


@pytest.mark.docker
def test_repo_hook_sees_no_secret_file(tmp_path: Path) -> None:
    engine = _engine()
    ws = _ws(tmp_path)
    (ws / "oidc_token").write_text("workload-token")
    runner = _runner(ws, engine)
    rc, lines = _run(runner.for_repo(), "cat ../oidc_token", ws / "repo")
    assert rc != 0 and "workload-token" not in lines
    rc, lines = _run(runner, "cat ../oidc_token", ws / "repo")  # tofu / a platform hook: allowed
    assert rc == 0 and lines == ["workload-token"]


@pytest.mark.docker
def test_container_is_unprivileged_and_read_only(tmp_path: Path) -> None:
    engine = _engine()
    ws = _ws(tmp_path)
    runner = _runner(ws, engine)
    rc, lines = _run(runner, "id -u; touch /etc/x 2>/dev/null || echo ro; touch /tmp/ok", ws)
    assert rc == 0 and lines[0] != "0" and lines[1] == "ro"
    rc, _ = _run(runner, "touch new-file", ws / "repo")  # the workspace is writable
    assert rc == 0 and (ws / "repo" / "new-file").exists()


@pytest.mark.docker
def test_capture_keeps_stdout_and_stderr_apart(tmp_path: Path) -> None:
    engine = _engine()
    ws = _ws(tmp_path)
    out = _runner(ws, engine).capture(
        ["sh", "-c", "echo '{\"a\": 1}'; echo warn >&2"], ws / "repo", {}
    )
    assert out.returncode == 0 and out.stdout == '{"a": 1}\n' and out.stderr == "warn"
