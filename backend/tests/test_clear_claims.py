"""Tests for DELETE /api/claims (admin danger-zone action).

Covers:
  * Unauthenticated → 401
  * Viewer role → 403 (cannot clear)
  * Analyst role → 200, claims actually removed, audit row written
  * Admin role  → 200 (admin inherits analyst permissions via RequireAnalyst)
  * Count of deleted rows is accurate
  * Users / reference data / audit log from prior actions are preserved
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from backend.db.database import (
    ApgResult,
    AuditLogEntry,
    Base,
    ClaimAdjustment,
    HcpcsToEapg,
    ParsedClaim,
    ParsedServiceLine,
    UserAccount,
    async_session,
    engine,
)
from backend.main import app
from backend.security import hash_password

os.environ.setdefault("APP_JWT_SECRET", "test-secret-do-not-use-in-prod")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def clean_db():
    """Start each test with no users, no claims, no audit log."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        await s.execute(delete(AuditLogEntry))
        await s.execute(delete(ApgResult))
        await s.execute(delete(ClaimAdjustment))
        await s.execute(delete(ParsedServiceLine))
        await s.execute(delete(ParsedClaim))
        await s.execute(delete(UserAccount))
        await s.commit()
    yield


async def _make_user(username: str, role: str, password: str = "correcthorse123") -> int:
    async with async_session() as s:
        u = UserAccount(username=username, role=role, password_hash=hash_password(password))
        s.add(u)
        await s.commit()
        return u.id


async def _seed_claims(n: int = 3) -> None:
    """Insert N fake claims + one service line + one APG result each."""
    async with async_session() as s:
        for i in range(n):
            claim = ParsedClaim(
                file_id="fx", file_type="835I",
                claim_id=f"TEST-{i:04d}",
                billed_amount=Decimal("100"),
                paid_amount=Decimal("80"),
                allowed_amount=Decimal("100"),
                patient_responsibility=Decimal("0"),
            )
            s.add(claim)
            await s.flush()
            s.add(ParsedServiceLine(
                claim_id_fk=claim.id, line_seq=1, procedure_code="99213",
                billed_amount=Decimal("100"), paid_amount=Decimal("80"),
                allowed_amount=Decimal("100"),
            ))
            s.add(ClaimAdjustment(
                claim_id_fk=claim.id, group_code="CO", reason_code="45",
                amount=Decimal("20"),
            ))
            s.add(ApgResult(
                claim_id_fk=claim.id,
                correct_apg_payment=Decimal("90"),
                actual_paid=Decimal("80"),
                variance=Decimal("10"),
                compression_pct=Decimal("11.11"),
                base_rate_applied=Decimal("150"),
                peer_group="Clinic*", region="Downstate",
                line_details=[],
            ))
        await s.commit()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unauthenticated_request_is_rejected(clean_db, client):
    r = client.delete("/api/claims")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_viewer_role_is_forbidden(clean_db, client):
    await _make_user("view", "viewer")
    token = _login(client, "view", "correcthorse123")

    # Seed some claims so we can confirm nothing got deleted
    await _seed_claims(3)

    r = client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403

    # Confirm data is untouched
    async with async_session() as s:
        count = (await s.execute(select(func.count()).select_from(ParsedClaim))).scalar_one()
    assert count == 3


@pytest.mark.asyncio
async def test_analyst_can_clear_all_claims(clean_db, client):
    await _make_user("analyst", "analyst")
    token = _login(client, "analyst", "correcthorse123")
    await _seed_claims(3)

    r = client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["claims_deleted"] == 3

    async with async_session() as s:
        # Claims + children all gone
        for cls in (ParsedClaim, ParsedServiceLine, ClaimAdjustment, ApgResult):
            count = (await s.execute(select(func.count()).select_from(cls))).scalar_one()
            assert count == 0, f"{cls.__name__} should be empty"


@pytest.mark.asyncio
async def test_admin_can_also_clear(clean_db, client):
    """RequireAnalyst admits both analyst and admin roles."""
    await _make_user("boss", "admin")
    token = _login(client, "boss", "correcthorse123")
    await _seed_claims(2)

    r = client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["claims_deleted"] == 2


@pytest.mark.asyncio
async def test_clear_writes_audit_entry(clean_db, client):
    await _make_user("analyst", "analyst")
    token = _login(client, "analyst", "correcthorse123")
    await _seed_claims(5)

    client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})

    async with async_session() as s:
        rows = (await s.execute(
            select(AuditLogEntry).where(AuditLogEntry.action == "claims.clear_all")
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].username_at_event == "analyst"
    assert rows[0].details == {"claims_deleted": 5}


@pytest.mark.asyncio
async def test_clear_preserves_users_and_reference_data(clean_db, client):
    """Clearing claims MUST NOT touch users, reference data, or login audit.

    We snapshot reference-data counts before + after rather than asserting a
    specific number, because the shared dev DB may already have the synthetic
    seed loaded from other tests / runs.
    """
    await _make_user("analyst", "analyst")
    await _make_user("bystander", "viewer")

    # Snapshot reference-table size BEFORE the clear
    async with async_session() as s:
        ref_before = (await s.execute(
            select(func.count()).select_from(HcpcsToEapg))).scalar_one()

    token = _login(client, "analyst", "correcthorse123")
    await _seed_claims(2)

    r = client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

    async with async_session() as s:
        # Users preserved (the two we just made)
        user_count = (await s.execute(select(func.count()).select_from(UserAccount))).scalar_one()
        assert user_count == 2
        # Reference data preserved — same count as before, untouched by clear
        ref_after = (await s.execute(
            select(func.count()).select_from(HcpcsToEapg))).scalar_one()
        assert ref_after == ref_before
        # Login audit events preserved (at least the one from _login above)
        login_count = (await s.execute(
            select(func.count()).select_from(AuditLogEntry)
            .where(AuditLogEntry.action == "login.success")
        )).scalar_one()
        assert login_count >= 1


@pytest.mark.asyncio
async def test_clear_on_empty_db_returns_zero(clean_db, client):
    """Clearing when nothing's there should succeed with a zero count."""
    await _make_user("analyst", "analyst")
    token = _login(client, "analyst", "correcthorse123")

    r = client.delete("/api/claims", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["claims_deleted"] == 0
