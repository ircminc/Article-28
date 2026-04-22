"""Tests for the APG engine's pricing ladder (introduced with the native
NYS DOH / eMedNY file loaders):

    Priority 1: Fee Schedule flat reimbursement (bypasses APG formula)
    Priority 2: Px-Based Weight override (uses base_rate × px_weight)
    Priority 3: APG-level weight (existing behavior)

Also exercises the v3.18 EAPG type coercion so the engine correctly routes
richer type names (e.g. "Radiologic Procedure", "Diagnostic or Therapeutic
Proc") into the canonical five categories that packaging/discounting use.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from backend.db.database import (
    ApgBaseRate,
    ApgWeight,
    Base,
    FeeScheduleItem,
    HcpcsToEapg,
    ProviderConfig,
    PxBasedWeight,
    async_session,
    engine,
)
from backend.engines.apg_engine import APGEngine, _coerce_eapg_type
from backend.models.schemas import (
    EapgType, FileType, ParsedClaim, Region, ServiceLine,
)


# ---------------------------------------------------------------------------
# _coerce_eapg_type — pure function tests, no DB required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Legacy canonical names
        ("Significant Procedure", EapgType.SIGNIFICANT_PROCEDURE),
        ("Medical Visit",         EapgType.MEDICAL_VISIT),
        ("Ancillary",             EapgType.ANCILLARY),
        ("Incidental",            EapgType.INCIDENTAL),
        ("Add-On",                EapgType.ADD_ON),
        ("Add On",                EapgType.ADD_ON),
        # v3.18 expanded taxonomy -> canonical five
        ("Radiologic Procedure",           EapgType.SIGNIFICANT_PROCEDURE),
        ("Diagnostic or Therapeutic Proc", EapgType.SIGNIFICANT_PROCEDURE),
        ("Dental or Oral Surgery Procs",   EapgType.SIGNIFICANT_PROCEDURE),
        ("Physical Therapy & Rehab",       EapgType.SIGNIFICANT_PROCEDURE),
        ("Behavioral Health & Counseling", EapgType.SIGNIFICANT_PROCEDURE),
        ("Per Diem",                       EapgType.MEDICAL_VISIT),
        ("Drug",                           EapgType.ANCILLARY),
        ("DME",                            EapgType.ANCILLARY),
        ("Unassigned",                     EapgType.UNKNOWN),
        # Case-insensitive
        ("RADIOLOGIC PROCEDURE",           EapgType.SIGNIFICANT_PROCEDURE),
        ("significant procedure",          EapgType.SIGNIFICANT_PROCEDURE),
        # Unknowns
        (None,                             EapgType.UNKNOWN),
        ("",                               EapgType.UNKNOWN),
        ("FooBar",                         EapgType.UNKNOWN),
    ],
)
def test_coerce_eapg_type(raw, expected):
    assert _coerce_eapg_type(raw) == expected


# ---------------------------------------------------------------------------
# Priority ladder — DB-backed engine tests
# ---------------------------------------------------------------------------

# Use procedure codes extremely unlikely to appear in the real crosswalk so
# we don't collide with seeded HCPCS/weights in the shared dev DB.
FS_CODE = "ZZFS01"      # Fee Schedule only
PX_CODE = "ZZPX01"      # Px-weight override
APG_CODE = "ZZAPG01"    # Normal APG-weight path
DOS = date(2024, 6, 15)
FAKE_APG = 99901        # out-of-range APG so we don't step on real weights
BASE_RATE = Decimal("200.00")


@pytest_asyncio.fixture()
async def ladder_session():
    """Seed just enough data to exercise the pricing ladder, clean up after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as s:
        # Base rate — Clinic*/Downstate/dtc
        # Only seed if not already present; real dev DB typically has this.
        existing = await s.execute(
            select(ApgBaseRate).where(
                ApgBaseRate.source == "dtc",
                ApgBaseRate.peer_group == "Clinic*",
                ApgBaseRate.region == "Downstate",
                ApgBaseRate.effective_date <= DOS,
            ).limit(1)
        )
        seeded_base_rate = False
        if existing.scalar_one_or_none() is None:
            s.add(ApgBaseRate(
                source="dtc", peer_group="Clinic*", region="Downstate",
                effective_date=date(2015, 4, 1), rate=BASE_RATE,
            ))
            seeded_base_rate = True

        # HCPCS → EAPG for each test code. Use Significant Procedure so the
        # APG path would naturally pay base_rate × weight without packaging.
        for code in (FS_CODE, PX_CODE, APG_CODE):
            s.add(HcpcsToEapg(
                hcpcs=code, eapg=FAKE_APG, eapg_desc="Test EAPG",
                eapg_type="Significant Procedure", eapg_category="Test",
                quarter_effective_date=date(2020, 1, 1),
                quarter_end_date=date(9999, 12, 31),
            ))

        # One shared APG weight for FAKE_APG so the priority-3 path has a weight
        s.add(ApgWeight(
            apg=FAKE_APG, apg_description="Test EAPG",
            effective_date=date(2020, 1, 1),
            weight=Decimal("1.0000"),
            is_final_rate=False,
        ))

        # Fee schedule row — only covers FS_CODE
        s.add(FeeScheduleItem(
            hcpcs=FS_CODE, description="Flat fee test",
            effective_date=date(2020, 1, 1),
            reimbursement=Decimal("150.00"),
            max_units=Decimal("2"),
        ))

        # Px weight — only covers PX_CODE
        s.add(PxBasedWeight(
            hcpcs=PX_CODE, description="Px weight test",
            effective_date=date(2020, 1, 1),
            weight=Decimal("3.5000"),
            units_limit=None,
        ))

        await s.commit()

    async with async_session() as s:
        yield s

    # Cleanup: remove the seeded rows
    async with async_session() as s:
        await s.execute(delete(HcpcsToEapg).where(HcpcsToEapg.hcpcs.in_(
            [FS_CODE, PX_CODE, APG_CODE]
        )))
        await s.execute(delete(ApgWeight).where(ApgWeight.apg == FAKE_APG))
        await s.execute(delete(FeeScheduleItem).where(FeeScheduleItem.hcpcs == FS_CODE))
        await s.execute(delete(PxBasedWeight).where(PxBasedWeight.hcpcs == PX_CODE))
        if seeded_base_rate:
            await s.execute(delete(ApgBaseRate).where(
                ApgBaseRate.source == "dtc",
                ApgBaseRate.peer_group == "Clinic*",
                ApgBaseRate.region == "Downstate",
                ApgBaseRate.rate == BASE_RATE,
                ApgBaseRate.effective_date == date(2015, 4, 1),
            ))
        await s.commit()


def _provider() -> ProviderConfig:
    return ProviderConfig(
        is_active=True,
        provider_name="Ladder Test Clinic",
        npi="0000000000",
        county_code=None,
        region="Downstate",
        peer_group="Clinic*",
        provider_type="dtc",
        capital_addon_eligible=False,
    )


def _claim(code: str, units: int = 1, paid: str = "0") -> ParsedClaim:
    return ParsedClaim(
        file_type=FileType.ERA_835I,
        claim_id=f"TEST-{code}",
        date_of_service=DOS,
        paid_amount=Decimal(paid),
        principal_diagnosis=None,
        service_lines=[
            ServiceLine(
                line_seq=1,
                procedure_code=code,
                modifiers=[],
                units=units,
                paid_amount=Decimal(paid),
                allowed_amount=Decimal(paid),
            )
        ],
    )


@pytest.mark.asyncio
async def test_priority1_fee_schedule_bypasses_apg(ladder_session):
    """A HCPCS on the Fee Schedule pays reimbursement × units regardless of
    the APG weight that would otherwise apply."""
    engine_ = APGEngine()
    # 2 units at $150 each, well under the max_units=2 cap
    result = await engine_.calculate(ladder_session, _claim(FS_CODE, units=2), _provider())

    assert len(result.line_details) == 1
    ld = result.line_details[0]
    # 150 * 2 = 300. With just the APG weight path this would be 200 * 1 = 200.
    assert ld.expected_payment == Decimal("300.00")
    assert result.correct_apg_payment == Decimal("300.00")
    assert any("Fee Schedule applied" in n for n in ld.notes)
    assert not ld.packaged
    assert not ld.discounted


@pytest.mark.asyncio
async def test_priority1_fee_schedule_caps_at_max_units(ladder_session):
    """max_units clamps the multiplier even when the claim bills more."""
    engine_ = APGEngine()
    result = await engine_.calculate(ladder_session, _claim(FS_CODE, units=10), _provider())
    ld = result.line_details[0]
    # Clamped to max_units=2 -> 150 * 2 = 300
    assert ld.expected_payment == Decimal("300.00")


@pytest.mark.asyncio
async def test_priority2_px_weight_overrides_apg_weight(ladder_session):
    """A HCPCS with a Px-based weight uses base_rate × px_weight, not the
    APG-level weight. Fee Schedule is NOT present for this code."""
    engine_ = APGEngine()
    result = await engine_.calculate(ladder_session, _claim(PX_CODE), _provider())
    ld = result.line_details[0]
    # base_rate (200) * px_weight (3.5) = 700.00  — not 200 * 1.0 = 200
    assert ld.expected_payment == Decimal("700.00")
    assert any("Px-Based Weight applied" in n for n in ld.notes)


@pytest.mark.asyncio
async def test_priority3_apg_weight_fallback(ladder_session):
    """With no Fee Schedule or Px weight hit, pricing falls back to the
    classic APG formula: base_rate × APG weight."""
    engine_ = APGEngine()
    result = await engine_.calculate(ladder_session, _claim(APG_CODE), _provider())
    ld = result.line_details[0]
    # base_rate (200) * APG weight (1.0) = 200.00
    assert ld.expected_payment == Decimal("200.00")
    # No fee-schedule or px notes
    assert not any("Fee Schedule" in n for n in ld.notes)
    assert not any("Px-Based Weight" in n for n in ld.notes)
