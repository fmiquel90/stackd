"""A minimal Docker Engine API client over the local unix socket (SPECS §7.4, `docker` runner).

httpx (already a dependency) speaks to the socket directly: no docker CLI, no SDK. Talking to the
API rather than running `docker run …` keeps every value of a command's environment out of any
process's argv (`ps`), and lets the runner kill and remove a container it no longer wants.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator
from typing import Any

import httpx

DEFAULT_SOCKET = "/var/run/docker.sock"


class DockerError(RuntimeError):
    pass


def socket_path(docker_host: str | None) -> str:
    """`unix:///path` (DOCKER_HOST form) or a bare path → the socket path. Only unix sockets: a TCP
    daemon would need TLS client certificates this runner does not handle."""
    if not docker_host:
        return DEFAULT_SOCKET
    if docker_host.startswith("unix://"):
        return docker_host.removeprefix("unix://")
    if docker_host.startswith("/"):
        return docker_host
    raise ValueError(
        f"STACKD_DOCKER_HOST must be a unix socket (unix:///path), got {docker_host!r}"
    )


def demux(chunks: Iterator[bytes]) -> Iterator[tuple[int, bytes]]:
    """Split the multiplexed stdout/stderr stream of a non-TTY container into (stream, payload)
    frames: an 8-byte header (stream type, 3 zero bytes, big-endian payload size), then the
    payload. stream 1 = stdout, 2 = stderr."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        while len(buf) >= 8:
            kind, size = buf[0], struct.unpack(">I", buf[4:8])[0]
            if len(buf) < 8 + size:
                break
            yield kind, buf[8 : 8 + size]
            buf = buf[8 + size :]


class LineSplitter:
    """Reassemble lines across frames, per stream (a frame can end mid-line)."""

    def __init__(self, emit: Callable[[int, str], None]) -> None:
        self._emit = emit
        self._partial: dict[int, bytes] = {}

    def feed(self, kind: int, data: bytes) -> None:
        data = self._partial.pop(kind, b"") + data
        *lines, rest = data.split(b"\n")
        for line in lines:
            self._emit(kind, line.decode("utf-8", errors="replace"))
        if rest:
            self._partial[kind] = rest

    def flush(self) -> None:
        for kind, rest in sorted(self._partial.items()):
            self._emit(kind, rest.decode("utf-8", errors="replace"))
        self._partial.clear()


class DockerEngine:
    def __init__(self, docker_host: str | None = None, timeout: float = 30.0) -> None:
        transport = httpx.HTTPTransport(uds=socket_path(docker_host))
        # The hostname is ignored on a unix socket; `docker` keeps the Host header meaningful.
        self._http = httpx.Client(transport=transport, base_url="http://docker", timeout=timeout)

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise DockerError(f"docker {what} failed ({resp.status_code}): {message}")
        return resp

    def ping(self) -> None:
        self._check(self._http.get("/_ping"), "ping")

    def image_exists(self, ref: str) -> bool:
        return self._http.get(f"/images/{ref}/json").status_code == 200

    def pull(self, ref: str) -> None:
        """Pull `ref` (`repo@sha256:…`): the daemon verifies the content against the digest."""
        repo, _, digest = ref.partition("@")
        with self._http.stream(
            "POST", "/images/create", params={"fromImage": repo, "tag": digest}, timeout=None
        ) as resp:
            self._check(resp, f"pull {ref}")
            for _ in resp.iter_bytes():  # progress messages; the pull ends with the stream
                pass
        if not self.image_exists(ref):
            raise DockerError(f"docker pull {ref} did not produce the image")

    def create(self, name: str, spec: dict[str, Any]) -> str:
        resp = self._http.post("/containers/create", params={"name": name}, json=spec)
        return str(self._check(resp, "create").json()["Id"])

    def start(self, cid: str) -> None:
        self._check(self._http.post(f"/containers/{cid}/start"), "start")

    def logs(self, cid: str) -> Iterator[tuple[int, bytes]]:
        """The container's stdout/stderr from its start, followed until it exits."""
        params = {"follow": "1", "stdout": "1", "stderr": "1"}
        with self._http.stream("GET", f"/containers/{cid}/logs", params=params, timeout=None) as r:
            self._check(r, "logs")
            yield from demux(r.iter_bytes())

    def wait(self, cid: str) -> int:
        resp = self._http.post(f"/containers/{cid}/wait", timeout=None)
        return int(self._check(resp, "wait").json().get("StatusCode", -1))

    def kill(self, cid: str, signal: str | None = None) -> bool:
        """Kill a running container (SIGKILL, or `signal`): True when it was running."""
        resp = self._http.post(
            f"/containers/{cid}/kill", params={"signal": signal} if signal else None
        )
        if resp.status_code in (404, 409):  # gone, or already stopped: nothing was killed
            return False
        self._check(resp, "kill")
        return True

    def remove(self, cid: str) -> None:
        resp = self._http.delete(f"/containers/{cid}", params={"force": "1", "v": "1"})
        if resp.status_code not in (204, 404):
            self._check(resp, "remove")
