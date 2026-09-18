from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import String, ForeignKey, Text, Float, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class QAJob(Base):
    __tablename__ = "qa_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    # mcSquare | log_reconstruction | gamma_mcSquare | gamma_log
    job_type: Mapped[str] = mapped_column(String(32))
    # queued | running | complete | error | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # Delivered fraction this job is for (log_reconstruction jobs only;
    # None for plan-level jobs like mcSquare/gamma). Lets the log job carry
    # its own fraction number instead of defaulting to 1.
    fraction_number: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]]
    completed_at: Mapped[Optional[datetime]]
    result_path: Mapped[Optional[str]] = mapped_column(Text)

    plan: Mapped["Plan"] = relationship(back_populates="qa_jobs")
