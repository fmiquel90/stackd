"""The `docker` runner (SPECS §7.4): every command of a job — tofu and hooks — in a fresh,
hardened, ephemeral container of the runner image pinned for the job's tool version.

One container per command, removed as soon as the command exits: nothing a command starts outlives
it (a `setsid` daemon included), so no later step's environment or files are within its reach.
What each container sees:

- tofu and **platform** hooks: the job's workspace (the checkout, plus the OIDC token file and the
  plan job's saved plan while a command that needs them runs);
- **repo** hooks (untrusted, §8.3): the checkout only.

The workspace is mounted at the same path it has on the agent's side, so every path the agent hands
a command (the token file, `-out=`, the cwd) is valid as is. Nothing of the agent's own environment
reaches a container: only the env the agent builds for the command (§7.4, §8.3).
"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent import shutdown
from agent.docker_engine import DockerEngine, LineSplitter

# The runner images' user (deploy/images): a root agent hands the workspace to it.
RUNNER_UID = 10001


@dataclass(frozen=True)
class DockerLimits:
    """Operator settings applied to every container (STACKD_DOCKER_*)."""

    network: str = "bridge"  # STACKD_DOCKER_NETWORK: e.g. an egress-filtered network
    memory_bytes: int | None = None  # STACKD_DOCKER_MEMORY
    nano_cpus: int | None = None  # STACKD_DOCKER_CPUS × 1e9
    pids_limit: int = 1024  # STACKD_DOCKER_PIDS_LIMIT
    runtime: str | None = None  # STACKD_DOCKER_RUNTIME: e.g. `runsc` (gVisor)
    tmp_size: str = "1g"  # the /tmp tmpfs (HOME): the rest of the root filesystem is read-only


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    """Hand the workspace to the runner image's user. Never follows a symlink: a link a command
    planted in the checkout can't redirect the chown out of the workspace."""
    os.chown(root, uid, gid, follow_symlinks=False)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)


class DockerRunner:
    name = "docker"

    def __init__(
        self,
        engine: DockerEngine,
        image: str,
        workspace: Path,
        job_id: str,
        limits: DockerLimits,
        *,
        repo_only: bool = False,
    ) -> None:
        self._engine = engine
        self._image = image
        self._workspace = workspace.resolve()
        self._job_id = job_id
        self._limits = limits
        self._repo_only = repo_only

    def for_repo(self) -> DockerRunner:
        """The runner for an untrusted repo hook: a container that mounts the checkout only."""
        return DockerRunner(
            self._engine,
            self._image,
            self._workspace,
            self._job_id,
            self._limits,
            repo_only=True,
        )

    @property
    def mount(self) -> Path:
        return self._workspace / "repo" if self._repo_only else self._workspace

    def _user(self) -> str:
        if os.getuid() == 0:
            # A root agent's files must not reach a container as root: the workspace goes to the
            # image's unprivileged user, which the agent (root) can still read and clean up.
            _chown_tree(self._workspace, RUNNER_UID, RUNNER_UID)
            return f"{RUNNER_UID}:{RUNNER_UID}"
        return f"{os.getuid()}:{os.getgid()}"

    def spec(self, cmd: list[str], cwd: Path, env: dict[str, str], user: str) -> dict[str, Any]:
        """The container to create for one command (Docker Engine API `ContainerCreate`)."""
        cwd = cwd.resolve()
        if not cwd.is_relative_to(self.mount):
            raise RuntimeError(f"command cwd {cwd} is outside the mounted {self.mount}")
        envs = {"HOME": "/tmp", **env}
        lim = self._limits
        host: dict[str, Any] = {
            "Binds": [f"{self.mount}:{self.mount}:rw"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": f"rw,nosuid,nodev,size={lim.tmp_size}"},
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": lim.pids_limit,
            "NetworkMode": lim.network,
            "Init": True,  # an init reaps and signals what the command leaves behind
            "Privileged": False,
        }
        if lim.memory_bytes:
            host["Memory"] = lim.memory_bytes
            host["MemorySwap"] = lim.memory_bytes  # no swap beyond the limit
        if lim.nano_cpus:
            host["NanoCpus"] = lim.nano_cpus
        if lim.runtime:
            host["Runtime"] = lim.runtime
        return {
            "Image": self._image,
            "Entrypoint": [],
            "Cmd": cmd,
            "Env": [f"{k}={v}" for k, v in envs.items()],
            "WorkingDir": str(cwd),
            "User": user,
            "Tty": False,
            "OpenStdin": False,
            "Labels": {"io.stackd.job": self._job_id, "io.stackd.repo-only": str(self._repo_only)},
            "HostConfig": host,
        }

    def _run(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        on_output: Callable[[int, str], None],
    ) -> tuple[int, bool]:
        name = f"stackd-{self._job_id}-{uuid.uuid4().hex[:12]}"
        cid = self._engine.create(name, self.spec(cmd, cwd, env, self._user()))
        killed = {"v": False}

        def _kill() -> None:
            # A container that exited a hair before the timer keeps its real exit code.
            killed["v"] = self._engine.kill(cid)

        timer = threading.Timer(timeout, _kill)
        try:
            self._engine.start(cid)
            timer.start()
            # A shutdown abort (agent.shutdown) kills the container too — not a timeout.
            with shutdown.SHUTDOWN.command(lambda: self._engine.kill(cid)):
                lines = LineSplitter(on_output)
                for kind, data in self._engine.logs(cid):
                    lines.feed(kind, data)
                lines.flush()
                rc = self._engine.wait(cid)
        finally:
            timer.cancel()
            # Whatever happened — exit, timeout, an error streaming the output — the container and
            # everything still running in it are gone before the next step.
            self._engine.remove(cid)
        return rc, killed["v"]

    def stream(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        timeout: int,
        on_line: Callable[[str], None],
    ) -> tuple[int, bool]:
        return self._run(cmd, cwd, env, timeout, lambda _kind, line: on_line(line))

    def capture(
        self, cmd: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        out: dict[int, list[str]] = {1: [], 2: []}
        rc, _ = self._run(
            cmd, cwd, env, 2 * 60 * 60, lambda kind, line: out.setdefault(kind, []).append(line)
        )
        return subprocess.CompletedProcess(
            cmd, rc, "\n".join(out[1]) + ("\n" if out[1] else ""), "\n".join(out[2])
        )
