"""Collect ETL exports (.dtsx, .ispac, Informatica XML, DataStage .dsx/.isx, SQL) with hashing."""

from __future__ import annotations

import zipfile
from pathlib import Path

from accelerator.plugins.interfaces import CollectedArtifact

EXTENSION_MAP: dict[str, tuple[str, str]] = {
    ".dtsx": ("dtsx", "ssis"),
    ".xml": ("informatica_xml", "informatica"),
    ".dsx": ("dsx", "datastage"),
    ".sql": ("sql", "tsql"),
    ".bteq": ("sql", "bteq"),
}


def collect_etl_artifacts(paths: list[str | Path], default_tool: str = "ssis") -> list[CollectedArtifact]:
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
                            artifacts.append(
                                CollectedArtifact(
                                    artifact_key=f"{file_path.name}!/{member}",
                                    artifact_type=art_type,
                                    source_tool=tool,
                                    content_bytes=zf.read(member),
                                    metadata={"archive": str(file_path)},
                                )
                            )
            elif ext in EXTENSION_MAP:
                art_type, tool = EXTENSION_MAP[ext]
                if ext == ".xml":
                    # Distinguish SSIS XML vs Informatica XML quickly by header
                    head = file_path.read_bytes()[:500].decode("utf-8", errors="ignore")
                    if "DTS:Executable" in head or "www.microsoft.com/SqlServer/Dts" in head:
                        art_type, tool = "dtsx", "ssis"
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
