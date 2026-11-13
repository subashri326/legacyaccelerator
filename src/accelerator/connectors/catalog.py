"""Connector catalog: the systems a user can connect, the form each needs, and how to test it.

Each connector type declares its form fields (shown by the UI, like Airbyte's connector specs),
how to build a SQLAlchemy URI from them, and a cheap query to prove the login works. Secret
fields (passwords, tokens) are only ever written to the secrets vault, never to the metadata DB.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlencode

from accelerator.connectors.base import open_connector


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "string"  # string | integer | password | boolean | select
    required: bool = True
    default: Any = None
    help: str = ""
    options: tuple[str, ...] = ()

    @property
    def secret(self) -> bool:
        return self.type == "password"


@dataclass(frozen=True)
class ConnectorType:
    id: str
    name: str
    kind: str  # source | destination | both
    database: str  # the client-config value: source.database or target.platform
    description: str
    fields: tuple[Field, ...]
    build_uri: Callable[[dict[str, Any]], str] = field(repr=False, compare=False)
    test_query: str = "SELECT 1"
    driver_extra: str = ""
    docs: str = ""

    def spec(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "database": self.database,
            "description": self.description,
            "driver_extra": self.driver_extra,
            "docs": self.docs,
            "fields": [
                asdict(f) | {"secret": f.secret, "options": list(f.options)} for f in self.fields
            ],
        }


def _q(v: Any) -> str:
    return quote_plus(str(v if v is not None else ""))


def _oracle(s: dict[str, Any]) -> str:
    return (
        f"oracle+oracledb://{_q(s['username'])}:{_q(s['password'])}@{s['host']}:"
        f"{s.get('port') or 1521}/?service_name={_q(s['service_name'])}"
    )


def _sqlserver(s: dict[str, Any]) -> str:
    params = {
        "driver": s.get("odbc_driver") or "ODBC Driver 18 for SQL Server",
        "Encrypt": "yes" if s.get("encrypt", True) else "no",
        "TrustServerCertificate": "yes" if s.get("trust_server_certificate") else "no",
    }
    return (
        f"mssql+pyodbc://{_q(s['username'])}:{_q(s['password'])}@{s['host']}:"
        f"{s.get('port') or 1433}/{_q(s['database'])}?{urlencode(params)}"
    )


def _teradata(s: dict[str, Any]) -> str:
    q = f"?database={_q(s['database'])}" if s.get("database") else ""
    return f"teradatasql://{_q(s['username'])}:{_q(s['password'])}@{s['host']}/{q}"


def _postgres(s: dict[str, Any]) -> str:
    return (
        f"postgresql+psycopg2://{_q(s['username'])}:{_q(s['password'])}@{s['host']}:"
        f"{s.get('port') or 5432}/{_q(s['database'])}"
    )


def _snowflake(s: dict[str, Any]) -> str:
    params = {k: s[k] for k in ("warehouse", "role") if s.get(k)}
    path = _q(s["database"]) + (f"/{_q(s['schema'])}" if s.get("schema") else "")
    q = f"?{urlencode(params)}" if params else ""
    return f"snowflake://{_q(s['username'])}:{_q(s['password'])}@{s['account']}/{path}{q}"


def _databricks(s: dict[str, Any]) -> str:
    params = {"http_path": s["http_path"], "catalog": s["catalog"]}
    if s.get("schema"):
        params["schema"] = s["schema"]
    host = str(s["server_hostname"]).removeprefix("https://").rstrip("/")
    return f"databricks://token:{_q(s['access_token'])}@{host}?{urlencode(params)}"


def _fabric(s: dict[str, Any]) -> str:
    params = {
        "driver": s.get("odbc_driver") or "ODBC Driver 18 for SQL Server",
        "authentication": "ActiveDirectoryServicePrincipal",
        "Encrypt": "yes",
    }
    return (
        f"mssql+pyodbc://{_q(s['client_id'])}:{_q(s['client_secret'])}@{s['server']}:1433/"
        f"{_q(s['warehouse'])}?{urlencode(params)}"
    )


def _duckdb(s: dict[str, Any]) -> str:
    return f"duckdb:///{s['path']}"


_HOST = Field("host", "Host", help="Server name or IP address")
_USER = Field("username", "Username")
_PASS = Field("password", "Password", type="password")

CATALOG: dict[str, ConnectorType] = {
    c.id: c
    for c in [
        ConnectorType(
            "oracle",
            "Oracle",
            "source",
            "oracle",
            "Oracle Database 12c and later (on-premises, Docker or Oracle Cloud).",
            (
                _HOST,
                Field("port", "Port", "integer", default=1521),
                Field("service_name", "Service name", help="e.g. FREEPDB1 or ORCLPDB1"),
                _USER,
                _PASS,
            ),
            _oracle,
            "SELECT 1 FROM DUAL",
            "oracle",
            "Read-only user with SELECT on the schemas and on ALL_* views.",
        ),
        ConnectorType(
            "sqlserver",
            "Microsoft SQL Server",
            "source",
            "sqlserver",
            "SQL Server 2016 and later, including Azure SQL.",
            (
                _HOST,
                Field("port", "Port", "integer", default=1433),
                Field("database", "Database"),
                _USER,
                _PASS,
                Field("encrypt", "Encrypt connection", "boolean", False, True),
                Field(
                    "trust_server_certificate",
                    "Trust server certificate",
                    "boolean",
                    False,
                    False,
                    "Turn on for self-signed certificates (e.g. SQL Server in Docker).",
                ),
                Field(
                    "odbc_driver",
                    "ODBC driver",
                    required=False,
                    default="ODBC Driver 18 for SQL Server",
                ),
            ),
            _sqlserver,
            "SELECT 1",
            "sqlserver",
            "Needs the Microsoft ODBC Driver 18 for SQL Server on the machine running the backend.",
        ),
        ConnectorType(
            "teradata",
            "Teradata",
            "source",
            "teradata",
            "Teradata 16 and later, including Vantage.",
            (_HOST, _USER, _PASS, Field("database", "Default database", required=False)),
            _teradata,
            "SELECT 1",
            "teradata",
        ),
        ConnectorType(
            "postgres",
            "PostgreSQL",
            "source",
            "postgres",
            "PostgreSQL 12 and later.",
            (
                _HOST,
                Field("port", "Port", "integer", default=5432),
                Field("database", "Database"),
                _USER,
                _PASS,
            ),
            _postgres,
            "SELECT 1",
        ),
        ConnectorType(
            "snowflake",
            "Snowflake",
            "destination",
            "snowflake",
            "Snowflake account (a 30-day trial works).",
            (
                Field(
                    "account",
                    "Account identifier",
                    help="e.g. abc12345.ap-south-1 or orgname-accountname",
                ),
                _USER,
                _PASS,
                Field("database", "Database"),
                Field("warehouse", "Warehouse", default="COMPUTE_WH"),
                Field("role", "Role", required=False, default="ACCOUNTADMIN"),
                Field("schema", "Default schema", required=False),
            ),
            _snowflake,
            "SELECT 1",
            "snowflake",
            "The user needs CREATE SCHEMA on the database and USAGE on the warehouse.",
        ),
        ConnectorType(
            "databricks",
            "Databricks",
            "destination",
            "databricks",
            "Databricks SQL warehouse (Free Edition or trial works).",
            (
                Field(
                    "server_hostname",
                    "Server hostname",
                    help="From the SQL warehouse's Connection details",
                ),
                Field("http_path", "HTTP path", help="e.g. /sql/1.0/warehouses/abc123"),
                Field("access_token", "Personal access token", "password"),
                Field("catalog", "Catalog", default="workspace"),
                Field("schema", "Default schema", required=False),
            ),
            _databricks,
            "SELECT 1",
            "databricks",
        ),
        ConnectorType(
            "fabric",
            "Microsoft Fabric Warehouse",
            "destination",
            "fabric",
            "Fabric Warehouse SQL endpoint, signing in with a service principal.",
            (
                Field(
                    "server",
                    "SQL connection string",
                    help="xxxx.datawarehouse.fabric.microsoft.com",
                ),
                Field("warehouse", "Warehouse name"),
                Field("client_id", "Service principal client ID"),
                Field("client_secret", "Client secret", "password"),
                Field(
                    "odbc_driver",
                    "ODBC driver",
                    required=False,
                    default="ODBC Driver 18 for SQL Server",
                ),
            ),
            _fabric,
            "SELECT 1",
            "sqlserver",
        ),
        ConnectorType(
            "duckdb",
            "DuckDB file (local test)",
            "both",
            "duckdb",
            "A local DuckDB file, for trying the product without any servers.",
            (
                Field("path", "File path", help="e.g. .l2c_data/demo/legacy.duckdb"),
                Field(
                    "acts_as",
                    "Behaves as (destinations only)",
                    "select",
                    False,
                    "snowflake",
                    "Which platform's SQL the generated code uses",
                    ("snowflake", "databricks", "fabric"),
                ),
            ),
            _duckdb,
            "SELECT 1",
        ),
    ]
}


def get_type(type_id: str) -> ConnectorType:
    if type_id not in CATALOG:
        raise ValueError(f"Unknown connector type '{type_id}'")
    return CATALOG[type_id]


def validate_settings(ct: ConnectorType, values: dict[str, Any]) -> dict[str, Any]:
    """Apply defaults, check required fields and types. Returns the cleaned values."""
    out: dict[str, Any] = {}
    missing = []
    for f in ct.fields:
        v = values.get(f.name)
        if v in (None, ""):
            v = f.default
        if v in (None, "") and f.required:
            missing.append(f.label)
            continue
        if v is not None and f.type == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{f.label} must be a whole number") from exc
        if f.type == "boolean":
            v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
        out[f.name] = v
    if missing:
        raise ValueError(f"Missing: {', '.join(missing)}")
    return out


def split_secrets(
    ct: ConnectorType, values: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(settings safe to store and show, secret values for the vault)."""
    secret_names = {f.name for f in ct.fields if f.secret}
    return (
        {k: v for k, v in values.items() if k not in secret_names},
        {k: v for k, v in values.items() if k in secret_names},
    )


@dataclass
class TestResult:
    ok: bool
    message: str
    seconds: float


def test_connection(
    ct: ConnectorType, values: dict[str, Any], timeout_hint: str = ""
) -> TestResult:
    """Log in and run a trivial query. Never raises: failures come back as a message."""
    started = time.monotonic()
    try:
        uri = ct.build_uri(values)
        with open_connector(uri, read_only=True) as conn:
            conn.query(ct.test_query)
            detail = _describe(conn)
        return TestResult(True, f"Connected to {ct.name}.{detail}", time.monotonic() - started)
    except ModuleNotFoundError as exc:
        hint = f' Install it with pip install ".[{ct.driver_extra}]".' if ct.driver_extra else ""
        return TestResult(
            False,
            f"The driver for {ct.name} is not installed ({exc.name}).{hint}",
            time.monotonic() - started,
        )
    except Exception as exc:
        return TestResult(False, _friendly(str(exc)), time.monotonic() - started)


def _describe(conn: Any) -> str:
    try:
        names = conn.list_schemas() if hasattr(conn, "list_schemas") else []
    except Exception:
        names = []
    return f" {len(names)} schema(s) visible." if names else ""


def _friendly(msg: str) -> str:
    """Trim driver noise and hide anything that looks like a credential."""
    first = msg.strip().splitlines()[0] if msg.strip() else "Unknown error"
    if "(Background on this error" in first:
        first = first.split("(Background on this error")[0].strip()
    import re

    first = re.sub(r"://[^@\s]+@", "://***@", first)
    return first[:400]
