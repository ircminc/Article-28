"""FastAPI dependencies for authentication + authorization + audit.

Usage pattern (in main.py):

    from backend.deps import CurrentUser, RequireAdmin, audit

    @app.get("/api/secret")
    async def secret_endpoint(user: CurrentUser):
        ...

    @app.post("/api/admin/users")
    async def create_user(payload: UserCreate, user: RequireAdmin, ...):
        ...

Pattern rationale:
  * `CurrentUser` — any authenticated, non-disabled user. Most endpoints use this.
  * `RequireAnalyst` — shorthand for "analyst or admin" (not a strict role check).
  * `RequireAdmin` — admin only. Returns 403 for analysts/viewers.
  * `audit()` — free function, not a Depends. Call from inside endpoints when
    something audit-worthy happens; it writes to audit_log.

Token delivery:
  We accept the JWT via the Authorization header ("Bearer <token>"). This works
  for both the React SPA (which attaches via axios interceptor) and CLI/curl
  callers.

Disabled-user check:
  Every request re-loads the user row and checks `disabled`. That means
  disabling a user takes effect immediately — no need for a revocation list.
  The cost is one extra SQL lookup per request; acceptable for this scale.
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import AuditLogEntry, UserAccount, get_session
from backend.security import decode_token

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level: parse the Authorization header into a UserAccount
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> UserAccount:
    """FastAPI dependency — resolves the requesting user or raises 401/403.

    Auth flow:
      1. Read Authorization header → extract the bearer token
      2. Decode the JWT → get user id
      3. Load the user → confirm not disabled
      4. Return the ORM row
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Malformed token payload")

    user = await session.get(UserAccount, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    if user.disabled:
        raise HTTPException(status_code=403, detail="Account disabled")

    # Stash on request.state so audit() calls inside the endpoint don't need
    # to re-parse the header.
    request.state.current_user = user
    return user


CurrentUser = Annotated[UserAccount, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------


def require_role(*allowed_roles: str):
    """Build a dependency that requires the user to have one of the given roles.

    Usage:
        @app.post("/api/admin/users")
        async def create_user(user: Annotated[UserAccount, Depends(require_role('admin'))]):
    """
    async def _gate(user: CurrentUser) -> UserAccount:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires one of roles: {', '.join(allowed_roles)}",
            )
        return user
    return _gate


# Common shorthands
RequireAdmin = Annotated[UserAccount, Depends(require_role("admin"))]
RequireAnalyst = Annotated[UserAccount, Depends(require_role("admin", "analyst"))]


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


async def audit(
    session: AsyncSession,
    *,
    user: Optional[UserAccount],
    action: str,
    resource: Optional[str] = None,
    success: bool = True,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    """Write one AuditLogEntry. Call this from inside endpoints on sensitive actions.

    Passing `request` is the preferred way to get the IP — it handles X-Forwarded-For
    when the app is behind a trusted reverse proxy.
    """
    ip = ip_address
    if ip is None and request is not None:
        ip = _extract_ip(request)

    entry = AuditLogEntry(
        user_id=user.id if user else None,
        username_at_event=user.username if user else None,
        action=action,
        resource=resource,
        success=success,
        details=details or None,
        ip_address=ip,
    )
    session.add(entry)
    # Caller is expected to commit the enclosing transaction.


def _extract_ip(request: Request) -> Optional[str]:
    """Prefer X-Forwarded-For when present (trusted reverse proxy), else peer IP."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First IP is the original client; upstream proxies append their own
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else None


__all__ = [
    "CurrentUser", "RequireAdmin", "RequireAnalyst",
    "get_current_user", "require_role",
    "audit",
]
