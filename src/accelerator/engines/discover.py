"""Stage 1: Discover Engine — collects catalogs, ETL exports, and usage stats with hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.models import (
    DbColumn,
    DbObject,
    EtlJob,
    SourceArtifact,
)
from accelerator.db.repository import get_active_config, record_audit
from accelerator.engines.state_machine import StateMachine
from accelerator.ir.models import CatalogIR
from accelerator.plugins.collectors.etl_files import collect_etl_artifacts
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class DiscoverSummary:
    project_id: str
    artifacts_collected: int
    artifacts_unchanged: int
    tables_discovered: int
    views_discovered: int
    procedures_discovered: int
    etl_jobs_discovered: int
    dead_candidates: int


class DiscoverEngine:
    """Harvests read-only catalogs and ETL exports, hashes & versions raw artifacts."""

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
        actor_role: str = "engineer",
        extra_etl_paths: list[str | Path] | None = None,
        catalog_bundle_override: str | None = None,
    ) -> DiscoverSummary:
        with metrics.stage_span("discover", project_id):
            cfg_ver, cfg = get_active_config(db, project_id)
            collector = self.registry.get_collector(cfg.source.database)

            bundle_path = catalog_bundle_override or cfg.source.catalog_bundle_path
            catalog: CatalogIR = collector.collect_catalog(
                connection_uri=cfg.source.connection,
                schemas=cfg.source.schemas or None,
                catalog_bundle_path=bundle_path,
            )
            usage_map = collector.collect_usage(cfg.source.connection, cfg.source.usage_log_path)

            # Store catalog snapshot artifact versioned by hash
            cat_bytes = catalog.model_dump_json(indent=2).encode("utf-8")
            cat_art, cat_new = self._store_versioned_artifact(
                db=db,
                project_id=project_id,
                artifact_key=f"catalog/{catalog.database_name}.json",
                artifact_type="catalog",
                source_tool=cfg.source.database,
                content_bytes=cat_bytes,
                actor=actor,
            )
            artifacts_collected = 1 if cat_new else 0
            artifacts_unchanged = 0 if cat_new else 1

            dead_candidates = 0

            # Upsert tables
            for tbl in catalog.tables:
                reads = usage_map.get(
                    tbl.fqn.lower(),
                    usage_map.get(f"{tbl.schema_name}.{tbl.name}".lower(), tbl.usage_reads_30d),
                )
                is_dead = bool(usage_map and reads == 0) or tbl.is_dead_candidate
                if is_dead:
                    dead_candidates += 1
                # Structure only: row counts change daily and must not count as a source change.
                structure = [
                    (c.name, c.data_type, c.nullable, c.is_primary_key) for c in tbl.columns
                ]
                tbl_hash = hashlib.sha256(repr(structure).encode("utf-8")).hexdigest()
                db_obj = self._upsert_db_object(
                    db=db,
                    project_id=project_id,
                    source_artifact_id=cat_art.id,
                    database_name=tbl.database,
                    schema_name=tbl.schema_name,
                    object_name=tbl.name,
                    fqn=tbl.fqn,
                    object_type="TABLE",
                    row_count=tbl.row_count,
                    size_bytes=tbl.size_bytes,
                    usage_reads=reads,
                    is_dead=is_dead,
                    definition_sql=tbl.source_sql,
                    content_hash=tbl_hash,
                    actor=actor,
                )
                # Sync columns idempotently
                db.query(DbColumn).filter(DbColumn.db_object_id == db_obj.id).delete()
                for col in tbl.columns:
                    db.add(
                        DbColumn(
                            db_object_id=db_obj.id,
                            column_name=col.name,
                            data_type=col.data_type,
                            length=col.length,
                            precision=col.precision,
                            scale=col.scale,
                            nullable=col.nullable,
                            is_primary_key=col.is_primary_key,
                            is_foreign_key=col.is_foreign_key,
                            fk_target=col.fk_target,
                            ordinal_position=col.ordinal_position,
                            attributes=col.attributes,
                            created_by=actor,
                        )
                    )
                db_obj.attributes = tbl.attributes
                StateMachine.record_source(db, project_id, tbl.fqn, "table", tbl_hash, actor)

            # Upsert views
            for vw in catalog.views:
                fqn = (
                    f"{vw.database}.{vw.schema_name}.{vw.name}"
                    if vw.database
                    else f"{vw.schema_name}.{vw.name}"
                )
                vw_hash = hashlib.sha256(vw.definition_sql.encode("utf-8")).hexdigest()
                self._upsert_db_object(
                    db=db,
                    project_id=project_id,
                    source_artifact_id=cat_art.id,
                    database_name=vw.database,
                    schema_name=vw.schema_name,
                    object_name=vw.name,
                    fqn=fqn,
                    object_type="VIEW",
                    row_count=None,
                    size_bytes=len(vw.definition_sql),
                    usage_reads=usage_map.get(fqn.lower(), 0),
                    is_dead=False,
                    definition_sql=vw.definition_sql,
                    content_hash=vw_hash,
                    actor=actor,
                )
                StateMachine.record_source(db, project_id, fqn, "view", vw_hash, actor)

            # Upsert stored procedures
            for proc in catalog.procedures:
                fqn = (
                    f"{proc.database}.{proc.schema_name}.{proc.name}"
                    if proc.database
                    else f"{proc.schema_name}.{proc.name}"
                )
                p_hash = hashlib.sha256(proc.definition_sql.encode("utf-8")).hexdigest()
                self._upsert_db_object(
                    db=db,
                    project_id=project_id,
                    source_artifact_id=cat_art.id,
                    database_name=proc.database,
                    schema_name=proc.schema_name,
                    object_name=proc.name,
                    fqn=fqn,
                    object_type="PROCEDURE",
                    row_count=None,
                    size_bytes=len(proc.definition_sql),
                    usage_reads=usage_map.get(fqn.lower(), 0),
                    is_dead=False,
                    definition_sql=proc.definition_sql,
                    content_hash=p_hash,
                    actor=actor,
                )
                StateMachine.record_source(db, project_id, fqn, "procedure", p_hash, actor)

            # Collect ETL & SQL artifacts
            all_etl_paths: list[str | Path] = list(cfg.source.etl_export_paths)
            if extra_etl_paths:
                all_etl_paths.extend(extra_etl_paths)

            etl_artifacts = collect_etl_artifacts(
                all_etl_paths, default_tool=cfg.source.etl, source_database=cfg.source.database
            )
            etl_count = 0
            for item in etl_artifacts:
                art_row, is_new = self._store_versioned_artifact(
                    db=db,
                    project_id=project_id,
                    artifact_key=item.artifact_key,
                    artifact_type=item.artifact_type,
                    source_tool=item.source_tool,
                    content_bytes=item.content_bytes,
                    actor=actor,
                )
                if is_new:
                    artifacts_collected += 1
                else:
                    artifacts_unchanged += 1

                job_name = Path(item.artifact_key).stem
                existing_job = (
                    db.query(EtlJob)
                    .filter(EtlJob.project_id == project_id, EtlJob.job_name == job_name)
                    .first()
                )
                if existing_job is None:
                    db.add(
                        EtlJob(
                            project_id=project_id,
                            source_artifact_id=art_row.id,
                            job_name=job_name,
                            source_tool=item.source_tool,
                            content_hash=art_row.content_hash,
                            created_by=actor,
                        )
                    )
                else:
                    existing_job.source_artifact_id = art_row.id
                    existing_job.source_tool = item.source_tool
                    existing_job.content_hash = art_row.content_hash
                etl_count += 1
                StateMachine.record_source(
                    db, project_id, job_name, "etl_job", art_row.content_hash, actor
                )

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.discover.completed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details={
                    "tables": len(catalog.tables),
                    "views": len(catalog.views),
                    "procedures": len(catalog.procedures),
                    "etl_jobs": etl_count,
                },
            )

            return DiscoverSummary(
                project_id=project_id,
                artifacts_collected=artifacts_collected,
                artifacts_unchanged=artifacts_unchanged,
                tables_discovered=len(catalog.tables),
                views_discovered=len(catalog.views),
                procedures_discovered=len(catalog.procedures),
                etl_jobs_discovered=etl_count,
                dead_candidates=dead_candidates,
            )

    def _store_versioned_artifact(
        self,
        db: Session,
        project_id: str,
        artifact_key: str,
        artifact_type: str,
        source_tool: str,
        content_bytes: bytes,
        actor: str,
    ) -> tuple[SourceArtifact, bool]:
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        existing = (
            db.query(SourceArtifact)
            .filter(
                SourceArtifact.project_id == project_id,
                SourceArtifact.artifact_key == artifact_key,
                SourceArtifact.content_hash == content_hash,
            )
            .first()
        )
        if existing is not None:
            return existing, False

        prev_max = (
            db.query(SourceArtifact)
            .filter(
                SourceArtifact.project_id == project_id, SourceArtifact.artifact_key == artifact_key
            )
            .order_by(SourceArtifact.version_num.desc())
            .first()
        )
        next_ver = (prev_max.version_num + 1) if prev_max else 1
        db.query(SourceArtifact).filter(
            SourceArtifact.project_id == project_id,
            SourceArtifact.artifact_key == artifact_key,
        ).update({"is_latest": False})

        storage_key = f"projects/{project_id}/raw/{artifact_key}_v{next_ver}_{content_hash[:8]}"
        storage_uri = self.store.put_bytes(storage_key, content_bytes)

        row = SourceArtifact(
            project_id=project_id,
            artifact_key=artifact_key,
            artifact_type=artifact_type,
            source_tool=source_tool,
            content_hash=content_hash,
            version_num=next_ver,
            storage_uri=storage_uri,
            size_bytes=len(content_bytes),
            is_latest=True,
            created_by=actor,
        )
        db.add(row)
        db.flush()
        return row, True

    def _upsert_db_object(
        self,
        db: Session,
        *,
        project_id: str,
        source_artifact_id: str,
        database_name: str,
        schema_name: str,
        object_name: str,
        fqn: str,
        object_type: str,
        row_count: int | None,
        size_bytes: int | None,
        usage_reads: int,
        is_dead: bool,
        definition_sql: str | None,
        content_hash: str,
        actor: str,
    ) -> DbObject:
        obj = (
            db.query(DbObject)
            .filter(DbObject.project_id == project_id, DbObject.fqn == fqn)
            .first()
        )
        if obj is None:
            obj = DbObject(
                project_id=project_id,
                source_artifact_id=source_artifact_id,
                database_name=database_name,
                schema_name=schema_name,
                object_name=object_name,
                fqn=fqn,
                object_type=object_type,
                row_count=row_count,
                size_bytes=size_bytes,
                usage_reads_30d=usage_reads,
                is_dead_candidate=is_dead,
                definition_sql=definition_sql,
                content_hash=content_hash,
                created_by=actor,
            )
            db.add(obj)
        else:
            obj.source_artifact_id = source_artifact_id
            obj.row_count = row_count
            obj.size_bytes = size_bytes
            obj.usage_reads_30d = usage_reads
            obj.is_dead_candidate = is_dead
            obj.definition_sql = definition_sql
            obj.content_hash = content_hash
        db.flush()
        return obj
