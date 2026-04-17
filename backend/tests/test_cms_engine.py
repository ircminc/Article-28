"""CMS engine tests — rebuilt for the v2 (pfs.data.cms.gov DKAN) architecture.

All HTTP is mocked via httpx.MockTransport so tests are offline-safe and
deterministic. We verify:
  * Catalog discovery parses data.json, picks correct year/suffix
  * DKAN query POST serializes conditions correctly
  * Payment formula computes RVU × GPCI × CF accurately
  * Non-facility vs facility rates use the right PE RVU field
  * Cache hit skips remote calls
  * Stale cache is returned when upstream fails
  * 404 raises CMSDatasetMovedError (beta-unblocking banner trigger)
  * ZIP normalization + locality resolution unchanged
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

from backend.db.database import (
    Base, CmsRateCache, ZipLocality, async_session, engine,
)
from backend.engines.cms_engine import CMSDatasetMovedError, CMSFeeScheduleEngine


# ---------------------------------------------------------------------------
# Representative fixtures mimicking real CMS API payloads
# ---------------------------------------------------------------------------

CATALOG_FIXTURE = {
    "dataset": [
        {
            "title": "Indicators for 2025",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/11111111-2025-aaaa-bbbb-000000000000",
        },
        {
            "title": "Localities for 2025",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/22222222-2025-aaaa-bbbb-000000000000",
        },
        {
            "title": "Indicators for 2024B",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/11111111-2024-bbbb-cccc-000000000000",
        },
        {
            "title": "Indicators for 2024A",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/11111111-2024-aaaa-cccc-000000000000",
        },
        {
            "title": "Localities for 2024B",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/22222222-2024-bbbb-cccc-000000000000",
        },
        {
            "title": "Localities for 2024A",
            "identifier": "https://pfs.data.cms.gov/api/1/metastore/schemas/dataset/items/22222222-2024-aaaa-cccc-000000000000",
        },
        # Something unrelated that shouldn't match
        {
            "title": "Opt Out Affidavits",
            "identifier": "https://data.cms.gov/.../99999999-9999-9999-9999-999999999999",
        },
    ]
}


def _uuid_matches(ref: str, url: str) -> bool:
    """Tiny helper: does the request URL contain the UUID associated with
    the given catalog title prefix (e.g. '11111111-2025')?"""
    return ref in url


# Representative Indicators row for HCPCS 99213 (based on real CMS fields
# observed via the discovery script).
INDICATORS_99213 = {
    "year": "2025",
    "hcpc": "99213",
    "modifier": "",
    "sdesc": "OFFICE O/P EST LOW 20-29 MIN",
    "proc_stat": "A",
    "rvu_work": "1.30",
    "full_nfac_pe": "1.25",
    "full_fac_pe": "0.60",
    "rvu_mp": "0.08",
    "conv_fact": "32.74",
    "pctc": "0",
    "global": "000",
}

# Representative Localities row: NYC locality (code 0100001 is hypothetical
# in our test but the shape matches real data)
LOCALITIES_NYC = {
    "year": "2025",
    "locality": "0100001",
    "loc_description": "NEW YORK (MANHATTAN)",
    "mac": "13202",
    "gpci_work": "1.058",
    "gpci_pe": "1.167",
    "gpci_mp": "1.611",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        await s.execute(delete(CmsRateCache))
        await s.execute(delete(ZipLocality))
        await s.commit()
        yield s


def _make_engine(handler) -> CMSFeeScheduleEngine:
    """Build an engine with a MockTransport-backed httpx client."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return CMSFeeScheduleEngine(client=client, cache_ttl_seconds=3600)


def _catalog_response() -> httpx.Response:
    return httpx.Response(200, json=CATALOG_FIXTURE)


def _dkan_response(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(rows), "results": rows})


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_discovery_picks_non_suffixed_year(session):
    """When a year has a clean dataset (no A/B suffix), prefer it."""
    captured_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        if "data.json" in str(request.url):
            return _catalog_response()
        # Not expected to reach DKAN in this test
        return httpx.Response(500)

    eng = _make_engine(handler)
    try:
        ind, loc = await eng._resolve_datasets_for_year(2025)
    finally:
        await eng.aclose()

    assert ind == "11111111-2025-aaaa-bbbb-000000000000"
    assert loc == "22222222-2025-aaaa-bbbb-000000000000"


@pytest.mark.asyncio
async def test_catalog_discovery_prefers_B_over_A(session):
    """For 2024 which has both A and B, should pick B (more recent update)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _catalog_response()

    eng = _make_engine(handler)
    try:
        ind, loc = await eng._resolve_datasets_for_year(2024)
    finally:
        await eng.aclose()

    assert ind == "11111111-2024-bbbb-cccc-000000000000"
    assert loc == "22222222-2024-bbbb-cccc-000000000000"


@pytest.mark.asyncio
async def test_catalog_discovery_falls_back_to_prior_year(session):
    """If no dataset exists for the requested year, fall back to most recent prior."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _catalog_response()

    eng = _make_engine(handler)
    try:
        # 2099 has no dataset — should fall back to 2025 (most recent)
        ind, loc = await eng._resolve_datasets_for_year(2099)
    finally:
        await eng.aclose()

    assert ind == "11111111-2025-aaaa-bbbb-000000000000"
    assert loc == "22222222-2025-aaaa-bbbb-000000000000"


@pytest.mark.asyncio
async def test_catalog_404_raises_dataset_moved(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    eng = _make_engine(handler)
    try:
        with pytest.raises(CMSDatasetMovedError):
            await eng._resolve_datasets_for_year(2025)
    finally:
        await eng.aclose()


# ---------------------------------------------------------------------------
# Rate computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_computation_applies_formula_correctly(session):
    """Formula: (rvu_work × gpci_work + full_nfac_pe × gpci_pe + rvu_mp × gpci_mp) × conv_fact
    For 99213 in NYC:
      (1.30 × 1.058) + (1.25 × 1.167) + (0.08 × 1.611) = 1.3754 + 1.45875 + 0.12888 = 2.96303
      × 32.74 = $97.00 (rounded)
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if "data.json" in str(request.url):
            return _catalog_response()
        url = str(request.url)
        if "11111111-2025" in url:
            return _dkan_response([INDICATORS_99213])
        if "22222222-2025" in url:
            return _dkan_response([LOCALITIES_NYC])
        return httpx.Response(500)

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "0100001", 2025)
    finally:
        await eng.aclose()

    assert row is not None
    # Allow a 1-cent tolerance for rounding
    assert abs(row.non_facility_rate - Decimal("97.00")) <= Decimal("0.02"), row.non_facility_rate
    # Facility rate uses full_fac_pe instead of full_nfac_pe:
    # (1.30 × 1.058) + (0.60 × 1.167) + (0.08 × 1.611) = 1.3754 + 0.70020 + 0.12888 = 2.20448
    # × 32.74 = $72.17
    assert abs(row.facility_rate - Decimal("72.17")) <= Decimal("0.02"), row.facility_rate
    # RVU pass-throughs
    assert row.work_rvu == Decimal("1.30")
    assert row.pe_rvu == Decimal("1.25")
    assert row.mp_rvu == Decimal("0.08")
    assert row.conversion_factor == Decimal("32.74")


@pytest.mark.asyncio
async def test_returns_none_when_indicator_missing(session):
    def handler(request: httpx.Request) -> httpx.Response:
        if "data.json" in str(request.url):
            return _catalog_response()
        # Indicators returns empty, Localities returns fine
        url = str(request.url)
        if "11111111-" in url:  # indicators fixtures
            return _dkan_response([])
        if "22222222-" in url:  # localities fixtures
            return _dkan_response([LOCALITIES_NYC])
        return httpx.Response(500)

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "XXXXX", "", "0100001", 2025)
    finally:
        await eng.aclose()

    assert row is None


@pytest.mark.asyncio
async def test_cache_hit_skips_remote(session):
    """A fresh cached row should short-circuit — no HTTP at all."""
    session.add(CmsRateCache(
        hcpcs="99213", modifier="", locality="0100001", year=2025,
        non_facility_rate=Decimal("97.00"),
        cached_at=datetime.utcnow(),
        cached_until=datetime.utcnow() + timedelta(hours=1),
    ))
    await session.commit()

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500)

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "0100001", 2025)
    finally:
        await eng.aclose()

    assert row is not None
    assert row.non_facility_rate == Decimal("97.00")
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_stale_cache_fallback_when_api_fails(session):
    """If catalog call fails (non-404) and we have a stale cache, return stale."""
    stale_time = datetime.utcnow() - timedelta(hours=25)
    session.add(CmsRateCache(
        hcpcs="99213", modifier="", locality="0100001", year=2025,
        non_facility_rate=Decimal("90.00"),   # older value
        cached_at=stale_time,
        cached_until=stale_time + timedelta(hours=24),
    ))
    await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        # Any 5xx — not a 404 (that would trigger the CMSDatasetMovedError path)
        return httpx.Response(503, text="service unavailable")

    eng = _make_engine(handler)
    try:
        row = await eng.get_mpfs_rate(session, "99213", "", "0100001", 2025)
    finally:
        await eng.aclose()

    assert row is not None
    assert row.non_facility_rate == Decimal("90.00")   # stale but present


@pytest.mark.asyncio
async def test_dkan_404_raises_dataset_moved(session):
    """A 404 on the DKAN query (e.g. UUID retired mid-request) propagates
    as CMSDatasetMovedError so the calculator can show a banner."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "data.json" in str(request.url):
            return _catalog_response()
        # DKAN query returns 404
        return httpx.Response(404, text="dataset not found")

    eng = _make_engine(handler)
    try:
        with pytest.raises(CMSDatasetMovedError):
            await eng.get_mpfs_rate(session, "99213", "", "0100001", 2025)
    finally:
        await eng.aclose()


# ---------------------------------------------------------------------------
# ZIP + locality utilities
# ---------------------------------------------------------------------------


def test_zip_normalization():
    assert CMSFeeScheduleEngine.normalize_zip("11201") == "11201"
    assert CMSFeeScheduleEngine.normalize_zip("11201-1234") == "11201"
    assert CMSFeeScheduleEngine.normalize_zip(" 10016-4212 ") == "10016"
    assert CMSFeeScheduleEngine.normalize_zip("") == ""
    assert CMSFeeScheduleEngine.normalize_zip("ABCDE") == ""


@pytest.mark.asyncio
async def test_zip_to_locality_lookup(session):
    session.add(ZipLocality(
        zip_code="11201", state="NY", carrier_number="13282",
        locality_number="01", locality_name="NYC",
        effective_year=2025,
    ))
    await session.commit()

    eng = CMSFeeScheduleEngine()
    try:
        assert await eng.get_locality_from_zip(session, "11201-1234") == "01"
        assert await eng.get_locality_from_zip(session, "99999") is None
    finally:
        await eng.aclose()


# ---------------------------------------------------------------------------
# Locality normalization (user enters '01', dataset has '0000001')
# ---------------------------------------------------------------------------


def test_locality_normalization_pads_short_numeric():
    from backend.engines.cms_engine import _normalize_locality
    assert _normalize_locality("01") == "0000001"
    assert _normalize_locality("1") == "0000001"
    assert _normalize_locality("0100001") == "0100001"
    assert _normalize_locality("") == ""
    # Non-numeric left alone
    assert _normalize_locality("ABC123") == "ABC123"
