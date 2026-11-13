"""Run the full pipeline for an example whose legacy database is Oracle or Teradata.

The example's DDL export gives the catalog (as a client's export would); a DuckDB stand-in
holds the same tables' data so Move and the generated models can run here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from accelerator.core.secrets import get_vault
from accelerator.db.models import ResidueItem
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine


def run_pipeline(
    tmp: Path, example: Path, platform: str, approve_type_reviews: bool = True
) -> tuple[str, Path, Path]:
    spec = importlib.util.spec_from_file_location(
        f"seed_{example.name}", example / "seed_legacy_db.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src, tgt = mod.seed_all(base_dir=tmp / f"{example.name}_{platform}")
    raw = (example / "client_config.yaml").read_text(encoding="utf-8")
    src_ref = next(
        line.split(":", 1)[1].strip()
        for line in raw.splitlines()
        if line.strip().startswith("connection:")
    )
    tgt_ref = [
        line.split(":", 1)[1].strip()
        for line in raw.splitlines()
        if line.strip().startswith("connection:")
    ][1]
    get_vault().set_secret(src_ref, f"duckdb:///{src.as_posix()}")
    get_vault().set_secret(tgt_ref, f"duckdb:///{tgt.as_posix()}")
    raw = raw.replace("platform: snowflake", f"platform: {platform}")
    raw = raw.replace("project: ", f"project: {platform}_", 1)
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, raw)
        pid = proj.id
        DiscoverEngine().run(db, pid)
        AssessEngine().run(db, pid)
        ConvertEngine().run(db, pid)
        if approve_type_reviews:  # a reviewer accepts the documented type mappings
            for item in db.query(ResidueItem).filter(
                ResidueItem.project_id == pid, ResidueItem.reason_code == "TYPE_MAPPING_REVIEW"
            ):
                ConvertEngine().decide_residue(
                    db, pid, item.id, "approved", "", "Mapping accepted", "rev", "reviewer"
                )
            ConvertEngine().run(db, pid)
        MoveEngine().run(db, pid)
    return pid, src, tgt
