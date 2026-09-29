from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING
from sqlalchemy import String, ForeignKey, Float, Boolean, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base

if TYPE_CHECKING:
    from .plan import Plan


class MLPrediction(Base):
    __tablename__ = "ml_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    model_version: Mapped[str] = mapped_column(String(64))
    pass_probability: Mapped[float] = mapped_column(Float)
    # high | moderate | low
    confidence: Mapped[str] = mapped_column(String(16))
    # virtual_approve | flag | measure
    verdict: Mapped[str] = mapped_column(String(32))
    feature_vector: Mapped[str] = mapped_column(Text)       # JSON dict
    evidence_available: Mapped[str] = mapped_column(Text)   # JSON list
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


    # Ground-truth outcome (recorded after physical measurement, if performed)
    physical_outcome: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    physical_passing_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_recorded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    plan: Mapped["Plan"] = relationship(back_populates="ml_predictions")
