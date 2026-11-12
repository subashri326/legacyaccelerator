"""Stage 6: Track Engine — status dashboards, blocker reporting, steering exports, and client sign-off."""

from __future__ import annotations

import io
import json
from collections import Counter
from typing import Any

from openpyxl import Workbook
from sqlalchemy.orm import Session

from accelerator.core.exceptions import EvidenceRequiredError
from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.models import (
    ObjectStatus,
    ReconResult,
    Signoff,
    StatusEvent,
    Wave,
    WaveMember,
)
from accelerator.db.repository import record_audit
from accelerator.engines.state_machine import ObjectState, StateMachine


class TrackEngine:
    """Provides governed tracking dashboards, steering exports, and evidence-backed sign-off."""

    def __init__(self, store: ArtifactStore | None = None) -> None:
        self.store = store or get_artifact_store()

    def get_dashboard(self, db: Session, project_id: str) -> dict[str, Any]:
        statuses = db.query(ObjectStatus).filter(ObjectStatus.project_id == project_id).all()
        waves = (
            db.query(Wave).filter(Wave.project_id == project_id).order_by(Wave.wave_number).all()
        )

        state_counts = Counter(s.state for s in statuses)
        kind_counts = Counter(s.object_kind for s in statuses)
        owner_counts = Counter(s.owner for s in statuses)
        blockers = [
            {
                "object_key": s.object_key,
                "object_kind": s.object_kind,
                "state": s.state,
                "owner": s.owner,
                "blocker_reason": s.blocker_reason,
            }
            for s in statuses
            if s.blocker_reason
        ]

        status_by_key = {s.object_key: s for s in statuses}
        wave_progress = []
        for w in waves:
            members = db.query(WaveMember).filter(WaveMember.wave_id == w.id).all()
            w_states = Counter(
                status_by_key[m.object_key].state for m in members if m.object_key in status_by_key
            )
            wave_progress.append(
                {
                    "wave_id": w.id,
                    "wave_number": w.wave_number,
                    "name": w.name,
                    "total_objects": len(members),
                    "states": dict(w_states),
                }
            )

        return {
            "project_id": project_id,
            "total_objects": len(statuses),
            "by_state": {state.value: state_counts.get(state.value, 0) for state in ObjectState},
            "by_kind": dict(kind_counts),
            "by_owner": dict(owner_counts),
            "blockers_count": len(blockers),
            "blockers": blockers,
            "waves": wave_progress,
        }

    def sign_off(
        self,
        db: Session,
        project_id: str,
        object_key: str,
        approver: str,
        approver_role: str,
        notes: str = "Reconciliation verified and signed off",
        recon_run_id: str | None = None,
    ) -> Signoff:
        with metrics.stage_span("signoff", project_id):
            obj_status = (
                db.query(ObjectStatus)
                .filter(
                    ObjectStatus.project_id == project_id, ObjectStatus.object_key == object_key
                )
                .first()
            )
            if obj_status is None:
                raise ValueError(f"Object '{object_key}' not found in project")

            if obj_status.state not in (ObjectState.VALIDATED.value, ObjectState.SIGNED_OFF.value):
                raise EvidenceRequiredError(
                    f"Cannot sign off '{object_key}' in state '{obj_status.state}'; must be Validated first."
                )

            # Verify passing reconciliation evidence exists
            q = (
                db.query(ReconResult)
                .filter(
                    ReconResult.project_id == project_id,
                    ReconResult.object_key == object_key,
                    ReconResult.verdict.in_(["pass", "pass_within_tolerance"]),
                )
                .order_by(ReconResult.created_at.desc())
            )
            if recon_run_id:
                q = q.filter(ReconResult.recon_run_id == recon_run_id)
            recon_res = q.first()
            if recon_res is None:
                raise EvidenceRequiredError(
                    f"No passing reconciliation evidence found for '{object_key}'."
                )

            history = (
                db.query(StatusEvent)
                .filter(StatusEvent.project_id == project_id, StatusEvent.object_key == object_key)
                .order_by(StatusEvent.created_at)
                .all()
            )
            bundle = {
                "project_id": project_id,
                "object_key": object_key,
                "approver": approver,
                "notes": notes,
                "recon_run_id": recon_res.recon_run_id,
                "recon_level": recon_res.level,
                "recon_verdict": recon_res.verdict,
                "recon_evidence_uri": recon_res.evidence_uri,
                "lifecycle_history": [
                    {
                        "from": h.from_state,
                        "to": h.to_state,
                        "actor": h.actor,
                        "evidence": f"{h.evidence_type}:{h.evidence_id}",
                        "timestamp": h.created_at.isoformat(),
                    }
                    for h in history
                ],
            }
            bundle_uri = self.store.put_text(
                f"projects/{project_id}/evidence/signoffs/{object_key.replace('.', '_')}.json",
                json.dumps(bundle, indent=2),
            )

            signoff_row = Signoff(
                project_id=project_id,
                object_key=object_key,
                recon_run_id=recon_res.recon_run_id,
                approver=approver,
                decision="signed_off",
                notes=notes,
                evidence_bundle_uri=bundle_uri,
                created_by=approver,
            )
            db.add(signoff_row)
            db.flush()

            StateMachine.advance(
                db,
                project_id,
                object_key,
                obj_status.object_kind,
                ObjectState.SIGNED_OFF,
                approver,
                "signoff",
                signoff_row.id,
            )
            record_audit(
                db,
                actor=approver,
                actor_role=approver_role,
                action="object.signed_off",
                resource_type="signoff",
                resource_id=signoff_row.id,
                project_id=project_id,
                details={"object_key": object_key, "recon_run_id": recon_res.recon_run_id},
            )
            return signoff_row

    def export_steering_summary(self, db: Session, project_id: str) -> dict[str, str]:
        """Generate steering-meeting Markdown + Excel exports and persist URIs."""
        dash = self.get_dashboard(db, project_id)
        md_lines = [
            f"# Steering Meeting Summary — Project {project_id}",
            "",
            f"**Total Objects:** {dash['total_objects']} | **Active Blockers:** {dash['blockers_count']}",
            "",
            "## Lifecycle Funnel",
            "| State | Count |",
            "| --- | --- |",
        ]
        for state, cnt in dash["by_state"].items():
            md_lines.append(f"| {state} | {cnt} |")

        md_lines.extend(
            ["", "## Wave Progress", "| Wave | Total | States |", "| --- | --- | --- |"]
        )
        for w in dash["waves"]:
            md_lines.append(f"| {w['name']} | {w['total_objects']} | {w['states']} |")

        if dash["blockers"]:
            md_lines.extend(
                ["", "## Active Blockers", "| Object | State | Reason |", "| --- | --- | --- |"]
            )
            for b in dash["blockers"]:
                md_lines.append(f"| {b['object_key']} | {b['state']} | {b['blocker_reason']} |")

        md_uri = self.store.put_text(
            f"projects/{project_id}/reports/steering_summary.md",
            "\n".join(md_lines) + "\n",
        )

        wb = Workbook()
        ws = wb.active
        ws.title = "Lifecycle Funnel"
        ws.append(["State", "Count"])
        for state, cnt in dash["by_state"].items():
            ws.append([state, cnt])

        ws_b = wb.create_sheet("Blockers")
        ws_b.append(["Object Key", "Kind", "State", "Owner", "Blocker Reason"])
        for b in dash["blockers"]:
            ws_b.append(
                [b["object_key"], b["object_kind"], b["state"], b["owner"], b["blocker_reason"]]
            )

        buf = io.BytesIO()
        wb.save(buf)
        xlsx_uri = self.store.put_bytes(
            f"projects/{project_id}/reports/steering_summary.xlsx",
            buf.getvalue(),
        )
        return {"markdown_uri": md_uri, "excel_uri": xlsx_uri}
