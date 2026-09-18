"""
Authentication & Audit router for Virtual PSQA (HIPAA Compliant).

Provides:
- GET  /login: Modern login page
- POST /login: Form login handler setting session cookie with audit tracking
- GET  /logout: Session logout handler with audit tracking
- POST /api/auth/login: JSON API login
- POST /api/auth/logout: JSON API logout
- GET  /api/auth/me: Current authentication status and user identity
- GET  /api/auth/users: List registered system users (§ 164.312(a)(2)(i))
- POST /api/auth/users: Create unique user account
- GET  /api/auth/audit-logs: Audit trail log query (§ 164.312(b))
"""
from __future__ import annotations

import html
import logging
from typing import Any, Optional
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from services.audit_service import get_audit_logs, log_audit_event
from services.auth_service import (
    authenticate_user,
    create_session_token,
    create_user,
    list_users,
    verify_session_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["authentication"])


def render_login_html(error: Optional[str] = None, next_url: str = "/") -> str:
    """Render a modern, responsive login page styled for Virtual PSQA."""
    safe_next = html.escape(next_url or "/")
    error_html = ""
    if error:
        safe_err = html.escape(error)
        error_html = f"""
        <div class="alert-error" role="alert">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <span>{safe_err}</span>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Virtual PSQA — Sign In</title>
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }}
        body {{
            background-color: #0b0f19;
            background-image: 
                radial-gradient(at 0% 0%, rgba(14, 165, 233, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(59, 130, 246, 0.08) 0px, transparent 50%);
            color: #f1f5f9;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .login-card {{
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 12px;
            width: 100%;
            max-width: 420px;
            padding: 36px 32px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.6);
        }}
        .brand-header {{
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-bottom: 28px;
            text-align: center;
        }}
        .brand-icon {{
            width: 52px;
            height: 52px;
            background: rgba(14, 165, 233, 0.12);
            border: 1px solid rgba(14, 165, 233, 0.3);
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 14px;
            color: #38bdf8;
        }}
        .brand-title {{
            font-size: 20px;
            font-weight: 700;
            letter-spacing: -0.02em;
            color: #f8fafc;
            margin-bottom: 4px;
        }}
        .brand-subtitle {{
            font-size: 13px;
            color: #94a3b8;
        }}
        .security-badge {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            font-size: 11px;
            font-weight: 500;
            background: rgba(16, 185, 129, 0.1);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.2);
            padding: 2px 8px;
            border-radius: 9999px;
            margin-top: 8px;
        }}
        .security-badge svg {{
            width: 12px;
            height: 12px;
        }}
        .alert-error {{
            background: rgba(239, 68, 68, 0.12);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
            padding: 12px 14px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .form-group {{
            margin-bottom: 18px;
        }}
        .form-label {{
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: #cbd5e1;
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .form-input {{
            width: 100%;
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 7px;
            padding: 11px 14px;
            color: #f8fafc;
            font-size: 14px;
            transition: border-color 0.15s ease, box-shadow 0.15s ease;
            outline: none;
        }}
        .form-input:focus {{
            border-color: #0284c7;
            box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.25);
        }}
        .submit-btn {{
            width: 100%;
            background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%);
            border: none;
            border-radius: 7px;
            padding: 12px;
            color: #ffffff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.15s ease, transform 0.1s ease;
            margin-top: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }}
        .submit-btn:hover {{
            opacity: 0.95;
        }}
        .submit-btn:active {{
            transform: scale(0.99);
        }}
        .login-footer {{
            margin-top: 24px;
            text-align: center;
            font-size: 11px;
            color: #64748b;
            border-top: 1px solid #1f2937;
            padding-top: 16px;
        }}
    </style>
</head>
<body>
    <div class="login-card">
        <div class="brand-header">
            <div class="brand-icon">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                </svg>
            </div>
            <h1 class="brand-title">Virtual PSQA</h1>
            <p class="brand-subtitle">Proton PBS Patient-Specific Quality Assurance</p>
            <div class="security-badge">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <path d="m9 12 2 2 4-4"/>
                </svg>
                HIPAA Safeguards Active (TLS • Audit • 15m Timeout)
            </div>
        </div>

        {error_html}

        <form method="POST" action="/login">
            <input type="hidden" name="next" value="{safe_next}">
            <div class="form-group">
                <label class="form-label" for="username">Username</label>
                <input class="form-input" type="text" id="username" name="username" required autofocus autocomplete="username" placeholder="Enter clinical username">
            </div>
            <div class="form-group">
                <label class="form-label" for="password">Password</label>
                <input class="form-input" type="password" id="password" name="password" required autocomplete="current-password" placeholder="Enter password">
            </div>
            <button type="submit" class="submit-btn">
                <span>Sign In</span>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M5 12h14M12 5l7 7-7 7"/>
                </svg>
            </button>
        </form>

        <div class="login-footer">
            Protected Medical System • HIPAA Technical Safeguards Active
        </div>
    </div>
</body>
</html>
"""


@router.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: Optional[str] = None):
    """Serve login page. If already authenticated, redirect to next."""
    cookie = request.cookies.get(settings.AUTH_SESSION_COOKIE)
    if cookie and verify_session_token(cookie):
        dest = unquote(next) if next else "/"
        return RedirectResponse(url=dest, status_code=303)
    return HTMLResponse(content=render_login_html(error=error, next_url=next))


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    """Form submission handler for browser login with HIPAA audit logging."""
    client_ip = request.client.host if request.client else "unknown"
    user = authenticate_user(username, password, db=db)
    dest = unquote(next) if next and not next.startswith("/login") else "/"
    if not dest.startswith("/"):
        dest = "/"

    if not user:
        log_audit_event(
            username=username or "anonymous",
            action="LOGIN_FAILED",
            target_type="auth",
            details={"reason": "Invalid credentials", "attempted_user": username},
            ip_address=client_ip,
            db=db,
        )
        return HTMLResponse(
            content=render_login_html(
                error="Invalid username or password. Please check your credentials.",
                next_url=dest,
            ),
            status_code=401,
        )

    # Valid user: Record audit event and issue session cookie
    log_audit_event(
        username=user["username"],
        action="LOGIN_SUCCESS",
        target_type="auth",
        details={"role": user.get("role", "physicist")},
        ip_address=client_ip,
        db=db,
    )

    token = create_session_token(user["username"])
    response = RedirectResponse(url=dest, status_code=303)
    max_age = settings.AUTH_SESSION_EXPIRE_MINUTES * 60
    is_secure = (request.url.scheme == "https")
    response.set_cookie(
        key=settings.AUTH_SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return response


@router.get("/logout")
async def logout_get(request: Request, db: Session = Depends(get_db)):
    """Log out and redirect to login page with audit event."""
    client_ip = request.client.host if request.client else "unknown"
    cookie = request.cookies.get(settings.AUTH_SESSION_COOKIE)
    user_info = verify_session_token(cookie) if cookie else None
    username = user_info[0] if user_info else "anonymous"

    log_audit_event(
        username=username,
        action="LOGOUT",
        target_type="auth",
        ip_address=client_ip,
        db=db,
    )

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=settings.AUTH_SESSION_COOKIE, path="/")
    return response


@router.post("/logout")
async def logout_post(request: Request, db: Session = Depends(get_db)):
    """Log out via POST."""
    return await logout_get(request, db=db)


@router.post("/api/auth/login")
async def api_login(request: Request, db: Session = Depends(get_db)):
    """API JSON login endpoint."""
    client_ip = request.client.host if request.client else "unknown"
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

    username = str(data.get("username", ""))
    password = str(data.get("password", ""))

    user = authenticate_user(username, password, db=db)
    if not user:
        log_audit_event(
            username=username or "anonymous",
            action="API_LOGIN_FAILED",
            target_type="auth",
            ip_address=client_ip,
            db=db,
        )
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password"})

    log_audit_event(
        username=user["username"],
        action="API_LOGIN_SUCCESS",
        target_type="auth",
        ip_address=client_ip,
        db=db,
    )

    token = create_session_token(user["username"])
    response = JSONResponse(
        status_code=200,
        content={"status": "ok", "username": user["username"], "role": user.get("role")},
    )
    max_age = settings.AUTH_SESSION_EXPIRE_MINUTES * 60
    is_secure = (request.url.scheme == "https")
    response.set_cookie(
        key=settings.AUTH_SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )
    return response


@router.post("/api/auth/logout")
async def api_logout(request: Request, db: Session = Depends(get_db)):
    """API JSON logout endpoint."""
    client_ip = request.client.host if request.client else "unknown"
    cookie = request.cookies.get(settings.AUTH_SESSION_COOKIE)
    user_info = verify_session_token(cookie) if cookie else None
    username = user_info[0] if user_info else "anonymous"

    log_audit_event(
        username=username,
        action="API_LOGOUT",
        target_type="auth",
        ip_address=client_ip,
        db=db,
    )

    response = JSONResponse(status_code=200, content={"status": "logged_out"})
    response.delete_cookie(key=settings.AUTH_SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/me")
async def api_me(request: Request):
    """Return current session authentication status."""
    if not settings.AUTH_ENABLED:
        return {"authenticated": True, "auth_enabled": False, "user": "anonymous"}

    user = getattr(request.state, "user", None)
    if not user:
        cookie = request.cookies.get(settings.AUTH_SESSION_COOKIE)
        user_info = verify_session_token(cookie) if cookie else None
        if user_info:
            user = user_info[0]

    if user:
        return {"authenticated": True, "auth_enabled": True, "user": user}
    return {"authenticated": False, "auth_enabled": True, "user": None}


@router.get("/api/auth/users")
async def get_users_list(db: Session = Depends(get_db)):
    """List registered users for HIPAA unique user identification (§ 164.312(a)(2)(i))."""
    users = list_users(db)
    return [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/api/auth/users")
async def add_new_user(
    request: Request,
    db: Session = Depends(get_db),
):
    """Register a new unique user."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    full_name = str(data.get("full_name", "")).strip() or None
    role = str(data.get("role", "physicist")).strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    try:
        user = create_user(db, username=username, password=password, full_name=full_name, role=role)
        client_ip = request.client.host if request.client else "unknown"
        log_audit_event(
            username=getattr(request.state, "user", "admin"),
            action="USER_CREATED",
            target_type="user",
            target_id=username,
            ip_address=client_ip,
            db=db,
        )
        return {
            "status": "ok",
            "user": {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "role": user.role,
            },
        }
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))


@router.get("/api/auth/audit-logs")
async def get_audit_trail(
    limit: int = 50,
    skip: int = 0,
    username: Optional[str] = None,
    action: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Query HIPAA audit logs (§ 164.312(b))."""
    logs = get_audit_logs(limit=limit, skip=skip, username=username, action=action, db=db)
    return [
        {
            "id": l.id,
            "timestamp": l.timestamp.isoformat() if l.timestamp else None,
            "username": l.username,
            "action": l.action,
            "target_type": l.target_type,
            "target_id": l.target_id,
            "details": l.details,
            "ip_address": l.ip_address,
        }
        for l in logs
    ]
