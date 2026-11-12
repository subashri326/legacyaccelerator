"""Tests for all parser plugins, generator plugins, vendor wrappers, and golden checks."""

from __future__ import annotations

from pathlib import Path

from accelerator.config.client_config import load_client_config
from accelerator.ir.models import NodeOperator
from accelerator.plugins.collectors.base_sql import parse_ddl_to_catalog
from accelerator.plugins.registry import build_default_registry


def test_all_builtin_plugin_golden_checks() -> None:
    reg = build_default_registry()
    plugins = reg.list_unique()
    assert len(plugins) >= 14
    for plugin in plugins:
        ok, msg = plugin.run_golden_check()
        assert ok, f"Plugin {plugin.manifest.name} failed golden check: {msg}"


def test_ssis_informatica_datastage_parsing() -> None:
    reg = build_default_registry()
    base = Path("examples/acme_retail")

    ssis_bytes = (base / "ssis" / "Load_Customers_And_Orders.dtsx").read_bytes()
    ssis_pkg = reg.get_parser("ssis").parse_artifact("Load_Customers_And_Orders.dtsx", ssis_bytes)
    assert len(ssis_pkg.data_flows) == 1
    assert any(t.task_type == "opaque" for t in ssis_pkg.tasks)
    assert len(ssis_pkg.opaque_items) == 1
    assert ssis_pkg.opaque_items[0].reason_code == "SSIS_SCRIPT_TASK"

    infa_bytes = (base / "informatica" / "wf_orders_export.xml").read_bytes()
    infa_pkg = reg.get_parser("informatica").parse_artifact("wf_orders_export.xml", infa_bytes)
    assert len(infa_pkg.data_flows) == 1
    assert any(n.operator == NodeOperator.EXPRESSION for n in infa_pkg.data_flows[0].nodes)

    dsx_bytes = (base / "datastage" / "ds_product_dim.dsx").read_bytes()
    ds_pkg = reg.get_parser("datastage").parse_artifact("ds_product_dim.dsx", dsx_bytes)
    assert len(ds_pkg.data_flows) == 1
    assert len(ds_pkg.data_flows[0].nodes) == 3
    assert len(ds_pkg.data_flows[0].edges) == 2


def test_generate_across_snowflake_databricks_fabric() -> None:
    reg = build_default_registry()
    cfg, _, _ = load_client_config("examples/acme_retail/client_config.yaml")
    catalog = parse_ddl_to_catalog(
        Path("examples/acme_retail/legacy_ddl.sql").read_text(encoding="utf-8"),
        "sqlserver",
        "tsql",
        "legacy_ddl",
    )
    tables = {t.fqn: t for t in catalog.tables}
    ssis_pkg = reg.get_parser("ssis").parse_artifact(
        "Load_Customers_And_Orders.dtsx",
        Path("examples/acme_retail/ssis/Load_Customers_And_Orders.dtsx").read_bytes(),
    )

    for platform in ("snowflake", "databricks", "fabric"):
        cfg_p = cfg.model_copy(deep=True)
        cfg_p.target.platform = platform  # type: ignore[assignment]
        gen = reg.get_generator(platform)
        ddl = gen.generate_ddl(catalog.tables[0], cfg_p)
        assert ddl.compile_passed, f"{platform} DDL compile failed: {ddl.compile_log}"
        bundle = gen.generate_package(ssis_pkg, tables, cfg_p)
        assert any(f.artifact_kind == "dbt_model" for f in bundle.files)
        assert any(f.artifact_kind in ("task", "pipeline_json") for f in bundle.files)
        assert len(bundle.residues) >= 1
