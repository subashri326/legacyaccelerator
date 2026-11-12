"""Client configuration schema, YAML loader, and deterministic config versioning."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from accelerator.core.exceptions import ConfigError

EtlTool = Literal["ssis", "informatica", "datastage", "sql", "none"]
TargetPlatform = Literal["snowflake", "databricks", "fabric"]


class SourceConfig(BaseModel):
    database: Literal["sqlserver", "oracle", "teradata", "postgres", "duckdb", "sqlite"]
    # `etl` accepts one tool or a list; `etl_tools` always holds the full list and `etl` the primary one.
    etl: EtlTool = "none"
    etl_tools: list[EtlTool] = Field(default_factory=list)
    connection: str = ""
    schemas: list[str] = Field(default_factory=list)
    catalog_bundle_path: str | None = None
    etl_export_paths: list[str] = Field(default_factory=list)
    usage_log_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_etl(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        etl = data.get("etl")
        if isinstance(etl, list):
            data["etl_tools"] = list(etl)
            data["etl"] = etl[0] if etl else "none"
        elif etl and not data.get("etl_tools"):
            data["etl_tools"] = [] if etl == "none" else [etl]
        return data


class TargetConfig(BaseModel):
    # Leave empty while the client is still comparing targets; Assess still runs for all three.
    platform: TargetPlatform | None = None
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


_RULE_CLAUSE = re.compile(
    r"^(component_type|component_name|object_key)\s*(==|!=|contains|startswith|endswith)\s*'([^']*)'$"
)


class CustomRule(BaseModel):
    """A client rule such as `component_type == 'ScriptTask'`.

    Grammar: clauses `<field> <op> '<value>'` joined by `and` / `or` (and binds tighter).
    Fields: component_type, component_name, object_key. Ops: ==, !=, contains, startswith, endswith.
    Matching is case-insensitive; component_type also matches its last dotted part
    (so 'DerivedColumn' matches 'Microsoft.DerivedColumn').
    """

    match: str
    action: Literal["flag_manual", "flag_review", "auto_convert", "skip"] = "flag_manual"
    reason: str = "Matched client custom rule"
    effort_days_override: float | None = None

    @field_validator("match")
    @classmethod
    def _check_grammar(cls, v: str) -> str:
        for group in re.split(r"\s+or\s+", v.strip()):
            for clause in re.split(r"\s+and\s+", group.strip()):
                if not _RULE_CLAUSE.match(clause.strip()):
                    raise ValueError(
                        f"Invalid rule clause {clause!r}; expected e.g. component_type == 'ScriptTask'"
                    )
        return v

    def matches(
        self, component_type: str = "", component_name: str = "", object_key: str = ""
    ) -> bool:
        values = {
            "component_type": component_type,
            "component_name": component_name,
            "object_key": object_key,
        }

        def clause_ok(clause: str) -> bool:
            m = _RULE_CLAUSE.match(clause.strip())
            assert m is not None
            field_name, op, want = m.groups()
            have = (values[field_name] or "").lower()
            want = want.lower()
            candidates = [have]
            if field_name == "component_type" and "." in have:
                candidates.append(have.rsplit(".", 1)[-1])
            if not have:
                return op == "!="
            if op == "==":
                return want in candidates
            if op == "!=":
                return want not in candidates
            if op == "contains":
                return any(want in c for c in candidates)
            if op == "startswith":
                return any(c.startswith(want) for c in candidates)
            return any(c.endswith(want) for c in candidates)

        return any(
            all(clause_ok(c) for c in re.split(r"\s+and\s+", group.strip()))
            for group in re.split(r"\s+or\s+", self.match.strip())
        )


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
    target: TargetConfig = Field(default_factory=TargetConfig)
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

    def require_target(self) -> str:
        """Return the chosen target platform, or fail clearly if the client hasn't chosen yet."""
        if not self.target.platform:
            raise ConfigError(
                "No target platform chosen yet (project is in 'comparing'). "
                "Set target.platform in the client config before Convert, Move or Reconcile."
            )
        return self.target.platform

    def matching_rule(
        self, component_type: str = "", component_name: str = "", object_key: str = ""
    ) -> CustomRule | None:
        """First flag_manual / flag_review rule that matches a component, if any."""
        for rule in self.custom_rules:
            if rule.action in ("flag_manual", "flag_review") and rule.matches(
                component_type, component_name, object_key
            ):
                return rule
        return None

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
