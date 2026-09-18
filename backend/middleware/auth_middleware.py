"""
Authentication & Audit middleware for Virtual PSQA (HIPAA Compliant).

Enforces:
1. Unique user identity verification (§ 164.312(a)(2)(i))
2. 15-minute sliding inactivity timeout (§ 164.312(a)(2)(iii))
3. Patient ePHI access audit recording (§ 164.312(b))
4. Secure cookie transmission (§ 164.312(e))
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from config import settings
from services.audit_service import log_audit_event
from services.auth_service import create_session_token, verify_basic_auth, verify_session_token

logger = logging.getLogger(__name__)

PUBLIC_PREFIXES = (
    "/login",
    "/logout",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/favicon.ico",
)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not settings.AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path

        # 1. Allow public authentication endpoints
        for prefix in PUBLIC_PREFIXES:
            if path == prefix or path.startswith(prefix + "?") or path.startswith(prefix + "/"):
                return await call_next(request)

        # 2. Check session cookie
        cookie_val = request.cookies.get(settings.AUTH_SESSION_COOKIE)
        auth_res = verify_session_token(cookie_val) if cookie_val else None
        user = None
        issued_at = 0
        should_renew_cookie = False

        if auth_res:
            user, issued_at = auth_res
            # Sliding session: renew cookie if older than 60s
            if time.time() - issued_at > 60:
                should_renew_cookie = True

        # 3. Fallback: Check HTTP Basic Auth header
        if not user:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Basic "):
                user = verify_basic_auth(auth_header)

        # 4. If authenticated, process request
        if user:
            request.state.user = user
            response = await call_next(request)

            # Slide session timeout forward for active user
            if should_renew_cookie:
                new_token = create_session_token(user)
                max_age = settings.AUTH_SESSION_EXPIRE_MINUTES * 60
                is_secure = (request.url.scheme == "https")
                response.set_cookie(
                    key=settings.AUTH_SESSION_COOKIE,
                    value=new_token,
                    max_age=max_age,
                    httponly=True,
                    samesite="lax",
                    secure=is_secure,
                    path="/",
                )

            # HIPAA Audit trail for sensitive ePHI operations
            if path.startswith("/api/plans/") and request.method in ("DELETE", "POST"):
                client_ip = request.client.host if request.client else "unknown"
                parts = path.split("/")
                plan_id = parts[3] if len(parts) > 3 else "unknown"
                log_audit_event(
                    username=user,
                    action=f"PLAN_{request.method}",
                    target_type="plan",
                    target_id=plan_id,
                    details={"path": path, "status_code": response.status_code},
                    ip_address=client_ip,
                )

            return response

        # 5. Handle unauthenticated requests
        accept_header = request.headers.get("accept", "")
        is_api = (
            path.startswith("/api/")
            or "application/json" in accept_header
            or "text/event-stream" in accept_header
        )

        if is_api:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required. Session may have timed out after 15 minutes of inactivity."},
                headers={"WWW-Authenticate": 'Basic realm="Virtual PSQA"'},
            )

        # Browser navigation: redirect to /login with next target
        next_target = path
        if request.url.query:
            next_target += f"?{request.url.query}"

        encoded_next = quote(next_target, safe="")
        return RedirectResponse(url=f"/login?next={encoded_next}", status_code=303)
