from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, ForeignKey, Float, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base
class GammaResult(Base):
    __tablename__ = "gamma_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    fraction_number: Mapped[Optional[int]]  # None = pre-treatment plan QA
    field_name: Mapped[str] = mapped_column(String(32))
    # DICOM BeamNumber for per-beam rows; None for composite rows. This is the
    # authoritative pairing key for the frontend -- never rely on row order.
    beam_number: Mapped[Optional[int]]
    # mcSquare_vs_TPS | log_vs_TPS | mcSquare_vs_log
    comparison_type: Mapped[str] = mapped_column(String(32))
    dd_percent: Mapped[float] = mapped_column(Float)
    dta_mm: Mapped[float] = mapped_column(Float)
    passing_rate: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    gamma_map_path: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    plan: Mapped["Plan"] = relationship(back_populates="gamma_results")
