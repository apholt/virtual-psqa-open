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


def _apply_audit_filters(
    q,
    username: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    search: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    from sqlalchemy import or_

    if username:
        q = q.filter(AuditLog.username == username)
    if action:
        q = q.filter(AuditLog.action == action)
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    if start_date:
        q = q.filter(AuditLog.timestamp >= start_date)
    if end_date:
        q = q.filter(AuditLog.timestamp <= end_date)
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                AuditLog.username.ilike(term),
                AuditLog.action.ilike(term),
                AuditLog.target_type.ilike(term),
                AuditLog.target_id.ilike(term),
                AuditLog.details.ilike(term),
                AuditLog.ip_address.ilike(term),
            )
        )
    return q


def get_audit_logs(
    limit: int = 100,
    skip: int = 0,
    username: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    search: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Optional[Session] = None,
) -> list[AuditLog]:
    """Retrieve audit log entries for review and inspection with optional search and date filters."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        q = db.query(AuditLog)
        q = _apply_audit_filters(
            q,
            username=username,
            action=action,
            target_type=target_type,
            search=search,
            start_date=start_date,
            end_date=end_date,
        )
        return q.order_by(AuditLog.timestamp.desc()).offset(skip).limit(limit).all()
    finally:
        if own_db and db:
            db.close()


def count_audit_logs(
    username: Optional[str] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    search: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db: Optional[Session] = None,
) -> int:
    """Count audit log entries matching filters for pagination."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        q = db.query(AuditLog)
        q = _apply_audit_filters(
            q,
            username=username,
            action=action,
            target_type=target_type,
            search=search,
            start_date=start_date,
            end_date=end_date,
        )
        return q.count()
    finally:
        if own_db and db:
            db.close()


def get_audit_stats(db: Optional[Session] = None) -> dict[str, Any]:
    """Retrieve summary metrics for audit logs."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        total_logs = db.query(AuditLog).count()
        logins_today = (
            db.query(AuditLog)
            .filter(
                AuditLog.timestamp >= today_start,
                AuditLog.action.in_(["LOGIN_SUCCESS", "API_LOGIN_SUCCESS"]),
            )
            .count()
        )
        failures_today = (
            db.query(AuditLog)
            .filter(
                AuditLog.timestamp >= today_start,
                AuditLog.action.in_(["LOGIN_FAILED", "API_LOGIN_FAILED"]),
            )
            .count()
        )
        unique_users = db.query(AuditLog.username).distinct().count()

        # Action breakdown for recent 500 logs
        recent_rows = (
            db.query(AuditLog.action).order_by(AuditLog.id.desc()).limit(500).all()
        )
        breakdown: dict[str, int] = {}
        for (act,) in recent_rows:
            breakdown[act] = breakdown.get(act, 0) + 1

        return {
            "total_logs": total_logs,
            "logins_today": logins_today,
            "failures_today": failures_today,
            "unique_users": unique_users,
            "recent_actions": breakdown,
        }
    finally:
        if own_db and db:
            db.close()
