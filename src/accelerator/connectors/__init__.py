"""Database connectors: one small interface over every source and target engine.

A connection reference (usually a `vault://` secret) resolves to a URI:

* ``duckdb:///path.duckdb``                         -> DuckDB (native, used as a local stand-in)
* ``sqlite:///path.db``                             -> SQLite via SQLAlchemy
* ``postgresql+psycopg2://...``                     -> PostgreSQL via SQLAlchemy
* ``mssql+pyodbc://...``                            -> SQL Server via SQLAlchemy (``[sqlserver]`` extra)
* ``mssql+pyodbc://...fabric.microsoft.com...``     -> Microsoft Fabric Warehouse SQL endpoint
* ``snowflake://...``                               -> Snowflake (``[snowflake]`` extra)
* ``databricks://...``                              -> Databricks SQL warehouse (``[databricks]`` extra)

Everything the movers and validators do goes through `Connector`, so each engine only has to
answer a few questions: how to run SQL, how to stream rows out as Arrow, how to write rows in.
"""

from __future__ import annotations

from accelerator.connectors.base import Connector, open_connector

__all__ = ["Connector", "open_connector"]
