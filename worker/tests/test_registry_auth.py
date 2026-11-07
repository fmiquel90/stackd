"""Private registry credentials for runner image pulls (SPECS §7.4): `STACKD_REGISTRY_AUTH_FILE`,
a Docker config.json; the `X-Registry-Auth` header of the matching registry, never logged, never in
an error, never in a job container's environment."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

import agent.main as main
from agent.config import Settings
from agent.docker_engine import DockerEngine, DockerError
from agent.registry_auth import (
    RegistryAuthError,
    RegistryCredential,
    config_key_host,
    credential_for,
    load_auths,
    registry_host,
)

DIGEST = "sha256:" + "a" * 64
PASSWORD = "s3cr3t-Pa55word"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.refresh-token"


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _decode_header(value: str) -> dict[str, str]:
    return json.loads(base64.urlsafe_b64decode(value))


def _config(tmp_path: Path, doc: Any) -> str:
    path = tmp_path / "config.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(path)


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        (f"registry.example.com/stackd/opentofu@{DIGEST}", "registry.example.com"),
        (f"registry.example.com:5000/stackd/opentofu@{DIGEST}", "registry.example.com:5000"),
        (f"localhost:5000/opentofu@{DIGEST}", "localhost:5000"),
        (f"localhost/opentofu@{DIGEST}", "localhost"),
        (f"Registry.Example.COM/team/x:1.0@{DIGEST}", "registry.example.com"),
        (f"stackd/opentofu@{DIGEST}", "docker.io"),
        (f"alpine@{DIGEST}", "docker.io"),
        (f"docker.io/stackd/opentofu@{DIGEST}", "docker.io"),
        (f"index.docker.io/stackd/opentofu@{DIGEST}", "docker.io"),
        (
            f"123456789012.dkr.ecr.eu-west-3.amazonaws.com/opentofu@{DIGEST}",
            "123456789012.dkr.ecr.eu-west-3.amazonaws.com",
        ),
    ],
)
def test_registry_host_of_a_reference(ref: str, host: str) -> None:
    assert registry_host(ref) == host


@pytest.mark.parametrize(
    ("key", "host"),
    [
        ("https://index.docker.io/v1/", "docker.io"),
        ("docker.io", "docker.io"),
        ("registry-1.docker.io", "docker.io"),
        ("registry.example.com", "registry.example.com"),
        ("https://registry.example.com:5000/v2/", "registry.example.com:5000"),
        ("http://localhost:5000", "localhost:5000"),
    ],
)
def test_config_key_host(key: str, host: str) -> None:
    assert config_key_host(key) == host


def test_auth_entry_gives_user_and_password(tmp_path: Path) -> None:
    path = _config(
        tmp_path, {"auths": {"registry.example.com:5000": {"auth": _b64(f"ci:{PASSWORD}")}}}
    )
    cred = credential_for(f"registry.example.com:5000/stackd/opentofu@{DIGEST}", path)
    assert cred is not None
    assert _decode_header(cred.header()) == {
        "username": "ci",
        "password": PASSWORD,
        "serveraddress": "registry.example.com:5000",
    }
    # Another registry, or the same host on another port: anonymous.
    assert credential_for(f"registry.example.com/stackd/opentofu@{DIGEST}", path) is None
    assert credential_for(f"other.example.com/x@{DIGEST}", path) is None


def test_docker_hub_key_matches_hub_images(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {"auths": {"https://index.docker.io/v1/": {"username": "me", "password": PASSWORD}}},
    )
    for ref in (f"stackd/opentofu@{DIGEST}", f"docker.io/stackd/opentofu@{DIGEST}"):
        cred = credential_for(ref, path)
        assert cred is not None
        assert _decode_header(cred.header()) == {
            "username": "me",
            "password": PASSWORD,
            "serveraddress": "https://index.docker.io/v1/",
        }


def test_identity_token_entry(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {
            "auths": {
                "myreg.azurecr.io": {
                    "auth": _b64("00000000-0000-0000-0000-000000000000:"),
                    "identitytoken": TOKEN,
                }
            }
        },
    )
    cred = credential_for(f"myreg.azurecr.io/opentofu@{DIGEST}", path)
    assert cred is not None
    assert _decode_header(cred.header()) == {
        "identitytoken": TOKEN,
        "serveraddress": "myreg.azurecr.io",
    }


def test_header_is_base64url(tmp_path: Path) -> None:
    # A password whose JSON encodes to `+`/`/` in standard base64 must use `-`/`_`.
    cred = RegistryCredential("r.example.com", "u", "??>>??>>~~~")
    header = cred.header()
    assert "+" not in header and "/" not in header
    assert _decode_header(header)["password"] == "??>>??>>~~~"


def test_entries_without_a_credential_are_skipped(tmp_path: Path, caplog: Any) -> None:
    """`docker login` with a credential helper writes `{}`: not supported, says so, no crash."""
    path = _config(tmp_path, {"auths": {"registry.example.com": {}}, "credsStore": "desktop"})
    with caplog.at_level(logging.WARNING):
        assert load_auths(path) == {}
    assert credential_for(f"registry.example.com/x@{DIGEST}", path) is None


@pytest.mark.parametrize(
    "doc",
    [
        {"auths": {"registry.example.com": {"auth": "not base64 !!" + PASSWORD}}},
        {"auths": {"registry.example.com": {"auth": _b64(PASSWORD)}}},  # no `user:` part
        '{"auths": {"registry.example.com": {"auth": "' + PASSWORD,  # truncated JSON
    ],
)
def test_a_broken_file_never_quotes_its_content(tmp_path: Path, doc: Any) -> None:
    path = _config(tmp_path, doc)
    with pytest.raises(RegistryAuthError) as exc:
        load_auths(path)
    assert PASSWORD not in str(exc.value)


def test_a_missing_file_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKD_REGISTRY_AUTH_FILE", str(tmp_path / "absent.json"))
    with pytest.raises(RegistryAuthError, match="cannot read"):
        Settings.from_env()


def test_credential_repr_hides_secrets() -> None:
    cred = RegistryCredential("r.example.com", "u", PASSWORD, TOKEN)
    assert PASSWORD not in repr(cred) and TOKEN not in repr(cred)


def test_mask_covers_every_form() -> None:
    cred = RegistryCredential("r.example.com", "ci", PASSWORD)
    text = f"{PASSWORD} {cred.header()} {_b64('ci:' + PASSWORD)}"
    masked = cred.mask(text)
    assert PASSWORD not in masked and cred.header() not in masked
    assert _b64("ci:" + PASSWORD) not in masked


# --- the pull ---------------------------------------------------------------------------------


def _engine(handler: Any) -> DockerEngine:
    engine = DockerEngine()
    engine._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    return engine


def test_pull_sends_the_header_and_only_on_the_pull() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/images/create":
            return httpx.Response(200, content=b'{"status":"Pulling"}\n{"status":"Done"}\n')
        return httpx.Response(200, json={})  # image_exists

    cred = RegistryCredential("registry.example.com", "ci", PASSWORD)
    _engine(handler).pull(f"registry.example.com/stackd/opentofu@{DIGEST}", cred)
    pull = next(r for r in seen if r.url.path == "/images/create")
    assert pull.headers["X-Registry-Auth"] == cred.header()
    assert pull.url.params["fromImage"] == "registry.example.com/stackd/opentofu"
    assert pull.url.params["tag"] == DIGEST
    assert all("X-Registry-Auth" not in r.headers for r in seen if r is not pull)


def test_anonymous_pull_has_no_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"{}\n" if "create" in request.url.path else b"{}")

    _engine(handler).pull(f"alpine@{DIGEST}")
    assert all("X-Registry-Auth" not in r.headers for r in seen)


def test_pull_errors_are_reported_and_masked() -> None:
    cred = RegistryCredential("registry.example.com", "ci", PASSWORD)

    def in_stream(request: httpx.Request) -> httpx.Response:
        msg = json.dumps({"error": f"unauthorized: bad credential {PASSWORD} / {cred.header()}"})
        return httpx.Response(200, content=msg.encode() + b"\n")

    with pytest.raises(DockerError) as exc:
        _engine(in_stream).pull(f"registry.example.com/x@{DIGEST}", cred)
    assert "unauthorized" in str(exc.value)
    assert PASSWORD not in str(exc.value) and cred.header() not in str(exc.value)

    def http_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": f"auth {PASSWORD} rejected"})

    with pytest.raises(DockerError) as exc:
        _engine(http_error).pull(f"registry.example.com/x@{DIGEST}", cred)
    assert "rejected" in str(exc.value) and PASSWORD not in str(exc.value)


def test_runner_pulls_with_the_matching_credential_and_logs_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    image = f"registry.example.com:5000/stackd/opentofu@{DIGEST}"
    path = _config(
        tmp_path, {"auths": {"registry.example.com:5000": {"auth": _b64(f"ci:{PASSWORD}")}}}
    )
    pulls: list[tuple[str, RegistryCredential | None]] = []

    class FakeEngine:
        def __init__(self, docker_host: str | None = None) -> None:
            pass

        def image_exists(self, ref: str) -> bool:
            return False

        def pull(self, ref: str, credential: RegistryCredential | None = None) -> None:
            pulls.append((ref, credential))

    monkeypatch.setenv("STACKD_RUNNER", "docker")
    monkeypatch.setenv("STACKD_RUNNER_IMAGES", json.dumps({"opentofu:1.12.2": image}))
    monkeypatch.setenv("STACKD_REGISTRY_AUTH_FILE", path)
    monkeypatch.setattr(main, "DockerEngine", FakeEngine)
    monkeypatch.setattr(main, "DockerRunner", lambda *a, **k: object())
    settings = Settings.from_env()
    job = {"job_id": "run-1", "environment": {"tool": "opentofu", "tool_version": "1.12.2"}}

    class Ws:
        path = tmp_path

    with caplog.at_level(logging.DEBUG):
        main._make_runner(settings, job, Ws())  # type: ignore[arg-type]
    assert len(pulls) == 1 and pulls[0][0] == image
    cred = pulls[0][1]
    assert cred is not None and _decode_header(cred.header())["username"] == "ci"
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "agent.image_pull" in logged
    assert PASSWORD not in logged and cred.header() not in logged
    assert _b64(f"ci:{PASSWORD}") not in logged


def test_the_auth_file_never_reaches_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path is an agent setting (`STACKD_*`, stripped on the local runner); the docker runner
    passes only proxies and locale. The credential itself only ever travels in the pull header."""
    monkeypatch.setenv("STACKD_REGISTRY_AUTH_FILE", "/run/secrets/registry.json")
    monkeypatch.setenv("DOCKER_CONFIG", "/root/.docker")
    for runner in ("local", "docker"):
        for env in (main._job_environ(runner), main._repo_environ(runner)):
            assert "STACKD_REGISTRY_AUTH_FILE" not in env
    assert "DOCKER_CONFIG" not in main._job_environ("docker")
