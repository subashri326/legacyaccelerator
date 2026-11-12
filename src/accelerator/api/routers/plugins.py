"""Plugin registry inspection and golden test execution routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_perm
from accelerator.core.security import Permission
from accelerator.db.models import PluginRegistryEntry
from accelerator.plugins.registry import get_plugin_registry

router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])


@router.get("")
def list_plugins(
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    reg = get_plugin_registry()
    reg.sync_to_db(db)
    db.commit()
    rows = (
        db.query(PluginRegistryEntry)
        .order_by(PluginRegistryEntry.plugin_type, PluginRegistryEntry.name)
        .all()
    )
    return [
        {
            "name": r.name,
            "plugin_type": r.plugin_type,
            "version": r.version,
            "capabilities": r.capabilities,
            "enabled": r.enabled,
            "last_golden_status": r.last_golden_status,
        }
        for r in rows
    ]


@router.post("/run-golden")
def run_golden_tests(
    _user: CurrentUser = Depends(require_perm(Permission.MANAGE_PLUGINS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    reg = get_plugin_registry()
    reg.sync_to_db(db)
    results = []
    for plugin in reg.list_unique():
        passed, msg = plugin.run_golden_check()
        row = (
            db.query(PluginRegistryEntry)
            .filter(PluginRegistryEntry.name == plugin.manifest.name)
            .first()
        )
        if row:
            row.last_golden_status = "passed" if passed else "failed"
        results.append(
            {
                "name": plugin.manifest.name,
                "plugin_type": plugin.manifest.plugin_type,
                "passed": passed,
                "message": msg,
            }
        )
    db.commit()
    return results
