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

    infa_bytes = (base / "informatica" / "wf_acme_orders.xml").read_bytes()
    infa_pkg = reg.get_parser("informatica").parse_artifact("wf_acme_orders.xml", infa_bytes)
    # one flow per mapping, plus a separate flow where a session overrides the source filter
    assert {f.id for f in infa_pkg.data_flows} == {
        "m_order_bands", "m_order_products@s_m_order_products", "m_customer_union",
        "m_order_status",
    }  # fmt: skip
    assert any(n.operator == NodeOperator.EXPRESSION for n in infa_pkg.data_flows[0].nodes)
    sessions = {t.id: t.data_flow_id for t in infa_pkg.tasks if t.task_type == "data_flow"}
    assert sessions["s_m_customer_union"] == "m_customer_union"  # each session -> its own mapping

    dsx_bytes = (base / "datastage" / "acme_jobs.dsx").read_bytes()
    ds_pkg = reg.get_parser("datastage").parse_artifact("acme_jobs.dsx", dsx_bytes)
    assert {f.id for f in ds_pkg.data_flows} == {
        "px_customer_orders",
        "px_order_products",
        "px_order_status",
    }
    # the sequence job runs two jobs then a command; the third job still gets its own task
    flows = {t.data_flow_id for t in ds_pkg.tasks if t.task_type == "data_flow"}
    assert flows == {"px_customer_orders", "px_order_products", "px_order_status"}
    assert any(
        (d.upstream_task_id, d.downstream_task_id) == ("Run_Customer_Orders", "Run_Order_Products")
        for d in ds_pkg.task_dependencies
    )


def test_datastage_xml_export_reads_like_dsx() -> None:
    """The XML export (DSExport) gives the same flow as the text .dsx export."""
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<DSExport><Header CharacterSet="CP1252" ExportingTool="IBM InfoSphere DataStage Export"/>
<Job Identifier="px_xml" DateModified="2026-10-28">
 <Record Identifier="ROOT" Type="JobDefn"><Property Name="Name">px_xml</Property>
  <Property Name="JobType">3</Property></Record>
 <Record Identifier="V0S0" Type="CustomStage"><Property Name="Name">Src</Property>
  <Property Name="StageType">ODBCConnectorPX</Property><Property Name="OutputPins">V0S0P1</Property>
  <Collection Name="Properties" Type="CustomProperty"><SubRecord>
   <Property Name="Name">XMLProperties</Property>
   <Property Name="Value" PreFormatted="1">&lt;?xml version='1.0' encoding='UTF-16'?&gt;&lt;Properties&gt;&lt;Usage&gt;&lt;GenerateSQL&gt;1&lt;/GenerateSQL&gt;&lt;TableName&gt;dbo.products&lt;/TableName&gt;&lt;/Usage&gt;&lt;/Properties&gt;</Property>
  </SubRecord></Collection></Record>
 <Record Identifier="V0S0P1" Type="CustomOutput"><Property Name="Name">lnk</Property>
  <Property Name="Partner">V0S1|V0S1P1</Property>
  <Collection Name="Columns" Type="OutputColumn">
   <SubRecord><Property Name="Name">sku</Property><Property Name="SqlType">12</Property></SubRecord>
  </Collection></Record>
 <Record Identifier="V0S1" Type="CustomStage"><Property Name="Name">Tgt</Property>
  <Property Name="StageType">ODBCConnectorPX</Property><Property Name="InputPins">V0S1P1</Property>
  <Collection Name="Properties" Type="CustomProperty"><SubRecord>
   <Property Name="Name">XMLProperties</Property>
   <Property Name="Value">&lt;Properties&gt;&lt;Usage&gt;&lt;WriteMode&gt;0&lt;/WriteMode&gt;&lt;TableName&gt;dbo.skus&lt;/TableName&gt;&lt;/Usage&gt;&lt;/Properties&gt;</Property>
  </SubRecord></Collection></Record>
 <Record Identifier="V0S1P1" Type="CustomInput"><Property Name="Name">lnk</Property>
  <Property Name="Partner">V0S0|V0S0P1</Property>
  <Collection Name="Columns" Type="OutputColumn">
   <SubRecord><Property Name="Name">sku</Property><Property Name="SqlType">12</Property></SubRecord>
  </Collection></Record>
</Job></DSExport>"""
    pkg = build_default_registry().get_parser("datastage").parse_artifact("px_xml.xml", xml)
    df = pkg.data_flows[0]
    assert [n.operator for n in df.nodes] == [NodeOperator.SOURCE, NodeOperator.TARGET]
    assert df.nodes[0].sql_query == "SELECT sku FROM dbo.products"
    assert df.nodes[1].table_ref == "dbo.skus" and not pkg.opaque_items


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
