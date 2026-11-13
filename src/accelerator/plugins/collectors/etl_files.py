"""Collect ETL exports (.dtsx, .ispac, Informatica XML, DataStage .dsx/.isx, SQL) with hashing."""

from __future__ import annotations

import zipfile
from pathlib import Path

from accelerator.plugins.interfaces import CollectedArtifact

EXTENSION_MAP: dict[str, tuple[str, str]] = {
    ".dtsx": ("dtsx", "ssis"),
    ".xml": ("informatica_xml", "informatica"),
    ".dsx": ("dsx", "datastage"),
    ".sql": ("sql", "tsql"),  # replaced by the source database's own SQL dialect
    ".bteq": ("sql", "bteq"),
    ".btq": ("sql", "bteq"),
    ".pls": ("sql", "plsql"),
    ".pks": ("sql", "plsql"),
    ".pkb": ("sql", "plsql"),
    ".prc": ("sql", "plsql"),
    ".fnc": ("sql", "plsql"),
}
SQL_TOOL_BY_DATABASE = {"oracle": "plsql", "teradata": "bteq"}


def _xml_tool(head: str, default: tuple[str, str]) -> tuple[str, str]:
    if "DTS:Executable" in head or "www.microsoft.com/SqlServer/Dts" in head:
        return "dtsx", "ssis"
    if "<DSExport" in head:
        return "dsx", "datastage"
    return default


def collect_etl_artifacts(
    paths: list[str | Path], default_tool: str = "ssis", source_database: str = "sqlserver"
) -> list[CollectedArtifact]:
    """Walk given files/directories and collect ETL/SQL exports, unpacking .ispac/.isx zips."""
    artifacts: list[CollectedArtifact] = []

    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file_path in files:
            ext = file_path.suffix.lower()
            if ext in (".ispac", ".isx"):
                with zipfile.ZipFile(file_path, "r") as zf:
                    for member in sorted(zf.namelist()):
                        m_ext = Path(member).suffix.lower()
                        if m_ext in (".dtsx", ".xml", ".dsx", ".sql"):
                            art_type, tool = EXTENSION_MAP.get(m_ext, ("xml", default_tool))
                            data = zf.read(member)
                            if m_ext == ".xml":  # .isx archives hold DataStage XML jobs
                                default = (
                                    ("dsx", "datastage") if ext == ".isx" else (art_type, tool)
                                )
                                art_type, tool = _xml_tool(
                                    data[:500].decode("utf-8", errors="ignore"), default
                                )
                            if m_ext == ".sql":
                                tool = SQL_TOOL_BY_DATABASE.get(source_database, "tsql")
                            artifacts.append(
                                CollectedArtifact(
                                    artifact_key=f"{file_path.name}!/{member}",
                                    artifact_type=art_type,
                                    source_tool=tool,
                                    content_bytes=data,
                                    metadata={"archive": str(file_path)},
                                )
                            )
            elif ext in EXTENSION_MAP:
                art_type, tool = EXTENSION_MAP[ext]
                if ext == ".xml":
                    # SSIS, DataStage XML or Informatica XML: tell them apart by the header
                    head = file_path.read_bytes()[:500].decode("utf-8", errors="ignore")
                    art_type, tool = _xml_tool(head, (art_type, tool))
                if ext == ".sql":
                    tool = SQL_TOOL_BY_DATABASE.get(source_database, "tsql")
                artifacts.append(
                    CollectedArtifact(
                        artifact_key=file_path.name,
                        artifact_type=art_type,
                        source_tool=tool,
                        content_bytes=file_path.read_bytes(),
                        metadata={"source_path": str(file_path)},
                    )
                )

    return artifacts
