# Live demo: Oracle and SQL Server to Snowflake and Databricks

Two real old databases run on your laptop in Docker. The accelerator moves them to your
Snowflake and Databricks trial accounts while the audience watches each step in the web app.

| | Use case 1 | Use case 2 |
|---|---|---|
| Old system | **Oracle** 23ai Free, schema `SALES` | **SQL Server** 2022, database `acme` |
| Also brings | 4 views in Oracle-only SQL, 1 PL/SQL procedure | 1 view, 1 T-SQL procedure, **SSIS, Informatica and DataStage jobs** |
| Moves to | **Snowflake** | **Databricks** |
| Data | 2,000 customers, 40,000 orders | 3,000 customers, 60,000 orders, 60,000 ledger rows |
| Shows off | Oracle quirks handled: `''` is NULL, `DATE` has a time, `NUMBER` without precision, CLOB, time zones | Jobs from three ETL tools converted, staging tables, row-by-row checks |

You need: Docker Desktop (running), the project set up as in the main README, a Snowflake trial,
and a Databricks account (Free Edition or trial). Allow about 30 minutes the first time, mostly
for downloads.

---

## Part A — one-time setup

### A1. Start the two old databases

In a VS Code terminal, from the project folder:

```powershell
docker compose -f demo/docker-compose.yml up -d
docker compose -f demo/docker-compose.yml ps
```

The first run downloads about 3 GB. Wait until both show `(healthy)` in the `STATUS` column
(Oracle takes 1–2 minutes after the download). Docker Desktop needs about 4 GB of memory for both.

### A2. Install the database drivers

```powershell
.venv\Scripts\Activate.ps1
pip install -e ".[demo]"
```

This adds the Oracle, SQL Server, Snowflake and Databricks drivers. Nothing else needs installing:
the SQL Server driver (pymssql) and the Oracle driver (python-oracledb) are pure Python packages.

### A3. Load the demo data

```powershell
python demo/seed_demo.py
```

It prints the row counts and the read-only logins it created. It's safe to run again later; it resets
both databases. If you run it again, stop the backend first, because Oracle won't drop a user who is
still connected.

### A4. Prepare Snowflake

1. Sign in to Snowflake. Open **Projects → Worksheets → +** and run:

   ```sql
   CREATE DATABASE IF NOT EXISTS L2C_DEMO;
   SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS account_identifier,
          CURRENT_USER() AS user_name;
   ```

   Note the **account identifier** and **user name** it shows.

2. New Snowflake accounts may block tools that sign in with a password (MFA). Sign in with a key
   file instead. In the VS Code terminal:

   ```powershell
   python demo/snowflake_keypair.py YOUR_USER_NAME
   ```

   Copy the `ALTER USER ...` line it prints, run it in the Snowflake worksheet, and note the
   **key file path** it prints.

### A5. Prepare Databricks

1. In Databricks, open **SQL Warehouses**, open your warehouse (Free Edition has one called
   *Serverless Starter Warehouse*), go to **Connection details** and note the **Server hostname**
   and **HTTP path**. Start the warehouse if it is stopped.
2. Click your profile picture (top right) → **Settings → Developer → Access tokens →
   Generate new token**. Name it `l2c-demo` and copy the token.
3. Catalog: Free Edition uses **`workspace`**. In a trial, use the catalog shown under
   **Catalog** in the left menu (often your workspace name, or `main`).

---

## Part B — set up the connections in the app

Start the backend (`l2c serve --port 8000`) and the frontend, and sign in. Go to **Settings** and
add a client called `Global Sales` (for use case 1). The existing `Acme Retail` client is for
use case 2.

### Sources (pick the client in the sidebar first)

| | Oracle (client: Global Sales) | SQL Server (client: Acme Retail) |
|---|---|---|
| Tile | Oracle | Microsoft SQL Server |
| Name | `Oracle Sales (on-prem)` | `Acme SQL Server (on-prem)` |
| Host | `localhost` | `localhost` |
| Port | `1521` | `1433` |
| Service name / Database | `FREEPDB1` | `acme` |
| Username | `l2c_reader` | `l2c_reader` |
| Password | `L2cReader2026` | `L2c_Reader_2026!` |
| Driver | — | leave **pymssql** |

Press **Test connection** and then **Set up source**. On the source page, **Browse schemas** should
list `sales` (Oracle) or `dbo` and `finance` (SQL Server).

### Destinations

| | Snowflake (client: Global Sales) | Databricks (client: Acme Retail) |
|---|---|---|
| Name | `Snowflake trial` | `Databricks trial` |
| Account identifier | from A4 | — |
| Username | from A4 | — |
| Password | leave empty | — |
| Private key file | the path from A4 | — |
| Database | `L2C_DEMO` | — |
| Warehouse | `COMPUTE_WH` | — |
| Server hostname / HTTP path | — | from A5 |
| Access token | — | from A5 |
| Catalog | — | from A5 (`workspace` on Free Edition) |

---

## Part C — the demo

### Use case 1: Oracle → Snowflake (client: Global Sales)

1. **Migrations → New migration**: source `Oracle Sales (on-prem)`, destination `Snowflake trial`,
   tick schema **sales**, no job files, then **Create and start**.
2. Watch the five steps. Points to make while it runs:
   - **Read the old system**: it found the 3 tables and 4 views (and the stored procedure) by
     itself, through a read-only login.
   - **Estimate effort**: days of manual work, before anyone has started.
   - **Write new code**: Snowflake SQL was written; some items need a person (next step).
   - **Copy the data**: `customers` is **held**, because `credit_limit` is an Oracle `NUMBER`
     with no precision. The accelerator won't guess how many decimals to keep.
3. Open **Review**. Explain the items:
   - `credit_limit` type: **Approve**. It becomes `NUMBER(38,10)`, and the numbers check will prove
     nothing was lost.
   - `v_top_orders` uses `ROWNUM`, and `v_recent_orders` relies on an Oracle date-format setting,
     so neither is translated blindly. Paste a replacement and **Approve**:

     ```sql
     -- v_top_orders
     SELECT order_id, amount FROM curated_sales.orders ORDER BY amount DESC LIMIT 5
     -- v_recent_orders
     SELECT order_id, order_date FROM curated_sales.orders WHERE order_date >= '2026-03-01'
     ```
   - `p_archive_orders`: a PL/SQL loop. Approve with empty code ("not needed") or paste a
     rewrite.
4. **Run again**. All tables copy, and **Check the numbers** shows every table matching.
5. Prove it in Snowflake (worksheet):

   ```sql
   SELECT COUNT(*) FROM L2C_DEMO.CURATED_SALES.ORDERS;                         -- 40,000
   SELECT COUNT(*) FROM L2C_DEMO.CURATED_SALES.ORDERS WHERE NOTE IS NULL;      -- Oracle '' became NULL
   SELECT * FROM L2C_DEMO.CURATED_SALES.CUSTOMERS ORDER BY CUSTOMER_ID LIMIT 10;
   ```

### Use case 2: SQL Server + jobs → Databricks (client: Acme Retail)

1. **New migration**: source `Acme SQL Server (on-prem)`, destination `Databricks trial`, tick
   **dbo** and **finance**. Under job files, add all four from `examples/acme_retail`:
   `ssis/Load_Customers_And_Orders.dtsx`, `ssis/Load_Finance_GL.dtsx`,
   `informatica/wf_acme_orders.xml`, `datastage/acme_jobs.dsx`. Then **Create and start**.
2. Points to make: three different ETL tools go through one process; the jobs become Databricks
   SQL, shown in the **Generated code** tab.
3. **Review**: the items are the parts of the jobs that can't be translated with certainty. For
   example, the C# script task in SSIS has no Databricks equivalent (approve with empty code,
   meaning "not needed"), and a DataStage filter uses the job parameter `#pRegion#`, which needs a
   real value.
4. **Run again**, then show **Data & checks**: row counts on both sides, all matching.
5. Prove it in Databricks (SQL Editor):

   ```sql
   SELECT COUNT(*) FROM workspace.curated_dbo.orders;          -- 60,000
   SELECT account_code, SUM(debit_amount) FROM workspace.curated_finance.gl_entries GROUP BY 1;
   ```

   Use your catalog name instead of `workspace` in a trial.

Copying to Databricks takes a minute or two: rows go in as INSERTs of 5,000 rows each through
the SQL warehouse, which is fine at demo sizes. For large tables in a real project, the faster route is
cloud storage plus `COPY INTO`.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `seed_demo.py` keeps "waiting for Oracle" | `docker compose -f demo/docker-compose.yml ps`: wait for `(healthy)`. First start can take 3 minutes. |
| Port 1433 or 1521 already in use | Another SQL Server or Oracle is running. Stop it, or change the left-hand port in `docker-compose.yml` and use that port in the source form. |
| Snowflake: `JWT token is invalid` | The `ALTER USER` line was run for a different user, or the key was regenerated afterwards. Run `snowflake_keypair.py` and the new `ALTER USER` again. |
| Snowflake: `Object does not exist ... L2C_DEMO` | Run the `CREATE DATABASE` line from A4. |
| Databricks: `Invalid access token` | Generate a new token (A5) and paste it into the destination, then **Save changes**. |
| Databricks is slow on the first step | The warehouse is starting. Wait for it to show *Running* in **SQL Warehouses**. |
| A table shows **Doesn't match** | Open **Data & checks** and hover over the badge to see which column differs. Send me a screenshot. |

To stop the databases: `docker compose -f demo/docker-compose.yml stop`. To delete them and their
data: `docker compose -f demo/docker-compose.yml down -v`.
