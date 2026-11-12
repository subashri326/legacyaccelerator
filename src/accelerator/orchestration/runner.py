"""Built-in restartable, idempotent RunOrchestrator (Section 3 & 7.1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from accelerator.engines.assess import AssessEngine, AssessSummary
from accelerator.engines.convert import ConvertEngine, ConvertSummary
from accelerator.engines.discover import DiscoverEngine, DiscoverSummary
from accelerator.engines.move import MoveEngine, MoveSummary
from accelerator.engines.reconcile import ReconcileEngine, ReconcileSummary


@dataclass
class PipelineRunSummary:
    discover: DiscoverSummary
    assess: AssessSummary
    convert: ConvertSummary
    move: MoveSummary
    reconcile: ReconcileSummary


class RunOrchestrator:
    """Executes individual stages or a governed end-to-end migration pipeline."""

    def __init__(self) -> None:
        self.discover_engine = DiscoverEngine()
        self.assess_engine = AssessEngine()
        self.convert_engine = ConvertEngine()
        self.move_engine = MoveEngine()
        self.reconcile_engine = ReconcileEngine()

    def run_pipeline(
        self,
        db: Session,
        project_id: str,
        *,
        actor: str = "system",
        actor_role: str = "migration_lead",
        deliver_git: bool = False,
        git_repo_path: str | Path | None = None,
    ) -> PipelineRunSummary:
        d = self.discover_engine.run(db, project_id, actor=actor, actor_role=actor_role)
        a = self.assess_engine.run(db, project_id, actor=actor, actor_role=actor_role)
        c = self.convert_engine.run(
            db,
            project_id,
            deliver_git=deliver_git,
            git_repo_path=git_repo_path,
            actor=actor,
            actor_role=actor_role,
        )
        m = self.move_engine.run(db, project_id, actor=actor, actor_role=actor_role)
        r = self.reconcile_engine.run(db, project_id, actor=actor, actor_role=actor_role)
        return PipelineRunSummary(discover=d, assess=a, convert=c, move=m, reconcile=r)
