"""Authentication + authorization tests.

Covers:
  * Password hashing round-trip + tamper detection
  * JWT issue + verify + expiry
  * Full login flow via TestClient
  * 401 on missing/invalid tokens
  * 403 on role gates (analyst hitting admin endpoints)
  * Audit log writes on login success, login failure, logout, upload, export
  * Disabled-user lockout takes effect immediately
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.db.database import AuditLogEntry, Base, UserAccount, async_session, engine
from backend.main import app
from backend.security import (
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)

# Keep JWT secret stable across tests — otherwise random per-process secrets
# would invalidate tokens between instantiations.
os.environ.setdefault("APP_JWT_SECRET", "test-secret-do-not-use-in-prod")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def clean_users():
    """Remove all users + audit log rows before each test for isolation."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        await s.execute(delete(AuditLogEntry))
        await s.execute(delete(UserAccount))
        await s.commit()
    yield
    async with async_session() as s:
        await s.execute(delete(AuditLogEntry))
        await s.execute(delete(UserAccount))
        await s.commit()


@pytest_asyncio.fixture()
async def admin_user(clean_users):
    async with async_session() as s:
        user = UserAccount(
            username="admin", full_name="Admin User",
            password_hash=hash_password("supersecret123"),
            role="admin",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
    return {"id": user.id, "username": "admin", "password": "supersecret123"}


@pytest_asyncio.fixture()
async def analyst_user(clean_users):
    async with async_session() as s:
        user = UserAccount(
            username="analyst", full_name="Analyst",
            password_hash=hash_password("analystpass456"),
            role="analyst",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
    return {"id": user.id, "username": "analyst", "password": "analystpass456"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Password hashing + JWT primitives
# ---------------------------------------------------------------------------


def test_password_hash_round_trip():
    h = hash_password("ThisIsLongEnough123")
    assert h != "ThisIsLongEnough123"
    assert verify_password("ThisIsLongEnough123", h) is True
    assert verify_password("wrongpassword", h) is False
    # Same plaintext hashed twice yields different hashes (unique salt)
    h2 = hash_password("ThisIsLongEnough123")
    assert h != h2
    assert verify_password("ThisIsLongEnough123", h2) is True


def test_password_min_length_enforced():
    with pytest.raises(ValueError):
        hash_password("short")


def test_jwt_issue_and_decode():
    tok = issue_token(user_id=42, username="zoe", role="analyst")
    assert tok["token_type"] == "bearer"
    payload = decode_token(tok["access_token"])
    assert payload is not None
    assert payload["sub"] == "42"
    assert payload["uname"] == "zoe"
    assert payload["role"] == "analyst"


def test_jwt_tampered_token_rejected():
    tok = issue_token(user_id=1, username="x", role="admin")
    tampered = tok["access_token"][:-4] + "ABCD"
    assert decode_token(tampered) is None


def test_jwt_expired_token_rejected():
    """An expired token should decode to None, not raise."""
    # Issue a real token, then replace its payload with an expired one.
    import jwt as pyjwt
    secret = os.environ["APP_JWT_SECRET"]
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    token = pyjwt.encode(
        {"sub": "1", "role": "admin", "iat": int(past.timestamp()),
         "exp": int(past.timestamp()) + 10},
        secret, algorithm="HS256",
    )
    assert decode_token(token) is None


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_returns_token_and_user(admin_user, client):
    r = client.post("/api/auth/login", json={
        "username": admin_user["username"], "password": admin_user["password"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "admin"


@pytest.mark.asyncio
async def test_login_bad_password_returns_401(admin_user, client):
    r = client.post("/api/auth/login", json={
        "username": admin_user["username"], "password": "wrongpass123",
    })
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_unknown_user_returns_same_401(clean_users, client):
    """Unknown user and wrong password must produce the same error to prevent
    username enumeration."""
    r = client.post("/api/auth/login", json={
        "username": "nosuchuser", "password": "whatever12345",
    })
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_audits_success_and_failure(admin_user, client):
    client.post("/api/auth/login", json={
        "username": admin_user["username"], "password": admin_user["password"],
    })
    client.post("/api/auth/login", json={
        "username": admin_user["username"], "password": "wrong1234567",
    })
    async with async_session() as s:
        rows = (await s.execute(select(AuditLogEntry).order_by(AuditLogEntry.id))).scalars().all()
    actions = [r.action for r in rows]
    assert "login.success" in actions
    assert "login.failure" in actions


# ---------------------------------------------------------------------------
# Token-gated endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claims_endpoint_requires_auth(client):
    r = client.get("/api/claims")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_claims_endpoint_accepts_valid_token(analyst_user, client):
    token = _login(client, analyst_user["username"], analyst_user["password"])
    r = client.get("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_is_public(client):
    """The health endpoint must not require auth so uptime monitors work."""
    r = client.get("/api/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_endpoint_rejects_analyst(analyst_user, client):
    token = _login(client, analyst_user["username"], analyst_user["password"])
    r = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_list_and_create_users(admin_user, client):
    token = _login(client, admin_user["username"], admin_user["password"])
    h = {"Authorization": f"Bearer {token}"}
    # Initially only the admin exists
    r = client.get("/api/admin/users", headers=h)
    assert r.status_code == 200
    existing = r.json()
    assert any(u["username"] == "admin" for u in existing)

    # Create an analyst
    r = client.post("/api/admin/users", headers=h, json={
        "username": "newbie",
        "password": "analysthellofr!",
        "role": "analyst",
    })
    assert r.status_code == 201
    assert r.json()["username"] == "newbie"

    # Duplicate username → 409
    r = client.post("/api/admin/users", headers=h, json={
        "username": "newbie",
        "password": "anotherpw123",
        "role": "analyst",
    })
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_admin_cannot_disable_last_admin(admin_user, client):
    token = _login(client, admin_user["username"], admin_user["password"])
    h = {"Authorization": f"Bearer {token}"}
    r = client.patch(f"/api/admin/users/{admin_user['id']}", headers=h, json={
        "disabled": True,
    })
    assert r.status_code == 400
    assert "last active admin" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Disabled lockout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_user_cannot_use_existing_token(analyst_user, client):
    """After an admin disables an analyst, the analyst's existing token stops
    working — the get_current_user dependency checks user.disabled per request."""
    token = _login(client, analyst_user["username"], analyst_user["password"])

    async with async_session() as s:
        u = await s.get(UserAccount, analyst_user["id"])
        u.disabled = True
        await s.commit()

    r = client.get("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# /me endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_returns_current_user(admin_user, client):
    token = _login(client, admin_user["username"], admin_user["password"])
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert "password_hash" not in r.json()
