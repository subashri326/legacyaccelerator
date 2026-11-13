"""Source-aware data type mapping for Oracle and Teradata.

The same type name can mean different things per database, so types are first read as a
neutral meaning (what values the column can hold) and then written for the target:

* Oracle ``DATE`` holds a time of day -> a timestamp, never a plain date.
* Oracle ``NUMBER`` without precision holds any number -> ``DECIMAL(38,10)`` and a review note
  (confirm the scale from the data; reconciliation will show any loss).
* Oracle ``VARCHAR2(n CHAR)``/``NVARCHAR2`` and Teradata ``CHARACTER SET UNICODE`` hold up to n
  characters; Fabric counts ``VARCHAR`` length in bytes (UTF-8), so the length is widened.
* Fabric has no ``TINYINT``-compatible byte, ``TIME ZONE`` or ``DATETIMEOFFSET``; Databricks has
  no ``TIME``. These are mapped to the closest exact holder with a review note.
* Types with no equivalent (``INTERVAL``, ``PERIOD``, ``ROWID``, ``XMLTYPE``, spatial...) are
  mapped to text with a review note rather than silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Neutral:
    kind: str  # int8|int16|int32|int64|decimal|float32|float64|string|char|text|binary|blob|
    #            bool|date|time|timestamp|timestamptz|unsupported
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    unicode: bool = False
    note: str | None = None


def _nums(t: str) -> list[str]:
    m = re.search(r"\(([^)]*)\)", t)
    return [p.strip() for p in m.group(1).split(",")] if m else []


def _int(v: str | None, default: int | None = None) -> int | None:
    return int(v) if v and re.fullmatch(r"-?\d+", v) else default


def read_oracle(t: str, unicode: bool = False) -> Neutral:
    u = re.sub(r"\s+", " ", t.strip().upper())
    base = u.split("(")[0].strip()
    n = _nums(u)
    if base in ("NUMBER", "NUMERIC", "DECIMAL", "DEC"):
        if not n:
            return Neutral(
                "decimal",
                precision=38,
                scale=10,
                note=(
                    "Oracle NUMBER without precision can hold any number; mapped to DECIMAL(38,10). "
                    "Confirm the scale from the data."
                ),
            )
        p = _int(n[0], 38) if n[0] != "*" else 38
        s = _int(n[1], 0) if len(n) > 1 else 0
        s = max(s or 0, 0)
        return Neutral("decimal", precision=max(p or 38, s), scale=s)
    if base in ("INTEGER", "INT", "SMALLINT"):
        return Neutral("decimal", precision=38, scale=0)  # Oracle INTEGER = NUMBER(38)
    if base == "FLOAT":
        return Neutral("float64")
    if base == "BINARY_FLOAT":
        return Neutral("float32")
    if base in ("BINARY_DOUBLE", "DOUBLE PRECISION", "REAL"):
        return Neutral("float64")
    if base in ("VARCHAR2", "VARCHAR", "NVARCHAR2", "CHARACTER VARYING", "CHAR VARYING"):
        return Neutral(
            "string", length=_int(n[0] if n else None, 4000), unicode=unicode or base == "NVARCHAR2"
        )
    if base in ("CHAR", "NCHAR", "CHARACTER"):
        return Neutral(
            "char", length=_int(n[0] if n else None, 1), unicode=unicode or base == "NCHAR"
        )
    if base in ("CLOB", "NCLOB", "LONG"):
        return Neutral(
            "text", unicode=True, note="LONG is deprecated in Oracle" if base == "LONG" else None
        )
    if base == "RAW":
        return Neutral("binary", length=_int(n[0] if n else None, 2000))
    if base in ("BLOB", "LONG RAW", "BFILE"):
        return Neutral(
            "blob", note="BFILE points to files outside the database" if base == "BFILE" else None
        )
    if base == "DATE":
        return Neutral("timestamp", precision=0)  # Oracle DATE includes the time of day
    if base.startswith("TIMESTAMP"):
        p = _int(n[0] if n else None, 6)
        if "TIME ZONE" in u:
            return Neutral(
                "timestamptz",
                precision=p,
                note=(
                    "WITH LOCAL TIME ZONE values are normalised to the session time zone"
                    if "LOCAL" in u
                    else None
                ),
            )
        return Neutral("timestamp", precision=p)
    return Neutral(
        "unsupported",
        note=f"Oracle type {t} has no direct equivalent; stored as "
        "text. Decide how it should be represented.",
    )


def read_teradata(t: str, unicode: bool = False) -> Neutral:
    u = re.sub(r"\s+", " ", t.strip().upper())
    base = u.split("(")[0].strip()
    n = _nums(u)
    ints = {
        "BYTEINT": "int8",
        "SMALLINT": "int16",
        "INTEGER": "int32",
        "INT": "int32",
        "BIGINT": "int64",
    }
    if base in ints:
        return Neutral(ints[base])
    if base in ("DECIMAL", "DEC", "NUMERIC"):
        return Neutral(
            "decimal",
            precision=_int(n[0] if n else None, 5),
            scale=_int(n[1] if len(n) > 1 else None, 0),
        )
    if base == "NUMBER":
        if not n or n[0] == "*":
            return Neutral(
                "decimal",
                precision=38,
                scale=10,
                note=(
                    "Teradata NUMBER without precision; mapped to DECIMAL(38,10). Confirm the scale."
                ),
            )
        return Neutral(
            "decimal", precision=_int(n[0], 38), scale=_int(n[1] if len(n) > 1 else None, 0)
        )
    if base in ("FLOAT", "REAL", "DOUBLE PRECISION"):
        return Neutral("float64")
    if base in ("VARCHAR", "CHARACTER VARYING", "CHAR VARYING", "LONG VARCHAR"):
        return Neutral("string", length=_int(n[0] if n else None, 64000), unicode=unicode)
    if base in ("CHAR", "CHARACTER"):
        return Neutral("char", length=_int(n[0] if n else None, 1), unicode=unicode)
    if base == "CLOB":
        return Neutral("text", unicode=unicode)
    if base in ("BYTE", "VARBYTE"):
        return Neutral("binary", length=_int(n[0] if n else None, 64000))
    if base == "BLOB":
        return Neutral("blob")
    if base == "DATE":
        return Neutral("date")
    if base.startswith("TIMESTAMP"):
        p = _int(n[0] if n else None, 6)
        return Neutral("timestamptz" if "TIME ZONE" in u else "timestamp", precision=p)
    if base.startswith("TIME"):
        return Neutral("time", precision=_int(n[0] if n else None, 6))
    return Neutral(
        "unsupported",
        note=f"Teradata type {t} has no direct equivalent; stored as "
        "text. Decide how it should be represented.",
    )


def write(nt: Neutral, platform: str) -> tuple[str, str | None]:
    """Target type for a neutral type, plus a review note when the mapping is not exact."""
    k, note = nt.kind, nt.note
    if platform == "snowflake":
        m = {
            "int8": "NUMBER(3,0)",
            "int16": "NUMBER(5,0)",
            "int32": "NUMBER(10,0)",
            "int64": "NUMBER(19,0)",
            "float32": "FLOAT",
            "float64": "FLOAT",
            "bool": "BOOLEAN",
            "date": "DATE",
            "text": "VARCHAR",
            "blob": "BINARY",
        }
        if k in m:
            return m[k], note
        if k == "decimal":
            return f"NUMBER({nt.precision},{nt.scale})", note
        if k in ("string", "char"):
            return (f"VARCHAR({nt.length})" if k == "string" else f"CHAR({nt.length})"), note
        if k == "binary":
            return f"BINARY({nt.length})", note
        if k == "time":
            return f"TIME({min(nt.precision or 9, 9)})", note
        if k == "timestamp":
            return f"TIMESTAMP_NTZ({min(nt.precision or 0, 9)})", note
        if k == "timestamptz":
            return f"TIMESTAMP_TZ({min(nt.precision or 0, 9)})", note
    if platform == "databricks":
        m = {
            "int8": "TINYINT",
            "int16": "SMALLINT",
            "int32": "INT",
            "int64": "BIGINT",
            "float32": "FLOAT",
            "float64": "DOUBLE",
            "bool": "BOOLEAN",
            "date": "DATE",
            "string": "STRING",
            "char": "STRING",
            "text": "STRING",
            "binary": "BINARY",
            "blob": "BINARY",
            "timestamp": "TIMESTAMP_NTZ",
        }  # legacy timestamps have no zone
        if k in m:
            return m[k], note
        if k == "decimal":
            return f"DECIMAL({nt.precision},{nt.scale})", note
        if k == "time":
            return "STRING", _join(note, "Databricks has no TIME type; stored as text 'HH:MM:SS'.")
        if k == "timestamptz":
            return "TIMESTAMP", _join(
                note,
                "Databricks TIMESTAMP stores the instant (UTC); the original offset is not kept.",
            )
    if platform == "fabric":
        m = {
            "int8": "SMALLINT",
            "int16": "SMALLINT",
            "int32": "INT",
            "int64": "BIGINT",
            "float32": "REAL",
            "float64": "FLOAT",
            "bool": "BIT",
            "date": "DATE",
            "text": "VARCHAR(MAX)",
            "blob": "VARBINARY(MAX)",
        }
        if k in m:
            return m[k], note
        if k == "decimal":
            return f"DECIMAL({nt.precision},{nt.scale})", note
        if k in ("string", "char"):
            # Fabric VARCHAR length is in bytes (UTF-8): up to 4 per character.
            n = (nt.length or 1) * (4 if nt.unicode else 1)
            size = str(n) if n <= 8000 else "MAX"
            return (f"VARCHAR({size})" if k == "string" or nt.unicode else f"CHAR({size})"), note
        if k == "binary":
            return (
                f"VARBINARY({nt.length})" if (nt.length or 0) <= 8000 else "VARBINARY(MAX)"
            ), note
        if k == "time":
            return f"TIME({min(nt.precision or 6, 6)})", note
        if k == "timestamp":
            return f"DATETIME2({min(nt.precision or 0, 6)})", note
        if k == "timestamptz":
            return f"DATETIME2({min(nt.precision or 0, 6)})", _join(
                note, "Fabric has no time-zone type; values are stored as UTC."
            )
    if k == "unsupported":
        return {"snowflake": "VARCHAR", "databricks": "STRING"}.get(platform, "VARCHAR(MAX)"), note
    raise ValueError(f"No mapping for {nt} on {platform}")


def _join(a: str | None, b: str) -> str:
    return f"{a} {b}" if a else b


def map_source_type(
    source_type: str, source_db: str, platform: str, attributes: dict | None = None
) -> tuple[str, str | None]:
    attributes = attributes or {}
    reader = read_oracle if source_db == "oracle" else read_teradata
    return write(reader(source_type, bool(attributes.get("unicode"))), platform)
