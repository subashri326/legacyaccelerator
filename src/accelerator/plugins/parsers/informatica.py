"""Informatica PowerCenter XML Parser plugin producing Neutral IR."""

from __future__ import annotations

from lxml import etree

from accelerator.ir.expressions import ExpressionConverter
from accelerator.ir.models import (
    ColumnIR,
    DataFlowGraphIR,
    EdgeIR,
    ExpressionIR,
    NodeIR,
    NodeOperator,
    OpaqueSnippetIR,
    OrchestrationTaskIR,
    PackageIR,
    TaskDependencyIR,
)
from accelerator.plugins.interfaces import Parser, PluginManifest

INFA_TYPE_MAP: dict[str, NodeOperator] = {
    "source definition": NodeOperator.SOURCE,
    "source qualifier": NodeOperator.SOURCE,
    "filter": NodeOperator.FILTER,
    "expression": NodeOperator.EXPRESSION,
    "lookup procedure": NodeOperator.LOOKUP,
    "joiner": NodeOperator.JOIN,
    "aggregator": NodeOperator.AGGREGATE,
    "router": NodeOperator.ROUTER,
    "union": NodeOperator.UNION,
    "sorter": NodeOperator.SORT,
    "target definition": NodeOperator.TARGET,
}


class InformaticaParser(Parser):
    """Parse Informatica PowerCenter XML exports (Mappings + Workflows) into Neutral IR."""

    manifest = PluginManifest(
        plugin_type="Parser",
        name="informatica_parser",
        version="0.1.0",
        supported_versions=["PowerCenter 9.x", "PowerCenter 10.x"],
        capabilities={"mappings": True, "workflows": True, "expressions": True},
        description="Parses Informatica PowerCenter XML into Neutral IR.",
    )

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        root = etree.fromstring(content_bytes)
        mappings = root.xpath(".//MAPPING")
        workflows = root.xpath(".//WORKFLOW")

        pkg_name = (
            workflows[0].attrib.get("NAME")
            if workflows
            else (mappings[0].attrib.get("NAME") if mappings else artifact_name.removesuffix(".xml"))
        )

        data_flows: list[DataFlowGraphIR] = []
        opaque_items: list[OpaqueSnippetIR] = []
        tasks: list[OrchestrationTaskIR] = []
        task_deps: list[TaskDependencyIR] = []

        # Collect source/target metadata tables by name
        table_attrs = {
            s.attrib.get("NAME", ""): s
            for s in root.xpath(".//SOURCE | .//TARGET")
        }

        for m_idx, mapping in enumerate(mappings, start=1):
            m_name = mapping.attrib.get("NAME") or f"m_flow_{m_idx}"
            nodes: list[NodeIR] = []
            edges: list[EdgeIR] = []
            seen_instances: dict[str, NodeIR] = {}

            # Map transformation templates inside mapping
            trans_defs = {t.attrib.get("NAME", ""): t for t in mapping.xpath("./TRANSFORMATION")}

            for inst in mapping.xpath("./INSTANCE"):
                inst_name = inst.attrib.get("NAME") or ""
                inst_type = (inst.attrib.get("TRANSFORMATION_TYPE") or inst.attrib.get("TYPE") or "").lower()
                t_name = inst.attrib.get("TRANSFORMATION_NAME") or inst_name
                operator = INFA_TYPE_MAP.get(inst_type, NodeOperator.OPAQUE)

                t_def = trans_defs.get(t_name)
                expressions: list[ExpressionIR] = []
                out_cols: list[ColumnIR] = []
                sql_query: str | None = None
                condition_ir: ExpressionIR | None = None
                table_ref: str | None = None

                if inst_type in ("source", "source definition", "target", "target definition"):
                    table_ref = t_name
                    if t_name in table_attrs:
                        for f in table_attrs[t_name].xpath("./SOURCEFIELD | ./TARGETFIELD"):
                            out_cols.append(
                                ColumnIR(
                                    name=f.attrib.get("NAME", ""),
                                    data_type=f.attrib.get("DATATYPE", "string"),
                                )
                            )

                if t_def is not None:
                    for attr in t_def.xpath("./TABLEATTRIBUTE"):
                        a_name = (attr.attrib.get("NAME") or "").lower()
                        a_val = attr.attrib.get("VALUE") or ""
                        if "sql query" in a_name and a_val:
                            sql_query = a_val
                        elif "filter condition" in a_name and a_val:
                            condition_ir = ExpressionConverter.to_neutral(
                                f"{inst_name}_flt", a_val, "informatica"
                            )
                        elif "lookup table name" in a_name and a_val:
                            table_ref = a_val

                    for tf in t_def.xpath("./TRANSFORMFIELD"):
                        f_name = tf.attrib.get("NAME", "")
                        f_type = tf.attrib.get("DATATYPE", "string")
                        f_expr = tf.attrib.get("EXPRESSION")
                        if f_name:
                            out_cols.append(ColumnIR(name=f_name, data_type=f_type))
                        if f_expr and f_expr != f_name:
                            expressions.append(
                                ExpressionConverter.to_neutral(
                                    f"{inst_name}_{f_name}", f_expr, "informatica", target_column=f_name
                                )
                            )

                opaque_item: OpaqueSnippetIR | None = None
                if operator == NodeOperator.OPAQUE:
                    snippet = etree.tostring(t_def if t_def is not None else inst, encoding="unicode")[:2000]
                    opaque_item = OpaqueSnippetIR(
                        node_id=inst_name,
                        component_name=inst_name,
                        component_type=inst_type or "UnknownTransformation",
                        reason_code="INFA_OPAQUE_TRANSFORMATION",
                        reason_detail=f"Informatica transformation '{inst_type}' requires manual review.",
                        original_snippet=snippet,
                        location=f"{artifact_name}/{m_name}/{inst_name}",
                    )
                    opaque_items.append(opaque_item)

                node = NodeIR(
                    id=inst_name,
                    name=inst_name,
                    operator=operator,
                    source_component_type=inst_type,
                    table_ref=table_ref,
                    sql_query=sql_query,
                    condition=condition_ir,
                    expressions=expressions,
                    output_columns=out_cols,
                    opaque=opaque_item,
                )
                seen_instances[inst_name] = node
                nodes.append(node)

            # Deduplicate connector edges at node level while keeping column lineage
            edge_map: dict[tuple[str, str], EdgeIR] = {}
            for conn in mapping.xpath("./CONNECTOR"):
                from_inst = conn.attrib.get("FROMINSTANCE", "")
                to_inst = conn.attrib.get("TOINSTANCE", "")
                if from_inst in seen_instances and to_inst in seen_instances:
                    key = (from_inst, to_inst)
                    if key not in edge_map:
                        edge_map[key] = EdgeIR(
                            id=f"{from_inst}_to_{to_inst}",
                            source_node_id=from_inst,
                            target_node_id=to_inst,
                        )
                    edge_map[key].column_lineage.append(
                        {
                            "from": conn.attrib.get("FROMFIELD", ""),
                            "to": conn.attrib.get("TOFIELD", ""),
                        }
                    )
            edges.extend(edge_map.values())
            data_flows.append(DataFlowGraphIR(id=m_name, name=m_name, nodes=nodes, edges=edges))

        for wf in workflows:
            for ti in wf.xpath("./TASKINSTANCE"):
                ti_name = ti.attrib.get("NAME", "")
                t_type = (ti.attrib.get("TASKTYPE") or "").lower()
                mapped_type = "data_flow" if "session" in t_type else "execute_sql"
                df_ref = ti.attrib.get("TASKNAME")
                if mapped_type == "data_flow" and data_flows:
                    df_ref = data_flows[0].id
                tasks.append(
                    OrchestrationTaskIR(
                        id=ti_name,
                        name=ti_name,
                        task_type=mapped_type,
                        data_flow_id=df_ref if mapped_type == "data_flow" else None,
                    )
                )
            for link in wf.xpath("./WORKFLOWLINK"):
                up = link.attrib.get("FROMTASK", "")
                down = link.attrib.get("TOTASK", "")
                cond = link.attrib.get("CONDITION") or None
                if up and down:
                    task_deps.append(
                        TaskDependencyIR(
                            upstream_task_id=up,
                            downstream_task_id=down,
                            condition_type="expression" if cond else "success",
                            condition_expr=cond,
                        )
                    )

        return PackageIR(
            id=pkg_name,
            name=pkg_name,
            source_tool="informatica",
            source_artifact_name=artifact_name,
            data_flows=data_flows,
            tasks=tasks,
            task_dependencies=task_deps,
            opaque_items=opaque_items,
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"""<?xml version="1.0"?>
<POWERMART><REPOSITORY><FOLDER NAME="FIN">
  <SOURCE NAME="SRC_ORD"><SOURCEFIELD NAME="ID" DATATYPE="integer"/></SOURCE>
  <TARGET NAME="TGT_ORD"><TARGETFIELD NAME="ID" DATATYPE="integer"/></TARGET>
  <MAPPING NAME="m_ord">
    <TRANSFORMATION NAME="EXP_CALC" TYPE="Expression">
      <TRANSFORMFIELD NAME="NET" DATATYPE="decimal" EXPRESSION="IIF(ISNULL(AMT), 0, AMT)"/>
    </TRANSFORMATION>
    <INSTANCE NAME="SRC_ORD" TRANSFORMATION_NAME="SRC_ORD" TYPE="SOURCE" TRANSFORMATION_TYPE="Source Definition"/>
    <INSTANCE NAME="EXP_CALC" TRANSFORMATION_NAME="EXP_CALC" TYPE="TRANSFORMATION" TRANSFORMATION_TYPE="Expression"/>
    <INSTANCE NAME="TGT_ORD" TRANSFORMATION_NAME="TGT_ORD" TYPE="TARGET" TRANSFORMATION_TYPE="Target Definition"/>
    <CONNECTOR FROMINSTANCE="SRC_ORD" FROMFIELD="ID" TOINSTANCE="EXP_CALC" TOFIELD="ID"/>
    <CONNECTOR FROMINSTANCE="EXP_CALC" FROMFIELD="ID" TOINSTANCE="TGT_ORD" TOFIELD="ID"/>
  </MAPPING>
</FOLDER></REPOSITORY></POWERMART>"""
        pkg = self.parse_artifact("golden_infa.xml", sample)
        ok = len(pkg.data_flows) == 1 and len(pkg.data_flows[0].nodes) == 3 and len(pkg.data_flows[0].edges) == 2
        return ok, "InformaticaParser golden check passed" if ok else "Failed golden check"
