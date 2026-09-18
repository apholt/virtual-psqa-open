
from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from sqlalchemy import String, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    plan_label: Mapped[str] = mapped_column(String(128))
    plan_name: Mapped[str] = mapped_column(String(128))
    treatment_site: Mapped[Optional[str]] = mapped_column(String(64))
    number_of_fractions: Mapped[Optional[int]]
    number_of_fields: Mapped[int]
    dicom_store_path: Mapped[str] = mapped_column(Text)
    rtplan_uid: Mapped[str] = mapped_column(String(64), unique=True)
    rtdose_uid: Mapped[Optional[str]] = mapped_column(String(64))
    rtstruct_uid: Mapped[Optional[str]] = mapped_column(String(64))
    # pending | running | pass | flagged | measure_needed | failed
    qa_status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    patient: Mapped["Patient"] = relationship(back_populates="plans")
    qa_jobs: Mapped[List["QAJob"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    fractions: Mapped[List["Fraction"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    gamma_results: Mapped[List["GammaResult"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    synthetic_cts: Mapped[List["SyntheticCT"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    chart_checks: Mapped[List["ChartCheck"]] = relationship(back_populates="plan", cascade="all, delete-orphan")

    @property
    def patient_identifier(self) -> Optional[str]:
        return self.patient.patient_id if self.patient else None

    @property
    def patient_name(self) -> Optional[str]:
        return self.patient.patient_name if self.patient else None

