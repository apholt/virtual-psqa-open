"""
AuditLog model for HIPAA-compliant access control and tracking (§ 164.312(b)).
Records user actions on patient ePHI, authentication, and clinical plan changes.
"""
from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, String, Text
from database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    username = Column(String(64), index=True, nullable=False)
    action = Column(String(64), index=True, nullable=False)  # LOGIN, LOGIN_FAILED, LOGOUT, VIEW_PLAN, VIEW_PATIENT, UPLOAD_DICOM, RUN_QA, EXPORT_REPORT, DELETE_PLAN, USER_CREATED
    target_type = Column(String(32), nullable=True)          # patient, plan, fraction, system, user
    target_id = Column(String(64), nullable=True)            # ID or patient MRN
    details = Column(Text, nullable=True)                    # JSON or description string
    ip_address = Column(String(64), nullable=True)
