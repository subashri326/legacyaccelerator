"""Built-in mover: source -> Parquet staging -> target, chunk by chunk, restartable.

How a table is moved:

1. The generated target DDL (from Convert) is run on the target if the table doesn't exist yet,
   so the target gets the converted names and types. Without it the load refuses to start.
2. Rows are read in key order, one chunk at a time (``WHERE key > last ORDER BY key LIMIT n``).
   Each chunk is staged as Parquet (evidence + cloud COPY INTO), written to the target in one
   transaction, and reported through ``on_partition`` so the engine can checkpoint it at once.
3. Bulk: the target is emptied first, unless resuming - then loading continues after the
   highest key already on the target, so nothing is loaded twice.
4. Incremental: rows changed since the last watermark go to a staging table, then replace their
   old versions by key in one transaction (delete + insert). Needs a watermark column and a
   single-column primary key; without them the load refuses rather than duplicating rows.
   Rows deleted at the source are not detected by watermark loads.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from sqlglot import exp

from accelerator.config.client_config import ClientConfig
from accelerator.connectors import Connector, open_connector
from accelerator.connectors.base import arrow_to_parquet_bytes
from accelerator.core.exceptions import ConfigError
from accelerator.ir.expressions import DIALECT_MAP
from accelerator.ir.models import TableIR
from accelerator.plugins.interfaces import MoveOutcome, Mover, PartitionResult, PluginManifest

STAGING_SUFFIX = "__L2C_STG"


def build_native_bulk_load_sql(
    platform: str, target_schema: str, target_table: str, parquet_uri: str
) -> str:
    """Platform-native bulk load of one staged Parquet file."""
    fqn = f"{target_schema}.{target_table}"
    if platform == "snowflake":
        return (
            f"COPY INTO {fqn}\nFROM '{parquet_uri}'\nFILE_FORMAT = (TYPE = PARQUET)\n"
            "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;"
        )
    if platform == "databricks":
        return f"COPY INTO {fqn}\nFROM '{parquet_uri}'\nFILEFORMAT = PARQUET\nCOPY_OPTIONS ('mergeSchema' = 'false');"
    return f"COPY INTO {fqn}\nFROM '{parquet_uri}'\nWITH (FILE_TYPE = 'PARQUET');"


def _is_cloud_uri(uri: str | None) -> bool:
    return bool(uri) and uri.startswith(("s3://", "abfs://", "abfss://", "gs://"))  # type: ignore[union-attr]


def _single_key(table: TableIR) -> str | None:
    pks = [c.name for c in table.columns if c.is_primary_key]
    return pks[0] if len(pks) == 1 else None


def _page_query(
    src: Connector,
    table: TableIR,
    key: str | None,
    after_key: Any,
    wm_col: str | None,
    wm_value: Any,
    size: int,
    offset: int,
) -> str:
    cols = [exp.column(src.quote(c.name)) for c in table.columns]
    q = exp.select(*cols).from_(
        exp.table_(
            src.quote(table.name), db=src.quote(table.schema_name) if table.schema_name else None
        )
    )
    conds: list[exp.Expression] = []
    if wm_col and wm_value is not None:
        conds.append(exp.column(src.quote(wm_col)) > exp.convert(wm_value))
    if key and after_key is not None:
        conds.append(exp.column(src.quote(key)) > exp.convert(after_key))
    if conds:
        q = q.where(exp.and_(*conds))
    order = [exp.column(src.quote(key))] if key else cols
    if not key and offset and src.dialect == "teradata":
        # Teradata has no OFFSET: number the rows and keep this page's range.
        rn = exp.Window(this=exp.RowNumber(), order=exp.Order(expressions=[c.copy() for c in cols]))
        q.set(
            "qualify",
            exp.Qualify(
                this=exp.Between(
                    this=rn,
                    low=exp.Literal.number(offset + 1),
                    high=exp.Literal.number(offset + size),
                )
            ),
        )
        return src.render(q.order_by(*order))
    q = q.order_by(*order).limit(size)
    if not key and offset:
        q = q.offset(offset)
    return src.render(q)


def _ensure_target_table(tgt: Connector, tt: TableIR, ddl_sql: str | None, platform: str) -> bool:
    """Create schema + table from the converted DDL if missing. Returns True if DDL was executed."""
    tgt.create_schema(tt.schema_name)
    if tgt.table_exists(tt.schema_name, tt.name):
        return False
    if not ddl_sql:
        raise ConfigError(
            f"No converted DDL for {tt.schema_name}.{tt.name}: run Convert before Move so the "
            "target table is created with the converted names and types."
        )
    platform_dialect = DIALECT_MAP.get(platform, platform)
    for stmt in tgt.transpile(ddl_sql, read=platform_dialect):
        if stmt.lstrip().upper().startswith("CREATE SCHEMA"):
            continue  # handled by create_schema (syntax differs per engine)
        tgt.execute(stmt)
    return True


class ParquetMover(Mover):
    """Chunked, checkpointed source -> Parquet -> target mover for any supported engine."""

    manifest = PluginManifest(
        plugin_type="Mover",
        name="parquet_mover",
        version="0.2.0",
        supported_versions=[
            "SQL Server",
            "PostgreSQL",
            "Snowflake",
            "Databricks",
            "Fabric",
            "DuckDB",
            "SQLite",
        ],
        capabilities={
            "bulk": True,
            "incremental_merge": True,
            "checkpointing": True,
            "resume": True,
        },
        description="Keyset-paged extraction, Parquet staging, per-chunk checkpoints, incremental upserts.",
    )

    def load_table(
        self,
        table: TableIR,
        source_uri: str,
        target_uri: str,
        config: ClientConfig,
        mode: str = "bulk",
        watermark_value: str | None = None,
        resume: bool = False,
        start_partition_index: int = 0,
        staging_writer: Callable[[int, bytes], str] | None = None,
        target_ddl_sql: str | None = None,
        target_table: TableIR | None = None,
        on_partition: Callable[[PartitionResult], None] | None = None,
    ) -> MoveOutcome:
        tt = target_table or table
        if len(tt.columns) != len(table.columns):
            raise ConfigError("Target table must have the same columns as the source table")
        platform = config.require_target()
        key = _single_key(table)
        key_target = tt.columns[[c.name for c in table.columns].index(key)].name if key else None
        wm_col = config.movement.watermark_columns.get(
            table.fqn
        ) or config.movement.watermark_columns.get(f"{table.schema_name}.{table.name}")
        if mode == "incremental" and (not wm_col or not key):
            raise ConfigError(
                f"Incremental load of {table.fqn} needs a watermark column (movement.watermark_columns) "
                "and a single-column primary key. Use a bulk load instead."
            )
        size = max(1, config.movement.partition_size_rows)

        with open_connector(source_uri, read_only=True) as src, open_connector(target_uri) as tgt:
            ddl_executed = _ensure_target_table(tgt, tt, target_ddl_sql, platform)
            write_name = tt.name
            after_key: Any = None
            if mode == "bulk":
                if resume and key_target:
                    rows = tgt.query(
                        f"SELECT MAX({tgt.quote(key_target)}) FROM {tgt.qualified(tt.schema_name, tt.name)}"
                    )
                    after_key = rows[0][0] if rows else None
                else:
                    tgt.truncate(tt.schema_name, tt.name)
                    start_partition_index = 0
            else:
                write_name = f"{tt.name}{STAGING_SUFFIX}"
                tgt.drop_table(tt.schema_name, write_name)
                tgt.create_empty_like(tt.schema_name, write_name, tt.schema_name, tt.name)
                start_partition_index = 0

            partitions: list[PartitionResult] = []
            native_cmds: list[str] = []
            total = 0
            new_wm: Any = watermark_value
            idx = start_partition_index
            offset = 0
            target_names = [c.name for c in tt.columns]
            while True:
                sql = _page_query(
                    src,
                    table,
                    key,
                    after_key,
                    wm_col if mode == "incremental" else None,
                    watermark_value if mode == "incremental" else None,
                    size,
                    offset,
                )
                batch = src.fetch_arrow(sql)
                if batch.num_rows == 0:
                    break
                batch = batch.rename_columns(target_names)
                pq_bytes = arrow_to_parquet_bytes(batch)
                checksum = hashlib.sha256(pq_bytes).hexdigest()
                uri = staging_writer(idx, pq_bytes) if staging_writer else None
                if _is_cloud_uri(uri) and tgt.kind in ("databricks", "fabric") and mode == "bulk":
                    cmd = build_native_bulk_load_sql(
                        platform, tt.schema_name, write_name, uri or ""
                    )
                    tgt.execute(cmd.rstrip(";"))
                    written = batch.num_rows
                else:
                    written = tgt.write_arrow(batch, tt.schema_name, write_name)
                if uri:
                    native_cmds.append(
                        build_native_bulk_load_sql(platform, tt.schema_name, tt.name, uri)
                    )

                if key:
                    prev = after_key
                    after_key = batch.column(key_target)[-1].as_py()
                    predicate = (
                        f"{key} > {prev!r} AND {key} <= {after_key!r}"
                        if prev is not None
                        else f"{key} <= {after_key!r}"
                    )
                else:
                    predicate = f"rows {offset + 1}-{offset + batch.num_rows} (no single-column key: offset paging)"
                    offset += batch.num_rows
                if wm_col:
                    wm_target = (
                        target_names[[c.name for c in table.columns].index(wm_col)]
                        if wm_col in [c.name for c in table.columns]
                        else None
                    )
                    if wm_target:
                        batch_max = pc.max(batch.column(wm_target)).as_py()
                        if batch_max is not None and (
                            new_wm is None or str(batch_max) > str(new_wm)
                        ):
                            new_wm = batch_max
                part = PartitionResult(
                    partition_index=idx,
                    range_predicate=predicate,
                    parquet_uri=uri or "",
                    rows_extracted=batch.num_rows,
                    rows_loaded=written,
                    checksum=checksum,
                    status="completed",
                )
                partitions.append(part)
                if on_partition:
                    on_partition(part)
                total += written
                idx += 1
                if batch.num_rows < size:
                    break

            if mode == "incremental":
                t = tgt.qualified(tt.schema_name, tt.name)
                s = tgt.qualified(tt.schema_name, write_name)
                k = tgt.quote(key_target or "")
                tgt.execute_in_transaction(
                    [
                        f"DELETE FROM {t} WHERE EXISTS (SELECT 1 FROM {s} WHERE {s}.{k} = {t}.{k})",
                        f"INSERT INTO {t} SELECT * FROM {s}",
                    ]
                )
                tgt.drop_table(tt.schema_name, write_name)

        return MoveOutcome(
            object_key=table.fqn,
            rows_loaded=total,
            mode=mode,
            watermark_value=str(new_wm) if new_wm is not None else None,
            partitions=partitions,
            evidence={
                "target_table": f"{tt.schema_name}.{tt.name}",
                "target_ddl_executed": ddl_executed,
                "paging": f"keyset on {key}" if key else "offset (no single-column primary key)",
                "resumed_after_key": None if not resume else str(after_key),
                "native_bulk_commands": native_cmds,
            },
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sql = build_native_bulk_load_sql(
            "snowflake", "CURATED", "ORDERS", "s3://bucket/orders.parquet"
        )
        ok = "COPY INTO CURATED.ORDERS" in sql and "FILE_FORMAT = (TYPE = PARQUET)" in sql
        return ok, "ParquetMover golden check passed" if ok else "Failed golden check"


__all__ = ["ParquetMover", "build_native_bulk_load_sql", "pa"]
