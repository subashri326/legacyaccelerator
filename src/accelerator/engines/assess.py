"""Stage 2: Assess Engine — parses artifacts to Neutral IR, builds lineage, scores effort, plans waves."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import networkx as nx
from openpyxl import Workbook
from sqlalchemy.orm import Session

from accelerator.config.client_config import ClientConfig
from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.models import (
    ComplexityScore,
    DbColumn,
    DbObject,
    Dependency,
    EffortEstimate,
    EtlComponent,
    EtlJob,
    IrEdge,
    IrExpression,
    IrFlow,
    IrNode,
    IrTask,
    SourceArtifact,
    Wave,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit
from accelerator.engines.state_machine import ObjectState, StateMachine
from accelerator.ir.models import NodeOperator, PackageIR
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class AssessSummary:
    project_id: str
    ir_flows_saved: int
    lineage_edges: int
    total_objects_scored: int
    classification_counts: dict[str, int]
    effort_by_platform: dict[str, float]
    waves_created: int
    report_uri: str
    excel_uri: str


class AssessEngine:
    """Parses raw artifacts into Neutral IR once, then scores, builds lineage, and plans waves."""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        self.registry = registry or get_plugin_registry()
        self.store = store or get_artifact_store()

    def run(
        self,
        db: Session,
        project_id: str,
        actor: str = "system",
        actor_role: str = "migration_lead",
    ) -> AssessSummary:
        with metrics.stage_span("assess", project_id):
            cfg_ver, cfg = get_active_config(db, project_id)

            # 1. Parse all latest ETL jobs & SQL objects into the Neutral IR tables
            ir_packages: dict[str, PackageIR] = {}
            etl_jobs = db.query(EtlJob).filter(EtlJob.project_id == project_id).all()
            db_objects = db.query(DbObject).filter(DbObject.project_id == project_id).all()

            # Build normalized lookup from short table names -> canonical FQN
            fqn_lookup: dict[str, str] = {}
            for obj in db_objects:
                fqn_lookup[obj.fqn.lower()] = obj.fqn
                fqn_lookup[f"{obj.schema_name}.{obj.object_name}".lower()] = obj.fqn
                fqn_lookup[obj.object_name.lower()] = obj.fqn

            for job in etl_jobs:
                if not job.source_artifact_id:
                    continue
                art = (
                    db.query(SourceArtifact)
                    .filter(SourceArtifact.id == job.source_artifact_id)
                    .first()
                )
                if art is None:
                    continue
                raw_bytes = self.store.get_bytes(art.storage_uri)
                parser = self.registry.get_parser(job.source_tool)
                pkg = parser.parse_artifact(art.artifact_key, raw_bytes)
                pkg.id = job.job_name
                pkg.name = job.job_name
                ir_packages[job.job_name] = pkg
                self._persist_ir_and_components(
                    db, project_id, job, None, pkg, actor, art.id, art.content_hash
                )

            for obj in db_objects:
                if obj.object_type in ("VIEW", "PROCEDURE") and obj.definition_sql:
                    dialect_tool = {
                        "sqlserver": "tsql",
                        "oracle": "plsql",
                        "teradata": "bteq",
                        "postgres": "postgres",
                    }.get(cfg.source.database, "tsql")
                    parser = self.registry.get_parser(dialect_tool)
                    pkg = parser.parse_artifact(obj.fqn, obj.definition_sql.encode("utf-8"))
                    pkg.id = obj.fqn
                    pkg.name = obj.fqn
                    ir_packages[obj.fqn] = pkg
                    self._persist_ir_and_components(
                        db,
                        project_id,
                        None,
                        obj,
                        pkg,
                        actor,
                        obj.source_artifact_id,
                        obj.content_hash,
                    )

            # 2. Build cross-tool Lineage Graph (tables, views, procedures, ETL jobs)
            graph = nx.DiGraph()
            for obj in db_objects:
                graph.add_node(obj.fqn, kind=obj.object_type.lower())
            for job in etl_jobs:
                graph.add_node(job.job_name, kind="etl_job")

            edges_to_save: list[tuple[str, str, str, list[dict[str, str]]]] = []

            # FK dependencies between tables
            for obj in db_objects:
                cols = db.query(DbColumn).filter(DbColumn.db_object_id == obj.id).all()
                for col in cols:
                    if col.is_foreign_key and col.fk_target:
                        up_fqn = self._resolve_ref(col.fk_target, fqn_lookup)
                        if up_fqn and up_fqn != obj.fqn:
                            edges_to_save.append(
                                (
                                    up_fqn,
                                    obj.fqn,
                                    "fk",
                                    [{"from": col.column_name, "to": col.fk_target}],
                                )
                            )

            # DataFlow IR source -> job -> target dependencies
            for key, pkg in ir_packages.items():
                is_db_obj = any(o.fqn == key for o in db_objects)
                for df in pkg.data_flows:
                    for node in df.nodes:
                        if not node.table_ref:
                            continue
                        resolved_tbl = self._resolve_ref(node.table_ref, fqn_lookup)
                        if node.operator in (NodeOperator.SOURCE, NodeOperator.LOOKUP):
                            edges_to_save.append((resolved_tbl, key, "reads", []))
                        elif node.operator == NodeOperator.TARGET and (
                            not is_db_obj or resolved_tbl != key
                        ):
                            edges_to_save.append((key, resolved_tbl, "writes", []))

            # Replace project lineage edges idempotently
            db.query(Dependency).filter(Dependency.project_id == project_id).delete()
            seen_edges: set[tuple[str, str, str]] = set()
            for up, down, etype, col_lin in edges_to_save:
                if (up, down, etype) in seen_edges:
                    continue
                seen_edges.add((up, down, etype))
                graph.add_node(up, kind="table")
                graph.add_node(down, kind="etl_job" if down in ir_packages else "table")
                graph.add_edge(up, down, edge_type=etype)
                db.add(
                    Dependency(
                        project_id=project_id,
                        upstream_key=up,
                        downstream_key=down,
                        edge_type=etype,
                        column_lineage=col_lin,
                        created_by=actor,
                    )
                )

            # 3. Complexity scoring + side-by-side effort across Snowflake / Databricks / Fabric
            class_counts = {"auto_convertible": 0, "needs_review": 0, "manual": 0}
            effort_totals = {"snowflake": 0.0, "databricks": 0.0, "fabric": 0.0}

            db.query(ComplexityScore).filter(ComplexityScore.project_id == project_id).delete()
            db.query(EffortEstimate).filter(EffortEstimate.project_id == project_id).delete()

            scored_objects: list[tuple[str, str]] = []
            for obj in db_objects:
                kind = obj.object_type.lower()
                scored_objects.append((obj.fqn, kind))
                score, classification, opaque_cnt, reasons = self._score_object(
                    obj.fqn, kind, obj, ir_packages.get(obj.fqn), cfg
                )
                class_counts[classification] += 1
                score_row = self._save_score_and_effort(
                    db,
                    project_id,
                    obj.fqn,
                    kind,
                    score,
                    classification,
                    opaque_cnt,
                    reasons,
                    cfg,
                    cfg_ver.id,
                    effort_totals,
                    actor,
                )
                StateMachine.advance(
                    db,
                    project_id,
                    obj.fqn,
                    kind,
                    ObjectState.ASSESSED,
                    actor,
                    "complexity_score",
                    score_row.id,
                )

            for job in etl_jobs:
                scored_objects.append((job.job_name, "etl_job"))
                score, classification, opaque_cnt, reasons = self._score_object(
                    job.job_name, "etl_job", None, ir_packages.get(job.job_name), cfg
                )
                class_counts[classification] += 1
                score_row = self._save_score_and_effort(
                    db,
                    project_id,
                    job.job_name,
                    "etl_job",
                    score,
                    classification,
                    opaque_cnt,
                    reasons,
                    cfg,
                    cfg_ver.id,
                    effort_totals,
                    actor,
                )
                StateMachine.advance(
                    db,
                    project_id,
                    job.job_name,
                    "etl_job",
                    ObjectState.ASSESSED,
                    actor,
                    "complexity_score",
                    score_row.id,
                )

            # 4. Dependency-ordered Wave Planning (no wave ever depends on an unmigrated table!)
            waves = self._build_waves(db, project_id, graph, scored_objects, actor)

            # 5. Export JSON + Excel assessment report
            report_payload = {
                "project_id": project_id,
                "config_version": cfg_ver.version_num,
                "classification_counts": class_counts,
                "effort_by_platform_days": {k: round(v, 2) for k, v in effort_totals.items()},
                "waves": waves,
                "lineage_edges": len(seen_edges),
            }
            report_uri = self.store.put_text(
                f"projects/{project_id}/reports/assessment_report.json",
                json.dumps(report_payload, indent=2),
            )
            excel_bytes = self._build_excel_report(db, project_id, report_payload)
            excel_uri = self.store.put_bytes(
                f"projects/{project_id}/reports/assessment_report.xlsx",
                excel_bytes,
            )

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.assess.completed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details=report_payload,
            )

            return AssessSummary(
                project_id=project_id,
                ir_flows_saved=len(ir_packages),
                lineage_edges=len(seen_edges),
                total_objects_scored=len(scored_objects),
                classification_counts=class_counts,
                effort_by_platform={k: round(v, 2) for k, v in effort_totals.items()},
                waves_created=len(waves),
                report_uri=report_uri,
                excel_uri=excel_uri,
            )

    @staticmethod
    def _resolve_ref(raw_ref: str, fqn_lookup: dict[str, str]) -> str:
        clean = raw_ref.replace("[", "").replace("]", "").strip().lower()
        if clean in fqn_lookup:
            return fqn_lookup[clean]
        short = clean.split(".")[-1]
        return fqn_lookup.get(short, raw_ref.replace("[", "").replace("]", "").strip())

    def _persist_ir_and_components(
        self,
        db: Session,
        project_id: str,
        etl_job: EtlJob | None,
        db_obj: DbObject | None,
        pkg: PackageIR,
        actor: str,
        source_artifact_id: str | None = None,
        source_content_hash: str = "",
    ) -> None:
        if etl_job is not None:
            db.query(EtlComponent).filter(EtlComponent.etl_job_id == etl_job.id).delete()
            for df in pkg.data_flows:
                for node in df.nodes:
                    db.add(
                        EtlComponent(
                            etl_job_id=etl_job.id,
                            component_name=node.name,
                            component_type=node.source_component_type or node.operator.value,
                            raw_properties=node.metadata,
                            created_by=actor,
                        )
                    )
            for task in pkg.tasks:
                db.add(
                    EtlComponent(
                        etl_job_id=etl_job.id,
                        component_name=task.name,
                        component_type=task.task_type,
                        raw_properties=task.metadata,
                        created_by=actor,
                    )
                )

        existing_flow = (
            db.query(IrFlow)
            .filter(IrFlow.project_id == project_id, IrFlow.flow_key == pkg.id)
            .first()
        )
        if existing_flow is not None:
            db.delete(existing_flow)
            db.flush()

        ir_flow = IrFlow(
            project_id=project_id,
            etl_job_id=etl_job.id if etl_job else None,
            db_object_id=db_obj.id if db_obj else None,
            flow_key=pkg.id,
            name=pkg.name,
            source_tool=pkg.source_tool,
            source_artifact_id=source_artifact_id,
            source_content_hash=source_content_hash,
            ir_payload=pkg.model_dump(mode="json"),
            created_by=actor,
        )
        db.add(ir_flow)
        db.flush()

        for df in pkg.data_flows:
            for node in df.nodes:
                ir_node = IrNode(
                    ir_flow_id=ir_flow.id,
                    node_key=node.id,
                    name=node.name,
                    operator=node.operator.value,
                    table_ref=node.table_ref,
                    is_opaque=(node.operator == NodeOperator.OPAQUE),
                    opaque_reason=node.opaque.reason_code if node.opaque else None,
                    node_payload=node.model_dump(mode="json"),
                    created_by=actor,
                )
                db.add(ir_node)
                db.flush()
                for expr in node.expressions:
                    db.add(
                        IrExpression(
                            ir_flow_id=ir_flow.id,
                            ir_node_id=ir_node.id,
                            target_column=expr.target_column,
                            original_expr=expr.original_expr,
                            neutral_sql=expr.neutral_sql,
                            ast_json=expr.ast_json,
                            is_opaque=expr.is_opaque,
                            opaque_reason=expr.opaque_reason,
                            created_by=actor,
                        )
                    )
            for edge in df.edges:
                db.add(
                    IrEdge(
                        ir_flow_id=ir_flow.id,
                        source_node_key=edge.source_node_id,
                        target_node_key=edge.target_node_id,
                        branch_name=edge.branch_name,
                        created_by=actor,
                    )
                )

        deps_by_task: dict[str, list[str]] = {}
        for dep in pkg.task_dependencies:
            deps_by_task.setdefault(dep.downstream_task_id, []).append(dep.upstream_task_id)
        for task in pkg.tasks:
            db.add(
                IrTask(
                    ir_flow_id=ir_flow.id,
                    task_key=task.id,
                    name=task.name,
                    task_type=task.task_type,
                    depends_on=deps_by_task.get(task.id, []),
                    task_payload=task.model_dump(mode="json"),
                    created_by=actor,
                )
            )
        db.flush()

    def _score_object(
        self,
        object_key: str,
        kind: str,
        db_obj: DbObject | None,
        pkg: PackageIR | None,
        cfg: ClientConfig,
    ) -> tuple[int, str, int, list[str]]:
        score = 10
        reasons: list[str] = []
        opaque_cnt = 0

        if kind == "table":
            if db_obj and db_obj.is_dead_candidate:
                reasons.append(
                    "Dead object candidate (0 reads in 30d) — confirm archive vs migrate"
                )
            return 10, "auto_convertible", 0, reasons

        if pkg is None:
            return 25, "needs_review", 0, ["No parsed IR flow; SQL review recommended"]

        opaque_cnt = len(pkg.opaque_items)
        for df in pkg.data_flows:
            score += len(df.nodes) * 4
            for n in df.nodes:
                if n.operator == NodeOperator.OPAQUE:
                    score += 35
                    reasons.append(f"Opaque component '{n.name}' ({n.source_component_type})")
                rule = cfg.matching_rule(n.source_component_type, n.name, object_key)
                if rule is not None:
                    score += 40 if rule.action == "flag_manual" else 20
                    opaque_cnt += 1 if rule.action == "flag_manual" else 0
                    reasons.append(f"Client rule: {rule.reason}")
                for expr in n.expressions:
                    if expr.is_opaque:
                        score += 15
                        opaque_cnt += 1
                        reasons.append(f"Unparsed expression in '{n.name}'")

        for t in pkg.tasks:
            if t.task_type == "opaque":
                score += 40
                reasons.append(f"Manual task '{t.name}'")
            t_type = t.opaque.component_type if t.opaque else t.task_type
            rule = cfg.matching_rule(t_type, t.name, object_key)
            if rule is not None:
                score += 40 if rule.action == "flag_manual" else 20
                opaque_cnt += 1 if rule.action == "flag_manual" else 0
                reasons.append(f"Client rule: {rule.reason}")

        if db_obj and obj_has_proc_complexity(db_obj.definition_sql or ""):
            score += 35
            reasons.append("Procedural SQL / dynamic execution / cursor detected")

        if opaque_cnt > 0 or score >= 70:
            classification = "manual"
        elif score >= 35:
            classification = "needs_review"
        else:
            classification = "auto_convertible"
        return score, classification, opaque_cnt, list(dict.fromkeys(reasons))

    def _save_score_and_effort(
        self,
        db: Session,
        project_id: str,
        object_key: str,
        object_kind: str,
        score: int,
        classification: str,
        opaque_cnt: int,
        reasons: list[str],
        cfg: ClientConfig,
        cfg_ver_id: str,
        effort_totals: dict[str, float],
        actor: str,
    ) -> ComplexityScore:
        score_row = ComplexityScore(
            project_id=project_id,
            object_key=object_key,
            object_kind=object_kind,
            score=score,
            classification=classification,
            opaque_count=opaque_cnt,
            reasons=reasons,
            config_version_id=cfg_ver_id,
            created_by=actor,
        )
        db.add(score_row)
        db.flush()
        base_days = {
            "auto_convertible": cfg.effort_rates.auto_convertible_days,
            "needs_review": cfg.effort_rates.needs_review_days,
            "manual": cfg.effort_rates.manual_days,
        }[classification]

        for platform in ("snowflake", "databricks", "fabric"):
            mult = cfg.effort_rates.platform_multipliers.get(platform, 1.0)
            days = round(base_days * mult, 2)
            effort_totals[platform] += days
            db.add(
                EffortEstimate(
                    project_id=project_id,
                    object_key=object_key,
                    target_platform=platform,
                    person_days=days,
                    breakdown={
                        "base_days": base_days,
                        "platform_multiplier": mult,
                        "classification": classification,
                    },
                    created_by=actor,
                )
            )
        return score_row

    def _build_waves(
        self,
        db: Session,
        project_id: str,
        graph: nx.DiGraph,
        scored_objects: list[tuple[str, str]],
        actor: str,
    ) -> list[dict[str, Any]]:
        db.query(Wave).filter(Wave.project_id == project_id).delete()
        db.flush()

        kind_map = dict(scored_objects)
        # Break any cycles safely on a condensation/DAG copy for wave leveling
        dag = nx.DiGraph(graph)
        while not nx.is_directed_acyclic_graph(dag):
            cycle = nx.find_cycle(dag)
            dag.remove_edge(*cycle[-1][:2])

        # Compute longest upstream path depth = wave index (1-based)
        depth: dict[str, int] = {}
        for node in nx.topological_sort(dag):
            preds = list(dag.predecessors(node))
            depth[node] = (max(depth[p] for p in preds) + 1) if preds else 1

        grouped: dict[int, list[tuple[str, str]]] = {}
        for obj_key, kind in scored_objects:
            w_num = depth.get(obj_key, 1)
            grouped.setdefault(w_num, []).append((obj_key, kind))

        waves_out: list[dict[str, Any]] = []
        for idx, w_num in enumerate(sorted(grouped), start=1):
            wave_name = (
                f"Wave {idx} ({'Foundation Tables' if idx == 1 else 'Downstream Logic & Curated'})"
            )
            wave_row = Wave(
                project_id=project_id,
                wave_number=idx,
                name=wave_name,
                status="planned",
                created_by=actor,
            )
            db.add(wave_row)
            db.flush()
            members_list = []
            for order_idx, (obj_key, kind) in enumerate(sorted(grouped[w_num]), start=1):
                db.add(
                    WaveMember(
                        wave_id=wave_row.id,
                        object_key=obj_key,
                        object_kind=kind_map.get(obj_key, kind),
                        topo_order=order_idx,
                        created_by=actor,
                    )
                )
                members_list.append({"object_key": obj_key, "object_kind": kind})
            waves_out.append({"wave_number": idx, "name": wave_name, "members": members_list})
        db.flush()
        return waves_out

    def _build_excel_report(
        self,
        db: Session,
        project_id: str,
        summary: dict[str, Any],
    ) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "Executive Summary"
        ws.append(["Metric", "Value"])
        ws.append(["Project ID", project_id])
        ws.append(["Config Version", summary["config_version"]])
        ws.append(["Lineage Edges", summary["lineage_edges"]])
        for k, v in summary["classification_counts"].items():
            ws.append([f"Objects ({k})", v])
        for p, d in summary["effort_by_platform_days"].items():
            ws.append([f"Total Effort ({p}) Person-Days", d])

        ws_obj = wb.create_sheet("Object Assessment")
        ws_obj.append(
            [
                "Object Key",
                "Kind",
                "Score",
                "Classification",
                "Opaque Items",
                "Snowflake Days",
                "Databricks Days",
                "Fabric Days",
                "Reasons",
            ]
        )
        scores = db.query(ComplexityScore).filter(ComplexityScore.project_id == project_id).all()
        efforts = db.query(EffortEstimate).filter(EffortEstimate.project_id == project_id).all()
        eff_map: dict[tuple[str, str], float] = {
            (e.object_key, e.target_platform): e.person_days for e in efforts
        }
        for s in scores:
            ws_obj.append(
                [
                    s.object_key,
                    s.object_kind,
                    s.score,
                    s.classification,
                    s.opaque_count,
                    eff_map.get((s.object_key, "snowflake"), 0.0),
                    eff_map.get((s.object_key, "databricks"), 0.0),
                    eff_map.get((s.object_key, "fabric"), 0.0),
                    "; ".join(s.reasons or []),
                ]
            )

        ws_waves = wb.create_sheet("Wave Plan")
        ws_waves.append(["Wave Number", "Wave Name", "Object Key", "Kind"])
        for w in summary["waves"]:
            for m in w["members"]:
                ws_waves.append([w["wave_number"], w["name"], m["object_key"], m["object_kind"]])

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()


def obj_has_proc_complexity(sql_text: str) -> bool:
    upper = sql_text.upper()
    return any(tok in upper for tok in ("CURSOR", "SP_EXECUTESQL", "EXECUTE IMMEDIATE", "WHILE "))
