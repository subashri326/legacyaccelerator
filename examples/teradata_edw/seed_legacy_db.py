"""Create a DuckDB stand-in holding the Teradata EDW database's data (for demos and tests)."""

from __future__ import annotations

from pathlib import Path

import duckdb


def seed_all(base_dir: Path) -> tuple[Path, Path]:
    base_dir.mkdir(parents=True, exist_ok=True)
    src, tgt = base_dir / "teradata_edw.duckdb", base_dir / "target.duckdb"
    for p in (src, tgt):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(src))
    con.execute("CREATE SCHEMA edw")
    con.execute("""CREATE TABLE edw.customer_dim (cust_id INTEGER PRIMARY KEY, cust_nm VARCHAR
        NOT NULL, seg_cd VARCHAR, city_nm VARCHAR, birth_dt DATE, score TINYINT, load_ts TIMESTAMP)""")
    con.execute("""CREATE TABLE edw.sales_fact (sale_id BIGINT NOT NULL, cust_id INTEGER NOT NULL,
        sale_dt DATE NOT NULL, qty INTEGER NOT NULL, units_per_box INTEGER NOT NULL,
        amt DECIMAL(18,2))""")
    con.execute("""CREATE TABLE edw.sales_mart (cust_id INTEGER PRIMARY KEY,
        total_amt DECIMAL(18,2), sale_cnt INTEGER)""")
    cities = ["Pune", "PUNE", "Chennai", None]
    for i in range(1, 11):
        con.execute(
            "INSERT INTO edw.customer_dim VALUES (?, ?, ?, ?, DATE '1980-01-01' + ?, ?, "
            "TIMESTAMP '2026-01-01 00:00:00')",
            [i, f"Customer {i}", "ENT" if i % 2 else "SMB", cities[i % 4], i * 400, i * 9],
        )
    for i in range(1, 51):
        con.execute(
            "INSERT INTO edw.sales_fact VALUES (?, ?, DATE '2026-03-01' + ?, ?, ?, ?)",
            [1000 + i, (i % 10) + 1, i % 20, i % 7, (i % 3) + 2, None if i % 6 == 0 else i * 3.25],
        )
    con.close()
    duckdb.connect(str(tgt)).close()
    return src, tgt
