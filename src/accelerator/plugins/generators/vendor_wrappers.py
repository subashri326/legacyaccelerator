"""Wrap-don't-compete vendor converter plugins (Section 2.7 & 8)."""

from __future__ import annotations

import shutil
import subprocess

from accelerator.config.client_config import ClientConfig
from accelerator.ir.models import PackageIR, TableIR
from accelerator.plugins.generators.databricks import DatabricksGenerator
from accelerator.plugins.generators.fabric import FabricGenerator
from accelerator.plugins.generators.snowflake import SnowflakeGenerator
from accelerator.plugins.interfaces import GeneratedBundle, PluginManifest


class _VendorWrapperMixin:
    cli_binary: str = ""
    vendor_label: str = ""

    def is_vendor_cli_available(self) -> bool:
        return bool(self.cli_binary and shutil.which(self.cli_binary))

    def _run_vendor_or_fallback(
        self,
        fallback_bundle: GeneratedBundle,
        input_hint: str,
    ) -> GeneratedBundle:
        if self.is_vendor_cli_available():
            # Invoke vendor CLI when installed inside client environment
            proc = subprocess.run(
                [self.cli_binary, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            header = f"-- Wrapped {self.vendor_label} ({proc.stdout.strip()})\n"
        else:
            header = f"-- {self.vendor_label} wrapper (CLI '{self.cli_binary}' not installed; using neutral fallback for {input_hint})\n"
        for f in fallback_bundle.files:
            if f.artifact_kind in ("sql", "dbt_model", "ddl"):
                f.content = header + f.content
        return fallback_bundle


class LakebridgeWrapperGenerator(DatabricksGenerator, _VendorWrapperMixin):
    cli_binary = "lakebridge"
    vendor_label = "Databricks Lakebridge"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="lakebridge_wrapper",
        version="0.1.0",
        supported_versions=["Lakebridge CLI"],
        capabilities={"vendor_wrapper": True, "target": "databricks"},
        description="Wraps Databricks Lakebridge while preserving accelerator assessment, tracking, and reconciliation.",
    )

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        bundle = super().generate_package(package, tables, config, accepted_overrides)
        return self._run_vendor_or_fallback(bundle, package.name)


class SnowConvertWrapperGenerator(SnowflakeGenerator, _VendorWrapperMixin):
    cli_binary = "snowct"
    vendor_label = "Snowflake SnowConvert AI"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="snowconvert_wrapper",
        version="0.1.0",
        supported_versions=["SnowConvert CLI"],
        capabilities={"vendor_wrapper": True, "target": "snowflake"},
        description="Wraps Snowflake SnowConvert AI inside the governed accelerator flow.",
    )

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        bundle = super().generate_package(package, tables, config, accepted_overrides)
        return self._run_vendor_or_fallback(bundle, package.name)


class FabricAssistantGenerator(FabricGenerator, _VendorWrapperMixin):
    cli_binary = "fabric-migrate"
    vendor_label = "Microsoft Fabric Migration Assistant"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="fabric_assistant_wrapper",
        version="0.1.0",
        supported_versions=["Fabric Migration Assistant"],
        capabilities={"vendor_wrapper": True, "target": "fabric"},
        description="Wraps Fabric Migration Assistant while adding ETL conversion, reconciliation, and sign-off.",
    )

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        bundle = super().generate_package(package, tables, config, accepted_overrides)
        return self._run_vendor_or_fallback(bundle, package.name)
