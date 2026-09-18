from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base

if TYPE_CHECKING:
    from models.plan import Plan


class ChartCheck(Base):
    __tablename__ = "chart_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    check_number: Mapped[int] = mapped_column(Integer)  # 1, 2, 3... running tally
    fractions_covered: Mapped[str] = mapped_column(String(64))  # e.g. "1-3", "4-8"
    start_fraction: Mapped[int] = mapped_column(Integer)
    end_fraction: Mapped[int] = mapped_column(Integer)
    fraction_count: Mapped[int] = mapped_column(Integer)
    reviewer_name: Mapped[str] = mapped_column(String(128), default="Medical Physicist")
    status: Mapped[str] = mapped_column(String(32), default="complete")  # "complete", "flagged"
    table_status: Mapped[str] = mapped_column(String(32), default="pass")
    log_status: Mapped[str] = mapped_column(String(32), default="pass")
    oir_status: Mapped[str] = mapped_column(String(32), default="pass")
    documents_status: Mapped[str] = mapped_column(String(32), default="pass")
    checklist_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    plan: Mapped["Plan"] = relationship(back_populates="chart_checks")
