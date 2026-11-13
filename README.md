# Legacy-to-Cloud Data Modernization Accelerator

A platform-neutral, metadata-driven data modernization product that takes legacy databases (**SQL Server, Oracle, Teradata**) and ETL estates (**SSIS, Informatica PowerCenter, IBM DataStage, T-SQL / PL-SQL / BTEQ**) from discovery through governed sign-off on **Snowflake, Databricks, or Microsoft Fabric** from a single Neutral Intermediate Representation (IR).

## Architecture Overview

- **Experience Layer**: FastAPI REST API (`accelerator.api`), Typer CLI (`l2c`), Python-native Streamlit Web UI (`accelerator.ui.streamlit_app`), and React + TypeScript SPA (`frontend/`).
- **Control Plane**: Client YAML configuration versioning, restartable/idempotent `RunOrchestrator`, 8-state evidence-backed `StateMachine` (`Discovered -> Assessed -> Converted -> Reviewed -> Deployed -> Loaded -> Validated -> SignedOff`), RBAC, and immutable `AuditLog`.
- **Six Stage Engines**: `DiscoverEngine`, `AssessEngine`, `ConvertEngine`, `MoveEngine`, `ReconcileEngine`, `TrackEngine`.
- **Plugin SDK (5 Interfaces)**: `SourceCollector`, `Parser`, `TargetGenerator` (including vendor wrappers for Lakebridge, SnowConvert AI, Fabric Migration Assistant), `Mover` (Parquet bulk/incremental + Airbyte), `Validator` (pushed-down L1–L4 + job-level).
- **Foundations**: 10-group Metadata DB (PostgreSQL DDL in `sql/postgres_ddl.sql` + SQLAlchemy/Alembic + local SQLite fallback), S3/ADLS/Local `ArtifactStore`, HashiCorp/Env/Local `SecretsVault`, Git wave PR delivery, Prometheus `/metrics`.

## Quickstart (Local Virtualenv + Acme Retail Demo)

```powershell
# 1. Activate virtualenv
.venv\Scripts\Activate.ps1

# 2. Initialize metadata DB, RBAC roles, and plugin registry
l2c init

# 3. Seed realistic local DuckDB legacy estate & store vault references
python examples\acme_retail\seed_legacy_db.py
l2c set-secret vault://acme/sqlserver-ro duckdb:///.l2c_data/demo_acme/legacy_acme.duckdb
l2c set-secret vault://acme/snowflake-deploy duckdb:///.l2c_data/demo_acme/target_cloud.duckdb

# 4. Register client configuration & run the full 5-stage pipeline
l2c register examples\acme_retail\client_config.yaml
l2c run-pipeline <PROJECT_ID> --deliver-git
l2c track <PROJECT_ID>

# 5. Launch REST API or Streamlit Web UI
l2c serve --port 8000
l2c ui --port 8501
```

## What the ETL readers convert, and what they flag

Each reader converts only what it can reproduce exactly; everything else becomes a review item
(never a guess). The Acme demo proves the converted jobs return the same rows as the legacy logic
on all three targets (`tests/test_conversion_correctness.py`, `tests/test_etl_tools_correctness.py`).

| Tool | Converted | Flagged for review |
|---|---|---|
| **SSIS** | sources, derived columns, conditional split (first match), lookups, union, sort, aggregate, destinations | script tasks/components, unknown components |
| **Informatica PowerCenter** (XML export) | source qualifiers (filter, user join, distinct, SQL override), expressions incl. variable ports, filter, router (every matching group), connected lookups (with source filter), joiner (normal/master/detail/full), aggregator, sorter, union, session overrides, workflow order | Update Strategy, Sequence Generator, mapplets, normalizer, stored procedures, unconnected lookups, `$$` parameters, Command/Decision/Email tasks |
| **IBM DataStage** (.dsx or XML export) | DB connector sources/targets, Transformer (constraints, derivations, stage variables, otherwise links), Lookup (continue/drop/fail), Join (inner/left/right), Aggregator, Funnel, Sort, Remove Duplicates, Copy, single-condition Filter, sequence job order | Modify, Merge, Change Capture, SCD, generators, file/data-set stages, containers, update/upsert writes, `#job parameters#`, row-to-row stage variables |

Expression functions are translated one by one with their exact rules (for example,
Informatica `||` ignores NULLs, DataStage `Trim()` also collapses inner spaces). A function a
target cannot run exactly (e.g. `LPAD` on Fabric) is flagged for that target only.

## Docker Compose & Kubernetes Helm

```bash
# Full stack (PostgreSQL, MinIO, FastAPI, Streamlit, React Nginx UI, Prometheus)
docker compose -f deploy/docker-compose.yml up --build

# Client-hosted Kubernetes via Helm
helm upgrade --install l2c deploy/helm/l2c-accelerator
```

## Running Tests

```powershell
.venv\Scripts\pytest
```

## Connecting to real systems

Connections are stored in the secrets vault and referenced from the client config
(`source.connection` / `target.connection`, e.g. `vault://acme/sqlserver-ro`). The secret is a
SQLAlchemy-style URI:

| Engine | URI | Install |
|---|---|---|
| SQL Server | `mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+18+for+SQL+Server` | `pip install ".[sqlserver]"` + Microsoft ODBC Driver 18 |
| PostgreSQL | `postgresql+psycopg2://user:pass@host/db` | included |
| Snowflake | `snowflake://user:pass@account/database/schema?warehouse=WH&role=ROLE` | `pip install ".[snowflake]"` |
| Databricks | `databricks://token:<pat>@<host>?http_path=<warehouse path>&catalog=<c>&schema=<s>` | `pip install ".[databricks]"` |
| Fabric Warehouse | `mssql+pyodbc://...@<workspace>.datawarehouse.fabric.microsoft.com/<warehouse>?driver=ODBC+Driver+18+for+SQL+Server&authentication=ActiveDirectoryServicePrincipal` | `pip install ".[sqlserver]"` |
| DuckDB (local stand-in) | `duckdb:///path/file.duckdb` | included |

`pip install ".[connectors]"` installs every driver. The backend Docker image includes the ODBC driver.

**How data moves:** Move needs Convert to have run first. The converted table definition
creates each target table. Rows are read in primary-key order, one chunk at a time (chunk size
`movement.partition_size_rows`). Each chunk is saved as soon as it is written, so a failed load
can continue with `l2c move <project> --object-key <table> --resume-load-run-id <run>`.
Incremental loads (`--mode incremental`) need `movement.watermark_columns` for the table and a
single-column primary key. Changed rows replace their old versions by key. Rows deleted at the
source are not detected.

**What is tested where:** the automated tests run the full flow against a real PostgreSQL
server and against DuckDB. Snowflake, Databricks, Fabric and SQL Server use the same code path
through their SQLAlchemy dialects, but need your accounts to test end to end.

## Security model

- **Who sees what:** every route under `/projects/{id}` requires membership of that project, and
  uses the user's role *on that project*. Admins see everything; anyone else sees only their
  projects, and other clients' projects answer "not found". Whoever registers a project becomes
  its first member; leads add others with `POST /api/v1/projects/{id}/members`.
- **Sign-in:** `L2C_AUTH_MODE=local` (passwords + signed tokens) or `oidc` (single sign-on: tokens
  from your identity provider are verified against its published keys, issuer, audience and expiry;
  `L2C_OIDC_ROLE_MAP` turns identity-provider roles or groups into product roles; passwords are
  disabled).
- **Outside dev** (`L2C_ENVIRONMENT` not `dev`): no demo accounts are created, and the API refuses to
  start with the default token secret or a wildcard CORS origin. Set `L2C_BOOTSTRAP_ADMIN_PASSWORD`
  once to create the first admin when using local sign-in.
- **Files:** uploaded file names are reduced to a plain name with an allowed extension and stored in
  the project's own upload folder. Server paths given over the API (Discover options, client
  configs) must be inside `L2C_ALLOWED_IMPORT_ROOTS`, and client configs registered over the API
  must use `vault://` / `env://` connection references. Only admins write secrets.
- **Data samples:** masked row samples from failed checks are deleted after
  `reconciliation.l4_retention_days`; counts stay as evidence.
- **Deployment:** the Helm chart keeps the database URL and other credentials in a Kubernetes
  Secret; when several API replicas start, a database lock lets only one run migrations.
