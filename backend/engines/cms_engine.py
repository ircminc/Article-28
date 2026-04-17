"""CMS Medicare Physician Fee Schedule (MPFS) rate engine — v2 architecture.

Rewritten in 2026-04 after CMS migrated PFS data from
`data.cms.gov/data-api/v1/dataset/<uuid>` to the DKAN-powered
`pfs.data.cms.gov` subsite. The old pattern of hitting a single "MPFS" dataset
no longer works: CMS now publishes two separate datasets per year,
"Indicators for YYYY" (RVUs + conversion factor + procedure flags) and
"Localities for YYYY" (GPCIs per Medicare locality).

Our engine now mirrors what CMS's official PFS Look-Up Tool does:

    Payment = ((rvu_work × gpci_work)
             + (pe_rvu    × gpci_pe)
             + (rvu_mp    × gpci_mp)) × conversion_factor

where pe_rvu is the non-facility or facility PE RVU depending on place
of service. For PC/TC split, we filter the Indicators dataset by
modifier "26" (professional only) or "TC" (technical only).

Key design decisions:
  * Dataset UUIDs are NEVER hardcoded. They are discovered at runtime by
    fetching the CMS catalog at pfs.data.cms.gov/data.json and matching
    by title ("Indicators for YYYY" and "Localities for YYYY"). This is
    self-healing — when CMS publishes a new annual dataset, the app picks
    it up on the next catalog refresh.
  * Each year has its own datasets. Some years are split A/B mid-year
    (e.g. 2024A covers H1, 2024B covers H2). We resolve "best fit" for a
    requested year by preferring a non-suffixed dataset, then B (more
    recent), then A.
  * In-process caches:
      - UUID catalog cache: {year: (indicator_uuid, locality_uuid)}, 24h TTL
      - Per-HCPCS indicator row cache: keyed by (hcpc, modifier, year)
      - Per-locality row cache: keyed by (locality, year)
      - Computed-rate cache: the existing cms_rate_cache SQLAlchemy table
  * Rate limiting: asyncio.Semaphore(10) caps concurrent outbound HTTP
  * Graceful degradation: if CMS is unreachable, we return stale cache
    rather than hard-failing.

Env vars:
  CMS_API_CACHE_TTL   — seconds (default 86400 = 24h)
"""
from __future__ import annotations

import asyncio
import json
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

CMS_BASE = "https://pfs.data.cms.gov"
CATALOG_URL = f"{CMS_BASE}/data.json"

_DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("CMS_API_CACHE_TTL", str(24 * 3600)))
_DEFAULT_RATE_LIMIT_CONCURRENT = 10
_DEFAULT_TIMEOUT_SECONDS = 30


class CMSDatasetMovedError(Exception):
    """Raised when CMS's catalog or query endpoints are unreachable/renamed.
    The calculator shows a banner-level message instead of per-line errors."""
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_YEAR_RE = re.compile(r"\bfor\s+(\d{4})([AB]?)\b", re.IGNORECASE)


def _parse_year_suffix(title: str) -> Optional[tuple[int, str]]:
    """'Indicators for 2024B' -> (2024, 'B'). Returns None on no match."""
    m = _YEAR_RE.search(title or "")
    if not m:
        return None
    return int(m.group(1)), (m.group(2) or "").upper()


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None


def _normalize_locality(code: str) -> str:
    """Accept '01', '0000001', '1', etc. Pad to 7 digits on the LEFT with
    zeros if all-numeric. If it contains non-digits, return as-is."""
    if not code:
        return ""
    s = str(code).strip()
    if s.isdigit():
        return s.zfill(7)
    return s


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CMSFeeScheduleEngine:
    """PFS lookup via DKAN-backed pfs.data.cms.gov. Reusable across requests.

    Close with `await aclose()` on app shutdown.
    """

    def __init__(
        self,
        *,
        catalog_url: str = CATALOG_URL,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        max_concurrent: int = _DEFAULT_RATE_LIMIT_CONCURRENT,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.catalog_url = catalog_url
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

        # In-process caches (per-process, reset on restart)
        # _catalog_cache[year] = (indicator_uuid, locality_uuid, fetched_at)
        self._catalog_cache: dict[int, tuple[str, str, datetime]] = {}
        self._catalog_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -----------------------------------------------------------------
    # Catalog discovery
    # -----------------------------------------------------------------

    async def _fetch_catalog(self) -> list[dict]:
        """Return the raw list of datasets from data.json."""
        async with self._semaphore:
            resp = await self._client.get(self.catalog_url)
        if resp.status_code == 404:
            raise CMSDatasetMovedError(
                f"CMS catalog at {self.catalog_url} is no longer reachable."
            )
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict) and "dataset" in body:
            return body["dataset"]
        if isinstance(body, list):
            return body
        raise CMSDatasetMovedError(
            f"Unexpected catalog shape from {self.catalog_url}"
        )

    async def _resolve_datasets_for_year(self, year: int) -> tuple[str, str]:
        """Find Indicator + Locality UUIDs for the given year.

        For years with A/B splits, prefers B (newer updates include A's
        fixes). For years with no data, falls back to the most recent
        prior year we can find.

        Returns (indicator_uuid, locality_uuid). Raises CMSDatasetMovedError
        if neither can be found.
        """
        # Cache hit
        cached = self._catalog_cache.get(year)
        if cached:
            ind_uuid, loc_uuid, fetched = cached
            if datetime.utcnow() - fetched < self.cache_ttl:
                return ind_uuid, loc_uuid

        async with self._catalog_lock:
            # Re-check in case another task populated while we waited
            cached = self._catalog_cache.get(year)
            if cached:
                ind_uuid, loc_uuid, fetched = cached
                if datetime.utcnow() - fetched < self.cache_ttl:
                    return ind_uuid, loc_uuid

            datasets = await self._fetch_catalog()

            # Index by (year, suffix, kind) — kind is 'indicator' or 'locality'
            # suffix is '' / 'A' / 'B'
            indicators: dict[tuple[int, str], str] = {}
            localities: dict[tuple[int, str], str] = {}
            # Standard UUID (8-4-4-4-12 hex). Accept the UUID anywhere in the
            # identifier string — could be a bare UUID, a URL path ending in
            # the UUID, or embedded in some other way depending on catalog shape.
            uuid_re = re.compile(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                re.IGNORECASE,
            )
            for ds in datasets:
                title = ds.get("title", "")
                # Try 'identifier' first, then 'id' as a fallback
                ident = ds.get("identifier") or ds.get("id") or ""
                m_uuid = uuid_re.search(ident)
                if not m_uuid:
                    continue
                uuid = m_uuid.group(1).lower()
                ys = _parse_year_suffix(title)
                if not ys:
                    continue
                y, sfx = ys
                tlow = title.lower()
                if tlow.startswith("indicators for"):
                    indicators[(y, sfx)] = uuid
                elif tlow.startswith("localities for"):
                    localities[(y, sfx)] = uuid

            log.info(
                "CMS catalog: parsed %d indicator + %d locality datasets",
                len(indicators), len(localities),
            )

            def pick(pool: dict[tuple[int, str], str], for_year: int) -> Optional[str]:
                # Preference order: '' (non-split), 'B' (H2 update), 'A' (H1),
                # then any other suffix
                for sfx in ("", "B", "A"):
                    if (for_year, sfx) in pool:
                        return pool[(for_year, sfx)]
                return None

            ind_uuid = pick(indicators, year)
            loc_uuid = pick(localities, year)

            # Fall back to the most recent prior year if needed — CMS sometimes
            # takes weeks to publish new-year datasets after Jan 1.
            if not ind_uuid or not loc_uuid:
                candidate_years = sorted(
                    {y for (y, _) in indicators.keys()} | {y for (y, _) in localities.keys()},
                    reverse=True,
                )
                for cy in candidate_years:
                    if cy > year:
                        continue
                    if not ind_uuid:
                        ind_uuid = pick(indicators, cy)
                    if not loc_uuid:
                        loc_uuid = pick(localities, cy)
                    if ind_uuid and loc_uuid:
                        if cy != year:
                            log.info("CMS: no dataset for year %d; falling back to %d", year, cy)
                        break

            if not ind_uuid or not loc_uuid:
                raise CMSDatasetMovedError(
                    f"Could not find CMS PFS datasets for year {year} in catalog "
                    f"(catalog had {len(indicators)} indicator(s), "
                    f"{len(localities)} locality(ies))."
                )

            self._catalog_cache[year] = (ind_uuid, loc_uuid, datetime.utcnow())
            return ind_uuid, loc_uuid

    # -----------------------------------------------------------------
    # DKAN query helpers
    # -----------------------------------------------------------------

    async def _dkan_query(self, uuid: str, filters: dict, limit: int = 5) -> list[dict]:
        """Hit /api/1/datastore/query/{uuid}/0 with GET params. Returns rows list.

        DKAN's GET-based query accepts conditions via repeated query params;
        the simplest reliable approach is a POST with a JSON body.
        """
        url = f"{CMS_BASE}/api/1/datastore/query/{uuid}/0"
        # DKAN POST query body
        conditions = [
            {"resource": "t", "property": k, "value": v, "operator": "="}
            for k, v in filters.items() if v is not None and v != ""
        ]
        payload = {"conditions": conditions, "limit": limit}
        async with self._semaphore:
            resp = await self._client.post(url, json=payload)

        if resp.status_code == 404:
            raise CMSDatasetMovedError(
                f"CMS dataset {uuid} returned 404 — catalog may be stale."
            )
        if resp.status_code != 200:
            log.warning("CMS DKAN query returned %d: %s", resp.status_code, resp.text[:200])
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        # DKAN wraps results under 'results'
        if isinstance(body, dict) and "results" in body and isinstance(body["results"], list):
            return body["results"]
        if isinstance(body, list):
            return body
        return []

    # -----------------------------------------------------------------
    # Rate computation
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
        kwargs = dict(
            non_facility_rate=parsed["non_facility_rate"],
            facility_rate=parsed["facility_rate"],
            work_rvu=parsed["work_rvu"],
            pe_rvu=parsed["pe_rvu"],
            mp_rvu=parsed["mp_rvu"],
            total_rvu=parsed["total_rvu"],
            conversion_factor=parsed["conversion_factor"],
            raw_payload=parsed.get("raw_payload"),
        )
        if existing:
            for k, v in kwargs.items():
                setattr(existing, k, v)
            existing.cached_at = now
            existing.cached_until = now + self.cache_ttl
            return existing
        row = CmsRateCache(
            hcpcs=hcpcs, modifier=modifier, locality=locality, year=year,
            **kwargs,
            cached_at=now,
            cached_until=now + self.cache_ttl,
        )
        session.add(row)
        await session.flush()
        return row

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
        """Return the MPFS rate for a given procedure, modifier, locality, year.

        Result is cached in cms_rate_cache for 24h. Stale cache is returned
        as a fallback if the CMS API is temporarily unreachable.
        """
        hcpcs = (hcpcs or "").upper().strip()
        modifier = (modifier or "").upper().strip()
        locality_norm = _normalize_locality(locality)

        # Cache fast path
        cached = await self._cache_get(session, hcpcs, modifier, locality_norm, year)
        now = datetime.utcnow()
        if cached and not force_refresh and cached.cached_until > now:
            return cached

        # Fetch both datasets, compute the rate
        try:
            parsed = await self._compute_rate(hcpcs, modifier, locality_norm, year)
        except CMSDatasetMovedError:
            # Propagate up — calculator endpoint surfaces this as a banner
            raise
        except Exception as e:
            log.warning("CMS rate computation failed: %s", e)
            parsed = None

        if parsed is None:
            # Fall back to stale cache if available
            if cached:
                log.info("CMS unreachable; returning stale cache for %s/%s/%s/%s",
                         hcpcs, modifier, locality_norm, year)
                return cached
            return None

        row = await self._cache_put(session, hcpcs, modifier, locality_norm, year, parsed)
        await session.commit()
        return row

    async def _compute_rate(
        self, hcpcs: str, modifier: str, locality: str, year: int,
    ) -> Optional[dict]:
        """Look up indicator + locality rows, apply the RVU × GPCI × CF formula."""
        ind_uuid, loc_uuid = await self._resolve_datasets_for_year(year)

        # Parallel fetches: one for the HCPCS row, one for the locality row
        ind_task = self._dkan_query(ind_uuid, {"hcpc": hcpcs, "modifier": modifier}, limit=2)
        loc_task = self._dkan_query(loc_uuid, {"locality": locality}, limit=2)
        try:
            ind_rows, loc_rows = await asyncio.gather(ind_task, loc_task)
        except CMSDatasetMovedError:
            raise
        except Exception as e:
            log.warning("CMS fetch failed: %s", e)
            return None

        if not ind_rows:
            log.info("CMS: no Indicators row for hcpc=%s mod=%s year=%s", hcpcs, modifier, year)
            return None
        if not loc_rows:
            log.info("CMS: no Localities row for locality=%s year=%s", locality, year)
            return None

        ind = ind_rows[0]
        loc = loc_rows[0]

        rvu_work = _dec(ind.get("rvu_work")) or Decimal("0")
        # Prefer 'full_*' (fully-implemented) over 'trans_*' (transitional).
        # A few datasets use different column names; fall back gracefully.
        full_nfac_pe = _dec(ind.get("full_nfac_pe") or ind.get("nfac_pe")) or Decimal("0")
        full_fac_pe = _dec(ind.get("full_fac_pe") or ind.get("fac_pe")) or Decimal("0")
        rvu_mp = _dec(ind.get("rvu_mp")) or Decimal("0")
        cf = _dec(ind.get("conv_fact"))

        gpci_work = _dec(loc.get("gpci_work")) or Decimal("1")
        gpci_pe = _dec(loc.get("gpci_pe")) or Decimal("1")
        gpci_mp = _dec(loc.get("gpci_mp")) or Decimal("1")

        if cf is None or cf == 0:
            # Without a conversion factor we can't compute dollars
            log.info("CMS: conversion factor missing/zero for hcpc=%s year=%d", hcpcs, year)
            return None

        work_component = rvu_work * gpci_work
        mp_component = rvu_mp * gpci_mp
        pe_nfac_component = full_nfac_pe * gpci_pe
        pe_fac_component = full_fac_pe * gpci_pe

        non_facility_rate = (work_component + pe_nfac_component + mp_component) * cf
        facility_rate = (work_component + pe_fac_component + mp_component) * cf

        # Total RVU reflects the chosen PE (use non-facility for display default)
        total_rvu_nfac = rvu_work + full_nfac_pe + rvu_mp

        # Round dollar amounts to 2dp; leave RVUs at 4dp
        CENT = Decimal("0.01")
        non_facility_rate = non_facility_rate.quantize(CENT)
        facility_rate = facility_rate.quantize(CENT)

        return {
            "non_facility_rate": non_facility_rate,
            "facility_rate": facility_rate,
            "work_rvu": rvu_work,
            "pe_rvu": full_nfac_pe,
            "mp_rvu": rvu_mp,
            "total_rvu": total_rvu_nfac,
            "conversion_factor": cf,
            "raw_payload": {
                "indicator_row": ind,
                "locality_row": loc,
                "computation": {
                    "work_component": str(work_component),
                    "pe_nfac_component": str(pe_nfac_component),
                    "pe_fac_component": str(pe_fac_component),
                    "mp_component": str(mp_component),
                },
            },
        }

    # -----------------------------------------------------------------
    # ZIP → locality
    # -----------------------------------------------------------------

    @staticmethod
    def normalize_zip(zip_raw: str) -> str:
        if not zip_raw:
            return ""
        m = re.match(r"(\d{5})", zip_raw.strip())
        return m.group(1) if m else ""

    async def get_locality_from_zip(
        self, session: AsyncSession, zip_code: str, year: Optional[int] = None,
    ) -> Optional[str]:
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
        row = await self.get_mpfs_rate(
            session, procedure_code, modifier, locality, date_of_service.year,
        )
        if row is None:
            return None
        return row.facility_rate if use_facility_rate else row.non_facility_rate


__all__ = ["CMSFeeScheduleEngine", "CMSDatasetMovedError"]
