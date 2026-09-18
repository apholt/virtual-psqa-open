from __future__ import annotations
from datetime import date
from typing import Optional
from sqlalchemy import String, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Fraction(Base):
    __tablename__ = "fractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    fraction_number: Mapped[int]
    delivery_date: Mapped[Optional[date]]
    rtrecord_uid: Mapped[Optional[str]] = mapped_column(String(64))
    rtrecord_path: Mapped[Optional[str]] = mapped_column(Text)
    # pending | running | pass | flagged | measure_needed | failed
    qa_status: Mapped[str] = mapped_column(String(32), default="pending")
    machine: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # verification (pre-treatment dry run without patient) | curative (clinical fraction with patient)
    delivery_type: Mapped[str] = mapped_column(String(32), default="curative")
    # Flag for interrupted / partial delivery session
    is_interrupted: Mapped[bool] = mapped_column(default=False)
    interruption_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    plan: Mapped["Plan"] = relationship(back_populates="fractions")
