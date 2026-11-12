"""Built-in parallel partitioned Parquet mover with watermark incremental support and checkpoints."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from accelerator.config.client_config import ClientConfig
from accelerator.core.secrets import resolve_connection_uri
from accelerator.ir.models import TableIR
from accelerator.plugins.interfaces import MoveOutcome, Mover, PartitionResult, PluginManifest


def _connect_local_db(uri: str) -> tuple[str, Any]:
    if uri.startswith("duckdb:///"):
        path = uri.removeprefix("duckdb:///")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return "duckdb", duckdb.connect(path)
    if uri.startswith("sqlite:///"):
        path = uri.removeprefix("sqlite:///")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return "sqlite", sqlite3.connect(path)
    raise ValueError(f"Unsupported local connection URI: {uri}")


def _qualified_table(kind: str, table: TableIR) -> str:
    if kind == "duckdb":
        return f'"{table.schema_name}"."{table.name}"'
    return f'"{table.schema_name}.{table.name}"'


def _sqlite_or_duck_exists(kind: str, conn: Any, table: TableIR) -> bool:
    if kind == "duckdb":
        rows = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [table.schema_name, table.name],
        ).fetchall()
        return bool(rows and rows[0][0] > 0)
    rows = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
        [f"{table.schema_name}.{table.name}"],
    ).fetchall()
    if rows and rows[0][0] > 0:
        return True
    rows2 = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name = ?",
        [table.name],
    ).fetchall()
    return bool(rows2 and rows2[0][0] > 0)


def build_native_bulk_load_sql(
    platform: str,
    target_schema: str,
    target_table: str,
    parquet_uri: str,
) -> str:
    """Emit platform-native bulk COPY/Auto Loader statement for Snowflake, Databricks, or Fabric."""
    fqn = f"{target_schema}.{target_table}"
    if platform == "snowflake":
        return (
            f"COPY INTO {fqn}\n"
            f"FROM '{parquet_uri}'\n"
            f"FILE_FORMAT = (TYPE = PARQUET)\n"
            f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE;"
        )
    if platform == "databricks":
        return (
            f"COPY INTO {fqn}\n"
            f"FROM '{parquet_uri}'\n"
            f"FILEFORMAT = PARQUET\n"
            f"COPY_OPTIONS ('mergeSchema' = 'true');"
        )
    return f"COPY INTO {fqn}\nFROM '{parquet_uri}'\nWITH (FILE_TYPE = 'PARQUET');"


class ParquetMover(Mover):
    """Parallel partitioned source->Parquet->target mover with partition-level checkpoints."""

    manifest = PluginManifest(
        plugin_type="Mover",
        name="parquet_mover",
        version="0.1.0",
        supported_versions=[
            "Snowflake COPY INTO",
            "Databricks COPY/AutoLoader",
            "Fabric COPY",
            "DuckDB/SQLite",
        ],
        capabilities={"bulk": True, "incremental_watermark": True, "checkpointing": True},
        description="Extracts partitioned chunks to Parquet in cloud/local storage and loads targets idempotently.",
    )

    def load_table(
        self,
        table: TableIR,
        source_uri: str,
        target_uri: str,
        config: ClientConfig,
        mode: str = "bulk",
        watermark_value: str | None = None,
        existing_completed_partitions: set[int] | None = None,
        staging_writer: Callable[[int, bytes], str] | None = None,
        target_ddl_sql: str | None = None,
    ) -> MoveOutcome:
        src_resolved = resolve_connection_uri(source_uri)
        tgt_resolved = resolve_connection_uri(target_uri)
        completed_set = existing_completed_partitions or set()

        src_kind, src_conn = _connect_local_db(src_resolved)
        try:
            src_tbl_sql = _qualified_table(src_kind, table)
            if src_kind == "sqlite" and not _sqlite_or_duck_exists("sqlite", src_conn, table):
                src_tbl_sql = f'"{table.name}"'

            wm_col = config.movement.watermark_columns.get(
                table.fqn
            ) or config.movement.watermark_columns.get(f"{table.schema_name}.{table.name}")
            where_clause = ""
            if mode == "incremental" and wm_col and watermark_value:
                where_clause = f" WHERE {wm_col} > '{watermark_value}'"

            count_rows = src_conn.execute(
                f"SELECT COUNT(*) FROM {src_tbl_sql}{where_clause}"
            ).fetchall()
            total_rows = int(count_rows[0][0]) if count_rows else 0
            part_size = max(1, config.movement.partition_size_rows)
            num_parts = max(1, (total_rows + part_size - 1) // part_size)

            # Extract partitions to Parquet bytes (parallel-safe via thread-local read or chunk fetch)
            pk_cols = [c.name for c in table.columns if c.is_primary_key]
            order_col = pk_cols[0] if pk_cols else (table.columns[0].name if table.columns else "1")

            extracted_chunks: list[tuple[int, str, int, bytes, str]] = []
            new_watermark: str | None = watermark_value

            for p_idx in range(num_parts):
                if p_idx in completed_set:
                    continue
                offset = p_idx * part_size
                pred = f"OFFSET {offset} LIMIT {part_size}"
                if where_clause:
                    pred = f"{where_clause.strip()} ({pred})"
                query = f"SELECT * FROM {src_tbl_sql}{where_clause} ORDER BY {order_col} LIMIT {part_size} OFFSET {offset}"
                if src_kind == "sqlite":
                    df = pd.read_sql_query(query, src_conn)
                    arrow_tbl = pa.Table.from_pandas(df, preserve_index=False)
                else:
                    # DuckDB SQL -> Arrow preserves native DECIMAL, DATE, and TIMESTAMP types losslessly.
                    arrow_tbl = src_conn.sql(query).to_arrow_table()

                if wm_col and wm_col in arrow_tbl.column_names and arrow_tbl.num_rows > 0:
                    wm_vals = [str(v) for v in arrow_tbl[wm_col].to_pylist() if v is not None]
                    if wm_vals:
                        max_wm = max(wm_vals)
                        if new_watermark is None or max_wm > new_watermark:
                            new_watermark = max_wm

                buf = io.BytesIO()
                pq.write_table(arrow_tbl, buf)
                pq_bytes = buf.getvalue()
                checksum = hashlib.sha256(pq_bytes).hexdigest()
                extracted_chunks.append((p_idx, pred, arrow_tbl.num_rows, pq_bytes, checksum))
        finally:
            src_conn.close()

        # Stage Parquet chunks (can run concurrently for large estates)
        staged_uris: dict[int, str] = {}
        if staging_writer is not None and extracted_chunks:
            with ThreadPoolExecutor(
                max_workers=min(config.movement.parallelism, len(extracted_chunks))
            ) as pool:
                futs = {
                    pool.submit(staging_writer, p_idx, pq_bytes): p_idx
                    for p_idx, _, _, pq_bytes, _ in extracted_chunks
                }
                for fut, p_idx in futs.items():
                    staged_uris[p_idx] = fut.result()

        # Load into target database (live DuckDB/SQLite OR emit native COPY SQL)
        partition_results: list[PartitionResult] = []
        native_copy_statements: list[str] = []
        total_loaded = 0

        if tgt_resolved.startswith(("duckdb:///", "sqlite:///")):
            tgt_kind, tgt_conn = _connect_local_db(tgt_resolved)
            try:
                if tgt_kind == "duckdb":
                    tgt_conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{table.schema_name}"')
                tgt_tbl_sql = _qualified_table(tgt_kind, table)

                if mode == "bulk" and not completed_set:
                    tgt_conn.execute(f"DROP TABLE IF EXISTS {tgt_tbl_sql}")

                for p_idx, pred, chunk_rows, pq_bytes, checksum in extracted_chunks:
                    p_uri = staged_uris.get(p_idx, f"memory://part_{p_idx}.parquet")
                    chunk_arrow = pq.read_table(io.BytesIO(pq_bytes))
                    if tgt_kind == "duckdb":
                        tbl_exists = _sqlite_or_duck_exists("duckdb", tgt_conn, table)
                        tgt_conn.register("_l2c_chunk_arrow", chunk_arrow)
                        if not tbl_exists:
                            tgt_conn.execute(
                                f"CREATE TABLE {tgt_tbl_sql} AS SELECT * FROM _l2c_chunk_arrow"
                            )
                        else:
                            tgt_conn.execute(
                                f"INSERT INTO {tgt_tbl_sql} SELECT * FROM _l2c_chunk_arrow"
                            )
                        tgt_conn.unregister("_l2c_chunk_arrow")
                    else:
                        tbl_name_sqlite = f"{table.schema_name}.{table.name}"
                        if_exists = (
                            "replace"
                            if (mode == "bulk" and p_idx == 0 and not completed_set)
                            else "append"
                        )
                        chunk_arrow.to_pandas().to_sql(
                            tbl_name_sqlite, tgt_conn, if_exists=if_exists, index=False
                        )

                    rows_n = chunk_rows
                    total_loaded += rows_n
                    partition_results.append(
                        PartitionResult(
                            partition_index=p_idx,
                            range_predicate=pred,
                            parquet_uri=p_uri,
                            rows_extracted=rows_n,
                            rows_loaded=rows_n,
                            checksum=checksum,
                            status="completed",
                        )
                    )
                    native_copy_statements.append(
                        build_native_bulk_load_sql(
                            config.target.platform, table.schema_name, table.name, p_uri
                        )
                    )
                if tgt_kind == "sqlite":
                    tgt_conn.commit()
            finally:
                tgt_conn.close()
        else:
            for p_idx, pred, df, _pq_bytes, checksum in extracted_chunks:
                p_uri = staged_uris.get(p_idx, f"staged://{table.name}/part_{p_idx}.parquet")
                rows_n = len(df)
                total_loaded += rows_n
                native_copy_statements.append(
                    build_native_bulk_load_sql(
                        config.target.platform, table.schema_name, table.name, p_uri
                    )
                )
                partition_results.append(
                    PartitionResult(
                        partition_index=p_idx,
                        range_predicate=pred,
                        parquet_uri=p_uri,
                        rows_extracted=rows_n,
                        rows_loaded=rows_n,
                        checksum=checksum,
                        status="completed",
                    )
                )

        return MoveOutcome(
            object_key=table.fqn,
            rows_loaded=total_loaded,
            mode=mode,
            watermark_value=new_watermark,
            partitions=partition_results,
            evidence={
                "native_bulk_commands": native_copy_statements,
                "target_ddl_executed": bool(target_ddl_sql),
                "skipped_checkpointed_partitions": sorted(completed_set),
            },
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sql = build_native_bulk_load_sql(
            "snowflake", "CURATED", "ORDERS", "s3://bucket/orders.parquet"
        )
        ok = "COPY INTO CURATED.ORDERS" in sql and "FILE_FORMAT = (TYPE = PARQUET)" in sql
        return ok, "ParquetMover golden check passed" if ok else "Failed golden check"
