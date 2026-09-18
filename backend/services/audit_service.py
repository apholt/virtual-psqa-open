"""
Audit service for HIPAA compliance (§ 164.312(b)).
Records access to patient ePHI, authentication attempts, and data operations.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Optional
from sqlalchemy.orm import Session

from database import SessionLocal
from models.audit_log import AuditLog

logger = logging.getLogger(__name__)


def log_audit_event(
    username: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[Any] = None,
    ip_address: Optional[str] = None,
    db: Optional[Session] = None,
) -> Optional[AuditLog]:
    """
    Record an immutable audit log entry.
    Can be called with an existing db session or creates its own.
    """
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        details_str = (
            json.dumps(details)
            if isinstance(details, (dict, list))
            else (str(details) if details else None)
        )
        entry = AuditLog(
            timestamp=datetime.utcnow(),
            username=username or "anonymous",
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details_str,
            ip_address=ip_address,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        logger.info(
            f"[HIPAA-AUDIT] user={username} action={action} "
            f"target={target_type}:{target_id} ip={ip_address}"
        )
        return entry
    except Exception as exc:
        logger.error(f"Failed to write HIPAA audit log: {exc}")
        if db:
            db.rollback()
        return None
    finally:
        if own_db and db:
            db.close()


def get_audit_logs(
    limit: int = 100,
    skip: int = 0,
    username: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    db: Optional[Session] = None,
) -> list[AuditLog]:
    """Retrieve audit log entries for review and inspection."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        q = db.query(AuditLog)
        if username:
            q = q.filter(AuditLog.username == username)
        if action:
            q = q.filter(AuditLog.action == action)
        if target_type:
            q = q.filter(AuditLog.target_type == target_type)
        return q.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()
    finally:
        if own_db and db:
            db.close()
