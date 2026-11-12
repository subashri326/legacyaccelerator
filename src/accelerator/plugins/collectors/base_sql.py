"""Shared catalog harvesting logic: live DB queries, DuckDB/SQLite inspection, and offline DDL/JSON bundles."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.core.exceptions import ConfigError
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
                has_dynamic_sql=bool(
                    re.search(
                        r"\bEXEC(?:UTE)?\s*\(|sp_executesql|EXECUTE\s+IMMEDIATE",
                        body,
                        re.IGNORECASE,
                    )
                ),
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
    """Harvest tables, columns, real primary/foreign keys, row counts and views from DuckDB."""
    clean_path = db_path.removeprefix("duckdb:///")
    conn = duckdb.connect(clean_path, read_only=True)
    db_name = Path(clean_path).stem
    try:
        allowed = {s.lower() for s in schemas} if schemas else None
        pks: dict[tuple[str, str], set[str]] = {}
        fks: dict[tuple[str, str, str], str] = {}
        for sch, tbl, ctype, cols, ref_tbl, ref_cols in conn.execute(
            "SELECT schema_name, table_name, constraint_type, constraint_column_names, "
            "referenced_table, referenced_column_names FROM duckdb_constraints()"
        ).fetchall():
            if ctype == "PRIMARY KEY":
                pks[(sch, tbl)] = {c.lower() for c in cols}
            elif ctype == "FOREIGN KEY" and ref_tbl:
                for c, rc in zip(cols, ref_cols or [], strict=False):
                    fks[(sch, tbl, c.lower())] = f"{sch}.{ref_tbl}({rc})"
        tables: list[TableIR] = []
        views: list[ViewIR] = []
        for sch, name, ttype in conn.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') ORDER BY 1, 2"
        ).fetchall():
            if allowed and sch.lower() not in allowed:
                continue
            if "VIEW" in str(ttype).upper():
                sql = conn.execute(
                    "SELECT sql FROM duckdb_views() WHERE schema_name = ? AND view_name = ?",
                    [sch, name],
                ).fetchone()
                views.append(
                    ViewIR(
                        database=db_name,
                        schema_name=sch,
                        name=name,
                        definition_sql=(sql[0] if sql else ""),
                    )
                )
                continue
            col_rows = conn.execute(
                "SELECT column_name, data_type, is_nullable, ordinal_position FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [sch, name],
            ).fetchall()
            key = pks.get((sch, name), set())
            cols = [
                ColumnIR(
                    name=c,
                    data_type=t,
                    nullable=str(n).upper() == "YES" and c.lower() not in key,
                    is_primary_key=c.lower() in key,
                    is_foreign_key=(sch, name, c.lower()) in fks,
                    fk_target=fks.get((sch, name, c.lower())),
                    ordinal_position=int(pos),
                )
                for c, t, n, pos in col_rows
            ]
            count = int(conn.execute(f'SELECT COUNT(*) FROM "{sch}"."{name}"').fetchone()[0])
            tables.append(
                TableIR(database=db_name, schema_name=sch, name=name, columns=cols, row_count=count)
            )
        return CatalogIR(
            source_system=source_system, database_name=db_name, tables=tables, views=views
        )
    finally:
        conn.close()


_SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "sys",
    "guest",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
}


def _row_count(conn: Any, dialect: str, schema: str, name: str) -> int | None:
    """Cheap row count from catalog statistics where the engine has them, else COUNT(*)."""
    from sqlalchemy import text

    try:
        if dialect == "mssql":
            r = conn.execute(
                text(
                    "SELECT SUM(p.rows) FROM sys.partitions p JOIN sys.tables t ON p.object_id = t.object_id "
                    "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                    "WHERE s.name = :s AND t.name = :t AND p.index_id IN (0, 1)"
                ),
                {"s": schema, "t": name},
            ).scalar()
            if r is not None:
                return int(r)
        if dialect == "postgresql":
            r = conn.execute(
                text(
                    "SELECT c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :s AND c.relname = :t"
                ),
                {"s": schema, "t": name},
            ).scalar()
            if r is not None and r >= 0:
                return int(r)
        q = (
            f'SELECT COUNT(*) FROM "{schema}"."{name}"'
            if dialect != "mssql"
            else f"SELECT COUNT(*) FROM [{schema}].[{name}]"
        )
        return int(conn.execute(text(q)).scalar() or 0)
    except Exception:
        return None


def _procedures(
    conn: Any, dialect: str, db_name: str, allowed: set[str] | None
) -> list[ProcedureIR]:
    from sqlalchemy import text

    queries = {
        "mssql": "SELECT s.name, p.name, m.definition FROM sys.procedures p "
        "JOIN sys.schemas s ON p.schema_id = s.schema_id JOIN sys.sql_modules m ON m.object_id = p.object_id",
        "postgresql": "SELECT n.nspname, p.proname, pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prokind = 'p' "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
    }
    if dialect not in queries:
        return []
    out: list[ProcedureIR] = []
    try:
        rows = conn.execute(text(queries[dialect])).fetchall()
    except Exception:
        return []
    src_dialect = {"mssql": "tsql", "postgresql": "postgres"}[dialect]
    for sch, name, body in rows:
        if allowed and sch.lower() not in allowed:
            continue
        body = body or ""
        out.append(
            ProcedureIR(
                database=db_name,
                schema_name=sch,
                name=name,
                source_dialect=src_dialect,
                definition_sql=body,
                has_dynamic_sql=bool(
                    re.search(
                        r"\bEXEC(?:UTE)?\s*\(|sp_executesql|EXECUTE\s+IMMEDIATE",
                        body,
                        re.IGNORECASE,
                    )
                ),
                has_cursors=bool(re.search(r"\bCURSOR\b", body, re.IGNORECASE)),
            )
        )
    return out


def harvest_sqlalchemy_catalog(
    url: str,
    source_system: str,
    schemas: list[str] | None = None,
) -> CatalogIR:
    """Harvest any engine with a SQLAlchemy dialect (SQL Server, PostgreSQL, Oracle, SQLite...)
    through the standard inspector: tables, columns, primary and foreign keys, views, procedures,
    row counts. Read-only."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(url, future=True)
    dialect = engine.dialect.name
    db_name = engine.url.database or source_system
    if dialect == "sqlite":
        db_name = Path(db_name).stem
    try:
        insp = inspect(engine)
        allowed = {s.lower() for s in schemas} if schemas else None
        names = [
            s
            for s in insp.get_schema_names()
            if s.lower() not in _SYSTEM_SCHEMAS and not s.lower().startswith("pg_")
        ]
        if dialect == "sqlite":
            names = [None]  # type: ignore[list-item]
        tables: list[TableIR] = []
        views: list[ViewIR] = []
        with engine.connect() as conn:
            for sch in names:
                if allowed and sch and sch.lower() not in allowed:
                    continue
                schema_name = sch or "main"
                for name in insp.get_table_names(schema=sch):
                    pk = {
                        c.lower()
                        for c in (
                            insp.get_pk_constraint(name, schema=sch).get("constrained_columns")
                            or []
                        )
                    }
                    fk_map: dict[str, str] = {}
                    for fk in insp.get_foreign_keys(name, schema=sch):
                        ref = f"{fk.get('referred_schema') or schema_name}.{fk['referred_table']}"
                        for c, rc in zip(
                            fk["constrained_columns"], fk["referred_columns"], strict=False
                        ):
                            fk_map[c.lower()] = f"{ref}({rc})"
                    cols = [
                        ColumnIR(
                            name=c["name"],
                            data_type=str(c["type"]),
                            nullable=bool(c.get("nullable", True)) and c["name"].lower() not in pk,
                            is_primary_key=c["name"].lower() in pk,
                            is_foreign_key=c["name"].lower() in fk_map,
                            fk_target=fk_map.get(c["name"].lower()),
                            ordinal_position=i,
                        )
                        for i, c in enumerate(insp.get_columns(name, schema=sch), start=1)
                    ]
                    tables.append(
                        TableIR(
                            database=db_name,
                            schema_name=schema_name,
                            name=name,
                            columns=cols,
                            row_count=_row_count(conn, dialect, schema_name, name),
                        )
                    )
                for vname in insp.get_view_names(schema=sch):
                    try:
                        definition = insp.get_view_definition(vname, schema=sch) or ""
                    except Exception:
                        definition = ""
                    if definition and not definition.lstrip().upper().startswith("CREATE"):
                        definition = f"CREATE VIEW {schema_name}.{vname} AS {definition}"
                    views.append(
                        ViewIR(
                            database=db_name,
                            schema_name=schema_name,
                            name=vname,
                            definition_sql=definition,
                        )
                    )
            procedures = _procedures(conn, dialect, db_name, allowed)
        return CatalogIR(
            source_system=source_system,
            database_name=db_name,
            tables=tables,
            views=views,
            procedures=procedures,
        )
    finally:
        engine.dispose()


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
        """Offline bundle (DDL script / JSON / DuckDB file) if given, otherwise the live database."""
        if catalog_bundle_path:
            bundle = Path(catalog_bundle_path)
            if bundle.suffix.lower() == ".json":
                return CatalogIR.model_validate_json(bundle.read_text(encoding="utf-8"))
            if bundle.suffix.lower() in (".duckdb", ".db"):
                return harvest_duckdb_catalog(str(bundle), self.source_system, schemas)
            if bundle.suffix.lower() in (".sqlite", ".sqlite3"):
                return harvest_sqlalchemy_catalog(
                    f"sqlite:///{bundle}", self.source_system, schemas
                )
            ddl_text = bundle.read_text(encoding="utf-8")
            return parse_ddl_to_catalog(ddl_text, self.source_system, self.dialect, bundle.stem)

        if not connection_uri:
            raise ConfigError(
                "No source connection or catalog bundle configured: set source.connection "
                "(a vault:// reference) or source.catalog_bundle_path"
            )
        resolved = resolve_connection_uri(connection_uri)
        if resolved.startswith("duckdb:///"):
            return harvest_duckdb_catalog(resolved, self.source_system, schemas)
        return harvest_sqlalchemy_catalog(resolved, self.source_system, schemas)

    def collect_usage(
        self, connection_uri: str, usage_log_path: str | None = None
    ) -> dict[str, int]:
        return load_usage_csv_or_json(usage_log_path)
