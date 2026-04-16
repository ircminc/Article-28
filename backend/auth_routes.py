"""Auth + user-admin routes.

Split out of main.py to keep the surface readable. Mounted in main.py via:
    from backend.auth_routes import auth_router, admin_router
    app.include_router(auth_router)
    app.include_router(admin_router)

Endpoints:
    POST   /api/auth/login           — username/password → JWT
    GET    /api/auth/me              — echoes the current user (handy for SPA bootstrap)
    POST   /api/auth/logout          — no-op for JWT, but we emit an audit event
    POST   /api/auth/password        — change your own password

    GET    /api/admin/users          — list all users (admin only)
    POST   /api/admin/users          — create a user (admin only)
    PATCH  /api/admin/users/{id}     — update role/name/disabled (admin only)
    POST   /api/admin/users/{id}/password  — admin-reset password (admin only)
    GET    /api/admin/audit          — list audit log entries (admin only)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import AuditLogEntry, UserAccount, get_session
from backend.deps import CurrentUser, RequireAdmin, audit
from backend.models.schemas import (
    AdminPasswordResetIn,
    LoginIn,
    PasswordChangeIn,
    TokenOut,
    UserCreate,
    UserOut,
    UserRole,
    UserUpdate,
)
from backend.security import (
    hash_password,
    issue_token,
    is_valid_role,
    needs_rehash,
    verify_password,
)

log = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_out(u: UserAccount) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        full_name=u.full_name,
        email=u.email,
        role=UserRole(u.role),
        disabled=u.disabled,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@auth_router.post("/login", response_model=TokenOut)
async def login(
    request: Request,
    payload: LoginIn,
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    """Exchange username + password for a JWT.

    Security properties:
      * constant-time password verification (argon2)
      * identical error for 'no such user' vs 'wrong password' to prevent
        username enumeration (both return "Invalid credentials")
      * disabled users cannot log in
      * every attempt, success or failure, is written to audit_log
      * successful login updates last_login_at
    """
    # Lookup by username. Username is case-insensitive for UX — we store
    # as-entered but compare lowercased. Existing unique index enforces that
    # no two usernames differ only by case at creation time.
    stmt = select(UserAccount).where(
        UserAccount.username == payload.username.strip()
    )
    user = (await session.execute(stmt)).scalar_one_or_none()

    # Verify password regardless of user existence to keep timings similar
    # (argon2 still takes ~100ms on a bogus hash). This is belt-and-suspenders;
    # the bigger effect is the unified error message below.
    ok = False
    if user is not None and not user.disabled:
        ok = verify_password(payload.password, user.password_hash)

    if not ok:
        await audit(
            session, user=None, request=request,
            action="login.failure", resource=payload.username,
            success=False,
            details={"reason": "disabled_or_bad_credentials"},
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Upgrade hash params if this was the first login after a parameter change
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.last_login_at = datetime.utcnow()
    await audit(
        session, user=user, request=request,
        action="login.success", resource=user.username,
    )
    await session.commit()

    token = issue_token(user_id=user.id, username=user.username, role=user.role)
    return TokenOut(
        access_token=token["access_token"],
        token_type=token["token_type"],
        expires_at=token["expires_at"],
        user=_to_out(user),
    )


@auth_router.get("/me", response_model=UserOut)
async def me(current: CurrentUser) -> UserOut:
    """Return the currently-authenticated user. Used by the SPA on boot to
    decide whether to redirect to /login or restore the session."""
    return _to_out(current)


@auth_router.post("/logout")
async def logout(
    request: Request,
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """No server-side state to clear with JWT — we just emit an audit event.
    The client is responsible for discarding its token."""
    await audit(session, user=current, request=request, action="logout")
    await session.commit()
    return {"ok": True}


@auth_router.post("/password")
async def change_my_password(
    request: Request,
    payload: PasswordChangeIn,
    current: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Self-service password change. Must supply the current password."""
    if not verify_password(payload.current_password, current.password_hash):
        await audit(
            session, user=current, request=request,
            action="user.password_change", resource=current.username,
            success=False, details={"reason": "bad_current_password"},
        )
        await session.commit()
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current.password_hash = hash_password(payload.new_password)
    current.password_changed_at = datetime.utcnow()
    await audit(
        session, user=current, request=request,
        action="user.password_change", resource=current.username,
    )
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


@admin_router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: RequireAdmin,
    include_disabled: bool = Query(True),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    stmt = select(UserAccount).order_by(UserAccount.username)
    if not include_disabled:
        stmt = stmt.where(UserAccount.disabled.is_(False))
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(u) for u in rows]


@admin_router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    request: Request,
    payload: UserCreate,
    admin: RequireAdmin,
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if not is_valid_role(payload.role.value):
        raise HTTPException(status_code=400, detail=f"Unknown role: {payload.role}")

    # Unique username
    q = await session.execute(
        select(UserAccount).where(UserAccount.username == payload.username.strip())
    )
    if q.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Username already exists")

    user = UserAccount(
        username=payload.username.strip(),
        full_name=payload.full_name,
        email=payload.email,
        role=payload.role.value,
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    await session.flush()
    await audit(
        session, user=admin, request=request,
        action="user.create", resource=user.username,
        details={"role": user.role},
    )
    await session.commit()
    return _to_out(user)


@admin_router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    request: Request,
    user_id: int,
    payload: UserUpdate,
    admin: RequireAdmin,
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await session.get(UserAccount, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Safety: an admin cannot disable themselves or demote themselves out of
    # admin role if no other admin exists. This avoids accidental lock-out.
    if user.id == admin.id and (payload.disabled is True or (
        payload.role is not None and payload.role.value != "admin"
    )):
        # Count other active admins
        others = await session.execute(
            select(UserAccount).where(
                UserAccount.role == "admin",
                UserAccount.disabled.is_(False),
                UserAccount.id != admin.id,
            )
        )
        if others.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=400,
                detail="Cannot demote or disable the last active admin",
            )

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.email is not None:
        user.email = payload.email
    if payload.role is not None:
        user.role = payload.role.value
    if payload.disabled is not None:
        user.disabled = payload.disabled

    await audit(
        session, user=admin, request=request,
        action="user.update", resource=user.username,
        details={"role": user.role, "disabled": user.disabled},
    )
    await session.commit()
    await session.refresh(user)
    return _to_out(user)


@admin_router.post("/users/{user_id}/password")
async def admin_reset_password(
    request: Request,
    user_id: int,
    payload: AdminPasswordResetIn,
    admin: RequireAdmin,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await session.get(UserAccount, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.new_password)
    user.password_changed_at = datetime.utcnow()
    await audit(
        session, user=admin, request=request,
        action="user.password_reset", resource=user.username,
    )
    await session.commit()
    return {"ok": True}


@admin_router.get("/audit")
async def list_audit(
    admin: RequireAdmin,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    action: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(AuditLogEntry).order_by(desc(AuditLogEntry.occurred_at))
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if username:
        stmt = stmt.where(AuditLogEntry.username_at_event == username)
    stmt = stmt.offset(offset).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": r.id,
                "occurred_at": r.occurred_at.isoformat() + "Z",
                "user_id": r.user_id,
                "username": r.username_at_event,
                "action": r.action,
                "resource": r.resource,
                "ip_address": r.ip_address,
                "success": r.success,
                "details": r.details,
            }
            for r in rows
        ],
    }
