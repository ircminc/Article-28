"""CMS Medicare Physician Fee Schedule (MPFS) rate engine.

Purpose: for non-Article 28 (professional) claims, look up the correct Medicare
MPFS reimbursement for a given procedure code × modifier × locality × year,
and compare it to the actual paid amount.

Data source: CMS public data API at https://data.cms.gov
  - Dataset: Medicare Physician Fee Schedule
  - Dataset ID: 9767cb68-8ea9-4f0b-8179-9a7a94480c2f
  - Endpoint: /data-api/v1/dataset/{dataset_id}/data?filter[HCPCS_CD]=...

Caching: results are written to the `cms_rate_cache` table with a configurable
TTL (default 24h). Stale rows are still returned as a fallback if the API is
unreachable — a medical billing tool should not silently error when the upstream
rate service has a hiccup.

Rate limiting: an asyncio semaphore caps concurrent requests at 10, and a small
sleep between batches keeps us under ~10 requests/second. For backfill-style
usage patterns, prefer the batch helpers.

ZIP → locality: provider configuration carries a `cms_locality` field that
callers should set from the `zip_locality` table (populated separately from the
annual CMS ZIP5 file).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import CmsRateCache, ZipLocality

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CMS_DATASET_ID = "9767cb68-8ea9-4f0b-8179-9a7a94480c2f"
CMS_BASE_URL = f"https://data.cms.gov/data-api/v1/dataset/{CMS_DATASET_ID}/data"

# Default TTL matches the spec (24h). Override via env or constructor.
_DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("CMS_API_CACHE_TTL", str(24 * 3600)))
_DEFAULT_RATE_LIMIT_CONCURRENT = 10
_DEFAULT_TIMEOUT_SECONDS = 20


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def _dec(raw) -> Optional[Decimal]:
    """Convert the CMS JSON string fields (always strings) to Decimal."""
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None


def _first_present(obj: dict, keys: list[str]) -> Optional[str]:
    """Return the first non-empty value among the given keys.

    CMS has historically renamed columns across dataset revisions
    (HCPCS_CD vs HCPCS, WORK_RVU vs RVU_WORK, etc.). This helper tolerates that.
    """
    for k in keys:
        v = obj.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _parse_cms_record(rec: dict) -> dict:
    """Normalize one CMS dataset row into our internal flat shape."""
    non_facility = _first_present(rec, ["NON_FAC_PRICE", "NON_FACILITY_PRICE", "NON_FAC_PAYMENT"])
    facility = _first_present(rec, ["FACILITY_PRICE", "FAC_PRICE", "FACILITY_PAYMENT"])
    work = _first_present(rec, ["WORK_RVU", "RVU_WORK"])
    pe = _first_present(rec, ["NON_FAC_PE_RVU", "PE_RVU", "RVU_PE"])
    mp = _first_present(rec, ["MP_RVU", "RVU_MP"])
    total = _first_present(rec, ["NON_FAC_TOTAL_RVU", "TOTAL_RVU", "RVU_TOTAL"])
    cf = _first_present(rec, ["CONV_FACTOR", "CONVERSION_FACTOR"])
    return {
        "non_facility_rate": _dec(non_facility),
        "facility_rate": _dec(facility),
        "work_rvu": _dec(work),
        "pe_rvu": _dec(pe),
        "mp_rvu": _dec(mp),
        "total_rvu": _dec(total),
        "conversion_factor": _dec(cf),
        "raw_payload": rec,
    }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CMSFeeScheduleEngine:
    """CMS MPFS lookup with read-through SQLite cache.

    Typical usage:
        engine = CMSFeeScheduleEngine()
        rate = await engine.get_mpfs_rate(session, "99213", "", "14", 2023)
        expected = await engine.calculate_expected_payment(service_line, "14", dos)

    The engine is reusable; one instance per app is fine. Close via `await engine.aclose()`
    when shutting down the application.
    """

    def __init__(
        self,
        *,
        base_url: str = CMS_BASE_URL,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        max_concurrent: int = _DEFAULT_RATE_LIMIT_CONCURRENT,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Allow dependency injection for tests (e.g. httpx.MockTransport)
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -----------------------------------------------------------------
    # Cache helpers
    # -----------------------------------------------------------------

    async def _cache_get(
        self, session: AsyncSession, hcpcs: str, modifier: str, locality: str, year: int,
    ) -> Optional[CmsRateCache]:
        stmt = select(CmsRateCache).where(
            CmsRateCache.hcpcs == hcpcs,
            CmsRateCache.modifier == modifier,
            CmsRateCache.locality == locality,
            CmsRateCache.year == year,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    async def _cache_put(
        self,
        session: AsyncSession,
        hcpcs: str, modifier: str, locality: str, year: int,
        parsed: dict,
    ) -> CmsRateCache:
        now = datetime.utcnow()
        existing = await self._cache_get(session, hcpcs, modifier, locality, year)
        if existing:
            existing.non_facility_rate = parsed["non_facility_rate"]
            existing.facility_rate = parsed["facility_rate"]
            existing.work_rvu = parsed["work_rvu"]
            existing.pe_rvu = parsed["pe_rvu"]
            existing.mp_rvu = parsed["mp_rvu"]
            existing.total_rvu = parsed["total_rvu"]
            existing.conversion_factor = parsed["conversion_factor"]
            existing.raw_payload = parsed["raw_payload"]
            existing.cached_at = now
            existing.cached_until = now + self.cache_ttl
            return existing
        row = CmsRateCache(
            hcpcs=hcpcs, modifier=modifier, locality=locality, year=year,
            non_facility_rate=parsed["non_facility_rate"],
            facility_rate=parsed["facility_rate"],
            work_rvu=parsed["work_rvu"],
            pe_rvu=parsed["pe_rvu"],
            mp_rvu=parsed["mp_rvu"],
            total_rvu=parsed["total_rvu"],
            conversion_factor=parsed["conversion_factor"],
            raw_payload=parsed["raw_payload"],
            cached_at=now,
            cached_until=now + self.cache_ttl,
        )
        session.add(row)
        await session.flush()
        return row

    # -----------------------------------------------------------------
    # HTTP
    # -----------------------------------------------------------------

    async def _fetch_from_api(
        self, hcpcs: str, modifier: str, locality: str, year: int,
    ) -> Optional[dict]:
        """Call the CMS API and return the normalized first-match record, or None."""
        params = {
            "filter[HCPCS_CD]": hcpcs,
            "filter[LOCALITY_NUM]": locality,
            "filter[YEAR]": str(year),
        }
        if modifier:
            params["filter[MODIFIER]"] = modifier

        async with self._semaphore:
            try:
                resp = await self._client.get(self.base_url, params=params)
            except httpx.RequestError as e:
                log.warning("CMS API request failed: %s", e)
                return None

        if resp.status_code != 200:
            log.warning("CMS API non-200 status %s for %s", resp.status_code, params)
            return None

        try:
            data = resp.json()
        except ValueError:
            log.warning("CMS API returned non-JSON")
            return None

        # The dataset API returns a list of records directly.
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict) and "data" in data:
            records = data["data"]
        else:
            records = []

        if not records:
            log.info(
                "CMS API returned no rows for HCPCS=%s mod=%s locality=%s year=%d",
                hcpcs, modifier, locality, year,
            )
            return None

        # If multiple records match, prefer the first. The caller can pass MODIFIER
        # to narrow; global/unmodified rates come back without a modifier row.
        return _parse_cms_record(records[0])

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def get_mpfs_rate(
        self,
        session: AsyncSession,
        hcpcs: str,
        modifier: str,
        locality: str,
        year: int,
        *,
        force_refresh: bool = False,
    ) -> Optional[CmsRateCache]:
        """Fetch a cached or live CMS MPFS rate.

        Returns a CmsRateCache row (guaranteed fresh within TTL, or stale fallback)
        or None if neither cache nor API had data.
        """
        hcpcs = hcpcs.upper().strip()
        modifier = (modifier or "").upper().strip()

        cached = await self._cache_get(session, hcpcs, modifier, locality, year)
        now = datetime.utcnow()
        if cached and not force_refresh and cached.cached_until > now:
            log.debug("CMS cache hit (fresh) for %s mod=%s loc=%s yr=%d", hcpcs, modifier, locality, year)
            return cached

        parsed = await self._fetch_from_api(hcpcs, modifier, locality, year)
        if parsed is None:
            # Fall back to stale cache if present
            if cached:
                log.info(
                    "CMS API unreachable/empty; returning stale cache (cached_at=%s) for %s",
                    cached.cached_at, hcpcs,
                )
                return cached
            return None

        row = await self._cache_put(session, hcpcs, modifier, locality, year, parsed)
        await session.commit()
        return row

    # -----------------------------------------------------------------
    # ZIP → locality
    # -----------------------------------------------------------------

    @staticmethod
    def normalize_zip(zip_raw: str) -> str:
        """Strip formatting and ZIP+4 down to the 5-digit base."""
        if not zip_raw:
            return ""
        m = re.match(r"(\d{5})", zip_raw.strip())
        return m.group(1) if m else ""

    async def get_locality_from_zip(
        self, session: AsyncSession, zip_code: str, year: Optional[int] = None,
    ) -> Optional[str]:
        """Resolve a ZIP code to a Medicare locality number via the `zip_locality` table.

        If the table is empty, returns None — the caller should surface a setup error.
        """
        zip5 = self.normalize_zip(zip_code)
        if not zip5:
            return None
        stmt = select(ZipLocality).where(ZipLocality.zip_code == zip5)
        if year is not None:
            stmt = stmt.where(ZipLocality.effective_year <= year).order_by(
                ZipLocality.effective_year.desc()
            )
        else:
            stmt = stmt.order_by(ZipLocality.effective_year.desc().nullslast())
        stmt = stmt.limit(1)
        res = await session.execute(stmt)
        row = res.scalar_one_or_none()
        return row.locality_number if row else None

    # -----------------------------------------------------------------
    # Payment comparison
    # -----------------------------------------------------------------

    async def calculate_expected_payment(
        self,
        session: AsyncSession,
        procedure_code: str,
        modifier: str,
        locality: str,
        date_of_service: date,
        *,
        use_facility_rate: bool = False,
    ) -> Optional[Decimal]:
        """Return the expected MPFS payment for a single service line.

        - Chooses the non-facility or facility rate based on `use_facility_rate`.
        - Uses the year of DOS for the cache / API lookup.
        """
        year = date_of_service.year
        row = await self.get_mpfs_rate(session, procedure_code, modifier, locality, year)
        if row is None:
            return None
        rate = row.facility_rate if use_facility_rate else row.non_facility_rate
        return rate
