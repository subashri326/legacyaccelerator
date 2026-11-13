"""Background pipeline runs with live, per-stage progress (for the web app).

A run is recorded in ``pipeline_run`` and executed on a background thread, one stage at a time.
Each stage commits its own work, so progress is visible to other requests as soon as a stage
moves on, and a failure stops the run at that stage with the error recorded. One run per project
at a time. Runs left unfinished by a server restart are marked "interrupted" at start-up.

(For several API servers, the same functions can be called from a job queue worker; the run row
is the single source of truth either way.)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

from accelerator.core.exceptions import ConfigError
from accelerator.db.base import utcnow
from accelerator.db.models import PipelineRun
from accelerator.db.session import session_scope

logger = logging.getLogger(__name__)

STAGES = ("discover", "assess", "convert", "move", "reconcile")
_ACTIVE = ("queued", "running")


def _summary(result: Any) -> dict[str, Any]:
    data = asdict(result) if is_dataclass(result) else dict(result or {})
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, list) and len(v) > 20:
            out[k] = [*v[:20], f"... {len(v) - 20} more"]
        elif isinstance(v, (str, int, float, bool, type(None), list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _run_stage(name: str, db: Any, project_id: str, actor: str, actor_role: str) -> Any:
    from accelerator.orchestration.runner import RunOrchestrator

    o = RunOrchestrator()
    if name == "discover":
        return o.discover_engine.run(db, project_id, actor=actor, actor_role=actor_role)
    if name == "assess":
        return o.assess_engine.run(db, project_id, actor=actor, actor_role=actor_role)
    if name == "convert":
        return o.convert_engine.run(db, project_id, actor=actor, actor_role=actor_role)
    if name == "move":
        return o.move_engine.run(db, project_id, actor=actor, actor_role=actor_role)
    if name == "reconcile":
        return o.reconcile_engine.run(db, project_id, actor=actor, actor_role=actor_role)
    raise ValueError(f"Unknown stage '{name}'")


def start_run(
    project_id: str,
    actor: str,
    actor_role: str,
    stages: list[str] | None = None,
    background: bool = True,
) -> str:
    """Record a run and start it. Returns the run id. Refuses if one is already active."""
    wanted = [s for s in STAGES if s in (stages or STAGES)]
    if not wanted:
        raise ConfigError(f"Choose at least one stage from {', '.join(STAGES)}")
    with session_scope() as db:
        busy = (
            db.query(PipelineRun)
            .filter(PipelineRun.project_id == project_id, PipelineRun.status.in_(_ACTIVE))
            .first()
        )
        if busy is not None:
            raise ConfigError(f"A run is already in progress for this project ({busy.id})")
        run = PipelineRun(
            project_id=project_id,
            status="queued",
            stages=[{"name": s, "status": "waiting"} for s in wanted],
            created_by=actor,
        )
        db.add(run)
        db.flush()
        run_id = run.id
    if background:
        threading.Thread(
            target=execute_run,
            args=(run_id, actor, actor_role),
            name=f"l2c-run-{run_id[:8]}",
            daemon=True,
        ).start()
    else:
        execute_run(run_id, actor, actor_role)
    return run_id


def _update(run_id: str, **changes: Any) -> None:
    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        stage_update = changes.pop("stage_update", None)
        if stage_update:
            name, fields = stage_update
            stages = [dict(s) for s in run.stages]
            for s in stages:
                if s["name"] == name:
                    s.update(fields)
            run.stages = stages  # reassign so the JSON change is saved
        for k, v in changes.items():
            setattr(run, k, v)


def execute_run(run_id: str, actor: str, actor_role: str) -> None:
    with session_scope() as db:
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        project_id = run.project_id
        names = [s["name"] for s in run.stages]
    _update(run_id, status="running", started_at=utcnow())
    for name in names:
        _update(
            run_id,
            current_stage=name,
            stage_update=(name, {"status": "running", "started_at": utcnow().isoformat()}),
        )
        try:
            with session_scope() as db:
                result = _run_stage(name, db, project_id, actor, actor_role)
        except Exception as exc:
            logger.exception("Run %s failed at %s", run_id, name)
            message = str(exc).splitlines()[0][:1000] if str(exc) else type(exc).__name__
            _update(
                run_id,
                status="failed",
                error=f"{name}: {message}",
                finished_at=utcnow(),
                stage_update=(
                    name,
                    {"status": "failed", "error": message, "finished_at": utcnow().isoformat()},
                ),
            )
            for later in names[names.index(name) + 1 :]:
                _update(run_id, stage_update=(later, {"status": "skipped"}))
            return
        _update(
            run_id,
            stage_update=(
                name,
                {
                    "status": "done",
                    "summary": _summary(result),
                    "finished_at": utcnow().isoformat(),
                },
            ),
        )
    _update(run_id, status="succeeded", current_stage=None, finished_at=utcnow())


def mark_interrupted_runs() -> int:
    """Runs that were active when the server stopped can't resume their thread: close them."""
    with session_scope() as db:
        runs = db.query(PipelineRun).filter(PipelineRun.status.in_(_ACTIVE)).all()
        for run in runs:
            run.status = "interrupted"
            run.error = "The server stopped while this run was in progress; start it again."
            run.finished_at = utcnow()
            run.stages = [
                {**s, "status": "interrupted"} if s.get("status") in ("waiting", "running") else s
                for s in run.stages
            ]
        return len(runs)


def run_view(run: PipelineRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "status": run.status,
        "current_stage": run.current_stage,
        "stages": run.stages,
        "error": run.error,
        "started_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
