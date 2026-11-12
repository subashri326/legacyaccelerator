"""Central plugin registry with built-in plugins, entry-point discovery, and DB sync."""

from __future__ import annotations

from importlib import metadata
from typing import TypeVar

from sqlalchemy.orm import Session

from accelerator.core.exceptions import PluginNotFoundError
from accelerator.db.models import PluginRegistryEntry
from accelerator.plugins.interfaces import (
    BasePlugin,
    Mover,
    Parser,
    PluginKind,
    SourceCollector,
    TargetGenerator,
    Validator,
)

TPlugin = TypeVar("TPlugin", bound=BasePlugin)


class PluginRegistry:
    """Thread-safe catalog of registered SourceCollector, Parser, TargetGenerator, Mover, Validator."""

    def __init__(self) -> None:
        self._plugins: dict[tuple[PluginKind, str], BasePlugin] = {}

    def register(self, plugin: BasePlugin, aliases: list[str] | None = None) -> None:
        kind = plugin.manifest.plugin_type
        self._plugins[(kind, plugin.manifest.name.lower())] = plugin
        for alias in aliases or []:
            self._plugins[(kind, alias.lower())] = plugin

    def get(self, kind: PluginKind, name: str) -> BasePlugin:
        key = (kind, name.lower())
        if key not in self._plugins:
            available = sorted(n for (k, n) in self._plugins if k == kind)
            raise PluginNotFoundError(
                f"No {kind} plugin registered for '{name}'. Available: {available}"
            )
        return self._plugins[key]

    def get_collector(self, name: str) -> SourceCollector:
        return self.get("SourceCollector", name)  # type: ignore[return-value]

    def get_parser(self, name: str) -> Parser:
        return self.get("Parser", name)  # type: ignore[return-value]

    def get_generator(self, name: str) -> TargetGenerator:
        return self.get("TargetGenerator", name)  # type: ignore[return-value]

    def get_mover(self, name: str) -> Mover:
        return self.get("Mover", name)  # type: ignore[return-value]

    def get_validator(self, name: str) -> Validator:
        return self.get("Validator", name)  # type: ignore[return-value]

    def list_unique(self) -> list[BasePlugin]:
        seen: dict[str, BasePlugin] = {}
        for plugin in self._plugins.values():
            seen[f"{plugin.manifest.plugin_type}:{plugin.manifest.name}"] = plugin
        return list(seen.values())

    def discover_entrypoints(self) -> None:
        """Load third-party plugins advertised via pyproject entry_points(group='accelerator.plugins')."""
        eps = metadata.entry_points()
        group = eps.select(group="accelerator.plugins") if hasattr(eps, "select") else []
        for ep in group:
            plugin_cls = ep.load()
            instance = plugin_cls() if isinstance(plugin_cls, type) else plugin_cls
            self.register(instance)

    def sync_to_db(self, db: Session) -> list[PluginRegistryEntry]:
        entries: list[PluginRegistryEntry] = []
        for plugin in self.list_unique():
            m = plugin.manifest
            row = db.query(PluginRegistryEntry).filter(PluginRegistryEntry.name == m.name).first()
            if row is None:
                row = PluginRegistryEntry(
                    plugin_type=m.plugin_type,
                    name=m.name,
                    version=m.version,
                    capabilities=m.capabilities,
                    enabled=True,
                )
                db.add(row)
            else:
                row.plugin_type = m.plugin_type
                row.version = m.version
                row.capabilities = m.capabilities
            entries.append(row)
        db.flush()
        return entries


_default_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """Return initialized global PluginRegistry with all built-in + entrypoint plugins."""
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry


def build_default_registry() -> PluginRegistry:
    from accelerator.plugins.collectors.oracle import OracleCollector
    from accelerator.plugins.collectors.sqlserver import PostgresCollector, SqlServerCollector
    from accelerator.plugins.collectors.teradata import TeradataCollector
    from accelerator.plugins.generators.databricks import DatabricksGenerator
    from accelerator.plugins.generators.fabric import FabricGenerator
    from accelerator.plugins.generators.snowflake import SnowflakeGenerator
    from accelerator.plugins.generators.vendor_wrappers import (
        FabricAssistantGenerator,
        LakebridgeWrapperGenerator,
        SnowConvertWrapperGenerator,
    )
    from accelerator.plugins.movers.airbyte_mover import AirbyteMover
    from accelerator.plugins.movers.parquet_mover import ParquetMover
    from accelerator.plugins.parsers.datastage import DataStageParser
    from accelerator.plugins.parsers.informatica import InformaticaParser
    from accelerator.plugins.parsers.sql_dialect import (
        BteqParser,
        PgSqlParser,
        PlSqlParser,
        TSqlParser,
    )
    from accelerator.plugins.parsers.ssis import SsisParser
    from accelerator.plugins.validators.sql_validator import SqlPushdownValidator

    reg = PluginRegistry()

    # Collectors
    reg.register(SqlServerCollector(), aliases=["sqlserver", "duckdb", "sqlite"])
    reg.register(OracleCollector(), aliases=["oracle"])
    reg.register(TeradataCollector(), aliases=["teradata"])
    reg.register(PostgresCollector(), aliases=["postgres", "postgresql"])

    # Parsers
    reg.register(TSqlParser(), aliases=["tsql", "sqlserver", "sql"])
    reg.register(PlSqlParser(), aliases=["plsql", "oracle"])
    reg.register(BteqParser(), aliases=["bteq", "teradata"])
    reg.register(PgSqlParser(), aliases=["postgres", "postgresql"])
    reg.register(SsisParser(), aliases=["ssis", "dtsx"])
    reg.register(InformaticaParser(), aliases=["informatica", "powercenter"])
    reg.register(DataStageParser(), aliases=["datastage", "dsx", "isx"])

    # Target Generators + Vendor Wrappers
    reg.register(SnowflakeGenerator(), aliases=["snowflake"])
    reg.register(DatabricksGenerator(), aliases=["databricks"])
    reg.register(FabricGenerator(), aliases=["fabric"])
    reg.register(LakebridgeWrapperGenerator())
    reg.register(SnowConvertWrapperGenerator())
    reg.register(FabricAssistantGenerator())

    # Movers
    reg.register(ParquetMover(), aliases=["parquet", "builtin"])
    reg.register(AirbyteMover(), aliases=["airbyte"])

    # Validators
    reg.register(
        SqlPushdownValidator(),
        aliases=["sql_validator", "snowflake", "databricks", "fabric", "duckdb", "sqlite"],
    )

    reg.discover_entrypoints()
    return reg
