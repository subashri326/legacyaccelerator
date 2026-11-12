"""Client configuration schema, YAML loader, and deterministic config versioning."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class SourceConfig(BaseModel):
    database: Literal["sqlserver", "oracle", "teradata", "duckdb", "sqlite"]
    etl: Literal["ssis", "informatica", "datastage", "sql", "none"] = "none"
    connection: str = ""
    schemas: list[str] = Field(default_factory=list)
    catalog_bundle_path: str | None = None
    etl_export_paths: list[str] = Field(default_factory=list)
    usage_log_path: str | None = None


class TargetConfig(BaseModel):
    platform: Literal["snowflake", "databricks", "fabric"]
    output: Literal["dbt", "sql", "native"] = "dbt"
    connection: str = ""
    database: str = "MODERN_DW"
    warehouse: str = "COMPUTE_WH"


class NamingConfig(BaseModel):
    schema_template: str = Field(default="{layer}_{source_db}", alias="schema")
    table_case: Literal["upper", "lower", "preserve"] = "upper"
    column_case: Literal["upper", "lower", "preserve"] = "lower"
    layer_default: str = "curated"

    model_config = {"populate_by_name": True}


class TypeOverride(BaseModel):
    source: str
    target: str


class CustomRule(BaseModel):
    match: str
    action: Literal["flag_manual", "flag_review", "auto_convert", "skip"] = "flag_manual"
    reason: str = "Matched client custom rule"
    effort_days_override: float | None = None


class EffortRatesConfig(BaseModel):
    auto_convertible_days: float = 0.25
    needs_review_days: float = 1.5
    manual_days: float = 4.0
    platform_multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "snowflake": 1.0,
            "databricks": 1.15,
            "fabric": 1.1,
        }
    )


class ReconOverride(BaseModel):
    table: str
    level: Literal["L1", "L2", "L3", "L4", "JOB"] = "L2"
    partition_key: str | None = None
    numeric_tolerance: float = 0.0
    date_tolerance_seconds: int = 0
    masked_columns: list[str] = Field(default_factory=list)


class ReconciliationConfig(BaseModel):
    default_level: Literal["L1", "L2", "L3", "L4", "JOB"] = "L2"
    numeric_tolerance: float = 0.0
    l4_sample_limit: int = 100
    l4_retention_days: int = 7
    masked_columns: list[str] = Field(
        default_factory=lambda: ["ssn", "tax_id", "email", "phone", "credit_card", "account_number"]
    )
    overrides: list[ReconOverride] = Field(default_factory=list)

    def get_table_rule(self, table_fqn: str) -> ReconOverride:
        norm = table_fqn.lower()
        for ov in self.overrides:
            if ov.table.lower() == norm or norm.endswith(f".{ov.table.lower()}"):
                return ov
        return ReconOverride(
            table=table_fqn,
            level=self.default_level,
            numeric_tolerance=self.numeric_tolerance,
            masked_columns=list(self.masked_columns),
        )


class MovementConfig(BaseModel):
    mover: Literal["parquet", "airbyte"] = "parquet"
    partition_size_rows: int = 10000
    parallelism: int = 4
    staging_prefix: str = "staging/parquet"
    watermark_columns: dict[str, str] = Field(default_factory=dict)
    airbyte_workspace_id: str = ""
    airbyte_connection_id: str = ""


class AiAssistConfig(BaseModel):
    enabled: bool = False
    endpoint: str | None = None
    model: str | None = None


class GitConfig(BaseModel):
    repo_path: str = ""
    base_branch: str = "main"
    branch_prefix: str = "l2c/wave"
    provider: Literal["github", "azure_devops", "gitlab", "local"] = "local"


class ClientConfig(BaseModel):
    """Canonical client configuration model (Section 6.1)."""

    client: str
    project: str = ""
    environment: str = "dev"
    source: SourceConfig
    target: TargetConfig
    naming: NamingConfig = Field(default_factory=NamingConfig)
    type_overrides: list[TypeOverride] = Field(default_factory=list)
    custom_rules: list[CustomRule] = Field(default_factory=list)
    effort_rates: EffortRatesConfig = Field(default_factory=EffortRatesConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    movement: MovementConfig = Field(default_factory=MovementConfig)
    ai_assist: AiAssistConfig = Field(default_factory=AiAssistConfig)
    git: GitConfig = Field(default_factory=GitConfig)

    @field_validator("project", mode="before")
    @classmethod
    def default_project_name(cls, v: Any, info: Any) -> str:
        if v:
            return str(v)
        client = info.data.get("client", "client")
        return f"{client}_modernization"

    def type_override_map(self) -> dict[str, str]:
        return {item.source.upper(): item.target for item in self.type_overrides}


def parse_client_config_yaml(raw_yaml: str) -> tuple[ClientConfig, str]:
    """Validate raw YAML text and return (ClientConfig, sha256_hex)."""
    data = yaml.safe_load(raw_yaml) or {}
    cfg = ClientConfig.model_validate(data)
    digest = hashlib.sha256(raw_yaml.encode("utf-8")).hexdigest()
    return cfg, digest


def load_client_config(path: str | Path) -> tuple[ClientConfig, str, str]:
    """Load a client YAML file from disk -> (ClientConfig, raw_yaml, sha256_hex)."""
    raw_yaml = Path(path).read_text(encoding="utf-8")
    cfg, digest = parse_client_config_yaml(raw_yaml)
    return cfg, raw_yaml, digest
