"""Entrypoint of the production API image (api/Dockerfile). Not used in dev.

serve    uvicorn on $PORT (default 8443). Unless STACKD_TLS_SELF_SIGNED=false, it serves TLS
         with a self-signed certificate generated at start (in memory-backed /tmp): the load
         balancer in front does not verify target certificates, but the last hop is encrypted
         like the others. Proxy headers (X-Forwarded-For/-Proto) are trusted only from
         FORWARDED_ALLOW_IPS — the load balancer's subnets — so the audit `ip` and the rate
         limiter see the client, and a client can't forge its address.
migrate  `alembic upgrade head`, then `python -m app.seed --base` (the default space and the
         tier catalog, SPECS §3.0/§2.4). Idempotent: run it before each rollout.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def self_signed_certificate(directory: Path, hostname: str = "stackd-api") -> tuple[Path, Path]:
    """A fresh EC P-256 key and a self-signed certificate valid for a year: (cert, key) paths."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "tls.crt", directory / "tls.key"
    key_path.touch(mode=0o600)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def serve_args() -> list[str]:
    args = [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        os.environ.get("PORT", "8443"),
        "--proxy-headers",
        "--no-server-header",
        # Without FORWARDED_ALLOW_IPS uvicorn trusts 127.0.0.1 only: proxy headers are ignored.
        "--forwarded-allow-ips",
        os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    ]
    if os.environ.get("STACKD_TLS_SELF_SIGNED", "true").lower() != "false":
        cert, key = self_signed_certificate(Path(tempfile.mkdtemp(prefix="stackd-tls-")))
        args += ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
    return args


def main(argv: list[str]) -> None:
    command = argv[0] if argv else "serve"
    if command == "serve":
        args = serve_args()
        os.execvp(args[0], args)
    elif command == "migrate":
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        os.execvp(sys.executable, [sys.executable, "-m", "app.seed", "--base"])
    else:
        sys.exit(f"unknown command {command!r}: expected `serve` or `migrate`")


if __name__ == "__main__":
    main(sys.argv[1:])
