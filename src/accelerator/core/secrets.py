"""Secrets Vault abstraction supporting local vault, env vars, and HashiCorp Vault."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import SecretsError


class SecretsVault(ABC):
    """Resolve credential URIs without storing secrets in configs or metadata DB."""

    @abstractmethod
    def get_secret(self, reference: str) -> str:
        """Retrieve secret string for a URI like vault://acme/sqlserver-ro."""

    @abstractmethod
    def set_secret(self, reference: str, value: str) -> None:
        """Store or update a secret in the configured backend."""


class LocalSecretsVault(SecretsVault):
    """Local JSON-backed secret store for development and offline assessments."""

    def __init__(self, file_path: Path | None = None) -> None:
        self.file_path = file_path or get_settings().vault_local_file

    def _load(self) -> dict[str, str]:
        if not self.file_path.exists():
            return {}
        return json.loads(self.file_path.read_text(encoding="utf-8"))

    def get_secret(self, reference: str) -> str:
        key = reference.removeprefix("vault://")
        data = self._load()
        if key in data:
            return data[key]
        env_key = "L2C_SECRET_" + key.replace("/", "_").replace("-", "_").upper()
        if env_key in os.environ:
            return os.environ[env_key]
        raise SecretsError(f"Secret '{reference}' not found in local vault or env '{env_key}'")

    def set_secret(self, reference: str, value: str) -> None:
        key = reference.removeprefix("vault://")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._load()
        data[key] = value
        self.file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class EnvSecretsVault(SecretsVault):
    """Environment-variable-only secret resolver."""

    def get_secret(self, reference: str) -> str:
        key = reference.removeprefix("vault://").removeprefix("env://")
        env_key = "L2C_SECRET_" + key.replace("/", "_").replace("-", "_").upper()
        if env_key in os.environ:
            return os.environ[env_key]
        if key in os.environ:
            return os.environ[key]
        raise SecretsError(f"Environment secret '{env_key}' not set for '{reference}'")

    def set_secret(self, reference: str, value: str) -> None:
        key = reference.removeprefix("vault://").removeprefix("env://")
        env_key = "L2C_SECRET_" + key.replace("/", "_").replace("-", "_").upper()
        os.environ[env_key] = value


class HashiCorpSecretsVault(SecretsVault):
    """HashiCorp Vault KV v2 client."""

    def __init__(self, addr: str | None = None, token: str | None = None) -> None:
        settings = get_settings()
        self.addr = (addr or settings.hashicorp_vault_addr).rstrip("/")
        self.token = token or settings.hashicorp_vault_token
        if not self.addr or not self.token:
            raise SecretsError("HashiCorp Vault requires L2C_HASHICORP_VAULT_ADDR and TOKEN")

    def get_secret(self, reference: str) -> str:
        path = reference.removeprefix("vault://")
        url = f"{self.addr}/v1/secret/data/{path}"
        resp = httpx.get(url, headers={"X-Vault-Token": self.token}, timeout=10.0)
        if resp.status_code != 200:
            raise SecretsError(f"Vault lookup failed ({resp.status_code}) for '{reference}'")
        payload = resp.json()
        data = payload.get("data", {}).get("data", {})
        if "dsn" in data:
            return str(data["dsn"])
        if "value" in data:
            return str(data["value"])
        return json.dumps(data)

    def set_secret(self, reference: str, value: str) -> None:
        path = reference.removeprefix("vault://")
        url = f"{self.addr}/v1/secret/data/{path}"
        resp = httpx.post(
            url,
            headers={"X-Vault-Token": self.token},
            json={"data": {"value": value}},
            timeout=10.0,
        )
        if resp.status_code not in (200, 204):
            raise SecretsError(f"Vault write failed ({resp.status_code}) for '{reference}'")


def get_vault() -> SecretsVault:
    backend = get_settings().vault_backend
    if backend == "env":
        return EnvSecretsVault()
    if backend == "hashicorp":
        return HashiCorpSecretsVault()
    return LocalSecretsVault()


def resolve_connection_uri(reference: str) -> str:
    """Resolve vault:// or env:// references; pass direct local URIs through safely."""
    if reference.startswith(("vault://", "env://")):
        return get_vault().get_secret(reference)
    return reference
