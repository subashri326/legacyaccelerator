"""Stage 3: Convert Engine — deterministic generation, residue queue, opt-in AI assist, review & Git PR."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from accelerator.core.ai_assist import ResidueAiAssistant
from accelerator.core.exceptions import EvidenceRequiredError
from accelerator.core.git_delivery import GitDeliveryResult, GitDeliveryService
from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.base import utcnow
from accelerator.db.models import (
    ConversionRun,
    DbColumn,
    DbObject,
    GeneratedArtifact,
    IrFlow,
    ObjectStatus,
    ResidueItem,
    ReviewDecision,
    Wave,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit
from accelerator.engines.state_machine import ObjectState, StateMachine
from accelerator.ir.models import ColumnIR, PackageIR, TableIR
from accelerator.plugins.interfaces import GeneratedFile
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class ConvertSummary:
    conversion_run_id: str
    project_id: str
    target_platform: str
    files_generated: int
    compile_passed_count: int
    compile_failed_count: int
    residue_items_opened: int
    git_branch: str | None = None
    git_commit_sha: str | None = None
    pr_reference: str | None = None


class ConvertEngine:
    """Converts Neutral IR into target SQL/dbt/PySpark/orchestration with residue review & Git PRs."""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        store: ArtifactStore | None = None,
        ai_assistant: ResidueAiAssistant | None = None,
    ) -> None:
        self.registry = registry or get_plugin_registry()
        self.store = store or get_artifact_store()
        self.ai_assistant = ai_assistant or ResidueAiAssistant()

    def run(
        self,
        db: Session,
        project_id: str,
        *,
        wave_id: str | None = None,
        object_key: str | None = None,
        generator_name: str | None = None,
        deliver_git: bool = False,
        git_repo_path: str | Path | None = None,
        actor: str = "system",
        actor_role: str = "engineer",
    ) -> ConvertSummary:
        with metrics.stage_span("convert", project_id):
            cfg_ver, cfg = get_active_config(db, project_id)
            cfg.require_target()
            gen_key = generator_name or cfg.target.platform
            generator = self.registry.get_generator(gen_key)

            run_row = ConversionRun(
                project_id=project_id,
                wave_id=wave_id,
                config_version_id=cfg_ver.id,
                target_platform=cfg.target.platform,
                output_mode=cfg.target.output,
                status="running",
                created_by=actor,
            )
            db.add(run_row)
            db.flush()

            # Determine scope: single object, wave, or entire estate
            allowed_keys: set[str] | None = None
            wave_name = "all-waves"
            if object_key:
                allowed_keys = {object_key}
            elif wave_id:
                wave = db.query(Wave).filter(Wave.id == wave_id, Wave.project_id == project_id).first()
                if wave is None:
                    raise ValueError(f"Wave '{wave_id}' not found")
                wave_name = f"wave-{wave.wave_number}"
                members = db.query(WaveMember).filter(WaveMember.wave_id == wave_id).all()
                allowed_keys = {m.object_key for m in members}

            db_objects = db.query(DbObject).filter(DbObject.project_id == project_id).all()
            tables_map: dict[str, TableIR] = {}
            for obj in db_objects:
                if obj.object_type == "TABLE":
                    cols = (
                        db.query(DbColumn)
                        .filter(DbColumn.db_object_id == obj.id)
                        .order_by(DbColumn.ordinal_position)
                        .all()
                    )
                    tables_map[obj.fqn] = TableIR(
                        database=obj.database_name,
                        schema_name=obj.schema_name,
                        name=obj.object_name,
                        columns=[
                            ColumnIR(
                                name=c.column_name,
                                data_type=c.data_type,
                                nullable=c.nullable,
                                is_primary_key=c.is_primary_key,
                                is_foreign_key=c.is_foreign_key,
                                fk_target=c.fk_target,
                                ordinal_position=c.ordinal_position,
                            )
                            for c in cols
                        ],
                        row_count=obj.row_count,
                    )

            generated_files: list[GeneratedFile] = []
            compile_ok = 0
            compile_fail = 0
            residue_opened = 0

            # Clear unresolved open residue items for scoped objects so re-runs are idempotent
            res_q = db.query(ResidueItem).filter(
                ResidueItem.project_id == project_id,
                ResidueItem.status.in_(["open", "ai_proposed"]),
            )
            if allowed_keys is not None:
                res_q = res_q.filter(ResidueItem.object_key.in_(allowed_keys))
            res_q.delete(synchronize_session=False)

            # Generate DDLs for tables in scope
            for fqn, tbl_ir in tables_map.items():
                if allowed_keys is not None and fqn not in allowed_keys:
                    continue
                ddl_file = generator.generate_ddl(tbl_ir, cfg)
                generated_files.append(ddl_file)
                self._upsert_generated_artifact(db, run_row.id, project_id, cfg.target.platform, ddl_file, actor)
                if ddl_file.compile_passed:
                    compile_ok += 1
                    StateMachine.advance(
                        db, project_id, fqn, "table", ObjectState.CONVERTED, actor, "conversion_run", run_row.id
                    )
                else:
                    compile_fail += 1
                    StateMachine.set_blocker(db, project_id, fqn, ddl_file.compile_log)

            # Load accepted residue overrides from previous human decisions
            accepted_decisions = (
                db.query(ReviewDecision)
                .filter(
                    ReviewDecision.project_id == project_id,
                    ReviewDecision.decision == "approved",
                    ReviewDecision.replacement_code.isnot(None),
                )
                .all()
            )
            accepted_map: dict[str, dict[str, str]] = {}
            for dec in accepted_decisions:
                if dec.residue_item_id:
                    r_item = db.query(ResidueItem).filter(ResidueItem.id == dec.residue_item_id).first()
                    if r_item and dec.replacement_code:
                        accepted_map.setdefault(dec.object_key, {})[r_item.component_name] = dec.replacement_code

            # Generate code from persisted Neutral IR flows (never raw files!)
            flows = db.query(IrFlow).filter(IrFlow.project_id == project_id).all()
            for flow in flows:
                if allowed_keys is not None and flow.flow_key not in allowed_keys:
                    continue
                pkg = PackageIR.model_validate(flow.ir_payload)
                bundle = generator.generate_package(
                    pkg, tables_map, cfg, accepted_overrides=accepted_map.get(flow.flow_key)
                )

                obj_compile_ok = True
                for gf in bundle.files:
                    generated_files.append(gf)
                    self._upsert_generated_artifact(db, run_row.id, project_id, cfg.target.platform, gf, actor)
                    if gf.compile_passed:
                        compile_ok += 1
                    else:
                        compile_fail += 1
                        obj_compile_ok = False

                obj_residues = 0
                for res in bundle.residues:
                    obj_residues += 1
                    residue_opened += 1
                    db.add(
                        ResidueItem(
                            conversion_run_id=run_row.id,
                            project_id=project_id,
                            object_key=flow.flow_key,
                            component_name=res.component_name,
                            reason_code=res.reason_code,
                            reason_detail=res.reason_detail,
                            original_snippet=res.original_snippet,
                            status="open",
                            created_by=actor,
                        )
                    )

                obj_kind = "etl_job" if flow.etl_job_id else "view"
                if obj_residues == 0 and obj_compile_ok:
                    StateMachine.advance(
                        db,
                        project_id,
                        flow.flow_key,
                        obj_kind,
                        ObjectState.CONVERTED,
                        actor,
                        "conversion_run",
                        run_row.id,
                    )
                else:
                    blocker = (
                        f"{obj_residues} flagged residue item(s) awaiting review"
                        if obj_residues > 0
                        else "Target compile check failed"
                    )
                    StateMachine.set_blocker(db, project_id, flow.flow_key, blocker)

            git_result: GitDeliveryResult | None = None
            if deliver_git and generated_files:
                repo_dir = Path(git_repo_path or cfg.git.repo_path or f".l2c_data/git_repos/{project_id}")
                git_svc = GitDeliveryService(repo_dir, base_branch=cfg.git.base_branch)
                files_dict = {gf.relative_path: gf.content for gf in generated_files}
                git_result = git_svc.deliver_wave(
                    wave_name=wave_name,
                    branch_prefix=cfg.git.branch_prefix,
                    files=files_dict,
                    summary_lines=[
                        f"Target platform: {cfg.target.platform} ({cfg.target.output})",
                        f"Compile passed: {compile_ok}, failed: {compile_fail}",
                        f"Open residue items: {residue_opened}",
                    ],
                )
                run_row.git_branch = git_result.branch_name
                run_row.git_commit_sha = git_result.commit_sha
                run_row.pr_reference = git_result.pr_url_or_path

            run_row.status = "completed"
            run_row.completed_at = utcnow()
            run_row.summary = {
                "files_generated": len(generated_files),
                "compile_passed": compile_ok,
                "compile_failed": compile_fail,
                "residue_opened": residue_opened,
            }

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.convert.completed",
                resource_type="conversion_run",
                resource_id=run_row.id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details=run_row.summary,
            )

            return ConvertSummary(
                conversion_run_id=run_row.id,
                project_id=project_id,
                target_platform=cfg.target.platform,
                files_generated=len(generated_files),
                compile_passed_count=compile_ok,
                compile_failed_count=compile_fail,
                residue_items_opened=residue_opened,
                git_branch=git_result.branch_name if git_result else None,
                git_commit_sha=git_result.commit_sha if git_result else None,
                pr_reference=git_result.pr_url_or_path if git_result else None,
            )

    def _upsert_generated_artifact(
        self,
        db: Session,
        run_id: str,
        project_id: str,
        platform: str,
        gf: GeneratedFile,
        actor: str,
    ) -> GeneratedArtifact:
        digest = hashlib.sha256(gf.content.encode("utf-8")).hexdigest()
        storage_uri = self.store.put_text(
            f"projects/{project_id}/generated/{platform}/{gf.relative_path}", gf.content
        )
        existing = (
            db.query(GeneratedArtifact)
            .filter(
                GeneratedArtifact.project_id == project_id,
                GeneratedArtifact.object_key == gf.object_key,
                GeneratedArtifact.relative_path == gf.relative_path,
                GeneratedArtifact.target_platform == platform,
            )
            .first()
        )
        if existing is None:
            row = GeneratedArtifact(
                conversion_run_id=run_id,
                project_id=project_id,
                object_key=gf.object_key,
                target_platform=platform,
                artifact_kind=gf.artifact_kind,
                relative_path=gf.relative_path,
                storage_uri=storage_uri,
                content_hash=digest,
                compile_status="passed" if gf.compile_passed else "failed",
                compile_log=gf.compile_log,
                created_by=actor,
            )
            db.add(row)
            db.flush()
            return row

        existing.conversion_run_id = run_id
        existing.storage_uri = storage_uri
        existing.content_hash = digest
        existing.compile_status = "passed" if gf.compile_passed else "failed"
        existing.compile_log = gf.compile_log
        db.flush()
        return existing

    def propose_ai_fix(
        self,
        db: Session,
        project_id: str,
        residue_id: str,
        actor: str = "engineer",
        actor_role: str = "engineer",
    ) -> ResidueItem:
        _, cfg = get_active_config(db, project_id)
        item = (
            db.query(ResidueItem)
            .filter(ResidueItem.id == residue_id, ResidueItem.project_id == project_id)
            .first()
        )
        if item is None:
            raise ValueError(f"ResidueItem '{residue_id}' not found")

        suggestion = self.ai_assistant.suggest_fix(
            original_snippet=item.original_snippet,
            reason_code=item.reason_code,
            source_dialect=cfg.source.etl or cfg.source.database,
            target_platform=cfg.target.platform,
            ai_config=cfg.ai_assist,
        )
        item.ai_proposed_code = suggestion.proposed_code
        item.ai_rationale = suggestion.rationale
        item.ai_model = suggestion.model_used
        item.status = "ai_proposed"
        db.flush()

        record_audit(
            db,
            actor=actor,
            actor_role=actor_role,
            action="residue.ai_proposed",
            resource_type="residue_item",
            resource_id=item.id,
            project_id=project_id,
            details={"model": suggestion.model_used},
        )
        return item

    def decide_residue(
        self,
        db: Session,
        project_id: str,
        residue_id: str,
        decision: str,  # approved | rejected
        replacement_code: str | None,
        comment: str,
        actor: str,
        actor_role: str,
    ) -> ResidueItem:
        item = (
            db.query(ResidueItem)
            .filter(ResidueItem.id == residue_id, ResidueItem.project_id == project_id)
            .first()
        )
        if item is None:
            raise ValueError(f"ResidueItem '{residue_id}' not found")

        final_code = replacement_code or item.ai_proposed_code
        if decision == "approved":
            item.status = "accepted"
        else:
            item.status = "rejected"

        dec = ReviewDecision(
            project_id=project_id,
            object_key=item.object_key,
            residue_item_id=item.id,
            decision=decision,
            replacement_code=final_code,
            reviewer=actor,
            comment=comment,
            created_by=actor,
        )
        db.add(dec)
        db.flush()

        # Check if any open residues remain for this object
        remaining = (
            db.query(ResidueItem)
            .filter(
                ResidueItem.project_id == project_id,
                ResidueItem.object_key == item.object_key,
                ResidueItem.status.in_(["open", "ai_proposed"]),
            )
            .count()
        )
        if remaining == 0 and decision == "approved":
            obj_status = (
                db.query(ObjectStatus)
                .filter(ObjectStatus.project_id == project_id, ObjectStatus.object_key == item.object_key)
                .first()
            )
            kind = obj_status.object_kind if obj_status else "etl_job"
            if obj_status and obj_status.state == ObjectState.ASSESSED.value:
                StateMachine.advance(
                    db, project_id, item.object_key, kind, ObjectState.CONVERTED, actor, "review_decision", dec.id
                )
            StateMachine.advance(
                db, project_id, item.object_key, kind, ObjectState.REVIEWED, actor, "review_decision", dec.id
            )

        record_audit(
            db,
            actor=actor,
            actor_role=actor_role,
            action=f"residue.{decision}",
            resource_type="residue_item",
            resource_id=item.id,
            project_id=project_id,
            details={"object_key": item.object_key, "comment": comment},
        )
        return item

    def approve_and_deploy_object(
        self,
        db: Session,
        project_id: str,
        object_key: str,
        actor: str,
        actor_role: str,
        comment: str = "Approved for deployment",
    ) -> ObjectStatus:
        open_res = (
            db.query(ResidueItem)
            .filter(
                ResidueItem.project_id == project_id,
                ResidueItem.object_key == object_key,
                ResidueItem.status.in_(["open", "ai_proposed"]),
            )
            .count()
        )
        if open_res > 0:
            raise EvidenceRequiredError(
                f"Object '{object_key}' still has {open_res} unresolved residue item(s)."
            )

        obj_status = (
            db.query(ObjectStatus)
            .filter(ObjectStatus.project_id == project_id, ObjectStatus.object_key == object_key)
            .first()
        )
        if obj_status is None:
            raise ValueError(f"Object '{object_key}' not found")

        dec = ReviewDecision(
            project_id=project_id,
            object_key=object_key,
            decision="approved",
            reviewer=actor,
            comment=comment,
            created_by=actor,
        )
        db.add(dec)
        db.flush()

        if obj_status.state == ObjectState.CONVERTED.value:
            StateMachine.advance(
                db, project_id, object_key, obj_status.object_kind, ObjectState.REVIEWED, actor, "review_decision", dec.id
            )
        updated = StateMachine.advance(
            db, project_id, object_key, obj_status.object_kind, ObjectState.DEPLOYED, actor, "deployment", dec.id
        )
        record_audit(
            db,
            actor=actor,
            actor_role=actor_role,
            action="object.deployed",
            resource_type="object_status",
            resource_id=object_key,
            project_id=project_id,
        )
        return updated
