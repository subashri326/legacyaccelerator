"""Celery worker adapter for distributed stage execution (Section 7.1 & 9.3)."""

from __future__ import annotations

import os
from typing import Any

from accelerator.db.session import session_scope
from accelerator.orchestration.runner import RunOrchestrator

CELERY_BROKER_URL = os.getenv("L2C_CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_BACKEND_URL = os.getenv("L2C_CELERY_RESULT_BACKEND", "redis://localhost:6379/1")


def create_celery_app() -> Any:
    from celery import Celery

    app = Celery("l2c_accelerator", broker=CELERY_BROKER_URL, backend=CELERY_BACKEND_URL)

    @app.task(name="l2c.run_stage", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
    def run_stage_task(_self: Any, stage: str, project_id: str, actor: str = "celery") -> dict[str, Any]:
        orch = RunOrchestrator()
        with session_scope() as db:
            if stage == "discover":
                return orch.discover_engine.run(db, project_id, actor=actor).__dict__
            if stage == "assess":
                return orch.assess_engine.run(db, project_id, actor=actor).__dict__
            if stage == "convert":
                return orch.convert_engine.run(db, project_id, actor=actor).__dict__
            if stage == "move":
                return orch.move_engine.run(db, project_id, actor=actor).__dict__
            if stage == "reconcile":
                return orch.reconcile_engine.run(db, project_id, actor=actor).__dict__
            raise ValueError(f"Unknown stage '{stage}'")

    return app
