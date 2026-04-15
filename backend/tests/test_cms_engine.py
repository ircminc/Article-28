"""Unit tests for the CMS MPFS engine.

The CMS API is mocked with httpx.MockTransport so tests are offline-safe and
deterministic. We verify:
  - A successful response parses into a CmsRateCache row with all RVU fields
  - A cache hit skips the API call
  - A stale cache is returned as fallback when the API fails
  - force_refresh bypasses the cache
  - ZIP → locality resolution
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from backend.db.database import (
    Base,
    CmsRateCache,
    ZipLocality,
    async_session,
    engine,
)
from backend.engines.cms_engine import CMSFeeScheduleEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def session():
    # Ensure schema exists (tests may run before startup hook)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        # Clean caches between tests
        await s.execute(delete(CmsRateCache))
        await s.execute(delete(ZipLocality))
        await s.commit()
        yield s


def _make_engine(handler) -> CMSFeeScheduleEngine:
    """Build a CMSFeeScheduleEngine with a MockTransport-backed httpx client."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return CMSFeeScheduleEngine(client=client, cache_ttl_seconds=3600)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_lookup_populates_cache(session):
    """A 200 response with a matching record is parsed and cached."""
    payload = [{
        "HCPCS_CD": "99213",
        "LOCALITY_NUM": "14",
        "YEAR": "2023",
        "NON_FAC_PRICE": "92.47",
        "FACILITY_PRICE": "68.32",
        "WORK_RVU": "1.30",
        "NON_FAC_PE_RVU": "1.25",
        "MP_RVU": "0.08",
        "NON_FAC_TOTAL_RVU": "2.63",
        "CONV_FACTOR": "33.8872",
    }]

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx >=0.26 returns bytes for .query; decode defensively
        qs = request.url.query
        if isinstance(qs, bytes):
            qs = qs.decode("ascii")
        assert "99213" in qs
        assert "14" in qs
        return httpx.Response(200, json=payload)

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "14", 2023)
        assert row is not None
        assert row.non_facility_rate == Decimal("92.47")
        assert row.facility_rate == Decimal("68.32")
        assert row.work_rvu == Decimal("1.30")
        assert row.total_rvu == Decimal("2.63")
        assert row.cached_until > datetime.utcnow()
    finally:
        await eng.aclose()


@pytest.mark.asyncio
async def test_cache_hit_skips_api(session):
    """A fresh cached row must prevent any API call."""
    # Seed a fresh cache entry
    session.add(CmsRateCache(
        hcpcs="99213", modifier="", locality="14", year=2023,
        non_facility_rate=Decimal("92.47"),
        cached_at=datetime.utcnow(),
        cached_until=datetime.utcnow() + timedelta(hours=1),
    ))
    await session.commit()

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500)  # would be an error if called

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "14", 2023)
        assert row is not None
        assert row.non_facility_rate == Decimal("92.47")
        assert call_count["n"] == 0, "API should not have been called on fresh cache hit"
    finally:
        await eng.aclose()


@pytest.mark.asyncio
async def test_stale_cache_fallback_when_api_fails(session):
    """If cache is stale and the API call fails, we return the stale row."""
    stale_time = datetime.utcnow() - timedelta(hours=25)
    session.add(CmsRateCache(
        hcpcs="99213", modifier="", locality="14", year=2023,
        non_facility_rate=Decimal("90.00"),
        cached_at=stale_time,
        cached_until=stale_time + timedelta(hours=24),  # expired
    ))
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "14", 2023)
        assert row is not None
        assert row.non_facility_rate == Decimal("90.00")  # stale but returned
    finally:
        await eng.aclose()


@pytest.mark.asyncio
async def test_api_empty_result_and_no_cache_returns_none(session):
    """No cache + empty API response → None (not an exception)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "XXXXX", "", "14", 2023)
        assert row is None
    finally:
        await eng.aclose()


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache(session):
    """force_refresh=True should call the API even when cache is fresh."""
    session.add(CmsRateCache(
        hcpcs="99213", modifier="", locality="14", year=2023,
        non_facility_rate=Decimal("50.00"),  # stale-looking value
        cached_at=datetime.utcnow(),
        cached_until=datetime.utcnow() + timedelta(hours=1),
    ))
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "HCPCS_CD": "99213", "LOCALITY_NUM": "14", "YEAR": "2023",
            "NON_FAC_PRICE": "99.99",
        }])

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "14", 2023, force_refresh=True)
        assert row is not None
        assert row.non_facility_rate == Decimal("99.99")  # refreshed
    finally:
        await eng.aclose()


@pytest.mark.asyncio
async def test_zip_normalization():
    """ZIP+4 formats reduce to their 5-digit base; non-numeric returns empty."""
    assert CMSFeeScheduleEngine.normalize_zip("11201") == "11201"
    assert CMSFeeScheduleEngine.normalize_zip("11201-1234") == "11201"
    assert CMSFeeScheduleEngine.normalize_zip(" 10016-4212 ") == "10016"
    assert CMSFeeScheduleEngine.normalize_zip("") == ""
    assert CMSFeeScheduleEngine.normalize_zip("ABCDE") == ""


@pytest.mark.asyncio
async def test_zip_to_locality_lookup(session):
    """Seed a ZIP row and confirm the engine returns the locality."""
    session.add(ZipLocality(
        zip_code="11201", state="NY", carrier_number="13282",
        locality_number="01", locality_name="NYC",
        effective_year=2025,
    ))
    await session.commit()

    eng = CMSFeeScheduleEngine()
    try:
        loc = await eng.get_locality_from_zip(session, "11201-1234")
        assert loc == "01"
        # Miss should return None
        loc2 = await eng.get_locality_from_zip(session, "99999")
        assert loc2 is None
    finally:
        await eng.aclose()
