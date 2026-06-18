from __future__ import annotations

import base64
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    # Environment
    stackd_env: str = "development"
    stackd_log_format: str = "json"  # structured JSON logs by default; "pretty" for local reading
    stackd_log_level: str = "INFO"  # raise to DEBUG to also capture reads/polls/heartbeats

    # Database
    database_url: str = "postgresql+asyncpg://stackd:stackd@localhost:5432/stackd"

    # Crypto / sessions
    stackd_encryption_key: str = ""  # base64-encoded 32 bytes (AES-256-GCM master key, §1)
    stackd_jwt_secret: str = ""

    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 14 * 24 * 3600

    # Auth
    stackd_dev_auth: bool = False
    stackd_allowed_domains: str = ""  # comma-separated `hd` allowlist
    google_client_id: str = ""
    google_client_secret: str = ""
    stackd_public_url: str = "http://localhost:8000"  # browser/OAuth + OIDC issuer (must be public)
    # Base URL workers use to reach the API (state backend). Defaults to the public URL; in compose
    # set it to the in-network address (http://api:8000) since workers can't resolve localhost.
    stackd_internal_url: str | None = None
    # Base URL of the SPA, used to build deep links in outbound notifications. Defaults to the
    # public URL; in dev the front is served separately (http://localhost:5173).
    stackd_app_url: str | None = None
    # GitHub API base for VCS post-back (§18); override for GitHub Enterprise.
    stackd_github_api_url: str = "https://api.github.com"

    # Object storage
    s3_endpoint_url: str | None = None
    s3_bucket: str = "stackd"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    aws_region: str = "garage"

    # Dev timings
    stackd_heartbeat_interval: int = 20
    stackd_worker_offline_seconds: int = 60
    stackd_worker_lost_seconds: int = 120
    stackd_head_poll_interval: int = 900
    stackd_apply_affinity_seconds: int = 60
    # Drift detection (§19): minimum spacing between read-only drift plans per environment.
    stackd_drift_interval_seconds: int = 21600  # 6h

    # Background scheduler (§7.5) — disabled under tests so it can't fail runs mid-assertion.
    stackd_run_scheduler: bool = True

    # Observability / guardrails (§H).
    stackd_otlp_endpoint: str | None = None  # set → enable OpenTelemetry OTLP export (no-op unset)
    stackd_discovery_max_repo_mb: int = 200  # reject input-discovery clones larger than this
    stackd_discovery_max_tf_files: int = 500  # cap the .tf files parsed during discovery

    @field_validator("stackd_dev_auth")
    @classmethod
    def _no_dev_auth_in_prod(cls, v: bool, info) -> bool:  # type: ignore[no-untyped-def]
        # Hard guard (DEV §3): dev login must never be reachable in production.
        if v and info.data.get("stackd_env") == "production":
            raise ValueError("STACKD_DEV_AUTH cannot be enabled when STACKD_ENV=production")
        return v

    @property
    def is_production(self) -> bool:
        return self.stackd_env == "production"

    @property
    def internal_url(self) -> str:
        return self.stackd_internal_url or self.stackd_public_url

    @property
    def allowed_domains(self) -> set[str]:
        return {d.strip() for d in self.stackd_allowed_domains.split(",") if d.strip()}

    @property
    def encryption_key_bytes(self) -> bytes:
        key = base64.b64decode(self.stackd_encryption_key)
        if len(key) != 32:
            raise ValueError("STACKD_ENCRYPTION_KEY must decode to 32 bytes (AES-256)")
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
