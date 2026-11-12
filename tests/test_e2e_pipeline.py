"""End-to-end integration tests for all 6 stage engines, state machine, checkpoints, L4 masked diffs, and Git delivery."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

from accelerator.core.exceptions import EvidenceRequiredError
from accelerator.core.secrets import get_vault
from accelerator.db.models import LoadRun, ReconResult, ResidueItem
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.engines.track import TrackEngine


def _seed_acme(tmp_path: Path) -> tuple[str, Path, Path]:
    spec = importlib.util.spec_from_file_location(
        "seed_legacy_db", Path("examples/acme_retail/seed_legacy_db.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src_db, tgt_db = mod.seed_all(base_dir=tmp_path / "duckdbs")

    vault = get_vault()
    vault.set_secret("vault://acme/sqlserver-ro", f"duckdb:///{src_db.as_posix()}")
    vault.set_secret("vault://acme/snowflake-deploy", f"duckdb:///{tgt_db.as_posix()}")

    raw_yaml = Path("examples/acme_retail/client_config.yaml").read_text(encoding="utf-8")
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, raw_yaml, actor="lead", actor_role="migration_lead")
        return proj.id, src_db, tgt_db


def test_full_six_stage_e2e_and_signoff(isolated_env: Path) -> None:
    project_id, _src_db, tgt_db = _seed_acme(isolated_env)

    # 1. Discover (twice to verify idempotency & hash deduplication)
    with session_scope() as db:
        d1 = DiscoverEngine().run(db, project_id, actor="eng")
        assert d1.tables_discovered == 5
        assert d1.views_discovered == 1
        assert d1.etl_jobs_discovered == 4
        assert d1.dead_candidates == 1

        d2 = DiscoverEngine().run(db, project_id, actor="eng")
        assert d2.artifacts_collected == 0
        assert d2.artifacts_unchanged >= 5

    # 2. Assess
    with session_scope() as db:
        a = AssessEngine().run(db, project_id, actor="lead")
        assert a.total_objects_scored == 10
        assert a.effort_by_platform["snowflake"] > 0
        assert a.effort_by_platform["databricks"] > a.effort_by_platform["snowflake"]
        assert a.waves_created >= 2

    # 3. Convert + Git PR delivery
    git_repo = isolated_env / "target_repo"
    with session_scope() as db:
        c = ConvertEngine().run(db, project_id, deliver_git=True, git_repo_path=git_repo, actor="eng")
        assert c.files_generated >= 8
        assert c.compile_failed_count == 0
        assert c.residue_items_opened >= 1
        assert c.git_branch is not None
        assert (git_repo / ".git").exists()

        # Cannot deploy object with unresolved residue!
        with pytest.raises(EvidenceRequiredError):
            ConvertEngine().approve_and_deploy_object(
                db, project_id, "Load_Customers_And_Orders", actor="eng", actor_role="engineer"
            )

        # AI assist proposes fix -> Human accepts -> Now deploy succeeds!
        res_item = (
            db.query(ResidueItem)
            .filter(ResidueItem.project_id == project_id, ResidueItem.object_key == "Load_Customers_And_Orders")
            .first()
        )
        assert res_item is not None
        ConvertEngine().propose_ai_fix(db, project_id, res_item.id, actor="eng", actor_role="engineer")
        ConvertEngine().decide_residue(
            db, project_id, res_item.id, "approved", None, "Looks good", actor="reviewer", actor_role="reviewer"
        )
        st = ConvertEngine().approve_and_deploy_object(
            db, project_id, "Load_Customers_And_Orders", actor="eng", actor_role="engineer"
        )
        assert st.state == "Deployed"

    # 4. Move Data (Bulk with multiple 50-row partitions on 120-row orders table)
    with session_scope() as db:
        m = MoveEngine().run(db, project_id, mode="bulk", actor="eng")
        assert m.tables_loaded == 5
        assert m.total_rows_loaded == 271  # 20 cust + 10 prod + 120 orders + 120 gl + 1 log
        orders_run = (
            db.query(LoadRun)
            .filter(LoadRun.project_id == project_id, LoadRun.object_key == "legacy_ddl.dbo.orders")
            .first()
        )
        assert orders_run is not None
        assert len(orders_run.partitions) == 3  # 120 / 50 = 3 partitions!

        # Verify restartable resume skips already-completed partitions cleanly
        m_resume = MoveEngine().run(
            db,
            project_id,
            object_key="legacy_ddl.dbo.orders",
            mode="bulk",
            resume_load_run_id=orders_run.id,
            actor="eng",
        )
        assert m_resume.total_rows_loaded == 0

    # 5. Reconcile (L2 + L3 on finance.gl_entries)
    with session_scope() as db:
        r = ReconcileEngine().run(db, project_id, actor="eng")
        assert r.tables_checked == 5
        assert r.passed == 5
        assert r.failed == 0

    # 6. Track & Sign-off
    with session_scope() as db:
        signoff = TrackEngine().sign_off(
            db,
            project_id,
            object_key="legacy_ddl.dbo.orders",
            approver="approver",
            approver_role="client_approver",
        )
        assert signoff.decision == "signed_off"
        dash = TrackEngine().get_dashboard(db, project_id)
        assert dash["by_state"]["SignedOff"] == 1
        assert dash["by_state"]["Validated"] == 4

        # Verify cannot sign off an object that is NOT Validated
        with pytest.raises(EvidenceRequiredError):
            TrackEngine().sign_off(
                db,
                project_id,
                object_key="Load_Customers_And_Orders",
                approver="approver",
                approver_role="client_approver",
            )

    # 7. Inject a mismatch in target DB and verify L4 diff masks PII columns (ssn, email)!
    t_conn = duckdb.connect(str(tgt_db))
    t_conn.execute("UPDATE dbo.customers SET email = 'tampered@evil.test' WHERE customer_id = 1;")
    t_conn.close()

    with session_scope() as db:
        r_fail = ReconcileEngine().run(
            db, project_id, object_key="legacy_ddl.dbo.customers", level_override="L4", actor="eng"
        )
        assert r_fail.failed == 1
        latest_res = (
            db.query(ReconResult)
            .filter(ReconResult.project_id == project_id, ReconResult.object_key == "legacy_ddl.dbo.customers")
            .order_by(ReconResult.created_at.desc())
            .first()
        )
        assert latest_res is not None
        assert latest_res.verdict == "fail"
        l4 = latest_res.diff_summary["l4_diff"]
        assert l4["value_mismatch_count"] >= 1
        masked_src = l4["value_mismatch_sample"][0]["source"]
        # Ensure raw SSN and email are masked!
        assert "123-45-1001" not in str(masked_src["ssn"])
        assert "cust1@acme.example" not in str(masked_src["email"])
