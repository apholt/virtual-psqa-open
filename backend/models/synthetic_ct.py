from __future__ import annotations

from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class SyntheticCT(Base):
    __tablename__ = "synthetic_cts"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)
    fraction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("fractions.id"), index=True, nullable=True)
    fraction_number: Mapped[int] = mapped_column(index=True)
    series_instance_uid: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    study_instance_uid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    series_description: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    scan_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dicom_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    num_slices: Mapped[int] = mapped_column(Integer, default=0)
    pixel_spacing: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    slice_thickness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dimensions: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Raw CBCT metadata
    cbct_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cbct_series_instance_uid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cbct_num_slices: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Deformable Image Registration (DIR) metrics
    dir_method: Mapped[Optional[str]] = mapped_column(String(32), default="demons", nullable=True)
    dir_mean_displacement_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dir_p99_displacement_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_hu_before: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_hu_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status & QA metrics
    status: Mapped[str] = mapped_column(String(32), default="pending")  # cbct_uploaded | generating | pending | running | complete | error
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dose_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gamma_passing_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 3%/3mm
    gamma_2mm_passing_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 2%/2mm
    gamma_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    mean_dose_diff_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_dose_diff_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_shift_lat_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_shift_long_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    setup_shift_vert_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dvh_metrics: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    calculated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    plan: Mapped["Plan"] = relationship(back_populates="synthetic_cts")
    fraction: Mapped[Optional["Fraction"]] = relationship()
