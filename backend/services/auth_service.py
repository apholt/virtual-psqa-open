"""
Authentication service for Virtual PSQA (HIPAA Compliant).

Provides:
- PBKDF2-HMAC password hashing and constant-time verification.
- HMAC-SHA256 signed session cookie creation, validation, and sliding renewal.
- Unique user database support (§ 164.312(a)(2)(i)).
- 15-minute inactivity session expiration (§ 164.312(a)(2)(iii)).
- HTTP Basic Auth header parsing and verification.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Optional
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from models.user import User

logger = logging.getLogger(__name__)


def hash_password(password: str, iterations: int = 100_000) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(plain_password: str, stored_credential: str) -> bool:
    """Verify a plain password against a stored plaintext or PBKDF2 hash."""
    if not plain_password or not stored_credential:
        return False

    if stored_credential.startswith("pbkdf2_sha256$"):
        parts = stored_credential.split("$")
        if len(parts) != 4:
            return False
        try:
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected_hex = parts[3]
            computed = hashlib.pbkdf2_hmac(
                "sha256", plain_password.encode("utf-8"), salt, iterations
            )
            return secrets.compare_digest(computed.hex(), expected_hex)
        except Exception as exc:
            logger.error(f"Error checking PBKDF2 password hash: {exc}")
            return False
    else:
        return secrets.compare_digest(
            plain_password.encode("utf-8"), stored_credential.encode("utf-8")
        )


def ensure_initial_admin(db: Optional[Session] = None) -> None:
    """Ensure at least one admin account exists in the database."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        user_count = db.query(User).count()
        if user_count == 0:
            username = settings.AUTH_USERNAME.strip() if settings.AUTH_USERNAME else "admin"
            password = settings.AUTH_PASSWORD.strip() if settings.AUTH_PASSWORD else "psqa-admin-2026!"
            admin_user = User(
                username=username,
                hashed_password=hash_password(password),
                full_name="System Administrator",
                role="admin",
                is_active=True,
            )
            db.add(admin_user)
            db.commit()
            logger.info(f"Initialized default HIPAA admin user '{username}' in database.")
    except Exception as exc:
        logger.error(f"Failed to ensure initial admin user: {exc}")
        if db:
            db.rollback()
    finally:
        if own_db and db:
            db.close()


def authenticate_user(
    username: str, password: str, db: Optional[Session] = None
) -> Optional[dict[str, Any]]:
    """
    Validate username and password against database User table,
    falling back to config if needed.
    """
    if not username or not password:
        return None

    clean_user = username.strip()
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True

    try:
        db_user = db.query(User).filter(User.username == clean_user).first()
        if db_user:
            if not db_user.is_active:
                logger.warning(f"Authentication rejected for deactivated user: {clean_user}")
                return None
            if verify_password(password, db_user.hashed_password):
                return {
                    "username": db_user.username,
                    "role": db_user.role,
                    "full_name": db_user.full_name,
                }
            return None
    except Exception as exc:
        logger.error(f"Database query failed during auth: {exc}")
    finally:
        if own_db and db:
            db.close()

    # Fallback to .env users if not found in database
    config_users = _get_config_users()
    stored = config_users.get(clean_user)
    if stored and verify_password(password, stored):
        return {"username": clean_user, "role": "admin", "full_name": "Admin User"}

    return None


def _get_config_users() -> dict[str, str]:
    """Compile dict of username -> credential from config/env."""
    users: dict[str, str] = {}
    if settings.AUTH_USERNAME and settings.AUTH_PASSWORD:
        users[settings.AUTH_USERNAME.strip()] = settings.AUTH_PASSWORD

    if settings.AUTH_USERS:
        raw = settings.AUTH_USERS.strip()
        if raw.startswith("{"):
            try:
                users.update(json.loads(raw))
            except Exception as exc:
                logger.error(f"Failed to parse AUTH_USERS JSON: {exc}")
        else:
            for item in raw.split(","):
                if ":" in item:
                    u, p = item.split(":", 1)
                    users[u.strip()] = p.strip()
    return users


def create_user(
    db: Session,
    username: str,
    password: str,
    full_name: Optional[str] = None,
    role: str = "physicist",
) -> User:
    """Create a new unique user for HIPAA compliance."""
    clean_user = username.strip().lower()
    existing = db.query(User).filter(User.username == clean_user).first()
    if existing:
        raise ValueError(f"User '{clean_user}' already exists.")

    new_user = User(
        username=clean_user,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


def list_users(db: Session) -> list[User]:
    """List all registered users."""
    return db.query(User).order_by(User.id.asc()).all()


def create_session_token(username: str) -> str:
    """Create a tamper-proof HMAC-SHA256 signed session token."""
    now = int(time.time())
    payload = f"{username}:{now}"
    secret_key = settings.AUTH_SECRET_KEY.encode("utf-8")
    sig = hmac.new(secret_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_session_token(token: str) -> Optional[tuple[str, int]]:
    """
    Verify session token signature and 15-minute inactivity expiration.
    Returns (username, issued_timestamp) if valid, None if expired or invalid.
    """
    if not token:
        return None

    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 3:
            return None

        username, ts_str, signature = parts
        timestamp = int(ts_str)

        # HIPAA § 164.312(a)(2)(iii): Inactivity timeout
        max_age_seconds = settings.AUTH_SESSION_EXPIRE_MINUTES * 60
        if time.time() - timestamp > max_age_seconds:
            logger.info(f"Session expired due to inactivity for user: {username}")
            return None

        # Constant-time signature verification
        secret_key = settings.AUTH_SECRET_KEY.encode("utf-8")
        payload = f"{username}:{ts_str}"
        expected_sig = hmac.new(
            secret_key, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if secrets.compare_digest(signature, expected_sig):
            return (username, timestamp)
        return None
    except Exception as exc:
        logger.debug(f"Invalid session token format: {exc}")
        return None


def verify_basic_auth(auth_header: str, db: Optional[Session] = None) -> Optional[str]:
    """Parse HTTP Basic authorization header and authenticate credentials."""
    if not auth_header or not auth_header.startswith("Basic "):
        return None

    try:
        b64_val = auth_header[6:].strip()
        decoded = base64.b64decode(b64_val).decode("utf-8")
        if ":" not in decoded:
            return None
        user, pwd = decoded.split(":", 1)
        res = authenticate_user(user, pwd, db=db)
        if res:
            return res["username"]
        return None
    except Exception as exc:
        logger.debug(f"Failed to parse Basic Auth header: {exc}")
        return None
