"""Load the demo data into the Oracle and SQL Server containers (demo/docker-compose.yml).

    python demo/seed_demo.py              # both databases
    python demo/seed_demo.py oracle       # only Oracle
    python demo/seed_demo.py sqlserver    # only SQL Server

Safe to run again: it drops and recreates everything it owns. Needs `pip install -e ".[demo]"`.

Use case 1 - Oracle SALES schema (moves to Snowflake)
    customers, orders, orders_archive; 4 views using Oracle-only SQL (DECODE, NVL, (+) joins,
    ROWNUM, date arithmetic, NLS date literals); one PL/SQL procedure; one sequence.
    Oracle-specific data: DATE with a time of day, NUMBER without precision, CLOB,
    TIMESTAMP WITH TIME ZONE, and '' stored as NULL.

Use case 2 - SQL Server database `acme` (moves to Databricks, with SSIS/Informatica/DataStage jobs)
    dbo.customers, dbo.products, dbo.orders, dbo.stg_orders, dbo.customer_totals,
    dbo.audit_notes, dbo.unused_temp_log, finance.gl_entries; one view; one T-SQL procedure.

Each database also gets a read-only login for the accelerator (l2c_reader), the way a client's
DBA would set one up.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

HOST = "localhost"
ORACLE = {"port": 1521, "service": "FREEPDB1", "system_password": "L2cDemo2026"}
ORACLE_OWNER = ("sales", "L2cSales2026")
ORACLE_READER = ("l2c_reader", "L2cReader2026")
MSSQL = {"port": 1433, "sa_password": "L2c_Demo_2026!", "database": "acme"}
MSSQL_READER = ("l2c_reader", "L2c_Reader_2026!")

N_ORA_CUSTOMERS, N_ORA_ORDERS = 2_000, 40_000
N_MS_CUSTOMERS, N_MS_PRODUCTS, N_MS_ORDERS = 3_000, 250, 60_000


def wait_for(name: str, connect: Callable[[], Any], minutes: int = 6) -> Any:
    """Databases take a minute or two to start the first time; keep trying."""
    deadline = time.time() + minutes * 60
    last: Exception | None = None
    while time.time() < deadline:
        try:
            return connect()
        except Exception as exc:
            last = exc
            print(f"  waiting for {name} to accept connections...")
            time.sleep(10)
    raise SystemExit(f"{name} did not start in {minutes} minutes: {last}")


def batches(rows: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


# --------------------------------------------------------------------------------------------
# Use case 1: Oracle
# --------------------------------------------------------------------------------------------
ORACLE_OBJECTS = [
    """CREATE TABLE customers (
        customer_id  NUMBER(10,0) NOT NULL,
        full_name    VARCHAR2(120 CHAR) NOT NULL,
        email        VARCHAR2(200),
        segment      VARCHAR2(10),
        created_on   DATE DEFAULT SYSDATE NOT NULL,
        credit_limit NUMBER,
        CONSTRAINT pk_customers PRIMARY KEY (customer_id))""",
    "COMMENT ON COLUMN customers.segment IS 'ENT = enterprise; SMB = small business'",
    """CREATE TABLE orders (
        order_id     NUMBER(12,0) NOT NULL,
        customer_id  NUMBER(10,0) NOT NULL,
        order_date   DATE NOT NULL,
        status       CHAR(1),
        amount       NUMBER(12,2) NOT NULL,
        discount     NUMBER(12,2),
        note         VARCHAR2(400 CHAR),
        updated_ts   TIMESTAMP(6) WITH TIME ZONE,
        payload      CLOB,
        CONSTRAINT pk_orders PRIMARY KEY (order_id),
        CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id))""",
    "CREATE INDEX ix_orders_date ON orders (order_date)",
    "CREATE TABLE orders_archive AS SELECT * FROM orders WHERE 1 = 0",
    "CREATE SEQUENCE order_seq START WITH 100001 INCREMENT BY 1 CACHE 20",
    """CREATE OR REPLACE VIEW v_order_summary
        (order_id, customer_label, order_day, due_date, status_text, net_amount, note_flag,
         email_prefix) AS
      SELECT o.order_id,
             c.full_name || ' (' || c.segment || ')',
             TRUNC(o.order_date),
             o.order_date + 30,
             DECODE(o.status, 'N', 'New', 'S', 'Shipped', NULL, 'Unknown', 'Other'),
             o.amount - NVL(o.discount, 0),
             CASE WHEN o.note = '' THEN 'EMPTY' WHEN o.note IS NULL THEN 'NONE' ELSE 'TEXT' END,
             SUBSTR(c.email, 0, 3)
        FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id""",
    """CREATE OR REPLACE VIEW v_customer_activity
        (customer_id, full_name, order_count, last_order_month) AS
      SELECT c.customer_id, c.full_name, COUNT(o.order_id), MAX(TO_CHAR(o.order_date, 'YYYY-MM'))
        FROM customers c, orders o
       WHERE c.customer_id = o.customer_id(+)
       GROUP BY c.customer_id, c.full_name""",
    """CREATE OR REPLACE VIEW v_top_orders (order_id, amount) AS
      SELECT order_id, amount FROM (SELECT order_id, amount FROM orders ORDER BY amount DESC)
       WHERE ROWNUM <= 5""",
    """CREATE OR REPLACE VIEW v_recent_orders (order_id, order_date) AS
      SELECT order_id, order_date FROM orders
       WHERE order_date >= '01-MAR-26'""",  # relies on the session's NLS date format
    """CREATE OR REPLACE PROCEDURE p_archive_orders (p_before IN DATE) AS
        v_count NUMBER := 0;
      BEGIN
        FOR r IN (SELECT order_id FROM orders WHERE order_date < p_before) LOOP
          INSERT INTO orders_archive SELECT * FROM orders WHERE order_id = r.order_id;
          DELETE FROM orders WHERE order_id = r.order_id;
          v_count := v_count + 1;
        END LOOP;
        COMMIT;
        DBMS_OUTPUT.PUT_LINE('Archived ' || v_count);
      END p_archive_orders;""",
]
ORACLE_READABLE = [
    "customers",
    "orders",
    "orders_archive",
    "v_order_summary",
    "v_customer_activity",
    "v_top_orders",
    "v_recent_orders",
]


def oracle_rows() -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    customers = []
    for i in range(1, N_ORA_CUSTOMERS + 1):
        customers.append(
            (
                i,
                f"Customer {i:04d}",
                None if i % 9 == 0 else f"cust{i:04d}@globalsales.example",
                None if i % 11 == 0 else ("ENT" if i % 3 == 0 else "SMB"),
                datetime(2025, 1, 1, 9, 30) + timedelta(days=i % 365, minutes=i),
                # NUMBER without precision: values with different scales
                None if i % 4 else Decimal(i) * Decimal("10.125"),
            )
        )
    orders = []
    base = datetime(2025, 6, 1, 8, 0)
    for i in range(1, N_ORA_ORDERS + 1):
        ts = base + timedelta(minutes=13 * i)
        orders.append(
            (
                i,
                (i * 7) % N_ORA_CUSTOMERS + 1,
                ts,
                [None, "N", "S", "X"][i % 4],
                Decimal(25) + Decimal(i % 500) * Decimal("1.75"),
                None if i % 3 == 0 else (Decimal(i % 40) * Decimal("0.25")),
                # Oracle stores '' as NULL: these rows arrive on the target as NULL too.
                "" if i % 5 == 0 else (None if i % 2 else f"note {i}"),
                ts + timedelta(hours=2),  # stored as UTC (+00:00)
                f'{{"id": {i}, "channel": "{["web", "store", "phone"][i % 3]}"}}',
            )
        )
    return customers, orders


def seed_oracle() -> None:
    import oracledb

    dsn = f"{HOST}:{ORACLE['port']}/{ORACLE['service']}"
    print(f"Oracle ({dsn})")
    system = wait_for(
        "Oracle",
        lambda: oracledb.connect(user="system", password=ORACLE["system_password"], dsn=dsn),
    )
    owner, owner_pw = ORACLE_OWNER
    reader, reader_pw = ORACLE_READER
    with system.cursor() as cur:
        for user in (reader, owner):
            cur.execute(
                f"BEGIN EXECUTE IMMEDIATE 'DROP USER {user} CASCADE'; "
                "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -1918 THEN RAISE; END IF; END;"
            )
        cur.execute(f'CREATE USER {owner} IDENTIFIED BY "{owner_pw}" QUOTA UNLIMITED ON users')
        cur.execute(
            f"GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, "
            f"CREATE PROCEDURE TO {owner}"
        )
        cur.execute(f'CREATE USER {reader} IDENTIFIED BY "{reader_pw}"')
        cur.execute(f"GRANT CREATE SESSION TO {reader}")
    system.close()

    conn = oracledb.connect(user=owner, password=owner_pw, dsn=dsn)
    with conn.cursor() as cur:
        for ddl in ORACLE_OBJECTS:
            cur.execute(ddl)
        customers, orders = oracle_rows()
        cur.executemany(
            "INSERT INTO customers VALUES (:1, :2, :3, :4, :5, :6)", customers, batcherrors=False
        )
        for chunk in batches(orders, 5_000):
            cur.executemany(
                "INSERT INTO orders VALUES (:1, :2, :3, :4, :5, :6, :7, "
                "FROM_TZ(CAST(:8 AS TIMESTAMP), '+00:00'), :9)",
                list(chunk),
            )
        for obj in ORACLE_READABLE:
            cur.execute(f"GRANT SELECT ON {obj} TO {reader}")
        # EXECUTE lets the read-only login see the procedure's code (ALL_SOURCE).
        cur.execute(f"GRANT EXECUTE ON p_archive_orders TO {reader}")
        conn.commit()
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM customers), (SELECT COUNT(*) FROM orders) FROM dual"
        )
        n_c, n_o = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM orders WHERE note IS NULL")
        n_null = cur.fetchone()[0]
    conn.close()
    print(
        f"  schema SALES: customers {n_c:,} rows, orders {n_o:,} rows ({n_null:,} notes are NULL)"
    )
    print("  4 views, 1 procedure, 1 sequence")
    print(f"  read-only login for the accelerator: {reader} / {reader_pw}")


# --------------------------------------------------------------------------------------------
# Use case 2: SQL Server
# --------------------------------------------------------------------------------------------
MSSQL_OBJECTS = [
    "CREATE SCHEMA finance",
    """CREATE TABLE dbo.customers (
        customer_id INT NOT NULL PRIMARY KEY,
        full_name   NVARCHAR(120) NOT NULL,
        email       VARCHAR(200) NULL,
        ssn         VARCHAR(20) NULL,
        segment     VARCHAR(40) NULL,
        is_active   BIT NOT NULL,
        created_at  DATETIME2 NOT NULL)""",
    """CREATE TABLE dbo.products (
        product_id   INT NOT NULL PRIMARY KEY,
        sku          VARCHAR(40) NOT NULL,
        product_name NVARCHAR(150) NOT NULL,
        unit_price   DECIMAL(12,2) NOT NULL,
        list_price   MONEY NULL)""",
    """CREATE TABLE dbo.orders (
        order_id     INT NOT NULL PRIMARY KEY,
        customer_id  INT NOT NULL REFERENCES dbo.customers (customer_id),
        product_id   INT NOT NULL REFERENCES dbo.products (product_id),
        quantity     INT NOT NULL,
        gross_amount DECIMAL(14,2) NOT NULL,
        discount     DECIMAL(10,2) NULL,
        updated_at   DATETIME2 NOT NULL)""",
    """CREATE TABLE dbo.stg_orders (
        order_id     INT NOT NULL,
        customer_id  INT NOT NULL,
        product_id   INT NOT NULL,
        quantity     INT NOT NULL,
        gross_amount DECIMAL(14,2) NOT NULL,
        discount     DECIMAL(10,2) NULL,
        updated_at   DATETIME2 NOT NULL,
        load_batch   INT NOT NULL)""",
    """CREATE TABLE dbo.customer_totals (
        customer_id INT NOT NULL PRIMARY KEY,
        net_spend   DECIMAL(16,2) NOT NULL)""",
    """CREATE TABLE dbo.audit_notes (
        note_id INT NOT NULL PRIMARY KEY,
        body    NVARCHAR(MAX) NOT NULL)""",
    """CREATE TABLE dbo.unused_temp_log (
        log_id  INT NOT NULL PRIMARY KEY,
        message VARCHAR(500) NULL)""",
    """CREATE TABLE finance.gl_entries (
        entry_id      INT NOT NULL PRIMARY KEY,
        order_id      INT NOT NULL REFERENCES dbo.orders (order_id),
        account_code  VARCHAR(20) NOT NULL,
        debit_amount  DECIMAL(14,2) NOT NULL,
        credit_amount DECIMAL(14,2) NOT NULL,
        posted_at     DATETIME2 NOT NULL)""",
    """CREATE VIEW dbo.v_customer_orders AS
    SELECT c.customer_id,
           c.full_name,
           COUNT(o.order_id) AS order_count,
           SUM(o.gross_amount - ISNULL(o.discount, 0)) AS net_spend
      FROM dbo.customers c
      JOIN dbo.orders o ON c.customer_id = o.customer_id
     GROUP BY c.customer_id, c.full_name""",
    """CREATE PROCEDURE dbo.usp_refresh_customer_totals @since DATETIME2 AS
    BEGIN
        SET NOCOUNT ON;
        IF OBJECT_ID('tempdb..#totals') IS NOT NULL DROP TABLE #totals;
        SELECT customer_id, SUM(gross_amount - ISNULL(discount, 0)) AS net_spend
          INTO #totals
          FROM dbo.orders
         WHERE updated_at >= @since
         GROUP BY customer_id;
        MERGE dbo.customer_totals AS t
        USING #totals AS s ON t.customer_id = s.customer_id
        WHEN MATCHED THEN UPDATE SET net_spend = s.net_spend
        WHEN NOT MATCHED THEN INSERT (customer_id, net_spend) VALUES (s.customer_id, s.net_spend);
    END""",
]


def mssql_rows() -> dict[str, list[tuple[Any, ...]]]:
    base = datetime(2026, 1, 1, 8, 0)
    customers = [
        (
            i,
            f"Customer {i:04d}",
            None if i % 13 == 0 else f"cust{i:04d}@acme.example",
            f"123-45-{1000 + i}",
            None if i % 17 == 0 else ("ENT" if i % 2 == 0 else "SMB"),
            i % 10 != 0,
            base + timedelta(hours=i),
        )
        for i in range(1, N_MS_CUSTOMERS + 1)
    ]
    products = [
        (
            i,
            f"SKU-{100 + i:04d}",
            f"Widget {i:04d}",
            Decimal(i % 60 + 1) * Decimal("4.99"),
            None if i % 7 == 0 else Decimal(i % 60 + 1) * Decimal("5.4900"),
        )
        for i in range(1, N_MS_PRODUCTS + 1)
    ]
    orders, gl = [], []
    for i in range(1, N_MS_ORDERS + 1):
        pid = (i - 1) % N_MS_PRODUCTS + 1
        qty = i % 5 + 1
        gross = (Decimal(qty) * Decimal(pid % 60 + 1) * Decimal("4.99")).quantize(Decimal("0.01"))
        disc = (gross * Decimal("0.05")).quantize(Decimal("0.01")) if i % 3 == 0 else None
        ts = base + timedelta(minutes=7 * i)
        orders.append((i, (i - 1) % N_MS_CUSTOMERS + 1, pid, qty, gross, disc, ts))
        net = gross - (disc or Decimal(0))
        gl.append((i, i, "4000-REV" if i % 10 else "2100-AR", net, net, ts + timedelta(hours=1)))
    stg = [(*o, 7) for o in orders[-1_000:]]
    notes = [(i, f"Audit note {i:05d}: order batch reviewed") for i in range(1, 201)]
    return {
        "dbo.customers": customers,
        "dbo.products": products,
        "dbo.orders": orders,
        "finance.gl_entries": gl,
        "dbo.stg_orders": stg,
        "dbo.audit_notes": notes,
        "dbo.unused_temp_log": [(1, "legacy debug line")],
    }


def seed_sqlserver() -> None:
    import pymssql

    db = MSSQL["database"]
    reader, reader_pw = MSSQL_READER
    print(f"SQL Server ({HOST}:{MSSQL['port']})")

    def connect(database: str) -> Any:
        return pymssql.connect(
            server=HOST,
            port=str(MSSQL["port"]),
            user="sa",
            password=MSSQL["sa_password"],
            database=database,
            autocommit=True,
            tds_version="7.4",
        )

    master = wait_for("SQL Server", lambda: connect("master"))
    cur = master.cursor()
    cur.execute(
        f"IF DB_ID('{db}') IS NOT NULL BEGIN "
        f"ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{db}]; END"
    )
    cur.execute(f"CREATE DATABASE [{db}]")
    cur.execute(
        f"IF SUSER_ID('{reader}') IS NULL "
        f"CREATE LOGIN {reader} WITH PASSWORD = '{reader_pw}', CHECK_POLICY = OFF"
    )
    master.close()

    conn = connect(db)
    cur = conn.cursor()
    for ddl in MSSQL_OBJECTS:
        cur.execute(ddl)
    for table, rows in mssql_rows().items():
        width = len(rows[0])
        row_ph = "(" + ", ".join(["%s"] * width) + ")"
        for chunk in batches(rows, 500):
            sql = f"INSERT INTO {table} VALUES " + ", ".join([row_ph] * len(chunk))
            cur.execute(sql, tuple(v for row in chunk for v in row))
    cur.execute(f"CREATE USER {reader} FOR LOGIN {reader}")
    cur.execute(f"ALTER ROLE db_datareader ADD MEMBER {reader}")
    cur.execute(f"GRANT VIEW DEFINITION TO {reader}")  # lets it read view and procedure code
    counts = {}
    for t in ("dbo.customers", "dbo.products", "dbo.orders", "finance.gl_entries"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        counts[t] = cur.fetchone()[0]
    conn.close()
    print(f"  database {db}: " + ", ".join(f"{t} {n:,} rows" for t, n in counts.items()))
    print("  plus stg_orders, customer_totals, audit_notes, unused_temp_log; 1 view, 1 procedure")
    print(f"  read-only login for the accelerator: {reader} / {reader_pw}")


def main(argv: list[str]) -> None:
    which = argv[1:] or ["oracle", "sqlserver"]
    unknown = set(which) - {"oracle", "sqlserver"}
    if unknown:
        raise SystemExit(f"Unknown: {', '.join(unknown)}. Use: oracle, sqlserver")
    started = time.time()
    if "oracle" in which:
        seed_oracle()
    if "sqlserver" in which:
        seed_sqlserver()
    print(f"Done in {time.time() - started:.0f}s ({datetime.now(UTC):%H:%M} UTC).")


if __name__ == "__main__":
    main(sys.argv)
