"""Python-native multi-page Web UI for the Legacy-to-Cloud Data Modernization Accelerator."""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import AcceleratorError
from accelerator.core.paths import save_upload
from accelerator.core.secrets import get_vault
from accelerator.core.security import Permission, has_permission, verify_password
from accelerator.core.storage import get_artifact_store
from accelerator.db.models import (
    AuditLog,
    ComplexityScore,
    DbObject,
    Dependency,
    EffortEstimate,
    EtlJob,
    GeneratedArtifact,
    IrFlow,
    LoadRun,
    ObjectStatus,
    PluginRegistryEntry,
    Project,
    ProjectMember,
    ReconResult,
    ResidueItem,
    Signoff,
    User,
    Wave,
    WaveMember,
)
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import init_db, session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.engines.track import TrackEngine
from accelerator.orchestration.runner import RunOrchestrator
from accelerator.plugins.registry import get_plugin_registry

st.set_page_config(
    page_title="L2C Data Modernization Accelerator",
    page_icon="☁️",
    layout="wide",
)


def _bootstrap() -> None:
    init_db()
    with session_scope() as db:
        get_plugin_registry().sync_to_db(db)


def _seed_acme_demo() -> str:
    """Seed the runnable Acme Retail SQL Server + SSIS + Informatica + DataStage demo estate."""
    root = Path(__file__).resolve().parents[3]
    example_dir = root / "examples" / "acme_retail"
    seed_script = example_dir / "seed_legacy_db.py"
    if seed_script.exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("seed_legacy_db", seed_script)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.seed_all(base_dir=Path(".l2c_data/demo_acme"))

    vault = get_vault()
    vault.set_secret(
        "vault://acme/sqlserver-ro", "duckdb:///.l2c_data/demo_acme/legacy_acme.duckdb"
    )
    vault.set_secret(
        "vault://acme/snowflake-deploy", "duckdb:///.l2c_data/demo_acme/target_cloud.duckdb"
    )

    cfg_path = example_dir / "client_config.yaml"
    raw_yaml = cfg_path.read_text(encoding="utf-8")
    with session_scope() as db:
        project, _, _ = ensure_project_from_config(
            db, raw_yaml, actor="ui_demo", actor_role="admin"
        )
        for u in db.query(User).filter(User.role_name != "admin").all():  # demo personas join it
            if not db.query(ProjectMember).filter_by(project_id=project.id, user_id=u.id).first():
                db.add(
                    ProjectMember(
                        project_id=project.id,
                        user_id=u.id,
                        role_name=u.role_name,
                        created_by="ui_demo",
                    )
                )
        return project.id


def _render_lineage_plotly(nodes: list[str], edges: list[tuple[str, str, str]]) -> go.Figure:
    g = nx.DiGraph()
    for n in nodes:
        g.add_node(n)
    for u, v, t in edges:
        g.add_edge(u, v, edge_type=t)
    pos = nx.spring_layout(g, seed=42) if len(g.nodes) > 0 else {}

    edge_x, edge_y = [], []
    for u, v in g.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])

    edge_trace = go.Scatter(
        x=edge_x,
        y=edge_y,
        line={"width": 1.5, "color": "#94a3b8"},
        hoverinfo="none",
        mode="lines",
    )
    node_x = [pos[n][0] for n in g.nodes()]
    node_y = [pos[n][1] for n in g.nodes()]
    node_trace = go.Scatter(
        x=node_x,
        y=node_y,
        mode="markers+text",
        text=list(g.nodes()),
        textposition="top center",
        marker={"size": 16, "color": "#2563eb", "line": {"width": 2, "color": "#ffffff"}},
        hoverinfo="text",
    )
    fig = go.Figure(
        data=[edge_trace, node_trace],
        layout=go.Layout(
            title="Cross-Tool Dependency & Lineage Graph",
            showlegend=False,
            hovermode="closest",
            margin={"b": 20, "l": 5, "r": 5, "t": 40},
            xaxis={"showgrid": False, "zeroline": False, "showticklabels": False},
            yaxis={"showgrid": False, "zeroline": False, "showticklabels": False},
            height=430,
        ),
    )
    return fig


def main() -> None:
    _bootstrap()
    st.sidebar.title("L2C Accelerator")
    st.sidebar.caption("Platform-Neutral · Metadata-Driven")

    # Authentication: in dev the demo admin is pre-selected; elsewhere a real sign-in is required.
    settings = get_settings()
    if "auth_user" not in st.session_state and settings.is_dev:
        st.session_state["auth_user"] = {"username": "admin", "role": "admin"}

    with st.sidebar.expander("Account & RBAC Persona", expanded=False):
        with session_scope() as db:
            users = db.query(User).order_by(User.username).all()
            user_opts = {
                f"{u.username} ({u.role_name})": (u.username, u.role_name, u.password_hash)
                for u in users
            }
        selected_label = st.selectbox("Persona", list(user_opts.keys()))
        pwd = st.text_input(
            "Password", value="admin123!" if settings.is_dev else "", type="password"
        )
        if st.button("Sign In"):
            uname, rname, phash = user_opts[selected_label]
            if verify_password(pwd, phash):
                st.session_state["auth_user"] = {"username": uname, "role": rname}
                st.success(f"Signed in as {uname} ({rname})")
            else:
                st.error("Invalid password")

    if "auth_user" not in st.session_state:
        st.title("Legacy-to-Cloud Data Modernization Accelerator")
        st.info("Sign in from the sidebar to continue.")
        return
    actor = st.session_state["auth_user"]["username"]
    role = st.session_state["auth_user"]["role"]
    st.sidebar.info(f"Signed in: **{actor}** (`{role}`)")

    if settings.is_dev and st.sidebar.button("Load Acme Retail Demo Estate", type="primary"):
        pid = _seed_acme_demo()
        st.session_state["selected_project_id"] = pid
        st.rerun()

    with session_scope() as db:
        q = db.query(Project).order_by(Project.created_at.desc())
        if role != "admin":  # same rule as the API: members only
            user_row = db.query(User).filter(User.username == actor).first()
            member_of = [
                m.project_id
                for m in db.query(ProjectMember).filter(
                    ProjectMember.user_id == (user_row.id if user_row else "")
                )
            ]
            q = q.filter(Project.id.in_(member_of))
        projects = q.all()
        proj_map = {f"{p.name} [{p.source_platform} → {p.target_platform}]": p.id for p in projects}

    if not proj_map:
        st.title("Legacy-to-Cloud Data Modernization Accelerator")
        st.warning(
            "No projects registered yet. Click **Load Acme Retail Demo Estate** in the sidebar or register a client YAML below."
        )
        raw_yaml = st.text_area("Paste Client Config YAML", height=240)
        if st.button("Register Config YAML") and raw_yaml.strip():
            with session_scope() as db:
                proj, _, _ = ensure_project_from_config(db, raw_yaml, actor=actor, actor_role=role)
                st.session_state["selected_project_id"] = proj.id
            st.rerun()
        return

    proj_labels = list(proj_map.keys())
    default_idx = 0
    if "selected_project_id" in st.session_state:
        for i, lbl in enumerate(proj_labels):
            if proj_map[lbl] == st.session_state["selected_project_id"]:
                default_idx = i
                break
    selected_proj_lbl = st.sidebar.selectbox("Active Project", proj_labels, index=default_idx)
    project_id = proj_map[selected_proj_lbl]
    st.session_state["selected_project_id"] = project_id

    page = st.sidebar.radio(
        "Stage Navigation",
        [
            "Executive Track",
            "1. Discover",
            "2. Assess & Waves",
            "3. Convert & Review Queue",
            "4. Move Data",
            "5. Reconcile",
            "6. Client Sign-Off",
            "Admin, Plugins & Audit",
        ],
    )

    if page == "Executive Track":
        st.title("Migration Control Tower & Tracking")
        col_a, _col_b = st.columns([1, 3])
        with col_a:
            if st.button(
                "Run Full Pipeline (Stages 1-5)",
                disabled=not has_permission(role, Permission.RUN_CONVERT),
            ):
                with session_scope() as db:
                    res = RunOrchestrator().run_pipeline(
                        db, project_id, actor=actor, actor_role=role, deliver_git=True
                    )
                st.success(
                    f"Pipeline complete: {res.discover.tables_discovered} tables, "
                    f"{res.convert.files_generated} artifacts, "
                    f"{res.reconcile.passed} passed recon."
                )

        with session_scope() as db:
            dash = TrackEngine().get_dashboard(db, project_id)
            exports = TrackEngine().export_steering_summary(db, project_id)
            store = get_artifact_store()
            md_text = store.get_text(exports["markdown_uri"])
            xlsx_bytes = store.get_bytes(exports["excel_uri"])

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Objects", dash["total_objects"])
        m2.metric("Validated", dash["by_state"].get("Validated", 0))
        m3.metric("Signed Off", dash["by_state"].get("SignedOff", 0))
        m4.metric("Active Blockers", dash["blockers_count"])

        df_funnel = pd.DataFrame([{"State": k, "Count": v} for k, v in dash["by_state"].items()])
        fig = px.bar(
            df_funnel, x="State", y="Count", title="Object Lifecycle State Funnel", color="State"
        )
        st.plotly_chart(fig, width="stretch")

        if dash["waves"]:
            st.subheader("Wave Progress")
            st.dataframe(pd.DataFrame(dash["waves"]), width="stretch")
        if dash["blockers"]:
            st.subheader("Active Blockers")
            st.dataframe(pd.DataFrame(dash["blockers"]), width="stretch")

        st.download_button(
            "Download Steering Summary (Markdown)", md_text, file_name="steering_summary.md"
        )
        st.download_button(
            "Download Steering Summary (Excel)", xlsx_bytes, file_name="steering_summary.xlsx"
        )

    elif page == "1. Discover":
        st.title("Stage 1 · Discover Legacy Estate")
        uploaded = st.file_uploader(
            "Upload additional ETL export (.dtsx, .xml, .dsx, .sql)", accept_multiple_files=True
        )
        if st.button("Run Discover", disabled=not has_permission(role, Permission.RUN_DISCOVER)):
            extra_paths: list[str] = []
            if uploaded:
                for f in uploaded:
                    extra_paths.append(str(save_upload(project_id, f.name, f.getvalue())))
            with session_scope() as db:
                res = DiscoverEngine().run(
                    db, project_id, actor=actor, actor_role=role, extra_etl_paths=extra_paths
                )
            st.success(
                f"Discovered {res.tables_discovered} tables, {res.views_discovered} views, {res.procedures_discovered} procs, {res.etl_jobs_discovered} ETL jobs."
            )

        with session_scope() as db:
            objs = db.query(DbObject).filter(DbObject.project_id == project_id).all()
            jobs = db.query(EtlJob).filter(EtlJob.project_id == project_id).all()
        st.subheader("Database Objects")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "FQN": o.fqn,
                        "Type": o.object_type,
                        "Rows": o.row_count,
                        "30d Reads": o.usage_reads_30d,
                        "Dead Candidate": o.is_dead_candidate,
                    }
                    for o in objs
                ]
            ),
            width="stretch",
        )
        st.subheader("ETL Jobs & Scripts")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Job Name": j.job_name, "Tool": j.source_tool, "Hash": j.content_hash[:12]}
                    for j in jobs
                ]
            ),
            width="stretch",
        )

    elif page == "2. Assess & Waves":
        st.title("Stage 2 · Assess Complexity, Platform Effort & Wave Plan")
        if st.button("Run Assess", disabled=not has_permission(role, Permission.RUN_ASSESS)):
            with session_scope() as db:
                res = AssessEngine().run(db, project_id, actor=actor, actor_role=role)
            st.success(
                f"Assessed {res.total_objects_scored} objects across {res.waves_created} waves."
            )

        with session_scope() as db:
            scores = (
                db.query(ComplexityScore).filter(ComplexityScore.project_id == project_id).all()
            )
            efforts = db.query(EffortEstimate).filter(EffortEstimate.project_id == project_id).all()
            deps = db.query(Dependency).filter(Dependency.project_id == project_id).all()
            waves = (
                db.query(Wave)
                .filter(Wave.project_id == project_id)
                .order_by(Wave.wave_number)
                .all()
            )
            wave_rows = [
                {
                    "Wave": w.wave_number,
                    "Name": w.name,
                    "Object": m.object_key,
                    "Kind": m.object_kind,
                    "Order": m.topo_order,
                }
                for w in waves
                for m in db.query(WaveMember)
                .filter(WaveMember.wave_id == w.id)
                .order_by(WaveMember.topo_order)
                .all()
            ]
            flows = db.query(IrFlow).filter(IrFlow.project_id == project_id).all()
            flow_payloads = {f.flow_key: f.ir_payload for f in flows}

        if efforts:
            df_eff = (
                pd.DataFrame(
                    [{"Platform": e.target_platform, "PersonDays": e.person_days} for e in efforts]
                )
                .groupby("Platform", as_index=False)["PersonDays"]
                .sum()
            )
            st.plotly_chart(
                px.bar(
                    df_eff,
                    x="Platform",
                    y="PersonDays",
                    color="Platform",
                    title="Side-by-Side Effort Comparison (Person-Days)",
                ),
                width="stretch",
            )

        if deps:
            nodes = sorted({d.upstream_key for d in deps} | {d.downstream_key for d in deps})
            edges = [(d.upstream_key, d.downstream_key, d.edge_type) for d in deps]
            st.plotly_chart(_render_lineage_plotly(nodes, edges), width="stretch")

        st.subheader("Complexity Scores")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Object": s.object_key,
                        "Kind": s.object_kind,
                        "Score": s.score,
                        "Classification": s.classification,
                        "Opaque Items": s.opaque_count,
                        "Reasons": "; ".join(s.reasons or []),
                    }
                    for s in scores
                ]
            ),
            width="stretch",
        )
        st.subheader("Dependency-Ordered Wave Plan")
        st.dataframe(pd.DataFrame(wave_rows), width="stretch")

        if flow_payloads:
            with st.expander("Inspect Neutral Intermediate Representation (IR JSON)"):
                sel_ir = st.selectbox("Flow", list(flow_payloads.keys()))
                st.json(flow_payloads[sel_ir])

    elif page == "3. Convert & Review Queue":
        st.title("Stage 3 · Convert & Human-in-the-Loop Residue Review")
        col1, col2 = st.columns(2)
        with col1:
            deliver_git = st.checkbox("Deliver wave code to Git branch & PR", value=True)
        with col2:
            gen_override = st.selectbox(
                "Generator Plugin",
                [
                    "default (from config)",
                    "snowflake_generator",
                    "databricks_generator",
                    "fabric_generator",
                    "lakebridge_wrapper",
                    "snowconvert_wrapper",
                ],
            )

        if st.button("Run Convert", disabled=not has_permission(role, Permission.RUN_CONVERT)):
            g_name = None if gen_override.startswith("default") else gen_override
            with session_scope() as db:
                res = ConvertEngine().run(
                    db,
                    project_id,
                    generator_name=g_name,
                    deliver_git=deliver_git,
                    actor=actor,
                    actor_role=role,
                )
            st.success(
                f"Generated {res.files_generated} files (compile ok={res.compile_passed_count}, residue={res.residue_items_opened}). Git branch: {res.git_branch}"
            )

        with session_scope() as db:
            residues = db.query(ResidueItem).filter(ResidueItem.project_id == project_id).all()
            artifacts = (
                db.query(GeneratedArtifact).filter(GeneratedArtifact.project_id == project_id).all()
            )

        st.subheader(f"Flagged Residue Review Queue ({len(residues)})")
        store = get_artifact_store()
        for r in residues:
            with st.expander(
                f"[{r.status.upper()}] {r.object_key} :: {r.component_name} ({r.reason_code})",
                expanded=(r.status in ("open", "ai_proposed")),
            ):
                st.write(r.reason_detail)
                st.code(r.original_snippet, language="xml")
                if r.ai_proposed_code:
                    st.caption(f"AI Proposed Fix ({r.ai_model}): {r.ai_rationale}")
                    st.code(r.ai_proposed_code, language="sql")
                c1, c2, c3 = st.columns(3)
                if c1.button(
                    "Propose AI Fix (Opt-in)",
                    key=f"ai_{r.id}",
                    disabled=not has_permission(role, Permission.REVIEW_RESIDUE),
                ):
                    try:
                        with session_scope() as db:
                            ConvertEngine().propose_ai_fix(
                                db, project_id, r.id, actor=actor, actor_role=role
                            )
                        st.rerun()
                    except (
                        AcceleratorError,
                        PermissionError,
                        ValueError,
                        RuntimeError,
                        OSError,
                    ) as exc:
                        st.error(str(exc))
                replacement = st.text_area(
                    "Approved Replacement Code", value=r.ai_proposed_code or "", key=f"rep_{r.id}"
                )
                if c2.button(
                    "Approve & Resolve",
                    key=f"acc_{r.id}",
                    disabled=not has_permission(role, Permission.REVIEW_RESIDUE),
                ):
                    with session_scope() as db:
                        ConvertEngine().decide_residue(
                            db,
                            project_id,
                            r.id,
                            "approved",
                            replacement,
                            "Accepted in UI",
                            actor,
                            role,
                        )
                    st.rerun()
                if c3.button(
                    "Reject",
                    key=f"rej_{r.id}",
                    disabled=not has_permission(role, Permission.REVIEW_RESIDUE),
                ):
                    with session_scope() as db:
                        ConvertEngine().decide_residue(
                            db, project_id, r.id, "rejected", None, "Rejected in UI", actor, role
                        )
                    st.rerun()

        st.subheader("Generated Code Artifacts")
        if artifacts:
            art_map = {f"{a.relative_path} [{a.compile_status}]": a for a in artifacts}
            sel_art = st.selectbox("Inspect Generated File", list(art_map.keys()))
            art_obj = art_map[sel_art]
            st.caption(f"Compile status: {art_obj.compile_status} — {art_obj.compile_log}")
            st.code(store.get_text(art_obj.storage_uri))

    elif page == "4. Move Data":
        st.title("Stage 4 · Move Data (Parquet Bulk & Watermark Incremental)")
        mode = st.radio("Load Mode", ["bulk", "incremental"], horizontal=True)
        if st.button("Execute Move Engine", disabled=not has_permission(role, Permission.RUN_MOVE)):
            with session_scope() as db:
                res = MoveEngine().run(db, project_id, mode=mode, actor=actor, actor_role=role)
            st.success(f"Loaded {res.tables_loaded} table(s), {res.total_rows_loaded} total rows.")

        with session_scope() as db:
            runs = (
                db.query(LoadRun)
                .filter(LoadRun.project_id == project_id)
                .order_by(LoadRun.created_at.desc())
                .all()
            )
            rows = [
                {
                    "Run ID": r.id[:8],
                    "Table": r.object_key,
                    "Mode": r.mode,
                    "Rows Loaded": r.rows_loaded,
                    "Watermark": r.watermark_value,
                    "Partitions": len(r.partitions),
                    "Status": r.status,
                }
                for r in runs
            ]
        st.dataframe(pd.DataFrame(rows), width="stretch")

    elif page == "5. Reconcile":
        st.title("Stage 5 · Reconcile (Pushed-Down L1-L4 & Evidence)")
        level_opt = st.selectbox("Level Override", ["(use client config)", "L1", "L2", "L3", "L4"])
        if st.button(
            "Run Reconciliation", disabled=not has_permission(role, Permission.RUN_RECONCILE)
        ):
            lvl = None if level_opt.startswith("(") else level_opt
            with session_scope() as db:
                res = ReconcileEngine().run(
                    db, project_id, level_override=lvl, actor=actor, actor_role=role
                )
            st.info(
                f"Checked {res.tables_checked} table(s): Pass={res.passed}, Tolerance={res.passed_within_tolerance}, Fail={res.failed}"
            )

        with session_scope() as db:
            results = (
                db.query(ReconResult)
                .filter(ReconResult.project_id == project_id)
                .order_by(ReconResult.created_at.desc())
                .all()
            )
        if results:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Object": r.object_key,
                            "Level": r.level,
                            "Verdict": r.verdict,
                            "Source Rows": r.source_metrics.get("row_count"),
                            "Target Rows": r.target_metrics.get("row_count"),
                            "Diff Keys": list(r.diff_summary.keys()),
                            "Evidence": r.evidence_uri,
                        }
                        for r in results
                    ]
                ),
                width="stretch",
            )
            sel_r = st.selectbox(
                "Inspect Evidence Details",
                [f"{r.object_key} ({r.level} - {r.verdict}) [{r.id[:8]}]" for r in results],
            )
            idx = [f"{r.object_key} ({r.level} - {r.verdict}) [{r.id[:8]}]" for r in results].index(
                sel_r
            )
            st.json(
                {
                    "source_metrics": results[idx].source_metrics,
                    "target_metrics": results[idx].target_metrics,
                    "diff_summary": results[idx].diff_summary,
                }
            )

    elif page == "6. Client Sign-Off":
        st.title("Stage 6 · Client Data Owner Sign-Off")
        with session_scope() as db:
            validated = (
                db.query(ObjectStatus)
                .filter(
                    ObjectStatus.project_id == project_id,
                    ObjectStatus.state.in_(["Validated", "SignedOff"]),
                )
                .all()
            )
            signoffs = db.query(Signoff).filter(Signoff.project_id == project_id).all()

        if not validated:
            st.info("No objects are in 'Validated' state yet. Complete Move & Reconcile first.")
        else:
            for obj in validated:
                col1, col2, col3 = st.columns([3, 1, 2])
                col1.write(f"**{obj.object_key}** (`{obj.state}`)")
                col2.write(obj.last_evidence_ref or "")
                if obj.state == "Validated":
                    if col3.button(
                        "Sign Off",
                        key=f"so_{obj.id}",
                        disabled=not has_permission(role, Permission.SIGN_OFF),
                    ):
                        with session_scope() as db:
                            TrackEngine().sign_off(db, project_id, obj.object_key, actor, role)
                        st.rerun()
                else:
                    col3.success("Signed Off")

        if signoffs:
            st.subheader("Signed-Off Evidence Bundles")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Object": s.object_key,
                            "Approver": s.approver,
                            "Recon Run": s.recon_run_id[:8],
                            "Evidence Bundle": s.evidence_bundle_uri,
                            "Signed At": s.signed_at,
                        }
                        for s in signoffs
                    ]
                ),
                width="stretch",
            )

    elif page == "Admin, Plugins & Audit":
        st.title("Platform Admin · Plugins, Golden Tests & Audit Log")
        if st.button(
            "Run All Plugin Golden Tests",
            disabled=not has_permission(role, Permission.MANAGE_PLUGINS),
        ):
            reg = get_plugin_registry()
            out = []
            with session_scope() as db:
                for p in reg.list_unique():
                    ok, msg = p.run_golden_check()
                    entry = (
                        db.query(PluginRegistryEntry)
                        .filter(PluginRegistryEntry.name == p.manifest.name)
                        .first()
                    )
                    if entry:
                        entry.last_golden_status = "passed" if ok else "failed"
                    out.append(
                        {
                            "Plugin": p.manifest.name,
                            "Type": p.manifest.plugin_type,
                            "Passed": ok,
                            "Message": msg,
                        }
                    )
            st.dataframe(pd.DataFrame(out), width="stretch")

        with session_scope() as db:
            plugins = (
                db.query(PluginRegistryEntry)
                .order_by(PluginRegistryEntry.plugin_type, PluginRegistryEntry.name)
                .all()
            )
            audits = (
                db.query(AuditLog)
                .filter(AuditLog.project_id == project_id)
                .order_by(AuditLog.timestamp.desc())
                .limit(100)
                .all()
            )

        st.subheader("Registered Plugins")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Type": p.plugin_type,
                        "Name": p.name,
                        "Version": p.version,
                        "Golden Status": p.last_golden_status,
                        "Capabilities": json.dumps(p.capabilities),
                    }
                    for p in plugins
                ]
            ),
            width="stretch",
        )
        st.subheader("Immutable Audit Log")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Timestamp": a.timestamp,
                        "Actor": f"{a.actor} ({a.actor_role})",
                        "Action": a.action,
                        "Resource": f"{a.resource_type}:{a.resource_id}",
                        "Details": json.dumps(a.details),
                    }
                    for a in audits
                ]
            ),
            width="stretch",
        )


if __name__ == "__main__":
    main()
