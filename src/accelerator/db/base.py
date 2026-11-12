"""SQLAlchemy declarative base and UDIF-style audit mixin."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Base SQLAlchemy class for all metadata database tables."""


class AuditMixin:
    """Standard audit columns reused across metadata entities (Section 5.2)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
