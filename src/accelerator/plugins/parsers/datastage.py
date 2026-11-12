"""IBM DataStage (.dsx / .isx) Parser plugin producing Neutral IR."""

from __future__ import annotations

import re

from accelerator.ir.expressions import ExpressionConverter
from accelerator.ir.models import (
    DataFlowGraphIR,
    EdgeIR,
    NodeIR,
    NodeOperator,
    OpaqueSnippetIR,
    PackageIR,
)
from accelerator.plugins.interfaces import Parser, PluginManifest

DS_STAGE_MAP: dict[str, NodeOperator] = {
    "pxodbcconnector": NodeOperator.SOURCE,
    "odbcconnectorpx": NodeOperator.SOURCE,
    "pxsequentialfile": NodeOperator.SOURCE,
    "oracleconnectorpx": NodeOperator.SOURCE,
    "ctransformerstage": NodeOperator.EXPRESSION,
    "pxfilter": NodeOperator.FILTER,
    "pxlookup": NodeOperator.LOOKUP,
    "pxjoin": NodeOperator.JOIN,
    "pxaggregator": NodeOperator.AGGREGATE,
    "pxfunnel": NodeOperator.UNION,
    "pxsort": NodeOperator.SORT,
    "pxremdup": NodeOperator.DEDUPLICATE,
}


class DataStageParser(Parser):
    """Parse IBM InfoSphere DataStage .dsx text exports into Neutral IR."""

    manifest = PluginManifest(
        plugin_type="Parser",
        name="datastage_parser",
        version="0.1.0",
        supported_versions=["DataStage 11.5", "DataStage 11.7"],
        capabilities={"parallel_jobs": True, "transformers": True},
        description="Parses DataStage .dsx job exports into Neutral IR.",
    )

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        text = content_bytes.decode("utf-8", errors="replace")
        job_match = re.search(r'BEGIN\s+DSJOB.*?Identifier\s+"([^"]+)"', text, re.DOTALL | re.IGNORECASE)
        job_name = job_match.group(1) if job_match else artifact_name.removesuffix(".dsx")

        records = re.findall(r"BEGIN\s+DSRECORD(.*?)END\s+DSRECORD", text, re.DOTALL | re.IGNORECASE)
        nodes: list[NodeIR] = []
        edges: list[EdgeIR] = []
        opaque_items: list[OpaqueSnippetIR] = []
        stage_names: set[str] = set()
        link_endpoints: dict[str, dict[str, str]] = {}

        for rec in records:
            id_m = re.search(r'Identifier\s+"([^"]+)"', rec)
            ole_m = re.search(r'OLEType\s+"([^"]+)"', rec)
            name_m = re.search(r'Name\s+"([^"]+)"', rec)
            stype_m = re.search(r'StageType\s+"([^"]+)"', rec)
            table_m = re.search(r'TableName\s+"([^"]+)"', rec)
            deriv_matches = re.findall(r'Derivation\s+"([^"]+)"', rec)
            ole_type = (ole_m.group(1) if ole_m else "").lower()
            rec_name = name_m.group(1) if name_m else (id_m.group(1) if id_m else "stage")

            if "stage" in ole_type and stype_m:
                stype = stype_m.group(1)
                low_s = stype.lower()
                operator = DS_STAGE_MAP.get(low_s, NodeOperator.OPAQUE)
                if (
                    "target" in rec_name.lower() or "tgt" in rec_name.lower() or "out" in rec_name.lower()
                ) and operator == NodeOperator.SOURCE:
                    operator = NodeOperator.TARGET
                if "custom" in low_s or "routine" in low_s:
                    operator = NodeOperator.OPAQUE

                expressions = [
                    ExpressionConverter.to_neutral(f"{rec_name}_d{i}", d, "datastage")
                    for i, d in enumerate(deriv_matches, start=1)
                    if d and not d.startswith("DSLink")
                ]

                opaque_item: OpaqueSnippetIR | None = None
                if operator == NodeOperator.OPAQUE:
                    opaque_item = OpaqueSnippetIR(
                        node_id=rec_name,
                        component_name=rec_name,
                        component_type=stype,
                        reason_code="DATASTAGE_OPAQUE_STAGE",
                        reason_detail=f"DataStage stage '{stype}' requires manual translation/review.",
                        original_snippet=rec.strip()[:2000],
                        location=f"{artifact_name}/{rec_name}",
                    )
                    opaque_items.append(opaque_item)

                stage_names.add(rec_name)
                nodes.append(
                    NodeIR(
                        id=rec_name,
                        name=rec_name,
                        operator=operator,
                        source_component_type=stype,
                        table_ref=table_m.group(1) if table_m else None,
                        expressions=expressions,
                        opaque=opaque_item,
                    )
                )

                out_pins = re.findall(r'OutputPins\s+"([^"]+)"', rec)
                in_pins = re.findall(r'InputPins\s+"([^"]+)"', rec)
                for p in out_pins:
                    for pin in p.split("|"):
                        link_endpoints.setdefault(pin, {})["from"] = rec_name
                for p in in_pins:
                    for pin in p.split("|"):
                        link_endpoints.setdefault(pin, {})["to"] = rec_name

        # Extract link connections via Partner fields in pin/output/input records
        for rec in records:
            id_m = re.search(r'Identifier\s+"([^"]+)"', rec)
            partner_m = re.search(r'Partner\s+"([^"]+)"', rec)
            if id_m and partner_m:
                p1 = id_m.group(1)
                p2 = partner_m.group(1).split("|")[0]
                src = link_endpoints.get(p1, {}).get("from")
                tgt = link_endpoints.get(p2, {}).get("to")
                if src and tgt and src in stage_names and tgt in stage_names:
                    edges.append(EdgeIR(id=f"{src}_{tgt}_{p1}", source_node_id=src, target_node_id=tgt))

        # Deduplicate edges
        unique_edges = {(e.source_node_id, e.target_node_id): e for e in edges}

        df = DataFlowGraphIR(
            id=job_name,
            name=job_name,
            nodes=nodes,
            edges=list(unique_edges.values()),
        )
        return PackageIR(
            id=job_name,
            name=job_name,
            source_tool="datastage",
            source_artifact_name=artifact_name,
            data_flows=[df] if nodes else [],
            opaque_items=opaque_items,
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"""BEGIN DSJOB
   Identifier "ds_product_dim"
   BEGIN DSRECORD
      Identifier "V0S1"
      OLEType "CCustomStage"
      Name "Src_Products"
      StageType "ODBCConnectorPX"
      TableName "dbo.products"
      OutputPins "V0S1P1"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S2"
      OLEType "CTransformerStage"
      Name "Xfm_Clean"
      StageType "CTransformerStage"
      InputPins "V0S2P1"
      OutputPins "V0S2P2"
      Derivation "NullToValue(name, 'UNKNOWN')"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S3"
      OLEType "CCustomStage"
      Name "Tgt_Products"
      StageType "ODBCConnectorPX"
      TableName "dim_products"
      InputPins "V0S3P1"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S1P1"
      OLEType "CCustomOutput"
      Partner "V0S2P1"
   END DSRECORD
   BEGIN DSRECORD
      Identifier "V0S2P2"
      OLEType "CCustomOutput"
      Partner "V0S3P1"
   END DSRECORD
END DSJOB"""
        pkg = self.parse_artifact("ds_product_dim.dsx", sample)
        ok = len(pkg.data_flows) == 1 and len(pkg.data_flows[0].nodes) == 3 and len(pkg.data_flows[0].edges) == 2
        return ok, "DataStageParser golden check passed" if ok else "Failed golden check"
