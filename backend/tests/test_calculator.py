"""Tests for POST /api/calculator/calculate (manual CPT/ICD entry)."""
from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from backend.db.database import (
    AuditLogEntry, Base, HcpcsToEapg, ProviderConfig, ProviderCounty,
    UserAccount, async_session, engine,
)
from backend.main import app
from backend.security import hash_password

os.environ.setdefault("APP_JWT_SECRET", "test-secret-do-not-use-in-prod")


@pytest_asyncio.fixture()
async def clean_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        await s.execute(delete(AuditLogEntry))
        await s.execute(delete(UserAccount))
        await s.execute(delete(ProviderConfig))
        await s.commit()
    yield


async def _make_analyst() -> str:
    """Create an analyst user, return a valid JWT."""
    async with async_session() as s:
        u = UserAccount(username="analyst", role="analyst",
                        password_hash=hash_password("correcthorse123"))
        s.add(u)
        await s.commit()
    client = TestClient(app)
    r = client.post("/api/auth/login",
                    json={"username": "analyst", "password": "correcthorse123"})
    return r.json()["access_token"]


async def _seed_provider(cms_locality: str | None = None) -> None:
    """Install a Clinic*/Downstate DTC provider, with optional locality."""
    async with async_session() as s:
        # Reference data may already be seeded from seed_synthetic in conftest
        # or CI; we just need a county to resolve region from.
        county_exists = (await s.execute(
            select(func.count()).select_from(ProviderCounty))).scalar_one()
        if county_exists == 0:
            s.add(ProviderCounty(county_code=24, county_name="KINGS",
                                 region="Downstate"))
            await s.flush()

        s.add(ProviderConfig(
            is_active=True,
            provider_name="Test Clinic",
            county_code=24, region="Downstate",
            peer_group="Clinic*", provider_type="dtc",
            cms_locality=cms_locality,
        ))
        await s.commit()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_calculator_requires_authentication(clean_db, client):
    r = client.post("/api/calculator/calculate", json={
        "date_of_service": "2023-06-15",
        "service_lines": [{"procedure_code": "99213"}],
        "target": "apg",
    })
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# APG target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apg_requires_active_provider(clean_db, client):
    token = await _make_analyst()
    # No provider seeded
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{"procedure_code": "99213"}],
                        "target": "apg",
                    })
    assert r.status_code == 400
    assert "active provider" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_apg_returns_breakdown_with_provider(clean_db, client):
    token = await _make_analyst()
    await _seed_provider()

    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [
                            {"procedure_code": "99213", "units": 1, "billed_amount": "150.00"},
                        ],
                        "principal_diagnosis": "I10",
                        "target": "apg",
                    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target"] == "apg"
    assert body["apg"] is not None
    assert body["apg"]["peer_group"] == "Clinic*"
    assert body["apg"]["region"] == "Downstate"
    # Should have one line in the detail
    assert len(body["apg"]["line_details"]) == 1
    assert body["apg"]["line_details"][0]["procedure_code"] == "99213"
    # CMS side not populated for target=apg
    assert body["cms_lines"] is None


@pytest.mark.asyncio
async def test_apg_honours_modifier_casing(clean_db, client):
    """Modifiers submitted lowercase should be uppercased before APG lookup."""
    token = await _make_analyst()
    await _seed_provider()

    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{
                            "procedure_code": "99213", "modifiers": ["u6"], "units": 1,
                        }],
                        "target": "apg",
                    })
    assert r.status_code == 200
    # The APG engine uppercases on its own, but we should NOT double-uppercase
    # or lose the modifier. It should appear as 'U6' in the line_details.
    body = r.json()
    mods = body["apg"]["line_details"][0]["modifiers"]
    assert "U6" in mods


# ---------------------------------------------------------------------------
# CMS target (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cms_requires_locality(clean_db, client):
    token = await _make_analyst()
    await _seed_provider(cms_locality=None)   # no locality on provider

    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{"procedure_code": "99213"}],
                        "target": "cms",
                    })
    assert r.status_code == 400
    assert "locality" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cms_uses_provider_locality(clean_db, client):
    """When no locality in payload, CMS should fall back to the provider config."""
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    # Mock the CMS engine call so we don't hit the live API in tests
    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        from backend.db.database import CmsRateCache
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        return CmsRateCache(
            hcpcs=hcpcs, modifier=modifier, locality=locality, year=year,
            non_facility_rate=Decimal("92.47"),
            facility_rate=Decimal("68.32"),
            work_rvu=Decimal("1.30"),
            cached_at=now, cached_until=now + timedelta(hours=24),
        )

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [{"procedure_code": "99213", "units": 1}],
                            "target": "cms",
                        })

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cms_locality_used"] == "01"
    assert len(body["cms_lines"]) == 1
    assert Decimal(body["cms_lines"][0]["non_facility_rate"]) == Decimal("92.47")
    # 1 unit → expected = non-facility
    assert Decimal(body["cms_lines"][0]["expected_payment"]) == Decimal("92.47")


@pytest.mark.asyncio
async def test_cms_explicit_locality_overrides_provider(clean_db, client):
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    captured = {}

    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        captured["locality"] = locality
        return None  # no rate found — still valid response

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [{"procedure_code": "99213"}],
                            "target": "cms",
                            "cms_locality": "14",
                        })

    assert r.status_code == 200
    assert captured["locality"] == "14"
    assert r.json()["cms_locality_used"] == "14"


@pytest.mark.asyncio
async def test_cms_missing_rate_returns_error_on_line(clean_db, client):
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        return None

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [{"procedure_code": "XXXXX"}],
                            "target": "cms",
                        })
    assert r.status_code == 200
    assert r.json()["cms_lines"][0]["error"] is not None
    assert "XXXXX" in r.json()["cms_lines"][0]["error"]


# ---------------------------------------------------------------------------
# Both target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cms_pc_tc_split_when_opted_in(clean_db, client):
    """When cms_include_pc_tc=true, each line is enriched with professional
    (-26) and technical (-TC) rates fetched from additional CMS lookups.
    Codes with no PC/TC split (e.g. E/M) return None for those fields."""
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        # Only procedure '70450' (hypothetical radiology) has a PC/TC split
        # in this fixture. '99213' (E/M) does not.
        from backend.db.database import CmsRateCache
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        if hcpcs == "70450":
            if modifier == "26":
                return CmsRateCache(
                    hcpcs=hcpcs, modifier="26", locality=locality, year=year,
                    non_facility_rate=Decimal("42.00"),
                    cached_at=now, cached_until=now + timedelta(hours=24),
                )
            if modifier == "TC":
                return CmsRateCache(
                    hcpcs=hcpcs, modifier="TC", locality=locality, year=year,
                    non_facility_rate=Decimal("78.00"),
                    cached_at=now, cached_until=now + timedelta(hours=24),
                )
            return CmsRateCache(
                hcpcs=hcpcs, modifier="", locality=locality, year=year,
                non_facility_rate=Decimal("120.00"),
                cached_at=now, cached_until=now + timedelta(hours=24),
            )
        if hcpcs == "99213":
            if modifier in ("26", "TC"):
                return None   # no PC/TC split for E/M codes
            return CmsRateCache(
                hcpcs=hcpcs, modifier="", locality=locality, year=year,
                non_facility_rate=Decimal("92.47"),
                cached_at=now, cached_until=now + timedelta(hours=24),
            )
        return None

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [
                                {"procedure_code": "70450"},
                                {"procedure_code": "99213"},
                            ],
                            "target": "cms",
                            "cms_include_pc_tc": True,
                        })
    assert r.status_code == 200, r.text
    lines = r.json()["cms_lines"]

    # 70450 has PC/TC split
    assert Decimal(lines[0]["non_facility_rate"]) == Decimal("120.00")
    assert Decimal(lines[0]["professional_rate"]) == Decimal("42.00")
    assert Decimal(lines[0]["technical_rate"]) == Decimal("78.00")

    # 99213 doesn't — professional and technical are null
    assert Decimal(lines[1]["non_facility_rate"]) == Decimal("92.47")
    assert lines[1]["professional_rate"] is None
    assert lines[1]["technical_rate"] is None


@pytest.mark.asyncio
async def test_cms_pc_tc_off_by_default(clean_db, client):
    """Without the flag, professional/technical fields are null for every line."""
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    call_count = {"n": 0}

    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        call_count["n"] += 1
        from backend.db.database import CmsRateCache
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        return CmsRateCache(
            hcpcs=hcpcs, modifier=modifier, locality=locality, year=year,
            non_facility_rate=Decimal("100.00"),
            cached_at=now, cached_until=now + timedelta(hours=24),
        )

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [{"procedure_code": "99213"}],
                            "target": "cms",
                            # cms_include_pc_tc not passed — defaults to False
                        })
    assert r.status_code == 200
    body = r.json()
    assert body["cms_lines"][0]["professional_rate"] is None
    assert body["cms_lines"][0]["technical_rate"] is None
    # Only ONE lookup fired, not three
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_apg_variance_uses_paid_field(clean_db, client):
    """The form's Paid $ (per line) must drive the APG variance calculation.
    If paid total equals correct APG, variance = 0 and compression = 0."""
    token = await _make_analyst()
    await _seed_provider()

    # First call: no paid amount — variance should equal the full correct APG
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{"procedure_code": "17000"}],
                        "target": "apg",
                    })
    assert r.status_code == 200
    apg = r.json()["apg"]
    correct = Decimal(apg["correct_apg_payment"])
    assert Decimal(apg["variance"]) == correct   # full underpayment vs $0

    # Second call: paid = correct. Variance should be 0.
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [
                            {"procedure_code": "17000", "billed_amount": str(correct)},
                        ],
                        "target": "apg",
                    })
    apg2 = r.json()["apg"]
    assert Decimal(apg2["variance"]) == Decimal("0")


@pytest.mark.asyncio
async def test_cms_dataset_moved_surfaces_as_banner_warning(clean_db, client):
    """When the CMS dataset URL returns 404 (CMS migrated their API), the
    calculator should hide the CMS panel and surface a single friendly
    warning message — not a per-line error spam."""
    from backend.engines.cms_engine import CMSDatasetMovedError

    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    async def fake_get_mpfs_raises(self, session, hcpcs, modifier, locality, year, **_):
        raise CMSDatasetMovedError(
            "CMS MPFS dataset is no longer available at the expected URL."
        )

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs_raises,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [
                                {"procedure_code": "99213"},
                                {"procedure_code": "17000"},
                            ],
                            "target": "cms",
                        })

    assert r.status_code == 200
    body = r.json()
    # No per-line CMS errors — the whole panel is hidden
    assert body["cms_lines"] is None
    assert body["cms_locality_used"] is None
    # Instead, a single banner-level warning explaining the situation
    assert any("CMS MPFS integration is being updated" in w
               for w in body["warnings"]), body["warnings"]


@pytest.mark.asyncio
async def test_both_returns_apg_and_cms(clean_db, client):
    token = await _make_analyst()
    await _seed_provider(cms_locality="01")

    async def fake_get_mpfs(self, session, hcpcs, modifier, locality, year, **_):
        from backend.db.database import CmsRateCache
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        return CmsRateCache(
            hcpcs=hcpcs, modifier=modifier, locality=locality, year=year,
            non_facility_rate=Decimal("100.00"),
            cached_at=now, cached_until=now + timedelta(hours=24),
        )

    with patch(
        "backend.engines.cms_engine.CMSFeeScheduleEngine.get_mpfs_rate",
        new=fake_get_mpfs,
    ):
        r = client.post("/api/calculator/calculate",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "date_of_service": "2023-06-15",
                            "service_lines": [
                                {"procedure_code": "99213", "units": 2},
                            ],
                            "target": "both",
                        })

    assert r.status_code == 200
    body = r.json()
    assert body["apg"] is not None
    assert body["cms_lines"] is not None
    assert len(body["cms_lines"]) == 1
    # 2 units → expected = 2 * 100 = 200
    assert Decimal(body["cms_lines"][0]["expected_payment"]) == Decimal("200.00")


@pytest.mark.asyncio
async def test_both_tolerates_missing_provider_for_cms_only(clean_db, client):
    """If target=both but only CMS is configurable, we warn + return CMS."""
    token = await _make_analyst()
    # No provider seeded — APG will be skipped with a warning
    # But no CMS locality either — CMS will be skipped with a warning
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{"procedure_code": "99213"}],
                        "target": "both",
                    })
    assert r.status_code == 200
    body = r.json()
    assert body["apg"] is None
    assert body["cms_lines"] is None
    assert any("APG skipped" in w for w in body["warnings"])
    assert any("CMS skipped" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_empty_service_lines(clean_db, client):
    token = await _make_analyst()
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [],
                        "target": "apg",
                    })
    assert r.status_code == 422   # pydantic validation error


@pytest.mark.asyncio
async def test_rejects_too_many_modifiers(clean_db, client):
    token = await _make_analyst()
    r = client.post("/api/calculator/calculate",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "date_of_service": "2023-06-15",
                        "service_lines": [{
                            "procedure_code": "99213",
                            "modifiers": ["25", "59", "76", "LT", "RT"],   # 5 > max 4
                        }],
                        "target": "apg",
                    })
    assert r.status_code == 422
