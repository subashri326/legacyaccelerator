"""Shared catalog harvesting logic: live DB queries, DuckDB/SQLite inspection, and offline DDL/JSON bundles."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.core.secrets import resolve_connection_uri
from accelerator.ir.models import CatalogIR, ColumnIR, ProcedureIR, TableIR, ViewIR
from accelerator.plugins.interfaces import SourceCollector


def parse_ddl_to_catalog(
    ddl_text: str,
    source_system: str,
    dialect: str,
    database_name: str = "legacy_db",
) -> CatalogIR:
    """Parse offline SQL DDL/view/procedure script into CatalogIR using sqlglot + pattern extraction."""
    tables: list[TableIR] = []
    views: list[ViewIR] = []
    procedures: list[ProcedureIR] = []

    # Split statements safely
    parsed_stmts: list[exp.Expression | None] = []
    try:
        parsed_stmts = sqlglot.parse(ddl_text, read=dialect)
    except (SqlglotError, ValueError):
        parsed_stmts = []

    for stmt in parsed_stmts:
        if stmt is None:
            continue
        if isinstance(stmt, exp.Create):
            kind = str(stmt.args.get("kind") or "").upper()
            this = stmt.this
            if kind in ("TABLE", "") and isinstance(this, exp.Schema):
                tbl_expr = this.this
                schema_name = tbl_expr.db or "dbo" if isinstance(tbl_expr, exp.Table) else "dbo"
                tbl_name = tbl_expr.name if isinstance(tbl_expr, exp.Table) else str(tbl_expr)
                cols: list[ColumnIR] = []
                pk_cols: set[str] = set()
                fk_map: dict[str, str] = {}

                for expr in this.expressions:
                    if isinstance(expr, exp.PrimaryKey):
                        for pk_col in expr.expressions:
                            pk_cols.add(pk_col.name.lower())
                    elif isinstance(expr, exp.ForeignKey):
                        local_cols = [c.name.lower() for c in expr.expressions]
                        ref = expr.args.get("reference")
                        ref_name = ref.this.sql() if ref and ref.this else "unknown"
                        for lc in local_cols:
                            fk_map[lc] = ref_name

                pos = 1
                for expr in this.expressions:
                    if not isinstance(expr, exp.ColumnDef):
                        continue
                    col_name = expr.name
                    dtype = expr.args.get("kind")
                    dtype_str = dtype.sql(dialect=dialect) if dtype else "VARCHAR"
                    constraints = expr.args.get("constraints") or []
                    is_pk = col_name.lower() in pk_cols
                    nullable = True
                    for c in constraints:
                        if isinstance(c.kind, exp.PrimaryKeyColumnConstraint):
                            is_pk = True
                            nullable = False
                        elif isinstance(c.kind, exp.NotNullColumnConstraint):
                            nullable = False
                    cols.append(
                        ColumnIR(
                            name=col_name,
                            data_type=dtype_str,
                            nullable=nullable,
                            is_primary_key=is_pk,
                            is_foreign_key=col_name.lower() in fk_map,
                            fk_target=fk_map.get(col_name.lower()),
                            ordinal_position=pos,
                        )
                    )
                    pos += 1

                tables.append(
                    TableIR(
                        database=database_name,
                        schema_name=schema_name,
                        name=tbl_name,
                        object_type="TABLE",
                        columns=cols,
                        source_sql=stmt.sql(dialect=dialect),
                    )
                )
            elif kind == "VIEW":
                tbl_expr = this.this if isinstance(this, exp.Schema) else this
                schema_name = tbl_expr.db or "dbo" if isinstance(tbl_expr, exp.Table) else "dbo"
                view_name = tbl_expr.name if isinstance(tbl_expr, exp.Table) else str(tbl_expr)
                inner = stmt.expression
                refs = [t.sql() for t in inner.find_all(exp.Table)] if inner else []
                views.append(
                    ViewIR(
                        database=database_name,
                        schema_name=schema_name,
                        name=view_name,
                        definition_sql=stmt.sql(dialect=dialect),
                        neutral_sql=inner.sql(dialect="postgres") if inner else "",
                        referenced_tables=sorted(set(refs)),
                    )
                )

    # Extract CREATE PROCEDURE blocks if present (often outside sqlglot's standard CREATE TABLE)
    import re

    proc_pattern = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?PROC(?:EDURE)?\s+([A-Za-z0-9_\.\[\]]+)(.*?)(?=\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC|TABLE|VIEW)|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in proc_pattern.finditer(ddl_text):
        raw_name = match.group(1).replace("[", "").replace("]", "")
        parts = raw_name.split(".")
        schema_name = parts[-2] if len(parts) >= 2 else "dbo"
        proc_name = parts[-1]
        body = match.group(0).strip()
        refs = sorted(
            set(
                re.findall(
                    r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_]+\.[A-Za-z0-9_]+)",
                    body,
                    flags=re.IGNORECASE,
                )
            )
        )
        targets = sorted(
            set(
                re.findall(
                    r"\b(?:INSERT\s+INTO|UPDATE|MERGE\s+INTO)\s+([A-Za-z0-9_]+\.[A-Za-z0-9_]+)",
                    body,
                    flags=re.IGNORECASE,
                )
            )
        )
        procedures.append(
            ProcedureIR(
                database=database_name,
                schema_name=schema_name,
                name=proc_name,
                source_dialect=dialect,
                definition_sql=body,
                referenced_tables=refs,
                target_tables=targets,
                has_dynamic_sql=bool(re.search(r"\bEXEC(?:UTE)?\s*\(|sp_executesql|EXECUTE\s+IMMEDIATE", body, re.IGNORECASE)),
                has_cursors=bool(re.search(r"\bCURSOR\b", body, re.IGNORECASE)),
            )
        )

    return CatalogIR(
        source_system=source_system,
        database_name=database_name,
        tables=tables,
        views=views,
        procedures=procedures,
    )


def harvest_duckdb_catalog(
    db_path: str,
    source_system: str,
    schemas: list[str] | None = None,
) -> CatalogIR:
    """Harvest real tables, columns, row counts, and views from a DuckDB file."""
    clean_path = db_path.removeprefix("duckdb:///")
    conn = duckdb.connect(clean_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
            ORDER BY table_schema, table_name
            """
        ).fetchall()

        tables: list[TableIR] = []
        views: list[ViewIR] = []
        allowed = {s.lower() for s in schemas} if schemas else None

        for schema_name, table_name, table_type in rows:
            if allowed and schema_name.lower() not in allowed:
                continue
            col_rows = conn.execute(
                """
                SELECT column_name, data_type, is_nullable, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                ORDER BY ordinal_position
                """,
                [schema_name, table_name],
            ).fetchall()
            cols = [
                ColumnIR(
                    name=c_name,
                    data_type=c_type,
                    nullable=(str(is_null).upper() == "YES"),
                    is_primary_key=(idx == 0 and c_name.lower().endswith("id")),
                    ordinal_position=int(ord_pos),
                )
                for idx, (c_name, c_type, is_null, ord_pos) in enumerate(col_rows)
            ]
            if "VIEW" in str(table_type).upper():
                views.append(
                    ViewIR(
                        database=Path(clean_path).stem,
                        schema_name=schema_name,
                        name=table_name,
                        definition_sql=f"SELECT * FROM {schema_name}.{table_name}",
                        referenced_tables=[f"{schema_name}.{table_name}"],
                    )
                )
            else:
                cnt_rows = conn.execute(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"').fetchall()
                row_count = int(cnt_rows[0][0]) if cnt_rows else 0
                tables.append(
                    TableIR(
                        database=Path(clean_path).stem,
                        schema_name=schema_name,
                        name=table_name,
                        object_type="TABLE",
                        columns=cols,
                        row_count=row_count,
                        size_bytes=row_count * max(len(cols), 1) * 32,
                    )
                )

        return CatalogIR(
            source_system=source_system,
            database_name=Path(clean_path).stem,
            tables=tables,
            views=views,
        )
    finally:
        conn.close()


def harvest_sqlite_catalog(
    db_path: str,
    source_system: str,
) -> CatalogIR:
    """Harvest real tables, columns, PKs, and row counts from a SQLite file."""
    clean_path = db_path.removeprefix("sqlite:///")
    conn = sqlite3.connect(f"file:{clean_path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        tbl_rows = cur.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables: list[TableIR] = []
        views: list[ViewIR] = []
        for name, obj_type, sql_text in tbl_rows:
            if "." in name:
                schema_name, tbl_name = name.split(".", 1)
            else:
                schema_name, tbl_name = "dbo", name
            if obj_type == "view":
                views.append(
                    ViewIR(
                        database=Path(clean_path).stem,
                        schema_name=schema_name,
                        name=tbl_name,
                        definition_sql=sql_text or "",
                    )
                )
                continue
            pragma = cur.execute(f'PRAGMA table_info("{name}")').fetchall()
            cols = [
                ColumnIR(
                    name=col[1],
                    data_type=col[2] or "VARCHAR",
                    nullable=(col[3] == 0),
                    is_primary_key=(col[5] > 0),
                    ordinal_position=int(col[0]) + 1,
                )
                for col in pragma
            ]
            cnt = cur.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            tables.append(
                TableIR(
                    database=Path(clean_path).stem,
                    schema_name=schema_name,
                    name=tbl_name,
                    object_type="TABLE",
                    columns=cols,
                    row_count=int(cnt),
                    size_bytes=int(cnt) * max(len(cols), 1) * 32,
                    source_sql=sql_text,
                )
            )
        return CatalogIR(
            source_system=source_system,
            database_name=Path(clean_path).stem,
            tables=tables,
            views=views,
        )
    finally:
        conn.close()


def load_usage_csv_or_json(usage_log_path: str | None) -> dict[str, int]:
    """Read optional query-log usage counts to identify dead/unused tables."""
    if not usage_log_path:
        return {}
    path = Path(usage_log_path)
    if not path.exists():
        return {}
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).lower(): int(v) for k, v in raw.items()}
    usage: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            fqn = str(row.get("object_fqn") or row.get("table") or "").strip().lower()
            reads = int(row.get("reads_30d") or row.get("reads") or 0)
            if fqn:
                usage[fqn] = reads
    return usage


class BaseSqlCollector(SourceCollector):
    """Shared base for SQL Server, Oracle, Teradata catalog collectors."""

    dialect: str = "tsql"
    source_system: str = "sqlserver"

    def collect_catalog(
        self,
        connection_uri: str,
        schemas: list[str] | None = None,
        catalog_bundle_path: str | None = None,
    ) -> CatalogIR:
        if catalog_bundle_path:
            bundle = Path(catalog_bundle_path)
            if bundle.suffix.lower() == ".json":
                return CatalogIR.model_validate_json(bundle.read_text(encoding="utf-8"))
            if bundle.suffix.lower() in (".duckdb", ".db"):
                return harvest_duckdb_catalog(str(bundle), self.source_system, schemas)
            if bundle.suffix.lower() in (".sqlite", ".sqlite3"):
                return harvest_sqlite_catalog(str(bundle), self.source_system)
            ddl_text = bundle.read_text(encoding="utf-8")
            return parse_ddl_to_catalog(ddl_text, self.source_system, self.dialect, bundle.stem)

        resolved = resolve_connection_uri(connection_uri)
        if resolved.startswith("duckdb:///"):
            return harvest_duckdb_catalog(resolved, self.source_system, schemas)
        if resolved.startswith("sqlite:///"):
            return harvest_sqlite_catalog(resolved, self.source_system)
        return self._collect_live_enterprise(resolved, schemas)

    def _collect_live_enterprise(
        self, resolved_uri: str, schemas: list[str] | None = None
    ) -> CatalogIR:
        raise NotImplementedError("Override in specific enterprise DB collector")

    def collect_usage(self, connection_uri: str, usage_log_path: str | None = None) -> dict[str, int]:
        return load_usage_csv_or_json(usage_log_path)
