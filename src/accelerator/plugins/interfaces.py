"""Five canonical Plugin SDK interfaces (Section 6.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from accelerator.config.client_config import ClientConfig, ReconOverride
from accelerator.ir.models import CatalogIR, PackageIR, TableIR

PluginKind = Literal["SourceCollector", "Parser", "TargetGenerator", "Mover", "Validator"]


@dataclass(frozen=True)
class PluginManifest:
    plugin_type: PluginKind
    name: str
    version: str
    supported_versions: list[str] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class CollectedArtifact:
    artifact_key: str
    artifact_type: str  # dtsx | informatica_xml | dsx | sql | catalog | usage | scheduler
    source_tool: str
    content_bytes: bytes
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedFile:
    relative_path: str
    artifact_kind: str  # ddl | sql | dbt_model | dbt_schema | pyspark | task | pipeline_json
    content: str
    object_key: str
    compile_passed: bool = True
    compile_log: str = "Syntax check passed"
    # Mapping decisions a person must confirm (e.g. a type with no exact target equivalent).
    review_notes: list[GeneratedResidue] = field(default_factory=list)


@dataclass
class GeneratedResidue:
    object_key: str
    component_name: str
    reason_code: str
    reason_detail: str
    original_snippet: str
    # Stable id of the component in the neutral model (node or task id); approved fixes key on it.
    component_key: str = ""


@dataclass
class GeneratedBundle:
    files: list[GeneratedFile] = field(default_factory=list)
    residues: list[GeneratedResidue] = field(default_factory=list)


@dataclass
class PartitionResult:
    partition_index: int
    range_predicate: str
    parquet_uri: str
    rows_extracted: int
    rows_loaded: int
    checksum: str
    status: str  # completed | failed
    error_message: str | None = None


@dataclass
class MoveOutcome:
    object_key: str
    rows_loaded: int
    mode: str
    watermark_value: str | None
    partitions: list[PartitionResult] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationOutcome:
    object_key: str
    level: str
    verdict: str  # pass | pass_within_tolerance | fail
    source_metrics: dict[str, Any] = field(default_factory=dict)
    target_metrics: dict[str, Any] = field(default_factory=dict)
    diff_summary: dict[str, Any] = field(default_factory=dict)


class BasePlugin(ABC):
    """Base contract for all accelerator plugins."""

    manifest: PluginManifest

    def run_golden_check(self) -> tuple[bool, str]:
        """Execute plugin self-test against golden inputs."""
        return True, f"{self.manifest.name} golden check passed"


class SourceCollector(BasePlugin):
    """Harvest read-only catalogs, ETL code exports, and usage history."""

    @abstractmethod
    def collect_catalog(
        self,
        connection_uri: str,
        schemas: list[str] | None = None,
        catalog_bundle_path: str | None = None,
    ) -> CatalogIR:
        """Collect tables, columns, keys, views, procedures, row counts and sizes."""

    def collect_usage(
        self, connection_uri: str, usage_log_path: str | None = None
    ) -> dict[str, int]:
        """Optional: collect 30-day read counts keyed by lowercase FQN."""
        return {}


class Parser(BasePlugin):
    """Parse legacy SQL or ETL exports into the platform-neutral IR."""

    @abstractmethod
    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        """Turn raw ETL or SQL artifact into a PackageIR."""


class TargetGenerator(BasePlugin):
    """Render neutral CatalogIR / PackageIR into target platform code + compile check."""

    @abstractmethod
    def generate_ddl(self, table: TableIR, config: ClientConfig) -> GeneratedFile:
        """Generate target CREATE TABLE DDL."""

    @abstractmethod
    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        """Generate SQL/dbt/PySpark/orchestration files and collect flagged residues."""

    def target_table(self, table: TableIR, config: ClientConfig) -> TableIR:
        """The table as it is named on the target after naming rules (same column order)."""
        return table

    @abstractmethod
    def compile_check(
        self,
        code: str,
        artifact_kind: str,
        connection_uri: str | None = None,
    ) -> tuple[bool, str]:
        """Dry-run / compile check generated code before advancing to Converted."""


class Mover(BasePlugin):
    """Create target schemas/tables and move data directly source-to-target."""

    @abstractmethod
    def load_table(
        self,
        table: TableIR,
        source_uri: str,
        target_uri: str,
        config: ClientConfig,
        mode: str = "bulk",
        watermark_value: str | None = None,
        resume: bool = False,
        start_partition_index: int = 0,
        staging_writer: Callable[[int, bytes], str] | None = None,
        target_ddl_sql: str | None = None,
        target_table: TableIR | None = None,
        on_partition: Callable[[PartitionResult], None] | None = None,
    ) -> MoveOutcome:
        """Load one table bulk or incrementally, reporting each finished chunk to `on_partition`
        (the engine checkpoints it immediately). With `resume`, continue after what is already
        on the target instead of starting over."""


class Validator(BasePlugin):
    """Run pushed-down L1-L4 and Job-level reconciliation checks."""

    @abstractmethod
    def reconcile_table(
        self,
        table: TableIR,
        rule: ReconOverride,
        source_uri: str,
        target_uri: str,
        config: ClientConfig,
        target_table: TableIR | None = None,
    ) -> ValidationOutcome:
        """Compare source and target tables using pushed-down queries.

        `target_table` is the table as named on the target (after naming rules); its columns are in
        the same order as the source table's. Defaults to the source naming.
        """

    def reconcile_job(
        self,
        package: PackageIR,
        legacy_sql: str,
        target_sql: str,
        source_uri: str,
        target_uri: str,
        numeric_tolerance: float = 0.0,
    ) -> ValidationOutcome:
        """Run legacy vs converted job logic on shared input and diff outputs."""
        raise NotImplementedError
