"""The production image's entrypoint (api/docker/entrypoint.py): TLS on the task port and proxy
headers trusted from FORWARDED_ALLOW_IPS only."""

from __future__ import annotations

import importlib.util
import ssl
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.py"


def _entrypoint() -> ModuleType:
    # Loaded by path: a `docker` package name would collide with the Docker SDK.
    spec = importlib.util.spec_from_file_location("stackd_prod_entrypoint", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_signed_certificate_loads_into_a_tls_context(tmp_path: Path) -> None:
    cert, key = _entrypoint().self_signed_certificate(tmp_path)
    assert key.stat().st_mode & 0o777 == 0o600
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)  # raises if the pair does not match


def test_serve_args_trust_proxy_headers_from_configured_ranges_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.10.0/24,10.0.11.0/24")
    monkeypatch.setenv("STACKD_TLS_SELF_SIGNED", "false")
    args = _entrypoint().serve_args()
    assert "--proxy-headers" in args
    assert args[args.index("--forwarded-allow-ips") + 1] == "10.0.10.0/24,10.0.11.0/24"
    assert "--ssl-certfile" not in args


def test_serve_args_default_to_tls_and_no_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("STACKD_TLS_SELF_SIGNED", raising=False)
    args = _entrypoint().serve_args()
    assert args[args.index("--forwarded-allow-ips") + 1] == "127.0.0.1"
    assert Path(args[args.index("--ssl-certfile") + 1]).is_file()
    assert Path(args[args.index("--ssl-keyfile") + 1]).is_file()
