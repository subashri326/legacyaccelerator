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
