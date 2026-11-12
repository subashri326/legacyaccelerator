"""Seed realistic DuckDB legacy and empty target databases for end-to-end verification."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb


def seed_all(base_dir: Path | str = ".l2c_data/demo_acme") -> tuple[Path, Path]:
    out_dir = Path(base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_path = out_dir / "legacy_acme.duckdb"
    tgt_path = out_dir / "target_cloud.duckdb"

    if src_path.exists():
        src_path.unlink()
    if tgt_path.exists():
        tgt_path.unlink()

    conn = duckdb.connect(str(src_path))
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS dbo;")
        conn.execute("CREATE SCHEMA IF NOT EXISTS finance;")
        conn.execute(
            """
            CREATE TABLE dbo.customers (
                customer_id INT PRIMARY KEY,
                full_name VARCHAR,
                email VARCHAR,
                ssn VARCHAR,
                segment VARCHAR,
                created_at TIMESTAMP
            );
            CREATE TABLE dbo.products (
                product_id INT PRIMARY KEY,
                sku VARCHAR,
                product_name VARCHAR,
                unit_price DECIMAL(12,2)
            );
            CREATE TABLE dbo.orders (
                order_id INT PRIMARY KEY,
                customer_id INT,
                product_id INT,
                quantity INT,
                gross_amount DECIMAL(14,2),
                discount DECIMAL(10,2),
                updated_at TIMESTAMP
            );
            CREATE TABLE finance.gl_entries (
                entry_id INT PRIMARY KEY,
                order_id INT,
                account_code VARCHAR,
                debit_amount DECIMAL(14,2),
                credit_amount DECIMAL(14,2),
                posted_at TIMESTAMP
            );
            CREATE TABLE dbo.unused_temp_log (
                log_id INT PRIMARY KEY,
                message VARCHAR
            );
            """
        )
        base_ts = datetime(2026, 1, 1, 8, 0, 0)
        for i in range(1, 21):
            conn.execute(
                "INSERT INTO dbo.customers VALUES (?, ?, ?, ?, ?, ?)",
                [i, f"Customer {i}", f"cust{i}@acme.example", f"123-45-{1000 + i}", "ENT" if i % 2 == 0 else "SMB", base_ts + timedelta(days=i)],
            )
        for i in range(1, 11):
            conn.execute(
                "INSERT INTO dbo.products VALUES (?, ?, ?, ?)",
                [i, f"SKU-{100+i}", f"Widget {i}", round(19.99 * i, 2)],
            )
        for i in range(1, 121):
            cid = ((i - 1) % 20) + 1
            pid = ((i - 1) % 10) + 1
            qty = (i % 5) + 1
            gross = round(qty * 19.99 * pid, 2)
            disc = round(gross * 0.05, 2) if i % 3 == 0 else 0.0
            ts = base_ts + timedelta(hours=i)
            conn.execute(
                "INSERT INTO dbo.orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                [i, cid, pid, qty, gross, disc, ts],
            )
            conn.execute(
                "INSERT INTO finance.gl_entries VALUES (?, ?, ?, ?, ?, ?)",
                [i, i, "4000-REV", round(gross - disc, 2), round(gross - disc, 2), ts],
            )
        conn.execute("INSERT INTO dbo.unused_temp_log VALUES (1, 'legacy debug line')")
    finally:
        conn.close()

    # Initialize empty target DuckDB
    t_conn = duckdb.connect(str(tgt_path))
    t_conn.close()
    return src_path, tgt_path


if __name__ == "__main__":
    s, t = seed_all()
    print(f"Seeded legacy={s} and target={t}")
