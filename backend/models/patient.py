from __future__ import annotations
from datetime import date, datetime
from typing import List, Optional
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    patient_name: Mapped[str] = mapped_column(String(128))
    date_of_birth: Mapped[Optional[date]]
    sex: Mapped[Optional[str]] = mapped_column(String(1))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    plans: Mapped[List["Plan"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
