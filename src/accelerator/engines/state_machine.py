"""Strict 8-state object lifecycle state machine backed by stored evidence (Section 4.6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from accelerator.core.exceptions import EvidenceRequiredError, StateTransitionError
from accelerator.db.models import ObjectStatus, StatusEvent


class ObjectState(StrEnum):
    DISCOVERED = "Discovered"
    ASSESSED = "Assessed"
    CONVERTED = "Converted"
    REVIEWED = "Reviewed"
    DEPLOYED = "Deployed"
    LOADED = "Loaded"
    VALIDATED = "Validated"
    SIGNED_OFF = "SignedOff"


STATE_ORDER: dict[ObjectState, int] = {
    ObjectState.DISCOVERED: 0,
    ObjectState.ASSESSED: 1,
    ObjectState.CONVERTED: 2,
    ObjectState.REVIEWED: 3,
    ObjectState.DEPLOYED: 4,
    ObjectState.LOADED: 5,
    ObjectState.VALIDATED: 6,
    ObjectState.SIGNED_OFF: 7,
}

# Allowed forward transitions (plus idempotent same-state re-runs and rollback to earlier stages)
ALLOWED_NEXT: dict[ObjectState, set[ObjectState]] = {
    ObjectState.DISCOVERED: {ObjectState.ASSESSED},
    ObjectState.ASSESSED: {ObjectState.CONVERTED},
    ObjectState.CONVERTED: {ObjectState.REVIEWED},
    ObjectState.REVIEWED: {ObjectState.DEPLOYED},
    ObjectState.DEPLOYED: {ObjectState.LOADED, ObjectState.VALIDATED},
    ObjectState.LOADED: {ObjectState.VALIDATED},
    ObjectState.VALIDATED: {ObjectState.SIGNED_OFF},
    ObjectState.SIGNED_OFF: set(),
}


class StateMachine:
    """Enforces object lifecycle progression and persists immutable StatusEvent history."""

    @staticmethod
    def ensure_object(
        db: Session,
        project_id: str,
        object_key: str,
        object_kind: str,
        actor: str = "system",
    ) -> ObjectStatus:
        row = (
            db.query(ObjectStatus)
            .filter(ObjectStatus.project_id == project_id, ObjectStatus.object_key == object_key)
            .first()
        )
        if row is None:
            row = ObjectStatus(
                project_id=project_id,
                object_key=object_key,
                object_kind=object_kind,
                state=ObjectState.DISCOVERED.value,
                created_by=actor,
            )
            db.add(row)
            db.flush()
            db.add(
                StatusEvent(
                    project_id=project_id,
                    object_key=object_key,
                    from_state=None,
                    to_state=ObjectState.DISCOVERED.value,
                    actor=actor,
                    evidence_type="discovery",
                    evidence_id=object_key,
                    created_by=actor,
                )
            )
            db.flush()
        return row

    @staticmethod
    def advance(
        db: Session,
        project_id: str,
        object_key: str,
        object_kind: str,
        to_state: ObjectState,
        actor: str,
        evidence_type: str,
        evidence_id: str,
        blocker_reason: str | None = None,
        details: dict[str, Any] | None = None,
        allow_re_run: bool = True,
    ) -> ObjectStatus:
        if not evidence_id or not evidence_type:
            raise EvidenceRequiredError(
                f"Cannot advance '{object_key}' to '{to_state.value}' without stored evidence."
            )

        row = StateMachine.ensure_object(db, project_id, object_key, object_kind, actor=actor)
        current = ObjectState(row.state)

        # Allow re-running an earlier/same stage idempotently without regressing signed-off objects
        if current == to_state and allow_re_run:
            row.blocker_reason = blocker_reason
            row.last_evidence_ref = f"{evidence_type}:{evidence_id}"
            db.flush()
            return row

        if STATE_ORDER[to_state] < STATE_ORDER[current] and allow_re_run:
            # Idempotent re-execution of an earlier stage preserves higher progress unless blocked
            if blocker_reason:
                row.blocker_reason = blocker_reason
            db.flush()
            return row

        # Allow skipping directly from Assessed/Converted/Reviewed to Loaded for pure table bulk moves
        allowed = ALLOWED_NEXT.get(current, set())
        table_bulk_shortcut = (
            object_kind == "table"
            and to_state == ObjectState.LOADED
            and current in (ObjectState.ASSESSED, ObjectState.CONVERTED, ObjectState.REVIEWED, ObjectState.DEPLOYED)
        )
        if to_state not in allowed and not table_bulk_shortcut:
            raise StateTransitionError(
                f"Illegal transition for '{object_key}': {current.value} -> {to_state.value}"
            )

        row.state = to_state.value
        row.blocker_reason = blocker_reason
        row.last_evidence_ref = f"{evidence_type}:{evidence_id}"
        db.add(
            StatusEvent(
                project_id=project_id,
                object_key=object_key,
                from_state=current.value,
                to_state=to_state.value,
                actor=actor,
                evidence_type=evidence_type,
                evidence_id=evidence_id,
                details=details or {},
                created_by=actor,
            )
        )
        db.flush()
        return row

    @staticmethod
    def set_blocker(
        db: Session,
        project_id: str,
        object_key: str,
        reason: str,
    ) -> None:
        row = (
            db.query(ObjectStatus)
            .filter(ObjectStatus.project_id == project_id, ObjectStatus.object_key == object_key)
            .first()
        )
        if row is not None:
            row.blocker_reason = reason
            db.flush()
