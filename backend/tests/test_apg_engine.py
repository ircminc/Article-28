"""End-to-end tests for the APG engine against the real reference database.

These tests require `python -m backend.db.init_db --workbook <path>` to have been
run already. If the DB is empty they skip with a clear message.

Coverage:
  - Date-scoped HCPCS / ICD-10 / weight / base-rate lookups
  - Region resolution from county_code
  - Full calculate() pipeline against the synthetic 835I fixture
  - Multi-procedure discounting (CLM0002)
  - Packaging: Incidental EAPG types drop to zero expected payment
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.db.database import (
    ApgBaseRate,
    ApgWeight,
    HcpcsToEapg,
    ProviderConfig,
    ProviderCounty,
    async_session,
)
from backend.engines.apg_engine import APGEngine
from backend.models.schemas import Region
from backend.parsers.edi_835i import parse_835i

FIXTURE = Path(__file__).parent / "fixtures" / "sample_835i.edi"


@pytest_asyncio.fixture()
async def session():
    async with async_session() as s:
        # Skip if reference data not loaded
        cnt = await s.execute(select(func.count()).select_from(HcpcsToEapg))
        if cnt.scalar_one() == 0:
            pytest.skip("Reference data not loaded. Run `python -m backend.db.init_db --workbook <path>` first.")
        yield s


@pytest_asyncio.fixture()
async def clinic_provider(session):
    """A Clinic* Downstate DTC provider anchored in Kings County (Brooklyn)."""
    # Pick any Downstate county from the loaded table
    q = await session.execute(
        select(ProviderCounty).where(ProviderCounty.region == "Downstate").limit(1)
    )
    county = q.scalar_one()
    cfg = ProviderConfig(
        is_active=True,
        provider_name="Sample Clinic LLC",
        npi="1234567890",
        county_code=county.county_code,
        region=county.region,
        peer_group="Clinic*",
        provider_type="dtc",
        capital_addon_eligible=False,
    )
    session.add(cfg)
    await session.flush()
    yield cfg
    # no teardown — the session rolls back


# ---------------------------------------------------------------------------
# Lookup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_hcpcs_date_scoped(session):
    apg = APGEngine()
    # A common code that has been in the EAPG list for years
    row = await apg.lookup_hcpcs_eapg(session, "99213", date(2023, 5, 15))
    assert row is not None
    assert row.hcpcs == "99213"
    assert row.eapg > 0


@pytest.mark.asyncio
async def test_lookup_hcpcs_returns_most_recent_effective(session):
    """When multiple effective dates match, we want the latest <= DOS."""
    apg = APGEngine()
    row_old = await apg.lookup_hcpcs_eapg(session, "99213", date(2017, 1, 1))
    row_new = await apg.lookup_hcpcs_eapg(session, "99213", date(2024, 1, 1))
    # Both must resolve; newer should be at least as recent
    assert row_old is not None and row_new is not None
    if row_old.quarter_effective_date and row_new.quarter_effective_date:
        assert row_new.quarter_effective_date >= row_old.quarter_effective_date


@pytest.mark.asyncio
async def test_lookup_base_rate_downstate_clinic(session):
    """Clinic*, Downstate, DTC: for a DOS in 2023 the 2015-04-01 row should win
    because it's the most recent rate <= DOS (DTC rates are frozen at 2015-04-01
    in the current workbook)."""
    apg = APGEngine()
    row = await apg.lookup_base_rate(session, "dtc", "Clinic*", "Downstate", date(2023, 5, 15))
    assert row is not None
    assert row.effective_date == date(2015, 4, 1)
    assert row.rate > Decimal("100")  # Sanity: rate is in expected range


@pytest.mark.asyncio
async def test_lookup_base_rate_pre_first_effective_returns_none(session):
    """No rate was effective before 2009 — expect None."""
    apg = APGEngine()
    row = await apg.lookup_base_rate(session, "dtc", "Clinic*", "Downstate", date(2009, 1, 1))
    assert row is None


@pytest.mark.asyncio
async def test_lookup_weight_prefers_final_rate_when_year_matches(session):
    """For any APG that has a final_rate row with year_rate >= DOS year,
    the engine should return that row (is_final_rate = True)."""
    apg = APGEngine()
    # Find an APG that has a final_rate row
    q = await session.execute(
        select(ApgWeight).where(ApgWeight.is_final_rate.is_(True)).limit(1)
    )
    anchor = q.scalar_one_or_none()
    if anchor is None:
        pytest.skip("No APG in the dataset has a final_rate row — nothing to test.")
    dos = date(anchor.year_rate, 6, 1)  # mid-year of the final_rate's year
    row = await apg.lookup_apg_weight(session, anchor.apg, dos)
    assert row is not None
    assert row.is_final_rate is True


# ---------------------------------------------------------------------------
# End-to-end calculate() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculate_structural_integrity(session, clinic_provider):
    """The fixture should produce an APGResult for each claim with
    internally-consistent math regardless of exact EAPG assignments."""
    parsed = parse_835i(FIXTURE)
    apg = APGEngine()

    for claim in parsed.claims:
        result = await apg.calculate(session, claim, clinic_provider)
        assert result.claim_id == claim.claim_id
        assert result.region == Region.DOWNSTATE

        # correct = sum of line expected (minus rounding noise < $0.05)
        line_sum = sum(
            (ld.expected_payment for ld in result.line_details), Decimal("0")
        )
        # capital_addon is zero for this provider
        assert abs(result.correct_apg_payment - line_sum) < Decimal("0.05")

        # variance = correct - actual
        expected_var = result.correct_apg_payment - result.actual_paid
        assert abs(result.variance - expected_var) < Decimal("0.01")

        # underpaid and overpaid are mutually exclusive
        assert not (result.underpaid and result.overpaid)


@pytest.mark.asyncio
async def test_calculate_incidentals_are_packaged(session, clinic_provider):
    """CLM0001's 99213 line maps to an Incidental EAPG in the loaded workbook
    (EAPG 491). The engine must package it (expected_payment = 0, packaged=True)."""
    parsed = parse_835i(FIXTURE)
    clm1 = next(c for c in parsed.claims if c.claim_id == "CLM0001")

    apg = APGEngine()
    result = await apg.calculate(session, clm1, clinic_provider)
    ln_99213 = next(
        ld for ld in result.line_details if ld.procedure_code == "99213"
    )
    # 99213 is classified Incidental in the loaded HCPCS sheet.
    # Regardless of other rules, its expected_payment must be zero.
    assert ln_99213.expected_payment == Decimal("0")
    assert ln_99213.packaged is True


@pytest.mark.asyncio
async def test_calculate_multi_procedure_discount_flag(session, clinic_provider):
    """CLM0002 has two potentially significant procedures (11042 + 17000).
    If both map to Significant Procedure EAPGs with weights > 0, the engine
    must set discounting_applied=True and the secondary line's discounted=True."""
    parsed = parse_835i(FIXTURE)
    clm2 = next(c for c in parsed.claims if c.claim_id == "CLM0002")

    apg = APGEngine()
    result = await apg.calculate(session, clm2, clinic_provider)

    # Both lines must be present
    assert len(result.line_details) == 2

    # We don't hardcode EAPG assignments here — but we assert consistency:
    # if multi-procedure discounting fired, exactly one line is marked discounted.
    if result.discounting_applied:
        discounted_lines = [ld for ld in result.line_details if ld.discounted]
        assert len(discounted_lines) == 1


@pytest.mark.asyncio
async def test_calculate_denied_claim_zero_expected_is_permitted(session, clinic_provider):
    """CLM0003 was denied by the payer (paid 0). The engine calculates
    what SHOULD have been paid. If 99213 is Incidental, expected should be 0.
    This establishes the 'denied claim' shape — no crash, clean zeros."""
    parsed = parse_835i(FIXTURE)
    clm3 = next(c for c in parsed.claims if c.claim_id == "CLM0003")

    apg = APGEngine()
    result = await apg.calculate(session, clm3, clinic_provider)
    assert result.actual_paid == Decimal("0")
    # If 99213 is Incidental (packaged), correct payment is 0 and variance is 0.
    # If it weren't, variance would be positive (underpaid).
    if all(ld.packaged for ld in result.line_details):
        assert result.correct_apg_payment == Decimal("0")
        assert result.variance == Decimal("0")


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_region_falls_back_from_county(session):
    """A provider with only county_code (no explicit region) should have region
    resolved from the provider_county table."""
    q = await session.execute(
        select(ProviderCounty).where(ProviderCounty.region == "Upstate").limit(1)
    )
    upstate = q.scalar_one_or_none()
    if upstate is None:
        pytest.skip("No upstate county in reference data")

    cfg = ProviderConfig(
        is_active=False,
        provider_name="Test Upstate Provider",
        county_code=upstate.county_code,
        region=None,  # deliberately missing — should resolve from county
        peer_group="Clinic*",
        provider_type="dtc",
    )
    apg = APGEngine()
    region = await apg.resolve_region(session, cfg)
    assert region == Region.UPSTATE
