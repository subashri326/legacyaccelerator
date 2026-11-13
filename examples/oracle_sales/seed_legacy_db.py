"""Create a DuckDB stand-in holding the Oracle SALES schema's data (for demos and tests).

Oracle has no empty strings ('' is NULL), so the data has NULLs and no '' values.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def seed_all(base_dir: Path) -> tuple[Path, Path]:
    base_dir.mkdir(parents=True, exist_ok=True)
    src, tgt = base_dir / "oracle_sales.duckdb", base_dir / "target.duckdb"
    for p in (src, tgt):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(src))
    con.execute("CREATE SCHEMA sales")
    con.execute("""CREATE TABLE sales.customers (customer_id BIGINT PRIMARY KEY,
        full_name VARCHAR NOT NULL, email VARCHAR, segment VARCHAR, created_on TIMESTAMP NOT NULL,
        credit_limit DECIMAL(38,10))""")
    con.execute("""CREATE TABLE sales.orders (order_id BIGINT PRIMARY KEY, customer_id BIGINT NOT
        NULL, order_date TIMESTAMP NOT NULL, status VARCHAR, amount DECIMAL(12,2) NOT NULL,
        discount DECIMAL(12,2), note VARCHAR, updated_ts TIMESTAMPTZ, payload VARCHAR)""")
    for i in range(1, 13):
        con.execute(
            "INSERT INTO sales.customers VALUES (?, ?, ?, ?, TIMESTAMP '2026-01-05 09:30:00' + "
            "INTERVAL (?) DAY, ?)",
            [i, f"Customer {i}", None if i % 4 == 0 else f"cust{i}@example.com",
             None if i % 5 == 0 else ("ENT" if i % 2 else "SMB"), i, None if i % 3 else 1000.5],
        )
    for i in range(1, 61):
        con.execute(
            "INSERT INTO sales.orders VALUES (?, ?, TIMESTAMP '2026-02-01 08:15:00' + "
            "INTERVAL (?) HOUR, ?, ?, ?, ?, TIMESTAMPTZ '2026-02-10 10:00:00+00', ?)",
            [i, (i % 12) + 1, i * 13, [None, "N", "S", "X"][i % 4], 25 + i * 7.5,
             None if i % 3 == 0 else round(i * 0.25, 2), None if i % 2 else f"note {i}",
             '{"id": %d}' % i],
        )
    con.close()
    duckdb.connect(str(tgt)).close()
    return src, tgt
