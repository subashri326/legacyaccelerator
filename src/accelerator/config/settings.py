"""Application runtime settings loaded from environment variables or .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global control-plane settings for the accelerator."""

    model_config = SettingsConfigDict(
        env_prefix="L2C_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Legacy-to-Cloud Data Modernization Accelerator"
    environment: str = "dev"

    # Metadata database (PostgreSQL in prod/Docker, SQLite by default locally)
    database_url: str = "sqlite:///./.l2c_data/accelerator.db"
    db_echo: bool = False

    # Artifact & Parquet staging store
    storage_backend: Literal["local", "s3", "adls"] = "local"
    storage_local_root: Path = Path("./.l2c_data/artifacts")
    s3_bucket: str = ""
    s3_endpoint_url: str | None = None
    adls_account_url: str = ""
    adls_container: str = "l2c-artifacts"

    # Secrets Vault
    vault_backend: Literal["local", "env", "hashicorp"] = "local"
    vault_local_file: Path = Path("./.l2c_data/vault.json")
    hashicorp_vault_addr: str = ""
    hashicorp_vault_token: str = ""

    # Authentication & RBAC
    auth_mode: Literal["local", "oidc"] = "local"
    jwt_secret: str = "dev-insecure-secret-change-for-production-32bytes"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480
    oidc_issuer: str = ""
    oidc_audience: str = ""

    # Optional AI Assist (code-only residue suggestions, client opt-in)
    ai_enabled: bool = False
    ai_endpoint: str = "http://localhost:11434/v1/chat/completions"
    ai_model: str = "llama3.1"
    ai_api_key: str = ""

    # Data governance & observability
    l4_sample_retention_days: int = 7
    otel_service_name: str = "l2c-accelerator"
    default_admin_password: str = "admin123!"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings()
