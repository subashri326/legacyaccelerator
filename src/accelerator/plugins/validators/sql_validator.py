"""Reconciliation: compare a source table with its migrated copy, inside each engine.

Every check is an aggregate query run where the data lives (no rows are copied out, except a
small masked sample when a check fails or L4 is asked for). Queries are built once and rendered
in each engine's own SQL dialect, so source and target can be different engines
(e.g. SQL Server vs Snowflake).

Levels
* L1  row count
* L2  per column: sum (numbers), min, max, null count, distinct count
* L3  L2 plus per-bucket fingerprints (rows split into 16 buckets by an integer key): count,
      numeric sums and string lengths per bucket - pinpoints where a difference is
* L4  L3 plus a masked row-level diff of a bounded sample
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp

from accelerator.config.client_config import ClientConfig, ReconOverride
from accelerator.connectors import Connector, open_connector
from accelerator.core.security import mask_row_sample
from accelerator.ir.models import PackageIR, TableIR
from accelerator.plugins.interfaces import PluginManifest, ValidationOutcome, Validator

BUCKETS = 16
NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "DECIMAL",
    "NUMERIC",
    "FLOAT",
    "REAL",
    "DOUBLE",
    "MONEY",
    "SMALLMONEY",
    "NUMBER",
    "BYTEINT",
    "BINARY_FLOAT",
    "BINARY_DOUBLE",
}
INTEGER_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}
BOOL_TYPES = {"BIT", "BOOLEAN", "BOOL"}
STRING_TYPES = {
    "VARCHAR",
    "NVARCHAR",
    "CHAR",
    "NCHAR",
    "STRING",
    "VARCHAR2",
    "NVARCHAR2",
    "CHARACTER",
}
LOB_TYPES = {
    "TEXT",
    "NTEXT",
    "IMAGE",
    "XML",
    "CLOB",
    "BLOB",
    "VARBINARY",
    "BINARY",
    "GEOGRAPHY",
    "GEOMETRY",
}


def _base(dtype: str) -> str:
    return dtype.split("(")[0].strip().upper()


_TZ_TYPES = {"TIMESTAMP_TZ", "TIMESTAMPTZ", "TIMESTAMP_LTZ", "DATETIMEOFFSET"}
# The value as a plain UTC timestamp, per engine. Comparing instants this way is fairer than
# comparing offsets, and avoids drivers' time-zone conversions (Snowflake's Windows path overflows).
_UTC_SQL = {
    "oracle": "SYS_EXTRACT_UTC({c})",
    "snowflake": "CAST(CONVERT_TIMEZONE('UTC', {c}) AS TIMESTAMP_NTZ)",
    "tsql": "CAST(SWITCHOFFSET({c}, '+00:00') AS DATETIME2)",
    "postgres": "({c} AT TIME ZONE 'UTC')",
    "duckdb": "({c} AT TIME ZONE 'UTC')",
}


def _utc(conn: Connector, c: exp.Expression) -> exp.Expression:
    template = _UTC_SQL.get(conn.dialect)
    if not template:
        return c
    return sqlglot.parse_one(template.format(c=c.sql(dialect=conn.dialect)), read=conn.dialect)


def _kind(dtype: str) -> str:
    b = _base(dtype)
    up = dtype.upper()
    if ("TIME ZONE" in up and "WITHOUT TIME ZONE" not in up) or b in _TZ_TYPES:
        return "tstz"
    if b in BOOL_TYPES:
        return "bool"
    if b in NUMERIC_TYPES:
        return "number"
    if b in LOB_TYPES or "(MAX)" in dtype.upper():
        return "lob"
    if b in STRING_TYPES:
        return "string"
    return "other"  # dates, times, uuids...


def _norm(v: Any) -> Any:
    """Make values from different drivers comparable."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, datetime):
        if v.tzinfo is not None:  # compare instants: engines return different offsets
            v = v.astimezone(UTC)
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, str):
        return v.rstrip()  # CHAR padding differs between engines
    return str(v)


def _close(a: float | None, b: float | None, tol: float) -> tuple[bool, float]:
    if a is None or b is None:
        return a is b, 0.0
    delta = abs(a - b)
    # Allow tiny float noise from different engines' summation order.
    return delta <= tol + 1e-9 * max(1.0, abs(a), abs(b)), delta


# Snowflake's Python connector (5.x) fails converting date/time values on Windows ("Python int
# too large to convert to C int"). For checks, Snowflake returns them as fixed-format text.
_SNOWFLAKE_TEXT_FORMATS = {
    "DATE": "YYYY-MM-DD",
    "TIME": "HH24:MI:SS.FF6",
    "TIMESTAMP_NTZ": "YYYY-MM-DD HH24:MI:SS.FF6",
    "TIMESTAMP_LTZ": "YYYY-MM-DD HH24:MI:SS.FF6",
    "TIMESTAMP_TZ": "YYYY-MM-DD HH24:MI:SS.FF6",
}


def _snowflake_types(conn: Connector, tbl: TableIR) -> dict[str, str]:
    """Column name (upper case) -> Snowflake data type, from the information schema."""
    rows = conn.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        [tbl.schema_name.upper(), tbl.name.upper()],
    )
    return {str(n).upper(): str(t).upper() for n, t in rows}


def _from_text(v: Any) -> Any:
    """Turn the fixed-format text back into a date, time or timestamp."""
    if not isinstance(v, str):
        return v
    try:
        if len(v) == 10:
            return date.fromisoformat(v)
        if len(v) <= 15 and v[2] == ":":
            return time.fromisoformat(v)
        return datetime.fromisoformat(v)
    except ValueError:
        return v


def _profile(
    conn: Connector, tbl: TableIR, kinds: list[str], level: str, key: str | None
) -> dict[str, Any]:
    t = exp.to_table(conn.qualified(tbl.schema_name, tbl.name), dialect=conn.dialect)
    sel: list[exp.Expression] = [exp.Count(this=exp.Star())]
    layout: list[tuple[str, str]] = []
    sf_types = (
        _snowflake_types(conn, tbl)
        if conn.dialect == "snowflake" and any(k in ("other", "tstz") for k in kinds)
        else {}
    )
    as_text: set[str] = set()
    for col, kind in zip(tbl.columns, kinds, strict=True):
        c = exp.column(conn.quote(col.name))
        nulls = exp.Sum(
            this=exp.Case()
            .when(exp.Is(this=c.copy(), expression=exp.Null()), exp.Literal.number(1))
            .else_(exp.Literal.number(0))
        )
        if level == "L1":
            continue
        if kind == "number":
            sel += [
                exp.Sum(this=exp.cast(c.copy(), "DOUBLE")),
                exp.Min(this=c.copy()),
                exp.Max(this=c.copy()),
                nulls,
                exp.Count(this=exp.Distinct(expressions=[c.copy()])),
            ]
            layout += [(col.name, s) for s in ("sum", "min", "max", "null_count", "distinct_count")]
        elif kind == "bool":
            trues = exp.Sum(
                this=exp.Case().when(c.copy(), exp.Literal.number(1)).else_(exp.Literal.number(0))
            )
            sel += [trues, nulls]
            layout += [(col.name, "true_count"), (col.name, "null_count")]
        elif kind == "lob":
            sel += [nulls]
            layout += [(col.name, "null_count")]
        else:
            v = _utc(conn, c.copy()) if kind == "tstz" else c
            lo: exp.Expression = exp.Min(this=v.copy())
            hi: exp.Expression = exp.Max(this=v.copy())
            sf_type = sf_types.get(col.name.upper(), "")
            if kind == "tstz" and sf_type:
                sf_type = "TIMESTAMP_NTZ"  # _utc already made it a plain UTC timestamp
            fmt = _SNOWFLAKE_TEXT_FORMATS.get(sf_type)
            if fmt:
                lo, hi = (
                    exp.Anonymous(this="TO_VARCHAR", expressions=[e, exp.Literal.string(fmt)])
                    for e in (lo, hi)
                )
                as_text.add(col.name)
            sel += [lo, hi, nulls, exp.Count(this=exp.Distinct(expressions=[v.copy()]))]
            layout += [(col.name, s) for s in ("min", "max", "null_count", "distinct_count")]
    row = conn.query(conn.render(exp.select(*sel).from_(t)))[0]
    prof: dict[str, Any] = {"row_count": int(row[0]), "columns": {}, "buckets": {}}
    for (cname, stat), val in zip(layout, row[1:], strict=True):
        if cname in as_text and stat in ("min", "max"):
            val = _from_text(val)
        prof["columns"].setdefault(cname, {})[stat] = _norm(val)

    if level in ("L3", "L4") and key:
        kc = exp.column(conn.quote(key))
        bucket = exp.Mod(this=kc, expression=exp.Literal.number(BUCKETS))
        parts: list[exp.Expression] = [exp.alias_(bucket, "l2c_b"), exp.Count(this=exp.Star())]
        names: list[str] = ["count"]
        for col, kind in zip(tbl.columns, kinds, strict=True):
            c = exp.column(conn.quote(col.name))
            if kind == "number":
                parts.append(exp.Sum(this=exp.cast(c, "DOUBLE")))
                names.append(f"sum:{col.name}")
            elif kind == "string":
                # Trailing spaces are ignored: CHAR padding differs between engines.
                trimmed = exp.Trim(this=c, position="TRAILING")
                length: exp.Expression = (
                    exp.Anonymous(this="CHARACTER_LENGTH", expressions=[trimmed])
                    if conn.dialect == "teradata"
                    else exp.Length(this=trimmed)
                )
                parts.append(exp.Sum(this=length))
                names.append(f"len:{col.name}")
        q = exp.select(*parts).from_(t).group_by(bucket.copy())
        for r in conn.query(conn.render(q)):
            prof["buckets"][str(int(r[0]))] = {
                n: _norm(v) for n, v in zip(names, r[1:], strict=True)
            }
    return prof


def _sample(
    conn: Connector, tbl: TableIR, key_pos: int, buckets: list[str], limit: int
) -> list[tuple[Any, ...]]:
    t = exp.to_table(conn.qualified(tbl.schema_name, tbl.name), dialect=conn.dialect)
    cols = [exp.column(conn.quote(c.name)) for c in tbl.columns]
    kc = cols[key_pos].copy()
    text_pos: list[int] = []
    shown: list[exp.Expression] = list(cols)
    if conn.dialect == "snowflake":  # see _SNOWFLAKE_TEXT_FORMATS
        sf_types = _snowflake_types(conn, tbl)
        for i, col in enumerate(tbl.columns):
            fmt = _SNOWFLAKE_TEXT_FORMATS.get(sf_types.get(col.name.upper(), ""))
            if fmt:
                c = cols[i].copy()
                if sf_types[col.name.upper()] in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
                    c = _utc(conn, c)
                shown[i] = exp.Anonymous(
                    this="TO_VARCHAR", expressions=[c, exp.Literal.string(fmt)]
                )
                text_pos.append(i)
    q = exp.select(*shown).from_(t)
    if buckets:
        q = q.where(
            exp.In(
                this=exp.Mod(this=kc.copy(), expression=exp.Literal.number(BUCKETS)),
                expressions=[exp.Literal.number(int(b)) for b in buckets],
            )
        )
    q = q.order_by(kc).limit(limit)
    rows = conn.query(conn.render(q))
    if not text_pos:
        return rows
    return [tuple(_from_text(v) if i in text_pos else v for i, v in enumerate(r)) for r in rows]


class SqlPushdownValidator(Validator):
    """L1-L4 reconciliation across any two supported engines."""

    manifest = PluginManifest(
        plugin_type="Validator",
        name="sql_pushdown_validator",
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
        capabilities={"L1": True, "L2": True, "L3": True, "L4": True, "cross_engine": True},
        description="Pushed-down aggregate checks rendered per engine dialect, masked L4 samples.",
    )

    def reconcile_table(
        self,
        table: TableIR,
        rule: ReconOverride,
        source_uri: str,
        target_uri: str,
        config: ClientConfig,
        target_table: TableIR | None = None,
    ) -> ValidationOutcome:
        tt = target_table or table
        level = "L2" if rule.level == "JOB" else rule.level
        kinds = [_kind(c.data_type) for c in table.columns]
        keys = [i for i, c in enumerate(table.columns) if c.is_primary_key]
        key_pos = keys[0] if len(keys) == 1 else None
        int_key = key_pos is not None and _base(table.columns[key_pos].data_type) in INTEGER_TYPES
        tol = rule.numeric_tolerance

        with (
            open_connector(source_uri, read_only=True) as src,
            open_connector(target_uri, read_only=True) as tgt,
        ):
            s = _profile(src, table, kinds, level, table.columns[key_pos].name if int_key else None)
            t = _profile(tgt, tt, kinds, level, tt.columns[key_pos].name if int_key else None)
            # Report the target in source column names (target may rename by naming rules).
            rename = {tc.name: sc.name for sc, tc in zip(table.columns, tt.columns, strict=True)}
            t["columns"] = {rename.get(k, k): v for k, v in t["columns"].items()}
            t["buckets"] = {
                b: {
                    f"{n.split(':', 1)[0]}:{rename.get(n.split(':', 1)[1], n.split(':', 1)[1])}"
                    if ":" in n
                    else n: v
                    for n, v in m.items()
                }
                for b, m in t["buckets"].items()
            }

            diffs: dict[str, Any] = {}
            failed = within = False
            if s["row_count"] != t["row_count"]:
                failed = True
                diffs["row_count_delta"] = t["row_count"] - s["row_count"]

            col_diffs: dict[str, Any] = {}
            kind_of = {c.name: k for c, k in zip(table.columns, kinds, strict=True)}
            for cname, sc in s["columns"].items():
                tc = t["columns"].get(cname, {})
                for stat, sv in sc.items():
                    tv = tc.get(stat)
                    if (
                        stat in ("sum", "min", "max")
                        and isinstance(sv, float)
                        and isinstance(tv, float)
                    ):
                        ok, delta = _close(sv, tv, tol)
                        if not ok:
                            failed = True
                            col_diffs.setdefault(cname, {})[stat] = {
                                "source": sv,
                                "target": tv,
                                "delta": delta,
                            }
                        elif delta > 0 and not math.isclose(sv, tv, rel_tol=1e-12):
                            within = within or delta > 1e-9 * max(1.0, abs(sv))
                            if within:
                                col_diffs.setdefault(cname, {})[stat] = {
                                    "delta": delta,
                                    "status": "within_tolerance",
                                }
                    elif (
                        sv != tv
                        and stat == "distinct_count"
                        and kind_of[cname] == "number"
                        and tol > 0
                    ):
                        # Drift inside the tolerance can merge or split values; sums, min/max and
                        # bucket fingerprints still catch real differences.
                        within = True
                        col_diffs.setdefault(cname, {})[stat] = {
                            "source": sv,
                            "target": tv,
                            "status": "within_tolerance",
                        }
                    elif sv != tv:
                        failed = True
                        col_diffs.setdefault(cname, {})[stat] = {"source": sv, "target": tv}
            if col_diffs:
                diffs["column_diffs"] = col_diffs

            bad_buckets: list[str] = []
            if level in ("L3", "L4"):
                if not int_key:
                    diffs["l3_note"] = "No single-column integer key: bucket fingerprints skipped."
                for b in sorted(set(s["buckets"]) | set(t["buckets"]), key=int):
                    sb, tb = s["buckets"].get(b, {}), t["buckets"].get(b, {})
                    same = sb.keys() == tb.keys() and all(
                        _close(sb[k], tb[k], tol)[0]
                        if isinstance(sb[k], float) and isinstance(tb[k], float)
                        else sb[k] == tb[k]
                        for k in sb
                    )
                    if not same:
                        bad_buckets.append(b)
                if bad_buckets:
                    failed = True
                    diffs["bucket_mismatches"] = {
                        b: {"source": s["buckets"].get(b), "target": t["buckets"].get(b)}
                        for b in bad_buckets
                    }

            if key_pos is not None and (level == "L4" or failed):
                try:
                    self._row_sample(config, rule, src, tgt, table, tt, key_pos, bad_buckets, diffs)
                except Exception as exc:  # keep the column differences already found
                    diffs["l4_diff_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

        verdict = "fail" if failed else ("pass_within_tolerance" if within else "pass")
        return ValidationOutcome(table.fqn, level, verdict, s, t, diffs)

    def _row_sample(
        self,
        config: Any,
        rule: Any,
        src: Connector,
        tgt: Connector,
        table: TableIR,
        tt: TableIR,
        key_pos: int,
        bad_buckets: list[str],
        diffs: dict[str, Any],
    ) -> None:
        """Compare actual rows (a sample) to show which ones differ."""
        limit = config.reconciliation.l4_sample_limit
        masked = sorted(set(config.reconciliation.masked_columns) | set(rule.masked_columns))
        names = [c.name for c in table.columns]
        s_rows = {
            _norm(r[key_pos]): dict(zip(names, map(_norm, r), strict=True))
            for r in _sample(src, table, key_pos, bad_buckets, limit * 5)
        }
        t_rows = {
            _norm(r[key_pos]): dict(zip(names, map(_norm, r), strict=True))
            for r in _sample(tgt, tt, key_pos, bad_buckets, limit * 5)
        }
        mism = [
            {
                "key": k,
                "source": mask_row_sample(s_rows[k], masked),
                "target": mask_row_sample(t_rows[k], masked),
            }
            for k in sorted(s_rows.keys() & t_rows.keys(), key=str)
            if s_rows[k] != t_rows[k]
        ][:limit]
        diffs["l4_diff"] = {
            "only_in_source_count": len(s_rows.keys() - t_rows.keys()),
            "only_in_target_count": len(t_rows.keys() - s_rows.keys()),
            "value_mismatch_count": len(mism),
            "only_in_source_sample": [
                mask_row_sample(s_rows[k], masked)
                for k in sorted(s_rows.keys() - t_rows.keys(), key=str)[:limit]
            ],
            "only_in_target_sample": [
                mask_row_sample(t_rows[k], masked)
                for k in sorted(t_rows.keys() - s_rows.keys(), key=str)[:limit]
            ],
            "value_mismatch_sample": mism,
            "masked_columns": masked,
            "sampled_buckets": bad_buckets or "first rows by key",
        }

    def reconcile_job(
        self,
        package: PackageIR,
        legacy_sql: str,
        target_sql: str,
        source_uri: str,
        target_uri: str,
        numeric_tolerance: float = 0.0,
    ) -> ValidationOutcome:
        """Run the legacy query and the converted query and compare their outputs as row sets."""
        with (
            open_connector(source_uri, read_only=True) as src,
            open_connector(target_uri, read_only=True) as tgt,
        ):
            s_rows = sorted((tuple(_norm(v) for v in r) for r in src.query(legacy_sql)), key=repr)
            t_rows = sorted((tuple(_norm(v) for v in r) for r in tgt.query(target_sql)), key=repr)
        match = len(s_rows) == len(t_rows) and all(
            all(
                _close(a, b, numeric_tolerance)[0]
                if isinstance(a, float) and isinstance(b, float)
                else a == b
                for a, b in zip(x, y, strict=True)
            )
            for x, y in zip(s_rows, t_rows, strict=True)
        )
        return ValidationOutcome(
            package.id,
            "JOB",
            "pass" if match else "fail",
            {"output_rows": len(s_rows)},
            {"output_rows": len(t_rows)},
            {} if match else {"sample_mismatch": True},
        )

    def run_golden_check(self) -> tuple[bool, str]:
        from accelerator.connectors.base import DuckDBConnector
        from accelerator.ir.models import ColumnIR

        conn = DuckDBConnector(":memory:")
        conn.execute("CREATE TABLE t (id INT, v DECIMAL(10,2), s VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 2.50, 'a'), (2, NULL, 'bb')")
        tbl = TableIR(
            schema_name="",
            name="t",
            columns=[
                ColumnIR(name="id", data_type="INT", is_primary_key=True),
                ColumnIR(name="v", data_type="DECIMAL(10,2)"),
                ColumnIR(name="s", data_type="VARCHAR"),
            ],
        )
        prof = _profile(conn, tbl, [_kind(c.data_type) for c in tbl.columns], "L3", "id")
        conn.close()
        ok = (
            prof["row_count"] == 2
            and prof["columns"]["v"]["sum"] == 2.5
            and prof["columns"]["v"]["null_count"] == 0 + 1
            and len(prof["buckets"]) == 2
        )
        return (
            ok,
            "SqlPushdownValidator golden check passed" if ok else f"Unexpected profile: {prof}",
        )
