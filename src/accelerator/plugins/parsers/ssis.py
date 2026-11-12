"""SSIS (.dtsx) Parser plugin turning Control Flow & Data Flow XML into Neutral IR."""

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
    ParameterIR,
    TaskDependencyIR,
    VariableIR,
)
from accelerator.plugins.interfaces import Parser, PluginManifest

DTS_NS = "www.microsoft.com/SqlServer/Dts"
SQLTASK_NS = "www.microsoft.com/sqlserver/dts/tasks/sqltask"

SSIS_COMPONENT_MAP: dict[str, NodeOperator] = {
    "microsoft.oledbsource": NodeOperator.SOURCE,
    "microsoft.adonetdestination": NodeOperator.TARGET,
    "microsoft.oledbdestination": NodeOperator.TARGET,
    "microsoft.adonetSource": NodeOperator.SOURCE,
    "microsoft.derivedcolumn": NodeOperator.EXPRESSION,
    "microsoft.conditionalsplit": NodeOperator.ROUTER,
    "microsoft.lookup": NodeOperator.LOOKUP,
    "microsoft.mergejoin": NodeOperator.JOIN,
    "microsoft.aggregate": NodeOperator.AGGREGATE,
    "microsoft.sort": NodeOperator.SORT,
    "microsoft.unionall": NodeOperator.UNION,
    "microsoft.filter": NodeOperator.FILTER,
}


def _attr(elem: etree._Element, local_name: str, default: str = "") -> str:
    """Read DTS-namespaced or plain attribute cleanly."""
    for key in (f"{{{DTS_NS}}}{local_name}", local_name):
        if key in elem.attrib:
            return elem.attrib[key]
    for child in elem:
        tag_local = etree.QName(child).localname
        if tag_local == "Property" and child.attrib.get(f"{{{DTS_NS}}}Name") == local_name:
            return (child.text or "").strip()
    return default


def _prop_dict(elem: etree._Element) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in elem.xpath(".//properties/property"):
        name = prop.attrib.get("name") or prop.attrib.get("Name") or ""
        if name:
            props[name] = (prop.text or "").strip()
    return props


def _classify_ssis_component(class_id: str, props: dict[str, str]) -> NodeOperator:
    low = class_id.lower()
    if "scriptcomponent" in low or "scripttask" in low:
        return NodeOperator.OPAQUE
    if "sort" in low and props.get("EliminateDuplicates", "").lower() == "true":
        return NodeOperator.DEDUPLICATE
    for key, op in SSIS_COMPONENT_MAP.items():
        if key.lower() in low:
            return op
    if "source" in low:
        return NodeOperator.SOURCE
    if "destination" in low:
        return NodeOperator.TARGET
    return NodeOperator.OPAQUE


class SsisParser(Parser):
    """Parse SSIS .dtsx XML into the three-layer Neutral IR."""

    manifest = PluginManifest(
        plugin_type="Parser",
        name="ssis_parser",
        version="0.1.0",
        supported_versions=["SSIS 2012+", "SSIS 2016/2019/2022"],
        capabilities={
            "control_flow": True,
            "data_flow": True,
            "expressions": True,
            "script_task_opaque": True,
        },
        description="Parses SSIS .dtsx packages into Neutral IR without silent drops.",
    )

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        root = etree.fromstring(content_bytes)
        pkg_name = _attr(root, "ObjectName") or artifact_name.removesuffix(".dtsx")

        variables: list[VariableIR] = []
        for var_el in root.xpath(".//*[local-name()='Variable']"):
            v_name = _attr(var_el, "ObjectName")
            if v_name:
                val_els = var_el.xpath("./*[local-name()='VariableValue']")
                val = (val_els[0].text or "") if val_els else None
                variables.append(
                    VariableIR(
                        name=v_name,
                        namespace=_attr(var_el, "Namespace", "User"),
                        default_value=val,
                    )
                )

        parameters: list[ParameterIR] = []
        for param_el in root.xpath(".//*[local-name()='PackageParameter']"):
            p_name = _attr(param_el, "ObjectName")
            if p_name:
                parameters.append(
                    ParameterIR(name=p_name, required=_attr(param_el, "Required") == "1")
                )

        data_flows: list[DataFlowGraphIR] = []
        tasks: list[OrchestrationTaskIR] = []
        task_deps: list[TaskDependencyIR] = []
        opaque_items: list[OpaqueSnippetIR] = []

        self._walk_executables(
            parent_el=root,
            artifact_name=artifact_name,
            data_flows=data_flows,
            tasks=tasks,
            task_deps=task_deps,
            opaque_items=opaque_items,
        )

        return PackageIR(
            id=pkg_name,
            name=pkg_name,
            source_tool="ssis",
            source_artifact_name=artifact_name,
            data_flows=data_flows,
            tasks=tasks,
            task_dependencies=task_deps,
            variables=variables,
            parameters=parameters,
            opaque_items=opaque_items,
        )

    def _walk_executables(
        self,
        parent_el: etree._Element,
        artifact_name: str,
        data_flows: list[DataFlowGraphIR],
        tasks: list[OrchestrationTaskIR],
        task_deps: list[TaskDependencyIR],
        opaque_items: list[OpaqueSnippetIR],
    ) -> None:
        exec_containers = parent_el.xpath(
            "./*[local-name()='Executables']/*[local-name()='Executable']"
        )
        for idx, ex in enumerate(exec_containers, start=1):
            ex_name = _attr(ex, "ObjectName") or f"Task_{idx}"
            ex_id = _attr(ex, "DTSID") or _attr(ex, "refId") or ex_name
            ex_type = _attr(ex, "ExecutableType") or ""
            low_type = ex_type.lower()

            if "pipeline" in low_type:
                df_id = f"{ex_name}_df"
                df, df_opaque = self._parse_pipeline(ex, df_id, ex_name, artifact_name)
                data_flows.append(df)
                opaque_items.extend(df_opaque)
                tasks.append(
                    OrchestrationTaskIR(
                        id=ex_id,
                        name=ex_name,
                        task_type="data_flow",
                        data_flow_id=df_id,
                    )
                )
            elif "executesql" in low_type:
                sql_data = ex.xpath(".//*[local-name()='SqlTaskData']")
                sql_stmt = ""
                if sql_data:
                    sql_stmt = (
                        sql_data[0].attrib.get(f"{{{SQLTASK_NS}}}SqlStatementSource")
                        or sql_data[0].attrib.get("SqlStatementSource")
                        or ""
                    )
                tasks.append(
                    OrchestrationTaskIR(
                        id=ex_id,
                        name=ex_name,
                        task_type="execute_sql",
                        sql_statement=sql_stmt,
                    )
                )
            elif "scripttask" in low_type:
                snippet = etree.tostring(ex, encoding="unicode")[:2500]
                opaque = OpaqueSnippetIR(
                    node_id=ex_id,
                    component_name=ex_name,
                    component_type="ScriptTask",
                    reason_code="SSIS_SCRIPT_TASK",
                    reason_detail="SSIS ScriptTask contains custom .NET C#/VB code requiring manual or AI review.",
                    original_snippet=snippet,
                    location=f"{artifact_name}/{ex_name}",
                )
                opaque_items.append(opaque)
                tasks.append(
                    OrchestrationTaskIR(
                        id=ex_id,
                        name=ex_name,
                        task_type="opaque",
                        opaque=opaque,
                    )
                )
            elif "sequence" in low_type or "forloop" in low_type or "foreach" in low_type:
                child_tasks: list[OrchestrationTaskIR] = []
                self._walk_executables(
                    ex, artifact_name, data_flows, child_tasks, task_deps, opaque_items
                )
                tasks.append(
                    OrchestrationTaskIR(
                        id=ex_id,
                        name=ex_name,
                        task_type="container",
                        children=child_tasks,
                    )
                )
            else:
                snippet = etree.tostring(ex, encoding="unicode")[:1500]
                opaque = OpaqueSnippetIR(
                    node_id=ex_id,
                    component_name=ex_name,
                    component_type=ex_type or "UnknownTask",
                    reason_code="UNSUPPORTED_SSIS_TASK",
                    reason_detail=f"Unmapped SSIS executable type '{ex_type}'.",
                    original_snippet=snippet,
                    location=f"{artifact_name}/{ex_name}",
                )
                opaque_items.append(opaque)
                tasks.append(
                    OrchestrationTaskIR(
                        id=ex_id,
                        name=ex_name,
                        task_type="opaque",
                        opaque=opaque,
                    )
                )

        for pc in parent_el.xpath(
            "./*[local-name()='PrecedenceConstraints']/*[local-name()='PrecedenceConstraint']"
        ):
            up = _attr(pc, "From")
            down = _attr(pc, "To")
            expr = _attr(pc, "Expression") or None
            val = _attr(pc, "Value", "0")
            cond_type = "success" if val == "0" else ("failure" if val == "1" else "completion")
            if expr:
                cond_type = "expression"
            if up and down:
                task_deps.append(
                    TaskDependencyIR(
                        upstream_task_id=up,
                        downstream_task_id=down,
                        condition_type=cond_type,
                        condition_expr=expr,
                    )
                )

    def _parse_pipeline(
        self,
        pipeline_exec: etree._Element,
        df_id: str,
        df_name: str,
        artifact_name: str,
    ) -> tuple[DataFlowGraphIR, list[OpaqueSnippetIR]]:
        nodes: list[NodeIR] = []
        edges: list[EdgeIR] = []
        opaque_items: list[OpaqueSnippetIR] = []
        output_to_node: dict[str, str] = {}
        input_to_node: dict[str, str] = {}
        output_branch_name: dict[str, str] = {}

        for comp in pipeline_exec.xpath(".//pipeline/components/component"):
            c_id = (
                comp.attrib.get("refId") or comp.attrib.get("id") or comp.attrib.get("name", "comp")
            )
            c_name = comp.attrib.get("name") or c_id
            class_id = comp.attrib.get("componentClassID") or ""
            props = _prop_dict(comp)
            operator = _classify_ssis_component(class_id, props)

            table_ref = props.get("OpenRowset") or props.get("TableName") or None
            if table_ref:
                table_ref = table_ref.replace("[", "").replace("]", "")
            sql_cmd = props.get("SqlCommand") or props.get("SqlCommandParam") or None

            expressions: list[ExpressionIR] = []
            routes: list[dict[str, str]] = []
            out_cols: list[ColumnIR] = []
            opaque_item: OpaqueSnippetIR | None = None

            for out_el in comp.xpath("./outputs/output"):
                out_ref = out_el.attrib.get("refId") or out_el.attrib.get("id") or ""
                out_name = out_el.attrib.get("name") or "default"
                is_err = out_el.attrib.get("isErrorOut", "false").lower() == "true"
                if out_ref:
                    output_to_node[out_ref] = c_id
                    output_branch_name[out_ref] = out_name

                out_props = _prop_dict(out_el)
                if (
                    operator == NodeOperator.ROUTER
                    and not is_err
                    and (out_props.get("FriendlyExpression") or out_props.get("Expression"))
                ):
                    raw_cond = out_props.get("FriendlyExpression") or out_props.get(
                        "Expression", ""
                    )
                    expr_ir = ExpressionConverter.to_neutral(f"{c_id}_{out_name}", raw_cond, "ssis")
                    expressions.append(expr_ir)
                    routes.append({"name": out_name, "condition": expr_ir.neutral_sql or raw_cond})

                for col_el in out_el.xpath("./outputColumns/outputColumn"):
                    col_name = col_el.attrib.get("name") or ""
                    col_dtype = col_el.attrib.get("dataType") or "str"
                    if col_name and not is_err:
                        out_cols.append(ColumnIR(name=col_name, data_type=col_dtype))
                    c_props = _prop_dict(col_el)
                    raw_expr = c_props.get("FriendlyExpression") or c_props.get("Expression")
                    if raw_expr:
                        expr_ir = ExpressionConverter.to_neutral(
                            f"{c_id}_{col_name}", raw_expr, "ssis", target_column=col_name
                        )
                        expressions.append(expr_ir)

            for in_el in comp.xpath("./inputs/input"):
                in_ref = in_el.attrib.get("refId") or in_el.attrib.get("id") or ""
                if in_ref:
                    input_to_node[in_ref] = c_id

            if operator == NodeOperator.OPAQUE:
                opaque_item = OpaqueSnippetIR(
                    node_id=c_id,
                    component_name=c_name,
                    component_type=class_id or "UnknownPipelineComponent",
                    reason_code="SSIS_OPAQUE_COMPONENT",
                    reason_detail=f"SSIS component '{class_id}' requires custom translation/review.",
                    original_snippet=etree.tostring(comp, encoding="unicode")[:2500],
                    location=f"{artifact_name}/{df_name}/{c_name}",
                )
                opaque_items.append(opaque_item)

            nodes.append(
                NodeIR(
                    id=c_id,
                    name=c_name,
                    operator=operator,
                    source_component_type=class_id,
                    table_ref=table_ref,
                    sql_query=sql_cmd,
                    expressions=expressions,
                    routes=routes,
                    output_columns=out_cols,
                    opaque=opaque_item,
                    metadata={"properties": props},
                )
            )

        for p_idx, path_el in enumerate(pipeline_exec.xpath(".//pipeline/paths/path"), start=1):
            start_id = path_el.attrib.get("startId") or ""
            end_id = path_el.attrib.get("endId") or ""
            src_node = output_to_node.get(start_id)
            tgt_node = input_to_node.get(end_id)
            if src_node and tgt_node:
                edges.append(
                    EdgeIR(
                        id=path_el.attrib.get("refId") or f"path_{p_idx}",
                        source_node_id=src_node,
                        target_node_id=tgt_node,
                        branch_name=output_branch_name.get(start_id, "default"),
                    )
                )

        return DataFlowGraphIR(id=df_id, name=df_name, nodes=nodes, edges=edges), opaque_items

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"""<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="GoldenPkg">
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="Microsoft.Pipeline" DTS:ObjectName="DF_Load" DTS:DTSID="{1}">
      <DTS:ObjectData>
        <pipeline>
          <components>
            <component refId="src1" name="Src" componentClassID="Microsoft.OLEDBSource">
              <properties><property name="OpenRowset">[dbo].[src_tbl]</property></properties>
              <outputs><output refId="src1_out" name="Out"/></outputs>
            </component>
            <component refId="tgt1" name="Tgt" componentClassID="Microsoft.OLEDBDestination">
              <properties><property name="OpenRowset">[dbo].[tgt_tbl]</property></properties>
              <inputs><input refId="tgt1_in" name="In"/></inputs>
            </component>
          </components>
          <paths><path refId="p1" startId="src1_out" endId="tgt1_in"/></paths>
        </pipeline>
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>"""
        pkg = self.parse_artifact("golden.dtsx", sample)
        ok = (
            pkg.name == "GoldenPkg"
            and len(pkg.data_flows) == 1
            and len(pkg.data_flows[0].nodes) == 2
            and len(pkg.data_flows[0].edges) == 1
        )
        return ok, "SsisParser golden check passed" if ok else "Failed golden check"
